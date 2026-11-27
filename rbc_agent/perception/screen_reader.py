"""Stage 3: find the RBC panels on a frame and OCR their text boxes.

Pixels -> text only. Turning text into quests, prices and bees is
``game/screens.py``. Layout lives in ``data/rbc_screens.json``.

Each panel is located by its solid green title bar (the "anchor"). Boxes are
stored as pixels on a 1920x1080 screenshot and are moved and scaled with the
anchor that was found, so the same layout works at any window size.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from ..core.config import OcrConfig
from ..core.paths import SCREENS_FILE
from .window import Rect

KINDS = ("quest_choice", "bee_pick", "upgrade_shop", "dialog", "in_round", "unknown")


class Reader(Protocol):
    def read(self, img: np.ndarray, *, mode: str = "gray", psm: int = 6) -> str: ...


def load_layout(path: Path = SCREENS_FILE) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _longest_run(flags: np.ndarray) -> tuple[int, int]:
    """(start, end) of the longest run of True values; (0, 0) if none."""
    best, start = (0, 0), None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    return best


def find_anchor(img: np.ndarray, search: list[float], aspect: list[float],
                cfg: OcrConfig) -> Rect | None:
    """Locate a solid green title bar inside ``search`` (normalised x, y, w, h)."""
    H, W = img.shape[:2]
    left, top = int(search[0] * W), int(search[1] * H)
    crop = img[top:top + int(search[3] * H), left:left + int(search[2] * W)].astype(np.int16)
    r, g, b = cfg.anchor_rgb
    tol = cfg.anchor_tolerance
    mask = ((abs(crop[..., 2] - r) <= tol) & (abs(crop[..., 1] - g) <= tol)
            & (abs(crop[..., 0] - b) <= tol))
    rows = mask.sum(axis=1)
    if rows.size == 0 or rows.max() < 20:
        return None
    # Width from the rows above the title text (nearly all green)...
    y0, y1 = _longest_run(rows >= 0.9 * rows.max())
    x0, x1 = _longest_run(mask[y0:y1].sum(axis=0) >= 0.5 * (y1 - y0))
    if x1 - x0 < 20:
        return None
    # ...height from the bar's plain left and right ends, which the text never reaches.
    m = max(2, (x1 - x0) // 33)
    ends = (mask[:, x0:x0 + m].mean(axis=1) >= 0.8) & (mask[:, x1 - m:x1].mean(axis=1) >= 0.8)
    y0, y1 = _longest_run(ends)
    w, h = x1 - x0, y1 - y0
    if w < 20 or h < 8:
        return None
    if mask[y0:y1, x0:x1].mean() < 0.5 or not aspect[0] <= w / h <= aspect[1]:
        return None
    return Rect(left + x0, top + y0, w, h)


@dataclass
class RawScreen:
    kind: str
    texts: dict[str, str] = field(default_factory=dict)
    # Upgrade shop only: (header, effects) text per row, top to bottom.
    shop_rows: list[tuple[str, str]] = field(default_factory=list)
    anchor: Rect | None = None
    boxes: dict[str, Rect] = field(default_factory=dict)   # where each text came from


class ScreenReader:
    def __init__(self, ocr: Reader, cfg: OcrConfig, layout: dict | None = None):
        self.ocr = ocr
        self.cfg = cfg
        self.layout = layout if layout is not None else load_layout()

    # -- geometry -------------------------------------------------------------
    @staticmethod
    def _place(box: list, ref_anchor: list, anchor: Rect, scale_by: str, dy: float = 0) -> Rect:
        ax0, ay0, ax1, ay1 = ref_anchor
        s = anchor.width / (ax1 - ax0) if scale_by == "width" else anchor.height / (ay1 - ay0)
        l, t, r, b = box[0], box[1] + dy, box[2], box[3] + dy
        return Rect(int(anchor.left + (l - ax0) * s), int(anchor.top + (t - ay0) * s),
                    max(1, int((r - l) * s)), max(1, int((b - t) * s)))

    def _read_box(self, img: np.ndarray, rect: Rect, mode: str, psm: int) -> str:
        H, W = img.shape[:2]
        l, t = max(0, rect.left), max(0, rect.top)
        r, b = min(W, rect.right), min(H, rect.bottom)
        if r <= l or b <= t:
            return ""
        return self.ocr.read(img[t:b, l:r], mode=mode, psm=psm)

    def _read_group(self, img, out: RawScreen, group: dict, boxes: dict, anchor: Rect,
                    dy: float = 0, suffix: str = "") -> None:
        for name, box in boxes.items():
            if name.startswith("_") or not isinstance(box, list):
                continue
            rect = self._place(box, group["anchor"], anchor, group["scale_by"], dy)
            out.boxes[name + suffix] = rect
            out.texts[name + suffix] = self._read_box(img, rect, box[4], box[5])

    # -- reading --------------------------------------------------------------
    def read(self, img: np.ndarray) -> RawScreen:
        """OCR whichever RBC panel is on the frame (BGR client-area image)."""
        dlg = self.layout["dialog"]
        anchor = find_anchor(img, dlg["search"], dlg["aspect"], self.cfg)
        if anchor is not None:
            out = RawScreen("dialog", anchor=anchor)
            self._read_group(img, out, dlg, dlg["boxes"], anchor)
            sub = out.texts.get("subtitle", "").lower()
            for kind, words in self.layout["subtitles"].items():
                if any(w in sub for w in words):
                    out.kind = kind
                    break
            if out.kind == "upgrade_shop":
                self._read_shop(img, out, dlg, anchor)
            elif out.kind in dlg:
                self._read_group(img, out, dlg, dlg[out.kind], anchor)
            return out

        hud = self.layout["hud"]
        anchor = find_anchor(img, hud["search"], hud["aspect"], self.cfg)
        if anchor is not None:
            out = RawScreen("in_round", anchor=anchor)
            self._read_group(img, out, hud, hud["boxes"], anchor)
            if "round" not in out.texts.get("round_time", "").lower():
                out.kind = "unknown"
            return out
        return RawScreen("unknown")

    def _read_shop(self, img, out: RawScreen, dlg: dict, anchor: Rect) -> None:
        shop = dlg["upgrade_shop"]
        for i in range(shop["max_rows"]):
            boxes = {"header": shop["header"], "effects": shop["effects"]}
            self._read_group(img, out, dlg, boxes, anchor, dy=i * shop["pitch"], suffix=f"_{i}")
            header = out.texts[f"header_{i}"]
            if "cost" not in header.lower():
                break                                   # no more rows
            out.shop_rows.append((header, out.texts[f"effects_{i}"]))
