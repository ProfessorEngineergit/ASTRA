"""IServ integration.

IServ exposes mail and calendar data through standard protocols such as IMAP,
WebDAV, and CalDAV. Mail access can already be configured through the IMAP
plugin.
"""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class IServPlugin(Plugin):
    slug = "iserv"
    name = "IServ"
    description = "Aufgaben, E-Mails & Termine aus IServ (Schulplattform)."
    category = PluginCategory.SCHOOL
    icon = "🏫"
    coming_soon = True
    config_fields = [
        ConfigField("domain", "IServ-Domain", help="z. B. schule.example.de"),
    ]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED,
                            "Native IServ-Anbindung kommt bald. Tipp: E-Mails schon heute über das "
                            "IMAP-Plugin (imap.<deine-schule>).")
