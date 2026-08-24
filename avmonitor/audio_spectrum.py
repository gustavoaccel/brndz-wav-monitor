"""Real-time system-audio spectrum analyzer.

Captures WASAPI loopback (what's playing out the speakers, not the mic) via
pyaudiowpatch, runs a windowed FFT per block, and buckets the result into
log-spaced bands like a graphic EQ. Runs entirely on its own thread; the UI
thread only ever reads the latest published SpectrumFrame.
"""
import threading
import time
import queue
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

from .master_volume import MasterVolumeReader
from .util import push_latest, PORTAUDIO_INIT_LOCK, DeviceWatcher
from .lufs import MomentaryLufsMeter
from . import win_native


def _live_default_output_name() -> Optional[str]:
    """The Windows default output device's friendly name, straight from
    Core Audio (COM) via pycaw -- used instead of trusting pyaudiowpatch's
    own notion of "default device", which is enumerated once when its
    PortAudio host API is initialized and was observed to keep pointing at
    whatever was default at that moment even after the user changed the
    Windows default output afterwards.
    """
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().FriendlyName
    except Exception:
        return None


def list_output_devices() -> "List[Tuple[str, str]]":
    """(display_name, loopback_name) for every loopback-capable output
    device, for the device-picker UI. `loopback_name` is what
    AudioSpectrumAnalyzer.set_device_override() expects.
    """
    try:
        import pyaudiowpatch as pyaudio
    except Exception:
        return []

    devices = []
    try:
        # Called from the main thread (startup + every dropdown open) while
        # AudioSpectrumAnalyzer/AudioIOMonitor's own PyAudio() instances live
        # on their own threads for the whole session -- construction/teardown
        # still needs the shared lock even though this one is short-lived.
        # See util.PORTAUDIO_INIT_LOCK's docstring.
        with PORTAUDIO_INIT_LOCK:
            p = pyaudio.PyAudio()
        try:
            seen = set()
            for loopback in p.get_loopback_device_info_generator():
                name = loopback["name"]
                if name in seen:
                    continue
                seen.add(name)
                display = name.replace(" [Loopback]", "")
                devices.append((display, name))
        finally:
            with PORTAUDIO_INIT_LOCK:
                try:
                    p.terminate()
                except Exception:
                    pass
    except Exception:
        return []
    return devices


@dataclass
class SpectrumFrame:
    bands: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    available: bool = False
    error: Optional[str] = None
    peak_freq_hz: Optional[float] = None
    peak_db: Optional[float] = None
    device_name: Optional[str] = None
    # RMS/peak of the same block already captured for the FFT -- the
    # ÁUDIO I/O panel's OUT meter reads these instead of opening a second
    # loopback capture of the same device.
    output_level_db: Optional[float] = None
    output_peak_db: Optional[float] = None
    output_lufs: Optional[float] = None


def log_band_edges(n_bands: int, fmin: float, fmax: float) -> np.ndarray:
    """n_bands+1 log-spaced frequency edges from fmin to fmax.

    Public so the UI can precompute band center frequencies for axis labels
    without waiting on a live audio frame (same formula, cfg-driven).
    """
    return np.geomspace(fmin, fmax, n_bands + 1)


class AudioSpectrumAnalyzer(threading.Thread):
    def __init__(self, cfg, out_queue: "queue.Queue[SpectrumFrame]"):
        super().__init__(name="AudioSpectrumAnalyzer", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self._stop_event = threading.Event()

        self.fft_size = cfg.eq_fft_size
        self._window = np.hanning(self.fft_size).astype(np.float32)
        # Amplitude correction for a windowed FFT: dividing by sum(window)
        # instead of fft_size (and doubling for the one-sided rfft output)
        # makes a full-scale sinusoid read ~0dBFS, so the dB scale is
        # actually calibrated instead of an arbitrary heuristic.
        self._amp_scale = 2.0 / float(np.sum(self._window))
        # Scratch buffers reused every frame to avoid per-frame allocation.
        self._mono_buf = np.empty(self.fft_size, dtype=np.float32)
        self._band_out = np.zeros(cfg.eq_bands, dtype=np.float32)

        self._floor_db = cfg.eq_floor_db
        self._ceil_db = cfg.eq_ceil_db

        # None = follow the Windows default output device automatically;
        # otherwise a loopback device name from list_output_devices(),
        # picked explicitly by the user in the UI. Plain attribute assignment
        # is fine across threads here (single reference, GIL-atomic) --
        # read once per poll cycle in _find_loopback_device.
        self._device_override: Optional[str] = None

        # Recording hook: main.py points this at AudioRecorder.write() when
        # REC is active (None otherwise). Deliberately just a plain
        # attribute (GIL-atomic single-reference swap, same reasoning as
        # _device_override) rather than a queue -- the recorder needs the
        # *exact* same bytes already read for the FFT, not a copy routed
        # through another queue, so it can never fall behind or duplicate
        # capture of the same device.
        self._recording_sink = None
        self.current_sample_rate = 0
        self.current_channels = 0
        # Tracks whether this thread's OS priority is currently raised --
        # compared against `self._recording_sink is not None` once per
        # capture-loop iteration so the syscall only fires on a genuine
        # start/stop transition, not every chunk. See
        # win_native.set_current_thread_priority()'s docstring.
        self._priority_raised = False
        self._lufs_meter = MomentaryLufsMeter()

    def stop(self):
        self._stop_event.set()

    def set_device_override(self, loopback_name: Optional[str]):
        """None to go back to following the Windows default; otherwise a
        loopback device name from list_output_devices(). Takes effect on
        the next device-poll cycle (audio_device_poll_s), not instantly --
        that's fine, it's a deliberate user choice, not a live meter value.
        """
        self._device_override = loopback_name

    def set_recording_sink(self, sink):
        """`sink(raw_bytes, sample_rate, channels)` or None to stop. Called
        from this thread for every block already read for the spectrum --
        zero extra capture, zero extra stream. Exceptions in `sink` are
        swallowed so a broken recorder can never take down audio capture.
        """
        self._recording_sink = sink

    def _publish_error(self, message: str):
        push_latest(self.out_queue, SpectrumFrame(available=False, error=message))

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            self._publish_error(f"pyaudiowpatch indisponível: {e}")
            return

        com_initialized = False
        try:
            import comtypes
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass  # master-volume reading degrades gracefully without it

        master_volume = MasterVolumeReader()

        # Resolves "which device is current" on its own thread/PyAudio()
        # instance -- confirmed in the field that doing this inline, in
        # the real-time capture loop's periodic recheck, could take up to
        # ~86ms (more than a whole chunk's ~85ms time budget) and cause
        # real audible glitches in exactly what was being captured/
        # recorded. See util.DeviceWatcher's docstring.
        watcher = DeviceWatcher(self._find_loopback_device, self.cfg.audio_device_poll_s)
        watcher.start()
        deadline = time.time() + 2.0
        while watcher.current is None and time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.02)

        # Construction (and, symmetrically, termination) of PyAudio() is not
        # safe to run concurrently with AudioIOMonitor's -- see
        # util.PORTAUDIO_INIT_LOCK's docstring. Only the moment of
        # construction/teardown needs the lock, not the whole session.
        with PORTAUDIO_INIT_LOCK:
            p = pyaudio.PyAudio()
        try:
            device = watcher.current
            if device is None:
                self._publish_error("Nenhum dispositivo de loopback WASAPI encontrado")

            # Outer loop: one iteration per audio session against a given
            # device. _open_and_capture returns normally (never raises)
            # when it detects the Windows default output device changed
            # (e.g. speakers -> headset) or hits a read error, so we
            # land back here and pick whatever the current default is.
            while not self._stop_event.is_set():
                if device is None:
                    if self._stop_event.wait(self.cfg.audio_device_poll_s):
                        break
                    device = watcher.current
                    continue

                device = self._open_and_capture(p, pyaudio, device, master_volume, watcher)
        except Exception as e:
            self._publish_error(f"Erro no loopback de áudio: {e}")
        finally:
            watcher.stop()
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

    def _open_and_capture(self, p, pyaudio, device, master_volume: MasterVolumeReader, watcher: DeviceWatcher):
        """Opens a loopback stream for `device` and captures from it until
        the default output device changes, a read fails, or we're told to
        stop. Always closes the stream it opened (no handle leak on device
        switch) and returns the *new* current default device (or None), so
        the caller's loop knows what to try next -- never raises.
        """
        try:
            sample_rate = int(device["defaultSampleRate"])
            channels = max(1, min(2, device["maxInputChannels"]))
            bin_indices = self._precompute_band_bins(sample_rate)
            freq_per_bin = sample_rate / self.fft_size
            n_fft_bins = self.fft_size // 2 + 1
            peak_lo = max(1, int(self.cfg.eq_min_freq / freq_per_bin))
            peak_hi = min(n_fft_bins, int(min(self.cfg.eq_max_freq, sample_rate / 2) / freq_per_bin))

            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                frames_per_buffer=self.fft_size,
                input=True,
                input_device_index=device["index"],
            )
        except Exception as e:
            self._publish_error(f"Erro abrindo dispositivo de áudio: {e}")
            self._stop_event.wait(self.cfg.audio_device_poll_s)
            return watcher.current

        try:
            return self._capture_loop(p, pyaudio, stream, device, channels, bin_indices, freq_per_bin, peak_lo, peak_hi, master_volume, watcher)
        finally:
            stream.stop_stream()
            stream.close()
            self.current_sample_rate = 0
            self.current_channels = 0

    def _find_loopback_device(self, p, pyaudio):
        try:
            loopbacks = list(p.get_loopback_device_info_generator())
        except Exception:
            return None

        override = self._device_override
        if override is not None:
            for loopback in loopbacks:
                if loopback["name"] == override:
                    return loopback
            self._publish_error(f"Dispositivo selecionado não encontrado: {override}")
            return None

        live_name = _live_default_output_name()
        if live_name is not None:
            for loopback in loopbacks:
                if live_name in loopback["name"]:
                    return loopback

        # pycaw unavailable for some reason -- fall back to pyaudiowpatch's
        # own idea of the default device (may lag behind if the user swaps
        # outputs mid-session, but better than nothing).
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output_idx = wasapi_info.get("defaultOutputDevice", -1)
            if default_output_idx >= 0:
                default_speakers = p.get_device_info_by_index(default_output_idx)
                if default_speakers.get("isLoopbackDevice"):
                    return default_speakers
                for loopback in loopbacks:
                    if default_speakers["name"] in loopback["name"]:
                        return loopback
        except Exception:
            pass

        return None

    def _precompute_band_bins(self, sample_rate: int):
        fmax = min(self.cfg.eq_max_freq, sample_rate / 2)
        edges = log_band_edges(self.cfg.eq_bands, self.cfg.eq_min_freq, fmax)
        freq_per_bin = sample_rate / self.fft_size
        n_fft_bins = self.fft_size // 2 + 1

        bins = []
        for i in range(self.cfg.eq_bands):
            start = int(edges[i] / freq_per_bin)
            end = int(edges[i + 1] / freq_per_bin)
            start = max(0, min(start, n_fft_bins - 1))
            end = max(start + 1, min(end, n_fft_bins))
            bins.append((start, end))
        return bins

    def _read_with_timeout(self, stream, n_frames: int, timeout_s: float):
        """`stream.read()` has no native timeout and was observed to block
        *forever* against a loopback device with no active render session
        (a real device on this machine: a headset's "Chat" output endpoint
        that nothing was actively playing through) -- polling
        get_read_available() instead lets us give up and hand control back
        instead of hanging the capture thread indefinitely.

        Always reads *everything* currently buffered, never just a fixed
        `n_frames` -- confirmed directly (wall-clock session time vs. a
        real recording's declared WAV duration) that reading a fixed chunk
        size silently loses audio: if this loop is delayed past one
        chunk's time budget for any reason (GIL contention with the 60fps
        render thread, WMI/COM calls on other threads, real system load),
        more than `n_frames` piles up in the driver's own ring buffer:
        reading only `n_frames` back then leaves the surplus behind, and
        if it isn't drained before the next overflow the driver drops it
        with no exception and no audible glitch -- the recording just
        silently skips ahead in time. Returns (raw_bytes, frame_count) or
        None on timeout/stop.
        """
        deadline = time.time() + timeout_s
        while True:
            available = stream.get_read_available()
            if available >= n_frames:
                break
            if self._stop_event.is_set() or time.time() >= deadline:
                return None
            time.sleep(0.01)
        return stream.read(available, exception_on_overflow=False), available

    def _capture_loop(self, p, pyaudio, stream, device, channels: int, bin_indices, freq_per_bin: float,
                       peak_lo: int, peak_hi: int, master_volume: MasterVolumeReader, watcher: DeviceWatcher):
        device_name = device["name"]
        sample_rate = int(device["defaultSampleRate"])
        self.current_sample_rate = sample_rate
        self.current_channels = channels
        next_device_check = time.time() + self.cfg.audio_device_poll_s

        while not self._stop_event.is_set():
            if time.time() >= next_device_check:
                next_device_check = time.time() + self.cfg.audio_device_poll_s
                # Instant read of DeviceWatcher's own thread, not a fresh
                # enumeration call here -- see DeviceWatcher's docstring
                # for why doing the real work inline used to risk stalling
                # this loop long enough to glitch whatever's capturing.
                current = watcher.current
                if current is None or current["name"] != device_name:
                    return current  # tell the caller to reopen against the new default

            master_volume.refresh()
            try:
                result = self._read_with_timeout(stream, self.fft_size, timeout_s=3.0)
            except Exception as e:
                self._publish_error(f"Erro lendo stream de áudio: {e}")
                # Don't retry reads on a stream that just failed -- most
                # likely the device it was opened against just disappeared
                # (unplugged/switched). Hand back to the caller to rediscover
                # the current default and open a fresh stream against it.
                return self._find_loopback_device(p, pyaudio)

            if result is None:
                if self._stop_event.is_set():
                    return None
                # No data for 3s straight -- most likely this device has no
                # active audio session at all (observed directly: a headset
                # "Chat" endpoint with nothing routed to it just never
                # produces frames). Report it and let the caller re-resolve
                # rather than sit here blocked forever.
                self._publish_error(f'Sem áudio de "{device_name}" (dispositivo pode estar inativo)')
                return self._find_loopback_device(p, pyaudio)

            raw, frames_read = result

            # Same bytes handed to a recorder if REC is active -- the whole
            # block, however large, so a delayed loop draining a backlog
            # never drops audio from the file. No second capture, no second
            # stream. Any exception in the sink (e.g. a disk write failure)
            # is the recorder's problem, never ours.
            sink = self._recording_sink
            if (sink is not None) != self._priority_raised:
                win_native.set_current_thread_priority(sink is not None)
                self._priority_raised = sink is not None
            if sink is not None:
                try:
                    sink(raw, sample_rate, channels)
                except Exception:
                    pass

            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)

            # The meter/FFT only need the most recent fft_size samples --
            # when a delayed loop reads a bigger backlog than usual, using
            # the tail (freshest audio) instead of the head keeps the live
            # visual in sync with "now" instead of falling behind.
            n = min(len(samples), self.fft_size)
            self._mono_buf[:n] = samples[-n:]
            if n < self.fft_size:
                self._mono_buf[n:] = 0.0

            # Cheap RMS/peak straight off the same block, scaled by the same
            # master-volume factor as the spectrum -- the ÁUDIO I/O panel's
            # OUT meter, no FFT involved.
            scaled = self._mono_buf[:n] * master_volume.scale
            out_rms = float(np.sqrt(np.mean(np.square(scaled)))) if n else 0.0
            out_peak = float(np.max(np.abs(scaled))) if n else 0.0
            output_level_db = 20.0 * np.log10(max(out_rms, 1e-6))
            output_peak_db = 20.0 * np.log10(max(out_peak, 1e-6))

            # Same raw block, no second capture -- see lufs.py.
            output_lufs = self._lufs_meter.update(
                np.frombuffer(raw, dtype=np.int16), channels, sample_rate,
            )

            windowed = self._mono_buf * self._window
            spectrum = np.fft.rfft(windowed)
            # Scaled by the actual OS master volume (see master_volume.py) so
            # the meter reflects what's really coming out of the speakers,
            # not just the pre-master-gain mix that loopback taps by default.
            magnitude = np.abs(spectrum) * (self._amp_scale * master_volume.scale)

            for i, (start, end) in enumerate(bin_indices):
                band_mag = magnitude[start:end].mean() if end > start else 0.0
                db = 20.0 * np.log10(band_mag + 1e-9)
                norm = (db - self._floor_db) / (self._ceil_db - self._floor_db)
                self._band_out[i] = float(np.clip(norm, 0.0, 1.0))

            # Dominant frequency: exact FFT-bin peak within the analyzed
            # range (not the coarser log-band aggregate), so "what note/
            # frequency is playing right now" reads precisely instead of
            # just which wide band lit up.
            peak_freq_hz = None
            peak_db = None
            if peak_hi > peak_lo:
                peak_bin = peak_lo + int(np.argmax(magnitude[peak_lo:peak_hi]))
                peak_mag = magnitude[peak_bin]
                peak_db = 20.0 * np.log10(peak_mag + 1e-9)
                if peak_db > self._floor_db + 8:  # ignore near-silence noise bins
                    peak_freq_hz = peak_bin * freq_per_bin

            push_latest(self.out_queue, SpectrumFrame(
                bands=self._band_out.copy(), available=True,
                peak_freq_hz=peak_freq_hz, peak_db=peak_db, device_name=device_name,
                output_level_db=output_level_db, output_peak_db=output_peak_db,
                output_lufs=output_lufs,
            ))
