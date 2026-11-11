import getpass
import json
import logging
import os
import sys

import httpx

from .log import setup_logger, teardown_logger
from .sets import get_console, setup
from .util import ANSI, print_url

tlogger = logging.getLogger("auth")
tag = "Authentication"

MAX_PROMPTS = 3


def _credentials_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "mini-mlop", "credentials.json")


def _load() -> dict:
    try:
        with open(_credentials_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    path = _credentials_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(path + ".tmp", path)


def _check(settings, key):
    """Returns (True, None) when the server accepts `key`, (False, None) when
    it rejects it, and (None, reason) when the server cannot be reached."""
    try:
        with httpx.Client(
            verify=not settings.insecure_disable_ssl,
            proxy=settings.http_proxy or settings.https_proxy or None,
            timeout=httpx.Timeout(settings.x_file_stream_timeout_seconds),
        ) as client:
            r = client.post(
                settings.url_login,
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if r.status_code == 200:
        _warn_on_version_mismatch(r)
        return True, None
    if r.status_code in (401, 403):
        return False, None
    return None, f"unexpected response {r.status_code}"


def _warn_on_version_mismatch(response):
    """Client and server are released together; before 1.0 a differing minor
    version may mean a differing protocol, so say so instead of failing oddly."""
    from . import __version__

    try:
        theirs = str(response.json().get("version") or "")
    except Exception:
        return
    if theirs and theirs.split(".")[:2] != __version__.split(".")[:2]:
        tlogger.warning(
            "%s: this client is version %s but the server is %s; "
            "upgrade whichever is older if logging misbehaves",
            tag, __version__, theirs,
        )


def _interactive() -> bool:
    if get_console() == "jupyter":
        return True
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def login(settings=None, key=None) -> bool:
    """Make sure we hold a key the server accepts, and remember it.

    The key is taken from, in order: the `key` argument, settings["_auth"],
    the MINI_MLOP_API_KEY environment variable, and the keys saved by earlier
    logins. In a terminal or notebook you are prompted if none of those work;
    anywhere else an error explains what to set.
    """
    settings = setup(settings)
    setup_logger(settings=settings, logger=tlogger)
    base = settings.url_base()
    saved = _load()
    key = key or settings._auth or saved.get(base)

    try:
        for attempt in range(MAX_PROMPTS + 1):
            ok, problem = _check(settings, key)
            if ok:
                settings._auth = key or ""
                if key and saved.get(base) != key:
                    saved[base] = key
                    try:
                        _save(saved)
                    except OSError as e:
                        tlogger.warning("%s: could not save key: %s", tag, e)
                tlogger.info(f"{tag}: connected to {print_url(base)}")
                return True
            if ok is None:
                raise ConnectionError(
                    f"cannot reach the mini-mlop server at {base} ({problem}). "
                    "Start one with `mini-mlop-server`, or point MINI_MLOP_URL / "
                    'settings={"url": ...} at a running server.'
                )
            if not _interactive() or attempt == MAX_PROMPTS:
                raise PermissionError(
                    f"the mini-mlop server at {base} did not accept "
                    f"{'the API key' if key else 'a request without an API key'}. "
                    "Set MINI_MLOP_API_KEY or run `mini-mlop login`; keys are created in "
                    f"the web UI ({settings.url_token}) or with "
                    "`mini-mlop-server keys create NAME`."
                )
            if key:
                tlogger.warning("%s: the server rejected that key", tag)
            tlogger.info(
                f"{tag}: an API key is required\n\n"
                f" {ANSI.cyan}- Create or copy one at {print_url(settings.url_token)}\n"
                f" {ANSI.green}- Press CTRL+C / ⌃+C to cancel\n"
            )
            key = getpass.getpass(prompt="Enter API key: ").strip()
    finally:
        teardown_logger(tlogger)


def logout(settings=None) -> None:
    """Forget the saved key for this server."""
    settings = setup(settings)
    setup_logger(settings=settings, logger=tlogger)
    saved = _load()
    if saved.pop(settings.url_base(), None) is not None:
        try:
            _save(saved)
        except OSError as e:
            tlogger.warning("%s: could not update saved keys: %s", tag, e)
    tlogger.info(f"{tag}: logged out of {settings.url_base()}")
    teardown_logger(tlogger)
