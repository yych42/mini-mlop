"""Command line: `mlop-server` / `python -m mlop_server`."""

import argparse
import os
import signal
import sys
import time

from . import __version__
from .app import Config, make_server
from .store import Store

DEFAULT_DATA = os.path.join("~", ".mlop", "server")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        sys.exit(f"{name} must be an integer")


def build_parser():
    p = argparse.ArgumentParser(
        prog="mlop-server",
        description="mlop experiment tracking server: one process, one data directory.",
    )
    p.add_argument("--version", action="version", version=f"mlop-server {__version__}")
    p.add_argument(
        "--data",
        default=os.environ.get("MLOP_DATA_DIR", DEFAULT_DATA),
        help="directory holding the database and uploaded files "
        "(env MLOP_DATA_DIR, default %(default)s)",
    )
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the server (default)")
    for parser, top in ((p, True), (s, False)):
        # Options are accepted before or after `serve`; the subparser copies
        # carry no defaults so they never clobber values given up front.
        def opt(*names, default=None, **kw):
            parser.add_argument(
                *names, default=default if top else argparse.SUPPRESS, **kw
            )

        opt(
            "--host",
            default=os.environ.get("MLOP_HOST", "0.0.0.0"),
            help="address to listen on (env MLOP_HOST, default 0.0.0.0)",
        )
        opt(
            "--port",
            type=int,
            default=_env_int("MLOP_PORT", 2718),
            help="port to listen on (env MLOP_PORT, default 2718)",
        )
        opt(
            "--no-auth",
            action="store_true",
            default=os.environ.get("MLOP_NO_AUTH", "") not in ("", "0", "false"),
            help="accept requests without an API key; only for networks you trust",
        )
        opt(
            "--public-url",
            default=os.environ.get("MLOP_PUBLIC_URL"),
            help="URL users reach this server at, used in run links "
            "(env MLOP_PUBLIC_URL, default: taken from each request)",
        )
        opt(
            "--stale-after",
            type=int,
            default=300,
            metavar="SECONDS",
            help="mark a run failed after this long without a heartbeat (default 300)",
        )
        opt(
            "--max-upload-mb",
            type=int,
            default=2048,
            help="largest accepted upload (default 2048)",
        )
        opt("--verbose", action="store_true", default=False, help="log every request")

    k = sub.add_parser("keys", help="manage API keys")
    ksub = k.add_subparsers(dest="action", required=True)
    ksub.add_parser("list", help="list keys")
    kc = ksub.add_parser("create", help="create a key and print it once")
    kc.add_argument("name", nargs="?", default="default")
    kr = ksub.add_parser("revoke", help="revoke a key by id")
    kr.add_argument("id", type=int)
    return p


def keys_command(args):
    store = Store(args.data, readers=1)
    try:
        if args.action == "list":
            keys = store.list_keys()
            if not keys:
                print("no keys yet; create one with: mlop-server keys create NAME")
            for k in keys:
                used = (
                    time.strftime("%Y-%m-%d", time.localtime(k["last_used_at"] / 1000))
                    if k["last_used_at"]
                    else "never"
                )
                print(f"{k['id']:>4}  {k['hint']:<16} last used {used:<10}  {k['name']}")
        elif args.action == "create":
            k = store.create_key(args.name)
            print(k["key"])
            print(f"key {k['id']} ({k['name']}) created; it is not shown again", file=sys.stderr)
        elif args.action == "revoke":
            store.delete_key(args.id)
            print(f"key {args.id} revoked", file=sys.stderr)
    finally:
        store.close()


def serve(args):
    config = Config(
        data_dir=args.data,
        host=args.host,
        port=args.port,
        auth=not args.no_auth,
        stale_after=args.stale_after,
        max_upload_mb=args.max_upload_mb,
        public_url=args.public_url,
        verbose=args.verbose,
    )
    try:
        server = make_server(config)
    except OSError as e:
        sys.exit(f"mlop-server: cannot listen on {args.host}:{args.port}: {e}")

    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    url = config.public_url or f"http://{shown}:{server.server_address[1]}"
    print(f"mlop-server {__version__}")
    print(f"  data  {server.store.dir}")
    print(f"  open  {url}")
    if not config.auth:
        print("  auth  DISABLED: anyone who can reach this port can read and write")
    elif server.store.count_keys() == 0:
        key = server.store.create_key("default")["key"]
        print(f"  key   {key}")
        print("        first start: this API key was just created and is shown once.")
        print("        use it to sign in to the web UI and for `mlop login`.")
    else:
        print("  auth  API key required (mlop-server keys create NAME for a new one)")
    sys.stdout.flush()

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "keys":
        keys_command(args)
    else:
        serve(args)


if __name__ == "__main__":
    main()
