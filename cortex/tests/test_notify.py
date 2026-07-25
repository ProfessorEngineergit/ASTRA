"""Zustellungs-Router (W3): die Kanalwahl ist reine Logik und damit der Kern-Test.
Dringlichkeit UND Anwesenheit entscheiden, wohin eine Meldung geht."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import notify


# ─── Kanalwahl ────────────────────────────────────────────────────────────────
def test_control_always_telegram():
    assert notify.choose_channels("control", at_home=True, awake=True) == ["telegram"]
    assert notify.choose_channels("control", at_home=False, awake=False) == ["telegram"]


def test_normal_is_push():
    assert notify.choose_channels("normal", at_home=True, awake=True) == ["push"]
    assert notify.choose_channels("normal", at_home=False, awake=True) == ["push"]


def test_urgent_adds_speaker_only_when_home_and_awake():
    assert notify.choose_channels("urgent", at_home=True, awake=True) == ["push", "speak"]
    assert notify.choose_channels("urgent", at_home=False, awake=True) == ["push"]
    assert notify.choose_channels("urgent", at_home=True, awake=False) == ["push"]


def test_unknown_presence_never_speaks():
    # at_home=None (HA nicht erreichbar) darf nie laut werden — Push reicht.
    assert notify.choose_channels("urgent", at_home=None, awake=True) == ["push"]


# ─── Wach-Fenster ─────────────────────────────────────────────────────────────
def _at(h, m=0):
    return datetime(2026, 7, 25, h, m, tzinfo=ZoneInfo("Europe/Berlin"))


def test_awake_default_window():
    s = {}
    assert notify.is_awake(s, now=_at(9)) is True
    assert notify.is_awake(s, now=_at(3)) is False
    assert notify.is_awake(s, now=_at(23, 30)) is False


def test_awake_custom_window():
    s = {"awake_start": "06:30", "awake_end": "22:00"}
    assert notify.is_awake(s, now=_at(6, 45)) is True
    assert notify.is_awake(s, now=_at(22, 30)) is False


def test_awake_window_over_midnight():
    # Nachtschicht: 22:00–06:00 → 02:00 gilt als wach.
    s = {"awake_start": "22:00", "awake_end": "06:00"}
    assert notify.is_awake(s, now=_at(2)) is True
    assert notify.is_awake(s, now=_at(12)) is False
