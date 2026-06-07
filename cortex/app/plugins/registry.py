"""Plugin discovery + lifecycle manager.

- Discovers every Plugin subclass under `plugins/builtin/`.
- Instantiates each with config from the ConfigStore (DB > .env > default).
- On `rebuild()`: re-registers the tools of *enabled* plugins into tools.REGISTRY
  and (re)starts their background tasks — i.e. live reconfiguration without a
  container restart.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import re

from .. import tools
from ..config_store import get_config_store
from . import base, builtin
from .base import Plugin

log = logging.getLogger("astra.plugins")


def _discover_classes() -> list[type[Plugin]]:
    """Import every module under builtin/ and return all Plugin subclasses."""
    for _finder, modname, _ispkg in pkgutil.iter_modules(builtin.__path__):
        importlib.import_module(f"{builtin.__name__}.{modname}")
    seen: dict[str, type[Plugin]] = {}

    def walk(cls: type[Plugin]) -> None:
        for sub in cls.__subclasses__():
            if getattr(sub, "slug", ""):
                seen[sub.slug] = sub
            walk(sub)

    walk(Plugin)
    return list(seen.values())


class PluginManager:
    def __init__(self) -> None:
        self._classes: list[type[Plugin]] = []
        self._instances: dict[str, Plugin] = {}
        self._bg_tasks: dict[str, list[asyncio.Task]] = {}

    @staticmethod
    def _tool_name_for_installation(tool_name: str, install_id: str) -> str:
        if install_id == "default":
            return tool_name
        suffix = re.sub(r"[^a-zA-Z0-9_]", "_", install_id).strip("_") or "extra"
        return f"{tool_name}__{suffix}"[:64]

    # ── loading ──────────────────────────────────────────────────────────────
    async def load_all(self) -> None:
        """(Re)instantiate every plugin from current config."""
        if not self._classes:
            self._classes = _discover_classes()
        store = get_config_store()
        self._instances = {}
        for cls in self._classes:
            try:
                for cfg in await store.load_installations(cls):
                    key = str(cfg.get("__runtime_slug") or cls.slug)
                    self._instances[key] = cls(cfg)
            except Exception:  # noqa: BLE001 — one bad plugin must not break the rest
                log.exception("Failed to load plugin %s", getattr(cls, "slug", "?"))

    def get(self, slug: str) -> Plugin | None:
        return self._instances.get(slug)

    def plugin_class(self, slug: str) -> type[Plugin] | None:
        for cls in self._classes:
            if cls.slug == slug:
                return cls
        return None

    def all(self) -> list[Plugin]:
        return sorted(
            [p for p in self._instances.values() if p.installation_id == "default"],
            key=lambda p: (p.category.value, p.name),
        )

    def installations(self, slug: str) -> list[Plugin]:
        return sorted(
            [p for p in self._instances.values() if p.base_slug == slug],
            key=lambda p: (p.installation_id != "default", p.installation_name.lower()),
        )

    def enabled(self) -> list[Plugin]:
        return [p for p in self._instances.values() if p.enabled]

    # ── tool registration ────────────────────────────────────────────────────
    def _register_tools(self) -> None:
        tools.clear_all_plugin_tools()
        for p in self.enabled():
            try:
                for t in p.tools():
                    t.name = self._tool_name_for_installation(t.name, p.installation_id)
                    t.source = p.runtime_slug
                    if p.installation_id != "default":
                        t.description = f"{t.description} Installation: {p.installation_name}."
                    tools.register(t)
            except Exception:  # noqa: BLE001
                log.exception("Tool registration failed for plugin %s", p.runtime_slug)

    # ── background tasks ─────────────────────────────────────────────────────
    def _sync_background_tasks(self) -> None:
        enabled_slugs = {p.runtime_slug for p in self.enabled()}
        # cancel tasks of plugins no longer enabled
        for slug in list(self._bg_tasks):
            if slug not in enabled_slugs:
                for t in self._bg_tasks.pop(slug):
                    t.cancel()
        # start tasks for newly enabled plugins
        for p in self.enabled():
            if p.runtime_slug in self._bg_tasks:
                continue
            coros = p.background_tasks()
            if coros:
                self._bg_tasks[p.runtime_slug] = [
                    asyncio.create_task(c, name=f"plugin:{p.runtime_slug}") for c in coros
                ]
                log.info("Started %d background task(s) for plugin %s", len(coros), p.runtime_slug)

    async def rebuild(self) -> None:
        """Reload config, re-register tools, resync background tasks."""
        await self.load_all()
        self._register_tools()
        self._sync_background_tasks()
        log.info("Plugins rebuilt — enabled: %s",
                 ", ".join(p.runtime_slug for p in self.enabled()) or "(none)")

    # ── aggregation for other subsystems ─────────────────────────────────────
    async def briefing_sections(self) -> list[str]:
        out: list[str] = []
        for p in self.enabled():
            try:
                s = await p.briefing_section()
            except Exception as e:  # noqa: BLE001
                log.warning("briefing_section failed for %s: %s", p.slug, e)
                s = None
            if s:
                out.append(s)
        return out

    async def shutdown(self) -> None:
        for tasks_ in self._bg_tasks.values():
            for t in tasks_:
                t.cancel()
        self._bg_tasks.clear()


_manager: PluginManager | None = None


def get_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager


# expose category labels for the web UI
CATEGORY_LABELS = base.CATEGORY_LABELS
