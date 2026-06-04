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

- **🧠 Cortex (Python/FastAPI):** Das zentrale Gehirn. Hier liegt die gesamte Geschäftslogik, das Gedächtnis, die Richtlinien (Policies) und die "Human-in-the-Loop"-Steuerung.
- **⚙️ n8n:** Zustandslose Workflow-Engine. Beinhaltet keine Business-Logik, sondern stellt dem Cortex lediglich Tools und API-Schnittstellen (als Workflows) zur Verfügung.
- **🗄️ PostgreSQL & pgvector:** Datenbank für App-State, Langfuse und Vektorspeicher (Memory/Embeddings).
- **🔴 Redis:** Kümmert sich um den State-Machine-Zustand der Threads, Deferral Timer und Pub/Sub-Events.
- **💬 Messenger-Gateways:**
  - **WAHA:** WhatsApp-Anbindung.
  - **Signal-CLI:** Signal-Anbindung.
  - **Telegram:** Haupt-Steuerkanal und Genehmigungs-Channel für den Besitzer.
- **👁️ Langfuse:** LLM-Tracing und Debugging für maximale Transparenz bei KI-Entscheidungen.
- **🌐 Caddy:** Reverse Proxy für sicheren TLS-Zugriff von außen.

## 🛠️ Installation & Setup

ASTRA ist für ein Self-Hosted Setup via Docker Compose ausgelegt.

1. **Repository klonen**
   ```bash
   git clone https://github.com/ProfessorEngineergit/ASTRA.git
   cd ASTRA
   ```

2. **Umgebungsvariablen konfigurieren**
   Kopiere die Beispiel-Konfiguration und fülle sie mit deinen Tokens (OpenAI, Telegram, Passwörter etc.) aus.
   ```bash
   cp .env.example .env
   # .env bearbeiten
   ```

3. **Container starten**
   Lass Docker Compose alle Services herunterladen und starten.
   ```bash
   docker-compose up -d
   ```

> **Tipp:** Für den ersten lokalen Test kannst du `ASTRA_TELEGRAM_MODE=poll` setzen, sodass du keine öffentliche Domain (Caddy) benötigst, um mit dem Bot zu interagieren.

## 👨‍💻 Tech-Stack

- **Backend:** Python 3.11+, FastAPI, Pydantic, Asyncpg
- **KI/LLM:** OpenAI (GPT-4o, Embeddings), Mem0, Langfuse
- **Infrastruktur:** Docker Compose, Caddy, PostgreSQL (+ pgvector), Redis, n8n

---

*Made with ❤️ for Bahrian — still building the future.*
