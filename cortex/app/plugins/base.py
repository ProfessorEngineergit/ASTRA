"""Plugin interface — declarative config schema + capability hooks."""
from __future__ import annotations

from abc import ABC
from collections.abc import Awaitable, Coroutine, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldType(str, Enum):
    TEXT = "text"
    PASSWORD = "password"
    BOOL = "bool"
    NUMBER = "number"
    SELECT = "select"
    MULTISELECT = "multiselect"


class PluginCategory(str, Enum):
    TRANSPORT = "transport"
    SMART_HOME = "smart_home"
    SCHOOL = "school"
    PRODUCTIVITY = "productivity"
    MEDIA = "media"
    INFRA_AI = "infra_ai"
    COMMS = "comms"


CATEGORY_LABELS: dict[PluginCategory, str] = {
    PluginCategory.TRANSPORT: "Transport",
    PluginCategory.SMART_HOME: "Smart Home",
    PluginCategory.SCHOOL: "Schule",
    PluginCategory.PRODUCTIVITY: "Produktivität",
    PluginCategory.MEDIA: "Medien",
    PluginCategory.INFRA_AI: "Infra & KI",
    PluginCategory.COMMS: "Kommunikation",
}

# Fallback card icon per category (cards may still use emojis; filter chips don't).
CATEGORY_EMOJI: dict[PluginCategory, str] = {
    PluginCategory.TRANSPORT: "🚆",
    PluginCategory.SMART_HOME: "🏠",
    PluginCategory.SCHOOL: "🎓",
    PluginCategory.PRODUCTIVITY: "✅",
    PluginCategory.MEDIA: "🎬",
    PluginCategory.INFRA_AI: "🧩",
    PluginCategory.COMMS: "💬",
}


@dataclass
class ConfigField:
    """One configurable value — renders one form input in the web admin."""
    key: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = False
    default: Any = None
    help: str = ""
    secret: bool = False                       # encrypted at rest, never echoed back
    options: list[str] | None = None           # for SELECT / MULTISELECT
    # Optional fallback to a legacy .env var (back-compat with the pre-plugin config).
    env_fallback: str | None = None

    def coerce(self, raw: Any) -> Any:
        """Coerce a stored/submitted raw value to the field's Python type."""
        if raw is None or raw == "":
            return self.default
        if self.type is FieldType.BOOL:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if self.type is FieldType.NUMBER:
            try:
                f = float(raw)
                return int(f) if f.is_integer() else f
            except (TypeError, ValueError):
                return self.default
        if self.type is FieldType.MULTISELECT:
            if isinstance(raw, list):
                return raw
            return [p for p in str(raw).split(",") if p]
        return raw


class HealthState(str, Enum):
    OK = "ok"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"


@dataclass
class HealthStatus:
    state: HealthState
    message: str = ""

    @classmethod
    def ok(cls, message: str = "Verbunden.") -> "HealthStatus":
        return cls(HealthState.OK, message)

    @classmethod
    def error(cls, message: str) -> "HealthStatus":
        return cls(HealthState.ERROR, message)

    @classmethod
    def not_configured(cls, message: str = "Nicht konfiguriert.") -> "HealthStatus":
        return cls(HealthState.NOT_CONFIGURED, message)

    @classmethod
    def disabled(cls, message: str = "Deaktiviert.") -> "HealthStatus":
        return cls(HealthState.DISABLED, message)


class Plugin(ABC):
    """Base class for every capability. Subclass in `plugins/builtin/`.

    Subclasses set the class attributes (slug/name/…/config_fields) and override
    the capability hooks they provide. They MUST degrade to a no-op when not
    enabled — the agent only ever sees tools of enabled plugins, but defence in
    depth: handlers should re-check `self.enabled`.
    """

    slug: str = ""
    name: str = ""
    description: str = ""
    category: PluginCategory = PluginCategory.INFRA_AI
    icon: str = "🔌"
    config_fields: list[ConfigField] = []
    # Catalog placeholder — shown with a "bald" badge, not yet implemented.
    coming_soon: bool = False
    # If True, the plugin's tools are personal-assistant tools and must never be
    # exposed to third parties (the Tool objects it returns are owner_only).
    owner_only: bool = True

    def __init__(self, cfg: Mapping[str, Any]):
        # cfg is already merged (DB > .env > default) and type-coerced.
        self.cfg: dict[str, Any] = dict(cfg)
        self.installation_id = str(self.cfg.get("__installation_id") or "default")
        self.installation_name = str(self.cfg.get("__installation_name") or "Standard")
        self.base_slug = str(self.cfg.get("__base_slug") or self.slug)
        self.runtime_slug = str(self.cfg.get("__runtime_slug") or self.slug)

    # ── config helpers ───────────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        return self.cfg.get(key, default)

    @property
    def is_toggled_on(self) -> bool:
        # The synthetic "__enabled" key is stored alongside real config.
        return bool(self.cfg.get("__enabled", False))

    @property
    def has_required(self) -> bool:
        for f in self.config_fields:
            if f.required and not self.cfg.get(f.key):
                return False
        return True

    @property
    def enabled(self) -> bool:
        return self.is_toggled_on and self.has_required

    # ── capability hooks (override what you provide) ─────────────────────────
    async def health_check(self) -> HealthStatus:
        if not self.has_required:
            return HealthStatus.not_configured()
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        return HealthStatus.ok("Konfiguriert.")

    def tools(self) -> list[Any]:
        """Return tools.Tool objects this plugin contributes (when enabled)."""
        return []

    async def briefing_section(self) -> str | None:
        """Optional line(s) for the morning briefing."""
        return None

    def background_tasks(self) -> list[Coroutine[Any, Any, Any] | Awaitable[Any]]:
        """Long-running coroutines (e.g. a poller) started while enabled."""
        return []
