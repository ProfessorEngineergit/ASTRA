"""Ereignisbus für verbundene Wand-Displays (OpenBoard) — in-process, ohne Redis.

Cortex läuft mit genau einem Uvicorn-Worker, daher genügt ein asyncio-Pub/Sub: jede
SSE-Verbindung bekommt eine eigene, begrenzte Queue. Ist sie voll (Display hängt,
Netz stockt), fliegt das ÄLTESTE Ereignis raus — ein Display soll den aktuellen
Stand zeigen, nicht eine Minute Rückstau abarbeiten.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("astra.display.hub")

# Ereignistypen laut „ASTRA-Display-Protokoll v1“ (SSE `event:`-Namen).
EVENT_TYPES = frozenset({"hello", "card", "cards", "say", "reply", "alarm", "command", "board", "ping"})
QUEUE_MAX = 64


@dataclass
class Subscriber:
    id: int
    peer: str = ""
    agent: str = ""
    since: float = field(default_factory=time.time)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_MAX))
    dropped: int = 0


class DisplayHub:
    def __init__(self, maxsize: int = QUEUE_MAX) -> None:
        self._subs: dict[int, Subscriber] = {}
        self._ids = itertools.count(1)
        self.maxsize = maxsize
        self.last_seen: float = 0.0

    # ── Verbindungen ─────────────────────────────────────────────────────────
    def subscribe(self, *, peer: str = "", agent: str = "") -> Subscriber:
        sub = Subscriber(id=next(self._ids), peer=peer, agent=agent[:120],
                         queue=asyncio.Queue(self.maxsize))
        self._subs[sub.id] = sub
        self.last_seen = time.time()
        log.info("Display verbunden (#%d, %s) — %d aktiv.", sub.id, peer or "?", len(self._subs))
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if self._subs.pop(sub.id, None) is not None:
            self.last_seen = time.time()
            log.info("Display getrennt (#%d) — %d aktiv.", sub.id, len(self._subs))

    @property
    def connected(self) -> int:
        return len(self._subs)

    def clients(self) -> list[dict]:
        return [{"id": s.id, "peer": s.peer, "agent": s.agent, "since": s.since,
                 "queued": s.queue.qsize(), "dropped": s.dropped} for s in self._subs.values()]

    # ── Senden ───────────────────────────────────────────────────────────────
    def publish(self, event: str, data: dict | None = None) -> int:
        """Ereignis an alle Displays. Gibt die Zahl der Empfänger zurück (0 = keiner da)."""
        if event not in EVENT_TYPES:
            raise ValueError(f"Unbekannter Display-Ereignistyp: {event}")
        msg = (event, data if data is not None else {})
        for sub in list(self._subs.values()):
            while True:
                try:
                    sub.queue.put_nowait(msg)
                    break
                except asyncio.QueueFull:
                    try:
                        sub.queue.get_nowait()       # drop-oldest
                        sub.dropped += 1
                    except asyncio.QueueEmpty:
                        pass
        return len(self._subs)


def format_sse(event: str, data: dict) -> str:
    """Ein SSE-Frame. JSON enthält keine rohen Zeilenumbrüche (escaped) → eine data-Zeile."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n"


_hub: DisplayHub | None = None


def get_hub() -> DisplayHub:
    global _hub
    if _hub is None:
        _hub = DisplayHub()
    return _hub
