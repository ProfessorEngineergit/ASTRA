"""Smart-Antwort: Nachrichtenart, Entscheidung, Marker, Vorstellung."""
from __future__ import annotations

import pytest

from app import smart_reply as sr

NOW = 1_000_000.0
CFG = sr.settings(None)


@pytest.mark.parametrize("text,kind", [
    ("", sr.NOISE), ("   ", sr.NOISE), ("😂", sr.NOISE), ("😂😂😂", sr.NOISE), ("👍", sr.NOISE), ("❤️", sr.NOISE),
    ("😂 !!", sr.NOISE), ("haha", sr.NOISE), ("hahahaha", sr.NOISE), ("lol", sr.NOISE), ("XD", sr.NOISE),
    ("krass", sr.NOISE), ("hmm", sr.NOISE), ("https://tenor.com/view/abc", sr.NOISE),
    ("https://youtu.be/x https://youtu.be/y", sr.NOISE),
    ("ok", sr.THANKS), ("Okay!", sr.THANKS), ("danke", sr.THANKS), ("Danke dir 🙏", sr.THANKS), ("alles klar", sr.THANKS),
    ("bis morgen", sr.THANKS), ("Gute Nacht", sr.THANKS), ("super danke", sr.THANKS), ("alles klar, bis später", sr.THANKS),
    ("ok dann bis morgen", sr.THANKS), ("Danke, wann kommt er heute?", sr.REQUEST),
    ("hey", sr.PING), ("Hallo", sr.PING), ("Hi Bahrian", sr.PING), ("Moin!", sr.PING), ("bist du da?", sr.PING),
    ("Bahrian, bist du da? Hallo, kann ich mit dir reden?", sr.PING),
    ("Hallo, kann ich mit dir reden?", sr.PING), ("hast du kurz zeit?", sr.PING), ("Kann ich dich was fragen?", sr.PING),
    ("bist du online??", sr.PING), ("hey, hast du mal kurz zeit", sr.PING), ("??", sr.PING), ("hey 👋", sr.PING),
    ("Hast du morgen Zeit?", sr.REQUEST), ("Bist du am Samstag frei?", sr.REQUEST), ("Wann hast du heute Schluss?", sr.REQUEST),
    ("Können wir uns um 18 Uhr treffen", sr.REQUEST), ("Hast du am Wochenende Zeit für ein Treffen?", sr.REQUEST),
    ("Was steht diese Woche in deinem Kalender", sr.REQUEST), ("Richte ihm bitte aus, dass ich später komme", sr.REQUEST),
    ("astra, ist er heute erreichbar", sr.REQUEST), ("Wann ist der nächste Termin?", sr.REQUEST),
    ("Ist Bahrian in der Schule?", sr.REQUEST), ("Kannst du Freitag um 15:30?", sr.REQUEST),
    ("Wie war dein Tag", sr.CHAT), ("Das Konzert gestern war der Wahnsinn, du hättest da sein müssen", sr.CHAT),
    ("Ich hab übrigens ein neues Handy bekommen", sr.CHAT), ("Was hältst du von dem neuen Update?", sr.CHAT),
])
def test_classify(text, kind):
    assert sr.classify(text) == kind, text


def test_ping_only_when_no_concrete_concern():
    assert sr.is_ping("Hallo, bist du da?") and not sr.is_ping("Hallo, bist du morgen da?")
    assert not sr.is_ping("Hallo Bahrian, ich wollte dir was zeigen")
    assert not sr.is_ping("")


def _decide(kind, *, smart=True, state="idle", meta=None, ignore=True):
    return sr.decide(kind, smart=smart, ignore_noise=ignore, state=state, meta=meta or {}, now=NOW)


def test_noise_and_thanks_are_always_ignored_even_without_smart_mode():
    for kind in (sr.NOISE, sr.THANKS):
        assert _decide(kind).action == "ignore" and _decide(kind, smart=False).action == "ignore"
    assert _decide(sr.NOISE, ignore=False, smart=False).action == "reply"      # abschaltbar


def test_fresh_conversation_waits_for_requests_and_intros():
    for kind in (sr.REQUEST, sr.PING, sr.CHAT):
        v = _decide(kind)
        assert v.action == "wait" and v.kind == kind
    assert _decide(sr.REQUEST, smart=False).action == "reply"                   # ohne Smart: wie bisher normal


def test_running_conversation_replies_instantly_to_requests_and_ignores_chitchat():
    meta = {"smart_until": NOW + 600}
    assert _decide(sr.REQUEST, state="answered", meta=meta).action == "reply"
    assert _decide(sr.CHAT, state="answered", meta=meta).action == "ignore"
    assert _decide(sr.PING, state="answered", meta=meta).action == "ignore"
    # abgelaufenes Gespräch = frisch: wieder warten
    assert _decide(sr.REQUEST, state="answered", meta={"smart_until": NOW - 1}).action == "wait"
    # Zustand nicht „answered“ (z. B. Stand-down nach Eingreifen): auch wenn der Marker noch stünde → warten
    assert _decide(sr.REQUEST, state="standdown", meta=meta).action == "wait"


def test_quiet_period_after_intro_only_answers_real_requests():
    meta = {"quiet_until": NOW + 3600}
    assert _decide(sr.CHAT, state="answered", meta=meta).action == "ignore"
    assert _decide(sr.PING, state="answered", meta=meta).action == "ignore"
    assert _decide(sr.REQUEST, state="answered", meta=meta).action == "reply"
    assert _decide(sr.CHAT, state="answered", meta={"quiet_until": NOW - 1}).action == "wait"      # Ruhe vorbei


def test_messages_arriving_while_waiting_only_upgrade_the_kind_and_never_restart_the_timer():
    assert _decide(sr.CHAT, state="deferred").action == "wait" and _decide(sr.CHAT, state="deferred").reason == "smart-wait-more"
    assert sr.merge_kind(sr.PING, sr.REQUEST) == sr.REQUEST
    assert sr.merge_kind(sr.REQUEST, sr.PING) == sr.REQUEST
    assert sr.merge_kind(sr.PING, sr.CHAT) == sr.CHAT
    assert sr.merge_kind("", sr.PING) == sr.PING


def test_markers_after_reply_and_after_owner():
    intro = sr.meta_after_reply(sr.PING, CFG, NOW)
    assert intro["quiet_until"] == NOW + 180 * 60 and intro["smart_until"] == NOW + 30 * 60
    ans = sr.meta_after_reply(sr.REQUEST, CFG, NOW)
    assert "quiet_until" not in ans and ans["smart_until"] == NOW + 1800
    assert sr.meta_after_owner() == {"smart_until": 0, "quiet_until": 0, "smart_kind": ""}


def test_settings_clamp_and_applies_rules():
    s = sr.settings({"secretary": {"smart": {"wait_seconds": "9999", "conversation_minutes": 0, "quiet_minutes": "x",
                                            "enabled": False}}})
    assert s["wait_seconds"] == 900 and s["conversation_minutes"] == 1 and s["quiet_minutes"] == 180 and s["enabled"] is False
    sec = lambda mode, act="on": {"activation_mode": act, "channels": {"waha": {"mode": mode}}}      # noqa: E731
    assert sr.applies(sec("policy"), "waha", None, CFG) and sr.applies(sec("school_direct"), "waha", None, CFG)
    assert sr.applies(sec("smart", "auto"), "waha", None, CFG)                     # ausdrücklich „smart“ gilt immer
    assert not sr.applies(sec("policy", "auto"), "waha", None, CFG)                # Auto/Schulzeit bleibt wie bisher
    assert not sr.applies(sec("direct"), "waha", None, CFG) and not sr.applies(sec("wait"), "waha", None, CFG)
    assert not sr.applies(sec("policy"), "telegram", None, CFG) and not sr.applies(sec("policy"), "email", None, CFG)
    assert not sr.applies(sec("policy"), "waha", {"rule": "direct"}, CFG)
    assert not sr.applies(sec("policy"), "waha", None, {**CFG, "enabled": False})


def test_intro_only_promises_what_is_possible():
    with_cal = sr.intro_instruction(calendar=True)
    without = sr.intro_instruction(calendar=False)
    assert "Zugriff auf Bahrians Kalender" in with_cal and "KEINEN Zugriff" not in with_cal
    assert "KEINEN Zugriff auf Bahrians Kalender" in without and "keine Terminauskünfte" in without
    assert "KI-Assistent" in with_cal and "noch nicht gemeldet" in with_cal


# ─── Antworten auf Rückfragen von ASTRA ───────────────────────────────────────
def test_awaiting_answer_detects_a_question_from_asta():
    assert sr.awaiting_answer([{"role": "user", "content": "x"}, {"role": "assistant", "content": "Meinst du Samstag?"},
                               {"role": "user", "content": "ja"}])
    assert sr.awaiting_answer([{"role": "assistant", "content": "Passt 15 Uhr? 🙂"}])
    assert not sr.awaiting_answer([{"role": "assistant", "content": "Ich richte es aus."}, {"role": "user", "content": "ok"}])
    assert not sr.awaiting_answer([]) and not sr.awaiting_answer([{"role": "user", "content": "hi?"}])


def test_short_answers_to_a_question_from_asta_are_answered_not_ignored():
    active = {"smart_until": NOW + 600}
    for kind in (sr.THANKS, sr.CHAT, sr.PING):
        assert _decide(kind, state="answered", meta=active).action == "ignore"
        v = sr.decide(kind, smart=True, ignore_noise=True, state="answered", meta=active, now=NOW, awaiting=True)
        assert v.action == "reply" and v.reason == "smart-answer", kind
    # nur im laufenden Gespräch — ein altes „?“ von gestern zählt nicht
    stale = sr.decide(sr.THANKS, smart=True, ignore_noise=True, state="answered", meta={"smart_until": NOW - 1}, now=NOW,
                      awaiting=True)
    assert stale.action == "ignore"
    # Emojis bleiben Rauschen, auch als Antwort
    assert sr.decide(sr.NOISE, smart=True, ignore_noise=True, state="answered", meta=active, now=NOW, awaiting=True).action == "ignore"
