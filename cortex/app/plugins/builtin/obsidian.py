"""Obsidian Vault — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class ObsidianPlugin(Plugin):
    slug = "obsidian"
    name = "Obsidian Vault"
    description = "Notizen im Obsidian-Vault via Local REST API anlegen."
    category = PluginCategory.PRODUCTIVITY
    icon = "🔮"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
