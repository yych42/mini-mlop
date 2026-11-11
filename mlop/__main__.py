#!/usr/bin/env python3

import argparse
import sys

from .auth import login, logout


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        # Convenience alias for the `mlop-server` command.
        from mlop_server.__main__ import main as server_main

        return server_main(sys.argv[2:])

    parser = argparse.ArgumentParser(description="mlop")
    subparsers = parser.add_subparsers(dest="command", help="commands")

    for name, text in (
        ("login", "save an API key for an mlop server"),
        ("logout", "forget the saved API key for an mlop server"),
    ):
        p = subparsers.add_parser(name, help=text)
        if name == "login":
            p.add_argument("key", nargs="?", help="API key")
        p.add_argument(
            "--url", help="server address (default: $MLOP_URL or http://localhost:2718)"
        )
    subparsers.add_parser("server", help="run the mlop server (same as `mlop-server`)")

    args = parser.parse_args()
    settings = {"url": args.url} if getattr(args, "url", None) else {}

    if args.command == "login":
        try:
            login(settings=settings, key=args.key)
        except (ConnectionError, PermissionError) as e:
            sys.exit(f"mlop: {e}")
        except KeyboardInterrupt:
            sys.exit(1)
    elif args.command == "logout":
        logout(settings=settings)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
