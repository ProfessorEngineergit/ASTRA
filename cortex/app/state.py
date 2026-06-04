"""Per-thread conversation state machine — PURE logic (stdlib only, unit-tested).

This encodes the "schnell vs. 2 Minuten warten + stand-down" behaviour:

    inbound(other) ─▶ classify ─▶ AUTO  → reply now            → ANSWERED
                                  DEFER  → wait defer_seconds   → DEFERRED ─(timer)─▶ step in
                                  ASK    → ask owner            → AWAITING_APPROVAL ─(decision)─▶ resume

At any moment, if the OWNER replies themselves on the thread, ASTRA stands down.
The persistence (defer_until, summary, ...) lives in db.py; this module is the
side-effect-free brain of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .policy import Mode


class ThreadState(str, Enum):
    IDLE = "idle"
    DEFERRED = "deferred"
    AWAITING_APPROVAL = "awaiting_approval"
    ANSWERED = "answered"
    STANDDOWN = "standdown"


class Signal(str, Enum):
    INBOUND_OTHER = "inbound_other"        # third party wrote
    INBOUND_OWNER = "inbound_owner"        # owner replied themselves on this thread
    DEFER_ELAPSED = "defer_elapsed"        # the wait timer fired
    APPROVAL_DECIDED = "approval_decided"  # owner answered an ask_principal
    REPLIED = "replied"                    # ASTRA sent its reply


# What the orchestrator should DO as a result of a transition.
class Act(str, Enum):
    NONE = "none"
    CLASSIFY = "classify"
    STEP_IN = "step_in"
    STAND_DOWN = "stand_down"
    RESUME = "resume"


@dataclass(frozen=True)
class Transition:
    state: ThreadState
    act: Act


def state_for_mode(mode: Mode) -> ThreadState:
    """Target state once a freshly-classified message has been dispatched."""
    return {
        Mode.AUTO: ThreadState.ANSWERED,
        Mode.DEFER: ThreadState.DEFERRED,
        Mode.ASK: ThreadState.AWAITING_APPROVAL,
    }[mode]


def next_state(current: ThreadState, signal: Signal) -> Transition:
    # Owner stepping in always wins.
    if signal == Signal.INBOUND_OWNER:
        if current in (ThreadState.DEFERRED, ThreadState.AWAITING_APPROVAL):
            return Transition(ThreadState.STANDDOWN, Act.STAND_DOWN)
        return Transition(current, Act.NONE)

    # Any new inbound from the other party (re)triggers classification.
    if signal == Signal.INBOUND_OTHER:
        return Transition(ThreadState.IDLE, Act.CLASSIFY)

    # The deferral timer fired: step in only if still waiting.
    if signal == Signal.DEFER_ELAPSED:
        if current == ThreadState.DEFERRED:
            return Transition(ThreadState.ANSWERED, Act.STEP_IN)
        return Transition(current, Act.NONE)

    # Owner decided an approval: resume only if we were waiting on it.
    if signal == Signal.APPROVAL_DECIDED:
        if current == ThreadState.AWAITING_APPROVAL:
            return Transition(ThreadState.ANSWERED, Act.RESUME)
        return Transition(current, Act.NONE)

    if signal == Signal.REPLIED:
        return Transition(ThreadState.ANSWERED, Act.NONE)

    return Transition(current, Act.NONE)
