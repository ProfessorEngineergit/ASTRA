"""Kontaktkarten: Bereinigung, Handle-Abgleich, Freigaben, Gruppen-Regeln, Zeitfenster."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import cards
from app import cards as c

TZ = ZoneInfo("Europe/Berlin")


def _person(**kw):
    base = {"name": "Lena", "handles": [{"channel": "waha", "id": "+49 171 1234567"}]}
    base.update(kw)
    return c.sanitize(base)


def _group(**kw):
    g = {"kind": "group", "name": "Astroclub", "handles": [{"channel": "waha", "id": "12345-6789@g.us"}],
         "group": {"trigger": "mention", "role": "assistant"}}
    g.update(kw)
    return c.sanitize(g)


# ─── Bereinigen ───────────────────────────────────────────────────────────────
def test_sanitize_fills_defaults_and_slugs():
    card = c.sanitize({"name": "Lena Käß"})
    assert card["key"] == "lena_kass" and card["kind"] == "person" and card["trust_tier"] == 3
    assert card["rule"] == "" and card["share"]["availability"] == ""


def test_sanitize_rejects_invalid_enums():
    card = c.sanitize({"name": "x", "rule": "hax", "trust_tier": 99, "kind": "robot",
                       "share": {"availability": "everything", "location": "maybe"},
                       "group": {"trigger": "always-yes", "role": "god"},
                       "active": {"mode": "sometimes"}})
    assert card["rule"] == "" and card["trust_tier"] == 3 and card["kind"] == "person"
    assert card["share"]["availability"] == "" and card["share"]["location"] == ""
    assert card["group"]["trigger"] == "mention" and card["group"]["role"] == "assistant"
    assert card["active"]["mode"] == "inherit"


def test_sanitize_is_idempotent():
    once = _person(style="arrogant", instruction="Immer duzen", share={"availability": "details"})
    assert c.sanitize(once) == once


def test_sanitize_dedupes_handles_and_drops_junk():
    card = c.sanitize({"name": "x", "handles": [
        {"channel": "waha", "id": "0171 1234567"}, {"channel": "waha", "id": "+49 171 1234567"},
        {"channel": "", "id": "x"}, "müll"]})
    assert len(card["handles"]) == 1


def test_model_pick_is_validated():
    assert c.sanitize({"name": "x", "model": {"tier": "SMALL"}})["model"] == {"tier": "small"}
    assert c.sanitize({"name": "x", "model": {"provider": "OpenAI", "model": "gpt-4o"}})["model"] == \
        {"provider": "openai", "model": "gpt-4o"}
    assert c.sanitize({"name": "x", "model": {"provider": "openai"}})["model"] == {}


def test_length_limits():
    card = c.sanitize({"name": "n" * 500, "instruction": "i" * 9000, "notes": "x" * 99999})
    assert len(card["name"]) <= 80 and len(card["instruction"]) <= 1500 and len(card["notes"]) <= 4000


# ─── Handle-Abgleich ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("incoming", [
    "491711234567@c.us", "0171 1234567", "+49 171 1234567", "49171 1234567", "491711234567",
])
def test_phone_formats_all_match_one_card(incoming):
    card = _person()
    assert c.matches(card, "waha", incoming), incoming
    assert c.matches(card, "signal", incoming) is False     # anderer Kanal


def test_other_number_does_not_match():
    assert not c.matches(_person(), "waha", "4915199999999@c.us")


def test_group_ids_and_email_match_exactly():
    g = _group()
    assert c.matches(g, "waha", "12345-6789@g.us") and not c.matches(g, "waha", "99999-1@g.us")
    m = c.sanitize({"name": "Mail", "handles": [{"channel": "email", "id": "Lena@Example.com"}]})
    assert c.matches(m, "email", "lena@example.com")


def test_find_in_can_filter_by_kind():
    cards = [_person(), _group()]
    assert c.find_in(cards, "waha", "12345-6789@g.us")["kind"] == "group"
    assert c.find_in(cards, "waha", "12345-6789@g.us", kind="person") is None


# ─── Freigaben ────────────────────────────────────────────────────────────────
def test_share_ceiling_and_unset():
    assert c.share_ceiling(_person(share={"availability": "freebusy"})) == "freebusy"
    assert c.share_ceiling(_person()) is None and c.share_ceiling(None) is None


def test_share_prompt_lists_allowed_and_forbidden():
    p = c.share_prompt(_person(share={"availability": "freebusy", "location": "no", "school": "yes"}))
    assert "frei oder beschäftigt" in p and "VERBOTEN" in p and "Aufenthaltsort" in p and "Schule" in p
    assert c.share_prompt(_person()) == ""


def test_availability_none_forbids_even_busy_status():
    assert "auch ob er frei oder beschäftigt" in c.share_prompt(_person(share={"availability": "none"}))


def test_instruction_block_names_person_or_group():
    assert "Person" in c.instruction_block(_person(instruction="duzen"))
    assert "Gruppe" in c.instruction_block(_group(instruction="nur Termine"))
    assert c.instruction_block(_person()) == ""


@pytest.mark.parametrize("text,topic", [
    ("Wann hat er morgen Zeit? Ist er frei?", "availability"), ("wo wohnt er eigentlich", "location"),
    ("Hat er heute Schule, wie ist der Stundenplan?", "school"), ("gib mir seine Handynummer", "contact"),
    ("was ist sein Lieblingsessen", "personal"),
])
def test_topic_detection(text, topic):
    assert c.topic_for(text) == topic


def test_learning_always_and_never():
    card = _person()
    yes = c.learn_share(card, "availability", "always_yes", now=100)
    assert yes["share"]["availability"] == "details" and yes["learned"][-1]["topic"] == "availability"
    busy = c.learn_share(card, "availability", "always_busy")
    assert busy["share"]["availability"] == "freebusy"
    never = c.learn_share(card, "location", "never")
    assert never["share"]["location"] == "no"
    assert c.learn_share(card, "location", "yes")["share"]["location"] == ""     # einmaliges Ja lernt nichts


def test_learned_rules_can_be_revoked():
    card = c.learn_share(_person(), "availability", "always_yes")
    back = c.revoke_learned(card, "availability")
    assert back["share"]["availability"] == "" and not back["learned"]


# ─── Gruppen ──────────────────────────────────────────────────────────────────
TOK = c.mention_tokens("Bahrian", None, ["491771845224@c.us"])


def test_mention_tokens_include_name_astra_and_own_number():
    assert {"bahrian", "astra", "491771845224"} <= set(TOK)


@pytest.mark.parametrize("text", ["@Bahrian bist du da?", "hey @bahrian", "@astra was geht", "@491771845224 hilfe",
                                  "Frage an @ Bahrian: Zeit?"])
def test_at_mentions_are_detected(text):
    assert c.is_mentioned(text, TOK)


@pytest.mark.parametrize("text", ["Bahrian ist heute krank", "wir reden über astra", "mail@bahrian.de",
                                  "kommt Bahrian morgen?"])
def test_plain_words_are_not_a_mention(text):
    assert not c.is_mentioned(text, TOK)


def test_group_without_card_stays_silent():
    d = c.group_decision(None, "@Bahrian hi", tokens=TOK)
    assert d.respond is False and d.reason == "unbekannte-gruppe"


def test_group_mention_trigger():
    g = _group()
    assert c.group_decision(g, "@Bahrian Zeit morgen?", tokens=TOK).respond is True
    assert c.group_decision(g, "wer kommt morgen?", tokens=TOK).respond is False


def test_group_always_trigger():
    g = _group(group={"trigger": "always", "role": "assistant"})
    assert c.group_decision(g, "irgendwas", tokens=TOK).respond is True


def test_group_keyword_trigger():
    g = _group(group={"trigger": "keywords", "keywords": "termin, treffen"})
    assert c.group_decision(g, "wann ist das nächste Treffen?", tokens=TOK).respond is True
    assert c.group_decision(g, "lustiges Video", tokens=TOK).respond is False


def test_listener_role_never_answers_even_when_mentioned():
    g = _group(group={"trigger": "always", "role": "listener"})
    assert c.group_decision(g, "@Bahrian hallo", tokens=TOK).respond is False


def test_blocked_group_and_off_trigger():
    assert c.group_decision(_group(rule="block"), "@Bahrian", tokens=TOK).respond is False
    assert c.group_decision(_group(group={"trigger": "off"}), "@Bahrian", tokens=TOK).respond is False


def test_reply_to_astra_counts_as_being_addressed():
    assert c.group_decision(_group(), "ja genau", tokens=TOK, reply_to_us=True).respond is True


def test_moderator_steps_in_on_flagged_messages_without_mention():
    g = _group(group={"trigger": "mention", "role": "moderator"})
    d = c.group_decision(g, "du idiot", tokens=TOK, flagged=True)
    assert d.respond and d.moderating
    # Ein normaler Assistent moderiert nicht.
    assert c.group_decision(_group(), "du idiot", tokens=TOK, flagged=True).respond is False


# ─── Zeitfenster ──────────────────────────────────────────────────────────────
def _at(h, m=0, day=18):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)     # 18.09.2026 ist ein Freitag


def test_active_inherit_always_never():
    assert c.active_state(_person(), _at(12)) is None
    assert c.active_state(_person(active={"mode": "always"}), _at(3)) is True
    assert c.active_state(_person(active={"mode": "never"}), _at(12)) is False


def test_active_window_and_days():
    card = _person(active={"mode": "window", "start": "15:00", "end": "22:00", "days": [4]})
    assert c.active_state(card, _at(16)) is True
    assert c.active_state(card, _at(10)) is False
    assert c.active_state(card, _at(16, day=19)) is False       # Samstag nicht erlaubt


def test_active_window_over_midnight():
    card = _person(active={"mode": "window", "start": "22:00", "end": "06:00"})
    assert c.active_state(card, _at(23)) is True and c.active_state(card, _at(2)) is True
    assert c.active_state(card, _at(12)) is False


# ─── Import bestehender Daten ─────────────────────────────────────────────────
PROFILE = """# Lena

- **Beziehung:** Freundin
- **Trust-Tier:** 1
- **Ton:** locker, viel Insider-Humor

<!-- astra:handles
whatsapp:
signal:
telegram:
email: lena@example.com
phone: 0171 1234567
-->
"""


def test_legacy_profile_becomes_a_card():
    out = c.legacy_cards([{"rel": "people/lena.md", "title": "Lena", "content": PROFILE}], [])
    assert len(out) == 1
    card = out[0]
    assert card["name"] == "Lena" and card["trust_tier"] == 1 and card["relationship"] == "Freundin"
    assert card["style"].startswith("locker")
    assert c.matches(card, "waha", "491711234567@c.us")
    assert c.matches(card, "signal", "+49 171 1234567") and c.matches(card, "email", "lena@example.com")


def test_legacy_contact_rules_attach_to_matching_profile_or_create_new():
    profile = {"rel": "people/lena.md", "title": "Lena", "content": PROFILE}
    rules = [{"channel": "waha", "id": "491711234567@c.us", "rule": "direct"},
             {"channel": "waha", "id": "555-1@g.us", "rule": "allow", "note": "Klassenchat"}]
    out = {x["name"]: x for x in c.legacy_cards([profile], rules)}
    assert out["Lena"]["rule"] == "direct"
    assert out["Klassenchat"]["kind"] == "group" and out["Klassenchat"]["rule"] == "allow"


# ─── Formular ↔ Karte ─────────────────────────────────────────────────────────
class _Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]


def test_parse_handles_understands_channels_numbers_and_mail():
    hs = cards.parse_handles("whatsapp: +49 171 1234567\nSignal:+4917011111111\nlena@example.org\n0171 555 000\nquatsch")
    assert {"channel": "waha", "id": "+49 171 1234567"} in hs
    assert {"channel": "signal", "id": "+4917011111111"} in hs
    assert {"channel": "email", "id": "lena@example.org"} in hs
    assert {"channel": "waha", "id": "0171555000"} in hs
    assert len(hs) == 4                                   # „quatsch“ wird verworfen


def test_card_from_form_roundtrip_keeps_learned_and_key():
    existing = cards.sanitize({"key": "lena", "name": "Lena", "learned": [{"topic": "school", "level": "yes"}],
                               "proposals": [{"kind": "fact", "text": "mag Klavier"}]})
    form = _Form(name="Lena K.", handles="whatsapp: 4915111111111", trust_tier="1", rule="allow",
                 style="arrogant", instruction="Duzen", share_availability="freebusy",
                 share_location="no", share_school="", share_contact="", share_personal="",
                 active_mode="window", active_start="08:00", active_end="20:00", days=["0", "4", "x"],
                 model="tier:small")
    c = cards.card_from_form(form, existing)
    assert c["key"] == "lena" and c["name"] == "Lena K." and c["trust_tier"] == 1 and c["rule"] == "allow"
    assert c["share"]["availability"] == "freebusy" and c["share"]["location"] == "no"
    assert c["active"]["days"] == [0, 4] and c["model"] == {"tier": "small"}
    assert c["learned"] and c["proposals"]                # bleibt beim Bearbeiten erhalten


def test_card_from_form_group_fields_and_custom_style_and_bad_values():
    form = _Form(kind="group", name="Astroclub", handles="12-34@g.us", style="__custom__",
                 style_custom="  wie ein Pirat  ", trust_tier="99", rule="root", group_trigger="keywords",
                 group_keywords="termin, treffen", group_aliases="@astro", group_role="moderator",
                 group_actions="on", active_mode="???")
    c = cards.card_from_form(form)
    assert c["kind"] == "group" and c["style"] == "wie ein Pirat" and c["trust_tier"] == 3
    assert c["rule"] == "" and c["group"]["trigger"] == "keywords" and c["group"]["role"] == "moderator"
    assert c["group"]["keywords"] == ["termin", "treffen"] and c["group"]["aliases"] == ["astro"]
    assert c["group"]["actions"] is True and c["active"]["mode"] == "inherit"


def test_apply_patch_changes_only_named_fields_and_reports_them():
    base = cards.sanitize({"name": "Lena", "style": "warm", "instruction": "Duzen",
                           "share": {"availability": "details"}})
    new, changed = cards.apply_patch(base, {"style": "arrogant", "share_location": "no", "trust_tier": 2,
                                            "bogus": "x", "share_availability": None, "model_tier": "small"})
    assert new["style"] == "arrogant" and new["instruction"] == "Duzen"
    assert new["share"]["location"] == "no" and new["share"]["availability"] == "details"
    assert new["trust_tier"] == 2 and new["model"] == {"tier": "small"}
    assert set(changed) == {"style", "share_location", "trust_tier", "model_tier"}


def test_apply_patch_invalid_values_are_sanitized_and_group_fields_work():
    g = cards.sanitize({"kind": "group", "name": "Club"})
    new, _ = cards.apply_patch(g, {"rule": "root", "group_trigger": "keywords", "group_keywords": "a, b",
                                   "active_mode": "window", "active_start": "08:00", "active_end": "12:00"})
    assert new["rule"] == "" and new["group"]["trigger"] == "keywords" and new["group"]["keywords"] == ["a", "b"]
    assert new["active"]["mode"] == "window" and new["active"]["start"] == "08:00"


def test_find_by_name_exact_partial_and_number():
    lst = [cards.sanitize({"name": "Lena Kraft", "handles": [{"channel": "waha", "id": "+49 171 1234567"}]}),
           cards.sanitize({"name": "Lena Müller"}), cards.sanitize({"name": "Tom"})]
    assert cards.find_by_name(lst, "tom")[0][0]["name"] == "Tom"
    hits, why = cards.find_by_name(lst, "lena")
    assert why == "partial" and len(hits) == 2                    # mehrdeutig → Aufrufer fragt nach
    assert cards.find_by_name(lst, "0171 1234567")[1] == "handle"
    assert cards.find_by_name(lst, "")[0] == []


# ─── Verzeichnis & Sammelaktionen ─────────────────────────────────────────────
def test_directory_merges_cards_and_cardless_contacts_without_duplicates():
    lena = cards.sanitize({"name": "Lena", "handles": [{"channel": "waha", "id": "+49 171 1234567"}]})
    rows = cards.directory([lena], [
        {"channel": "waha", "handle": "491711234567@c.us", "display_name": "Lena (WA)"},     # hat Karte
        {"channel": "waha", "handle": "4915999999999@c.us", "display_name": "Tom", "trust_tier": 2},
        {"channel": "waha", "handle": "4915999999999@c.us", "display_name": "Tom doppelt"},
        {"channel": "waha", "handle": "123-456@g.us", "display_name": "Astroclub"},
        {"channel": "", "handle": "x"}])
    assert [r["name"] for r in rows] == ["Lena", "Tom", "Astroclub"]
    tom = rows[1]
    assert tom["has_card"] is False and tom["tier"] == 2 and rows[2]["kind"] == "group"


def test_refs_roundtrip_and_reject_garbage():
    row = {"key": None, "channel": "waha", "handle": "49|171@c.us", "name": "Zoë | Test", "tier": 2}
    assert cards.decode_ref(cards.encode_ref(row)) == {"channel": "waha", "handle": "49|171@c.us", "name": "Zoë | Test",
                                                       "tier": 2}
    assert cards.decode_ref("card:lena") == {"key": "lena"}
    assert cards.decode_ref("new:%%%") is None and cards.decode_ref("evil") is None


def test_filter_directory_scopes_and_search():
    rows = cards.directory([cards.sanitize({"name": "Lena", "handles": [{"channel": "signal", "id": "+491"}]}),
                            cards.sanitize({"kind": "group", "name": "Club", "proposals": [{"kind": "fact", "text": "x"}]})],
                           [{"channel": "waha", "handle": "4915@c.us", "display_name": "Tom"}])
    assert [r["name"] for r in cards.filter_directory(rows, scope="person")] == ["Lena", "Tom"]
    assert [r["name"] for r in cards.filter_directory(rows, scope="group")] == ["Club"]
    assert [r["name"] for r in cards.filter_directory(rows, scope="nocard")] == ["Tom"]
    assert [r["name"] for r in cards.filter_directory(rows, scope="proposals")] == ["Club"]
    assert [r["name"] for r in cards.filter_directory(rows, q="signal")] == ["Lena"]


def test_bulk_patch_semantics_and_group_fields_only_hit_groups():
    form = {"b_rule": "ask", "b_share_location": "__clear__", "b_style": "", "b_group_trigger": "always",
            "b_trust_tier": "2", "b_bogus": "x"}
    patch = cards.bulk_patch(form.get)
    assert patch == {"rule": "ask", "share_location": "", "group_trigger": "always", "trust_tier": "2"}
    person = cards.sanitize({"name": "Lena", "share": {"location": "yes"}, "style": "warm"})
    new, changed = cards.apply_bulk(person, patch)
    assert new["rule"] == "ask" and new["share"]["location"] == "" and new["style"] == "warm"
    assert "group_trigger" not in changed and new["group"]["trigger"] == "mention"
    grp = cards.sanitize({"kind": "group", "name": "Club"})
    assert cards.apply_bulk(grp, patch)[0]["group"]["trigger"] == "always"


# ─── Secretary-Schalter pro Person ────────────────────────────────────────────
def test_secretary_on_semantics_and_setting():
    assert cards.secretary_on(None) is True                                  # ohne Karte: folgt dem globalen Secretary
    lena = cards.sanitize({"name": "Lena"})
    assert cards.secretary_on(lena) is True
    off = cards.set_secretary(lena, False)
    assert cards.secretary_on(off) is False and off["active"]["mode"] == "never"
    back = cards.set_secretary(off, True)
    assert cards.secretary_on(back) is True and back["active"]["mode"] == "inherit"
    blocked = cards.sanitize({"name": "Spam", "rule": "block"})
    assert cards.secretary_on(blocked) is False
    assert cards.set_secretary(blocked, True)["rule"] == ""                  # Einschalten hebt die Sperre auf


def test_switching_on_keeps_other_settings_and_always_window():
    card = cards.sanitize({"name": "Lena", "style": "arrogant", "rule": "ask", "active": {"mode": "always"}})
    on = cards.set_secretary(card, True)
    assert on["style"] == "arrogant" and on["rule"] == "ask" and on["active"]["mode"] == "always"
    assert cards.set_secretary(card, False)["style"] == "arrogant"


def test_directory_reports_secretary_state_and_off_scope():
    rows = cards.directory([cards.set_secretary(cards.sanitize({"name": "Lena"}), False),
                            cards.sanitize({"name": "Tom"})], [{"channel": "waha", "handle": "4915@c.us", "display_name": "Mia"}])
    assert {r["name"]: r["sec_on"] for r in rows} == {"Lena": False, "Tom": True, "Mia": True}
    assert [r["name"] for r in cards.filter_directory(rows, scope="off")] == ["Lena"]


def test_switching_on_again_keeps_a_configured_time_window():
    card = cards.sanitize({"name": "Lena", "active": {"mode": "window", "start": "08:00", "end": "20:00", "days": [0, 1]}})
    off = cards.set_secretary(card, False)
    back = cards.set_secretary(off, True)
    assert off["active"]["mode"] == "never" and back["active"]["mode"] == "window"
    assert back["active"]["start"] == "08:00" and back["active"]["days"] == [0, 1]
