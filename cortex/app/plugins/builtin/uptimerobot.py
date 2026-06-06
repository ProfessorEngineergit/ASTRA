"""UptimeRobot — monitor status via the API key."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://api.uptimerobot.com/v2"
_STATE = {0: "⏸ pausiert", 1: "❓ noch nicht geprüft", 2: "🟢 up", 8: "🟠 seems down", 9: "🔴 down"}


class UptimeRobotPlugin(Plugin):
    slug = "uptimerobot"
    name = "UptimeRobot"
    description = "Status deiner UptimeRobot-Monitore (Cloud-Uptime)."
    category = PluginCategory.INFRA_AI
    icon = "🟢"
    config_fields = [
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="uptimerobot.com → My Settings → API → Read-Only API Key"),
    ]

    async def _monitors(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(f"{API}/getMonitors", data={"api_key": self.get("api_key"),
                                                         "format": "json"})
        return r.json().get("monitors", [])

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            m = await self._monitors()
            return HealthStatus.ok(f"{len(m)} Monitore.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _summary(self) -> str:
        m = await self._monitors()
        down = [x for x in m if x.get("status") in (8, 9)]
        if down:
            return "🔴 Down: " + ", ".join(x.get("friendly_name", "?") for x in down)
        return f"🟢 Alle {len(m)} Monitore up."

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            s = await self._summary()
            return s if "🔴" in s else None
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "UptimeRobot ist deaktiviert."
            m = await self._monitors()
            if not m:
                return "Keine Monitore."
            return "\n".join(f"{_STATE.get(x.get('status'), '?')} {x.get('friendly_name')}"
                             for x in m[:25])

        return [Tool(
            name="uptimerobot_status", description="Status aller UptimeRobot-Monitore.",
            parameters={"type": "object", "properties": {}},
            handler=_status, owner_only=True, source=self.slug,
        )]
