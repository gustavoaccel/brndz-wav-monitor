"""One-shot LAN device scan: ping sweep + hostname + MAC + a light port probe
to flag likely camera/AV gear (RTSP, PTZ control, web UI). Reimplements the
AV Toolkit's `network_scan.ps1` in Python so it can run from a GUI window
instead of a console script producing only a .md file.

Pure logic, no UI -- `network_mapper.py` drives this from a pygame window.
"""
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import win_native

_CREATE_NO_WINDOW = 0x08000000

# Ports worth a quick knock: strong signals for cameras/PTZ/AV-over-IP gear,
# not a general-purpose port scanner.
_PROBE_PORTS = {
    80: "HTTP",
    8080: "HTTP",
    8000: "HTTP",
    554: "RTSP",
    5678: "VISCA",
    23: "Telnet",
}

_CAMERA_HOSTNAME_HINTS = [
    "cam", "ptz", "hikvision", "dahua", "axis", "bosch", "avonic",
    "vaddio", "birddog", "marshall", "panasonic", "lumens", "jvc",
]

# MAC OUI (first 3 bytes) -> manufacturer, for the handful of brands that
# dominate the CCTV/PTZ market (Hikvision and Dahua alone cover a huge
# share, including a lot of white-label/OEM gear sold under other names).
# Sourced from public OUI lookups (maclookup.app / netify.ai), not
# exhaustive -- this is the one signal that's always available regardless
# of firewalls, AP client isolation, or DNS/NetBIOS not being set up on the
# venue's network, since it only needs the ARP-derived MAC (which works
# even when hostname/port probing don't).
_CAMERA_OUI_VENDORS = {
    "18:68:CB": "Hikvision", "28:57:BE": "Hikvision", "44:19:B6": "Hikvision",
    "4C:BD:8F": "Hikvision", "54:C4:15": "Hikvision", "64:DB:8B": "Hikvision",
    "94:E1:AC": "Hikvision", "A4:14:37": "Hikvision", "B4:A3:82": "Hikvision",
    "BC:AD:28": "Hikvision", "C0:56:E3": "Hikvision", "C4:2F:90": "Hikvision",
    "14:A7:8B": "Dahua", "38:AF:29": "Dahua", "3C:EF:8C": "Dahua",
    "4C:11:BF": "Dahua", "90:02:A9": "Dahua", "BC:32:5F": "Dahua", "E0:50:8B": "Dahua",
    "00:40:8C": "Axis", "AC:CC:8E": "Axis",
}


def _mac_vendor(mac: str) -> Optional[str]:
    if mac == "-" or len(mac) < 8:
        return None
    prefix = mac.upper().replace("-", ":")[:8]
    return _CAMERA_OUI_VENDORS.get(prefix)


@dataclass
class HostInfo:
    ip: str
    hostname: str = "-"
    mac: str = "-"
    open_ports: "List[int]" = field(default_factory=list)
    device_guess: str = "-"


def local_network_prefix() -> Optional[str]:
    """First three octets of the outbound-facing local IP -- doesn't
    actually send anything (UDP connect just picks a route/interface)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ".".join(ip.split(".")[:3])
    except Exception:
        return None


def _ping_once(ip: str, timeout_ms: int) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), ip],
            capture_output=True, text=True,
            timeout=(timeout_ms / 1000.0) + 1.0,
            creationflags=_CREATE_NO_WINDOW,
        )
        return result.returncode == 0 and "TTL" in result.stdout.upper()
    except Exception:
        return False


def _read_arp_table() -> "dict[str, str]":
    """IP -> MAC from the system ARP cache (populated by the ping sweep
    that ran just before this is called -- one `arp -a` call instead of
    querying per host)."""
    mac_by_ip = {}
    try:
        result = subprocess.run(
            ["arp", "-a"], capture_output=True, text=True,
            timeout=5.0, creationflags=_CREATE_NO_WINDOW,
        )
        pattern = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})")
        for line in result.stdout.splitlines():
            m = pattern.search(line)
            if m:
                mac_by_ip[m.group(1)] = m.group(2)
    except Exception:
        pass
    return mac_by_ip


def _reverse_dns(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return "-"


def _probe_ports(ip: str, timeout_s: float = 0.75) -> "List[int]":
    # Measured directly against real LAN devices: an open port answers in
    # 1-2ms, a closed/filtered one just eats the whole timeout with no RST
    # (no active refusal on this kind of network) -- so a short timeout
    # only risked false negatives on a slower/busier venue network, never
    # bought speed on the ports that were actually open.
    open_ports = []
    for port in _PROBE_PORTS:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout_s)
            if s.connect_ex((ip, port)) == 0:
                open_ports.append(port)
            s.close()
        except Exception:
            pass
    return open_ports


def _guess_device(hostname: str, mac: str, open_ports: "List[int]") -> str:
    # MAC vendor first: the only signal that doesn't depend on hostname
    # resolution or port probing actually reaching the device (both can be
    # blocked by AP client isolation or a venue network with no local DNS/
    # NetBIOS, while MAC comes straight from the ARP sweep and just works).
    vendor = _mac_vendor(mac)
    if vendor:
        return f"Câmera/PTZ ({vendor})"

    host_lower = hostname.lower()
    if any(k in host_lower for k in _CAMERA_HOSTNAME_HINTS):
        return "Câmera/PTZ"
    if 554 in open_ports:
        return "Câmera (RTSP)"
    if 5678 in open_ports:
        return "PTZ (VISCA)"
    if any(p in open_ports for p in (80, 8080, 8000)):
        return "Web/HTTP"
    if 23 in open_ports:
        return "Telnet"
    return "-"


def scan_network(
    prefix: str,
    ping_timeout_ms: int = 500,
    max_workers: int = 100,
    progress_cb: "Optional[Callable[[int, int], None]]" = None,
    stop_check: "Optional[Callable[[], bool]]" = None,
) -> "List[HostInfo]":
    """Pings prefix.1 .. prefix.254, then enriches whoever answered with
    hostname/MAC/open-port info. `progress_cb(done, total)` is called as
    the ping sweep progresses; `stop_check()` lets the caller cancel
    mid-scan (checked between phases and periodically during the sweep).
    """
    targets = [f"{prefix}.{i}" for i in range(1, 255)]
    total = len(targets)
    done = 0
    active_ips = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_ping_once, ip, ping_timeout_ms): ip for ip in targets}
        for future in futures:
            if stop_check and stop_check():
                break
            ip = futures[future]
            if future.result():
                active_ips.append(ip)
            done += 1
            if progress_cb:
                progress_cb(done, total)

    active_ips.sort(key=lambda ip: int(ip.split(".")[-1]))
    if stop_check and stop_check():
        return [HostInfo(ip=ip) for ip in active_ips]

    mac_by_ip = _read_arp_table()

    hosts = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        dns_results = dict(zip(active_ips, pool.map(_reverse_dns, active_ips)))
        port_results = dict(zip(active_ips, pool.map(_probe_ports, active_ips)))

    for ip in active_ips:
        hostname = dns_results.get(ip, "-")
        open_ports = port_results.get(ip, [])
        mac = mac_by_ip.get(ip, "-")
        hosts.append(HostInfo(
            ip=ip,
            hostname=hostname,
            mac=mac,
            open_ports=open_ports,
            device_guess=_guess_device(hostname, mac, open_ports),
        ))

    return hosts


def save_network_map(hosts: "List[HostInfo]", prefix: str, cfg) -> Path:
    """Same drive-detection + folder convention as session_log.py, under
    `Mapas_de_Rede` instead of `Logs_Evento` -- matches network_scan.ps1's
    existing output location so both tools' output lands in one place."""
    drive = win_native.find_drive_by_label(cfg.drive_label)
    log_dir = (drive / "AV_TOOLKIT" / "07_DOCUMENTATION" / "Mapas_de_Rede") if drive \
        else Path(cfg.fallback_log_dir) / "Mapas_de_Rede"
    log_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = log_dir / f"scan_{stamp}.md"

    lines = [
        f"# Scan de rede - {prefix}.0/24",
        f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        f"Máquina que rodou o scan: brndz.wav Monitor",
        "",
        f"{len(hosts)} dispositivos respondendo de 254 endereços testados.",
        "",
        "| IP | Hostname | MAC | Tipo | Portas abertas |",
        "|---|---|---|---|---|",
    ]
    for h in hosts:
        ports = ", ".join(str(p) for p in h.open_ports) if h.open_ports else "-"
        lines.append(f"| {h.ip} | {h.hostname} | {h.mac} | {h.device_guess} | {ports} |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
