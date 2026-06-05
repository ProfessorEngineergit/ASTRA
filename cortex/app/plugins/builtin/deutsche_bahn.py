"""Deutsche Bahn — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class DeutscheBahnPlugin(Plugin):
    slug = "deutsche_bahn"
    name = "Deutsche Bahn"
    description = "DB Reiseauskunft: Verbindungen, Verspätungen, Störungen."
    category = PluginCategory.TRANSPORT
    icon = "🚄"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
