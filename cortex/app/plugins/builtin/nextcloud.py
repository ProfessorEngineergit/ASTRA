"""Nextcloud — notes (Notes app API) + create note."""
from __future__ import annotations

import base64

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class NextcloudPlugin(Plugin):
    slug = "nextcloud"
    name = "Nextcloud"
    description = "Notizen lesen & anlegen (Nextcloud Notes-App)."
    category = PluginCategory.PRODUCTIVITY
    icon = "☁️"
    config_fields = [
        ConfigField("base_url", "Nextcloud-URL", required=True, help="z. B. https://cloud.example.com"),
        ConfigField("username", "Benutzer", required=True),
        ConfigField("app_password", "App-Passwort", required=True, secret=True,
                    help="Einstellungen → Sicherheit → App-Passwort erstellen"),
    ]

    def _client(self) -> httpx.AsyncClient:
        cred = base64.b64encode(
            f"{self.get('username')}:{self.get('app_password')}".encode()).decode()
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Basic {cred}",
                                          "OCS-APIRequest": "true", "Accept": "application/json"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/index.php/apps/notes/api/v1/notes", params={"pruneBefore": 0})
            return (HealthStatus.ok(f"Notes erreichbar — {len(r.json())} Notizen.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Nextcloud ist deaktiviert."
            async with self._client() as c:
                r = await c.post("/index.php/apps/notes/api/v1/notes",
                                 json={"title": args.get("title", "ASTRA"),
                                       "content": args.get("content", "")})
            return "Notiz in Nextcloud angelegt." if r.status_code < 300 \
                else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="nextcloud_note",
            description="Lege eine Notiz in Nextcloud Notes an.",
            parameters={"type": "object", "properties": {
                "title": {"type": "string"}, "content": {"type": "string"}},
                "required": ["content"]},
            handler=_add, owner_only=True, source=self.slug,
        )]
