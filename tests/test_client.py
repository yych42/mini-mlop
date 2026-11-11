"""End to end: the real logging client against a real server."""

import time

import pytest

import mlop
from mlop import auth

from conftest import Api

FAST = {
    "x_sys_sampling_interval": 1,
    "x_file_stream_retry_max": 2,
    "disable_console": True,
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MLOP_URL", raising=False)
    monkeypatch.delenv("MLOP_API_KEY", raising=False)


def start(server, tmp_path, **kw):
    settings = {"url": server.url, "_auth": getattr(server, "key", None), **FAST}
    return mlop.init(
        dir=str(tmp_path / "client"), project="e2e", settings=settings, **kw
    )


def test_metrics_round_trip(server, api, tmp_path):
    run = start(server, tmp_path, name="metrics", config={"lr": 0.01})
    for i in range(200):
        run.log({"train/loss": 1 / (i + 1), "train/acc": i / 200})
    run.finish()

    run_id = run.settings._op_id
    assert run.settings.url_view == f"{server.url}/#/runs/{run_id}"

    _, info = api.get(f"/api/runs/{run_id}")
    assert info["name"] == "metrics" and info["project"] == "e2e"
    assert info["status"] == "COMPLETED"
    assert info["config"] == {"lr": 0.01}
    assert info["system"]["platform"]["system"]
    kinds = {s["name"]: s["kind"] for s in info["series"]}
    assert kinds["train/loss"] == "METRIC"
    assert any(n.startswith("sys/") for n in kinds), "system metrics are logged"

    _, m = api.get(f"/api/runs/{run_id}/metrics?name=train/loss&name=train/acc")
    assert m["train/loss"]["count"] == 200
    assert m["train/loss"]["x"] == list(range(1, 201))
    assert m["train/loss"]["y"][0] == 1.0
    assert m["train/acc"]["y"][-1] == pytest.approx(199 / 200)


def test_structured_data_and_files(server, api, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")

    run = start(server, tmp_path, name="media")
    rng = np.random.default_rng(0)
    for step in range(3):
        run.log(
            {
                "dist/weights": mlop.Histogram(rng.normal(size=500), bins=16),
                "eval/table": mlop.Table(
                    data=[[1, 2.5], [3, 4.5]], columns=["a", "b"]
                ),
                "samples/image": mlop.Image(
                    rng.integers(0, 255, size=(8, 8, 3)).astype("uint8"),
                    caption="noise",
                ),
                "notes": mlop.Text(f"step {step}"),
            }
        )
    run.finish()
    run_id = run.settings._op_id

    _, info = api.get(f"/api/runs/{run_id}")
    kinds = {s["name"]: s["kind"] for s in info["series"]}
    assert kinds["dist/weights"] == "HISTOGRAM"
    assert kinds["eval/table"] == "TABLE"
    assert kinds["samples/image"] == "IMAGE"
    assert kinds["notes"] == "TEXT"

    _, h = api.get(f"/api/runs/{run_id}/data?name=dist/weights")
    assert h["total"] == 3
    assert sum(h["entries"][0]["data"]["freq"]) == 500
    assert h["entries"][0]["data"]["bins"]["num"] == 16

    _, t = api.get(f"/api/runs/{run_id}/data?name=eval/table")
    assert t["entries"][0]["data"]["table"] == [[1, 2.5], [3, 4.5]]

    _, imgs = api.get(f"/api/runs/{run_id}/files?name=samples/image")
    assert [f["step"] for f in imgs["files"]] == [1, 2, 3]
    status, png, headers = api.get(f"/api/files/{imgs['files'][0]['id']}", raw=True)
    assert status == 200 and png.startswith(b"\x89PNG")
    assert headers["Content-Type"] == "image/png"

    _, notes = api.get(f"/api/runs/{run_id}/files?name=notes")
    _, text, _ = api.get(f"/api/files/{notes['files'][-1]['id']}", raw=True)
    assert text == b"step 2"


def test_histogram_without_numpy_matches_numpy():
    np = pytest.importorskip("numpy")
    from mlop import data as mdata

    values = [0.1, 0.2, 0.2, 0.9, 1.5, 3.0, 3.0, 2.2]
    want = mdata._histogram(values, 4)
    import builtins

    real_import = builtins.__import__

    def no_numpy(name, *a, **k):
        if name == "numpy":
            raise ImportError(name)
        return real_import(name, *a, **k)

    builtins.__import__ = no_numpy
    try:
        got = mdata._histogram(values, 4)
    finally:
        builtins.__import__ = real_import
    assert got[0] == want[0]
    assert got[1] == pytest.approx(want[1])


def test_console_output_is_captured(server, api, tmp_path, capsys):
    settings = {"url": server.url, "_auth": server.key, **FAST, "disable_console": False}
    run = mlop.init(dir=str(tmp_path / "client"), project="e2e", settings=settings)
    print("hello from the training script")
    run.finish()
    capsys.readouterr()

    _, logs = api.get(f"/api/runs/{run.settings._op_id}/logs")
    assert "hello from the training script" in [l["message"] for l in logs["lines"]]


def test_alert_is_stored(server, api, tmp_path):
    run = start(server, tmp_path)
    run.alert(title="diverged", message="loss is nan", level="ERROR")
    run.finish()
    _, info = api.get(f"/api/runs/{run.settings._op_id}")
    assert info["alerts"][0]["title"] == "diverged"
    assert info["alerts"][0]["level"] == "ERROR"


def test_stop_from_server_interrupts_the_script(server, api, tmp_path):
    run = start(server, tmp_path, name="to-cancel")
    run_id = run.settings._op_id
    api.post(f"/api/runs/{run_id}/stop")
    with pytest.raises(KeyboardInterrupt):
        for i in range(600):  # the monitor checks in once a second here
            run.log({"loss": i})
            time.sleep(0.05)
    run.finish()
    _, info = api.get(f"/api/runs/{run_id}")
    assert info["status"] == "CANCELLED"
    _, m = api.get(f"/api/runs/{run_id}/metrics?name=loss")
    assert m["loss"]["count"] >= 1, "data logged before the stop is kept"


def test_wrong_key_is_a_clear_error(server, tmp_path):
    with pytest.raises(PermissionError, match="did not accept the API key"):
        mlop.init(
            dir=str(tmp_path), settings={"url": server.url, "_auth": "mlop_wrong"}
        )


def test_unreachable_server_is_a_clear_error(tmp_path):
    with pytest.raises(ConnectionError, match="cannot reach the mlop server"):
        mlop.init(
            dir=str(tmp_path),
            settings={"url": "http://127.0.0.1:9", "x_file_stream_timeout_seconds": 2},
        )


def test_login_saves_key_for_later_runs(server, tmp_path):
    assert auth.login(settings={"url": server.url}, key=server.key)
    assert auth._load() == {server.url: server.key}

    run = mlop.init(
        dir=str(tmp_path / "client"), project="e2e", settings={"url": server.url, **FAST}
    )
    run.log({"x": 1})
    run.finish()

    auth.logout(settings={"url": server.url})
    assert auth._load() == {}


def test_no_auth_server_needs_no_key(open_server, tmp_path):
    run = start(open_server, tmp_path)
    run.log({"x": 1.5})
    run.finish()
    _, m = Api(open_server.url).get(f"/api/runs/{run.settings._op_id}/metrics?name=x")
    assert m["x"]["y"] == [1.5]


def test_settings_resolve_server_address(monkeypatch):
    from mlop.sets import Settings, setup

    assert Settings().url_base() == "http://localhost:2718"
    assert setup({"host": "pi.local"}).url_base() == "http://pi.local:2718"
    assert setup({"host": "pi.local:9000"}).url_base() == "http://pi.local:9000"
    assert setup({"url": "https://mlop.example.com/"}).url_num == (
        "https://mlop.example.com/ingest/metrics"
    )
    monkeypatch.setenv("MLOP_URL", "http://tracker:1234")
    assert Settings().url_base() == "http://tracker:1234"
