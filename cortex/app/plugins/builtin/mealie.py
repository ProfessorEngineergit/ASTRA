"""Mealie — recipe search + today's meal plan via the REST API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class MealiePlugin(Plugin):
    slug = "mealie"
    name = "Mealie"
    description = "Rezepte suchen & heutigen Essensplan abrufen (Mealie)."
    category = PluginCategory.PRODUCTIVITY
    icon = "🍽️"
    config_fields = [
        ConfigField("base_url", "Mealie-URL", required=True, help="z. B. https://mealie.example.com"),
        ConfigField("token", "API-Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Mealie → Profil → API-Tokens"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=12, base_url=self.get("base_url").rstrip("/"),
                                 headers={"Authorization": f"Bearer {self.get('token')}"})

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with self._client() as c:
                r = await c.get("/api/app/about")
            return (HealthStatus.ok(f"Mealie {r.json().get('version', '')} erreichbar.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Mealie ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/recipes", params={"search": args.get("query", ""), "perPage": 5})
            items = r.json().get("items", [])
            if not items:
                return "Keine Rezepte gefunden."
            return "\n".join(f"• {i['name']}" for i in items)

        async def _today(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Mealie ist deaktiviert."
            async with self._client() as c:
                r = await c.get("/api/households/mealplans/today")
            items = r.json() if isinstance(r.json(), list) else []
            if not items:
                return "Kein Essensplan für heute."
            return "🍽️ Heute:\n" + "\n".join(
                f"• {m.get('recipe', {}).get('name') or m.get('title', '?')}" for m in items)

        return [
            Tool(name="mealie_search", description="Suche Rezepte in Mealie.",
                 parameters={"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]},
                 handler=_search, owner_only=True, source=self.slug),
            Tool(name="mealie_today", description="Heutiger Essensplan aus Mealie.",
                 parameters={"type": "object", "properties": {}},
                 handler=_today, owner_only=True, source=self.slug),
        ]
