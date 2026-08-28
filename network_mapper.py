"""Network mapper: standalone pygame window that ping-sweeps the local LAN
and lists what answered (IP/hostname/MAC/likely device type), highlighting
probable cameras/PTZ/AV-over-IP gear. Launched as a separate process by the
main monitor's "Mapear Rede" button (see main.py's --network-map dispatch)
so it gets its own window without touching pygame's single-window model.
"""
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pygame

from avmonitor.config import load_config
from avmonitor.network_scan import local_network_prefix, save_network_map, scan_network
from avmonitor.ui import theme
from avmonitor.ui.renderer import apply_theme_to_window
from avmonitor import win_native

ROW_H = 24
HEADER_H = 60
FOOTER_H = 50


def _resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def _get_hwnd():
    try:
        return pygame.display.get_wm_info().get("window")
    except Exception:
        return None


def _draw_button(surface, font, rect, text, enabled=True, active=False):
    border = theme.ACCENT if active else theme.PANEL_BORDER
    text_color = theme.TEXT if enabled else theme.TEXT_LABEL
    pygame.draw.rect(surface, theme.PANEL_BG, rect, border_radius=6)
    pygame.draw.rect(surface, border, rect, width=1 if not active else 2, border_radius=6)
    label = font.render(text, True, text_color)
    surface.blit(label, label.get_rect(center=rect.center))


class ScanWorker(threading.Thread):
    def __init__(self, prefix: str, out_queue: "queue.Queue"):
        super().__init__(daemon=True)
        self.prefix = prefix
        self.out_queue = out_queue
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        def progress(done, total):
            self.out_queue.put(("progress", done, total))

        try:
            hosts = scan_network(
                self.prefix,
                progress_cb=progress,
                stop_check=self._stop_event.is_set,
            )
            if not self._stop_event.is_set():
                self.out_queue.put(("done", hosts))
        except Exception as e:
            self.out_queue.put(("error", str(e)))


def main():
    # Empty argv on purpose: this process was launched with `--network-map`
    # (see main.py's dispatch), which isn't a flag load_config()/argparse
    # knows about -- this window doesn't take CLI args of its own, it just
    # wants the same config.json defaults (drive_label, fallback_log_dir).
    cfg = load_config([])
    # Opens already matching whichever EQ color palette was last
    # selected in the main app (persisted to config.json on every theme
    # change there) -- a one-shot read, not a live sync; see
    # renderer.apply_theme_to_window()'s docstring.
    apply_theme_to_window(cfg.eq_color_theme)

    # See main.py's own comment on this same substitution -- pygame.init()
    # opens a real audio-output stream via pygame.mixer that this app
    # never uses, registering a phantom "audio session" for this process
    # in Windows' volume mixer for no reason.
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_caption("brndz.wav — Mapa de Rede")
    try:
        pygame.display.set_icon(pygame.image.load(_resource_path("brndz_icon_512.png")))
    except Exception:
        pass
    screen = pygame.display.set_mode((900, 600), pygame.RESIZABLE)
    clock = pygame.time.Clock()

    font_title = pygame.font.SysFont("segoe ui", 18, bold=True)
    font_row = pygame.font.SysFont(theme.FONT_NAME, 14)
    font_sm = pygame.font.SysFont(theme.FONT_NAME, 12)
    font_btn = pygame.font.SysFont("segoe ui", 15, bold=True)

    prefix = local_network_prefix() or "192.168.1"
    result_q: "queue.Queue" = queue.Queue()
    worker = None
    scanning = False
    progress = (0, 0)
    hosts = []
    status_msg = ""
    scroll_y = 0

    rescan_rect = pygame.Rect(0, 0, 150, 34)
    save_rect = pygame.Rect(0, 0, 170, 34)
    copy_rect = pygame.Rect(0, 0, 150, 34)
    saved_path = None
    saved_flash_until = 0.0

    selected_ip = None
    last_click_time = 0.0
    last_click_row = None
    copy_flash_until = 0.0

    def copy_to_clipboard(text: str):
        try:
            subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            return False

    def start_scan():
        nonlocal worker, scanning, progress, hosts, status_msg
        if worker and worker.is_alive():
            return
        hosts = []
        progress = (0, 254)
        scanning = True
        status_msg = ""
        worker = ScanWorker(prefix, result_q)
        worker.start()

    start_scan()

    # Always-on-top mirrors the main app's own "sempre no topo" toggle
    # live -- this window has no toggle of its own, explicit request:
    # topmost ONLY while the main app's is active, tracking it in real
    # time (not a one-shot read at launch) by asking the OS directly
    # whether the main window currently carries WS_EX_TOPMOST. No IPC or
    # config polling needed for that -- see win_native.is_window_topmost().
    hwnd = _get_hwnd()
    is_topmost = False
    reassert_topmost_at = 0.0

    running = True
    while running:
        clock.tick(30)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False
            elif event.type == pygame.MOUSEWHEEL:
                scroll_y = max(0, scroll_y - event.y * ROW_H * 2)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if rescan_rect.collidepoint(event.pos) and not scanning:
                    start_scan()
                elif save_rect.collidepoint(event.pos) and hosts and not scanning:
                    try:
                        saved_path = save_network_map(hosts, prefix, cfg)
                        status_msg = f"Salvo em: {saved_path}"
                    except Exception as e:
                        status_msg = f"Erro ao salvar: {e}"
                    saved_flash_until = time.time() + 4.0
                elif copy_rect.collidepoint(event.pos) and selected_ip:
                    if copy_to_clipboard(selected_ip):
                        copy_flash_until = time.time() + 1.5
                else:
                    # Row click: single click selects (enables "Copiar IP"),
                    # a second click on the *same* row within 400ms opens
                    # its web UI -- most cameras/PTZ/encoders/switches have
                    # one, and typing the IP by hand every time is the
                    # actual friction this is fixing.
                    content_top_hit = HEADER_H + 30
                    row_idx = (event.pos[1] - content_top_hit + scroll_y) // ROW_H
                    if event.pos[1] >= content_top_hit and 0 <= row_idx < len(hosts):
                        host = hosts[row_idx]
                        now = time.time()
                        if last_click_row == row_idx and now - last_click_time < 0.4:
                            webbrowser.open(f"http://{host.ip}")
                            last_click_row = None
                        else:
                            selected_ip = host.ip
                            last_click_row = row_idx
                            last_click_time = now

        while True:
            try:
                msg = result_q.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "progress":
                progress = (msg[1], msg[2])
            elif msg[0] == "done":
                hosts = msg[1]
                scanning = False
                status_msg = f"{len(hosts)} dispositivo(s) encontrado(s) em {prefix}.0/24"
            elif msg[0] == "error":
                scanning = False
                status_msg = f"Erro no scan: {msg[1]}"

        if hwnd and time.time() >= reassert_topmost_at:
            reassert_topmost_at = time.time() + 1.5
            # Reads main.py's own always_on_top/compact_mode_active state
            # from config.json rather than querying its live window style
            # cross-process -- that approach was tried first and confirmed
            # unreliable in the field (this window losing topmost after
            # certain OS-level window-manager disruptions, e.g. switching
            # to another app's fullscreen video, and not recovering). See
            # Config.always_on_top's docstring / streaming_window.py's own
            # reassert block for the full reasoning. Only the main
            # window's mere *existence* is still checked live (a plain
            # title lookup, not a style-bit read) -- a cheap sanity net
            # against a stale flag left behind by an unclean shutdown.
            main_hwnd = win_native.find_window_containing("brndz.wav Monitor")
            if main_hwnd is None:
                wants_topmost = False
                yield_front_to_eq = False
            else:
                live_cfg = load_config([])
                # Real bug fixed here: this used to be
                # `live_cfg.always_on_top and not live_cfg.compact_mode_active`
                # -- i.e. fully DROP OUT of the topmost band the instant the
                # EQ compact overlay was active, on the theory that this
                # window should rank "below EQ". But Windows only has a
                # binary topmost/not-topmost distinction, no sub-priority
                # levels -- dropping out entirely didn't rank this window
                # below EQ specifically, it sank it below EVERY window,
                # including an ordinary browser tab or someone else's
                # fullscreen video, which is exactly the bug reported
                # ("clico em outra janela... a janela fecha"/falls behind).
                # Now it keeps its own topmost membership for as long as its
                # own condition (the toggle) says so, and only *yields the
                # front of the band* (skips re-claiming it) while EQ compact
                # is active, letting EQ's own reassert cycle win the front
                # spot instead of the two fighting over it every 1.5s.
                wants_topmost = live_cfg.always_on_top
                yield_front_to_eq = live_cfg.compact_mode_active
            if wants_topmost != is_topmost:
                win_native.set_always_on_top(hwnd, wants_topmost)
                is_topmost = wants_topmost
            elif (
                wants_topmost and not yield_front_to_eq
                and win_native.find_window_containing("Configurações") is None
                and win_native.find_window_containing("Gerenciador de Tarefas") is None
            ):
                # Still reassert periodically even when the state hasn't
                # changed -- another topmost window (STREAMING) could
                # have reclaimed the front of the band since our last
                # check. Skipped while Windows' own Settings or Task
                # Manager window is open -- those get absolute priority
                # regardless of any other window's always-on-top state
                # (see main.py's _pin_window_topmost).
                win_native.set_always_on_top(hwnd, True)

        w, h = screen.get_size()
        screen.fill(theme.BG)

        # -- header --------------------------------------------------------
        title = font_title.render(f"Mapa de Rede — {prefix}.0/24", True, theme.TEXT)
        screen.blit(title, (16, 12))

        rescan_rect.topleft = (w - 500, 13)
        save_rect.topleft = (w - 340, 13)
        copy_rect.topleft = (w - 160, 13)
        _draw_button(screen, font_btn, rescan_rect, "Escaneando..." if scanning else "Re-escanear", enabled=not scanning)
        _draw_button(screen, font_btn, save_rect, "Salvar mapa (.md)", enabled=bool(hosts) and not scanning,
                     active=saved_path is not None and time.time() < saved_flash_until)
        _draw_button(screen, font_btn, copy_rect, "IP copiado!" if time.time() < copy_flash_until else "Copiar IP",
                     enabled=bool(selected_ip), active=time.time() < copy_flash_until)

        if scanning:
            done, total = progress
            frac = done / total if total else 0
            bar_rect = pygame.Rect(16, 40, w - 32, 6)
            pygame.draw.rect(screen, theme.PANEL_BORDER, bar_rect, border_radius=3)
            fill_w = int(bar_rect.width * frac)
            if fill_w > 0:
                pygame.draw.rect(screen, theme.ACCENT, pygame.Rect(bar_rect.x, bar_rect.y, fill_w, bar_rect.height), border_radius=3)
        elif status_msg:
            msg_color = theme.CRIT if "erro" in status_msg.lower() else theme.TEXT_DIM
            screen.blit(font_sm.render(status_msg, True, msg_color), (16, 40))

        pygame.draw.line(screen, theme.PANEL_BORDER, (0, HEADER_H), (w, HEADER_H))

        # -- table -----------------------------------------------------------
        table_rect = pygame.Rect(0, HEADER_H, w, h - HEADER_H - FOOTER_H)
        col_ip, col_host, col_mac, col_type = 16, 150, 400, 560
        col_ports = 700

        header_y = table_rect.y + 8
        for label, x in [("IP", col_ip), ("HOSTNAME", col_host), ("MAC", col_mac),
                          ("TIPO", col_type), ("PORTAS", col_ports)]:
            screen.blit(font_sm.render(label, True, theme.TEXT_LABEL), (x, header_y))

        content_top = table_rect.y + 30
        max_scroll = max(0, len(hosts) * ROW_H - (table_rect.height - 30))
        scroll_y = min(scroll_y, max_scroll)

        clip = screen.get_clip()
        screen.set_clip(pygame.Rect(0, content_top, w, table_rect.height - 30))
        for i, host in enumerate(hosts):
            y = content_top + i * ROW_H - scroll_y
            if y + ROW_H < content_top or y > table_rect.bottom:
                continue
            is_camera = "Câmera" in host.device_guess or "PTZ" in host.device_guess
            color = theme.ACCENT if is_camera else theme.TEXT
            if is_camera:
                pygame.draw.rect(screen, theme.PANEL_BG, pygame.Rect(0, y, w, ROW_H))
                pygame.draw.rect(screen, theme.ACCENT, pygame.Rect(0, y, 4, ROW_H))
            if host.ip == selected_ip:
                pygame.draw.rect(screen, theme.PANEL_BORDER, pygame.Rect(0, y, w, ROW_H), width=1)
            screen.blit(font_row.render(host.ip, True, color), (col_ip, y + 3))
            screen.blit(font_row.render(host.hostname[:32], True, color), (col_host, y + 3))
            screen.blit(font_row.render(host.mac, True, color), (col_mac, y + 3))
            screen.blit(font_row.render(host.device_guess, True, color), (col_type, y + 3))
            ports_txt = ",".join(str(p) for p in host.open_ports) if host.open_ports else "-"
            screen.blit(font_row.render(ports_txt, True, color), (col_ports, y + 3))
        screen.set_clip(clip)

        if not scanning and not hosts:
            empty = font_row.render("Nenhum dispositivo respondeu.", True, theme.TEXT_DIM)
            screen.blit(empty, (16, content_top + 8))

        # -- footer ------------------------------------------------------------
        pygame.draw.line(screen, theme.PANEL_BORDER, (0, h - FOOTER_H), (w, h - FOOTER_H))
        hint = font_sm.render(
            "Clique seleciona (Copiar IP) · clique duplo abre http://IP no navegador · destaque = provável câmera/PTZ",
            True, theme.TEXT_LABEL,
        )
        screen.blit(hint, (16, h - FOOTER_H + 16))

        pygame.display.flip()

    if worker:
        worker.stop()
    pygame.quit()
    sys.exit(0)


if __name__ == "__main__":
    main()
