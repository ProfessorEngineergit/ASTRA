"""Philips Hue — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class PhilipsHuePlugin(Plugin):
    slug = "philips_hue"
    name = "Philips Hue"
    description = "Lampen steuern via Hue Bridge."
    category = PluginCategory.SMART_HOME
    icon = "💡"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
