"""Mealie Rezepte — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class MealiePlugin(Plugin):
    slug = "mealie"
    name = "Mealie Rezepte"
    description = "Rezepte suchen und Mahlzeitenplanung via Mealie."
    category = PluginCategory.PRODUCTIVITY
    icon = "🍽️"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
