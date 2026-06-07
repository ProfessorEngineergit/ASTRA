"""Plugin discovery, owner-only tools, and live (de)registration via the manager."""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import time

from app import tools
from app.config_store import get_config_store
from app.plugins.builtin.edupage import EduPagePlugin
from app.plugins.builtin.home_assistant import HomeAssistantPlugin
from app.plugins.registry import _discover_classes, get_manager
from app.tools import ToolContext

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


def test_home_assistant_read_tools_search_and_areas(monkeypatch):
    plugin = HomeAssistantPlugin({"__enabled": True, "base_url": "http://ha", "token": "t"})

    async def fake_search(query="", *, domain="", limit=20):
        assert query == "wohnzimmer"
        return [{
            "entity_id": "sensor.wohnzimmer_temperatur",
            "name": "Wohnzimmer Temperatur",
            "state": "21.4",
            "unit": "°C",
            "domain": "sensor",
        }]

    async def fake_area(area="", *, domain="", limit=40):
        assert area == "Wohnzimmer"
        assert domain == "sensor"
        return "Wohnzimmer:\n- sensor.wohnzimmer_temperatur | Wohnzimmer Temperatur | 21.4"

    monkeypatch.setattr(plugin, "search_states", fake_search)
    monkeypatch.setattr(plugin, "area_overview", fake_area)
    tool_map = {t.name: t for t in plugin.tools()}
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    found = asyncio.run(tool_map["home_assistant_state"].handler({"query": "wohnzimmer"}, ctx))
    assert "sensor.wohnzimmer_temperatur" in found
    assert "21.4 °C" in found

    area = asyncio.run(tool_map["list_home_assistant_areas"].handler({
        "area": "Wohnzimmer",
        "domain": "sensor",
    }, ctx))
    assert "Wohnzimmer Temperatur" in area
    assert {"home_assistant_state", "search_home_assistant", "list_home_assistant_areas"} <= set(tool_map)


def test_edupage_auto_scans_for_next_lesson(monkeypatch):
    plugin = EduPagePlugin({"__enabled": True, "subdomain": "school", "username": "u", "password": "p"})
    calls = []

    async def fake_timetable(day):
        calls.append(day)
        if len(calls) < 2:
            return []
        return [{
            "period": "1",
            "subject": "Mathe",
            "teacher": "Frau X",
            "classroom": "A101",
            "start": "08:00",
            "end": "08:45",
        }]

    monkeypatch.setattr(plugin, "timetable", fake_timetable)
    timetable = next(t for t in plugin.tools() if t.name == "get_timetable")
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    result = asyncio.run(timetable.handler({}, ctx))

    assert "Mathe" in result
    assert len(calls) == 2


def test_edupage_fetch_uses_current_get_my_timetable_signature(monkeypatch):
    calls = []

    class FakeEdupage:
        def login(self, username, password, subdomain):
            calls.append(("login", username, subdomain))

        def get_my_timetable(self, day):
            calls.append(("my", day.isoformat()))
            lesson = types.SimpleNamespace(
                period=1,
                subject=types.SimpleNamespace(name="Deutsch"),
                teachers=[types.SimpleNamespace(name="Herr D")],
                classrooms=[types.SimpleNamespace(name="B204")],
                classes=[],
                groups=["B"],
                start_time=time(8, 0),
                end_time=time(8, 45),
                curriculum="Epochal",
                online_lesson_link=None,
                is_cancelled=False,
                is_event=False,
            )
            return types.SimpleNamespace(lessons=[lesson])

        def get_timetable(self, *_args):
            raise AssertionError("old get_timetable(day) path must not be used first")

    fake_mod = types.SimpleNamespace(Edupage=FakeEdupage)
    monkeypatch.setitem(sys.modules, "edupage_api", fake_mod)

    plugin = EduPagePlugin({"__enabled": True, "subdomain": "school", "username": "u", "password": "p"})
    lessons = plugin._fetch_sync(__import__("datetime").date(2026, 6, 8))

    assert calls[0] == ("login", "u", "school")
    assert calls[1] == ("my", "2026-06-08")
    assert lessons[0]["subject"] == "Deutsch"
    assert lessons[0]["groups"] == ["B"]
    assert lessons[0]["curriculum"] == "Epochal"


def test_edupage_group_filter_and_substitutions(monkeypatch):
    plugin = EduPagePlugin({
        "__enabled": True,
        "subdomain": "school",
        "username": "u",
        "password": "p",
        "preferred_group": "B",
    })

    async def fake_timetable(day):
        return [
            {"period": "1", "subject": "Physik", "teacher": "Frau P", "classroom": "P1",
             "groups": ["A"], "start": "08:00", "end": "08:45"},
            {"period": "2", "subject": "Mathe", "teacher": "Herr M", "classroom": "M2",
             "groups": ["B"], "start": "08:50", "end": "09:35", "curriculum": "Epoch"},
            {"period": "3", "subject": "Deutsch", "teacher": "Frau D", "classroom": "D3",
             "groups": [], "start": "09:50", "end": "10:35"},
        ]

    async def fake_changes(day):
        return [{"class": "10B", "lesson": "2", "title": "Mathe in M4", "action": "change"}]

    monkeypatch.setattr(plugin, "timetable", fake_timetable)
    monkeypatch.setattr(plugin, "timetable_changes", fake_changes)
    timetable = next(t for t in plugin.tools() if t.name == "get_timetable")
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    result = asyncio.run(timetable.handler({"day": "tomorrow"}, ctx))

    assert "Gruppe B" in result
    assert "Mathe" in result
    assert "Epoch" in result
    assert "Deutsch" in result
    assert "Physik" not in result
    assert "Vertretungen" in result
