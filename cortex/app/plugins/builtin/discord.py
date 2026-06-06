"""Discord — post to a channel via an incoming webhook URL."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class DiscordPlugin(Plugin):
    slug = "discord"
    name = "Discord"
    description = "Nachrichten in einen Discord-Kanal posten (Webhook)."
    category = PluginCategory.COMMS
    icon = "🎮"
    config_fields = [
        ConfigField("webhook_url", "Webhook-URL", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Kanal → Einstellungen → Integrationen → Webhook erstellen → URL kopieren"),
        ConfigField("username", "Anzeigename", default="ASTRA",
                    help="Name, unter dem ASTRA postet"),
    ]

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(self.get("webhook_url"))
            if r.status_code == 200:
                return HealthStatus.ok(f"Webhook gültig: {r.json().get('name', 'ok')}")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Discord ist deaktiviert."
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(self.get("webhook_url"),
                                 json={"content": args.get("text", ""),
                                       "username": self.get("username", "ASTRA")})
            return "In Discord gepostet." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="discord_send",
            description="Poste eine Nachricht in den Discord-Kanal.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
