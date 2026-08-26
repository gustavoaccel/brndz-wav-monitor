"""pygame dark-theme UI: big spectrum analyzer + resizable stats strip.

The spectrum panel is drawn like a hardware LED meter ladder (segmented
rows colored dark wine->gold->orange->red by position, not by instantaneous
level -- that's what makes a real meter's ladder read at a glance), with a
peak-hold cap per band, a per-band clip LED, a dB scale on the left,
frequency labels along the bottom, and a translucent "brndz.wav" watermark
sitting behind the bars.

Layout is proportional (fractions of window size), recomputed every frame
from the current surface size, so resizing the window reflows every panel
-- not just the spectrum. The boundary between the stats strip and the EQ,
and the boundaries between the four stats columns, are drag handles.
"""
import math
import sys
import time
from pathlib import Path

import numpy as np
import pygame

from . import theme
from ..audio_spectrum import log_band_edges


def _watermark_font_path():
    """First .ttf/.otf found in ui/fonts/, if any -- drop a custom display
    face there (e.g. Hunters.otf) and the watermark picks it up
    automatically, no filename to hardcode. Works from source and from the
    frozen exe (see brndzwav_monitor.spec's datas entry for this folder).
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    fonts_dir = base / "avmonitor" / "ui" / "fonts"
    if not fonts_dir.is_dir():
        return None
    candidates = sorted(fonts_dir.glob("*.otf")) + sorted(fonts_dir.glob("*.ttf"))
    return candidates[0] if candidates else None

_COLOR_STOPS = [
    (0.0, theme.BAR_LOW),
    (0.5, theme.BAR_MID),
    (0.8, theme.BAR_HIGH),
    (1.0, theme.BAR_CLIP),
]

# Alternate EQ palettes, cycled by the "C" button -- available on both the
# main panel and compact mode, one shared choice (Renderer.eq_color_theme)
# so switching between the two views never shows a different color.
_EQ_THEME_ORDER = ["quente", "frio", "medio", "brndz", "neon", "cyberpunk", "radioativo"]
_EQ_COLOR_STOPS = {
    "quente": _COLOR_STOPS,  # unchanged default -- dark wine -> gold -> orange -> red
    "frio": [
        (0.0, (18, 42, 66)),
        (0.5, (34, 132, 176)),
        (0.8, (46, 184, 150)),
        (1.0, (94, 232, 150)),
    ],
    "medio": [
        (0.0, (44, 24, 68)),
        (0.5, (108, 58, 168)),
        (0.8, (158, 78, 198)),
        (1.0, (208, 108, 220)),
    ],
    # More aggressive than the other palettes on purpose (user's explicit
    # ask): red shows up early instead of staying brown until near the
    # top -- brown base -> crimson/wine by 30% -> scarlet at the peak.
    "brndz": [
        (0.0, (46, 22, 18)),
        (0.3, (140, 32, 38)),
        (0.65, (185, 40, 30)),
        (1.0, (215, 60, 20)),
    ],
    # Neon green -> moss yellow, with a hot-pink accent at the very top --
    # explicitly requested as a more vivid/neon option than the other 4,
    # which all stay fairly dark/moody even at full brightness.
    "neon": [
        (0.0, (24, 46, 18)),
        (0.5, (70, 235, 55)),
        (0.8, (175, 220, 40)),
        (1.0, (255, 55, 180)),
    ],
    # Cyberpunk pink -- deep purple-blue base into hot magenta, capped with
    # an electric cyan accent at the peak (the classic magenta+cyan pairing
    # that reads as "cyberpunk"/synthwave rather than just "pink").
    "cyberpunk": [
        (0.0, (32, 10, 46)),
        (0.5, (220, 20, 140)),
        (0.8, (255, 55, 195)),
        (1.0, (60, 235, 255)),
    ],
    # Radioactive moss yellow-green transitioning into orange-red at the top.
    "radioativo": [
        (0.0, (36, 42, 8)),
        (0.5, (190, 230, 20)),
        (0.8, (235, 140, 30)),
        (1.0, (235, 60, 25)),
    ],
}

# Background/panel colors per EQ theme -- (bg, panel_bg, panel_border).
# "quente" is the original fixed palette, unchanged. Most of the rest
# lean the dark background toward that theme's own hue instead of one
# one-size-fits-all warm brown, so a full theme switch reads as "the
# whole room changed," not just the bar colors. "brndz" is the one
# genuinely *light* theme in the set (see sync_theme_module(), which also
# swaps TEXT/TEXT_DIM/TEXT_LABEL/GOLD to dark-on-light for it).
# Populated lazily on first use by Renderer.sync_theme_module() with the
# original dark-mode TEXT/TEXT_DIM/TEXT_LABEL/GOLD values, so switching
# away from "brndz" (the one light theme) can restore them exactly.
_DARK_TEXT_DEFAULTS = None

_EQ_THEME_BG = {
    "quente": (theme.BG, theme.PANEL_BG, theme.PANEL_BORDER),
    "frio": ((14, 18, 24), (21, 27, 35), (40, 55, 68)),
    "medio": ((19, 15, 24), (29, 23, 35), (56, 43, 66)),
    # Dark charcoal/slate, not brown or black -- deliberately neutral so
    # the wine accent (bars, buttons, titles) is the only warm thing on
    # screen and actually pops, instead of blending into a brown bg the
    # way it did on the original fixed palette.
    # Genuinely light -- the one light theme in the set, per the user's
    # explicit request. Warm off-white/greige, not stark white, so the
    # wine accent (text/bars/buttons) reads as the star of the palette.
    "brndz": ((222, 213, 202), (204, 192, 180), (150, 100, 92)),
    "neon": ((13, 19, 13), (20, 29, 20), (40, 58, 38)),
    "cyberpunk": ((10, 7, 15), (18, 13, 24), (48, 32, 58)),
    "radioativo": ((18, 18, 10), (27, 27, 16), (56, 56, 30)),
}


def _bar_color_themed(v: float, stops):
    v = max(0.0, min(1.0, v))
    for (a_pos, a_col), (b_pos, b_col) in zip(stops, stops[1:]):
        if a_pos <= v <= b_pos:
            t = 0.0 if b_pos == a_pos else (v - a_pos) / (b_pos - a_pos)
            return tuple(int(a_col[i] + (b_col[i] - a_col[i]) * t) for i in range(3))
    return stops[-1][1]


_FREQ_LABELS_HZ = [60, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

_N_SEGMENTS = 28
_SEGMENT_GAP = 2

_LEFT_MARGIN = 46
_BOTTOM_MARGIN = 20
_TOP_MARGIN = 18

_TOP_PAD = 8
_MIN_TOP_H = 285  # tall enough for the Áudio I/O panel's two stacked REC buttons (MIC + OUT/gear) + the LUFS row under OUT
_MIN_COL_FRAC = 0.12
_SPLIT_HIT = 10

_DISPLAY_NAME_OVERRIDES = {
    "memcompression": "Compressão de Memória (Windows)",
    "system idle process": "Ocioso (Windows)",
    "svchost": "Serviço do Windows (svchost)",
}


def _bar_color(v: float):
    """dark wine -> gold -> orange -> bright red, v in [0,1]."""
    v = max(0.0, min(1.0, v))
    for (a_pos, a_col), (b_pos, b_col) in zip(_COLOR_STOPS, _COLOR_STOPS[1:]):
        if a_pos <= v <= b_pos:
            t = 0.0 if b_pos == a_pos else (v - a_pos) / (b_pos - a_pos)
            return tuple(int(a_col[i] + (b_col[i] - a_col[i]) * t) for i in range(3))
    return _COLOR_STOPS[-1][1]


def _dim(color, factor=0.12):
    return tuple(max(3, int(c * factor)) for c in color)


def _freq_label(hz: float) -> str:
    if hz >= 1000:
        khz = hz / 1000
        return f"{khz:.0f}k" if khz == int(khz) else f"{khz:.1f}k"
    return f"{hz:.0f}"


def _display_process_name(name: str) -> str:
    return _DISPLAY_NAME_OVERRIDES.get(name.lower(), name)


class Renderer:
    def __init__(self, cfg):
        pygame.font.init()
        self.cfg = cfg

        # Segoe UI for labels/headings (Windows' own UI face, reads cleanly
        # at small sizes); Consolas for tabular rows, so numbers line up.
        # Each font below is named for the one role it plays -- no font gets
        # reused across an unrelated role, so a size/weight tweak for one
        # spot can't silently ripple into another.
        self.font_label = pygame.font.SysFont("segoe ui", 11)                          # KPI captions (dim, small)
        self.font_label_bold = pygame.font.SysFont("segoe ui semibold", 12, bold=True)  # KPI captions, emphasized: CPU/RAM/GPU
        self.font_value = pygame.font.SysFont("segoe ui semibold", 13, bold=True)      # KPI big numbers (CPU/RAM/GPU %)
        self.font_meter_label = pygame.font.SysFont("segoe ui semibold", 15, bold=True)  # IN/OUT meter labels
        self.font_row = pygame.font.SysFont(theme.FONT_NAME, 14)                       # tabular rows -- monospace, numbers align
        self.font_xs = pygame.font.SysFont(theme.FONT_NAME, 11)                        # fine print: LOG box, sub-labels
        self.font_md = pygame.font.SysFont("segoe ui", 16, bold=True)                  # toolbar buttons
        self.font_title = pygame.font.SysFont("segoe ui", 13, bold=True)               # panel titles (SISTEMA/ÁUDIO I/O/...)

        n = cfg.eq_bands
        self.smoothed_bands = np.zeros(n, dtype=np.float32)
        self.peak_level = np.zeros(n, dtype=np.float32)
        self.peak_hold_until = np.zeros(n, dtype=np.float64)
        self.clip_until = np.zeros(n, dtype=np.float64)
        self.spectrum_available = False
        self.spectrum_error = None
        self.peak_freq_hz = None
        self.peak_db = None
        self.output_device_name = None
        self.output_level_db = None
        self.output_peak_db = None
        self.output_lufs = None

        # Smoothed IN/OUT meter values (attack-fast/release-slow, same
        # shape as the EQ bands above) -- the raw snapshots only update at
        # ~12-23Hz (one per audio chunk), so without this the VU bars held
        # each value static for 40-85ms at a time and looked choppy/stuck
        # at the render loop's 60fps, unlike the main EQ which already
        # smooths every frame via update_spectrum(). update_audio_io()
        # advances these once per frame; _draw_io_channel only ever reads
        # the smoothed numbers, never the raw snapshot's level/peak.
        floor_db = cfg.eq_floor_db
        self._in_level_smoothed = floor_db
        self._in_peak_smoothed = floor_db
        self._out_level_smoothed = floor_db
        self._out_peak_smoothed = floor_db
        # LUFS itself already carries a ~400ms EMA from
        # lufs.MomentaryLufsMeter, but that only updates once per audio
        # chunk (~85ms) -- this render-frame smoothing is just to keep the
        # bar's motion as continuous as the other meters at 60fps, same
        # mechanism, much lighter time constant since the source is
        # already smooth.
        self._out_lufs_smoothed = -70.0
        self.vu_in_enabled = True
        self.vu_out_enabled = True
        self.vu_in_toggle_rect = pygame.Rect(0, 0, 0, 0)
        self.vu_out_toggle_rect = pygame.Rect(0, 0, 0, 0)

        edges = log_band_edges(n, cfg.eq_min_freq, cfg.eq_max_freq)
        self.band_centers = np.sqrt(edges[:-1] * edges[1:])
        self._label_band_indices = self._pick_label_bands()

        self._event_flashes = []  # dicts: msg, expire, severity

        # Resizable layout state: fraction of window height given to the
        # stats strip, and fraction of that strip's width given to each of
        # the 4 columns. Dragged live via handle_mouse_*.
        self.top_h_frac = 0.30
        self.col_fracs = [1 / 4, 1 / 4, 1 / 4, 1 / 4]
        self._drag = None  # ('top_h', None) or ('col', index)
        self._hover = None
        self.top_collapsed = False
        self.map_network_button_rect = pygame.Rect(0, 0, 0, 0)
        self.flush_dns_button_rect = pygame.Rect(0, 0, 0, 0)
        self.open_taskmgr_button_rect = pygame.Rect(0, 0, 0, 0)
        self.rec_button_rect = pygame.Rect(0, 0, 0, 0)
        self.mic_rec_button_rect = pygame.Rect(0, 0, 0, 0)
        self.audio_settings_button_rect = pygame.Rect(0, 0, 0, 0)
        self.mixer_button_rect = pygame.Rect(0, 0, 0, 0)
        self.audio_settings_open = False
        self.log_box_rect = pygame.Rect(0, 0, 0, 0)
        self.log_history_open = False

        self._watermark_key = None
        self._watermark_surf = None

        # "C" button (main panel header + compact mode) cycles through
        # these -- quente is the long-standing default look (unchanged
        # unless the user clicks), the rest are discrete alternates, not a
        # continuous picker, to keep this a one-button toggle instead of
        # new UI chrome. One shared choice for both views.
        self.eq_color_theme = "quente"

        self.lufs_alert_threshold = cfg.lufs_alert_threshold
        self.lufs_threshold_minus_rect = pygame.Rect(0, 0, 0, 0)
        self.lufs_threshold_plus_rect = pygame.Rect(0, 0, 0, 0)

        self._text_cache = {}

        self._row_colors = [_bar_color((seg + 0.5) / _N_SEGMENTS) for seg in range(_N_SEGMENTS)]
        self._row_colors_theme = "quente"
        self._ladder_key = None
        self._ladder_surf = None
        self._ladder_seg_y = None

        self._hbar_key = None
        self._hbar_surf = None
        self._vbar_key = None
        self._vbar_surf = None

    # ---- setup helpers -----------------------------------------------

    def _pick_label_bands(self):
        picked = {}
        for target_hz in _FREQ_LABELS_HZ:
            idx = int(np.argmin(np.abs(self.band_centers - target_hz)))
            picked[idx] = target_hz  # last write wins if two targets share a band
        return picked

    def _db_grid_marks(self):
        cfg = self.cfg
        marks = []
        db = cfg.eq_ceil_db
        while db >= cfg.eq_floor_db:
            marks.append(db)
            db -= 6.0
        return marks

    # ---- layout / resizing --------------------------------------------

    def _compute_layout(self, w, h):
        n_cols = len(self.col_fracs)
        if self.top_collapsed:
            top_h = 0
        else:
            top_h = max(_MIN_TOP_H, min(int(h * self.top_h_frac), h - 220))
        avail = w - _TOP_PAD * (n_cols + 1)
        col_h = max(0, top_h - _TOP_PAD * 2)
        cols = []
        x = _TOP_PAD
        for i in range(n_cols):
            cw = max(40, int(avail * self.col_fracs[i]))
            cols.append(pygame.Rect(x, _TOP_PAD, cw, col_h))
            x += cw + _TOP_PAD
        return top_h, cols

    def toggle_top_collapsed(self):
        self.top_collapsed = not self.top_collapsed

    def _hit_test_splitter(self, pos, w, h):
        top_h, cols = self._compute_layout(w, h)
        if abs(pos[1] - top_h) <= _SPLIT_HIT and -20 <= pos[0] <= w + 20:
            return ("top_h", None)
        if 0 <= pos[1] <= top_h:
            for i in range(len(cols) - 1):
                edge_x = (cols[i].right + cols[i + 1].x) // 2
                if abs(pos[0] - edge_x) <= _SPLIT_HIT:
                    return ("col", i)
        return None

    def splitter_at(self, pos, w, h):
        """Hit-test only (no side effects) -- used both for a hover-cursor
        every frame (so the drag handles are actually discoverable) and to
        highlight the handle being hovered/dragged. Returns 'top_h', 'col',
        or None; also caches the hit for _draw_split_handles to read.
        """
        hit = self._hit_test_splitter(pos, w, h)
        self._hover = hit
        return hit[0] if hit else None

    def handle_mouse_down(self, pos, w, h):
        hit = self._hit_test_splitter(pos, w, h)
        if hit:
            self._drag = hit
            return True
        return False

    def handle_mouse_motion(self, pos, w, h):
        if self._drag is None:
            return
        kind, i = self._drag
        n_cols = len(self.col_fracs)
        if kind == "top_h":
            self.top_h_frac = max(0.12, min(0.6, pos[1] / max(1, h)))
        elif kind == "col":
            avail = w - _TOP_PAD * (n_cols + 1)
            boundary_frac = (pos[0] - _TOP_PAD) / max(1, avail)
            before = sum(self.col_fracs[:i])
            after_next = before + self.col_fracs[i] + self.col_fracs[i + 1]
            new_i = max(_MIN_COL_FRAC, min(after_next - _MIN_COL_FRAC, boundary_frac) - before)
            self.col_fracs[i] = new_i
            self.col_fracs[i + 1] = after_next - before - new_i

    def handle_mouse_up(self):
        self._drag = None

    # ---- state updates -------------------------------------------------

    def update_spectrum(self, frame, dt: float):
        now = time.time()
        cfg = self.cfg

        target = frame.bands if frame.available and len(frame.bands) == len(self.smoothed_bands) \
            else np.zeros_like(self.smoothed_bands)

        attack = 1.0 - math.exp(-dt / 0.03)   # rises fast
        decay = 1.0 - math.exp(-dt / 0.4)     # falls slow, like a real VU meter
        rising = target > self.smoothed_bands
        coeff = np.where(rising, attack, decay)
        self.smoothed_bands += (target - self.smoothed_bands) * coeff

        new_peak = target > self.peak_level
        self.peak_level[new_peak] = target[new_peak]
        self.peak_hold_until[new_peak] = now + cfg.eq_peak_hold_s
        expired = (~new_peak) & (now > self.peak_hold_until)
        if expired.any():
            self.peak_level[expired] = np.maximum(0.0, self.peak_level[expired] - 0.6 * dt)

        clipping = target >= cfg.eq_clip_threshold
        self.clip_until[clipping] = now + cfg.eq_peak_hold_s

        self.spectrum_available = frame.available
        self.spectrum_error = frame.error
        self.peak_freq_hz = frame.peak_freq_hz
        self.peak_db = frame.peak_db
        self.output_device_name = frame.device_name
        self.output_level_db = frame.output_level_db
        self.output_peak_db = frame.output_peak_db
        self.output_lufs = frame.output_lufs

    def update_audio_io(self, audio_io, dt: float):
        """Advances the smoothed IN/OUT meter values one frame -- must run
        after update_spectrum() each frame (reads self.output_level_db,
        set there). When a side is disconnected, its target is the meter
        floor, so the bar eases back down instead of freezing at its last
        real reading.
        """
        floor_db = self.cfg.eq_floor_db
        attack = 1.0 - math.exp(-dt / 0.03)
        # Was 0.4s (matched a real analog VU meter's ballistics) -- cut to
        # 0.15s after the user flagged the meters as feeling laggy against
        # the real audio during live monitoring. Still eases instead of
        # snapping instantly (avoids a jittery/flickery number), just
        # settles much faster.
        decay = 1.0 - math.exp(-dt / 0.15)

        def _step(current, target):
            coeff = attack if target > current else decay
            return current + (target - current) * coeff

        inp = audio_io.input if audio_io is not None else None
        in_connected = bool(inp and inp.connected)
        self._in_level_smoothed = _step(self._in_level_smoothed, inp.level_db if in_connected else floor_db)
        self._in_peak_smoothed = _step(self._in_peak_smoothed, inp.peak_db if in_connected else floor_db)

        out_connected = bool(self.spectrum_available and self.output_level_db is not None)
        self._out_level_smoothed = _step(self._out_level_smoothed, self.output_level_db if out_connected else floor_db)
        self._out_peak_smoothed = _step(self._out_peak_smoothed, self.output_peak_db if out_connected else floor_db)
        # No extra smoothing here, unlike the two above -- LUFS already
        # comes out of lufs.MomentaryLufsMeter with its own real
        # time-domain EMA (that's what "momentary" loudness actually is).
        # Passing it through a second decay filter on top of that was
        # stacking two ~0.2-0.4s filters into a much laggier combined
        # response than either alone; this was the main fix for "LUFS
        # feels the most behind" once the chunk-size latency (see
        # Config.eq_fft_size) was addressed too.
        self._out_lufs_smoothed = self.output_lufs if (out_connected and self.output_lufs is not None) else -70.0

    def push_event(self, message: str, severity: str = "warn"):
        self._event_flashes.append({"msg": message, "expire": time.time() + 6.0, "severity": severity})
        self._event_flashes = self._event_flashes[-8:]

    # ---- drawing ---------------------------------------------------------

    def draw(self, surface, stats, network, processes, audio_io=None, log_events=None, recording=None, mic_recording=None):
        self.sync_theme_module()
        w, h = surface.get_size()
        bg, _, _ = self.bg_colors()
        surface.fill(bg)

        events_h = 70 if self._event_flashes else 0
        top_h, cols = self._compute_layout(w, h)
        stats_rect = pygame.Rect(0, 0, w, top_h)
        full_eq_rect = pygame.Rect(10, top_h + 10, w - 20, h - top_h - events_h - 20)
        events_rect = pygame.Rect(0, h - events_h, w, events_h)

        # LUFS meter claims a fixed-width strip on the right of the EQ
        # area, same visual weight as the EQ itself -- only when there's
        # real room for it, so a narrow/small window just drops it
        # instead of crushing the EQ down to nothing.
        lufs_w = 92
        if full_eq_rect.width > lufs_w * 3:
            lufs_rect = pygame.Rect(full_eq_rect.right - lufs_w, full_eq_rect.y, lufs_w, full_eq_rect.height)
            eq_rect = pygame.Rect(full_eq_rect.x, full_eq_rect.y, full_eq_rect.width - lufs_w - 10, full_eq_rect.height)
        else:
            lufs_rect = None
            eq_rect = full_eq_rect

        if not self.top_collapsed:
            self._draw_top_panels(surface, stats_rect, cols, stats, network, processes, audio_io, log_events, recording, mic_recording)
        else:
            # Not drawn (and not clickable) while the stats strip is
            # hidden -- otherwise a click on the now-EQ-occupied area at
            # its old coordinates could hit a stale button rect.
            self.map_network_button_rect = pygame.Rect(0, 0, 0, 0)
            self.flush_dns_button_rect = pygame.Rect(0, 0, 0, 0)
            self.open_taskmgr_button_rect = pygame.Rect(0, 0, 0, 0)
            self.rec_button_rect = pygame.Rect(0, 0, 0, 0)
            self.mic_rec_button_rect = pygame.Rect(0, 0, 0, 0)
            self.audio_settings_button_rect = pygame.Rect(0, 0, 0, 0)
            self.mixer_button_rect = pygame.Rect(0, 0, 0, 0)
        self._draw_split_handles(surface, w, top_h, cols)
        self._draw_spectrum(surface, eq_rect)
        if lufs_rect is not None:
            self._draw_loudness_vertical(surface, lufs_rect)
        else:
            self.lufs_threshold_minus_rect = pygame.Rect(0, 0, 0, 0)
            self.lufs_threshold_plus_rect = pygame.Rect(0, 0, 0, 0)
        if events_h:
            self._draw_events(surface, events_rect)

    def _draw_split_handles(self, surface, w, top_h, cols):
        active = self._drag or self._hover
        if self.top_collapsed:
            hot = bool(active) and active[0] == "top_h"
            color = self.chrome_accent() if hot else theme.PANEL_BORDER
            pygame.draw.line(surface, color, (4, top_h + 1), (w - 4, top_h + 1), width=3 if hot else 2)
            return
        for i in range(len(cols) - 1):
            edge_x = (cols[i].right + cols[i + 1].x) // 2
            hot = active == ("col", i)
            color = self.chrome_accent() if hot else theme.PANEL_BORDER
            pygame.draw.line(surface, color, (edge_x, 4), (edge_x, top_h - 4), width=3 if hot else 1)

        hot = bool(active) and active[0] == "top_h"
        color = self.chrome_accent() if hot else theme.PANEL_BORDER
        pygame.draw.line(surface, color, (4, top_h), (w - 4, top_h), width=3 if hot else 1)

    def bg_colors(self):
        """(bg, panel_bg, panel_border) for the current EQ theme -- see
        _EQ_THEME_BG."""
        return _EQ_THEME_BG.get(self.eq_color_theme, _EQ_THEME_BG["quente"])

    def sync_theme_module(self):
        """Mutates theme.py's own "constants" (BG/PANEL_BG/PANEL_BORDER,
        and TEXT/TEXT_DIM/TEXT_LABEL/GOLD for brndz specifically) to match
        the current eq_color_theme, every frame. Deliberately a global
        module mutation rather than routing every one of the hundreds of
        existing `theme.TEXT`/`theme.PANEL_BG` call sites through a
        per-instance lookup -- those call sites read the attribute fresh
        on every draw (Python doesn't cache attribute access), so this is
        both correct and by far the smallest change that makes every
        button/panel/popup in the whole app respect the active theme,
        including brndz being the one genuinely *light* theme in the set
        (dark wine text on a warm off-white, not just a lighter dark
        background) -- everything else stays dark-mode with its own hue.
        Call once at the top of draw()/draw_compact(), before anything
        else reads a theme.* color this frame.
        """
        global _DARK_TEXT_DEFAULTS
        if _DARK_TEXT_DEFAULTS is None:
            _DARK_TEXT_DEFAULTS = {
                "TEXT": theme.TEXT, "TEXT_DIM": theme.TEXT_DIM,
                "TEXT_LABEL": theme.TEXT_LABEL, "GOLD": theme.GOLD,
                "OK": theme.OK, "WARN": theme.WARN, "BAR_CLIP": theme.BAR_CLIP,
            }
        bg, panel_bg, panel_border = self.bg_colors()
        theme.BG, theme.PANEL_BG, theme.PANEL_BORDER = bg, panel_bg, panel_border
        if self.eq_color_theme == "brndz":
            theme.TEXT = (40, 24, 22)
            theme.TEXT_DIM = (112, 84, 78)
            theme.TEXT_LABEL = (148, 120, 112)
            theme.GOLD = (150, 40, 35)  # crimson, not gold -- gold read poorly here too (e.g. "Tempo Online")
            # The original light green/orange both washed out against the
            # warm beige background -- darker, still-recognizable shades
            # of the same colors, not different hues (green still means
            # "ok", orange still means "attention"). Tried blue for OK
            # first; reverted, plain darker green reads more naturally.
            theme.OK = (28, 118, 58)
            theme.WARN = (188, 110, 28)
            # Clipping/"over" goes blue -- not a brighter red, which is
            # where the rest of this palette already lives -- so it reads
            # as an unmistakable anomaly against all that red/wine.
            theme.BAR_CLIP = (30, 130, 220)
        else:
            theme.TEXT = _DARK_TEXT_DEFAULTS["TEXT"]
            theme.TEXT_DIM = _DARK_TEXT_DEFAULTS["TEXT_DIM"]
            theme.TEXT_LABEL = _DARK_TEXT_DEFAULTS["TEXT_LABEL"]
            theme.GOLD = _DARK_TEXT_DEFAULTS["GOLD"]
            theme.OK = _DARK_TEXT_DEFAULTS["OK"]
            theme.WARN = _DARK_TEXT_DEFAULTS["WARN"]
            theme.BAR_CLIP = _DARK_TEXT_DEFAULTS["BAR_CLIP"]

    def chrome_accent(self):
        """Themed replacement for the old fixed theme.ACCENT (wine) --
        panel titles, button borders, popup borders, idle-state colors,
        the MIXER button, the watermark. Went back and forth on this one:
        themed, then reverted to fixed-wine-everywhere, now themed again
        per the user's final call ("volta pra como era antes, tira só o
        brndz da regra" -- brndz keeps its own look either way, since its
        palette is wine-toned regardless). Deliberately NOT used for
        anything carrying real meaning (level meters, the LUFS threshold
        marker, recording-active red, the status dot) -- those stay fixed
        on purpose, same reasoning as _get_hbar_texture's docstring.
        """
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)
        return _bar_color_themed(0.62, stops)

    def chrome_title(self):
        """Deeper-shade themed replacement for theme.PANEL_TITLE (panel
        headers: SISTEMA/ÁUDIO I/O/REDE/PROCESSOS/LOG/popup titles)."""
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)
        return _bar_color_themed(0.5, stops)

    def event_text_color(self, crit: bool):
        """Color for a log-flash event line (bottom of the EQ area).
        Critical stays theme.CRIT (fixed red, same "danger" convention as
        the status dot) always. Non-critical is theme.WARN (yellow) on
        every dark theme, but that reads poorly on brndz's light beige --
        crimson instead there, for the same reason GOLD/OK got swapped.
        """
        if crit:
            return theme.CRIT
        if self.eq_color_theme == "brndz":
            return self.chrome_accent()
        return theme.WARN

    def _text(self, font, text, color):
        """Cached font.render: the stats/labels barely change between
        stat-poll ticks, so re-rasterizing the same string 60x/sec would
        burn CPU for nothing. Keyed by (font, text, color); cleared if it
        grows past a cap so a long session can't leak memory.
        """
        key = (id(font), text, color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 500:
                self._text_cache.clear()
            surf = font.render(text, True, color)
            self._text_cache[key] = surf
        return surf

    def _panel_rect(self, surface, rect, title):
        _, panel_bg, panel_border = self.bg_colors()
        pygame.draw.rect(surface, panel_bg, rect, border_radius=6)
        pygame.draw.rect(surface, panel_border, rect, width=1, border_radius=6)
        label = self._text(self.font_title, title, self.chrome_title())
        surface.blit(label, (rect.x + 12, rect.y + 8))
        pygame.draw.line(surface, panel_border, (rect.x + 12, rect.y + 26), (rect.right - 12, rect.y + 26))

    def _truncate(self, font, text, max_w):
        # No room at all -- drop the text rather than draw it full-width,
        # which is exactly how narrow columns used to bleed text into the
        # neighboring panel (every panel blits onto one shared surface;
        # nothing clips a draw at its own column's edge automatically).
        if max_w <= 0:
            return ""
        if font.size(text)[0] <= max_w:
            return text
        while text and font.size(text + "…")[0] > max_w:
            text = text[:-1]
        return f"{text}…" if text else ""

    def _row(self, surface, rect, y, left_text, right_text, color):
        # Right side capped to at most half the column so a long value
        # (a long dB/percentage string) can never by itself push into the
        # previous column, even when left_text is empty.
        right_text = self._truncate(self.font_row, right_text, max(0, rect.width - 24))
        right_img = self._text(self.font_row, right_text, color)
        right_x = rect.right - right_img.get_width() - 12
        surface.blit(right_img, (right_x, y))

        left_max_w = right_x - (rect.x + 12) - 8
        left_text = self._truncate(self.font_row, left_text, left_max_w)
        left_img = self._text(self.font_row, left_text, theme.TEXT_DIM)
        surface.blit(left_img, (rect.x + 12, y))
        return y + 19

    def _kpi(self, surface, rect, y, label, value_text, frac, color, sub_text=None):
        label_img = self._text(self.font_label_bold, label.upper(), theme.TEXT)
        surface.blit(label_img, (rect.x + 12, y))

        value_text = self._truncate(self.font_value, value_text, rect.width - 24)
        value_img = self._text(self.font_value, value_text, color)
        surface.blit(value_img, (rect.x + 12, y + 14))

        bar_y = y + 14 + value_img.get_height() + 3
        bar_rect = pygame.Rect(rect.x + 12, bar_y, rect.width - 24, 4)
        pygame.draw.rect(surface, theme.PANEL_BORDER, bar_rect, border_radius=2)
        if frac is not None:
            fill_w = max(0, min(bar_rect.width, int(bar_rect.width * frac)))
            if fill_w > 0:
                pygame.draw.rect(surface, color, pygame.Rect(bar_rect.x, bar_rect.y, fill_w, bar_rect.height), border_radius=2)
        y = bar_y + 7

        if sub_text:
            sub_text = self._truncate(self.font_xs, sub_text, rect.width - 24)
            sub_img = self._text(self.font_xs, sub_text, theme.TEXT_LABEL)
            surface.blit(sub_img, (rect.x + 12, y))
            y += sub_img.get_height() + 4

        return y

    def _draw_top_panels(self, surface, rect, cols, stats, network, processes, audio_io, log_events, recording, mic_recording=None):
        self._draw_system_panel(surface, cols[0], stats)
        self._draw_audio_io_panel(surface, cols[1], audio_io, recording, mic_recording)
        self._draw_network_panel(surface, cols[2], network, log_events)
        self._draw_process_panel(surface, cols[3], stats, processes)

    def _draw_io_channel(self, surface, rect, y, label, level_db, peak_db, clipping, connected, status_text, toggle_key=None):
        """One IN/OUT row: gold bold label + a tiny VU on/off toggle, a
        right-aligned dB reading, then a horizontal VU bar underneath --
        reads like an actual meter channel instead of a plain text line.

        `level_db`/`peak_db` are expected already-smoothed (see
        update_audio_io()) -- this method never touches raw snapshot
        values, so the bar/number always move fluidly at the render
        loop's frame rate regardless of how often real audio data arrives.
        `toggle_key`: "in" or "out", selects which vu_*_enabled flag/rect
        this row owns.
        """
        label_img = self._text(self.font_meter_label, label, theme.GOLD)
        surface.blit(label_img, (rect.x + 12, y))

        vu_enabled = self.vu_in_enabled if toggle_key == "in" else self.vu_out_enabled
        toggle_size = 12
        toggle_rect = pygame.Rect(rect.x + 12 + label_img.get_width() + 8, y + (label_img.get_height() - toggle_size) // 2, toggle_size, toggle_size)
        if toggle_key == "in":
            self.vu_in_toggle_rect = toggle_rect
        elif toggle_key == "out":
            self.vu_out_toggle_rect = toggle_rect
        toggle_color = theme.OK if vu_enabled else theme.TEXT_LABEL
        pygame.draw.rect(surface, theme.PANEL_BG, toggle_rect, border_radius=3)
        pygame.draw.rect(surface, toggle_color, toggle_rect, width=1 if vu_enabled else 1, border_radius=3)
        if vu_enabled:
            inner = toggle_rect.inflate(-6, -6)
            pygame.draw.rect(surface, toggle_color, inner, border_radius=1)

        if connected:
            val_color = theme.CRIT if clipping else theme.OK
            val_text = f"{level_db:.0f}dB  pk {peak_db:.0f}dB"
        else:
            val_color = theme.CRIT if connected is not None else theme.TEXT_DIM
            val_text = status_text or "OFFLINE"
        # Capped to the space actually left of the label+toggle -- on a
        # narrow column this used to be able to overlap them instead of
        # just running off the panel edge.
        val_text = self._truncate(self.font_row, val_text, max(0, rect.right - 12 - (toggle_rect.right + 8)))
        val_img = self._text(self.font_row, val_text, val_color)
        surface.blit(val_img, (rect.right - val_img.get_width() - 12, y + 3))

        y += label_img.get_height() + 5
        vu_rect = pygame.Rect(rect.x + 12, y, rect.width - 24, 9)
        if vu_enabled:
            self._draw_vu_meter(surface, vu_rect, level_db if connected else None, peak_db if connected else None, clipping)
        else:
            # Bar is only 9px tall -- no room for a label inside it, so
            # "off" just reads as a flat muted line instead of the usual
            # animated gradient fill.
            pygame.draw.rect(surface, theme.BG, vu_rect, border_radius=2)
            pygame.draw.rect(surface, theme.PANEL_BORDER, vu_rect, width=1, border_radius=2)
            pygame.draw.line(surface, theme.PANEL_BORDER, (vu_rect.x + 2, vu_rect.centery), (vu_rect.right - 2, vu_rect.centery), width=1)
        return y + vu_rect.height + 6

    def _get_hbar_texture(self, w, h):
        """Same idea as _get_ladder_texture but horizontal: one pre-rendered
        low->gold->orange->red gradient strip, cached by size, cropped to
        the current level's width each frame instead of drawing gradient
        pixels every call. Deliberately NOT themed with the EQ's color
        cycle (tried it, reverted) -- a level meter's color carries real
        meaning ("red = hot/near clipping"), and that has to stay
        consistent no matter what decorative palette is active, or a
        glance at the meter stops telling you anything reliable."""
        key = (w, h)
        if key == self._hbar_key:
            return self._hbar_surf
        w, h = max(1, w), max(1, h)
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for x in range(w):
            pygame.draw.line(surf, _bar_color(x / max(1, w - 1)), (x, 0), (x, h - 1))
        self._hbar_surf = surf
        self._hbar_key = key
        return surf

    def _draw_vu_meter(self, surface, rect, level_db, peak_db, clipping):
        """Horizontal VU bar: filled portion colored by the same
        amplitude-ladder gradient as the main EQ (so a glance at either
        reads the same "low=dark, hot=red" language), dB scale ticks
        (white, every 10dB, after a reference analog/digital VU meter),
        and a wine-red peak marker (not the plain white/cream used
        everywhere else -- makes the peak read as a distinct "hot" mark
        against the bar rather than blending into the fill at high
        levels)."""
        pygame.draw.rect(surface, theme.BG, rect, border_radius=2)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1, border_radius=2)
        if level_db is None:
            return
        floor_db, ceil_db = self.cfg.eq_floor_db, self.cfg.eq_ceil_db
        span = max(1e-6, ceil_db - floor_db)
        inner = rect.inflate(-2, -2)
        texture = self._get_hbar_texture(inner.width, inner.height)
        level_frac = max(0.0, min(1.0, (level_db - floor_db) / span))
        fill_w = int(inner.width * level_frac)
        if fill_w > 0:
            surface.blit(texture, inner.topleft, area=pygame.Rect(0, 0, fill_w, inner.height))

        db = ceil_db - 10.0
        while db > floor_db + 1e-6:
            frac = (db - floor_db) / span
            x = inner.x + int(inner.width * frac)
            pygame.draw.line(surface, theme.TEXT, (x, inner.bottom - 3), (x, inner.bottom), width=1)
            db -= 10.0

        if peak_db is not None:
            peak_frac = max(0.0, min(1.0, (peak_db - floor_db) / span))
            peak_x = inner.x + int(inner.width * peak_frac)
            pygame.draw.line(surface, theme.ACCENT, (peak_x, inner.y), (peak_x, inner.bottom - 1), width=2)
        if clipping:
            pygame.draw.rect(surface, theme.BAR_CLIP, rect, width=2, border_radius=2)

    # Real reference points from professional loudness meters (Youlean
    # Loudness Meter, TC Electronic LM, Nugen VisLM): -23 LUFS is the
    # EBU R128 broadcast target, -14 is the current Spotify/YouTube/
    # Amazon Music streaming standard. -9 is this app's own fixed ceiling
    # (see _LUFS_RED_DB below), also drawn bold since it's just as much a
    # real decision point. -19/-17 flank -14 for finer resolution right
    # around it. Numbers only in the UI -- no text labels (see chat/
    # CLAUDE.md for what each one means).
    _LUFS_REF_TICKS = (-23.0, -19.0, -14.0, -9.0)
    _LUFS_GRID = (-60, -40, -30, -17, -10, -5)
    # Fixed, not adjustable: at/above this the *entire* fill goes solid
    # red, all the way down -- past this point the signal is almost
    # certainly being compressed/limited, worth flagging unmistakably
    # regardless of where the user's own target marker sits.
    _LUFS_RED_DB = -9.0
    # Compact-mode-only fixed tone-shift point (the main panel uses the
    # adjustable lufs_alert_threshold marker instead; compact has no room
    # for its +/- stepper).
    _LUFS_COMPACT_TONE_DB = -19.0

    def _get_vbar_texture(self, w: int, h: int, theme_key: str):
        """Vertical version of _get_hbar_texture: one pre-rendered
        low->high gradient strip in the *current* EQ color theme, cached
        by (size, theme) so a theme change or resize rebuilds it but nothing
        else does. y=0 (top) is loudest, matching the meter's own layout."""
        key = (w, h, theme_key)
        if key == self._vbar_key:
            return self._vbar_surf
        w, h = max(1, w), max(1, h)
        stops = _EQ_COLOR_STOPS.get(theme_key, _COLOR_STOPS)
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for y in range(h):
            frac = 1.0 - (y / max(1, h - 1))
            pygame.draw.line(surf, _bar_color_themed(frac, stops), (0, y), (w - 1, y))
        self._vbar_surf = surf
        self._vbar_key = key
        return surf

    def _draw_loudness_vertical(self, surface, rect):
        """Vertical LUFS (momentary, ITU-R BS.1770-4) meter running the
        full height of the EQ area, to its right -- same visual weight as
        the EQ itself, not squeezed into a side panel. Bar fill is a
        gradient matching the current EQ color theme (not a flat color).

        Two independent markers, two different meanings:
        - `lufs_alert_threshold` (wine tick/number, user-adjustable via
          the -/+ stepper): a loudness *target* for the event ("I want to
          hit -14 tonight"). Once the reading reaches/passes it, only the
          part of the fill *above* that line switches to a brighter
          shade of the same theme -- "you hit your mark," not an alarm.
        - `_LUFS_RED_DB` (fixed at -9, not adjustable): the ceiling past
          which the signal is almost certainly being compressed/limited.
          Crossing it turns the *entire* fill solid red, overriding the
          target zone entirely -- unmistakable at a glance, since this
          one is a real problem, not just "past your goal."
        """
        pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=6)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1, border_radius=6)

        connected = bool(self.spectrum_available and self.output_level_db is not None)
        lufs = self._out_lufs_smoothed if connected else None
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)
        tone_color = _bar_color_themed(0.92, stops)
        if lufs is not None and lufs >= self._LUFS_RED_DB:
            active_color = theme.BAR_CLIP
        elif lufs is not None and lufs >= self.lufs_alert_threshold:
            active_color = tone_color
        else:
            active_color = _bar_color_themed(0.65, stops)

        title_img = self._text(self.font_xs, "LUFS", theme.OK)
        surface.blit(title_img, (rect.centerx - title_img.get_width() // 2, rect.y + 6))
        num_img = self._text(self.font_row, f"{lufs:.1f}" if lufs is not None else "--", active_color)
        surface.blit(num_img, (rect.centerx - num_img.get_width() // 2, rect.y + 6 + title_img.get_height() + 2))

        step_size = 16
        stepper_top = rect.bottom - step_size - 6
        bar_top = rect.y + 6 + title_img.get_height() + 2 + num_img.get_height() + 8
        bar_bottom = stepper_top - 6
        bar_w = 14
        bar_rect = pygame.Rect(rect.x + 26, bar_top, bar_w, max(1, bar_bottom - bar_top))
        pygame.draw.rect(surface, theme.BG, bar_rect, border_radius=2)
        pygame.draw.rect(surface, theme.PANEL_BORDER, bar_rect, width=1, border_radius=2)

        floor_db, ceil_db = self.cfg.eq_floor_db, self.cfg.eq_ceil_db
        span = max(1e-6, ceil_db - floor_db)
        inner = bar_rect.inflate(-2, -2)

        def y_for(db):
            frac = max(0.0, min(1.0, (db - floor_db) / span))
            return int(inner.bottom - inner.height * frac)

        if lufs is not None and inner.height > 2:
            y_current = y_for(lufs)
            fill_h = inner.bottom - y_current
            if fill_h > 0:
                if lufs >= self._LUFS_RED_DB:
                    # Past the fixed ceiling: the whole fill goes solid
                    # red, not just the part above it -- this is a "you're
                    # almost certainly compressing" flag, not a graded
                    # warning, so it reads as unmistakable at a glance.
                    pygame.draw.rect(surface, theme.BAR_CLIP, pygame.Rect(inner.x, y_current, inner.width, fill_h))
                else:
                    texture = self._get_vbar_texture(inner.width, inner.height, self.eq_color_theme)
                    crop = pygame.Rect(0, inner.height - fill_h, inner.width, fill_h)
                    surface.blit(texture, (inner.x, y_current), area=crop)

                    thr_db = self.lufs_alert_threshold
                    if lufs >= thr_db:
                        # Reached/passed the user's own target: only the
                        # part of the fill *above* the target line
                        # switches to the brighter tone, not the whole bar
                        # -- "you hit your mark and kept going," not an
                        # alert.
                        y_thr = y_for(thr_db)
                        tone_rect = pygame.Rect(inner.x, y_current, inner.width, max(0, y_thr - y_current))
                        if tone_rect.height > 0:
                            pygame.draw.rect(surface, tone_color, tone_rect)

        for db in self._LUFS_GRID:
            if db < floor_db or db > ceil_db:
                continue
            near_ref = min((abs(db - ref) for ref in self._LUFS_REF_TICKS), default=99) < 2
            y = y_for(db)
            pygame.draw.line(surface, theme.TEXT_LABEL, (inner.right + 2, y), (inner.right + 5, y), width=1)
            if not near_ref:
                label = self._text(self.font_xs, str(db), theme.TEXT_LABEL)
                surface.blit(label, (inner.right + 9, y - label.get_height() // 2))

        for ref_db in self._LUFS_REF_TICKS:
            if ref_db < floor_db or ref_db > ceil_db:
                continue
            y = y_for(ref_db)
            pygame.draw.line(surface, theme.TEXT, (inner.right + 2, y), (inner.right + 8, y), width=2)
            label = self._text(self.font_xs, f"{ref_db:.0f}", theme.TEXT)
            surface.blit(label, (inner.right + 10, y - label.get_height() // 2))

        if floor_db <= self.lufs_alert_threshold <= ceil_db:
            ty = y_for(self.lufs_alert_threshold)
            pygame.draw.line(surface, theme.ACCENT, (inner.x - 2, ty), (inner.right + 2, ty), width=2)
            thr_label = self._text(self.font_xs, f"{self.lufs_alert_threshold:.0f}", theme.ACCENT)
            label_x = inner.x - 4 - thr_label.get_width()
            if label_x >= rect.x:
                surface.blit(thr_label, (label_x, ty - thr_label.get_height() // 2))

        minus_rect = pygame.Rect(rect.x + 4, stepper_top, step_size, step_size)
        plus_rect = pygame.Rect(rect.right - step_size - 4, stepper_top, step_size, step_size)
        for step_rect, label in ((minus_rect, "-"), (plus_rect, "+")):
            pygame.draw.rect(surface, theme.PANEL_BG, step_rect, border_radius=3)
            pygame.draw.rect(surface, theme.PANEL_BORDER, step_rect, width=1, border_radius=3)
            step_label = self._text(self.font_xs, label, theme.TEXT)
            surface.blit(step_label, step_label.get_rect(center=step_rect.center))

        self.lufs_threshold_minus_rect = minus_rect
        self.lufs_threshold_plus_rect = plus_rect

    def _draw_rec_badge(self, surface, rect, active, label_text):
        """REC button: a "[ ● REC ]" lockup after the user's reference
        image (black brackets/text + red dot), recolored into this app's
        palette -- brackets and label in the same gray-brown as the rest
        of the panel's writing (TEXT_DIM), dot in wine (brighter red only
        once actually recording, matching the CRIT border). Bordered/
        PANEL_BG button chrome stays consistent with every other button in
        this row (Mapear Rede, Gerenciador de Tarefas). "[" and "]" are
        plain ASCII -- no font-fallback risk the way a Unicode ○/●/✓ glyph
        would be.
        """
        border = theme.CRIT if active else self.chrome_accent()
        pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=5)
        pygame.draw.rect(surface, border, rect, width=2 if active else 1, border_radius=5)

        bracket_color = theme.TEXT_DIM
        dot_color = theme.CRIT if active else self.chrome_title()
        dot_r = 5
        gap = 4

        open_img = self._text(self.font_row, "[", bracket_color)
        close_img = self._text(self.font_row, "]", bracket_color)
        fixed_w = open_img.get_width() + gap + dot_r * 2 + gap + gap + close_img.get_width()
        label_text = self._truncate(self.font_row, label_text, max(0, rect.width - 8 - fixed_w))
        label_img = self._text(self.font_row, label_text, theme.TEXT_DIM)

        total_w = open_img.get_width() + gap + dot_r * 2 + gap + label_img.get_width() + gap + close_img.get_width()
        x = rect.centerx - total_w // 2
        cy = rect.centery

        surface.blit(open_img, (x, cy - open_img.get_height() // 2))
        x += open_img.get_width() + gap
        pygame.draw.circle(surface, dot_color, (x + dot_r, cy), dot_r)
        x += dot_r * 2 + gap
        surface.blit(label_img, (x, cy - label_img.get_height() // 2))
        x += label_img.get_width() + gap
        surface.blit(close_img, (x, cy - close_img.get_height() // 2))

    def _draw_gear_plus_icon(self, surface, center, radius):
        """Settings icon: coral gear + gold plus-badge, drawn as vector
        primitives rather than a loaded image -- consistent with the rest
        of this UI's chrome (buttons, splitters, REC dot) being pygame
        primitives, and avoids the earlier font-glyph-fallback problem the
        text "CFG" placeholder was working around (U+2699 isn't reliably
        present in Consolas/Segoe UI on a stock Windows install)."""
        cx, cy = int(center[0]), int(center[1])
        gear_color = (224, 106, 78)
        outline = (18, 15, 12)
        body_r = radius * 0.66
        tooth = max(2, radius * 0.34)
        inner_r = max(2, radius * 0.30)

        for i in range(8):
            angle = (2 * math.pi / 8) * i
            tx = cx + math.cos(angle) * (body_r + tooth * 0.5)
            ty = cy + math.sin(angle) * (body_r + tooth * 0.5)
            tooth_rect = pygame.Rect(0, 0, int(tooth), int(tooth))
            tooth_rect.center = (int(tx), int(ty))
            pygame.draw.rect(surface, gear_color, tooth_rect, border_radius=1)
        pygame.draw.circle(surface, gear_color, (cx, cy), int(body_r))
        pygame.draw.circle(surface, theme.PANEL_BG, (cx, cy), int(inner_r))

        badge_r = max(4, int(radius * 0.44))
        bx, by = cx + int(radius * 0.4), cy + int(radius * 0.4)
        pygame.draw.circle(surface, theme.GOLD, (bx, by), badge_r)
        half = max(2, int(badge_r * 0.5))
        pygame.draw.line(surface, outline, (bx - half, by), (bx + half, by), width=2)
        pygame.draw.line(surface, outline, (bx, by - half), (bx, by + half), width=2)

    def _draw_eq_icon(self, surface, rect):
        """Small EQ-bars glyph, vector primitives -- for the compact-mode
        toggle button, after the reference image the user sent (a
        graphic-EQ silhouette)."""
        heights = (0.35, 0.65, 1.0, 0.5, 0.8)
        n = len(heights)
        pad = 3
        gap = 2
        bar_w = (rect.width - pad * 2 - gap * (n - 1)) / n
        base = rect.bottom - pad
        for i, hfrac in enumerate(heights):
            bh = max(2, (rect.height - pad * 2) * hfrac)
            x = rect.x + pad + i * (bar_w + gap)
            bar_rect = pygame.Rect(int(x), int(base - bh), max(1, int(bar_w)), int(bh))
            pygame.draw.rect(surface, theme.GOLD, bar_rect, border_radius=1)

    def _draw_lock_icon(self, surface, rect):
        """Small padlock glyph, vector primitives -- for the
        always-on-top toggle button, after the reference padlock image."""
        cx = rect.centerx
        body_w = rect.width * 0.55
        body_h = rect.height * 0.4
        body_rect = pygame.Rect(0, 0, int(body_w), int(body_h))
        body_rect.midtop = (cx, int(rect.centery))
        pygame.draw.rect(surface, theme.GOLD, body_rect, border_radius=2)
        shackle_r = max(2, int(body_w * 0.4))
        shackle_rect = pygame.Rect(0, 0, shackle_r * 2, shackle_r * 2)
        shackle_rect.midbottom = (cx, body_rect.top + 2)
        pygame.draw.arc(surface, theme.GOLD, shackle_rect, 0, math.pi, width=max(2, int(rect.width * 0.14)))

    def _draw_palette_icon(self, surface, rect):
        """Palette + paint-dots glyph, vector primitives -- replaces the
        plain "C" letter on the EQ-color-theme-cycle button, after the
        reference palette/brush image. Dot colors come from the
        *current* eq_color_theme -- this button literally picks it, so
        the icon showing that palette's own colors reads as self-
        explanatory rather than decorative.
        """
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)
        cx, cy = rect.center
        r = min(rect.width, rect.height) * 0.36
        pygame.draw.circle(surface, theme.PANEL_BORDER, (cx, cy), int(r), width=1)
        dot_positions = ((-0.35, -0.3), (0.15, -0.42), (0.42, 0.0), (0.18, 0.35), (-0.3, 0.3))
        for i, (dx, dy) in enumerate(dot_positions):
            frac = i / max(1, len(dot_positions) - 1)
            color = _bar_color_themed(frac, stops)
            dot_r = max(1, int(r * 0.32))
            pygame.draw.circle(surface, color, (int(cx + dx * r), int(cy + dy * r)), dot_r)

    def _draw_hollow_text(self, surface, font, text, pos, outline_color, fill_color):
        """Outline-only ("vazada") text: no native outline mode in
        pygame's font renderer, so this fakes it -- render solid in
        `outline_color` at 8 offsets around the target position (builds
        a ring), then render once more in `fill_color` dead-center
        (erases the middle back to the button's own fill, leaving only
        the ring/edges showing)."""
        x, y = pos
        outline_img = font.render(text, True, outline_color)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                surface.blit(outline_img, (x + dx, y + dy))
        inner_img = font.render(text, True, fill_color)
        surface.blit(inner_img, (x, y))

    def _draw_mini_fader_icon(self, surface, rect):
        """Tiny 3-fader-bank glyph for the MIXER button -- a track per
        channel + a knob at a different height on each, just enough to
        read as "mixer" at a glance without needing the real thing built
        yet. Drawn in theme.BG (dark) against the button's own gradient
        fill, same convention as the hollow "MIXER" text next to it."""
        n = 3
        gap = 3
        track_w = max(1, (rect.width - gap * (n - 1)) // n)
        knob_fracs = (0.35, 0.7, 0.5)
        for i in range(n):
            cx = rect.x + i * (track_w + gap) + track_w // 2
            pygame.draw.line(surface, theme.BG, (cx, rect.y), (cx, rect.bottom), width=2)
            ky = rect.bottom - int(rect.height * knob_fracs[i])
            pygame.draw.circle(surface, theme.BG, (cx, ky), 3)

    def _draw_audio_io_panel(self, surface, rect, audio_io, recording, mic_recording=None):
        self._panel_rect(surface, rect, "ÁUDIO I/O")

        # Settings gear moved up to the title row, right-aligned -- it
        # used to sit crammed next to REC OUT at the bottom, competing
        # for space with the record buttons.
        gear_size = 20
        self.audio_settings_button_rect = pygame.Rect(rect.right - gear_size - 8, rect.y + 6, gear_size, gear_size)
        pygame.draw.rect(surface, theme.PANEL_BG, self.audio_settings_button_rect, border_radius=5)
        pygame.draw.rect(surface, self.chrome_accent(), self.audio_settings_button_rect, width=1, border_radius=5)
        self._draw_gear_plus_icon(
            surface, self.audio_settings_button_rect.center,
            min(self.audio_settings_button_rect.width, self.audio_settings_button_rect.height) / 2 - 1,
        )

        y = 34
        roomy = rect.width >= 150
        btn_h = 24
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)

        inp = audio_io.input if audio_io is not None else None
        in_connected = bool(inp is not None and inp.connected)
        in_status = "aguardando..." if inp is None else (inp.error or "OFFLINE")
        y = self._draw_io_channel(
            surface, rect, y, "IN",
            self._in_level_smoothed, self._in_peak_smoothed,
            bool(inp and inp.clipping), in_connected if inp is not None else None, in_status,
            toggle_key="in",
        )
        if in_connected and roomy:
            in_name = self._truncate(self.font_row, inp.name, rect.width - 24)
            surface.blit(self._text(self.font_row, in_name, theme.TEXT_DIM), (rect.x + 12, y))
            y += self.font_row.get_height() + 4

        # REC button lives directly under its own channel now (was two
        # buttons stacked together at the bottom of the whole panel,
        # disconnected from which device each one actually belonged to).
        self.mic_rec_button_rect = pygame.Rect(rect.x + 10, y, rect.width - 20, btn_h)
        mic_rec_active = bool(mic_recording and mic_recording.active)
        if mic_rec_active:
            elapsed = max(0.0, time.time() - mic_recording.started_at)
            mins, secs = divmod(int(elapsed), 60)
            mic_rec_text = f"REC IN {mins:02d}:{secs:02d}"
        else:
            mic_rec_text = "REC IN"
        self._draw_rec_badge(surface, self.mic_rec_button_rect, mic_rec_active, mic_rec_text)
        y += btn_h + 10

        out_connected = bool(self.spectrum_available and self.output_level_db is not None)
        out_clipping = bool((self.clip_until > time.time()).any())
        y = self._draw_io_channel(
            surface, rect, y, "OUT",
            self._out_level_smoothed, self._out_peak_smoothed, out_clipping,
            out_connected, "OFFLINE",
            toggle_key="out",
        )
        if out_connected and roomy:
            out_name = self._truncate(self.font_row, self.output_device_name or "-", rect.width - 24)
            surface.blit(self._text(self.font_row, out_name, theme.TEXT_DIM), (rect.x + 12, y))
            y += self.font_row.get_height() + 4

        self.rec_button_rect = pygame.Rect(rect.x + 10, y, rect.width - 20, btn_h)
        rec_active = bool(recording and recording.active)
        if rec_active:
            elapsed = max(0.0, time.time() - recording.started_at)
            mins, secs = divmod(int(elapsed), 60)
            rec_text = f"REC OUT {mins:02d}:{secs:02d}"
        else:
            rec_text = "REC OUT"
        self._draw_rec_badge(surface, self.rec_button_rect, rec_active, rec_text)
        y += btn_h + 10

        # MIXER: placeholder only, no function yet -- just claiming its
        # spot in the layout for a later session. Gradient fill (the same
        # low->high theme colors as the EQ bars, not a flat color) + a
        # tiny fader-bank glyph + hollow/outline letters -- deliberately
        # more ornate than every other button here, so it reads as
        # "something bigger lives behind this" without needing a text
        # label saying so.
        mixer_h = 28
        if y + mixer_h <= rect.bottom - 8:
            r = pygame.Rect(rect.x + 10, rect.bottom - 8 - mixer_h, rect.width - 20, mixer_h)
            self.mixer_button_rect = r
            for gx in range(r.width):
                frac = gx / max(1, r.width - 1)
                pygame.draw.line(surface, _bar_color_themed(frac, stops), (r.x + gx, r.y), (r.x + gx, r.bottom - 1))
            pygame.draw.rect(surface, theme.TEXT, r, width=1, border_radius=6)

            icon_rect = pygame.Rect(r.x + 10, r.y + 5, 26, r.height - 10)
            self._draw_mini_fader_icon(surface, icon_rect)

            mid_color = _bar_color_themed(0.5, stops)
            label_img = self.font_label_bold.render("MIXER", True, theme.BG)
            label_x = icon_rect.right + 10
            label_y = r.centery - label_img.get_height() // 2
            self._draw_hollow_text(surface, self.font_label_bold, "MIXER", (label_x, label_y), theme.BG, mid_color)
        else:
            self.mixer_button_rect = pygame.Rect(0, 0, 0, 0)

    def _draw_log_box(self, surface, rect, log_events, bottom_y):
        """Recent-activity strip: a dedicated header row (title + a rule,
        matching how every other panel separates its title from its body)
        instead of one "LOG:" tag jammed against the first entry, and a
        fixed time/icon/message column layout instead of one concatenated
        string per row -- keeps the message legible regardless of how wide
        the timestamp or icon happen to render.

        `bottom_y`: absolute y the box's bottom edge sits at -- the
        caller owns how much button row(s) worth of space to reserve
        below it, since that varies per panel (REDE has two buttons
        stacked under it now, not one).
        """
        # Only the single most-recent event now -- the "clique p/ mais"
        # popup covers the deeper look this used to need 3 cramped rows
        # for.
        box_h = 50
        box = pygame.Rect(rect.x + 8, bottom_y - box_h, rect.width - 16, box_h)
        self.log_box_rect = box  # main.py hit-tests this to open the bigger history popup
        pygame.draw.rect(surface, theme.BG, box, border_radius=6)
        pygame.draw.rect(surface, theme.PANEL_BORDER, box, width=1, border_radius=6)

        title = self._text(self.font_xs, "LOG", self.chrome_title())
        surface.blit(title, (box.x + 8, box.y + 6))
        # All-or-nothing, not truncated -- a fragment like "cli…" isn't
        # useful to anyone, so this only shows up when the full phrase
        # actually fits without touching "LOG".
        hint = self._text(self.font_xs, "clique p/ mais", theme.TEXT_LABEL)
        if title.get_width() + 10 + hint.get_width() + 16 <= box.width:
            surface.blit(hint, (box.right - hint.get_width() - 8, box.y + 6))
        pygame.draw.line(surface, theme.PANEL_BORDER, (box.x + 8, box.y + 19), (box.right - 8, box.y + 19))

        events = list(log_events or [])[-1:]
        if not events:
            empty = self._text(self.font_xs, "sem eventos", theme.TEXT_LABEL)
            surface.blit(empty, (box.x + 8, box.y + 26))
            return

        # A colored dot instead of a text glyph for the level marker -- a
        # checkmark/bullet character (✓/●) isn't guaranteed present in
        # Consolas on a stock Windows install (same class of gap as the
        # U+2699 gear glyph that "CFG" used to work around), so this
        # sidesteps font-fallback risk entirely instead of picking glyphs
        # by trial and error.
        time_col_w = self.font_xs.size("00:00:00")[0] + 6
        icon_col_w = 14
        msg_x = box.x + 8 + time_col_w + icon_col_w
        row_h = 18
        row_y = box.y + 25
        for event in reversed(events):
            level = event.get("level", "INFO")
            color = theme.CRIT if level in ("ERROR", "CRASH", "HANG") else self.event_text_color(False) if level == "WARNING" else theme.OK if level in ("RECOVERY", "OK") else theme.TEXT_DIM

            surface.blit(self._text(self.font_xs, event.get("time", ""), theme.TEXT_LABEL), (box.x + 8, row_y))
            dot_cy = row_y + self.font_xs.get_height() // 2
            pygame.draw.circle(surface, color, (box.x + 8 + time_col_w + 5, dot_cy), 3)
            msg = self._truncate(self.font_xs, event.get("message", ""), box.right - 6 - msg_x)
            surface.blit(self._text(self.font_xs, msg, color), (msg_x, row_y))
            row_y += row_h

    def _draw_system_panel(self, surface, rect, stats):
        self._panel_rect(surface, rect, "SISTEMA")
        y = 34
        if stats is None:
            self._row(surface, rect, y, "aguardando...", "", theme.TEXT_DIM)
            return
        hw = stats.hw
        # Below this, the panel is narrow enough (dragged splitter, or a
        # small window) that the least essential text -- CPU model/core
        # count, GPU model name -- gets dropped instead of squeezed down
        # to an unreadable "..." fragment. The number/percentage that
        # actually matters at a glance always stays.
        roomy = rect.width >= 150

        cpu_color = theme.CRIT if stats.cpu_percent > 90 else theme.WARN if stats.cpu_percent > 70 else theme.OK
        cpu_sub = None
        if roomy:
            cpu_sub = hw.cpu_name
            if hw.cpu_cores_physical:
                cpu_sub += f" · {hw.cpu_cores_physical}C/{hw.cpu_cores_logical}T"
            if hw.cpu_freq_mhz:
                cpu_sub += f" · {hw.cpu_freq_mhz / 1000:.1f}GHz"
        y = self._kpi(surface, rect, y, "CPU", f"{stats.cpu_percent:.1f}%", stats.cpu_percent / 100, cpu_color, cpu_sub)

        ram_color = theme.CRIT if stats.ram_percent > 90 else theme.WARN if stats.ram_percent > 75 else theme.OK
        ram_total_gb = (hw.ram_total_mb or stats.ram_total_mb) / 1024
        ram_value = f"{stats.ram_percent:.1f}%" if not roomy else f"{stats.ram_percent:.1f}% · {stats.ram_used_mb / 1024:.1f} / {ram_total_gb:.1f}GB"
        y = self._kpi(surface, rect, y, "RAM", ram_value, stats.ram_percent / 100, ram_color)

        if stats.gpu.available:
            gcolor = theme.CRIT if stats.gpu.util_percent > 90 else theme.WARN if stats.gpu.util_percent > 70 else theme.OK
            vram_total_gb = (hw.gpu_vram_total_mb or stats.gpu.vram_total_mb) / 1024
            gpu_value = f"{stats.gpu.util_percent:.1f}%" if not roomy else f"{stats.gpu.util_percent:.1f}% · {stats.gpu.vram_used_mb / 1024:.1f} / {vram_total_gb:.1f}GB"
            y = self._kpi(surface, rect, y, "GPU", gpu_value, stats.gpu.util_percent / 100, gcolor,
                          (hw.gpu_name or stats.gpu.name) if roomy else None)
        else:
            y = self._row(surface, rect, y + 6, "GPU", "indisponível", theme.TEXT_DIM)

        for d in stats.disks:
            if y > rect.height - 16:
                break
            if d.error:
                y = self._row(surface, rect, y, d.path, "erro", theme.CRIT)
                continue
            color = theme.CRIT if d.free_gb < 10 else theme.WARN if d.free_gb < 50 else theme.TEXT_DIM
            y = self._row(surface, rect, y, d.path, f"{d.free_gb:.0f}GB livres", color)

    def _draw_network_panel(self, surface, rect, network, log_events=None):
        self._panel_rect(surface, rect, "REDE")
        y = 34
        if network is None:
            self._row(surface, rect, y, "aguardando...", "", theme.TEXT_DIM)
        else:
            y = self._row(surface, rect, y, "download", f"{network.download_mbps:.2f} Mbps", theme.OK)
            y = self._row(surface, rect, y, "upload", f"{network.upload_mbps:.2f} Mbps", theme.OK)
            y += 6

            # Capped at 2 -- the panel also needs room for the log box and
            # button below, and the default config only has 2 targets anyway.
            for t in network.targets[:2]:
                if t.alive:
                    color = theme.WARN if t.loss_percent > 0 else theme.OK
                    y = self._row(surface, rect, y, t.host, f"{t.latency_ms:.0f}ms  {t.loss_percent:.0f}%loss", color)
                else:
                    y = self._row(surface, rect, y, t.host, f"DOWN  {t.loss_percent:.0f}%loss", theme.CRIT)

        # Anchored to the bottom of the panel (not flowed after the rows
        # above) so both stay in a predictable spot regardless of how many
        # ping targets are configured. Flush DNS sits right above Mapear
        # Rede -- the gap this used to leave blank under the ping targets
        # is now two stacked network-utility actions instead of dead space.
        btn_h = 26
        gap = 6
        map_y = rect.bottom - btn_h - 8
        dns_y = map_y - gap - btn_h
        self.map_network_button_rect = pygame.Rect(rect.x + 10, map_y, rect.width - 20, btn_h)
        self.flush_dns_button_rect = pygame.Rect(rect.x + 10, dns_y, rect.width - 20, btn_h)

        self._draw_log_box(surface, rect, log_events, dns_y - 8)

        pygame.draw.rect(surface, theme.PANEL_BG, self.flush_dns_button_rect, border_radius=5)
        pygame.draw.rect(surface, self.chrome_accent(), self.flush_dns_button_rect, width=1, border_radius=5)
        dns_label_text = self._truncate(self.font_row, "Flush DNS", self.flush_dns_button_rect.width - 12)
        dns_label = self._text(self.font_row, dns_label_text, self.chrome_accent())
        surface.blit(dns_label, dns_label.get_rect(center=self.flush_dns_button_rect.center))

        pygame.draw.rect(surface, theme.PANEL_BG, self.map_network_button_rect, border_radius=5)
        pygame.draw.rect(surface, self.chrome_accent(), self.map_network_button_rect, width=1, border_radius=5)
        label_text = self._truncate(self.font_row, "Mapear Rede", self.map_network_button_rect.width - 12)
        label = self._text(self.font_row, label_text, self.chrome_accent())
        surface.blit(label, label.get_rect(center=self.map_network_button_rect.center))

    def draw_audio_settings_popup(self, surface, directory_display, recording_format, detail_display, mic_gain_db, out_gain_db):
        """Small modal for the settings button -- directory is browse-only
        (a native folder picker, driven from main.py), not an in-app text
        field, to avoid building text-input editing for one setting.
        `recording_format` is "wav" or "mp3" (drives which pill is
        highlighted); `detail_display` is the sample rate/channels/bit-depth
        (or bitrate) line, precomputed by main.py since it already knows
        cfg. `mic_gain_db`/`out_gain_db` drive the mic/OUT boost steppers
        -- -/+ pairs instead of drag sliders, since a fixed step is all
        either needs and a slider would be real extra layout/drag-handling
        code for no real benefit here.
        Returns (browse_button_rect, close_button_rect, wav_rect, mp3_rect,
        mic_gain_minus_rect, mic_gain_plus_rect, out_gain_minus_rect,
        out_gain_plus_rect).
        """
        w, h = surface.get_size()
        panel = pygame.Rect(0, 0, 380, 322)
        panel.center = (w // 2, h // 2)

        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 120))
        surface.blit(overlay, (0, 0))

        pygame.draw.rect(surface, self.bg_colors()[1], panel, border_radius=8)
        pygame.draw.rect(surface, self.chrome_accent(), panel, width=2, border_radius=8)

        title = self._text(self.font_title, "GRAVAÇÃO — CONFIGURAÇÕES", self.chrome_title())
        surface.blit(title, (panel.x + 16, panel.y + 12))

        close_rect = pygame.Rect(panel.right - 32, panel.y + 8, 22, 22)
        pygame.draw.rect(surface, theme.PANEL_BORDER, close_rect, width=1, border_radius=4)
        x_label = self._text(self.font_row, "x", theme.TEXT)
        surface.blit(x_label, x_label.get_rect(center=close_rect.center))

        y = panel.y + 42
        surface.blit(self._text(self.font_xs, "PASTA DE GRAVAÇÃO", theme.TEXT_LABEL), (panel.x + 16, y))
        y += 16
        dir_text = self._truncate(self.font_row, directory_display, panel.width - 32)
        surface.blit(self._text(self.font_row, dir_text, theme.TEXT), (panel.x + 16, y))
        y += 26

        browse_rect = pygame.Rect(panel.x + 16, y, 130, 28)
        pygame.draw.rect(surface, theme.PANEL_BORDER, browse_rect, width=1, border_radius=5)
        browse_label = self._text(self.font_row, "Procurar...", theme.TEXT)
        surface.blit(browse_label, browse_label.get_rect(center=browse_rect.center))

        y += 44
        surface.blit(self._text(self.font_xs, "FORMATO", theme.TEXT_LABEL), (panel.x + 16, y))
        y += 18

        wav_rect = pygame.Rect(panel.x + 16, y, 90, 28)
        mp3_rect = pygame.Rect(wav_rect.right + 10, y, 90, 28)
        for pill_rect, label, active in ((wav_rect, "WAV", recording_format != "mp3"), (mp3_rect, "MP3", recording_format == "mp3")):
            pygame.draw.rect(surface, theme.PANEL_BG, pill_rect, border_radius=5)
            pygame.draw.rect(surface, self.chrome_accent() if active else theme.PANEL_BORDER, pill_rect, width=2 if active else 1, border_radius=5)
            pill_label = self._text(self.font_row, label, self.chrome_accent() if active else theme.TEXT_DIM)
            surface.blit(pill_label, pill_label.get_rect(center=pill_rect.center))
        y += 36

        surface.blit(self._text(self.font_xs, detail_display, theme.TEXT_LABEL), (panel.x + 16, y))
        y += 26

        surface.blit(self._text(self.font_xs, "GANHO DO MIC", theme.TEXT_LABEL), (panel.x + 16, y))
        y += 18

        step_size = 28
        minus_rect = pygame.Rect(panel.x + 16, y, step_size, step_size)
        plus_rect = pygame.Rect(minus_rect.right + 90, y, step_size, step_size)
        for step_rect, label in ((minus_rect, "-"), (plus_rect, "+")):
            pygame.draw.rect(surface, theme.PANEL_BG, step_rect, border_radius=5)
            pygame.draw.rect(surface, theme.PANEL_BORDER, step_rect, width=1, border_radius=5)
            step_label = self._text(self.font_row, label, theme.TEXT)
            surface.blit(step_label, step_label.get_rect(center=step_rect.center))

        gain_text = self._text(self.font_row, f"+{mic_gain_db:.0f} dB", self.chrome_accent())
        gain_rect = gain_text.get_rect()
        gain_rect.center = ((minus_rect.right + plus_rect.left) // 2, minus_rect.centery)
        surface.blit(gain_text, gain_rect)
        y += step_size + 20

        surface.blit(self._text(self.font_xs, "GANHO DO OUT", theme.TEXT_LABEL), (panel.x + 16, y))
        y += 18

        out_minus_rect = pygame.Rect(panel.x + 16, y, step_size, step_size)
        out_plus_rect = pygame.Rect(out_minus_rect.right + 90, y, step_size, step_size)
        for step_rect, label in ((out_minus_rect, "-"), (out_plus_rect, "+")):
            pygame.draw.rect(surface, theme.PANEL_BG, step_rect, border_radius=5)
            pygame.draw.rect(surface, theme.PANEL_BORDER, step_rect, width=1, border_radius=5)
            step_label = self._text(self.font_row, label, theme.TEXT)
            surface.blit(step_label, step_label.get_rect(center=step_rect.center))

        out_gain_text = self._text(self.font_row, f"{'+' if out_gain_db >= 0 else ''}{out_gain_db:.0f} dB", self.chrome_accent())
        out_gain_rect = out_gain_text.get_rect()
        out_gain_rect.center = ((out_minus_rect.right + out_plus_rect.left) // 2, out_minus_rect.centery)
        surface.blit(out_gain_text, out_gain_rect)

        return browse_rect, close_rect, wav_rect, mp3_rect, minus_rect, plus_rect, out_minus_rect, out_plus_rect

    def draw_confirm_popup(self, surface, title, message, yes_label, no_label):
        """Generic small Yes/No modal -- used for "recording is active,
        stop and exit?" so a quit while REC never silently loses the file.
        Returns (yes_rect, no_rect).
        """
        w, h = surface.get_size()
        panel = pygame.Rect(0, 0, 400, 140)
        panel.center = (w // 2, h // 2)

        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        surface.blit(overlay, (0, 0))

        pygame.draw.rect(surface, self.bg_colors()[1], panel, border_radius=8)
        pygame.draw.rect(surface, theme.CRIT, panel, width=2, border_radius=8)

        surface.blit(self._text(self.font_title, title, theme.CRIT), (panel.x + 16, panel.y + 14))
        msg_text = self._truncate(self.font_row, message, panel.width - 32)
        surface.blit(self._text(self.font_row, msg_text, theme.TEXT), (panel.x + 16, panel.y + 44))

        btn_w, btn_h, gap = 160, 32, 12
        yes_rect = pygame.Rect(panel.centerx - btn_w - gap // 2, panel.bottom - btn_h - 14, btn_w, btn_h)
        no_rect = pygame.Rect(panel.centerx + gap // 2, panel.bottom - btn_h - 14, btn_w, btn_h)

        pygame.draw.rect(surface, theme.PANEL_BG, yes_rect, border_radius=5)
        pygame.draw.rect(surface, theme.CRIT, yes_rect, width=1, border_radius=5)
        yes_img = self._text(self.font_row, yes_label, theme.CRIT)
        surface.blit(yes_img, yes_img.get_rect(center=yes_rect.center))

        pygame.draw.rect(surface, theme.PANEL_BG, no_rect, border_radius=5)
        pygame.draw.rect(surface, theme.PANEL_BORDER, no_rect, width=1, border_radius=5)
        no_img = self._text(self.font_row, no_label, theme.TEXT)
        surface.blit(no_img, no_img.get_rect(center=no_rect.center))

        return yes_rect, no_rect

    def draw_log_history_popup(self, surface, event_records, log_dir_display):
        """Bigger read-only log view for the "clique p/ mais" hint on the
        LOG box -- a quick look at more recent activity without stopping
        the session to open the CSV. Shows the most recent rows that fit
        (no scroll wheel handling, keeping this simple); the full history
        always still goes to the CSV/Markdown report regardless of what's
        shown here. `log_dir_display` is the current (or overridden) log
        destination -- this popup is where the "PASTA DE LOGS" browse
        button lives (moved here from the recording-settings popup: this
        is "the logs panel," so that's where a log-path setting belongs).
        Returns (close_rect, log_browse_rect).
        """
        w, h = surface.get_size()
        panel = pygame.Rect(0, 0, min(760, w - 60), min(520, h - 60))
        panel.center = (w // 2, h // 2)

        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 140))
        surface.blit(overlay, (0, 0))

        pygame.draw.rect(surface, self.bg_colors()[1], panel, border_radius=8)
        pygame.draw.rect(surface, self.chrome_accent(), panel, width=2, border_radius=8)

        title = self._text(self.font_title, "HISTÓRICO DE EVENTOS", self.chrome_title())
        surface.blit(title, (panel.x + 16, panel.y + 12))

        close_rect = pygame.Rect(panel.right - 32, panel.y + 8, 22, 22)
        pygame.draw.rect(surface, theme.PANEL_BORDER, close_rect, width=1, border_radius=4)
        x_label = self._text(self.font_row, "x", theme.TEXT)
        surface.blit(x_label, x_label.get_rect(center=close_rect.center))

        pygame.draw.line(surface, theme.PANEL_BORDER, (panel.x + 16, panel.y + 38), (panel.right - 16, panel.y + 38))

        row_h = 19
        list_top = panel.y + 46
        list_bottom = panel.bottom - 66  # leaves room for the footer + log-path row below
        max_rows = min(20, max(1, (list_bottom - list_top) // row_h))

        events = list(event_records or [])
        shown = events[-max_rows:]
        time_col_w = self.font_xs.size("00:00:00")[0] + 10
        level_col_w = self.font_xs.size("WARNING")[0] + 10
        msg_x = panel.x + 16 + time_col_w + level_col_w

        y = list_top
        for event in reversed(shown):
            level = event.get("level", "INFO")
            color = theme.CRIT if level in ("ERROR", "CRASH", "HANG") else self.event_text_color(False) if level == "WARNING" else theme.OK if level in ("RECOVERY", "OK") else theme.TEXT_DIM
            surface.blit(self._text(self.font_xs, event.get("time", ""), theme.TEXT_LABEL), (panel.x + 16, y))
            surface.blit(self._text(self.font_xs, level, color), (panel.x + 16 + time_col_w, y))
            msg = self._truncate(self.font_xs, f"[{event.get('source', '')}] {event.get('message', '')}", panel.right - 16 - msg_x)
            surface.blit(self._text(self.font_xs, msg, theme.TEXT_DIM), (msg_x, y))
            y += row_h

        if not shown:
            empty = self._text(self.font_row, "sem eventos ainda", theme.TEXT_LABEL)
            surface.blit(empty, (panel.x + 16, list_top))

        footer = f"Mostrando os {len(shown)} mais recentes de {len(events)} — histórico completo no CSV/relatório da sessão"
        footer_img = self._text(self.font_xs, self._truncate(self.font_xs, footer, panel.width - 32), theme.TEXT_LABEL)
        surface.blit(footer_img, (panel.x + 16, panel.bottom - 44))

        pygame.draw.line(surface, theme.PANEL_BORDER, (panel.x + 16, panel.bottom - 34), (panel.right - 16, panel.bottom - 34))

        log_browse_rect = pygame.Rect(panel.right - 16 - 130, panel.bottom - 26, 130, 20)
        pygame.draw.rect(surface, theme.PANEL_BORDER, log_browse_rect, width=1, border_radius=4)
        log_browse_label = self._text(self.font_xs, "Alterar pasta...", theme.TEXT)
        surface.blit(log_browse_label, log_browse_label.get_rect(center=log_browse_rect.center))

        # "aplica no próximo início" -- SessionLogger opens its CSV/events
        # files once at startup, so browsing a new folder here can't
        # redirect a handle that's already open (matches the toast
        # main.py shows after picking).
        log_label = self._truncate(self.font_xs, f"PASTA DE LOGS: {log_dir_display}", log_browse_rect.x - (panel.x + 16) - 8)
        surface.blit(self._text(self.font_xs, log_label, theme.TEXT_LABEL), (panel.x + 16, panel.bottom - 24))

        return close_rect, log_browse_rect

    def _draw_process_panel(self, surface, rect, stats, processes):
        self._panel_rect(surface, rect, "PROCESSOS")
        y = 34
        content_bottom = rect.height - 44  # leaves room for the button anchored below

        if processes is not None:
            for p in processes.states:
                # A watched process (obs64/vMix64/...) only ever shows up
                # here if something's actually wrong -- crashed, hung, or
                # erroring. Never started / closed normally / running fine
                # stays invisible, it'd just be noise next to the top-RAM
                # list below.
                if p.error:
                    y = self._row(surface, rect, y, p.name, "erro", theme.CRIT)
                elif p.crashed:
                    y = self._row(surface, rect, y, p.name, "crashou", theme.CRIT)
                elif p.hung:
                    y = self._row(surface, rect, y, p.name, "travado", theme.CRIT)
                elif p.running and p.ram_mb > self.cfg.ram_alert_mb:
                    y = self._row(surface, rect, y, p.name, f"{p.ram_mb:.0f}MB", theme.WARN)

        if stats is not None and stats.top_processes and y < content_bottom:
            sep = self._text(self.font_xs, "PROCESSOS", theme.TEXT_LABEL)
            surface.blit(sep, (rect.x + 12, y + 2))
            y += 18
            for proc in stats.top_processes[: self.cfg.top_n_processes]:
                if y > content_bottom:
                    break
                y = self._row(surface, rect, y, _display_process_name(proc.name)[:22], f"{proc.ram_mb:.0f}MB", theme.TEXT_DIM)

        btn_h = 26
        self.open_taskmgr_button_rect = pygame.Rect(rect.x + 10, rect.bottom - btn_h - 8, rect.width - 20, btn_h)
        pygame.draw.rect(surface, theme.PANEL_BG, self.open_taskmgr_button_rect, border_radius=5)
        pygame.draw.rect(surface, self.chrome_accent(), self.open_taskmgr_button_rect, width=1, border_radius=5)
        # Longest button label in the whole toolbar -- the one that
        # actually visibly broke (spilled past its own rounded-rect
        # border, though still within this panel's column) on a narrow
        # PROCESSOS column before this was truncated like every other
        # button label already was.
        label_text = self._truncate(self.font_row, "Gerenciador de Tarefas", self.open_taskmgr_button_rect.width - 12)
        label = self._text(self.font_row, label_text, self.chrome_accent())
        surface.blit(label, label.get_rect(center=self.open_taskmgr_button_rect.center))

    # ---- spectrum / meter ------------------------------------------------

    def _draw_spectrum(self, surface, rect):
        pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=8)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1, border_radius=8)

        n = len(self.smoothed_bands)
        if n == 0:
            return

        plot = pygame.Rect(
            rect.x + _LEFT_MARGIN, rect.y + _TOP_MARGIN,
            rect.width - _LEFT_MARGIN - 10, rect.height - _TOP_MARGIN - _BOTTOM_MARGIN,
        )
        baseline = plot.bottom
        max_h = plot.height

        self._draw_watermark(surface, rect)
        self._draw_db_grid(surface, rect, plot, baseline, max_h)
        self._draw_bars(surface, plot, baseline, max_h, n)
        self._draw_freq_labels(surface, plot, baseline)
        self._draw_clip_banner(surface, rect)

        if not self.spectrum_available:
            msg = self.spectrum_error or "Áudio indisponível"
            img = self._text(self.font_md, f"⚠ {msg}", theme.CRIT)
            surface.blit(img, (plot.centerx - img.get_width() // 2, plot.centery - img.get_height() // 2))

    def _draw_watermark(self, surface, rect):
        # Fixed wine, not themed -- only the EQ/LUFS meter follow the
        # color cycle, everything else (including this) stays put.
        watermark_color = self.chrome_accent()
        key = (rect.width, rect.height)
        if key != self._watermark_key:
            size = max(28, int(rect.height * 0.34))
            font_path = _watermark_font_path()
            try:
                font = pygame.font.Font(str(font_path), size) if font_path \
                    else pygame.font.SysFont("segoe ui", size, bold=True)
            except Exception:
                font = pygame.font.SysFont("segoe ui", size, bold=True)
            text_surf = font.render("brndz.wav", True, watermark_color).convert_alpha()

            # Small soundwave glyph under the wordmark -- the same bar
            # heights as brndz_icon.svg's mark (half-heights 0/30/58/24/74/
            # 24/58/30/0, normalized here), drawn as vector bars rather
            # than loaded from the icon file, same reasoning as the
            # gear/plus settings icon: this UI never loads image assets for
            # chrome, everything is pygame primitives.
            wave_h = max(12, int(size * 0.24))
            wave_w = int(text_surf.get_width() * 0.62)
            heights = [0.0, 0.405, 0.784, 0.324, 1.0, 0.324, 0.784, 0.405, 0.0]
            bars = len(heights)
            bar_w = max(2, int(wave_w / (bars * 1.8)))
            gap = (wave_w - bars * bar_w) / max(1, bars - 1)
            wave_surf = pygame.Surface((wave_w, wave_h), pygame.SRCALPHA)
            for i, hfrac in enumerate(heights):
                bh = max(2, int(wave_h * max(0.06, hfrac)))
                bx = int(i * (bar_w + gap))
                by = (wave_h - bh) // 2
                pygame.draw.rect(wave_surf, watermark_color, pygame.Rect(bx, by, bar_w, bh), border_radius=bar_w // 2)

            combined_h = text_surf.get_height() + 6 + wave_h
            combined_w = max(text_surf.get_width(), wave_w)
            combined = pygame.Surface((combined_w, combined_h), pygame.SRCALPHA)
            combined.blit(text_surf, ((combined_w - text_surf.get_width()) // 2, 0))
            combined.blit(wave_surf, ((combined_w - wave_w) // 2, text_surf.get_height() + 6))
            # Wine-red carries less luminance than the old white did at the
            # same alpha, so it needs a touch more to read at a glance.
            # Applied once to the whole combined image so text and
            # soundwave stay equally translucent relative to each other.
            combined.set_alpha(34)

            self._watermark_surf = combined
            self._watermark_key = key
        surf = self._watermark_surf
        # Horizontally dead-center (previously offset toward the
        # high-frequency side, which read as misaligned rather than
        # deliberate). Kept in the upper third vertically, not dead-center
        # top-to-bottom -- bars grow from the bottom, so that's where it
        # stays clearest of the loudest material instead of fighting it.
        pos = (rect.x + (rect.width - surf.get_width()) // 2, rect.y + int(rect.height * 0.26) - surf.get_height() // 2)
        surface.blit(surf, pos)

    def _draw_db_grid(self, surface, rect, plot, baseline, max_h):
        cfg = self.cfg
        span = cfg.eq_ceil_db - cfg.eq_floor_db
        for db in self._db_grid_marks():
            norm = (db - cfg.eq_floor_db) / span
            y = int(baseline - norm * max_h)
            pygame.draw.line(surface, theme.PANEL_BORDER, (plot.x, y), (plot.right, y), width=1)
            label = self._text(self.font_xs, f"{db:.0f}", theme.TEXT_LABEL)
            surface.blit(label, (rect.x + 4, y - label.get_height() // 2))

    def _get_ladder_texture(self, bar_w: int, max_h: int):
        """Pre-rendered full-height LED ladder for one bar column, cached by
        (bar_w, max_h, theme). Built once (and again only on resize or a
        theme change via the "C" button) instead of up to _N_SEGMENTS
        pygame.draw.rect calls per bar per frame -- with 24+ bands at 60fps
        that was ~700 rect fills/frame and the single biggest CPU cost in
        the whole renderer.
        """
        if self.eq_color_theme != self._row_colors_theme:
            stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)
            self._row_colors = [_bar_color_themed((seg + 0.5) / _N_SEGMENTS, stops) for seg in range(_N_SEGMENTS)]
            self._row_colors_theme = self.eq_color_theme

        key = (bar_w, max_h, self.eq_color_theme)
        if key == self._ladder_key:
            return self._ladder_surf, self._ladder_seg_y

        surf = pygame.Surface((max(1, bar_w), max(1, max_h)), pygame.SRCALPHA)
        seg_h = (max_h - _SEGMENT_GAP * (_N_SEGMENTS - 1)) / _N_SEGMENTS
        seg_y = []  # top-of-segment y, texture-local (0 = top of column)
        for seg in range(_N_SEGMENTS):
            y = max_h - (seg + 1) * seg_h - seg * _SEGMENT_GAP
            seg_y.append(y)
            rect = pygame.Rect(0, int(y), surf.get_width(), math.ceil(seg_h))
            pygame.draw.rect(surf, self._row_colors[seg], rect, border_radius=1)

        self._ladder_surf = surf
        self._ladder_seg_y = seg_y
        self._ladder_key = key
        return surf, seg_y

    def _draw_bars(self, surface, plot, baseline, max_h, n):
        now = time.time()
        gap = 3
        bar_w = (plot.width - gap * (n + 1)) / n
        bar_w_i = max(1, int(bar_w))
        max_h_i = max(1, int(max_h))
        seg_h = (max_h - _SEGMENT_GAP * (_N_SEGMENTS - 1)) / _N_SEGMENTS

        texture, seg_y = self._get_ladder_texture(bar_w_i, max_h_i)

        for i in range(n):
            x = plot.x + gap + i * (bar_w + gap)
            value = self.smoothed_bands[i]
            lit = int(round(value * _N_SEGMENTS))

            # Only the lit (bottom) portion of the pre-rendered texture gets
            # blitted; the rest of the column is left showing whatever's
            # already drawn underneath (panel bg + watermark), same look as
            # the old per-segment draw but as a single blit instead of many.
            if lit > 0:
                crop_top = max(0, int(seg_y[min(lit, _N_SEGMENTS) - 1]))
                src = pygame.Rect(0, crop_top, bar_w_i, max_h_i - crop_top)
                surface.blit(texture, (int(x), plot.y + crop_top), area=src)

            peak = self.peak_level[i]
            if peak > 0.01:
                peak_seg = min(_N_SEGMENTS - 1, int(peak * _N_SEGMENTS))
                peak_y = baseline - (peak_seg + 1) * seg_h - peak_seg * _SEGMENT_GAP
                cap_rect = pygame.Rect(int(x), int(peak_y), bar_w_i, max(2, int(seg_h * 0.5)))
                pygame.draw.rect(surface, theme.TEXT, cap_rect, border_radius=1)

            if now < self.clip_until[i]:
                clip_rect = pygame.Rect(int(x), plot.y - 12, bar_w_i, 8)
                pygame.draw.rect(surface, theme.BAR_CLIP, clip_rect, border_radius=1)

    def _draw_freq_labels(self, surface, plot, baseline):
        n = len(self.smoothed_bands)
        gap = 3
        bar_w = (plot.width - gap * (n + 1)) / n
        for idx, hz in self._label_band_indices.items():
            if idx >= n:
                continue
            x_center = plot.x + gap + idx * (bar_w + gap) + bar_w / 2
            label = self._text(self.font_xs, _freq_label(hz), theme.TEXT_LABEL)
            surface.blit(label, (x_center - label.get_width() / 2, baseline + 4))
            pygame.draw.line(surface, theme.PANEL_BORDER, (x_center, baseline), (x_center, baseline + 3))

    def _draw_clip_banner(self, surface, rect):
        now = time.time()
        if not (self.clip_until > now).any():
            return
        label = self._text(self.font_row, "CLIP", theme.BAR_CLIP)
        surface.blit(label, (rect.right - label.get_width() - 12, rect.y + 4))

    def _draw_events(self, surface, rect):
        pygame.draw.rect(surface, theme.PANEL_BG, rect)
        pygame.draw.line(surface, theme.PANEL_BORDER, (rect.x, rect.y), (rect.right, rect.y))

        now = time.time()
        self._event_flashes = [e for e in self._event_flashes if e["expire"] > now]

        y = rect.y + 6
        for e in self._event_flashes[-3:]:
            color = self.event_text_color(e["severity"] == "crit")
            img = self._text(self.font_row, e["msg"], color)
            surface.blit(img, (rect.x + 10, y))
            y += 18

    # ---- compact/overlay mode --------------------------------------------
    #
    # A separate, much simpler draw path: solid colorkey background + flat
    # bars, no rounded corners/antialiasing/translucent watermark anywhere.
    # Colorkey transparency is all-or-nothing per pixel (unlike real alpha),
    # so any softened edge would leave a visible halo where it blended with
    # the magic-color background instead of matching it exactly.

    def draw_compact(self, surface, colorkey):
        self.sync_theme_module()
        surface.fill(colorkey)
        n = len(self.smoothed_bands)
        if n == 0:
            return

        w, h = surface.get_size()
        stops = _EQ_COLOR_STOPS.get(self.eq_color_theme, _COLOR_STOPS)

        # LUFS VU strip, left edge -- deliberately a *solid* single tone
        # below -19 (not the EQ bars' own low->high gradient, which the
        # user found blended into the multicolor EQ bars next to it).
        # Simplified two-point version of the main panel's target/ceiling
        # system, since compact mode has no room for the adjustable
        # marker's +/- stepper: -19 (fixed) is where the fill above it
        # jumps to a deliberately strong, unmissable tone shift within
        # the same theme; -9 (fixed, matches the main meter's ceiling)
        # turns the whole fill scarlet. Only these 2 tick marks, no
        # numbers -- compact mode stays text-free.
        lufs_w = 12
        eq_x0 = lufs_w + 6
        connected = bool(self.spectrum_available and self.output_level_db is not None)
        lufs = self._out_lufs_smoothed if connected else None
        floor_db, ceil_db = self.cfg.eq_floor_db, self.cfg.eq_ceil_db
        span = max(1e-6, ceil_db - floor_db)
        if lufs is not None:
            y_current = int(h - h * max(0.0, min(1.0, (lufs - floor_db) / span)))
            fill_h = h - y_current
            if fill_h > 0:
                if lufs >= self._LUFS_RED_DB:
                    pygame.draw.rect(surface, theme.BAR_CLIP, pygame.Rect(2, y_current, lufs_w, fill_h))
                else:
                    pygame.draw.rect(surface, _bar_color_themed(0.4, stops), pygame.Rect(2, y_current, lufs_w, fill_h))
                    if lufs >= self._LUFS_COMPACT_TONE_DB:
                        tone_frac = max(0.0, min(1.0, (self._LUFS_COMPACT_TONE_DB - floor_db) / span))
                        y_tone = int(h - h * tone_frac)
                        tone_rect = pygame.Rect(2, y_current, lufs_w, max(0, y_tone - y_current))
                        if tone_rect.height > 0:
                            pygame.draw.rect(surface, _bar_color_themed(1.0, stops), tone_rect)

        for ref_db in (self._LUFS_COMPACT_TONE_DB, self._LUFS_RED_DB):
            if ref_db < floor_db or ref_db > ceil_db:
                continue
            ty = int(h - h * max(0.0, min(1.0, (ref_db - floor_db) / span)))
            pygame.draw.line(surface, theme.TEXT, (2, ty), (2 + lufs_w, ty), width=1)

        gap = 2
        bar_area_w = w - eq_x0
        bar_w = (bar_area_w - gap * (n + 1)) / n
        baseline = h

        for i, v in enumerate(self.smoothed_bands):
            bar_h = max(2, v * h)
            x = eq_x0 + gap + i * (bar_w + gap)
            rect = pygame.Rect(int(x), int(baseline - bar_h), max(1, int(bar_w)), int(bar_h))
            pygame.draw.rect(surface, _bar_color_themed(v, stops), rect)

        if not self.spectrum_available:
            msg = self._text(self.font_xs, "sem áudio", theme.CRIT)
            surface.blit(msg, (eq_x0 + 4, 6))

    def draw_compact_restore_button(self, surface):
        size = 20
        rect = pygame.Rect(1, surface.get_height() - size - 1, size, size)
        pygame.draw.rect(surface, theme.PANEL_BG, rect)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1)
        label = self._text(self.font_md, "x", theme.TEXT)
        surface.blit(label, label.get_rect(center=rect.center))
        return rect

    def draw_theme_button(self, surface, rect):
        """"C" button -- used in both the main panel header (next to the
        status widget) and compact mode (next to the close button). Cycles
        eq_color_theme through _EQ_THEME_ORDER on click (main.py handles
        the click, this only draws); caller positions `rect`.
        """
        pygame.draw.rect(surface, theme.PANEL_BG, rect)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1)
        self._draw_palette_icon(surface, rect)
        return rect

    def draw_compact_toggle_icon_button(self, surface, rect):
        """Small icon button replacing the "Modo compacto" text button --
        EQ-bars glyph, after the user's reference image."""
        pygame.draw.rect(surface, theme.PANEL_BG, rect)
        pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1)
        self._draw_eq_icon(surface, rect)
        return rect

    def draw_topmost_toggle_icon_button(self, surface, rect, active: bool):
        """Small icon button replacing the "Sempre no topo" text button --
        padlock glyph, after the user's reference image. `active`
        (already pinned) gets the accent border, same convention as
        every other toggle button in this header."""
        pygame.draw.rect(surface, theme.PANEL_BG, rect)
        pygame.draw.rect(surface, self.chrome_accent() if active else theme.PANEL_BORDER, rect, width=2 if active else 1)
        self._draw_lock_icon(surface, rect)
        return rect

    def cycle_eq_color_theme(self):
        idx = _EQ_THEME_ORDER.index(self.eq_color_theme) if self.eq_color_theme in _EQ_THEME_ORDER else 0
        self.eq_color_theme = _EQ_THEME_ORDER[(idx + 1) % len(_EQ_THEME_ORDER)]
