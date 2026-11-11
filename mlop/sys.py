import importlib.metadata
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Union

import psutil

from .sets import Settings
from .util import run_cmd, to_human  # TODO: move to server side

logger = logging.getLogger(f"{__name__.split('.')[0]}")
tag = "System"


def _strip_credentials(url: str) -> str:
    """https://user:token@host/repo -> https://host/repo"""
    return re.sub(r"(?<=://)[^/@]+@", "", url)


class System:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        self.uname: Dict[str, str] = platform.uname()._asdict()
        self.timezone: List[str] = list(time.tzname)

        self.cpu_count = psutil.cpu_count
        try:  # perf
            self.cpu_freq: List[Dict[str, float]] = [
                i._asdict() for i in psutil.cpu_freq(percpu=True)
            ]
            assert self.cpu_freq
        except Exception:  # errors on aarch64
            self.cpu_freq = [{"current": 0, "min": 0, "max": 0}]

        self.svmem: Dict[str, Any] = psutil.virtual_memory()._asdict()
        self.sswap: Dict[str, Any] = psutil.swap_memory()._asdict()
        self.disk: List[Dict[str, Any]] = [
            i._asdict() for i in psutil.disk_partitions()
        ]
        self.net_if_addrs: Dict[str, List[Dict[str, Any]]] = {
            i: [
                {k: v for k, v in j._asdict().items() if k != "family"}
                for j in psutil.net_if_addrs()[i]
            ]
            for i in psutil.net_if_addrs()
        }
        self.boot_time: float = psutil.boot_time()
        self.users: List[Dict[str, Any]] = [i._asdict() for i in psutil.users()]

        self.pid: int = os.getpid()
        self.proc: psutil.Process = psutil.Process(pid=self.pid)
        with self.proc.oneshot():  # perf
            self.proc_info: Dict[str, Any] = self.proc.as_dict(attrs=["exe", "cmdline"])
            self.proc_child: List[psutil.Process] = self.proc.children(recursive=True)
            self.pid_child: List[int] = [p.pid for p in self.proc_child] + [self.pid]

        self.requirements: List[str] = [
            f"{p.metadata.get('Name')}=={p.version}"
            for p in importlib.metadata.distributions()
            if p.metadata
        ]
        if self.settings.mode == "debug":  # privacy guard
            self.environ: Dict[str, str] = self.proc.environ()

        self.gpu: Dict[str, Any] = self.get_gpu()
        self.git: Dict[str, Any] = self.get_git()

    def __getattr__(self, name: str) -> Optional[Any]:
        if name == "settings" or name.startswith("_"):
            raise AttributeError(name)
        return self.get_psutil(name)

    def get_psutil(self, name: str) -> Optional[Any]:  # handling os specific methods
        if hasattr(psutil, name):
            return getattr(psutil, name)
        else:
            return None

    def get_gpu(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}

        # NVIDIA
        n = run_cmd("nvidia-smi")
        if n:
            d["nvidia"] = {"smi": n}
            try:
                import pynvml

                try:
                    pynvml.nvmlInit()
                    logger.info(f"{tag}: NVIDIA GPU detected")
                    d["nvidia"].update(
                        {
                            "count": pynvml.nvmlDeviceGetCount(),
                            "driver": pynvml.nvmlSystemGetDriverVersion(),
                            "devices": [],
                            "handles": [],
                        }
                    )
                    for i in range(d["nvidia"]["count"]):
                        h = pynvml.nvmlDeviceGetHandleByIndex(i)
                        d["nvidia"]["handles"].append(h)
                        d["nvidia"]["devices"].append(
                            {
                                "name": pynvml.nvmlDeviceGetName(h),
                                "memory": {
                                    "total": pynvml.nvmlDeviceGetMemoryInfo(h).total
                                },
                                "temp": pynvml.nvmlDeviceGetTemperature(
                                    h, pynvml.NVML_TEMPERATURE_GPU
                                ),
                                "pid": [
                                    p.pid
                                    for p in (
                                        pynvml.nvmlDeviceGetComputeRunningProcesses(h)
                                        + pynvml.nvmlDeviceGetGraphicsRunningProcesses(
                                            h
                                        )
                                    )
                                ],
                            }
                        )
                except pynvml.NVMLError_LibraryNotFound:
                    logger.debug(f"{tag}: NVIDIA: driver not found")
                except Exception as e:
                    logger.error("%s: NVIDIA: error: %s", tag, e)
            except ImportError:
                logger.debug(f"{tag}: NVIDIA: pynvml not found")
        return d

    def get_git(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}

        def git(*args):
            if not shutil.which("git"):
                return None
            try:
                r = subprocess.run(
                    ["git", "-C", f"{self.settings.dir}", *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            return r.stdout.strip() if r.returncode == 0 else None

        root = git("rev-parse", "--show-toplevel")
        if not root:
            logger.debug("%s: git: repository not detected", tag)
            return d
        status = git("status", "--porcelain", "--untracked-files=no")
        d = {
            "root": root,
            "dirty": bool(status),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit": git("rev-parse", "HEAD"),
        }
        for key, name in (("name", "user.name"), ("email", "user.email")):
            value = git("config", "--get", name)
            if value:
                d[key] = value
        url = git("config", "--get", "remote.origin.url")
        if url:
            d["url"] = _strip_credentials(url)
        d["diff"] = {"remote": git("diff", "--submodule=diff", "@{u}")}
        if d["dirty"]:
            d["diff"]["head"] = git("diff", "--submodule=diff", "HEAD")
        return d

    def get_info(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "process": {
                **self.proc_info,
                "pid": self.pid,
            },
            "platform": self.uname,
            "timezone": self.timezone,
            "cpu": {
                "physical": self.cpu_count(logical=False),
                "virtual": self.cpu_count(logical=True),
                "freq": {
                    "min": min([i["min"] for i in self.cpu_freq]),
                    "max": max([i["max"] for i in self.cpu_freq]),
                },
            },
            "memory": {
                "virt": self.svmem["total"],
                "swap": self.sswap["total"],
            },
            "boot_time": self.boot_time,
            "requirements": self.requirements,
        }
        if self.gpu:
            d["gpu"] = {}
            if self.gpu.get("nvidia"):
                d["gpu"]["nvidia"] = {
                    k: v for k, v in self.gpu["nvidia"].items() if k != "handles"
                }
        if self.git:
            d["git"] = self.git
        if self.settings.mode == "debug":
            d["process"]["environ"] = self.environ
            d = {
                **d,
                "disk": self.disk,
                "network": self.net_if_addrs,
                "users": self.users,
            }
        return d

    def monitor(self) -> Dict[str, Union[int, float]]:
        p = self.settings.x_sys_label
        d: Dict[str, Union[int, float]] = {
            **{
                f"{p}/cpu.pct.{i}": v
                for i, v in enumerate(psutil.cpu_percent(percpu=True))
            },
            **{
                f"{p}/mem.{k}": v
                for k, v in psutil.virtual_memory()._asdict().items()
                if k in ("active", "used")
            },
            **{
                f"{p}/disk.{k}": v
                for k, v in psutil.disk_usage(self.settings.get_dir())._asdict().items()
                if k in ("used",)
            },
            **{
                f"{p}/net.{k}": v
                for k, v in psutil.net_io_counters()._asdict().items()
                if k.startswith("bytes")
            },
        }
        if self.gpu:
            if self.gpu.get("nvidia", {}).get("handles"):
                import pynvml

                for h in self.gpu["nvidia"].get("handles", []):
                    dev = str(pynvml.nvmlDeviceGetName(h)).lower().replace(" ", "_")
                    d[f"{p}/gpu.nvda.{dev}.pct"] = pynvml.nvmlDeviceGetUtilizationRates(
                        h
                    ).gpu
                    d[f"{p}/gpu.nvda.{dev}.mem.pct"] = (
                        pynvml.nvmlDeviceGetUtilizationRates(h).memory
                    )
                    d[f"{p}/gpu.nvda.{dev}.power"] = pynvml.nvmlDeviceGetPowerUsage(h)
        return d

    def monitor_human(self) -> Dict[str, Any]:  # TODO: deprecate
        try:
            cpu_freq = [i.current for i in psutil.cpu_freq(percpu=True)]
        except Exception:  # errors on darwin t81xx
            cpu_freq = [0]

        d: Dict[str, Any] = {
            "cpu": {
                "percent": psutil.cpu_percent(percpu=True),
                "freq": cpu_freq,
            },
            "memory": {
                "virt": {
                    k: to_human(v)
                    for k, v in psutil.virtual_memory()._asdict().items()
                    if k != "percent"
                },
            },
            "disk": {
                "out": to_human(psutil.disk_io_counters().read_bytes),
                "in": to_human(psutil.disk_io_counters().write_bytes),
                "usage": {
                    k: to_human(v)
                    for k, v in psutil.disk_usage(self.settings.get_dir())
                    ._asdict()
                    .items()
                    if k != "percent"
                },
            },
            "network": {
                "out": to_human(psutil.net_io_counters().bytes_sent),
                "in": to_human(psutil.net_io_counters().bytes_recv),
            },
        }
        with self.proc.oneshot():  # perf
            d["process"] = {
                **self.proc.as_dict(
                    attrs=["status", "cpu_percent", "memory_percent", "num_threads"]
                ),
                "memory": to_human(self.proc.memory_info().rss),
            }
        if self.gpu:
            d["gpu"] = {}
            if self.gpu.get("nvidia", {}).get("handles"):
                import pynvml

                d["gpu"]["nvidia"] = {}
                d["gpu"]["nvidia"]["devices"] = [
                    {
                        "name": pynvml.nvmlDeviceGetName(h),
                        "temp": pynvml.nvmlDeviceGetTemperature(
                            h, pynvml.NVML_TEMPERATURE_GPU
                        ),
                        "gpu_percent": pynvml.nvmlDeviceGetUtilizationRates(h).gpu,
                        "memory": {
                            "percent": pynvml.nvmlDeviceGetUtilizationRates(h).memory,
                            "used": to_human(pynvml.nvmlDeviceGetMemoryInfo(h).used),
                            "total": to_human(pynvml.nvmlDeviceGetMemoryInfo(h).total),
                        },
                        "power": {
                            "usage": pynvml.nvmlDeviceGetPowerUsage(h),
                            "limit": pynvml.nvmlDeviceGetEnforcedPowerLimit(h),
                        },
                        "pid": [
                            p.pid
                            for p in (
                                pynvml.nvmlDeviceGetComputeRunningProcesses(h)
                                + pynvml.nvmlDeviceGetGraphicsRunningProcesses(h)
                            )
                        ],
                    }
                    for h in self.gpu["nvidia"].get("handles", [])
                ]
        if self.settings.mode == "debug":
            d["memory"]["swap"] = {
                k: to_human(v)
                for k, v in psutil.swap_memory()._asdict().items()
                if k != "percent"
            }
        return d
