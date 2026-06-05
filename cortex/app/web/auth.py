"""Admin authentication: first-run password, bcrypt, signed session cookies.

Password resolution order:
  1. settings['admin_password_hash'] (set via the first-run wizard or a prior save)
  2. ASTRA_ADMIN_PASSWORD env  → hashed and stored on first boot
  3. neither → FIRST-RUN: the web UI forces a setup screen before anything else
"""
from __future__ import annotations

import logging
import secrets
import time

import bcrypt
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .. import db
from ..config import get_settings

log = logging.getLogger("astra.web.auth")

COOKIE_NAME = "astra_session"
CSRF_COOKIE = "astra_csrf"
SESSION_TTL = 7 * 24 * 3600  # 7 days

_serializer: URLSafeTimedSerializer | None = None

# naive in-memory login rate limiter: ip -> list[timestamps]
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW = 300  # 5 min


# ─── password hashing ─────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _check(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:  # noqa: BLE001
        return False


async def _stored_hash() -> str | None:
    return await db.get_setting("admin_password_hash", None)


async def ensure_password_from_env() -> None:
    """On boot: if no hash stored yet but ASTRA_ADMIN_PASSWORD is set, store it."""
    if await _stored_hash():
        return
    env_pw = get_settings().astra_admin_password
    if env_pw:
        await db.set_setting("admin_password_hash", hash_password(env_pw))
        log.info("Admin password initialised from ASTRA_ADMIN_PASSWORD.")


async def has_admin_password() -> bool:
    return bool(await _stored_hash())


async def set_admin_password(pw: str) -> None:
    await db.set_setting("admin_password_hash", hash_password(pw))
    await db.audit("admin_password_set", actor="owner")


async def verify_password(pw: str) -> bool:
    h = await _stored_hash()
    return bool(h) and _check(pw, h)


# ─── session signing ──────────────────────────────────────────────────────────
async def _serializer_inst() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        secret = await db.get_setting("session_secret", None)
        if not secret:
            secret = secrets.token_hex(32)
            await db.set_setting("session_secret", secret)
        _serializer = URLSafeTimedSerializer(secret, salt="astra-admin-session")
    return _serializer


async def issue_session() -> str:
    s = await _serializer_inst()
    return s.dumps({"admin": True, "iat": int(time.time())})


async def valid_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        s = await _serializer_inst()
        data = s.loads(token, max_age=SESSION_TTL)
        return bool(data.get("admin"))
    except (BadSignature, SignatureExpired):
        return False
    except Exception:  # noqa: BLE001
        return False


# ─── CSRF (signed token in a cookie, mirrored in a hidden form field) ─────────
async def issue_csrf() -> str:
    s = await _serializer_inst()
    return s.dumps({"csrf": secrets.token_hex(8)})


async def valid_csrf(token: str | None) -> bool:
    if not token:
        return False
    try:
        s = await _serializer_inst()
        s.loads(token, max_age=SESSION_TTL)
        return True
    except Exception:  # noqa: BLE001
        return False


# ─── rate limiting ────────────────────────────────────────────────────────────
def rate_limited(ip: str) -> bool:
    now = time.time()
    hist = [t for t in _attempts.get(ip, []) if now - t < _WINDOW]
    _attempts[ip] = hist
    return len(hist) >= _MAX_ATTEMPTS


def record_attempt(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())


# ─── FastAPI dependency ───────────────────────────────────────────────────────
async def require_admin(request: Request) -> bool:
    """Guard for /admin* routes. Redirects (303) to setup or login as needed."""
    if not await has_admin_password():
        raise HTTPException(status_code=303, headers={"Location": "/admin/setup"})
    token = request.cookies.get(COOKIE_NAME)
    if not await valid_session(token):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return True
