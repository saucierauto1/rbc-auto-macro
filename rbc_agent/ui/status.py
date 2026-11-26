"""What the control panel shows, kept separate from Tk so it can be tested.

The automation thread publishes ``StatusSnapshot`` objects to a ``StatusBus``.
The GUI polls the bus about 10 times a second and redraws. Neither side ever
blocks the other.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class StatusSnapshot:
    status: str = "IDLE"
    rbc_round: int | None = None
    current_field: str = ""
    pollen: int | None = None
    capacity: int | None = None
    objective: str = ""
    strategy: str = ""
    action: str = ""
    run_time_s: float = 0.0
    recovery_count: int = 0
    detection_confidence: float | None = None


def _num(n: int | None) -> str:
    if n is None:
        return "–"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            return f"{n / div:.2f}{suffix}"
    return str(n)


def format_rows(s: StatusSnapshot) -> list[tuple[str, str]]:
    """Label/value pairs in display order."""
    if s.capacity:
        pct = f" ({100 * (s.pollen or 0) / s.capacity:.0f}%)"
    else:
        pct = ""
    h, rem = divmod(int(s.run_time_s), 3600)
    m, sec = divmod(rem, 60)
    conf = "–" if s.detection_confidence is None else f"{s.detection_confidence:.0%}"
    return [
        ("STATUS", s.status),
        ("RBC ROUND", "–" if s.rbc_round is None else f"{s.rbc_round} / 25"),
        ("CURRENT FIELD", s.current_field or "–"),
        ("POLLEN", _num(s.pollen) + pct),
        ("CAPACITY", _num(s.capacity)),
        ("CURRENT OBJECTIVE", s.objective or "–"),
        ("CURRENT STRATEGY", s.strategy or "–"),
        ("CURRENT ACTION", s.action or "–"),
        ("RUN TIME", f"{h:02d}:{m:02d}:{sec:02d}"),
        ("RECOVERY COUNT", str(s.recovery_count)),
        ("DETECTION CONFIDENCE", conf),
    ]


class StatusBus:
    """Thread-safe holder of the latest snapshot plus a change counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = StatusSnapshot()
        self._version = 0

    def publish(self, **changes) -> None:
        with self._lock:
            self._snap = replace(self._snap, **changes)
            self._version += 1

    def read(self) -> tuple[int, StatusSnapshot]:
        with self._lock:
            return self._version, self._snap
