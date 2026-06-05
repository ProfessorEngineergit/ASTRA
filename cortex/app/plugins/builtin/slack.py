"""Slack — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class SlackPlugin(Plugin):
    slug = "slack"
    name = "Slack"
    description = "Nachrichten in Slack-Kanäle senden und empfangen."
    category = PluginCategory.COMMS
    icon = "💬"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
