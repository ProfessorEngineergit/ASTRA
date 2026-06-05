"""RMV public-transport client (HAFAS open API, https://www.rmv.de/hapi).

Provides next departures / connections so ASTRA can say "nimm die Bahn um 07:42"
or warn about cancellations. Needs a free accessId (RMV OpenData).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import get_settings

log = logging.getLogger("astra.rmv")

_BASE = "https://www.rmv.de/hapi"


class RMV:
    def __init__(self) -> None:
        self.s = get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.rmv_enabled

    async def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        if not self.enabled:
            return None
        params = {"accessId": self.s.rmv_api_key, "format": "json", **params}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{_BASE}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def departures(self, stop_id: str | None = None, max_results: int = 5) -> list[dict]:
        """Next departures from a stop (extId/station id)."""
        stop_id = stop_id or self.s.rmv_home_stop_id
        if not stop_id:
            return []
        data = await self._get("/departureBoard", {"extId": stop_id, "maxJourneys": max_results})
        if not data:
            return []
        out = []
        for d in data.get("Departure", [])[:max_results]:
            out.append(
                {
                    "line": (d.get("Product") or {}).get("line") or d.get("name", ""),
                    "direction": d.get("direction", ""),
                    "time": d.get("time", ""),
                    "rtTime": d.get("rtTime"),          # realtime (None = on time)
                    "cancelled": bool(d.get("cancelled", False)),
                    "track": d.get("rtTrack") or d.get("track", ""),
                }
            )
        return out

    async def trip(self, origin_id: str, dest_id: str, max_results: int = 3) -> list[dict]:
        """Connections between two stops."""
        data = await self._get(
            "/trip", {"originExtId": origin_id, "destExtId": dest_id, "numF": max_results}
        )
        if not data:
            return []
        out = []
        for t in data.get("Trip", [])[:max_results]:
            legs = (t.get("LegList") or {}).get("Leg", [])
            if not legs:
                continue
            dep = legs[0].get("Origin", {})
            arr = legs[-1].get("Destination", {})
            out.append(
                {
                    "depart": dep.get("rtTime") or dep.get("time", ""),
                    "arrive": arr.get("rtTime") or arr.get("time", ""),
                    "changes": max(0, len(legs) - 1),
                    "duration": t.get("duration", ""),
                }
            )
        return out


_rmv: RMV | None = None


def get_rmv() -> RMV:
    global _rmv
    if _rmv is None:
        _rmv = RMV()
    return _rmv
