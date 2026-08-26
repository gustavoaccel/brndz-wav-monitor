"""Config loading: defaults <- JSON file <- CLI args (later wins)."""
import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List


@dataclass
class Config:
    ping_targets: List[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1"])
    ping_interval_s: float = 1.0
    ping_timeout_ms: int = 1000
    ping_window: int = 20  # how many recent pings count toward loss rate

    watched_processes: List[str] = field(default_factory=lambda: ["obs64", "vMix64"])
    ram_alert_mb: float = 3072.0

    disk_paths: List[str] = field(default_factory=lambda: ["C:\\"])

    stats_interval_s: float = 2.0
    top_n_processes: int = 9
    # Walking every process on the system for its RSS is the priciest thing
    # this app does; refreshed less often than the rest of the stats so it
    # doesn't compete with OBS/vMix/Resolume for CPU.
    top_processes_interval_s: float = 6.0
    process_check_interval_s: float = 2.0

    eq_bands: int = 24
    eq_fps: int = 60
    # 2048 @ 48kHz = ~43ms latency (was 4096/~85ms) -- halved after the
    # user flagged the EQ/VU/LUFS all feeling noticeably behind the real
    # audio during live monitoring. Frequency resolution drops to
    # 23.4Hz/bin (was 11.7Hz/bin), which mostly costs precision at the
    # very bottom of a 24-band log-spaced *visual* display, not
    # perceptible there. This is the real latency floor everything else
    # (OUT VU, LUFS) inherits, since they all read off the same capture
    # chunk -- reducing it here is the actual fix, not just smoothing
    # tricks downstream.
    eq_fft_size: int = 2048
    eq_min_freq: float = 40.0
    eq_max_freq: float = 16000.0
    # dBFS-calibrated: 0dB is true digital full scale (see the amplitude
    # correction in audio_spectrum.py), so the top of the meter and the
    # clip threshold both mean something real instead of a tuned heuristic.
    eq_floor_db: float = -60.0
    eq_ceil_db: float = 0.0
    eq_clip_threshold: float = 0.97
    eq_peak_hold_s: float = 1.2
    # How often to check whether the Windows default output device changed
    # (e.g. speakers -> headset) and reopen the loopback stream if so.
    audio_device_poll_s: float = 1.5
    audio_io_poll_s: float = 1.0
    audio_io_clip_threshold: float = 0.98
    # Mic input gain applied in software, in dB, before both the IN
    # meter and any mic recording -- some interfaces/headset mics
    # deliver a genuinely quiet raw signal (confirmed directly: -54.8dB
    # RMS / -37.2dB peak on a real recording, with the Windows input
    # volume already at 94%, not muted -- the raw capture is just quiet
    # at the source). Applied with a hard clip so a loud moment can never
    # wrap/overflow instead of just flattening at full scale.
    #
    # Deviates from the original spec's "no processing" rule for
    # recordings (no normalize/compressor/limiter/EQ) -- explicitly
    # requested by the user after hearing a too-quiet mic recording.
    # +20dB (the first value tried) was reported too loud on real
    # hardware, so the default was lowered to +12dB and a "GANHO DO MIC"
    # -/+ stepper was added to the ÁUDIO I/O settings popup (0-30dB, 2dB
    # steps) so the user can tune it live instead of editing config.json.
    mic_boost_db: float = 12.0
    # Same idea as mic_boost_db, mirrored for OUT -- applied before the
    # recording sink and the meter/FFT/LUFS so they all agree on the
    # real level. Default 0dB (no boost): unlike the mic, OUT already
    # gets master_volume.scale applied, so this is only for the rare
    # case of needing extra headroom on top of that. -/+ stepper in the
    # same settings popup as the mic gain.
    out_boost_db: float = 0.0

    recording_enabled: bool = True
    # Empty = auto: same brndz.wav-drive-then-fallback resolution as
    # session_log.py, resolved at record-start time (not here) so it always
    # reflects whatever drive is actually plugged in right now. Set this to
    # override with a fixed path instead.
    recording_directory: str = ""
    recording_format: str = "wav"
    recording_sample_rate: int = 48000
    recording_channels: int = 2
    recording_bit_depth: int = 16

    compact_width: int = 420
    compact_height: int = 150

    drive_label: str = "brndz.wav"
    log_subdir: str = "AV_TOOLKIT/07_DOCUMENTATION/Logs_Evento"
    fallback_log_dir: str = str(Path.home() / "Documents" / "AV_Monitor_Logs")
    # Empty = auto (drive_label -> fallback_log_dir, same as before). Set
    # via the settings popup's "PASTA DE LOGS" browse button, which also
    # persists it to config.json -- unlike the other settings-popup
    # toggles (session-only), this one has to survive to the *next*
    # launch to be useful at all, since SessionLogger opens its files once
    # at startup and can't be redirected mid-session.
    log_directory_override: str = ""

    # LUFS target: a loudness goal the user sets for the event ("I want
    # to hit -14 tonight"), not an alert -- once the reading reaches/
    # passes it, only the part of the LUFS meter's fill above that line
    # switches to a brighter tone. Separate from the fixed reference
    # ticks (real broadcast/streaming targets, never move) and from the
    # meter's own fixed -9 LUFS ceiling, where the *entire* fill always
    # goes solid red regardless of this value (past that point, almost
    # certainly compressed/limited). Must stay quieter than -9 for the
    # target zone between the two to actually show up -- -14 (already a
    # real streaming reference) is a sensible default. Adjustable live
    # via the -/+ stepper in the meter.
    lufs_alert_threshold: float = -14.0

    window_width: int = 1280
    window_height: int = 800

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path) -> None:
        try:
            path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _merge_json(cfg: Config, path: Path) -> Config:
    if not path.exists():
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AV Monitor - monitoramento de audiovisual ao vivo")
    p.add_argument("--config", type=str, default=None, help="Caminho para arquivo .json de config")
    p.add_argument("--ping-target", action="append", dest="ping_targets", help="Alvo de ping (pode repetir)")
    p.add_argument("--process", action="append", dest="watched_processes", help="Nome de processo a monitorar (sem .exe, pode repetir)")
    p.add_argument("--ram-alert-mb", type=float, default=None)
    p.add_argument("--disk", action="append", dest="disk_paths", help="Caminho de drive a monitorar espaço livre (pode repetir)")
    p.add_argument("--eq-bands", type=int, default=None)
    p.add_argument("--eq-fps", type=int, default=None)
    p.add_argument("--stats-interval", type=float, default=None, dest="stats_interval_s")
    p.add_argument("--drive-label", type=str, default=None)
    return p


def default_config_path() -> Path:
    # Frozen mode: __file__ resolves inside the onefile temp extraction
    # dir, not next to the actual .exe -- a config.json placed beside
    # "brndz.wav Monitor.exe" would silently never be found otherwise.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent.parent / "config.json"


def load_config(argv=None) -> Config:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cfg = Config()

    if args.config:
        cfg = _merge_json(cfg, Path(args.config))
    else:
        cfg = _merge_json(cfg, default_config_path())

    overrides = {
        "ping_targets": args.ping_targets,
        "watched_processes": args.watched_processes,
        "ram_alert_mb": args.ram_alert_mb,
        "disk_paths": args.disk_paths,
        "eq_bands": args.eq_bands,
        "eq_fps": args.eq_fps,
        "stats_interval_s": args.stats_interval_s,
        "drive_label": args.drive_label,
    }
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)

    return cfg
