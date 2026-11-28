"""Stage 5: the agent's current state, when it started, and its timeout.

Which state to be in comes from the confirmed screen (see ``rbc_agent/agent.py``);
this class only records changes and says when a state has lasted longer than
``timeouts_s`` allows, so no state can hang forever.
"""

from __future__ import annotations

from .config import AppConfig
from .states import AgentState
from .timing import DEFAULT_CLOCK, Clock


class StateMachine:
    def __init__(self, cfg: AppConfig, clock: Clock = DEFAULT_CLOCK):
        self.cfg = cfg
        self._clock = clock
        self.state = AgentState.IDLE
        self.entered_at = clock.now()

    def go(self, state: AgentState) -> bool:
        """Switch to ``state``. Returns True if it is a change (the timer restarts)."""
        if state is self.state:
            return False
        self.state = state
        self.entered_at = self._clock.now()
        return True

    @property
    def elapsed_s(self) -> float:
        return self._clock.now() - self.entered_at

    def timed_out(self) -> bool:
        limit = self.cfg.timeout_for(self.state)
        return limit is not None and self.elapsed_s > limit
