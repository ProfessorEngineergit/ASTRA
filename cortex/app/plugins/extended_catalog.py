"""Extended integration catalog — a large, data-driven list of known services.

These are NOT native plugins (no code/tools yet); they populate the catalog so the
UI shows the full breadth of what ASTRA *can* integrate, each tagged "Katalog".
Ask ASTRA to implement any of them and it becomes a real plugin in builtin/.

Each entry: (name, simpleicons-brand-or-None, short German description).
The brand slug drives the card logo (falls back to the category emoji on 404).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .base import CATEGORY_EMOJI, PluginCategory as C


@dataclass(frozen=True)
class CatalogEntry:
    slug: str
    name: str
    category: C
    brand: str | None
    description: str
    icon: str  # emoji fallback


def _slug(name: str) -> str:
    s = name.lower().replace("+", " plus ").replace("&", " und ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return f"cat_{s}"


# (name, brand, description) grouped by category
_RAW: dict[C, list[tuple[str, str | None, str]]] = {
    C.TRANSPORT: [
        ("MVG München", "mvg", "Münchner Verkehr – Abfahrten & Störungen"),
        ("MVV München", None, "Münchner Verkehrs- und Tarifverbund"),
        ("VBB Berlin-Brandenburg", None, "Verbund Berlin-Brandenburg"),
        ("VVS Stuttgart", None, "Verkehrs- und Tarifverbund Stuttgart"),
        ("VRR Rhein-Ruhr", None, "Verkehrsverbund Rhein-Ruhr"),
        ("VRS Köln/Bonn", None, "Verkehrsverbund Rhein-Sieg"),
        ("VGN Nürnberg", None, "Verkehrsverbund Großraum Nürnberg"),
        ("RMV Rhein-Main", None, "Bereits nativ – Rhein-Main-Verkehrsverbund"),
        ("MDV Mitteldeutschland", None, "Mitteldeutscher Verkehrsverbund"),
        ("ÖBB Österreich", None, "Österreichische Bundesbahnen"),
        ("SBB Schweiz", "sbb", "Schweizerische Bundesbahnen"),
        ("NS Niederlande", None, "Nederlandse Spoorwegen"),
        ("SNCF Frankreich", None, "Französische Bahn – TGV & TER"),
        ("Trenitalia", "trenitalia", "Italienische Bahn"),
        ("Renfe Spanien", "renfe", "Spanische Bahn – AVE"),
        ("FlixBus / FlixTrain", "flixbus", "Fernbus & -bahn in Europa"),
        ("Transport for London", "tfl", "U-Bahn, Bus & Status in London"),
        ("Citymapper", "citymapper", "Multimodale Routenplanung"),
        ("Moovit", "moovit", "ÖPNV-Routing weltweit"),
        ("Uber", "uber", "Fahrten buchen & verfolgen"),
        ("Bolt", "bolt", "Fahrten & E-Scooter"),
        ("FreeNow", None, "Taxi & Mobilität"),
        ("Nextbike", None, "Stadtrad-Verleih"),
        ("TIER / Dott", None, "E-Scooter-Sharing"),
        ("ADAC Verkehr", None, "Staus, Baustellen, Blitzer"),
        ("Flightradar24", "flightradar24", "Flugverfolgung in Echtzeit"),
    ],
    C.SMART_HOME: [
        ("IKEA Dirigera/Trådfri", "ikea", "IKEA Smart-Home-Hub"),
        ("Matter / Thread", None, "Hersteller­übergreifender Standard"),
        ("Tuya / SmartLife", None, "Tuya-basierte Geräte"),
        ("SwitchBot", None, "Bots, Vorhänge, Sensoren"),
        ("Govee", None, "LED & Sensoren (Cloud)"),
        ("Nanoleaf", "nanoleaf", "Licht-Panels & Lines"),
        ("LIFX", "lifx", "WLAN-Lampen"),
        ("Sonos", "sonos", "Multiroom-Audio"),
        ("Yeelight", "yeelight", "Xiaomi-Lampen"),
        ("Aqara", None, "Zigbee-Sensoren & Hubs"),
        ("Bosch Smart Home", "bosch", "Bosch-Hub & Geräte"),
        ("AVM FRITZ!Box", None, "Router, DECT, Smart-Home"),
        ("Homematic IP", None, "eQ-3 Smart-Home"),
        ("Nuki", None, "Smartes Türschloss"),
        ("Netatmo", None, "Wetter & Kameras"),
        ("Roborock", None, "Saugroboter"),
        ("Tado°", None, "Smarte Heizungssteuerung"),
        ("Samsung SmartThings", "samsung", "SmartThings-Hub"),
        ("Google Home / Nest", "googlehome", "Nest-Geräte & Routinen"),
        ("Amazon Alexa", "amazonalexa", "Echo & Routinen"),
        ("Reolink", None, "Überwachungskameras"),
        ("UniFi Protect", "ubiquiti", "Ubiquiti-Kameras"),
        ("Frigate NVR", None, "KI-Objekterkennung (NVR)"),
        ("ESPHome", "espressif", "DIY-Sensoren auf ESP"),
    ],
    C.SCHOOL: [
        ("WebUntis", None, "Stundenplan & Vertretung"),
        ("Schulmanager Online", None, "Schulorganisation"),
        ("Microsoft Teams (Edu)", "microsoftteams", "Klassen & Aufgaben"),
        ("Google Classroom", "googleclassroom", "Kurse & Aufgaben"),
        ("itslearning", None, "Lernplattform"),
        ("Sdui", None, "Schul-Messenger & Pläne"),
        ("ANTON", None, "Lern-App"),
        ("Sofatutor", None, "Lernvideos"),
        ("Kahoot!", "kahoot", "Quiz-Lernen"),
        ("Quizlet", "quizlet", "Karteikarten"),
        ("Duolingo", "duolingo", "Sprachen lernen"),
        ("Khan Academy", "khanacademy", "Kostenlose Kurse"),
        ("Moodle", "moodle", "Bereits nativ – LMS"),
        ("mebis Bayern", None, "Bayerische Lernplattform"),
        ("LernSax", None, "Sächsische Lernplattform"),
    ],
    C.PRODUCTIVITY: [
        ("Microsoft 365", "microsoftoffice", "Word, Excel, Outlook & Co."),
        ("Microsoft Outlook", "microsoftoutlook", "Mail & Kalender"),
        ("Microsoft To Do", None, "Aufgabenlisten"),
        ("Google Drive", "googledrive", "Dateien & Docs"),
        ("Dropbox", "dropbox", "Cloud-Speicher"),
        ("OneDrive", "microsoftonedrive", "Cloud-Speicher"),
        ("Apple Reminders", "apple", "Erinnerungen (iCloud)"),
        ("Apple Notes", "apple", "Notizen (iCloud)"),
        ("TickTick", "ticktick", "Aufgaben & Gewohnheiten"),
        ("Things 3", None, "GTD-Aufgaben (Apple)"),
        ("Asana", "asana", "Projektmanagement"),
        ("ClickUp", "clickup", "Projektmanagement"),
        ("Airtable", "airtable", "Datenbank-Tabellen"),
        ("Confluence", "confluence", "Team-Wiki"),
        ("Zapier", "zapier", "Automatisierungen"),
        ("IFTTT", "ifttt", "Wenn-Dann-Automationen"),
        ("Make (Integromat)", "make", "Visuelle Automationen"),
        ("Cal.com", "caldotcom", "Terminbuchung"),
        ("Joplin", "joplin", "Open-Source-Notizen"),
        ("Logseq", "logseq", "Vernetzte Notizen"),
        ("Stripe", "stripe", "Zahlungen (Read-only)"),
        ("PayPal", "paypal", "Kontostand & Transaktionen"),
        ("Wise", "wise", "Multi-Währungskonto"),
        ("Actual Budget", None, "Open-Source-Haushaltsbuch"),
        ("Firefly III", None, "Selbstgehostetes Finanztool"),
        ("Splitwise", None, "Ausgaben teilen"),
        ("Grocy", None, "Vorrats- & Haushaltsverwaltung"),
        ("Habitica", None, "Gewohnheiten als RPG"),
    ],
    C.MEDIA: [
        ("Apple Music", "applemusic", "Streaming & Mediathek"),
        ("Apple Podcasts", "applepodcasts", "Podcast-Abos"),
        ("Deezer", "deezer", "Musik-Streaming"),
        ("Tidal", "tidal", "HiFi-Streaming"),
        ("SoundCloud", "soundcloud", "Musik & Tracks"),
        ("YouTube Music", "youtubemusic", "Musik-Streaming"),
        ("Twitch", "twitch", "Live-Streams & Follows"),
        ("Netflix", "netflix", "Watchlist & Verlauf"),
        ("Kodi", "kodi", "Mediacenter-Steuerung"),
        ("Emby", "emby", "Medienserver"),
        ("Tautulli", None, "Plex-Statistiken"),
        ("Overseerr / Jellyseerr", "overseerr", "Medien-Anfragen"),
        ("Prowlarr", None, "Indexer-Manager"),
        ("Lidarr", None, "Musik-Sammlung verwalten"),
        ("Navidrome", None, "Subsonic-Musikserver"),
        ("AntennaPod", "antennapod", "Open-Source-Podcasts"),
        ("Pocket", "pocket", "Später lesen"),
        ("Feedly", "feedly", "RSS-Reader"),
        ("Reddit", "reddit", "Feeds & Benachrichtigungen"),
        ("Bluesky", "bluesky", "Posts veröffentlichen"),
        ("Letterboxd", "letterboxd", "Film-Tagebuch"),
        ("Trakt", "trakt", "Serien-/Film-Tracking"),
        ("TMDB", "themoviedatabase", "Film-/Seriendaten"),
        ("Goodreads", "goodreads", "Bücher-Tracking"),
        ("Komga", None, "Comic-/Manga-Server"),
        ("Audible", "audible", "Hörbücher"),
    ],
    C.INFRA_AI: [
        ("OpenAI", "openai", "GPT-Modelle & Nutzung"),
        ("Anthropic Claude", "anthropic", "Claude-Modelle"),
        ("Google Gemini", "googlegemini", "Gemini-Modelle"),
        ("Mistral AI", "mistralai", "Offene Modelle"),
        ("Hugging Face", "huggingface", "Modelle & Inferenz"),
        ("OpenRouter", None, "Viele LLMs über eine API"),
        ("Groq", None, "Sehr schnelle Inferenz"),
        ("Perplexity", "perplexity", "KI-Suche"),
        ("GitHub Actions", "githubactions", "CI/CD-Status"),
        ("AWS", "amazonaws", "Cloud-Ressourcen"),
        ("Hetzner Cloud", None, "Server & Volumes"),
        ("DigitalOcean", "digitalocean", "Droplets & Apps"),
        ("Syncthing", "syncthing", "P2P-Dateisync"),
        ("Traefik", "traefikproxy", "Reverse Proxy"),
        ("Nginx Proxy Manager", "nginxproxymanager", "Proxy & Zertifikate"),
        ("Authentik", "authentik", "SSO / Identity"),
        ("Keycloak", "keycloak", "Identity-Provider"),
        ("WireGuard", "wireguard", "VPN-Status"),
        ("Prometheus", "prometheus", "Metriken"),
        ("InfluxDB", "influxdb", "Zeitreihen-DB"),
        ("Zabbix", "zabbix", "Monitoring"),
        ("Sentry", "sentry", "Fehler-Tracking"),
        ("Plausible Analytics", "plausibleanalytics", "Datenschutz-Web-Analytics"),
        ("Umami", "umami", "Web-Analytics"),
        ("Speedtest Tracker", None, "Internet-Geschwindigkeit"),
        ("Scrutiny", None, "Festplatten-SMART-Status"),
        ("Watchtower", None, "Auto-Updates für Container"),
        ("Beszel", None, "Leichtgewichtiges Server-Monitoring"),
    ],
    C.COMMS: [
        ("Signal", "signal", "Bereits als Kanal – Messenger"),
        ("WhatsApp", "whatsapp", "Bereits via WAHA – Messenger"),
        ("iMessage", "imessage", "Apple-Nachrichten (Bridge)"),
        ("Threema", "threema", "Sicherer Messenger"),
        ("Facebook Messenger", "messenger", "Meta-Messenger"),
        ("Instagram DM", "instagram", "Direktnachrichten"),
        ("Google Chat", "googlechat", "Workspace-Chat"),
        ("Rocket.Chat", "rocketdotchat", "Selbstgehosteter Chat"),
        ("Mattermost", "mattermost", "Team-Chat"),
        ("Zulip", "zulip", "Threaded Team-Chat"),
        ("iCloud Mail", "icloud", "Apple-Mail (IMAP/SMTP)"),
        ("ProtonMail", "protonmail", "Verschlüsselte Mail (Bridge)"),
        ("Fastmail", "fastmail", "Mail & Kalender (JMAP)"),
        ("Mailgun", "mailgun", "Transaktionale Mails"),
        ("Twilio", "twilio", "SMS & Anrufe"),
        ("Pushbullet", "pushbullet", "Geräteübergreifende Pushes"),
        ("Zoom", "zoom", "Meetings"),
        ("Google Meet", "googlemeet", "Videoanrufe"),
        ("Jitsi Meet", "jitsi", "Open-Source-Video"),
        ("Nextcloud Talk", "nextcloud", "Chat & Anrufe"),
        ("Apple Health", "apple", "Aktivität & Vitalwerte (Bridge)"),
        ("Apple Fitness", "apple", "Workouts & Ringe (Bridge)"),
        ("Amazfit / Zepp", None, "Helio-Strap & Wearables (Zepp)"),
        ("Garmin Connect", "garmin", "Fitness & Aktivität"),
        ("Fitbit", "fitbit", "Schritte & Schlaf"),
        ("Strava", "strava", "Sport-Aktivitäten"),
        ("Withings", "withings", "Waage & Gesundheit"),
        ("Oura Ring", "ouraring", "Schlaf & Readiness"),
        ("Whoop", "whoop", "Recovery & Strain"),
    ],
}


def all_entries() -> list[CatalogEntry]:
    out: list[CatalogEntry] = []
    for cat, items in _RAW.items():
        for name, brand, desc in items:
            out.append(CatalogEntry(_slug(name), name, cat, brand, desc, CATEGORY_EMOJI[cat]))
    return out


def count() -> int:
    return sum(len(v) for v in _RAW.values())
