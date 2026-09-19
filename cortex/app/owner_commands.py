"""Schnellbefehle für Bahrian — Secretary per Telegram an/aus/auto/Pause, sofort.

Warum deterministisch und nicht über das LLM? „Secretary aus" muss in Millisekunden und
zu 100 % zuverlässig wirken, ohne Token und ohne dass ein Modell den Satz falsch versteht.
Der Parser ist rein (testbar); nur `execute()` schreibt die Einstellungen.

Erkannt werden (deutsch, locker formuliert):
    /secretary                     → Status + Knöpfe
    /secretary aus | an | auto
    /secretary aus 2h              → 2 Stunden aus, danach automatisch wie zuvor
    /secretary aus bis 18:30       → bis 18:30 Uhr aus
    „schalte den Sekretär aus“  ·  „secretary bis 18 uhr aus“  ·  „sekretär an für 3 stunden“
Nur kurze Nachrichten mit dem Wort Secretary/Sekretär (oder Slash-Befehl) zählen — normale
Unterhaltung wird nie gekapert.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_WORD = re.compile(r"\b(secretary|secretaer|sekretaer|sekretär|sekretaerin|sekretärin)\b", re.I)
_SLASH = re.compile(r"^\s*/(secretary|sekretaer|sekretär|sek|sec)\b\s*(.*)$", re.I)
_ON = re.compile(r"\b(an|ein|on|aktiv\w*|starte\w*|anschalten|einschalten|anmachen)\b", re.I)
_OFF = re.compile(r"\b(aus|off|ab|deaktiv\w*|stopp?\w*|ausschalten|ausmachen|pause|still|ruhe)\b", re.I)
_AUTO = re.compile(r"\b(auto|automatisch|automatik)\b", re.I)
_STATUS = re.compile(r"\b(status|wie steht|läuft|laeuft|aktiv\?)\b", re.I)
_DUR = re.compile(r"(\d+(?:[.,]\d+)?)\s*(min(?:uten?)?|m|h|std\.?|stunden?)\b", re.I)
_UNTIL = re.compile(r"\bbis\s+(?:(morgen)\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:uhr)?\b", re.I)
_UNTIL_TOMORROW = re.compile(r"\bbis\s+morgen\b(?!\s*\d)", re.I)


@dataclass(frozen=True)
class Command:
    action: str                       # on | off | auto | status
    minutes: float | None = None      # „für 2h“
    until_hhmm: tuple[int, int] | None = None
    tomorrow: bool = False            # „bis morgen 8“ / „bis morgen“


def parse(text: str) -> Command | None:
    """Text → Command oder None (= ganz normale Nachricht). Rein."""
    raw = (text or "").strip()
    if not raw or len(raw) > 90:
        return None
    body = raw
    m = _SLASH.match(raw)
    if m:
        body = m.group(2) or ""
    elif not _WORD.search(raw):
        return None

    minutes: float | None = None
    until: tuple[int, int] | None = None
    tomorrow = bool(_UNTIL_TOMORROW.search(body))
    d = _DUR.search(body)
    if d:
        val = float(d.group(1).replace(",", "."))
        minutes = val * (60 if d.group(2).lower().startswith(("h", "s")) else 1)
    u = _UNTIL.search(body)
    if u:
        hour = int(u.group(2))
        minute = int(u.group(3) or 0)
        if hour <= 24 and minute < 60:
            until = (hour % 24, minute)
            tomorrow = tomorrow or bool(u.group(1))
    # Für die Aktionswahl die Zeitangaben entfernen, damit „2h“/„18“ nichts verfälschen.
    words = _DUR.sub(" ", _UNTIL.sub(" ", body))

    if _AUTO.search(words):
        action = "auto"
    elif _OFF.search(words) and not re.search(r"\bnicht\s+(aus|ab)\b", words, re.I):
        action = "off"
    elif _ON.search(words):
        action = "on"
    elif _STATUS.search(words) or not words.strip(" ?!.:,"):
        action = "status"
    else:
        return None if not m else Command("status")
    if action == "status":
        return Command("status")
    # „auto“ kennt keine Frist; „an“/„aus“ dürfen befristet sein.
    if action == "auto":
        return Command("auto")
    return Command(action, minutes=minutes, until_hhmm=until, tomorrow=tomorrow)


def resolve_until(cmd: Command, now: datetime) -> datetime | None:
    """Endzeitpunkt der Befristung (lokale Zeit) oder None = unbefristet."""
    if cmd.minutes:
        return now + timedelta(minutes=cmd.minutes)
    if cmd.until_hhmm is not None:
        h, mi = cmd.until_hhmm
        end = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cmd.tomorrow or end <= now:
            end += timedelta(days=1)
        return end
    if cmd.tomorrow:
        return (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return None


def apply_to_settings(app_settings: dict, cmd: Command, now: datetime) -> tuple[dict, str]:
    """Befehl auf die Einstellungen anwenden. Rein: gibt (neue Einstellungen, Antworttext)."""
    s = dict(app_settings or {})
    sec = dict(s.get("secretary") or {})
    if cmd.action == "status":
        return s, ""
    if cmd.action == "auto":
        sec.pop("override", None)
        sec["activation_mode"] = "auto"
        sec["enabled"] = True
        s["secretary"] = sec
        return s, "Secretary: 🔄 AUTO — folgt deinem Stundenplan/Kalender."
    until = resolve_until(cmd, now)
    if until:
        sec["override"] = {"mode": cmd.action, "until": until.isoformat()}
        s["secretary"] = sec
        word = "aus 🔕" if cmd.action == "off" else "an ✅"
        return s, (f"Secretary {word} bis {until:%H:%M}"
                   f"{' morgen' if until.date() != now.date() else ''} — danach wieder wie zuvor.")
    sec.pop("override", None)
    sec["activation_mode"] = cmd.action
    sec["enabled"] = cmd.action != "off"
    s["secretary"] = sec
    return s, ("Secretary: ✅ AN — antwortet immer." if cmd.action == "on"
               else "Secretary: 🔕 AUS — ich antworte niemandem, bis du es wieder einschaltest.")


def buttons() -> list[dict]:
    return [{"text": "✅ An", "callback_data": "sec:on"},
            {"text": "🔄 Auto", "callback_data": "sec:auto"},
            {"text": "🔕 Aus", "callback_data": "sec:off"},
            {"text": "⏸ 1 h Pause", "callback_data": "sec:pause60"}]


def command_from_callback(data: str) -> Command | None:
    """`sec:on|off|auto|pause60|status` → Command (Knöpfe unter der Statusnachricht)."""
    if not data.startswith("sec:"):
        return None
    what = data[4:]
    if what in ("on", "off", "auto", "status"):
        return Command(what)
    if what.startswith("pause"):
        try:
            return Command("off", minutes=float(what[5:] or 60))
        except ValueError:
            return None
    return None


async def status_text(app_settings: dict, timezone: str) -> str:
    """Kurzer, ehrlicher Status: Modus, aktuell aktiv?, Pause, Schattenmodus."""
    from . import secretary
    st = secretary.secretary_settings(app_settings)
    live = await secretary.resolve_service_status(app_settings, timezone)
    mode = {"auto": "🔄 AUTO", "on": "✅ AN", "off": "🔕 AUS"}[st["activation_mode"]]
    lines = [f"Secretary: {mode}"]
    if st.get("override"):
        until = st["override"]["until"].astimezone(ZoneInfo(timezone))
        lines.append(f"⏱ Übersteuerung bis {until:%H:%M} (danach: {st['base_mode'].upper()})")
    lines.append(("Gerade aktiv" if live.active else "Gerade inaktiv")
                 + f" — {live.reason} ({live.source})")
    shadow = (app_settings.get("secretary") or {})
    if shadow.get("shadow_all") or any((shadow.get("shadow") or {}).values()):
        lines.append("🕶 Schattenmodus an: Antworten gehen zur Kontrolle an dich.")
    return "\n".join(lines)


async def execute(cmd: Command, *, timezone: str) -> tuple[str, bool]:
    """Befehl ausführen, Einstellungen speichern und live anwenden. → (Antwort, mit Knöpfen?)"""
    from . import db
    now = datetime.now(ZoneInfo(timezone))
    app = await db.get_setting("app_settings", {}) or {}
    if cmd.action == "status":
        return await status_text(app, timezone), True
    new, reply = apply_to_settings(app, cmd, now)
    await db.set_setting("app_settings", new)
    await db.audit("secretary_command", actor="owner",
                   detail={"action": cmd.action, "minutes": cmd.minutes,
                           "until": str(cmd.until_hhmm)})
    return reply + "\n" + await status_text(new, timezone), True
