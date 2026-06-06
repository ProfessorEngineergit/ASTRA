"""Vaultwarden/Bitwarden — stub (vault decryption needs the BW crypto stack).

Reading secrets requires the full Bitwarden key-derivation + decryption flow; a
safe implementation is planned. Until then this stays a catalog placeholder.
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
