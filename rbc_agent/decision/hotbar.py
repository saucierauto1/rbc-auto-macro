"""Which hotbar slots to press right now. Pure logic: the caller passes the
situation in, gets slot numbers out, and the input layer (Stage 6) presses them."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.config import DriveConfig, HotbarConfig, HotbarSlot
from ..game.hive import HiveProfile
from .drives import FIXED_ROUNDS


@dataclass(frozen=True)
class HotbarContext:
    now: float
    in_round: bool               # a quest timer is running
    round_no: int
    phase: str                   # round_start | in_field | travelling | at_hive | menu
    backpack: float | None = None    # 0..1, None = unknown
    time_left_s: float | None = None
    mobs_visible: int = 0


@dataclass
class HotbarState:
    """Remembers when each slot was used. Reset ``per_round`` at each new round."""

    last_used: dict[int, float] = field(default_factory=dict)
    per_round: dict[int, int] = field(default_factory=dict)
    last_press: float = -1e9
    round_no: int = 0

    def new_round(self, round_no: int) -> None:
        self.round_no = round_no
        self.per_round.clear()

    def record(self, slot: int, now: float) -> None:
        self.last_used[slot] = now
        self.per_round[slot] = self.per_round.get(slot, 0) + 1
        self.last_press = now


def _is_drive(item: str) -> bool:
    return "drive" in item.lower()


def slot_ready(s: HotbarSlot, ctx: HotbarContext, st: HotbarState, cfg: HotbarConfig,
               drives: DriveConfig, hive: HiveProfile) -> tuple[bool, str]:
    if s.use_when == "never" or not s.item:
        return False, "never"
    if s.only_during_rounds and not ctx.in_round:
        return False, "not in a round"
    if ctx.round_no < s.from_round:
        return False, f"from round {s.from_round}"
    if s.max_per_round and st.per_round.get(s.slot, 0) >= s.max_per_round:
        return False, "max uses this round"
    if ctx.now - st.last_used.get(s.slot, -1e9) < s.every_s:
        return False, "cooling down"
    if _is_drive(s.item) and drives.only_with_digital_bee and not hive.has_digital_bee:
        return False, "drive needs Digital Bee in hive"
    w = s.use_when
    if w == "always":
        ok = True
    elif w == "round_start":
        ok = ctx.phase == "round_start"
    elif w == "in_field":
        ok = ctx.phase == "in_field"
    elif w == "at_hive":
        ok = ctx.phase == "at_hive"
    elif w == "backpack_full":
        ok = ctx.backpack is not None and ctx.backpack >= cfg.backpack_full_at
    elif w == "mobs_nearby":
        ok = ctx.mobs_visible >= cfg.mobs_nearby_count
    elif w == "fixed_rounds":
        ok = ctx.round_no in FIXED_ROUNDS and ctx.phase in ("round_start", "in_field")
    elif w == "low_time":
        ok = ctx.time_left_s is not None and ctx.time_left_s <= s.low_time_s
    else:
        ok = False
    return ok, w if ok else f"waiting for {w}"


def slots_to_use(cfg: HotbarConfig, ctx: HotbarContext, st: HotbarState,
                 drives: DriveConfig, hive: HiveProfile) -> list[int]:
    """Slots due now, at most one per call (respecting min_gap_s between presses),
    lowest slot number first."""
    if not cfg.enabled or ctx.now - st.last_press < cfg.min_gap_s:
        return []
    if ctx.round_no != st.round_no:
        st.new_round(ctx.round_no)
    for s in sorted(cfg.slots, key=lambda s: s.slot):
        ok, _ = slot_ready(s, ctx, st, cfg, drives, hive)
        if ok:
            return [s.slot]
    return []
