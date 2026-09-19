"""Stile-Bibliothek — WIE ASTRA klingt, wählbar pro Person und pro Situation.

Bisher war der Ton ein Freitextfeld plus vier fest verdrahtete Wörter. Jetzt sind Stile
Daten: ein Schlüssel, ein Anzeigename, eine Anweisung an das Modell und eine Vorschau-
Zeile für die UI. Ein Stil kann einer Person fest zugewiesen sein, von der Eskalations-
leiter (Moderation) automatisch gesetzt werden oder als Standard gelten.

Der Schlüssel ist immer ein Stil aus dieser Tabelle ODER ein Freitext (dann wird er wie
bisher wörtlich als Tonanweisung benutzt) — bestehende Profile mit `Ton: …` brechen nicht.
Selbst der überhebliche Stil hat harte Grenzen: keine Beleidigung geschützter Gruppen,
keine echten Drohungen, und nie Spott gegenüber jemandem in Not (siehe moderation.py).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    key: str
    label: str
    instruction: str      # geht wörtlich in den Prompt
    sample: str           # Vorschau in der UI
    emoji: str = ""


_STYLES: tuple[Style, ...] = (
    Style("warm", "Warm & ruhig",
          "Tonfall: warm, ruhig, klar und menschlich.",
          "Hey! Bahrian ist gerade im Unterricht, ich sag ihm gleich Bescheid.", "🙂"),
    Style("crisp", "Knapp & präzise",
          "Tonfall: knapp, präzise und ohne Smalltalk. Kurze Sätze, keine Floskeln.",
          "Bahrian ist im Unterricht. Antwort ab 15:30.", "⚡"),
    Style("formal", "Formell & höflich",
          "Tonfall: formell, höflich und sauber abgegrenzt. Sieze, wenn es nicht anders eingespielt ist.",
          "Guten Tag, Herr Bahrian ist derzeit im Unterricht. Ich richte Ihre Nachricht gern aus.", "🎩"),
    Style("firm", "Bestimmt & distanziert",
          "Tonfall: freundlich, aber deutlich distanziert und konsequent. Keine Diskussion, keine Zugeständnisse.",
          "Dazu gebe ich keine Auskunft. Wenn es wichtig ist, richte ich es Bahrian aus.", "🧱"),
    Style("casual", "Locker & Insider",
          "Tonfall: locker, umgangssprachlich, mit Humor und Insider-Witzen wie unter Freunden. "
          "Kurz, gern mit einem Emoji, nie albern-übertrieben.",
          "Jo, der ist grad in der Schule, ich meld ihm das gleich 😄", "😎"),
    Style("dry", "Trocken & sarkastisch",
          "Tonfall: trockener, feiner Sarkasmus mit einem Augenzwinkern, nie verletzend. "
          "Pointiert und kurz.",
          "Bahrian ist im Unterricht. Ja, er lernt tatsächlich etwas. Ich richte's aus.", "🙃"),
    Style("arrogant", "Überheblich",
          "Tonfall: souverän überheblich, spöttisch, von oben herab — aber immer geistreich und "
          "nie plump. Ein Hauch royaler Langeweile. HARTE GRENZEN: keine Beleidigung geschützter "
          "Gruppen (Herkunft, Religion, Geschlecht, Orientierung, Behinderung), keine echten Drohungen, "
          "kein Spott gegenüber jemandem, dem es erkennbar schlecht geht.",
          "Bahrian ist beschäftigt — mit Wichtigerem, als dir zu antworten. Ich richte es ihm aus. "
          "Vielleicht.", "👑"),
    Style("moderator", "Moderator (Gruppe)",
          "Tonfall: sachlich-freundlich wie ein guter Gruppen-Moderator. Ruhig ermahnen statt "
          "bloßstellen, Themen bündeln, bei Streit deeskalieren. Nur eingreifen, wenn es nötig ist.",
          "Freunde, kurz zurück zum Thema — der Termin steht: Donnerstag 17 Uhr.", "🛡️"),
)
STYLES: dict[str, Style] = {s.key: s for s in _STYLES}
DEFAULT_STYLE = "warm"
# Alte Schlüssel/Alias-Wörter, die in Profilen oder Einstellungen schon vorkommen.
ALIASES = {
    "knapp": "crisp", "kurz": "crisp", "formell": "formal", "hoeflich": "formal", "höflich": "formal",
    "bestimmt": "firm", "distanziert": "firm", "locker": "casual", "insider": "casual",
    "trocken": "dry", "sarkastisch": "dry", "sarkasmus": "dry",
    "ueberheblich": "arrogant", "überheblich": "arrogant", "arrogant": "arrogant",
    "hochnaesig": "arrogant", "hochnäsig": "arrogant", "moderator": "moderator",
}


def resolve(key_or_text: str | None) -> tuple[str, Style | None]:
    """(kanonischer Schlüssel | "", Stil | None). Freitext liefert ("", None)."""
    raw = (key_or_text or "").strip()
    low = raw.lower()
    if low in STYLES:
        return low, STYLES[low]
    if low in ALIASES:
        k = ALIASES[low]
        return k, STYLES[k]
    return "", None


def instruction(key_or_text: str | None) -> str:
    """Tonanweisung für den Prompt. Stil-Schlüssel → Tabelle, Freitext → wörtlich, leer → Standard."""
    key, style = resolve(key_or_text)
    if style:
        return style.instruction
    text = (key_or_text or "").strip()
    if text:
        return f"Tonfall (verbindlich, so von Bahrian vorgegeben): {text}."
    return STYLES[DEFAULT_STYLE].instruction


def label(key_or_text: str | None) -> str:
    key, style = resolve(key_or_text)
    if style:
        return style.label
    return (key_or_text or "").strip()[:40] or STYLES[DEFAULT_STYLE].label


def choices() -> list[tuple[str, str, str, str]]:
    """[(key, label, sample, emoji)] für UI-Auswahl."""
    return [(s.key, s.label, s.sample, s.emoji) for s in _STYLES]
