"""OpenBoard Display — ASTRA auf dem Wand-Touch-Display (Karten, Sprache, Whiteboard, Wecker).

Die Verbindung selbst (Token, /display/v1/*, SSE) lebt in `app/display`; dieses Plugin
gibt ASTRA die Werkzeuge, um das Display aktiv zu bespielen. Kommt ein Turn vom
Display selbst (channel="display"), landen Karten und Aktionen direkt in der Antwort
statt doppelt über den Ereignisstrom.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ...config import get_settings
from ...display import cards as cardlib
from ...display import service as display
from ...display import speech
from ...tools import Tool, ToolContext, tool_result
from ..base import HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.display")

_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_DATA_HELP = (
    "markdown {text} · list {items:[{title, detail?, meta?, icon?}]} · facts {rows:[{label, value}]} · "
    "sketch {svg} (reines SVG, keine Skripte) · image {src (data:image/… | https://… | astra-file:…), alt, "
    "caption?} · calendar {events:[{title, start, end, all_day}]} · home {entities:[{name, state, unit?, "
    "domain}]} · alarm {at, label}"
)


def _on_display(ctx: ToolContext) -> bool:
    return ctx.channel == "display"


def _no_display() -> str:
    return tool_result(ok=False, source="display",
                       summary="Kein Display verbunden. OpenBoard braucht URL + Token aus Admin → Display.")


def _parse_day(raw: str, today: date) -> date | None:
    v = str(raw or "").strip().lower()
    if not v:
        return None
    if v in ("heute", "today"):
        return today
    if v in ("morgen", "tomorrow"):
        return today + timedelta(days=1)
    if v in ("übermorgen", "uebermorgen"):
        return today + timedelta(days=2)
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


class DisplayPlugin(Plugin):
    slug = "display"
    name = "OpenBoard Display"
    description = ("ASTRA auf dem Wand-Touch-Display: Karten, Sprachausgabe, Whiteboard, Wecker "
                   "und Morgen-Briefing. Verbindung (URL + Token) unter Admin → Display.")
    category = PluginCategory.SMART_HOME
    icon = "📺"
    config_fields = []

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        n = display.connected()
        if n:
            return HealthStatus.ok(f"{n} Display{'s' if n != 1 else ''} verbunden.")
        return HealthStatus.ok("Bereit — noch kein Display verbunden (URL + Token unter Admin → Display).")

    def rule_templates(self) -> list[dict]:
        tmpl = display.alarm_rule(at="07:00", label="Aufstehen", days=[0, 1, 2, 3, 4], briefing=True)
        tmpl["name"] = "Wecker"
        return [tmpl]

    # ── Werkzeuge ────────────────────────────────────────────────────────────
    def tools(self) -> list[Tool]:
        src = self.slug

        async def _show(args: dict, ctx: ToolContext) -> str:
            replace = bool(args.get("replace"))
            raw = args.get("cards") or args.get("card") or {
                k: v for k, v in args.items() if k in ("id", "type", "title", "subtitle", "data", "ttl_seconds")}
            raw_list = raw if isinstance(raw, list) else [raw]
            try:
                cards = [cardlib.validate_card(c) for c in raw_list]
            except cardlib.CardError as e:
                return tool_result(ok=False, source=src, summary=f"Karte ungültig: {e}")
            titles = ", ".join(c.get("title") or c["type"] for c in cards)
            if _on_display(ctx):
                return tool_result(ok=True, source=src, summary=f"Wird angezeigt: {titles}.",
                                   data={"display": {"cards": cards}})
            if not display.connected():
                return _no_display()
            try:
                if len(cards) == 1 and not replace:
                    display.publish("card", {"card": cards[0]})
                else:
                    display.publish("cards", {"cards": cards, "replace": replace})
            except cardlib.CardError as e:
                return tool_result(ok=False, source=src, summary=f"Karte ungültig: {e}")
            return tool_result(ok=True, source=src, summary=f"Auf dem Display angezeigt: {titles}.",
                               data={"cards": [{"id": c["id"], "type": c["type"]} for c in cards]})

        async def _say(args: dict, ctx: ToolContext) -> str:
            text = str(args.get("text") or "").strip()
            if not text:
                return tool_result(ok=False, source=src, summary="Kein Text zum Sprechen.")
            if not display.connected():
                return _no_display()
            payload = {"text": text}
            if (sp := await speech.synthesize(text)) is not None:
                payload["speech"] = sp
            display.publish("say", payload)
            how = "gesprochen" if "speech" in payload else "angezeigt (ohne Sprachausgabe)"
            return tool_result(ok=True, source=src, summary=f"Auf dem Display {how}: „{text[:80]}“")

        async def _command(action: str, app: str | None, ctx: ToolContext, done: str) -> str:
            try:
                cmd = display.command_payload(action, app)
            except cardlib.CardError as e:
                return tool_result(ok=False, source=src, summary=str(e))
            if _on_display(ctx):
                act = {"type": cmd["action"], **({"app": cmd["app"]} if "app" in cmd else {})}
                return tool_result(ok=True, source=src, summary=done, data={"display": {"actions": [act]}})
            if not display.connected():
                return _no_display()
            display.publish("command", cmd)
            return tool_result(ok=True, source=src, summary=done)

        async def _power(args: dict, ctx: ToolContext) -> str:
            action = str(args.get("action") or "").strip().lower()
            action = {"aus": "sleep", "off": "sleep", "an": "wake", "on": "wake"}.get(action, action)
            done = "Display schläft." if action == "sleep" else "Display ist wach."
            return await _command(action, None, ctx, done)

        async def _open_app(args: dict, ctx: ToolContext) -> str:
            app = str(args.get("app") or "").strip()
            return await _command("open_app", app, ctx, f"App „{app}“ auf dem Display geöffnet.")

        async def _board(args: dict, ctx: ToolContext) -> str:
            try:
                op = cardlib.validate_board_op(args)
            except cardlib.CardError as e:
                return tool_result(ok=False, source=src, summary=f"Board-Operation ungültig: {e}")
            if not display.connected():
                return _no_display()
            try:
                display.publish("board", op)
            except cardlib.CardError as e:
                return tool_result(ok=False, source=src, summary=str(e))
            return tool_result(ok=True, source=src, summary=f"Aufs Whiteboard gelegt ({op['op']}).")

        async def _alarm_now(args: dict, ctx: ToolContext) -> str:
            if not display.connected():
                return _no_display()
            stamp = datetime.now(ZoneInfo(get_settings().astra_timezone)).strftime("%H%M%S")
            payload = await display.build_alarm(args, alarm_id=f"alarm-now-{stamp}")
            display.publish("alarm", payload)
            return tool_result(ok=True, source=src, summary=f"Wecker „{payload['label']}“ klingelt jetzt.")

        async def _alarm_set(args: dict, ctx: ToolContext) -> str:
            from ... import db
            from ...admin_tools import _writes_allowed
            if not await _writes_allowed(ctx):
                return tool_result(ok=False, source=src, summary="Regeln anlegen ist deaktiviert (allow_self_config).")
            at = str(args.get("time") or args.get("at") or "").strip()
            m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", at)
            if not m:
                return tool_result(ok=False, source=src, summary="time muss HH:MM sein, z. B. 07:00.")
            at = f"{int(m.group(1)):02d}:{m.group(2)}"
            tz = ZoneInfo(get_settings().astra_timezone)
            now = datetime.now(tz)
            days = [int(d) for d in (args.get("days") or []) if str(d).lstrip("-").isdigit() and 0 <= int(d) <= 6]
            day = _parse_day(args.get("date") or "", now.date())
            if args.get("date") and day is None:
                return tool_result(ok=False, source=src, summary="date muss YYYY-MM-DD, heute oder morgen sein.")
            if day is None and not days:
                # „Weck mich um 7“ ohne Tag: das nächste 7:00 — heute, sonst morgen.
                h, mi = (int(x) for x in at.split(":"))
                day = now.date() if (now.hour, now.minute) < (h, mi) else now.date() + timedelta(days=1)
            if day is not None and day < now.date():
                return tool_result(ok=False, source=src, summary="Das Datum liegt in der Vergangenheit.")
            label = str(args.get("label") or "Wecker").strip()[:80]
            rule = display.alarm_rule(at=at, label=label, days=None if day else days,
                                      date=day.isoformat() if day else "",
                                      sound=str(args.get("sound") or "gentle"),
                                      briefing=bool(args.get("briefing", True)),
                                      speak=str(args.get("speak") or ""))
            rule_id = await db.add_rule(
                name=rule["name"], trigger=rule["trigger"], condition=rule["condition"],
                actions=rule["actions"], plugin_slug=src, principal_key=ctx.principal or "",
                created_by="owner" if ctx.is_owner else "astra", confirmed=bool(ctx.is_owner))
            await db.audit("rule_created", actor="astra",
                           detail={"id": rule_id, "name": rule["name"], "confirmed": bool(ctx.is_owner)})
            when = (f"{_WEEKDAYS[day.weekday()]} {day:%d.%m.}" if day
                    else ", ".join(_WEEKDAYS[d] for d in sorted(set(days))))
            hint = "" if display.connected() else " (Hinweis: gerade ist kein Display verbunden — sonst kommt er als Meldung.)"
            state = "" if ctx.is_owner else " — wartet auf deine Freigabe (astra_rule_confirm)"
            return tool_result(ok=True, source=src,
                               summary=f"Wecker #{rule_id} gestellt: {when} {at} · {label}{state}.{hint}",
                               data={"rule_id": rule_id, "trigger": rule["trigger"]})

        async def _settings(args: dict, ctx: ToolContext) -> str:
            from ... import briefing
            patch: dict = {}
            if args.get("tts_voice"):
                if args["tts_voice"] not in speech.VOICES:
                    return tool_result(ok=False, source=src,
                                       summary=f"Stimme unbekannt. Möglich: {', '.join(speech.VOICES)}.")
                patch["tts_voice"] = args["tts_voice"]
            if args.get("tts_model"):
                patch["tts_model"] = str(args["tts_model"]).strip()[:60]
            for key in ("display_briefing", "notify"):
                if args.get(key) is not None:
                    patch[key] = bool(args[key])
            new_time = str(args.get("briefing_time") or "").strip()
            if patch or new_time:
                from ...admin_tools import _writes_allowed
                if not await _writes_allowed(ctx):
                    return tool_result(ok=False, source=src, summary="Einstellungen ändern ist deaktiviert.")
            if new_time:
                if not briefing.valid_hhmm(new_time):
                    return tool_result(ok=False, source=src, summary="briefing_time muss HH:MM sein.")
                from ... import db
                appset = await db.get_setting("app_settings", {}) or {}
                b = appset.get("briefing") if isinstance(appset.get("briefing"), dict) else {}
                appset["briefing"] = {**b, "time": new_time}
                await db.set_setting("app_settings", appset)
            cfg = await (display.save_display_settings(patch) if patch else display.display_settings())
            voice = await speech.voice_settings()
            bset = await briefing.briefing_settings()
            summary = (f"Display: {display.connected()} verbunden · Stimme {voice['voice']} ({voice['model']}) · "
                       f"Briefing {bset['time']} (Display {'an' if bset['display'] else 'aus'}, "
                       f"Telegram {'an' if bset['telegram'] else 'aus'}) · Meldungen aufs Display "
                       f"{'an' if cfg.get('notify', True) else 'aus'}")
            return tool_result(ok=True, source=src, summary=("Gespeichert. " if patch or new_time else "") + summary)

        ctl = ["control"]
        return [
            Tool(name="display_show",
                 description=("Zeige eine Karte auf Bahrians Wand-Display (OpenBoard). type: markdown, list, facts, "
                              "sketch, image, calendar, home, alarm. data je Typ: " + _DATA_HELP + ". "
                              "replace=true leert vorher den Bildschirm. Kalender/Wetter/Smart-Home-Ergebnisse "
                              "erscheinen von selbst — nutze das hier für eigene Erklärungen, Listen, Skizzen."),
                 parameters={"type": "object", "properties": {
                     "type": {"type": "string", "enum": list(cardlib.CARD_TYPES)},
                     "title": {"type": "string"}, "subtitle": {"type": "string"},
                     "data": {"type": "object", "description": _DATA_HELP},
                     "ttl_seconds": {"type": "integer", "description": "Nach so vielen Sekunden ausblenden"},
                     "replace": {"type": "boolean"}},
                     "required": ["type", "data"]},
                 handler=_show, owner_only=True, source=src, safety="auto", intents=ctl,
                 examples=["Zeig mir die Einkaufsliste auf dem Display", "Skizzier mir einen Spannungsteiler"]),
            Tool(name="display_say",
                 description="Sprich einen Text laut auf dem Wand-Display (TTS) und zeige ihn an.",
                 parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                 handler=_say, owner_only=True, source=src, safety="auto", intents=ctl),
            Tool(name="display_power",
                 description="Wand-Display schlafen legen oder aufwecken. action=sleep|wake.",
                 parameters={"type": "object", "properties": {
                     "action": {"type": "string", "enum": ["sleep", "wake"]}}, "required": ["action"]},
                 handler=_power, owner_only=True, source=src, safety="auto", intents=ctl),
            Tool(name="display_open_app",
                 description=("Öffne eine App auf dem Wand-Display. app = App-ID, z. B. board (Whiteboard), "
                              "astra, home (Home Assistant), gev (God's Eye View), settings."),
                 parameters={"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]},
                 handler=_open_app, owner_only=True, source=src, safety="auto", intents=ctl),
            Tool(name="display_board",
                 description=("Lege etwas aufs Whiteboard des Displays. op: add_text {text, x?, y?} · add_mermaid "
                              "{definition} (Diagramme) · add_image {src, caption?} (auch astra-file:… aus "
                              "generate_image) · add_svg {svg} · add_elements {elements: Excalidraw-Skeletons}."),
                 parameters={"type": "object", "properties": {
                     "op": {"type": "string", "enum": list(cardlib.BOARD_OPS)},
                     "text": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
                     "definition": {"type": "string"}, "src": {"type": "string"},
                     "caption": {"type": "string"}, "svg": {"type": "string"},
                     "elements": {"type": "array", "items": {"type": "object"}}},
                     "required": ["op"]},
                 handler=_board, owner_only=True, source=src, safety="auto", intents=ctl),
            Tool(name="display_alarm_now",
                 description=("Lass den Wecker auf dem Display SOFORT klingeln (Test). label, sound=gentle|classic|"
                              "none, speak=Text zum Vorlesen, briefing=true hängt das Kurz-Briefing an."),
                 parameters={"type": "object", "properties": {
                     "label": {"type": "string"}, "sound": {"type": "string", "enum": list(display.SOUNDS)},
                     "speak": {"type": "string"}, "briefing": {"type": "boolean"}}},
                 handler=_alarm_now, owner_only=True, source=src, safety="auto", intents=ctl),
            Tool(name="display_alarm_set",
                 description=("Stelle einen Wecker auf dem Wand-Display („Weck mich morgen um 7“). Legt eine "
                              "Regel mit display-Aktion an. time=HH:MM; date=YYYY-MM-DD|heute|morgen für einmalig; "
                              "days=[0..6] (0=Mo) für wiederkehrend; ohne beides = nächstes Vorkommen. "
                              "briefing=true (Standard) liest danach Wetter + Termine vor. Anzeigen/Löschen über "
                              "astra_rules_list / astra_rule_delete."),
                 parameters={"type": "object", "properties": {
                     "time": {"type": "string"}, "date": {"type": "string"},
                     "days": {"type": "array", "items": {"type": "integer"}},
                     "label": {"type": "string"}, "sound": {"type": "string", "enum": list(display.SOUNDS)},
                     "briefing": {"type": "boolean"}, "speak": {"type": "string"}},
                     "required": ["time"]},
                 handler=_alarm_set, owner_only=True, source=src, safety="mutation", intents=ctl,
                 examples=["Weck mich morgen um 7", "Wecker werktags 6:40"]),
            Tool(name="display_settings",
                 description=("Display-Einstellungen lesen/ändern: tts_voice (" + ", ".join(speech.VOICES) + "), "
                              "tts_model, display_briefing (Morgen-Briefing aufs Display), briefing_time (HH:MM), "
                              "notify (Meldungen aufs Display). Ohne Argumente: nur anzeigen."),
                 parameters={"type": "object", "properties": {
                     "tts_voice": {"type": "string"}, "tts_model": {"type": "string"},
                     "display_briefing": {"type": "boolean"}, "briefing_time": {"type": "string"},
                     "notify": {"type": "boolean"}}},
                 handler=_settings, owner_only=True, source=src, safety="mutation", intents=["status", "control"]),
        ]
