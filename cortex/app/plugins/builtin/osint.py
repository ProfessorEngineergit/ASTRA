"""OSINT & Recherche — offene Quellen, öffentliche Webcams, Ausgang über Tor.

Der zweite Pfeiler neben dem Sekretär. Alles hier ist **owner-only** und
standardmäßig **aus**; der Verkehr verlässt das Haus ausschließlich über den
Tor-Container (Cortex startet ihn als Abhängigkeit).

Was dieses Plugin tut — und was ausdrücklich nicht:
  ✓ öffentlich zugängliche Quellen durchsuchen und lesen
  ✓ **öffentliche** Webcam-Verzeichnisse abfragen (Windy-Webcams-API)
  ✓ Bahrians eigene Herkunft beim Recherchieren verschleiern
  ✗ keine privaten/passwortgeschützten Kameras oder Systeme
  ✗ kein Umgehen von Authentifizierung, Paywalls oder Bot-Schutz
  ✗ kein Zusammentragen von Daten über einzelne Privatpersonen
Die Ziel-Prüfung (`netguard`) erzwingt zusätzlich, dass nie nach innen gezeigt wird
— weder ins Heim-LAN noch auf Container dieses Stacks.

Der Anonymitätsmodus arbeitet fail-closed: kein verifizierter Tor-Ausgang, keine
externe Recherche. Es gibt keinen stillen direkten Fallback.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import struct
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ... import db, netguard
from ...config import get_settings
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.osint")

_TOR_CHECK = "https://check.torproject.org/api/ip"
_WINDY_WEBCAMS = "https://api.windy.com/webcams/api/v3/webcams"
_TOR_VERIFY_TTL = 60.0


class OsintPlugin(Plugin):
    slug = "osint"
    name = "OSINT & Recherche"
    description = "Offene Quellen und öffentliche Webcams — Ausgang über Tor."
    category = PluginCategory.INFRA_AI
    icon = ""
    config_fields = [
        # BEWUSST ohne Default: ein Pflichtfeld mit Vorgabewert gilt dem
        # ConfigStore als „erfüllt" und würde das Plugin von selbst aktivieren.
        # Recherche muss man einschalten, nicht ausschalten müssen.
        ConfigField("tor_proxy", "Tor SOCKS5-Proxy", required=True,
                    env_fallback="tor_proxy_url",
                    help="Trage socks5://tor:9050 ein (Tor-Sidecar im Compose-Stack)"),
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
        ConfigField("scan_targets", "Freigegebene Pen-Test-Ziele (IP/CIDR)", required=False,
                    help="Kommagetrennt. Öffentliche Ziele nur mit ausdrücklicher Autorisierung; "
                         "z. B. 203.0.113.10 oder 198.51.100.0/24"),
        ConfigField("shodan_key", "Shodan API-Key", FieldType.PASSWORD, required=False,
                    secret=True, help="Für Selbst-Exposition (öffentliche Daten über deine IP)"),
        ConfigField("hibp_key", "HaveIBeenPwned API-Key", FieldType.PASSWORD, required=False,
                    secret=True, help="Für Breach-Check deiner eigenen Mailadressen"),
        ConfigField("timeout", "Timeout (Sekunden)", FieldType.NUMBER, default=20,
                    help="Maximal 25 Sekunden pro externer Anfrage."),
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

    def scan_targets(self) -> list[str]:
        return [c.strip() for c in str(self.get("scan_targets") or "").split(",") if c.strip()]

    def _scan_allowlist(self) -> list[str]:
        return self.scan_networks() + self.scan_targets()

    def _public_target_allowed(self, target: str) -> bool:
        """Public target is permitted only when it is inside the explicit target list."""
        try:
            target_net = ipaddress.ip_network(target.strip(), strict=False)
        except ValueError:
            return False
        for raw in self.scan_targets():
            try:
                allowed = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                continue
            if target_net.version == allowed.version and target_net.subnet_of(allowed):
                return True
        return False

    async def _here(self) -> dict:
        """Aktueller Standort für 'in der Nähe' — vom Handy über die HA-Companion-App."""
        try:
            from ..registry import get_manager
            ha = get_manager().get("home_assistant")
            if ha and ha.enabled:
                return await ha.location()
        except Exception:  # noqa: BLE001
            pass
        # Fallback für den Browser: der Recon-Tab speichert eine ausdrücklich
        # freigegebene Position in app_settings, damit Telegram/WhatsApp dieselbe
        # Position verwenden können. Kein stiller Standortzugriff.
        try:
            appset = await db.get_setting("app_settings", {}) or {}
            loc = appset.get("location") if isinstance(appset, dict) else None
            if isinstance(loc, dict) and loc.get("lat") is not None and loc.get("lon") is not None:
                return {"ok": True, "lat": float(loc["lat"]), "lon": float(loc["lon"]),
                        "source": "browser_saved"}
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "kein Home-Assistant-Standort verfügbar"}

    def _proxy(self) -> str | None:
        raw = str(self.get("tor_proxy") or "").strip()
        if not raw:
            return None
        try:
            parsed = urlparse(raw)
            port = parsed.port
        except ValueError:
            return None
        # Fail closed: OSINT accepts only an unauthenticated SOCKS5 endpoint.
        # HTTP proxies and URLs containing credentials are rejected.
        if (parsed.scheme != "socks5" or not parsed.hostname
                or not port or parsed.username or parsed.password):
            return None
        return raw

    def _request_timeout(self, requested: float | None = None, *, ceiling: float = 25.0) -> float:
        """Return a bounded timeout so a broken Tor sidecar cannot hang a request."""
        try:
            value = float(requested if requested is not None else (self.get("timeout") or 20))
        except (TypeError, ValueError):
            value = 20.0
        return max(3.0, min(float(ceiling), value))

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        """HTTP client with a mandatory SOCKS5 proxy and a hard timeout."""
        proxy = self._proxy()
        if not proxy:
            raise RuntimeError(
                "Tor-Kill-Switch: kein gültiger SOCKS5-Proxy konfiguriert. "
                "Direkte Verbindung verweigert.")
        kwargs: dict[str, Any] = {"timeout": self._request_timeout(timeout),
                                  "follow_redirects": True,
                                  "headers": {"User-Agent": "ASTRA-Recon/1.0"},
                                  "proxy": proxy}
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _safe_http_error(service: str, exc: Exception) -> str:
        """Return an actionable error without URLs, query strings or secrets."""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if service == "Shodan" and status == 403:
                return (
                    "Shodan verweigert die Suche (403). Der Tor-Ausgang kann blockiert "
                    "sein oder der API-Tarif erlaubt diese Suchfilter nicht. "
                    "Der Anonymitäts-Kill-Switch verhindert einen direkten Fallback.")
            if status == 401:
                return f"{service}: Zugangsdaten abgelehnt (401)."
            if status == 429:
                return f"{service}: Rate-Limit erreicht (429)."
            return f"{service} antwortet mit HTTP {status}."
        if isinstance(exc, httpx.TimeoutException):
            return f"{service} über Tor: Timeout."
        if isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
            return f"{service} über Tor nicht erreichbar. Tor-Sidecar prüfen."
        return f"{service} über Tor fehlgeschlagen ({type(exc).__name__})."

    async def _verify_tor(self, *, force: bool = False) -> tuple[bool, str]:
        """Verify the current egress and cache only successful checks briefly."""
        now = time.monotonic()
        if not force and now < float(getattr(self, "_tor_verified_until", 0.0)):
            exit_ip = str(getattr(self, "_tor_exit_ip", "?"))
            return True, f"Tor-Kill-Switch aktiv · Exit-IP {exit_ip}"
        try:
            async with self._client(8.0) as client:
                response = await client.get(_TOR_CHECK)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001
            return False, self._safe_http_error("Tor-Prüfung", exc)
        if not data.get("IsTor"):
            return False, "Tor-Prüfung fehlgeschlagen. Direkte Verbindung verweigert."
        self._tor_exit_ip = str(data.get("IP") or "?")
        self._tor_verified_until = now + _TOR_VERIFY_TTL
        return True, f"Tor-Kill-Switch aktiv · Exit-IP {self._tor_exit_ip}"

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        ok, message = await self._verify_tor(force=True)
        if ok:
            return HealthStatus.ok(
                f"{message}. Fail-closed: direkte Fallbacks und externe Browser-Links "
                "sind gesperrt.")
        return HealthStatus.error(
            f"{message} Starte 'docker compose up -d tor' und prüfe "
            "'docker compose ps tor'.")

    # ── Abrufe ───────────────────────────────────────────────────────────────
    async def fetch(self, url: str) -> dict:
        """Eine öffentliche Seite über Tor holen (Text). Prüft das Ziel vorher."""
        # Do not let the host resolver see research domains. Literal/internal
        # targets are still rejected here; hostname resolution happens at Tor.
        ok, reason = netguard.check_url(url, resolve=False)
        if not ok:
            return {"ok": False, "text": "", "reason": reason}
        tor_ok, tor_message = await self._verify_tor()
        if not tor_ok:
            return {"ok": False, "text": "", "reason": tor_message}
        try:
            async with self._client() as c:
                r = await c.get(url)
                r.raise_for_status()
                return {"ok": True, "text": r.text, "status": r.status_code,
                        "content_type": r.headers.get("content-type", "")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "",
                    "reason": self._safe_http_error("Abruf", exc)}

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

    async def _probe_via_tor(self, host: str, port: int, timeout: float) -> bool:
        """TCP connect through the configured Tor SOCKS5 proxy (no direct fallback)."""
        parsed = urlparse(self._proxy() or "")
        if not parsed.hostname:
            return False
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, parsed.port or 1080),
                timeout=timeout)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            if await asyncio.wait_for(reader.readexactly(2), timeout) != b"\x05\x00":
                return False
            ip = ipaddress.ip_address(host)
            if ip.version == 4:
                address = b"\x01" + ip.packed
            else:
                address = b"\x04" + ip.packed
            writer.write(b"\x05\x01\x00" + address + struct.pack("!H", port))
            await writer.drain()
            reply = await asyncio.wait_for(reader.readexactly(4), timeout)
            if reply[0] != 5 or reply[1] != 0:
                return False
            if reply[3] == 1:
                await asyncio.wait_for(reader.readexactly(4), timeout)
            elif reply[3] == 4:
                await asyncio.wait_for(reader.readexactly(16), timeout)
            elif reply[3] == 3:
                length = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
                await asyncio.wait_for(reader.readexactly(length), timeout)
            else:
                return False
            await asyncio.wait_for(reader.readexactly(2), timeout)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass

    async def scan(self, target: str, *, ports: list[int] | None = None,
                   max_hosts: int = 64) -> dict:
        """TCP-Connect-Audit eines freigegebenen Netzes. Prüft die Autorisierung."""
        public_target = self._public_target_allowed(target)
        ok, reason = netguard.scan_target_ok(
            target, self._scan_allowlist(), allow_public=public_target)
        if not ok:
            return {"ok": False, "reason": reason}
        if public_target:
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return {"ok": False, "reason": tor_message}
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
                probe = self._probe_via_tor if public_target else self._probe
                return host, port, await probe(host, port, timeout)

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
            targets = self._scan_allowlist()
            if not targets:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Kein Ziel freigegeben. Trage unter 'scan_networks' private Netze "
                            "oder unter 'scan_targets' ausdrücklich autorisierte IPs/CIDRs ein. "
                            "Beliebige Ziele werden nicht gescannt.")
            if not target:
                target = targets[0]
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
            if not ip:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Öffentliche IP ausdrücklich angeben. ASTRA ermittelt sie nicht "
                            "über eine direkte Verbindung; über Tor wäre nur die Exit-IP sichtbar.")
            try:
                parsed_ip = ipaddress.ip_address(ip)
                if not parsed_ip.is_global:
                    raise ValueError
            except ValueError:
                return tool_result(ok=False, source=self.slug,
                                   summary="Eine gültige öffentliche IP angeben.")
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                async with self._client() as c:
                    r = await c.get(f"https://api.shodan.io/shodan/host/{ip}",
                                    params={"key": key})
                    if r.status_code == 404:
                        return tool_result(ok=True, source=self.slug,
                                           summary=f"{ip}: Shodan kennt keine offenen Dienste "
                                                   "(gut — nichts öffentlich indexiert).")
                    r.raise_for_status()
                    data = r.json()
            except Exception as exc:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=self._safe_http_error("Shodan", exc))
            ports = data.get("ports") or []
            return tool_result(
                ok=True, source=self.slug,
                summary=f"Exposition {ip}: {len(ports)} Port(s) öffentlich sichtbar: "
                        f"{', '.join(str(p) for p in ports) or '—'}. "
                        f"Org: {data.get('org', '?')}",
                data={"ip": ip, "ports": ports, "hostnames": data.get("hostnames")})

        async def _nearby_exposure(args: dict, ctx: ToolContext) -> str:
            """Passive Shodan search around Bahrian's location.

            Returns metadata only. It never connects to the indexed device and
            deliberately does not expose camera/player/provider URLs.
            """
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            key = str(self.get("shodan_key") or "").strip()
            if not key:
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Kein Shodan-Key hinterlegt. Öffne Plugins → OSINT & Recherche.")
            category = str(args.get("category") or "cameras").strip().lower()
            if category not in {"cameras", "printers"}:
                return tool_result(ok=False, source=self.slug,
                                   summary="Kategorie muss cameras oder printers sein.")
            lat, lon = args.get("lat"), args.get("lon")
            if lat is None or lon is None:
                location = await self._here()
                if location.get("ok"):
                    lat, lon = location.get("lat"), location.get("lon")
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError):
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Kein Standort verfügbar. Im Recon-Tab Standort freigeben "
                            "oder Home Assistant verbinden.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return tool_result(ok=False, source=self.slug, summary="Ungültige Koordinaten.")
            radius = max(1, min(50, int(args.get("radius") or 15)))
            limit = max(1, min(20, int(args.get("limit") or 12)))
            signature = (
                "has_screenshot:true"
                if category == "cameras"
                else "port:9100,631"
            )
            # Do not disclose the browser's metre-accurate position to Shodan.
            # Two decimals are roughly kilometre precision at German latitudes.
            query_lat, query_lon = round(lat, 2), round(lon, 2)
            query = f"{signature} geo:{query_lat:.2f},{query_lon:.2f},{radius}"
            fields = "ip_str,port,product,org,hostnames,location,transport,timestamp"
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                async with self._client() as client:
                    response = await client.get(
                        "https://api.shodan.io/shodan/host/search",
                        params={"key": key, "query": query, "minify": "true", "fields": fields},
                    )
                    response.raise_for_status()
                    payload = response.json()
            except Exception as exc:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=self._safe_http_error("Shodan", exc))

            rows = []
            for match in (payload.get("matches") or [])[:limit]:
                ip = str(match.get("ip_str") or "").strip()
                if not ip:
                    continue
                location = match.get("location") if isinstance(match.get("location"), dict) else {}
                try:
                    dlat = math.radians(float(location.get("latitude")) - lat)
                    dlon = math.radians(float(location.get("longitude")) - lon)
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(math.radians(lat))
                         * math.cos(math.radians(float(location.get("latitude"))))
                         * math.sin(dlon / 2) ** 2)
                    distance = round(6371 * 2 * math.asin(min(1, math.sqrt(a))), 1)
                except (TypeError, ValueError):
                    distance = None
                rows.append({
                    "ip": ip,
                    "port": match.get("port"),
                    "transport": match.get("transport") or "tcp",
                    "product": match.get("product") or "",
                    "org": match.get("org") or "",
                    "city": location.get("city") or "",
                    "country": location.get("country_name") or "",
                    "distance_km": distance,
                    "updated": match.get("timestamp") or "",
                })
            label = "Kameras" if category == "cameras" else "Drucker"
            if not rows:
                return tool_result(
                    ok=True, source=self.slug,
                    summary=f"Keine von Shodan indexierten {label} im Radius von {radius} km.",
                    data={"category": category, "radius_km": radius, "results": []})
            lines = [
                f"• {row['product'] or label[:-1]} · {row['city'] or 'Ort unbekannt'}"
                + (f" · {row['distance_km']} km" if row["distance_km"] is not None else "")
                + f" · {row['transport']}/{row['port']} · IP {row['ip']}"
                for row in rows
            ]
            return tool_result(
                ok=True, source=self.slug,
                summary=f"{len(rows)} Shodan-Treffer für {label} (nur Metadaten):\n"
                        + "\n".join(lines),
                data={"category": category, "radius_km": radius,
                      "total": payload.get("total", len(rows)), "results": rows},
                warnings=[
                    "Standort wurde vor der Shodan-Anfrage auf ca. 1 km vergröbert.",
                    "Nur Systeme prüfen, für die du ausdrücklich autorisiert bist.",
                ])

        async def _breach_check(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="OSINT ist deaktiviert.")
            key = str(self.get("hibp_key") or "").strip()
            account = str(args.get("email") or "").strip()
            if not key:
                return tool_result(ok=False, source=self.slug, summary="Kein HIBP-Key hinterlegt.")
            if not account:
                return tool_result(ok=False, source=self.slug, summary="Keine Mailadresse angegeben.")
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                async with self._client() as c:
                    r = await c.get(
                        f"https://haveibeenpwned.com/api/v3/breachedaccount/{account}",
                        headers={"hibp-api-key": key, "user-agent": "ASTRA"})
                    if r.status_code == 404:
                        return tool_result(ok=True, source=self.slug,
                                           summary=f"{account}: in keinem bekannten Leak.")
                    r.raise_for_status()
                    breaches = [b.get("Name") for b in r.json()]
            except Exception as exc:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=self._safe_http_error("HIBP", exc))
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
            out: dict[str, Any] = {"domain": domain}
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                # DNS-over-HTTPS travels through the same verified Tor client.
                # socket.getaddrinfo() here would leak the query to the host resolver.
                async with self._client() as c:
                    dns = await c.get("https://dns.google/resolve",
                                      params={"name": domain, "type": "A"})
                    dns.raise_for_status()
                    answers = dns.json().get("Answer") or []
                    out["a"] = sorted({
                        str(answer.get("data") or "")
                        for answer in answers if answer.get("type") == 1 and answer.get("data")
                    })
            except Exception as exc:  # noqa: BLE001
                out["a"] = []
                out["dns_error"] = self._safe_http_error("DNS-over-HTTPS", exc)
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
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                async with self._client() as c:
                    r = await c.get(url, params={"q": query, "format": "json"})
                    r.raise_for_status()
                    results = (r.json().get("results") or [])[:8]
            except Exception as exc:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=self._safe_http_error("Suche", exc))
            if not results:
                return tool_result(ok=True, source=self.slug, summary="Keine Treffer.")
            safe_results = [{
                "title": str(x.get("title") or ""),
                "source": urlparse(str(x.get("url") or "")).hostname or "",
                "content": str(x.get("content") or "")[:500],
            } for x in results]
            lines = [f"• {x['title']} — {x['source']}\n  {x['content'][:160]}"
                     for x in safe_results]
            return tool_result(ok=True, source=self.slug,
                               summary="Treffer (über Tor):\n" + "\n".join(lines),
                               data={"results": safe_results},
                               warnings=["Keine externen Direktlinks ausgegeben. Inhalte mit "
                                         "osint_fetch serverseitig über Tor abrufen."])

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
            tor_ok, tor_message = await self._verify_tor()
            if not tor_ok:
                return tool_result(ok=False, source=self.slug, summary=tor_message)
            try:
                async with self._client() as c:
                    r = await c.get(_WINDY_WEBCAMS, params=params,
                                    headers={"x-windy-api-key": key})
                    r.raise_for_status()
                    cams = (r.json().get("webcams") or [])[:8]
            except Exception as exc:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=self._safe_http_error("Windy-Webcams", exc))
            if not cams:
                return tool_result(ok=True, source=self.slug,
                                   summary=f"Keine öffentlichen Webcams gefunden"
                                           + (f" für {where}." if where else "."))
            lines = []
            safe_cams = []
            for cam in cams:
                loc = (cam.get("location") or {})
                lines.append(f"• {cam.get('title', '?')} ({loc.get('city', '')}, "
                             f"{loc.get('country', '')})")
                safe_cams.append({
                    "title": cam.get("title") or "",
                    "location": {"city": loc.get("city") or "",
                                 "country": loc.get("country") or ""},
                })
            return tool_result(ok=True, source=self.slug,
                               summary="Öffentliche Webcams:\n" + "\n".join(lines),
                               data={"webcams": safe_cams},
                               warnings=["Keine direkten Player- oder Geräte-URLs ausgegeben."])

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
            Tool(name="osint_nearby_exposure",
                 description="Finde über Shodan passiv indexierte Kameras oder Drucker in der "
                             "Nähe. Liefert ausschließlich Metadaten ohne externe Direktlinks; "
                             "verbindet sich nie mit Geräten und liefert keine Kamera-Feeds. "
                             "Nutze das nur für eigene oder ausdrücklich autorisierte Systeme.",
                 parameters={"type": "object", "properties": {
                     "category": {"type": "string", "enum": ["cameras", "printers"]},
                     "lat": {"type": "number"}, "lon": {"type": "number"},
                     "radius": {"type": "integer", "minimum": 1, "maximum": 50},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                     "required": ["category"]},
                 handler=_nearby_exposure, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"],
                 examples=["zeige mir über shodan kameras in meiner nähe",
                           "welche drucker sind bei mir in der nähe indexiert"]),
            Tool(name="osint_exit_ip",
                 description="Prüfe, ob die Recherche wirklich über Tor läuft, und zeige die "
                             "Exit-IP.",
                 parameters={"type": "object", "properties": {}},
                 handler=_exit_ip, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status"]),
            Tool(name="osint_net_scan",
                 description=(
                     "Netz-Audit: findet offene Ports (Drucker 9100/631, Kamera 554, SSH, "
                     "Web …) — auf privaten Netzen aus 'scan_networks' oder ausdrücklich "
                     "autorisierten öffentlichen IPs/CIDRs aus 'scan_targets'. Beliebige "
                     "Fremdziele lehnt die Prüfung ab. target=CIDR/Host, leer=erstes Ziel."
                 ),
                 parameters={"type": "object", "properties": {"target": {"type": "string"}}},
                 handler=_net_scan, owner_only=True, source=self.slug,
                 safety="external_send", intents=["research"],
                 examples=["welche geräte in meinem netz haben offene ports"]),
            Tool(name="osint_self_exposure",
                 description="Zeigt, was von einer ausdrücklich angegebenen eigenen öffentlichen "
                             "IP bei Shodan sichtbar ist — liest nur veröffentlichte Daten.",
                 parameters={"type": "object", "properties": {
                     "ip": {"type": "string", "description": "eigene öffentliche IP"}},
                     "required": ["ip"]},
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
