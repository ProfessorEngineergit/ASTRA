"""Google Calendar — via n8n webhook bridge."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.google_calendar")


class GoogleCalendarPlugin(Plugin):
    slug = "google_calendar"
    name = "Google Kalender"
    description = "Termine abrufen, erstellen und Konflikte prüfen (über n8n-Bridge)."
    category = PluginCategory.PRODUCTIVITY
    icon = "📅"
    config_fields = [
        ConfigField("n8n_url", "n8n URL", required=True,
                    default="http://n8n:5678", env_fallback="N8N_BASE_URL"),
        ConfigField("shared_secret", "Shared Secret", FieldType.PASSWORD,
                    required=True, secret=True, env_fallback="CORTEX_SHARED_SECRET"),
        ConfigField("calendar_id", "Kalender-ID", default="primary",
                    help="Google Kalender-ID (Standard: primary)"),
    ]

    def _base(self) -> str:
        return (self.get("n8n_url") or "http://n8n:5678").rstrip("/")

    def _headers(self) -> dict:
        return {"X-Astra-Secret": self.get("shared_secret", "")}

    async def _webhook(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self._base()}/webhook/tool/{path}",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(self._base())
            if r.status_code < 500:
                return HealthStatus.ok(f"n8n erreichbar ({self._base()}).")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            data = await self._webhook("calendar_today",
                                       {"calendar_id": self.get("calendar_id", "primary")})
            events = data if isinstance(data, list) else data.get("events", [])
            if not events:
                return "📅 Kalender: Keine Termine heute."
            titles = [e.get("summary", "?") for e in events[:3]]
            return "📅 Heute: " + " · ".join(titles)
        except Exception as e:
            log.warning("Calendar briefing failed: %s", e)
            return None

    def tools(self) -> list[Tool]:
        async def _today(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                data = await self._webhook("calendar_today",
                                           {"calendar_id": self.get("calendar_id", "primary")})
                events = data if isinstance(data, list) else data.get("events", [])
                if not events:
                    return "Keine Termine heute."
                lines = ["**Heutige Termine**"]
                for e in events:
                    start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
                    lines.append(f"- {start[:16].replace('T', ' ')}: {e.get('summary', '?')}")
                return "\n".join(lines)
            except Exception as e:
                return f"Kalender-Fehler: {e}"

        async def _add_event(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            title = args.get("title", "").strip()
            start = args.get("start", "")
            end = args.get("end", "")
            if not title or not start or not end:
                return "title, start und end sind erforderlich."
            try:
                data = await self._webhook("calendar_add_event", {
                    "calendar_id": self.get("calendar_id", "primary"),
                    "title": title,
                    "start": start,
                    "end": end,
                    "description": args.get("description", ""),
                })
                link = data.get("htmlLink", "") if isinstance(data, dict) else ""
                return f"Termin '{title}' angelegt.{(' ' + link) if link else ''}"
            except Exception as e:
                return f"Kalender-Fehler: {e}"

        async def _conflicts(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            start = args.get("start", "")
            end = args.get("end", "")
            if not start or not end:
                return "start und end sind erforderlich."
            try:
                data = await self._webhook("calendar_conflicts", {
                    "calendar_id": self.get("calendar_id", "primary"),
                    "start": start,
                    "end": end,
                })
                conflicts = data if isinstance(data, list) else data.get("conflicts", [])
                if not conflicts:
                    return "Keine Konflikte in diesem Zeitraum."
                lines = ["**Konflikte:**"]
                for e in conflicts:
                    lines.append(f"- {e.get('summary', '?')}")
                return "\n".join(lines)
            except Exception as e:
                return f"Kalender-Fehler: {e}"

        return [
            Tool(
                name="calendar_today",
                description="Heutige Google-Kalender-Termine anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_today, owner_only=True, source=self.slug,
            ),
            Tool(
                name="calendar_add_event",
                description="Neuen Termin im Google Kalender anlegen.",
                parameters={"type": "object", "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO-8601 Datetime"},
                    "end": {"type": "string", "description": "ISO-8601 Datetime"},
                    "description": {"type": "string"},
                }, "required": ["title", "start", "end"]},
                handler=_add_event, owner_only=True, source=self.slug,
            ),
            Tool(
                name="calendar_conflicts",
                description="Prüfen, ob im angegebenen Zeitraum Terminkonflikte bestehen.",
                parameters={"type": "object", "properties": {
                    "start": {"type": "string", "description": "ISO-8601 Datetime"},
                    "end": {"type": "string", "description": "ISO-8601 Datetime"},
                }, "required": ["start", "end"]},
                handler=_conflicts, owner_only=True, source=self.slug,
            ),
        ]
