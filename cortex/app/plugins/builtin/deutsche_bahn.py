"""Deutsche Bahn — departures + journeys via the free db-rest API (no key)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory

API = "https://v6.db.transport.rest"


class DeutscheBahnPlugin(Plugin):
    slug = "deutsche_bahn"
    name = "Deutsche Bahn"
    description = "DB-Verbindungen, Abfahrten & Verspätungen (kostenlose db-rest API)."
    category = PluginCategory.TRANSPORT
    icon = "🚄"
    config_fields = [
        ConfigField("home_station", "Heim-Bahnhof", default="Frankfurt(Main)Hbf",
                    help="Name des Standard-Bahnhofs"),
    ]

    async def _find_station(self, query: str) -> dict | None:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{API}/locations", params={"query": query, "results": 1})
        d = r.json()
        return d[0] if d else None

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            st = await self._find_station(self.get("home_station", "Frankfurt"))
            return (HealthStatus.ok(f"DB erreichbar — Heimat: {st['name']}.") if st
                    else HealthStatus.error("Bahnhof nicht gefunden."))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _departures(self, station: str) -> str:
        st = await self._find_station(station)
        if not st:
            return f"Bahnhof '{station}' nicht gefunden."
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{API}/stops/{st['id']}/departures",
                            params={"duration": 60, "results": 8})
        deps = r.json().get("departures", r.json()) if isinstance(r.json(), dict) else r.json()
        lines = []
        for d in deps[:8]:
            when = (d.get("when") or d.get("plannedWhen") or "")[11:16]
            delay = d.get("delay")
            late = f" +{delay // 60}′" if delay else ""
            lines.append(f"• {when}{late} {d.get('line', {}).get('name', '?')} → {d.get('direction', '?')}")
        return f"🚄 {st['name']}:\n" + "\n".join(lines) if lines else "Keine Abfahrten."

    def tools(self) -> list[Tool]:
        async def _dep(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "DB-Plugin ist deaktiviert."
            return await self._departures(args.get("station") or self.get("home_station"))

        return [Tool(
            name="db_departures",
            description="Nächste DB-Abfahrten an einem Bahnhof (Standard: Heim-Bahnhof).",
            parameters={"type": "object", "properties": {
                "station": {"type": "string", "description": "Bahnhofsname, optional"}}},
            handler=_dep, owner_only=True, source=self.slug,
        )]
