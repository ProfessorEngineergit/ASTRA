"""Plugin discovery, owner-only tools, and live (de)registration via the manager."""
from __future__ import annotations

import asyncio

from app import tools
from app.config_store import get_config_store
from app.plugins.registry import _discover_classes, get_manager

EXPECTED = {"rmv", "home_assistant", "edupage", "google_tasks"}


def test_discovery_finds_builtin_plugins():
    slugs = {c.slug for c in _discover_classes()}
    assert EXPECTED <= slugs


def test_all_plugin_tools_are_owner_only():
    for cls in _discover_classes():
        inst = cls({"__enabled": True})
        for t in inst.tools():
            assert t.owner_only is True, f"{cls.slug}:{t.name} not owner_only"
            assert t.source == cls.slug


def test_rebuild_registers_enabled_plugin_tools(memdb):
    cs = get_config_store()
    # Configure + enable RMV, then rebuild the registry.
    asyncio.run(cs.save(
        next(c for c in _discover_classes() if c.slug == "rmv"),
        {"api_key": "k", "home_stop_id": "3000001"}, enabled=True,
    ))
    mgr = get_manager()
    asyncio.run(mgr.rebuild())

    assert "get_departures" in tools.REGISTRY
    assert tools.REGISTRY["get_departures"].owner_only is True
    assert tools.REGISTRY["get_departures"].source == "rmv"
    # core tools survive a rebuild
    assert "recall_memory" in tools.REGISTRY
    assert tools.REGISTRY["recall_memory"].source == "core"


def test_clear_all_plugin_tools_keeps_core(memdb):
    tools.clear_all_plugin_tools()
    names = set(tools.REGISTRY)
    assert {"recall_memory", "request_owner_approval", "remember_fact"} <= names
    assert "get_departures" not in names
