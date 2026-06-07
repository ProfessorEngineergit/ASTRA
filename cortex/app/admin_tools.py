"""Self-administration tools — give ASTRA full control over its own setup.

Registered as owner-only CORE tools (survive plugin rebuilds, never exposed to
third parties). With these, ASTRA can — from the web/Telegram chat — list and
configure its own integrations, flip settings, switch its model, set its
autonomy level and read its container health. The owner is in the chat, so the
owner is the authoriser; a master switch (allow_self_config) can disable writes.
"""
from __future__ import annotations

import json
import logging

from . import db, sysinfo
from .config_store import SECRET_SENTINEL, get_config_store
from .models import set_model_override
from .plugins.base import CATEGORY_LABELS
from .plugins.registry import get_manager
from .tools import REGISTRY, Tool, ToolContext, capability_manifest, dispatch, register

log = logging.getLogger("astra.admin_tools")

AUTONOMY_LEVELS = {
    "ask": "Fragt vor heiklen Aktionen nach (Standard).",
    "confident": "Wartet nicht ab, fragt aber bei Heiklem weiter nach.",
    "full": "Handelt eigenständig und überspringt Rückfragen/Freigaben.",
}


async def _settings() -> dict:
    return await db.get_setting("app_settings", {}) or {}


async def _writes_allowed(ctx: ToolContext | None = None) -> bool:
    if ctx and ctx.is_owner and ctx.channel == "web" and ctx.permission_mode == "bypass":
        return True
    s = await _settings()
    return s.get("allow_self_config", True)


# ─── Integration management ────────────────────────────────────────────────────
async def _list_integrations(args: dict, ctx: ToolContext) -> str:
    mgr = get_manager()
    q = (args.get("query") or "").lower()
    only = args.get("only_enabled", False)
    rows = []
    for p in mgr.all():
        if only and not p.enabled:
            continue
        if q and q not in (p.name + " " + p.slug + " " + p.description).lower():
            continue
        state = ("aktiv" if p.enabled else "bereit" if p.has_required
                 else "nicht konfiguriert")
        rows.append(f"• {p.slug} — {p.name} [{CATEGORY_LABELS.get(p.category, '')}] → {state}")
    if not rows:
        return "Keine passenden Integrationen."
    return f"{len(rows)} Integrationen:\n" + "\n".join(rows[:80])


async def _integration_details(args: dict, ctx: ToolContext) -> str:
    mgr = get_manager()
    slug = args.get("slug", "")
    cls, inst = mgr.plugin_class(slug), mgr.get(slug)
    if not cls or not inst:
        return f"Unbekannte Integration '{slug}'. Nutze astra_list_integrations."
    meta = await get_config_store().stored_meta(cls)
    lines = [f"{cls.name} ({slug}) — {'aktiv' if inst.enabled else 'inaktiv'}"]
    for f in cls.config_fields:
        req = " (Pflicht)" if f.required else ""
        if f.secret:
            val = "•••• gesetzt" if meta.get(f.key) else "nicht gesetzt"
        else:
            val = inst.get(f.key, f.default)
        lines.append(f"  - {f.key}{req}: {val}")
    try:
        hs = await inst.health_check()
        lines.append(f"Status: {hs.state.value} — {hs.message}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"Status: Fehler ({e})")
    return "\n".join(lines)


async def _configure_integration(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Selbst-Konfiguration ist deaktiviert (Einstellungen → allow_self_config)."
    mgr = get_manager()
    slug = args.get("slug", "")
    cls = mgr.plugin_class(slug)
    if not cls:
        return f"Unbekannte Integration '{slug}'."
    store = get_config_store()
    cur = await store.load(cls)
    changes = args.get("config") or {}
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except json.JSONDecodeError:
            changes = {}
    values: dict = {}
    for f in cls.config_fields:
        if f.key in changes and changes[f.key] not in (None, ""):
            values[f.key] = f.coerce(changes[f.key])
        elif f.secret:
            values[f.key] = SECRET_SENTINEL          # keep existing secret
        else:
            values[f.key] = cur.get(f.key, f.default)
    enable = args.get("enable")
    enabled = bool(enable) if enable is not None else bool(cur.get("__enabled"))
    await store.save(cls, values, enabled)
    await mgr.rebuild()
    await db.audit("self_config", actor="astra", detail={"slug": slug, "enabled": enabled})
    inst = mgr.get(slug)
    note = ""
    try:
        hs = await inst.health_check()
        note = f" Status: {hs.state.value} — {hs.message}"
    except Exception:  # noqa: BLE001
        pass
    return f"'{cls.name}' aktualisiert (aktiv={enabled}).{note}"


async def _test_integration(args: dict, ctx: ToolContext) -> str:
    inst = get_manager().get(args.get("slug", ""))
    if not inst:
        return "Unbekannte Integration."
    try:
        hs = await inst.health_check()
        return f"{hs.state.value} — {hs.message}"
    except Exception as e:  # noqa: BLE001
        return f"Fehler: {e}"


async def _list_capabilities(args: dict, ctx: ToolContext) -> str:
    caps = capability_manifest(is_owner=ctx.is_owner)
    q = (args.get("query") or "").lower()
    if q:
        caps = [
            c for c in caps
            if q in (c["tool"] + " " + c["source"] + " " + c["description"]).lower()
        ]
    if not caps:
        return "Keine passenden Agentenfähigkeiten."
    rows = [
        f"• {c['tool']} [{c['source']}/{c['safety']}] intents={','.join(c['intents']) or 'generic'}"
        for c in caps[:80]
    ]
    return f"{len(caps)} Agentenfähigkeiten:\n" + "\n".join(rows)


async def _explain_capability(args: dict, ctx: ToolContext) -> str:
    slug = (args.get("slug") or args.get("tool") or "").strip()
    caps = [
        c for c in capability_manifest(is_owner=ctx.is_owner)
        if c["source"] == slug or c["tool"] == slug
    ]
    if not caps:
        return f"Keine Fähigkeit zu '{slug}' gefunden."
    return json.dumps(caps[:20], ensure_ascii=False, indent=2)


async def _test_capability(args: dict, ctx: ToolContext) -> str:
    tool_name = (args.get("tool") or "").strip()
    if tool_name not in REGISTRY:
        return f"Unbekanntes Tool: {tool_name}"
    test_args = args.get("args") or {}
    if not isinstance(test_args, dict):
        test_args = {}
    test_ctx = ToolContext(
        thread_id=ctx.thread_id,
        channel=ctx.channel,
        contact=ctx.contact,
        max_sensitivity=ctx.max_sensitivity,
        is_owner=ctx.is_owner,
        permission_mode="bypass" if ctx.permission_mode == "bypass" else "auto",
    )
    return await dispatch(tool_name, test_args, test_ctx)


# ─── Settings / autonomy / system ──────────────────────────────────────────────
async def _get_settings(args: dict, ctx: ToolContext) -> str:
    s = await _settings()
    loc = s.get("location", {}) or {}
    return (
        f"Modell: {s.get('ai_model') or '(Standard aus .env)'}\n"
        f"Autonomie: {s.get('autonomy', 'ask')}\n"
        f"Sparmodus: {bool(s.get('economy_mode'))}\n"
        f"Selbst-Konfig erlaubt: {s.get('allow_self_config', True)}\n"
        f"Schriftart: {s.get('font', 'inter')}\n"
        f"Name: {s.get('owner_name', 'Bahrian')} · TZ: {s.get('timezone', 'Europe/Berlin')}\n"
        f"Standort: {loc.get('city', '—')} ({loc.get('lat', '?')},{loc.get('lon', '?')})"
    )


async def _update_settings(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Änderungen an Einstellungen sind deaktiviert (allow_self_config)."
    from .brain import set_autonomy
    from .web.templates import set_font
    s = await _settings()
    changed = []
    if "model" in args and args["model"] is not None:
        s["ai_model"] = str(args["model"]).strip()
        set_model_override(s["ai_model"])
        changed.append(f"Modell={s['ai_model'] or 'Standard'}")
    if "autonomy" in args and args["autonomy"] in AUTONOMY_LEVELS:
        s["autonomy"] = args["autonomy"]
        set_autonomy(s["autonomy"])
        changed.append(f"Autonomie={s['autonomy']}")
    if "economy" in args and args["economy"] is not None:
        s["economy_mode"] = bool(args["economy"])
        changed.append(f"Sparmodus={s['economy_mode']}")
    if "allow_self_config" in args and args["allow_self_config"] is not None:
        s["allow_self_config"] = bool(args["allow_self_config"])
        changed.append(f"Selbst-Konfig={s['allow_self_config']}")
    if "font" in args and args["font"]:
        s["font"] = str(args["font"])
        set_font(s["font"])
        changed.append(f"Font={s['font']}")
    if not changed:
        return "Nichts geändert. Felder: model, autonomy(ask|confident|full), economy, font, allow_self_config."
    await db.set_setting("app_settings", s)
    await db.audit("self_settings", actor="astra", detail={"changed": changed})
    return "Aktualisiert: " + ", ".join(changed)


async def _system_status(args: dict, ctx: ToolContext) -> str:
    snap = sysinfo.snapshot()
    m, d, c = snap["mem"], snap["disk"], snap["cpu"]
    def pc(v):
        return f"{v:.0f}%" if v is not None else "n/a"
    recs = " | ".join(r["text"] for r in snap["recommendations"])
    return (f"RAM {pc(m['pct'])} ({m['used_h']}/{m['total_h']}) · "
            f"Disk {pc(d['pct'])} ({d['used_h']}/{d['total_h']}) · "
            f"CPU {pc(c['load_pct'])} ({c['count']} Kerne) · Uptime {snap['uptime_h']}\n{recs}")


def register_admin_tools() -> None:
    """Register all self-admin tools as owner-only core tools (idempotent)."""
    defs = [
        ("astra_list_integrations", "Liste alle Integrationen/Plugins mit Status (slug, Name, aktiv?).",
         {"type": "object", "properties": {
             "query": {"type": "string", "description": "Filtertext, optional"},
             "only_enabled": {"type": "boolean"}}}, _list_integrations),
        ("astra_integration_details", "Zeige Konfigfelder + Status einer Integration (Secrets maskiert).",
         {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
         _integration_details),
        ("astra_configure_integration",
         "Konfiguriere & (de)aktiviere eine eigene Integration. 'config' = Objekt aus Feld→Wert.",
         {"type": "object", "properties": {
             "slug": {"type": "string"},
             "enable": {"type": "boolean"},
             "config": {"type": "object", "description": "Feld→Wert, z. B. {\"api_key\":\"…\"}"}},
          "required": ["slug"]}, _configure_integration),
        ("astra_test_integration", "Teste die Verbindung einer Integration (health_check).",
         {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
         _test_integration),
        ("astra_get_settings", "Zeige ASTRAs aktuelle Einstellungen (Modell, Autonomie, Standort …).",
         {"type": "object", "properties": {}}, _get_settings),
        ("astra_update_settings",
         "Ändere ASTRAs Einstellungen: model, autonomy(ask|confident|full), economy, font, allow_self_config.",
         {"type": "object", "properties": {
             "model": {"type": "string"}, "autonomy": {"type": "string"},
             "economy": {"type": "boolean"}, "font": {"type": "string"},
             "allow_self_config": {"type": "boolean"}}}, _update_settings),
        ("astra_system_status", "Container-Leistung (RAM/CPU/Disk/Uptime) + Empfehlungen.",
         {"type": "object", "properties": {}}, _system_status),
        ("astra_list_capabilities", "Liste verfügbare Agentenfähigkeiten mit Safety/Intent.",
         {"type": "object", "properties": {"query": {"type": "string"}}}, _list_capabilities),
        ("astra_explain_capability", "Erkläre eine Integration oder ein Tool aus dem Capability-Manifest.",
         {"type": "object", "properties": {
             "slug": {"type": "string"},
             "tool": {"type": "string"}}}, _explain_capability),
        ("astra_test_capability", "Teste ein konkretes Agenten-Tool mit optionalen Argumenten.",
         {"type": "object", "properties": {
             "tool": {"type": "string"},
             "args": {"type": "object"}}}, _test_capability),
    ]
    for name, desc, params, handler in defs:
        safety = "mutation" if name in {
            "astra_configure_integration", "astra_update_settings", "astra_test_capability"
        } else "private_read"
        intents = ["status", "list"] if "list" in name or "status" in name else ["control"] if safety == "mutation" else ["status"]
        register(Tool(name=name, description=desc, parameters=params, handler=handler,
                      owner_only=True, source="core", safety=safety, intents=intents))
    log.info("Registered %d self-admin tools.", len(defs))
