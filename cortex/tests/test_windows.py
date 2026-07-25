"""W6: die Zeitfenster-Engine ist reine Logik — sie entscheidet, was der Sekretär
zu welcher Uhrzeit tut. Inklusive Nachtruhe mit Tagesübergang (fehlte vorher)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app import secretary as sec


def _at(h, m=0, date="2026-07-24"):  # Freitag
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=ZoneInfo("Europe/Berlin"))


def _settings(windows):
    return {"secretary": {"windows": windows}}


def test_named_window_active_inside():
    s = _settings([{"name": "schule", "start": "07:30", "end": "15:30",
                    "days": [0, 1, 2, 3, 4], "behavior": "auto"}])
    assert sec.window_behavior(s, _at(10)) == "auto"
    assert sec.window_behavior(s, _at(16)) == ""


def test_night_window_wraps_past_midnight():
    s = _settings([{"name": "nacht", "start": "22:00", "end": "06:00",
                    "days": [0, 1, 2, 3, 4, 5, 6], "behavior": "silent"}])
    assert sec.window_behavior(s, _at(23)) == "silent"   # abends
    assert sec.window_behavior(s, _at(2)) == "silent"    # nachts
    assert sec.window_behavior(s, _at(12)) == ""         # mittags


def test_window_priority_is_list_order():
    s = _settings([
        {"name": "fokus", "start": "09:00", "end": "11:00", "behavior": "hold"},
        {"name": "tag", "start": "00:00", "end": "23:59", "behavior": "auto"},
    ])
    assert sec.active_window(s, _at(10))["name"] == "fokus"
    assert sec.active_window(s, _at(14))["name"] == "tag"


def test_silent_window_produces_silent_plan():
    s = _settings([{"name": "nacht", "start": "22:00", "end": "06:00", "behavior": "silent"}])
    plan = sec.plan_for(channel="waha", mode=sec.Mode.DEFER,
                        max_sensitivity=sec.Sensitivity.FREEBUSY, app_settings=s,
                        timezone="Europe/Berlin", now=_at(23))
    assert plan.silent is True


def test_backcompat_synthesizes_window_from_school_settings():
    s = {"secretary": {"school_start": "07:30", "school_end": "15:30",
                       "workdays": [0, 1, 2, 3, 4], "school_direct": True}}
    wins = sec.secretary_windows(s)
    assert wins and wins[0]["behavior"] == "auto"


def test_shadow_flags():
    assert sec.shadow_enabled({"secretary": {"shadow_all": True}}, "waha") is True
    assert sec.shadow_enabled({"secretary": {"shadow": {"waha": True}}}, "waha") is True
    assert sec.shadow_enabled({"secretary": {"shadow": {"waha": True}}}, "signal") is False
    assert sec.shadow_enabled({}, "waha") is False
