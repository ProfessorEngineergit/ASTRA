"""Linear — create issues via the GraphQL API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://api.linear.app/graphql"


class LinearPlugin(Plugin):
    slug = "linear"
    name = "Linear"
    description = "Issues in Linear erstellen und anzeigen."
    category = PluginCategory.PRODUCTIVITY
    icon = "📋"
    config_fields = [
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Linear → Settings → API → Personal API key (lin_api_…)"),
        ConfigField("team_id", "Team-ID", required=True,
                    help="Settings → API → oder via teams-Query; UUID des Teams"),
    ]

    async def _gql(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(API, headers={"Authorization": self.get("api_key"),
                                           "Content-Type": "application/json"},
                             json={"query": query, "variables": variables})
            return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            d = await self._gql("{ viewer { name email } }", {})
            v = d.get("data", {}).get("viewer")
            return (HealthStatus.ok(f"Verbunden als {v['name']}.") if v
                    else HealthStatus.error(str(d.get("errors", d))[:90]))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _create(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Linear ist deaktiviert."
            q = ("mutation($t:String!,$d:String,$team:String!){issueCreate(input:"
                 "{title:$t,description:$d,teamId:$team}){success issue{identifier url}}}")
            d = await self._gql(q, {"t": args.get("title", ""), "d": args.get("description", ""),
                                    "team": self.get("team_id")})
            issue = d.get("data", {}).get("issueCreate", {}).get("issue")
            return f"Issue {issue['identifier']} angelegt: {issue['url']}" if issue \
                else f"Fehler: {d.get('errors', d)}"

        return [Tool(
            name="linear_create_issue",
            description="Erstelle ein Issue in Linear.",
            parameters={"type": "object", "properties": {
                "title": {"type": "string"}, "description": {"type": "string"}},
                "required": ["title"]},
            handler=_create, owner_only=True, source=self.slug,
        )]
