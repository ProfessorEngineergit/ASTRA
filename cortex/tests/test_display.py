"""OpenBoard-Display (ASTRA-Display-Protokoll v1): Auth, Gesprächs-Turn, Karten, SSE-Hub,
Regel-Aktion `display`, Glance, Plugin-Werkzeuge und Kanal-Verdrahtung."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.display import api as display_api
from app.display import cards as cardlib
from app.display import hub as hublib
from app.display import service, speech
from app.tools import ToolContext, tool_result
from app.web import auth

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture
def disp(memdb, monkeypatch):
    """Frischer Hub/Token-Cache, keine echte TTS, Token fest vorgegeben über den Store."""
    hublib._hub = None
    service._reset_for_tests()
    display_api._failures.clear()

    async def no_tts(text):
        return None
    monkeypatch.setattr(speech, "synthesize", no_tts)
    yield memdb
    hublib._hub = None
    service._reset_for_tests()
    get_settings.cache_clear()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(display_api.router)
    return TestClient(app)


def _token() -> str:
    return asyncio.run(service.display_token())


def _auth(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


# ─── Auth ─────────────────────────────────────────────────────────────────────
def test_hello_requires_bearer_token(disp):
    c = _client()
    assert c.get("/display/v1/hello").status_code == 401
    assert c.get("/display/v1/hello", headers=_auth("falsch")).status_code == 401
    r = c.get("/display/v1/hello", headers=_auth(_token()))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "ASTRA" and "version" in body and "message" in body["capabilities"]


def test_admin_cookie_and_shared_secret_are_not_accepted(disp):
    asyncio.run(auth.set_admin_password("password1"))
    session = asyncio.run(auth.issue_session())
    c = _client()
    c.cookies.set(auth.COOKIE_NAME, session)
    secret = get_settings().cortex_shared_secret
    for path in ("/display/v1/hello", "/display/v1/glance", "/display/v1/events"):
        r = c.get(path, headers={"X-Astra-Secret": secret})
        assert r.status_code == 401, path
    r = c.post("/display/v1/message", json={"text": "hi"}, headers={"X-Astra-Secret": secret})
    assert r.status_code == 401


def test_token_generated_once_and_stored_encrypted(disp):
    tok = _token()
    assert len(tok) >= 32
    stored = disp[service.TOKEN_KEY]
    assert stored and tok not in stored          # Fernet-verschlüsselt, nicht im Klartext
    service._reset_for_tests()                     # Cache leer → aus dem Store lesen
    assert _token() == tok


def test_regenerate_invalidates_old_token(disp):
    old = _token()
    new = asyncio.run(service.regenerate_token())
    assert new != old
    c = _client()
    assert c.get("/display/v1/hello", headers=_auth(old)).status_code == 401
    assert c.get("/display/v1/hello", headers=_auth(new)).status_code == 200


def test_env_token_wins_and_cannot_be_regenerated(disp, monkeypatch):
    monkeypatch.setenv("ASTRA_DISPLAY_TOKEN", "env-token-0123456789abcdef")
    get_settings.cache_clear()
    assert service.token_source() == "env"
    assert _token() == "env-token-0123456789abcdef"
    with pytest.raises(RuntimeError):
        asyncio.run(service.regenerate_token())


def test_token_compare_is_constant_time(disp, monkeypatch):
    calls = []
    real = service.hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)
    monkeypatch.setattr(service.hmac, "compare_digest", spy)
    assert service.tokens_equal("abc", "abc") is True
    assert service.tokens_equal("abc", "abcdef") is False
    assert calls and all(a == b == 32 for a, b in calls)   # gleich lange Digests
    assert service.tokens_equal("", "abc") is False


def test_failed_attempts_are_rate_limited(disp):
    c = _client()
    for _ in range(display_api._FAIL_MAX):
        assert c.get("/display/v1/hello", headers=_auth("nope")).status_code == 401
    assert c.get("/display/v1/hello", headers=_auth(_token())).status_code == 429


# ─── Gesprächs-Turn ───────────────────────────────────────────────────────────
def _cal_call():
    events = [{"summary": "Klavier", "start": {"dateTime": "2026-10-09T16:00:00+02:00"},
               "end": {"dateTime": "2026-10-09T17:00:00+02:00"}, "colorId": "5"}]
    return {"tool": "calendar_today", "args": {}, "ok": True, "summary": "Heutige Termine: …",
            "result": json.loads(tool_result(ok=True, summary="Heutige Termine", data=events,
                                             source="google_calendar"))}


def test_message_flow_with_mocked_agent(disp, monkeypatch):
    from app import agent
    from app.persona import Register
    seen = {}

    async def fake_reply(**kw):
        seen.update(kw)
        return {"reply": "Heute hast du Klavier um 16 Uhr.", "tool_calls": [_cal_call()]}
    monkeypatch.setattr(agent, "generate_reply_meta", fake_reply)
    c = _client()
    r = c.post("/display/v1/message", headers=_auth(_token()), json={
        "session_id": "display-main", "text": "Was steht heute an?", "speak": False,
        "context": {"active_app": "astra", "locale": "de-DE", "theme": "dark"}})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"session_id", "transcript", "reply", "cards", "speech", "actions"}
    assert body["transcript"] == "Was steht heute an?"
    assert body["reply"].startswith("Heute hast du")
    assert body["speech"] is None and body["actions"] == []
    card = body["cards"][0]
    assert card["type"] == "calendar"
    ev = card["data"]["events"][0]
    assert ev == {"title": "Klavier", "start": "2026-10-09T16:00:00+02:00",
                  "end": "2026-10-09T17:00:00+02:00", "all_day": False, "color": "#f6bf26"}
    assert seen["channel"] == "display" and seen["register"] == Register.OWNER
    assert seen["thread_id"] == "display:display-main"
    assert "Aktive App: astra" in seen["extra_system"]
    # Verlauf ist gespeichert und fließt in den nächsten Turn.
    stored = disp[service.SESSION_PREFIX + "display-main"]["messages"]
    assert [m["role"] for m in stored] == ["user", "assistant"]
    c.post("/display/v1/message", headers=_auth(_token()), json={"text": "Und morgen?"})
    assert [m["content"] for m in seen["history"]] == [
        "Was steht heute an?", "Heute hast du Klavier um 16 Uhr.", "Und morgen?"]


def test_new_conversation_resets_history(disp, monkeypatch):
    from app import agent
    seen = {}

    async def fake_reply(**kw):
        seen.update(kw)
        return {"reply": "Okay.", "tool_calls": []}
    monkeypatch.setattr(agent, "generate_reply_meta", fake_reply)
    c = _client()
    c.post("/display/v1/message", headers=_auth(_token()), json={"session_id": "display-main", "text": "Erste Frage"})
    c.post("/display/v1/message", headers=_auth(_token()), json={
        "session_id": "display-main", "text": "Neues Thema", "context": {"new_conversation": True}})
    assert [m["content"] for m in seen["history"]] == ["Neues Thema"]


def test_message_speak_uses_voice_register_and_returns_speech(disp, monkeypatch):
    from app import agent
    from app.persona import Register
    seen = {}

    async def fake_reply(**kw):
        seen.update(kw)
        return {"reply": "**Klar.** Erledigt.", "tool_calls": []}

    async def fake_tts(text):
        return {"mime": "audio/mpeg", "b64": base64.b64encode(text.encode()).decode()}
    monkeypatch.setattr(agent, "generate_reply_meta", fake_reply)
    monkeypatch.setattr(speech, "synthesize", fake_tts)
    r = _client().post("/display/v1/message", headers=_auth(_token()),
                       json={"text": "Mach das Licht an", "speak": True})
    body = r.json()
    assert seen["register"] == Register.VOICE
    assert "VORGELESEN" in seen["extra_system"]
    assert body["speech"]["mime"] == "audio/mpeg"


def test_message_with_audio_is_transcribed(disp, monkeypatch):
    from app import agent
    from app.integrations import transcription

    class FakeTr:
        enabled = True

        async def transcribe(self, audio, filename="voice.ogg"):
            assert audio == b"RIFFdata" and filename == "voice.wav"
            return "Wie spät ist es?"

    async def fake_reply(**kw):
        return {"reply": "Viertel nach drei.", "tool_calls": []}
    monkeypatch.setattr(transcription, "get_transcriber", lambda: FakeTr())
    monkeypatch.setattr(agent, "generate_reply_meta", fake_reply)
    r = _client().post("/display/v1/message", headers=_auth(_token()), json={
        "audio": {"mime": "audio/wav", "b64": base64.b64encode(b"RIFFdata").decode()}})
    assert r.status_code == 200
    assert r.json()["transcript"] == "Wie spät ist es?"


def test_message_rejects_empty_and_bad_audio(disp):
    c = _client()
    assert c.post("/display/v1/message", headers=_auth(_token()), json={}).status_code == 400
    r = c.post("/display/v1/message", headers=_auth(_token()), json={"audio": {"b64": "!!kein base64!!"}})
    assert r.status_code == 400


def test_session_history_is_capped(disp, monkeypatch):
    from app import agent
    seen = {}

    async def fake_reply(**kw):
        seen["n"] = len(kw["history"])
        return {"reply": "ok", "tool_calls": []}
    monkeypatch.setattr(agent, "generate_reply_meta", fake_reply)
    disp[service.SESSION_PREFIX + "s1"] = {"messages": [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(100)]}
    asyncio.run(service.handle_message({"session_id": "s1", "text": "neu"}))
    assert seen["n"] == service.MAX_CONTEXT
    assert len(disp[service.SESSION_PREFIX + "s1"]["messages"]) == service.MAX_STORED


def test_tts_endpoint(disp, monkeypatch):
    c = _client()
    monkeypatch.setattr(speech, "available", lambda: False)
    assert c.post("/display/v1/tts", headers=_auth(_token()), json={"text": "Hallo"}).status_code == 503

    async def fake_tts(text):
        return {"mime": "audio/mpeg", "b64": "QUJD"}
    monkeypatch.setattr(speech, "available", lambda: True)
    monkeypatch.setattr(speech, "synthesize", fake_tts)
    r = c.post("/display/v1/tts", headers=_auth(_token()), json={"text": "Hallo"})
    assert r.json() == {"mime": "audio/mpeg", "b64": "QUJD"}


def test_speakable_strips_markdown_links_and_emoji():
    out = speech.speakable("## Heute\n- **Klavier** um 16 Uhr 🎹\n[Link](https://x.y) siehe https://a.b")
    assert "*" not in out and "#" not in out and "http" not in out and "🎹" not in out
    assert "Klavier um 16 Uhr" in out and "Link" in out


# ─── Glance ───────────────────────────────────────────────────────────────────
def test_glance_shape_without_integrations(disp, monkeypatch):
    rules = [{"id": 7, "enabled": True, "confirmed_at": "x", "name": "Wecker",
              "trigger": {"type": "schedule", "at": "07:00"},
              "actions": [{"type": "display", "event": "alarm", "data": {"label": "Aufstehen"}}]},
             {"id": 8, "enabled": True, "confirmed_at": "x", "name": "Andere",
              "trigger": {"type": "schedule", "at": "08:00"}, "actions": [{"type": "notify"}]}]

    async def list_rules(**kw):
        return rules
    monkeypatch.setattr(db, "list_rules", list_rules)
    r = _client().get("/display/v1/glance", headers=_auth(_token()))
    body = r.json()
    assert set(body) == {"generated_at", "greeting", "weather", "calendar", "briefing", "alarms"}
    assert body["weather"] is None and body["calendar"] is None
    assert [a["id"] for a in body["alarms"]] == ["rule-7"]
    assert body["alarms"][0]["label"] == "Aufstehen" and "T07:00" in body["alarms"][0]["at"]


@pytest.mark.parametrize("hour,word", [(7, "Guten Morgen"), (13, "Hallo"), (19, "Guten Abend"), (23, "Gute Nacht")])
def test_greeting_by_time_of_day(hour, word):
    assert service.greeting(datetime(2026, 10, 9, hour, 0, tzinfo=TZ), "Bahrian") == f"{word}, Bahrian"


# ─── Karten ───────────────────────────────────────────────────────────────────
def test_extract_home_weather_and_generic():
    home = {"tool": "home_state", "ok": True, "summary": "Wohnzimmer: …", "result": {"ok": True, "data": {
        "area": "Wohnzimmer", "values": [{"entity_id": "sensor.temp", "name": "Temperatur",
                                          "state": "21.5", "unit": "°C"}]}}}
    weather = {"tool": "get_weather", "ok": True, "summary": "…", "result": {"ok": True, "data": {
        "location": "Frankfurt", "now": {"temp": 18, "condition": "partly", "description": "Wolken"},
        "hourly": [], "daily": []}}}
    lst = {"tool": "todoist_list", "ok": True, "summary": "Offene Aufgaben", "result": {"ok": True, "data": [
        {"content": "x", "title": "Milch kaufen", "description": "2 Liter"}, {"title": "Steuer"}]}}
    facts = {"tool": "proxmox_status", "ok": True, "summary": "Proxmox", "result": {"ok": True, "data": {
        "cpu": "12 %", "ram_used": "8 GB", "online": True}}}
    admin = {"tool": "astra_get_settings", "ok": True, "summary": "x", "result": {"ok": True, "data": {"a": 1}}}
    failed = {"tool": "todoist_list", "ok": False, "summary": "Fehler", "result": {"ok": False, "data": [{"title": "x"}]}}
    cards, actions = cardlib.extract([home, weather, lst, facts, admin, failed])
    assert [c["type"] for c in cards] == ["home", "weather", "list", "facts"]
    assert cards[0]["data"]["entities"][0] == {"name": "Temperatur", "state": "21.5", "domain": "sensor", "unit": "°C"}
    assert cards[2]["data"]["items"][0] == {"title": "Milch kaufen", "detail": "2 Liter"}
    assert {"label": "online", "value": "ja"} in cards[3]["data"]["rows"]
    assert actions == []


def test_extract_explicit_display_payload_and_drops_invalid():
    call = {"tool": "display_show", "ok": True, "summary": "", "result": {"ok": True, "data": {"display": {
        "cards": [{"type": "markdown", "data": {"text": "# Hi"}}, {"type": "nope", "data": {}}],
        "actions": [{"type": "open_app", "app": "board"}]}}}}
    cards, actions = cardlib.extract([call])
    assert len(cards) == 1 and cards[0]["type"] == "markdown" and cards[0]["id"].startswith("markdown-")
    assert actions == [{"type": "open_app", "app": "board"}]


def test_validate_card_schema():
    ok = cardlib.validate_card({"type": "facts", "title": "T", "data": {"rows": {"a": 1}}, "ttl_seconds": "30"})
    assert ok["data"]["rows"] == [{"label": "a", "value": "1"}] and ok["ttl_seconds"] == 30
    with pytest.raises(cardlib.CardError):
        cardlib.validate_card({"type": "sketch", "data": {"svg": "<svg><script>alert(1)</script></svg>"}})
    with pytest.raises(cardlib.CardError):
        cardlib.validate_card({"type": "sketch", "data": {"svg": '<svg onload="x()"></svg>'}})
    assert cardlib.validate_card({"type": "sketch", "data": {"svg": "<svg viewBox='0 0 1 1'/>"}})
    with pytest.raises(cardlib.CardError):
        cardlib.validate_card({"type": "image", "data": {"src": "javascript:alert(1)"}})
    with pytest.raises(cardlib.CardError):
        cardlib.validate_card({"type": "weather", "data": {"now": {"condition": "sunny"}}})


def test_board_ops():
    assert cardlib.validate_board_op({"op": "add_text", "text": "Hi", "x": 10}) == {"op": "add_text", "text": "Hi", "x": 10}
    assert cardlib.validate_board_op({"op": "add_mermaid", "definition": "graph TD; A-->B"})["op"] == "add_mermaid"
    with pytest.raises(cardlib.CardError):
        cardlib.validate_board_op({"op": "rm_rf"})


def test_file_refs_are_materialized_to_data_urls(disp):
    name = "img-test.jpg"
    (cardlib.media_dir() / name).write_bytes(b"\xff\xd8jpeg")
    card = cardlib.validate_card({"type": "image", "data": {"src": cardlib.file_ref(name), "alt": "x"}})
    out = cardlib.materialize({"card": card})
    assert out["card"]["data"]["src"] == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode()
    with pytest.raises(cardlib.CardError):
        cardlib.materialize({"src": "astra-file:../../etc/passwd"})


# ─── SSE-Hub ──────────────────────────────────────────────────────────────────
def test_hub_publish_subscribe_and_drop_oldest():
    async def run():
        hub = hublib.DisplayHub(maxsize=2)
        assert hub.publish("say", {"text": "niemand da"}) == 0
        sub = hub.subscribe(peer="10.0.0.5")
        assert hub.connected == 1
        for i in range(3):
            hub.publish("say", {"text": str(i)})
        got = [sub.queue.get_nowait(), sub.queue.get_nowait()]
        assert [d["text"] for _e, d in got] == ["1", "2"] and sub.dropped == 1
        with pytest.raises(ValueError):
            hub.publish("unbekannt", {})
        hub.unsubscribe(sub)
        assert hub.connected == 0
    asyncio.run(run())


def test_format_sse_frame():
    assert hublib.format_sse("say", {"text": "a\nb"}) == 'event: say\ndata: {"text":"a\\nb"}\n\n'


def test_event_stream_hello_events_and_ping(disp):
    class Req:
        client = None
        headers = {"user-agent": "openboard"}

        async def is_disconnected(self):
            return False

    async def run():
        gen = display_api.event_stream(Req(), ping_seconds=0.05)
        first = await gen.__anext__()
        assert first.startswith("event: hello\n") and '"version"' in first
        assert service.connected() == 1
        service.publish("command", {"action": "wake"})
        assert await gen.__anext__() == 'event: command\ndata: {"action":"wake"}\n\n'
        assert await gen.__anext__() == "event: ping\ndata: {}\n\n"
        await gen.aclose()
        assert service.connected() == 0
    asyncio.run(run())


# ─── Regel-Aktion `display` ───────────────────────────────────────────────────
def test_rule_display_alarm_pushes_alarm_event(disp, monkeypatch):
    from app import rules

    async def fake_tts(text):
        return {"mime": "audio/mpeg", "b64": "eA=="}
    monkeypatch.setattr(speech, "synthesize", fake_tts)

    async def run():
        sub = hublib.get_hub().subscribe()
        res = await rules.run_actions([{"type": "display", "event": "alarm",
                                        "data": {"label": "Aufstehen", "sound": "classic", "speak": "Guten Morgen"}}],
                                      rule_id=12)
        event, data = sub.queue.get_nowait()
        return res, event, data
    res, event, data = asyncio.run(run())
    assert res[0]["ok"] is True and res[0]["type"] == "display"
    assert event == "alarm"
    assert data["label"] == "Aufstehen" and data["sound"] == "classic" and data["id"].startswith("rule-12-")
    assert data["speak"] == "Guten Morgen" and data["speech"]["mime"] == "audio/mpeg"


def test_rule_display_alarm_with_briefing_attaches_cards(disp, monkeypatch):
    from app import briefing

    async def spoken():
        return "Draußen 12 Grad.", [cardlib.validate_card({"type": "markdown", "data": {"text": "x"}})]
    monkeypatch.setattr(briefing, "compose_spoken", spoken)
    payload = asyncio.run(service.build_alarm({"label": "Wecker", "briefing": True}, alarm_id="a1"))
    assert payload["speak"] == "Draußen 12 Grad." and payload["cards"][0]["type"] == "markdown"


def test_rule_display_alarm_without_display_falls_back_to_notify(disp, monkeypatch):
    from app import notify, rules
    sent = []

    async def fake_notify(text, **kw):
        sent.append((text, kw.get("urgency")))
        return {"push": True}
    monkeypatch.setattr(notify, "notify", fake_notify)
    res = asyncio.run(rules.run_actions([{"type": "display", "event": "alarm", "data": {"label": "Aufstehen"}}]))
    assert res[0]["ok"] is False and sent == [("⏰ Aufstehen", "urgent")]


def test_rule_display_command_and_say(disp):
    async def run():
        sub = hublib.get_hub().subscribe()
        await service.run_rule_action({"type": "display", "event": "command", "action": "open_app", "app": "board"})
        await service.run_rule_action({"type": "display", "event": "say", "data": {"text": "Hallo"}})
        return [sub.queue.get_nowait(), sub.queue.get_nowait()]
    got = asyncio.run(run())
    assert got[0] == ("command", {"action": "open_app", "app": "board"})
    assert got[1] == ("say", {"text": "Hallo"})


def test_one_shot_rule_disables_after_scheduled_fire(disp, monkeypatch):
    from app import rules
    calls = []

    async def mark(rid, res):
        calls.append(("mark", rid))

    async def set_enabled(rid, on):
        calls.append(("enabled", rid, on))

    async def fake_actions(actions, **kw):
        return [{"ok": True}]
    monkeypatch.setattr(db, "mark_rule_run", mark)
    monkeypatch.setattr(db, "set_rule_enabled", set_enabled)
    monkeypatch.setattr(rules, "run_actions", fake_actions)
    rule = {"id": 3, "trigger": {"type": "schedule", "at": "07:00", "date": "2026-10-10"}, "actions": [{}]}
    asyncio.run(rules.fire_rule(rule))                      # manueller Testlauf: bleibt aktiv
    assert ("enabled", 3, False) not in calls
    asyncio.run(rules.fire_rule(rule, scheduled=True))
    assert ("enabled", 3, False) in calls


def test_schedule_with_date_only_fires_that_day():
    from app import rules
    trig = {"type": "schedule", "at": "07:00", "date": "2026-10-10"}
    assert rules.schedule_matches(trig, datetime(2026, 10, 10, 7, 0, tzinfo=TZ)) is True
    assert rules.schedule_matches(trig, datetime(2026, 10, 11, 7, 0, tzinfo=TZ)) is False


def test_next_occurrence():
    from app import rules
    now = datetime(2026, 10, 9, 8, 0, tzinfo=TZ)          # Freitag
    assert rules.next_occurrence({"type": "schedule", "at": "07:00"}, now) == datetime(2026, 10, 10, 7, 0, tzinfo=TZ)
    assert rules.next_occurrence({"type": "schedule", "at": "09:00"}, now) == datetime(2026, 10, 9, 9, 0, tzinfo=TZ)
    werktags = {"type": "schedule", "at": "06:40", "days": [0, 1, 2, 3, 4]}
    assert rules.next_occurrence(werktags, now) == datetime(2026, 10, 12, 6, 40, tzinfo=TZ)   # Montag
    assert rules.next_occurrence({"type": "schedule", "at": "07:00", "date": "2026-10-08"}, now) is None


# ─── Plugin-Werkzeuge ─────────────────────────────────────────────────────────
def _plugin():
    from app.plugins.builtin.display import DisplayPlugin
    return DisplayPlugin({"__enabled": True})


def _tool(name):
    return next(t for t in _plugin().tools() if t.name == name)


def _ctx(channel="web"):
    return ToolContext(thread_id="t", channel=channel, contact={}, is_owner=True)


def test_display_show_on_display_channel_goes_into_response(disp):
    raw = asyncio.run(_tool("display_show").handler(
        {"type": "list", "title": "Einkauf", "data": {"items": ["Milch", "Brot"]}}, _ctx("display")))
    res = json.loads(raw)
    assert res["ok"] is True
    assert res["data"]["display"]["cards"][0]["data"]["items"][1] == {"title": "Brot"}
    assert hublib.get_hub().connected == 0


def test_display_show_pushes_card_when_connected(disp):
    async def run():
        sub = hublib.get_hub().subscribe()
        raw = await _tool("display_show").handler({"type": "markdown", "data": {"text": "Hallo"}}, _ctx())
        return json.loads(raw), sub.queue.get_nowait()
    res, (event, data) = asyncio.run(run())
    assert res["ok"] is True and event == "card" and data["card"]["data"]["text"] == "Hallo"


def test_display_tools_without_display_report_it(disp):
    res = json.loads(asyncio.run(_tool("display_say").handler({"text": "Hi"}, _ctx())))
    assert res["ok"] is False and "Kein Display" in res["summary"]


def test_display_open_app_on_display_channel_is_action(disp):
    res = json.loads(asyncio.run(_tool("display_open_app").handler({"app": "board"}, _ctx("display"))))
    assert res["data"]["display"]["actions"] == [{"type": "open_app", "app": "board"}]


def test_display_alarm_set_creates_one_shot_rule(disp, monkeypatch):
    created = {}

    async def add_rule(**kw):
        created.update(kw)
        return 42
    monkeypatch.setattr(db, "add_rule", add_rule)
    res = json.loads(asyncio.run(_tool("display_alarm_set").handler(
        {"time": "7:00", "date": "2099-01-02", "label": "Aufstehen"}, _ctx())))
    assert res["ok"] is True and "#42" in res["summary"]
    assert created["trigger"] == {"type": "schedule", "at": "07:00", "date": "2099-01-02"}
    assert created["confirmed"] is True
    action = created["actions"][0]
    assert action["type"] == "display" and action["event"] == "alarm"
    assert action["data"] == {"label": "Aufstehen", "sound": "gentle", "briefing": True}


def test_display_alarm_template_is_valid_rule_shape():
    tmpl = _plugin().rule_templates()[0]
    assert tmpl["name"] == "Wecker" and tmpl["trigger"]["days"] == [0, 1, 2, 3, 4]
    assert tmpl["actions"][0]["type"] == "display"


# ─── Kanal-Verdrahtung ────────────────────────────────────────────────────────
def test_channels_send_display_publishes_say(disp):
    from app.channels import Channels

    async def run():
        ch = Channels()
        try:
            assert await ch.send("display", "", "ohne Display") is False
            sub = hublib.get_hub().subscribe()
            assert await ch.send("display", "", "Hallo Wand") is True
            return sub.queue.get_nowait()
        finally:
            await ch.aclose()
    assert asyncio.run(run()) == ("say", {"text": "Hallo Wand"})


def test_notify_routes_to_display_when_connected(disp, monkeypatch):
    from app import notify

    async def presence(principal):
        return None, True

    async def tg(text, principal, actions):
        return True

    async def push(text, title, actions):
        return True
    monkeypatch.setattr(notify, "_presence", presence)
    monkeypatch.setattr(notify, "_telegram", tg)
    monkeypatch.setattr(notify, "_push", push)

    async def run():
        res_without = await notify.notify("Zug fällt aus")
        sub = hublib.get_hub().subscribe()
        res_with = await notify.notify("Zug fällt aus", title="RMV")
        ctl = await notify.notify("Freigabe?", urgency="control")
        return res_without, res_with, ctl, sub.queue.get_nowait(), sub.queue.empty()
    without, with_, ctl, (event, data), empty = asyncio.run(run())
    assert "display" not in without
    assert with_["display"] is True and event == "say" and data == {"text": "RMV: Zug fällt aus"}
    assert "display" not in ctl and empty


def test_notify_display_respects_setting(disp):
    disp["app_settings"] = {"display": {"notify": False}}

    async def run():
        hublib.get_hub().subscribe()
        return await service.notify_target()
    assert asyncio.run(run()) is False
