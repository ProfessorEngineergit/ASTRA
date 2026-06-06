"""IServ — stub (IServ exposes WebDAV/CalDAV/IMAP, no clean REST yet).

Tip: IServ mail can already be read today via the IMAP plugin
(imap.<deine-schule>.de), and the calendar via a future CalDAV plugin.
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
