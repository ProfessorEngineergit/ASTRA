"""Readwise — recent highlights via the public API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class ReadwisePlugin(Plugin):
    slug = "readwise"
    name = "Readwise"
    description = "Letzte Highlights aus Readwise abrufen."
    category = PluginCategory.MEDIA
    icon = "📚"
    config_fields = [
        ConfigField("token", "Access-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="readwise.io/access_token"),
    ]

    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.get('token')}"}

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://readwise.io/api/v2/auth/", headers=self._headers())
            return (HealthStatus.ok("Readwise-Token gültig.") if r.status_code == 204
                    else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _recent(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Readwise ist deaktiviert."
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get("https://readwise.io/api/v2/highlights/",
                                headers=self._headers(), params={"page_size": 5})
            items = r.json().get("results", [])
            if not items:
                return "Keine Highlights gefunden."
            return "📚 Letzte Highlights:\n" + "\n".join(
                f"• {h.get('text', '')[:140]}" for h in items)

        return [Tool(
            name="readwise_recent",
            description="Letzte Readwise-Highlights.",
            parameters={"type": "object", "properties": {}},
            handler=_recent, owner_only=True, source=self.slug,
        )]
