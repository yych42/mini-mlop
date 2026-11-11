"""HTTP front end: client ingest API, read API for the web UI, static UI.

Built on http.server so the whole thing runs anywhere Python does with
nothing to install. One thread per connection; all persistence goes through
`Store`.
"""

import csv
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import socket
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from .query import QueryError, describe as describe_query
from .store import Invalid, NotFound, Store

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
COOKIE = "mlop_session"
MAX_JSON_BODY = 32 * 2**20
MAX_LINE = 8 * 2**20
CHUNK = 64 * 1024
# Types a browser may render inline when a stored file is opened directly.
# Everything else is sent as a download.
INLINE_TYPES = ("image/", "audio/", "video/", "text/plain")
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Config:
    def __init__(
        self,
        data_dir,
        host="0.0.0.0",
        port=2718,
        auth=True,
        stale_after=300,
        max_upload_mb=2048,
        public_url=None,
        verbose=False,
    ):
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.auth = auth
        self.stale_after = stale_after
        self.max_upload = max_upload_mb * 2**20
        self.public_url = public_url.rstrip("/") if public_url else None
        self.verbose = verbose


class Static:
    """The web UI: a handful of files read once and served from memory."""

    def __init__(self, root):
        self.files = {}
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                body = f.read()
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
                ctype += "; charset=utf-8"
            self.files["/" + name] = (
                body,
                gzip.compress(body, 9),
                ctype,
                '"' + hashlib.sha256(body).hexdigest()[:16] + '"',
            )
        if "/index.html" in self.files:
            self.files["/"] = self.files["/index.html"]


ROUTES = []


def route(method, pattern, auth=True):
    regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")

    def register(fn):
        ROUTES.append((method, regex, fn, auth))
        return fn

    return register


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlop/" + __version__
    sys_version = ""
    timeout = 75  # idle keep-alive connections are dropped after this

    # -- request plumbing -------------------------------------------------

    def log_message(self, format, *args):
        if self.server.config.verbose:
            super().log_message(format, *args)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_HEAD(self):
        self.dispatch("GET", head=True)

    def dispatch(self, method, head=False):
        self.head = head
        self.body_read = self.body_done = False
        self.store = self.server.store
        url = urlsplit(self.path)
        self.query = parse_qs(url.query)
        path = unquote(url.path)
        try:
            if method == "GET" and path in self.server.static.files:
                return self.send_static(path)
            for m, regex, fn, needs_auth in ROUTES:
                match = regex.match(path)
                if not match:
                    continue
                if m != method:
                    continue
                if needs_auth:
                    self.authenticate()
                return fn(self, **match.groupdict())
            raise HTTPError(404, "not found")
        except HTTPError as e:
            self.send_error_json(e.status, e.message)
        except NotFound as e:
            self.send_error_json(404, str(e))
        except Invalid as e:
            self.send_error_json(400, str(e))
        except (BrokenPipeError, ConnectionError, socket.timeout):
            self.close_connection = True
        except Exception as e:  # keep serving; report without leaking internals
            self.log_error("unhandled error on %s %s: %r", method, path, e)
            self.send_error_json(500, "internal server error")

    def authenticate(self):
        cfg = self.server.config
        self.key = None
        if not cfg.auth:
            return
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            self.key = self.store.check_key(header[7:].strip())
            if self.key:
                return
            raise HTTPError(401, "invalid API key")
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            jar = {}
        if COOKIE in jar:
            self.key = self.store.check_key(jar[COOKIE].value)
        if not self.key:
            raise HTTPError(401, "not logged in")
        # Cookie-authenticated writes must come from our own pages.
        if self.command not in ("GET", "HEAD"):
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise HTTPError(403, "cross-origin request refused")

    def base_url(self):
        cfg = self.server.config
        if cfg.public_url:
            return cfg.public_url
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        if not host or not re.fullmatch(r"[\w.\-:\[\]]+", host):
            host = f"localhost:{cfg.port}"
        return f"{'https' if proto == 'https' else 'http'}://{host}"

    # -- request bodies ---------------------------------------------------

    def iter_body(self, limit):
        """Yield the request body in chunks, whichever way it was framed."""
        self.body_read = True
        total = 0
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                line = self.rfile.readline(1024)
                try:
                    size = int(line.split(b";")[0].strip() or b"x", 16)
                except ValueError:
                    self.close_connection = True
                    raise HTTPError(400, "malformed chunked body")
                if size == 0:
                    while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    self.body_done = True
                    return
                total += size
                if total > limit:
                    self.close_connection = True
                    raise HTTPError(413, "request body too large")
                while size:
                    part = self.rfile.read(min(size, CHUNK))
                    if not part:
                        self.close_connection = True
                        raise HTTPError(400, "connection closed mid-body")
                    size -= len(part)
                    yield part
                self.rfile.readline(8)  # CRLF after each chunk
        else:
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HTTPError(400, "bad Content-Length")
            if remaining > limit or remaining < 0:
                self.close_connection = True
                raise HTTPError(413, "request body too large")
            while remaining:
                part = self.rfile.read(min(remaining, CHUNK))
                if not part:
                    self.close_connection = True
                    raise HTTPError(400, "connection closed mid-body")
                remaining -= len(part)
                yield part
            self.body_done = True

    def read_json(self):
        raw = b"".join(self.iter_body(MAX_JSON_BODY))
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise HTTPError(400, "body is not valid JSON")
        if not isinstance(body, dict):
            raise HTTPError(400, "body must be a JSON object")
        return body

    def iter_json_lines(self):
        """Parse a newline-delimited JSON body as it streams in."""
        tail = b""
        for chunk in self.iter_body(self.server.config.max_upload):
            lines = (tail + chunk).split(b"\n")
            tail = lines.pop()
            if len(tail) > MAX_LINE:
                self.close_connection = True
                raise HTTPError(413, "line too long")
            for line in lines:
                if line.strip():
                    yield self._parse_line(line)
        if tail.strip():
            yield self._parse_line(tail)

    def _parse_line(self, line):
        try:
            return json.loads(line)
        except ValueError:
            raise HTTPError(400, "body is not valid newline-delimited JSON")

    def drain(self):
        """Discard an unread request body so the connection can be reused;
        give up on the connection if that is not cheaply possible."""
        if self.body_done or self.command not in ("POST", "PUT"):
            return
        if self.body_read:
            self.close_connection = True  # partially consumed: cannot resync
            return
        try:
            for _ in self.iter_body(2**20):
                pass
        except HTTPError:
            self.close_connection = True

    def run_id(self, body=None):
        value = (body or {}).get("runId")
        if value is None:
            value = self.headers.get("X-Run-Id")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HTTPError(400, "missing or invalid run id")

    def arg(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def int_arg(self, name, default):
        try:
            return int(self.arg(name, default))
        except (TypeError, ValueError):
            raise HTTPError(400, f"{name} must be an integer")

    # -- responses --------------------------------------------------------

    def send_body(self, status, body, ctype, headers=()):
        self.drain()
        encoded = False
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body, encoded = gzip.compress(body, 1), True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if not self.head:
            self.wfile.write(body)

    def send_json(self, obj, status=200, headers=()):
        self.send_body(
            status,
            json.dumps(obj, separators=(",", ":")).encode(),
            "application/json",
            headers,
        )

    def send_error_json(self, status, message):
        try:
            self.send_json({"error": message}, status)
        except (BrokenPipeError, ConnectionError, socket.timeout):
            self.close_connection = True

    def send_static(self, path):
        body, gz, ctype, etag = self.server.static.files[path]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body, encoded = gz, True
        else:
            encoded = False
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if not self.head:
            self.wfile.write(body)

    def send_file(self, path, name):
        try:
            f = open(path, "rb")
        except OSError:
            raise HTTPError(404, "file content not found")
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and (m.group(1) or m.group(2)) and size:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1 if size else 0
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            inline = ctype.startswith(INLINE_TYPES)
            self.send_header(
                "Content-Disposition",
                f"{'inline' if inline else 'attachment'}; "
                f"filename*=UTF-8''{quote(name)}",
            )
            # Uploaded content never gets to run as a page on this origin.
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=86400, immutable")
            self.end_headers()
            if self.head:
                return
            f.seek(start)
            while length > 0:
                part = f.read(min(CHUNK, length))
                if not part:
                    break
                self.wfile.write(part)
                length -= len(part)


# -- session ---------------------------------------------------------------


@route("GET", "/api/health", auth=False)
def health(h):
    h.send_json({"status": "ok", "version": __version__, "auth": h.server.config.auth})


@route("POST", "/api/login", auth=False)
def login(h):
    key = str(h.read_json().get("key") or "").strip()
    if h.server.config.auth and not h.store.check_key(key):
        time.sleep(0.5)
        raise HTTPError(401, "invalid API key")
    secure = "; Secure" if h.base_url().startswith("https://") else ""
    cookie = f"{COOKIE}={quote(key)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
    h.send_json({"ok": True}, headers=[("Set-Cookie", cookie + secure)])


@route("POST", "/api/logout", auth=False)
def logout(h):
    h.send_json(
        {"ok": True},
        headers=[("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; Max-Age=0")],
    )


@route("GET", "/api/me")
def me(h):
    h.send_json(
        {
            "auth": h.server.config.auth,
            "key": h.key["name"] if h.key else None,
            "version": __version__,
            "url": h.base_url(),
        }
    )


# -- client API (the Python logger talks to these) -------------------------


@route("POST", "/api/slug")
def slug(h):
    # Kept for wire compatibility with upstream mlop clients.
    h.send_json({"organization": {"slug": "mlop"}, "version": __version__})


@route("POST", "/api/runs/create")
def runs_create(h):
    b = h.read_json()
    run_id = h.store.create_run(
        b.get("projectName"),
        b.get("runName"),
        config=b.get("config"),
        system=b.get("systemMetadata"),
        settings=b.get("loggerSettings"),
    )
    h.send_json(
        {
            "runId": run_id,
            "projectName": b.get("projectName"),
            "organizationSlug": "mlop",
            "url": f"{h.base_url()}/#/runs/{run_id}",
        }
    )


@route("POST", "/api/runs/status/update")
def runs_status(h):
    b = h.read_json()
    h.store.set_status(h.run_id(b), b.get("status"), b.get("statusMetadata"))
    h.send_json({"success": True})


@route("POST", "/api/runs/logName/add")
def runs_log_name(h):
    b = h.read_json()
    h.store.declare_series(h.run_id(b), b.get("logName"), b.get("logType"))
    h.send_json({"success": True})


@route("POST", "/api/runs/modelGraph/create")
def runs_graph(h):
    b = h.read_json()
    if not isinstance(b.get("graph"), dict):
        raise HTTPError(400, "graph must be an object")
    h.store.set_graph(h.run_id(b), b["graph"])
    h.send_json({"success": True})


@route("POST", "/api/runs/trigger")
def runs_trigger(h):
    # Doubles as the client's heartbeat; a CANCELLED status tells it to stop.
    h.send_json({"status": h.store.heartbeat(h.run_id(h.read_json()))})


@route("POST", "/api/runs/alert")
def runs_alert(h):
    b = h.read_json()
    if not isinstance(b.get("alert"), dict):
        raise HTTPError(400, "alert must be an object")
    h.store.add_alert(h.run_id(b), b["alert"])
    h.send_json({"success": True})


@route("POST", "/ingest/metrics")
def ingest_metrics(h):
    h.send_json({"ingested": h.store.ingest_metrics(h.run_id(), h.iter_json_lines())})


@route("POST", "/ingest/data")
def ingest_data(h):
    h.send_json({"ingested": h.store.ingest_data(h.run_id(), h.iter_json_lines())})


@route("POST", "/ingest/logs")
def ingest_logs(h):
    h.send_json({"ingested": h.store.ingest_logs(h.run_id(), h.iter_json_lines())})


@route("POST", "/files")
def files_register(h):
    b = h.read_json()
    out = {}
    for log_name, file_name, fid in h.store.register_files(h.run_id(), b.get("files")):
        out.setdefault(log_name, []).append({file_name: f"/api/files/{fid}"})
    h.send_json(out)


@route("PUT", "/api/files/<file_id>")
def files_upload(h, file_id):
    try:
        fid = int(file_id)
    except ValueError:
        raise HTTPError(404, "file not found")
    size = h.store.save_file(fid, h.iter_body(h.server.config.max_upload))
    h.send_json({"size": size})


# -- read API (the web UI talks to these) ----------------------------------


def _int_path(value):
    try:
        return int(value)
    except ValueError:
        raise HTTPError(404, "not found")


@route("GET", "/api/files/<file_id>")
def files_get(h, file_id):
    f = h.store.get_file(_int_path(file_id))
    h.send_file(f["path"], f"{f['name']}")


@route("GET", "/api/projects")
def projects_list(h):
    h.send_json({"projects": h.store.list_projects()})


@route("GET", "/api/projects/<project>/runs")
def project_runs(h, project):
    h.send_json(
        h.store.list_runs(
            project,
            limit=max(1, min(h.int_arg("limit", 200), 1000)),
            offset=max(0, h.int_arg("offset", 0)),
        )
    )


@route("DELETE", "/api/projects/<project>")
def project_delete(h, project):
    h.send_json({"deleted_runs": h.store.delete_project(project)})


@route("GET", "/api/runs/<run_id>")
def run_get(h, run_id):
    h.send_json(h.store.get_run(_int_path(run_id)))


@route("DELETE", "/api/runs/<run_id>")
def run_delete(h, run_id):
    h.store.delete_run(_int_path(run_id))
    h.send_json({"ok": True})


@route("POST", "/api/runs/<run_id>/stop")
def run_stop(h, run_id):
    h.store.cancel_run(_int_path(run_id))
    h.send_json({"ok": True})


@route("GET", "/api/runs/<run_id>/metrics")
def run_metrics(h, run_id):
    names = h.query.get("name", [])
    if not names or len(names) > 500:
        raise HTTPError(400, "pass 1 to 500 name parameters")
    h.send_json(
        h.store.read_metrics(
            _int_path(run_id),
            names,
            points=h.int_arg("points", 500),
            axis=h.arg("x", "step"),
        )
    )


@route("GET", "/api/runs/<run_id>/data")
def run_data(h, run_id):
    h.send_json(
        h.store.read_data(
            _int_path(run_id), h.arg("name", ""), limit=h.int_arg("limit", 64)
        )
    )


@route("GET", "/api/runs/<run_id>/files")
def run_files(h, run_id):
    h.send_json(
        h.store.read_files(
            _int_path(run_id), h.arg("name", ""), limit=h.int_arg("limit", 500)
        )
    )


@route("GET", "/api/runs/<run_id>/logs")
def run_logs(h, run_id):
    after = h.arg("after")
    h.send_json(
        {
            "lines": h.store.read_logs(
                _int_path(run_id),
                after=int(after) if after and after.isdigit() else 0,
                limit=h.int_arg("limit", 1000),
                tail=after is None,
            )
        }
    )


@route("GET", "/api/runs/<run_id>/graph")
def run_graph(h, run_id):
    h.send_json({"graph": h.store.get_graph(_int_path(run_id))})


@route("GET", "/api/keys")
def keys_list(h):
    h.send_json({"keys": h.store.list_keys(), "current": h.key and h.key["id"]})


@route("POST", "/api/keys")
def keys_create(h):
    h.send_json(h.store.create_key(str(h.read_json().get("name") or "").strip()))


@route("DELETE", "/api/keys/<key_id>")
def keys_delete(h, key_id):
    key_id = _int_path(key_id)
    if h.key and h.key["id"] == key_id:
        raise HTTPError(400, "refusing to revoke the key you are signed in with")
    h.store.delete_key(key_id)
    h.send_json({"ok": True})


@route("GET", "/api/stats")
def stats(h):
    h.send_json(h.store.stats())


# -- open-ended read access: SQL -------------------------------------------


def _run_query(h, sql, params, limit, fmt):
    try:
        result = h.store.sql.run(sql, params, limit)
    except QueryError as e:
        raise HTTPError(400, str(e))
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(result["columns"])
        w.writerows(result["rows"])
        headers = [("X-Truncated", "1")] if result["truncated"] else []
        return h.send_body(200, out.getvalue().encode(), "text/csv; charset=utf-8", headers)
    if fmt not in (None, "json"):
        raise HTTPError(400, "format must be json or csv")
    h.send_json(result)


@route("GET", "/api/query/schema")
def query_schema(h):
    h.send_json(describe_query())


@route("GET", "/api/query")
def query_get(h):
    if h.arg("sql") is None:
        return h.send_json(describe_query())
    _run_query(h, h.arg("sql"), None, h.arg("limit"), h.arg("format"))


@route("POST", "/api/query")
def query_post(h):
    body = h.read_json()
    _run_query(
        h, body.get("sql"), body.get("params"), body.get("limit"),
        body.get("format") or h.arg("format"),
    )


# -- server ----------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, config, store=None):
        self.config = config
        self.store = store or Store(config.data_dir)
        self.static = Static(WEB_DIR)
        self._stop = threading.Event()
        if ":" in config.host:
            self.address_family = socket.AF_INET6
        super().__init__((config.host, config.port), Handler)
        self._janitor = threading.Thread(target=self._watch_runs, daemon=True)
        self._janitor.start()

    def _watch_runs(self):
        while not self._stop.wait(30):
            try:
                self.store.mark_stalled(self.config.stale_after * 1000)
            except Exception:
                pass

    def handle_error(self, request, client_address):
        if self.config.verbose:
            super().handle_error(request, client_address)

    def close(self):
        self._stop.set()
        self.server_close()
        self.store.close()


def make_server(config, store=None):
    # Connection threads mostly sit idle on keep-alive; small stacks keep a
    # busy server's footprint low on memory-constrained boards.
    try:
        threading.stack_size(512 * 1024)
    except (ValueError, RuntimeError):
        pass
    return Server(config, store)
