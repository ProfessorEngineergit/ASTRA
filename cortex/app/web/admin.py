"""Web admin router: first-run setup, login, plugin catalog + config forms."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import __version__ as ASTRA_VERSION
from .. import db, sysinfo
from ..config import get_settings
from ..config_store import SECRET_SENTINEL, get_config_store
from ..plugins.base import CATEGORY_LABELS, FieldType
from ..plugins.registry import get_manager
from . import auth
from ..models import set_model_override
from ..plugins import extended_catalog
from .templates import (
    LOGO_LONG, brand_icon, esc, font_choices, icon_html, page, set_font,
)

GH_REPO = "https://github.com/ProfessorEngineergit/ASTRA"
GH_OWNER_REPO = "ProfessorEngineergit/ASTRA"
REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATE_PULL_COMMAND = ["git", "pull", "--ff-only", "origin", "main"]
UPDATE_REBUILD_COMMAND = "docker compose up -d --build cortex"
_GH_SVG = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">'
           '<path d="M12 .5A12 12 0 0 0 8.2 23.9c.6.1.8-.3.8-.6v-2c-3.3.7-4-1.6-4-1.6-.5-1.4-1.3-1.8-1.3-1.8'
           '-1.1-.7.1-.7.1-.7 1.2 0 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.7-1.6-2.6-.3-5.4-1.3-5.4-5.9'
           '0-1.3.5-2.4 1.2-3.2 0-.3-.5-1.5.1-3.1 0 0 1-.3 3.3 1.2a11.5 11.5 0 0 1 6 0C17 4.7 18 5 18 5c.6 1.6.1 2.8.1 3.1'
           '.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.5.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A12 12 0 0 0 12 .5Z"/></svg>')

log = logging.getLogger("astra.web.admin")
router = APIRouter()


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


async def _check_csrf(request: Request, form) -> bool:
    tok = form.get("csrf")
    return bool(tok) and tok == request.cookies.get(auth.CSRF_COOKIE) and await auth.valid_csrf(tok)


def _html_with_csrf(html: str, token: str, status: int = 200) -> HTMLResponse:
    resp = HTMLResponse(html, status_code=status)
    resp.set_cookie(auth.CSRF_COOKIE, token, max_age=auth.SESSION_TTL, samesite="strict")
    return resp


def _redirect_with_session(url: str, token: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL,
                    httponly=True, samesite="strict")
    return resp


async def _run_update_cmd(args: list[str], *, timeout: float = 12) -> dict:
    """Run a fixed maintenance command from the repo root."""
    if args and args[0] == "git" and not which("git"):
        return {"ok": False, "code": 127, "out": "", "err": "git ist auf diesem Server nicht installiert."}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=REPO_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "code": 124, "out": "", "err": f"Timeout nach {timeout:.0f}s."}
    except FileNotFoundError:
        return {"ok": False, "code": 127, "out": "", "err": f"{args[0]} wurde nicht gefunden."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": 1, "out": "", "err": str(e)}
    out = out_b.decode(errors="replace").strip()
    err = err_b.decode(errors="replace").strip()
    return {"ok": proc.returncode == 0, "code": proc.returncode, "out": out, "err": err}


async def _git_text(args: list[str], *, timeout: float = 8) -> str:
    result = await _run_update_cmd(args, timeout=timeout)
    return result["out"].strip() if result["ok"] else ""


async def _git_update_status(*, fetch: bool = False) -> dict:
    repo_check = await _run_update_cmd(["git", "rev-parse", "--git-dir"], timeout=5)
    if not repo_check["ok"]:
        return {
            "ok": False,
            "git_available": bool(which("git")),
            "repo_root": str(REPO_ROOT),
            "app_version": ASTRA_VERSION,
            "message": "ASTRA läuft hier nicht aus einem Git-Checkout. Pull ist deshalb nicht möglich.",
        }

    fetch_result = None
    if fetch:
        fetch_result = await _run_update_cmd(["git", "fetch", "--tags", "origin", "main"], timeout=35)

    local_full = await _git_text(["git", "rev-parse", "HEAD"])
    remote_full = await _git_text(["git", "rev-parse", "origin/main"])
    local_short = await _git_text(["git", "rev-parse", "--short", "HEAD"])
    remote_short = await _git_text(["git", "rev-parse", "--short", "origin/main"])
    local_tag = await _git_text(["git", "describe", "--tags", "--exact-match", "HEAD"])
    latest_tag = await _git_text(["git", "describe", "--tags", "--abbrev=0", "origin/main"])
    ahead_behind = await _git_text(["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"])
    dirty = bool(await _git_text(["git", "status", "--short"]))
    commit_lines = await _git_text(
        ["git", "log", "--oneline", "--no-decorate", "--max-count=6", "HEAD..origin/main"]
    )
    ahead = behind = 0
    if ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    target_version = latest_tag or remote_short or "unbekannt"
    current_version = local_tag or (f"{ASTRA_VERSION}+{local_short}" if local_short else ASTRA_VERSION)
    data = {
        "ok": True,
        "git_available": True,
        "repo_root": str(REPO_ROOT),
        "repo": GH_OWNER_REPO,
        "app_version": ASTRA_VERSION,
        "current_sha": local_short,
        "remote_sha": remote_short,
        "current_version": current_version,
        "target_version": target_version,
        "release_mode": "release-tag" if latest_tag else "commit-fallback",
        "update_available": bool(remote_full and local_full and remote_full != local_full),
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "commits": [line for line in commit_lines.splitlines() if line],
        "pull_command": " ".join(UPDATE_PULL_COMMAND),
        "rebuild_command": UPDATE_REBUILD_COMMAND,
        "release_note": (
            "Die Karte bevorzugt GitHub Releases/Tags. Ohne Tag nutzt ASTRA den neuesten Commit als Version."
        ),
    }
    if fetch_result is not None:
        data["fetch"] = {
            "ok": fetch_result["ok"],
            "code": fetch_result["code"],
            "out": fetch_result["out"][-3000:],
            "err": fetch_result["err"][-3000:],
        }
    return data


async def _git_pull_update() -> dict:
    before = await _git_update_status(fetch=False)
    if not before.get("ok"):
        return before
    if before.get("dirty"):
        return {
            **before,
            "ok": False,
            "message": "Lokale Git-Änderungen blockieren den Pull. Erst committen, stashen oder bewusst aufräumen.",
        }
    result = await _run_update_cmd(UPDATE_PULL_COMMAND, timeout=75)
    after = await _git_update_status(fetch=False)
    payload = {
        **after,
        "ok": result["ok"],
        "pull": {
            "code": result["code"],
            "out": result["out"][-6000:],
            "err": result["err"][-6000:],
        },
        "message": "Git pull abgeschlossen." if result["ok"] else "Git pull ist fehlgeschlagen.",
    }
    await db.audit("self_update_pull", actor="owner", detail={"ok": result["ok"], "code": result["code"]})
    return payload


# ─── First-run setup ──────────────────────────────────────────────────────────
@router.get("/admin/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    if await auth.has_admin_password():
        return RedirectResponse("/admin/login", status_code=303)
    token = await auth.issue_csrf()
    body = f"""<div class="center">
      <div class="auth-logo"><img src="{LOGO_LONG}" alt="ASTRA"></div>
      <div class="panel">
        <h2>Willkommen 👋</h2>
        <p class="note" style="text-align:center;margin:-6px 0 20px">Lege ein Admin-Passwort für
          die Weboberfläche fest.</p>
        <form method="post" action="/admin/setup">
          <input type="hidden" name="csrf" value="{esc(token)}">
          <div class="field"><label>Passwort (min. 8 Zeichen)</label>
            <input type="password" name="password" required minlength="8" autofocus></div>
          <div class="field"><label>Passwort wiederholen</label>
            <input type="password" name="confirm" required></div>
          <button class="btn block" type="submit">Passwort setzen &amp; loslegen</button>
        </form>
      </div></div>"""
    return _html_with_csrf(page("Setup", body, nav=False), token)


@router.post("/admin/setup")
async def setup_submit(request: Request):
    if await auth.has_admin_password():
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/setup", status_code=303)
    pw, confirm = form.get("password", ""), form.get("confirm", "")
    if len(pw) < 8 or pw != confirm:
        token = await auth.issue_csrf()
        body = ('<div class="center"><div class="flash err">Passwörter ungleich oder zu kurz.</div>'
                '<a class="btn" href="/admin/setup">Zurück</a></div>')
        return _html_with_csrf(page("Setup", body, nav=False), token)
    await auth.set_admin_password(pw)
    return _redirect_with_session("/admin", await auth.issue_session())


# ─── Login / Logout ───────────────────────────────────────────────────────────
@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    if not await auth.has_admin_password():
        return RedirectResponse("/admin/setup", status_code=303)
    token = await auth.issue_csrf()
    err = f'<div class="flash err">{esc(error)}</div>' if error else ""
    body = f"""<div class="center">
      <div class="auth-logo"><img src="{LOGO_LONG}" alt="ASTRA"></div>
      <div class="panel">
        <h2>Anmeldung</h2>{err}
        <form method="post" action="/admin/login">
          <input type="hidden" name="csrf" value="{esc(token)}">
          <div class="field"><label>Admin-Passwort</label>
            <input type="password" name="password" required autofocus></div>
          <button class="btn block" type="submit">Anmelden</button>
        </form>
      </div></div>"""
    return _html_with_csrf(page("Login", body, nav=False), token)


@router.post("/admin/login")
async def login_submit(request: Request):
    ip = _ip(request)
    if auth.rate_limited(ip):
        return RedirectResponse("/admin/login?error=Zu+viele+Versuche.+Bitte+warten.",
                                status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/login?error=Sitzung+abgelaufen.", status_code=303)
    if await auth.verify_password(form.get("password", "")):
        await db.audit("admin_login", actor="owner", detail={"ip": ip})
        return _redirect_with_session("/admin", await auth.issue_session())
    auth.record_attempt(ip)
    await db.audit("admin_login_failed", actor="owner", detail={"ip": ip})
    return RedirectResponse("/admin/login?error=Falsches+Passwort.", status_code=303)


@router.post("/admin/logout")
async def logout(request: Request):
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ─── Catalog ──────────────────────────────────────────────────────────────────
async def _favorites() -> list[str]:
    return await db.get_setting("favorites", []) or []


GH_NEW_ISSUE = "https://github.com/ProfessorEngineergit/ASTRA/issues/new"

_DEFAULT_LABS = {
    "font": "inter",
    "density": "comfortable",
    "motion": "normal",
    "event_horizon": "subtle",
    "surface_glow": "focused",
    "accent": "platinum",
    "catalog_view": "spacious",
    "map_style": "dark",
    "diagnostics": "off",
    "save_effect": "on",
}

_LAB_SELECTS = {
    "density": [
        ("comfortable", "Comfortable", "ruhige Abstände, Alltagspanel"),
        ("compact", "Compact", "mehr Inhalt pro Bildschirm"),
        ("dense", "Dense Ops", "maximal scanbar"),
    ],
    "motion": [
        ("calm", "Calm", "reduzierte Bewegung"),
        ("normal", "Normal", "polierte Mikrointeraktionen"),
        ("hyperspace", "Hyperspace", "mehr Energie, aber respektiert Reduce Motion"),
    ],
    "event_horizon": [
        ("off", "Off", "pechschwarz"),
        ("subtle", "Subtle", "OLED-Sternenrauschen"),
        ("deep", "Deep Field", "mehr kosmische Tiefe"),
    ],
    "surface_glow": [
        ("off", "Off", "streng und flach"),
        ("focused", "Focused", "leichter Fokusglanz"),
        ("cinematic", "Cinematic", "stärkeres Hover-Licht"),
    ],
    "accent": [
        ("platinum", "Platinum", "neutral und edel"),
        ("ion", "Ion", "kühles Cyan"),
        ("aurora", "Aurora", "grün-violette Signale"),
        ("ember", "Ember", "warme Warnlampen"),
    ],
    "catalog_view": [
        ("spacious", "Spacious Cards", "Karten mit Luft"),
        ("compact", "Compact Scan", "dichter Integrationsscanner"),
    ],
    "map_style": [
        ("dark", "Dark Matter", "CARTO Dark"),
        ("standard", "Street Grid", "OpenStreetMap Standard"),
        ("transit", "Transit Lines", "ÖPNV-orientierte Kacheln"),
    ],
    "diagnostics": [
        ("off", "Off", "saubere Oberfläche"),
        ("on", "On", "Slug- und Source-Badges sichtbar"),
    ],
    "save_effect": [
        ("off", "Off", "stilles Speichern"),
        ("on", "Pulse", "kurzer Erfolgsimpuls"),
    ],
}

_AREA_META = {
    "rmv": {"countries": ["de"], "states": ["hessen"], "label": "Hessen / Rhein-Main"},
    "bvg": {"countries": ["de"], "states": ["berlin"], "label": "Berlin"},
    "hvv": {"countries": ["de"], "states": ["hamburg"], "label": "Hamburg"},
    "deutsche_bahn": {"countries": ["de"], "label": "Deutschland"},
    "cat_mvg_muenchen": {"countries": ["de"], "states": ["bayern"], "label": "München / Bayern"},
    "cat_mvv_muenchen": {"countries": ["de"], "states": ["bayern"], "label": "München / Bayern"},
    "cat_vbb_berlin_brandenburg": {
        "countries": ["de"], "states": ["berlin", "brandenburg"], "label": "Berlin / Brandenburg"
    },
    "cat_vvs_stuttgart": {
        "countries": ["de"], "states": ["baden-wuerttemberg"], "label": "Stuttgart"
    },
    "cat_vrr_rhein_ruhr": {
        "countries": ["de"], "states": ["nordrhein-westfalen"], "label": "Rhein-Ruhr"
    },
    "cat_vrs_koeln_bonn": {
        "countries": ["de"], "states": ["nordrhein-westfalen"], "label": "Köln / Bonn"
    },
    "cat_vgn_nuernberg": {"countries": ["de"], "states": ["bayern"], "label": "Nürnberg"},
    "cat_mdv_mitteldeutschland": {
        "countries": ["de"],
        "states": ["sachsen", "sachsen-anhalt", "thueringen"],
        "label": "Mitteldeutschland",
    },
    "cat_oebb_oesterreich": {"countries": ["at"], "label": "Österreich"},
    "cat_sbb_schweiz": {"countries": ["ch"], "label": "Schweiz"},
    "cat_ns_niederlande": {"countries": ["nl"], "label": "Niederlande"},
    "cat_sncf_frankreich": {"countries": ["fr"], "label": "Frankreich"},
    "cat_trenitalia": {"countries": ["it"], "label": "Italien"},
    "cat_renfe_spanien": {"countries": ["es"], "label": "Spanien"},
    "cat_transport_for_london": {"countries": ["gb"], "label": "London"},
    "weather": {"global": True, "label": "weltweit"},
    "google_maps": {"global": True, "label": "weltweit"},
}


def _norm_area(v: str | None) -> str:
    return (v or "").strip().lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")


def _labs(s: dict) -> dict:
    labs = dict(_DEFAULT_LABS)
    stored = s.get("labs", {}) or {}
    if isinstance(stored, dict):
        labs.update({k: v for k, v in stored.items() if v not in (None, "")})
    if s.get("font") and not stored.get("font"):
        labs["font"] = s["font"]
    return labs


def _opt(key: str, selected: str) -> str:
    return "".join(
        f'<option value="{esc(value)}" {"selected" if selected == value else ""}>'
        f'{esc(label)} — {esc(desc)}</option>'
        for value, label, desc in _LAB_SELECTS[key]
    )


def _area_for(slug: str) -> dict:
    return _AREA_META.get(slug, {"global": True, "label": "global"})


def _area_attrs(slug: str) -> str:
    meta = _area_for(slug)
    countries = " ".join(_norm_area(v) for v in meta.get("countries", []))
    states = " ".join(_norm_area(v) for v in meta.get("states", []))
    counties = " ".join(_norm_area(v) for v in meta.get("counties", []))
    label = meta.get("label", "global")
    return (
        f'data-area-global="{"1" if meta.get("global") else "0"}" '
        f'data-area-countries="{esc(countries)}" '
        f'data-area-states="{esc(states)}" '
        f'data-area-counties="{esc(counties)}" '
        f'data-area-label="{esc(label)}"'
    )


def _labs_css(labs: dict) -> str:
    accent = {
        "platinum": ("#f4f5f8", "#aab4d6"),
        "ion": ("#9ee7ff", "#67e8f9"),
        "aurora": ("#c4f1be", "#c4b5fd"),
        "ember": ("#ffd6a5", "#f5c451"),
    }.get(labs.get("accent"), ("#f4f5f8", "#aab4d6"))
    event_opacity = {"off": "0", "subtle": ".35", "deep": ".62"}.get(
        labs.get("event_horizon"), ".35"
    )
    density_gap = {"comfortable": "16px", "compact": "12px", "dense": "9px"}.get(
        labs.get("density"), "16px"
    )
    density_pad = {"comfortable": "22px 24px", "compact": "18px 20px", "dense": "14px 16px"}.get(
        labs.get("density"), "22px 24px"
    )
    motion = labs.get("motion")
    glow = labs.get("surface_glow")
    css = [
        "<style>",
        f":root{{--accent:{accent[0]};--link:{accent[1]};--ring:{accent[1]}88;}}",
        f"body::before{{opacity:{event_opacity};}}",
        f".panel{{padding:{density_pad};}} .toolbar,.row{{gap:{density_gap};}}",
    ]
    if motion == "calm":
        css.append("*,*::before,*::after{transition-duration:.01ms!important;animation-duration:.01ms!important;}")
    elif motion == "hyperspace":
        css.append(".hero h1{animation:labDrift 5s ease-in-out infinite alternate;}")
    if glow == "off":
        css.append(".card:hover,.panel:hover{box-shadow:none!important;}")
    elif glow == "cinematic":
        css.append(".card:hover,.panel:hover{box-shadow:0 18px 60px rgba(170,180,214,.18),var(--shadow);}")
    else:
        css.append(".panel:hover{border-color:#292932;}")
    css.append("@keyframes labDrift{from{filter:none}to{filter:drop-shadow(0 0 16px rgba(170,180,214,.24))}}")
    css.append("@media (prefers-reduced-motion: reduce){*,*::before,*::after{animation:none!important;transition-duration:.01ms!important;}}")
    css.append("</style>")
    return "".join(css)


_BEAKER_SVG = (
    '<svg class="beaker" width="44" height="44" viewBox="0 0 48 48" fill="none" '
    'aria-hidden="true"><path d="M18 5h12M21 5v12L10.7 35.8C8.9 39.1 11.3 43 15 43h18'
    'c3.7 0 6.1-3.9 4.3-7.2L27 17V5" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round" stroke-linejoin="round"/><path d="M15 33c3 1.7 6.2 1.8 9.6.3'
    '3.6-1.7 6.5-1.5 8.7.2" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round"/><path d="M18 37h12" stroke="currentColor" stroke-width="2.2" '
    'stroke-linecap="round"/><circle cx="31.5" cy="20.5" r="1.7" fill="currentColor"/>'
    '<circle cx="19" cy="27" r="1.4" fill="currentColor"/></svg>'
)


def _select_html(name: str, options: str, *, select_id: str | None = None) -> str:
    attr = f' id="{esc(select_id)}"' if select_id else ""
    return f'<select name="{esc(name)}"{attr}>{options}</select>'


def _lab_tile(title: str, eyebrow: str, body: str, control: str) -> str:
    return (
        '<div class="lab-tile">'
        f'<div class="lab-eyebrow">{esc(eyebrow)}</div>'
        f'<div class="lab-title">{esc(title)}</div>'
        f'<p>{esc(body)}</p>{control}</div>'
    )


def _card_html(p, is_fav: bool) -> str:
    if getattr(p, "coming_soon", False):
        badge = '<span class="badge b-soon">bald</span>'
        action = ('<a class="btn ghost sm" style="margin-left:auto" '
                  f'href="/admin/plugin/{esc(p.slug)}">Details</a>')
    elif p.enabled:
        badge = '<span class="badge b-ok">aktiv</span>'
        action = (f'<a class="btn secondary sm" style="margin-left:auto" '
                  f'href="/admin/plugin/{esc(p.slug)}">Verwalten</a>')
    elif p.has_required:
        badge = '<span class="badge b-off">bereit</span>'
        action = (f'<a class="btn sm" style="margin-left:auto" '
                  f'href="/admin/plugin/{esc(p.slug)}">Aktivieren →</a>')
    else:
        badge = '<span class="badge b-off">nicht konfiguriert</span>'
        action = (f'<a class="btn secondary sm" style="margin-left:auto" '
                  f'href="/admin/plugin/{esc(p.slug)}">Einrichten →</a>')
    cat_label = esc(CATEGORY_LABELS.get(p.category, p.category.value))
    return f"""
        <div class="card {'on' if p.enabled else ''}"
             data-slug="{esc(p.slug)}"
             data-name="{esc((p.name + ' ' + p.description).lower())}"
             data-cat="{p.category.value}" data-source="nativ" data-fav="{'1' if is_fav else '0'}"
             {_area_attrs(p.slug)}>
          <div class="top">
            {icon_html(p.slug, p.icon)}
            <div class="meta">
              <h3>{esc(p.name)} <span class="tag-nativ">nativ</span></h3>
              <div class="cat">{cat_label}</div>
            </div>
            <button class="star {'on' if is_fav else ''}" data-slug="{esc(p.slug)}"
                    title="Favorit">★</button>
          </div>
          <p>{esc(p.description)}</p>
          <div class="row">{badge}{action}</div>
        </div>"""


def _catalog_card_html(e) -> str:
    """A non-native catalog entry — tagged 'Katalog', links to a GitHub request."""
    cat_label = esc(CATEGORY_LABELS.get(e.category, e.category.value))
    issue = f"{GH_NEW_ISSUE}?title=Integration:+{esc(e.name)}&labels=integration"
    return f"""
        <div class="card cat-entry"
             data-slug="{esc(e.slug)}"
             data-name="{esc((e.name + ' ' + e.description).lower())}"
             data-cat="{e.category.value}" data-source="katalog" data-fav="0"
             {_area_attrs(e.slug)}>
          <div class="top">
            {brand_icon(e.brand, e.icon)}
            <div class="meta">
              <h3>{esc(e.name)} <span class="tag-katalog">Katalog</span></h3>
              <div class="cat">{cat_label}</div>
            </div>
          </div>
          <p>{esc(e.description)}</p>
          <div class="row"><span class="badge b-soon">nicht nativ</span>
            <a class="btn ghost sm" style="margin-left:auto" target="_blank" rel="noopener"
               href="{issue}">Anfragen ↗</a></div>
        </div>"""


@router.get("/admin", response_class=HTMLResponse)
async def catalog(request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    appset = await _app_settings()
    labs = _labs(appset)
    loc = appset.get("location", {}) or {}
    region = {
        "country_code": _norm_area(loc.get("country_code")),
        "country": _norm_area(loc.get("country")),
        "state": _norm_area(loc.get("state")),
        "county": _norm_area(loc.get("county")),
        "city": _norm_area(loc.get("city")),
    }
    region_label = loc.get("county") or loc.get("state") or loc.get("country") or "Standort"
    favs = set(await _favorites())
    plugins = mgr.all()
    n_active = sum(1 for p in plugins if p.enabled)
    n_ready = sum(1 for p in plugins if p.has_required and not p.enabled
                  and not getattr(p, "coming_soon", False))

    # Extended catalog: skip entries that overlap an existing native plugin (by name).
    native_names = {p.name.lower() for p in plugins} | {p.slug for p in plugins}
    cat_entries = [e for e in extended_catalog.all_entries()
                   if e.name.lower() not in native_names]
    n_total = len(plugins) + len(cat_entries)

    cat_order = list(CATEGORY_LABELS)
    chips = '<span class="chip active" data-cat="all">Alle</span>' + "".join(
        f'<span class="chip" data-cat="{c.value}">{esc(CATEGORY_LABELS.get(c, c.value))}</span>'
        for c in cat_order if any(p.category == c for p in plugins) or any(e.category == c for e in cat_entries)
    )

    sections = []
    for c in cat_order:
        members = [p for p in plugins if p.category == c]
        entries = [e for e in cat_entries if e.category == c]
        if not members and not entries:
            continue
        cards = "".join(_card_html(p, p.slug in favs) for p in members)
        cards += "".join(_catalog_card_html(e) for e in entries)
        sections.append(f"""
        <div class="section" data-section="{c.value}">
          <div class="section-head">
            <h2>{esc(CATEGORY_LABELS.get(c, c.value))}</h2>
            <span class="count">{len(members) + len(entries)}</span>
          </div>
          <div class="grid">{cards}</div>
        </div>""")

    search_icon = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" '
                   'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
                   '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>')

    catalog_style = ""
    if labs.get("catalog_view") == "compact":
        catalog_style += (
            ".grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}"
            ".card{padding:13px;gap:9px}.card p{font-size:12.5px;line-height:1.42}"
            ".card .icon{width:38px;height:38px;font-size:22px}.card h3{font-size:14px}"
        )
    if labs.get("diagnostics") == "on":
        catalog_style += (
            ".card::after{content:attr(data-slug);position:absolute;right:12px;top:12px;"
            "font:10px 'JetBrains Mono',monospace;color:var(--text-faint);opacity:.72}"
        )
    else:
        catalog_style += ".tag-nativ,.tag-katalog{display:none}"

    catalog_script = (
        "<script>const astraRegion = " + json.dumps(region) + ";\n" + """
      const q=document.getElementById('q'), favonly=document.getElementById('favonly');
      const areaonly=document.getElementById('areaonly'), areanote=document.getElementById('areanote');
      let cat='all', src='all';
      const split=v=>(v||'').split(/\\s+/).filter(Boolean);
      const hasRegion=Boolean(astraRegion.country_code||astraRegion.state||astraRegion.county);
      const matchRegion=c=>{
        if(!areaonly.checked) return true;
        if(c.dataset.areaGlobal==='1') return true;
        const countries=split(c.dataset.areaCountries);
        const states=split(c.dataset.areaStates);
        const counties=split(c.dataset.areaCounties);
        if(counties.length && astraRegion.county && counties.includes(astraRegion.county)) return true;
        if(states.length && astraRegion.state && states.includes(astraRegion.state)) return true;
        if(states.length || counties.length) return false;
        if(countries.length && astraRegion.country_code && countries.includes(astraRegion.country_code)) return true;
        return !hasRegion && c.dataset.areaGlobal==='1';
      };
      function apply() {
        const term=q.value.toLowerCase(); let anyVisible=false;
        document.querySelectorAll('.section').forEach(sec=>{
          let shown=0;
          sec.querySelectorAll('.card').forEach(c=>{
            const okQ=c.dataset.name.includes(term);
            const okC=cat==='all'||c.dataset.cat===cat;
            const okF=!favonly.checked||c.dataset.fav==='1';
            const okS=src==='all'||c.dataset.source===src;
            const okA=matchRegion(c);
            const ok=okQ&&okC&&okF&&okS&&okA;
            c.style.display=ok?'':'none'; if(ok) shown++;
          });
          sec.style.display=shown?'':'none'; if(shown) anyVisible=true;
        });
        document.getElementById('empty').style.display=anyVisible?'none':'';
        if(areanote) areanote.style.display=areaonly.checked?'':'none';
      }
      q.oninput=apply; favonly.onchange=apply; areaonly.onchange=apply;
      document.querySelectorAll('#chips .chip').forEach(ch=>ch.onclick=()=>{
        document.querySelectorAll('#chips .chip').forEach(x=>x.classList.remove('active'));
        ch.classList.add('active'); cat=ch.dataset.cat; apply();
      });
      document.querySelectorAll('#seg .seg-btn').forEach(b=>b.onclick=()=>{
        document.querySelectorAll('#seg .seg-btn').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); src=b.dataset.src; apply();
      });
      document.querySelectorAll('.star').forEach(s=>s.onclick=async()=>{
        const r=await fetch('/admin/favorite/'+s.dataset.slug,{method:'POST'});
        const d=await r.json(); s.classList.toggle('on', d.favorite);
        s.closest('.card').dataset.fav=d.favorite?'1':'0'; apply();
      });
      apply();
    </script>"""
    )

    body = f"""
    {_labs_css(labs)}<style>{catalog_style}</style>
    <div class="hero">
      <h1>Deine <span class="grad">Integrationen</span></h1>
      <p>Verbinde ASTRA mit deiner Welt — Verkehr, Smart Home, Server, Messenger und mehr.
         <span class="note">Über {n_total} Dienste im Katalog.</span></p>
      <div class="stats">
        <div class="stat"><span class="dot" style="background:var(--ok)"></span><b>{n_active}</b> aktiv</div>
        <div class="stat"><span class="dot" style="background:var(--link)"></span><b>{n_ready}</b> startklar</div>
        <div class="stat"><span class="dot" style="background:#a78bfa"></span><b>{len(plugins)}</b> nativ</div>
        <div class="stat"><span class="dot" style="background:var(--text-faint)"></span><b>{len(cat_entries)}</b> im Katalog</div>
      </div>
    </div>
    <div class="toolbar">
      <div class="searchwrap">{search_icon}
        <input class="search" id="q" type="text" placeholder="Über {n_total} Integrationen durchsuchen…"></div>
      <div class="seg" id="seg">
        <button class="seg-btn active" data-src="all">Alle</button>
        <button class="seg-btn" data-src="nativ">Nativ</button>
        <button class="seg-btn" data-src="katalog">Katalog</button>
      </div>
      <label class="switch"><input type="checkbox" id="favonly" class="toggle"
        style="width:40px;height:23px"> nur Favoriten</label>
      <label class="switch"><input type="checkbox" id="areaonly" class="toggle"
        style="width:40px;height:23px"> nur in meinem Gebiet</label>
    </div>
    <div id="areanote" class="note" style="display:none;margin:-6px 0 14px">
      Gebietsfilter nutzt {esc(region_label)}. Globale und deutschlandweite Dienste bleiben sichtbar.
    </div>
    <div class="chips" id="chips">{chips}</div>
    <div id="sections">{''.join(sections)}</div>
    <div class="empty" id="empty" style="display:none">Keine Integrationen gefunden.</div>
    {catalog_script}"""
    return HTMLResponse(page("Plugins", body, active="plugins"))


# ─── Plugin config form ───────────────────────────────────────────────────────
def _field_input(f, value, is_set: bool) -> str:
    if f.type is FieldType.BOOL:
        checked = "checked" if value else ""
        return f'<input class="toggle" type="checkbox" name="{esc(f.key)}" {checked}>'
    if f.type is FieldType.SELECT and f.options:
        opts = "".join(f'<option {"selected" if str(value)==o else ""}>{esc(o)}</option>'
                       for o in f.options)
        return f'<select name="{esc(f.key)}">{opts}</select>'
    if f.secret:
        ph = "•••• gesetzt — leer lassen zum Behalten" if is_set else "noch nicht gesetzt"
        return f'<input type="password" name="{esc(f.key)}" placeholder="{esc(ph)}">'
    itype = "number" if f.type is FieldType.NUMBER else "text"
    return f'<input type="{itype}" name="{esc(f.key)}" value="{esc(value if value is not None else "")}">'


@router.get("/admin/plugin/{slug}", response_class=HTMLResponse)
async def plugin_form(slug: str, request: Request, _: bool = Depends(auth.require_admin),
                      saved: str = ""):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    inst = mgr.get(slug)
    if not cls or not inst:
        return HTMLResponse(page("?", '<div class="flash err">Unbekanntes Plugin.</div>'), 404)
    store = get_config_store()
    meta = await store.stored_meta(cls)
    token = await auth.issue_csrf()
    flash = '<div class="flash ok">Gespeichert.</div>' if saved else ""

    fields_html = ""
    for f in cls.config_fields:
        val = "" if f.secret else inst.get(f.key, f.default)
        help_ = f'<div class="help">{esc(f.help)}</div>' if f.help else ""
        req = ' <span class="req">*</span>' if f.required else ""
        fields_html += (f'<div class="field"><label>{esc(f.label)}{req}</label>'
                        f'{_field_input(f, val, meta.get(f.key, False))}{help_}</div>')

    soon = getattr(cls, "coming_soon", False)
    soon_banner = ('<div class="flash err">🚧 Dieses Plugin ist im Katalog gelistet, aber noch '
                   'nicht implementiert. Sag ASTRA, wenn du es priorisiert haben möchtest.</div>'
                   if soon else "")
    cat_label = esc(CATEGORY_LABELS.get(cls.category, cls.category.value))
    toggled = "checked" if inst.is_toggled_on else ""
    test_btn = ('' if soon else
                '<button class="btn secondary" type="button" id="testbtn">Verbindung testen</button>'
                '<span id="testresult" class="note"></span>')
    save_btn = ('' if soon else '<button class="btn" type="submit">Speichern</button>')
    test_script = '' if soon else f"""
    <script>
      document.getElementById('testbtn').onclick=async()=>{{
        const out=document.getElementById('testresult'); out.textContent='Teste…';
        const fd=new FormData(document.querySelector('form'));
        const r=await fetch('/admin/plugin/{esc(slug)}/test',{{method:'POST',body:fd}});
        const d=await r.json();
        out.textContent=(d.state==='ok'?'✅ ':d.state==='error'?'❌ ':'• ')+d.message;
      }};
    </script>"""

    body = f"""
    <div class="crumb"><a href="/admin">← Alle Plugins</a> · {cat_label}</div>
    <div class="hero" style="display:flex;align-items:center;gap:16px;margin-bottom:20px">
      <span class="icon" style="font-size:34px;width:60px;height:60px;display:grid;
        place-items:center;background:var(--surface-2);border:1px solid var(--border-soft);
        border-radius:var(--r-lg)">{esc(cls.icon)}</span>
      <div><h1 style="font-size:24px;margin:0 0 4px">{esc(cls.name)}</h1>
        <p style="margin:0">{esc(cls.description)}</p></div>
    </div>
    {soon_banner}{flash}
    <form method="post" action="/admin/plugin/{esc(slug)}">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="panel">
        <div class="toggle-row" style="margin-bottom:20px">
          <input class="toggle" type="checkbox" name="__enabled" {toggled}>
          <div><div style="font-weight:600;font-size:14px">Plugin aktiviert</div>
            <div class="note" style="font-size:12px">Tools werden sofort scharf geschaltet — ohne Neustart.</div></div>
        </div>
        {fields_html or '<p class="note">Keine Konfiguration nötig.</p>'}
        <hr>
        <div class="row">{save_btn}{test_btn}</div>
      </div>
    </form>{test_script}"""
    return _html_with_csrf(page(cls.name, body, active="plugins"), token)


def _values_from_form(cls, form) -> dict:
    values = {}
    for f in cls.config_fields:
        if f.type is FieldType.BOOL:
            values[f.key] = f.key in form
        else:
            values[f.key] = form.get(f.key, "")
    return values


@router.post("/admin/plugin/{slug}")
async def plugin_save(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    if not cls:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse(f"/admin/plugin/{slug}", status_code=303)
    values = _values_from_form(cls, form)
    enabled = "__enabled" in form
    await get_config_store().save(cls, values, enabled)
    await mgr.rebuild()
    return RedirectResponse(f"/admin/plugin/{slug}?saved=1", status_code=303)


@router.post("/admin/plugin/{slug}/test")
async def plugin_test(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    if not cls:
        return JSONResponse({"state": "error", "message": "Unbekanntes Plugin."})
    store = get_config_store()
    form = await request.form()
    # Build a temp config: stored values, overlaid with submitted non-empty ones.
    cfg = await store.load(cls)
    for f in cls.config_fields:
        if f.type is FieldType.BOOL:
            cfg[f.key] = f.key in form
        else:
            sub = form.get(f.key, "")
            if sub not in ("", SECRET_SENTINEL):
                cfg[f.key] = f.coerce(sub)
    cfg["__enabled"] = True  # test connectivity regardless of toggle
    try:
        status = await cls(cfg).health_check()
        return JSONResponse({"state": status.state.value, "message": status.message})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"state": "error", "message": str(e)})


@router.post("/admin/favorite/{slug}")
async def toggle_favorite(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    favs = await _favorites()
    if slug in favs:
        favs.remove(slug)
        is_fav = False
    else:
        favs.append(slug)
        is_fav = True
    await db.set_setting("favorites", favs)
    return JSONResponse({"favorite": is_fav})


# ─── Settings (general + location) ────────────────────────────────────────────
async def _app_settings() -> dict:
    return await db.get_setting("app_settings", {}) or {}


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_form(request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    s = await _app_settings()
    loc = s.get("location", {}) or {}
    labs = _labs(s)
    update_status = await _git_update_status(fetch=False)
    token = await auth.issue_csrf()
    save_fx = " save-pulse" if saved and labs.get("save_effect") == "on" else ""
    flash = f'<div class="flash ok{save_fx}">Gespeichert.</div>' if saved else ""
    lat = loc.get("lat", 50.1109)
    lon = loc.get("lon", 8.6821)
    city = loc.get("city", "")
    state = loc.get("state", "")
    county = loc.get("county", "")
    country = loc.get("country", "")
    country_code = loc.get("country_code", "")
    postcode = loc.get("postcode", "")
    tz_opts = "".join(
        f'<option {"selected" if s.get("timezone", "Europe/Berlin") == t else ""}>{t}</option>'
        for t in ["Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/London", "UTC"])
    units = s.get("units", "metric")
    lang = s.get("language", "de")
    model = esc(s.get("ai_model", ""))
    eco = "checked" if s.get("economy_mode") else ""
    cur_auto = s.get("autonomy", "ask")
    allow_sc = "checked" if s.get("allow_self_config", True) else ""
    auto_opts = "".join(
        f'<option value="{k}" {"selected" if cur_auto == k else ""}>{esc(k)} — {esc(v)}</option>'
        for k, v in {
            "ask": "fragt vor heiklen Aktionen nach",
            "confident": "wartet nicht ab, fragt bei Heiklem weiter",
            "full": "handelt eigenständig, überspringt Rückfragen",
        }.items())
    cur_font = labs.get("font", s.get("font", "inter"))
    addr = esc(loc.get("address", ""))
    update_ok = bool(update_status.get("ok"))
    update_available = bool(update_status.get("update_available"))
    update_title = "Update verfügbar" if update_available else "ASTRA ist aktuell" if update_ok else "Update-Check nicht bereit"
    update_subtitle = (
        f"Lokal {update_status.get('current_version', 'unbekannt')} · Remote {update_status.get('target_version', 'unbekannt')}"
        if update_ok else update_status.get("message", "Git-Status konnte nicht gelesen werden.")
    )
    update_meter = "100" if update_available else "12" if update_ok else "0"
    update_commits = update_status.get("commits") or []
    update_notes = "".join(
        f'<li>{esc(line)}</li>' for line in update_commits[:4]
    ) or '<li>Keine entfernten Commits im lokalen Cache. „Nach Updates suchen" aktualisiert den Stand.</li>'
    update_pull_disabled = "disabled" if (not update_available or update_status.get("dirty") or not update_ok) else ""
    font_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if k == cur_font else ""}>{esc(name)}</option>'
        for k, name in font_choices())
    lab_tiles = "".join([
        _lab_tile(
            "Font Forge", "Typography",
            "UI-Schrift mit Live-Vorschau und lokalen Fonts aus dem Fonts-Ordner.",
            _select_html("lab_font", font_opts, select_id="lab_font"),
        ),
        _lab_tile(
            "Cockpit Density", "Layout",
            "Wechselt die Informationsdichte zwischen Lounge und Kontrollraum.",
            _select_html("lab_density", _opt("density", labs.get("density", "comfortable"))),
        ),
        _lab_tile(
            "Motion Profile", "Motion",
            "Mikroanimationen von ruhig bis Hyperspace, mit Reduced-Motion-Respekt.",
            _select_html("lab_motion", _opt("motion", labs.get("motion", "normal"))),
        ),
        _lab_tile(
            "Event Horizon", "Backdrop",
            "Regelt die Tiefe des OLED-Sternenfelds hinter der Oberfläche.",
            _select_html(
                "lab_event_horizon", _opt("event_horizon", labs.get("event_horizon", "subtle"))
            ),
        ),
        _lab_tile(
            "Surface Glow", "Surfaces",
            "Steuert, wie stark Panels und Karten beim Fokus leuchten.",
            _select_html("lab_surface_glow", _opt("surface_glow", labs.get("surface_glow", "focused"))),
        ),
        _lab_tile(
            "Accent Spectrum", "Signal",
            "Wählt einen zurückhaltenden Signalton für Links, Fokus und Primärflächen.",
            _select_html("lab_accent", _opt("accent", labs.get("accent", "platinum"))),
        ),
        _lab_tile(
            "Catalog View", "Scanner",
            "Schaltet den Integrationskatalog zwischen Cards und dichtem Scan-Modus.",
            _select_html("lab_catalog_view", _opt("catalog_view", labs.get("catalog_view", "spacious"))),
        ),
        _lab_tile(
            "Map Style", "Geospatial",
            "Wählt den Kartenlook für Standort und Umgebung.",
            _select_html("lab_map_style", _opt("map_style", labs.get("map_style", "dark")), select_id="lab_map_style"),
        ),
        _lab_tile(
            "Diagnostic Badges", "Debug",
            "Blendet technische Slugs und Source-Marker im Katalog ein.",
            _select_html("lab_diagnostics", _opt("diagnostics", labs.get("diagnostics", "off"))),
        ),
        _lab_tile(
            "Save Effect", "Feedback",
            "Aktiviert einen kurzen Erfolgsimpuls nach dem Speichern.",
            _select_html("lab_save_effect", _opt("save_effect", labs.get("save_effect", "on"))),
        ),
    ])
    settings_script = (
        "<script>const fontLabels = "
        + json.dumps({key: name for key, name in font_choices()})
        + ";\n"
        + """
      const $ = id => document.getElementById(id);
      const status = $('geostatus'), addr = $('addr'), results = $('addrresults');
      const latInput = $('lat'), lonInput = $('lon'), mapStyle = $('lab_map_style');
      let aborter = null, searchTimer = null, lastResults = [];

      function setStatus(text, kind='') {
        status.textContent = text;
        status.dataset.kind = kind;
      }
      function setField(id, value) {
        const el = $(id); if (el) el.value = value || '';
      }
      function updateChips() {
        $('citychip').textContent = $('city').value || '—';
        $('regionchip').textContent = $('county').value || $('state').value || '—';
        $('countrychip').textContent = $('country').value || $('country_code').value || '—';
      }
      function cityFrom(a, fallback='') {
        return a.city || a.town || a.village || a.municipality || a.hamlet || a.county || fallback || '';
      }
      function applyPlace(d) {
        const a = d.address || {};
        setField('address', d.display_name || addr.value);
        setField('country_code', (a.country_code || '').toLowerCase());
        setField('country', a.country || '');
        setField('state', a.state || a.region || '');
        setField('county', a.county || a.state_district || '');
        setField('postcode', a.postcode || '');
        setField('city', cityFrom(a, d.name));
        if (d.display_name) addr.value = d.display_name;
        updateChips();
      }
      const tileDefs = {
        dark: {
          url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
          attr: '© OpenStreetMap, © CARTO'
        },
        standard: {
          url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
          attr: '© OpenStreetMap'
        },
        transit: {
          url: 'https://tile.memomaps.de/tilegen/{z}/{x}/{y}.png',
          attr: '© OpenStreetMap, Memomaps'
        }
      };
      const map = L.map('map').setView([
        parseFloat(latInput.value) || 50.1109,
        parseFloat(lonInput.value) || 8.6821
      ], 11);
      const marker = L.marker(map.getCenter(), { draggable: true }).addTo(map);
      let tileLayer = null;
      function setMapStyle(style) {
        const def = tileDefs[style] || tileDefs.dark;
        if (tileLayer) map.removeLayer(tileLayer);
        tileLayer = L.tileLayer(def.url, { attribution: def.attr, maxZoom: 19 }).addTo(map);
      }
      async function reverse(la, lo) {
        setField('lat', la.toFixed(5)); setField('lon', lo.toFixed(5));
        setStatus('Region wird gelesen…');
        try {
          const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&addressdetails=1&lat=${la}&lon=${lo}&zoom=16&accept-language=de`);
          const d = await r.json();
          applyPlace(d);
          setStatus('Standort übernommen.', 'ok');
        } catch (e) {
          setStatus('Konnte Region nicht laden. Koordinaten sind gesetzt.', 'warn');
        }
      }
      function place(la, lo, source) {
        map.setView([la, lo], 14);
        marker.setLatLng([la, lo]);
        reverse(la, lo);
        if (source) setStatus(source);
      }
      function hideResults() {
        results.hidden = true;
        results.innerHTML = '';
      }
      function escHtml(s) {
        return String(s || '').replace(/[&<>"']/g, ch => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[ch]));
      }
      function renderResults(items) {
        lastResults = items || [];
        if (!lastResults.length) {
          results.innerHTML = '<div class="addr-empty">Kein Treffer.</div>';
          results.hidden = false;
          return;
        }
        results.innerHTML = lastResults.map((d, i) => {
          const a = d.address || {};
          const line = [cityFrom(a, d.name), a.county || a.state, a.country].filter(Boolean).join(' · ');
          return `<button type="button" data-i="${i}"><b>${escHtml(d.display_name || d.name)}</b><span>${escHtml(line)}</span></button>`;
        }).join('');
        results.hidden = false;
      }
      async function searchAddress(q) {
        if (aborter) aborter.abort();
        aborter = new AbortController();
        setStatus('Suche Adresse…');
        try {
          const url = `https://nominatim.openstreetmap.org/search?format=jsonv2&addressdetails=1&limit=6&accept-language=de&q=${encodeURIComponent(q)}`;
          const r = await fetch(url, { signal: aborter.signal });
          renderResults(await r.json());
          setStatus('Vorschlag wählen oder ersten Treffer übernehmen.');
        } catch (e) {
          if (e.name !== 'AbortError') setStatus('Adresssuche gerade nicht erreichbar.', 'warn');
        }
      }
      addr.addEventListener('input', () => {
        clearTimeout(searchTimer);
        const q = addr.value.trim();
        setField('address', q);
        if (q.length < 3) { hideResults(); setStatus('Mindestens 3 Zeichen für Autocomplete.'); return; }
        searchTimer = setTimeout(() => searchAddress(q), 280);
      });
      results.addEventListener('click', e => {
        const btn = e.target.closest('button[data-i]');
        if (!btn) return;
        const d = lastResults[Number(btn.dataset.i)];
        if (!d) return;
        applyPlace(d);
        place(parseFloat(d.lat), parseFloat(d.lon), 'Adresse übernommen.');
        hideResults();
      });
      $('addrbtn').onclick = async () => {
        const q = addr.value.trim(); if (!q) return;
        if (!lastResults.length) await searchAddress(q);
        if (lastResults[0]) {
          const d = lastResults[0];
          applyPlace(d);
          place(parseFloat(d.lat), parseFloat(d.lon), 'Erster Treffer übernommen.');
          hideResults();
        }
      };
      $('geobtn').onclick = () => {
        if (!navigator.geolocation) {
          setStatus('Browser-Geolocation ist nicht verfügbar. Nutze Adresse oder Pin.', 'warn');
          return;
        }
        if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
          setStatus('Browser-Standort braucht HTTPS oder localhost. Adresse/Pin funktionieren weiter.', 'warn');
          return;
        }
        setStatus('Frage Browser nach Standort…');
        navigator.geolocation.getCurrentPosition(
          p => place(p.coords.latitude, p.coords.longitude, 'Browser-Standort übernommen.'),
          err => {
            const msg = err.code === 1
              ? 'Standortzugriff abgelehnt. Du kannst Adresse oder Pin nutzen.'
              : 'Standort konnte nicht bestimmt werden. Adresse oder Pin nutzen.';
            setStatus(msg, 'warn');
          },
          { enableHighAccuracy: true, timeout: 8000 }
        );
      };
      map.on('click', e => place(e.latlng.lat, e.latlng.lng, 'Pin gesetzt.'));
      marker.on('dragend', e => {
        const p = e.target.getLatLng();
        place(p.lat, p.lng, 'Pin verschoben.');
      });
      if (mapStyle) {
        setMapStyle(mapStyle.value);
        mapStyle.addEventListener('change', () => setMapStyle(mapStyle.value));
      }
      const fontSelect = $('lab_font'), preview = $('fontpreview');
      function updateFontPreview() {
        const name = fontLabels[fontSelect.value] || fontSelect.value || 'Inter';
        preview.style.setProperty('--ui-font-preview', `'${name}'`);
        preview.querySelector('span').textContent = `FONT FORGE · ${name}`;
      }
      fontSelect.addEventListener('change', updateFontPreview);
      updateFontPreview();
      updateChips();
      setTimeout(() => map.invalidateSize(), 200);
    </script>"""
    )
    update_script = """
    <script>
      (() => {
        const root = document.querySelector('[data-settings-update]');
        if (!root) return;
        const title = document.getElementById('settingsUpdateTitle');
        const subtitle = document.getElementById('settingsUpdateSubtitle');
        const version = document.getElementById('settingsUpdateVersion');
        const bar = document.getElementById('settingsUpdateBar');
        const log = document.getElementById('settingsUpdateLog');
        const notes = document.getElementById('settingsUpdateNotes');
        const checkBtn = document.getElementById('settingsUpdateCheck');
        const pullBtn = document.getElementById('settingsUpdatePull');

        function renderList(lines) {
          notes.innerHTML = '';
          (lines && lines.length ? lines : ['Keine entfernten Commits im lokalen Cache.']).slice(0, 6).forEach(line => {
            const li = document.createElement('li');
            li.textContent = line;
            notes.appendChild(li);
          });
        }
        function render(d) {
          title.textContent = d.update_available ? 'Update verfügbar' : d.ok ? 'ASTRA ist aktuell' : 'Update-Check nicht bereit';
          version.textContent = d.target_version || d.remote_sha || d.current_version || 'Latest';
          subtitle.textContent = d.ok
            ? `Lokal ${d.current_version || '?'} · Remote ${d.target_version || d.remote_sha || '?'} · ${d.release_mode || 'commit'}`
            : (d.message || 'Git-Status konnte nicht gelesen werden.');
          bar.style.width = d.update_available ? '100%' : d.ok ? '12%' : '0%';
          pullBtn.disabled = !d.ok || d.dirty || !d.update_available;
          if (d.dirty) log.textContent = 'Lokale Git-Änderungen blockieren Pulls. Erst committen/stashen/aufräumen.';
          renderList(d.commits || []);
          if (d.pull) {
            log.textContent = [d.message, d.pull.out, d.pull.err].filter(Boolean).join('\\n\\n') || d.message || '';
          } else if (d.fetch && !d.fetch.ok) {
            log.textContent = [d.fetch.err, d.fetch.out].filter(Boolean).join('\\n') || 'Fetch fehlgeschlagen.';
          } else if (!d.dirty && !d.pull) {
            log.textContent = d.release_note || '';
          }
        }
        async function call(url, method) {
          const r = await fetch(url, { method: method || 'GET' });
          const d = await r.json();
          render(d);
          return d;
        }
        checkBtn.onclick = async () => {
          checkBtn.disabled = true;
          log.textContent = 'Prüfe origin/main und Tags...';
          try { await call('/admin/update/check', 'POST'); }
          catch (e) { log.textContent = 'Update-Check fehlgeschlagen: ' + e; }
          finally { checkBtn.disabled = false; }
        };
        pullBtn.onclick = async () => {
          if (!confirm('git pull --ff-only origin main jetzt auf dem Server ausführen?')) return;
          pullBtn.disabled = true;
          root.classList.add('is-pulling');
          log.textContent = 'Pull läuft...';
          try { await call('/admin/update/pull', 'POST'); }
          catch (e) { log.textContent = 'Pull fehlgeschlagen: ' + e; }
          finally { root.classList.remove('is-pulling'); pullBtn.disabled = false; }
        };
        call('/admin/update/status').catch(() => {});
      })();
    </script>"""
    body = f"""
    {_labs_css(labs)}
    <div class="settings-hero">
      <div class="hero" style="margin:0">
        <h1>Einstellungen</h1>
        <p>Allgemeine Angaben, KI-Modell & dein Standort — den nutzen Plugins für Wetter,
           nächste Haltestelle und „in der Nähe".</p>
      </div>
      <a class="github-capsule" target="_blank" rel="noopener" href="{GH_REPO}" aria-label="Auf GitHub mitwirken">
        {_GH_SVG}
        <span class="gh-pop"><b>Hi.</b><small>Mitwirken?</small></span>
      </a>
    </div>
    {flash}
    <div class="settings-tabs">
      <a href="#settings-general">Allgemein</a>
      <a href="#settings-location">Standort</a>
      <a href="#settings-labs">Labs</a>
      <a href="#settings-updates">Updates</a>
    </div>
    <form method="post" action="/admin/settings" id="settings-form">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="panel" id="settings-general" style="margin-bottom:16px">
        <div class="field"><label>Name</label>
          <input type="text" name="owner_name" value="{esc(s.get('owner_name', 'Bahrian'))}"></div>
        <div class="row" style="gap:16px;align-items:flex-start">
          <div class="field" style="flex:1"><label>Zeitzone</label>
            <select name="timezone">{tz_opts}</select></div>
          <div class="field" style="flex:1"><label>Einheiten</label>
            <select name="units">
              <option value="metric" {"selected" if units == "metric" else ""}>Metrisch (°C, km)</option>
              <option value="imperial" {"selected" if units == "imperial" else ""}>Imperial (°F, mi)</option>
            </select></div>
          <div class="field" style="flex:1"><label>Sprache</label>
            <select name="language">
              <option value="de" {"selected" if lang == "de" else ""}>Deutsch</option>
              <option value="en" {"selected" if lang == "en" else ""}>English</option>
            </select></div>
        </div>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <h2 style="margin:0 0 12px;font-size:15px">KI-Modell</h2>
        <div class="field"><label>Chat-Modell</label>
          <input type="text" name="ai_model" value="{model}" placeholder="z. B. gpt-4o, gpt-4o-mini, o3-mini">
          <div class="help">Überschreibt die .env-Vorgabe live. Kleinere Modelle sparen Geld.
            Links: <a href="https://platform.openai.com/docs/models" target="_blank" rel="noopener">OpenAI-Modelle ↗</a> ·
            <a href="https://platform.openai.com/usage" target="_blank" rel="noopener">Nutzung &amp; Kosten ↗</a></div></div>
        <div class="toggle-row"><input class="toggle" type="checkbox" name="economy" {eco}>
          <div><div style="font-weight:600;font-size:14px">Sparmodus</div>
            <div class="note" style="font-size:12px">Reduziert proaktive API-Aufrufe (Briefing-Quellen,
              Health-Checks) — weniger Requests, weniger Kosten.</div></div></div>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <h2 style="margin:0 0 12px;font-size:15px">Autonomie &amp; Selbst-Zugriff</h2>
        <div class="field"><label>Autonomie-Stufe</label>
          <select name="autonomy">{auto_opts}</select>
          <div class="help">Im Chat handelt ASTRA ohnehin als du. Diese Stufe steuert eingehende
            Dritt-Nachrichten: <b>full</b> = ASTRA antwortet eigenständig und überspringt
            Telegram-Rückfragen/Freigaben.</div></div>
        <div class="toggle-row"><input class="toggle" type="checkbox" name="allow_self_config" {allow_sc}>
          <div><div style="font-weight:600;font-size:14px">ASTRA darf sich selbst konfigurieren</div>
            <div class="note" style="font-size:12px">Erlaubt ASTRA, eigene Integrationen zu aktivieren,
              Schlüssel zu setzen und Einstellungen zu ändern — direkt aus dem Chat.</div></div></div>
      </div>

      <div class="panel" id="settings-location" style="margin-bottom:16px">
        <h2 style="margin:0 0 6px;font-size:15px">Standort</h2>
        <p class="note" style="margin:0 0 12px">Adresse tippen, Vorschlag übernehmen, Pin ziehen
          oder Browser-Standort nutzen. Landkreis, Bundesland und Land speichert ASTRA für regionale Filter.</p>
        <div class="row location-row" style="gap:10px;margin-bottom:10px">
          <div class="address-box">
            <input type="text" id="addr" value="{addr}" placeholder="Adresse suchen (Straße, Ort)…"
              autocomplete="off">
            <div id="addrresults" class="address-results" hidden></div>
          </div>
          <button class="btn secondary" type="button" id="addrbtn">Ersten Treffer</button>
          <button class="btn secondary" type="button" id="geobtn">Mein Standort</button>
        </div>
        <div id="geostatus" class="note geo-status">Bereit für Standortsuche.</div>
        <input type="hidden" name="address" id="address" value="{addr}">
        <input type="hidden" name="country_code" id="country_code" value="{esc(country_code)}">
        <input type="hidden" name="country" id="country" value="{esc(country)}">
        <input type="hidden" name="state" id="state" value="{esc(state)}">
        <input type="hidden" name="county" id="county" value="{esc(county)}">
        <input type="hidden" name="postcode" id="postcode" value="{esc(postcode)}">
        <div class="region-strip" id="regionstrip">
          <span>Stadt: <b id="citychip">{esc(city or "—")}</b></span>
          <span>Region: <b id="regionchip">{esc(county or state or "—")}</b></span>
          <span>Land: <b id="countrychip">{esc(country or country_code or "—")}</b></span>
        </div>
        <div class="row" style="gap:12px;margin-bottom:14px">
          <div class="field" style="flex:2;margin:0"><label>Stadt</label>
            <input type="text" name="city" id="city" value="{esc(city)}" placeholder="wird erkannt…"></div>
          <div class="field" style="flex:1;margin:0"><label>Breite</label>
            <input type="text" name="lat" id="lat" value="{lat}" readonly></div>
          <div class="field" style="flex:1;margin:0"><label>Länge</label>
            <input type="text" name="lon" id="lon" value="{lon}" readonly></div>
        </div>
        <div id="map" style="height:340px;border-radius:var(--r);overflow:hidden;
             border:1px solid var(--border)"></div>
      </div>

      <div class="panel labs-console" id="settings-labs" style="margin-bottom:16px">
        <div class="labs-head">
          {_BEAKER_SVG}
          <div>
            <div class="lab-eyebrow">ASTRA Labs</div>
            <h2>Experimental Console</h2>
            <p>Polierte Nerd-Schalter für Oberfläche, Karten, Scanner und Feedback. Eigene Fonts
              in <code>cortex/app/web/static/fonts/</code> erscheinen automatisch.</p>
          </div>
        </div>
        <div class="font-preview" id="fontpreview">
          <span>FONT FORGE PREVIEW</span>
          <strong>ASTRA sieht scharf aus, wenn der Kontrollraum scharf ist.</strong>
        </div>
        <div class="labs-grid">{lab_tiles}</div>
      </div>

      <div class="panel settings-update-panel" id="settings-updates" style="margin-bottom:16px">
        <div class="settings-section-head">
          <div>
            <div class="lab-eyebrow">Updates</div>
            <h2>Server-Update</h2>
            <p>ASTRA prüft Releases/Tags und fällt sonst sauber auf Commits zurück. Der Pull läuft als
              feste Git-Aktion im Repository, ohne freie Shell-Befehle.</p>
          </div>
          <a class="btn ghost sm" href="/admin/update">Große Karte</a>
        </div>
        <div class="settings-update-card {'has-update' if update_available else ''}" data-settings-update>
          <div class="settings-update-grid"></div>
          <div class="settings-update-inner">
            <div class="settings-update-copy">
              <span class="settings-update-eyebrow">Release Channel · {esc(update_status.get('release_mode', 'commit-fallback'))}</span>
              <h3 id="settingsUpdateTitle">{esc(update_title)}</h3>
              <p id="settingsUpdateSubtitle">{esc(update_subtitle)}</p>
            </div>
            <div class="settings-update-version" id="settingsUpdateVersion">
              {esc(update_status.get('target_version') or update_status.get('remote_sha') or "Latest")}
            </div>
            <div class="settings-update-progress"><i id="settingsUpdateBar" style="width:{update_meter}%"></i></div>
            <div class="settings-update-actions">
              <button class="btn secondary" type="button" id="settingsUpdateCheck">Nach Updates suchen</button>
              <button class="btn" type="button" id="settingsUpdatePull" {update_pull_disabled}>Git Pull ausführen</button>
            </div>
          </div>
        </div>
        <div class="settings-update-notes">
          <div>
            <div class="notes-header">Nächste Änderungen</div>
            <ul id="settingsUpdateNotes">{update_notes}</ul>
          </div>
          <details>
            <summary>Release-Regel für Agenten</summary>
            <p>Ab jetzt ist ideal: nach erfolgreichem Commit einen Tag wie <code>v2026.06.07.1</code>
              setzen und pushen. Die UI zeigt dann diesen Release-Tag; ohne Tag bleibt der Commit-SHA sichtbar.</p>
            <pre>git tag -a vYYYY.MM.DD.N -m "ASTRA update"
git push origin main --tags</pre>
          </details>
        </div>
        <pre class="settings-update-log" id="settingsUpdateLog">{esc(update_status.get('release_note', ''))}</pre>
      </div>

      <button class="btn" type="submit">Alles speichern</button>
    </form>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    {settings_script}
    {update_script}"""
    return _html_with_csrf(page("Einstellungen", body, active="settings"), token)


@router.post("/admin/settings")
async def settings_save(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/settings", status_code=303)

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    s = await _app_settings()
    font_choice = form.get("lab_font", form.get("font", s.get("font", "inter")))
    labs = {
        "font": font_choice,
        "density": form.get("lab_density", _DEFAULT_LABS["density"]),
        "motion": form.get("lab_motion", _DEFAULT_LABS["motion"]),
        "event_horizon": form.get("lab_event_horizon", _DEFAULT_LABS["event_horizon"]),
        "surface_glow": form.get("lab_surface_glow", _DEFAULT_LABS["surface_glow"]),
        "accent": form.get("lab_accent", _DEFAULT_LABS["accent"]),
        "catalog_view": form.get("lab_catalog_view", _DEFAULT_LABS["catalog_view"]),
        "map_style": form.get("lab_map_style", _DEFAULT_LABS["map_style"]),
        "diagnostics": form.get("lab_diagnostics", _DEFAULT_LABS["diagnostics"]),
        "save_effect": form.get("lab_save_effect", _DEFAULT_LABS["save_effect"]),
    }
    s.update({
        "owner_name": form.get("owner_name", "").strip(),
        "timezone": form.get("timezone", "Europe/Berlin"),
        "units": form.get("units", "metric"),
        "language": form.get("language", "de"),
        "ai_model": form.get("ai_model", "").strip(),
        "autonomy": form.get("autonomy", "ask"),
        "allow_self_config": "allow_self_config" in form,
        "economy_mode": "economy" in form,
        "font": font_choice,
        "labs": labs,
        "location": {"lat": _f(form.get("lat"), 50.1109),
                     "lon": _f(form.get("lon"), 8.6821),
                     "city": form.get("city", "").strip(),
                     "address": form.get("address", "").strip(),
                     "country_code": form.get("country_code", "").strip().lower(),
                     "country": form.get("country", "").strip(),
                     "state": form.get("state", "").strip(),
                     "county": form.get("county", "").strip(),
                     "postcode": form.get("postcode", "").strip()},
    })
    await db.set_setting("app_settings", s)
    set_model_override(s["ai_model"])     # live, no restart
    set_font(s["font"])
    from ..brain import set_autonomy
    set_autonomy(s["autonomy"])
    await db.audit("settings_change", actor="owner",
                   detail={"city": s["location"]["city"], "model": s["ai_model"],
                           "font": s["font"], "county": s["location"]["county"]})
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ─── System: performance + services & URLs ────────────────────────────────────
def _meter(pct, used_h, total_h) -> str:
    if pct is None:
        return f'<div class="sub">{esc(used_h)} / {esc(total_h)}</div>'
    cls = " warn" if pct > 85 else ""
    return (f'<div class="sub">{esc(used_h)} / {esc(total_h)} · {pct:.0f}%</div>'
            f'<div class="meter{cls}"><i style="width:{min(pct,100):.0f}%"></i></div>')


@router.get("/admin/system", response_class=HTMLResponse)
async def system_page(request: Request, _: bool = Depends(auth.require_admin)):
    snap = sysinfo.snapshot()
    st = get_settings()
    host = request.url.hostname or "127.0.0.1"
    mem, disk, cpu = snap["mem"], snap["disk"], snap["cpu"]
    load = (f'{cpu["load1"]:.2f} / {cpu["load5"]:.2f} / {cpu["load15"]:.2f}'
            if cpu["load1"] is not None else "n/a")

    def pc(v):
        return f"{v:.0f}%" if v is not None else "n/a"

    metrics = f"""
    <div class="metrics">
      <div class="metric"><div class="k">Arbeitsspeicher</div>
        <div class="v">{pc(mem['pct'])}</div>{_meter(mem['pct'], mem['used_h'], mem['total_h'])}
        <div class="sub" style="margin-top:6px">{esc(mem['scope'] or '')}</div></div>
      <div class="metric"><div class="k">Speicherplatz</div>
        <div class="v">{pc(disk['pct'])}</div>{_meter(disk['pct'], disk['used_h'], disk['total_h'])}</div>
      <div class="metric"><div class="k">CPU-Last ({cpu['count']} Kerne)</div>
        <div class="v">{pc(cpu['load_pct'])}</div>
        <div class="sub">load avg: {load}</div></div>
      <div class="metric"><div class="k">Laufzeit</div>
        <div class="v" style="font-size:20px">{esc(snap['uptime_h'])}</div>
        <div class="sub">{snap['procs'] or '?'} Prozesse · Stand {esc(snap['ts'])}</div></div>
    </div>"""

    recs = "".join(
        f'<div class="rec {r["level"]}">{"⚠️" if r["level"]=="warn" else "✅"}&nbsp; {esc(r["text"])}</div>'
        for r in snap["recommendations"])

    services = [
        ("ASTRA Admin", f"http://{host}:8088/admin", True),
        ("Status-Dashboard", f"http://{host}:8088/dashboard", True),
        ("n8n (Workflows)", f"http://{host}:5678", False),
        ("WAHA (WhatsApp)", f"http://{host}:3000", False),
        ("Langfuse (Tracing)", f"http://{host}:3001", False),
        ("Signal-API", f"http://{host}:8080", False),
    ]
    svc_rows = "".join(
        f'<div class="svc"><span class="dot {"up" if internal else ""}"></span>'
        f'<div style="flex:1"><div class="nm">{esc(nm)}</div><div class="u">{esc(url)}</div></div>'
        f'<a class="btn ghost sm" target="_blank" rel="noopener" href="{esc(url)}">Öffnen ↗</a></div>'
        for nm, url, internal in services)

    body = f"""
    <div class="hero"><h1>System</h1>
      <p>Container-Leistung, Empfehlungen und alle Dienste auf diesem Server.</p></div>
    <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);margin:0 0 12px">Leistung</h2>
    {metrics}
    <div style="margin:18px 0 26px">{recs}</div>
    <div class="panel" style="margin-bottom:18px">
      <h2 style="margin:0 0 4px;font-size:15px">Dienste &amp; URLs</h2>
      <p class="note" style="margin:0 0 8px">Vom Server vergebene Adressen. Interne Ports nur im LAN erreichbar.</p>
      {svc_rows}
    </div>
    <p class="note">Modell aktiv: <b>{esc(st.openai_model)}</b> (überschreibbar in den Einstellungen) ·
      <a href="/admin/update">Updates &amp; Versionen →</a></p>
    <script>setTimeout(() => location.reload(), 15000);</script>"""
    return HTMLResponse(page("System", body, active="system"))


# ─── Chat: multi-thread owner agent ────────────────────────────────────────────
CHAT_KEY = "web_chats_v2"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _msg(role: str, content: str, **extra) -> dict:
    return {"id": f"m_{uuid4().hex[:10]}", "role": role, "content": content, "ts": _now_iso(), **extra}


def _new_chat(title: str = "Neuer Chat", *, messages: list[dict] | None = None) -> dict:
    now = _now_iso()
    return {
        "id": f"c_{uuid4().hex[:10]}",
        "title": title,
        "archived": False,
        "permission_mode": "ask",
        "messages": messages or [],
        "pending_action": None,
        "created_at": now,
        "updated_at": now,
    }


def _title_from(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:38] + ("…" if len(text) > 38 else "") if text else "Neuer Chat"


def _normalize_chat(c: dict) -> dict:
    out = _new_chat(c.get("title") or "Neuer Chat")
    out.update({k: c.get(k, out[k]) for k in out})
    out["messages"] = [
        {**_msg(m.get("role", "assistant"), m.get("content", "")), **m}
        for m in (c.get("messages") or [])
    ]
    return out


async def _chat_store() -> dict:
    store = await db.get_setting(CHAT_KEY, None)
    if isinstance(store, dict) and isinstance(store.get("chats"), list):
        chats = [_normalize_chat(c) for c in store["chats"]]
        if not chats:
            chats = [_new_chat()]
        active = store.get("active_id") or chats[0]["id"]
        if not any(c["id"] == active and not c.get("archived") for c in chats):
            active = next((c["id"] for c in chats if not c.get("archived")), chats[0]["id"])
        return {"active_id": active, "chats": chats}

    legacy = await db.get_setting("web_chat", []) or []
    messages = [
        _msg("user" if m.get("role") == "user" else "assistant", m.get("content", ""))
        for m in legacy if isinstance(m, dict)
    ]
    chat = _new_chat(_title_from(messages[0]["content"]) if messages else "Neuer Chat", messages=messages)
    return {"active_id": chat["id"], "chats": [chat]}


async def _save_chat_store(store: dict) -> None:
    await db.set_setting(CHAT_KEY, store)


def _get_chat(store: dict, chat_id: str | None = None) -> dict:
    cid = chat_id or store.get("active_id")
    for chat in store["chats"]:
        if chat["id"] == cid:
            store["active_id"] = chat["id"]
            return chat
    return store["chats"][0]


def _select_chat(store: dict, chat_id: str | None = None, *, archived: bool = False) -> dict | None:
    candidates = [c for c in store["chats"] if bool(c.get("archived")) is archived]
    if chat_id:
        picked = next((c for c in candidates if c["id"] == chat_id), None)
        if picked:
            if not archived:
                store["active_id"] = picked["id"]
            return picked
    if not candidates and not archived:
        fresh = _new_chat()
        store["chats"].insert(0, fresh)
        store["active_id"] = fresh["id"]
        return fresh
    if not candidates:
        return None
    active_id = store.get("active_id")
    picked = next((c for c in candidates if c["id"] == active_id), candidates[0])
    if not archived:
        store["active_id"] = picked["id"]
    return picked


def _chat_messages_for_agent(chat: dict) -> list[dict]:
    return [
        {"role": m["role"], "content": m["content"]}
        for m in chat.get("messages", [])
        if m.get("role") in ("user", "assistant")
    ][-40:]


def _mode_label(mode: str) -> str:
    return {
        "ask": "jedes Mal fragen",
        "auto": "Automodus",
        "bypass": "Berechtigungen umgehen",
    }.get(mode, "Automodus")


def _render_chat_list(store: dict, active_id: str, *, archived: bool = False) -> str:
    rows = []
    for c in store["chats"]:
        if bool(c.get("archived")) is not archived:
            continue
        active = " active" if c["id"] == active_id else ""
        href = f'/admin/chat?{"view=archive&" if archived else ""}chat={esc(c["id"])}'
        thread = (
            f'<a class="thread{active}" href="{href}">'
            f'<span>{esc(c.get("title") or "Neuer Chat")}</span>'
            f'<small>{len(c.get("messages", []))} Nachrichten · {esc(_mode_label(c.get("permission_mode", "ask")))}</small>'
            '</a>'
        )
        if archived:
            rows.append(
                f'<div class="thread-wrap">{thread}'
                f'<button type="button" data-restore-chat="{esc(c["id"])}">Zurück</button></div>'
            )
        else:
            rows.append(thread)
    if not rows:
        rows.append('<div class="arch-note">Keine archivierten Chats.</div>' if archived else '<div class="arch-note">Kein aktiver Chat.</div>')
    return "".join(rows)


def _render_messages(chat: dict) -> str:
    msgs = []
    for m in chat.get("messages", []):
        role = m.get("role", "assistant")
        cls = "user" if role == "user" else "bot" if role == "assistant" else "sys"
        actions = ""
        if role == "user":
            actions = (
                f'<button data-edit="{esc(m["id"])}">Bearbeiten</button>'
                f'<button data-branch="{esc(m["id"])}">Branch</button>'
            )
        elif role == "assistant":
            actions = f'<button data-branch="{esc(m["id"])}">Branch</button>'
        pending = ""
        if m.get("pending_action"):
            p = m["pending_action"]
            pending = (
                '<div class="action-card">'
                f'<div><b>Agentenaktion</b><span>{esc(p.get("tool"))}</span></div>'
                f'<pre>{esc(json.dumps(p.get("args", {}), ensure_ascii=False, indent=2))}</pre>'
                '<div class="row">'
                f'<button class="btn sm" data-run-action="{esc(m["id"])}">Ausführen</button>'
                f'<button class="btn ghost sm" data-deny-action="{esc(m["id"])}">Ablehnen</button>'
                '</div></div>'
            )
        msgs.append(
            f'<div class="msg-row {cls}" data-mid="{esc(m["id"])}">'
            f'<div class="msg {cls}">{esc(m.get("content", ""))}{pending}</div>'
            f'<div class="msg-actions">{actions}</div></div>'
        )
    if not msgs:
        return '<div class="msg sys">Sag Hallo zu ASTRA. Dieser Thread gehört nur dir.</div>'
    return "".join(msgs)


@router.get("/admin/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    _: bool = Depends(auth.require_admin),
    chat: str = "",
    view: str = "",
):
    store = await _chat_store()
    archive_view = view == "archive"
    active = _select_chat(store, chat or None, archived=archive_view)
    await _save_chat_store(store)
    appset = await _app_settings()
    autonomy = appset.get("autonomy", "ask")
    mode = (active or {}).get("permission_mode", "ask")
    active_id = (active or {}).get("id", "")
    title = (active or {}).get("title") or "Archiv"
    active_count = sum(1 for c in store["chats"] if not c.get("archived"))
    archived_count = sum(1 for c in store["chats"] if c.get("archived"))
    title_actions = (
        f'<button class="btn sm" id="restorechat">Wiederherstellen</button>'
        if archive_view and active else
        '<button class="btn ghost sm" id="branchchat">Branch</button>'
    )
    archive_button = "" if archive_view else '<button class="btn ghost sm" id="archivechat">Archivieren</button>'
    input_html = (
        '<div class="chat-input archived"><p>Archivierter Thread. Wiederherstellen, um weiterzuschreiben.</p>'
        '<button class="btn sm" id="restorebottom">Wiederherstellen</button></div>'
        if archive_view and active else
        '<div class="chat-input">'
        '<textarea id="inp" placeholder="Nachricht oder Aufgabe an ASTRA…" rows="1"></textarea>'
        '<button class="btn" id="send">Senden</button>'
        '<button class="btn ghost sm" id="clear" title="Verlauf leeren">Leeren</button>'
        '</div>'
    )
    messages_html = _render_messages(active) if active else '<div class="msg sys">Noch nichts im Archiv.</div>'
    body = f"""
    <div class="chat-shell" data-chat="{esc(active_id)}" data-view="{'archive' if archive_view else 'active'}">
      <aside class="chat-side">
        <div class="side-head"><b>ASTRA Chat</b><button class="btn sm" id="newchat">Neu</button></div>
        <div class="chat-tabs">
          <a class="{'active' if not archive_view else ''}" href="/admin/chat">Aktiv <span>{active_count}</span></a>
          <a class="{'active' if archive_view else ''}" href="/admin/chat?view=archive">Archiv <span>{archived_count}</span></a>
        </div>
        <div class="threads">{_render_chat_list(store, active_id, archived=archive_view)}</div>
        <div class="perm-box">
          <label>Ausführung</label>
          <select id="perm" {"disabled" if archive_view else ""}>
            <option value="ask" {"selected" if mode == "ask" else ""}>Jedes Mal fragen</option>
            <option value="auto" {"selected" if mode == "auto" else ""}>Automodus</option>
            <option value="bypass" {"selected" if mode == "bypass" else ""}>Berechtigungen umgehen</option>
          </select>
          <label>Autonomielevel</label>
          <select id="autonomy" {"disabled" if archive_view else ""}>
            <option value="ask" {"selected" if autonomy == "ask" else ""}>ask</option>
            <option value="confident" {"selected" if autonomy == "confident" else ""}>confident</option>
            <option value="full" {"selected" if autonomy == "full" else ""}>full</option>
          </select>
          <p>Ask pausiert riskante Toolcalls. Bypass gilt nur hier im Owner-Webchat.</p>
        </div>
        {archive_button}
      </aside>
      <section class="chat-main">
        <div class="chat-title">
          <div><span>{"Archivierter Thread" if archive_view else "Thread"}</span><h1>{esc(title)}</h1></div>
          <div class="chat-title-actions">{title_actions}</div>
        </div>
        <div class="chat-log" id="log">{messages_html}</div>
        {input_html}
      </section>
    </div>
    <script>
      const root=document.querySelector('.chat-shell'), chatId=root.dataset.chat, archiveView=root.dataset.view==='archive';
      const log=document.getElementById('log'), inp=document.getElementById('inp');
      const perm=document.getElementById('perm'), autonomy=document.getElementById('autonomy');
      const scroll=()=>log.scrollTop=log.scrollHeight; scroll();
      function add(role,txt){{const r=document.createElement('div');r.className='msg-row '+role;
        const b=document.createElement('div');b.className='msg '+role;b.textContent=txt;r.appendChild(b);log.appendChild(r);scroll();return r;}}
      async function post(url, data){{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data||{{}})}});return await r.json();}}
      async function restore(id){{const d=await post('/admin/chat/restore',{{chat_id:id||chatId}}); location.href='/admin/chat?chat='+encodeURIComponent(d.chat_id||id||chatId);}}
      async function saveSettings(){{if(!archiveView&&chatId) await post('/admin/chat/settings',{{chat_id:chatId,permission_mode:perm.value,autonomy:autonomy.value}});}}
      if(perm) perm.onchange=saveSettings; if(autonomy) autonomy.onchange=saveSettings;
      if(inp) {{
        inp.addEventListener('input',()=>{{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,220)+'px';}});
        inp.addEventListener('keydown',e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();go();}}}});
        document.getElementById('send').onclick=go;
      }}
      async function go(){{
        const t=inp.value.trim(); if(!t) return; inp.value=''; inp.style.height='auto';
        add('user',t); const typing=add('typing','ASTRA arbeitet…');
        try{{
          await saveSettings();
          const d=await post('/admin/chat/send',{{chat_id:chatId,message:t,permission_mode:perm.value}});
          typing.remove(); location.href='/admin/chat?chat='+encodeURIComponent(d.chat_id||chatId);
        }}catch(e){{ typing.remove(); add('bot','Fehler: '+e); }}
      }}
      document.getElementById('newchat').onclick=async()=>{{const d=await post('/admin/chat/new',{{}}); location.href='/admin/chat?chat='+d.chat_id;}};
      const archiveBtn=document.getElementById('archivechat'), branchBtn=document.getElementById('branchchat');
      const clearBtn=document.getElementById('clear'), restoreBtn=document.getElementById('restorechat');
      const restoreBottom=document.getElementById('restorebottom');
      if(archiveBtn) archiveBtn.onclick=async()=>{{await post('/admin/chat/archive',{{chat_id:chatId}}); location.href='/admin/chat?view=archive&chat='+encodeURIComponent(chatId);}};
      if(branchBtn) branchBtn.onclick=async()=>{{const d=await post('/admin/chat/branch',{{chat_id:chatId}}); location.href='/admin/chat?chat='+d.chat_id;}};
      if(clearBtn) clearBtn.onclick=async()=>{{await post('/admin/chat/clear',{{chat_id:chatId}}); location.reload();}};
      if(restoreBtn) restoreBtn.onclick=()=>restore(chatId);
      if(restoreBottom) restoreBottom.onclick=()=>restore(chatId);
      document.querySelectorAll('[data-restore-chat]').forEach(b=>b.onclick=()=>restore(b.dataset.restoreChat));
      log.onclick=async e=>{{
        const edit=e.target.closest('[data-edit]'), branch=e.target.closest('[data-branch]');
        const run=e.target.closest('[data-run-action]'), deny=e.target.closest('[data-deny-action]');
        if(edit){{const current=edit.closest('.msg-row').querySelector('.msg').childNodes[0].textContent;
          const text=prompt('Nachricht bearbeiten. Alles danach wird abgeschnitten:', current);
          if(text!==null){{await post('/admin/chat/edit',{{chat_id:chatId,message_id:edit.dataset.edit,content:text}}); location.reload();}}}}
        if(branch){{const d=await post('/admin/chat/branch',{{chat_id:chatId,message_id:branch.dataset.branch}}); location.href='/admin/chat?chat='+d.chat_id;}}
        if(run){{await post('/admin/chat/action',{{chat_id:chatId,message_id:run.dataset.runAction,decision:'run'}}); location.reload();}}
        if(deny){{await post('/admin/chat/action',{{chat_id:chatId,message_id:deny.dataset.denyAction,decision:'deny'}}); location.reload();}}
      }};
    </script>"""
    return HTMLResponse(page("Chat", body, active="chat"))


@router.post("/admin/chat/settings")
async def chat_settings(request: Request, _: bool = Depends(auth.require_admin)):
    from ..brain import set_autonomy
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    mode = data.get("permission_mode")
    if mode in ("ask", "auto", "bypass"):
        chat["permission_mode"] = mode
    appset = await _app_settings()
    autonomy = data.get("autonomy")
    if autonomy in ("ask", "confident", "full"):
        appset["autonomy"] = autonomy
        set_autonomy(autonomy)
        await db.set_setting("app_settings", appset)
    chat["updated_at"] = _now_iso()
    await _save_chat_store(store)
    return JSONResponse({"ok": True})


@router.post("/admin/chat/new")
async def chat_new(request: Request, _: bool = Depends(auth.require_admin)):
    store = await _chat_store()
    chat = _new_chat()
    store["chats"].insert(0, chat)
    store["active_id"] = chat["id"]
    await _save_chat_store(store)
    return JSONResponse({"chat_id": chat["id"]})


@router.post("/admin/chat/send")
async def chat_send(request: Request, _: bool = Depends(auth.require_admin)):
    from ..agent import generate_reply_meta
    from ..persona import Register
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return JSONResponse({"reply": "(leer)"})
    st = get_settings()
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    if data.get("permission_mode") in ("ask", "auto", "bypass"):
        chat["permission_mode"] = data["permission_mode"]
    user_msg = _msg("user", msg)
    chat["messages"].append(user_msg)
    if len([m for m in chat["messages"] if m["role"] == "user"]) == 1:
        chat["title"] = _title_from(msg)
    try:
        result = await generate_reply_meta(
            register=Register.OWNER,
            contact={"id": "owner", "name": st.astra_owner_name, "is_owner": True},
            thread_id=f"web-owner:{chat['id']}", channel="web",
            history=_chat_messages_for_agent(chat),
            permission_mode=chat.get("permission_mode", "ask"),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("web chat failed")
        result = {"reply": f"Fehler: {e}"}
    bot_msg = _msg("assistant", result.get("reply") or "(keine Antwort)")
    if result.get("pending_action"):
        bot_msg["pending_action"] = result["pending_action"]
        chat["pending_action"] = {"message_id": bot_msg["id"], **result["pending_action"]}
    chat["messages"].append(bot_msg)
    chat["updated_at"] = _now_iso()
    chat["messages"] = chat["messages"][-80:]
    await _save_chat_store(store)
    await db.audit("web_chat", actor="owner", detail={"len": len(msg), "chat_id": chat["id"]})
    return JSONResponse({"reply": bot_msg["content"], "chat_id": chat["id"]})


@router.post("/admin/chat/action")
async def chat_action(request: Request, _: bool = Depends(auth.require_admin)):
    from ..tools import ToolContext, dispatch
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    target = next((m for m in chat["messages"] if m["id"] == data.get("message_id")), None)
    pending = (target or {}).get("pending_action")
    if not target or not pending:
        return JSONResponse({"ok": False, "error": "Keine offene Aktion."})
    if data.get("decision") == "deny":
        target.pop("pending_action", None)
        chat["pending_action"] = None
        chat["messages"].append(_msg("assistant", "Aktion abgelehnt. Ich habe nichts ausgeführt."))
        await _save_chat_store(store)
        return JSONResponse({"ok": True})
    ctx = ToolContext(
        thread_id=f"web-owner:{chat['id']}", channel="web",
        contact={"id": "owner", "is_owner": True}, is_owner=True,
        permission_mode="bypass",
    )
    result = await dispatch(pending["tool"], pending.get("args") or {}, ctx)
    target.pop("pending_action", None)
    chat["pending_action"] = None
    chat["messages"].append(_msg("assistant", f"Ausgeführt: {pending['tool']}\n\n{result}"))
    chat["updated_at"] = _now_iso()
    await _save_chat_store(store)
    return JSONResponse({"ok": True})


@router.post("/admin/chat/edit")
async def chat_edit(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    mid = data.get("message_id")
    for i, m in enumerate(chat["messages"]):
        if m["id"] == mid and m["role"] == "user":
            m["content"] = (data.get("content") or "").strip()
            m["edited_at"] = _now_iso()
            chat["messages"] = chat["messages"][: i + 1]
            chat["pending_action"] = None
            chat["updated_at"] = _now_iso()
            await _save_chat_store(store)
            return JSONResponse({"ok": True})
    return JSONResponse({"ok": False})


@router.post("/admin/chat/branch")
async def chat_branch(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    messages = chat["messages"]
    mid = data.get("message_id")
    if mid:
        for i, m in enumerate(messages):
            if m["id"] == mid:
                messages = messages[: i + 1]
                break
    copied = [{**m, "id": f"m_{uuid4().hex[:10]}", "pending_action": None} for m in messages]
    child = _new_chat(f"{chat.get('title', 'Chat')} / Branch", messages=copied)
    child["permission_mode"] = chat.get("permission_mode", "ask")
    store["chats"].insert(0, child)
    store["active_id"] = child["id"]
    await _save_chat_store(store)
    return JSONResponse({"chat_id": child["id"]})


@router.post("/admin/chat/archive")
async def chat_archive(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    chat["archived"] = True
    store["active_id"] = next((c["id"] for c in store["chats"] if not c.get("archived")), chat["id"])
    await _save_chat_store(store)
    return JSONResponse({"ok": True})


@router.post("/admin/chat/restore")
async def chat_restore(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    chat["archived"] = False
    chat["updated_at"] = _now_iso()
    store["active_id"] = chat["id"]
    await _save_chat_store(store)
    return JSONResponse({"ok": True, "chat_id": chat["id"]})


@router.post("/admin/chat/clear")
async def chat_clear(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    chat["messages"] = []
    chat["pending_action"] = None
    chat["updated_at"] = _now_iso()
    await _save_chat_store(store)
    return JSONResponse({"ok": True})


@router.get("/admin/updates", response_class=HTMLResponse)
async def updates_legacy(_: bool = Depends(auth.require_admin)):
    return RedirectResponse("/admin/update", status_code=303)


@router.get("/admin/update/status")
async def update_status(_: bool = Depends(auth.require_admin)):
    return JSONResponse(await _git_update_status(fetch=False))


@router.post("/admin/update/check")
async def update_check(_: bool = Depends(auth.require_admin)):
    return JSONResponse(await _git_update_status(fetch=True))


@router.post("/admin/update/pull")
async def update_pull(_: bool = Depends(auth.require_admin)):
    return JSONResponse(await _git_pull_update())


# ─── Updates: release notes + hyperspace + GitHub links ───────────────────────
@router.get("/admin/update", response_class=HTMLResponse)
async def updates_page(request: Request, _: bool = Depends(auth.require_admin)):
    body = f"""
    <style>
        .astra-module-container {{
            width: 100%;
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 1rem;
        }}

        /* --- DIE UPDATE KARTE --- */
        .astra-card {{
            position: relative;
            width: 100%;
            min-height: 160px;
            background: var(--bg-1, rgba(10, 11, 16, 0.6));
            border: 1px solid var(--border, rgba(0, 210, 255, 0.15));
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5), inset 0 0 30px rgba(0, 210, 255, 0.04);
            display: flex;
            align-items: center;
            transition: all 0.6s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 1;
        }}

        .hyperspace-canvas {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 0;
            opacity: 0.85;
            transition: opacity 0.5s ease;
        }}

        .card-inner {{
            position: relative;
            z-index: 2;
            width: 100%;
            padding: 32px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 24px;
            transition: opacity 0.4s ease, transform 0.4s ease;
        }}

        /* Gruppierung für die linke Seite (Version + Info) */
        .card-left-group {{
            display: flex;
            align-items: center;
            gap: 36px;
        }}

        /* DIE FETTE VERSIONSNUMMER LINKS (Analog zum Progress Counter) */
        .big-version {{
            display: none;
            font-size: 64px;
            font-weight: 900;
            font-variant-numeric: tabular-nums;
            letter-spacing: -2px;
            line-height: 1;
            background: linear-gradient(to bottom, #ffffff, #a2b4c7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            filter: drop-shadow(0 0 30px rgba(0, 210, 255, 0.4));
        }}

        /* Aktivierungs-Klasse nach dem Update-Prozess */
        .astra-card.has-updated .big-version {{
            display: block;
            animation: slideInLeft 0.6s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }}

        .card-info {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .card-title {{
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -0.02em;
            text-shadow: 0 0 20px rgba(255, 255, 255, 0.2);
            transition: color 0.4s ease;
            margin: 0;
        }}

        .card-subtitle {{
            font-size: 14px;
            color: var(--text-dim, #8e94a6);
            font-weight: 400;
            transition: color 0.4s ease;
            margin: 0;
        }}

        .card-actions {{
            display: flex;
            align-items: center;
            gap: 20px;
        }}

        /* --- BUTTONS & LINKS --- */
        .card-actions .btn-primary {{
            background: linear-gradient(135deg, #00f0ff 0%, #0072ff 100%);
            color: #000;
            border: none;
            padding: 12px 28px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 0 20px rgba(0, 240, 255, 0.3);
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }}

        .card-actions .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 0 30px rgba(0, 240, 255, 0.6);
            background: linear-gradient(135deg, #55f5ff 0%, #338cff 100%);
        }}

        .card-actions .btn-primary:active {{
            transform: translateY(0);
        }}

        .card-actions .link-secondary {{
            color: var(--text-dim, #8e94a6);
            font-size: 14px;
            text-decoration: none;
            cursor: pointer;
            transition: color 0.2s ease;
            background: none;
            border: none;
            font-family: inherit;
        }}

        .card-actions .link-secondary:hover {{
            color: var(--text, #ffffff);
        }}

        /* --- ZUSTAND: CONFIRMATION --- */
        .confirm-wrapper {{
            display: none;
            gap: 12px;
            align-items: center;
            animation: fadeIn 0.3s ease forwards;
        }}

        .confirm-text {{
            font-size: 14px;
            color: #00d2ff;
            font-weight: 500;
            margin-right: 8px;
        }}

        .card-actions .btn-danger {{
            background: transparent;
            color: #ff4a4a;
            border: 1px solid rgba(255, 74, 74, 0.3);
            padding: 10px 18px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        }}

        .card-actions .btn-danger:hover {{
            background: rgba(255, 74, 74, 0.1);
            border-color: rgba(255, 74, 74, 0.6);
        }}

        /* --- ZUSTAND: UPDATING SCREEN --- */
        .progress-overlay {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 3;
            opacity: 0;
            pointer-events: none;
            transform: scale(0.95);
            transition: all 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }}

        .progress-counter {{
            font-size: 72px;
            font-weight: 900;
            font-variant-numeric: tabular-nums;
            letter-spacing: -3px;
            background: linear-gradient(to bottom, #ffffff, #a2b4c7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            filter: drop-shadow(0 0 30px rgba(0, 210, 255, 0.4));
        }}

        /* --- RELEASE NOTES AREA --- */
        .release-notes-wrapper {{
            display: grid;
            grid-template-rows: 0fr;
            transition: grid-template-rows 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }}

        .release-notes-wrapper.is-open {{
            grid-template-rows: 1fr;
        }}

        .release-notes-content {{
            overflow: hidden;
        }}

        .release-notes-card {{
            background: var(--bg-2, rgba(10, 11, 16, 0.3));
            border: 1px solid var(--border, rgba(255, 255, 255, 0.04));
            border-radius: 12px;
            padding: 24px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            margin-bottom: 4px;
        }}

        .notes-header {{
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: #00d2ff;
            font-weight: 700;
        }}

        .commit-list {{
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 10px;
            padding: 0;
            margin: 0;
        }}

        .commit-item {{
            font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
            font-size: 13px;
            color: var(--text-dim, #8e94a6);
            display: flex;
            align-items: flex-start;
            gap: 8px;
            line-height: 1.5;
        }}

        .commit-item::before {{
            content: "•";
            color: #00d2ff;
            font-weight: bold;
        }}

        .commit-item span.feat {{ color: #50e3c2; }}
        .commit-item span.fix {{ color: #ff4a4a; }}
        .commit-item span.chore {{ color: #a2b4c7; }}

        .astra-card.is-updating .card-inner {{
            opacity: 0;
            transform: scale(0.95);
            pointer-events: none;
        }}

        .astra-card.is-updating .progress-overlay {{
            opacity: 1;
            transform: scale(1);
            pointer-events: auto;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateX(10px); }}
            to {{ opacity: 1; transform: translateX(0); }}
        }}

        @keyframes slideInLeft {{
            from {{ opacity: 0; transform: translateX(-20px); }}
            to {{ opacity: 1; transform: translateX(0); }}
        }}

        @media (max-width: 768px) {{
            .card-inner {{ flex-direction: column; align-items: flex-start; padding: 24px; gap: 20px; }}
            .card-left-group {{ flex-direction: column; align-items: flex-start; gap: 16px; width: 100%; }}
            .card-actions {{ width: 100%; flex-direction: column-reverse; align-items: stretch; gap: 16px; }}
            .confirm-wrapper {{ flex-direction: column; align-items: stretch; }}
            .confirm-text {{ text-align: center; margin-bottom: 4px; }}
            .card-actions .btn-primary, .card-actions .link-secondary, .card-actions .btn-danger {{ text-align: center; width: 100%; padding: 14px; }}
            .progress-counter {{ font-size: 54px; }}
            .big-version {{ font-size: 52px; }}
        }}
    </style>

    <div class="hero" style="margin-bottom:0px">
      <h1>Updates</h1>
      <p>Neueste Änderungen aus dem Repository holen.</p>
    </div>

    <div class="astra-module-container">
        
        <div class="astra-card" id="astraCard">
            <canvas class="hyperspace-canvas" id="spaceCanvas"></canvas>
            
            <div class="card-inner" id="cardInner">
                <div class="card-left-group">
                    <div class="big-version" id="bigVersion"></div>
                    <div class="card-info">
                        <h2 class="card-title" id="cardTitle">Update verfügbar</h2>
                        <p class="card-subtitle" id="cardSubtitle">Verbindung zu GitHub wird aufgebaut...</p>
                    </div>
                </div>
                
                <div class="card-actions">
                    <button class="link-secondary" id="toggleNotesBtn" onclick="toggleReleaseNotes()">Versionshinweise lesen</button>
                    <button class="btn-primary" id="mainUpdateBtn" onclick="showConfirmation()">Update starten</button>
                    
                    <div class="confirm-wrapper" id="confirmWrapper">
                        <span class="confirm-text">Sicher updaten?</span>
                        <button class="btn-danger" onclick="cancelUpdate()">Abbrechen</button>
                        <button class="btn-primary" onclick="startUpdateProcess()">Updaten</button>
                    </div>
                </div>
            </div>

            <div class="progress-overlay" id="progressOverlay">
                <div class="progress-counter" id="progressNumber">0%</div>
            </div>
        </div>

        <div class="release-notes-wrapper" id="releaseNotesWrapper">
            <div class="release-notes-content">
                <div class="release-notes-card">
                    <div class="notes-header">Changelog / Neueste GitHub Commits</div>
                    <ul class="commit-list" id="commitList">
                        <li class="commit-item"><span class="chore">Lade...</span> Commits werden abgerufen...</li>
                    </ul>
                </div>
            </div>
        </div>

        <div class="panel" style="margin-top:20px">
          <h2 style="margin:0 0 6px;font-size:15px">Manuelles Update</h2>
          <p class="note" style="margin:0 0 8px">Auf dem Server im Terminal ausführen:</p>
          <pre style="background:var(--bg-2);border:1px solid var(--border);border-radius:var(--r-sm);
            padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:13px;overflow-x:auto;margin:0">cd /opt/astra &amp;&amp; git pull origin main &amp;&amp; docker compose up -d --build cortex</pre>
        </div>

    </div>

    <script>
        const GITHUB_REPO = 'ProfessorEngineergit/ASTRA'; 
        let TARGET_VERSION = 'Latest'; 

        const canvas = document.getElementById('spaceCanvas');
        const ctx = canvas.getContext('2d');
        let animationFrameId;
        let stars = [];
        const numStars = 180;
        let speedSettings = {{ current: 0.8, target: 0.8, normal: 0.8, hyper: 22 }};

        function resizeCanvas() {{
            const rect = canvas.getBoundingClientRect();
            if(!rect.width) return;
            canvas.width = rect.width * (window.devicePixelRatio || 1);
            canvas.height = rect.height * (window.devicePixelRatio || 1);
            ctx.scale(window.devicePixelRatio || 1, window.devicePixelRatio || 1);
            initStars();
        }}

        function initStars() {{
            stars = [];
            const dpr = window.devicePixelRatio || 1;
            const w = canvas.width / dpr;
            const h = canvas.height / dpr;
            for (let i = 0; i < numStars; i++) {{
                stars.push({{
                    x: (Math.random() - 0.5) * w * 2,
                    y: (Math.random() - 0.5) * h * 2,
                    z: Math.random() * w,
                    ox: 0, oy: 0
                }});
            }}
        }}

        function updateAndDrawStars() {{
            const dpr = window.devicePixelRatio || 1;
            const w = canvas.width / dpr;
            const h = canvas.height / dpr;
            const cx = w / 2; const cy = h / 2;

            ctx.fillStyle = `rgba(10, 11, 16, ${{speedSettings.current > 5 ? 0.22 : 0.45}})`;
            ctx.fillRect(0, 0, w, h);
            speedSettings.current += (speedSettings.target - speedSettings.current) * 0.04;

            for (let i = 0; i < stars.length; i++) {{
                let s = stars[i];
                let k = 128 / s.z;
                let px = s.x * k + cx; let py = s.y * k + cy;
                s.z -= speedSettings.current;

                if (s.z <= 0 || px < 0 || px > w || py < 0 || py > h) {{
                    s.z = w;
                    s.x = (Math.random() - 0.5) * w * 2;
                    s.y = (Math.random() - 0.5) * h * 2;
                    px = s.ox = s.x * (128 / s.z) + cx;
                    py = s.oy = s.y * (128 / s.z) + cy;
                }}

                let k2 = 128 / s.z;
                let nx = s.x * k2 + cx; let ny = s.y * k2 + cy;

                if (s.ox !== 0) {{
                    ctx.beginPath();
                    if (speedSettings.current > 5) {{
                        ctx.strokeStyle = `rgba(160, 230, 255, ${{Math.min(1, (w - s.z) / w)}})`;
                        ctx.lineWidth = Math.min(2.5, (1 - s.z / w) * 3);
                    }} else {{
                        ctx.strokeStyle = `rgba(180, 210, 255, ${{Math.min(0.6, (w - s.z) / w)}})`;
                        ctx.lineWidth = 1;
                    }}
                    ctx.moveTo(px, py); ctx.lineTo(nx, ny);
                    ctx.stroke();
                }}
                s.ox = nx; s.oy = ny;
            }}
            animationFrameId = requestAnimationFrame(updateAndDrawStars);
        }}

        window.addEventListener('resize', resizeCanvas);
        setTimeout(resizeCanvas, 100);
        updateAndDrawStars();

        function renderCommitLines(lines) {{
            const commitListEl = document.getElementById('commitList');
            commitListEl.innerHTML = '';
            const useLines = lines && lines.length ? lines : ['Keine entfernten Commits im lokalen Cache.'];
            useLines.slice(0, 6).forEach(line => {{
                const li = document.createElement('li');
                li.className = 'commit-item';
                const kind = line.includes(' feat') || line.startsWith('feat') ? 'feat'
                    : line.includes(' fix') || line.startsWith('fix') ? 'fix'
                    : 'chore';
                const span = document.createElement('span');
                span.className = kind;
                span.textContent = kind + ':';
                li.appendChild(span);
                li.appendChild(document.createTextNode(' ' + line.replace(/^[a-f0-9]{{7,}}\\s+/, '')));
                commitListEl.appendChild(li);
            }});
        }}

        function applyUpdateStatus(d) {{
            TARGET_VERSION = d.target_version || d.remote_sha || d.current_version || 'Latest';
            bigVersion.textContent = TARGET_VERSION;
            cardTitle.textContent = d.update_available ? 'Update verfügbar' : d.ok ? 'ASTRA ist aktuell' : 'Update nicht bereit';
            cardSubtitle.textContent = d.ok
                ? `Lokal ${{d.current_version || '?'}} · Remote ${{d.target_version || d.remote_sha || '?'}} · ${{d.release_mode || 'commit'}}`
                : (d.message || 'Git-Status konnte nicht gelesen werden.');
            mainUpdateBtn.disabled = !d.ok || d.dirty || !d.update_available;
            mainUpdateBtn.textContent = d.update_available ? 'Update starten' : d.dirty ? 'Lokale Änderungen' : 'Aktuell';
            if (d.dirty) {{
                cardSubtitle.textContent = 'Lokale Git-Änderungen blockieren Pulls. Erst committen/stashen/aufräumen.';
            }}
            renderCommitLines(d.commits || []);
        }}

        /* --- UPDATE STATUS FETCH --- */
        async function fetchGitHubCommits() {{
            const subtitleEl = document.getElementById('cardSubtitle');

            try {{
                const response = await fetch('/admin/update/status');
                if (response.ok) {{
                    applyUpdateStatus(await response.json());
                }}
            }} catch (error) {{
                console.error("Update-Status fehlgeschlagen", error);
                subtitleEl.textContent = "Update-Status konnte nicht gelesen werden.";
            }}
        }}

        document.addEventListener('DOMContentLoaded', fetchGitHubCommits);

        const card = document.getElementById('astraCard');
        const mainUpdateBtn = document.getElementById('mainUpdateBtn');
        const confirmWrapper = document.getElementById('confirmWrapper');
        const releaseNotesWrapper = document.getElementById('releaseNotesWrapper');
        const progressOverlay = document.getElementById('progressOverlay');
        const progressNumber = document.getElementById('progressNumber');
        const cardTitle = document.getElementById('cardTitle');
        const cardSubtitle = document.getElementById('cardSubtitle');
        const toggleNotesBtn = document.getElementById('toggleNotesBtn');
        const bigVersion = document.getElementById('bigVersion');

        let isNotesOpen = false;

        function toggleReleaseNotes() {{
            isNotesOpen = !isNotesOpen;
            if (isNotesOpen) {{
                releaseNotesWrapper.classList.add('is-open');
                toggleNotesBtn.textContent = "Versionshinweise schließen";
            }} else {{
                releaseNotesWrapper.classList.remove('is-open');
                toggleNotesBtn.textContent = "Versionshinweise lesen";
            }}
        }}

        function showConfirmation() {{
            mainUpdateBtn.style.display = 'none';
            confirmWrapper.style.display = 'flex';
        }}

        function cancelUpdate() {{
            confirmWrapper.style.display = 'none';
            mainUpdateBtn.style.display = 'block';
        }}

        async function startUpdateProcess() {{
            if (isNotesOpen) toggleReleaseNotes();
            toggleNotesBtn.style.display = 'none';

            speedSettings.target = speedSettings.hyper;
            card.classList.add('is-updating');

            let currentPercent = 0;
            let done = false;
            let result = null;
            function simulateProgress() {{
                if (done) {{
                    currentPercent = 100;
                    progressNumber.textContent = `${{currentPercent}}%`;
                    showReloadState(result);
                }} else {{
                    let increment = Math.floor(Math.random() * 4) + 1;
                    if (currentPercent > 76) increment = Math.random() > 0.55 ? 1 : 0;
                    currentPercent = Math.min(96, currentPercent + increment);
                    progressNumber.textContent = `${{currentPercent}}%`;
                    setTimeout(simulateProgress, Math.random() * 80 + 40);
                }}
            }}
            setTimeout(simulateProgress, 800);
            try {{
                const response = await fetch('/admin/update/pull', {{method: 'POST'}});
                result = await response.json();
            }} catch (error) {{
                result = {{ok:false, message:String(error)}};
            }}
            done = true;
        }}

        function showReloadState(result) {{
            speedSettings.target = speedSettings.normal;

            setTimeout(() => {{
                confirmWrapper.style.display = 'none';
                mainUpdateBtn.textContent = "Status neu laden";
                mainUpdateBtn.setAttribute('onclick', 'location.reload()');
                mainUpdateBtn.style.display = 'block';
                
                card.classList.remove('is-updating');
                cardTitle.textContent = result && result.ok ? "Update bereitgestellt" : "Update fehlgeschlagen";
                cardSubtitle.textContent = result && result.ok
                    ? "Git pull wurde ausgeführt. Bei Docker-Deployments danach Container neu bauen/starten."
                    : ((result && result.message) || "Git pull konnte nicht ausgeführt werden.");
                
                if (result) {{
                    renderCommitLines(result.commits || []);
                    if (result.target_version || result.current_version) {{
                        TARGET_VERSION = result.target_version || result.current_version;
                    }}
                }}
                bigVersion.textContent = TARGET_VERSION;
                card.classList.add('has-updated');
                
            }}, 600);
        }}
    </script>
    """
    return HTMLResponse(page("Updates", body, active="update"))
