"""When to buy and use a Drive. Pure logic, no screen or input.

Drives boost pollen/capacity/attack for one round; with Digital Bee active
they also corrupt the field and permanently enhance Digital Bee (wiki: Drive
Expansion). With ``only_with_digital_bee`` on (the default), the agent never
spends Cogs on a drive unless Digital Bee is in your hive.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import DRIVE_TYPES, DriveConfig
from ..game.hive import HiveProfile

FIXED_ROUNDS = frozenset({5, 10, 15, 20, 25})
POLLEN_QUESTS = frozenset({"collect_field_pollen", "collect_color_pollen", "convert_pollen"})
HIVE_COLOR_TO_DRIVE = {"red": "red", "blue": "blue", "white": "white", "mixed": "glitched"}


@dataclass(frozen=True)
class DriveDecision:
    buy: bool
    drive: str = ""
    use: bool = False
    reason: str = ""


def preferred_drives(cfg: DriveConfig, hive: HiveProfile) -> list[str]:
    first = HIVE_COLOR_TO_DRIVE.get(hive.color, "glitched") if cfg.choice == "auto" else cfg.choice
    order = [first] + [d for d in cfg.fallback_order if d != first]
    return [d for d in order if d in DRIVE_TYPES]


def plan_drive(
    cfg: DriveConfig,
    hive: HiveProfile,
    *,
    round_no: int,
    quest_type: str | None,
    cogs: int | None,
    offered: dict[str, int | None],
    used_this_run: int = 0,
) -> DriveDecision:
    """Decide whether to buy (and use) a drive this round.

    ``offered`` maps drive type -> price read from the shop (None = price not
    readable). ``cogs`` is None when the Cog counter couldn't be read.
    """
    if not cfg.enabled:
        return DriveDecision(False, reason="drives disabled")
    if cfg.only_with_digital_bee and not hive.has_digital_bee:
        return DriveDecision(False, reason="Digital Bee not in hive")
    if round_no < cfg.start_round:
        return DriveDecision(False, reason=f"waiting until round {cfg.start_round}")
    if cfg.use_on == "fixed_rounds_only" and round_no not in FIXED_ROUNDS:
        return DriveDecision(False, reason="not a fixed round")
    if cfg.use_on == "pollen_quests_only" and quest_type not in POLLEN_QUESTS:
        return DriveDecision(False, reason="quest is not a pollen quest")
    if cfg.max_per_run and used_this_run >= cfg.max_per_run:
        return DriveDecision(False, reason=f"max {cfg.max_per_run} drives per run reached")

    for drive in preferred_drives(cfg, hive):
        if drive not in offered:
            continue
        price = offered[drive]
        if price is None or cogs is None:
            # Can't verify the cost: only buy if we also can't see Cogs is short.
            if price is None and cogs is None:
                return DriveDecision(True, drive, cfg.use_right_after_buying, "price unknown, trying")
            continue
        if cogs - price >= cfg.min_cog_reserve:
            return DriveDecision(True, drive, cfg.use_right_after_buying,
                                 f"{drive} drive for {price} cogs (hive {hive.color})")
    return DriveDecision(False, reason="no wanted drive offered or affordable")
