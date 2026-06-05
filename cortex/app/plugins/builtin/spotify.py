"""Spotify — playback status and controls via OAuth refresh token."""
from __future__ import annotations

import base64
import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.spotify")

_ACCOUNTS = "https://accounts.spotify.com"
_API = "https://api.spotify.com/v1"


class SpotifyPlugin(Plugin):
    slug = "spotify"
    name = "Spotify"
    description = "Aktuell spielenden Track anzeigen und Wiedergabe steuern."
    category = PluginCategory.MEDIA
    icon = "🎵"
    config_fields = [
        ConfigField("client_id", "Client-ID", required=True,
                    help="Spotify Developer Dashboard → App → Client ID"),
        ConfigField("client_secret", "Client-Secret", FieldType.PASSWORD,
                    required=True, secret=True),
        ConfigField("refresh_token", "Refresh-Token", FieldType.PASSWORD,
                    required=True, secret=True,
                    help="Einmalig via OAuth holen: https://developer.spotify.com/documentation/web-api"),
    ]

    async def _get_access_token(self) -> str:
        cid = self.get("client_id", "")
        secret = self.get("client_secret", "")
        refresh = self.get("refresh_token", "")
        creds = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{_ACCOUNTS}/api/token",
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
            r.raise_for_status()
        return r.json()["access_token"]

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            token = await self._get_access_token()
            if token:
                return HealthStatus.ok("Access-Token erfolgreich abgerufen.")
            return HealthStatus.error("Kein Access-Token erhalten.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _now_playing(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                token = await self._get_access_token()
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(f"{_API}/me/player/currently-playing",
                                    headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 204:
                    return "Aktuell läuft nichts auf Spotify."
                r.raise_for_status()
                data = r.json()
                if not data or not data.get("item"):
                    return "Keine Wiedergabeinformation verfügbar."
                item = data["item"]
                artists = ", ".join(a["name"] for a in item.get("artists", []))
                track = item.get("name", "?")
                album = item.get("album", {}).get("name", "")
                progress = data.get("progress_ms", 0) // 1000
                duration = item.get("duration_ms", 0) // 1000
                state = "▶" if data.get("is_playing") else "⏸"
                return (f"{state} {artists} — {track}\n"
                        f"Album: {album}\n"
                        f"Position: {progress}s/{duration}s")
            except Exception as e:
                return f"Spotify-Fehler: {e}"

        async def _control(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            action = args.get("action", "").lower()
            if action not in ("play", "pause", "next", "previous"):
                return "action muss play, pause, next oder previous sein."
            try:
                token = await self._get_access_token()
                method = "POST" if action in ("next", "previous") else "PUT"
                endpoint = {"play": "/me/player/play", "pause": "/me/player/pause",
                            "next": "/me/player/next",
                            "previous": "/me/player/previous"}[action]
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.request(
                        method, f"{_API}{endpoint}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                if r.status_code in (200, 204):
                    return f"Spotify: {action} ausgeführt."
                return f"Spotify-Fehler: HTTP {r.status_code}"
            except Exception as e:
                return f"Spotify-Fehler: {e}"

        return [
            Tool(
                name="spotify_now_playing",
                description="Aktuell auf Spotify spielenden Track anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_now_playing, owner_only=True, source=self.slug,
            ),
            Tool(
                name="spotify_control",
                description="Spotify-Wiedergabe steuern: play, pause, next, previous.",
                parameters={"type": "object", "properties": {
                    "action": {"type": "string",
                               "enum": ["play", "pause", "next", "previous"]},
                }, "required": ["action"]},
                handler=_control, owner_only=True, source=self.slug,
            ),
        ]
