"""Telegram-Schnellbefehle: Parser (rein), Befristung und Anwenden auf die Einstellungen."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import owner_commands as oc
from app import secretary

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 18, 14, 0, tzinfo=TZ)


@pytest.mark.parametrize("text,action", [
    ("/secretary aus", "off"), ("/secretary an", "on"), ("/secretary auto", "auto"),
    ("/secretary", "status"), ("/sekretär", "status"), ("/secretary status", "status"),
    ("schalte den Secretary aus", "off"), ("Secretary aus", "off"), ("sekretär an", "on"),
    ("schalte deinen Sekretär Modus an", "on"), ("secretary auf automatisch", "auto"),
    ("Secretary pause", "off"), ("secretary ausschalten", "off"), ("Secretary einschalten", "on"),
])
def test_commands_are_understood(text, action):
    cmd = oc.parse(text)
    assert cmd is not None and cmd.action == action, text


@pytest.mark.parametrize("text", [
    "Hast du morgen Zeit?", "Wie geht's dir?", "Der Sekretär meiner Mutter ist krank",
    "", "schalte das Licht aus", "mach die Musik an",
    "x" * 200 + " secretary aus",              # zu lang → normale Nachricht
])
def test_normal_chat_is_never_hijacked(text):
    assert oc.parse(text) is None


def test_duration_parsing():
    assert oc.parse("/secretary aus 2h").minutes == 120
    assert oc.parse("/secretary aus 30 min").minutes == 30
    assert oc.parse("secretary aus für 1,5 stunden").minutes == 90
    assert oc.parse("sekretär an für 3 stunden").minutes == 180


def test_until_parsing():
    c = oc.parse("secretary bis 18 uhr aus")
    assert c.action == "off" and c.until_hhmm == (18, 0)
    c = oc.parse("/secretary aus bis 18:30")
    assert c.until_hhmm == (18, 30)
    c = oc.parse("/secretary aus bis morgen 8")
    assert c.until_hhmm == (8, 0) and c.tomorrow


def test_resolve_until_same_day_and_rollover():
    assert oc.resolve_until(oc.parse("/secretary aus bis 18:00"), NOW).hour == 18
    # 12 Uhr liegt schon hinter uns (jetzt 14:00) → morgen
    end = oc.resolve_until(oc.parse("/secretary aus bis 12:00"), NOW)
    assert end.day == 19 and end.hour == 12
    assert oc.resolve_until(oc.parse("/secretary aus 2h"), NOW).hour == 16
    assert oc.resolve_until(oc.parse("/secretary aus"), NOW) is None


def test_permanent_off_and_on_write_the_activation_mode():
    new, reply = oc.apply_to_settings({}, oc.Command("off"), NOW)
    assert new["secretary"]["activation_mode"] == "off" and "AUS" in reply
    new, _ = oc.apply_to_settings(new, oc.Command("on"), NOW)
    assert new["secretary"]["activation_mode"] == "on" and new["secretary"]["enabled"] is True


def test_timed_off_sets_an_override_that_expires_by_itself():
    now = datetime.now(TZ)     # secretary_settings liest die echte Uhr — Test darf nicht davon abweichen
    new, reply = oc.apply_to_settings({"secretary": {"activation_mode": "auto"}},
                                      oc.parse("/secretary aus 2h"), now)
    assert new["secretary"]["activation_mode"] == "auto"           # Grundmodus unangetastet
    # Innerhalb der Frist wirkt „aus“ …
    st = secretary.secretary_settings(new)
    assert st["activation_mode"] == "off" and st["base_mode"] == "auto"
    # … und nach Ablauf ist es ohne Aufräumen wieder Auto.
    from datetime import timedelta
    ov = secretary.active_override(new["secretary"]["override"], now=now + timedelta(hours=2, minutes=1))
    assert ov is None


def test_expired_override_is_ignored():
    sec = {"activation_mode": "auto",
           "override": {"mode": "off", "until": "2020-01-01T00:00:00+00:00"}}
    assert secretary.secretary_settings({"secretary": sec})["activation_mode"] == "auto"


def test_garbage_override_is_ignored():
    for ov in ({"mode": "off", "until": "kaputt"}, {"mode": "weird", "until": "2099-01-01T00:00:00+00:00"}, {}):
        assert secretary.active_override(ov) is None


def test_auto_clears_any_override():
    start = {"secretary": {"override": {"mode": "off", "until": "2099-01-01T00:00:00+00:00"}}}
    new, _ = oc.apply_to_settings(start, oc.Command("auto"), NOW)
    assert "override" not in new["secretary"] and new["secretary"]["activation_mode"] == "auto"


def test_button_callbacks():
    assert oc.command_from_callback("sec:off").action == "off"
    assert oc.command_from_callback("sec:auto").action == "auto"
    c = oc.command_from_callback("sec:pause60")
    assert c.action == "off" and c.minutes == 60
    assert oc.command_from_callback("apv:1:yes") is None
    assert oc.command_from_callback("sec:evil") is None
    assert {b["callback_data"] for b in oc.buttons()} >= {"sec:on", "sec:off", "sec:auto"}
