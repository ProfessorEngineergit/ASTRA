"""Plugin discovery, owner-only tools, and live (de)registration via the manager."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import time

import httpx
import pytest

from app import tools
from app.config_store import get_config_store
from app.plugins import extended_catalog
from app.plugins.builtin.edupage import EduPagePlugin
from app.plugins.builtin.google_calendar import GoogleCalendarPlugin
from app.plugins.builtin.google_tasks import GoogleTasksPlugin
from app.plugins.builtin.home_assistant import HomeAssistantPlugin
from app.plugins.builtin.native_catalog_pack import _slug as native_slug
from app.plugins.builtin.osint import OsintPlugin
from app.plugins.registry import _discover_classes, get_manager
from app.tools import ToolContext

EXPECTED = {"rmv", "home_assistant", "edupage", "google_tasks"}


def _payload(text: str) -> dict:
    return json.loads(text)


def test_discovery_finds_builtin_plugins():
    slugs = {c.slug for c in _discover_classes()}
    assert EXPECTED <= slugs


def test_native_catalog_pack_adds_many_service_plugins():
    classes = _discover_classes()
    native = [c for c in classes if getattr(c, "native_http", False)]

    assert len(native) >= extended_catalog.count() - 5
    sample = next(c for c in native if c.slug == "mattermost")
    plugin = sample({"__enabled": True})
    status = asyncio.run(plugin.tools()[0].handler({}, ToolContext(
        thread_id="web-owner:test",
        channel="web",
        contact={"id": "owner"},
        is_owner=True,
    )))
    payload = _payload(status)
    assert payload["ok"] is False
    assert payload["source"] == "mattermost"
    assert payload["error"]["type"] == "not_configured"


def test_catalog_entries_have_runtime_plugins():
    classes = _discover_classes()
    class_slugs = {c.slug for c in classes}
    class_names = {c.name.lower() for c in classes}
    missing = [
        entry.name
        for entry in extended_catalog.all_entries()
        if native_slug(entry.name) not in class_slugs and entry.name.lower() not in class_names
    ]

    assert missing == []


def test_all_plugin_tools_are_owner_only():
    for cls in _discover_classes():
        inst = cls({"__enabled": True})
        for t in inst.tools():
            assert t.owner_only is True, f"{cls.slug}:{t.name} not owner_only"
            assert t.source == cls.slug


def test_osint_nearby_is_passive_shodan_metadata(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "total": 1,
                "matches": [{
                    "ip_str": "203.0.113.10",
                    "port": 443,
                    "transport": "tcp",
                    "product": "Example Camera",
                    "org": "Example ISP",
                    "location": {
                        "city": "Frankfurt",
                        "country_name": "Germany",
                        "latitude": 50.12,
                        "longitude": 8.69,
                    },
                }],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

    plugin = OsintPlugin({
        "__enabled": True,
        "tor_proxy": "socks5://tor:9050",
        "shodan_key": "secret",
    })
    async def tor_verified(**_kwargs):
        return True, "Tor-Kill-Switch aktiv"

    monkeypatch.setattr(plugin, "_verify_tor", tor_verified)
    monkeypatch.setattr(plugin, "_client", lambda: FakeClient())
    tool = next(t for t in plugin.tools() if t.name == "osint_nearby_exposure")
    ctx = ToolContext(thread_id="web-owner:test", channel="web",
                      contact={"id": "owner"}, is_owner=True)

    result = asyncio.run(tool.handler({
        "category": "cameras", "lat": 50.114321, "lon": 8.684321, "radius": 15,
    }, ctx))
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["data"]["results"][0]["ip"] == "203.0.113.10"
    assert "shodan_url" not in payload["data"]["results"][0]
    assert "feed" not in payload["data"]["results"][0]
    assert [call[0] for call in calls] == ["https://api.shodan.io/shodan/host/search"]
    assert "geo:50.11,8.68,15" in calls[0][1]["params"]["query"]


def test_osint_client_fails_closed_without_valid_tor_proxy():
    for proxy in ("", "http://proxy:8080", "socks5://user:secret@tor:9050"):
        plugin = OsintPlugin({"__enabled": True, "tor_proxy": proxy})
        with pytest.raises(RuntimeError, match="Kill-Switch"):
            plugin._client()


def test_osint_http_error_never_exposes_secret_url():
    request = httpx.Request(
        "GET", "https://api.shodan.io/shodan/host/search?key=visible-secret&query=test")
    response = httpx.Response(403, request=request)
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)

    message = OsintPlugin._safe_http_error("Shodan", exc)

    assert "403" in message
    assert "visible-secret" not in message
    assert "api.shodan.io" not in message
    assert "direkten Fallback" in message


def test_osint_public_scan_uses_tor_probe_only(monkeypatch):
    plugin = OsintPlugin({
        "__enabled": True,
        "tor_proxy": "socks5://tor:9050",
        "scan_targets": "8.8.8.8",
    })
    calls = []

    async def tor_verified(**_kwargs):
        return True, "Tor-Kill-Switch aktiv"

    async def tor_probe(host, port, timeout):
        calls.append((host, port, timeout))
        return True

    async def direct_probe(*_args, **_kwargs):
        raise AssertionError("public target must never use direct TCP")

    monkeypatch.setattr(plugin, "_verify_tor", tor_verified)
    monkeypatch.setattr(plugin, "_probe_via_tor", tor_probe)
    monkeypatch.setattr(plugin, "_probe", direct_probe)

    result = asyncio.run(plugin.scan("8.8.8.8", ports=[443]))

    assert result["ok"] is True
    assert result["found"]["8.8.8.8"][0]["port"] == 443
    assert calls and calls[0][:2] == ("8.8.8.8", 443)


def test_osint_location_falls_back_to_browser_saved_setting(memdb):
    memdb["app_settings"] = {"location": {"lat": 50.123456, "lon": 8.654321}}
    plugin = OsintPlugin({"__enabled": True, "tor_proxy": "socks5://tor:9050"})
    here = asyncio.run(plugin._here())
    assert here["ok"] is True
    assert here["source"] == "browser_saved"
    assert here["lat"] == 50.123456


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


def test_rebuild_registers_extra_installation_tools(memdb):
    cs = get_config_store()
    rmv_cls = next(c for c in _discover_classes() if c.slug == "rmv")
    asyncio.run(cs.save(rmv_cls, {"api_key": "k1", "home_stop_id": "3000001"}, enabled=True))
    install_id = asyncio.run(cs.save_installation(
        rmv_cls,
        "__new__",
        {"api_key": "k2", "home_stop_id": "3000002"},
        True,
        name="Server 2",
    ))

    mgr = get_manager()
    asyncio.run(mgr.rebuild())

    extra_name = f"get_departures__{install_id}"
    assert "get_departures" in tools.REGISTRY
    assert extra_name in tools.REGISTRY
    assert tools.REGISTRY[extra_name].source == f"rmv:{install_id}"
    assert "Server 2" in tools.REGISTRY[extra_name].description


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


def test_google_tasks_native_add_uses_tasks_api(monkeypatch):
    calls = []

    class FakeResponse:
        def json(self):
            return {"id": "task1", "title": "Mathe"}

    async def fake_google_api(plugin, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("app.plugins.builtin.google_tasks.google_api", fake_google_api)
    plugin = GoogleTasksPlugin({
        "__enabled": True,
        "backend": "native",
        "client_id": "cid",
        "client_secret": "sec",
        "refresh_token": "ref",
        "list": "@default",
    })

    result = asyncio.run(plugin.add("Mathe", notes="S. 42"))

    assert result["id"] == "task1"
    assert calls[0][0] == "POST"
    assert "tasks.googleapis.com/tasks/v1/lists/%40default/tasks" in calls[0][1]
    assert calls[0][2]["json"]["notes"] == "S. 42"


def test_google_calendar_native_today_uses_calendar_api(monkeypatch):
    calls = []

    class FakeResponse:
        def json(self):
            return {"items": [{"summary": "Schule", "start": {"dateTime": "2026-06-08T08:00:00+02:00"}}]}

    async def fake_google_api(plugin, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("app.plugins.builtin.google_calendar.google_api", fake_google_api)
    plugin = GoogleCalendarPlugin({
        "__enabled": True,
        "backend": "native",
        "client_id": "cid",
        "client_secret": "sec",
        "refresh_token": "ref",
        "calendar_id": "primary",
    })

    events = asyncio.run(plugin.today())

    assert events[0]["summary"] == "Schule"
    assert calls[0][0] == "GET"
    assert "www.googleapis.com/calendar/v3/calendars/primary/events" in calls[0][1]


def test_edupage_auto_scans_for_next_lesson(monkeypatch):
    plugin = EduPagePlugin({"__enabled": True, "subdomain": "school", "username": "u", "password": "p"})
    calls = []

    async def fake_timetable_result(day):
        calls.append(day)
        if len(calls) < 2:
            return {"ok": True, "method": "get_my_timetable", "date": day.isoformat(), "lessons": [], "error": None}
        return {"ok": True, "method": "get_my_timetable", "date": day.isoformat(), "lessons": [{
            "period": "1",
            "subject": "Mathe",
            "teacher": "Frau X",
            "classroom": "A101",
            "start": "08:00",
            "end": "08:45",
        }], "error": None}

    async def fake_changes_result(day):
        return {"ok": True, "method": "get_timetable_changes", "date": day.isoformat(), "changes": [], "error": None}

    monkeypatch.setattr(plugin, "timetable_result", fake_timetable_result)
    monkeypatch.setattr(plugin, "changes_result", fake_changes_result)
    timetable = next(t for t in plugin.tools() if t.name == "get_timetable")
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    result = asyncio.run(timetable.handler({}, ctx))
    payload = _payload(result)

    assert payload["ok"] is True
    assert "Mathe" in payload["summary"]
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
    result = plugin._fetch_sync(__import__("datetime").date(2026, 6, 8))
    lessons = result["lessons"]

    assert result["ok"] is True
    assert result["method"] == "get_my_timetable"
    assert calls[0] == ("login", "u", "school")
    assert calls[1] == ("my", "2026-06-08")
    assert lessons[0]["subject"] == "Deutsch"
    assert lessons[0]["groups"] == ["B"]
    assert lessons[0]["curriculum"] == "Epochal"


def test_edupage_normalizes_url_and_switches_child(monkeypatch):
    calls = []

    class FakeEdupage:
        def login(self, username, password, subdomain):
            calls.append(("login", subdomain))

        def switch_to_child(self, child):
            calls.append(("child", child))

        def get_user_id(self):
            return "Student123"

        def get_my_timetable(self, day):
            calls.append(("tt", day.isoformat()))
            return types.SimpleNamespace(lessons=[])

    monkeypatch.setitem(sys.modules, "edupage_api", types.SimpleNamespace(Edupage=FakeEdupage))
    plugin = EduPagePlugin({
        "__enabled": True,
        "subdomain": "https://demo.edupage.org/login/",
        "username": "u",
        "password": "p",
        "child_id": "42",
    })

    result = plugin._fetch_sync(__import__("datetime").date(2026, 6, 8))

    assert calls[:2] == [("login", "demo"), ("child", 42)]
    assert result["ok"] is True


def test_edupage_group_filter_and_substitutions(monkeypatch):
    plugin = EduPagePlugin({
        "__enabled": True,
        "subdomain": "school",
        "username": "u",
        "password": "p",
        "preferred_group": "B",
    })

    async def fake_timetable_result(day):
        return {"ok": True, "method": "get_my_timetable", "date": day.isoformat(), "lessons": [
            {"period": "1", "subject": "Physik", "teacher": "Frau P", "classroom": "P1",
             "groups": ["A"], "start": "08:00", "end": "08:45"},
            {"period": "2", "subject": "Mathe", "teacher": "Herr M", "classroom": "M2",
             "groups": ["B"], "start": "08:50", "end": "09:35", "curriculum": "Epoch"},
            {"period": "3", "subject": "Deutsch", "teacher": "Frau D", "classroom": "D3",
             "groups": [], "start": "09:50", "end": "10:35"},
        ], "error": None}

    async def fake_changes_result(day):
        return {"ok": True, "method": "get_timetable_changes", "date": day.isoformat(),
                "changes": [{"class": "10B", "lesson": "2", "title": "Mathe in M4", "action": "change"}],
                "error": None}

    monkeypatch.setattr(plugin, "timetable_result", fake_timetable_result)
    monkeypatch.setattr(plugin, "changes_result", fake_changes_result)
    timetable = next(t for t in plugin.tools() if t.name == "get_timetable")
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    result = asyncio.run(timetable.handler({"day": "tomorrow"}, ctx))
    payload = _payload(result)

    assert payload["ok"] is True
    assert "Gruppe B" in payload["summary"]
    assert "Mathe" in payload["summary"]
    assert "Epoch" in payload["summary"]
    assert "Deutsch" in payload["summary"]
    assert "Physik" not in payload["summary"]
    assert "Vertretungen" in payload["summary"]
    assert payload["data"]["debug"]["raw_lesson_count"] == 3
    assert payload["data"]["debug"]["filtered_lesson_count"] == 2


def test_edupage_api_error_is_diagnostic(monkeypatch):
    plugin = EduPagePlugin({"__enabled": True, "subdomain": "school", "username": "u", "password": "p"})

    async def boom(day):
        return {"ok": False, "method": "get_my_timetable", "date": day.isoformat(), "lessons": [],
                "error": {"type": "LoginError", "message": "bad auth", "method": "get_my_timetable"}}

    monkeypatch.setattr(plugin, "timetable_result", boom)
    timetable = next(t for t in plugin.tools() if t.name == "edupage_get_timetable")
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    payload = _payload(asyncio.run(timetable.handler({"day": "tomorrow"}, ctx)))

    assert payload["ok"] is False
    assert "API lieferte LoginError" in payload["summary"]
    assert payload["data"]["debug"]["timetable_error"]["message"] == "bad auth"


def test_edupage_changes_and_debug_tools(monkeypatch):
    plugin = EduPagePlugin({"__enabled": True, "subdomain": "school", "username": "u", "password": "p"})

    async def fake_timetable_result(day):
        return {"ok": True, "method": "get_my_timetable", "date": day.isoformat(), "lessons": [], "error": None}

    async def fake_changes_result(day):
        return {"ok": True, "method": "get_timetable_changes", "date": day.isoformat(),
                "changes": [{"class": "10B", "lesson": "1", "title": "Raumwechsel", "action": "change"}],
                "error": None}

    monkeypatch.setattr(plugin, "timetable_result", fake_timetable_result)
    monkeypatch.setattr(plugin, "changes_result", fake_changes_result)
    tool_map = {t.name: t for t in plugin.tools()}
    ctx = ToolContext(thread_id="web-owner:test", channel="web", contact={"id": "owner"}, is_owner=True)

    changes = _payload(asyncio.run(tool_map["edupage_get_changes"].handler({"day": "tomorrow"}, ctx)))
    debug = _payload(asyncio.run(tool_map["edupage_debug_day"].handler({"day": "tomorrow"}, ctx)))

    assert changes["ok"] is True
    assert "Raumwechsel" in changes["summary"]
    assert debug["data"]["debug"]["changes_count"] == 1
    assert "edupage_get_timetable" in tool_map
