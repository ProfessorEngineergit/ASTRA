"""Discord — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class DiscordPlugin(Plugin):
    slug = "discord"
    name = "Discord"
    description = "Discord-Kanal-Nachrichten senden (Webhook)."
    category = PluginCategory.COMMS
    icon = "🎮"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
