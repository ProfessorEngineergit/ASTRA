"""YouTube — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class YouTubePlugin(Plugin):
    slug = "youtube"
    name = "YouTube"
    description = "YouTube-Suche und Kanal-Benachrichtigungen."
    category = PluginCategory.MEDIA
    icon = "▶️"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
