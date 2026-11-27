"""Checks shown when you press START. Says exactly what is ready and what's missing."""

from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig

# Flip these as later stages land. The GUI only enables real automation when
# every entry is True.
ENGINE_PARTS: dict[str, bool] = {
    "Screen capture (Stage 2)": True,
    "Screen reading with OCR (Stage 3)": True,
    "State machine (Stage 5)": False,
    "Keyboard/mouse input (Stage 6)": False,
    "Movement + field routes (Stage 7)": False,
    "RBC screen detection (Stage 8)": False,
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def run_preflight(cfg: AppConfig, find_window=None) -> list[Check]:
    from ..game.hive import HiveProfile
    if find_window is None:
        from ..perception.window import find_window as _fw
        find_window = lambda t: _fw(t, cfg.window.process_name)  # noqa: E731
    checks: list[Check] = [Check("Config", True, "config.json is valid")]

    win = find_window(cfg.window.title_contains)
    if win is None:
        checks.append(Check("Roblox window", False, "Roblox isn't open (or not found by title)"))
    else:
        checks.append(Check("Roblox window", not win.minimized,
                            f"{win.width}x{win.height}" + (" (minimised!)" if win.minimized else "")))
        ratio = win.width / win.height if win.height else 0
        if win.height and not 1.6 <= ratio <= 1.95:
            checks.append(Check("Window shape", True, f"{ratio:.2f}:1, regions are tuned for 16:9; "
                                "other shapes are untested, a 16:9 size is safest", blocking=False))
        if win.height and win.height < 600:
            checks.append(Check("Window size", True, "small window: text may be too small to read",
                                blocking=False))
    try:
        from ..perception.capture import backend_available
        cap_ok = backend_available()
    except ImportError:
        cap_ok = False
    if not cap_ok:
        checks.append(Check("Capture library", False, "mss/numpy missing: pip install -r requirements.txt"))
    hk = cfg.hotbar
    used = [s for s in hk.slots if s.use_when != "never"]
    if hk.enabled and used:
        checks.append(Check("Hotbar", True, f"{len(used)} slot(s) set to auto-use", blocking=False))

    hive = HiveProfile.from_config(cfg.hive)
    checks.append(Check("Hive entered", hive.total > 0,
                        f"{hive.total} bees, {hive.color} hive" if hive.total
                        else "add your bees on the Hive tab", blocking=False))
    if cfg.drives.enabled:
        detail = ("Digital Bee in hive: drives will be bought" if hive.has_digital_bee
                  else "no Digital Bee: drives will be skipped")
        checks.append(Check("Drives", True, detail, blocking=False))
    from ..perception.ocr import find_tesseract
    tess = find_tesseract(cfg.ocr.tesseract_path)
    checks.append(Check("Tesseract OCR", tess is not None,
                        str(tess) if tess else "not found: it comes inside the RBCAgent.zip release "
                        "(tesseract folder next to the exe), or install it from "
                        "https://github.com/UB-Mannheim/tesseract/wiki"))
    for part, ready in ENGINE_PARTS.items():
        checks.append(Check(part, ready, "ready" if ready else "not built yet"))
    return checks


def can_start(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.blocking)
