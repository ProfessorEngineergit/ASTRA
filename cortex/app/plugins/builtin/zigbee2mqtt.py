"""Zigbee2MQTT — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class Zigbee2MqttPlugin(Plugin):
    slug = "zigbee2mqtt"
    name = "Zigbee2MQTT"
    description = "Zigbee-Geräte via MQTT/Zigbee2MQTT steuern."
    category = PluginCategory.SMART_HOME
    icon = "📡"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
