"""AV Monitor entry point: wires up the capture/monitor threads, the pygame
render loop, and session logging. Auto-starts on launch, no setup screen.
"""
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import winsound
from pathlib import Path

import pygame

from avmonitor.config import load_config, default_config_path
from avmonitor.audio_spectrum import AudioSpectrumAnalyzer, SpectrumFrame, list_output_devices
from avmonitor.audio_io import AudioIOMonitor, list_input_devices
from avmonitor.audio_recorder import AudioRecorder, resolve_recording_dir
from avmonitor.system_stats import SystemStatsMonitor
from avmonitor.network_monitor import NetworkMonitor
from avmonitor.process_watch import ProcessWatcher
from avmonitor.session_log import SessionLogger, resolve_log_dir, cleanup_old_reports
from avmonitor.ui.renderer import Renderer
from avmonitor.ui import theme
from avmonitor.util import drain_all
from avmonitor import win_native

# Fixed toolbar strip above the resizable content -- buttons live here so
# they never overlap the stats panels, whatever the panels' current height.
# Only shown in normal mode; compact mode has no toolbar at all.
HEADER_H = 44

# All positioned per-frame, right-to-left (see _layout_buttons in the draw loop).
TOPMOST_BUTTON_RECT = pygame.Rect(0, 0, 150, 32)
COMPACT_BUTTON_RECT = pygame.Rect(0, 0, 150, 32)
VISIBILITY_BUTTON_RECT = pygame.Rect(0, 0, 150, 32)
DEVICE_BUTTON_RECT = pygame.Rect(0, 0, 260, 32)
INPUT_DEVICE_BUTTON_RECT = pygame.Rect(0, 0, 260, 32)

DEVICE_ROW_H = 26
DEVICE_LIST_MAX_ROWS = 8
_CREATE_NO_WINDOW = 0x08000000

_CURSOR_FOR_KIND = {
    "top_h": pygame.SYSTEM_CURSOR_SIZENS,
    "col": pygame.SYSTEM_CURSOR_SIZEWE,
}


def _app_dir() -> Path:
    """Directory containing the actual .exe (or main.py from source) --
    NOT __file__'s parent when frozen, which resolves inside the onefile
    temp extraction dir. Elevated launches also sometimes inherit
    C:\\Windows\\System32 as CWD, so this is what config.json/resources are
    anchored to, never the process's ambient CWD."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _initial_window_size(cfg) -> "tuple[int, int]":
    """Sized proportional to the actual screen the app is launching on
    (78% width / 82% height, clamped to a sane minimum and never wider/
    taller than the screen itself) instead of always the same fixed
    default -- 1280x800 could be cramped on a small field-monitor laptop
    or tiny on a 4K rig. Falls back to cfg.window_width/height if the
    screen resolution can't be read for any reason.
    """
    screen_size = win_native.get_screen_size()
    if not screen_size:
        return cfg.window_width, cfg.window_height
    sw, sh = screen_size
    w = max(1000, min(int(sw * 0.78), sw - 80))
    h = max(650, min(int(sh * 0.82), sh - 80))
    return w, h


def _resource_path(name: str) -> Path:
    """Works both running from source and from the PyInstaller onefile exe
    (which unpacks bundled data files under sys._MEIPASS at runtime)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def _write_bootstrap_crash(exc) -> "Path | None":
    """Last-resort crash file for failures before/around SessionLogger."""
    try:
        log_dir = Path.home() / "Documents" / "AV_Monitor_Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"startup_crash_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path.write_text(f"{traceback.format_exc()}\n{exc}", encoding="utf-8")
        return path
    except Exception:
        return None


def _show_fatal_error(title: str, message: str):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _beep(severity: str):
    try:
        flag = winsound.MB_ICONHAND if severity == "crit" else winsound.MB_ICONEXCLAMATION
        winsound.MessageBeep(flag)
    except Exception:
        pass


def _launch_network_mapper(logger=None):
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--network-map"]
    else:
        args = [sys.executable, str(Path(__file__).resolve()), "--network-map"]
    try:
        subprocess.Popen(args, cwd=str(_app_dir()), creationflags=_CREATE_NO_WINDOW, close_fds=True)
        if logger:
            logger.add_event("Mapa de Rede iniciado", level="OK", source="NETWORK", event="NETWORK_MAP_START")
    except Exception as e:
        if logger:
            logger.add_event(f"Falha ao iniciar Mapa de Rede: {e}", level="ERROR", source="NETWORK", event="NETWORK_MAP_ERROR")


def _launch_streaming(logger=None):
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--streaming"]
    else:
        args = [sys.executable, str(Path(__file__).resolve()), "--streaming"]
    try:
        subprocess.Popen(args, cwd=str(_app_dir()), creationflags=_CREATE_NO_WINDOW, close_fds=True)
        if logger:
            logger.add_event("STREAMING iniciado", level="OK", source="AUDIO", event="STREAMING_START")
    except Exception as e:
        if logger:
            logger.add_event(f"Falha ao iniciar STREAMING: {e}", level="ERROR", source="AUDIO", event="STREAMING_ERROR")


def _pin_window_topmost(title_substring: str):
    """Generic version of what used to be Settings-only: some external
    window ("Configurações", "Gerenciador de Tarefas") opens as a normal
    (non-topmost) window -- if this app's own window (or STREAMING's) is
    pinned always-on-top, that window could never render above it no
    matter how much focus it gets (SetForegroundWindow doesn't let a
    normal window rise above a topmost one). Runs on its own daemon
    thread, one instance per external window being pinned.

    Pinning it once wasn't reliably enough -- this app's own periodic
    HWND_TOPMOST reassertion tries to defer to it once detected, but
    there's a real race window between that window opening and this
    thread finding+pinning it (up to ~5s of polling) during which the
    OTHER window's own reassert cycle can still win a claim. Both
    Settings and Task Manager get explicit "priority máxima, mesmo com
    o lock do programa ativado" per the user's request -- so this keeps
    re-claiming the front spot every 0.5s for as long as the window
    stays open, closing that race instead of just reducing its odds.
    Exits on its own once the window closes.
    """
    hwnd = None
    deadline = time.time() + 5.0
    while hwnd is None and time.time() < deadline:
        time.sleep(0.2)
        hwnd = win_native.find_window_containing(title_substring)
    if hwnd is None:
        return
    while True:
        win_native.set_always_on_top(hwnd, True)
        time.sleep(0.5)
        if win_native.find_window_containing(title_substring) is None:
            return


def _open_windows_mixer(logger=None):
    """The MIXER button used to open a whole custom window (its own
    per-app volume/mute faders) -- dropped in favor of just opening
    Windows' own per-app volume mixer directly (ms-settings:apps-volume,
    the same page "Config. de som do Windows" used to link to from
    inside that window). It already has everything the custom window
    offered, no reason to maintain a second, smaller version of it.
    """
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:apps-volume"], shell=False)
        threading.Thread(target=_pin_window_topmost, args=("Configurações",), daemon=True).start()
        if logger:
            logger.add_event("Config. de som do Windows aberta", level="OK", source="AUDIO", event="MIXER_START")
    except Exception as e:
        if logger:
            logger.add_event(f"Falha ao abrir config. de som: {e}", level="ERROR", source="AUDIO", event="MIXER_ERROR")


def _launch_task_manager(logger=None):
    ok, detail = win_native.launch_task_manager()
    if ok:
        # Same absolute-priority treatment as Windows' own Settings --
        # explicit request: Task Manager should render above everything,
        # even this app's own "sempre no topo" lock.
        threading.Thread(target=_pin_window_topmost, args=("Gerenciador de Tarefas",), daemon=True).start()
    if logger:
        logger.add_event(
            "Gerenciador de Tarefas iniciado" if ok else f"Falha ao iniciar Gerenciador de Tarefas: {detail}",
            level="OK" if ok else "ERROR", source="SYSTEM", event="TASK_MANAGER",
        )


def _flush_dns(logger=None, renderer=None):
    """`ipconfig /flushdns` -- the safe, fast "internet just broke, fix it
    now" action for the REDE panel's second button. Deliberately not a
    DHCP renew (`ipconfig /release && /renew`): that briefly drops the
    adapter's own IP before it gets a new one, which is a real risk of
    making a connection that's already having trouble worse for a few
    seconds -- exactly the wrong moment during a live event. A stale/
    corrupted DNS cache is a common enough cause of "sites won't load but
    the connection is up" that flushing it costs nothing and can only help.
    Runs synchronously (blocks the render loop briefly) -- fine here
    specifically because ipconfig /flushdns has a short, deterministic
    runtime and no user interaction to wait on. NOT the same tradeoff as
    the folder picker (see _start_folder_browse): a picker dialog waits on
    however long a human takes to navigate, which measured in the tens of
    seconds is long enough for Windows to flag the whole window as hung.
    """
    try:
        result = subprocess.run(
            ["ipconfig", "/flushdns"], capture_output=True, text=True, timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
        ok = result.returncode == 0
    except Exception as e:
        ok = False
        result = None
        detail_exc = str(e)
    if logger:
        if ok:
            logger.add_event("Cache DNS limpo (ipconfig /flushdns)", level="OK", source="NETWORK", event="DNS_FLUSH")
        else:
            detail = result.stderr.strip() if result and result.stderr else locals().get("detail_exc", "erro desconhecido")
            logger.add_event(f"Falha ao limpar cache DNS: {detail}", level="ERROR", source="NETWORK", event="DNS_FLUSH_ERROR")
    if renderer:
        renderer.push_event("Cache DNS limpo" if ok else "Falha ao limpar cache DNS", severity="warn" if ok else "crit")


def _browse_folder_worker(initial_dir: str, result_q: "queue.Queue[str | None]"):
    """Runs on its own thread -- see _start_folder_browse for why. Native
    folder picker via a PowerShell one-liner (System.Windows.Forms
    FolderBrowserDialog) instead of hand-rolling a picker in pygame.
    """
    # PowerShell single-quoted strings escape an embedded ' by doubling it
    # -- without this, a directory path that happens to contain a quote
    # would break the generated script instead of just picking oddly.
    safe_dir = initial_dir.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$f.SelectedPath = '{safe_dir}'; "
        "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=120, creationflags=_CREATE_NO_WINDOW,
        )
        path = result.stdout.strip()
        result_q.put(path or None)
    except Exception:
        result_q.put(None)


def _start_folder_browse(initial_dir: str) -> "queue.Queue[str | None]":
    """Launches the folder picker on a background thread and returns the
    queue its result lands on -- NEVER call the picker synchronously on
    the main thread.

    Confirmed the hard way: a user's session hit Windows' own "Application
    Hang" detector (Event Viewer: AppHangB1, Windows Error Reporting
    force-closed the process) while browsing for a folder -- the picker
    dialog can legitimately stay open for as long as someone takes to
    navigate, and a synchronous subprocess.run() for that whole time
    blocks pygame's message pump completely, which Windows reads as "this
    program stopped responding" and can kill outright. Same
    thread+queue+poll-once-per-frame shape as every other background I/O
    in this app -- the picker being manual/occasional never justified
    blocking the one thread that has to keep pumping window messages.
    """
    q: "queue.Queue[str | None]" = queue.Queue(maxsize=1)
    threading.Thread(target=_browse_folder_worker, args=(initial_dir, q), daemon=True).start()
    return q


_truncate_text_cache: "dict[tuple, str]" = {}


def _truncate_text(font, text, max_w):
    # Cached -- same reasoning as Renderer._truncate: most button labels
    # (device names, etc.) are identical from one frame to the next, so
    # re-measuring/re-shrinking them with font.size() every frame at 60fps
    # is pure waste. font.size() showed up as the single biggest chunk of
    # render time in a profiled run.
    key = (id(font), text, max_w)
    cached = _truncate_text_cache.get(key)
    if cached is not None:
        return cached
    if len(_truncate_text_cache) > 500:
        _truncate_text_cache.clear()
    if font.size(text)[0] <= max_w:
        result = text
    else:
        t = text
        while t and font.size(t + "…")[0] > max_w:
            t = t[:-1]
        result = f"{t}…" if t else "…"
    _truncate_text_cache[key] = result
    return result


def _draw_button(surface, font, rect, text, active=False, accent=theme.ACCENT):
    color = accent if active else theme.PANEL_BORDER
    pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=6)
    pygame.draw.rect(surface, color, rect, width=1 if not active else 2, border_radius=6)
    text = _truncate_text(font, text, rect.width - 16)
    label = font.render(text, True, theme.TEXT if not active else accent)
    surface.blit(label, label.get_rect(center=rect.center))


def _compute_live_warn_level(cfg, stats, network, processes, spectrum_available) -> bool:
    """Whether *right now* something looks off enough to be worth a
    glance -- the status dot's yellow baseline. Mirrors the same
    thresholds each panel already colors WARN with individually (CPU>70%,
    RAM>75%, GPU>70%, disk<50GB free, network loss, a watched process
    over its RAM alert, audio output down); this just asks "is any of
    that true right now" in one place instead of making the user scan
    four panels to notice. Crashes/hangs/errors are handled separately
    (see status_critical_active in main()) -- those latch red until
    acknowledged, this is purely live/reflective and clears itself the
    instant the underlying condition does.
    """
    if stats is not None:
        if stats.cpu_percent > 70 or stats.ram_percent > 75:
            return True
        if stats.gpu.available and stats.gpu.util_percent > 70:
            return True
        for d in stats.disks:
            if not d.error and d.free_gb < 50:
                return True
    if network is not None:
        for t in network.targets:
            if not t.alive or t.loss_percent > 0:
                return True
    if processes is not None:
        for p in processes.states:
            if p.error or (p.running and p.ram_mb > cfg.ram_alert_mb):
                return True
    if not spectrum_available:
        return True
    return False


def _draw_status_widget(surface, font_label, font_value, x, y, height, level, uptime_text, accent=theme.GOLD):
    """Status dot (green=ok/amber=warn/red=crit, click to acknowledge red)
    + "Tempo Online" uptime, filling the header gap between "Saída" and
    "Modo compacto". Returns (dot_rect, right_edge_x) -- dot_rect for click
    hit-testing, right_edge_x so a caller can place something right after
    the widget (the "C" EQ-theme button) without guessing its width.
    """
    # Fixed regardless of the active EQ theme -- green=ok/amber=warn/
    # red=critical is a universal status convention that has to read the
    # same no matter which decorative palette is picked (theme.OK gets
    # overridden to blue for the brndz theme's own text legibility, which
    # would otherwise leak into this dot and break the convention).
    color = {"crit": (235, 65, 55), "warn": (235, 160, 50)}.get(level, (80, 210, 120))
    dot_r = 7
    cy = y + height // 2
    dot_rect = pygame.Rect(x, cy - dot_r, dot_r * 2, dot_r * 2)
    pygame.draw.circle(surface, color, dot_rect.center, dot_r)
    if level == "crit":
        pygame.draw.circle(surface, theme.TEXT, dot_rect.center, dot_r, width=1)

    text_x = dot_rect.right + 10
    label_img = font_label.render("TEMPO ONLINE", True, theme.TEXT_LABEL)
    value_img = font_value.render(uptime_text, True, accent)
    surface.blit(label_img, (text_x, cy - label_img.get_height() - 1))
    surface.blit(value_img, (text_x, cy + 1))

    right_edge = text_x + max(label_img.get_width(), value_img.get_width())
    return dot_rect, right_edge


def _layout_buttons_right_to_left(window_width, y, *rects):
    x = window_width - 10
    for rect in rects:
        x -= rect.width
        rect.topleft = (x, y)
        x -= 10


def _draw_device_dropdown(surface, font, anchor_rect, options, selected_name, accent=theme.ACCENT):
    """options: list of (display, loopback_name). Returns the list of row
    rects in the same order as drawn, for hit-testing -- index 0 is always
    "Automático" (selected_name None), the rest mirror `options`.
    """
    rows = [("Automático (padrão do Windows)", None)] + options
    rows = rows[: DEVICE_LIST_MAX_ROWS + 1]

    panel = pygame.Rect(anchor_rect.x, anchor_rect.bottom + 4, anchor_rect.width, len(rows) * DEVICE_ROW_H + 8)
    pygame.draw.rect(surface, theme.PANEL_BG, panel, border_radius=6)
    pygame.draw.rect(surface, theme.PANEL_BORDER, panel, width=1, border_radius=6)

    mouse_pos = pygame.mouse.get_pos()
    row_rects = []
    y = panel.y + 4
    for display, name in rows:
        row = pygame.Rect(panel.x + 4, y, panel.width - 8, DEVICE_ROW_H)
        row_rects.append(row)
        hot = row.collidepoint(mouse_pos)
        current = name == selected_name
        if hot:
            pygame.draw.rect(surface, theme.PANEL_BORDER, row, border_radius=4)
        color = accent if current else theme.TEXT
        label = font.render(_truncate_text(font, display, row.width - 12), True, color)
        surface.blit(label, (row.x + 8, row.y + (row.height - label.get_height()) // 2))
        y += DEVICE_ROW_H

    return panel, row_rects


def _enter_compact_mode(cfg, always_on_top):
    """Switches to a small frameless colorkey-transparent window showing
    just the EQ, centered where the normal window was. Always topmost
    while active, regardless of the "sempre no topo" toggle -- explicit
    request: the compact EQ overlay's whole purpose is staying visible
    over other software (OBS/vMix/etc.), so it shouldn't need the toggle
    turned on separately to do that. The toggle still governs the full
    (non-compact) window on its own. The caption changes specifically so
    STREAMING/Mapa de Rede (which otherwise mirror this window's own
    always-on-top state and would then fight it for the front of the
    z-order band) can tell compact mode is active and yield outright
    instead -- see their own reassert-topmost blocks.
    """
    old_hwnd = _get_hwnd()
    center = None
    if old_hwnd:
        l, t, r, b = win_native.get_window_rect(old_hwnd)
        center = ((l + r) // 2, (t + b) // 2)

    screen = pygame.display.set_mode((cfg.compact_width, cfg.compact_height), pygame.NOFRAME)
    hwnd = _get_hwnd()
    if hwnd:
        win_native.enable_colorkey_transparency(hwnd, theme.COMPACT_COLORKEY)
        if center:
            win_native.move_window(hwnd, center[0] - cfg.compact_width // 2, center[1] - cfg.compact_height // 2)
        pygame.display.set_caption("brndz.wav Monitor — Modo Compacto")
        win_native.set_always_on_top(hwnd, True)
    return screen


def _exit_compact_mode(normal_size, always_on_top):
    screen = pygame.display.set_mode(normal_size, pygame.RESIZABLE)
    hwnd = _get_hwnd()
    if hwnd:
        win_native.disable_layered(hwnd)
        pygame.display.set_caption("brndz.wav Monitor")
        if always_on_top:
            win_native.set_always_on_top(hwnd, True)
    return screen


def _get_hwnd():
    try:
        return pygame.display.get_wm_info().get("window")
    except Exception:
        return None


def _clamp_onscreen_if_lost(hwnd):
    """Second layer of defense against the compact window ending up
    somewhere with no visible monitor under it (see the dragging_window
    safety net above for the main fix) -- if literally none of the
    window's rect overlaps the primary screen after a drag ends, recenter
    it there instead of leaving it stranded off-screen with no way back
    except restarting the app. A no-op in the overwhelmingly common case
    (window is somewhere reasonable)."""
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


def _toggle_recording(recorder, thread, logger, renderer, sample_rate, channels, label):
    """Shared by both independent recorders -- `recorder`/`thread` is
    either (output AudioRecorder, AudioSpectrumAnalyzer) or (mic
    AudioRecorder, AudioIOMonitor); both thread classes expose the same
    set_recording_sink(sink_or_None) shape. `label` ("OUT"/"IN") only
    disambiguates log/toast text -- the two recorders never touch each
    other's state, so they can run at the same time into two separate
    files without any risk of mixing.
    """
    if recorder.is_active():
        # stop() only flips the REC button off right away -- the file
        # isn't actually finished/closed until the recorder's own writer
        # thread drains whatever's still queued (see audio_recorder.py).
        # The generic per-frame pop_last_saved() poll below (same one that
        # already handles a mid-recording auto-stop) picks up the "saved"
        # log/toast whenever that lands, a frame or few later -- not
        # necessarily this one.
        recorder.stop()
        thread.set_recording_sink(None)
        return

    ok, detail = recorder.start(sample_rate, channels)
    if ok:
        directory, used_fallback = resolve_recording_dir(recorder.cfg)
        logger.add_event(f"Recording ({label}) started", level="ACTION", source="RECORDING", event="REC_START")
        logger.add_event(f"Recording ({label}) destination: {directory}", level="INFO", source="RECORDING", event="REC_DEST")
        logger.add_event(f"Recording ({label}) format: {recorder.state.format.upper()} {sample_rate}Hz {channels}ch", level="INFO", source="RECORDING", event="REC_FORMAT")
        thread.set_recording_sink(recorder.write)
        renderer.push_event(f"Gravação ({label}) iniciada", severity="warn")
    else:
        logger.add_event(f"Recording ({label}) failed to start: {detail}", level="ERROR", source="RECORDING", event="REC_START_ERROR")
        renderer.push_event(f"Gravação ({label}) falhou: {detail}", severity="crit")
        _beep("crit")


def main():
    app_dir = _app_dir()
    try:
        os.chdir(app_dir)
    except Exception:
        pass

    cfg = load_config()
    # always_on_top/compact_mode_active always start False in a fresh
    # session (see the `always_on_top = False` local below -- neither is
    # meant to persist across launches). But config.json itself can still
    # have a stale True left over from the previous run (especially an
    # unclean shutdown, which skips the exit-compact-mode write) -- and
    # STREAMING/Mapa de Rede read this file fresh, cross-process, so a
    # stale flag here would mislead them into thinking this window is
    # topmost/compact when it genuinely isn't yet. Reset both to match
    # reality before any derived window can read them.
    if cfg.always_on_top or cfg.compact_mode_active:
        cfg.always_on_top = False
        cfg.compact_mode_active = False
        cfg.save(default_config_path())
    logger = SessionLogger(cfg)
    logger.add_event("Startup iniciado", level="INFO", source="SYSTEM", event="STARTUP_BEGIN")
    logger.add_event(
        f"Modo: {'ADMIN' if win_native.is_admin() else 'USER'} | frozen={bool(getattr(sys, 'frozen', False))}",
        level="INFO", source="SYSTEM", event="STARTUP_ENV",
    )
    removed_reports = cleanup_old_reports(logger.log_dir, max_age_days=90)
    if removed_reports:
        logger.add_event(
            f"{removed_reports} relatório(s) de sessão com mais de 3 meses removido(s)",
            level="INFO", source="SYSTEM", event="OLD_REPORTS_CLEANED",
        )

    # pygame.init() initializes every subsystem, including pygame.mixer --
    # which opens a real WASAPI *render* (output) stream. This app never
    # plays a single pygame sound (alerts go through winsound.MessageBeep,
    # a separate WinAPI call), so that stream served no purpose except
    # registering this process as a live audio-rendering session in
    # Windows' own volume mixer -- confirmed directly (pygame.mixer.get_init()
    # returned a real (44100, -16, 2) stream after a bare pygame.init())
    # after the user noticed the app itself showing up as a channel to
    # control, which directly contradicts this project's own "it's a
    # monitor, it should never generate audio" rule. Only the 2 subsystems
    # actually used (display, font) are initialized instead of the
    # blanket pygame.init().
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_caption("brndz.wav Monitor")
    try:
        pygame.display.set_icon(pygame.image.load(_resource_path("brndz_icon_512.png")))
    except Exception:
        pass
    initial_window_size = _initial_window_size(cfg)
    screen = pygame.display.set_mode(initial_window_size, pygame.RESIZABLE)
    clock = pygame.time.Clock()

    audio_q: "queue.Queue[SpectrumFrame]" = queue.Queue(maxsize=1)
    stats_q = queue.Queue(maxsize=1)
    network_q = queue.Queue(maxsize=50)
    process_q = queue.Queue(maxsize=50)
    audio_io_q = queue.Queue(maxsize=1)

    audio_thread = AudioSpectrumAnalyzer(cfg, audio_q)
    audio_io_thread = AudioIOMonitor(cfg, audio_io_q)
    threads = [
        audio_thread,
        SystemStatsMonitor(cfg, stats_q),
        NetworkMonitor(cfg, network_q),
        ProcessWatcher(cfg, process_q),
        audio_io_thread,
    ]
    for t in threads:
        t.start()

    recorder = AudioRecorder(cfg)
    # Independent from `recorder` (output/EQ) -- own AudioRecorder instance,
    # own sink on AudioIOMonitor, own filename prefix so the two never
    # collide/overwrite each other on disk, and never mixed into one file.
    # Both share `cfg`, so directory/format settings (from the same CFG
    # popup) apply to both automatically.
    mic_recorder = AudioRecorder(cfg, filename_prefix="BRNDZ_MIC")

    # One-time enumeration for the device picker -- a brief (~100ms) blocking
    # call at startup is fine; re-enumerated on demand when the dropdown is
    # opened, in case something got plugged/unplugged mid-session.
    available_devices = list_output_devices()
    available_input_devices = list_input_devices()

    renderer = Renderer(cfg)

    last_spectrum = SpectrumFrame(available=False, error="iniciando captura de áudio...")
    latest_stats = None
    latest_network = None
    latest_process = None
    latest_audio_io = None
    last_logged_ts = None
    spectrum_fault_active = True
    last_spectrum_error = last_spectrum.error
    always_on_top = False
    reassert_topmost_at = 0.0

    session_start_time = time.time()
    # Latches red on the first ERROR/CRASH/HANG-level event and stays red
    # until the user clicks the dot -- unlike the live yellow baseline
    # (_compute_live_warn_level), a crash that already recovered on its
    # own would otherwise vanish from the dot before anyone saw it.
    status_critical_active = False
    status_events_seen = 0
    status_dot_rect = pygame.Rect(0, 0, 0, 0)
    theme_button_rect = pygame.Rect(0, 0, 0, 0)

    compact_mode = False
    normal_window_size = initial_window_size
    dragging_window = False
    drag_offset = (0, 0)
    compact_restore_rect = pygame.Rect(0, 0, 0, 0)
    compact_move_rect = pygame.Rect(0, 0, 0, 0)
    compact_theme_rect = pygame.Rect(0, 0, 0, 0)

    selected_device_name = None  # None = automatic (follow Windows default)
    device_dropdown_open = False
    dropdown_row_rects = []

    selected_input_device_name = None  # None = automatic (follow Windows default)
    input_device_dropdown_open = False
    input_dropdown_row_rects = []

    confirm_exit_open = False
    audio_settings_browse_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_close_rect = pygame.Rect(0, 0, 0, 0)
    folder_browse_queue = None  # non-None while the background picker thread is running
    audio_settings_wav_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_mp3_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_gain_minus_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_gain_plus_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_out_gain_minus_rect = pygame.Rect(0, 0, 0, 0)
    audio_settings_out_gain_plus_rect = pygame.Rect(0, 0, 0, 0)
    log_history_browse_rect = pygame.Rect(0, 0, 0, 0)
    log_folder_browse_queue = None

    running = True
    while running:
        dt = clock.tick(cfg.eq_fps) / 1000.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                if (recorder.is_active() or mic_recorder.is_active()) and not compact_mode:
                    confirm_exit_open = True
                else:
                    running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                if confirm_exit_open:
                    confirm_exit_open = False
                elif renderer.log_history_open:
                    renderer.log_history_open = False
                elif renderer.audio_settings_open:
                    renderer.audio_settings_open = False
                elif compact_mode:
                    compact_mode = False
                    screen = _exit_compact_mode(normal_window_size, always_on_top)
                    cfg.compact_mode_active = False
                    cfg.save(default_config_path())
                elif recorder.is_active() or mic_recorder.is_active():
                    confirm_exit_open = True
                else:
                    running = False
            elif event.type == pygame.VIDEORESIZE and not compact_mode:
                screen = pygame.display.set_mode((event.w, event.h), pygame.RESIZABLE)
                normal_window_size = (event.w, event.h)
                if always_on_top:
                    hwnd = _get_hwnd()
                    if hwnd:
                        win_native.set_always_on_top(hwnd, True)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if confirm_exit_open:
                    w, h = screen.get_size()
                    yes_rect, no_rect = renderer.draw_confirm_popup(
                        screen, "Gravação ativa", "Parar gravação e sair?", "Parar & Sair", "Cancelar",
                    )
                    if yes_rect.collidepoint(event.pos):
                        if recorder.is_active():
                            _toggle_recording(recorder, audio_thread, logger, renderer, 0, 0, "OUT")
                        if mic_recorder.is_active():
                            _toggle_recording(mic_recorder, audio_io_thread, logger, renderer, 0, 0, "IN")
                        confirm_exit_open = False
                        running = False
                    elif no_rect.collidepoint(event.pos):
                        confirm_exit_open = False
                elif renderer.log_history_open:
                    if log_history_browse_rect.collidepoint(event.pos):
                        if log_folder_browse_queue is None:
                            log_dir, _ = resolve_log_dir(cfg)
                            log_folder_browse_queue = _start_folder_browse(str(log_dir))
                    else:
                        # Any other click dismisses it (X or clicking
                        # outside), same catch-all-closes pattern as the
                        # settings popup.
                        renderer.log_history_open = False
                elif renderer.audio_settings_open:
                    if audio_settings_browse_rect.collidepoint(event.pos):
                        if folder_browse_queue is None:  # ignore a repeat click while one's already open
                            directory, _ = resolve_recording_dir(cfg)
                            folder_browse_queue = _start_folder_browse(str(directory))
                    elif audio_settings_wav_rect.collidepoint(event.pos):
                        if cfg.recording_format != "wav":
                            cfg.recording_format = "wav"
                            logger.add_event("Formato de gravação: WAV", level="INFO", source="RECORDING", event="REC_FORMAT_CHANGED")
                    elif audio_settings_mp3_rect.collidepoint(event.pos):
                        if cfg.recording_format != "mp3":
                            cfg.recording_format = "mp3"
                            logger.add_event("Formato de gravação: MP3", level="INFO", source="RECORDING", event="REC_FORMAT_CHANGED")
                    elif audio_settings_gain_minus_rect.collidepoint(event.pos):
                        cfg.mic_boost_db = max(0.0, cfg.mic_boost_db - 2.0)
                        audio_io_thread.set_gain_db(cfg.mic_boost_db)
                        logger.add_event(f"Ganho do mic: +{cfg.mic_boost_db:.0f}dB", level="INFO", source="RECORDING", event="MIC_GAIN_CHANGED")
                    elif audio_settings_gain_plus_rect.collidepoint(event.pos):
                        cfg.mic_boost_db = min(30.0, cfg.mic_boost_db + 2.0)
                        audio_io_thread.set_gain_db(cfg.mic_boost_db)
                        logger.add_event(f"Ganho do mic: +{cfg.mic_boost_db:.0f}dB", level="INFO", source="RECORDING", event="MIC_GAIN_CHANGED")
                    elif audio_settings_out_gain_minus_rect.collidepoint(event.pos):
                        cfg.out_boost_db = max(-12.0, cfg.out_boost_db - 2.0)
                        audio_thread.set_out_gain_db(cfg.out_boost_db)
                        logger.add_event(f"Ganho do OUT: {cfg.out_boost_db:+.0f}dB", level="INFO", source="RECORDING", event="OUT_GAIN_CHANGED")
                    elif audio_settings_out_gain_plus_rect.collidepoint(event.pos):
                        cfg.out_boost_db = min(20.0, cfg.out_boost_db + 2.0)
                        audio_thread.set_out_gain_db(cfg.out_boost_db)
                        logger.add_event(f"Ganho do OUT: {cfg.out_boost_db:+.0f}dB", level="INFO", source="RECORDING", event="OUT_GAIN_CHANGED")
                    elif audio_settings_close_rect.collidepoint(event.pos):
                        renderer.audio_settings_open = False
                    else:
                        renderer.audio_settings_open = False
                elif compact_mode:
                    if compact_restore_rect.collidepoint(event.pos):
                        compact_mode = False
                        screen = _exit_compact_mode(normal_window_size, always_on_top)
                        cfg.compact_mode_active = False
                        cfg.save(default_config_path())
                    elif compact_theme_rect.collidepoint(event.pos):
                        renderer.cycle_eq_color_theme(allow_rainbow=True)
                        cfg.eq_color_theme = renderer.eq_color_theme
                        cfg.save(default_config_path())
                    else:
                        # No title bar to drag by -- track the grab offset in
                        # screen coordinates and reposition the window under
                        # the cursor on every subsequent MOUSEMOTION.
                        dragging_window = True
                        hwnd = _get_hwnd()
                        if hwnd:
                            cx, cy = win_native.get_cursor_pos()
                            wx, wy, _, _ = win_native.get_window_rect(hwnd)
                            drag_offset = (cx - wx, cy - wy)
                elif device_dropdown_open:
                    # This click's only job is the dropdown: pick a row if
                    # one was hit, then close either way -- clicking
                    # elsewhere while it's open just dismisses it.
                    for i, row in enumerate(dropdown_row_rects):
                        if row.collidepoint(event.pos):
                            if i == 0:
                                selected_device_name = None
                            else:
                                selected_device_name = available_devices[i - 1][1]
                            audio_thread.set_device_override(selected_device_name)
                            break
                    device_dropdown_open = False
                elif input_device_dropdown_open:
                    for i, row in enumerate(input_dropdown_row_rects):
                        if row.collidepoint(event.pos):
                            if i == 0:
                                selected_input_device_name = None
                            else:
                                selected_input_device_name = available_input_devices[i - 1][1]
                            audio_io_thread.set_device_override(selected_input_device_name)
                            break
                    input_device_dropdown_open = False
                elif status_dot_rect.collidepoint(event.pos):
                    if status_critical_active:
                        status_critical_active = False
                        logger.add_event("Status crítico reconhecido pelo usuário", level="ACTION", source="SYSTEM", event="STATUS_ACK")
                elif theme_button_rect.collidepoint(event.pos):
                    renderer.cycle_eq_color_theme()
                    cfg.eq_color_theme = renderer.eq_color_theme
                    cfg.save(default_config_path())
                elif VISIBILITY_BUTTON_RECT.collidepoint(event.pos):
                    renderer.toggle_top_collapsed()
                elif TOPMOST_BUTTON_RECT.collidepoint(event.pos):
                    always_on_top = not always_on_top
                    hwnd = _get_hwnd()
                    if hwnd:
                        win_native.set_always_on_top(hwnd, always_on_top)
                    # Persisted so STREAMING/Mapa de Rede can read the
                    # toggle's real state via config.json instead of
                    # querying this window's live WS_EX_TOPMOST bit
                    # cross-process -- see Config.always_on_top's docstring.
                    cfg.always_on_top = always_on_top
                    cfg.save(default_config_path())
                elif COMPACT_BUTTON_RECT.collidepoint(event.pos):
                    compact_mode = True
                    device_dropdown_open = False
                    input_device_dropdown_open = False
                    screen = _enter_compact_mode(cfg, always_on_top)
                    cfg.compact_mode_active = True
                    cfg.save(default_config_path())
                elif DEVICE_BUTTON_RECT.collidepoint(event.pos):
                    device_dropdown_open = True
                    available_devices = list_output_devices()  # cheap-ish, catches plug/unplug
                elif INPUT_DEVICE_BUTTON_RECT.collidepoint(event.pos):
                    input_device_dropdown_open = True
                    available_input_devices = list_input_devices()  # cheap-ish, catches plug/unplug
                elif event.pos[1] >= HEADER_H and renderer.map_network_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _launch_network_mapper(logger)
                elif event.pos[1] >= HEADER_H and renderer.mixer_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _open_windows_mixer(logger)
                elif event.pos[1] >= HEADER_H and renderer.streaming_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _launch_streaming(logger)
                elif event.pos[1] >= HEADER_H and renderer.flush_dns_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _flush_dns(logger, renderer)
                elif event.pos[1] >= HEADER_H and renderer.open_taskmgr_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _launch_task_manager(logger)
                elif event.pos[1] >= HEADER_H and renderer.rec_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    _toggle_recording(
                        recorder, audio_thread, logger, renderer,
                        audio_thread.current_sample_rate, audio_thread.current_channels, "OUT",
                    )
                elif event.pos[1] >= HEADER_H and renderer.mic_rec_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    mic_rate = latest_audio_io.input.sample_rate if latest_audio_io else 0
                    mic_channels = latest_audio_io.input.channels if latest_audio_io else 0
                    _toggle_recording(mic_recorder, audio_io_thread, logger, renderer, mic_rate, mic_channels, "IN")
                elif event.pos[1] >= HEADER_H and renderer.lufs_threshold_minus_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    cfg.lufs_alert_threshold = max(-40.0, cfg.lufs_alert_threshold - 1.0)
                    renderer.lufs_alert_threshold = cfg.lufs_alert_threshold
                elif event.pos[1] >= HEADER_H and renderer.lufs_threshold_plus_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    # Capped just under the meter's fixed -9 LUFS full-red
                    # ceiling -- pushed any higher and the target zone
                    # between the two would have zero height, hiding the
                    # marker's whole purpose.
                    cfg.lufs_alert_threshold = min(-10.0, cfg.lufs_alert_threshold + 1.0)
                    renderer.lufs_alert_threshold = cfg.lufs_alert_threshold
                elif event.pos[1] >= HEADER_H and renderer.audio_settings_button_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    renderer.audio_settings_open = True
                elif event.pos[1] >= HEADER_H and renderer.log_box_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    renderer.log_history_open = True
                elif event.pos[1] >= HEADER_H and renderer.vu_in_toggle_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    renderer.vu_in_enabled = not renderer.vu_in_enabled
                elif event.pos[1] >= HEADER_H and renderer.vu_out_toggle_rect.collidepoint(
                    (event.pos[0], event.pos[1] - HEADER_H)
                ):
                    renderer.vu_out_enabled = not renderer.vu_out_enabled
                elif event.pos[1] >= HEADER_H:
                    cw, ch = screen.get_size()
                    renderer.handle_mouse_down((event.pos[0], event.pos[1] - HEADER_H), cw, ch - HEADER_H)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                if compact_mode:
                    dragging_window = False
                    _clamp_onscreen_if_lost(_get_hwnd())
                else:
                    renderer.handle_mouse_up()
            elif event.type == pygame.MOUSEMOTION:
                if compact_mode:
                    if dragging_window:
                        hwnd = _get_hwnd()
                        if hwnd:
                            cx, cy = win_native.get_cursor_pos()
                            win_native.move_window(hwnd, cx - drag_offset[0], cy - drag_offset[1])
                else:
                    cw, ch = screen.get_size()
                    renderer.handle_mouse_motion((event.pos[0], event.pos[1] - HEADER_H), cw, ch - HEADER_H)

        if dragging_window and not win_native.is_left_button_down():
            # Safety net for a real bug reported in the field: a
            # MOUSEBUTTONUP that happens while the cursor is outside this
            # frameless window's small bounds isn't reliably delivered to
            # the app, so the flag set on MOUSEBUTTONDOWN could never
            # clear on its own -- every later MOUSEMOTION anywhere then
            # kept dragging the window, which could send it off-screen
            # with no way back short of restarting. Polling the real OS
            # button state once a frame self-corrects regardless of
            # whether the matching event ever arrives.
            dragging_window = False
            _clamp_onscreen_if_lost(_get_hwnd())

        if (always_on_top or compact_mode) and time.time() >= reassert_topmost_at:
            # A one-time SetWindowPos can get buried under some other window
            # that *also* asks for topmost afterwards (z-order among topmost
            # windows still follows recency) -- reasserting periodically
            # wins that back instead of silently losing the "always on top"
            # promise. Compact mode reasserts unconditionally (regardless
            # of the "sempre no topo" toggle) -- explicit request: the
            # compact EQ overlay should always stay the top layer while
            # active, full stop.
            #
            # Exception: Windows' own Settings window (opened by the
            # "Config. Avançadas" button) always gets absolute priority,
            # no matter what -- same now for Task Manager ("Gerenciador
            # de Tarefas"), explicit request: both should render above
            # everything, even this window's own compact-mode lock.
            # STREAMING and Mapa de Rede also mirror/claim topmost on
            # their own periodic cadence (tracking this window's own
            # toggle live -- see win_native.is_window_topmost()), so in
            # normal (non-compact) mode with the toggle on, this window
            # defers to them too ("most recently opened wins", same
            # principle STREAMING applies to its own prompt dialogs)
            # instead of fighting for the front spot every ~1.5s. Compact
            # mode does NOT defer to STREAMING/Mapa de Rede -- it's meant
            # to stay in evidence over the app's own other windows too,
            # not just third-party ones -- but it DOES still defer to
            # Settings/Task Manager, which outrank even compact mode.
            priority_window_open = (
                win_native.find_window_containing("Configurações") is not None
                or win_native.find_window_containing("Gerenciador de Tarefas") is not None
            )
            own_window_open = (
                win_native.find_window_containing("STREAMING") is not None
                or win_native.find_window_containing("Mapa de Rede") is not None
            )
            if not priority_window_open and (compact_mode or not own_window_open):
                hwnd = _get_hwnd()
                if hwnd:
                    win_native.set_always_on_top(hwnd, True)
            reassert_topmost_at = time.time() + 1.5

        if not compact_mode and not device_dropdown_open and not input_device_dropdown_open and not renderer.audio_settings_open and not confirm_exit_open and not renderer.log_history_open:
            mx, my = pygame.mouse.get_pos()
            cw, ch = screen.get_size()
            hover_kind = renderer.splitter_at((mx, my - HEADER_H), cw, ch - HEADER_H) if my >= HEADER_H else None
            try:
                pygame.mouse.set_cursor(_CURSOR_FOR_KIND.get(hover_kind, pygame.SYSTEM_CURSOR_ARROW))
            except pygame.error:
                pass  # some environments (e.g. no real display driver) can't set system cursors

        try:
            last_spectrum = audio_q.get_nowait()
        except queue.Empty:
            pass
        renderer.update_spectrum(last_spectrum, dt)
        if not last_spectrum.available:
            if not spectrum_fault_active or last_spectrum.error != last_spectrum_error:
                msg = last_spectrum.error or "Áudio de saída indisponível"
                logger.add_event(msg, level="ERROR", source="AUDIO", event="AUDIO_OUTPUT_ERROR")
                renderer.push_event(msg, severity="crit")
            spectrum_fault_active = True
            last_spectrum_error = last_spectrum.error
        elif spectrum_fault_active:
            logger.add_event("Áudio de saída restaurado", level="RECOVERY", source="AUDIO", event="AUDIO_OUTPUT_RECOVERY")
            renderer.push_event("Áudio de saída restaurado", severity="warn")
            spectrum_fault_active = False
            last_spectrum_error = None

        try:
            latest_stats = stats_q.get_nowait()
        except queue.Empty:
            pass

        new_audio_io = None
        try:
            new_audio_io = audio_io_q.get_nowait()
        except queue.Empty:
            pass
        if new_audio_io is not None:
            latest_audio_io = new_audio_io
        # Advances the smoothed IN/OUT meter values every frame regardless
        # of whether new audio data arrived this frame -- must run after
        # update_spectrum() above (reads renderer.output_level_db, set
        # there). This is what keeps the VU bars moving fluidly at the
        # render loop's frame rate instead of holding each raw ~12-23Hz
        # reading static until the next one arrives.
        renderer.update_audio_io(latest_audio_io, dt)
        # Events are only processed off a snapshot that's *new this frame* --
        # audio_io_q is a push_latest (maxsize=1) queue, so on any frame
        # where the audio thread hasn't published since we last checked,
        # get_nowait() raises queue.Empty and latest_audio_io still holds
        # the previous snapshot (deliberately, so the panel keeps rendering
        # its last known level/peak instead of blanking between updates).
        # Looping over `latest_audio_io.events` unconditionally would
        # re-log/re-beep the same discrete event (a device just detected, a
        # clip) on every subsequent frame until a fresh snapshot arrives --
        # reproduced for real: 2-4x duplicate "Áudio IN detectado" events
        # logged within the same second at startup, whenever the render
        # loop ran a few frames before the next audio_io publish landed.
        if new_audio_io is not None:
            for msg in new_audio_io.events:
                low = msg.lower()
                if "clip" in low:
                    level, severity = "WARNING", "warn"
                elif "desconectado" in low or "sem dados" in low:
                    level, severity = "ERROR", "crit"
                elif "voltou" in low:
                    level, severity = "RECOVERY", "warn"
                elif "mudou" in low or "detectado" in low:
                    level, severity = "INFO", "warn"
                else:
                    level, severity = "INFO", "warn"
                logger.add_event(msg, level=level, source="AUDIO", event="AUDIO_IO")
                renderer.push_event(msg, severity=severity)
                if severity == "crit":
                    _beep("crit")

        # Recorder errors/completions raised from the audio thread are
        # polled here (main thread) rather than logged directly from
        # AudioRecorder.write() -- see audio_recorder.py's module docstring
        # for why that split matters.
        for rec, rec_label in ((recorder, "OUT"), (mic_recorder, "IN")):
            rec_error = rec.pop_pending_error()
            if rec_error:
                logger.add_event(f"Recording ({rec_label}) storage unavailable: {rec_error}", level="ERROR", source="RECORDING", event="REC_ERROR")
                renderer.push_event(f"Gravação ({rec_label}) interrompida: {rec_error}", severity="crit")
                _beep("crit")
            rec_saved = rec.pop_last_saved()
            if rec_saved:
                dropped = rec_saved.get("dropped_chunks")
                if dropped:
                    # Real but rare: the write-behind buffer (~25s) filled
                    # up because the disk was stuck that whole time, not
                    # just momentarily slow -- the take saved fine overall
                    # but has a gap in it.
                    logger.add_event(
                        f"Recording ({rec_label}) saved with {dropped} dropped chunk(s) (disco lento/travado durante a gravação): {rec_saved['filename']}",
                        level="WARNING", source="RECORDING", event="REC_SAVED_GAPS",
                    )
                else:
                    logger.add_event(f"Recording ({rec_label}) saved: {rec_saved['filename']}", level="OK", source="RECORDING", event="REC_SAVED")
                logger.add_recording(
                    rec_saved["filename"], rec_saved["started_at"], rec_saved["ended_at"],
                    rec_saved["format"], rec_saved["sample_rate"], rec_saved["channels"], rec_saved["size_bytes"],
                )
                renderer.push_event(f"Gravação ({rec_label}) salva: {rec_saved['filename']}", severity="warn")

        for snap in drain_all(network_q):
            latest_network = snap
            for msg in snap.events:
                level = "RECOVERY" if "voltou" in msg.lower() else "WARNING"
                logger.add_event(msg, level=level, source="NETWORK", event="NETWORK_EVENT")
                renderer.push_event(msg, severity="warn")
                _beep("warn")

        for snap in drain_all(process_q):
            latest_process = snap
            for msg in snap.events:
                low = msg.lower()
                if "crash" in low or "sumiu" in low:
                    level, severity = "CRASH", "crit"
                elif "travado" in low:
                    level, severity = "HANG", "crit"
                elif "voltou" in low:
                    level, severity = "RECOVERY", "warn"
                else:
                    level, severity = "WARNING", "warn"
                logger.add_event(msg, level=level, source="PROCESS", event="PROCESS_EVENT")
                renderer.push_event(msg, severity=severity)
                _beep(severity)

        if latest_stats is not None and latest_stats.timestamp != last_logged_ts:
            logger.log_row(latest_stats, latest_network)
            last_logged_ts = latest_stats.timestamp

        if folder_browse_queue is not None:
            try:
                picked = folder_browse_queue.get_nowait()
                folder_browse_queue = None
                if picked:
                    cfg.recording_directory = picked
                    logger.add_event(f"Pasta de gravação alterada: {picked}", level="INFO", source="RECORDING", event="REC_DIR_CHANGED")
            except queue.Empty:
                pass  # picker dialog still open -- keep rendering normally, check again next frame

        if log_folder_browse_queue is not None:
            try:
                picked = log_folder_browse_queue.get_nowait()
                log_folder_browse_queue = None
                if picked:
                    cfg.log_directory_override = picked
                    cfg.save(default_config_path())
                    logger.add_event(f"Pasta de logs alterada (aplica no próximo início): {picked}", level="INFO", source="SYSTEM", event="LOG_DIR_CHANGED")
                    renderer.push_event(f"Pasta de logs salva -- aplica no próximo início: {picked}", severity="warn")
            except queue.Empty:
                pass

        # Status dot: latch red the first time any ERROR/CRASH/HANG-level
        # event lands, from whatever source (audio/network/process/
        # recording) -- scanning the tail of the already-append-only
        # event log instead of hooking every individual add_event() call
        # site. O(new events since last frame), not the whole history.
        if len(logger.event_records) > status_events_seen:
            for e in logger.event_records[status_events_seen:]:
                if e["level"] in ("ERROR", "CRASH", "HANG"):
                    status_critical_active = True
            status_events_seen = len(logger.event_records)

        if compact_mode:
            renderer.draw_compact(screen, theme.COMPACT_COLORKEY)
            # Geometry is fixed/deterministic (doesn't depend on hover),
            # so it's computed up front to check the mouse against the
            # *whole 3-button cluster* -- hovering any one of the 3
            # reveals all 3, not just the one directly under the cursor.
            btn_size = 20
            restore_rect = pygame.Rect(1, screen.get_height() - btn_size - 1, btn_size, btn_size)
            move_rect = pygame.Rect(restore_rect.right + 2, restore_rect.y, btn_size, btn_size)
            theme_rect = pygame.Rect(move_rect.right + 2, restore_rect.y, btn_size, btn_size)
            cluster_rect = restore_rect.unionall([move_rect, theme_rect])
            buttons_visible = cluster_rect.collidepoint(pygame.mouse.get_pos())
            compact_restore_rect = renderer.draw_compact_restore_button(screen, buttons_visible)
            compact_move_rect = renderer.draw_move_icon_button(screen, move_rect, buttons_visible)
            compact_theme_rect = renderer.draw_theme_button(screen, theme_rect, visible=buttons_visible)
        else:
            w, h = screen.get_size()
            content = screen.subsurface((0, HEADER_H, w, max(0, h - HEADER_H)))
            renderer.draw(content, latest_stats, latest_network, latest_process,
                           latest_audio_io, logger.recent_events, recorder.state, mic_recorder.state)

            screen.fill(theme.BG, (0, 0, w, HEADER_H))
            pygame.draw.line(screen, theme.PANEL_BORDER, (0, HEADER_H - 1), (w, HEADER_H - 1))

            # All action buttons grouped on the right, OUTPUTS/INPUTS (device
            # pickers) rightmost -- leaves the whole left side free for the
            # status widget below. "Sempre no topo"/"Modo compacto"/
            # "Ocultar painel" are all small icon buttons next to the "C"
            # theme button instead (see below), not in this row. No
            # explicit "Parar & Salvar" button either -- closing the window
            # (native X, or ESC) already runs the exact same stop-
            # recording-if-active-then-save-and-exit path (see the QUIT/
            # ESCAPE handling above), so a second button doing the same
            # thing was pure redundancy.
            _layout_buttons_right_to_left(
                w, 6, DEVICE_BUTTON_RECT, INPUT_DEVICE_BUTTON_RECT,
            )

            device_display = "Automático (padrão do Windows)"
            if selected_device_name is not None:
                for disp, name in available_devices:
                    if name == selected_device_name:
                        device_display = disp
                        break

            input_device_display = "Automático (padrão do Windows)"
            if selected_input_device_name is not None:
                for disp, name in available_input_devices:
                    if name == selected_input_device_name:
                        input_device_display = disp
                        break

            _draw_button(screen, renderer.font_md, DEVICE_BUTTON_RECT, f"OUTPUT: {device_display}",
                         active=device_dropdown_open, accent=renderer.chrome_accent())
            _draw_button(screen, renderer.font_md, INPUT_DEVICE_BUTTON_RECT, f"INPUT: {input_device_display}",
                         active=input_device_dropdown_open, accent=renderer.chrome_accent())

            live_warn = _compute_live_warn_level(cfg, latest_stats, latest_network, latest_process, renderer.spectrum_available)
            status_level = "crit" if status_critical_active else ("warn" if live_warn else "ok")
            elapsed = max(0.0, time.time() - session_start_time)
            eh, erem = divmod(int(elapsed), 3600)
            emin, esec = divmod(erem, 60)
            uptime_text = f"{eh:02d}:{emin:02d}:{esec:02d}"
            status_x = 10
            if status_x + 265 < INPUT_DEVICE_BUTTON_RECT.x:
                status_dot_rect, status_right_edge = _draw_status_widget(
                    screen, renderer.font_label, renderer.font_value, status_x, 6, 32, status_level, uptime_text,
                    accent=renderer.chrome_accent(),
                )
                theme_button_rect = renderer.draw_theme_button(screen, pygame.Rect(status_right_edge + 14, 6, 22, 22))
                COMPACT_BUTTON_RECT.update(theme_button_rect.right + 6, 6, 22, 22)
                TOPMOST_BUTTON_RECT.update(COMPACT_BUTTON_RECT.right + 6, 6, 22, 22)
                VISIBILITY_BUTTON_RECT.update(TOPMOST_BUTTON_RECT.right + 6, 6, 22, 22)
                renderer.draw_compact_toggle_icon_button(screen, COMPACT_BUTTON_RECT)
                renderer.draw_topmost_toggle_icon_button(screen, TOPMOST_BUTTON_RECT, always_on_top)
                renderer.draw_visibility_toggle_icon_button(screen, VISIBILITY_BUTTON_RECT, renderer.top_collapsed)
            else:
                status_dot_rect = pygame.Rect(0, 0, 0, 0)  # window too narrow -- no room, no stale hit target
                theme_button_rect = pygame.Rect(0, 0, 0, 0)
                COMPACT_BUTTON_RECT.update(0, 0, 0, 0)
                TOPMOST_BUTTON_RECT.update(0, 0, 0, 0)
                VISIBILITY_BUTTON_RECT.update(0, 0, 0, 0)

            if device_dropdown_open:
                _, dropdown_row_rects = _draw_device_dropdown(
                    screen, renderer.font_row, DEVICE_BUTTON_RECT, available_devices, selected_device_name,
                    accent=renderer.chrome_accent(),
                )

            if input_device_dropdown_open:
                _, input_dropdown_row_rects = _draw_device_dropdown(
                    screen, renderer.font_row, INPUT_DEVICE_BUTTON_RECT, available_input_devices, selected_input_device_name,
                    accent=renderer.chrome_accent(),
                )

            if renderer.audio_settings_open:
                directory, _ = resolve_recording_dir(cfg)
                if cfg.recording_format == "mp3":
                    detail = f"{cfg.recording_sample_rate / 1000:.1f}kHz · {cfg.recording_channels}ch · 192kbps"
                else:
                    detail = f"{cfg.recording_sample_rate / 1000:.1f}kHz · {cfg.recording_channels}ch · {cfg.recording_bit_depth}-bit"
                (audio_settings_browse_rect, audio_settings_close_rect,
                 audio_settings_wav_rect, audio_settings_mp3_rect,
                 audio_settings_gain_minus_rect, audio_settings_gain_plus_rect,
                 audio_settings_out_gain_minus_rect, audio_settings_out_gain_plus_rect) = renderer.draw_audio_settings_popup(
                    screen, str(directory), cfg.recording_format, detail, cfg.mic_boost_db, cfg.out_boost_db,
                )

            if renderer.log_history_open:
                log_dir, _ = resolve_log_dir(cfg)
                _, log_history_browse_rect = renderer.draw_log_history_popup(screen, logger.event_records, str(log_dir))

            if confirm_exit_open:
                renderer.draw_confirm_popup(
                    screen, "Gravação ativa", "Parar gravação e sair?", "Parar & Sair", "Cancelar",
                )

        pygame.display.flip()

    # Should be unreachable (the confirm dialog gates this), but never let
    # a recording in progress get silently dropped either way.
    if recorder.is_active():
        _toggle_recording(recorder, audio_thread, logger, renderer, 0, 0, "OUT")
    if mic_recorder.is_active():
        _toggle_recording(mic_recorder, audio_io_thread, logger, renderer, 0, 0, "IN")

    for t in threads:
        t.stop()

    logger.add_event("Shutdown iniciado", level="INFO", source="SYSTEM", event="SHUTDOWN")
    summary_path = logger.close()
    print(f"Sessão encerrada. Resumo salvo em: {summary_path}")

    for t in threads:
        t.join(timeout=2.0)

    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    if "--network-map" in sys.argv:
        # Separate window, dispatched by the "Mapear Rede" button (see
        # _launch_network_mapper) rather than main() itself, since pygame
        # is single-window-per-process -- a second OS process is the
        # simplest way to get a second window, and works identically from
        # source and from the frozen exe (sys.executable IS the exe there).
        import network_mapper
        network_mapper.main()
    elif "--streaming" in sys.argv:
        # Same separate-process pattern as --network-map, dispatched by
        # the STREAMING button in the ÁUDIO I/O panel (see _launch_streaming).
        import streaming_window
        streaming_window.main()
    else:
        try:
            main()
        except SystemExit:
            raise
        except Exception as exc:
            crash_path = _write_bootstrap_crash(exc)
            detail = f"brndz.wav Monitor encontrou um erro inesperado.\n\n{type(exc).__name__}: {exc}"
            if crash_path:
                detail += f"\n\nLog de emergência: {crash_path}"
            _show_fatal_error("brndz.wav Monitor — erro", detail)
            raise
