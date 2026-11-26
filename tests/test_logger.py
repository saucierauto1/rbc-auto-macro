import queue

from rbc_agent.core.config import LoggingConfig
from rbc_agent.core.logger import Category, setup_logging


def test_events_reach_gui_queue_and_file(tmp_path):
    q = queue.Queue(maxsize=100)
    log_file = tmp_path / "agent.log"
    log = setup_logging(LoggingConfig(file=str(log_file), console=False), gui_queue=q)

    log.state("RBC_FARMING", round=8)
    log.decision("Continue farming", reason="capacity 91%", confidence=0.91)

    ev1, ev2 = q.get_nowait(), q.get_nowait()
    assert ev1.category is Category.STATE and ev1.fields == {"round": 8}
    assert ev2.format().endswith('reason="capacity 91%"  confidence=0.91')
    text = log_file.read_text()
    assert "STATE     RBC_FARMING  round=8" in text


def test_full_gui_queue_drops_oldest_without_blocking(tmp_path):
    q = queue.Queue(maxsize=3)
    log = setup_logging(LoggingConfig(file="", console=False), gui_queue=q)
    for i in range(10):
        log.action(f"a{i}")
    msgs = [q.get_nowait().message for _ in range(q.qsize())]
    assert msgs == ["a7", "a8", "a9"]
