"""Stage 5: the agent loop. Read the screen -> track the run -> pick the state -> decide -> act.

Each ``step()`` reads the screen once. Decisions for a dialog are made once per
screen content (screen, round, Cogs, offers), not once per read, so the same
click is never sent twice. The actor carries decisions out: ``DryRunActor``
only logs them (Watch); Stage 6 adds one that presses keys. Timeouts only
count when the actor is live, because in Watch you are the one acting.
"""

from __future__ import annotations

from typing import Protocol

from .core.config import AppConfig
from .core.state_machine import StateMachine
from .core.states import AgentState
from .core.timing import DEFAULT_CLOCK, CancelToken, Clock, interruptible_sleep
from .decision.engine import Decision, DecisionContext, decide
from .decision.hotbar import HotbarContext, HotbarState, slots_to_use
from .game.hive import BeeInfo, HiveProfile
from .game.rules import RBCRules
from .game.screens import ScreenState
from .game.state import DIALOGS, GameState, RunState

SCREEN_STATE = {
    "quest_choice": AgentState.RBC_SELECTING_QUEST,
    "bee_pick": AgentState.RBC_MANAGING_BEES,
    "upgrade_shop": AgentState.RBC_SELECTING_UPGRADES,
    "in_round": AgentState.RBC_FARMING,
    "unknown": AgentState.WAITING_FOR_RBC,
}

_VERB = {"choose_quest": "choose", "reroll_quest": "use the", "pick_bee": "pick", "buy_upgrade": "buy",
         "lock_upgrade": "lock", "press_slot": "press hotbar slot"}


def describe_decision(d: Decision) -> str:
    if d.action == "wait":
        return f"Waiting: {d.reason}"
    text = "Would start the round" if d.action == "start_round" else f"Would {_VERB[d.action]} {d.what}"
    return text + (f" ({d.reason})" if d.reason else "")


class Eyes(Protocol):
    def read_state(self) -> ScreenState: ...


class Log(Protocol):
    def state(self, msg: str) -> None: ...
    def decision(self, msg: str) -> None: ...
    def recovery(self, msg: str) -> None: ...


class DryRunActor:
    """Logs what the agent would do and touches nothing."""

    live = False

    def __init__(self, log: Log):
        self.log = log

    def act(self, d: Decision) -> None:
        self.log.decision(describe_decision(d))


class Agent:
    def __init__(self, cfg: AppConfig, rules: RBCRules, catalog: dict[str, BeeInfo], eyes: Eyes,
                 actor, log: Log, clock: Clock = DEFAULT_CLOCK):
        self.cfg, self.eyes, self.actor, self.log, self._clock = cfg, eyes, actor, log, clock
        self.run = RunState(cfg.detection, clock)
        self.machine = StateMachine(cfg, clock)
        self.ctx = DecisionContext(cfg, rules, catalog, HiveProfile.from_config(cfg.hive, catalog))
        self.hotbar = HotbarState()
        self._decided: tuple | None = None          # screen content the last decisions were for
        self._round_first_seen: tuple[int | None, float] = (None, 0.0)

    def step(self) -> GameState:
        for event in self.run.observe(self.eyes.read_state()):
            self.log.state(event)
        gs = self.run.snapshot()
        if self.machine.go(SCREEN_STATE[gs.screen]):
            self._decided = None
        elif self.actor.live and self.machine.timed_out():
            limit = self.cfg.timeout_for(self.machine.state)
            self.log.recovery(f"{self.machine.state.value} took over {limit:.0f} s, trying again")
            self.machine.go(AgentState.RECOVERY)        # next step re-enters the screen's state and re-decides
            return gs
        if gs.screen in DIALOGS:
            content = (gs.screen, gs.round_no, gs.cogs, gs.quest_offers, gs.bee_offers, gs.shop)
            if content != self._decided:
                self._decided = content
                for d in decide(gs, self.ctx):
                    self.actor.act(d)
        elif gs.screen == "in_round":
            self._hotbar(gs)
        return gs

    def _hotbar(self, gs: GameState) -> None:
        now = self._clock.now()
        if self._round_first_seen[0] != gs.round_no:
            self._round_first_seen = (gs.round_no, now)
        just_started = now - self._round_first_seen[1] <= self.cfg.hotbar.round_start_window_s
        # Where the player is (field / hive) is unknown until Stage 7, so only time-based phases are used.
        ctx = HotbarContext(now, in_round=True, round_no=gs.round_no or 0,
                            phase="round_start" if just_started else "unknown", time_left_s=gs.time_left_s)
        for n in slots_to_use(self.cfg.hotbar, ctx, self.hotbar, self.cfg.drives, self.ctx.hive):
            slot = self.cfg.hotbar.slot(n)
            self.actor.act(Decision("press_slot", n, f"{n} ({slot.item})", slot.use_when))
            self.hotbar.record(n, now)      # also in a dry run, so cooldowns move as if pressed

    def run_until_stopped(self, token: CancelToken, on_step=None) -> None:
        """Loop until the token is stopped (raises AutomationCancelled)."""
        while True:
            token.check()
            gs = self.step()
            if on_step is not None:
                on_step(gs, self.machine.state)
            interruptible_sleep(1.0 / self.cfg.scan_rates.ocr_hz, token=token, clock=self._clock)
