"""EduPage school timetable (community `edupage-api`, optional dependency)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date as date_cls
from datetime import timedelta

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
    ]

    def _fetch_sync(self, day: date_cls) -> list[dict]:
        from edupage_api import Edupage  # type: ignore

        ep = Edupage()
        ep.login(self.get("username"), self.get("password"), self.get("subdomain"))
        tt = ep.get_timetable(day)
        out = []
        for ls in (getattr(tt, "lessons", None) or []):
            def _name(o):
                return getattr(o, "name", None) or (str(o) if o else "")
            out.append({
                "period": str(getattr(ls, "period", "") or ""),
                "subject": _name(getattr(ls, "subject", "")),
                "teacher": ", ".join(_name(t) for t in (getattr(ls, "teachers", None) or [])),
                "classroom": ", ".join(_name(r) for r in (getattr(ls, "classrooms", None) or [])),
                "start": str(getattr(ls, "start_time", "") or ""),
                "end": str(getattr(ls, "end_time", "") or ""),
            })
        return out

    async def timetable(self, day: date_cls | None = None) -> list[dict]:
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._fetch_sync, day)
        except Exception as e:  # noqa: BLE001
            log.warning("EduPage fetch failed: %s", e)
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

    def tools(self) -> list[Tool]:
        async def _get_timetable(args: dict, ctx: ToolContext) -> str:
            day = date_cls.today()
            if (args.get("day") or "today").lower() in ("tomorrow", "morgen"):
                day = day + timedelta(days=1)
            lessons = await self.timetable(day)
            if not lessons:
                return f"Kein Stundenplan für {day.isoformat()} (oder unterrichtsfrei)."
            lines = [f"{l['period']}. {l['subject']} {l['start']}-{l['end']} "
                     f"({l['classroom']}, {l['teacher']})".strip() for l in lessons]
            return f"Stundenplan {day.isoformat()}:\n- " + "\n- ".join(lines)

        return [Tool(
            name="get_timetable",
            description="Hole Bahrians Schul-Stundenplan (EduPage) für heute oder morgen.",
            parameters={"type": "object", "properties": {
                "day": {"type": "string", "enum": ["today", "tomorrow"]}}},
            handler=_get_timetable, owner_only=True, source=self.slug,
        )]

    async def briefing_section(self) -> str | None:
        lessons = await self.timetable(date_cls.today())
        if not lessons:
            return None
        body = ", ".join(l["subject"] for l in lessons[:8])
        return f"🏫 *Schule:* {lessons[0]['start']}–{lessons[-1]['end']} · {body}"
