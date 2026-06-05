"""GitHub — issues, pull requests, and issue creation."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.github")

_API = "https://api.github.com"


class GitHubPlugin(Plugin):
    slug = "github"
    name = "GitHub"
    description = "Issues und Pull Requests lesen, Issues erstellen."
    category = PluginCategory.PRODUCTIVITY
    icon = "🐙"
    config_fields = [
        ConfigField("token", "Personal Access Token", FieldType.PASSWORD,
                    required=True, secret=True, env_fallback="GITHUB_TOKEN"),
        ConfigField("username", "GitHub-Nutzername", required=True,
                    env_fallback="GITHUB_USERNAME"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {self.get('token', '')}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/user")
                r.raise_for_status()
            login = r.json().get("login", "?")
            return HealthStatus.ok(f"Verbunden als {login}.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        username = self.get("username", "")

        async def _issues(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            repo = args.get("repo", "")
            if not repo:
                return "repo ist erforderlich."
            owner = args.get("owner") or self.get("username", "")
            try:
                async with self._client() as c:
                    r = await c.get(f"/repos/{owner}/{repo}/issues",
                                    params={"state": "open", "per_page": 20})
                    r.raise_for_status()
                items = r.json()
                if not items:
                    return f"Keine offenen Issues in {owner}/{repo}."
                lines = [f"**Offene Issues in {owner}/{repo}**"]
                for issue in items:
                    if "pull_request" in issue:
                        continue
                    lines.append(f"#{issue['number']} {issue['title']} ({issue['user']['login']})")
                return "\n".join(lines)
            except Exception as e:
                return f"GitHub-Fehler: {e}"

        async def _prs(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            repo = args.get("repo", "")
            if not repo:
                return "repo ist erforderlich."
            owner = args.get("owner") or self.get("username", "")
            try:
                async with self._client() as c:
                    r = await c.get(f"/repos/{owner}/{repo}/pulls",
                                    params={"state": "open", "per_page": 20})
                    r.raise_for_status()
                items = r.json()
                if not items:
                    return f"Keine offenen PRs in {owner}/{repo}."
                lines = [f"**Offene Pull Requests in {owner}/{repo}**"]
                for pr in items:
                    lines.append(f"#{pr['number']} {pr['title']} — {pr['user']['login']}")
                return "\n".join(lines)
            except Exception as e:
                return f"GitHub-Fehler: {e}"

        async def _create_issue(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            repo = args.get("repo", "")
            title = args.get("title", "").strip()
            body = args.get("body", "")
            if not repo or not title:
                return "repo und title sind erforderlich."
            owner = args.get("owner") or self.get("username", "")
            try:
                async with self._client() as c:
                    r = await c.post(f"/repos/{owner}/{repo}/issues",
                                     json={"title": title, "body": body})
                    r.raise_for_status()
                url = r.json().get("html_url", "")
                return f"Issue erstellt: {url}"
            except Exception as e:
                return f"GitHub-Fehler: {e}"

        return [
            Tool(
                name="github_issues",
                description="Offene Issues eines GitHub-Repos anzeigen.",
                parameters={"type": "object", "properties": {
                    "repo": {"type": "string", "description": "Repository-Name"},
                    "owner": {"type": "string",
                              "description": "Owner (leer = konfigurierter Nutzername)"},
                }, "required": ["repo"]},
                handler=_issues, owner_only=True, source=self.slug,
            ),
            Tool(
                name="github_prs",
                description="Offene Pull Requests eines GitHub-Repos anzeigen.",
                parameters={"type": "object", "properties": {
                    "repo": {"type": "string", "description": "Repository-Name"},
                    "owner": {"type": "string",
                              "description": "Owner (leer = konfigurierter Nutzername)"},
                }, "required": ["repo"]},
                handler=_prs, owner_only=True, source=self.slug,
            ),
            Tool(
                name="github_create_issue",
                description="Neues Issue in einem GitHub-Repo erstellen.",
                parameters={"type": "object", "properties": {
                    "repo": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "owner": {"type": "string"},
                }, "required": ["repo", "title"]},
                handler=_create_issue, owner_only=True, source=self.slug,
            ),
        ]
