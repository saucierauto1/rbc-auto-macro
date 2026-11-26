"""RBC Agent entry point.

Stage 1 commands:
    python main.py --check-config            validate config/config.json and print a summary
    python main.py --write-default-config    regenerate config/default_config.json
    python main.py --print-config            print the fully merged config as JSON

Without arguments (or as RBCAgent.exe) the control panel window opens.
"""

from __future__ import annotations

import argparse
import json
import sys

from rbc_agent import __version__
from rbc_agent.core.config import ConfigManager, to_dict
from rbc_agent.core.errors import ConfigError
from rbc_agent.core.logger import setup_logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Robo Bear Challenge agent")
    ap.add_argument("--check-config", action="store_true")
    ap.add_argument("--write-default-config", action="store_true")
    ap.add_argument("--print-config", action="store_true")
    args = ap.parse_args(argv)

    if not (args.check_config or args.write_default_config or args.print_config):
        from rbc_agent.ui.app import run_gui  # Tk only needed for the window
        return run_gui()

    mgr = ConfigManager()
    if args.write_default_config:
        print(f"Wrote {mgr.write_default_file()}")
        return 0

    mgr.ensure_user_file()
    try:
        cfg = mgr.load()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.print_config:
        print(json.dumps(to_dict(cfg), indent=2))
        return 0

    log = setup_logging(cfg.logging)
    log.system(f"RBC Agent {__version__}")
    log.config("Configuration OK",
               profile=cfg.strategy.profile.value,
               regions=len(cfg.regions),
               hotkeys=f"{cfg.hotkeys.pause}/{cfg.hotkeys.resume}/{cfg.hotkeys.emergency_stop}",
               debug=cfg.debug.enabled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
