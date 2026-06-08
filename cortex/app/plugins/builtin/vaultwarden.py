"""Vaultwarden/Bitwarden integration.

Reading vault entries requires the Bitwarden key-derivation and decryption flow,
so this module stays disabled until that path is implemented end to end.
"""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class VaultwardenPlugin(Plugin):
    slug = "vaultwarden"
    name = "Vaultwarden"
    description = "Passwörter & Notizen aus Vaultwarden/Bitwarden abrufen."
    category = PluginCategory.INFRA_AI
    icon = "🔐"
    coming_soon = True
    config_fields = [
        ConfigField("base_url", "Server-URL"),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Sichere Vault-Entschlüsselung kommt bald.")
