# RBC Agent

A state-aware Robo Bear Challenge agent for Bee Swarm Simulator that uses only
the screen and normal keyboard/mouse input. See **ARCHITECTURE.md** for the
full design, the state table and the capability matrix.

**Current stage: 2 of 16 (window + screen capture) + settings GUI and decision logic.**

> **Why START doesn't run anything yet:** this build has no screen capture, OCR,
> keyboard/mouse input or state machine (stages 2-8). START opens a pre-flight
> check that lists exactly what is ready and what is still missing. Everything you
> set in the tabs is saved to `config/config.json` and will be used once those stages land.

## Stage 2: screen capture
* `perception/window.py` finds the Roblox window (by title, or by the `RobloxPlayerBeta.exe`
  process), makes the app DPI-aware, and reads the client area's position, size and focus.
* `perception/capture.py` grabs the client area or any config region with `mss`, as BGR numpy
  arrays, with a short frame cache so detectors share one grab per tick.
* `perception/regions.py` maps normalised config regions to screen pixels.
* `perception/debug_dump.py` saves PNGs (with region overlays) and keeps `debug/` under
  `debug.max_screenshots`.
* **📸 Screenshot** on the Dashboard: counts down (Controls tab), then saves the game screen,
  a copy with every region outlined, and one image per region into `debug/`. Use it to
  send the screenshots needed for Stage 3.

## Control panel tabs
| Tab | What you set |
|---|---|
| Dashboard | Live status, START (pre-flight check), log |
| Hive | Your bees (count / gifted / level), detected hive colour, Digital Bee check |
| Bee Picks | Bees to pick first (ordered), bees to avoid, gifted / colour preference, rerolls |
| Quests & Fields | Quest priority, free-reroll rule, field bias + blacklist, field scoring weights |
| Upgrades | Priority upgrades (bought first, LOCKED if unaffordable), never-buy list, Cog reserve, rarity multipliers, strategy profile |
| Hotbar | What is in slots 1-7 and when to use it (round start, while farming, at hive, backpack full, mobs nearby, fixed rounds, time running out), cooldowns, per-round limits |
| Combat | Dodging (circle/zigzag vs Mechsquitos, sidestep Cogturrets and Cogmowers), fighting (walk near mobs, turrets first, Golden Cogmower hunt), mob guide |
| Drives | Auto-buy + use drives **only when Digital Bee is in your hive**; which drive (auto = hive colour, glitched for mixed), when, limits, hotbar slot, fallback order |
| Farming | Backpack thresholds, farm pattern, token values |
| Controls | Hotkeys, in-game keys, run limits, window |

Decision logic for these lives in `rbc_agent/decision/` (`drives.py`, `picks.py`) and
`rbc_agent/game/hive.py`, and is unit-tested without Roblox.

## Requirements
* Python 3.10+ (Windows for the live agent from Stage 2; Stage 1 runs anywhere)
* `pip install -r requirements.txt` (Stage 1 only needs `pytest`)

## Make the .exe (Windows)
Double-click **`build_exe.bat`**. It installs PyInstaller, builds the program and opens
`dist\RBCAgent\` containing `RBCAgent.exe` plus the `config` and `data` folders.
Keep those folders next to the exe. Rebuild after every new stage.

## Run
```bash
python main.py                         # open the control panel window
```
```bash
python main.py --check-config          # validate your config, print a summary
python main.py --print-config          # see the fully merged config
python main.py --write-default-config  # regenerate config/default_config.json
python -m pytest -q                    # run the test suite (74 tests)
```

## Configuring
* `config/default_config.json` is a generated reference of every setting. **Don't edit it.**
* `config/config.json` holds **your overrides only**. Example:
```json
{
  "_comment": "keys starting with _ are ignored",
  "strategy": { "profile": "BLUE" },
  "pollen":   { "must_return_above": 0.97 },
  "hotkeys":  { "emergency_stop": "F12" },
  "timeouts_s": { "WAITING_FOR_RBC": 20 }
}
```
Typos, wrong types and impossible values are rejected with a clear message,
for example `poln: unknown setting (did you mean 'pollen'?)`.

* `data/rbc_rules.json` holds game knowledge (fields, objective text patterns, upgrades).
  The upgrade entries are **placeholders**. Fill them in from what the RBC shop actually shows.

## Stage 1 contents
| File | What it does |
|---|---|
| `core/config.py` | Typed frozen dataclasses (single source of defaults), strict parser, per-section validation, deep-merge overrides, thread-safe hot reload that keeps the old config on error |
| `core/timing.py` | `wait_until`, `wait_for_change`, `Deadline`, `RateGate` (per-detector Hz), `CancelToken` (pause/stop; pause time doesn't eat timeouts), `FakeClock` for instant tests |
| `core/logger.py` | `[HH:MM:SS] CATEGORY message key=value` format to console, rotating file and a non-blocking GUI queue |
| `core/states.py` | `AgentState` and `StrategyProfile` enums |
| `core/errors.py` | Exception hierarchy (`ConfigError`, `OperationTimeout`, `AutomationCancelled`, `WindowLostError`, …) |

## Known limitations (Stage 1)
* There is no game interaction yet: no capture, input or state machine.
* The default `regions` are guesses for a 16:9 HUD. The Stage 3 calibration tool replaces them.
* Type errors are reported first. Range checks run once types are valid, so a badly broken file can take two passes to fix.
* Upgrade data comes from the BSS wiki (68 upgrades). Prices, reroll costs and exact quest wording are not published; prices are read on screen, quest regexes need checking in-game.
