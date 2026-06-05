"""IServ Schulplattform — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class IServPlugin(Plugin):
    slug = "iserv"
    name = "IServ Schulplattform"
    description = "Aufgaben, E-Mails und Ankündigungen aus IServ."
    category = PluginCategory.SCHOOL
    icon = "🏫"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
