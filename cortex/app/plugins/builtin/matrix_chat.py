"""Matrix/Element Chat — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class MatrixChatPlugin(Plugin):
    slug = "matrix"
    name = "Matrix/Element Chat"
    description = "Ende-zu-Ende-verschlüsselte Matrix-Räume lesen und beschreiben."
    category = PluginCategory.COMMS
    icon = "💬"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
