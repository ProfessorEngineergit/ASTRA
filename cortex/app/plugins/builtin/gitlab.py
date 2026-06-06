"""GitLab — assigned issues via the REST API (gitlab.com or self-hosted)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class GitLabPlugin(Plugin):
    slug = "gitlab"
    name = "GitLab"
    description = "Dir zugewiesene GitLab-Issues anzeigen."
    category = PluginCategory.PRODUCTIVITY
    icon = "🦊"
    config_fields = [
        ConfigField("base_url", "GitLab-URL", required=True, default="https://gitlab.com"),
        ConfigField("token", "Personal Access Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="GitLab → Preferences → Access Tokens (Scope: read_api)"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"PRIVATE-TOKEN": self.get("token")})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/v4/user")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('username')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _issues(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "GitLab ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/v4/issues",
                                params={"scope": "assigned_to_me", "state": "opened", "per_page": 10})
            items = r.json()
            if not items:
                return "Keine zugewiesenen offenen Issues."
            return "\n".join(f"• {i['references']['full']}: {i['title']}" for i in items[:10])

        return [Tool(
            name="gitlab_my_issues",
            description="Dir zugewiesene offene GitLab-Issues.",
            parameters={"type": "object", "properties": {}},
            handler=_issues, owner_only=True, source=self.slug,
        )]
