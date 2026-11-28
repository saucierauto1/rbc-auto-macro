"""Stage 4: what the agent knows about the current RBC run.

``RunState`` takes one ``ScreenState`` per screen read and keeps a memory of
the run. A read only counts once ``detection.confirm_frames`` reads in a row
agree (same screen, same round, same Cogs), so one OCR slip changes nothing.
Impossible values are ignored: the round only stays or goes up by one, unless
round 1 shows up on the quest screen (a new run) or the same jump keeps being
read ``detection.jump_accept_reads`` times (then the old value was the slip).

``snapshot()`` returns an immutable ``GameState`` for the decision layer.
No screen or keyboard access here; tests feed it ScreenState lists.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from ..core.config import DetectionConfig
from ..core.timing import DEFAULT_CLOCK, Clock
from .screens import BeeOffer, Objective, QuestOffer, ScreenState, ShopOffer

DIALOGS = ("quest_choice", "bee_pick", "upgrade_shop")
MAX_ROUND_TIME_S = 300


@dataclass(frozen=True)
class GameState:
    screen: str                          # confirmed screen: quest_choice, bee_pick, upgrade_shop, in_round, unknown
    round_no: int | None
    cogs: int | None
    time_left_s: int | None
    objectives: tuple[Objective, ...]    # quest being played this round
    quest_offers: tuple[QuestOffer, ...]
    shop: tuple[ShopOffer, ...]
    bee_offers: tuple[BeeOffer, ...]
    free_reroll_left: bool | None
    reroll_cost: int | None
    team: tuple[str, ...]                # bees picked this run (recorded by the agent)
    upgrades: tuple[tuple[str, int], ...]  # (upgrade id, level) bought this run
    confidence: float                    # share of recent reads that agree with `screen`
    seen_at: float                       # clock time of the last confirmed read


def _key(st: ScreenState) -> tuple:
    """What must match between reads. Parsed values, never raw text; time changes every read.

    The offers are part of it: two bee screens in one round have the same round and
    Cogs, so without them one garbled read would replace the confirmed bees.
    """
    if st.kind == "quest_choice":
        offers = tuple((tuple((o.type, o.amount, o.color, o.place, o.mob) for o in q.objectives), q.reward)
                       for q in st.quests)
    elif st.kind == "bee_pick":
        offers = tuple((b.name, b.level, b.mutation) for b in st.bees)
    elif st.kind == "upgrade_shop":
        offers = tuple((o.upgrade_id or o.name, o.price) for o in st.shop)
    elif st.kind == "in_round":
        offers = tuple((o.type, o.amount, o.color, o.place, o.mob) for o in st.objectives)
    else:
        offers = ()
    return (st.kind, st.round_no, st.cogs if st.kind in DIALOGS else None, offers)


class RunState:
    def __init__(self, cfg: DetectionConfig, clock: Clock = DEFAULT_CLOCK):
        self.cfg = cfg
        self._clock = clock
        self._recent: deque[tuple] = deque(maxlen=cfg.confirm_frames)
        self._last_known_at = -1e9
        self._jump: tuple[int, int] = (0, 0)       # (round value, reads in a row)
        self.reset()

    def reset(self) -> None:
        self.screen = "unknown"
        self.round_no: int | None = None
        self.cogs: int | None = None
        self.time_left_s: int | None = None
        self.objectives: tuple[Objective, ...] = ()
        self.quest_offers: tuple[QuestOffer, ...] = ()
        self.shop: tuple[ShopOffer, ...] = ()
        self.bee_offers: tuple[BeeOffer, ...] = ()
        self.free_reroll_left: bool | None = None
        self.reroll_cost: int | None = None
        self.team: list[str] = []
        self.upgrades: dict[str, int] = {}
        self.seen_at = 0.0

    # -- reading the screen ---------------------------------------------------
    def observe(self, st: ScreenState) -> list[str]:
        """Feed one screen read. Returns what changed, as log lines."""
        now = self._clock.now()
        if st.kind == "unknown":
            # Screen changes and loading are short; only give up after the grace time.
            self._recent.clear()
            if self.screen != "unknown" and now - self._last_known_at > self.cfg.unknown_state_grace_s:
                self.screen = "unknown"
                return ["No RBC screen visible"]
            return []
        self._last_known_at = now
        self._recent.append(_key(st))
        if len(self._recent) < self._recent.maxlen or len(set(self._recent)) > 1:
            return []                                   # not confirmed yet
        return self._apply(st, now)

    def _apply(self, st: ScreenState, now: float) -> list[str]:
        events: list[str] = []
        if st.round_no is not None and st.round_no != self.round_no:
            verdict = self._judge_round(st)
            if verdict == "reject":
                return [f"Ignored round {st.round_no} (still round {self.round_no}): looks like a misread"] \
                    if self._jump[1] == 1 else []
            if verdict == "new_run":
                self.reset()
                events.append("New RBC run")
            self.round_no = st.round_no
            self.objectives = ()
            self.time_left_s = None
            self.reroll_cost = None
            events.append(f"Round {st.round_no}")
        self._jump = (0, 0)
        if st.kind != self.screen:
            self.screen = st.kind
            events.append(f"Screen: {st.kind.replace('_', ' ')}")
        if st.cogs is not None and st.cogs != self.cogs:
            self.cogs = st.cogs
            events.append(f"Cogs: {st.cogs}")
        if st.kind == "quest_choice":
            self.quest_offers = tuple(st.quests)
            if st.free_rerolls is not None:
                self.free_reroll_left = st.free_rerolls > 0
        elif st.kind == "bee_pick":
            self.bee_offers = tuple(st.bees)
        elif st.kind == "upgrade_shop":
            self.shop = tuple(st.shop)
        if st.kind in ("bee_pick", "upgrade_shop") and st.reroll_cost is not None:
            self.reroll_cost = st.reroll_cost
        if st.kind == "in_round":
            if st.time_left_s is not None and 0 <= st.time_left_s <= MAX_ROUND_TIME_S:
                self.time_left_s = st.time_left_s
            if st.objectives and tuple(st.objectives) != self.objectives:
                self.objectives = tuple(st.objectives)
                events.append("Quest: " + "; ".join(o.text for o in st.objectives) + self._which_card())
        self.seen_at = now
        return events

    def _judge_round(self, st: ScreenState) -> str:
        new, old = st.round_no, self.round_no
        if old is None or new == old + 1:
            return "accept"                             # first read (may start mid-run) or next round
        if new == 1 and st.kind == "quest_choice":
            return "new_run"
        same = self._jump[0] == new
        self._jump = (new, self._jump[1] + 1 if same else 1)
        return "accept" if self._jump[1] >= self.cfg.jump_accept_reads else "reject"

    def _which_card(self) -> str:
        texts = {o.text for o in self.objectives}
        for side, offer in zip("AB", self.quest_offers):
            if texts == {o.text for o in offer.objectives}:
                return f" (Quest {side})"
        return ""

    # -- what the agent did ---------------------------------------------------
    def record_bee(self, name: str) -> None:
        self.team.append(name)

    def record_upgrade(self, upgrade_id: str) -> None:
        self.upgrades[upgrade_id] = self.upgrades.get(upgrade_id, 0) + 1

    def record_free_reroll_used(self) -> None:
        self.free_reroll_left = False

    # -- snapshot -------------------------------------------------------------
    def snapshot(self) -> GameState:
        agree = sum(1 for k in self._recent if k and k[0] == self.screen)
        conf = agree / self._recent.maxlen if self.screen != "unknown" else 0.0
        return GameState(self.screen, self.round_no, self.cogs, self.time_left_s, self.objectives,
                         self.quest_offers, self.shop, self.bee_offers, self.free_reroll_left,
                         self.reroll_cost, tuple(self.team), tuple(self.upgrades.items()), conf,
                         self.seen_at)
