"""Perceiver: one shared capture -> OCR -> parse read for Read screen, Watch and Stage 5."""

from pathlib import Path

import numpy as np
import pytest

from rbc_agent.core.config import parse_config
from rbc_agent.core.errors import WindowLostError
from rbc_agent.game.perceive import Perceiver
from rbc_agent.game.rules import RBCRules
from rbc_agent.perception.capture import FakeBackend, ScreenCapture
from rbc_agent.perception.ocr import find_tesseract
from rbc_agent.perception.window import Rect, WindowInfo

PIL = pytest.importorskip("PIL.Image")
CFG = parse_config({})
RULES = RBCRules.load()


class NoOcr:
    def read(self, img, *, mode="gray", psm=6):
        return ""


class ClosableBackend(FakeBackend):
    closed = 0

    def close(self):
        self.closed += 1


def capture(screen, window=True):
    h, w = screen.shape[:2]
    find = (lambda t: WindowInfo(1, "Roblox", w, h, True, False, Rect(0, 0, w, h))) if window else (lambda t: None)
    backend = ClosableBackend(screen)
    return ScreenCapture(CFG, backend, find), backend


def test_no_window_is_unknown_in_loops_and_an_error_once():
    cap, backend = capture(np.zeros((720, 1280, 3), np.uint8), window=False)
    with Perceiver(CFG, RULES, capture=cap, ocr=NoOcr()) as eyes:
        assert eyes.read_state().kind == "unknown"
        with pytest.raises(WindowLostError):
            eyes.read()
    assert backend.closed == 1                     # the with block closed the capture


def test_capture_closed_even_when_reading_fails():
    class Boom(NoOcr):
        def read(self, img, *, mode="gray", psm=6):
            raise RuntimeError("ocr crashed")
    img = np.asarray(PIL.open(Path(__file__).parent / "fixtures/screens/quest_choice.png").convert("RGB"))[..., ::-1]
    cap, backend = capture(np.ascontiguousarray(img))
    with pytest.raises(RuntimeError):
        with Perceiver(CFG, RULES, capture=cap, ocr=Boom()) as eyes:
            eyes.read()
    assert backend.closed == 1


@pytest.mark.skipif(find_tesseract() is None, reason="Tesseract not installed")
def test_reads_a_real_screen_end_to_end():
    img = np.asarray(PIL.open(Path(__file__).parent / "fixtures/screens/quest_choice.png").convert("RGB"))[..., ::-1]
    cap, _ = capture(np.ascontiguousarray(img))
    with Perceiver(CFG, RULES, capture=cap) as eyes:
        r = eyes.read()
    assert (r.state.kind, r.state.round_no, r.state.cogs) == ("quest_choice", 1, 10)
    assert r.raw.boxes and r.image.shape == img.shape
