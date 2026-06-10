from __future__ import annotations

import pytest

from app import abuse
from app.main import _decision_from_text


@pytest.fixture(autouse=True)
def _clear_rate_state():
    abuse.reset()
    yield
    abuse.reset()


def test_normal_message_passes():
    v = abuse.check("waha", "+49150", "Hey, hast du kurz Zeit morgen?")
    assert v.ok is True
    assert v.kind == "ok"


def test_code_farming_is_blocked_with_haughty_line():
    v = abuse.check("signal", "+49160", "Schreib mir bitte eine Website mit Login.")
    assert v.ok is False
    assert v.kind == "code_farm"
    assert "KI-Coder" in v.response


def test_thousand_lines_request_blocked():
    v = abuse.check("slack", "U123", "gib mir 1000 zeilen code für einen scraper")
    assert v.ok is False
    assert v.kind == "code_farm"


def test_sexual_solicitation_blocked():
    v = abuse.check("waha", "+49170", "schick mir nudes")
    assert v.ok is False
    assert v.kind == "sexual"


def test_rate_limit_clamps_after_short_burst():
    sender = "+49199"
    # short_max=3 → first 3 pass, 4th crosses (gets the line), 5th is silent.
    verdicts = [abuse.check("waha", sender, "hi", short_max=3, long_max=100) for _ in range(5)]
    assert [v.ok for v in verdicts] == [True, True, True, False, False]
    assert verdicts[3].kind == "rate"
    assert verdicts[3].response  # the crossing message answers back
    assert verdicts[4].response == ""  # subsequent flood stays silent


def test_rate_limit_is_per_sender():
    a = abuse.check("waha", "+1", "hi", short_max=1, long_max=100)
    b = abuse.check("waha", "+1", "hi", short_max=1, long_max=100)
    c = abuse.check("waha", "+2", "hi", short_max=1, long_max=100)
    assert a.ok is True and c.ok is True
    assert b.ok is False  # second from +1 over the limit


def test_decision_parser_accepts_bare_yes_no():
    assert _decision_from_text("ja") == "yes"
    assert _decision_from_text("Senden!") == "yes"
    assert _decision_from_text("nein") == "no"
    assert _decision_from_text("abbrechen") == "no"


def test_decision_parser_ignores_real_sentences():
    assert _decision_from_text("ja ich brauche noch die Adresse") is None
    assert _decision_from_text("nein danke, lieber morgen früh um acht") is None
    assert _decision_from_text("") is None
