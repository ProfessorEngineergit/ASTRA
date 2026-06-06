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
                await brain.resume_after_approval(approval, decision)
                log.info("Approval %s decided: %s", approval_id, decision)
            else:
                log.warning("Malformed apv callback_data: %r", data)
        else:
            # Unrecognised button — forward as text from the owner
            text = cb.get("message", {}).get("text") or data
            await brain.handle_inbound(
                channel="telegram",
                sender_handle=sender_id,
                text=text,
                sender_display=_tg_display(cb["from"]),
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
        await brain.handle_inbound(
            channel="telegram",
            sender_handle=sender_id,
            text=text,
            sender_display=_tg_display(sender),
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

    tasks: list[asyncio.Task] = []

    sweeper_task = asyncio.create_task(_deferral_sweeper(), name="deferral_sweeper")
    tasks.append(sweeper_task)
    log.info("Deferral sweeper started.")

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

    # WhatsApp JID looks like "4915212345678@c.us" — use as handle
    sender_handle = from_jid
    display_name: str | None = (payload.get("_data") or {}).get("notifyName")

    await brain.handle_inbound(
        channel="waha",
        sender_handle=sender_handle,
        text=text,
        sender_display=display_name,
        force_owner=from_me,   # fromMe=True → owner sent this himself
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

    await brain.handle_inbound(
        channel="signal",
        sender_handle=sender_handle,
        text=text,
        sender_display=display_name,
    )
    return {"ok": True}
