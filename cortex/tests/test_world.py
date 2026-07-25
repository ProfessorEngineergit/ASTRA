"""Weltmodell-Resolver: echte Transkriptions- und Sprachfehler → richtiger Raum.

Die Tabelle unten ist der eigentliche Test dieses Features. Sie ist bewusst aus
Fehlern gebaut, die Whisper und Tippen tatsächlich produzieren (verschluckte
Endung, vertauschter Anlaut, getrennte Wortfuge, ae-statt-ä), plus den beiden
Fällen, die NICHT raten dürfen: mehrdeutig und unbekannt.
"""
from __future__ import annotations

import pytest

from app import world


def _home() -> list[world.Node]:
    """Ein realistisch geschnittenes Zuhause: mehrere *zimmer (Kollisionsgefahr!)."""
    def area(name: str) -> world.Node:
        return world.Node(id=f"area:{name}", kind="area", area=name,
                          names=(name,), source="home_assistant")

    def sensor(entity: str, name: str, room: str, device_class: str) -> world.Node:
        return world.Node(id=entity, kind="sensor", area=room, names=(name,),
                          caps=(f"class:{device_class}",), source="home_assistant")

    def light(entity: str, name: str, room: str) -> world.Node:
        return world.Node(id=entity, kind="light", area=room, names=(name,),
                          caps=("turn_on", "turn_off", "toggle"), source="home_assistant")

    return [
        area("Wohnzimmer"), area("Schlafzimmer"), area("Kinderzimmer"),
        area("Küche"), area("Bad"), area("Arbeitszimmer"),
        sensor("sensor.wohnzimmer_temperatur", "Wohnzimmer Temperatur", "Wohnzimmer", "temperature"),
        sensor("sensor.wohnzimmer_luftfeuchte", "Wohnzimmer Luftfeuchte", "Wohnzimmer", "humidity"),
        sensor("sensor.schlafzimmer_temperatur", "Schlafzimmer Temperatur", "Schlafzimmer", "temperature"),
        sensor("sensor.kueche_temperatur", "Küche Temperatur", "Küche", "temperature"),
        sensor("sensor.bad_temperatur", "Bad Temperatur", "Bad", "temperature"),
        light("light.wohnzimmer_decke", "Wohnzimmer Decke", "Wohnzimmer"),
        light("light.wohnzimmer_stehlampe", "Stehlampe", "Wohnzimmer"),
        light("light.kueche_spots", "Küche Spots", "Küche"),
        world.Node(id="climate.bad", kind="climate", area="Bad", names=("Bad Heizung",),
                   caps=("set_temperature",), source="home_assistant"),
        world.Node(id="media_player.schlafzimmer", kind="media_player", area="Schlafzimmer",
                   names=("Schlafzimmer Lautsprecher",), caps=("speak", "play"),
                   source="home_assistant"),
    ]


# ─── Der Kern: fehlerhafte Eingabe → trotzdem der richtige Raum ────────────────
@pytest.mark.parametrize("spoken,expected", [
    ("Wohnzimmer", "Wohnzimmer"),          # sauber
    ("wohnzimmer", "Wohnzimmer"),          # Kleinschreibung
    ("Wohnzimma", "Wohnzimmer"),           # verschluckte Endung (Whisper, Dialekt)
    ("Bohnzimmer", "Wohnzimmer"),          # vertauschter Anlaut
    ("wohn zimmer", "Wohnzimmer"),         # getrennte Wortfuge
    ("im Wohnzimmer", "Wohnzimmer"),       # Füllwort davor
    ("Küche", "Küche"),                    # Umlaut
    ("Kueche", "Küche"),                   # ae-Schreibweise
    ("kuche", "Küche"),                    # Umlaut ganz verschluckt
    ("Schlafzimma", "Schlafzimmer"),
    ("Arbeitszimmer", "Arbeitszimmer"),
    ("Bad", "Bad"),
])
def test_room_resolves_despite_transcription_noise(spoken, expected):
    res = world.resolve_in(_home(), spoken, kinds=("area",))
    assert res.status == "unique", f"{spoken!r} → {res.status} ({res.method})"
    assert res.node is not None
    assert res.node.area == expected


def test_bare_zimmer_is_ambiguous_and_offers_candidates():
    """„Zimmer" passt auf vier Räume — hier MUSS zurückgefragt werden, nicht geraten."""
    res = world.resolve_in(_home(), "Zimmer", kinds=("area",))
    assert res.status == "ambiguous"
    names = {n.area for n in res.candidates}
    assert {"Wohnzimmer", "Schlafzimmer", "Kinderzimmer"} <= names
    assert res.node is None


def test_unknown_room_returns_none_not_a_wrong_guess():
    """Ein Raum, den es nicht gibt, darf NIE auf einen anderen fallen."""
    res = world.resolve_in(_home(), "Garage", kinds=("area",))
    assert res.status == "none"
    assert res.node is None


def test_short_phonetic_codes_do_not_collide():
    """Regression: 'Garage' (474) und 'Küche' (44) galten kurzzeitig als Treffer."""
    score, _method = world._score_pair("garage", "kueche")
    assert score == 0.0


# ─── Aliasse ──────────────────────────────────────────────────────────────────
def test_alias_wins_even_though_it_contains_a_filler_word():
    """„mein Zimmer" trägt ein Füllwort — die Variante MIT Füllwörtern muss greifen."""
    nodes = world.apply_aliases(_home(), {"Schlafzimmer": ["mein zimmer", "oben"]})
    res = world.resolve_in(nodes, "mein Zimmer", kinds=("area",))
    assert res.status == "unique"
    assert res.node.area == "Schlafzimmer"


def test_alias_short_word():
    nodes = world.apply_aliases(_home(), {"Wohnzimmer": ["unten", "couch"]})
    res = world.resolve_in(nodes, "unten", kinds=("area",))
    assert res.status == "unique"
    assert res.node.area == "Wohnzimmer"


def test_aliases_do_not_replace_the_real_name():
    nodes = world.apply_aliases(_home(), {"Schlafzimmer": ["mein zimmer"]})
    res = world.resolve_in(nodes, "Schlafzimmer", kinds=("area",))
    assert res.status == "unique"
    assert res.node.area == "Schlafzimmer"


def test_apply_aliases_ignores_unknown_targets():
    nodes = world.apply_aliases(_home(), {"Dachboden": ["oben"]})
    assert world.resolve_in(nodes, "oben", kinds=("area",)).status == "none"


def test_multiword_alias_does_not_make_ambiguous_queries_unique():
    """Regression: der Alias „mein zimmer" erzeugte den Schlüssel „zimmer",
    wodurch die absichtlich mehrdeutige Frage nach dem „Zimmer" exakt traf."""
    nodes = world.apply_aliases(_home(), {"Schlafzimmer": ["mein zimmer"]})
    assert world.resolve_in(nodes, "Zimmer", kinds=("area",)).status == "ambiguous"


# ─── Absicht → Knotenart ──────────────────────────────────────────────────────
@pytest.mark.parametrize("what,kinds,device_class", [
    ("Temperatur", ("sensor", "climate"), "temperature"),
    ("wie warm", ("sensor", "climate"), "temperature"),
    ("Luftfeuchte", ("sensor",), "humidity"),
    ("Licht", ("light",), ""),
    ("Lampe", ("light",), ""),
    ("Heizung", ("climate",), ""),
    ("Rollo", ("cover",), ""),
    ("Lautsprecher", ("media_player",), ""),
])
def test_intent_filter_maps_everyday_words(what, kinds, device_class):
    assert world.intent_filter(what) == (kinds, device_class)


def test_intent_filter_passes_unknown_words_through():
    assert world.intent_filter("Quantenfluktuation") == (None, "")
    assert world.intent_filter("") == (None, "")


# ─── Filter ───────────────────────────────────────────────────────────────────
def test_the_actual_failing_case_end_to_end():
    """»Temperatur im Wohnzimmer« — getrennt in what/where, mit Tippfehler im Raum."""
    nodes = _home()
    area = world.resolve_in(nodes, "Wohnzimma", kinds=("area",))
    assert area.status == "unique"
    kinds, device_class = world.intent_filter("Temperatur")
    hits = world.filter_nodes(nodes, kinds=kinds, device_class=device_class,
                              area=area.node.area)
    assert [n.id for n in hits] == ["sensor.wohnzimmer_temperatur"]


def test_filter_by_area_is_umlaut_insensitive():
    hits = world.filter_nodes(_home(), kinds=("light",), area="Kueche")
    assert [n.id for n in hits] == ["light.kueche_spots"]


def test_area_with_multiple_lights_returns_all():
    """Mehrere Lampen im Raum sind keine Rückfrage — die schaltet man gemeinsam."""
    hits = world.filter_nodes(_home(), kinds=("light",), area="Wohnzimmer")
    assert len(hits) == 2


def test_areas_lists_every_room_sorted():
    assert world.areas(_home()) == [
        "Arbeitszimmer", "Bad", "Kinderzimmer", "Küche", "Schlafzimmer", "Wohnzimmer",
    ]


def test_resolve_in_on_empty_world_is_none_not_a_crash():
    res = world.resolve_in([], "Wohnzimmer", kinds=("area",))
    assert res.status == "none"


# ─── Normalisierung & Phonetik ────────────────────────────────────────────────
@pytest.mark.parametrize("raw,folded", [
    ("Küche", "kueche"),
    ("Straße", "strasse"),
    ("Wohnzimmer!", "wohnzimmer"),
    ("  Bad   Oben ", "bad oben"),
    ("Café", "cafe"),
])
def test_fold_normalizes_umlauts_and_punctuation(raw, folded):
    assert world.fold(raw) == folded


def test_variants_cover_filler_and_word_seam():
    got = world.variants("im mein Wohn Zimmer")
    assert "wohnzimmer" in got            # Füllwörter weg + Fuge geschlossen
    assert "im mein wohn zimmer" in got   # Originalform bleibt (für Aliasse)


def test_cologne_phonetic_is_stable_for_a_known_word():
    assert world.cologne_phonetic("Wohnzimmer") == "36867"
    assert world.cologne_phonetic("") == ""


# ─── Prompt-Ausschnitt ────────────────────────────────────────────────────────
def test_digest_lists_rooms_with_device_kinds_but_no_entity_ids():
    text = world.digest(_home())
    assert "Wohnzimmer:" in text
    assert "Temperatur" in text and "Licht" in text
    # Der Digest ist ein Überblick — entity_ids gehören nicht in den Kontext.
    assert "sensor.wohnzimmer_temperatur" not in text


def test_digest_of_empty_world_is_empty_string():
    assert world.digest([]) == ""
