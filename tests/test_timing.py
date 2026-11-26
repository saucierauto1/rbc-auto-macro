import threading
import time

import pytest

from rbc_agent.core.errors import AutomationCancelled, OperationTimeout
from rbc_agent.core.timing import (CancelToken, FakeClock, RateGate, RunMode,
                                   interruptible_sleep, jittered, wait_for_change, wait_until)


def test_wait_until_success_after_virtual_delay():
    clock = FakeClock()
    res = wait_until(lambda: clock.now() >= 3.0, timeout_s=15, poll_s=0.5, clock=clock)
    assert res.ok and 3.0 <= res.elapsed_s < 3.6


def test_wait_until_timeout_returns_false():
    clock = FakeClock()
    res = wait_until(lambda: False, timeout_s=15, poll_s=0.5, clock=clock)
    assert not res.ok and res.elapsed_s == pytest.approx(15, abs=0.01)


def test_wait_until_raise_on_timeout():
    with pytest.raises(OperationTimeout) as e:
        wait_until(lambda: False, 2, clock=FakeClock(), raise_on_timeout=True,
                   description="RBC start")
    assert "RBC start" in str(e.value)


def test_condition_checked_at_least_once_with_zero_timeout():
    assert wait_until(lambda: "yes", 0, clock=FakeClock()).value == "yes"


def test_stop_interrupts_wait():
    clock, token = FakeClock(), CancelToken()
    clock.on_sleep.append(lambda t: token.stop("F10") if t >= 1 else None)
    with pytest.raises(AutomationCancelled):
        wait_until(lambda: False, 60, clock=clock, token=token)
    assert clock.now() < 1.5


def test_pause_extends_deadline():
    clock, token = FakeClock(), CancelToken()
    # Pause from t=1 to t=11. The condition becomes true at t=14, which is
    # past a naive 10 s timeout but within 10 s of *unpaused* time.
    def hook(t):
        if 1 <= t < 1.2 and token.mode is RunMode.RUNNING:
            token.pause()
        if t >= 11 and token.paused:
            token.resume()
    clock.on_sleep.append(hook)
    res = wait_until(lambda: clock.now() >= 14, 10, poll_s=0.1, clock=clock, token=token)
    assert res.ok


def test_stop_is_terminal():
    t = CancelToken()
    t.stop("x")
    t.resume()
    assert t.stopped and t.stop_reason == "x"


def test_real_clock_pause_resume_across_threads():
    token = CancelToken()
    token.pause()
    threading.Timer(0.1, token.resume).start()
    start = time.monotonic()
    token.wait_if_paused()
    assert 0.05 < time.monotonic() - start < 1.0


def test_wait_for_change():
    clock = FakeClock()
    rnd = {"n": 3}
    clock.on_sleep.append(lambda t: rnd.update(n=4) if t >= 2 else None)
    res = wait_for_change(lambda: rnd["n"], 10, clock=clock)
    assert res.ok and res.value == 4


def test_interruptible_sleep_honours_stop():
    clock, token = FakeClock(), CancelToken()
    clock.on_sleep.append(lambda t: token.stop() if t >= 0.2 else None)
    with pytest.raises(AutomationCancelled):
        interruptible_sleep(5, token=token, clock=clock)


def test_rate_gate_frequency_and_no_burst():
    clock = FakeClock()
    gate = RateGate(4, clock)          # every 0.25 s
    runs = 0
    for _ in range(100):               # 1 s in 10 ms steps
        if gate.try_acquire():
            runs += 1
        clock.advance(0.01)
    assert 4 <= runs <= 5
    clock.advance(10)                  # lag spike
    burst = sum(gate.try_acquire() for _ in range(10))
    assert burst == 1                  # no catch-up burst


def test_jitter_bounds():
    import random
    rng = random.Random(1)
    vals = [jittered(0.1, 15, rng) for _ in range(200)]
    assert all(0.085 <= v <= 0.115 for v in vals)
    assert jittered(0.1, 0) == 0.1
