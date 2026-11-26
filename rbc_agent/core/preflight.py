"""Checks shown when you press START. Says exactly what is ready and what's missing."""

from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .paths import CALIBRATION_FILE

# Flip these as later stages land. The GUI only enables real automation when
# every entry is True.
ENGINE_PARTS: dict[str, bool] = {
    "Screen capture (Stage 2)": True,
    "OCR + template matching (Stage 3)": False,
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
        if win.height and not 1.6 <= ratio <= 1.85:
            checks.append(Check("Window shape", True, f"{ratio:.2f}:1, regions are tuned for 16:9; "
                                "calibrate or use a 16:9 size", blocking=False))
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
    checks.append(Check("Calibration", CALIBRATION_FILE.exists(),
                        "done" if CALIBRATION_FILE.exists() else "not calibrated yet (Stage 3)"))
    for part, ready in ENGINE_PARTS.items():
        checks.append(Check(part, ready, "ready" if ready else "not built yet"))
    return checks


def can_start(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.blocking)
