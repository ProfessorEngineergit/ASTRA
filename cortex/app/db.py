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
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_config (
            plugin_slug TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       JSONB,                       -- encrypted string when is_secret
            is_secret   BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (plugin_slug, key)
        )
        """
    )
    # Compact, structured owner knowledge — one terse triple per row instead of
    # dumping whole markdown files into every prompt. principal_key '' = default
    # owner (W2 fills this in later); no migration needed then.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            id            BIGSERIAL PRIMARY KEY,
            principal_key TEXT NOT NULL DEFAULT '',
            kind          TEXT NOT NULL DEFAULT 'bio',   -- alias|pref|bio|relation|place|note|…
            subject       TEXT NOT NULL DEFAULT '',
            value         TEXT NOT NULL DEFAULT '',
            tags          TEXT[] NOT NULL DEFAULT '{}',
            always_on     BOOLEAN NOT NULL DEFAULT FALSE, -- pinned into every prompt
            weight        REAL NOT NULL DEFAULT 1.0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at  TIMESTAMPTZ
        )
        """
    )
    await _pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_facts_principal ON facts (principal_key, kind)"
    )
    # Multi-tenant seam: one principal per served person. Only the default (Bahrian)
    # exists today; a second user is later a row here, not a refactor. Threads and
    # approvals gain a principal_key ('' = default) so nothing existing needs migrating.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS principals (
            key              TEXT PRIMARY KEY,
            display_name     TEXT NOT NULL DEFAULT '',
            is_default       BOOLEAN NOT NULL DEFAULT FALSE,
            telegram_chat_id TEXT NOT NULL DEFAULT '',
            ha_person_entity TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Cross-integration rules (W4): "when trigger + condition → actions". JSON so
    # they are inspectable and editable. ASTRA-authored rules stay unconfirmed
    # (confirmed_at NULL) until Bahrian approves.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS rules (
            id            BIGSERIAL PRIMARY KEY,
            principal_key TEXT NOT NULL DEFAULT '',
            plugin_slug   TEXT NOT NULL DEFAULT '',
            name          TEXT NOT NULL DEFAULT '',
            enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            trigger       JSONB NOT NULL DEFAULT '{}'::jsonb,
            condition     JSONB NOT NULL DEFAULT '{}'::jsonb,
            actions       JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_by    TEXT NOT NULL DEFAULT 'owner',  -- owner | astra
            confirmed_at  TIMESTAMPTZ,
            last_run_at   TIMESTAMPTZ,
            last_result   TEXT NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Delegated jobs (W7): the small model hands a task to the big brain, with an
    # explicit permission envelope. Steps outside the envelope create approvals.
    await _pool.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id            BIGSERIAL PRIMARY KEY,
            principal_key TEXT NOT NULL DEFAULT '',
            goal          TEXT NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'analyze',   -- analyze | ops | research
            envelope      JSONB NOT NULL DEFAULT '{}'::jsonb, -- hosts, write?, budget
            status        TEXT NOT NULL DEFAULT 'pending',    -- pending|running|done|failed|cancelled
            plan          TEXT NOT NULL DEFAULT '',
            result        TEXT NOT NULL DEFAULT '',
            created_by    TEXT NOT NULL DEFAULT 'astra',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at   TIMESTAMPTZ
        )
        """
    )
    await _pool.execute("ALTER TABLE threads ADD COLUMN IF NOT EXISTS principal_key TEXT NOT NULL DEFAULT ''")
    await _pool.execute("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS principal_key TEXT NOT NULL DEFAULT ''")
    # Seed exactly one default principal from the configured owner name.
    s = get_settings()
    await _pool.execute(
        """
        INSERT INTO principals (key, display_name, is_default, telegram_chat_id)
        VALUES ('', $1, TRUE, $2)
        ON CONFLICT (key) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                telegram_chat_id = EXCLUDED.telegram_chat_id
        """,
        s.astra_owner_name, str(s.telegram_owner_chat_id or ""),
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


async def latest_pending_approval() -> dict | None:
    """Most recently created still-pending approval (full row), or None."""
    row = await pool().fetchrow(
        "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at DESC LIMIT 1"
    )
    return dict(row) if row else None


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


# ─── Principals (multi-tenant seam; only the default is active today) ─────────
DEFAULT_PRINCIPAL = ""


async def default_principal() -> dict:
    row = await pool().fetchrow(
        "SELECT * FROM principals WHERE is_default ORDER BY created_at LIMIT 1"
    )
    if row:
        return dict(row)
    row = await pool().fetchrow("SELECT * FROM principals WHERE key=''")
    return dict(row) if row else {"key": DEFAULT_PRINCIPAL, "display_name": "", "is_default": True}


async def get_principal(key: str) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM principals WHERE key=$1", key)
    return dict(row) if row else None


async def list_principals() -> list[dict]:
    rows = await pool().fetch("SELECT * FROM principals ORDER BY is_default DESC, key")
    return [dict(r) for r in rows]


async def upsert_principal(
    key: str,
    *,
    display_name: str = "",
    telegram_chat_id: str = "",
    ha_person_entity: str = "",
) -> dict:
    row = await pool().fetchrow(
        """
        INSERT INTO principals (key, display_name, telegram_chat_id, ha_person_entity)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (key) DO UPDATE SET
            display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), principals.display_name),
            telegram_chat_id = COALESCE(NULLIF(EXCLUDED.telegram_chat_id, ''), principals.telegram_chat_id),
            ha_person_entity = COALESCE(NULLIF(EXCLUDED.ha_person_entity, ''), principals.ha_person_entity)
        RETURNING *
        """,
        key, display_name, telegram_chat_id, ha_person_entity,
    )
    return dict(row)


async def principal_for_telegram(chat_id: str) -> str:
    """Map a Telegram chat id to a principal key ('' default if unknown)."""
    key = await pool().fetchval(
        "SELECT key FROM principals WHERE telegram_chat_id = $1 AND telegram_chat_id <> ''",
        str(chat_id),
    )
    return key if key is not None else DEFAULT_PRINCIPAL


def _principal_setting_key(key: str, principal: str) -> str:
    return key if not principal else f"principal:{principal}:{key}"


async def get_principal_setting(key: str, principal: str = DEFAULT_PRINCIPAL, default=None):
    """Per-principal setting. The default principal keeps the historical flat keys
    (e.g. app_settings), so nothing existing has to move."""
    return await get_setting(_principal_setting_key(key, principal), default)


async def set_principal_setting(key: str, value, principal: str = DEFAULT_PRINCIPAL) -> None:
    await set_setting(_principal_setting_key(key, principal), value)


# ─── Plugin config (per-plugin key/value, secrets stored as ciphertext) ───────
async def plugin_config_all(slug: str) -> dict[str, dict]:
    """All stored config for one plugin → {key: {"value": ..., "is_secret": bool}}."""
    rows = await pool().fetch(
        "SELECT key, value, is_secret FROM plugin_config WHERE plugin_slug=$1", slug
    )
    out: dict[str, dict] = {}
    for r in rows:
        v = r["value"]
        out[r["key"]] = {
            "value": v.get("v") if isinstance(v, dict) else v,
            "is_secret": r["is_secret"],
        }
    return out


async def plugin_config_set(slug: str, key: str, value, is_secret: bool = False) -> None:
    await pool().execute(
        """
        INSERT INTO plugin_config (plugin_slug, key, value, is_secret, updated_at)
        VALUES ($1,$2,$3,$4, now())
        ON CONFLICT (plugin_slug, key)
        DO UPDATE SET value=EXCLUDED.value, is_secret=EXCLUDED.is_secret, updated_at=now()
        """,
        slug, key, {"v": value}, is_secret,
    )


async def plugin_config_delete(slug: str, key: str) -> None:
    await pool().execute(
        "DELETE FROM plugin_config WHERE plugin_slug=$1 AND key=$2", slug, key
    )


# ─── Facts (compact structured owner knowledge) ───────────────────────────────
async def add_fact(
    kind: str,
    subject: str,
    value: str,
    *,
    tags: list[str] | None = None,
    always_on: bool = False,
    weight: float = 1.0,
    principal_key: str = "",
) -> int:
    """Insert a fact. A non-empty subject replaces any prior fact with the same
    (principal, kind, subject) — so re-teaching an alias updates, never duplicates."""
    tags = [t for t in (tags or []) if t]
    async with pool().acquire() as conn:
        async with conn.transaction():
            if subject.strip():
                await conn.execute(
                    "DELETE FROM facts WHERE principal_key=$1 AND kind=$2 "
                    "AND lower(subject)=lower($3)",
                    principal_key, kind, subject,
                )
            return int(await conn.fetchval(
                """
                INSERT INTO facts (principal_key, kind, subject, value, tags, always_on, weight)
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
                """,
                principal_key, kind, subject, value, tags, always_on, weight,
            ))


async def delete_fact(kind: str, subject: str, *, principal_key: str = "") -> int:
    row = await pool().fetchval(
        "WITH d AS (DELETE FROM facts WHERE principal_key=$1 AND kind=$2 "
        "AND lower(subject)=lower($3) RETURNING 1) SELECT count(*) FROM d",
        principal_key, kind, subject,
    )
    return int(row or 0)


async def all_facts(*, principal_key: str = "") -> list[dict]:
    rows = await pool().fetch(
        "SELECT id, kind, subject, value, tags, always_on, weight "
        "FROM facts WHERE principal_key=$1 ORDER BY kind, subject",
        principal_key,
    )
    return [dict(r) for r in rows]


async def touch_facts(ids: list[int]) -> None:
    if not ids:
        return
    await pool().execute("UPDATE facts SET last_used_at=now() WHERE id = ANY($1)", ids)


# ─── Rules (cross-integration automation) ─────────────────────────────────────
async def add_rule(
    *,
    name: str,
    trigger: dict,
    condition: dict,
    actions: list,
    plugin_slug: str = "",
    principal_key: str = "",
    created_by: str = "owner",
    confirmed: bool = False,
) -> int:
    return int(await pool().fetchval(
        """
        INSERT INTO rules (principal_key, plugin_slug, name, trigger, condition, actions,
                           created_by, confirmed_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7, CASE WHEN $8 THEN now() ELSE NULL END)
        RETURNING id
        """,
        principal_key, plugin_slug, name, trigger, condition, actions, created_by, confirmed,
    ))


async def list_rules(*, principal_key: str | None = None, include_unconfirmed: bool = True) -> list[dict]:
    q = "SELECT * FROM rules"
    conds, args = [], []
    if principal_key is not None:
        args.append(principal_key)
        conds.append(f"principal_key = ${len(args)}")
    if not include_unconfirmed:
        conds.append("confirmed_at IS NOT NULL")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in await pool().fetch(q, *args)]


async def get_rule(rule_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM rules WHERE id=$1", rule_id)
    return dict(row) if row else None


async def active_schedule_rules() -> list[dict]:
    """Enabled, confirmed rules whose trigger is a schedule (the scheduler polls these)."""
    rows = await pool().fetch(
        "SELECT * FROM rules WHERE enabled AND confirmed_at IS NOT NULL "
        "AND trigger->>'type' = 'schedule'"
    )
    return [dict(r) for r in rows]


async def confirm_rule(rule_id: int) -> bool:
    return bool(await pool().fetchval(
        "UPDATE rules SET confirmed_at = now() WHERE id=$1 AND confirmed_at IS NULL RETURNING id",
        rule_id,
    ))


async def mark_rule_run(rule_id: int, result: str) -> None:
    await pool().execute(
        "UPDATE rules SET last_run_at = now(), last_result = $2 WHERE id=$1", rule_id, result[:400]
    )


async def set_rule_enabled(rule_id: int, enabled: bool) -> None:
    await pool().execute("UPDATE rules SET enabled=$2 WHERE id=$1", rule_id, enabled)


async def delete_rule(rule_id: int) -> int:
    return int(await pool().fetchval(
        "WITH d AS (DELETE FROM rules WHERE id=$1 RETURNING 1) SELECT count(*) FROM d", rule_id
    ) or 0)


# ─── Jobs (delegated to the big brain) ────────────────────────────────────────
async def add_job(*, goal: str, kind: str = "analyze", envelope: dict | None = None,
                  principal_key: str = "", created_by: str = "astra") -> int:
    return int(await pool().fetchval(
        "INSERT INTO jobs (principal_key, goal, kind, envelope, created_by) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING id",
        principal_key, goal, kind, envelope or {}, created_by,
    ))


async def get_job(job_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM jobs WHERE id=$1", job_id)
    return dict(row) if row else None


async def list_jobs(*, principal_key: str | None = None, limit: int = 20) -> list[dict]:
    if principal_key is None:
        rows = await pool().fetch("SELECT * FROM jobs ORDER BY created_at DESC LIMIT $1", limit)
    else:
        rows = await pool().fetch(
            "SELECT * FROM jobs WHERE principal_key=$1 ORDER BY created_at DESC LIMIT $2",
            principal_key, limit)
    return [dict(r) for r in rows]


async def update_job(job_id: int, *, status: str | None = None, plan: str | None = None,
                     result: str | None = None) -> None:
    await pool().execute(
        """
        UPDATE jobs SET
            status = COALESCE($2, status),
            plan   = COALESCE($3, plan),
            result = COALESCE($4, result),
            finished_at = CASE WHEN $2 IN ('done','failed','cancelled') THEN now() ELSE finished_at END
        WHERE id=$1
        """,
        job_id, status, plan, result,
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
