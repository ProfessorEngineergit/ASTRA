"""Briefing scheduler time math — pure, no network."""
from __future__ import annotations

from datetime import time

from app import briefing


def test_parse_time_valid():
    assert briefing._parse_time("07:30") == time(7, 30)
    assert briefing._parse_time("23:05") == time(23, 5)


def test_parse_time_invalid_falls_back_to_0700():
    assert briefing._parse_time("garbage") == time(7, 0)
    assert briefing._parse_time("") == time(7, 0)


def test_seconds_until_is_within_a_day():
    secs = briefing._seconds_until(time(7, 0))
    assert 0 < secs <= 24 * 3600


def test_channel_label():
    assert briefing._channel_label("waha") == "WhatsApp"
    assert briefing._channel_label("signal") == "Signal"
    assert briefing._channel_label("unknown") == "unknown"
