"""The agent stack: model providers, sandboxes, the chat loop and MCP.

Model providers are played by small local servers that speak the real wire
formats, so everything below runs offline.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mini_mlop_server import agent, llm, sandbox
from mini_mlop_server.sandbox_runner import make_runner


class FakeProvider:
    """Plays back scripted model replies and records what it was sent."""

    def __init__(self, wire):
        self.wire, self.replies, self.requests = wire, [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({
                    "path": self.path, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                key = self.headers.get("x-api-key") or self.headers.get("Authorization", "")[7:]
                if key != "sk-test":
                    return self._send(401, {"error": {"message": "bad key"}})
                self._send(200, fake.replies.pop(0))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def say(self, text="", calls=()):
        """Queue one assistant reply in this provider's wire format."""
        if self.wire == "openai":
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [
                    {"id": i, "type": "function",
                     "function": {"name": n, "arguments": json.dumps(a)}}
                    for i, n, a in calls
                ]
            self.replies.append({
                "choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            })
        else:
            blocks = [{"type": "thinking", "thinking": "", "signature": "sig"}]
            if text:
                blocks.append({"type": "text", "text": text})
            blocks += [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls]
            self.replies.append({
                "content": blocks, "stop_reason": "tool_use" if calls else "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            })

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(params=["openai", "anthropic"])
def provider(request):
    fake = FakeProvider(request.param)
    yield fake
    fake.close()


def configure(api, fake, **sandbox_cfg):
    provider = "custom-openai" if fake.wire == "openai" else "custom-anthropic"
    status, body = api.request("PUT", "/api/agent/config", {
        "llm": {"provider": provider, "base_url": fake.url, "api_key": "sk-test", "model": "model-a"},
        "sandbox": sandbox_cfg or {"provider": "none"},
    })
    assert status == 200, body
    return body


def ask(api, message, **extra):
    status, data, _ = api.request("POST", "/api/chat", {"message": message, **extra}, raw=True)
    assert status == 200, data
    return [json.loads(line) for line in data.decode().splitlines()]


@pytest.fixture
def runs(api):
    a = api.new_run(project="mnist", name="base", config={"lr": 0.1})
    b = api.new_run(project="mnist", name="tuned", config={"lr": 0.01})
    for run, scale in ((a, 1.0), (b, 2.0)):
        api.ingest("metrics", run, [
            {"time": 1_700_000_000_000 + i, "step": i, "data": {"loss": i * scale}}
            for i in range(50)
        ])
    return a, b


# -- configuration -------------------------------------------------------------


def test_agent_routes_need_a_key(server):
    from conftest import Api

    anon = Api(server.url)
    for method, path in (("GET", "/api/agent/config"), ("POST", "/api/chat"),
                         ("GET", "/api/chats"), ("POST", "/mcp")):
        assert anon.request(method, path, {} if method == "POST" else None)[0] == 401


def test_provider_key_is_stored_but_never_returned(api, server, provider):
    body = configure(api, provider)
    assert body["ready"] is True
    assert "api_key" not in body["config"]["llm"]
    assert body["config"]["llm"]["api_key_hint"] == "…st"
    assert "sk-test" not in json.dumps(body)
    assert {p["id"] for p in body["providers"]} >= {"openrouter", "openai", "anthropic"}
    # an update that omits the key keeps it
    api.request("PUT", "/api/agent/config", {"llm": {"model": "model-b"}})
    stored = agent.load_config(server.store)["llm"]
    assert stored["api_key"] == "sk-test" and stored["model"] == "model-b"
    # ... and the query interface cannot reach it
    status, out = api.post("/api/query", {"sql": "SELECT * FROM settings"})
    assert status == 400 and "sk-test" not in json.dumps(out)


def test_openrouter_is_a_preset(api, server):
    api.request("PUT", "/api/agent/config", {"llm": {"provider": "openrouter", "api_key": "k", "model": "a/b"}})
    cfg = llm.resolve(agent.load_config(server.store)["llm"])
    assert cfg["wire"] == "openai" and cfg["base_url"] == "https://openrouter.ai/api/v1"


def test_bad_config_is_refused(api):
    assert api.request("PUT", "/api/agent/config", {"llm": {"provider": "nope"}})[0] == 400
    assert api.request("PUT", "/api/agent/config", {"sandbox": {"provider": "nope"}})[0] == 400


def test_model_list_comes_from_the_provider(api, provider):
    configure(api, provider)
    assert api.get("/api/agent/models") == (200, {"models": ["model-a", "model-b"]})


def test_unconfigured_agent_explains_itself(api):
    events = ask(api, "hello")
    assert events[-2]["type"] == "error" and "Settings" in events[-2]["text"]
    assert events[-1]["type"] == "done"


# -- the loop, over both wire formats -----------------------------------------


def test_agent_queries_then_answers(api, server, provider, runs):
    a, _ = runs
    configure(api, provider)
    sql = "SELECT r.name, max(m.value) AS top FROM runs r JOIN metrics m ON m.run_id = r.id GROUP BY r.id ORDER BY r.id"
    provider.say("Let me look.", calls=[("call_1", "query", {"sql": sql})])
    provider.say("**tuned** peaks higher (98 vs 49).")

    events = ask(api, "which run peaks higher?", context={"route": f"#/runs/{a}"})
    kinds = [e["type"] for e in events]
    assert kinds == ["chat", "text", "tool_call", "tool_result", "text", "done"]
    assert events[2]["input"] == {"sql": sql} and events[3]["summary"] == "2 rows"
    assert events[4]["text"].startswith("**tuned**")
    assert events[-1]["usage"] == {"input": 200, "output": 20}

    first, second = (r["body"] for r in provider.requests)
    blob = json.dumps(second)
    assert "base,49.0" in blob and "tuned,98.0" in blob, "the model was given the rows"
    assert f'looking at run {a} \\"base\\"' in json.dumps(first), "page context reached the model"
    if provider.wire == "openai":
        assert first["messages"][0]["role"] == "system"
        assert [t["function"]["name"] for t in first["tools"]] == ["query"]
        assert second["messages"][-1] == {
            "role": "tool", "tool_call_id": "call_1", "content": second["messages"][-1]["content"],
        }
        assert provider.requests[0]["headers"]["authorization"] == "Bearer sk-test"
    else:
        assert "query" in first["system"] and first["tools"][0]["input_schema"]["required"] == ["sql"]
        assert second["messages"][-1]["content"][0]["type"] == "tool_result"
        # the assistant turn is replayed exactly, thinking block included
        assert second["messages"][-2]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
        assert provider.requests[0]["headers"]["x-api-key"] == "sk-test"
        assert "temperature" not in first and "thinking" not in first

    # the conversation is saved, shown without provider payloads, and continues
    chat_id = events[0]["id"]
    _, saved = api.get(f"/api/chats/{chat_id}")
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "assistant"]
    assert saved["messages"][1]["tools"][0]["summary"] == "2 rows"
    assert "native" not in json.dumps(saved)
    provider.say("Yes.")
    ask(api, "sure?", chat_id=chat_id)
    assert len(provider.requests[-1]["body"]["messages"]) >= 5
    assert api.get("/api/chats")[1]["chats"][0]["id"] == chat_id
    assert api.delete(f"/api/chats/{chat_id}")[0] == 200
    assert api.get(f"/api/chats/{chat_id}")[0] == 404
    assert api.get("/api/agent/config")[1]["tokens_today"] == 330


def test_sql_errors_go_back_to_the_model(api, provider, runs):
    configure(api, provider)
    provider.say(calls=[("c1", "query", {"sql": "DELETE FROM runs"})])
    provider.say("I can only read.")
    events = ask(api, "delete everything")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] is True
    assert "read-only" in json.dumps(provider.requests[1]["body"])
    assert api.post("/api/query", {"sql": "SELECT count(*) FROM runs"})[1]["rows"] == [[2]]


def test_rejected_provider_key_is_reported(api, provider):
    configure(api, provider)
    api.request("PUT", "/api/agent/config", {"llm": {"api_key": "wrong"}})
    events = ask(api, "hi")
    assert events[-2]["type"] == "error" and "rejected the API key" in events[-2]["text"]


def test_step_and_daily_limits(api, server, provider, monkeypatch):
    configure(api, provider)
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    for i in range(2):
        provider.say(calls=[(f"c{i}", "query", {"sql": "SELECT 1"})])
    assert "stopped after 2 steps" in ask(api, "loop")[-2]["text"]
    api.request("PUT", "/api/agent/config", {"daily_token_limit": 10})
    assert "token limit" in ask(api, "again")[-2]["text"]
    assert len(provider.requests) == 2, "no model call once the limit is hit"


# -- sandboxes -------------------------------------------------------------------


def test_local_sandbox_runs_code_with_input_files():
    out = sandbox.execute(
        "import csv\nrows = list(csv.DictReader(open('d.csv')))\nprint(sum(float(r['v']) for r in rows))",
        {"d.csv": b"v\n1\n2.5\n"},
    )
    assert out["stdout"].strip() == "3.5" and out["exit_code"] == 0 and not out["timed_out"]


def test_sandbox_reports_failures_and_timeouts():
    out = sandbox.execute("import sys\nprint('partial')\nsys.exit(3)")
    assert out["exit_code"] == 3 and out["stdout"].strip() == "partial"
    out = sandbox.execute("raise ValueError('boom')")
    assert out["exit_code"] == 1 and "ValueError: boom" in out["stderr"]
    out = sandbox.execute("import time\nprint('start', flush=True)\ntime.sleep(30)", timeout=1)
    assert out["timed_out"] and out["exit_code"] is None and "start" in out["stdout"]


def test_sandbox_does_not_inherit_the_environment(monkeypatch):
    monkeypatch.setenv("SECRET_THING", "hunter2")
    out = sandbox.execute("import os\nprint(os.environ.get('SECRET_THING'), os.getcwd())")
    assert out["stdout"].startswith("None ") and "mlop-sandbox-" in out["stdout"]


def test_sandbox_output_is_capped_and_file_names_checked():
    out = sandbox.execute("print('x' * 500000)")
    assert len(out["stdout"]) < 70_000 and "truncated" in out["stdout"]
    for name in ("../evil.csv", "a/b.csv", "main.py", ""):
        with pytest.raises(sandbox.SandboxError):
            sandbox.execute("print(1)", {name: b""})


@pytest.fixture
def runner():
    server = make_runner("127.0.0.1", 0, "runner-token-123456")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_http_sandbox_against_the_reference_runner(runner):
    box = sandbox.make({"provider": "http", "url": runner, "token": "runner-token-123456"})
    out = box.run("print(open('in.txt').read().upper())", {"in.txt": b"hello"})
    assert out["stdout"].strip() == "HELLO" and out["exit_code"] == 0
    bad = sandbox.make({"provider": "http", "url": runner, "token": "wrong"})
    with pytest.raises(sandbox.SandboxError, match="rejected the token"):
        bad.run("print(1)")
    gone = sandbox.make({"provider": "http", "url": "http://127.0.0.1:9", "token": "x"})
    with pytest.raises(sandbox.SandboxError, match="could not reach"):
        gone.run("print(1)")
    assert sandbox.make({"provider": "none"}) is None and sandbox.make({}) is None


@pytest.mark.parametrize("kind", ["local", "http"])
def test_agent_runs_python_over_query_results(api, provider, runs, runner, kind):
    a, _ = runs
    cfg = {"provider": "local"} if kind == "local" else {
        "provider": "http", "url": runner, "token": "runner-token-123456"}
    body = configure(api, provider, **cfg)
    assert "token" not in body["config"]["sandbox"]
    code = "import csv\nv = [float(r['value']) for r in csv.DictReader(open('loss.csv'))]\nprint(len(v), sum(v) / len(v))"
    provider.say(calls=[("c1", "run_python", {
        "code": code,
        "inputs": [{"name": "loss.csv", "sql": f"SELECT step, value FROM metrics WHERE run_id = {a} AND name = 'loss'"}],
    })])
    provider.say("The mean loss is 24.5 over 50 points.")
    events = ask(api, "mean loss?")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] is False and result["summary"].startswith("ran in")
    sent = json.dumps(provider.requests[1]["body"])
    assert "50 24.5" in sent and "loss.csv: 50 rows" in sent
    tools = provider.requests[0]["body"]["tools"]
    names = [t.get("name") or t["function"]["name"] for t in tools]
    assert names == ["query", "run_python"]


# -- MCP -------------------------------------------------------------------------


def rpc(api, method, params=None, msg_id=1):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return api.request("POST", "/mcp", body)


def test_mcp_handshake_and_tools(api, runs):
    status, out = rpc(api, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert status == 200 and out["id"] == 1
    assert out["result"]["protocolVersion"] == "2025-06-18"
    assert out["result"]["serverInfo"]["name"] == "mini-mlop"
    assert "tools" in out["result"]["capabilities"]
    assert rpc(api, "initialize", {"protocolVersion": "1999-01-01"})[1]["result"]["protocolVersion"] == "2025-06-18"

    status, data, _ = api.request(
        "POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, raw=True)
    assert status == 202 and data == b""
    assert rpc(api, "ping")[1]["result"] == {}

    tools = rpc(api, "tools/list")[1]["result"]["tools"]
    assert [t["name"] for t in tools] == ["projects", "schema", "query"]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)

    schema = rpc(api, "tools/call", {"name": "schema", "arguments": {}})[1]["result"]
    assert "metrics" in schema["content"][0]["text"] and schema["isError"] is False

    out = rpc(api, "tools/call", {"name": "query", "arguments": {
        "sql": "SELECT name FROM runs WHERE project = ? ORDER BY id", "params": ["mnist"]}})[1]["result"]
    assert out["isError"] is False and out["content"][0]["text"].startswith("name\nbase\ntuned\n")

    refused = rpc(api, "tools/call", {"name": "query", "arguments": {"sql": "DROP TABLE runs"}})[1]["result"]
    assert refused["isError"] is True and "read-only" in refused["content"][0]["text"]


def test_mcp_protocol_errors(api):
    assert rpc(api, "tools/call", {"name": "nope"})[1]["error"]["code"] == -32602
    assert rpc(api, "resources/list")[1]["error"]["code"] == -32601
    status, out = api.request("POST", "/mcp", b"{not json")
    assert status == 400 and out["error"]["code"] == -32700
    status, out = api.request("POST", "/mcp", [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ])
    assert status == 200 and out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    assert api.request("GET", "/mcp", raw=True)[0] == 405


# -- project scope ---------------------------------------------------------------


def test_conversations_are_confined_to_a_project(api, server, provider):
    a = api.new_run(project="alpha", name="a1")
    b = api.new_run(project="beta", name="b1")
    configure(api, provider)
    everything = "SELECT project, name FROM runs ORDER BY id"

    # started from a page in project alpha: the model sees alpha only
    provider.say(calls=[("c1", "query", {"sql": everything})])
    provider.say("Only a1.")
    events = ask(api, "what runs are there?", context={"route": f"#/runs/{a}"})
    assert events[0]["project"] == "alpha"
    second = json.dumps(provider.requests[-1]["body"])
    assert "alpha,a1" in second and "beta" not in second
    first = json.dumps(provider.requests[-2]["body"])
    assert "confined to the project" in first and "alpha" in first

    # the scope sticks even when the user has wandered to another project
    provider.say(calls=[("c2", "query", {"sql": everything})])
    provider.say("Still only a1.")
    ask(api, "and now?", chat_id=events[0]["id"], context={"route": f"#/runs/{b}"})
    assert "b1" not in json.dumps(provider.requests[-1]["body"])

    # asked for explicitly
    provider.say(calls=[("c3", "query", {"sql": everything})])
    provider.say("b1.")
    events = ask(api, "runs?", project="beta", context={"route": f"#/runs/{a}"})
    assert events[0]["project"] == "beta"
    sent = json.dumps(provider.requests[-1]["body"])
    assert "beta,b1" in sent and "alpha" not in sent

    # all projects, on request
    provider.say(calls=[("c4", "query", {"sql": everything})])
    provider.say("Two.")
    events = ask(api, "runs?", project=None, context={"route": f"#/runs/{a}"})
    assert events[0]["project"] is None
    sent = json.dumps(provider.requests[-1]["body"])
    assert "alpha,a1" in sent and "beta,b1" in sent

    status, _, _ = api.request("POST", "/api/chat", {"message": "x", "project": "nope"}, raw=True)
    assert status == 400
    projects = {c["project"] for c in api.get("/api/chats")[1]["chats"]}
    assert projects == {"alpha", "beta", None}


def test_sandbox_inputs_respect_the_project(api, provider):
    api.new_run(project="alpha", name="a1")
    api.new_run(project="beta", name="b1")
    configure(api, provider, provider="local")
    provider.say(calls=[("c1", "run_python", {
        "code": "print(open('r.csv').read())",
        "inputs": [{"name": "r.csv", "sql": "SELECT project, name FROM runs"}],
    })])
    provider.say("done")
    ask(api, "go", project="alpha")
    sent = json.dumps(provider.requests[-1]["body"])
    assert "alpha,a1" in sent and "beta" not in sent


def test_mcp_lists_projects_and_confines_queries(api):
    call = lambda name, **args: rpc(api, "tools/call", {"name": name, "arguments": args})[1]["result"]  # noqa: E731
    assert "no projects yet" in call("projects")["content"][0]["text"]
    api.new_run(project="alpha", name="a1")
    api.new_run(project="alpha", name="a2")
    api.new_run(project="beta", name="b1")

    listing = call("projects")["content"][0]["text"].splitlines()
    assert listing[0] == "name,runs,running,last_active_utc"
    assert sorted(line.split(",")[:3] for line in listing[1:]) == [["alpha", "2", "2"], ["beta", "1", "1"]]

    sql = "SELECT project, name FROM runs ORDER BY id"
    everything = call("query", sql=sql)["content"][0]["text"]
    assert "alpha,a1" in everything and "beta,b1" in everything
    scoped = call("query", sql=sql, project="beta")
    assert scoped["isError"] is False
    text = scoped["content"][0]["text"]
    assert text.startswith("project,name\nbeta,b1\n") and "alpha" not in text
    assert "project 'beta' only" in text
    assert "name\nbeta\n" in call("query", sql="SELECT name FROM projects", project="beta")["content"][0]["text"]

    escape = call("query", sql="SELECT * FROM main.runs", project="beta")
    assert escape["isError"] is True and "alpha" not in escape["content"][0]["text"]
    missing = call("query", sql=sql, project="nope")
    assert missing["isError"] is True and "Call `projects`" in missing["content"][0]["text"]
