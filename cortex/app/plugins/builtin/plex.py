"""Plex Media Server — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class PlexPlugin(Plugin):
    slug = "plex"
    name = "Plex Media Server"
    description = "Bibliothek durchsuchen und aktuelle Wiedergabe."
    category = PluginCategory.MEDIA
    icon = "🎬"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
