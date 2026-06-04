"""Agent tool registry.

Phase 1 ships two tools: memory recall and the human-in-the-loop approval.
Phase 2+ append calendar / edupage / smarthome / booking tools that dispatch to
the n8n tool/* workflows — same Tool shape, just a different handler.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import db
from .channels import get_channels
from .config import get_settings
from .memory import get_memory

log = logging.getLogger("astra.tools")


@dataclass
class ToolContext:
    thread_id: str
    channel: str
    contact: dict
    max_sensitivity: str = "none"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, "ToolContext"], Awaitable[str]]
    requires_approval: bool = False

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


def openai_tools() -> list[dict]:
    return [t.spec() for t in REGISTRY.values()]


async def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    tool = REGISTRY.get(name)
    if not tool:
        return f"Unbekanntes Tool: {name}"
    try:
        return await tool.handler(args, ctx)
    except Exception as e:  # noqa: BLE001
        log.error("tool %s failed: %s", name, e)
        return f"Tool {name} fehlgeschlagen: {e}"
