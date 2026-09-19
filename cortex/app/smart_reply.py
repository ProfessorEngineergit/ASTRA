"""Smart-Antwort — wann ASTRA wartet, antwortet oder bewusst schweigt.

Ziel (Bahrian): ASTRA ist „immer an“, soll sich aber wie ein guter Assistent verhalten und nicht wie ein
Bot, der auf alles antwortet. Der Ablauf pro Chat mit einer Person:

  1. Frische Unterhaltung: ASTRA wartet erst ~1 Minute, ob DU antwortest. Antwortest du, bleibt ASTRA still.
  2. Danach hängt es von der Art der Nachricht ab:
       · Anfrage (Kalender/Termine/Verabredung/Ausrichten …)  → ASTRA beantwortet sie.
       · „Bist du da?“ / Hallo / Smalltalk                   → EINE Vorstellung: „Ich bin Bahrians KI-Assistent,
         er hat sich noch nicht gemeldet, ich kann zu Kalender/Terminen helfen — frag mich gern.“ Danach Ruhe.
       · Emoji, GIF, Sticker, Link, „ok“, „danke“, Lachen      → gar keine Antwort (so wirkt nichts „gelesen und ignoriert“).
  3. Läuft die Unterhaltung mit ASTRA (letzte Antwort < 30 Min.), antwortet ASTRA auf weitere Anfragen sofort;
     Smalltalk dazwischen wird ignoriert.
  4. Nach der Vorstellung schweigt ASTRA zu Smalltalk für eine Ruhephase (Standard 3 Std.) — nur eine konkrete
     Anfrage bekommt wieder eine Antwort.
  5. Greifst DU ein (schreibst der Person selbst), hört ASTRA sofort auf; beim nächsten Mal wartet es wieder.

Reine Logik ohne I/O (testbar): `classify`, `decide`, `meta_after_reply`, `meta_after_owner`, `capability_note`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

NOISE, THANKS, PING, REQUEST, CHAT = "noise", "thanks", "ping", "request", "chat"
_PRIORITY = {PING: 1, CHAT: 2, REQUEST: 3}

DEFAULTS = {"enabled": True, "wait_seconds": 60, "conversation_minutes": 30, "quiet_minutes": 180,
            "ignore_noise": True, "keep_unread": True}
SMART_CHANNELS = ("waha", "signal", "slack")


def settings(app_settings: dict | None) -> dict:
    raw = (((app_settings or {}).get("secretary") or {}).get("smart")) or {}
    out = dict(DEFAULTS)

    def num(key: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(float(raw.get(key, DEFAULTS[key])))))
        except (TypeError, ValueError):
            return DEFAULTS[key]
    out["wait_seconds"] = num("wait_seconds", 5, 900)
    out["conversation_minutes"] = num("conversation_minutes", 1, 720)
    out["quiet_minutes"] = num("quiet_minutes", 5, 2880)
    for k in ("enabled", "ignore_noise", "keep_unread"):
        if k in raw:
            out[k] = bool(raw[k])
    return out


def applies(secretary: dict, channel: str, card: dict | None, cfg: dict) -> bool:
    """Gilt das Warten/Filtern für diesen Chat? `secretary` = secretary.secretary_settings(...).

    Ausdrückliche Wahl gewinnt: Kanalmodus „direkt/warten/immer fragen“ und Karten-Regel „direkt/fragen“
    bleiben, wie sie sind. Smart gilt bei Kanalmodus „smart“ oder bei „immer an“ mit Standard-Kanalmodus."""
    if not cfg.get("enabled") or channel not in SMART_CHANNELS:
        return False
    if (card or {}).get("rule") in ("direct", "ask", "block"):
        return False
    mode = ((secretary.get("channels") or {}).get(channel) or {}).get("mode", "policy")
    if mode == "smart":
        return True
    if mode in ("direct", "wait", "always_ask"):
        return False
    return secretary.get("activation_mode") == "on"


# ─── Nachrichtenart ───────────────────────────────────────────────────────────
_EMOJI_CATS = {"So", "Sk", "Cf", "Mn", "Me", "Cs", "Co"}
_FILLER = re.compile(r"^(?:(?:a?ha)+h?|he+h?e*|lol+|lmao+|rofl+|xd+|hm+|mh+|jo+|joa+|jup+|jep+|nice+|geil|krass|wtf|omg|"
                     r"ups|oha|uff|puh|yay+|yeah+|wow+|ach so|aha+|oh+)$")
_ACK_WORDS = frozenset((
    "ok okay oki kk k alles klar gut passt super cool top perfekt prima klasse danke dankeschön dankeschoen dankee "
    "schön schoen dir dann vielen dank thx thanks merci gerne bitte bis später spaeter gleich morgen ciao tschüss "
    "tschau tschuess gute nacht gn8 bye schönen schoenen abend tag hab dich lieb sehr ja-ok nice geil").split())
# „ok“, „danke“, „super danke“, „alles klar bis später“ … (nur Höflichkeitsfloskeln, höchstens 5 Wörter)
def _is_ack(low: str) -> bool:
    words = re.findall(r"[a-zäöüß0-9\-]+", low)
    return 0 < len(words) <= 5 and all(w in _ACK_WORDS for w in words)


_URL_ONLY = re.compile(r"^(?:https?://\S+\s*)+$", re.I)
_DAYWORDS = re.compile(
    r"\b(heute|morgen|übermorgen|uebermorgen|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
    r"wochenende|nachmittag|vormittag|abend|nächste woche|naechste woche)\b|\b\d{1,2}[:.]\d{2}\b|"
    r"\b\d{1,2}\s?uhr\b|\b\d{1,2}\.\d{1,2}\.", re.I)
_ADDRESS = re.compile(r"\b(astra|assistent|assistant|ki-?agent|bot)\b", re.I)
_REQUEST = re.compile(
    r"\b(kalender|termin\w*|verfügbar\w*|verfuegbar\w*|beschäftigt|beschaeftigt|frei|treffen|treff|verabred\w*|"
    r"stundenplan|unterricht|schule|hausaufgabe\w*|vertretung|ausrichten|richte\w*(?: \w+){0,2} aus|"
    r"absagen|zusagen|verschieb\w*|dringend|wichtig|erreichbar|rückruf|rueckruf|zurückrufen|zurueckrufen|"
    r"wann|wo ist|kommt er|kommst du|hat er|hat bahrian|kann er|kann bahrian|ist er|ist bahrian|"
    r"nachricht (?:an|für|fuer) ihn|sag(?:e)? ihm|sag(?:e)? bahrian)\b", re.I)
_NAME = re.compile(r"\b(bahrian|bahri|bahr|astra|@\d+|@bahrian)\b[,:!]?", re.I)
_PING_PARTS = [re.compile(p, re.I) for p in (
    r"^(?:hey+|hi+|hallo+|hello|moin+|servus|yo+|huhu|na|sup|hey ho|guten (?:morgen|tag|abend)|grüß dich|gruess dich)$",
    r"^bist du (?:da|online|erreichbar|wach|dran|am handy|zu sprechen|frei|noch da|gerade da|zuhause|zu hause)$",
    r"^(?:ist|bist) (?:er|bahrian|jemand) (?:da|online|erreichbar)$",
    r"^kann ich (?:dich |mit dir |kurz |mal |gerade |jetzt |bitte )*(?:was |etwas |dich mal |kurz )*"
    r"(?:fragen|sprechen|reden|anrufen|stören|stoeren|schreiben|texten)$",
    r"^(?:hast|hättest|haettest) du (?:kurz |mal |gerade |jetzt |eine )*(?:zeit|minute|moment|sekunde)$",
    r"^(?:hörst|hoerst) du mich$", r"^meld(?:e)? dich(?: mal)?$", r"^antwort(?:e)?(?: mal)?$",
    r"^(?:ping|test|hello\?+|halloo+|\?+|hallo\?+)$", r"^(?:wie gehts|wie geht'?s|was geht|was los|alles gut|wie läuft'?s)$",
    r"^(?:ich )?(?:muss|möchte|will|wollte) (?:dich )?(?:kurz |mal )?(?:was )?(?:fragen|sprechen|reden)$",
)]


def _strip_emoji(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in _EMOJI_CATS)


def _has_emoji(text: str) -> bool:
    return any(unicodedata.category(ch) in _EMOJI_CATS for ch in text)


def _sentences(text: str) -> list[str]:
    parts = [p.strip(" \t,;:-—–") for p in re.split(r"[.!?,;\n]+", text)]
    return [p for p in parts if p]


def is_ping(text: str) -> bool:
    """Nur Begrüßung / „bist du da“ / „kann ich mit dir reden“ — ohne konkretes Anliegen. Rein."""
    low = _strip_emoji(text).lower().strip()
    if not low or _DAYWORDS.search(low):
        return False
    sents = _sentences(low)
    if not sents:
        return bool(re.fullmatch(r"\?+", low))
    for s in sents:
        s = re.sub(r"\s+", " ", _NAME.sub(" ", s)).strip(" ,:")
        if not s or any(p.fullmatch(s) for p in _PING_PARTS):
            continue
        return False
    return True


def classify(text: str) -> str:
    """Art der Nachricht: noise | thanks | ping | request | chat. Rein und ohne Modell (kein Token)."""
    raw = (text or "").strip()
    if not raw:
        return NOISE
    plain = _strip_emoji(raw)
    stripped = re.sub(r"[\s.!?,;:…\-–—~*_()\[\]\"'`^°]+", "", plain)
    if not stripped and _has_emoji(raw):
        return NOISE                                              # nur Emoji / Zeichen
    if not stripped:
        return NOISE if not re.fullmatch(r"\?+", raw.strip()) else PING
    if _URL_ONLY.match(raw):
        return NOISE                                              # nur ein Link (Meme/GIF/Video)
    low = re.sub(r"\s+", " ", plain.lower()).strip(" .!?,;:…~")
    if len(low) <= 22 and _FILLER.fullmatch(low):
        return NOISE
    if len(low) <= 40 and _is_ack(low):
        return THANKS
    if is_ping(raw):
        return PING
    if _ADDRESS.search(low) or _REQUEST.search(low) or _DAYWORDS.search(low):
        return REQUEST
    return CHAT


def merge_kind(old: str, new: str) -> str:
    """Kommt während des Wartens noch etwas dazu, gilt die „stärkere“ Art (Anfrage > Smalltalk > Begrüßung)."""
    return new if _PRIORITY.get(new, 0) >= _PRIORITY.get(old, 0) else old


# ─── Entscheidung ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verdict:
    action: str      # ignore | wait | reply
    kind: str
    reason: str


def awaiting_answer(history: list[dict]) -> bool:
    """Hat ASTRA zuletzt eine Rückfrage gestellt (letzte ASTRA-Nachricht endet mit „?“)? Rein."""
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            text = _strip_emoji(str(m.get("content") or "")).strip()
            return text.endswith("?")
    return False


def decide(kind: str, *, smart: bool, ignore_noise: bool, state: str, meta: dict, now: float,
           awaiting: bool = False) -> Verdict:
    """Was tun mit einer eingehenden Nachricht? `state` = Thread-Zustand VOR dieser Nachricht.

    `awaiting`: ASTRA hat gerade eine Rückfrage gestellt — dann ist auch ein kurzes „ja“/„gerne“/„nein“ eine Antwort."""
    active = state == "answered" and float(meta.get("smart_until") or 0) > now
    quiet = float(meta.get("quiet_until") or 0) > now
    answering = awaiting and (active or quiet)
    if ignore_noise and kind == NOISE:
        return Verdict("ignore", kind, "smart-noise")
    if ignore_noise and kind == THANKS and not answering:
        return Verdict("ignore", kind, "smart-ack")
    if not smart:
        return Verdict("reply", kind, "smart-off")
    if answering and kind in (THANKS, CHAT, PING):
        return Verdict("reply", kind, "smart-answer")
    if kind == REQUEST:
        if active or quiet:
            return Verdict("reply", kind, "smart-active" if active else "smart-quiet-request")
        return Verdict("wait", kind, "smart-wait-request")
    if state == "deferred":                     # wir warten schon — nur die Art merken
        return Verdict("wait", kind, "smart-wait-more")
    if active or quiet:
        return Verdict("ignore", kind, "smart-quiet")
    return Verdict("wait", kind, "smart-wait-intro")


def meta_after_reply(kind: str, cfg: dict, now: float) -> dict:
    """Marker nach ASTRAs Antwort. Vorstellung (ping/chat) → Ruhephase; Anfrage → laufendes Gespräch."""
    if kind in (PING, CHAT):
        return {"quiet_until": now + cfg["quiet_minutes"] * 60, "smart_until": now + cfg["conversation_minutes"] * 60,
                "smart_kind": ""}
    return {"smart_until": now + cfg["conversation_minutes"] * 60, "smart_kind": ""}


def meta_after_owner() -> dict:
    """Bahrian hat selbst geschrieben → ASTRA vergisst das laufende Gespräch (nächstes Mal wieder warten)."""
    return {"smart_until": 0, "quiet_until": 0, "smart_kind": ""}


# ─── Vorstellung ──────────────────────────────────────────────────────────────
def capability_note(*, calendar: bool, owner: str = "Bahrian") -> str:
    """Was ASTRA für diese Person WIRKLICH kann — die Vorstellung darf nichts versprechen, was nicht geht."""
    if calendar:
        return (f"Du hast Zugriff auf {owner}s Kalender (nur im freigegebenen Rahmen) und kannst Fragen zu Terminen und "
                f"Verfügbarkeit beantworten sowie Nachrichten an {owner} ausrichten.")
    return (f"Du hast aktuell KEINEN Zugriff auf {owner}s Kalender und kannst deshalb keine Terminauskünfte geben — "
            f"sag das ehrlich. Du kannst Nachrichten an {owner} ausrichten.")


def intro_instruction(*, calendar: bool, owner: str = "Bahrian") -> str:
    return (f"{owner} hat sich noch nicht gemeldet. Stelle dich JETZT einmal kurz vor (1–3 Sätze, im festgelegten Stil): "
            f"Du bist {owner}s KI-Assistent, er ist gerade offenbar nicht erreichbar bzw. hat noch nicht geantwortet. "
            + capability_note(calendar=calendar, owner=owner) +
            " Lade dazu ein, die Frage direkt an dich zu stellen. Beantworte keine Smalltalk-Fragen inhaltlich, "
            "erfinde nichts und verspreche keine Zusagen.")


def calendar_ready() -> bool:
    """Kann ASTRA Kalenderauskünfte geben? (Google-Kalender-Plugin an und verbunden, oder n8n-Fallback.)"""
    try:
        from . import google_oauth
        from .plugins.registry import get_manager
        plugin = get_manager().get("google_calendar")
        if plugin is None or not plugin.enabled:
            return False
        if str(plugin.get("backend") or "native") == "n8n":
            return True
        return bool(google_oauth.has_google_connection(plugin.cfg))
    except Exception:  # noqa: BLE001 — im Zweifel nichts versprechen
        return False
