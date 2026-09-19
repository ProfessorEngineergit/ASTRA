"""Google OAuth helpers shared by native Google plugins."""
from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

from . import google_hub
from .plugins.base import ConfigField, FieldType

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


ACCOUNT_LEGACY = "legacy"


def google_oauth_fields() -> list[ConfigField]:
    return [
        ConfigField("google_account", "Google-Konto", required=False,
                    help="Leer = Standardkonto aus Admin → Google. Oder die Konto-ID / „legacy“ für die "
                         "eigenen Zugangsdaten dieses Plugins (unten). Auswahl am besten unter Admin → Google."),
        ConfigField("client_id", "Google OAuth Client ID", required=False,
                    help="Google Cloud Console -> OAuth Client (Web application)."),
        ConfigField("client_secret", "Google OAuth Client Secret", FieldType.PASSWORD,
                    required=False, secret=True),
        ConfigField("refresh_token", "Google Refresh Token", FieldType.PASSWORD,
                    required=False, secret=True, help="Wird durch 'Mit Google verbinden' gesetzt."),
        ConfigField("access_token", "Google Access Token", FieldType.PASSWORD,
                    required=False, secret=True, help="Kurzlebig, wird automatisch erneuert."),
        ConfigField("expires_at", "Token Ablauf", required=False),
        ConfigField("account_email", "Verbundenes Google-Konto", required=False),
    ]


def _legacy_ready(cfg: dict) -> bool:
    return bool(cfg.get("client_id") and cfg.get("client_secret") and cfg.get("refresh_token"))


def route(cfg: dict) -> tuple[str, str]:
    """Woher kommt der Zugriff? → ("hub", konto_id | "") oder ("legacy", "").

    Ausdrücklich gewähltes Konto gewinnt; sonst bleiben bestehende Plugin-eigene Tokens bewusst
    in Betrieb (nichts bricht beim Update); erst ohne die gilt das Standardkonto der Zentrale."""
    acct = str(cfg.get("google_account") or "").strip()
    if acct == ACCOUNT_LEGACY:
        return "legacy", ""
    if acct:
        return "hub", acct
    if _legacy_ready(cfg):
        return "legacy", ""
    return "hub", ""


def has_google_connection(cfg: dict) -> bool:
    kind, acct = route(cfg)
    return _legacy_ready(cfg) if kind == "legacy" else google_hub.usable(acct)


def authorization_url(*, client_id: str, redirect_uri: str, scopes: list[str], state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        r.raise_for_status()
        return r.json()


async def user_email(access_token: str) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 400:
            return ""
        return str(r.json().get("email") or "")


async def access_token(plugin) -> str:
    kind, acct = route(plugin.cfg if hasattr(plugin, "cfg") else {})
    if kind == "hub":
        return await google_hub.access_token(acct)
    token = str(plugin.get("access_token") or "")
    try:
        expires_at = float(plugin.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if token and expires_at > time.time() + 90:
        return token

    refresh = plugin.get("refresh_token")
    if not refresh:
        raise RuntimeError("Google OAuth ist nicht verbunden.")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data={
            "client_id": plugin.get("client_id"),
            "client_secret": plugin.get("client_secret"),
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        })
        if r.status_code >= 400:
            raise google_hub.GoogleApiError(google_hub.explain_error(r.status_code, google_hub._body(r)))
        data = r.json()
    return str(data["access_token"])


async def google_api(plugin, method: str, url: str, **kwargs) -> httpx.Response:
    kind, acct = route(plugin.cfg if hasattr(plugin, "cfg") else {})
    if kind == "hub":
        return await google_hub.api(acct, method, url, **kwargs)
    token = await access_token(plugin)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.request(method, url, headers=headers, **kwargs)
        if r.status_code >= 400:
            raise google_hub.GoogleApiError(
                google_hub.explain_error(r.status_code, google_hub._body(r), google_hub.product_for_url(url)))
        return r


def token_patch(token_data: dict, email: str = "") -> dict:
    now = time.time()
    patch = {
        "access_token": token_data.get("access_token", ""),
        "expires_at": str(now + int(token_data.get("expires_in") or 3600)),
    }
    if token_data.get("refresh_token"):
        patch["refresh_token"] = token_data["refresh_token"]
    if email:
        patch["account_email"] = email
    return patch
