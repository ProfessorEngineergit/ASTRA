"""Web admin smoke test via TestClient: first-run setup → login session → catalog.

Uses a bare app with just the admin router (no lifespan/DB) + the in-memory db
fixture, and a manager pre-populated without touching Postgres.
"""
from __future__ import annotations

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
    assert "Experimental Console" in r.text
    assert "lab_density" in r.text
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
        "ai_model": "gpt-4o",
        "autonomy": "confident",
        "allow_self_config": "on",
        "lab_font": "orbitron",
        "lab_density": "dense",
        "lab_motion": "hyperspace",
        "lab_event_horizon": "deep",
        "lab_surface_glow": "cinematic",
        "lab_accent": "ion",
        "lab_catalog_view": "compact",
        "lab_map_style": "transit",
        "lab_diagnostics": "on",
        "lab_save_effect": "on",
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
    assert saved["labs"]["density"] == "dense"
    assert saved["labs"]["catalog_view"] == "compact"
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
