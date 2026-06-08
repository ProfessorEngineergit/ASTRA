"""Configurable REST and webhook integrations for common services.

Each integration exposes the same small surface: status, list, search, and send.
Concrete endpoints stay configurable because many self-hosted services differ by
instance, proxy path, or authentication scheme.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


def _slug(name: str) -> str:
    s = name.lower().replace("+", " plus ").replace("&", " und ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:48]


def _class_name(slug: str) -> str:
    return "".join(part.capitalize() for part in slug.split("_")) + "Plugin"


def _short(value: Any, limit: int = 700) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class NativeHttpCatalogPlugin(Plugin):
    """Base class for services that can be connected through HTTP endpoints."""

    owner_only = True
    native_http = True
    default_status_path = ""
    default_list_path = ""
    default_search_path = ""
    default_send_path = ""
    default_query_param = "q"
    auth_style = "bearer"
    config_fields = [
        ConfigField("base_url", "API-Basis-URL", required=False,
                    help="Direkte API-URL des Dienstes, ohne abschliessenden Slash."),
        ConfigField("api_token", "API-Token", FieldType.PASSWORD, required=False, secret=True),
        ConfigField("auth_header", "Auth-Header", default="Authorization",
                    help="Standard: Authorization. Leer lassen fuer keinen Header."),
        ConfigField("auth_scheme", "Auth-Schema", FieldType.SELECT, default="Bearer",
                    options=["Bearer", "Token", "Basic", "X-Api-Key", "raw", "none"]),
        ConfigField("status_path", "Status-Endpunkt", required=False,
                    help="GET-Pfad relativ zur Basis-URL."),
        ConfigField("list_path", "Listen-Endpunkt", required=False,
                    help="GET-Pfad fuer Uebersichten."),
        ConfigField("search_path", "Such-Endpunkt", required=False,
                    help="GET-Pfad fuer Suche."),
        ConfigField("send_path", "Sende-/Create-Endpunkt", required=False,
                    help="POST-Pfad fuer einfache Aktionen."),
        ConfigField("webhook_url", "Webhook-URL", FieldType.PASSWORD, required=False, secret=True,
                    help="Optionaler direkter Webhook fuer send/create statt base_url+send_path."),
        ConfigField("query_param", "Suchparameter", default="q"),
    ]

    def _base(self) -> str:
        return str(self.get("base_url") or "").rstrip("/")

    def _path(self, key: str, default_attr: str) -> str:
        return str(self.get(key) or getattr(self, default_attr, "") or "").strip()

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = self._base()
        if not base:
            return ""
        return base + "/" + path.lstrip("/")

    def _headers(self) -> dict:
        token = str(self.get("api_token") or "")
        header = str(self.get("auth_header") or "Authorization")
        scheme = str(self.get("auth_scheme") or "Bearer")
        if not token or not header or scheme == "none":
            return {}
        if scheme == "X-Api-Key":
            return {"X-Api-Key": token}
        if scheme == "raw":
            return {header: token}
        return {header: f"{scheme} {token}"}

    async def _request(self, method: str, url: str, **kwargs) -> tuple[bool, Any, str]:
        if not url:
            return False, None, "Kein Endpunkt konfiguriert."
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.request(method, url, headers=self._headers(), **kwargs)
            content_type = r.headers.get("content-type", "")
            data = r.json() if "json" in content_type else r.text
            if r.status_code >= 400:
                return False, data, f"HTTP {r.status_code}"
            return True, data, f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, None, str(e)

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        path = self._path("status_path", "default_status_path")
        url = self._url(path) if path else self._base()
        if not url and not self.get("webhook_url"):
            return HealthStatus.not_configured("base_url oder webhook_url fehlt.")
        if not url:
            return HealthStatus.ok("Webhook konfiguriert.")
        ok, _data, summary = await self._request("GET", url)
        return HealthStatus.ok(f"{self.name}: {summary}") if ok else HealthStatus.error(summary)

    def _missing(self, capability: str) -> str:
        return tool_result(
            ok=False,
            summary=f"{self.name}: {capability} ist noch nicht konfiguriert.",
            data={"plugin": self.slug, "needed": capability},
            source=self.slug,
            error={"type": "not_configured", "message": f"Konfiguriere {capability}."},
        )

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            path = self._path("status_path", "default_status_path")
            url = self._url(path) if path else self._base()
            if not url:
                return self._missing("base_url/status_path")
            ok, data, summary = await self._request("GET", url)
            return tool_result(ok=ok, summary=f"{self.name} Status: {summary}",
                               data=data, source=self.slug,
                               error=None if ok else {"type": "http_error", "message": summary})

        async def _list(args: dict, ctx: ToolContext) -> str:
            path = self._path("list_path", "default_list_path")
            url = self._url(path)
            if not url:
                return self._missing("list_path")
            ok, data, summary = await self._request("GET", url)
            return tool_result(ok=ok, summary=f"{self.name} Liste: {_short(data)}",
                               data=data, source=self.slug,
                               error=None if ok else {"type": "http_error", "message": summary})

        async def _search(args: dict, ctx: ToolContext) -> str:
            path = self._path("search_path", "default_search_path")
            url = self._url(path)
            if not url:
                return self._missing("search_path")
            q = args.get("query") or args.get("q") or ""
            param = str(self.get("query_param") or self.default_query_param or "q")
            ok, data, summary = await self._request("GET", url, params={param: q})
            return tool_result(ok=ok, summary=f"{self.name} Suche: {_short(data)}",
                               data=data, source=self.slug,
                               error=None if ok else {"type": "http_error", "message": summary})

        async def _send(args: dict, ctx: ToolContext) -> str:
            webhook = str(self.get("webhook_url") or "")
            path = self._path("send_path", "default_send_path")
            url = webhook or self._url(path)
            if not url:
                return self._missing("webhook_url oder send_path")
            payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
            if args.get("text"):
                payload = {**payload, "text": args["text"]}
            ok, data, summary = await self._request("POST", url, json=payload)
            return tool_result(ok=ok, summary=f"{self.name} Aktion: {summary}",
                               data=data, source=self.slug,
                               error=None if ok else {"type": "http_error", "message": summary})

        prefix = self.slug
        return [
            Tool(
                name=f"{prefix}_status",
                description=f"Status/Health von {self.name} abrufen.",
                parameters={"type": "object", "properties": {}},
                handler=_status, owner_only=True, source=self.slug,
                safety="private_read", intents=["status"],
            ),
            Tool(
                name=f"{prefix}_list",
                description=f"Liste/Uebersicht aus {self.name} abrufen.",
                parameters={"type": "object", "properties": {}},
                handler=_list, owner_only=True, source=self.slug,
                safety="private_read", intents=["list", "status"],
            ),
            Tool(
                name=f"{prefix}_search",
                description=f"{self.name} durchsuchen.",
                parameters={"type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"]},
                handler=_search, owner_only=True, source=self.slug,
                safety="private_read", intents=["search"],
            ),
            Tool(
                name=f"{prefix}_send",
                description=f"Einfache Aktion oder Webhook fuer {self.name} ausfuehren.",
                parameters={"type": "object", "properties": {
                    "text": {"type": "string"},
                    "payload": {"type": "object"},
                }},
                handler=_send, owner_only=True, source=self.slug,
                safety="external_send", intents=["send", "create"],
            ),
        ]


_SPECS = [
    ("MVG München", PluginCategory.TRANSPORT, "Muenchner Verkehr: Abfahrten und Stoerungen."),
    ("MVV München", PluginCategory.TRANSPORT, "Muenchner Verkehrs- und Tarifverbund."),
    ("VBB Berlin-Brandenburg", PluginCategory.TRANSPORT, "Verbund Berlin-Brandenburg."),
    ("VVS Stuttgart", PluginCategory.TRANSPORT, "Verkehrs- und Tarifverbund Stuttgart."),
    ("VRR Rhein-Ruhr", PluginCategory.TRANSPORT, "Verkehrsverbund Rhein-Ruhr."),
    ("VRS Köln/Bonn", PluginCategory.TRANSPORT, "Verkehrsverbund Rhein-Sieg."),
    ("VGN Nürnberg", PluginCategory.TRANSPORT, "Verkehrsverbund Grossraum Nuernberg."),
    ("MDV Mitteldeutschland", PluginCategory.TRANSPORT, "Mitteldeutscher Verkehrsverbund."),
    ("ÖBB Österreich", PluginCategory.TRANSPORT, "Oesterreichische Bahn-Informationen."),
    ("SBB Schweiz", PluginCategory.TRANSPORT, "Schweizer Bahn-Informationen."),
    ("IKEA Dirigera/Trådfri", PluginCategory.SMART_HOME, "IKEA Smart-Home-Hub."),
    ("Matter / Thread", PluginCategory.SMART_HOME, "Herstelleruebergreifende Smart-Home-Bruecke."),
    ("Tuya / SmartLife", PluginCategory.SMART_HOME, "Tuya-/SmartLife-Geraete."),
    ("SwitchBot", PluginCategory.SMART_HOME, "SwitchBot-Geraete, Bots und Sensoren."),
    ("Govee", PluginCategory.SMART_HOME, "Govee LED- und Sensor-Geraete."),
    ("Nanoleaf", PluginCategory.SMART_HOME, "Nanoleaf Licht-Panels und Lines."),
    ("LIFX", PluginCategory.SMART_HOME, "LIFX WLAN-Lampen."),
    ("Sonos", PluginCategory.SMART_HOME, "Sonos Multiroom-Audio."),
    ("Nuki", PluginCategory.SMART_HOME, "Nuki Smart-Lock."),
    ("Netatmo", PluginCategory.SMART_HOME, "Netatmo Wetter, Kameras und Sensoren."),
    ("WebUntis", PluginCategory.SCHOOL, "Stundenplan und Vertretungen."),
    ("Schulmanager Online", PluginCategory.SCHOOL, "Schulorganisation und Aufgaben."),
    ("Microsoft Teams (Edu)", PluginCategory.SCHOOL, "Teams Education Klassen und Aufgaben."),
    ("Google Classroom", PluginCategory.SCHOOL, "Kurse und Aufgaben."),
    ("itslearning", PluginCategory.SCHOOL, "Lernplattform."),
    ("Sdui", PluginCategory.SCHOOL, "Schul-Messenger und Plaene."),
    ("Quizlet", PluginCategory.SCHOOL, "Karteikarten und Lernsets."),
    ("Microsoft 365", PluginCategory.PRODUCTIVITY, "Microsoft 365 Graph-Dienste."),
    ("Microsoft Outlook", PluginCategory.PRODUCTIVITY, "Outlook Mail und Kalender."),
    ("Microsoft To Do", PluginCategory.PRODUCTIVITY, "Microsoft Aufgabenlisten."),
    ("Google Drive", PluginCategory.PRODUCTIVITY, "Google Drive Dateien und Metadaten."),
    ("Dropbox", PluginCategory.PRODUCTIVITY, "Dropbox Dateien und Links."),
    ("OneDrive", PluginCategory.PRODUCTIVITY, "OneDrive Dateien."),
    ("TickTick", PluginCategory.PRODUCTIVITY, "TickTick Aufgaben und Gewohnheiten."),
    ("Asana", PluginCategory.PRODUCTIVITY, "Asana Projekte und Aufgaben."),
    ("ClickUp", PluginCategory.PRODUCTIVITY, "ClickUp Projektmanagement."),
    ("Airtable", PluginCategory.PRODUCTIVITY, "Airtable Tabellen und Datensaetze."),
    ("Confluence", PluginCategory.PRODUCTIVITY, "Confluence Wiki-Seiten."),
    ("Cal.com", PluginCategory.PRODUCTIVITY, "Terminbuchungen und Slots."),
    ("Joplin", PluginCategory.PRODUCTIVITY, "Joplin Notizen."),
    ("Stripe", PluginCategory.PRODUCTIVITY, "Stripe Read-only Kontodaten."),
    ("PayPal", PluginCategory.PRODUCTIVITY, "PayPal Transaktionen."),
    ("Actual Budget", PluginCategory.PRODUCTIVITY, "Actual Budget Haushaltsbuch."),
    ("Firefly III", PluginCategory.PRODUCTIVITY, "Firefly III Finanzdaten."),
    ("Grocy", PluginCategory.PRODUCTIVITY, "Grocy Vorrat und Haushalt."),
    ("Apple Music", PluginCategory.MEDIA, "Apple Music Mediathek."),
    ("Deezer", PluginCategory.MEDIA, "Deezer Musikdaten."),
    ("Twitch", PluginCategory.MEDIA, "Twitch Streams und Follows."),
    ("Kodi", PluginCategory.MEDIA, "Kodi Mediacenter."),
    ("Emby", PluginCategory.MEDIA, "Emby Medienserver."),
    ("Tautulli", PluginCategory.MEDIA, "Plex-Statistiken."),
    ("Overseerr / Jellyseerr", PluginCategory.MEDIA, "Medien-Anfragen."),
    ("Prowlarr", PluginCategory.MEDIA, "Indexer-Manager."),
    ("Lidarr", PluginCategory.MEDIA, "Musik-Sammlung."),
    ("Navidrome", PluginCategory.MEDIA, "Subsonic-Musikserver."),
    ("Pocket", PluginCategory.MEDIA, "Spaeter-lesen-Liste."),
    ("Feedly", PluginCategory.MEDIA, "RSS-Reader."),
    ("Reddit", PluginCategory.MEDIA, "Reddit Feeds und Posts."),
    ("Bluesky", PluginCategory.MEDIA, "Bluesky Posts und Feeds."),
    ("Trakt", PluginCategory.MEDIA, "Serien-/Film-Tracking."),
    ("TMDB", PluginCategory.MEDIA, "Film-/Seriendaten."),
    ("Anthropic Claude", PluginCategory.INFRA_AI, "Claude API und Nutzung."),
    ("Google Gemini", PluginCategory.INFRA_AI, "Gemini API und Nutzung."),
    ("Mistral AI", PluginCategory.INFRA_AI, "Mistral API und Modelle."),
    ("Hugging Face", PluginCategory.INFRA_AI, "Hugging Face Inference und Modelle."),
    ("OpenRouter", PluginCategory.INFRA_AI, "LLM-Router API."),
    ("Groq", PluginCategory.INFRA_AI, "Groq Inference API."),
    ("Perplexity", PluginCategory.INFRA_AI, "Perplexity AI Search."),
    ("Hetzner Cloud", PluginCategory.INFRA_AI, "Hetzner Cloud Server und Volumes."),
    ("DigitalOcean", PluginCategory.INFRA_AI, "DigitalOcean Droplets und Apps."),
    ("Syncthing", PluginCategory.INFRA_AI, "Syncthing Sync-Status."),
    ("Traefik", PluginCategory.INFRA_AI, "Traefik Proxy-Status."),
    ("Nginx Proxy Manager", PluginCategory.INFRA_AI, "Proxy Hosts und Zertifikate."),
    ("Authentik", PluginCategory.INFRA_AI, "Authentik Identity Provider."),
    ("Keycloak", PluginCategory.INFRA_AI, "Keycloak Identity Provider."),
    ("WireGuard", PluginCategory.INFRA_AI, "WireGuard VPN-Status."),
    ("Prometheus", PluginCategory.INFRA_AI, "Prometheus Metriken."),
    ("InfluxDB", PluginCategory.INFRA_AI, "InfluxDB Zeitreihen."),
    ("Zabbix", PluginCategory.INFRA_AI, "Zabbix Monitoring."),
    ("Sentry", PluginCategory.INFRA_AI, "Sentry Issues und Releases."),
    ("Plausible Analytics", PluginCategory.INFRA_AI, "Plausible Web-Analytics."),
    ("Umami", PluginCategory.INFRA_AI, "Umami Web-Analytics."),
    ("Speedtest Tracker", PluginCategory.INFRA_AI, "Internet-Speedtest-Verlauf."),
    ("Scrutiny", PluginCategory.INFRA_AI, "SMART-Disk-Status."),
    ("Watchtower", PluginCategory.INFRA_AI, "Container-Update-Status."),
    ("Beszel", PluginCategory.INFRA_AI, "Server-Monitoring."),
    ("Threema", PluginCategory.COMMS, "Threema Messenger."),
    ("Google Chat", PluginCategory.COMMS, "Google Workspace Chat."),
    ("Rocket.Chat", PluginCategory.COMMS, "Selbstgehosteter Team-Chat."),
    ("Mattermost", PluginCategory.COMMS, "Mattermost Team-Chat."),
    ("Zulip", PluginCategory.COMMS, "Threaded Team-Chat."),
    ("Fastmail", PluginCategory.COMMS, "Fastmail JMAP/Mail."),
    ("Mailgun", PluginCategory.COMMS, "Transaktionale Mails."),
    ("Twilio", PluginCategory.COMMS, "SMS und Telefonie."),
    ("Pushbullet", PluginCategory.COMMS, "Geraeteuebergreifende Pushes."),
    ("Zoom", PluginCategory.COMMS, "Meetings und Aufzeichnungen."),
    ("Jitsi Meet", PluginCategory.COMMS, "Jitsi Meetings."),
    ("Nextcloud Talk", PluginCategory.COMMS, "Nextcloud Chat und Calls."),
    ("Garmin Connect", PluginCategory.COMMS, "Fitness und Aktivitaet."),
    ("Fitbit", PluginCategory.COMMS, "Schritte und Schlaf."),
    ("Strava", PluginCategory.COMMS, "Sport-Aktivitaeten."),
    ("Withings", PluginCategory.COMMS, "Waage und Gesundheitsdaten."),
    ("Oura Ring", PluginCategory.COMMS, "Schlaf und Readiness."),
    ("Whoop", PluginCategory.COMMS, "Recovery und Strain."),
]


for _name, _category, _description in _SPECS:
    _plugin_slug = _slug(_name)
    globals()[_class_name(_plugin_slug)] = type(
        _class_name(_plugin_slug),
        (NativeHttpCatalogPlugin,),
        {
            "slug": _plugin_slug,
            "name": _name,
            "description": _description + " Direkte REST/Webhook-Anbindung.",
            "category": _category,
            "icon": "🔌",
        },
    )
