"""YouTube — search videos via the Data API v3."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://www.googleapis.com/youtube/v3"


class YouTubePlugin(Plugin):
    slug = "youtube"
    name = "YouTube"
    description = "YouTube-Videos suchen (Data API v3)."
    category = PluginCategory.MEDIA
    icon = "▶️"
    config_fields = [
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="console.cloud.google.com → YouTube Data API v3 aktivieren → Key"),
    ]

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/search", params={"part": "snippet", "q": "test",
                                                         "maxResults": 1, "key": self.get("api_key")})
            return (HealthStatus.ok("YouTube Data API erreichbar.") if r.status_code == 200
                    else HealthStatus.error(f"HTTP {r.status_code}: {r.json().get('error', {}).get('message', '')[:70]}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "YouTube ist deaktiviert."
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(f"{API}/search", params={"part": "snippet", "q": args.get("query", ""),
                                                         "maxResults": 5, "type": "video",
                                                         "key": self.get("api_key")})
            items = r.json().get("items", [])
            if not items:
                return "Keine Videos gefunden."
            return "\n".join(
                f"• {i['snippet']['title']} — https://youtu.be/{i['id']['videoId']}" for i in items)

        return [Tool(
            name="youtube_search",
            description="Suche YouTube-Videos.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]},
            handler=_search, owner_only=True, source=self.slug,
        )]
