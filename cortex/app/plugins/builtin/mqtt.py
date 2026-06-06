"""MQTT — stub (requires an MQTT client lib not yet bundled)."""
from ..base import ConfigField, FieldType, HealthState, HealthStatus, Plugin, PluginCategory


class MqttPlugin(Plugin):
    slug = "mqtt"
    name = "MQTT Broker"
    description = "Nachrichten via MQTT veröffentlichen/abonnieren (Mosquitto u. a.)."
    category = PluginCategory.SMART_HOME
    icon = "📡"
    coming_soon = True
    config_fields = [
        ConfigField("host", "Broker-Host", help="z. B. 192.168.178.x"),
        ConfigField("port", "Port", type=FieldType.NUMBER, default=1883),
        ConfigField("username", "Benutzer"),
        ConfigField("password", "Passwort", secret=True),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Benötigt eine MQTT-Client-Bibliothek — kommt bald.")
