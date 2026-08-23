"""Streams the output loopback's raw PCM to a WAV or MP3 file on disk via
a background writer thread -- never buffers a whole session in RAM, and
(as of this module's second revision) never lets disk I/O timing touch
the audio capture thread either.

No second capture: AudioSpectrumAnalyzer/AudioIOMonitor hand this the
exact same block they already read for the spectrum/meter (via
set_recording_sink), and idle cost is exactly zero: the sink is None and
the capture thread does nothing extra until REC is pressed.

WAV via the stdlib `wave` module (no dependency). MP3 via `lameenc` -- a
compiled LAME wheel with no external binary/DLL to find on PATH, small
enough (~150KB installed) to not violate the "no heavy encoder" rule that
kept MP3 out of earlier rounds; both writers satisfy the same
write(raw_bytes)/close() shape so _RecordingSession doesn't care which
one it's holding.

Threading, and why write() used to be the wrong place for disk I/O:
`write()` is called directly from the audio capture thread's sink (the
same thread doing FFT/meter work every ~40-85ms). The first version of
this module called the writer's write() -- real disk I/O -- right there,
synchronously, on that thread. That's a real bug, not just a style
nit: if the disk stalls even briefly (an external/USB drive doing a
seek, antivirus scanning the freshly-written file, Windows Search
indexing it, or just contention from other apps writing to the same
drive during a live event), the capture thread falls behind on reading
the next block from PortAudio -- and a WASAPI capture stream that isn't
read fast enough drops/corrupts audio at the driver level, which shows
up as audible glitching in exactly the recording being written, not
necessarily anywhere else. `write()` now only ever does an in-memory
queue.put_nowait() (microseconds, never blocks); a dedicated per-session
thread (_RecordingSession) does the actual writer.write() disk I/O,
completely decoupled from the capture thread's timing.

`start()`/`stop()` still run on the main/UI thread (button clicks), but
neither blocks it either: `stop()` only flips a flag and returns --
finishing the file (draining the queue, closing the writer) happens on
the session's own thread, reported back via pop_last_saved()/
pop_pending_error() exactly like before, just arriving a frame or few
later instead of on the same frame as the click. main.py's existing
per-frame polling already handled "whenever it shows up", so this
needed no changes on that side.
"""
import queue
import shutil
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import win_native

_MIN_FREE_BYTES = 200 * 1024 * 1024  # refuse to start below this much free space
_MP3_BITRATE_KBPS = 192  # reference-quality for monitoring, not archival mastering
# ~25s of buffered audio at the output capture's ~85ms/chunk (less at the
# mic's ~43ms/chunk) -- enough to absorb a real transient disk stall
# without dropping anything; if it's still full after that, the disk is
# genuinely stuck, not just slow, and dropping (not blocking) is correct.
_WRITE_QUEUE_MAXSIZE = 300


@dataclass
class RecordingState:
    active: bool = False
    filename: str = ""
    path: Optional[Path] = None
    started_at: float = 0.0
    bytes_written: int = 0
    sample_rate: int = 0
    channels: int = 0
    format: str = "wav"


def resolve_recording_dir(cfg) -> "tuple[Path, bool]":
    """Returns (directory, used_fallback). Same brndz.wav-drive-then-local
    convention as session_log.py's resolve_log_dir, so recordings land
    next to the session logs unless the user pinned a directory."""
    if cfg.recording_directory:
        return Path(cfg.recording_directory), False
    drive = win_native.find_drive_by_label(cfg.drive_label)
    if drive is not None:
        return drive / "AV_TOOLKIT" / "07_DOCUMENTATION" / "Gravacoes", False
    return Path(cfg.fallback_log_dir) / "Gravacoes", True


def _unique_path(directory: Path, stamp: str, ext: str, prefix: str = "BRNDZ") -> Path:
    path = directory / f"{prefix}_{stamp}.{ext}"
    n = 1
    while path.exists():
        path = directory / f"{prefix}_{stamp}_{n:02d}.{ext}"
        n += 1
    return path


class _WavWriter:
    """Wraps stdlib `wave` -- writeframes() is already streaming/append-only."""

    def __init__(self, path: Path, sample_rate: int, channels: int):
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(channels)
        self._wav.setsampwidth(2)  # paInt16, straight from AudioSpectrumAnalyzer's capture format
        self._wav.setframerate(sample_rate)

    def write(self, raw: bytes):
        self._wav.writeframes(raw)

    def close(self):
        self._wav.close()


class _Mp3Writer:
    """Wraps lameenc -- encode() returns whatever compressed bytes are
    ready for the chunk just fed in (LAME buffers internally across
    calls), written straight to disk; flush() on close() drains the
    encoder's tail so the last fraction-of-a-frame isn't lost."""

    def __init__(self, path: Path, sample_rate: int, channels: int):
        import lameenc
        self._encoder = lameenc.Encoder()
        self._encoder.set_bit_rate(_MP3_BITRATE_KBPS)
        self._encoder.set_in_sample_rate(sample_rate)
        self._encoder.set_channels(channels)
        self._encoder.set_quality(2)  # 2 = high quality, slower encode (still real-time-cheap at these rates)
        self._file = open(path, "wb")

    def write(self, raw: bytes):
        encoded = self._encoder.encode(raw)
        if encoded:
            self._file.write(encoded)

    def close(self):
        try:
            tail = self._encoder.flush()
            if tail:
                self._file.write(tail)
        finally:
            self._file.close()


def _make_writer(fmt: str, path: Path, sample_rate: int, channels: int):
    if fmt == "mp3":
        return _Mp3Writer(path, sample_rate, channels)
    return _WavWriter(path, sample_rate, channels)


class _RecordingSession:
    """One take: its own queue + background disk-writer thread. A fresh
    instance every AudioRecorder.start() -- never reused -- so a fast
    stop-then-start-again can never let the new take's state clobber the
    previous one's still-draining writer thread; the old session finishes
    itself out using only its own local references, however long that
    takes, with nothing left pointing back at it from AudioRecorder.
    """

    def __init__(self, writer, path: Path, sample_rate: int, channels: int, fmt: str, on_done, on_error):
        self.writer = writer
        self.path = path
        self.sample_rate = sample_rate
        self.channels = channels
        self.fmt = fmt
        self.started_at = time.time()
        self.bytes_written = 0
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=_WRITE_QUEUE_MAXSIZE)
        self._stop_requested = False
        self._dropped_chunks = 0
        self._on_done = on_done
        self._on_error = on_error
        self._thread = threading.Thread(target=self._run, name="RecordingWriter", daemon=True)
        self._thread.start()

    def push(self, raw: bytes):
        """Non-blocking -- called from the audio capture thread. Drops
        (never blocks) if the queue is genuinely full, i.e. the disk has
        been stuck for the whole ~25s buffer, not just momentarily slow."""
        try:
            self._queue.put_nowait(raw)
        except queue.Full:
            self._dropped_chunks += 1

    def finish(self):
        """Non-blocking -- called from the main thread. Only sets a flag;
        the writer thread notices once the queue drains and finishes the
        file on its own time, never here."""
        self._stop_requested = True

    def _run(self):
        try:
            while True:
                try:
                    raw = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._stop_requested:
                        break
                    continue
                self.writer.write(raw)
                self.bytes_written += len(raw)
        except Exception as e:
            try:
                self.writer.close()
            except Exception:
                pass
            self._on_error(str(e))
            return

        try:
            self.writer.close()
        except Exception as e:
            self._on_error(str(e))
            return
        self._on_done(self)


class AudioRecorder:
    def __init__(self, cfg, filename_prefix: str = "BRNDZ"):
        self.cfg = cfg
        self._filename_prefix = filename_prefix
        self._lock = threading.Lock()
        self._session: Optional[_RecordingSession] = None
        self.state = RecordingState()
        self._pending_error: Optional[str] = None
        self._last_saved: Optional[dict] = None  # for the main thread to log + report

    def is_active(self) -> bool:
        return self.state.active

    def pop_pending_error(self) -> Optional[str]:
        """Main-thread poll: any error that stopped a recording (format
        mismatch on the capture thread, or a disk write failure reported
        asynchronously by the session's writer thread). Clears it."""
        with self._lock:
            err, self._pending_error = self._pending_error, None
            return err

    def pop_last_saved(self) -> Optional[dict]:
        """Main-thread poll: details of the most recently *fully finished*
        take (file closed on disk), for SessionLogger.add_recording().
        Arrives here whenever the session's writer thread finishes
        draining and closing -- not necessarily the same frame stop() was
        called on. Clears it."""
        with self._lock:
            saved, self._last_saved = self._last_saved, None
            return saved

    def start(self, sample_rate: int, channels: int) -> "tuple[bool, str]":
        """Call from the main thread only."""
        with self._lock:
            if self.state.active:
                return False, "já gravando"
            if sample_rate <= 0 or channels <= 0:
                # Shared by both the output and mic recorders -- neither
                # knows here which source is missing, just that whatever
                # feeds it hasn't got a usable format yet.
                return False, "fonte de áudio indisponível no momento"

            directory, _ = resolve_recording_dir(self.cfg)
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, f"não foi possível criar a pasta de gravação ({directory}): {e}"

            try:
                free = shutil.disk_usage(directory).free
                if free < _MIN_FREE_BYTES:
                    return False, f"espaço insuficiente em {directory} ({free / (1024 * 1024):.0f}MB livres)"
            except Exception:
                pass

            fmt = "mp3" if self.cfg.recording_format == "mp3" else "wav"
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = _unique_path(directory, stamp, fmt, self._filename_prefix)
            try:
                writer = _make_writer(fmt, path, sample_rate, channels)
            except Exception as e:
                return False, f"não foi possível criar o arquivo: {e}"

            self._session = _RecordingSession(
                writer, path, sample_rate, channels, fmt,
                on_done=self._session_done, on_error=self._session_error,
            )
            self.state = RecordingState(
                active=True, filename=path.name, path=path, started_at=self._session.started_at,
                sample_rate=sample_rate, channels=channels, format=fmt,
            )
            return True, str(path)

    def write(self, raw: bytes, sample_rate: int, channels: int):
        """Called from the audio capture thread via the recording sink --
        must stay cheap (no disk I/O, no lock contention with the writer
        thread) and must never touch SessionLogger, see module docstring.
        """
        session = self._session
        if not self.state.active or session is None:
            return
        if sample_rate != session.sample_rate or channels != session.channels:
            # Output device/format changed mid-recording -- stop safely
            # instead of pushing mismatched-format bytes into the file.
            with self._lock:
                if self._session is session:  # still the active one, not stale
                    self.state = RecordingState()
                    self._session = None
            session.finish()
            self._pending_error = "o dispositivo de saída mudou de formato durante a gravação"
            return
        session.push(raw)

    def stop(self) -> "tuple[bool, str]":
        """Call from the main thread only. Non-blocking: flips to inactive
        immediately (the REC button reflects it right away), but the file
        isn't actually finished/closed until the session's own writer
        thread drains -- see pop_last_saved()."""
        with self._lock:
            if not self.state.active or self._session is None:
                return False, "não estava gravando"
            session = self._session
            path = self.state.path
            self._session = None
            self.state = RecordingState()
        session.finish()
        return True, str(path) if path else ""

    def _session_done(self, session: "_RecordingSession"):
        """Called from the session's own writer thread once the file is
        fully drained and closed."""
        try:
            size_bytes = session.path.stat().st_size if session.path.exists() else session.bytes_written
        except Exception:
            size_bytes = session.bytes_written
        saved = {
            "filename": session.path.name, "path": session.path,
            "started_at": session.started_at, "ended_at": time.time(),
            "format": session.fmt, "sample_rate": session.sample_rate, "channels": session.channels,
            "size_bytes": size_bytes,
        }
        if session._dropped_chunks:
            # Real but rare -- the disk was stuck long enough to fill the
            # ~25s write-behind buffer, so this take has a gap in it. Still
            # a successful save (most of it is fine), just flagged.
            saved["dropped_chunks"] = session._dropped_chunks
        with self._lock:
            self._last_saved = saved

    def _session_error(self, message: str):
        """Called from the session's own writer thread on a real write/close
        failure (e.g. the drive went away mid-recording)."""
        with self._lock:
            self._pending_error = message
