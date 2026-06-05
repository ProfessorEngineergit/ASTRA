"""Async Postgres access layer (asyncpg). Source of truth for contacts, threads,
messages, approvals and the audit log. JSONB columns are transparently dict<->json."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import asyncpg

from .config import get_settings

log = logging.getLogger("astra.db")

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # JSONB <-> Python dict automatically.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            get_settings().database_url, min_size=1, max_size=10, init=_init_conn
        )
        await _migrate()
        log.info("Postgres pool ready.")


async def _migrate() -> None:
    """Idempotent schema additions — safe on both fresh and existing databases."""
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() on startup.")
    return _pool


# ─── Contacts ─────────────────────────────────────────────────────────────────
async def resolve_contact(channel: str, handle: str) -> dict | None:
    row = await pool().fetchrow(
        "SELECT * FROM contacts WHERE channel=$1 AND handle=$2", channel, handle
    )
    return dict(row) if row else None


async def get_contact(contact_id) -> dict | None:
    if not contact_id:
        return None
    row = await pool().fetchrow("SELECT * FROM contacts WHERE id=$1", contact_id)
    return dict(row) if row else None


async def upsert_contact(
    channel: str,
    handle: str,
    *,
    display_name: str | None = None,
    trust_tier: int = 3,
    is_owner: bool = False,
    relationship: str | None = None,
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO contacts (channel, handle, display_name, trust_tier, is_owner, relationship)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (channel, handle) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, contacts.display_name),
                updated_at = now()
        RETURNING *
        """,
        channel, handle, display_name, trust_tier, is_owner, relationship,
    )
    return dict(row)


async def is_owner_handle(channel: str, handle: str) -> bool:
    return bool(
        await pool().fetchval(
            "SELECT is_owner FROM contacts WHERE channel=$1 AND handle=$2", channel, handle
        )
    )


# ─── Threads ──────────────────────────────────────────────────────────────────
async def get_thread(thread_id: str) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM threads WHERE thread_id=$1", thread_id)
    return dict(row) if row else None


async def ensure_thread(thread_id: str, channel: str, contact_id) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO threads (thread_id, channel, contact_id, last_event_at)
        VALUES ($1,$2,$3, now())
        ON CONFLICT (thread_id) DO UPDATE SET last_event_at = now()
        RETURNING *
        """,
        thread_id, channel, contact_id,
    )
    return dict(row)


async def set_thread_state(thread_id: str, state: str, *, defer_until: datetime | None = None) -> None:
    await pool().execute(
        "UPDATE threads SET state=$2, defer_until=$3, last_event_at=now() WHERE thread_id=$1",
        thread_id, state, defer_until,
    )


async def merge_thread_meta(thread_id: str, patch: dict) -> None:
    await pool().execute(
        "UPDATE threads SET meta = meta || $2::jsonb WHERE thread_id=$1", thread_id, patch
    )


async def update_summary(thread_id: str, summary: str) -> None:
    await pool().execute("UPDATE threads SET summary=$2 WHERE thread_id=$1", thread_id, summary)


async def due_deferrals(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = await pool().fetch(
        "SELECT * FROM threads WHERE state='deferred' AND defer_until IS NOT NULL "
        "AND defer_until <= $1",
        now,
    )
    return [dict(r) for r in rows]


# ─── Messages ─────────────────────────────────────────────────────────────────
async def add_message(thread_id: str, role: str, content: str, sender_handle: str | None = None) -> None:
    await pool().execute(
        "INSERT INTO messages (thread_id, role, content, sender_handle) VALUES ($1,$2,$3,$4)",
        thread_id, role, content, sender_handle,
    )


async def recent_messages(thread_id: str, limit: int = 12) -> list[dict]:
    rows = await pool().fetch(
        "SELECT role, content, created_at FROM messages WHERE thread_id=$1 "
        "ORDER BY created_at DESC LIMIT $2",
        thread_id, limit,
    )
    return [dict(r) for r in reversed(rows)]


async def message_count(thread_id: str) -> int:
    return int(await pool().fetchval("SELECT count(*) FROM messages WHERE thread_id=$1", thread_id))


# ─── Approvals (ask_principal) ──────────────────────────────────────────────────
async def create_approval(
    *, thread_id: str | None, contact_id, kind: str, question: str, payload: dict
) -> str:
    return await pool().fetchval(
        """
        INSERT INTO approvals (thread_id, contact_id, kind, question, payload)
        VALUES ($1,$2,$3,$4,$5) RETURNING id
        """,
        thread_id, contact_id, kind, question, payload,
    )


async def get_approval(approval_id: str) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM approvals WHERE id=$1", approval_id)
    return dict(row) if row else None


async def decide_approval(approval_id: str, decision: str) -> dict | None:
    row = await pool().fetchrow(
        """
        UPDATE approvals SET status = CASE WHEN $2='no' THEN 'denied' ELSE 'approved' END,
               decision=$2, decided_at=now()
        WHERE id=$1 AND status='pending' RETURNING *
        """,
        approval_id, decision,
    )
    return dict(row) if row else None


# ─── Briefing / dashboard read models ────────────────────────────────────────
async def inbound_since(since: datetime) -> list[dict]:
    """Third-party inbound messages since `since`, newest last, with contact name."""
    rows = await pool().fetch(
        """
        SELECT m.thread_id, m.content, m.created_at, t.channel,
               COALESCE(c.display_name, m.sender_handle) AS who,
               c.is_owner
        FROM messages m
        JOIN threads t ON t.thread_id = m.thread_id
        LEFT JOIN contacts c ON c.id = t.contact_id
        WHERE m.role = 'user' AND m.created_at >= $1
        ORDER BY m.created_at
        """,
        since,
    )
    return [dict(r) for r in rows]


async def list_threads(limit: int = 25) -> list[dict]:
    rows = await pool().fetch(
        """
        SELECT t.thread_id, t.channel, t.state, t.last_event_at,
               COALESCE(c.display_name, t.thread_id) AS who, c.trust_tier
        FROM threads t LEFT JOIN contacts c ON c.id = t.contact_id
        ORDER BY t.last_event_at DESC LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def pending_approvals() -> list[dict]:
    rows = await pool().fetch(
        "SELECT id, thread_id, question, created_at FROM approvals "
        "WHERE status='pending' ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def recent_audit(limit: int = 30) -> list[dict]:
    rows = await pool().fetch(
        "SELECT ts, event_type, channel, thread_id, detail FROM audit_log "
        "ORDER BY ts DESC LIMIT $1",
        limit,
    )
    return [dict(r) for r in rows]


# ─── Settings KV (runtime toggles from the dashboard) ─────────────────────────
async def get_setting(key: str, default=None):
    val = await pool().fetchval("SELECT value FROM settings WHERE key=$1", key)
    if val is None:
        return default
    return val.get("v", default) if isinstance(val, dict) else val


async def set_setting(key: str, value) -> None:
    await pool().execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
        """,
        key, {"v": value},
    )


# ─── Audit ────────────────────────────────────────────────────────────────────
async def audit(
    event_type: str,
    *,
    actor: str = "astra",
    channel: str | None = None,
    thread_id: str | None = None,
    contact_id=None,
    detail: dict | None = None,
) -> None:
    await pool().execute(
        """
        INSERT INTO audit_log (actor, event_type, channel, thread_id, contact_id, detail)
        VALUES ($1,$2,$3,$4,$5,$6)
        """,
        actor, event_type, channel, thread_id, contact_id, detail or {},
    )
