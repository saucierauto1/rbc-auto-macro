"""Dodging and mob tactics. Pure logic based on the BSS wiki strategies:

* Mechsquito / Mega Mechsquito aim at where you ARE -> keep moving (circle/zigzag).
* Cogturret fires straight ahead after rising -> step sideways out of its line.
* Cogmower drives straight and doesn't aim -> step out of its path.
* Bees attack mobs near you -> move close to mobs you want dead (turrets first).

Positions are relative to the player in field units (x right, y forward);
perception (Stages 8/10) fills them in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core.config import CombatConfig

SHOOTERS = {"mechsquito", "mega_mechsquito"}


@dataclass(frozen=True)
class Threat:
    kind: str                 # mechsquito | mega_mechsquito | cogmower | golden_cogmower | cogturret
    x: float
    y: float
    heading: tuple[float, float] | None = None   # unit vector it moves/fires along
    raised: bool = True       # cogturret above ground (about to fire)

    @property
    def dist(self) -> float:
        return math.hypot(self.x, self.y)


@dataclass(frozen=True)
class MoveHint:
    action: str               # none | circle | zigzag | sidestep | approach | leave_field
    direction: tuple[float, float] = (0.0, 0.0)
    duration_s: float = 0.0
    jump: bool = False
    reason: str = ""


def _in_line(t: Threat, width: float) -> bool:
    """Is the player (origin) in front of ``t`` along its heading, within ``width``?"""
    if not t.heading:
        return False
    hx, hy = t.heading
    px, py = -t.x, -t.y                  # vector from threat to player
    along = px * hx + py * hy
    across = abs(px * hy - py * hx)
    return along > 0 and across < width


def _perp_away(t: Threat) -> tuple[float, float]:
    """Unit vector perpendicular to the threat's heading, on the player's side."""
    hx, hy = t.heading or (0.0, 1.0)
    a, b = (-hy, hx), (hy, -hx)
    px, py = -t.x, -t.y
    return a if px * a[0] + py * a[1] >= 0 else b


def plan_dodge(threats: list[Threat], cfg: CombatConfig, *, toxin_stacks: int = 0) -> MoveHint:
    """Most urgent movement for this moment. The farming pattern resumes on ``none``."""
    line_width = cfg.danger_line_width
    if cfg.leave_field_at_toxin_stacks and toxin_stacks >= cfg.leave_field_at_toxin_stacks:
        return MoveHint("leave_field", reason=f"{toxin_stacks} toxin stacks")
    if not cfg.dodge_enabled:
        return MoveHint("none", reason="dodging off")

    # 1) Get out of a raised turret's firing line (biggest single hit).
    if cfg.sidestep_cogturrets:
        for t in sorted((t for t in threats if t.kind == "cogturret" and t.raised), key=lambda t: t.dist):
            if _in_line(t, line_width):
                return MoveHint("sidestep", _perp_away(t), cfg.sidestep_s, cfg.jump_while_dodging,
                                "cogturret aiming at you")
    # 2) Step out of a cogmower's path.
    if cfg.avoid_cogmowers:
        for t in sorted((t for t in threats if t.kind in ("cogmower", "golden_cogmower")), key=lambda t: t.dist):
            if t.dist < cfg.cogmower_alert_range and _in_line(t, line_width):
                return MoveHint("sidestep", _perp_away(t), cfg.sidestep_s, False, f"{t.kind} heading at you")
    # 3) Shooters: keep moving so shots land where you were.
    shooters = [t for t in threats if t.kind in SHOOTERS]
    if shooters and cfg.mechsquito_dodge != "none" and toxin_stacks >= cfg.dodge_at_toxin_stacks:
        mega = any(t.kind == "mega_mechsquito" for t in shooters)
        return MoveHint(cfg.mechsquito_dodge, (0.0, 0.0), 2.0 * cfg.dodge_radius,
                        cfg.jump_while_dodging, ("mega " if mega else "") + "mechsquito shooting")
    # 4) Walk toward a target so bees lock on (turrets first).
    if cfg.stand_near_mobs:
        def prio(t: Threat) -> tuple:
            first = cfg.prioritize_cogturrets and t.kind == "cogturret"
            return (not first, t.kind != "golden_cogmower", t.dist)
        targets = sorted((t for t in threats if t.kind != "cogturret" or t.raised), key=prio)
        if targets and cfg.approach_min_dist < targets[0].dist <= cfg.approach_range:
            t = targets[0]
            return MoveHint("approach", (t.x / t.dist, t.y / t.dist), 0.5, False, f"bees target {t.kind}")
    return MoveHint("none", reason="no threat")


def golden_cogmower_fields(round_no: int, found_in_windows: set[int], rules_mobs: dict,
                           cfg: CombatConfig) -> list[str]:
    """Fields worth checking for a Golden Cogmower this round ([] if not worth it).

    ``found_in_windows`` holds the indexes of spawn windows where one was
    already found/killed, so we stop searching for that window.
    """
    if not cfg.hunt_golden_cogmower:
        return []
    g = rules_mobs.get("Golden Cogmower", {})
    for i, window in enumerate(g.get("spawn_rounds", [])):
        if round_no in window and i not in found_in_windows:
            return list(g.get("fields", []))
    return []
