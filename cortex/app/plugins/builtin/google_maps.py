"""Google Maps — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class GoogleMapsPlugin(Plugin):
    slug = "google_maps"
    name = "Google Maps Verkehr"
    description = "Verkehrslage und Routenplanung via Google Maps."
    category = PluginCategory.TRANSPORT
    icon = "🗺️"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
