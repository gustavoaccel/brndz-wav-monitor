"""Reads the system master output volume/mute via Windows Core Audio (COM).

WASAPI loopback capture taps the render mix *before* the master volume gain
stage -- Windows applies the OS volume slider as a separate, later step in
the audio engine. Without this, muting or lowering the master volume has
zero effect on what the loopback stream delivers (per-app volume mixing
still shows up fine, since that happens earlier in the pipeline, which is
why the meter reacted to a browser tab's own volume but not the master
slider). Reading it separately via `pycaw` and applying it as a manual
scale factor on the captured magnitude is the standard workaround.
"""
import time


class MasterVolumeReader:
    """Call `.refresh()` periodically (not every audio frame -- COM calls
    have real overhead) and read `.scale` for the current effective gain
    (0.0 when muted). Must be used from a thread where COM has been
    initialized (`comtypes.CoInitialize()`).
    """

    def __init__(self, refresh_interval_s: float = 0.3):
        self.scale = 1.0
        self.available = False
        self._interval = refresh_interval_s
        self._next_refresh = 0.0
        self._endpoint_volume = None

    def _connect(self):
        try:
            from pycaw.pycaw import AudioUtilities
            device = AudioUtilities.GetSpeakers()
            self._endpoint_volume = device.EndpointVolume
            self.available = True
        except Exception:
            self._endpoint_volume = None
            self.available = False

    def refresh(self):
        now = time.time()
        if now < self._next_refresh:
            return
        self._next_refresh = now + self._interval

        if self._endpoint_volume is None:
            self._connect()
            if self._endpoint_volume is None:
                return

        try:
            muted = self._endpoint_volume.GetMute()
            scalar = self._endpoint_volume.GetMasterVolumeLevelScalar()
            self.scale = 0.0 if muted else float(scalar)
        except Exception:
            self._endpoint_volume = None  # device changed/unplugged; reconnect next refresh
