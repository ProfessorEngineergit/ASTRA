"""Google-Konten zentral: Scopes, Weiterleitung, Fehlertexte, Token-Lebenszyklus, Konten."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import google_hub as gh
from app.config import get_settings


# ─── Reine Logik ──────────────────────────────────────────────────────────────
def test_account_id_and_scope_merging():
    assert gh.account_id("Bahrian.N@Gmail.com") == "bahrian_n_gmail_com"
    sc = gh.scopes_for(["calendar", "tasks", "bogus"])
    assert sc[:3] == ["openid", "email", "profile"] and gh.PRODUCTS["calendar"].scopes[0] in sc
    widened = gh.scopes_for(["gmail_read"], existing=sc)
    assert set(sc) <= set(widened) and gh.PRODUCTS["gmail_read"].scopes[0] in widened     # nichts geht verloren
    assert gh.products_from_scopes(widened) == ["calendar", "tasks", "gmail_read"]
    assert gh.products_from_scopes(["openid"]) == []


@pytest.mark.parametrize("base,mode,uri", [
    ("http://10.60.0.190:8088/", "manual", "http://localhost:8088/admin/oauth/google/callback"),
    ("http://192.168.178.189:8088/admin/google", "manual", "http://localhost:8088/admin/oauth/google/callback"),
    ("http://astra.local/", "manual", "http://localhost/admin/oauth/google/callback"),
    ("https://astra.lan/", "manual", "http://localhost/admin/oauth/google/callback"),
    ("https://10.0.0.5/", "manual", "http://localhost/admin/oauth/google/callback"),
    ("http://localhost:8088/", "direct", "http://localhost:8088/admin/oauth/google/callback"),
    ("http://127.0.0.1:8088/", "direct", "http://127.0.0.1:8088/admin/oauth/google/callback"),
    ("https://astra.example.com/", "direct", "https://astra.example.com/admin/oauth/google/callback"),
    ("http://astra.example.com/", "manual", "http://localhost/admin/oauth/google/callback"),   # http+Domain ist bei Google verboten
])
def test_pick_redirect_modes(base, mode, uri):
    r = gh.pick_redirect(base)
    assert (r["mode"], r["uri"]) == (mode, uri)


def test_configured_redirect_wins():
    r = gh.pick_redirect("http://10.60.0.190:8088/", "https://x.example.org/admin/oauth/google/callback")
    assert r["mode"] == "configured" and r["uri"].startswith("https://x.example.org")


def test_parse_pasted_accepts_url_query_and_bare_code():
    url = "http://localhost:8088/admin/oauth/google/callback?state=v1.abc&code=4%2F0AXyz&scope=email%20profile"
    assert gh.parse_pasted(url) == {"code": "4/0AXyz", "state": "v1.abc", "error": ""}
    assert gh.parse_pasted("code=4%2F0AXyz&state=v1.abc")["code"] == "4/0AXyz"
    assert gh.parse_pasted("  4/0AXyz  ") == {"code": "4/0AXyz", "state": "", "error": ""}
    assert gh.parse_pasted("http://localhost:8088/x?error=access_denied&state=s")["error"] == "access_denied"
    assert gh.parse_pasted("")["error"]


def test_explain_error_gives_a_next_step_for_the_common_failures():
    disabled = {"error": {"code": 403, "message": "Google Calendar API has not been used in project 123456789012 "
                          "before or it is disabled.", "errors": [{"reason": "accessNotConfigured"}]}}
    e = gh.explain_error(403, disabled, "calendar")
    assert e["kind"] == "api_disabled" and "123456789012" in e["action_url"] and "calendar-json" in e["action_url"]
    scope = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                       "errors": [{"reason": "insufficientPermissions"}]}}
    assert gh.explain_error(403, scope, "tasks")["kind"] == "scope" and "Aufgaben" in gh.explain_error(403, scope, "tasks")["message"]
    grant = gh.explain_error(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    assert grant["kind"] == "reauth" and "7 Tage" in grant["message"]
    assert gh.explain_error(401, {"error": {"status": "UNAUTHENTICATED"}})["kind"] == "reauth"
    assert gh.explain_error(400, {"error": "invalid_client"})["kind"] == "client"
    assert gh.explain_error(400, {"error": "redirect_uri_mismatch"})["kind"] == "redirect"
    assert gh.explain_error(403, {"error": "access_denied"})["kind"] == "denied"
    assert gh.explain_error(429, "x")["kind"] == "quota"
    assert "HTTP 500" in gh.explain_error(500, "boom")["message"]


def test_auth_url_lets_the_user_pick_another_account():
    url = gh.build_auth_url(client_id="cid", redirect_uri="http://localhost:8088/cb", scopes=["openid", "email"],
                            state="S", login_hint="a@b.de")
    assert "prompt=select_account+consent" in url and "access_type=offline" in url and "login_hint=a%40b.de" in url
    assert "login_hint" not in gh.build_auth_url(client_id="c", redirect_uri="r", scopes=[], state="s")


def test_product_for_url():
    assert gh.product_for_url("https://www.googleapis.com/calendar/v3/calendars/primary/events") == "calendar"
    assert gh.product_for_url("https://tasks.googleapis.com/tasks/v1/users/@me/lists") == "tasks"
    assert gh.product_for_url("https://gmail.googleapis.com/gmail/v1/users/me/messages/send") == "gmail_send"
    assert gh.product_for_url("https://gmail.googleapis.com/gmail/v1/users/me/messages") == "gmail_read"


# ─── Zustand & Token-Lebenszyklus (ohne Netz: MockTransport; Fixture `hub` in conftest.py) ─────────
def run(coro):
    return asyncio.run(coro)


async def _connect(scopes=None):
    await gh.set_client("cid", "secret")
    return await gh.upsert_account(email="a@b.de", name="A", refresh_token="RT-1",
                                   scopes=scopes or gh.scopes_for(["calendar", "tasks"]))


def test_accounts_persist_encrypted_and_reload(hub, memdb):
    async def go():
        await _connect()
        await gh.upsert_account(email="schule@x.de", refresh_token="RT-2", scopes=gh.scopes_for(["gmail_read"]))
        gh._reset_for_tests()
        await gh.load(force=True)
        return gh.summary()
    s = run(go())
    assert [a["email"] for a in s["accounts"]] == ["a@b.de", "schule@x.de"]
    assert s["default"] == "a_b_de" and s["accounts"][0]["default"] is True
    assert s["accounts"][0]["products"] == ["calendar", "tasks"] and s["has_secret"]
    stored = run(__import__("app.db", fromlist=["db"]).plugin_config_all(gh.SLUG))
    values = [v["value"] for v in stored.values()]
    assert "secret" not in values and not any("RT-1" in str(v) or "RT-2" in str(v) for v in values)   # verschlüsselt
    assert stored["accounts"]["is_secret"] and stored["client_secret"]["is_secret"]


def test_access_token_is_cached_and_refreshed_once(hub):
    async def go():
        await _connect()
        a = await gh.access_token()
        b = await gh.access_token()
        await asyncio.gather(*[gh.access_token(force=False) for _ in range(5)])
        return a, b
    a, b = run(go())
    assert a == b == "AT-1"
    assert sum(1 for c in hub.calls if c[1] == gh.TOKEN_URL) == 1
    assert hub.calls[0][2]["refresh_token"] == "RT-1" and hub.calls[0][2]["client_id"] == "cid"


def test_resolve_default_explicit_and_missing_accounts(hub):
    async def go():
        await _connect()
        await gh.upsert_account(email="s@x.de", refresh_token="RT-2", scopes=gh.scopes_for(["tasks"]))
        assert gh.resolve("")["email"] == "a@b.de" and gh.resolve("s_x_de")["email"] == "s@x.de"
        assert gh.resolve("gibtsnicht") is None                               # ausdrücklich gewählt, aber weg
        assert await gh.set_default("s_x_de") and gh.resolve(None)["email"] == "s@x.de"
        assert not await gh.set_default("nope")
        assert gh.usable("s_x_de") and not gh.usable("nope")
    run(go())


def test_invalid_grant_marks_the_account_for_reauth_with_a_helpful_message(hub):
    hub.script["token"] = lambda d: (400, {"error": "invalid_grant", "error_description": "expired"})

    async def go():
        await _connect()
        with pytest.raises(gh.GoogleApiError) as e:
            await gh.access_token()
        return e.value, gh.summary()["accounts"][0]
    err, acct = run(go())
    assert err.kind == "reauth" and "7 Tage" in str(err)
    assert acct["status"] == "reauth"


def test_api_retries_once_on_401_and_explains_403(hub):
    seen = {"n": 0}

    def api(req):
        seen["n"] += 1
        return (401, {"error": {"status": "UNAUTHENTICATED"}}) if seen["n"] == 1 else (200, {"items": []})
    hub.script["api"] = api

    async def go():
        await _connect()
        r = await gh.api("", "GET", "https://tasks.googleapis.com/tasks/v1/users/@me/lists")
        assert r.json() == {"items": []}
        hub.script["api"] = lambda req: (403, {"error": {"code": 403, "message": "Google Tasks API has not been used in "
                                                       "project 555555555 before or it is disabled.",
                                                       "errors": [{"reason": "accessNotConfigured"}]}})
        with pytest.raises(gh.GoogleApiError) as e:
            await gh.api("", "GET", "https://tasks.googleapis.com/tasks/v1/users/@me/lists")
        return e.value
    err = run(go())
    assert seen["n"] == 2 and err.kind == "api_disabled" and "tasks.googleapis.com" in err.action_url
    assert sum(1 for c in hub.calls if c[1] == gh.TOKEN_URL) == 2             # 1. Token + Erneuerung nach 401


def test_probe_reports_scope_skip_and_success(hub):
    async def go():
        await _connect(gh.scopes_for(["calendar", "gmail_send"]))
        return (await gh.probe("a_b_de", "calendar"), await gh.probe("a_b_de", "tasks"),
                await gh.probe("a_b_de", "gmail_send"), await gh.probe("nope", "calendar"))
    ok, noscope, skipped, unknown = run(go())
    assert ok["ok"] and noscope["kind"] == "scope" and skipped["kind"] == "skipped" and not unknown["ok"]


def test_complete_login_stores_account_from_google_and_requires_refresh_token(hub):
    hub.script["token"] = lambda d: (200, {"access_token": "AT", "expires_in": 3600, "refresh_token": "RT-NEW",
                                           "scope": "openid email profile https://www.googleapis.com/auth/calendar"})

    async def go():
        await gh.set_client("cid", "secret")
        acct = await gh.complete_login("CODE", "http://localhost:8088/cb")
        hub.script["token"] = lambda d: (200, {"access_token": "AT2", "expires_in": 3600, "scope": "openid"})
        hub.script["userinfo"] = {"email": "neu@x.de"}
        with pytest.raises(gh.GoogleApiError):
            await gh.complete_login("CODE2", "http://localhost:8088/cb")            # kein Refresh-Token
        return acct
    acct = run(go())
    assert acct["email"] == "bahrian@gmail.com" and acct["refresh_token"] == "RT-NEW"
    assert gh.products_from_scopes(acct["scopes"]) == ["calendar"]
    assert hub.calls[0][2]["redirect_uri"].startswith("http")                        # dieselbe URI wie bei der Anmeldung


def test_relogin_widens_scopes_and_keeps_old_refresh_token_if_none_returned(hub):
    async def go():
        await _connect(gh.scopes_for(["calendar"]))
        return await gh.upsert_account(email="a@b.de", scopes=gh.scopes_for(["tasks"]))
    acct = run(go())
    assert acct["refresh_token"] == "RT-1" and gh.products_from_scopes(acct["scopes"]) == ["calendar", "tasks"]


def test_remove_account_revokes_and_moves_default(hub):
    async def go():
        await _connect()
        await gh.upsert_account(email="s@x.de", refresh_token="RT-2", scopes=gh.scopes_for(["tasks"]))
        assert await gh.remove_account("a_b_de")
        return gh.summary()
    s = run(go())
    assert [a["id"] for a in s["accounts"]] == ["s_x_de"] and s["default"] == "s_x_de"
    assert any(c[1] == gh.REVOKE_URL and c[2].get("token") == "RT-1" for c in hub.calls)


# ─── Routing der Plugins ──────────────────────────────────────────────────────
def test_route_prefers_explicit_account_then_existing_plugin_tokens_then_hub_default():
    from app import google_oauth as go
    legacy = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
    assert go.route({"google_account": "a_b_de", **legacy}) == ("hub", "a_b_de")
    assert go.route({"google_account": "legacy", **legacy}) == ("legacy", "")
    assert go.route(legacy) == ("legacy", "")                       # bestehende Verbindungen bleiben unangetastet
    assert go.route({}) == ("hub", "")                              # nichts eingerichtet → Standardkonto der Zentrale
    assert go.route({"google_account": "legacy"}) == ("legacy", "")


def test_plugin_calls_go_through_the_selected_hub_account(hub):
    from app import google_oauth as go
    from app.plugins.builtin.google_calendar import GoogleCalendarPlugin

    async def go_():
        await _connect()
        await gh.upsert_account(email="s@x.de", refresh_token="RT-S", scopes=gh.scopes_for(["calendar"]))
        default = GoogleCalendarPlugin({"__enabled": True, "backend": "native"})
        school = GoogleCalendarPlugin({"__enabled": True, "backend": "native", "google_account": "s_x_de"})
        gone = GoogleCalendarPlugin({"__enabled": True, "backend": "native", "google_account": "weg"})
        assert go.has_google_connection(default.cfg) and go.has_google_connection(school.cfg)
        assert not go.has_google_connection(gone.cfg)
        await go.google_api(default, "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events")
        await go.google_api(school, "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events")
        with pytest.raises(gh.GoogleApiError):
            await go.google_api(gone, "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events")
    run(go_())
    used = [c[2].get("refresh_token") for c in hub.calls if c[1] == gh.TOKEN_URL]
    assert used == ["RT-1", "RT-S"]                                   # zwei Konten, zwei Token-Erneuerungen


def test_legacy_path_now_raises_readable_errors(hub, monkeypatch):
    from app import google_oauth as go
    from app.plugins.builtin.google_tasks import GoogleTasksPlugin
    p = GoogleTasksPlugin({"__enabled": True, "backend": "native", "client_id": "c", "client_secret": "s",
                           "refresh_token": "r", "access_token": "AT", "expires_at": str(time.time() + 3600)})
    hub.script["api"] = lambda req: (403, {"error": {"code": 403, "message": "Tasks API has not been used in project "
                                                    "999999999 before or it is disabled.",
                                                    "errors": [{"reason": "accessNotConfigured"}]}})
    monkeypatch.setattr(go.httpx, "AsyncClient", gh.httpx.AsyncClient)
    with pytest.raises(gh.GoogleApiError) as e:
        run(go.google_api(p, "GET", "https://tasks.googleapis.com/tasks/v1/users/@me/lists"))
    assert e.value.kind == "api_disabled" and "999999999" in e.value.action_url


# ─── Eigene Domain / flexible Weiterleitung ───────────────────────────────────
@pytest.mark.parametrize("text,uri,err", [
    ("", "", ""),
    ("manual", "manual", ""),
    ("MANUAL", "manual", ""),
    ("astra.bahriannovotny.space", "https://astra.bahriannovotny.space/admin/oauth/google/callback", ""),
    ("https://astra.bahriannovotny.space/", "https://astra.bahriannovotny.space/admin/oauth/google/callback", ""),
    ("https://astra.bahriannovotny.space/admin/login", "", "muss auf"),
    ("https://astra.bahriannovotny.space/admin", "https://astra.bahriannovotny.space/admin/oauth/google/callback", ""),
    ("https://Astra.Example.com:8443/admin/oauth/google/callback?x=1",
     "https://astra.example.com:8443/admin/oauth/google/callback", ""),
    ("https://astra.example.com:443", "https://astra.example.com/admin/oauth/google/callback", ""),
    ("http://astra.example.com", "", "http nur für localhost"),
    ("http://10.60.0.190:8088", "", "http nur für localhost"),
    ("https://10.60.0.190", "", "echten Domain"),
    ("https://astra.local", "", "echten Domain"),
    ("http://localhost:8088", "http://localhost:8088/admin/oauth/google/callback", ""),
    ("https://", "", "gültige Adresse"),
])
def test_normalize_redirect(text, uri, err):
    got_uri, got_err = gh.normalize_redirect(text)
    assert got_uri == uri and (err in got_err if err else got_err == "")


def test_pick_redirect_manual_forced_and_invalid_config_falls_back_to_auto():
    lan = "http://10.60.0.190:8088/"
    assert gh.pick_redirect(lan, "manual")["mode"] == "manual"
    assert gh.pick_redirect("https://astra.example.com/", force_manual=True)["uri"] == "http://localhost/admin/oauth/google/callback"
    r = gh.pick_redirect(lan, "astra.bahriannovotny.space")
    assert r["mode"] == "configured" and r["uri"] == "https://astra.bahriannovotny.space/admin/oauth/google/callback"
    assert gh.pick_redirect(lan, "http://10.0.0.1")["mode"] == "manual"          # ungültig → sicherer Automatik-Fall
    assert "Domain" in gh.pick_redirect(lan)["reason"]                            # Hinweis auf die Domain-Option


def test_origin_of():
    assert gh.origin_of("https://Astra.Example.com/admin/x?y=1") == "https://astra.example.com"
    assert gh.origin_of("http://10.60.0.190:8088/") == "http://10.60.0.190:8088"
    assert gh.origin_of("müll") == "" and gh.origin_of("") == ""
