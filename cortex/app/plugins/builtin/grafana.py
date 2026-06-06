"""Grafana — firing alerts + dashboard list via an API token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class GrafanaPlugin(Plugin):
    slug = "grafana"
    name = "Grafana"
    description = "Feuernde Alerts & Dashboards aus Grafana."
    category = PluginCategory.INFRA_AI
    icon = "📊"
    config_fields = [
        ConfigField("base_url", "Grafana-URL", required=True, help="z. B. https://grafana.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Grafana → Administration → Service accounts → Token"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Bearer {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/health")
            return (HealthStatus.ok(f"Grafana {r.json().get('version', '')} erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _alerts(self) -> str:
        async with self._client() as c:
            r = await c.get("/api/alertmanager/grafana/api/v2/alerts")
        if r.status_code != 200:
            return f"Alerts nicht abrufbar (HTTP {r.status_code})."
        firing = [a for a in r.json() if a.get("status", {}).get("state") == "active"]
        if not firing:
            return "✅ Grafana: keine feuernden Alerts."
        names = [a.get("labels", {}).get("alertname", "?") for a in firing]
        return "⚠️ Grafana-Alerts: " + ", ".join(names[:8])

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
                return "Grafana ist deaktiviert."
            return await self._alerts()

        return [Tool(
            name="grafana_alerts",
            description="Aktuell feuernde Grafana-Alerts.",
            parameters={"type": "object", "properties": {}},
            handler=_a, owner_only=True, source=self.slug,
        )]
