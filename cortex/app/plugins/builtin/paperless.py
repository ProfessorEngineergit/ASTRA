"""Paperless-NGX — search documents via the REST API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class PaperlessPlugin(Plugin):
    slug = "paperless"
    name = "Paperless-NGX"
    description = "Dokumente durchsuchen in deinem Paperless-NGX-Archiv."
    category = PluginCategory.PRODUCTIVITY
    icon = "📄"
    config_fields = [
        ConfigField("base_url", "Paperless-URL", required=True, help="z. B. https://paperless.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Paperless → Einstellungen → API-Token"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Token {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/documents/", params={"page_size": 1})
            return (HealthStatus.ok(f"{r.json().get('count', 0)} Dokumente im Archiv.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Paperless ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/documents/", params={"query": args.get("query", ""),
                                                            "page_size": 5})
            items = r.json().get("results", [])
            if not items:
                return "Keine Dokumente gefunden."
            return "\n".join(f"• {d.get('title')} ({d.get('created', '')[:10]})" for d in items)

        return [Tool(
            name="paperless_search",
            description="Suche Dokumente in Paperless-NGX (Volltext).",
            parameters={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"]},
            handler=_search, owner_only=True, source=self.slug,
        )]
