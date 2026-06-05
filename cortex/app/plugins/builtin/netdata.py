"""Netdata Monitoring — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class NetdataPlugin(Plugin):
    slug = "netdata"
    name = "Netdata Monitoring"
    description = "Server-Metriken und Alerts aus Netdata."
    category = PluginCategory.INFRA_AI
    icon = "📈"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
