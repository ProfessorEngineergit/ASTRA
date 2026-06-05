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
        ConfigField("n8n_url", "n8n URL", required=True, default="http://n8n:5678",
                    env_fallback="N8N_BASE_URL",
                    help="Basis-URL deines n8n (Standard: http://n8n:5678 im Docker-Netz)"),
        ConfigField("shared_secret", "ASTRA Shared Secret", required=True, secret=True,
                    env_fallback="CORTEX_SHARED_SECRET",
                    help="CORTEX_SHARED_SECRET aus der .env — authentifiziert cortex → n8n"),
        ConfigField("list", "Task-Liste", default="@default",
                    help="@default oder eine Listen-ID aus Google Tasks",
                    env_fallback="google_tasks_list"),
    ]

    def _n8n_url(self) -> str:
        return (self.get("n8n_url") or get_settings().n8n_base_url).rstrip("/")

    def _secret(self) -> str:
        return self.get("shared_secret") or get_settings().cortex_shared_secret

    async def add(self, title: str, notes: str = "", due: str | None = None) -> bool:
        if get_settings().astra_dry_run:
            log.info("[DRY_RUN] Google Task: %s", title)
            return True
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self._n8n_url()}/webhook/tool/google_tasks_add",
                headers={"X-Astra-Secret": self._secret()},
                json={"list": self.get("list", "@default"), "title": title,
                      "notes": notes, "due": due},
            )
            r.raise_for_status()
            return True

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(self._n8n_url(), timeout=5)
            return HealthStatus.ok(f"n8n erreichbar ({r.status_code}). Workflow muss importiert sein.")
        except Exception as e:
            return HealthStatus.error(f"n8n nicht erreichbar: {e}")

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
