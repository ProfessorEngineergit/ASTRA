"""OpenBoard-Display: Token, Einstellungen, Sitzungen, Gesprächs-Turn, Glance, Alarme.

Das Display ist ein Owner-Kanal: Wer den Display-Token hat, spricht als Bahrian
(Register OWNER bzw. VOICE). Darum ist der Token eigenständig — weder Admin-Cookie
noch X-Astra-Secret öffnen /display/* — und wird zeitkonstant verglichen.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .. import __version__, db
from ..config import get_settings
from . import cards as cardlib
from . import speech
from .hub import get_hub

log = logging.getLogger("astra.display")

TOKEN_KEY = "display_token"
SESSION_PREFIX = "display_session:"
MAX_STORED = 80          # Nachrichten pro Display-Sitzung (wie Web-Chats)
MAX_CONTEXT = 40         # davon gehen so viele an das Modell
MAX_AUDIO_B64 = 14_000_000   # ≈ 10 MB Audio
SOUNDS = ("gentle", "classic", "none")
DEFAULTS = {"display_briefing": False, "notify": True}

_AUDIO_NAMES = {
    "audio/wav": "voice.wav", "audio/x-wav": "voice.wav", "audio/wave": "voice.wav",
    "audio/webm": "voice.webm", "audio/ogg": "voice.ogg", "audio/mpeg": "voice.mp3",
    "audio/mp3": "voice.mp3", "audio/mp4": "voice.m4a", "audio/m4a": "voice.m4a",
    "audio/flac": "voice.flac",
}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().astra_timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


# ─── Token ────────────────────────────────────────────────────────────────────
_token_cache: str | None = None


def token_source() -> str:
    """'env' wenn ASTRA_DISPLAY_TOKEN gesetzt ist (dann im Admin nicht änderbar), sonst 'store'."""
    return "env" if get_settings().astra_display_token.strip() else "store"


async def display_token(*, create: bool = True) -> str:
    """Der gültige Display-Token. Ohne .env-Wert: verschlüsselt in settings, beim ersten Bedarf erzeugt."""
    global _token_cache
    env = get_settings().astra_display_token.strip()
    if env:
        return env
    if _token_cache:
        return _token_cache
    from ..config_store import get_config_store
    store = get_config_store()
    raw = await db.get_setting(TOKEN_KEY, None)
    token = store.decrypt(raw) if raw else ""
    if not token and create:
        if raw:
            log.warning("Display-Token nicht entschlüsselbar (Config-Key geändert?) — erzeuge neuen.")
        token = secrets.token_urlsafe(32)
        await db.set_setting(TOKEN_KEY, store.encrypt(token))
        await db.audit("display_token_created", actor="astra")
    _token_cache = token or None
    return token


async def regenerate_token() -> str:
    global _token_cache
    if token_source() == "env":
        raise RuntimeError("Token kommt aus ASTRA_DISPLAY_TOKEN und wird dort geändert.")
    from ..config_store import get_config_store
    token = secrets.token_urlsafe(32)
    await db.set_setting(TOKEN_KEY, get_config_store().encrypt(token))
    _token_cache = token
    await db.audit("display_token_regenerated", actor="owner")
    return token


def tokens_equal(presented: str, expected: str) -> bool:
    """Zeitkonstanter Vergleich (gleiche Länge per SHA-256, dann compare_digest)."""
    if not presented or not expected:
        return False
    a = hashlib.sha256(presented.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


async def check_token(presented: str) -> bool:
    try:
        expected = await display_token()
    except Exception:  # noqa: BLE001 — ohne DB kein Token, also kein Zugang
        log.warning("Display-Token nicht lesbar.", exc_info=True)
        return False
    return tokens_equal(presented, expected)


def _reset_for_tests() -> None:
    global _token_cache
    _token_cache = None
    _cache.clear()
    _locks.clear()


# ─── Einstellungen ────────────────────────────────────────────────────────────
async def display_settings() -> dict:
    try:
        appset = await db.get_setting("app_settings", {}) or {}
    except Exception:  # noqa: BLE001
        appset = {}
    cfg = appset.get("display") if isinstance(appset.get("display"), dict) else {}
    return {**DEFAULTS, **cfg}


async def save_display_settings(patch: dict) -> dict:
    appset = await db.get_setting("app_settings", {}) or {}
    cfg = appset.get("display") if isinstance(appset.get("display"), dict) else {}
    cfg = {**cfg, **patch}
    appset["display"] = cfg
    await db.set_setting("app_settings", appset)
    return {**DEFAULTS, **cfg}


# ─── Kleiner TTL-Cache (Glance wird gepollt; Wetter/Kalender nicht hämmern) ───
_cache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, fn: Callable[[], Awaitable[Any]]) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await fn()
    _cache[key] = (time.monotonic(), value)
    return value


def _plugin(slug: str):
    try:
        from ..plugins.registry import get_manager
        p = get_manager().get(slug)
        return p if p is not None and p.enabled else None
    except Exception:  # noqa: BLE001
        return None


async def weather_data() -> dict | None:
    """WeatherData der konfigurierten Stadt (10 min Cache) oder None."""
    plugin = _plugin("weather")
    if plugin is None or not hasattr(plugin, "weather_data"):
        return None

    async def load():
        try:
            return await plugin.weather_data()
        except Exception as e:  # noqa: BLE001
            log.info("Wetter für Display nicht verfügbar: %s", e)
            return None
    return await _cached("weather", 600, load)


async def today_events() -> list[dict] | None:
    """Heutige Termine (normalisiert, 5 min Cache) oder None ohne Kalender."""
    plugin = _plugin("google_calendar")
    if plugin is None or not hasattr(plugin, "today"):
        return None

    async def load():
        try:
            raw = await plugin.today()
        except Exception as e:  # noqa: BLE001
            log.info("Kalender für Display nicht verfügbar: %s", e)
            return None
        return [n for n in (cardlib.normalize_event(e) for e in raw or []) if n]
    return await _cached("calendar", 300, load)


# ─── Zustellung ───────────────────────────────────────────────────────────────
def publish(event: str, data: dict) -> int:
    """Ereignis an alle Displays (Dateiverweise werden zu data:-URLs). Anzahl Empfänger."""
    return get_hub().publish(event, cardlib.materialize(data))


def connected() -> int:
    return get_hub().connected


async def deliver_text(text: str) -> bool:
    """Channels.send('display', …): Text als `say`-Ereignis. False, wenn kein Display da ist."""
    if not connected():
        return False
    return publish("say", {"text": str(text or "")}) > 0


async def notify_target() -> bool:
    """Darf der notify-Router das Display nutzen? (verbunden + Einstellung an)"""
    if not connected():
        return False
    return bool((await display_settings()).get("notify", True))


async def notify_display(text: str, *, title: str = "", urgent: bool = False) -> bool:
    if not connected():
        return False
    payload: dict[str, Any] = {"text": (f"{title}: {text}" if title else text)}
    if urgent and (sp := await speech.synthesize(payload["text"])):
        payload["speech"] = sp
    return publish("say", payload) > 0


# ─── Gesprächs-Turn (POST /display/v1/message) ────────────────────────────────
_locks: dict[str, asyncio.Lock] = {}

DISPLAY_HINT = (
    "Kanal: OpenBoard — {owner}s Wand-Touch-Display zu Hause. Aktive App: {app}.\n"
    "- Ergebnisse von Kalender-, Wetter- und Smart-Home-Tools erscheinen automatisch als Karten "
    "auf dem Display. Fasse in deiner Antwort nur das Wichtigste zusammen, statt alles aufzuzählen.\n"
    "- Für eigene Inhalte (Erklärung, Liste, Tabelle, Skizze) nutze display_show; fürs Whiteboard "
    "display_board; zum Öffnen einer App display_open_app.\n"
    "- Wecker/Alarme auf dem Display sind Regeln mit einer display-Aktion: lege sie mit "
    "display_alarm_set an (einmalig mit Datum, sonst mit Wochentagen)."
)
SPOKEN_HINT = (
    "Deine Antwort wird auf dem Display VORGELESEN: 1–3 kurze, gesprochene Sätze. Kein Markdown, "
    "keine Listen, keine URLs, keine Emojis. Details stehen auf den Karten."
)


def clean_session_id(raw: Any) -> str:
    sid = re.sub(r"[^A-Za-z0-9_.:-]", "", str(raw or ""))[:64]
    return sid or "display-main"


async def load_session(sid: str) -> dict:
    try:
        data = await db.get_setting(SESSION_PREFIX + sid, None)
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        data = {"id": sid, "messages": []}
    return data


async def save_session(sid: str, data: dict) -> None:
    data["messages"] = data.get("messages", [])[-MAX_STORED:]
    data["updated_at"] = datetime.now(_tz()).isoformat()
    await db.set_setting(SESSION_PREFIX + sid, data)


class BadRequest(ValueError):
    """Ungültige Anfrage (→ HTTP 400/413)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def transcribe(audio: dict) -> str:
    if not isinstance(audio, dict) or not audio.get("b64"):
        raise BadRequest("audio braucht {mime, b64}.")
    b64 = str(audio.get("b64"))
    if len(b64) > MAX_AUDIO_B64:
        raise BadRequest("Audio ist zu groß (max. ~10 MB).", 413)
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest("audio.b64 ist kein gültiges Base64.") from None
    mime = str(audio.get("mime") or "audio/wav").split(";", 1)[0].strip().lower()
    from ..integrations.transcription import get_transcriber
    tr = get_transcriber()
    if not tr.enabled:
        return ""
    return await tr.transcribe(raw, filename=_AUDIO_NAMES.get(mime, "voice.wav"))


async def handle_message(body: dict) -> dict:
    """Ein Gesprächs-Turn vom Display: Text/Audio → Agent → Antwort + Karten + Sprache."""
    from .. import agent
    from ..persona import Register

    if not isinstance(body, dict):
        raise BadRequest("JSON-Objekt erwartet.")
    sid = clean_session_id(body.get("session_id"))
    speak = bool(body.get("speak"))
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    text = str(body.get("text") or "").strip()
    if not text and body.get("audio"):
        text = (await transcribe(body["audio"])).strip()
        if not text:
            reply = ("Ich habe dich leider nicht verstanden." if speech.available()
                     else "Spracherkennung ist nicht eingerichtet.")
            return {"session_id": sid, "transcript": "", "reply": reply, "cards": [],
                    "speech": await speech.synthesize(reply) if speak else None, "actions": []}
    if not text:
        raise BadRequest("text oder audio fehlt.")
    text = text[:8000]

    s = get_settings()
    lock = _locks.setdefault(sid, asyncio.Lock())
    async with lock:
        session = await load_session(sid)
        now_iso = datetime.now(_tz()).isoformat()
        session["messages"].append({"role": "user", "content": text, "ts": now_iso})
        history = [{"role": m["role"], "content": m["content"]}
                   for m in session["messages"] if m.get("role") in ("user", "assistant")][-MAX_CONTEXT:]
        extra = DISPLAY_HINT.format(owner=s.astra_owner_name,
                                    app=str(context.get("active_app") or "unbekannt")[:40])
        if speak:
            extra += "\n" + SPOKEN_HINT
        try:
            result = await agent.generate_reply_meta(
                register=Register.VOICE if speak else Register.OWNER,
                contact={"id": "owner", "name": s.astra_owner_name, "is_owner": True},
                thread_id=f"display:{sid}", channel="display", history=history,
                max_sensitivity="details", extra_system=extra, permission_mode="auto",
                chat_id=f"display:{sid}",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Display-Turn fehlgeschlagen")
            result = {"reply": f"Da ist etwas schiefgegangen: {e}", "tool_calls": []}
        reply = str(result.get("reply") or "").strip() or "(keine Antwort)"
        cards, actions = cardlib.extract(result.get("tool_calls"))
        stored = {"role": "assistant", "content": reply, "ts": datetime.now(_tz()).isoformat()}
        if cards:
            stored["cards"] = [{"type": c["type"], "title": c.get("title", "")} for c in cards]
        session["messages"].append(stored)
        await save_session(sid, session)

    spoken = await speech.synthesize(reply) if speak else None
    try:
        await db.audit("display_turn", actor="owner", channel="display", thread_id=f"display:{sid}",
                       detail={"len": len(text), "cards": len(cards), "speak": speak,
                               "tools": [c.get("tool") for c in result.get("tool_calls") or []][:10]})
    except Exception:  # noqa: BLE001
        pass
    try:
        out_cards = cardlib.materialize(cards)
    except cardlib.CardError:
        out_cards = [c for c in cards if c["type"] != "image"]
    return {"session_id": sid, "transcript": text, "reply": reply, "cards": out_cards,
            "speech": spoken, "actions": actions}


# ─── Glance (GET /display/v1/glance) ──────────────────────────────────────────
def greeting(now: datetime, owner: str) -> str:
    h = now.hour
    if 5 <= h < 11:
        word = "Guten Morgen"
    elif 11 <= h < 17:
        word = "Hallo"
    elif 17 <= h < 22:
        word = "Guten Abend"
    else:
        word = "Gute Nacht"
    return f"{word}, {owner}" if owner else word


_MD = re.compile(r"[*_`]")


async def _briefing_text() -> str | None:
    """Kurztext aus den Briefing-Abschnitten der Plugins (ohne Wetter/Kalender — die haben eigene Felder)."""
    async def load():
        try:
            from ..plugins.registry import get_manager
            lines = []
            for p in get_manager().enabled():
                if p.base_slug in ("weather", "google_calendar"):
                    continue
                try:
                    sec = await asyncio.wait_for(p.briefing_section(), timeout=8)
                except Exception:  # noqa: BLE001
                    sec = None
                if sec:
                    lines.append(_MD.sub("", sec).strip())
            return "\n".join(lines) or None
        except Exception:  # noqa: BLE001
            return None
    return await _cached("briefing", 900, load)


def _is_alarm_rule(rule: dict) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "display" and (a.get("event") or "alarm") == "alarm"
               for a in rule.get("actions") or [])


def _alarm_label(rule: dict) -> str:
    for a in rule.get("actions") or []:
        if isinstance(a, dict) and a.get("type") == "display":
            data = a.get("data") if isinstance(a.get("data"), dict) else {}
            return str(data.get("label") or a.get("label") or rule.get("name") or "Wecker")
    return str(rule.get("name") or "Wecker")


async def upcoming_alarms(now: datetime | None = None, *, horizon_days: int = 7, limit: int = 5) -> list[dict]:
    from ..rules import next_occurrence
    now = now or datetime.now(_tz())
    try:
        rules = await db.list_rules()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rules:
        if not (r.get("enabled") and r.get("confirmed_at")) or not _is_alarm_rule(r):
            continue
        at = next_occurrence(r.get("trigger") or {}, now, horizon_days=horizon_days)
        if at:
            out.append({"id": f"rule-{r['id']}", "at": at.isoformat(), "label": _alarm_label(r)})
    out.sort(key=lambda a: a["at"])
    return out[:limit]


async def glance() -> dict:
    s = get_settings()
    now = datetime.now(_tz())

    async def safe(coro, default=None):
        try:
            return await coro
        except Exception:  # noqa: BLE001
            log.debug("glance part failed", exc_info=True)
            return default

    weather, events, brief, alarms = await asyncio.gather(
        safe(weather_data()), safe(today_events()), safe(_briefing_text()), safe(upcoming_alarms(now), []))
    return {
        "generated_at": now.isoformat(),
        "greeting": greeting(now, s.astra_owner_name),
        "weather": weather,
        "calendar": {"events": events} if events is not None else None,
        "briefing": {"text": brief} if brief else None,
        "alarms": alarms or [],
    }


def hello() -> dict:
    from ..integrations.transcription import get_transcriber
    caps = ["message", "glance", "events", "cards", "alarms", "board", "commands"]
    if get_transcriber().enabled:
        caps.append("audio")
    if speech.available():
        caps.append("tts")
    if _plugin("image_generation") is not None:
        caps.append("image")
    return {"name": "ASTRA", "version": __version__, "capabilities": caps}


# ─── Alarme & Regel-Aktion `display` ──────────────────────────────────────────
async def build_alarm(data: dict, *, alarm_id: str) -> dict:
    label = str(data.get("label") or "Wecker")[:120]
    sound = data.get("sound") if data.get("sound") in SOUNDS else "gentle"
    speak = str(data.get("speak") or "").strip()
    payload: dict[str, Any] = {"id": alarm_id, "label": label, "sound": sound}
    alarm_cards: list[dict] = []
    if data.get("briefing"):
        from .. import briefing
        try:
            text, alarm_cards = await briefing.compose_spoken()
            speak = f"{speak} {text}".strip()
        except Exception:  # noqa: BLE001
            log.warning("Briefing für Wecker fehlgeschlagen.", exc_info=True)
    if speak:
        payload["speak"] = speak
        if sp := await speech.synthesize(speak):
            payload["speech"] = sp
    if alarm_cards:
        payload["cards"] = alarm_cards
    return payload


def _action_data(action: dict) -> dict:
    data = dict(action.get("data")) if isinstance(action.get("data"), dict) else {}
    for k in ("label", "sound", "speak", "briefing", "text", "app", "action", "card", "cards", "replace"):
        if k in action and k not in data:
            data[k] = action[k]
    return data


async def run_rule_action(action: dict, *, rule_id: Any = None, principal: str = "") -> dict:
    """Regel-Aktion {"type":"display","event":"alarm|say|card|cards|command|board","data":{…}}."""
    event = str(action.get("event") or "alarm")
    data = _action_data(action)
    online = connected() > 0
    if event == "alarm":
        label = str(data.get("label") or "Wecker")
        if not online:
            from .. import notify as notify_mod
            res = await notify_mod.notify(f"⏰ {label}", urgency="urgent", principal=principal)
            return {"ok": False, "summary": "kein Display verbunden — per notify zugestellt", "detail": res}
        stamp = datetime.now(_tz()).strftime("%Y%m%d%H%M")
        payload = await build_alarm(data, alarm_id=f"rule-{rule_id}-{stamp}" if rule_id else f"alarm-{stamp}")
        n = publish("alarm", payload)
        return {"ok": n > 0, "summary": f"Wecker an {n} Display(s)"}
    if not online:
        return {"ok": False, "summary": "kein Display verbunden"}
    if event == "say":
        text = str(data.get("text") or data.get("speak") or "").strip()
        if not text:
            return {"ok": False, "summary": "say braucht text"}
        payload = {"text": text}
        if sp := await speech.synthesize(text):
            payload["speech"] = sp
        n = publish("say", payload)
    elif event == "card":
        n = publish("card", {"card": cardlib.validate_card(data.get("card") or data)})
    elif event == "cards":
        cs = [cardlib.validate_card(c) for c in data.get("cards") or []]
        n = publish("cards", {"cards": cs, "replace": bool(data.get("replace"))})
    elif event == "command":
        cmd = command_payload(str(data.get("action") or ""), data.get("app"))
        n = publish("command", cmd)
    elif event == "board":
        n = publish("board", cardlib.validate_board_op(data))
    else:
        return {"ok": False, "summary": f"unbekanntes Display-Ereignis {event}"}
    return {"ok": n > 0, "summary": f"{event} an {n} Display(s)"}


def command_payload(action: str, app: Any = None) -> dict:
    if action not in ("sleep", "wake", "open_app"):
        raise cardlib.CardError("action muss sleep, wake oder open_app sein.")
    out: dict[str, Any] = {"action": action}
    if action == "open_app":
        app_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(app or ""))[:64]
        if not app_id:
            raise cardlib.CardError("open_app braucht eine App-ID (z. B. board, home, gev).")
        out["app"] = app_id
    return out


def alarm_rule(*, at: str, label: str, days: list[int] | None = None, date: str = "",
               sound: str = "gentle", briefing: bool = True, speak: str = "") -> dict:
    """Regel-Gerüst „Wecker“ (auch als Plugin-Vorlage genutzt)."""
    trigger: dict[str, Any] = {"type": "schedule", "at": at}
    if date:
        trigger["date"] = date
    elif days:
        trigger["days"] = sorted({int(d) for d in days if 0 <= int(d) <= 6})
    data: dict[str, Any] = {"label": label, "sound": sound if sound in SOUNDS else "gentle",
                            "briefing": bool(briefing)}
    if speak:
        data["speak"] = speak
    return {"name": f"Wecker {at}" + (f" · {label}" if label and label != "Wecker" else ""),
            "plugin_slug": "display", "trigger": trigger, "condition": {"type": "always"},
            "actions": [{"type": "display", "event": "alarm", "data": data}]}
