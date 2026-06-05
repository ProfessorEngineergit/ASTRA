"""Tasmota Smart Plugs — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class TasmotaPlugin(Plugin):
    slug = "tasmota"
    name = "Tasmota Smart Plugs"
    description = "Tasmota-basierte Smart Plugs und Schalter steuern."
    category = PluginCategory.SMART_HOME
    icon = "🔌"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
