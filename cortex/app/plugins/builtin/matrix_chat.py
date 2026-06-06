"""Matrix/Element — send messages into a room via the client-server API."""
from __future__ import annotations

import time

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class MatrixPlugin(Plugin):
    slug = "matrix"
    name = "Matrix/Element Chat"
    description = "Nachrichten in Matrix-Räume senden (Access-Token)."
    category = PluginCategory.COMMS
    icon = "💬"
    config_fields = [
        ConfigField("homeserver", "Homeserver-URL", required=True, default="https://matrix.org",
                    help="z. B. https://matrix.org oder deine eigene Instanz"),
        ConfigField("access_token", "Access-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Element → Einstellungen → Hilfe → Zugriffstoken"),
        ConfigField("room_id", "Raum-ID", required=True,
                    help="z. B. !abcdef:matrix.org (Raum-Einstellungen → Erweitert)"),
    ]

    def _base(self) -> str:
        return self.get("homeserver", "").rstrip("/")

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self._base()}/_matrix/client/v3/account/whoami",
                                headers={"Authorization": f"Bearer {self.get('access_token')}"})
            if r.status_code == 200:
                return HealthStatus.ok(f"Verbunden als {r.json().get('user_id')}.")
            return HealthStatus.error(f"HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Matrix ist deaktiviert."
            txn = str(int(time.time() * 1000))
            room = self.get("room_id")
            url = f"{self._base()}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}"
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.put(url, headers={"Authorization": f"Bearer {self.get('access_token')}"},
                                json={"msgtype": "m.text", "body": args.get("text", "")})
            return "In Matrix gesendet." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="matrix_send",
            description="Sende eine Nachricht in den Matrix-Raum.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
