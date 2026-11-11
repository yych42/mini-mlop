# Agents: the query interface, MCP, and the built-in assistant

Everything the server stores can be read with SQL. This is the interface meant for coding
agents, notebooks and scripts: instead of a fixed list of analysis endpoints, you (or your agent)
write the question.

There are three ways in, all backed by the same endpoint:

```bash
mini-mlop query                       # print the tables, columns, notes and examples
mini-mlop query "SELECT id, project, name, status FROM runs ORDER BY id DESC"
mini-mlop query --format csv - < question.sql
```

```bash
curl -s -H "Authorization: Bearer $MLOP_API_KEY" "$MLOP_URL/api/query/schema"
curl -s -H "Authorization: Bearer $MLOP_API_KEY" "$MLOP_URL/api/query" \
     -d '{"sql": "SELECT name, status FROM runs WHERE project = ?", "params": ["mnist"]}'
```

And the **Query** page in the web UI, which lists the tables and has runnable examples.

## For agents

Point a coding agent at this file, or just tell it: *"run `mini-mlop query` to see what is
available, then answer my question with `mini-mlop query "<sql>"`"*. The schema output is written
to be read by a model: every table and column is described, the conventions are spelled out, and
the examples cover the common shapes (compare runs, find a shift, find the biggest jumps, read
errors from logs).

`GET /api/query/schema` returns the same thing as JSON.

## Request and response

`POST /api/query` with a JSON body:

| Field | | |
|---|---|---|
| `sql` | required | One `SELECT` (or `WITH … SELECT`) statement |
| `params` | optional | List for `?` placeholders, or object for `:name` placeholders |
| `limit` | optional | Most rows to return; default 1000, at most 50000 |
| `format` | optional | `json` (default) or `csv` |

`GET /api/query?sql=…&limit=…&format=…` does the same for quick use.

```json
{"columns": ["name", "best"], "rows": [["baseline", 1.59], ["wide", 0.98]],
 "row_count": 2, "truncated": false, "elapsed_ms": 0.4}
```

`truncated` is true when the row or size limit cut the result short. Errors come back as HTTP 400
with `{"error": "..."}`; SQL mistakes carry SQLite's own message so they can be corrected.

## Tables

`runs`, `series`, `metrics`, `data`, `files`, `logs`, `alerts`, `projects`. Run
`mini-mlop query` for the columns. The conventions:

- Times are Unix milliseconds: `datetime(created_at / 1000, 'unixepoch')`.
- `runs.config`, `runs.system` and `data.body` are JSON: `json_extract(config, '$.lr')`.
- `metrics` is the big table. Filter it by `run_id` and `name`, and aggregate in SQL rather than
  pulling every point.
- The dialect is SQLite, with window functions, common table expressions and the JSON functions.

## What it will not do

- **Write.** The database is opened read-only and only `SELECT` is authorised; `INSERT`, `UPDATE`,
  `DELETE`, DDL, `PRAGMA` and `ATTACH` are refused.
- **Reveal API keys.** The key table cannot be read through this interface.
- **Run forever.** A query is stopped after 10 seconds, and one query runs at a time so a heavy
  one cannot slow logging or the UI.

Anyone holding an API key can read every project through this interface — the same access the
rest of the API gives.

## MCP

The server speaks the Model Context Protocol at `/mcp` (Streamable HTTP, JSON responses), with
two tools: `schema` and `query`. Authenticate with an API key as a bearer token.

```bash
claude mcp add --transport http mini-mlop http://your-server:2718/mcp \
    --header "Authorization: Bearer $MLOP_API_KEY"
```

Any MCP client that supports HTTP servers with a custom header works the same way. The endpoint
has been tested against the protocol's request and response shapes, not yet against a particular
client.

## The built-in assistant

The **Ask** button in the web UI opens a chat panel. The assistant knows which run or project you
are looking at, answers by running SQL through the same query interface, and shows each query it
ran so the answer can be checked. It is read-only: it cannot stop, change or delete anything.

Set it up under **Settings → Assistant**: pick a provider, paste a key, choose a model.

### Model providers

| Provider | Wire format | Notes |
|---|---|---|
| OpenRouter | OpenAI Chat Completions | The first supported provider; one key, many models |
| OpenAI | OpenAI Chat Completions | |
| Anthropic | Anthropic Messages | |
| Other (OpenAI-compatible) | OpenAI Chat Completions | Any base URL, including local servers |
| Other (Anthropic-compatible) | Anthropic Messages | Any base URL |

Both wire formats are implemented in `mini_mlop_server/llm.py` behind one function, so adding a
provider is an entry in a table. All of them are covered by tests that play the provider with a
local server speaking the real format; none has been run against the live service from this
repository yet, so treat the first real call as the test.

What to know before turning it on:

- **Data leaves your server.** Your question, and the run names, configs, metric values and log
  lines the assistant looks up, are sent to the provider you choose.
- **Keys are stored in the server's database**, unencrypted, readable by anyone with access to the
  data directory. They are never returned by the API and cannot be read through the query
  interface.
- **Spending is capped** by a daily token limit (default 2,000,000) and 12 model calls per
  question. Anyone with an API key to your server can use the assistant.
- Answers arrive a step at a time, not word by word.

### Code sandbox

SQL covers finding, comparing and aggregating. For real statistics the assistant can write and
run Python, if you give it somewhere to run. Query results are handed to the code as CSV files and
only printed output comes back, so a sandbox never needs a network route to your server.

| Sandbox | What separates the code from your server |
|---|---|
| Off (default) | Nothing runs; the assistant uses SQL only |
| Remote runner | A different machine. It sees only the query results sent with each run |
| This machine | **Nothing.** A subprocess with CPU, memory and time limits that can read the server's files, including the database and saved keys |

The code is written by a model that has read your run names and log lines, which a training
script (or anyone who can log to your server) controls. Use "This machine" only when everything
that logs to the server is trusted.

**Remote runner.** Run this on a machine that holds nothing you care about, such as a small
container on Fly, Railway, Modal or Cloud Run:

```bash
docker build -f deploy/sandbox.Dockerfile -t mini-mlop-sandbox .
docker run -p 8700:8700 -e MLOP_SANDBOX_TOKEN=a-long-random-secret mini-mlop-sandbox
```

or without Docker: `MLOP_SANDBOX_TOKEN=… python -m mini_mlop_server.sandbox_runner`. Then enter
its URL and token in Settings. Put it behind HTTPS if it is reachable from the internet.

**Other sandbox providers.** A provider is one class with one method:

```python
class Sandbox:
    def run(self, code, files, timeout):   # files: {name: bytes}
        return {"stdout": str, "stderr": str, "exit_code": int | None,
                "timed_out": bool, "duration_ms": int}
```

registered in `PROVIDERS` in `mini_mlop_server/sandbox.py`. An adapter for a vendor's sandbox API
(Cloudflare, Modal, E2B and so on) implements `run()` with that vendor's calls and declares the
settings fields it needs; the assistant and the settings page pick it up from the registry. None
is shipped yet. The remote runner's protocol is two endpoints (`GET /health`, `POST /run`),
documented at the top of `mini_mlop_server/sandbox_runner.py`, for anyone who would rather
implement the runner side on their platform.
