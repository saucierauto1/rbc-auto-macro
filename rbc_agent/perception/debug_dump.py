"""Save frames as PNG (pure Python, no Pillow/OpenCV needed) with optional
region overlays, and keep the debug folder under its size limit."""

from __future__ import annotations

import struct
import zlib
from datetime import datetime
from pathlib import Path

import numpy as np

from ..core.config import AppConfig
from ..core.paths import resolve
from .regions import to_local, to_pixels
from .window import Rect

OVERLAY_COLORS_BGR = [(50, 182, 242), (255, 196, 124), (155, 227, 155), (255, 166, 195),
                      (87, 158, 255), (107, 107, 255), (230, 230, 230)]


def write_png(path: Path, bgr: np.ndarray) -> Path:
    """Write an (H, W, 3) BGR uint8 array as an RGB PNG."""
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        raise ValueError("expected an (H, W, 3) image")
    rgb = np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.uint8)
    h, w = rgb.shape[:2]
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def draw_box(img: np.ndarray, r: Rect, color: tuple[int, int, int], thickness: int = 2) -> None:
    h, w = img.shape[:2]
    x0, y0 = max(0, r.left), max(0, r.top)
    x1, y1 = min(w, r.right), min(h, r.bottom)
    if x1 <= x0 or y1 <= y0:
        return
    t = thickness
    img[y0:min(y0 + t, y1), x0:x1] = color
    img[max(y1 - t, y0):y1, x0:x1] = color
    img[y0:y1, x0:min(x0 + t, x1)] = color
    img[y0:y1, max(x1 - t, x0):x1] = color


def overlay_regions(img: np.ndarray, cfg: AppConfig, client: Rect) -> np.ndarray:
    out = img.copy()
    for i, (name, region) in enumerate(cfg.regions.items()):
        draw_box(out, to_local(to_pixels(region, client), client), OVERLAY_COLORS_BGR[i % len(OVERLAY_COLORS_BGR)])
    return out


class DebugDumper:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.dir = resolve(cfg.debug.screenshot_dir)

    def save(self, img: np.ndarray, label: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        p = write_png(self.dir / f"{stamp}_{safe}.png", img)
        self.prune()
        return p

    def prune(self) -> int:
        """Delete the oldest PNGs above debug.max_screenshots. Returns how many."""
        limit = self.cfg.debug.max_screenshots
        if limit == 0:          # 0 = keep everything
            return 0
        files = sorted(self.dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        extra = files[:-limit]
        for p in extra:
            p.unlink(missing_ok=True)
        return len(extra)
