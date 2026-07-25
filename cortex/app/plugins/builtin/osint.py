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
        # Recon (Selbst-Audit / autorisierte Netze). Aktives Scannen NUR auf den
        # hier eingetragenen eigenen/freigegebenen Netzen — die Prüfung erzwingt es.
        ConfigField("scan_networks", "Freigegebene Scan-Netze (CIDR)", required=False,
                    help="Kommagetrennt, NUR eigene/autorisierte, z. B. 192.168.178.0/24"),
        ConfigField("shodan_key", "Shodan API-Key", FieldType.PASSWORD, required=False,
                    secret=True, help="Für Selbst-Exposition (öffentliche Daten über deine IP)"),
        ConfigField("hibp_key", "HaveIBeenPwned API-Key", FieldType.PASSWORD, required=False,
                    secret=True, help="Für Breach-Check deiner eigenen Mailadressen"),
        ConfigField("timeout", "Timeout (Sekunden)", FieldType.NUMBER, default=45),
    ]

    # Häufige „interessante" Ports für den Netz-Audit (Drucker, Kamera, Web, SSH …).
    _AUDIT_PORTS: dict[int, str] = {
        21: "FTP", 22: "SSH", 23: "Telnet", 80: "HTTP", 443: "HTTPS",
        445: "SMB", 554: "RTSP/Kamera", 631: "IPP/Drucker", 1883: "MQTT",
        3389: "RDP", 5000: "UPnP", 8080: "HTTP-alt", 8123: "Home Assistant",
        9000: "div.", 9100: "JetDirect/Drucker", 32400: "Plex",
    }

    def scan_networks(self) -> list[str]:
        return [c.strip() for c in str(self.get("scan_networks") or "").split(",") if c.strip()]

    async def _here(self) -> dict:
        """Aktueller Standort für 'in der Nähe' — vom Handy über die HA-Companion-App."""
        try:
            from ..registry import get_manager
            ha = get_manager().get("home_assistant")
            if ha and ha.enabled:
                return await ha.location()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "kein Home-Assistant-Standort verfügbar"}

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

    # ── Autorisierter Netz-Audit (nur eigene/freigegebene Netze) ─────────────
    async def _probe(self, host: str, port: int, timeout: float) -> bool:
        """Ein einzelner TCP-Connect. Kein Payload, kein Exploit — nur 'offen?'."""
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    async def scan(self, target: str, *, ports: list[int] | None = None,
                   max_hosts: int = 64) -> dict:
        """TCP-Connect-Audit eines freigegebenen Netzes. Prüft die Autorisierung."""
        import ipaddress
        ok, reason = netguard.scan_target_ok(target, self.scan_networks())
        if not ok:
            return {"ok": False, "reason": reason}
        ports = ports or list(self._AUDIT_PORTS)
        try:
            net = ipaddress.ip_network(target.strip(), strict=False)
            hosts = [str(net.network_address)] if net.num_addresses == 1 \
                else [str(h) for h in net.hosts()]
        except ValueError:
            return {"ok": False, "reason": "ungültiges Ziel"}
        if len(hosts) > max_hosts:
            return {"ok": False, "reason": f"Netz zu groß ({len(hosts)} Hosts > {max_hosts}). "
                                           "Nimm ein kleineres CIDR."}
        timeout = min(2.0, float(self.get("timeout") or 45) / 10)
        sem = asyncio.Semaphore(200)

        async def probe_one(host: str, port: int):
            async with sem:
                return host, port, await self._probe(host, port, timeout)

        results = await asyncio.gather(*(probe_one(h, p) for h in hosts for p in ports))
        found: dict[str, list[dict]] = {}
        for host, port, is_open in results:
            if is_open:
                found.setdefault(host, []).append(
                    {"port": port, "service": self._AUDIT_PORTS.get(port, "?")})
        return {"ok": True, "hosts_scanned": len(hosts), "ports": len(ports),
                "found": found}

    def tools(self) -> list[Tool]:
        async def _net_scan(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            target = str(args.get("target") or "").strip()
            nets = self.scan_networks()
            if not nets:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Kein Netz freigegeben. Trage im Feld 'scan_networks' NUR deine "
                            "eigenen/autorisierten Netze ein (z. B. 192.168.178.0/24). Aktives "
                            "Scannen fremder Netze mache ich nicht.")
            if not target:
                target = nets[0]
            res = await self.scan(target)
            if not res["ok"]:
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Nicht gescannt: {res['reason']}")
            found = res["found"]
            if not found:
                return tool_result(ok=True, source=self.slug,
                                   summary=f"{res['hosts_scanned']} Hosts geprüft — keine "
                                           "offenen Ports aus der Audit-Liste.")
            lines = []
            for host, ports in sorted(found.items()):
                pl = ", ".join(f"{p['port']}({p['service']})" for p in ports)
                lines.append(f"• {host}: {pl}")
            return tool_result(
                ok=True, source=self.slug,
                summary=f"Netz-Audit {target} — offene Ports auf {len(found)} Gerät(en):\n"
                        + "\n".join(lines),
                data={"target": target, "found": found})

        async def _self_exposure(args: dict, ctx: ToolContext) -> str:
            """Shodan-Blick auf die EIGENE (oder eine angegebene) öffentliche IP — liest
            nur bereits veröffentlichte Daten, scannt nichts."""
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            key = str(self.get("shodan_key") or "").strip()
            if not key:
                return tool_result(ok=False, source=self.slug,
                                   summary="Kein Shodan-Key hinterlegt.")
            ip = str(args.get("ip") or "").strip()
            try:
                async with self._client() as c:
                    if not ip:
                        rr = await c.get("https://api.ipify.org", params={"format": "json"})
                        ip = rr.json().get("ip", "")
                    r = await c.get(f"https://api.shodan.io/shodan/host/{ip}",
                                    params={"key": key})
                    if r.status_code == 404:
                        return tool_result(ok=True, source=self.slug,
                                           summary=f"{ip}: Shodan kennt keine offenen Dienste "
                                                   "(gut — nichts öffentlich indexiert).")
                    r.raise_for_status()
                    data = r.json()
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Shodan-Abfrage fehlgeschlagen: {e}")
            ports = data.get("ports") or []
            return tool_result(
                ok=True, source=self.slug,
                summary=f"Exposition {ip}: {len(ports)} Port(s) öffentlich sichtbar: "
                        f"{', '.join(str(p) for p in ports) or '—'}. "
                        f"Org: {data.get('org', '?')}",
                data={"ip": ip, "ports": ports, "hostnames": data.get("hostnames")})

        async def _breach_check(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            key = str(self.get("hibp_key") or "").strip()
            account = str(args.get("email") or "").strip()
            if not key:
                return tool_result(ok=False, source=self.slug, summary="Kein HIBP-Key hinterlegt.")
            if not account:
                return tool_result(ok=False, source=self.slug, summary="Keine Mailadresse angegeben.")
            try:
                async with self._client() as c:
                    r = await c.get(
                        f"https://haveibeenpwned.com/api/v3/breachedaccount/{account}",
                        headers={"hibp-api-key": key, "user-agent": "ASTRA"})
                    if r.status_code == 404:
                        return tool_result(ok=True, source=self.slug,
                                           summary=f"{account}: in keinem bekannten Leak. 👍")
                    r.raise_for_status()
                    breaches = [b.get("Name") for b in r.json()]
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug, summary=f"HIBP fehlgeschlagen: {e}")
            return tool_result(ok=True, source=self.slug,
                               summary=f"{account} in {len(breaches)} Leak(s): {', '.join(breaches)}",
                               data={"breaches": breaches})

        async def _dns_intel(args: dict, ctx: ToolContext) -> str:
            """DNS + Cert-Transparency (crt.sh) zu einer Domain — öffentliche Register."""
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            domain = str(args.get("domain") or "").strip().lower()
            if not domain or "." not in domain:
                return tool_result(ok=False, source=self.slug, summary="Gültige Domain angeben.")
            import socket as _socket
            out: dict[str, Any] = {"domain": domain}
            try:
                out["a"] = sorted({info[4][0]
                                   for info in _socket.getaddrinfo(domain, None)})
            except OSError as e:
                out["a"] = []
                out["dns_error"] = str(e)
            try:
                async with self._client() as c:
                    r = await c.get("https://crt.sh/", params={"q": domain, "output": "json"})
                    if r.status_code == 200:
                        subs = sorted({row.get("common_name", "") for row in r.json()})
                        out["subdomains"] = [s for s in subs if s][:40]
            except Exception:  # noqa: BLE001
                out["subdomains"] = []
            return tool_result(
                ok=True, source=self.slug,
                summary=f"{domain}: {', '.join(out.get('a') or []) or 'keine A-Records'} · "
                        f"{len(out.get('subdomains') or [])} Subdomains (crt.sh)",
                data=out)

        async def _image_exif(args: dict, ctx: ToolContext) -> str:
            """EXIF/Geodaten aus einem lokal hochgeladenen Bild (Uploads-Ordner)."""
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            try:
                from PIL import Image, ExifTags  # type: ignore
            except Exception:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary="Bild-Forensik braucht Pillow (Extra 'research').")
            from pathlib import Path
            name = str(args.get("file") or "").strip()
            path = Path(get_settings().brain_data_dir) / "uploads" / Path(name).name
            if not name or not path.exists():
                return tool_result(ok=False, source=self.slug,
                                   summary="Bild nicht gefunden (erst im Chat hochladen).")
            try:
                img = Image.open(path)
                raw = img._getexif() or {}
                exif = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug, summary=f"EXIF-Fehler: {e}")
            gps = exif.get("GPSInfo")
            interesting = {k: str(exif.get(k)) for k in
                           ("Make", "Model", "DateTimeOriginal", "Software") if exif.get(k)}
            return tool_result(
                ok=True, source=self.slug,
                summary=(f"{name}: {interesting.get('Make', '')} {interesting.get('Model', '')} · "
                         f"{interesting.get('DateTimeOriginal', 'kein Datum')}"
                         + (" · GPS enthalten!" if gps else " · kein GPS")),
                data={"exif": interesting, "has_gps": bool(gps)})

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
            lat, lon = args.get("lat"), args.get("lon")
            # "in der Nähe" ohne Koordinaten → Standort vom Handy über HA holen.
            if lat is None and not where:
                loc = await self._here()
                if loc.get("ok"):
                    lat, lon = loc["lat"], loc["lon"]
            params: dict[str, Any] = {"limit": 8, "include": "location,urls,player"}
            if lat is not None:
                params["nearby"] = f"{lat},{lon},{args.get('radius', 50)}"
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
            Tool(name="osint_net_scan",
                 description=(
                     "Netz-Audit: findet offene Ports (Drucker 9100/631, Kamera 554, SSH, "
                     "Web …) — NUR auf deinen eigenen/freigegebenen Netzen aus 'scan_networks'. "
                     "Fremde Netze lehnt die Prüfung ab. target=CIDR oder Host, leer=erstes "
                     "freigegebenes Netz."
                 ),
                 parameters={"type": "object", "properties": {"target": {"type": "string"}}},
                 handler=_net_scan, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"],
                 examples=["welche geräte in meinem netz haben offene ports"]),
            Tool(name="osint_self_exposure",
                 description="Zeigt, was von DIR im Internet sichtbar ist (Shodan über deine "
                             "öffentliche IP) — liest nur veröffentlichte Daten, scannt nichts.",
                 parameters={"type": "object", "properties": {
                     "ip": {"type": "string", "description": "optional, sonst eigene IP"}}},
                 handler=_self_exposure, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research", "status"]),
            Tool(name="osint_breach_check",
                 description="Prüft, ob eine (deiner) Mailadresse in bekannten Daten-Leaks "
                             "auftaucht (HaveIBeenPwned).",
                 parameters={"type": "object", "properties": {"email": {"type": "string"}},
                             "required": ["email"]},
                 handler=_breach_check, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"]),
            Tool(name="osint_dns",
                 description="DNS-Records + Subdomains (Cert-Transparency/crt.sh) zu einer "
                             "Domain — öffentliche Register.",
                 parameters={"type": "object", "properties": {"domain": {"type": "string"}},
                             "required": ["domain"]},
                 handler=_dns_intel, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"]),
            Tool(name="osint_image_exif",
                 description="EXIF-/Geodaten aus einem hochgeladenen Bild lesen (Kamera, "
                             "Zeitstempel, ggf. GPS).",
                 parameters={"type": "object", "properties": {"file": {"type": "string"}},
                             "required": ["file"]},
                 handler=_image_exif, owner_only=True, source=self.slug,
                 safety="private_read", intents=["research"]),
        ]
