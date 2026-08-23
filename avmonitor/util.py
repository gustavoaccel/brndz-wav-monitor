"""Small shared helpers used across monitor threads."""
import queue
import threading

# pyaudiowpatch/PortAudio's PyAudio() construction (and, symmetrically,
# .terminate()) is NOT safe to call from two threads at the exact same
# moment -- reproduced directly (2/2, with a native-crash traceback showing
# both threads stuck inside pyaudiowpatch's __init__) when
# AudioSpectrumAnalyzer and AudioIOMonitor both start up back-to-back and
# race to initialize the WASAPI host API simultaneously. Each thread that
# constructs/terminates a PyAudio() instance must hold this lock for just
# that moment -- not for its whole lifetime, or they'd never run concurrently.
PORTAUDIO_INIT_LOCK = threading.Lock()


def push_latest(q: "queue.Queue", item) -> None:
    """Put `item` into a maxsize=1 queue, dropping whatever was there before.

    Stats/audio producers only care that the UI sees the freshest snapshot,
    never a backlog of stale ones, so this replaces instead of blocking.
    """
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def push_fifo(q: "queue.Queue", item) -> None:
    """Put `item` into a bounded queue, dropping the oldest entry if full.

    Used for discrete events (network status changes, process crashes) where
    every item matters and losing one would hide a real event -- unlike
    push_latest, which is for high-rate streams where only the newest value
    is ever useful.
    """
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def drain_all(q: "queue.Queue") -> list:
    """Return every item currently queued, without blocking."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


class DeviceWatcher(threading.Thread):
    """Resolves "what's the current audio device" on its own thread, with
    its own PyAudio() instance -- completely separate from whatever
    real-time capture thread is reading a stream at the same time.

    Why this exists: `resolve_fn` (the loopback/input device-picking
    logic in audio_spectrum.py/audio_io.py) does real OS/COM device
    enumeration work that isn't free -- measured directly on real
    hardware at up to ~86ms for the output picker and ~76ms for the input
    picker, against per-chunk time budgets of ~85ms and ~43ms
    respectively. Calling that synchronously, inline, in the real-time
    capture loop's periodic "did the device change?" recheck means that
    on a slow poll, the *next* stream.read() gets delayed by nearly (or
    more than) an entire chunk's worth of time -- which risks a real
    driver-level buffer overflow, audible as choppy/glitchy audio in
    whatever's being captured/recorded at that exact moment. Confirmed
    this was happening in the field (both output and mic recordings
    reported as choppy), not just a theoretical risk.

    The capture thread only ever reads `.current` (a plain attribute --
    single-reference swap, GIL-atomic, safe to read from another thread
    without a lock) instead of calling `resolve_fn` itself. `resolve_fn`
    runs on its own PyAudio() instance rather than sharing the capture
    thread's, so there's no risk of the two ever contending over the same
    native handle either.
    """

    def __init__(self, resolve_fn, poll_s: float):
        super().__init__(name="DeviceWatcher", daemon=True)
        self._resolve_fn = resolve_fn
        self._poll_s = poll_s
        self._stop_event = threading.Event()
        self.current = None  # whatever resolve_fn(p, pyaudio) returns; None until the first poll lands

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
        except Exception:
            return

        com_initialized = False
        try:
            import comtypes
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        with PORTAUDIO_INIT_LOCK:
            p = pyaudio.PyAudio()
        try:
            while not self._stop_event.is_set():
                try:
                    self.current = self._resolve_fn(p, pyaudio)
                except Exception:
                    pass
                if self._stop_event.wait(self._poll_s):
                    break
        finally:
            with PORTAUDIO_INIT_LOCK:
                try:
                    p.terminate()
                except Exception:
                    pass
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
