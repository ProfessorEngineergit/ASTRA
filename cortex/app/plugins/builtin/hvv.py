"""HVV Hamburg — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class HvvPlugin(Plugin):
    slug = "hvv"
    name = "HVV Hamburg"
    description = "Hamburger Abfahrtszeiten und Verbindungen (HVV API)."
    category = PluginCategory.TRANSPORT
    icon = "🚇"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
