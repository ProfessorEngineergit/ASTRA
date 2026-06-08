"""Zigbee2MQTT integration.

Direct control runs through MQTT. If Home Assistant is already connected,
Zigbee devices can also be controlled through the Home Assistant plugin.
"""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class Zigbee2MqttPlugin(Plugin):
    slug = "zigbee2mqtt"
    name = "Zigbee2MQTT"
    description = "Zigbee-Geräte via Zigbee2MQTT steuern (oder schon heute über Home Assistant)."
    category = PluginCategory.SMART_HOME
    icon = "📶"
    coming_soon = True
    config_fields = [
        ConfigField("base_topic", "Base-Topic", default="zigbee2mqtt"),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Steuerung läuft über MQTT — kommt bald. Tipp: nutze solange das "
                            "Home-Assistant-Plugin.")
