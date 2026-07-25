"""Effizientes Gedächtnis: der Kern ist der Relevanz-Scorer — er entscheidet, was
in den Prompt kommt und was nicht. Genau das macht ihn testbar ohne Datenbank."""
from __future__ import annotations

import pytest

from app import knowledge
from app.knowledge import Fact, score_facts


def _facts() -> list[Fact]:
    return [
        Fact(kind="alias", subject="quantum room", value="Schlafzimmer"),
        Fact(kind="pref", subject="Wecker", value="6:40 werktags"),
        Fact(kind="bio", subject="", value="Fährt mit der Bahn zur Schule"),
        Fact(kind="relation", subject="Klavierlehrerin", value="donnerstags 17 Uhr"),
        Fact(kind="place", subject="Astroclub", value="trifft sich in Hamburg"),
    ]


def test_scorer_surfaces_the_relevant_fact_and_drops_the_rest():
    chosen = score_facts(_facts(), "mach im quantum room das licht an", limit=3)
    lines = [f.line() for f in chosen]
    assert any("Schlafzimmer" in ln for ln in lines)
    assert not any("Wecker" in ln for ln in lines)      # nichts mit Wecker gefragt
    assert not any("Klavier" in ln for ln in lines)


def test_scorer_respects_the_limit():
    assert len(score_facts(_facts(), "quantum room wecker bahn", limit=2)) == 2


def test_always_on_facts_are_pinned_even_without_overlap():
    facts = _facts() + [Fact(kind="bio", subject="Name", value="Bahrian, 16", always_on=True)]
    chosen = score_facts(facts, "völlig unbezogene frage über wetter", limit=5)
    assert any("Bahrian" in f.line() for f in chosen)


def test_no_overlap_yields_only_pinned():
    assert score_facts(_facts(), "xyzzy plugh", limit=5) == []


def test_scorer_is_umlaut_insensitive():
    facts = [Fact(kind="place", subject="Küche", value="Kaffeemaschine steht links")]
    assert score_facts(facts, "was steht in der kueche", limit=3)


def test_fact_line_is_compact_not_prose():
    assert Fact(kind="alias", subject="quantum room", value="Schlafzimmer").line() \
        == "[alias] quantum room: Schlafzimmer"
    assert Fact(kind="bio", subject="", value="mag Robotik").line() == "[bio] mag Robotik"


# ─── Markdown bullets werden zu Kandidaten (bestehende facts.md geht nicht verloren) ─
def test_bullets_parse_into_facts(monkeypatch):
    sample = (
        "# Fakten\n\n"
        "- Name: Bahrian, 16\n"
        "- **Schule:** Gymnasium XY\n"
        "- (2026-07-25) mag Robotik\n"
        "- _Beispiel:_ ignorier mich\n"
    )
    monkeypatch.setattr(knowledge, "read_file", lambda rel: sample)
    facts = knowledge._bullets_as_facts("facts.md", "bio")
    pairs = {(f.subject, f.value) for f in facts}
    assert ("Name", "Bahrian, 16") in pairs
    assert ("Schule", "Gymnasium XY") in pairs
    assert ("", "mag Robotik") in pairs                 # Datumsstempel entfernt
    assert not any("ignorier" in f.value for f in facts)  # Template-Zeile übersprungen
