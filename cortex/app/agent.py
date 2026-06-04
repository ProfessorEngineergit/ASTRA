"""The agent loop: assemble context → OpenAI tool-calling → final reply text."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get_settings
from .models import get_gateway
from .persona import Register, system_prompt
from .tools import ToolContext, dispatch, openai_tools

log = logging.getLogger("astra.agent")
MAX_TOOL_ITERS = 4


def _now_str(tz: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz)).strftime("%a %d.%m.%Y %H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%a %d.%m.%Y %H:%M")


def _history_to_messages(history: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in history:
        role = m.get("role")
        if role == "assistant":
            out.append({"role": "assistant", "content": m["content"]})
        elif role == "owner":
            out.append({"role": "system", "content": f"(Bahrian selbst schrieb: {m['content']})"})
        else:
            out.append({"role": "user", "content": m["content"]})
    return out


async def generate_reply(
    *,
    register: Register,
    contact: dict,
    thread_id: str,
    channel: str,
    history: list[dict],
    summary: str = "",
    max_sensitivity: str = "none",
    extra_system: str = "",
) -> str:
    s = get_settings()
    gw = get_gateway()
    if not gw.enabled:
        return "(ASTRA: kein OpenAI-Key konfiguriert — Antwort übersprungen.)"

    sys = system_prompt(
        register, owner=s.astra_owner_name, now=_now_str(s.astra_timezone), tz=s.astra_timezone
    )
    messages: list[dict] = [{"role": "system", "content": sys}]
    if summary:
        messages.append({"role": "system", "content": f"Bisheriger Gesprächskontext: {summary}"})
    if register == Register.THIRD:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"Freigabe-Ceiling für diese Person: '{max_sensitivity}'. "
                    "none = nichts Privates; freebusy = höchstens ob Bahrian frei/beschäftigt ist; "
                    "details = Details erlaubt. Überschreite das NIE. Wenn die Anfrage mehr "
                    "verlangt, nutze das Tool request_owner_approval."
                ),
            }
        )
    if extra_system:
        messages.append({"role": "system", "content": extra_system})
    messages += _history_to_messages(history)

    ctx = ToolContext(
        thread_id=thread_id, channel=channel, contact=contact, max_sensitivity=max_sensitivity
    )
    tools = openai_tools()

    for _ in range(MAX_TOOL_ITERS):
        msg = await gw.chat(messages, tools=tools)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return (msg.content or "").strip()
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await dispatch(tc.function.name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    final = await gw.chat(messages)
    return (final.content or "").strip()
