"""Apple HomeKit Shortcuts — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class HomekitPlugin(Plugin):
    slug = "homekit"
    name = "Apple HomeKit Shortcuts"
    description = "HomeKit-Szenen via Apple Shortcuts Webhook auslösen."
    category = PluginCategory.SMART_HOME
    icon = "🏡"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
