"""Home Assistant — active control + status (REST API)."""
from __future__ import annotations

import json as _json
import logging
from typing import Any

import httpx

from ...config import get_settings
from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.ha")


class HomeAssistantPlugin(Plugin):
    slug = "home_assistant"
    name = "Home Assistant"
    description = "Geräte/Settings aktiv schalten, Zustände lesen, offline-Entitäten finden."
    category = PluginCategory.SMART_HOME
    icon = "🏠"
    config_fields = [
        ConfigField("base_url", "Base-URL", required=True,
                    help="z.B. http://192.168.178.50:8123", env_fallback="home_assistant_base_url"),
        ConfigField("token", "Long-Lived Access Token", FieldType.PASSWORD, required=True,
                    secret=True, env_fallback="home_assistant_token"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=str(self.get("base_url", "")).rstrip("/"),
            headers={"Authorization": f"Bearer {self.get('token')}",
                     "Content-Type": "application/json"},
            timeout=15,
        )

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        async with self._client() as c:
            r = await c.get(f"/api/states/{entity_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def call_service(self, domain: str, service: str, data: dict | None = None) -> bool:
        if get_settings().astra_dry_run:
            log.info("[DRY_RUN] HA %s.%s %s", domain, service, data)
            return True
        async with self._client() as c:
            r = await c.post(f"/api/services/{domain}/{service}", json=data or {})
            r.raise_for_status()
            return True

    async def unavailable_entities(self) -> list[dict[str, str]]:
        async with self._client() as c:
            r = await c.get("/api/states")
            r.raise_for_status()
            out = []
            for st in r.json():
                if st.get("state") in ("unavailable", "unknown"):
                    out.append({"entity_id": st.get("entity_id", ""),
                                "name": (st.get("attributes") or {}).get("friendly_name",
                                                                         st.get("entity_id", "")),
                                "state": st.get("state", "")})
            return out

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/api/")
                r.raise_for_status()
            return HealthStatus.ok("HA-API erreichbar.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"HA-API: {e}")

    def tools(self) -> list[Tool]:
        async def _state(args: dict, ctx: ToolContext) -> str:
            if args.get("unavailable_only"):
                offline = await self.unavailable_entities()
                if not offline:
                    return "Keine Entität ist offline/unavailable."
                return "Offline/unavailable:\n- " + "\n- ".join(
                    f"{e['name']} ({e['entity_id']}): {e['state']}" for e in offline[:25])
            st = await self.get_state(args.get("entity_id", ""))
            if not st:
                return f"Keine Entität '{args.get('entity_id')}' gefunden."
            attrs = st.get("attributes") or {}
            return f"{attrs.get('friendly_name', args.get('entity_id'))} = {st.get('state')}"

        async def _call(args: dict, ctx: ToolContext) -> str:
            domain, service = args.get("domain", ""), args.get("service", "")
            data = args.get("data") or {}
            if isinstance(data, str):
                try:
                    data = _json.loads(data)
                except Exception:  # noqa: BLE001
                    data = {}
            if not domain or not service:
                return "domain und service sind erforderlich (z.B. light.turn_on)."
            ok = await self.call_service(domain, service, data)
            return f"{domain}.{service} ausgeführt." if ok else f"{domain}.{service} fehlgeschlagen."

        return [
            Tool(name="home_assistant_state",
                 description="Lies eine HA-Entität ODER liste alle offline/unavailable Entitäten.",
                 parameters={"type": "object", "properties": {
                     "entity_id": {"type": "string"},
                     "unavailable_only": {"type": "boolean"}}},
                 handler=_state, owner_only=True, source=self.slug),
            Tool(name="home_assistant_call",
                 description="Rufe einen HA-Service auf, um aktiv etwas zu schalten/ändern.",
                 parameters={"type": "object", "properties": {
                     "domain": {"type": "string"}, "service": {"type": "string"},
                     "data": {"type": "object"}}, "required": ["domain", "service"]},
                 handler=_call, owner_only=True, source=self.slug),
        ]
