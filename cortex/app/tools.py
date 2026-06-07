"""Agent tool registry.

Core tools (memory recall, owner approval, remember-fact) are registered at import
and always available. Plugin tools are registered dynamically by the PluginManager
(tagged with `source=<plugin.slug>`) and re-registered on every config change. The
owner-only gate in `dispatch` / `openai_tools` is the security boundary: a third
party messaging Bahrian can never see or invoke a personal-assistant tool.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, "ToolContext"], Awaitable[str]]
    requires_approval: bool = False
    owner_only: bool = False   # personal-assistant tools a third party must never reach
    source: str = "core"       # "core" or a plugin slug (for clean re-registration)

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


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


# ─── Registry ─────────────────────────────────────────────────────────────────
REGISTRY: dict[str, Tool] = {}

_SAFE_TOOL_NAMES = {
    "recall_memory",
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
