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
import re
import uuid as _uuid
from pathlib import Path

from . import db, knowledge, sysinfo, world
from .channels import get_channels
from .config import get_settings
from .config_store import SECRET_SENTINEL, get_config_store
from .models import set_model_override
from .plugins.base import CATEGORY_LABELS
from .plugins.registry import get_manager
from .plugins import extended_catalog
from .tools import (
    REGISTRY, Tool, ToolContext, capability_manifest, dispatch, register, tool_result,
)

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


async def _integration_installations(args: dict, ctx: ToolContext) -> str:
    mgr = get_manager()
    slug = args.get("slug", "")
    cls = mgr.plugin_class(slug)
    if not cls:
        return f"Unbekannte Integration '{slug}'."
    rows = []
    for p in mgr.installations(slug):
        state = "aktiv" if p.enabled else "aus" if p.has_required else "unvollständig"
        rows.append(f"• {p.installation_id} — {p.installation_name} [{state}] → {p.runtime_slug}")
    return "\n".join(rows) if rows else "Keine Installationen."


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
    slug = args.get("runtime_slug") or args.get("slug", "")
    inst = get_manager().get(slug)
    if not inst:
        return "Unbekannte Integration."
    try:
        hs = await inst.health_check()
        return f"{hs.state.value} — {hs.message}"
    except Exception as e:  # noqa: BLE001
        return f"Fehler: {e}"


async def _list_plugin_catalog(args: dict, ctx: ToolContext) -> str:
    q = (args.get("query") or "").lower()
    native = []
    for p in get_manager().all():
        if q and q not in (p.slug + " " + p.name + " " + p.description).lower():
            continue
        native.append(f"• nativ:{p.slug} — {p.name} [{CATEGORY_LABELS.get(p.category, '')}]")
    catalog = []
    for e in extended_catalog.all_entries():
        if q and q not in (e.slug + " " + e.name + " " + e.description).lower():
            continue
        catalog.append(f"• katalog:{e.slug} — {e.name} [{CATEGORY_LABELS.get(e.category, '')}]")
    rows = (native + catalog)[:100]
    return f"{len(rows)} passende Plugin-Einträge:\n" + "\n".join(rows) if rows else "Keine passenden Plugin-Einträge."


async def _create_plugin_module(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Selbst-Konfiguration ist deaktiviert (Einstellungen → allow_self_config)."
    slug = str(args.get("slug") or "").strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", slug):
        return "Ungültiger slug. Erlaubt: a-z, 0-9, _, beginnt mit Buchstabe."
    name = str(args.get("name") or slug.replace("_", " ").title()).strip()
    description = str(args.get("description") or "Neue ASTRA-Integration.").strip()
    category_raw = str(args.get("category") or "productivity").strip()
    category = next((c for c in CATEGORY_LABELS if c.value == category_raw), None)
    if category is None:
        return "Ungültige Kategorie. Nutze: " + ", ".join(c.value for c in CATEGORY_LABELS)
    root = Path(__file__).resolve().parent / "plugins" / "builtin"
    target = root / f"{slug}.py"
    if target.exists():
        return f"Plugin-Datei existiert bereits: {target}"
    class_name = "".join(part.capitalize() for part in slug.split("_")) + "Plugin"
    target.write_text(
        f'''""" {name} integration module. """\nfrom __future__ import annotations\n\nfrom ...tools import Tool, ToolContext, tool_result\nfrom ..base import ConfigField, HealthStatus, Plugin, PluginCategory\n\n\nclass {class_name}(Plugin):\n    slug = "{slug}"\n    name = "{name}"\n    description = "{description}"\n    category = PluginCategory.{category.name}\n    icon = "🔌"\n    config_fields = [\n        ConfigField("base_url", "Basis-URL", required=False),\n    ]\n\n    async def health_check(self) -> HealthStatus:\n        if not self.is_toggled_on:\n            return HealthStatus.disabled()\n        return HealthStatus.ok("Grundmodul installiert; Detailfunktionen sind noch nicht hinterlegt.")\n\n    def tools(self) -> list[Tool]:\n        async def _status(args: dict, ctx: ToolContext) -> str:\n            return tool_result(ok=True, summary="{name} ist als Grundmodul verfuegbar.", source=self.slug)\n\n        return [Tool(\n            name="{slug}_status",\n            description="Status der {name}-Integration.",\n            parameters={{"type": "object", "properties": {{}}}},\n            handler=_status,\n            owner_only=True,\n            source=self.slug,\n            safety="private_read",\n            intents=["status"],\n        )]\n''',
        encoding="utf-8",
    )
    await db.audit("plugin_module_created", actor="astra", detail={"slug": slug, "path": str(target)})
    return f"Plugin-Modul angelegt: {target}. Danach Tests laufen lassen und PluginManager neu laden."


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
        f"Secretary: {bool((s.get('secretary') or {}).get('enabled', True))}\n"
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
    if "secretary_enabled" in args and args["secretary_enabled"] is not None:
        secretary = s.get("secretary") if isinstance(s.get("secretary"), dict) else {}
        secretary["enabled"] = bool(args["secretary_enabled"])
        s["secretary"] = secretary
        changed.append(f"Secretary={secretary['enabled']}")
    if "font" in args and args["font"]:
        s["font"] = str(args["font"])
        set_font(s["font"])
        changed.append(f"Font={s['font']}")
    if not changed:
        return ("Nichts geändert. Felder: model, autonomy(ask|confident|full), economy, "
                "font, allow_self_config, secretary_enabled.")
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


# ─── Brain files: read & write ASTRA's memory about people ─────────────────────
async def _brain_list(args: dict, ctx: ToolContext) -> str:
    q = (args.get("query") or "").lower()
    files = knowledge.list_files()
    rows = []
    for e in files:
        if q and q not in (e["title"] + " " + e["rel"] + " " + e["preview"]).lower():
            continue
        rows.append(f"• {e['rel']} — {e['title']} [{e['tag']}] ({e['lines']} Z., {e['mtime']})")
    if not rows:
        return "Keine Brain-Dateien gefunden."
    return f"{len(rows)} Brain-Dateien:\n" + "\n".join(rows)


async def _brain_read(args: dict, ctx: ToolContext) -> str:
    rel = args.get("file", "")
    text = knowledge.read_file(rel)
    if not text:
        return f"'{rel}' ist leer oder existiert nicht. Nutze astra_brain_list."
    return f"# {rel}\n{text}"


async def _person_lookup(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("name") or "").strip()
    profiles = knowledge.person_profiles_for_name(query)
    if not profiles:
        return tool_result(ok=False, source="brain",
                           summary=f"Keine Personen-Datei für '{query}' gefunden.")
    rows = []
    for profile in profiles[:8]:
        handles = profile.get("handles") or {}
        parts = []
        for key, label in (("phone", "Telefon"), ("waha", "WhatsApp"),
                           ("signal", "Signal"), ("telegram", "Telegram"),
                           ("email", "E-Mail")):
            values = handles.get(key) or []
            if values:
                parts.append(f"{label}: {', '.join(values)}")
        rows.append(f"{profile['name']}: " + (" · ".join(parts) if parts else
                                                "keine Kontaktdaten hinterlegt"))
    return tool_result(ok=True, source="brain", summary="\n".join(rows),
                       data={"profiles": profiles[:8]})


async def _brain_write(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Schreiben in Brain-Dateien ist deaktiviert (allow_self_config)."
    rel, content = args.get("file", ""), args.get("content")
    if content is None:
        return "Kein 'content' übergeben."
    if knowledge.write_file(rel, str(content)):
        await db.audit("brain_write", actor="astra", detail={"file": rel})
        return f"'{rel}' gespeichert ({len(str(content))} Zeichen)."
    return f"Konnte '{rel}' nicht schreiben (ungültiger Pfad?)."


async def _brain_append(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Schreiben in Brain-Dateien ist deaktiviert (allow_self_config)."
    rel, text = args.get("file", ""), (args.get("text") or "").strip()
    if not text:
        return "Kein 'text' übergeben."
    cur = knowledge.read_file(rel)
    new = (cur.rstrip() + f"\n- {text}\n") if cur else f"- {text}\n"
    if knowledge.write_file(rel, new):
        await db.audit("brain_append", actor="astra", detail={"file": rel})
        return f"An '{rel}' angehängt."
    return f"Konnte '{rel}' nicht ergänzen."


async def _brain_add_person(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Anlegen ist deaktiviert (allow_self_config)."
    rel = knowledge.create_person(args.get("name", ""))
    if not rel:
        return "Ungültiger Name."
    await db.audit("brain_add_person", actor="astra", detail={"file": rel})
    return f"Personen-Datei angelegt: {rel} — jetzt mit astra_brain_write füllen."


async def _world_alias(args: dict, ctx: ToolContext) -> str:
    """List/add/remove a nickname for a room or device (feeds the world resolver).

    Learned aliases live in the compact facts table (kind=alias, subject=spoken word,
    value=real target); the markdown world_aliases.md stays readable as a manual override."""
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        by_target: dict[str, list[str]] = {t: list(v) for t, v in knowledge.world_aliases().items()}
        for target, spoken in (await knowledge.world_aliases_db()).items():
            by_target.setdefault(target, []).extend(spoken)
        if not by_target:
            return "Keine Aliasse gesetzt."
        return "Aliasse (echter Name ← deine Wörter):\n" + "\n".join(
            f"• {target}: {', '.join(dict.fromkeys(vals))}" for target, vals in sorted(by_target.items())
        )
    if not await _writes_allowed(ctx):
        return "Schreiben ist deaktiviert (allow_self_config)."
    target = str(args.get("target") or "").strip()
    alias = str(args.get("alias") or "").strip()
    if not alias:
        return "Bitte 'alias' (Bahrians Wort) angeben."
    principal = getattr(ctx, "principal", "") or ""
    if action == "remove":
        n = await db.delete_fact("alias", alias, principal_key=principal)
        world.invalidate()
        return (f"Alias '{alias}' entfernt." if n else
                f"'{alias}' war nicht als Alias eingetragen.")
    if not target:
        return "Bitte 'target' (echter Raum-/Geräte-Name) angeben."
    await db.add_fact("alias", alias, target, principal_key=principal)
    world.invalidate()
    await db.audit("world_alias", actor="astra",
                   detail={"action": "add", "target": target, "alias": alias})
    return f"Gemerkt: '{alias}' bedeutet '{target}'."


async def _models_show(args: dict, ctx: ToolContext) -> str:
    from . import models as m
    lines = ["Modell-Rollen (Anbieter/Modell):", m.describe_roles(), "", "Anbieter:"]
    for name, p in sorted(m.providers().items()):
        state = "konfiguriert" if p.configured else "kein Key/URL"
        tools = "Tool-Calling" if p.tools else "nur Text (kein Tool-Calling)"
        lines.append(f"• {name} [{p.kind}] {state} · {tools}"
                     + (f" · {p.base_url}" if p.base_url else ""))
    lines.append(f"\nSparmodus: {'an' if m.get_economy() else 'aus'}")
    return "\n".join(lines)


async def _models_set(args: dict, ctx: ToolContext) -> str:
    """Assign a provider+model to one role (small|medium|heavy|code|osint)."""
    if not await _writes_allowed(ctx):
        return "Änderungen sind deaktiviert (allow_self_config)."
    from . import models as m
    role = str(args.get("role") or "").strip().lower()
    if role not in m.ROLES:
        return f"Unbekannte Rolle. Erlaubt: {', '.join(m.ROLES)}."
    provider = str(args.get("provider") or "").strip().lower()
    model = str(args.get("model") or "").strip()
    if not provider or not model:
        return "Bitte 'provider' und 'model' angeben (astra_models_show zeigt die Anbieter)."
    known = m.providers()
    if provider not in known:
        return f"Unbekannter Anbieter '{provider}'. Bekannt: {', '.join(sorted(known))}."
    if role == "medium" and not known[provider].tools:
        return (f"'{provider}' kann in diesem Loop kein Tool-Calling — für die Rolle 'medium' "
                "braucht es einen OpenAI-kompatiblen Anbieter (openai, openrouter, ollama).")
    appset = await _settings()
    cfg = appset.get("models") or {}
    roles = dict(cfg.get("roles") or {})
    roles[role] = {"provider": provider, "model": model}
    cfg["roles"] = roles
    appset["models"] = cfg
    await db.set_setting("app_settings", appset)
    m.set_model_config(cfg)
    await db.audit("model_role_set", actor="astra",
                   detail={"role": role, "provider": provider, "model": model})
    return f"Rolle '{role}' läuft jetzt auf {provider}/{model}."


async def _models_add_provider(args: dict, ctx: ToolContext) -> str:
    """Register any OpenAI-compatible endpoint (OpenRouter, Groq, vLLM, LM Studio …)."""
    if not await _writes_allowed(ctx):
        return "Änderungen sind deaktiviert (allow_self_config)."
    from . import models as m
    name = str(args.get("name") or "").strip().lower()
    if not name:
        return "Bitte 'name' angeben."
    appset = await _settings()
    cfg = appset.get("models") or {}
    provs = dict(cfg.get("providers") or {})
    entry = dict(provs.get(name) or {})
    entry["kind"] = str(args.get("kind") or entry.get("kind") or "openai_compat")
    if args.get("base_url"):
        entry["base_url"] = str(args["base_url"]).strip()
    if args.get("api_key"):
        entry["api_key"] = m.protect_api_key(str(args["api_key"]).strip())
    entry["tools"] = bool(args.get("tools", entry.get("tools", entry["kind"] == "openai_compat")))
    provs[name] = entry
    cfg["providers"] = provs
    appset["models"] = cfg
    await db.set_setting("app_settings", appset)
    m.set_model_config(cfg)
    await db.audit("model_provider_set", actor="astra", detail={"provider": name})
    return f"Anbieter '{name}' gespeichert ({entry['kind']})."


async def _model_run(args: dict, ctx: ToolContext) -> str:
    """Run one explicit subtask through a selected configured role."""
    from . import models as m
    role = str(args.get("role") or m.HEAVY).strip().lower()
    if role not in m.ROLES:
        return f"Unbekannte Rolle. Erlaubt: {', '.join(m.ROLES)}."
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "Bitte 'prompt' angeben."
    system = str(args.get("system") or "").strip() or (
        "Du bist ein spezialisiertes Teilmodell innerhalb von ASTRA. Bearbeite den "
        "Teilauftrag präzise. Behaupte keine ausgeführten Aktionen und gib keine "
        "Geheimnisse aus."
    )
    try:
        answer = await m.get_gateway().complete(
            role, system, prompt,
            max_tokens=max(256, min(8000, int(args.get("max_tokens") or 2500))),
        )
    except Exception as exc:  # noqa: BLE001
        return f"Modell-Aufruf für Rolle '{role}' fehlgeschlagen: {exc}"
    provider, model = m.role_target(role)
    return f"[{role} · {provider.name}/{model}]\n{answer}"


async def _delegate_job(args: dict, ctx: ToolContext) -> str:
    """Hand a task to the big brain with an explicit permission envelope."""
    if not await _writes_allowed(ctx):
        return "Delegation ist deaktiviert (allow_self_config)."
    from . import jobs as jobs_mod
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return "Bitte 'goal' angeben — was soll erreicht werden?"
    hosts = args.get("hosts") or []
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    envelope = {"hosts": hosts, "write": bool(args.get("write")),
                "budget_steps": int(args.get("budget_steps") or 10)}
    job_id = await db.add_job(goal=goal, kind=str(args.get("kind") or "ops"),
                              envelope=envelope, principal_key=getattr(ctx, "principal", "") or "",
                              created_by="owner" if ctx.is_owner else "astra")
    plan = await jobs_mod.run_job(job_id)
    return (f"Job #{job_id} geplant (Umschlag: Hosts={hosts or '—'}, "
            f"Schreiben={'ja' if envelope['write'] else 'nein'}).\n\n{plan}")


async def _jobs_list(args: dict, ctx: ToolContext) -> str:
    rows = await db.list_jobs(principal_key=getattr(ctx, "principal", "") or "")
    if not rows:
        return "Keine Jobs."
    return "Jobs:\n" + "\n".join(
        f"#{j['id']} [{j['status']}] {j['goal'][:70]} — {j['result'][:60]}" for j in rows)


async def _job_show(args: dict, ctx: ToolContext) -> str:
    job = await db.get_job(int(args.get("id") or 0))
    if not job:
        return "Job nicht gefunden."
    return (f"Job #{job['id']} [{job['status']}]\nZiel: {job['goal']}\n"
            f"Umschlag: {job['envelope']}\n\n{job['plan'] or '(kein Plan)'}")


async def _secretary_simulate(args: dict, ctx: ToolContext) -> str:
    """Dry-run the secretary decision for a fake inbound — shows the full chain
    (window → plan → shadow) and sends NOTHING. The safe way to test time windows."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .policy import Mode, Sensitivity
    from . import secretary
    channel = str(args.get("channel") or "waha")
    triage = str(args.get("triage") or "defer")
    tz = get_settings().astra_timezone
    when = str(args.get("time") or "").strip()
    try:
        now = datetime.now(ZoneInfo(tz))
        if when:
            hh, mm = when.split(":", 1)
            day = int(args.get("weekday", now.weekday()))
            # nearest date with that weekday, at HH:MM
            now = now.replace(hour=int(hh), minute=int(mm[:2]), second=0, microsecond=0)
            now += __import__("datetime").timedelta(days=(day - now.weekday()) % 7)
    except Exception:  # noqa: BLE001
        now = datetime.now(ZoneInfo(tz))
    appset = await _settings()
    try:
        mode = Mode(triage)
    except ValueError:
        mode = Mode.DEFER
    win = secretary.active_window(appset, now)
    plan = secretary.plan_for(
        channel=channel, mode=mode, max_sensitivity=Sensitivity.FREEBUSY,
        app_settings=appset, timezone=tz, now=now,
        is_group=bool(args.get("is_group")),
    )
    shadow = secretary.shadow_enabled(appset, channel)
    outcome = ("STILL (keine Antwort)" if plan.silent else
               "SCHATTEN → an dich statt Kontakt" if (shadow and plan.mode == Mode.AUTO) else
               {"auto": "AUTO (antwortet selbst)", "defer": "DEFER (wartet auf dich)",
                "ask": "ASK (fragt dich)"}.get(plan.mode.value, plan.mode.value))
    return (f"Simulation {channel} · {now:%a %H:%M} · Triage={mode.value}\n"
            f"Fenster: {win.get('name') if win else '—'} ({win.get('behavior') if win else '—'})\n"
            f"Plan: {plan.mode.value} · Grund: {plan.reason}\n"
            f"Schattenmodus: {'an' if shadow else 'aus'}\n"
            f"→ Ergebnis: {outcome}")


async def _rules_list(args: dict, ctx: ToolContext) -> str:
    principal = getattr(ctx, "principal", "") or ""
    rules = await db.list_rules(principal_key=principal)
    if not rules:
        return "Keine Regeln angelegt."
    rows = []
    for r in rules:
        state = "aktiv" if (r["enabled"] and r["confirmed_at"]) else \
                ("wartet auf Freigabe" if not r["confirmed_at"] else "aus")
        trig = r["trigger"] or {}
        when = f"{trig.get('at','?')}" + (f" -{trig.get('offset_min')}min" if trig.get("offset_min") else "") \
               if trig.get("type") == "schedule" else trig.get("type", "?")
        last = f" · zuletzt: {r['last_result']}" if r["last_result"] else ""
        rows.append(f"#{r['id']} „{r['name']}“ [{state}] ({when}){last}")
    return "Regeln:\n" + "\n".join(rows)


async def _rules_create(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Regeln anlegen ist deaktiviert (allow_self_config)."
    name = str(args.get("name") or "").strip()
    trigger = args.get("trigger") or {}
    actions = args.get("actions") or []
    if isinstance(trigger, str):
        try:
            trigger = json.loads(trigger)
        except json.JSONDecodeError:
            return "trigger ist kein gültiges JSON-Objekt."
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except json.JSONDecodeError:
            return "actions ist kein gültiges JSON-Array."
    if not name or not isinstance(trigger, dict) or not isinstance(actions, list) or not actions:
        return "Bitte name, trigger (Objekt) und actions (nicht-leere Liste) angeben."
    condition = args.get("condition") or {"type": "always"}
    principal = getattr(ctx, "principal", "") or ""
    # Bahrian asking in his own chat IS the confirmation → active immediately.
    # Anything else (a proposal outside the owner's chat) stays pending.
    owner_here = bool(ctx.is_owner)
    rule_id = await db.add_rule(
        name=name, trigger=trigger, condition=condition, actions=actions,
        plugin_slug=str(args.get("plugin_slug") or ""), principal_key=principal,
        created_by=("owner" if owner_here else "astra"), confirmed=owner_here,
    )
    await db.audit("rule_created", actor="astra",
                   detail={"id": rule_id, "name": name, "confirmed": owner_here})
    if owner_here:
        return f"Regel #{rule_id} „{name}“ angelegt und aktiv."
    return f"Regel #{rule_id} „{name}“ angelegt — wartet auf deine Freigabe (astra_rule_confirm)."


async def _rules_confirm(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Deaktiviert (allow_self_config)."
    rid = int(args.get("id") or 0)
    ok = await db.confirm_rule(rid)
    return f"Regel #{rid} freigegeben und aktiv." if ok else f"Regel #{rid} war nicht offen."


async def _rules_delete(args: dict, ctx: ToolContext) -> str:
    if not await _writes_allowed(ctx):
        return "Deaktiviert (allow_self_config)."
    rid = int(args.get("id") or 0)
    n = await db.delete_rule(rid)
    return f"Regel #{rid} gelöscht." if n else f"Regel #{rid} nicht gefunden."


async def _rules_run_now(args: dict, ctx: ToolContext) -> str:
    """Fire a rule immediately (test it), regardless of its schedule."""
    if not await _writes_allowed(ctx):
        return "Deaktiviert (allow_self_config)."
    from . import rules as rules_engine
    rule = await db.get_rule(int(args.get("id") or 0))
    if not rule:
        return "Regel nicht gefunden."
    return f"Regel #{rule['id']} ausgeführt: {await rules_engine.fire_rule(rule)}"


async def _secretary_email_set(args: dict, ctx: ToolContext) -> str:
    """Set/overwrite the Secretary email_accounts list in app_settings."""
    if not await _writes_allowed(ctx):
        return "Schreiben ist deaktiviert (allow_self_config)."
    accounts = args.get("accounts")
    if not isinstance(accounts, list):
        return "Erwarte 'accounts' als Liste von Objekten (from_address, imap_host, …)."
    appset = await _settings()
    secretary = appset.get("secretary") or {}
    # Preserve existing passwords for entries where password is blank/omitted
    prev = secretary.get("email_accounts") or []
    merged = []
    for i, acc in enumerate(accounts):
        prev_acc = prev[i] if i < len(prev) else {}
        merged.append({
            "title": acc.get("title") or f"Konto {i + 1}",
            "enabled": bool(acc.get("enabled", True)),
            "from_address": acc.get("from_address", ""),
            "imap_host": acc.get("imap_host", ""),
            "imap_port": str(acc.get("imap_port") or "993"),
            "imap_user": acc.get("imap_user") or acc.get("from_address", ""),
            "password": acc.get("password") or prev_acc.get("password", ""),
            "imap_mailbox": acc.get("imap_mailbox") or "INBOX",
            "smtp_host": acc.get("smtp_host", ""),
            "smtp_port": str(acc.get("smtp_port") or "587"),
            "poll_minutes": str(acc.get("poll_minutes") or "5"),
            "webhook_url": "/ingress/email",
        })
    secretary["email_accounts"] = merged
    appset["secretary"] = secretary
    await db.set_setting("app_settings", appset)
    await db.audit("secretary_email_set", actor="astra", detail={"count": len(merged)})
    titles = [a.get("title") or a.get("from_address") for a in merged]
    return f"{len(merged)} Mail-Konto/Konten gespeichert: {', '.join(str(t) for t in titles)}"


async def _secretary_contact_rules_get(args: dict, ctx: ToolContext) -> str:
    """List current contact rules + unknown_sender_action setting."""
    appset = await _settings()
    sec = appset.get("secretary") or {}
    rules = sec.get("contact_rules") or []
    action = sec.get("unknown_sender_action") or "policy"
    if not rules:
        return f"Keine Kontaktregeln gesetzt. Unbekannte Sender: {action}."
    rows = [f"Unbekannte Sender: {action}", ""]
    for r in rules:
        rows.append(f"[{r.get('channel','?')}] {r.get('id','?')} → {r.get('rule','?')}"
                    + (f"  ({r.get('note')})" if r.get("note") else ""))
    return "\n".join(rows)


async def _secretary_contact_rules_set(args: dict, ctx: ToolContext) -> str:
    """Add, update or remove contact rules. action=add|remove|set_unknown."""
    if not await _writes_allowed(ctx):
        return "Schreiben ist deaktiviert (allow_self_config)."
    appset = await _settings()
    sec = appset.setdefault("secretary", {})
    rules: list = list(sec.get("contact_rules") or [])

    op = str(args.get("action") or "add")
    if op == "set_unknown":
        val = str(args.get("unknown_sender_action") or "policy")
        if val not in ("policy", "ask_owner", "block"):
            return "Ungültiger Wert. Erlaubt: policy, ask_owner, block."
        sec["unknown_sender_action"] = val
        appset["secretary"] = sec
        await db.set_setting("app_settings", appset)
        return f"Unbekannte Sender: {val}"

    channel = str(args.get("channel") or "waha")
    cid = str(args.get("id") or "").strip()
    if not cid:
        return "Bitte 'id' (Telefonnummer oder JID) angeben."

    if op == "remove":
        before = len(rules)
        rules = [r for r in rules if not (r.get("channel") == channel and r.get("id") == cid)]
        sec["contact_rules"] = rules
        appset["secretary"] = sec
        await db.set_setting("app_settings", appset)
        removed = before - len(rules)
        return f"{removed} Regel(n) entfernt für {channel}:{cid}."

    # add / update
    rule = str(args.get("rule") or "block")
    note = str(args.get("note") or "")
    existing = next((r for r in rules if r.get("channel") == channel and r.get("id") == cid), None)
    if existing:
        existing["rule"] = rule
        existing["note"] = note
    else:
        rules.append({"channel": channel, "id": cid, "rule": rule, "note": note})
    sec["contact_rules"] = rules
    appset["secretary"] = sec
    await db.set_setting("app_settings", appset)
    await db.audit("secretary_contact_rule_set", actor="astra",
                   detail={"channel": channel, "id": cid, "rule": rule})
    return f"Regel gesetzt: [{channel}] {cid} → {rule}" + (f" ({note})" if note else "")


async def _secretary_email_get(args: dict, ctx: ToolContext) -> str:
    """Read back the current email_accounts config (passwords masked)."""
    appset = await _settings()
    accounts = (appset.get("secretary") or {}).get("email_accounts") or []
    if not accounts:
        return "Keine Mail-Konten konfiguriert."
    rows = []
    for i, a in enumerate(accounts):
        pw = "✓ gesetzt" if a.get("password") else "— leer"
        rows.append(
            f"{i+1}. {a.get('title','?')} | {a.get('from_address','')} | "
            f"IMAP {a.get('imap_host','')}:{a.get('imap_port','')} | "
            f"SMTP {a.get('smtp_host','')}:{a.get('smtp_port','')} | "
            f"Passwort: {pw} | aktiv: {a.get('enabled', True)}"
        )
    return "\n".join(rows)


# Channels the transport layer sends natively (self-send + the confirm gate live here).
# Mail/Slack keep their dedicated send_email / slack_send tools.
_SEND_CHANNELS = {"telegram", "waha", "signal"}
_SEND_LABELS = {"telegram": "Telegram", "waha": "WhatsApp", "signal": "Signal"}


async def _person_profiles_save(args: dict, ctx: ToolContext) -> str:
    """Create/update one or many person profiles (people/<slug>.md).

    Built for dumping Apple contacts: each entry becomes a profile with its
    phone numbers (→ usable directly for WhatsApp/Signal), relationship, trust
    tier and per-person tone. Existing notes are preserved (handles/tone merged).
    """
    profiles = args.get("profiles")
    if isinstance(profiles, dict):
        profiles = [profiles]
    if not isinstance(profiles, list) or not profiles:
        # Allow a single flat profile too.
        if args.get("name"):
            profiles = [args]
        else:
            return tool_result(ok=False, source="core",
                               summary="Gib 'profiles' (Liste) oder 'name' an.")
    saved, failed = [], []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            failed.append("(ohne Name)")
            continue
        # A stored 'phone' already matches both WhatsApp and Signal inbound, so a
        # bare mobile number is enough — no need to duplicate it per channel.
        rel = knowledge.upsert_person_profile(name, p)
        (saved if rel else failed).append(name)
    summary = f"{len(saved)} Profil(e) gespeichert" + (f", {len(failed)} fehlgeschlagen" if failed else "")
    return tool_result(ok=bool(saved), source="core", summary=summary,
                       data={"saved": saved, "failed": failed})


async def _is_owner_target(channel: str, to: str) -> bool:
    """True if `to` is Bahrian himself on this channel → no confirmation needed."""
    if channel == "waha" and to == "__self__":
        return True
    s = get_settings()
    norm = to.replace(" ", "").lower()
    if channel == "telegram" and str(to) == str(s.telegram_owner_chat_id):
        return True
    if channel == "signal" and norm == (s.signal_phone_number or "").replace(" ", "").lower():
        return True
    try:
        if await db.is_owner_handle(channel, to):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _send_message(args: dict, ctx: ToolContext) -> str:
    """Send a message, with the confirmation routed to where the request started.

    • Web AstraChat origin → the pending-action card in the chat is the
      confirmation (external_send always pauses there), so once we actually run
      we just send.
    • Telegram (or any other) origin → to Bahrian himself it goes straight
      through; to a third party only after a Telegram "ja"/✅.
    """
    channel = (args.get("channel") or "").strip().lower()
    to = (args.get("to") or "").strip()
    text = (args.get("text") or "").strip()
    if channel not in _SEND_CHANNELS:
        return tool_result(ok=False, source="core",
                           summary="channel muss telegram|waha|signal sein. Für Mail nutze "
                                   "send_email, für Slack slack_send.")
    if not to or not text:
        return tool_result(ok=False, source="core",
                           summary="'to' (Telefonnummer/Handle) und 'text' sind erforderlich.")

    label = _SEND_LABELS.get(channel, channel)
    display_to = to
    owner_aliases = {"ich", "mich", "mir", "me", "self", "owner",
                     get_settings().astra_owner_name.strip().lower()}
    if channel == "waha" and to.strip().lower() in owner_aliases:
        # WAHA's live me.id is authoritative for the self-chat. It may be a LID
        # instead of the phone-number JID stored in a person profile.
        to = "__self__"
    elif channel in {"waha", "signal"} and "@" not in to and not any(ch.isdigit() for ch in to):
        resolved = knowledge.person_handle_for(channel, to)
        if not resolved:
            return tool_result(
                ok=False, source="core",
                summary=f"Für {to} ist keine {label}-Nummer im Personenprofil hinterlegt.")
        to = resolved

    def failure_summary(reason: str = "") -> str:
        suffix = f": {reason}" if reason else "."
        return f"Senden an {display_to} ({label}) ist fehlgeschlagen{suffix}"

    # Web origin: already confirmed via the AstraChat card → send now.
    if ctx.channel == "web":
        transport = get_channels()
        ok = await transport.send(channel, to, text)
        reason = transport.last_error(channel)
        await db.audit("outbound_web", channel=channel, detail={"to": to, "ok": ok})
        return tool_result(
            ok=ok, source="core",
            summary=(f"Gesendet an {display_to} ({label})." if ok else failure_summary(reason)),
        )

    # Send to Bahrian himself (incl. self-test) → straight through, no gate.
    if await _is_owner_target(channel, to):
        transport = get_channels()
        ok = await transport.send(channel, to, text)
        reason = transport.last_error(channel)
        await db.audit("outbound_self", channel=channel, detail={"to": to, "ok": ok})
        return tool_result(
            ok=ok, source="core",
            summary=(f"An dich selbst auf {label} gesendet." if ok
                     else failure_summary(reason)),
        )

    # Third party → require an explicit Telegram confirmation before anything leaves.
    # The approval rows have FKs: contact_id is a UUID → contacts, thread_id → threads.
    # An owner-initiated send isn't tied to an inbound thread and the owner chat's
    # pseudo ids ("owner" / "web-owner:…") aren't persisted, so pass them only when
    # they actually exist, otherwise NULL.
    s = get_settings()
    raw_cid = ctx.contact.get("id")
    try:
        contact_id = str(_uuid.UUID(str(raw_cid))) if raw_cid else None
    except (ValueError, TypeError):
        contact_id = None
    thread_id = ctx.thread_id if (ctx.thread_id and await db.get_thread(ctx.thread_id)) else None
    approval_id = await db.create_approval(
        thread_id=thread_id, contact_id=contact_id,
        kind="outbound_send", question=text,
        payload={"channel": channel, "to": to, "text": text},
    )
    if s.telegram_enabled and s.telegram_owner_chat_id:
        buttons = [
            {"text": "✅ Senden", "callback_data": f"apv:{approval_id}:yes"},
            {"text": "❌ Abbrechen", "callback_data": f"apv:{approval_id}:no"},
        ]
        await get_channels().send_telegram(
            s.telegram_owner_chat_id,
            f"✉️ ASTRA möchte an {to} ({label}) senden:\n„{text[:800]}“\n\n"
            "Senden? Tippe ✅ oder antworte einfach „ja“.",
            buttons=buttons,
        )
        return tool_result(
            ok=True, source="core",
            summary=f"Bestätigung an dich per Telegram geschickt. Nach deinem „ja“ geht "
                    f"die Nachricht an {to} ({label}) raus.",
        )
    return tool_result(
        ok=False, source="core",
        summary="Telegram ist nicht konfiguriert — ohne Bestätigungskanal sende ich "
                "nichts an Dritte.",
    )


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
        ("astra_integration_installations", "Liste Installationen einer Integration mit runtime_slug.",
         {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
         _integration_installations),
        ("astra_configure_integration",
         "Konfiguriere & (de)aktiviere eine eigene Integration. 'config' = Objekt aus Feld→Wert.",
         {"type": "object", "properties": {
             "slug": {"type": "string"},
             "enable": {"type": "boolean"},
             "config": {"type": "object", "description": "Feld→Wert, z. B. {\"api_key\":\"…\"}"}},
          "required": ["slug"]}, _configure_integration),
        ("astra_test_integration", "Teste die Verbindung einer Integration (health_check).",
         {"type": "object", "properties": {"slug": {"type": "string"}, "runtime_slug": {"type": "string"}},
          "required": ["slug"]},
         _test_integration),
        ("astra_list_plugin_catalog", "Liste native und katalogisierte Plugins, die ASTRA einrichten oder priorisieren kann.",
         {"type": "object", "properties": {"query": {"type": "string"}}}, _list_plugin_catalog),
        ("astra_create_plugin_module",
         "Lege eine neue Plugin-Datei an. Mutiert den Quellcode und braucht Freigabe im Ask-Modus.",
         {"type": "object", "properties": {
             "slug": {"type": "string"},
             "name": {"type": "string"},
             "description": {"type": "string"},
             "category": {"type": "string"}},
          "required": ["slug", "name", "category"]}, _create_plugin_module),
        ("astra_get_settings", "Zeige ASTRAs aktuelle Einstellungen (Modell, Autonomie, Standort …).",
         {"type": "object", "properties": {}}, _get_settings),
        ("astra_update_settings",
         "Ändere ASTRAs Einstellungen: model, autonomy(ask|confident|full), economy, font, "
         "allow_self_config und Secretary global ein/aus.",
         {"type": "object", "properties": {
             "model": {"type": "string"}, "autonomy": {"type": "string"},
             "economy": {"type": "boolean"}, "font": {"type": "string"},
             "allow_self_config": {"type": "boolean"},
             "secretary_enabled": {"type": "boolean",
                                     "description": "Secretary global ein- oder ausschalten"}}},
         _update_settings),
        ("astra_system_status", "Container-Leistung (RAM/CPU/Disk/Uptime) + Empfehlungen.",
         {"type": "object", "properties": {}}, _system_status),
        ("astra_brain_list", "Liste alle Brain-/Wissens-Dateien (über mich + pro Person).",
         {"type": "object", "properties": {"query": {"type": "string"}}}, _brain_list),
        ("astra_brain_read", "Lies eine Brain-Datei (z. B. facts.md oder people/<slug>.md).",
         {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]},
         _brain_read),
        ("astra_person_lookup",
         "Suche automatisch in Personen-Dateien nach einem Namen und liefere Telefon-, "
         "WhatsApp-, Signal-, Telegram- und Mail-Kontaktdaten. Bei Fragen wie 'Was ist die "
         "Nummer von Penelope?' dieses Tool direkt verwenden, ohne vorher nachzufragen.",
         {"type": "object", "properties": {
             "name": {"type": "string", "description": "Vollständiger oder teilweiser Name"}},
          "required": ["name"]}, _person_lookup),
        ("astra_brain_write", "Überschreibe eine Brain-Datei komplett mit neuem Markdown.",
         {"type": "object", "properties": {
             "file": {"type": "string"}, "content": {"type": "string"}},
          "required": ["file", "content"]}, _brain_write),
        ("astra_brain_append", "Hänge eine Notiz (Bullet) an eine Brain-Datei an.",
         {"type": "object", "properties": {
             "file": {"type": "string"}, "text": {"type": "string"}},
          "required": ["file", "text"]}, _brain_append),
        ("astra_brain_add_person", "Lege eine neue Personen-Brain-Datei an (people/<slug>.md).",
         {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
         _brain_add_person),
        ("astra_world_alias",
         "Merke dir, wie Bahrian einen Raum oder ein Gerät nennt — z. B. 'mein Zimmer' für "
         "'Schlafzimmer' oder 'unten' für 'Wohnzimmer'. Danach findet home_state/home_control "
         "das Ziel auch unter diesem Wort. action=list|add|remove, target=echter Name, "
         "alias=Bahrians Wort. Tippfehler und Umlaute musst du NICHT als Alias anlegen — "
         "die verzeiht der Resolver von sich aus.",
         {"type": "object", "properties": {
             "action": {"type": "string", "enum": ["list", "add", "remove"]},
             "target": {"type": "string"},
             "alias": {"type": "string"}}}, _world_alias),
        ("astra_secretary_contacts_get",
         "Zeige alle Secretary-Kontaktregeln und die Standardaktion für unbekannte Sender.",
         {"type": "object", "properties": {}}, _secretary_contact_rules_get),
        ("astra_secretary_contacts_set",
         "Füge eine Kontaktregel hinzu/aktualisiere sie (action=add), entferne sie (action=remove), "
         "oder setze die Unbekannt-Sender-Aktion (action=set_unknown, unknown_sender_action=policy|ask_owner|block). "
         "Felder: channel(waha|signal|slack|email|*), id(Nummer/JID), rule(block|ask|allow|direct), note.",
         {"type": "object", "properties": {
             "action": {"type": "string", "enum": ["add", "remove", "set_unknown"]},
             "channel": {"type": "string"},
             "id": {"type": "string"},
             "rule": {"type": "string", "enum": ["block", "ask", "allow", "direct"]},
             "note": {"type": "string"},
             "unknown_sender_action": {"type": "string"}}},
         _secretary_contact_rules_set),
        ("astra_secretary_email_get",
         "Zeige die aktuell konfigurierten Mail-Konten im Secretary (Passwörter maskiert).",
         {"type": "object", "properties": {}}, _secretary_email_get),
        ("astra_secretary_email_set",
         "Speichere die Secretary Mail-Konten-Liste. Jedes Konto: from_address, imap_host, imap_port, "
         "imap_user, password (leer=behalten), smtp_host, smtp_port, title, enabled, poll_minutes.",
         {"type": "object", "properties": {
             "accounts": {
                 "type": "array",
                 "items": {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "from_address": {"type": "string"},
                     "imap_host": {"type": "string"},
                     "imap_port": {"type": "string"},
                     "imap_user": {"type": "string"},
                     "password": {"type": "string"},
                     "imap_mailbox": {"type": "string"},
                     "smtp_host": {"type": "string"},
                     "smtp_port": {"type": "string"},
                     "poll_minutes": {"type": "string"},
                     "enabled": {"type": "boolean"}}}}},
          "required": ["accounts"]}, _secretary_email_set),
        ("astra_person_profiles_save",
         "Lege/aktualisiere Personen-Profile (people/<slug>.md) — ideal um Apple-Kontakte "
         "reinzudumpen. Jeder Eintrag: name, relationship, trust_tier(0-3), tone(Umgangston), "
         "phone/whatsapp/signal/telegram/email, notes. Telefonnummer wird auto. für WhatsApp+Signal "
         "übernommen. Bestehende Notizen bleiben erhalten (Handles/Ton werden gemergt).",
         {"type": "object", "properties": {
             "profiles": {"type": "array", "items": {"type": "object", "properties": {
                 "name": {"type": "string"},
                 "relationship": {"type": "string"},
                 "trust_tier": {"type": "string"},
                 "tone": {"type": "string"},
                 "phone": {"type": "string"},
                 "whatsapp": {"type": "string"},
                 "signal": {"type": "string"},
                 "telegram": {"type": "string"},
                 "email": {"type": "string"},
                 "notes": {"type": "string"}}}}},
          "required": ["profiles"]}, _person_profiles_save),
        ("astra_models_show",
         "Zeige, welches Modell welcher Stufe zugeordnet ist (small/medium/heavy/code/osint) "
         "und welche Anbieter konfiguriert sind.",
         {"type": "object", "properties": {}}, _models_show),
        ("astra_models_set",
         "Ordne einer Stufe einen Anbieter + ein Modell zu. role=small(einfach)|medium(normal, "
         "mit Tools)|heavy(schwer)|code(Programmieren, z. B. OpenAI Codex)|osint(Recherche). "
         "provider=openai|anthropic|openrouter|ollama oder ein selbst angelegter. "
         "Achtung: 'medium' braucht einen OpenAI-kompatiblen Anbieter, weil dort Tools laufen.",
         {"type": "object", "properties": {
             "role": {"type": "string", "enum": ["small", "medium", "heavy", "code", "osint"]},
             "provider": {"type": "string"},
             "model": {"type": "string"}},
          "required": ["role", "provider", "model"]}, _models_set),
        ("astra_models_add_provider",
         "Registriere einen weiteren Anbieter. Jeder OpenAI-kompatible Endpoint funktioniert "
         "(OpenRouter, Groq, DeepSeek, Together, LM Studio, vLLM, lokales Ollama): "
         "kind=openai_compat + base_url + api_key. Für Claude: kind=anthropic.",
         {"type": "object", "properties": {
             "name": {"type": "string"},
             "kind": {"type": "string", "enum": ["openai_compat", "anthropic"]},
             "base_url": {"type": "string"},
             "api_key": {"type": "string"},
             "tools": {"type": "boolean"}},
          "required": ["name"]}, _models_add_provider),
        ("astra_model_run",
         "Führe einen einzelnen Teilauftrag über eine konfigurierte Modell-Rolle aus. Nutze "
         "dieses Tool insbesondere, wenn Bahrian ausdrücklich sagt „nutze Claude“, „nutze "
         "Codex“ oder „nutze das Recherche-Modell“: Claude liegt typischerweise auf heavy, "
         "Codex auf code und OSINT auf osint. Der Aufruf ist Text-only und führt selbst keine "
         "Aktionen aus.",
         {"type": "object", "properties": {
             "role": {"type": "string",
                      "enum": ["small", "medium", "heavy", "code", "osint"]},
             "prompt": {"type": "string"},
             "system": {"type": "string"},
             "max_tokens": {"type": "integer", "minimum": 256, "maximum": 8000}},
          "required": ["role", "prompt"]}, _model_run),
        ("astra_delegate_job",
         "Gib eine schwere HomeLab-/Analyse-Aufgabe an das große Modell (Claude) ab, statt sie "
         "selbst zu lösen. goal=Ziel im Klartext, hosts=Liste erlaubter Hosts (Rechte-Umschlag!), "
         "write=true nur wenn verändert werden darf, kind=ops|analyze|research. Das große Modell "
         "plant; jeder Schritt wird gegen die Command-Policy geprüft — Allow-List läuft autonom, "
         "alles andere braucht Bahrians Freigabe, Destruktives wird blockiert.",
         {"type": "object", "properties": {
             "goal": {"type": "string"},
             "hosts": {"type": "array", "items": {"type": "string"}},
             "write": {"type": "boolean"},
             "kind": {"type": "string", "enum": ["ops", "analyze", "research"]},
             "budget_steps": {"type": "integer"}},
          "required": ["goal"]}, _delegate_job),
        ("astra_jobs_list", "Liste die letzten delegierten Jobs mit Status.",
         {"type": "object", "properties": {}}, _jobs_list),
        ("astra_job_show", "Zeige Plan und Rechte-Umschlag eines Jobs. id = Jobnummer.",
         {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
         _job_show),
        ("astra_secretary_simulate",
         "Simuliere die Sekretär-Entscheidung für eine erfundene Nachricht — zeigt Fenster, "
         "Plan (AUTO/DEFER/ASK/STILL) und Schattenmodus, SENDET NICHTS. Felder: channel"
         "(waha|signal|slack|email), time('HH:MM', optional), weekday(0=Mo..6=So, optional), "
         "triage(auto|defer|ask), is_group(bool).",
         {"type": "object", "properties": {
             "channel": {"type": "string"}, "time": {"type": "string"},
             "weekday": {"type": "integer"}, "triage": {"type": "string"},
             "is_group": {"type": "boolean"}}}, _secretary_simulate),
        ("astra_rules_list", "Zeige alle Automatik-Regeln (Trigger → Aktionen) mit Status.",
         {"type": "object", "properties": {}}, _rules_list),
        ("astra_rule_create",
         "Lege eine Automatik-Regel an: 'wenn Trigger (+ Bedingung), dann Aktionen'. "
         "trigger z. B. {\"type\":\"schedule\",\"at\":\"21:50\",\"days\":[0,1,2,3,4]}. "
         "condition optional, z. B. {\"type\":\"tool\",\"tool\":\"duolingo_status\","
         "\"expect\":{\"path\":\"data.done\",\"equals\":false}}. actions ist eine Liste, z. B. "
         "[{\"type\":\"speak\",\"text\":\"Duolingo nicht vergessen\",\"where\":\"Schlafzimmer\"},"
         "{\"type\":\"notify\",\"text\":\"Duolingo offen\"},{\"type\":\"tool\","
         "\"tool\":\"add_google_task\",\"args\":{\"title\":\"Duolingo\"}}]. Wenn Bahrian dich im "
         "Chat darum bittet, ist die Regel sofort aktiv.",
         {"type": "object", "properties": {
             "name": {"type": "string"},
             "trigger": {"type": "object"},
             "condition": {"type": "object"},
             "actions": {"type": "array", "items": {"type": "object"}},
             "plugin_slug": {"type": "string"}},
          "required": ["name", "trigger", "actions"]}, _rules_create),
        ("astra_rule_confirm", "Gib eine wartende Regel frei (macht sie aktiv). id = Regelnummer.",
         {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
         _rules_confirm),
        ("astra_rule_delete", "Lösche eine Regel. id = Regelnummer.",
         {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
         _rules_delete),
        ("astra_rule_run_now", "Führe eine Regel sofort testweise aus (ignoriert den Zeitplan).",
         {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
         _rules_run_now),
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
            "astra_configure_integration", "astra_update_settings", "astra_test_capability",
            "astra_create_plugin_module", "astra_brain_write", "astra_brain_append",
            "astra_brain_add_person", "astra_secretary_email_set", "astra_secretary_contacts_set",
            "astra_person_profiles_save", "astra_world_alias",
            "astra_rule_create", "astra_rule_confirm", "astra_rule_delete", "astra_rule_run_now",
            "astra_delegate_job", "astra_models_set", "astra_models_add_provider",
        } else "private_read"
        intents = ["status", "list"] if "list" in name or "status" in name else ["control"] if safety == "mutation" else ["status"]
        register(Tool(name=name, description=desc, parameters=params, handler=handler,
                      owner_only=True, source="core", safety=safety, intents=intents))

    model_tool = REGISTRY.get("astra_model_run")
    if model_tool:
        model_tool.safety = "external_send"
        model_tool.intents = ["research", "control"]

    register(Tool(
        name="astra_send_message",
        description=(
            "Sende eine WhatsApp-/Signal-/Telegram-Nachricht. An dich selbst (Bahrian) geht "
            "sie sofort raus; an jede andere Person erst nach deiner Bestätigung im "
            "Ursprungs-Chat. channel=telegram|waha(WhatsApp)|signal, to=Telefonnummer/Handle "
            "(WhatsApp: Nummer; für dich selbst 'Bahrian' oder 'mich'), text=Inhalt. "
            "Für Mail nutze send_email, für Slack slack_send."
        ),
        parameters={"type": "object", "properties": {
            "channel": {"type": "string", "enum": sorted(_SEND_CHANNELS)},
            "to": {"type": "string", "description": "Telefonnummer, Handle oder Mailadresse"},
            "text": {"type": "string"}},
            "required": ["channel", "to", "text"]},
        handler=_send_message, owner_only=True, source="core",
        safety="external_send", intents=["control"],
    ))
    log.info("Registered %d self-admin tools.", len(defs) + 1)
