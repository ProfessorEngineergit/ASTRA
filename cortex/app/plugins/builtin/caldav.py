"""CalDAV calendar integration.

Generic CalDAV support needs robust iCalendar parsing and sync handling. Google
Calendar has a dedicated plugin for read/write access.
"""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class CalDavPlugin(Plugin):
    slug = "caldav"
    name = "CalDAV Kalender"
    description = "Generischer CalDAV-Kalender (Nextcloud, Radicale, mailbox.org …)."
    category = PluginCategory.PRODUCTIVITY
    icon = "📆"
    coming_soon = True
    config_fields = [
        ConfigField("url", "CalDAV-URL"),
        ConfigField("username", "Benutzer"),
        ConfigField("password", "Passwort", secret=True),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Generisches CalDAV kommt bald. Tipp: für Google nutze das "
                            "Google-Kalender-Plugin (read + write).")
