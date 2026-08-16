from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.policy import Mode, Sensitivity
from app.secretary import plan_for, tone_instruction, with_secretary_header


def test_secretary_auto_replies_during_school_window_for_waha_defer():
    plan = plan_for(
        channel="waha",
        mode=Mode.DEFER,
        max_sensitivity=Sensitivity.FREEBUSY,
        app_settings={"secretary": {"enabled": True}},
        timezone="Europe/Berlin",
        now=datetime(2026, 6, 8, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert plan.mode == Mode.AUTO
    assert plan.in_service_window is True


def test_secretary_email_requires_owner_confirmation():
    plan = plan_for(
        channel="email",
        mode=Mode.AUTO,
        max_sensitivity=Sensitivity.NONE,
        app_settings={"secretary": {"enabled": True}},
        timezone="Europe/Berlin",
        now=datetime(2026, 6, 8, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
    )

    assert plan.mode == Mode.ASK
    assert plan.should_notify_owner is True


def test_secretary_header_intro_then_short_header():
    app_settings = {"secretary": {"intro": "--ASTRA-KI-AGENT--\nIntro", "header": "--ASTRA--"}}

    assert with_secretary_header("Hallo", first_interaction=True, app_settings=app_settings).startswith(
        "--ASTRA-KI-AGENT--"
    )
    assert with_secretary_header("Hallo", first_interaction=False, app_settings=app_settings).startswith(
        "--ASTRA--"
    )


def test_secretary_group_requires_owner_grant_by_default():
    plan = plan_for(
        channel="waha",
        mode=Mode.AUTO,
        max_sensitivity=Sensitivity.FREEBUSY,
        app_settings={"secretary": {"enabled": True}},
        timezone="Europe/Berlin",
        now=datetime(2026, 6, 8, 10, 0, tzinfo=ZoneInfo("Europe/Berlin")),
        is_group=True,
    )

    assert plan.mode == Mode.ASK
    assert plan.reason == "group-action-requires-owner-grant"


def test_secretary_security_watch_uses_thread_local_firm_tone():
    text = tone_instruction(
        {"secretary": {"tone": "warm", "jailbreak_tone": "firm"}},
        {"security_watch": True},
    )

    assert "distanziert" in text


def test_freeform_default_tone_is_used_when_set():
    text = tone_instruction({"secretary": {"default_tone": "freundlich-knapp, trocken"}}, {})
    assert text == "Tonfall (Standard): freundlich-knapp, trocken."


def test_security_watch_beats_default_tone():
    text = tone_instruction(
        {"secretary": {"default_tone": "locker", "jailbreak_tone": "firm"}},
        {"security_watch": True},
    )
    assert "distanziert" in text  # firm wins over the freeform default


def test_disabled_secretary_records_inbound_but_never_replies(memdb, monkeypatch):
    from app import brain, db

    memdb["app_settings"] = {"secretary": {"enabled": False}}
    contact = {"id": "contact-1", "handle": "49123@c.us", "trust_tier": 3,
               "is_owner": False, "relationship": None}
    thread = {"thread_id": "waha:49123@c.us", "state": "idle", "meta": {}, "summary": ""}
    recorded = []

    async def false(*args, **kwargs):
        return False

    async def resolve(*args, **kwargs):
        return contact

    async def ensure(*args, **kwargs):
        return thread

    async def noop(*args, **kwargs):
        return None

    async def add_message(*args, **kwargs):
        recorded.append(args)

    async def should_not_run(*args, **kwargs):
        raise AssertionError("Disabled Secretary attempted to generate or send a reply")

    monkeypatch.setattr(db, "is_owner_handle", false)
    monkeypatch.setattr(db, "resolve_contact", resolve)
    monkeypatch.setattr(db, "ensure_thread", ensure)
    monkeypatch.setattr(db, "merge_thread_meta", noop)
    monkeypatch.setattr(db, "add_message", add_message)
    monkeypatch.setattr(brain, "record_interaction", noop)
    monkeypatch.setattr(brain, "generate_reply", should_not_run)
    monkeypatch.setattr(brain, "_send_and_record", should_not_run)

    asyncio.run(brain.handle_inbound(
        channel="waha", sender_handle="49123@c.us", text="Hallo",
    ))

    assert recorded and recorded[0][1] == "user"
