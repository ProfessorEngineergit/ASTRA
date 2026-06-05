"""Paperless-NGX — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class PaperlessPlugin(Plugin):
    slug = "paperless"
    name = "Paperless-NGX"
    description = "Dokumente suchen und hochladen via Paperless-NGX."
    category = PluginCategory.PRODUCTIVITY
    icon = "📄"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
