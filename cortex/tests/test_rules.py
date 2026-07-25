"""Regelwerk (W4): die Zeit- und Bedingungslogik ist rein und damit der Kern-Test.
Sie entscheidet, ob eine Regel JETZT feuert und ob ihre Bedingung greift."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import rules


def _at(h, m, wd_date="2026-07-24"):  # 2026-07-24 ist ein Freitag (weekday 4)
    y, mo, d = (int(x) for x in wd_date.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=ZoneInfo("Europe/Berlin"))


# ─── schedule_matches ─────────────────────────────────────────────────────────
def test_fires_inside_its_minute_slot():
    trig = {"type": "schedule", "at": "21:00"}
    assert rules.schedule_matches(trig, _at(21, 0)) is True
    assert rules.schedule_matches(trig, _at(21, 1)) is False
    assert rules.schedule_matches(trig, _at(20, 59)) is False


def test_offset_shifts_the_fire_time_earlier():
    # "10 min vor 22:00" → feuert um 21:50.
    trig = {"type": "schedule", "at": "22:00", "offset_min": 10}
    assert rules.schedule_matches(trig, _at(21, 50)) is True
    assert rules.schedule_matches(trig, _at(22, 0)) is False


def test_day_filter():
    trig = {"type": "schedule", "at": "08:00", "days": [0, 1, 2, 3, 4]}  # Mo–Fr
    assert rules.schedule_matches(trig, _at(8, 0, "2026-07-24")) is True   # Freitag
    assert rules.schedule_matches(trig, _at(8, 0, "2026-07-25")) is False  # Samstag


def test_debounced_by_last_run_in_same_minute():
    trig = {"type": "schedule", "at": "21:00"}
    now = _at(21, 0)
    assert rules.schedule_matches(trig, now, last_run=now) is False


def test_non_schedule_trigger_never_matches_here():
    assert rules.schedule_matches({"type": "state"}, _at(21, 0)) is False
    assert rules.schedule_matches({"type": "schedule", "at": "nonsense"}, _at(21, 0)) is False


# ─── condition_holds ──────────────────────────────────────────────────────────
def test_condition_equals_on_nested_path():
    result = {"data": {"done": False, "streak": 12}}
    assert rules.condition_holds(result, {"path": "data.done", "equals": False}) is True
    assert rules.condition_holds(result, {"path": "data.done", "equals": True}) is False


def test_condition_numeric_comparisons():
    result = {"data": {"streak": 12}}
    assert rules.condition_holds(result, {"path": "data.streak", "gt": 5}) is True
    assert rules.condition_holds(result, {"path": "data.streak", "lt": 5}) is False


def test_empty_expectation_always_holds():
    assert rules.condition_holds({"anything": 1}, {}) is True


def test_missing_path_is_falsey():
    assert rules.condition_holds({}, {"path": "data.done", "equals": False}) is False


# ─── Das Duolingo-Regeltemplate ist wohlgeformt ───────────────────────────────
def test_duolingo_template_is_valid_rule_shape():
    from app.plugins.builtin.duolingo import DuolingoPlugin
    tmpl = DuolingoPlugin({"__enabled": True, "username": "x"}).rule_templates()[0]
    assert tmpl["trigger"]["type"] == "schedule"
    assert tmpl["condition"]["expect"]["equals"] is False
    kinds = [a["type"] for a in tmpl["actions"]]
    assert kinds == ["speak", "notify", "tool"]
