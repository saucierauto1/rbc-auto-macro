"""Exception hierarchy for the RBC agent.

Every failure the agent can raise derives from :class:`RBCAgentError`, so the
state machine can catch "our" errors separately from genuine bugs.
"""

from __future__ import annotations


class RBCAgentError(Exception):
    """Base class for all agent errors."""


class ConfigError(RBCAgentError):
    """Configuration file is missing, malformed, or contains invalid values."""

    def __init__(self, problems: list[str] | str):
        if isinstance(problems, str):
            problems = [problems]
        self.problems: list[str] = problems
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(problems))


class OperationTimeout(RBCAgentError):
    """A wait_until / state timeout expired before its condition was met."""

    def __init__(self, what: str, timeout_s: float):
        self.what = what
        self.timeout_s = timeout_s
        super().__init__(f"Timed out after {timeout_s:.1f}s waiting for: {what}")


class AutomationCancelled(RBCAgentError):
    """Raised inside automation code when the CancelToken is stopped."""


class WindowLostError(RBCAgentError):
    """Roblox window disappeared, lost focus, or changed geometry unexpectedly."""


class DetectionError(RBCAgentError):
    """Perception could not produce a result with sufficient confidence."""


class RecoveryExhausted(RBCAgentError):
    """All recovery levels failed; the agent must pause for the user."""
