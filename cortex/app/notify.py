"""Zustellungs-Router — EIN Weg, ASTRA proaktiv zu Wort kommen zu lassen.

Heute ruft alles direkt `channels.send_telegram`. Für proaktive Meldungen
(Zugausfall, offene Aufgabe, Terminkonflikt — die Nutzer der Regelschicht in W4)
soll stattdessen die Dringlichkeit UND die Anwesenheit entscheiden, wohin es geht:

  control → Telegram (Freigaben, Buttons — unverändert)
  normal  → Push auf die HA-Companion-App (iOS + Android)
  urgent  → zusätzlich gesprochen auf dem Lautsprecher, wenn zu Hause & wach

Push-Fehlschlag fällt auf Telegram zurück. Ohne HA/Push konfiguriert bleibt immer
Telegram als Sockel. Die Kanalwahl ist als reine Funktion herausgezogen, damit sie
ohne HA testbar ist.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import db
from .channels import get_channels
from .config import get_settings

log = logging.getLogger("astra.notify")

CONTROL = "control"
NORMAL = "normal"
URGENT = "urgent"


def choose_channels(urgency: str, *, at_home: bool | None, awake: bool) -> list[str]:
    """Reine Routing-Entscheidung. `at_home=None` = unbekannt (dann kein Speaker)."""
    if urgency == CONTROL:
        return ["telegram"]
    channels = ["push"]
    if urgency == URGENT and at_home is True and awake:
        channels.append("speak")
    return channels


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        h, m = str(value).split(":", 1)
        return time(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return fallback


def is_awake(settings: dict, *, now: datetime | None = None, tz: str = "Europe/Berlin") -> bool:
    """Grobe Wach-Heuristik aus einem konfigurierbaren Zeitfenster (Default 7–23)."""
    try:
        local = now or datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        local = now or datetime.now()
    start = _parse_hhmm(settings.get("awake_start", "07:00"), time(7, 0))
    end = _parse_hhmm(settings.get("awake_end", "23:00"), time(23, 0))
    t = local.time()
    return start <= t <= end if start <= end else (t >= start or t <= end)


def _ha():
    """The enabled Home Assistant plugin instance, or None."""
    try:
        from .plugins.registry import get_manager
        ha = get_manager().get("home_assistant")
        return ha if ha and ha.enabled else None
    except Exception:  # noqa: BLE001
        return None


async def _notify_settings(principal: str) -> dict:
    appset = await db.get_principal_setting("app_settings", principal, {}) or {}
    return (appset.get("notify") or {})


async def _presence(principal: str) -> tuple[bool | None, bool]:
    """(at_home, awake) for a principal. Unknown presence → (None, awake-by-time)."""
    settings = await _notify_settings(principal)
    awake = is_awake(settings, tz=get_settings().astra_timezone)
    ha = _ha()
    if not ha:
        return None, awake
    try:
        pres = await ha.presence()
        return pres.get("at_home"), awake
    except Exception:  # noqa: BLE001
        log.debug("presence lookup failed", exc_info=True)
        return None, awake


async def _owner_chat(principal: str) -> str:
    """Telegram chat id for a principal (default → configured owner)."""
    try:
        p = await db.get_principal(principal)
        if p and p.get("telegram_chat_id"):
            return str(p["telegram_chat_id"])
    except Exception:  # noqa: BLE001
        pass
    return str(get_settings().telegram_owner_chat_id or "")


async def _push(text: str, title: str, actions: list[dict] | None) -> bool:
    ha = _ha()
    if not ha:
        return False
    try:
        return await ha.notify_push(text, title=title, actions=actions)
    except Exception:  # noqa: BLE001
        log.debug("push failed", exc_info=True)
        return False


async def _speak(text: str, where: str) -> bool:
    ha = _ha()
    if not ha:
        return False
    try:
        from . import world
        res = await world.resolve(where, kinds=("media_player",)) if where else None
        speaker = res.node.id if (res and res.ok) else ""
        if not speaker:
            # No specific/only speaker resolved → let HA's configured default handle it.
            return False
        return await ha.speak(text, media_player=speaker)
    except Exception:  # noqa: BLE001
        log.debug("speak failed", exc_info=True)
        return False


async def _telegram(text: str, principal: str, actions: list[dict] | None) -> bool:
    chat = await _owner_chat(principal)
    if not chat:
        return False
    buttons = [{"text": a.get("title", a.get("action", "OK")),
                "callback_data": a.get("callback_data", a.get("action", "ok"))}
               for a in (actions or [])] or None
    return await get_channels().send_telegram(chat, text, buttons=buttons)


async def notify(
    text: str,
    *,
    urgency: str = NORMAL,
    principal: str = "",
    where: str = "",
    title: str = "",
    actions: list[dict] | None = None,
) -> dict:
    """Deliver a proactive message on the channels that fit urgency + presence.

    Returns {channel: ok} for every channel attempted. Never raises — a dead HA
    just means Telegram carries the message."""
    at_home, awake = await _presence(principal)
    wanted = choose_channels(urgency, at_home=at_home, awake=awake)
    results: dict[str, bool] = {}

    if "telegram" in wanted:
        results["telegram"] = await _telegram(text, principal, actions)
    if "push" in wanted:
        ok = await _push(text, title, actions)
        results["push"] = ok
        if not ok:   # push unavailable/failed → make sure it still reaches him
            results["telegram_fallback"] = await _telegram(text, principal, actions)
    if "speak" in wanted:
        results["speak"] = await _speak(text, where)

    await db.audit("notify", detail={"urgency": urgency, "where": where,
                                     "at_home": at_home, "awake": awake, "results": results})
    return results
