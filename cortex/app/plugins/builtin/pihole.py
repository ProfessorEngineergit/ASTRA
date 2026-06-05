"""Pi-hole DNS — stats and enable/disable toggle."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.pihole")


class PiholePlugin(Plugin):
    slug = "pihole"
    name = "Pi-hole DNS"
    description = "Pi-hole-Statistiken anzeigen und DNS-Filterung ein-/ausschalten."
    category = PluginCategory.INFRA_AI
    icon = "🕳️"
    config_fields = [
        ConfigField("base_url", "Pi-hole URL", required=True,
                    default="http://pi.hole",
                    help="Basis-URL deines Pi-hole (z.B. http://192.168.178.2)"),
        ConfigField("api_key", "API-Key", FieldType.PASSWORD, required=True, secret=True,
                    help="Pi-hole → Einstellungen → API/Web-Interface → API-Key"),
    ]

    def _api_url(self) -> str:
        return f"{(self.get('base_url') or 'http://pi.hole').rstrip('/')}/admin/api.php"

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(self._api_url(),
                                params={"status": True, "auth": self.get("api_key", "")})
                r.raise_for_status()
            status = r.json().get("status", "unknown")
            return HealthStatus.ok(f"Pi-hole erreichbar, Status: {status}")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _stats(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(self._api_url(),
                                    params={"summaryRaw": True, "auth": self.get("api_key", "")})
                    r.raise_for_status()
                d = r.json()
                blocked = d.get("ads_blocked_today", 0)
                queries = d.get("dns_queries_today", 0)
                pct = d.get("ads_percentage_today", 0.0)
                status = d.get("status", "?")
                return (
                    f"**Pi-hole Statistiken**\n"
                    f"Status: {status}\n"
                    f"Anfragen heute: {queries:,}\n"
                    f"Blockiert heute: {blocked:,} ({pct:.1f}%)"
                )
            except Exception as e:
                return f"Pi-hole-Fehler: {e}"

        async def _toggle(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            enable = args.get("enable", True)
            action = "enable" if enable else "disable"
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(self._api_url(),
                                    params={action: True, "auth": self.get("api_key", "")})
                    r.raise_for_status()
                status = r.json().get("status", "?")
                return f"Pi-hole {action}d — neuer Status: {status}"
            except Exception as e:
                return f"Pi-hole-Fehler: {e}"

        return [
            Tool(
                name="pihole_stats",
                description="Pi-hole Statistiken: blockierte Anfragen, Status.",
                parameters={"type": "object", "properties": {}},
                handler=_stats, owner_only=True, source=self.slug,
            ),
            Tool(
                name="pihole_toggle",
                description="Pi-hole DNS-Filterung aktivieren oder deaktivieren.",
                parameters={"type": "object", "properties": {
                    "enable": {"type": "boolean",
                               "description": "true = einschalten, false = ausschalten"},
                }, "required": ["enable"]},
                handler=_toggle, owner_only=True, source=self.slug,
            ),
        ]
