"""Web admin router: first-run setup, login, plugin catalog + config forms."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import __version__ as ASTRA_VERSION
from .. import db, knowledge, sysinfo
from ..config import get_settings
from ..config_store import SECRET_SENTINEL, get_config_store
from ..google_oauth import authorization_url, exchange_code, token_patch, user_email
from ..plugins.base import CATEGORY_LABELS, FieldType
from ..plugins.registry import get_manager
from ..secretary import CHANNEL_LABELS, resolve_service_status, secretary_settings
from . import auth
from ..models import (
    ROLES,
    model_config_snapshot,
    protect_api_key,
    set_model_config,
    set_model_override,
)
from ..plugins import extended_catalog
from .templates import (
    LOGO_LONG, brand_icon, esc, font_choices, font_live_specs, icon_html, page, set_font, set_theme,
    theme_choices,
)

GH_REPO = "https://github.com/ProfessorEngineergit/ASTRA"
GH_OWNER_REPO = "ProfessorEngineergit/ASTRA"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATE_PULL_COMMAND = ["git", "pull", "--ff-only", "origin", "main"]
UPDATE_FETCH_COMMAND = ["git", "fetch", "--tags", "origin", "main"]
UPDATE_REBUILD_COMMAND = "docker compose up -d --build cortex"
_GH_SVG = (
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<circle cx="6" cy="5" r="2.25" stroke="currentColor" stroke-width="1.8"/>'
    '<circle cx="6" cy="19" r="2.25" stroke="currentColor" stroke-width="1.8"/>'
    '<circle cx="18" cy="7" r="2.25" stroke="currentColor" stroke-width="1.8"/>'
    '<path d="M6 7.5v9M8.25 7h3.25A6.5 6.5 0 0 1 18 13.5V16" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
)

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


def _repo_root_candidates() -> list[Path]:
    paths: list[Path] = []

    def add(raw: str | os.PathLike | None) -> None:
        if not raw:
            return
        try:
            path = Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            path = Path(raw).expanduser()
        if path not in paths:
            paths.append(path)

    add(os.getenv("ASTRA_UPDATE_REPO_ROOT"))
    add(os.getenv("ASTRA_REPO_ROOT"))
    add(DEFAULT_REPO_ROOT)
    add(Path.cwd())
    for parent in Path(__file__).resolve().parents:
        add(parent)
    for common in ("/opt/astra", "/srv/astra", "/workspace", "/app", "/srv"):
        add(common)
    return paths


def _repo_root() -> Path:
    candidates = _repo_root_candidates()
    for path in candidates:
        if (path / ".git").exists():
            return path
    return candidates[0] if candidates else DEFAULT_REPO_ROOT


async def _run_update_cmd(args: list[str], *, timeout: float = 12, cwd: Path | None = None) -> dict:
    """Run a fixed maintenance command from the repo root."""
    if args and args[0] == "git" and not which("git"):
        return {"ok": False, "code": 127, "out": "", "err": "git ist auf diesem Server nicht installiert."}
    workdir = cwd or _repo_root()
    run_args = list(args)
    if run_args and run_args[0] == "git":
        run_args = ["git", "-c", f"safe.directory={workdir}", *run_args[1:]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *run_args,
            cwd=workdir,
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
    repo_root = _repo_root()
    candidates = [str(p) for p in _repo_root_candidates()]
    if not which("git"):
        return {
            "ok": False,
            "git_available": False,
            "repo_root": str(repo_root),
            "repo_candidates": candidates,
            "app_version": ASTRA_VERSION,
            "message": "git ist im ASTRA-Container nicht installiert. Das neue Image installiert git automatisch.",
        }

    repo_check = await _run_update_cmd(["git", "rev-parse", "--show-toplevel"], timeout=5, cwd=repo_root)
    if not repo_check["ok"]:
        return {
            "ok": False,
            "git_available": True,
            "repo_root": str(repo_root),
            "repo_candidates": candidates,
            "app_version": ASTRA_VERSION,
            "git_error": repo_check.get("err") or repo_check.get("out"),
            "message": (
                "ASTRA findet im Container keinen Git-Checkout. Mount den Server-Checkout nach "
                "/opt/astra oder setze ASTRA_UPDATE_REPO_ROOT auf den Repo-Pfad."
            ),
        }
    repo_root = Path(repo_check["out"] or repo_root)

    fetch_result = None
    if fetch:
        fetch_result = await _run_update_cmd(
            UPDATE_FETCH_COMMAND, timeout=35, cwd=repo_root
        )

    local_full = (
        await _run_update_cmd(["git", "rev-parse", "HEAD"], timeout=8, cwd=repo_root)
    )["out"].strip()
    remote_full = (
        await _run_update_cmd(["git", "rev-parse", "origin/main"], timeout=8, cwd=repo_root)
    )["out"].strip()
    local_short = (
        await _run_update_cmd(["git", "rev-parse", "--short", "HEAD"], timeout=8, cwd=repo_root)
    )["out"].strip()
    remote_short = (
        await _run_update_cmd(["git", "rev-parse", "--short", "origin/main"], timeout=8, cwd=repo_root)
    )["out"].strip()
    local_tag = (
        await _run_update_cmd(["git", "describe", "--tags", "--exact-match", "HEAD"], timeout=8, cwd=repo_root)
    )["out"].strip()
    local_nearest_tag = (
        await _run_update_cmd(["git", "describe", "--tags", "--abbrev=0", "HEAD"], timeout=8, cwd=repo_root)
    )["out"].strip()
    latest_tag = (
        await _run_update_cmd(["git", "describe", "--tags", "--abbrev=0", "origin/main"], timeout=8, cwd=repo_root)
    )["out"].strip()
    ahead_behind = (
        await _run_update_cmd(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
            timeout=8,
            cwd=repo_root,
        )
    )["out"].strip()
    tracked_status = (
        await _run_update_cmd(["git", "status", "--short", "--untracked-files=no"], timeout=8, cwd=repo_root)
    )["out"].strip()
    untracked_status = (
        await _run_update_cmd(["git", "ls-files", "--others", "--exclude-standard"], timeout=8, cwd=repo_root)
    )["out"].strip()
    dirty = bool(tracked_status)
    commit_lines = (
        await _run_update_cmd(
            ["git", "log", "--oneline", "--no-decorate", "--max-count=6", "HEAD..origin/main"],
            timeout=8,
            cwd=repo_root,
        )
    )["out"].strip()
    ahead = behind = 0
    if ahead_behind:
        parts = ahead_behind.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    diverged = ahead > 0 and behind > 0
    target_version = latest_tag or remote_short or "unbekannt"
    current_version = local_tag or (
        f"{local_nearest_tag}+{local_short}" if local_nearest_tag and local_short else
        f"v{ASTRA_VERSION}+{local_short}" if local_short else f"v{ASTRA_VERSION}"
    )
    data = {
        "ok": True,
        "git_available": True,
        "repo_root": str(repo_root),
        "repo_candidates": candidates,
        "repo": GH_OWNER_REPO,
        "app_version": ASTRA_VERSION,
        "current_sha": local_short,
        "remote_sha": remote_short,
        "current_version": current_version,
        "target_version": target_version,
        "release_mode": "release-tag" if latest_tag else "commit-fallback",
        "update_available": behind > 0,
        "diverged": diverged,
        "can_update": behind > 0 and not dirty,
        "sync_strategy": "backup-reset" if diverged else "fast-forward",
        "ahead": ahead,
        "behind": behind,
        "dirty": dirty,
        "tracked_changes": [line for line in tracked_status.splitlines() if line],
        "untracked_count": len([line for line in untracked_status.splitlines() if line]),
        "commits": [line for line in commit_lines.splitlines() if line],
        "pull_command": " ".join(UPDATE_PULL_COMMAND),
        "fetch_command": " ".join(UPDATE_FETCH_COMMAND),
        "rebuild_command": UPDATE_REBUILD_COMMAND,
        "release_note": (
            "Die Karte bevorzugt GitHub Releases/Tags. Ohne Tag nutzt ASTRA den neuesten Commit als Version."
        ),
    }
    if diverged:
        data["message"] = (
            "Lokaler und entfernter Branch sind divergiert. ASTRA kann vor dem Sync eine Backup-Branch "
            "anlegen und dann origin/main auschecken."
        )
    if fetch_result is not None:
        data["fetch"] = {
            "ok": fetch_result["ok"],
            "code": fetch_result["code"],
            "out": fetch_result["out"][-3000:],
            "err": fetch_result["err"][-3000:],
        }
    return data


async def _git_pull_update() -> dict:
    before = await _git_update_status(fetch=True)
    if not before.get("ok"):
        return before
    if before.get("dirty"):
        return {
            **before,
            "ok": False,
            "message": "Lokale Git-Änderungen blockieren den Pull. Erst committen, stashen oder bewusst aufräumen.",
        }
    if not before.get("update_available"):
        return {**before, "ok": True, "message": "ASTRA ist bereits aktuell."}
    repo_root = Path(before["repo_root"])
    backup = None
    steps: list[dict] = []
    if before.get("diverged"):
        backup = "server-backup/" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch_result = await _run_update_cmd(["git", "branch", backup, "HEAD"], timeout=12, cwd=repo_root)
        steps.append({"step": "backup_branch", "branch": backup, **branch_result})
        if not branch_result["ok"]:
            after = await _git_update_status(fetch=False)
            return {
                **after,
                "ok": False,
                "backup_branch": backup,
                "steps": steps,
                "message": "Backup-Branch konnte nicht angelegt werden. Update abgebrochen.",
            }
        result = await _run_update_cmd(["git", "reset", "--hard", "origin/main"], timeout=45, cwd=repo_root)
        steps.append({"step": "reset_origin_main", **result})
    else:
        result = await _run_update_cmd(UPDATE_PULL_COMMAND, timeout=75, cwd=repo_root)
        steps.append({"step": "fast_forward_pull", **result})
    after = await _git_update_status(fetch=False)
    payload = {
        **after,
        "ok": result["ok"],
        "sync_strategy": "backup-reset" if before.get("diverged") else "fast-forward",
        "backup_branch": backup,
        "steps": [
            {k: (v[-3000:] if k in {"out", "err"} and isinstance(v, str) else v) for k, v in step.items()}
            for step in steps
        ],
        "pull": {
            "code": result["code"],
            "out": result["out"][-6000:],
            "err": result["err"][-6000:],
        },
        "message": (
            "Update synchronisiert." if result["ok"] and not backup else
            f"Update synchronisiert. Lokaler Altstand liegt auf {backup}." if result["ok"] else
            "Update-Sync ist fehlgeschlagen."
        ),
    }
    await db.audit(
        "self_update_pull",
        actor="owner",
        detail={"ok": result["ok"], "code": result["code"], "strategy": payload["sync_strategy"], "backup": backup},
    )
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
        <h2>Willkommen</h2>
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
    "theme": "event_horizon",
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
        labs.update({k: v for k, v in stored.items() if k in _DEFAULT_LABS and v not in (None, "")})
    if s.get("font") and not stored.get("font"):
        labs["font"] = s["font"]
    return labs


def _area_for(slug: str) -> dict:
    if slug in _AREA_META:
        return _AREA_META[slug]
    if not slug.startswith("cat_") and f"cat_{slug}" in _AREA_META:
        return _AREA_META[f"cat_{slug}"]
    if slug.startswith("cat_") and slug[4:] in _AREA_META:
        return _AREA_META[slug[4:]]
    return {"global": True, "label": "global"}


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


def _theme_picker(selected: str) -> str:
    cards = []
    for key, theme in theme_choices():
        checked = " checked" if key == selected else ""
        cards.append(
            f'<label class="theme-card" style="--preview-accent:{esc(theme["accent"])};'
            f'--preview-link:{esc(theme["link"])};--preview-signal:{esc(theme["signal"])};'
            f'--preview-radius:{esc(theme["radius"])}">'
            f'<input type="radio" name="lab_theme" value="{esc(key)}"{checked}>'
            '<span class="theme-card-visual"><i></i><i></i><i></i></span>'
            f'<span class="theme-card-copy"><b>{esc(theme["name"])}</b>'
            f'<small>{esc(theme["desc"])}</small></span></label>'
        )
    return '<div class="theme-picker" id="themePicker">' + "".join(cards) + "</div>"


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


def _card_html(p, is_fav: bool, installation_count: int = 1) -> str:
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
    install_tag = (
        f'<span class="badge b-off">{installation_count} Installationen</span>'
        if installation_count > 1 else ""
    )
    # Messenger plugins double as Secretary channels — flag them.
    sec_tag = ('<span class="tag-katalog" title="Auch als Secretary-Kanal nutzbar">Secretary</span>'
               if getattr(p.category, "value", "") == "comms" else "")
    return f"""
        <div class="card {'on' if p.enabled else ''}"
             data-slug="{esc(p.slug)}"
             data-name="{esc((p.name + ' ' + p.description).lower())}"
             data-cat="{p.category.value}" data-source="nativ" data-fav="{'1' if is_fav else '0'}"
             {_area_attrs(p.slug)}>
          <div class="top">
            {icon_html(p.slug, p.icon)}
            <div class="meta">
              <h3>{esc(p.name)} <span class="tag-nativ">nativ</span>{sec_tag}</h3>
              <div class="cat">{cat_label}</div>
            </div>
            <button class="star {'on' if is_fav else ''}" data-slug="{esc(p.slug)}"
                    title="Favorit">★</button>
          </div>
          <p>{esc(p.description)}</p>
          <div class="row">{badge}{install_tag}{action}</div>
        </div>"""


def _catalog_card_html(e) -> str:
    """Catalog-only fallback card for entries without runtime tools."""
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
          <div class="row"><span class="badge b-soon">nur im Katalog</span>
            <a class="btn ghost sm" style="margin-left:auto" target="_blank" rel="noopener"
               href="{issue}">Anfragen ↗</a></div>
        </div>"""


@router.get("/admin", response_class=HTMLResponse)
async def catalog(request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    appset = await _app_settings()
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
        cards = "".join(_card_html(p, p.slug in favs, len(mgr.installations(p.slug))) for p in members)
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

    catalog_style = ".tag-nativ,.tag-katalog{display:none}"

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
    <style>{catalog_style}</style>
    <div class="hero">
      <h1>Deine <span class="grad">Integrationen</span></h1>
      <p>Verbinde ASTRA mit deiner Welt — Verkehr, Smart Home, Server, Messenger und mehr.
         <span class="note">Über {n_total} Dienste im Katalog.</span></p>
      <div class="stats">
        <div class="stat"><span class="dot" style="background:var(--ok)"></span><b>{n_active}</b> aktiv</div>
        <div class="stat"><span class="dot" style="background:var(--link)"></span><b>{n_ready}</b> startklar</div>
        <div class="stat"><span class="dot" style="background:var(--theme-signal)"></span><b>{len(plugins)}</b> nativ</div>
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
                      saved: str = "", installation: str = "default"):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    inst = mgr.get(slug)
    if not cls or not inst:
        return HTMLResponse(page("?", '<div class="flash err">Unbekanntes Plugin.</div>'), 404)
    store = get_config_store()
    installations = mgr.installations(slug)
    active_inst = next((p for p in installations if p.installation_id == installation), installations[0])
    meta = await store.installation_meta(cls, active_inst.installation_id)
    token = await auth.issue_csrf()
    flash = '<div class="flash ok">Gespeichert.</div>' if saved else ""

    fields_html = ""
    for f in cls.config_fields:
        val = "" if f.secret else active_inst.get(f.key, f.default)
        help_ = f'<div class="help">{esc(f.help)}</div>' if f.help else ""
        req = ' <span class="req">*</span>' if f.required else ""
        fields_html += (f'<div class="field"><label>{esc(f.label)}{req}</label>'
                        f'{_field_input(f, val, meta.get(f.key, False))}{help_}</div>')

    soon = getattr(cls, "coming_soon", False)
    soon_banner = ('<div class="flash err">Dieses Plugin ist im Katalog gelistet, aber noch '
                   'nicht implementiert. Sag ASTRA, wenn du es priorisiert haben möchtest.</div>'
                   if soon else "")
    cat_label = esc(CATEGORY_LABELS.get(cls.category, cls.category.value))
    toggled = "checked" if inst.is_toggled_on else ""
    install_rows = []
    for p in installations:
        selected = " active" if p.installation_id == active_inst.installation_id else ""
        state = "aktiv" if p.enabled else "aus" if p.has_required else "unvollständig"
        href = f"/admin/plugin/{esc(slug)}?installation={esc(p.installation_id)}"
        install_rows.append(
            f'<a class="install-card{selected}" href="{href}">'
            f'<strong>{esc(p.installation_name)}</strong>'
            f'<span>{esc(state)} · {esc(p.runtime_slug)}</span></a>'
        )
    create_href = f"/admin/plugin/{esc(slug)}/installation/new"
    oauth_html = ""
    scopes = getattr(cls, "google_scopes", [])
    if scopes:
        connected = active_inst.get("account_email") or ("Token gesetzt" if meta.get("refresh_token") else "nicht verbunden")
        oauth_html = f"""
        <div class="panel" style="margin-top:14px">
          <div class="row" style="justify-content:space-between;align-items:center">
            <div><h2 style="margin:0;font-size:16px">Google OAuth</h2>
              <div class="note">Status: {esc(connected)} · Redirect: {esc(str(request.url_for("oauth_google_callback")))}</div></div>
            <form method="post" action="/admin/plugin/{esc(slug)}/oauth/google/start" style="margin:0">
              <input type="hidden" name="csrf" value="{esc(token)}">
              <input type="hidden" name="installation_id" value="{esc(active_inst.installation_id)}">
              <button class="btn sm" type="submit">Mit Google verbinden</button>
            </form>
          </div>
        </div>"""
    test_btn = ('' if soon else
                '<button class="btn secondary" type="button" id="testbtn">Verbindung testen</button>'
                '<span id="testresult" class="note"></span>')
    save_btn = ('' if soon else '<button class="btn" type="submit">Speichern</button>')
    test_script = '' if soon else f"""
    <script>
      document.getElementById('testbtn').onclick=async()=>{{
        const btn=document.getElementById('testbtn');
        const out=document.getElementById('testresult');
        const limit=20000; const controller=new AbortController();
        const timer=setTimeout(()=>controller.abort(),limit);
        btn.disabled=true; out.textContent='Teste… (max. 20 s)';
        const fd=new FormData(document.getElementById('plugin-config-form'));
        fd.set('installation_id','{esc(active_inst.installation_id)}');
        try {{
          const r=await fetch('/admin/plugin/{esc(slug)}/test',{{method:'POST',body:fd,signal:controller.signal}});
          const d=await r.json();
          out.textContent=(d.state==='ok'?'OK · ':d.state==='error'?'FEHLER · ':'— ')+d.message;
        }} catch (err) {{
          out.textContent=err.name==='AbortError'?'FEHLER · Verbindungstest nach 20 s abgebrochen.':'FEHLER · '+err;
        }} finally {{ clearTimeout(timer); btn.disabled=false; }}
      }};
    </script>"""

    body = f"""
    <style>
      .install-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin:14px 0 18px}}
      .install-card{{display:flex;flex-direction:column;gap:4px;padding:12px 13px;border:1px solid var(--border-soft);
        border-radius:8px;background:var(--surface-2);text-decoration:none;color:var(--text)}}
      .install-card.active{{border-color:var(--link);box-shadow:0 0 0 3px var(--ring)}}
      .install-card span{{font-size:12px;color:var(--text-dim)}}
    </style>
    <div class="crumb"><a href="/admin">← Alle Plugins</a> · {cat_label}</div>
    <div class="hero" style="display:flex;align-items:center;gap:16px;margin-bottom:20px">
      {icon_html(slug, cls.icon)}
      <div><h1 style="font-size:24px;margin:0 0 4px">{esc(cls.name)}</h1>
        <p style="margin:0">{esc(cls.description)}</p></div>
    </div>
    {soon_banner}{flash}
    <form method="post" action="/admin/plugin/{esc(slug)}/master">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="panel">
        <div class="toggle-row" style="margin-bottom:20px">
          <input class="toggle" type="checkbox" name="__enabled" {toggled}>
          <div><div style="font-weight:600;font-size:14px">Plugin aktiviert</div>
            <div class="note" style="font-size:12px">Master-Schalter für alle Installationen.</div></div>
        </div>
        <button class="btn secondary sm" type="submit">Master speichern</button>
      </div>
    </form>
    <div class="panel" style="margin-top:14px">
      <div class="row" style="justify-content:space-between;align-items:center">
        <div><h2 style="margin:0;font-size:16px">Installationen</h2>
          <div class="note">Jede Installation hat eigene Zugangsdaten und kann separat an/aus sein.</div></div>
        <form method="post" action="{create_href}">
          <input type="hidden" name="csrf" value="{esc(token)}">
          <button class="btn sm" type="submit">+ Installation</button>
        </form>
      </div>
      <div class="install-grid">{''.join(install_rows)}</div>
    </div>
    {oauth_html}
    <form id="plugin-config-form" method="post" action="/admin/plugin/{esc(slug)}">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <input type="hidden" name="installation_id" value="{esc(active_inst.installation_id)}">
      <div class="panel" style="margin-top:14px">
        <div class="field"><label>Name der Installation</label>
          <input type="text" name="__installation_name" value="{esc(active_inst.installation_name)}"></div>
        <div class="toggle-row" style="margin-bottom:20px">
          <input class="toggle" type="checkbox" name="__instance_enabled" {"checked" if active_inst.cfg.get("__instance_enabled", active_inst.is_toggled_on) else ""}>
          <div><div style="font-weight:600;font-size:14px">Installation eingeschaltet</div>
            <div class="note" style="font-size:12px">Wirkt nur, wenn der Master-Schalter oben aktiv ist.</div></div>
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
    install_id = str(form.get("installation_id") or "default")
    enabled = "__instance_enabled" in form
    name = str(form.get("__installation_name") or "").strip()
    saved_id = await get_config_store().save_installation(cls, install_id, values, enabled, name=name)
    await mgr.rebuild()
    return RedirectResponse(f"/admin/plugin/{slug}?installation={saved_id}&saved=1", status_code=303)


@router.post("/admin/plugin/{slug}/master")
async def plugin_master_save(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    if not cls:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse(f"/admin/plugin/{slug}", status_code=303)
    cfg = await get_config_store().load(cls)
    await get_config_store().save(cls, cfg, "__enabled" in form)
    await mgr.rebuild()
    return RedirectResponse(f"/admin/plugin/{slug}?saved=1", status_code=303)


@router.post("/admin/plugin/{slug}/installation/new")
async def plugin_installation_new(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    if not cls:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse(f"/admin/plugin/{slug}", status_code=303)
    defaults = {f.key: f.default for f in cls.config_fields}
    install_id = await get_config_store().save_installation(
        cls, "__new__", defaults, False, name="Neue Installation"
    )
    await mgr.rebuild()
    return RedirectResponse(f"/admin/plugin/{slug}?installation={install_id}&saved=1", status_code=303)


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
    install_id = str(form.get("installation_id") or "default")
    if install_id != "default":
        installs = await store.load_installations(cls)
        cfg = next((i for i in installs if i.get("__installation_id") == install_id), cfg)
    for f in cls.config_fields:
        if f.type is FieldType.BOOL:
            cfg[f.key] = f.key in form
        else:
            sub = form.get(f.key, "")
            if sub not in ("", SECRET_SENTINEL):
                cfg[f.key] = f.coerce(sub)
    cfg["__enabled"] = True  # test connectivity regardless of toggle
    # A plugin must never hold the admin UI open indefinitely. OSINT gets the
    # shorter limit because a dead Tor sidecar otherwise looks like a frozen page.
    test_timeout = 15.0 if slug == "osint" else 30.0
    try:
        status = await asyncio.wait_for(cls(cfg).health_check(), timeout=test_timeout)
        return JSONResponse({"state": status.state.value, "message": status.message})
    except asyncio.TimeoutError:
        return JSONResponse({"state": "error",
                             "message": f"Verbindungstest nach {int(test_timeout)} s abgebrochen. "
                                        "Erreichbarkeit und Konfiguration prüfen."})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"state": "error", "message": str(e)})


def _config_values_from_cfg(cls, cfg: dict) -> dict:
    return {f.key: cfg.get(f.key, f.default) for f in cls.config_fields}


@router.post("/admin/plugin/{slug}/oauth/google/start")
async def plugin_google_oauth_start(slug: str, request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    cls = mgr.plugin_class(slug)
    if not cls or not getattr(cls, "google_scopes", None):
        return RedirectResponse(f"/admin/plugin/{slug}", status_code=303)
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse(f"/admin/plugin/{slug}", status_code=303)
    install_id = str(form.get("installation_id") or "default")
    store = get_config_store()
    cfg = next(
        (i for i in await store.load_installations(cls) if i.get("__installation_id") == install_id),
        await store.load(cls),
    )
    client_id = str(cfg.get("client_id") or "").strip()
    client_secret = str(cfg.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return RedirectResponse(f"/admin/plugin/{slug}?installation={install_id}&oauth=missing_client", status_code=303)
    state = secrets.token_urlsafe(24)
    await db.set_setting(f"oauth_state:{state}", {
        "provider": "google",
        "slug": slug,
        "installation_id": install_id,
        "redirect_uri": str(request.url_for("oauth_google_callback")),
        "ts": _now_iso(),
    })
    url = authorization_url(
        client_id=client_id,
        redirect_uri=str(request.url_for("oauth_google_callback")),
        scopes=list(getattr(cls, "google_scopes", [])),
        state=state,
    )
    return RedirectResponse(url, status_code=303)


@router.get("/admin/oauth/google/callback", name="oauth_google_callback")
async def oauth_google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(page("OAuth", f'<div class="flash err">Google OAuth: {esc(error)}</div>'), 400)
    state_data = await db.get_setting(f"oauth_state:{state}", None)
    if not state_data or state_data.get("provider") != "google":
        return HTMLResponse(page("OAuth", '<div class="flash err">OAuth-State ungueltig oder abgelaufen.</div>'), 400)
    mgr = get_manager()
    cls = mgr.plugin_class(state_data.get("slug", ""))
    if not cls:
        return HTMLResponse(page("OAuth", '<div class="flash err">Plugin nicht gefunden.</div>'), 404)
    store = get_config_store()
    install_id = str(state_data.get("installation_id") or "default")
    cfg = next(
        (i for i in await store.load_installations(cls) if i.get("__installation_id") == install_id),
        await store.load(cls),
    )
    try:
        token_data = await exchange_code(
            client_id=str(cfg.get("client_id") or ""),
            client_secret=str(cfg.get("client_secret") or ""),
            code=code,
            redirect_uri=str(state_data.get("redirect_uri") or request.url_for("oauth_google_callback")),
        )
        email = await user_email(str(token_data.get("access_token") or ""))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(page("OAuth", f'<div class="flash err">Token-Austausch fehlgeschlagen: {esc(e)}</div>'), 400)
    values = _config_values_from_cfg(cls, cfg)
    values.update(token_patch(token_data, email=email))
    saved_id = await store.save_installation(
        cls,
        install_id,
        values,
        bool(cfg.get("__instance_enabled", cfg.get("__enabled"))),
        name=str(cfg.get("__installation_name") or "Standard"),
    )
    await mgr.rebuild()
    await db.audit("oauth_connected", actor="owner", detail={"provider": "google", "plugin": cls.slug})
    return RedirectResponse(f"/admin/plugin/{cls.slug}?installation={saved_id}&saved=1", status_code=303)


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
    token = await auth.issue_csrf()
    flash = '<div class="flash ok">Gespeichert.</div>' if saved else ""
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
    set_model_config(s.get("models"))
    model_snapshot = model_config_snapshot()
    stored_models = s.get("models") if isinstance(s.get("models"), dict) else {}
    stored_providers = stored_models.get("providers") if isinstance(stored_models.get("providers"), dict) else {}
    provider_names = list(dict.fromkeys(
        ["openai", "openrouter", "anthropic", "ollama", *model_snapshot["providers"].keys()]
    ))
    provider_labels = {
        "openai": ("OpenAI", "GPT und Codex"),
        "openrouter": ("OpenRouter", "Modelle mehrerer Anbieter"),
        "anthropic": ("Anthropic", "Claude"),
        "ollama": ("Ollama", "lokale Modelle"),
    }
    provider_cards = []
    for provider_name in provider_names:
        current = model_snapshot["providers"].get(provider_name, {})
        stored_provider = stored_providers.get(provider_name)
        stored_provider = stored_provider if isinstance(stored_provider, dict) else {}
        label, subtitle = provider_labels.get(
            provider_name, (provider_name.replace("_", " ").title(), "OpenAI-kompatibel")
        )
        kind = str(current.get("kind") or "openai_compat")
        configured = bool(current.get("configured"))
        key_saved = bool(stored_provider.get("api_key"))
        status_label = "bereit" if configured else "Key fehlt"
        status_class = "ok" if configured else ""
        base_url = str(stored_provider.get("base_url") or current.get("base_url") or "")
        tools = bool(current.get("tools", kind == "openai_compat"))
        provider_cards.append(f"""
        <div class="card model-provider" data-provider="{esc(provider_name)}">
          <div class="row" style="justify-content:space-between;gap:10px">
            <div><b>{esc(label)}</b><div class="note" style="font-size:11px">{esc(subtitle)}</div></div>
            <span class="badge {status_class}">{status_label}</span>
          </div>
          <input type="hidden" name="model_provider_{esc(provider_name)}_kind" value="{esc(kind)}">
          <div class="field" style="margin-top:12px"><label>API-Key</label>
            <input type="password" name="model_provider_{esc(provider_name)}_api_key"
              value="" autocomplete="new-password"
              placeholder="{"gespeichert · leer = behalten" if key_saved else "aus .env oder hier eintragen"}"></div>
          <div class="field"><label>Base URL</label>
            <input type="url" name="model_provider_{esc(provider_name)}_base_url"
              value="{esc(base_url)}" placeholder="Standard-Endpunkt"></div>
          <label class="toggle-row" style="padding:8px 0 0">
            <input class="toggle" type="checkbox"
              name="model_provider_{esc(provider_name)}_tools" {"checked" if tools else ""}>
            <span><b style="font-size:12px">Tool-Calling</b>
              <span class="note" style="font-size:11px;display:block">Nur aktivieren, wenn der Endpunkt OpenAI-kompatibel ist.</span></span>
          </label>
        </div>""")
    provider_options = "".join(
        f'<option value="{esc(name)}">{{label}}</option>'.format(
            label=esc(provider_labels.get(name, (name, ""))[0])
        )
        for name in provider_names
    )
    role_labels = {
        "small": ("Schnell", "Triage, Zusammenfassungen"),
        "medium": ("Hauptmodell", "normaler Chat mit Tools"),
        "heavy": ("Analyse", "Planung und schwierige Aufgaben"),
        "code": ("Code", "Codex oder anderes Coding-Modell"),
        "osint": ("Recherche", "OSINT und Recherche über Tor"),
    }
    role_rows = []
    for role in ROLES:
        target = model_snapshot["roles"].get(role, {})
        selected_provider = str(target.get("provider") or "openai")
        options = provider_options.replace(
            f'value="{esc(selected_provider)}"',
            f'value="{esc(selected_provider)}" selected',
            1,
        )
        title, desc = role_labels[role]
        role_rows.append(f"""
        <div class="model-route">
          <div><b>{esc(title)}</b><span class="note">{esc(desc)}</span></div>
          <select name="model_role_{role}_provider">{options}</select>
          <input type="text" name="model_role_{role}_model"
            value="{esc(str(target.get('model') or ''))}" placeholder="Modell-ID">
        </div>""")
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
    font_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if k == cur_font else ""}>{esc(name)}</option>'
        for k, name in font_choices())
    settings_script = (
        "<script>const fontSpecs = "
        + json.dumps(font_live_specs())
        + ";\n"
        + """
      const $ = id => document.getElementById(id);
      const status = $('geostatus'), addr = $('addr'), results = $('addrresults');
      const latInput = $('lat'), lonInput = $('lon');
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
      setMapStyle('dark');
      const fontSelect = $('lab_font'), preview = $('fontpreview');
      function updateFontPreview() {
        const spec = fontSpecs[fontSelect.value] || {name:'Inter',family:"'Inter'",query:''};
        if (spec.query && !document.getElementById('live-font-' + fontSelect.value)) {
          const link = document.createElement('link');
          link.id = 'live-font-' + fontSelect.value;
          link.rel = 'stylesheet';
          link.href = 'https://fonts.googleapis.com/css2?family=' + spec.query + '&display=swap';
          document.head.appendChild(link);
        }
        document.documentElement.style.setProperty('--ui-font', spec.family);
        preview.style.setProperty('--ui-font-preview', spec.family);
        const name = spec.name || fontSelect.value || 'Inter';
        preview.querySelector('span').textContent = `FONT FORGE · ${name}`;
      }
      fontSelect.addEventListener('change', updateFontPreview);
      updateFontPreview();
      document.querySelectorAll('input[name="lab_theme"]').forEach(input => {{
        input.addEventListener('change', () => {{
          if (input.checked) document.documentElement.dataset.astraTheme = input.value;
        }});
      }});
      updateChips();
      setTimeout(() => map.invalidateSize(), 200);
    </script>"""
    )
    body = f"""
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

      <div class="panel model-picker" style="margin-bottom:16px">
        <div class="section-head">
          <div><h2>Model Routing</h2>
            <p>Ein Anbieter pro Aufgabe. Änderungen gelten sofort und brauchen keinen Neustart.</p></div>
          <span class="badge">5 Rollen</span>
        </div>
        <div class="model-routes">{"".join(role_rows)}</div>
        <div class="section-head" style="margin-top:24px">
          <div><h2>Anbieter</h2>
            <p>Keys werden verschlüsselt gespeichert und nie wieder im Klartext angezeigt.</p></div>
        </div>
        <div class="grid model-providers">{"".join(provider_cards)}</div>
        <div class="note" style="margin-top:12px">Das <b>Hauptmodell</b> benötigt einen
          OpenAI-kompatiblen Anbieter für ASTRA-Tools. Claude funktioniert direkt in den
          Rollen Analyse, Code oder Recherche. „Nutze Claude“ kann ASTRA über die
          zugeordnete Rolle ausführen. Codex ist eine Modell-ID in der Rolle Code.</div>
        <div class="toggle-row"><input class="toggle" type="checkbox" name="economy" {eco}>
          <div><div style="font-weight:600;font-size:14px">Sparmodus</div>
            <div class="note" style="font-size:12px">Normaler Chat nutzt vorübergehend die
              Rolle Schnell — weniger Kosten, gleiche Routing-Regeln.</div></div></div>
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
            <h2>Theme &amp; Typografie</h2>
            <p>Zwei Regler, die sofort auf die gesamte Oberfläche wirken.</p>
          </div>
        </div>
        <div class="theme-lab">
          <div class="lab-eyebrow">Theme Deck</div>
          <h3>Zehn OLED-Kontrollräume</h3>
          <p>Nicht nur Farbe: Navigation, Einzüge, Kanten, Überschriften und Instrumente wechseln live mit.</p>
          {_theme_picker(labs.get("theme", "event_horizon"))}
        </div>
        <div class="typography-lab">
          <div class="lab-eyebrow">Typography</div>
          <h3>UI-Schrift</h3>
          <p>Wirkt sofort global. Eigene Fonts im Fonts-Ordner erscheinen automatisch.</p>
          <select name="lab_font" id="lab_font">{font_opts}</select>
          <div class="font-preview" id="fontpreview">
            <span>LIVE TYPE</span>
            <strong>ASTRA kontrolliert den Raum.</strong>
          </div>
        </div>
      </div>

      <button class="btn" type="submit">Alles speichern</button>
    </form>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    {settings_script}"""
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
        "theme": form.get("lab_theme", _DEFAULT_LABS["theme"]),
    }
    old_models = s.get("models") if isinstance(s.get("models"), dict) else {}
    old_providers = old_models.get("providers") if isinstance(old_models.get("providers"), dict) else {}
    provider_names = {"openai", "openrouter", "anthropic", "ollama"}
    provider_names.update(
        key.removeprefix("model_provider_").removesuffix("_kind")
        for key in form.keys()
        if key.startswith("model_provider_") and key.endswith("_kind")
    )
    model_providers: dict[str, dict] = {}
    for name in sorted(provider_names):
        old = old_providers.get(name)
        old = old if isinstance(old, dict) else {}
        api_key = str(form.get(f"model_provider_{name}_api_key") or "").strip()
        saved_key = protect_api_key(api_key) if api_key else old.get("api_key", "")
        model_providers[name] = {
            "kind": str(form.get(f"model_provider_{name}_kind") or old.get("kind")
                        or ("anthropic" if name == "anthropic" else "openai_compat")),
            "base_url": str(form.get(f"model_provider_{name}_base_url") or "").strip(),
            "api_key": saved_key,
            "tools": f"model_provider_{name}_tools" in form,
        }
    model_roles = {
        role: {
            "provider": str(form.get(f"model_role_{role}_provider") or "").strip(),
            "model": str(form.get(f"model_role_{role}_model") or "").strip(),
        }
        for role in ROLES
    }
    model_config = {"providers": model_providers, "roles": model_roles}
    s.update({
        "owner_name": form.get("owner_name", "").strip(),
        "timezone": form.get("timezone", "Europe/Berlin"),
        "units": form.get("units", "metric"),
        "language": form.get("language", "de"),
        "ai_model": "",
        "models": model_config,
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
    set_model_override(None)
    set_model_config(model_config)
    set_font(s["font"])
    set_theme(labs["theme"])
    from ..brain import set_autonomy
    set_autonomy(s["autonomy"])
    await db.audit("settings_change", actor="owner",
                   detail={"city": s["location"]["city"], "models": model_roles,
                           "font": s["font"], "county": s["location"]["county"]})
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ─── System: performance + services & URLs ────────────────────────────────────
def _meter(pct, used_h, total_h) -> str:
    if pct is None:
        return f'<div class="sub">{esc(used_h)} / {esc(total_h)}</div>'
    cls = " warn" if pct > 85 else ""
    return (f'<div class="sub">{esc(used_h)} / {esc(total_h)} · {pct:.0f}%</div>'
            f'<div class="meter{cls}"><i style="width:{min(pct,100):.0f}%"></i></div>')


def _tool_badge(safety: str) -> str:
    label = {
        "read": "read",
        "private_read": "private",
        "mutation": "ändert",
        "external_send": "sendet",
        "destructive": "riskant",
    }.get(safety, safety or "tool")
    cls = "ok" if safety in ("read", "private_read") else "warn"
    return f'<span class="badge {cls}">{esc(label)}</span>'


async def _agent_tools_panel() -> str:
    from ..tools import capability_manifest
    caps = capability_manifest(is_owner=True)
    if not caps:
        return '<div class="panel"><h2>Agent Tools</h2><p class="note">Keine Tools registriert.</p></div>'
    rows = []
    for cap in caps[:80]:
        intents = ", ".join(cap.get("intents") or ["generic"])
        examples = "; ".join(cap.get("examples") or [])
        rows.append(
            '<div class="svc">'
            f'<span class="dot {"up" if not cap.get("requires_confirmation") else ""}"></span>'
            f'<div style="flex:1"><div class="nm">{esc(cap["tool"])} {_tool_badge(cap.get("safety", ""))}</div>'
            f'<div class="u">{esc(cap["source"])} · {esc(intents)}'
            + (f' · {esc(examples)}' if examples else "")
            + '</div></div>'
            f'<form method="post" action="/admin/system/tool-test" style="margin:0">'
            f'<input type="hidden" name="tool" value="{esc(cap["tool"])}">'
            f'<button class="btn ghost sm" type="submit">Test</button></form></div>'
        )
    last = await db.get_setting("agent_tool_last", {}) or {}
    last_html = ""
    if last:
        last_html = (
            '<details style="margin-top:12px"><summary>Letzter Toolcall</summary>'
            f'<pre>{esc(json.dumps(last, ensure_ascii=False, indent=2))}</pre></details>'
        )
    return (
        '<div class="panel" style="margin-bottom:18px"><h2 style="margin:0 0 4px;font-size:15px">'
        'Agent Tools</h2><p class="note" style="margin:0 0 12px">Registrierte Fähigkeiten, Safety und Schnelltest.</p>'
        + "".join(rows) + last_html + '</div>'
    )


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
        f'<div class="rec {r["level"]}"><b>{"WARN" if r["level"]=="warn" else "OK"}</b>&nbsp; {esc(r["text"])}</div>'
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
    agent_tools = await _agent_tools_panel()

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
    {agent_tools}
    <p class="note">Modell aktiv: <b>{esc(st.openai_model)}</b> (überschreibbar in den Einstellungen) ·
      <a href="/admin/update">Updates &amp; Versionen →</a></p>
    <script>setTimeout(() => location.reload(), 15000);</script>"""
    return HTMLResponse(page("System", body, active="system"))


@router.post("/admin/system/tool-test")
async def system_tool_test(request: Request, _: bool = Depends(auth.require_admin)):
    from ..tools import ToolContext, dispatch
    form = await request.form()
    tool = str(form.get("tool") or "")
    result = await dispatch(
        tool,
        {},
        ToolContext(thread_id="web-system:tool-test", channel="web", contact={"id": "owner"}, is_owner=True),
    )
    await db.set_setting("agent_tool_last", {"tool": tool, "result": result, "ts": _now_iso()})
    return RedirectResponse("/admin/system", status_code=303)


# ─── Secretary: channel setup, policy, and live channel threads ───────────────
def _channel_label(channel: str) -> str:
    return {**CHANNEL_LABELS, "web": "Web"}.get(channel, channel)


def _render_hub_messages(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        role = m.get("role", "user")
        cls = "user" if role in ("user", "owner") else "bot" if role == "assistant" else "sys"
        label = "Bahrian" if role == "owner" else "ASTRA" if role == "assistant" else "Kontakt"
        rows.append(
            f'<div class="msg-row {cls}"><div class="msg {cls}">'
            f'<b style="display:block;font-size:11px;color:var(--text-faint);margin-bottom:4px">{esc(label)}</b>'
            f'{esc(m.get("content", ""))}</div></div>'
        )
    return "".join(rows) if rows else '<div class="msg sys">Noch keine Nachrichten in diesem Thread.</div>'


def _checked(value: bool) -> str:
    return " checked" if value else ""


def _select(name: str, value: str, options: list[tuple[str, str]]) -> str:
    opts = "".join(
        f'<option value="{esc(v)}" {"selected" if value == v else ""}>{esc(label)}</option>'
        for v, label in options
    )
    return f'<select name="{esc(name)}">{opts}</select>'


SECRETARY_SETUP_CHANNELS = ("waha", "signal", "slack", "email")
SECRETARY_CHANNEL_SETUP = {
    "waha": {
        "title": "WhatsApp via WAHA",
        "kind": "WhatsApp",
        "summary": "Session, Webhook, QR-Pairing und Gruppenbremse.",
    },
    "signal": {
        "title": "Signal",
        "kind": "Signal",
        "summary": "signal-cli-rest-api, Account und Gruppenmetadaten.",
    },
    "slack": {
        "title": "Slack",
        "kind": "Slack",
        "summary": "Bot-Token, Standard-Kanal und Freigabegrenzen.",
    },
    "email": {
        "title": "Mail",
        "kind": "IMAP/SMTP",
        "summary": "IMAP-Eingang, SMTP-Ausgang und Freigabegrenzen.",
    },
}


def _secretary_installations(settings: dict) -> dict:
    raw = settings.get("installations") or {}
    defaults = {
        "waha": {
            "title": "WhatsApp via WAHA",
            "base_url": "",
            "session": "default",
            "api_key": "",
            "webhook_url": "/ingress/waha",
            "events": "message",
            "status": "not_configured",
        },
        "signal": {
            "title": "Signal",
            "base_url": "",
            "account": "",
            "webhook_url": "/ingress/signal",
            "status": "not_configured",
        },
        "slack": {
            "title": "Slack",
            "bot_token": "",
            "default_channel": "#general",
            "webhook_url": "/ingress/slack",
            "status": "not_configured",
        },
        "email": {
            "title": "Mail",
            "provider": "imap_smtp",
            "imap_host": "",
            "imap_port": "993",
            "imap_user": "",
            "imap_mailbox": "INBOX",
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_user": "",
            "from_address": "",
            "poll_minutes": "5",
            "webhook_url": "/ingress/email",
            "status": "not_configured",
        },
    }
    out = {}
    for channel, default in defaults.items():
        saved = raw.get(channel) or {}
        out[channel] = {**default, **saved}
    return out


def _email_accounts(raw_secretary: dict) -> list[dict]:
    """Return the configured email accounts. Migrates a legacy single 'email'
    installation into the list form on first read."""
    accounts = raw_secretary.get("email_accounts")
    if isinstance(accounts, list) and accounts:
        return accounts
    legacy = (raw_secretary.get("installations") or {}).get("email") or {}
    if legacy.get("imap_host") or legacy.get("smtp_host"):
        return [legacy]
    return []


def _email_account_block(i: int, acc: dict) -> str:
    title = acc.get("title", "") or (f"Konto {i + 1}")
    has_imap = bool(acc.get("imap_host"))
    test_btn = (
        f'<button class="btn sm" type="button" data-email-test="{i}">Testen</button>'
        if has_imap else ""
    )
    return (
        f'<div class="email-account" data-email-account data-email-idx="{i}">'
        f'<div class="email-account-head"><b>Mail-Konto {i + 1}</b>'
        f'<div style="display:flex;gap:8px;align-items:center">'
        + test_btn
        + f'<label class="secretary-switch"><input type="checkbox" name="sec_email_{i}_enabled"'
        f'{_checked(bool(acc.get("enabled", True)))}> aktiv</label></div></div>'
        + f'<div class="install-fields">'
        + _field(f"sec_email_{i}_title", "Bezeichnung", title, placeholder="z. B. iCloud / Schule / Google")
        + _field(f"sec_email_{i}_from", "Absender-Adresse", acc.get("from_address", ""), placeholder="name@example.com")
        + _field(f"sec_email_{i}_imap_host", "IMAP Host", acc.get("imap_host", ""), placeholder="imap.example.com")
        + _field(f"sec_email_{i}_imap_port", "IMAP Port", acc.get("imap_port", "993"))
        + _field(f"sec_email_{i}_imap_user", "IMAP User", acc.get("imap_user", ""), placeholder="name@example.com  –oder–  haupt@s.de\\freigabe@s.de")
        + _field(f"sec_email_{i}_password", "Passwort / App-Passwort", "", placeholder="••• (leer = unverändert)", typ="password")
        + _field(f"sec_email_{i}_imap_mailbox", "Postfach", acc.get("imap_mailbox", "INBOX"))
        + _field(f"sec_email_{i}_smtp_host", "SMTP Host", acc.get("smtp_host", ""), placeholder="smtp.example.com  (leer = aus IMAP-Host ableiten)")
        + _field(f"sec_email_{i}_smtp_port", "SMTP Port", acc.get("smtp_port", "587"))
        + _field(f"sec_email_{i}_poll", "Poll Minuten", acc.get("poll_minutes", "5"), typ="number")
        + f'</div>'
        + f'<details class="adv" data-email-test-wrap-{i} hidden>'
        + f'<summary>Live-Test Konto {i + 1}</summary>'
        + f'<div class="waha-test-box" data-email-test-box="{i}"></div>'
        + f'</details>'
        + f'</div>'
    )


def _setup_seed(channel: str) -> list[dict]:
    label = _channel_label(channel)
    guides = {
        "waha": (
            "Ich helfe dir beim WhatsApp-Setup. Ziel: WAHA schickt eingehende Nachrichten an "
            "/ingress/waha, ASTRA erkennt Gruppen, fromMe und Rueckfragen sauber."
        ),
        "signal": (
            "Ich helfe dir beim Signal-Setup. Ziel: signal-cli-rest-api liefert Nachrichten an "
            "/ingress/signal inklusive groupInfo, damit Gruppen sicher behandelt werden."
        ),
        "email": (
            "Ich helfe dir beim Mail-Setup. Ziel: IMAP/Gmail normalisiert Mails nach /ingress/email; "
            "Antworten bleiben standardmaessig bestaetigungspflichtig."
        ),
    }
    return [{"role": "assistant", "content": guides.get(channel, f"Ich helfe dir beim {label}-Setup.")}]


def _setup_messages(store: dict, channel: str) -> list[dict]:
    chats = store.setdefault("chats", {})
    if channel not in chats:
        chats[channel] = _setup_seed(channel)
    return chats[channel]


def _render_setup_chat(channel: str, messages: list[dict]) -> str:
    rows = []
    for msg in messages[-8:]:
        role = msg.get("role", "assistant")
        cls = "user" if role == "user" else "bot"
        who = "Du" if role == "user" else "ASTRA"
        rows.append(
            f'<div class="setup-msg {cls}"><b>{esc(who)}</b><span>{esc(msg.get("content", ""))}</span></div>'
        )
    return (
        f'<div class="setup-chatbox" data-setup-channel="{esc(channel)}">'
        f'<div class="setup-log">{"".join(rows)}</div>'
        '<div class="setup-input">'
        '<input type="text" placeholder="Frag ASTRA zur Einrichtung...">'
        '<button class="btn sm" type="button">Fragen</button>'
        '</div></div>'
    )


def _field(name: str, label: str, value: str = "", *, placeholder: str = "", typ: str = "text") -> str:
    return (
        f'<div class="setup-field"><label>{esc(label)}</label>'
        f'<input type="{esc(typ)}" name="{esc(name)}" value="{esc(value)}" '
        f'placeholder="{esc(placeholder)}"></div>'
    )


def _channel_setup_fields(channel: str, inst: dict, connected: bool = False) -> str:
    # Auto-fill the boring technical values from the known Docker service config,
    # so the owner normally only has to scan the QR.
    s = get_settings()
    if channel == "waha":
        base = inst.get("base_url") or s.waha_base_url or "http://waha:3000"
        session = inst.get("session") or s.waha_session or "default"
        api_key = inst.get("api_key") or s.waha_api_key or ""
        if connected:
            pairing = (
                '<div class="pairing-panel primary is-connected" data-waha-pairing data-connected="1">'
                '<div><b>✓ WhatsApp verbunden</b><span>Diese Session ist mit deinem WhatsApp-Profil '
                'gekoppelt. ASTRA empfängt und beantwortet Nachrichten gemäß deiner Policy.</span></div>'
                '<div class="pairing-actions">'
                '<button class="btn sm" type="button" data-waha-test>Testen</button>'
                '<button class="btn sm ghost" type="button" data-waha-recouple>Neu koppeln (QR)</button></div>'
                '<details class="adv waha-test" data-waha-test-wrap hidden>'
                '<summary>Live-Test (Selbst-Chat)</summary>'
                '<div class="waha-test-box" data-waha-test-box></div></details>'
                '<div class="qr-box" data-qr-box hidden></div></div>'
            )
        else:
            pairing = (
                '<div class="pairing-panel primary" data-waha-pairing>'
                '<div><b>WhatsApp verbinden</b><span>Ein Klick bereitet alles vor. Öffne danach in WhatsApp '
                '<i>Verknüpfte Geräte</i> und scanne den QR-Code.</span></div>'
                '<div class="pairing-actions">'
                '<button class="btn" type="button" data-waha-connect>WhatsApp verbinden</button></div>'
                '<div class="pairing-progress" data-waha-progress hidden>'
                '<div class="pairing-progress-head"><span data-waha-progress-label>Verbindung wird vorbereitet…</span>'
                '<b data-waha-progress-value>10%</b></div>'
                '<div class="pairing-progress-track"><i data-waha-progress-bar></i></div>'
                '<div class="pairing-steps"><span class="active">Session prüfen</span>'
                '<span>WhatsApp vorbereiten</span><span>QR bereit</span></div></div>'
                '<div class="qr-box" data-qr-box hidden></div></div>'
            )
        return (
            pairing
            + '<details class="adv"><summary>Erweiterte Einstellungen (automatisch befüllt)</summary>'
            '<div class="install-fields">'
            + _field("sec_waha_title", "Titel", inst.get("title", ""), placeholder="WhatsApp via WAHA")
            + _field("sec_waha_base_url", "WAHA Base URL", base, placeholder="http://waha:3000")
            + _field("sec_waha_session", "Session", session, placeholder="default")
            + _field("sec_waha_api_key", "API Key (aus .env)", api_key, placeholder="X-Api-Key")
            + _field("sec_waha_events", "Hook Events", inst.get("events", "message"), placeholder="message")
            + _field("sec_waha_webhook", "ASTRA Webhook", inst.get("webhook_url", "/ingress/waha"))
            + "</div></details>"
        )
    if channel == "signal":
        base = inst.get("base_url") or s.signal_base_url or "http://signal-cli:8080"
        account = inst.get("account") or s.signal_phone_number or ""
        if connected:
            pairing = (
                '<div class="pairing-panel primary is-connected" data-signal-pairing data-connected="1">'
                '<div><b>✓ Signal verbunden</b><span>ASTRA ist als verknüpftes Gerät an deinem '
                'Signal-Account angemeldet und empfängt Nachrichten auf <code>/ingress/signal</code>.'
                '</span></div>'
                '<div class="pairing-actions">'
                '<button class="btn sm" type="button" data-signal-test>Testen</button>'
                '<button class="btn sm ghost" type="button" data-signal-recouple>Neu koppeln (QR)</button></div>'
                '<details class="adv waha-test" data-signal-test-wrap hidden>'
                '<summary>Live-Test (Selbst-Chat)</summary>'
                '<div class="waha-test-box" data-signal-test-box></div></details>'
                '<div class="qr-box" data-signal-qr-box></div></div>'
            )
        else:
            pairing = (
                '<div class="pairing-panel primary" data-signal-pairing>'
                '<div><b>Signal verbinden</b><span>QR anzeigen &nbsp;→&nbsp; in Signal unter '
                '<i>Einstellungen · Verknüpfte Geräte · Gerät verknüpfen</i> scannen. Server-URL ist '
                'automatisch gesetzt; ASTRA empfängt danach Webhooks auf <code>/ingress/signal</code>.</span></div>'
                '<div class="pairing-actions">'
                '<button class="btn sm" type="button" data-signal-qr>QR anzeigen</button></div>'
                '<div class="qr-box" data-signal-qr-box></div></div>'
            )
        return (
            pairing
            + '<details class="adv"><summary>Erweiterte Einstellungen (automatisch befüllt)</summary>'
            '<div class="install-fields">'
            + _field("sec_signal_title", "Titel", inst.get("title", ""), placeholder="Signal")
            + _field("sec_signal_base_url", "signal-cli API URL", base, placeholder="http://signal-cli:8080")
            + _field("sec_signal_account", "Account / Nummer", account, placeholder="+491...")
            + _field("sec_signal_webhook", "ASTRA Webhook", inst.get("webhook_url", "/ingress/signal"))
            + "</div></details>"
        )
    if channel == "slack":
        return (
            '<div class="pairing-panel primary">'
            '<div><b>Slack verbinden</b><span>Auf api.slack.com eine App anlegen, Bot-Token '
            '(<code>xoxb-…</code>, Scope <code>chat:write</code>) hier eintragen. Eingehende Nachrichten '
            'laufen über die Slack Events API auf <code>/ingress/slack</code>.</span></div></div>'
            '<details class="adv" open><summary>Slack-Zugang</summary><div class="install-fields">'
            + _field("sec_slack_title", "Titel", inst.get("title", ""), placeholder="Slack")
            + _field("sec_slack_bot_token", "Bot-Token (xoxb-…)", inst.get("bot_token", ""), placeholder="xoxb-…")
            + _field("sec_slack_channel", "Standard-Kanal", inst.get("default_channel", "#general"), placeholder="#general")
            + _field("sec_slack_webhook", "ASTRA Webhook", inst.get("webhook_url", "/ingress/slack"))
            + "</div></details>"
        )
    # Email: multiple accounts. Render one block per saved account + one blank.
    accounts = list(inst.get("_accounts") or [])
    accounts.append({})  # always offer a fresh blank block at the end
    blocks = "".join(_email_account_block(i, acc) for i, acc in enumerate(accounts))
    return (
        '<details class="adv" open><summary>Mail-Konten (IMAP / SMTP)</summary>'
        '<p class="note" style="margin:0 0 10px">Du kannst beliebig viele Konten anlegen. '
        'Für SMTP wird – wenn leer – derselbe Server/User wie für IMAP angenommen. '
        'Passwort leer lassen = unverändert.</p>'
        f'<input type="hidden" name="sec_email_count" data-email-count value="{len(accounts)}">'
        f'<div data-email-list>{blocks}</div>'
        '<div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">'
        '<button class="btn sm ghost" type="button" data-email-add>+ Weiteres Konto</button>'
        '<button class="btn sm" type="submit" style="margin-left:auto">Mail-Konten speichern ↑</button>'
        '</div>'
        '</details>'
    )


def _contact_rule_row(i: int, rule: dict) -> str:
    channel_opts = [("waha", "WhatsApp"), ("signal", "Signal"), ("slack", "Slack"),
                    ("email", "Mail"), ("*", "Alle Kanäle")]
    rule_opts = [("block", "Blockieren"), ("ask", "Owner fragen"), ("allow", "Erlauben (Policy)"),
                 ("direct", "Direkt antworten")]
    return (
        f'<div class="contact-rule-row" data-rule-row>'
        + _select(f"cr_{i}_channel", rule.get("channel", "waha"), channel_opts)
        + f'<input type="text" name="cr_{i}_id" value="{esc(rule.get("id",""))}" '
          f'placeholder="+491234… / ID@c.us" style="flex:2">'
        + _select(f"cr_{i}_rule", rule.get("rule", "block"), rule_opts)
        + f'<input type="text" name="cr_{i}_note" value="{esc(rule.get("note",""))}" '
          f'placeholder="Notiz (optional)" style="flex:2">'
        + f'<button class="btn sm ghost" type="button" onclick="this.closest(\'[data-rule-row]\').remove()" '
          f'style="padding:4px 10px">✕</button>'
        + f'</div>'
    )


def _render_contact_rules(rules: list) -> str:
    if not rules:
        return '<div data-contact-rule-list></div>'
    rows = "".join(_contact_rule_row(i, r) for i, r in enumerate(rules))
    return f'<div data-contact-rule-list>{rows}</div>'


def _secretary_channel_card(channel: str, cfg: dict, inst: dict) -> str:
    mode_options = {
        "waha": [("school_direct", "Schulzeit direkt"), ("always_ask", "Immer fragen"), ("wait", "Warten"), ("direct", "Direkt")],
        "signal": [("school_direct", "Schulzeit direkt"), ("always_ask", "Immer fragen"), ("wait", "Warten"), ("direct", "Direkt")],
        "slack": [("school_direct", "Schulzeit direkt"), ("always_ask", "Immer fragen"), ("wait", "Warten"), ("direct", "Direkt")],
        "email": [("always_ask", "Immer fragen"), ("wait", "Warten"), ("direct", "Direkt")],
    }[channel]
    setup = {
        "waha": "WAHA Webhook: /ingress/waha mit X-Astra-Secret. Gruppen werden standardmaessig nur nach Freigabe bearbeitet.",
        "signal": "signal-cli-rest-api Webhook: /ingress/signal mit X-Astra-Secret. groupInfo aktiviert Gruppenschutz.",
        "slack": "Slack-Bot (xoxb-Token, Scope chat:write). Eingang via Slack Events API auf /ingress/slack.",
        "email": "Mail-Ingress: /ingress/email fuer normalisierte IMAP/Gmail-Events. Senden bleibt bestaetigungspflichtig.",
    }[channel]
    live = cfg.get("_live") or {}
    connected = bool(live.get("connected"))
    meta = SECRETARY_CHANNEL_SETUP[channel]
    status = "konfiguriert" if any(inst.get(k) for k in ("base_url", "imap_host", "bot_token")) else "offen"
    if connected:
        status = "verbunden" + (f" · {live.get('me')}" if live.get("me") else "")
    if connected:
        tag = '<span class="source-tag" style="color:#34d399;border-color:#34d399">verbunden</span>'
    else:
        tag = f'<span class="source-tag">{"aktiv" if cfg.get("enabled") else "aus"}</span>'
    setup_label = "Verbindung verwalten" if connected else "Einrichten"
    return f"""
      <div class="secretary-card install-card{' is-connected' if connected else ''}" data-install="{esc(channel)}">
        <div class="install-head">
          <div><span>{esc(meta["kind"])}</span><h3>{esc(inst.get("title") or meta["title"])}</h3></div>
          {tag}
        </div>
        <p>{esc(setup)}</p>
        <div class="install-actions">
          <button class="btn sm" type="button" data-open-setup="{esc(channel)}">{esc(setup_label)}</button>
          <span class="mini">{esc(status)} · {esc(meta["summary"])}</span>
        </div>
        <div class="install-config" data-config="{esc(channel)}">
          {_channel_setup_fields(channel, inst, connected=connected)}
          <div class="install-policy">
            <label class="secretary-switch">
              <input type="checkbox" name="sec_{esc(channel)}_enabled"{_checked(bool(cfg.get("enabled")))}>
              Installation aktiv
            </label>
            <div class="setup-field">
              <label>Modus</label>
              {_select(f"sec_{channel}_mode", cfg.get("mode", "policy"), mode_options)}
            </div>
            <button class="btn sm" type="submit" style="margin-left:auto">Speichern ↑</button>
          </div>
        </div>
        <div class="mini">{esc(channel)}</div>
      </div>
    """


@router.get("/admin/inbox", response_class=HTMLResponse)
async def inbox_legacy(_: bool = Depends(auth.require_admin), thread: str = ""):
    suffix = f"?thread={esc(thread)}" if thread else ""
    return RedirectResponse(f"/admin/secretary{suffix}", status_code=303)


@router.get("/admin/secretary", response_class=HTMLResponse)
async def secretary_page(request: Request, _: bool = Depends(auth.require_admin), thread: str = ""):
    appset = await _app_settings()
    settings = secretary_settings(appset)
    service_status = await resolve_service_status(appset, get_settings().astra_timezone)
    all_threads = await db.list_threads(80)
    threads = [t for t in all_threads if t.get("channel") in SECRETARY_SETUP_CHANNELS]
    selected = next((t for t in threads if t.get("thread_id") == thread), threads[0] if threads else None)
    active_id = selected.get("thread_id") if selected else ""
    side_rows = []
    for t in threads:
        tid = t.get("thread_id", "")
        active = " active" if tid == active_id else ""
        who = t.get("who") or tid
        side_rows.append(
            f'<a class="thread{active}" href="/admin/secretary?thread={esc(tid)}">'
            f'<span>{esc(who)}</span>'
            f'<small>{esc(_channel_label(t.get("channel", "")))} · {esc(t.get("state", ""))}</small></a>'
        )
    messages = await db.recent_messages(active_id, 80) if active_id else []
    title = selected.get("who") if selected else "Keine Threads"
    token = await auth.issue_csrf()
    raw_secretary = (appset or {}).get("secretary", {}) or {}
    installations = _secretary_installations(raw_secretary)
    installations["email"]["_accounts"] = _email_accounts(raw_secretary)
    live_status = {}
    try:
        s = get_settings()
        w = installations["waha"]
        live_status["waha"] = await _waha_session_status(
            w.get("base_url") or s.waha_base_url or "http://waha:3000",
            w.get("session") or s.waha_session or "default",
            w.get("api_key") or s.waha_api_key or "",
        )
    except Exception:  # noqa: BLE001
        live_status["waha"] = {"ok": False, "connected": False}
    try:
        s = get_settings()
        sig = installations["signal"]
        live_status["signal"] = await _signal_status(
            sig.get("base_url") or s.signal_base_url or "http://signal-cli:8080",
            sig.get("account") or s.signal_phone_number or "",
        )
    except Exception:  # noqa: BLE001
        live_status["signal"] = {"ok": False, "connected": False}
    channel_cards = "".join(
        _secretary_channel_card(
            ch,
            {**settings["channels"][ch], "_live": live_status.get(ch) or {}},
            installations[ch],
        )
        for ch in SECRETARY_SETUP_CHANNELS
    )
    workdays = {int(v) for v in (settings.get("workdays") or []) if str(v).isdigit()}
    weekday_checks = "".join(
        f'<label class="secretary-switch"><input type="checkbox" name="workday" value="{i}"{_checked(i in workdays)}>{esc(label)}</label>'
        for i, label in enumerate(["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"])
    )
    tone_options = [("warm", "Warm"), ("crisp", "Knapp"), ("formal", "Formell"), ("firm", "Klar distanziert")]
    group_options = [("owner_grant", "Nur nach Auftrag"), ("always_ask", "Immer fragen"), ("auto", "Automatisch erlauben")]
    body = f"""
    <div class="hero">
      <h1>ASTRA Secretary</h1>
      <p>WhatsApp, Signal und Mail so einrichten, dass ASTRA nur dort stellvertretend spricht, wo es wirklich gemeint ist.</p>
    </div>
    <form method="post" action="/admin/secretary">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <section class="panel" data-secretary-master-card style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap">
          <div>
            <h2 style="margin:0 0 5px;font-size:17px">Secretary</h2>
            <p class="note" data-secretary-master-copy style="margin:0">
              {"Gerade aktiv" if service_status.active else "Gerade inaktiv"} ·
              {"EduPage" if service_status.source == "edupage" else
               "Google Kalender" if service_status.source == "google_calendar" else
               "manuell" if service_status.source == "manual" else "statischer Ersatzplan"}.
            </p>
          </div>
          <input type="hidden" name="sec_activation_mode" data-secretary-mode-input
                 value="{esc(settings.get('activation_mode', 'auto'))}">
          <div role="group" aria-label="Secretary-Modus" style="display:flex;gap:7px;flex-wrap:wrap">
            {''.join(
                f'<button class="btn sm {"" if settings.get("activation_mode") == value else "ghost"}" '
                f'type="button" data-secretary-mode="{value}">{label}</button>'
                for value, label in (("auto", "Automatisch"), ("on", "Immer an"), ("off", "Aus"))
            )}
          </div>
        </div>
      </section>
      <section class="panel">
        <h2 style="margin:0 0 6px;font-size:17px">Kanäle</h2>
        <p class="note" style="margin:0 0 14px">Server, Keys und Sessions sind – wo möglich –
          automatisch befüllt. Öffne den gewünschten Kanal und folge nur den sichtbaren Schritten;
          technische Details bleiben standardmäßig eingeklappt.</p>
        <div class="secretary-cards">{channel_cards}</div>
      </section>

      <div class="panel" style="margin-top:16px">
        <h2 style="margin:0 0 6px;font-size:17px">Secretary Policy</h2>
        <div class="secretary-row">
          <div><label>Tonfall</label>{_select("sec_tone", settings.get("tone", "warm"), tone_options)}</div>
          <div><label>Security-Tonfall</label>{_select("sec_jailbreak_tone", settings.get("jailbreak_tone", "firm"), tone_options)}</div>
          <div><label>Gruppenaktionen</label>{_select("sec_group_actions", settings.get("group_actions", "owner_grant"), group_options)}</div>
          <div><label>Schulzeit Start</label><input type="text" name="sec_school_start" value="{esc(settings.get("school_start"))}"></div>
          <div><label>Schulzeit Ende</label><input type="text" name="sec_school_end" value="{esc(settings.get("school_end"))}"></div>
          <div><label>Nachfragen nach Minuten</label><input type="number" name="sec_confirm_after" min="1" max="240" value="{esc(settings.get("confirm_after_minutes"))}"></div>
          <div><label>Warten bis Eingriff</label><input type="number" name="sec_wait_after" min="1" max="480" value="{esc(settings.get("wait_after_minutes"))}"></div>
        </div>
        <div style="margin-top:12px">
          <label>Standard-Umgangston (Freitext, gilt wenn die Person kein eigenes Ton-Profil hat)</label>
          <input type="text" name="sec_default_tone" style="width:100%"
                 placeholder="z.B. freundlich-knapp, leicht trocken, nie anbiedernd"
                 value="{esc(settings.get("default_tone", ""))}">
          <p class="mini" style="margin:6px 0 0;color:var(--text-dim)">
            Pro Person legst du den Ton in ihrer Profildatei fest (Feld <b>Ton:</b> unter
            <a href="/admin/brain">Brain → Personen</a>). ASTRA zieht ihn beim Antworten heran.
          </p>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:13px">
          <label class="secretary-switch"><input type="checkbox" name="sec_school_direct"{_checked(settings.get("school_direct", True))}> In Schulzeit direkt antworten</label>
          {weekday_checks}
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px">
          <div><label>Erste Nachricht</label><input type="text" name="sec_intro" value="{esc(settings.get("intro"))}"></div>
          <div><label>Folge-Header</label><input type="text" name="sec_header" value="{esc(settings.get("header"))}"></div>
        </div>
        <button class="btn" type="submit" style="margin-top:14px">Secretary speichern</button>
      </div>

      <div class="panel" style="margin-top:16px">
        <h2 style="margin:0 0 4px;font-size:17px">Kontaktregeln</h2>
        <p class="note" style="margin:0 0 12px">Legt fest, wie ASTRA mit einzelnen Absendern umgeht.
          Unbekannte Sender werden nach der Standardregel behandelt.</p>
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
          <label>Unbekannter Absender:</label>
          {_select("sec_unknown_sender_action",
            str((appset or {}).get("secretary", {}).get("unknown_sender_action") or "policy"),
            [("policy", "Normale Policy anwenden"),
             ("ask_owner", "Owner benachrichtigen &amp; fragen"),
             ("block", "Stillschweigend blockieren")])}
          <button class="btn sm ghost" type="button" data-waha-import-contacts>
            Kontakte aus WhatsApp importieren
          </button>
        </div>
        {_render_contact_rules(raw_secretary.get("contact_rules") or [])}
        <div data-contact-rule-list>
        </div>
        <button class="btn sm ghost" type="button" data-contact-rule-add style="margin-top:8px">
          + Regel hinzufügen
        </button>
        <button class="btn" type="submit" style="margin-top:12px">Regeln speichern</button>
      </div>
    </form>

    {f'''<div class="chat-shell" style="height:auto;min-height:640px;margin-top:16px">
      <aside class="chat-side">
        <div class="side-head"><div><small>Live</small><b>Channel Threads</b></div><span class="badge b-off">{len(threads)}</span></div>
        <div class="threads">{''.join(side_rows) or '<div class="arch-note">Noch keine Kanal-Threads.</div>'}</div>
      </aside>
      <section class="chat-main">
        <div class="chat-title">
          <div><span>{esc(_channel_label(selected.get("channel", "")) if selected else "Secretary")}</span>
          <h1>{esc(title or active_id or "Secretary")}</h1></div>
        </div>
        <div class="chat-log">{_render_hub_messages(messages)}</div>
      </section>
    </div>''' if threads else ''}
    <script>
      const activeSetup = {{channel: 'waha'}};
      const labels = {{waha:'WhatsApp / WAHA', signal:'Signal', slack:'Slack', email:'Mail'}};
      const secretaryModeInput = document.querySelector('[data-secretary-mode-input]');
      const secretaryModeButtons = [...document.querySelectorAll('[data-secretary-mode]')];
      secretaryModeButtons.forEach(button => button.addEventListener('click', async () => {{
        const previous = secretaryModeInput.value;
        const mode = button.dataset.secretaryMode;
        const copy = document.querySelector('[data-secretary-master-copy]');
        secretaryModeButtons.forEach(item => item.disabled = true);
        const data = new FormData();
        data.append('csrf', document.querySelector('input[name="csrf"]').value);
        data.append('mode', mode);
        try {{
          const r = await fetch('/admin/secretary/toggle', {{method:'POST', body:data}});
          const d = await r.json();
          if (!r.ok || !d.ok) throw new Error(d.error || 'Speichern fehlgeschlagen');
          secretaryModeInput.value = mode;
          secretaryModeButtons.forEach(item => item.classList.toggle('ghost', item.dataset.secretaryMode !== mode));
          if (copy) copy.textContent = mode === 'off'
            ? 'Aus – Nachrichten werden erfasst, aber ASTRA antwortet niemandem stellvertretend.'
            : mode === 'on'
              ? 'Immer an – ASTRA antwortet auf freigegebenen Kanälen.'
              : 'Automatisch – EduPage bestimmt den echten Schultag; Kalender und Ersatzplan springen nur bei Ausfall ein.';
        }} catch (e) {{
          secretaryModeInput.value = previous;
          alert('Secretary konnte nicht umgeschaltet werden.');
        }} finally {{
          secretaryModeButtons.forEach(item => item.disabled = false);
        }}
      }}));
      function formContext(channel) {{
        const card = document.querySelector(`[data-install="${{channel}}"]`);
        const out = {{}};
        if (!card) return out;
        card.querySelectorAll('input,select').forEach(el => {{
          const key = (el.name || '').replace(/^sec_/, '');
          if (!key) return;
          out[key] = el.type === 'checkbox' ? el.checked : el.value;
        }});
        return out;
      }}
      function updateCoach(channel) {{
        activeSetup.channel = channel;
        document.querySelectorAll('.install-card').forEach(card => card.classList.toggle('active', card.dataset.install === channel));
        document.querySelectorAll('.install-config').forEach(cfg => cfg.hidden = cfg.dataset.config !== channel);
        const ci = document.getElementById('coachIntro');
        if (ci) ci.textContent = `Einrichtung fuer ${{labels[channel]}}.`;
        const ca = document.getElementById('coachAwareness');
        if (ca) {{
          const ctx = formContext(channel);
          const visible = Object.entries(ctx).filter(([,v]) => v !== '' && v !== false).slice(0, 7)
            .map(([k,v]) => `${{k}}=${{String(v).slice(0, 42)}}`).join(' · ');
          ca.innerHTML = '<strong>On-screen</strong><br>' + (visible || 'Noch keine Felder ausgefuellt.');
        }}
      }}
      document.querySelectorAll('[data-open-setup]').forEach(btn => btn.onclick = () => {{
        const channel = btn.dataset.openSetup;
        const cfg = document.querySelector(`.install-config[data-config="${{channel}}"]`);
        if (channel === activeSetup.channel && cfg && !cfg.hidden) {{
          cfg.hidden = true;
        }} else {{
          updateCoach(channel);
          if (cfg) {{ cfg.hidden = false; cfg.scrollIntoView({{behavior:'smooth', block:'nearest'}}); }}
        }}
      }});
      document.querySelectorAll('.install-card input,.install-card select').forEach(el => el.addEventListener('input', () => updateCoach(activeSetup.channel)));
      updateCoach('waha');
      document.querySelectorAll('.install-config').forEach(cfg => cfg.hidden = true);

      const ruleChannelOpts = '<option value="waha">WhatsApp</option><option value="signal">Signal</option><option value="slack">Slack</option><option value="email">Mail</option><option value="*">Alle Kanäle</option>';
      const ruleActionOpts = '<option value="block">Blockieren</option><option value="ask">Owner fragen</option><option value="allow">Erlauben (Policy)</option><option value="direct">Direkt antworten</option>';
      function makeRuleRow() {{
        const row = document.createElement('div');
        row.className = 'contact-rule-row'; row.setAttribute('data-rule-row','');
        const idx = document.querySelectorAll('[data-rule-row]').length;
        row.innerHTML = `<select name="cr_${{idx}}_channel">${{ruleChannelOpts}}</select>`
          + `<input type="text" name="cr_${{idx}}_id" placeholder="+491234… / ID@c.us" style="flex:2">`
          + `<select name="cr_${{idx}}_rule">${{ruleActionOpts}}</select>`
          + `<input type="text" name="cr_${{idx}}_note" placeholder="Notiz" style="flex:2">`
          + `<button class="btn sm ghost" type="button" style="padding:4px 10px" onclick="this.closest('[data-rule-row]').remove()">✕</button>`;
        return row;
      }}
      const ruleAdd = document.querySelector('[data-contact-rule-add]');
      if (ruleAdd) ruleAdd.onclick = () => {{
        document.querySelector('[data-contact-rule-list]').appendChild(makeRuleRow());
      }};
      const wahaImport = document.querySelector('[data-waha-import-contacts]');
      if (wahaImport) wahaImport.onclick = async () => {{
        wahaImport.disabled = true; wahaImport.textContent = 'Lade Kontakte…';
        try {{
          const r = await fetch('/admin/secretary/waha/contacts', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify(formContext('waha'))
          }});
          const d = await r.json();
          if (!d.ok) {{ alert(d.message || 'Fehler beim Importieren.'); }}
          else {{
            const list = document.querySelector('[data-contact-rule-list]');
            (d.rules || []).forEach(rule => {{
              const row = makeRuleRow();
              const idx = document.querySelectorAll('[data-rule-row]').length - 1;
              row.querySelector(`[name="cr_${{idx}}_channel"]`).value = rule.channel;
              row.querySelector(`[name="cr_${{idx}}_id"]`).value = rule.id;
              row.querySelector(`[name="cr_${{idx}}_rule"]`).value = rule.rule;
              row.querySelector(`[name="cr_${{idx}}_note"]`).value = rule.note || '';
              list.appendChild(row);
            }});
            wahaImport.textContent = `${{d.count}} Kontakte importiert ✓`;
          }}
        }} catch(e) {{
          alert('Import fehlgeschlagen.');
        }}
        wahaImport.disabled = false;
      }};
      document.querySelectorAll('[data-email-test]').forEach(btn => {{
        btn.onclick = async () => {{
          const idx = btn.dataset.emailTest;
          const wrap = document.querySelector(`[data-email-test-wrap-${{idx}}]`);
          const box  = document.querySelector(`[data-email-test-box="${{idx}}"]`);
          if (wrap) {{ wrap.hidden = false; wrap.open = true; }}
          if (!box) return;
          box.innerHTML = '<div class="mini">Sende Test-Mail an eigene Adresse und prüfe IMAP…</div>';
          btn.disabled = true;
          try {{
            const r = await fetch('/admin/secretary/email/test', {{
              method:'POST', headers:{{'Content-Type':'application/json'}},
              body: JSON.stringify({{idx: parseInt(idx)}})
            }});
            const d = await r.json();
            if (!d.ok) {{
              box.innerHTML = `<div class="mini" style="color:#f87171">${{d.message || 'Test fehlgeschlagen.'}}</div>`;
            }} else {{
              const info = d.imap_ok
                ? `<div class="mini" style="color:#34d399">✓ SMTP gesendet &amp; IMAP empfangen (${{d.found}} Treffer) · ${{d.from}}</div>`
                : `<div class="mini" style="color:#fbbf24">SMTP ok, IMAP-Abruf schlug fehl: ${{d.imap_error || '?'}}</div>`;
              const rows = (d.messages || []).map(m => {{
                const div = document.createElement('div');
                div.className = 'setup-msg ' + (m.dir === 'sent' ? 'user' : 'bot');
                div.innerHTML = '<b>' + (m.dir === 'sent' ? 'Gesendet' : 'Empfangen') + '</b><span></span>';
                div.querySelector('span').textContent = m.body;
                return div.outerHTML;
              }}).join('');
              box.innerHTML = info + '<div class="setup-log">' + rows + '</div>';
            }}
          }} catch(e) {{
            box.innerHTML = '<div class="mini">Test konnte nicht ausgeführt werden.</div>';
          }}
          btn.disabled = false;
        }};
      }});
      const emailAdd = document.querySelector('[data-email-add]');
      if (emailAdd) emailAdd.onclick = () => {{
        const list = document.querySelector('[data-email-list]');
        const countEl = document.querySelector('[data-email-count]');
        const blocks = list.querySelectorAll('[data-email-account]');
        const idx = blocks.length;
        const tpl = blocks[blocks.length - 1].cloneNode(true);
        tpl.querySelectorAll('input').forEach(el => {{
          if (el.name) el.name = el.name.replace(/sec_email_\\d+_/, 'sec_email_' + idx + '_');
          if (el.type === 'checkbox') el.checked = true; else el.value = '';
        }});
        const head = tpl.querySelector('.email-account-head b');
        if (head) head.textContent = 'Mail-Konto ' + (idx + 1);
        list.appendChild(tpl);
        if (countEl) countEl.value = idx + 1;
      }};

      function setWahaProgress(percent, label) {{
        const wrap = document.querySelector('[data-waha-progress]');
        if (!wrap) return;
        wrap.hidden = false;
        const value = Math.max(0, Math.min(100, percent));
        wrap.querySelector('[data-waha-progress-bar]').style.width = value + '%';
        wrap.querySelector('[data-waha-progress-value]').textContent = value + '%';
        wrap.querySelector('[data-waha-progress-label]').textContent = label;
        const steps = wrap.querySelectorAll('.pairing-steps span');
        steps.forEach((step, i) => step.classList.toggle('active', i <= (value >= 100 ? 2 : value >= 45 ? 1 : 0)));
      }}
      async function loadWahaQr(attempt = 0, recovery = 0) {{
        const box = document.querySelector('[data-qr-box]');
        if (!box) return;
        box.hidden = false;
        setWahaProgress(Math.min(90, 45 + attempt * 3), attempt ? 'WhatsApp erstellt den QR-Code…' : 'QR-Code wird angefordert…');
        box.textContent = attempt ? 'QR wird vorbereitet…' : 'QR wird geladen...';
        const r = await fetch('/admin/secretary/waha/qr', {{
          method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
        }});
        const d = await r.json();
        if (d.image) {{
          setWahaProgress(100, 'QR-Code ist bereit');
          const connect = document.querySelector('[data-waha-connect]');
          if (connect) connect.textContent = 'QR neu laden';
          box.innerHTML = `<img src="${{d.image}}" alt="WhatsApp QR"><small>Nach dem Scannen bestätigt sich die Verbindung automatisch…</small>`;
          pollWahaStatus();
        }} else if (d.connected) {{
          setWahaProgress(100, 'WhatsApp ist verbunden');
          box.textContent = d.message || 'WhatsApp ist bereits verbunden.';
          setTimeout(() => location.reload(), 600);
        }} else if (d.retryable && attempt < 40) {{
          box.textContent = d.message || 'QR wird vorbereitet…';
          setTimeout(() => loadWahaQr(attempt + 1, recovery), 1000);
        }} else if (recovery < 1) {{
          setWahaProgress(35, 'Session wird automatisch neu gestartet…');
          box.textContent = 'WhatsApp braucht einen zweiten Anlauf. ASTRA übernimmt das automatisch…';
          try {{
            await fetch('/admin/secretary/waha/start', {{
              method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
            }});
            setTimeout(() => loadWahaQr(0, recovery + 1), 1200);
          }} catch (e) {{
            box.textContent = 'WAHA ist gerade nicht erreichbar.';
          }}
        }} else box.textContent = d.message || d.error || 'Kein QR verfügbar.';
      }}
      const connectBtn = document.querySelector('[data-waha-connect]');
      if (connectBtn) connectBtn.onclick = async () => {{
        const box = document.querySelector('[data-qr-box]');
        connectBtn.disabled = true;
        setWahaProgress(10, 'Session wird geprüft…');
        if (box) {{ box.hidden = true; box.textContent = ''; }}
        try {{
          const r = await fetch('/admin/secretary/waha/start', {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
          }});
          const d = await r.json();
          if (!d.ok) {{
            setWahaProgress(0, 'Verbindung konnte nicht gestartet werden');
            if (box) box.textContent = d.message || 'Start fehlgeschlagen.';
            return;
          }}
          setWahaProgress(45, 'WhatsApp wird vorbereitet…');
          loadWahaQr();
        }} catch (e) {{
          setWahaProgress(0, 'WAHA ist nicht erreichbar');
          if (box) box.textContent = 'Verbindung konnte nicht gestartet werden.';
        }} finally {{
          connectBtn.disabled = false;
        }}
      }};
      const recoupleBtn = document.querySelector('[data-waha-recouple]');
      if (recoupleBtn) recoupleBtn.onclick = async () => {{
        if (!confirm('WhatsApp wirklich neu koppeln? Die aktuelle Verbindung wird dabei getrennt, bis du den neuen QR-Code gescannt hast.')) return;
        const box = document.querySelector('[data-qr-box]');
        if (box) {{ box.hidden = false; box.textContent = 'Alte Verbindung wird entfernt…'; }}
        recoupleBtn.disabled = true;
        try {{
          const r = await fetch('/admin/secretary/waha/recouple', {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
          }});
          const d = await r.json();
          if (!d.ok) {{
            if (box) box.textContent = d.message || 'Neu koppeln fehlgeschlagen.';
            return;
          }}
        }} catch (e) {{
          if (box) box.textContent = 'Neu koppeln konnte nicht gestartet werden.';
          return;
        }} finally {{
          recoupleBtn.disabled = false;
        }}
        loadWahaQr();
      }};
      const testBtn = document.querySelector('[data-waha-test]');
      if (testBtn) testBtn.onclick = async () => {{
        const wrap = document.querySelector('[data-waha-test-wrap]');
        const box = document.querySelector('[data-waha-test-box]');
        if (wrap) {{ wrap.hidden = false; wrap.open = true; }}
        if (!box) return;
        box.innerHTML = '<div class="mini">Sende Testnachrichten an deinen eigenen Chat…</div>';
        testBtn.disabled = true;
        try {{
          const r = await fetch('/admin/secretary/waha/test', {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
          }});
          const d = await r.json();
          if (!d.ok) {{ box.innerHTML = `<div class="mini">${{d.message || 'Test fehlgeschlagen.'}}</div>`; }}
          else {{
            const head = `<div class="mini">${{d.sent}} Nachrichten gesendet${{d.me ? ' · ' + d.me : ''}} · Spiegel des Selbst-Chats:</div>`;
            const rows = (d.messages || []).map(m => {{
              const div = document.createElement('div');
              div.className = 'setup-msg ' + (m.fromMe ? 'user' : 'bot');
              div.innerHTML = '<b>' + (m.fromMe ? 'Gesendet' : 'Empfangen') + '</b><span></span>';
              div.querySelector('span').textContent = m.body;
              return div.outerHTML;
            }}).join('');
            box.innerHTML = head + '<div class="setup-log">' + rows + '</div>';
          }}
        }} catch (e) {{
          box.innerHTML = '<div class="mini">Test konnte nicht ausgeführt werden.</div>';
        }}
        testBtn.disabled = false;
      }};
      async function loadSignalQr() {{
        const box = document.querySelector('[data-signal-qr-box]');
        if (!box) return;
        box.textContent = 'QR wird geladen...';
        const r = await fetch('/admin/secretary/signal/qr', {{
          method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('signal'))
        }});
        const d = await r.json();
        if (d.image) {{
          box.innerHTML = `<img src="${{d.image}}" alt="Signal QR"><small>In Signal · Verknüpfte Geräte scannen — die Verbindung bestätigt sich automatisch…</small>`;
          pollSignalStatus();
        }} else box.textContent = d.message || d.error || 'Kein QR verfuegbar.';
      }}
      const signalQrBtn = document.querySelector('[data-signal-qr]');
      if (signalQrBtn) signalQrBtn.onclick = loadSignalQr;
      const signalRecouple = document.querySelector('[data-signal-recouple]');
      if (signalRecouple) signalRecouple.onclick = () => {{
        if (!confirm('Signal wirklich neu koppeln? Die aktuelle Geräte-Verknüpfung bleibt bestehen, bis du den neuen QR-Code scannst.')) return;
        loadSignalQr();
      }};
      const signalTestBtn = document.querySelector('[data-signal-test]');
      if (signalTestBtn) signalTestBtn.onclick = async () => {{
        const wrap = document.querySelector('[data-signal-test-wrap]');
        const box = document.querySelector('[data-signal-test-box]');
        if (wrap) {{ wrap.hidden = false; wrap.open = true; }}
        if (!box) return;
        box.innerHTML = '<div class="mini">Sende Testnachrichten an deine eigene Nummer…</div>';
        signalTestBtn.disabled = true;
        try {{
          const r = await fetch('/admin/secretary/signal/test', {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('signal'))
          }});
          const d = await r.json();
          if (!d.ok) {{ box.innerHTML = `<div class="mini">${{d.message || 'Test fehlgeschlagen.'}}</div>`; }}
          else {{
            const head = `<div class="mini">${{d.sent}} Nachrichten gesendet${{d.me ? ' · ' + d.me : ''}} · Spiegel des Selbst-Chats:</div>`;
            const rows = (d.messages || []).map(m => {{
              const div = document.createElement('div');
              div.className = 'setup-msg ' + (m.fromMe ? 'user' : 'bot');
              div.innerHTML = '<b>' + (m.fromMe ? 'Gesendet' : 'Empfangen') + '</b><span></span>';
              div.querySelector('span').textContent = m.body;
              return div.outerHTML;
            }}).join('');
            box.innerHTML = head + '<div class="setup-log">' + rows + '</div>';
          }}
        }} catch (e) {{
          box.innerHTML = '<div class="mini">Test konnte nicht ausgeführt werden.</div>';
        }}
        signalTestBtn.disabled = false;
      }};
      let signalPoll = null;
      function pollSignalStatus() {{
        if (signalPoll) return;
        let tries = 0;
        signalPoll = setInterval(async () => {{
          tries++;
          try {{
            const r = await fetch('/admin/secretary/signal/status', {{
              method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('signal'))
            }});
            const d = await r.json();
            if (d.connected) {{ clearInterval(signalPoll); signalPoll = null; location.reload(); }}
          }} catch (e) {{}}
          if (tries > 40) {{ clearInterval(signalPoll); signalPoll = null; }}
        }}, 3000);
      }}
      let wahaPoll = null;
      function pollWahaStatus() {{
        if (wahaPoll) return;
        let tries = 0;
        wahaPoll = setInterval(async () => {{
          tries++;
          try {{
            const r = await fetch('/admin/secretary/waha/status', {{
              method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(formContext('waha'))
            }});
            const d = await r.json();
            if (d.connected) {{ clearInterval(wahaPoll); wahaPoll = null; location.reload(); }}
          }} catch (e) {{}}
          if (tries > 40) {{ clearInterval(wahaPoll); wahaPoll = null; }}
        }}, 3000);
      }}
    </script>"""
    return _html_with_csrf(page("Secretary", body, active="secretary"), token)


@router.post("/admin/secretary/toggle")
async def secretary_toggle(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return JSONResponse({"ok": False, "error": "Ungültige Sitzung."}, status_code=403)
    mode = str(form.get("mode") or "").strip().lower()
    if mode not in {"auto", "on", "off"}:
        enabled = str(form.get("enabled") or "").lower() in {"1", "true", "yes", "on"}
        mode = "auto" if enabled else "off"
    enabled = mode != "off"
    appset = await _app_settings()
    secretary = appset.get("secretary") if isinstance(appset.get("secretary"), dict) else {}
    secretary["enabled"] = enabled
    secretary["activation_mode"] = mode
    appset["secretary"] = secretary
    await db.set_setting("app_settings", appset)
    await db.audit("secretary_toggled", actor="owner", detail={"enabled": enabled, "mode": mode})
    return JSONResponse({"ok": True, "enabled": enabled, "mode": mode})


@router.post("/admin/secretary")
async def secretary_save(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/secretary", status_code=303)
    appset = await _app_settings()
    raw_secretary = (appset or {}).get("secretary", {}) or {}
    current = secretary_settings(appset)

    def intval(name: str, fallback: int) -> int:
        try:
            return int(form.get(name) or fallback)
        except (TypeError, ValueError):
            return fallback

    channels = {}
    for ch in SECRETARY_SETUP_CHANNELS:
        channels[ch] = {
            "enabled": form.get(f"sec_{ch}_enabled") == "on",
            "mode": str(form.get(f"sec_{ch}_mode") or current["channels"][ch]["mode"]),
            "label": current["channels"][ch].get("label") or _channel_label(ch),
        }
    installations = {
        "waha": {
            "title": str(form.get("sec_waha_title") or "WhatsApp via WAHA"),
            "base_url": str(form.get("sec_waha_base_url") or ""),
            "session": str(form.get("sec_waha_session") or "default"),
            "api_key": str(form.get("sec_waha_api_key") or ""),
            "events": str(form.get("sec_waha_events") or "message"),
            "webhook_url": str(form.get("sec_waha_webhook") or "/ingress/waha"),
        },
        "signal": {
            "title": str(form.get("sec_signal_title") or "Signal"),
            "base_url": str(form.get("sec_signal_base_url") or ""),
            "account": str(form.get("sec_signal_account") or ""),
            "webhook_url": str(form.get("sec_signal_webhook") or "/ingress/signal"),
        },
        "slack": {
            "title": str(form.get("sec_slack_title") or "Slack"),
            "bot_token": str(form.get("sec_slack_bot_token") or ""),
            "default_channel": str(form.get("sec_slack_channel") or "#general"),
            "webhook_url": str(form.get("sec_slack_webhook") or "/ingress/slack"),
        },
    }
    # Multiple email accounts (indexed sec_email_{i}_*). Keep existing passwords
    # when the field is left blank.
    prev_accounts = _email_accounts((appset or {}).get("secretary", {}) or {})
    try:
        email_count = int(form.get("sec_email_count") or 0)
    except (TypeError, ValueError):
        email_count = 0
    email_accounts = []
    for i in range(max(email_count, len(prev_accounts)) + 1):
        imap_host = str(form.get(f"sec_email_{i}_imap_host") or "").strip()
        from_addr = str(form.get(f"sec_email_{i}_from") or "").strip()
        smtp_host = str(form.get(f"sec_email_{i}_smtp_host") or "").strip()
        if not (imap_host or from_addr or smtp_host):
            continue
        prev = prev_accounts[i] if i < len(prev_accounts) else {}
        password = str(form.get(f"sec_email_{i}_password") or "")
        if not password:
            password = prev.get("password", "")
        email_accounts.append({
            "title": str(form.get(f"sec_email_{i}_title") or f"Konto {i + 1}"),
            "enabled": form.get(f"sec_email_{i}_enabled") == "on",
            "from_address": from_addr,
            "imap_host": imap_host,
            "imap_port": str(form.get(f"sec_email_{i}_imap_port") or "993"),
            "imap_user": str(form.get(f"sec_email_{i}_imap_user") or from_addr),
            "password": password,
            "imap_mailbox": str(form.get(f"sec_email_{i}_imap_mailbox") or "INBOX"),
            "smtp_host": smtp_host or imap_host.replace("imap", "smtp"),
            "smtp_port": str(form.get(f"sec_email_{i}_smtp_port") or "587"),
            "poll_minutes": str(form.get(f"sec_email_{i}_poll") or "5"),
            "webhook_url": "/ingress/email",
        })
    # Parse contact rules (cr_{i}_channel/id/rule/note)
    contact_rules_list = []
    i = 0
    while form.get(f"cr_{i}_id") is not None:
        cid = str(form.get(f"cr_{i}_id") or "").strip()
        if cid:
            contact_rules_list.append({
                "channel": str(form.get(f"cr_{i}_channel") or "waha"),
                "id": cid,
                "rule": str(form.get(f"cr_{i}_rule") or "block"),
                "note": str(form.get(f"cr_{i}_note") or ""),
            })
        i += 1

    activation_mode = str(form.get("sec_activation_mode") or current["activation_mode"]).lower()
    if activation_mode not in {"auto", "on", "off"}:
        activation_mode = "auto"
    appset["secretary"] = {
        **raw_secretary,
        "enabled": activation_mode != "off",
        "activation_mode": activation_mode,
        "tone": str(form.get("sec_tone") or "warm"),
        "default_tone": str(form.get("sec_default_tone") or "").strip(),
        "jailbreak_tone": str(form.get("sec_jailbreak_tone") or "firm"),
        "school_direct": form.get("sec_school_direct") == "on",
        "school_start": str(form.get("sec_school_start") or "07:30"),
        "school_end": str(form.get("sec_school_end") or "15:30"),
        "workdays": [int(v) for v in form.getlist("workday") if str(v).isdigit()],
        "confirm_after_minutes": intval("sec_confirm_after", 10),
        "wait_after_minutes": intval("sec_wait_after", 45),
        "group_actions": str(form.get("sec_group_actions") or "owner_grant"),
        "intro": str(form.get("sec_intro") or current["intro"]),
        "header": str(form.get("sec_header") or current["header"]),
        "channels": channels,
        "installations": installations,
        "email_accounts": email_accounts,
        "contact_rules": contact_rules_list,
        "unknown_sender_action": str(form.get("sec_unknown_sender_action") or "policy"),
    }
    await db.set_setting("app_settings", appset)
    await db.audit("secretary_settings_saved", actor="owner",
                   detail={"channels": list(channels), "email_accounts": len(email_accounts),
                           "activation_mode": activation_mode})
    try:
        await _mirror_secretary_to_plugins(installations, channels, email_accounts)
    except Exception:  # noqa: BLE001
        log.warning("Secretary→Plugin mirror failed", exc_info=True)
    return RedirectResponse("/admin/secretary", status_code=303)


async def _mirror_secretary_to_plugins(installations: dict, channels: dict,
                                       email_accounts: list | None = None) -> None:
    """Propagate Secretary channel credentials into the normal plugin store so the
    standard ASTRA agent can use the same accounts. Only channels that have a native
    plugin counterpart are mirrored (Slack, IMAP, SMTP). Each mail account becomes a
    separate, stably-keyed installation of the imap_email and smtp plugins."""
    store = get_config_store()
    changed = False

    slack = installations.get("slack") or {}
    if (slack.get("bot_token") or "").strip():
        from ..plugins.builtin.slack import SlackPlugin
        await store.save(
            SlackPlugin,
            {"bot_token": slack.get("bot_token", ""),
             "default_channel": slack.get("default_channel") or "#general"},
            enabled=bool(channels.get("slack", {}).get("enabled")),
        )
        changed = True

    email_enabled = bool(channels.get("email", {}).get("enabled"))
    for idx, acc in enumerate(email_accounts or []):
        name = acc.get("title") or acc.get("from_address") or f"Mail {idx + 1}"
        acc_enabled = email_enabled and bool(acc.get("enabled", True))
        if (acc.get("imap_host") or "").strip():
            from ..plugins.builtin.imap_email import ImapEmailPlugin
            await store.save_installation(
                ImapEmailPlugin, f"sec-mail-{idx}",
                {"host": acc.get("imap_host", ""),
                 "port": acc.get("imap_port") or "993",
                 "username": acc.get("imap_user") or acc.get("from_address", ""),
                 "password": acc.get("password", ""),
                 "mailbox": acc.get("imap_mailbox") or "INBOX"},
                acc_enabled, name=name,
            )
            changed = True
        if (acc.get("smtp_host") or "").strip():
            from ..plugins.builtin.smtp_mail import SmtpPlugin
            await store.save_installation(
                SmtpPlugin, f"sec-mail-{idx}",
                {"email": acc.get("from_address") or acc.get("imap_user", ""),
                 "password": acc.get("password", ""),
                 "host": acc.get("smtp_host", ""),
                 "port": acc.get("smtp_port") or "465"},
                acc_enabled, name=name,
            )
            changed = True

    if changed:
        try:
            await get_manager().rebuild()
        except Exception:  # noqa: BLE001
            log.debug("manager rebuild after secretary mirror failed", exc_info=True)


def _setup_fallback_reply(channel: str, message: str) -> str:
    base = {
        "waha": (
            "Fuer WAHA brauchst du den Container, eine aktive Session und einen Webhook auf "
            "/ingress/waha. Wichtig sind body, from, fromMe und bei Gruppen die Gruppen-JID."
        ),
        "signal": (
            "Fuer Signal brauchst du signal-cli-rest-api, eine registrierte Nummer und Webhooks auf "
            "/ingress/signal. Achte darauf, groupInfo mitzugeben, damit Gruppen nicht automatisch handeln."
        ),
        "email": (
            "Fuer Mail sollte ein Poller oder Gmail/IMAP-Plugin normalisierte Felder an /ingress/email senden: "
            "from, name, subject und text. Antworten bleiben in der Regel bestaetigungspflichtig."
        ),
    }.get(channel, "Ich helfe dir bei dieser Secretary-Einrichtung.")
    lowered = message.lower()
    if "secret" in lowered or "header" in lowered:
        return "Nutze den HTTP-Header X-Astra-Secret mit deinem CORTEX_SHARED_SECRET. Ohne diesen Header lehnt ASTRA Ingress-Anfragen ab."
    if "gruppe" in lowered or "group" in lowered:
        return "Gruppen sind absichtlich gebremst: ASTRA fragt nach, solange du nicht genau diese Gruppe freigibst. Das ist fuer WhatsApp und Signal wichtig."
    return base


def _clean_setup_context(value: dict | None) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, raw in value.items():
        if "api_key" in key:
            out[key] = "set" if raw else ""
        elif isinstance(raw, (str, int, float, bool)):
            out[key] = str(raw)[:180] if not isinstance(raw, bool) else raw
    return out


@router.post("/admin/secretary/setup-chat")
async def secretary_setup_chat(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    channel = str(data.get("channel") or "")
    message = str(data.get("message") or "").strip()
    context = _clean_setup_context(data.get("context"))
    if channel not in SECRETARY_SETUP_CHANNELS or not message:
        return JSONResponse({"ok": False, "reply": "Diese Einrichtung kenne ich nicht."}, status_code=400)
    store = await db.get_setting("secretary_setup_chats", {}) or {}
    messages = _setup_messages(store, channel)
    messages.append({"role": "user", "content": message, "ts": _now_iso()})
    reply = ""
    try:
        from ..agent import generate_reply_meta
        from ..persona import Register
        result = await generate_reply_meta(
            register=Register.OWNER,
            contact={"id": "owner", "is_owner": True},
            thread_id=f"web-secretary-setup:{channel}",
            channel="web",
            history=[
                {"role": m.get("role", "assistant"), "content": m.get("content", "")}
                for m in messages[-12:]
                if m.get("role") in {"user", "assistant"}
            ],
            extra_system=(
                "Du hilfst Bahrian interaktiv bei einer ASTRA-Secretary-Einrichtung. "
                f"Kanal: {_channel_label(channel)}. Antworte konkret, kurz, mit naechsten Schritten "
                "und bleibe bei Webhook, Sicherheit, Gruppenverhalten, Testpayloads und Fehlersuche. "
                f"Aktuelle Formularfelder: {json.dumps(context, ensure_ascii=False)}"
            ),
            permission_mode="ask",
        )
        reply = result.get("reply") or ""
        if reply.startswith("(ASTRA: kein OpenAI-Key"):
            reply = ""
    except Exception:  # noqa: BLE001
        log.debug("Secretary setup chat used fallback for %s", channel, exc_info=True)
    reply = reply or _setup_fallback_reply(channel, message)
    messages.append({"role": "assistant", "content": reply, "ts": _now_iso()})
    store.setdefault("chats", {})[channel] = messages[-40:]
    await db.set_setting("secretary_setup_chats", store)
    await db.audit("secretary_setup_chat", actor="owner", detail={"channel": channel, "len": len(message)})
    return JSONResponse({"ok": True, "reply": reply, "messages": store["chats"][channel][-8:]})


def _waha_headers(api_key: str) -> dict:
    if not api_key:
        return {}
    return {"X-Api-Key": api_key, "Authorization": f"Bearer {api_key}"}


def _waha_base(value: str) -> str:
    return (value or "").strip().rstrip("/")


async def _waha_request(base_url: str, path: str, *, api_key: str = "", method: str = "GET",
                        json_body: dict | None = None) -> dict:
    import httpx
    base = _waha_base(base_url)
    if not base:
        return {"ok": False, "message": "WAHA Base URL fehlt."}
    url = f"{base}{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(method, url, headers=_waha_headers(api_key), json=json_body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"WAHA nicht erreichbar: {e}"}
    content_type = response.headers.get("content-type", "")
    return {
        "ok": 200 <= response.status_code < 300,
        "status": response.status_code,
        "content_type": content_type,
        "content": response.content,
        "text": response.text[:1200],
        "url": url,
    }


_WAHA_CONNECTED_STATES = {"WORKING", "CONNECTED", "AUTHENTICATED"}
_WAHA_RUNNING_STATES = _WAHA_CONNECTED_STATES | {
    "STARTING", "SCAN_QR_CODE", "PASSKEY_REQUIRED", "PASSKEY_CONFIRMATION_REQUIRED",
}


def _waha_state(result: dict) -> str:
    try:
        data = json.loads(result.get("text") or "{}")
    except json.JSONDecodeError:
        return ""
    return str(data.get("status") or data.get("state") or "").upper()


def _waha_already_running(result: dict) -> bool:
    text = str(result.get("text") or result.get("message") or "").lower()
    return result.get("status") == 422 and "already started" in text


async def _waha_session_status(base_url: str, session: str, api_key: str) -> dict:
    """Query WAHA for the live session state. connected == True means a phone is paired."""
    result = await _waha_request(base_url, f"/api/sessions/{session}", api_key=api_key)
    if not result.get("ok"):
        return {"ok": False, "connected": False, "state": "", "me": "",
                "message": result.get("text") or result.get("message") or ""}
    try:
        data = json.loads(result.get("text") or "{}")
    except json.JSONDecodeError:
        data = {}
    state = str(data.get("status") or data.get("state") or "").upper()
    me = data.get("me") or {}
    label = ""
    chat_id = ""
    if isinstance(me, dict):
        label = me.get("pushName") or me.get("id") or ""
        chat_id = me.get("id") or ""
    return {
        "ok": True,
        "connected": state in _WAHA_CONNECTED_STATES,
        "state": state,
        "me": str(label),
        "chat_id": str(chat_id),
    }


def _image_payload(result: dict, *, source: str) -> dict | None:
    content_type = result.get("content_type", "")
    content = result.get("content") or b""
    if content_type.startswith("image/") and content:
        encoded = base64.b64encode(content).decode("ascii")
        return {"ok": True, "image": f"data:{content_type.split(';')[0]};base64,{encoded}", "source": source}
    try:
        data = json.loads(result.get("text") or "{}")
    except json.JSONDecodeError:
        data = {}
    for key in ("image", "qr", "data", "base64"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            if raw.startswith("data:image/"):
                return {"ok": True, "image": raw, "source": source}
            if len(raw) > 100:
                return {"ok": True, "image": "data:image/png;base64," + raw.split(",", 1)[-1], "source": source}
    return None


@router.post("/admin/secretary/waha/start")
async def secretary_waha_start(request: Request, _: bool = Depends(auth.require_admin)):
    data = await request.json()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or "")
    session = str(data.get("waha_session") or data.get("session") or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or "")
    status = await _waha_request(base_url, f"/api/sessions/{session}", api_key=api_key)
    if status.get("ok"):
        state = _waha_state(status)
        if state in _WAHA_RUNNING_STATES:
            result = {"ok": True}
        elif state == "FAILED":
            result = await _waha_request(
                base_url, f"/api/sessions/{session}/logout", api_key=api_key, method="POST",
            )
            if result.get("ok"):
                result = await _waha_request(
                    base_url, f"/api/sessions/{session}/start", api_key=api_key, method="POST",
                )
        else:
            result = await _waha_request(
                base_url, f"/api/sessions/{session}/start", api_key=api_key, method="POST",
            )
    elif status.get("status") == 404:
        result = await _waha_request(
            base_url, "/api/sessions", api_key=api_key, method="POST",
            json_body={"name": session, "start": True},
        )
    else:
        result = status
    if result.get("ok") or _waha_already_running(result):
        return JSONResponse({"ok": True, "message": f"WAHA-Session {session} wurde gestartet."})
    return JSONResponse({"ok": False, "message": result.get("text") or result.get("message") or "Start fehlgeschlagen."})


@router.post("/admin/secretary/waha/recouple")
async def secretary_waha_recouple(request: Request, _: bool = Depends(auth.require_admin)):
    """Forget only the WhatsApp login and reuse the existing WAHA configuration."""
    data = await request.json()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or "")
    session = str(data.get("waha_session") or data.get("session") or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or "")

    status = await _waha_request(base_url, f"/api/sessions/{session}", api_key=api_key)
    if status.get("ok"):
        logout = await _waha_request(
            base_url, f"/api/sessions/{session}/logout", api_key=api_key, method="POST",
        )
        if not logout.get("ok"):
            return JSONResponse({
                "ok": False,
                "message": logout.get("text") or logout.get("message") or
                           "Die bestehende WhatsApp-Verbindung konnte nicht getrennt werden.",
            })
        start = await _waha_request(
            base_url, f"/api/sessions/{session}/start", api_key=api_key, method="POST",
        )
    elif status.get("status") == 404:
        start = await _waha_request(
            base_url, "/api/sessions", api_key=api_key, method="POST",
            json_body={"name": session, "start": True},
        )
    else:
        start = status

    if not start.get("ok"):
        return JSONResponse({
            "ok": False,
            "message": start.get("text") or start.get("message") or
                       "Die WhatsApp-Session konnte nicht neu gestartet werden.",
        })
    await db.audit("secretary_waha_recouple", actor="owner", detail={"session": session})
    return JSONResponse({
        "ok": True,
        "message": "Die alte WhatsApp-Anmeldung wurde entfernt. Neuer QR-Code wird geladen.",
    })


@router.post("/admin/secretary/waha/status")
async def secretary_waha_status(request: Request, _: bool = Depends(auth.require_admin)):
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or s.waha_base_url or "http://waha:3000")
    session = str(data.get("waha_session") or data.get("session") or s.waha_session or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or s.waha_api_key or "")
    return JSONResponse(await _waha_session_status(base_url, session, api_key))


async def _signal_status(base_url: str, account: str) -> dict:
    """signal-cli-rest-api: GET /v1/accounts lists registered/linked numbers."""
    result = await _waha_request(base_url, "/v1/accounts")
    if not result.get("ok"):
        return {"ok": False, "connected": False, "accounts": []}
    try:
        accounts = json.loads(result.get("text") or "[]")
    except json.JSONDecodeError:
        accounts = []
    accounts = [str(a) for a in accounts] if isinstance(accounts, list) else []
    connected = (account in accounts) if account else bool(accounts)
    return {"ok": True, "connected": connected, "accounts": accounts,
            "me": account or (accounts[0] if accounts else "")}


@router.post("/admin/secretary/waha/contacts")
async def secretary_waha_contacts(request: Request, _: bool = Depends(auth.require_admin)):
    """Fetch contact list from WAHA and return as contact rules (default: ask)."""
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or s.waha_base_url or "http://waha:3000")
    session = str(data.get("waha_session") or data.get("session") or s.waha_session or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or s.waha_api_key or "")
    result = await _waha_request(base_url, f"/api/contacts?session={session}", api_key=api_key)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "message": result.get("text") or "Kontakte konnten nicht geladen werden."})
    try:
        contacts = json.loads(result.get("text") or "[]")
    except json.JSONDecodeError:
        contacts = []
    rules = []
    for c in contacts if isinstance(contacts, list) else []:
        cid = str(c.get("id") or c.get("jid") or "")
        name = str(c.get("name") or c.get("pushname") or c.get("notify") or "")
        if not cid or cid.endswith("@g.us"):
            continue
        rules.append({"channel": "waha", "id": cid, "rule": "ask", "note": name})
    return JSONResponse({"ok": True, "count": len(rules), "rules": rules})


@router.post("/admin/secretary/signal/status")
async def secretary_signal_status(request: Request, _: bool = Depends(auth.require_admin)):
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("signal_base_url") or data.get("base_url") or s.signal_base_url or "http://signal-cli:8080")
    account = str(data.get("signal_account") or data.get("account") or s.signal_phone_number or "")
    return JSONResponse(await _signal_status(base_url, account))


@router.post("/admin/secretary/signal/qr")
async def secretary_signal_qr(request: Request, _: bool = Depends(auth.require_admin)):
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("signal_base_url") or data.get("base_url") or s.signal_base_url or "http://signal-cli:8080")
    device = str(data.get("signal_title") or data.get("title") or "ASTRA").strip() or "ASTRA"
    from urllib.parse import quote
    result = await _waha_request(base_url, f"/v1/qrcodelink?device_name={quote(device)}")
    if result.get("ok"):
        payload = _image_payload(result, source="signal-link")
        if payload:
            return JSONResponse(payload)
    return JSONResponse({
        "ok": False,
        "message": "Kein QR von signal-cli erhalten. Läuft der Container im json-rpc/native-Modus und stimmt die URL?",
    })


@router.post("/admin/secretary/email/test")
async def secretary_email_test(request: Request, _: bool = Depends(auth.require_admin)):
    """Send a test e-mail to the account's own address via SMTP, then check IMAP for it."""
    import asyncio, imaplib, smtplib, ssl
    from email.message import EmailMessage
    data = await request.json()
    idx = int(data.get("idx", 0))
    appset = await _app_settings()
    accounts = _email_accounts((appset or {}).get("secretary", {}) or {})
    if idx >= len(accounts):
        return JSONResponse({"ok": False, "message": "Konto nicht gefunden — bitte zuerst speichern."})
    acc = accounts[idx]
    imap_host = (acc.get("imap_host") or "").strip()
    imap_port = int(acc.get("imap_port") or 993)
    imap_user = (acc.get("imap_user") or acc.get("from_address") or "").strip()
    password  = (acc.get("password") or "").strip()
    smtp_host = (acc.get("smtp_host") or "").strip()
    smtp_port = int(acc.get("smtp_port") or 587)
    from_addr = (acc.get("from_address") or imap_user).strip()
    if not (imap_host and imap_user and password):
        return JSONResponse({"ok": False, "message": "IMAP-Host, User und Passwort müssen gesetzt sein."})
    token = datetime.now().strftime("%H%M%S")
    subject = f"ASTRA Selbsttest #{token}"
    body    = f"ASTRA Mail-Test 1/1 · #{token}\nDieser Test bestätigt, dass SMTP-Senden und IMAP-Empfangen für dieses Konto funktionieren."

    def _smtp_send() -> str:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"]   = from_addr
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            ctx = ssl.create_default_context()
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_host or imap_host.replace("imap", "smtp"), smtp_port, context=ctx, timeout=15) as s:
                    s.login(imap_user, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host or imap_host.replace("imap", "smtp"), smtp_port, timeout=15) as s:
                    s.ehlo(); s.starttls(context=ctx); s.ehlo()
                    s.login(imap_user, password)
                    s.send_message(msg)
            return ""
        except Exception as e:
            return str(e)

    def _imap_check() -> dict:
        try:
            ctx = ssl.create_default_context()
            M = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx)
            M.login(imap_user, password)
            M.select("INBOX")
            _typ, data = M.search(None, f'SUBJECT "#{token}"')
            ids = (data[0] or b"").split()
            msgs = []
            for mid in ids[-3:]:
                _t2, raw = M.fetch(mid, "(BODY[TEXT])")
                text = ""
                if raw and raw[0]:
                    part = raw[0][1] if isinstance(raw[0], tuple) else raw[0]
                    text = part.decode(errors="replace")[:300] if isinstance(part, bytes) else str(part)[:300]
                msgs.append({"id": mid.decode(), "body": text.strip()})
            M.logout()
            return {"ok": True, "found": len(ids), "messages": msgs}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    loop = asyncio.get_event_loop()
    smtp_err = await loop.run_in_executor(None, _smtp_send)
    if smtp_err:
        return JSONResponse({"ok": False, "sent": False,
                             "message": f"SMTP-Fehler: {smtp_err}"})
    await asyncio.sleep(3)
    imap_result = await loop.run_in_executor(None, _imap_check)
    await db.audit("secretary_email_test", actor="owner",
                   detail={"account": idx, "found": imap_result.get("found", 0)})
    messages = []
    messages.append({"dir": "sent", "body": f"[SMTP → {from_addr}] {subject}"})
    for m in imap_result.get("messages", []):
        messages.append({"dir": "received", "body": m.get("body") or "(leer)"})
    return JSONResponse({
        "ok": True, "sent": True,
        "imap_ok": imap_result.get("ok"),
        "found": imap_result.get("found", 0),
        "from": from_addr,
        "messages": messages,
        "imap_error": imap_result.get("error"),
    })


@router.post("/admin/secretary/signal/test")
async def secretary_signal_test(request: Request, _: bool = Depends(auth.require_admin)):
    """Send a few messages from the owner's Signal number to itself, receive them
    back and mirror the result. Each run is tagged so only its messages show."""
    import asyncio
    from urllib.parse import quote
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("signal_base_url") or data.get("base_url") or s.signal_base_url or "http://signal-cli:8080")
    account = str(data.get("signal_account") or data.get("account") or s.signal_phone_number or "")
    status = await _signal_status(base_url, account)
    if not status.get("connected"):
        return JSONResponse({"ok": False, "message": "Signal ist nicht verbunden."})
    account = account or status.get("me") or ""
    if not account:
        return JSONResponse({"ok": False, "message": "Eigene Signal-Nummer konnte nicht ermittelt werden."})
    token = datetime.now().strftime("%H%M%S")
    texts = [
        f"ASTRA Selbsttest 1/3 · #{token}",
        f"ASTRA Selbsttest 2/3 · #{token} — Senden & Empfangen ok?",
        f"ASTRA Selbsttest 3/3 · #{token} — fertig.",
    ]
    sent = 0
    for text in texts:
        res = await _waha_request(
            base_url, "/v2/send", method="POST",
            json_body={"message": text, "number": account, "recipients": [account]},
        )
        if res.get("ok"):
            sent += 1
        await asyncio.sleep(0.4)
    if not sent:
        return JSONResponse({"ok": False, "message": "Testnachrichten konnten nicht gesendet werden."})
    await asyncio.sleep(2.0)
    messages = []
    read = await _waha_request(base_url, f"/v1/receive/{quote(account)}?timeout=3")
    if read.get("ok"):
        try:
            arr = json.loads(read.get("text") or "[]")
        except json.JSONDecodeError:
            arr = []
        for env in arr if isinstance(arr, list) else []:
            envelope = env.get("envelope", env) if isinstance(env, dict) else {}
            dm = (envelope.get("dataMessage") or envelope.get("syncMessage", {}).get("sentMessage") or {})
            body = str(dm.get("message") or "")
            if f"#{token}" not in body:
                continue
            messages.append({"fromMe": True, "body": body, "ts": int(envelope.get("timestamp") or 0)})
        messages.sort(key=lambda x: x["ts"])
    if not messages:
        messages = [{"fromMe": True, "body": t, "ts": 0} for t in texts]
    await db.audit("secretary_signal_test", actor="owner", detail={"sent": sent, "shown": len(messages)})
    return JSONResponse({"ok": True, "sent": sent, "me": account, "messages": messages})


@router.post("/admin/secretary/waha/test")
async def secretary_waha_test(request: Request, _: bool = Depends(auth.require_admin)):
    """Send a few messages to the owner's own WhatsApp chat, read them back and
    mirror the result. Each run is tagged so only this run's messages are shown."""
    import asyncio
    from urllib.parse import quote
    data = await request.json()
    s = get_settings()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or s.waha_base_url or "http://waha:3000")
    session = str(data.get("waha_session") or data.get("session") or s.waha_session or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or s.waha_api_key or "")
    status = await _waha_session_status(base_url, session, api_key)
    if not status.get("connected"):
        return JSONResponse({"ok": False, "message": "WhatsApp ist nicht verbunden."})
    chat_id = status.get("chat_id") or ""
    if not chat_id:
        return JSONResponse({"ok": False, "message": "Eigene WhatsApp-Nummer konnte nicht ermittelt werden."})
    token = datetime.now().strftime("%H%M%S")
    texts = [
        f"ASTRA Selbsttest 1/3 · #{token}",
        f"ASTRA Selbsttest 2/3 · #{token} — Senden & Empfangen ok?",
        f"ASTRA Selbsttest 3/3 · #{token} — fertig.",
    ]
    sent = 0
    for text in texts:
        res = await _waha_request(
            base_url, "/api/sendText", api_key=api_key, method="POST",
            json_body={"session": session, "chatId": chat_id, "text": text},
        )
        if res.get("ok"):
            sent += 1
        await asyncio.sleep(0.4)
    if not sent:
        return JSONResponse({"ok": False, "message": "Testnachrichten konnten nicht gesendet werden."})
    await asyncio.sleep(1.5)
    messages = []
    read = await _waha_request(
        base_url,
        f"/api/{session}/chats/{quote(chat_id)}/messages?limit=40&downloadMedia=false",
        api_key=api_key,
    )
    if read.get("ok"):
        try:
            arr = json.loads(read.get("text") or "[]")
        except json.JSONDecodeError:
            arr = []
        for m in arr if isinstance(arr, list) else []:
            body = str(m.get("body") or "")
            if f"#{token}" not in body:
                continue
            messages.append({
                "fromMe": bool(m.get("fromMe")),
                "body": body,
                "ts": int(m.get("timestamp") or 0),
            })
        messages.sort(key=lambda x: x["ts"])
    if not messages:
        # Reading may be unsupported on this WAHA tier — fall back to what we sent.
        messages = [{"fromMe": True, "body": t, "ts": 0} for t in texts]
    await db.audit("secretary_waha_test", actor="owner", detail={"sent": sent, "shown": len(messages)})
    return JSONResponse({"ok": True, "sent": sent, "me": status.get("me"), "messages": messages})


@router.post("/admin/secretary/waha/qr")
async def secretary_waha_qr(request: Request, _: bool = Depends(auth.require_admin)):
    data = await request.json()
    base_url = str(data.get("waha_base_url") or data.get("base_url") or "")
    session = str(data.get("waha_session") or data.get("session") or "default")
    api_key = str(data.get("waha_api_key") or data.get("api_key") or "")
    status = await _waha_session_status(base_url, session, api_key)
    if status.get("connected"):
        return JSONResponse({
            "ok": True,
            "connected": True,
            "message": "WhatsApp ist bereits verbunden.",
        })
    attempts = [
        (f"/api/{session}/auth/qr", "auth qr"),
        (f"/api/screenshot?session={session}", "screenshot"),
        ("/api/screenshot", "screenshot"),
    ]
    errors = []
    for path, source in attempts:
        result = await _waha_request(base_url, path, api_key=api_key)
        if result["ok"]:
            payload = _image_payload(result, source=source)
            if payload:
                return JSONResponse(payload)
        errors.append(f"{source}: HTTP {result.get('status') or '-'} {result.get('message') or result.get('text') or ''}"[:260])
    return JSONResponse({
        "ok": False,
        "retryable": status.get("state") in {"STARTING", "SCAN_QR_CODE", ""},
        "state": status.get("state") or "",
        "message": (
            "WhatsApp bereitet den QR-Code vor…"
            if status.get("state") in {"STARTING", "SCAN_QR_CODE", ""}
            else "Kein QR-Bild von WAHA erhalten. Session neu starten und erneut versuchen."
        ),
        "errors": errors,
    })


# ─── Chat: multi-thread owner agent ────────────────────────────────────────────
CHAT_KEY = "web_chats_v2"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _msg(role: str, content: str, **extra) -> dict:
    return {"id": f"m_{uuid4().hex[:10]}", "role": role, "content": content, "ts": _now_iso(), **extra}


def _upload_dir() -> Path:
    path = Path(get_settings().brain_data_dir) / "uploads" / "web_chat"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _save_uploads(form) -> list[dict]:
    allowed_prefixes = ("image/", "video/", "audio/")
    attachments = []
    for upload in form.getlist("files"):
        filename = getattr(upload, "filename", "") or ""
        content_type = getattr(upload, "content_type", "") or "application/octet-stream"
        if not filename or not any(content_type.startswith(p) for p in allowed_prefixes):
            continue
        ext = Path(filename).suffix[:12]
        stored = f"{uuid4().hex}{ext}"
        target = _upload_dir() / stored
        size = 0
        with target.open("wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    target.unlink(missing_ok=True)
                    break
                f.write(chunk)
        if target.exists():
            attachments.append({
                "id": stored,
                "name": filename,
                "content_type": content_type,
                "size": size,
                "path": str(target),
                "url": f"/admin/uploads/{stored}",
            })
    return attachments


def _chat_icon(name: str) -> str:
    icons = {
        "edit": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"/>'
            '<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>'
        ),
        "branch": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="6" r="3"/>'
            '<circle cx="18" cy="18" r="3"/><path d="M6 9v3a6 6 0 0 0 6 6h3"/>'
            '<path d="M6 12a6 6 0 0 1 6-6h3"/></svg>'
        ),
        "merge": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="18" r="3"/>'
            '<circle cx="6" cy="6" r="3"/><path d="M6 9v3a6 6 0 0 0 6 6h3"/>'
            '<path d="M18 15V6"/><path d="m15 9 3-3 3 3"/></svg>'
        ),
        "copy": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>'
        ),
        "delete": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/>'
            '<path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>'
            '<path d="M10 11v5M14 11v5"/></svg>'
        ),
        "shield": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 20 7v5c0 5-3.4 8.6-8 9'
            ' -4.6-.4-8-4-8-9V7Z"/><path d="m9 12 2 2 4-5"/></svg>'
        ),
        "send": (
            '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m22 2-7 20-4-9-9-4Z"/>'
            '<path d="M22 2 11 13"/></svg>'
        ),
    }
    return icons[name]


def _icon_button(action: str, mid: str, label: str) -> str:
    return (
        f'<button class="icon-btn" data-{action}="{esc(mid)}" '
        f'title="{esc(label)}" aria-label="{esc(label)}">{_chat_icon(action)}</button>'
    )


def _new_chat(title: str = "Neuer Chat", *, messages: list[dict] | None = None) -> dict:
    now = _now_iso()
    return {
        "id": f"c_{uuid4().hex[:10]}",
        "title": title,
        "archived": False,
        "parent_id": None,
        "branch_base_count": 0,
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
    for key in ("source_channel", "source_thread_id", "source_tag"):
        if key in c:
            out[key] = c[key]
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


def _channel_chat_id(thread_id: str) -> str:
    return "channel_" + hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:12]


def _channel_message(thread_id: str, idx: int, message: dict) -> dict:
    role = "assistant" if message.get("role") == "assistant" else "user"
    created = message.get("created_at")
    seed = f"{thread_id}:{idx}:{message.get('role')}:{message.get('content')}"
    return {
        "id": f"m_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}",
        "role": role,
        "content": message.get("content", ""),
        "ts": created.isoformat() if hasattr(created, "isoformat") else (created or _now_iso()),
        "source_channel": thread_id.split(":", 1)[0],
    }


async def _sync_channel_threads_into_chats(store: dict) -> None:
    try:
        threads = await db.list_threads(80)
    except Exception:  # noqa: BLE001
        return
    for t in threads:
        if t.get("channel") != "telegram":
            continue
        tid = t.get("thread_id") or ""
        if not tid:
            continue
        cid = _channel_chat_id(tid)
        try:
            messages = await db.recent_messages(tid, 80)
        except Exception:  # noqa: BLE001
            messages = []
        mapped = [_channel_message(tid, i, m) for i, m in enumerate(messages)]
        existing = next((c for c in store["chats"] if c.get("id") == cid), None)
        if not existing:
            existing = _new_chat(t.get("who") or tid, messages=[])
            existing["id"] = cid
            existing["source_channel"] = "telegram"
            existing["source_thread_id"] = tid
            existing["source_tag"] = "from Telegram"
            store["chats"].append(existing)
        existing["title"] = t.get("who") or existing.get("title") or tid
        existing["source_channel"] = "telegram"
        existing["source_thread_id"] = tid
        existing["source_tag"] = "from Telegram"
        existing["permission_mode"] = "ask"
        existing["messages"] = mapped
        existing["updated_at"] = _now_iso()


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
    messages = []
    for m in chat.get("messages", []):
        if m.get("role") not in ("user", "assistant"):
            continue
        content = _friendly_stored_tool_result(m.get("content", ""))
        if m.get("attachments"):
            refs = [
                f"- {a.get('name')} ({a.get('content_type')}, {a.get('path')})"
                for a in m.get("attachments", [])
            ]
            content = f"{content}\n\nAnhänge:\n" + "\n".join(refs)
        messages.append({"role": m["role"], "content": content})
    return messages[-40:]


def _friendly_stored_tool_result(content: str) -> str:
    """Collapse legacy raw tool-result messages already persisted in chat history."""
    text = str(content or "")
    candidates = [text]
    if text.startswith("Ausgeführt:") and "\n\n" in text:
        candidates.insert(0, text.split("\n\n", 1)[1].strip())
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("summary"):
            return str(payload["summary"])
    return text


async def _refresh_agent_tools() -> None:
    try:
        from ..admin_tools import register_admin_tools
        await get_manager().rebuild()
        register_admin_tools()
    except Exception:  # noqa: BLE001
        log.warning("Could not refresh agent tools before web chat.", exc_info=True)


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
        source = f'<span class="source-tag">{esc(c.get("source_tag"))}</span>' if c.get("source_tag") else ""
        thread = (
            f'<a class="thread{active}" href="{href}">'
            f'<span>{esc(c.get("title") or "Neuer Chat")}</span>'
            f'<small>{source}{len(c.get("messages", []))} Nachrichten · {esc(_mode_label(c.get("permission_mode", "ask")))}</small>'
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
                _icon_button("edit", m["id"], "Bearbeiten")
                + _icon_button("branch", m["id"], "Branch erstellen")
                + _icon_button("copy", m["id"], "Kopieren")
                + _icon_button("delete", m["id"], "Löschen")
            )
        elif role == "assistant":
            actions = (
                _icon_button("branch", m["id"], "Branch erstellen")
                + _icon_button("copy", m["id"], "Kopieren")
                + _icon_button("delete", m["id"], "Löschen")
            )
        pending = ""
        if m.get("pending_action"):
            p = m["pending_action"]
            pa = p.get("args", {}) or {}
            is_send = p.get("tool") == "astra_send_message"
            if is_send:
                ch_label = {"waha": "WhatsApp", "signal": "Signal",
                            "telegram": "Telegram"}.get(pa.get("channel"), pa.get("channel") or "")
                head_small, head_b = "Nachricht senden", f"An {pa.get('to','?')} · {ch_label}"
                body = f'<blockquote class="action-msg">{esc(pa.get("text",""))}</blockquote>'
                run_label = "Senden"
            else:
                head_small, head_b = "Freigabe erforderlich", str(p.get("tool"))
                body = (
                    '<p>Diese Aktion wartet auf deine Entscheidung.</p>'
                    '<details class="action-details"><summary>Argumente anzeigen</summary>'
                    f'<pre>{esc(json.dumps(pa, ensure_ascii=False, indent=2))}</pre></details>'
                )
                run_label = "Ausführen"
            pending = (
                '<div class="action-card">'
                '<div class="action-head">'
                f'<span class="action-mark">{_chat_icon("send" if is_send else "shield")}</span>'
                f'<div><small>{esc(head_small)}</small><b>{esc(head_b)}</b></div></div>'
                f'{body}'
                '<div class="action-row">'
                f'<button class="btn sm" data-run-action="{esc(m["id"])}">{esc(run_label)}</button>'
                f'<button class="btn ghost sm danger" data-deny-action="{esc(m["id"])}">Ablehnen</button>'
                '</div></div>'
            )
        tool_cards = ""
        if m.get("tool_calls"):
            cards = []
            for call in m.get("tool_calls", [])[:6]:
                ok = call.get("ok")
                state = "ok" if ok is True else "warn" if ok is False else ""
                cards.append(
                    f'<details class="tool-card {state}"><summary>'
                    f'<b>{esc(call.get("tool", "tool"))}</b><span>{esc(call.get("summary", ""))}</span>'
                    '</summary>'
                    f'<pre>{esc(json.dumps(call, ensure_ascii=False, indent=2))}</pre></details>'
                )
            tool_cards = '<div class="tool-cards">' + "".join(cards) + '</div>'
        attachments = ""
        if m.get("attachments"):
            items = []
            for a in m.get("attachments", []):
                kind = str(a.get("content_type", "")).split("/", 1)[0]
                items.append(
                    f'<a class="attachment" href="{esc(a.get("url", "#"))}" target="_blank" rel="noopener">'
                    f'<b>{esc(kind or "file")}</b><span>{esc(a.get("name", "Anhang"))}</span></a>'
                )
            attachments = '<div class="attachments">' + "".join(items) + '</div>'
        content = _friendly_stored_tool_result(m.get("content", ""))
        msgs.append(
            f'<div class="msg-row {cls}" data-mid="{esc(m["id"])}">'
            f'<div class="msg {cls}"><span class="msg-content">{esc(content)}</span>{attachments}{tool_cards}{pending}</div>'
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
    await _sync_channel_threads_into_chats(store)
    archive_view = view == "archive"
    active = _select_chat(store, chat or None, archived=archive_view)
    await _save_chat_store(store)
    appset = await _app_settings()
    autonomy = appset.get("autonomy", "ask")
    mode = (active or {}).get("permission_mode", "ask")
    active_id = (active or {}).get("id", "")
    title = (active or {}).get("title") or "Archiv"
    source_tag = (active or {}).get("source_tag")
    mode_cls = mode if mode in ("ask", "auto", "bypass") else "auto"
    autonomy_cls = autonomy if autonomy in ("ask", "confident", "full") else "ask"
    active_count = sum(1 for c in store["chats"] if not c.get("archived"))
    archived_count = sum(1 for c in store["chats"] if c.get("archived"))
    can_merge = bool(active and active.get("parent_id") and not archive_view)
    delete_chat_btn = (
        '<button class="icon-btn title-icon danger" id="deletechat" title="Chat löschen" '
        f'aria-label="Chat löschen">{_chat_icon("delete")}</button>'
        if active else ""
    )
    title_actions = (
        (
            f'<button class="btn sm" id="restorechat">Wiederherstellen</button>'
            if archive_view and active else
            (
                '<button class="icon-btn title-icon" id="mergechat" title="In Ursprung mergen" '
                f'aria-label="In Ursprung mergen">{_chat_icon("merge")}</button>'
                if can_merge else ""
            )
            + '<button class="icon-btn title-icon" id="branchchat" title="Branch erstellen" '
            f'aria-label="Branch erstellen">{_chat_icon("branch")}</button>'
        )
        + delete_chat_btn
    )
    archive_button = "" if archive_view else '<button class="btn ghost sm" id="archivechat">Archivieren</button>'
    input_html = (
        '<div class="chat-input archived"><p>Archivierter Thread. Wiederherstellen, um weiterzuschreiben.</p>'
        '<button class="btn sm" id="restorebottom">Wiederherstellen</button></div>'
        if archive_view and active else
        '<div class="chat-input">'
        '<label class="icon-btn upload-btn" title="Foto, Video oder Audio anhängen">'
        '<input id="files" type="file" accept="image/*,video/*,audio/*" multiple hidden>'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.7-9.7a4 4 0 0 1 5.7 5.7L9.2 18.2a2 2 0 1 1-2.8-2.8l8.8-8.8"/></svg>'
        '</label>'
        '<textarea id="inp" placeholder="Nachricht oder Aufgabe an ASTRA…" rows="1"></textarea>'
        f'<button class="btn send-btn" id="send">{_chat_icon("send")}<span>Senden</span></button>'
        '<button class="btn ghost sm" id="clear" title="Verlauf leeren">Leeren</button>'
        '</div>'
    )
    messages_html = _render_messages(active) if active else '<div class="msg sys">Noch nichts im Archiv.</div>'
    body = f"""
    <div class="chat-shell" data-chat="{esc(active_id)}" data-view="{'archive' if archive_view else 'active'}">
      <aside class="chat-side">
        <div class="side-head"><div><small>Workspace</small><b>ASTRA Chat</b></div><button class="btn sm" id="newchat">Neu</button></div>
        <div class="chat-tabs">
          <a class="{'active' if not archive_view else ''}" href="/admin/chat">Aktiv <span>{active_count}</span></a>
          <a class="{'active' if archive_view else ''}" href="/admin/chat?view=archive">Archiv <span>{archived_count}</span></a>
        </div>
        <div class="threads">{_render_chat_list(store, active_id, archived=archive_view)}</div>
        <div class="perm-box">
          <div class="perm-head">
            <span class="perm-icon">{_chat_icon("shield")}</span>
            <div><small>Berechtigungen</small><strong>Ausführung</strong></div>
          </div>
          <div class="perm-grid">
            <label>Modus</label>
            <select id="perm" {"disabled" if archive_view else ""}>
              <option value="ask" {"selected" if mode == "ask" else ""}>Fragen</option>
              <option value="auto" {"selected" if mode == "auto" else ""}>Auto</option>
              <option value="bypass" {"selected" if mode == "bypass" else ""}>Berechtigungen umgehen</option>
            </select>
            <label>Autonomie</label>
            <select id="autonomy" {"disabled" if archive_view else ""}>
              <option value="ask" {"selected" if autonomy == "ask" else ""}>ask</option>
              <option value="confident" {"selected" if autonomy == "confident" else ""}>confident</option>
              <option value="full" {"selected" if autonomy == "full" else ""}>full</option>
            </select>
          </div>
          <div class="perm-status mode-{esc(mode_cls)}">
            <span></span>{esc(_mode_label(mode))}
          </div>
        </div>
        {archive_button}
      </aside>
      <section class="chat-main">
        <div class="chat-title">
          <div><span>{"Archivierter Thread" if archive_view else "Thread"}</span><h1>{esc(title)} {f'<span class="source-tag">{esc(source_tag)}</span>' if source_tag else ''}</h1>
            <div class="chat-state">
              <span class="mode-pill mode-{esc(mode_cls)}">{esc(_mode_label(mode))}</span>
              <span class="mode-pill autonomy-{esc(autonomy_cls)}">Autonomie {esc(autonomy)}</span>
            </div>
          </div>
          <div class="chat-title-actions">{title_actions}</div>
        </div>
        <div class="chat-log" id="log">{messages_html}</div>
        {input_html}
      </section>
    </div>
    <div class="confirm-modal" id="confirmModal" hidden>
      <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
        <div class="confirm-mark">{_chat_icon("shield")}</div>
        <div>
          <h2 id="confirmTitle">Bestätigen</h2>
          <p id="confirmText">Diese Aktion ausführen?</p>
          <div class="confirm-actions">
            <button class="btn ghost sm" id="confirmCancel" type="button">Abbrechen</button>
            <button class="btn sm" id="confirmOk" type="button">Bestätigen</button>
          </div>
        </div>
      </div>
    </div>
    <script>
      const root=document.querySelector('.chat-shell'), chatId=root.dataset.chat, archiveView=root.dataset.view==='archive';
      const log=document.getElementById('log'), inp=document.getElementById('inp'), files=document.getElementById('files');
      const perm=document.getElementById('perm'), autonomy=document.getElementById('autonomy');
      const scroll=()=>log.scrollTop=log.scrollHeight; scroll();
      function add(role,txt){{const r=document.createElement('div');r.className='msg-row '+role;
        const b=document.createElement('div');b.className='msg '+role;b.textContent=txt;r.appendChild(b);log.appendChild(r);scroll();return r;}}
      async function post(url, data){{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data||{{}})}});return await r.json();}}
      async function postForm(url, fd){{const r=await fetch(url,{{method:'POST',body:fd}});return await r.json();}}
      async function restore(id){{const d=await post('/admin/chat/restore',{{chat_id:id||chatId}}); location.href='/admin/chat?chat='+encodeURIComponent(d.chat_id||id||chatId);}}
      async function copyText(text, btn){{
        let ok=false;
        try {{
          if(navigator.clipboard && window.isSecureContext){{
            await navigator.clipboard.writeText(text); ok=true;
          }}
        }} catch(e) {{ ok=false; }}
        if(!ok){{
          // Plain-HTTP LAN has no async clipboard — use the legacy execCommand path.
          try {{
            const ta=document.createElement('textarea');
            ta.value=text; ta.setAttribute('readonly','');
            ta.style.position='fixed'; ta.style.top='-1000px'; ta.style.opacity='0';
            document.body.appendChild(ta); ta.focus(); ta.select();
            ta.setSelectionRange(0, text.length);
            ok=document.execCommand('copy'); document.body.removeChild(ta);
          }} catch(e) {{ ok=false; }}
        }}
        if(btn){{
          btn.classList.add(ok?'copied':'copy-fail');
          setTimeout(()=>btn.classList.remove('copied','copy-fail'), 1100);
        }}
      }}
      function confirmChoice(title, text, okLabel='Bestätigen', tone='default') {{
        return new Promise(resolve => {{
          const modal=document.getElementById('confirmModal');
          const titleEl=document.getElementById('confirmTitle'), textEl=document.getElementById('confirmText');
          const ok=document.getElementById('confirmOk'), cancel=document.getElementById('confirmCancel');
          titleEl.textContent=title; textEl.textContent=text; ok.textContent=okLabel;
          ok.classList.toggle('danger', tone==='danger');
          modal.hidden=false;
          const close = value => {{
            modal.hidden=true;
            ok.onclick=null; cancel.onclick=null; modal.onclick=null; document.onkeydown=null;
            resolve(value);
          }};
          ok.onclick=()=>close(true); cancel.onclick=()=>close(false);
          modal.onclick=e=>{{if(e.target===modal) close(false);}};
          document.onkeydown=e=>{{if(e.key==='Escape') close(false);}};
        }});
      }}
      async function saveSettings(){{if(!archiveView&&chatId) await post('/admin/chat/settings',{{chat_id:chatId,permission_mode:perm.value,autonomy:autonomy.value}});}}
      if(perm) perm.onchange=saveSettings; if(autonomy) autonomy.onchange=saveSettings;
      if(inp) {{
        inp.addEventListener('input',()=>{{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,220)+'px';}});
        inp.addEventListener('keydown',e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();go();}}}});
        document.getElementById('send').onclick=go;
      }}
      async function go(){{
        const t=inp.value.trim(); const hasFiles=files&&files.files.length; if(!t&&!hasFiles) return; inp.value=''; inp.style.height='auto';
        add('user',t); const typing=add('typing','ASTRA arbeitet…');
        try{{
          await saveSettings();
          const fd=new FormData(); fd.set('chat_id',chatId); fd.set('message',t); fd.set('permission_mode',perm.value);
          if(files){{[...files.files].forEach(f=>fd.append('files',f)); files.value='';}}
          const d=await postForm('/admin/chat/send',fd);
          typing.remove(); location.href='/admin/chat?chat='+encodeURIComponent(d.chat_id||chatId);
        }}catch(e){{ typing.remove(); add('bot','Fehler: '+e); }}
      }}
      document.getElementById('newchat').onclick=async()=>{{const d=await post('/admin/chat/new',{{}}); location.href='/admin/chat?chat='+d.chat_id;}};
      const archiveBtn=document.getElementById('archivechat'), branchBtn=document.getElementById('branchchat');
      const mergeBtn=document.getElementById('mergechat');
      const clearBtn=document.getElementById('clear'), restoreBtn=document.getElementById('restorechat');
      const restoreBottom=document.getElementById('restorebottom');
      if(archiveBtn) archiveBtn.onclick=async()=>{{if(!await confirmChoice('Chat archivieren', 'Der Thread bleibt erhalten und wandert ins Archiv.', 'Archivieren')) return;
        await post('/admin/chat/archive',{{chat_id:chatId}}); location.href='/admin/chat?view=archive&chat='+encodeURIComponent(chatId);}};
      if(branchBtn) branchBtn.onclick=async()=>{{const d=await post('/admin/chat/branch',{{chat_id:chatId}}); location.href='/admin/chat?chat='+d.chat_id;}};
      const deleteChatBtn=document.getElementById('deletechat');
      if(deleteChatBtn) deleteChatBtn.onclick=async()=>{{
        if(!await confirmChoice('Chat löschen', 'Dieser Chat wird dauerhaft gelöscht — das lässt sich nicht rückgängig machen.', 'Löschen', 'danger')) return;
        await post('/admin/chat/delete',{{chat_id:chatId}}); location.href='/admin/chat';}};
      if(mergeBtn) mergeBtn.onclick=async()=>{{
        if(!await confirmChoice('Branch mergen', 'Neue Nachrichten werden in den Ursprung übernommen.', 'Mergen')) return;
        const d=await post('/admin/chat/merge',{{chat_id:chatId}});
        location.href='/admin/chat?chat='+encodeURIComponent(d.chat_id||chatId);
      }};
      if(clearBtn) clearBtn.onclick=async()=>{{if(!await confirmChoice('Verlauf leeren', 'Alle Nachrichten in diesem Chat werden entfernt.', 'Leeren', 'danger')) return;
        await post('/admin/chat/clear',{{chat_id:chatId}}); location.reload();}};
      if(restoreBtn) restoreBtn.onclick=()=>restore(chatId);
      if(restoreBottom) restoreBottom.onclick=()=>restore(chatId);
      document.querySelectorAll('[data-restore-chat]').forEach(b=>b.onclick=()=>restore(b.dataset.restoreChat));
      log.onclick=async e=>{{
        const edit=e.target.closest('[data-edit]'), branch=e.target.closest('[data-branch]');
        const copy=e.target.closest('[data-copy]'), del=e.target.closest('[data-delete]');
        const run=e.target.closest('[data-run-action]'), deny=e.target.closest('[data-deny-action]');
        if(edit){{const current=edit.closest('.msg-row').querySelector('.msg').childNodes[0].textContent;
          const text=prompt('Nachricht bearbeiten. Alles danach wird abgeschnitten:', current);
          if(text!==null){{await post('/admin/chat/edit',{{chat_id:chatId,message_id:edit.dataset.edit,content:text}}); location.reload();}}}}
        if(branch){{const d=await post('/admin/chat/branch',{{chat_id:chatId,message_id:branch.dataset.branch}}); location.href='/admin/chat?chat='+d.chat_id;}}
        if(copy){{const row=copy.closest('.msg-row'); const text=row.querySelector('.msg-content')?.textContent||''; await copyText(text, copy);}}
        if(del){{if(!await confirmChoice('Nachricht löschen', 'Diese Nachricht wird aus dem Thread entfernt.', 'Löschen', 'danger')) return;
          await post('/admin/chat/delete_message',{{chat_id:chatId,message_id:del.dataset.delete}}); location.reload();}}
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
    attachments = []
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        data = dict(form)
        attachments = await _save_uploads(form)
    else:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
    msg = (data.get("message") or "").strip()
    if not msg and not attachments:
        return JSONResponse({"reply": "(leer)"})
    st = get_settings()
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    if data.get("permission_mode") in ("ask", "auto", "bypass"):
        chat["permission_mode"] = data["permission_mode"]
    user_msg = _msg("user", msg or "Anhang", attachments=attachments)
    chat["messages"].append(user_msg)
    if len([m for m in chat["messages"] if m["role"] == "user"]) == 1:
        chat["title"] = _title_from(msg)
    try:
        await _refresh_agent_tools()
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
    if result.get("tool_calls"):
        bot_msg["tool_calls"] = result["tool_calls"]
    if result.get("pending_action"):
        bot_msg["pending_action"] = result["pending_action"]
        chat["pending_action"] = {"message_id": bot_msg["id"], **result["pending_action"]}
    chat["messages"].append(bot_msg)
    chat["updated_at"] = _now_iso()
    chat["messages"] = chat["messages"][-80:]
    await _save_chat_store(store)
    await db.audit("web_chat", actor="owner", detail={"len": len(msg), "chat_id": chat["id"]})
    return JSONResponse({"reply": bot_msg["content"], "chat_id": chat["id"]})


@router.get("/admin/uploads/{filename}")
async def chat_upload(filename: str, _: bool = Depends(auth.require_admin)):
    if "/" in filename or "\\" in filename:
        return JSONResponse({"error": "bad filename"}, status_code=400)
    path = _upload_dir() / filename
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


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
    await _refresh_agent_tools()
    result = await dispatch(pending["tool"], pending.get("args") or {}, ctx)
    display_result = result
    try:
        parsed_result = json.loads(result)
        if isinstance(parsed_result, dict) and parsed_result.get("summary"):
            display_result = str(parsed_result["summary"])
    except (json.JSONDecodeError, TypeError):
        pass
    target.pop("pending_action", None)
    chat["pending_action"] = None
    chat["messages"].append(_msg("assistant", display_result))
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
    child["parent_id"] = chat["id"]
    child["branch_base_count"] = len(copied)
    store["chats"].insert(0, child)
    store["active_id"] = child["id"]
    await _save_chat_store(store)
    return JSONResponse({"chat_id": child["id"]})


@router.post("/admin/chat/merge")
async def chat_merge(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    parent_id = chat.get("parent_id")
    parent = next((c for c in store["chats"] if c["id"] == parent_id), None)
    if not parent:
        return JSONResponse({"ok": False, "error": "Kein Ursprung für diesen Branch.", "chat_id": chat["id"]})
    base = int(chat.get("branch_base_count") or 0)
    additions = []
    for m in chat.get("messages", [])[base:]:
        if m.get("pending_action"):
            continue
        additions.append({**m, "id": f"m_{uuid4().hex[:10]}", "merged_from": chat["id"]})
    if additions:
        parent["messages"].extend(additions)
        parent["messages"] = parent["messages"][-80:]
        parent["updated_at"] = _now_iso()
    chat["merged_at"] = _now_iso()
    store["active_id"] = parent["id"]
    await _save_chat_store(store)
    return JSONResponse({"ok": True, "chat_id": parent["id"], "merged": len(additions)})


@router.post("/admin/chat/delete_message")
async def chat_delete_message(request: Request, _: bool = Depends(auth.require_admin)):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    store = await _chat_store()
    chat = _get_chat(store, data.get("chat_id"))
    mid = data.get("message_id")
    before = len(chat.get("messages", []))
    chat["messages"] = [m for m in chat.get("messages", []) if m.get("id") != mid]
    if len(chat["messages"]) == before:
        return JSONResponse({"ok": False, "error": "Nachricht nicht gefunden."})
    if (chat.get("pending_action") or {}).get("message_id") == mid:
        chat["pending_action"] = None
    chat["updated_at"] = _now_iso()
    await _save_chat_store(store)
    return JSONResponse({"ok": True})


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


@router.post("/admin/chat/delete")
async def chat_delete(request: Request, _: bool = Depends(auth.require_admin)):
    """Permanently remove a whole chat (not just clear it)."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    cid = data.get("chat_id")
    store = await _chat_store()
    store["chats"] = [c for c in store["chats"] if c.get("id") != cid]
    if store.get("active_id") == cid:
        store["active_id"] = next((c["id"] for c in store["chats"] if not c.get("archived")), None)
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
            background: linear-gradient(180deg, rgba(16, 16, 21, 0.92), rgba(7, 7, 10, 0.98));
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.035);
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
            filter: drop-shadow(0 0 30px rgba(170, 180, 214, 0.32));
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
            background: var(--accent);
            color: var(--accent-ink);
            border: none;
            padding: 12px 28px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 12px 28px rgba(244, 245, 248, 0.08);
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }}

        .card-actions .btn-primary:hover {{
            transform: translateY(-2px);
            box-shadow: 0 16px 36px rgba(244, 245, 248, 0.14);
            filter: brightness(.94);
        }}

        .card-actions .btn-primary:active {{
            transform: translateY(0);
        }}

        .card-actions .btn-primary:disabled {{
            background: var(--surface-2);
            color: var(--text-faint);
            border: 1px solid var(--border);
            box-shadow: none;
            cursor: not-allowed;
            filter: none;
            transform: none;
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
            color: var(--text);
            font-weight: 500;
            margin-right: 8px;
        }}

        .card-actions .btn-danger {{
            background: transparent;
            color: #fecdd3;
            border: 1px solid rgba(251, 113, 133, 0.34);
            padding: 10px 18px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        }}

        .card-actions .btn-danger:hover {{
            background: rgba(251, 113, 133, 0.12);
            border-color: rgba(251, 113, 133, 0.55);
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
            filter: drop-shadow(0 0 30px rgba(170, 180, 214, 0.32));
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
            background: var(--surface);
            border: 1px solid var(--border);
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
            color: var(--link);
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
            color: var(--link);
            font-weight: bold;
        }}

        .commit-item span.feat {{ color: var(--link); }}
        .commit-item span.fix {{ color: #fecdd3; }}
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
            padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:13px;overflow-x:auto;margin:0">cd /opt/astra &amp;&amp; git pull --ff-only origin main &amp;&amp; docker compose up -d --build cortex

# Falls der Server divergiert ist:
cd /opt/astra &amp;&amp; git fetch --tags origin main &amp;&amp; git branch server-backup/$(date -u +%Y%m%d-%H%M%S) HEAD &amp;&amp; git reset --hard origin/main &amp;&amp; docker compose up -d --build cortex</pre>
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
                cardSubtitle.textContent = 'Getrackte lokale Git-Änderungen blockieren Pulls. Erst committen/stashen/aufräumen.';
            }} else if (d.diverged) {{
                cardSubtitle.textContent += ' · Divergenz erkannt: ASTRA legt beim Update eine Backup-Branch an und synchronisiert origin/main.';
            }} else if (d.untracked_count) {{
                cardSubtitle.textContent += ` · ${{d.untracked_count}} ungetrackte Datei(en) ignoriert.`;
            }}
            renderCommitLines(d.commits || []);
        }}

        /* --- UPDATE STATUS FETCH --- */
        async function fetchGitHubCommits() {{
            const subtitleEl = document.getElementById('cardSubtitle');

            try {{
                const response = await fetch('/admin/update/check', {{method: 'POST'}});
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


# ─── Wissen / Brain files (live-editable markdown about me + each person) ───────
_BRAIN_TAGS = ["über mich", "persona", "routinen", "personen", "person", "sonstiges"]


@router.get("/admin/brain", response_class=HTMLResponse)
async def brain_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    files = knowledge.list_files()
    token = await auth.issue_csrf()
    flash = '<div class="flash ok">Gespeichert.</div>' if saved else ""
    present = [t for t in _BRAIN_TAGS if any(f["tag"] == t for f in files)]
    chips = '<span class="chip active" data-tag="all">Alle</span>' + "".join(
        f'<span class="chip" data-tag="{esc(t)}">{esc(t)}</span>' for t in present)

    cards = []
    for f in files:
        content = knowledge.read_file(f["rel"])
        cards.append(f"""
        <details class="card brain" data-name="{esc((f['title'] + ' ' + f['rel'] + ' ' + f['preview']).lower())}"
                 data-tag="{esc(f['tag'])}">
          <summary>
            <div class="meta"><h3>{esc(f['title'])} <span class="tag-katalog">{esc(f['tag'])}</span></h3>
              <div class="cat">{esc(f['rel'])} · {f['lines']} Zeilen · {esc(f['mtime'])}</div>
              <p class="note" style="margin:6px 0 0">{esc(f['preview'])}</p></div>
            <span class="chev">▾</span>
          </summary>
          <form method="post" action="/admin/brain/save" style="margin-top:12px">
            <input type="hidden" name="csrf" value="{esc(token)}">
            <input type="hidden" name="file" value="{esc(f['rel'])}">
            <textarea name="content" class="brain-edit" spellcheck="false">{esc(content)}</textarea>
            <div class="row" style="margin-top:10px"><button class="btn" type="submit">Speichern</button>
              <span class="note">Markdown · ASTRA bearbeitet dieselben Dateien per Chat.</span></div>
          </form>
        </details>""")

    body = f"""
    <div class="hero"><h1>Wissen</h1>
      <p>Live-editierbare Brain-Dateien — über dich und über jede Person. ASTRA liest und
         pflegt genau diese Dateien (auch via Telegram).</p>
      <details class="panel" style="margin-top:12px">
        <summary style="cursor:pointer;font-weight:600">So baust du Personen-Profile (Apple-Kontakte, Ton, Verläufe)</summary>
        <div style="margin-top:10px;color:var(--text-dim);font-size:13.5px;line-height:1.6">
          <p><b>Kontakte reindumpen:</b> Exportiere deine Apple-Kontakte (Kontakte-App →
          Kontakte markieren → <i>Teilen</i> / <i>Exportieren</i> als vCard) und schick den
          Text einfach ASTRA im Chat: „<i>Leg aus diesen Kontakten Profile an.</i>“ ASTRA legt
          pro Person eine Datei an — mit Telefonnummer (damit direkt für WhatsApp/Signal
          nutzbar), Beziehung und Trust-Tier.</p>
          <p><b>Umgangston pro Person:</b> In jeder Profildatei gibt es ein Feld
          „<b>Ton:</b>“. Trag ein, wie ASTRA mit der Person reden soll
          („locker, viel Insider-Humor“ / „formell, knapp“). Beim Antworten zieht ASTRA genau
          diesen Ton heran — sonst den <a href="/admin/secretary">Standard-Umgangston</a>.</p>
          <p><b>Aus Verläufen lernen:</b> Schreib ASTRA „<i>Ergänze das Profil von [Name]</i>“
          und kopier einen echten Nachrichtenverlauf rein. ASTRA erkennt euren Umgangston und
          schreibt ihn samt Beispielen ins Profil — die Dateien werden so mit der Zeit immer
          treffsicherer.</p>
        </div>
      </details>
    </div>
    {flash}
    <div class="toolbar">
      <div class="searchwrap"><svg width="17" height="17" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>
        <path d="m21 21-4.3-4.3"/></svg>
        <input class="search" id="bq" type="text" placeholder="Wissen durchsuchen…"></div>
      <form method="post" action="/admin/brain/add-person" class="row" style="gap:8px">
        <input type="hidden" name="csrf" value="{esc(token)}">
        <input type="text" name="name" placeholder="Person hinzufügen…" style="width:200px">
        <button class="btn secondary sm" type="submit">+ Person</button>
      </form>
    </div>
    <div class="chips" id="btags">{chips}</div>
    <div id="brainlist" style="display:flex;flex-direction:column;gap:12px">{''.join(cards)}</div>
    <div class="empty" id="bempty" style="display:none">Nichts gefunden.</div>
    <script>
      const bq=document.getElementById('bq'); let btag='all';
      function bapply(){{
        const t=bq.value.toLowerCase(); let n=0;
        document.querySelectorAll('.brain').forEach(c=>{{
          const ok=(btag==='all'||c.dataset.tag===btag)&&c.dataset.name.includes(t);
          c.style.display=ok?'':'none'; if(ok)n++;
        }});
        document.getElementById('bempty').style.display=n?'none':'';
      }}
      bq.oninput=bapply;
      document.querySelectorAll('#btags .chip').forEach(ch=>ch.onclick=()=>{{
        document.querySelectorAll('#btags .chip').forEach(x=>x.classList.remove('active'));
        ch.classList.add('active'); btag=ch.dataset.tag; bapply();
      }});
    </script>"""
    return _html_with_csrf(page("Wissen", body, active="brain"), token)


@router.post("/admin/brain/save")
async def brain_save(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/brain", status_code=303)
    rel = form.get("file", "")
    if knowledge.write_file(rel, form.get("content", "")):
        await db.audit("brain_write", actor="owner", detail={"file": rel})
    return RedirectResponse("/admin/brain?saved=1", status_code=303)


@router.post("/admin/brain/add-person")
async def brain_add_person(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return RedirectResponse("/admin/brain", status_code=303)
    rel = knowledge.create_person(form.get("name", ""))
    if rel:
        await db.audit("brain_add_person", actor="owner", detail={"file": rel})
    return RedirectResponse("/admin/brain?saved=1", status_code=303)


# ─── OSINT / Recon tab ─────────────────────────────────────────────────────────
# A compact frontend for passive Shodan metadata and Tor-routed research. Active
# audit tools still exist for explicitly authorized owner chat, but do not clutter
# the day-to-day Recon surface.
_OSINT_CARDS = [
    ("osint_nearby_exposure", "Kameras in der Nähe",
     "Passive Shodan-Metadaten. Kein Feed, kein Direktlink.", "", "", "cameras"),
    ("osint_nearby_exposure", "Drucker in der Nähe",
     "Indexierte Druckdienste im Radius, ohne Verbindung zum Gerät.",
     "", "", "printers"),
    ("osint_search", "OSINT-Recherche",
     "Offene Quellen über deine SearXNG-Instanz und Tor durchsuchen.",
     "query", "Wonach soll ASTRA recherchieren?", ""),
    ("osint_exit_ip", "Tor-Verbindung",
     "Prüft Exit-IP und ob Recherche wirklich durch Tor läuft.", "", "", ""),
]


def _osint_card(
    tool: str,
    title: str,
    desc: str,
    field: str,
    placeholder: str,
    category: str,
) -> str:
    from ..web.templates import esc
    input_html = (f'<input name="value" placeholder="{esc(placeholder)}" '
                  f'style="flex:1;min-width:0">' if field else "")
    nearby = tool == "osint_nearby_exposure"
    latlon = ('<input type="hidden" name="lat" class="osint-lat">'
              '<input type="hidden" name="lon" class="osint-lon">'
              f'<input type="hidden" name="category" value="{esc(category)}">'
              '<label class="recon-radius"><span>Radius</span>'
              '<select name="radius"><option value="5">5 km</option>'
              '<option value="15" selected>15 km</option><option value="30">30 km</option>'
              '<option value="50">50 km</option></select></label>'
              if nearby else "")
    action = "Suchen" if tool != "osint_exit_ip" else "Verbindung prüfen"
    return f"""
<div class="card osint-card" data-tool="{tool}">
  <div class="recon-card-head"><span class="recon-mark" aria-hidden="true"></span>
    <div><h2>{esc(title)}</h2><p>{esc(desc)}</p></div></div>
  <form class="osint-form" data-tool="{tool}">
    {input_html}{latlon}
    <button class="btn sm" type="submit">{action}</button>
  </form>
  <div class="osint-out" hidden aria-live="polite"></div>
</div>"""


@router.get("/admin/osint", response_class=HTMLResponse)
async def osint_page(request: Request, _: bool = Depends(auth.require_admin)):
    from ..plugins.registry import get_manager
    plugin = get_manager().get("osint")
    token = await auth.issue_csrf()
    enabled = bool(plugin and plugin.enabled)
    banner = ("" if enabled else
              '<div class="card recon-banner">Das OSINT-Plugin ist noch nicht '
              'aktiv. Richte es unter <a href="/admin/plugin/osint">Plugins → OSINT</a> ein '
              '(Tor-Proxy, Shodan-Key und optional SearXNG).</div>')
    appset = await _app_settings()
    saved_location = appset.get("location") if isinstance(appset.get("location"), dict) else {}
    initial_lat = saved_location.get("lat")
    initial_lon = saved_location.get("lon")
    initial_city = str(saved_location.get("city") or "")
    cards = "".join(_osint_card(*c) for c in _OSINT_CARDS)
    body = f"""
<section class="hero recon-hero"><div><div class="lab-eyebrow">PASSIVE RECON</div>
<h1>Recon</h1>
<p>Shodan-Metadaten in deiner Nähe und OSINT-Recherche über Tor. ASTRA verbindet
sich niemals mit einem gefundenen Gerät.</p></div>
<div class="recon-policy"><b>TOR KILL-SWITCH</b><span>Kein verifizierter Tor-Ausgang,
keine externe Anfrage.</span></div>
</section>
<div class="recon-location"><span class="recon-location-dot"></span>
  <span id="osint-loc">Standort: {esc(initial_city) if initial_city else "nicht gesetzt"}</span>
  <button class="btn ghost sm" type="button" id="osint-locate">Aktuellen Standort nutzen</button>
</div>
<div class="recon-privacy">Fail-closed: Shodan, DNS und Recherche laufen serverseitig über
den verifizierten Tor-Proxy. Es gibt weder direkte Fallbacks noch externe Browser-Links.
Der Standort wird vor Shodan auf ungefähr einen Kilometer vergröbert.</div>
{banner}
<div class="grid recon-grid">
{cards}
</div>
<script>
const locBadge = document.getElementById('osint-loc');
let here = {{lat:{json.dumps(initial_lat)}, lon:{json.dumps(initial_lon)}}};
const osintCsrf = {json.dumps(token)};
function syncLocation() {{
  document.querySelectorAll('.osint-lat').forEach(i => i.value = here.lat ?? '');
  document.querySelectorAll('.osint-lon').forEach(i => i.value = here.lon ?? '');
}}
syncLocation();
document.getElementById('osint-locate').onclick = (e) => {{
  e.preventDefault();
  navigator.geolocation.getCurrentPosition(p => {{
    here = {{lat:p.coords.latitude, lon:p.coords.longitude}};
    locBadge.textContent = 'Standort: ' + here.lat.toFixed(4) + ', ' + here.lon.toFixed(4);
    syncLocation();
    const fd = new FormData(); fd.append('csrf', osintCsrf);
    fd.append('lat', String(here.lat)); fd.append('lon', String(here.lon));
    fetch('/admin/osint/location', {{method:'POST', body:fd}}).then(r => {{
      if (!r.ok) throw new Error('Standort konnte nicht gespeichert werden');
    }}).catch(() => {{ locBadge.textContent += ' · nicht gespeichert'; }});
  }}, () => locBadge.textContent = 'Standort nicht freigegeben · gespeicherter Standort bleibt aktiv');
}};
function appendText(parent, tag, text, cls='') {{
  const el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls;
  parent.appendChild(el); return el;
}}
function renderResult(out, payload) {{
  out.replaceChildren(); out.hidden = false;
  const data = payload.data || {{}};
  const rows = Array.isArray(data.results) ? data.results : [];
  if (!rows.length) {{
    appendText(out, 'p', payload.summary || payload.error || 'Keine Treffer.', 'recon-summary');
    return;
  }}
  appendText(out, 'div', rows.length + ' Treffer · ausschließlich Metadaten', 'recon-result-count');
  rows.forEach(row => {{
    const item = document.createElement('div');
    item.className = 'recon-result';
    const title = row.product || row.title || row.ip || 'Shodan-Treffer';
    appendText(item, 'b', title);
    const where = [row.city, row.distance_km != null ? row.distance_km + ' km' : '',
      row.org].filter(Boolean).join(' · ');
    appendText(item, 'span', where || row.content || row.source || 'Öffentliche Quelle');
    if (row.port) appendText(item, 'small', (row.transport || 'tcp') + '/' + row.port);
    out.appendChild(item);
  }});
}}
document.querySelectorAll('.osint-form').forEach(f => {{
  f.addEventListener('submit', async (e) => {{
    e.preventDefault();
    const submit = f.querySelector('button[type=submit]');
    const out = f.parentElement.querySelector('.osint-out');
    const limit = 25000; const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), limit);
    const started = Date.now();
    const ticker = setInterval(() => {{
      out.textContent = 'Abfrage läuft über Tor… ' + Math.floor((Date.now()-started)/1000) + ' s';
    }}, 1000);
    if (submit) submit.disabled = true;
    out.hidden = false; out.classList.add('loading');
    out.textContent = 'Abfrage läuft über Tor… 0 s (max. 25 s)';
    const fd = new FormData(f); fd.append('tool', f.dataset.tool);
    try {{
      const r = await fetch('/admin/osint/run', {{method:'POST', body:fd, signal:controller.signal}});
      const j = await r.json();
      renderResult(out, j);
    }} catch (err) {{
      out.textContent = err.name === 'AbortError'
        ? 'Timeout nach 25 s. Tor-Sidecar und Plugin-Konfiguration prüfen.'
        : 'Fehler: ' + err;
    }} finally {{
      clearTimeout(timer); clearInterval(ticker); out.classList.remove('loading');
      if (submit) submit.disabled = false;
    }}
  }});
}});
</script>"""
    response = _html_with_csrf(
        page("Recon", body, active="osint", external_assets=False), token)
    # The Recon document itself may contact only ASTRA. This also prevents a
    # future theme/icon change from silently adding a browser-side egress.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "font-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(self)"
    return response


@router.post("/admin/osint/location")
async def osint_location(request: Request, _: bool = Depends(auth.require_admin)):
    """Persist a browser-approved location for owner chat/Telegram fallbacks."""
    form = await request.form()
    if not await _check_csrf(request, form):
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    try:
        lat, lon = float(form.get("lat")), float(form.get("lon"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Ungültige Koordinaten."}, status_code=400)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return JSONResponse({"ok": False, "error": "Ungültige Koordinaten."}, status_code=400)
    appset = await _app_settings()
    location = appset.get("location") if isinstance(appset.get("location"), dict) else {}
    location.update({"lat": round(lat, 6), "lon": round(lon, 6), "source": "browser"})
    appset["location"] = location
    await db.set_setting("app_settings", appset)
    await db.audit("osint_location_saved", actor="owner", detail={"source": "browser"})
    return JSONResponse({"ok": True, "lat": lat, "lon": lon})


@router.post("/admin/osint/run")
async def osint_run(request: Request, _: bool = Depends(auth.require_admin)):
    from ..tools import ToolContext, dispatch, REGISTRY, result_summary
    form = await request.form()
    tool = str(form.get("tool") or "")
    if not tool.startswith("osint_") or tool not in REGISTRY:
        return JSONResponse({"error": "Unbekanntes Tool."}, status_code=400)
    # Map the single visible field back to the tool's real argument name.
    field_map = {"osint_search": "query"}
    args: dict = {}
    value = str(form.get("value") or "").strip()
    if tool in field_map and value:
        args[field_map[tool]] = value
    if tool == "osint_nearby_exposure":
        args["category"] = str(form.get("category") or "")
        try:
            args["radius"] = int(form.get("radius") or 15)
        except (TypeError, ValueError):
            args["radius"] = 15
        for k in ("lat", "lon"):
            if form.get(k):
                try:
                    args[k] = float(form.get(k))
                except (TypeError, ValueError):
                    pass
    try:
        raw = await asyncio.wait_for(dispatch(tool, args, ToolContext(
            thread_id="web-osint", channel="web", contact={"id": "owner"}, is_owner=True)),
            timeout=25.0)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False,
                             "summary": "Recon-Timeout nach 25 s. Tor-Sidecar und Plugin-Konfiguration prüfen.",
                             "data": None, "warnings": []}, status_code=504)
    ok, summary, payload = result_summary(raw)
    data = payload.get("data") if isinstance(payload, dict) else None
    warnings = payload.get("warnings") if isinstance(payload, dict) else []
    return JSONResponse({"ok": ok, "summary": summary, "data": data, "warnings": warnings})
