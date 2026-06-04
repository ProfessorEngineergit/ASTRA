"""Disclosure & autonomy policy — PURE logic (stdlib only, fully unit-tested).

The LLM *proposes* (triage), the policy *disposes*. Hard rules here can only ever
make ASTRA more cautious than the model wanted, never less.

Trust tiers:  0 owner · 1 trusted · 2 known · 3 unknown
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class TrustTier(IntEnum):
    OWNER = 0
    TRUSTED = 1
    KNOWN = 2
    UNKNOWN = 3


class Sensitivity(str, Enum):
    NONE = "none"          # general chat, no private info
    FREEBUSY = "freebusy"  # whether Bahrian is free/busy (+ coarse info)
    DETAILS = "details"    # specifics: what exactly, where, with whom


class Mode(str, Enum):
    AUTO = "auto"    # answer now, autonomously
    DEFER = "defer"  # wait for the owner first, then maybe step in
    ASK = "ask"      # ask the owner for permission / confirmation


# Highest sensitivity a tier may receive WITHOUT asking the owner.
_MAX_SENSITIVITY: dict[TrustTier, Sensitivity] = {
    TrustTier.OWNER: Sensitivity.DETAILS,
    TrustTier.TRUSTED: Sensitivity.FREEBUSY,
    TrustTier.KNOWN: Sensitivity.FREEBUSY,
    TrustTier.UNKNOWN: Sensitivity.NONE,
}
_RANK = {Sensitivity.NONE: 0, Sensitivity.FREEBUSY: 1, Sensitivity.DETAILS: 2}


def max_sensitivity_for(tier: TrustTier) -> Sensitivity:
    return _MAX_SENSITIVITY[tier]


def disclosure_allowed(tier: TrustTier, sensitivity: Sensitivity) -> bool:
    """May `tier` autonomously receive info of this `sensitivity`?"""
    return _RANK[sensitivity] <= _RANK[_MAX_SENSITIVITY[tier]]


@dataclass(frozen=True)
class Decision:
    mode: Mode
    max_sensitivity: Sensitivity  # ceiling ASTRA must respect when it does reply
    reason: str


def reconcile(triage_mode: Mode, tier: TrustTier, sensitivity: Sensitivity) -> Decision:
    """Combine the model's triage with hard disclosure rules."""
    allowed = max_sensitivity_for(tier)

    # The owner talking to ASTRA: always full + immediate.
    if tier == TrustTier.OWNER:
        return Decision(Mode.AUTO, Sensitivity.DETAILS, "owner")

    # The request needs more than this tier may autonomously receive.
    if not disclosure_allowed(tier, sensitivity):
        if triage_mode == Mode.DEFER:
            # Owner gets first shot; if ASTRA later steps in it must stay within `allowed`.
            return Decision(Mode.DEFER, allowed, "defer-then-limited")
        return Decision(Mode.ASK, allowed, "disclosure-above-tier")

    # Within tier → honour the model's call, but cap the disclosure ceiling.
    return Decision(triage_mode, allowed, "within-tier")
