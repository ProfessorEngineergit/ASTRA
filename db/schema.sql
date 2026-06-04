-- ═══════════════════════════════════════════════════════════════════════════
--  ASTRA cortex schema. Run against the `cortex` database.
--  (db/init/01-cortex-schema.sql loads this on first Postgres init.)
-- ═══════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;       -- for Mem0 long-term memory
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── Contacts + trust tiers (basis of the disclosure policy) ─────────────────
--   tier 0 = you (owner) · 1 = trusted · 2 = known · 3 = unknown
CREATE TABLE IF NOT EXISTS contacts (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    channel       TEXT NOT NULL,                       -- waha | signal | telegram | ...
    handle        TEXT NOT NULL,                       -- phone / username / chat id
    display_name  TEXT,
    relationship  TEXT,                                -- 'mother', 'classmate', ...
    trust_tier    SMALLINT NOT NULL DEFAULT 3 CHECK (trust_tier BETWEEN 0 AND 3),
    is_owner      BOOLEAN NOT NULL DEFAULT FALSE,       -- your own handles → standdown detection
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel, handle)
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts (is_owner) WHERE is_owner;

-- ─── Conversation threads + state machine ────────────────────────────────────
--   state: idle | classifying | deferred | awaiting_approval | answered | standdown
CREATE TABLE IF NOT EXISTS threads (
    thread_id     TEXT PRIMARY KEY,                    -- e.g. 'waha:49123@c.us'
    channel       TEXT NOT NULL,
    contact_id    UUID REFERENCES contacts(id) ON DELETE SET NULL,
    state         TEXT NOT NULL DEFAULT 'idle',
    summary       TEXT,                                -- rolling summary of older turns
    defer_until   TIMESTAMPTZ,                         -- when ASTRA may step in
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Message log (audit + context rebuild) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    role          TEXT NOT NULL,                       -- user | assistant | owner | system
    sender_handle TEXT,
    content       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_id, created_at);

-- ─── Approvals (the "Ich frag mal Bahrian" / confirmation flow) ──────────────
--   kind: disclosure | action     status: pending | approved | denied | expired
CREATE TABLE IF NOT EXISTS approvals (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    thread_id     TEXT REFERENCES threads(thread_id) ON DELETE SET NULL,
    contact_id    UUID REFERENCES contacts(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,
    question      TEXT NOT NULL,                       -- what was asked of you
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,  -- pending tool call / disclosure
    status        TEXT NOT NULL DEFAULT 'pending',
    decision      TEXT,                                -- 'yes' | 'no' | 'busy_only' | ...
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals (status) WHERE status = 'pending';

-- ─── Audit log (everything ASTRA decides / does) ────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL DEFAULT 'astra',       -- astra | owner | system
    event_type    TEXT NOT NULL,                       -- reply_sent | deferred | standdown | ask_principal | tool_call | ...
    channel       TEXT,
    thread_id     TEXT,
    contact_id    UUID,
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts DESC);

-- ─── Seed: you, the owner (edit handles after first run) ─────────────────────
INSERT INTO contacts (channel, handle, display_name, trust_tier, is_owner)
VALUES ('telegram', 'OWNER_PLACEHOLDER', 'Bahrian', 0, TRUE)
ON CONFLICT (channel, handle) DO NOTHING;
