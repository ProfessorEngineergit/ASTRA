"""ASTRA plugin system.

A plugin is a self-contained capability (RMV, Home Assistant, …) declared by a
single class. Its `config_fields` drive the web-admin form, validation and the
`enabled` check at once. The PluginManager discovers every plugin under
`plugins/builtin/`, instantiates it with config from the ConfigStore (DB > .env >
default), and registers the tools of *enabled* plugins into the agent.

Adding a plugin = one ~50-line file in `plugins/builtin/`. Nothing else to touch.
"""
from .base import (
    ConfigField,
    FieldType,
    HealthState,
    HealthStatus,
    Plugin,
    PluginCategory,
)

__all__ = [
    "ConfigField",
    "FieldType",
    "HealthState",
    "HealthStatus",
    "Plugin",
    "PluginCategory",
]
