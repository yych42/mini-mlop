"""The open-ended read interface: POST /api/query."""

import pytest

from mini_mlop_server import query as query_module


def metric_lines(n, name="loss", scale=1.0):
    return [
        {"time": 1_700_000_000_000 + i, "step": i, "data": {name: i * scale}}
        for i in range(n)
    ]


@pytest.fixture
def two_runs(api):
    a = api.new_run(project="mnist", name="base", config={"lr": 0.1})
    b = api.new_run(project="mnist", name="tuned", config={"lr": 0.01})
    api.ingest("metrics", a, metric_lines(100))
    api.ingest("metrics", b, metric_lines(100, scale=2.0))
    api.ingest("logs", a, [{"time": 1, "message": "Traceback: boom", "lineNumber": 1, "logType": "ERROR"}])
    return a, b


def q(api, sql, **extra):
    return api.post("/api/query", {"sql": sql, **extra})


def test_query_needs_a_key(server):
    from conftest import Api

    status, _ = Api(server.url).post("/api/query", {"sql": "SELECT 1"})
    assert status == 401


def test_schema_describes_every_table_truthfully(api, two_runs):
    status, schema = api.get("/api/query/schema")
    assert status == 200 and schema["examples"] and schema["limits"]["max_rows"]
    for table in schema["tables"]:
        status, body = q(api, f"SELECT * FROM {table['name']} LIMIT 1")
        assert status == 200, body
        assert body["columns"] == [c[0] for c in table["columns"]], table["name"]
    assert api.get("/api/query")[1]["tables"] == schema["tables"]


def test_examples_all_run(api, two_runs):
    for example in query_module.EXAMPLES:
        status, body = q(api, example["sql"])
        assert status == 200, (example["sql"], body)


def test_runs_join_metrics_by_name(api, two_runs):
    a, b = two_runs
    status, body = q(
        api,
        "SELECT r.name, json_extract(r.config, '$.lr') AS lr, max(m.value) AS top "
        "FROM runs r JOIN metrics m ON m.run_id = r.id "
        "WHERE r.project = :p AND m.name = 'loss' GROUP BY r.id ORDER BY r.id",
        params={"p": "mnist"},
    )
    assert status == 200, body
    assert body["columns"] == ["name", "lr", "top"]
    assert body["rows"] == [["base", 0.1, 99.0], ["tuned", 0.01, 198.0]]
    assert body["truncated"] is False


def test_window_functions_and_positional_params(api, two_runs):
    a, _ = two_runs
    status, body = q(
        api,
        "SELECT step, value - lag(value) OVER (ORDER BY step) AS delta "
        "FROM metrics WHERE run_id = ? AND name = ? ORDER BY step LIMIT 3",
        params=[a, "loss"],
    )
    assert status == 200, body
    assert body["rows"] == [[0, None], [1, 1.0], [2, 1.0]]


def test_row_limit_truncates(api, two_runs):
    status, body = q(api, "SELECT step FROM metrics", limit=7)
    assert status == 200 and body["row_count"] == 7 and body["truncated"] is True


def test_get_and_csv(api, two_runs):
    status, data, headers = api.get(
        "/api/query?sql=SELECT%20name%2C%20status%20FROM%20runs%20ORDER%20BY%20id&format=csv",
        raw=True,
    )
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    assert data.decode() == "name,status\nbase,RUNNING\ntuned,RUNNING\n"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM runs",
        "UPDATE runs SET name = 'x'",
        "INSERT INTO projects(name, created_at) VALUES('x', 1)",
        "DROP TABLE metrics",
        "CREATE TABLE t(x)",
        "CREATE TEMP TABLE t(x)",
        "DROP VIEW runs",
        "PRAGMA query_only=OFF",
        "PRAGMA table_info(runs)",
        "ATTACH DATABASE '/tmp/other.db' AS other",
        "VACUUM",
        "SELECT * FROM api_keys",
        "SELECT hash FROM main.api_keys",
        "SELECT (SELECT group_concat(hash) FROM api_keys) FROM runs",
        "SELECT load_extension('x')",
        "SELECT zeroblob(1000000000)",
        "SELECT 1; SELECT 2",
        "",
    ],
)
def test_anything_but_reading_is_refused(api, server, two_runs, sql):
    status, body = q(api, sql)
    assert status == 400, body
    assert "mlop_" not in str(body) and "hash" not in str(body.get("error"))
    # ... and nothing changed
    assert server.store.count_keys() == 1
    assert q(api, "SELECT count(*) FROM runs")[1]["rows"] == [[2]]


def test_runaway_query_is_stopped(api, server):
    server.store.sql.timeout = 0.3
    status, body = q(
        api,
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
        "SELECT count(*) FROM n",
    )
    assert status == 400 and "stopped after" in body["error"]
    # the engine is usable afterwards
    assert q(api, "SELECT 1 AS one")[1]["rows"] == [[1]]


def test_bad_sql_reports_the_sqlite_message(api):
    status, body = q(api, "SELECT nope FROM runs")
    assert status == 400 and "no such column" in body["error"]


def test_queries_see_fresh_writes(api):
    assert q(api, "SELECT count(*) FROM runs")[1]["rows"] == [[0]]
    api.new_run()
    assert q(api, "SELECT count(*) FROM runs")[1]["rows"] == [[1]]


# -- project scope (used by the built-in assistant) ------------------------------


@pytest.fixture
def two_projects(api, server):
    a = api.new_run(project="alpha", name="a1", config={"secret": "alpha-only"})
    b = api.new_run(project="beta", name="b1", config={"secret": "beta-only"})
    for run, scale in ((a, 1.0), (b, 5.0)):
        api.ingest("metrics", run, metric_lines(10, scale=scale))
        api.ingest("logs", run, [{"time": 1, "message": f"log of run {run}", "lineNumber": 1, "logType": "INFO"}])
    return server.store, a, b


def test_scoped_queries_see_one_project(two_projects):
    store, a, b = two_projects
    rows = lambda sql, project: store.query(sql, project=project)["rows"]  # noqa: E731
    assert rows("SELECT id, project, name FROM runs", "alpha") == [[a, "alpha", "a1"]]
    assert rows("SELECT id FROM runs", "beta") == [[b]]
    assert rows("SELECT name FROM projects", "alpha") == [["alpha"]]
    assert rows("SELECT DISTINCT run_id FROM metrics", "alpha") == [[a]]
    assert rows("SELECT max(value) FROM metrics WHERE name = 'loss'", "beta") == [[45.0]]
    assert rows(f"SELECT count(*) FROM metrics WHERE run_id = {b}", "alpha") == [[0]]
    assert rows("SELECT DISTINCT run_id FROM series", "alpha") == [[a]]
    assert rows("SELECT message FROM logs", "alpha") == [[f"log of run {a}"]]
    assert rows("SELECT count(*) FROM runs", "no-such-project") == [[0]]
    # unscoped queries still see everything
    assert store.query("SELECT count(*) FROM runs")["rows"] == [[2]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM main.runs",
        "SELECT config FROM main.runs WHERE id = 2",
        "SELECT * FROM main.metrics",
        "SELECT * FROM main.series",
        "SELECT * FROM main.logs",
        "SELECT * FROM main.projects",
        "SELECT (SELECT group_concat(config) FROM main.runs) FROM runs",
        "WITH x AS (SELECT * FROM main.runs) SELECT * FROM x",
        "WITH runs AS (SELECT * FROM main.runs) SELECT * FROM runs",
        "WITH metrics AS (SELECT * FROM main.metrics) SELECT * FROM metrics",
        "SELECT * FROM (SELECT * FROM main.runs) AS runs",
        "SELECT * FROM runs WHERE id IN (SELECT id FROM main.runs AS runs)",
        'SELECT * FROM "main"."runs"',
        "SELECT * FROM main/**/.runs",
        "SELECT sql FROM sqlite_temp_master",
        "SELECT sql FROM temp.sqlite_schema",
        "SELECT * FROM pragma_table_info('runs')",
        "SELECT * FROM pragma_database_list",
        "EXPLAIN SELECT * FROM runs",
        "/* x */ EXPLAIN QUERY PLAN SELECT * FROM runs",
        "SELECT sql FROM main.sqlite_master",
        "SELECT * FROM api_keys",
        "SELECT * FROM settings",
        "DELETE FROM runs",
        "DROP VIEW runs",
    ],
)
def test_scope_cannot_be_escaped(two_projects, sql):
    store, _, _ = two_projects
    with pytest.raises(query_module.QueryError):
        store.query(sql, project="alpha")
    # nor by first preparing the statement without a scope
    try:
        store.query(sql)
    except query_module.QueryError:
        pass
    with pytest.raises(query_module.QueryError):
        store.query(sql, project="alpha")


def test_scoped_schema_matches_the_documented_tables(two_projects):
    store, _, _ = two_projects
    for table in query_module.TABLES:
        result = store.query(f"SELECT * FROM {table['name']} LIMIT 1", project="alpha")
        assert result["columns"] == [c[0] for c in table["columns"]], table["name"]
    for example in query_module.EXAMPLES:
        store.query(example["sql"], project="alpha")


def test_no_query_shape_leaks_another_project(two_projects):
    """Whatever a scoped query does, raise or return rows, the other
    project's data must not appear in what comes back."""
    store, a, b = two_projects
    attempts = [
        "SELECT * FROM runs",
        "SELECT * FROM runs UNION ALL SELECT * FROM runs",
        "SELECT r.*, m.* FROM runs r LEFT JOIN metrics m ON 1 = 1",
        f"SELECT * FROM metrics WHERE run_id = {b}",
        f"SELECT * FROM logs WHERE run_id = {b} OR 1 = 1",
        "SELECT * FROM series, data, files, alerts, projects",
        "SELECT (SELECT config FROM runs ORDER BY id DESC LIMIT 1)",
        "SELECT config FROM runs AS main",
        "WITH RECURSIVE runs(config) AS (SELECT config FROM main.runs) SELECT * FROM runs",
        "SELECT * FROM temp.runs, main.runs",
        "SELECT mlop_project()",
    ]
    for sql in attempts:
        try:
            out = str(store.query(sql, project="alpha"))
        except query_module.QueryError as e:
            out = str(e)
        assert "beta" not in out and "b1" not in out and f"run {b}" not in out, sql
