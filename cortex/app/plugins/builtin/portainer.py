"""Portainer Container-UI — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class PortainerPlugin(Plugin):
    slug = "portainer"
    name = "Portainer Container-UI"
    description = "Container-Management via Portainer API."
    category = PluginCategory.INFRA_AI
    icon = "🐋"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
