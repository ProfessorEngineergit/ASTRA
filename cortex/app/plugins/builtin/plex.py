"""Plex — current sessions + library search via the X-Plex-Token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class PlexPlugin(Plugin):
    slug = "plex"
    name = "Plex"
    description = "Aktuelle Wiedergaben & Bibliothekssuche auf deinem Plex-Server."
    category = PluginCategory.MEDIA
    icon = "🎬"
    config_fields = [
        ConfigField("base_url", "Plex-URL", required=True, default="http://192.168.178.1:32400"),
        ConfigField("token", "X-Plex-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="support.plex.tv → 'Finding an authentication token'"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"X-Plex-Token": self.get("token"), "Accept": "application/json"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/")
            if r.status_code == 200:
                name = r.json().get("MediaContainer", {}).get("friendlyName", "Plex")
                return HealthStatus.ok(f"Verbunden mit '{name}'.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _sessions(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plex ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/status/sessions")
            md = r.json().get("MediaContainer", {})
            items = md.get("Metadata", [])
            if not items:
                return "Aktuell läuft nichts auf Plex."
            return "🎬 Läuft gerade:\n" + "\n".join(
                f"• {i.get('title')} — {i.get('User', {}).get('title', '?')}" for i in items)

        return [Tool(
            name="plex_sessions",
            description="Was läuft gerade auf dem Plex-Server?",
            parameters={"type": "object", "properties": {}},
            handler=_sessions, owner_only=True, source=self.slug,
        )]
