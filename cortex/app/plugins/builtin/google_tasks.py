"""Google Tasks — added via the n8n `tool/google_tasks_add` workflow.

cortex owns no OAuth here: it POSTs to n8n (which holds the Google credential)
with the shared secret. Enable this plugin after importing the workflow + cred.
"""
from __future__ import annotations

import logging

import httpx

from ...config import get_settings
from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.gtasks")


class GoogleTasksPlugin(Plugin):
    slug = "google_tasks"
    name = "Google Tasks"
    description = "To-Dos anlegen (über die n8n-Workflow-Brücke)."
    category = PluginCategory.PRODUCTIVITY
    icon = "✅"
    config_fields = [
        ConfigField("list", "Task-Liste", default="@default",
                    help="@default oder eine Listen-ID", env_fallback="google_tasks_list"),
    ]

    async def add(self, title: str, notes: str = "", due: str | None = None) -> bool:
        s = get_settings()
        if s.astra_dry_run:
            log.info("[DRY_RUN] Google Task: %s", title)
            return True
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{s.n8n_base_url}/webhook/tool/google_tasks_add",
                headers={"X-Astra-Secret": s.cortex_shared_secret},
                json={"list": self.get("list", "@default"), "title": title,
                      "notes": notes, "due": due},
            )
            r.raise_for_status()
            return True

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        return HealthStatus.ok("Aktiv — Versand läuft über n8n (Workflow muss importiert sein).")

    def tools(self) -> list[Tool]:
        async def _add(args: dict, ctx: ToolContext) -> str:
            title = args.get("title", "").strip()
            if not title:
                return "Kein Titel übergeben."
            ok = await self.add(title, notes=args.get("notes", ""), due=args.get("due"))
            return f"Aufgabe '{title}' hinzugefügt." if ok else "Aufgabe konnte nicht angelegt werden."

        return [Tool(
            name="add_google_task",
            description="Lege eine Google-Task (To-Do) für Bahrian an.",
            parameters={"type": "object", "properties": {
                "title": {"type": "string"}, "notes": {"type": "string"},
                "due": {"type": "string", "description": "RFC-3339 Datum, optional"}},
                "required": ["title"]},
            handler=_add, owner_only=True, source=self.slug,
        )]
