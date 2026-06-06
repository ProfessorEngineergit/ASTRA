"""Netdata — server health snapshot via the local API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class NetdataPlugin(Plugin):
    slug = "netdata"
    name = "Netdata"
    description = "CPU/RAM/Last & Alarme deines Servers aus Netdata."
    category = PluginCategory.INFRA_AI
    icon = "📈"
    config_fields = [
        ConfigField("base_url", "Netdata-URL", required=True, default="http://192.168.178.189:19999"),
    ]

    def _base(self) -> str:
        return self.get("base_url", "").rstrip("/")

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self._base()}/api/v1/info")
            if r.status_code == 200:
                d = r.json()
                return HealthStatus.ok(f"{d.get('hostname', 'Server')} — "
                                       f"{d.get('cores_total', '?')} Kerne erreichbar.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _snapshot(self) -> str:
        async with httpx.AsyncClient(timeout=10) as c:
            alarms = await c.get(f"{self._base()}/api/v1/alarms", params={"active": "true"})
        active = alarms.json().get("alarms", {}) if alarms.status_code == 200 else {}
        crit = [a["name"] for a in active.values() if a.get("status") in ("CRITICAL", "WARNING")]
        if crit:
            return "⚠️ Netdata-Alarme: " + ", ".join(crit[:8])
        return "✅ Netdata: keine aktiven Alarme."

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            snap = await self._snapshot()
            return snap if "⚠️" in snap else None  # only surface problems in the briefing
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Netdata ist deaktiviert."
            return await self._snapshot()

        return [Tool(
            name="netdata_alarms",
            description="Aktive Netdata-Alarme / Server-Gesundheit.",
            parameters={"type": "object", "properties": {}},
            handler=_status, owner_only=True, source=self.slug,
        )]
