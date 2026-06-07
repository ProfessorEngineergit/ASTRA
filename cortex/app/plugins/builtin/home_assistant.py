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

    async def states(self) -> list[dict[str, Any]]:
        async with self._client() as c:
            r = await c.get("/api/states")
            r.raise_for_status()
            return r.json()

    async def search_states(
        self, query: str = "", *, domain: str = "", limit: int = 20
    ) -> list[dict[str, str]]:
        q = (query or "").strip().lower()
        wanted_domain = (domain or "").strip().lower().removesuffix(".")
        matches: list[dict[str, str]] = []
        for st in await self.states():
            entity_id = str(st.get("entity_id", ""))
            attrs = st.get("attributes") or {}
            friendly = str(attrs.get("friendly_name") or entity_id)
            entity_domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
            if wanted_domain and entity_domain != wanted_domain:
                continue
            haystack = " ".join(
                str(v)
                for v in (
                    entity_id,
                    friendly,
                    st.get("state", ""),
                    attrs.get("device_class", ""),
                    attrs.get("unit_of_measurement", ""),
                )
            ).lower()
            if q and q not in haystack:
                continue
            matches.append({
                "entity_id": entity_id,
                "name": friendly,
                "state": str(st.get("state", "")),
                "unit": str(attrs.get("unit_of_measurement") or ""),
                "domain": entity_domain,
            })
            if len(matches) >= max(1, min(int(limit or 20), 60)):
                break
        return matches

    async def render_template(self, template: str) -> str:
        async with self._client() as c:
            r = await c.post("/api/template", json={"template": template})
            r.raise_for_status()
            return r.text.strip()

    async def area_overview(self, area: str = "", *, domain: str = "", limit: int = 40) -> str:
        area_lit = _json.dumps((area or "").strip().lower())
        domain_lit = _json.dumps((domain or "").strip().lower().removesuffix("."))
        limit_n = max(1, min(int(limit or 40), 120))
        template = f"""
{{% set wanted_area = {area_lit} %}}
{{% set wanted_domain = {domain_lit} %}}
{{% set ns = namespace(count=0) %}}
{{% for area_name in areas() | sort %}}
{{% if (not wanted_area or wanted_area in (area_name | lower)) and ns.count < {limit_n} %}}
{{{{ area_name }}}}:
{{% for entity in area_entities(area_name) | sort %}}
{{% if (not wanted_domain or entity.split('.')[0] == wanted_domain) and ns.count < {limit_n} %}}
- {{{{ entity }}}} | {{{{ state_attr(entity, 'friendly_name') or entity }}}} | {{{{ states(entity) }}}}
{{% set ns.count = ns.count + 1 %}}
{{% endif %}}
{{% endfor %}}
{{% endif %}}
{{% endfor %}}
""".strip()
        return await self.render_template(template)

    async def services(self) -> list[dict[str, Any]]:
        async with self._client() as c:
            r = await c.get("/api/services")
            r.raise_for_status()
            return r.json()

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
            entity_id = (args.get("entity_id") or "").strip()
            query = (args.get("query") or "").strip()
            if not entity_id and query:
                matches = await self.search_states(query, domain=args.get("domain", ""), limit=10)
                if not matches:
                    return f"Keine Home-Assistant-Entität zu '{query}' gefunden."
                return "Gefundene HA-Entitäten:\n- " + "\n- ".join(
                    f"{m['name']} ({m['entity_id']}) = {m['state']}{(' ' + m['unit']).rstrip()}"
                    for m in matches
                )
            st = await self.get_state(entity_id)
            if not st:
                return f"Keine Entität '{entity_id}' gefunden."
            attrs = st.get("attributes") or {}
            unit = attrs.get("unit_of_measurement") or ""
            return f"{attrs.get('friendly_name', entity_id)} ({entity_id}) = {st.get('state')} {unit}".strip()

        async def _search(args: dict, ctx: ToolContext) -> str:
            matches = await self.search_states(
                args.get("query", ""), domain=args.get("domain", ""), limit=args.get("limit", 20)
            )
            if not matches:
                return "Keine passenden Home-Assistant-Entitäten gefunden."
            return "HA-Suche:\n- " + "\n- ".join(
                f"{m['name']} ({m['entity_id']}) = {m['state']}{(' ' + m['unit']).rstrip()}"
                for m in matches
            )

        async def _areas(args: dict, ctx: ToolContext) -> str:
            try:
                text = await self.area_overview(
                    args.get("area", ""), domain=args.get("domain", ""), limit=args.get("limit", 40)
                )
            except Exception as e:  # noqa: BLE001
                return (
                    "Home-Assistant-Räume konnten nicht gelesen werden "
                    f"({e}). Suche stattdessen mit search_home_assistant nach Entitäten."
                )
            cleaned = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
            return cleaned or "Keine Home-Assistant-Räume/Entitäten gefunden."

        async def _services(args: dict, ctx: ToolContext) -> str:
            rows = []
            domain_filter = (args.get("domain") or "").strip().lower()
            for domain_block in await self.services():
                domain_name = str(domain_block.get("domain") or "")
                if domain_filter and domain_name != domain_filter:
                    continue
                services = domain_block.get("services") or {}
                rows.append(f"{domain_name}: {', '.join(sorted(services)[:40])}")
            return "HA-Services:\n- " + "\n- ".join(rows[:30]) if rows else "Keine HA-Services gefunden."

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
                 description=(
                     "Lies eine Home-Assistant-Entität per entity_id, suche per query/friendly name "
                     "oder liste offline/unavailable Entitäten."
                 ),
                 parameters={"type": "object", "properties": {
                     "entity_id": {"type": "string"},
                     "query": {"type": "string", "description": "Friendly Name, Raumname oder Teil der entity_id"},
                     "domain": {"type": "string", "description": "Optional z. B. sensor, light, switch"},
                     "unavailable_only": {"type": "boolean"}}},
                 handler=_state, owner_only=True, source=self.slug),
            Tool(name="search_home_assistant",
                 description="Suche Home-Assistant-Entitäten nach Friendly Name, entity_id, Domain oder Zustand.",
                 parameters={"type": "object", "properties": {
                     "query": {"type": "string"},
                     "domain": {"type": "string", "description": "Optional z. B. sensor, light, switch"},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 60}}},
                 handler=_search, owner_only=True, source=self.slug),
            Tool(name="list_home_assistant_areas",
                 description=(
                     "Liste Home-Assistant-Räume/Bereiche und deren Entitäten. Nutze das für Fragen wie "
                     "'zeige Sensoren im Wohnzimmer'."
                 ),
                 parameters={"type": "object", "properties": {
                     "area": {"type": "string", "description": "Optionaler Raum-/Bereichsname"},
                     "domain": {"type": "string", "description": "Optional z. B. sensor, light, switch"},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 120}}},
                 handler=_areas, owner_only=True, source=self.slug),
            Tool(name="list_home_assistant_services",
                 description="Liste verfügbare Home-Assistant-Service-Domains und Services für gezielte Aktionen.",
                 parameters={"type": "object", "properties": {
                     "domain": {"type": "string", "description": "Optional z. B. light oder climate"}}},
                 handler=_services, owner_only=True, source=self.slug),
            Tool(name="home_assistant_call",
                 description="Rufe einen HA-Service auf, um aktiv etwas zu schalten/ändern.",
                 parameters={"type": "object", "properties": {
                     "domain": {"type": "string"}, "service": {"type": "string"},
                     "data": {"type": "object"}}, "required": ["domain", "service"]},
                 handler=_call, owner_only=True, source=self.slug),
        ]
