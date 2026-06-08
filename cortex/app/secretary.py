"""Secretary policy for third-party channels."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .policy import Mode, Sensitivity

SECRETARY_CHANNELS = {"waha", "signal", "email"}


@dataclass(frozen=True)
class SecretaryPlan:
    mode: Mode
    reason: str
    in_service_window: bool
    should_notify_owner: bool = False


def secretary_settings(app_settings: dict | None) -> dict:
    raw = (app_settings or {}).get("secretary", {}) or {}
    return {
        "enabled": raw.get("enabled", True),
        "school_direct": raw.get("school_direct", True),
        "workdays": raw.get("workdays", [0, 1, 2, 3, 4]),
        "school_start": raw.get("school_start", "07:30"),
        "school_end": raw.get("school_end", "15:30"),
        "intro": raw.get(
            "intro",
            "--ASTRA-KI-AGENT--\nIch bin Bahrians persoenlicher Assistent. "
            "Ich kann organisatorische Dinge beantworten oder Bahrian bei Bedarf fragen.",
        ),
        "header": raw.get("header", "--ASTRA--"),
    }


def _parse_hhmm(value: str, fallback: time) -> time:
    try:
        h, m = str(value).split(":", 1)
        return time(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return fallback


def in_school_window(now: datetime, settings: dict) -> bool:
    if now.weekday() not in set(settings.get("workdays") or []):
        return False
    start = _parse_hhmm(settings.get("school_start", "07:30"), time(7, 30))
    end = _parse_hhmm(settings.get("school_end", "15:30"), time(15, 30))
    return start <= now.time() <= end


def plan_for(
    *,
    channel: str,
    mode: Mode,
    max_sensitivity: Sensitivity,
    app_settings: dict | None,
    timezone: str,
    now: datetime | None = None,
) -> SecretaryPlan:
    settings = secretary_settings(app_settings)
    if channel not in SECRETARY_CHANNELS or not settings["enabled"]:
        return SecretaryPlan(mode, "not-secretary-channel", False)
    try:
        local_now = now or datetime.now(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001
        local_now = now or datetime.now()
    in_window = in_school_window(local_now, settings)
    if channel == "email":
        return SecretaryPlan(Mode.ASK, "email-requires-owner-confirmation", in_window, True)
    if in_window and settings["school_direct"] and mode == Mode.DEFER:
        return SecretaryPlan(Mode.AUTO, "school-window-direct-secretary", True)
    if mode == Mode.DEFER:
        return SecretaryPlan(mode, "owner-likely-personal-reply", in_window, True)
    return SecretaryPlan(mode, "policy-kept", in_window, mode == Mode.ASK)


def with_secretary_header(text: str, *, first_interaction: bool, app_settings: dict | None) -> str:
    settings = secretary_settings(app_settings)
    header = settings["intro"] if first_interaction else settings["header"]
    stripped = (text or "").strip()
    if not stripped:
        return header
    if stripped.lower().startswith("--astra"):
        return stripped
    return f"{header}\n{stripped}"
