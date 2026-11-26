import json

import pytest

from rbc_agent.core.config import AppConfig, ConfigManager, deep_merge, parse_config, to_dict
from rbc_agent.core.errors import ConfigError
from rbc_agent.core.states import AgentState, StrategyProfile


def test_defaults_are_valid():
    cfg = parse_config({})
    assert cfg == AppConfig()


def test_partial_override_keeps_other_defaults():
    cfg = parse_config({"pollen": {"must_return_above": 0.99}})
    assert cfg.pollen.must_return_above == 0.99
    assert cfg.pollen.continue_below == AppConfig().pollen.continue_below


def test_unknown_key_suggests_fix():
    with pytest.raises(ConfigError) as e:
        parse_config({"poln": {}})
    assert "did you mean 'pollen'" in str(e.value)


def test_wrong_type_reported():
    with pytest.raises(ConfigError) as e:
        parse_config({"detection": {"confirm_frames": "three"}})
    assert "detection.confirm_frames" in str(e.value)


def test_all_problems_reported_at_once():
    with pytest.raises(ConfigError) as e:
        parse_config({"pollen": {"continue_below": 0.95, "must_return_above": 0.5},
                      "hotkeys": {"pause": "F10"}})
    assert len(e.value.problems) >= 2


def test_threshold_order_enforced():
    with pytest.raises(ConfigError):
        parse_config({"pollen": {"continue_below": 0.95}})


def test_region_outside_window_rejected():
    with pytest.raises(ConfigError) as e:
        parse_config({"regions": {"pollen_bar": {"x": 0.9, "y": 0, "w": 0.5, "h": 0.1}}})
    assert "outside" in str(e.value)


def test_user_can_add_region():
    cfg = parse_config({"regions": {"cog_counter": {"x": 0.8, "y": 0.0, "w": 0.1, "h": 0.05}}})
    assert "cog_counter" in cfg.regions and "pollen_bar" in cfg.regions


def test_confidence_weights_must_sum_to_one():
    with pytest.raises(ConfigError) as e:
        parse_config({"detection": {"confidence_weights": {"rbc_end": {"round_ui_gone": 0.9}}}})
    assert "sum" in str(e.value)


def test_strategy_profile_enum_and_weights():
    cfg = parse_config({"strategy": {"profile": "RED"}})
    assert cfg.strategy.profile is StrategyProfile.RED
    assert cfg.strategy.active_weights().color_bias["red"] > 1.0
    with pytest.raises(ConfigError):
        parse_config({"strategy": {"profile": "PURPLE"}})


def test_custom_profile_override_merges_dicts():
    cfg = parse_config({"strategy": {"profile": "CUSTOM",
                                     "profiles": {"CUSTOM": {"tag_bias": {"token": 3.0}}}}})
    w = cfg.strategy.active_weights()
    assert w.tag_bias["token"] == 3.0 and w.tag_bias["pollen"] == 1.0


def test_timeouts():
    cfg = parse_config({"timeouts_s": {"WAITING_FOR_RBC": 12}})
    assert cfg.timeout_for(AgentState.WAITING_FOR_RBC) == 12.0
    assert cfg.timeout_for(AgentState.PAUSED) is None
    with pytest.raises(ConfigError):
        parse_config({"timeouts_s": {"PAUSED": 5}})
    with pytest.raises(ConfigError):
        parse_config({"timeouts_s": {"NOT_A_STATE": 5}})


def test_hotkeys_must_be_distinct():
    with pytest.raises(ConfigError):
        parse_config({"hotkeys": {"resume": "F8"}})


def test_config_is_immutable():
    cfg = parse_config({})
    with pytest.raises(Exception):
        cfg.pollen.must_return_above = 0.5  # type: ignore[misc]


def test_roundtrip_to_dict():
    cfg = parse_config({"strategy": {"profile": "BLUE"}})
    assert parse_config(to_dict(cfg)) == cfg


def test_deep_merge_does_not_mutate():
    a = {"x": {"y": 1}}
    b = {"x": {"z": 2}}
    m = deep_merge(a, b)
    assert m == {"x": {"y": 1, "z": 2}} and a == {"x": {"y": 1}}


def test_manager_reload_keeps_old_config_on_error(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"run": {"max_runs": 5}}))
    mgr = ConfigManager(user_file=f, default_file=tmp_path / "d.json")
    seen = []
    mgr.subscribe(seen.append)
    assert mgr.load().run.max_runs == 5

    f.write_text("{ broken json")
    with pytest.raises(ConfigError) as e:
        mgr.reload()
    assert "line 1" in str(e.value)
    assert mgr.config.run.max_runs == 5          # old snapshot still active
    assert len(seen) == 1                        # listeners only on success


def test_manager_ignores_comment_keys(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"_comment": "my notes", "debug": {"enabled": True}}))
    assert ConfigManager(user_file=f).load().debug.enabled is True


def test_write_default_file_matches_defaults(tmp_path):
    mgr = ConfigManager(user_file=tmp_path / "u.json", default_file=tmp_path / "d.json")
    path = mgr.write_default_file()
    assert parse_config(json.loads(path.read_text())) == AppConfig()
