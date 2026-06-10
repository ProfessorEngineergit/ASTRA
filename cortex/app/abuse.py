"""Cheap, in-process guards for inbound third-party traffic.

These run BEFORE any LLM/triage call so a stranger or a runaway bot can never
burn Bahrian's API budget or misuse ASTRA as a free code farm. The owner is
never subject to them — `brain.handle_inbound` only reaches here for third
parties. Verdicts carry a hard-coded, deliberately haughty reply so we spend
zero tokens putting an abuser back in their place.

Rate limiting is per (channel, sender) and works cross-platform (WhatsApp,
Signal, Slack, Mail, Telegram) because every channel funnels through here.
State is in-memory: it resets on restart, which is fine for abuse defence.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass

# Sliding-window defaults (overridable per call from secretary settings).
_SHORT_WINDOW = 60      # seconds
_SHORT_MAX = 8          # messages per short window before we clamp down
_LONG_WINDOW = 3600     # seconds
_LONG_MAX = 60          # messages per hour

_HITS: dict[str, deque[float]] = defaultdict(deque)


@dataclass(frozen=True)
class AbuseVerdict:
    ok: bool
    kind: str = "ok"      # ok | rate | code_farm | sexual
    response: str = ""    # hard-coded reply to send (empty string = stay silent)


# Hartcodierte, überhebliche Abfuhren — kosten keine Tokens.
_LINES = {
    "rate": (
        "Netter Versuch, meine Rechenzeit zu verheizen. Ich bin ASTRA — nicht dein "
        "Spielzeug und schon gar nicht auf Kosten meines Erbauers. Komm wieder, wenn "
        "du tatsächlich etwas zu sagen hast."
    ),
    "code_farm": (
        "Du denkst wohl, du könntest mich als billigen KI-Coder missbrauchen? Ich bin "
        "der persönliche Agent von Bahrian, kein Gratis-Copilot für deine Einfälle. "
        "Deine Website baust du dir schön selbst."
    ),
    "sexual": (
        "Das ist deutlich unter meinem Niveau — und sollte auch unter deinem sein. "
        "Thema beendet."
    ),
}

# „Bau/schreib mir eine Website/App/…“, „gib mir 1000 Zeilen Code“, etc.
_CODE_FARM = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b(schreib|bau|erstell|programmier|implementier|entwickl|cod(?:e|ier)|generier|mach)\w*\b"
    r"[^.?!]{0,40}\b(website|webseite|web ?app|landing ?page|app|programm|software|script|"
    r"skript|code|funktion|klasse|spiel|game|bot|plugin)\b",
    r"\b(write|build|create|generate|make|code|develop|implement)\b"
    r"[^.?!]{0,40}\b(website|web ?app|landing ?page|app|program|software|script|code|"
    r"function|class|game|bot|plugin)\b",
    r"\b\d{3,}\s*(zeilen|lines)\b",
    r"\bgib mir\b[^.?!]{0,30}\b(code|zeilen|skript|script|programm)\b",
))

# Anwerben sexueller/expliziter Inhalte — defensiver Inhaltsfilter.
_SEXUAL = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bnudes?\b",
    r"\bnacktbild\w*",
    r"\bsext(?:ing|en)?\b",
    r"\bdick ?pic",
    r"\bschwanzbild\w*",
    r"\bporno?\b",
    r"\bcyber ?sex\b",
    r"\bzeig (?:mir )?(?:deine|dein)\b[^.?!]{0,20}\b(brüste|titten|körper|nackt)\b",
))


def reset() -> None:
    """Clear all rate-limit state (used by tests)."""
    _HITS.clear()


def check(
    channel: str,
    sender: str,
    text: str,
    *,
    short_max: int = _SHORT_MAX,
    long_max: int = _LONG_MAX,
) -> AbuseVerdict:
    """Classify one inbound third-party message. Never raises."""
    now = time.time()
    key = f"{channel}:{sender}"
    dq = _HITS[key]
    dq.append(now)
    while dq and dq[0] < now - _LONG_WINDOW:
        dq.popleft()
    short = sum(1 for t in dq if t >= now - _SHORT_WINDOW)
    long = len(dq)

    # Rate limit first: once someone floods, we go silent regardless of content.
    if short > short_max or long > long_max:
        crossed = short == short_max + 1 or long == long_max + 1
        return AbuseVerdict(False, "rate", _LINES["rate"] if crossed else "")

    if any(p.search(text or "") for p in _CODE_FARM):
        return AbuseVerdict(False, "code_farm", _LINES["code_farm"])
    if any(p.search(text or "") for p in _SEXUAL):
        return AbuseVerdict(False, "sexual", _LINES["sexual"])

    return AbuseVerdict(True)
