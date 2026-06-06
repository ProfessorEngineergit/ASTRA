"""Vikunja — tasks via the API token (open-source to-do)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class VikunjaPlugin(Plugin):
    slug = "vikunja"
    name = "Vikunja"
    description = "Aufgaben erstellen & anzeigen (Open-Source To-Do)."
    category = PluginCategory.PRODUCTIVITY
    icon = "✔️"
    config_fields = [
        ConfigField("base_url", "Vikunja-URL", required=True, help="z. B. https://vikunja.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Vikunja → Einstellungen → API-Tokens"),
        ConfigField("project_id", "Projekt-ID", type=FieldType.NUMBER, default=1,
                    help="ID des Standard-Projekts für neue Aufgaben"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Bearer {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/v1/user")
            return (HealthStatus.ok(f"Verbunden als {r.json().get('username')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _list(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Vikunja ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/v1/tasks/all", params={"filter": "done = false",
                                                            "per_page": 12})
            tasks = r.json() if isinstance(r.json(), list) else []
            if not tasks:
                return "Keine offenen Aufgaben."
            return "\n".join(f"• {t.get('title')}" for t in tasks[:12])

        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Vikunja ist deaktiviert."
            pid = int(self.get("project_id", 1))
            async with self._client() as c:
                r = await c.put(f"/api/v1/projects/{pid}/tasks", json={"title": args.get("title", "")})
            return "Aufgabe angelegt." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [
            Tool(name="vikunja_tasks", description="Offene Vikunja-Aufgaben.",
                 parameters={"type": "object", "properties": {}},
                 handler=_list, owner_only=True, source=self.slug),
            Tool(name="vikunja_add", description="Neue Vikunja-Aufgabe anlegen.",
                 parameters={"type": "object", "properties": {"title": {"type": "string"}},
                             "required": ["title"]},
                 handler=_add, owner_only=True, source=self.slug),
        ]
