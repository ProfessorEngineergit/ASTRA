"""Unit tests for the per-thread state machine."""
from app.policy import Mode
from app.state import Act, Signal, ThreadState, Transition, next_state, state_for_mode


def test_mode_maps_to_state():
    assert state_for_mode(Mode.AUTO) == ThreadState.ANSWERED
    assert state_for_mode(Mode.DEFER) == ThreadState.DEFERRED
    assert state_for_mode(Mode.ASK) == ThreadState.AWAITING_APPROVAL


def test_owner_reply_stands_down_a_deferred_thread():
    assert next_state(ThreadState.DEFERRED, Signal.INBOUND_OWNER) == Transition(
        ThreadState.STANDDOWN, Act.STAND_DOWN
    )


def test_owner_reply_stands_down_a_pending_approval():
    assert next_state(ThreadState.AWAITING_APPROVAL, Signal.INBOUND_OWNER) == Transition(
        ThreadState.STANDDOWN, Act.STAND_DOWN
    )


def test_owner_reply_on_idle_does_nothing():
    assert next_state(ThreadState.IDLE, Signal.INBOUND_OWNER) == Transition(
        ThreadState.IDLE, Act.NONE
    )


def test_inbound_other_triggers_classify():
    assert next_state(ThreadState.ANSWERED, Signal.INBOUND_OTHER) == Transition(
        ThreadState.IDLE, Act.CLASSIFY
    )


def test_timer_steps_in_only_while_deferred():
    assert next_state(ThreadState.DEFERRED, Signal.DEFER_ELAPSED) == Transition(
        ThreadState.ANSWERED, Act.STEP_IN
    )
    # If the owner already stood the thread down, the timer must NOT step in.
    assert next_state(ThreadState.STANDDOWN, Signal.DEFER_ELAPSED) == Transition(
        ThreadState.STANDDOWN, Act.NONE
    )


def test_approval_resumes_only_while_awaiting():
    assert next_state(ThreadState.AWAITING_APPROVAL, Signal.APPROVAL_DECIDED) == Transition(
        ThreadState.ANSWERED, Act.RESUME
    )
    assert next_state(ThreadState.STANDDOWN, Signal.APPROVAL_DECIDED) == Transition(
        ThreadState.STANDDOWN, Act.NONE
    )
