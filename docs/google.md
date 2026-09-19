# Google-Konten (Kalender · Aufgaben · Gmail)

Admin → **Google**: ein OAuth-Client, beliebig viele Konten (privat, Schule …), pro Plugin/Installation ein wählbares Konto.

## Einrichten (einmalig)
1. [Google Cloud Console → Clients](https://console.cloud.google.com/auth/clients) → Client erstellen → Typ **Webanwendung**.
2. APIs aktivieren: *Google Calendar API*, *Google Tasks API*, *Gmail API* (je nach Bedarf).
3. **Autorisierte Weiterleitungs-URI**: genau die Adresse eintragen, die Admin → Google anzeigt.
4. Zustimmung auf **„In production"** stellen (für den Eigengebrauch ohne Prüfung möglich). Bei „Testing" laufen Tokens nach 7 Tagen ab,
   und jedes Konto muss als Testnutzer eingetragen sein.
5. Client-ID + Secret in Admin → Google speichern, dann **Mit Google anmelden**.

## Warum „OAuth geht nicht" bei LAN-Adressen
Google akzeptiert für Weiterleitungen nur `https://<echte Domain>` oder `http://localhost` — nicht `http://10.60.0.190:8088`.
ASTRA erkennt das und wechselt in den **manuellen Modus**: Google leitet auf `http://localhost:8088/admin/oauth/google/callback`
zurück, die Seite lädt nicht (gewollt), du kopierst die **komplette Adresse aus der Browserleiste** und fügst sie unter
„Anmeldung abschließen" ein. Läuft der Admin über `localhost` (z. B. SSH-Tunnel `ssh -L 8088:localhost:8088 root@box`) oder eine
https-Domain, geht es direkt ohne Einfügen.

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
Maps und YouTube nutzen einen API-Key (Plugin-Formular), nicht die Google-Anmeldung.
