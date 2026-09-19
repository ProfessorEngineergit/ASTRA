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

- **Klick wählt aus, Einstellungen unten:** Ein Klick auf eine Zeile wählt den Kontakt nur aus (nichts wird geöffnet; nochmal klicken =
  abwählen). Mehrere: Häkchen oder Strg/Cmd-Klick, „alle" oben. Direkt **unter der Liste** erscheinen die **Secretary-Einstellungen**:
  bei **einem** Kontakt mit seinen aktuellen Werten (Vorgehen, Vertrauensstufe, Stil, Freigaben, Aktivzeiten, Modell, Anweisung, bei
  Gruppen Auslöser und Rolle) — „Speichern" sendet nur, was du geändert hast; bei **mehreren** als Sammeländerung („nicht ändern" ist
  Standard). Für Notizen, Zeitfenster, Kennungen, Verlauf und „Alles vergessen" gibt es den Link „Alle Details →".
- **Secretary An/Aus:** ein Schalter pro Zeile (wirkt sofort) und bei Auswahl eine feste Leiste oben mit einem Schalter für alle
  Ausgewählten (An / Aus / „Gemischt (x von y an)"; bei „Gemischt" schaltet ein Klick zuerst alle ein). **Aus** = der Secretary
  antwortet nicht, notiert die Nachrichten aber weiter; **An** hebt „nie"/„blockieren" wieder auf. Filter „Secretary aus" zeigt alle
  Stummen.
- **Kontakte ohne Karte** lassen sich auswählen und einstellen; die Karte entsteht erst beim Speichern/Schalten (mit der bekannten
  Vertrauensstufe). Ein reines Ansehen ändert nichts — wichtig, weil ein Kontakt mit Karte nicht mehr als „unbekannter Absender"
  nachgefragt wird.

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

## Smart-Antwort (wann ASTRA wartet, antwortet oder schweigt)
Gilt für Einzelchats auf WhatsApp/Signal/Slack bei **„Immer an"** oder Kanalmodus **„Smart"** (Admin → Secretary). Ausdrücklich gewählte
Kanalmodi „Direkt / Warten / Immer fragen" und Karten-Regeln „direkt / fragen" bleiben, wie sie sind. **Wichtig:** Steht ein Kanal auf
„Direkt", wartet ASTRA nie — für das Verhalten unten den Kanalmodus auf „Smart" stellen.

1. **Frische Unterhaltung:** ASTRA wartet ~1 Minute (einstellbar), ob du selbst antwortest. Schreibst du der Person, bleibt ASTRA still.
   Kommt während des Wartens noch etwas dazu, läuft der Timer weiter (ab der ersten Nachricht).
2. **Danach nach Art der Nachricht** (ohne Modell, kein Token):
   - **Anfrage** (Kalender, Termine, Verabredung, „richte ihm aus", Uhrzeiten, Wochentage …) → ASTRA beantwortet sie.
   - **„Hallo / bist du da? / kann ich mit dir reden?" oder Smalltalk** → **eine** Vorstellung: „Ich bin Bahrians KI-Assistent, er hat sich noch nicht
     gemeldet, ich kann zu … helfen — frag mich gern." Sie sagt ehrlich, was geht (**Kalenderzugriff ja/nein**, Kartenfreigabe beachtet).
     Danach **Ruhephase** (Standard 3 Std.): Smalltalk bekommt keine Antwort mehr, eine konkrete Anfrage schon.
   - **Emoji, GIF/Sticker/Link, Lachen, „ok", „danke"** → gar keine Antwort (auch kein Ratenlimit-Zähler).
3. **Laufendes Gespräch** (ASTRAs letzte Antwort < 30 Min.): Anfragen bekommen sofort eine Antwort, Smalltalk wird ignoriert.
   Stellt ASTRA eine **Rückfrage** („…?"), zählt auch ein kurzes „ja / gerne / nein" als Antwort.
4. **Du greifst ein** (du schreibst der Person selbst — auch mitten im Gespräch): ASTRA hört **sofort** auf und vergisst das laufende
   Gespräch; die nächste Nachricht wartet wieder eine Minute. ASTRAs **eigene** Nachrichten (WhatsApp meldet sie als „fromMe" zurück)
   werden per Echo-Filter erkannt und zählen nie als Eingreifen.
5. **Ungelesen:** Nach ASTRAs Antwort wird der Chat auf deinem Handy wieder auf „ungelesen" gesetzt (WAHA `chats/{id}/unread`, Best effort;
   Einstellung „Chats ungelesen lassen"). Unterstützt deine WAHA-Version das nicht, wird es einmal geloggt und übersprungen.

Einstellbar unter Admin → Secretary → Smart-Antwort: Wartezeit, Gesprächsfenster, Ruhephase, Rausch-Filter, ungelesen lassen.

**Stilwechsel mitten im Chat:** Die Stil-Anweisung steht jetzt zusätzlich als „Stil-Erinnerung" **hinter** dem Gesprächsverlauf im Prompt
(„frühere Antworten können anders klingen — richte dich nicht nach ihnen"). Vorher orientierte sich das Modell am Ton seiner eigenen
früheren Antworten, ein neu gewählter Stil kam deshalb nicht an.

## Kalender-Intelligenz
Tool `suggest_meeting_times`: freie Slots mit Puffer und Verteilung über die Tage; Dritten nur Zeiten, nie Titel,
nur im Rahmen ihrer Kartenfreigabe.

## Tools (owner-only)
`usage_report` · `secretary_switch` · `contact_cards_list/get/update` · `context_forget` · `prompt_show` · `prompt_propose`.
Verändernde Tools pausieren im Web-Chat-Modus „Fragen" bis zur Bestätigung; Änderungen an Karten sind im Audit-Log.
