"""TrueNAS — pool health + active alerts via the API key."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class TrueNasPlugin(Plugin):
    slug = "truenas"
    name = "TrueNAS"
    description = "Pool-Gesundheit & Alarme deines TrueNAS-Servers."
    category = PluginCategory.INFRA_AI
    icon = "🗄️"
    config_fields = [
        ConfigField("base_url", "TrueNAS-URL", required=True, help="z. B. https://nas.example.com"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="TrueNAS → Account → API Keys"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, verify=False, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Bearer {self.get('api_key')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/v2.0/system/info")
            if r.status_code == 200:
                return HealthStatus.ok(f"{r.json().get('hostname', 'TrueNAS')} erreichbar.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _alerts(self) -> str:
        async with self._client() as c:
            r = await c.get("/api/v2.0/alert/list")
        active = [a for a in r.json() if not a.get("dismissed")] if r.status_code == 200 else []
        if not active:
            return "✅ TrueNAS: keine aktiven Alarme."
        return "⚠️ TrueNAS-Alarme:\n" + "\n".join(
            f"• [{a.get('level')}] {a.get('formatted', a.get('text', ''))[:80]}" for a in active[:8])

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            s = await self._alerts()
            return s if "⚠️" in s else None
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _a(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "TrueNAS ist deaktiviert."
            return await self._alerts()

        return [Tool(
            name="truenas_alerts", description="Aktive TrueNAS-Alarme / Status.",
            parameters={"type": "object", "properties": {}},
            handler=_a, owner_only=True, source=self.slug,
        )]
