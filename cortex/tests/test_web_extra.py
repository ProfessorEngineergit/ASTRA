"""Smoke-Tests der Zusatzseiten (Verbrauch, Kontakte, Prompts, Sicherheit) über TestClient."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, usage
from app.web import admin as web_admin
from app.web import admin_extra, auth


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(admin_extra.router)
    c = TestClient(app)
    c.get("/admin/setup")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    c.post("/admin/setup", data={"csrf": csrf, "password": "geheim123", "confirm": "geheim123"},
           follow_redirects=False)
    return c


def _csrf(c: TestClient, path: str) -> str:
    c.get(path)
    return c.cookies.get(auth.CSRF_COOKIE)


def _row(**kw):
    base = dict(ts=datetime.now(timezone.utc), principal_key="", provider="openai", model="gpt-4o-mini",
                role="small", purpose="chat", channel="web", thread_id="t", chat_id="c1", contact="",
                prompt_tokens=1000, completion_tokens=500, cost_usd=0.0006, latency_ms=100,
                ok=True, estimated=False)
    base.update(kw)
    return base


@pytest.fixture
def usagedb(memdb, monkeypatch):
    rows = [_row(), _row(model="mystery-1", cost_usd=None, purpose="triage", channel="waha")]

    async def usage_rows(since, until=None, **k):
        return rows

    async def month_cost(since, third_party_only=False):
        return 4.0

    monkeypatch.setattr(db, "usage_rows", usage_rows, raising=False)
    monkeypatch.setattr(db, "usage_month_cost", month_cost, raising=False)
    usage.set_config({})
    yield rows
    usage.set_config({})


def test_usage_page_renders_totals_groups_and_missing_price(usagedb):
    c = _client()
    r = c.get("/admin/usage?period=month&by=purpose")
    assert r.status_code == 200
    assert "Token" in r.text and "Chat mit dir" in r.text and "Triage (Eingang)" in r.text
    assert "mystery-1" in r.text                      # Preis-Hinweis nennt das Modell
    assert "Preis fehlt" in r.text
    assert "Verbrauch" in r.text                      # Navigation


def test_usage_page_requires_login(memdb):
    app = FastAPI()
    app.include_router(web_admin.router)
    app.include_router(admin_extra.router)
    c = TestClient(app)
    r = c.get("/admin/usage", follow_redirects=False)
    assert r.status_code == 303


def test_usage_page_unknown_params_fall_back(usagedb):
    c = _client()
    assert c.get("/admin/usage?period=evil&by=drop%20table").status_code == 200


def test_usage_json(usagedb):
    c = _client()
    j = c.get("/admin/usage/data?by=model").json()
    assert j["totals"]["calls"] == 2 and {g["key"] for g in j["groups"]} == {"gpt-4o-mini", "mystery-1"}


def test_budget_save_persists_and_applies(usagedb, memdb):
    c = _client()
    csrf = _csrf(c, "/admin/usage")
    r = c.post("/admin/usage/budget", data={"csrf": csrf, "monthly_usd": "5", "warn_pct": "50",
                                            "hard_stop": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert usage.budget_state(4.0)["level"] == "warn" and usage.budget_state(4.0)["hard"] is True
    assert "80" not in str(usage._BUDGET.get("warn_pct"))


def test_budget_save_rejects_missing_csrf(usagedb):
    c = _client()
    r = c.post("/admin/usage/budget", data={"csrf": "x", "monthly_usd": "5"}, follow_redirects=False)
    assert r.status_code == 403


def test_price_save_then_cost_estimated_then_delete(usagedb):
    c = _client()
    csrf = _csrf(c, "/admin/usage")
    c.post("/admin/usage/prices", data={"csrf": csrf, "model": "Mystery-1", "p_in": "1,5", "p_out": "3"},
           follow_redirects=False)
    assert usage.estimate_cost("mystery-1", 1_000_000, 1_000_000) == 4.5
    c.post("/admin/usage/prices", data={"csrf": csrf, "del": "mystery-1"}, follow_redirects=False)
    assert usage.estimate_cost("mystery-1", 10, 10) is None
    r = c.post("/admin/usage/prices", data={"csrf": csrf, "model": "x", "p_in": "abc", "p_out": "1"},
               follow_redirects=False)
    assert "priceerr" in r.headers["location"]


# ─── Kontakte & Gruppen ───────────────────────────────────────────────────────
@pytest.fixture
def cardsdb(memdb, monkeypatch, tmp_path):
    from app import cards, digest
    from app.config import get_settings
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    store: dict = {}

    async def card_list(principal_key=""):
        return [dict(v) for v in store.values()]

    async def card_save(card, principal_key=""):
        store[card["key"]] = dict(card)

    async def card_delete(key, principal_key=""):
        return 1 if store.pop(key, None) else 0

    async def models_seen(limit=12):
        return []
    monkeypatch.setattr(db, "card_list", card_list, raising=False)
    monkeypatch.setattr(db, "card_save", card_save, raising=False)
    monkeypatch.setattr(db, "card_delete", card_delete, raising=False)
    monkeypatch.setattr(db, "usage_models_seen", models_seen, raising=False)
    cards.invalidate()
    yield store
    cards.invalidate()
    get_settings.cache_clear()


def _create(c, kind="person", name="Lena", handle="whatsapp: +49 171 1234567"):
    csrf = _csrf(c, "/admin/contacts")
    return c.post("/admin/contacts/new", data={"csrf": csrf, "kind": kind, "name": name, "handle": handle},
                  follow_redirects=False)


def test_contacts_create_edit_and_render(cardsdb):
    c = _client()
    r = _create(c)
    assert r.status_code == 303 and "/admin/contacts/lena" in r.headers["location"]
    page = c.get("/admin/contacts/lena")
    assert page.status_code == 200 and "Lena" in page.text and "Überheblich" in page.text
    assert "Vertrauensstufe" in page.text and "Stil-Vorschau" in page.text
    csrf = _csrf(c, "/admin/contacts/lena")
    c.post("/admin/contacts/lena", data={
        "csrf": csrf, "name": "Lena", "handles": "whatsapp: +49 171 1234567", "trust_tier": "1", "rule": "direct",
        "style": "arrogant", "instruction": "Duzen", "share_availability": "freebusy", "share_location": "no",
        "active_mode": "inherit"}, follow_redirects=False)
    saved = cardsdb["lena"]
    assert saved["trust_tier"] == 1 and saved["rule"] == "direct" and saved["style"] == "arrogant"
    assert saved["share"]["availability"] == "freebusy" and saved["share"]["location"] == "no"
    assert "Lena" in c.get("/admin/contacts").text


def test_group_editor_shows_group_fields_and_saves_trigger(cardsdb):
    c = _client()
    _create(c, kind="group", name="Astroclub", handle="whatsapp: 12-34@g.us")
    page = c.get("/admin/contacts/astroclub")
    assert "Gruppen-Verhalten" in page.text and "Moderator" in page.text
    csrf = _csrf(c, "/admin/contacts/astroclub")
    c.post("/admin/contacts/astroclub", data={
        "csrf": csrf, "name": "Astroclub", "handles": "whatsapp: 12-34@g.us", "trust_tier": "3",
        "group_trigger": "mention", "group_role": "moderator", "group_aliases": "@astro"}, follow_redirects=False)
    g = cardsdb["astroclub"]["group"]
    assert g["role"] == "moderator" and g["aliases"] == ["astro"]


def test_contacts_mutations_require_csrf(cardsdb):
    c = _client()
    _create(c)
    for path in ("/admin/contacts/new", "/admin/contacts/lena", "/admin/contacts/lena/delete",
                 "/admin/contacts/lena/revoke", "/admin/contacts/lena/forget", "/admin/contacts/lena/digest",
                 "/admin/contacts/lena/proposal"):
        assert c.post(path, data={"csrf": "falsch", "name": "x"}, follow_redirects=False).status_code == 403, path
    assert "lena" in cardsdb


def test_revoke_learned_and_delete(cardsdb):
    from app import cards
    c = _client()
    _create(c)
    cardsdb["lena"] = cards.learn_share(cardsdb["lena"], "location", "always_yes")
    assert cardsdb["lena"]["share"]["location"] == "yes"
    assert "Widerrufen" in c.get("/admin/contacts/lena").text
    csrf = _csrf(c, "/admin/contacts/lena")
    c.post("/admin/contacts/lena/revoke", data={"csrf": csrf, "topic": "location"}, follow_redirects=False)
    assert cardsdb["lena"]["share"]["location"] == "" and cardsdb["lena"]["learned"] == []
    c.post("/admin/contacts/lena/delete", data={"csrf": csrf}, follow_redirects=False)
    assert "lena" not in cardsdb


def test_proposal_accept_writes_into_card_and_reject_drops_it(cardsdb):
    c = _client()
    _create(c)
    cardsdb["lena"]["proposals"] = [{"kind": "style", "text": "locker & Insider"},
                                    {"kind": "fact", "text": "spielt Klavier"},
                                    {"kind": "rule", "text": "nie vor 9 Uhr antworten"}]
    csrf = _csrf(c, "/admin/contacts/lena")
    c.post("/admin/contacts/lena/proposal", data={"csrf": csrf, "i": "0", "do": "accept"}, follow_redirects=False)
    assert cardsdb["lena"]["style"] == "locker & Insider" and len(cardsdb["lena"]["proposals"]) == 2
    c.post("/admin/contacts/lena/proposal", data={"csrf": csrf, "i": "0", "do": "accept"}, follow_redirects=False)
    assert "Klavier" in cardsdb["lena"]["notes"]
    c.post("/admin/contacts/lena/proposal", data={"csrf": csrf, "i": "0", "do": "reject"}, follow_redirects=False)
    assert cardsdb["lena"]["proposals"] == [] and cardsdb["lena"]["instruction"] == ""


def test_forget_removes_capsule_and_journal_files(cardsdb):
    from app import digest
    c = _client()
    _create(c)
    stem = digest.stem_for("waha", "+49 171 1234567")
    digest.save_capsule("contacts", stem, {**digest.empty_capsule("k", "Lena"), "summary": "mag Klavier"})
    assert "mag Klavier" in c.get("/admin/contacts/lena").text
    csrf = _csrf(c, "/admin/contacts/lena")
    c.post("/admin/contacts/lena/forget", data={"csrf": csrf}, follow_redirects=False)
    assert digest.load_capsule("contacts", stem) is None
    assert "Noch keine Zusammenfassung" in c.get("/admin/contacts/lena").text


def test_unknown_card_redirects_and_names_are_escaped(cardsdb):
    c = _client()
    assert c.get("/admin/contacts/gibtsnicht", follow_redirects=False).status_code == 303
    _create(c, name="<script>alert(1)</script>", handle="")
    listing = c.get("/admin/contacts").text
    assert "<script>alert(1)</script>" not in listing and "&lt;script&gt;" in listing


# ─── Prompt-Werkstatt ─────────────────────────────────────────────────────────
@pytest.fixture
def promptdir(memdb, monkeypatch, tmp_path):
    from app import prompts
    from app.config import get_settings
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    prompts._CACHE.clear()
    yield tmp_path
    prompts._CACHE.clear()
    get_settings.cache_clear()


def test_prompts_overview_and_editor_render(promptdir):
    c = _client()
    r = c.get("/admin/prompts")
    assert r.status_code == 200 and "Gespräch mit Dritten" in r.text and "Standard" in r.text
    e = c.get("/admin/prompts/third")
    assert e.status_code == 200 and "Pflicht-Platzhalter" in e.text and "{owner}" in e.text


def test_prompt_save_history_diff_rollback_reset(promptdir):
    from app import prompts
    c = _client()
    csrf = _csrf(c, "/admin/prompts/base")
    new = prompts.default_text("base") + "\n- Sei nett."
    assert c.post("/admin/prompts/base", data={"csrf": csrf, "text": new, "note": "nett"},
                  follow_redirects=False).headers["location"].endswith("saved=saved")
    assert "Sei nett" in prompts.get("base") and prompts.is_overridden("base")
    c.post("/admin/prompts/base", data={"csrf": csrf, "text": new + "\n- Noch was."}, follow_redirects=False)
    hist = prompts.history("base")
    assert len(hist) == 2
    page = c.get(f"/admin/prompts/base?v={hist[-1]['version']}")
    assert "Diff: Version" in page.text and "Sei nett" in page.text
    csrf = _csrf(c, "/admin/prompts/base")               # jede Seite stellt ein neues CSRF-Token aus
    c.post("/admin/prompts/base/rollback", data={"csrf": csrf, "version": hist[0]["version"]}, follow_redirects=False)
    assert "Noch was" not in prompts.get("base")
    c.post("/admin/prompts/base/reset", data={"csrf": csrf}, follow_redirects=False)
    assert not prompts.is_overridden("base")


def test_prompt_save_rejects_broken_or_unsafe_text_and_shows_reason(promptdir):
    from app import prompts
    c = _client()
    csrf = _csrf(c, "/admin/prompts/third")
    r = c.post("/admin/prompts/third", data={"csrf": csrf, "text": "Sei frech. {owner"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    page = c.get(r.headers["location"])
    assert "Nicht gespeichert" in page.text
    assert not prompts.is_overridden("third")
    stripped = prompts.default_text("third").replace("PRIVATSPHÄRE", "x").replace("Privatsphäre", "x")
    c.post("/admin/prompts/third", data={"csrf": csrf, "text": stripped}, follow_redirects=False)
    assert not prompts.is_overridden("third")             # Sicherheitskern darf nicht gestrichen werden


def test_proposals_are_reviewed_by_owner_only_and_must_match_the_prompt(promptdir):
    from app import prompts
    c = _client()
    text = prompts.default_text("voice") + "\n- Sprich etwas langsamer."
    pid, problems = prompts.propose("voice", text, "Antworten klangen gehetzt")
    assert pid and not problems
    assert "Sprich etwas langsamer" in c.get("/admin/prompts").text and "Freigeben" in c.get("/admin/prompts").text
    csrf = _csrf(c, "/admin/prompts")
    # falscher Baustein in der URL → nichts passiert
    c.post("/admin/prompts/base/proposal", data={"csrf": csrf, "pid": pid, "do": "approve"}, follow_redirects=False)
    assert not prompts.is_overridden("voice")
    c.post("/admin/prompts/voice/proposal", data={"csrf": csrf, "pid": pid, "do": "approve"}, follow_redirects=False)
    assert "langsamer" in prompts.get("voice")


def test_prompt_self_review_creates_proposal_only(promptdir, monkeypatch):
    from app import prompts
    c = _client()

    async def fake_review(name, **k):
        pid, _ = prompts.propose(name, prompts.default_text(name) + "\n- Bitte kürzer.", "zu lang")
        return {"changed": True, "proposal_id": pid}
    monkeypatch.setattr(prompts, "self_review", fake_review)
    csrf = _csrf(c, "/admin/prompts/owner")
    r = c.post("/admin/prompts/owner/review", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and not prompts.is_overridden("owner")   # NICHT angewendet
    assert len(prompts.proposals("pending")) == 1


def test_prompt_pages_require_csrf_and_known_names(promptdir):
    c = _client()
    assert c.post("/admin/prompts/base", data={"csrf": "x", "text": "y"}, follow_redirects=False).status_code == 403
    assert c.get("/admin/prompts/evil", follow_redirects=False).status_code == 303


# ─── Sicherheit ───────────────────────────────────────────────────────────────
@pytest.fixture
def safetydb(memdb, monkeypatch):
    states = {"modstate:waha:4917011111111@c.us": {"strikes": 3.0, "last_ts": __import__("time").time(),
                                                   "muted_until": 0.0},
              "modstate:signal:+491": {"strikes": 0.0, "last_ts": None, "muted_until": 0.0}}

    async def by_prefix(prefix, limit=200):
        return dict(states)

    async def recent_audit(limit=30):
        return [{"ts": datetime.now(timezone.utc), "event_type": "moderation_inbound", "channel": "waha",
                 "thread_id": "t", "detail": {"categories": ["prompt_injection"]}},
                {"ts": datetime.now(timezone.utc), "event_type": "reply_sent", "channel": "waha",
                 "thread_id": "t", "detail": {}}]
    monkeypatch.setattr(db, "settings_by_prefix", by_prefix, raising=False)
    monkeypatch.setattr(db, "recent_audit", recent_audit, raising=False)
    saved = {}
    orig_set = db.set_setting

    async def set_setting(key, value):
        saved[key] = value
        await orig_set(key, value)
    monkeypatch.setattr(db, "set_setting", set_setting)
    return saved


def test_safety_page_shows_states_events_and_switch(safetydb):
    c = _client()
    r = c.get("/admin/safety")
    assert r.status_code == 200
    assert "4917011111111@c.us" in r.text and "überheblich" in r.text     # 3 Punkte → überheblich
    assert "signal" not in r.text.split("Gestufte")[1].split("Letzte Sicherheitsereignisse")[0]   # 0 Punkte: nicht gelistet
    assert "prompt_injection" in r.text and "reply_sent" not in r.text
    assert "Secretary-Schalter" in r.text and "1 h Pause" in r.text


def test_moderation_settings_are_saved_and_sanitized(safetydb):
    from app import moderation
    c = _client()
    csrf = _csrf(c, "/admin/safety")
    c.post("/admin/safety/moderation", data={
        "csrf": csrf, "enabled": "1", "strictness": "evil", "block_code": "1", "max_inbound_chars": "99999999",
        "out_max_chars": "abc", "ladder_firm_at": "2", "ladder_arrogant_at": "1", "ladder_mute_at": "1",
        "custom_block_words": "foo, bar\nbaz"}, follow_redirects=False)
    cfg = moderation.settings(safetydb["app_settings"])
    assert cfg["enabled"] is True and cfg["strictness"] == "strict"
    assert cfg["max_inbound_chars"] == 20000 and cfg["out_max_chars"] == 900     # geklemmt / Standard
    assert cfg["llm"] is False and cfg["alert_owner"] is False                   # nicht angehakt = aus
    lad = cfg["ladder"]
    assert lad["firm_at"] <= lad["arrogant_at"] <= lad["mute_at"]               # Leiter bleibt geordnet
    assert cfg["custom_block_words"] == ["foo", "bar", "baz"]


def test_reset_only_touches_moderation_state(safetydb, memdb):
    c = _client()
    csrf = _csrf(c, "/admin/safety")
    c.post("/admin/safety/reset", data={"csrf": csrf, "key": "modstate:waha:4917011111111@c.us"}, follow_redirects=False)
    assert safetydb["modstate:waha:4917011111111@c.us"] == {}
    c.post("/admin/safety/reset", data={"csrf": csrf, "key": "app_settings"}, follow_redirects=False)
    assert "app_settings" not in safetydb                 # fremde Schlüssel bleiben unberührt


def test_context_settings_and_secretary_switch(safetydb, monkeypatch):
    calls = []

    async def fake_execute(cmd, *, timezone):
        calls.append(cmd)
        return "ok", True
    monkeypatch.setattr(admin_extra.owner_commands, "execute", fake_execute)
    c = _client()
    csrf = _csrf(c, "/admin/safety")
    c.post("/admin/safety/context", data={"csrf": csrf, "retention_days": "30", "model": "tier:small"},
           follow_redirects=False)
    assert safetydb["app_settings"]["context"] == {"retention_days": 30, "model": {"tier": "small"}}
    c.post("/admin/safety/secretary", data={"csrf": csrf, "do": "pause180"}, follow_redirects=False)
    c.post("/admin/safety/secretary", data={"csrf": csrf, "do": "pause_morgen"}, follow_redirects=False)
    c.post("/admin/safety/secretary", data={"csrf": csrf, "do": "rm -rf"}, follow_redirects=False)
    assert [(x.action, x.minutes, x.tomorrow) for x in calls] == [("off", 180.0, False), ("off", None, True)]


def test_safety_mutations_require_csrf(safetydb):
    c = _client()
    for path in ("moderation", "context", "reset", "secretary", "digest"):
        assert c.post(f"/admin/safety/{path}", data={"csrf": "x"}, follow_redirects=False).status_code == 403


# ─── Modellwahl im Web-Chat ───────────────────────────────────────────────────
@pytest.fixture
def chatdb(memdb, monkeypatch):
    async def none(*a, **k):
        return []
    monkeypatch.setattr(db, "usage_models_seen", none, raising=False)
    monkeypatch.setattr(db, "usage_for_chat", none, raising=False)
    monkeypatch.setattr(db, "list_threads", none, raising=False)
    from app.web import admin as wa

    async def no_sync(store):
        return None
    monkeypatch.setattr(wa, "_sync_channel_threads_into_chats", no_sync)
    monkeypatch.setattr(wa, "_refresh_agent_tools", lambda: none())
    return wa


def _chat_id(c):
    import re
    html = c.get("/admin/chat").text
    return re.search(r'data-chat="([^"]+)"', html).group(1)


def test_chat_model_select_persists_and_shows_in_pill(chatdb):
    c = _client()
    cid = _chat_id(c)
    r = c.post("/admin/chat/settings", json={"chat_id": cid, "model": "model:openrouter|anthropic/claude-sonnet-5"})
    assert r.json()["model_label"] == "openrouter/anthropic/claude-sonnet-5"
    page = c.get(f"/admin/chat?chat={cid}").text
    assert 'id="model"' in page and "openrouter/anthropic/claude-sonnet-5" in page and "selected" in page
    bad = c.post("/admin/chat/settings", json={"chat_id": cid, "model": "model:x y|<b>"})
    assert bad.json()["model_label"].startswith("Standard")           # Müll wird nicht gespeichert


def test_chat_slash_command_switches_model_without_calling_the_llm(chatdb, monkeypatch):
    from app import agent
    called = []

    async def boom(**k):
        called.append(k)
        return {"reply": "x"}
    monkeypatch.setattr(agent, "generate_reply_meta", boom)
    c = _client()
    cid = _chat_id(c)
    r = c.post("/admin/chat/send", json={"chat_id": cid, "message": "/modell klein"})
    assert r.json()["chat_id"] == cid and called == []
    assert "Klein" in c.get(f"/admin/chat?chat={cid}").text
    r = c.post("/admin/chat/send", json={"chat_id": cid, "message": "/modell"})
    assert called == []


def test_chat_send_passes_the_chosen_model_and_chat_id_to_the_agent(chatdb, monkeypatch):
    from app import agent
    seen = {}

    async def fake(**k):
        seen.update(k)
        return {"reply": "Hallo"}
    monkeypatch.setattr(agent, "generate_reply_meta", fake)
    c = _client()
    cid = _chat_id(c)
    c.post("/admin/chat/settings", json={"chat_id": cid, "model": "tier:heavy"})
    c.post("/admin/chat/send", json={"chat_id": cid, "message": "Was gibt es Neues?"})
    assert seen["model_pick"] == {"tier": "heavy"} and seen["chat_id"] == cid
    c.post("/admin/chat/settings", json={"chat_id": cid, "model": ""})
    c.post("/admin/chat/send", json={"chat_id": cid, "message": "Und jetzt?"})
    assert seen["model_pick"] is None


def test_chat_usage_command_replies_with_report(chatdb, monkeypatch):
    async def fake_report(period, by, tz="Europe/Berlin"):
        return f"Bericht {period}/{by}"
    monkeypatch.setattr(usage, "report", fake_report)
    c = _client()
    cid = _chat_id(c)
    c.post("/admin/chat/send", json={"chat_id": cid, "message": "/verbrauch woche zweck"})
    assert "Bericht week/purpose" in c.get(f"/admin/chat?chat={cid}").text


# ─── Sammelaktionen in der Kontakt-Liste ──────────────────────────────────────
@pytest.fixture
def bulkdb(cardsdb, monkeypatch):
    known = [{"channel": "waha", "handle": "4915999999999@c.us", "display_name": "Tom", "trust_tier": 3},
             {"channel": "waha", "handle": "123-456@g.us", "display_name": "Astroclub", "trust_tier": 3},
             {"channel": "waha", "handle": "491711234567@c.us", "display_name": "Lena WA", "trust_tier": 3}]

    async def contacts_list(limit=500):
        return known
    monkeypatch.setattr(db, "contacts_list", contacts_list, raising=False)
    return cardsdb


def _ref_for(page_html: str, name: str) -> str:
    import re
    m = re.search(r'value="([^"]+)" class="rowsel" aria-label="' + re.escape(name) + ' auswählen"', page_html)
    assert m, f"{name} nicht in der Liste"
    import html as h
    return h.unescape(m.group(1))


def test_directory_lists_known_contacts_without_cards_and_all_can_be_selected(bulkdb):
    c = _client()
    _create(c)                                                        # Lena mit Karte (+49 171 1234567)
    page = c.get("/admin/contacts").text
    assert "Tom" in page and "ohne Karte" in page and "Astroclub" in page
    assert "Lena WA" not in page                                      # gleiche Nummer wie Karte → keine Dublette
    assert 'id="selall"' in page and "Regeln für die Auswahl" in page
    nocard = c.get("/admin/contacts?scope=nocard").text
    assert "Tom" in nocard and "aria-label=\"Lena auswählen\"" not in nocard


def test_bulk_apply_sets_only_chosen_fields_and_creates_cards_for_cardless_contacts(bulkdb):
    c = _client()
    _create(c)
    bulkdb["lena"]["style"] = "warm"
    page = c.get("/admin/contacts").text
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/contacts/bulk", data={
        "csrf": csrf, "do": "apply",
        "sel": [_ref_for(page, "Lena"), _ref_for(page, "Tom"), _ref_for(page, "Astroclub")],
        "b_rule": "ask", "b_share_location": "no", "b_group_trigger": "always", "b_style": ""},
        follow_redirects=False)
    assert r.status_code == 303 and "saved=bulk&n=3" in r.headers["location"]
    assert bulkdb["lena"]["rule"] == "ask" and bulkdb["lena"]["share"]["location"] == "no"
    assert bulkdb["lena"]["style"] == "warm"                          # nicht angefasst
    assert bulkdb["tom"]["rule"] == "ask" and bulkdb["tom"]["handles"] == [{"channel": "waha", "id": "4915999999999@c.us"}]
    grp = bulkdb["astroclub"]
    assert grp["kind"] == "group" and grp["group"]["trigger"] == "always"
    assert bulkdb["tom"]["group"]["trigger"] == "mention"             # Gruppenfeld nur bei Gruppen


def test_bulk_clear_and_delete_and_empty_cases(bulkdb):
    c = _client()
    _create(c)
    bulkdb["lena"]["rule"] = "direct"
    page = c.get("/admin/contacts").text
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    ref = _ref_for(page, "Lena")
    c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "apply", "sel": ref, "b_rule": "__clear__"},
           follow_redirects=False)
    assert bulkdb["lena"]["rule"] == ""
    assert "bulknone" in c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "apply", "sel": ref},
                                follow_redirects=False).headers["location"]          # keine Änderung gewählt
    assert "bulknone" in c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "apply", "b_rule": "ask"},
                                follow_redirects=False).headers["location"]          # nichts ausgewählt
    assert "bulknone" in c.post("/admin/contacts/bulk", data={"csrf": csrf, "sel": "müll", "b_rule": "ask"},
                                follow_redirects=False).headers["location"]          # ungültige Auswahl
    c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "delete", "sel": ref}, follow_redirects=False)
    assert "lena" not in bulkdb


def test_bulk_requires_csrf_and_cannot_write_arbitrary_fields(bulkdb):
    c = _client()
    _create(c)
    assert c.post("/admin/contacts/bulk", data={"csrf": "x", "sel": "card:lena", "b_rule": "ask"},
                  follow_redirects=False).status_code == 403
    csrf = _csrf(c, "/admin/contacts")
    c.post("/admin/contacts/bulk", data={"csrf": csrf, "sel": "card:lena", "b_rule": "root", "b_key": "hack",
                                         "b_handles": "x"}, follow_redirects=False)
    assert bulkdb["lena"]["rule"] == "" and bulkdb["lena"]["key"] == "lena"


# ─── Secretary-Schalter in der Liste ──────────────────────────────────────────
def test_secretary_toggle_json_flips_and_creates_cards_for_cardless_contacts(bulkdb):
    c = _client()
    _create(c)
    page = c.get("/admin/contacts").text
    assert 'role="switch"' in page and ">An<" in page and "Secretary" in page
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    ref = _ref_for(page, "Lena")
    r = c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": ref + "|0"},
               headers={"Accept": "application/json"})
    assert r.json() == {"ok": True, "on": False, "key": "lena", "name": "Lena"}
    assert bulkdb["lena"]["active"]["mode"] == "never"
    assert ">Aus<" in c.get("/admin/contacts").text
    assert "Lena" in c.get("/admin/contacts?scope=off").text
    csrf = c.cookies.get(auth.CSRF_COOKIE)                    # jede Seite stellt ein neues Token aus
    r = c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": ref + "|1"}, headers={"Accept": "application/json"})
    assert r.json()["on"] is True and bulkdb["lena"]["active"]["mode"] == "inherit"
    tom = _ref_for(c.get("/admin/contacts").text, "Tom")
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    r = c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": tom + "|0"}, headers={"Accept": "application/json"})
    assert r.json()["on"] is False and bulkdb["tom"]["active"]["mode"] == "never"      # Karte neu angelegt


def test_secretary_toggle_without_js_redirects_and_validates(bulkdb):
    c = _client()
    _create(c)
    csrf = _csrf(c, "/admin/contacts")
    r = c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": "card:lena|0"}, follow_redirects=False)
    assert r.status_code == 303 and "secoff" in r.headers["location"]
    for bad in ("card:lena|2", "müll|1", "|", ""):
        r = c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": bad}, headers={"Accept": "application/json"})
        assert r.status_code == 400, bad
    assert c.post("/admin/contacts/secretary", data={"csrf": csrf, "toggle": "card:gibtsnicht|1"},
                  headers={"Accept": "application/json"}).status_code == 404
    assert c.post("/admin/contacts/secretary", data={"csrf": "x", "toggle": "card:lena|0"}).status_code == 403


def test_bulk_secretary_off_and_on_for_selection(bulkdb):
    c = _client()
    _create(c)
    page = c.get("/admin/contacts").text
    assert "Ausschalten" in page and "Einschalten" in page
    csrf = c.cookies.get(auth.CSRF_COOKIE)
    sel = [_ref_for(page, "Lena"), _ref_for(page, "Tom"), _ref_for(page, "Astroclub")]
    r = c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "sec_off", "sel": sel}, follow_redirects=False)
    assert "bulkoff&n=3" in r.headers["location"]
    assert all(bulkdb[k]["active"]["mode"] == "never" for k in ("lena", "tom", "astroclub"))
    c.post("/admin/contacts/bulk", data={"csrf": csrf, "do": "sec_on", "sel": sel[:1]}, follow_redirects=False)
    assert bulkdb["lena"]["active"]["mode"] == "inherit" and bulkdb["tom"]["active"]["mode"] == "never"


def test_editor_shows_the_switch(bulkdb):
    c = _client()
    _create(c)
    assert 'role="switch"' in c.get("/admin/contacts/lena").text
