"""WLED — control an addressable-LED controller via its JSON HTTP API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class WledPlugin(Plugin):
    slug = "wled"
    name = "WLED"
    description = "LED-Streifen (WLED) ein/aus, Helligkeit & Farbe setzen."
    category = PluginCategory.SMART_HOME
    icon = "🌈"
    config_fields = [
        ConfigField("device_ip", "Geräte-IP", required=True, help="IP des WLED-Controllers"),
    ]

    def _base(self) -> str:
        return f"http://{self.get('device_ip')}"

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self._base()}/json/info")
            return (HealthStatus.ok(f"WLED '{r.json().get('name', '')}' erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _set(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "WLED ist deaktiviert."
            state: dict = {"on": bool(args.get("on", True))}
            if "brightness" in args:
                state["bri"] = max(1, min(255, int(args["brightness"])))
            if args.get("color"):
                hexs = args["color"].lstrip("#")
                state["seg"] = [{"col": [[int(hexs[0:2], 16), int(hexs[2:4], 16), int(hexs[4:6], 16)]]}]
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(f"{self._base()}/json/state", json=state)
            return "Gesetzt." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="wled_set",
            description="Steuere den WLED-Streifen: an/aus, Helligkeit, Farbe (#RRGGBB).",
            parameters={"type": "object", "properties": {
                "on": {"type": "boolean"}, "brightness": {"type": "number", "description": "1-255"},
                "color": {"type": "string", "description": "Hex z. B. #ff8800"}},
                "required": ["on"]},
            handler=_set, owner_only=True, source=self.slug,
        )]
