"""Thin ctypes wrappers around a few Win32 calls.

We use raw ctypes here instead of pywin32: it's one less heavy dependency
to install (Windows-only build anyway, so no portability lost) and the two
calls we need (GetVolumeInformationW, IsHungAppWindow) are trivial to bind
directly. Deviates from the spec's suggested `pywin32`, documented here.
"""
import ctypes
import string
from ctypes import wintypes
from pathlib import Path
from typing import Optional

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32


def find_drive_by_label(label: str) -> Optional[Path]:
    """Scan fixed drive letters for a volume whose label matches `label`.

    Mirrors the PowerShell prototype's approach: find the drive by volume
    name, not by letter, since the letter assigned to a USB HD can change
    between machines/ports.
    """
    label = label.strip().lower()
    volume_name_buf = ctypes.create_unicode_buffer(261)
    fs_name_buf = ctypes.create_unicode_buffer(261)

    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        # DRIVE_REMOVABLE=2, DRIVE_FIXED=3 -- the toolkit HD could be either
        if drive_type not in (2, 3):
            continue

        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            volume_name_buf,
            ctypes.sizeof(volume_name_buf),
            None,
            None,
            None,
            fs_name_buf,
            ctypes.sizeof(fs_name_buf),
        )
        if not ok:
            continue

        if volume_name_buf.value.strip().lower() == label:
            return Path(root)

    return None


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def is_process_hung(pid: int) -> bool:
    """True if any top-level window owned by `pid` is reported hung by the OS.

    Windows itself tracks "not responding" via the message pump, exposed as
    IsHungAppWindow. We enumerate top-level windows, keep the ones owned by
    our pid, and ask the OS directly rather than guessing from CPU usage.
    """
    hung = False
    found_window = False

    def callback(hwnd, _lparam):
        nonlocal hung, found_window
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        found_window = True
        if user32.IsHungAppWindow(hwnd):
            hung = True
            return False
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return hung if found_window else False


_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010

user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL


def set_always_on_top(hwnd: int, enabled: bool) -> bool:
    """Pin/unpin the window above all others via SetWindowPos(HWND_TOPMOST).
    No pygame/SDL API for this, so it's the one bit of direct window
    management we do ourselves."""
    insert_after = _HWND_TOPMOST if enabled else _HWND_NOTOPMOST
    return bool(user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE))


# ---- compact/overlay window mode: colorkey transparency + manual drag -----
#
# Chosen over pywin32 for the same reason as the rest of this module: it's
# one call each to GetWindowLongW/SetWindowLongW/SetLayeredWindowAttributes,
# no reason to pull in a heavier dependency for that. Colorkey (not
# per-pixel alpha) is deliberate too -- it's the simpler of the two
# transparency mechanisms Win32 offers, at the cost of hard edges (no
# anti-aliasing) on whatever we draw against it; good enough for a bars-only
# overlay, upgrade to UpdateLayeredWindow-based per-pixel alpha later if the
# hard edges ever bother anyone.

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_LWA_COLORKEY = 0x00000001
_SWP_NOZORDER = 0x0004

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


def enable_colorkey_transparency(hwnd: int, rgb: "tuple[int, int, int]") -> bool:
    """Any pixel we draw in exactly `rgb` becomes fully transparent (click
    still lands on us, not through us -- only WS_EX_TRANSPARENT would make
    clicks pass through, which we deliberately don't set since dragging the
    overlay needs our own mouse events)."""
    style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_LAYERED)
    colorref = rgb[0] | (rgb[1] << 8) | (rgb[2] << 16)  # COLORREF is 0x00BBGGRR
    return bool(user32.SetLayeredWindowAttributes(hwnd, wintypes.DWORD(colorref), 0, _LWA_COLORKEY))


def disable_layered(hwnd: int) -> None:
    style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style & ~_WS_EX_LAYERED)


def get_cursor_pos() -> "tuple[int, int]":
    """Screen-absolute mouse position -- pygame only exposes window-relative
    coordinates, useless for moving the window itself while dragging it."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_window_rect(hwnd: int) -> "tuple[int, int, int, int]":
    """(left, top, right, bottom) in screen coordinates."""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def move_window(hwnd: int, x: int, y: int) -> bool:
    """Reposition without resizing or touching z-order -- used for the
    frameless compact window, which has no title bar to drag by."""
    return bool(user32.SetWindowPos(hwnd, None, x, y, 0, 0, _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE))


# ---- process launching / elevation -----------------------------------
#
# Task Manager was launched via a bare `subprocess.Popen(["taskmgr.exe"])`,
# relying on PATH resolution -- fine normally, but PATH can be stripped or
# different under an elevated/UAC-launched process. ShellExecuteW against
# the real System32 path (resolved via GetSystemDirectoryW, which also
# handles WOW64 redirection correctly) doesn't depend on PATH at all.

shell32 = ctypes.windll.shell32
shell32.ShellExecuteW.argtypes = [
    wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
]
shell32.ShellExecuteW.restype = wintypes.HINSTANCE
shell32.IsUserAnAdmin.restype = wintypes.BOOL

kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
kernel32.GetSystemDirectoryW.restype = wintypes.UINT
kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel32.GetModuleFileNameW.restype = wintypes.DWORD

_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20
_SW_SHOWNORMAL = 1


def is_admin() -> bool:
    """Whether this process has an elevated administrator token."""
    try:
        token = wintypes.HANDLE()
        if not kernel32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            class _TokenElevation(ctypes.Structure):
                _fields_ = [("TokenIsElevated", wintypes.DWORD)]
            elev = _TokenElevation()
            size = wintypes.DWORD()
            ok = kernel32.GetTokenInformation(
                token, _TOKEN_ELEVATION, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size),
            )
            return bool(ok and elev.TokenIsElevated)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        try:
            return bool(shell32.IsUserAnAdmin())
        except Exception:
            return False


def system_directory() -> Path:
    buf = ctypes.create_unicode_buffer(32768)
    n = kernel32.GetSystemDirectoryW(buf, len(buf))
    return Path(buf.value) if n else Path(r"C:\Windows\System32")


def current_executable_path() -> str:
    buf = ctypes.create_unicode_buffer(32768)
    n = kernel32.GetModuleFileNameW(None, buf, len(buf))
    return buf.value[:n]


def launch_executable(path, args: Optional[str] = None, verb: str = "open") -> "tuple[bool, str]":
    """Launch via the Windows Shell (not subprocess) -- returns (ok, detail)."""
    exe = str(path)
    try:
        rc = shell32.ShellExecuteW(None, verb, exe, args, str(Path(exe).parent), _SW_SHOWNORMAL)
        value = ctypes.cast(rc, ctypes.c_void_p).value or 0
        if value <= 32:  # ShellExecute's own convention: <=32 means failure
            return False, f"ShellExecuteW retornou {value}"
        return True, "iniciado"
    except Exception as e:
        return False, str(e)


def launch_task_manager() -> "tuple[bool, str]":
    """Absolute System32 path first (immune to PATH quirks); bare name as
    a last-resort fallback if that binary is somehow missing."""
    exe = system_directory() / "Taskmgr.exe"
    if exe.exists():
        return launch_executable(exe)
    return launch_executable("taskmgr.exe")


def relaunch_as_admin(arguments: Optional[str] = None) -> "tuple[bool, str]":
    """Restart this exe elevated via the UAC 'runas' shell verb."""
    return launch_executable(current_executable_path(), arguments, verb="runas")


# ---- GPU Engine performance counters (PDH) -----------------------------
#
# Vendor-agnostic GPU utilization fallback for when pynvml (NVIDIA-only)
# isn't available. `\GPU Engine(*)\Utilization Percentage` is a Windows
# 10+ DXGK-level counter -- exposed by the OS's own graphics scheduler for
# *any* GPU (NVIDIA/AMD/Intel alike), not something vendor drivers opt
# into individually. ctypes over pdh.dll, same rationale as the rest of
# this module: one less dependency than pywin32 for a handful of calls.

pdh = ctypes.windll.pdh

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2
_PDH_NO_DATA = 0x800007D5
_PDH_INVALID_DATA = 0xC0000BC6


class _PdhFmtCounterValueDouble(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _PdhFmtCounterValueItemW(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhFmtCounterValueDouble)]


pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhOpenQueryW.restype = wintypes.DWORD
pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
pdh.PdhCollectQueryData.restype = wintypes.DWORD
pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
pdh.PdhCloseQuery.restype = wintypes.DWORD


class GpuEngineCounters:
    """Wraps one PDH query against a wildcard `\\<object>(*)\\<counter>`
    path (e.g. `\\GPU Engine(*)\\Utilization Percentage`, `\\GPU Process
    Memory(*)\\Dedicated Usage`), returning {instance_name: value} per read.

    Like most PDH rate counters, the first CollectQueryData after opening
    has nothing to compute a delta from yet (PDH_NO_DATA/PDH_INVALID_DATA)
    -- callers should expect an empty dict on the very first read() and
    real values from the second one on, same as this class's own internal
    two-sample nature (it's the query handle, not the caller, that
    remembers the previous raw sample between calls).
    """

    def __init__(self, path: str):
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._ok = False
        try:
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            if pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counter)) != 0:
                return
            self._ok = True
        except Exception:
            self._ok = False

    @property
    def available(self) -> bool:
        return self._ok

    def read(self) -> dict:
        if not self._ok:
            return {}
        try:
            status = pdh.PdhCollectQueryData(self._query)
            if status not in (0,):
                return {}

            buf_size = wintypes.DWORD(0)
            item_count = wintypes.DWORD(0)
            status = pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count), None,
            )
            if status != _PDH_MORE_DATA or item_count.value == 0:
                return {}

            buffer = ctypes.create_string_buffer(buf_size.value)
            status = pdh.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE, ctypes.byref(buf_size), ctypes.byref(item_count),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
            if status != 0:
                return {}

            items = ctypes.cast(buffer, ctypes.POINTER(_PdhFmtCounterValueItemW))
            result = {}
            for i in range(item_count.value):
                item = items[i]
                if item.FmtValue.CStatus == 0:
                    result[item.szName] = item.FmtValue.doubleValue
            return result
        except Exception:
            return {}

    def close(self):
        if self._query:
            try:
                pdh.PdhCloseQuery(self._query)
            except Exception:
                pass
        self._ok = False
