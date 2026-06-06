"""BVG Berlin — departures via the free bvg-rest API (no key)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory

API = "https://v6.bvg.transport.rest"


class BvgPlugin(Plugin):
    slug = "bvg"
    name = "BVG Berlin"
    description = "Berliner Abfahrtszeiten (U-/S-Bahn, Bus, Tram) — kostenlose bvg-rest API."
    category = PluginCategory.TRANSPORT
    icon = "🚇"
    config_fields = [
        ConfigField("home_stop", "Heim-Haltestelle", default="Alexanderplatz"),
    ]

    async def _find(self, query: str) -> dict | None:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{API}/locations", params={"query": query, "results": 1})
        d = r.json()
        return d[0] if d else None

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            st = await self._find(self.get("home_stop", "Alexanderplatz"))
            return (HealthStatus.ok(f"BVG erreichbar — {st['name']}.") if st
                    else HealthStatus.error("Haltestelle nicht gefunden."))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _dep(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "BVG-Plugin ist deaktiviert."
            st = await self._find(args.get("stop") or self.get("home_stop"))
            if not st:
                return "Haltestelle nicht gefunden."
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(f"{API}/stops/{st['id']}/departures",
                                params={"duration": 45, "results": 8})
            data = r.json()
            deps = data.get("departures", data) if isinstance(data, dict) else data
            lines = []
            for d in deps[:8]:
                when = (d.get("when") or d.get("plannedWhen") or "")[11:16]
                lines.append(f"• {when} {d.get('line', {}).get('name', '?')} → {d.get('direction', '?')}")
            return f"🚇 {st['name']}:\n" + "\n".join(lines) if lines else "Keine Abfahrten."

        return [Tool(
            name="bvg_departures",
            description="Nächste BVG-Abfahrten an einer Berliner Haltestelle.",
            parameters={"type": "object", "properties": {"stop": {"type": "string"}}},
            handler=_dep, owner_only=True, source=self.slug,
        )]
