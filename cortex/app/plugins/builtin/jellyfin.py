"""Jellyfin — media server sessions and search."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.jellyfin")


class JellyfinPlugin(Plugin):
    slug = "jellyfin"
    name = "Jellyfin"
    description = "Aktuell laufende Wiedergaben anzeigen und die Medienbibliothek durchsuchen."
    category = PluginCategory.MEDIA
    icon = "🎬"
    config_fields = [
        ConfigField("base_url", "Jellyfin URL", required=True,
                    help="z.B. http://192.168.178.10:8096"),
        ConfigField("api_key", "API-Key", FieldType.PASSWORD, required=True, secret=True,
                    help="Jellyfin → Dashboard → API-Keys"),
        ConfigField("user_id", "User-ID", help="Für benutzerspezifische Abfragen (optional)"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=(self.get("base_url") or "").rstrip("/"),
            headers={"X-Emby-Token": self.get("api_key", "")},
            timeout=15,
        )

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/health")
                r.raise_for_status()
            return HealthStatus.ok("Jellyfin erreichbar.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _sessions(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with self._client() as c:
                    r = await c.get("/Sessions")
                    r.raise_for_status()
                sessions = [s for s in r.json() if s.get("NowPlayingItem")]
                if not sessions:
                    return "Keine aktiven Wiedergaben."
                lines = ["**Aktuelle Jellyfin-Wiedergaben**"]
                for s in sessions:
                    item = s["NowPlayingItem"]
                    user = s.get("UserName", "?")
                    client = s.get("Client", "?")
                    title = item.get("Name", "?")
                    itype = item.get("Type", "?")
                    pos = s.get("PlayState", {}).get("PositionTicks", 0) // 10_000_000
                    dur = item.get("RunTimeTicks", 0) // 10_000_000
                    lines.append(f"{user} ({client}): {itype} '{title}' — {pos}s/{dur}s")
                return "\n".join(lines)
            except Exception as e:
                return f"Jellyfin-Fehler: {e}"

        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            query = args.get("query", "").strip()
            if not query:
                return "query ist erforderlich."
            try:
                async with self._client() as c:
                    r = await c.get("/Items", params={
                        "searchTerm": query,
                        "Limit": 5,
                        "Recursive": "true",
                        "Fields": "Overview",
                    })
                    r.raise_for_status()
                items = r.json().get("Items", [])
                if not items:
                    return f"Keine Ergebnisse für '{query}'."
                lines = [f"**Suchergebnisse für '{query}'**"]
                for item in items:
                    year = item.get("ProductionYear", "")
                    lines.append(f"- {item.get('Type', '?')}: {item['Name']} ({year})")
                return "\n".join(lines)
            except Exception as e:
                return f"Jellyfin-Fehler: {e}"

        return [
            Tool(
                name="jellyfin_sessions",
                description="Aktuell laufende Jellyfin-Wiedergaben anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_sessions, owner_only=True, source=self.slug,
            ),
            Tool(
                name="jellyfin_search",
                description="Jellyfin-Medienbibliothek nach einem Titel durchsuchen.",
                parameters={"type": "object", "properties": {
                    "query": {"type": "string", "description": "Suchbegriff"},
                }, "required": ["query"]},
                handler=_search, owner_only=True, source=self.slug,
            ),
        ]
