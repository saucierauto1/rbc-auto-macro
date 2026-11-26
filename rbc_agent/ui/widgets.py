"""Theme + reusable Tk widgets for the control panel."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

BG = "#14161b"
PANEL = "#1d2028"
PANEL_2 = "#252a34"
BORDER = "#2f3542"
FG = "#e6e8ee"
MUTED = "#8a92a6"
ACCENT = "#f2b632"
OK = "#7ddc8a"
BAD = "#ff6b6b"
WARN = "#ffb35c"
COLOR_HEX = {"red": "#ff7070", "blue": "#6fb1ff", "white": "#e6e8ee", "mixed": "#c3a6ff"}
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_S = ("Segoe UI", 9)


def apply_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=BG)
    root.option_add("*TCombobox*Listbox.background", PANEL_2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#111")
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=FG, font=FONT, bordercolor=BORDER,
                lightcolor=PANEL, darkcolor=PANEL, troughcolor=PANEL, focuscolor=ACCENT)
    s.configure("TFrame", background=BG)
    s.configure("Panel.TFrame", background=PANEL)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_S)
    s.configure("Panel.TLabel", background=PANEL, foreground=FG)
    s.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=FONT_S)
    s.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Segoe UI", 11, "bold"))
    s.configure("Title.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 15, "bold"))
    s.configure("H2.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 11, "bold"))
    s.configure("Hint.TLabel", background=BG, foreground=MUTED, font=FONT_S, wraplength=820)
    s.configure("TLabelframe", background=BG, bordercolor=BORDER, relief="solid")
    s.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=FONT_B)
    s.configure("TButton", background=PANEL_2, foreground=FG, padding=(10, 5), borderwidth=1)
    s.map("TButton", background=[("disabled", PANEL), ("active", "#323846")],
          foreground=[("disabled", "#5a6070")])
    s.configure("Accent.TButton", background=ACCENT, foreground="#111", font=FONT_B)
    s.map("Accent.TButton", background=[("active", "#ffc94d"), ("disabled", PANEL)])
    s.configure("Danger.TButton", background="#5a2228", foreground=FG)
    s.map("Danger.TButton", background=[("active", "#7a2a32"), ("disabled", PANEL)])
    s.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=PANEL_2,
                indicatorforeground=ACCENT)
    s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", PANEL_2)])
    s.configure("TRadiobutton", background=BG, foreground=FG, indicatorbackground=PANEL_2)
    s.map("TRadiobutton", background=[("active", BG)])
    for w in ("TEntry", "TSpinbox", "TCombobox"):
        s.configure(w, fieldbackground=PANEL_2, foreground=FG, background=PANEL_2,
                    arrowcolor=FG, insertcolor=FG, padding=3)
    s.map("TCombobox", fieldbackground=[("readonly", PANEL_2)], foreground=[("readonly", FG)],
          selectbackground=[("readonly", PANEL_2)], selectforeground=[("readonly", FG)])
    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
    s.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 7), font=FONT_B)
    s.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", ACCENT)])
    s.configure("Treeview", background=PANEL_2, fieldbackground=PANEL_2, foreground=FG,
                rowheight=24, borderwidth=0)
    s.configure("Treeview.Heading", background=PANEL, foreground=MUTED, font=FONT_S, relief="flat")
    s.map("Treeview", background=[("selected", "#3b4252")])
    s.configure("Horizontal.TScale", background=BG, troughcolor=PANEL_2)
    s.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, arrowcolor=MUTED)
    return s


def listbox(parent, **kw) -> tk.Listbox:
    opts = dict(bg=PANEL_2, fg=FG, selectbackground=ACCENT, selectforeground="#111",
                highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                relief="flat", activestyle="none", font=FONT, exportselection=False)
    opts.update(kw)
    return tk.Listbox(parent, **opts)


class ScrollFrame(ttk.Frame):
    """A vertically scrolling frame. Put children in ``.body``."""

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=(16, 12))
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        # Let listboxes/trees under the cursor scroll themselves.
        w = self.winfo_containing(e.x_root, e.y_root)
        if isinstance(w, (tk.Listbox, ttk.Treeview, tk.Text)):
            return
        if self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")


class OrderedPicker(ttk.Frame):
    """Two lists: everything available on the left, your ordered choice on the right.

    ``items`` is a list of (key, label). ``get()`` returns the chosen keys in order.
    """

    def __init__(self, parent, items: list[tuple[str, str]], *, left_title="Available",
                 right_title="Your order (top = first)", height=10,
                 on_change: Callable[[], None] | None = None,
                 color_of: Callable[[str], str] | None = None):
        super().__init__(parent)
        self._items = dict(items)
        self._order = [k for k, _ in items]
        self._chosen: list[str] = []
        self._on_change = on_change
        self._color_of = color_of
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh())

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew")
        hdr = ttk.Frame(left)
        hdr.pack(fill="x")
        ttk.Label(hdr, text=left_title, style="Muted.TLabel").pack(side="left")
        ttk.Entry(hdr, textvariable=self.filter_var, width=14).pack(side="right")
        ttk.Label(hdr, text="search", style="Muted.TLabel").pack(side="right", padx=4)
        self.lb_avail = listbox(left, height=height, width=28)
        self.lb_avail.pack(fill="both", expand=True, pady=(4, 0))
        self.lb_avail.bind("<Double-Button-1>", lambda e: self.add())

        mid = ttk.Frame(self)
        mid.grid(row=0, column=1, padx=8)
        for text, cmd in (("Add →", self.add), ("← Remove", self.remove),
                          ("▲ Up", lambda: self.move(-1)), ("▼ Down", lambda: self.move(1)),
                          ("Clear", self.clear)):
            ttk.Button(mid, text=text, command=cmd, width=10).pack(pady=2)

        right = ttk.Frame(self)
        right.grid(row=0, column=2, sticky="nsew")
        ttk.Label(right, text=right_title, style="Muted.TLabel").pack(anchor="w")
        self.lb_chosen = listbox(right, height=height, width=28)
        self.lb_chosen.pack(fill="both", expand=True, pady=(4, 0))
        self.lb_chosen.bind("<Double-Button-1>", lambda e: self.remove())
        self.columnconfigure(0, weight=1)
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)

    # public -----------------------------------------------------------------
    def get(self) -> list[str]:
        return list(self._chosen)

    def set(self, keys: list[str]) -> None:
        self._chosen = [k for k in keys if k in self._items]
        self._refresh()

    def add(self) -> None:
        for i in self.lb_avail.curselection():
            key = self._avail_keys[i]
            if key not in self._chosen:
                self._chosen.append(key)
        self._refresh(changed=True)

    def remove(self) -> None:
        sel = set(self.lb_chosen.curselection())
        self._chosen = [k for i, k in enumerate(self._chosen) if i not in sel]
        self._refresh(changed=True)

    def move(self, d: int) -> None:
        sel = self.lb_chosen.curselection()
        if not sel:
            return
        i = sel[0]
        j = i + d
        if 0 <= j < len(self._chosen):
            self._chosen[i], self._chosen[j] = self._chosen[j], self._chosen[i]
            self._refresh(changed=True)
            self.lb_chosen.selection_set(j)
            self.lb_chosen.see(j)

    def clear(self) -> None:
        self._chosen = []
        self._refresh(changed=True)

    # internal ---------------------------------------------------------------
    def _refresh(self, changed: bool = False) -> None:
        f = self.filter_var.get().strip().lower()
        self._avail_keys = [k for k in self._order if k not in self._chosen
                            and (not f or f in self._items[k].lower())]
        self.lb_avail.delete(0, "end")
        for k in self._avail_keys:
            self.lb_avail.insert("end", self._items[k])
            self._paint(self.lb_avail, k)
        self.lb_chosen.delete(0, "end")
        for n, k in enumerate(self._chosen, 1):
            self.lb_chosen.insert("end", f"{n:>2}. {self._items[k]}")
            self._paint(self.lb_chosen, k)
        if changed and self._on_change:
            self._on_change()

    def _paint(self, lb: tk.Listbox, key: str) -> None:
        if self._color_of:
            lb.itemconfigure("end", foreground=COLOR_HEX.get(self._color_of(key), FG))


def labeled(parent, row: int, text: str, widget_factory, hint: str = "", col: int = 0):
    """Grid a label + widget (+ optional hint) on one row. Returns the widget."""
    ttk.Label(parent, text=text).grid(row=row, column=col, sticky="w", pady=4, padx=(0, 10))
    w = widget_factory(parent)
    w.grid(row=row, column=col + 1, sticky="w", pady=4)
    if hint:
        ttk.Label(parent, text=hint, style="Muted.TLabel").grid(row=row, column=col + 2, sticky="w", padx=10)
    return w


class PercentScale(ttk.Frame):
    """Slider for a 0..1 fraction with a live % readout."""

    def __init__(self, parent, var: tk.DoubleVar, lo: float = 0.3, hi: float = 1.0, length: int = 220):
        super().__init__(parent)
        self.var = var
        self.lbl = ttk.Label(self, width=5)
        ttk.Scale(self, from_=lo, to=hi, variable=var, length=length,
                  command=lambda *_: self._show()).pack(side="left")
        self.lbl.pack(side="left", padx=6)
        var.trace_add("write", lambda *_: self._show())
        self._show()

    def _show(self):
        try:
            self.lbl.configure(text=f"{self.var.get():.0%}")
        except tk.TclError:
            pass
