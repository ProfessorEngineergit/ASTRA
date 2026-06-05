"""Krypto-Kurse via CoinGecko — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class CryptoPricesPlugin(Plugin):
    slug = "crypto"
    name = "Krypto-Kurse"
    description = "Aktueller BTC/ETH/… Preis via CoinGecko (kostenlos)."
    category = PluginCategory.PRODUCTIVITY
    icon = "💰"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
