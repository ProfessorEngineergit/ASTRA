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

## Konventionen (Kurzfassung)

- Neue Fähigkeiten = **Plugins** in `cortex/app/plugins/builtin/` (eine Datei, `Plugin`-Subklasse).
- Plugin-Tools sind `owner_only=True`; Secrets via `ConfigField(secret=True)` (Fernet-verschlüsselt).
- No-op-sicher: ohne Konfiguration bootet alles trotzdem; Handler prüfen `self.enabled`.
- Dauerhafte Nutzerdaten ins `brain_data`-Volume, nie ins Repo.
- Tests laufen mit reinem `pytest`; vor Push grün halten.
- Web-Admin ist OLED-„Event Horizon"-Design (siehe `web/templates.py`); Marken-Logos via Simple Icons.
- README/UI bleiben menschlich — kein Hinweis auf KI-Tooling im Produkt.

Details: siehe `README.md` und die Memory-Notizen des Maintainers.
