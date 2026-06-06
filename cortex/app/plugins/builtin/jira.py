"""Jira — assigned open issues via the REST API (email + API token)."""
from __future__ import annotations

import base64

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class JiraPlugin(Plugin):
    slug = "jira"
    name = "Jira"
    description = "Dir zugewiesene offene Jira-Vorgänge anzeigen."
    category = PluginCategory.PRODUCTIVITY
    icon = "📋"
    config_fields = [
        ConfigField("base_url", "Jira-URL", required=True, help="z. B. https://deinteam.atlassian.net"),
        ConfigField("email", "E-Mail", required=True),
        ConfigField("api_token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="id.atlassian.com → Sicherheit → API-Token erstellen"),
    ]

    def _client(self) -> httpx.AsyncClient:
        cred = base64.b64encode(f"{self.get('email')}:{self.get('api_token')}".encode()).decode()
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Basic {cred}", "Accept": "application/json"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/rest/api/3/myself")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('displayName')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _issues(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Jira ist deaktiviert."
            jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
            async with self._client() as c:
                r = await c.get("/rest/api/3/search", params={"jql": jql, "maxResults": 10,
                                                              "fields": "summary,status"})
            issues = r.json().get("issues", [])
            if not issues:
                return "Keine offenen Vorgänge."
            return "\n".join(f"• {i['key']}: {i['fields']['summary']} "
                             f"[{i['fields']['status']['name']}]" for i in issues)

        return [Tool(
            name="jira_my_issues", description="Dir zugewiesene offene Jira-Vorgänge.",
            parameters={"type": "object", "properties": {}},
            handler=_issues, owner_only=True, source=self.slug,
        )]
