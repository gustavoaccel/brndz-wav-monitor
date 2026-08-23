"""Audio INPUT monitor for the ÁUDIO I/O panel -- output reuses
audio_spectrum.py's own capture (see SpectrumFrame.output_*), this module
only owns the input/mic/interface side, independent of it.

Same architecture as audio_spectrum.py's output capture: a persistent
stream opened once and read continuously, not torn down and reopened every
poll -- and the same get_read_available() polling instead of a raw
blocking read, since a device with no active input session can hang a
blocking stream.read() forever (verified directly on the output side;
nothing suggests input behaves any differently).
"""
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .util import push_latest, PORTAUDIO_INIT_LOCK, DeviceWatcher
from . import win_native

_CHUNK = 2048  # ~43ms @ 48kHz => ~23 meter updates/sec, plenty smooth without polling harder than needed


def _apply_gain_int16(raw: bytes, gain: float) -> bytes:
    """Scales a raw int16 PCM buffer by a linear gain factor with a hard
    clip -- applied once, here, before both the meter math and the
    recording sink see the data, so the two always agree on what "the
    signal" is. Clipping (not wrapping) on overflow: a boosted transient
    that would have exceeded full scale flattens at it instead of
    wrapping around to a huge sample of the opposite sign, which would
    sound far worse than a clean clip.
    """
    if gain == 1.0:
        return raw
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    samples *= gain
    np.clip(samples, -32768.0, 32767.0, out=samples)
    return samples.astype(np.int16).tobytes()


def _live_default_input_name() -> Optional[str]:
    """The Windows default INPUT device's friendly name, straight from
    Core Audio (COM) via pycaw -- same fix as audio_spectrum.py's
    _live_default_output_name(), same reason: pyaudiowpatch's own
    get_default_input_device_info() is enumerated once when its PortAudio
    host API is initialized and keeps pointing at whatever was default at
    that moment, never noticing a later change of the Windows default mic
    (confirmed directly: pycaw reported a different live default than
    pyaudiowpatch's cached one on this exact machine).

    pycaw's GetMicrophone() -- unlike GetSpeakers() -- returns a raw
    IMMDevice with no .FriendlyName, so it needs wrapping through
    CreateDevice() to get one; that asymmetry looks like an omission in
    pycaw itself, not something this project can fix upstream.
    """
    try:
        from pycaw.pycaw import AudioUtilities
        device = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone())
        return device.FriendlyName if device is not None else None
    except Exception:
        return None


@dataclass
class AudioInputInfo:
    name: str = ""
    sample_rate: int = 0
    channels: int = 0
    connected: bool = False
    level_db: float = -60.0
    peak_db: float = -60.0
    clipping: bool = False
    error: Optional[str] = None


@dataclass
class AudioIOSnapshot:
    timestamp: float
    input: AudioInputInfo = field(default_factory=AudioInputInfo)
    events: List[str] = field(default_factory=list)


class AudioIOMonitor(threading.Thread):
    def __init__(self, cfg, out_queue):
        super().__init__(name="AudioIOMonitor", daemon=True)
        self.cfg = cfg
        self.out_queue = out_queue
        self._stop_event = threading.Event()
        self._last_device_name: Optional[str] = None
        self._clip_active = False
        self._clip_streak = 0
        self._no_data_active = False
        # Linear gain applied to every raw chunk before the meter or a
        # recording ever see it -- see Config.mic_boost_db. Computed once
        # here, not per-chunk.
        self._boost_gain = 10.0 ** (cfg.mic_boost_db / 20.0)
        # Same OS-priority-elevation-while-recording pattern as
        # AudioSpectrumAnalyzer -- see win_native.set_current_thread_priority().
        self._priority_raised = False
        # Recording hook: main.py points this at a *second*, independent
        # AudioRecorder's write() when mic-REC is active -- deliberately
        # separate from AudioSpectrumAnalyzer's own recording_sink (output),
        # so the two can run at the same time without mixing into one file.
        # Same plain-attribute pattern as AudioSpectrumAnalyzer's sink: the
        # recorder needs the exact bytes already read for the meter, not a
        # copy routed through another queue.
        self._recording_sink = None

    def stop(self):
        self._stop_event.set()

    def set_gain_db(self, gain_db: float):
        """Live-adjust the mic boost from the UI settings popup (main
        thread) -- a single float attribute reassignment, GIL-atomic, safe
        to call while the capture thread is reading `self._boost_gain` on
        every chunk without a lock (same pattern as DeviceWatcher.current).
        """
        self._boost_gain = 10.0 ** (gain_db / 20.0)

    def set_recording_sink(self, sink):
        """`sink(raw_bytes, sample_rate, channels)` or None to stop. Called
        from this thread for every block already read for the IN meter --
        zero extra capture. Exceptions in `sink` are swallowed so a broken
        recorder can never take down the input meter."""
        self._recording_sink = sink

    def _publish(self, info: AudioInputInfo, events=None):
        push_latest(self.out_queue, AudioIOSnapshot(timestamp=time.time(), input=info, events=events or []))

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            self._publish(AudioInputInfo(error=str(e)))
            return

        # WASAPI is COM-based under the hood; running it from a thread that
        # never initialized COM was observed to segfault the whole process
        # as soon as this thread's PyAudio() ran *concurrently* with
        # audio_spectrum.py's (which does CoInitialize) -- each COM-using
        # thread needs its own apartment, not just one somewhere in the
        # process. Verified: reproduced 2/2 without this, gone with it.
        com_initialized = False
        try:
            import comtypes
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        # See util.PORTAUDIO_INIT_LOCK's docstring: construction/teardown of
        # PyAudio() races with AudioSpectrumAnalyzer's if unguarded. Only the
        # instant of construction/teardown needs the lock, not the session.
        # Resolves "which input device is current" on its own thread/
        # PyAudio() instance -- confirmed in the field that doing this
        # inline, in the real-time capture loop's periodic recheck, could
        # take up to ~76ms against a ~43ms per-chunk time budget, and
        # cause real audible glitches in exactly what was being captured/
        # recorded (the mic recording). See util.DeviceWatcher's docstring.
        watcher = DeviceWatcher(lambda p, _pyaudio: self._find_input_device(p), self.cfg.audio_device_poll_s)
        watcher.start()
        deadline = time.time() + 2.0
        while watcher.current is None and time.time() < deadline and not self._stop_event.is_set():
            time.sleep(0.02)

        with PORTAUDIO_INIT_LOCK:
            p = pyaudio.PyAudio()
        try:
            while not self._stop_event.is_set():
                device = watcher.current
                if device is None:
                    events = []
                    if self._last_device_name is not None:
                        events.append(f"Áudio IN desconectado: {self._last_device_name}")
                        self._last_device_name = None
                    self._publish(AudioInputInfo(error="Nenhum dispositivo de entrada disponível"), events)
                    if self._stop_event.wait(self.cfg.audio_device_poll_s):
                        break
                    continue

                session_started_at = time.time()
                self._run_session(p, pyaudio, device, watcher)
                # A session that ends almost immediately after starting is a
                # sign of transient contention (WASAPI enumeration still
                # settling right at startup, GIL contention with the render
                # thread delaying get_read_available()) rather than a real
                # repeated device change -- pausing here before the outer
                # loop retries keeps "detectado"/"mudou" events a real
                # debounce interval apart instead of flapping back-to-back.
                if not self._stop_event.is_set() and time.time() - session_started_at < 0.5:
                    if self._stop_event.wait(self.cfg.audio_device_poll_s):
                        break
        except Exception as e:
            self._publish(AudioInputInfo(error=str(e)))
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

    def _find_input_device(self, p):
        """Picks the current default input device by matching pycaw's live
        Windows default against the enumerated device list -- not by
        trusting pyaudiowpatch's own get_default_input_device_info(),
        which was confirmed stuck on whatever was default when the host
        API was constructed (see _live_default_input_name()).

        Windows exposes the *same physical device* multiple times, once
        per audio API (MME, DirectSound, WASAPI) -- confirmed directly on
        a HyperX headset: the DirectSound entry opened without error but
        never delivered a single sample (get_read_available() stuck at 0
        indefinitely) while the WASAPI entry for the exact same microphone
        worked immediately. A name match alone (matching pycaw's live
        default name against *any* enumerated device) can land on
        whichever API happens to come first in enumeration order, which
        isn't necessarily the one that actually works for that driver.
        WASAPI is preferred here for the same reason it's already the only
        thing used for output/loopback in audio_spectrum.py: it's the
        modern, most broadly reliable Windows audio API. Falls through to
        any other API only if no WASAPI match exists for the live name.
        """
        try:
            devices = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
        except Exception:
            devices = []
        inputs = [d for d in devices if d.get("maxInputChannels", 0) > 0]

        try:
            import pyaudiowpatch as pyaudio
            wasapi_host_index = p.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except Exception:
            wasapi_host_index = None
        wasapi_inputs = [d for d in inputs if d.get("hostApi") == wasapi_host_index] if wasapi_host_index is not None else []

        live_name = _live_default_input_name()
        if live_name is not None:
            for d in wasapi_inputs:
                if live_name in d.get("name", ""):
                    return d
            for d in inputs:
                if live_name in d.get("name", ""):
                    return d
            # pycaw resolved a live name but it's not in pyaudiowpatch's
            # own input list at all (e.g. a WASAPI-only endpoint quirk) --
            # fall through to the stale-but-better-than-nothing path below
            # rather than reporting no device at all.

        if wasapi_inputs:
            try:
                idx = p.get_default_input_device_info()["index"]
                for d in wasapi_inputs:
                    if d["index"] == idx:
                        return d
            except Exception:
                pass
            return wasapi_inputs[0]

        try:
            idx = p.get_default_input_device_info()["index"]
            info = p.get_device_info_by_index(idx)
            return info if info.get("maxInputChannels", 0) > 0 else None
        except Exception:
            return inputs[0] if inputs else None

    def _run_session(self, p, pyaudio, device, watcher: DeviceWatcher):
        name = device.get("name") or "Entrada desconhecida"
        rate = int(device.get("defaultSampleRate") or 48000)
        channels = max(1, min(2, int(device.get("maxInputChannels") or 1)))

        events = []
        if self._last_device_name != name:
            events.append(
                f"Áudio IN detectado: {name}" if self._last_device_name is None
                else f"Áudio IN mudou: {self._last_device_name} → {name}"
            )
            self._last_device_name = name

        try:
            stream = p.open(
                format=pyaudio.paInt16, channels=channels, rate=rate,
                frames_per_buffer=_CHUNK, input=True, input_device_index=device["index"],
            )
        except Exception as e:
            self._publish(AudioInputInfo(name=name, error=str(e)), events)
            self._stop_event.wait(self.cfg.audio_device_poll_s)
            return

        next_device_check = time.time() + self.cfg.audio_device_poll_s
        first_publish = True
        try:
            while not self._stop_event.is_set():
                if time.time() >= next_device_check:
                    next_device_check = time.time() + self.cfg.audio_device_poll_s
                    # Instant read of DeviceWatcher's own thread, not a
                    # fresh enumeration call here -- see DeviceWatcher's
                    # docstring for why doing the real work inline used to
                    # risk stalling this loop long enough to glitch the mic
                    # capture/recording.
                    current = watcher.current
                    if current is None or current.get("name") != name:
                        return  # outer loop rediscovers/reopens

                result = self._read_with_timeout(stream, _CHUNK, timeout_s=2.5)
                if result is None:
                    if self._stop_event.is_set():
                        return
                    # Debounced like every other device event here: log
                    # once on the transition into "no data", not every
                    # ~2.5s retry -- a device that's silently gone inactive
                    # (headset asleep, capture endpoint stuck) was flooding
                    # the LOG box/CSV/beep with an identical line every
                    # cycle before this. The panel's own error state (not
                    # logged) still updates every cycle either way, so the
                    # UI itself never goes stale.
                    no_data_events = [] if self._no_data_active else ["Áudio IN sem dados (dispositivo pode estar inativo)"]
                    self._no_data_active = True
                    self._publish(AudioInputInfo(name=name, error="sem dados"), no_data_events)
                    # Back off before the outer loop retries -- without
                    # this, a device stuck with no data gets reopened
                    # immediately after every 2.5s read timeout, hammering
                    # open/close for no benefit.
                    self._stop_event.wait(self.cfg.audio_device_poll_s)
                    return

                raw, frames_read = result

                if self._boost_gain != 1.0:
                    raw = _apply_gain_int16(raw, self._boost_gain)

                # Same bytes handed to the mic recorder if mic-REC is
                # active -- no second capture, no second stream. Any
                # exception in the sink (e.g. a disk write failure) is the
                # recorder's problem, never the meter's.
                sink = self._recording_sink
                if (sink is not None) != self._priority_raised:
                    win_native.set_current_thread_priority(sink is not None)
                    self._priority_raised = sink is not None
                if sink is not None:
                    try:
                        sink(raw, rate, channels)
                    except Exception:
                        pass

                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
                peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
                over_threshold = peak >= self.cfg.audio_io_clip_threshold

                # A single over-threshold chunk (~43ms) is as likely to be a
                # stale/garbage buffer PortAudio handed back after a brief
                # overflow (exception_on_overflow=False silently returns
                # whatever was in the ring buffer instead of raising) as it
                # is real clipping -- reproduced in the field as a "clip"
                # flashing every ~30s on a muted USB mic with nothing
                # plugged into the room. Real clipping (someone actually too
                # loud) holds across consecutive chunks; a buffer glitch
                # doesn't. Requiring 2 in a row filters the glitch without
                # adding perceptible latency to a genuine clip indication.
                if over_threshold:
                    self._clip_streak += 1
                else:
                    self._clip_streak = 0
                clipping = self._clip_streak >= 2

                clip_events = list(events) if first_publish else []
                events = []
                first_publish = False
                if self._no_data_active:
                    clip_events.append(f"Áudio IN voltou a receber dados: {name}")
                    self._no_data_active = False
                if clipping and not self._clip_active:
                    clip_events.append(f"CLIP no áudio IN: {name}")
                self._clip_active = clipping

                self._publish(AudioInputInfo(
                    name=name, sample_rate=rate, channels=channels, connected=True,
                    level_db=max(-60.0, 20.0 * np.log10(max(rms, 1e-6))),
                    peak_db=max(-60.0, 20.0 * np.log10(max(peak, 1e-6))),
                    clipping=clipping,
                ), clip_events)
        finally:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def _read_with_timeout(self, stream, n_frames: int, timeout_s: float):
        """See the same-named method in audio_spectrum.py -- identical fix,
        identical reasoning: always drain everything buffered instead of a
        fixed `n_frames`, or a loop delayed past its time budget silently
        loses mic audio to the driver's ring buffer overflowing. Returns
        (raw_bytes, frame_count) or None on timeout/stop.
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
