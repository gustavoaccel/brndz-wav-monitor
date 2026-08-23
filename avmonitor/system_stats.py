"""CPU / RAM / GPU / disk / top-processes polling.

Runs on its own thread at cfg.stats_interval_s, independent of the EQ frame
rate -- psutil calls (especially process iteration) are relatively heavy
and have no reason to run at 60Hz.
"""
import platform
import subprocess
import threading
import time
import queue
from dataclasses import dataclass, field
from typing import List, Optional

import psutil

from .util import push_latest
from . import win_native

try:
    import pynvml
    _NVML_AVAILABLE = True
except Exception:
    _NVML_AVAILABLE = False

_CREATE_NO_WINDOW = 0x08000000


@dataclass
class ProcessInfo:
    pid: int
    name: str
    ram_mb: float


@dataclass
class DiskInfo:
    path: str
    free_gb: float
    total_gb: float
    error: Optional[str] = None


@dataclass
class GpuInfo:
    available: bool = False
    name: str = ""
    util_percent: float = 0.0
    vram_used_mb: float = 0.0
    vram_total_mb: float = 0.0
    error: Optional[str] = None


@dataclass
class HardwareInfo:
    """Static machine specs -- fetched once at monitor startup, not on every
    poll, since none of this changes during a session."""
    cpu_name: str = ""
    cpu_cores_physical: int = 0
    cpu_cores_logical: int = 0
    cpu_freq_mhz: float = 0.0
    ram_total_mb: float = 0.0
    gpu_name: str = ""
    gpu_vram_total_mb: float = 0.0


@dataclass
class StatsSnapshot:
    timestamp: float
    cpu_percent: float
    ram_percent: float
    ram_used_mb: float
    ram_total_mb: float
    gpu: GpuInfo
    disks: List[DiskInfo]
    top_processes: List[ProcessInfo]
    hw: HardwareInfo = field(default_factory=HardwareInfo)


def _cpu_name() -> str:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return " ".join(name.split())  # collapse the double spaces Windows likes to pad in
    except Exception:
        return platform.processor() or "CPU"


def _wmi_primary_adapter() -> "tuple[str, float]":
    """(name, vram_total_mb) of the first physical video adapter, via a
    one-time PowerShell/CIM query -- only called once at GpuMonitor
    construction (fallback path), not per-poll, so a brief subprocess here
    is fine (same tradeoff already made for the folder-picker in main.py).
    Filters AdapterRAM>0 to skip virtual/basic-render adapters ("Microsoft
    Basic Render Driver", RDP sessions) which report null/zero RAM.

    Known WMI quirk, not worked around here: `AdapterRAM` is a 32-bit
    field, so cards with >=4GB VRAM can report a wrapped/truncated value
    on some driver/OS combinations. Good enough for a rough field-monitor
    reading; not treated as authoritative.
    """
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Where-Object { $_.AdapterRAM -gt 0 } | "
        "Select-Object -First 1 Name, AdapterRAM | ConvertTo-Json -Compress"
    )
    try:
        import json
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
        data = json.loads(result.stdout.strip())
        name = data.get("Name") or "GPU"
        ram_mb = float(data.get("AdapterRAM") or 0) / (1024 * 1024)
        return name, ram_mb
    except Exception:
        return "GPU", 0.0


class PdhGpuMonitor:
    """Vendor-agnostic GPU utilization/memory fallback via Windows' own
    `GPU Engine`/`GPU Process Memory` performance counters -- used only
    when pynvml (NVIDIA-only) isn't available, so this is what makes
    AMD/Intel GPUs show up at all instead of "indisponível".

    Deliberately does *not* correlate engine instances back to a specific
    physical adapter by LUID (each instance name carries a `luid_...`
    segment that would allow it) -- solving that correctly for a genuinely
    multi-GPU machine is real, untested complexity this field tool doesn't
    need. Instead: report the *maximum* utilization and *summed* memory
    across every engine/process instance found, labeled with whichever
    single adapter WMI reports first. Correct for the common case (one
    dGPU, which is what this tool is built for); a deliberate
    simplification -- not a bug -- on a machine with more than one.
    """

    def __init__(self):
        self._util = win_native.GpuEngineCounters(r"\GPU Engine(*)\Utilization Percentage")
        self._mem = win_native.GpuEngineCounters(r"\GPU Process Memory(*)\Dedicated Usage")
        self._ok = self._util.available
        self._name, self._vram_total_mb = "", 0.0
        if self._ok:
            self._name, self._vram_total_mb = _wmi_primary_adapter()
            # PDH rate counters have nothing to diff against on their very
            # first sample -- prime both now so the polling loop's first
            # real read() already has a delta to report instead of an
            # empty/zero frame.
            self._util.read()
            self._mem.read()

    @property
    def available(self) -> bool:
        return self._ok

    def read(self) -> GpuInfo:
        if not self._ok:
            return GpuInfo(available=False, error="GPU não detectada (NVML e contadores de performance indisponíveis)")
        try:
            util_vals = self._util.read()
            mem_vals = self._mem.read()
            util = max(util_vals.values()) if util_vals else 0.0
            used_mb = (sum(mem_vals.values()) / (1024 * 1024)) if mem_vals else 0.0
            return GpuInfo(
                available=True, name=self._name or "GPU",
                util_percent=min(100.0, util), vram_used_mb=used_mb,
                vram_total_mb=self._vram_total_mb,
            )
        except Exception as e:
            return GpuInfo(available=False, error=str(e))

    def shutdown(self):
        self._util.close()
        self._mem.close()


class GpuMonitor:
    """NVIDIA via pynvml first (per-process accurate, cheap, already
    proven against real hardware); falls back to vendor-agnostic Windows
    performance counters (AMD/Intel/anything NVML doesn't cover) only
    when NVML isn't importable or finds no NVIDIA device."""

    def __init__(self):
        self._ok = False
        self._handle = None
        self._fallback: Optional[PdhGpuMonitor] = None
        if _NVML_AVAILABLE:
            try:
                pynvml.nvmlInit()
                self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._ok = True
            except Exception:
                self._ok = False
        if not self._ok:
            fb = PdhGpuMonitor()
            if fb.available:
                self._fallback = fb

    def read(self) -> GpuInfo:
        if self._ok:
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(self._handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
                name = pynvml.nvmlDeviceGetName(self._handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="ignore")
                return GpuInfo(
                    available=True,
                    name=name,
                    util_percent=float(util.gpu),
                    vram_used_mb=mem.used / (1024 * 1024),
                    vram_total_mb=mem.total / (1024 * 1024),
                )
            except Exception as e:
                return GpuInfo(available=False, error=str(e))
        if self._fallback is not None:
            return self._fallback.read()
        return GpuInfo(available=False, error="GPU não detectada (NVIDIA/AMD/Intel)")

    def shutdown(self):
        if self._ok:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
        if self._fallback is not None:
            self._fallback.shutdown()


def _read_disks(paths: List[str]) -> List[DiskInfo]:
    results = []
    for p in paths:
        try:
            usage = psutil.disk_usage(p)
            results.append(DiskInfo(
                path=p,
                free_gb=usage.free / (1024 ** 3),
                total_gb=usage.total / (1024 ** 3),
            ))
        except Exception as e:
            results.append(DiskInfo(path=p, free_gb=0.0, total_gb=0.0, error=str(e)))
    return results


def _top_processes(n: int) -> List[ProcessInfo]:
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
        try:
            mem = p.info["memory_info"]
            if mem is None:
                continue
            procs.append(ProcessInfo(
                pid=p.info["pid"],
                name=p.info["name"] or "?",
                ram_mb=mem.rss / (1024 * 1024),
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x.ram_mb, reverse=True)
    return procs[:n]


class SystemStatsMonitor(threading.Thread):
    def __init__(self, cfg, out_queue: "queue.Queue[StatsSnapshot]"):
        super().__init__(name="SystemStatsMonitor", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self._stop_event = threading.Event()
        self._gpu = GpuMonitor()
        self._top_processes_cache: List[ProcessInfo] = []
        self._top_processes_at = 0.0
        self._hw = HardwareInfo()

    def stop(self):
        self._stop_event.set()

    def run(self):
        # Prime psutil's internal CPU-percent baseline; first real call
        # right after this returns a meaningful (non-zero) delta.
        psutil.cpu_percent(interval=None)
        self._hw = self._collect_hardware_info()

        while not self._stop_event.is_set():
            try:
                snapshot = self._collect()
                push_latest(self.out_queue, snapshot)
            except Exception as e:
                push_latest(self.out_queue, StatsSnapshot(
                    timestamp=time.time(), cpu_percent=0.0, ram_percent=0.0,
                    ram_used_mb=0.0, ram_total_mb=0.0,
                    gpu=GpuInfo(available=False, error=str(e)),
                    disks=[], top_processes=[], hw=self._hw,
                ))
            self._stop_event.wait(self.cfg.stats_interval_s)

        self._gpu.shutdown()

    def _collect_hardware_info(self) -> HardwareInfo:
        freq = None
        try:
            freq = psutil.cpu_freq()
        except Exception:
            pass
        gpu = self._gpu.read()
        return HardwareInfo(
            cpu_name=_cpu_name(),
            cpu_cores_physical=psutil.cpu_count(logical=False) or 0,
            cpu_cores_logical=psutil.cpu_count(logical=True) or 0,
            cpu_freq_mhz=(freq.max or freq.current) if freq else 0.0,
            ram_total_mb=psutil.virtual_memory().total / (1024 * 1024),
            gpu_name=gpu.name if gpu.available else "",
            gpu_vram_total_mb=gpu.vram_total_mb if gpu.available else 0.0,
        )

    def _collect(self) -> StatsSnapshot:
        cpu = psutil.cpu_percent(interval=None)
        vmem = psutil.virtual_memory()

        now = time.time()
        if now - self._top_processes_at >= self.cfg.top_processes_interval_s:
            self._top_processes_cache = _top_processes(self.cfg.top_n_processes)
            self._top_processes_at = now

        return StatsSnapshot(
            timestamp=now,
            cpu_percent=cpu,
            ram_percent=vmem.percent,
            ram_used_mb=vmem.used / (1024 * 1024),
            ram_total_mb=vmem.total / (1024 * 1024),
            gpu=self._gpu.read(),
            disks=_read_disks(self.cfg.disk_paths),
            top_processes=self._top_processes_cache,
            hw=self._hw,
        )
