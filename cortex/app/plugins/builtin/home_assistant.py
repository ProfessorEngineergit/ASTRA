"""Home Assistant — active control + status (REST API)."""
from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any

import httpx

from ... import world
from ...config import get_settings
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.ha")

# Domains worth putting into the world model. Everything else (automation,
# script, update, sun, tts, …) is machinery the user never names out loud.
_WORLD_DOMAINS = (
    "light", "switch", "climate", "sensor", "binary_sensor", "cover",
    "media_player", "fan", "lock", "vacuum", "humidifier", "water_heater",
    "camera", "scene", "person", "device_tracker",
)

# What each domain can be asked to do — lets other subsystems (e.g. the notify
# router picking a speaker) find capable targets without knowing HA internals.
_CONTROL_CAPS: dict[str, tuple[str, ...]] = {
    "light": ("turn_on", "turn_off", "toggle", "set_brightness"),
    "switch": ("turn_on", "turn_off", "toggle"),
    "fan": ("turn_on", "turn_off", "toggle"),
    "climate": ("set_temperature",),
    "cover": ("open", "close", "set_position"),
    "media_player": ("play", "pause", "speak", "set_volume"),
    "lock": ("lock", "unlock"),
    "vacuum": ("start", "stop"),
    "scene": ("activate",),
    "humidifier": ("turn_on", "turn_off"),
    "water_heater": ("turn_on", "turn_off"),
}

# Free-text action → HA service. Folded before lookup, so "Anmachen" works.
_ACTION_ON = frozenset({"an", "ein", "on", "anmachen", "einschalten", "anschalten",
                        "aktivieren", "start", "starten", "auf", "oeffnen", "hoch"})
_ACTION_OFF = frozenset({"aus", "off", "ausmachen", "ausschalten", "abschalten",
                         "deaktivieren", "stop", "stoppen", "zu", "schliessen", "runter"})
_ACTION_TOGGLE = frozenset({"toggle", "umschalten", "wechseln", "umdrehen"})
_ACTION_SET = frozenset({"set", "setzen", "stelle", "stellen", "aendern", "regeln", "auf"})


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
        # Optional — power the notify router (W3): phone push, spoken output, presence.
        ConfigField("notify_service", "Companion Push-Service", required=False,
                    help="z. B. notify.mobile_app_iphone (für Push aufs Handy)"),
        ConfigField("tts_engine", "TTS-Engine (Entity)", required=False,
                    help="z. B. tts.google_de (für gesprochene Ausgabe)"),
        ConfigField("person_entity", "Deine Person (Anwesenheit)", required=False,
                    help="z. B. person.bahrian (für 'bin ich zu Hause')"),
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

    # ── World model provider ─────────────────────────────────────────────────
    # HA's REST API has no area registry, so we ask HA to walk its own registry
    # for us: one template render returns the whole topology (area, entity,
    # friendly name, device class) in a single request instead of a state dump.
    _WORLD_TEMPLATE = """
{%- set doms = DOMAINS %}
{%- for a in areas() | sort %}
{%- set an = area_name(a) or a %}
@{{ an }}
{%- for e in area_entities(a) | sort %}
{%- if e.split('.')[0] in doms %}
{{ e }}|{{ state_attr(e, 'friendly_name') or e }}|{{ state_attr(e, 'device_class') or '' }}
{%- endif %}
{%- endfor %}
{%- endfor %}
""".strip()

    async def world_nodes(self) -> list[world.Node]:
        if not self.enabled:
            return []
        template = self._WORLD_TEMPLATE.replace("DOMAINS", _json.dumps(list(_WORLD_DOMAINS)))
        try:
            raw = await self.render_template(template)
        except Exception as e:  # noqa: BLE001 — a dead HA must not break the resolver
            log.warning("HA world topology unavailable: %s", e)
            return []
        nodes: list[world.Node] = []
        area = ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("@"):
                area = line[1:].strip()
                if area:
                    nodes.append(world.Node(
                        id=f"area:{area}", kind="area", area=area,
                        names=(area,), source=self.slug,
                    ))
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            entity_id, friendly = parts[0].strip(), parts[1].strip()
            device_class = parts[2].strip() if len(parts) > 2 else ""
            if not entity_id or "." not in entity_id:
                continue
            domain = entity_id.split(".", 1)[0]
            caps = list(_CONTROL_CAPS.get(domain, ()))
            if device_class:
                caps.append(f"class:{device_class}")
            nodes.append(world.Node(
                id=entity_id, kind=domain, area=area,
                names=(friendly,) if friendly else (),
                caps=tuple(caps), source=self.slug,
            ))
        return nodes

    async def read_values(self, entity_ids: list[str]) -> list[dict[str, str]]:
        """Fresh values for already-resolved entities (concurrent, best effort)."""
        results = await asyncio.gather(
            *(self.get_state(eid) for eid in entity_ids), return_exceptions=True
        )
        out: list[dict[str, str]] = []
        for entity_id, st in zip(entity_ids, results):
            if isinstance(st, BaseException) or not st:
                out.append({"entity_id": entity_id, "name": entity_id,
                            "state": "unbekannt", "unit": ""})
                continue
            attrs = st.get("attributes") or {}
            out.append({
                "entity_id": entity_id,
                "name": str(attrs.get("friendly_name") or entity_id),
                "state": str(st.get("state", "")),
                "unit": str(attrs.get("unit_of_measurement") or ""),
            })
        return out

    async def services(self) -> list[dict[str, Any]]:
        async with self._client() as c:
            r = await c.get("/api/services")
            r.raise_for_status()
            return r.json()

    # ── Delivery: phone push, spoken output, presence (W3 notify router) ──────
    async def notify_push(
        self, message: str, *, title: str = "", actions: list[dict] | None = None,
        service: str = "",
    ) -> bool:
        """Push to the HA Companion App (iOS + Android). `service` like
        'notify.mobile_app_x'; falls back to the configured notify_service."""
        service = service or str(self.get("notify_service") or "")
        if "." not in service:
            return False
        domain, name = service.split(".", 1)
        data: dict[str, Any] = {"message": message}
        if title:
            data["title"] = title
        if actions:
            data["data"] = {"actions": actions}
        return await self.call_service(domain, name, data)

    async def speak(self, text: str, *, media_player: str, engine: str = "") -> bool:
        """Speak `text` on a media_player via the configured TTS engine."""
        engine = engine or str(self.get("tts_engine") or "")
        if not engine or not media_player:
            return False
        return await self.call_service("tts", "speak", {
            "entity_id": engine,
            "media_player_entity_id": media_player,
            "message": text,
        })

    async def presence(self, entity_id: str = "") -> dict[str, Any]:
        """State of a person/device_tracker: {state, at_home}. '' → configured person."""
        entity_id = entity_id or str(self.get("person_entity") or "")
        if not entity_id:
            return {"state": "unknown", "at_home": None, "entity_id": ""}
        st = await self.get_state(entity_id)
        state = str((st or {}).get("state") or "unknown")
        return {"state": state, "at_home": state == "home", "entity_id": entity_id}

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

        # ── Intent-shaped tools (resolver-backed) ────────────────────────────
        # `what`/`where` stay SEPARATE fields on purpose: the resolver never has
        # to parse "Temperatur im Wohnzimmer" as one string, which is exactly
        # what breaks the plain substring search below.
        def _names(nodes: list[world.Node]) -> str:
            return ", ".join(
                f"{n.label} ({n.area})" if n.area and n.area != n.label else n.label
                for n in nodes
            )

        def _area_of(res: world.Resolution) -> str:
            return (res.node.area or res.node.label) if res.node else ""

        def _area_error(where: str, res: world.Resolution, nodes: list[world.Node]) -> str | None:
            """Turn a failed area lookup into a helpful answer, or None if it worked."""
            if res.status == "unique":
                return None
            if res.status == "ambiguous":
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Mehrere Räume passen zu „{where}“: {_names(res.candidates)}. "
                            "Welchen meinst du?",
                    data={"ambiguous": True,
                          "candidates": [n.label for n in res.candidates]},
                )
            available = world.areas(nodes)
            return tool_result(
                ok=False, source=self.slug,
                summary=f"Einen Raum „{where}“ kenne ich nicht. Vorhanden: "
                        f"{', '.join(available) or '(keine Räume in Home Assistant vergeben)'}.",
                data={"areas": available},
            )

        async def _home_state(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug,
                                   summary="Home Assistant ist nicht konfiguriert.")
            what = str(args.get("what") or "").strip()
            where = str(args.get("where") or "").strip()
            nodes = await world.snapshot()
            if not nodes:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Das Weltmodell ist leer — sind in Home Assistant überhaupt "
                            "Bereiche/Räume vergeben?",
                )
            area = ""
            if where:
                res = world.resolve_in(nodes, where, kinds=("area",))
                if (err := _area_error(where, res, nodes)):
                    return err
                area = _area_of(res)

            kinds, device_class = world.intent_filter(what)
            pool = [n for n in world.filter_nodes(nodes, kinds=kinds,
                                                  device_class=device_class, area=area)
                    if n.kind != "area"]

            if not pool:
                present = [n for n in world.filter_nodes(nodes, area=area) if n.kind != "area"]
                if present:
                    have = sorted({world.thing_label(n) for n in present})
                    return tool_result(
                        ok=False, source=self.slug,
                        summary=f"Zu „{what or 'das'}“ finde ich in "
                                f"{area or 'deinem Zuhause'} nichts. Dort gibt es: "
                                f"{', '.join(have)}.",
                        data={"area": area, "available": have},
                    )
                # Last resort: the old substring search (weaker, but better than nothing).
                matches = await self.search_states(what, limit=8)
                if matches:
                    return tool_result(
                        ok=True, source=self.slug,
                        summary="Über die Namenssuche gefunden: " + " · ".join(
                            f"{m['name']}: {m['state']}{(' ' + m['unit']).rstrip()}"
                            for m in matches),
                        data={"matches": matches, "via": "search_fallback"},
                    )
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Zu „{what}“ habe ich nichts gefunden.")

            # Reads are permissive: several matches are informative, not dangerous.
            values = await self.read_values([n.id for n in pool[:4]])
            body = " · ".join(
                f"{v['name']}: {v['state']}{(' ' + v['unit']).rstrip()}" for v in values
            )
            more = f" (+{len(pool) - 4} weitere)" if len(pool) > 4 else ""
            return tool_result(
                ok=True, source=self.slug,
                summary=(f"{area}: " if area else "") + body + more,
                data={"area": area, "values": values, "total": len(pool)},
            )

        def _service_for(kind: str, action: str, value) -> tuple[str, str, dict]:
            """Map (node kind, free-text action, optional value) → HA service call."""
            if action in _ACTION_TOGGLE:
                return kind, "toggle", {}
            if value is not None and (action in _ACTION_SET or action in _ACTION_ON):
                try:
                    num = float(str(value).replace(",", "."))
                except (TypeError, ValueError):
                    num = None
                if num is not None:
                    if kind == "climate":
                        return "climate", "set_temperature", {"temperature": num}
                    if kind == "light":
                        return "light", "turn_on", {"brightness_pct": int(max(1, min(100, num)))}
                    if kind == "cover":
                        return "cover", "set_cover_position", {"position": int(max(0, min(100, num)))}
                    if kind == "media_player":
                        level = num / 100 if num > 1 else num
                        return "media_player", "volume_set", {"volume_level": max(0.0, min(1.0, level))}
            if action in _ACTION_ON:
                if kind == "cover":
                    return "cover", "open_cover", {}
                if kind == "lock":
                    return "lock", "unlock", {}
                if kind == "scene":
                    return "scene", "turn_on", {}
                return kind, "turn_on", {}
            if action in _ACTION_OFF:
                if kind == "cover":
                    return "cover", "close_cover", {}
                if kind == "lock":
                    return "lock", "lock", {}
                return kind, "turn_off", {}
            return "", "", {}

        async def _home_control(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug,
                                   summary="Home Assistant ist nicht konfiguriert.")
            action = world.fold(str(args.get("action") or ""))
            target = str(args.get("target") or "").strip()
            where = str(args.get("where") or "").strip()
            value = args.get("value")
            if not action:
                return tool_result(ok=False, source=self.slug,
                                   summary="'action' fehlt — z. B. an, aus, toggle oder setzen.")
            nodes = await world.snapshot()
            area = ""
            if where:
                res = world.resolve_in(nodes, where, kinds=("area",))
                if (err := _area_error(where, res, nodes)):
                    return err
                area = _area_of(res)

            kinds, device_class = world.intent_filter(target)
            pool = [n for n in world.filter_nodes(nodes, kinds=kinds,
                                                  device_class=device_class, area=area)
                    if n.kind != "area"]
            if controllable := [n for n in pool if n.kind in _CONTROL_CAPS]:
                pool = controllable
            if not pool:
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Nichts Schaltbares zu „{target}“"
                            + (f" in {area}" if area else "") + " gefunden.",
                    data={"area": area},
                )

            # One kind → the user means all of them ("mach das Licht unten an"
            # with three lamps is not a question). Several kinds → ask.
            by_kind = {n.kind for n in pool}
            if len(by_kind) > 1:
                res = world.resolve_in(pool, target) if target else world.Resolution("ambiguous")
                if res.status != "unique":
                    shown = res.candidates or tuple(pool[:8])
                    return tool_result(
                        ok=False, source=self.slug,
                        summary=f"Was genau meinst du? {_names(shown)}",
                        data={"ambiguous": True, "candidates": [n.id for n in shown]},
                    )
                pool = [res.node]

            targets = pool[:12]
            domain, service, data = _service_for(targets[0].kind, action, value)
            if not service:
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Die Aktion „{args.get('action')}“ kenne ich für "
                            f"{world.thing_label(targets[0])} nicht.",
                )
            ok = await self.call_service(
                domain, service, {"entity_id": [n.id for n in targets], **data}
            )
            names = ", ".join(n.label for n in targets)
            return tool_result(
                ok=ok, source=self.slug,
                summary=(f"{names} → {domain}.{service} ausgeführt."
                         if ok else f"{domain}.{service} für {names} fehlgeschlagen."),
                data={"entities": [n.id for n in targets], "service": f"{domain}.{service}",
                      "area": area, **data},
            )

        async def _speak_tool(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug,
                                   summary="Home Assistant ist nicht konfiguriert.")
            if not self.get("tts_engine"):
                return tool_result(ok=False, source=self.slug,
                                   summary="Keine TTS-Engine konfiguriert (Feld 'tts_engine' "
                                           "im Home-Assistant-Plugin, z. B. tts.google_de).")
            text = str(args.get("text") or "").strip()
            where = str(args.get("where") or "").strip()
            if not text:
                return tool_result(ok=False, source=self.slug, summary="Kein Text zum Sprechen.")
            nodes = await world.snapshot()
            res = world.resolve_in(nodes, where, kinds=("media_player",)) if where else None
            if where and (not res or not res.ok):
                cands = res.candidates if res else ()
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Welcher Lautsprecher? {_names(cands)}" if cands
                            else f"Einen Lautsprecher „{where}“ kenne ich nicht.",
                    data={"candidates": [n.id for n in cands]},
                )
            speaker = res.node.id if res and res.ok else ""
            if not speaker:
                players = world.filter_nodes(nodes, kinds=("media_player",))
                if len(players) == 1:
                    speaker = players[0].id
                else:
                    return tool_result(ok=False, source=self.slug,
                                       summary="Auf welchem Lautsprecher? Bitte Raum angeben.")
            ok = await self.speak(text, media_player=speaker)
            return tool_result(ok=ok, source=self.slug,
                               summary=(f"Gesprochen auf {speaker}." if ok
                                        else "Ansage fehlgeschlagen."),
                               data={"media_player": speaker})

        async def _list_areas(args: dict, ctx: ToolContext) -> str:
            nodes = await world.snapshot()
            names = world.areas(nodes)
            if not names:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Ich kenne keine Räume — in Home Assistant sind vermutlich "
                            "keine Bereiche vergeben.",
                )
            lines = []
            for name in names:
                things = [n for n in nodes if n.area == name and n.kind != "area"]
                labels = sorted({world.thing_label(n) for n in things})
                lines.append(f"{name}: {', '.join(labels) or 'keine Geräte zugeordnet'}")
            return tool_result(ok=True, source=self.slug,
                               summary="Räume:\n" + "\n".join(lines),
                               data={"areas": names})

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
            Tool(name="home_state",
                 description=(
                     "ERSTE WAHL für Zustandsfragen im Zuhause („wie warm ist es im "
                     "Wohnzimmer?“). Gib Absicht und Ort GETRENNT an: what='Temperatur', "
                     "where='Wohnzimmer' — niemals beides in ein Feld. Tippfehler, Umlaute, "
                     "verschluckte Endungen und Spitznamen werden toleriert. Passen mehrere "
                     "Räume, kommt ok=false mit Kandidaten zurück: stell dann EINE kurze "
                     "Rückfrage, statt zu raten."
                 ),
                 parameters={"type": "object", "properties": {
                     "what": {"type": "string",
                              "description": "Was interessiert: Temperatur, Luftfeuchte, Licht, "
                                             "Fenster, Batterie, Strom, Musik …"},
                     "where": {"type": "string",
                               "description": "Raum in Bahrians Worten, z. B. 'Wohnzimmer', "
                                              "'mein Zimmer', 'unten'. Leer = überall."}},
                     "required": ["what"]},
                 handler=_home_state, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status"],
                 examples=["wie warm ist es im Wohnzimmer", "ist im Bad ein Fenster offen"]),
            Tool(name="home_control",
                 description=(
                     "ERSTE WAHL zum Schalten im Zuhause. action='an'|'aus'|'toggle'|'setzen', "
                     "target='Licht'|'Heizung'|'Rollo'…, where=Raum in Bahrians Worten, "
                     "value=Zahl für 'setzen' (Grad bei Heizung, Prozent bei Licht/Rollo). "
                     "Mehrere Geräte derselben Art in einem Raum werden gemeinsam geschaltet; "
                     "ist das Ziel unklar, kommt ok=false mit Kandidaten — dann nachfragen."
                 ),
                 parameters={"type": "object", "properties": {
                     "action": {"type": "string", "description": "an | aus | toggle | setzen"},
                     "target": {"type": "string", "description": "Licht, Heizung, Rollo, Steckdose …"},
                     "where": {"type": "string", "description": "Raum in Bahrians Worten"},
                     "value": {"type": "number", "description": "Zielwert für 'setzen'"}},
                     "required": ["action", "target"]},
                 handler=_home_control, owner_only=True, source=self.slug,
                 safety="mutation", intents=["control"],
                 examples=["mach das Licht unten an", "stell die Heizung im Bad auf 22"]),
            Tool(name="list_areas",
                 description="Liste alle Räume, die ASTRA kennt, mit den Gerätearten darin. "
                             "Nutze das, wenn ein Raumname nicht gefunden wurde.",
                 parameters={"type": "object", "properties": {}},
                 handler=_list_areas, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status", "list"]),
            Tool(name="speak",
                 description="Lass ASTRA etwas laut auf einem Lautsprecher sagen. text=Ansage, "
                             "where=Raum in Bahrians Worten (leer, wenn es nur einen gibt). "
                             "Braucht eine konfigurierte TTS-Engine.",
                 parameters={"type": "object", "properties": {
                     "text": {"type": "string"},
                     "where": {"type": "string", "description": "Raum/Lautsprecher, optional"}},
                     "required": ["text"]},
                 handler=_speak_tool, owner_only=True, source=self.slug,
                 safety="mutation", intents=["control"]),
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
