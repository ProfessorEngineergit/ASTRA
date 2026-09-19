"""Admin → Google: Client, Anmeldung (direkt + eingefügte Adresse), Konten, Zuordnung zu Plugins, Übernahme."""
from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, quote, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import google_hub as gh
from app import google_oauth
from app.config_store import get_config_store
from app.plugins.builtin.google_calendar import GoogleCalendarPlugin
from app.plugins.registry import _discover_classes, get_manager
from app.web import admin as web_admin
from app.web import admin_extra, admin_google, auth


def _client(base="http://10.60.0.190:8088") -> TestClient:
    app = FastAPI()
    for r in (web_admin.router, admin_extra.router, admin_google.router):
        app.include_router(r)
    c = TestClient(app, base_url=base)
    c.get("/admin/setup")
    c.post("/admin/setup", data={"csrf": c.cookies.get(auth.CSRF_COOKIE), "password": "geheim123",
                                 "confirm": "geheim123"}, follow_redirects=False)
    return c


def _login(base: str) -> TestClient:
    """Zweiter Zugang (andere Adresse) zur bereits eingerichteten Instanz."""
    app = FastAPI()
    for r in (web_admin.router, admin_extra.router, admin_google.router):
        app.include_router(r)
    c = TestClient(app, base_url=base)
    c.get("/admin/login")
    c.post("/admin/login", data={"csrf": c.cookies.get(auth.CSRF_COOKIE), "password": "geheim123"},
           follow_redirects=False)
    return c


def _csrf(c, path="/admin/google"):
    c.get(path)
    return c.cookies.get(auth.CSRF_COOKIE)


@pytest.fixture
def env(hub, memdb, monkeypatch):
    monkeypatch.delenv("ASTRA_DOMAIN", raising=False)
    mgr = get_manager()
    mgr._classes = _discover_classes()
    mgr._instances = {cls.slug: cls({"__enabled": False}) for cls in mgr._classes}
    hub.script["token"] = lambda d: (200, {"access_token": "AT", "expires_in": 3600, "refresh_token": "RT-NEW",
                                           "scope": "openid email profile https://www.googleapis.com/auth/calendar "
                                                    "https://www.googleapis.com/auth/tasks"})
    return hub


def _save_client(c):
    csrf = _csrf(c)
    c.post("/admin/google/client", data={"csrf": csrf, "client_id": "cid.apps.googleusercontent.com",
                                         "client_secret": "sek"}, follow_redirects=False)


def _start(c, products=("calendar", "tasks"), hint=""):
    csrf = _csrf(c)
    r = c.post("/admin/google/connect", data={"csrf": csrf, "products": list(products), "login_hint": hint},
               follow_redirects=False)
    assert r.status_code == 303
    u = urlparse(r.headers["location"])
    return r, parse_qs(u.query)


def test_page_explains_the_lan_ip_problem_and_shows_the_uri_to_register(env):
    c = _client("http://10.60.0.190:8088")
    page = c.get("/admin/google").text
    assert "http://localhost:8088/admin/oauth/google/callback" in page          # nicht die LAN-IP
    assert "keine LAN-Adresse" in page and "Anmeldung abschließen (nötig bei dir)" in page
    assert "Google" in page and "Erst Client-ID" in page                        # Anmelden gesperrt ohne Client
    direct = _client("http://localhost:8088").get("/admin/google").text
    assert "Anmeldung abschließen (nötig" not in direct


def test_client_is_saved_encrypted_and_secret_never_echoed(env):
    c = _client()
    _save_client(c)
    page = c.get("/admin/google").text
    assert "cid.apps.googleusercontent.com" in page and "sek" not in page.replace("gesetzt", "")
    assert gh.summary()["has_secret"] and gh.has_client()
    csrf = _csrf(c)
    c.post("/admin/google/client", data={"csrf": csrf, "client_id": "other-id", "client_secret": ""},
           follow_redirects=False)
    assert gh.summary()["client_id"] == "other-id" and gh._STATE["client_secret"] == "sek"   # leer = behalten
    assert c.post("/admin/google/client", data={"csrf": csrf, "client_id": ""}, follow_redirects=False
                  ).headers["location"].startswith("/admin/google?err=")


def test_connect_builds_google_url_with_hub_redirect_and_union_scopes(env):
    c = _client()
    no_client = c.post("/admin/google/connect", data={"csrf": _csrf(c)}, follow_redirects=False)
    assert no_client.headers["location"].startswith("/admin/google?err=")           # ohne Client kein Google-Aufruf
    _save_client(c)
    r, q = _start(c, ("calendar", "gmail_read"))
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["redirect_uri"] == ["http://localhost:8088/admin/oauth/google/callback"]
    scopes = q["scope"][0].split()
    assert "https://www.googleapis.com/auth/calendar" in scopes and "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "tasks" not in q["scope"][0] and q["prompt"] == ["select_account consent"]
    payload = asyncio.run(auth.read_oauth_state(q["state"][0]))
    assert payload["provider"] == "google_hub" and payload["redirect_uri"] == q["redirect_uri"][0]


def test_paste_flow_completes_login_and_shows_the_account(env):
    c = _client()
    _save_client(c)
    _, q = _start(c)
    pasted = (f"http://localhost:8088/admin/oauth/google/callback?state={quote(q['state'][0])}"
              f"&code=4%2F0ABC&scope=email")
    csrf = _csrf(c)
    r = c.post("/admin/google/paste", data={"csrf": csrf, "pasted": pasted}, follow_redirects=False)
    assert r.headers["location"] == "/admin/google?saved=connected"
    exch = next(x for x in env.calls if x[2].get("grant_type") == "authorization_code")
    assert exch[2]["code"] == "4%2F0ABC" and exch[2]["redirect_uri"].startswith("http%3A%2F%2Flocalhost%3A8088")
    page = c.get("/admin/google?saved=connected").text
    assert "bahrian@gmail.com" in page and "Konto verbunden." in page and "✔ Kalender" in page and "✔ Aufgaben" in page
    assert gh.usable("bahrian_gmail_com")


def test_paste_flow_rejects_garbage_missing_state_foreign_state_and_google_errors(env):
    c = _client()
    _save_client(c)
    csrf = _csrf(c)

    def post(text):
        return c.post("/admin/google/paste", data={"csrf": csrf, "pasted": text}, follow_redirects=False).headers["location"]
    assert "err=" in post("")
    assert "state" in post("4/0ABCnurdercode")
    assert "err=" in post("http://localhost:8088/x?code=1&state=v1.gefaelscht")
    _, q = _start(c)
    csrf = _csrf(c)
    denied = post(f"http://localhost:8088/x?error=access_denied&state={quote(q['state'][0])}")
    assert "Testnutzer" in __import__("urllib.parse", fromlist=["unquote"]).unquote(denied)
    assert not gh.has_accounts()


def test_direct_callback_completes_login_too(env):
    c = _client("http://localhost:8088")
    _save_client(c)
    _, q = _start(c)
    r = c.get("/admin/oauth/google/callback", params={"state": q["state"][0], "code": "4/0X"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/google?saved=connected" and gh.has_accounts()
    r = c.get("/admin/oauth/google/callback", params={"state": q["state"][0], "error": "access_denied"},
              follow_redirects=False)
    assert r.headers["location"].startswith("/admin/google?err=")


def test_expired_or_legacy_states_do_not_touch_the_hub(env):
    c = _client()
    r = c.get("/admin/oauth/google/callback", params={"state": "zufall", "code": "x"}, follow_redirects=False)
    assert r.status_code == 400 and not gh.has_accounts()


def test_switch_default_disconnect_and_test(env):
    c = _client()
    _save_client(c)
    for email, rt in (("a@b.de", "RT-A"), ("schule@x.de", "RT-S")):
        env.script["userinfo"] = {"email": email, "name": email}
        env.script["token"] = lambda d, rt=rt: (200, {"access_token": "AT", "expires_in": 3600, "refresh_token": rt,
                                                      "scope": "openid email https://www.googleapis.com/auth/calendar"})
        _, q = _start(c)
        c.post("/admin/google/paste", data={"csrf": _csrf(c), "pasted": f"http://localhost/x?code=c&state={quote(q['state'][0])}"},
               follow_redirects=False)
    assert [a["id"] for a in gh.summary()["accounts"]] == ["a_b_de", "schule_x_de"] and gh.summary()["default"] == "a_b_de"
    csrf = _csrf(c)
    c.post("/admin/google/default", data={"csrf": csrf, "account": "schule_x_de"}, follow_redirects=False)
    assert gh.summary()["default"] == "schule_x_de"
    ok = c.post("/admin/google/test", data={"csrf": csrf, "account": "a_b_de", "product": "calendar"}).json()
    assert ok["ok"] and "funktioniert" in ok["message"]
    scope = c.post("/admin/google/test", data={"csrf": csrf, "account": "a_b_de", "product": "tasks"}).json()
    assert not scope["ok"] and scope["kind"] == "scope"
    env.script["api"] = lambda req: (403, {"error": {"code": 403, "message": "API has not been used in project 424242424 "
                                                     "before or it is disabled.", "errors": [{"reason": "accessNotConfigured"}]}})
    off = c.post("/admin/google/test", data={"csrf": csrf, "account": "a_b_de", "product": "calendar"}).json()
    assert off["kind"] == "api_disabled" and "424242424" in off["action_url"]
    c.post("/admin/google/disconnect", data={"csrf": csrf, "account": "schule_x_de"}, follow_redirects=False)
    assert gh.summary()["default"] == "a_b_de" and len(gh.summary()["accounts"]) == 1
    assert any(x[1] == gh.REVOKE_URL for x in env.calls)


def test_assign_account_to_plugin_installation_switches_the_account_used(env):
    c = _client()
    _save_client(c)
    asyncio.run(gh.upsert_account(email="a@b.de", refresh_token="RT-A", scopes=gh.scopes_for(["calendar"])))
    asyncio.run(gh.upsert_account(email="s@x.de", refresh_token="RT-S", scopes=gh.scopes_for(["calendar"])))
    csrf = _csrf(c)
    r = c.post("/admin/google/assign", data={"csrf": csrf, "slug": "google_calendar", "install_id": "default",
                                             "account": "s_x_de"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/google?saved=assigned"
    cfg = asyncio.run(get_config_store().load(GoogleCalendarPlugin))
    assert cfg["google_account"] == "s_x_de"
    assert google_oauth.route(cfg) == ("hub", "s_x_de")
    assert "nutzt s@x.de" in c.get("/admin/google").text
    csrf = c.cookies.get(auth.CSRF_COOKIE)                                            # jede Seite stellt ein neues Token aus
    bad = c.post("/admin/google/assign", data={"csrf": csrf, "slug": "google_calendar", "install_id": "default",
                                               "account": "hacker"}, follow_redirects=False)
    assert "err=" in bad.headers["location"]
    assert "err=" in c.post("/admin/google/assign", data={"csrf": csrf, "slug": "spotify", "install_id": "default",
                                                          "account": ""}, follow_redirects=False).headers["location"]


def test_plugin_page_offers_the_account_select_and_link_to_hub(env):
    c = _client()
    asyncio.run(gh.upsert_account(email="a@b.de", refresh_token="RT-A", scopes=gh.scopes_for(["calendar"])))
    page = c.get("/admin/plugin/google_calendar").text
    assert 'name="google_account"' in page and "a@b.de" in page and "Standardkonto" in page
    assert "/admin/google" in page and "Zentrales Konto: a@b.de" in page


def test_legacy_plugin_tokens_keep_working_and_can_be_imported(env):
    c = _client()
    store = get_config_store()
    mgr = get_manager()
    asyncio.run(store.save_installation(GoogleCalendarPlugin, "default", {
        "backend": "native", "client_id": "old-cid", "client_secret": "old-sec", "refresh_token": "RT-OLD",
        "access_token": "", "expires_at": "", "account_email": "Alt@Gmail.com", "calendar_id": "primary",
        "google_account": ""}, True, name="Standard"))
    asyncio.run(mgr.rebuild())
    cfg = asyncio.run(store.load(GoogleCalendarPlugin))
    assert google_oauth.route(cfg) == ("legacy", "") and google_oauth.has_google_connection(cfg)   # nichts bricht
    csrf = _csrf(c)
    r = c.post("/admin/google/import", data={"csrf": csrf, "target": "google_calendar|default"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/google?saved=imported"
    assert gh.has_client() and gh.resolve("alt_gmail_com")["refresh_token"] == "RT-OLD"
    cfg = asyncio.run(store.load(GoogleCalendarPlugin))
    assert cfg["google_account"] == "alt_gmail_com" and google_oauth.route(cfg) == ("hub", "alt_gmail_com")
    nothing = c.post("/admin/google/import", data={"csrf": csrf, "target": "google_tasks|default"}, follow_redirects=False)
    assert "err=" in nothing.headers["location"]


def test_all_mutations_require_csrf_and_login(env):
    c = _client()
    for path in ("client", "connect", "paste", "default", "disconnect", "assign", "import"):
        assert c.post(f"/admin/google/{path}", data={"csrf": "x"}, follow_redirects=False).status_code == 403, path
    assert c.post("/admin/google/test", data={"csrf": "x"}).status_code == 403
    # ohne Sitzung: Weiterleitung zum Login
    app = FastAPI()
    app.include_router(admin_google.router)
    assert TestClient(app).get("/admin/google", follow_redirects=False).status_code == 303


# ─── Eigene Domain / flexible Weiterleitung ───────────────────────────────────
DOMAIN = "https://astra.bahriannovotny.space"
DOMAIN_CB = DOMAIN + "/admin/oauth/google/callback"
LOCAL_CB = "http://localhost:8088/admin/oauth/google/callback"


def _save(c, mode="auto", domain="", **extra):
    csrf = _csrf(c)
    return c.post("/admin/google/client", data={"csrf": csrf, "client_id": "cid", "client_secret": "sek",
                                                "redirect_mode": mode, "redirect_domain": domain, **extra},
                  follow_redirects=False)


def test_domain_mode_can_be_chosen_while_connected_locally_and_shows_both_uris(env):
    c = _client("http://10.60.0.190:8088")
    page = c.get("/admin/google").text
    assert 'name="redirect_mode"' in page and "Eigene Domain" in page and 'name="redirect_domain"' in page
    r = _save(c, "domain", "astra.bahriannovotny.space")                       # nackte Domain reicht
    assert r.headers["location"] == "/admin/google?saved=client"
    assert gh.summary()["redirect_uri"] == DOMAIN_CB
    page = c.get("/admin/google").text
    assert DOMAIN_CB in page and LOCAL_CB in page                              # beide zum Eintragen bei Google
    assert "über eine andere Adresse verbunden" in page                        # Hinweis: lokal gestartet, Domain als Rückweg
    assert '<option value="domain" selected>' in page


def test_invalid_domain_is_rejected_before_saving_anything(env):
    c = _client()
    for bad, needle in (("http://10.60.0.190:8088", "http"), ("https://astra.local", "Domain"),
                        ("https://astra.example.com/admin/login", "enden"), ("", "Domain")):
        r = _save(c, "domain", bad)
        assert r.headers["location"].startswith("/admin/google?err="), bad
    assert not gh.has_client() and gh.summary()["redirect_uri"] == ""


def test_manual_and_auto_modes_roundtrip(env):
    c = _client("http://10.60.0.190:8088")
    _save(c, "manual")
    assert gh.summary()["redirect_uri"] == "manual"
    assert '<option value="manual" selected>' in c.get("/admin/google").text
    _save(c, "auto")
    assert gh.summary()["redirect_uri"] == ""


def test_connect_uses_the_domain_and_remembers_where_you_started(env):
    c = _client("http://10.60.0.190:8088")
    _save(c, "domain", DOMAIN)
    _, q = _start(c)
    assert q["redirect_uri"] == [DOMAIN_CB]
    payload = asyncio.run(auth.read_oauth_state(q["state"][0]))
    assert payload["redirect_uri"] == DOMAIN_CB and payload["return_to"] == "http://10.60.0.190:8088/"


def test_manual_button_forces_localhost_even_when_a_domain_is_configured(env):
    c = _client("http://10.60.0.190:8088")
    _save(c, "domain", DOMAIN)
    csrf = _csrf(c)
    r = c.post("/admin/google/connect", data={"csrf": csrf, "products": ["calendar"], "redirect": "manual"},
               follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == [LOCAL_CB]
    assert asyncio.run(auth.read_oauth_state(q["state"][0]))["mode"] == "manual"


def test_callback_on_the_domain_finishes_the_login_and_sends_you_back_to_where_you_started(env):
    local = _client("http://10.60.0.190:8088")
    _save(local, "domain", DOMAIN)
    _, q = _start(local)
    # Google leitet auf die DOMAIN zurück (anderer Ursprung, dort ist niemand angemeldet)
    dom = TestClient(_app_for_callback(), base_url=DOMAIN)
    r = dom.get("/admin/oauth/google/callback", params={"state": q["state"][0], "code": "4/0X"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "http://10.60.0.190:8088/admin/google?saved=connected"
    assert gh.has_accounts()
    # Fehler von Google gehen ebenfalls zurück zum Ausgangspunkt
    r = dom.get("/admin/oauth/google/callback", params={"state": q["state"][0], "error": "access_denied"},
                follow_redirects=False)
    assert r.headers["location"].startswith("http://10.60.0.190:8088/admin/google?err=")


def _app_for_callback():
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(admin_google.router)
    return app


def test_same_origin_callback_stays_relative_and_forged_return_to_is_impossible(env):
    c = _client(DOMAIN)
    _save(c, "domain", DOMAIN)
    _, q = _start(c)
    r = c.get("/admin/oauth/google/callback", params={"state": q["state"][0], "code": "4/0X"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/google?saved=connected"
    # ein selbst gebauter State (ohne Signatur) wird nicht akzeptiert → kein Rücksprung an fremde Adressen
    r = c.get("/admin/oauth/google/callback", params={"state": "v1.evil", "code": "x"}, follow_redirects=False)
    assert r.status_code == 400


def test_forwarded_headers_make_a_proxied_domain_count_as_https(env):
    c = _client("http://astra.bahriannovotny.space")          # Proxy terminiert TLS: intern kommt http an
    plain = c.get("/admin/google").text
    assert 'id="g-redirect-0" readonly value="http://localhost/admin/oauth/google/callback"' in plain   # ohne Header: manuell
    page = c.get("/admin/google", headers={"X-Forwarded-Proto": "https"}).text
    assert f'id="g-redirect-0" readonly value="{DOMAIN_CB}"' in page       # erkannt: https + echte Domain → direkter Weg
    csrf = _csrf(c)
    r = c.post("/admin/google/connect", data={"csrf": csrf}, headers={"X-Forwarded-Proto": "https"},
               follow_redirects=False)
    assert "/admin/google?err=" in r.headers["location"]       # ohne Client noch kein Google-Aufruf


# ─── Automatik mit bekannter Domain, sichtbare Sende-Adresse, freundliche Callback-Seiten ─────
def test_auto_mode_uses_the_domain_from_env_even_when_connected_by_lan_ip(env, monkeypatch):
    monkeypatch.setenv("ASTRA_DOMAIN", "astra.bahriannovotny.space")
    c = _client("http://10.60.0.190:8088")
    _save(c, "auto")
    page = c.get("/admin/google").text
    assert f'id="g-redirect-0" readonly value="{DOMAIN_CB}"' in page and "Diese Adresse sendet ASTRA beim Anmelden" in page
    assert f"sendet ASTRA diese Rücksprung-Adresse: <b>{DOMAIN_CB}</b>" in page
    assert "Aktuell würde localhost gesendet" not in page
    _, q = _start(c)
    assert q["redirect_uri"] == [DOMAIN_CB]                                           # genau das, was angezeigt wurde
    assert "g-use-known" in page                                                       # Vorschlag „Diese Domain verwenden“


def test_visiting_via_the_domain_teaches_astra_the_domain_for_later_local_use(env, monkeypatch):
    monkeypatch.delenv("ASTRA_DOMAIN", raising=False)
    via_domain = _client(DOMAIN)
    via_domain.get("/admin/google")
    assert gh.known_domain() == DOMAIN
    local = _login("http://10.60.0.190:8088")
    _save(local, "auto")
    assert f'id="g-redirect-0" readonly value="{DOMAIN_CB}"' in local.get("/admin/google").text


def test_without_any_known_domain_the_page_steers_to_domain_input_and_warns_about_localhost(env, monkeypatch):
    monkeypatch.delenv("ASTRA_DOMAIN", raising=False)
    c = _client("http://10.60.0.190:8088")
    page = c.get("/admin/google").text
    assert '<option value="domain" selected>' in page and "Aktuell würde localhost gesendet" in page
    assert "Eigene Domain</b> eintragen" in page and "deinen Rechner" in page


def test_bare_callback_url_says_it_is_reachable_and_bad_state_explains_itself(env):
    c = TestClient(_app_for_callback(), base_url=DOMAIN)
    bare = c.get("/admin/oauth/google/callback")
    assert bare.status_code == 200 and "erreichbar" in bare.text and "/admin/google" in bare.text
    bad = c.get("/admin/oauth/google/callback", params={"state": "v1.alt", "code": "x"})
    assert bad.status_code == 400 and "20 Minuten" in bad.text and "von Hand" in bad.text


def test_known_domain_beats_localhost_even_when_you_are_at_localhost(env, monkeypatch):
    monkeypatch.setenv("ASTRA_DOMAIN", "astra.bahriannovotny.space")
    c = _client("http://localhost:8088")
    _save(c, "auto")
    _, q = _start(c)
    assert q["redirect_uri"] == [DOMAIN_CB]
    assert asyncio.run(auth.read_oauth_state(q["state"][0]))["return_to"] == "http://localhost:8088/"
