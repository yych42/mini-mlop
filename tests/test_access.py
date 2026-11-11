"""API-key access levels, project-bound keys, and encryption of stored secrets."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest
from conftest import Api, _serve

from mini_mlop_server import agent
from mini_mlop_server.secretbox import SecretBox, SecretError, is_encrypted
from mini_mlop_server.store import Store


def lines(n):
    return [{"time": 1_700_000_000_000 + i, "step": i, "data": {"loss": float(i)}} for i in range(n)]


@pytest.fixture
def team(server, api):
    """An admin, two projects with a run each, and keys of every kind."""
    a = api.new_run(project="alpha", name="a1")
    b = api.new_run(project="beta", name="b1")
    api.ingest("metrics", a, lines(5))
    api.ingest("metrics", b, lines(5))

    def key(**kw):
        status, made = api.post("/api/keys", {"name": "k", **kw})
        assert status == 200, made
        return Api(server.url, made["key"])

    return {
        "admin": api, "a": a, "b": b,
        "alpha_write": key(project="alpha"),
        "alpha_read": key(project="alpha", access="read"),
        "reader": key(access="read"),
        "writer": key(access="write"),
    }


# -- access levels -------------------------------------------------------------


def test_key_kinds(team, server):
    assert team["admin"].get("/api/me")[1]["access"] == "admin"
    me = team["alpha_write"].get("/api/me")[1]
    assert (me["access"], me["project"]) == ("write", "alpha")
    assert team["reader"].get("/api/me")[1] == {**team["reader"].get("/api/me")[1], "access": "read", "project": None}
    listed = {(k["access"], k["project"]) for k in team["admin"].get("/api/keys")[1]["keys"]}
    assert listed == {("admin", None), ("write", "alpha"), ("read", "alpha"), ("read", None), ("write", None)}
    assert team["admin"].post("/api/keys", {"name": "x", "project": "alpha", "access": "admin"})[0] == 400
    assert team["admin"].post("/api/keys", {"name": "x", "access": "owner"})[0] == 400


@pytest.mark.parametrize("who", ["alpha_write", "alpha_read", "reader", "writer"])
def test_only_admins_manage_the_server(team, who):
    k = team[who]
    for method, path, body in (
        ("GET", "/api/keys", None),
        ("POST", "/api/keys", {"name": "more"}),
        ("DELETE", "/api/keys/1", None),
        ("PUT", "/api/agent/config", {"llm": {"provider": "openrouter"}}),
        ("GET", "/api/agent/models", None),
        ("GET", "/api/stats", None),
        ("DELETE", "/api/projects/alpha", None),
    ):
        status, out = k.request(method, path, body)
        assert status == 403 and "admin" in out["error"], (method, path)
    assert k.get("/api/agent/config")[1] == {"ready": False}, "no provider details for non-admins"


@pytest.mark.parametrize("who", ["alpha_read", "reader"])
def test_read_keys_cannot_change_anything(team, who):
    k = team[who]
    a = team["a"]
    assert k.post("/api/runs/create", {"projectName": "alpha", "runName": "x"})[0] == 403
    assert k.ingest("metrics", a, lines(1))[0] == 403
    assert k.ingest("logs", a, [])[0] == 403
    assert k.post("/api/runs/status/update", {"runId": a, "status": "FAILED"})[0] == 403
    assert k.post("/files", {"files": []}, headers={"X-Run-Id": str(a)})[0] == 403
    assert k.post(f"/api/runs/{a}/stop")[0] == 403
    assert k.delete(f"/api/runs/{a}")[0] == 403
    # ... but they can look
    assert k.get(f"/api/runs/{a}")[1]["status"] == "RUNNING"
    assert k.get(f"/api/runs/{a}/metrics?name=loss")[1]["loss"]["count"] == 5
    assert k.post("/api/query", {"sql": "SELECT count(*) FROM runs WHERE project = 'alpha'"})[1]["rows"] == [[1]]


def test_unbound_write_key_logs_anywhere_but_is_not_admin(team):
    k = team["writer"]
    assert k.new_run(project="gamma", name="g1")
    assert k.ingest("metrics", team["b"], lines(1))[0] == 200
    assert k.delete(f"/api/runs/{team['b']}")[0] == 200
    assert k.delete("/api/projects/alpha")[0] == 403


# -- project-bound keys ----------------------------------------------------------


def test_project_key_logs_only_to_its_project(team):
    k = team["alpha_write"]
    new = k.new_run(project="alpha", name="a2")
    assert k.ingest("metrics", new, lines(3))[0] == 200
    status, out = k.post("/api/runs/create", {"projectName": "beta", "runName": "sneaky"})
    assert status == 403 and "can only log to the project 'alpha'" in out["error"]
    b = team["b"]
    assert k.ingest("metrics", b, lines(1))[0] == 404
    assert k.ingest("logs", b, [])[0] == 404
    assert k.post("/api/runs/status/update", {"runId": b, "status": "FAILED"})[0] == 404
    assert k.post("/api/runs/trigger", {"runId": b})[0] == 404
    assert k.post("/files", {"files": []}, headers={"X-Run-Id": str(b)})[0] == 404
    assert k.post(f"/api/runs/{b}/stop")[0] == 404
    assert k.delete(f"/api/runs/{b}")[0] == 404
    assert team["admin"].get(f"/api/runs/{b}")[1]["status"] == "RUNNING", "beta untouched"
    assert team["admin"].get(f"/api/runs/{b}/metrics?name=loss")[1]["loss"]["count"] == 5
    # within its project it can stop and delete
    assert k.post(f"/api/runs/{new}/stop")[0] == 200
    assert k.delete(f"/api/runs/{new}")[0] == 200


@pytest.mark.parametrize("who", ["alpha_write", "alpha_read"])
def test_project_key_sees_only_its_project(team, server, who):
    k, a, b = team[who], team["a"], team["b"]
    assert [p["name"] for p in k.get("/api/projects")[1]["projects"]] == ["alpha"]
    assert k.get("/api/projects/alpha/runs")[0] == 200
    assert k.get("/api/projects/beta/runs")[0] == 404
    for tail in ("", "/metrics?name=loss", "/data?name=x", "/files?name=x", "/logs", "/graph"):
        if "name=x" not in tail:  # series x does not exist anywhere
            assert k.get(f"/api/runs/{a}{tail}")[0] == 200, tail
        assert k.get(f"/api/runs/{b}{tail}")[0] == 404, tail
    assert k.get("/api/runs/99999")[0] == 404

    sql = "SELECT project, name FROM runs ORDER BY id"
    assert k.post("/api/query", {"sql": sql})[1]["rows"] == [["alpha", "a1"]], "confined without asking"
    assert k.post("/api/query", {"sql": sql, "project": "alpha"})[1]["rows"] == [["alpha", "a1"]]
    assert k.post("/api/query", {"sql": sql, "project": "beta"})[0] == 403
    assert k.post("/api/query", {"sql": "SELECT * FROM main.runs"})[0] == 400

    def tool(name, **args):
        return k.request("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })[1]["result"]

    assert tool("projects")["content"][0]["text"].splitlines()[1].startswith("alpha,")
    assert "beta" not in tool("projects")["content"][0]["text"]
    assert "beta" not in tool("query", sql=sql)["content"][0]["text"]
    assert tool("query", sql=sql, project="beta")["isError"] is True


def test_project_key_cannot_reach_other_projects_files(team, server):
    admin, b = team["admin"], team["b"]
    _, urls = admin.post(
        "/files",
        {"files": [{"fileName": "s.txt", "fileSize": 3, "fileType": "txt", "logName": "note"}]},
        headers={"X-Run-Id": str(b)},
    )
    path = urls["note"][0]["s.txt"]
    assert admin.request("PUT", path, b"abc")[0] == 200
    for who in ("alpha_write", "alpha_read"):
        assert team[who].request("GET", path, raw=True)[0] == 404
        assert team[who].request("PUT", path, b"xyz")[0] in (403, 404)
    assert admin.request("GET", path, raw=True)[1] == b"abc"


def test_project_key_conversations_stay_in_the_project(team, server):
    admin = team["admin"]
    beta_chat = server.store.create_chat("about beta", "beta")
    all_chat = server.store.create_chat("everything", None)
    alpha_chat = server.store.create_chat("about alpha", "alpha")
    k = team["alpha_read"]
    assert [c["id"] for c in k.get("/api/chats")[1]["chats"]] == [alpha_chat]
    assert k.get(f"/api/chats/{beta_chat}")[0] == 404 and k.get(f"/api/chats/{all_chat}")[0] == 404
    assert k.delete(f"/api/chats/{alpha_chat}")[0] == 403, "read keys cannot delete"
    assert team["alpha_write"].delete(f"/api/chats/{beta_chat}")[0] == 404
    for body in ({"message": "hi", "project": "beta"}, {"message": "hi", "chat_id": beta_chat}):
        assert k.request("POST", "/api/chat", body, raw=True)[0] in (403, 404)
    # a new conversation from a project key lands in its project, whatever it asks for
    status, data, _ = k.request("POST", "/api/chat", {"message": "hi", "project": None}, raw=True)
    assert status == 200 and json.loads(data.decode().splitlines()[0])["project"] == "alpha"
    assert len(admin.get("/api/chats")[1]["chats"]) == 4


def test_client_with_project_key(team, server, tmp_path):
    import mini_mlop as mlop

    key = team["alpha_write"].key
    run = mlop.init(dir=str(tmp_path), project="alpha", name="from-client",
                    settings={"url": server.url, "_auth": key})
    run.log({"loss": 1.0})
    run.finish()
    names = [r["name"] for r in team["admin"].get("/api/projects/alpha/runs")[1]["runs"]]
    assert "from-client" in names
    with pytest.raises(Exception, match="can only log to the project 'alpha'"):
        mlop.init(dir=str(tmp_path), project="beta", settings={"url": server.url, "_auth": key})


def test_cli_creates_scoped_keys(tmp_path):
    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "mini_mlop_server", "--data", str(tmp_path), "keys", *args],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )

    made = cli("create", "ci", "--project", "alpha")
    assert made.stdout.startswith("mini_mlop_") and "write, project alpha" in made.stderr
    assert "read, all projects" in cli("create", "dash", "--access", "read").stderr
    assert cli("create", "bad", "--project", "alpha", "--access", "admin").returncode != 0
    listing = cli("list").stdout
    assert "write  alpha" in listing and "read   all projects" in listing


def test_rotating_a_key_replaces_its_secret(team):
    admin, old = team["admin"], team["alpha_write"]
    key_id = next(k["id"] for k in admin.get("/api/keys")[1]["keys"] if k["project"] == "alpha"
                  and k["access"] == "write")
    status, made = admin.post(f"/api/keys/{key_id}/rotate")
    assert status == 200 and made["id"] == key_id and not made["current"]
    assert (made["name"], made["access"], made["project"]) == ("k", "write", "alpha")
    assert made["key"] != old.key
    assert old.get("/api/me")[0] == 401, "the old secret stops working"
    assert Api(old.url, made["key"]).get("/api/me")[1]["project"] == "alpha"
    assert admin.post("/api/keys/9999/rotate")[0] == 404
    assert team["writer"].post(f"/api/keys/{key_id}/rotate")[0] == 403, "admins only"


def test_rotating_the_signed_in_key_moves_the_session(server):
    anon = Api(server.url)
    _, _, headers = anon.request("POST", "/api/login", {"key": server.key}, raw=True)
    session = {"Cookie": headers["Set-Cookie"].split(";")[0]}
    me = anon.get("/api/keys", headers=session)[1]
    status, data, headers = anon.request(
        "POST", f"/api/keys/{me['current']}/rotate", {}, headers=session, raw=True
    )
    made = json.loads(data)
    assert status == 200 and made["current"] and made["access"] == "admin"
    assert anon.get("/api/me", headers=session)[0] == 401, "old cookie is dead"
    fresh = {"Cookie": headers["Set-Cookie"].split(";")[0]}
    assert anon.get("/api/me", headers=fresh)[0] == 200, "new cookie works"


def test_cli_rotates_keys(tmp_path):
    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "mini_mlop_server", "--data", str(tmp_path), "keys", *args],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )

    first = cli("create", "seed").stdout.strip()
    rotated = cli("rotate", "1")
    new = rotated.stdout.strip()
    assert new.startswith("mini_mlop_") and new != first and "rotated" in rotated.stderr
    store = Store(str(tmp_path), readers=1)
    try:
        assert store.check_key(first) is None and store.check_key(new)["name"] == "seed"
    finally:
        store.close()
    assert cli("rotate", "42").returncode != 0


def test_existing_keys_become_admins(tmp_path):
    data = tmp_path / "old"
    store = Store(str(data))
    key = store.create_key("legacy")["key"]
    store.close()
    db = sqlite3.connect(str(data / "mini-mlop.db"))
    db.executescript(
        "ALTER TABLE api_keys DROP COLUMN access; ALTER TABLE api_keys DROP COLUMN project;"
        "UPDATE meta SET value = '3' WHERE key = 'schema';"
    )
    db.close()
    store = Store(str(data))
    assert store.check_key(key)["access"] == "admin" and store.check_key(key)["project"] is None
    store.close()


# -- encryption of stored provider secrets ---------------------------------------


def test_secretbox_round_trip(tmp_path):
    box = SecretBox(str(tmp_path), "correct horse")
    sealed = box.encrypt("sk-or-v1-abcdef")
    assert is_encrypted(sealed) and "sk-or" not in sealed
    assert box.encrypt("sk-or-v1-abcdef") != sealed, "a fresh nonce every time"
    assert box.decrypt(sealed) == "sk-or-v1-abcdef"
    assert SecretBox(str(tmp_path), "correct horse").decrypt(sealed) == "sk-or-v1-abcdef"
    assert box.decrypt("") == "" and box.encrypt("") == ""
    assert box.decrypt("plain-old-value") == "plain-old-value"
    with pytest.raises(SecretError):
        SecretBox(str(tmp_path), "wrong secret").decrypt(sealed)
    tampered = sealed[:-6] + ("AAAAAA" if not sealed.endswith("AAAAAA") else "BBBBBB")
    with pytest.raises(SecretError):
        box.decrypt(tampered)
    with pytest.raises(SecretError):
        SecretBox(str(tmp_path)).decrypt(sealed)  # key file instead of the secret


def test_provider_keys_are_encrypted_at_rest(tmp_path):
    def start(secret):
        server, thread = _serve(tmp_path, secret=secret)
        api = Api(f"http://127.0.0.1:{server.server_address[1]}", server.store.create_key("t")["key"])
        return server, thread, api

    def stop(server, thread):
        server.shutdown()
        thread.join()
        server.close()

    def on_disk():
        blob = b""
        root = tmp_path / "data"
        for name in os.listdir(root):
            if name.startswith("mini-mlop.db"):
                blob += (root / name).read_bytes()
        return blob

    server, thread, api = start("s3cret-one")
    body = api.request("PUT", "/api/agent/config", {
        "llm": {"provider": "openrouter", "api_key": "sk-or-v1-PLAINTEXT-KEY", "model": "a/b"},
        "sandbox": {"provider": "http", "url": "http://runner", "token": "RUNNER-TOKEN-VALUE"},
    })[1]
    assert body["encryption"] == "env" and body["unreadable"] == []
    assert agent.load_config(server.store)["llm"]["api_key"] == "sk-or-v1-PLAINTEXT-KEY"
    stop(server, thread)
    assert b"PLAINTEXT-KEY" not in on_disk() and b"RUNNER-TOKEN-VALUE" not in on_disk()
    assert b"enc:v1:" in on_disk()

    # same secret: readable again after a restart
    server, thread, api = start("s3cret-one")
    assert agent.load_config(server.store)["sandbox"]["token"] == "RUNNER-TOKEN-VALUE"
    stop(server, thread)

    # different secret: reported, not silently treated as "no key"
    server, thread, api = start("another-secret")
    cfg = api.get("/api/agent/config")[1]
    assert cfg["unreadable"] == ["llm.api_key", "sandbox.token"]
    status, data, _ = api.request("POST", "/api/chat", {"message": "hi"}, raw=True)
    assert "can no longer be decrypted" in data.decode()
    # saving something unrelated keeps the ciphertext for when the secret comes back
    api.request("PUT", "/api/agent/config", {"llm": {"model": "c/d"}})
    stop(server, thread)
    server, thread, api = start("s3cret-one")
    cfg = agent.load_config(server.store)
    assert cfg["llm"]["api_key"] == "sk-or-v1-PLAINTEXT-KEY" and cfg["llm"]["model"] == "c/d"
    stop(server, thread)


def test_plaintext_secrets_from_older_versions_are_encrypted_on_start(tmp_path):
    store = Store(str(tmp_path / "data"))
    store.set_setting("agent", {"llm": {"provider": "openrouter", "api_key": "sk-OLD-PLAIN", "model": "m"}})
    store.close()
    server, thread = _serve(tmp_path)
    try:
        raw = server.store.get_setting("agent")["llm"]["api_key"]
        assert is_encrypted(raw) and "OLD-PLAIN" not in raw
        assert agent.load_config(server.store)["llm"]["api_key"] == "sk-OLD-PLAIN"
        assert server.store.box.source == "keyfile"
        assert oct(os.stat(tmp_path / "data" / "secret.key").st_mode & 0o777) == "0o600"
    finally:
        server.shutdown()
        thread.join()
        server.close()
