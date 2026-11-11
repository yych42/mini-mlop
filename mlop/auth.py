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
    return os.path.join(base, "mlop", "credentials.json")


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
        return True, None
    if r.status_code in (401, 403):
        return False, None
    return None, f"unexpected response {r.status_code}"


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
    the MLOP_API_KEY environment variable, and the keys saved by earlier
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
                    f"cannot reach the mlop server at {base} ({problem}). "
                    "Start one with `mlop-server`, or point MLOP_URL / "
                    'settings={"url": ...} at a running server.'
                )
            if not _interactive() or attempt == MAX_PROMPTS:
                raise PermissionError(
                    f"the mlop server at {base} did not accept "
                    f"{'the API key' if key else 'a request without an API key'}. "
                    "Set MLOP_API_KEY or run `mlop login`; keys are created in "
                    f"the web UI ({settings.url_token}) or with "
                    "`mlop-server keys create NAME`."
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
