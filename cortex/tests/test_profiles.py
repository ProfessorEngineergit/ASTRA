from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    from app import config
    config.get_settings.cache_clear()  # type: ignore[attr-defined]
    from app import knowledge
    importlib.reload(knowledge)
    return knowledge


def test_upsert_creates_profile_with_handles_and_tone(brain):
    rel = brain.upsert_person_profile("Lorenzo Bay-Mueller", {
        "relationship": "Freund", "trust_tier": "2",
        "tone": "locker, viel Insider-Humor", "phone": "+49 178 3201644",
    })
    assert rel == "people/lorenzo_bay_mueller.md"
    txt = brain.read_file(rel)
    assert brain.person_tone(txt) == "locker, viel Insider-Humor"
    assert brain.parse_person_handles(txt)["phone"] == ["+49 178 3201644"]


def test_inbound_matches_profile_across_phone_channels(brain):
    brain.upsert_person_profile("Lorenzo", {"phone": "+49 178 3201644", "tone": "frech"})
    # WhatsApp arrives as a JID, Signal as +E.164 — both normalise to the digits.
    assert brain.person_file_for("waha", "491783201644@c.us")["tone"] == "frech"
    assert brain.person_file_for("signal", "+49 178 3201644") is not None
    assert brain.person_file_for("waha", "490000000000") is None


def test_email_matches_only_email_channel(brain):
    brain.upsert_person_profile("Frau Schmidt", {"email": "schmidt@schule.de", "tone": "formell"})
    assert brain.person_file_for("email", "schmidt@schule.de")["tone"] == "formell"
    assert brain.person_file_for("waha", "schmidt@schule.de") is None


def test_update_preserves_notes_and_merges(brain):
    rel = brain.upsert_person_profile("Lorenzo", {"phone": "+49 178 3201644",
                                                  "tone": "locker", "notes": "Spätschreiber."})
    brain.upsert_person_profile("Lorenzo", {"tone": "trockener", "notes": "Keine Sprachnachrichten."})
    txt = brain.read_file(rel)
    assert brain.person_tone(txt) == "trockener"          # tone updated
    assert "Spätschreiber." in txt and "Keine Sprachnachrichten." in txt  # notes kept
    assert brain.parse_person_handles(txt)["phone"] == ["+49 178 3201644"]  # handle kept


def test_placeholder_tone_is_ignored(brain):
    rel = brain.create_person("Neu")            # seed template with placeholder Ton
    assert brain.person_tone(brain.read_file(rel)) == ""
