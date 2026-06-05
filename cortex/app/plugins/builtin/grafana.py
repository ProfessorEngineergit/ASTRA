"""Grafana Dashboards — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class GrafanaPlugin(Plugin):
    slug = "grafana"
    name = "Grafana Dashboards"
    description = "Dashboard-Snapshots und Alert-Status aus Grafana."
    category = PluginCategory.INFRA_AI
    icon = "📊"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
