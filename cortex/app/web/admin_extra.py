"""Zusätzliche Admin-Seiten: Verbrauch, Kontakte/Gruppen, Prompt-Werkstatt, Sicherheit.

Bewusst ein eigenes Modul (admin.py ist mit 5000+ Zeilen schon groß). Gleiche Auth,
gleiche CSRF-Regeln, gleiches Seiten-Grundgerüst — nur eigene Routen.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db, model_choice, models, usage
from . import auth
from .admin import _app_settings, _check_csrf, _html_with_csrf
from .templates import esc, page

log = logging.getLogger("astra.web.extra")
router = APIRouter()

_X_CSS = """
<style>
.x-bar{height:7px;border-radius:99px;background:var(--surface-2);overflow:hidden;min-width:70px}
.x-bar>i{display:block;height:100%;background:var(--link);border-radius:99px}
.x-bar.warn>i{background:#f5c451}.x-bar.over>i{background:#fb7185}
.x-tbl{width:100%;border-collapse:collapse;font-size:13.5px}
.x-tbl th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-faint);
  text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);font-weight:600}
.x-tbl td{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
.x-tbl td.num,.x-tbl th.num{text-align:right;font-variant-numeric:tabular-nums}
.x-tbl tr:last-child td{border-bottom:0}
.x-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.x-kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px}
.x-kpi small{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-faint)}
.x-kpi b{font-size:24px;letter-spacing:-.6px}
.x-kpi span{display:block;font-size:12px;color:var(--text-faint);margin-top:2px}
.x-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
.x-sec{margin:22px 0 10px}
.x-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.x-form .field{margin:0}
.x-form input[type=text],.x-form input[type=number]{min-width:110px}
.x-muted{color:var(--text-faint);font-size:12.5px}
.x-warn{color:#f8dfa0}
.x-pill{display:inline-block;padding:2px 9px;border-radius:99px;border:1px solid var(--border);
  font-size:11.5px;color:var(--text-dim)}
.sec-toggle{display:inline-flex;align-items:center;gap:8px;background:none;border:0;padding:2px;cursor:pointer;
  color:var(--text-dim);font:600 12.5px inherit;font-family:inherit}
.sec-toggle .track{width:38px;height:22px;border-radius:99px;background:var(--surface-2);border:1px solid var(--border);
  position:relative;transition:background .15s,border-color .15s;flex:0 0 auto}
.sec-toggle .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;
  background:var(--text-faint);transition:transform .15s,background .15s}
.sec-toggle.on{color:#a7f3d0}.sec-toggle.on .track{background:rgba(54,211,153,.22);border-color:rgba(54,211,153,.45)}
.sec-toggle.on .track::after{transform:translateX(16px);background:#36d399}
.sec-toggle:focus-visible{outline:2px solid var(--link);border-radius:8px}
.sec-toggle.busy{opacity:.5;pointer-events:none}
@media(max-width:640px){.x-tbl{font-size:12.5px}.x-tbl td,.x-tbl th{padding:7px 6px}}
</style>
"""


def _shell(title: str, body: str, active: str) -> str:
    return page(title, _X_CSS + body, active=active)


def _flash(kind: str, text: str) -> str:
    return f'<div class="flash {kind}">{esc(text)}</div>' if text else ""


# ══ Verbrauch ═════════════════════════════════════════════════════════════════
_BY_ORDER = ("model", "purpose", "channel", "chat_id", "provider", "role", "day")


async def usage_data(period: str, by: str) -> dict:
    period = period if period in usage.PERIODS else "month"
    by = by if by in usage.GROUPS else "model"
    appset = await _app_settings()
    tz = str((appset.get("location") or {}).get("timezone") or "Europe/Berlin")
    rows = await db.usage_rows(usage.period_start(period, tz=tz))
    spent = await db.usage_month_cost(usage.month_start())
    return {
        "period": period, "by": by, "rows": rows, "totals": usage.totals(rows),
        "groups": usage.group_rows(rows, by), "budget": usage.budget_state(spent),
        "cfg": appset.get("usage") if isinstance(appset.get("usage"), dict) else {},
    }


def _chip_links(current: str, options: dict[str, str], param: str, other: dict) -> str:
    out = []
    for key, label in options.items():
        qs = "&".join(f"{k}={v}" for k, v in {**other, param: key}.items())
        out.append(f'<a class="chip{" active" if key == current else ""}" href="/admin/usage?{qs}">{esc(label)}</a>')
    return '<div class="chips">' + "".join(out) + "</div>"


def _render_usage(d: dict, flash: str, token: str) -> str:
    t, b = d["totals"], d["budget"]
    if b["limit"]:
        cls = "over" if b["level"] == "over" else "warn" if b["level"] == "warn" else ""
        budget_kpi = (f'<div class="x-kpi"><small>Monatsbudget</small><b>{b["pct"]:.0f} %</b>'
                      f'<div class="x-bar {cls}" style="margin-top:8px"><i style="width:{min(100, b["pct"]):.0f}%"></i></div>'
                      f'<span>{esc(usage.fmt_cost(b["spent"]))} von {esc(usage.fmt_cost(b["limit"]))}</span></div>')
    else:
        budget_kpi = ('<div class="x-kpi"><small>Monatsbudget</small><b>—</b>'
                      '<span>kein Limit gesetzt (unten einstellbar)</span></div>')
    kpis = (
        f'<div class="x-kpis">'
        f'<div class="x-kpi"><small>Kosten · {esc(usage.PERIODS[d["period"]])}</small><b>{esc(usage.fmt_cost(t["cost"]))}</b>'
        f'<span>{t["calls"]} Aufrufe</span></div>'
        f'<div class="x-kpi"><small>Token</small><b>{esc(usage.fmt_tokens(t["tokens"]))}</b>'
        f'<span>{esc(usage.fmt_tokens(t["prompt"]))} rein · {esc(usage.fmt_tokens(t["completion"]))} raus</span></div>'
        f'{budget_kpi}'
        f'<div class="x-kpi"><small>Ohne Preis</small><b class="{"x-warn" if t["unpriced"] else ""}">{t["unpriced"]}</b>'
        f'<span>{"Preis unten eintragen" if t["unpriced"] else "alles bepreist"}</span></div></div>'
    )
    top_cost = max((g["cost"] for g in d["groups"]), default=0) or 1
    rows_html = []
    for g in d["groups"]:
        width = 0 if not top_cost else min(100, g["cost"] / top_cost * 100)
        cost = usage.fmt_cost(g["cost"]) if (g["cost"] or not g["unpriced"]) else "Preis fehlt"
        note = f' <span class="x-pill">{g["unpriced"]}× ohne Preis</span>' if g["unpriced"] and g["cost"] else ""
        rows_html.append(
            f'<tr><td>{esc(usage.nice_key(d["by"], g["key"]))}{note}</td>'
            f'<td class="num">{g["calls"]}</td><td class="num">{esc(usage.fmt_tokens(g["tokens"]))}</td>'
            f'<td class="num">{esc(cost)}</td>'
            f'<td style="width:22%"><div class="x-bar"><i style="width:{width:.0f}%"></i></div></td></tr>')
    table = (f'<div class="panel"><table class="x-tbl"><thead><tr><th>{esc(usage.GROUP_LABELS[d["by"]])}</th>'
             f'<th class="num">Aufrufe</th><th class="num">Token</th><th class="num">Kosten</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows_html) or "<tr><td colspan=5 class=x-muted>Noch keine Aufrufe in diesem Zeitraum.</td></tr>"}'
             f'</tbody></table></div>')
    other = {"period": d["period"], "by": d["by"]}
    filters = (_chip_links(d["period"], usage.PERIODS, "period", {"by": d["by"]}) +
               _chip_links(d["by"], {k: usage.GROUP_LABELS[k] for k in _BY_ORDER}, "by", {"period": d["period"]}))
    unpriced = sorted({str(r.get("model")) for r in d["rows"] if r.get("cost_usd") is None and r.get("model")})
    price_hint = ""
    if unpriced:
        price_hint = ('<p class="x-muted">Für diese Modelle ist kein Preis hinterlegt: <b>'
                      + esc(", ".join(unpriced[:8])) + '</b>. Trag sie hier ein, dann rechnet ASTRA sie ein.</p>')
    prices = (d["cfg"].get("prices") or {}) if isinstance(d["cfg"], dict) else {}
    price_rows = "".join(
        f'<tr><td>{esc(m)}</td><td class="num">{esc(str(v[0]))}</td><td class="num">{esc(str(v[1]))}</td>'
        f'<td class="num"><button class="btn ghost sm danger" name="del" value="{esc(m)}" form="priceform">×</button></td></tr>'
        for m, v in sorted(prices.items()) if isinstance(v, (list, tuple)) and len(v) == 2)
    budget_cfg = (d["cfg"].get("budget") or {}) if isinstance(d["cfg"], dict) else {}
    settings = f"""
<div class="x-grid2">
  <div class="panel"><div class="section-head"><h2>Budget</h2></div>
    <form method="post" action="/admin/usage/budget">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="x-form">
        <div class="field"><label>Monatslimit (USD, 0 = aus)</label>
          <input type="number" step="0.5" min="0" name="monthly_usd" value="{esc(str(budget_cfg.get('monthly_usd', 0)))}"></div>
        <div class="field"><label>Warnung ab (%)</label>
          <input type="number" min="10" max="100" name="warn_pct" value="{esc(str(budget_cfg.get('warn_pct', 80)))}"></div>
      </div>
      <div class="field" style="margin-top:14px"><label style="font-weight:500"><input type="checkbox" name="hard_stop" value="1" {"checked" if budget_cfg.get("hard_stop_third_party") else ""}>
        Bei Limit Sekretär-Antworten stoppen</label>
        <div class="help">Trifft nur Fremd-Nachrichten. Dein eigener Chat läuft immer weiter.</div></div>
      <button class="btn sm" type="submit">Speichern</button>
    </form></div>
  <div class="panel"><div class="section-head"><h2>Modellpreise (USD je 1 Mio. Token)</h2></div>
    {price_hint}
    <form method="post" action="/admin/usage/prices" id="priceform" class="x-form">
      <input type="hidden" name="csrf" value="{esc(token)}">
      <div class="field"><label>Modell (Präfix reicht)</label><input type="text" name="model" placeholder="{esc(unpriced[0]) if unpriced else 'gpt-4.1'}"></div>
      <div class="field"><label>Input</label><input type="number" step="0.01" min="0" name="p_in" style="min-width:80px"></div>
      <div class="field"><label>Output</label><input type="number" step="0.01" min="0" name="p_out" style="min-width:80px"></div>
      <button class="btn sm" type="submit">Eintragen</button>
    </form>
    {f'<table class="x-tbl" style="margin-top:12px"><tbody>{price_rows}</tbody></table>' if price_rows else ''}
    <p class="x-muted" style="margin-top:10px">Lokale Modelle (Ollama) kosten 0. Ohne Eintrag zeigt ASTRA „Preis fehlt“ statt eines geratenen Werts.</p>
  </div>
</div>"""
    recent_rows = "".join(
        f'<tr><td class="x-muted">{esc(r["ts"].astimezone().strftime("%d.%m. %H:%M") if hasattr(r["ts"], "astimezone") else str(r["ts"])[:16])}</td>'
        f'<td>{esc(str(r.get("model") or ""))}</td><td>{esc(usage.PURPOSE_LABELS.get(str(r.get("purpose")), str(r.get("purpose") or "")))}</td>'
        f'<td>{esc(str(r.get("channel") or ""))}</td>'
        f'<td class="num">{esc(usage.fmt_tokens(int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)))}</td>'
        f'<td class="num">{esc(usage.fmt_cost(r.get("cost_usd")))}</td>'
        f'<td>{"" if r.get("ok") else "<span class=x-pill>Fehler</span>"}</td></tr>'
        for r in d["rows"][:25])
    recent = (f'<div class="section-head x-sec"><h2>Letzte Aufrufe</h2></div><div class="panel">'
              f'<table class="x-tbl"><thead><tr><th>Zeit</th><th>Modell</th><th>Zweck</th><th>Kanal</th>'
              f'<th class="num">Token</th><th class="num">Kosten</th><th></th></tr></thead><tbody>{recent_rows}</tbody></table></div>'
              if recent_rows else "")
    return f"""
<section class="hero"><div class="lab-eyebrow">VERBRAUCH</div><h1>Token &amp; Kosten</h1>
<p>Jeder Aufruf am Modell-Gateway wird mitgeschrieben — nach Modell, Zweck, Kanal und Chat.
Nicht erfasst: Embeddings (mem0) und Spracherkennung.</p></section>
{_flash("ok", flash)}{filters}{kpis}{table}
<div class="section-head x-sec"><h2>Einstellungen</h2></div>{settings}{recent}"""


@router.get("/admin/usage", response_class=HTMLResponse)
async def usage_page(request: Request, _: bool = Depends(auth.require_admin),
                     period: str = "month", by: str = "model", saved: str = ""):
    token = await auth.issue_csrf()
    d = await usage_data(period, by)
    msg = {"budget": "Budget gespeichert.", "price": "Preis gespeichert.", "priceerr": ""}.get(saved, "")
    return _html_with_csrf(_shell("Verbrauch", _render_usage(d, msg, token), "usage"), token)


@router.get("/admin/usage/data")
async def usage_json(_: bool = Depends(auth.require_admin), period: str = "month", by: str = "model"):
    d = await usage_data(period, by)
    return JSONResponse({"period": d["period"], "by": d["by"], "totals": d["totals"],
                         "groups": d["groups"], "budget": d["budget"]})


async def _save_usage_cfg(mutate) -> None:
    appset = await _app_settings()
    cfg = appset.get("usage") if isinstance(appset.get("usage"), dict) else {}
    mutate(cfg)
    appset["usage"] = cfg
    await db.set_setting("app_settings", appset)
    usage.set_config(cfg)
    usage._BUDGET_CACHE["state"] = None      # neues Limit sofort wirksam


@router.post("/admin/usage/budget")
async def usage_budget_save(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    try:
        limit = max(0.0, float(str(form.get("monthly_usd") or "0").replace(",", ".")))
        warn = min(100, max(10, int(float(form.get("warn_pct") or 80))))
    except ValueError:
        return RedirectResponse("/admin/usage?saved=priceerr", status_code=303)
    hard = bool(form.get("hard_stop"))
    await _save_usage_cfg(lambda c: c.__setitem__("budget", {
        "monthly_usd": limit, "warn_pct": warn, "hard_stop_third_party": hard}))
    await db.audit("usage_budget_saved", actor="owner", detail={"limit": limit, "warn": warn, "hard": hard})
    return RedirectResponse("/admin/usage?saved=budget", status_code=303)


@router.post("/admin/usage/prices")
async def usage_price_save(request: Request, _: bool = Depends(auth.require_admin)):
    form = await request.form()
    if not await _check_csrf(request, form):
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    remove = str(form.get("del") or "").strip().lower()
    model = str(form.get("model") or "").strip().lower()
    if remove:
        await _save_usage_cfg(lambda c: (c.setdefault("prices", {}) or {}).pop(remove, None))
        return RedirectResponse("/admin/usage?saved=price", status_code=303)
    try:
        p_in = float(str(form.get("p_in") or "").replace(",", "."))
        p_out = float(str(form.get("p_out") or "").replace(",", "."))
        if not model or len(model) > 120 or p_in < 0 or p_out < 0:
            raise ValueError
    except ValueError:
        return RedirectResponse("/admin/usage?saved=priceerr", status_code=303)
    await _save_usage_cfg(lambda c: c.setdefault("prices", {}).__setitem__(model, [p_in, p_out]))
    await db.audit("usage_price_saved", actor="owner", detail={"model": model})
    return RedirectResponse("/admin/usage?saved=price", status_code=303)


# ══ Kontakte & Gruppen (Karten) ═══════════════════════════════════════════════
from urllib.parse import quote  # noqa: E402

from .. import cards, digest, styles  # noqa: E402

RULE_LABELS = {"": "Standard (nach Vertrauensstufe)", "block": "Nie antworten (ignorieren)",
               "ask": "Immer erst Bahrian fragen", "allow": "Antworten erlaubt (Triage entscheidet)",
               "direct": "Immer direkt antworten"}
TIER_LABELS = {0: "0 · Ich", 1: "1 · Eng / vertraut", 2: "2 · Bekannt", 3: "3 · Fremd"}
SHARE_LEVEL_LABELS = {"": "Standard", "none": "Nichts sagen", "freebusy": "Nur frei/beschäftigt",
                      "details": "Termindetails erlaubt"}
YESNO_LABELS = {"": "Standard", "yes": "Darf erfahren", "no": "Nie sagen"}
GROUP_TRIGGER_LABELS = {"mention": "Nur bei @Erwähnung / Antwort auf ASTRA", "always": "Auf alles reagieren",
                        "keywords": "Nur bei Stichwörtern", "off": "Nur zuhören (nie antworten)"}
GROUP_ROLE_LABELS = {"assistant": "Assistent (beantwortet Fragen)", "moderator": "Moderator (ermahnt bei Streit)",
                     "listener": "Zuhörer (schweigt, merkt sich)"}
ACTIVE_LABELS = {"inherit": "Wie global eingestellt", "always": "Immer aktiv", "never": "Nie aktiv",
                 "window": "Nur in einem Zeitfenster"}
DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _select(name: str, options: dict, current, *, extra: str = "") -> str:
    cur = "" if current is None else str(current)
    inner = "".join(f'<option value="{esc(str(k))}"{" selected" if str(k) == cur else ""}>{esc(v)}</option>'
                    for k, v in options.items())
    return f'<select name="{esc(name)}" {extra}>{inner}</select>'


def _kind_stem(card: dict) -> str:
    return "groups" if card.get("kind") == "group" else "contacts"


def _stems(card: dict) -> list[str]:
    return digest.card_stems(card)


def _card_capsule(card: dict) -> tuple[dict | None, str]:
    """Erste vorhandene Kapsel dieser Karte (+ Stem)."""
    kind = _kind_stem(card)
    for stem in _stems(card):
        cap = digest.load_capsule(kind, stem)
        if cap:
            return cap, stem
    return None, (_stems(card) or [""])[0]


def _card_tile(c: dict) -> str:
    style_label = styles.label(c["style"]) if c.get("style") else "Standard"
    tags = [f'<span class="x-pill">Stufe {c["trust_tier"]}</span>',
            f'<span class="x-pill">{esc(style_label)}</span>']
    if c["rule"]:
        tags.insert(0, f'<span class="x-pill">{esc(RULE_LABELS.get(c["rule"], c["rule"]).split(" (")[0])}</span>')
    if c["kind"] == "group":
        tags.append(f'<span class="x-pill">{esc(GROUP_TRIGGER_LABELS[c["group"]["trigger"]].split(" /")[0])}</span>')
    if c["proposals"]:
        tags.append(f'<span class="x-pill x-warn">{len(c["proposals"])} Vorschlag</span>')
    who = ", ".join(f'{h["channel"]}' for h in c["handles"]) or "keine Kennung"
    return (f'<a class="card" href="/admin/contacts/{quote(c["key"])}" style="text-decoration:none;color:inherit">'
            f'<div class="top"><div class="meta"><h3>{esc(c["name"])}</h3><div class="cat">{esc(who)}'
            f'{" · " + esc(c["relationship"]) if c["relationship"] else ""}</div></div></div>'
            f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">{"".join(tags)}</div></a>')


def _new_card_form(kind: str, token: str) -> str:
    who = "Gruppe" if kind == "group" else "Person"
    ph = "Gruppen-ID, z. B. 123456789-1234@g.us" if kind == "group" else "Nummer, z. B. +49 171 1234567"
    return (f'<form method="post" action="/admin/contacts/new" class="x-form" style="margin-bottom:14px">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="kind" value="{kind}">'
            f'<div class="field"><label>Neue {who}</label><input type="text" name="name" placeholder="Name" required></div>'
            f'<div class="field"><label>Kennung (optional)</label><input type="text" name="handle" placeholder="{ph}" style="min-width:230px"></div>'
            f'<button class="btn sm" type="submit">Anlegen</button></form>')


_SCOPES = {"all": "Alle", "person": "Personen", "group": "Gruppen", "nocard": "Ohne Karte", "off": "Secretary aus", "proposals": "Vorschläge"}
_KEEP = {"": "— nicht ändern —"}


def _bulk_select(field: str, label: str, options: dict, *, clearable: bool = True) -> str:
    opts = dict(_KEEP)
    if clearable:
        opts["__clear__"] = "Standard (zurücksetzen)"
    opts.update({k: v for k, v in options.items() if k != ""})
    return f'<div class="field"><label>{esc(label)}</label>{_select("b_" + field, opts, "")}</div>'


def _bulk_panel() -> str:
    style_opts = {k: f"{e} {l}".strip() for k, l, _s, e in styles.choices()}
    model_opts = {"small": "Klein", "medium": "Mittel", "heavy": "Schwer", "code": "Code"}
    fields = "".join([
        _bulk_select("rule", "Regel", {k: v for k, v in RULE_LABELS.items() if k}),
        _bulk_select("trust_tier", "Vertrauensstufe", {str(k): v for k, v in TIER_LABELS.items() if k}, clearable=False),
        _bulk_select("style", "Stil", style_opts),
        _bulk_select("share_availability", "Kalender / Verfügbarkeit", {k: v for k, v in SHARE_LEVEL_LABELS.items() if k}),
        *(_bulk_select(f"share_{t}", cards.TOPIC_LABELS[t], {k: v for k, v in YESNO_LABELS.items() if k})
          for t in cards.SHARE_TOPICS[1:]),
        _bulk_select("active_mode", "Aktivzeiten", {k: v for k, v in ACTIVE_LABELS.items() if k != "window"}, clearable=False),
        _bulk_select("model_tier", "Modell", model_opts),
        _bulk_select("group_trigger", "Gruppen: wann reagieren?", GROUP_TRIGGER_LABELS, clearable=False),
        _bulk_select("group_role", "Gruppen: Rolle", GROUP_ROLE_LABELS, clearable=False),
    ])
    return f"""
<div class="panel" id="bulkpanel" style="margin-top:14px">
  <div class="section-head"><h2>Regeln für die Auswahl</h2><span class="count" id="selcount">0 ausgewählt</span></div>
  <p class="x-muted">Nur was du hier änderst, wird gesetzt — alles andere bleibt pro Person wie es ist.
  Kontakte ohne Karte bekommen dabei automatisch eine. Gruppen-Felder wirken nur auf Gruppen.</p>
  <div class="x-form" style="margin-bottom:14px">
    <b>Secretary für die Auswahl:</b>
    <button class="btn secondary sm" type="submit" name="do" value="sec_on" id="bulkon" disabled>✅ Einschalten</button>
    <button class="btn secondary sm" type="submit" name="do" value="sec_off" id="bulkoff" disabled>🔕 Ausschalten</button>
  </div>
  <div class="x-grid2" style="grid-template-columns:repeat(auto-fit,minmax(210px,1fr))">{fields}</div>
  <div class="x-form" style="margin-top:6px">
    <button class="btn sm" type="submit" name="do" value="apply" id="bulkapply" disabled>Auf Auswahl anwenden</button>
    <button class="btn ghost danger sm" type="submit" name="do" value="delete" id="bulkdelete" disabled
      onclick="return confirm('Die Karten der ausgewählten Einträge löschen? (Nachrichten-Journale bleiben.)')">Karten löschen</button>
  </div>
</div>"""


def _sec_toggle(ref: str, on: bool, who: str) -> str:
    """Schalter „Secretary“ für eine Person/Gruppe. Ohne JavaScript ein normaler Formular-Knopf."""
    return (f'<button type="submit" class="sec-toggle {"on" if on else ""}" formaction="/admin/contacts/secretary" '
            f'name="toggle" value="{esc(ref)}|{0 if on else 1}" role="switch" aria-checked="{"true" if on else "false"}" '
            f'aria-label="Secretary für {esc(who)}" title="Secretary für {esc(who)} {"ausschalten" if on else "einschalten"}">'
            f'<span class="track"></span><span class="lbl">{"An" if on else "Aus"}</span></button>')


def _dir_row(r: dict) -> str:
    ref = cards.encode_ref(r)
    name = (f'<a href="/admin/contacts/{quote(r["key"])}"><b>{esc(r["name"])}</b></a>' if r["has_card"]
            else f'<b>{esc(r["name"])}</b> <span class="x-pill">ohne Karte</span>')
    extra = f' <span class="x-pill x-warn">{r["proposals"]} Vorschlag</span>' if r["proposals"] else ""
    kind = "Gruppe" if r["kind"] == "group" else "Person"
    style = styles.label(r["style"]) if r["style"] else "—"
    rule = RULE_LABELS.get(r["rule"], r["rule"]).split(" (")[0] if r["rule"] else "—"
    return (f'<tr><td style="width:34px"><input type="checkbox" name="sel" value="{esc(ref)}" class="rowsel" aria-label="{esc(r["name"])} auswählen"></td>'
            f'<td>{name}{extra}<div class="x-muted">{kind} · {esc(", ".join(r["channels"]))}'
            f'{" · " + esc(r["relationship"]) if r["relationship"] else ""}</div></td>'
            f'<td>{_sec_toggle(ref, r["sec_on"], r["name"])}</td>'
            f'<td class="num">{r["tier"]}</td><td>{esc(rule)}</td><td>{esc(style)}</td></tr>')


@router.get("/admin/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = "", q: str = "",
                        scope: str = "all", n: int = 0):
    token = await auth.issue_csrf()
    scope = scope if scope in _SCOPES else "all"
    try:
        known = await db.contacts_list()
    except Exception:  # noqa: BLE001
        known = []
    everything = cards.directory(await cards.load_all(force=True), known)
    rows = cards.filter_directory(everything, q=q, scope=scope)
    counts = {k: len(cards.filter_directory(everything, scope=k)) for k in _SCOPES}
    chips = "".join(
        f'<a class="chip{" active" if k == scope else ""}" href="/admin/contacts?scope={k}{"&q=" + quote(q) if q else ""}">'
        f'{esc(v)} · {counts[k]}</a>' for k, v in _SCOPES.items())
    msg = {"created": "Karte angelegt.", "deleted": "Karte gelöscht.",
           "bulk": f"Regeln auf {n} Einträge angewendet.", "bulkdel": f"{n} Karten gelöscht.",
           "bulkon": f"Secretary für {n} Einträge eingeschaltet.", "bulkoff": f"Secretary für {n} Einträge ausgeschaltet.",
           "secon": "Secretary eingeschaltet.", "secoff": "Secretary ausgeschaltet.",
           "bulknone": "Nichts ausgewählt bzw. nichts zu ändern."}.get(saved, "")
    table = (f'<div class="panel"><table class="x-tbl" id="dirtbl"><thead><tr>'
             f'<th><input type="checkbox" id="selall" aria-label="Alle auswählen"></th><th>Name</th><th>Secretary</th>'
             f'<th class="num">Stufe</th><th>Regel</th><th>Stil</th></tr></thead>'
             f'<tbody>{"".join(_dir_row(r) for r in rows) or "<tr><td colspan=6 class=x-muted>Keine Einträge in dieser Ansicht.</td></tr>"}</tbody></table></div>')
    body = f"""
<section class="hero"><div class="lab-eyebrow">KONTAKTE</div><h1>Personen &amp; Gruppen</h1>
<p>Alle, die ASTRA kennt. Klick auf einen Namen für die Details — oder wähle mehrere (oder alle) aus und
setze Regeln in einem Rutsch. Gruppen funktionieren wie Nutzer: nur was du freigibst, existiert für ASTRA.</p></section>
{_flash("ok", msg)}
<div class="chips">{chips}</div>
<form method="get" class="x-form" style="margin-bottom:14px"><input type="hidden" name="scope" value="{esc(scope)}">
<div class="field"><input type="text" name="q" value="{esc(q)}" placeholder="Suchen…"></div>
<button class="btn ghost sm" type="submit">Suchen</button></form>
<form method="post" action="/admin/contacts/bulk" id="bulkform">
<input type="hidden" name="csrf" value="{esc(token)}">
{table}
{_bulk_panel()}
</form>
<script>
(function(){{
  const form=document.getElementById('bulkform'), all=document.getElementById('selall');
  const rows=()=>[...form.querySelectorAll('.rowsel')];
  function sync(){{
    const n=rows().filter(r=>r.checked).length;
    document.getElementById('selcount').textContent=n+' ausgewählt';
    document.getElementById('bulkapply').disabled=n===0;
    document.getElementById('bulkdelete').disabled=n===0;
    document.getElementById('bulkon').disabled=n===0; document.getElementById('bulkoff').disabled=n===0;
    all.checked=n>0&&n===rows().length; all.indeterminate=n>0&&n<rows().length;
  }}
  all.addEventListener('change',()=>{{rows().forEach(r=>r.checked=all.checked);sync();}});
  form.addEventListener('click',async e=>{{
    const b=e.target.closest('.sec-toggle'); if(!b) return;
    e.preventDefault(); if(b.classList.contains('busy')) return;
    b.classList.add('busy');
    const fd=new FormData(); fd.append('csrf',form.querySelector('[name=csrf]').value);
    fd.append('toggle',b.value); 
    try {{
      const r=await fetch('/admin/contacts/secretary',{{method:'POST',body:fd,headers:{{'Accept':'application/json'}}}});
      const j=await r.json(); if(!j.ok) throw new Error(j.error||'Fehler');
      b.classList.toggle('on',j.on); b.setAttribute('aria-checked',j.on?'true':'false');
      b.querySelector('.lbl').textContent=j.on?'An':'Aus'; b.value=b.value.split('|')[0]+'|'+(j.on?0:1);
    }} catch(err) {{ alert('Konnte den Schalter nicht setzen: '+err.message); }}
    b.classList.remove('busy');
  }});
  form.addEventListener('change',e=>{{if(e.target.classList.contains('rowsel'))sync();}});
  sync();
}})();
</script>
<div class="section-head x-sec"><h2>Neu anlegen</h2></div>
<div class="x-grid2"><div class="panel">{_new_card_form("person", token)}</div><div class="panel">{_new_card_form("group", token)}</div></div>
<p class="x-muted" style="margin-top:12px">Neue Absender bekommen automatisch eine Karte, sobald du bei einer Freigabe „Immer erlauben“ drückst.</p>"""
    return _html_with_csrf(_shell("Kontakte", body, "contacts"), token)


def _find_by_key(all_cards: list[dict], key: str) -> dict | None:
    return next((c for c in all_cards if c["key"] == key), None)


def _render_card_editor(c: dict, token: str, flash: str, snapshot: dict, seen: list[dict]) -> str:
    is_group = c["kind"] == "group"
    share = c["share"]
    style_keys = {k: f"{e} {l}".strip() for k, l, _s, e in styles.choices()}
    style_keys = {"": "Standard (Profil / Vorgabe)", **style_keys, "__custom__": "Eigener Text …"}
    cur_style = c["style"] if c["style"] in style_keys else ("__custom__" if c["style"] else "")
    style_samples = "".join(
        f'<div class="x-muted"><b>{esc(l)}:</b> „{esc(s)}“</div>' for _k, l, s, _e in styles.choices())
    model_opts = {o["value"]: o["label"] + (" (nicht eingerichtet)" if o["disabled"] else "")
                  for o in model_choice.options(snapshot, seen)}
    cur_model = model_choice.encode(c["model"])
    if cur_model and cur_model not in model_opts:
        model_opts[cur_model] = model_choice.label(c["model"], snapshot)
    model_opts[""] = "Standard (wie global eingestellt)"
    days = "".join(f'<label style="display:inline-flex;align-items:center;gap:5px;margin:0 14px 0 0;font-weight:500"><input type="checkbox" name="days" value="{i}"'
                   f'{" checked" if i in c["active"]["days"] else ""}> {n}</label>' for i, n in enumerate(DAY_NAMES))
    group_block = ""
    if is_group:
        g = c["group"]
        group_block = f"""
<div class="panel"><div class="section-head"><h2>Gruppen-Verhalten</h2></div>
  <div class="x-grid2">
    <div class="field"><label>Wann reagiert ASTRA?</label>{_select("group_trigger", GROUP_TRIGGER_LABELS, g["trigger"])}
      <div class="help">Standard: nur wenn jemand dich (@Bahrian), „astra“ oder einen Alias nennt.</div></div>
    <div class="field"><label>Rolle</label>{_select("group_role", GROUP_ROLE_LABELS, g["role"])}</div>
    <div class="field"><label>Stichwörter (Komma)</label><input type="text" name="group_keywords" value="{esc(', '.join(g['keywords']))}"></div>
    <div class="field"><label>Aliasse / Spitznamen (Komma)</label><input type="text" name="group_aliases" value="{esc(', '.join(g['aliases']))}"></div>
  </div>
  <label><input type="checkbox" name="group_actions" value="1" {"checked" if g["actions"] else ""}> ASTRA darf in dieser Gruppe Aktionen vorbereiten (du bestätigst weiter selbst)</label>
</div>"""
    learned = ""
    if c["learned"]:
        rows = "".join(
            f'<tr><td>{esc(cards.TOPIC_LABELS.get(x["topic"], x["topic"]))}</td><td>{esc(str(x.get("level", "")))}</td>'
            f'<td class="num"><form method="post" action="/admin/contacts/{quote(c["key"])}/revoke" style="margin:0">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="topic" value="{esc(x["topic"])}">'
            f'<button class="btn ghost sm" type="submit">Widerrufen</button></form></td></tr>' for x in c["learned"][::-1][:12])
        learned = (f'<div class="panel"><div class="section-head"><h2>Gelernt aus deinen Entscheidungen</h2></div>'
                   f'<table class="x-tbl"><tbody>{rows}</tbody></table></div>')
    proposals = ""
    if c["proposals"]:
        rows = "".join(
            f'<tr><td><span class="x-pill">{esc(str(p.get("kind", "")))}</span></td><td>{esc(p["text"])}</td>'
            f'<td class="num" style="white-space:nowrap"><form method="post" action="/admin/contacts/{quote(c["key"])}/proposal" style="margin:0;display:inline">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="i" value="{i}">'
            f'<button class="btn sm" name="do" value="accept" type="submit">Übernehmen</button> '
            f'<button class="btn ghost sm" name="do" value="reject" type="submit">Verwerfen</button></form></td></tr>'
            for i, p in enumerate(c["proposals"]))
        proposals = (f'<div class="panel"><div class="section-head"><h2>Vorschläge von ASTRA</h2></div>'
                     f'<p class="x-muted">Aus den Nachrichten abgeleitet. Nichts davon gilt, bevor du es übernimmst.</p>'
                     f'<table class="x-tbl"><tbody>{rows}</tbody></table></div>')
    cap, stem = _card_capsule(c)
    if cap:
        quotes = "".join(f'<li>„{esc(q.get("text", "") if isinstance(q, dict) else str(q))}“</li>' for q in (cap.get("quotes") or [])[:6])
        facts = "".join(f"<li>{esc(str(f))}</li>" for f in (cap.get("facts") or [])[:10])
        capsule_html = (f'<p>{esc(cap.get("summary") or "")}</p>'
                        + (f'<b>Fakten</b><ul>{facts}</ul>' if facts else "")
                        + (f'<b>Wörtliche Zitate</b><ul>{quotes}</ul>' if quotes else "")
                        + f'<p class="x-muted">Stand: {esc(str(cap.get("updated") or cap.get("last_ts") or "?"))}</p>')
    else:
        capsule_html = '<p class="x-muted">Noch keine Zusammenfassung. Sie entsteht nachts um 03:30 oder per Knopf.</p>'
    ctx_buttons = ""
    if c["handles"]:
        ctx_buttons = (
            f'<form method="post" action="/admin/contacts/{quote(c["key"])}/digest" style="display:inline">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><button class="btn sm secondary" type="submit">Jetzt zusammenfassen</button></form> '
            f'<form method="post" action="/admin/contacts/{quote(c["key"])}/forget" style="display:inline" '
            f'onsubmit="return confirm(\'Journal, Rohlog und Zusammenfassung dieser {"Gruppe" if is_group else "Person"} restlos löschen?\')">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><button class="btn ghost sm danger" type="submit">Alles vergessen</button></form>')
    return f"""
<section class="hero"><div class="lab-eyebrow">{"GRUPPE" if is_group else "PERSON"}</div><h1>{esc(c["name"])}</h1>
<p><a href="/admin/contacts">← Alle Kontakte</a></p>
<form method="post" action="/admin/contacts/secretary" style="margin-top:12px">
  <input type="hidden" name="csrf" value="{esc(token)}">
  <span class="x-muted" style="margin-right:8px">Secretary für {"diese Gruppe" if is_group else "diese Person"}:</span>{_sec_toggle(cards.encode_ref({"key": c["key"]}), cards.secretary_on(c), c["name"])}
</form></section>
{_flash("ok", flash)}
<form method="post" action="/admin/contacts/{quote(c["key"])}" class="x-stack">
<input type="hidden" name="csrf" value="{esc(token)}">
<div class="panel"><div class="section-head"><h2>Identität &amp; Regel</h2></div>
  <div class="x-grid2">
    <div class="field"><label>Name</label><input type="text" name="name" value="{esc(c["name"])}" required></div>
    <div class="field"><label>Beziehung</label><input type="text" name="relationship" value="{esc(c["relationship"])}" placeholder="Freundin, Lehrer, Verein …"></div>
    <div class="field"><label>Vertrauensstufe</label>{_select("trust_tier", TIER_LABELS, c["trust_tier"])}</div>
    <div class="field"><label>Regel</label>{_select("rule", RULE_LABELS, c["rule"])}</div>
  </div>
  <div class="field"><label>Kennungen (eine pro Zeile: kanal: kennung)</label>
    <textarea name="handles" rows="3" placeholder="whatsapp: +49 171 1234567&#10;signal: +49 …">{esc(cards.handles_text(c))}</textarea>
    <div class="help">Nummern werden tolerant verglichen (0171… = +49 171…). Kanäle: whatsapp, signal, telegram, email.</div></div>
</div>
<div class="panel"><div class="section-head"><h2>Ton &amp; Anweisung</h2></div>
  <div class="x-grid2">
    <div class="field"><label>Stil</label>{_select("style", style_keys, cur_style)}
      <input type="text" name="style_custom" value="{esc(c["style"] if cur_style == "__custom__" else "")}" placeholder="Eigener Stil (nur bei „Eigener Text“)" style="margin-top:8px"></div>
    <div class="field"><label>Modell nur für diese {"Gruppe" if is_group else "Person"}</label>{_select("model", model_opts, cur_model)}</div>
  </div>
  <details><summary class="x-muted">Stil-Vorschau</summary>{style_samples}</details>
  <div class="field" style="margin-top:12px"><label>Anweisung (verbindlich)</label>
    <textarea name="instruction" rows="3" placeholder="z. B. „Duze sie immer, erwähne nie meinen Stundenplan.“">{esc(c["instruction"])}</textarea></div>
</div>
<div class="panel"><div class="section-head"><h2>Was darf {"die Gruppe" if is_group else "sie/er"} erfahren?</h2></div>
  <div class="x-grid2">
    <div class="field"><label>{esc(cards.TOPIC_LABELS["availability"])}</label>{_select("share_availability", SHARE_LEVEL_LABELS, share["availability"])}</div>
    {"".join(f'<div class="field"><label>{esc(cards.TOPIC_LABELS[t])}</label>{_select("share_" + t, YESNO_LABELS, share[t])}</div>' for t in cards.SHARE_TOPICS[1:])}
  </div>
  <p class="x-muted">„Standard“ heißt: es gilt die Vertrauensstufe. Alles darüber fragt ASTRA bei dir nach.</p>
</div>
{group_block}
<div class="panel"><div class="section-head"><h2>Aktivzeiten</h2></div>
  <div class="x-form">
    <div class="field"><label>Modus</label>{_select("active_mode", ACTIVE_LABELS, c["active"]["mode"])}</div>
    <div class="field"><label>Von</label><input type="text" name="active_start" value="{esc(c["active"]["start"])}" placeholder="08:00" style="min-width:80px"></div>
    <div class="field"><label>Bis</label><input type="text" name="active_end" value="{esc(c["active"]["end"])}" placeholder="20:00" style="min-width:80px"></div>
  </div>
  <div class="field"><label>Tage (leer = alle)</label>{days}</div>
</div>
<div class="panel"><div class="section-head"><h2>Notizen (nur für dich)</h2></div>
  <textarea name="notes" rows="3">{esc(c["notes"])}</textarea></div>
<div><button class="btn" type="submit">Speichern</button></div>
</form>
<style>.x-stack>*{{margin-bottom:14px}}.x-stack textarea{{width:100%}}</style>
<div class="section-head x-sec"><h2>Kontext</h2></div>
<div class="panel">{capsule_html}<div style="margin-top:12px">{ctx_buttons}</div></div>
{learned}{proposals}
<div class="section-head x-sec"><h2>Gefahrenzone</h2></div>
<form method="post" action="/admin/contacts/{quote(c["key"])}/delete" onsubmit="return confirm('Karte wirklich löschen?')">
<input type="hidden" name="csrf" value="{esc(token)}"><button class="btn ghost danger sm" type="submit">Karte löschen</button></form>"""


@router.get("/admin/contacts/{key}", response_class=HTMLResponse)
async def contact_edit_page(key: str, request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    token = await auth.issue_csrf()
    card = _find_by_key(await cards.load_all(force=True), key)
    if not card:
        return RedirectResponse("/admin/contacts", status_code=303)
    try:
        seen = await db.usage_models_seen()
    except Exception:  # noqa: BLE001
        seen = []
    msg = {"saved": "Gespeichert.", "revoked": "Freigabe widerrufen.", "digest": "Zusammenfassung aktualisiert.",
           "nodigest": "Keine neuen Nachrichten oder kein Modell verfügbar.", "forgot": "Kontext gelöscht.",
           "accepted": "Vorschlag übernommen.", "rejected": "Vorschlag verworfen."}.get(saved, "")
    html = _render_card_editor(card, token, msg, models.model_config_snapshot(), seen)
    return _html_with_csrf(_shell(card["name"], html, "contacts"), token)


async def _csrf_form(request: Request):
    form = await request.form()
    return form, await _check_csrf(request, form)


@router.post("/admin/contacts/new")
async def contact_new(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    name = str(form.get("name") or "").strip()
    kind = "group" if form.get("kind") == "group" else "person"
    if not name:
        return RedirectResponse("/admin/contacts", status_code=303)
    handles = cards.parse_handles(str(form.get("handle") or ""))
    card = cards.new_card(kind, name, handles)
    keys = {c["key"] for c in await cards.load_all(force=True)}
    base, i = card["key"], 2
    while card["key"] in keys:
        card["key"], i = f"{base}_{i}", i + 1
    if kind == "group":
        card["trust_tier"] = 3
    saved = await cards.save_card(card)
    await db.audit("card_created", actor="owner", detail={"key": saved["key"], "kind": kind})
    return RedirectResponse(f"/admin/contacts/{quote(saved['key'])}?saved=saved", status_code=303)


async def _card_for_ref(ref: dict, existing: list[dict], by_key: dict, keys: set) -> dict | None:
    """Karte zu einer Auswahl-Referenz: vorhandene Karte oder — für Kontakte ohne Karte — eine neue."""
    card = by_key.get(ref.get("key", ""))
    if card is not None or not ref.get("handle"):
        return card
    found = cards.find_in(existing, ref["channel"], ref["handle"])
    if found is not None:
        return found
    kind = "group" if cards.is_group_handle(ref["channel"], ref["handle"]) else "person"
    card = cards.new_card(kind, ref["name"] or ref["handle"], [{"channel": ref["channel"], "id": ref["handle"]}])
    base, i = card["key"], 2
    while card["key"] in keys:
        card["key"], i = f"{base}_{i}", i + 1
    keys.add(card["key"])
    if kind == "group":
        card["trust_tier"] = 3
    return card


@router.post("/admin/contacts/secretary")
async def contacts_secretary_toggle(request: Request, _: bool = Depends(auth.require_admin)):
    """Ein Schalter „Secretary an/aus“ für genau eine Person/Gruppe (`toggle` = '<ref>|<0/1>')."""
    form, ok = await _csrf_form(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    raw, _, flag = str(form.get("toggle") or "").rpartition("|")
    ref = cards.decode_ref(raw)
    if not ref or flag not in ("0", "1"):
        return JSONResponse({"ok": False, "error": "Ungültige Auswahl."}, status_code=400) if wants_json \
            else RedirectResponse("/admin/contacts?saved=bulknone", status_code=303)
    existing = await cards.load_all(force=True)
    card = await _card_for_ref(ref, existing, {c["key"]: c for c in existing}, {c["key"] for c in existing})
    if card is None:
        return JSONResponse({"ok": False, "error": "Karte nicht gefunden."}, status_code=404) if wants_json \
            else RedirectResponse("/admin/contacts?saved=bulknone", status_code=303)
    saved = await cards.save_card(cards.set_secretary(card, flag == "1"))
    on = cards.secretary_on(saved)
    await db.audit("card_secretary_toggled", actor="owner", detail={"key": saved["key"], "on": on})
    if wants_json:
        return JSONResponse({"ok": True, "on": on, "key": saved["key"], "name": saved["name"]})
    return RedirectResponse(f"/admin/contacts?saved={'secon' if on else 'secoff'}", status_code=303)


@router.post("/admin/contacts/bulk")
async def contacts_bulk(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    refs = [cards.decode_ref(str(r)) for r in form.getlist("sel")][:300]
    refs = [r for r in refs if r]
    if not refs:
        return RedirectResponse("/admin/contacts?saved=bulknone", status_code=303)
    existing = await cards.load_all(force=True)
    by_key = {c["key"]: c for c in existing}
    action = str(form.get("do") or "apply")
    if action == "delete":
        n = 0
        for r in refs:
            if r.get("key") in by_key:
                n += await cards.delete_card(r["key"])
        await db.audit("cards_bulk_deleted", actor="owner", detail={"n": n})
        return RedirectResponse(f"/admin/contacts?saved=bulkdel&n={n}", status_code=303)
    if action in ("sec_on", "sec_off"):
        patcher = lambda c: cards.set_secretary(c, action == "sec_on")   # noqa: E731
        fields = ["secretary"]
    else:
        patch = cards.bulk_patch(form.get)
        if not patch:
            return RedirectResponse("/admin/contacts?saved=bulknone", status_code=303)
        patcher = lambda c: cards.apply_bulk(c, patch)[0]               # noqa: E731
        fields = sorted(patch)
    keys, n = set(by_key), 0
    for r in refs:
        card = await _card_for_ref(r, existing, by_key, keys)
        if card is None:
            continue
        await cards.save_card(patcher(card))
        n += 1
    await db.audit("cards_bulk_saved", actor="owner", detail={"n": n, "fields": fields,
                                                              "secretary": action if action.startswith("sec_") else None})
    saved = {"sec_on": "bulkon", "sec_off": "bulkoff"}.get(action, "bulk")
    return RedirectResponse(f"/admin/contacts?saved={saved}&n={n}", status_code=303)


@router.post("/admin/contacts/{key}")
async def contact_save(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    existing = _find_by_key(await cards.load_all(force=True), key)
    if not existing:
        return RedirectResponse("/admin/contacts", status_code=303)
    card = cards.card_from_form(form, existing)
    await cards.save_card(card)
    await db.audit("card_saved", actor="owner", detail={"key": key})
    return RedirectResponse(f"/admin/contacts/{quote(key)}?saved=saved", status_code=303)


@router.post("/admin/contacts/{key}/revoke")
async def contact_revoke(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    card = _find_by_key(await cards.load_all(force=True), key)
    if card:
        await cards.save_card(cards.revoke_learned(card, str(form.get("topic") or "")))
        await db.audit("card_revoked", actor="owner", detail={"key": key, "topic": str(form.get("topic"))})
    return RedirectResponse(f"/admin/contacts/{quote(key)}?saved=revoked", status_code=303)


@router.post("/admin/contacts/{key}/proposal")
async def contact_proposal(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    card = _find_by_key(await cards.load_all(force=True), key)
    try:
        idx = int(form.get("i"))
    except (TypeError, ValueError):
        idx = -1
    if not card or not (0 <= idx < len(card["proposals"])):
        return RedirectResponse(f"/admin/contacts/{quote(key)}", status_code=303)
    prop = card["proposals"][idx]
    card["proposals"] = card["proposals"][:idx] + card["proposals"][idx + 1:]
    accepted = form.get("do") == "accept"
    if accepted:
        line = str(prop["text"]).strip()
        if prop.get("kind") == "style":
            card["style"] = line
        elif prop.get("kind") == "fact":
            card["notes"] = (card["notes"] + "\n" + line).strip()
        else:
            card["instruction"] = (card["instruction"] + " " + line).strip()
    await cards.save_card(card)
    await db.audit("card_proposal", actor="owner", detail={"key": key, "accepted": accepted})
    return RedirectResponse(f"/admin/contacts/{quote(key)}?saved={'accepted' if accepted else 'rejected'}",
                            status_code=303)


@router.post("/admin/contacts/{key}/digest")
async def contact_digest(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    card = _find_by_key(await cards.load_all(force=True), key)
    if not card:
        return RedirectResponse("/admin/contacts", status_code=303)
    from ..config import get_settings
    appset = await _app_settings()
    ctx = appset.get("context") or {}
    result = None
    for stem in _stems(card):
        result = await digest.digest_one(_kind_stem(card), stem, name=card["name"],
                                         owner_name=get_settings().astra_owner_name,
                                         retention_days=int(ctx.get("retention_days") or 0),
                                         pick=ctx.get("model") or None, force=True) or result
    return RedirectResponse(f"/admin/contacts/{quote(key)}?saved={'digest' if result else 'nodigest'}",
                            status_code=303)


@router.post("/admin/contacts/{key}/forget")
async def contact_forget(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    card = _find_by_key(await cards.load_all(force=True), key)
    if card:
        removed = sum(digest.forget(_kind_stem(card), stem) for stem in _stems(card))
        await db.audit("card_forgotten", actor="owner", detail={"key": key, "files": removed})
    return RedirectResponse(f"/admin/contacts/{quote(key)}?saved=forgot", status_code=303)


@router.post("/admin/contacts/{key}/delete")
async def contact_delete(key: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    await cards.delete_card(key)
    await db.audit("card_deleted", actor="owner", detail={"key": key})
    return RedirectResponse("/admin/contacts?saved=deleted", status_code=303)


# ══ Prompt-Werkstatt ══════════════════════════════════════════════════════════
from .. import prompts  # noqa: E402

_PRE = ('style="white-space:pre-wrap;font:12.5px/1.5 ui-monospace,Menlo,monospace;background:var(--surface-2);'
        'padding:12px;border-radius:8px;overflow:auto;max-height:340px"')


def _diff_html(text: str) -> str:
    """Diff mit Farbe pro Zeile (alles escaped)."""
    lines = []
    for ln in (text or "").splitlines():
        color = "#86efac" if ln.startswith("+") and not ln.startswith("+++") else \
                "#fda4af" if ln.startswith("-") and not ln.startswith("---") else "inherit"
        lines.append(f'<span style="color:{color}">{esc(ln)}</span>')
    return "\n".join(lines)


def _proposal_card(p: dict, token: str, current: str) -> str:
    st = p.get("stats") or {}
    return (f'<div class="panel"><div class="section-head"><h2>Vorschlag von {esc(str(p.get("author", "astra")))}</h2></div>'
            f'<p>{esc(p.get("rationale") or "(ohne Begründung)")}</p>'
            f'<p class="x-muted">+{st.get("added", "?")} / −{st.get("removed", "?")} Zeilen · '
            f'Ähnlichkeit {esc(str(st.get("similarity", "?")))}</p>'
            f'<pre {_PRE}>{_diff_html(prompts.diff(current, p["text"]))}</pre>'
            f'<form method="post" action="/admin/prompts/{esc(p["name"])}/proposal" class="x-form" style="margin-top:10px">'
            f'<input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="pid" value="{esc(p["id"])}">'
            f'<button class="btn sm" name="do" value="approve" type="submit">Freigeben</button>'
            f'<button class="btn ghost sm" name="do" value="reject" type="submit">Ablehnen</button></form></div>')


@router.get("/admin/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    token = await auth.issue_csrf()
    pending = prompts.proposals("pending")
    tiles = []
    for name in prompts.NAMES:
        n = sum(1 for p in pending if p["name"] == name)
        state = "angepasst" if prompts.is_overridden(name) else "Standard"
        badge = f' <span class="x-pill x-warn">{n} Vorschlag</span>' if n else ""
        tiles.append(f'<a class="card" href="/admin/prompts/{name}" style="text-decoration:none;color:inherit">'
                     f'<div class="top"><div class="meta"><h3>{esc(prompts.LABELS[name])}</h3>'
                     f'<div class="cat">{state}{badge}</div></div></div>'
                     f'<p>{esc(prompts.get(name)[:140])}…</p></a>')
    msg = {"reviewed": "Selbstprüfung abgeschlossen.", "nochange": "ASTRA sieht aktuell nichts zu verbessern."}.get(saved, "")
    body = f"""
<section class="hero"><div class="lab-eyebrow">WERKSTATT</div><h1>Prompts &amp; Anweisungen</h1>
<p>Alle Grundanweisungen von ASTRA sind hier editierbar, versioniert und rückrollbar. ASTRA darf
Verbesserungen <b>vorschlagen</b> — wirksam wird nichts, bevor du freigibst.</p></section>
{_flash("ok", msg)}
<div class="grid">{"".join(tiles)}</div>
<div class="section-head x-sec"><h2>Offene Vorschläge</h2><span class="count">{len(pending)}</span></div>
{"".join(_proposal_card(p, token, prompts.get(p["name"])) for p in pending) or '<p class="x-muted">Keine offenen Vorschläge.</p>'}"""
    return _html_with_csrf(_shell("Prompts", body, "prompts"), token)


@router.get("/admin/prompts/{name}", response_class=HTMLResponse)
async def prompt_edit_page(name: str, request: Request, _: bool = Depends(auth.require_admin),
                           saved: str = "", err: str = "", v: str = ""):
    if name not in prompts.NAMES:
        return RedirectResponse("/admin/prompts", status_code=303)
    token = await auth.issue_csrf()
    current = prompts.get(name)
    hist = prompts.history(name)
    hist_rows = "".join(
        f'<tr><td>{esc(h["version"])}</td><td>{esc(h["author"])}</td><td>{esc(h["note"])}</td>'
        f'<td class="num" style="white-space:nowrap"><a class="btn ghost sm" href="/admin/prompts/{name}?v={esc(h["version"])}">Diff</a> '
        f'<form method="post" action="/admin/prompts/{name}/rollback" style="display:inline"><input type="hidden" name="csrf" value="{esc(token)}">'
        f'<input type="hidden" name="version" value="{esc(h["version"])}"><button class="btn ghost sm" type="submit">Zurückrollen</button></form></td></tr>'
        for h in hist[:15])
    diff_box = ""
    if v:
        old = prompts.read_version(name, v)
        if old is not None:
            diff_box = (f'<div class="panel"><div class="section-head"><h2>Diff: Version {esc(v)} → aktuell</h2></div>'
                        f'<pre {_PRE}>{_diff_html(prompts.diff(old, current))}</pre></div>')
    pending = [p for p in prompts.proposals("pending") if p["name"] == name]
    placeholders = ", ".join("{" + p + "}" for p in prompts.PLACEHOLDERS.get(name, ())) or "keine"
    must = ", ".join(f"„{m}“" for m in prompts.MUST_KEEP.get(name, ())) or "keine festen Sätze"
    errors = "".join(f"<li>{esc(e)}</li>" for e in err.split("|") if e) if err else ""
    msg = {"saved": "Gespeichert — wirkt sofort.", "reset": "Auf Standard zurückgesetzt.",
           "rolled": "Zurückgerollt.", "approved": "Vorschlag freigegeben.", "rejected": "Vorschlag abgelehnt."}.get(saved, "")
    body = f"""
<section class="hero"><div class="lab-eyebrow">PROMPT</div><h1>{esc(prompts.LABELS[name])}</h1>
<p><a href="/admin/prompts">← Alle Prompts</a></p></section>
{_flash("ok", msg)}{f'<div class="flash err"><b>Nicht gespeichert:</b><ul>{errors}</ul></div>' if errors else ""}
<div class="panel"><form method="post" action="/admin/prompts/{name}">
  <input type="hidden" name="csrf" value="{esc(token)}">
  <textarea name="text" rows="18" style="width:100%;font:13px/1.5 ui-monospace,Menlo,monospace">{esc(current)}</textarea>
  <p class="x-muted">Pflicht-Platzhalter: {esc(placeholders)} · Muss enthalten bleiben: {esc(must)} · max. {prompts.MAX_LEN} Zeichen.</p>
  <div class="x-form"><div class="field"><input type="text" name="note" placeholder="Notiz zur Änderung (optional)" style="min-width:280px"></div>
  <button class="btn sm" type="submit">Speichern</button></div>
</form>
<div class="x-form" style="margin-top:10px">
  <form method="post" action="/admin/prompts/{name}/review"><input type="hidden" name="csrf" value="{esc(token)}"><button class="btn secondary sm" type="submit">ASTRA prüfen lassen</button></form>
  {f'<form method="post" action="/admin/prompts/{name}/reset" onsubmit="return confirm(\'Auf die Standardfassung zurücksetzen?\')"><input type="hidden" name="csrf" value="{esc(token)}"><button class="btn ghost danger sm" type="submit">Auf Standard zurücksetzen</button></form>' if prompts.is_overridden(name) else ""}
</div></div>
{diff_box}
{"".join(_proposal_card(p, token, current) for p in pending)}
<div class="section-head x-sec"><h2>Verlauf</h2><span class="count">{len(hist)}</span></div>
<div class="panel">{f'<table class="x-tbl"><thead><tr><th>Version</th><th>Von</th><th>Notiz</th><th></th></tr></thead><tbody>{hist_rows}</tbody></table>' if hist_rows else '<p class="x-muted">Noch keine früheren Fassungen.</p>'}</div>"""
    return _html_with_csrf(_shell(prompts.LABELS[name], body, "prompts"), token)


def _err_redirect(name: str, problems: list[str]) -> RedirectResponse:
    return RedirectResponse(f"/admin/prompts/{name}?err={quote('|'.join(problems)[:600])}", status_code=303)


@router.post("/admin/prompts/{name}")
async def prompt_save(name: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok or name not in prompts.NAMES:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    problems = prompts.save(name, str(form.get("text") or ""), author="owner", note=str(form.get("note") or "")[:120])
    if problems:
        return _err_redirect(name, problems)
    await db.audit("prompt_saved", actor="owner", detail={"name": name})
    return RedirectResponse(f"/admin/prompts/{name}?saved=saved", status_code=303)


@router.post("/admin/prompts/{name}/reset")
async def prompt_reset(name: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok or name not in prompts.NAMES:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    prompts.reset(name)
    await db.audit("prompt_reset", actor="owner", detail={"name": name})
    return RedirectResponse(f"/admin/prompts/{name}?saved=reset", status_code=303)


@router.post("/admin/prompts/{name}/rollback")
async def prompt_rollback(name: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok or name not in prompts.NAMES:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    problems = prompts.rollback(name, str(form.get("version") or ""))
    if problems:
        return _err_redirect(name, problems)
    await db.audit("prompt_rollback", actor="owner", detail={"name": name, "version": str(form.get("version"))})
    return RedirectResponse(f"/admin/prompts/{name}?saved=rolled", status_code=303)


@router.post("/admin/prompts/{name}/proposal")
async def prompt_proposal(name: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok or name not in prompts.NAMES:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    pid = str(form.get("pid") or "")
    prop = prompts.get_proposal(pid)
    if not prop or prop.get("name") != name:                 # Vorschlag muss zu diesem Baustein gehören
        return RedirectResponse(f"/admin/prompts/{name}", status_code=303)
    if form.get("do") == "approve":
        problems = prompts.approve(pid)
        if problems:
            return _err_redirect(name, problems)
        await db.audit("prompt_proposal_approved", actor="owner", detail={"name": name, "id": pid})
        return RedirectResponse(f"/admin/prompts/{name}?saved=approved", status_code=303)
    prompts.reject(pid)
    await db.audit("prompt_proposal_rejected", actor="owner", detail={"name": name, "id": pid})
    return RedirectResponse(f"/admin/prompts/{name}?saved=rejected", status_code=303)


@router.post("/admin/prompts/{name}/review")
async def prompt_review(name: str, request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok or name not in prompts.NAMES:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    res = await prompts.self_review(name)
    if res.get("error"):
        return _err_redirect(name, [str(res["error"])])
    if res.get("rejected"):
        return _err_redirect(name, ["ASTRAs Vorschlag wurde von den Leitplanken abgelehnt:"] + res["rejected"])
    return RedirectResponse(f"/admin/prompts{'/' + name if res.get('changed') else ''}?saved="
                            f"{'reviewed' if res.get('changed') else 'nochange'}", status_code=303)


# ══ Sicherheit, Secretary-Schalter, Kontext ═══════════════════════════════════
from .. import moderation, owner_commands  # noqa: E402

_STRICT_LABELS = {"strict": "Streng (empfohlen)", "normal": "Normal", "relaxed": "Locker"}


def _cb(name: str, label: str, on: bool, help_: str = "") -> str:
    h = f'<div class="help">{esc(help_)}</div>' if help_ else ""
    return (f'<div class="field"><label style="font-weight:500"><input type="checkbox" name="{name}" value="1"'
            f'{" checked" if on else ""}> {esc(label)}</label>{h}</div>')


def _num(name: str, label: str, value, step: str = "1", help_: str = "") -> str:
    h = f'<div class="help">{esc(help_)}</div>' if help_ else ""
    return (f'<div class="field"><label>{esc(label)}</label><input type="number" step="{step}" name="{name}" '
            f'value="{esc(str(value))}" style="min-width:90px">{h}</div>')


def _fmt_state_time(ts: float | None, tz: str) -> str:
    if not ts:
        return "—"
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromtimestamp(float(ts), ZoneInfo(tz)).strftime("%d.%m. %H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def describe_states(raw: dict, ladder: dict, now: float, tz: str = "Europe/Berlin") -> list[dict]:
    """`modstate:<kanal>:<handle>` → sortierbare Zeilen (rein). Nur Einträge mit Wirkung."""
    rows = []
    for key, state in (raw or {}).items():
        if not isinstance(state, dict) or not key.startswith("modstate:"):
            continue
        _, _, rest = key.partition(":")
        channel, _, handle = rest.partition(":")
        strikes = moderation.decay_strikes(state, now, float(ladder["decay_hours"]))
        muted = moderation.is_muted(state, now)
        if strikes < 0.05 and not muted:
            continue
        rows.append({"key": key, "channel": channel, "handle": handle, "strikes": round(strikes, 2),
                     "style": moderation.style_for(strikes, ladder), "muted": muted,
                     "muted_until": _fmt_state_time(state.get("muted_until"), tz),
                     "last": _fmt_state_time(state.get("last_ts"), tz)})
    return sorted(rows, key=lambda r: (not r["muted"], -r["strikes"]))


STYLE_NAMES = {"normal": "freundlich", "firm": "bestimmt", "arrogant": "überheblich"}


@router.get("/admin/safety", response_class=HTMLResponse)
async def safety_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    import time as _time
    from ..config import get_settings
    token = await auth.issue_csrf()
    appset = await _app_settings()
    tz = get_settings().astra_timezone
    cfg = moderation.settings(appset)
    lad = cfg["ladder"]
    try:
        states = describe_states(await db.settings_by_prefix("modstate:"), lad, _time.time(), tz)
    except Exception:  # noqa: BLE001
        states = []
    try:
        events = [e for e in await db.recent_audit(200)
                  if str(e.get("event_type", "")).startswith(("moderation", "security"))][:15]
    except Exception:  # noqa: BLE001
        events = []
    status = await owner_commands.status_text(appset, tz)
    ctx = appset.get("context") if isinstance(appset.get("context"), dict) else {}
    snapshot = models.model_config_snapshot()
    model_opts = {"": "Standard (Stufe klein)", **{o["value"]: o["label"] for o in model_choice.options(snapshot)
                                                    if o["value"] and not o["disabled"]}}
    switch = "".join(
        f'<form method="post" action="/admin/safety/secretary" style="display:inline">'
        f'<input type="hidden" name="csrf" value="{esc(token)}"><button class="btn {cls} sm" name="do" value="{val}" type="submit">{esc(label)}</button></form> '
        for label, val, cls in (("✅ An", "on", ""), ("🔄 Auto", "auto", "secondary"), ("🔕 Aus", "off", "secondary"),
                                ("⏸ 1 h Pause", "pause60", "ghost"), ("⏸ 3 h Pause", "pause180", "ghost"),
                                ("⏸ bis morgen", "pause_morgen", "ghost")))
    state_rows = "".join(
        f'<tr><td>{esc(r["channel"])}</td><td>{esc(r["handle"])}</td><td class="num">{r["strikes"]}</td>'
        f'<td>{esc(STYLE_NAMES.get(r["style"], r["style"]))}</td>'
        f'<td>{("🔇 bis " + esc(r["muted_until"])) if r["muted"] else "—"}</td><td class="x-muted">{esc(r["last"])}</td>'
        f'<td class="num"><form method="post" action="/admin/safety/reset" style="margin:0"><input type="hidden" name="csrf" value="{esc(token)}">'
        f'<input type="hidden" name="key" value="{esc(r["key"])}"><button class="btn ghost sm" type="submit">Zurücksetzen</button></form></td></tr>'
        for r in states)
    event_rows = "".join(
        f'<tr><td class="x-muted">{esc(e["ts"].astimezone().strftime("%d.%m. %H:%M") if hasattr(e["ts"], "astimezone") else str(e["ts"])[:16])}</td>'
        f'<td>{esc(str(e.get("event_type")))}</td><td>{esc(str(e.get("channel") or ""))}</td>'
        f'<td class="x-muted">{esc(", ".join((e.get("detail") or {}).get("categories", [])) if isinstance(e.get("detail"), dict) else "")}</td></tr>'
        for e in events)
    msg = {"mod": "Moderation gespeichert.", "ctx": "Kontext-Einstellungen gespeichert.", "reset": "Zurückgesetzt.",
           "sec": "Secretary-Schalter gesetzt.", "digest": "Zusammenfassung abgeschlossen."}.get(saved, "")
    body = f"""
<section class="hero"><div class="lab-eyebrow">SICHERHEIT</div><h1>Secretary, Moderation &amp; Kontext</h1>
<p>Schalter für den Secretary, Schutz vor Missbrauch (Eingang und Ausgang) und was ASTRA sich merkt.
Du selbst wirst nie moderiert.</p></section>
{_flash("ok", msg)}
<div class="panel"><div class="section-head"><h2>Secretary-Schalter</h2></div>
  <pre style="margin:0 0 12px;font:13px/1.5 inherit;white-space:pre-wrap">{esc(status)}</pre>{switch}
  <p class="x-muted" style="margin-top:8px">Auch per Telegram: „/secretary aus 2h“, „secretary bis 18 uhr aus“.</p></div>
<div class="section-head x-sec"><h2>Moderation</h2></div>
<form method="post" action="/admin/safety/moderation" class="panel">
  <input type="hidden" name="csrf" value="{esc(token)}">
  <div class="x-grid2">
    <div>
      {_cb("enabled", "Moderation aktiv", cfg["enabled"], "Prüft alle Nachrichten Dritter, bevor das Modell etwas kostet.")}
      <div class="field"><label>Strenge</label>{_select("strictness", _STRICT_LABELS, cfg["strictness"])}</div>
      {_cb("llm", "Kostenlose OpenAI-Moderation als Zweitmeinung", cfg["llm"], "Nur mit OpenAI-Key; bei Ausfall wird ignoriert, nicht blockiert.")}
      {_cb("block_code", "Code-Anfragen abwehren", cfg["block_code"], "Schützt deine API vor „schreib mir ein Skript“.")}
      {_cb("block_free_llm", "Gratis-KI-Nutzung abwehren", cfg["block_free_llm"], "Hausaufgaben, Aufsätze, Übersetzungen …")}
      {_cb("alert_owner", "Mich bei schweren Fällen benachrichtigen", cfg["alert_owner"])}
      {_num("max_inbound_chars", "Max. Länge eingehender Nachrichten", cfg["max_inbound_chars"])}
    </div>
    <div>
      <b>Ausgehende Antworten</b>
      {_cb("out_strip_urls", "Links entfernen", cfg["out_strip_urls"])}
      {_cb("out_strip_pii", "Fremde Telefonnummern/Mails/Zugangsdaten entfernen", cfg["out_strip_pii"])}
      {_cb("out_block_code", "Code-Blöcke nie senden", cfg["out_block_code"])}
      {_num("out_max_chars", "Max. Länge einer Antwort", cfg["out_max_chars"])}
    </div>
  </div>
  <div class="section-head x-sec"><h2>Eskalations-Leiter</h2></div>
  <p class="x-muted">Jeder Verstoß gibt Punkte (lästig 0,5 · Missbrauch 1 · schwer 3). Die Punkte klingen ab.</p>
  <div class="x-form">
    {_num("ladder_firm_at", "Bestimmt ab", lad["firm_at"], "0.5")}{_num("ladder_arrogant_at", "Überheblich ab", lad["arrogant_at"], "0.5")}
    {_num("ladder_mute_at", "Stumm ab", lad["mute_at"], "0.5")}{_num("ladder_mute_hours", "Stumm für (Std.)", lad["mute_hours"])}
    {_num("ladder_decay_hours", "Abklingen: Halbierung alle (Std.)", lad["decay_hours"])}
  </div>
  <div class="field" style="margin-top:12px"><label>Eigene Sperrwörter (Komma oder Zeilenumbruch)</label>
    <textarea name="custom_block_words" rows="2" style="width:100%">{esc(", ".join(cfg["custom_block_words"]))}</textarea></div>
  <button class="btn sm" type="submit">Speichern</button>
</form>
<div class="section-head x-sec"><h2>Gestufte &amp; stummgeschaltete Kontakte</h2><span class="count">{len(states)}</span></div>
<div class="panel">{f'<table class="x-tbl"><thead><tr><th>Kanal</th><th>Kennung</th><th class="num">Punkte</th><th>Ton</th><th>Stumm</th><th>Zuletzt</th><th></th></tr></thead><tbody>{state_rows}</tbody></table>' if state_rows else '<p class="x-muted">Niemand ist aktuell gestuft. So soll es sein.</p>'}</div>
<div class="section-head x-sec"><h2>Letzte Sicherheitsereignisse</h2></div>
<div class="panel">{f'<table class="x-tbl"><thead><tr><th>Zeit</th><th>Ereignis</th><th>Kanal</th><th>Kategorien</th></tr></thead><tbody>{event_rows}</tbody></table>' if event_rows else '<p class="x-muted">Keine Ereignisse.</p>'}</div>
<div class="section-head x-sec"><h2>Kontext-Gedächtnis</h2></div>
<form method="post" action="/admin/safety/context" class="panel">
  <input type="hidden" name="csrf" value="{esc(token)}">
  <div class="x-form">
    {_num("retention_days", "Rohnachrichten aufbewahren (Tage, 0 = alles)", ctx.get("retention_days", 0), help_="Zusammenfassungen mit wörtlichen Zitaten bleiben bestehen.")}
    <div class="field"><label>Modell für Zusammenfassungen</label>{_select("model", model_opts, model_choice.encode(ctx.get("model")))}</div>
    <button class="btn sm" type="submit">Speichern</button>
  </div>
</form>
<form method="post" action="/admin/safety/digest" style="margin-top:10px"><input type="hidden" name="csrf" value="{esc(token)}">
  <button class="btn secondary sm" type="submit">Jetzt alle zusammenfassen</button>
  <span class="x-muted">Läuft sonst jede Nacht um 03:30.</span></form>"""
    return _html_with_csrf(_shell("Sicherheit", body, "safety"), token)


@router.post("/admin/safety/moderation")
async def safety_moderation_save(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    appset = await _app_settings()
    appset["moderation"] = moderation.settings_from_form(form)
    await db.set_setting("app_settings", appset)
    await db.audit("moderation_settings_saved", actor="owner", detail={"enabled": appset["moderation"]["enabled"]})
    return RedirectResponse("/admin/safety?saved=mod", status_code=303)


@router.post("/admin/safety/context")
async def safety_context_save(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    try:
        days = max(0, min(3650, int(float(str(form.get("retention_days") or "0").replace(",", ".")))))
    except ValueError:
        days = 0
    appset = await _app_settings()
    appset["context"] = {**(appset.get("context") or {}), "retention_days": days,
                         "model": model_choice.decode(str(form.get("model") or ""))}
    await db.set_setting("app_settings", appset)
    await db.audit("context_settings_saved", actor="owner", detail={"retention_days": days})
    return RedirectResponse("/admin/safety?saved=ctx", status_code=303)


@router.post("/admin/safety/reset")
async def safety_reset(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    key = str(form.get("key") or "")
    if key.startswith("modstate:"):                      # nur Moderations-Zustände, nie beliebige Einstellungen
        await db.set_setting(key, {})
        await db.audit("moderation_reset", actor="owner", detail={"key": key})
    return RedirectResponse("/admin/safety?saved=reset", status_code=303)


@router.post("/admin/safety/secretary")
async def safety_secretary(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    do = str(form.get("do") or "")
    cmd = owner_commands.command_from_callback("sec:" + do)
    if do == "pause_morgen":
        cmd = owner_commands.Command("off", tomorrow=True)
    if cmd is not None:
        from ..config import get_settings
        await owner_commands.execute(cmd, timezone=get_settings().astra_timezone)
    return RedirectResponse("/admin/safety?saved=sec", status_code=303)


@router.post("/admin/safety/digest")
async def safety_digest(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _csrf_form(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)
    await digest.digest_all()
    return RedirectResponse("/admin/safety?saved=digest", status_code=303)
