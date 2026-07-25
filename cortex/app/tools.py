"""Agent tool registry.

Core tools (memory recall, owner approval, remember-fact) are registered at import
and always available. Plugin tools are registered dynamically by the PluginManager
(tagged with `source=<plugin.slug>`) and re-registered on every config change. The
owner-only gate in `dispatch` / `openai_tools` is the security boundary: a third
party messaging Bahrian can never see or invoke a personal-assistant tool.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import db, knowledge
from .channels import get_channels  # noqa: F401  (kept for parity / future tools)
from .config import get_settings  # noqa: F401
from .memory import get_memory

log = logging.getLogger("astra.tools")


@dataclass
class ToolContext:
    thread_id: str
    channel: str
    contact: dict
    max_sensitivity: str = "none"
    is_owner: bool = False     # True only when ASTRA is talking to the owner himself
    permission_mode: str = "auto"  # web owner chat: ask | auto | bypass
    principal: str = ""        # served-person key ('' = default owner); multi-tenant seam


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, "ToolContext"], Awaitable[str]]
    requires_approval: bool = False
    owner_only: bool = False   # personal-assistant tools a third party must never reach
    source: str = "core"       # "core" or a plugin slug (for clean re-registration)
    safety: str = "auto"  # auto | read | private_read | mutation | external_send | destructive
    intents: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_result(
    *,
    ok: bool,
    summary: str,
    data: Any = None,
    source: str = "core",
    warnings: list[str] | None = None,
    error: dict | str | None = None,
) -> str:
    payload = {
        "ok": ok,
        "summary": summary,
        "data": data,
        "source": source,
        "fresh_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings or [],
        "error": error,
    }
    return json.dumps(payload, ensure_ascii=False)


def result_summary(raw: str) -> tuple[bool | None, str, dict | None]:
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, raw.strip()[:500], None
    if isinstance(parsed, dict) and "summary" in parsed:
        return bool(parsed.get("ok")), str(parsed.get("summary") or ""), parsed
    return None, raw.strip()[:500], None


# ─── Core handlers ────────────────────────────────────────────────────────────
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


async def _remember_fact(args: dict, ctx: ToolContext) -> str:
    text = args.get("fact", "").strip()
    if not text:
        return "Kein Fakt übergeben."
    target = args.get("file", "facts.md")
    ok = knowledge.append_fact(text, file=target)
    return f"Gespeichert in {target}." if ok else "Konnte den Fakt nicht speichern."


async def _remember(args: dict, ctx: ToolContext) -> str:
    """Store one compact, structured fact about the owner (efficient memory).

    subject+value is a terse pair ('Quantum Room' → 'Schlafzimmer'), not prose.
    Only relevant facts are pulled back into later prompts, so this stays cheap."""
    kind = (args.get("kind") or "bio").strip().lower()
    subject = (args.get("subject") or "").strip()
    value = (args.get("value") or "").strip()
    if not value and not subject:
        return "Nichts zu merken — gib subject und/oder value an."
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    principal = getattr(ctx, "principal", "") or ""
    try:
        await db.add_fact(kind, subject, value, tags=tags,
                          always_on=bool(args.get("always_on")), principal_key=principal)
    except Exception as e:  # noqa: BLE001
        log.warning("remember failed: %s", e)
        return f"Konnte das nicht merken: {e}"
    if kind == "alias":
        try:
            from . import world
            world.invalidate()
        except Exception:  # noqa: BLE001
            pass
    shown = f"{subject}: {value}" if subject and value else (value or subject)
    return f"Gemerkt [{kind}]: {shown}"


# ─── Registry ─────────────────────────────────────────────────────────────────
REGISTRY: dict[str, Tool] = {}

_SAFE_TOOL_NAMES = {
    "recall_memory",
    "home_state",
    "list_areas",
    "astra_list_integrations",
    "astra_integration_details",
    "astra_test_integration",
    "astra_get_settings",
    "astra_system_status",
    "home_assistant_state",
}
_SAFE_PREFIXES = (
    "get_",
    "list_",
    "read_",
    "search_",
    "recent_",
    "today_",
    "status_",
)
_MUTATING_HINTS = (
    "add",
    "call",
    "complete",
    "configure",
    "control",
    "create",
    "delete",
    "note",
    "post",
    "power",
    "publish",
    "remove",
    "remember",
    "scene",
    "send",
    "set",
    "toggle",
    "trigger",
    "update",
    "write",
)


def needs_confirmation(name: str) -> bool:
    """Whether a web-owner-chat tool call should pause in ask mode."""
    tool = REGISTRY.get(name)
    if not tool:
        return False
    if tool.requires_approval:
        return True
    if tool.safety in ("read", "private_read"):
        return False
    if tool.safety in ("mutation", "external_send", "destructive"):
        return True
    lname = name.lower()
    if lname in _SAFE_TOOL_NAMES:
        return False
    if lname.startswith(_SAFE_PREFIXES):
        return False
    return any(hint in lname for hint in _MUTATING_HINTS) or tool.owner_only


def register(tool: Tool) -> None:
    REGISTRY[tool.name] = tool


def unregister(name: str) -> None:
    REGISTRY.pop(name, None)


def clear_source(source: str) -> None:
    """Remove every tool registered by a given plugin (used before re-registering)."""
    for name in [n for n, t in REGISTRY.items() if t.source == source]:
        del REGISTRY[name]


def clear_all_plugin_tools() -> None:
    for name in [n for n, t in REGISTRY.items() if t.source != "core"]:
        del REGISTRY[name]


register(Tool(
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
))

register(Tool(
    name="request_owner_approval",
    description=(
        "Hole Bahrians Freigabe, BEVOR du Sensibles preisgibst oder etwas mit Außenwirkung/"
        "Geld tust. Pflicht, wenn die Information über der Freigabe-Stufe der Person liegt."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Worum es konkret geht (für die Akte)"},
            "owner_summary": {"type": "string", "description": "Kurzfassung, die Bahrian per Telegram sieht"},
        },
        "required": ["question", "owner_summary"],
    },
    handler=_request_owner_approval,
    requires_approval=True,
))

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
    name="remember",
    description=(
        "Merke dir EINEN kompakten, strukturierten Fakt über Bahrian — knapp, kein "
        "Fließtext. subject+value als Paar (z. B. subject='Quantum Room', value='Schlafzimmer', "
        "oder subject='Wecker', value='6:40 werktags'). kind ordnet ein: alias (Raum/Gerät-"
        "Spitzname), pref (Vorliebe), bio (über ihn), relation (Person), place (Ort), note. "
        "Nur relevante Fakten kommen später in den Kontext, darum ist das billig. always_on=true "
        "nur für Dinge, die IMMER gelten müssen."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string",
                     "enum": ["alias", "pref", "bio", "relation", "place", "note"]},
            "subject": {"type": "string", "description": "Kurzes Stichwort / linke Seite"},
            "value": {"type": "string", "description": "Der Wert / rechte Seite"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "always_on": {"type": "boolean"},
        },
        "required": ["value"],
    },
    handler=_remember,
    owner_only=True,
    safety="mutation",
    intents=["control"],
))


def openai_tools(*, is_owner: bool = False) -> list[dict]:
    """Tool specs visible to the model. Owner-only tools are hidden from the
    third-party register entirely (defence in depth — dispatch also enforces it)."""
    return [t.spec() for t in REGISTRY.values() if is_owner or not t.owner_only]


def capability_manifest(*, is_owner: bool = False) -> list[dict]:
    out = []
    for t in REGISTRY.values():
        if t.owner_only and not is_owner:
            continue
        out.append({
            "tool": t.name,
            "source": t.source,
            "description": t.description,
            "safety": t.safety,
            "intents": t.intents,
            "examples": t.examples,
            "requires_confirmation": needs_confirmation(t.name),
        })
    return out


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
    started = time.perf_counter()
    try:
        result = await tool.handler(args, ctx)
    except Exception as e:  # noqa: BLE001
        log.error("tool %s failed: %s", name, e)
        result = tool_result(
            ok=False,
            summary=f"Tool {name} fehlgeschlagen: {e}",
            source=tool.source,
            error={"type": type(e).__name__, "message": str(e)},
        )
    ok, summary, parsed = result_summary(result)
    try:
        last_detail = {
            "tool": name,
            "source": tool.source,
            "safety": tool.safety,
            "args": args,
            "ok": ok,
            "summary": summary,
            "error": (parsed or {}).get("error") if parsed else None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "fresh_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.set_setting("agent_tool_last", last_detail)
        await db.audit(
            "agent_tool_call",
            channel=ctx.channel,
            thread_id=ctx.thread_id,
            contact_id=ctx.contact.get("id"),
            detail=last_detail,
        )
    except Exception:  # noqa: BLE001
        pass
    return result
