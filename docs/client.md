# Using the logger

```bash
pip install git+https://github.com/yych42/mini-mlop.git
```

The base install needs only `httpx` and `psutil`. Extras:

| Extra | Adds | Needed for |
|---|---|---|
| `media` | numpy, pillow, soundfile | `mlop.Image` / `mlop.Audio` from arrays |
| `gpu` | nvidia-ml-py | NVIDIA utilisation, memory and power metrics |
| `full` | all of the above | |

```bash
pip install "mini-mlop[full] @ git+https://github.com/yych42/mini-mlop.git"
```

## Pointing at your server

In order of precedence:

1. `mlop.init(settings={"url": "http://pi.local:2718"})`
2. `mlop.init(settings={"host": "pi.local"})` — shorthand for port 2718
3. the `MINI_MLOP_URL` environment variable
4. `http://localhost:2718`

The API key comes from `settings={"_auth": ...}`, then `MINI_MLOP_API_KEY`, then whatever `mini-mlop login`
saved for that server in `~/.config/mini-mlop/credentials.json` (mode 0600, one key per server
address). In a terminal or notebook you are prompted when none of those works; in a batch job you
get a `PermissionError` that says what to set. An unreachable server raises `ConnectionError`
from `mlop.init()` rather than failing silently later.

```bash
mini-mlop login --url http://pi.local:2718
```

## Logging

```python
import mini_mlop as mlop

run = mlop.init(project="mnist", name="baseline", config={"lr": 1e-3, "epochs": 10})

run.log({"train/loss": 0.42, "train/acc": 0.91})          # scalars; step auto-increments
run.log({"val/loss": 0.5}, step=100)                      # or set it yourself
run.log({"weights": mlop.Histogram(array, bins=64)})
run.log({"confusion": mlop.Table(data=[[5, 1], [0, 6]], columns=["cat", "dog"])})
run.log({"samples": mlop.Image(array_or_pil_or_path, caption="epoch 3")})
run.log({"speech": mlop.Audio(waveform, rate=16000)})
run.log({"note": mlop.Text("learning rate halved")})
run.log({"checkpoint": mlop.Artifact("model.pt")})

run.alert(title="Diverged", message="loss is nan", level="ERROR")
run.finish()
```

Names containing `/` are grouped by their prefix in the UI. Everything printed to stdout and
stderr while a run is active is captured and shown under **Logs**. `mlop.log(...)`,
`mlop.alert(...)` and `mlop.finish()` act on the most recent run.

Pressing **Stop run** in the UI raises `KeyboardInterrupt` in your script's main thread within a
few seconds, so `try/finally` cleanup runs and buffered data is still uploaded.

`run.alert(..., url="https://hooks.slack.com/…", remote=False)` posts to a Slack- or
Discord-style webhook directly from the client instead of storing the alert on the server.

## Integrations

```python
run.watch(model)                                   # PyTorch: gradient and parameter histograms
```

```python
from mini_mlop.compat.lightning import MLOPLogger       # Lightning
trainer = Trainer(logger=MLOPLogger(project="mnist"))
```

```python
from mini_mlop.compat.transformers import MLOPCallback  # Hugging Face Trainer
trainer = Trainer(..., callbacks=[MLOPCallback()])
```

The notebooks in [`examples/`](../examples) walk through each.

## Settings

Pass a dict as `settings=`. The ones most worth knowing:

| Setting | Default | |
|---|---|---|
| `url` / `host` | see above | Server address |
| `mode` | `"perf"` | `"noop"` disables everything (no server needed); `"debug"` is verbose |
| `disable_console` | `False` | Do not capture stdout/stderr |
| `disable_store` | `True` | Set `False` to also keep a local SQLite copy next to your script |
| `x_sys_sampling_interval` | `4` | Seconds between system-metric samples (and heartbeats) |
| `x_log_level` | `16` | Python logging level for the logger's own messages |
| `http_proxy`, `https_proxy`, `insecure_disable_ssl` | | Network plumbing |

Each run also writes a small directory under `./.mini-mlop/<project>/<run>/` (staged files and local
logs); it is safe to delete after the run finishes.
