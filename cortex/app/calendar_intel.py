"""Kalender-Intelligenz — aus „belegt/frei“ wird „schlag Zeiten vor, erkenne Konflikte“.

Die Datenbasis (Google Kalender + EduPage-Stundenplan → `effective_busy`) gibt es schon.
Hier kommt die reine Rechenlogik obendrauf, ohne I/O und damit vollständig testbar:

  • merge_busy      überlappende/angrenzende Belegungen zusammenfassen
  • free_windows    freie Fenster je Tag innerhalb der „Tagesgrenzen“ (z. B. 09–20 Uhr)
  • propose_times   konkrete Terminvorschläge (auf :00/:30 gerastert, mit Puffer, über mehrere
                    Tage gestreut statt fünf Slots am selben Nachmittag)
  • find_conflicts  überschneidet ein geplanter Termin etwas?
  • reveal          was dürfen wir zeigen? Stufe none|freebusy|details (die Karten-Freigabe)

`reveal` ist die Datenschutz-Naht: Vorschläge enthalten NUR Zeiten. Titel erscheinen
ausschließlich bei „details“ — und auch dann nie in Terminvorschlägen an Dritte.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def label(self) -> str:
        d = f"{WEEKDAYS[self.start.weekday()]} {self.start:%d.%m.}"
        return f"{d} {self.start:%H:%M}–{self.end:%H:%M}"


def _iv(item) -> tuple[datetime, datetime, str]:
    """Belegung aus dict ({"start","end","title"}) oder Tupel normalisieren."""
    if isinstance(item, dict):
        return item["start"], item["end"], str(item.get("title") or "")
    return item[0], item[1], ""


def merge_busy(busy: list, *, gap_minutes: int = 0) -> list[tuple[datetime, datetime]]:
    """Überlappende (und optional knapp benachbarte) Belegungen zusammenfassen."""
    ivs = sorted(((s, e) for s, e, _ in map(_iv, busy) if e > s), key=lambda x: x[0])
    out: list[list[datetime]] = []
    gap = timedelta(minutes=gap_minutes)
    for s, e in ivs:
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def find_conflicts(busy: list, start: datetime, end: datetime) -> list[dict]:
    """Belegungen, die den geplanten Zeitraum überschneiden ([{start,end,title}])."""
    hits = []
    for item in busy:
        s, e, title = _iv(item)
        if e > start and s < end:
            hits.append({"start": s, "end": e, "title": title})
    return sorted(hits, key=lambda h: h["start"])


def free_windows(busy: list, range_start: datetime, range_end: datetime, *,
                 day_start: time = time(9, 0), day_end: time = time(20, 0),
                 weekdays: set[int] | None = None, buffer_minutes: int = 0) -> list[Slot]:
    """Freie Fenster je Tag zwischen `day_start` und `day_end` (lokale Zeit von range_start)."""
    merged = merge_busy(busy, gap_minutes=0)
    buf = timedelta(minutes=buffer_minutes)
    out: list[Slot] = []
    tz = range_start.tzinfo
    d = range_start.date()
    while d <= range_end.date():
        if weekdays is None or d.weekday() in weekdays:
            win_s = max(datetime.combine(d, day_start, tzinfo=tz), range_start)
            win_e = min(datetime.combine(d, day_end, tzinfo=tz), range_end)
            cur = win_s
            for s, e in merged:
                if e + buf <= cur or s - buf >= win_e:
                    continue
                if s - buf > cur:
                    out.append(Slot(cur, min(s - buf, win_e)))
                cur = max(cur, e + buf)
                if cur >= win_e:
                    break
            if cur < win_e:
                out.append(Slot(cur, win_e))
        d += timedelta(days=1)
    return [s for s in out if s.end > s.start]


def _ceil_to(dt: datetime, step: int) -> datetime:
    minute = (dt.minute + step - 1) // step * step
    base = dt.replace(second=0, microsecond=0)
    return base + timedelta(minutes=minute - dt.minute)


def propose_times(busy: list, *, now: datetime, days: int = 7, duration_min: int = 60,
                  earliest: time = time(9, 0), latest: time = time(20, 0),
                  weekdays: set[int] | None = None, limit: int = 5, buffer_min: int = 15,
                  step_min: int = 30, per_day: int = 2, lead_min: int = 60) -> list[Slot]:
    """Konkrete Terminvorschläge. Frühestens `lead_min` nach jetzt, auf `step_min` gerastert,
    höchstens `per_day` je Tag — so entsteht eine sinnvolle Auswahl statt fünf Slots am selben
    Nachmittag. Reihenfolge: nach Tag, dann Uhrzeit."""
    start = _ceil_to(now + timedelta(minutes=lead_min), step_min)
    windows = free_windows(busy, start, now + timedelta(days=days), day_start=earliest,
                           day_end=latest, weekdays=weekdays, buffer_minutes=buffer_min)
    need = timedelta(minutes=duration_min)
    picked: list[Slot] = []
    per: dict[date, int] = {}
    for w in windows:
        cursor = _ceil_to(w.start, step_min)
        while cursor + need <= w.end and len(picked) < limit:
            if per.get(cursor.date(), 0) >= per_day:
                break
            picked.append(Slot(cursor, cursor + need))
            per[cursor.date()] = per.get(cursor.date(), 0) + 1
            # nächster Vorschlag im selben Fenster frühestens 2 h später (Streuung)
            cursor = _ceil_to(cursor + need + timedelta(hours=2), step_min)
        if len(picked) >= limit:
            break
    return picked


def reveal(items: list, level: str) -> list[dict]:
    """Belegungen gemäß Freigabe-Stufe darstellbar machen (Datenschutz-Naht).

    none → nichts · freebusy → nur Zeiten · details → Zeiten + Titel."""
    if level not in ("freebusy", "details"):
        return []
    out = []
    for item in items:
        s, e, title = _iv(item)
        row = {"start": s.isoformat(), "end": e.isoformat()}
        if level == "details":
            row["title"] = title
        out.append(row)
    return out


def describe_slots(slots: list[Slot]) -> str:
    return "\n".join(f"• {s.label()}" for s in slots) if slots else "Keine passenden freien Zeiten gefunden."
