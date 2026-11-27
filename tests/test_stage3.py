"""Stage 3: finding the RBC panels, OCR, and turning screen text into game values.

The fixture images in tests/fixtures/screens are real 1920x1080 screenshots
(other players' names blacked out). Tests that run Tesseract are skipped when
it isn't installed; the GitHub build installs it, so they always run there.
"""

from pathlib import Path

import numpy as np
import pytest

from rbc_agent.core.config import QuestConfig, parse_config
from rbc_agent.core.errors import ConfigError
from rbc_agent.decision.picks import QuestCard, pick_quest
from rbc_agent.game.hive import HiveProfile, load_catalog
from rbc_agent.game.rules import RBCRules
from rbc_agent.game.screens import (QuestOffer, last_int, parse_amount, parse_bee_list,
                                    parse_objectives, parse_reroll, parse_round, parse_screen,
                                    parse_shop_row, parse_time_left)
from rbc_agent.perception.capture import FakeBackend, ScreenCapture
from rbc_agent.perception.ocr import find_tesseract
from rbc_agent.perception.screen_reader import ScreenReader, find_anchor, load_layout
from rbc_agent.perception.window import Rect, WindowInfo
from rbc_agent.ui.screen_report import describe

PIL = pytest.importorskip("PIL.Image")
FIX = Path(__file__).parent / "fixtures" / "screens"
RULES = RBCRules.load()
CAT = load_catalog()
CFG = parse_config({})
LAYOUT = load_layout()
needs_tesseract = pytest.mark.skipif(find_tesseract() is None, reason="Tesseract not installed")


def shot(name: str, size: tuple[int, int] | None = None) -> np.ndarray:
    im = PIL.open(FIX / f"{name}.png").convert("RGB")
    if size:
        im = im.resize(size, PIL.LANCZOS)
    return np.asarray(im)[..., ::-1].copy()            # BGR, like mss


# ------------------------------------------------------------------ text parsing (no OCR)
def test_numbers_from_messy_text():
    assert last_int("COGS: 21") == 21 and last_int("CO65:21") == 21 and last_int("Cogs: 1O") == 10
    assert last_int("nothing") is None
    assert parse_amount("20,000") == 20000 and parse_amount("1.5m") == 1_500_000
    assert parse_round("Robo Bear Challenge:/Round|1") == 1 and parse_round("Round)/2") == 2
    assert parse_time_left("ROUND: 2 TIME: 4:57") == 297 and parse_time_left("ROUND: 2") is None
    assert parse_reroll("| Reroll (1 Remaining)") == (1, None)
    assert parse_reroll("‘Reroll (Cost: 4 Cogs)") == (None, 4)


def test_quest_objectives_as_ocr_reads_them():
    objs = parse_objectives("« Collect 20,000 Red\nPollen.\ne Collect 20,000 Blue\nPollen.", RULES)
    assert [(o.type, o.amount, o.color) for o in objs] == [
        ("collect_color_pollen", 20000, "red"), ("collect_color_pollen", 20000, "blue")]
    objs = parse_objectives("* Make 60,000 Honey.\n° Collect 30,000 Red\nPollen.", RULES)
    assert [o.type for o in objs] == ["convert_pollen", "collect_color_pollen"]
    # Plain "Collect N Pollen" (round HUD), also when OCR misreads pollen as "potten".
    for text in ("Collect 120,000 Pollen.", "Collect 120,000 Potten."):
        (o,) = parse_objectives(text, RULES)
        assert o.type == "collect_pollen" and o.amount == 120000
    (o,) = parse_objectives("Collect 5k Pollen from the Pine Tree Forest", RULES)
    assert o.type == "collect_field_pollen" and o.place == "Pine Tree Forest" and o.amount == 5000


def test_shop_rows_with_mangled_level_text():
    o = parse_shop_row("[Overfit: Red] tv. i (max 102) Cost: 8 Cogs", "x1.25 Red Pollen", RULES)
    assert (o.upgrade_id, o.price) == ("overfit_red", 8)
    o = parse_shop_row("-(Nullify] LVL 1 (Max 2) Cost: 10 Cogs", "", RULES)
    assert (o.name, o.upgrade_id, o.price, o.level) == ("Nullify", "nullify", 10, 1)
    assert parse_shop_row("[Dynamq] Lvt 1 (Max 10) Cost: 11 Cogs", "", RULES).upgrade_id == "dynamo"
    assert parse_shop_row("[Not An Upgrade] Cost: 3 Cogs", "", RULES).upgrade_id is None
    # Halfway between two colours: skip rather than guess the wrong one.
    assert RULES.upgrade_by_name("Overfit: Bled") is None
    assert RULES.upgrade_by_name("0verfit: Blue").id == "overfit_blue"
    assert parse_shop_row("ns", "nn", RULES) is None
    assert parse_shop_row("[Crypto] Cost: 9 Cogs", "+3 Cogs Per Round, x0.8 Capacity |", RULES).effects ==         "+3 Cogs Per Round, x0.8 Capacity"


def test_bee_list_variable_length():
    text = ("OS\n* Vector Bee (Lvl 22)\nBeequip: None, Mutation: +4\nMovespeed !\n"
            "X Ninja Bee (Lvl 21)\nBeequip: None, Mutation: None\n—$_$__ a\n")
    bees = parse_bee_list(text, CAT)
    assert [(b.name, b.level, b.mutation, b.known) for b in bees] == [
        ("Vector", 22, "+4 Movespeed", True), ("Ninja", 21, "", True)]
    assert parse_bee_list("", CAT) == []


def test_quest_pick_uses_worst_objective_and_reward():
    offers = [parse_objectives("Collect 20,000 Red Pollen. Collect 20,000 Blue Pollen.", RULES),
              parse_objectives("Make 60,000 Honey. Collect 30,000 Red Pollen.", RULES)]
    cards = [QuestCard.from_offer(QuestOffer(tuple(o), r, "")) for o, r in zip(offers, (10, 13))]
    assert cards[1].parts == ("convert_pollen", "collect_color_pollen") and cards[1].reward == 13
    hive = HiveProfile.from_config(CFG.hive, CAT)
    # Quest B pays more but includes "make honey", which ranks last by default.
    assert pick_quest(cards, QuestConfig(), hive, RULES.fields, free_reroll_left=False).index == 0
    # Same types on both cards: the bigger reward wins.
    same = [QuestCard("collect_pollen", 9e9, reward=5), QuestCard("collect_pollen", 1, reward=8)]
    assert pick_quest(same, QuestConfig(), hive, {}, free_reroll_left=False).index == 1


# ------------------------------------------------------------------ finding the panels (no OCR)
@pytest.mark.parametrize("name", ["quest_choice", "upgrade_shop", "bee_pick"])
def test_dialog_anchor_found_and_scales(name):
    d = LAYOUT["dialog"]
    a = find_anchor(shot(name), d["search"], d["aspect"], CFG.ocr)
    assert (a.left, a.top, a.right, a.bottom) == tuple(d["anchor"])
    small = find_anchor(shot(name, (1280, 720)), d["search"], d["aspect"], CFG.ocr)
    assert abs(small.width - 528 * 1280 / 1916) <= 3


def test_hud_anchor_and_no_false_dialog():
    h, d = LAYOUT["hud"], LAYOUT["dialog"]
    img = shot("round_hud")
    a = find_anchor(img, h["search"], h["aspect"], CFG.ocr)
    assert (a.left, a.top, a.right, a.bottom) == tuple(h["anchor"])
    assert find_anchor(img, d["search"], d["aspect"], CFG.ocr) is None     # green floor isn't a dialog
    assert find_anchor(np.zeros((720, 1280, 3), np.uint8), d["search"], d["aspect"], CFG.ocr) is None


class NoOcr:
    def read(self, img, *, mode="gray", psm=6):
        return ""


def test_reader_unknown_without_panels():
    assert ScreenReader(NoOcr(), CFG.ocr, LAYOUT).read(np.zeros((720, 1280, 3), np.uint8)).kind == "unknown"


# ------------------------------------------------------------------ real OCR on the fixtures
@pytest.fixture(scope="module")
def read():
    from rbc_agent.perception.ocr import Tesseract
    reader = ScreenReader(Tesseract(CFG.ocr), CFG.ocr, LAYOUT)

    def go(name, size=None):
        raw = reader.read(shot(name, size))
        return parse_screen(raw.kind, raw.texts, raw.shop_rows, RULES, CAT)
    return go


@needs_tesseract
def test_ocr_quest_choice(read):
    st = read("quest_choice")
    assert (st.kind, st.round_no, st.cogs, st.free_rerolls) == ("quest_choice", 1, 10, 1)
    assert [q.reward for q in st.quests] == [10, 13]
    assert [[(o.type, o.amount, o.color) for o in q.objectives] for q in st.quests] == [
        [("collect_color_pollen", 20000, "red"), ("collect_color_pollen", 20000, "blue")],
        [("convert_pollen", 60000, ""), ("collect_color_pollen", 30000, "red")]]
    assert st.problems == []
    assert any(line.startswith("Bot would choose: Quest A") for line in describe(st, CFG, RULES, CAT))


@needs_tesseract
def test_ocr_upgrade_shop(read):
    st = read("upgrade_shop")
    assert (st.kind, st.round_no, st.cogs, st.reroll_cost) == ("upgrade_shop", 1, 10, 4)
    assert [(o.upgrade_id, o.price) for o in st.shop] == [("overfit_red", 8), ("nullify", 10), ("dynamo", 11)]
    assert st.shop[0].effects == "x1.25 Red Pollen, x0.9 Capacity"
    assert st.problems == []


@needs_tesseract
def test_ocr_bee_pick(read):
    st = read("bee_pick")
    assert (st.kind, st.round_no, st.cogs, st.reroll_cost) == ("bee_pick", 2, 21, 4)
    assert [(b.name, b.level, b.mutation) for b in st.bees] == [
        ("Vector", 22, "+4 Movespeed"), ("Ninja", 21, ""), ("Vector", 22, "+4 Movespeed")]
    assert st.problems == []


@needs_tesseract
def test_ocr_round_hud(read):
    st = read("round_hud")
    assert (st.kind, st.round_no, st.time_left_s, st.cogs) == ("in_round", 2, 297, 21)
    assert [(o.type, o.amount) for o in st.objectives] == [("collect_pollen", 120000)]


@needs_tesseract
@pytest.mark.parametrize("name", ["quest_choice", "upgrade_shop", "round_hud"])
def test_ocr_smaller_window(read, name):
    """Same screens shrunk to 1280x720: the anchor moves the boxes, values stay the same."""
    big, small = read(name), read(name, (1280, 720))
    assert (small.kind, small.round_no, small.cogs) == (big.kind, big.round_no, big.cogs)


# ------------------------------------------------------------------ capture cleanup + preflight
def test_capture_close_frees_backend():
    class Closable(FakeBackend):
        closed = 0

        def close(self):
            self.closed += 1
    be = Closable(np.zeros((10, 10, 3), np.uint8))
    cap = ScreenCapture(CFG, be, lambda t: WindowInfo(1, "Roblox", 10, 10, True, False, Rect(0, 0, 10, 10)))
    cap.close()
    assert be.closed == 1
    ScreenCapture(CFG, FakeBackend(np.zeros((1, 1, 3), np.uint8))).close()   # no close(): fine


def test_preflight_reports_tesseract():
    from rbc_agent.core.preflight import run_preflight
    checks = {c.name: c for c in run_preflight(parse_config({"ocr": {"tesseract_path": "C:/nope/t.exe"}}),
                                               find_window=lambda t: None)}
    assert not checks["Tesseract OCR"].ok and "UB-Mannheim" in checks["Tesseract OCR"].detail
    assert "Calibration" not in checks
    with pytest.raises(ConfigError):
        parse_config({"ocr": {"upscale": 9}})


# ------------------------------------------------------------------ live 1920x1009 window (title bar visible)
@needs_tesseract
def test_ocr_real_window_1009(read):
    q = read("quest_choice_1009")
    assert (q.kind, q.round_no, q.cogs, q.free_rerolls) == ("quest_choice", 3, 34, 1)
    assert [(q2.reward, [(o.type, o.amount, o.color) for o in q2.objectives]) for q2 in q.quests] == [
        (11, [("collect_color_pollen", 187500, "red")]), (11, [("collect_pollen", 225000, "")])]
    b = read("bee_pick_1009")
    assert (b.round_no, b.cogs, b.reroll_cost) == (3, 34, 5)
    assert [(x.name, x.level, x.mutation) for x in b.bees] == [
        ("Spicy", 21, "+3% Ability Rate"), ("Spicy", 21, "+3% Ability Rate"), ("Precise", 22, "+5 Movespeed")]
    s = read("upgrade_shop_1009")
    assert [(o.upgrade_id, o.price) for o in s.shop] == [("overfit_blue", 8), ("credit", 7), ("crypto", 9)]
    assert s.reroll_cost == 5


def test_shop_skips_other_colour_for_mono_hive():
    from rbc_agent.core.config import UpgradeConfig
    from rbc_agent.decision.picks import ShopItem, plan_shop
    red = HiveProfile.from_config(parse_config({"hive": {"color_override": "red"}}).hive, CAT)
    items = [ShopItem("overfit_blue", 8), ShopItem("credit", 7), ShopItem("crypto", 9)]
    w = CFG.strategy.active_weights()
    assert "overfit_blue" not in plan_shop(items, 34, RULES.upgrades, UpgradeConfig(), w, red).buy
    # Off with the setting, or when the user put it on the priority list.
    assert "overfit_blue" in plan_shop(items, 34, RULES.upgrades, UpgradeConfig(skip_off_color=False), w, red).buy
    assert "overfit_blue" in plan_shop(items, 34, RULES.upgrades, UpgradeConfig(priority=["overfit_blue"]), w, red).buy
