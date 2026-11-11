"""Read-only SQL over everything the server stores.

This is the open-ended interface for people and agents: instead of a fixed
set of analysis endpoints, any SELECT can be run against a handful of
friendly tables. `describe()` is the machine-readable manual.

Safety comes from four independent layers:

  * the connection opens the database file read-only;
  * an authorizer allows SELECT and nothing else, and hides API keys;
  * a time limit interrupts runaway queries;
  * row and size limits bound the response.

One query runs at a time, on its own connection, so a heavy question cannot
starve the readers that serve the web UI and ingest.
"""

import math
import secrets
import sqlite3
import threading
import time

DEFAULT_ROWS = 1000
MAX_ROWS = 50_000
MAX_BYTES = 8 * 1024 * 1024  # approximate cap on one response
MAX_SQL = 20_000
TIMEOUT_S = 10.0
MAX_VALUE_BYTES = 4 * 1024 * 1024  # largest string/blob a query may build

# Friendly shapes over the storage tables: names instead of integer series
# ids, project names instead of project ids. They live in the connection's
# temp schema, so they shadow the raw tables for queries only.
VIEWS = """
CREATE TEMP VIEW runs AS
    SELECT r.id, p.name AS project, r.name, r.status, r.stalled,
           r.config, r.system, r.created_at, r.heartbeat_at, r.finished_at
    FROM main.runs r JOIN main.projects p ON p.id = r.project_id;
CREATE TEMP VIEW series AS
    SELECT run_id, name, kind FROM main.series;
CREATE TEMP VIEW metrics AS
    SELECT s.run_id, s.name, m.step, m.time, m.value
    FROM main.metrics m JOIN main.series s ON s.id = m.series_id;
CREATE TEMP VIEW data AS
    SELECT s.run_id, s.name, s.kind, d.step, d.time, d.body
    FROM main.data d JOIN main.series s ON s.id = d.series_id;
CREATE TEMP VIEW files AS
    SELECT f.id, f.run_id, s.name, f.step, f.time,
           f.name AS filename, f.ext, f.size, f.uploaded
    FROM main.files f JOIN main.series s ON s.id = f.series_id;
"""

# The same shapes restricted to one project, for callers that must not see
# anything else (the built-in assistant working inside a project).
#
# How the restriction holds: each public view reads from an inner view that
# applies the project filter, and the authorizer allows a stored table to be
# read only when the innermost view doing the reading is one of those inner
# views. SQLite reports a CTE or subquery alias in the same slot, and a query
# may name those anything it likes, so the inner views carry a random name
# chosen per connection and the catalog that would reveal it is unreadable.
_SCOPED_SHAPES = {
    "runs": "SELECT r.id, p.name AS project, r.name, r.status, r.stalled, "
    "r.config, r.system, r.created_at, r.heartbeat_at, r.finished_at "
    "FROM main.runs r JOIN main.projects p ON p.id = r.project_id "
    "WHERE p.name = mlop_project()",
    "projects": "SELECT id, name, created_at FROM main.projects WHERE name = mlop_project()",
    "series": "SELECT run_id, name, kind FROM main.series WHERE run_id IN {scope}",
    "metrics": "SELECT s.run_id, s.name, m.step, m.time, m.value "
    "FROM main.metrics m JOIN main.series s ON s.id = m.series_id "
    "WHERE s.run_id IN {scope}",
    "data": "SELECT s.run_id, s.name, s.kind, d.step, d.time, d.body "
    "FROM main.data d JOIN main.series s ON s.id = d.series_id "
    "WHERE s.run_id IN {scope}",
    "files": "SELECT f.id, f.run_id, s.name, f.step, f.time, f.name AS filename, "
    "f.ext, f.size, f.uploaded FROM main.files f "
    "JOIN main.series s ON s.id = f.series_id WHERE f.run_id IN {scope}",
    "logs": "SELECT id, run_id, time, level, message FROM main.logs WHERE run_id IN {scope}",
    "alerts": "SELECT id, run_id, time, level, title, body FROM main.alerts "
    "WHERE run_id IN {scope}",
}
_IN_SCOPE = (
    "(SELECT r.id FROM main.runs r JOIN main.projects p ON p.id = r.project_id "
    "WHERE p.name = mlop_project())"
)
_CATALOGS = {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}
_EXPLAIN_COLUMNS = (["addr", "opcode"], ["id", "parent", "notused", "detail"])

TABLES = [
    {
        "name": "runs",
        "description": "One row per run.",
        "columns": [
            ["id", "INTEGER", "run id; other tables refer to it as run_id"],
            ["project", "TEXT", "project name"],
            ["name", "TEXT", "run name"],
            ["status", "TEXT", "RUNNING, COMPLETED, FAILED, TERMINATED or CANCELLED"],
            ["stalled", "INTEGER", "1 when a RUNNING run has stopped sending heartbeats"],
            ["config", "TEXT", "JSON object passed to init(config=...); may be NULL"],
            ["system", "TEXT", "JSON: host, python, git and hardware details; may be NULL"],
            ["created_at", "INTEGER", "start time"],
            ["heartbeat_at", "INTEGER", "last sign of life"],
            ["finished_at", "INTEGER", "end time; NULL while running"],
        ],
    },
    {
        "name": "series",
        "description": "One row per named thing a run logged.",
        "columns": [
            ["run_id", "INTEGER", ""],
            ["name", "TEXT", "e.g. train/loss; system metrics start with sys/"],
            ["kind", "TEXT", "METRIC for scalars; HISTOGRAM, TABLE, IMAGE, AUDIO, VIDEO, TEXT, ARTIFACT"],
        ],
    },
    {
        "name": "metrics",
        "description": "Scalar points. Filter by run_id and name to stay fast.",
        "columns": [
            ["run_id", "INTEGER", ""],
            ["name", "TEXT", "series name"],
            ["step", "INTEGER", ""],
            ["time", "INTEGER", ""],
            ["value", "REAL", ""],
        ],
    },
    {
        "name": "data",
        "description": "Structured values: histograms and tables.",
        "columns": [
            ["run_id", "INTEGER", ""],
            ["name", "TEXT", "series name"],
            ["kind", "TEXT", ""],
            ["step", "INTEGER", ""],
            ["time", "INTEGER", ""],
            ["body", "TEXT", "JSON payload"],
        ],
    },
    {
        "name": "files",
        "description": "Uploaded media and artifacts (metadata; bytes are at /api/files/<id>).",
        "columns": [
            ["id", "INTEGER", "file id"],
            ["run_id", "INTEGER", ""],
            ["name", "TEXT", "series name"],
            ["step", "INTEGER", ""],
            ["time", "INTEGER", ""],
            ["filename", "TEXT", ""],
            ["ext", "TEXT", ""],
            ["size", "INTEGER", "bytes"],
            ["uploaded", "INTEGER", "1 once the bytes have arrived"],
        ],
    },
    {
        "name": "logs",
        "description": "Captured stdout/stderr lines.",
        "columns": [
            ["id", "INTEGER", "increases in arrival order"],
            ["run_id", "INTEGER", ""],
            ["time", "INTEGER", ""],
            ["level", "TEXT", "INFO, ERROR, ..."],
            ["message", "TEXT", ""],
        ],
    },
    {
        "name": "alerts",
        "description": "Alerts raised with run.alert().",
        "columns": [
            ["id", "INTEGER", ""],
            ["run_id", "INTEGER", ""],
            ["time", "INTEGER", ""],
            ["level", "TEXT", ""],
            ["title", "TEXT", ""],
            ["body", "TEXT", ""],
        ],
    },
    {
        "name": "projects",
        "description": "One row per project.",
        "columns": [
            ["id", "INTEGER", ""],
            ["name", "TEXT", ""],
            ["created_at", "INTEGER", ""],
        ],
    },
]

EXAMPLES = [
    {
        "question": "Which runs are in a project, newest first, with a config value?",
        "sql": "SELECT id, name, status, json_extract(config, '$.lr') AS lr "
        "FROM runs WHERE project = 'mnist' ORDER BY id DESC",
    },
    {
        "question": "Final and best value of a metric for every run in a project",
        "sql": "SELECT r.id, r.name, min(m.value) AS best, max(m.step) AS last_step, "
        "(SELECT value FROM metrics WHERE run_id = r.id AND name = 'val/loss' "
        "ORDER BY step DESC LIMIT 1) AS final "
        "FROM runs r JOIN metrics m ON m.run_id = r.id "
        "WHERE r.project = 'mnist' AND m.name = 'val/loss' GROUP BY r.id",
    },
    {
        "question": "Did a metric shift? Compare its mean in consecutive 1000-step windows",
        "sql": "SELECT step / 1000 * 1000 AS from_step, count(*) AS n, avg(value) AS mean, "
        "min(value) AS lo, max(value) AS hi FROM metrics "
        "WHERE run_id = 12 AND name = 'train/loss' GROUP BY 1 ORDER BY 1",
    },
    {
        "question": "Largest single-step jumps in a metric",
        "sql": "SELECT step, value, value - lag(value) OVER (ORDER BY step) AS delta "
        "FROM metrics WHERE run_id = 12 AND name = 'train/loss' "
        "ORDER BY abs(delta) DESC LIMIT 10",
    },
    {
        "question": "Two runs side by side at matching steps",
        "sql": "SELECT a.step, a.value AS run_12, b.value AS run_15, b.value - a.value AS diff "
        "FROM metrics a JOIN metrics b ON b.step = a.step AND b.name = a.name "
        "WHERE a.run_id = 12 AND b.run_id = 15 AND a.name = 'val/acc' ORDER BY a.step",
    },
    {
        "question": "Error lines from a run's output",
        "sql": "SELECT datetime(time / 1000, 'unixepoch') AS at, message FROM logs "
        "WHERE run_id = 12 AND (level = 'ERROR' OR message LIKE '%Traceback%') ORDER BY id",
    },
]

_SQLITE_READ = 20
_SQLITE_SELECT = 21
_SQLITE_FUNCTION = 31
_SQLITE_RECURSIVE = 33
_HIDDEN_TABLES = {"api_keys", "settings", "chats"}
_BLOCKED_FUNCTIONS = {"load_extension", "zeroblob", "randomblob"}


class QueryError(Exception):
    """The query was refused or failed; the message is safe to show."""


def describe():
    return {
        "dialect": "SQLite %s" % sqlite3.sqlite_version,
        "endpoint": "POST /api/query with JSON {sql, params?, limit?, format?, project?} "
        "or GET /api/query?sql=...; format is json (default) or csv",
        "notes": [
            "Read-only: one SELECT (or WITH ... SELECT) statement per request.",
            "All *_at and time columns are Unix time in milliseconds; "
            "datetime(time / 1000, 'unixepoch') makes them readable.",
            "config, system and body hold JSON; use json_extract(config, '$.key') "
            "and json_each().",
            "Window functions and common table expressions are available.",
            "Always filter metrics by run_id and name; aggregate in SQL instead "
            "of fetching every point.",
            "Bind values with ? or :name placeholders via params.",
            "Projects are unrelated to each other. SELECT name FROM projects lists "
            "them; pass project to confine a query to one, so every table holds only "
            "that project's rows.",
        ],
        "limits": {
            "default_rows": DEFAULT_ROWS,
            "max_rows": MAX_ROWS,
            "max_response_bytes": MAX_BYTES,
            "timeout_seconds": TIMEOUT_S,
            "concurrent_queries": 1,
        },
        "tables": TABLES,
        "examples": EXAMPLES,
    }


def _authorize(action, arg1, arg2, _db, _source):
    if action == _SQLITE_READ:
        return sqlite3.SQLITE_DENY if arg1 in _HIDDEN_TABLES else sqlite3.SQLITE_OK
    return _authorize_rest(action, arg2)


def _authorize_rest(action, arg2):
    if action == _SQLITE_FUNCTION:
        blocked = (arg2 or "").lower() in _BLOCKED_FUNCTIONS
        return sqlite3.SQLITE_DENY if blocked else sqlite3.SQLITE_OK
    if action in (_SQLITE_SELECT, _SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None  # JSON has no NaN or Infinity
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    return value


def _size(value):
    if isinstance(value, str):
        return len(value) + 3
    return 12


class QueryEngine:
    """`scoped=True` makes an engine whose every query is confined to the
    project passed to run(). Scoped and unscoped queries never share a
    connection, so a statement prepared under one rule set cannot be reused
    under the other."""

    def __init__(self, path, timeout=TIMEOUT_S, scoped=False):
        self.path = path
        self.timeout = timeout
        self.scoped = scoped
        self._project = None
        self._lock = threading.Lock()
        self._c = None
        self._deadline = 0.0

    def _connect(self):
        c = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=5,
        )
        c.execute("PRAGMA cache_size=-2048")  # 2 MiB
        if self.scoped:
            c.create_function("mlop_project", 0, lambda: self._project, deterministic=True)
            token = secrets.token_hex(16)
            inner = {name: f"v_{token}_{name}" for name in _SCOPED_SHAPES}
            for name, shape in _SCOPED_SHAPES.items():
                c.execute(f"CREATE TEMP VIEW {inner[name]} AS " + shape.format(scope=_IN_SCOPE))
                c.execute(f"CREATE TEMP VIEW {name} AS SELECT * FROM {inner[name]}")
            trusted = set(inner.values())

            def authorize(action, arg1, arg2, db, source):
                if action == _SQLITE_READ:
                    if arg1 in _HIDDEN_TABLES or arg1 in _CATALOGS:
                        return sqlite3.SQLITE_DENY
                    if db == "main" and source not in trusted:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                return _authorize_rest(action, arg2)
        else:
            c.executescript(VIEWS)
            authorize = _authorize
        c.execute("PRAGMA query_only=ON")
        if hasattr(c, "setlimit"):  # Python 3.11+
            c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        c.set_progress_handler(lambda: time.monotonic() > self._deadline, 5000)
        c.set_authorizer(authorize)
        return c

    def run(self, sql, params=None, limit=None, project=None):
        """Returns {columns, rows, row_count, truncated, elapsed_ms}."""
        if self.scoped != (project is not None):
            raise QueryError("this query engine was asked for the wrong scope")
        if not isinstance(sql, str) or not sql.strip():
            raise QueryError("sql is required")
        if len(sql) > MAX_SQL:
            raise QueryError(f"sql is longer than {MAX_SQL} characters")
        if params is None:
            params = ()
        elif not isinstance(params, (list, dict)):
            raise QueryError("params must be a list or an object")
        try:
            limit = DEFAULT_ROWS if limit is None else int(limit)
        except (TypeError, ValueError):
            raise QueryError("limit must be an integer")
        limit = max(1, min(limit, MAX_ROWS))

        if not self._lock.acquire(timeout=self.timeout):
            raise QueryError("another query is still running; try again shortly")
        try:
            if self._c is None:
                self._c = self._connect()
            started = time.monotonic()
            self._deadline = started + self.timeout
            self._project = project
            try:
                cur = self._c.execute(sql, params)
                if cur.description is None:
                    raise QueryError("only SELECT statements are allowed")
                columns = [d[0] for d in cur.description]
                if self.scoped and any(columns[: len(e)] == e for e in _EXPLAIN_COLUMNS):
                    raise QueryError("EXPLAIN is not available here")
                rows, size, truncated = [], 0, False
                for row in cur:
                    if len(rows) >= limit or size > MAX_BYTES:
                        truncated = True
                        break
                    row = [_clean(v) for v in row]
                    size += sum(_size(v) for v in row)
                    rows.append(row)
                cur.close()
            except sqlite3.Error as e:
                raise QueryError(self._explain(e)) from None
            except (ValueError, OverflowError) as e:  # bad bind values, NUL bytes
                raise QueryError(str(e)) from None
            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        finally:
            self._lock.release()

    def _explain(self, error):
        text = str(error)
        if "interrupted" in text:
            return (
                f"query stopped after {self.timeout:g} seconds; narrow it "
                "(filter metrics by run_id and name, aggregate, add LIMIT)"
            )
        if "not authorized" in text or "prohibited" in text or "readonly" in text:
            return "only read-only SELECT statements over the documented tables are allowed"
        if "one statement at a time" in text:
            return "send one statement per request"
        return text

    def close(self):
        with self._lock:
            if self._c is not None:
                self._c.close()
                self._c = None
