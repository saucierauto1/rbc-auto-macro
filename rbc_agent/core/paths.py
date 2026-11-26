"""Central place for project paths, so no module hardcodes its own."""

from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # Running as RBCAgent.exe (PyInstaller): config/, data/, logs/ sit next to the exe.
    PROJECT_ROOT: Path = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR: Path = PROJECT_ROOT / "config"
DEFAULT_CONFIG_FILE: Path = CONFIG_DIR / "default_config.json"
USER_CONFIG_FILE: Path = CONFIG_DIR / "config.json"

DATA_DIR: Path = PROJECT_ROOT / "data"
RULES_FILE: Path = DATA_DIR / "rbc_rules.json"
CALIBRATION_FILE: Path = DATA_DIR / "calibration.json"
TEMPLATES_DIR: Path = DATA_DIR / "templates"

LOGS_DIR: Path = PROJECT_ROOT / "logs"
DEBUG_DIR: Path = PROJECT_ROOT / "debug"


def resolve(path: str | Path) -> Path:
    """Resolve a config-supplied path relative to the project root."""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p
