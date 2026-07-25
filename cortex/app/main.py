"""FastAPI entry point for ASTRA cortex.

Startup:
  • Initialises the Postgres connection pool
  • Launches two background tasks:
      – deferral_sweeper  : wakes every 5 s, fires brain.step_in() for due threads
      – telegram_poller   : long-polls Telegram getUpdates (only when
                            ASTRA_TELEGRAM_MODE="poll")

Ingress webhooks (protected by X-Astra-Secret header):
  POST /ingress/waha    — WhatsApp via WAHA container
  POST /ingress/signal  — Signal via signal-cli-rest-api container

Approval callbacks:
  Inline-keyboard buttons sent by brain._ask_owner() carry
  callback_data = "apv:{approval_id}:{decision}".
  The Telegram poller intercepts them, resolves the approval and
  calls brain.resume_after_approval().

Health:
  GET /health  — returns {"status":"ok"} for Docker HEALTHCHECK
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import brain, briefing, db, knowledge
from .channels import get_channels
from .config import get_settings
from .integrations.transcription import get_transcriber
from .plugins.registry import get_manager
from .web import admin as web_admin
from .web import auth as web_auth

log = logging.getLogger("astra.main")

# ─── Background tasks ─────────────────────────────────────────────────────────

async def _deferral_sweeper() -> None:
    """Wakes every 5 seconds and steps in on any thread whose deferral has elapsed."""
    while True:
        try:
            due = await db.due_deferrals()
            for thread in due:
                tid = thread["thread_id"]
                log.info("Sweeper: stepping in on deferred thread %s", tid)
                try:
                    await brain.step_in(tid)
                except Exception:  # noqa: BLE001
                    log.exception("step_in failed for %s", tid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Deferral sweeper error (will retry in 5 s)")
        await asyncio.sleep(5)


async def _rules_scheduler() -> None:
    """Wakes every 30 s and fires any schedule-triggered rule that is due."""
    from . import rules
    while True:
        try:
            n = await rules.tick()
            if n:
                log.info("Rules scheduler fired %d rule(s).", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Rules scheduler error (retry in 30 s)")
        await asyncio.sleep(30)


async def _telegram_poller() -> None:
    """Long-poll Telegram getUpdates and dispatch to brain."""
    s = get_settings()
    if not s.telegram_bot_token:
        log.warning("Telegram poller: no bot token configured — skipping.")
        return

    base = f"https://api.telegram.org/bot{s.telegram_bot_token}"
    offset: int | None = None

    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                params: dict[str, Any] = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset

                resp = await client.get(f"{base}/getUpdates", params=params)
                resp.raise_for_status()
                data = resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        await _handle_tg_update(update, base, client)
                    except Exception:  # noqa: BLE001
                        log.exception("Error handling Telegram update %s", update.get("update_id"))

            except asyncio.CancelledError:
                raise
            except (httpx.ReadTimeout, httpx.ConnectTimeout):
                # Normal during long-polling — just loop again
                pass
            except Exception:  # noqa: BLE001
                log.exception("Telegram poller error — backing off 10 s")
                await asyncio.sleep(10)


async def _handle_tg_update(
    update: dict, base: str, client: httpx.AsyncClient
) -> None:
    """Dispatch a single Telegram update."""
    s = get_settings()

    # ── Inline-keyboard callback (approval buttons) ──────────────────────────
    if cb := update.get("callback_query"):
        callback_id = cb["id"]
        data = cb.get("data", "")
        sender_id = str(cb["from"]["id"])

        # Acknowledge immediately so the spinner disappears
        await client.post(f"{base}/answerCallbackQuery", json={"callback_query_id": callback_id})

        if data.startswith("apv:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                _, approval_id, decision = parts
                approval = await db.get_approval(approval_id)
                if not approval:
                    log.warning("Callback for unknown approval %s", approval_id)
                    return
                # Record decision in DB (decide_approval returns None if already decided)
                decided = await db.decide_approval(approval_id, decision)
                if not decided:
                    log.info("Approval %s already decided, ignoring duplicate callback.", approval_id)
                    return
                await _resume_approval(approval, decision)
                log.info("Approval %s decided: %s", approval_id, decision)
            else:
                log.warning("Malformed apv callback_data: %r", data)
        else:
            # Unrecognised button — forward as text from the owner
            text = cb.get("message", {}).get("text") or data
            chat = (cb.get("message") or {}).get("chat") or {}
            chat_type = chat.get("type", "private")
            is_group = chat_type in {"group", "supergroup"}
            await brain.handle_inbound(
                channel="telegram",
                sender_handle=str(chat.get("id") or sender_id) if is_group else sender_id,
                text=text,
                sender_display=chat.get("title") if is_group else _tg_display(cb["from"]),
                thread_meta={
                    "is_group": is_group,
                    "chat_type": chat_type,
                    "participant_handle": sender_id,
                    "participant_display": _tg_display(cb["from"]),
                    "participant_username": cb["from"].get("username"),
                    "username": cb["from"].get("username"),
                    "source_tag": "from Telegram",
                },
            )
        return

    # ── Regular message (text, caption, or voice/audio note) ─────────────────
    if msg := update.get("message"):
        text = msg.get("text") or msg.get("caption") or ""

        # Voice / audio → transcribe with Whisper, then treat as text.
        if not text and (voice := (msg.get("voice") or msg.get("audio"))):
            text = await _transcribe_voice(voice, base, client)
            if text:
                text = f"🎤 {text}"

        if not text:
            return  # ignore stickers, photos without caption, etc.

        sender = msg["from"]
        sender_id = str(sender["id"])
        chat = msg.get("chat") or {}
        chat_type = chat.get("type", "private")
        is_group = chat_type in {"group", "supergroup"}

        # Owner typing a bare "ja"/"nein" decides a pending approval directly.
        if not is_group and sender_id == str(s.telegram_owner_chat_id):
            if await _maybe_resolve_pending_approval(text):
                return

        await brain.handle_inbound(
            channel="telegram",
            sender_handle=str(chat.get("id") or sender_id) if is_group else sender_id,
            text=text,
            sender_display=chat.get("title") if is_group else _tg_display(sender),
            thread_meta={
                "is_group": is_group,
                "chat_type": chat_type,
                "participant_handle": sender_id,
                "participant_display": _tg_display(sender),
                "participant_username": sender.get("username"),
                "username": sender.get("username"),
                "source_tag": "from Telegram",
            },
        )


async def _transcribe_voice(voice: dict, base: str, client: httpx.AsyncClient) -> str:
    """Download a Telegram voice/audio file and transcribe it via Whisper."""
    tr = get_transcriber()
    if not tr.enabled:
        log.info("Voice message received but transcription disabled.")
        return ""
    file_id = voice.get("file_id")
    if not file_id:
        return ""
    try:
        meta = await client.get(f"{base}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]
        token = get_settings().telegram_bot_token
        dl = await client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
        dl.raise_for_status()
        fname = file_path.split("/")[-1] or "voice.ogg"
        text = await tr.transcribe(dl.content, filename=fname)
        log.info("Transcribed voice note (%d chars).", len(text))
        return text
    except Exception as e:  # noqa: BLE001
        log.warning("voice download/transcribe failed: %s", e)
        return ""


def _tg_display(user: dict) -> str:
    parts = [user.get("first_name", ""), user.get("last_name", "")]
    full = " ".join(p for p in parts if p).strip()
    return full or user.get("username") or str(user.get("id", "unknown"))


_AFFIRM = {"ja", "jo", "jep", "jepp", "jup", "yes", "yep", "ok", "okay", "okey",
           "passt", "sende", "senden", "send", "schick", "schicken", "los", "mach",
           "👍", "✅", "🆗", "ja!", "jo!"}
_NEGATE = {"nein", "ne", "nö", "noe", "no", "nope", "stop", "stopp", "abbrechen",
           "cancel", "lass", "lassen", "abbruch", "❌", "🚫", "nein!"}


def _decision_from_text(text: str) -> str | None:
    """Map a bare 'ja'/'nein'-style owner reply to an approval decision, else None.

    Strict: only a short, standalone affirmation/negation counts — a real chat
    message like 'ja ich brauche noch...' must never silently approve anything.
    """
    norm = (text or "").strip().lower().rstrip(".!? ")
    if len(norm) > 12:
        return None
    if norm in _AFFIRM:
        return "yes"
    if norm in _NEGATE:
        return "no"
    return None


async def _resume_approval(approval: dict, decision: str) -> None:
    """Route a decided approval to the flow that created it."""
    kind = approval.get("kind")
    if kind == "outbound_send":
        await brain.resume_outbound_send(approval, decision)
    elif kind == "ops_exec":
        await brain.resume_ops_exec(approval, decision)
    else:
        await brain.resume_after_approval(approval, decision)


async def _maybe_resolve_pending_approval(text: str) -> bool:
    """If the owner typed a bare yes/no and an approval is pending, decide it.

    Returns True when the message was consumed as a decision."""
    decision = _decision_from_text(text)
    if decision is None:
        return False
    approval = await db.latest_pending_approval()
    if not approval:
        return False
    decided = await db.decide_approval(approval["id"], decision)
    if not decided:
        return False
    await _resume_approval(approval, decision)
    log.info("Approval %s decided via typed reply: %s", approval["id"], decision)
    return True


# ─── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB pool + background tasks. Shutdown: clean up."""
    s = get_settings()

    # Configure root log level from settings
    logging.basicConfig(
        level=s.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    log.info("ASTRA cortex starting up…")
    knowledge.ensure_seeded()
    await db.init_pool()
    await web_auth.ensure_password_from_env()

    # Load plugins → register their tools + start their background tasks.
    await get_manager().rebuild()

    # Give ASTRA control over its own setup (owner-only core tools).
    from .admin_tools import register_admin_tools
    register_admin_tools()

    # Apply web-configured preferences (model override, UI font, autonomy) live.
    try:
        appset = await db.get_setting("app_settings", {}) or {}
        from .brain import set_autonomy
        from .models import set_economy, set_model_config, set_model_override
        from .web.templates import set_font
        set_model_override(appset.get("ai_model"))
        set_economy(bool(appset.get("economy_mode")))
        set_model_config(appset.get("models"))   # provider registry + role assignment
        set_font(appset.get("font"))
        set_autonomy(appset.get("autonomy", "ask"))
    except Exception:  # noqa: BLE001
        log.warning("Could not apply saved app_settings.", exc_info=True)

    tasks: list[asyncio.Task] = []

    sweeper_task = asyncio.create_task(_deferral_sweeper(), name="deferral_sweeper")
    tasks.append(sweeper_task)
    log.info("Deferral sweeper started.")

    rules_task = asyncio.create_task(_rules_scheduler(), name="rules_scheduler")
    tasks.append(rules_task)
    log.info("Rules scheduler started.")

    if s.astra_telegram_mode == "poll":
        poller_task = asyncio.create_task(_telegram_poller(), name="telegram_poller")
        tasks.append(poller_task)
        log.info("Telegram poller started (mode=poll).")
    else:
        log.info("Telegram mode=%s — no background poller.", s.astra_telegram_mode)

    if s.astra_briefing_enabled and s.telegram_enabled:
        briefing_task = asyncio.create_task(briefing.scheduler(), name="briefing")
        tasks.append(briefing_task)
        log.info("Morning briefing scheduler started (%s).", s.astra_briefing_time)

    # Self-documenting boot log: voice + which plugins are live.
    enabled_plugins = ", ".join(p.slug for p in get_manager().enabled()) or "(none)"
    log.info("Capabilities — voice:%s · plugins: %s", s.voice_enabled, enabled_plugins)

    yield  # ← server is running

    log.info("ASTRA cortex shutting down…")
    await get_manager().shutdown()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await get_channels().aclose()
    await db.close_pool()
    log.info("Shutdown complete.")


# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="ASTRA cortex", version="2.0.0", lifespan=lifespan)

_STATIC_DIR = Path(__file__).parent / "web" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(web_admin.router)


# ─── Auth helper ──────────────────────────────────────────────────────────────

def _verify_secret(x_astra_secret: str | None) -> None:
    """Raise 403 if the shared secret header is missing or wrong."""
    if x_astra_secret != get_settings().cortex_shared_secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Astra-Secret header.",
        )


# ─── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["infra"])
async def health():
    return {"status": "ok"}


# ─── Morning briefing (manual trigger) ──────────────────────────────────────────

@app.post("/briefing/run", tags=["briefing"])
async def briefing_run(x_astra_secret: str | None = Header(default=None)):
    _verify_secret(x_astra_secret)
    ok = await briefing.send()
    return {"ok": ok}


@app.get("/briefing/preview", tags=["briefing"])
async def briefing_preview(x_astra_secret: str | None = Header(default=None)):
    """Render the briefing text without sending it (for testing)."""
    _verify_secret(x_astra_secret)
    return {"text": await briefing.compose()}


# ─── Dashboard (lightweight status GUI) ─────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse, tags=["infra"])
async def dashboard():
    from .dashboard import render
    s = get_settings()
    threads = await db.list_threads(20)
    approvals = await db.pending_approvals()
    audit = await db.recent_audit(25)
    return render(s, threads, approvals, audit)


# ─── Telegram webhook (optional — used when ASTRA_TELEGRAM_MODE=webhook) ──────

@app.post("/ingress/telegram", tags=["ingress"])
async def ingress_telegram(
    request: Request,
    x_astra_secret: str | None = Header(default=None),
):
    """Telegram can be configured to POST updates here instead of long-polling.
    Only active when ASTRA_TELEGRAM_MODE=webhook.  The background poller is
    then NOT started.  Both paths share _handle_tg_update().
    """
    _verify_secret(x_astra_secret)
    update = await request.json()
    s = get_settings()
    async with httpx.AsyncClient(timeout=10) as client:
        base = f"https://api.telegram.org/bot{s.telegram_bot_token}"
        await _handle_tg_update(update, base, client)
    return {"ok": True}


# ─── WAHA (WhatsApp) ingress ───────────────────────────────────────────────────

@app.post("/ingress/waha", tags=["ingress"])
async def ingress_waha(
    request: Request,
    x_astra_secret: str | None = Header(default=None),
):
    """Receives WAHA webhook events.

    Expected WAHA payload (event=message):
    {
      "event": "message",
      "session": "default",
      "payload": {
        "id":       "...",
        "from":     "4915212345678@c.us",
        "fromMe":   false,
        "body":     "Hallo",
        "_data": { "notifyName": "Max Mustermann" }
      }
    }
    """
    _verify_secret(x_astra_secret)
    body = await request.json()
    log.debug("WAHA event: %s", body.get("event"))

    if body.get("event") != "message":
        return {"ok": True, "skipped": "not a message event"}

    payload = body.get("payload", {})
    text: str = payload.get("body") or ""
    if not text.strip():
        return {"ok": True, "skipped": "empty body"}

    from_jid: str = payload.get("from", "")
    from_me: bool = bool(payload.get("fromMe", False))
    raw_data = payload.get("_data") or {}

    # WhatsApp JID looks like "4915212345678@c.us" — use as handle
    sender_handle = from_jid
    display_name: str | None = raw_data.get("chatName") or raw_data.get("notifyName")
    is_group = from_jid.endswith("@g.us")

    await brain.handle_inbound(
        channel="waha",
        sender_handle=sender_handle,
        text=text,
        sender_display=display_name,
        force_owner=from_me,   # fromMe=True → owner sent this himself
        thread_meta={
            "is_group": is_group,
            "group_id": from_jid if is_group else None,
            "participant_handle": payload.get("participant") or raw_data.get("participant"),
            "participant_display": raw_data.get("notifyName"),
            "participant_username": raw_data.get("pushname") or raw_data.get("notifyName"),
            "username": raw_data.get("pushname") or raw_data.get("notifyName"),
            "source_tag": "from WhatsApp",
        },
    )
    return {"ok": True}


# ─── Signal ingress ────────────────────────────────────────────────────────────

@app.post("/ingress/signal", tags=["ingress"])
async def ingress_signal(
    request: Request,
    x_astra_secret: str | None = Header(default=None),
):
    """Receives signal-cli-rest-api webhook events.

    Expected payload:
    {
      "envelope": {
        "source":      "+4915212345678",
        "sourceName":  "Max Mustermann",
        "dataMessage": {
          "message": "Hallo"
        }
      }
    }
    """
    _verify_secret(x_astra_secret)
    body = await request.json()
    log.debug("Signal envelope received.")

    envelope = body.get("envelope", {})
    data_msg = envelope.get("dataMessage") or {}
    text: str = data_msg.get("message") or ""
    if not text.strip():
        return {"ok": True, "skipped": "empty message"}

    sender_handle: str = envelope.get("source") or envelope.get("sourceNumber") or ""
    if not sender_handle:
        log.warning("Signal webhook missing source number.")
        return {"ok": True, "skipped": "no source"}

    display_name: str | None = envelope.get("sourceName")
    group_info = data_msg.get("groupInfo") or {}
    group_id = group_info.get("groupId") or group_info.get("group_id")
    is_group = bool(group_id)

    await brain.handle_inbound(
        channel="signal",
        sender_handle=group_id or sender_handle,
        text=text,
        sender_display=group_info.get("name") or display_name,
        thread_meta={
            "is_group": is_group,
            "group_id": group_id,
            "group_name": group_info.get("name"),
            "participant_handle": sender_handle,
            "participant_display": display_name,
            "participant_username": envelope.get("sourceUuid") or display_name,
            "username": envelope.get("sourceUuid") or display_name,
            "source_tag": "from Signal",
        },
    )
    return {"ok": True}


# ─── Voice ingress (HA Assist satellite → spoken reply) ────────────────────────

@app.post("/ingress/voice", tags=["ingress"])
async def ingress_voice(
    request: Request,
    x_astra_secret: str | None = Header(default=None),
):
    """HA Assist pipeline posts the recognized text here; ASTRA replies in one or
    two spoken sentences that HA reads back. The satellite may send its `area`,
    which becomes the default room for 'wie warm ist es hier'.

    Body: {"text": "...", "area": "Wohnzimmer", "principal": "" }
    """
    _verify_secret(x_astra_secret)
    from .agent import generate_reply
    from .persona import Register

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"reply": ""}
    area = (body.get("area") or "").strip()
    principal = (body.get("principal") or "").strip()

    extra = (f"Der Lautsprecher steht im Raum „{area}“. Wenn {get_settings().astra_owner_name} "
             f"„hier“ meint, ist dieser Raum gemeint." if area else "")
    thread_id = f"voice:{principal or 'default'}"
    reply = await generate_reply(
        register=Register.VOICE, contact={}, thread_id=thread_id, channel="voice",
        history=[{"role": "owner", "content": text}], max_sensitivity="details",
        extra_system=extra, principal=principal,
    )
    await db.audit("voice_turn", channel="voice", thread_id=thread_id,
                   detail={"area": area, "text": text[:200]})
    return {"reply": reply}


# ─── E-Mail ingress ───────────────────────────────────────────────────────────

@app.post("/ingress/email", tags=["ingress"])
async def ingress_email(
    request: Request,
    x_astra_secret: str | None = Header(default=None),
):
    """Receives normalized inbound mail from n8n, IMAP pollers, or future plugins."""
    _verify_secret(x_astra_secret)
    body = await request.json()
    sender_handle = body.get("from") or body.get("sender") or body.get("email") or ""
    if not sender_handle:
        return {"ok": True, "skipped": "no sender"}
    subject = (body.get("subject") or "").strip()
    text = (body.get("text") or body.get("body") or "").strip()
    if not text and not subject:
        return {"ok": True, "skipped": "empty email"}
    content = f"Betreff: {subject}\n\n{text}".strip() if subject else text
    await brain.handle_inbound(
        channel="email",
        sender_handle=sender_handle,
        text=content,
        sender_display=body.get("name") or sender_handle,
        thread_meta={"source_tag": "from Mail", "username": body.get("name") or sender_handle},
    )
    return {"ok": True}
