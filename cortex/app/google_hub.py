"""Google-Konten zentral — EIN OAuth-Client, mehrere Konten, jedes Plugin wählt sein Konto.

Bisher hatte jedes Google-Plugin (Kalender, Aufgaben, Gmail) eigene Client-ID/-Secret und einen
eigenen Refresh-Token: dreimal eintragen, dreimal zustimmen, kein Kontowechsel. Hier gibt es
stattdessen eine zentrale Verwaltung:

    Client (id/secret)  ─┐
    Konto A (privat)     ├─ Produkte je Konto: Kalender · Aufgaben · Gmail lesen · Gmail senden
    Konto B (Schule)     ┘
    Plugin/Installation  →  wählt ein Konto (oder das Standardkonto)

Anmeldung: Google akzeptiert als Weiterleitungsadresse NUR https://<echte Domain> oder
http://localhost — eine LAN-Adresse wie http://10.60.0.190:8088 wird vom Client abgelehnt.
Darum gibt es zwei Modi (`pick_redirect`):
  • direct — Admin läuft über localhost oder eine https-Domain: Google leitet direkt zurück.
  • manual — Admin läuft über eine LAN-IP: Weiterleitung auf http://localhost:<Port>/…, die Seite lädt
    nicht, aber die Adresse in der Browserleiste enthält den Code; sie wird in ASTRA eingefügt.
Refresh-Tokens liegen verschlüsselt (Fernet) in der Plugin-Konfiguration; Access-Tokens nur im Speicher.

Reine Logik (Scopes, Weiterleitung, Fehlertexte, Adress-Parsing) ist ohne I/O testbar.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger("astra.google")

SLUG = "google_hub"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
CALLBACK_PATH = "/admin/oauth/google/callback"
BASE_SCOPES = ("openid", "email", "profile")


@dataclass(frozen=True)
class Product:
    key: str
    label: str
    scopes: tuple[str, ...]
    api_id: str            # Dienstname für „API aktivieren“
    api_title: str
    probe_url: str | None  # harmloser Lesezugriff zum Testen (None = nicht testbar)
    hint: str = ""


PRODUCTS: dict[str, Product] = {p.key: p for p in (
    Product("calendar", "Kalender", ("https://www.googleapis.com/auth/calendar",),
            "calendar-json.googleapis.com", "Google Calendar API",
            "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1"),
    Product("tasks", "Aufgaben", ("https://www.googleapis.com/auth/tasks",),
            "tasks.googleapis.com", "Google Tasks API",
            "https://tasks.googleapis.com/tasks/v1/users/@me/lists?maxResults=1"),
    Product("gmail_read", "Gmail lesen", ("https://www.googleapis.com/auth/gmail.readonly",),
            "gmail.googleapis.com", "Gmail API",
            "https://gmail.googleapis.com/gmail/v1/users/me/profile"),
    Product("gmail_send", "Gmail senden", ("https://www.googleapis.com/auth/gmail.send",),
            "gmail.googleapis.com", "Gmail API", None,
            "Nur Senderecht — lässt sich nicht gefahrlos testen."),
)}
# Welche Produkte ein Plugin braucht (für den Hinweis „Konto hat das nicht freigegeben“).
PLUGIN_PRODUCTS = {"google_calendar": ("calendar",), "google_tasks": ("tasks",),
                   "gmail": ("gmail_read", "gmail_send")}


# ─── Reine Logik ──────────────────────────────────────────────────────────────
def account_id(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "").strip().lower()).strip("_")[:64] or "konto"


def scopes_for(products, existing=()) -> list[str]:
    """Basis-Scopes + die der gewählten Produkte + bereits erteilte (nichts geht beim Erweitern verloren)."""
    out = list(BASE_SCOPES)
    for key in products or ():
        for sc in PRODUCTS[key].scopes if key in PRODUCTS else ():
            if sc not in out:
                out.append(sc)
    for sc in existing or ():
        if sc and sc not in out:
            out.append(sc)
    return out


def products_from_scopes(scopes) -> list[str]:
    have = set(scopes or [])
    return [k for k, p in PRODUCTS.items() if all(s in have for s in p.scopes)]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _public_domain(host: str) -> bool:
    host = host.lower().rstrip(".")
    if not host or "." not in host or _is_ip(host):
        return False
    return not host.endswith((".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".intranet"))


REDIRECT_MANUAL = "manual"


def normalize_redirect(text: str) -> tuple[str, str]:
    """Eingabe „Eigene Domain“ → (Weiterleitungs-URI, Fehler). Rein.

    Erlaubt: leer (automatisch), „manual“, eine Domain (`astra.example.com`), eine Adresse mit https
    (`https://astra.example.com`) oder die volle Callback-Adresse. Google akzeptiert nur https mit echter
    Domain oder http://localhost — alles andere wird hier abgelehnt, bevor Google es tut."""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw.lower() == REDIRECT_MANUAL:
        return REDIRECT_MANUAL, ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if not host:
        return "", "Das ist keine gültige Adresse."
    path = u.path.rstrip("/")
    if path in ("", "/admin"):
        path = CALLBACK_PATH
    if path != CALLBACK_PATH:
        return "", (f"Die Adresse muss auf {CALLBACK_PATH} enden — oder gib nur die Domain an "
                    f"(z. B. https://astra.example.com).")
    local = host in ("localhost", "127.0.0.1", "::1")
    if not (local or (u.scheme == "https" and _public_domain(host))):
        why = ("Google erlaubt http nur für localhost." if u.scheme == "http" and not local
               else "Google erlaubt nur https mit einer echten Domain (keine IP, kein .local/.lan).")
        return "", why
    port = f":{u.port}" if u.port and not (u.port == 443 and u.scheme == "https") and not (
        u.port == 80 and u.scheme == "http") else ""
    return f"{u.scheme}://{host}{port}{CALLBACK_PATH}", ""


def origin_of(url: str) -> str:
    """scheme://host[:port] einer Adresse (ohne Pfad), '' wenn ungültig."""
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}".lower() if u.scheme and u.netloc else ""


def pick_redirect(base_url: str, configured: str = "", *, force_manual: bool = False) -> dict:
    """Weiterleitungsadresse wählen. → {uri, mode: direct|manual|configured, reason}.

    `base_url` ist die Adresse, unter der der Admin gerade offen ist (z. B. http://10.60.0.190:8088/).
    `configured`: leer = automatisch, „manual“ = immer localhost + Adresse einfügen, sonst eigene Domain/URI."""
    u = urlparse(base_url)
    host, scheme = (u.hostname or ""), (u.scheme or "http")
    port = f":{u.port}" if u.port and not (u.port == 80 and scheme == "http") and not (
        u.port == 443 and scheme == "https") else ""
    manual = {"uri": f"http://localhost{port}{CALLBACK_PATH}", "mode": "manual",
              "reason": ("Google leitet auf localhost zurück; die Seite lädt nicht — kopiere dann die Adresse aus der "
                         "Browserleiste und füge sie in ASTRA ein.")}
    if force_manual or (configured or "").strip().lower() == REDIRECT_MANUAL:
        return manual
    if (configured or "").strip():
        uri, err = normalize_redirect(configured)
        if uri and not err:
            return {"uri": uri, "mode": "configured",
                    "reason": "Von dir festgelegte Weiterleitungsadresse (eigene Domain)."}
    if host in ("localhost", "127.0.0.1", "::1"):
        return {"uri": f"{scheme}://{host}{port}{CALLBACK_PATH}", "mode": "direct",
                "reason": "Du bist über localhost verbunden — Google darf direkt zurückleiten."}
    if scheme == "https" and _public_domain(host):
        return {"uri": f"https://{host}{port}{CALLBACK_PATH}", "mode": "direct",
                "reason": "Du bist über eine https-Domain verbunden — Google darf direkt zurückleiten."}
    manual["reason"] = ("Google erlaubt für Weiterleitungen keine LAN-Adresse (nur https-Domain oder localhost). "
                        + manual["reason"] + " Oder trage unten deine Domain ein.")
    return manual


def parse_pasted(text: str) -> dict:
    """Eingefügte Adresse / Query / nackter Code → {code, state, error}. Rein."""
    raw = (text or "").strip().strip('"\'<>')
    if not raw:
        return {"code": "", "state": "", "error": "Nichts eingefügt."}
    query = ""
    if "://" in raw or raw.startswith("/") or "?" in raw:
        query = urlparse(raw if "://" in raw else "http://x" + (raw if raw.startswith(("/", "?")) else "?" + raw)).query
    elif "=" in raw and "&" in raw or raw.startswith(("code=", "state=")):
        query = raw
    if query:
        q = parse_qs(query)
        return {"code": (q.get("code") or [""])[0], "state": (q.get("state") or [""])[0],
                "error": (q.get("error") or [""])[0]}
    return {"code": raw, "state": "", "error": ""}


def _first_project(text: str) -> str:
    m = re.search(r"project[s/ =]+(\d{6,})", text or "")
    return m.group(1) if m else ""


def product_for_url(url: str) -> str | None:
    if "calendar/v3" in url:
        return "calendar"
    if "tasks.googleapis.com" in url or "/tasks/v1" in url:
        return "tasks"
    if "gmail" in url:
        return "gmail_send" if url.rstrip("/").endswith("/messages/send") else "gmail_read"
    return None


def explain_error(status: int, body, product: str | None = None) -> dict:
    """Google-Fehlerantwort → {kind, message, action_url}. Rein.

    Die häufigsten Ursachen für „Google geht nicht“ bekommen einen klaren nächsten Schritt."""
    payload = body if isinstance(body, dict) else {}
    err = payload.get("error")
    err_obj = err if isinstance(err, dict) else {}
    text = json.dumps(payload, ensure_ascii=False) if payload else str(body or "")
    message = str(err_obj.get("message") or payload.get("error_description") or (err if isinstance(err, str) else "") or "")
    reasons = {str(e.get("reason", "")) for e in err_obj.get("errors", []) if isinstance(e, dict)}
    for d in err_obj.get("details", []) if isinstance(err_obj.get("details"), list) else []:
        if isinstance(d, dict):
            reasons.add(str(d.get("reason", "")))
    prod = PRODUCTS.get(product or "")
    low = (message + " " + text).lower()

    if err == "invalid_grant" or "invalid_grant" in low:
        return {"kind": "reauth", "action_url": "", "message": (
            "Google hat den Zugriff beendet (Token abgelaufen oder widerrufen). Verbinde das Konto neu. "
            "Tipp: Steht die OAuth-Zustimmung in der Google Cloud Console auf „Testing“, laufen Tokens nach "
            "7 Tagen ab — stelle sie auf „In production“ (für den Eigengebrauch reicht das ohne Prüfung).")}
    if err in ("invalid_client", "unauthorized_client") or "invalid_client" in low:
        return {"kind": "client", "action_url": "", "message": (
            "Client-ID oder Client-Secret stimmen nicht (invalid_client). Prüfe beide in der Google Cloud "
            "Console unter „Clients“ — der Typ muss „Webanwendung“ sein.")}
    if "redirect_uri_mismatch" in low:
        return {"kind": "redirect", "action_url": "", "message": (
            "Die Weiterleitungsadresse ist bei Google nicht eingetragen (redirect_uri_mismatch). Trage genau die "
            "auf der Seite „Google-Konten“ angezeigte Adresse als „Autorisierte Weiterleitungs-URI“ ein.")}
    if status == 403 and ({"accessNotConfigured", "SERVICE_DISABLED"} & reasons or "has not been used" in low
                          or "is disabled" in low):
        proj = _first_project(message + " " + text)
        api = prod.api_id if prod else ""
        url = (f"https://console.developers.google.com/apis/api/{api}/overview" + (f"?project={proj}" if proj else "")
               if api else "https://console.cloud.google.com/apis/library")
        return {"kind": "api_disabled", "action_url": url, "message": (
            f"Die {prod.api_title if prod else 'benötigte Google-API'} ist im Google-Cloud-Projekt nicht "
            "aktiviert. Aktiviere sie (Link) und teste nach einer Minute erneut.")}
    if status == 403 and ({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} & reasons
                          or "insufficient" in low and "scope" in low):
        return {"kind": "scope", "action_url": "", "message": (
            f"Dieses Konto hat „{prod.label if prod else 'dieses Produkt'}“ nicht freigegeben. Erweitere die "
            "Produkte des Kontos (Knopf „Produkte ändern“) und stimme bei Google zu.")}
    if "access_denied" in low or status == 403 and "test" in low and "user" in low:
        return {"kind": "denied", "action_url": "", "message": (
            "Google hat den Zugriff verweigert. Steht die App auf „Testing“, muss dieses Konto als „Testnutzer“ "
            "eingetragen sein (Google Auth Platform → Zielgruppe).")}
    if status == 401:
        return {"kind": "reauth", "action_url": "", "message":
                "Google lehnt das Token ab (401). Verbinde das Konto neu."}
    if status == 429:
        return {"kind": "quota", "action_url": "", "message": "Google-Limit erreicht (429). Später erneut versuchen."}
    return {"kind": "other", "action_url": "", "message": f"Google antwortet mit HTTP {status}: {message[:160] or 'ohne Details'}"}


class GoogleApiError(RuntimeError):
    def __init__(self, info: dict):
        super().__init__(info["message"])
        self.kind = info.get("kind", "other")
        self.action_url = info.get("action_url", "")


def _body(r: httpx.Response):
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return r.text[:400]


def build_auth_url(*, client_id: str, redirect_uri: str, scopes: list[str], state: str,
                   login_hint: str = "") -> str:
    """`select_account consent`: erlaubt ein ANDERES Konto zu wählen und liefert immer einen Refresh-Token."""
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes), "state": state, "access_type": "offline",
              "prompt": "select_account consent", "include_granted_scopes": "true"}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


# ─── Zustand ──────────────────────────────────────────────────────────────────
_STATE: dict = {"loaded": False, "client_id": "", "client_secret": "", "accounts": {}, "default": "",
                "redirect_uri": ""}
_TOKENS: dict[str, tuple[str, float]] = {}
_LOCK = asyncio.Lock()


def _reset_for_tests() -> None:
    _STATE.update(loaded=False, client_id="", client_secret="", accounts={}, default="", redirect_uri="")
    _TOKENS.clear()


async def load(force: bool = False) -> dict:
    if _STATE["loaded"] and not force:
        return _STATE
    from . import db
    from .config_store import get_config_store
    store = get_config_store()
    try:
        raw = await db.plugin_config_all(SLUG)
    except Exception:  # noqa: BLE001 — ohne DB gibt es einfach keine Konten
        raw = {}

    def val(key, secret=False):
        v = (raw.get(key) or {}).get("value")
        return store.decrypt(v) if secret and v else (v or "")
    accounts: dict = {}
    try:
        accounts = json.loads(val("accounts", True) or "{}")
    except json.JSONDecodeError:
        log.warning("Google-Konten nicht lesbar — Speicher wird ignoriert.")
    _STATE.update(loaded=True, client_id=val("client_id"), client_secret=val("client_secret", True),
                  accounts=accounts if isinstance(accounts, dict) else {}, default=val("default"),
                  redirect_uri=val("redirect_uri"))
    return _STATE


async def _persist() -> None:
    from . import db
    from .config_store import get_config_store
    store = get_config_store()
    await db.plugin_config_set(SLUG, "client_id", _STATE["client_id"], False)
    await db.plugin_config_set(SLUG, "client_secret", store.encrypt(_STATE["client_secret"]) if _STATE["client_secret"] else "", True)
    await db.plugin_config_set(SLUG, "accounts", store.encrypt(json.dumps(_STATE["accounts"], ensure_ascii=False)), True)
    await db.plugin_config_set(SLUG, "default", _STATE["default"], False)
    await db.plugin_config_set(SLUG, "redirect_uri", _STATE["redirect_uri"], False)


def has_client() -> bool:
    return bool(_STATE["client_id"] and _STATE["client_secret"])


def has_accounts() -> bool:
    return any(a.get("refresh_token") for a in _STATE["accounts"].values())


def resolve(acct: str | None = None) -> dict | None:
    """Konto per id; leer = Standardkonto, sonst das erste vorhandene."""
    accts = _STATE["accounts"]
    if acct and acct in accts:
        return accts[acct]
    if acct and acct not in ("", "default"):
        return None                                 # ausdrücklich gewähltes Konto existiert nicht (mehr)
    if _STATE["default"] in accts:
        return accts[_STATE["default"]]
    return next(iter(accts.values()), None)


def usable(acct: str | None = None) -> bool:
    a = resolve(acct)
    return bool(a and a.get("refresh_token") and has_client())


def summary() -> dict:
    """Secret-freie Sicht für die UI."""
    return {"client_id": _STATE["client_id"], "has_secret": bool(_STATE["client_secret"]),
            "redirect_uri": _STATE["redirect_uri"], "default": _STATE["default"],
            "accounts": [{"id": a["id"], "email": a.get("email", ""), "name": a.get("name", ""),
                          "products": products_from_scopes(a.get("scopes")), "status": a.get("status", "ok"),
                          "note": a.get("note", ""), "added": a.get("added", ""),
                          "default": a["id"] == (_STATE["default"] or next(iter(_STATE["accounts"]), ""))}
                         for a in _STATE["accounts"].values()]}


async def set_client(client_id: str, client_secret: str = "", redirect_uri: str | None = None) -> None:
    await load()
    _STATE["client_id"] = client_id.strip()
    if client_secret.strip():                                  # leer = bestehendes Secret behalten
        _STATE["client_secret"] = client_secret.strip()
    if redirect_uri is not None:
        _STATE["redirect_uri"] = redirect_uri.strip()
    _TOKENS.clear()
    await _persist()


async def set_default(acct: str) -> bool:
    await load()
    if acct not in _STATE["accounts"]:
        return False
    _STATE["default"] = acct
    await _persist()
    return True


async def upsert_account(*, email: str, name: str = "", refresh_token: str = "", scopes=None) -> dict:
    """Konto anlegen oder ergänzen. Ohne neuen Refresh-Token bleibt der alte erhalten."""
    await load()
    aid = account_id(email)
    old = _STATE["accounts"].get(aid, {})
    merged = scopes_for([], list(old.get("scopes", [])) + list(scopes or []))
    acct = {"id": aid, "email": email, "name": name or old.get("name", ""),
            "refresh_token": refresh_token or old.get("refresh_token", ""), "scopes": merged,
            "added": old.get("added") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "ok" if (refresh_token or old.get("refresh_token")) else "reauth", "note": ""}
    _STATE["accounts"][aid] = acct
    if not _STATE["default"] or _STATE["default"] not in _STATE["accounts"]:
        _STATE["default"] = aid
    _TOKENS.pop(aid, None)
    await _persist()
    return acct


async def remove_account(acct: str, *, revoke: bool = True) -> bool:
    await load()
    a = _STATE["accounts"].pop(acct, None)
    if not a:
        return False
    _TOKENS.pop(acct, None)
    if _STATE["default"] == acct:
        _STATE["default"] = next(iter(_STATE["accounts"]), "")
    await _persist()
    if revoke and a.get("refresh_token"):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(REVOKE_URL, data={"token": a["refresh_token"]})
        except Exception:  # noqa: BLE001 — Trennen darf nie am Netz scheitern
            log.debug("Google revoke failed", exc_info=True)
    return True


async def _mark(acct: str, status: str, note: str = "") -> None:
    a = _STATE["accounts"].get(acct)
    if a and (a.get("status") != status or a.get("note") != note):
        a["status"], a["note"] = status, note[:200]
        try:
            await _persist()
        except Exception:  # noqa: BLE001
            log.debug("could not persist account status", exc_info=True)


# ─── Token & API ──────────────────────────────────────────────────────────────
async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(TOKEN_URL, data=data)
    if r.status_code >= 400:
        raise GoogleApiError(explain_error(r.status_code, _body(r)))
    return r.json()


async def exchange_code(code: str, redirect_uri: str) -> dict:
    await load()
    return await _token_request({"client_id": _STATE["client_id"], "client_secret": _STATE["client_secret"],
                                 "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})


async def access_token(acct: str | None = None, *, force: bool = False) -> str:
    await load()
    a = resolve(acct)
    if not a:
        raise GoogleApiError({"kind": "missing", "message": (
            "Kein Google-Konto verbunden. Verbinde eines unter Admin → Google.")})
    if not has_client():
        raise GoogleApiError({"kind": "client", "message": "Google-Client (ID/Secret) fehlt — Admin → Google."})
    if not a.get("refresh_token"):
        raise GoogleApiError({"kind": "reauth", "message": f"{a.get('email')}: bitte neu verbinden (Admin → Google)."})
    hit = _TOKENS.get(a["id"])
    if hit and not force and hit[1] > time.time() + 90:
        return hit[0]
    async with _LOCK:                                            # parallele Aufrufe erneuern nur einmal
        hit = _TOKENS.get(a["id"])
        if hit and not force and hit[1] > time.time() + 90:
            return hit[0]
        try:
            data = await _token_request({"client_id": _STATE["client_id"], "client_secret": _STATE["client_secret"],
                                         "refresh_token": a["refresh_token"], "grant_type": "refresh_token"})
        except GoogleApiError as e:
            if e.kind == "reauth":
                await _mark(a["id"], "reauth", str(e))
            raise
        _TOKENS[a["id"]] = (str(data["access_token"]), time.time() + int(data.get("expires_in") or 3600))
        if a.get("status") != "ok":
            await _mark(a["id"], "ok")
        return _TOKENS[a["id"]][0]


async def api(acct: str | None, method: str, url: str, **kwargs) -> httpx.Response:
    """Google-API-Aufruf mit Konto; bei 401 einmal Token erneuern; Fehler mit klarem Text."""
    product = product_for_url(url)
    headers = dict(kwargs.pop("headers", {}) or {})
    for attempt in (0, 1):
        token = await access_token(acct, force=bool(attempt))
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.request(method, url, headers={**headers, "Authorization": f"Bearer {token}"}, **kwargs)
        if r.status_code == 401 and not attempt:
            continue
        if r.status_code >= 400:
            info = explain_error(r.status_code, _body(r), product)
            a = resolve(acct)
            if a and info["kind"] == "reauth":
                await _mark(a["id"], "reauth", info["message"])
            raise GoogleApiError(info)
        return r
    raise GoogleApiError({"kind": "reauth", "message": "Google lehnt das Token ab."})   # pragma: no cover


async def probe(acct: str, product: str) -> dict:
    """Ein Produkt eines Kontos prüfen → {ok, message, action_url, kind}."""
    p = PRODUCTS.get(product)
    a = resolve(acct)
    if not p or not a:
        return {"ok": False, "kind": "other", "message": "Unbekanntes Konto oder Produkt.", "action_url": ""}
    if product not in products_from_scopes(a.get("scopes")):
        return {"ok": False, "kind": "scope", "action_url": "",
                "message": f"„{p.label}“ ist für dieses Konto nicht freigegeben."}
    if not p.probe_url:
        return {"ok": True, "kind": "skipped", "message": p.hint or "Nicht testbar.", "action_url": ""}
    try:
        await api(a["id"], "GET", p.probe_url)
        return {"ok": True, "kind": "ok", "message": f"{p.label}: funktioniert.", "action_url": ""}
    except GoogleApiError as e:
        return {"ok": False, "kind": e.kind, "message": str(e), "action_url": e.action_url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": "other", "message": f"Netzwerkfehler: {e}"[:200], "action_url": ""}


async def user_info(access: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
    return r.json() if r.status_code < 400 else {}


async def complete_login(code: str, redirect_uri: str) -> dict:
    """Code → Konto speichern. Der Konto-Name kommt von Google, nicht vom Nutzer."""
    data = await exchange_code(code, redirect_uri)
    info = await user_info(str(data.get("access_token") or ""))
    email = str(info.get("email") or "").strip().lower()
    if not email:
        raise GoogleApiError({"kind": "other", "message": "Google hat keine E-Mail-Adresse geliefert."})
    scopes = str(data.get("scope") or "").split()
    acct = await upsert_account(email=email, name=str(info.get("name") or ""),
                                refresh_token=str(data.get("refresh_token") or ""), scopes=scopes)
    if not acct["refresh_token"]:
        raise GoogleApiError({"kind": "reauth", "message": (
            "Google hat keinen Refresh-Token geliefert. Entferne ASTRA unter myaccount.google.com/permissions "
            "und verbinde das Konto erneut.")})
    _TOKENS[acct["id"]] = (str(data["access_token"]), time.time() + int(data.get("expires_in") or 3600))
    return acct
