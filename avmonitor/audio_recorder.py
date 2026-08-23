"""Streams the output loopback's raw PCM straight to a WAV or MP3 file on
disk.

No second capture: AudioSpectrumAnalyzer hands this the exact same block
it already read for the spectrum/meter (via set_recording_sink), and this
writes it to disk immediately -- never buffers a session in RAM. Idle
cost is exactly zero: the sink is None and the capture thread does
nothing extra until REC is pressed.

WAV via the stdlib `wave` module (no dependency). MP3 via `lameenc` -- a
compiled LAME wheel with no external binary/DLL to find on PATH, small
enough (~150KB installed) to not violate the "no heavy encoder" rule that
kept MP3 out of earlier rounds; both writers satisfy the same
write(raw_bytes)/close() shape so AudioRecorder itself doesn't care which
one it's holding.

Threading note: `write()` runs on the audio capture thread (it's called
directly from the sink), while `start()`/`stop()` run on the main/UI
thread (button clicks). Both go through `_lock`, so they can't race each
other -- but `write()` must never touch the SessionLogger directly, since
that object isn't safe for concurrent access from two threads at once.
Errors raised on the audio thread are stashed in `pending_error` instead;
main.py polls `pop_pending_error()` once a frame and logs from there.
"""
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


class AudioRecorder:
    def __init__(self, cfg, filename_prefix: str = "BRNDZ"):
        self.cfg = cfg
        self._filename_prefix = filename_prefix
        self._lock = threading.Lock()
        self._writer = None
        self.state = RecordingState()
        self._pending_error: Optional[str] = None
        self._last_saved: Optional[dict] = None  # for the main thread to log + report

    def is_active(self) -> bool:
        return self.state.active

    def pop_pending_error(self) -> Optional[str]:
        """Main-thread poll: any error that stopped a recording from the
        audio thread (format mismatch, disk write failure). Clears it."""
        with self._lock:
            err, self._pending_error = self._pending_error, None
            return err

    def pop_last_saved(self) -> Optional[dict]:
        """Main-thread poll: details of the most recent successful/auto
        stop, for SessionLogger.add_recording(). Clears it."""
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

            self._writer = writer
            self.state = RecordingState(
                active=True, filename=path.name, path=path, started_at=time.time(),
                sample_rate=sample_rate, channels=channels, format=fmt,
            )
            return True, str(path)

    def write(self, raw: bytes, sample_rate: int, channels: int):
        """Called from the audio capture thread via AudioSpectrumAnalyzer's
        recording sink. Must never touch SessionLogger -- see module
        docstring."""
        with self._lock:
            if not self.state.active or self._writer is None:
                return
            if sample_rate != self.state.sample_rate or channels != self.state.channels:
                # Output device/format changed mid-recording -- stop safely
                # instead of writing mismatched-format bytes into the file.
                self._finish_locked(error="o dispositivo de saída mudou de formato durante a gravação")
                return
            try:
                self._writer.write(raw)
                self.state.bytes_written += len(raw)
            except Exception as e:
                self._finish_locked(error=str(e))

    def stop(self) -> "tuple[bool, str]":
        """Call from the main thread only."""
        with self._lock:
            if not self.state.active:
                return False, "não estava gravando"
            return self._finish_locked()

    def _finish_locked(self, error: Optional[str] = None) -> "tuple[bool, str]":
        """Caller must already hold `_lock`."""
        path, sample_rate, channels = self.state.path, self.state.sample_rate, self.state.channels
        bytes_written, started_at, fmt = self.state.bytes_written, self.state.started_at, self.state.format

        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self.state = RecordingState()

        if error:
            self._pending_error = error
            return False, error

        if path is not None:
            self._last_saved = {
                "filename": path.name, "path": path, "started_at": started_at, "ended_at": time.time(),
                "format": fmt, "sample_rate": sample_rate, "channels": channels,
                # MP3's actual on-disk size is the encoded file, not the raw
                # PCM byte count fed into the encoder -- stat the real file
                # instead of trusting bytes_written for the report/CSV.
                "size_bytes": path.stat().st_size if path.exists() else bytes_written,
            }
        return True, str(path) if path else ""
