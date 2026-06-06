"""Portainer — container overview via the API (access token)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class PortainerPlugin(Plugin):
    slug = "portainer"
    name = "Portainer"
    description = "Container-Übersicht & Status über Portainer."
    category = PluginCategory.INFRA_AI
    icon = "🐋"
    config_fields = [
        ConfigField("base_url", "Portainer-URL", required=True, help="z. B. https://portainer.example.com"),
        ConfigField("api_key", "API-Key", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Portainer → My account → Access tokens"),
        ConfigField("endpoint_id", "Endpoint-ID", type=FieldType.NUMBER, default=1,
                    help="Meist 1 (lokale Docker-Umgebung)"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, verify=False, base_url=self.get("base_url").rstrip("/"),
                                 headers={"X-API-Key": self.get("api_key")})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/endpoints")
            return (HealthStatus.ok(f"{len(r.json())} Endpoint(s) erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _ps(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Portainer ist deaktiviert."
            eid = int(self.get("endpoint_id", 1))
            async with self._client() as c:
                r = await c.get(f"/api/endpoints/{eid}/docker/containers/json",
                                params={"all": "true"})
            items = r.json()
            if not isinstance(items, list) or not items:
                return "Keine Container."
            out = []
            for ct in items[:20]:
                name = (ct.get("Names", ["?"])[0]).lstrip("/")
                out.append(f"• {name}: {ct.get('State')} ({ct.get('Status', '')})")
            return "🐋 Container:\n" + "\n".join(out)

        return [Tool(
            name="portainer_containers",
            description="Liste Docker-Container mit Status (über Portainer).",
            parameters={"type": "object", "properties": {}},
            handler=_ps, owner_only=True, source=self.slug,
        )]
