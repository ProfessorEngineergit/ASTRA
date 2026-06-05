"""Moodle LMS — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class MoodlePlugin(Plugin):
    slug = "moodle"
    name = "Moodle LMS"
    description = "Aufgaben, Kurse und Ankündigungen aus Moodle."
    category = PluginCategory.SCHOOL
    icon = "🎓"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
