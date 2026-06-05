"""Immich Fotos — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class ImmichPlugin(Plugin):
    slug = "immich"
    name = "Immich Fotos"
    description = "Fotos durchsuchen und Erinnerungen anzeigen via Immich."
    category = PluginCategory.MEDIA
    icon = "🖼️"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
