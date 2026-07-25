"""Morning briefing — the proactive 'wake up, here's your day' message.

Composes a Telegram message from: overnight inbound messages (grouped by channel),
today's school timetable (EduPage), next departures (RMV), and — if a key is set —
a short LLM-written intro. Everything degrades gracefully: sections whose source is
unconfigured are simply omitted.

A scheduler loop (started in main.py when ASTRA_BRIEFING_ENABLED=true) fires it once
per day at ASTRA_BRIEFING_TIME (local). It can also be triggered manually via
POST /briefing/run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import db, knowledge
from .config import get_settings
from .channels import get_channels
from .models import get_gateway
from .plugins.registry import get_manager

log = logging.getLogger("astra.briefing")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().astra_timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _channel_label(ch: str) -> str:
    return {"waha": "WhatsApp", "signal": "Signal", "telegram": "Telegram"}.get(ch, ch)


async def _overnight_section() -> str:
    since = datetime.now(_tz()) - timedelta(hours=12)
    msgs = await db.inbound_since(since)
    if not msgs:
        return "📭 Über Nacht keine neuen Nachrichten."
    by_channel: dict[str, list[dict]] = {}
    for m in msgs:
        by_channel.setdefault(m["channel"], []).append(m)
    lines = [f"📬 *Über Nacht* ({len(msgs)} Nachrichten):"]
    for ch, items in by_channel.items():
        senders = {}
        for it in items:
            senders[it["who"]] = senders.get(it["who"], 0) + 1
        who = ", ".join(f"{name} ({n})" for name, n in list(senders.items())[:6])
        lines.append(f"  • {_channel_label(ch)}: {who}")
    return "\n".join(lines)


async def _intro(sections: list[str]) -> str:
    gw = get_gateway()
    if not gw.enabled:
        return f"☀️ Guten Morgen, {get_settings().astra_owner_name}!"
    try:
        kb = knowledge.owner_context()
        msg = [
            {"role": "system", "content":
                "Du bist ASTRA. Schreibe EINEN kurzen, energiegeladenen Guten-Morgen-Satz "
                f"für {get_settings().astra_owner_name} (du-Form). Kein Markdown, keine Emojis."},
            {"role": "user", "content": "Kontext:\n" + "\n".join(sections)[:1500] +
                (f"\n\nRoutinen:\n{kb[:800]}" if kb else "")},
        ]
        # One throwaway greeting sentence does not need the expensive model.
        from .models import SMALL
        out = await gw.chat(msg, temperature=0.7, role=SMALL)
        return "☀️ " + (out.content or "Guten Morgen!").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("briefing intro failed: %s", e)
        return f"☀️ Guten Morgen, {get_settings().astra_owner_name}!"


async def compose() -> str:
    """Build the full briefing text (Telegram Markdown)."""
    sections: list[str] = []
    try:
        overnight = await _overnight_section()
        if overnight:
            sections.append(overnight)
    except Exception as e:  # noqa: BLE001
        log.warning("overnight section failed: %s", e)
    # Plugin-contributed sections (timetable, transit, …)
    sections.extend(await get_manager().briefing_sections())
    intro = await _intro(sections)
    today = datetime.now(_tz()).strftime("%A, %d.%m.%Y")
    return f"{intro}\n\n_{today}_\n\n" + "\n\n".join(sections)


async def send(chat_id: str | None = None) -> bool:
    s = get_settings()
    chat = chat_id or s.briefing_chat
    if not chat:
        log.warning("Briefing: no chat id configured.")
        return False
    text = await compose()
    # parse_mode renders the *bold* / _italic_ section headers instead of showing
    # literal asterisks; send_telegram falls back to plain text if a name breaks it.
    ok = await get_channels().send_telegram(chat, text, parse_mode="Markdown")
    await db.audit("briefing_sent", channel="telegram", detail={"ok": ok})
    return ok


# ─── Scheduler ────────────────────────────────────────────────────────────────
def _seconds_until(target: time) -> float:
    now = datetime.now(_tz())
    nxt = datetime.combine(now.date(), target, tzinfo=_tz())
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _parse_time(hhmm: str) -> time:
    try:
        h, m = hhmm.split(":")
        return time(int(h), int(m))
    except Exception:  # noqa: BLE001
        return time(7, 0)


async def scheduler() -> None:
    """Sleep until the configured local time, send, repeat daily.

    The time is re-read each loop, so changing ASTRA_BRIEFING_TIME (or a future
    web setting) takes effect the next day without a restart."""
    log.info("Briefing scheduler armed (%s local).", get_settings().astra_briefing_time)
    while True:
        try:
            target = _parse_time(get_settings().astra_briefing_time)
            await asyncio.sleep(_seconds_until(target))
            log.info("Briefing scheduler: composing & sending.")
            await send()
            await asyncio.sleep(60)  # avoid double-fire within the same minute
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Briefing scheduler error; retrying in 5 min")
            await asyncio.sleep(300)
