"""RSS/Atom feed reader — digest of headlines."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.rss")

_DEFAULT_FEED = "https://www.tagesschau.de/index~rss2.xml"


def _parse_feed(xml_text: str, max_items: int) -> list[dict]:
    """Parse RSS 2.0 or Atom feed, return list of {title, link}."""
    root = ET.fromstring(xml_text)
    ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
    items = []

    # RSS 2.0
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if title:
            items.append({"title": title, "link": link})
        if len(items) >= max_items:
            break

    # Atom (if no RSS items found)
    if not items:
        atom_ns = "http://www.w3.org/2005/Atom"
        for entry in root.iter(f"{{{atom_ns}}}entry"):
            title_el = entry.find(f"{{{atom_ns}}}title")
            link_el = entry.find(f"{{{atom_ns}}}link")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.get("href", "") if link_el is not None else ""
            if title:
                items.append({"title": title, "link": link})
            if len(items) >= max_items:
                break

    return items


class RssNewsPlugin(Plugin):
    slug = "rss"
    name = "RSS/News Digest"
    description = "Schlagzeilen aus RSS/Atom-Feeds lesen und zusammenfassen."
    category = PluginCategory.MEDIA
    icon = "📰"
    config_fields = [
        ConfigField("feeds", "Feed-URLs", type=FieldType.TEXT,
                    default=_DEFAULT_FEED,
                    help="Kommagetrennte Feed-URLs"),
        ConfigField("max_items", "Max. Artikel", type=FieldType.NUMBER,
                    default=5,
                    help="Wie viele Artikel pro Feed anzeigen"),
    ]

    def _feed_list(self) -> list[str]:
        raw = self.get("feeds") or _DEFAULT_FEED
        return [u.strip() for u in str(raw).split(",") if u.strip()]

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        feeds = self._feed_list()
        if not feeds:
            return HealthStatus.error("Keine Feed-URLs konfiguriert.")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(feeds[0])
                r.raise_for_status()
            if "<rss" in r.text or "<feed" in r.text:
                return HealthStatus.ok(f"Feed erreichbar: {feeds[0]}")
            return HealthStatus.error("Antwort scheint kein Feed zu sein.")
        except Exception as e:
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        feeds = self._feed_list()
        if not feeds:
            return None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(feeds[0])
                r.raise_for_status()
            items = _parse_feed(r.text, 3)
            if not items:
                return None
            lines = ["📰 News:"] + [f"- {i['title']}" for i in items]
            return "\n".join(lines)
        except Exception as e:
            log.warning("RSS briefing failed: %s", e)
            return None

    def tools(self) -> list[Tool]:
        async def _digest(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            max_items = int(self.get("max_items") or 5)
            feed_url = args.get("feed_url")
            feeds = [feed_url] if feed_url else self._feed_list()
            if not feeds:
                return "Keine Feeds konfiguriert."
            results = []
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                for url in feeds[:5]:
                    try:
                        r = await c.get(url)
                        r.raise_for_status()
                        items = _parse_feed(r.text, max_items)
                        host = urlparse(url).netloc
                        results.append(f"**{host}**")
                        for item in items:
                            results.append(f"- {item['title']}")
                            if item["link"]:
                                results.append(f"  {item['link']}")
                    except Exception as e:
                        results.append(f"[{url}] Fehler: {e}")
            return "\n".join(results) if results else "Keine Artikel gefunden."

        return [Tool(
            name="rss_digest",
            description="Schlagzeilen aus konfigurierten RSS/Atom-Feeds abrufen.",
            parameters={"type": "object", "properties": {
                "feed_url": {"type": "string",
                             "description": "Optionale Feed-URL — leer = alle konfigurierten Feeds"},
            }},
            handler=_digest, owner_only=True, source=self.slug,
        )]
