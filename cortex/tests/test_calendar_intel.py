"""Kalender-Intelligenz: freie Fenster, Terminvorschläge, Konflikte, Datenschutz-Stufen."""
from __future__ import annotations

import asyncio
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app import calendar_intel as ci

TZ = ZoneInfo("Europe/Berlin")


def at(day, h, m=0):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)     # 21.09.2026 = Montag


def busy(day, h1, h2, title="Termin", m1=0, m2=0):
    return {"start": at(day, h1, m1), "end": at(day, h2, m2), "title": title}


# ─── Zusammenfassen ───────────────────────────────────────────────────────────
def test_overlapping_and_touching_busy_blocks_are_merged():
    merged = ci.merge_busy([busy(21, 10, 12), busy(21, 11, 13), busy(21, 13, 14), busy(21, 16, 17)])
    assert merged == [(at(21, 10), at(21, 14)), (at(21, 16), at(21, 17))]


def test_merge_accepts_tuples_and_ignores_zero_length():
    assert ci.merge_busy([(at(21, 10), at(21, 11)), (at(21, 12), at(21, 12))]) == [(at(21, 10), at(21, 11))]


# ─── Freie Fenster ────────────────────────────────────────────────────────────
def test_free_windows_respect_daily_bounds_and_gaps():
    wins = ci.free_windows([busy(21, 12, 14)], at(21, 0), at(21, 23, 59),
                           day_start=time(9, 0), day_end=time(18, 0))
    assert [(w.start.hour, w.end.hour) for w in wins] == [(9, 12), (14, 18)]


def test_buffer_shrinks_the_windows_around_appointments():
    wins = ci.free_windows([busy(21, 12, 14)], at(21, 0), at(21, 23, 59), day_start=time(9, 0),
                           day_end=time(18, 0), buffer_minutes=30)
    assert (wins[0].end.hour, wins[0].end.minute) == (11, 30) and (wins[1].start.hour, wins[1].start.minute) == (14, 30)


def test_a_fully_booked_day_has_no_windows():
    assert ci.free_windows([busy(21, 8, 21)], at(21, 0), at(21, 23, 59), day_start=time(9, 0), day_end=time(18, 0)) == []


def test_weekday_filter_skips_weekends():
    wins = ci.free_windows([], at(26, 0), at(27, 23, 59), weekdays={0, 1, 2, 3, 4})   # Sa+So
    assert wins == []


def test_multi_day_range():
    wins = ci.free_windows([busy(21, 9, 18)], at(21, 0), at(22, 23, 59), day_start=time(9, 0), day_end=time(18, 0))
    assert [w.start.day for w in wins] == [22]


# ─── Vorschläge ───────────────────────────────────────────────────────────────
def test_proposals_are_aligned_free_and_long_enough():
    slots = ci.propose_times([busy(21, 9, 17)], now=at(21, 8, 10), duration_min=60, limit=3)
    assert slots and all(s.minutes == 60 for s in slots)
    assert all(s.start.minute in (0, 30) for s in slots)
    assert not ci.find_conflicts([busy(21, 9, 17)], slots[0].start, slots[0].end)
    assert slots[0].start >= at(21, 17)                          # erst nach dem Termin


def test_proposals_start_after_the_lead_time():
    slots = ci.propose_times([], now=at(21, 10, 0), lead_min=90, limit=1)
    assert slots[0].start >= at(21, 11, 30)


def test_proposals_are_spread_over_days_not_piled_on_one_afternoon():
    slots = ci.propose_times([], now=at(21, 8), days=5, limit=6, per_day=2, duration_min=60)
    days = [s.start.day for s in slots]
    assert max(days.count(d) for d in set(days)) <= 2 and len(set(days)) >= 3


def test_proposals_never_overlap_each_other_or_busy_time():
    b = [busy(21, 10, 12), busy(21, 15, 16), busy(22, 9, 20)]
    slots = ci.propose_times(b, now=at(21, 8), days=4, limit=6, duration_min=90)
    for i, s in enumerate(slots):
        assert not ci.find_conflicts(b, s.start, s.end)
        for other in slots[i + 1:]:
            assert s.end <= other.start or other.end <= s.start


def test_duration_longer_than_any_window_yields_nothing():
    assert ci.propose_times([busy(21, 9, 19)], now=at(21, 8), days=1, duration_min=180, earliest=time(9), latest=time(20)) == []


def test_slot_label_is_human_readable():
    assert ci.Slot(at(21, 17), at(21, 18)).label() == "Mo 21.09. 17:00–18:00"


# ─── Konflikte ────────────────────────────────────────────────────────────────
def test_conflict_detection_edges():
    b = [busy(21, 10, 12, "Klavier")]
    assert ci.find_conflicts(b, at(21, 11), at(21, 13))[0]["title"] == "Klavier"
    assert ci.find_conflicts(b, at(21, 12), at(21, 13)) == []      # Ende = Anfang → kein Konflikt
    assert ci.find_conflicts(b, at(21, 9), at(21, 10)) == []


# ─── Datenschutz-Stufen ───────────────────────────────────────────────────────
def test_reveal_levels():
    items = [busy(21, 10, 12, "Arzttermin")]
    assert ci.reveal(items, "none") == [] and ci.reveal(items, "") == []
    fb = ci.reveal(items, "freebusy")
    assert fb and "title" not in fb[0]
    assert ci.reveal(items, "details")[0]["title"] == "Arzttermin"


# ─── Das Werkzeug ─────────────────────────────────────────────────────────────
def test_tool_is_available_to_third_parties_but_gated_by_the_card_ceiling():
    from app import tools
    t = tools.REGISTRY["suggest_meeting_times"]
    assert t.owner_only is False
    ctx = tools.ToolContext(thread_id="t", channel="waha", contact={}, max_sensitivity="none")
    out = asyncio.run(t.handler({}, ctx))
    assert '"ok": false' in out.lower() and "keine Kalenderauskunft" in out


def test_tool_output_never_contains_appointment_titles(monkeypatch):
    from app import tools
    from app.plugins import registry

    class Cal:
        enabled = True

        async def effective_busy(self, start, end):
            now = datetime.now(TZ)
            return [{"start": now, "end": now.replace(hour=23, minute=59), "title": "Geheimer Arzttermin"}]

    class Mgr:
        def get(self, slug):
            return Cal()
    monkeypatch.setattr(registry, "get_manager", lambda: Mgr())
    ctx = tools.ToolContext(thread_id="t", channel="waha", contact={}, max_sensitivity="details")
    out = asyncio.run(tools.REGISTRY["suggest_meeting_times"].handler({"days": 3}, ctx))
    assert "Geheimer Arzttermin" not in out and "Freie Zeiten" in out
