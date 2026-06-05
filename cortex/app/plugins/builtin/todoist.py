"""Todoist — task management via the REST API v2."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.todoist")

_API = "https://api.todoist.com/rest/v2"


class TodoistPlugin(Plugin):
    slug = "todoist"
    name = "Todoist"
    description = "Aufgaben in Todoist lesen, hinzufügen und abschließen."
    category = PluginCategory.PRODUCTIVITY
    icon = "🎯"
    config_fields = [
        ConfigField("api_token", "API-Token", FieldType.PASSWORD,
                    required=True, secret=True, env_fallback="TODOIST_TOKEN"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API,
            headers={"Authorization": f"Bearer {self.get('api_token', '')}"},
            timeout=15,
        )

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/projects")
                r.raise_for_status()
            count = len(r.json())
            return HealthStatus.ok(f"Verbunden — {count} Projekte.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _today(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with self._client() as c:
                    r = await c.get("/tasks", params={"filter": "today"})
                    r.raise_for_status()
                tasks = r.json()
                if not tasks:
                    return "Keine Aufgaben für heute."
                lines = ["**Heutige Aufgaben**"]
                for t in tasks:
                    priority = "❗" * (5 - t.get("priority", 1)) if t.get("priority", 1) > 1 else ""
                    lines.append(f"[{t['id']}] {priority}{t['content']}")
                return "\n".join(lines)
            except Exception as e:
                return f"Todoist-Fehler: {e}"

        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            content = args.get("content", "").strip()
            if not content:
                return "content ist erforderlich."
            payload: dict = {"content": content}
            if args.get("due_string"):
                payload["due_string"] = args["due_string"]
            try:
                async with self._client() as c:
                    r = await c.post("/tasks", json=payload)
                    r.raise_for_status()
                return f"Aufgabe '{content}' angelegt (ID {r.json()['id']})."
            except Exception as e:
                return f"Todoist-Fehler: {e}"

        async def _complete(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            task_id = args.get("task_id", "")
            if not task_id:
                return "task_id ist erforderlich."
            try:
                async with self._client() as c:
                    r = await c.post(f"/tasks/{task_id}/close")
                    r.raise_for_status()
                return f"Aufgabe {task_id} als erledigt markiert."
            except Exception as e:
                return f"Todoist-Fehler: {e}"

        return [
            Tool(
                name="todoist_today",
                description="Heutige Todoist-Aufgaben anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_today, owner_only=True, source=self.slug,
            ),
            Tool(
                name="todoist_add",
                description="Neue Aufgabe in Todoist anlegen.",
                parameters={"type": "object", "properties": {
                    "content": {"type": "string", "description": "Aufgabentext"},
                    "due_string": {"type": "string",
                                   "description": "Fälligkeitsdatum (z.B. 'morgen', 'übermorgen 14:00')"},
                }, "required": ["content"]},
                handler=_add, owner_only=True, source=self.slug,
            ),
            Tool(
                name="todoist_complete",
                description="Todoist-Aufgabe als erledigt markieren.",
                parameters={"type": "object", "properties": {
                    "task_id": {"type": "string", "description": "Task-ID aus todoist_today"},
                }, "required": ["task_id"]},
                handler=_complete, owner_only=True, source=self.slug,
            ),
        ]
