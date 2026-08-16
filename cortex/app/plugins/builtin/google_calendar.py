"""Google Calendar — native Google OAuth, with n8n as a legacy fallback."""
from __future__ import annotations

import asyncio
import logging
from datetime import date as date_cls
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
SCHOOL_BASELINE_MARKER = "ASTRA_SCHOOL_BASELINE"


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

    async def events(
        self,
        start: str,
        end: str,
        *,
        max_results: int = 20,
        query: str = "",
    ) -> list[dict]:
        if self._backend() == "n8n":
            data = await self._webhook("calendar_conflicts", {
                "calendar_id": self.get("calendar_id", "primary"), "start": start, "end": end,
            })
            return data if isinstance(data, list) else data.get("conflicts", [])
        params = {
            "timeMin": start,
            "timeMax": end,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 250)),
        }
        if query.strip():
            params["q"] = query.strip()
        r = await google_api(
            self,
            "GET",
            f"{CAL_API}/calendars/{self._calendar_id()}/events",
            params=params,
        )
        return r.json().get("items", [])

    async def today(self) -> list[dict]:
        if self._backend() == "n8n":
            data = await self._webhook("calendar_today",
                                       {"calendar_id": self.get("calendar_id", "primary")})
            return data if isinstance(data, list) else data.get("events", [])
        start, end = self._day_bounds()
        return await self.events(start, end, max_results=20)

    async def add_event(
        self,
        title: str,
        start: str,
        end: str,
        description: str = "",
        *,
        location: str = "",
    ) -> dict:
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
        if location:
            body["location"] = location
        r = await google_api(
            self,
            "POST",
            f"{CAL_API}/calendars/{self._calendar_id()}/events",
            json=body,
        )
        return r.json()

    async def update_event(self, event_id: str, changes: dict) -> dict:
        body: dict = {}
        aliases = {"title": "summary", "description": "description", "location": "location"}
        for source, target in aliases.items():
            if source in changes:
                body[target] = str(changes[source] or "")
        if changes.get("start"):
            body["start"] = {"dateTime": str(changes["start"])}
        if changes.get("end"):
            body["end"] = {"dateTime": str(changes["end"])}
        if not body:
            raise ValueError("Keine Aenderung angegeben.")
        r = await google_api(
            self,
            "PATCH",
            f"{CAL_API}/calendars/{self._calendar_id()}/events/{quote(event_id, safe='')}",
            json=body,
        )
        return r.json()

    async def delete_event(self, event_id: str) -> dict:
        await google_api(
            self,
            "DELETE",
            f"{CAL_API}/calendars/{self._calendar_id()}/events/{quote(event_id, safe='')}",
        )
        return {"id": event_id, "deleted": True}

    @staticmethod
    def _parse_datetime(value: str, tz: ZoneInfo) -> datetime:
        raw = str(value or "").strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    @classmethod
    def _event_interval(cls, event: dict, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
        start_data, end_data = event.get("start") or {}, event.get("end") or {}
        start_raw, end_raw = start_data.get("dateTime"), end_data.get("dateTime")
        if start_raw and end_raw:
            return cls._parse_datetime(start_raw, tz), cls._parse_datetime(end_raw, tz)
        if start_data.get("date") and end_data.get("date"):
            start_day = date_cls.fromisoformat(start_data["date"])
            end_day = date_cls.fromisoformat(end_data["date"])
            return (
                datetime.combine(start_day, time.min, tzinfo=tz),
                datetime.combine(end_day, time.min, tzinfo=tz),
            )
        return None

    async def _live_school_intervals(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[set[date_cls], list[dict]]:
        """Return EduPage-backed school intervals for the requested days.

        A day is only included in ``live_days`` after a successful API response.
        Callers may then replace imported baseline events for exactly that day.
        """
        try:
            from ..registry import get_manager

            plugin = get_manager().get("edupage")
        except Exception:  # noqa: BLE001
            return set(), []
        if plugin is None or not plugin.enabled or not hasattr(plugin, "timetable_result"):
            return set(), []
        live_days: set[date_cls] = set()
        intervals: list[dict] = []
        day = start.date()
        last_day = min(end.date(), day + timedelta(days=14))
        group = plugin._default_group() if hasattr(plugin, "_default_group") else "B"
        while day <= last_day:
            result = await plugin.timetable_result(day)
            if result.get("ok"):
                live_days.add(day)
                lessons = result.get("lessons") or []
                if hasattr(plugin, "_filter_lessons"):
                    lessons = plugin._filter_lessons(lessons, group)
                for lesson in lessons:
                    if lesson.get("cancelled"):
                        continue
                    start_t = plugin._parse_hhmm(lesson.get("start", ""))
                    end_t = plugin._parse_hhmm(lesson.get("end", ""))
                    if start_t and end_t:
                        intervals.append({
                            "start": datetime.combine(day, start_t, tzinfo=start.tzinfo),
                            "end": datetime.combine(day, end_t, tzinfo=start.tzinfo),
                            "title": str(lesson.get("subject") or "Schule"),
                            "source": "edupage",
                        })
            day += timedelta(days=1)
        return live_days, intervals

    async def effective_busy(self, start: str, end: str) -> list[dict]:
        tz = ZoneInfo(get_settings().astra_timezone)
        start_dt, end_dt = self._parse_datetime(start, tz), self._parse_datetime(end, tz)
        if end_dt <= start_dt:
            raise ValueError("end muss nach start liegen.")
        events = await self.events(start_dt.isoformat(), end_dt.isoformat(), max_results=250)
        try:
            live_days, school_intervals = await asyncio.wait_for(
                self._live_school_intervals(start_dt, end_dt), timeout=12,
            )
        except Exception:  # noqa: BLE001
            log.warning("EduPage calendar overlay timed out; using imported baseline.")
            live_days, school_intervals = set(), []
        busy = []
        for event in events:
            interval = self._event_interval(event, tz)
            if not interval:
                continue
            event_start, event_end = interval
            if SCHOOL_BASELINE_MARKER in str(event.get("description") or "") and event_start.date() in live_days:
                continue
            if event_end > start_dt and event_start < end_dt:
                busy.append({
                    "start": event_start,
                    "end": event_end,
                    "title": str(event.get("summary") or "Termin"),
                    "source": "google_calendar",
                    "event_id": str(event.get("id") or ""),
                })
        busy.extend(
            item for item in school_intervals
            if item["end"] > start_dt and item["start"] < end_dt
        )
        return sorted(busy, key=lambda item: item["start"])

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
            data = await self.add_event(
                title, start, end, args.get("description", ""), location=args.get("location", "")
            )
            link = data.get("htmlLink", "") if isinstance(data, dict) else ""
            return tool_result(
                ok=True,
                summary=f"Termin '{title}' angelegt.{(' ' + link) if link else ''}",
                data=data,
                source=self.slug,
            )

        async def _search(args: dict, ctx: ToolContext) -> str:
            tz = ZoneInfo(get_settings().astra_timezone)
            now = datetime.now(tz)
            start = str(args.get("start") or now.isoformat())
            end = str(args.get("end") or (now + timedelta(days=90)).isoformat())
            events = await self.events(start, end, max_results=100, query=str(args.get("query") or ""))
            if not events:
                return tool_result(ok=True, summary="Keine passenden Termine gefunden.", data=[], source=self.slug)
            return tool_result(
                ok=True,
                summary="Gefundene Termine:\n" + "\n".join(self._event_line(e) for e in events[:30]),
                data=events,
                source=self.slug,
            )

        async def _update(args: dict, ctx: ToolContext) -> str:
            event_id = str(args.get("event_id") or "").strip()
            if not event_id:
                return tool_result(ok=False, summary="event_id ist erforderlich.", source=self.slug)
            data = await self.update_event(event_id, args)
            return tool_result(ok=True, summary="Kalendertermin aktualisiert.", data=data, source=self.slug)

        async def _delete(args: dict, ctx: ToolContext) -> str:
            event_id = str(args.get("event_id") or "").strip()
            if not event_id:
                return tool_result(ok=False, summary="event_id ist erforderlich.", source=self.slug)
            data = await self.delete_event(event_id)
            return tool_result(ok=True, summary="Kalendertermin geloescht.", data=data, source=self.slug)

        async def _freebusy(args: dict, ctx: ToolContext) -> str:
            start, end = str(args.get("start") or ""), str(args.get("end") or "")
            if not start or not end:
                return tool_result(ok=False, summary="start und end sind erforderlich.", source=self.slug)
            tz = ZoneInfo(get_settings().astra_timezone)
            start_dt, end_dt = self._parse_datetime(start, tz), self._parse_datetime(end, tz)
            if not ctx.is_owner and end_dt - start_dt > timedelta(days=14):
                return tool_result(
                    ok=False,
                    summary="Free/Busy-Abfragen fuer Dritte sind auf 14 Tage begrenzt.",
                    source=self.slug,
                )
            if not ctx.is_owner and ctx.max_sensitivity not in {"freebusy", "details"}:
                return tool_result(
                    ok=False,
                    summary="Fuer diese Person ist keine Kalenderauskunft freigegeben.",
                    source=self.slug,
                )
            blocked = await self.effective_busy(start, end)
            if not blocked:
                return tool_result(
                    ok=True,
                    summary="Bahrian ist in diesem Zeitraum frei.",
                    data={"busy": False, "start": start, "end": end},
                    source=self.slug,
                )
            if ctx.is_owner or ctx.max_sensitivity == "details":
                lines = [
                    f"- {item['start'].strftime('%d.%m. %H:%M')}-{item['end'].strftime('%H:%M')}: {item['title']}"
                    for item in blocked
                ]
                summary = "Bahrian ist in diesem Zeitraum beschaeftigt:\n" + "\n".join(lines)
                data = [{**item, "start": item["start"].isoformat(), "end": item["end"].isoformat()} for item in blocked]
            else:
                summary = "Bahrian ist in diesem Zeitraum beschaeftigt."
                data = {
                    "busy": True,
                    "blocked": [
                        {"start": item["start"].isoformat(), "end": item["end"].isoformat()}
                        for item in blocked
                    ],
                }
            return tool_result(ok=True, summary=summary, data=data, source=self.slug)

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
                    "location": {"type": "string"},
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
            Tool(
                name="google_calendar_search",
                description="Google-Kalendertermine nach Text und Zeitraum suchen; liefert event_id fuer Aenderungen.",
                parameters={"type": "object", "properties": {
                    "query": {"type": "string"},
                    "start": {"type": "string", "description": "ISO-8601 Datetime"},
                    "end": {"type": "string", "description": "ISO-8601 Datetime"},
                }},
                handler=_search, owner_only=True, source=self.slug,
                safety="private_read", intents=["search", "list"],
            ),
            Tool(
                name="google_calendar_update_event",
                description="Google-Kalendertermin per event_id aendern.",
                parameters={"type": "object", "properties": {
                    "event_id": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                }, "required": ["event_id"]},
                handler=_update, owner_only=True, source=self.slug,
                safety="mutation", intents=["update"],
            ),
            Tool(
                name="google_calendar_delete_event",
                description="Google-Kalendertermin per event_id loeschen.",
                parameters={"type": "object", "properties": {
                    "event_id": {"type": "string"},
                }, "required": ["event_id"]},
                handler=_delete, owner_only=True, source=self.slug,
                safety="destructive", intents=["delete"],
            ),
            Tool(
                name="calendar_freebusy",
                description=(
                    "Pruefe, ob Bahrian in einem Zeitraum frei ist. Fuer Dritte wird hart nur Free/Busy "
                    "ausgegeben; Details nur bei entsprechendem Freigabe-Ceiling. EduPage ersetzt dabei "
                    "den statischen Schulplan fuer Tage mit erfolgreichem Live-Abruf."
                ),
                parameters={"type": "object", "properties": {
                    "start": {"type": "string", "description": "ISO-8601 Datetime"},
                    "end": {"type": "string", "description": "ISO-8601 Datetime"},
                }, "required": ["start", "end"]},
                handler=_freebusy, owner_only=True, source=self.slug,
                safety="private_read", intents=["search", "status"],
                examples=["Ist Bahrian morgen um 16 Uhr frei?"],
            ),
        ]
