"""Slack — send messages via a bot token (chat.postMessage)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class SlackPlugin(Plugin):
    slug = "slack"
    name = "Slack"
    description = "Nachrichten in Slack-Kanäle senden (Bot-Token)."
    category = PluginCategory.COMMS
    icon = "💬"
    config_fields = [
        ConfigField("bot_token", "Bot-Token (xoxb-…)", type=FieldType.PASSWORD,
                    required=True, secret=True,
                    help="api.slack.com → App anlegen → OAuth → Bot Token. Scope: chat:write"),
        ConfigField("default_channel", "Standard-Kanal", default="#general",
                    help="Kanal-Name (#general) oder Channel-ID"),
    ]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.get('bot_token')}",
                "Content-Type": "application/json; charset=utf-8"}

    async def post(self, text: str, channel: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://slack.com/api/chat.postMessage", headers=self._headers(),
                             json={"channel": channel or self.get("default_channel"), "text": text})
            return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post("https://slack.com/api/auth.test", headers=self._headers())
            d = r.json()
            if d.get("ok"):
                return HealthStatus.ok(f"Verbunden als {d.get('user')} ({d.get('team')}).")
            return HealthStatus.error(f"Slack: {d.get('error')}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Slack ist deaktiviert."
            d = await self.post(args.get("text", ""), args.get("channel"))
            return "In Slack gepostet." if d.get("ok") else f"Slack-Fehler: {d.get('error')}"

        return [Tool(
            name="slack_send",
            description="Sende eine Nachricht in einen Slack-Kanal.",
            parameters={"type": "object", "properties": {
                "text": {"type": "string"},
                "channel": {"type": "string", "description": "optional, sonst Standard-Kanal"}},
                "required": ["text"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
