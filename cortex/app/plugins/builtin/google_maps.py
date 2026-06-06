"""Google Maps — traffic-aware travel time + directions (Directions API)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class GoogleMapsPlugin(Plugin):
    slug = "google_maps"
    name = "Google Maps"
    description = "Fahrzeit mit aktueller Verkehrslage & Routen (Directions API)."
    category = PluginCategory.TRANSPORT
    icon = "🗺️"
    config_fields = [
        ConfigField("api_key", "Google API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="console.cloud.google.com → Directions API aktivieren → Key"),
        ConfigField("mode", "Verkehrsmittel", type=FieldType.SELECT,
                    options=["driving", "transit", "bicycling", "walking"], default="driving"),
    ]

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://maps.googleapis.com/maps/api/directions/json",
                                params={"origin": "Frankfurt", "destination": "Mainz",
                                        "key": self.get("api_key")})
            st = r.json().get("status")
            return (HealthStatus.ok("Directions API erreichbar.") if st == "OK"
                    else HealthStatus.error(f"API-Status: {st}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _route(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Google Maps ist deaktiviert."
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get("https://maps.googleapis.com/maps/api/directions/json",
                                params={"origin": args["origin"], "destination": args["destination"],
                                        "mode": self.get("mode", "driving"),
                                        "departure_time": "now", "key": self.get("api_key")})
            d = r.json()
            if d.get("status") != "OK":
                return f"Keine Route ({d.get('status')})."
            leg = d["routes"][0]["legs"][0]
            dur = leg.get("duration_in_traffic", leg["duration"])["text"]
            return f"🗺️ {leg['start_address']} → {leg['end_address']}: {dur} ({leg['distance']['text']})"

        return [Tool(
            name="maps_route",
            description="Fahrzeit & Distanz zwischen zwei Orten (mit Verkehrslage).",
            parameters={"type": "object", "properties": {
                "origin": {"type": "string"}, "destination": {"type": "string"}},
                "required": ["origin", "destination"]},
            handler=_route, owner_only=True, source=self.slug,
        )]
