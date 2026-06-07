"""EduPage school timetable (community `edupage-api`, optional dependency)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ...config import get_settings
from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.edupage")


class EduPagePlugin(Plugin):
    slug = "edupage"
    name = "EduPage (Stundenplan)"
    description = "Schul-Stundenplan für heute/morgen aus EduPage."
    category = PluginCategory.SCHOOL
    icon = "🏫"
    config_fields = [
        ConfigField("subdomain", "Subdomain", required=True,
                    help='z.B. "gymnasium-xy" → gymnasium-xy.edupage.org',
                    env_fallback="edupage_subdomain"),
        ConfigField("username", "Benutzername", required=True, env_fallback="edupage_username"),
        ConfigField("password", "Passwort", FieldType.PASSWORD, required=True, secret=True,
                    env_fallback="edupage_password"),
        ConfigField("preferred_group", "Standard-Gruppe", required=False, default="B",
                    help="z. B. B. Wird genutzt, wenn Bahrian keine Gruppe nennt."),
    ]

    def _login_sync(self):
        from edupage_api import Edupage  # type: ignore

        ep = Edupage()
        ep.login(self.get("username"), self.get("password"), self.get("subdomain"))
        return ep

    @staticmethod
    def _name(o) -> str:
        return getattr(o, "name", None) or getattr(o, "short", None) or (str(o) if o else "")

    @staticmethod
    def _time_text(value) -> str:
        if isinstance(value, time):
            return value.strftime("%H:%M")
        return str(value or "")

    @classmethod
    def _lesson_to_dict(cls, ls) -> dict:
        groups = [str(g) for g in (getattr(ls, "groups", None) or []) if str(g)]
        is_cancelled = bool(getattr(ls, "is_cancelled", False))
        is_event = bool(getattr(ls, "is_event", False))
        online = getattr(ls, "online_lesson_link", None)
        return {
            "period": str(getattr(ls, "period", "") or ""),
            "subject": cls._name(getattr(ls, "subject", "")) or ("Termin" if is_event else ""),
            "teacher": ", ".join(cls._name(t) for t in (getattr(ls, "teachers", None) or [])),
            "classroom": ", ".join(cls._name(r) for r in (getattr(ls, "classrooms", None) or [])),
            "classes": ", ".join(cls._name(c) for c in (getattr(ls, "classes", None) or [])),
            "groups": groups,
            "start": cls._time_text(getattr(ls, "start_time", "") or getattr(ls, "start", "")),
            "end": cls._time_text(getattr(ls, "end_time", "") or getattr(ls, "end", "")),
            "curriculum": str(getattr(ls, "curriculum", "") or ""),
            "online": bool(online),
            "cancelled": is_cancelled,
            "event": is_event,
        }

    def _fetch_sync(self, day: date_cls) -> list[dict]:
        ep = self._login_sync()
        try:
            tt = ep.get_my_timetable(day)
        except AttributeError:
            tt = ep.get_timetable(day)
        out = []
        for ls in (getattr(tt, "lessons", None) or []):
            out.append(self._lesson_to_dict(ls))
        return out

    def _changes_sync(self, day: date_cls) -> list[dict]:
        ep = self._login_sync()
        changes = []
        for ch in ep.get_timetable_changes(day) or []:
            action = getattr(ch, "action", "")
            changes.append({
                "class": str(getattr(ch, "change_class", "") or ""),
                "lesson": str(getattr(ch, "lesson_n", "") or ""),
                "title": str(getattr(ch, "title", "") or ""),
                "action": getattr(action, "value", str(action or "")),
            })
        return changes

    async def timetable(self, day: date_cls | None = None) -> list[dict]:
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._fetch_sync, day)
        except Exception as e:  # noqa: BLE001
            log.warning("EduPage fetch failed: %s", e)
            return []

    async def timetable_changes(self, day: date_cls | None = None) -> list[dict]:
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._changes_sync, day)
        except Exception as e:  # noqa: BLE001
            log.warning("EduPage substitution fetch failed: %s", e)
            return []

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            import edupage_api  # type: ignore  # noqa: F401
        except Exception:  # noqa: BLE001
            return HealthStatus.error("edupage-api Lib fehlt (im Image best-effort installiert).")
        return HealthStatus.ok("Zugangsdaten gesetzt (Login wird bei Abruf geprüft).")

    def _default_group(self) -> str:
        return str(self.get("preferred_group") or "B").strip().upper()

    @staticmethod
    def _wanted_group(args: dict, default: str) -> str:
        raw = str(args.get("group") or args.get("gruppe") or default or "").strip().upper()
        if raw.startswith("GRUPPE "):
            raw = raw.split(None, 1)[1].strip()
        return raw

    @staticmethod
    def _lesson_matches_group(lesson: dict, group: str) -> bool:
        groups = [str(g).strip().upper() for g in lesson.get("groups") or [] if str(g).strip()]
        if not group or not groups:
            return True
        return any(group == g or group in g.split() or g.endswith(group) for g in groups)

    @classmethod
    def _filter_lessons(cls, lessons: list[dict], group: str) -> list[dict]:
        return [lesson for lesson in lessons if cls._lesson_matches_group(lesson, group)]

    @staticmethod
    def _parse_hhmm(value: str) -> time | None:
        try:
            h, m = str(value).split(":", 1)
            return time(int(h), int(m[:2]))
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _current_or_next(cls, lessons: list[dict], now_t: time) -> tuple[str, dict | None]:
        upcoming = []
        for lesson in lessons:
            start, end = cls._parse_hhmm(lesson.get("start", "")), cls._parse_hhmm(lesson.get("end", ""))
            if start and end and start <= now_t <= end:
                return "jetzt", lesson
            if start and now_t < start:
                upcoming.append((start, lesson))
        if upcoming:
            return "als Nächstes", sorted(upcoming, key=lambda x: x[0])[0][1]
        return "heute nicht mehr", None

    @staticmethod
    def _lesson_line(lesson: dict) -> str:
        bits = [
            f"{lesson.get('period')}. " if lesson.get("period") else "",
            str(lesson.get("subject") or "—"),
            f" {lesson.get('start')}-{lesson.get('end')}" if lesson.get("start") or lesson.get("end") else "",
        ]
        meta = []
        if lesson.get("groups"):
            meta.append("Gr. " + "/".join(lesson["groups"]))
        if lesson.get("classroom"):
            meta.append(str(lesson["classroom"]))
        if lesson.get("teacher"):
            meta.append(str(lesson["teacher"]))
        if lesson.get("cancelled"):
            meta.append("entfällt")
        if lesson.get("curriculum"):
            meta.append(str(lesson["curriculum"]))
        return "".join(bits).strip() + (f" ({', '.join(meta)})" if meta else "")

    @staticmethod
    def _format_changes(changes: list[dict], group: str = "") -> str:
        if not changes:
            return "Keine Vertretungen/Änderungen gemeldet."
        rows = []
        g = group.lower()
        for ch in changes:
            text = f"{ch.get('class')}: {ch.get('lesson')}. {ch.get('title')} [{ch.get('action')}]"
            if not g or g in text.lower() or "gruppe" not in text.lower():
                rows.append(text)
        if not rows:
            rows = [f"{ch.get('class')}: {ch.get('lesson')}. {ch.get('title')} [{ch.get('action')}]" for ch in changes]
        return "Vertretungen/Änderungen:\n- " + "\n- ".join(rows[:12])

    def tools(self) -> list[Tool]:
        async def _get_timetable(args: dict, ctx: ToolContext) -> str:
            tz = ZoneInfo(get_settings().astra_timezone)
            now = datetime.now(tz)
            today = now.date()
            raw_day = str(args.get("day") or "auto").strip().lower()
            mode = str(args.get("mode") or "").strip().lower()
            include_changes = bool(args.get("include_changes", True))
            group = self._wanted_group(args, self._default_group())
            if raw_day in ("now", "jetzt"):
                raw_day = "today"
                mode = "now"
            if raw_day in ("today", "heute"):
                days = [today]
            elif raw_day in ("tomorrow", "morgen"):
                days = [today + timedelta(days=1)]
            elif raw_day in ("auto", "next", "next_school_day", "nächster schultag", "naechster schultag"):
                days = [today + timedelta(days=i) for i in range(0, 8)]
            else:
                try:
                    days = [date_cls.fromisoformat(raw_day)]
                except ValueError:
                    days = [today + timedelta(days=i) for i in range(0, 8)]

            checked: list[str] = []
            for day in days:
                checked.append(day.isoformat())
                raw_lessons = await self.timetable(day)
                lessons = self._filter_lessons(raw_lessons, group)
                changes = await self.timetable_changes(day) if include_changes else []
                if lessons:
                    header = f"Stundenplan {day.isoformat()} · Gruppe {group}" if group else f"Stundenplan {day.isoformat()}"
                    if mode in ("now", "jetzt", "current", "next", "aktuell"):
                        label, lesson = self._current_or_next(lessons, now.time())
                        if lesson:
                            body = f"{header}\n{label}: {self._lesson_line(lesson)}"
                        else:
                            body = f"{header}\nHeute keine weitere Stunde für Gruppe {group}."
                    else:
                        lines = [self._lesson_line(l) for l in lessons]
                        body = header + ":\n- " + "\n- ".join(lines)
                    if include_changes:
                        body += "\n\n" + self._format_changes(changes, group)
                    return body
                if raw_lessons:
                    return (
                        f"Für {day.isoformat()} gibt es EduPage-Stunden, aber keine passende Stunde "
                        f"für Gruppe {group}. Verfügbare Gruppen: "
                        + ", ".join(sorted({g for l in raw_lessons for g in (l.get("groups") or [])}) or ["—"])
                    )
                if changes:
                    return f"Kein Stundenplan für {day.isoformat()} gefunden.\n\n{self._format_changes(changes, group)}"
            return f"Kein Stundenplan gefunden. Geprüft: {', '.join(checked)}."

        return [Tool(
            name="get_timetable",
            description=(
                "Hole Bahrians Schul-Stundenplan (EduPage). Ohne day sucht ASTRA den nächsten "
                "Tag mit Unterricht. Standardgruppe ist B, außer Bahrian fragt explizit nach Gruppe A/B. "
                "Für 'was habe ich jetzt?' mode=now nutzen. Vertretungen/Änderungen werden mitgelesen."
            ),
            parameters={"type": "object", "properties": {
                "day": {"type": "string", "description": "today, tomorrow, next_school_day/auto, now oder YYYY-MM-DD"},
                "group": {"type": "string", "description": "A oder B; leer = Bahrians Standardgruppe B"},
                "mode": {"type": "string", "description": "full oder now"},
                "include_changes": {"type": "boolean", "description": "Vertretungen/Änderungen mit ausgeben"}}},
            handler=_get_timetable, owner_only=True, source=self.slug,
        )]

    async def briefing_section(self) -> str | None:
        lessons = await self.timetable(date_cls.today())
        if not lessons:
            return None
        body = ", ".join(l["subject"] for l in lessons[:8])
        return f"🏫 *Schule:* {lessons[0]['start']}–{lessons[-1]['end']} · {body}"
