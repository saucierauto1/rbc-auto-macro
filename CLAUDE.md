# RBC Agent – notes for Claude

State-aware Robo Bear Challenge (Bee Swarm Simulator) agent in Python. It uses
screen capture and normal keyboard/mouse input only: no memory reading,
injection, Roblox API abuse or anti-cheat bypass. Full design and capability
matrix: `ARCHITECTURE.md`. Original spec: 16 stages, each delivered with
architecture notes, implementation, how to run, how to test, and known limitations.

## Status
- **Stage 1 done:** typed config (`rbc_agent/core/config.py`, defaults live only in
  the dataclasses; `config/default_config.json` is generated), logger, timing
  (`wait_until`, `CancelToken`, `RateGate`, `FakeClock`), errors, state enums,
  game rules loader (`rbc_agent/game/rules.py`), and the Tk control panel
  (`rbc_agent/ui/app.py`; Start/Pause/Stop/Calibrate are disabled until later stages).
- **Settings GUI + decision logic (0.2.0):** tabbed control panel (`ui/app.py`,
  `ui/widgets.py`): Hive, Bee Picks, Quests & Fields, Upgrades, Drives, Farming,
  Controls; saves only non-default values to `config/config.json`. New config
  sections `hive.bees`, `bee_picks`, `quests`, `upgrades`, `drives`. Pure pickers in
  `decision/picks.py` and `decision/drives.py` (drives only with Digital Bee in hive),
  hive colour in `game/hive.py` (bee list in `data/bees.json`). START runs
  `core/preflight.py`; flip `ENGINE_PARTS` there as stages land.
- **Stage 2 done (0.3.0):** `perception/window.py` (title or process match, DPI aware,
  client rect on screen), `capture.py` (mss, BGR numpy, FrameCache, FakeBackend for
  tests), `regions.py`, `debug_dump.py` (pure-Python PNG + overlays). Dashboard
  📸 Screenshot button saves client/overlay/region crops to `debug/`.
- **Also added:** Hotbar tab + `decision/hotbar.py` (slot use rules, items in
  `data/hotbar_items.json`), Combat tab + `decision/combat.py` (wiki dodge tactics,
  Golden Cogmower hunt). Mob/drive/challenge facts from the wiki are in
  `data/rbc_rules.json` (speed bonus is 3:45, not 2:00; drives cost Cogs + honey).
- **Stage 3 done (0.4.0):** `perception/ocr.py` (Tesseract via stdin, no pytesseract),
  `perception/screen_reader.py` (finds the dialog / round panel by the green title bar
  RGB 74,213,84, boxes in `data/rbc_screens.json` scale with it), `game/screens.py` (text ->
  ScreenState), `ui/screen_report.py` + Dashboard 🔍 Read screen. Real screenshots in
  `tests/fixtures/screens/` (other players' names blacked out). CI installs Tesseract and the
  release zip bundles it (`tools/bundle_tesseract.sh`). Template matching moved to Stage 10,
  scheduler to Stage 5 (nothing to schedule yet). Only seen at 1920x1080: other window sizes are
  tested by shrinking the screenshots, not on a real smaller window.
- **Stage 4 done (0.5.0):** `game/state.py` `RunState.observe(ScreenState)` confirms reads over
  `detection.confirm_frames` (key = kind, round, Cogs on dialogs), round only same/+1 (jump believed
  after `detection.jump_accept_reads`; round 1 on quest screen = new run), unknown gaps shorter than
  `unknown_state_grace_s` keep state; `snapshot()` -> frozen `GameState`. Dashboard 👁 Watch runs
  read -> observe in a thread (CancelToken, no Tk calls from the thread). Also from the first live
  run: mutation parsing by "+N", `upgrades.skip_off_color`, Read screen saves a `_clean` copy,
  live 1920x1009 fixtures. A dialog read takes 1-1.7 s, so confirmation costs ~5 s: Stage 5 should
  act on single reads where safe and confirm only before spending Cogs.
- **Decided (user):** hive colour and the Digital Bee drive rule use the **whole hive** from the
  Hive tab (`HiveProfile.from_config`), not the RBC team picked during the run. `GameState.team`
  is only a record of picks.
- **`game/perceive.py` `Perceiver`:** the one capture -> ScreenReader -> parse_screen chain (Read
  screen, Watch, and Stage 5 use it). Create and close it in the thread that uses it (`with`).
  `RBCRules.mobs` is the typed access to mob facts; nothing outside rules.py reads `.raw`.
- **Stage 5 done (0.6.0):** `decision/engine.py` `decide(GameState, DecisionContext)` -> `Decision`
  list with index targets (quest card, bee row, shop row; buys mapped to rows once each), `wait`
  when a card/bee/price is incomplete. `core/state_machine.py` StateMachine (state, entered_at,
  `timeout_for`). `rbc_agent/agent.py` Agent.step(): RunState -> SCREEN_STATE -> decide once per
  (screen, round, Cogs, offers); hotbar via `slots_to_use` (round_start window from
  `hotbar.round_start_window_s`, else phase "unknown" until Stage 7); timeouts only when
  `actor.live`; DryRunActor logs "Would …". Watch runs the Agent. RunState's confirmation key now
  includes parsed offers, so a changed set of cards needs `confirm_frames` reads. Drives are not
  in the engine (no drive shop seen on screen yet).
- **Next: Stage 6** (input: an actor with `live = True` that turns Decisions into clicks/keys).

## Game facts (see data/rbc_rules.json)
25 rounds, 300 s each, bonus Cogs under 3:45 (225 s). Each round: quest choice (1 of 2,
one free reroll per run) → bee picks (3 in round 1, then +2) → upgrades (buy /
LOCK / reroll with Cogs) → play. Fixed rounds 5/10/15/20. 68 upgrades are
generated by `tools/build_rules.py` from the BSS wiki. Prices, reroll costs and
exact quest wording are NOT published, so read them on screen with OCR.

## Conventions
- Run tests: `python -m pytest -q` (must stay green; the GitHub Action runs them too).
- All tunables go in config dataclasses, never as literals in logic.
- Game knowledge goes in `data/rbc_rules.json`, not code.
- The decision layer is pure (GameState → Decision) and never touches input or capture.
- Every wait uses `wait_until`/`interruptible_sleep` with a `CancelToken`. No bare `sleep`.
- Pushing to `main` triggers `.github/workflows/build.yml`, which builds `RBCAgent.exe`
  and publishes `RBCAgent.zip` as a GitHub Release.

## User
Windows PC, not a programmer: give click-by-click instructions, keep explanations
short. Shares the exe with others via the Releases page.
