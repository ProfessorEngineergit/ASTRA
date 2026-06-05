"""Pushover — push notifications via the Pushover API."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.pushover")

_API = "https://api.pushover.net/1"


class PushoverPlugin(Plugin):
    slug = "pushover"
    name = "Pushover Push"
    description = "Push-Benachrichtigungen über die Pushover-App (iOS/Android)."
    category = PluginCategory.COMMS
    icon = "📲"
    config_fields = [
        ConfigField("app_token", "App-Token", FieldType.PASSWORD, required=True, secret=True,
                    help="API-Token der Pushover-Applikation"),
        ConfigField("user_key", "User-Key", FieldType.PASSWORD, required=True, secret=True,
                    help="Dein persönlicher Pushover User Key"),
    ]

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(f"{_API}/users/validate.json", data={
                    "token": self.get("app_token", ""),
                    "user": self.get("user_key", ""),
                })
            data = r.json()
            if data.get("status") == 1:
                return HealthStatus.ok("User validiert.")
            return HealthStatus.error(f"Validierung fehlgeschlagen: {data.get('errors', '')}")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(f"{_API}/messages.json", data={
                        "token": self.get("app_token", ""),
                        "user": self.get("user_key", ""),
                        "message": args.get("message", ""),
                        "title": args.get("title", "ASTRA"),
                    })
                    data = r.json()
                if data.get("status") == 1:
                    return f"Pushover-Nachricht '{args.get('title', 'ASTRA')}' gesendet."
                return f"Pushover-Fehler: {data.get('errors', 'Unbekannt')}"
            except Exception as e:
                return f"Pushover-Fehler: {e}"

        return [Tool(
            name="send_pushover",
            description="Push-Benachrichtigung via Pushover senden.",
            parameters={"type": "object", "properties": {
                "message": {"type": "string", "description": "Nachrichtentext"},
                "title": {"type": "string", "description": "Titel (Standard: ASTRA)"},
            }, "required": ["message"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
