"""Home Assistant REST client.

Lets ASTRA read entity states, call services (actively change settings/devices),
and report which integrations/entities are unavailable ("warum ist X offline?").

Docs: https://developers.home-assistant.io/docs/api/rest/
Needs a long-lived access token (HA profile → Long-Lived Access Tokens).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import get_settings

log = logging.getLogger("astra.ha")


class HomeAssistant:
    def __init__(self) -> None:
        self.s = get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.ha_enabled

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.s.home_assistant_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.s.home_assistant_token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        async with self._client() as c:
            r = await c.get(f"/api/states/{entity_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def call_service(
        self, domain: str, service: str, data: dict | None = None
    ) -> bool:
        """e.g. domain='light', service='turn_on', data={'entity_id': 'light.desk'}."""
        if not self.enabled:
            return False
        if self.s.astra_dry_run:
            log.info("[DRY_RUN] HA %s.%s %s", domain, service, data)
            return True
        async with self._client() as c:
            r = await c.post(f"/api/services/{domain}/{service}", json=data or {})
            r.raise_for_status()
            return True

    async def unavailable_entities(self) -> list[dict[str, str]]:
        """Entities currently 'unavailable'/'unknown' — useful for 'what's offline?'."""
        if not self.enabled:
            return []
        async with self._client() as c:
            r = await c.get("/api/states")
            r.raise_for_status()
            out = []
            for st in r.json():
                if st.get("state") in ("unavailable", "unknown"):
                    out.append(
                        {
                            "entity_id": st.get("entity_id", ""),
                            "name": (st.get("attributes") or {}).get(
                                "friendly_name", st.get("entity_id", "")
                            ),
                            "state": st.get("state", ""),
                        }
                    )
            return out


_ha: HomeAssistant | None = None


def get_ha() -> HomeAssistant:
    global _ha
    if _ha is None:
        _ha = HomeAssistant()
    return _ha
