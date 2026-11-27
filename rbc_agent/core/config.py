"""Typed, validated, hot-reloadable configuration.

Design
------
* **Single source of truth.** Defaults are defined *once*, in the dataclasses
  below. ``config/default_config.json`` is *generated* from them
  (``python main.py --write-default-config``), so code and JSON can't drift.
* **Partial overrides.** ``config/config.json`` only has to contain the
  values the user wants to change. It is deep-merged on top of the defaults.
* **Strict parsing.** Unknown keys are errors (with a "did you mean" hint),
  so a typo like ``"poln"`` is caught instead of silently ignored. Types are
  checked, and every section validates its own value ranges.
* **Immutable snapshots.** ``AppConfig`` is frozen. ``ConfigManager.reload()``
  swaps in a whole new snapshot atomically. Code that grabbed the old snapshot
  keeps a consistent view until its next read.
* **Reload is safe.** If the new file is invalid, the old config stays active
  and a ``ConfigError`` listing *every* problem is raised.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import threading
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

from .errors import ConfigError
from .paths import DEFAULT_CONFIG_FILE, USER_CONFIG_FILE
from .states import UNTIMED_STATES, AgentState, StrategyProfile

# --------------------------------------------------------------------------- #
# Small value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Region:
    """A rectangle in *normalised* client-area coordinates (0..1).

    Normalised coordinates mean the same config works at any resolution:
    Stage 2 maps them to pixels using the live Roblox client rect.
    """

    x: float
    y: float
    w: float
    h: float

    def validate(self) -> list[str]:
        p: list[str] = []
        for name in ("x", "y", "w", "h"):
            v = getattr(self, name)
            if not 0.0 <= v <= 1.0:
                p.append(f"{name}={v} must be between 0 and 1")
        if self.w <= 0 or self.h <= 0:
            p.append("w and h must be > 0")
        if self.x + self.w > 1.0 + 1e-9 or self.y + self.h > 1.0 + 1e-9:
            p.append("region extends outside the window")
        return p


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WindowConfig:
    title_contains: str = "Roblox"
    process_name: str = "RobloxPlayerBeta.exe"
    # Resolution that pixel-sized values (template images) were captured at.
    # Templates are rescaled by actual_height / reference_height.
    reference_width: int = 1920
    reference_height: int = 1080
    dpi_aware: bool = True
    require_focus: bool = True
    # If the window loses focus: try to re-focus it (True) or pause (False).
    refocus_automatically: bool = False

    def validate(self) -> list[str]:
        p = []
        if not self.title_contains.strip():
            p.append("title_contains must not be empty")
        if self.reference_width < 640 or self.reference_height < 480:
            p.append("reference resolution must be at least 640x480")
        return p


def _default_regions() -> dict[str, Region]:
    # Rough guesses for a 16:9 Roblox HUD, used by the Screenshot button's overlay.
    # The RBC panels are found by their green title bars instead (data/rbc_screens.json).
    # pollen_text was measured on a real 1920x1080 screenshot.
    return {
        "pollen_bar": Region(0.36, 0.005, 0.28, 0.035),
        "pollen_text": Region(0.53, 0.025, 0.12, 0.026),
        "buff_bar": Region(0.0, 0.045, 0.60, 0.06),
        "round_banner": Region(0.30, 0.10, 0.40, 0.08),
        "round_timer": Region(0.42, 0.18, 0.16, 0.05),
        "objective_text": Region(0.70, 0.25, 0.30, 0.30),
        "center_dialog": Region(0.20, 0.20, 0.60, 0.60),
        "shop_panel": Region(0.15, 0.10, 0.70, 0.80),
        "scene": Region(0.10, 0.25, 0.80, 0.60),
    }


@dataclass(frozen=True)
class DetectionConfig:
    template_threshold: float = 0.82
    ocr_min_confidence: float = 0.60
    # Combined evidence score needed before a state transition is accepted.
    transition_threshold: float = 0.75
    # Evidence must hold for this many consecutive perception frames.
    confirm_frames: int = 3
    # Below this overall confidence the game state counts as UNKNOWN.
    unknown_state_threshold: float = 0.40
    # Seconds of unknown state before recovery is triggered.
    unknown_state_grace_s: float = 4.0
    confidence_weights: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "rbc_end": {
                "round_ui_gone": 0.35,
                "completion_text": 0.25,
                "robo_bear_ui": 0.20,
                "layout_match": 0.20,
            },
            "round_start": {
                "round_banner": 0.40,
                "round_number_changed": 0.35,
                "timer_reset": 0.25,
            },
            "shop_open": {
                "shop_template": 0.60,
                "cog_counter": 0.25,
                "layout_match": 0.15,
            },
        }
    )

    def validate(self) -> list[str]:
        p = []
        for n in ("template_threshold", "ocr_min_confidence",
                  "transition_threshold", "unknown_state_threshold"):
            v = getattr(self, n)
            if not 0.0 < v <= 1.0:
                p.append(f"{n}={v} must be in (0, 1]")
        if self.confirm_frames < 1:
            p.append("confirm_frames must be >= 1")
        if self.unknown_state_grace_s < 0:
            p.append("unknown_state_grace_s must be >= 0")
        for group, weights in self.confidence_weights.items():
            if any(w < 0 for w in weights.values()):
                p.append(f"confidence_weights.{group}: weights must be >= 0")
            total = sum(weights.values())
            if abs(total - 1.0) > 0.01:
                p.append(f"confidence_weights.{group}: weights sum to {total:.3f}, expected 1.0")
        return p


@dataclass(frozen=True)
class ScanRatesConfig:
    """How often (Hz) each detector runs. Expensive detectors run slower."""

    character_motion_hz: float = 15.0
    tokens_hz: float = 8.0
    pollen_hz: float = 3.0
    ui_hz: float = 3.0
    ocr_hz: float = 1.5
    buffs_hz: float = 1.0
    window_check_hz: float = 2.0
    # Hive analysis is event-driven (only when needed) and has no rate here.

    def validate(self) -> list[str]:
        return [f"{f.name}={getattr(self, f.name)} must be in (0, 60]"
                for f in dataclasses.fields(self)
                if not 0 < getattr(self, f.name) <= 60]


@dataclass(frozen=True)
class PollenConfig:
    """Base capacity thresholds. Stage 12 adjusts them by time and distance."""

    continue_below: float = 0.70
    return_if_objective_below: float = 0.90
    consider_return_above: float = 0.90
    must_return_above: float = 0.98
    # With less than this many seconds left, returning is usually wasted
    # time unless conversion itself counts toward the objective.
    # (RBC rounds are 300 s; finishing under 3:45 pays the Round Speed Bonus.)
    low_time_s: float = 20.0
    travel_safety_margin_s: float = 3.0
    convert_timeout_s: float = 90.0

    def validate(self) -> list[str]:
        p = []
        order = [self.continue_below, self.consider_return_above, self.must_return_above]
        if not all(0 < v <= 1 for v in order + [self.return_if_objective_below]):
            p.append("thresholds must be fractions in (0, 1]")
        if not order == sorted(order):
            p.append("continue_below <= consider_return_above <= must_return_above is required")
        if self.low_time_s < 0 or self.travel_safety_margin_s < 0:
            p.append("time values must be >= 0")
        if self.convert_timeout_s <= 0:
            p.append("convert_timeout_s must be > 0")
        return p


@dataclass(frozen=True)
class MovementConfig:
    # Stuck = scene motion below threshold for stuck_window_s while keys are held.
    stuck_window_s: float = 2.0
    stuck_motion_threshold: float = 0.02
    max_corrective_attempts: int = 3
    recenter_interval_s: float = 30.0
    farm_pattern: str = "snake"
    pattern_scale: float = 1.0
    # Characters move at different speeds depending on hive/buffs. Routes
    # are timed for this speed and rescaled at runtime from measured speed.
    route_reference_speed: float = 1.0

    def validate(self) -> list[str]:
        p = []
        if self.stuck_window_s <= 0:
            p.append("stuck_window_s must be > 0")
        if not 0 < self.stuck_motion_threshold < 1:
            p.append("stuck_motion_threshold must be in (0, 1)")
        if self.max_corrective_attempts < 0:
            p.append("max_corrective_attempts must be >= 0")
        if self.farm_pattern not in {"snake", "square", "circle", "stationary"}:
            p.append(f"farm_pattern '{self.farm_pattern}' unknown (snake/square/circle/stationary)")
        if not 0.1 <= self.pattern_scale <= 3.0:
            p.append("pattern_scale must be in [0.1, 3.0]")
        return p


@dataclass(frozen=True)
class InputConfig:
    key_bindings: dict[str, str] = field(default_factory=lambda: {
        "forward": "w", "back": "s", "left": "a", "right": "d",
        "jump": "space", "interact": "e",
        "camera_left": ",", "camera_right": ".",
        "zoom_in": "i", "zoom_out": "o",
    })
    key_tap_ms: int = 50
    click_hold_ms: int = 40
    mouse_move_duration_ms: int = 80
    # Small random variation so we don't hit the exact frame boundary of a
    # UI animation every time (robustness, not detection evasion).
    timing_jitter_ms: int = 15

    REQUIRED_ACTIONS = ("forward", "back", "left", "right", "jump", "interact")

    def validate(self) -> list[str]:
        p = [f"key_bindings missing '{a}'" for a in self.REQUIRED_ACTIONS
             if a not in self.key_bindings]
        if self.key_tap_ms < 10 or self.click_hold_ms < 10:
            p.append("key_tap_ms and click_hold_ms must be >= 10")
        if not 0 <= self.timing_jitter_ms <= 100:
            p.append("timing_jitter_ms must be in [0, 100]")
        return p


@dataclass(frozen=True)
class TokenConfig:
    # Base value per token type. Names must match template names (Stage 10).
    # Unknown/unclassified tokens use "generic".
    values: dict[str, float] = field(default_factory=lambda: {
        "generic": 10.0,
        "boost": 40.0,
        "haste": 30.0,
        "focus": 35.0,
        "inspire": 35.0,
        "bomb": 25.0,
        "link": 20.0,
    })
    min_score: float = 15.0
    # Never spend more than this many seconds walking to one token.
    max_chase_s: float = 4.0

    def validate(self) -> list[str]:
        p = []
        if "generic" not in self.values:
            p.append("values must include 'generic' (fallback for unclassified tokens)")
        if any(v < 0 for v in self.values.values()):
            p.append("token values must be >= 0")
        if self.max_chase_s <= 0:
            p.append("max_chase_s must be > 0")
        return p


@dataclass(frozen=True)
class FieldWeights:
    hive_synergy: float = 1.0
    objective_synergy: float = 2.0
    token_value: float = 0.5
    buff_value: float = 0.5
    pollen_value: float = 1.0
    travel_cost: float = 1.0
    risk: float = 1.0
    capacity_penalty: float = 1.0


@dataclass(frozen=True)
class FieldConfig:
    weights: FieldWeights = field(default_factory=FieldWeights)
    # Optional per-field bias added to the score, e.g. {"Pine Tree Forest": 5}.
    bias: dict[str, float] = field(default_factory=dict)
    # Fields the agent must never pick (e.g. too dangerous for your hive).
    blacklist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StrategyWeights:
    """One strategy profile = a set of weights for the upgrade/field scorers."""

    base_value: float = 1.0
    hive_synergy: float = 1.0
    objective_synergy: float = 1.0
    buff_synergy: float = 1.0
    scaling_value: float = 1.0
    opportunity_cost: float = 1.0
    color_bias: dict[str, float] = field(default_factory=lambda: {"red": 1.0, "blue": 1.0, "white": 1.0})
    # Multipliers for upgrade tags from rbc_rules.json.
    tag_bias: dict[str, float] = field(default_factory=lambda: {
        "token": 1.0, "pollen": 1.0, "capacity": 1.0, "conversion": 1.0,
        "attack": 1.0, "speed": 1.0, "cogs": 1.0,
    })


def _profile(**over: Any) -> StrategyWeights:
    base = StrategyWeights()
    color = dict(base.color_bias, **over.pop("color_bias", {}))
    tags = dict(base.tag_bias, **over.pop("tag_bias", {}))
    return dataclasses.replace(base, color_bias=color, tag_bias=tags, **over)


def _default_profiles() -> dict[str, StrategyWeights]:
    return {
        "BALANCED": _profile(),
        "RED": _profile(color_bias={"red": 1.6, "blue": 0.6}),
        "BLUE": _profile(color_bias={"blue": 1.6, "red": 0.6}),
        "WHITE": _profile(color_bias={"white": 1.6, "red": 0.8, "blue": 0.8}),
        "TOKEN_FOCUSED": _profile(tag_bias={"token": 1.8}),
        "POLLEN_FOCUSED": _profile(tag_bias={"pollen": 1.8}),
        "CONVERSION_FOCUSED": _profile(tag_bias={"conversion": 1.8}),
        "ATTACK_FOCUSED": _profile(tag_bias={"attack": 1.8}),
        "SPEED_FOCUSED": _profile(tag_bias={"speed": 1.8}),
        "CUSTOM": _profile(),
    }


@dataclass(frozen=True)
class StrategyConfig:
    profile: StrategyProfile = StrategyProfile.BALANCED
    # Pick the profile automatically from the scanned hive colour (Stage 9/11).
    auto_profile_from_hive: bool = True
    profiles: dict[str, StrategyWeights] = field(default_factory=_default_profiles)

    def validate(self) -> list[str]:
        p = []
        missing = [s.value for s in StrategyProfile if s.value not in self.profiles]
        if missing:
            p.append(f"profiles missing: {', '.join(missing)}")
        unknown = [k for k in self.profiles if k not in StrategyProfile.__members__]
        if unknown:
            p.append(f"unknown profile names: {', '.join(unknown)}")
        for name, w in self.profiles.items():
            if any(v < 0 for v in list(w.color_bias.values()) + list(w.tag_bias.values())):
                p.append(f"profiles.{name}: biases must be >= 0")
        return p

    def active_weights(self) -> StrategyWeights:
        return self.profiles[self.profile.value]


@dataclass(frozen=True)
class BeeEntry:
    """One kind of bee in your hive (entered on the Hive tab)."""

    bee: str
    count: int = 1
    gifted: int = 0          # how many of `count` are gifted
    level: int = 1           # typical level of these bees

    def validate(self) -> list[str]:
        p = []
        if not self.bee.strip():
            p.append("bee name must not be empty")
        if self.count < 1 or self.count > 50:
            p.append(f"{self.bee}: count must be 1..50")
        if not 0 <= self.gifted <= self.count:
            p.append(f"{self.bee}: gifted must be between 0 and count")
        if not 1 <= self.level <= 25:
            p.append(f"{self.bee}: level must be 1..25")
        return p


@dataclass(frozen=True)
class HiveConfig:
    profile_file: str = "data/hive_profile.json"
    auto_scan: bool = True
    # "" = detect automatically; otherwise force red/blue/white/mixed.
    color_override: str = ""
    # Your hive, entered by hand on the Hive tab. Used for colour detection,
    # bee-pick scoring and the Digital Bee drive rule.
    bees: list[BeeEntry] = field(default_factory=list)

    def validate(self) -> list[str]:
        p = []
        if self.color_override not in {"", "red", "blue", "white", "mixed"}:
            p.append(f"color_override '{self.color_override}' must be '', red, blue, white or mixed")
        names = [b.bee.lower() for b in self.bees]
        if len(names) != len(set(names)):
            p.append("bees: the same bee is listed twice (use count instead)")
        if sum(b.count for b in self.bees) > 50:
            p.append("bees: a hive holds at most 50 bees")
        return p

    def has_bee(self, name: str) -> bool:
        n = name.strip().lower().removesuffix(" bee")
        return any(b.bee.strip().lower().removesuffix(" bee") == n for b in self.bees)


@dataclass(frozen=True)
class BeePickConfig:
    """How to choose bees in the RBC bee-pick screen."""

    # Bees to take first, best first (names from data/bees.json).
    priority: list[str] = field(default_factory=lambda: [
        "Digital", "Vicious", "Tabby", "Precise", "Spicy", "Buoyant", "Fuzzy",
        "Tadpole", "Vector", "Photon", "Windy", "Festive", "Ninja", "Diamond", "Lion",
    ])
    # Bees never to pick unless nothing else is offered.
    avoid: list[str] = field(default_factory=list)
    prefer_gifted: bool = True
    # Give extra score to bees matching your hive colour.
    prefer_hive_color: bool = True
    # Use a (paid) reroll if none of the offered bees is on the priority list.
    reroll_if_no_priority: bool = False
    max_reroll_cogs: int = 0
    # Scoring weights (score = priority + rarity + colour + gifted).
    priority_base: float = 100.0
    priority_step: float = 3.0          # each place down the list loses this much
    rarity_weight: float = 2.0          # x rarity rank (common 0 .. mythic/event 4)
    color_match_bonus: float = 6.0
    gifted_bonus: float = 8.0

    def validate(self) -> list[str]:
        p = []
        overlap = {b.lower() for b in self.priority} & {b.lower() for b in self.avoid}
        if overlap:
            p.append(f"bees on both priority and avoid lists: {', '.join(sorted(overlap))}")
        if self.max_reroll_cogs < 0:
            p.append("max_reroll_cogs must be >= 0")
        return p


QUEST_TYPES = ("collect_field_pollen", "collect_color_pollen", "collect_pollen", "convert_pollen",
               "defeat_mobs")


@dataclass(frozen=True)
class QuestConfig:
    """How to choose between the two quest cards each round."""

    # Best first. Ids match objective_patterns in data/rbc_rules.json.
    priority: list[str] = field(default_factory=lambda: [
        "collect_color_pollen", "collect_pollen", "collect_field_pollen", "defeat_mobs", "convert_pollen"])
    # Prefer the harder card (more Cogs) when both are the same type.
    prefer_harder_for_cogs: bool = True
    # Spend the one free reroll per run when both cards are of these types.
    free_reroll_if_both: list[str] = field(default_factory=lambda: ["convert_pollen"])
    # Try to finish under 3:45 for the Round Speed Bonus (more aggressive returns).
    chase_fast_bonus: bool = True

    def validate(self) -> list[str]:
        bad = [q for q in self.priority + self.free_reroll_if_both if q not in QUEST_TYPES]
        return [f"unknown quest type(s): {', '.join(bad)} (use {', '.join(QUEST_TYPES)})"] if bad else []


@dataclass(frozen=True)
class UpgradeConfig:
    """Shop behaviour between rounds."""

    # Upgrade ids (from data/rbc_rules.json) to always buy first, best first.
    priority: list[str] = field(default_factory=list)
    # Upgrade ids never to buy.
    blacklist: list[str] = field(default_factory=list)
    auto_buy: bool = True
    lock_unaffordable_priority: bool = True
    # Always keep at least this many Cogs after a purchase.
    min_cog_reserve: int = 0
    max_rerolls_per_round: int = 0
    # Extra multiplier per rarity on the upgrade score.
    rarity_bias: dict[str, float] = field(default_factory=lambda: {
        "common": 1.0, "rare": 1.0, "epic": 1.1, "legendary": 1.25})
    # Scoring weights.
    base_score: float = 10.0
    hive_color_mult: float = 1.3        # upgrade matches your hive colour
    off_color_mult: float = 0.5         # red upgrade in a blue hive or vice versa
    priority_bonus: float = 1000.0
    priority_step: float = 10.0

    def validate(self) -> list[str]:
        p = []
        if self.min_cog_reserve < 0 or self.max_rerolls_per_round < 0:
            p.append("min_cog_reserve and max_rerolls_per_round must be >= 0")
        if set(self.priority) & set(self.blacklist):
            p.append("an upgrade can't be on both priority and blacklist")
        if any(v < 0 for v in self.rarity_bias.values()):
            p.append("rarity_bias values must be >= 0")
        return p


DRIVE_TYPES = ("red", "blue", "white", "glitched")


@dataclass(frozen=True)
class DriveConfig:
    """Auto-buy and use Drives. Drives only do anything with Digital Bee,
    so by default nothing is bought unless Digital Bee is in your hive."""

    enabled: bool = True
    only_with_digital_bee: bool = True
    # "auto" = pick the drive matching your hive colour (glitched for mixed),
    # otherwise one of red/blue/white/glitched.
    choice: str = "auto"
    # Fallback order if the chosen drive isn't offered / affordable.
    fallback_order: list[str] = field(default_factory=lambda: ["glitched", "red", "blue", "white"])
    # every_round | fixed_rounds_only (5/10/15/20/25) | pollen_quests_only
    use_on: str = "every_round"
    start_round: int = 1
    max_per_run: int = 0                # 0 = no limit
    min_cog_reserve: int = 0
    use_right_after_buying: bool = True
    # Hotbar slot (1-7) the drive sits in; 0 = use it from the inventory.
    hotbar_slot: int = 0

    def validate(self) -> list[str]:
        p = []
        if self.choice not in ("auto",) + DRIVE_TYPES:
            p.append(f"choice '{self.choice}' must be auto, {', '.join(DRIVE_TYPES)}")
        bad = [d for d in self.fallback_order if d not in DRIVE_TYPES]
        if bad:
            p.append(f"fallback_order: unknown drive(s) {', '.join(bad)}")
        if self.use_on not in ("every_round", "fixed_rounds_only", "pollen_quests_only"):
            p.append("use_on must be every_round, fixed_rounds_only or pollen_quests_only")
        if not 1 <= self.start_round <= 25:
            p.append("start_round must be 1..25")
        if self.max_per_run < 0 or self.min_cog_reserve < 0:
            p.append("max_per_run and min_cog_reserve must be >= 0")
        if not 0 <= self.hotbar_slot <= 7:
            p.append("hotbar_slot must be 0..7")
        return p


HOTBAR_WHEN = ("never", "always", "round_start", "in_field", "at_hive", "backpack_full",
               "mobs_nearby", "fixed_rounds", "low_time")


@dataclass(frozen=True)
class HotbarSlot:
    """One hotbar slot (keys 1-7) and when to use what's in it."""

    slot: int
    item: str = ""                    # name from data/hotbar_items.json (or your own)
    use_when: str = "never"
    every_s: float = 60.0             # min seconds between uses (item cooldown / duration)
    max_per_round: int = 0            # 0 = no limit
    from_round: int = 1
    low_time_s: float = 60.0          # for use_when=low_time: use when less than this is left
    only_during_rounds: bool = True   # never use it between rounds / outside RBC

    def validate(self) -> list[str]:
        p = []
        if not 1 <= self.slot <= 7:
            p.append(f"slot {self.slot}: must be 1..7")
        if self.use_when not in HOTBAR_WHEN:
            p.append(f"slot {self.slot}: use_when '{self.use_when}' must be one of {', '.join(HOTBAR_WHEN)}")
        if self.use_when != "never" and not self.item.strip():
            p.append(f"slot {self.slot}: pick an item (or set use_when to never)")
        if self.every_s < 0.5:
            p.append(f"slot {self.slot}: every_s must be >= 0.5")
        if self.max_per_round < 0 or not 1 <= self.from_round <= 25 or self.low_time_s < 0:
            p.append(f"slot {self.slot}: max_per_round >= 0, from_round 1..25, low_time_s >= 0")
        return p


@dataclass(frozen=True)
class HotbarConfig:
    enabled: bool = True
    slots: list[HotbarSlot] = field(default_factory=list)
    # Never press two hotbar keys closer together than this.
    min_gap_s: float = 0.4
    # use_when=mobs_nearby: how many visible mobs count as "nearby".
    mobs_nearby_count: int = 2
    # use_when=backpack_full: fraction of capacity.
    backpack_full_at: float = 0.95

    def validate(self) -> list[str]:
        p = []
        slots = [s.slot for s in self.slots]
        if len(slots) != len(set(slots)):
            p.append("the same hotbar slot is listed twice")
        if self.min_gap_s < 0 or self.mobs_nearby_count < 1:
            p.append("min_gap_s must be >= 0 and mobs_nearby_count >= 1")
        if not 0.3 <= self.backpack_full_at <= 1.0:
            p.append("backpack_full_at must be in [0.3, 1.0]")
        return p

    def slot(self, n: int) -> HotbarSlot | None:
        return next((s for s in self.slots if s.slot == n), None)


@dataclass(frozen=True)
class CombatConfig:
    """Dodging and fighting RBC mobs (tactics from the BSS wiki)."""

    dodge_enabled: bool = True
    # Mechsquitos aim at where you ARE: keep moving so their shots miss.
    # circle | zigzag | none
    mechsquito_dodge: str = "circle"
    dodge_radius: float = 1.0               # size of the dodge pattern (1 = normal)
    # Start dodging when this many Mechsquito Toxin stacks are on you (0 = always when one is seen).
    dodge_at_toxin_stacks: int = 0
    # Cogturrets fire straight ahead after rising: step sideways out of their line.
    sidestep_cogturrets: bool = True
    sidestep_s: float = 0.6
    # Cogmowers drive in straight lines and don't aim: step out of their path.
    avoid_cogmowers: bool = True
    # Move close to mobs so your bees lock on and kill them faster.
    stand_near_mobs: bool = True
    prioritize_cogturrets: bool = True
    jump_while_dodging: bool = False
    # Golden Cogmower: 14-20 Cogs, 3 per run in Clover/Pineapple/Pumpkin/Mountain Top.
    hunt_golden_cogmower: bool = True
    golden_max_detour_s: float = 25.0
    # Leave a field if this many Toxin stacks pile up (0 = never).
    leave_field_at_toxin_stacks: int = 8
    # Geometry, in field units (~ one player width = 1).
    danger_line_width: float = 1.5      # half-width of a turret shot / mower path
    cogmower_alert_range: float = 4.0
    approach_min_dist: float = 2.0      # closer than this: already close enough
    approach_range: float = 6.0         # farther than this: not worth walking to

    def validate(self) -> list[str]:
        p = []
        if self.mechsquito_dodge not in ("circle", "zigzag", "none"):
            p.append("mechsquito_dodge must be circle, zigzag or none")
        if not 0.2 <= self.dodge_radius <= 3.0:
            p.append("dodge_radius must be in [0.2, 3.0]")
        if not 0 <= self.dodge_at_toxin_stacks <= 10 or not 0 <= self.leave_field_at_toxin_stacks <= 10:
            p.append("toxin stack values must be 0..10")
        if self.sidestep_s <= 0 or self.golden_max_detour_s < 0:
            p.append("sidestep_s must be > 0 and golden_max_detour_s >= 0")
        return p


@dataclass(frozen=True)
class CaptureConfig:
    # A grab is reused for this long, so detectors in the same tick share it.
    cache_max_age_ms: int = 50
    # Countdown before the "Screenshot" button grabs, so you can switch to Roblox.
    screenshot_delay_s: int = 3
    save_region_crops: bool = True

    def validate(self) -> list[str]:
        p = []
        if not 0 <= self.cache_max_age_ms <= 2000:
            p.append("cache_max_age_ms must be 0..2000")
        if not 0 <= self.screenshot_delay_s <= 30:
            p.append("screenshot_delay_s must be 0..30")
        return p


@dataclass(frozen=True)
class OcrConfig:
    # Full path to tesseract.exe. Empty = find it automatically (see perception/ocr.py).
    tesseract_path: str = ""
    # Crops are enlarged this many times before reading; small game text needs it.
    upscale: int = 3
    timeout_s: float = 5.0
    # Yellow reward text ("Reward: 13 Cogs"): pixels with red and green at least
    # this bright and blue at most yellow_max_b count as text.
    yellow_min_rg: int = 190
    yellow_max_b: int = 140
    # Colour of the green title bars that locate the RBC dialog and round panel (RGB).
    anchor_rgb: list[int] = field(default_factory=lambda: [74, 213, 84])
    anchor_tolerance: int = 18

    def validate(self) -> list[str]:
        p = []
        if not 1 <= self.upscale <= 6:
            p.append("upscale must be 1..6")
        if not 0.5 <= self.timeout_s <= 30:
            p.append("timeout_s must be 0.5..30")
        if len(self.anchor_rgb) != 3 or not all(0 <= c <= 255 for c in self.anchor_rgb):
            p.append("anchor_rgb must be three numbers 0..255")
        if not 0 <= self.anchor_tolerance <= 100:
            p.append("anchor_tolerance must be 0..100")
        return p


def _default_timeouts() -> dict[str, float]:
    t = {s.value: 30.0 for s in AgentState if s not in UNTIMED_STATES}
    t.update({
        "STARTING_ROBO_BEAR": 20.0,
        "WAITING_FOR_RBC": 15.0,
        "RBC_ROUND_START": 10.0,
        "RBC_SELECTING_QUEST": 15.0,
        "RBC_ANALYZING": 5.0,
        "RBC_MOVING_TO_FIELD": 45.0,
        "RBC_FARMING": 900.0,           # watchdog only; rounds end on evidence
        "RBC_COLLECTING_TOKENS": 8.0,
        "RBC_MANAGING_CAPACITY": 3.0,
        "RBC_RETURNING_HIVE": 60.0,
        "RBC_SELECTING_UPGRADES": 20.0,
        "RBC_PURCHASING_UPGRADES": 10.0,
        "RBC_NEXT_ROUND": 20.0,
        "RECOVERY": 60.0,
    })
    return t


@dataclass(frozen=True)
class RecoveryConfig:
    max_attempts_per_level: int = 2
    max_recoveries_per_round: int = 6
    # Pause after this many consecutive recoveries that did not help.
    max_consecutive_failures: int = 3
    # Level at which escalation starts for each failure kind (1..6).
    start_level: dict[str, int] = field(default_factory=lambda: {
        "detection": 1, "timeout": 1, "stuck": 2, "unexpected_ui": 1,
        "focus_lost": 1, "death": 3, "loading": 1,
    })
    save_screenshot: bool = True

    def validate(self) -> list[str]:
        p = []
        if self.max_attempts_per_level < 1:
            p.append("max_attempts_per_level must be >= 1")
        if self.max_recoveries_per_round < 1 or self.max_consecutive_failures < 1:
            p.append("recovery limits must be >= 1")
        p += [f"start_level.{k}={v} must be 1..6" for k, v in self.start_level.items()
              if not 1 <= v <= 6]
        return p


_VALID_HOTKEYS = {f"F{i}" for i in range(1, 13)} | {"PAUSE", "SCROLL_LOCK", "INSERT", "HOME", "END"}


@dataclass(frozen=True)
class HotkeysConfig:
    pause: str = "F8"
    resume: str = "F9"
    emergency_stop: str = "F10"

    def validate(self) -> list[str]:
        keys = [self.pause.upper(), self.resume.upper(), self.emergency_stop.upper()]
        p = [f"hotkey '{k}' not allowed (use one of {sorted(_VALID_HOTKEYS)})"
             for k in keys if k not in _VALID_HOTKEYS]
        if len(set(keys)) != 3:
            p.append("pause, resume and emergency_stop must be different keys")
        return p


@dataclass(frozen=True)
class DebugConfig:
    enabled: bool = False
    save_screenshots_on_failure: bool = True
    screenshot_dir: str = "debug"
    max_screenshots: int = 200
    draw_overlays: bool = False

    def validate(self) -> list[str]:
        return [] if self.max_screenshots >= 0 else ["max_screenshots must be >= 0"]


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/rbc_agent.log"
    max_bytes: int = 5_000_000
    backup_count: int = 5
    console: bool = True

    def validate(self) -> list[str]:
        p = []
        if self.level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            p.append(f"level '{self.level}' must be DEBUG/INFO/WARNING/ERROR")
        if self.max_bytes < 10_000 or self.backup_count < 0:
            p.append("max_bytes must be >= 10000 and backup_count >= 0")
        return p


@dataclass(frozen=True)
class RunConfig:
    max_rounds: int = 0                 # 0 = no limit (play until RBC ends)
    max_runs: int = 1                   # how many RBC attempts in a row
    max_runtime_min: float = 0.0        # 0 = no limit
    stop_after_consecutive_failed_runs: int = 3

    def validate(self) -> list[str]:
        p = []
        if self.max_rounds < 0 or self.max_runs < 0 or self.max_runtime_min < 0:
            p.append("run limits must be >= 0")
        if self.stop_after_consecutive_failed_runs < 1:
            p.append("stop_after_consecutive_failed_runs must be >= 1")
        return p


@dataclass(frozen=True)
class StatisticsConfig:
    file: str = "logs/statistics.json"
    export_csv: bool = True


@dataclass(frozen=True)
class AppConfig:
    window: WindowConfig = field(default_factory=WindowConfig)
    regions: dict[str, Region] = field(default_factory=_default_regions)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    scan_rates: ScanRatesConfig = field(default_factory=ScanRatesConfig)
    pollen: PollenConfig = field(default_factory=PollenConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)
    input: InputConfig = field(default_factory=InputConfig)
    tokens: TokenConfig = field(default_factory=TokenConfig)
    fields: FieldConfig = field(default_factory=FieldConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    hive: HiveConfig = field(default_factory=HiveConfig)
    bee_picks: BeePickConfig = field(default_factory=BeePickConfig)
    quests: QuestConfig = field(default_factory=QuestConfig)
    upgrades: UpgradeConfig = field(default_factory=UpgradeConfig)
    drives: DriveConfig = field(default_factory=DriveConfig)
    hotbar: HotbarConfig = field(default_factory=HotbarConfig)
    combat: CombatConfig = field(default_factory=CombatConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    timeouts_s: dict[str, float] = field(default_factory=_default_timeouts)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    run: RunConfig = field(default_factory=RunConfig)
    statistics: StatisticsConfig = field(default_factory=StatisticsConfig)

    REQUIRED_REGIONS = ("pollen_bar", "round_banner", "center_dialog", "scene")

    def validate(self) -> list[str]:
        p = [f"regions missing required '{r}'" for r in self.REQUIRED_REGIONS
             if r not in self.regions]
        for name, val in self.timeouts_s.items():
            if name not in AgentState.__members__:
                p.append(f"timeouts_s: unknown state '{name}'")
            elif AgentState(name) in UNTIMED_STATES:
                p.append(f"timeouts_s: state '{name}' cannot have a timeout")
            elif val <= 0:
                p.append(f"timeouts_s.{name} must be > 0")
        # Cross-section: the drive's hotbar slot can't hold another item.
        ds = self.drives.hotbar_slot
        other = self.hotbar.slot(ds) if ds else None
        if other and other.item and "drive" not in other.item.lower():
            p.append(f"drives.hotbar_slot={ds} but hotbar slot {ds} holds '{other.item}'")
        return p

    def timeout_for(self, state: AgentState) -> float | None:
        """Timeout for a state, or None if the state is untimed."""
        if state in UNTIMED_STATES:
            return None
        return self.timeouts_s.get(state.value, 30.0)


# --------------------------------------------------------------------------- #
# Generic dict <-> dataclass conversion
# --------------------------------------------------------------------------- #


def to_dict(obj: Any) -> Any:
    """Convert a config object into plain JSON-compatible data."""
    if dataclasses.is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def _convert(tp: Any, value: Any, path: str, problems: list[str]) -> Any:
    """Convert raw JSON ``value`` to type ``tp``, recording problems."""
    origin = get_origin(tp)

    if origin in (Union, types.UnionType):  # Optional[X] etc.
        args = [a for a in get_args(tp) if a is not type(None)]
        if value is None:
            return None
        return _convert(args[0], value, path, problems)

    if dataclasses.is_dataclass(tp):
        if not isinstance(value, dict):
            problems.append(f"{path}: expected an object, got {type(value).__name__}")
            return None
        return _build(tp, value, path, problems)

    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except ValueError:
            problems.append(f"{path}: '{value}' is not one of {[m.value for m in tp]}")
            return None

    if origin is dict:
        _, vt = get_args(tp)
        if not isinstance(value, dict):
            problems.append(f"{path}: expected an object, got {type(value).__name__}")
            return {}
        return {str(k): _convert(vt, v, f"{path}.{k}", problems) for k, v in value.items()}

    if origin in (list, tuple):
        (et, *_) = get_args(tp) or (Any,)
        if not isinstance(value, list):
            problems.append(f"{path}: expected a list, got {type(value).__name__}")
            return []
        return [_convert(et, v, f"{path}[{i}]", problems) for i, v in enumerate(value)]

    if tp is bool:
        if not isinstance(value, bool):
            problems.append(f"{path}: expected true/false, got {value!r}")
        return bool(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{path}: expected an integer, got {value!r}")
            return 0
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{path}: expected a number, got {value!r}")
            return 0.0
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            problems.append(f"{path}: expected text, got {value!r}")
            return str(value)
        return value
    return value


def _build(cls: type, data: dict[str, Any], path: str, problems: list[str]) -> Any:
    hints = get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    for key in data:
        if key not in names:
            hint = difflib.get_close_matches(key, names, n=1)
            suffix = f" (did you mean '{hint[0]}'?)" if hint else ""
            problems.append(f"{path}.{key}: unknown setting{suffix}".lstrip("."))
    kwargs = {}
    for name in names:
        if name in data:
            kwargs[name] = _convert(hints[name], data[name], f"{path}.{name}".lstrip("."), problems)
    try:
        return cls(**kwargs)
    except TypeError as exc:  # missing required field (e.g. Region without "h")
        problems.append(f"{path}: {exc}")
        return None


def _validate_tree(obj: Any, path: str, problems: list[str]) -> None:
    """Call ``validate()`` on every dataclass in the tree, depth first."""
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            _validate_tree(getattr(obj, f.name), f"{path}.{f.name}".lstrip("."), problems)
        if hasattr(obj, "validate"):
            problems.extend(f"{path or 'config'}: {msg}" for msg in obj.validate())
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _validate_tree(v, f"{path}.{k}", problems)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _validate_tree(v, f"{path}[{i}]", problems)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return ``base`` with ``override`` applied recursively (inputs untouched)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_config(overrides: dict[str, Any] | None = None) -> AppConfig:
    """Build and validate an AppConfig from defaults plus ``overrides``.

    Raises ConfigError listing *all* problems found.
    """
    merged = deep_merge(to_dict(AppConfig()), overrides or {})
    problems: list[str] = []
    cfg = _build(AppConfig, merged, "", problems)
    if not problems:
        _validate_tree(cfg, "", problems)
    if problems:
        raise ConfigError(problems)
    return cfg


# --------------------------------------------------------------------------- #
# File handling + hot reload
# --------------------------------------------------------------------------- #

Listener = Callable[[AppConfig], None]


class ConfigManager:
    """Owns the active config snapshot and handles (re)loading from disk."""

    def __init__(self, user_file: Path = USER_CONFIG_FILE,
                 default_file: Path = DEFAULT_CONFIG_FILE):
        self._user_file = Path(user_file)
        self._default_file = Path(default_file)
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._config: AppConfig = AppConfig()

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return self._config

    def subscribe(self, listener: Listener) -> None:
        """Get called with the new snapshot after every successful reload."""
        with self._lock:
            self._listeners.append(listener)

    def write_default_file(self) -> Path:
        """(Re)generate default_config.json from the dataclass defaults."""
        self._default_file.parent.mkdir(parents=True, exist_ok=True)
        self._default_file.write_text(json.dumps(to_dict(AppConfig()), indent=2), encoding="utf-8")
        return self._default_file

    def ensure_user_file(self) -> Path:
        """Create an empty override file on first run so the user has somewhere to edit."""
        if not self._user_file.exists():
            self._user_file.parent.mkdir(parents=True, exist_ok=True)
            self._user_file.write_text("{}\n", encoding="utf-8")
        return self._user_file

    def load(self) -> AppConfig:
        """Load defaults + user overrides. On error the previous config stays active."""
        overrides = self._read_overrides()
        new_cfg = parse_config(overrides)
        with self._lock:
            self._config = new_cfg
            listeners = list(self._listeners)
        for listener in listeners:
            listener(new_cfg)
        return new_cfg

    reload = load

    def _read_overrides(self) -> dict[str, Any]:
        if not self._user_file.exists():
            return {}
        try:
            data = json.loads(self._user_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self._user_file.name}: invalid JSON at line {exc.lineno}, "
                              f"column {exc.colno}: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{self._user_file.name}: top level must be a JSON object")
        # Allow "_comment" keys anywhere at the top level for user notes.
        return {k: v for k, v in data.items() if not k.startswith("_")}
