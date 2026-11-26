"""Stage 2: find the Roblox window, its client area on screen, DPI and focus.

Read-only Win32 calls through ctypes: window title, rectangles, foreground
window. No input is sent and nothing is read from the game's memory. On
non-Windows systems every function returns None/False, so tests run anywhere.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

# Titles that contain "Roblox" but are not the game client.
_EXCLUDE = ("chrome", "edge", "firefox", "opera", "brave", "studio", "rbc agent", "discord",
            "explorer", "visual studio", "notepad")

_dpi_done = False


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    width: int              # client area size in physical pixels
    height: int
    focused: bool
    minimized: bool
    client: Rect | None = None   # client area in screen coordinates
    dpi_scale: float = 1.0

    @property
    def usable(self) -> bool:
        return not self.minimized and self.width >= 320 and self.height >= 240


def make_dpi_aware() -> None:
    """Make Win32 report physical pixels (otherwise 125%/150% scaling breaks
    every coordinate). Safe to call more than once."""
    global _dpi_done
    if _dpi_done or sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
    _dpi_done = True


def is_game_title(title: str, needle: str = "Roblox") -> bool:
    t = title.lower()
    return needle.lower() in t and not any(x in t for x in _EXCLUDE)


def _info(hwnd: int, title: str) -> WindowInfo:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = r.right - r.left, r.bottom - r.top
    try:
        dpi = user32.GetDpiForWindow(hwnd) / 96.0
    except (AttributeError, OSError):
        dpi = 1.0
    return WindowInfo(int(hwnd), title, w, h, hwnd == user32.GetForegroundWindow(),
                      bool(user32.IsIconic(hwnd)), Rect(pt.x, pt.y, w, h), dpi or 1.0)


def _process_name(hwnd) -> str:
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def find_window(title_contains: str = "Roblox", process_name: str = "RobloxPlayerBeta.exe") -> WindowInfo | None:
    """The biggest visible game window: matched by title, or (if the title
    is different, e.g. renamed by a launcher) by the Roblox process name."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    make_dpi_aware()
    user32 = ctypes.windll.user32
    found: list[WindowInfo] = []
    proc = process_name.lower()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if is_game_title(buf.value, title_contains) or (
                        proc and _process_name(hwnd).lower() == proc):
                    found.append(_info(hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    return max(found, key=lambda w: (not w.minimized, w.width * w.height))


def refresh(win: WindowInfo) -> WindowInfo | None:
    """Re-read position/size/focus of a known window (None if it closed)."""
    if sys.platform != "win32":
        return None
    import ctypes
    if not ctypes.windll.user32.IsWindow(win.hwnd):
        return None
    return _info(win.hwnd, win.title)


def focus(win: WindowInfo) -> bool:
    """Bring Roblox to the front. Only used when the user enabled
    window.refocus_automatically. Returns True if it is now focused."""
    if sys.platform != "win32":
        return False
    import ctypes
    user32 = ctypes.windll.user32
    if win.minimized:
        user32.ShowWindow(win.hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(win.hwnd)
    return user32.GetForegroundWindow() == win.hwnd
