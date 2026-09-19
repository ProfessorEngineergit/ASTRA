# Google-Konten (Kalender · Aufgaben · Gmail)

Admin → **Google**: ein OAuth-Client, beliebig viele Konten (privat, Schule …), pro Plugin/Installation ein wählbares Konto.

## Einrichten (einmalig)
1. [Google Cloud Console → Clients](https://console.cloud.google.com/auth/clients) → Client erstellen → Typ **Webanwendung**.
2. APIs aktivieren: *Google Calendar API*, *Google Tasks API*, *Gmail API* (je nach Bedarf).
3. **Autorisierte Weiterleitungs-URIs**: die Adressen eintragen, die Admin → Google anzeigt (Domain und localhost).
4. Zustimmung auf **„In production"** stellen (für den Eigengebrauch ohne Prüfung möglich). Bei „Testing" laufen Tokens nach 7 Tagen ab,
   und jedes Konto muss als Testnutzer eingetragen sein.
5. Client-ID + Secret in Admin → Google speichern, dann **Mit Google anmelden**.

## Weiterleitung: eigene Domain, localhost oder automatisch
Google akzeptiert für Weiterleitungen nur `https://<echte Domain>` oder `http://localhost` — nicht `http://10.60.0.190:8088`.
Unter Admin → Google → „Weiterleitung zurück zu ASTRA" wählst du:
- **Eigene Domain** (empfohlen), z. B. `https://astra.bahriannovotny.space`: Domain eintragen (Pfad ergänzt ASTRA selbst), dieselbe
  Adresse plus `/admin/oauth/google/callback` bei Google eintragen. Du kannst **lokal** gestartet haben: Google leitet über die Domain
  zurück, ASTRA schließt die Anmeldung dort ab (kein Login auf der Domain nötig) und schickt dich danach dorthin zurück, wo du
  angefangen hast. Ungültige Eingaben (LAN-IP, http, falscher Pfad) lehnt ASTRA ab, bevor Google es tut.
- **Automatisch**: richtet sich nach der Adresse, unter der du gerade bist (localhost oder https-Domain → direkt; sonst manuell).
  Hinter einem Proxy/Tunnel erkennt ASTRA https über `X-Forwarded-Proto`.
- **Nur localhost (manuell)**: Google leitet auf `http://localhost:<Port>/admin/oauth/google/callback` zurück, die Seite lädt nicht
  (gewollt), du kopierst die **komplette Adresse aus der Browserleiste** und fügst sie unter „Anmeldung abschließen" ein. Der Knopf
  „Über localhost anmelden" nutzt diesen Weg auch dann, wenn eine Domain eingestellt ist.

Trage am besten **beide** Adressen bei Google ein (Domain + localhost): die Seite zeigt sie mit Kopieren-Knopf.

## Berechtigungen (Scopes) und APIs
| Produkt | Google-API aktivieren | Scope, den ASTRA erfragt | Wofür |
|---|---|---|---|
| Kalender | Google Calendar API | `…/auth/calendar` | Termine lesen, anlegen, Konflikte prüfen |
| Aufgaben | Google Tasks API | `…/auth/tasks` | Listen lesen, Aufgaben anlegen/abhaken |
| Gmail lesen | Gmail API | `…/auth/gmail.readonly` | Mails lesen/zusammenfassen |
| Gmail senden | Gmail API | `…/auth/gmail.send` | Mails senden (nur nach deiner Freigabe) |
| immer dabei | — | `openid`, `email`, `profile` | Konto erkennen (E-Mail, Name) |

Unter „Datenzugriff" (Google Auth Platform) dieselben Scopes hinzufügen. Beim Anmelden kannst du pro Konto nur die Produkte wählen, die
du willst (Standard: Kalender, Aufgaben, Gmail lesen). **Maps und YouTube brauchen keine Anmeldung**, sondern einen API-Key
(*Directions API* bzw. *YouTube Data API v3*).

## Konten wechseln
- **Standardkonto**: Knopf „Als Standard" (oder Chat: „Nimm mein Schulkonto" → Tool `google_set_default_account`).
- **Pro Plugin**: Tabelle „Welches Plugin nutzt welches Konto?" bzw. Feld „Google-Konto" im Plugin-Formular. Mehrere Installationen
  eines Plugins (z. B. zwei Kalender) können verschiedene Konten nutzen.
- **Produkte ändern**: „Produkte ändern / erneut anmelden" bei einem Konto — bereits erteilte Rechte bleiben erhalten.
- Bestehende Plugin-eigene Verbindungen laufen unverändert weiter; „Übernehmen" macht daraus ein zentrales Konto ohne erneutes Zustimmen.

## Fehler verstehen
„Alles testen" prüft jedes freigegebene Produkt und erklärt die Ursache mit nächstem Schritt: API nicht aktiviert (mit Direktlink),
Produkt nicht freigegeben, Token abgelaufen/widerrufen, falscher Client, fehlende Weiterleitungs-URI, Konto nicht als Testnutzer.
Tokens liegen verschlüsselt (Fernet) in der Datenbank, Access-Tokens nur im Speicher; „Trennen" widerruft bei Google.

## Nicht über OAuth
Maps (Directions API) und YouTube (Data API v3) nutzen einen API-Key im Plugin-Formular, nicht die Google-Anmeldung.
