"""Vaultwarden/Bitwarden — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class VaultwardenPlugin(Plugin):
    slug = "vaultwarden"
    name = "Vaultwarden/Bitwarden"
    description = "Passwörter und Notizen aus Vaultwarden abrufen."
    category = PluginCategory.INFRA_AI
    icon = "🔐"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
