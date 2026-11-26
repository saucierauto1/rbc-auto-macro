"""Stage 2: screen capture of the Roblox client area and its regions.

* Frames are numpy arrays, shape (H, W, 3), dtype uint8, **BGR** (OpenCV order).
* ``mss`` does the grabbing. Each thread gets its own mss instance, because
  mss handles are not thread-safe.
* ``FrameCache`` keeps a grab for ``max_age_s`` so several detectors asking
  for the same region in one tick share a single screenshot.
* The backend is swappable, so tests use ``FakeBackend`` without a screen.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from ..core.config import AppConfig
from ..core.errors import WindowLostError
from ..core.timing import DEFAULT_CLOCK, Clock
from . import window as win_mod
from .regions import to_pixels
from .window import Rect, WindowInfo


class Backend(Protocol):
    def grab(self, rect: Rect) -> np.ndarray: ...


class MssBackend:
    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def grab(self, rect: Rect) -> np.ndarray:
        shot = self._sct().grab({"left": rect.left, "top": rect.top,
                                 "width": rect.width, "height": rect.height})
        return np.asarray(shot, dtype=np.uint8)[:, :, :3].copy()   # BGRA -> BGR


class FakeBackend:
    """Returns slices of a fixed 'screen' image. For tests and offline replays."""

    def __init__(self, screen: np.ndarray):
        self.screen = screen
        self.grabs = 0

    def grab(self, rect: Rect) -> np.ndarray:
        self.grabs += 1
        return self.screen[rect.top:rect.bottom, rect.left:rect.right].copy()


def backend_available() -> bool:
    try:
        import mss  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    rect: Rect               # where on screen it came from
    taken_at: float


class FrameCache:
    def __init__(self, max_age_s: float, clock: Clock = DEFAULT_CLOCK):
        self.max_age_s = max_age_s
        self._clock = clock
        self._frames: dict[tuple, Frame] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple, make: Callable[[], Frame]) -> Frame:
        now = self._clock.now()
        with self._lock:
            f = self._frames.get(key)
            if f is not None and now - f.taken_at <= self.max_age_s:
                return f
        f = make()
        with self._lock:
            self._frames[key] = f
        return f

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()


class ScreenCapture:
    """Grab the whole client area or a named config region."""

    def __init__(self, cfg: AppConfig, backend: Backend | None = None,
                 find_window: Callable[[str], WindowInfo | None] | None = None,
                 clock: Clock = DEFAULT_CLOCK):
        self.cfg = cfg
        self.backend = backend or MssBackend()
        self._find = find_window or (lambda t: win_mod.find_window(t, cfg.window.process_name))
        self._clock = clock
        self.cache = FrameCache(cfg.capture.cache_max_age_ms / 1000.0, clock)
        self.window: WindowInfo | None = None
        self._last_window_check = -1e9

    # -- window ---------------------------------------------------------------
    def locate(self, force: bool = False) -> WindowInfo:
        """Find (or re-check) the Roblox window, at most window_check_hz times a second."""
        now = self._clock.now()
        if force or self.window is None or now - self._last_window_check >= 1.0 / self.cfg.scan_rates.window_check_hz:
            w = self._find(self.cfg.window.title_contains)
            self._last_window_check = now
            if w is None:
                self.window = None
                raise WindowLostError("Roblox window not found")
            if w.minimized:
                raise WindowLostError("Roblox window is minimised")
            if self.window and (w.client != self.window.client):
                self.cache.clear()      # moved or resized: cached frames are stale
            self.window = w
        assert self.window is not None
        return self.window

    @property
    def client(self) -> Rect:
        w = self.locate()
        if w.client is None:
            raise WindowLostError("Roblox client area unknown")
        return w.client

    # -- grabbing -------------------------------------------------------------
    def region_rect(self, name: str) -> Rect:
        if name not in self.cfg.regions:
            raise KeyError(f"unknown region '{name}' (known: {', '.join(self.cfg.regions)})")
        return to_pixels(self.cfg.regions[name], self.client)

    def grab_client(self, fresh: bool = False) -> Frame:
        rect = self.client
        return self._grab(("client", rect), rect, fresh)

    def grab_region(self, name: str, fresh: bool = False) -> Frame:
        rect = self.region_rect(name)
        return self._grab((name, rect), rect, fresh)

    def _grab(self, key: tuple, rect: Rect, fresh: bool) -> Frame:
        make = lambda: Frame(self.backend.grab(rect), rect, self._clock.now())  # noqa: E731
        if fresh:
            f = make()
            self.cache._frames[key] = f
            return f
        return self.cache.get(key, make)
