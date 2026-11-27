"""Stage 3: read text from screen crops with Tesseract.

Tesseract runs as a separate program (``tesseract.exe``). We send it a PNG on
stdin and read plain text from stdout, so no Python wrapper is needed.
Where it is looked for, first match wins:

1. ``ocr.tesseract_path`` in the config,
2. ``tesseract/tesseract.exe`` next to RBCAgent.exe (the release zip bundles it),
3. ``tesseract`` on PATH,
4. the default installer folders under Program Files.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

from ..core.config import OcrConfig
from ..core.errors import ConfigError
from ..core.paths import PROJECT_ROOT

_INSTALL_DIRS = (r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR")


def find_tesseract(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    bundled = PROJECT_ROOT / "tesseract" / "tesseract.exe"
    if bundled.is_file():
        return bundled
    on_path = shutil.which("tesseract")
    if on_path:
        return Path(on_path)
    for d in _INSTALL_DIRS:
        p = Path(d) / "tesseract.exe"
        if p.is_file():
            return p
    return None


def prepare(img: np.ndarray, cfg: OcrConfig, mode: str = "gray") -> "Image.Image":  # noqa: F821
    """BGR crop -> upscaled greyscale image Tesseract reads well.

    ``mode="yellow"`` keeps only yellow pixels (the outlined reward text),
    drawn black on white, because the thick outline confuses Tesseract.
    """
    from PIL import Image, ImageOps

    if mode == "yellow":
        b, g, r = (img[..., i].astype(int) for i in range(3))
        mask = (r >= cfg.yellow_min_rg) & (g >= cfg.yellow_min_rg) & (b <= cfg.yellow_max_b)
        im = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8))
    else:
        im = ImageOps.grayscale(Image.fromarray(np.ascontiguousarray(img[..., ::-1])))
    im = im.resize((im.width * cfg.upscale, im.height * cfg.upscale), Image.LANCZOS)
    return ImageOps.autocontrast(im)


class Tesseract:
    def __init__(self, cfg: OcrConfig):
        self.cfg = cfg
        exe = find_tesseract(cfg.tesseract_path)
        if exe is None:
            raise ConfigError("Tesseract OCR not found. Install it from "
                              "https://github.com/UB-Mannheim/tesseract/wiki or set ocr.tesseract_path")
        self.exe = exe

    def read(self, img: np.ndarray, *, mode: str = "gray", psm: int = 6) -> str:
        """Text in a BGR crop. Empty lines are dropped; lines are joined with newlines."""
        if img.size == 0:
            return ""
        buf = io.BytesIO()
        prepare(img, self.cfg, mode).save(buf, format="PNG")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0   # no console flash in the exe
        out = subprocess.run([str(self.exe), "stdin", "stdout", "--psm", str(psm), "-l", "eng"],
                             input=buf.getvalue(), capture_output=True,
                             timeout=self.cfg.timeout_s, creationflags=flags)
        text = out.stdout.decode("utf-8", errors="replace")
        return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
