# Running the server

```bash
mini-mlop-server
```

`python -m mini_mlop_server` and `mini-mlop server` do the same thing. The server needs Python 3.10+ and
nothing else — if you would rather not pip-install on the box, copying the `mini_mlop_server/` folder
and running `python -m mini_mlop_server` next to it works too.

## Options

| Flag | Environment | Default | |
|---|---|---|---|
| `--data DIR` | `MINI_MLOP_DATA_DIR` | `~/.mini-mlop/server` | Database and uploaded files |
| `--host ADDR` | `MINI_MLOP_HOST` | `0.0.0.0` | Listen address; `127.0.0.1` for this machine only |
| `--port N` | `MINI_MLOP_PORT` | `2718` | |
| `--public-url URL` | `MINI_MLOP_PUBLIC_URL` | from each request | Address used in the run links sent to clients |
| `--no-auth` | `MINI_MLOP_NO_AUTH=1` | off | Accept requests without a key |
| `--stale-after SECONDS` | | `300` | Mark a run failed after this long with no heartbeat |
| `--max-upload-mb N` | | `2048` | Largest accepted file or ingest request |
| `--verbose` | | off | Log every request |
| | `MINI_MLOP_SECRET_KEY` | key file | Secret that unlocks saved provider keys; see [Secrets](#secrets) |

## API keys

Every request needs a key unless the server runs with `--no-auth`. The first start creates one
and prints it once. After that:

```bash
mini-mlop-server keys create laptop
```

```bash
mini-mlop-server keys list
```

```bash
mini-mlop-server keys revoke 2
```

These work whether or not the server is running (pass the same `--data` if you changed it), so a
lost key is never a lock-out. Keys can also be created and revoked from **Settings** in the web
UI. Only a SHA-256 hash of each key is stored.

The same key signs you in to the web UI. There are no user accounts; a key is the identity. Give
each person, training job and agent its own, so one can be revoked without disturbing the others.

### Access levels and project keys

A key has an access level, and can be limited to one project:

| Access | Can |
|---|---|
| `read` | View runs, charts, files and logs; query; use MCP and the assistant |
| `write` | Everything `read` can, plus log runs and stop or delete them |
| `admin` | Everything, including creating and revoking keys, assistant settings and deleting projects |

```bash
mini-mlop-server keys create ci-mnist --project mnist                 # write, mnist only
```

```bash
mini-mlop-server keys create agent --project mnist --access read      # read, mnist only
```

```bash
mini-mlop-server keys create dashboard --access read                  # read, every project
```

A key limited to a project can only log to that project, and everything else on the server is
invisible to it: other projects' runs, files and assistant conversations return "not found", and
its queries (SQL, MCP, the assistant) see only its project's rows. Admin keys are never limited to
a project. The first key the server prints, and any key made before access levels existed, is an
admin key.

What this is for: keeping teammates and jobs out of each other's projects by accident, and giving
agents least privilege. It is not a multi-tenant boundary for people who distrust each other;
everyone shares one server, one disk and one assistant budget.

`--no-auth` is for a machine only you can reach (a laptop, or a private tailnet). With it, anyone
who can connect to the port can read and delete your data.

## Secrets

The server stores two kinds of secret, differently:

- **Its own API keys** are stored only as hashes and cannot be recovered.
- **Keys it must send elsewhere** (the assistant's model-provider key and a sandbox runner token)
  are stored encrypted, because the server needs the original value.

Set `MINI_MLOP_SECRET_KEY` to a long random value to control that encryption:

```bash
export MINI_MLOP_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

With it set, a copy of the data directory or a backup does not reveal the provider keys. Keep the
value somewhere other than the data directory (your process manager's environment, a secret
store) and keep it stable: if it changes, the saved keys cannot be read and Settings will ask for
them again.

Without it, the server generates `secret.key` in the data directory and uses that. A stray copy of
the database file alone is then still safe, but a copy of the whole directory is not.

The encryption is built from the Python standard library (scrypt, SHAKE-256, HMAC-SHA-256) so the
server stays free of dependencies. It protects data at rest; it does not protect against someone
who can read the server's memory or environment.

## Keeping it running

**systemd** (Debian, Ubuntu, Raspberry Pi OS…): [`deploy/mini-mlop-server.service`](../deploy/mini-mlop-server.service)
has the unit and the four commands to install it in its header.

**Docker**: from the repository root,

```bash
docker compose -f deploy/compose.yaml up -d
```

```bash
docker compose -f deploy/compose.yaml logs mini-mlop
```

The second command shows the API key printed on first start. The image is `python:alpine` plus
the `mini_mlop_server` folder. (The Dockerfile has not been exercised in CI yet — the daemon was not
available where this was written — so treat it as a starting point.)

**Fly.io** (cheap, sleeps when idle): [`fly.toml`](../fly.toml) runs the server on the smallest
Fly machine (shared CPU, 256 MB) with a 1 GB volume for the data. When nothing has talked to the
server for a few minutes, Fly suspends the machine, and the next request (the web UI, `mini-mlop`
or a training job's logger) wakes it in well under a second. While a run is logging, its
heartbeats keep the machine awake. From the repository root, with
[`flyctl`](https://fly.io/docs/flyctl/install/) installed and `fly auth login` done:

```bash
fly launch --copy-config --no-deploy
```

```bash
fly volumes create mini_mlop_data --size 1 --region iad
```

```bash
fly deploy
```

```bash
fly logs
```

`fly launch` asks for an app name (write the one you pick into `fly.toml`); create the volume in
the same region as `primary_region`. `fly logs` shows the API key printed on first start, and the
server is at `https://<app>.fly.dev`. Optionally set `fly secrets set MINI_MLOP_SECRET_KEY=...`
before the first deploy (see [Secrets](#secrets)). Fly terminates HTTPS, and run links pick up
the public address from its forwarded headers. For a full shutdown instead of suspend, set
`auto_stop_machines = "stop"`: it costs nothing but disk while stopped, and starts in a second or
two. The volume is billed whether the machine runs or not.

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

All state is in the data directory: `mini-mlop.db` (plus `mini-mlop.db-wal` while running) and `files/`.

- Stopped server: copy the directory.
- Running server: `sqlite3 ~/.mini-mlop/server/mini-mlop.db ".backup backup.db"` gives a consistent copy of
  the database; then copy `files/`.

To move to another machine, copy the directory and start the server there with `--data`.

## Disk use

A metric point costs about 29 bytes, so a run logging 20 metrics every step for 100,000 steps is
around 60 MB. System metrics add roughly a dozen series sampled every 4 seconds. Uploaded media is
stored as-is. Deleting a run or project in the UI removes its rows and files; the database file
does not shrink on its own, but the space is reused. To hand it back to the filesystem, stop the
server and run `sqlite3 mini-mlop.db "VACUUM"`.

## Limits worth knowing

- One writer: SQLite serialises writes. On desktop hardware that is hundreds of thousands of
  points per second, which is far more than a small team produces; it is not a design for hundreds
  of simultaneous heavy trainers.
- No user accounts (keys carry the access level), no retention policies, no e-mail.
- Charts are downsampled on read by scanning the series, so a 10-million-point series takes
  proportionally longer to draw than a 100,000-point one.
