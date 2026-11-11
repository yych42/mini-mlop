# Asking questions of your runs: the query interface

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
