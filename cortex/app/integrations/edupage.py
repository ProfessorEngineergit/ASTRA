"""EduPage timetable client.

Uses the community `edupage-api` library (optional dependency — best-effort
installed in the Dockerfile, like mem0ai). If it's missing or login fails, this
degrades to an empty timetable so nothing else breaks.

The library is synchronous, so calls are off-loaded to a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date as date_cls

from ..config import get_settings

log = logging.getLogger("astra.edupage")


@dataclass
class Lesson:
    period: str
    subject: str
    teacher: str
    classroom: str
    start: str
    end: str


class EduPage:
    def __init__(self) -> None:
        self.s = get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.edupage_enabled

    def _fetch_sync(self, day: date_cls) -> list[Lesson]:
        from edupage_api import Edupage  # type: ignore

        ep = Edupage()
        ep.login(self.s.edupage_username, self.s.edupage_password, self.s.edupage_subdomain)
        tt = ep.get_timetable(day)
        lessons: list[Lesson] = []
        for ls in (getattr(tt, "lessons", None) or []):
            def _name(obj):
                return getattr(obj, "name", None) or (str(obj) if obj else "")

            teachers = getattr(ls, "teachers", None) or []
            rooms = getattr(ls, "classrooms", None) or []
            lessons.append(
                Lesson(
                    period=str(getattr(ls, "period", "") or ""),
                    subject=_name(getattr(ls, "subject", "")),
                    teacher=", ".join(_name(t) for t in teachers),
                    classroom=", ".join(_name(r) for r in rooms),
                    start=str(getattr(ls, "start_time", "") or ""),
                    end=str(getattr(ls, "end_time", "") or ""),
                )
            )
        return lessons

    async def timetable(self, day: date_cls | None = None) -> list[Lesson]:
        if not self.enabled:
            return []
        day = day or date_cls.today()
        try:
            return await asyncio.to_thread(self._fetch_sync, day)
        except Exception as e:  # noqa: BLE001 — library/login/network issues never crash us
            log.warning("EduPage timetable fetch failed: %s", e)
            return []


_edupage: EduPage | None = None


def get_edupage() -> EduPage:
    global _edupage
    if _edupage is None:
        _edupage = EduPage()
    return _edupage
