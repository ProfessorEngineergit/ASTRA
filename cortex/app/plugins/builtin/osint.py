"""OSINT & Recherche — offene Quellen, öffentliche Webcams, Ausgang über Tor.

Der zweite Pfeiler neben dem Sekretär. Alles hier ist **owner-only** und
standardmäßig **aus**; der Verkehr verlässt das Haus ausschließlich über den
Tor-Container (siehe docker-compose, Profil `research`).

Was dieses Plugin tut — und was ausdrücklich nicht:
  ✓ öffentlich zugängliche Quellen durchsuchen und lesen
  ✓ **öffentliche** Webcam-Verzeichnisse abfragen (Windy-Webcams-API)
  ✓ Bahrians eigene Herkunft beim Recherchieren verschleiern
  ✗ keine privaten/passwortgeschützten Kameras oder Systeme
  ✗ kein Umgehen von Authentifizierung, Paywalls oder Bot-Schutz
  ✗ kein Zusammentragen von Daten über einzelne Privatpersonen
Die Ziel-Prüfung (`netguard`) erzwingt zusätzlich, dass nie nach innen gezeigt wird
— weder ins Heim-LAN noch auf Container dieses Stacks.

„Untrackable" ist best effort: Tor verschleiert die Herkunft, garantiert aber keine
Anonymität. Das steht auch so in der Statusmeldung.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ... import netguard
from ...config import get_settings
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.osint")

_TOR_CHECK = "https://check.torproject.org/api/ip"
_WINDY_WEBCAMS = "https://api.windy.com/webcams/api/v3/webcams"


class OsintPlugin(Plugin):
    slug = "osint"
    name = "OSINT & Recherche"
    description = "Offene Quellen und öffentliche Webcams — Ausgang über Tor."
    category = PluginCategory.INFRA_AI
    icon = "🔎"
    config_fields = [
        # BEWUSST ohne Default: ein Pflichtfeld mit Vorgabewert gilt dem
        # ConfigStore als „erfüllt" und würde das Plugin von selbst aktivieren.
        # Recherche muss man einschalten, nicht ausschalten müssen.
        ConfigField("tor_proxy", "Tor SOCKS5-Proxy", required=True,
                    env_fallback="tor_proxy_url",
                    help="Trage socks5://tor:9050 ein (Container aus dem 'research'-Profil)"),
        ConfigField("searx_url", "SearXNG-Instanz", required=False,
                    help="Selbstgehostete Meta-Suche, z. B. http://searxng:8080"),
        ConfigField("windy_key", "Windy-Webcams API-Key", FieldType.PASSWORD,
                    required=False, secret=True,
                    help="Kostenlos auf windy.com — für öffentliche Webcams"),
        ConfigField("browser_url", "Browser-Container", required=False,
                    default="http://browser:3000", env_fallback="browser_ws_url"),
        ConfigField("timeout", "Timeout (Sekunden)", FieldType.NUMBER, default=45),
    ]

    def _proxy(self) -> str | None:
        return str(self.get("tor_proxy") or "").strip() or None

    def _client(self) -> httpx.AsyncClient:
        """HTTP-Client, dessen Verkehr durch Tor läuft."""
        proxy = self._proxy()
        kwargs: dict[str, Any] = {"timeout": float(self.get("timeout") or 45),
                                  "follow_redirects": True,
                                  "headers": {"User-Agent": "Mozilla/5.0"}}
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get(_TOR_CHECK)
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(
                f"Tor nicht erreichbar ({e}). Läuft der Stack mit "
                "'docker compose --profile research up -d'?")
        if data.get("IsTor"):
            return HealthStatus.ok(
                f"Ausgang über Tor (Exit-IP {data.get('IP', '?')}). "
                "Hinweis: verschleiert die Herkunft, garantiert keine Anonymität.")
        return HealthStatus.error(
            f"Verkehr läuft NICHT über Tor (IP {data.get('IP', '?')}) — "
            "ich recherchiere so nicht.")

    # ── Abrufe ───────────────────────────────────────────────────────────────
    async def fetch(self, url: str) -> dict:
        """Eine öffentliche Seite über Tor holen (Text). Prüft das Ziel vorher."""
        ok, reason = netguard.check_url(url)
        if not ok:
            return {"ok": False, "text": "", "reason": reason}
        try:
            async with self._client() as c:
                r = await c.get(url)
                r.raise_for_status()
                return {"ok": True, "text": r.text, "status": r.status_code,
                        "content_type": r.headers.get("content-type", "")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": "", "reason": str(e)}

    def tools(self) -> list[Tool]:
        async def _search(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            query = str(args.get("query") or "").strip()
            if not query:
                return tool_result(ok=False, source=self.slug, summary="Keine Suchanfrage.")
            searx = str(self.get("searx_url") or "").strip()
            if not searx:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Keine Suchmaschine konfiguriert. Trage eine SearXNG-Instanz "
                            "im Feld 'searx_url' ein — oder nutze osint_fetch mit einer "
                            "konkreten URL.")
            url = f"{searx.rstrip('/')}/search"
            try:
                async with self._client() as c:
                    r = await c.get(url, params={"q": query, "format": "json"})
                    r.raise_for_status()
                    results = (r.json().get("results") or [])[:8]
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Suche fehlgeschlagen: {e}")
            if not results:
                return tool_result(ok=True, source=self.slug, summary="Keine Treffer.")
            lines = [f"• {x.get('title', '')} — {x.get('url', '')}\n  {x.get('content', '')[:160]}"
                     for x in results]
            return tool_result(ok=True, source=self.slug,
                               summary="Treffer (über Tor):\n" + "\n".join(lines),
                               data={"results": results})

        async def _fetch(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            url = str(args.get("url") or "").strip()
            res = await self.fetch(url)
            if not res["ok"]:
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Nicht geholt: {res['reason']}")
            text = res["text"]
            if "html" in (res.get("content_type") or ""):
                import re as _re
                text = _re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                               flags=_re.S | _re.I)
                text = _re.sub(r"<[^>]+>", " ", text)
                text = " ".join(text.split())
            return tool_result(ok=True, source=self.slug,
                               summary=text[:3000],
                               data={"url": url, "chars": len(res["text"])})

        async def _webcams(args: dict, ctx: ToolContext) -> str:
            """Public webcam directory. Explicitly public feeds only."""
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            key = str(self.get("windy_key") or "").strip()
            if not key:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Kein Windy-API-Key hinterlegt — ohne den kann ich keine "
                            "öffentlichen Webcams abfragen.")
            where = str(args.get("where") or "").strip()
            params: dict[str, Any] = {"limit": 8, "include": "location,urls,player"}
            if lat := args.get("lat"):
                params["nearby"] = f"{lat},{args.get('lon')},{args.get('radius', 50)}"
            try:
                async with self._client() as c:
                    r = await c.get(_WINDY_WEBCAMS, params=params,
                                    headers={"x-windy-api-key": key})
                    r.raise_for_status()
                    cams = (r.json().get("webcams") or [])[:8]
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Webcam-Abfrage fehlgeschlagen: {e}")
            if not cams:
                return tool_result(ok=True, source=self.slug,
                                   summary=f"Keine öffentlichen Webcams gefunden"
                                           + (f" für {where}." if where else "."))
            lines = []
            for cam in cams:
                loc = (cam.get("location") or {})
                link = ((cam.get("urls") or {}).get("detail")
                        or (cam.get("player") or {}).get("live", ""))
                lines.append(f"• {cam.get('title', '?')} ({loc.get('city', '')}, "
                             f"{loc.get('country', '')}) {link}")
            return tool_result(ok=True, source=self.slug,
                               summary="Öffentliche Webcams:\n" + "\n".join(lines),
                               data={"webcams": cams})

        async def _exit_ip(args: dict, ctx: ToolContext) -> str:
            hs = await self.health_check()
            return tool_result(ok=hs.state.value == "ok", source=self.slug, summary=hs.message)

        return [
            Tool(name="osint_search",
                 description="Durchsuche offene Quellen über Tor (Meta-Suche). "
                             "Nur öffentlich zugängliche Inhalte.",
                 parameters={"type": "object", "properties": {"query": {"type": "string"}},
                             "required": ["query"]},
                 handler=_search, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"]),
            Tool(name="osint_fetch",
                 description="Hole eine öffentliche Seite über Tor und gib den Text zurück. "
                             "Interne/private Adressen werden abgelehnt.",
                 parameters={"type": "object", "properties": {"url": {"type": "string"}},
                             "required": ["url"]},
                 handler=_fetch, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"]),
            Tool(name="osint_webcams",
                 description="Finde ÖFFENTLICHE Webcams (Windy-Verzeichnis) — per Ort oder "
                             "Koordinaten. Nur frei zugängliche Kameras, keine privaten.",
                 parameters={"type": "object", "properties": {
                     "where": {"type": "string"},
                     "lat": {"type": "number"}, "lon": {"type": "number"},
                     "radius": {"type": "number", "description": "km, Standard 50"}}},
                 handler=_webcams, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"]),
            Tool(name="osint_exit_ip",
                 description="Prüfe, ob die Recherche wirklich über Tor läuft, und zeige die "
                             "Exit-IP.",
                 parameters={"type": "object", "properties": {}},
                 handler=_exit_ip, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status"]),
        ]
