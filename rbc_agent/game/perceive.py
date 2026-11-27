"""One screen read: grab the Roblox client area, OCR the RBC panels, parse the text.

The single place that chains capture -> ScreenReader -> parse_screen. Used by
the Read screen and Watch buttons, and by the Stage 5 state machine.

Create a ``Perceiver`` in the thread that uses it and close it there (use it as
a ``with`` block): the screen-capture handles belong to that thread.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.config import AppConfig
from ..core.errors import WindowLostError
from ..perception.capture import ScreenCapture
from ..perception.ocr import Tesseract
from ..perception.screen_reader import RawScreen, Reader, ScreenReader
from .hive import BeeInfo, load_catalog
from .rules import RBCRules
from .screens import ScreenState, parse_screen


@dataclass(frozen=True)
class Reading:
    state: ScreenState        # what the text means
    raw: RawScreen            # the text and where each piece was read (for debug pictures)
    image: np.ndarray         # the client-area frame it came from (BGR)


class Perceiver:
    def __init__(self, cfg: AppConfig, rules: RBCRules, catalog: dict[str, BeeInfo] | None = None, *,
                 capture: ScreenCapture | None = None, ocr: Reader | None = None):
        self.rules = rules
        self.catalog = catalog if catalog is not None else load_catalog()
        # OCR first: if Tesseract is missing this raises before any screen handle exists.
        self.reader = ScreenReader(ocr or Tesseract(cfg.ocr), cfg.ocr)
        self.capture = capture if capture is not None else ScreenCapture(cfg)

    def read(self) -> Reading:
        """Read the screen once. Raises WindowLostError if Roblox isn't found or is minimised."""
        image = self.capture.grab_client(fresh=True).image
        raw = self.reader.read(image)
        return Reading(parse_screen(raw.kind, raw.texts, raw.shop_rows, self.rules, self.catalog), raw, image)

    def read_state(self) -> ScreenState:
        """For loops: no Roblox window counts as an unknown screen instead of an error."""
        try:
            return self.read().state
        except WindowLostError:
            return ScreenState("unknown")

    def close(self) -> None:
        self.capture.close()

    def __enter__(self) -> "Perceiver":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
