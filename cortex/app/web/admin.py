"""Web admin router: first-run setup, login, plugin catalog + config forms."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db
from ..config_store import SECRET_SENTINEL, get_config_store
from ..plugins.base import CATEGORY_LABELS, FieldType
from ..plugins.registry import get_manager
from . import auth
from .templates import astra_mark, esc, page

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
      <div class="auth-logo">{astra_mark(52)}<span class="word">ASTRA</span></div>
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
      <div class="auth-logo">{astra_mark(52)}<span class="word">ASTRA</span></div>
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
             data-cat="{p.category.value}" data-fav="{'1' if is_fav else '0'}">
          <div class="top">
            <span class="icon">{esc(p.icon)}</span>
            <div class="meta">
              <h3>{esc(p.name)}</h3>
              <div class="cat">{cat_label}</div>
            </div>
            <button class="star {'on' if is_fav else ''}" data-slug="{esc(p.slug)}"
                    title="Favorit">★</button>
          </div>
          <p>{esc(p.description)}</p>
          <div class="row">{badge}{action}</div>
        </div>"""


@router.get("/admin", response_class=HTMLResponse)
async def catalog(request: Request, _: bool = Depends(auth.require_admin)):
    mgr = get_manager()
    favs = set(await _favorites())
    plugins = mgr.all()
    n_active = sum(1 for p in plugins if p.enabled)
    n_ready = sum(1 for p in plugins if p.has_required and not p.enabled
                  and not getattr(p, "coming_soon", False))

    # Category chips (ordered by the canonical CATEGORY_LABELS order).
    cat_order = [c for c in CATEGORY_LABELS if any(p.category == c for p in plugins)]
    chips = '<span class="chip active" data-cat="all">Alle</span>' + "".join(
        f'<span class="chip" data-cat="{c.value}">{esc(CATEGORY_LABELS.get(c, c.value))}</span>'
        for c in cat_order
    )

    # Cards grouped into category sections.
    sections = []
    for c in cat_order:
        members = [p for p in plugins if p.category == c]
        cards = "".join(_card_html(p, p.slug in favs) for p in members)
        sections.append(f"""
        <div class="section" data-section="{c.value}">
          <div class="section-head">
            <h2>{esc(CATEGORY_LABELS.get(c, c.value))}</h2>
            <span class="count">{len(members)}</span>
          </div>
          <div class="grid">{cards}</div>
        </div>""")

    search_icon = ('<svg width="17" height="17" viewBox="0 0 24 24" fill="none" '
                   'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
                   '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>')

    body = f"""
    <div class="hero">
      <h1>Deine <span class="grad">Integrationen</span></h1>
      <p>Verbinde ASTRA mit deiner Welt — Verkehr, Smart Home, Server, Messenger und mehr.</p>
      <div class="stats">
        <div class="stat"><span class="dot" style="background:var(--ok)"></span><b>{n_active}</b> aktiv</div>
        <div class="stat"><span class="dot" style="background:var(--aurora-1)"></span><b>{n_ready}</b> startklar</div>
        <div class="stat"><span class="dot" style="background:var(--text-faint)"></span><b>{len(plugins)}</b> insgesamt</div>
      </div>
    </div>
    <div class="toolbar">
      <div class="searchwrap">{search_icon}
        <input class="search" id="q" type="text" placeholder="Plugins durchsuchen…"></div>
      <label class="switch"><input type="checkbox" id="favonly" class="toggle"
        style="width:40px;height:23px"> nur Favoriten</label>
    </div>
    <div class="chips" id="chips">{chips}</div>
    <div id="sections">{''.join(sections)}</div>
    <div class="empty" id="empty" style="display:none">Keine Plugins gefunden.</div>
    <script>
      const q=document.getElementById('q'), favonly=document.getElementById('favonly');
      let cat='all';
      function apply() {{
        const term=q.value.toLowerCase(); let anyVisible=false;
        document.querySelectorAll('.section').forEach(sec=>{{
          let shown=0;
          sec.querySelectorAll('.card').forEach(c=>{{
            const okQ=c.dataset.name.includes(term);
            const okC=cat==='all'||c.dataset.cat===cat;
            const okF=!favonly.checked||c.dataset.fav==='1';
            const ok=okQ&&okC&&okF;
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
