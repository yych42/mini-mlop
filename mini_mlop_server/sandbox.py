"""Where the agent's Python runs.

The agent can write code to do real analysis (statistics, fits, anything SQL
is awkward for). That code is written by a language model that has read your
run names and log lines, so *where* it runs is a security decision, and one
you should be able to change. Everything here hangs off one small contract:

    sandbox.run(code, files, timeout) -> {
        "stdout": str, "stderr": str, "exit_code": int | None,
        "timed_out": bool, "duration_ms": int,
    }

`files` maps file names to bytes; they are placed in the working directory
before `code` runs. Data goes in as files and comes back as printed text: a
sandbox needs no network route back to this server.

A provider is a class with that one method, registered in PROVIDERS. Shipped:

  * "http"  — any machine running `python -m mini_mlop_server.sandbox_runner`
              (a container on Fly, Railway, Modal, Cloud Run, a spare box...).
              The isolation is that it is a different machine with no access
              to this server's database or keys.
  * "local" — a subprocess on this machine. Resource-limited but NOT
              isolated: the code can read whatever the server can.

Adapters for vendor sandbox APIs plug in the same way: subclass Sandbox,
implement run(), add it to PROVIDERS.
"""

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
MAX_OUTPUT = 64 * 1024  # bytes of stdout / stderr kept
MAX_INPUT_BYTES = 48 * 1024 * 1024
MEMORY_LIMIT = 2 * 1024**3
FILE_LIMIT = 256 * 1024**2
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")


class SandboxError(Exception):
    """The sandbox could not run the code at all; safe to show."""


def check_files(files):
    total = 0
    for name, data in files.items():
        if not _NAME.match(name) or name == "main.py":
            raise SandboxError(f"invalid input file name: {name!r}")
        total += len(data)
    if total > MAX_INPUT_BYTES:
        raise SandboxError("input files are too large for the sandbox")


def clamp_timeout(timeout):
    try:
        return max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _limits(timeout):  # runs in the child, before exec
    import resource

    for what, value in (
        (resource.RLIMIT_CPU, timeout + 2),
        (resource.RLIMIT_AS, MEMORY_LIMIT),
        (resource.RLIMIT_FSIZE, FILE_LIMIT),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(what, (value, value))
        except (ValueError, OSError):
            pass  # not every platform enforces every limit


def _tail(path):
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_OUTPUT + 1)
    except OSError:
        return ""
    text = data[:MAX_OUTPUT].decode("utf-8", "replace")
    return text + ("\n… output truncated" if len(data) > MAX_OUTPUT else "")


def execute(code, files=None, timeout=DEFAULT_TIMEOUT):
    """Run `code` with this machine's Python in a throwaway directory.

    Used by the local provider and by the remote runner. It limits CPU time,
    memory, file size and wall-clock time; it does not restrict what the code
    can read or which hosts it can reach.
    """
    files = files or {}
    check_files(files)
    timeout = clamp_timeout(timeout)
    work = tempfile.mkdtemp(prefix="mini-mlop-sandbox-")
    started = time.monotonic()
    try:
        for name, data in files.items():
            with open(os.path.join(work, name), "wb") as f:
                f.write(data)
        with open(os.path.join(work, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": work,
            "TMPDIR": work,
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLBACKEND": "Agg",
        }
        out_path, err_path = os.path.join(work, ".stdout"), os.path.join(work, ".stderr")
        posix = os.name == "posix"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(
                [sys.executable, "-I", "main.py"],
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=posix,
                preexec_fn=(lambda: _limits(timeout)) if posix else None,
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    if posix:
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except OSError:
                    pass
                proc.wait()
        return {
            "stdout": _tail(out_path),
            "stderr": _tail(err_path),
            "exit_code": None if timed_out else proc.returncode,
            "timed_out": timed_out,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


class Sandbox:
    """Base class for sandbox providers."""

    #: shown in Settings
    label = ""
    #: one sentence on what separates the code from this server
    isolation = ""
    #: config fields this provider needs: [(name, label, secret?)]
    fields = ()

    def __init__(self, config):
        self.config = config or {}

    def run(self, code, files=None, timeout=DEFAULT_TIMEOUT):
        raise NotImplementedError


class LocalSandbox(Sandbox):
    label = "This machine (no isolation)"
    isolation = (
        "None. Code runs as the server's user with CPU, memory and time limits, "
        "and can read the server's files, including its database and saved keys. "
        "Use only when everything that logs to this server is trusted."
    )

    def run(self, code, files=None, timeout=DEFAULT_TIMEOUT):
        return execute(code, files, timeout)


class HttpSandbox(Sandbox):
    label = "Remote runner (HTTP)"
    isolation = (
        "A separate machine running the mini-mlop sandbox runner. It never sees "
        "this server's database or keys, only the query results sent with each run."
    )
    fields = (("url", "Runner URL", False), ("token", "Runner token", True))

    def run(self, code, files=None, timeout=DEFAULT_TIMEOUT):
        files = files or {}
        check_files(files)
        timeout = clamp_timeout(timeout)
        url = (self.config.get("url") or "").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise SandboxError("the sandbox runner URL is not set")
        body = json.dumps({
            "code": code,
            "timeout": timeout,
            "files": {n: base64.b64encode(d).decode() for n, d in files.items()},
        }).encode()
        req = urllib.request.Request(
            url + "/run",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + (self.config.get("token") or ""),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                result = json.loads(r.read(4 * 1024 * 1024))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SandboxError("the sandbox runner rejected the token")
            raise SandboxError(f"the sandbox runner returned an error ({e.code})")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SandboxError(f"could not reach the sandbox runner at {url}: {getattr(e, 'reason', e)}")
        if not isinstance(result, dict) or "stdout" not in result:
            raise SandboxError("the sandbox runner sent an unexpected response")
        return {
            "stdout": str(result.get("stdout") or "")[: MAX_OUTPUT + 64],
            "stderr": str(result.get("stderr") or "")[: MAX_OUTPUT + 64],
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out")),
            "duration_ms": int(result.get("duration_ms") or 0),
        }


PROVIDERS = {"http": HttpSandbox, "local": LocalSandbox}


def make(config):
    """The configured sandbox, or None when code execution is off."""
    config = config or {}
    cls = PROVIDERS.get(config.get("provider") or "")
    return cls(config) if cls else None


def catalog():
    return [
        {"id": "none", "label": "Off", "isolation": "The agent answers with SQL only.", "fields": []}
    ] + [
        {
            "id": key,
            "label": cls.label,
            "isolation": cls.isolation,
            "fields": [{"name": n, "label": lbl, "secret": s} for n, lbl, s in cls.fields],
        }
        for key, cls in PROVIDERS.items()
    ]
