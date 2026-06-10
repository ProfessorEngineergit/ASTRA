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
from datetime import datetime
from pathlib import Path

from .config import get_settings

log = logging.getLogger("astra.knowledge")

# Owner-private files (injected only when ASTRA talks to the owner himself).
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
}


def _dir() -> Path:
    return Path(get_settings().brain_data_dir)


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
    """Concatenated private knowledge for the OWNER register (system prompt block)."""
    parts = [c for name in _PRIVATE if (c := _read(name))]
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
