"""Bee picks, quest choice and upgrade buying. Pure logic driven by config.

Each picker returns what it chose *and* a reason, so the log can say why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.config import BeePickConfig, QuestConfig, StrategyWeights, UpgradeConfig
from ..game.hive import BeeInfo, HiveProfile, normalize_bee
from ..game.rules import UpgradeDef
from ..game.screens import QuestOffer

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
        s += cfg.priority_base - cfg.priority_step * prio.index(name)        # list order matters most
    if info:
        s += cfg.rarity_weight * RARITY_SCORE.get(info.rarity, 0)
        if cfg.prefer_hive_color and hive.color in ("red", "blue", "white") and info.color == hive.color:
            s += cfg.color_match_bonus
    if cfg.prefer_gifted and bee.gifted:
        s += cfg.gifted_bonus
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
    # A real card has several objectives: types of all of them, and its Cog reward.
    parts: tuple[str, ...] = ()
    reward: int | None = None

    @classmethod
    def from_offer(cls, offer: QuestOffer) -> "QuestCard":
        objs = offer.objectives
        first = next((o for o in objs if o.color or o.place), objs[0] if objs else None)
        return cls(first.type if first else "unknown",
                   sum(o.amount or 0 for o in objs),
                   color=first.color if first else "", field=first.place if first else "",
                   parts=tuple(o.type for o in objs), reward=offer.reward)


@dataclass(frozen=True)
class QuestPickResult:
    index: int                   # which card to click (-1 = use reroll)
    reroll: bool
    reason: str


def pick_quest(cards: list[QuestCard], cfg: QuestConfig, hive: HiveProfile,
               field_colors: dict[str, str], *, free_reroll_left: bool) -> QuestPickResult:
    if not cards:
        return QuestPickResult(-1, False, "no quest cards")
    unwanted = lambda c: any(t in cfg.free_reroll_if_both for t in (c.parts or (c.type,)))  # noqa: E731
    if free_reroll_left and cfg.free_reroll_if_both and all(unwanted(c) for c in cards):
        return QuestPickResult(-1, True, "both quests unwanted, using free reroll")

    def rank_of(t: str) -> int:
        return cfg.priority.index(t) if t in cfg.priority else len(cfg.priority)

    def key(ic: tuple[int, QuestCard]):
        i, c = ic
        rank = max(rank_of(t) for t in (c.parts or (c.type,)))     # a card is as bad as its worst part
        color = c.color or field_colors.get(c.field, "")
        matches = int(hive.color != "mixed" and color == hive.color)
        size = c.reward if c.reward is not None else c.amount
        harder = size if cfg.prefer_harder_for_cogs else -size
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
    s = cfg.base_score * cfg.rarity_bias.get(u.rarity, 1.0)
    for t in u.tags:
        s *= weights.tag_bias.get(t, 1.0)
    if u.color in ("red", "blue", "white"):
        s *= weights.color_bias.get(u.color, 1.0)
        if u.color == hive.color:
            s *= cfg.hive_color_mult
        elif hive.color in ("red", "blue") and u.color in ("red", "blue"):
            s *= cfg.off_color_mult   # off-colour upgrade for a mono hive
    if u.id in cfg.priority:
        s += cfg.priority_bonus - cfg.priority_step * cfg.priority.index(u.id)
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
