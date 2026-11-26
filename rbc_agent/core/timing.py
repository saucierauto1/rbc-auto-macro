"""Timing primitives: the replacement for scattered ``sleep()`` calls.

Everything that waits takes a ``Clock`` and a ``CancelToken``, which gives two
properties:

* **Tests run instantly.** ``FakeClock`` advances virtual time on ``sleep``,
  so a 15-second timeout test finishes in microseconds.
* **Every wait can be cancelled.** Each poll checks the token, so emergency
  stop interrupts any wait within one poll interval. Time spent *paused* is
  not counted against a timeout.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Generic, Protocol, TypeVar

from .errors import AutomationCancelled, OperationTimeout

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Clocks
# --------------------------------------------------------------------------- #


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class MonotonicClock:
    """Real clock. Uses monotonic time, so system clock changes don't matter."""

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests. ``sleep`` advances virtual time.

    ``on_sleep`` hooks let a test change the world at a given virtual time,
    e.g. make a UI element "appear" after 3 s.
    """

    def __init__(self, start: float = 0.0):
        self._t = start
        self.on_sleep: list[Callable[[float], None]] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += max(0.0, seconds)
        for hook in list(self.on_sleep):
            hook(self._t)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


DEFAULT_CLOCK: Clock = MonotonicClock()


# --------------------------------------------------------------------------- #
# Cancellation / pause
# --------------------------------------------------------------------------- #


class RunMode(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class CancelToken:
    """Shared run/pause/stop flag, set by hotkeys, the GUI or recovery.

    Thread-safe. STOPPED is terminal: once stopped, a token can't be resumed
    (create a new one for a new run). This guarantees that a stop is never
    undone by a stray resume.
    """

    def __init__(self) -> None:
        self._mode = RunMode.RUNNING
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self.stop_reason: str = ""

    @property
    def mode(self) -> RunMode:
        with self._lock:
            return self._mode

    @property
    def stopped(self) -> bool:
        return self.mode is RunMode.STOPPED

    @property
    def paused(self) -> bool:
        return self.mode is RunMode.PAUSED

    def pause(self) -> None:
        self._set(RunMode.PAUSED)

    def resume(self) -> None:
        self._set(RunMode.RUNNING)

    def stop(self, reason: str = "stop requested") -> None:
        with self._lock:
            if self._mode is not RunMode.STOPPED:
                self.stop_reason = reason
            self._mode = RunMode.STOPPED
            self._changed.notify_all()

    def _set(self, mode: RunMode) -> None:
        with self._lock:
            if self._mode is RunMode.STOPPED:
                return
            self._mode = mode
            self._changed.notify_all()

    def check(self) -> None:
        """Raise AutomationCancelled if stopped. Call this in loops."""
        if self.stopped:
            raise AutomationCancelled(self.stop_reason)

    def wait_if_paused(self, clock: Clock = DEFAULT_CLOCK, poll_s: float = 0.05) -> float:
        """Block while paused. Returns the time spent paused.

        Raises AutomationCancelled if the token is stopped while waiting.
        """
        start = clock.now()
        while True:
            mode = self.mode
            if mode is RunMode.STOPPED:
                raise AutomationCancelled(self.stop_reason)
            if mode is RunMode.RUNNING:
                return clock.now() - start
            if isinstance(clock, MonotonicClock):
                with self._lock:
                    self._changed.wait(timeout=poll_s)
            else:
                clock.sleep(poll_s)


# --------------------------------------------------------------------------- #
# Deadlines and waits
# --------------------------------------------------------------------------- #


class Deadline:
    """A point in time that can be pushed back (e.g. by time spent paused)."""

    def __init__(self, timeout_s: float, clock: Clock = DEFAULT_CLOCK):
        self._clock = clock
        self._start = clock.now()
        self._end = self._start + timeout_s
        self.timeout_s = timeout_s

    def extend(self, seconds: float) -> None:
        self._end += seconds

    @property
    def remaining(self) -> float:
        return max(0.0, self._end - self._clock.now())

    @property
    def expired(self) -> bool:
        return self._clock.now() >= self._end

    @property
    def elapsed(self) -> float:
        return self._clock.now() - self._start


@dataclass(frozen=True)
class WaitResult(Generic[T]):
    ok: bool
    value: T | None
    elapsed_s: float
    polls: int

    def __bool__(self) -> bool:
        return self.ok


def wait_until(
    condition: Callable[[], T],
    timeout_s: float,
    *,
    poll_s: float = 0.1,
    description: str = "condition",
    clock: Clock = DEFAULT_CLOCK,
    token: CancelToken | None = None,
    raise_on_timeout: bool = False,
) -> WaitResult[T]:
    """Poll ``condition`` until it returns a truthy value or the timeout expires.

    * The condition is always evaluated at least once, even with timeout 0.
    * Exceptions raised by the condition propagate (a broken detector is a
      bug, not a "not yet").
    * Time spent paused through ``token`` extends the deadline.
    """
    deadline = Deadline(timeout_s, clock)
    polls = 0
    while True:
        if token is not None:
            token.check()
            deadline.extend(token.wait_if_paused(clock))
        value = condition()
        polls += 1
        if value:
            return WaitResult(True, value, deadline.elapsed, polls)
        if deadline.expired:
            if raise_on_timeout:
                raise OperationTimeout(description, timeout_s)
            return WaitResult(False, None, deadline.elapsed, polls)
        clock.sleep(min(poll_s, max(deadline.remaining, 0.001)))


def wait_for_change(
    read: Callable[[], T],
    timeout_s: float,
    *,
    initial: T | None = None,
    **kwargs,
) -> WaitResult[T]:
    """Wait until ``read()`` returns something different from ``initial``.

    Used for things like ``wait_until_round_changes`` and
    ``wait_until_position_changes``.
    """
    baseline = read() if initial is None else initial
    box: list[T] = []

    def changed() -> bool:
        v = read()
        if v != baseline:
            box.append(v)
            return True
        return False

    res = wait_until(changed, timeout_s, **kwargs)
    return WaitResult(res.ok, box[-1] if box else None, res.elapsed_s, res.polls)


def interruptible_sleep(seconds: float, *, token: CancelToken | None = None,
                        clock: Clock = DEFAULT_CLOCK, slice_s: float = 0.05) -> None:
    """Sleep that reacts to stop/pause within ``slice_s``. Use only for
    genuine fixed durations (e.g. how long a key is held), not for waiting on
    game state; use wait_until for that."""
    deadline = Deadline(seconds, clock)
    while not deadline.expired:
        if token is not None:
            token.check()
            deadline.extend(token.wait_if_paused(clock))
        clock.sleep(min(slice_s, deadline.remaining))


# --------------------------------------------------------------------------- #
# Rate control
# --------------------------------------------------------------------------- #


class RateGate:
    """Says whether a periodic task is due. The scheduler (Stage 3) holds one
    per detector so each runs at its configured Hz."""

    def __init__(self, hz: float, clock: Clock = DEFAULT_CLOCK):
        if hz <= 0:
            raise ValueError("hz must be > 0")
        self._clock = clock
        self.interval = 1.0 / hz
        self._next = clock.now()

    def due(self) -> bool:
        return self._clock.now() >= self._next

    def mark(self) -> None:
        """Record that the task ran. Skips missed slots instead of bursting
        to catch up, which matters after a lag spike."""
        now = self._clock.now()
        self._next = max(self._next + self.interval, now + self.interval * 0.5)

    def try_acquire(self) -> bool:
        if self.due():
            self.mark()
            return True
        return False

    def force(self) -> None:
        """Make the gate due immediately (event-driven re-check)."""
        self._next = self._clock.now()


def jittered(base_s: float, jitter_ms: int, rng: random.Random | None = None) -> float:
    """``base_s`` ± up to ``jitter_ms``, never negative.

    Used to avoid lock-step timing against UI animations. It is not meant to
    disguise the automation.
    """
    if jitter_ms <= 0:
        return base_s
    r = rng or random
    return max(0.0, base_s + r.uniform(-jitter_ms, jitter_ms) / 1000.0)
