"""Web admin router: first-run setup, login, plugin catalog + config forms."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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
             data-name="{esc((p.name + ' ' + p.description).lower())}"
             data-cat="{p.category.value}" data-source="nativ" data-fav="{'1' if is_fav else '0'}">
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
             data-name="{esc((e.name + ' ' + e.description).lower())}"
             data-cat="{e.category.value}" data-source="katalog" data-fav="0">
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

    body = f"""
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
    </div>
    <div class="chips" id="chips">{chips}</div>
    <div id="sections">{''.join(sections)}</div>
    <div class="empty" id="empty" style="display:none">Keine Integrationen gefunden.</div>
    <script>
      const q=document.getElementById('q'), favonly=document.getElementById('favonly');
      let cat='all', src='all';
      function apply() {{
        const term=q.value.toLowerCase(); let anyVisible=false;
        document.querySelectorAll('.section').forEach(sec=>{{
          let shown=0;
          sec.querySelectorAll('.card').forEach(c=>{{
            const okQ=c.dataset.name.includes(term);
            const okC=cat==='all'||c.dataset.cat===cat;
            const okF=!favonly.checked||c.dataset.fav==='1';
            const okS=src==='all'||c.dataset.source===src;
            const ok=okQ&&okC&&okF&&okS;
            c.style.display=ok?'':'none'; if(ok) shown++;
          }});
          sec.style.display=shown?'':'none'; if(shown) anyVisible=true;
        }});
        document.getElementById('empty').style.display=anyVisible?'none':'';
      }}
      q.oninput=apply; favonly.onchange=apply;
      document.querySelectorAll('#chips .chip').forEach(ch=>ch.onclick=()=>{{
        document.querySelectorAll('#chips .chip').forEach(x=>x.classList.remove('active'));
        ch.classList.add('active'); cat=ch.dataset.cat; apply();
      }});
      document.querySelectorAll('#seg .seg-btn').forEach(b=>b.onclick=()=>{{
        document.querySelectorAll('#seg .seg-btn').forEach(x=>x.classList.remove('active'));
        b.classList.add('active'); src=b.dataset.src; apply();
      }});
      document.querySelectorAll('.star').forEach(s=>s.onclick=async()=>{{
        const r=await fetch('/admin/favorite/'+s.dataset.slug,{{method:'POST'}});
        const d=await r.json(); s.classList.toggle('on', d.favorite);
        s.closest('.card').dataset.fav=d.favorite?'1':'0'; apply();
      }});
    </script>"""
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
    token = await auth.issue_csrf()
    flash = '<div class="flash ok">Gespeichert.</div>' if saved else ""
    lat = loc.get("lat", 50.1109)
    lon = loc.get("lon", 8.6821)
    city = loc.get("city", "")
    tz_opts = "".join(
        f'<option {"selected" if s.get("timezone", "Europe/Berlin") == t else ""}>{t}</option>'
        for t in ["Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/London", "UTC"])
    units = s.get("units", "metric")
    lang = s.get("language", "de")
    model = esc(s.get("ai_model", ""))
    eco = "checked" if s.get("economy_mode") else ""
    cur_font = s.get("font", "inter")
    addr = esc(loc.get("address", ""))
    font_opts = "".join(
        f'<option value="{esc(k)}" {"selected" if k == cur_font else ""}>{esc(name)}</option>'
        for k, name in font_choices())
    body = f"""
    <div class="hero"><h1>Einstellungen</h1>
      <p>Allgemeine Angaben, KI-Modell & dein Standort — den nutzen Plugins für Wetter,
         nächste Haltestelle und „in der Nähe".</p></div>
    {flash}
    <form method="post" action="/admin/settings" id="settings-form">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="panel" style="margin-bottom:16px">
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
        <h2 style="margin:0 0 6px;font-size:15px">📍 Standort</h2>
        <p class="note" style="margin:0 0 12px">Adresse suchen, Pin ziehen oder Browser-Standort nutzen —
           die Stadt wird automatisch erkannt.</p>
        <div class="row" style="gap:10px;margin-bottom:12px">
          <input type="text" id="addr" value="{addr}" placeholder="Adresse suchen (Straße, Ort)…" style="flex:1">
          <button class="btn secondary" type="button" id="addrbtn">Suchen</button>
          <button class="btn secondary" type="button" id="geobtn">Mein Standort</button>
        </div>
        <input type="hidden" name="address" id="address" value="{addr}">
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

      <div class="panel" style="margin-bottom:16px">
        <h2 style="margin:0 0 6px;font-size:15px">Labs</h2>
        <p class="note" style="margin:0 0 12px">Experimentell. Eigene Schriften einfach in
           <code>cortex/app/web/static/fonts/</code> ablegen — sie erscheinen hier automatisch.</p>
        <div class="field" style="margin:0"><label>Schriftart der Oberfläche</label>
          <select name="font">{font_opts}</select></div>
      </div>

      <div class="panel" style="margin-bottom:16px">
        <div class="lab-card">{_GH_SVG}
          <div style="flex:1"><div style="font-weight:600;font-size:14px">Auf GitHub mitwirken</div>
            <div class="note" style="font-size:12px">Code, Ideen und Bug-Reports sind willkommen.</div></div>
          <a class="btn secondary sm" target="_blank" rel="noopener" href="{GH_REPO}">Repository ↗</a>
        </div>
      </div>

      <button class="btn" type="submit">Alles speichern</button>
    </form>

    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
      const map = L.map('map').setView([{lat}, {lon}], 11);
      L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
        attribution: '© OpenStreetMap, © CARTO', maxZoom: 19 }}).addTo(map);
      const marker = L.marker([{lat}, {lon}], {{ draggable: true }}).addTo(map);
      async function reverse(la, lo) {{
        document.getElementById('lat').value = la.toFixed(5);
        document.getElementById('lon').value = lo.toFixed(5);
        try {{
          const r = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${{la}}&lon=${{lo}}&zoom=14&accept-language=de`);
          const d = await r.json(); const a = d.address || {{}};
          const c = a.city || a.town || a.village || a.county || d.name || '';
          if (c) document.getElementById('city').value = c;
          if (d.display_name) document.getElementById('address').value = d.display_name;
        }} catch (e) {{}}
      }}
      function place(la, lo) {{ map.setView([la, lo], 14); marker.setLatLng([la, lo]); reverse(la, lo); }}
      map.on('click', e => {{ marker.setLatLng(e.latlng); reverse(e.latlng.lat, e.latlng.lng); }});
      marker.on('dragend', e => {{ const p = e.target.getLatLng(); reverse(p.lat, p.lng); }});
      document.getElementById('addrbtn').onclick = async () => {{
        const q = document.getElementById('addr').value.trim(); if (!q) return;
        document.getElementById('address').value = q;
        const r = await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&accept-language=de&q=${{encodeURIComponent(q)}}`);
        const d = await r.json(); if (d[0]) place(parseFloat(d[0].lat), parseFloat(d[0].lon));
      }};
      document.getElementById('geobtn').onclick = () => {{
        if (!navigator.geolocation) return alert('Geolocation nicht verfügbar.');
        navigator.geolocation.getCurrentPosition(
          p => place(p.coords.latitude, p.coords.longitude),
          () => alert('Standortzugriff abgelehnt.'), {{ enableHighAccuracy: true, timeout: 8000 }});
      }};
      setTimeout(() => map.invalidateSize(), 200);
    </script>"""
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
    s.update({
        "owner_name": form.get("owner_name", "").strip(),
        "timezone": form.get("timezone", "Europe/Berlin"),
        "units": form.get("units", "metric"),
        "language": form.get("language", "de"),
        "ai_model": form.get("ai_model", "").strip(),
        "economy_mode": "economy" in form,
        "font": form.get("font", "inter"),
        "location": {"lat": _f(form.get("lat"), 50.1109),
                     "lon": _f(form.get("lon"), 8.6821),
                     "city": form.get("city", "").strip(),
                     "address": form.get("address", "").strip()},
    })
    await db.set_setting("app_settings", s)
    set_model_override(s["ai_model"])     # live, no restart
    set_font(s["font"])
    await db.audit("settings_change", actor="owner",
                   detail={"city": s["location"]["city"], "model": s["ai_model"], "font": s["font"]})
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
      <a href="/admin/updates">Updates &amp; Versionen →</a></p>
    <script>setTimeout(() => location.reload(), 15000);</script>"""
    return HTMLResponse(page("System", body, active="system"))


# ─── Chat: talk to ASTRA from the web (full owner agent) ──────────────────────
async def _chat_history() -> list[dict]:
    return await db.get_setting("web_chat", []) or []


@router.get("/admin/chat", response_class=HTMLResponse)
async def chat_page(request: Request, _: bool = Depends(auth.require_admin)):
    hist = await _chat_history()
    bubbles = "".join(
        f'<div class="msg {"user" if m["role"] == "user" else "bot"}">{esc(m["content"])}</div>'
        for m in hist) or '<div class="msg sys">Sag Hallo zu ASTRA — alles wie in Telegram, nur hier.</div>'
    body = f"""
    <div class="chat-wrap">
      <div class="chat-log" id="log">{bubbles}</div>
      <div class="chat-input">
        <textarea id="inp" placeholder="Nachricht oder Aufgabe an ASTRA…" rows="1"></textarea>
        <button class="btn" id="send">Senden</button>
        <button class="btn ghost sm" id="clear" title="Verlauf leeren">Leeren</button>
      </div>
    </div>
    <script>
      const log=document.getElementById('log'), inp=document.getElementById('inp');
      const scroll=()=>log.scrollTop=log.scrollHeight; scroll();
      function add(cls,txt){{const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;log.appendChild(d);scroll();return d;}}
      inp.addEventListener('input',()=>{{inp.style.height='auto';inp.style.height=Math.min(inp.scrollHeight,160)+'px';}});
      inp.addEventListener('keydown',e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();go();}}}});
      document.getElementById('send').onclick=go;
      async function go(){{
        const t=inp.value.trim(); if(!t) return; inp.value=''; inp.style.height='auto';
        add('user',t); const typing=add('typing','ASTRA tippt…');
        try{{
          const r=await fetch('/admin/chat/send',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:t}})}});
          const d=await r.json(); typing.remove(); add('bot', d.reply||'(keine Antwort)');
        }}catch(e){{ typing.remove(); add('bot','Fehler: '+e); }}
      }}
      document.getElementById('clear').onclick=async()=>{{
        await fetch('/admin/chat/clear',{{method:'POST'}}); log.innerHTML='';
        add('sys','Verlauf geleert.');
      }};
    </script>"""
    return HTMLResponse(page("Chat", body, active="chat"))


@router.post("/admin/chat/send")
async def chat_send(request: Request, _: bool = Depends(auth.require_admin)):
    from ..agent import generate_reply
    from ..persona import Register
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return JSONResponse({"reply": "(leer)"})
    st = get_settings()
    hist = await _chat_history()
    hist.append({"role": "user", "content": msg})
    try:
        reply = await generate_reply(
            register=Register.OWNER,
            contact={"id": "owner", "name": st.astra_owner_name, "is_owner": True},
            thread_id="web-owner", channel="web", history=hist,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("web chat failed")
        return JSONResponse({"reply": f"Fehler: {e}"})
    hist.append({"role": "assistant", "content": reply})
    await db.set_setting("web_chat", hist[-40:])
    await db.audit("web_chat", actor="owner", detail={"len": len(msg)})
    return JSONResponse({"reply": reply})


@router.post("/admin/chat/clear")
async def chat_clear(request: Request, _: bool = Depends(auth.require_admin)):
    await db.set_setting("web_chat", [])
    return JSONResponse({"ok": True})


# ─── Updates: release notes + hyperspace + GitHub links ───────────────────────
@router.get("/admin/updates", response_class=HTMLResponse)
async def updates_page(request: Request, _: bool = Depends(auth.require_admin)):
    import httpx
    commits_html = '<p class="note">Konnte GitHub gerade nicht erreichen.</p>'
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.github.com/repos/ProfessorEngineergit/ASTRA/commits",
                            params={"per_page": 12}, headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            rows = []
            for cm in r.json():
                sha = cm.get("sha", "")[:7]
                msg = (cm.get("commit", {}).get("message", "") or "").split("\n")[0]
                url = cm.get("html_url", "#")
                rows.append(f'<div class="commit"><a class="h" href="{esc(url)}" target="_blank" '
                            f'rel="noopener">{esc(sha)} ↗</a><div class="m">{esc(msg)}</div></div>')
            commits_html = "".join(rows)
    except Exception:  # noqa: BLE001
        pass

    body = f"""
    <div id="hyper"><canvas id="hsc"></canvas></div>
    <div class="hero"><h1>Updates</h1>
      <p>Neueste Änderungen aus dem Repository. Prüfe auf Updates und sieh dir die Notes an.</p></div>
    <div class="row" style="margin-bottom:20px">
      <button class="btn" id="check">Auf Updates prüfen</button>
      <a class="btn secondary" target="_blank" rel="noopener"
         href="https://github.com/ProfessorEngineergit/ASTRA/commits/main">Alle Commits ↗</a>
      <a class="btn ghost" target="_blank" rel="noopener"
         href="https://github.com/ProfessorEngineergit/ASTRA/releases">Releases ↗</a>
    </div>
    <div class="panel" style="margin-bottom:18px">
      <h2 style="margin:0 0 6px;font-size:15px">So aktualisierst du</h2>
      <p class="note" style="margin:0 0 8px">Auf dem Server ausführen — Code holen &amp; neu bauen:</p>
      <pre style="background:var(--bg-2);border:1px solid var(--border);border-radius:var(--r-sm);
        padding:12px 14px;font-family:'JetBrains Mono',monospace;font-size:13px;overflow-x:auto">cd /opt/astra &amp;&amp; git pull origin main &amp;&amp; docker compose up -d --build cortex</pre>
    </div>
    <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-dim);margin:0 0 12px">Was ist neu</h2>
    <div id="notes">{commits_html}</div>
    <script>
      const hyper=document.getElementById('hyper'), cv=document.getElementById('hsc');
      function hyperspace(ms){{
        hyper.classList.add('on'); const ctx=cv.getContext('2d');
        cv.width=innerWidth; cv.height=innerHeight;
        const cx=cv.width/2, cy=cv.height/2, N=320, stars=[];
        for(let i=0;i<N;i++) stars.push({{x:(Math.random()-0.5)*cv.width,y:(Math.random()-0.5)*cv.height,z:Math.random()*cv.width}});
        let t0=performance.now(), raf;
        (function frame(t){{
          ctx.fillStyle='rgba(0,0,0,.35)'; ctx.fillRect(0,0,cv.width,cv.height);
          ctx.strokeStyle='#cfe0ff'; ctx.lineWidth=2;
          for(const s of stars){{
            s.z-=18; if(s.z<1){{s.z=cv.width;s.x=(Math.random()-0.5)*cv.width;s.y=(Math.random()-0.5)*cv.height;}}
            const k=128/s.z, x=cx+s.x*k, y=cy+s.y*k, k2=128/(s.z+18), px=cx+s.x*k2, py=cy+s.y*k2;
            ctx.beginPath(); ctx.moveTo(px,py); ctx.lineTo(x,y); ctx.stroke();
          }}
          if(t-t0<ms) raf=requestAnimationFrame(frame);
          else {{ cancelAnimationFrame(raf); hyper.classList.remove('on'); }}
        }})(t0);
      }}
      document.getElementById('check').onclick=()=>{{ hyperspace(1800);
        setTimeout(()=>document.getElementById('notes').scrollIntoView({{behavior:'smooth'}}),1400); }};
    </script>"""
    return HTMLResponse(page("Updates", body, active="updates"))
