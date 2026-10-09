"""Admin-Seite „Display“: OpenBoard-Wanddisplay verbinden (URL + Token), Stimme,
Morgen-Briefing und Wecker. Gleiche Auth-/CSRF-Regeln wie der Rest des Admins."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import briefing, db
from ..config import get_settings
from ..display import service as display
from ..display import speech
from ..display.hub import get_hub
from . import auth
from .admin import _check_csrf, _html_with_csrf
from .admin_extra import _X_CSS, _cb, _flash, _select
from .templates import esc, page

log = logging.getLogger("astra.web.display")
router = APIRouter()

_DAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_MSG = {
    "token": "Neuer Token erzeugt — in OpenBoard eintragen, der alte gilt nicht mehr.",
    "settings": "Gespeichert.", "plugin": "Werkzeuge umgeschaltet.", "alarm": "Wecker gestellt.",
    "deleted": "Wecker gelöscht.", "test": "Testnachricht gesendet.", "notest": "Kein Display verbunden.",
    "bad": "Ungültige Eingabe.",
}


async def _form(request: Request):
    form = await request.form()
    return form, await _check_csrf(request, form)


def _csrf_fail() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "CSRF-Prüfung fehlgeschlagen."}, status_code=403)


def _plugin_cls():
    from ..plugins.registry import get_manager
    mgr = get_manager()
    return mgr.plugin_class("display"), mgr.get("display")


async def _alarm_rules() -> list[dict]:
    try:
        rules = await db.list_rules()
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rules if display._is_alarm_rule(r)]


def _next_at(upcoming: dict, rule_id: int) -> str:
    return str(upcoming.get(f"rule-{rule_id}", {}).get("at", ""))[:16].replace("T", " ")


def _when(trigger: dict) -> str:
    at = trigger.get("at", "?")
    if trigger.get("date"):
        try:
            return f"{datetime.fromisoformat(trigger['date']):%d.%m.%Y} · {at}"
        except ValueError:
            return f"{trigger['date']} · {at}"
    days = trigger.get("days") or []
    label = "täglich" if not days or len(days) == 7 else (
        "werktags" if sorted(days) == [0, 1, 2, 3, 4] else ", ".join(_DAYS[int(d)] for d in sorted(days)))
    return f"{label} · {at}"


@router.get("/admin/display", response_class=HTMLResponse)
async def display_page(request: Request, _: bool = Depends(auth.require_admin), saved: str = ""):
    token_csrf = await auth.issue_csrf()
    tok = await display.display_token()
    from_env = display.token_source() == "env"
    hub = get_hub()
    cfg = await display.display_settings()
    voice = await speech.voice_settings()
    bset = await briefing.briefing_settings()
    s = get_settings()
    _cls, plugin = _plugin_cls()
    tools_on = bool(plugin and plugin.enabled)
    base = str(request.base_url).rstrip("/")
    alarms = await _alarm_rules()
    upcoming = {a["id"]: a for a in await display.upcoming_alarms()}

    clients = "".join(
        f'<tr><td>#{c["id"]}</td><td>{esc(c["peer"])}</td><td class="x-muted">{esc(c["agent"] or "—")}</td>'
        f'<td>{datetime.fromtimestamp(c["since"]):%d.%m. %H:%M}</td></tr>' for c in hub.clients())
    status = (f'<span class="x-pill" style="color:#a7f3d0">● {hub.connected} verbunden</span>' if hub.connected
              else '<span class="x-pill">○ kein Display verbunden</span>')
    csrf_in = f'<input type="hidden" name="csrf" value="{esc(token_csrf)}">'
    regen = ('<p class="x-muted">Der Token kommt aus <code>ASTRA_DISPLAY_TOKEN</code> (.env) und wird dort geändert.</p>'
             if from_env else
             f'<form method="post" action="/admin/display/token" style="margin-top:10px" '
             f'onsubmit="return confirm(\'Neuen Token erzeugen? Das Display muss dann neu verbunden werden.\')">'
             f'{csrf_in}<button class="btn secondary sm" type="submit">Neuen Token erzeugen</button></form>')
    alarm_rows = "".join(
        f'<tr><td>#{r["id"]}</td><td>{esc(display._alarm_label(r))}</td><td>{esc(_when(r.get("trigger") or {}))}</td>'
        f'<td>{"aktiv" if (r["enabled"] and r["confirmed_at"]) else ("wartet auf Freigabe" if not r["confirmed_at"] else "aus")}</td>'
        f'<td class="x-muted">{esc(_next_at(upcoming, r["id"]))}</td>'
        f'<td class="num"><form method="post" action="/admin/display/alarm/delete" style="margin:0">{csrf_in}'
        f'<input type="hidden" name="id" value="{r["id"]}"><button class="btn ghost sm" type="submit">Löschen</button>'
        f'</form></td></tr>' for r in alarms)
    day_boxes = "".join(
        f'<label style="font-weight:500;margin-right:10px"><input type="checkbox" name="day{i}" value="1"'
        f'{" checked" if i < 5 else ""}> {d}</label>' for i, d in enumerate(_DAYS))
    voices = {v: v + (" (Standard)" if v == "nova" else "") for v in speech.VOICES}

    body = f"""
<section class="hero"><div class="lab-eyebrow">DISPLAY</div><h1>OpenBoard-Display</h1>
<p>ASTRA auf deinem Wand-Touch-Display: Gespräch per Sprache oder Touch, Karten für Termine, Wetter und
Smart Home, Whiteboard, Wecker und Morgen-Briefing. Die Verbindung läuft nur im Heimnetz.</p></section>
{_flash("ok" if saved not in ("bad", "notest") else "err", _MSG.get(saved, ""))}
<div class="x-grid2">
<div class="panel"><div class="section-head"><h2>Verbindung</h2>{status}</div>
  <p class="x-muted">In OpenBoard unter <b>Einstellungen → ASTRA</b> eintragen:</p>
  <div class="field"><label>ASTRA-URL</label><input type="text" readonly value="{esc(base)}" onclick="this.select()"></div>
  <div class="field"><label>Token</label>
    <div style="display:flex;gap:8px"><input id="dtok" type="password" readonly value="{esc(tok)}" style="flex:1">
    <button class="btn ghost sm" type="button" onclick="const i=document.getElementById('dtok');i.type=i.type==='password'?'text':'password'">Anzeigen</button>
    <button class="btn ghost sm" type="button" onclick="navigator.clipboard.writeText(document.getElementById('dtok').value);this.textContent='Kopiert'">Kopieren</button></div></div>
  {regen}
  <p class="x-muted" style="margin-top:12px">Die URL ist die LAN-Adresse von ASTRA (Port 8088). <code>/display/*</code>
  ist bewusst nicht über Caddy/Internet erreichbar.</p>
  {f'<table class="x-tbl" style="margin-top:10px"><thead><tr><th>#</th><th>Adresse</th><th>Client</th><th>Seit</th></tr></thead><tbody>{clients}</tbody></table>' if clients else ''}
  <form method="post" action="/admin/display/test" style="margin-top:12px">{csrf_in}
    <button class="btn secondary sm" type="submit" name="kind" value="say">Testnachricht</button>
    <button class="btn ghost sm" type="submit" name="kind" value="alarm">Wecker testen</button></form>
</div>
<div class="panel"><div class="section-head"><h2>ASTRA-Werkzeuge</h2>
  <span class="x-pill">{"aktiv" if tools_on else "aus"}</span></div>
  <p class="x-muted">Damit ASTRA von sich aus Karten zeigt, spricht, Apps öffnet, aufs Whiteboard zeichnet und
  Wecker stellt (Plugin <a href="/admin/plugin/display">OpenBoard Display</a>). Bilder erzeugen kann das Plugin
  <a href="/admin/plugin/image_generation">Bildgenerierung</a>.</p>
  <form method="post" action="/admin/display/plugin">{csrf_in}
    <button class="btn sm" type="submit" name="enabled" value="{"0" if tools_on else "1"}">{"Ausschalten" if tools_on else "Einschalten"}</button></form>
</div>
</div>
<div class="section-head x-sec"><h2>Stimme &amp; Briefing</h2></div>
<form method="post" action="/admin/display/settings" class="panel">{csrf_in}
  <div class="x-grid2"><div>
    <div class="field"><label>Stimme</label>{_select("tts_voice", voices, voice["voice"])}</div>
    <div class="field"><label>TTS-Modell</label><input type="text" name="tts_model" value="{esc(voice["model"])}"></div>
    <div class="field"><label>Sprechweise (optional)</label><textarea name="tts_instructions" rows="2" style="width:100%"
      placeholder="{esc(speech.DEFAULT_INSTRUCTIONS)}">{esc(cfg.get("tts_instructions") or "")}</textarea></div>
    {_cb("notify", "Meldungen auch aufs Display (wenn wach & zu Hause)", bool(cfg.get("notify", True)))}
    {"" if speech.available() else '<p class="x-warn">Kein OpenAI-Key — das Display zeigt dann nur Text.</p>'}
  </div><div>
    <div class="field"><label>Briefing-Uhrzeit</label><input type="time" name="briefing_time" value="{esc(bset["time"])}"></div>
    {_cb("display_briefing", "Morgen-Briefing aufs Display (Karten + Sprache)", bset["display"])}
    {_cb("telegram_briefing", "Morgen-Briefing per Telegram", bset["telegram"],
         "" if s.telegram_enabled else "Telegram ist nicht konfiguriert.")}
  </div></div>
  <button class="btn sm" type="submit">Speichern</button>
</form>
<div class="section-head x-sec"><h2>Wecker</h2><span class="count">{len(alarms)}</span></div>
<div class="panel">
  {f'<table class="x-tbl"><thead><tr><th>#</th><th>Name</th><th>Wann</th><th>Status</th><th>Nächster</th><th></th></tr></thead><tbody>{alarm_rows}</tbody></table>' if alarm_rows else '<p class="x-muted">Noch kein Wecker. Sag ASTRA einfach „Weck mich morgen um 7“ — oder stell ihn hier.</p>'}
  <form method="post" action="/admin/display/alarm" class="x-form" style="margin-top:14px">{csrf_in}
    <div class="field"><label>Uhrzeit</label><input type="time" name="time" value="07:00" required></div>
    <div class="field"><label>Name</label><input type="text" name="label" value="Aufstehen"></div>
    <div class="field"><label>Klang</label>{_select("sound", {"gentle": "sanft", "classic": "klassisch", "none": "ohne"}, "gentle")}</div>
    <div class="field"><label>Tage</label><div>{day_boxes}</div></div>
    {_cb("briefing", "danach Briefing vorlesen", True)}
    <button class="btn sm" type="submit">Wecker stellen</button>
  </form>
  <p class="x-muted" style="margin-top:8px">Klingelt nur, wenn ein Display verbunden ist — sonst kommt er als dringende Meldung.</p>
</div>"""
    return _html_with_csrf(page("Display", _X_CSS + body, active="display"), token_csrf)


@router.post("/admin/display/token")
async def display_token_regen(request: Request, _: bool = Depends(auth.require_admin)):
    _form_data, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    try:
        await display.regenerate_token()
    except RuntimeError:
        return RedirectResponse("/admin/display?saved=bad", status_code=303)
    return RedirectResponse("/admin/display?saved=token", status_code=303)


@router.post("/admin/display/settings")
async def display_settings_save(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    voice = str(form.get("tts_voice") or "nova")
    patch = {
        "tts_voice": voice if voice in speech.VOICES else "nova",
        "tts_model": str(form.get("tts_model") or "").strip()[:60],
        "tts_instructions": str(form.get("tts_instructions") or "").strip()[:500],
        "display_briefing": form.get("display_briefing") == "1",
        "notify": form.get("notify") == "1",
    }
    await display.save_display_settings(patch)
    at = str(form.get("briefing_time") or "").strip()
    appset = await db.get_setting("app_settings", {}) or {}
    b = appset.get("briefing") if isinstance(appset.get("briefing"), dict) else {}
    b["telegram"] = form.get("telegram_briefing") == "1"
    if briefing.valid_hhmm(at):
        b["time"] = at
    appset["briefing"] = b
    await db.set_setting("app_settings", appset)
    await db.audit("display_settings_saved", actor="owner",
                   detail={**{k: v for k, v in patch.items() if k != "tts_instructions"}, "briefing": b})
    return RedirectResponse("/admin/display?saved=settings", status_code=303)


@router.post("/admin/display/plugin")
async def display_plugin_toggle(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    from ..config_store import get_config_store
    from ..plugins.registry import get_manager
    cls, _plugin = _plugin_cls()
    if cls is None:
        return RedirectResponse("/admin/display?saved=bad", status_code=303)
    await get_config_store().save(cls, {}, form.get("enabled") == "1")
    await get_manager().rebuild()
    try:
        from ..admin_tools import register_admin_tools
        register_admin_tools()
    except Exception:  # noqa: BLE001
        log.debug("admin tools re-register failed", exc_info=True)
    return RedirectResponse("/admin/display?saved=plugin", status_code=303)


@router.post("/admin/display/alarm")
async def display_alarm_add(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    at = str(form.get("time") or "").strip()
    if not briefing.valid_hhmm(at):
        return RedirectResponse("/admin/display?saved=bad", status_code=303)
    hh, mm = at.split(":")
    at = f"{int(hh):02d}:{mm}"
    days = [i for i in range(7) if form.get(f"day{i}") == "1"] or list(range(7))
    rule = display.alarm_rule(at=at, label=str(form.get("label") or "Wecker").strip()[:80] or "Wecker",
                              days=days, sound=str(form.get("sound") or "gentle"),
                              briefing=form.get("briefing") == "1")
    rid = await db.add_rule(name=rule["name"], trigger=rule["trigger"], condition=rule["condition"],
                            actions=rule["actions"], plugin_slug="display", created_by="owner", confirmed=True)
    await db.audit("rule_created", actor="owner", detail={"id": rid, "name": rule["name"], "confirmed": True})
    return RedirectResponse("/admin/display?saved=alarm", status_code=303)


@router.post("/admin/display/alarm/delete")
async def display_alarm_delete(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    try:
        rid = int(str(form.get("id") or "0"))
    except ValueError:
        rid = 0
    rule = await db.get_rule(rid) if rid else None
    if rule and display._is_alarm_rule(rule):        # nur Wecker, nie beliebige Regeln
        await db.delete_rule(rid)
        await db.audit("rule_deleted", actor="owner", detail={"id": rid})
    return RedirectResponse("/admin/display?saved=deleted", status_code=303)


@router.post("/admin/display/test")
async def display_test(request: Request, _: bool = Depends(auth.require_admin)):
    form, ok = await _form(request)
    if not ok:
        return _csrf_fail()
    if not display.connected():
        return RedirectResponse("/admin/display?saved=notest", status_code=303)
    if form.get("kind") == "alarm":
        payload = await display.build_alarm({"label": "Testwecker", "sound": "gentle",
                                             "speak": "Das ist ein Test. Guten Morgen!"}, alarm_id="alarm-test")
        display.publish("alarm", payload)
    else:
        text = f"Hallo {get_settings().astra_owner_name}, hier ist ASTRA. Die Verbindung steht."
        payload = {"text": text}
        if sp := await speech.synthesize(text):
            payload["speech"] = sp
        display.publish("say", payload)
    return RedirectResponse("/admin/display?saved=test", status_code=303)
