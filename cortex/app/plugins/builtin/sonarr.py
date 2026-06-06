"""Sonarr — upcoming episodes via the API."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class SonarrPlugin(Plugin):
    slug = "sonarr"
    name = "Sonarr"
    description = "Anstehende Serien-Episoden aus Sonarr."
    category = PluginCategory.MEDIA
    icon = "📺"
    config_fields = [
        ConfigField("base_url", "Sonarr-URL", required=True, default="http://192.168.178.189:8989"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Sonarr → Settings → General → API Key"),
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
            return (HealthStatus.ok(f"Sonarr {r.json().get('version', '')} erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _cal(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Sonarr ist deaktiviert."
            start = datetime.utcnow().date()
            end = start + timedelta(days=int(args.get("days", 7)))
            async with self._client() as c:
                r = await c.get("/api/v3/calendar",
                                params={"start": str(start), "end": str(end)})
            items = r.json()
            if not items:
                return "Keine anstehenden Episoden."
            out = []
            for e in items[:12]:
                air = (e.get("airDate") or "")[:10]
                out.append(f"• {air} {e.get('series', {}).get('title', '?')} "
                           f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}")
            return "📺 Anstehend:\n" + "\n".join(out)

        return [Tool(
            name="sonarr_upcoming",
            description="Anstehende Episoden aus Sonarr (Standard: 7 Tage).",
            parameters={"type": "object", "properties": {"days": {"type": "number"}}},
            handler=_cal, owner_only=True, source=self.slug,
        )]
