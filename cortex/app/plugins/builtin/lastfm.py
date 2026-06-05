"""Last.fm Musik — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class LastFmPlugin(Plugin):
    slug = "lastfm"
    name = "Last.fm Musik"
    description = "Aktuell gespielt, Scrobbles und Top-Tracks aus Last.fm."
    category = PluginCategory.MEDIA
    icon = "🎵"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
