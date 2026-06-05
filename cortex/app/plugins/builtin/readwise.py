"""Readwise Reader — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class ReadwisePlugin(Plugin):
    slug = "readwise"
    name = "Readwise Reader"
    description = "Highlights und Artikel aus Readwise Reader."
    category = PluginCategory.MEDIA
    icon = "📚"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
