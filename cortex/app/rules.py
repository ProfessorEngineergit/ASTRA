"""Regelwerk — „wenn Trigger + Bedingung, dann Aktionen", quer über alle Integrationen.

Das ist die Schicht, die „sieh nach ob ich Duolingo gemacht habe, sonst erinnere
mich und leg einen Google-Task an" möglich macht, ohne sie 75× pro Plugin-Paar zu
bauen. Regeln sind JSON (inspizierbar, editierbar) und liegen in der `rules`-Tabelle.

Aufbau einer Regel:
    trigger   {"type":"schedule","at":"21:50","days":[0,1,2,3,4],"offset_min":0}
              (einmalig: {"type":"schedule","at":"07:00","date":"2026-10-10"} — danach aus)
    condition {"type":"always"} | {"type":"tool","tool":"duolingo_status",
               "expect":{"path":"data.done","equals":false}}
    actions   [{"type":"speak","text":"…","where":"Schlafzimmer"},
               {"type":"notify","text":"…","urgency":"normal"},
               {"type":"tool","tool":"add_google_task","args":{"title":"Duolingo"}},
               {"type":"display","event":"alarm","data":{"label":"Aufstehen","sound":"gentle",
                "briefing":true}}]   # → OpenBoard-Wanddisplay (Wecker, say, card, command, board)

Die Zeit- und Bedingungslogik ist rein (I/O-frei) gehalten — genau so testbar.
Ausführung läuft über den notify-Router (W3) und das Tool-Registry (dispatch).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("astra.rules")


# ─── Trigger: Zeit (rein) ─────────────────────────────────────────────────────
def _parse_hhmm(value: str) -> time | None:
    try:
        h, m = str(value).split(":", 1)
        return time(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return None


def schedule_matches(trigger: dict, now: datetime, *, last_run: datetime | None = None) -> bool:
    """True, wenn eine schedule-Regel JETZT feuern soll.

    `at` minus `offset_min` ergibt die effektive Zeit (so wird „10 min vor 22:00"
    zu 21:50). Ein Ein-Minuten-Fenster + Entprellung über `last_run` verhindert
    Doppelauslösung, wenn der Ticker mehrmals pro Minute läuft."""
    if trigger.get("type") != "schedule":
        return False
    base = _parse_hhmm(trigger.get("at", ""))
    if base is None:
        return False
    days = trigger.get("days")
    if days and now.weekday() not in {int(d) for d in days}:
        return False
    offset = int(trigger.get("offset_min") or 0)
    fire_dt = (datetime.combine(now.date(), base, tzinfo=now.tzinfo)
               - timedelta(minutes=offset))
    if trigger.get("date"):
        # Einmalige Regel: nur an diesem Kalendertag (Offset kann über Mitternacht schieben).
        try:
            day = date.fromisoformat(str(trigger["date"]))
        except ValueError:
            return False
        fire_dt = datetime.combine(day, base, tzinfo=now.tzinfo) - timedelta(minutes=offset)
    # innerhalb desselben Minuten-Slots?
    if not (fire_dt <= now < fire_dt + timedelta(minutes=1)):
        return False
    if last_run and last_run.astimezone(now.tzinfo).replace(second=0, microsecond=0) == \
            now.replace(second=0, microsecond=0):
        return False   # in dieser Minute schon gelaufen
    return True


def is_one_shot(trigger: dict) -> bool:
    return bool((trigger or {}).get("date") or (trigger or {}).get("once"))


def next_occurrence(trigger: dict, now: datetime, *, horizon_days: int = 7) -> datetime | None:
    """Nächster Feuerzeitpunkt einer schedule-Regel ab `now` (inklusive), sonst None. Rein."""
    if (trigger or {}).get("type") != "schedule":
        return None
    base = _parse_hhmm(trigger.get("at", ""))
    if base is None:
        return None
    offset = timedelta(minutes=int(trigger.get("offset_min") or 0))
    floor = now.replace(second=0, microsecond=0)
    if trigger.get("date"):
        try:
            day = date.fromisoformat(str(trigger["date"]))
        except ValueError:
            return None
        at = datetime.combine(day, base, tzinfo=now.tzinfo) - offset
        return at if floor <= at <= now + timedelta(days=horizon_days) else None
    days = {int(d) for d in trigger.get("days") or []}
    for i in range(horizon_days + 1):
        day = now.date() + timedelta(days=i)
        if days and day.weekday() not in days:
            continue
        at = datetime.combine(day, base, tzinfo=now.tzinfo) - offset
        if at >= floor:
            return at
    return None


# ─── Bedingung ────────────────────────────────────────────────────────────────
def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def condition_holds(result: dict | None, expect: dict) -> bool:
    """Vergleicht ein Tool-Ergebnis mit der Erwartung. Rein — kein I/O."""
    if not expect:
        return True
    value = _dig(result or {}, expect.get("path", ""))
    if "equals" in expect:
        return value == expect["equals"]
    if "not_equals" in expect:
        return value != expect["not_equals"]
    if "gt" in expect:
        try:
            return float(value) > float(expect["gt"])
        except (TypeError, ValueError):
            return False
    if "lt" in expect:
        try:
            return float(value) < float(expect["lt"])
        except (TypeError, ValueError):
            return False
    if "truthy" in expect:
        return bool(value) == bool(expect["truthy"])
    return value is not None


# ─── Ausführung (I/O) ─────────────────────────────────────────────────────────
async def _run_condition(condition: dict, principal: str) -> bool:
    ctype = (condition or {}).get("type", "always")
    if ctype == "always" or not condition:
        return True
    if ctype == "tool":
        from . import tools
        from .tools import ToolContext, result_summary
        tool_name = condition.get("tool", "")
        ctx = ToolContext(thread_id="rule", channel="rule", contact={},
                          is_owner=True, principal=principal)
        raw = await tools.dispatch(tool_name, condition.get("args") or {}, ctx)
        _ok, _summary, parsed = result_summary(raw)
        return condition_holds(parsed, condition.get("expect") or {})
    return False


async def run_actions(actions: list, *, principal: str = "", rule_id: Any = None) -> list[dict]:
    """Führt die Aktionen einer Regel aus. Gibt je Aktion ein Ergebnis zurück."""
    from . import notify as notify_mod
    from . import tools
    from .tools import ToolContext, result_summary
    out: list[dict] = []
    for action in actions or []:
        atype = action.get("type")
        try:
            if atype == "notify":
                res = await notify_mod.notify(
                    action.get("text", ""), urgency=action.get("urgency", "normal"),
                    principal=principal, where=action.get("where", ""),
                    title=action.get("title", ""),
                )
                out.append({"type": "notify", "ok": True, "detail": res})
            elif atype == "speak":
                res = await notify_mod.notify(
                    action.get("text", ""), urgency="urgent",
                    principal=principal, where=action.get("where", ""))
                out.append({"type": "speak", "ok": True, "detail": res})
            elif atype == "tool":
                ctx = ToolContext(thread_id="rule", channel="rule", contact={},
                                  is_owner=True, principal=principal)
                raw = await tools.dispatch(action.get("tool", ""), action.get("args") or {}, ctx)
                ok, summary, _p = result_summary(raw)
                out.append({"type": "tool", "tool": action.get("tool"),
                            "ok": ok is not False, "summary": summary})
            elif atype == "display":
                from .display import service as display
                res = await display.run_rule_action(action, rule_id=rule_id, principal=principal)
                out.append({"type": "display", "event": action.get("event") or "alarm", **res})
            else:
                out.append({"type": atype, "ok": False, "summary": "unbekannte Aktion"})
        except Exception as e:  # noqa: BLE001 — one bad action must not abort the rest
            log.warning("rule action %s failed: %s", atype, e)
            out.append({"type": atype, "ok": False, "summary": str(e)})
    return out


async def fire_rule(rule: dict, *, scheduled: bool = False) -> str:
    """Bedingung prüfen, ggf. Aktionen ausführen, Ergebnis als kurzer String.

    `scheduled`: vom Ticker ausgelöst — dann schaltet sich eine einmalige Regel danach ab
    (ein manueller Testlauf über astra_rule_run_now lässt sie aktiv)."""
    from . import db
    principal = rule.get("principal_key") or ""
    try:
        if not await _run_condition(rule.get("condition") or {}, principal):
            await db.mark_rule_run(rule["id"], "condition-false")
            return "condition-false"
        results = await run_actions(rule.get("actions") or [], principal=principal,
                                    rule_id=rule.get("id"))
        ok = sum(1 for r in results if r.get("ok"))
        summary = f"{ok}/{len(results)} Aktionen ok"
        await db.mark_rule_run(rule["id"], summary)
        if scheduled and is_one_shot(rule.get("trigger") or {}):
            await db.set_rule_enabled(rule["id"], False)   # einmalig: erledigt
        await db.audit("rule_fired", detail={"rule": rule.get("name"), "id": rule["id"],
                                             "results": results})
        return summary
    except Exception as e:  # noqa: BLE001
        log.exception("rule %s failed", rule.get("id"))
        try:
            await db.mark_rule_run(rule["id"], f"error: {e}")
        except Exception:  # noqa: BLE001
            pass
        return f"error: {e}"


async def tick(now: datetime | None = None) -> int:
    """Vom Scheduler aufgerufen: feuert alle fälligen schedule-Regeln. Anzahl zurück."""
    from . import db
    from .config import get_settings
    try:
        tz = ZoneInfo(get_settings().astra_timezone)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    now = now or datetime.now(tz)
    fired = 0
    for rule in await db.active_schedule_rules():
        if schedule_matches(rule.get("trigger") or {}, now, last_run=rule.get("last_run_at")):
            await fire_rule(rule, scheduled=True)
            fired += 1
    return fired
