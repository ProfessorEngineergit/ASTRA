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
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import (abuse, cards, db, digest, knowledge, model_choice, models, moderation,
               moderation_gate, outbox, prompts, smart_reply, styles, usage)
from .agent import TAIL_MARK, generate_reply
from .channels import get_channels
from .config import get_settings
from .context_ledger import record_interaction
from .memory import get_memory
from .models import get_gateway
from .persona import TRIAGE_INSTRUCTIONS, Register
from .policy import Decision, Mode, Sensitivity, TrustTier, reconcile
from .secretary import (
    CHANNEL_LABELS, SECRETARY_CHANNELS, channel_enabled, contact_rule_for, is_group_context,
    plan_for, resolve_service_status, secretary_settings, shadow_enabled, tone_instruction,
    unknown_sender_action, with_secretary_header,
)
from .security import check_inbound, check_outbound
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
async def _app_settings() -> dict:
    return await db.get_setting("app_settings", {}) or {}


def _secretary_system(
    channel: str,
    plan_reason: str = "",
    *,
    app_settings: dict | None = None,
    thread_meta: dict | None = None,
    handle: str = "",
    card: dict | None = None,
) -> str:
    if channel not in SECRETARY_CHANNELS:
        return ""
    person = knowledge.person_file_for(channel, handle) if handle else None
    meta = thread_meta or {}
    # Vorrang beim Ton: Eskalation der Moderation (bestimmt/überheblich) > Stil der
    # Kontaktkarte > `Ton:` im Profil > globaler Standard.
    if meta.get("tone_override") or meta.get("security_watch"):
        tone = tone_instruction(app_settings, thread_meta)
    elif card and card.get("style"):
        tone = styles.instruction(card["style"])
    elif person and person.get("tone"):
        tone = f"Umgangston mit dieser Person (verbindlich): {person['tone']}."
    else:
        tone = tone_instruction(app_settings, thread_meta)
    group_note = ""
    if meta.get("is_group"):
        role = ((card or {}).get("group") or {}).get("role", "assistant")
        group_note = (
            "Dieser Thread ist ein Gruppenchat, in dem du angesprochen wurdest. Antworte kurz und nur "
            "auf das, was dich betrifft; führe keine Aktionen aus und triff keine Zusagen, wenn Bahrian "
            "das nicht für genau diese Gruppe freigegeben hat. "
            + ("Du bist hier als Moderator freigegeben: ruhig ermahnen statt bloßstellen. "
               if role == "moderator" else "")
        )
    person_block = ""
    if person:
        person_block = (
            "\n\nProfil dieser Person (nutze es fuer Ton, Beziehung und was du teilen darfst):\n"
            + person["content"][:1600]
        )
    card_block = " ".join(x for x in (cards.instruction_block(card), cards.share_prompt(card)) if x)
    # Notizkarte NUR dieser Person/Gruppe (nie die eines anderen) — als Daten markiert.
    try:
        capsule = digest.prompt_block(digest.capsule_for(channel, handle, group=bool(meta.get("is_group"))))
    except Exception:  # noqa: BLE001
        capsule = ""
    if capsule:
        card_block = (card_block + " " if card_block else "") + capsule
    head = (
        prompts.get("secretary_core")
        + f"{tone} {group_note}"
        f"{card_block + ' ' if card_block else ''}"
        f"Policy-Grund: {plan_reason or 'secretary'}."
        f"{person_block}"
    )
    # Stil-Erinnerung NACH dem Gesprächsverlauf: sonst richtet sich das Modell nach dem Ton seiner eigenen
    # früheren Antworten — ein neu gewählter Stil (z. B. „überheblich“) käme mitten im Chat nicht an.
    reminder = ("Stil-Erinnerung (verbindlich für DIESE Antwort): " + tone.strip() +
                " Frühere Antworten in diesem Chat können in einem anderen Stil geschrieben sein — richte dich "
                "NICHT nach ihnen, sondern ausschließlich nach diesem Stil.")
    return head + TAIL_MARK + reminder


async def _send_and_record(
    channel: str,
    peer: str,
    thread_id: str,
    text: str,
    contact: dict,
    *,
    max_sensitivity: str = "none",
    moderate: bool = True,
) -> None:
    """Senden + protokollieren. `moderate=False` nur für die festen Moderationsantworten
    (die enthalten bewusst Nummern wie die Telefonseelsorge und dürfen nicht geschwärzt werden)."""
    if not text:
        return
    if channel in SECRETARY_CHANNELS:
        thread = await db.get_thread(thread_id)
        meta = (thread or {}).get("meta") or {}
        appset = await _app_settings()
        text = with_secretary_header(
            text,
            first_interaction=not bool(meta.get("secretary_announced")),
            app_settings=appset,
        )
        verdict = check_outbound(text, channel=channel, max_sensitivity=max_sensitivity)
        if not verdict.ok:
            await db.audit(
                "security_blocked_outbound",
                channel=channel,
                thread_id=thread_id,
                contact_id=contact.get("id"),
                detail={"reasons": verdict.reasons, "preview": text[:160]},
            )
            text = with_secretary_header(
                "Ich kann diese Antwort so nicht sicher senden. Ich frage Bahrian direkt.",
                first_interaction=not bool(meta.get("secretary_announced")),
                app_settings=appset,
            )
        await db.merge_thread_meta(thread_id, {"secretary_announced": True})
        if moderate:
            out = moderation.moderate_outbound(text, app_settings=appset, third_party=True,
                                               recipient_handles=(peer,))
            if out.reasons:
                await db.audit("moderation_outbound", channel=channel, thread_id=thread_id,
                               contact_id=contact.get("id"),
                               detail={"reasons": list(out.reasons), "blocked": out.blocked,
                                       "preview": text[:160]})
            if out.blocked:
                text = with_secretary_header(
                    out.text, first_interaction=False, app_settings=appset)
            else:
                text = out.text
    # Antworten an Dritte lassen den Chat auf Bahrians Handy „ungelesen“ (Einstellung, Standard an).
    keep_unread = False
    if channel == "waha" and not contact.get("is_owner"):
        try:
            keep_unread = bool(smart_reply.settings(await _app_settings())["keep_unread"])
        except Exception:  # noqa: BLE001
            keep_unread = False
    ok = await (get_channels().send(channel, peer, text, keep_unread=True) if keep_unread
                else get_channels().send(channel, peer, text))
    await db.add_message(thread_id, "assistant", text)
    try:
        thread = await db.get_thread(thread_id)
        meta = (thread or {}).get("meta") or {}
        await record_interaction(
            channel=channel,
            thread_id=thread_id,
            handle=peer,
            role="assistant",
            text=text,
            display=contact.get("display_name") or contact.get("handle"),
            meta={
                **meta,
                "relationship": contact.get("relationship"),
                "trust_tier": contact.get("trust_tier"),
                "is_owner": contact.get("is_owner"),
            },
        )
    except Exception:  # noqa: BLE001
        log.debug("Secretary context ledger write failed for outbound %s", thread_id, exc_info=True)
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
    thread_meta: dict | None = None,
) -> None:
    """Process one inbound message. `force_owner` overrides owner detection — used
    by the WAHA ingress for `fromMe` messages (you replying yourself = stand-down)."""
    s = get_settings()
    # Echo der eigenen Antwort (ASTRA sendet über DEINEN Account, WhatsApp meldet es als „fromMe“ zurück):
    # kein Eingreifen von Bahrian — sonst würde sich ASTRA bei jeder Antwort selbst unterbrechen.
    if force_owner and channel in ("waha", "signal") and outbox.is_echo(text):
        log.debug("Echo of own message on %s ignored.", channel)
        return
    thread_meta = dict(thread_meta or {})
    thread_meta.setdefault("source_channel", channel)
    if is_group_context(channel, sender_handle, thread_meta):
        thread_meta["is_group"] = True
    thread_id = f"{channel}:{sender_handle}"

    # Is the PEER (this thread's other party) the owner himself? → his own DM to ASTRA.
    peer_is_owner = await db.is_owner_handle(channel, sender_handle)
    if channel == "telegram" and str(sender_handle) == str(s.telegram_owner_chat_id):
        peer_is_owner = True
    # Did the OWNER author THIS message? (force_owner=True for WhatsApp `fromMe` self-replies,
    # where the peer is a THIRD party but Bahrian sent the message from his phone.)
    author_is_owner = force_owner if force_owner is not None else peer_is_owner

    # ── Karten: Gruppen sind wie Benutzer — ohne Freigabe existieren sie nicht ────────
    card: dict | None = None
    is_group = bool(thread_meta.get("is_group"))
    # In Gruppen zählt der TEILNEHMER (nicht die Gruppen-Id) für Rate-Limit und Moderation,
    # sonst würde ein Störer die ganze Gruppe stummschalten.
    actor = (thread_meta.get("participant_handle") if is_group else None) or sender_handle
    if not peer_is_owner:
        card = await cards.find_card(channel, sender_handle, kind="group" if is_group else "person")
        if is_group:
            if card is None:
                if not author_is_owner:
                    await _ask_new_group(channel, sender_handle, sender_display, text)
                return
            if card.get("rule") == "block":
                await db.audit("group_blocked", channel=channel, detail={"group": sender_handle})
                return
            if actor != sender_handle:
                pcard = await cards.find_card(channel, actor, kind="person")
                if pcard and pcard.get("rule") == "block":
                    return

    contact = await db.resolve_contact(channel, sender_handle)
    if not contact:
        contact = await db.upsert_contact(
            channel, sender_handle, display_name=sender_display,
            trust_tier=0 if peer_is_owner else 3, is_owner=peer_is_owner,
        )
    contact_meta = {
        "relationship": contact.get("relationship"),
        "trust_tier": contact.get("trust_tier"),
        "is_owner": contact.get("is_owner"),
    }
    thread = await db.ensure_thread(thread_id, channel, contact["id"])
    if thread_meta:
        await db.merge_thread_meta(thread_id, thread_meta)
        thread = {**thread, "meta": {**(thread.get("meta") or {}), **thread_meta}}
    await db.add_message(thread_id, "owner" if author_is_owner else "user", text, sender_handle)
    try:
        await record_interaction(
            channel=channel,
            thread_id=thread_id,
            handle=sender_handle,
            role="owner" if author_is_owner else "user",
            text=text,
            display=sender_display,
            meta={**(thread.get("meta") or {}), **contact_meta},
        )
    except Exception:  # noqa: BLE001
        log.debug("Secretary context ledger write failed for inbound %s", thread_id, exc_info=True)

    # ── Owner's own conversation with ASTRA (peer IS the owner) ─────────────────
    if peer_is_owner:
        # Deterministische Chat-Befehle: /modell … (pro Chat) und /verbrauch … — ohne LLM/Token.
        snapshot = models.model_config_snapshot()
        is_cmd, new_pick, cmd_reply = model_choice.parse_command(text, snapshot)
        if is_cmd:
            meta_now = thread.get("meta") or {}
            if new_pick is not None:
                await db.merge_thread_meta(thread_id, {"model_pick": new_pick})
                cmd_reply = f"{cmd_reply} Aktiv: {model_choice.label(new_pick, snapshot)}"
            elif not cmd_reply:
                cmd_reply = ("Aktuell: " + model_choice.label(meta_now.get("model_pick"), snapshot) +
                             "\nWechseln: /modell schwer · klein · mittel · code · auto")
            await _send_and_record(channel, sender_handle, thread_id, cmd_reply, contact, moderate=False)
            return
        if (ucmd := usage.parse_usage_command(text)) is not None:
            report = await usage.report(*ucmd, tz=get_settings().astra_timezone)
            await _send_and_record(channel, sender_handle, thread_id, report, contact, moderate=False)
            return
        history = await db.recent_messages(thread_id)
        reply = await generate_reply(
            register=Register.OWNER, contact=contact, thread_id=thread_id, channel=channel,
            history=history, summary=thread.get("summary") or "", max_sensitivity="details",
            model_pick=model_choice.clean_pick((thread.get("meta") or {}).get("model_pick")) or None,
            chat_id=thread_id,
        )
        await _send_and_record(channel, sender_handle, thread_id, reply, contact)
        await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
        _remember(contact, text, owner=True)
        return

    # ── Owner replied to a third party himself → stand down, stay silent ────────
    if author_is_owner:
        if next_state(ThreadState(thread["state"]), Signal.INBOUND_OWNER).act == Act.STAND_DOWN:
            await db.set_thread_state(thread_id, ThreadState.STANDDOWN.value)
            # Auch das laufende Gespräch/die Ruhephase ist damit vorbei: beim nächsten Mal wartet ASTRA wieder.
            await db.merge_thread_meta(thread_id, smart_reply.meta_after_owner())
            await db.audit("standdown", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                           detail={"was": thread["state"]})
            log.info("Owner stepped in on %s → stand down.", thread_id)
        _remember(contact, text, owner=True)
        return

    # ── Third party → contact rules ────────────────────────────────────────────
    appset_pre = await _app_settings()
    if not channel_enabled(appset_pre, channel):
        await db.audit("secretary_disabled", channel=channel, thread_id=thread_id,
                       contact_id=contact["id"])
        log.info("Secretary disabled for %s — recorded inbound message without replying.", channel)
        return
    contact_rule = (card or {}).get("rule") or contact_rule_for(appset_pre, channel, sender_handle)

    # Explicit block rule → silent, cheapest possible path.
    if contact_rule == "block":
        log.info("Contact %s on %s blocked by rule.", sender_handle, channel)
        await db.audit("contact_blocked", channel=channel, thread_id=thread_id,
                       contact_id=contact["id"])
        return

    # Emojis, GIF-/Link-Nachrichten, „ok“, „danke“, Lachen: keine Antwort, kein Ratenlimit-Zähler, keine Rückfrage
    # bei unbekannten Absendern — kostet nichts und wirkt nicht wie „gelesen und ignoriert“ durch einen Bot.
    if not thread_meta.get("is_group") and smart_reply.settings(appset_pre)["ignore_noise"]:
        early_kind = smart_reply.classify(text)
        ignore_it = early_kind == smart_reply.NOISE
        if early_kind == smart_reply.THANKS:
            # „ja“/„gerne“/„ok“ als Antwort auf eine Rückfrage von ASTRA ist keine Höflichkeitsfloskel.
            meta_now = thread.get("meta") or {}
            running = float(meta_now.get("smart_until") or 0) > time.time() or float(meta_now.get("quiet_until") or 0) > time.time()
            ignore_it = not (running and smart_reply.awaiting_answer(await db.recent_messages(thread_id)))
        if ignore_it:
            await db.audit("smart_ignored", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                           detail={"kind": early_kind, "reason": "smart-noise" if early_kind == smart_reply.NOISE
                                   else "smart-ack"})
            return

    # Cheap abuse / rate guards BEFORE any LLM or triage cost. The owner has
    # already been handled above, so this only ever clamps third parties.
    sec_pre = (appset_pre.get("secretary") or {})
    # Inhalte (Code-Farming, Sexuelles …) prüft ab hier die Moderation — abuse.check
    # bleibt für Rate-Limits zuständig (leerer Text = nur zählen).
    av = abuse.check(
        channel, actor, "",
        short_max=int(sec_pre.get("rate_short_max") or 8),
        long_max=int(sec_pre.get("rate_long_max") or 60),
    )
    if not av.ok:
        await db.audit("abuse_blocked", channel=channel, thread_id=thread_id,
                       contact_id=contact["id"], detail={"kind": av.kind})
        log.info("Abuse guard (%s) clamped %s on %s.", av.kind, sender_handle, channel)
        # Don't insult a trusted contact who simply texted fast — just stop spending.
        # Code-farming / sexual content stays a hard block for everyone.
        trusted = contact_rule in ("allow", "direct") or int(contact.get("trust_tier") or 3) <= 1
        response = "" if (av.kind == "rate" and trusted) else av.response
        if response:
            await _send_and_record(channel, sender_handle, thread_id, response,
                                   contact, max_sensitivity="none")
        return

    # ── Content-Moderation: Eingang prüfen, BEVOR ein Token bezahlt wird ────────
    tokens = cards.mention_tokens(s.astra_owner_name, card,
                                  [thread_meta.get("own_id"), s.signal_phone_number])
    reply_to_us = bool(thread_meta.get("reply_to_us") or thread_meta.get("mentioned_us"))
    # Die Vertrauensstufe der Karte gilt auch für die Moderation (Kumpels sammeln keine Strikes).
    gate_contact = {**contact, "trust_tier": card["trust_tier"]} if card and not is_group else contact
    gate = await moderation_gate.gate_inbound(
        channel=channel, handle=actor, thread_id=thread_id, contact=gate_contact,
        text=text, app_settings=appset_pre)
    if gate.escalation and gate.escalation.style != "normal":
        # Die Eskalation schaltet den Stil dieser Person (bestimmt → überheblich).
        thread_meta_patch = {"tone_override": gate.escalation.style, "security_watch": True,
                             "security_reasons": list(gate.verdict.categories)}
        await db.merge_thread_meta(thread_id, thread_meta_patch)
        thread = {**thread, "meta": {**(thread.get("meta") or {}), **thread_meta_patch}}
    if gate.stop:
        log.info("Moderation stopped %s on %s (%s).", actor, channel,
                 "muted" if gate.muted else gate.verdict.action)
        speak = bool(gate.verdict.response) and not gate.muted and channel != "email"
        if is_group:   # in Gruppen nur antworten, wenn ASTRA überhaupt angesprochen wurde
            speak = speak and cards.group_decision(card, text, tokens=tokens,
                                                   reply_to_us=reply_to_us).respond
        if speak:
            await _send_and_record(channel, sender_handle, thread_id, gate.verdict.response,
                                   contact, max_sensitivity="none", moderate=False)
        return

    # ── Gruppen: spricht ASTRA hier überhaupt? (Trigger/Rolle der Gruppenkarte) ────
    if is_group:
        gd = cards.group_decision(card, text, tokens=tokens, reply_to_us=reply_to_us,
                                  flagged=gate.verdict.flagged)
        if not gd.respond:
            log.debug("Group %s: not addressed (%s) — listening only.", sender_handle, gd.reason)
            return
        if gd.moderating:
            await _moderate_group(channel, sender_handle, thread_id, contact, text, card,
                                  thread, appset_pre)
            return

    # The global Secretary state is resolved before contact-specific automation.
    # In Auto mode, EduPage is authoritative; Calendar/static school time are
    # fallbacks. This also prevents a `direct` contact from receiving a reply
    # after Bahrian's actual school day has ended.
    # Eigenes Zeitfenster der Karte: „nie“ schlägt alles, „immer“ schlägt den Secretary-Plan
    # (nicht aber den Master-Schalter — der stoppt schon weiter oben).
    card_active = cards.active_state(card, datetime.now(ZoneInfo(s.astra_timezone)))
    if card_active is False:
        await db.audit("card_inactive", channel=channel, thread_id=thread_id,
                       contact_id=contact["id"], detail={"card": (card or {}).get("key")})
        return
    service_status = await resolve_service_status(appset_pre, s.astra_timezone)
    if not service_status.active and card_active is not True:
        await db.audit(
            "secretary_inactive",
            channel=channel,
            thread_id=thread_id,
            contact_id=contact["id"],
            detail={"reason": service_status.reason, "source": service_status.source},
        )
        log.info(
            "Secretary inactive on %s (%s/%s) — recorded without replying.",
            thread_id,
            service_status.source,
            service_status.reason,
        )
        return

    if contact_rule is None and card is None:
        # Unknown sender — apply the configured default action
        action = unknown_sender_action(appset_pre)
        if action == "block":
            log.info("Unknown sender %s on %s — blocked by unknown_sender_action.", sender_handle, channel)
            await db.audit("contact_blocked_unknown", channel=channel, thread_id=thread_id,
                           contact_id=contact["id"])
            return
        if action == "ask_owner":
            log.info("Unknown sender %s on %s — asking owner.", sender_handle, channel)
            name = contact.get("display_name") or sender_handle
            if s.telegram_enabled and s.telegram_owner_chat_id:
                approval_id = await db.create_approval(
                    thread_id=thread_id, contact_id=contact["id"], kind="unknown_sender",
                    question=text, payload={"channel": channel, "sender": sender_handle},
                )
                await db.set_thread_state(thread_id, ThreadState.AWAITING_APPROVAL.value)
                buttons = [
                    {"text": "✅ Erlauben", "callback_data": f"apv:{approval_id}:yes"},
                    {"text": "📋 Nur einmal", "callback_data": f"apv:{approval_id}:busy_only"},
                    {"text": "🚫 Blockieren", "callback_data": f"apv:{approval_id}:no"},
                ]
                channel_label = {"waha": "WhatsApp", "signal": "Signal", "slack": "Slack",
                                 "email": "Mail"}.get(channel, channel)
                await get_channels().send_telegram(
                    s.telegram_owner_chat_id,
                    f"\U0001f514 Unbekannter Kontakt auf {channel_label}: {name}\n„{text[:800]}“\n\n"
                    "Regel festlegen — gilt ab sofort für diesen Kontakt:",
                    buttons=buttons,
                )
            await db.audit("contact_ask_unknown", channel=channel, thread_id=thread_id,
                           contact_id=contact["id"], detail={"sender": sender_handle})
            return
        # action == "policy" → fall through to normal triage
    elif contact_rule == "ask":
        await _ask_owner(channel, sender_handle, thread_id, contact, text,
                         type("_D", (), {"mode": Mode.ASK, "max_sensitivity": Sensitivity.FREEBUSY,
                                         "reason": "contact-rule-ask"})())
        return
    elif contact_rule == "direct":
        # Force AUTO regardless of triage
        appset_for_reply = appset_pre
        history = await db.recent_messages(thread_id)
        reply = await generate_reply(
            register=Register.THIRD, contact=contact, thread_id=thread_id, channel=channel,
            history=history, summary=thread.get("summary") or "",
            max_sensitivity=Sensitivity.DETAILS.value,
            model_pick=(card or {}).get("model") or None,
            extra_system=_secretary_system(channel, "contact-rule-direct",
                                           app_settings=appset_for_reply,
                                           thread_meta=thread.get("meta") or {},
                                           handle=sender_handle, card=card),
        )
        await _send_and_record(channel, sender_handle, thread_id, reply, contact,
                               max_sensitivity=Sensitivity.DETAILS.value)
        await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
        return
    # contact_rule == "allow" → fall through to normal triage (policy decides mode)

    # ── Third party → triage + policy ──────────────────────────────────────────
    tier = TrustTier(int(card["trust_tier"]) if card else int(contact["trust_tier"]))
    history = await db.recent_messages(thread_id)

    # ── Smart-Antwort: wann warten, antworten oder schweigen? (kein Token, bevor es sich lohnt) ─────────
    smart_kind, smart_instant = "", False
    if not is_group:
        scfg = smart_reply.settings(appset_pre)
        smart_on = smart_reply.applies(secretary_settings(appset_pre), channel, {"rule": contact_rule}, scfg)
        smart_kind = smart_reply.classify(text)
        now_ts = time.time()
        verdict = smart_reply.decide(smart_kind, smart=smart_on, ignore_noise=scfg["ignore_noise"],
                                     state=thread["state"], meta=thread.get("meta") or {}, now=now_ts,
                                     awaiting=smart_reply.awaiting_answer(history))
        if verdict.action == "ignore":
            await db.audit("smart_ignored", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                           detail={"kind": smart_kind, "reason": verdict.reason})
            log.info("Smart: %s on %s not answered (%s).", smart_kind, thread_id, verdict.reason)
            return
        if verdict.action == "wait":
            meta_now = thread.get("meta") or {}
            du = thread.get("defer_until")
            waiting = thread["state"] == ThreadState.DEFERRED.value and du is not None and (
                du.timestamp() if hasattr(du, "timestamp") else float(du)) > now_ts
            kind_now = smart_reply.merge_kind(str(meta_now.get("smart_kind") or ""), smart_kind) if waiting else smart_kind
            patch = {"smart_kind": kind_now}
            if not waiting:                        # Wartezeit läuft ab der ERSTEN Nachricht, nicht ab der letzten
                ceiling = cards.share_ceiling(card) or reconcile(Mode.AUTO, tier, Sensitivity.DETAILS).max_sensitivity.value
                patch["max_sensitivity"] = ceiling
                until = datetime.now(timezone.utc) + timedelta(seconds=scfg["wait_seconds"])
                await db.set_thread_state(thread_id, ThreadState.DEFERRED.value, defer_until=until)
            await db.merge_thread_meta(thread_id, patch)
            await db.audit("deferred", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                           detail={"defer_seconds": scfg["wait_seconds"], "smart": verdict.reason, "kind": kind_now})
            log.info("Smart: waiting %ss on %s (%s).", scfg["wait_seconds"], thread_id, kind_now)
            return
        smart_instant = smart_on and verdict.reason.startswith("smart-") and verdict.action == "reply"

    gw = get_gateway()
    if gw.enabled:
        sysmsg = prompts.render("triage", owner=s.astra_owner_name, tier=int(tier))
        with usage.tag(purpose="triage", channel=channel, thread_id=thread_id,
                       contact=str(contact.get("display_name") or sender_handle),
                       third_party=True):
            triage = await gw.triage(sysmsg, _transcript(history, s.astra_owner_name))
        decision = reconcile(_as_mode(triage.mode), tier, _as_sens(triage.sensitivity))
    else:
        decision = reconcile(Mode.DEFER, tier, Sensitivity.DETAILS)

    await db.audit(
        "classified", channel=channel, thread_id=thread_id, contact_id=contact["id"],
        detail={"mode": decision.mode.value, "ceiling": decision.max_sensitivity.value, "reason": decision.reason},
    )
    _remember(contact, text, owner=False)

    # Kartenfreigabe („Verfügbarkeit: nur frei/belegt“) ist die Obergrenze — nach oben UND unten.
    # Wünscht die Anfrage mehr als erlaubt, fragt ASTRA Bahrian (Freigabe-Schleife, „Immer“/„Nie“).
    ceiling = cards.share_ceiling(card)
    card_ask = False
    if ceiling:
        order = {Sensitivity.NONE: 0, Sensitivity.FREEBUSY: 1, Sensitivity.DETAILS: 2}
        wanted = _as_sens(triage.sensitivity) if gw.enabled else Sensitivity.DETAILS
        new_mode = decision.mode
        if order[wanted] > order[Sensitivity(ceiling)]:
            # Mehr als freigegeben verlangt → Bahrian fragen (auch wenn die Vertrauensstufe
            # schon ASK sagte: der Kanalmodus „direct“ würde das sonst wieder wegbügeln).
            if decision.mode in (Mode.AUTO, Mode.ASK):
                new_mode = Mode.ASK
                card_ask = True
        elif decision.mode == Mode.ASK and decision.reason.startswith("disclosure-above-tier"):
            # Die Karte erlaubt es ausdrücklich, obwohl die Stufe „fremd“ fragen würde → antworten.
            new_mode = Mode.AUTO
        decision = Decision(new_mode, Sensitivity(ceiling), decision.reason + "+card-share")

    # Autonomy override: a confident/full owner lets ASTRA skip waiting/asking.
    mode = decision.mode
    appset = appset_pre
    secretary_plan = plan_for(
        channel=channel,
        mode=mode,
        max_sensitivity=decision.max_sensitivity,
        app_settings=appset,
        timezone=s.astra_timezone,
        # Eine Gruppe mit Karte hat Bahrian schon freigegeben (Trigger/Rolle stehen dort) —
        # das pauschale „Gruppen fragen immer nach“ aus plan_for würde jede Erwähnung erneut fragen.
        is_group=False,
        service_active=service_status.active or card_active is True,
        service_reason=service_status.reason,
    )
    # A 'silent' window (e.g. night quiet) → don't respond at all, just log.
    if secretary_plan.silent:
        await db.audit("secretary_silent", channel=channel, thread_id=thread_id,
                       contact_id=contact["id"], detail={"reason": secretary_plan.reason})
        log.info("Secretary silent window on %s — not responding.", thread_id)
        return

    mode = secretary_plan.mode
    auto = get_autonomy()
    if smart_instant and mode == Mode.DEFER:      # laufendes Gespräch: nicht schon wieder warten
        mode = Mode.AUTO
    if auto == "full" and mode in (Mode.DEFER, Mode.ASK):
        mode = Mode.AUTO
        log.info("Autonomy=full → %s escalated to AUTO for %s", decision.mode.value, thread_id)
    elif auto == "confident" and mode == Mode.DEFER:
        mode = Mode.AUTO
    # Verletzt die Anfrage die Freigabe der Karte, fragt ASTRA Bahrian — auch bei Kanalmodus
    # „direct“. Nur bei Autonomie „full“ antwortet ASTRA stattdessen innerhalb der Obergrenze
    # (der Prompt verbietet mehr; das Tool request_owner_approval bleibt für Rückfragen).
    if card_ask and auto != "full" and mode == Mode.AUTO:
        mode = Mode.ASK

    if mode == Mode.AUTO:
        reply = await generate_reply(
            register=Register.THIRD, contact=contact, thread_id=thread_id, channel=channel,
            history=history, summary=thread.get("summary") or "",
            max_sensitivity=decision.max_sensitivity.value,
            model_pick=(card or {}).get("model") or None,
            extra_system=_secretary_system(
                channel,
                secretary_plan.reason,
                app_settings=appset,
                thread_meta=thread.get("meta") or {},
                handle=sender_handle,
                card=card,
            ),
        )
        # Shadow mode: send the draft to Bahrian for review instead of the contact.
        if shadow_enabled(appset, channel):
            await _shadow_to_owner(channel, thread_id, contact, text, reply)
            await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
            return
        await _send_and_record(
            channel, sender_handle, thread_id, reply, contact,
            max_sensitivity=decision.max_sensitivity.value,
        )
        cur = await db.get_thread(thread_id)
        if cur and cur["state"] != ThreadState.AWAITING_APPROVAL.value:  # a tool may have asked
            await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
        if smart_kind and not is_group and smart_reply.applies(secretary_settings(appset), channel,
                                                               {"rule": contact_rule}, smart_reply.settings(appset)):
            await db.merge_thread_meta(thread_id, smart_reply.meta_after_reply(
                smart_reply.REQUEST, smart_reply.settings(appset), time.time()))

    elif mode == Mode.DEFER:
        defer_until = datetime.now(timezone.utc) + timedelta(seconds=s.astra_defer_seconds)
        await db.set_thread_state(thread_id, ThreadState.DEFERRED.value, defer_until=defer_until)
        await db.merge_thread_meta(thread_id, {"max_sensitivity": decision.max_sensitivity.value})
        await db.audit("deferred", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                       detail={"defer_seconds": s.astra_defer_seconds, "secretary": secretary_plan.reason})
        if secretary_plan.should_notify_owner:
            await _notify_owner(channel, thread_id, contact, text, "Secretary wartet auf Bahrian.")
        log.info("Deferred %s for %ss (waiting for owner).", thread_id, s.astra_defer_seconds)

    else:  # ASK
        await _ask_owner(channel, sender_handle, thread_id, contact, text, decision)


async def _card_for_thread(thread: dict) -> dict | None:
    """Kontaktkarte zu einem gespeicherten Thread (für step_in/resume ohne handle_inbound-Kontext)."""
    try:
        kind = "group" if (thread.get("meta") or {}).get("is_group") else "person"
        return await cards.find_card(thread["channel"], _peer(thread["thread_id"]), kind=kind)
    except Exception:  # noqa: BLE001
        return None


async def _ask_new_group(channel: str, handle: str, name: str | None, text: str) -> None:
    """Eine unbekannte Gruppe schreibt: Bahrian fragen, was ASTRA dort tun soll (höchstens alle
    3 Tage pro Gruppe). Bis zur Freigabe wird NICHTS gespeichert und nicht geantwortet."""
    s = get_settings()
    key = f"groupask:{channel}:{handle}"
    try:
        last = float(await db.get_setting(key, 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if time.time() - last < 3 * 24 * 3600:
        return
    await db.set_setting(key, time.time())
    if not (s.telegram_enabled and s.telegram_owner_chat_id):
        return
    label = CHANNEL_LABELS.get(channel, channel)
    title = name or handle
    approval_id = await db.create_approval(
        thread_id=None, contact_id=None, kind="new_group", question=text[:500],
        payload={"channel": channel, "handle": handle, "name": title})
    buttons = [
        [{"text": "👂 Nur zuhören", "callback_data": f"apv:{approval_id}:g_listen"},
         {"text": "💬 Bei @Erwähnung", "callback_data": f"apv:{approval_id}:g_mention"}],
        [{"text": "🚫 Blockieren", "callback_data": f"apv:{approval_id}:g_block"}],
    ]
    await get_channels().send_telegram(
        s.telegram_owner_chat_id,
        f"👥 Neue Gruppe auf {label}: {title}\n„{text[:300]}“\n\nGruppen gibt es für mich erst, "
        "wenn du sie freigibst. Was soll ich hier tun?",
        buttons=buttons)
    await db.audit("group_ask", channel=channel, detail={"group": handle, "name": title})


async def resume_new_group(approval: dict, decision: str) -> None:
    """Bahrian hat über eine neue Gruppe entschieden → Gruppenkarte anlegen."""
    s = get_settings()
    payload = approval.get("payload") or {}
    ch, handle, name = payload.get("channel", ""), payload.get("handle", ""), payload.get("name", "")
    setup = {"g_listen": ("listener", "off", "allow", "👂 Ich höre nur zu (sammle Kontext, antworte nie)."),
             "g_mention": ("assistant", "mention", "allow",
                           "💬 Ich antworte nur, wenn jemand @Bahrian/@ASTRA schreibt."),
             "g_block": ("assistant", "mention", "block", "🚫 Blockiert — ich ignoriere die Gruppe.")}.get(decision)
    if not (ch and handle and setup):
        return
    role, trigger, rule, said = setup
    card = cards.new_card("group", name or handle, [{"channel": ch, "id": handle}])
    card["group"].update(role=role, trigger=trigger)
    card["rule"] = rule
    taken = {c["key"] for c in await cards.load_all(force=True)}
    base, i = card["key"], 2
    while card["key"] in taken:
        card["key"] = f"{base}_{i}"
        i += 1
    await cards.save_card(card)
    await db.audit("group_decided", actor="owner", channel=ch, detail={"group": handle, "decision": decision})
    if s.telegram_enabled and s.telegram_owner_chat_id:
        await get_channels().send_telegram(
            s.telegram_owner_chat_id, f"Gruppe „{name}“: {said}\n(Feinheiten unter Personen → Gruppen.)")


async def _moderate_group(channel: str, handle: str, thread_id: str, contact: dict, text: str,
                          card: dict, thread: dict, appset: dict) -> None:
    """Moderator-Rolle: bei auffälligen Nachrichten ruhig eingreifen (mit Abkühlzeit)."""
    meta = thread.get("meta") or {}
    if time.time() - float(meta.get("last_moderated_at") or 0) < 90:
        return
    history = await db.recent_messages(thread_id)
    m_meta = {**meta, "tone_override": "moderator"}
    reply = await generate_reply(
        register=Register.THIRD, contact=contact, thread_id=thread_id, channel=channel,
        history=history, summary=thread.get("summary") or "", max_sensitivity="none",
        model_pick=card.get("model") or None,
        extra_system=_secretary_system(channel, "group-moderation", app_settings=appset,
                                       thread_meta=m_meta, handle=handle, card=card)
        + " Eine Nachricht in der Gruppe war unangemessen. Greife kurz, ruhig und freundlich ein, "
          "ohne jemanden namentlich bloßzustellen.")
    await db.merge_thread_meta(thread_id, {"last_moderated_at": time.time()})
    await _send_and_record(channel, handle, thread_id, reply, contact, max_sensitivity="none")


async def _ask_owner(channel: str, peer: str, thread_id: str, contact: dict, text: str, decision) -> None:
    s = get_settings()
    approval_id = await db.create_approval(
        thread_id=thread_id, contact_id=contact["id"], kind="disclosure",
        question=text, payload={"channel": channel, "topic": cards.topic_for(text)},
    )
    await db.set_thread_state(thread_id, ThreadState.AWAITING_APPROVAL.value)
    await db.audit("ask_principal", channel=channel, thread_id=thread_id, contact_id=contact["id"],
                   detail={"approval_id": approval_id})
    name = contact.get("display_name") or contact.get("handle")
    if s.telegram_enabled and s.telegram_owner_chat_id:
        topic = cards.TOPIC_LABELS.get(cards.topic_for(text), "")
        buttons = [
            [{"text": "✅ Ja", "callback_data": f"apv:{approval_id}:yes"},
             {"text": "🟡 Nur 'beschäftigt'", "callback_data": f"apv:{approval_id}:busy_only"},
             {"text": "❌ Nein", "callback_data": f"apv:{approval_id}:no"}],
            [{"text": "♾ Immer ja", "callback_data": f"apv:{approval_id}:always_yes"},
             {"text": "♾ Immer nur 'beschäftigt'", "callback_data": f"apv:{approval_id}:always_busy"},
             {"text": "🚫 Nie", "callback_data": f"apv:{approval_id}:never"}],
        ]
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"🔔 {name} ({channel}) fragt:\n„{text}“\n\nDarf ASTRA antworten?"
            + (f"\n(Thema: {topic} — „Immer/Nie“ merke ich mir für {name}.)" if topic else ""),
            buttons=buttons,
        )
    if channel == "email":
        await db.add_message(
            thread_id,
            "assistant",
            "E-Mail-Antwort wartet auf Bahrians Freigabe.",
        )
    else:
        await _send_and_record(
            channel, peer, thread_id,
            "Einen Moment — ich halte kurz Rücksprache mit Bahrian und melde mich gleich.",
            contact,
            max_sensitivity="none",
        )


async def _shadow_to_owner(channel: str, thread_id: str, contact: dict, incoming: str, draft: str) -> None:
    """Shadow mode: show Bahrian the reply ASTRA WOULD send, without sending it."""
    s = get_settings()
    await db.add_message(thread_id, "assistant", f"[SCHATTEN, nicht gesendet] {draft}")
    await db.audit("secretary_shadow", channel=channel, thread_id=thread_id,
                   contact_id=contact.get("id"), detail={"preview": draft[:200]})
    if s.telegram_enabled and s.telegram_owner_chat_id:
        name = contact.get("display_name") or contact.get("handle") or "Unbekannt"
        label = CHANNEL_LABELS.get(channel, channel)
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"🕶️ Schattenmodus ({label}) — {name} schrieb:\n„{incoming[:400]}“\n\n"
            f"ASTRA würde antworten:\n„{draft}“\n\n(Nicht gesendet. Zum Scharfschalten "
            f"Schattenmodus im Secretary aus.)",
        )


async def _notify_owner(channel: str, thread_id: str, contact: dict, text: str, note: str) -> None:
    s = get_settings()
    if not (s.telegram_enabled and s.telegram_owner_chat_id):
        return
    name = contact.get("display_name") or contact.get("handle") or "Unbekannt"
    await get_channels().send_telegram(
        s.telegram_owner_chat_id,
        f"Secretary-Hinweis: {note}\n{name} ({channel}) schrieb:\n{text[:900]}\n\nThread: {thread_id}",
    )


# ─── deferral step-in (called by the sweeper) ──────────────────────────────────
async def step_in(thread_id: str) -> None:
    thread = await db.get_thread(thread_id)
    if not thread:
        return
    if next_state(ThreadState(thread["state"]), Signal.DEFER_ELAPSED).act != Act.STEP_IN:
        return  # owner already stood it down / it was answered
    appset = await _app_settings()
    if not channel_enabled(appset, thread["channel"]):
        await db.set_thread_state(thread_id, ThreadState.STANDDOWN.value)
        await db.audit("secretary_disabled", channel=thread["channel"], thread_id=thread_id,
                       contact_id=thread.get("contact_id"), detail={"phase": "deferred_step_in"})
        return
    contact = await db.get_contact(thread["contact_id"]) if thread.get("contact_id") else {}
    meta = thread.get("meta") or {}
    ceiling = meta.get("max_sensitivity", "freebusy")
    history = await db.recent_messages(thread_id)
    card = await _card_for_thread(thread)
    # Smart-Antwort: nach dem Warten nur bei einer echten Anfrage inhaltlich antworten; bei „Hallo/bist du da“
    # oder Smalltalk stellt sich ASTRA EINMAL vor (ehrlich, was es kann) und schweigt danach eine Weile.
    smart_kind = str(meta.get("smart_kind") or "")
    intro = smart_kind in (smart_reply.PING, smart_reply.CHAT)
    if intro:
        lead = smart_reply.intro_instruction(
            calendar=smart_reply.calendar_ready() and cards.share_ceiling(card) != "none",
            owner=get_settings().astra_owner_name) + " "
    else:
        lead = "Bahrian hat nicht selbst geantwortet. Antworte jetzt stellvertretend, knapp und souverän. "
    reply = await generate_reply(
        register=Register.THIRD, contact=contact or {}, thread_id=thread_id, channel=thread["channel"],
        history=history, summary=thread.get("summary") or "", max_sensitivity=ceiling,
        model_pick=(card or {}).get("model") or None,
        extra_system=(
            lead
            + _secretary_system(
                thread["channel"],
                "defer-elapsed",
                app_settings=appset,
                thread_meta=thread.get("meta") or {},
                handle=_peer(thread_id),
                card=card,
            )
        ),
    )
    await _send_and_record(
        thread["channel"], _peer(thread_id), thread_id, reply, contact or {},
        max_sensitivity=ceiling,
    )
    await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
    if smart_kind:
        await db.merge_thread_meta(thread_id, smart_reply.meta_after_reply(
            smart_kind, smart_reply.settings(appset), time.time()))
    await db.audit("stepin", channel=thread["channel"], thread_id=thread_id,
                   contact_id=thread.get("contact_id"), detail={"intro": intro} if smart_kind else None)
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
    appset = await _app_settings()
    if not channel_enabled(appset, thread["channel"]):
        await db.set_thread_state(thread_id, ThreadState.STANDDOWN.value)
        await db.audit("secretary_disabled", channel=thread["channel"], thread_id=thread_id,
                       contact_id=thread.get("contact_id"), detail={"phase": "approval_resume"})
        return
    contact = await db.get_contact(thread["contact_id"]) if thread.get("contact_id") else {}
    # „Immer/Nie“ → dauerhaft in die Karte schreiben; für DIESE Antwort gilt die Grundentscheidung.
    if decision in cards.LEARN_DECISIONS:
        await cards.learn_from_approval(approval, decision)
        decision = cards.LEARN_DECISIONS[decision]
    ceiling, instruction = _RESUME.get(decision, _RESUME["no"])
    history = await db.recent_messages(thread_id)
    card = await _card_for_thread(thread)
    reply = await generate_reply(
        register=Register.THIRD, contact=contact or {}, thread_id=thread_id, channel=thread["channel"],
        history=history, summary=thread.get("summary") or "", max_sensitivity=ceiling.value,
        model_pick=(card or {}).get("model") or None,
        extra_system=instruction + " " + _secretary_system(
            thread["channel"],
            "owner-approved",
            app_settings=appset,
            thread_meta=thread.get("meta") or {},
            handle=_peer(thread_id),
            card=card,
        ),
    )
    await _send_and_record(
        thread["channel"], _peer(thread_id), thread_id, reply, contact or {},
        max_sensitivity=ceiling.value,
    )
    await db.set_thread_state(thread_id, ThreadState.ANSWERED.value)
    await db.audit("resume", channel=thread["channel"], thread_id=thread_id,
                   contact_id=thread.get("contact_id"), detail={"decision": decision})


# ─── ops command confirmation (HomeLab execution, gated on Telegram) ───────────
async def resume_ops_exec(approval: dict, decision: str) -> None:
    """Owner decided on a command that needed approval. 'yes' → run it now."""
    s = get_settings()
    payload = approval.get("payload") or {}
    host, command = payload.get("host", ""), payload.get("command", "")

    async def tell(text: str) -> None:
        if s.telegram_enabled and s.telegram_owner_chat_id:
            await get_channels().send_telegram(s.telegram_owner_chat_id, text)

    if decision != "yes":
        await db.audit("ops_cancelled", detail={"host": host, "command": command})
        await tell(f"Abgebrochen — auf {host} wurde nichts ausgeführt.")
        return

    from .plugins.registry import get_manager
    plugin = get_manager().get(payload.get("plugin") or "ops_exec")
    if not plugin or not plugin.enabled:
        await tell("Die HomeLab-Ausführung ist nicht mehr aktiv — nichts ausgeführt.")
        return
    result = await plugin.run(host, command)
    await db.audit("ops_exec", detail={"host": host, "command": command,
                                       "ok": result["ok"], "via": "approval"})
    head = "✅" if result["ok"] else "⚠️"
    await tell(f"{head} {host}$ {command}\n\n{result['output'][:1500]}")


# ─── outbound send confirmation (owner-initiated, gated on Telegram) ────────────
async def resume_outbound_send(approval: dict, decision: str) -> None:
    """ASTRA wanted to message a third party; the owner just decided on Telegram.

    decision: 'yes'/'send' → actually send; anything else → cancel.
    """
    s = get_settings()
    payload = approval.get("payload") or {}
    channel = payload.get("channel") or ""
    to = payload.get("to") or ""
    text = payload.get("text") or ""

    if decision == "no":
        await db.audit("outbound_cancelled", channel=channel, detail={"to": to})
        if s.telegram_enabled and s.telegram_owner_chat_id:
            await get_channels().send_telegram(
                s.telegram_owner_chat_id, f"Abgebrochen — an {to} wurde nichts gesendet."
            )
        return

    ok = await get_channels().send(channel, to, text)
    await db.audit("outbound_sent", channel=channel,
                   detail={"to": to, "ok": ok, "preview": text[:160]})
    if s.telegram_enabled and s.telegram_owner_chat_id:
        label = CHANNEL_LABELS.get(channel, channel)
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"✅ Gesendet an {to} ({label})." if ok
            else (f"⚠️ Senden an {to} ({label}) ist fehlgeschlagen"
                  + (f": {get_channels().last_error(channel)}"
                     if get_channels().last_error(channel) else ".")),
        )
