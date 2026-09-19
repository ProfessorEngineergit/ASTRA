#!/usr/bin/env bash
# CORTEX_SHARED_SECRET rotieren (cortex <-> WAHA-Webhook <-> n8n). Auf der Box ausführen:
#   cd /opt/astra && bash scripts/rotate_secret.sh
# Setzt ein neues zufälliges Secret in .env und startet cortex + waha neu. Der
# Webhook der WAHA-Session wird danach von cortex selbst nachgezogen (waha_hooks).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env fehlt" >&2; exit 1; }
NEW="$(openssl rand -hex 24)"
cp .env ".env.bak.$(date +%s)"
if grep -q '^CORTEX_SHARED_SECRET=' .env; then
  sed -i.tmp "s|^CORTEX_SHARED_SECRET=.*|CORTEX_SHARED_SECRET=${NEW}|" .env && rm -f .env.tmp
else
  echo "CORTEX_SHARED_SECRET=${NEW}" >> .env
fi
docker compose up -d --force-recreate cortex waha
echo "Secret rotiert. cortex setzt den WAHA-Webhook in ~20 s selbst neu (Log: 'WAHA-Webhook-Check')."
