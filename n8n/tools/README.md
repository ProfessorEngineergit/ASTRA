# n8n Tool-Workflows

Stateless Send-Workflows für ASTRA. Sie werden **nur** genutzt, wenn in der `.env`
`ASTRA_SEND_BACKEND=n8n` gesetzt ist (Default ist `direct` — dann ruft cortex die
APIs selbst auf und diese Workflows werden nicht gebraucht).

| Datei | Webhook-Pfad | Ruft auf |
|-------|--------------|----------|
| `send_waha.json`     | `POST /webhook/tool/send_waha`     | WAHA `/api/sendText` |
| `send_signal.json`   | `POST /webhook/tool/send_signal`   | signal-cli `/v2/send` |
| `send_telegram.json` | `POST /webhook/tool/send_telegram` | Telegram `sendMessage` |

Alle drei erwarten den Body `{ "to": "...", "text": "..." }` und prüfen den
Header `X-Astra-Secret` gegen `$env.CORTEX_SHARED_SECRET` (gibt sonst 403 zurück).

## Import

1. n8n öffnen → `http://127.0.0.1:5678` (User/Passwort aus `.env`).
2. Oben rechts **⋯ → Import from File** → die `.json` wählen.
3. Den Workflow **aktivieren** (Toggle oben rechts).
4. Für die anderen beiden wiederholen.

Die nötigen Env-Variablen (`WAHA_BASE_URL`, `WAHA_API_KEY`, `WAHA_SESSION`,
`SIGNAL_BASE_URL`, `SIGNAL_PHONE_NUMBER`, `TELEGRAM_BOT_TOKEN`) werden vom
`n8n`-Service in `docker-compose.yml` bereits aus der `.env` durchgereicht.

## Schnelltest (vom Server aus)

```bash
curl -X POST http://127.0.0.1:5678/webhook/tool/send_telegram \
  -H "X-Astra-Secret: $CORTEX_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"to":"<DEINE_CHAT_ID>","text":"n8n send_telegram works ✅"}'
```
