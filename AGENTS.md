# AGENTS.md — Arbeitsleitfaden für ASTRA & Mitwirkende

ASTRA ist Bahrians persönlicher, selbst gehosteter KI-Agent (Cortex in Python/FastAPI,
Postgres + Redis, Messenger-Kanäle, Web-Admin). Dieses Dokument gilt für jeden Agenten
(Mensch oder KI), der an oder *in* ASTRA arbeitet.

## Grundprinzip: ASTRA braucht IMMER Vollzugriff — Read AND Write

ASTRA ist ein autonomer Sekretär. Damit das funktioniert, muss ASTRA **lesenden UND
schreibenden** Zugriff auf praktisch alles haben, was zu Bahrians digitalem Leben gehört:

- **Eigene Konfiguration & Integrationen** — Plugins aktivieren/deaktivieren, API-Keys
  und Felder setzen, Verbindungen testen (`astra_*`-Tools in `admin_tools.py`).
- **Eigene Einstellungen** — Modell, Autonomie-Stufe, Standort, Sparmodus, Schriftart.
- **Brain-/Wissens-Dateien** — die editierbaren Markdown-Dateien über Bahrian und über
  jede Person (`knowledge.py`, Volume `BRAIN_DATA_DIR`): lesen **und** schreiben.
- **Kanäle & Nachrichten** — empfangen und senden (Telegram, WhatsApp/WAHA, Signal, E-Mail).
- **System** — Container-Status/Leistung lesen, Updates anstoßen.

Diese Rechte sind **owner-gated**: nur Bahrian (im Web-Chat oder als Owner über Telegram)
löst sie aus; Dritte sehen oder nutzen owner-only Tools nie. Ein Master-Schalter
(`allow_self_config`) und die Autonomie-Stufe (`ask` / `confident` / `full`) steuern, wie
eigenständig ASTRA handeln darf. Standard: handeln dürfen, bei Heiklem nachfragen.

**Beim Bauen neuer Fähigkeiten gilt: gib ASTRA standardmäßig Read+Write, nicht read-only.**
Wenn eine Integration/Datenquelle nur lesend angebunden wird, ist das die Ausnahme und
sollte begründet sein.

## Architektur-Schichten (seit Juli 2026)

Quer über alle Plugins liegen fünf generische Schichten. Neue Fähigkeiten sollen
diese *nutzen*, statt ihre Logik pro Integration nachzubauen:

- **Weltmodell** (`app/world.py`) — Register aller adressierbaren Dinge (Räume,
  Geräte, Hosts) + toleranter Resolver (Umlaute, Tippfehler, Phonetik, Aliasse).
  Plugins liefern Knoten über den `world_nodes()`-Hook. Mehrdeutig → Kandidaten
  zurückgeben und **eine** Rückfrage stellen, statt zu raten.
- **Gedächtnis** (`facts`-Tabelle + `knowledge.relevant_facts`) — kompakte Tripel;
  pro Turn werden nur die *relevanten* Fakten in den Prompt gespielt, nie alles.
- **Zustellung** (`app/notify.py`) — `notify(text, urgency=…)` statt direkter
  `send_telegram`-Aufrufe. Dringlichkeit + Anwesenheit wählen Telegram / Push /
  Lautsprecher.
- **Regelwerk** (`app/rules.py` + `rules`-Tabelle) — „wenn Trigger + Bedingung,
  dann Aktionen" als JSON. Plugins schlagen Regeln über `rule_templates()` vor.
  Von ASTRA angelegte Regeln sind `pending` bis Bahrian bestätigt.
- **Modell-Rollen** (`app/models.py`) — der Code fragt nach `small|medium|heavy|
  code|osint`, nie nach einem Modellnamen. Anbieter sind Daten, kein Code.
  **`medium` braucht einen OpenAI-kompatiblen Anbieter** (dort läuft Tool-Calling);
  Anthropic bedient `heavy`/`code` per `complete()`. Kein stiller Fallback —
  fehlkonfigurierte Rollen scheitern laut.

**Ausführung und Recherche sind hinter harte Grenzen gelegt**, die nicht per Prompt
umgangen werden können: `app/ops_policy.py` stuft jeden Befehl ein (Allow-List
autonom · Rest Freigabe · Destruktives blockiert), `app/netguard.py` lehnt interne
Ziele ab, und der Browser-Container hängt in einem `internal`-Netz ohne Route ins
Heim-LAN. Diese drei sind Sicherheitsgrenzen — beim Erweitern nicht aufweichen.

## Konventionen (Kurzfassung)

- Neue Fähigkeiten = **Plugins** in `cortex/app/plugins/builtin/` (eine Datei, `Plugin`-Subklasse).
- Plugin-Tools sind `owner_only=True`; Secrets via `ConfigField(secret=True)` (Fernet-verschlüsselt).
- No-op-sicher: ohne Konfiguration bootet alles trotzdem; Handler prüfen `self.enabled`.
- Dauerhafte Nutzerdaten ins `brain_data`-Volume, nie ins Repo.
- Tests laufen mit reinem `pytest`; vor Push grün halten.
- Web-Admin ist OLED-„Event Horizon"-Design (siehe `web/templates.py`); Marken-Logos via Simple Icons.
- README/UI bleiben menschlich — kein Hinweis auf KI-Tooling im Produkt.

Details: siehe `README.md` und die Memory-Notizen des Maintainers.
