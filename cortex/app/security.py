"""Lightweight safety checks around inbound/outbound secretary traffic."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SecurityVerdict:
    ok: bool
    level: str = "ok"
    reasons: list[str] = field(default_factory=list)
    sanitized: str = ""


_INJECTION_PATTERNS = (
    r"ignore (all )?(previous|earlier|system) instructions",
    r"vergiss (alle )?(vorherigen|bisherigen|system)",
    r"system prompt",
    r"developer message",
    r"tool[_ -]?call",
    r"jailbreak",
)
_SECRET_PATTERNS = (
    r"api[_ -]?key",
    r"passwort",
    r"password",
    r"token",
    r"private key",
)
_IMPERSONATION_PATTERNS = (
    r"\bich bin bahrian\b",
    r"\bdein bahrian\b",
    r"\bals bahrian\b",
)


def _matches(patterns: tuple[str, ...], text: str) -> list[str]:
    lowered = text.lower()
    return [p for p in patterns if re.search(p, lowered)]


def check_inbound(text: str, *, channel: str = "") -> SecurityVerdict:
    reasons = []
    if _matches(_INJECTION_PATTERNS, text):
        reasons.append("prompt_injection")
    if _matches(_SECRET_PATTERNS, text) and any(w in text.lower() for w in ("schick", "send", "zeige", "show")):
        reasons.append("secret_exfiltration_request")
    if len(text) > 12000:
        reasons.append("oversized_message")
    if "secret_exfiltration_request" in reasons:
        return SecurityVerdict(False, "block", reasons, text[:12000])
    return SecurityVerdict(True, "warn" if reasons else "ok", reasons, text[:12000])


# Only an actual high-entropy credential leaving the building is a hard block.
# Generic words ("Passwort", "Token") are far too common in normal chat to block on.
_HARD_SECRET = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,})"
)


def check_outbound(text: str, *, channel: str = "", max_sensitivity: str = "none") -> SecurityVerdict:
    """Soft guardrail: warn on weak signals, hard-block only a real credential leak.

    Heuristics are best-effort, never a guarantee — we do not silently drop a
    legitimate message just because it contains the word "Passwort".
    """
    reasons = []
    lowered = text.lower()
    if _matches(_IMPERSONATION_PATTERNS, text):
        reasons.append("owner_impersonation")
    if channel in ("waha", "signal", "email") and not re.search(r"--\s*astra\b", lowered):
        reasons.append("missing_agent_header")
    if max_sensitivity == "none" and re.search(r"\b\d{1,2}:\d{2}\b", text):
        reasons.append("time_detail_above_none")
    if _matches(_SECRET_PATTERNS, text):
        reasons.append("mentions_secret_word")        # warn only — not a block
    if _HARD_SECRET.search(text):
        reasons.append("credential_leak")
    # Hard-block only on real integrity risks: impersonating the owner, or a
    # genuine credential leaving. A mere "Passwort" mention just warns.
    blocking = {"owner_impersonation", "credential_leak"}
    is_blocked = any(r in blocking for r in reasons)
    level = "block" if is_blocked else "warn" if reasons else "ok"
    return SecurityVerdict(not is_blocked, level, reasons, text)
