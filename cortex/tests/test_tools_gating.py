"""The owner-only safety gate: a third party messaging Bahrian must never be able
to see or invoke personal-assistant tools (Home Assistant, tasks, timetable …)."""
from __future__ import annotations

import asyncio

from app.tools import REGISTRY, ToolContext, dispatch, openai_tools

OWNER_ONLY = {
    "remember_fact", "home_assistant_state", "home_assistant_call",
    "get_timetable", "get_departures", "add_google_task",
}
ALWAYS = {"recall_memory", "request_owner_approval"}


def test_owner_only_tools_registered():
    for name in OWNER_ONLY:
        assert name in REGISTRY, f"{name} not registered"
        assert REGISTRY[name].owner_only is True


def test_third_party_tool_list_hides_owner_tools():
    third = {t["function"]["name"] for t in openai_tools(is_owner=False)}
    owner = {t["function"]["name"] for t in openai_tools(is_owner=True)}
    assert OWNER_ONLY & third == set(), "owner-only tools leaked to third party"
    assert ALWAYS <= third
    assert OWNER_ONLY <= owner


def test_dispatch_blocks_owner_tool_for_third_party():
    ctx = ToolContext(thread_id="waha:49x@c.us", channel="waha", contact={"id": None}, is_owner=False)
    res = asyncio.run(
        dispatch("home_assistant_call", {"domain": "light", "service": "turn_on"}, ctx)
    )
    assert "nur für Bahrian" in res


def test_unconfigured_tool_returns_friendly_message_for_owner():
    # Owner allowed through the gate; integration is unconfigured → graceful message,
    # never an exception.
    ctx = ToolContext(thread_id="telegram:1", channel="telegram", contact={"id": None}, is_owner=True)
    res = asyncio.run(dispatch("home_assistant_state", {"entity_id": "light.x"}, ctx))
    assert "nicht konfiguriert" in res
