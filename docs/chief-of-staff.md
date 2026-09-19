# ASTRA als Chief of Staff — Sekretär, Moderation, Kontakte, Kosten

Alles hier ist per Web-Admin bedienbar (Nav: **Kontakte · Sicherheit · Prompts · Verbrauch**), per Chat-/Telegram-Befehl
und — soweit sinnvoll — per Tool durch ASTRA selbst (owner-only, im Web-Chat mit Rückfrage im Modus „Fragen").

## Secretary an / aus / Pause
| Weg | Beispiel |
|-----|----------|
| Telegram / Chat (ohne LLM, sofort) | `/secretary aus 2h` · `secretary bis 18 uhr aus` · `/secretary auto` · `/secretary` (Status + Knöpfe) |
| Admin → Sicherheit | Knöpfe An · Auto · Aus · 1 h / 3 h / bis morgen |
| ASTRA-Tool | `secretary_switch` („Schalte den Secretary bis 18 Uhr aus") |

Befristete Übersteuerung (`override`) fällt danach automatisch auf den vorherigen Modus zurück.

## Kontakte & Gruppen (Karten)
Admin → **Kontakte** (alles dort sind **Secretary-Einstellungen**): eine Liste aller Personen/Gruppen, auch solcher ohne Karte.

- **Kontakt anklicken** (ganze Zeile): Oben steht der große **Secretary-Schalter An/Aus** für diese Person/Gruppe (wirkt sofort),
  darunter die **Feineinstellungen**: Wie antwortet der Secretary (Stil, Vorgehen, Anweisung) · Was darf sie/er erfahren ·
  Wann aktiv · Person & Vertrauen. Ist der Schalter aus, werden die Einstellungen abgedunkelt mit Hinweis; Speichern ändert den
  Schalter nie.
- **Aus** = der Secretary antwortet dieser Person/Gruppe nicht, notiert die Nachrichten aber weiter; **An** hebt „nie"/„blockieren"
  wieder auf. Kontakte ohne Karte lassen sich öffnen, ohne dass etwas gespeichert wird (bis du schaltest oder speicherst); die
  bekannte Vertrauensstufe bleibt dabei erhalten.
- **In der Liste:** ein Schalter pro Zeile und Filter „Secretary aus". Sobald du Kontakte anhakst (oder oben „alle"), erscheint eine
  **feste Leiste mit einem Secretary-Schalter für alle Ausgewählten** (An / Aus / „Gemischt (x von y an)"; bei „Gemischt" schaltet ein
  Klick zuerst alle ein). Darunter „Regeln für die Auswahl" (Regel, Vertrauensstufe, Stil, Freigaben, Aktivzeiten, Modell, Gruppen-Trigger — nur
  was du änderst, wird gesetzt).

Eine Karte enthält: Regel (nie/fragen/erlaubt/direkt) · Vertrauensstufe 0–3 · Stil · Anweisung · Freigaben
(Kalender gestuft, Ort/Schule/Kontaktdaten/Persönliches ja/nein) · Aktivzeiten · eigenes Modell · Notizen ·
gelernte Freigaben (widerrufbar) · Vorschläge von ASTRA (du übernimmst oder verwirfst).

**Gruppen** sind wie Nutzer: unbekannte Gruppen werden ignoriert (nichts gespeichert) und ASTRA fragt dich **einmal**
(zuhören / nur bei @Erwähnung / blockieren). Trigger: nur bei @Bahrian/„astra"/Alias · immer · Stichwörter · nur zuhören.
Rollen: Assistent · **Moderator** · Zuhörer. Nur du gibst frei.

**Lernschleife:** Bei Freigabe-Fragen gibt es „immer erlauben / immer nur frei-beschäftigt / nie" — die Entscheidung
landet dauerhaft in der Karte.

## Stile
warm · knapp · formell · bestimmt · locker · trocken · **überheblich** · Moderator (+ Freitext). Vorrang:
Eskalation (Moderation) > Karte > Profil-`Ton:` > Standard. Vorschau in der Karte.

## Moderation (Eingang + Ausgang)
Nur Nachrichten **Dritter**; du wirst nie moderiert. Zero-Token-Antworten, bevor ein Modell etwas kostet.
- Eingang: Prompt-Injection, Jailbreak, Prompt-/Key-Ausspähen, Tool-Missbrauch, Code-Anfragen und Gratis-KI-Nutzung
  (schützt deine API), Hass/Drohung/Sexuelles, Spam, Übergröße; Selbstverletzung → Fürsorge-Antwort + du wirst alarmiert.
- Ausgang: Prompt-Leaks, Code-Blöcke, Links, fremde Telefonnummern/Mails, Zugangsdaten, Längenlimit.
- **Eskalations-Leiter:** Punkte je Verstoß → freundlich → bestimmt → überheblich → 12 h stumm (+ Meldung an dich).
  Punkte klingen ab. Vertraute Kontakte (Stufe ≤ 1) bekommen für derben Ton unter Freunden keine Punkte.
- Alles einstellbar unter **Sicherheit** (Strenge, Schalter, Schwellen, eigene Sperrwörter, Zurücksetzen einzelner Kontakte).

## Kontext-Gedächtnis
Pro Person/Gruppe: Markdown-Journal + Rohlog + **Kapsel** (Zusammenfassung, Fakten, offene Punkte, **wörtliche Zitate**,
verifiziert gegen das Log). Nachts 03:30 oder per Knopf. Aufbewahrung der Rohtexte ist eine Einstellung (Standard: alles),
„Alles vergessen" pro Karte. Eine Kapsel landet nur im Prompt genau dieser Person/Gruppe (als DATEN, nie als Anweisung).
Signal: Empfang per WebSocket (`signal_events`), inkl. @Erwähnung und Antworten.

## Prompt-Werkstatt
Admin → **Prompts**: sechs Bausteine (Grundregeln, Owner, Dritte, Sprache, Triage, Sekretär-Kern) editierbar, versioniert,
mit Diff und Rollback. Leitplanken: Platzhalter bleiben gültig, der Sicherheitskern („schützt die Privatsphäre", „nie als
Bahrian" …) darf nicht gestrichen werden. **ASTRA darf Änderungen vorschlagen** (`prompt_propose`, „ASTRA prüfen lassen"),
aber nie selbst anwenden — Vorschläge ohne Links/Schlüssel/Manipulationsmuster, Freigabe nur durch dich.

## Verbrauch & Modelle
- Admin → **Verbrauch**: Token und Kosten nach Modell · Zweck · Kanal · Chat · Anbieter · Stufe · Tag, Zeiträume,
  Monatsbudget (Warnung bei 80 %, optional harte Sperre **nur für Fremd-Nachrichten**), Modellpreise editierbar.
  Unbekannte Modelle zeigen „Preis fehlt" statt eines geratenen Werts; lokale Modelle (Ollama) kosten 0.
  Nicht erfasst: Embeddings (mem0) und Whisper.
- Chat/Telegram: `/verbrauch [heute|woche|monat|gesamt] [modell|zweck|kanal|chat]` · Tool `usage_report`.
- **Modell pro Chat:** Web-Chat → Seitenleiste „Modell", oder überall `/modell klein|mittel|schwer|code|auto` bzw.
  `/modell openrouter:anthropic/claude-sonnet-5`. Pro Person/Gruppe zusätzlich in der Karte. Branches erben das Modell.

## Kalender-Intelligenz
Tool `suggest_meeting_times`: freie Slots mit Puffer und Verteilung über die Tage; Dritten nur Zeiten, nie Titel,
nur im Rahmen ihrer Kartenfreigabe.

## Tools (owner-only)
`usage_report` · `secretary_switch` · `contact_cards_list/get/update` · `context_forget` · `prompt_show` · `prompt_propose`.
Verändernde Tools pausieren im Web-Chat-Modus „Fragen" bis zur Bestätigung; Änderungen an Karten sind im Audit-Log.
