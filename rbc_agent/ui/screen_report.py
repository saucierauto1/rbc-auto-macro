"""Text for the "Read screen" button: what was read, and what the bot would choose."""

from __future__ import annotations

from ..core.config import AppConfig
from ..decision.picks import OfferedBee, QuestCard, ShopItem, pick_bees, pick_quest, plan_shop
from ..game.hive import BeeInfo, HiveProfile
from ..game.rules import RBCRules
from ..game.screens import ScreenState

KIND_NAMES = {"quest_choice": "Quest choice", "bee_pick": "Bee pick", "upgrade_shop": "Upgrade shop",
              "in_round": "Round in progress", "dialog": "RBC window (unknown page)"}


def _num(v) -> str:
    return "?" if v is None else f"{v:,.0f}" if isinstance(v, float) else str(v)


def _objectives(objs) -> str:
    return "; ".join(o.text.capitalize() for o in objs) or "(not understood)"


def describe(st: ScreenState, cfg: AppConfig, rules: RBCRules,
             catalog: dict[str, BeeInfo]) -> list[str]:
    if st.kind == "unknown":
        return ["No RBC screen found. Open the quest, bee or upgrade window, or start a round, and try again."]
    hive = HiveProfile.from_config(cfg.hive, catalog)
    lines = [f"{KIND_NAMES.get(st.kind, st.kind)}: round {_num(st.round_no)}, {_num(st.cogs)} Cogs"]
    if st.kind == "in_round":
        t = st.time_left_s
        lines.append(f"Time left {t // 60}:{t % 60:02d}" if t is not None else "Time left ?")
        lines.append(f"Quest: {_objectives(st.objectives)}")
    elif st.kind == "quest_choice":
        for side, q in zip("AB", st.quests):
            lines.append(f"Quest {side} ({_num(q.reward)} Cogs): {_objectives(q.objectives)}")
        lines.append(f"Free rerolls left: {_num(st.free_rerolls)}")
        if len(st.quests) == 2 and all(q.objectives for q in st.quests):
            res = pick_quest([QuestCard.from_offer(q) for q in st.quests], cfg.quests, hive,
                             rules.fields, free_reroll_left=bool(st.free_rerolls))
            choice = "reroll" if res.reroll else f"Quest {'AB'[res.index]}"
            lines.append(f"Bot would choose: {choice} ({res.reason})")
    elif st.kind == "upgrade_shop":
        for o in st.shop:
            known = "" if o.upgrade_id else "  [not in upgrade list]"
            lines.append(f"{o.name}: {_num(o.price)} Cogs, {o.effects}{known}")
        lines.append(f"Reroll costs {_num(st.reroll_cost)} Cogs")
        items = [ShopItem(o.upgrade_id, o.price) for o in st.shop if o.upgrade_id]
        if items:
            plan = plan_shop(items, st.cogs, rules.upgrades, cfg.upgrades,
                             cfg.strategy.active_weights(), hive)
            names = {o.upgrade_id: o.name for o in st.shop}
            buy = ", ".join(names[i] for i in plan.buy) or "nothing"
            lock = ", ".join(names[i] for i in plan.lock) or "nothing"
            lines.append(f"Bot would buy: {buy}; lock: {lock}")
    elif st.kind == "bee_pick":
        for b in st.bees:
            extra = f", {b.mutation}" if b.mutation else ""
            lines.append(f"{b.name} Bee (Lvl {_num(b.level)}{extra})")
        lines.append(f"Reroll costs {_num(st.reroll_cost)} Cogs")
        if st.bees:
            res = pick_bees([OfferedBee(b.name) for b in st.bees], 1, cfg.bee_picks, hive, catalog)
            lines.append(f"Bot would pick: {res.picks[0].name} Bee ({res.reason})")
    lines += [f"Could not read: {p}" for p in st.problems]
    return lines
