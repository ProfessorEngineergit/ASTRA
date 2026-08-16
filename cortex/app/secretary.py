"""Secretary policy for third-party channels."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .policy import Mode, Sensitivity

SECRETARY_CHANNELS = {"waha", "signal", "slack", "email"}
ACTIVATION_MODES = ("auto", "on", "off")

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


@dataclass(frozen=True)
class SecretaryServiceStatus:
    active: bool
    source: str
    reason: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None


_SERVICE_CACHE: dict[tuple[str, str], tuple[datetime, SecretaryServiceStatus]] = {}


def secretary_settings(app_settings: dict | None) -> dict:
    raw = (app_settings or {}).get("secretary", {}) or {}
    channels = raw.get("channels") or {}
    activation_mode = str(raw.get("activation_mode") or "").strip().lower()
    if activation_mode not in ACTIVATION_MODES:
        activation_mode = "auto" if bool(raw.get("enabled", True)) else "off"

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
        # ``enabled`` stays as a compatibility view for older callers/configs.
        # The three-state activation_mode is authoritative once present.
        "enabled": activation_mode != "off",
        "activation_mode": activation_mode,
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


def _parse_optional_hhmm(value: str) -> time | None:
    try:
        h, m = str(value).split(":", 1)
        return time(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return None


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


def _school_span(
    lessons: list[dict],
    day,
    tz: ZoneInfo,
    *,
    parse_time,
) -> tuple[datetime, datetime] | None:
    intervals: list[tuple[datetime, datetime]] = []
    for lesson in lessons:
        if lesson.get("cancelled"):
            continue
        start_t = parse_time(lesson.get("start", ""))
        end_t = parse_time(lesson.get("end", ""))
        if start_t and end_t:
            intervals.append((
                datetime.combine(day, start_t, tzinfo=tz),
                datetime.combine(day, end_t, tzinfo=tz),
            ))
    if not intervals:
        return None
    return min(start for start, _ in intervals), max(end for _, end in intervals)


async def resolve_service_status(
    app_settings: dict | None,
    timezone: str,
    *,
    now: datetime | None = None,
    refresh: bool = False,
) -> SecretaryServiceStatus:
    """Resolve whether Secretary should currently be on.

    In automatic mode a successful EduPage day is authoritative. Cancelled
    lessons are ignored, so an omitted/cancelled last period shortens the day.
    The imported Google Calendar baseline is the next fallback, followed by the
    legacy static school window. Results are cached briefly to keep incoming
    messages fast without hiding timetable changes for long.
    """
    settings = secretary_settings(app_settings)
    activation_mode = settings["activation_mode"]
    try:
        tz = ZoneInfo(timezone)
        if now is None:
            local_now = datetime.now(tz)
        elif now.tzinfo:
            local_now = now.astimezone(tz)
        else:
            local_now = now.replace(tzinfo=tz)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Europe/Berlin")
        if now is None:
            local_now = datetime.now(tz)
        elif now.tzinfo:
            local_now = now.astimezone(tz)
        else:
            local_now = now.replace(tzinfo=tz)

    if activation_mode == "off":
        return SecretaryServiceStatus(False, "manual", "manual-off")
    if activation_mode == "on":
        return SecretaryServiceStatus(True, "manual", "manual-on")

    cache_key = (timezone, local_now.date().isoformat())
    cached = _SERVICE_CACHE.get(cache_key)
    if not refresh and cached and (local_now - cached[0]).total_seconds() < 60:
        return cached[1]

    def remember(status: SecretaryServiceStatus) -> SecretaryServiceStatus:
        _SERVICE_CACHE[cache_key] = (local_now, status)
        return status

    try:
        from .plugins.registry import get_manager
        manager = get_manager()
    except Exception:  # noqa: BLE001
        manager = None

    try:
        edupage = manager.get("edupage") if manager else None
        if edupage is not None and edupage.enabled and hasattr(edupage, "timetable_result"):
            result = await asyncio.wait_for(edupage.timetable_result(local_now.date()), timeout=8)
            if result.get("ok"):
                lessons = result.get("lessons") or []
                group = edupage._default_group() if hasattr(edupage, "_default_group") else "B"
                if hasattr(edupage, "_filter_lessons"):
                    lessons = edupage._filter_lessons(lessons, group)
                parser = edupage._parse_hhmm if hasattr(edupage, "_parse_hhmm") else _parse_optional_hhmm
                span = _school_span(lessons, local_now.date(), tz, parse_time=parser)
                if span:
                    start, end = span
                    active = start <= local_now <= end
                    return remember(SecretaryServiceStatus(
                        active, "edupage",
                        "edupage-school-day" if active else "edupage-outside-school-day",
                        start, end,
                    ))
                return remember(SecretaryServiceStatus(
                    False, "edupage", "edupage-no-active-lessons",
                ))
    except Exception:  # noqa: BLE001
        pass

    try:
        calendar = manager.get("google_calendar") if manager else None
        if calendar is not None and calendar.enabled and hasattr(calendar, "events"):
            from .plugins.builtin.google_calendar import SCHOOL_BASELINE_MARKER

            day_start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
            day_end = datetime.combine(local_now.date(), time.max, tzinfo=tz)
            events = await asyncio.wait_for(
                calendar.events(day_start.isoformat(), day_end.isoformat(), max_results=100),
                timeout=8,
            )
            intervals = []
            for event in events:
                if SCHOOL_BASELINE_MARKER not in str(event.get("description") or ""):
                    continue
                interval = calendar._event_interval(event, tz)
                if interval:
                    intervals.append(interval)
            if intervals:
                start = min(item[0] for item in intervals)
                end = max(item[1] for item in intervals)
                active = start <= local_now <= end
                return remember(SecretaryServiceStatus(
                    active, "google_calendar",
                    "calendar-school-day" if active else "calendar-outside-school-day",
                    start, end,
                ))
    except Exception:  # noqa: BLE001
        # A remote source must never stop Secretary from reaching the configured
        # local fallback. Connection details are logged by the plugins themselves.
        pass

    active = in_school_window(local_now, settings)
    return remember(SecretaryServiceStatus(
        active, "static",
        "static-school-window" if active else "static-outside-school-window",
    ))


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
    service_active: bool | None = None,
    service_reason: str = "",
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
    activation_mode = settings["activation_mode"]
    if activation_mode == "on":
        in_window = True
    elif service_active is not None:
        in_window = bool(service_active)
    else:
        in_window = in_school_window(local_now, settings)
    if activation_mode == "auto" and not in_window:
        return SecretaryPlan(
            mode,
            service_reason or "auto-outside-service-window",
            False,
            silent=True,
        )
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
        return SecretaryPlan(Mode.AUTO, service_reason or "window-auto", in_window)
    if behavior == "hold":
        return SecretaryPlan(Mode.DEFER, "window-hold", in_window, True)
    if behavior == "notify":
        return SecretaryPlan(Mode.DEFER, "window-notify", in_window, True)
    if channel_mode == "direct":
        return SecretaryPlan(Mode.AUTO, f"{channel}-direct", in_window)
    if channel_mode == "wait":
        return SecretaryPlan(Mode.DEFER, f"{channel}-wait", in_window, True)
    if activation_mode == "on":
        return SecretaryPlan(Mode.AUTO, "manual-on", True)
    if in_window and settings["school_direct"] and mode in (Mode.DEFER, Mode.ASK):
        return SecretaryPlan(
            Mode.AUTO,
            service_reason or "school-window-direct-secretary",
            True,
        )
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
