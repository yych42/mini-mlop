import json
import threading
import urllib.error
import urllib.request

import pytest

from mlop_server.app import Config, make_server


class Api:
    """Tiny HTTP helper so server tests need nothing but the stdlib."""

    def __init__(self, url, key=None):
        self.url, self.key = url, key

    def request(self, method, path, body=None, headers=None, key="default", raw=False):
        headers = dict(headers or {})
        key = self.key if key == "default" else key
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, data, rh = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, data, rh = e.code, e.read(), e.headers
        if raw:
            return status, data, rh
        return status, json.loads(data) if data else None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def new_run(self, project="proj", name="run", **extra):
        status, body = self.post(
            "/api/runs/create", {"projectName": project, "runName": name, **extra}
        )
        assert status == 200, body
        return body["runId"]

    def ingest(self, kind, run_id, lines):
        body = "".join(json.dumps(line) + "\n" for line in lines).encode()
        return self.post(f"/ingest/{kind}", body, headers={"X-Run-Id": str(run_id)})


def _serve(tmp_path, **kw):
    server = make_server(Config(data_dir=str(tmp_path / "data"), host="127.0.0.1", port=0, **kw))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    return server, thread


@pytest.fixture
def server(tmp_path):
    server, thread = _serve(tmp_path)
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    server.key = server.store.create_key("test")["key"]
    yield server
    server.shutdown()
    thread.join()
    server.close()


@pytest.fixture
def open_server(tmp_path):
    """A server started with --no-auth."""
    server, thread = _serve(tmp_path, auth=False)
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    thread.join()
    server.close()


@pytest.fixture
def api(server):
    return Api(server.url, server.key)
