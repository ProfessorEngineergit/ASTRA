"""Kontaktkarten — pro Person UND pro Gruppe: wer darf was, wie klingt ASTRA, was gilt dort.

Das ist das Herzstück der Vision „ich gebe pro Benutzer frei, was er bekommt". Eine Karte
bündelt, was bisher auf drei Orte verstreut war (Kontaktregeln in den Einstellungen,
Profil-Markdown, Trust-Tier am Kontakt):

    rule         block | ask | allow | direct   ("" = Standard-Policy)
    trust_tier   0 Ich · 1 eng · 2 bekannt · 3 fremd
    style        Schlüssel aus styles.py oder Freitext
    instruction  Freitext-Anweisung nur für diese Person/Gruppe
    share        was darf sie/er über mich erfahren (Verfügbarkeit gestuft, Rest ja/nein)
    group        Gruppen: Trigger (nur bei @Erwähnung …), Rolle (Assistent/Moderator/Zuhörer)
    active       eigenes Zeitfenster (immer / nie / von–bis) statt des globalen Secretary-Plans
    model        eigenes Modell (z. B. günstig für Kumpels, stark für Schule)
    learned      was ASTRA aus Freigaben gelernt hat ("Verfügbarkeit: immer erlaubt") — widerrufbar

Gruppen sind wie Benutzer: nur was Bahrian freigibt, existiert für ASTRA. Alles hier ist
rein (Bereinigen, Abgleichen, Entscheiden) und ohne Datenbank testbar; nur load/save/sync
sprechen mit db.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time as dtime

SHARE_LEVELS = ("", "none", "freebusy", "details")
SHARE_TOPICS = ("availability", "location", "school", "contact", "personal")
YESNO = ("", "yes", "no")
RULES = ("", "block", "ask", "allow", "direct")
GROUP_TRIGGERS = ("mention", "always", "keywords", "off")
GROUP_ROLES = ("assistant", "moderator", "listener")
ACTIVE_MODES = ("inherit", "always", "never", "window")

TOPIC_LABELS = {
    "availability": "Verfügbarkeit (Kalender)", "location": "Aufenthaltsort / Zuhause",
    "school": "Schule / Stundenplan", "contact": "Kontaktdaten", "personal": "Persönliches",
}


# ─── Aufbau & Bereinigung ─────────────────────────────────────────────────────
def slugify(name: str) -> str:
    t = unicodedata.normalize("NFKD", (name or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", t.replace("ß", "ss")).strip("_")[:48] or "unbenannt"


def new_card(kind: str, name: str, handles: list[dict] | None = None) -> dict:
    return sanitize({"kind": kind, "name": name, "key": slugify(name), "handles": handles or []})


def _pick(value, allowed, default=""):
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def norm_handle(channel: str, handle: str) -> str:
    """Vergleichbare Form: Telefon → letzte 9 Ziffern (0171… == +49 171…), Mail klein,
    Gruppen-Ids unverändert klein. Damit passt eine Karte auch bei anderer Schreibweise."""
    raw = (handle or "").strip().lower()
    if not raw:
        return ""
    if channel == "email" or "@" in raw and "@c.us" not in raw and "@g.us" not in raw \
            and "@s.whatsapp" not in raw and "@lid" not in raw:
        return raw
    if raw.endswith("@g.us") or channel == "telegram" and raw.startswith("-"):
        return raw
    digits = re.sub(r"\D", "", raw.split("@", 1)[0])
    if len(digits) >= 8:
        return digits[-9:]
    return raw.split("@", 1)[0]


def sanitize(card: dict) -> dict:
    """Karte auf gültige Werte bringen (Formular, Import, ASTRA-Tool — alles läuft hier durch)."""
    c = dict(card or {})
    kind = _pick(c.get("kind"), ("person", "group"), "person")
    name = str(c.get("name") or "").strip()[:80]
    key = slugify(c.get("key") or name)
    handles, seen = [], set()
    for h in c.get("handles") or []:
        if not isinstance(h, dict):
            continue
        ch, hid = str(h.get("channel") or "").strip().lower(), str(h.get("id") or "").strip()
        if ch and hid and (ch, norm_handle(ch, hid)) not in seen:
            seen.add((ch, norm_handle(ch, hid)))
            handles.append({"channel": ch, "id": hid})
    try:
        tier = max(0, min(3, int(c.get("trust_tier", 3))))
    except (TypeError, ValueError):
        tier = 3
    share_in = c.get("share") or {}
    share = {"availability": _pick(share_in.get("availability"), SHARE_LEVELS)}
    for t in SHARE_TOPICS[1:]:
        share[t] = _pick(share_in.get(t), YESNO)
    g = c.get("group") or {}
    kws = g.get("keywords") or []
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",")]
    aliases = g.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",")]
    group = {
        "trigger": _pick(g.get("trigger"), GROUP_TRIGGERS, "mention"),
        "keywords": [k.strip() for k in kws if k and k.strip()][:20],
        "aliases": [a.strip().lstrip("@") for a in aliases if a and a.strip()][:10],
        "role": _pick(g.get("role"), GROUP_ROLES, "assistant"),
        "actions": bool(g.get("actions")),
    }
    a = c.get("active") or {}
    days = a.get("days") or []
    active = {
        "mode": _pick(a.get("mode"), ACTIVE_MODES, "inherit"),
        "start": str(a.get("start") or "")[:5], "end": str(a.get("end") or "")[:5],
        "days": sorted({int(d) for d in days if str(d).isdigit() and 0 <= int(d) <= 6}),
    }
    model = c.get("model") or {}
    pick = {}
    if model.get("tier"):
        pick = {"tier": str(model["tier"]).strip().lower()}
    elif model.get("provider") and model.get("model"):
        pick = {"provider": str(model["provider"]).strip().lower(), "model": str(model["model"]).strip()}
    learned = [x for x in (c.get("learned") or []) if isinstance(x, dict) and x.get("topic")][-30:]
    proposals = [x for x in (c.get("proposals") or []) if isinstance(x, dict) and x.get("text")][-10:]
    return {
        "key": key, "kind": kind, "name": name or key, "handles": handles,
        "relationship": str(c.get("relationship") or "").strip()[:60],
        "trust_tier": tier, "rule": _pick(c.get("rule"), RULES),
        "style": str(c.get("style") or "").strip()[:200],
        "instruction": str(c.get("instruction") or "").strip()[:1500],
        "share": share, "group": group, "active": active, "model": pick,
        "notes": str(c.get("notes") or "").strip()[:4000],
        "learned": learned, "proposals": proposals,
    }


# ─── Abgleich ─────────────────────────────────────────────────────────────────
def matches(card: dict, channel: str, handle: str) -> bool:
    target = norm_handle(channel, handle)
    if not target:
        return False
    for h in card.get("handles") or []:
        if h.get("channel") in (channel, "*") and norm_handle(h["channel"] if h["channel"] != "*" else channel,
                                                              h.get("id", "")) == target:
            return True
    return False


def find_in(cards: list[dict], channel: str, handle: str, *, kind: str | None = None) -> dict | None:
    for c in cards:
        if kind and c.get("kind") != kind:
            continue
        if matches(c, channel, handle):
            return c
    return None


# ─── Freigaben ────────────────────────────────────────────────────────────────
def share_ceiling(card: dict | None) -> str | None:
    """Obergrenze für Kalenderauskünfte laut Karte: none|freebusy|details, None = nicht gesetzt."""
    level = ((card or {}).get("share") or {}).get("availability") or ""
    return level or None


def share_prompt(card: dict | None) -> str:
    """Kurzer Prompt-Block: was diese Person wissen darf / nicht (nur ausdrücklich Gesetztes)."""
    share = (card or {}).get("share") or {}
    allowed, denied = [], []
    lvl = share.get("availability") or ""
    if lvl == "none":
        denied.append("Kalender/Verfügbarkeit (auch ob er frei oder beschäftigt ist)")
    elif lvl == "freebusy":
        allowed.append("nur ob Bahrian frei oder beschäftigt ist (keine Termindetails)")
    elif lvl == "details":
        allowed.append("Kalender-Details (Was/Wann/Wo)")
    for topic in SHARE_TOPICS[1:]:
        v = share.get(topic) or ""
        if v == "yes":
            allowed.append(TOPIC_LABELS[topic])
        elif v == "no":
            denied.append(TOPIC_LABELS[topic])
    if not allowed and not denied:
        return ""
    parts = []
    if allowed:
        parts.append("Bahrian hat für diese Person FREIGEGEBEN: " + "; ".join(allowed) + ".")
    if denied:
        parts.append("Bahrian hat für diese Person AUSDRÜCKLICH VERBOTEN: " + "; ".join(denied)
                     + ". Sag dazu nichts, auch nicht andeutungsweise.")
    return " ".join(parts)


def instruction_block(card: dict | None) -> str:
    text = ((card or {}).get("instruction") or "").strip()
    if not text:
        return ""
    who = "Gruppe" if (card or {}).get("kind") == "group" else "Person"
    return f"Anweisung von Bahrian für diese {who} (verbindlich, geht vor allgemeinen Gewohnheiten): {text}"


TOPIC_KEYWORDS = {
    "availability": ("kalender", "termin", "zeit", "frei", "beschäftigt", "beschaeftigt", "wann", "verfügbar",
                     "verfuegbar", "uhr", "heute", "morgen", "abend", "wochenende", "treffen"),
    "location": ("wo ", "wo?", "standort", "zuhause", "zu hause", "adresse", "unterwegs", "wohnst", "bahn"),
    "school": ("schule", "stundenplan", "unterricht", "klasse", "lehrer", "hausaufgabe", "klausur", "vertretung"),
    "contact": ("nummer", "telefon", "handynummer", "mail", "email", "e-mail", "kontakt"),
}


def topic_for(text: str) -> str:
    """Grobe Themenerkennung für „Immer erlauben“: worum ging die Freigabe-Frage?"""
    low = (text or "").lower() + " "
    best, hits = "personal", 0
    for topic, words in TOPIC_KEYWORDS.items():
        n = sum(1 for w in words if w in low)
        if n > hits:
            best, hits = topic, n
    return best


def learn_share(card: dict, topic: str, decision: str, now: float | None = None) -> dict:
    """Freigabe-Entscheidung („immer“/„nie“) dauerhaft in die Karte schreiben. Rein."""
    c = sanitize(card)
    topic = topic if topic in SHARE_TOPICS else "personal"
    if topic == "availability":
        level = {"always_yes": "details", "always_busy": "freebusy", "never": "none"}.get(decision)
    else:
        level = {"always_yes": "yes", "always_busy": "yes", "never": "no"}.get(decision)
    if level is None:
        return c
    c["share"][topic] = level
    c["learned"] = (c["learned"] + [{"topic": topic, "level": level, "at": int(now or time.time())}])[-30:]
    return sanitize(c)


def revoke_learned(card: dict, topic: str) -> dict:
    c = sanitize(card)
    if topic in SHARE_TOPICS:
        c["share"][topic] = ""
    c["learned"] = [x for x in c["learned"] if x.get("topic") != topic]
    return sanitize(c)


# ─── Gruppen ──────────────────────────────────────────────────────────────────
def mention_tokens(owner_name: str, card: dict | None = None, own_ids: list[str] | None = None) -> list[str]:
    """Wörter, bei denen ASTRA sich in einer Gruppe angesprochen fühlt: Bahrians Name, „astra“,
    Aliasse der Gruppe und die eigene Nummer (WhatsApp schreibt Erwähnungen als @49177…)."""
    toks = {"astra"}
    if owner_name:
        toks.add(owner_name.strip().lower())
    for a in ((card or {}).get("group") or {}).get("aliases") or []:
        toks.add(a.lower())
    for oid in own_ids or []:
        digits = re.sub(r"\D", "", str(oid).split("@", 1)[0])
        if len(digits) >= 6:
            toks.add(digits)
    return sorted(t for t in toks if t)


def is_mentioned(text: str, tokens: list[str]) -> bool:
    """@Name (oder @Nummer) irgendwo im Text. Ein bloßes Wort ohne @ zählt NICHT — sonst
    reagiert ASTRA auf jede Erwähnung im Gespräch über ihn."""
    low = unicodedata.normalize("NFKC", text or "").lower()
    for t in tokens:
        if re.search(rf"(?<![\w@])@\s?{re.escape(t)}\b", low):
            return True
    return False


@dataclass(frozen=True)
class GroupDecision:
    respond: bool
    reason: str
    moderating: bool = False


def group_decision(card: dict | None, text: str, *, tokens: list[str], reply_to_us: bool = False,
                   flagged: bool = False) -> GroupDecision:
    """Soll ASTRA in dieser Gruppe auf diese Nachricht antworten? Rein.

    Ohne Karte: nein (Gruppen existieren erst, wenn Bahrian sie freigibt). Zuhörer: nie,
    nur Kontext sammeln. Moderator: bei @Erwähnung UND bei auffälligen Nachrichten."""
    if not card or card.get("kind") != "group":
        return GroupDecision(False, "unbekannte-gruppe")
    if card.get("rule") == "block":
        return GroupDecision(False, "gruppe-blockiert")
    g = card.get("group") or {}
    role, trigger = g.get("role", "assistant"), g.get("trigger", "mention")
    if role == "listener" or trigger == "off":
        return GroupDecision(False, "zuhörer")
    if role == "moderator" and flagged:
        return GroupDecision(True, "moderation", moderating=True)
    if reply_to_us:
        return GroupDecision(True, "antwort-auf-astra")
    if trigger == "always":
        return GroupDecision(True, "immer")
    if trigger == "mention" and is_mentioned(text, tokens):
        return GroupDecision(True, "erwähnt")
    if trigger == "keywords":
        low = (text or "").lower()
        if is_mentioned(text, tokens) or any(k.lower() in low for k in g.get("keywords") or []):
            return GroupDecision(True, "stichwort")
    return GroupDecision(False, "nicht-angesprochen")


# ─── Zeitfenster pro Karte ────────────────────────────────────────────────────
def _hhmm(v: str, fallback: dtime) -> dtime:
    try:
        h, m = str(v).split(":", 1)
        return dtime(int(h), int(m[:2]))
    except Exception:  # noqa: BLE001
        return fallback


def active_state(card: dict | None, now: datetime) -> bool | None:
    """True = antworte jetzt sicher, False = jetzt nicht, None = globaler Secretary-Plan gilt."""
    a = (card or {}).get("active") or {}
    mode = a.get("mode", "inherit")
    if mode == "always":
        return True
    if mode == "never":
        return False
    if mode != "window":
        return None
    days = a.get("days") or []
    if days and now.weekday() not in days:
        return False
    start, end = _hhmm(a.get("start", ""), dtime(0, 0)), _hhmm(a.get("end", ""), dtime(23, 59))
    t = now.time()
    return (start <= t <= end) if start <= end else (t >= start or t <= end)


# ─── Speicher (I/O) ───────────────────────────────────────────────────────────
_CACHE: dict = {"at": 0.0, "cards": []}
_TTL = 15.0


def invalidate() -> None:
    _CACHE.update(at=0.0, cards=[])


async def load_all(*, force: bool = False, principal_key: str = "") -> list[dict]:
    now = time.monotonic()
    if not force and _CACHE["cards"] and now - _CACHE["at"] < _TTL:
        return _CACHE["cards"]
    try:
        from . import db
        rows = [sanitize(r) for r in await db.card_list(principal_key)]
    except Exception:  # noqa: BLE001 — ohne DB gibt es einfach keine Karten
        rows = []
    _CACHE.update(at=now, cards=rows)
    return rows


async def find_card(channel: str, handle: str, *, kind: str | None = None) -> dict | None:
    return find_in(await load_all(), channel, handle, kind=kind)


async def save_card(card: dict, principal_key: str = "") -> dict:
    from . import db
    c = sanitize(card)
    await db.card_save(c, principal_key)
    invalidate()
    return c


async def delete_card(key: str, principal_key: str = "") -> int:
    from . import db
    n = await db.card_delete(key, principal_key)
    invalidate()
    return n


async def ensure_card_for(channel: str, handle: str, name: str = "", *, kind: str = "person") -> dict:
    """Karte holen oder minimal anlegen (z. B. wenn „immer erlauben“ zum ersten Mal gedrückt wird)."""
    existing = await find_card(channel, handle, kind=kind)
    if existing:
        return existing
    card = new_card(kind, name or handle, [{"channel": channel, "id": handle}])
    # Schlüssel-Kollision (gleicher Name, andere Person) vermeiden.
    keys = {c["key"] for c in await load_all()}
    base, i = card["key"], 2
    while card["key"] in keys:
        card["key"] = f"{base}_{i}"
        i += 1
    return await save_card(card)


# ─── Import bestehender Daten (idempotent, überschreibt nie) ──────────────────
def legacy_cards(person_files: list[dict], contact_rules: list[dict]) -> list[dict]:
    """Aus people/*.md-Profilen und secretary.contact_rules Karten ableiten. Rein.

    `person_files`: [{"rel","title","content"}] · `contact_rules`: [{"channel","id","rule","note"}]."""
    from . import knowledge
    by_key: dict[str, dict] = {}
    for f in person_files:
        text = f.get("content") or ""
        handles = []
        for ch, vals in knowledge.parse_person_handles(text).items():
            for v in vals:
                if ch == "phone":
                    handles += [{"channel": "waha", "id": v}, {"channel": "signal", "id": v}]
                else:
                    handles.append({"channel": ch, "id": v})
        rel = re.search(r"\*{0,2}Beziehung\*{0,2}\s*:\s*\*{0,2}\s*(.+)", text)
        tier = re.search(r"\*{0,2}Trust-Tier\*{0,2}\s*:\s*\*{0,2}\s*(\d)", text)
        card = new_card("person", f.get("title") or f["rel"], handles)
        card["style"] = knowledge.person_tone(text)
        card["relationship"] = (rel.group(1).strip() if rel else "")[:60]
        card["trust_tier"] = int(tier.group(1)) if tier else 3
        card["notes"] = text[:4000]
        by_key[card["key"]] = sanitize(card)
    for r in contact_rules:
        ch, hid, rule = r.get("channel", ""), r.get("id", ""), r.get("rule", "")
        if not hid or rule not in RULES:
            continue
        hit = find_in(list(by_key.values()), ch if ch != "*" else "waha", hid)
        if hit:
            hit["rule"] = hit.get("rule") or rule
            continue
        is_group = str(hid).endswith("@g.us")
        c = new_card("group" if is_group else "person", r.get("note") or hid,
                     [{"channel": ch if ch != "*" else "waha", "id": hid}])
        c["rule"] = rule
        c["key"] = slugify(f"{c['name']}_{norm_handle(ch, hid)[-4:]}")
        by_key.setdefault(c["key"], sanitize(c))
    return list(by_key.values())


async def sync_legacy(app_settings: dict) -> int:
    """Fehlende Karten aus alten Profilen/Regeln anlegen. Gibt die Zahl neuer Karten zurück."""
    from . import knowledge
    existing = await load_all(force=True)
    people = []
    for e in knowledge.list_files():
        if e["tag"] == "person":
            people.append({**e, "content": knowledge.read_file(e["rel"])})
    rules = list(((app_settings or {}).get("secretary") or {}).get("contact_rules") or [])
    created = 0
    for c in legacy_cards(people, rules):
        dup = any(x["key"] == c["key"] or any(matches(x, h["channel"], h["id"]) for h in c["handles"])
                  for x in existing)
        if dup:
            continue
        await save_card(c)
        existing.append(c)
        created += 1
    return created


# ─── Aus Freigaben lernen (I/O) ───────────────────────────────────────────────
LEARN_DECISIONS = {"always_yes": "yes", "always_busy": "busy_only", "never": "no"}


async def learn_from_approval(approval: dict, decision: str) -> dict | None:
    """„Immer“/„Nie“ aus einer Telegram-Freigabe dauerhaft in die Karte der Person schreiben.

    Das ist die lernende Schleife: beim zweiten Mal fragt ASTRA nicht mehr. Die Karte wird
    bei Bedarf minimal angelegt; das Gelernte steht sichtbar in `learned` und ist in der UI
    widerrufbar."""
    if decision not in LEARN_DECISIONS:
        return None
    from . import db
    payload = approval.get("payload") or {}
    thread_id = approval.get("thread_id") or ""
    channel = payload.get("channel") or (thread_id.split(":", 1)[0] if ":" in thread_id else "")
    handle = thread_id.split(":", 1)[1] if ":" in thread_id else ""
    if not channel or not handle:
        return None
    contact = await db.get_contact(approval.get("contact_id")) if approval.get("contact_id") else {}
    card = await ensure_card_for(channel, handle, (contact or {}).get("display_name") or handle)
    topic = payload.get("topic") or topic_for(approval.get("question") or "")
    updated = learn_share(card, topic, decision)
    await save_card(updated)
    await db.audit("card_learned", actor="owner",
                   detail={"card": updated["key"], "topic": topic, "decision": decision})
    return updated


# ─── Formular ↔ Karte (rein; die Admin-Seite ruft nur das hier) ───────────────
_CHANNEL_ALIASES = {"whatsapp": "waha", "wa": "waha", "waha": "waha", "signal": "signal",
                    "telegram": "telegram", "tg": "telegram", "mail": "email", "email": "email",
                    "e-mail": "email", "slack": "slack", "*": "*"}


def parse_handles(text: str) -> list[dict]:
    """Zeilenweise „kanal: kennung“ → Handles. Ohne Kanal: Nummer → WhatsApp, @ → E-Mail."""
    out = []
    for line in (text or "").replace(";", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        chan, sep, ident = line.partition(":")
        if sep and chan.strip().lower() in _CHANNEL_ALIASES and ident.strip():
            out.append({"channel": _CHANNEL_ALIASES[chan.strip().lower()], "id": ident.strip()})
        elif "@" in line and "@c.us" not in line and "@g.us" not in line:
            out.append({"channel": "email", "id": line})
        elif line.endswith("@g.us") or line.endswith("@c.us") or re.sub(r"[\d\s+()-]", "", line) == "":
            out.append({"channel": "waha", "id": line.replace(" ", "")})
    return out


def handles_text(card: dict) -> str:
    return "\n".join(f"{h['channel']}: {h['id']}" for h in card.get("handles") or [])


def card_from_form(form, existing: dict | None = None) -> dict:
    """Bearbeitungsformular → bereinigte Karte. `existing` liefert, was das Formular nicht
    enthält (gelernte Freigaben, Vorschläge, Schlüssel)."""
    def g(name, default=""):
        v = form.get(name)
        return default if v is None else str(v)

    base = dict(existing or {})
    kind = base.get("kind") or g("kind", "person")
    model_value = g("model")
    pick: dict = {}
    if model_value.startswith("tier:"):
        pick = {"tier": model_value[5:]}
    elif model_value.startswith("model:") and "|" in model_value:
        prov, _, mod = model_value[6:].partition("|")
        pick = {"provider": prov, "model": mod}
    style = g("style_custom").strip() if g("style") == "__custom__" else g("style")
    days = [d for d in (form.getlist("days") if hasattr(form, "getlist") else []) if str(d).isdigit()]
    card = {
        **base,
        "kind": kind, "key": base.get("key") or slugify(g("name")), "name": g("name"),
        "handles": parse_handles(g("handles")), "relationship": g("relationship"),
        "trust_tier": g("trust_tier", "3"), "rule": g("rule"), "style": style,
        "instruction": g("instruction"), "notes": g("notes"), "model": pick,
        "share": {"availability": g("share_availability"), **{t: g(f"share_{t}") for t in SHARE_TOPICS[1:]}},
        "active": {"mode": g("active_mode", "inherit"), "start": g("active_start"),
                   "end": g("active_end"), "days": days},
    }
    if kind == "group":
        card["group"] = {"trigger": g("group_trigger", "mention"), "role": g("group_role", "assistant"),
                         "keywords": g("group_keywords"), "aliases": g("group_aliases"),
                         "actions": bool(form.get("group_actions"))}
    return sanitize(card)


# ─── Änderungen per Tool/Befehl (rein) ────────────────────────────────────────
PATCH_FIELDS = ("style", "instruction", "rule", "trust_tier", "relationship", "notes", "share_availability",
                *(f"share_{t}" for t in SHARE_TOPICS[1:]), "group_trigger", "group_role", "group_keywords",
                "group_aliases", "active_mode", "active_start", "active_end", "model_tier")


def apply_patch(card: dict, patch: dict) -> tuple[dict, list[str]]:
    """Einzelne Felder ändern, alles andere bleibt. → (neue Karte, Liste geänderter Felder).
    Unbekannte Felder werden ignoriert; ungültige Werte fallen in `sanitize` auf Standard."""
    c = sanitize(card)
    changed: list[str] = []
    for field in PATCH_FIELDS:
        if field not in patch or patch[field] is None:
            continue
        v = patch[field]
        if field in ("style", "instruction", "rule", "trust_tier", "relationship", "notes"):
            c[field] = v
        elif field.startswith("share_"):
            c["share"][field[6:]] = v
        elif field.startswith("group_"):
            c["group"][field[6:]] = v
        elif field.startswith("active_"):
            c["active"][field[7:]] = v
        elif field == "model_tier":
            c["model"] = {"tier": v} if v else {}
        changed.append(field)
    return sanitize(c), changed


def find_by_name(card_list: list[dict], query: str) -> tuple[list[dict], str]:
    """Karte per Name/Schlüssel/Kennung finden. → (Treffer, Grund). Exakt schlägt „enthält“."""
    q = slugify(query)
    low = (query or "").strip().lower()
    if not q or q == "unbenannt" and not low:
        return [], "empty"
    exact = [c for c in card_list if c["key"] == q or c["name"].lower() == low]
    if exact:
        return exact[:1], "exact"
    digits = re.sub(r"\D", "", low)
    if len(digits) >= 8:
        hit = [c for c in card_list if any(norm_handle(h["channel"], h["id"]) == digits[-9:] for h in c["handles"])]
        if hit:
            return hit, "handle"
    part = [c for c in card_list if low in c["name"].lower() or q in c["key"]]
    return part, "partial"


# ─── Verzeichnis & Sammelaktionen (rein) ──────────────────────────────────────
import base64 as _b64
import json as _json


def is_group_handle(channel: str, handle: str) -> bool:
    h = (handle or "").lower()
    return h.endswith("@g.us") or (channel == "telegram" and h.startswith("-")) or (
        channel == "signal" and h.endswith("=="))


def encode_ref(row: dict) -> str:
    """Auswahl-Wert einer Zeile: vorhandene Karte oder (noch) kartenloser Kontakt."""
    if row.get("key"):
        return "card:" + row["key"]
    raw = _json.dumps({"c": row["channel"], "h": row["handle"], "n": row.get("name", "")}, ensure_ascii=False)
    return "new:" + _b64.urlsafe_b64encode(raw.encode()).decode()


def decode_ref(ref: str) -> dict | None:
    try:
        if ref.startswith("card:"):
            return {"key": ref[5:]}
        if ref.startswith("new:"):
            d = _json.loads(_b64.urlsafe_b64decode(ref[4:].encode()).decode())
            return {"channel": str(d["c"]), "handle": str(d["h"]), "name": str(d.get("n") or "")}
    except Exception:  # noqa: BLE001
        return None
    return None


def directory(card_list: list[dict], contacts: list[dict]) -> list[dict]:
    """Eine Liste aus Karten + bekannten Kontakten ohne Karte. Jede Zeile hat `key` (Karte)
    oder `channel`/`handle` (noch keine Karte). Sortiert: Karten zuerst, dann Kontakte."""
    rows: list[dict] = []
    for c in card_list:
        rows.append({"key": c["key"], "name": c["name"], "kind": c["kind"], "has_card": True,
                     "channels": sorted({h["channel"] for h in c["handles"]}), "tier": c["trust_tier"],
                     "rule": c["rule"], "style": c["style"], "relationship": c["relationship"],
                     "active": (c["active"] or {}).get("mode", "inherit"), "proposals": len(c["proposals"]),
                     "sec_on": secretary_on(c)})
    seen = set()
    for ct in contacts:
        ch, hd = str(ct.get("channel") or ""), str(ct.get("handle") or "")
        if not ch or not hd or (ch, norm_handle(ch, hd)) in seen or find_in(card_list, ch, hd):
            continue
        seen.add((ch, norm_handle(ch, hd)))
        name = str(ct.get("display_name") or hd)
        rows.append({"key": None, "channel": ch, "handle": hd, "name": name,
                     "kind": "group" if is_group_handle(ch, hd) else "person", "has_card": False,
                     "channels": [ch], "tier": int(ct.get("trust_tier") if ct.get("trust_tier") is not None else 3),
                     "rule": "", "style": "", "relationship": str(ct.get("relationship") or ""),
                     "active": "inherit", "proposals": 0, "sec_on": True})
    return rows


def filter_directory(rows: list[dict], *, q: str = "", scope: str = "all") -> list[dict]:
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if scope == "person" and r["kind"] != "person" or scope == "group" and r["kind"] != "group":
            continue
        if scope == "nocard" and r["has_card"]:
            continue
        if scope == "proposals" and not r["proposals"]:
            continue
        if scope == "off" and r["sec_on"]:
            continue
        if q and q not in r["name"].lower() and not any(q in c for c in r["channels"]) \
                and q not in str(r.get("handle") or "").lower():
            continue
        out.append(r)
    return out


def bulk_patch(get) -> dict:
    """Formularfelder `b_<feld>` → Änderungen. '' = nicht ändern, '__clear__' = auf Standard/leer."""
    patch: dict = {}
    for f in PATCH_FIELDS:
        v = get("b_" + f)
        if v is None or str(v) == "":
            continue
        patch[f] = "" if v == "__clear__" else v
    return patch


def apply_bulk(card: dict, patch: dict) -> tuple[dict, list[str]]:
    """Wie apply_patch, aber Gruppenfelder betreffen nur Gruppen."""
    p = {k: v for k, v in patch.items() if not k.startswith("group_") or card.get("kind") == "group"}
    return apply_patch(card, p)


# ─── Secretary pro Person/Gruppe an/aus (rein) ────────────────────────────────
def secretary_on(card: dict | None) -> bool:
    """Antwortet ASTRA dieser Person/Gruppe überhaupt? Aus = eigene Aktivzeit „nie“ oder Regel „blockieren“.
    Eine Person ohne Karte folgt dem globalen Secretary (also: an)."""
    if not card:
        return True
    return (card.get("active") or {}).get("mode") != "never" and card.get("rule") != "block"


def set_secretary(card: dict, on: bool) -> dict:
    """Schalter setzen. AUS: ASTRA schweigt (Nachrichten werden weiter notiert). AN: die Sperren
    (nie / blockieren) fallen weg, ansonsten bleibt alles wie eingestellt."""
    c = sanitize(card)
    if on:
        if c["active"]["mode"] == "never":
            c["active"]["mode"] = "inherit"
        if c["rule"] == "block":
            c["rule"] = ""
    else:
        c["active"]["mode"] = "never"
    return sanitize(c)
