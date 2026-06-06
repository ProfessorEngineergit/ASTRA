"""Obsidian — append notes via the Local REST API community plugin."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class ObsidianPlugin(Plugin):
    slug = "obsidian"
    name = "Obsidian"
    description = "Notizen in deinen Vault schreiben (Local REST API Plugin)."
    category = PluginCategory.PRODUCTIVITY
    icon = "🔮"
    config_fields = [
        ConfigField("base_url", "API-URL", required=True, default="https://127.0.0.1:27124",
                    help="Aus dem 'Local REST API'-Plugin (meist https://<host>:27124)"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True),
        ConfigField("inbox_file", "Inbox-Datei", default="Inbox.md",
                    help="Datei, an die neue Notizen angehängt werden"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, verify=False,
                                 headers={"Authorization": f"Bearer {self.get('api_key')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get(f"{self.get('base_url').rstrip('/')}/")
            return (HealthStatus.ok("Obsidian Local REST API erreichbar.")
                    if r.status_code < 400 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _note(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Obsidian ist deaktiviert."
            text = args.get("text", "")
            path = self.get("inbox_file", "Inbox.md")
            async with self._client() as c:
                r = await c.post(f"{self.get('base_url').rstrip('/')}/vault/{path}",
                                 content=f"\n- {text}",
                                 headers={"Content-Type": "text/markdown"})
            return "In Obsidian gespeichert." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="obsidian_note",
            description="Hänge eine Notiz an die Obsidian-Inbox an.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=_note, owner_only=True, source=self.slug,
        )]
