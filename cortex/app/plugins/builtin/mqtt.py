"""MQTT Broker — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class MqttPlugin(Plugin):
    slug = "mqtt"
    name = "MQTT Broker"
    description = "Nachrichten via MQTT veröffentlichen und abonnieren."
    category = PluginCategory.SMART_HOME
    icon = "📡"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
