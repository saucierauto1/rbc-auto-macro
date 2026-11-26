"""Categorised event logging.

Output format (console, file and GUI share the same line format)::

    [17:42:10] STATE     RBC_FARMING
    [17:42:11] DETECT    Pollen: 72%  conf=0.94
    [17:42:15] DECISION  Continue farming  reason="capacity 91%, 82s left"

Each record also carries structured ``fields`` (dict), which the GUI and
statistics read without having to parse text.
"""

from __future__ import annotations

import logging
import queue
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import Any

from .paths import resolve

LOGGER_NAME = "rbc_agent"


class Category(str, Enum):
    STATE = "STATE"
    DETECT = "DETECT"
    DECISION = "DECISION"
    ACTION = "ACTION"
    RECOVERY = "RECOVERY"
    CONFIG = "CONFIG"
    STATS = "STATS"
    SYSTEM = "SYSTEM"
    ERROR = "ERROR"


@dataclass(frozen=True)
class LogEvent:
    """What the GUI receives for its live log."""

    time: datetime
    level: int
    category: Category
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        return format_line(self.time, self.category, self.message, self.fields)


def _fmt_value(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.2f}"
    s = str(v)
    return f'"{s}"' if " " in s else s


def format_line(t: datetime, category: Category, message: str, fields: dict[str, Any]) -> str:
    extra = "  ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    line = f"[{t:%H:%M:%S}] {category.value:<9} {message}"
    return f"{line}  {extra}" if extra else line


class _AgentFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cat = getattr(record, "category", Category.SYSTEM)
        fields = getattr(record, "fields", {})
        line = format_line(datetime.fromtimestamp(record.created), cat, record.getMessage(), fields)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class GuiQueueHandler(logging.Handler):
    """Pushes LogEvents onto a bounded queue for the Tk thread to drain.

    Never blocks the automation thread: if the GUI falls behind, the oldest
    events are dropped (they are still in the log file).
    """

    def __init__(self, q: "queue.Queue[LogEvent]"):
        super().__init__()
        self.queue = q

    def emit(self, record: logging.LogRecord) -> None:
        ev = LogEvent(
            time=datetime.fromtimestamp(record.created),
            level=record.levelno,
            category=getattr(record, "category", Category.SYSTEM),
            message=record.getMessage(),
            fields=dict(getattr(record, "fields", {})),
        )
        while True:
            try:
                self.queue.put_nowait(ev)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass


class AgentLogger:
    """Thin facade so call sites read like the log they produce.

    >>> log.state("RBC_FARMING", round=8)
    >>> log.decision("Continue farming", reason="capacity 91%", confidence=0.91)
    """

    def __init__(self, name: str = LOGGER_NAME):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, cat: Category, msg: str, fields: dict[str, Any],
              exc_info: bool = False) -> None:
        self._log.log(level, msg, extra={"category": cat, "fields": fields},
                      exc_info=exc_info, stacklevel=3)

    def state(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.STATE, msg, f)
    def detect(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.DETECT, msg, f)
    def detect_debug(self, msg: str, **f: Any) -> None: self._emit(logging.DEBUG, Category.DETECT, msg, f)
    def decision(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.DECISION, msg, f)
    def action(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.ACTION, msg, f)
    def recovery(self, msg: str, **f: Any) -> None: self._emit(logging.WARNING, Category.RECOVERY, msg, f)
    def config(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.CONFIG, msg, f)
    def stats(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.STATS, msg, f)
    def system(self, msg: str, **f: Any) -> None: self._emit(logging.INFO, Category.SYSTEM, msg, f)
    def warning(self, msg: str, **f: Any) -> None: self._emit(logging.WARNING, Category.SYSTEM, msg, f)

    def error(self, msg: str, exc: bool = False, **f: Any) -> None:
        self._emit(logging.ERROR, Category.ERROR, msg, f, exc_info=exc)


def setup_logging(cfg: "LoggingConfig", gui_queue: "queue.Queue[LogEvent] | None" = None) -> AgentLogger:  # noqa: F821
    """Configure handlers from config. Safe to call again after a config reload."""
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(cfg.level.upper())
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    fmt = _AgentFormatter()
    if cfg.console and sys.stdout is not None:  # None inside a windowed .exe
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    if cfg.file:
        path = resolve(cfg.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=cfg.max_bytes,
                                 backupCount=cfg.backup_count, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if gui_queue is not None:
        root.addHandler(GuiQueueHandler(gui_queue))
    return AgentLogger()


log = AgentLogger()
