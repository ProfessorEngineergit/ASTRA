"""Moderations-Gate — verbindet die reine Logik (moderation.py) mit Zustand und Welt.

Ablauf für JEDE Nachricht eines Dritten, bevor irgendein LLM-Token bezahlt wird:
  1. stumm geschaltet? → still verwerfen (Eskalationsleiter, Stufe „stumm")
  2. Regeln (+ optional OpenAI-Moderation als zweite Meinung) → Verdict
  3. Strike verbuchen → aktueller Stil der Person (normal/bestimmt/überheblich) für DIESE
     Antwort ist der Stand VOR dem Strike, der neue Stand gilt ab der nächsten Nachricht
  4. Bahrian benachrichtigen, wenn es ernst ist (Drohung/Hass/Selbstgefährdung/Stummschaltung)
  5. Audit-Eintrag — damit man in der UI sieht, was abgewehrt wurde
Der Zustand liegt in der settings-Tabelle unter `modstate:<kanal>:<handle>`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import db, moderation
from .channels import get_channels
from .config import get_settings

log = logging.getLogger("astra.moderation")


@dataclass
class GateResult:
    verdict: moderation.Verdict = field(default_factory=moderation.Verdict)
    escalation: moderation.Escalation | None = None
    muted: bool = False
    style: str = "normal"       # Stil, in dem auf DIESE Nachricht geantwortet wird

    @property
    def stop(self) -> bool:
        return self.muted or self.verdict.stop


def _key(channel: str, handle: str) -> str:
    return f"modstate:{channel}:{handle}"


async def load_state(channel: str, handle: str) -> dict:
    try:
        return await db.get_setting(_key(channel, handle), {}) or {}
    except Exception:  # noqa: BLE001
        return {}


async def save_state(channel: str, handle: str, state: dict) -> None:
    try:
        await db.set_setting(_key(channel, handle), state)
    except Exception:  # noqa: BLE001
        log.debug("could not persist moderation state", exc_info=True)


async def reset_state(channel: str, handle: str) -> None:
    await save_state(channel, handle, {})


async def alert_owner(channel: str, contact: dict, verdict: moderation.Verdict, text: str,
                      *, note: str = "") -> None:
    """Ernste Fälle an Bahrian: Telegram immer, dazu Push/Lautsprecher nach Dringlichkeit."""
    s = get_settings()
    name = contact.get("display_name") or contact.get("handle") or "Unbekannt"
    label = {"waha": "WhatsApp", "signal": "Signal", "slack": "Slack", "email": "Mail"}.get(channel, channel)
    kinds = ", ".join(verdict.categories) or "Auffälligkeit"
    icon = "🚨" if moderation.SELF_HARM in verdict.categories or moderation.THREAT in verdict.categories else "⚠️"
    msg = (f"{icon} Moderation ({label}) — {name}: {kinds}\n„{text[:300]}“"
           + (f"\n{note}" if note else ""))
    try:
        if s.telegram_enabled and s.telegram_owner_chat_id:
            await get_channels().send_telegram(s.telegram_owner_chat_id, msg)
    except Exception:  # noqa: BLE001
        log.debug("owner telegram alert failed", exc_info=True)
    urgent = bool({moderation.SELF_HARM, moderation.THREAT} & set(verdict.categories))
    try:
        from . import notify as notify_mod
        await notify_mod.notify(msg, urgency="urgent" if urgent else "normal")
    except Exception:  # noqa: BLE001
        log.debug("owner push alert failed", exc_info=True)


async def gate_inbound(*, channel: str, handle: str, thread_id: str, contact: dict,
                       text: str, app_settings: dict, now: float | None = None) -> GateResult:
    """Eine Nachricht eines Dritten prüfen. Wirft nie — im Zweifel wird durchgelassen,
    weil die Regeln später (Ausgang, Rate-Limit, Budget) noch greifen."""
    try:
        return await _gate(channel, handle, thread_id, contact, text, app_settings,
                           now if now is not None else time.time())
    except Exception:  # noqa: BLE001
        log.exception("moderation gate failed — letting the message through")
        return GateResult()


async def _gate(channel, handle, thread_id, contact, text, app_settings, now) -> GateResult:
    cfg = moderation.settings(app_settings)
    if not cfg["enabled"]:
        return GateResult()
    state = await load_state(channel, handle)

    # 1) Stummgeschaltet: nichts kostet mehr Geld, nichts wird gesendet.
    if moderation.is_muted(state, now):
        await db.audit("moderation_muted", channel=channel, thread_id=thread_id,
                       contact_id=contact.get("id"),
                       detail={"until": state.get("muted_until")})
        return GateResult(muted=True, style=moderation.style_for(
            moderation.decay_strikes(state, now, cfg["ladder"]["decay_hours"]), cfg["ladder"]))

    # Stil VOR diesem Verstoß (erster Fehltritt bekommt die freundliche Antwort).
    strikes_before = moderation.decay_strikes(state, now, float(cfg["ladder"]["decay_hours"]))
    style = moderation.style_for(strikes_before, cfg["ladder"])

    # 2) Regeln; die kostenlose LLM-Zweitmeinung nur, wenn die Regeln nichts Hartes fanden.
    first = moderation.moderate_inbound(text, app_settings=app_settings, style=style)
    llm_cats: list[str] = []
    if cfg["llm"] and not first.stop and len(text) >= 4:
        llm_cats = await moderation.llm_flags(text)
    verdict = (moderation.moderate_inbound(text, app_settings=app_settings, style=style,
                                           llm_categories=llm_cats) if llm_cats else first)
    if not verdict.flagged:
        return GateResult(verdict=verdict, style=style)
    # Vertraute Kontakte (Tier ≤ 1): ein derber Spruch unter Freunden ist kein Verstoß.
    try:
        trusted = int(contact.get("trust_tier") or 3) <= 1
    except (TypeError, ValueError):
        trusted = False
    if trusted and set(verdict.categories) <= {moderation.HOSTILE}:
        return GateResult(verdict=verdict, style=style)

    # 3) Strike verbuchen und persistieren.
    new_state, esc = moderation.apply_strike(state, verdict, now, cfg["ladder"])
    await save_state(channel, handle, new_state)

    # 4) Audit + Benachrichtigung.
    await db.audit(
        "moderation_inbound", channel=channel, thread_id=thread_id, contact_id=contact.get("id"),
        detail={"action": verdict.action, "categories": list(verdict.categories),
                "severity": verdict.severity, "style_after": esc.style, "strikes": esc.strikes,
                "muted": esc.muted, "llm": bool(llm_cats), "preview": text[:120]})
    if esc.notify_owner or verdict.alert_owner:
        note = "Ab jetzt stumm geschaltet." if esc.muted else ""
        await alert_owner(channel, contact, verdict, text, note=note)

    # Antwort in dem Stil, den die Person VERDIENT hat: nach diesem Verstoß.
    return GateResult(verdict=verdict, escalation=esc, style=style)
