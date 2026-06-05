"""BVG Berlin — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class BvgPlugin(Plugin):
    slug = "bvg"
    name = "BVG Berlin"
    description = "Berliner Abfahrtszeiten und Verbindungen (BVG API)."
    category = PluginCategory.TRANSPORT
    icon = "🚌"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
