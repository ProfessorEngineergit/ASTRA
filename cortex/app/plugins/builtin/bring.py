"""Bring! — shopping list via the unofficial Bring API."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.bring")

_API = "https://api.getbring.com/rest/v2"


class BringPlugin(Plugin):
    slug = "bring"
    name = "Bring! Einkaufsliste"
    description = "Einkaufsliste in der Bring!-App lesen, Artikel hinzufügen und entfernen."
    category = PluginCategory.PRODUCTIVITY
    icon = "🛒"
    config_fields = [
        ConfigField("email", "E-Mail", required=True),
        ConfigField("password", "Passwort", FieldType.PASSWORD, required=True, secret=True),
    ]

    # ── auth helpers ──────────────────────────────────────────────────────────
    async def _login(self) -> tuple[str, str]:
        """Login and return (access_token, default_list_uuid)."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{_API}/bringauth/login",
                data={"email": self.get("email", ""),
                      "password": self.get("password", "")},
                headers={"X-BRING-CLIENT": "webApp",
                         "X-BRING-COUNTRY": "DE",
                         "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Yw"},
            )
            r.raise_for_status()
            data = r.json()
        token = data.get("access_token", "")
        # Get the default list UUID
        async with httpx.AsyncClient(timeout=10) as c:
            lr = await c.get(
                f"{_API}/bringlists",
                headers={"Authorization": f"Bearer {token}",
                         "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Yw"},
            )
            lr.raise_for_status()
            lists = lr.json().get("lists", [])
        list_uuid = lists[0]["listUuid"] if lists else ""
        return token, list_uuid

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            token, list_uuid = await self._login()
            if token:
                return HealthStatus.ok(f"Eingeloggt, Default-Liste: {list_uuid[:8]}…")
            return HealthStatus.error("Login fehlgeschlagen.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _items(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                token, list_uuid = await self._login()
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(
                        f"{_API}/bringitems/{list_uuid}",
                        headers={"Authorization": f"Bearer {token}",
                                 "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Yw"},
                    )
                    r.raise_for_status()
                items = r.json().get("purchase", [])
                if not items:
                    return "Einkaufsliste ist leer."
                return "**Einkaufsliste:**\n" + "\n".join(
                    f"- {i['name']}" + (f" ({i['specification']})" if i.get("specification") else "")
                    for i in items
                )
            except Exception as e:
                return f"Bring!-Fehler: {e}"

        async def _add(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            item = args.get("item", "").strip()
            if not item:
                return "item ist erforderlich."
            specification = args.get("specification", "")
            try:
                token, list_uuid = await self._login()
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.put(
                        f"{_API}/bringitems/{list_uuid}",
                        headers={"Authorization": f"Bearer {token}",
                                 "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Yw"},
                        data={"purchase": item, "recently": "",
                              "specification": specification},
                    )
                    r.raise_for_status()
                return f"'{item}' zur Einkaufsliste hinzugefügt."
            except Exception as e:
                return f"Bring!-Fehler: {e}"

        async def _remove(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            item = args.get("item", "").strip()
            if not item:
                return "item ist erforderlich."
            try:
                token, list_uuid = await self._login()
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.delete(
                        f"{_API}/bringitems/{list_uuid}",
                        headers={"Authorization": f"Bearer {token}",
                                 "X-BRING-API-KEY": "cof4Nc6D8saplXjE3h3HXqHH8m7VU2i1Gs0g85Yw"},
                        params={"purchase": item},
                    )
                    r.raise_for_status()
                return f"'{item}' von der Einkaufsliste entfernt."
            except Exception as e:
                return f"Bring!-Fehler: {e}"

        return [
            Tool(
                name="bring_items",
                description="Aktuelle Bring!-Einkaufsliste anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_items, owner_only=True, source=self.slug,
            ),
            Tool(
                name="bring_add",
                description="Artikel zur Bring!-Einkaufsliste hinzufügen.",
                parameters={"type": "object", "properties": {
                    "item": {"type": "string", "description": "Artikelname"},
                    "specification": {"type": "string",
                                      "description": "Zusatzangabe (Marke, Menge…)"},
                }, "required": ["item"]},
                handler=_add, owner_only=True, source=self.slug,
            ),
            Tool(
                name="bring_remove",
                description="Artikel von der Bring!-Einkaufsliste entfernen.",
                parameters={"type": "object", "properties": {
                    "item": {"type": "string", "description": "Artikelname"},
                }, "required": ["item"]},
                handler=_remove, owner_only=True, source=self.slug,
            ),
        ]
