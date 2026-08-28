"""Watches specific processes (e.g. obs64, vMix64) for crash, hang, or
excessive RAM use, and raises sound+visual alerts for each transition.
"""
import threading
import time
import queue
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import psutil

from . import win_native
from .util import push_fifo


@dataclass
class ProcessState:
    name: str
    running: bool
    pid: Optional[int] = None
    ram_mb: float = 0.0
    hung: bool = False
    crashed: bool = False  # was seen running before, isn't anymore
    error: Optional[str] = None


@dataclass
class ProcessSnapshot:
    timestamp: float
    states: List[ProcessState] = field(default_factory=list)
    events: List[str] = field(default_factory=list)


# How long to wait before retrying the expensive full-process-list scan
# after it comes back empty. Measured directly on a real, moderately busy
# dev machine (198 processes): _find_process() alone costs ~371ms when
# the target isn't running (has to walk the whole list -- no early exit).
# With the default process_check_interval_s=2.0 and 2 default watched
# processes (obs64, vMix64), a technician who opens this monitor *before*
# launching their production software -- a completely normal setup
# order -- would otherwise burn ~740ms of CPU every 2s (two full scans,
# both empty) continuously until it launches, for no benefit: nothing
# useful can change faster than this backoff allows anyway, since the
# watched app isn't running yet. 5s keeps detection of it appearing
# still fast enough to be imperceptible to a human, while cutting that
# sustained cost by more than half.
_NOT_FOUND_RESCAN_S = 5.0


class _WatchState:
    def __init__(self, name: str):
        self.name = name
        self.was_running: Optional[bool] = None
        self.ram_alerted = False
        self.hang_alerted = False
        self.ever_running = False  # only a crash if it was ever actually seen running
        self.proc: Optional[psutil.Process] = None  # cached handle, see _resolve_process
        self.last_scan_attempt = 0.0  # time.time() of the last (possibly empty) _find_process() call


def _find_process(name: str) -> Optional[psutil.Process]:
    target = name.lower().removesuffix(".exe")
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            pname = (p.info["name"] or "").lower().removesuffix(".exe")
            if pname == target:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


class ProcessWatcher(threading.Thread):
    def __init__(self, cfg, out_queue: "queue.Queue[ProcessSnapshot]"):
        super().__init__(name="ProcessWatcher", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self._stop_event = threading.Event()
        self._watch: Dict[str, _WatchState] = {
            name: _WatchState(name) for name in cfg.watched_processes
        }

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            snapshot = ProcessSnapshot(timestamp=time.time())

            for name, watch in self._watch.items():
                try:
                    state = self._check_one(name, watch)
                except Exception as e:
                    state = ProcessState(name=name, running=False, error=str(e))
                    snapshot.events.append(f"{name}: erro monitorando ({e})")

                snapshot.states.append(state)
                snapshot.events.extend(self._events_for(watch, state))

            push_fifo(self.out_queue, snapshot)
            self._stop_event.wait(self.cfg.process_check_interval_s)

    def _resolve_process(self, name: str, watch: _WatchState) -> Optional[psutil.Process]:
        """Reuse the cached handle while it's still the right process --
        checking one known pid is a single syscall, versus walking every
        process on the system. Only re-scans when there's no live handle
        (never found yet, or the watched app just closed/crashed/restarted)
        -- and even then, not more often than _NOT_FOUND_RESCAN_S while it
        keeps coming back empty (measured directly: ~371ms per scan on a
        real, moderately busy machine -- retrying that every single
        process_check_interval_s while the target simply isn't running
        yet, e.g. before the technician has launched OBS/vMix, burns real
        sustained CPU for no benefit).
        """
        target = name.lower().removesuffix(".exe")
        if watch.proc is not None:
            try:
                if watch.proc.is_running() and watch.proc.name().lower().removesuffix(".exe") == target:
                    return watch.proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            watch.proc = None

        now = time.time()
        if now - watch.last_scan_attempt < _NOT_FOUND_RESCAN_S:
            return None
        watch.last_scan_attempt = now
        watch.proc = _find_process(name)
        return watch.proc

    def _check_one(self, name: str, watch: _WatchState) -> ProcessState:
        proc = self._resolve_process(name, watch)

        if proc is None:
            state = ProcessState(name=name, running=False, crashed=watch.ever_running)
        else:
            try:
                ram_mb = proc.memory_info().rss / (1024 * 1024)
                hung = win_native.is_process_hung(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # `was_running` is deliberately left untouched here --
                # _events_for() is the only place that's supposed to
                # update it, right after comparing it against the new
                # state to detect a running->not-running transition. This
                # early write (found during the pre-V3 review) shadowed
                # that comparison for the narrow race where the process
                # dies between _resolve_process()'s liveness check and
                # memory_info()/is_process_hung() here, so a real crash
                # in that window could go undetected/unlogged.
                return ProcessState(name=name, running=False, crashed=watch.ever_running)
            state = ProcessState(name=name, running=True, pid=proc.pid, ram_mb=ram_mb, hung=hung)
            watch.ever_running = True

        return state

    def _events_for(self, watch: _WatchState, state: ProcessState) -> List[str]:
        events = []

        if watch.was_running is True and state.running is False:
            events.append(f"{state.name} sumiu (possível crash)")
        elif watch.was_running is False and state.running is True:
            events.append(f"{state.name} voltou a rodar")

        if state.running:
            if state.hung and not watch.hang_alerted:
                events.append(f"{state.name} não está respondendo (travado)")
                watch.hang_alerted = True
            elif not state.hung:
                watch.hang_alerted = False

            if state.ram_mb > self.cfg.ram_alert_mb and not watch.ram_alerted:
                events.append(f"{state.name} passou do limite de RAM ({state.ram_mb:.0f} MB)")
                watch.ram_alerted = True
            elif state.ram_mb <= self.cfg.ram_alert_mb:
                watch.ram_alerted = False
        else:
            watch.hang_alerted = False
            watch.ram_alerted = False

        watch.was_running = state.running
        return events
