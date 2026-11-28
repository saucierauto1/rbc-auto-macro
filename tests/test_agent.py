"""Stage 5: agent loop = run tracking + state machine + decisions, fed scripted screens."""

import pytest

from rbc_agent.agent import Agent, DryRunActor, describe_decision
from rbc_agent.core.config import parse_config
from rbc_agent.core.errors import AutomationCancelled
from rbc_agent.core.states import AgentState
from rbc_agent.core.timing import CancelToken, FakeClock
from rbc_agent.decision.engine import Decision
from rbc_agent.game.hive import load_catalog
from rbc_agent.game.rules import RBCRules
from rbc_agent.game.screens import BeeOffer, Objective, QuestOffer, ScreenState, ShopOffer

RULES = RBCRules.load()
CAT = load_catalog()
RED = Objective("collect_color_pollen", 187500, "red", text="collect 187,500 red pollen")
ANY = Objective("collect_pollen", 225000, text="collect 225,000 pollen")


def quest(a=(RED,), b=(ANY,), rnd=3, cogs=34):
    return ScreenState("quest_choice", round_no=rnd, cogs=cogs, free_rerolls=1,
                       quests=[QuestOffer(tuple(a), 11, ""), QuestOffer(tuple(b), 11, "")])


def bees(*names, rnd=3, cogs=34):
    return ScreenState("bee_pick", round_no=rnd, cogs=cogs, reroll_cost=5,
                       bees=[BeeOffer(n, n in CAT, 21, "") for n in names])


def shop(rows, rnd=3, cogs=34):
    return ScreenState("upgrade_shop", round_no=rnd, cogs=cogs, reroll_cost=5,
                       shop=[ShopOffer(n, i, p, 1, "") for n, i, p in rows])


def hud(rnd=3, t=297):
    return ScreenState("in_round", round_no=rnd, cogs=34, time_left_s=t, objectives=[ANY])


class Script:
    def __init__(self, *screens):
        self.screens = list(screens)

    def read_state(self):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]


class Log:
    def __init__(self):
        self.lines = []

    def state(self, m): self.lines.append(("state", m))
    def decision(self, m): self.lines.append(("decision", m))
    def recovery(self, m): self.lines.append(("recovery", m))

    def of(self, kind):
        return [m for k, m in self.lines if k == kind]


class Recorder:
    def __init__(self, live=False):
        self.live, self.done = live, []

    def act(self, d):
        self.done.append(d)


def agent(screens, overrides=None, live=False, clock=None):
    cfg = parse_config({"hive": {"color_override": "red"}, **(overrides or {})})
    clock = clock or FakeClock()
    actor = Recorder(live)
    return Agent(cfg, RULES, CAT, Script(*screens), actor, Log(), clock), actor, clock


def steps(a, clock, n, dt=1.0):
    for _ in range(n):
        a.step()
        clock.advance(dt)


def test_quest_decided_once_per_screen_not_per_read():
    a, actor, clock = agent([quest()] * 10)
    steps(a, clock, 10)
    assert [(d.action, d.target) for d in actor.done] == [("choose_quest", 0)]   # red quest for a red hive
    assert a.machine.state is AgentState.RBC_SELECTING_QUEST


def test_half_read_card_means_wait_not_a_guess():
    a, actor, clock = agent([quest(b=())] * 4)
    steps(a, clock, 4)
    assert [d.action for d in actor.done] == ["wait"] and "quest B unreadable" in actor.done[0].reason


def test_two_bee_screens_in_one_round_each_decided_after_confirming():
    first, second = bees("Spicy", "Spicy", "Precise"), bees("Vector", "Ninja", "Vector")
    a, actor, clock = agent([first] * 3 + [second] * 3)
    steps(a, clock, 4)                                   # 3 reads confirm the first, 1 of the second
    assert len(actor.done) == 1 and actor.done[0].action == "pick_bee"
    steps(a, clock, 2)
    assert len(actor.done) == 2 and a.run.snapshot().bee_offers[0].name == "Vector"
    assert actor.done[0].target in (0, 1, 2) and actor.done[1].target in (0, 1, 2)


def test_unknown_bee_waits():
    a, actor, clock = agent([bees("Spicy", "Notabee")] * 3)
    steps(a, clock, 3)
    assert actor.done[0].action == "wait" and "Notabee" in actor.done[0].reason


def test_shop_rows_and_order_red_hive_skips_blue():
    rows = [("Overfit: Blue", "overfit_blue", 8), ("Credit", "credit", 7), ("Crypto", "crypto", 9)]
    a, actor, clock = agent([shop(rows)] * 3)
    steps(a, clock, 3)
    acts = [(d.action, d.target) for d in actor.done]
    assert acts[-1] == ("start_round", None)
    bought = {t for act, t in acts if act == "buy_upgrade"}
    assert 0 not in bought and bought <= {1, 2} and bought


def test_shop_same_upgrade_twice_maps_to_two_rows():
    rows = [("Credit", "credit", 7), ("Credit", "credit", 7)]
    a, actor, clock = agent([shop(rows, cogs=50)] * 3)
    steps(a, clock, 3)
    assert sorted(d.target for d in actor.done if d.action == "buy_upgrade") == [0, 1]


def test_shop_unreadable_price_waits():
    a, actor, clock = agent([shop([("Credit", "credit", None)])] * 3)
    steps(a, clock, 3)
    assert actor.done[0].action == "wait"


def test_timeout_only_when_live_then_retries():
    a, actor, clock = agent([quest()] * 30, live=True)
    steps(a, clock, 20)                                  # quest state times out after 15 s
    assert a.log.of("recovery") and [d.action for d in actor.done].count("choose_quest") == 2
    dry, dry_actor, dclock = agent([quest()] * 30, live=False)
    steps(dry, dclock, 20)
    assert not dry.log.of("recovery") and len(dry_actor.done) == 1


def test_hotbar_round_start_and_cooldown_in_dry_run():
    slots = [{"slot": 2, "item": "Glitter", "use_when": "round_start"},
             {"slot": 3, "item": "Micro-Converter", "use_when": "always", "every_s": 30}]
    a, actor, clock = agent([hud()] * 40, {"hotbar": {"slots": slots}})
    steps(a, clock, 40)
    pressed = [d.target for d in actor.done]
    assert pressed.count(2) == 1                         # round start: once
    assert pressed.count(3) == 2                         # every 30 s over ~37 s of round
    assert all(d.action == "press_slot" for d in actor.done)


def test_dry_run_actor_text_and_loop_stops():
    log = Log()
    DryRunActor(log).act(Decision("choose_quest", 1, "Quest B", "collect_pollen ranks highest"))
    assert log.of("decision") == ["Would choose Quest B (collect_pollen ranks highest)"]
    assert describe_decision(Decision("start_round")) == "Would start the round"
    a, _, clock = agent([quest()])
    token = CancelToken()
    seen = []

    def on_step(gs, state):
        seen.append(state)
        if len(seen) == 3:
            token.stop("test")
    with pytest.raises(AutomationCancelled):
        a.run_until_stopped(token, on_step)
    assert len(seen) == 3
