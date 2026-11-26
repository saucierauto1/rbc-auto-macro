"""Your hive: bee catalogue (data/bees.json) + the bees you entered on the Hive tab."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..core.config import HiveConfig
from ..core.paths import DATA_DIR

BEES_FILE: Path = DATA_DIR / "bees.json"
DIGITAL_BEE = "Digital"
# A colour counts as "your hive colour" once it has this share of colour bees.
DOMINANT_SHARE = 0.6


@dataclass(frozen=True)
class BeeInfo:
    name: str
    color: str      # red / blue / white
    rarity: str


def normalize_bee(name: str) -> str:
    """'digital bee', 'Digital Bee ', 'DIGITAL' -> 'Digital'."""
    n = " ".join(name.strip().split())
    if n.lower().endswith(" bee"):
        n = n[:-4]
    return n[:1].upper() + n[1:].lower()


@lru_cache(maxsize=4)
def load_catalog(path: Path = BEES_FILE) -> dict[str, BeeInfo]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))["bees"]
    return {n: BeeInfo(n, v["color"], v["rarity"]) for n, v in raw.items()}


@dataclass(frozen=True)
class HiveProfile:
    counts: dict[str, int]           # bee name -> how many
    gifted: dict[str, int]
    color: str                        # red / blue / white / mixed
    color_counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def has(self, bee: str) -> bool:
        return self.counts.get(normalize_bee(bee), 0) > 0

    @property
    def has_digital_bee(self) -> bool:
        return self.has(DIGITAL_BEE)

    @classmethod
    def from_config(cls, cfg: HiveConfig, catalog: dict[str, BeeInfo] | None = None) -> "HiveProfile":
        catalog = catalog if catalog is not None else load_catalog()
        counts: dict[str, int] = {}
        gifted: dict[str, int] = {}
        colors: Counter[str] = Counter()
        for e in cfg.bees:
            name = normalize_bee(e.bee)
            counts[name] = counts.get(name, 0) + e.count
            gifted[name] = gifted.get(name, 0) + e.gifted
            info = catalog.get(name)
            if info:
                colors[info.color] += e.count
        color = cfg.color_override or dominant_color(colors)
        return cls(counts, gifted, color, dict(colors))


def dominant_color(colors: Counter[str] | dict[str, int]) -> str:
    """Red/blue hive if that colour has >= 60% of the coloured (red+blue) bees
    and outnumbers white; white if white has >= 60% of all bees; else mixed."""
    red, blue, white = colors.get("red", 0), colors.get("blue", 0), colors.get("white", 0)
    total = red + blue + white
    if total == 0:
        return "mixed"
    if white / total >= DOMINANT_SHARE:
        return "white"
    for c, n, other in (("red", red, blue), ("blue", blue, red)):
        if n + other and n / (n + other) >= DOMINANT_SHARE and n >= white:
            return c
    return "mixed"
