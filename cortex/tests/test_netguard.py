"""SSRF-Schutz der Recherche: OSINT darf NIE nach innen zeigen. Ohne das wäre
'hol mir mal http://192.168.178.179:8123' ein Hebel auf Bahrians Home Assistant."""
import pytest

from app import netguard


@pytest.mark.parametrize("url", [
    "http://192.168.178.179:8123",   # Home Assistant
    "http://10.0.0.5", "http://172.16.0.1", "http://127.0.0.1:8088",
    "http://localhost/admin", "http://[::1]:8000",
    "http://cortex:8000/health",     # Container dieses Stacks
    "http://postgres:5432", "http://waha:3000", "http://browser:3000",
    "https://astra.local", "http://nas.lan", "http://box.internal",
    "http://169.254.169.254/latest/meta-data/",   # Cloud-Metadaten
    "http://0.0.0.0",
])
def test_internal_targets_are_refused(url):
    ok, _reason = netguard.check_url(url, resolve=False)
    assert ok is False


@pytest.mark.parametrize("url", [
    "https://example.com", "https://www.tagesschau.de/inland",
    "http://1.1.1.1", "https://api.windy.com/webcams/api/v3/webcams",
])
def test_public_targets_pass(url):
    ok, _reason = netguard.check_url(url, resolve=False)
    assert ok is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com", "gopher://x", "ws://browser:3000",
])
def test_only_http_schemes_are_allowed(url):
    assert netguard.check_url(url, resolve=False)[0] is False


def test_onion_is_refused_as_an_explicit_choice():
    # Tor als Ausgang ja — Hidden Services nein.
    assert netguard.check_url("http://abcdefgh.onion", resolve=False)[0] is False


def test_empty_and_garbage():
    assert netguard.check_url("", resolve=False)[0] is False
    assert netguard.check_url("   ", resolve=False)[0] is False


def test_assert_public_raises_with_reason():
    with pytest.raises(ValueError, match="abgelehnt"):
        netguard.assert_public("http://192.168.1.1", resolve=False)
    assert netguard.assert_public("https://example.com", resolve=False)
