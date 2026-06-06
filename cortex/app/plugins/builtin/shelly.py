"""Shelly — control a Shelly relay/plug over the local HTTP API (Gen1 + Gen2)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class ShellyPlugin(Plugin):
    slug = "shelly"
    name = "Shelly"
    description = "Shelly-Steckdosen & -Relais direkt im LAN schalten."
    category = PluginCategory.SMART_HOME
    icon = "⚡"
    config_fields = [
        ConfigField("device_ip", "Geräte-IP", required=True, help="IP des Shelly im LAN"),
        ConfigField("gen", "Generation", type=FieldType.SELECT, options=["gen2", "gen1"], default="gen2",
                    help="Plus/Pro = Gen2, ältere = Gen1"),
        ConfigField("password", "Passwort", secret=True, help="optional, falls Auth aktiviert"),
    ]

    def _auth(self):
        pw = self.get("password")
        return httpx.DigestAuth("admin", pw) if pw else None

    async def _toggle(self, on: bool) -> bool:
        ip = self.get("device_ip")
        if self.get("gen") == "gen1":
            url = f"http://{ip}/relay/0?turn={'on' if on else 'off'}"
        else:
            url = f"http://{ip}/rpc/Switch.Set?id=0&on={'true' if on else 'false'}"
        async with httpx.AsyncClient(timeout=8, auth=self._auth()) as c:
            r = await c.get(url)
            return r.status_code < 300

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        ip = self.get("device_ip")
        url = f"http://{ip}/shelly"
        try:
            async with httpx.AsyncClient(timeout=8, auth=self._auth()) as c:
                r = await c.get(url)
            if r.status_code == 200:
                d = r.json()
                return HealthStatus.ok(f"{d.get('model', d.get('type', 'Shelly'))} erreichbar.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _set(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Shelly ist deaktiviert."
            ok = await self._toggle(bool(args.get("on", True)))
            return ("Eingeschaltet." if args.get("on") else "Ausgeschaltet.") if ok else "Fehler."

        return [Tool(
            name="shelly_switch",
            description="Schalte das Shelly-Gerät ein oder aus.",
            parameters={"type": "object", "properties": {"on": {"type": "boolean"}},
                        "required": ["on"]},
            handler=_set, owner_only=True, source=self.slug,
        )]
