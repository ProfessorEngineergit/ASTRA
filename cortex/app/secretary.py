"""Secretary policy for third-party channels."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .policy import Mode, Sensitivity

SECRETARY_CHANNELS = {"waha", "signal", "slack", "email"}

# Contact rule values
CONTACT_RULES = ("block", "allow", "ask", "direct")


def contact_rules(app_settings: dict | None) -> list[dict]:
    """Return the contact-level rules list. Each entry: {channel, id, rule, note}."""
    return list((app_settings or {}).get("secretary", {}).get("contact_rules") or [])


def contact_rule_for(app_settings: dict | None, channel: str, sender_id: str) -> str | None:
    """Return the rule ('block'|'allow'|'ask'|'direct') for this sender, or None if unknown."""
    for entry in contact_rules(app_settings):
        if entry.get("channel") in (channel, "*") and entry.get("id") == sender_id:
            return entry.get("rule")
    return None


def unknown_sender_action(app_settings: dict | None) -> str:
    """What to do when a sender has no contact rule: 'ask_owner'|'policy'|'block'."""
    return str(
        (app_settings or {}).get("secretary", {}).get("unknown_sender_action") or "policy"
    )
CHANNEL_LABELS = {
    "telegram": "Telegram",
    "waha": "WhatsApp",
    "signal": "Signal",
    "slack": "Slack",
    "email": "Mail",
}


@dataclass(frozen=True)
class SecretaryPlan:
    mode: Mode
    reason: str
    in_service_window: bool
    should_notify_owner: bool = False
    silent: bool = False   # a 'silent' window (e.g. night): don't respond at all


def secretary_settings(app_settings: dict | None) -> dict:
    raw = (app_settings or {}).get("secretary", {}) or {}
    channels = raw.get("channels") or {}

    def as_int(key: str, fallback: int) -> int:
        try:
            return int(raw.get(key, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback

    def chan(name: str, *, enabled: bool = True, mode: str = "policy") -> dict:
        data = channels.get(name) or {}
        return {
            "enabled": bool(data.get("enabled", enabled)),
            "mode": data.get("mode", mode),
            "label": data.get("label") or CHANNEL_LABELS.get(name, name),
        }

    return {
        "enabled": bool(raw.get("enabled", True)),
        "tone": raw.get("tone", "warm"),
        "default_tone": (raw.get("default_tone") or "").strip(),
        "jailbreak_tone": raw.get("jailbreak_tone", "firm"),
        "school_direct": bool(raw.get("school_direct", True)),
        "workdays": raw.get("workdays", [0, 1, 2, 3, 4]),
        "school_start": raw.get("school_start", "07:30"),
        "school_end": raw.get("school_end", "15:30"),
        "confirm_after_minutes": as_int("confirm_after_minutes", 10),
        "wait_after_minutes": as_int("wait_after_minutes", 45),
        "group_actions": raw.get("group_actions", "owner_grant"),
        "intro": raw.get(
            "intro",
            "--ASTRA-KI-AGENT--\nIch bin Bahrians persoenlicher Assistent. "
            "Ich kann organisatorische Dinge beantworten oder Bahrian bei Bedarf fragen.",
        ),
        "header": raw.get("header", "--ASTRA--"),
        "channels": {
            "waha": chan("waha", enabled=True, mode="school_direct"),
            "signal": chan("signal", enabled=True, mode="school_direct"),
            "slack": chan("slack", enabled=True, mode="school_direct"),
            "email": chan("email", enabled=True, mode="always_ask"),
        },
    }


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        h, m = str(value).split(":", 1)
        return time(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return fallback


def in_school_window(now: datetime, settings: dict) -> bool:
    try:
        workdays = {int(day) for day in (settings.get("workdays") or [])}
    except (TypeError, ValueError):
        workdays = {0, 1, 2, 3, 4}
    if now.weekday() not in workdays:
        return False
    start = _parse_hhmm(settings.get("school_start", "07:30"), time(7, 30))
    end = _parse_hhmm(settings.get("school_end", "15:30"), time(15, 30))
    return start <= now.time() <= end


def channel_enabled(app_settings: dict | None, channel: str) -> bool:
    settings = secretary_settings(app_settings)
    return bool(settings["enabled"] and settings["channels"].get(channel, {}).get("enabled", True))


def is_group_context(channel: str, handle: str, meta: dict | None = None) -> bool:
    meta = meta or {}
    if meta.get("is_group"):
        return True
    if channel == "telegram":
        return str(handle).startswith("-") or meta.get("chat_type") in {"group", "supergroup"}
    if channel == "waha":
        return str(handle).endswith("@g.us")
    if channel == "signal":
        return bool(meta.get("group_id") or meta.get("group_name"))
    return False


def tone_instruction(app_settings: dict | None, thread_meta: dict | None = None) -> str:
    settings = secretary_settings(app_settings)
    meta = thread_meta or {}
    # Security watch and an explicit per-thread override beat the standard tone.
    if meta.get("security_watch"):
        tone = settings.get("jailbreak_tone") or "firm"
    elif meta.get("tone_override"):
        tone = meta["tone_override"]
    elif settings.get("default_tone"):
        # Freeform standard tone set by Bahrian (used when no per-person tone applies).
        return f"Tonfall (Standard): {settings['default_tone']}."
    else:
        tone = settings["tone"]
    return {
        "warm": "Tonfall: warm, ruhig, klar und menschlich.",
        "crisp": "Tonfall: knapp, praezise und ohne Smalltalk.",
        "formal": "Tonfall: formell, hoeflich und sauber abgegrenzt.",
        "firm": "Tonfall: freundlich, aber deutlich distanziert und konsequent.",
    }.get(tone, "Tonfall: warm, ruhig, klar und menschlich.")


def plan_for(
    *,
    channel: str,
    mode: Mode,
    max_sensitivity: Sensitivity,
    app_settings: dict | None,
    timezone: str,
    now: datetime | None = None,
    is_group: bool = False,
) -> SecretaryPlan:
    settings = secretary_settings(app_settings)
    if channel not in SECRETARY_CHANNELS or not settings["enabled"]:
        return SecretaryPlan(mode, "not-secretary-channel", False)
    channel_cfg = settings["channels"].get(channel, {})
    if not channel_cfg.get("enabled", True):
        return SecretaryPlan(mode, f"{channel}-disabled", False)
    try:
        local_now = now or datetime.now(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        local_now = now or datetime.now()
    in_window = in_school_window(local_now, settings)
    if is_group and settings.get("group_actions") != "auto":
        return SecretaryPlan(Mode.ASK, "group-action-requires-owner-grant", in_window, True)

    # Named time-window behavior takes priority over channel modes (except the
    # email safety guard below): night → silent, focus → hold, etc.
    behavior = window_behavior(app_settings, local_now)
    if behavior == "silent":
        return SecretaryPlan(mode, "window-silent", in_window, silent=True)

    channel_mode = channel_cfg.get("mode", "policy")
    if channel == "email" or channel_mode == "always_ask":
        return SecretaryPlan(Mode.ASK, "email-requires-owner-confirmation", in_window, True)

    if behavior == "auto":
        return SecretaryPlan(Mode.AUTO, "window-auto", in_window)
    if behavior == "hold":
        return SecretaryPlan(Mode.DEFER, "window-hold", in_window, True)
    if behavior == "notify":
        return SecretaryPlan(Mode.DEFER, "window-notify", in_window, True)
    if channel_mode == "direct":
        return SecretaryPlan(Mode.AUTO, f"{channel}-direct", in_window)
    if channel_mode == "wait":
        return SecretaryPlan(Mode.DEFER, f"{channel}-wait", in_window, True)
    if in_window and settings["school_direct"] and mode == Mode.DEFER:
        return SecretaryPlan(Mode.AUTO, "school-window-direct-secretary", True)
    if mode == Mode.DEFER:
        return SecretaryPlan(mode, "owner-likely-personal-reply", in_window, True)
    return SecretaryPlan(mode, "policy-kept", in_window, mode == Mode.ASK)


# ─── Named time-window engine (generalizes the single school window) ──────────
# A window is {name, start, end, days, behavior}. `behavior` says what the
# secretary does for third parties while the window is active:
#   auto   → answer autonomously   hold → wait for Bahrian (DEFER)
#   notify → only ping Bahrian      silent → stay quiet (e.g. night)
# Night windows may wrap past midnight (start > end). Falls back to the legacy
# school window so existing configs keep working unchanged.
_BEHAVIORS = ("auto", "hold", "notify", "silent")


def secretary_windows(app_settings: dict | None) -> list[dict]:
    raw = (app_settings or {}).get("secretary", {}) or {}
    windows = raw.get("windows")
    if isinstance(windows, list) and windows:
        return [w for w in windows if isinstance(w, dict)]
    # Back-compat: synthesize windows from the historical school settings.
    s = secretary_settings(app_settings)
    return [{
        "name": "schule", "start": s["school_start"], "end": s["school_end"],
        "days": s["workdays"], "behavior": "auto" if s["school_direct"] else "hold",
    }]


def _in_window(now: datetime, win: dict) -> bool:
    try:
        days = {int(d) for d in (win.get("days") or [0, 1, 2, 3, 4, 5, 6])}
    except (TypeError, ValueError):
        days = {0, 1, 2, 3, 4, 5, 6}
    start = _parse_hhmm(win.get("start", "00:00"), time(0, 0))
    end = _parse_hhmm(win.get("end", "23:59"), time(23, 59))
    t = now.time()
    if start <= end:
        return now.weekday() in days and start <= t <= end
    # Wrap past midnight: active from `start` tonight until `end` tomorrow morning.
    return (now.weekday() in days and t >= start) or t <= end


def active_window(app_settings: dict | None, now: datetime) -> dict | None:
    """First matching window (order = priority), or None."""
    for win in secretary_windows(app_settings):
        if _in_window(now, win):
            return win
    return None


def window_behavior(app_settings: dict | None, now: datetime) -> str:
    win = active_window(app_settings, now)
    beh = (win or {}).get("behavior", "")
    return beh if beh in _BEHAVIORS else ""


def shadow_enabled(app_settings: dict | None, channel: str) -> bool:
    """Shadow mode: draft the reply and send it to Bahrian for approval instead of
    to the contact. The only honest way to test the secretary on real WhatsApp."""
    raw = (app_settings or {}).get("secretary", {}) or {}
    if raw.get("shadow_all"):
        return True
    per = raw.get("shadow") or {}
    return bool(per.get(channel))


def with_secretary_header(text: str, *, first_interaction: bool, app_settings: dict | None) -> str:
    settings = secretary_settings(app_settings)
    header = settings["intro"] if first_interaction else settings["header"]
    stripped = (text or "").strip()
    if not stripped:
        return header
    if stripped.lower().startswith("--astra"):
        return stripped
    return f"{header}\n{stripped}"
