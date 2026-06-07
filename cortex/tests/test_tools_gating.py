"""The owner-only safety gate: a third party messaging Bahrian must never see or
invoke personal-assistant tools (core remember_fact + every plugin tool)."""
from __future__ import annotations

import asyncio

from app.plugins.registry import _discover_classes
from app.tools import (
    REGISTRY, Tool, ToolContext, clear_source, dispatch, needs_confirmation, openai_tools, register,
)

CORE_ALWAYS = {"recall_memory", "request_owner_approval"}


def test_core_tools_registered():
    assert CORE_ALWAYS <= set(REGISTRY)
    assert REGISTRY["remember_fact"].owner_only is True


def test_every_plugin_tool_is_owner_only():
    for cls in _discover_classes():
        for t in cls({"__enabled": True}).tools():
            assert t.owner_only is True


def test_third_party_never_sees_owner_tools():
    # Register a representative plugin tool, then check visibility for a third party.
    rmv = next(c for c in _discover_classes() if c.slug == "rmv")
    for t in rmv({"__enabled": True, "api_key": "k"}).tools():
        register(t)
    try:
        third = {t["function"]["name"] for t in openai_tools(is_owner=False)}
        owner = {t["function"]["name"] for t in openai_tools(is_owner=True)}
        assert "get_departures" not in third
        assert "remember_fact" not in third
        assert CORE_ALWAYS <= third
        assert "get_departures" in owner
    finally:
        clear_source("rmv")


def test_dispatch_blocks_owner_tool_for_third_party():
    register(Tool(name="_t_owner", description="x", parameters={"type": "object", "properties": {}},
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test"))
    try:
        ctx = ToolContext(thread_id="waha:x", channel="waha", contact={"id": None}, is_owner=False)
        res = asyncio.run(dispatch("_t_owner", {}, ctx))
        assert "nur für Bahrian" in res
    finally:
        clear_source("test")


def test_home_assistant_read_state_does_not_pause_in_ask_mode():
    prior = {name: REGISTRY.get(name) for name in ("home_assistant_state", "home_assistant_call")}
    register(Tool(name="home_assistant_state", description="x", parameters={"type": "object", "properties": {}},
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test"))
    register(Tool(name="home_assistant_call", description="x", parameters={"type": "object", "properties": {}},
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test"))
    try:
        assert needs_confirmation("home_assistant_state") is False
        assert needs_confirmation("home_assistant_call") is True
    finally:
        for name, tool in prior.items():
            if tool is None:
                REGISTRY.pop(name, None)
            else:
                REGISTRY[name] = tool


async def _async(v):
    return v
