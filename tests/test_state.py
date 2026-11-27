"""Stage 4: RunState keeps a correct picture of the run from noisy screen reads."""

from rbc_agent.core.config import DetectionConfig
from rbc_agent.core.timing import FakeClock
from rbc_agent.game.screens import Objective, QuestOffer, ScreenState
from rbc_agent.game.state import RunState

CFG = DetectionConfig(confirm_frames=3, jump_accept_reads=4, unknown_state_grace_s=4.0)
RED = Objective("collect_color_pollen", 187500, "red", text="collect 187,500 red pollen")
ANY = Objective("collect_pollen", 225000, text="collect 225,000 pollen")


def quest(rnd, cogs=34, rerolls=1):
    return ScreenState("quest_choice", round_no=rnd, cogs=cogs, free_rerolls=rerolls,
                       quests=[QuestOffer((RED,), 11, ""), QuestOffer((ANY,), 11, "")])


def hud(rnd, t=297, objs=(ANY,), cogs=34):
    return ScreenState("in_round", round_no=rnd, cogs=cogs, time_left_s=t, objectives=list(objs))


def feed(rs, *reads, clock=None):
    events = []
    for st in reads:
        events += rs.observe(st)
        if clock:
            clock.advance(1.0)
    return events


def test_needs_confirm_frames_and_starts_mid_run():
    rs = RunState(CFG, FakeClock())
    assert feed(rs, quest(3), quest(3)) == [] and rs.round_no is None
    ev = feed(rs, quest(3))
    assert ev[:2] == ["Round 3", "Screen: quest choice"] and rs.cogs == 34
    gs = rs.snapshot()
    assert (gs.screen, gs.round_no, gs.free_reroll_left, gs.confidence) == ("quest_choice", 3, True, 1.0)


def test_one_misread_changes_nothing():
    rs = RunState(CFG, FakeClock())
    feed(rs, quest(3), quest(3), quest(3))
    feed(rs, quest(8), quest(3), quest(3))         # one slip breaks the streak only
    assert rs.round_no == 3
    assert feed(rs, quest(3, cogs=None)) == []     # None never overwrites (and isn't confirmed)
    assert rs.cogs == 34


def test_round_jump_rejected_then_accepted_if_it_persists():
    rs = RunState(CFG, FakeClock())
    feed(rs, *[hud(3)] * 3)
    ev = feed(rs, *[hud(9)] * 3)                   # confirmed jump 3 -> 9: rejected
    assert rs.round_no == 3 and any("Ignored round 9" in e for e in ev)
    feed(rs, *[hud(9)] * 3)                        # still read as 9 -> believe it
    assert rs.round_no == 9


def test_next_round_and_new_run():
    rs = RunState(CFG, FakeClock())
    feed(rs, *[hud(3)] * 3)
    assert "Round 4" in feed(rs, *[quest(4)] * 3)
    ev = feed(rs, *[quest(1, cogs=10)] * 3)
    assert ev[:2] == ["New RBC run", "Round 1"] and rs.cogs == 10


def test_quest_taken_is_matched_to_its_card():
    rs = RunState(CFG, FakeClock())
    feed(rs, *[quest(3)] * 3)
    ev = feed(rs, *[hud(3, objs=(ANY,))] * 3)
    assert "Quest: collect 225,000 pollen (Quest B)" in ev
    assert rs.snapshot().time_left_s == 297
    feed(rs, *[hud(3, t=999)] * 3)                 # impossible time: ignored
    assert rs.time_left_s == 297


def test_short_unknown_gap_keeps_state_long_gap_reports_it():
    clock = FakeClock()
    rs = RunState(CFG, clock)
    feed(rs, *[hud(3)] * 3, clock=clock)
    unknown = ScreenState("unknown")
    assert feed(rs, unknown, unknown, clock=clock) == []      # 2 s: still in round
    assert rs.snapshot().screen == "in_round"
    assert feed(rs, unknown, unknown, unknown, clock=clock) == ["No RBC screen visible"]
    gs = rs.snapshot()
    assert gs.screen == "unknown" and gs.round_no == 3 and gs.confidence == 0.0


def test_recorded_actions_show_in_snapshot():
    rs = RunState(CFG, FakeClock())
    rs.record_bee("Spicy")
    rs.record_upgrade("credit")
    rs.record_upgrade("credit")
    rs.record_free_reroll_used()
    gs = rs.snapshot()
    assert gs.team == ("Spicy",) and gs.upgrades == (("credit", 2),) and gs.free_reroll_left is False
