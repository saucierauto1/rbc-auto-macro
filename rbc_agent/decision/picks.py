"""Bee picks, quest choice and upgrade buying. Pure logic driven by config.

Each picker returns what it chose *and* a reason, so the log can say why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.config import BeePickConfig, QuestConfig, StrategyWeights, UpgradeConfig
from ..game.hive import BeeInfo, HiveProfile, normalize_bee
from ..game.rules import UpgradeDef

RARITY_SCORE = {"common": 0, "rare": 1, "epic": 2, "legendary": 3, "mythic": 4, "event": 4}


# --------------------------------------------------------------------------- bees
@dataclass(frozen=True)
class OfferedBee:
    name: str
    gifted: bool = False


@dataclass(frozen=True)
class BeePickResult:
    picks: list[OfferedBee]
    reroll: bool
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""


def score_bee(bee: OfferedBee, cfg: BeePickConfig, hive: HiveProfile,
              catalog: dict[str, BeeInfo]) -> float:
    name = normalize_bee(bee.name)
    prio = [normalize_bee(b) for b in cfg.priority]
    avoid = {normalize_bee(b) for b in cfg.avoid}
    info = catalog.get(name)
    s = 0.0
    if name in prio:
        s += 100.0 - 3.0 * prio.index(name)        # list order matters most
    if info:
        s += 2.0 * RARITY_SCORE.get(info.rarity, 0)
        if cfg.prefer_hive_color and hive.color in ("red", "blue", "white") and info.color == hive.color:
            s += 6.0
    if cfg.prefer_gifted and bee.gifted:
        s += 8.0
    if name in avoid:
        s -= 1000.0
    return s


def pick_bees(offered: list[OfferedBee], n: int, cfg: BeePickConfig, hive: HiveProfile,
              catalog: dict[str, BeeInfo], *, can_reroll: bool = False) -> BeePickResult:
    scored = [(score_bee(b, cfg, hive, catalog), i, b) for i, b in enumerate(offered)]
    scored.sort(key=lambda t: (-t[0], t[1]))
    prio = {normalize_bee(b) for b in cfg.priority}
    any_prio = any(normalize_bee(b.name) in prio for b in offered)
    reroll = can_reroll and cfg.reroll_if_no_priority and not any_prio
    picks = [b for _, _, b in scored[:n]]
    reason = ("no priority bee offered, rerolling" if reroll
              else "picked " + ", ".join(b.name + ("*" if b.gifted else "") for b in picks))
    return BeePickResult(picks, reroll, {f"{b.name}#{i}": s for s, i, b in scored}, reason)


# --------------------------------------------------------------------------- quests
@dataclass(frozen=True)
class QuestCard:
    type: str
    amount: float = 0.0          # pollen / mobs requested (bigger = harder)
    color: str = ""              # for collect_color_pollen
    field: str = ""              # for collect_field_pollen


@dataclass(frozen=True)
class QuestPickResult:
    index: int                   # which card to click (-1 = use reroll)
    reroll: bool
    reason: str


def pick_quest(cards: list[QuestCard], cfg: QuestConfig, hive: HiveProfile,
               field_colors: dict[str, str], *, free_reroll_left: bool) -> QuestPickResult:
    if not cards:
        return QuestPickResult(-1, False, "no quest cards")
    if free_reroll_left and cfg.free_reroll_if_both and all(c.type in cfg.free_reroll_if_both for c in cards):
        return QuestPickResult(-1, True, "both quests unwanted, using free reroll")

    def key(ic: tuple[int, QuestCard]):
        i, c = ic
        rank = cfg.priority.index(c.type) if c.type in cfg.priority else len(cfg.priority)
        color = c.color or field_colors.get(c.field, "")
        matches = int(hive.color != "mixed" and color == hive.color)
        harder = c.amount if cfg.prefer_harder_for_cogs else -c.amount
        return (rank, -matches, -harder, i)

    i, best = min(enumerate(cards), key=key)
    return QuestPickResult(i, False, f"{best.type} ({best.color or best.field or 'any'}) ranks highest")


# --------------------------------------------------------------------------- upgrades
@dataclass(frozen=True)
class ShopItem:
    upgrade_id: str
    price: int | None            # None = price not readable
    locked: bool = False


@dataclass(frozen=True)
class ShopPlan:
    buy: list[str]
    lock: list[str]
    reason: str


def score_upgrade(u: UpgradeDef, cfg: UpgradeConfig, weights: StrategyWeights, hive: HiveProfile) -> float:
    s = 10.0 * cfg.rarity_bias.get(u.rarity, 1.0)
    for t in u.tags:
        s *= weights.tag_bias.get(t, 1.0)
    if u.color in ("red", "blue", "white"):
        s *= weights.color_bias.get(u.color, 1.0)
        if u.color == hive.color:
            s *= 1.3
        elif hive.color in ("red", "blue") and u.color in ("red", "blue"):
            s *= 0.5   # off-colour upgrade for a mono hive
    if u.id in cfg.priority:
        s += 1000.0 - 10.0 * cfg.priority.index(u.id)
    return s


def plan_shop(items: list[ShopItem], cogs: int | None, upgrades: dict[str, UpgradeDef],
              cfg: UpgradeConfig, weights: StrategyWeights, hive: HiveProfile) -> ShopPlan:
    if not cfg.auto_buy:
        return ShopPlan([], [], "auto-buy is off")
    cands = [(score_upgrade(upgrades[it.upgrade_id], cfg, weights, hive), it)
             for it in items if it.upgrade_id in upgrades and it.upgrade_id not in cfg.blacklist]
    cands.sort(key=lambda t: -t[0])
    budget = cogs
    buy, lock = [], []
    for _, it in cands:
        if it.price is not None and budget is not None and budget - it.price >= cfg.min_cog_reserve:
            buy.append(it.upgrade_id)
            budget -= it.price
        elif cfg.lock_unaffordable_priority and it.upgrade_id in cfg.priority and not it.locked:
            lock.append(it.upgrade_id)
    reason = f"buy {buy or 'nothing'}, lock {lock or 'nothing'}, cogs left {budget}"
    return ShopPlan(buy, lock, reason)
