"""Tailscale — device list via the API (access token)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class TailscalePlugin(Plugin):
    slug = "tailscale"
    name = "Tailscale"
    description = "Geräte & Online-Status in deinem Tailnet."
    category = PluginCategory.INFRA_AI
    icon = "🔗"
    config_fields = [
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="login.tailscale.com → Settings → Keys → API access token"),
        ConfigField("tailnet", "Tailnet", default="-",
                    help="meist '-' (Standard-Tailnet) oder deine org"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url="https://api.tailscale.com",
                                 headers={"Authorization": f"Bearer {self.get('api_key')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get(f"/api/v2/tailnet/{self.get('tailnet', '-')}/devices")
            if r.status_code == 200:
                return HealthStatus.ok(f"{len(r.json().get('devices', []))} Geräte im Tailnet.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _devices(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Tailscale ist deaktiviert."
            async with self._client() as c:
                r = await c.get(f"/api/v2/tailnet/{self.get('tailnet', '-')}/devices")
            devs = r.json().get("devices", [])
            if not devs:
                return "Keine Geräte."
            out = []
            for d in devs[:20]:
                online = "🟢" if d.get("online") else "⚪"
                out.append(f"{online} {d.get('hostname', '?')} ({d.get('addresses', ['?'])[0]})")
            return "🔗 Tailnet:\n" + "\n".join(out)

        return [Tool(
            name="tailscale_devices", description="Liste Tailscale-Geräte mit Online-Status.",
            parameters={"type": "object", "properties": {}},
            handler=_devices, owner_only=True, source=self.slug,
        )]
