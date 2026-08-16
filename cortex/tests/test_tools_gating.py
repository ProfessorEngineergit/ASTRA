"""The owner-only safety gate: a third party messaging Bahrian must never see or
invoke personal-assistant tools (core remember_fact + every plugin tool)."""
from __future__ import annotations

import asyncio

from app.plugins.registry import _discover_classes
from app.tools import (
    REGISTRY, Tool, ToolContext, capability_manifest, clear_source, dispatch, needs_confirmation,
    openai_tools, register,
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
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test", safety="mutation"))
    try:
        assert needs_confirmation("home_assistant_state") is False
        assert needs_confirmation("home_assistant_call") is True
    finally:
        for name, tool in prior.items():
            if tool is None:
                REGISTRY.pop(name, None)
            else:
                REGISTRY[name] = tool


def test_send_message_is_external_send_and_confirms():
    """astra_send_message is external_send → it always needs confirmation; the web
    chat routes that to a pending-action card (see agent.generate_reply_meta)."""
    from app.admin_tools import register_admin_tools
    register_admin_tools()
    assert REGISTRY["astra_send_message"].safety == "external_send"
    assert needs_confirmation("astra_send_message") is True
    assert needs_confirmation("astra_configure_integration") is True


def test_update_settings_can_toggle_secretary_via_json(memdb):
    from app.admin_tools import _update_settings

    memdb["app_settings"] = {"secretary": {"enabled": True, "tone": "warm"}}
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"},
                      is_owner=True, permission_mode="bypass")

    result = asyncio.run(_update_settings({"secretary_enabled": False}, ctx))

    assert "Secretary=False" in result
    assert memdb["app_settings"]["secretary"] == {"enabled": False, "tone": "warm"}


def test_send_message_resolves_owner_name_to_live_waha_self(monkeypatch, memdb):
    from app import admin_tools

    seen = []

    class Transport:
        async def send(self, channel, to, text):
            seen.append((channel, to, text))
            return True

        def last_error(self, channel):
            return ""

    monkeypatch.setattr(admin_tools, "get_channels", lambda: Transport())
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"},
                      is_owner=True, permission_mode="bypass")

    result = asyncio.run(admin_tools._send_message(
        {"channel": "waha", "to": "Bahrian", "text": "Test"}, ctx,
    ))

    assert seen == [("waha", "__self__", "Test")]
    assert '"ok": true' in result


def test_send_message_keeps_transport_error_for_chat_result(monkeypatch, memdb):
    from app import admin_tools

    class Transport:
        async def send(self, channel, to, text):
            return False

        def last_error(self, channel):
            return "WAHA-Session 'default' ist STARTING."

    transport = Transport()
    monkeypatch.setattr(admin_tools, "get_channels", lambda: transport)
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"},
                      is_owner=True, permission_mode="bypass")

    result = asyncio.run(admin_tools._send_message(
        {"channel": "waha", "to": "Bahrian", "text": "Test"}, ctx,
    ))

    assert "WAHA-Session 'default' ist STARTING" in result


def test_safety_controls_confirmation_and_manifest_visibility():
    register(Tool(name="_t_read", description="read", parameters={"type": "object", "properties": {}},
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test",
                  safety="private_read", intents=["status"], examples=["read it"]))
    register(Tool(name="_t_mutate", description="mutate", parameters={"type": "object", "properties": {}},
                  handler=lambda a, c: _async("ok"), owner_only=True, source="test",
                  safety="mutation", intents=["control"]))
    try:
        assert needs_confirmation("_t_read") is False
        assert needs_confirmation("_t_mutate") is True
        third = {c["tool"] for c in capability_manifest(is_owner=False)}
        owner = {c["tool"] for c in capability_manifest(is_owner=True)}
        assert "_t_read" not in third
        assert "_t_read" in owner and "_t_mutate" in owner
    finally:
        clear_source("test")


async def _async(v):
    return v
