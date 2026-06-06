"""Gotify — self-hosted push notifications."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class GotifyPlugin(Plugin):
    slug = "gotify"
    name = "Gotify Push"
    description = "Self-hosted Push-Benachrichtigungen via Gotify."
    category = PluginCategory.COMMS
    icon = "🔔"
    config_fields = [
        ConfigField("server_url", "Server-URL", required=True, help="z. B. https://gotify.example.com"),
        ConfigField("app_token", "App-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Gotify → Apps → App anlegen → Token"),
    ]

    def _base(self) -> str:
        return self.get("server_url", "").rstrip("/")

    async def push(self, title: str, message: str, priority: int = 5) -> bool:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self._base()}/message?token={self.get('app_token')}",
                             json={"title": title, "message": message, "priority": priority})
            return r.status_code < 300

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self._base()}/version")
            return (HealthStatus.ok(f"Gotify {r.json().get('version', '')} erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Gotify ist deaktiviert."
            ok = await self.push(args.get("title", "ASTRA"), args.get("message", ""),
                                 int(args.get("priority", 5)))
            return "Push gesendet." if ok else "Push fehlgeschlagen."

        return [Tool(
            name="gotify_push",
            description="Sende eine Push-Benachrichtigung via Gotify.",
            parameters={"type": "object", "properties": {
                "title": {"type": "string"}, "message": {"type": "string"},
                "priority": {"type": "number"}}, "required": ["message"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
