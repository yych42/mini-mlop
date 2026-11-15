"""End to end: the real logging client against a real server."""

import time

import pytest

import mini_mlop as mlop
from mini_mlop import auth

from conftest import Api

FAST = {
    "x_sys_sampling_interval": 1,
    "x_file_stream_retry_max": 2,
    "disable_console": True,
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("MINI_MLOP_URL", raising=False)
    monkeypatch.delenv("MINI_MLOP_API_KEY", raising=False)


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
    assert run.settings.url_view == f"{server.url}/runs/{run_id}"

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
    from mini_mlop import data as mdata

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


def test_logging_configured_after_init_does_not_loop(
    server, api, tmp_path, capsys, monkeypatch
):
    """A handler made after init holds the capturing stderr as its stream. The
    captured line must not come back to that handler through the root logger,
    and what sending a line logs must not be captured as a line to send."""
    import logging
    import sys

    settings = {"url": server.url, "_auth": server.key, **FAST, "disable_console": False}
    run = mlop.init(dir=str(tmp_path / "client"), project="e2e", settings=settings)
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    root.addHandler(handler)
    monkeypatch.setattr(root, "level", logging.INFO)  # as basicConfig(level=INFO)
    try:
        logging.getLogger("host.app").warning("first log line")
        print("print after logging")
        time.sleep(2.5)  # a few rounds of sending, each logged by httpx at INFO
    finally:
        run.finish()
        handler.stream.write("written after the run\n")  # passes through, unrecorded
        root.removeHandler(handler)
    assert capsys.readouterr().err.count("first log line") == 1

    _, logs = api.get(f"/api/runs/{run.settings._op_id}/logs")
    messages = [l["message"] for l in logs["lines"]]
    assert messages.count("first log line") == 1
    assert "print after logging" in messages
    assert "written after the run" not in messages
    assert not [m for m in messages if "HTTP Request" in m or "mini_mlop" in m]


def test_finish_restores_only_its_own_streams(server, tmp_path):
    import sys

    settings = {"url": server.url, "_auth": server.key, **FAST, "disable_console": False}
    before = sys.stdout
    run = mlop.init(dir=str(tmp_path / "client"), project="e2e", settings=settings)
    assert sys.stdout is not before
    run.finish()
    assert sys.stdout is before


def test_gpu_metrics_survive_an_unsupported_query(monkeypatch):
    """Unified-memory devices (NVIDIA GB10) answer NotSupported for memory."""
    pynvml = pytest.importorskip("pynvml")
    from types import SimpleNamespace as NS

    from mini_mlop import sys as mlop_sys

    def unsupported(*_):
        raise pynvml.NVMLError(pynvml.NVML_ERROR_NOT_SUPPORTED)

    fake = {
        "nvmlInit": lambda: None,
        "nvmlDeviceGetCount": lambda: 1,
        "nvmlSystemGetDriverVersion": lambda: "1.0",
        "nvmlDeviceGetHandleByIndex": lambda i: i,
        "nvmlDeviceGetName": lambda h: "NVIDIA GB10",
        "nvmlDeviceGetMemoryInfo": unsupported,
        "nvmlDeviceGetTemperature": lambda h, sensor: 30,
        "nvmlDeviceGetUtilizationRates": lambda h: NS(gpu=7, memory=0),
        "nvmlDeviceGetPowerUsage": lambda h: 4000,
        "nvmlDeviceGetEnforcedPowerLimit": unsupported,
        "nvmlDeviceGetComputeRunningProcesses": lambda h: [NS(pid=1, usedGpuMemory=100)],
        "nvmlDeviceGetGraphicsRunningProcesses": lambda h: [NS(pid=2, usedGpuMemory=None)],
    }
    for name, fn in fake.items():
        fn.__name__ = name
        monkeypatch.setattr(pynvml, name, fn)
    monkeypatch.setattr(mlop_sys, "run_cmd", lambda *a, **k: "smi output")

    system = object.__new__(mlop_sys.System)
    system.settings = mlop.Settings()
    system.gpu = system.get_gpu()
    assert system.gpu["nvidia"]["devices"] == [{"name": "NVIDIA GB10", "temp": 30, "pid": [1, 2]}]
    assert system.monitor_gpu() == {
        "sys/gpu.nvda.nvidia_gb10.pct": 7,
        "sys/gpu.nvda.nvidia_gb10.mem.pct": 0,
        "sys/gpu.nvda.nvidia_gb10.mem.used": 100,
        "sys/gpu.nvda.nvidia_gb10.power": 4000,
        "sys/gpu.nvda.nvidia_gb10.temp": 30,
    }


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
            dir=str(tmp_path), settings={"url": server.url, "_auth": "mini_mlop_wrong"}
        )


def test_unreachable_server_is_a_clear_error(tmp_path):
    with pytest.raises(ConnectionError, match="cannot reach the mini-mlop server"):
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
    from mini_mlop.sets import Settings, setup

    assert Settings().url_base() == "http://localhost:2718"
    assert setup({"host": "pi.local"}).url_base() == "http://pi.local:2718"
    assert setup({"host": "pi.local:9000"}).url_base() == "http://pi.local:9000"
    assert setup({"url": "https://mlop.example.com/"}).url_num == (
        "https://mlop.example.com/ingest/metrics"
    )
    monkeypatch.setenv("MINI_MLOP_URL", "http://tracker:1234")
    assert Settings().url_base() == "http://tracker:1234"
    assert setup({"host": "pi.local"}).url_base() == "http://pi.local:2718"  # explicit wins


def test_version_mismatch_with_server_is_warned(server, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(mlop, "__version__", "9.9.0")
    with caplog.at_level("WARNING"):
        ok, _ = auth._check(mlop.sets.setup({"url": server.url}), server.key)
    assert ok
    assert "client is version 9.9.0 but the server is" in caplog.text


def test_query_command(server, tmp_path):
    import os
    import subprocess
    import sys

    run = mlop.init(
        dir=str(tmp_path), project="cli", name="one",
        settings={"url": server.url, "_auth": server.key},
    )
    run.log({"loss": 0.5})
    run.finish()

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "mini_mlop", "query", "--url", server.url, *args],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={**os.environ, "MINI_MLOP_API_KEY": server.key},
        )

    assert "metrics" in cli().stdout and "Examples:" in cli("--schema").stdout
    assert cli("SELECT project, name FROM runs", "--format", "csv").stdout == "project,name\ncli,one\n"
    assert "cli      one" in cli("SELECT project, name FROM runs").stdout
    refused = cli("DELETE FROM runs")
    assert refused.returncode != 0 and refused.stderr.startswith("mini-mlop: ")
