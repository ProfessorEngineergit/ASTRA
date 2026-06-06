"""Notion — append a page to a database via the official API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

VERSION = "2022-06-28"


class NotionPlugin(Plugin):
    slug = "notion"
    name = "Notion"
    description = "Seiten in einer Notion-Datenbank anlegen & suchen."
    category = PluginCategory.PRODUCTIVITY
    icon = "📝"
    config_fields = [
        ConfigField("token", "Integration-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="notion.so/my-integrations → Internal Integration Secret (secret_…)"),
        ConfigField("database_id", "Datenbank-ID", required=True,
                    help="Die ID aus der Datenbank-URL; Integration muss freigegeben sein"),
        ConfigField("title_property", "Titel-Eigenschaft", default="Name",
                    help="Name der Titel-Spalte der Datenbank"),
    ]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get('token')}", "Notion-Version": VERSION,
                "Content-Type": "application/json"}

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(f"https://api.notion.com/v1/databases/{self.get('database_id')}",
                                headers=self._headers())
            if r.status_code == 200:
                title = "".join(t.get("plain_text", "") for t in r.json().get("title", []))
                return HealthStatus.ok(f"Datenbank '{title or 'ok'}' erreichbar.")
            return HealthStatus.error(f"HTTP {r.status_code}: {r.json().get('message', '')[:80]}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Notion ist deaktiviert."
            title = args.get("title", "").strip()
            payload = {"parent": {"database_id": self.get("database_id")},
                       "properties": {self.get("title_property", "Name"): {
                           "title": [{"text": {"content": title}}]}}}
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post("https://api.notion.com/v1/pages", headers=self._headers(),
                                 json=payload)
            return f"Notion-Seite '{title}' angelegt." if r.status_code < 300 \
                else f"Fehler: {r.json().get('message', r.status_code)}"

        return [Tool(
            name="notion_add_page",
            description="Lege eine neue Seite in der Notion-Datenbank an.",
            parameters={"type": "object", "properties": {"title": {"type": "string"}},
                        "required": ["title"]},
            handler=_add, owner_only=True, source=self.slug,
        )]
