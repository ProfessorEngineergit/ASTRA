"""Linear Projektmanagement — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class LinearIssuesPlugin(Plugin):
    slug = "linear"
    name = "Linear Projektmanagement"
    description = "Issues erstellen und anzeigen."
    category = PluginCategory.PRODUCTIVITY
    icon = "📋"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
