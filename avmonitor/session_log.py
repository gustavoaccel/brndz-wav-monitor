"""CSV logging + end-of-session Markdown summary.

Drive resolution mirrors the PowerShell prototype: look for a volume
labeled `drive_label` (e.g. "brndz.wav") and write under
`AV_TOOLKIT/07_DOCUMENTATION/Logs_Evento/` on it; if that drive isn't
plugged in, fall back to a local folder so the session still gets logged.

Events are structured (level/source/event/message) instead of plain
strings so the UI's 3-line LOG box and the CSV/Markdown reports can filter
and count meaningfully. Debounce lives at the call site (process_watch.py,
network_monitor.py etc. only ever emit on a state *transition*, never every
poll) -- this logger just records whatever it's handed.
"""
import csv
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from . import win_native

if TYPE_CHECKING:
    from .system_stats import StatsSnapshot
    from .network_monitor import NetworkSnapshot


CSV_HEADER = [
    "timestamp", "cpu_percent", "ram_percent", "ram_used_mb",
    "gpu_available", "gpu_util_percent", "gpu_vram_used_mb", "disks", "ping_targets",
]
EVENT_CSV_HEADER = ["timestamp", "level", "source", "event", "message"]
_SUMMARY_LEVELS = ("INFO", "ACTION", "OK", "WARNING", "ERROR", "CRASH", "HANG", "RECOVERY")


def resolve_log_dir(cfg) -> "tuple[Path, bool]":
    """Returns (directory, used_fallback)."""
    drive = win_native.find_drive_by_label(cfg.drive_label)
    if drive is not None:
        return drive / cfg.log_subdir, False
    return Path(cfg.fallback_log_dir), True


def _fmt_disks(disks) -> str:
    parts = []
    for d in disks:
        if d.error:
            parts.append(f"{d.path}:erro")
        else:
            parts.append(f"{d.path}:{d.free_gb:.1f}GB livres")
    return ";".join(parts)


def _fmt_targets(targets) -> str:
    parts = []
    for t in targets:
        if t.alive:
            parts.append(f"{t.host}:{t.latency_ms:.0f}ms({t.loss_percent:.0f}%loss)")
        else:
            parts.append(f"{t.host}:DOWN({t.loss_percent:.0f}%loss)")
    return ";".join(parts)


class SessionLogger:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_time = time.time()
        self.event_records: List[dict] = []
        self.recordings: List[dict] = []
        self._recent = deque(maxlen=3)
        self._last_targets = None

        self.log_dir, self.used_fallback = resolve_log_dir(cfg)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = self.log_dir / f"av_monitor_{stamp}.csv"
        self.events_csv_path = self.log_dir / f"av_monitor_{stamp}_eventos.csv"
        self.summary_path = self.log_dir / f"av_monitor_{stamp}_resumo.md"

        self._csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow(CSV_HEADER)

        self._events_file = open(self.events_csv_path, "w", newline="", encoding="utf-8")
        self._events_writer = csv.writer(self._events_file)
        self._events_writer.writerow(EVENT_CSV_HEADER)
        self._events_file.flush()

    def log_row(self, stats: Optional["StatsSnapshot"], network: Optional["NetworkSnapshot"]):
        if stats is None:
            return
        if network is not None:
            self._last_targets = network.targets

        self._writer.writerow([
            datetime.fromtimestamp(stats.timestamp).isoformat(timespec="seconds"),
            f"{stats.cpu_percent:.1f}",
            f"{stats.ram_percent:.1f}",
            f"{stats.ram_used_mb:.0f}",
            stats.gpu.available,
            f"{stats.gpu.util_percent:.1f}" if stats.gpu.available else "",
            f"{stats.gpu.vram_used_mb:.0f}" if stats.gpu.available else "",
            _fmt_disks(stats.disks),
            _fmt_targets(self._last_targets) if self._last_targets else "",
        ])
        self._csv_file.flush()

    def add_event(self, message: str, level: str = "INFO", source: str = "SYSTEM", event: str = "EVENT"):
        now = datetime.now()
        record = {
            "timestamp": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M:%S"),
            "level": str(level).upper(),
            "source": str(source).upper(),
            "event": str(event).upper(),
            "message": str(message),
        }
        self.event_records.append(record)
        self._recent.append(record)
        try:
            self._events_writer.writerow([record[k] for k in EVENT_CSV_HEADER])
            self._events_file.flush()
        except Exception:
            pass

    def add_events(self, messages: List[str], level: str = "INFO", source: str = "SYSTEM", event: str = "EVENT"):
        for m in messages:
            self.add_event(m, level=level, source=source, event=event)

    @property
    def recent_events(self) -> list:
        """Last 3 events, for the UI's LOG box -- a deque(maxlen=3) so this
        is O(1) regardless of how long the session runs, no scanning the
        full history every frame."""
        return list(self._recent)

    def add_recording(self, filename: str, start: float, end: float, fmt: str,
                       sample_rate: int, channels: int, size_bytes: int):
        self.recordings.append({
            "filename": filename,
            "start": datetime.fromtimestamp(start).isoformat(timespec="seconds"),
            "end": datetime.fromtimestamp(end).isoformat(timespec="seconds"),
            "duration_s": max(0.0, end - start),
            "format": fmt,
            "sample_rate": sample_rate,
            "channels": channels,
            "size_mb": size_bytes / (1024 * 1024),
        })

    def close(self) -> Path:
        try:
            self._csv_file.close()
        finally:
            try:
                self._events_file.close()
            except Exception:
                pass

        duration_s = time.time() - self.start_time
        hours, rem = divmod(int(duration_s), 3600)
        minutes, seconds = divmod(rem, 60)

        level_counts = {lvl: 0 for lvl in _SUMMARY_LEVELS}
        for e in self.event_records:
            if e["level"] in level_counts:
                level_counts[e["level"]] += 1

        lines = [
            "# brndz.wav Monitor — Resumo da Sessão",
            "",
            f"- Início: {datetime.fromtimestamp(self.start_time).isoformat(timespec='seconds')}",
            f"- Duração: {hours:02d}:{minutes:02d}:{seconds:02d}",
            f"- CSV métricas: `{self.csv_path}`",
            f"- CSV eventos: `{self.events_csv_path}`",
            f"- Drive `{self.cfg.drive_label}` {'NÃO encontrado (fallback local)' if self.used_fallback else 'detectado'}",
            "",
            "## Resumo dos eventos",
            "",
        ]
        for lvl in _SUMMARY_LEVELS:
            lines.append(f"- {lvl}: {level_counts[lvl]}")

        if self.recordings:
            lines += ["", "## Gravações", "", "| Arquivo | Início | Fim | Duração | Formato | Sample Rate | Canais | Tamanho |",
                      "|---|---|---|---|---|---|---|---|"]
            for r in self.recordings:
                dur_m, dur_s = divmod(int(r["duration_s"]), 60)
                lines.append(
                    f"| {r['filename']} | {r['start']} | {r['end']} | {dur_m:02d}:{dur_s:02d} | "
                    f"{r['format']} | {r['sample_rate']}Hz | {r['channels']} | {r['size_mb']:.1f}MB |"
                )

        lines += ["", "## Eventos", ""]
        if self.event_records:
            lines.extend(
                f"- [{e['timestamp']}] [{e['level']}] [{e['source']}] {e['message']}"
                for e in self.event_records
            )
        else:
            lines.append("- Nenhum evento registrado.")
        lines.append("")

        self.summary_path.write_text("\n".join(lines), encoding="utf-8")
        return self.summary_path
