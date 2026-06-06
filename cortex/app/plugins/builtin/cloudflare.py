"""Cloudflare — list zones / DNS overview via an API token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class CloudflarePlugin(Plugin):
    slug = "cloudflare"
    name = "Cloudflare"
    description = "Deine Zonen & DNS-Status bei Cloudflare."
    category = PluginCategory.INFRA_AI
    icon = "☁️"
    config_fields = [
        ConfigField("api_token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="dash.cloudflare.com → My Profile → API Tokens (Zone.Read)"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url="https://api.cloudflare.com/client/v4",
                                 headers={"Authorization": f"Bearer {self.get('api_token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/user/tokens/verify")
            ok = r.status_code == 200 and r.json().get("success")
            return HealthStatus.ok("Token gültig.") if ok else HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _zones(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Cloudflare ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/zones", params={"per_page": 20})
            zones = r.json().get("result", [])
            if not zones:
                return "Keine Zonen."
            return "☁️ Zonen:\n" + "\n".join(
                f"• {z['name']} — {z.get('status')}" for z in zones[:20])

        return [Tool(
            name="cloudflare_zones", description="Liste Cloudflare-Zonen (Domains) mit Status.",
            parameters={"type": "object", "properties": {}},
            handler=_zones, owner_only=True, source=self.slug,
        )]
