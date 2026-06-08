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


def check_outbound(text: str, *, channel: str = "", max_sensitivity: str = "none") -> SecurityVerdict:
    reasons = []
    lowered = text.lower()
    if _matches(_IMPERSONATION_PATTERNS, text):
        reasons.append("owner_impersonation")
    if channel in ("waha", "signal", "email") and not re.search(r"--\s*astra\b", lowered):
        reasons.append("missing_agent_header")
    if max_sensitivity == "none" and re.search(r"\b\d{1,2}:\d{2}\b", text):
        reasons.append("time_detail_above_none")
    if _matches(_SECRET_PATTERNS, text):
        reasons.append("possible_secret_leak")
    blocking = {"owner_impersonation", "possible_secret_leak"}
    return SecurityVerdict(not any(r in blocking for r in reasons), "block" if any(r in blocking for r in reasons) else "warn" if reasons else "ok", reasons, text)
