"""Chief-of-Staff-Werkzeuge — ASTRA bedient damit die eigene Sekretariats-Zentrale.

Alles owner-only (Dritte sehen diese Werkzeuge nie, `dispatch` blockt zusätzlich). Wer im
Chat mit ASTRA spricht, ist Bahrian; im Web-Chat pausiert der Modus „Fragen“ jede
verändernde Aktion bis zur Bestätigung (siehe `tools.needs_confirmation`).

    usage_report          Verbrauch/Kosten (Zeitraum, Aufschlüsselung)
    secretary_switch      Secretary an/aus/auto, auch befristet
    contact_cards_list    alle Personen-/Gruppenkarten (kurz)
    contact_card_get      eine Karte lesen
    contact_card_update   Ton, Regeln, Freigaben, Gruppen-Trigger … ändern (legt neue Karten an)
    context_forget        Journal + Zusammenfassung einer Person/Gruppe restlos löschen
    prompt_show           einen Prompt-Baustein lesen
    prompt_propose        Prompt-Änderung VORSCHLAGEN (wirkt erst nach Bahrians Freigabe im Admin)
"""
from __future__ import annotations

import logging

from . import cards, db, digest, google_hub, owner_commands, prompts, styles, usage
from .config import get_settings
from .tools import Tool, ToolContext, register, tool_result

log = logging.getLogger("astra.chief_tools")


# ─── Verbrauch ────────────────────────────────────────────────────────────────
async def _usage_report(args: dict, ctx: ToolContext) -> str:
    period = str(args.get("period") or "month").lower()
    by = str(args.get("by") or "model").lower()
    period = period if period in usage.PERIODS else "month"
    by = by if by in usage.GROUPS else "model"
    text = await usage.report(period, by, tz=get_settings().astra_timezone)
    return tool_result(ok=True, summary=text, data={"period": period, "by": by}, source="usage")


# ─── Secretary-Schalter ───────────────────────────────────────────────────────
async def _secretary_switch(args: dict, ctx: ToolContext) -> str:
    action = str(args.get("action") or "").lower()
    if action not in ("on", "off", "auto", "status"):
        return tool_result(ok=False, summary="action muss on, off, auto oder status sein.", source="secretary")
    minutes = args.get("minutes")
    until = str(args.get("until") or "").strip()
    hhmm = None
    if until:
        try:
            h, _, m = until.partition(":")
            hhmm = (int(h), int(m or 0))
            if not (0 <= hhmm[0] < 24 and 0 <= hhmm[1] < 60):
                raise ValueError
        except ValueError:
            return tool_result(ok=False, summary="until muss 'HH:MM' sein.", source="secretary")
    try:
        minutes = float(minutes) if minutes else None
    except (TypeError, ValueError):
        return tool_result(ok=False, summary="minutes muss eine Zahl sein.", source="secretary")
    cmd = owner_commands.Command(action, minutes=minutes, until_hhmm=hhmm, tomorrow=bool(args.get("tomorrow")))
    reply, _ = await owner_commands.execute(cmd, timezone=get_settings().astra_timezone)
    return tool_result(ok=True, summary=reply, source="secretary")


# ─── Kontaktkarten ────────────────────────────────────────────────────────────
def _brief(c: dict) -> str:
    bits = [f'{c["name"]} ({c["kind"]}, key={c["key"]})', f'Stufe {c["trust_tier"]}']
    if c["rule"]:
        bits.append(f'Regel {c["rule"]}')
    if c["style"]:
        bits.append("Stil " + styles.label(c["style"]))
    return " · ".join(bits)


def _detail(c: dict) -> dict:
    return {k: c[k] for k in ("key", "kind", "name", "relationship", "trust_tier", "rule", "style", "instruction",
                              "share", "group", "active", "model", "notes", "learned")} | {
        "handles": [f'{h["channel"]}:{h["id"]}' for h in c["handles"]]}


async def _cards_list(args: dict, ctx: ToolContext) -> str:
    kind = str(args.get("kind") or "")
    rows = [c for c in await cards.load_all(force=True) if not kind or c["kind"] == kind]
    return tool_result(ok=True, summary="\n".join(_brief(c) for c in rows) or "Noch keine Karten.",
                       data=[{"key": c["key"], "name": c["name"], "kind": c["kind"]} for c in rows],
                       source="contacts")


async def _resolve(name: str) -> tuple[dict | None, str]:
    hits, why = cards.find_by_name(await cards.load_all(force=True), name)
    if not hits:
        return None, "not_found"
    if len(hits) > 1:
        return None, "ambiguous:" + ", ".join(h["name"] for h in hits[:5])
    return hits[0], why


async def _card_get(args: dict, ctx: ToolContext) -> str:
    card, why = await _resolve(str(args.get("name") or ""))
    if not card:
        msg = ("Mehrdeutig — meinst du: " + why.split(":", 1)[1]) if why.startswith("ambiguous") else "Keine Karte gefunden."
        return tool_result(ok=False, summary=msg, source="contacts")
    return tool_result(ok=True, summary=_brief(card), data=_detail(card), source="contacts")


_UPDATE_PROPS = {
    "style": {"type": "string", "description": "Stil-Schlüssel (" + ", ".join(k for k, *_ in styles.choices())
                                              + ") oder Freitext"},
    "instruction": {"type": "string", "description": "verbindliche Anweisung für diese Person/Gruppe"},
    "rule": {"type": "string", "enum": ["", "block", "ask", "allow", "direct"]},
    "trust_tier": {"type": "integer", "description": "0 ich, 1 eng, 2 bekannt, 3 fremd"},
    "relationship": {"type": "string"}, "notes": {"type": "string"},
    "share_availability": {"type": "string", "enum": ["", "none", "freebusy", "details"]},
    "share_location": {"type": "string", "enum": ["", "yes", "no"]},
    "share_school": {"type": "string", "enum": ["", "yes", "no"]},
    "share_contact": {"type": "string", "enum": ["", "yes", "no"]},
    "share_personal": {"type": "string", "enum": ["", "yes", "no"]},
    "group_trigger": {"type": "string", "enum": ["mention", "always", "keywords", "off"]},
    "group_role": {"type": "string", "enum": ["assistant", "moderator", "listener"]},
    "group_keywords": {"type": "string"}, "group_aliases": {"type": "string"},
    "active_mode": {"type": "string", "enum": ["inherit", "always", "never", "window"]},
    "active_start": {"type": "string"}, "active_end": {"type": "string"},
    "model_tier": {"type": "string", "enum": ["", "small", "medium", "heavy", "code"]},
}


async def _card_update(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return tool_result(ok=False, summary="name fehlt.", source="contacts")
    card, why = await _resolve(name)
    created = False
    if not card:
        if why.startswith("ambiguous"):
            return tool_result(ok=False, summary="Mehrdeutig — meinst du: " + why.split(":", 1)[1], source="contacts")
        if not args.get("create"):
            return tool_result(ok=False, source="contacts", summary=(
                f"Keine Karte für '{name}'. Mit create=true (und handle='+49…', channel) neu anlegen."))
        handle = str(args.get("handle") or "").strip()
        kind = "group" if args.get("kind") == "group" else "person"
        card = cards.new_card(kind, name, cards.parse_handles(
            f'{args.get("channel") or "whatsapp"}: {handle}') if handle else [])
        created = True
    patch = {k: args[k] for k in cards.PATCH_FIELDS if k in args}
    if not patch and not created:
        return tool_result(ok=False, summary="Keine Änderung angegeben.", source="contacts")
    new, changed = cards.apply_patch(card, patch)
    saved = await cards.save_card(new)
    await db.audit("card_saved", actor="astra", detail={"key": saved["key"], "fields": changed, "created": created})
    return tool_result(ok=True, source="contacts", data=_detail(saved), summary=(
        ("Neue Karte angelegt. " if created else "") + f"{saved['name']}: " + (", ".join(changed) or "unverändert") + " gesetzt."))


async def _context_forget(args: dict, ctx: ToolContext) -> str:
    card, why = await _resolve(str(args.get("name") or ""))
    if not card:
        return tool_result(ok=False, summary="Keine (eindeutige) Karte gefunden.", source="contacts")
    kind = "groups" if card["kind"] == "group" else "contacts"
    removed = sum(digest.forget(kind, stem) for stem in digest.card_stems(card))
    await db.audit("card_forgotten", actor="astra", detail={"key": card["key"], "files": removed})
    return tool_result(ok=True, summary=f"Journal und Zusammenfassung von {card['name']} gelöscht ({removed} Dateien).",
                       source="contacts")


# ─── Prompts ──────────────────────────────────────────────────────────────────
async def _prompt_show(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "")
    if name not in prompts.NAMES:
        return tool_result(ok=False, summary="Bausteine: " + ", ".join(prompts.NAMES), source="prompts")
    return tool_result(ok=True, summary=prompts.get(name), source="prompts",
                       data={"overridden": prompts.is_overridden(name), "pending": len(
                           [p for p in prompts.proposals("pending") if p["name"] == name])})


async def _prompt_propose(args: dict, ctx: ToolContext) -> str:
    name = str(args.get("name") or "")
    if name not in prompts.NAMES:
        return tool_result(ok=False, summary="Bausteine: " + ", ".join(prompts.NAMES), source="prompts")
    pid, problems = prompts.propose(name, str(args.get("text") or ""), str(args.get("rationale") or ""),
                                    author="astra")
    if problems:
        return tool_result(ok=False, summary="Vorschlag abgelehnt: " + " ".join(problems), source="prompts")
    await db.audit("prompt_proposed", actor="astra", detail={"name": name, "id": pid})
    return tool_result(ok=True, source="prompts", data={"id": pid}, summary=(
        f"Vorschlag {pid} gespeichert. Er wirkt NICHT, bis Bahrian ihn unter Admin → Prompts freigibt."))


# ─── Google-Konten ────────────────────────────────────────────────────────────
async def _google_accounts(args: dict, ctx: ToolContext) -> str:
    await google_hub.load()
    summ = google_hub.summary()
    if not summ["accounts"]:
        return tool_result(ok=True, summary="Kein Google-Konto verbunden (Admin → Google).", data=[], source="google")
    lines = [f'{"★ " if a["default"] else ""}{a["email"]} — ' + (", ".join(google_hub.PRODUCTS[p].label for p in a["products"]) or "keine Produkte")
             + ("" if a["status"] == "ok" else " (neu verbinden!)") for a in summ["accounts"]]
    return tool_result(ok=True, summary="\n".join(lines), source="google",
                       data=[{"id": a["id"], "email": a["email"], "default": a["default"], "products": a["products"],
                              "status": a["status"]} for a in summ["accounts"]])


async def _google_default(args: dict, ctx: ToolContext) -> str:
    await google_hub.load()
    q = str(args.get("account") or "").strip().lower()
    hits = [a for a in google_hub.summary()["accounts"] if q and (q == a["id"] or q in a["email"].lower())]
    if len(hits) != 1:
        return tool_result(ok=False, source="google", summary=(
            "Kein eindeutiges Konto gefunden." if not hits else "Mehrdeutig: " + ", ".join(a["email"] for a in hits)))
    await google_hub.set_default(hits[0]["id"])
    await db.audit("google_default_set", actor="astra", detail={"account": hits[0]["id"]})
    return tool_result(ok=True, source="google", summary=f"Standard-Google-Konto ist jetzt {hits[0]['email']}.")


async def _google_test(args: dict, ctx: ToolContext) -> str:
    await google_hub.load()
    q = str(args.get("account") or "").strip().lower()
    accts = google_hub.summary()["accounts"]
    hit = next((a for a in accts if q and (q == a["id"] or q in a["email"].lower())), None) if q else \
        next((a for a in accts if a["default"]), None)
    if not hit:
        return tool_result(ok=False, source="google", summary="Kein Google-Konto gefunden.")
    results = [await google_hub.probe(hit["id"], p) for p in hit["products"]]
    lines = [("✅ " if r["ok"] else "❌ ") + r["message"] + (f" ({r['action_url']})" if r["action_url"] else "") for r in results]
    return tool_result(ok=all(r["ok"] for r in results), source="google",
                       summary=f"{hit['email']}:\n" + ("\n".join(lines) or "keine Produkte freigegeben"))


def register_chief_tools() -> None:
    def reg(**kw):
        register(Tool(owner_only=True, **kw))

    reg(name="usage_report", handler=_usage_report, safety="private_read", intents=["status"],
        description="Verbrauch und Kosten der Sprachmodelle. period: today|week|month|all; "
                    "by: model|purpose|channel|chat_id|provider|role|day.",
        parameters={"type": "object", "properties": {
            "period": {"type": "string", "enum": list(usage.PERIODS)},
            "by": {"type": "string", "enum": list(usage.GROUPS)}}},
        examples=["Was hat mich ASTRA diese Woche gekostet?"])
    reg(name="secretary_switch", handler=_secretary_switch, safety="mutation", intents=["control"],
        description="Secretary (Antworten an Dritte) an/aus/auto schalten oder Status lesen. Befristet mit "
                    "minutes oder until='HH:MM' (danach gilt wieder der vorherige Modus).",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["on", "off", "auto", "status"]},
            "minutes": {"type": "number"}, "until": {"type": "string"}, "tomorrow": {"type": "boolean"}},
            "required": ["action"]},
        examples=["Schalte den Secretary bis 18 Uhr aus"])
    reg(name="contact_cards_list", handler=_cards_list, safety="private_read", intents=["search"],
        description="Alle Personen- und Gruppenkarten (Vertrauensstufe, Regel, Stil). kind: person|group.",
        parameters={"type": "object", "properties": {"kind": {"type": "string", "enum": ["person", "group"]}}})
    reg(name="contact_card_get", handler=_card_get, safety="private_read", intents=["search"],
        description="Eine Kontaktkarte lesen (Name, Schlüssel oder Nummer).",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
    reg(name="contact_card_update", handler=_card_update, safety="mutation", intents=["control"],
        description="Kontaktkarte ändern: Ton/Stil, Anweisung, Regel, Vertrauensstufe, Freigaben (was die Person "
                    "erfahren darf), Gruppen-Trigger/-Rolle, Aktivzeiten, Modell. Existiert die Karte nicht: "
                    "create=true mit channel+handle (Gruppen: kind='group').",
        parameters={"type": "object", "properties": {
            "name": {"type": "string"}, "create": {"type": "boolean"}, "kind": {"type": "string"},
            "channel": {"type": "string"}, "handle": {"type": "string"}, **_UPDATE_PROPS}, "required": ["name"]},
        examples=["Lena darf nicht wissen, wo ich bin", "Antworte in der Astro-Gruppe nur, wenn ich erwähnt werde"])
    reg(name="context_forget", handler=_context_forget, safety="destructive", intents=["control"],
        description="Journal, Rohlog und Zusammenfassung einer Person/Gruppe restlos löschen.",
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
    reg(name="google_accounts", handler=_google_accounts, safety="private_read", intents=["status"],
        description="Verbundene Google-Konten mit freigegebenen Produkten (Kalender, Aufgaben, Gmail) und Standardkonto.",
        parameters={"type": "object", "properties": {}})
    reg(name="google_set_default_account", handler=_google_default, safety="mutation", intents=["control"],
        description="Standard-Google-Konto wechseln (E-Mail oder Teil davon).",
        parameters={"type": "object", "properties": {"account": {"type": "string"}}, "required": ["account"]},
        examples=["Nimm ab jetzt mein Schulkonto bei Google"])
    reg(name="google_test", handler=_google_test, safety="private_read", intents=["status"],
        description="Google-Zugriff eines Kontos prüfen (Kalender/Aufgaben/Gmail) und Fehlerursache erklären.",
        parameters={"type": "object", "properties": {"account": {"type": "string"}}})
    reg(name="prompt_show", handler=_prompt_show, safety="private_read", intents=["search"],
        description="Einen System-Prompt-Baustein lesen: " + ", ".join(prompts.NAMES) + ".",
        parameters={"type": "object", "properties": {"name": {"type": "string", "enum": list(prompts.NAMES)}},
                    "required": ["name"]})
    reg(name="prompt_propose", handler=_prompt_propose, safety="mutation", intents=["control"],
        description="Eine Verbesserung an einem System-Prompt VORSCHLAGEN (kompletter neuer Text + Begründung). "
                    "Wirkt nie sofort — Bahrian gibt im Admin frei. Sicherheitskern und Platzhalter müssen bleiben.",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "enum": list(prompts.NAMES)}, "text": {"type": "string"},
            "rationale": {"type": "string"}}, "required": ["name", "text", "rationale"]})
