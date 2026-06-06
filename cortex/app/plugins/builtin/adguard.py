"""AdGuard Home — DNS stats + protection toggle."""
from __future__ import annotations

import base64

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class AdGuardPlugin(Plugin):
    slug = "adguard"
    name = "AdGuard Home"
    description = "DNS-Statistiken & Werbeschutz an/aus (AdGuard Home)."
    category = PluginCategory.INFRA_AI
    icon = "🛡️"
    config_fields = [
        ConfigField("base_url", "AdGuard-URL", required=True, default="http://192.168.178.189:3000"),
        ConfigField("username", "Benutzer", required=True),
        ConfigField("password", "Passwort", required=True, secret=True),
    ]

    def _client(self) -> httpx.AsyncClient:
        cred = base64.b64encode(f"{self.get('username')}:{self.get('password')}".encode()).decode()
        return httpx.AsyncClient(timeout=10, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Basic {cred}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/control/status")
            if r.status_code == 200:
                d = r.json()
                return HealthStatus.ok(f"AdGuard {d.get('version', '')} — "
                                       f"Schutz {'an' if d.get('protection_enabled') else 'aus'}.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _stats(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "AdGuard ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/control/stats")
            d = r.json()
            return (f"🛡️ Heute: {d.get('num_dns_queries', 0)} Anfragen, "
                    f"{d.get('num_blocked_filtering', 0)} blockiert "
                    f"({d.get('num_blocked_filtering', 0) * 100 // max(d.get('num_dns_queries', 1), 1)}%).")

        async def _toggle(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "AdGuard ist deaktiviert."
            async with self._client() as c:
                r = await c.post("/control/protection",
                                 json={"enabled": bool(args.get("enable", True))})
            return ("Schutz aktiviert." if args.get("enable") else "Schutz pausiert.") \
                if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [
            Tool(name="adguard_stats", description="AdGuard-DNS-Statistiken (Anfragen/blockiert).",
                 parameters={"type": "object", "properties": {}},
                 handler=_stats, owner_only=True, source=self.slug),
            Tool(name="adguard_protection", description="AdGuard-Werbeschutz an/aus schalten.",
                 parameters={"type": "object", "properties": {"enable": {"type": "boolean"}},
                             "required": ["enable"]},
                 handler=_toggle, owner_only=True, source=self.slug),
        ]
