"""Pocket Casts — "up next" queue via the web API (email/password login)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory

API = "https://api.pocketcasts.com"


class PocketCastsPlugin(Plugin):
    slug = "pocket_casts"
    name = "Pocket Casts"
    description = "Deine Podcast-Warteschlange (Up Next) abrufen."
    category = PluginCategory.MEDIA
    icon = "🎙️"
    config_fields = [
        ConfigField("email", "E-Mail", required=True),
        ConfigField("password", "Passwort", required=True, secret=True),
    ]

    async def _token(self, c: httpx.AsyncClient) -> str | None:
        r = await c.post(f"{API}/user/login",
                         json={"email": self.get("email"), "password": self.get("password"),
                               "scope": "webplayer"})
        return r.json().get("token") if r.status_code == 200 else None

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=12) as c:
                tok = await self._token(c)
            return (HealthStatus.ok("Pocket Casts Login erfolgreich.") if tok
                    else HealthStatus.error("Login fehlgeschlagen — E-Mail/Passwort prüfen."))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _next(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Pocket Casts ist deaktiviert."
            async with httpx.AsyncClient(timeout=15) as c:
                tok = await self._token(c)
                if not tok:
                    return "Login fehlgeschlagen."
                r = await c.post(f"{API}/up_next/list", headers={"Authorization": f"Bearer {tok}"},
                                 json={"version": 2})
            episodes = r.json().get("episodes", [])
            if not episodes:
                return "Warteschlange ist leer."
            return "🎙️ Als Nächstes:\n" + "\n".join(
                f"• {e.get('title')} — {e.get('podcastTitle', '')}" for e in episodes[:8])

        return [Tool(
            name="pocketcasts_up_next",
            description="Zeige die Pocket-Casts-Warteschlange (Up Next).",
            parameters={"type": "object", "properties": {}},
            handler=_next, owner_only=True, source=self.slug,
        )]
