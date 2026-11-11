# Architecture

```
 training script                        the server (one process)
┌────────────────────┐                ┌───────────────────────────────────────┐
│ mini_mlop (Python) │   HTTP         │ mini_mlop_server                           │
│  log() → queue ────┼── NDJSON ─────▶│  app.py    routing, auth, streaming   │
│  files → uploader ─┼── PUT ────────▶│  store.py  SQLite + files on disk     │
│  heartbeat ────────┼── POST ───────▶│  web/      static UI (no build step)  │
└────────────────────┘                └──────────────┬────────────────────────┘
        browser ──────── GET /api/… ─────────────────┘
                                         <data>/mlop.db     <data>/files/<run>/
```

## Repository layout

| Path | What |
|---|---|
| `mini_mlop/` | The logging client. Depends on `httpx` and `psutil`. |
| `mini_mlop/compat/` | PyTorch, Lightning and Transformers integrations. |
| `mini_mlop_server/` | The server. Standard library only; never imports `mini_mlop`. |
| `mini_mlop_server/web/` | The web UI: `index.html`, `app.js`, `chart.js`, `style.css`. |
| `tests/` | End-to-end tests: real client, real server, real HTTP. |
| `deploy/` | Dockerfile, Compose file, systemd unit. |
| `examples/` | Demo script and notebooks. |

One `pyproject.toml` builds one distribution, `mini-mlop`, containing both packages. A test
(`test_server_imports_nothing_but_the_standard_library`) keeps the server dependency-free.

## Design choices

**One process, standard library only.** The target is a small always-on box. `http.server` with a
thread per connection is not the fastest HTTP stack, but the client batches hundreds of log calls
into each request, so request rate is low and the time goes into SQLite, which is C. In exchange
there is nothing to compile, no wheels to find for ARM, and the install cannot break.

**SQLite in WAL mode, one writer.** A single connection does all writes behind a lock; four
read-only connections serve queries, so charts stay responsive during ingest. Cache sizes are
fixed (4 MiB for the writer, 2 MiB per reader), which is why memory does not grow with data.
`synchronous=NORMAL` means a power cut can lose the last moments of writes but cannot corrupt
the database.

**Series ids instead of repeated strings.** Each `(run, name)` pair gets an integer id. A metric
row is `(series_id, step, time, value)` in a clustered `WITHOUT ROWID` table keyed on the first
three, so a chart is one contiguous range scan and a point costs ~29 bytes. The same key makes
re-sent batches idempotent: a client retry overwrites rather than duplicates.

**Downsample on read.** The UI asks for at most a few hundred points per chart. Longer series are
grouped into that many buckets in SQL, returning each bucket's mean, min and max; the chart draws
the mean as the line and min–max as a band, so spikes survive the reduction.

**Streaming ingest.** Request bodies (chunked or not) are parsed line by line and written in
batches of 5,000 rows, each its own short transaction. A slow or huge upload never holds the
write lock for long and never sits in memory whole.

**Files on disk, named by number.** Uploads land in `<data>/files/<run id>/<file id>.<ext>`.
Client-supplied names are kept in the database for display but never touch a path. Files are
served with `Content-Security-Policy: sandbox`, and anything that is not an image, audio, video
or plain text is sent as a download, so an uploaded HTML file cannot run as a page on the
server's origin.

**API keys only.** Keys are random 192-bit tokens stored as SHA-256 hashes. The web UI signs in
with the same key, held in an `HttpOnly; SameSite=Strict` cookie; cookie-authenticated writes
are additionally refused when the `Origin` header names another site.

**Heartbeats.** The client checks in every few seconds. A run silent for `--stale-after` seconds
is shown as failed ("Stalled"), and flips back to running if the client returns — a laptop lid
closing should not permanently fail a run. The same check-in carries the answer to "has someone
pressed Stop?".

## HTTP API

All routes except `/api/health`, `/api/login`, `/api/logout` and the static UI require
`Authorization: Bearer <key>` (or the session cookie).

Written by the client (paths kept from upstream mlop's v1 protocol, all on one origin):

| | |
|---|---|
| `POST /api/slug` | Check a key |
| `POST /api/runs/create` | `{projectName, runName, config, systemMetadata, loggerSettings}` → `{runId, url}` |
| `POST /api/runs/status/update` | `{runId, status, statusMetadata}` |
| `POST /api/runs/logName/add` | `{runId, logName: [...], logType}` — declare series and their kind |
| `POST /api/runs/trigger` | `{runId}` → `{status}` — heartbeat; `CANCELLED` means stop |
| `POST /api/runs/alert` | `{runId, alert: {title, body, level, timestamp}}` |
| `POST /api/runs/modelGraph/create` | `{runId, graph}` |
| `POST /ingest/metrics` | NDJSON `{time, step, data: {name: value}}`; header `X-Run-Id` |
| `POST /ingest/data` | NDJSON `{time, step, logName, dataType, data}` |
| `POST /ingest/logs` | NDJSON `{time, message, logType}` |
| `POST /files` | `{files: [{fileName, fileType, fileSize, logName, step, time}]}` → upload paths |
| `PUT /api/files/<id>` | File bytes |

Read by the UI (and usable from scripts):

| | |
|---|---|
| `GET /api/projects` | Projects with run counts |
| `GET /api/projects/<name>/runs` | Runs with config and metric names |
| `GET /api/runs/<id>` | Run details, series list, alerts |
| `GET /api/runs/<id>/metrics?name=…&name=…&points=500&x=step\|time` | Downsampled series |
| `GET /api/runs/<id>/data?name=…&limit=64` | Histogram / table entries |
| `GET /api/runs/<id>/files?name=…` | File listing; `GET /api/files/<id>` for content (supports `Range`) |
| `GET /api/runs/<id>/logs?after=<id>&limit=…` | Console lines |
| `GET /api/runs/<id>/graph` | Model graph |
| `POST /api/runs/<id>/stop`, `DELETE /api/runs/<id>`, `DELETE /api/projects/<name>` | |
| `GET/POST /api/keys`, `DELETE /api/keys/<id>`, `GET /api/stats`, `GET /api/me` | |

## Not built yet

Roughly in the order they seem worth doing: measurements on real Raspberry Pi hardware; retention
rules (auto-delete or thin old runs); pre-aggregated rollups for very long series; run tags, notes
and search; CSV export; alert forwarding from the server; a published PyPI package and container
image.
