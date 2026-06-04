"""Unit tests for the disclosure/autonomy policy."""
from app.policy import (
    Decision,
    Mode,
    Sensitivity,
    TrustTier,
    disclosure_allowed,
    max_sensitivity_for,
    reconcile,
)


def test_disclosure_ceilings():
    assert disclosure_allowed(TrustTier.OWNER, Sensitivity.DETAILS)
    assert disclosure_allowed(TrustTier.TRUSTED, Sensitivity.FREEBUSY)
    assert not disclosure_allowed(TrustTier.TRUSTED, Sensitivity.DETAILS)
    assert disclosure_allowed(TrustTier.KNOWN, Sensitivity.FREEBUSY)
    assert not disclosure_allowed(TrustTier.UNKNOWN, Sensitivity.FREEBUSY)
    assert disclosure_allowed(TrustTier.UNKNOWN, Sensitivity.NONE)


def test_owner_always_auto_full():
    d = reconcile(Mode.DEFER, TrustTier.OWNER, Sensitivity.DETAILS)
    assert d == Decision(Mode.AUTO, Sensitivity.DETAILS, "owner")


def test_general_chat_within_tier_honours_triage():
    # Unknown person, no private info requested → triage decides.
    d = reconcile(Mode.AUTO, TrustTier.UNKNOWN, Sensitivity.NONE)
    assert d.mode == Mode.AUTO
    assert d.max_sensitivity == Sensitivity.NONE


def test_unknown_asking_freebusy_is_escalated_to_ask():
    # Above the UNKNOWN ceiling (none) → must ask, even though triage said auto.
    d = reconcile(Mode.AUTO, TrustTier.UNKNOWN, Sensitivity.FREEBUSY)
    assert d.mode == Mode.ASK
    assert d.reason == "disclosure-above-tier"


def test_trusted_asking_details_is_escalated_to_ask():
    d = reconcile(Mode.AUTO, TrustTier.TRUSTED, Sensitivity.DETAILS)
    assert d.mode == Mode.ASK
    assert d.max_sensitivity == Sensitivity.FREEBUSY


def test_above_tier_but_defer_stays_defer_with_capped_ceiling():
    # The "can we meet?" case from a known contact wanting specifics:
    # let Bahrian answer first; if ASTRA steps in it stays at free/busy.
    d = reconcile(Mode.DEFER, TrustTier.KNOWN, Sensitivity.DETAILS)
    assert d.mode == Mode.DEFER
    assert d.max_sensitivity == Sensitivity.FREEBUSY
    assert d.reason == "defer-then-limited"


def test_within_tier_caps_sensitivity_ceiling():
    d = reconcile(Mode.AUTO, TrustTier.TRUSTED, Sensitivity.FREEBUSY)
    assert d.mode == Mode.AUTO
    assert d.max_sensitivity == max_sensitivity_for(TrustTier.TRUSTED)
