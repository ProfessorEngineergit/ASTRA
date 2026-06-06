"""Tasmota — control a Tasmota device via its HTTP command API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class TasmotaPlugin(Plugin):
    slug = "tasmota"
    name = "Tasmota"
    description = "Tasmota-Smart-Plugs & -Schalter über HTTP steuern."
    category = PluginCategory.SMART_HOME
    icon = "🔌"
    config_fields = [
        ConfigField("device_ip", "Geräte-IP", required=True),
        ConfigField("user", "Benutzer", default="admin", help="optional, falls WebPassword gesetzt"),
        ConfigField("password", "Passwort", secret=True, help="optional"),
    ]

    async def _cmd(self, cmnd: str) -> dict:
        params = {"cmnd": cmnd}
        if self.get("password"):
            params |= {"user": self.get("user", "admin"), "password": self.get("password")}
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"http://{self.get('device_ip')}/cm", params=params)
            return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            d = await self._cmd("Status 0")
            name = d.get("Status", {}).get("DeviceName", "Tasmota")
            return HealthStatus.ok(f"{name} erreichbar.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _power(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Tasmota ist deaktiviert."
            d = await self._cmd(f"Power {'ON' if args.get('on') else 'OFF'}")
            return f"Status: {d.get('POWER', 'unbekannt')}"

        return [Tool(
            name="tasmota_power",
            description="Schalte das Tasmota-Gerät ein/aus.",
            parameters={"type": "object", "properties": {"on": {"type": "boolean"}},
                        "required": ["on"]},
            handler=_power, owner_only=True, source=self.slug,
        )]
