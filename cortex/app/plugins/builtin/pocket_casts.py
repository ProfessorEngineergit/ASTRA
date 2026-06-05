"""Pocket Casts Podcasts — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class PocketCastsPlugin(Plugin):
    slug = "pocket_casts"
    name = "Pocket Casts"
    description = "Podcast-Queue und nächste Episoden aus Pocket Casts."
    category = PluginCategory.MEDIA
    icon = "🎙️"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
