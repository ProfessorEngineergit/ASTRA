"""ntfy.sh — push notifications to a topic."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.ntfy")


class NtfyPlugin(Plugin):
    slug = "ntfy"
    name = "ntfy Push"
    description = "Push-Benachrichtigungen via ntfy.sh oder selbst gehosteten ntfy-Server."
    category = PluginCategory.COMMS
    icon = "🔔"
    config_fields = [
        ConfigField("server", "Server-URL", default="https://ntfy.sh",
                    help="Standard: https://ntfy.sh — oder eigene Instanz"),
        ConfigField("topic", "Topic", required=True,
                    help="ntfy-Topic-Name (ohne URL-Präfix)"),
        ConfigField("token", "Access-Token", FieldType.PASSWORD, secret=True,
                    help="Für private Topics — leer lassen bei öffentlichen Topics"),
    ]

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.get("token"):
            h["Authorization"] = f"Bearer {self.get('token')}"
        return h

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            server = (self.get("server") or "https://ntfy.sh").rstrip("/")
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{server}/v1/health")
            if r.status_code == 200:
                return HealthStatus.ok(f"ntfy erreichbar ({server})")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            server = (self.get("server") or "https://ntfy.sh").rstrip("/")
            topic = self.get("topic", "")
            title = args.get("title", "ASTRA")
            message = args.get("message", "")
            priority = int(args.get("priority", 3))
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(
                        f"{server}/{topic}",
                        headers={**self._headers(),
                                 "X-Title": title,
                                 "X-Priority": str(priority)},
                        content=message.encode(),
                    )
                    r.raise_for_status()
                return f"Benachrichtigung '{title}' gesendet."
            except Exception as e:
                return f"ntfy-Fehler: {e}"

        return [Tool(
            name="send_ntfy",
            description="Push-Benachrichtigung via ntfy senden.",
            parameters={"type": "object", "properties": {
                "title": {"type": "string", "description": "Titel der Nachricht"},
                "message": {"type": "string", "description": "Text der Nachricht"},
                "priority": {"type": "integer", "description": "1–5, Standard 3",
                             "minimum": 1, "maximum": 5},
            }, "required": ["message"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
