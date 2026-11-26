# RBC Agent — Technical Architecture

A state-aware automation agent for the Robo Bear Challenge (RBC) in Bee Swarm
Simulator. It runs the game **only through what is visible on screen and
normal keyboard/mouse input**. It does no memory reading, injection, Roblox API
calls or anti-cheat bypass.

---

## 1. The core loop

Each cycle does the same thing, so every action has a check after it:

```
 ┌────────────┐   frames    ┌─────────────┐  GameState  ┌───────────────┐
 │  Capture   │ ──────────▶ │ Perception  │ ──────────▶ │ State machine │
 │ (window +  │             │ (detectors, │             │ (current      │
 │  ROI grab) │             │  confidence)│             │  state logic) │
 └────────────┘             └─────────────┘             └──────┬────────┘
       ▲                                                       │ asks
       │                                                       ▼
       │                                               ┌───────────────┐
       │                                               │ DecisionEngine│
       │                                               │ evaluate(gs)  │
       │                                               └──────┬────────┘
       │                                                      │ Decision
       │        ┌────────────┐   verify(expected)     ┌───────▼───────┐
       └─────── │  Verifier  │ ◀───────────────────── │ Action exec   │
                │ (did it    │                        │ (input layer) │
                │  work?)    │ ── fail ──▶ Recovery   └───────────────┘
                └────────────┘
```

1. **Perceive.** Grab only the regions that are due this tick (each detector has its own scan rate).
2. **Build a `GameState`.** It is an immutable snapshot, and every field carries a confidence value.
3. **State handler.** The current state looks at the snapshot and either stays, changes state (only above a confidence threshold), or asks the decision engine what to do.
4. **Decide.** `DecisionEngine.evaluate(GameState) -> Decision` returns an action, a target, a confidence and a reason.
5. **Act.** The input layer carries out the action. Every action states what it **expects** to happen.
6. **Verify.** `wait_until(expected, timeout)`. If the expectation isn't met, the state handler escalates to the **Recovery engine**.

---

## 2. Layers and modules

```
rbc_agent/
├── main.py                       entry point (CLI / GUI launcher)
├── config/
│   ├── default_config.json       shipped defaults (never edited by the user)
│   └── config.json               user overrides (created on first run)
├── data/
│   ├── rbc_rules.json            editable game knowledge: fields, upgrades, tokens
│   ├── calibration.json          saved UI anchor points (Stage 2/3)
│   └── templates/                reference images captured on YOUR screen
├── logs/   debug/                runtime output
├── tests/                        unit + simulation tests
└── rbc_agent/
    ├── core/
    │   ├── config.py             typed, validated, hot-reloadable configuration
    │   ├── logger.py             categorised event log (+ GUI queue sink)
    │   ├── timing.py             Clock, Deadline, wait_until, RateGate, CancelToken
    │   ├── errors.py             exception hierarchy
    │   ├── paths.py              project paths
    │   ├── state_machine.py      (Stage 5) State base class, registry, transitions
    │   └── scheduler.py          (Stage 3) per-detector scan-rate scheduler
    ├── perception/
    │   ├── window.py             (Stage 2) find Roblox window, DPI, focus, client rect
    │   ├── capture.py            (Stage 2) mss region capture, frame cache
    │   ├── regions.py            (Stage 2) normalised ROI → pixel mapping
    │   ├── templates.py          (Stage 3) multi-scale template matching
    │   ├── ocr.py                (Stage 3) digit/text OCR with sanity filtering
    │   ├── ui_detector.py        (Stage 3/8) screen classification (shop/round/loading…)
    │   ├── pollen_detector.py    (Stage 12) bar-fill measurement
    │   ├── token_detector.py     (Stage 10)
    │   ├── motion.py             (Stage 7) frame-diff / optical-flow for stuck detection
    │   └── hive_scanner.py       (Stage 9)
    ├── game/                     domain models + managers (GameState, HiveProfile, …)
    ├── decision/                 scorers: field, upgrade, token, capacity, round strategy
    ├── input/                    InputController (SendInput scan codes), movement
    ├── recovery/                 RecoveryEngine with 6 escalating levels
    └── ui/                       Tkinter control panel (runs on the main thread)
```

**Dependency rule:** arrows only point downwards.
`ui → (core, game)`, `decision → game`, `game → perception, input`,
`perception/input → core`. The decision layer **never** touches input or
screenshots. It is pure functions of `GameState`, which is what makes it
testable without Roblox.

---

## 3. Threading model

| Thread | Responsibility | Talks to others via |
|---|---|---|
| Main (Tk) | GUI only, never blocks | reads a `queue.Queue` of log/status events |
| Automation | state machine loop | owns the input controller |
| Hotkey listener | F8/F9/F10 (configurable) | sets a shared `CancelToken` |

Pausing and stopping go through one `CancelToken`. Every `wait_until`, every
held key and every movement loop checks it. **Emergency stop** does five things:
the token goes to `STOPPED`, `InputController.stop_all_input()` releases every
key it has recorded as pressed (plus a safety release of all bound keys), the
automation thread exits at its next check (≤ one poll interval), the state is
saved to `logs/last_state.json`, and the GUI shows **STOPPED**.

---

## 4. State machine

Each state is a class with: `on_enter`, `tick(gs) -> Transition | None`,
`on_exit`, `timeout_s` (taken from config), `expected_evidence` and
`recovery_policy`. The machine itself enforces the timeouts, so no state can
hang forever. When a timeout fires, the machine saves a screenshot, logs the
detected state and hands over to `RECOVERY`, which returns to the previous
state or escalates.

| State | Enter when | Leave when (evidence) | Timeout → |
|---|---|---|---|
| IDLE | start | user START | — |
| STARTING_ROBO_BEAR | IDLE + start | Robo Bear dialogue/UI detected | RECOVERY |
| WAITING_FOR_RBC | challenge accepted | round banner / round HUD seen | RECOVERY |
| RBC_ROUND_START | new round number read | quest-choice UI visible | RECOVERY |
| RBC_SELECTING_QUEST | 2 quest cards visible | quest picked (OCR + score; one free reroll per run) | pick first card, flag low confidence |
| RBC_ANALYZING | objective known | decision made | default field |
| RBC_MOVING_TO_FIELD | target field ≠ current | field signature matched | RECOVERY (stuck/lost) |
| RBC_FARMING | in field | capacity / token / objective / round-end evidence | — (watchdog only) |
| RBC_COLLECTING_TOKENS | token score > threshold | token gone or chase budget spent | RBC_FARMING |
| RBC_MANAGING_CAPACITY | capacity rule fires | decision: return / keep going | RBC_FARMING |
| RBC_RETURNING_HIVE | return decided | hive signature + convert prompt | RECOVERY |
| RBC_MANAGING_BEES | bee-pick UI visible (3 picks round 1, then 2 per round) | selection confirmed | RECOVERY |
| RBC_SELECTING_UPGRADES | upgrade cards visible | buy / LOCK / reroll decided (prices read by OCR) | skip buying |
| RBC_PURCHASING_UPGRADES | choice made | purchase confirmed visually | RECOVERY |
| RBC_NEXT_ROUND | shop closed | round number incremented | RECOVERY |
| RBC_COMPLETED / RBC_FAILED | end screen confidence ≥ threshold | stats written | IDLE |
| RECOVERY | any failure | level succeeds → previous state | escalate → PAUSED |
| PAUSED / STOPPED | hotkey, GUI or recovery exhausted | user | — |

Transitions use **confidence scoring**, never a single pixel:

```
rbc_end_conf = 0.35*round_ui_gone + 0.25*completion_text + 0.20*robo_bear_ui + 0.20*layout_match
transition only if rbc_end_conf ≥ detection.transition_threshold, held for N consecutive frames
```

Weights live in `config.detection.confidence_weights` and the hold count in
`detection.confirm_frames`.

---

## 5. Decision layer (pure and testable)

All scorers are small classes behind one protocol:

```python
class Scorer(Protocol[T]):
    def score(self, item: T, gs: GameState) -> ScoredItem[T]: ...  # value + breakdown dict
```

* **FieldScorer**: `hive_synergy + objective_synergy + token_value + buff_value + pollen_value − travel_cost − risk − capacity_penalty`
* **UpgradeScorer**: `base + hive_synergy + objective_synergy + buff_synergy + scaling(rounds_left) − opportunity_cost`
* **TokenScorer**: `value × objective_mult × buff_mult × hive_mult / travel_cost`, with a hard cap on chase time
* **CapacityPolicy**: base thresholds, adjusted by `time_left`, `distance_to_hive_s` and `convert_rate`
* **RoundStrategy**: combines the above and outputs a `Decision(action, target, confidence, reason, breakdown)`

Every weight comes from config, and strategy profiles (`BALANCED`, `RED`, `BLUE`, …)
are weight presets in config. `CUSTOM` is simply the user's own preset. The score breakdown
is logged, so you can always see *why* the agent chose what it did.

---

## 6. Capability matrix (honest version)

### A. Reliable with plain screen/UI automation
| Feature | Method |
|---|---|
| Find the Roblox window, focus, move/resize, DPI | Win32 `EnumWindows`, `GetClientRect`, `SetProcessDpiAwareness` |
| Pollen % of capacity | Fill fraction of the pollen bar ROI (colour run-length), with no OCR needed |
| Exact pollen / capacity numbers | OCR of the HUD text at 1–2 Hz, cross-checked against the bar % |
| Shop / dialogue / confirm / close buttons | Template matching in calibrated ROIs |
| Round number, round timer | Digit OCR on a small ROI, with plausibility filter (round only goes +1) |
| Loading screen / teleport | Low-variance frame + absent HUD |
| Freeze detection | Identical frames for N seconds while input is active |
| Window lost focus | `GetForegroundWindow` check before every input burst |
| Pause / resume / emergency stop | Global hotkeys + CancelToken |

### B. Needs more advanced computer vision (works, but not 100%)
| Feature | Difficulty and fallback |
|---|---|
| Token detection + type | Tokens are small, spin and overlap with particles. Uses colour pre-filter + multi-scale templates per token. **Fallback:** if classification is unsure, use a generic "token" value |
| Stuck detection | Frame-difference / optical flow on the scene area while keys are held. Reliable for "not moving at all", weaker for "sliding along a wall" |
| Character position within field | No coordinates on screen. Position is estimated from field-edge visibility and camera-relative cues. **Fallback:** a fixed farming pattern with periodic re-centring at known landmarks |
| Which field am I in | Ground colour/texture signature + landmark templates. **Fallback:** trust the navigation route that was just executed, verified at a checkpoint |
| Hive composition (bee type, gifted star, level) | Requires viewing the hive with a fixed camera and a template per bee icon. Gifted star and level digits are feasible; event/mythic can be identified via icon templates. **Fallback:** user fills in `hive_profile.json` once |
| Buff icons + stack counts | Template match on the buff bar + small-digit OCR. Moderate accuracy |
| RBC objective text | OCR of quest text, with fuzzy matching against known objective patterns in `rbc_rules.json` |

### C. Not possible legitimately/reliably (only internal data would give it)
| Feature | What we do instead |
|---|---|
| Exact world coordinates / pathfinding on the real map | **Calibrated routes** (dead-reckoning segments) with visual checkpoints after each segment |
| Hidden multipliers (exact pollen/conversion rates, ability proc odds) | Estimates measured during play (pollen gained per second) and stored in statistics |
| Token spawn timing, server-side RBC values not shown in UI | Not modelled; react to what appears |
| Exact positions of every bee/mob off-screen | Not modelled |
| Upgrade numeric effects not displayed | Values come from `rbc_rules.json`, written and tuned by you |

> **Game knowledge:** `data/rbc_rules.json` holds the published rules (25 rounds,
> 300 s per round, bonus Cogs under 120 s, the fixed rounds 5/10/15/20, mobs,
> drives, rarity weights) and all 68 upgrades with structured effects, generated
> by `tools/build_rules.py` from the BSS wiki. Each round runs:
> **quest choice → bee picks → upgrades → play.**
> **Not published anywhere:** upgrade prices, reroll prices, bonus-Cog amounts, the
> debuff curve and the exact on-screen quest wording. The agent reads prices from
> the shop with OCR, and the quest regexes are marked `_verify`.

---

## 7. Technology choices

| Need | Library | Why |
|---|---|---|
| Screen capture | `mss` | fast, region capture, no admin rights |
| Vision | `opencv-python`, `numpy` | template matching, colour masks, optical flow |
| OCR | `pytesseract` (+ Tesseract) or `easyocr` | digits only for HUD, restricted charset |
| Window | `ctypes` Win32 (optionally `pywin32`) | client rect, focus, DPI |
| Input | `ctypes` **SendInput with scan codes** | Roblox ignores virtual-key-only events. This is the same thing a physical keyboard sends |
| Hotkeys | `pynput` (or `keyboard`) | global listener on its own thread |
| GUI | `tkinter` | stdlib, no extra install |
| Tests | `pytest` | simulated `GameState`s, fake clock, fake capture |

Stage 1 uses **only the standard library**, so it runs anywhere.

---

## 8. Stage roadmap

| # | Stage | Main deliverable | Status |
|---|---|---|---|
| 1 | Architecture + configuration | this document, typed config, logger, timing, errors | **done** |
| 2 | Window + screenshot | window finder, DPI, ROI capture, frame cache, debug dumps | next |
| 3 | Basic perception | template matcher, OCR wrapper, scheduler, calibration tool | |
| 4 | Game-state models | GameState, HiveProfile, RoundState, Token, Buff, Upgrade, Decision | |
| 5 | State machine | State base class, timeouts, transitions, confidence gating | |
| 6 | Input controller | SendInput, held-key ledger, stop_all_input | |
| 7 | Movement | routes, checkpoints, stuck detection, re-centring | |
| 8 | RBC detection | round/shop/end screen classifiers | |
| 9 | Hive analysis | hive scan + manual profile fallback | |
| 10 | Token detection | colour prefilter + templates + scoring | |
| 11 | Field/upgrade engine | scorers + strategy profiles | |
| 12 | Pollen/capacity | bar reader + dynamic return policy | |
| 13 | Recovery | 6-level escalation engine | |
| 14 | GUI | Tk control panel + live log | |
| 15 | Statistics | run records, JSON/CSV export | |
| 16 | Testing/optimisation | simulation suite, profiling | |
