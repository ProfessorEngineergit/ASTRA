"""Trello — add a card to a list via the REST API."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://api.trello.com/1"


class TrelloPlugin(Plugin):
    slug = "trello"
    name = "Trello"
    description = "Karten zu einer Trello-Liste hinzufügen."
    category = PluginCategory.PRODUCTIVITY
    icon = "📌"
    config_fields = [
        ConfigField("api_key", "API-Key", required=True, secret=True,
                    help="trello.com/app-key"),
        ConfigField("token", "Token", type=FieldType.PASSWORD, required=True, secret=True,
                    help="Auf trello.com/app-key den 'Token'-Link nutzen"),
        ConfigField("list_id", "Listen-ID", required=True,
                    help="ID der Ziel-Liste (an Board-URL .json anhängen)"),
    ]

    def _auth(self) -> dict:
        return {"key": self.get("api_key"), "token": self.get("token")}

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{API}/members/me", params=self._auth())
            return (HealthStatus.ok(f"Verbunden als {r.json().get('username')}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Trello ist deaktiviert."
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.post(f"{API}/cards", params={**self._auth(),
                                                         "idList": self.get("list_id"),
                                                         "name": args.get("name", ""),
                                                         "desc": args.get("desc", "")})
            return "Trello-Karte angelegt." if r.status_code < 300 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="trello_add_card",
            description="Lege eine Karte in der Trello-Liste an.",
            parameters={"type": "object", "properties": {
                "name": {"type": "string"}, "desc": {"type": "string"}},
                "required": ["name"]},
            handler=_add, owner_only=True, source=self.slug,
        )]
