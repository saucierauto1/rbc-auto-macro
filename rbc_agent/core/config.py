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
    # PLACEHOLDERS. These are rough guesses for a default 16:9 Roblox HUD
    # and MUST be confirmed with the calibration tool (Stage 3).
    return {
        "pollen_bar": Region(0.36, 0.005, 0.28, 0.035),
        "pollen_text": Region(0.40, 0.005, 0.20, 0.035),
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
    # (RBC rounds are 300 s; finishing under 120 s pays bonus Cogs.)
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
class HiveConfig:
    profile_file: str = "data/hive_profile.json"
    auto_scan: bool = True
    # "" = detect automatically; otherwise force red/blue/white/mixed.
    color_override: str = ""

    def validate(self) -> list[str]:
        if self.color_override not in {"", "red", "blue", "white", "mixed"}:
            return [f"color_override '{self.color_override}' must be '', red, blue, white or mixed"]
        return []


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
