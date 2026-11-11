"""Log a few synthetic training runs so there is something to look at.

    mini-mlop-server                      # in one terminal
    python examples/demo.py          # in another (set MINI_MLOP_API_KEY or `mini-mlop login` first)

Needs numpy and pillow for the histogram/image parts: pip install "mini-mlop[media]"
"""

import math
import random
import sys

import mini_mlop as mlop

try:
    import numpy as np
except ImportError:
    np = None


def train(name, lr, layers, steps=400, seed=0):
    rng = random.Random(seed)
    run = mlop.init(
        project="demo",
        name=name,
        config={"lr": lr, "layers": layers, "optimizer": "adamw", "batch_size": 64},
    )
    print(f"training {name} with lr={lr}")
    speed = lr * 400 / layers
    for step in range(steps):
        t = step / steps
        loss = 2.3 * math.exp(-speed * t * 6) + 0.15 + rng.gauss(0, 0.03) * (1 - t / 2)
        data = {
            "train/loss": loss,
            "train/lr": lr * 0.5 * (1 + math.cos(math.pi * t)),
        }
        if step % 20 == 0:
            data["val/loss"] = loss * 1.08 + 0.02
            data["val/accuracy"] = max(0.0, min(0.99, 1 - loss / 2.4 + rng.gauss(0, 0.01)))
        if np is not None and step % 50 == 0:
            g = np.random.default_rng(seed + step)
            data["weights/layer0"] = mlop.Histogram(g.normal(0, 1 - t / 2, 2000), bins=40)
            x = np.linspace(0, 1, 96)
            wave = np.sin(np.outer(x, x) * (4 + 20 * t) * math.pi)
            image = np.stack([wave, np.roll(wave, 8, 0), -wave], axis=-1)
            data["samples/reconstruction"] = mlop.Image(image, caption=f"step-{step}")
            data["val/confusion"] = mlop.Table(
                data=[[int(90 * (0.5 + t / 2)), int(10 * (1 - t))], [int(12 * (1 - t)), 88]],
                columns=["pred cat", "pred dog"],
                rows=["cat", "dog"],
            )
        run.log(data)
    if loss > 1.0:
        run.alert(title="Not converging", message=f"final loss {loss:.3f}", level="WARN")
    print(f"finished {name}: loss {loss:.4f}")
    run.finish()


if __name__ == "__main__":
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    train("baseline", lr=1e-3, layers=4, steps=steps, seed=1)
    train("wide", lr=1e-3, layers=2, steps=steps, seed=2)
    train("slow-lr", lr=1e-4, layers=4, steps=steps, seed=3)
