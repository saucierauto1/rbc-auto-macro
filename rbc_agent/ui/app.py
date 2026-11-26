"""The RBC Agent control panel (Tkinter, main thread only).

Tabs: Dashboard · Hive · Bee Picks · Quests & Fields · Upgrades · Drives ·
Farming · Controls. Every settings tab writes to config/config.json (only
values that differ from the defaults), validated before saving.

Everything slow happens on other threads. The panel only reads the
``StatusBus`` and drains the log queue on a 100 ms timer.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from .. import __version__
from ..core.config import (DRIVE_TYPES, HOTBAR_WHEN, QUEST_TYPES, AppConfig, BeeEntry, ConfigManager,
                           DriveConfig, HiveConfig, parse_config, to_dict)
from ..core.errors import ConfigError
from ..core.logger import Category, LogEvent, setup_logging
from ..core.paths import DATA_DIR, LOGS_DIR, USER_CONFIG_FILE
from ..core.preflight import can_start, run_preflight
from ..core.states import StrategyProfile
from ..decision.drives import plan_drive, preferred_drives
from ..game.hive import HiveProfile, load_catalog
from ..game.rules import RBCRules
from .status import StatusBus, format_rows
from .widgets import (ACCENT, BAD, BG, COLOR_HEX, FG, MUTED, OK, PANEL, WARN, OrderedPicker,
                      PercentScale, ScrollFrame, apply_theme, labeled)

CAT_COLORS = {
    Category.STATE: "#7cc4ff", Category.DECISION: "#f2b632", Category.ACTION: "#9be39b",
    Category.DETECT: "#c3a6ff", Category.RECOVERY: "#ff9e57", Category.ERROR: "#ff6b6b",
    Category.CONFIG: "#8a92a6", Category.SYSTEM: "#8a92a6", Category.STATS: "#8a92a6",
}
LOG_LINES_MAX = 2000
QUEST_LABELS = {
    "collect_color_pollen": "Collect colour pollen (red/blue/white)",
    "collect_field_pollen": "Collect pollen from a field",
    "defeat_mobs": "Defeat mobs (Mechsquitos, Cogmowers…)",
    "convert_pollen": "Convert pollen at the hive",
}
DRIVE_LABELS = {"red": "Red Drive", "blue": "Blue Drive", "white": "White Drive", "glitched": "Glitched Drive"}
HOTKEYS = [f"F{i}" for i in range(1, 13)] + ["PAUSE", "SCROLL_LOCK", "INSERT", "HOME", "END"]
RARITY_TAG = {"common": "C", "rare": "R", "epic": "E", "legendary": "L", "mythic": "M", "event": "Ev"}


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    for p in path:
        d = d[p] if isinstance(d, dict) else getattr(d, p)
    return d


class Binder:
    """Connects GUI widgets to config paths, and turns them back into overrides."""

    def __init__(self) -> None:
        self.items: list[tuple[tuple[str, ...], Callable[[], Any], Callable[[Any], None]]] = []

    def bind(self, path: tuple[str, ...], getter: Callable[[], Any], setter: Callable[[Any], None]) -> None:
        self.items.append((path, getter, setter))

    def var(self, path: tuple[str, ...], var: tk.Variable, cast: Callable[[Any], Any] | None = None) -> tk.Variable:
        def get():
            v = var.get()
            return cast(v) if cast else v
        self.bind(path, get, var.set)
        return var

    def load(self, cfg: AppConfig) -> None:
        d = to_dict(cfg)
        for path, _, setter in self.items:
            setter(_dig(d, path))

    def overrides(self, base: dict[str, Any]) -> dict[str, Any]:
        """Apply GUI values onto ``base`` (the current config.json contents),
        dropping keys that equal the defaults so config.json stays short."""
        defaults = to_dict(AppConfig())
        out = json.loads(json.dumps(base))
        for path, getter, _ in self.items:
            val = getter()
            node = out
            for p in path[:-1]:
                node = node.setdefault(p, {})
            if val == _dig(defaults, path):
                node.pop(path[-1], None)
            else:
                node[path[-1]] = val
        return _prune(out)


def _prune(d: dict) -> dict:
    for k in list(d):
        if isinstance(d[k], dict):
            _prune(d[k])
            if not d[k]:
                del d[k]
    return d


def _f2(v: Any) -> float:
    return round(float(v), 2)


class ControlPanel:
    def __init__(self, root: tk.Tk, config_mgr: ConfigManager):
        self.root = root
        self.cfg_mgr = config_mgr
        self.log_queue: "queue.Queue[LogEvent]" = queue.Queue(maxsize=5000)
        self.status = StatusBus()
        self._shown_version = -1
        self.log = setup_logging(self.cfg_mgr.config.logging, gui_queue=self.log_queue)
        self.catalog = load_catalog()
        try:
            self.rules: RBCRules | None = RBCRules.load()
        except Exception as exc:  # still open the window
            self.rules = None
            self.log.error(f"data/rbc_rules.json could not be loaded: {exc}")
        self.binder = Binder()
        self.dirty = False

        self._build_window()
        self.binder.load(self.cfg_mgr.config)
        self._hive_from_cfg(self.cfg_mgr.config.hive)
        self._set_dirty(False)
        self.status.publish(strategy=self.cfg_mgr.config.strategy.profile.value)
        self.log.system(f"RBC Agent {__version__} started")
        self.log.config("Configuration OK", profile=self.cfg_mgr.config.strategy.profile.value)
        self.log.warning("The automation engine isn't built yet (stages 2-8). "
                         "Press START to see exactly what is missing.")
        self._tick()

    # ================================================================== layout
    def _build_window(self) -> None:
        r = self.root
        r.title(f"RBC Agent {__version__}")
        r.geometry("1000x760")
        r.minsize(860, 600)
        r.protocol("WM_DELETE_WINDOW", self._on_close)
        apply_theme(r)

        head = ttk.Frame(r, padding=(16, 12, 16, 4))
        head.pack(fill="x")
        ttk.Label(head, text="ROBO BEAR CHALLENGE AGENT", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"  v{__version__}", style="Muted.TLabel").pack(side="left", pady=(6, 0))
        self.pill = tk.Label(head, text=" IDLE ", bg=PANEL, fg=MUTED, font=("Segoe UI", 10, "bold"),
                             padx=10, pady=3)
        self.pill.pack(side="right")

        # save bar (visible on every tab); packed first so the tabs can't push it off-screen
        bar = ttk.Frame(r, padding=(16, 8, 16, 12))
        bar.pack(side="bottom", fill="x")
        self.btn_save = ttk.Button(bar, text="💾  Save settings", style="Accent.TButton",
                                   command=self._save)
        self.btn_save.pack(side="right")
        ttk.Button(bar, text="Revert", command=self._revert).pack(side="right", padx=6)
        self.save_lbl = ttk.Label(bar, text="", style="Muted.TLabel")
        self.save_lbl.pack(side="right", padx=10)
        ttk.Button(bar, text="Open config file", command=self._open_config).pack(side="left")
        ttk.Button(bar, text="Open logs folder", command=lambda: self._open_path(LOGS_DIR)).pack(
            side="left", padx=6)
        r.bind_all("<Control-s>", lambda e: self._save())

        self.nb = ttk.Notebook(r)
        self.nb.pack(fill="both", expand=True, padx=8)
        self._tab_dashboard()
        self._tab_hive()
        self._tab_bee_picks()
        self._tab_quests_fields()
        self._tab_upgrades()
        self._tab_drives()
        self._tab_hotbar()
        self._tab_combat()
        self._tab_farming()
        self._tab_controls()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._update_drive_preview())

    def _settings_tab(self, title: str, hint: str) -> ttk.Frame:
        sf = ScrollFrame(self.nb)
        self.nb.add(sf, text=title)
        ttk.Label(sf.body, text=hint, style="Hint.TLabel").pack(anchor="w", pady=(0, 10))
        return sf.body

    def _section(self, parent, title: str) -> ttk.Frame:
        lf = ttk.LabelFrame(parent, text=f" {title} ", padding=12)
        lf.pack(fill="x", pady=(0, 12))
        return lf

    def _track(self, var: tk.Variable) -> tk.Variable:
        var.trace_add("write", lambda *_: self._set_dirty(True))
        return var

    def _bvar(self, path, text, parent, row, hint="", col=0) -> tk.BooleanVar:
        v = self._track(self.binder.var(path, tk.BooleanVar(), bool))
        ttk.Checkbutton(parent, text=text, variable=v).grid(row=row, column=col, columnspan=2,
                                                            sticky="w", pady=3)
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel").grid(row=row, column=col + 2, sticky="w", padx=10)
        return v

    def _num(self, path, text, parent, row, lo, hi, inc=1, is_float=False, hint="", col=0, width=8):
        var = tk.DoubleVar() if is_float else tk.IntVar()
        self._track(self.binder.var(path, var, _f2 if is_float else int))
        labeled(parent, row, text, lambda p: ttk.Spinbox(p, from_=lo, to=hi, increment=inc,
                                                          textvariable=var, width=width), hint, col)
        return var

    def _combo(self, path, text, parent, row, values, hint="", col=0, width=20):
        var = self._track(self.binder.var(path, tk.StringVar()))
        labeled(parent, row, text, lambda p: ttk.Combobox(p, textvariable=var, values=values,
                                                           state="readonly", width=width), hint, col)
        return var

    def _picker(self, path, parent, items, **kw) -> OrderedPicker:
        p = OrderedPicker(parent, items, on_change=lambda: self._set_dirty(True), **kw)
        self.binder.bind(path, p.get, p.set)
        return p

    # ------------------------------------------------------------- dashboard
    def _tab_dashboard(self) -> None:
        tab = ttk.Frame(self.nb, padding=(12, 10))
        self.nb.add(tab, text="Dashboard")

        grid = ttk.Frame(tab, style="Panel.TFrame", padding=12)
        grid.pack(fill="x")
        self._value_labels: dict[str, ttk.Label] = {}
        for i, (label, _) in enumerate(format_rows(self.status.read()[1])):
            row, col = divmod(i, 3)
            cell = ttk.Frame(grid, style="Panel.TFrame")
            cell.grid(row=row, column=col, sticky="w", padx=(0, 30), pady=4)
            ttk.Label(cell, text=label, style="PanelMuted.TLabel").pack(anchor="w")
            v = ttk.Label(cell, text="–", style="Value.TLabel")
            v.pack(anchor="w")
            self._value_labels[label] = v
        for c in range(3):
            grid.columnconfigure(c, weight=1)

        self.hive_summary = ttk.Label(tab, text="", style="Muted.TLabel")
        self.hive_summary.pack(anchor="w", pady=(8, 0))

        bar = ttk.Frame(tab, padding=(0, 10))
        bar.pack(fill="x")
        hk = self.cfg_mgr.config.hotkeys
        self.btn_start = ttk.Button(bar, text="▶  START", style="Accent.TButton", command=self._start)
        self.btn_pause = ttk.Button(bar, text=f"⏸ PAUSE ({hk.pause})", state="disabled")
        self.btn_stop = ttk.Button(bar, text=f"■ STOP ({hk.emergency_stop})", style="Danger.TButton",
                                   state="disabled")
        self.btn_reload = ttk.Button(bar, text="⟳ Reload config", command=self._reload_config)
        self.btn_calib = ttk.Button(bar, text="⌖ Calibrate (Stage 3)", state="disabled")
        self.btn_shot = ttk.Button(bar, text="📸 Screenshot", command=self._screenshot)
        for b in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_shot, self.btn_reload, self.btn_calib):
            b.pack(side="left", padx=(0, 6))
        self.debug_var = tk.BooleanVar(value=self.cfg_mgr.config.debug.enabled)
        ttk.Checkbutton(bar, text="Debug mode", variable=self.debug_var,
                        command=self._toggle_debug).pack(side="left", padx=8)

        logf = ttk.Frame(tab)
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, bg="#0e1014", fg=FG, insertbackground=FG,
                                font=("Consolas", 9), wrap="none", relief="flat",
                                state="disabled", padx=8, pady=6)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for cat, color in CAT_COLORS.items():
            self.log_text.tag_configure(cat.value, foreground=color)

    # ------------------------------------------------------------------ hive
    def _tab_hive(self) -> None:
        body = self._settings_tab("Hive", "Enter the bees in your hive. The agent uses this for your "
                                  "hive colour, bee-pick scoring and the Digital Bee drive rule. "
                                  "Pick a bee on the left, set count / gifted / level, then Add.")
        top = ttk.Frame(body)
        top.pack(fill="both", expand=True)

        left = ttk.Frame(top)
        left.pack(side="left", fill="y")
        f = ttk.Frame(left)
        f.pack(fill="x")
        self.hive_filter = tk.StringVar(value="all")
        self.hive_search = tk.StringVar()
        ttk.Combobox(f, textvariable=self.hive_filter, state="readonly", width=10,
                     values=["all", "red", "blue", "white", "common", "rare", "epic",
                             "legendary", "mythic", "event"]).pack(side="left")
        ttk.Entry(f, textvariable=self.hive_search, width=14).pack(side="left", padx=6)
        self.hive_filter.trace_add("write", lambda *_: self._fill_catalog())
        self.hive_search.trace_add("write", lambda *_: self._fill_catalog())
        from .widgets import listbox
        self.cat_lb = listbox(left, height=16, width=26)
        self.cat_lb.pack(fill="y", expand=True, pady=6)
        self.cat_lb.bind("<Double-Button-1>", lambda e: self._hive_add())

        mid = ttk.Frame(top, padding=(14, 0))
        mid.pack(side="left", fill="y")
        self.h_count, self.h_gifted, self.h_level = tk.IntVar(value=1), tk.IntVar(value=0), tk.IntVar(value=15)
        labeled(mid, 0, "Count", lambda p: ttk.Spinbox(p, from_=1, to=50, textvariable=self.h_count, width=6))
        labeled(mid, 1, "Gifted", lambda p: ttk.Spinbox(p, from_=0, to=50, textvariable=self.h_gifted, width=6))
        labeled(mid, 2, "Level", lambda p: ttk.Spinbox(p, from_=1, to=25, textvariable=self.h_level, width=6))
        ttk.Button(mid, text="Add / update →", style="Accent.TButton",
                   command=self._hive_add).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 3))
        ttk.Button(mid, text="← Remove selected", command=self._hive_remove).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(mid, text="Clear hive", command=self._hive_clear).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=3)

        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True)
        cols = ("bee", "color", "rarity", "count", "gifted", "level")
        self.hive_tree = ttk.Treeview(right, columns=cols, show="headings", height=16)
        for c, w in zip(cols, (110, 60, 80, 55, 55, 50)):
            self.hive_tree.heading(c, text=c.upper())
            self.hive_tree.column(c, width=w, anchor="w" if c in ("bee", "color", "rarity") else "center")
        for color, hexv in COLOR_HEX.items():
            self.hive_tree.tag_configure(color, foreground=hexv)
        self.hive_tree.pack(fill="both", expand=True)
        self.hive_tree.bind("<<TreeviewSelect>>", self._hive_select)

        sec = self._section(body, "Hive colour")
        self._combo(("hive", "color_override"), "Force hive colour", sec, 0, ["", "red", "blue", "white", "mixed"],
                    "leave empty to work it out from your bees", width=10)
        self.hive_stats = ttk.Label(sec, text="", font=("Segoe UI", 11, "bold"))
        self.hive_stats.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.binder.bind(("hive", "bees"), self._hive_get, lambda v: None)
        self._fill_catalog()

    def _fill_catalog(self) -> None:
        f, s = self.hive_filter.get(), self.hive_search.get().strip().lower()
        self._cat_keys = [n for n, b in self.catalog.items()
                          if (f == "all" or f in (b.color, b.rarity)) and (not s or s in n.lower())]
        self.cat_lb.delete(0, "end")
        for n in self._cat_keys:
            b = self.catalog[n]
            self.cat_lb.insert("end", f"{n} Bee  ·  {RARITY_TAG[b.rarity]}")
            self.cat_lb.itemconfigure("end", foreground=COLOR_HEX[b.color])

    def _hive_rows(self) -> dict[str, BeeEntry]:
        out = {}
        for iid in self.hive_tree.get_children():
            v = self.hive_tree.item(iid, "values")
            out[v[0]] = BeeEntry(v[0], int(v[3]), int(v[4]), int(v[5]))
        return out

    def _hive_get(self) -> list[dict]:
        return [to_dict(e) for e in self._hive_rows().values()]

    def _hive_put(self, entries: list[BeeEntry]) -> None:
        self.hive_tree.delete(*self.hive_tree.get_children())
        order = list(self.catalog)
        for e in sorted(entries, key=lambda e: order.index(e.bee) if e.bee in order else 999):
            info = self.catalog.get(e.bee)
            color = info.color if info else "mixed"
            self.hive_tree.insert("", "end", iid=e.bee, tags=(color,), values=(
                e.bee, color, info.rarity if info else "?", e.count, e.gifted, e.level))
        self._update_hive_stats()

    def _hive_from_cfg(self, cfg: HiveConfig) -> None:
        self._hive_put(list(cfg.bees))

    def _hive_add(self) -> None:
        sel = self.cat_lb.curselection()
        if not sel:
            messagebox.showinfo("Hive", "Pick a bee from the list on the left first.")
            return
        try:
            count, gifted, level = self.h_count.get(), self.h_gifted.get(), self.h_level.get()
        except tk.TclError:
            messagebox.showerror("Hive", "Count, gifted and level must be whole numbers.")
            return
        rows = self._hive_rows()
        for i in sel:
            name = self._cat_keys[i]
            e = BeeEntry(name, count, min(gifted, count), level)
            problems = e.validate()
            if problems:
                messagebox.showerror("Hive", "\n".join(problems))
                return
            rows[name] = e
        if sum(e.count for e in rows.values()) > 50:
            messagebox.showwarning("Hive", "That would be more than 50 bees. A hive holds 50 at most.")
            return
        self._hive_put(list(rows.values()))
        self._set_dirty(True)

    def _hive_select(self, _e=None) -> None:
        sel = self.hive_tree.selection()
        if sel:
            v = self.hive_tree.item(sel[0], "values")
            self.h_count.set(int(v[3]))
            self.h_gifted.set(int(v[4]))
            self.h_level.set(int(v[5]))
            if v[0] in self._cat_keys:
                i = self._cat_keys.index(v[0])
                self.cat_lb.selection_clear(0, "end")
                self.cat_lb.selection_set(i)
                self.cat_lb.see(i)

    def _hive_remove(self) -> None:
        for iid in self.hive_tree.selection():
            self.hive_tree.delete(iid)
        self._update_hive_stats()
        self._set_dirty(True)

    def _hive_clear(self) -> None:
        if self.hive_tree.get_children() and messagebox.askyesno("Hive", "Remove every bee from the list?"):
            self._hive_put([])
            self._set_dirty(True)

    def _current_hive(self) -> HiveProfile:
        cfg = HiveConfig(color_override=self._safe(self.binder_get(("hive", "color_override")), ""),
                         bees=list(self._hive_rows().values()))
        return HiveProfile.from_config(cfg, self.catalog)

    def binder_get(self, path):
        for p, getter, _ in self.binder.items:
            if p == path:
                try:
                    return getter()
                except tk.TclError:
                    return None
        return None

    @staticmethod
    def _safe(v, default):
        return default if v is None else v

    def _update_hive_stats(self) -> None:
        h = self._current_hive()
        gifted = sum(h.gifted.values())
        cc = h.color_counts
        digi = "✔ Digital Bee in hive (drives ON)" if h.has_digital_bee else "✖ no Digital Bee (drives skipped)"
        text = (f"{h.total}/50 bees · {gifted} gifted · {h.color.upper()} hive   "
                f"(red {cc.get('red', 0)} · blue {cc.get('blue', 0)} · white {cc.get('white', 0)})   {digi}")
        if hasattr(self, "hive_stats"):
            self.hive_stats.configure(text=text, foreground=COLOR_HEX.get(h.color, FG))
        if hasattr(self, "hive_summary"):
            self.hive_summary.configure(text="Hive: " + text)
        self._update_drive_preview()

    # ------------------------------------------------------------- bee picks
    def _tab_bee_picks(self) -> None:
        body = self._settings_tab("Bee Picks", "Each RBC round you pick bees from a random selection of "
                                  "your hive (3 in round 1, then 2 more per round). Bees on your priority "
                                  "list are taken first, top to bottom. Colours show bee colour.")
        items = [(n, f"{n} Bee  ·  {RARITY_TAG[b.rarity]}") for n, b in self.catalog.items()]
        color_of = lambda k: self.catalog[k].color if k in self.catalog else ""  # noqa: E731
        sec = self._section(body, "Priority (pick these first)")
        self._picker(("bee_picks", "priority"), sec, items, color_of=color_of).pack(fill="both", expand=True)
        sec = self._section(body, "Avoid (only pick if nothing else)")
        self._picker(("bee_picks", "avoid"), sec, items, color_of=color_of, height=6,
                     right_title="Avoid these").pack(fill="both", expand=True)
        sec = self._section(body, "Options")
        self._bvar(("bee_picks", "prefer_gifted"), "Prefer gifted bees", sec, 0)
        self._bvar(("bee_picks", "prefer_hive_color"), "Prefer bees matching my hive colour", sec, 1)
        self._bvar(("bee_picks", "reroll_if_no_priority"), "Reroll (costs Cogs) when no priority bee is offered", sec, 2)
        self._num(("bee_picks", "max_reroll_cogs"), "Max Cogs to spend on a bee reroll", sec, 3, 0, 999)

    # ------------------------------------------------------ quests & fields
    def _tab_quests_fields(self) -> None:
        body = self._settings_tab("Quests & Fields", "Each round offers 2 quests. The agent picks the one "
                                  "highest on your list; ties go to the one matching your hive colour.")
        sec = self._section(body, "Quest priority")
        self._picker(("quests", "priority"), sec, [(q, QUEST_LABELS[q]) for q in QUEST_TYPES],
                     height=5).pack(fill="x")
        sec2 = ttk.Frame(sec)
        sec2.pack(fill="x", pady=(10, 0))
        self._bvar(("quests", "prefer_harder_for_cogs"), "Prefer the harder quest (more Cogs)", sec2, 0)
        self._bvar(("quests", "chase_fast_bonus"), "Go for the speed bonus Cogs (finish under 3:45)", sec2, 1)
        ttk.Label(sec2, text="Use the free reroll when BOTH quests are:", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 2))
        self._reroll_vars: dict[str, tk.BooleanVar] = {}
        rr = ttk.Frame(sec2)
        rr.grid(row=3, column=0, columnspan=3, sticky="w")
        for i, q in enumerate(QUEST_TYPES):
            v = self._track(tk.BooleanVar())
            self._reroll_vars[q] = v
            ttk.Checkbutton(rr, text=QUEST_LABELS[q].split(" (")[0], variable=v).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 20))
        self.binder.bind(("quests", "free_reroll_if_both"),
                         lambda: [q for q in QUEST_TYPES if self._reroll_vars[q].get()],
                         lambda v: [self._reroll_vars[q].set(q in v) for q in QUEST_TYPES])

        sec = self._section(body, "Fields: bias and blacklist")
        ttk.Label(sec, text="Bias adds to a field's score (+ = go there more, − = less). "
                  "Blacklisted fields are never used.", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=8, sticky="w", pady=(0, 6))
        fields = self.rules.fields if self.rules else {}
        self._bias_vars: dict[str, tk.DoubleVar] = {}
        self._bl_vars: dict[str, tk.BooleanVar] = {}
        for i, (name, color) in enumerate(fields.items()):
            row, col = 1 + i // 2, (i % 2) * 4
            tk.Label(sec, text=name, bg=BG, fg=COLOR_HEX.get(color, FG), font=("Segoe UI", 10)).grid(
                row=row, column=col, sticky="w", padx=(0, 6), pady=2)
            bv = self._track(tk.DoubleVar(value=0.0))
            ttk.Spinbox(sec, from_=-50, to=50, increment=1, textvariable=bv, width=6).grid(
                row=row, column=col + 1, pady=2)
            blv = self._track(tk.BooleanVar())
            ttk.Checkbutton(sec, text="blacklist", variable=blv).grid(row=row, column=col + 2,
                                                                     sticky="w", padx=(6, 30))
            self._bias_vars[name], self._bl_vars[name] = bv, blv
        self.binder.bind(("fields", "bias"),
                         lambda: {n: _f2(v.get()) for n, v in self._bias_vars.items() if _f2(v.get()) != 0},
                         lambda d: [v.set(d.get(n, 0.0)) for n, v in self._bias_vars.items()])
        self.binder.bind(("fields", "blacklist"),
                         lambda: [n for n, v in self._bl_vars.items() if v.get()],
                         lambda lst: [v.set(n in lst) for n, v in self._bl_vars.items()])

        sec = self._section(body, "Field scoring weights")
        w = ("fields", "weights")
        for i, (k, label) in enumerate([("hive_synergy", "Hive colour match"),
                                        ("objective_synergy", "Helps the quest"),
                                        ("token_value", "Token value"), ("buff_value", "Buff value"),
                                        ("pollen_value", "Pollen value"), ("travel_cost", "Travel cost (−)"),
                                        ("risk", "Mob risk (−)"), ("capacity_penalty", "Capacity penalty (−)")]):
            self._num(w + (k,), label, sec, i // 2, 0, 10, 0.1, True, col=(i % 2) * 3)

    # ------------------------------------------------------------- upgrades
    def _tab_upgrades(self) -> None:
        body = self._settings_tab("Upgrades", "Upgrades are bought with Cogs between rounds. Priority "
                                  "upgrades are bought first (and LOCKED if you can't afford them yet). "
                                  "Everything else is scored by your strategy profile.")
        ups = self.rules.upgrades if self.rules else {}
        items = [(u.id, f"{u.name}  ·  {u.rarity[:1].upper()}  ·  {', '.join(u.tags)}") for u in
                 sorted(ups.values(), key=lambda u: (["legendary", "epic", "rare", "common"].index(u.rarity), u.name))]
        color_of = lambda k: ups[k].color if k in ups else ""  # noqa: E731
        sec = self._section(body, "Priority upgrades")
        self._picker(("upgrades", "priority"), sec, items, color_of=color_of, height=9).pack(fill="both")
        sec = self._section(body, "Never buy")
        self._picker(("upgrades", "blacklist"), sec, items, color_of=color_of, height=5,
                     right_title="Blacklisted").pack(fill="both")
        sec = self._section(body, "Shop behaviour")
        self._bvar(("upgrades", "auto_buy"), "Auto-buy upgrades", sec, 0)
        self._bvar(("upgrades", "lock_unaffordable_priority"), "LOCK priority upgrades I can't afford yet", sec, 1)
        self._num(("upgrades", "min_cog_reserve"), "Keep at least this many Cogs", sec, 2, 0, 9999)
        self._num(("upgrades", "max_rerolls_per_round"), "Max shop rerolls per round", sec, 3, 0, 20)
        rb = ttk.Frame(sec)
        rb.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(rb, text="Rarity multipliers:").grid(row=0, column=0, sticky="w", padx=(0, 10))
        for i, r in enumerate(("common", "rare", "epic", "legendary")):
            self._num(("upgrades", "rarity_bias", r), r.title(), rb, 0, 0, 5, 0.05, True, col=1 + i * 2, width=5)

        sec = self._section(body, "Strategy")
        self._combo(("strategy", "profile"), "Strategy profile", sec, 0, [p.value for p in StrategyProfile],
                    "RED/BLUE/WHITE boost that colour; *_FOCUSED boost that upgrade tag")
        self._bvar(("strategy", "auto_profile_from_hive"), "Pick the profile from my hive colour automatically", sec, 1)

    # --------------------------------------------------------------- drives
    def _tab_drives(self) -> None:
        body = self._settings_tab("Drives", "Drives (Red / Blue / White / Glitched) give x1.25 pollen, "
                                  "x1.25 field capacity and +1 bee attack for one round. With Digital Bee "
                                  "active they also corrupt your field and permanently upgrade Digital Bee. "
                                  "Shop price: 50 Cogs + honey (Glitched: 100 Cogs + 5 of each other drive, "
                                  "needs the Gold Cog Amulet). With the safety switch on, nothing is bought "
                                  "unless Digital Bee is in your hive.")
        self.drive_banner = tk.Label(body, text="", bg=PANEL, fg=FG, font=("Segoe UI", 11, "bold"),
                                     anchor="w", padx=12, pady=10, justify="left")
        self.drive_banner.pack(fill="x", pady=(0, 12))

        sec = self._section(body, "Auto-buy")
        self._bvar(("drives", "enabled"), "Auto-buy and use drives", sec, 0)
        self._bvar(("drives", "only_with_digital_bee"), "Only when Digital Bee is in my hive (recommended)", sec, 1)
        self._combo(("drives", "choice"), "Which drive", sec, 2, ["auto"] + list(DRIVE_TYPES),
                    "auto = match hive colour (glitched for a mixed hive)", width=12)
        self._combo(("drives", "use_on"), "When", sec, 3,
                    ["every_round", "fixed_rounds_only", "pollen_quests_only"],
                    "fixed rounds = 5 / 10 / 15 / 20 / 25")
        self._num(("drives", "start_round"), "Start from round", sec, 4, 1, 25)
        self._num(("drives", "max_per_run"), "Max drives per run", sec, 5, 0, 25, hint="0 = no limit")
        self._num(("drives", "min_cog_reserve"), "Keep at least this many Cogs", sec, 6, 0, 9999)

        sec = self._section(body, "Using it")
        self._bvar(("drives", "use_right_after_buying"), "Use the drive right after buying it", sec, 0)
        self._num(("drives", "hotbar_slot"), "Hotbar slot", sec, 1, 0, 7, hint="1-7, or 0 = use from inventory")

        sec = self._section(body, "Fallback order (if the chosen drive isn't in the shop)")
        self._picker(("drives", "fallback_order"), sec, [(d, DRIVE_LABELS[d]) for d in DRIVE_TYPES],
                     height=4, color_of=lambda d: {"glitched": "mixed"}.get(d, d)).pack(fill="x")
        self.root.after(200, self._update_drive_preview)

    def _drive_cfg_from_gui(self) -> DriveConfig | None:
        try:
            vals = {p[1]: g() for p, g, _ in self.binder.items if p[0] == "drives"}
            return DriveConfig(**vals)
        except (tk.TclError, TypeError, ValueError):
            return None

    def _update_drive_preview(self) -> None:
        if not hasattr(self, "drive_banner"):
            return
        cfg = self._drive_cfg_from_gui()
        hive = self._current_hive()
        # Mid-reload the fields can be half-filled; skip until they're valid.
        if cfg is None or cfg.validate() or not preferred_drives(cfg, hive):
            return
        if not cfg.enabled:
            text, color = "Drives are OFF.", MUTED
        elif cfg.only_with_digital_bee and not hive.has_digital_bee:
            text, color = ("✖  No Digital Bee in your hive, so no drives will be bought.\n"
                           "    Add Digital Bee on the Hive tab if you have it."), WARN
        else:
            order = preferred_drives(cfg, hive)
            demo = plan_drive(cfg, hive, round_no=max(cfg.start_round, 5), quest_type="collect_color_pollen",
                              cogs=None, offered={d: None for d in DRIVE_TYPES})
            text = (f"✔  Digital Bee found. Will buy the {DRIVE_LABELS[order[0]]} "
                    f"({hive.color} hive), then {', '.join(DRIVE_LABELS[d] for d in order[1:])}.\n"
                    f"    When: {cfg.use_on.replace('_', ' ')}, from round {cfg.start_round}"
                    f"{'' if not cfg.max_per_run else f', max {cfg.max_per_run} per run'}"
                    f"{', used right away' if demo.use else ''}.")
            color = OK
        self.drive_banner.configure(text=text, fg=color)

    # --------------------------------------------------------------- hotbar
    def _tab_hotbar(self) -> None:
        body = self._settings_tab("Hotbar", "Tell the agent what is in each hotbar slot (keys 1-7) and "
                                  "when to use it. The wiki confirms consumables and field boosts work "
                                  "during RBC rounds (planters and Beesmas items don't). Drives in the "
                                  "hotbar follow the Digital Bee rule from the Drives tab.")
        try:
            self.item_db = json.loads((DATA_DIR / "hotbar_items.json").read_text(encoding="utf-8"))["items"]
        except (OSError, ValueError, KeyError):
            self.item_db = {}
        sec = self._section(body, "General")
        self._bvar(("hotbar", "enabled"), "Use hotbar items automatically", sec, 0)
        self._num(("hotbar", "min_gap_s"), "Min gap between key presses (s)", sec, 1, 0, 5, 0.1, True)
        self._num(("hotbar", "mobs_nearby_count"), "'Mobs nearby' means at least", sec, 2, 1, 20, hint="mobs on screen")
        v = self._track(self.binder.var(("hotbar", "backpack_full_at"), tk.DoubleVar(), _f2))
        labeled(sec, 3, "'Backpack full' means", lambda p: PercentScale(p, v, 0.5, 1.0))

        sec = self._section(body, "Slots")
        heads = ("Key", "Item", "Use when", "Every (s)", "Max/round", "From round", "Low time (s)", "Rounds only", "")
        for c, h in enumerate(heads):
            ttk.Label(sec, text=h, style="Muted.TLabel").grid(row=0, column=c, sticky="w", padx=3)
        when_help = {"never": "never", "always": "on cooldown", "round_start": "round start",
                     "in_field": "while farming", "at_hive": "at hive", "backpack_full": "backpack full",
                     "mobs_nearby": "mobs nearby", "fixed_rounds": "rounds 5/10/15/20/25",
                     "low_time": "time running out"}
        self._when_from_label = {v: k for k, v in when_help.items()}
        self._hb_rows: dict[int, dict[str, tk.Variable]] = {}
        for n in range(1, 8):
            r = {"item": tk.StringVar(), "use_when": tk.StringVar(value="never"),
                 "every_s": tk.DoubleVar(value=60.0), "max_per_round": tk.IntVar(value=0),
                 "from_round": tk.IntVar(value=1), "low_time_s": tk.DoubleVar(value=60.0),
                 "only_during_rounds": tk.BooleanVar(value=True)}
            for var in r.values():
                self._track(var)
            self._hb_rows[n] = r
            row = n
            tk.Label(sec, text=str(n), bg=PANEL, fg=ACCENT, width=3, font=("Segoe UI", 11, "bold")).grid(
                row=row, column=0, padx=3, pady=3)
            ttk.Combobox(sec, textvariable=r["item"], values=[""] + list(self.item_db), width=16).grid(
                row=row, column=1, padx=3)
            wl = tk.StringVar(value=when_help["never"])
            r["_when_label"] = wl
            cb = ttk.Combobox(sec, textvariable=wl, values=[when_help[w] for w in HOTBAR_WHEN],
                              state="readonly", width=16)
            cb.grid(row=row, column=2, padx=3)
            wl.trace_add("write", lambda *_, r=r: r["use_when"].set(self._when_from_label.get(r["_when_label"].get(), "never")))
            r["use_when"].trace_add("write", lambda *_, r=r, wh=when_help: r["_when_label"].set(wh.get(r["use_when"].get(), "never")))
            ttk.Spinbox(sec, from_=0.5, to=7200, increment=5, textvariable=r["every_s"], width=7).grid(row=row, column=3, padx=3)
            ttk.Spinbox(sec, from_=0, to=99, textvariable=r["max_per_round"], width=5).grid(row=row, column=4, padx=3)
            ttk.Spinbox(sec, from_=1, to=25, textvariable=r["from_round"], width=5).grid(row=row, column=5, padx=3)
            ttk.Spinbox(sec, from_=0, to=300, increment=5, textvariable=r["low_time_s"], width=6).grid(row=row, column=6, padx=3)
            ttk.Checkbutton(sec, variable=r["only_during_rounds"]).grid(row=row, column=7)
            ttk.Button(sec, text="Suggest", width=8, command=lambda n=n: self._hb_suggest(n)).grid(row=row, column=8, padx=3)
        ttk.Label(body, text="Tip: 'Suggest' fills in sensible settings for the item you picked. Item "
                  "durations are typical values; check them in your game.", style="Hint.TLabel").pack(anchor="w")
        self.binder.bind(("hotbar", "slots"), self._hb_get, self._hb_set)

    def _hb_suggest(self, n: int) -> None:
        r = self._hb_rows[n]
        info = self.item_db.get(r["item"].get())
        if not info:
            messagebox.showinfo("Hotbar", "Pick an item from the list first.")
            return
        r["use_when"].set(info.get("suggest", "never"))
        r["every_s"].set(float(info.get("every_s", 60)))

    def _hb_get(self) -> list[dict]:
        out = []
        for n, r in self._hb_rows.items():
            item, when = r["item"].get().strip(), r["use_when"].get()
            if not item and when == "never":
                continue
            out.append({"slot": n, "item": item, "use_when": when, "every_s": _f2(r["every_s"].get()),
                        "max_per_round": int(r["max_per_round"].get()), "from_round": int(r["from_round"].get()),
                        "low_time_s": _f2(r["low_time_s"].get()),
                        "only_during_rounds": bool(r["only_during_rounds"].get())})
        return out

    def _hb_set(self, slots: list[dict]) -> None:
        by = {s["slot"]: s for s in slots}
        for n, r in self._hb_rows.items():
            s = by.get(n, {})
            r["item"].set(s.get("item", ""))
            r["use_when"].set(s.get("use_when", "never"))
            r["every_s"].set(s.get("every_s", 60.0))
            r["max_per_round"].set(s.get("max_per_round", 0))
            r["from_round"].set(s.get("from_round", 1))
            r["low_time_s"].set(s.get("low_time_s", 60.0))
            r["only_during_rounds"].set(s.get("only_during_rounds", True))

    # --------------------------------------------------------------- combat
    def _tab_combat(self) -> None:
        body = self._settings_tab("Combat", "How to dodge and fight Robo Bear's mobs. Tactics come from "
                                  "the Bee Swarm Simulator wiki.")
        sec = self._section(body, "Dodging")
        self._bvar(("combat", "dodge_enabled"), "Dodge mob attacks", sec, 0)
        self._combo(("combat", "mechsquito_dodge"), "Vs Mechsquitos", sec, 1, ["circle", "zigzag", "none"],
                    "they aim where you ARE, so keep moving", width=10)
        self._num(("combat", "dodge_radius"), "Dodge pattern size", sec, 2, 0.2, 3.0, 0.1, True)
        self._num(("combat", "dodge_at_toxin_stacks"), "Start dodging at toxin stacks", sec, 3, 0, 10,
                  hint="0 = as soon as one is seen")
        self._bvar(("combat", "sidestep_cogturrets"), "Sidestep Cogturret shots (they fire straight ahead)", sec, 4)
        self._num(("combat", "sidestep_s"), "Sidestep for (s)", sec, 5, 0.1, 3, 0.1, True)
        self._bvar(("combat", "avoid_cogmowers"), "Step out of Cogmower paths", sec, 6)
        self._bvar(("combat", "jump_while_dodging"), "Jump while dodging", sec, 7)
        self._num(("combat", "leave_field_at_toxin_stacks"), "Leave the field at toxin stacks", sec, 8, 0, 10,
                  hint="0 = never")

        sec = self._section(body, "Fighting")
        self._bvar(("combat", "stand_near_mobs"), "Walk near mobs so bees lock on", sec, 0)
        self._bvar(("combat", "prioritize_cogturrets"), "Kill Cogturrets first (they pile up)", sec, 1)
        self._bvar(("combat", "hunt_golden_cogmower"), "Hunt the Golden Cogmower (14-20 Cogs)", sec, 2)
        self._num(("combat", "golden_max_detour_s"), "Max detour for it (s)", sec, 3, 0, 120, 5, True)

        sec = self._section(body, "Distances (field units, 1 ≈ your character's width)")
        self._num(("combat", "danger_line_width"), "Shot / path half-width", sec, 0, 0.5, 5, 0.1, True)
        self._num(("combat", "cogmower_alert_range"), "Cogmower alert range", sec, 1, 1, 15, 0.5, True)
        self._num(("combat", "approach_min_dist"), "Close enough to a mob", sec, 2, 0.5, 10, 0.5, True)
        self._num(("combat", "approach_range"), "Max distance to walk to a mob", sec, 3, 1, 30, 0.5, True)

        mobs = (self.rules.raw.get("mobs", {}) if self.rules else {})
        if mobs:
            sec = self._section(body, "Mob guide")
            for i, (name, m) in enumerate(mobs.items()):
                when = (f"from round {m['from_round']}" if "from_round" in m
                        else "rounds " + " / ".join("-".join(map(str, w)) for w in m.get("spawn_rounds", [])))
                tip = m.get("dodge") or m.get("tip", "")
                ttk.Label(sec, text=name, style="H2.TLabel").grid(row=i * 2, column=0, sticky="w", pady=(6, 0))
                ttk.Label(sec, text=when, style="Muted.TLabel").grid(row=i * 2, column=1, sticky="w", padx=10)
                ttk.Label(sec, text=tip, wraplength=760).grid(row=i * 2 + 1, column=0, columnspan=3, sticky="w")

    # ------------------------------------------------------------ screenshot
    def _screenshot(self) -> None:
        delay = self.cfg_mgr.config.capture.screenshot_delay_s
        self.btn_shot.configure(state="disabled")
        self.log.action(f"Screenshot in {delay} s: click into Roblox now")
        self._shot_countdown(delay)

    def _shot_countdown(self, left: int) -> None:
        if left > 0:
            self.btn_shot.configure(text=f"📸 {left}…")
            self.root.after(1000, self._shot_countdown, left - 1)
            return
        self.btn_shot.configure(text="📸 …")
        threading.Thread(target=self._shot_worker, daemon=True).start()

    def _shot_worker(self) -> None:
        cfg = self.cfg_mgr.config
        folder = None
        try:
            from ..perception.capture import ScreenCapture
            from ..perception.debug_dump import DebugDumper, overlay_regions
            cap = ScreenCapture(cfg)
            w = cap.locate(force=True)
            full = cap.grab_client(fresh=True)
            dump = DebugDumper(cfg)
            p = dump.save(full.image, "client")
            dump.save(overlay_regions(full.image, cfg, cap.client), "regions_overlay")
            if cfg.capture.save_region_crops:
                for name in cfg.regions:
                    dump.save(cap.grab_region(name).image, f"region_{name}")
            folder = p.parent
            self.log.action("Screenshot saved", size=f"{w.width}x{w.height}", dpi=f"{w.dpi_scale:.2f}",
                            focused=w.focused, folder=str(folder))
            if not w.focused:
                self.log.warning("Roblox wasn't the active window. If the image shows something "
                                 "else on top, try again and click into Roblox during the countdown.")
        except Exception as exc:  # show any failure in the log, never crash the GUI
            self.log.error(f"Screenshot failed: {exc}")
        self.root.after(0, self._shot_done, folder)

    def _shot_done(self, folder) -> None:
        self.btn_shot.configure(state="normal", text="📸 Screenshot")
        if folder is not None:
            self._open_path(folder)

    # -------------------------------------------------------------- farming
    def _tab_farming(self) -> None:
        body = self._settings_tab("Farming", "When to go back to the hive, how to move in a field and "
                                  "which tokens are worth chasing.")
        sec = self._section(body, "Pollen / backpack")
        for i, (k, label, hint) in enumerate([
                ("continue_below", "Keep farming below", "always stay in the field under this"),
                ("consider_return_above", "Think about returning above", "returns if time/distance make sense"),
                ("must_return_above", "Always return above", "backpack basically full"),
                ("return_if_objective_below", "Return early if quest progress below", "")]):
            v = self._track(self.binder.var(("pollen", k), tk.DoubleVar(), _f2))
            labeled(sec, i, label, lambda p, v=v: PercentScale(p, v), hint)
        self._num(("pollen", "low_time_s"), "Don't return with less than (s) left", sec, 4, 0, 300, 1, True)
        self._num(("pollen", "travel_safety_margin_s"), "Travel safety margin (s)", sec, 5, 0, 60, 0.5, True)
        self._num(("pollen", "convert_timeout_s"), "Give up converting after (s)", sec, 6, 5, 600, 5, True)

        sec = self._section(body, "Movement")
        self._combo(("movement", "farm_pattern"), "Farm pattern", sec, 0,
                    ["snake", "square", "circle", "stationary"], width=12)
        self._num(("movement", "pattern_scale"), "Pattern size", sec, 1, 0.1, 3.0, 0.1, True)
        self._num(("movement", "recenter_interval_s"), "Re-centre every (s)", sec, 2, 5, 300, 5, True)
        self._num(("movement", "stuck_window_s"), "Stuck if no motion for (s)", sec, 3, 0.5, 10, 0.5, True)
        self._num(("movement", "max_corrective_attempts"), "Unstuck attempts", sec, 4, 0, 10)

        sec = self._section(body, "Tokens")
        for i, k in enumerate(("generic", "boost", "haste", "focus", "inspire", "bomb", "link")):
            self._num(("tokens", "values", k), k.title(), sec, i // 2, 0, 200, 5, True, col=(i % 2) * 3)
        self._num(("tokens", "min_score"), "Min score to chase", sec, 4, 0, 200, 1, True)
        self._num(("tokens", "max_chase_s"), "Max seconds per token", sec, 4, 0.5, 20, 0.5, True, col=3)

    # ------------------------------------------------------------- controls
    def _tab_controls(self) -> None:
        body = self._settings_tab("Controls", "Hotkeys, in-game key bindings, run limits and the window "
                                  "the agent looks for.")
        sec = self._section(body, "Hotkeys")
        self._combo(("hotkeys", "pause"), "Pause", sec, 0, HOTKEYS, width=12)
        self._combo(("hotkeys", "resume"), "Resume", sec, 1, HOTKEYS, width=12)
        self._combo(("hotkeys", "emergency_stop"), "Emergency stop", sec, 2, HOTKEYS, width=12)

        sec = self._section(body, "In-game keys (match your Roblox settings)")
        keys = ("forward", "back", "left", "right", "jump", "interact", "camera_left",
                "camera_right", "zoom_in", "zoom_out")
        for i, k in enumerate(keys):
            v = self._track(self.binder.var(("input", "key_bindings", k), tk.StringVar(), lambda s: s.strip().lower()))
            labeled(sec, i // 2, k.replace("_", " ").title(), lambda p, v=v: ttk.Entry(p, textvariable=v, width=8),
                    col=(i % 2) * 3)

        sec = self._section(body, "Run limits")
        self._num(("run", "max_runs"), "RBC runs in a row", sec, 0, 0, 999, hint="0 = forever")
        self._num(("run", "max_rounds"), "Stop after round", sec, 1, 0, 25, hint="0 = play to the end")
        self._num(("run", "max_runtime_min"), "Stop after (minutes)", sec, 2, 0, 1440, 5, True, hint="0 = no limit")
        self._num(("run", "stop_after_consecutive_failed_runs"), "Stop after failed runs in a row", sec, 3, 1, 50)

        sec = self._section(body, "Window")
        v = self._track(self.binder.var(("window", "title_contains"), tk.StringVar()))
        labeled(sec, 0, "Window title contains", lambda p: ttk.Entry(p, textvariable=v, width=20))
        self._bvar(("window", "require_focus"), "Only send input while Roblox is focused", sec, 1)
        self._bvar(("window", "refocus_automatically"), "Re-focus Roblox automatically (else pause)", sec, 2)
        self._bvar(("debug", "save_screenshots_on_failure"), "Save a screenshot when something fails", sec, 3)

        sec = self._section(body, "Screen capture")
        self._num(("capture", "screenshot_delay_s"), "Screenshot countdown (s)", sec, 0, 0, 30,
                  hint="time to click into Roblox after pressing 📸")
        self._bvar(("capture", "save_region_crops"), "Also save each screen region on its own", sec, 1)
        self._num(("capture", "cache_max_age_ms"), "Reuse a grab for (ms)", sec, 2, 0, 2000, 10)
        self._num(("debug", "max_screenshots"), "Keep at most N debug images", sec, 3, 0, 5000, 10, hint="0 = keep all")

    # ================================================================== loop
    def _tick(self) -> None:
        version, snap = self.status.read()
        if version != self._shown_version:
            for label, value in format_rows(snap):
                self._value_labels[label].configure(text=value)
            self._shown_version = version
            st = snap.status
            col = {"IDLE": MUTED, "RUNNING": OK, "PAUSED": WARN, "STOPPED": BAD}.get(st, ACCENT)
            self.pill.configure(text=f" {st} ", fg=col)
        self._drain_log()
        self.root.after(100, self._tick)

    def _drain_log(self, max_items: int = 200) -> None:
        items = []
        try:
            for _ in range(max_items):
                items.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if not items:
            return
        t = self.log_text
        at_bottom = t.yview()[1] > 0.98
        t.configure(state="normal")
        for ev in items:
            tag = Category.ERROR.value if ev.level >= logging.ERROR else ev.category.value
            t.insert("end", ev.format() + "\n", tag)
        extra = int(t.index("end-1c").split(".")[0]) - LOG_LINES_MAX
        if extra > 0:
            t.delete("1.0", f"{extra + 1}.0")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    # ================================================================ actions
    def _set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        if hasattr(self, "save_lbl"):
            self.save_lbl.configure(text="● unsaved changes" if dirty else "all changes saved",
                                    foreground=WARN if dirty else MUTED)
        if dirty and hasattr(self, "hive_stats"):
            self._update_drive_preview()

    def _read_user_file(self) -> dict | None:
        path = USER_CONFIG_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError as exc:
            messagebox.showerror("config.json", f"config.json is not valid JSON (line {exc.lineno}). Fix it first.")
            return None

    def _save(self) -> None:
        base = self._read_user_file()
        if base is None:
            return
        try:
            data = self.binder.overrides(base)
        except tk.TclError as exc:
            messagebox.showerror("Can't save", f"A number field has an invalid value:\n{exc}")
            return
        try:
            parse_config({k: v for k, v in data.items() if not k.startswith("_")})
        except ConfigError as exc:
            messagebox.showerror("Can't save: invalid settings", str(exc))
            return
        USER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if self._reload_config(quiet=True):
            self._set_dirty(False)
            self.log.config("Settings saved", file=USER_CONFIG_FILE.name,
                            overrides=len([k for k in data if not k.startswith("_")]))

    def _revert(self) -> None:
        if self.dirty and not messagebox.askyesno("Revert", "Throw away unsaved changes?"):
            return
        self.binder.load(self.cfg_mgr.config)
        self._hive_from_cfg(self.cfg_mgr.config.hive)
        self._set_dirty(False)

    def _start(self) -> None:
        checks = run_preflight(self.cfg_mgr.config)
        ready = can_start(checks)
        for c in checks:
            (self.log.config if c.ok else self.log.warning)(f"Pre-flight  {c.name}: {c.detail}")
        if self.dirty:
            self.log.warning("You have unsaved settings. Save them before starting.")
        self._show_preflight(checks, ready)

    def _show_preflight(self, checks, ready: bool) -> None:
        w = tk.Toplevel(self.root, bg=BG, padx=18, pady=14)
        w.title("Pre-flight check")
        w.transient(self.root)
        w.resizable(False, False)
        tk.Label(w, text="Ready to start" if ready else "Can't start yet", bg=BG,
                 fg=OK if ready else BAD, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        for c in checks:
            mark, col = ("✔", OK) if c.ok else (("✖", BAD) if c.blocking else ("!", WARN))
            row = tk.Frame(w, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=mark, bg=BG, fg=col, width=2, font=("Segoe UI", 10, "bold")).pack(side="left")
            tk.Label(row, text=c.name, bg=BG, fg=FG, width=30, anchor="w").pack(side="left")
            tk.Label(row, text=c.detail, bg=BG, fg=MUTED, anchor="w").pack(side="left")
        if not ready:
            tk.Label(w, bg=BG, fg=MUTED, justify="left", wraplength=560, pady=10, text=(
                "This build is stage 1 of 16: settings, logging and this window. The parts that "
                "look at the game and press keys (marked ✖ 'not built yet') don't exist yet, so "
                "START has nothing to run. Your settings are saved and will be used once they land."
            )).pack(anchor="w")
        ttk.Button(w, text="OK", command=w.destroy).pack(anchor="e", pady=(6, 0))
        w.grab_set()

    def _reload_config(self, quiet: bool = False) -> bool:
        try:
            cfg = self.cfg_mgr.reload()
        except ConfigError as exc:
            self.log.error("Config reload failed; keeping previous config")
            for p in exc.problems:
                self.log.error(p)
            messagebox.showerror("Invalid config", str(exc))
            return False
        self.log = setup_logging(cfg.logging, gui_queue=self.log_queue)
        self.debug_var.set(cfg.debug.enabled)
        self.status.publish(strategy=cfg.strategy.profile.value)
        if not quiet:
            self.binder.load(cfg)
            self._hive_from_cfg(cfg.hive)
            self._set_dirty(False)
            self.log.config("Configuration reloaded", profile=cfg.strategy.profile.value)
        return True

    def _toggle_debug(self) -> None:
        """Persist the checkbox into config.json, then reload."""
        data = self._read_user_file()
        if data is None:
            self.debug_var.set(not self.debug_var.get())
            return
        data.setdefault("debug", {})["enabled"] = bool(self.debug_var.get())
        USER_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._reload_config(quiet=True)
        self.log.config("Debug mode " + ("on" if self.debug_var.get() else "off"))

    def _open_config(self) -> None:
        self.cfg_mgr.ensure_user_file()
        self._open_path(USER_CONFIG_FILE)

    def _open_path(self, path) -> None:
        if path.suffix == "":
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # Windows
        except AttributeError:
            messagebox.showinfo("Path", str(path))

    def _on_close(self) -> None:
        if self.dirty and not messagebox.askyesno("Unsaved settings", "You have unsaved settings. Quit anyway?"):
            return
        # Stage 6+: stop automation thread and release all keys here first.
        self.root.destroy()


def run_gui() -> int:
    mgr = ConfigManager()
    mgr.ensure_user_file()
    root = tk.Tk()
    try:
        mgr.load()
    except ConfigError as exc:
        messagebox.showerror("Invalid config, using defaults", str(exc), parent=root)
    ControlPanel(root, mgr)
    root.mainloop()
    return 0
