"""Web admin smoke test via TestClient: first-run setup → login session → catalog.

Uses a bare app with just the admin router (no lifespan/DB) + the in-memory db
fixture, and a manager pre-populated without touching Postgres.
"""
from __future__ import annotations

from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.plugins.registry import _discover_classes, get_manager
from app.web import admin as web_admin
from app.web import auth


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(web_admin.router)
    return app


def _prime_manager():
    mgr = get_manager()
    mgr._classes = _discover_classes()
    mgr._instances = {c.slug: c({"__enabled": False}) for c in mgr._classes}


def test_first_run_redirects_to_setup(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/setup"


def test_setup_then_catalog_and_logout(memdb):
    _prime_manager()
    c = TestClient(_app())

    # First-run setup page issues a CSRF cookie.
    r = c.get("/admin/setup")
    assert r.status_code == 200 and "Willkommen" in r.text
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    assert csrf

    # Set the admin password → redirect to /admin with a session cookie.
    r = c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                     "confirm": "geheim123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert auth.COOKIE_NAME in c.cookies

    # Catalog is now reachable and lists plugins.
    r = c.get("/admin")
    assert r.status_code == 200
    assert "RMV" in r.text and "Home Assistant" in r.text
    assert "durchsuchen" in r.text             # search box present
    assert "nur in meinem Gebiet" in r.text    # regional catalog filter present

    # Logout clears the session; /admin redirects back to login.
    c.post("/admin/logout", follow_redirects=False)
    r = c.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_login_required_for_plugin_config(memdb):
    _prime_manager()
    import asyncio
    asyncio.run(auth.set_admin_password("password1"))
    c = TestClient(_app())
    r = c.get("/admin/plugin/rmv", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_google_plugin_page_shows_oauth_connect(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/plugin/google_tasks")

    assert r.status_code == 200
    assert "Google OAuth" in r.text
    assert "Mit Google verbinden" in r.text
    assert "/admin/plugin/google_tasks/oauth/google/start" in r.text


def test_settings_labs_and_region_save(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/settings")
    assert r.status_code == 200
    assert "github-capsule" in r.text
    assert "Theme &amp; Typografie" in r.text
    assert r.text.count('<input type="radio" name="lab_theme"') == 10
    assert "LCARS 2364" in r.text
    assert "Retro Terminal" in r.text
    assert "Model Routing" in r.text
    assert "Hauptmodell" in r.text
    assert "OpenRouter" in r.text
    assert "Anthropic" in r.text
    assert "lab_density" not in r.text
    assert "settings-tabs" not in r.text
    assert "addrresults" in r.text
    assert 'name="country_code"' in r.text
    assert 'href="#settings-updates"' not in r.text
    assert "Git Pull ausführen" not in r.text
    assert "/admin/update/status" not in r.text

    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/settings", data={
        "csrf": csrf,
        "owner_name": "Bahrian",
        "timezone": "Europe/Berlin",
        "units": "metric",
        "language": "de",
        "model_provider_openai_kind": "openai_compat",
        "model_provider_openai_tools": "on",
        "model_provider_openrouter_kind": "openai_compat",
        "model_provider_openrouter_base_url": "https://openrouter.ai/api/v1",
        "model_provider_openrouter_api_key": "secret-openrouter",
        "model_provider_openrouter_tools": "on",
        "model_provider_anthropic_kind": "anthropic",
        "model_provider_ollama_kind": "openai_compat",
        "model_provider_ollama_base_url": "http://ollama:11434/v1",
        "model_provider_ollama_tools": "on",
        "model_role_small_provider": "openai",
        "model_role_small_model": "gpt-4o-mini",
        "model_role_medium_provider": "openai",
        "model_role_medium_model": "gpt-4o",
        "model_role_heavy_provider": "anthropic",
        "model_role_heavy_model": "claude-sonnet-4-5",
        "model_role_code_provider": "openai",
        "model_role_code_model": "gpt-5-codex",
        "model_role_osint_provider": "openrouter",
        "model_role_osint_model": "openai/gpt-5.6-terra",
        "autonomy": "confident",
        "allow_self_config": "on",
        "lab_font": "orbitron",
        "lab_theme": "tng_lcars",
        "address": "Frankfurt am Main",
        "city": "Frankfurt am Main",
        "lat": "50.1109",
        "lon": "8.6821",
        "country_code": "de",
        "country": "Deutschland",
        "state": "Hessen",
        "county": "Frankfurt am Main",
        "postcode": "60311",
    }, follow_redirects=False)
    assert r.status_code == 303

    saved = memdb["app_settings"]
    assert saved["autonomy"] == "confident"
    assert saved["allow_self_config"] is True
    assert saved["font"] == "orbitron"
    assert saved["labs"]["theme"] == "tng_lcars"
    assert set(saved["labs"]) == {"font", "theme"}
    assert saved["ai_model"] == ""
    assert saved["models"]["roles"]["medium"] == {
        "provider": "openai", "model": "gpt-4o"}
    assert saved["models"]["roles"]["osint"]["provider"] == "openrouter"
    assert saved["models"]["providers"]["openrouter"]["api_key"].startswith("fernet:")
    assert "secret-openrouter" not in saved["models"]["providers"]["openrouter"]["api_key"]
    assert saved["location"]["country_code"] == "de"
    assert saved["location"]["state"] == "Hessen"
    assert saved["location"]["county"] == "Frankfurt am Main"


def test_update_page_and_status(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/update")
    assert r.status_code == 200
    assert "Update starten" in r.text
    assert "/admin/update/pull" in r.text

    r = c.get("/admin/updates", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/update"

    r = c.get("/admin/update/status")
    assert r.status_code == 200
    data = r.json()
    assert "current_version" in data or "message" in data
    assert "app_version" in data
    assert "repo_root" in data


def test_system_shows_agent_tools(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/system")

    assert r.status_code == 200
    assert "Agent Tools" in r.text
    assert "recall_memory" in r.text


def test_chat_archive_restore_tabs(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/chat")
    assert r.status_code == 200
    assert "chat-tabs" in r.text
    assert "Archiv <span>0</span>" in r.text
    assert "Berechtigungen umgehen" in r.text

    created = c.post("/admin/chat/new", json={}).json()
    chat_id = created["chat_id"]
    r = c.post("/admin/chat/archive", json={"chat_id": chat_id})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = c.get(f"/admin/chat?view=archive&chat={chat_id}")
    assert r.status_code == 200
    assert "Archivierter Thread" in r.text
    assert "Wiederherstellen" in r.text
    assert f'data-restore-chat="{chat_id}"' in r.text

    r = c.post("/admin/chat/restore", json={"chat_id": chat_id})
    assert r.status_code == 200
    assert r.json()["chat_id"] == chat_id

    r = c.get(f"/admin/chat?chat={chat_id}")
    assert r.status_code == 200
    assert "Archiv <span>0</span>" in r.text


def test_chat_icon_actions_delete_and_merge(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    created = c.post("/admin/chat/new", json={}).json()
    chat_id = created["chat_id"]
    store = memdb[web_admin.CHAT_KEY]
    chat = next(ch for ch in store["chats"] if ch["id"] == chat_id)
    msg = web_admin._msg("user", "Bitte testen")
    chat["messages"].append(msg)
    memdb[web_admin.CHAT_KEY] = store

    r = c.get(f"/admin/chat?chat={chat_id}")
    assert r.status_code == 200
    assert "data-edit" in r.text
    assert "data-branch" in r.text
    assert "data-copy" in r.text
    assert "data-delete" in r.text
    assert ">Bearbeiten<" not in r.text

    branch = c.post("/admin/chat/branch", json={"chat_id": chat_id, "message_id": msg["id"]}).json()
    branch_id = branch["chat_id"]
    store = memdb[web_admin.CHAT_KEY]
    child = next(ch for ch in store["chats"] if ch["id"] == branch_id)
    child["messages"].append(web_admin._msg("assistant", "Neue Branch-Antwort"))
    memdb[web_admin.CHAT_KEY] = store

    r = c.get(f"/admin/chat?chat={branch_id}")
    assert r.status_code == 200
    assert "mergechat" in r.text
    assert "In Ursprung mergen" in r.text

    r = c.post("/admin/chat/merge", json={"chat_id": branch_id})
    assert r.status_code == 200
    assert r.json()["chat_id"] == chat_id
    parent = next(ch for ch in memdb[web_admin.CHAT_KEY]["chats"] if ch["id"] == chat_id)
    assert any(m["content"] == "Neue Branch-Antwort" for m in parent["messages"])

    target = parent["messages"][0]["id"]
    r = c.post("/admin/chat/delete_message", json={"chat_id": chat_id, "message_id": target})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    parent = next(ch for ch in memdb[web_admin.CHAT_KEY]["chats"] if ch["id"] == chat_id)
    assert all(m["id"] != target for m in parent["messages"])


def test_chat_renders_tool_call_cards(memdb):
    _prime_manager()
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    created = c.post("/admin/chat/new", json={}).json()
    chat_id = created["chat_id"]
    store = memdb[web_admin.CHAT_KEY]
    chat = next(ch for ch in store["chats"] if ch["id"] == chat_id)
    chat["messages"].append(web_admin._msg("assistant", "Ich habe EduPage gefragt.", tool_calls=[{
        "tool": "edupage_get_timetable",
        "args": {"day": "tomorrow"},
        "ok": False,
        "summary": "Ich habe EduPage gefragt, aber die API lieferte LoginError.",
        "result": {"ok": False, "error": {"type": "LoginError"}},
    }]))
    memdb[web_admin.CHAT_KEY] = store

    r = c.get(f"/admin/chat?chat={chat_id}")

    assert r.status_code == 200
    assert "tool-card warn" in r.text
    assert "edupage_get_timetable" in r.text


def test_chat_accepts_media_uploads(memdb, monkeypatch):
    _prime_manager()

    async def fake_reply(**kwargs):
        return {"reply": "Anhang gesehen."}

    monkeypatch.setattr("app.agent.generate_reply_meta", fake_reply)
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    created = c.post("/admin/chat/new", json={}).json()
    chat_id = created["chat_id"]
    r = c.post(
        "/admin/chat/send",
        data={"chat_id": chat_id, "message": "Sieh dir das an", "permission_mode": "ask"},
        files={"files": ("bild.png", BytesIO(b"fakepng"), "image/png")},
    )

    assert r.status_code == 200
    chat = next(ch for ch in memdb[web_admin.CHAT_KEY]["chats"] if ch["id"] == chat_id)
    assert chat["messages"][0]["attachments"][0]["name"] == "bild.png"

    r = c.get(f"/admin/chat?chat={chat_id}")
    assert r.status_code == 200
    assert "attachment" in r.text
    assert "bild.png" in r.text


def test_secretary_shows_channel_threads_and_chat_import(memdb, monkeypatch):
    _prime_manager()

    async def fake_list_threads(limit=80):
        return [
            {
                "thread_id": "telegram:123",
                "channel": "telegram",
                "state": "answered",
                "who": "Bahrian",
                "trust_tier": 0,
            },
            {
                "thread_id": "waha:49123@c.us",
                "channel": "waha",
                "state": "deferred",
                "who": "Max",
                "trust_tier": 3,
            },
        ]

    async def fake_recent_messages(thread_id, limit=80):
        if thread_id == "telegram:123":
            return [
                {"role": "owner", "content": "Telegram-Nachricht", "created_at": None},
                {"role": "assistant", "content": "Antwort", "created_at": None},
            ]
        return [{"role": "user", "content": "WhatsApp-Nachricht", "created_at": None}]

    from app import db
    monkeypatch.setattr(db, "list_threads", fake_list_threads)
    monkeypatch.setattr(db, "recent_messages", fake_recent_messages)

    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/inbox", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/secretary"

    r = c.get("/admin/secretary")

    assert r.status_code == 200
    assert "ASTRA Secretary" in r.text
    assert "Channel Threads" in r.text
    assert "WAHA Webhook" in r.text
    assert "sec_waha_base_url" in r.text
    assert "sec_email_0_imap_host" in r.text
    assert "sec_signal_base_url" in r.text
    assert "sec_slack_bot_token" in r.text  # Slack is a Secretary channel now
    assert "WhatsApp verbinden" in r.text
    assert "data-waha-progress" in r.text
    assert "data-waha-start" not in r.text
    assert "data-waha-qr" not in r.text
    assert "WhatsApp-Nachricht" in r.text
    assert "Telegram-Nachricht" not in r.text
    assert "sec_telegram_mode" not in r.text
    assert "sec_telegram_enabled" not in r.text
    assert "data-setup-channel" not in r.text

    chat_id = web_admin._channel_chat_id("telegram:123")
    r = c.get(f"/admin/chat?chat={chat_id}")

    assert r.status_code == 200
    assert "from Telegram" in r.text
    assert "Telegram-Nachricht" in r.text

    r = c.post("/admin/secretary/setup-chat", json={
        "channel": "waha",
        "message": "Wie teste ich den Header?",
        "context": {"waha_base_url": "http://waha:3000", "waha_api_key": "secret"},
    })
    assert r.status_code == 200
    assert "X-Astra-Secret" in r.json()["reply"]
    assert "secretary_setup_chats" in memdb


def test_secretary_hides_thread_shell_when_no_secretary_threads(memdb, monkeypatch):
    _prime_manager()

    async def fake_list_threads(limit=80):
        return [{
            "thread_id": "telegram:123",
            "channel": "telegram",
            "state": "answered",
            "who": "Bahrian",
            "trust_tier": 0,
        }]

    from app import db
    monkeypatch.setattr(db, "list_threads", fake_list_threads)

    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/secretary")

    assert r.status_code == 200
    assert "Channel Threads" not in r.text
    assert "Telegram" not in r.text


def test_secretary_waha_qr_endpoint_returns_image(memdb, monkeypatch):
    _prime_manager()

    async def fake_waha_request(base_url, path, *, api_key="", method="GET"):
        assert base_url == "http://waha:3000"
        assert api_key == "secret"
        if path == "/api/sessions/default":
            return {
                "ok": True,
                "status": 200,
                "content_type": "application/json",
                "content": b"",
                "text": '{"status":"SCAN_QR_CODE"}',
                "url": f"{base_url}{path}",
            }
        return {
            "ok": True,
            "status": 200,
            "content_type": "image/png",
            "content": b"pngdata",
            "text": "",
            "url": f"{base_url}{path}",
        }

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    r = c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/qr", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.status_code == 200
    assert r.json()["image"].startswith("data:image/png;base64,")


def test_secretary_waha_start_is_success_when_session_is_already_starting(memdb, monkeypatch):
    _prime_manager()
    calls = []

    async def fake_waha_request(base_url, path, *, api_key="", method="GET", json_body=None):
        calls.append((path, method))
        return {"ok": True, "status": 200, "text": '{"status":"STARTING"}'}

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/start", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.json()["ok"] is True
    assert calls == [("/api/sessions/default", "GET")]


def test_secretary_waha_start_accepts_legacy_already_started_response(memdb, monkeypatch):
    _prime_manager()

    async def fake_waha_request(base_url, path, *, api_key="", method="GET", json_body=None):
        if method == "GET":
            return {"ok": True, "status": 200, "text": '{"status":"STOPPED"}'}
        return {
            "ok": False,
            "status": 422,
            "text": '{"message":"Session default is already started","error":"Unprocessable Entity"}',
        }

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/start", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.json()["ok"] is True


def test_secretary_waha_qr_marks_starting_session_retryable(memdb, monkeypatch):
    _prime_manager()

    async def fake_waha_request(base_url, path, *, api_key="", method="GET", json_body=None):
        if path == "/api/sessions/default":
            return {"ok": True, "status": 200, "text": '{"status":"STARTING"}'}
        return {"ok": False, "status": 422, "text": '{"message":"QR is not ready"}'}

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/qr", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.json()["retryable"] is True
    assert r.json()["state"] == "STARTING"


def test_secretary_waha_recouple_logs_out_existing_session_before_restart(memdb, monkeypatch):
    _prime_manager()
    calls = []

    async def fake_waha_request(base_url, path, *, api_key="", method="GET", json_body=None):
        calls.append((path, method, json_body))
        return {"ok": True, "status": 200, "text": "{}"}

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/recouple", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == [
        ("/api/sessions/default", "GET", None),
        ("/api/sessions/default/logout", "POST", None),
        ("/api/sessions/default/start", "POST", None),
    ]


def test_secretary_waha_start_creates_missing_session(memdb, monkeypatch):
    _prime_manager()
    calls = []

    async def fake_waha_request(base_url, path, *, api_key="", method="GET", json_body=None):
        calls.append((path, method, json_body))
        if path == "/api/sessions/default":
            return {"ok": False, "status": 404, "text": "not found"}
        return {"ok": True, "status": 201, "text": "{}"}

    monkeypatch.setattr(web_admin, "_waha_request", fake_waha_request)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.post("/admin/secretary/waha/start", json={
        "waha_base_url": "http://waha:3000",
        "waha_session": "default",
        "waha_api_key": "secret",
    })

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == [
        ("/api/sessions/default", "GET", None),
        ("/api/sessions", "POST", {"name": "default", "start": True}),
    ]


def test_secretary_saves_multiple_email_accounts(memdb, monkeypatch):
    _prime_manager()

    async def _no_threads(*a, **k):
        return []

    from app import db
    monkeypatch.setattr(db, "list_threads", _no_threads)
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/secretary", data={
        "csrf": csrf,
        "sec_enabled": "on",
        "sec_email_enabled": "on",
        "sec_email_count": "2",
        "sec_email_0_from": "privat@example.com",
        "sec_email_0_imap_host": "imap.example.com",
        "sec_email_0_password": "pw1",
        "sec_email_0_enabled": "on",
        "sec_email_1_from": "schule@example.org",
        "sec_email_1_imap_host": "imap.example.org",
        "sec_email_1_password": "pw2",
        "sec_email_1_enabled": "on",
    }, follow_redirects=False)
    assert r.status_code == 303

    r = c.get("/admin/secretary")
    assert "imap.example.com" in r.text
    assert "imap.example.org" in r.text
    # second saved account + one fresh blank block => index 2 fields rendered
    assert "sec_email_2_imap_host" in r.text


def test_osint_tab_renders_and_run_endpoint_gates_tool(memdb):
    _prime_manager()
    c = TestClient(_app())
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)

    r = c.get("/admin/osint")
    assert r.status_code == 200
    assert "connect-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "fonts.googleapis.com" not in r.text
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    location = c.post("/admin/osint/location", data={
        "csrf": csrf, "lat": "50.123456", "lon": "8.654321",
    })
    assert location.status_code == 200
    assert location.json()["ok"] is True
    assert memdb["app_settings"]["location"]["source"] == "browser"
    assert memdb["app_settings"]["location"]["lat"] == 50.123456
    assert "Recon" in r.text and "Kameras in der Nähe" in r.text
    assert "Drucker in der Nähe" in r.text
    assert "OSINT-Recherche" in r.text
    assert "TOR KILL-SWITCH" in r.text
    assert "externe Browser-Links" in r.text
    assert "row.shodan_url" not in r.text
    assert "Netz-Audit" not in r.text
    assert "Breach-Check" not in r.text
    # Plugin ist im Test aus → Hinweis-Banner statt einer scheinbar aktiven Suche.
    assert "noch nicht aktiv" in r.text

    # Der Run-Endpoint lässt nur registrierte osint_-Tools zu. Ein Nicht-OSINT-Tool
    # wird abgelehnt; ein osint_-Tool bei ausgeschaltetem Plugin ebenfalls (nicht
    # registriert) — genau das ist die Absicht, kein Tool leckt durch.
    assert c.post("/admin/osint/run", data={"tool": "home_assistant_call"}).status_code == 400
    assert c.post("/admin/osint/run", data={"tool": "ops_exec"}).status_code == 400
    assert c.post("/admin/osint/run", data={"tool": "osint_exit_ip"}).status_code == 400
