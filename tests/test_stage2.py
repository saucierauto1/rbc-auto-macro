import struct
import zlib

import numpy as np
import pytest

from rbc_agent.core.config import (CombatConfig, DriveConfig, HotbarConfig, HotbarSlot, Region,
                                   parse_config)
from rbc_agent.core.errors import ConfigError, WindowLostError
from rbc_agent.core.preflight import run_preflight
from rbc_agent.core.timing import FakeClock
from rbc_agent.decision.combat import Threat, golden_cogmower_fields, plan_dodge
from rbc_agent.decision.hotbar import HotbarContext, HotbarState, slots_to_use
from rbc_agent.game.hive import HiveProfile, load_catalog
from rbc_agent.game.rules import RBCRules
from rbc_agent.perception.capture import FakeBackend, ScreenCapture
from rbc_agent.perception.debug_dump import DebugDumper, draw_box, overlay_regions, write_png
from rbc_agent.perception.regions import template_scale, to_pixels
from rbc_agent.perception.window import Rect, WindowInfo, is_game_title

CAT = load_catalog()
CLIENT = Rect(100, 50, 1600, 900)


def win(client=CLIENT, minimized=False):
    return WindowInfo(1, "Roblox", client.width, client.height, True, minimized, client)


def screen():
    img = np.zeros((1200, 2000, 3), dtype=np.uint8)
    img[:, :, 0] = np.arange(2000) % 256          # x pattern in blue
    img[:, :, 1] = (np.arange(1200) % 256)[:, None]  # y pattern in green
    return img


# ------------------------------------------------------------------ window + regions
def test_game_title_filter():
    assert is_game_title("Roblox")
    assert not is_game_title("Roblox - Google Chrome")
    assert not is_game_title("RBC Agent 0.2.0 – Roblox helper")


def test_region_to_pixels_and_clamp():
    r = to_pixels(Region(0.5, 0.5, 0.25, 0.1), CLIENT)
    assert r == Rect(900, 500, 400, 90)
    edge = to_pixels(Region(0.99, 0.99, 0.01, 0.01), Rect(0, 0, 10, 10))
    assert edge.width >= 1 and edge.right <= 10
    assert template_scale(1440, 1080) == pytest.approx(1.3333, rel=1e-3)


# ------------------------------------------------------------------ capture
def test_capture_region_matches_screen_and_caches():
    cfg = parse_config({})
    clock = FakeClock()
    be = FakeBackend(screen())
    cap = ScreenCapture(cfg, backend=be, find_window=lambda _t: win(), clock=clock)
    f = cap.grab_region("pollen_bar")
    rect = cap.region_rect("pollen_bar")
    assert f.image.shape == (rect.height, rect.width, 3)
    assert f.image[0, 0, 0] == rect.left % 256 and f.image[0, 0, 1] == rect.top % 256
    cap.grab_region("pollen_bar")
    assert be.grabs == 1                      # served from cache
    clock.advance(0.2)
    cap.grab_region("pollen_bar")
    assert be.grabs == 2                      # cache expired
    cap.grab_region("pollen_bar", fresh=True)
    assert be.grabs == 3


def test_capture_window_lost_and_moved():
    cfg = parse_config({})
    clock = FakeClock()
    state = {"w": win()}
    cap = ScreenCapture(cfg, backend=FakeBackend(screen()), find_window=lambda _t: state["w"], clock=clock)
    cap.grab_client()
    state["w"] = win(Rect(0, 0, 1280, 720))
    clock.advance(1.0)
    assert cap.grab_client().image.shape == (720, 1280, 3)   # picked up the resize
    state["w"] = None
    clock.advance(1.0)
    with pytest.raises(WindowLostError):
        cap.grab_client()
    state["w"] = win(minimized=True)
    with pytest.raises(WindowLostError):
        cap.locate(force=True)
    with pytest.raises(KeyError):
        ScreenCapture(cfg, backend=FakeBackend(screen()), find_window=lambda _t: win()).region_rect("nope")


# ------------------------------------------------------------------ debug dumps
def _read_png(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    i, idat = 8, b""
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        tag = data[i + 4:i + 8]
        if tag == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h)]
    return np.frombuffer(b"".join(rows), dtype=np.uint8).reshape(h, w, 3)


def test_png_roundtrip_and_overlay(tmp_path):
    img = screen()[:40, :60].copy()
    rgb = _read_png(write_png(tmp_path / "a.png", img))
    assert np.array_equal(rgb, img[:, :, ::-1])
    draw_box(img, Rect(5, 5, 10, 10), (0, 0, 255), 1)
    assert tuple(img[5, 7]) == (0, 0, 255)
    cfg = parse_config({})
    big = np.zeros((900, 1600, 3), dtype=np.uint8)
    assert overlay_regions(big, cfg, CLIENT).any()


def test_dumper_prunes(tmp_path):
    cfg = parse_config({"debug": {"screenshot_dir": str(tmp_path), "max_screenshots": 3}})
    d = DebugDumper(cfg)
    for i in range(5):
        d.save(np.zeros((4, 4, 3), dtype=np.uint8), f"x{i}")
    assert len(list(tmp_path.glob("*.png"))) == 3


# ------------------------------------------------------------------ hotbar
def hive(*bees):
    return HiveProfile.from_config(parse_config({"hive": {"bees": [{"bee": b} for b in bees]}}).hive, CAT)


def ctx(**kw):
    base = dict(now=100.0, in_round=True, round_no=3, phase="in_field", backpack=0.5, time_left_s=200, mobs_visible=0)
    base.update(kw)
    return HotbarContext(**base)


def hb(*slots, **kw):
    return HotbarConfig(slots=list(slots), **kw)


def test_hotbar_conditions():
    cfg = hb(HotbarSlot(1, "Glitter", "in_field", every_s=900),
             HotbarSlot(2, "Micro-Converter", "backpack_full", every_s=5),
             HotbarSlot(3, "Stinger", "mobs_nearby", every_s=30))
    d, h = DriveConfig(), hive("Bubble")
    assert slots_to_use(cfg, ctx(), HotbarState(), d, h) == [1]
    assert slots_to_use(cfg, ctx(phase="at_hive", backpack=0.97), HotbarState(), d, h) == [2]
    assert slots_to_use(cfg, ctx(phase="travelling", mobs_visible=3), HotbarState(), d, h) == [3]
    assert slots_to_use(cfg, ctx(in_round=False), HotbarState(), d, h) == []


def test_hotbar_cooldown_gap_and_round_limit():
    cfg = hb(HotbarSlot(1, "Jelly Beans", "always", every_s=10, max_per_round=2), min_gap_s=0.4)
    st, d, h = HotbarState(), DriveConfig(), hive("Bubble")
    assert slots_to_use(cfg, ctx(now=0), st, d, h) == [1]
    st.record(1, 0)
    assert slots_to_use(cfg, ctx(now=5), st, d, h) == []          # cooling down
    assert slots_to_use(cfg, ctx(now=11), st, d, h) == [1]
    st.record(1, 11)
    assert slots_to_use(cfg, ctx(now=30), st, d, h) == []         # 2 per round used
    assert slots_to_use(cfg, ctx(now=31, round_no=4), st, d, h) == [1]   # new round resets


def test_hotbar_drive_needs_digital_bee_and_low_time():
    cfg = hb(HotbarSlot(4, "Blue Drive", "round_start", every_s=300),
             HotbarSlot(5, "Super Smoothie", "low_time", low_time_s=60, every_s=1200))
    d = DriveConfig()
    assert slots_to_use(cfg, ctx(phase="round_start"), HotbarState(), d, hive("Bubble")) == []
    assert slots_to_use(cfg, ctx(phase="round_start"), HotbarState(), d, hive("Digital")) == [4]
    assert slots_to_use(cfg, ctx(time_left_s=45), HotbarState(), d, hive("Bubble")) == [5]


def test_hotbar_config_validation():
    with pytest.raises(ConfigError):
        parse_config({"hotbar": {"slots": [{"slot": 1, "item": "A", "use_when": "always"},
                                           {"slot": 1, "item": "B", "use_when": "always"}]}})
    with pytest.raises(ConfigError):
        parse_config({"hotbar": {"slots": [{"slot": 9, "item": "A"}]}})
    with pytest.raises(ConfigError):
        parse_config({"hotbar": {"slots": [{"slot": 2, "use_when": "always"}]}})   # no item
    with pytest.raises(ConfigError):   # drive slot holds something else
        parse_config({"drives": {"hotbar_slot": 3},
                      "hotbar": {"slots": [{"slot": 3, "item": "Glitter", "use_when": "in_field"}]}})
    parse_config({"drives": {"hotbar_slot": 3},
                  "hotbar": {"slots": [{"slot": 3, "item": "Red Drive", "use_when": "round_start"}]}})


# ------------------------------------------------------------------ combat
def test_dodge_cogturret_in_line_sidesteps():
    turret = Threat("cogturret", 0.0, 5.0, heading=(0.0, -1.0), raised=True)   # in front, facing us
    h = plan_dodge([turret], CombatConfig())
    assert h.action == "sidestep" and abs(h.direction[1]) < 1e-9
    off = Threat("cogturret", 5.0, 5.0, heading=(0.0, -1.0), raised=True)        # not in line
    assert plan_dodge([off], CombatConfig(stand_near_mobs=False)).action == "none"
    hidden = Threat("cogturret", 0.0, 5.0, heading=(0.0, -1.0), raised=False)
    assert plan_dodge([hidden], CombatConfig(stand_near_mobs=False)).action == "none"


def test_dodge_mechsquito_and_toxin():
    m = [Threat("mechsquito", 3.0, 3.0)]
    assert plan_dodge(m, CombatConfig()).action == "circle"
    assert plan_dodge(m, CombatConfig(mechsquito_dodge="zigzag")).action == "zigzag"
    assert plan_dodge(m, CombatConfig(dodge_at_toxin_stacks=3), toxin_stacks=1).action != "circle"
    assert plan_dodge(m, CombatConfig(), toxin_stacks=9).action == "leave_field"
    assert plan_dodge(m, CombatConfig(dodge_enabled=False)).action == "none"


def test_dodge_cogmower_path_and_approach_priority():
    mower = Threat("cogmower", 0.0, 3.0, heading=(0.0, -1.0))
    assert plan_dodge([mower], CombatConfig()).action == "sidestep"
    far = [Threat("cogmower", 4.0, 0.5, heading=(0.0, 1.0)), Threat("cogturret", -5.0, 0.0, heading=(0.0, 1.0))]
    h = plan_dodge(far, CombatConfig())
    assert h.action == "approach" and h.direction[0] < 0                      # turret first
    h2 = plan_dodge(far, CombatConfig(prioritize_cogturrets=False))
    assert h2.direction[0] > 0


def test_golden_cogmower_fields():
    mobs = RBCRules.load().mobs
    assert "Clover Field" in golden_cogmower_fields(4, set(), mobs, CombatConfig())
    assert golden_cogmower_fields(4, {0}, mobs, CombatConfig()) == []
    assert golden_cogmower_fields(5, set(), mobs, CombatConfig()) == []
    assert golden_cogmower_fields(4, set(), mobs, CombatConfig(hunt_golden_cogmower=False)) == []


def test_rules_updated_from_wiki():
    r = RBCRules.load()
    assert r.fast_bonus_under_s == 225
    assert r.raw["drives"]["glitched"]["cogs"] == 100


def test_preflight_marks_capture_ready_and_warns_on_shape():
    cfg = parse_config({})
    checks = {c.name: c for c in run_preflight(cfg, find_window=lambda _t: win(Rect(0, 0, 1000, 1000)))}
    assert checks["Screen capture (Stage 2)"].ok
    assert "Window shape" in checks and not checks["Window shape"].blocking
