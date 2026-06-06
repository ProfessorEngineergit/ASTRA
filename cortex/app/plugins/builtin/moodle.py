"""Moodle — upcoming assignments + courses via the web-service REST API."""
from __future__ import annotations

import time

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class MoodlePlugin(Plugin):
    slug = "moodle"
    name = "Moodle"
    description = "Anstehende Aufgaben, Kurse & Deadlines aus Moodle."
    category = PluginCategory.SCHOOL
    icon = "🎓"
    config_fields = [
        ConfigField("base_url", "Moodle-URL", required=True, help="z. B. https://moodle.schule.de"),
        ConfigField("token", "Web-Service-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Profil → Sicherheitsschlüssel → Token für 'Moodle mobile web service'"),
    ]

    async def _call(self, fn: str, **params) -> dict | list:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.get('base_url').rstrip('/')}/webservice/rest/server.php",
                            params={"wstoken": self.get("token"), "wsfunction": fn,
                                    "moodlewsrestformat": "json", **params})
            return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            d = await self._call("core_webservice_get_site_info")
            if isinstance(d, dict) and d.get("sitename"):
                return HealthStatus.ok(f"Verbunden: {d['sitename']} ({d.get('fullname')}).")
            return HealthStatus.error(str(d.get("message", d))[:90])
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def _upcoming(self) -> str:
        now = int(time.time())
        d = await self._call("core_calendar_get_action_events_by_timesort",
                             timesortfrom=now, limitnum=10)
        events = d.get("events", []) if isinstance(d, dict) else []
        if not events:
            return "Keine anstehenden Moodle-Aufgaben."
        lines = []
        for e in events[:10]:
            when = time.strftime("%d.%m %H:%M", time.localtime(e.get("timesort", now)))
            lines.append(f"• {when} — {e.get('name')} ({e.get('course', {}).get('shortname', '')})")
        return "🎓 Anstehend:\n" + "\n".join(lines)

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            return await self._upcoming()
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _up(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Moodle ist deaktiviert."
            return await self._upcoming()

        return [Tool(
            name="moodle_upcoming",
            description="Liste anstehende Moodle-Aufgaben und Deadlines.",
            parameters={"type": "object", "properties": {}},
            handler=_up, owner_only=True, source=self.slug,
        )]
