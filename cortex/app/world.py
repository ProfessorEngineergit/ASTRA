"""Weltmodell — Register adressierbarer Dinge + toleranter Resolver.

Plugins sprechen in Ids (`entity_id`, `light_id`, Containername), Bahrian spricht
in Räumen und Spitznamen — und eine Sprachnachricht legt Transkriptionsrauschen
darüber („Wohnzimma", „Bohnzimmer"). Dieses Modul ist die Schicht dazwischen:
jedes Plugin liefert über den `world_nodes()`-Hook Knoten, `resolve()` bildet eine
freie Formulierung darauf ab — in vier zunehmend gutmütigen Stufen.

Hier wird nicht selbst mit dem Netz geredet: die Provider holen die Daten und
geben schlichte `Node`-Objekte zurück; dieses Modul cached (TTL) und matcht.
Ohne konfigurierten Provider bleibt das Register leer und jeder Nutzer wird zum
No-op — wie jede andere Fähigkeit in ASTRA.

Der Resolver liefert bewusst DREI Ergebnisse statt Treffer/Fehler:
`unique` → handeln · `ambiguous` → Kandidaten, damit das Modell EINE kurze
Rückfrage stellt · `none` → sagen, was es tatsächlich gibt.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace

log = logging.getLogger("astra.world")

# Wie lange die Topologie (Räume/Geräte) gecacht wird. Zustände werden davon
# nicht berührt — die holen die Provider pro aufgelöster Entität frisch.
TTL_SECONDS = 600.0

# Füllwörter, die in „mach im Wohnzimmer mal das Licht an" keinen Ort benennen.
# Achtung: Varianten werden MIT und OHNE Füllwörter gebildet, weil Aliasse wie
# „mein Zimmer" selbst ein Füllwort tragen.
_FILLER = frozenset({
    "im", "in", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
    "bei", "beim", "auf", "an", "am", "vom", "von", "zum", "zur", "nach", "ins",
    "mein", "meine", "meinem", "meiner", "meins", "unser", "unsere",
    "mal", "bitte", "doch", "noch", "jetzt", "hier", "da", "dort",
    "ist", "sind", "es", "wie", "was", "wo", "the", "a", "of", "my",
})

# Deutsche Umlaut-Faltung: „Küche" und „Kueche" müssen dieselbe Zeichenkette
# ergeben. casefold() macht aus ß schon ss, die Einträge sind Absicherung.
_UMLAUT = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})

# Schwellen. Absichtlich konservativ: eine falsche Zuordnung („Licht an" im
# falschen Raum) ist teurer als eine kurze Rückfrage.
_SUBSTRING_BASE = 0.82
_FUZZY_CUTOFF = 0.72
# Phonetik ist die letzte Stufe und muss streng sein: kurze Codes kollidieren
# zufällig (》Garage《 → 474 und 》Küche《 → 44 ergäben sonst einen Treffer). Darum
# beide Codes mindestens 4 Zeichen UND ein hoher Schwellwert.
_PHONETIC_CUTOFF = 0.82
_PHONETIC_MIN_LEN = 4
_MIN_SCORE = 0.62
_AMBIGUOUS_MARGIN = 0.10


# ─── Normalisierung ───────────────────────────────────────────────────────────
def fold(text: str) -> str:
    """Auf eine vergleichbare Form bringen: klein, Umlaute gefaltet, akzentfrei."""
    s = (text or "").casefold().translate(_UMLAUT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def variants(text: str, *, strip_filler: bool = True) -> list[str]:
    """Vergleichsvarianten einer Eingabe.

    Formen, die je einen echten Fehlerfall abdecken: mit Füllwörtern (Aliasse wie
    „mein zimmer"), ohne Füllwörter („im wohnzimmer" → „wohnzimmer"), und je
    zusätzlich ohne Wortfugen („wohn zimmer" → „wohnzimmer", ein häufiges
    Transkriptions-Artefakt).

    `strip_filler=False` für SCHLÜSSEL von Knoten: dort darf nicht gekürzt werden.
    Sonst erzeugt der Alias „mein zimmer" den Schlüssel „zimmer" und die bewusst
    mehrdeutige Frage nach dem „Zimmer" träfe plötzlich exakt einen Raum.
    """
    folded = fold(text)
    if not folded:
        return []
    tokens = folded.split()
    forms = [" ".join(tokens), "".join(tokens)]
    if strip_filler:
        lean = [t for t in tokens if t not in _FILLER] or tokens
        forms += [" ".join(lean), "".join(lean)]
    out: list[str] = []
    for form in forms:
        if form and form not in out:
            out.append(form)
    return out


def cologne_phonetic(text: str) -> str:
    """Kölner Phonetik — deutschsprachiges Pendant zu Soundex.

    Letzte Rettung für Transkriptionsfehler, die difflib nicht mehr fängt.
    Codes werden NICHT auf Gleichheit verglichen (unterschiedlich lange Wörter
    ergeben unterschiedlich lange Codes: „Wohnzimma" → 3686 vs „Wohnzimmer" →
    36867), sondern per Ähnlichkeitsmaß — siehe `_score_pair`.
    """
    w = re.sub(r"[^a-z]", "", fold(text))
    if not w:
        return ""
    codes: list[str] = []
    for i, ch in enumerate(w):
        prev = w[i - 1] if i else ""
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in "aeijouy":
            code = "0"
        elif ch == "h":
            continue                      # H bekommt keinen Code
        elif ch == "b":
            code = "1"
        elif ch == "p":
            code = "3" if nxt == "h" else "1"
        elif ch in "dt":
            code = "8" if nxt in "csz" else "2"
        elif ch in "fvw":
            code = "3"
        elif ch in "gkq":
            code = "4"
        elif ch == "c":
            if not i:
                code = "4" if nxt in "ahkloqrux" else "8"
            elif prev in "sz":
                code = "8"
            else:
                code = "4" if nxt in "ahkoqux" else "8"
        elif ch == "x":
            code = "8" if prev in "ckq" else "48"
        elif ch == "l":
            code = "5"
        elif ch in "mn":
            code = "6"
        elif ch == "r":
            code = "7"
        elif ch in "sz":
            code = "8"
        else:
            continue
        codes.append(code)
    flat = "".join(codes)
    if not flat:
        return ""
    deduped = [flat[0]]
    for c in flat[1:]:
        if c != deduped[-1]:
            deduped.append(c)
    # Nullen nur an erster Stelle behalten.
    return deduped[0] + "".join(c for c in deduped[1:] if c != "0")


# ─── Datenmodell ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Node:
    """Ein adressierbares Ding. Topologie, kein Zustand — der wird frisch geholt."""
    id: str
    kind: str                       # area | light | climate | sensor | media_player | …
    area: str = ""                  # kanonischer Raumname ("" = keinem Raum zugeordnet)
    names: tuple[str, ...] = ()     # friendly_name, Slug-Varianten, Aliasse
    caps: tuple[str, ...] = ()      # z. B. "class:temperature", "turn_on", "speak"
    source: str = ""                # Plugin-Slug

    @property
    def label(self) -> str:
        return self.names[0] if self.names else self.id

    @property
    def device_class(self) -> str:
        for c in self.caps:
            if c.startswith("class:"):
                return c[6:]
        return ""


@dataclass(frozen=True)
class Resolution:
    """Ergebnis einer Auflösung. `status` ist unique | ambiguous | none."""
    status: str
    node: Node | None = None
    candidates: tuple[Node, ...] = ()
    score: float = 0.0
    method: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "unique" and self.node is not None


# ─── Scoring ──────────────────────────────────────────────────────────────────
def _node_keys(node: Node) -> list[str]:
    """Alle Vergleichsschlüssel eines Knotens (Namen, Id-Objektteil, Raum)."""
    raw: list[str] = list(node.names)
    # Aus "sensor.wohnzimmer_temperatur" wird "wohnzimmer temperatur" mitgeführt.
    # Nur bei echten entity_ids — synthetische Ids wie "area:Wohnzimmer" würden
    # sonst den Präfix als Suchwort einschleppen.
    if "." in node.id:
        raw.append(node.id.split(".", 1)[1].replace("_", " "))
    if node.kind == "area" and node.area:
        raw.append(node.area)
    keys: list[str] = []
    for name in raw:
        for form in variants(name, strip_filler=False):
            if form not in keys:
                keys.append(form)
    return keys


def _score_pair(query: str, key: str) -> tuple[float, str]:
    """Ähnlichkeit einer Anfragevariante zu einem Knotenschlüssel, 0.0 = kein Treffer."""
    if not query or not key:
        return 0.0, ""
    if query == key:
        return 1.0, "exact"
    # Teilstring in beide Richtungen, gedämpft nach Längenverhältnis — damit
    # "zimmer" nicht so stark wirkt wie "wohnzimmer".
    shorter, longer = (query, key) if len(query) <= len(key) else (key, query)
    if len(shorter) >= 3 and shorter in longer:
        return _SUBSTRING_BASE * (len(shorter) / len(longer)) ** 0.25, "substring"
    ratio = difflib.SequenceMatcher(None, query, key).ratio()
    if ratio >= _FUZZY_CUTOFF:
        return ratio * 0.95, "fuzzy"
    pq, pk = cologne_phonetic(query), cologne_phonetic(key)
    if len(pq) >= _PHONETIC_MIN_LEN and len(pk) >= _PHONETIC_MIN_LEN:
        pratio = difflib.SequenceMatcher(None, pq, pk).ratio()
        if pratio >= _PHONETIC_CUTOFF:
            return pratio * 0.80, "phonetic"
    return 0.0, ""


def _score_node(query_variants: list[str], node: Node) -> tuple[float, str]:
    best, method = 0.0, ""
    for key in _node_keys(node):
        for qv in query_variants:
            score, how = _score_pair(qv, key)
            if score > best:
                best, method = score, how
                if best >= 1.0:
                    return best, method
    return best, method


# ─── Filter & Auflösung (pur — genau so testbar) ──────────────────────────────
def filter_nodes(
    nodes: list[Node],
    *,
    kinds: tuple[str, ...] | None = None,
    device_class: str = "",
    area: str = "",
) -> list[Node]:
    out = nodes
    if kinds:
        out = [n for n in out if n.kind in kinds]
    if device_class:
        out = [n for n in out if n.device_class == device_class]
    if area:
        target = fold(area)
        out = [n for n in out if fold(n.area) == target]
    return list(out)


def resolve_in(
    nodes: list[Node],
    query: str,
    *,
    kinds: tuple[str, ...] | None = None,
    device_class: str = "",
    area: str = "",
) -> Resolution:
    """Freien Text auf einen Knoten abbilden. Pur: kein I/O, kein Cache."""
    pool = filter_nodes(nodes, kinds=kinds, device_class=device_class, area=area)
    if not pool:
        return Resolution("none")
    qv = variants(query)
    if not qv:
        return Resolution("none", candidates=tuple(pool[:12]))

    scored: list[tuple[float, str, Node]] = []
    for node in pool:
        score, method = _score_node(qv, node)
        if score > 0.0:
            scored.append((score, method, node))
    if not scored:
        return Resolution("none", candidates=tuple(pool[:12]))

    scored.sort(key=lambda row: (-row[0], row[2].label))
    best_score, best_method, best_node = scored[0]
    if best_score < _MIN_SCORE:
        return Resolution("none", candidates=tuple(n for _s, _m, n in scored[:12]))

    close = [n for score, _m, n in scored if score >= best_score - _AMBIGUOUS_MARGIN]
    if len(close) > 1:
        return Resolution("ambiguous", candidates=tuple(close[:8]),
                          score=best_score, method=best_method)
    return Resolution("unique", node=best_node, candidates=(best_node,),
                      score=best_score, method=best_method)


def areas(nodes: list[Node]) -> list[str]:
    """Kanonische Raumnamen, alphabetisch."""
    named = {n.area for n in nodes if n.area}
    named |= {n.label for n in nodes if n.kind == "area"}
    return sorted(named)


# ─── Aliasse ──────────────────────────────────────────────────────────────────
def apply_aliases(nodes: list[Node], aliases: dict[str, list[str]]) -> list[Node]:
    """Benutzer-Aliasse an die passenden Knoten hängen ({Ziel: [Alias, …]})."""
    if not aliases:
        return nodes
    folded = {fold(target): list(vals) for target, vals in aliases.items() if target}
    out: list[Node] = []
    for node in nodes:
        extra: list[str] = []
        keys = set(_node_keys(node))
        for target, vals in folded.items():
            if target and target in keys:
                extra.extend(v for v in vals if v)
        out.append(replace(node, names=node.names + tuple(extra)) if extra else node)
    return out


# ─── Absicht → Knotenart ──────────────────────────────────────────────────────
# „Temperatur" ist keine Entität, sondern eine Absicht. Diese Tabelle übersetzt
# Alltagssprache in (Knotenarten, device_class), damit `home_state` nicht raten muss.
_INTENTS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("temperatur", "warm", "kalt", "grad", "temp"), ("sensor", "climate"), "temperature"),
    (("luftfeuchte", "luftfeuchtigkeit", "feuchtigkeit", "feucht"), ("sensor",), "humidity"),
    (("co2", "kohlendioxid", "luftqualitaet"), ("sensor",), "carbon_dioxide"),
    (("helligkeit", "lux"), ("sensor",), "illuminance"),
    (("batterie", "akku"), ("sensor",), "battery"),
    (("strom", "verbrauch", "watt", "leistung"), ("sensor",), "power"),
    (("licht", "lampe", "lampen", "leuchte", "beleuchtung"), ("light",), ""),
    (("heizung", "thermostat", "klima"), ("climate",), ""),
    (("fenster",), ("binary_sensor", "cover"), "window"),
    (("tuer", "tur", "door"), ("binary_sensor",), "door"),
    (("bewegung", "anwesenheit"), ("binary_sensor",), "motion"),
    (("rollo", "rolladen", "jalousie", "vorhang", "cover"), ("cover",), ""),
    (("steckdose", "schalter", "stecker"), ("switch",), ""),
    (("musik", "lautsprecher", "speaker", "fernseher", "tv", "sonos", "radio"),
     ("media_player",), ""),
    (("ventilator", "luefter"), ("fan",), ""),
    (("schloss", "tuerschloss"), ("lock",), ""),
    (("staubsauger", "sauger"), ("vacuum",), ""),
)


def intent_filter(what: str) -> tuple[tuple[str, ...] | None, str]:
    """Alltagswort → (Knotenarten, device_class). ((None, "") = kein Filter)."""
    needle = fold(what)
    if not needle:
        return None, ""
    for words, kinds, device_class in _INTENTS:
        if any(w in needle or needle in w for w in words):
            return kinds, device_class
    return None, ""


# ─── Prompt-Ausschnitt ────────────────────────────────────────────────────────
_KIND_LABELS = {
    "light": "Licht", "climate": "Heizung", "sensor": "Sensor",
    "binary_sensor": "Kontakt", "switch": "Schalter", "cover": "Rollo",
    "media_player": "Medien", "fan": "Ventilator", "lock": "Schloss",
    "vacuum": "Sauger", "device_tracker": "Gerät", "person": "Person",
    "camera": "Kamera", "scene": "Szene", "humidifier": "Luftfeuchter",
}
_CLASS_LABELS = {
    "temperature": "Temperatur", "humidity": "Luftfeuchte", "battery": "Batterie",
    "power": "Strom", "illuminance": "Helligkeit", "motion": "Bewegung",
    "window": "Fenster", "door": "Tür", "carbon_dioxide": "CO₂",
}


def thing_label(node: Node) -> str:
    """Kurzes deutsches Etikett („Temperatur", „Licht") für Digest und Rückfragen."""
    dc = node.device_class
    if dc and dc in _CLASS_LABELS:
        return _CLASS_LABELS[dc]
    return _KIND_LABELS.get(node.kind, node.kind)


def digest(nodes: list[Node], *, max_areas: int = 24, max_kinds: int = 7) -> str:
    """Kompakter Weltausschnitt für den System-Prompt.

    Absichtlich Zählwerte statt Entitätslisten: das Modell soll wissen, WAS es
    wo gibt, und dann per Tool nachfragen — nicht 400 entity_ids im Kontext haben.
    """
    if not nodes:
        return ""
    by_area: dict[str, dict[str, int]] = {}
    homeless = 0
    for node in nodes:
        if node.kind == "area":
            by_area.setdefault(node.label, {})
            continue
        if not node.area:
            homeless += 1
            continue
        bucket = by_area.setdefault(node.area, {})
        label = thing_label(node)
        bucket[label] = bucket.get(label, 0) + 1

    if not by_area:
        return ""
    lines = ["Bekannte Räume und Geräte (Weltmodell, live):"]
    for area in sorted(by_area)[:max_areas]:
        things = by_area[area]
        if things:
            top = sorted(things.items(), key=lambda kv: (-kv[1], kv[0]))[:max_kinds]
            detail = ", ".join(f"{name}({count})" if count > 1 else name
                               for name, count in top)
        else:
            detail = "keine Geräte zugeordnet"
        lines.append(f"- {area}: {detail}")
    if len(by_area) > max_areas:
        lines.append(f"- … und {len(by_area) - max_areas} weitere Räume")
    if homeless:
        lines.append(f"- ohne Raumzuordnung: {homeless} Geräte")
    lines.append(
        "Frag Zustände mit home_state (what=Absicht, where=freier Raumtext) ab und "
        "schalte mit home_control. Tippfehler, Umlaute und Spitznamen werden toleriert; "
        "bei mehreren Treffern kommen Kandidaten zurück — stell dann EINE kurze Rückfrage."
    )
    return "\n".join(lines)


# ─── Register (Cache + Provider-Aggregation) ──────────────────────────────────
_nodes: list[Node] = []
_fetched_at: float = 0.0
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def invalidate() -> None:
    """Cache verwerfen — nach Plugin-Rebuild oder Alias-Änderung."""
    global _nodes, _fetched_at
    _nodes, _fetched_at = [], 0.0


async def snapshot(*, force: bool = False) -> list[Node]:
    """Aktuelles Register. Fragt die Plugin-Provider höchstens alle TTL Sekunden."""
    global _nodes, _fetched_at
    now = time.monotonic()
    if not force and _nodes and now - _fetched_at < TTL_SECONDS:
        return _nodes
    async with _get_lock():
        now = time.monotonic()
        if not force and _nodes and now - _fetched_at < TTL_SECONDS:
            return _nodes
        # Lokaler Import: registry lädt die builtin-Plugins, die ihrerseits dieses
        # Modul importieren — auf Modulebene wäre das ein Zyklus.
        from .plugins.registry import get_manager
        try:
            fetched = await get_manager().world_nodes()
        except Exception:  # noqa: BLE001 — ein Provider darf nie den Resolver kippen
            log.warning("Weltmodell konnte nicht aufgebaut werden.", exc_info=True)
            fetched = []
        try:
            from . import knowledge
            merged = dict(knowledge.world_aliases())          # manual markdown overrides
            for target, spoken in (await knowledge.world_aliases_db()).items():
                merged.setdefault(target, []).extend(spoken)  # aliases ASTRA learned
            fetched = apply_aliases(list(fetched), merged)
        except Exception:  # noqa: BLE001
            log.debug("Aliasse konnten nicht angewendet werden.", exc_info=True)
            fetched = list(fetched)
        _nodes, _fetched_at = fetched, time.monotonic()
        log.info("Weltmodell: %d Knoten, %d Räume.", len(_nodes), len(areas(_nodes)))
        return _nodes


async def resolve(
    query: str,
    *,
    kinds: tuple[str, ...] | None = None,
    device_class: str = "",
    area: str = "",
) -> Resolution:
    return resolve_in(await snapshot(), query,
                      kinds=kinds, device_class=device_class, area=area)


async def resolve_area(query: str) -> Resolution:
    return await resolve(query, kinds=("area",))


async def prompt_digest() -> str:
    """Weltausschnitt für den System-Prompt. Fehlertolerant — nie den Chat blockieren."""
    try:
        return digest(await snapshot())
    except Exception:  # noqa: BLE001
        log.debug("Weltausschnitt konnte nicht gebaut werden.", exc_info=True)
        return ""
