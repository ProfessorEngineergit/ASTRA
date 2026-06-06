"""Gitea — issues + repo overview via the REST API (self-hosted git)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class GiteaPlugin(Plugin):
    slug = "gitea"
    name = "Gitea"
    description = "Issues & Repos auf deiner Gitea-Instanz (selbst gehostetes Git)."
    category = PluginCategory.PRODUCTIVITY
    icon = "🍵"
    config_fields = [
        ConfigField("base_url", "Gitea-URL", required=True, help="z. B. https://git.example.com"),
        ConfigField("token", "Access-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Gitea → Einstellungen → Anwendungen → Token generieren"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"token {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/v1/user")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('login')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _issues(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Gitea ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/v1/repos/issues/search",
                                params={"state": "open", "limit": 10})
            items = r.json()
            if not items:
                return "Keine offenen Issues."
            return "\n".join(f"• {i['repository']['name']}#{i['number']}: {i['title']}"
                             for i in items[:10])

        return [Tool(
            name="gitea_issues",
            description="Liste offene Gitea-Issues über alle Repos.",
            parameters={"type": "object", "properties": {}},
            handler=_issues, owner_only=True, source=self.slug,
        )]
