"""Last.fm — recent tracks + now playing via the public API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://ws.audioscrobbler.com/2.0/"


class LastfmPlugin(Plugin):
    slug = "lastfm"
    name = "Last.fm"
    description = "Zuletzt gehört, aktuell gespielt & Top-Tracks."
    category = PluginCategory.MEDIA
    icon = "🎵"
    config_fields = [
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="last.fm/api/account/create → API key"),
        ConfigField("username", "Last.fm-Benutzername", required=True),
    ]

    async def _call(self, method: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(API, params={"method": method, "user": self.get("username"),
                                         "api_key": self.get("api_key"), "format": "json", **params})
            return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            d = await self._call("user.getinfo")
            u = d.get("user")
            return (HealthStatus.ok(f"{u['name']} — {u.get('playcount', '?')} Scrobbles.") if u
                    else HealthStatus.error(d.get("message", "Fehler")))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _recent(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Last.fm ist deaktiviert."
            d = await self._call("user.getrecenttracks", limit=8)
            tracks = d.get("recenttracks", {}).get("track", [])
            if not tracks:
                return "Keine Tracks gefunden."
            out = []
            for t in tracks[:8]:
                now = "▶ " if t.get("@attr", {}).get("nowplaying") else "  "
                out.append(f"{now}{t['artist']['#text']} – {t['name']}")
            return "🎵 Zuletzt gehört:\n" + "\n".join(out)

        return [Tool(
            name="lastfm_recent",
            description="Zuletzt gehörte Tracks (inkl. aktuell gespielt).",
            parameters={"type": "object", "properties": {}},
            handler=_recent, owner_only=True, source=self.slug,
        )]
