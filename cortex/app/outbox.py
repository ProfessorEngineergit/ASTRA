"""Eigene Sendungen wiedererkennen (Echo-Filter).

ASTRA schreibt über DEINEN WhatsApp-/Signal-Account. WhatsApp meldet jede dieser Nachrichten als
`fromMe` zurück — für ASTRA sieht das aus, als hättest DU selbst geantwortet. Ohne Unterscheidung
würde sich ASTRA bei jeder eigenen Antwort „von Bahrian unterbrochen“ fühlen (Stand-down mitten im
Gespräch). Darum merkt sich das Transportmodul jeden Text, den ASTRA sendet, für ein paar Minuten
(nur ein Hash im Speicher); kommt derselbe Text als `fromMe` zurück, ist es ein Echo und kein Eingreifen.

Bewusst nur im Speicher: das Echo trifft Sekunden nach dem Senden ein, ein Neustart dazwischen ist
vernachlässigbar — und nichts Persönliches wird gespeichert.
"""
from __future__ import annotations

import hashlib
import re
import time

TTL_SECONDS = 180.0
_MAX_ENTRIES = 500
_recent: dict[str, float] = {}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _key(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    for k in [k for k, ts in _recent.items() if now - ts > TTL_SECONDS]:
        _recent.pop(k, None)
    if len(_recent) > _MAX_ENTRIES:
        for k, _ in sorted(_recent.items(), key=lambda kv: kv[1])[: len(_recent) - _MAX_ENTRIES]:
            _recent.pop(k, None)


def remember(text: str, now: float | None = None) -> None:
    """Vor dem Senden aufrufen."""
    if not (text or "").strip():
        return
    now = time.time() if now is None else now
    _prune(now)
    _recent[_key(text)] = now


def is_echo(text: str, now: float | None = None) -> bool:
    """War das ein Text, den ASTRA gerade selbst gesendet hat?"""
    if not (text or "").strip():
        return False
    now = time.time() if now is None else now
    ts = _recent.get(_key(text))
    return ts is not None and now - ts <= TTL_SECONDS


def reset() -> None:
    _recent.clear()
