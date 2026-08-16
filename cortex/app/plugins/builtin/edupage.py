"""EduPage school timetable (community `edupage-api`, optional dependency)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ...config import get_settings
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.edupage")


def _err(e: Exception, *, method: str, day: date_cls) -> dict:
    return {"type": type(e).__name__, "message": str(e), "method": method, "date": day.isoformat()}


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
        ConfigField("child_id", "Kind-ID (optional)", required=False,
                    help="Für Elternaccounts: EduPage person_id des Kindes. Leer = kein Wechsel."),
    ]

    @staticmethod
    def _normalize_subdomain(raw: str) -> str:
        sub = (raw or "").strip()
        sub = sub.removeprefix("https://").removeprefix("http://")
        sub = sub.split("/", 1)[0]
        if sub.endswith(".edupage.org"):
            sub = sub[:-len(".edupage.org")]
        return sub.strip()

    def _login_sync(self):
        from edupage_api import Edupage  # type: ignore

        ep = Edupage()
        ep.login(self.get("username"), self.get("password"), self._normalize_subdomain(self.get("subdomain")))
        child_id = str(self.get("child_id") or "").strip()
        if child_id:
            if not child_id.isdigit():
                raise ValueError("child_id muss die numerische EduPage person_id sein.")
            ep.switch_to_child(int(child_id))
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

    def _fetch_sync(self, day: date_cls) -> dict:
        ep = self._login_sync()
        method = "get_my_timetable"
        user_id = ""
        try:
            user_id = str(ep.get_user_id() or "")
        except Exception:  # noqa: BLE001
            user_id = ""
        try:
            tt = ep.get_my_timetable(day)
        except AttributeError:
            method = "get_timetable_legacy"
            tt = ep.get_timetable(day)
        out = []
        for ls in (getattr(tt, "lessons", None) or []):
            out.append(self._lesson_to_dict(ls))
        warnings = []
        if user_id.startswith("Rodic") and not self.get("child_id"):
            warnings.append("EduPage ist als Elternaccount eingeloggt; child_id ist nicht gesetzt.")
        return {
            "ok": True, "method": method, "date": day.isoformat(), "lessons": out,
            "error": None, "user_id": user_id, "warnings": warnings,
        }

    def _changes_sync(self, day: date_cls) -> dict:
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
        return {
            "ok": True,
            "method": "get_timetable_changes",
            "date": day.isoformat(),
            "changes": changes,
            "error": None,
        }

    async def timetable(self, day: date_cls | None = None) -> list[dict]:
        day = day or date_cls.today()
        result = await self.timetable_result(day)
        return result.get("lessons", []) if result.get("ok") else []

    async def timetable_result(self, day: date_cls | None = None) -> dict:
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._fetch_sync, day)
        except Exception as e:  # noqa: BLE001
            log.warning("EduPage fetch failed: %s", e)
            return {
                "ok": False,
                "method": "get_my_timetable",
                "date": day.isoformat(),
                "lessons": [],
                "error": _err(e, method="get_my_timetable", day=day),
            }

    async def timetable_changes(self, day: date_cls | None = None) -> list[dict]:
        day = day or date_cls.today()
        result = await self.changes_result(day)
        return result.get("changes", []) if result.get("ok") else []

    async def changes_result(self, day: date_cls | None = None) -> dict:
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._changes_sync, day)
        except Exception as e:  # noqa: BLE001
            log.warning("EduPage substitution fetch failed: %s", e)
            return {
                "ok": False,
                "method": "get_timetable_changes",
                "date": day.isoformat(),
                "changes": [],
                "error": _err(e, method="get_timetable_changes", day=day),
            }

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            import edupage_api  # type: ignore  # noqa: F401
        except Exception:  # noqa: BLE001
            return HealthStatus.error("edupage-api Lib fehlt (im Image best-effort installiert).")
        try:
            await asyncio.wait_for(asyncio.to_thread(self._login_sync), timeout=15)
        except TimeoutError:
            return HealthStatus.error("EduPage-Login hat nach 15 Sekunden nicht geantwortet.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"EduPage-Login fehlgeschlagen: {e}")
        return HealthStatus.ok(f"EduPage-Login bestaetigt · Standardgruppe {self._default_group()}.")

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

    @staticmethod
    def _day_candidates(raw_day: str, today: date_cls) -> list[date_cls]:
        raw_day = (raw_day or "auto").strip().lower()
        if raw_day in ("now", "jetzt", "today", "heute"):
            return [today]
        if raw_day in ("tomorrow", "morgen"):
            return [today + timedelta(days=1)]
        if raw_day in ("auto", "next", "next_school_day", "nächster schultag", "naechster schultag"):
            return [today + timedelta(days=i) for i in range(0, 8)]
        try:
            return [date_cls.fromisoformat(raw_day)]
        except ValueError:
            return [today + timedelta(days=i) for i in range(0, 8)]

    @classmethod
    def _debug(cls, day: date_cls, raw_result: dict, changes_result: dict, filtered: list[dict], group: str) -> dict:
        raw_lessons = raw_result.get("lessons", []) if raw_result.get("ok") else []
        return {
            "date": day.isoformat(),
            "group": group,
            "timetable_method": raw_result.get("method"),
            "changes_method": changes_result.get("method"),
            "raw_lesson_count": len(raw_lessons),
            "filtered_lesson_count": len(filtered),
            "changes_count": len(changes_result.get("changes", []) if changes_result.get("ok") else []),
            "available_groups": sorted({g for l in raw_lessons for g in (l.get("groups") or [])}),
            "timetable_error": raw_result.get("error"),
            "changes_error": changes_result.get("error"),
            "user_id": raw_result.get("user_id", ""),
            "warnings": (raw_result.get("warnings") or []) + (changes_result.get("warnings") or []),
        }

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
            days = self._day_candidates(raw_day, today)

            checked: list[str] = []
            diagnostics = []
            for day in days:
                checked.append(day.isoformat())
                raw_result = await self.timetable_result(day)
                if not raw_result.get("ok"):
                    changes_result = await self.changes_result(day) if include_changes else {
                        "ok": True, "changes": [], "method": "disabled", "error": None,
                    }
                    debug = self._debug(day, raw_result, changes_result, [], group)
                    diagnostics.append(debug)
                    summary = (
                        f"Ich habe EduPage für {day.isoformat()} gefragt, aber die API lieferte "
                        f"{(raw_result.get('error') or {}).get('type', 'einen Fehler')}: "
                        f"{(raw_result.get('error') or {}).get('message', 'unbekannt')}."
                    )
                    return tool_result(
                        ok=False, summary=summary, data={"debug": debug}, source=self.slug,
                        error=raw_result.get("error"),
                    )
                raw_lessons = raw_result.get("lessons", [])
                lessons = self._filter_lessons(raw_lessons, group)
                changes_result = await self.changes_result(day) if include_changes else {
                    "ok": True, "changes": [], "method": "disabled", "error": None,
                }
                changes = changes_result.get("changes", []) if changes_result.get("ok") else []
                debug = self._debug(day, raw_result, changes_result, lessons, group)
                diagnostics.append(debug)
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
                        if not changes_result.get("ok"):
                            body += (
                                "\nVertretungen konnten nicht gelesen werden: "
                                + json.dumps(changes_result.get("error"), ensure_ascii=False)
                            )
                    return tool_result(
                        ok=True,
                        summary=body,
                        data={"date": day.isoformat(), "lessons": lessons, "changes": changes, "debug": debug},
                        source=self.slug,
                        warnings=debug.get("warnings", []) + ([] if changes_result.get("ok") else ["Vertretungen nicht verfügbar"]),
                        error=None if changes_result.get("ok") else changes_result.get("error"),
                    )
                if raw_lessons:
                    summary = (
                        f"Für {day.isoformat()} gibt es EduPage-Stunden, aber keine passende Stunde "
                        f"für Gruppe {group}. Verfügbare Gruppen: "
                        + ", ".join(sorted({g for l in raw_lessons for g in (l.get("groups") or [])}) or ["—"])
                    )
                    return tool_result(ok=False, summary=summary, data={"debug": debug}, source=self.slug)
                if changes:
                    summary = f"Kein Stundenplan für {day.isoformat()} gefunden.\n\n{self._format_changes(changes, group)}"
                    return tool_result(
                        ok=True, summary=summary,
                        data={"date": day.isoformat(), "lessons": [], "changes": changes, "debug": debug},
                        source=self.slug,
                    )
            return tool_result(
                ok=False,
                summary=f"Kein Stundenplan gefunden. Geprüft: {', '.join(checked)}.",
                data={"checked": checked, "debug": diagnostics},
                source=self.slug,
                error={"type": "empty_timetable", "message": "EduPage lieferte keine Stunden für die geprüften Tage."},
            )

        async def _get_changes(args: dict, ctx: ToolContext) -> str:
            tz = ZoneInfo(get_settings().astra_timezone)
            today = datetime.now(tz).date()
            day = self._day_candidates(str(args.get("day") or "today"), today)[0]
            group = self._wanted_group(args, self._default_group())
            result = await self.changes_result(day)
            if not result.get("ok"):
                summary = (
                    f"Ich habe EduPage-Vertretungen für {day.isoformat()} gefragt, aber die API lieferte "
                    f"{(result.get('error') or {}).get('type', 'einen Fehler')}: "
                    f"{(result.get('error') or {}).get('message', 'unbekannt')}."
                )
                return tool_result(ok=False, summary=summary, data=result, source=self.slug, error=result.get("error"))
            changes = result.get("changes", [])
            summary = f"{day.isoformat()}: " + self._format_changes(changes, group)
            return tool_result(ok=True, summary=summary, data=result, source=self.slug)

        async def _debug_day(args: dict, ctx: ToolContext) -> str:
            tz = ZoneInfo(get_settings().astra_timezone)
            today = datetime.now(tz).date()
            day = self._day_candidates(str(args.get("day") or "tomorrow"), today)[0]
            group = self._wanted_group(args, self._default_group())
            raw_result = await self.timetable_result(day)
            raw_lessons = raw_result.get("lessons", []) if raw_result.get("ok") else []
            lessons = self._filter_lessons(raw_lessons, group)
            changes_result = await self.changes_result(day)
            debug = self._debug(day, raw_result, changes_result, lessons, group)
            summary = f"EduPage Debug {day.isoformat()}: {json.dumps(debug, ensure_ascii=False)}"
            return tool_result(
                ok=bool(raw_result.get("ok")),
                summary=summary,
                data={"debug": debug, "raw": raw_result, "changes": changes_result},
                source=self.slug,
                error=raw_result.get("error") or changes_result.get("error"),
            )

        return [
            Tool(
            name="edupage_get_timetable",
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
            safety="private_read", intents=["now", "list", "status"],
            examples=["Was habe ich morgen?", "Was hat Gruppe A morgen?", "Was habe ich jetzt?"],
            ),
            Tool(
                name="get_timetable",
                description="Alias für edupage_get_timetable.",
                parameters={"type": "object", "properties": {
                    "day": {"type": "string"}, "group": {"type": "string"},
                    "mode": {"type": "string"}, "include_changes": {"type": "boolean"}}},
                handler=_get_timetable, owner_only=True, source=self.slug,
                safety="private_read", intents=["now", "list"],
                examples=["Stundenplan morgen"],
            ),
            Tool(
                name="edupage_get_changes",
                description="Hole EduPage-Vertretungen/Änderungen für einen exakten Tag.",
                parameters={"type": "object", "properties": {
                    "day": {"type": "string", "description": "today, tomorrow oder YYYY-MM-DD"},
                    "group": {"type": "string", "description": "A oder B; leer = Standardgruppe B"}}},
                handler=_get_changes, owner_only=True, source=self.slug,
                safety="private_read", intents=["status", "list"],
                examples=["Gibt es morgen Vertretungen?"],
            ),
            Tool(
                name="edupage_debug_day",
                description="Admin-Diagnose für EduPage an einem Tag: Methoden, Counts, verfügbare Gruppen, Fehler.",
                parameters={"type": "object", "properties": {
                    "day": {"type": "string", "description": "today, tomorrow oder YYYY-MM-DD"},
                    "group": {"type": "string"}}},
                handler=_debug_day, owner_only=True, source=self.slug,
                safety="private_read", intents=["status"],
                examples=["Debug EduPage morgen"],
            ),
        ]

    async def briefing_section(self) -> str | None:
        lessons = self._filter_lessons(await self.timetable(date_cls.today()), self._default_group())
        if not lessons:
            return None
        body = ", ".join(l["subject"] for l in lessons[:8])
        return f"🏫 *Schule:* {lessons[0]['start']}–{lessons[-1]['end']} · {body}"
