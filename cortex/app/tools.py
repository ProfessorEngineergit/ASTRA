"""Agent tool registry.

Phase 1 ships two tools: memory recall and the human-in-the-loop approval.
Phase 2+ append calendar / edupage / smarthome / booking tools that dispatch to
the n8n tool/* workflows — same Tool shape, just a different handler.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import db, knowledge
from .channels import get_channels
from .config import get_settings
from .integrations.edupage import get_edupage
from .integrations.home_assistant import get_ha
from .integrations.rmv import get_rmv
from .integrations.tasks import get_tasks
from .memory import get_memory

log = logging.getLogger("astra.tools")


@dataclass
class ToolContext:
    thread_id: str
    channel: str
    contact: dict
    max_sensitivity: str = "none"
    is_owner: bool = False     # True only when ASTRA is talking to the owner himself


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, "ToolContext"], Awaitable[str]]
    requires_approval: bool = False
    owner_only: bool = False   # personal-assistant tools a third party must never reach

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ─── Handlers ─────────────────────────────────────────────────────────────────
async def _recall_memory(args: dict, ctx: ToolContext) -> str:
    query = args.get("query", "")
    mem = get_memory()
    scope = str(ctx.contact.get("id") or ctx.contact.get("handle") or "unknown")
    facts = (await mem.recall(query, user_id="owner")) + (await mem.recall(query, user_id=scope))
    if not facts:
        return "Keine gespeicherten Erinnerungen dazu."
    return "Erinnerungen:\n- " + "\n- ".join(facts[:8])


async def _request_owner_approval(args: dict, ctx: ToolContext) -> str:
    s = get_settings()
    question = args.get("question", "Freigabe nötig.")
    owner_summary = args.get("owner_summary", question)
    approval_id = await db.create_approval(
        thread_id=ctx.thread_id,
        contact_id=ctx.contact.get("id"),
        kind="disclosure",
        question=question,
        payload={"channel": ctx.channel, "thread_id": ctx.thread_id},
    )
    await db.set_thread_state(ctx.thread_id, "awaiting_approval")
    await db.audit(
        "ask_principal",
        channel=ctx.channel,
        thread_id=ctx.thread_id,
        contact_id=ctx.contact.get("id"),
        detail={"question": question, "approval_id": approval_id},
    )
    if s.telegram_enabled and s.telegram_owner_chat_id:
        name = ctx.contact.get("display_name") or ctx.contact.get("handle")
        buttons = [
            {"text": "✅ Ja", "callback_data": f"apv:{approval_id}:yes"},
            {"text": "🟡 Nur 'beschäftigt'", "callback_data": f"apv:{approval_id}:busy_only"},
            {"text": "❌ Nein", "callback_data": f"apv:{approval_id}:no"},
        ]
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"🔔 {name} ({ctx.channel}) fragt:\n„{owner_summary}“\n\nDarf ASTRA antworten?",
            buttons=buttons,
        )
    return (
        "Freigabe bei Bahrian angefragt. Sag dem Gegenüber freundlich, dass du kurz "
        "Rücksprache hältst und dich gleich meldest."
    )


# ─── Owner-only handlers (personal assistant capabilities) ────────────────────
async def _remember_fact(args: dict, ctx: ToolContext) -> str:
    text = args.get("fact", "").strip()
    if not text:
        return "Kein Fakt übergeben."
    target = args.get("file", "facts.md")
    ok = knowledge.append_fact(text, file=target)
    return f"Gespeichert in {target}." if ok else "Konnte den Fakt nicht speichern."


async def _ha_get_state(args: dict, ctx: ToolContext) -> str:
    ha = get_ha()
    if not ha.enabled:
        return "Home Assistant ist nicht konfiguriert."
    entity = args.get("entity_id", "")
    if args.get("unavailable_only"):
        offline = await ha.unavailable_entities()
        if not offline:
            return "Keine Entität ist offline/unavailable."
        return "Offline/unavailable:\n- " + "\n- ".join(
            f"{e['name']} ({e['entity_id']}): {e['state']}" for e in offline[:25]
        )
    st = await ha.get_state(entity)
    if not st:
        return f"Keine Entität '{entity}' gefunden."
    attrs = st.get("attributes") or {}
    name = attrs.get("friendly_name", entity)
    return f"{name} = {st.get('state')} (zuletzt: {st.get('last_changed', '?')})"


async def _ha_call_service(args: dict, ctx: ToolContext) -> str:
    ha = get_ha()
    if not ha.enabled:
        return "Home Assistant ist nicht konfiguriert."
    domain = args.get("domain", "")
    service = args.get("service", "")
    data = args.get("data") or {}
    if isinstance(data, str):  # model sometimes passes JSON-as-string
        import json as _json
        try:
            data = _json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    if not domain or not service:
        return "domain und service sind erforderlich (z.B. light.turn_on)."
    ok = await ha.call_service(domain, service, data)
    await db.audit("tool_call", thread_id=ctx.thread_id, contact_id=ctx.contact.get("id"),
                   detail={"tool": "ha_call_service", "domain": domain, "service": service, "ok": ok})
    return f"{domain}.{service} ausgeführt." if ok else f"{domain}.{service} fehlgeschlagen."


async def _get_timetable(args: dict, ctx: ToolContext) -> str:
    from datetime import date, timedelta
    ep = get_edupage()
    if not ep.enabled:
        return "EduPage ist nicht konfiguriert."
    day = date.today()
    when = (args.get("day") or "today").lower()
    if when in ("tomorrow", "morgen"):
        day = day + timedelta(days=1)
    lessons = await ep.timetable(day)
    if not lessons:
        return f"Kein Stundenplan für {day.isoformat()} gefunden (oder unterrichtsfrei)."
    lines = [
        f"{l.period}. {l.subject} {l.start}-{l.end} ({l.classroom}, {l.teacher})".strip()
        for l in lessons
    ]
    return f"Stundenplan {day.isoformat()}:\n- " + "\n- ".join(lines)


async def _get_departures(args: dict, ctx: ToolContext) -> str:
    rmv = get_rmv()
    if not rmv.enabled:
        return "RMV ist nicht konfiguriert."
    deps = await rmv.departures(args.get("stop_id"))
    if not deps:
        return "Keine Abfahrten gefunden."
    lines = []
    for d in deps:
        flag = "⚠️ FÄLLT AUS" if d["cancelled"] else (
            f"(echtzeit {d['rtTime']})" if d["rtTime"] and d["rtTime"] != d["time"] else ""
        )
        lines.append(f"{d['time']} {d['line']} → {d['direction']} {flag}".strip())
    return "Nächste Abfahrten:\n- " + "\n- ".join(lines)


async def _add_google_task(args: dict, ctx: ToolContext) -> str:
    tk = get_tasks()
    if not tk.enabled:
        return "Google Tasks ist nicht konfiguriert."
    title = args.get("title", "").strip()
    if not title:
        return "Kein Titel übergeben."
    ok = await tk.add(title, notes=args.get("notes", ""), due=args.get("due"))
    return f"Aufgabe '{title}' hinzugefügt." if ok else "Aufgabe konnte nicht angelegt werden."


# ─── Registry ─────────────────────────────────────────────────────────────────
REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    REGISTRY[tool.name] = tool


register(
    Tool(
        name="recall_memory",
        description=(
            "Durchsuche das Langzeitgedächtnis nach Fakten über Bahrian oder diese Person "
            "(Vorlieben, frühere Aussagen, Beziehungen). Nutze es, bevor du etwas annimmst."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Wonach suchen"}},
            "required": ["query"],
        },
        handler=_recall_memory,
    )
)

register(
    Tool(
        name="request_owner_approval",
        description=(
            "Hole Bahrians Freigabe, BEVOR du Sensibles preisgibst oder etwas mit Außenwirkung/"
            "Geld tust. Pflicht, wenn die Information über der Freigabe-Stufe der Person liegt."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Worum es konkret geht (für die Akte)"},
                "owner_summary": {
                    "type": "string",
                    "description": "Kurzfassung, die Bahrian per Telegram sieht",
                },
            },
            "required": ["question", "owner_summary"],
        },
        handler=_request_owner_approval,
        requires_approval=True,
    )
)


# Personal-assistant tools — owner_only, so a third party messaging Bahrian can
# never drive his home/tasks/timetable. Registered unconditionally (they return a
# friendly "not configured" string when off) except where it needs a backend flag.
register(Tool(
    name="remember_fact",
    description="Speichere einen dauerhaften Fakt/Notiz über Bahrian (überlebt Updates). "
                "Nutze das, wenn Bahrian dir etwas mitteilt, das du dir merken sollst.",
    parameters={
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "Der zu merkende Fakt (ein Satz)"},
            "file": {"type": "string", "enum": ["facts.md", "routines.md", "people.md"],
                     "description": "Zieldatei (Default facts.md)"},
        },
        "required": ["fact"],
    },
    handler=_remember_fact,
    owner_only=True,
))

register(Tool(
    name="home_assistant_state",
    description="Lies den Zustand einer Home-Assistant-Entität ODER liste alle offline/"
                "unavailable Entitäten (für 'warum ist diese Integration offline?').",
    parameters={
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "z.B. light.desk, sensor.temp"},
            "unavailable_only": {"type": "boolean", "description": "true = nur offline Entitäten"},
        },
    },
    handler=_ha_get_state,
    owner_only=True,
))

register(Tool(
    name="home_assistant_call",
    description="Rufe einen Home-Assistant-Service auf, um aktiv etwas zu schalten/ändern "
                "(z.B. domain='light', service='turn_on', data={'entity_id':'light.desk'}).",
    parameters={
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "z.B. light, switch, climate, automation"},
            "service": {"type": "string", "description": "z.B. turn_on, turn_off, set_temperature"},
            "data": {"type": "object", "description": "Service-Daten inkl. entity_id"},
        },
        "required": ["domain", "service"],
    },
    handler=_ha_call_service,
    owner_only=True,
))

register(Tool(
    name="get_timetable",
    description="Hole Bahrians Schul-Stundenplan (EduPage) für heute oder morgen.",
    parameters={
        "type": "object",
        "properties": {"day": {"type": "string", "enum": ["today", "tomorrow"]}},
    },
    handler=_get_timetable,
    owner_only=True,
))

register(Tool(
    name="get_departures",
    description="Nächste ÖPNV-Abfahrten (RMV) von einer Haltestelle, inkl. Ausfall-Warnungen.",
    parameters={
        "type": "object",
        "properties": {"stop_id": {"type": "string", "description": "Haltestellen-ID (extId); "
                                   "leer = Heim-Haltestelle aus Config"}},
    },
    handler=_get_departures,
    owner_only=True,
))

register(Tool(
    name="add_google_task",
    description="Lege eine Google-Task (To-Do) für Bahrian an.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "notes": {"type": "string"},
            "due": {"type": "string", "description": "RFC-3339 Datum, optional"},
        },
        "required": ["title"],
    },
    handler=_add_google_task,
    owner_only=True,
))


def openai_tools(*, is_owner: bool = False) -> list[dict]:
    """Tool specs visible to the model. Owner-only tools are hidden from the
    third-party register entirely (defence in depth — dispatch also enforces it)."""
    return [t.spec() for t in REGISTRY.values() if is_owner or not t.owner_only]


async def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    tool = REGISTRY.get(name)
    if not tool:
        return f"Unbekanntes Tool: {name}"
    if tool.owner_only and not ctx.is_owner:
        log.warning("Blocked owner-only tool %s for non-owner thread %s", name, ctx.thread_id)
        try:
            await db.audit("tool_blocked", thread_id=ctx.thread_id, contact_id=ctx.contact.get("id"),
                           detail={"tool": name, "reason": "owner_only"})
        except Exception:  # noqa: BLE001 — the security gate must never fail open/crash
            pass
        return "Dieses Werkzeug ist nur für Bahrian selbst verfügbar."
    try:
        return await tool.handler(args, ctx)
    except Exception as e:  # noqa: BLE001
        log.error("tool %s failed: %s", name, e)
        return f"Tool {name} fehlgeschlagen: {e}"
