"""Immich — server stats + search via the API key."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class ImmichPlugin(Plugin):
    slug = "immich"
    name = "Immich"
    description = "Foto-Statistiken & Suche in deiner Immich-Instanz."
    category = PluginCategory.MEDIA
    icon = "🖼️"
    config_fields = [
        ConfigField("base_url", "Immich-URL", required=True, help="z. B. https://photos.example.com"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Immich → Account-Einstellungen → API-Keys"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"x-api-key": self.get("api_key")})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/server/statistics")
                if r.status_code == 404:
                    r = await c.get("/api/server-info/statistics")
            if r.status_code == 200:
                d = r.json()
                return HealthStatus.ok(f"{d.get('photos', '?')} Fotos, {d.get('videos', '?')} Videos.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Immich ist deaktiviert."
            async with self._client() as c:
                r = await c.post("/api/search/smart", json={"query": args.get("query", "")})
            if r.status_code >= 300:
                return f"Suche fehlgeschlagen (HTTP {r.status_code})."
            items = r.json().get("assets", {}).get("items", [])
            return f"🖼️ {len(items)} Treffer für '{args.get('query')}'." if items else "Keine Treffer."

        return [Tool(
            name="immich_search",
            description="Suche Fotos in Immich (semantisch).",
            parameters={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]},
            handler=_search, owner_only=True, source=self.slug,
        )]
