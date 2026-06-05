"""Gmail Digest — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class GmailPlugin(Plugin):
    slug = "gmail"
    name = "Gmail Digest"
    description = "Ungelesene E-Mails zusammenfassen und beantworten."
    category = PluginCategory.COMMS
    icon = "📧"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
