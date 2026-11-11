"""The server on its own, driven over plain HTTP."""

import http.client
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

import urllib.error

import pytest

from mini_mlop_server import store as store_module

from conftest import Api


def metric_lines(n, name="loss", start=0):
    return [
        {"time": 1_700_000_000_000 + i, "step": i, "data": {name: float(i)}}
        for i in range(start, start + n)
    ]


# -- authentication ----------------------------------------------------------


def test_health_is_public(server):
    status, body = Api(server.url).get("/api/health")
    assert status == 200 and body["status"] == "ok" and body["auth"] is True


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/projects"),
        ("GET", "/api/runs/1"),
        ("GET", "/api/files/1"),
        ("GET", "/api/keys"),
        ("POST", "/api/runs/create"),
        ("POST", "/ingest/metrics"),
        ("POST", "/files"),
        ("PUT", "/api/files/1"),
        ("DELETE", "/api/runs/1"),
    ],
)
def test_everything_else_needs_a_key(server, method, path):
    body = b"{}" if method in ("POST", "PUT") else None
    for key in (None, "mini_mlop_not-a-real-key"):
        status, _ = Api(server.url).request(method, path, body, key=key)
        assert status == 401, (method, path, key)


def test_keys_are_stored_hashed(server):
    with open(server.store.path, "rb") as f:
        assert server.key.encode() not in f.read()


def test_browser_session_cookie(server):
    anon = Api(server.url)
    status, _, headers = anon.request("POST", "/api/login", {"key": "nope"}, raw=True)
    assert status == 401 and "Set-Cookie" not in headers

    status, _, headers = anon.request("POST", "/api/login", {"key": server.key}, raw=True)
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    session = {"Cookie": cookie.split(";")[0]}

    status, me = anon.get("/api/me", headers=session)
    assert status == 200 and me["key"] == "test"

    # a page on another site cannot use the cookie to write
    status, _ = anon.post(
        "/api/keys", {"name": "x"}, headers={**session, "Origin": "http://evil.example"}
    )
    assert status == 403
    own = {**session, "Origin": server.url}
    assert anon.post("/api/keys", {"name": "x"}, headers=own)[0] == 200


def test_key_management(api, server):
    _, made = api.post("/api/keys", {"name": "laptop"})
    assert made["key"].startswith("mini_mlop_")
    assert Api(server.url, made["key"]).get("/api/projects")[0] == 200

    _, listing = api.get("/api/keys")
    assert [k["name"] for k in listing["keys"]] == ["test", "laptop"]
    assert all("key" not in k and "hash" not in k for k in listing["keys"])

    assert api.delete(f"/api/keys/{listing['current']}")[0] == 400  # not your own
    assert api.delete(f"/api/keys/{made['id']}")[0] == 200
    assert Api(server.url, made["key"]).get("/api/projects")[0] == 401


def test_no_auth_mode(open_server):
    anon = Api(open_server.url)
    assert anon.get("/api/health")[1]["auth"] is False
    run_id = anon.new_run()
    assert anon.ingest("metrics", run_id, metric_lines(3))[1] == {"ingested": 3}


# -- ingest ------------------------------------------------------------------


def test_metrics_ingest_and_read(api):
    run_id = api.new_run(config=json.dumps({"lr": 0.1}))
    assert api.ingest("metrics", run_id, metric_lines(50))[1] == {"ingested": 50}
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=loss&name=missing")
    assert m == {"loss": {"x": list(range(50)), "y": [float(i) for i in range(50)], "count": 50}}

    _, runs = api.get("/api/projects/proj/runs")
    assert runs["runs"][0]["config"] == {"lr": 0.1}
    assert runs["runs"][0]["metrics"] == ["loss"]


def test_resent_batches_do_not_duplicate_points(api):
    run_id = api.new_run()
    api.ingest("metrics", run_id, metric_lines(20))
    api.ingest("metrics", run_id, metric_lines(20))  # a client retry
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=loss")
    assert m["loss"]["count"] == 20


def test_non_finite_values_are_dropped_not_fatal(api):
    run_id = api.new_run()
    body = b'{"time":1,"step":1,"data":{"a":NaN,"b":Infinity,"c":2.5,"d":"x","e":true}}\n'
    status, out = api.post("/ingest/metrics", body, headers={"X-Run-Id": str(run_id)})
    assert status == 200 and out == {"ingested": 1}
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=a&name=c")
    assert m == {"c": {"x": [1], "y": [2.5], "count": 1}}


def test_large_series_is_downsampled_with_envelope(api):
    run_id = api.new_run()
    api.ingest("metrics", run_id, metric_lines(20_000))
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=loss&points=100")
    s = m["loss"]
    assert s["count"] == 20_000
    assert 90 <= len(s["x"]) <= 100
    assert s["x"] == sorted(s["x"]) and s["x"][-1] == 19_999
    assert s["min"][0] == 0.0 and s["max"][-1] == 19_999.0
    assert all(lo <= y <= hi for lo, y, hi in zip(s["min"], s["y"], s["max"]))
    # the buckets partition the data: bucket means average back to the mean
    assert sum(s["y"]) / len(s["y"]) == pytest.approx(9999.5, rel=0.01)


def test_time_axis(api):
    run_id = api.new_run()
    lines = [{"time": 5000 + 10 * i, "step": 7, "data": {"sys/cpu": float(i)}} for i in range(5)]
    api.ingest("metrics", run_id, lines)
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=sys/cpu&x=time")
    assert m["sys/cpu"]["x"] == [5000, 5010, 5020, 5030, 5040]
    assert api.get(f"/api/runs/{run_id}/metrics?name=sys/cpu&x=value")[0] == 400


def test_chunked_streaming_upload(server, api):
    """The client streams batches with chunked transfer encoding."""
    run_id = api.new_run()
    u = urlsplit(server.url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    lines = ("".join(json.dumps(l) + "\n" for l in metric_lines(1000))).encode()
    # split mid-line on purpose: lines must be reassembled across chunks
    chunks = [lines[i : i + 777] for i in range(0, len(lines), 777)]
    conn.request(
        "POST",
        "/ingest/metrics",
        body=iter(chunks),
        headers={"Authorization": f"Bearer {server.key}", "X-Run-Id": str(run_id)},
        encode_chunked=True,
    )
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read()) == {"ingested": 1000}
    # and the connection is still usable afterwards (keep-alive)
    conn.request("GET", "/api/health")
    assert conn.getresponse().status == 200
    conn.close()


@pytest.mark.parametrize(
    "body",
    [b"not json\n", b'{"time":1,"step":1}\n', b'{"time":"x","step":1,"data":{}}\n', b"[1,2]\n"],
)
def test_bad_ingest_is_a_400(api, body):
    run_id = api.new_run()
    status, out = api.post("/ingest/metrics", body, headers={"X-Run-Id": str(run_id)})
    assert status == 400 and "error" in out


def test_unknown_run_is_a_404(api):
    assert api.ingest("metrics", 999, metric_lines(1))[0] == 404
    assert api.get("/api/runs/999")[0] == 404
    assert api.post("/ingest/metrics", b"{}\n")[0] == 400  # no run id at all


def test_logs_and_structured_data(api):
    run_id = api.new_run()
    api.ingest(
        "logs",
        run_id,
        [{"time": i, "message": f"line {i}", "lineNumber": i, "logType": "INFO"} for i in range(30)],
    )
    _, tail = api.get(f"/api/runs/{run_id}/logs?limit=10")
    assert [l["message"] for l in tail["lines"]] == [f"line {i}" for i in range(20, 30)]
    _, more = api.get(f"/api/runs/{run_id}/logs?after={tail['lines'][-1]['id']}")
    assert more["lines"] == []

    api.post("/api/runs/logName/add", {"runId": run_id, "logName": ["h"], "logType": "HISTOGRAM"})
    api.ingest(
        "data",
        run_id,
        [
            {"time": i, "step": i, "logName": "h", "dataType": "HISTOGRAM",
             "data": json.dumps({"freq": [i], "bins": {"min": 0, "max": 1, "num": 1}})}
            for i in range(200)
        ],
    )
    _, h = api.get(f"/api/runs/{run_id}/data?name=h&limit=10")
    assert h["kind"] == "HISTOGRAM" and h["total"] == 200
    steps = [e["step"] for e in h["entries"]]
    assert len(steps) == 10 and steps[0] == 0 and steps[-1] == 199


# -- files -------------------------------------------------------------------


def upload(api, run_id, name="shot.png", content=b"\x89PNG fake", log_name="img", ext="png"):
    status, urls = api.post(
        "/files",
        {"files": [{"fileName": name, "fileSize": len(content), "fileType": ext,
                    "logName": log_name, "step": 3, "time": 1}]},
        headers={"X-Run-Id": str(run_id)},
    )
    assert status == 200, urls
    path = urls[log_name][0][name]
    status, _ = api.request("PUT", path, content, headers={"Content-Type": "image/png"})
    assert status == 200
    return path


def test_file_upload_download_and_range(api, server):
    run_id = api.new_run()
    path = upload(api, run_id, content=b"0123456789")
    status, body, headers = api.get(path, raw=True)
    assert (status, body) == (200, b"0123456789")
    assert headers["Content-Type"] == "image/png"
    assert "sandbox" in headers["Content-Security-Policy"]

    status, body, headers = api.get(path, raw=True, headers={"Range": "bytes=2-5"})
    assert (status, body) == (206, b"2345")
    assert headers["Content-Range"] == "bytes 2-5/10"
    status, body, _ = api.get(path, raw=True, headers={"Range": "bytes=-3"})
    assert (status, body) == (206, b"789")

    _, listing = api.get(f"/api/runs/{run_id}/files?name=img")
    assert listing["kind"] == "IMAGE"
    assert listing["files"][0]["size"] == 10 and listing["files"][0]["step"] == 3


def test_uploaded_html_is_never_served_as_a_page(api):
    run_id = api.new_run()
    path = upload(api, run_id, name="x.html", content=b"<script>alert(1)</script>",
                  log_name="art", ext="html")
    _, _, headers = api.get(path, raw=True)
    assert headers["Content-Disposition"].startswith("attachment")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_file_names_cannot_escape_the_data_directory(api, server):
    run_id = api.new_run()
    upload(api, run_id, name="../../../../etc/evil.png", log_name="../../up", ext="../../sh")
    stored = []
    for root, _, names in os.walk(server.store.dir):
        stored += [os.path.join(root, n) for n in names]
    inside = os.path.join(server.store.files_dir, str(run_id))
    blobs = [p for p in stored if not os.path.basename(p).startswith("mini-mlop.db")]
    assert blobs and all(os.path.dirname(p) == inside for p in blobs)
    assert all(os.path.basename(p).split(".")[0].isdigit() for p in blobs)


def test_upload_size_limit(tmp_path):
    from conftest import _serve

    server, thread = _serve(tmp_path, max_upload_mb=1)
    try:
        api = Api(f"http://127.0.0.1:{server.server_address[1]}", server.store.create_key("k")["key"])
        run_id = api.new_run()
        _, urls = api.post(
            "/files",
            {"files": [{"fileName": "big.bin", "fileSize": 1, "fileType": "bin", "logName": "a"}]},
            headers={"X-Run-Id": str(run_id)},
        )
        # The server answers 413 and hangs up without reading the body, so a
        # client still sending may see the reset instead of the status.
        try:
            status, _ = api.request("PUT", urls["a"][0]["big.bin"], b"x" * (2 * 2**20))
            assert status == 413
        except (urllib.error.URLError, ConnectionError):
            pass
        assert api.get(f"/api/runs/{run_id}/files?name=a")[1]["files"] == []
    finally:
        server.shutdown()
        thread.join()
        server.close()


# -- run lifecycle -----------------------------------------------------------


def test_status_updates_and_cancel(api):
    run_id = api.new_run()
    assert api.get(f"/api/runs/{run_id}")[1]["status"] == "RUNNING"
    assert api.post("/api/runs/trigger", {"runId": run_id})[1] == {"status": "RUNNING"}

    api.post(f"/api/runs/{run_id}/stop")
    assert api.post("/api/runs/trigger", {"runId": run_id})[1] == {"status": "CANCELLED"}
    # the client reporting its exit does not overwrite the cancellation
    api.post("/api/runs/status/update", {"runId": run_id, "status": "COMPLETED"})
    info = api.get(f"/api/runs/{run_id}")[1]
    assert info["status"] == "CANCELLED" and info["finished_at"]

    assert api.post("/api/runs/status/update", {"runId": run_id, "status": "BOGUS"})[0] == 400


def test_stalled_runs_fail_and_recover(api, server, monkeypatch):
    run_id = api.new_run()
    assert server.store.mark_stalled(older_than_ms=60_000) == 0  # fresh heartbeat
    assert server.store.mark_stalled(older_than_ms=-1) == 1
    info = api.get(f"/api/runs/{run_id}")[1]
    assert info["status"] == "FAILED" and info["stalled"] == 1

    # the client was only asleep: its next heartbeat revives the run
    monkeypatch.setattr(store_module, "HEARTBEAT_WRITE_INTERVAL_MS", 0)
    assert api.post("/api/runs/trigger", {"runId": run_id})[1] == {"status": "RUNNING"}
    info = api.get(f"/api/runs/{run_id}")[1]
    assert info["status"] == "RUNNING" and info["stalled"] == 0 and info["finished_at"] is None


def test_delete_run_and_project_remove_everything(api, server):
    keep = api.new_run(project="keep")
    api.ingest("metrics", keep, metric_lines(5))
    gone = [api.new_run(project="gone", name=f"r{i}") for i in range(2)]
    for run_id in gone:
        api.ingest("metrics", run_id, metric_lines(100))
        api.ingest("logs", run_id, [{"time": 1, "message": "m", "logType": "INFO"}])
        upload(api, run_id)
    run_dir = os.path.join(server.store.files_dir, str(gone[0]))
    assert os.listdir(run_dir)

    assert api.delete(f"/api/runs/{gone[0]}")[0] == 200
    assert not os.path.exists(run_dir)
    assert api.get(f"/api/runs/{gone[0]}")[0] == 404
    assert api.ingest("metrics", gone[0], metric_lines(1))[0] == 404

    assert api.delete("/api/projects/gone")[1] == {"deleted_runs": 1}
    assert [p["name"] for p in api.get("/api/projects")[1]["projects"]] == ["keep"]
    counts = {
        t: server.store._w.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("runs", "series", "metrics", "logs", "files")
    }
    assert counts == {"runs": 1, "series": 1, "metrics": 5, "logs": 0, "files": 0}


def test_data_survives_a_restart(tmp_path):
    from conftest import _serve

    server, thread = _serve(tmp_path)
    key = server.store.create_key("k")["key"]
    api = Api(f"http://127.0.0.1:{server.server_address[1]}", key)
    run_id = api.new_run()
    api.ingest("metrics", run_id, metric_lines(10))
    server.shutdown(); thread.join(); server.close()

    server, thread = _serve(tmp_path)
    try:
        api = Api(f"http://127.0.0.1:{server.server_address[1]}", key)
        assert api.get(f"/api/runs/{run_id}/metrics?name=loss")[1]["loss"]["count"] == 10
    finally:
        server.shutdown(); thread.join(); server.close()


# -- web UI and CLI ----------------------------------------------------------


def test_web_ui_is_served(server):
    anon = Api(server.url)
    status, body, headers = anon.get("/", raw=True)
    assert status == 200 and b"app.js" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    for asset in ("/app.js", "/chart.js", "/style.css", "/favicon.svg"):
        assert anon.get(asset, raw=True)[0] == 200, asset
    etag = headers["ETag"]
    assert anon.get("/", raw=True, headers={"If-None-Match": etag})[0] == 304
    assert anon.get("/../store.py", raw=True)[0] == 404
    assert anon.get("/%2e%2e/store.py", raw=True)[0] == 404


@pytest.mark.skipif(sys.version_info < (3, 10), reason="needs sys.stdlib_module_names")
def test_server_imports_nothing_but_the_standard_library():
    code = (
        "import sys, mini_mlop_server.app, mini_mlop_server.__main__\n"
        "std = set(sys.stdlib_module_names) | {'mini_mlop_server', 'sitecustomize', 'usercustomize'}\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} - std))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    extra = [m for m in eval(out.stdout) if not m.startswith("_")]
    assert extra == [], f"server pulled in third-party modules: {extra}"


def test_cli_key_commands(tmp_path):
    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "mini_mlop_server", "--data", str(tmp_path), "keys", *args],
            capture_output=True, text=True, check=True,
        )

    key = cli("create", "ci").stdout.strip()
    assert key.startswith("mini_mlop_")
    assert "ci" in cli("list").stdout and key not in cli("list").stdout
    cli("revoke", "1")
    assert "no keys yet" in cli("list").stdout
