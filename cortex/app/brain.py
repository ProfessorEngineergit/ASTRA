"""Orchestration — ties triage, policy, state machine, agent and channels together.

Inbound flow (third party):
    triage (cheap) → reconcile with policy → AUTO | DEFER | ASK
Owner inbound:
    if a deferred/awaiting thread → STAND DOWN; else converse as personal assistant.
The deferral sweeper (main.py) calls step_in(); Telegram callbacks call resume_after_approval().
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import db
from .agent import generate_reply
from .channels import get_channels
from .config import get_settings
from .memory import get_memory
from .models import get_gateway
from .persona import TRIAGE_INSTRUCTIONS, Register
from .policy import Mode, Sensitivity, TrustTier, reconcile
from .state import Act, Signal, ThreadState, next_state

log = logging.getLogger("astra.brain")

# Autonomy level set from the web settings (DB): how independently ASTRA acts.
#   "ask"       → default policy (asks the owner for sensitive third-party replies)
#   "confident" → no DEFER waiting (acts immediately), still ASKs when sensitive
#   "full"      → acts autonomously: ASK and DEFER are escalated to AUTO
_AUTONOMY = "ask"


def set_autonomy(level: str | None) -> None:
    global _AUTONOMY
    if level in ("ask", "confident", "full"):
        _AUTONOMY = level


def get_autonomy() -> str:
    return _AUTONOMY


def _as_mode(v: str) -> Mode:
    try:
        return Mode(v)
    except ValueError:
        return Mode.DEFER


def _as_sens(v: str) -> Sensitivity:
    try:
        return Sensitivity(v)
    except ValueError:
        return Sensitivity.DETAILS


def _peer(thread_id: str) -> str:
    return thread_id.split(":", 1)[1] if ":" in thread_id else thread_id


# ─── shared helpers ──────────────────────────────────────────────────────────
async def _send_and_record(channel: str, peer: str, thread_id: str, text: str, contact: dict) -> None:
    if not text:
        return
    ok = await get_channels().send(channel, peer, text)
    await db.add_message(thread_id, "assistant", text)
    await db.audit(
        "reply_sent",
        channel=channel,
        thread_id=thread_id,
        contact_id=contact.get("id"),
        detail={"ok": ok, "preview": text[:160]},
    )


def _remember(contact: dict, text: str, *, owner: bool) -> None:
    mem = get_memory()
    if not mem.enabled:
        return
    scope = "owner" if owner else str(contact.get("id"))
    asyncio.create_task(mem.write(text, user_id=scope, metadata={"channel": contact.get("channel")}))


def _transcript(history: list[dict], owner_name: str) -> str:
    lines = []
    for m in history[-8:]:
        who = {"assistant": "ASTRA", "owner": owner_name}.get(m["role"], "Person")
        lines.append(f"{who}: {m['content']}")
    return "\n".join(lines)


# ─── inbound ─────────────────────────────────────────────────────────────────
async def handle_inbound(
    *,
    channel: str,
    sender_handle: str,
    text: str,
    sender_display: str | None = None,
    force_owner: bool | None = None,
) -> None:
    """Process one inbound message. `force_owner` overrides owner detection — used
    by the WAHA ingress for `fromMe` messages (you replying yourself = stand-down)."""
    s = get_settings()
    thread_id = f"{channel}:{sender_handle}"

    # Is the PEER (this thread's other party) the owner himself? → his own DM to ASTRA.
    peer_is_owner = await db.is_owner_handle(channel, sender_handle)
    if channel == "telegram" and str(sender_handle) == str(s.telegram_owner_chat_id):
        peer_is_owner = True
    # Did the OWNER author THIS message? (force_owner=True for WhatsApp `fromMe` self-replies,
    # where the peer is a THIRD party but Bahrian sent the message from his phone.)
    author_is_owner = force_owner if force_owner is not None else peer_is_owner

    contact = await db.resolve_contact(channel, sender_handle)
    if not contact:
        contact = await db.upsert_contact(
            channel, sender_handle, display_name=sender_display,
            trust_tier=0 if peer_is_owner else 3, is_owner=peer_is_owner,
        )
    thread = await db.ensure_thread(thread_id, channel, contact["id"])
    await db.add_message(thread_id, "owner" if author_is_owner else "user", text, sender_handle)

    # ── Owner's own conversation with ASTRA (peer IS the owner) ─────────────────
    if peer_is_owner:
        history = await db.recent_messages(thread_id)
        reply = await generate_reply(
            register=Register.OWNER, contact=contact, thread_id=thread_id, channel=channel,
            history=history, summary=thread.get("summary") or "", max_sensitivity="details",
        )
        await _send_and_record(channel, sender_handle, thread_id, reply, contact)
        await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
        _remember(contact, text, owner=True)
        return

    # ── Owner replied to a third party himself → stand down, stay silent ────────
    if author_is_owner:
        if next_state(ThreadState(thread["state"]), Signal.INBOUND_OWNER).act == Act.STAND_DOWN:
            await db.set_thread_state(thread_id, ThreadState.STANDDOWN.value)
            await db.audit("standdown", channel=channel, thread_id=thread_id, contact_id=contact["id"])
            log.info("Owner stepped in on %s → stand down.", thread_id)
        _remember(contact, text, owner=True)
        return

    # ── Third party → triage + policy ──────────────────────────────────────────
    tier = TrustTier(int(contact["trust_tier"]))
    history = await db.recent_messages(thread_id)
    gw = get_gateway()
    if gw.enabled:
        sysmsg = TRIAGE_INSTRUCTIONS.format(owner=s.astra_owner_name, tier=int(tier))
        triage = await gw.triage(sysmsg, _transcript(history, s.astra_owner_name))
        decision = reconcile(_as_mode(triage.mode), tier, _as_sens(triage.sensitivity))
    else:
        decision = reconcile(Mode.DEFER, tier, Sensitivity.DETAILS)

    await db.audit(
        "classified", channel=channel, thread_id=thread_id, contact_id=contact["id"],
        detail={"mode": decision.mode.value, "ceiling": decision.max_sensitivity.value, "reason": decision.reason},
    )
    _remember(contact, text, owner=False)

    # Autonomy override: a confident/full owner lets ASTRA skip waiting/asking.
    mode = decision.mode
    auto = get_autonomy()
    if auto == "full" and mode in (Mode.DEFER, Mode.ASK):
        mode = Mode.AUTO
        log.info("Autonomy=full → %s escalated to AUTO for %s", decision.mode.value, thread_id)
    elif auto == "confident" and mode == Mode.DEFER:
        mode = Mode.AUTO

    if mode == Mode.AUTO:
        reply = await generate_reply(
            register=Register.THIRD, contact=contact, thread_id=thread_id, channel=channel,
            history=history, summary=thread.get("summary") or "",
            max_sensitivity=decision.max_sensitivity.value,
        )
        await _send_and_record(channel, sender_handle, thread_id, reply, contact)
        cur = await db.get_thread(thread_id)
        if cur and cur["state"] != ThreadState.AWAITING_APPROVAL.value:  # a tool may have asked
            await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)

    elif mode == Mode.DEFER:
        defer_until = datetime.now(timezone.utc) + timedelta(seconds=s.astra_defer_seconds)
        await db.set_thread_state(thread_id, ThreadState.DEFERRED.value, defer_until=defer_until)
        await db.merge_thread_meta(thread_id, {"max_sensitivity": decision.max_sensitivity.value})
        await db.audit("deferred", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                       detail={"defer_seconds": s.astra_defer_seconds})
        log.info("Deferred %s for %ss (waiting for owner).", thread_id, s.astra_defer_seconds)

    else:  # ASK
        await _ask_owner(channel, sender_handle, thread_id, contact, text, decision)


async def _ask_owner(channel: str, peer: str, thread_id: str, contact: dict, text: str, decision) -> None:
    s = get_settings()
    approval_id = await db.create_approval(
        thread_id=thread_id, contact_id=contact["id"], kind="disclosure",
        question=text, payload={"channel": channel},
    )
    await db.set_thread_state(thread_id, ThreadState.AWAITING_APPROVAL.value)
    await db.audit("ask_principal", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                   detail={"approval_id": approval_id})
    name = contact.get("display_name") or contact.get("handle")
    if s.telegram_enabled and s.telegram_owner_chat_id:
        buttons = [
            {"text": "✅ Ja", "callback_data": f"apv:{approval_id}:yes"},
            {"text": "🟡 Nur 'beschäftigt'", "callback_data": f"apv:{approval_id}:busy_only"},
            {"text": "❌ Nein", "callback_data": f"apv:{approval_id}:no"},
        ]
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"🔔 {name} ({channel}) fragt:\n„{text}“\n\nDarf ASTRA antworten?",
            buttons=buttons,
        )
    await _send_and_record(
        channel, peer, thread_id,
        "Einen Moment — ich halte kurz Rücksprache mit Bahrian und melde mich gleich.",
        contact,
    )


# ─── deferral step-in (called by the sweeper) ──────────────────────────────────
async def step_in(thread_id: str) -> None:
    thread = await db.get_thread(thread_id)
    if not thread:
        return
    if next_state(ThreadState(thread["state"]), Signal.DEFER_ELAPSED).act != Act.STEP_IN:
        return  # owner already stood it down / it was answered
    contact = await db.get_contact(thread["contact_id"]) if thread.get("contact_id") else {}
    ceiling = (thread.get("meta") or {}).get("max_sensitivity", "freebusy")
    history = await db.recent_messages(thread_id)
    reply = await generate_reply(
        register=Register.THIRD, contact=contact or {}, thread_id=thread_id, channel=thread["channel"],
        history=history, summary=thread.get("summary") or "", max_sensitivity=ceiling,
        extra_system="Bahrian hat nicht selbst geantwortet. Antworte jetzt stellvertretend, knapp und souverän.",
    )
    await _send_and_record(thread["channel"], _peer(thread_id), thread_id, reply, contact or {})
    await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
    await db.audit("stepin", channel=thread["channel"], thread_id=thread_id,
                   contact_id=thread.get("contact_id"))
    log.info("Stepped in on %s after deferral.", thread_id)


# ─── approval resume (called by Telegram callback) ──────────────────────────────
_RESUME = {
    "yes": (Sensitivity.DETAILS, "Bahrian hat zugestimmt — du darfst die angefragte Information teilen."),
    "busy_only": (Sensitivity.FREEBUSY, "Sag höchstens, dass Bahrian beschäftigt/verplant ist. Keine Details."),
    "no": (Sensitivity.NONE, "Bahrian möchte dazu nichts teilen. Lehne höflich und knapp ab, ohne Details."),
}


async def resume_after_approval(approval: dict, decision: str) -> None:
    thread_id = approval.get("thread_id")
    if not thread_id:
        return
    thread = await db.get_thread(thread_id)
    if not thread or thread["state"] != ThreadState.AWAITING_APPROVAL.value:
        return
    contact = await db.get_contact(thread["contact_id"]) if thread.get("contact_id") else {}
    ceiling, instruction = _RESUME.get(decision, _RESUME["no"])
    history = await db.recent_messages(thread_id)
    reply = await generate_reply(
        register=Register.THIRD, contact=contact or {}, thread_id=thread_id, channel=thread["channel"],
        history=history, summary=thread.get("summary") or "", max_sensitivity=ceiling.value,
        extra_system=instruction,
    )
    await _send_and_record(thread["channel"], _peer(thread_id), thread_id, reply, contact or {})
    await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
    await db.audit("resume", channel=thread["channel"], thread_id=thread_id,
                   contact_id=thread.get("contact_id"), detail={"decision": decision})
