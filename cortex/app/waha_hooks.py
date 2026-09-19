"""WAHA-Webhook selbst sicherstellen.

Ursache des wochenlangen „WhatsApp antwortet nie": die WAHA-Session hatte KEINEN
Webhook (`config: null`). Der globale `WHATSAPP_HOOK_*`-Env greift bei aktuellen
WAHA-Versionen nicht, der Hook lebt pro Session — und geht bei einer frisch
gekoppelten Session verloren. Damit das nie wieder still passiert, prüft cortex den
Hook beim Start und danach regelmäßig und setzt ihn bei Bedarf selbst.

Nebeneffekt: wird `CORTEX_SHARED_SECRET` rotiert, heilt sich der Hook mit.
Die Entscheidung („muss gesetzt werden?") ist rein und testbar; nur `ensure()` redet
mit WAHA.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .config import get_settings

log = logging.getLogger("astra.waha_hooks")

HOOK_URL = "http://cortex:8000/ingress/waha"
HOOK_EVENTS = ["message", "message.any"]
PLACEHOLDER_SECRETS = frozenset({"", "dev-secret", "change-me", "change-me-too", "changeme"})


def secret_is_placeholder(secret: str) -> bool:
    s = (secret or "").strip().lower()
    return s in PLACEHOLDER_SECRETS or s.startswith("change-me") or len(s) < 12


def desired_webhook(secret: str, url: str = HOOK_URL) -> dict:
    return {"url": url, "events": list(HOOK_EVENTS),
            "customHeaders": [{"name": "X-Astra-Secret", "value": secret}]}


def needs_update(session: dict | None, secret: str, url: str = HOOK_URL) -> tuple[bool, str]:
    """(muss gesetzt werden?, Grund). Rein: vergleicht Session-Config mit dem Soll."""
    if not session:
        return False, "keine Session"
    if session.get("status") in ("STOPPED", "FAILED"):
        return False, f"Session {session.get('status')} (nicht startklar)"
    hooks = ((session.get("config") or {}).get("webhooks")) or []
    if not hooks:
        return True, "kein Webhook gesetzt"
    for h in hooks:
        if h.get("url") != url:
            continue
        headers = {c.get("name"): c.get("value") for c in (h.get("customHeaders") or [])}
        if headers.get("X-Astra-Secret") != secret:
            return True, "Secret weicht ab"
        if not set(HOOK_EVENTS) <= set(h.get("events") or []):
            return True, "Events unvollständig"
        return False, "ok"
    return True, "Webhook zeigt woandershin"


async def _runtime() -> tuple[str, str, str]:
    """(base_url, session, api_key) — gleiche Quelle wie das Senden (Admin > .env)."""
    from .channels import get_channels
    return await get_channels()._waha_runtime_config()


async def ensure(*, client: httpx.AsyncClient | None = None) -> str:
    """Hook prüfen und ggf. setzen. Gibt einen Kurzstatus zurück; wirft nie."""
    s = get_settings()
    try:
        base, session, key = await _runtime()
    except Exception as e:  # noqa: BLE001
        return f"kein WAHA konfiguriert ({e})"
    if not base:
        return "kein WAHA konfiguriert"
    headers = {"X-Api-Key": key} if key else {}
    own = client is None
    c = client or httpx.AsyncClient(timeout=15)
    try:
        r = await c.get(f"{base}/api/sessions/{session}", headers=headers)
        if r.status_code == 404:
            return f"Session '{session}' existiert nicht (noch nicht gekoppelt)"
        r.raise_for_status()
        need, why = needs_update(r.json(), s.cortex_shared_secret)
        if not need:
            return f"ok ({why})"
        put = await c.put(f"{base}/api/sessions/{session}", headers=headers,
                          json={"config": {"webhooks": [desired_webhook(s.cortex_shared_secret)]}})
        put.raise_for_status()
        log.warning("WAHA-Webhook war nicht gesetzt (%s) — automatisch repariert.", why)
        return f"repariert ({why})"
    except Exception as e:  # noqa: BLE001
        return f"Fehler: {e}"
    finally:
        if own:
            await c.aclose()


async def watchdog(interval: float = 300.0) -> None:
    """Hintergrund-Task: Hook beim Start und dann alle `interval` s prüfen."""
    s = get_settings()
    if secret_is_placeholder(s.cortex_shared_secret):
        log.warning("CORTEX_SHARED_SECRET ist ein Platzhalter/zu kurz — bitte rotieren "
                    "(scripts/rotate_secret.sh). Jeder im LAN könnte sonst Nachrichten einschleusen.")
    await asyncio.sleep(20)        # WAHA/Compose hochkommen lassen
    while True:
        try:
            log.info("WAHA-Webhook-Check: %s", await ensure())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("waha watchdog error", exc_info=True)
        await asyncio.sleep(interval)
