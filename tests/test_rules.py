import pytest

from rbc_agent.core.config import AppConfig
from rbc_agent.core.errors import ConfigError
from rbc_agent.game.rules import RBCRules


@pytest.fixture(scope="module")
def rules():
    return RBCRules.load()


def test_shipped_rules_load(rules):
    assert rules.total_rounds == 25
    assert rules.round_time_limit_s == 300
    assert len(rules.upgrades) >= 60


def test_every_upgrade_tag_has_a_config_bias(rules):
    biases = set(AppConfig().strategy.profiles["BALANCED"].tag_bias)
    tags = {t for u in rules.upgrades.values() for t in u.tags}
    assert tags <= biases, f"add to strategy tag_bias: {tags - biases}"


def test_ocr_lookup_is_tolerant(rules):
    assert rules.upgrade_by_name("ssd : blue").id == "ssd_blue"
    assert rules.upgrade_by_name("STACK OVERFLOW").cap == 1
    assert rules.upgrade_by_name("nonsense") is None


def test_field_upgrades_point_at_real_fields(rules):
    router = rules.upgrades["router"]
    assert {e.field for e in router.effects} == {"Strawberry Field", "Spider Field",
                                                 "Bamboo Field", "Pineapple Patch"}


def test_rarity_weight_interpolates(rules):
    assert rules.rarity_weight("legendary", 1) == 1
    assert rules.rarity_weight("legendary", 25) == 10
    assert rules.rarity_weight("common", 13) == pytest.approx(70)


def test_bad_rules_rejected():
    raw = {"challenge": {}, "fields": {}, "upgrade_rarity_weights": {},
           "upgrades": [{"id": "x", "rarity": "mythic", "cap": 0, "effects": [{"op": "pow"}]}]}
    with pytest.raises(ConfigError) as e:
        RBCRules(raw)
    assert len(e.value.problems) == 3
