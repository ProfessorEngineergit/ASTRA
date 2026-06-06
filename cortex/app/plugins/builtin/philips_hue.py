"""Philips Hue — control lights via the local Bridge API (v1)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class PhilipsHuePlugin(Plugin):
    slug = "philips_hue"
    name = "Philips Hue"
    description = "Lampen & Gruppen steuern über die lokale Hue Bridge."
    category = PluginCategory.SMART_HOME
    icon = "💡"
    config_fields = [
        ConfigField("bridge_ip", "Bridge-IP", required=True, default="192.168.178.1",
                    help="IP der Hue Bridge (im Router oder in der Hue-App)"),
        ConfigField("username", "API-Username", required=True, secret=True,
                    help="Einmalig erzeugen: Bridge-Knopf drücken, dann POST /api {devicetype}"),
    ]

    def _base(self) -> str:
        return f"http://{self.get('bridge_ip')}/api/{self.get('username')}"

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self._base()}/lights")
            d = r.json()
            if isinstance(d, list) and d and "error" in d[0]:
                return HealthStatus.error(d[0]["error"].get("description", "Fehler"))
            return HealthStatus.ok(f"{len(d)} Lampen gefunden.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _lights(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Hue ist deaktiviert."
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self._base()}/lights")
            d = r.json()
            return "\n".join(f"• [{k}] {v['name']}: {'an' if v['state']['on'] else 'aus'}"
                             for k, v in d.items()) or "Keine Lampen."

        async def _set(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Hue ist deaktiviert."
            lid = args.get("light_id")
            body: dict = {"on": bool(args.get("on", True))}
            if "brightness" in args:
                body["bri"] = max(1, min(254, int(args["brightness"])))
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.put(f"{self._base()}/lights/{lid}/state", json=body)
            return "Gesetzt." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [
            Tool(name="hue_lights", description="Liste alle Hue-Lampen mit Status.",
                 parameters={"type": "object", "properties": {}},
                 handler=_lights, owner_only=True, source=self.slug),
            Tool(name="hue_set", description="Schalte/dimme eine Hue-Lampe (light_id aus hue_lights).",
                 parameters={"type": "object", "properties": {
                     "light_id": {"type": "string"}, "on": {"type": "boolean"},
                     "brightness": {"type": "number", "description": "1-254"}},
                     "required": ["light_id", "on"]},
                 handler=_set, owner_only=True, source=self.slug),
        ]
