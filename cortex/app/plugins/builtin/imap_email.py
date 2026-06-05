"""IMAP E-Mail Digest — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class ImapEmailPlugin(Plugin):
    slug = "imap_email"
    name = "IMAP E-Mail Digest"
    description = "Beliebige IMAP-Postfächer: Digest + Zusammenfassung."
    category = PluginCategory.COMMS
    icon = "📬"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
