"""Durable, human-editable knowledge — the 'Cloud over Markdown files'.

Files live in `BRAIN_DATA_DIR` (a Docker volume, NOT in git), so they survive
`git pull` / image rebuilds and can be edited live (server, dashboard, or by ASTRA
itself via the `remember_fact` tool). On first start the directory is seeded with
starter templates; existing files are never overwritten.

Layout:
    {BRAIN_DATA_DIR}/
        persona.md    — tone/voice overrides appended to the base persona
        facts.md      — durable facts about the owner (private — OWNER register only)
        routines.md   — recurring schedule (school, clubs, lessons, transit habits)
        people.md     — notes on recurring contacts (relationships, do/don't share)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import get_settings

log = logging.getLogger("astra.knowledge")

# Always-on core injected into every owner prompt — small and genuinely always
# relevant. facts.md / people.md are NO LONGER dumped here; their bullets are
# retrieved on demand by relevant_facts() so the prompt stays cheap.
_CORE_FILES = ("persona.md", "routines.md")
# Historical set (kept for reference / any external caller).
_PRIVATE = ("persona.md", "facts.md", "routines.md", "people.md")

_SEED: dict[str, str] = {
    "persona.md": """\
# Persona-Feinschliff (editierbar)

Ergänzungen zu ASTRAs Grundton. Schreib hier rein, wie ASTRA *mit dir* klingen soll.

- Sprich mich mit „du" an.
- Morgens kurz & energiegeladen, abends ruhiger.
- Technische Themen ruhig tief — ich bin Maker/Engineer.
""",
    "facts.md": """\
# Fakten über mich (privat — nur ASTRA↔ich)

ASTRA darf diese Fakten nutzen, aber NICHT an Dritte weitergeben, außer die
Trust-Tier/Policy erlaubt es ausdrücklich.

- Name: Bahrian, 16.
- (Trag hier feste Fakten ein: Schule, Stundenplan-Eigenheiten, Allergien, Adressen …)
""",
    "routines.md": """\
# Routinen & Wochenrhythmus (editierbar)

ASTRA nutzt das, um Tage zu planen, Konflikte zu erkennen und proaktiv zu helfen.

- **Schultage:** morgens mit der Bahn zur Schule, nachmittags zurück.
- **Samstag:** Matheclub.
- **Großeltern & Tante:** wohnen nahe der Schule — gelegentliche Besuche.
- **Arbeit:** Aufgaben kommen über **Slack**; viel **E-Mail**.
- **Astroclub:** sehr viele **Signal**-Nachrichten.
- **Klavierunterricht:** wöchentlich (Zeit variabel — kann sich mit Astroclub überschneiden).
- Bei Terminkonflikt (z. B. Klavier vorgezogen → kollidiert mit Astroclub):
  Kalender aktualisieren, mich informieren, relevante Personen benachrichtigen.
""",
    "people.md": """\
# Personen-Notizen (editierbar)

Pro Person: Beziehung, Trust-Tier-Hinweis, was geteilt werden darf / nicht.

- _Beispiel:_ **Mutter** — Tier 1. Darf wissen, wann ich nach Hause komme.
- _Beispiel:_ **Klavierlehrerin (WhatsApp)** — Terminänderungen in Kalender übernehmen.
""",
    "world_aliases.md": """\
# Aliasse für Räume & Geräte (editierbar)

Wie DU deine Räume und Geräte nennst. ASTRA schlägt hier nach, bevor es einen
Raum sucht — damit „mein Zimmer" oder „unten" trifft, auch wenn Home Assistant
den Raum anders nennt. Links steht der echte Name, rechts deine Wörter dafür.

Umlaute, Tippfehler und verschluckte Endungen muss du hier NICHT eintragen —
die verzeiht der Resolver von sich aus („Wohnzimma" findet das Wohnzimmer).

<!-- astra:aliases
wohnzimmer: wohnstube, unten, couch
schlafzimmer: mein zimmer, oben, bett
kueche: kochen, essen
-->

Trag deine eigenen Zeilen in den Block oben ein (oder sag ASTRA einfach
„mein Zimmer ist das Schlafzimmer" — dann schreibt es das selbst hier rein).
""",
}


def _dir() -> Path:
    return Path(get_settings().brain_data_dir)


def principal_dir(principal: str = "") -> Path:
    """Brain-data root for a principal. The default owner keeps the historical
    /srv/data (nothing moves); additional principals live under /srv/data/principals/<key>."""
    base = _dir()
    if not principal:
        return base
    safe = re.sub(r"[^a-z0-9_-]+", "_", principal.lower()).strip("_") or "unknown"
    return base / "principals" / safe


def ensure_seeded() -> None:
    """Create the data dir and write starter templates for any missing file."""
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        for name, content in _SEED.items():
            f = d / name
            if not f.exists():
                f.write_text(content, encoding="utf-8")
                log.info("Seeded knowledge file %s", f)
    except Exception as e:  # noqa: BLE001 — never block boot
        log.warning("knowledge.ensure_seeded failed: %s", e)


def _read(name: str) -> str:
    try:
        f = _dir() / name
        return f.read_text(encoding="utf-8").strip() if f.exists() else ""
    except Exception as e:  # noqa: BLE001
        log.warning("knowledge read %s failed: %s", name, e)
        return ""


def owner_context() -> str:
    """Always-on core for the OWNER register: persona + routines + a one-line people
    index. The long tail (facts.md bullets, per-person notes) is pulled per turn by
    relevant_facts() instead of dumped here — that is the efficiency win."""
    parts = [c for name in _CORE_FILES if (c := _read(name))]
    idx = people_index()
    if idx:
        parts.append(idx + "\n(Mit astra_brain_read/-write kannst du sie lesen & pflegen.)")
    return "\n\n".join(parts).strip()


def append_fact(text: str, *, file: str = "facts.md") -> bool:
    """Append a dated bullet to a knowledge file (used by the remember_fact tool)."""
    if file not in _SEED:
        file = "facts.md"
    try:
        ensure_seeded()
        f = _dir() / file
        stamp = datetime.now().strftime("%Y-%m-%d")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"\n- ({stamp}) {text.strip()}")
        log.info("Appended fact to %s", f)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("append_fact failed: %s", e)
        return False


# ─── Brain files: live-editable markdown (owner + per person) ───────────────────
# Display title + tag for the core files; person files live under people/<slug>.md.
_TITLES: dict[str, tuple[str, str]] = {
    "facts.md": ("Über mich", "über mich"),
    "persona.md": ("Persona & Ton", "persona"),
    "routines.md": ("Routinen", "routinen"),
    "people.md": ("Personen – Übersicht", "personen"),
    "world_aliases.md": ("Räume & Geräte – Aliasse", "zuhause"),
}
_PEOPLE_DIR = "people"


def _safe_path(rel: str) -> Path | None:
    """Resolve a relative brain path safely (only *.md inside BRAIN_DATA_DIR)."""
    rel = (rel or "").strip().lstrip("/")
    if not rel.endswith(".md") or ".." in rel or not re.fullmatch(r"[A-Za-z0-9 _\-./]+", rel):
        return None
    base = _dir().resolve()
    p = (base / rel).resolve()
    return p if str(p) == str(base) or str(p).startswith(str(base) + "/") else None


def _preview(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#-* ").strip()
        if s:
            return s[:120]
    return ""


def list_files() -> list[dict]:
    """All brain files with metadata (for the admin UI + agent index)."""
    ensure_seeded()
    base = _dir().resolve()
    out: list[dict] = []
    try:
        for f in sorted(base.rglob("*.md")):
            rel = str(f.relative_to(base))
            try:
                txt = f.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            if rel.startswith(f"{_PEOPLE_DIR}/"):
                title, tag = f.stem.replace("_", " ").title(), "person"
            else:
                title, tag = _TITLES.get(rel, (f.stem.replace("_", " ").title(), "sonstiges"))
            out.append({
                "rel": rel, "title": title, "tag": tag, "preview": _preview(txt),
                "size": len(txt), "lines": txt.count("\n") + 1,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d.%m %H:%M"),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("knowledge.list_files failed: %s", e)
    # Core files first (canonical order), then people alphabetically.
    order = list(_TITLES)
    out.sort(key=lambda e: (e["rel"] not in order, order.index(e["rel"]) if e["rel"] in order else 0, e["rel"]))
    return out


def read_file(rel: str) -> str:
    p = _safe_path(rel)
    if not p or not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("knowledge.read_file %s failed: %s", rel, e)
        return ""


def write_file(rel: str, content: str) -> bool:
    p = _safe_path(rel)
    if not p:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        log.info("Brain file written: %s (%d chars)", rel, len(content))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("knowledge.write_file %s failed: %s", rel, e)
        return False


def _person_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def create_person(name: str) -> str | None:
    """Create people/<slug>.md from a template; returns the relative path."""
    slug = _person_slug(name)
    if not slug:
        return None
    rel = f"{_PEOPLE_DIR}/{slug}.md"
    p = _safe_path(rel)
    if not p:
        return None
    if not p.exists():
        write_file(rel, f"""# {name.strip()}

- **Beziehung:**
- **Trust-Tier:** (0 = ich · 1 = eng · 2 = bekannt · 3 = fremd)
- **Ton:** (wie ASTRA mit dieser Person reden soll — z.B. „locker, viel Insider-Humor")

<!-- astra:handles
whatsapp:
signal:
telegram:
email:
phone:
-->

## Darf wissen / teilen


## Nicht teilen


## Notizen & Umgangston-Beispiele
""")
    return rel


# ─── Per-person matching, tone & structured profiles ───────────────────────────
# A machine-readable handle block inside each person file lets ASTRA match an
# inbound sender (phone/JID/e-mail) to the right profile and pull the number back
# out when Bahrian says "schick X eine WhatsApp".
_HANDLE_BLOCK = re.compile(r"<!--\s*astra:handles(.*?)-->", re.DOTALL | re.IGNORECASE)
_TONE_LINE = re.compile(
    r"^\s*[-*]?\s*\*{0,2}ton\*{0,2}\s*:\s*\*{0,2}\s*(.+?)\s*\*{0,2}\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HANDLE_ALIASES = {
    "whatsapp": "waha", "wa": "waha", "waha": "waha",
    "signal": "signal", "telegram": "telegram", "tg": "telegram",
    "email": "email", "mail": "email", "e-mail": "email",
    "phone": "phone", "telefon": "phone", "tel": "phone", "handy": "phone", "nummer": "phone",
}
# Which stored handle kinds a given inbound channel may match against.
_CHANNEL_KEYS = {
    "waha": ("waha", "phone"),
    "signal": ("signal", "phone"),
    "telegram": ("telegram", "phone"),
    "email": ("email",),
    "slack": ("slack",),
}


def _norm_handle(kind: str, raw: str) -> str:
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if kind == "email":
        return raw
    # phone-like: drop a JID suffix (…@c.us) and keep digits only.
    raw = raw.split("@", 1)[0]
    digits = re.sub(r"\D", "", raw)
    return digits or raw


def parse_person_handles(text: str) -> dict[str, list[str]]:
    """Return {canonical_channel: [values]} from a file's astra:handles block."""
    out: dict[str, list[str]] = {}
    m = _HANDLE_BLOCK.search(text or "")
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        ch = _HANDLE_ALIASES.get(k.strip().lower())
        v = v.strip()
        if ch and v:
            out.setdefault(ch, []).append(v)
    return out


def person_tone(text: str) -> str:
    m = _TONE_LINE.search(text or "")
    tone = m.group(1).strip() if m else ""
    # Ignore the seed placeholder.
    return "" if tone.startswith("(") else tone


def person_file_for(channel: str, handle: str) -> dict | None:
    """Find the person file whose handles match this inbound sender, or None."""
    target = _norm_handle("phone" if channel in ("waha", "signal") else channel, handle)
    if not target:
        return None
    keys = _CHANNEL_KEYS.get(channel, (channel,))
    for e in list_files():
        if e["tag"] != "person":
            continue
        txt = read_file(e["rel"])
        handles = parse_person_handles(txt)
        for k in keys:
            for v in handles.get(k, []):
                if _norm_handle("email" if k == "email" else "phone", v) == target:
                    return {"rel": e["rel"], "title": e["title"], "content": txt,
                            "tone": person_tone(txt)}
    return None


def _set_header_line(text: str, label: str, value: str) -> str:
    """Insert/replace a `- **Label:** value` bullet near the top of a person file."""
    pat = re.compile(rf"^(\s*[-*]\s*\*{{0,2}}{re.escape(label)}\*{{0,2}}\s*:).*$",
                     re.IGNORECASE | re.MULTILINE)
    line = f"- **{label}:** {value}"
    if pat.search(text):
        return pat.sub(line, text, count=1)
    # Insert after the first heading, else prepend.
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            lines.insert(i + 1, line)
            return "\n".join(lines)
    return line + "\n" + text


def _merge_handles_block(text: str, handles: dict[str, str]) -> str:
    """Update/insert the astra:handles block, keeping any existing values."""
    existing = parse_person_handles(text)
    merged: dict[str, str] = {k: (v[0] if v else "") for k, v in existing.items()}
    for ch, val in handles.items():
        if val:
            merged[_HANDLE_ALIASES.get(ch.lower(), ch.lower())] = val
    order = ["whatsapp", "signal", "telegram", "email", "phone"]
    canon = {"waha": "whatsapp"}
    rows = {canon.get(k, k): v for k, v in merged.items()}
    block = "<!-- astra:handles\n" + "".join(
        f"{key}: {rows.get(key, '')}\n" for key in order
    ) + "-->"
    if _HANDLE_BLOCK.search(text):
        return _HANDLE_BLOCK.sub(lambda _m: block, text, count=1)
    # Append after the header bullets (after first blank line), else at end.
    return text.rstrip() + "\n\n" + block + "\n"


def upsert_person_profile(name: str, fields: dict) -> str | None:
    """Create or update a person profile, preserving existing freeform notes.

    `fields` may contain: relationship, trust_tier, tone, notes, can_share,
    dont_share and handle keys (whatsapp/signal/telegram/email/phone)."""
    rel = create_person(name)
    if not rel:
        return None
    text = read_file(rel)
    for label, key in (("Beziehung", "relationship"), ("Trust-Tier", "trust_tier"), ("Ton", "tone")):
        val = str(fields.get(key) or "").strip()
        if val:
            text = _set_header_line(text, label, val)
    handles = {k: str(fields.get(k) or "").strip()
               for k in ("whatsapp", "signal", "telegram", "email", "phone")}
    if any(handles.values()):
        text = _merge_handles_block(text, handles)
    notes = str(fields.get("notes") or "").strip()
    if notes:
        stamp = datetime.now().strftime("%Y-%m-%d")
        text = text.rstrip() + f"\n\n## Notizen & Umgangston-Beispiele\n- ({stamp}) {notes}\n" \
            if "## Notizen" not in text else text.rstrip() + f"\n- ({stamp}) {notes}\n"
    write_file(rel, text)
    return rel


def people_index() -> str:
    """One-line index of available person files (so ASTRA knows they exist)."""
    people = [e for e in list_files() if e["tag"] == "person"]
    if not people:
        return ""
    return "Personen-Dateien: " + ", ".join(f"{e['title']} ({e['rel']})" for e in people)


# ─── Room/device aliases for the world model ───────────────────────────────────
# Same machine-readable-block idea as astra:handles above, one file for the whole
# home: `real name: my word, my other word`. The world resolver reads this so
# "mein Zimmer" resolves even though Home Assistant calls it "Schlafzimmer".
WORLD_ALIAS_FILE = "world_aliases.md"
_ALIAS_BLOCK = re.compile(r"<!--\s*astra:aliases(.*?)-->", re.DOTALL | re.IGNORECASE)


def world_aliases() -> dict[str, list[str]]:
    """Return {real_name: [alias, ...]} from world_aliases.md (empty on any problem)."""
    out: dict[str, list[str]] = {}
    m = _ALIAS_BLOCK.search(read_file(WORLD_ALIAS_FILE))
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        target, raw = line.split(":", 1)
        target = target.strip()
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if target and values:
            out.setdefault(target, []).extend(values)
    return out


# ─── Compact fact retrieval (efficient owner memory) ───────────────────────────
# Instead of dumping every markdown file into the prompt, we keep candidate facts
# — DB rows, facts.md/people bullets, aliases — as short lines and inject only the
# ones relevant to the current message. The scorer is pure so it is unit-testable
# without a database.

@dataclass(frozen=True)
class Fact:
    kind: str
    subject: str
    value: str
    tags: tuple[str, ...] = ()
    always_on: bool = False
    weight: float = 1.0
    id: int | None = None

    def line(self) -> str:
        """One compact line for the prompt (caveman style — no prose)."""
        head = f"{self.subject}: {self.value}" if self.subject and self.value else \
               (self.value or self.subject)
        return f"[{self.kind}] {head}".strip()

    def _haystack(self) -> str:
        return " ".join([self.subject, self.value, self.kind, *self.tags])


_STOPWORDS = frozenset({
    "der", "die", "das", "und", "oder", "im", "in", "ist", "war", "mit", "auf",
    "den", "dem", "ein", "eine", "wie", "was", "wo", "mir", "mich", "mal", "bitte",
    "the", "a", "of", "is", "my", "me", "to", "for",
})


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens (umlaut-folded via the world normalizer if present)."""
    try:
        from . import world
        folded = world.fold(text)
    except Exception:  # noqa: BLE001 — knowledge must not hard-depend on world
        folded = (text or "").lower()
    return {t for t in re.split(r"[^a-z0-9]+", folded) if len(t) > 2 and t not in _STOPWORDS}


def score_facts(candidates: list[Fact], query: str, *, limit: int = 12) -> list[Fact]:
    """Rank facts by token overlap with the query. always_on facts always make it
    in; the rest compete for the remaining slots. Pure — no I/O."""
    q = _tokens(query)
    pinned = [f for f in candidates if f.always_on]
    rest = [f for f in candidates if not f.always_on]
    scored: list[tuple[float, Fact]] = []
    for f in rest:
        toks = _tokens(f._haystack())
        if not toks:
            continue
        overlap = 0.0
        for qt in q:
            if qt in toks:
                overlap += 1.0
            # Compound-word tolerance: "kaffee" inside "filterkaffee" still counts,
            # at reduced weight so it never beats an exact hit.
            elif any(len(qt) >= 4 and (qt in ft or ft in qt) for ft in toks):
                overlap += 0.6
        if overlap:
            # Longer facts shouldn't win just by having more words to match.
            score = (overlap / (len(toks) ** 0.5)) * max(0.1, f.weight)
            scored.append((score, f))
    scored.sort(key=lambda row: (-row[0], row[1].subject))
    room = max(0, limit - len(pinned))
    return pinned + [f for _s, f in scored[:room]]


def _bullets_as_facts(rel: str, kind: str) -> list[Fact]:
    """Parse '- foo' / '- **Label:** value' bullets from a markdown brain file."""
    out: list[Fact] = []
    for line in read_file(rel).splitlines():
        s = line.strip()
        if not s.startswith(("-", "*")):
            continue
        s = s.lstrip("-* ").strip()
        # Drop the leading date stamp remember_fact writes: "(2026-07-25) …"
        s = re.sub(r"^\(\d{4}-\d{2}-\d{2}\)\s*", "", s)
        if not s or s.startswith("_"):   # skip template "_Beispiel:_" lines
            continue
        m = re.match(r"\*{0,2}([^:*]{1,40})\*{0,2}\s*:\s*(.+)", s)
        if m:
            subject = m.group(1).strip().strip("*").strip()
            value = m.group(2).strip().strip("*").strip()
            out.append(Fact(kind=kind, subject=subject, value=value))
        else:
            out.append(Fact(kind=kind, subject="", value=s.strip("*").strip()))
    return out


def markdown_facts() -> list[Fact]:
    """Existing facts.md + per-person notes as retrievable candidates (not dumped)."""
    facts = _bullets_as_facts("facts.md", "bio")
    for e in list_files():
        if e["tag"] == "person":
            facts.extend(_bullets_as_facts(e["rel"], "relation"))
    return facts


async def relevant_facts(query: str, *, limit: int = 12, principal_key: str = "") -> str:
    """Compact block of the facts most relevant to `query`, for the owner prompt.

    Merges structured DB facts, facts.md/people bullets and room aliases, ranks by
    relevance, returns a short bullet list. Fault-tolerant: a missing DB just means
    the markdown candidates are used."""
    candidates: list[Fact] = list(markdown_facts())
    try:
        from . import db
        for r in await db.all_facts(principal_key=principal_key):
            candidates.append(Fact(
                kind=r["kind"], subject=r["subject"], value=r["value"],
                tags=tuple(r.get("tags") or ()), always_on=bool(r["always_on"]),
                weight=float(r.get("weight") or 1.0), id=r.get("id"),
            ))
    except Exception:  # noqa: BLE001 — no pool in tests / DB down → markdown only
        log.debug("relevant_facts: DB facts unavailable.", exc_info=True)
    for target, aliases in world_aliases().items():
        for spoken in aliases:
            candidates.append(Fact(kind="alias", subject=spoken, value=target))

    chosen = score_facts(candidates, query, limit=limit)
    if not chosen:
        return ""
    try:
        from . import db
        await db.touch_facts([f.id for f in chosen if f.id])
    except Exception:  # noqa: BLE001
        pass
    return "Relevantes über Bahrian (kompakt):\n" + "\n".join(f"- {f.line()}" for f in chosen)


async def world_aliases_db(*, principal_key: str = "") -> dict[str, list[str]]:
    """Room/device aliases stored as kind=alias facts (subject=spoken, value=target),
    reshaped to {real_target: [spoken, …]} for the world resolver."""
    out: dict[str, list[str]] = {}
    try:
        from . import db
        for r in await db.all_facts(principal_key=principal_key):
            if r["kind"] == "alias" and r["subject"] and r["value"]:
                out.setdefault(r["value"], []).append(r["subject"])
    except Exception:  # noqa: BLE001 — no DB → markdown aliases still apply
        log.debug("world_aliases_db unavailable.", exc_info=True)
    return out


def _render_alias_block(aliases: dict[str, list[str]]) -> str:
    rows = "".join(
        f"{target}: {', '.join(dict.fromkeys(values))}\n"
        for target, values in sorted(aliases.items()) if values
    )
    return "<!-- astra:aliases\n" + rows + "-->"


def set_world_alias(target: str, alias: str, *, remove: bool = False) -> bool:
    """Add or remove one alias for a room/device. Keeps the rest of the file intact."""
    target, alias = (target or "").strip(), (alias or "").strip()
    if not target or not alias:
        return False
    ensure_seeded()
    aliases = world_aliases()
    current = aliases.get(target, [])
    if remove:
        lowered = alias.casefold()
        kept = [a for a in current if a.casefold() != lowered]
        if len(kept) == len(current):
            return False
        if kept:
            aliases[target] = kept
        else:
            aliases.pop(target, None)
    else:
        if any(a.casefold() == alias.casefold() for a in current):
            return True   # already known — nothing to write, still a success
        aliases[target] = current + [alias]

    text = read_file(WORLD_ALIAS_FILE) or _SEED[WORLD_ALIAS_FILE]
    block = _render_alias_block(aliases)
    if _ALIAS_BLOCK.search(text):
        text = _ALIAS_BLOCK.sub(lambda _m: block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    return write_file(WORLD_ALIAS_FILE, text)
