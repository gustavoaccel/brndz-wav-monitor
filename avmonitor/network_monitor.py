"""Ping-based network monitor.

Shells out to the Windows `ping` utility instead of raw ICMP sockets --
raw sockets need admin privileges on Windows, `ping.exe` doesn't.
Tracks a rolling window per target so the UI can show a real loss rate,
not just the last probe's OK/FAIL (an explicit improvement over the
PowerShell prototype per the spec).
"""
import re
import subprocess
import threading
import time
import queue
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

import psutil

from .util import push_fifo

_CREATE_NO_WINDOW = 0x08000000
_TIME_RE = re.compile(r"(?:tempo|time)[=<]\s*(\d+)\s*ms", re.IGNORECASE)


@dataclass
class TargetStatus:
    host: str
    alive: bool
    latency_ms: Optional[float]
    loss_percent: float
    just_changed: bool = False


@dataclass
class NetworkSnapshot:
    timestamp: float
    targets: List[TargetStatus] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    download_mbps: float = 0.0
    upload_mbps: float = 0.0


def _ping_once(host: str, timeout_ms: int) -> "tuple[bool, Optional[float]]":
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000.0) + 2.0,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return False, None

    match = _TIME_RE.search(result.stdout)
    success = result.returncode == 0 and match is not None
    latency = float(match.group(1)) if match else None
    return success, latency


class _TargetState:
    def __init__(self, host: str, window: int):
        self.host = host
        self.history = deque(maxlen=window)
        self.was_alive: Optional[bool] = None


class NetworkMonitor(threading.Thread):
    def __init__(self, cfg, out_queue: "queue.Queue[NetworkSnapshot]"):
        super().__init__(name="NetworkMonitor", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self._stop_event = threading.Event()
        self._states = [_TargetState(h, cfg.ping_window) for h in cfg.ping_targets]
        self._last_io = None
        self._last_io_at = None

    def stop(self):
        self._stop_event.set()

    def _measure_throughput(self) -> "tuple[float, float]":
        """Whole-system download/upload rate in Mbps, from the delta in
        psutil's cumulative byte counters between polls -- no per-target
        bandwidth is available from ping, this is a system-wide reading."""
        try:
            io = psutil.net_io_counters()
        except Exception:
            return 0.0, 0.0

        now = time.time()
        if self._last_io is None:
            self._last_io, self._last_io_at = io, now
            return 0.0, 0.0

        dt = max(1e-3, now - self._last_io_at)
        down_mbps = (io.bytes_recv - self._last_io.bytes_recv) * 8 / dt / 1_000_000
        up_mbps = (io.bytes_sent - self._last_io.bytes_sent) * 8 / dt / 1_000_000
        self._last_io, self._last_io_at = io, now
        return max(0.0, down_mbps), max(0.0, up_mbps)

    def run(self):
        while not self._stop_event.is_set():
            download_mbps, upload_mbps = self._measure_throughput()
            snapshot = NetworkSnapshot(timestamp=time.time(), download_mbps=download_mbps, upload_mbps=upload_mbps)

            for state in self._states:
                alive, latency = _ping_once(state.host, self.cfg.ping_timeout_ms)
                state.history.append(alive)
                loss = (state.history.count(False) / len(state.history)) * 100.0

                changed = state.was_alive is not None and alive != state.was_alive
                if changed:
                    snapshot.events.append(
                        f"{state.host} {'voltou (UP)' if alive else 'caiu (DOWN)'}"
                    )
                state.was_alive = alive

                snapshot.targets.append(TargetStatus(
                    host=state.host,
                    alive=alive,
                    latency_ms=latency,
                    loss_percent=loss,
                    just_changed=changed,
                ))

            push_fifo(self.out_queue, snapshot)
            self._stop_event.wait(self.cfg.ping_interval_s)
