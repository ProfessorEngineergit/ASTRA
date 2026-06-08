"""Google Tasks — native Google OAuth, with n8n as a legacy fallback."""
from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from ...config import get_settings
from ...google_oauth import google_api, google_oauth_fields, has_google_connection
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.gtasks")

TASKS_API = "https://tasks.googleapis.com/tasks/v1"


class GoogleTasksPlugin(Plugin):
    slug = "google_tasks"
    name = "Google Tasks"
    description = "To-Dos lesen und anlegen, nativ per Google OAuth oder optional ueber n8n."
    category = PluginCategory.PRODUCTIVITY
    icon = "✅"
    google_scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/tasks",
    ]
    config_fields = [
        ConfigField("backend", "Backend", FieldType.SELECT, default="native",
                    options=["native", "n8n"],
                    help="native = ASTRA OAuth; n8n = alter Webhook-Fallback."),
        *google_oauth_fields(),
        ConfigField("list", "Task-Liste", default="@default",
                    help="@default oder eine Listen-ID aus Google Tasks",
                    env_fallback="google_tasks_list"),
        ConfigField("n8n_url", "n8n URL", required=False, default="http://n8n:5678",
                    env_fallback="N8N_BASE_URL"),
        ConfigField("shared_secret", "ASTRA Shared Secret", FieldType.PASSWORD,
                    required=False, secret=True, env_fallback="CORTEX_SHARED_SECRET"),
    ]

    def _backend(self) -> str:
        return str(self.get("backend") or "native")

    def _list_id(self) -> str:
        return quote(str(self.get("list") or "@default"), safe="")

    def _n8n_url(self) -> str:
        return (self.get("n8n_url") or get_settings().n8n_base_url).rstrip("/")

    def _secret(self) -> str:
        return self.get("shared_secret") or get_settings().cortex_shared_secret

    async def _n8n_add(self, title: str, notes: str = "", due: str | None = None) -> bool:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self._n8n_url()}/webhook/tool/google_tasks_add",
                headers={"X-Astra-Secret": self._secret()},
                json={"list": self.get("list", "@default"), "title": title,
                      "notes": notes, "due": due},
            )
            r.raise_for_status()
            return True

    async def add(self, title: str, notes: str = "", due: str | None = None) -> dict:
        if get_settings().astra_dry_run:
            log.info("[DRY_RUN] Google Task: %s", title)
            return {"title": title, "dry_run": True}
        if self._backend() == "n8n":
            await self._n8n_add(title, notes=notes, due=due)
            return {"title": title, "backend": "n8n"}
        body = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = due
        r = await google_api(self, "POST", f"{TASKS_API}/lists/{self._list_id()}/tasks", json=body)
        return r.json()

    async def list_lists(self) -> list[dict]:
        r = await google_api(self, "GET", f"{TASKS_API}/users/@me/lists")
        return r.json().get("items", [])

    async def open_tasks(self, limit: int = 12) -> list[dict]:
        r = await google_api(
            self,
            "GET",
            f"{TASKS_API}/lists/{self._list_id()}/tasks",
            params={"showCompleted": "false", "maxResults": max(1, min(limit, 100))},
        )
        return r.json().get("items", [])

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        if self._backend() == "n8n":
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(self._n8n_url(), timeout=5)
                return HealthStatus.ok(f"n8n erreichbar ({r.status_code}).")
            except Exception as e:  # noqa: BLE001
                return HealthStatus.error(f"n8n nicht erreichbar: {e}")
        if not has_google_connection(self.cfg):
            return HealthStatus.not_configured("Google OAuth noch nicht verbunden.")
        try:
            lists = await self.list_lists()
            who = self.get("account_email") or "Google"
            return HealthStatus.ok(f"{who} verbunden; {len(lists)} Task-Listen gefunden.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"Google Tasks API: {e}")

    def tools(self) -> list[Tool]:
        async def _add(args: dict, ctx: ToolContext) -> str:
            title = args.get("title", "").strip()
            if not title:
                return tool_result(ok=False, summary="Kein Titel uebergeben.", source=self.slug)
            data = await self.add(title, notes=args.get("notes", ""), due=args.get("due"))
            return tool_result(
                ok=True,
                summary=f"Aufgabe '{title}' hinzugefuegt.",
                data=data,
                source=self.slug,
            )

        async def _list(args: dict, ctx: ToolContext) -> str:
            items = await self.open_tasks(int(args.get("limit") or 12))
            if not items:
                return tool_result(ok=True, summary="Keine offenen Google Tasks.", data=[], source=self.slug)
            lines = [f"- {t.get('title', '?')}" for t in items]
            return tool_result(
                ok=True,
                summary="Offene Google Tasks:\n" + "\n".join(lines),
                data=items,
                source=self.slug,
            )

        async def _lists(args: dict, ctx: ToolContext) -> str:
            lists = await self.list_lists()
            lines = [f"- {x.get('title')} ({x.get('id')})" for x in lists]
            return tool_result(
                ok=True,
                summary="Google Task-Listen:\n" + ("\n".join(lines) if lines else "Keine Listen."),
                data=lists,
                source=self.slug,
            )

        return [
            Tool(
                name="add_google_task",
                description="Lege eine Google-Task fuer Bahrian an.",
                parameters={"type": "object", "properties": {
                    "title": {"type": "string"}, "notes": {"type": "string"},
                    "due": {"type": "string", "description": "RFC-3339 Datum, optional"}},
                    "required": ["title"]},
                handler=_add, owner_only=True, source=self.slug,
                safety="mutation", intents=["create"],
            ),
            Tool(
                name="google_tasks_open",
                description="Liste offene Google Tasks.",
                parameters={"type": "object", "properties": {"limit": {"type": "number"}}},
                handler=_list, owner_only=True, source=self.slug,
                safety="private_read", intents=["list", "status"],
            ),
            Tool(
                name="google_task_lists",
                description="Liste Google-Tasklisten mit IDs.",
                parameters={"type": "object", "properties": {}},
                handler=_lists, owner_only=True, source=self.slug,
                safety="private_read", intents=["list", "status"],
            ),
        ]
