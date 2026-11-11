"""The remote half of the "http" sandbox provider.

Run this on a machine that is NOT your mini-mlop server and holds nothing you
care about, typically a small container:

    MLOP_SANDBOX_TOKEN=some-long-secret python -m mini_mlop_server.sandbox_runner

then enter its URL and the token under Settings → Agent → Sandbox.

Protocol (the whole of it):

    GET  /health            -> {"status": "ok"}
    POST /run               Authorization: Bearer <token>
         {"code": str, "timeout": int, "files": {name: base64}}
      -> {"stdout": str, "stderr": str, "exit_code": int | null,
          "timed_out": bool, "duration_ms": int}

Each run gets a fresh directory that is deleted afterwards. The runner limits
CPU, memory and time; containing the code is the job of the container or VM
it runs in. Standard library only.
"""

import argparse
import base64
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .sandbox import MAX_INPUT_BYTES, SandboxError, execute

MAX_BODY = MAX_INPUT_BYTES * 2  # base64 overhead and then some


class Runner(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlop-sandbox"
    sys_version = ""
    timeout = 60

    def log_message(self, format, *args):
        if self.server.verbose:
            super().log_message(format, *args)

    def reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            return self.reply(404, {"error": "not found"})
        given = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(given.encode(), expected.encode()):
            self.close_connection = True
            return self.reply(401, {"error": "invalid token"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self.close_connection = True
            return self.reply(413, {"error": "missing or oversized body"})
        try:
            body = json.loads(self.rfile.read(length))
            code = body["code"]
            files = {n: base64.b64decode(d) for n, d in (body.get("files") or {}).items()}
            if not isinstance(code, str):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.reply(400, {"error": "body must be JSON with code and optional files"})
        with self.server.slots:
            try:
                result = execute(code, files, body.get("timeout"))
            except SandboxError as e:
                return self.reply(400, {"error": str(e)})
        self.reply(200, result)


def make_runner(host, port, token, concurrency=2, verbose=False):
    server = ThreadingHTTPServer((host, port), Runner)
    server.daemon_threads = True
    server.token = token
    server.verbose = verbose
    server.slots = threading.BoundedSemaphore(concurrency)
    return server


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m mini_mlop_server.sandbox_runner",
        description="Runs Python sent by a mini-mlop server's agent. "
        "Put it on a machine that holds nothing you care about.",
    )
    p.add_argument("--host", default=os.environ.get("MLOP_SANDBOX_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8700)))
    p.add_argument("--concurrency", type=int, default=2, help="runs at once (default 2)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    token = os.environ.get("MLOP_SANDBOX_TOKEN", "")
    if len(token) < 16:
        sys.exit("set MLOP_SANDBOX_TOKEN to a secret of at least 16 characters")
    server = make_runner(args.host, args.port, token, args.concurrency, args.verbose)
    print(f"mini-mlop sandbox runner listening on {args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
