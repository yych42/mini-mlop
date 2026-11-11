"""SQLite-backed storage for the mlop server.

Everything the server knows lives in one directory:

    <data>/mlop.db        runs, metrics, logs, structured data, file index
    <data>/files/<run>/   uploaded media and artifacts

One connection does all the writing (SQLite allows a single writer anyway),
and a small pool of read-only connections serves queries, so memory use stays
flat no matter how many clients are connected.
"""

import hashlib
import json
import math
import os
import queue
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS api_keys(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    hint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    stalled INTEGER NOT NULL DEFAULT 0,
    config TEXT,
    system TEXT,
    settings TEXT,
    status_meta TEXT,
    graph TEXT,
    created_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS runs_by_project ON runs(project_id, id);
CREATE INDEX IF NOT EXISTS runs_by_status ON runs(status);

-- A series is one named thing logged by a run: a metric, a histogram, a set
-- of images... Rows in the tables below point at it by integer id, so a
-- metric point costs ~20 bytes instead of repeating project/run/name strings.
CREATE TABLE IF NOT EXISTS series(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    UNIQUE(run_id, name)
);

CREATE TABLE IF NOT EXISTS metrics(
    series_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    time INTEGER NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY(series_id, step, time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS data(
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    time INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS data_by_series ON data(series_id, step);

CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    series_id INTEGER NOT NULL,
    step INTEGER NOT NULL,
    time INTEGER NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_by_series ON files(series_id, step);
CREATE INDEX IF NOT EXISTS files_by_run ON files(run_id);

CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    time INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_by_run ON logs(run_id, id);

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    time INTEGER NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_by_run ON alerts(run_id, id);
"""

ACTIVE = "RUNNING"
STATUSES = ("RUNNING", "COMPLETED", "FAILED", "TERMINATED", "CANCELLED")
FILE_KINDS = {
    "png": "IMAGE", "jpg": "IMAGE", "jpeg": "IMAGE", "gif": "IMAGE",
    "webp": "IMAGE", "svg": "IMAGE", "bmp": "IMAGE",
    "wav": "AUDIO", "mp3": "AUDIO", "ogg": "AUDIO", "flac": "AUDIO",
    "mp4": "VIDEO", "webm": "VIDEO", "mov": "VIDEO",
    "txt": "TEXT",
}  # fmt: skip
KEY_PREFIX = "mlop_"
HEARTBEAT_WRITE_INTERVAL_MS = 15_000
INSERT_BATCH = 5000
MAX_NAME = 512
MAX_MESSAGE = 16_384


class NotFound(Exception):
    pass


class Invalid(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def hash_key(key):
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _json_field(value):
    """Clients send nested JSON as strings; store one canonical encoding."""
    if value is None:
        return None
    if isinstance(value, str):
        parsed = _loads(value)
        return None if parsed is None else json.dumps(parsed)
    return json.dumps(value)


def _int(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"{what} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise Invalid(f"{what} must be finite")
    value = int(value)
    if not -(2**62) < value < 2**62:
        raise Invalid(f"{what} is out of range")
    return value


def _name(value, what="name"):
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{what} must be a non-empty string")
    if len(value) > MAX_NAME:
        raise Invalid(f"{what} is longer than {MAX_NAME} characters")
    return value


class Store:
    def __init__(self, data_dir, readers=4):
        self.dir = os.path.abspath(os.path.expanduser(data_dir))
        self.files_dir = os.path.join(self.dir, "files")
        os.makedirs(self.files_dir, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.path = os.path.join(self.dir, "mlop.db")

        self._lock = threading.RLock()
        self._w = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30
        )
        self._w.execute("PRAGMA journal_mode=WAL")
        self._w.execute("PRAGMA synchronous=NORMAL")
        self._w.execute("PRAGMA cache_size=-4096")  # 4 MiB
        self._w.execute("PRAGMA temp_store=MEMORY")
        self._w.executescript(SCHEMA)
        self._migrate()

        self._readers = queue.LifoQueue()
        for _ in range(readers):
            c = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                check_same_thread=False,
                isolation_level=None,
                timeout=30,
            )
            c.execute("PRAGMA cache_size=-2048")  # 2 MiB each
            c.execute("PRAGMA query_only=ON")
            c.row_factory = sqlite3.Row
            self._readers.put(c)

        self._series = {}  # (run_id, name) -> series id
        self._runs = set()  # run ids known to exist
        self._beats = {}  # run id -> last heartbeat written (ms)

    # -- plumbing ---------------------------------------------------------

    def _migrate(self):
        row = self._w.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer mlop server "
                f"(schema {version}, this build understands {SCHEMA_VERSION})"
            )
        if version < SCHEMA_VERSION:
            self._w.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _write(self):
        with self._lock:
            self._w.execute("BEGIN IMMEDIATE")
            try:
                yield self._w
            except BaseException:
                self._w.execute("ROLLBACK")
                self._series.clear()  # may hold ids from the undone transaction
                raise
            else:
                self._w.execute("COMMIT")

    @contextmanager
    def _read(self):
        c = self._readers.get()
        try:
            yield c
        finally:
            self._readers.put(c)

    def close(self):
        with self._lock:
            try:
                self._w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._w.close()
        while True:
            try:
                self._readers.get_nowait().close()
            except queue.Empty:
                break

    # -- api keys ---------------------------------------------------------

    def create_key(self, name):
        name = _name(name or "default", "key name")
        key = KEY_PREFIX + secrets.token_urlsafe(24)
        with self._write() as w:
            cur = w.execute(
                "INSERT INTO api_keys(name, hash, hint, created_at) VALUES(?,?,?,?)",
                (name, hash_key(key), key[:9] + "…" + key[-4:], now_ms()),
            )
        return {"id": cur.lastrowid, "name": name, "key": key}

    def check_key(self, key):
        if not key:
            return None
        with self._read() as r:
            row = r.execute(
                "SELECT id, name, last_used_at FROM api_keys WHERE hash=?",
                (hash_key(key),),
            ).fetchone()
        if row is None:
            return None
        if (row["last_used_at"] or 0) < now_ms() - 3_600_000:  # hourly is plenty
            with self._write() as w:
                w.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE id=?",
                    (now_ms(), row["id"]),
                )
        return {"id": row["id"], "name": row["name"]}

    def list_keys(self):
        with self._read() as r:
            return [
                dict(row)
                for row in r.execute(
                    "SELECT id, name, hint, created_at, last_used_at "
                    "FROM api_keys ORDER BY id"
                )
            ]

    def delete_key(self, key_id):
        with self._write() as w:
            if not w.execute("DELETE FROM api_keys WHERE id=?", (key_id,)).rowcount:
                raise NotFound("key not found")

    def count_keys(self):
        with self._read() as r:
            return r.execute("SELECT count(*) FROM api_keys").fetchone()[0]

    # -- runs: write side -------------------------------------------------

    def create_run(self, project, name, config=None, system=None, settings=None):
        project, name = _name(project, "projectName"), _name(name, "runName")
        t = now_ms()
        with self._write() as w:
            w.execute(
                "INSERT OR IGNORE INTO projects(name, created_at) VALUES(?,?)",
                (project, t),
            )
            pid = w.execute(
                "SELECT id FROM projects WHERE name=?", (project,)
            ).fetchone()[0]
            cur = w.execute(
                "INSERT INTO runs(project_id, name, config, system, settings, "
                "created_at, heartbeat_at) VALUES(?,?,?,?,?,?,?)",
                (
                    pid,
                    name,
                    _json_field(config),
                    _json_field(system),
                    _json_field(settings),
                    t,
                    t,
                ),
            )
        self._runs.add(cur.lastrowid)
        return cur.lastrowid

    def require_run(self, run_id):
        run_id = _int(run_id, "runId")
        if run_id in self._runs:
            return run_id
        with self._read() as r:
            found = r.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
        if not found:
            raise NotFound(f"run {run_id} not found")
        self._runs.add(run_id)
        return run_id

    def set_status(self, run_id, status, status_meta=None):
        run_id = self.require_run(run_id)
        if status not in STATUSES:
            raise Invalid(f"status must be one of {', '.join(STATUSES)}")
        t = now_ms()
        with self._write() as w:
            # A run cancelled from the UI stays cancelled, even though the
            # client reports its own exit status on the way out.
            w.execute(
                "UPDATE runs SET "
                "status=CASE WHEN status='CANCELLED' AND ?2!='RUNNING' "
                "  THEN status ELSE ?2 END, "
                "status_meta=coalesce(?3, status_meta), stalled=0, heartbeat_at=?4, "
                "finished_at=CASE WHEN ?2='RUNNING' THEN NULL ELSE ?4 END "
                "WHERE id=?1",
                (run_id, status, _json_field(status_meta), t),
            )

    def cancel_run(self, run_id):
        run_id = self.require_run(run_id)
        with self._write() as w:
            w.execute(
                "UPDATE runs SET status='CANCELLED', stalled=0, finished_at=? "
                "WHERE id=? AND status='RUNNING'",
                (now_ms(), run_id),
            )

    def heartbeat(self, run_id, force=False):
        """Record that a run is alive; returns its current status."""
        run_id = self.require_run(run_id)
        t = now_ms()
        if force or t - self._beats.get(run_id, 0) >= HEARTBEAT_WRITE_INTERVAL_MS:
            self._beats[run_id] = t
            with self._write() as w:
                w.execute(
                    "UPDATE runs SET heartbeat_at=?, "
                    "status=CASE WHEN stalled=1 THEN 'RUNNING' ELSE status END, "
                    "finished_at=CASE WHEN stalled=1 THEN NULL ELSE finished_at END, "
                    "stalled=0 WHERE id=?",
                    (t, run_id),
                )
        with self._read() as r:
            row = r.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        return row["status"] if row else None

    def mark_stalled(self, older_than_ms):
        """Flag running runs that stopped reporting as failed. Reversible:
        if the client comes back, heartbeat() puts the run back to RUNNING."""
        t = now_ms()
        with self._write() as w:
            return w.execute(
                "UPDATE runs SET status='FAILED', stalled=1, finished_at=heartbeat_at "
                "WHERE status='RUNNING' AND heartbeat_at < ?",
                (t - older_than_ms,),
            ).rowcount

    def set_graph(self, run_id, graph):
        run_id = self.require_run(run_id)
        with self._write() as w:
            w.execute(
                "UPDATE runs SET graph=? WHERE id=?", (json.dumps(graph), run_id)
            )

    def add_alert(self, run_id, alert):
        run_id = self.require_run(run_id)
        t = alert.get("timestamp")
        with self._write() as w:
            w.execute(
                "INSERT INTO alerts(run_id, time, level, title, body) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    _int(t, "timestamp") if t is not None else now_ms(),
                    str(alert.get("level") or "INFO")[:32],
                    str(alert.get("title") or "Alert")[:MAX_NAME],
                    str(alert.get("body") or "")[:MAX_MESSAGE],
                ),
            )

    # -- series -----------------------------------------------------------

    def _series_id(self, w, run_id, name, kind):
        """Must be called inside a write transaction."""
        sid = self._series.get((run_id, name))
        if sid is not None:
            return sid
        row = w.execute(
            "SELECT id FROM series WHERE run_id=? AND name=?", (run_id, name)
        ).fetchone()
        if row:
            sid = row[0]
        else:
            sid = w.execute(
                "INSERT INTO series(run_id, name, kind) VALUES(?,?,?)",
                (run_id, _name(name, "log name"), kind),
            ).lastrowid
        if len(self._series) > 200_000:
            self._series.clear()
        self._series[(run_id, name)] = sid
        return sid

    def declare_series(self, run_id, names, kind):
        """The client announces names before (or while) logging to them. For
        anything but plain metrics the declared kind is authoritative."""
        run_id = self.require_run(run_id)
        kind = _name(kind, "logType").upper()[:32]
        if not isinstance(names, list):
            raise Invalid("logName must be a list")
        with self._write() as w:
            for name in names:
                sid = self._series_id(w, run_id, _name(name, "logName"), kind)
                if kind != "METRIC":
                    w.execute(
                        "UPDATE series SET kind=? WHERE id=? AND kind!=?",
                        (kind, sid, kind),
                    )

    # -- ingest -----------------------------------------------------------

    def ingest_metrics(self, run_id, lines):
        """lines: iterable of {"time": ms, "step": n, "data": {name: value}}"""
        run_id = self.require_run(run_id)
        total, rows = 0, []

        def flush():
            with self._write() as w:
                w.executemany(
                    "INSERT OR REPLACE INTO metrics(series_id, step, time, value) "
                    "VALUES(?,?,?,?)",
                    [
                        (self._series_id(w, run_id, n, "METRIC"), s, t, v)
                        for n, s, t, v in rows
                    ],
                )
            rows.clear()

        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("data"), dict):
                raise Invalid('expected {"time", "step", "data": {...}} per line')
            t, s = _int(line.get("time"), "time"), _int(line.get("step"), "step")
            for name, value in line["data"].items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if not math.isfinite(value):
                    continue  # NaN/inf cannot be plotted; drop rather than fail
                rows.append((name, s, t, float(value)))
                total += 1
            if len(rows) >= INSERT_BATCH:
                flush()
        if rows:
            flush()
        self.heartbeat(run_id)
        return total

    def ingest_data(self, run_id, lines):
        """lines: {"time", "step", "logName", "dataType", "data": json-string}"""
        run_id = self.require_run(run_id)
        total, rows = 0, []

        def flush():
            with self._write() as w:
                w.executemany(
                    "INSERT INTO data(series_id, step, time, body) VALUES(?,?,?,?)",
                    [
                        (self._series_id(w, run_id, n, k), s, t, b)
                        for n, k, s, t, b in rows
                    ],
                )
            rows.clear()

        for line in lines:
            if not isinstance(line, dict):
                raise Invalid("expected one JSON object per line")
            body = line.get("data")
            if not isinstance(body, str):
                body = json.dumps(body)
            rows.append(
                (
                    _name(line.get("logName"), "logName"),
                    str(line.get("dataType") or "DATA").upper()[:32],
                    _int(line.get("step"), "step"),
                    _int(line.get("time"), "time"),
                    body,
                )
            )
            total += 1
            if len(rows) >= 500:
                flush()
        if rows:
            flush()
        self.heartbeat(run_id)
        return total

    def ingest_logs(self, run_id, lines):
        """lines: {"time", "message", "logType"}"""
        run_id = self.require_run(run_id)
        total, rows = 0, []

        def flush():
            with self._write() as w:
                w.executemany(
                    "INSERT INTO logs(run_id, time, level, message) VALUES(?,?,?,?)",
                    rows,
                )
            rows.clear()

        for line in lines:
            if not isinstance(line, dict):
                raise Invalid("expected one JSON object per line")
            rows.append(
                (
                    run_id,
                    _int(line.get("time"), "time"),
                    str(line.get("logType") or "INFO").upper()[:16],
                    str(line.get("message") or "")[:MAX_MESSAGE],
                )
            )
            total += 1
            if len(rows) >= INSERT_BATCH:
                flush()
        if rows:
            flush()
        return total

    # -- files ------------------------------------------------------------

    def register_files(self, run_id, files):
        """Record file metadata; returns [(logName, fileName, file id)]. The
        bytes follow in a separate upload per file."""
        run_id = self.require_run(run_id)
        if not isinstance(files, list):
            raise Invalid("files must be a list")
        out = []
        with self._write() as w:
            for f in files:
                if not isinstance(f, dict):
                    raise Invalid("each file must be an object")
                log_name = _name(f.get("logName"), "logName")
                file_name = _name(f.get("fileName"), "fileName")
                ext = str(f.get("fileType") or "").lower().lstrip(".")
                if not ext.isalnum() or len(ext) > 16:
                    ext = "bin"
                sid = self._series_id(
                    w, run_id, log_name, FILE_KINDS.get(ext, "ARTIFACT")
                )
                fid = w.execute(
                    "INSERT INTO files(run_id, series_id, step, time, name, ext, size) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        sid,
                        _int(f.get("step", 0), "step"),
                        _int(f.get("time", now_ms()), "time"),
                        file_name,
                        ext,
                        _int(f.get("fileSize", 0), "fileSize"),
                    ),
                ).lastrowid
                out.append((log_name, file_name, fid))
        self.heartbeat(run_id)
        return out

    def file_path(self, run_id, file_id, ext):
        # Built only from integers and a validated extension: no client
        # supplied text ever reaches the filesystem path.
        return os.path.join(self.files_dir, str(int(run_id)), f"{int(file_id)}.{ext}")

    def get_file(self, file_id):
        with self._read() as r:
            row = r.execute(
                "SELECT id, run_id, name, ext, size, uploaded FROM files WHERE id=?",
                (file_id,),
            ).fetchone()
        if row is None:
            raise NotFound("file not found")
        d = dict(row)
        d["path"] = self.file_path(d["run_id"], d["id"], d["ext"])
        return d

    def save_file(self, file_id, chunks):
        """Write an upload to disk atomically; returns the byte count."""
        f = self.get_file(file_id)
        os.makedirs(os.path.dirname(f["path"]), exist_ok=True)
        tmp, size = f["path"] + ".part", 0
        try:
            with open(tmp, "wb") as out:
                for chunk in chunks:
                    out.write(chunk)
                    size += len(chunk)
            os.replace(tmp, f["path"])
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        with self._write() as w:
            w.execute("UPDATE files SET uploaded=1, size=? WHERE id=?", (size, file_id))
        return size

    # -- reads ------------------------------------------------------------

    def list_projects(self):
        with self._read() as r:
            return [
                dict(row)
                for row in r.execute(
                    "SELECT p.id, p.name, p.created_at, count(r.id) AS runs, "
                    "coalesce(sum(r.status='RUNNING'), 0) AS running, "
                    "coalesce(max(r.heartbeat_at), p.created_at) AS last_activity "
                    "FROM projects p LEFT JOIN runs r ON r.project_id=p.id "
                    "GROUP BY p.id ORDER BY last_activity DESC"
                )
            ]

    def list_runs(self, project, limit=200, offset=0):
        with self._read() as r:
            p = r.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
            if p is None:
                raise NotFound(f"project {project!r} not found")
            rows = r.execute(
                "SELECT id, name, status, stalled, config, created_at, heartbeat_at, "
                "finished_at FROM runs WHERE project_id=? ORDER BY id DESC "
                "LIMIT ? OFFSET ?",
                (p["id"], limit, offset),
            ).fetchall()
            total = r.execute(
                "SELECT count(*) FROM runs WHERE project_id=?", (p["id"],)
            ).fetchone()[0]
            names = {}
            ids = [row["id"] for row in rows]
            for i in range(0, len(ids), 500):
                part = ids[i : i + 500]
                for s in r.execute(
                    "SELECT run_id, name FROM series WHERE kind='METRIC' "
                    f"AND run_id IN ({','.join('?' * len(part))})",
                    part,
                ):
                    names.setdefault(s["run_id"], []).append(s["name"])
        runs = []
        for row in rows:
            d = dict(row)
            d["config"] = _loads(d["config"])
            d["metrics"] = sorted(names.get(d["id"], []))
            runs.append(d)
        return {"project": project, "total": total, "runs": runs}

    def get_run(self, run_id):
        with self._read() as r:
            row = r.execute(
                "SELECT r.*, p.name AS project FROM runs r "
                "JOIN projects p ON p.id=r.project_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"run {run_id} not found")
            series = [
                dict(s)
                for s in r.execute(
                    "SELECT name, kind FROM series WHERE run_id=? ORDER BY name",
                    (run_id,),
                )
            ]
            alerts = [
                dict(a)
                for a in r.execute(
                    "SELECT time, level, title, body FROM alerts WHERE run_id=? "
                    "ORDER BY id DESC LIMIT 100",
                    (run_id,),
                )
            ]
            logs = r.execute(
                "SELECT count(*) FROM logs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        d = dict(row)
        for k in ("config", "system", "settings", "status_meta"):
            d[k] = _loads(d[k])
        d["has_graph"] = d.pop("graph") is not None
        d.pop("project_id")
        d.update(series=series, alerts=alerts, log_lines=logs)
        return d

    def get_graph(self, run_id):
        with self._read() as r:
            row = r.execute("SELECT graph FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"run {run_id} not found")
        return _loads(row["graph"])

    def _find_series(self, r, run_id, names):
        found = {}
        for i in range(0, len(names), 500):
            part = names[i : i + 500]
            for row in r.execute(
                "SELECT id, name, kind FROM series WHERE run_id=? "
                f"AND name IN ({','.join('?' * len(part))})",
                [run_id] + part,
            ):
                found[row["name"]] = (row["id"], row["kind"])
        return found

    def read_metrics(self, run_id, names, points=500, axis="step"):
        """Return each series reduced to at most ~`points` buckets, with the
        mean, min and max of every bucket, so a chart of a million-step run
        costs the browser the same as a chart of a thousand-step one."""
        if axis not in ("step", "time"):
            raise Invalid("x must be 'step' or 'time'")
        points = max(10, min(int(points), 5000))
        out = {}
        with self._read() as r:
            for name, (sid, _) in self._find_series(r, run_id, names).items():
                n, lo, hi = r.execute(
                    f"SELECT count(*), min({axis}), max({axis}) FROM metrics "
                    "WHERE series_id=?",
                    (sid,),
                ).fetchone()
                if not n:
                    out[name] = {"x": [], "y": [], "count": 0}
                    continue
                if n <= points:
                    rows = r.execute(
                        f"SELECT {axis}, value FROM metrics WHERE series_id=? "
                        f"ORDER BY {axis}",
                        (sid,),
                    ).fetchall()
                    out[name] = {
                        "x": [row[0] for row in rows],
                        "y": [row[1] for row in rows],
                        "count": n,
                    }
                    continue
                span = hi - lo + 1
                rows = r.execute(
                    f"SELECT max({axis}), avg(value), min(value), max(value) "
                    f"FROM metrics WHERE series_id=?1 "
                    f"GROUP BY ({axis} - ?2) * ?3 / ?4 ORDER BY 1",
                    (sid, lo, points, span),
                ).fetchall()
                out[name] = {
                    "x": [row[0] for row in rows],
                    "y": [row[1] for row in rows],
                    "min": [row[2] for row in rows],
                    "max": [row[3] for row in rows],
                    "count": n,
                }
        return out

    def read_data(self, run_id, name, limit=64):
        """Entries of a structured series, evenly thinned to `limit` steps."""
        limit = max(1, min(int(limit), 500))
        with self._read() as r:
            found = self._find_series(r, run_id, [name])
            if name not in found:
                raise NotFound(f"series {name!r} not found")
            sid, kind = found[name]
            ids = [
                row[0]
                for row in r.execute(
                    "SELECT id FROM data WHERE series_id=? ORDER BY step, id", (sid,)
                )
            ]
            total = len(ids)
            if total > limit:
                stride = (total - 1) / (limit - 1) if limit > 1 else total
                ids = [ids[round(i * stride)] for i in range(limit)]
            rows = []
            for i in range(0, len(ids), 500):
                part = ids[i : i + 500]
                rows += r.execute(
                    "SELECT step, time, body FROM data "
                    f"WHERE id IN ({','.join('?' * len(part))}) ORDER BY step, id",
                    part,
                ).fetchall()
        return {
            "name": name,
            "kind": kind,
            "total": total,
            "entries": [
                {"step": row["step"], "time": row["time"], "data": _loads(row["body"])}
                for row in rows
            ],
        }

    def read_files(self, run_id, name, limit=500):
        limit = max(1, min(int(limit), 2000))
        with self._read() as r:
            found = self._find_series(r, run_id, [name])
            if name not in found:
                raise NotFound(f"series {name!r} not found")
            sid, kind = found[name]
            rows = r.execute(
                "SELECT id, step, time, name, ext, size FROM files "
                "WHERE series_id=? AND uploaded=1 ORDER BY step DESC, id DESC LIMIT ?",
                (sid, limit),
            ).fetchall()
        return {"name": name, "kind": kind, "files": [dict(row) for row in rows][::-1]}

    def read_logs(self, run_id, after=0, limit=1000, tail=False):
        limit = max(1, min(int(limit), 5000))
        with self._read() as r:
            if tail:
                rows = r.execute(
                    "SELECT id, time, level, message FROM logs WHERE run_id=? "
                    "ORDER BY id DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()[::-1]
            else:
                rows = r.execute(
                    "SELECT id, time, level, message FROM logs WHERE run_id=? "
                    "AND id>? ORDER BY id LIMIT ?",
                    (run_id, after, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    # -- deletion ---------------------------------------------------------

    def _delete_runs(self, w, run_ids):
        for run_id in run_ids:
            w.execute(
                "DELETE FROM metrics WHERE series_id IN "
                "(SELECT id FROM series WHERE run_id=?)",
                (run_id,),
            )
            w.execute(
                "DELETE FROM data WHERE series_id IN "
                "(SELECT id FROM series WHERE run_id=?)",
                (run_id,),
            )
            for table in ("files", "logs", "alerts", "series"):
                w.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            w.execute("DELETE FROM runs WHERE id=?", (run_id,))
        self._series.clear()
        self._runs.difference_update(run_ids)
        for run_id in run_ids:
            self._beats.pop(run_id, None)

    def _remove_run_files(self, run_ids):
        for run_id in run_ids:
            shutil.rmtree(
                os.path.join(self.files_dir, str(int(run_id))), ignore_errors=True
            )

    def delete_run(self, run_id):
        run_id = self.require_run(run_id)
        with self._write() as w:
            self._delete_runs(w, [run_id])
        self._remove_run_files([run_id])

    def delete_project(self, project):
        with self._write() as w:
            p = w.execute("SELECT id FROM projects WHERE name=?", (project,)).fetchone()
            if p is None:
                raise NotFound(f"project {project!r} not found")
            run_ids = [
                row[0]
                for row in w.execute("SELECT id FROM runs WHERE project_id=?", p)
            ]
            self._delete_runs(w, run_ids)
            w.execute("DELETE FROM projects WHERE id=?", p)
        self._remove_run_files(run_ids)
        return len(run_ids)

    def stats(self):
        with self._read() as r:
            runs = r.execute("SELECT count(*) FROM runs").fetchone()[0]
            projects = r.execute("SELECT count(*) FROM projects").fetchone()[0]
        db = sum(
            os.path.getsize(self.path + s)
            for s in ("", "-wal")
            if os.path.exists(self.path + s)
        )
        files = 0
        for root, _, names in os.walk(self.files_dir):
            for n in names:
                try:
                    files += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
        return {
            "projects": projects,
            "runs": runs,
            "db_bytes": db,
            "file_bytes": files,
        }
