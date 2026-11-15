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


def _nvml(fn, *args):
    """One NVML query, or None where this device or driver cannot answer it."""
    import pynvml

    try:
        return fn(*args)
    except pynvml.NVMLError as e:
        logger.debug("%s: NVIDIA: %s: %s", tag, fn.__name__, e)
        return None


def _nvml_processes(handle) -> list:
    import pynvml

    return (_nvml(pynvml.nvmlDeviceGetComputeRunningProcesses, handle) or []) + (
        _nvml(pynvml.nvmlDeviceGetGraphicsRunningProcesses, handle) or []
    )


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
            except ImportError:
                logger.debug(f"{tag}: NVIDIA: pynvml not found")
                return d
            try:
                pynvml.nvmlInit()
                logger.info(f"{tag}: NVIDIA GPU detected")
                d["nvidia"].update(
                    {
                        "count": pynvml.nvmlDeviceGetCount(),
                        "driver": _nvml(pynvml.nvmlSystemGetDriverVersion),
                        "devices": [],
                        "handles": [],
                    }
                )
                for i in range(d["nvidia"]["count"]):
                    h = pynvml.nvmlDeviceGetHandleByIndex(i)
                    # Every query below is optional: what a device reports
                    # varies (one with unified memory has no memory of its
                    # own to report), and one gap must not hide the rest.
                    dev = {"name": str(_nvml(pynvml.nvmlDeviceGetName, h) or f"gpu{i}")}
                    memory = _nvml(pynvml.nvmlDeviceGetMemoryInfo, h)
                    if memory is not None:
                        dev["memory"] = {"total": memory.total}
                    temp = _nvml(
                        pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU
                    )
                    if temp is not None:
                        dev["temp"] = temp
                    dev["pid"] = [p.pid for p in _nvml_processes(h)]
                    d["nvidia"]["devices"].append(dev)
                    d["nvidia"]["handles"].append(h)
            except pynvml.NVMLError_LibraryNotFound:
                logger.debug(f"{tag}: NVIDIA: driver not found")
            except Exception as e:
                logger.error("%s: NVIDIA: error: %s", tag, e)
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
        d.update(self.monitor_gpu())
        return d

    def monitor_gpu(self) -> Dict[str, Union[int, float]]:
        nvidia = (self.gpu or {}).get("nvidia", {})
        if not nvidia.get("handles"):
            return {}
        import pynvml

        p = self.settings.x_sys_label
        d: Dict[str, Union[int, float]] = {}
        names = [dev["name"].lower().replace(" ", "_") for dev in nvidia["devices"]]
        for i, h in enumerate(nvidia["handles"]):
            # two devices of one model must not write to the same series
            dev = names[i] if names.count(names[i]) == 1 else f"{names[i]}.{i}"
            m: Dict[str, Any] = {}
            rates = _nvml(pynvml.nvmlDeviceGetUtilizationRates, h)
            if rates is not None:
                m["pct"], m["mem.pct"] = rates.gpu, rates.memory
            memory = _nvml(pynvml.nvmlDeviceGetMemoryInfo, h)
            if memory is not None:
                m["mem.used"] = memory.used
            else:
                # Unified memory: the device has no pool of its own, so what
                # the processes on it hold is the closest thing to "used".
                used = [u.usedGpuMemory for u in _nvml_processes(h)]
                if used:
                    m["mem.used"] = sum(u or 0 for u in used)
            m["power"] = _nvml(pynvml.nvmlDeviceGetPowerUsage, h)
            m["temp"] = _nvml(
                pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU
            )
            d.update(
                {f"{p}/gpu.nvda.{dev}.{k}": v for k, v in m.items() if v is not None}
            )
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
                devices = []
                for h in self.gpu["nvidia"].get("handles", []):
                    rates = _nvml(pynvml.nvmlDeviceGetUtilizationRates, h)
                    memory = _nvml(pynvml.nvmlDeviceGetMemoryInfo, h)
                    devices.append(
                        {
                            "name": _nvml(pynvml.nvmlDeviceGetName, h),
                            "temp": _nvml(
                                pynvml.nvmlDeviceGetTemperature,
                                h,
                                pynvml.NVML_TEMPERATURE_GPU,
                            ),
                            "gpu_percent": rates.gpu if rates else None,
                            "memory": {
                                "percent": rates.memory if rates else None,
                                "used": to_human(memory.used) if memory else None,
                                "total": to_human(memory.total) if memory else None,
                            },
                            "power": {
                                "usage": _nvml(pynvml.nvmlDeviceGetPowerUsage, h),
                                "limit": _nvml(
                                    pynvml.nvmlDeviceGetEnforcedPowerLimit, h
                                ),
                            },
                            "pid": [p.pid for p in _nvml_processes(h)],
                        }
                    )
                d["gpu"]["nvidia"]["devices"] = devices
        if self.settings.mode == "debug":
            d["memory"]["swap"] = {
                k: to_human(v)
                for k, v in psutil.swap_memory()._asdict().items()
                if k != "percent"
            }
        return d
