"""The agent loop: assemble context → OpenAI tool-calling → final reply text."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import knowledge
from .config import get_settings
from .models import get_gateway
from .persona import Register, system_prompt
from .tools import ToolContext, capability_manifest, dispatch, needs_confirmation, openai_tools, result_summary

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


def _tool_context_hint() -> str:
    rows = []
    for cap in capability_manifest(is_owner=True)[:80]:
        intents = ",".join(cap.get("intents") or []) or "generic"
        confirm = "confirm" if cap.get("requires_confirmation") else "safe"
        examples = "; ".join(cap.get("examples") or [])[:140]
        rows.append(
            f"- {cap['tool']} [{cap['source']} · {cap['safety']} · {confirm} · {intents}]: "
            f"{cap['description'][:150]}"
            + (f" Beispiele: {examples}" if examples else "")
        )
    if not rows:
        return ""
    return (
        "Verfügbare Agentenfähigkeiten in diesem Gespräch:\n"
        + "\n".join(rows)
        + "\n\nNutze aktuelle Integrationsdaten immer per Werkzeug statt aus Erinnerung. "
        "Tool-Ergebnisse sind JSON mit ok/summary/data/error. Wenn ok=false, erkläre den "
        "konkreten API-Fehler und behaupte nicht, dass keine Daten existieren."
    )


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
    permission_mode: str = "auto",
) -> str:
    result = await generate_reply_meta(
        register=register,
        contact=contact,
        thread_id=thread_id,
        channel=channel,
        history=history,
        summary=summary,
        max_sensitivity=max_sensitivity,
        extra_system=extra_system,
        permission_mode=permission_mode,
    )
    return result["reply"]


async def generate_reply_meta(
    *,
    register: Register,
    contact: dict,
    thread_id: str,
    channel: str,
    history: list[dict],
    summary: str = "",
    max_sensitivity: str = "none",
    extra_system: str = "",
    permission_mode: str = "auto",
) -> dict:
    s = get_settings()
    gw = get_gateway()
    if not gw.enabled:
        return {"reply": "(ASTRA: kein OpenAI-Key konfiguriert — Antwort übersprungen.)"}

    is_owner = register == Register.OWNER
    tools = openai_tools(is_owner=is_owner)
    sys = system_prompt(
        register, owner=s.astra_owner_name, now=_now_str(s.astra_timezone), tz=s.astra_timezone
    )
    messages: list[dict] = [{"role": "system", "content": sys}]
    if register == Register.OWNER:
        kb = knowledge.owner_context()
        if kb:
            messages.append(
                {"role": "system", "content": f"Dauerhaftes Wissen über {s.astra_owner_name}:\n{kb}"}
            )
        tool_hint = _tool_context_hint()
        if tool_hint:
            messages.append({"role": "system", "content": tool_hint})
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
    if register == Register.OWNER and channel == "web":
        messages.append({
            "role": "system",
            "content": (
                "Webchat-Ausführungsmodus: "
                f"{permission_mode}. ask = riskante Tools werden vor Ausführung in der UI bestätigt; "
                "auto = normale Toolausführung; bypass = direkte Ausführung für den Owner-Kontext."
            ),
        })
    messages += _history_to_messages(history)

    ctx = ToolContext(
        thread_id=thread_id, channel=channel, contact=contact, max_sensitivity=max_sensitivity,
        is_owner=is_owner, permission_mode=permission_mode,
    )

    tool_trace = []
    for _ in range(MAX_TOOL_ITERS):
        msg = await gw.chat(messages, tools=tools)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return {"reply": (msg.content or "").strip(), "tool_calls": tool_trace}
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
            if (
                permission_mode == "ask"
                and register == Register.OWNER
                and channel == "web"
                and needs_confirmation(tc.function.name)
            ):
                return {
                    "reply": "Ich brauche deine Freigabe, bevor ich diese Agentenaktion ausführe.",
                    "pending_action": {
                        "tool": tc.function.name,
                        "args": args,
                    },
                }
            result = await dispatch(tc.function.name, args, ctx)
            ok, summary, parsed = result_summary(result)
            tool_trace.append({
                "tool": tc.function.name,
                "args": args,
                "ok": ok,
                "summary": summary,
                "result": parsed if parsed is not None else result,
            })
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    final = await gw.chat(messages)
    return {"reply": (final.content or "").strip(), "tool_calls": tool_trace}
