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
