"""Web admin router: first-run setup, login, plugin catalog + config forms."""
from __future__ import annotations

import json
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

      <div class="panel" style="margin-bottom:16px">
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

      <div class="panel labs-console" style="margin-bottom:16px">
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
