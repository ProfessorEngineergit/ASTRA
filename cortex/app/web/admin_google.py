"""Admin → Google: zentrale Verwaltung aller Google-Konten (Client, Konten, Produkte, Zuordnung zu Plugins)."""
from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db, google_hub, google_oauth
from ..config_store import get_config_store
from ..plugins.registry import get_manager
from . import auth
from .admin import _check_csrf, _config_values_from_cfg, _html_with_csrf, _now_iso
from .admin_extra import _flash, _select, _shell
from .templates import esc

log = logging.getLogger("astra.web.google")
router = APIRouter()

GOOGLE_PLUGINS = ("google_calendar", "google_tasks", "gmail")
_GCP_CLIENTS = "https://console.cloud.google.com/auth/clients"
_GCP_AUDIENCE = "https://console.cloud.google.com/auth/audience"


def request_base(request: Request) -> str:
    """Adresse, unter der der Admin gerade offen ist — auch hinter Proxy/Tunnel (X-Forwarded-*).

    Nur für Vorschläge und den Rücksprung nach der Anmeldung; keine Sicherheitsentscheidung hängt daran
    (die Weiterleitungsadresse muss ohnehin bei Google eingetragen sein)."""
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    scheme = fwd_proto if fwd_proto in ("http", "https") else request.url.scheme
    host = fwd_host or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/"


def _redirect_info(request: Request, configured: str, *, force_manual: bool = False) -> dict:
    return google_hub.pick_redirect(request_base(request), configured, force_manual=force_manual,
                                    known_domain=google_hub.known_domain())


def _mode_of(configured: str) -> str:
    c = (configured or "").strip().lower()
    return "manual" if c == google_hub.REDIRECT_MANUAL else ("domain" if c else "auto")


def _abs(base: str, path: str) -> str:
    """Pfad an die Adresse hängen, unter der die Anmeldung gestartet wurde ('' = relativ)."""
    return (base.rstrip("/") + path) if base else path


async def _plugin_rows() -> list[dict]:
    """Alle Installationen der Google-Plugins mit aktuell gewähltem Konto."""
    rows = []
    mgr = get_manager()
    for slug in GOOGLE_PLUGINS:
        cls = mgr.plugin_class(slug)
        if not cls:
            continue
        for p in mgr.installations(slug):
            kind, acct = google_oauth.route(p.cfg)
            rows.append({"slug": slug, "name": cls.name, "icon": getattr(cls, "icon", ""),
                         "install_id": p.installation_id, "install_name": p.installation_name,
                         "choice": str(p.cfg.get("google_account") or ""), "kind": kind, "acct": acct,
                         "needs": google_hub.PLUGIN_PRODUCTS.get(slug, ()),
                         "legacy_ready": google_oauth._legacy_ready(p.cfg),
                         "legacy_email": str(p.cfg.get("account_email") or "")})
    return rows


def _chips(products: list[str]) -> str:
    return "".join(
        f'<span class="x-pill" style="{"border-color:rgba(54,211,153,.45);color:#a7f3d0" if k in products else "opacity:.5"}">'
        f'{"✔ " if k in products else ""}{esc(p.label)}</span> '
        for k, p in google_hub.PRODUCTS.items())


def _product_boxes(checked: set[str]) -> str:
    return "".join(
        f'<label style="display:inline-flex;align-items:center;gap:6px;margin:0 16px 8px 0;font-weight:500">'
        f'<input type="checkbox" name="products" value="{k}"{" checked" if k in checked else ""}> {esc(p.label)}</label>'
        for k, p in google_hub.PRODUCTS.items())


def _account_card(a: dict, token: str) -> str:
    st = {"ok": ("", "verbunden"), "reauth": ("x-warn", "neu verbinden")}.get(a["status"], ("", a["status"]))
    default = ('<span class="x-pill" style="border-color:rgba(54,211,153,.45);color:#a7f3d0">Standard</span>'
               if a["default"] else "")
    make_default = ("" if a["default"] else
                    f'<form method="post" action="/admin/google/default" style="display:inline">'
                    f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="account" value="{esc(a["id"])}">'
                    f'<button class="btn ghost sm" type="submit">Als Standard</button></form> ')
    note = f'<p class="x-warn" style="margin:8px 0 0">{esc(a["note"])}</p>' if a["note"] else ""
    return f"""
<div class="panel" data-account="{esc(a["id"])}">
  <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
    <div><h3 style="margin:0 0 4px;font-size:16px">{esc(a["email"])} {default}</h3>
      <div class="x-muted">{esc(a["name"])} · <span class="{st[0]}">{esc(st[1])}</span></div></div>
    <div>{make_default}
      <form method="post" action="/admin/google/disconnect" style="display:inline"
        onsubmit="return confirm('Dieses Konto trennen und bei Google widerrufen?')">
        <input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="account" value="{esc(a["id"])}">
        <button class="btn ghost danger sm" type="submit">Trennen</button></form></div>
  </div>
  <div style="margin:12px 0">{_chips(a["products"])}</div>
  {note}
  <div class="x-form" style="margin-top:8px">
    <button class="btn secondary sm g-test" type="button" data-account="{esc(a["id"])}" data-products="{esc(",".join(a["products"]))}">Alles testen</button>
    <details><summary class="btn ghost sm" style="display:inline-flex">Produkte ändern / erneut anmelden</summary>
      <form method="post" action="/admin/google/connect" style="margin-top:10px">
        <input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="login_hint" value="{esc(a["email"])}">
        <div>{_product_boxes(set(a["products"]))}</div>
        <button class="btn sm" type="submit">Bei Google zustimmen</button></form></details>
  </div>
  <div class="g-result x-muted" style="margin-top:10px"></div>
</div>"""


def _assign_table(rows: list[dict], accounts: list[dict], token: str) -> str:
    if not rows:
        return '<p class="x-muted">Keine Google-Plugins geladen.</p>'
    opts = {"": "Standardkonto", **{a["id"]: a["email"] for a in accounts}, "legacy": "Eigene Zugangsdaten des Plugins"}
    trs = []
    for r in rows:
        cur = r["choice"] if (r["choice"] in opts) else ("" if not r["choice"] else r["choice"])
        if cur and cur not in opts:
            opts = {**opts, cur: f"{cur} (nicht mehr vorhanden)"}
        warn = ""
        acct = google_hub.resolve(r["acct"]) if r["kind"] == "hub" else None
        if r["kind"] == "hub":
            if not acct:
                warn = '<span class="x-warn">kein Konto</span>'
            else:
                missing = [google_hub.PRODUCTS[k].label for k in r["needs"]
                           if k not in google_hub.products_from_scopes(acct.get("scopes"))]
                warn = (f'<span class="x-warn">fehlt: {esc(", ".join(missing))}</span>' if missing
                        else f'<span class="x-muted">nutzt {esc(acct["email"])}</span>')
        else:
            warn = '<span class="x-muted">eigene Zugangsdaten</span>'
        imp = ""
        if r["legacy_ready"] and r["kind"] == "legacy":
            imp = (f'<button class="btn ghost sm" type="submit" formaction="/admin/google/import" name="target" '
                   f'value="{esc(r["slug"])}|{esc(r["install_id"])}" title="Bestehende Verbindung in die zentralen Konten übernehmen">Übernehmen</button>')
        trs.append(
            f'<tr><td>{esc(r["icon"])} <b>{esc(r["name"])}</b><div class="x-muted">{esc(r["install_name"])}</div></td>'
            f'<td><form method="post" action="/admin/google/assign" style="display:flex;gap:8px;align-items:center;margin:0">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="slug" value="{esc(r["slug"])}">'
            f'<input type="hidden" name="install_id" value="{esc(r["install_id"])}">'
            f'{_select("account", opts, cur)}<button class="btn sm" type="submit">Setzen</button>{imp}</form></td>'
            f'<td>{warn}</td></tr>')
    return ('<div class="panel"><table class="x-tbl"><thead><tr><th>Plugin</th><th>Verwendetes Konto</th><th>Status</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></div>')


_JS = """
<script>
document.addEventListener('click', async e => {
  const t = e.target.closest('.g-test'); if (t) {
    const card = t.closest('[data-account]'), out = card.querySelector('.g-result');
    const csrf = document.querySelector('input[name=csrf]').value;
    out.textContent = 'Teste …'; t.disabled = true;
    const lines = [];
    for (const p of (t.dataset.products || '').split(',').filter(Boolean)) {
      const fd = new FormData(); fd.append('csrf', csrf); fd.append('account', t.dataset.account); fd.append('product', p);
      try {
        const r = await fetch('/admin/google/test', {method:'POST', body: fd}); const j = await r.json();
        const row = document.createElement('div');
        row.textContent = (j.ok ? '✅ ' : '❌ ') + j.message;
        if (j.action_url) { const a = document.createElement('a'); a.href = j.action_url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = ' → API aktivieren'; row.appendChild(a); }
        lines.push(row);
      } catch (err) { const row = document.createElement('div'); row.textContent = '❌ ' + err; lines.push(row); }
    }
    if (!lines.length) { const row = document.createElement('div'); row.textContent = 'Keine Produkte freigegeben.'; lines.push(row); }
    out.replaceChildren(...lines); t.disabled = false;
  }
  const c = e.target.closest('.g-copy'); if (c) {
    const inp = document.getElementById(c.dataset.target); inp.select();
    try { await navigator.clipboard.writeText(inp.value); } catch (_) { document.execCommand('copy'); }
    c.textContent = 'Kopiert ✓'; setTimeout(() => c.textContent = 'Kopieren', 1500);
  }
});
</script>"""


def _render(request: Request, summ: dict, rows: list[dict], token: str, flash: str, err: str) -> str:
    red = _redirect_info(request, summ["redirect_uri"])
    manual = red["mode"] == "manual"
    has_client = bool(summ["client_id"] and summ["has_secret"])
    configured = summ["redirect_uri"]
    mode = _mode_of(configured)
    domain_value = configured if mode == "domain" else ""
    ui_mode = "domain" if (mode == "auto" and red["mode"] == "manual") else mode      # sonst bliebe nur localhost sichtbar
    other_origin = mode == "domain" and google_hub.origin_of(red["uri"]) != google_hub.origin_of(request_base(request))
    localhost_uri = google_hub.pick_redirect(request_base(request), "", force_manual=True)["uri"]
    uris = [red["uri"]] + ([localhost_uri] if localhost_uri != red["uri"] else [])
    labels = ["Diese Adresse sendet ASTRA beim Anmelden", "Ausweichweg (optional, nur für „Über localhost anmelden“)"]
    uri_rows = "".join(
        f'<div class="help" style="margin:8px 0 3px">{esc(labels[min(i, 1)])}</div>'
        f'<div class="x-form" style="align-items:center;margin-bottom:6px"><input type="text" id="g-redirect-{i}" readonly value="{esc(u)}" '
        f'style="min-width:380px" onclick="this.select()"><button class="btn secondary sm g-copy" type="button" '
        f'data-target="g-redirect-{i}">Kopieren</button></div>' for i, u in enumerate(uris))
    known = summ.get("known_domain", "")
    suggest = ""
    if known and mode != "domain":
        suggest = (f'<div class="flash ok" style="margin:10px 0">Erkannte Domain: <b>{esc(known)}</b> '
                   f'<button class="btn sm" type="button" id="g-use-known" data-domain="{esc(known)}" style="margin-left:8px">Diese Domain verwenden</button></div>')
    steps = f"""
<div class="panel">
  <div class="section-head"><h2>1 · Google-Client (einmalig)</h2></div>
  <p class="x-muted">Ein OAuth-Client für alle Google-Produkte: <a href="{_GCP_CLIENTS}" target="_blank" rel="noopener">Google Cloud Console → Clients</a>
  → „Client erstellen“ → Typ <b>Webanwendung</b>. Aktiviere dort die APIs, die du nutzen willst (Calendar, Tasks, Gmail).
  Steht die Zustimmung auf „Testing“, trage dein Konto als Testnutzer ein und stelle sie besser auf
  <a href="{_GCP_AUDIENCE}" target="_blank" rel="noopener">„In production“</a> — sonst laufen Tokens nach 7 Tagen ab.</p>
  <form method="post" action="/admin/google/client">
    <input type="hidden" name="csrf" value="{esc(token)}">
    <div class="x-form">
      <div class="field"><label>Client-ID</label><input type="text" name="client_id" value="{esc(summ["client_id"])}" style="min-width:320px"></div>
      <div class="field"><label>Client-Secret</label><input type="password" name="client_secret" placeholder="{"•••• gesetzt — leer lassen zum Behalten" if summ["has_secret"] else "noch nicht gesetzt"}" style="min-width:260px"></div>
    </div>
    <div class="section-head" style="margin-top:14px"><h2>Weiterleitung zurück zu ASTRA</h2></div>
    <p class="x-muted">Google leitet nach der Zustimmung auf eine Adresse zurück, die du bei Google eintragen musst — erlaubt sind nur
    <b>https mit echter Domain</b> oder <b>http://localhost</b> (keine LAN-IP wie 10.60.0.190).</p>
    <div class="x-form" style="align-items:flex-end">
      <div class="field"><label>Modus</label>
        <select name="redirect_mode" id="g-mode">
          <option value="auto"{" selected" if ui_mode == "auto" else ""}>Automatisch (nach Adresse, unter der du gerade bist)</option>
          <option value="domain"{" selected" if ui_mode == "domain" else ""}>Eigene Domain</option>
          <option value="manual"{" selected" if ui_mode == "manual" else ""}>Nur localhost (Adresse manuell einfügen)</option>
        </select></div>
      <div class="field" id="g-domain-field"><label>Deine Domain</label>
        <input type="text" name="redirect_domain" value="{esc(domain_value)}" placeholder="https://astra.example.com" style="min-width:320px"></div>
    </div>
    <div class="x-muted" style="margin:0 0 6px">{esc(red["reason"])}</div>{suggest}
    {"<div class='flash ok' style='margin-bottom:10px'>Du bist gerade über eine andere Adresse verbunden. Das ist okay: Google leitet über deine Domain zurück, ASTRA schließt die Anmeldung dort ab und schickt dich danach hierher zurück.</div>" if other_origin else ""}
    <div class="field"><label>Bei Google unter „Autorisierte Weiterleitungs-URIs“ eintragen (zeichengenau)</label>{uri_rows}
      <div class="help">Die erste Adresse ist Pflicht, sonst meldet Google „Fehler 400: redirect_uri_mismatch“. Die zweite ist nur der
      Ausweichweg. „localhost“ meint dabei <b>deinen Rechner</b>, nicht einen Google-Server.</div></div>
    <button class="btn sm" type="submit">Speichern</button>
  </form>
  <script>(function(){{const m=document.getElementById('g-mode'),f=document.getElementById('g-domain-field');
    const u=()=>f.style.display=m.value==='domain'?'':'none'; m.addEventListener('change',u); u();
    const k=document.getElementById('g-use-known'); if(k) k.addEventListener('click',()=>{{
      m.value='domain'; u(); f.querySelector('input').value=k.dataset.domain; f.querySelector('input').focus();}});}})();</script>
</div>"""
    cards_html = "".join(_account_card(a, token) for a in summ["accounts"]) or (
        '<p class="x-muted">Noch kein Konto verbunden.</p>')
    connect = f"""
<div class="panel">
  <div class="section-head"><h2>2 · Konto hinzufügen</h2></div>
  <form method="post" action="/admin/google/connect">
    <input type="hidden" name="csrf" value="{esc(token)}">
    <div>{_product_boxes({"calendar", "tasks", "gmail_read"})}</div>
    <p class="x-muted">Jedes Mal kannst du bei Google ein <b>anderes Konto</b> wählen (privat, Schule, …). Später lassen sich Produkte
    pro Konto erweitern.</p>
    <p class="x-muted">Beim Anmelden sendet ASTRA diese Rücksprung-Adresse: <b>{esc(red["uri"])}</b><br>
    Sie muss bei Google unter „Autorisierte Weiterleitungs-URIs“ stehen. Bei „Fehler 400: redirect_uri_mismatch“ zeigt Google unter
    „Fehlerdetails“ die gesendete Adresse — sie muss zeichengenau übereinstimmen.</p>
    {'<div class="flash err">Aktuell würde localhost gesendet. Das braucht das manuelle Einfügen der Adresse — einfacher: oben <b>Eigene Domain</b> eintragen und speichern.</div>' if manual else ""}
    <button class="btn" type="submit" {"" if has_client else "disabled"}>Mit Google anmelden</button>
    <button class="btn ghost" type="submit" name="redirect" value="manual" {"" if has_client else "disabled"}
      title="Google leitet auf localhost zurück; du fügst die Adresse danach hier ein">Über localhost anmelden (Adresse einfügen)</button>
    {"" if has_client else '<span class="x-muted"> Erst Client-ID und -Secret speichern.</span>'}
  </form>
</div>
<div class="panel" style="margin-top:14px">
  <div class="section-head"><h2>Anmeldung abschließen{" (nötig bei dir)" if manual else ""}</h2></div>
  <p class="x-muted">{"Nach der Zustimmung landet dein Browser auf einer Seite, die nicht lädt (localhost) — das ist beabsichtigt. Kopiere die <b>komplette Adresse aus der Adressleiste</b> und füge sie hier ein." if manual else "Normalerweise passiert das automatisch. Falls Google dich auf eine Fehlerseite schickt: Adresse aus der Adressleiste hier einfügen."}</p>
  <form method="post" action="/admin/google/paste" class="x-form">
    <input type="hidden" name="csrf" value="{esc(token)}">
    <div class="field" style="flex:1"><input type="text" name="pasted" placeholder="http://localhost:8088/admin/oauth/google/callback?state=…&amp;code=…" style="width:100%;min-width:300px"></div>
    <button class="btn sm" type="submit">Fertigstellen</button>
  </form>
</div>"""
    body = f"""
<section class="hero"><div class="lab-eyebrow">GOOGLE</div><h1>Google-Konten</h1>
<p>Ein Client, mehrere Konten, jedes Plugin wählt sein Konto. Kalender, Aufgaben und Gmail teilen sich die Anmeldung.</p></section>
{_flash("ok", flash)}{_flash("err", err)}
{steps}
<div class="section-head x-sec"><h2>Verbundene Konten</h2><span class="count">{len(summ["accounts"])}</span></div>
<div class="x-stack">{cards_html}</div>
<div class="x-sec"></div>{connect}
<div class="section-head x-sec"><h2>Welches Plugin nutzt welches Konto?</h2></div>
{_assign_table(rows, summ["accounts"], token)}
<style>.x-stack>*{{margin-bottom:14px}}</style>{_JS}"""
    return _shell("Google", body, "google")


_FLASH = {"client": "Client gespeichert.", "connected": "Konto verbunden.", "default": "Standardkonto gesetzt.",
          "disconnected": "Konto getrennt.", "assigned": "Zuordnung gespeichert.", "imported": "Bestehende Verbindung übernommen."}


@router.get("/admin/google", response_class=HTMLResponse)
async def google_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = "", err: str = ""):
    token = await auth.issue_csrf()
    await google_hub.load()
    await google_hub.remember_origin(request_base(request))          # über die Domain geöffnet → merken
    html = _render(request, google_hub.summary(), await _plugin_rows(), token, _FLASH.get(saved, ""), err[:400])
    return _html_with_csrf(html, token)


async def _csrf_form(request: Request):
    form = await request.form()
    return form, await _check_csrf(request, form)


def _bad_csrf():
    return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)


def _err(msg: str, base: str = "") -> RedirectResponse:
    return RedirectResponse(_abs(base, f"/admin/google?err={quote(str(msg)[:400])}"), status_code=303)


@router.post("/admin/google/client")
async def google_client_save(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    cid = str(form.get("client_id") or "").strip()
    if not cid:
        return _err("Client-ID fehlt.")
    mode = str(form.get("redirect_mode") or "auto")
    redirect = ""
    if mode == "manual":
        redirect = google_hub.REDIRECT_MANUAL
    elif mode == "domain":
        redirect, problem = google_hub.normalize_redirect(str(form.get("redirect_domain") or ""))
        if problem or not redirect:
            return _err(problem or "Bitte gib deine Domain an (z. B. https://astra.example.com).")
    await google_hub.set_client(cid, str(form.get("client_secret") or ""), redirect)
    await db.audit("google_client_saved", actor="owner", detail={"redirect_mode": mode})
    return RedirectResponse("/admin/google?saved=client", status_code=303)


@router.post("/admin/google/connect")
async def google_connect(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    await google_hub.load()
    if not google_hub.has_client():
        return _err("Speichere zuerst Client-ID und Client-Secret.")
    products = [p for p in form.getlist("products") if p in google_hub.PRODUCTS] or ["calendar", "tasks", "gmail_read"]
    hint = str(form.get("login_hint") or "").strip()
    existing = []
    if hint:
        acct = google_hub.resolve(google_hub.account_id(hint))
        existing = list(acct.get("scopes", [])) if acct else []
    await google_hub.remember_origin(request_base(request))
    red = _redirect_info(request, google_hub.summary()["redirect_uri"],
                         force_manual=str(form.get("redirect") or "") == "manual")
    state = await auth.issue_oauth_state({"provider": "google_hub", "redirect_uri": red["uri"], "mode": red["mode"],
                                          "products": products, "ts": _now_iso(),
                                          "return_to": request_base(request)})
    url = google_hub.build_auth_url(client_id=google_hub.summary()["client_id"], redirect_uri=red["uri"],
                                    scopes=google_hub.scopes_for(products, existing), state=state, login_hint=hint)
    return RedirectResponse(url, status_code=303)


async def finish(code: str, state: str, error: str, request: Request | None = None) -> RedirectResponse:
    """Gemeinsamer Abschluss (direkter Callback UND eingefügte Adresse).

    Wurde die Anmeldung auf einer anderen Adresse gestartet als der, auf der Google zurückleitet (z. B. lokal
    gestartet, Rückleitung über die Domain), geht es danach dorthin zurück, wo du angefangen hast."""
    payload = await auth.read_oauth_state(state)
    base = ""
    if payload and payload.get("provider") == "google_hub":
        rt = str(payload.get("return_to") or "")
        here = google_hub.origin_of(request_base(request)) if request is not None else ""
        if rt.startswith(("http://", "https://")) and google_hub.origin_of(rt) != here:
            base = rt
    if error:
        return _err(google_hub.explain_error(403, {"error": error})["message"] if error == "access_denied"
                    else f"Google meldet: {error}", base)
    if not payload or payload.get("provider") != "google_hub":
        return _err("Die Anmeldung ist abgelaufen oder gehört nicht zu dieser Sitzung. Starte sie erneut.")
    if not code:
        return _err("In der Adresse fehlt der Code.", base)
    try:
        acct = await google_hub.complete_login(code, str(payload.get("redirect_uri") or ""))
    except google_hub.GoogleApiError as e:
        return _err(str(e), base)
    except Exception as e:  # noqa: BLE001
        log.exception("Google login failed")
        return _err(f"Anmeldung fehlgeschlagen: {e}", base)
    await db.audit("google_account_connected", actor="owner", detail={"account": acct["id"]})
    try:
        await get_manager().rebuild()
    except Exception:  # noqa: BLE001
        log.debug("plugin rebuild after Google login failed", exc_info=True)
    return RedirectResponse(_abs(base, "/admin/google?saved=connected"), status_code=303)


@router.post("/admin/google/paste")
async def google_paste(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    parsed = google_hub.parse_pasted(str(form.get("pasted") or ""))
    if parsed["error"] == "Nichts eingefügt.":
        return _err(parsed["error"])
    if parsed["error"]:                                        # Google hat die Anmeldung abgelehnt (z. B. access_denied)
        return await finish("", parsed["state"], parsed["error"], request)
    if not parsed["state"]:
        return _err("Füge die komplette Adresse ein (mit „state=“), nicht nur den Code.")
    return await finish(parsed["code"], parsed["state"], "", request)


@router.post("/admin/google/default")
async def google_default(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    if await google_hub.set_default(str(form.get("account") or "")):
        await db.audit("google_default_set", actor="owner", detail={"account": str(form.get("account"))})
        return RedirectResponse("/admin/google?saved=default", status_code=303)
    return _err("Konto nicht gefunden.")


@router.post("/admin/google/disconnect")
async def google_disconnect(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    acct = str(form.get("account") or "")
    if await google_hub.remove_account(acct):
        await db.audit("google_account_removed", actor="owner", detail={"account": acct})
    return RedirectResponse("/admin/google?saved=disconnected", status_code=303)


@router.post("/admin/google/test")
async def google_test(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    await google_hub.load()
    return JSONResponse(await google_hub.probe(str(form.get("account") or ""), str(form.get("product") or "")))


async def _set_plugin_account(slug: str, install_id: str, account: str) -> bool:
    mgr, store = get_manager(), get_config_store()
    cls = mgr.plugin_class(slug)
    if not cls or slug not in GOOGLE_PLUGINS:
        return False
    cfg = next((i for i in await store.load_installations(cls) if i.get("__installation_id") == install_id), None)
    if cfg is None:
        return False
    values = _config_values_from_cfg(cls, cfg)
    values["google_account"] = account
    await store.save_installation(cls, install_id, values, bool(cfg.get("__instance_enabled", cfg.get("__enabled"))),
                                  name=str(cfg.get("__installation_name") or "Standard"))
    await mgr.rebuild()
    return True


@router.post("/admin/google/assign")
async def google_assign(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    await google_hub.load()
    account = str(form.get("account") or "")
    known = {a["id"] for a in google_hub.summary()["accounts"]}
    if account not in ("", google_oauth.ACCOUNT_LEGACY) and account not in known:
        return _err("Unbekanntes Konto.")
    if not await _set_plugin_account(str(form.get("slug") or ""), str(form.get("install_id") or "default"), account):
        return _err("Plugin nicht gefunden.")
    await db.audit("google_account_assigned", actor="owner", detail={"plugin": str(form.get("slug")), "account": account})
    return RedirectResponse("/admin/google?saved=assigned", status_code=303)


@router.post("/admin/google/import")
async def google_import(request: Request, _: bool = Depends(auth.require_admin)):
    """Bestehende Plugin-eigene Verbindung in die zentralen Konten übernehmen (kein erneutes Zustimmen)."""
    form, ok = await _csrf_form(request)
    if not ok:
        return _bad_csrf()
    slug, _, install_id = str(form.get("target") or "").partition("|")
    mgr, store = get_manager(), get_config_store()
    cls = mgr.plugin_class(slug)
    if not cls or slug not in GOOGLE_PLUGINS:
        return _err("Plugin nicht gefunden.")
    cfg = next((i for i in await store.load_installations(cls) if i.get("__installation_id") == (install_id or "default")), None)
    if not cfg or not google_oauth._legacy_ready(cfg):
        return _err("Keine vollständige bestehende Verbindung gefunden.")
    await google_hub.load()
    if not google_hub.has_client():
        await google_hub.set_client(str(cfg["client_id"]), str(cfg["client_secret"]))
    email = str(cfg.get("account_email") or "").strip().lower()
    if not email:
        try:
            tok = await google_hub._token_request({"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                                   "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token"})
            email = str((await google_hub.user_info(str(tok.get("access_token")))).get("email") or "").lower()
        except Exception as e:  # noqa: BLE001
            return _err(f"E-Mail des Kontos nicht ermittelbar: {e}")
    if not email:
        return _err("E-Mail des Kontos nicht ermittelbar.")
    acct = await google_hub.upsert_account(email=email, refresh_token=str(cfg["refresh_token"]),
                                           scopes=google_hub.scopes_for([], list(getattr(cls, "google_scopes", []))))
    await _set_plugin_account(slug, install_id or "default", acct["id"])
    await db.audit("google_account_imported", actor="owner", detail={"plugin": slug, "account": acct["id"]})
    return RedirectResponse("/admin/google?saved=imported", status_code=303)
