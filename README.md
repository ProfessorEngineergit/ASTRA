<div align="center">
  <h1>🌌 ASTRA</h1>
  <p><strong>Dein persönlicher, serverseitiger KI-Agent für alles.</strong></p>
</div>

---

**ASTRA** ist ein persönlicher KI-Agent, der auf einem Server läuft und als intelligentes Bindeglied zwischen deinen Messengern, APIs und automatisierten Workflows fungiert. ASTRA ist dein "Gehirn" in der Cloud, das Nachrichten orchestriert, Kontexte versteht, Zustände verwaltet und dir Arbeit abnimmt.

> ⚠️ **Status:** Starkes Work in Progress (WIP)

## ✨ Konzept & Features

- **Omnichannel:** Bündelt deine Kommunikation (WhatsApp, Signal, Telegram) an einem zentralen Ort.
- **Human-in-the-Loop:** ASTRA fragt über einen privaten Telegram-Kanal nach Erlaubnis für kritische Aktionen.
- **Deferral Timer:** Wartet eine definierte Zeit (`ASTRA_DEFER_SECONDS`), ob du selbst antworten möchtest, bevor die KI übernimmt.
- **Shadow Mode:** Im `ASTRA_DRY_RUN`-Modus analysiert ASTRA alles im Hintergrund und loggt Aktionen, ohne sie nach außen zu senden.
- **Volle Kontrolle:** Deine Daten, dein Server. Die Logik liegt zentral in deinem eigenen Cortex.

## 🧩 Architektur

ASTRA setzt auf eine moderne, stark entkoppelte Docker-Architektur:

- **🧠 Cortex (Python/FastAPI):** Das zentrale Gehirn. Hier liegt die gesamte Geschäftslogik, das Gedächtnis, die Richtlinien (Policies) und die "Human-in-the-Loop"-Steuerung. Besitzt **allen** State.
- **⚙️ n8n:** Zustandslose Workflow-Engine. Beinhaltet keine Business-Logik, sondern stellt dem Cortex lediglich Tools und API-Schnittstellen (als Workflows, `tool/send_*`) zur Verfügung.
- **🗄️ PostgreSQL & pgvector:** Source of Truth (contacts, threads, messages, approvals, audit_log) + Vektorspeicher (Memory/Embeddings).
- **🔴 Redis:** State-Machine-Zustand der Threads, Deferral Timer und Pub/Sub-Events.
- **💬 Messenger-Gateways:**
  - **WAHA:** WhatsApp-Anbindung.
  - **Signal-CLI:** Signal-Anbindung.
  - **Telegram:** Haupt-Steuerkanal und Genehmigungs-Channel für den Besitzer.
- **👁️ Langfuse:** LLM-Tracing und Debugging für maximale Transparenz bei KI-Entscheidungen.
- **🌐 Caddy:** Reverse Proxy für sicheren TLS-Zugriff von außen.

## 🔁 Funktionsweise (Kurz)

```
Eingang (3. Person) ──► Triage (billiges LLM) ──► Policy ──► AUTO | DEFER | ASK
                                                              │
   AUTO  → ASTRA antwortet sofort (innerhalb der Freigabe-Stufe)
   DEFER → wartet ASTRA_DEFER_SECONDS auf DICH; sonst springt der Sweeper ein
   ASK   → Telegram-Nachricht an dich mit Buttons ✅ Ja / 🟡 Nur „beschäftigt“ / ❌ Nein
```

Antwortest du selbst (auch direkt vom Handy via WhatsApp `fromMe`), erkennt ASTRA das und **hält sich raus** (Stand-down).

## 🛠️ Installation & Setup

ASTRA ist für ein Self-Hosted Setup via Docker Compose ausgelegt (z. B. unprivilegierter Debian-LXC auf Proxmox). Voraussetzung: ein OpenAI-API-Key und ein Telegram-Bot (via @BotFather).

```bash
git clone https://github.com/ProfessorEngineergit/ASTRA.git
cd ASTRA

cp .env.example .env
nano .env                 # Secrets eintragen (siehe Kommentare in der Datei)

docker compose build
docker compose up -d
docker compose logs -f cortex
```

Erfolgreich, wenn die Logs zeigen:

```
astra.db   : Postgres pool ready.
astra.main : Deferral sweeper started.
astra.main : Telegram poller started (mode=poll).
```

> **Tipp:** Mit `ASTRA_TELEGRAM_MODE=poll` (Default) brauchst du keine öffentliche Domain (Caddy), um mit dem Bot zu interagieren — der Poller holt Updates aktiv ab.

### Health-Check

```bash
curl http://127.0.0.1:8088/health      # → {"status":"ok"}
```

## 🔑 .env — Pflichtfelder

| Variable | Wofür |
|----------|-------|
| `POSTGRES_PASSWORD` | DB-Passwort (stark wählen) |
| `CORTEX_SHARED_SECRET` | authentifiziert cortex ⇄ n8n und die `/ingress/*`-Webhooks |
| `OPENAI_API_KEY` | LLM |
| `TELEGRAM_BOT_TOKEN` | dein Bot (@BotFather) |
| `TELEGRAM_OWNER_CHAT_ID` | deine numerische ID (@userinfobot) |
| `N8N_ENCRYPTION_KEY` | n8n (min. 24 Zeichen) |
| `WAHA_API_KEY` | WhatsApp-HTTP-API |
| `SIGNAL_PHONE_NUMBER` | deine registrierte Signal-Nummer |

Tipp für starke Werte: `openssl rand -hex 32`. Die `.env` ist `git-ignored` und wird nie commited.

## 📡 Kanäle anbinden

- **Telegram** — `ASTRA_TELEGRAM_MODE=poll` (Default): kein öffentlicher Port nötig, der Poller in `app/main.py` holt Updates aktiv ab. Buttons/Callbacks für Freigaben funktionieren sofort.
- **WhatsApp (WAHA)** — WAHA pusht eingehende Nachrichten an `http://cortex:8000/ingress/waha` (inkl. `X-Astra-Secret`-Header, siehe compose). QR-Code zum Pairing: `http://127.0.0.1:3000` (Dashboard-Login aus `.env`).
- **Signal** — eingehende Nachrichten an `POST /ingress/signal`. signal-cli pusht nicht von selbst; siehe „Offene Punkte".

## 🚚 Sende-Backend: `direct` vs. `n8n`

`ASTRA_SEND_BACKEND` (Default `direct`):

- **`direct`** — cortex ruft WAHA/Signal-APIs selbst auf (`app/channels.py`). Nichts weiter nötig.
- **`n8n`** — cortex POSTet an `tool/send_*`-Workflows. Dann die JSON-Workflows aus [`n8n/tools/`](n8n/tools/) in n8n importieren & aktivieren (Anleitung dort). Telegram geht in beiden Fällen immer direkt (niedrige Latenz + Buttons).

## 🔌 API-Endpoints (cortex)

| Methode | Pfad | Zweck |
|---------|------|-------|
| GET  | `/health` | Docker-Healthcheck |
| POST | `/ingress/waha` | WhatsApp-Eingang (WAHA) |
| POST | `/ingress/signal` | Signal-Eingang |
| POST | `/ingress/telegram` | nur wenn `ASTRA_TELEGRAM_MODE=webhook` |

Alle `/ingress/*` verlangen den Header `X-Astra-Secret: <CORTEX_SHARED_SECRET>`.

## ✅ Tests

```bash
cd cortex
.venv/bin/python -m pytest -q       # oder: python3 -m pytest -q
```

## 👨‍💻 Tech-Stack

- **Backend:** Python 3.11+, FastAPI, Pydantic, Asyncpg
- **KI/LLM:** OpenAI (GPT-4o, Embeddings), Mem0, Langfuse
- **Infrastruktur:** Docker Compose, Caddy, PostgreSQL (+ pgvector), Redis, n8n

## 🧭 Offene Punkte / nächste Schritte

- **Signal-Eingang:** `signal-cli-rest-api` im `json-rpc`-Mode pusht keine Webhooks. Optionen: (a) ein n8n-Schedule-Workflow, der `GET {SIGNAL_BASE_URL}/v1/receive/{NUMBER}` pollt und jede Nachricht an `/ingress/signal` weiterreicht, oder (b) den nativen Empfangs-Websocket nutzen. Ausgang funktioniert bereits.
- **Phase 2 Tools:** Kalender / Edupage / Smarthome / Booking als weitere `tool/*`-Workflows.

---

*Made with ❤️ for Bahrian — still building the future.*
