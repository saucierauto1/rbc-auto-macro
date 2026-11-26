import threading

from rbc_agent.ui.status import StatusBus, StatusSnapshot, format_rows


def test_format_rows_shows_all_spec_fields():
    labels = [l for l, _ in format_rows(StatusSnapshot())]
    assert labels == ["STATUS", "RBC ROUND", "CURRENT FIELD", "POLLEN", "CAPACITY",
                      "CURRENT OBJECTIVE", "CURRENT STRATEGY", "CURRENT ACTION",
                      "RUN TIME", "RECOVERY COUNT", "DETECTION CONFIDENCE"]


def test_format_values():
    rows = dict(format_rows(StatusSnapshot(rbc_round=8, pollen=1_820_000, capacity=2_000_000,
                                           run_time_s=3725, detection_confidence=0.914)))
    assert rows["RBC ROUND"] == "8 / 25"
    assert rows["POLLEN"] == "1.82M (91%)"
    assert rows["RUN TIME"] == "01:02:05"
    assert rows["DETECTION CONFIDENCE"] == "91%"


def test_bus_is_thread_safe_and_versioned():
    bus = StatusBus()
    def worker():
        for i in range(500):
            bus.publish(recovery_count=i)
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    version, snap = bus.read()
    assert version == 2000 and snap.recovery_count == 499
