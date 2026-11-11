# Running the server

```bash
mlop-server
```

`python -m mlop_server` and `mlop server` do the same thing. The server needs Python 3.9+ and
nothing else — if you would rather not pip-install on the box, copying the `mlop_server/` folder
and running `python -m mlop_server` next to it works too.

## Options

| Flag | Environment | Default | |
|---|---|---|---|
| `--data DIR` | `MLOP_DATA_DIR` | `~/.mlop/server` | Database and uploaded files |
| `--host ADDR` | `MLOP_HOST` | `0.0.0.0` | Listen address; `127.0.0.1` for this machine only |
| `--port N` | `MLOP_PORT` | `2718` | |
| `--public-url URL` | `MLOP_PUBLIC_URL` | from each request | Address used in the run links sent to clients |
| `--no-auth` | `MLOP_NO_AUTH=1` | off | Accept requests without a key |
| `--stale-after SECONDS` | | `300` | Mark a run failed after this long with no heartbeat |
| `--max-upload-mb N` | | `2048` | Largest accepted file or ingest request |
| `--verbose` | | off | Log every request |

## API keys

Every request needs a key unless the server runs with `--no-auth`. The first start creates one
and prints it once. After that:

```bash
mlop-server keys create laptop
```

```bash
mlop-server keys list
```

```bash
mlop-server keys revoke 2
```

These work whether or not the server is running (pass the same `--data` if you changed it), so a
lost key is never a lock-out. Keys can also be created and revoked from **Settings** in the web
UI. Only a SHA-256 hash of each key is stored.

The same key signs you in to the web UI. There are no user accounts: anyone holding a key can
read, write and delete everything. Give each person or machine its own key so one can be revoked
without disturbing the others.

`--no-auth` is for a machine only you can reach (a laptop, or a private tailnet). With it, anyone
who can connect to the port can read and delete your data.

## Keeping it running

**systemd** (Raspberry Pi OS, Debian, Ubuntu…): [`deploy/mlop-server.service`](../deploy/mlop-server.service)
has the unit and the four commands to install it in its header.

**Docker**: from the repository root,

```bash
docker compose -f deploy/compose.yaml up -d
```

```bash
docker compose -f deploy/compose.yaml logs mlop
```

The second command shows the API key printed on first start. The image is `python:alpine` plus
the `mlop_server` folder. (The Dockerfile has not been exercised in CI yet — the daemon was not
available where this was written — so treat it as a starting point.)

## HTTPS and exposure

The server speaks plain HTTP. On a home or lab network that is usually fine. Before exposing it
beyond a network you trust, put it behind something that terminates TLS — Caddy, nginx, or a
Tailscale/WireGuard tunnel — because API keys travel in request headers. A two-line Caddyfile is
enough:

```
mlop.example.com {
    reverse_proxy localhost:2718
}
```

Behind a proxy the server reads `X-Forwarded-Proto` and `X-Forwarded-Host` to build run links, or
set `--public-url` explicitly.

## Backups and moving

All state is in the data directory: `mlop.db` (plus `mlop.db-wal` while running) and `files/`.

- Stopped server: copy the directory.
- Running server: `sqlite3 ~/.mlop/server/mlop.db ".backup backup.db"` gives a consistent copy of
  the database; then copy `files/`.

To move to another machine, copy the directory and start the server there with `--data`.

## Disk use

A metric point costs about 29 bytes, so a run logging 20 metrics every step for 100,000 steps is
around 60 MB. System metrics add roughly a dozen series sampled every 4 seconds. Uploaded media is
stored as-is. Deleting a run or project in the UI removes its rows and files; the database file
does not shrink on its own, but the space is reused. To hand it back to the filesystem, stop the
server and run `sqlite3 mlop.db "VACUUM"`.

## Limits worth knowing

- One writer: SQLite serialises writes. On desktop hardware that is hundreds of thousands of
  points per second, which is far more than a small team produces; it is not a design for hundreds
  of simultaneous heavy trainers.
- No per-user permissions, no retention policies, no e-mail.
- Charts are downsampled on read by scanning the series, so a 10-million-point series takes
  proportionally longer to draw than a 100,000-point one.
