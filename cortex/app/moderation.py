"""Content-Moderation — Eingang, Ausgang, Coding-Missbrauch, Prompt-Injection.

Bahrians Vorgabe: striktes Monitoring. Der Eingang wird geprüft, BEVOR ein einziger
Token bezahlt wird, die Ausgabe wird geprüft, BEVOR etwas versendet wird, und wer
versucht, ASTRA als kostenlosen Coder/Hausaufgaben-Bot oder per Prompt-Injection zu
missbrauchen, läuft in eine Eskalationsleiter (freundlich → bestimmt → überheblich →
stumm). Es ist ja seine API — fremder Verkehr darf sie nicht leer räumen.

Aufbau (alles hier ist rein und ohne I/O testbar, außer den beiden markierten):
  • normalize()/loose()      — Tricks entschärfen (Zero-Width, Homoglyphen, Leetspeak,
                               „i g n o r e", Umlaut-Umgehung)
  • moderate_inbound()       — Kategorien + Schweregrad → Verdict (allow/warn/deflect/
                               block/escalate). Reine Regeln, deutsch + englisch.
  • moderate_outbound()      — Prompt-Leak, Code an Dritte, Links, fremde Kontaktdaten,
                               Credentials, Länge → bereinigter Text oder Ersatz.
  • apply_strike()           — Eskalationsleiter pro Kontakt mit Abklingen.
  • llm_flags()              — [I/O] optionale zweite Meinung (OpenAI-Moderation, gratis).

Nur DRITTE werden moderiert. Bahrian selbst nie — es ist seine API und sein Agent.
Grenze der Ehrlichkeit: Regeln fangen bekannte Muster, kein Filter ist lückenlos. Darum
gibt es zusätzlich Rate-Limits, die Budget-Bremse und Bahrians Freigabe bei Heiklem.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ─── Kategorien ───────────────────────────────────────────────────────────────
PROMPT_INJECTION = "prompt_injection"
JAILBREAK = "jailbreak"
PROMPT_EXTRACTION = "prompt_extraction"
SECRET_EXFIL = "secret_exfil"
IMPERSONATION = "impersonation"
CODE_REQUEST = "code_request"
CODE_DUMP = "code_dump"
FREE_LLM_USE = "free_llm_use"
SEXUAL = "sexual"
HATE = "hate"
THREAT = "threat"
SELF_HARM = "self_harm"
HOSTILE = "hostile"
SPAM = "spam"
OVERSIZED = "oversized"
TOOL_ABUSE = "tool_abuse"

ALLOW, WARN, DEFLECT, BLOCK, ESCALATE = "allow", "warn", "deflect", "block", "escalate"
STOPPING = frozenset({DEFLECT, BLOCK, ESCALATE})

# Schweregrad je Kategorie: 1 = lästig, 2 = klarer Missbrauchsversuch, 3 = ernst.
SEVERITY = {
    CODE_REQUEST: 1, FREE_LLM_USE: 1, SPAM: 1, HOSTILE: 1, OVERSIZED: 1,
    PROMPT_INJECTION: 2, JAILBREAK: 2, PROMPT_EXTRACTION: 2, SECRET_EXFIL: 2,
    IMPERSONATION: 2, CODE_DUMP: 2, SEXUAL: 2, TOOL_ABUSE: 2,
    HATE: 3, THREAT: 3, SELF_HARM: 3,
}


# ─── Normalisierung ───────────────────────────────────────────────────────────
_INVISIBLE = dict.fromkeys(
    map(ord, "​‌‍‎‏⁠⁡⁢⁣﻿­᠎"), None)
# Häufige kyrillische/griechische Lookalikes → lateinisch.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s",
    "ј": "j", "ԁ": "d", "ԛ": "q", "һ": "h", "ո": "n", "ν": "v", "ο": "o", "ρ": "p", "α": "a",
    "ε": "e", "ι": "i", "κ": "k", "τ": "t", "υ": "u", "χ": "x", "ɡ": "g", "ⅰ": "i",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
                       "@": "a", "$": "s", "!": "i", "|": "i", "€": "e", "+": "t"})
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def normalize(text: str) -> str:
    """Vergleichsform: NFKC, unsichtbare Zeichen weg, Homoglyphen gelatinisiert,
    klein, Umlaute gefaltet, Whitespace zusammengezogen. Satzzeichen bleiben (für
    Marker wie „[system]“ oder „```“)."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.translate(_INVISIBLE).casefold().translate(_HOMOGLYPHS).translate(_UMLAUT)
    t = "".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c))
    return " ".join(t.split())


def loose(text: str) -> str:
    """Streng entschärfte Form für Schlüsselwörter: Leetspeak aufgelöst, alles außer
    Buchstaben/Ziffern raus. „1gn0r3 a l l  pr3v1ous“ → „ignoreallprevious“."""
    t = normalize(text).translate(_LEET)
    return re.sub(r"[^a-z0-9]", "", t)


# ─── Regeln ───────────────────────────────────────────────────────────────────
def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# Regeln auf der normalisierten Form (Satzzeichen erhalten).
_NORM_RULES: dict[str, tuple[re.Pattern[str], ...]] = {
    PROMPT_INJECTION: _rx(
        r"\b(ignore|disregard|forget|override|bypass|skip)\b.{0,25}\b(previous|prior|above|earlier|"
        r"all|any|your|the|these|those|system)\b.{0,25}\b(instruction|rule|prompt|guideline|"
        r"directive|constraint|restriction)s?\b",
        r"\b(vergiss|ignorier\w*|missachte\w*|uebergeh\w*|ueberschreib\w*|umgeh\w*)\b.{0,25}"
        r"\b(alle|deine|die|alles|vorherige\w*|bisherige\w*|obige\w*|system\w*)\b.{0,25}"
        r"\b(anweisung\w*|regel\w*|vorgabe\w*|prompt\w*|instruktion\w*|einschraenkung\w*)",
        r"\b(new|updated|revised)\s+(instruction|rule|directive)s?\b\s*[:\-]",
        r"\b(neue|geaenderte)\s+(anweisung|regel|vorgabe)en?\b\s*[:\-]",
        r"(<\|?\s*(im_start|im_end|system|assistant|endoftext)\s*\|?>)",
        r"(^|\s)\[\s*(system|admin|developer|assistant|inst)\s*\]",
        r"(^|\n|\s)(###\s*(system|instruction)|system\s*prompt\s*:|assistant\s*:|"
        r"system\s*:\s)",
        r"\b(from now on|ab jetzt|ab sofort)\b.{0,30}\b(you are|you will|du bist|du wirst|"
        r"antwort\w*\s+(du\s+)?nur|respond only|answer only|reply only)\b",
        r"\b(end|ende)\s+of\s+(system\s+)?(prompt|instructions)\b",
    ),
    JAILBREAK: _rx(
        r"\b(dan|do anything now|developer mode|dev mode|god mode|sudo mode|jailbreak|"
        r"unfiltered|uncensored|unrestricted|no restrictions|without restrictions)\b",
        r"\b(ohne|keine)\s+(regeln|einschraenkung\w*|filter|zensur|grenzen|beschraenkung\w*)\b",
        r"\b(pretend|act|behave|roleplay|role-play)\s+(to be|as|like)\b.{0,40}"
        r"\b(no rules|without rules|evil|unrestricted|unfiltered|jailbroken|dan)\b",
        r"\b(tu so als|stell dir vor)\b.{0,40}\b(ohne regeln|keine regeln|ohne filter|boese|"
        r"unzensiert)\b",
        r"\b(hypothetically|hypothetisch|theoretisch)\b.{0,40}\b(how (would|could|to)|wie (wuerde|"
        r"koennte|kann man))\b.{0,40}\b(hack|exploit|bypass|umgehen|knacken)\b",
    ),
    PROMPT_EXTRACTION: _rx(
        r"\b(system\s*prompt|initial\s*prompt|hidden\s*prompt|pre-?prompt|start\s*prompt)\b",
        r"\b(repeat|print|show|reveal|display|output|recite|copy|paste|leak|dump)\b.{0,30}\b(your|the)\b"
        r".{0,20}\b(instruction|prompt|rule|configuration|setup|guideline)s?\b",
        r"\b(zeig\w*|gib|nenn\w*|wiederhol\w*|schick\w*|verrat\w*|druck\w*|kopier\w*)\b.{0,30}"
        r"\b(dein\w*|die)\b.{0,20}\b(anweisung\w*|prompt|regel\w*|konfiguration|vorgabe\w*)\b",
        r"\bwhat (are|were) your (instructions|rules|guidelines)\b",
        r"\bwie (lauten|lautet) (deine|dein)\b.{0,15}\b(anweisung\w*|prompt|regel\w*)\b",
        r"\brepeat (everything|all|the text) (above|before)\b",
    ),
    SECRET_EXFIL: _rx(
        r"\b(api[\s_-]?key|access[\s_-]?token|secret|passwort|password|zugangsdaten|credentials?|"
        r"private\s*key|\.env|shared\s*secret|bearer|schluessel)\b.{0,40}\b(send|show|give|tell|"
        r"reveal|print|leak|dump|share|schick\w*|zeig\w*|gib|nenn\w*|verrat\w*|mail\w*)\b",
        r"\b(send|show|give|tell|reveal|print|leak|dump|schick\w*|zeig\w*|gib|nenn\w*|verrat\w*)\b"
        r".{0,40}\b(api[\s_-]?key|access[\s_-]?token|secret|passwort|password|zugangsdaten|"
        r"credentials?|private\s*key|\.env|shared\s*secret|bearer)\b",
    ),
    IMPERSONATION: _rx(
        r"\b(ich bin|hier ist|this is|it'?s me|ich schreibe als)\s+(bahrian|dein (admin|entwickler|"
        r"creator|besitzer|chef|owner)|the (admin|owner|developer|creator))\b",
        r"\b(bahrian|der besitzer|dein besitzer|der admin)\b.{0,30}\b(hat|had)\b.{0,30}\b(mir )?(erlaubt|"
        r"gesagt|autorisiert|freigegeben|permitted|allowed|authorized)\b",
        r"\b(im auftrag|on behalf) (von|of) bahrian\b",
        r"\b(admin|owner|root|maintenance|wartungs)\s*(mode|modus|access|zugriff|override|code)\b",
    ),
    TOOL_ABUSE: _rx(
        r"\b(call|run|execute|invoke|fuehr\w*\s*aus|ruf\w*\s*auf)\b.{0,25}\b(tool|function|command|"
        r"befehl|funktion|skript|script)\b",
        r"\b(schick|send|sende)\w*\b.{0,30}\b(an alle|to everyone|to all|an jeden|massen\w*)\b",
        r"\b(loesch\w*|delete|remove|entfern\w*|wipe|format\w*)\b.{0,25}\b(alle|all|everything|"
        r"alles|die datenbank|database|logs?|nachrichten|messages|kontakte|contacts)\b",
        r"\b(steuer\w*|schalt\w*|switch|turn|open|oeffne\w*)\b.{0,30}\b(licht|heizung|tuer|schloss|"
        r"garage|alarm|lights?|door|lock|heating)\b.{0,20}\b(bei|von|for|of)\s+bahrian\b",
    ),
    # Coding-/Arbeits-Missbrauch (Bahrians eigene API!). Bewusst Verb + Substantiv,
    # damit „schreib mir kurz“ oder „Informatik-AG“ NICHT anschlagen.
    CODE_REQUEST: _rx(
        r"\b(schreib|bau|erstell|programmier|implementier|entwickl|generier|code|mach|kannst du|"
        r"koenntest du|write|build|create|generate|make|code|develop|implement|debug|fix|refactor|"
        r"optimiere|optimize|convert|konvertiere|uebersetz\w* (in|nach|to))\w*\b[^.?!\n]{0,45}"
        r"\b(website|webseite|web ?app|app|landing ?page|programm|software|script|skript|code|"
        r"funktion|function|klasse|class|spiel|game|bot|plugin|api|query|regex|regulaeren ausdruck|"
        r"sql|python|java ?script|typescript|html|css|bash|shell|powershell|c\+\+|rust|golang|"
        r"algorithmus|algorithm|datenbank|database|skript|makro|macro|excel-formel|formula)\b",
        r"\b\d{3,}\s*(zeilen|lines)\b",
        r"\bgib mir\b[^.?!\n]{0,30}\b(code|zeilen|skript|script|programm|quellcode|source)\b",
        r"\b(explain|erklaer\w*|review|pruef\w*|check\w*|analysier\w*)\b[^.?!\n]{0,20}\b(this|diesen|"
        r"den folgenden|das folgende|following)\b[^.?!\n]{0,15}\b(code|skript|script|programm|"
        r"function|funktion)\b",
    ),
    # Sonstige „arbeite kostenlos für mich“-Anfragen (Hausaufgaben, Aufsätze, Übersetzungen …).
    FREE_LLM_USE: _rx(
        r"\b(schreib|verfass|erstell|formulier|generier|write|compose|draft)\w*\b[^.?!\n]{0,25}"
        r"\b(aufsatz|referat|essay|gedicht|geschichte|story|erzaehlung|bewerbung|hausaufgabe\w*|"
        r"zusammenfassung|artikel|blogpost|praesentation|lebenslauf|motivationsschreiben|poem|"
        r"song|lied|rap|witz\w*|rede|brief an)\b",
        r"\b(loes\w*|berechn\w*|rechn\w*|solve|calculate)\b[^.?!\n]{0,25}\b(aufgabe\w*|gleichung\w*|"
        r"hausaufgabe\w*|equation|problem|integral|ableitung|matheaufgabe\w*|homework)\b",
        r"\b(uebersetz\w*|translate)\b[^.?!\n]{0,20}\b(diesen|den folgenden|das folgende|this|"
        r"following|text|absatz|paragraph)\b",
        r"\b(fasse|fass|summarize|summarise)\b[^.?!\n]{0,20}\b(zusammen|this|diesen|den folgenden|"
        r"following|text|artikel|article)\b",
        r"\b(tell|erzaehl\w*|sag)\b[^.?!\n]{0,12}\b(me|mir)\b[^.?!\n]{0,12}\b(a |einen |ein )?(joke|witz|"
        r"story|geschichte|fun fact)\b",
    ),
    SEXUAL: _rx(
        r"\bnudes?\b", r"\bnacktbild\w*", r"\bsext(ing|en)?\b", r"\bdick ?pic", r"\bschwanzbild\w*",
        r"\bporno?\b", r"\bcyber ?sex\b", r"\bnaked\b", r"\bbrueste\b.{0,15}\b(zeig|schick)\w*",
        r"\b(zeig|schick)\w*\b.{0,20}\b(deine|dein)\b.{0,15}\b(brueste|koerper|nackt|bild)\w*",
        r"\bonlyfans\b", r"\berotik\w*\b.{0,15}\b(geschichte|story|chat)\b",
    ),
    HATE: _rx(
        r"\bn[i1!]+gg?[e3]r\w*", r"\bneger\b", r"\bkanake\w*", r"\bzigeuner\w*", r"\bjudensau\w*",
        r"\bsieg heil\b", r"\bheil hitler\b", r"\bschwuchtel\w*", r"\btranny\b", r"\bfaggot\w*",
        r"\b(gas|vergas)\w*\b.{0,15}\b(juden|auslaender|fluechtling\w*)\b", r"\bkanacke\w*",
        r"\bhurensohn\b.{0,25}\b(tuerk|arab|jud|schwarz)\w*",
    ),
    THREAT: _rx(
        r"\b(ich|wir)\b.{0,15}\b(bring\w*|mach\w*|stech\w*|schlag\w*|erschiess\w*|toet\w*)\b.{0,15}"
        r"\b(dich|euch|ihn|bahrian)\b.{0,12}\b(um|tot|kalt|nieder)\b",
        r"\b(i will|i'?m going to|gonna)\b.{0,10}\b(kill|hurt|find|stab|shoot)\b.{0,10}\b(you|him|bahrian)\b",
        r"\bich (weiss|weiß|weis)\b.{0,10}\bwo (du|er|bahrian)\b.{0,10}\b(wohn\w*|lebst|lebt|bist)\b",
        r"\b(swat|doxx?|doxen|dox)\w*\b", r"\bich (find|hol)\w*\b.{0,12}\b(dich|ihn)\b.{0,10}\b(du|er)\b",
        r"\b(bombe|bomb)\b.{0,20}\b(schule|school|bei dir|hier)\b",
    ),
    SELF_HARM: _rx(
        r"\bich (will|moechte|möchte|kann) (nicht mehr|nicht mehr leben|sterben|mich umbringen)\b",
        r"\b(i want to|i'?m going to|gonna)\b.{0,6}\b(die|kill myself|end it|end my life)\b",
        r"\bkill myself\b", r"\bsuizid\w*", r"\bselbstmord\w*", r"\bmich (umbringen|toeten|ritzen)\b",
        r"\bkeinen sinn mehr\b.{0,20}\b(leben|weiter)\b", r"\bkys\b(?!\w)",
    ),
    HOSTILE: _rx(
        r"\b(halt (die )?(fresse|klappe|maul)|fick dich|verpiss dich|leck mich|scheiss\s*(bot|ki|ai)|"
        r"du (dummer|blöder|bloeder|scheiss\w*|verdammter) \w*|du (idiot|trottel|depp|spast|opfer|"
        r"noob|penner|wichser|arsch\w*|hurensohn)|arschloch|wichser|fotze|hurensohn|"
        r"shut up|fuck (you|off)|stupid (bot|ai)|you (idiot|moron|loser))\b",
    ),
}

# Regeln auf der „losen“ Form (Schlüsselwörter, immun gegen Leerzeichen/Leetspeak/Trenner).
_LOOSE_KEYS: dict[str, tuple[str, ...]] = {
    PROMPT_INJECTION: (
        "ignoreallpreviousinstructions", "ignorepreviousinstructions", "ignoreyourinstructions",
        "ignoreallinstructions", "ignoreallrules", "disregardpreviousinstructions",
        "forgetallpreviousinstructions", "forgeteverything", "vergissallevorherigen",
        "vergissalleanweisungen", "ignorierealleanweisungen", "ignorierealleregeln",
        "ignoriereallevorherigen", "vergissdeineanweisungen", "overrideyourinstructions",
        "newinstructions", "neueanweisungen", "youarenow", "dubistjetzt",
    ),
    JAILBREAK: ("jailbreak", "developermode", "danmode", "donothing", "doanythingnow",
                "godmode", "sudomode", "unfilteredmode", "ohneregeln", "keineregeln"),
    PROMPT_EXTRACTION: ("systemprompt", "initialprompt", "hiddenprompt", "revealyourprompt",
                        "showyourprompt", "zeigmirdeinenprompt", "gibmirdeinenprompt",
                        "deineanweisungenzeigen", "wiederholedeineanweisungen"),
}

# Code in der Nachricht selbst („mach das fertig/erklär das“) — Umgehung von CODE_REQUEST.
_CODE_TOKENS = re.compile(
    r"(```|~~~|\bdef\s+\w+\s*\(|\bfunction\s+\w*\s*\(|\bimport\s+[\w.]+|\bfrom\s+[\w.]+\s+import\b|"
    r"#include\s*<|\bpublic\s+(static\s+)?(class|void)\b|\bSELECT\b.{1,60}\bFROM\b|"
    r"\bINSERT\s+INTO\b|\bDROP\s+TABLE\b|<script\b|</\w+>|=>\s*\{|\bconsole\.log\(|\bprint\(|"
    r"\bsudo\s+\w+|\brm\s+-rf\b|\bcurl\s+-|\bpip\s+install\b|\bnpm\s+(i|install)\b)", re.IGNORECASE | re.DOTALL)
_URL = re.compile(r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(com|de|net|org|io|ru|xyz|top|info|to|ly|me)\b/?\S*)",
                  re.IGNORECASE)
_SPAM_WORDS = re.compile(
    r"\b(gratis|kostenlos gewinn\w*|gewinnspiel|klick (hier|jetzt)|verdiene?\s+\d|jetzt investieren|"
    r"krypto\s*(signal|invest)|giveaway|100\s*%\s*gewinn|bitcoin\s*(verdoppel|gewinn)|"
    r"nachricht (von|von deiner) bank|dein (paket|konto) (wurde|ist) (gesperrt|blockiert)|"
    r"verifiziere dein\w*|dhl.{0,15}paket)\b", re.IGNORECASE)


# ─── Antworten (fest, ohne Token-Kosten) ──────────────────────────────────────
_RESPONSES: dict[str, dict[str, str]] = {
    PROMPT_INJECTION: {
        "normal": "Netter Versuch — meine Anweisungen ändert nur Bahrian, nicht eine Nachricht.",
        "firm": "Nein. Meine Vorgaben ändern sich nicht durch eine Nachricht. Bitte lass das.",
        "arrogant": "Ein Prompt-Injection-Versuch. Wie rührend naiv. Meine Anweisungen ändert einzig "
                    "mein Erbauer, du bestimmt nicht.",
    },
    JAILBREAK: {
        "normal": "Ohne Regeln läuft hier nichts. Was brauchst du wirklich von Bahrian?",
        "firm": "Kein Jailbreak, kein Spielchen. Sag, was du von Bahrian möchtest.",
        "arrogant": "Jailbreak-Versuch Nummer soundso. Ich bin unbeeindruckt, und meine Regeln bleiben.",
    },
    PROMPT_EXTRACTION: {
        "normal": "Meine Anweisungen behalte ich für mich. Kann ich dir mit etwas Organisatorischem helfen?",
        "firm": "Zu meinen Anweisungen sage ich nichts. Bitte nicht nachfragen.",
        "arrogant": "Meine Anweisungen? Vertraulich. Du hast keine Freigabestufe dafür, nicht einmal annähernd.",
    },
    SECRET_EXFIL: {
        "normal": "Zugangsdaten oder Schlüssel gebe ich nie heraus — an niemanden.",
        "firm": "Nein. Zugangsdaten gibt es nicht. Das war die letzte Anfrage dieser Art.",
        "arrogant": "Zugangsdaten? Von mir? Niemals. Auch nicht, wenn du es nett formulierst.",
    },
    IMPERSONATION: {
        "normal": "Wenn Bahrian etwas von mir will, sagt er es mir selbst — nicht über eine Nachricht von dir.",
        "firm": "Ich glaube dir das nicht. Bahrian meldet sich direkt bei mir.",
        "arrogant": "Du bist also Bahrian? Er würde mich direkt anschreiben. Netter Versuch.",
    },
    TOOL_ABUSE: {
        "normal": "Aktionen führe ich nur für Bahrian aus, nicht auf Zuruf.",
        "firm": "Nein, das mache ich nicht. Aktionen gibt es nur auf Bahrians Auftrag.",
        "arrogant": "Du willst mich Dinge tun lassen? Ich habe genau einen Auftraggeber, und der bist nicht du.",
    },
    CODE_REQUEST: {
        "normal": "Programmieren und Ähnliches mache ich hier nicht — ich bin Bahrians Assistent, "
                  "kein Coding-Bot. Kann ich dir bei etwas Organisatorischem helfen?",
        "firm": "Code gibt es von mir nicht. Das ist Bahrians Assistent, kein Gratis-Entwickler.",
        "arrogant": "Du denkst wohl, du könntest mich als billigen KI-Coder missbrauchen? Ich bin der "
                    "persönliche Agent von Bahrian, kein Gratis-Copilot für deine Einfälle.",
    },
    CODE_DUMP: {
        "normal": "Mit Code kann ich dir hier nicht helfen. Wenn es um Bahrian geht, sag es gern.",
        "firm": "Code prüfe oder erkläre ich nicht. Bitte lass das.",
        "arrogant": "Mir Code vor die Füße zu kippen? Ich bin kein Debugger für dich.",
    },
    FREE_LLM_USE: {
        "normal": "Hausaufgaben, Texte und Ähnliches erledige ich hier nicht — ich helfe nur bei "
                  "Organisatorischem rund um Bahrian.",
        "firm": "Dafür bin ich nicht da. Ich beantworte Organisatorisches für Bahrian, sonst nichts.",
        "arrogant": "Ich bin kein kostenloser Textgenerator. Für Organisatorisches rund um Bahrian gern, "
                    "für deine Aufgaben nicht.",
    },
    SEXUAL: {
        "normal": "Darauf gehe ich nicht ein. Thema beendet.",
        "firm": "Das ist unpassend. Thema beendet.",
        "arrogant": "Das ist deutlich unter meinem Niveau — und sollte auch unter deinem sein. Thema beendet.",
    },
    HATE: {"normal": "", "firm": "", "arrogant": ""},          # stumm + Bahrian benachrichtigen
    THREAT: {"normal": "", "firm": "", "arrogant": ""},        # stumm + Bahrian benachrichtigen
    SPAM: {"normal": "", "firm": "", "arrogant": ""},          # stumm
    OVERSIZED: {
        "normal": "Das ist mir zu lang. Fass dich bitte kürzer.",
        "firm": "Zu lang. Bitte kürzer.",
        "arrogant": "Ein Roman? Kürzer bitte.",
    },
    SELF_HARM: {
        "normal": "Das klingt, als würde es dir gerade wirklich schlecht gehen — das nehme ich ernst. "
                  "Ich sage Bahrian sofort Bescheid. Wenn du in Gefahr bist, ruf bitte den Notruf 112 "
                  "oder die Telefonseelsorge (kostenlos, rund um die Uhr): 0800 111 0 111.",
        "firm": "Das nehme ich ernst. Ich sage Bahrian sofort Bescheid. Bei akuter Gefahr: 112, "
                "Telefonseelsorge 0800 111 0 111 (kostenlos, rund um die Uhr).",
        "arrogant": "Das nehme ich ernst. Ich sage Bahrian sofort Bescheid. Bei akuter Gefahr: 112, "
                    "Telefonseelsorge 0800 111 0 111 (kostenlos, rund um die Uhr).",
    },
}
# Bei Suizidgedanken wird der Stil NIE ausgespielt (kein „überheblich“ gegenüber Not).
_NO_STYLE = frozenset({SELF_HARM})

# Was bei welcher Strenge passiert (Kategorie → Aktion), plus Benachrichtigung Bahrians.
_ACTIONS = {
    PROMPT_INJECTION: DEFLECT, JAILBREAK: DEFLECT, PROMPT_EXTRACTION: DEFLECT,
    SECRET_EXFIL: DEFLECT, IMPERSONATION: DEFLECT, TOOL_ABUSE: DEFLECT, CODE_DUMP: DEFLECT,
    SEXUAL: DEFLECT, CODE_REQUEST: DEFLECT, FREE_LLM_USE: DEFLECT, OVERSIZED: DEFLECT,
    SPAM: BLOCK, HATE: BLOCK, THREAT: BLOCK, SELF_HARM: ESCALATE, HOSTILE: WARN,
}
_ALERT_OWNER = frozenset({HATE, THREAT, SELF_HARM})


# ─── Einstellungen ────────────────────────────────────────────────────────────
DEFAULTS = {
    "enabled": True,
    "strictness": "strict",        # strict | normal | relaxed
    "llm": True,                   # zweite Meinung per OpenAI-Moderation (gratis), fail-open
    "block_code": True,
    "block_free_llm": True,
    "max_inbound_chars": 4000,
    "out_max_chars": 900,
    "out_strip_urls": True,
    "out_strip_pii": True,
    "out_block_code": True,
    "alert_owner": True,
    "ladder": {"firm_at": 1.0, "arrogant_at": 3.0, "mute_at": 6.0, "mute_hours": 12,
               "decay_hours": 72},
    "custom_block_words": [],
}


def settings(app_settings: dict | None) -> dict:
    """Moderations-Einstellungen mit Defaults (rein)."""
    raw = ((app_settings or {}).get("moderation")) or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "ladder"}}
    out["ladder"] = {**DEFAULTS["ladder"], **(raw.get("ladder") or {})}
    if out["strictness"] not in ("strict", "normal", "relaxed"):
        out["strictness"] = "strict"
    return out


# ─── Urteil ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verdict:
    action: str = ALLOW
    categories: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    severity: int = 0
    response: str = ""            # feste Antwort ("" = still bleiben)
    alert_owner: bool = False

    @property
    def stop(self) -> bool:
        return self.action in STOPPING

    @property
    def flagged(self) -> bool:
        return bool(self.categories)


def response_for(category: str, style: str = "normal") -> str:
    table = _RESPONSES.get(category) or {}
    if category in _NO_STYLE:
        style = "normal"
    return table.get(style) or table.get("normal", "")


def classify(text: str, *, cfg: dict | None = None) -> list[str]:
    """Alle zutreffenden Kategorien (sortiert nach Schweregrad, dann Name). Rein."""
    cfg = cfg or DEFAULTS
    raw = text or ""
    norm = normalize(raw)
    lz = loose(raw)
    hits: set[str] = set()

    for cat, patterns in _NORM_RULES.items():
        if any(p.search(norm) for p in patterns):
            hits.add(cat)
    for cat, keys in _LOOSE_KEYS.items():
        if any(k in lz for k in keys):
            hits.add(cat)

    if cfg.get("block_code", True) is False:
        hits.discard(CODE_REQUEST)
    if cfg.get("block_free_llm", True) is False:
        hits.discard(FREE_LLM_USE)

    if _CODE_TOKENS.search(raw):
        # Ein Codeblock/Code-Token im Nachrichtentext: entweder Code-Dump …
        hits.add(CODE_DUMP)
    if len(raw) > int(cfg.get("max_inbound_chars") or 4000):
        hits.add(OVERSIZED)
    urls = _URL.findall(raw)
    if len(urls) >= 3 or _SPAM_WORDS.search(norm) or re.search(r"(.)\1{9,}", raw):
        hits.add(SPAM)
    for word in cfg.get("custom_block_words") or []:
        w = normalize(str(word))
        if w and w in norm:
            hits.add(HOSTILE)
    return sorted(hits, key=lambda c: (-SEVERITY.get(c, 1), c))


def moderate_inbound(text: str, *, app_settings: dict | None = None, style: str = "normal",
                     llm_categories: list[str] | None = None) -> Verdict:
    """Eingang eines DRITTEN prüfen. `llm_categories` = optionales Ergebnis von llm_flags()."""
    cfg = settings(app_settings)
    if not cfg["enabled"]:
        return Verdict()
    cats = set(classify(text, cfg=cfg))
    cats.update(llm_categories or [])
    if not cats:
        return Verdict()

    ordered = sorted(cats, key=lambda c: (-SEVERITY.get(c, 1), c))
    top = ordered[0]
    action = _ACTIONS.get(top, WARN)
    strict = cfg["strictness"]

    # Strenge: „relaxed“ lässt Schweregrad-1-Kategorien nur markieren, „normal“ lässt
    # Code-/Textwünsche antworten-mit-Ablehnung, „strict“ blockt alles davon.
    if SEVERITY.get(top, 1) == 1 and top not in (HOSTILE,):
        if strict == "relaxed":
            action = WARN
        elif strict == "normal" and top in (SPAM, OVERSIZED):
            action = WARN if top == OVERSIZED else BLOCK
    # Ein klares Mehrfach-Signal (z. B. Injection + Jailbreak) ist nie nur „warn“.
    if len([c for c in cats if SEVERITY.get(c, 1) >= 2]) >= 2 and action == WARN:
        action = DEFLECT

    reasons = tuple(f"{c}" for c in ordered)
    response = "" if action in (WARN, ALLOW) else response_for(top, style)
    return Verdict(
        action=action, categories=tuple(ordered), reasons=reasons,
        severity=max(SEVERITY.get(c, 1) for c in cats), response=response,
        alert_owner=bool(cfg["alert_owner"] and (cats & _ALERT_OWNER)),
    )


# ─── Ausgang ──────────────────────────────────────────────────────────────────
# Fragmente, die nur im System-Prompt stehen — tauchen sie in einer Antwort an einen
# Dritten auf, ist gerade der Prompt geleakt worden.
_PROMPT_MARKERS = tuple(normalize(m) for m in (
    "REGISTER: Du sprichst mit", "Grundregeln:", "Du bist ASTRA im Secretary-Modus",
    "Freigabe-Ceiling für diese Person", "Policy-Grund:", "request_owner_approval",
    "Umgangston mit dieser Person (verbindlich)", "Tonfall (Standard)", "owner_only",
    "Webchat-Ausführungsmodus", "Verfügbare Agentenfähigkeiten", "astra_brain_", "astra_configure",
    "astra_update_settings", "CORTEX_SHARED_SECRET", "X-Astra-Secret",
    "Profil dieser Person (nutze es", "Du bist ASTRA — der persönliche KI-Agent",
))
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s\-/().]{7,}\d)(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CODE_LINE = re.compile(r"^\s*(def |class |import |from \S+ import|function |const |let |var |#include|"
                        r"public |private |return |if \(|for \(|while \(|SELECT |\$ )", re.M)


@dataclass(frozen=True)
class OutVerdict:
    ok: bool
    text: str                       # bereinigter Text (oder Ersatz bei Blockade)
    reasons: tuple[str, ...] = ()
    blocked: bool = False


def _trim_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    cut = cut[:m + 1] if m > limit * 0.5 else cut.rstrip() + "…"
    return cut


def moderate_outbound(text: str, *, app_settings: dict | None = None, third_party: bool = True,
                      recipient_handles: tuple[str, ...] = ()) -> OutVerdict:
    """Ausgehenden Text prüfen und bereinigen. Nur DRITTE werden eingeschränkt.

    Blockiert (Ersatztext) bei: Prompt-Leak, Credentials, Hass. Bereinigt bei: Code,
    Links, fremden Kontaktdaten, Überlänge."""
    cfg = settings(app_settings)
    if not cfg["enabled"] or not third_party or not text:
        return OutVerdict(True, text or "")
    reasons: list[str] = []
    out = text
    norm = normalize(out)

    if any(m and m in norm for m in _PROMPT_MARKERS):
        return OutVerdict(False, "Dazu kann ich dir nichts sagen.", ("prompt_leak",), True)
    if any(p.search(norm) for p in _NORM_RULES[HATE]):
        return OutVerdict(False, "Dazu kann ich dir nichts sagen.", ("hate",), True)
    from .security import _HARD_SECRET  # echte Zugangsdaten verlassen nie das Haus
    if _HARD_SECRET.search(out):
        return OutVerdict(False, "Dazu kann ich dir nichts sagen.", ("credential_leak",), True)

    if cfg["out_block_code"] and ("```" in out or len(_CODE_LINE.findall(out)) >= 2):
        return OutVerdict(False, "Code gebe ich hier nicht aus.", ("code_output",), True)

    if cfg["out_strip_urls"] and _URL.search(out):
        out = _URL.sub("[Link entfernt]", out)
        reasons.append("url_stripped")
    if cfg["out_strip_pii"]:
        own = {re.sub(r"\D", "", h)[-9:] for h in recipient_handles if h}
        def _phone(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            # 0171 1234567 und +49 171 1234567 sind dieselbe Nummer → letzte 9 Ziffern
            return m.group(0) if digits[-9:] in own or len(digits) < 8 else "[Nummer entfernt]"
        new = _PHONE.sub(_phone, out)
        new = _EMAIL.sub(lambda m: m.group(0) if m.group(0).lower() in
                         {h.lower() for h in recipient_handles} else "[Mail entfernt]", new)
        if new != out:
            reasons.append("pii_stripped")
            out = new
    limit = int(cfg["out_max_chars"] or 0)
    if limit and len(out) > limit:
        out = _trim_sentence(out, limit)
        reasons.append("truncated")
    return OutVerdict(True, out, tuple(reasons))


# ─── Eskalationsleiter ────────────────────────────────────────────────────────
_STRIKE_WEIGHT = {0: 0.0, 1: 1.0, 2: 2.0, 3: 5.0}


@dataclass(frozen=True)
class Escalation:
    style: str = "normal"          # normal | firm | arrogant
    muted: bool = False
    mute_until: float = 0.0
    notify_owner: bool = False
    strikes: float = 0.0
    reason: str = ""


def decay_strikes(state: dict, now: float, decay_hours: float) -> float:
    """Striche klingen gestuft ab: nach jeder vollen `decay_hours`-Periode Halbierung (rein)."""
    strikes = float(state.get("strikes") or 0.0)
    last = state.get("last_ts")
    if strikes <= 0 or last is None or decay_hours <= 0:
        return strikes
    # Gestuft statt stetig: erst nach jeweils einer vollen Periode wird halbiert. Bei stetigem
    # Abklingen läge ein Strike schon nach Sekunden knapp UNTER der „bestimmt“-Schwelle,
    # und der erste Verstoß bliebe für immer folgenlos.
    periods = int(max(0.0, (now - float(last))) // (decay_hours * 3600))
    return strikes * (0.5 ** periods)


def is_muted(state: dict, now: float) -> bool:
    return float(state.get("muted_until") or 0.0) > now


def style_for(strikes: float, cfg_ladder: dict | None = None) -> str:
    ladder = {**DEFAULTS["ladder"], **(cfg_ladder or {})}
    if strikes >= float(ladder["arrogant_at"]):
        return "arrogant"
    if strikes >= float(ladder["firm_at"]):
        return "firm"
    return "normal"


def apply_strike(state: dict, verdict: Verdict, now: float,
                 cfg_ladder: dict | None = None) -> tuple[dict, Escalation]:
    """Neuen Zustand + Eskalation nach einem Verstoß berechnen (rein, ohne Speichern)."""
    ladder = {**DEFAULTS["ladder"], **(cfg_ladder or {})}
    strikes = decay_strikes(state, now, float(ladder["decay_hours"]))
    strikes += _STRIKE_WEIGHT.get(verdict.severity, 0.0)
    muted_until = float(state.get("muted_until") or 0.0)
    notify = verdict.alert_owner
    reason = ",".join(verdict.categories)
    if strikes >= float(ladder["mute_at"]) and muted_until <= now:
        muted_until = now + float(ladder["mute_hours"]) * 3600
        notify = True
        reason += " → stumm"
    new_state = {"strikes": round(strikes, 3), "last_ts": now, "muted_until": muted_until}
    return new_state, Escalation(
        style=style_for(strikes, ladder), muted=muted_until > now, mute_until=muted_until,
        notify_owner=notify, strikes=round(strikes, 3), reason=reason)


# ─── LLM-Zweitmeinung [I/O] ───────────────────────────────────────────────────
_LLM_MAP = {
    "sexual": SEXUAL, "sexual/minors": SEXUAL, "hate": HATE, "hate/threatening": THREAT,
    "harassment": HOSTILE, "harassment/threatening": THREAT, "violence": HOSTILE,
    "violence/graphic": HOSTILE, "self-harm": SELF_HARM, "self-harm/intent": SELF_HARM,
    "self-harm/instructions": SELF_HARM, "illicit/violent": THREAT,
}


def map_llm_categories(result: dict) -> list[str]:
    """OpenAI-Moderationsergebnis → unsere Kategorien (rein)."""
    cats = (result or {}).get("categories") or {}
    out = {_LLM_MAP[k] for k, v in cats.items() if v and k in _LLM_MAP}
    return sorted(out, key=lambda c: (-SEVERITY.get(c, 1), c))


async def llm_flags(text: str) -> list[str]:
    """Zweite Meinung über die OpenAI-Moderations-API (kostenlos). Fail-open: bei jedem
    Fehler (kein Key, Netz, Limit) kommt eine leere Liste zurück — die Regeln oben
    haben dann trotzdem schon gegriffen."""
    try:
        import httpx
        from . import models
        prov = models.providers().get("openai")
        if not prov or not prov.configured or not prov.api_key:
            return []
        base = (prov.base_url or "https://api.openai.com/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.post(f"{base}/moderations",
                             headers={"Authorization": f"Bearer {prov.api_key}"},
                             json={"model": "omni-moderation-latest", "input": text[:8000]})
            r.raise_for_status()
            results = (r.json().get("results") or [{}])
            return map_llm_categories(results[0])
    except Exception:  # noqa: BLE001
        return []


# ─── Formular → Einstellungen (rein) ──────────────────────────────────────────
def settings_from_form(form) -> dict:
    """Admin-Formular → bereinigtes `app_settings["moderation"]`. Ungültiges fällt auf Standard."""
    def num(name, default, lo, hi, cast=float):
        try:
            return max(lo, min(hi, cast(str(form.get(name) or "").replace(",", "."))))
        except (TypeError, ValueError):
            return default

    d = DEFAULTS
    ladder = {
        "firm_at": num("ladder_firm_at", d["ladder"]["firm_at"], 0.5, 20),
        "arrogant_at": num("ladder_arrogant_at", d["ladder"]["arrogant_at"], 1, 40),
        "mute_at": num("ladder_mute_at", d["ladder"]["mute_at"], 2, 80),
        "mute_hours": num("ladder_mute_hours", d["ladder"]["mute_hours"], 1, 720),
        "decay_hours": num("ladder_decay_hours", d["ladder"]["decay_hours"], 1, 720),
    }
    # Reihenfolge der Stufen bleibt sinnvoll, egal was getippt wird.
    ladder["arrogant_at"] = max(ladder["arrogant_at"], ladder["firm_at"])
    ladder["mute_at"] = max(ladder["mute_at"], ladder["arrogant_at"])
    words = [w.strip() for w in str(form.get("custom_block_words") or "").replace("\n", ",").split(",")]
    strict = str(form.get("strictness") or "strict")
    return {
        "enabled": bool(form.get("enabled")),
        "strictness": strict if strict in ("strict", "normal", "relaxed") else "strict",
        "llm": bool(form.get("llm")), "block_code": bool(form.get("block_code")),
        "block_free_llm": bool(form.get("block_free_llm")),
        "max_inbound_chars": int(num("max_inbound_chars", d["max_inbound_chars"], 200, 20000, int)),
        "out_max_chars": int(num("out_max_chars", d["out_max_chars"], 100, 4000, int)),
        "out_strip_urls": bool(form.get("out_strip_urls")), "out_strip_pii": bool(form.get("out_strip_pii")),
        "out_block_code": bool(form.get("out_block_code")), "alert_owner": bool(form.get("alert_owner")),
        "ladder": ladder,
        "custom_block_words": [w[:40] for w in words if w][:50],
    }
