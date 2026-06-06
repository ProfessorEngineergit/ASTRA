"""HVV Hamburg — stub (HVV's open API needs registered geofox credentials)."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class HvvPlugin(Plugin):
    slug = "hvv"
    name = "HVV Hamburg"
    description = "Hamburger Nahverkehr: Abfahrten & Verbindungen (geofox API)."
    category = PluginCategory.TRANSPORT
    icon = "🚏"
    coming_soon = True
    config_fields = [
        ConfigField("home_stop", "Heim-Haltestelle", default="Jungfernstieg"),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Braucht geofox-API-Zugang (Registrierung bei der HVV) — kommt bald.")
