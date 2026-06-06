"""qBittorrent — torrent list via the WebUI API (cookie login)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class QbittorrentPlugin(Plugin):
    slug = "qbittorrent"
    name = "qBittorrent"
    description = "Aktive Torrents & Fortschritt aus qBittorrent."
    category = PluginCategory.INFRA_AI
    icon = "🧲"
    config_fields = [
        ConfigField("base_url", "WebUI-URL", required=True, default="http://192.168.178.189:8080"),
        ConfigField("username", "Benutzer", required=True, default="admin"),
        ConfigField("password", "Passwort", required=True, secret=True),
    ]

    async def _login(self, c: httpx.AsyncClient) -> bool:
        r = await c.post(f"{self.get('base_url').rstrip('/')}/api/v2/auth/login",
                         data={"username": self.get("username"), "password": self.get("password")},
                         headers={"Referer": self.get("base_url").rstrip("/")})
        return r.status_code == 200 and "Ok" in r.text

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                ok = await self._login(c)
                if not ok:
                    return HealthStatus.error("Login fehlgeschlagen.")
                r = await c.get(f"{self.get('base_url').rstrip('/')}/api/v2/app/version")
            return HealthStatus.ok(f"qBittorrent {r.text} erreichbar.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _list(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "qBittorrent ist deaktiviert."
            async with httpx.AsyncClient(timeout=12) as c:
                if not await self._login(c):
                    return "Login fehlgeschlagen."
                r = await c.get(f"{self.get('base_url').rstrip('/')}/api/v2/torrents/info",
                                params={"filter": "downloading", "limit": 15})
            items = r.json()
            if not items:
                return "Keine aktiven Downloads."
            return "🧲 Aktiv:\n" + "\n".join(
                f"• {t['name'][:50]} — {t.get('progress', 0) * 100:.0f}%" for t in items[:12])

        return [Tool(
            name="qbittorrent_active",
            description="Liste aktive qBittorrent-Downloads mit Fortschritt.",
            parameters={"type": "object", "properties": {}},
            handler=_list, owner_only=True, source=self.slug,
        )]
