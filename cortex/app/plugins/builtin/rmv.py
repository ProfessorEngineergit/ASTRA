"""RMV public transport (HAFAS open API, https://www.rmv.de/hapi)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.rmv")
_BASE = "https://www.rmv.de/hapi"


class RMVPlugin(Plugin):
    slug = "rmv"
    name = "RMV (Nahverkehr)"
    description = "Nächste Abfahrten, Verbindungen und Ausfall-Warnungen im RMV-Gebiet."
    category = PluginCategory.TRANSPORT
    icon = "🚆"
    config_fields = [
        ConfigField("api_key", "RMV accessId", FieldType.PASSWORD, required=True, secret=True,
                    help="Kostenlos via opendata.rmv.de", env_fallback="rmv_api_key"),
        ConfigField("home_stop_id", "Heim-Haltestelle (extId)", help="Stations-ID",
                    env_fallback="rmv_home_stop_id"),
        ConfigField("school_stop_id", "Ziel/Schule (extId)", env_fallback="rmv_school_stop_id"),
    ]

    async def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        params = {"accessId": self.get("api_key"), "format": "json", **params}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{_BASE}{path}", params=params)
            r.raise_for_status()
            return r.json()

    async def departures(self, stop_id: str | None = None, max_results: int = 5) -> list[dict]:
        stop_id = stop_id or self.get("home_stop_id")
        if not stop_id:
            return []
        data = await self._get("/departureBoard", {"extId": stop_id, "maxJourneys": max_results})
        out = []
        for d in (data or {}).get("Departure", [])[:max_results]:
            out.append({
                "line": (d.get("Product") or {}).get("line") or d.get("name", ""),
                "direction": d.get("direction", ""),
                "time": d.get("time", ""),
                "rtTime": d.get("rtTime"),
                "cancelled": bool(d.get("cancelled", False)),
            })
        return out

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            await self._get("/departureBoard", {"extId": self.get("home_stop_id") or "3000001",
                                                "maxJourneys": 1})
            return HealthStatus.ok("RMV-API erreichbar.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"RMV-API: {e}")

    def tools(self) -> list[Tool]:
        async def _get_departures(args: dict, ctx: ToolContext) -> str:
            deps = await self.departures(args.get("stop_id"))
            if not deps:
                return "Keine Abfahrten gefunden."
            lines = []
            for d in deps:
                flag = "⚠️ FÄLLT AUS" if d["cancelled"] else (
                    f"(echtzeit {d['rtTime']})" if d["rtTime"] and d["rtTime"] != d["time"] else "")
                lines.append(f"{d['time']} {d['line']} → {d['direction']} {flag}".strip())
            return "Nächste Abfahrten:\n- " + "\n- ".join(lines)

        return [Tool(
            name="get_departures",
            description="Nächste ÖPNV-Abfahrten (RMV) von einer Haltestelle, inkl. Ausfall-Warnungen.",
            parameters={"type": "object", "properties": {
                "stop_id": {"type": "string", "description": "Haltestellen-extId; leer = Heim-Haltestelle"}}},
            handler=_get_departures, owner_only=True, source=self.slug,
        )]

    async def briefing_section(self) -> str | None:
        if not self.get("home_stop_id"):
            return None
        deps = await self.departures(max_results=4)
        if not deps:
            return None
        parts = []
        for d in deps[:4]:
            if d["cancelled"]:
                parts.append(f"⚠️ {d['time']} {d['line']} FÄLLT AUS")
            else:
                rt = f"→{d['rtTime']}" if d["rtTime"] and d["rtTime"] != d["time"] else ""
                parts.append(f"{d['time']}{rt} {d['line']}")
        return "🚆 *Abfahrten:* " + " · ".join(parts)
