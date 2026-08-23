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
    # 4096 @ 48kHz = ~85ms latency, still imperceptible for a visual meter,
    # and gives 2x the frequency resolution of 2048 (11.7Hz/bin vs 23.4Hz/bin)
    eq_fft_size: int = 4096
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

    window_width: int = 1280
    window_height: int = 800

    def to_dict(self) -> dict:
        return asdict(self)


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


def load_config(argv=None) -> Config:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cfg = Config()

    if args.config:
        cfg = _merge_json(cfg, Path(args.config))
    else:
        # Frozen mode: __file__ resolves inside the onefile temp extraction
        # dir, not next to the actual .exe -- a config.json placed beside
        # "brndz.wav Monitor.exe" would silently never be found otherwise.
        if getattr(sys, "frozen", False):
            default_path = Path(sys.executable).resolve().parent / "config.json"
        else:
            default_path = Path(__file__).resolve().parent.parent / "config.json"
        cfg = _merge_json(cfg, default_path)

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
