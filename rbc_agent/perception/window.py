"""Find the Roblox window (read-only: title, size, focus). Windows only; returns
None elsewhere. No input is sent and nothing is read from the game's memory."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    width: int
    height: int
    focused: bool
    minimized: bool


def find_window(title_contains: str = "Roblox") -> WindowInfo | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[WindowInfo] = []
    fg = user32.GetForegroundWindow()
    needle = title_contains.lower()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        # "Roblox" exactly is the game client; skip e.g. browser tabs about Roblox.
        if needle in title.lower() and not any(x in title.lower() for x in ("chrome", "edge", "firefox", "opera", "studio", "rbc agent")):
            r = wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            found.append(WindowInfo(int(hwnd or 0), title, r.right - r.left, r.bottom - r.top,
                                    hwnd == fg, bool(user32.IsIconic(hwnd))))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None
