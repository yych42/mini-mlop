#!/usr/bin/env python3

import argparse
import sys

from .auth import login, logout


def _query(args):
    """Run read-only SQL on the server; the open-ended way to ask questions."""
    import csv
    import json

    import httpx

    from .auth import _load
    from .sets import setup

    settings = setup({"url": args.url} if args.url else {})
    base = settings.url_base()
    key = settings._auth or _load().get(base)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        if args.schema or not args.sql:
            r = httpx.get(f"{base}/api/query/schema", headers=headers, timeout=30)
        else:
            sql = sys.stdin.read() if args.sql == "-" else args.sql
            body = {"sql": sql, "limit": args.limit}
            if args.project:
                body["project"] = args.project
            r = httpx.post(f"{base}/api/query", json=body, headers=headers, timeout=60)
    except httpx.HTTPError as e:
        sys.exit(f"mini-mlop: cannot reach the server at {base} ({e})")
    if r.status_code == 401:
        sys.exit("mini-mlop: the server did not accept the API key; run `mini-mlop login`")
    try:
        result = r.json()
    except ValueError:
        sys.exit(f"mini-mlop: unexpected response {r.status_code} from {base}")
    if r.status_code != 200:
        sys.exit(f"mini-mlop: {result.get('error', r.status_code)}")

    if "tables" in result:  # the schema
        if args.format == "json":
            return print(json.dumps(result, indent=2))
        print(f"{result['dialect']}, read-only. Tables:\n")
        for t in result["tables"]:
            print(f"{t['name']} — {t['description']}")
            for name, kind, note in t["columns"]:
                print(f"    {name:<13}{kind:<9}{note}")
        print("\nNotes:")
        for note in result["notes"]:
            print(f"  - {note}")
        print("\nExamples:")
        for ex in result["examples"]:
            print(f"  -- {ex['question']}\n  {ex['sql']}\n")
        return

    if args.format == "json":
        print(json.dumps(result))
    elif args.format == "csv":
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(result["columns"])
        w.writerows(result["rows"])
    else:
        cells = [result["columns"]] + [
            ["" if v is None else str(v) for v in row] for row in result["rows"]
        ]
        widths = [min(60, max(len(r[i]) for r in cells)) for i in range(len(cells[0]))]
        for n, row in enumerate(cells):
            print("  ".join(c[:60].ljust(w) for c, w in zip(row, widths)).rstrip())
            if n == 0:
                print("  ".join("-" * w for w in widths))
    if result["truncated"]:
        print(
            f"mini-mlop: stopped at {result['row_count']} rows; raise --limit or aggregate",
            file=sys.stderr,
        )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        # Convenience alias for the `mini-mlop-server` command.
        from mini_mlop_server.__main__ import main as server_main

        return server_main(sys.argv[2:])

    parser = argparse.ArgumentParser(prog="mini-mlop", description="mini-mlop logging client")
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
    qp = subparsers.add_parser(
        "query",
        help="run read-only SQL on the server (no SQL: print the schema)",
    )
    qp.add_argument("sql", nargs="?", help="a SELECT statement, or - to read it from stdin")
    qp.add_argument("--schema", action="store_true", help="print tables, columns and examples")
    qp.add_argument("--format", choices=("table", "csv", "json"), default="table")
    qp.add_argument("--limit", type=int, default=1000, help="most rows to return (default 1000)")
    qp.add_argument("--project", help="confine the query to one project")
    qp.add_argument(
        "--url", help="server address (default: $MLOP_URL or http://localhost:2718)"
    )
    subparsers.add_parser("server", help="run the mlop server (same as `mini-mlop-server`)")

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
    elif args.command == "query":
        _query(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
