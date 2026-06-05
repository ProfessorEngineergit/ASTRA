"""CalDAV Kalender — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class CalDavPlugin(Plugin):
    slug = "caldav"
    name = "CalDAV Kalender"
    description = "Generischer CalDAV-Kalender (Nextcloud, Radicale…)."
    category = PluginCategory.PRODUCTIVITY
    icon = "📆"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
