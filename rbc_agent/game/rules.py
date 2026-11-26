"""Typed access to data/rbc_rules.json (game knowledge lives in data, not code)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError
from ..core.paths import RULES_FILE

RARITIES = ("common", "rare", "epic", "legendary")
OPS = {"mul", "add", "add_pct", "special"}


@dataclass(frozen=True)
class Effect:
    op: str
    value: float = 0.0
    stat: str = ""
    field: str = ""
    text: str = ""


@dataclass(frozen=True)
class UpgradeDef:
    id: str
    name: str
    rarity: str
    cap: int
    effects: tuple[Effect, ...]
    effects_at_cap: tuple[Effect, ...] | None
    tags: tuple[str, ...]
    color: str

    @property
    def stackable(self) -> bool:
        return self.cap > 1


class RBCRules:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        problems = self._validate()
        if problems:
            raise ConfigError([f"rbc_rules: {p}" for p in problems])

    @classmethod
    def load(cls, path: Path = RULES_FILE) -> "RBCRules":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"rbc_rules.json: invalid JSON at line {exc.lineno}: {exc.msg}") from exc

    # --- convenient views -------------------------------------------------
    @property
    def round_time_limit_s(self) -> float:
        return float(self.raw["challenge"]["round_time_limit_s"])

    @property
    def fast_bonus_under_s(self) -> float:
        return float(self.raw["challenge"]["fast_bonus_under_s"])

    @property
    def total_rounds(self) -> int:
        return int(self.raw["challenge"]["total_rounds"])

    @property
    def fields(self) -> dict[str, str]:
        """Field name -> dominant colour."""
        return {k: v["color"] for k, v in self.raw["fields"].items()}

    @cached_property
    def upgrades(self) -> dict[str, UpgradeDef]:
        out = {}
        for u in self.raw["upgrades"]:
            conv = lambda lst: tuple(Effect(**e) for e in lst)  # noqa: E731
            out[u["id"]] = UpgradeDef(
                id=u["id"], name=u["name"], rarity=u["rarity"], cap=u["cap"],
                effects=conv(u["effects"]),
                effects_at_cap=conv(u["effects_at_cap"]) if u.get("effects_at_cap") else None,
                tags=tuple(u["tags"]), color=u["color"])
        return out

    def upgrade_by_name(self, ocr_name: str) -> UpgradeDef | None:
        """Look up an upgrade from OCR text (case/spacing tolerant)."""
        key = "".join(ch for ch in ocr_name.lower() if ch.isalnum())
        for u in self.upgrades.values():
            if "".join(ch for ch in u.name.lower() if ch.isalnum()) == key:
                return u
        return None

    def rarity_weight(self, rarity: str, round_no: int) -> float:
        lo, hi = self.raw["upgrade_rarity_weights"][rarity]
        r = min(max(round_no, 1), self.total_rounds)
        return lo + (hi - lo) * (r - 1) / (self.total_rounds - 1)

    # --- validation -------------------------------------------------------
    def _validate(self) -> list[str]:
        p: list[str] = []
        for key in ("challenge", "fields", "upgrades", "upgrade_rarity_weights"):
            if key not in self.raw:
                p.append(f"missing section '{key}'")
        if p:
            return p
        fields = set(self.raw["fields"])
        seen: set[str] = set()
        for u in self.raw["upgrades"]:
            uid = u.get("id", "?")
            if uid in seen:
                p.append(f"duplicate upgrade id '{uid}'")
            seen.add(uid)
            if u.get("rarity") not in RARITIES:
                p.append(f"{uid}: bad rarity {u.get('rarity')!r}")
            if not isinstance(u.get("cap"), int) or u["cap"] < 1:
                p.append(f"{uid}: cap must be an integer >= 1")
            for e in (u.get("effects") or []) + (u.get("effects_at_cap") or []):
                if e.get("op") not in OPS:
                    p.append(f"{uid}: bad effect op {e.get('op')!r}")
                if e.get("field") and e["field"] not in fields:
                    p.append(f"{uid}: unknown field '{e['field']}'")
        return p
