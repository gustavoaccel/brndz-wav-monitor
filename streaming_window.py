"""STREAMING: standalone pygame window showing live YouTube stream metrics
-- concurrent viewers (with a trend graph), likes, total views, and
comments. Launched as a separate process by the main monitor's STREAMING
button, same architecture as network_mapper.py/the old mixer_window.py
(own window, own event loop, no window-model conflict with the main
app's pygame instance).

Data source: YouTube Data API v3's `videos.list` endpoint, polled on a
background thread. Needs only a free API key (no OAuth) since it only
ever reads public data about a video the user gives us the ID/URL for --
confirmed against several reference implementations that this is the
standard, quota-cheap way to do it (1
unit per call, ~1000 calls for a 4h stream at the default 15s interval,
against a 10,000/day free quota). Deliberately does NOT touch live chat
(a separate, much quota-hungrier API needing much more frequent polling
to feel "live") -- explicit scope decision, confirmed with the user.
"""
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pygame

from avmonitor.config import load_config, default_config_path
from avmonitor.ui import theme
from avmonitor.ui.renderer import apply_theme_to_window
from avmonitor.util import push_latest
from avmonitor import win_native

_CREATE_NO_WINDOW = 0x08000000
_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_HISTORY_MAXLEN = 240  # ~1h of samples at the default 15s poll interval

_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _extract_video_id(text: str) -> Optional[str]:
    """Accepts a bare 11-char video ID or any of the common YouTube URL
    shapes (watch?v=, youtu.be/, /live/, /shorts/, /embed/) and returns
    just the ID, or None if nothing recognizable was pasted."""
    text = text.strip()
    if _BARE_ID_RE.match(text):
        return text
    m = _YOUTUBE_ID_RE.search(text)
    return m.group(1) if m else None


def _resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


@dataclass
class StreamMetrics:
    timestamp: float = 0.0
    connected: bool = False
    error: Optional[str] = None
    title: str = ""
    status: str = "unconfigured"  # unconfigured | live | upcoming | ended | not_live | unknown
    concurrent_viewers: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None


class YouTubeMetricsWorker(threading.Thread):
    """Polls videos.list on its own thread -- one HTTP round-trip per
    cycle, never on the render loop. `video_id`/`api_key` are plain
    attributes (GIL-atomic single-reference swap, same pattern as
    util.DeviceWatcher.current) so the UI can retarget the worker at any
    time without restarting the thread."""

    def __init__(self, out_queue: "queue.Queue[StreamMetrics]", poll_s: float = 15.0):
        super().__init__(name="YouTubeMetricsWorker", daemon=True)
        self.out_queue = out_queue
        self.poll_s = poll_s
        self._video_id: Optional[str] = None
        self._api_key: Optional[str] = None
        self._stop_event = threading.Event()
        self._poke_requested = False

    def set_target(self, video_id: Optional[str], api_key: Optional[str]) -> None:
        self._video_id = video_id
        self._api_key = api_key
        self._poke_requested = True

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            video_id, api_key = self._video_id, self._api_key
            if video_id and api_key:
                snapshot = self._fetch(video_id, api_key)
                push_latest(self.out_queue, snapshot)
            # Sleep in short slices so a target change (set_target) or a
            # stop() request doesn't wait out a stale, possibly 15s-long
            # sleep -- same poke-able pattern as util.DeviceWatcher.
            slept = 0.0
            while slept < self.poll_s and not self._stop_event.is_set():
                if self._poke_requested:
                    self._poke_requested = False
                    break
                time.sleep(0.1)
                slept += 0.1

    def _fetch(self, video_id: str, api_key: str) -> StreamMetrics:
        # Both values are percent-encoded before going into the query
        # string -- video_id is already regex-validated (11 safe chars)
        # by the time it gets here, but api_key comes straight from
        # whatever the user pasted into the WinForms box (main.py's
        # _prompt_text_worker), unvalidated. An unescaped stray
        # character (&, #, a space) could otherwise inject an extra
        # query param or truncate the URL at a fragment, producing a
        # confusing generic error instead of a real HTTP 400.
        params = urllib.parse.urlencode({
            "part": "snippet,liveStreamingDetails,statistics",
            "id": video_id,
            "key": api_key,
        })
        url = f"{_API_URL}?{params}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                reason = body.get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:
                reason = ""
            if reason == "quotaExceeded":
                msg = "Cota diária da API do YouTube excedida"
            elif reason in ("keyInvalid", "badRequest") or e.code == 400:
                msg = "Chave de API inválida"
            elif reason == "accessNotConfigured":
                msg = "API do YouTube não ativada nesse projeto/chave"
            else:
                msg = f"Erro da API do YouTube (HTTP {e.code})"
            return StreamMetrics(timestamp=time.time(), connected=False, error=msg)
        except urllib.error.URLError:
            return StreamMetrics(timestamp=time.time(), connected=False, error="Sem conexão com a internet")
        except Exception as e:
            return StreamMetrics(timestamp=time.time(), connected=False, error=f"Erro: {e}")

        items = data.get("items") or []
        if not items:
            return StreamMetrics(timestamp=time.time(), connected=False, error="Vídeo não encontrado -- confira o link/ID")

        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        live = item.get("liveStreamingDetails")

        broadcast = snippet.get("liveBroadcastContent", "none")
        if broadcast == "live":
            status = "live"
        elif broadcast == "upcoming":
            status = "upcoming"
        elif live is not None:
            status = "ended"  # was a live stream, isn't live/upcoming anymore
        else:
            status = "not_live"  # a regular video, never a livestream

        def _int(d, k):
            v = d.get(k)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return StreamMetrics(
            timestamp=time.time(),
            connected=True,
            error=None,
            title=snippet.get("title", ""),
            status=status,
            concurrent_viewers=_int(live, "concurrentViewers") if live else None,
            view_count=_int(stats, "viewCount"),
            like_count=_int(stats, "likeCount"),
            comment_count=_int(stats, "commentCount"),
        )


def _build_prompt_script(title: str, label: str, default_text: str, masked: bool) -> str:
    """Builds the PowerShell/WinForms script for _prompt_text_worker --
    split out on its own (no subprocess call here) so the generated
    script's syntax can be validated directly against PowerShell's own
    parser without actually popping a dialog up. Themed to match this
    app's own dark palette (BG/PANEL_BG/TEXT/GOLD, inlined as literal
    RGB since a PowerShell subprocess can't import theme.py) instead of
    the default gray Windows look; the API key field additionally masks
    input (UseSystemPasswordChar) since that's credential-shaped even
    though it's stored locally like every other setting in this app's
    own config.json. `TopMost = $true` here alone isn't the whole story
    -- see main()'s reassert-topmost block, which skips reclaiming the
    front spot while one of these prompts is open, or this window's own
    periodic reassertion would shove the prompt back behind it a couple
    seconds later.
    """
    safe_label = label.replace("'", "''").replace("\n", "`r`n")
    safe_title = title.replace("'", "''")
    safe_default = default_text.replace("'", "''")
    mask_line = "$tb.UseSystemPasswordChar = $true; " if masked else ""
    # BG/PANEL_BG/TEXT/GOLD literal RGB values, same as avmonitor/ui/theme.py --
    # a PowerShell subprocess can't import that module, so the palette
    # is inlined here.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "Add-Type -AssemblyName System.Drawing | Out-Null; "
        "$f = New-Object System.Windows.Forms.Form; "
        f"$f.Text = '{safe_title}'; $f.Width = 480; $f.Height = 210; "
        "$f.StartPosition = 'CenterScreen'; $f.TopMost = $true; "
        "$f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false; $f.MinimizeBox = $false; "
        "$f.BackColor = [System.Drawing.Color]::FromArgb(21,19,15); "
        "$lbl = New-Object System.Windows.Forms.Label; "
        f"$lbl.Text = '{safe_label}'; $lbl.AutoSize = $false; "
        "$lbl.Size = New-Object System.Drawing.Size(440,50); "
        "$lbl.Location = New-Object System.Drawing.Point(16,14); "
        "$lbl.ForeColor = [System.Drawing.Color]::FromArgb(242,236,226); "
        "$lbl.Font = New-Object System.Drawing.Font('Segoe UI', 10); "
        "$f.Controls.Add($lbl); "
        "$tb = New-Object System.Windows.Forms.TextBox; "
        "$tb.Location = New-Object System.Drawing.Point(16,68); $tb.Width = 440; "
        "$tb.BackColor = [System.Drawing.Color]::FromArgb(33,29,24); "
        "$tb.ForeColor = [System.Drawing.Color]::FromArgb(242,236,226); "
        "$tb.BorderStyle = 'FixedSingle'; "
        "$tb.Font = New-Object System.Drawing.Font('Consolas', 11); "
        f"{mask_line}"
        f"$tb.Text = '{safe_default}'; $f.Controls.Add($tb); "
        "$ok = New-Object System.Windows.Forms.Button; "
        "$ok.Text = 'OK'; $ok.Size = New-Object System.Drawing.Size(90,30); "
        "$ok.Location = New-Object System.Drawing.Point(276,110); "
        "$ok.BackColor = [System.Drawing.Color]::FromArgb(232,182,78); "
        "$ok.ForeColor = [System.Drawing.Color]::FromArgb(21,19,15); "
        "$ok.FlatStyle = 'Flat'; $ok.FlatAppearance.BorderSize = 0; "
        "$ok.Font = New-Object System.Drawing.Font('Segoe UI', 9, [System.Drawing.FontStyle]::Bold); "
        "$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK; $f.Controls.Add($ok); "
        "$cancel = New-Object System.Windows.Forms.Button; "
        "$cancel.Text = 'Cancelar'; $cancel.Size = New-Object System.Drawing.Size(90,30); "
        "$cancel.Location = New-Object System.Drawing.Point(372,110); "
        "$cancel.BackColor = [System.Drawing.Color]::FromArgb(33,29,24); "
        "$cancel.ForeColor = [System.Drawing.Color]::FromArgb(242,236,226); "
        "$cancel.FlatStyle = 'Flat'; $cancel.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(69,60,48); "
        "$cancel.Font = New-Object System.Drawing.Font('Segoe UI', 9); "
        "$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel; $f.Controls.Add($cancel); "
        "$f.AcceptButton = $ok; $f.CancelButton = $cancel; "
        "$f.Add_Shown({ $tb.Focus(); $tb.SelectAll() }); "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $tb.Text }"
    )
    return script


def _prompt_text_worker(title: str, label: str, default_text: str, masked: bool, result_q: "queue.Queue[str | None]"):
    """Runs on its own thread -- see main.py's _start_folder_browse for
    why a dialog that waits on a human must never block the render loop."""
    script = _build_prompt_script(title, label, default_text, masked)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=180, creationflags=_CREATE_NO_WINDOW,
        )
        text = result.stdout.strip()
        result_q.put(text or None)
    except Exception:
        result_q.put(None)


def _start_text_prompt(title: str, label: str, default_text: str = "", masked: bool = False) -> "queue.Queue[str | None]":
    q: "queue.Queue[str | None]" = queue.Queue(maxsize=1)
    threading.Thread(target=_prompt_text_worker, args=(title, label, default_text, masked, q), daemon=True).start()
    return q


_text_cache: "dict[tuple, pygame.Surface]" = {}
_truncate_cache: "dict[tuple, str]" = {}


def _text(font, text, color):
    key = (id(font), text, color)
    cached = _text_cache.get(key)
    if cached is not None:
        return cached
    img = font.render(text, True, color)
    if len(_text_cache) > 500:
        _text_cache.clear()
    _text_cache[key] = img
    return img


def _truncate(font, text, max_w):
    if max_w <= 0:
        return ""
    key = (id(font), text, max_w)
    cached = _truncate_cache.get(key)
    if cached is not None:
        return cached
    if font.size(text)[0] <= max_w:
        result = text
    else:
        result = text
        while result and font.size(result + "…")[0] > max_w:
            result = result[:-1]
        result = (result + "…") if result else ""
    if len(_truncate_cache) > 500:
        _truncate_cache.clear()
    _truncate_cache[key] = result
    return result


def _draw_button(surface, font, rect, text, accent):
    pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=5)
    pygame.draw.rect(surface, accent, rect, width=1, border_radius=5)
    label_text = _truncate(font, text, rect.width - 10)
    label = _text(font, label_text, accent)
    surface.blit(label, label.get_rect(center=rect.center))


def _format_count(n: Optional[int]) -> str:
    if n is None:
        return "--"
    return f"{n:,}".replace(",", ".")


def _draw_kpi(surface, font_val, font_label, rect, value_text, label_text, color):
    # Truncated to the column's own width -- previously rendered at full
    # width regardless of `rect`, so a narrow window (3 KPI columns
    # sharing it) let long text visibly spill into the neighboring
    # column instead of just clipping to its own space.
    value_text = _truncate(font_val, value_text, rect.width)
    label_text = _truncate(font_label, label_text, rect.width)
    val_img = _text(font_val, value_text, color)
    surface.blit(val_img, (rect.centerx - val_img.get_width() // 2, rect.y))
    lbl_img = _text(font_label, label_text, theme.TEXT_LABEL)
    surface.blit(lbl_img, (rect.centerx - lbl_img.get_width() // 2, rect.y + val_img.get_height() + 2))


def _draw_graph(surface, rect, history, font_hint):
    pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=6)
    pygame.draw.rect(surface, theme.PANEL_BORDER, rect, width=1, border_radius=6)
    if len(history) < 2:
        empty = _text(font_hint, "aguardando amostras...", theme.TEXT_LABEL)
        surface.blit(empty, empty.get_rect(center=rect.center))
        return
    values = [v for _, v in history]
    lo, hi = 0, max(1, max(values))
    hi = int(hi * 1.15) + 1
    pad = 10
    inner = rect.inflate(-pad * 2, -pad * 2)
    n = len(values)
    points = []
    for i, v in enumerate(values):
        x = inner.x + (i / max(1, n - 1)) * inner.width
        frac = (v - lo) / max(1, hi - lo)
        y = inner.bottom - frac * inner.height
        points.append((x, y))
    if len(points) >= 2:
        pygame.draw.lines(surface, theme.OK, False, points, width=2)
    pygame.draw.circle(surface, theme.OK, (int(points[-1][0]), int(points[-1][1])), 3)


def _status_label_color(status: str):
    return {
        "live": ("AO VIVO", theme.OK),
        "upcoming": ("AGENDADA", theme.WARN),
        "ended": ("ENCERRADA", theme.TEXT_DIM),
        "not_live": ("NÃO É UMA LIVE", theme.WARN),
        "unconfigured": ("NÃO CONFIGURADO", theme.TEXT_LABEL),
    }.get(status, ("--", theme.TEXT_LABEL))


def _get_hwnd():
    try:
        return pygame.display.get_wm_info().get("window")
    except Exception:
        return None


def _clamp_onscreen_if_lost(hwnd):
    """Same recovery net as main.py's own compact mode: if a drag ends
    with the window fully off-screen (no overlap with the primary
    monitor at all), recenter it there instead of leaving it stranded
    with no way back except restarting."""
    if not hwnd:
        return
    screen_size = win_native.get_screen_size()
    if not screen_size:
        return
    sw, sh = screen_size
    l, t, r, b = win_native.get_window_rect(hwnd)
    onscreen = pygame.Rect(0, 0, sw, sh).colliderect(pygame.Rect(l, t, max(1, r - l), max(1, b - t)))
    if not onscreen:
        w, h = max(1, r - l), max(1, b - t)
        win_native.move_window(hwnd, (sw - w) // 2, (sh - h) // 2)


_COMPACT_W = 280
_COMPACT_H = 210
_COMPACT_MIN_W = 230  # tall enough for "AO VIVO" + the HH:MM:SS uptime counter + the 3-button cluster to all fit on the header row without any of them getting dropped -- measured directly (49px + 56px text against a ~230-70=160px left side), not guessed
_FULL_MIN_W = 560  # the 4 header buttons alone (compact toggle + Assistir + API Key + Vídeo) need ~414px -- this leaves the "STREAMING" title a comfortable ~146px, on top of the title's own truncation as a second line of defense
_FULL_MIN_H = 340  # header(62) + viewers block(~120) + graph floor(40) + gap(16) + KPI row(50) + gap(16) + title(18)
_COMPACT_MIN_H = 150
_RESIZE_GRIP = 16


def _enter_compact_mode(is_topmost: bool, size=None):
    """A small frameless (NOFRAME) window, centered where the full window
    was -- but with a real, solid background (theme.PANEL_BG), unlike the
    main app's own compact EQ mode. Colorkey transparency was tried here
    first (matching the EQ's technique exactly) and reverted: colorkey
    only works cleanly for solid vector shapes (bars, icons) with no
    anti-aliasing -- this window is mostly *text* (viewer count, channel
    name, labels), and antialiased glyph edges blend against the
    colorkey color at less than full opacity, so those edge pixels don't
    become transparent and show up as a colored halo/smudge around every
    letter against whatever's behind the window on the real desktop.
    Confirmed as the reported "borrado, não dá pra ler" complaint after
    the first version shipped -- a solid background sidesteps the whole
    class of bug instead of trying to avoid anti-aliasing everywhere.
    """
    old_hwnd = _get_hwnd()
    center = None
    if old_hwnd:
        l, t, r, b = win_native.get_window_rect(old_hwnd)
        center = ((l + r) // 2, (t + b) // 2)
    w, h = size or (_COMPACT_W, _COMPACT_H)
    screen = pygame.display.set_mode((w, h), pygame.NOFRAME)
    hwnd = _get_hwnd()
    if hwnd:
        if center:
            win_native.move_window(hwnd, center[0] - w // 2, center[1] - h // 2)
        # Unconditional, not gated on `is_topmost` (the *previous*,
        # full-window mirrored state) -- compact mode always wants
        # topmost regardless, same as the main app's own compact EQ (see
        # main()'s reassert-topmost block for the ongoing/periodic half
        # of this, which is what actually keeps it there).
        win_native.set_always_on_top(hwnd, True)
    return screen, hwnd


def _exit_compact_mode(normal_size, is_topmost: bool):
    screen = pygame.display.set_mode(normal_size, pygame.RESIZABLE)
    hwnd = _get_hwnd()
    if hwnd and is_topmost:
        win_native.set_always_on_top(hwnd, True)
    return screen, hwnd


def _draw_compact_toggle_icon(surface, rect, accent):
    """Small "shrink to widget" icon for the full window's header --
    a rectangle with a smaller rectangle nested inside it, corners
    pulled inward -- reads as "minimize into a mini widget" without
    needing a text label. Same minimal-vector-primitives style as the
    icons in renderer.py (plain lines/rects, nothing that collapses at
    small sizes)."""
    pygame.draw.rect(surface, accent, rect, width=1, border_radius=3)
    inner = rect.inflate(-8, -8)
    pygame.draw.rect(surface, accent, inner, width=1, border_radius=2)


def _draw_restore_icon(surface, rect, accent):
    """X-shaped restore-to-full-window icon for compact mode's corner
    button -- same shape/weight as the main app's own compact-mode X."""
    pad = 6
    pygame.draw.line(surface, accent, (rect.x + pad, rect.y + pad), (rect.right - pad, rect.bottom - pad), width=2)
    pygame.draw.line(surface, accent, (rect.right - pad, rect.y + pad), (rect.x + pad, rect.bottom - pad), width=2)


def _draw_watch_icon(surface, rect, accent):
    """Play-triangle icon for the "Assistir" button -- universal symbol,
    no font/glyph coverage risk."""
    pad = 5
    x0, y0 = rect.x + pad, rect.y + pad
    x1 = rect.right - pad
    ymid = rect.centery
    y1 = rect.bottom - pad
    pygame.draw.polygon(surface, accent, [(x0, y0), (x1, ymid), (x0, y1)])


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _draw_move_icon(surface, rect, accent):
    """4 diagonal arrows from center -- the classic "move" glyph, same
    shape as the main app's own compact-mode move button."""
    cx, cy = rect.center
    r_out, r_in = rect.width * 0.42, rect.width * 0.16
    for angle_deg in (45, 135, 225, 315):
        a = math.radians(angle_deg)
        dx, dy = math.cos(a), math.sin(a)
        x0, y0 = cx + dx * r_in, cy + dy * r_in
        x1, y1 = cx + dx * r_out, cy + dy * r_out
        pygame.draw.line(surface, accent, (x0, y0), (x1, y1), width=2)
        # small arrowhead
        perp = (-dy, dx)
        head = 3
        p1 = (x1 - dx * head + perp[0] * head, y1 - dy * head + perp[1] * head)
        p2 = (x1 - dx * head - perp[0] * head, y1 - dy * head - perp[1] * head)
        pygame.draw.polygon(surface, accent, [(x1, y1), p1, p2])


def _draw_resize_grip(surface, rect, accent):
    """Diagonal-lines resize handle, bottom-right corner -- the standard
    "drag to resize" convention (matches the grip most native apps use
    in that corner)."""
    for offset in (4, 9, 14):
        x = rect.right - offset
        y = rect.bottom - offset
        if x > rect.x and y > rect.y:
            pygame.draw.line(surface, accent, (rect.right - 2, y), (x, rect.bottom - 2), width=2)


def _draw_compact(surface, latest, accent, restore_rect, move_rect, buttons_visible, resize_rect,
                   uptime_text="", watch_rect=None, has_video=False):
    w, h = surface.get_size()
    surface.fill(theme.PANEL_BG)
    pygame.draw.rect(surface, theme.PANEL_BORDER, pygame.Rect(0, 0, w, h), width=1)

    font_status = pygame.font.SysFont(theme.FONT_NAME, 12, bold=True)
    font_title = pygame.font.SysFont(theme.FONT_NAME, 10)
    font_num = pygame.font.SysFont(theme.FONT_NAME, min(34, max(18, h // 6)), bold=True)
    font_label = pygame.font.SysFont(theme.FONT_NAME, 10)
    font_likes = pygame.font.SysFont(theme.FONT_NAME, 12, bold=True)

    if latest.error:
        status_text, status_color = "ERRO", theme.CRIT
    else:
        status_text, status_color = _status_label_color(latest.status)
    pygame.draw.circle(surface, status_color, (14, 15), 4)
    status_img = _text(font_status, status_text, status_color)
    surface.blit(status_img, (24, 9))

    # Leftmost edge of the button cluster -- whichever of the (up to 3)
    # corner buttons is furthest left, so the uptime text truncates
    # before it regardless of how many buttons are actually present.
    cluster_left = min(r.x for r in (restore_rect, move_rect, watch_rect) if r is not None)
    if uptime_text:
        # "Tempo Online" -- just the counter, no label, right next to the
        # status text, same HH:MM:SS format as the main window's own
        # uptime widget (main.py's session_start_time/_draw_status_widget).
        uptime_img = _text(font_status, uptime_text, theme.TEXT_LABEL)
        uptime_x = 24 + status_img.get_width() + 10
        if uptime_x + uptime_img.get_width() < cluster_left - 4:
            surface.blit(uptime_img, (uptime_x, 9))

    # The restore ("voltar") button stays visible always -- unlike the
    # move/watch icons, this is the only way back to the full window, and
    # this compact widget is new enough that hover-only discovery (fine
    # for the main app's own compact EQ, which the user already knows
    # well) wasn't obvious here. Explicit request after the first version.
    _draw_restore_icon(surface, restore_rect, accent)
    if buttons_visible:
        _draw_move_icon(surface, move_rect, accent)
        if watch_rect is not None:
            _draw_watch_icon(surface, watch_rect, accent if has_video else theme.TEXT_LABEL)

    # Title, viewers, and likes are always shown, at every size down to
    # _COMPACT_MIN_H -- an earlier version hid title/likes below a height
    # threshold, which the user explicitly rejected ("isto não deve
    # acontecer, as informações devem ficar visíveis de alguma forma").
    # Font sizes/row heights below are tuned to fit comfortably even at
    # the minimum size, not just at the default.
    y = 26
    if latest.title:
        title_text = _truncate(font_title, latest.title, w - 20)
        surface.blit(_text(font_title, title_text, theme.TEXT_LABEL), (10, y))
        y += 14

    likes_block_h = 30
    content = pygame.Rect(0, y, w, max(0, h - y - likes_block_h))
    viewers_text = _format_count(latest.concurrent_viewers) if latest.status == "live" else "--"
    val_img = font_num.render(viewers_text, True, theme.TEXT)
    val_y = content.y + max(0, (content.height - val_img.get_height() - 14) // 2)
    surface.blit(val_img, (w // 2 - val_img.get_width() // 2, val_y))
    lbl_img = _text(font_label, "espectadores", theme.TEXT_LABEL)
    surface.blit(lbl_img, (w // 2 - lbl_img.get_width() // 2, val_y + val_img.get_height() + 2))

    # Same number-above-label convention as the viewers block above (and
    # as the full window's KPI row) -- no heart glyph or other symbol
    # relying on font coverage, same caution this project already
    # applies everywhere else (see session_log's log-box dot instead of
    # a checkmark character).
    likes_val = _text(font_likes, _format_count(latest.like_count), theme.GOLD)
    surface.blit(likes_val, (w // 2 - likes_val.get_width() // 2, h - likes_block_h))
    likes_lbl = _text(font_label, "curtidas", theme.TEXT_LABEL)
    surface.blit(likes_lbl, (w // 2 - likes_lbl.get_width() // 2, h - likes_block_h + likes_val.get_height()))

    _draw_resize_grip(surface, resize_rect, accent)


def main():
    # See main.py's own comment on this same substitution -- pygame.init()
    # opens a real audio-output stream via pygame.mixer that this app
    # never uses.
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_caption("brndz.wav — STREAMING")
    try:
        pygame.display.set_icon(pygame.image.load(_resource_path("brndz_icon_512.png")))
    except Exception:
        pass
    screen = pygame.display.set_mode((640, 520), pygame.RESIZABLE)
    clock = pygame.time.Clock()

    # Always-on-top mirrors the main app's own "sempre no topo" toggle
    # live -- this window has no toggle of its own, explicit request:
    # topmost ONLY while the main app's is active, tracked in real time
    # (not a one-shot read at launch) by asking the OS directly whether
    # the main window currently carries WS_EX_TOPMOST. No IPC or config
    # polling needed for that -- see win_native.is_window_topmost().
    hwnd = _get_hwnd()
    is_topmost = False
    reassert_topmost_at = 0.0

    font_title = pygame.font.SysFont("segoe ui", 18, bold=True)
    font_btn = pygame.font.SysFont(theme.FONT_NAME, 12, bold=True)
    font_big = pygame.font.SysFont(theme.FONT_NAME, 44, bold=True)
    font_kpi = pygame.font.SysFont(theme.FONT_NAME, 22, bold=True)
    font_label = pygame.font.SysFont(theme.FONT_NAME, 12)
    font_status = pygame.font.SysFont(theme.FONT_NAME, 13, bold=True)
    font_hint = pygame.font.SysFont(theme.FONT_NAME, 12)

    cfg = load_config([])
    cfg_path = default_config_path()
    # Opens already matching whichever EQ color palette was last
    # selected in the main app (persisted to config.json on every theme
    # change there) -- a one-shot read, not a live sync; see
    # renderer.apply_theme_to_window()'s docstring.
    apply_theme_to_window(cfg.eq_color_theme)

    api_key_btn_rect = pygame.Rect(0, 0, 130, 26)
    video_btn_rect = pygame.Rect(0, 0, 130, 26)
    watch_btn_rect = pygame.Rect(0, 0, 90, 26)
    compact_toggle_rect = pygame.Rect(0, 0, 24, 24)

    compact_mode = False
    normal_window_size = (640, 520)
    compact_size = (_COMPACT_W, _COMPACT_H)  # remembers the last size across enter/exit within this session
    # Geometry recomputed every frame from the current window size (see
    # the render section) -- these starting values only matter before
    # the first compact frame draws.
    compact_restore_rect = pygame.Rect(0, 0, 18, 18)
    compact_move_rect = pygame.Rect(0, 0, 18, 18)
    compact_watch_rect = pygame.Rect(0, 0, 18, 18)
    compact_resize_rect = pygame.Rect(0, 0, _RESIZE_GRIP, _RESIZE_GRIP)
    dragging_window = False
    drag_offset = (0, 0)
    resizing_window = False
    resize_start_mouse = (0, 0)
    resize_start_size = (0, 0)

    snapshot_q: "queue.Queue[StreamMetrics]" = queue.Queue(maxsize=1)
    worker = YouTubeMetricsWorker(snapshot_q)
    worker.start()
    if cfg.youtube_video_id and cfg.youtube_api_key:
        worker.set_target(cfg.youtube_video_id, cfg.youtube_api_key)

    latest = StreamMetrics(
        connected=False,
        status="unconfigured" if not (cfg.youtube_video_id and cfg.youtube_api_key) else "unknown",
    )
    history: "deque[tuple[float, int]]" = deque(maxlen=_HISTORY_MAXLEN)
    session_start_time = time.time()

    api_key_prompt_q = None
    video_prompt_q = None

    running = True
    while running:
        clock.tick(30)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            elif event.type == pygame.VIDEORESIZE and not compact_mode:
                # Same pattern as main.py's own window -- without this,
                # dragging the OS border resizes the real window but
                # pygame's internal drawing surface never follows, so the
                # layout keeps computing against the *old* w/h while
                # actually rendering into the new (differently sized)
                # window -- exactly what "elementos se sobrepondo" during
                # a resize looks like. This full window was missing this
                # handler entirely (compact mode's own resize goes through
                # a different, explicit set_mode() path on drag, unaffected).
                new_w, new_h = max(_FULL_MIN_W, event.w), max(_FULL_MIN_H, event.h)
                screen = pygame.display.set_mode((new_w, new_h), pygame.RESIZABLE)
                normal_window_size = (new_w, new_h)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if compact_mode:
                    if compact_restore_rect.collidepoint(event.pos):
                        compact_size = screen.get_size()
                        compact_mode = False
                        screen, hwnd = _exit_compact_mode(normal_window_size, is_topmost)
                    elif compact_resize_rect.collidepoint(event.pos):
                        resizing_window = True
                        resize_start_mouse = win_native.get_cursor_pos()
                        resize_start_size = screen.get_size()
                    elif compact_watch_rect.collidepoint(event.pos) and cfg.youtube_video_id:
                        webbrowser.open(_watch_url(cfg.youtube_video_id))
                    else:
                        # No title bar to drag by -- same technique as
                        # main.py's own compact mode: track the grab
                        # offset in screen coordinates, reposition the
                        # window under the cursor on every MOUSEMOTION.
                        # (Clicking the move icon itself falls through to
                        # here too -- it's a visual affordance, same as
                        # main.py's own compact-mode move button.)
                        dragging_window = True
                        if hwnd:
                            cx, cy = win_native.get_cursor_pos()
                            wx, wy, _, _ = win_native.get_window_rect(hwnd)
                            drag_offset = (cx - wx, cy - wy)
                elif compact_toggle_rect.collidepoint(event.pos):
                    normal_window_size = screen.get_size()
                    compact_mode = True
                    screen, hwnd = _enter_compact_mode(is_topmost, compact_size)
                    is_topmost = True  # compact mode always claims topmost immediately, not just on the next 1.5s reassert cycle
                elif watch_btn_rect.collidepoint(event.pos) and cfg.youtube_video_id:
                    webbrowser.open(_watch_url(cfg.youtube_video_id))
                elif api_key_btn_rect.collidepoint(event.pos) and api_key_prompt_q is None:
                    api_key_prompt_q = _start_text_prompt(
                        "Chave de API do YouTube",
                        "Cole sua API Key da YouTube Data API v3\n(console.cloud.google.com, gratuita):",
                        cfg.youtube_api_key, masked=True,
                    )
                elif video_btn_rect.collidepoint(event.pos) and video_prompt_q is None:
                    video_prompt_q = _start_text_prompt(
                        "Vídeo da transmissão",
                        "Cole o link ou ID do vídeo da live no YouTube:",
                        cfg.youtube_video_id, masked=False,
                    )
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if compact_mode:
                    dragging_window = False
                    resizing_window = False
                    _clamp_onscreen_if_lost(hwnd)
            elif event.type == pygame.MOUSEMOTION:
                if compact_mode and dragging_window and hwnd:
                    cx, cy = win_native.get_cursor_pos()
                    win_native.move_window(hwnd, cx - drag_offset[0], cy - drag_offset[1])
                elif compact_mode and resizing_window:
                    cx, cy = win_native.get_cursor_pos()
                    new_w = max(_COMPACT_MIN_W, resize_start_size[0] + (cx - resize_start_mouse[0]))
                    new_h = max(_COMPACT_MIN_H, resize_start_size[1] + (cy - resize_start_mouse[1]))
                    if (new_w, new_h) != screen.get_size():
                        # Resizing through pygame's own API (not a raw
                        # SetWindowPos) so its internal surface size stays
                        # in sync with the real OS window -- a size change
                        # applied only at the Win32 level would leave
                        # pygame still drawing to the *old* buffer size.
                        screen = pygame.display.set_mode((new_w, new_h), pygame.NOFRAME)
                        hwnd = _get_hwnd()
                        if hwnd and is_topmost:
                            win_native.set_always_on_top(hwnd, True)

        if compact_mode and (dragging_window or resizing_window) and not win_native.is_left_button_down():
            # Safety net -- see main.py's own compact-mode drag for why:
            # a MOUSEBUTTONUP outside this small frameless window's
            # bounds isn't reliably delivered, so polling the real OS
            # button state once a frame self-corrects regardless.
            dragging_window = False
            resizing_window = False
            _clamp_onscreen_if_lost(hwnd)

        if api_key_prompt_q is not None:
            try:
                result = api_key_prompt_q.get_nowait()
                api_key_prompt_q = None
                if result is not None:
                    cfg.youtube_api_key = result.strip()
                    cfg.save(cfg_path)
                    if cfg.youtube_video_id:
                        worker.set_target(cfg.youtube_video_id, cfg.youtube_api_key)
            except queue.Empty:
                pass
        if video_prompt_q is not None:
            try:
                result = video_prompt_q.get_nowait()
                video_prompt_q = None
                if result is not None:
                    vid = _extract_video_id(result)
                    if vid:
                        cfg.youtube_video_id = vid
                        cfg.save(cfg_path)
                        history.clear()
                        if cfg.youtube_api_key:
                            worker.set_target(vid, cfg.youtube_api_key)
            except queue.Empty:
                pass

        # Skip reclaiming the front of the topmost band while either a
        # text-prompt dialog (API key/video, both TopMost themselves) is
        # open -- otherwise this window's own periodic reassertion would
        # shove it back behind a couple seconds later. Same "most
        # recently opened window wins" principle as main.py deferring to
        # this window's own topmost.
        prompt_open = api_key_prompt_q is not None or video_prompt_q is not None
        if not prompt_open and hwnd and time.time() >= reassert_topmost_at:
            reassert_topmost_at = time.time() + 1.5
            # Reads main.py's own always_on_top/compact_mode_active state
            # from config.json rather than querying its live window style
            # cross-process (win_native.is_window_topmost() +
            # find_window_containing()) -- that approach was tried first
            # and confirmed unreliable in the field: this window would
            # lose topmost after certain OS-level window-manager
            # disruptions (reported directly -- switching to another
            # app's fullscreen video made it drop behind and never
            # recover) and not the EQ compact overlay, which asserts
            # unconditionally with no cross-process query at all. See
            # Config.always_on_top's docstring for the full reasoning.
            # Only the main window's mere *existence* is still checked
            # live (a plain title lookup, not a style-bit read) -- a
            # cheap sanity net against a stale flag left behind by an
            # unclean shutdown while main.py wasn't actually running.
            main_hwnd = win_native.find_window_containing("brndz.wav Monitor")
            if main_hwnd is None:
                wants_topmost = False
                yield_front_to_eq = False
            else:
                live_cfg = load_config([])
                # Real bug fixed here: this used to multiply in
                # `and not live_cfg.compact_mode_active` (for both compact
                # and full-window mode) on the theory that dropping out of
                # the topmost band entirely would rank this window "below
                # EQ". But Windows only has a binary topmost/not-topmost
                # distinction, no sub-priority levels -- dropping out
                # didn't rank it below EQ specifically, it sank it below
                # EVERY window, including an ordinary browser tab or
                # someone else's fullscreen video, whenever the EQ compact
                # overlay merely happened to be active at the same time --
                # exactly the bug reported ("clico em outra janela... a
                # janela/compacto streaming fecham"). Now this window keeps
                # its own topmost membership for as long as its own
                # condition says so (compact mode: unconditional, same as
                # EQ's own guarantee; full window: mirrors the toggle), and
                # only *yields the front of the topmost band* (skips
                # re-claiming it) while EQ compact is active, letting EQ's
                # own reassert cycle win the front spot instead of the two
                # fighting over it every 1.5s.
                wants_topmost = True if compact_mode else live_cfg.always_on_top
                yield_front_to_eq = live_cfg.compact_mode_active
            if wants_topmost != is_topmost:
                win_native.set_always_on_top(hwnd, wants_topmost)
                is_topmost = wants_topmost
            elif wants_topmost and not yield_front_to_eq and win_native.find_window_containing("Configurações") is None:
                # Windows' own Settings window gets absolute priority
                # regardless of any other window's always-on-top state
                # (see main.py's _pin_settings_window_topmost) -- skip
                # reclaiming the front spot while it's open.
                win_native.set_always_on_top(hwnd, True)

        try:
            latest = snapshot_q.get_nowait()
            if latest.connected and latest.concurrent_viewers is not None:
                history.append((latest.timestamp, latest.concurrent_viewers))
        except queue.Empty:
            pass

        elapsed = max(0.0, time.time() - session_start_time)
        eh, erem = divmod(int(elapsed), 3600)
        emin, esec = divmod(erem, 60)
        uptime_text = f"{eh:02d}:{emin:02d}:{esec:02d}"

        if compact_mode:
            cw, ch = screen.get_size()
            btn_size = 18
            compact_restore_rect = pygame.Rect(cw - btn_size - 4, 4, btn_size, btn_size)
            compact_move_rect = pygame.Rect(compact_restore_rect.x - btn_size - 4, 4, btn_size, btn_size)
            compact_watch_rect = pygame.Rect(compact_move_rect.x - btn_size - 4, 4, btn_size, btn_size)
            compact_resize_rect = pygame.Rect(cw - _RESIZE_GRIP, ch - _RESIZE_GRIP, _RESIZE_GRIP, _RESIZE_GRIP)
            # Same "hover any one of the group reveals all of them"
            # convention as the main app's own compact-mode buttons.
            cluster_rect = compact_restore_rect.unionall([compact_move_rect, compact_watch_rect])
            buttons_visible = cluster_rect.collidepoint(pygame.mouse.get_pos()) if hwnd else False
            _draw_compact(
                screen, latest, theme.GOLD, compact_restore_rect, compact_move_rect, buttons_visible,
                compact_resize_rect, uptime_text, compact_watch_rect, bool(cfg.youtube_video_id),
            )
            pygame.display.flip()
            continue

        w, h = screen.get_size()
        screen.fill(theme.BG)

        # Button rects computed *before* the title so its own width can
        # be clipped to whatever's actually left -- previously drawn
        # first at a fixed position, so a narrow window let the button
        # row's left edge run directly over "STREAMING" instead of
        # either of them yielding space to the other.
        video_btn_rect.topleft = (w - 16 - video_btn_rect.width, 14)
        api_key_btn_rect.topleft = (video_btn_rect.x - 8 - api_key_btn_rect.width, 14)
        watch_btn_rect.topleft = (api_key_btn_rect.x - 8 - watch_btn_rect.width, 14)
        compact_toggle_rect.topleft = (watch_btn_rect.x - 8 - compact_toggle_rect.width, 13)

        title_text = _truncate(font_title, "STREAMING", max(0, compact_toggle_rect.x - 8 - 16))
        title = _text(font_title, title_text, theme.GOLD)
        screen.blit(title, (16, 14))

        if latest.error:
            status_text, status_color = "ERRO", theme.CRIT
        else:
            status_text, status_color = _status_label_color(latest.status)
        status_img = _text(font_status, status_text, status_color)
        pygame.draw.circle(screen, status_color, (20, 46), 4)
        screen.blit(status_img, (30, 40))
        # Same "Tempo Online" counter as the compact widget -- just the
        # HH:MM:SS, no label, right next to the status text. Also clipped
        # against the button row for the same reason as the title above.
        uptime_img = _text(font_status, uptime_text, theme.TEXT_LABEL)
        uptime_x = 30 + status_img.get_width() + 14
        if uptime_x + uptime_img.get_width() < compact_toggle_rect.x - 8:
            screen.blit(uptime_img, (uptime_x, 40))

        _draw_button(screen, font_btn, api_key_btn_rect, "API Key", theme.GOLD)
        _draw_button(screen, font_btn, video_btn_rect, "Vídeo", theme.GOLD)
        _draw_button(screen, font_btn, watch_btn_rect, "Assistir", theme.GOLD if cfg.youtube_video_id else theme.TEXT_LABEL)
        _draw_compact_toggle_icon(screen, compact_toggle_rect, theme.GOLD)

        pygame.draw.line(screen, theme.PANEL_BORDER, (0, 62), (w, 62))

        if not (cfg.youtube_video_id and cfg.youtube_api_key):
            hint1 = _text(font_hint, "Configure a chave de API e o vídeo da live pra começar.", theme.TEXT_DIM)
            hint2 = _text(font_hint, "Botões \"API Key\" e \"Vídeo\" no canto superior direito.", theme.TEXT_LABEL)
            screen.blit(hint1, (w // 2 - hint1.get_width() // 2, h // 2 - 20))
            screen.blit(hint2, (w // 2 - hint2.get_width() // 2, h // 2 + 4))
        elif not latest.connected and latest.error:
            err_img = _text(font_hint, latest.error, theme.CRIT)
            screen.blit(err_img, (w // 2 - err_img.get_width() // 2, h // 2))
        else:
            viewers_text = _format_count(latest.concurrent_viewers) if latest.status == "live" else "--"
            val_img = font_big.render(viewers_text, True, theme.TEXT)
            screen.blit(val_img, (w // 2 - val_img.get_width() // 2, 76))
            lbl_img = _text(font_label, "espectadores agora", theme.TEXT_LABEL)
            screen.blit(lbl_img, (w // 2 - lbl_img.get_width() // 2, 76 + val_img.get_height() + 2))

            # Reserved bottom-up (title row, then the KPI row, then a gap)
            # so the graph's height is *derived* from what's actually left
            # over, rather than each row using a fixed offset from the top
            # independent of the others -- the previous version could
            # overlap the KPI row against the title (or run the KPI row
            # past the window's bottom edge entirely) once the window got
            # resized shorter than it was tuned for, since nothing here
            # accounted for the actual current height.
            kpi_h = 50
            title_h = 18 if latest.title else 0
            graph_top = 76 + val_img.get_height() + 26
            graph_bottom = h - 16 - kpi_h - title_h
            graph_rect = pygame.Rect(16, graph_top, max(1, w - 32), max(40, graph_bottom - graph_top))
            _draw_graph(screen, graph_rect, history, font_hint)

            kpi_y = graph_rect.bottom + 16
            kpi_w = (w - 32) // 3
            _draw_kpi(screen, font_kpi, font_label, pygame.Rect(16, kpi_y, kpi_w, kpi_h),
                      _format_count(latest.like_count), "CURTIDAS", theme.TEXT)
            _draw_kpi(screen, font_kpi, font_label, pygame.Rect(16 + kpi_w, kpi_y, kpi_w, kpi_h),
                      _format_count(latest.view_count), "VIEWS TOTAIS", theme.TEXT)
            _draw_kpi(screen, font_kpi, font_label, pygame.Rect(16 + kpi_w * 2, kpi_y, kpi_w, kpi_h),
                      _format_count(latest.comment_count), "COMENTÁRIOS", theme.TEXT)

            if latest.title:
                title_text = _truncate(font_hint, latest.title, w - 32)
                title_img = _text(font_hint, title_text, theme.TEXT_LABEL)
                screen.blit(title_img, (16, h - title_h + 2))

        pygame.display.flip()

    worker.stop()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
