"""Miniflux — unread RSS entries via the API token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class MinifluxPlugin(Plugin):
    slug = "miniflux"
    name = "Miniflux"
    description = "Ungelesene RSS-Artikel aus deinem Miniflux-Reader."
    category = PluginCategory.MEDIA
    icon = "📰"
    config_fields = [
        ConfigField("base_url", "Miniflux-URL", required=True, help="z. B. https://reader.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Miniflux → Settings → API Keys"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"X-Auth-Token": self.get("token")})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/v1/me")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('username')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _unread(self, limit: int = 8) -> str:
        async with self._client() as c:
            r = await c.get("/v1/entries", params={"status": "unread", "limit": limit,
                                                   "direction": "desc"})
        entries = r.json().get("entries", [])
        if not entries:
            return "Keine ungelesenen Artikel."
        return "📰 Ungelesen:\n" + "\n".join(
            f"• {e['title']} — {e.get('feed', {}).get('title', '')}" for e in entries[:limit])

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            return await self._unread(5)
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _u(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Miniflux ist deaktiviert."
            return await self._unread(int(args.get("limit", 8)))

        return [Tool(
            name="miniflux_unread", description="Ungelesene Miniflux-RSS-Artikel.",
            parameters={"type": "object", "properties": {"limit": {"type": "number"}}},
            handler=_u, owner_only=True, source=self.slug,
        )]
