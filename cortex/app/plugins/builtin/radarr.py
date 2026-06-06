"""Radarr — download queue via the API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class RadarrPlugin(Plugin):
    slug = "radarr"
    name = "Radarr"
    description = "Film-Download-Warteschlange & Status aus Radarr."
    category = PluginCategory.MEDIA
    icon = "🎞️"
    config_fields = [
        ConfigField("base_url", "Radarr-URL", required=True, default="http://192.168.178.189:7878"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Radarr → Settings → General → API Key"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"X-Api-Key": self.get("api_key")})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/v3/system/status")
            return (HealthStatus.ok(f"Radarr {r.json().get('version', '')} erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _queue(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Radarr ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/v3/queue", params={"pageSize": 20})
            items = r.json().get("records", [])
            if not items:
                return "Download-Warteschlange ist leer."
            out = []
            for q in items[:12]:
                pct = 100 - (q.get("sizeleft", 0) * 100 // max(q.get("size", 1), 1))
                out.append(f"• {q.get('title', '?')[:50]} — {pct}% ({q.get('status', '')})")
            return "🎞️ Downloads:\n" + "\n".join(out)

        return [Tool(
            name="radarr_queue",
            description="Aktuelle Radarr-Download-Warteschlange.",
            parameters={"type": "object", "properties": {}},
            handler=_queue, owner_only=True, source=self.slug,
        )]
