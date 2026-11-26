from rbc_agent.core.config import (BeePickConfig, DriveConfig, HiveConfig, QuestConfig,
                                  UpgradeConfig, parse_config)
from rbc_agent.core.preflight import can_start, run_preflight
from rbc_agent.decision.drives import plan_drive
from rbc_agent.decision.picks import (OfferedBee, QuestCard, ShopItem, pick_bees, pick_quest,
                                      plan_shop)
from rbc_agent.game.hive import HiveProfile, dominant_color, load_catalog, normalize_bee
from rbc_agent.game.rules import RBCRules
from rbc_agent.perception.window import WindowInfo

CAT = load_catalog()


def hive(*bees, override=""):
    cfg = parse_config({"hive": {"color_override": override,
                                 "bees": [dict(bee=b, count=c) for b, c in bees]}}).hive
    return HiveProfile.from_config(cfg, CAT)


BLUE_DIGI = hive(("Digital", 1), ("Bubble", 20), ("Tadpole", 5), ("Frosty", 10), ("Basic", 3))
BLUE_NO_DIGI = hive(("Bubble", 20), ("Tadpole", 5), ("Frosty", 10))


# ------------------------------------------------------------------ hive
def test_catalog_and_normalize():
    assert normalize_bee(" digital bee ") == "Digital"
    assert CAT["Digital"].rarity == "event" and len(CAT) >= 45


def test_hive_color():
    assert BLUE_DIGI.color == "blue" and BLUE_DIGI.has_digital_bee
    assert hive(("Rage", 10), ("Bubble", 10)).color == "mixed"
    assert hive(("Bubble", 1), override="red").color == "red"
    assert dominant_color({}) == "mixed"


def test_hive_config_rejects_duplicates_and_bad_gifted():
    import pytest
    from rbc_agent.core.errors import ConfigError
    with pytest.raises(ConfigError):
        parse_config({"hive": {"bees": [{"bee": "Rad"}, {"bee": "rad"}]}})
    with pytest.raises(ConfigError):
        parse_config({"hive": {"bees": [{"bee": "Rad", "count": 1, "gifted": 2}]}})


# ------------------------------------------------------------------ drives
def drive(cfg=DriveConfig(), h=BLUE_DIGI, **kw):
    args = dict(round_no=3, quest_type="collect_color_pollen", cogs=50,
                offered={"blue": 10, "red": 10, "glitched": 15})
    args.update(kw)
    return plan_drive(cfg, h, **args)


def test_drive_only_with_digital_bee():
    d = drive(h=BLUE_NO_DIGI)
    assert not d.buy and "Digital Bee" in d.reason


def test_drive_auto_matches_hive_color_and_uses():
    d = drive()
    assert d.buy and d.drive == "blue" and d.use


def test_drive_mixed_hive_prefers_glitched():
    mixed = hive(("Digital", 1), ("Rage", 10), ("Bubble", 10))
    assert drive(h=mixed).drive == "glitched"


def test_drive_fallback_and_reserve():
    d = drive(offered={"red": 10})
    assert d.drive == "red"
    assert not drive(DriveConfig(min_cog_reserve=45)).buy


def test_drive_round_rules():
    assert not drive(DriveConfig(use_on="fixed_rounds_only"), round_no=4).buy
    assert drive(DriveConfig(use_on="fixed_rounds_only"), round_no=5).buy
    assert not drive(DriveConfig(use_on="pollen_quests_only"), quest_type="defeat_mobs").buy
    assert not drive(DriveConfig(max_per_run=2), used_this_run=2).buy
    assert not drive(DriveConfig(start_round=6)).buy
    assert not drive(DriveConfig(enabled=False)).buy


def test_drive_can_ignore_digital_requirement():
    assert drive(DriveConfig(only_with_digital_bee=False), h=BLUE_NO_DIGI).buy


# ------------------------------------------------------------------ bees
def test_bee_priority_order_and_avoid():
    cfg = BeePickConfig(priority=["Tadpole", "Bubble"], avoid=["Basic"])
    offered = [OfferedBee("Basic"), OfferedBee("Bubble"), OfferedBee("Tadpole"), OfferedBee("Rad")]
    r = pick_bees(offered, 2, cfg, BLUE_DIGI, CAT)
    assert [b.name for b in r.picks] == ["Tadpole", "Bubble"]


def test_bee_gifted_breaks_tie_and_reroll():
    cfg = BeePickConfig(priority=[], reroll_if_no_priority=True)
    r = pick_bees([OfferedBee("Rad"), OfferedBee("Rad", gifted=True)], 1, cfg, BLUE_DIGI, CAT,
                  can_reroll=True)
    assert r.picks[0].gifted and r.reroll


def test_bee_priority_and_avoid_overlap_rejected():
    assert BeePickConfig(priority=["Rad"], avoid=["rad"]).validate()


# ------------------------------------------------------------------ quests
def test_quest_priority_and_color_match():
    cfg = QuestConfig()
    cards = [QuestCard("collect_color_pollen", 1e6, color="red"),
             QuestCard("collect_color_pollen", 1e6, color="blue")]
    assert pick_quest(cards, cfg, BLUE_DIGI, {}, free_reroll_left=True).index == 1
    cards = [QuestCard("defeat_mobs", 10), QuestCard("collect_field_pollen", 1e6, field="Pine Tree Forest")]
    assert pick_quest(cards, cfg, BLUE_DIGI, {"Pine Tree Forest": "blue"}, free_reroll_left=False).index == 1


def test_quest_free_reroll():
    cards = [QuestCard("convert_pollen"), QuestCard("convert_pollen")]
    r = pick_quest(cards, QuestConfig(), BLUE_DIGI, {}, free_reroll_left=True)
    assert r.reroll
    assert not pick_quest(cards, QuestConfig(), BLUE_DIGI, {}, free_reroll_left=False).reroll


# ------------------------------------------------------------------ upgrades
def test_shop_priority_blacklist_reserve_lock():
    rules = RBCRules.load()
    ids = list(rules.upgrades)[:4]
    cfg = UpgradeConfig(priority=[ids[2]], blacklist=[ids[0]], min_cog_reserve=5)
    w = parse_config({}).strategy.active_weights()
    items = [ShopItem(i, 10) for i in ids]
    plan = plan_shop(items, 26, rules.upgrades, cfg, w, BLUE_DIGI)
    assert plan.buy[0] == ids[2] and ids[0] not in plan.buy and len(plan.buy) == 2
    poor = plan_shop(items, 3, rules.upgrades, cfg, w, BLUE_DIGI)
    assert poor.buy == [] and poor.lock == [ids[2]]
    assert plan_shop(items, 99, rules.upgrades, UpgradeConfig(auto_buy=False), w, BLUE_DIGI).buy == []


# ------------------------------------------------------------------ preflight
def test_preflight_explains_why_start_is_blocked():
    cfg = parse_config({"hive": {"bees": [{"bee": "Digital"}]}})
    checks = run_preflight(cfg, find_window=lambda _t: WindowInfo(1, "Roblox", 1920, 1080, True, False))
    names = {c.name: c for c in checks}
    assert names["Roblox window"].ok
    assert "will be bought" in names["Drives"].detail
    assert not can_start(checks)              # engine stages not built yet
    no_win = run_preflight(cfg, find_window=lambda _t: None)
    assert not {c.name: c for c in no_win}["Roblox window"].ok
