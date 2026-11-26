"""The agent's state identifiers.

Only the *names* live here (Stage 1) because configuration needs them for
per-state timeouts. State *behaviour* is implemented in Stage 5
(``core/state_machine.py``).
"""

from __future__ import annotations

from enum import Enum


class AgentState(str, Enum):
    IDLE = "IDLE"
    STARTING_ROBO_BEAR = "STARTING_ROBO_BEAR"
    WAITING_FOR_RBC = "WAITING_FOR_RBC"
    RBC_ROUND_START = "RBC_ROUND_START"
    RBC_ANALYZING = "RBC_ANALYZING"
    RBC_SELECTING_QUEST = "RBC_SELECTING_QUEST"
    RBC_MOVING_TO_FIELD = "RBC_MOVING_TO_FIELD"
    RBC_FARMING = "RBC_FARMING"
    RBC_COLLECTING_TOKENS = "RBC_COLLECTING_TOKENS"
    RBC_MANAGING_CAPACITY = "RBC_MANAGING_CAPACITY"
    RBC_RETURNING_HIVE = "RBC_RETURNING_HIVE"
    RBC_MANAGING_BEES = "RBC_MANAGING_BEES"
    RBC_SELECTING_UPGRADES = "RBC_SELECTING_UPGRADES"
    RBC_PURCHASING_UPGRADES = "RBC_PURCHASING_UPGRADES"
    RBC_NEXT_ROUND = "RBC_NEXT_ROUND"
    RBC_COMPLETED = "RBC_COMPLETED"
    RBC_FAILED = "RBC_FAILED"
    RECOVERY = "RECOVERY"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


# States the agent sits in by user choice; they never time out.
UNTIMED_STATES: frozenset[AgentState] = frozenset(
    {AgentState.IDLE, AgentState.PAUSED, AgentState.STOPPED}
)


class StrategyProfile(str, Enum):
    BALANCED = "BALANCED"
    RED = "RED"
    BLUE = "BLUE"
    WHITE = "WHITE"
    TOKEN_FOCUSED = "TOKEN_FOCUSED"
    POLLEN_FOCUSED = "POLLEN_FOCUSED"
    CONVERSION_FOCUSED = "CONVERSION_FOCUSED"
    ATTACK_FOCUSED = "ATTACK_FOCUSED"
    SPEED_FOCUSED = "SPEED_FOCUSED"
    CUSTOM = "CUSTOM"
