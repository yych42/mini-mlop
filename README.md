<img src="design/favicon.svg" alt="mini-mlop logo" height="72">

# mini-mlop

Experiment tracking for machine learning that is small enough to self-host anywhere, and built
to be asked questions by agents.

**mini-mlop** is a lightweight fork of [mlop](https://github.com/mlop-ai/mlop). You get the same
five-line Python logger, and a server that is **one process with zero dependencies**: no ClickHouse,
no PostgreSQL, no object store, no containers required. Everything — logger, server, web UI,
deployment files — lives in this one repository.

| | upstream mlop server | mini-mlop server |
|---|---|---|
| Processes | 9 containers (ClickHouse, PostgreSQL, MinIO, Traefik, Rust ingest, Next.js API, web frontend, Python service, bucket init) | 1 Python process |
| Source | 4 repositories linked by submodules | this repository |
| Install | Docker Compose, image builds | `pip install`, or copy one folder |
| Server dependencies | Rust, Node and Python toolchains plus three databases | Python standard library |
| Storage | ClickHouse + PostgreSQL + S3 | one SQLite file + a folder of uploads |
| Memory | gigabytes | ~23 MB idle, ~34 MB after ingesting and charting a million points¹ |

¹ Measured on a 4-core x86 VM with Python 3.13; see [Footprint](#footprint).

## Quick start

On the machine that will hold your data (Python 3.9+, nothing else needed):

```bash
pip install git+https://github.com/yych42/mini-mlop.git
```

```bash
mini-mlop-server
```

The first start prints an API key and the address of the web UI (port 2718 by default).

On the machine that trains:

```bash
pip install git+https://github.com/yych42/mini-mlop.git
```

```bash
export MINI_MLOP_URL=http://your-server:2718
```

```bash
mini-mlop login
```

```python
import mini_mlop as mlop

run = mlop.init(project="hello-world", config={"lr": 1e-3})
for step in range(1000):
    run.log({"train/loss": 1 / (step + 1)})
run.finish()
```

Open the printed link to watch the run live. `python examples/demo.py` logs three sample runs with
metrics, histograms, images and tables if you want something to look at first.

## What you get

- **Logger** (`mini_mlop`): scalars, histograms, tables, images, audio, video, text and file artifacts;
  console capture; system and NVIDIA GPU metrics; git state; PyTorch `watch()`, Lightning and
  Hugging Face Transformers integrations. Logging is queued and batched off your training thread.
- **Server** (`mini_mlop_server`): ingest API, SQLite storage, stalled-run detection and the web
  UI, in standard-library Python with no dependencies.
- **Keys for a small team**: admin, write and read keys, optionally limited to one project, so a
  teammate, a training job or an agent gets exactly the access it needs.
- **Agent ready**: see below.
- **Web UI**: project and run lists, live charts with smoothing and log scale, run comparison,
  histogram/table/media browsers with a step slider, console logs, run config and machine details,
  stop and delete, API-key management. No build step; ~20 KB over the wire.

## Agent ready

Your experiment data is meant to be questioned, by you or by an agent working for you. There are
three ways in, all built on the same read-only SQL interface, and all able to be confined to one
project. [docs/agents.md](docs/agents.md) is the full guide.

**From a coding agent or a script.** The whole dataset is a handful of SQL tables:

```bash
mini-mlop query                 # describes the tables, with examples; written to be read by a model
```

```bash
mini-mlop query --project mnist "SELECT name, json_extract(config, '$.lr') AS lr FROM runs"
```

Tell an agent to run the first command and it can work out the rest. The same thing is available
over HTTP (`POST /api/query`) and on the **Query** page of the web UI.

**Over MCP.** The server is an MCP server at `/mcp`, with `projects`, `schema` and `query` tools:

```bash
claude mcp add --transport http mini-mlop http://your-server:2718/mcp --header "Authorization: Bearer $MINI_MLOP_API_KEY"
```

Give the agent a read key limited to one project and that is all it can ever see.

**In the web UI.** The **Ask** panel is a built-in assistant: "which run has the best validation
loss?", "did anything change after step 1000?", "what went wrong in this run?". It answers by
running queries (and shows them), works inside the project you are looking at, and cannot change
anything. Set it up under **Settings → Assistant**:

- **Model provider**: bring your own key. OpenRouter is the first supported provider; OpenAI,
  Anthropic and any OpenAI- or Anthropic-compatible endpoint use the same code path.
- **Code sandbox** (optional): lets the assistant run Python for real statistics. Sandboxes are
  swappable behind one small interface; a remote runner you deploy yourself and an unisolated
  local option are included.
- Your question and the data the assistant looks up are sent to the provider you choose. Saved
  provider keys are encrypted; set `MINI_MLOP_SECRET_KEY` on the server.

Status: the assistant and MCP endpoint are covered by tests that play the provider and client
locally. They have not yet been run against a live provider or a third-party MCP client from this
repository.

## Documentation

- [Running the server](docs/server.md) — options, API keys and access, systemd, Docker, backups, HTTPS
- [Using the logger](docs/client.md) — API, settings, optional extras, integrations
- [Agents](docs/agents.md) — the query interface, MCP, the assistant, model providers, sandboxes
- [Architecture](docs/architecture.md) — how it fits together, the HTTP API, what was left out

## Footprint

Measured with the server's own process (`VmRSS`), one client posting batches of 5,000 steps × 10
metrics, on a 4-core 2.3 GHz Xeon VM, Python 3.13, SSD:

| | |
|---|---|
| Idle memory | 23 MB |
| Memory after ingesting 1,000,000 points and reading them back | 34 MB |
| Ingest rate | ~380,000 points/s |
| Disk | ~29 bytes per metric point |
| Chart query, 100,000-point series reduced to 600 buckets | ~65 ms |

Memory is bounded by fixed SQLite cache sizes rather than by data volume, so it should stay in this
range on smaller machines; throughput will scale with the CPU.

## What is different from upstream mlop

Removed, because they are what made the server heavy or only matter for a hosted service:
organisations, members and invites; OAuth and e-mail sign-in (auth is by API key, with access
levels and per-project keys); e-mail alerts
(alerts are stored and shown in the UI; `run.alert(url=...)` webhooks still work); billing and usage
limits; the hosted code-server launcher; the W&B migration tool; threshold triggers. The interactive
model-graph view is a plain tree.

Changed in the logger: it talks to one address (`MINI_MLOP_URL`) instead of four; keys are kept in
`~/.config/mini-mlop/credentials.json` instead of the system keyring; `numpy`, `pillow`, `soundfile`
and the NVIDIA bindings are optional extras (`pip install "mini-mlop[full] @ git+…"`); the import
name is `mini_mlop`, so it installs alongside upstream `mlop` without clashing. To move an existing
script over, change its first line to `import mini_mlop as mlop`; the rest of the API is the same.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The suite starts real servers on free ports and drives them with the real client; it takes about
a minute.

## License

Apache 2.0, same as upstream. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
