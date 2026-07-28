"""Ziel-Prüfung für ausgehende Recherche — verhindert, dass OSINT nach innen zeigt.

Der Recherche-Pfeiler holt Seiten, die ein Modell vorschlägt. Ohne Prüfung wäre das
ein klassischer SSRF-Hebel: „hol mir mal http://192.168.178.179:8123" — und schon
liest die Recherche Bahrians Home Assistant, oder schlimmer den Docker-Metadaten-
Endpoint. Deshalb darf sie ausschließlich auf **öffentliche** Ziele zeigen.

Der Browser-Container ist zusätzlich netz-isoliert (siehe docker-compose). Das hier
ist die zweite Schicht: auch cortex selbst holt nie eine interne Adresse.

Rein und ohne I/O — bis auf die optionale DNS-Auflösung, die man abschalten kann.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# Namen, die nie aufgelöst werden müssen, um sie abzulehnen.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost",
    # Container-interne Dienste dieses Stacks
    "cortex", "postgres", "redis", "n8n", "waha", "signal-cli", "langfuse",
    "caddy", "tor", "browser", "homeassistant", "home-assistant",
})
_BLOCKED_SUFFIXES = (".local", ".internal", ".lan", ".home", ".arpa", ".onion")


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def check_url(url: str, *, resolve: bool = True) -> tuple[bool, str]:
    """(erlaubt, Grund). Nur öffentliche http(s)-Ziele sind erlaubt."""
    raw = (url or "").strip()
    if not raw:
        return False, "leere URL"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False, f"Schema '{parsed.scheme}' ist nicht erlaubt (nur http/https)"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "kein Host in der URL"
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return False, f"interner Host '{host}'"

    # Literale IP? Dann direkt prüfen, ohne DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return (True, "öffentlich") if _is_public_ip(ip) else (False, f"interne Adresse {host}")

    if not resolve:
        return True, "Name (nicht aufgelöst)"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return False, f"DNS fehlgeschlagen: {e}"
    for info in infos:
        addr = info[4][0]
        try:
            if not _is_public_ip(ipaddress.ip_address(addr)):
                # Rebinding-Schutz: EIN interner Treffer reicht zum Ablehnen.
                return False, f"'{host}' zeigt auf die interne Adresse {addr}"
        except ValueError:
            return False, f"unlesbare Adresse {addr}"
    return True, "öffentlich"


def assert_public(url: str, *, resolve: bool = True) -> str:
    ok, reason = check_url(url, resolve=resolve)
    if not ok:
        raise ValueError(f"Ziel abgelehnt: {reason}")
    return url


# ─── Aktives Scannen: nur auf ausdrücklich autorisierten Netzen ───────────────
# Passives Lesen öffentlicher Daten (oben) und AKTIVES Scannen sind zwei Paar
# Schuhe. Ein Portscan berührt ein Gerät. Deshalb darf er ausschließlich auf
# Netze zeigen, die Bahrian selbst als seine eigenen/freigegebenen einträgt —
# nie auf öffentliche Adressen und nie auf etwas außerhalb der Liste. Das ist
# die Grenze, die verhindert, dass „Recon" je einen Fremden trifft.
def _networks(allowed_cidrs: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for cidr in allowed_cidrs or []:
        try:
            nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
        except ValueError:
            continue
    return nets


def scan_target_ok(target: str, allowed_cidrs: list[str], *, allow_public: bool = False) -> tuple[bool, str]:
    """(erlaubt, Grund) für ein Scan-Ziel (Host oder CIDR).

    Erlaubt NUR, wenn das Ziel vollständig in einem der freigegebenen Netze liegt.
    Öffentliche Adressen bleiben standardmäßig tabu. Eine öffentliche Adresse darf
    nur passieren, wenn der aufrufende Plugin-Kontext sie zuvor über eine separate,
    explizite Eigentümer-Allowlist freigegeben hat (`allow_public=True`)."""
    raw = (target or "").strip()
    if not raw:
        return False, "leeres Ziel"
    allowed = _networks(allowed_cidrs)
    if not allowed:
        return False, "keine Netze freigegeben (Feld 'scan_networks' leer)"
    try:
        target_net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return False, f"'{raw}' ist keine IP/kein CIDR"
    # Öffentliche Ziele grundsätzlich ablehnen, egal was in der Liste steht.
    hosts_public = any(_is_public_ip(ip) for ip in
                       (target_net.network_address, target_net.broadcast_address))
    if hosts_public and not allow_public:
        return False, f"'{raw}' ist öffentlich — aktives Scannen nur im eigenen Netz"
    for net in allowed:
        if target_net.version == net.version and target_net.subnet_of(net):
            return True, f"innerhalb des freigegebenen Netzes {net}"
    return False, f"'{raw}' liegt außerhalb der freigegebenen Netze"
