"""Admin auth: hashing, session/CSRF signing, first-run state machine, rate limit."""
from __future__ import annotations

import asyncio

from app.web import auth


def test_hash_and_verify(memdb):
    asyncio.run(auth.set_admin_password("hunter2!"))
    assert asyncio.run(auth.verify_password("hunter2!")) is True
    assert asyncio.run(auth.verify_password("wrong")) is False


def test_first_run_state(memdb):
    assert asyncio.run(auth.has_admin_password()) is False
    asyncio.run(auth.set_admin_password("password1"))
    assert asyncio.run(auth.has_admin_password()) is True


def test_password_from_env(memdb, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ASTRA_ADMIN_PASSWORD", "fromenv12")
    get_settings.cache_clear()
    assert asyncio.run(auth.has_admin_password()) is False
    asyncio.run(auth.ensure_password_from_env())
    assert asyncio.run(auth.verify_password("fromenv12")) is True


def test_session_sign_and_verify(memdb):
    tok = asyncio.run(auth.issue_session())
    assert asyncio.run(auth.valid_session(tok)) is True
    assert asyncio.run(auth.valid_session("tampered")) is False
    assert asyncio.run(auth.valid_session(None)) is False


def test_csrf_sign_and_verify(memdb):
    tok = asyncio.run(auth.issue_csrf())
    assert asyncio.run(auth.valid_csrf(tok)) is True
    assert asyncio.run(auth.valid_csrf("nope")) is False


def test_oauth_state_sign_and_verify(memdb):
    payload = {
        "provider": "google",
        "slug": "google_calendar",
        "installation_id": "default",
    }

    token = asyncio.run(auth.issue_oauth_state(payload))

    assert token.startswith("v1.")
    assert asyncio.run(auth.read_oauth_state(token)) == payload
    assert asyncio.run(auth.read_oauth_state(f"{token}tampered")) is None
    assert asyncio.run(auth.read_oauth_state("legacy-state")) is None


def test_rate_limit(memdb):
    ip = "1.2.3.4"
    for _ in range(auth._MAX_ATTEMPTS):
        assert auth.rate_limited(ip) is False
        auth.record_attempt(ip)
    assert auth.rate_limited(ip) is True
