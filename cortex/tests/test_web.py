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
