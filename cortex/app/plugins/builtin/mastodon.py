"""Mastodon — post a status (toot) via an access token."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class MastodonPlugin(Plugin):
    slug = "mastodon"
    name = "Mastodon"
    description = "Posts (Toots) auf deiner Mastodon-Instanz veröffentlichen."
    category = PluginCategory.COMMS
    icon = "🐘"
    config_fields = [
        ConfigField("instance_url", "Instanz-URL", required=True, default="https://mastodon.social",
                    help="z. B. https://mastodon.social"),
        ConfigField("access_token", "Access-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Einstellungen → Entwicklung → Neue Anwendung → Zugriffstoken"),
        ConfigField("visibility", "Sichtbarkeit", type=FieldType.SELECT,
                    options=["public", "unlisted", "private", "direct"], default="public"),
    ]

    def _base(self) -> str:
        return self.get("instance_url", "").rstrip("/")

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self._base()}/api/v1/accounts/verify_credentials",
                                headers={"Authorization": f"Bearer {self.get('access_token')}"})
            if r.status_code == 200:
                return HealthStatus.ok(f"Verbunden als @{r.json().get('username')}.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _toot(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Mastodon ist deaktiviert."
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{self._base()}/api/v1/statuses",
                                 headers={"Authorization": f"Bearer {self.get('access_token')}"},
                                 json={"status": args.get("text", ""),
                                       "visibility": self.get("visibility", "public")})
            return "Getootet." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="mastodon_post",
            description="Veröffentliche einen Post (Toot) auf Mastodon.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=_toot, owner_only=True, source=self.slug,
        )]
