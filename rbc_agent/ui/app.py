"""The RBC Agent control panel (Tkinter, main thread only).

Everything slow happens on other threads. The panel only reads the
``StatusBus`` and drains the log queue on a 100 ms timer, so the window stays
responsive whatever the automation is doing.

Stage 1: Reload Config, Debug mode and the live log work. Start / Pause /
Stop / Calibrate are visible but disabled until their stages exist.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import tkinter as tk
from tkinter import messagebox, ttk

from .. import __version__
from ..core.config import ConfigManager
from ..core.errors import ConfigError
from ..core.logger import Category, LogEvent, setup_logging
from ..core.paths import LOGS_DIR, USER_CONFIG_FILE
from .status import StatusBus, format_rows

BG, PANEL, FG, MUTED, ACCENT = "#16181d", "#1f232b", "#e6e8ee", "#8a92a6", "#f2b632"
CAT_COLORS = {
    Category.STATE: "#7cc4ff", Category.DECISION: "#f2b632", Category.ACTION: "#9be39b",
    Category.DETECT: "#c3a6ff", Category.RECOVERY: "#ff9e57", Category.ERROR: "#ff6b6b",
    Category.CONFIG: "#8a92a6", Category.SYSTEM: "#8a92a6", Category.STATS: "#8a92a6",
}
LOG_LINES_MAX = 2000


class ControlPanel:
    def __init__(self, root: tk.Tk, config_mgr: ConfigManager):
        self.root = root
        self.cfg_mgr = config_mgr
        self.log_queue: "queue.Queue[LogEvent]" = queue.Queue(maxsize=5000)
        self.status = StatusBus()
        self._shown_version = -1
        self.log = setup_logging(self.cfg_mgr.config.logging, gui_queue=self.log_queue)

        self._build_window()
        self.status.publish(strategy=self.cfg_mgr.config.strategy.profile.value)
        self.log.system(f"RBC Agent {__version__} started")
        self.log.config("Configuration OK", profile=self.cfg_mgr.config.strategy.profile.value)
        self.log.warning("Automation is not built yet (Stage 5+). Start is disabled.")
        self._tick()

    # ------------------------------------------------------------------ layout
    def _build_window(self) -> None:
        r = self.root
        r.title(f"RBC Agent {__version__}")
        r.geometry("760x620")
        r.minsize(620, 480)
        r.configure(bg=BG)
        r.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Label.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Segoe UI", 10, "bold"))
        style.configure("Title.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 14, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=6)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])

        ttk.Label(r, text="ROBO BEAR CHALLENGE AGENT", style="Title.TLabel").pack(
            anchor="w", padx=14, pady=(12, 6))

        # status grid
        grid = ttk.Frame(r, style="Panel.TFrame", padding=10)
        grid.pack(fill="x", padx=14)
        self._value_labels: dict[str, ttk.Label] = {}
        for i, (label, _) in enumerate(format_rows(self.status.read()[1])):
            row, col = divmod(i, 2)
            ttk.Label(grid, text=label, style="Label.TLabel").grid(
                row=row, column=col * 2, sticky="w", padx=(0, 8), pady=2)
            v = ttk.Label(grid, text="–", style="Value.TLabel")
            v.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 24), pady=2)
            self._value_labels[label] = v
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        # buttons
        bar = ttk.Frame(r, padding=(14, 10))
        bar.pack(fill="x")
        hk = self.cfg_mgr.config.hotkeys
        self.btn_start = ttk.Button(bar, text="▶ START", state="disabled")
        self.btn_pause = ttk.Button(bar, text=f"⏸ PAUSE ({hk.pause})", state="disabled")
        self.btn_stop = ttk.Button(bar, text=f"■ STOP ({hk.emergency_stop})", state="disabled")
        self.btn_reload = ttk.Button(bar, text="⟳ RELOAD CONFIG", command=self._reload_config)
        self.btn_calib = ttk.Button(bar, text="⌖ CALIBRATE", state="disabled")
        for b in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_reload, self.btn_calib):
            b.pack(side="left", padx=(0, 6))
        self.debug_var = tk.BooleanVar(value=self.cfg_mgr.config.debug.enabled)
        ttk.Checkbutton(bar, text="DEBUG MODE", variable=self.debug_var,
                        command=self._toggle_debug).pack(side="left", padx=8)

        # live log
        logf = ttk.Frame(r, padding=(14, 0, 14, 8))
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, bg="#0f1115", fg=FG, insertbackground=FG,
                                font=("Consolas", 9), wrap="none", relief="flat",
                                state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for cat, color in CAT_COLORS.items():
            self.log_text.tag_configure(cat.value, foreground=color)

        foot = ttk.Frame(r, padding=(14, 0, 14, 10))
        foot.pack(fill="x")
        ttk.Button(foot, text="Open config file", command=self._open_config).pack(side="left")
        ttk.Button(foot, text="Open logs folder", command=lambda: self._open_path(LOGS_DIR)).pack(
            side="left", padx=6)

    # ------------------------------------------------------------------ loop
    def _tick(self) -> None:
        version, snap = self.status.read()
        if version != self._shown_version:
            for label, value in format_rows(snap):
                self._value_labels[label].configure(text=value)
            self._shown_version = version
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

    # ------------------------------------------------------------------ actions
    def _reload_config(self) -> None:
        try:
            cfg = self.cfg_mgr.reload()
        except ConfigError as exc:
            self.log.error("Config reload failed; keeping previous config")
            for p in exc.problems:
                self.log.error(p)
            messagebox.showerror("Invalid config", str(exc))
            return
        self.log = setup_logging(cfg.logging, gui_queue=self.log_queue)
        self.debug_var.set(cfg.debug.enabled)
        self.status.publish(strategy=cfg.strategy.profile.value)
        self.log.config("Configuration reloaded", profile=cfg.strategy.profile.value)

    def _toggle_debug(self) -> None:
        """Persist the checkbox into config.json, then reload."""
        path = USER_CONFIG_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError:
            messagebox.showerror("config.json", "config.json is not valid JSON; fix it first.")
            self.debug_var.set(not self.debug_var.get())
            return
        data.setdefault("debug", {})["enabled"] = bool(self.debug_var.get())
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._reload_config()

    def _open_config(self) -> None:
        self.cfg_mgr.ensure_user_file()
        self._open_path(USER_CONFIG_FILE)

    def _open_path(self, path) -> None:
        if path.suffix == "":           # a folder: make sure it exists
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]  # Windows
        except AttributeError:
            messagebox.showinfo("Path", str(path))

    def _on_close(self) -> None:
        # Stage 6+: stop automation thread and release all keys here first.
        self.root.destroy()


def run_gui() -> int:
    mgr = ConfigManager()
    mgr.ensure_user_file()
    root = tk.Tk()
    try:
        mgr.load()
    except ConfigError as exc:
        # Still open the window with defaults so the user can see and fix it.
        messagebox.showerror("Invalid config, using defaults", str(exc), parent=root)
    ControlPanel(root, mgr)
    root.mainloop()
    return 0
