"""Morning briefing — the proactive 'wake up, here's your day' message.

Composes a Telegram message from: overnight inbound messages (grouped by channel),
today's school timetable (EduPage), next departures (RMV), and — if a key is set —
a short LLM-written intro. Everything degrades gracefully: sections whose source is
unconfigured are simply omitted.

Ein Scheduler (immer gestartet, siehe main.py) feuert einmal täglich zur Briefing-Zeit:
`app_settings["briefing"]["time"]` (Web-Einstellung, Admin → Display) mit Fallback auf
ASTRA_BRIEFING_TIME. Ziele werden pro Tag neu bestimmt:
  • Telegram — wenn `app_settings["briefing"]["telegram"]` (Fallback ASTRA_BRIEFING_ENABLED)
    und ein Bot konfiguriert ist,
  • OpenBoard-Display — wenn `app_settings["display"]["display_briefing"]` an ist und ein
    Display verbunden ist (Karten + gesprochene Kurzfassung).
Manuell: POST /briefing/run.
"""
from __future__ import annotations

import asyncio
import logging
import re
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


async def _intro(sections: list[str], *, channel: str = "telegram") -> str:
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
        from . import usage
        with usage.tag(purpose="briefing", channel=channel, third_party=False):
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


# ─── Display (OpenBoard) ──────────────────────────────────────────────────────
_SKIP_FOR_DISPLAY = ("weather", "google_calendar")   # haben eigene Karten
_TG_BOLD = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")   # Telegram *fett* → Markdown **fett**
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F\u200d]")


def _plain(text: str) -> str:
    return re.sub(r"[ \t]+", " ", _EMOJI.sub("", re.sub(r"[*_`]", "", text or ""))).strip()


def weather_sentence(w: dict) -> str:
    now = (w or {}).get("now") or {}
    if now.get("temp") is None:
        return ""
    desc = str(now.get("description") or "").strip()
    out = f"Draußen sind es {now['temp']} Grad" + (f", {desc}" if desc else "")
    if now.get("high") is not None and now.get("high") != now.get("temp"):
        out += f", heute bis {now['high']} Grad"
    return out + "."


def _hhmm(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(_tz()).strftime("%H:%M")
    except ValueError:
        return ""


def calendar_sentence(events: list[dict]) -> str:
    if not events:
        return "Heute stehen keine Termine an."
    timed = [e for e in events if not e.get("all_day")]
    first = timed[0] if timed else events[0]
    when = "ganztägig" if first.get("all_day") else f"um {_hhmm(first.get('start', ''))} Uhr"
    if len(events) == 1:
        return f"Heute hast du einen Termin: {first.get('title')} {when}."
    return f"Heute hast du {len(events)} Termine, der erste {when}: {first.get('title')}."


async def compose_spoken() -> tuple[str, list[dict]]:
    """Kurzes, gesprochenes Briefing + Karten (Wetter, Kalender, Rest als Markdown)."""
    from .display import cards as cardlib
    from .display import service as display
    cards: list[dict] = []
    spoken: list[str] = []
    rest: list[str] = []
    weather = await display.weather_data()
    if weather:
        cards.append(cardlib.weather_card(weather))
        spoken.append(weather_sentence(weather))
    events = await display.today_events()
    if events is not None:
        cards.append(cardlib.calendar_card(events))
        spoken.append(calendar_sentence(events))
    try:
        rest.append(await _overnight_section())
    except Exception as e:  # noqa: BLE001
        log.warning("overnight section failed: %s", e)
    for p in get_manager().enabled():
        if p.base_slug in _SKIP_FOR_DISPLAY:
            continue
        try:
            sec = await p.briefing_section()
        except Exception as e:  # noqa: BLE001
            log.warning("briefing_section failed for %s: %s", p.slug, e)
            sec = None
        if sec:
            rest.append(sec)
    rest = [r for r in rest if r]
    if rest:
        cards.append({"id": "briefing-rest", "type": "markdown", "title": "Briefing",
                      "data": {"text": "\n\n".join(_TG_BOLD.sub(r"**\1**", r) for r in rest)}})
    intro = _plain(await _intro(spoken + rest, channel="display"))
    text = " ".join(x for x in [intro, *spoken] if x)
    return text, [cardlib.validate_card(c) for c in cards]


async def send_display() -> bool:
    """Briefing aufs Display: Karten ersetzen den Bildschirm, dann die gesprochene Fassung."""
    from .display import service as display
    from .display import speech
    if not display.connected():
        log.info("Briefing: kein Display verbunden — übersprungen.")
        return False
    text, cards = await compose_spoken()
    display.publish("cards", {"cards": cards, "replace": True})
    payload: dict = {"text": text}
    if sp := await speech.synthesize(text):
        payload["speech"] = sp
    ok = display.publish("say", payload) > 0
    await db.audit("briefing_sent", channel="display", detail={"ok": ok, "cards": len(cards)})
    return ok


# ─── Einstellungen ────────────────────────────────────────────────────────────
def valid_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(value or "").strip()))


async def briefing_settings() -> dict:
    """{time, telegram, display} — Web-Einstellung vor .env (ASTRA_BRIEFING_TIME/_ENABLED)."""
    s = get_settings()
    try:
        appset = await db.get_setting("app_settings", {}) or {}
    except Exception:  # noqa: BLE001
        appset = {}
    b = appset.get("briefing") if isinstance(appset.get("briefing"), dict) else {}
    d = appset.get("display") if isinstance(appset.get("display"), dict) else {}
    at = str(b.get("time") or "").strip()
    return {
        "time": at if valid_hhmm(at) else s.astra_briefing_time,
        "telegram": bool(b["telegram"]) if "telegram" in b else bool(s.astra_briefing_enabled),
        "display": bool(d.get("display_briefing")),
    }


async def run_scheduled(cfg: dict | None = None) -> dict:
    """Ein geplanter Lauf: an jedes eingeschaltete und erreichbare Ziel."""
    from .display import service as display
    s = get_settings()
    cfg = cfg or await briefing_settings()
    out: dict = {}
    if cfg.get("telegram") and s.telegram_enabled and s.briefing_chat:
        out["telegram"] = await send()
    if cfg.get("display") and display.connected():
        out["display"] = await send_display()
    if not out:
        log.info("Briefing fällig, aber kein Ziel aktiv/erreichbar.")
    return out


# ─── Scheduler ────────────────────────────────────────────────────────────────
def due(now: datetime, target: time, last_date=None, *, window_min: int = 2) -> bool:
    """Fällig, wenn `now` im Fenster [target, target+window) liegt und heute noch nicht gelaufen."""
    if last_date == now.date():
        return False
    start = datetime.combine(now.date(), target, tzinfo=now.tzinfo)
    return start <= now < start + timedelta(minutes=window_min)


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
    """Prüft alle 20 s, ob die Briefing-Zeit erreicht ist (Zeit/Ziele live aus den Einstellungen).

    Läuft unabhängig davon, ob Telegram konfiguriert ist — ein verbundenes Display reicht."""
    log.info("Briefing scheduler armed (Zeit/Ziele aus Admin → Display, Fallback .env).")
    last = None
    while True:
        try:
            cfg = await briefing_settings()
            now = datetime.now(_tz())
            if due(now, _parse_time(cfg["time"]), last):
                last = now.date()
                if cfg["telegram"] or cfg["display"]:
                    log.info("Briefing scheduler: composing & sending (%s).", cfg)
                    await run_scheduled(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Briefing scheduler error; retrying shortly")
        await asyncio.sleep(20)
