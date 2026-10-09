"""Rund ums Display: Wetter-Normalisierung, Briefing-Planung, Sicherheits-Fixes,
Admin-Seite „Display“ und Bildgenerierung (alles ohne Netz)."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import briefing, db, notify
from app.config import get_settings
from app.display import hub as hublib
from app.display import service, speech
from app.plugins.builtin import weather
from app.tools import ToolContext
from app.web import admin as web_admin
from app.web import admin_display, auth

TZ = ZoneInfo("Europe/Berlin")


# ─── Wetter ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("code,expected", [
    (211, "thunder"), (301, "drizzle"), (500, "rain"), (511, "sleet"), (521, "rain"), (601, "snow"),
    (613, "sleet"), (621, "snow"), (741, "fog"), (721, "fog"), (771, "wind"), (781, "wind"),
    (800, "clear"), (801, "partly"), (802, "partly"), (803, "cloudy"), (804, "cloudy"), (None, "cloudy"),
])
def test_condition_from_owm(code, expected):
    assert weather.condition_from_owm(code) == expected


def test_strong_wind_overrides_calm_sky_but_not_rain():
    assert weather.condition_from_owm(800, wind_kmh=60) == "wind"
    assert weather.condition_from_owm(500, wind_kmh=60) == "rain"


def _fc_item(ts, temp, code, pod="d", pop=0.0, wind=3.0):
    return {"dt": ts, "main": {"temp": temp, "feels_like": temp - 1, "temp_min": temp - 1, "temp_max": temp + 1},
            "weather": [{"id": code, "description": "x", "icon": f"01{pod}"}],
            "wind": {"speed": wind}, "pop": pop, "sys": {"pod": pod}}


def test_build_weather_data_shape():
    base = int(datetime(2026, 10, 9, 6, 0, tzinfo=TZ).timestamp())
    items = [_fc_item(base + i * 3 * 3600, 10 + i, 500 if i == 3 else 801, "n" if i in (0, 6, 7) else "d",
                      pop=0.8 if i == 3 else 0.1) for i in range(16)]
    current = {"dt": base, "name": "Frankfurt am Main", "timezone": 7200,
               "main": {"temp": 9.6, "feels_like": 7.2, "humidity": 81},
               "weather": [{"id": 803, "description": "Überwiegend bewölkt", "icon": "04n"}], "wind": {"speed": 5}}
    forecast = {"cod": "200", "city": {"name": "Frankfurt am Main", "timezone": 7200}, "list": items}
    data = weather.build_weather_data(current, forecast, units="metric",
                                      now=datetime(2026, 10, 9, 6, 0, tzinfo=TZ))
    assert data["location"] == "Frankfurt am Main"
    now = data["now"]
    assert now == {"temp": 10, "feels_like": 7, "condition": "cloudy", "is_day": False,
                   "description": "Überwiegend bewölkt", "humidity": 81, "wind_kmh": 18.0,
                   "high": now["high"], "low": now["low"]}
    assert now["high"] >= 10 and now["low"] <= 10
    assert len(data["hourly"]) == 8
    h = data["hourly"][0]
    assert set(h) == {"time", "temp", "condition", "is_day", "pop"} and h["time"].endswith("+02:00")
    assert data["hourly"][3]["condition"] == "rain"
    assert data["daily"][0]["date"] == "2026-10-09" and data["daily"][0]["condition"] == "rain"
    assert set(data["daily"][0]) == {"date", "min", "max", "condition", "pop"}
    assert all(d["condition"] in weather.CONDITIONS for d in data["daily"])


def test_get_weather_tool_returns_text_and_structured_data(monkeypatch):
    plugin = weather.WeatherPlugin({"__enabled": True, "api_key": "k", "city": "Frankfurt,DE", "units": "metric"})
    base = int(datetime(2026, 10, 9, 12, 0, tzinfo=TZ).timestamp())

    async def fc(city=None, *, cnt=8):
        return {"cod": "200", "city": {"name": "Frankfurt", "timezone": 7200},
                "list": [_fc_item(base + i * 10800, 15, 800) for i in range(10)]}

    async def cur(city=None):
        return {"dt": base, "name": "Frankfurt", "main": {"temp": 15, "feels_like": 14, "humidity": 50},
                "weather": [{"id": 800, "description": "klar", "icon": "01d"}], "wind": {"speed": 2}}
    monkeypatch.setattr(plugin, "_fetch_forecast", fc)
    monkeypatch.setattr(plugin, "_fetch_current", cur)
    tool = plugin.tools()[0]
    res = json.loads(asyncio.run(tool.handler({}, ToolContext(thread_id="t", channel="web", contact={}))))
    assert res["ok"] is True and "Wetter für Frankfurt" in res["summary"]
    assert res["data"]["now"]["condition"] == "clear" and res["summary"].count("\n") == 8


# ─── Zustell-Routing (rein) ───────────────────────────────────────────────────
def test_choose_channels_display():
    assert notify.choose_channels("normal", at_home=None, awake=True, display=True) == ["push", "display"]
    assert notify.choose_channels("urgent", at_home=True, awake=True, display=True) == ["push", "speak", "display"]
    # nachweislich unterwegs oder schlafend → nicht an die Wand
    assert notify.choose_channels("normal", at_home=False, awake=True, display=True) == ["push"]
    assert notify.choose_channels("urgent", at_home=True, awake=False, display=True) == ["push"]
    assert notify.choose_channels("control", at_home=True, awake=True, display=True) == ["telegram"]
    # ohne Display unverändert
    assert notify.choose_channels("normal", at_home=True, awake=True) == ["push"]


# ─── Briefing-Planung ─────────────────────────────────────────────────────────
def test_briefing_due_window_and_debounce():
    t = time(7, 0)
    assert briefing.due(datetime(2026, 10, 9, 7, 0, 10, tzinfo=TZ), t) is True
    assert briefing.due(datetime(2026, 10, 9, 7, 1, 50, tzinfo=TZ), t) is True
    assert briefing.due(datetime(2026, 10, 9, 7, 2, 0, tzinfo=TZ), t) is False
    assert briefing.due(datetime(2026, 10, 9, 6, 59, tzinfo=TZ), t) is False
    assert briefing.due(datetime(2026, 10, 9, 7, 0, 30, tzinfo=TZ), t, datetime(2026, 10, 9).date()) is False


def test_briefing_settings_web_overrides_env(memdb, monkeypatch):
    monkeypatch.setenv("ASTRA_BRIEFING_TIME", "06:30")
    monkeypatch.setenv("ASTRA_BRIEFING_ENABLED", "true")
    get_settings.cache_clear()
    cfg = asyncio.run(briefing.briefing_settings())
    assert cfg == {"time": "06:30", "telegram": True, "display": False}
    memdb["app_settings"] = {"briefing": {"time": "07:15", "telegram": False},
                             "display": {"display_briefing": True}}
    assert asyncio.run(briefing.briefing_settings()) == {"time": "07:15", "telegram": False, "display": True}
    memdb["app_settings"] = {"briefing": {"time": "25:99"}}
    assert asyncio.run(briefing.briefing_settings())["time"] == "06:30"
    get_settings.cache_clear()


def test_run_scheduled_without_telegram_still_serves_display(memdb, monkeypatch):
    hublib._hub = None
    sent = []

    async def send_display():
        sent.append("display")
        return True

    async def send(chat_id=None):
        sent.append("telegram")
        return True
    monkeypatch.setattr(briefing, "send_display", send_display)
    monkeypatch.setattr(briefing, "send", send)

    async def run():
        hublib.get_hub().subscribe()
        return await briefing.run_scheduled({"time": "07:00", "telegram": True, "display": True})
    out = asyncio.run(run())
    assert out == {"display": True} and sent == ["display"]   # kein Bot-Token → kein Telegram
    hublib._hub = None


def test_spoken_sentences():
    assert briefing.calendar_sentence([]) == "Heute stehen keine Termine an."
    one = [{"title": "Klavier", "start": "2026-10-09T16:00:00+02:00", "all_day": False}]
    assert briefing.calendar_sentence(one) == "Heute hast du einen Termin: Klavier um 16:00 Uhr."
    w = {"now": {"temp": 12, "description": "leichter Regen", "high": 15}}
    assert briefing.weather_sentence(w) == "Draußen sind es 12 Grad, leichter Regen, heute bis 15 Grad."


# ─── Sicherheits-Fixes ────────────────────────────────────────────────────────
def test_verify_secret_uses_constant_time_compare(monkeypatch):
    from app import main
    calls = []
    real = main.hmac.compare_digest

    def spy(a, b):
        calls.append(1)
        return real(a, b)
    monkeypatch.setattr(main.hmac, "compare_digest", spy)
    secret = get_settings().cortex_shared_secret
    main._verify_secret(secret)
    with pytest.raises(HTTPException):
        main._verify_secret("wrong")
    with pytest.raises(HTTPException):
        main._verify_secret(None)
    assert len(calls) == 2


def test_weak_secret_warning(caplog):
    from app import main
    assert main.warn_weak_secret("dev-secret") is True
    assert "CORTEX_SHARED_SECRET" in caplog.text
    assert main.warn_weak_secret("a" * 64) is False


def test_dashboard_requires_admin(memdb):
    from app import main
    c = TestClient(main.app)          # ohne Lifespan (kein Postgres)
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/setup"
    asyncio.run(auth.set_admin_password("password1"))
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_display_routes_are_mounted_and_not_in_caddyfile(memdb):
    from pathlib import Path

    from app import main
    service._reset_for_tests()
    c = TestClient(main.app)
    for method, path in (("get", "/display/v1/hello"), ("post", "/display/v1/message"),
                         ("get", "/display/v1/glance"), ("post", "/display/v1/tts"),
                         ("get", "/display/v1/events")):
        assert getattr(c, method)(path).status_code == 401, path      # gemountet, aber geschützt
    service._reset_for_tests()
    caddy = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text()
    assert "handle /display" not in caddy


# ─── Admin-Seite „Display“ ────────────────────────────────────────────────────
def _admin() -> TestClient:
    from app.plugins.registry import _discover_classes, get_manager
    mgr = get_manager()
    mgr._classes = _discover_classes()
    mgr._instances = {c.slug: c({"__enabled": False}) for c in mgr._classes}
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(admin_display.router)
    c = TestClient(app)
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123", "confirm": "geheim123"},
           follow_redirects=False)
    return c


def test_admin_display_page_shows_url_and_token(memdb, monkeypatch):
    service._reset_for_tests()

    async def list_rules(**kw):
        return []
    monkeypatch.setattr(db, "list_rules", list_rules)
    c = _admin()
    r = c.get("/admin/display")
    assert r.status_code == 200
    tok = asyncio.run(service.display_token())
    assert tok in r.text and "OpenBoard-Display" in r.text and "http://testserver" in r.text
    assert 'href="/admin/display" class="navlink active"' in r.text
    # Token neu erzeugen (CSRF-geschützt)
    assert c.post("/admin/display/token", data={"csrf": "x"}).status_code == 403
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/display/token", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert asyncio.run(service.display_token()) != tok
    service._reset_for_tests()


def test_admin_display_settings_and_alarm(memdb, monkeypatch):
    service._reset_for_tests()
    added = {}

    async def list_rules(**kw):
        return []

    async def add_rule(**kw):
        added.update(kw)
        return 5
    monkeypatch.setattr(db, "list_rules", list_rules)
    monkeypatch.setattr(db, "add_rule", add_rule)
    c = _admin()
    c.get("/admin/display")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/display/settings", data={
        "csrf": csrf, "tts_voice": "shimmer", "tts_model": "gpt-4o-mini-tts", "display_briefing": "1",
        "notify": "1", "briefing_time": "06:45"}, follow_redirects=False)
    assert r.status_code == 303
    appset = memdb["app_settings"]
    assert appset["display"]["tts_voice"] == "shimmer" and appset["display"]["display_briefing"] is True
    assert appset["briefing"] == {"telegram": False, "time": "06:45"}
    r = c.post("/admin/display/alarm", data={"csrf": csrf, "time": "6:40", "label": "Schule", "day0": "1",
                                             "day4": "1", "briefing": "1", "sound": "classic"},
               follow_redirects=False)
    assert r.status_code == 303
    assert added["trigger"] == {"type": "schedule", "at": "06:40", "days": [0, 4]}
    assert added["actions"][0]["data"] == {"label": "Schule", "sound": "classic", "briefing": True}
    service._reset_for_tests()


# ─── Bildgenerierung ──────────────────────────────────────────────────────────
def test_generate_image_saves_file_and_pushes_card(memdb, monkeypatch):
    from app.display import cards as cardlib
    from app.plugins.builtin import image_generation as ig
    hublib._hub = None
    calls = {}

    class FakeImages:
        async def generate(self, **kw):
            calls.update(kw)
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"\xff\xd8img").decode(),
                                                         revised_prompt=None)], usage=None)

    class FakeClient:
        def __init__(self, api_key):
            self.images = FakeImages()
    monkeypatch.setattr(ig, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(speech, "openai_key", lambda: "sk-test")
    plugin = ig.ImageGenerationPlugin({"__enabled": True, "model": "gpt-image-1", "size": "1536x1024",
                                       "quality": "medium", "daily_limit": 1})
    tool = plugin.tools()[0]
    assert tool.safety == "external_send" and tool.owner_only

    async def run():
        sub = hublib.get_hub().subscribe()
        ctx = ToolContext(thread_id="t", channel="web", contact={}, is_owner=True)
        first = json.loads(await tool.handler({"prompt": "Ein Roboter", "board": True}, ctx))
        second = json.loads(await tool.handler({"prompt": "Noch einer"}, ctx))
        return first, second, [sub.queue.get_nowait(), sub.queue.get_nowait()]
    first, second, events = asyncio.run(run())
    assert first["ok"] is True and first["data"]["ref"].startswith("astra-file:img-")
    assert "data:" not in json.dumps(first)                      # kein Bild im LLM-Kontext
    assert (cardlib.media_dir() / first["data"]["file"]).read_bytes() == b"\xff\xd8img"
    assert calls["model"] == "gpt-image-1" and calls["output_format"] == "jpeg"
    assert events[0][0] == "card" and events[0][1]["card"]["data"]["src"].startswith("data:image/jpeg;base64,")
    assert events[1][0] == "board" and events[1][1]["op"] == "add_image"
    assert second["ok"] is False and "Tageslimit" in second["summary"]
    hublib._hub = None
