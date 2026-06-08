"""Google Calendar — native Google OAuth, with n8n as a legacy fallback."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from ...config import get_settings
from ...google_oauth import google_api, google_oauth_fields, has_google_connection
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.google_calendar")

CAL_API = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarPlugin(Plugin):
    slug = "google_calendar"
    name = "Google Kalender"
    description = "Termine abrufen, erstellen und Konflikte pruefen, nativ per Google OAuth."
    category = PluginCategory.PRODUCTIVITY
    icon = "📅"
    google_scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/calendar",
    ]
    config_fields = [
        ConfigField("backend", "Backend", FieldType.SELECT, default="native",
                    options=["native", "n8n"],
                    help="native = ASTRA OAuth; n8n = alter Webhook-Fallback."),
        *google_oauth_fields(),
        ConfigField("calendar_id", "Kalender-ID", default="primary",
                    help="Google Kalender-ID (Standard: primary)"),
        ConfigField("n8n_url", "n8n URL", required=False,
                    default="http://n8n:5678", env_fallback="N8N_BASE_URL"),
        ConfigField("shared_secret", "Shared Secret", FieldType.PASSWORD,
                    required=False, secret=True, env_fallback="CORTEX_SHARED_SECRET"),
    ]

    def _backend(self) -> str:
        return str(self.get("backend") or "native")

    def _calendar_id(self) -> str:
        return quote(str(self.get("calendar_id") or "primary"), safe="")

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

    def _day_bounds(self) -> tuple[str, str]:
        tz = ZoneInfo(get_settings().astra_timezone)
        today = datetime.now(tz).date()
        start = datetime.combine(today, time.min, tzinfo=tz).isoformat()
        end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).isoformat()
        return start, end

    async def events(self, start: str, end: str, *, max_results: int = 20) -> list[dict]:
        if self._backend() == "n8n":
            data = await self._webhook("calendar_conflicts", {
                "calendar_id": self.get("calendar_id", "primary"), "start": start, "end": end,
            })
            return data if isinstance(data, list) else data.get("conflicts", [])
        r = await google_api(
            self,
            "GET",
            f"{CAL_API}/calendars/{self._calendar_id()}/events",
            params={
                "timeMin": start,
                "timeMax": end,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max(1, min(max_results, 100)),
            },
        )
        return r.json().get("items", [])

    async def today(self) -> list[dict]:
        if self._backend() == "n8n":
            data = await self._webhook("calendar_today",
                                       {"calendar_id": self.get("calendar_id", "primary")})
            return data if isinstance(data, list) else data.get("events", [])
        start, end = self._day_bounds()
        return await self.events(start, end, max_results=20)

    async def add_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        if self._backend() == "n8n":
            return await self._webhook("calendar_add_event", {
                "calendar_id": self.get("calendar_id", "primary"),
                "title": title, "start": start, "end": end, "description": description,
            })
        body = {
            "summary": title,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if description:
            body["description"] = description
        r = await google_api(
            self,
            "POST",
            f"{CAL_API}/calendars/{self._calendar_id()}/events",
            json=body,
        )
        return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        if self._backend() == "n8n":
            try:
                async with httpx.AsyncClient(timeout=8) as c:
                    r = await c.get(self._base())
                return HealthStatus.ok(f"n8n erreichbar ({self._base()}, HTTP {r.status_code}).")
            except Exception as e:  # noqa: BLE001
                return HealthStatus.error(str(e))
        if not has_google_connection(self.cfg):
            return HealthStatus.not_configured("Google OAuth noch nicht verbunden.")
        try:
            events = await self.today()
            who = self.get("account_email") or "Google"
            return HealthStatus.ok(f"{who} verbunden; {len(events)} Termine heute.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"Google Calendar API: {e}")

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            events = await self.today()
            if not events:
                return "📅 Kalender: Keine Termine heute."
            titles = [e.get("summary", "?") for e in events[:3]]
            return "📅 Heute: " + " · ".join(titles)
        except Exception as e:  # noqa: BLE001
            log.warning("Calendar briefing failed: %s", e)
            return None

    @staticmethod
    def _event_line(e: dict) -> str:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date", "")
        return f"- {start[:16].replace('T', ' ')}: {e.get('summary', '?')}"

    def tools(self) -> list[Tool]:
        async def _today(args: dict, ctx: ToolContext) -> str:
            events = await self.today()
            if not events:
                return tool_result(ok=True, summary="Keine Termine heute.", data=[], source=self.slug)
            return tool_result(
                ok=True,
                summary="Heutige Termine:\n" + "\n".join(self._event_line(e) for e in events),
                data=events,
                source=self.slug,
            )

        async def _add_event(args: dict, ctx: ToolContext) -> str:
            title = args.get("title", "").strip()
            start = args.get("start", "")
            end = args.get("end", "")
            if not title or not start or not end:
                return tool_result(ok=False, summary="title, start und end sind erforderlich.", source=self.slug)
            data = await self.add_event(title, start, end, args.get("description", ""))
            link = data.get("htmlLink", "") if isinstance(data, dict) else ""
            return tool_result(
                ok=True,
                summary=f"Termin '{title}' angelegt.{(' ' + link) if link else ''}",
                data=data,
                source=self.slug,
            )

        async def _conflicts(args: dict, ctx: ToolContext) -> str:
            start = args.get("start", "")
            end = args.get("end", "")
            if not start or not end:
                return tool_result(ok=False, summary="start und end sind erforderlich.", source=self.slug)
            conflicts = await self.events(start, end)
            if not conflicts:
                return tool_result(ok=True, summary="Keine Konflikte in diesem Zeitraum.", data=[], source=self.slug)
            return tool_result(
                ok=True,
                summary="Konflikte:\n" + "\n".join(self._event_line(e) for e in conflicts),
                data=conflicts,
                source=self.slug,
            )

        return [
            Tool(
                name="calendar_today",
                description="Heutige Google-Kalender-Termine anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_today, owner_only=True, source=self.slug,
                safety="private_read", intents=["today", "list", "status"],
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
                safety="mutation", intents=["create"],
            ),
            Tool(
                name="calendar_conflicts",
                description="Pruefen, ob im angegebenen Zeitraum Terminkonflikte bestehen.",
                parameters={"type": "object", "properties": {
                    "start": {"type": "string", "description": "ISO-8601 Datetime"},
                    "end": {"type": "string", "description": "ISO-8601 Datetime"},
                }, "required": ["start", "end"]},
                handler=_conflicts, owner_only=True, source=self.slug,
                safety="private_read", intents=["search", "status"],
            ),
        ]
