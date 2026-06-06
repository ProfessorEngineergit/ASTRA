"""Audiobookshelf — items in progress via the API token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class AudiobookshelfPlugin(Plugin):
    slug = "audiobookshelf"
    name = "Audiobookshelf"
    description = "Hörbücher & Podcasts, die du gerade hörst."
    category = PluginCategory.MEDIA
    icon = "🎧"
    config_fields = [
        ConfigField("base_url", "Server-URL", required=True, help="z. B. https://abs.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Audiobookshelf → Account → API Token"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Bearer {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/me")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('username')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _progress(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Audiobookshelf ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/me/items-in-progress")
            items = r.json().get("libraryItems", [])
            if not items:
                return "Nichts in Arbeit."
            out = []
            for it in items[:8]:
                md = it.get("media", {}).get("metadata", {})
                out.append(f"• {md.get('title', '?')} — {md.get('authorName', '')}")
            return "🎧 Gerade dran:\n" + "\n".join(out)

        return [Tool(
            name="abs_in_progress", description="Hörbücher/Podcasts, die gerade laufen.",
            parameters={"type": "object", "properties": {}},
            handler=_progress, owner_only=True, source=self.slug,
        )]
