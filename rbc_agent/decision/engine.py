"""Stage 5: what to do on each RBC screen. Pure: GameState in, decisions out.

Targets are indexes (quest card 0/1, bee row, shop row), never pixels; the
input layer (Stage 6) turns an index into a click. If a card or row wasn't
read completely, the answer is a single ``wait`` decision with the reason,
never a guess. Drives aren't decided here yet: no drive shop has been seen
on screen.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import AppConfig
from ..game.hive import BeeInfo, HiveProfile
from ..game.rules import RBCRules
from ..game.state import GameState
from .picks import OfferedBee, QuestCard, ShopItem, pick_bees, pick_quest, plan_shop

ACTIONS = ("choose_quest", "reroll_quest", "pick_bee", "buy_upgrade", "lock_upgrade", "start_round",
           "press_slot", "wait")


@dataclass(frozen=True)
class Decision:
    action: str                 # one of ACTIONS
    target: int | None = None   # card / row index, or hotbar slot number
    what: str = ""              # human name of the target ("Quest B", "Spicy Bee", "Credit")
    reason: str = ""


@dataclass(frozen=True)
class DecisionContext:
    cfg: AppConfig
    rules: RBCRules
    catalog: dict[str, BeeInfo]
    hive: HiveProfile


def _wait(reason: str) -> list[Decision]:
    return [Decision("wait", reason=reason)]


def decide(gs: GameState, ctx: DecisionContext) -> list[Decision]:
    """Decisions for the dialog on screen, in the order to carry them out."""
    if gs.screen == "quest_choice":
        return _quest(gs, ctx)
    if gs.screen == "bee_pick":
        return _bee(gs, ctx)
    if gs.screen == "upgrade_shop":
        return _shop(gs, ctx)
    return []


def _quest(gs: GameState, ctx: DecisionContext) -> list[Decision]:
    if len(gs.quest_offers) != 2:
        return _wait(f"expected 2 quest cards, read {len(gs.quest_offers)}")
    for side, q in zip("AB", gs.quest_offers):
        if not q.objectives:
            return _wait(f"quest {side} unreadable")
    res = pick_quest([QuestCard.from_offer(q) for q in gs.quest_offers], ctx.cfg.quests, ctx.hive,
                     ctx.rules.fields, free_reroll_left=bool(gs.free_reroll_left))
    if res.reroll:
        return [Decision("reroll_quest", what="free reroll", reason=res.reason)]
    return [Decision("choose_quest", res.index, f"Quest {'AB'[res.index]}", res.reason)]


def _bee(gs: GameState, ctx: DecisionContext) -> list[Decision]:
    if not gs.bee_offers:
        return _wait("no bees read")
    unknown = [b.name for b in gs.bee_offers if not b.known]
    if unknown:
        return _wait(f"bee not in bee list: {', '.join(unknown)}")
    offered = [OfferedBee(b.name) for b in gs.bee_offers]
    res = pick_bees(offered, 1, ctx.cfg.bee_picks, ctx.hive, ctx.catalog)
    i = next(i for i, b in enumerate(offered) if b is res.picks[0])   # same name twice: keep the row
    return [Decision("pick_bee", i, f"{offered[i].name} Bee")]      # pick_bees' reason would just repeat the name


def _shop(gs: GameState, ctx: DecisionContext) -> list[Decision]:
    if gs.cogs is None:
        return _wait("Cogs not read")
    if not gs.shop:
        return _wait("no upgrades read")
    missing = [o.name for o in gs.shop if o.price is None]
    if missing:
        return _wait(f"price unreadable: {', '.join(missing)}")
    # Rows not in the upgrade list (misread or new) can't be scored, so they are never bought.
    items = [ShopItem(o.upgrade_id, o.price) for o in gs.shop if o.upgrade_id]
    plan = plan_shop(items, gs.cogs, ctx.rules.upgrades, ctx.cfg.upgrades,
                     ctx.cfg.strategy.active_weights(), ctx.hive)
    free_rows = list(range(len(gs.shop)))

    def row_of(upgrade_id: str) -> int:
        i = next(r for r in free_rows if gs.shop[r].upgrade_id == upgrade_id)
        free_rows.remove(i)                     # each row once, so two equal offers stay distinct
        return i

    out = [Decision("buy_upgrade", r, gs.shop[r].name, f"{gs.shop[r].price} Cogs")
           for r in map(row_of, plan.buy)]
    out += [Decision("lock_upgrade", r, gs.shop[r].name, "priority, can't afford yet")
            for r in map(row_of, plan.lock)]
    spent = sum(gs.shop[d.target].price for d in out if d.action == "buy_upgrade")
    out.append(Decision("start_round", reason=f"{gs.cogs - spent} Cogs left"))
    return out
