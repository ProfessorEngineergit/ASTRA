"""Display-Karten: Schema-Prüfung, Bausteine und Extraktion aus Tool-Ergebnissen.

Eine Karte ist laut „ASTRA-Display-Protokoll v1“
    {id, type, title?, subtitle?, data, ttl_seconds?}
mit `type` ∈ weather · calendar · list · markdown · image · sketch · facts · home · alarm.

Die Agent-Schleife liefert pro Turn eine Liste `tool_calls` ({tool, args, ok, summary,
result}). `extract()` macht daraus Karten (Kalender → calendar, home_state → home,
get_weather → weather, generische strukturierte Ergebnisse → list/facts) und
Aktionen (z. B. App öffnen), die Display-Tools explizit unter `data.display` ablegen.

Große Binärdaten (generierte Bilder) stehen nie im Tool-Ergebnis — das landet im
LLM-Kontext. Stattdessen verweist `src` auf `astra-file:<name>`; `materialize()`
ersetzt das erst beim Ausliefern durch eine data:-URL.
"""
from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from ..config import get_settings

CARD_TYPES = ("weather", "calendar", "list", "markdown", "image", "sketch", "facts", "home", "alarm")
CONDITIONS = ("clear", "partly", "cloudy", "fog", "drizzle", "rain", "snow", "sleet", "thunder", "wind")
BOARD_OPS = ("add_text", "add_mermaid", "add_image", "add_svg", "add_elements")
FILE_REF = "astra-file:"
MAX_CARDS_PER_TURN = 6
_MAX_SVG = 200_000
_MAX_TEXT = 20_000
_MAX_ITEMS = 50

# Google-Kalender colorId → Hex (offizielle Ereignis-Palette).
_GCAL_COLORS = {
    "1": "#7986cb", "2": "#33b679", "3": "#8e24aa", "4": "#e67c73", "5": "#f6bf26", "6": "#f4511e",
    "7": "#039be5", "8": "#616161", "9": "#3f51b5", "10": "#0b8043", "11": "#d50000",
}


class CardError(ValueError):
    """Karte passt nicht zum Schema — Text ist für das LLM gedacht (deutsch, konkret)."""


# ─── Dateiverweise (generierte Bilder) ────────────────────────────────────────
def media_dir() -> Path:
    """Gleicher Ordner wie die Web-Chat-Uploads → im Admin unter /admin/uploads/<name> sichtbar."""
    path = Path(get_settings().brain_data_dir) / "uploads" / "web_chat"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_ref(name: str) -> str:
    return FILE_REF + name


def _resolve_ref(src: str) -> str:
    name = src[len(FILE_REF):]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name) or name.startswith("."):
        raise CardError("Ungültiger Dateiverweis.")
    path = media_dir() / name
    if not path.is_file():
        raise CardError(f"Datei {name} gibt es nicht (mehr).")
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise CardError("Nur Bilder können angezeigt werden.")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def materialize(obj: Any) -> Any:
    """Ersetzt `astra-file:`-Verweise in `src`-Feldern durch data:-URLs (rekursiv, kopierend)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "src" and isinstance(v, str) and v.startswith(FILE_REF):
                out[k] = _resolve_ref(v)
            else:
                out[k] = materialize(v)
        return out
    if isinstance(obj, list):
        return [materialize(v) for v in obj]
    return obj


# ─── Validierung ──────────────────────────────────────────────────────────────
def _s(v: Any, limit: int = 300) -> str:
    return str(v if v is not None else "").strip()[:limit]


def _image_src_ok(src: str) -> bool:
    return src.startswith(("data:image/", "https://", FILE_REF))


def check_svg(svg: str) -> str:
    svg = str(svg or "").strip()
    if not svg or "<svg" not in svg[:2000].lower():
        raise CardError("sketch.svg muss ein <svg>-Dokument sein.")
    if len(svg) > _MAX_SVG:
        raise CardError("SVG ist zu groß (max. 200 KB).")
    low = svg.lower()
    # Wird nur als Bild gerendert — trotzdem kein aktiver Inhalt (Verteidigung in der Tiefe).
    if "<script" in low or "<foreignobject" in low or "javascript:" in low or re.search(r"\son\w+\s*=", low):
        raise CardError("SVG darf keine Skripte, foreignObject oder on*-Attribute enthalten.")
    return svg


def _validate_data(ctype: str, data: Any) -> dict:
    if not isinstance(data, dict):
        raise CardError(f"{ctype}: data muss ein Objekt sein.")
    if ctype == "markdown":
        text = _s(data.get("text"), _MAX_TEXT)
        if not text:
            raise CardError("markdown: data.text fehlt.")
        return {"text": text}
    if ctype == "list":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise CardError("list: data.items muss eine nicht-leere Liste sein.")
        out = []
        for it in items[:_MAX_ITEMS]:
            if isinstance(it, str):
                it = {"title": it}
            if not isinstance(it, dict) or not _s(it.get("title")):
                raise CardError("list: jedes Element braucht einen title.")
            row = {"title": _s(it.get("title"), 200)}
            for k in ("detail", "meta", "icon"):
                if it.get(k) not in (None, ""):
                    row[k] = _s(it.get(k), 400)
            out.append(row)
        return {"items": out}
    if ctype == "facts":
        rows = data.get("rows")
        if isinstance(rows, dict):
            rows = [{"label": k, "value": v} for k, v in rows.items()]
        if not isinstance(rows, list) or not rows:
            raise CardError("facts: data.rows muss eine nicht-leere Liste {label, value} sein.")
        out = []
        for r in rows[:_MAX_ITEMS]:
            if not isinstance(r, dict) or not _s(r.get("label")):
                raise CardError("facts: jede Zeile braucht label und value.")
            out.append({"label": _s(r.get("label"), 120), "value": _s(r.get("value"), 400)})
        return {"rows": out}
    if ctype == "image":
        src = _s(data.get("src"), 20_000_000)
        if not _image_src_ok(src):
            raise CardError("image: data.src muss eine data:image/…- oder https://-URL sein.")
        row = {"src": src, "alt": _s(data.get("alt"), 300)}
        if data.get("caption"):
            row["caption"] = _s(data.get("caption"), 400)
        return row
    if ctype == "sketch":
        return {"svg": check_svg(data.get("svg"))}
    if ctype == "calendar":
        events = data.get("events")
        if not isinstance(events, list):
            raise CardError("calendar: data.events muss eine Liste sein.")
        out = []
        for e in events[:_MAX_ITEMS]:
            if not isinstance(e, dict) or not _s(e.get("title")) or not _s(e.get("start")):
                raise CardError("calendar: jedes Ereignis braucht title und start (ISO 8601).")
            row = {"title": _s(e["title"], 200), "start": _s(e["start"], 40),
                   "end": _s(e.get("end") or e["start"], 40), "all_day": bool(e.get("all_day"))}
            for k in ("location", "calendar", "color"):
                if e.get(k):
                    row[k] = _s(e[k], 200)
            out.append(row)
        return {"events": out}
    if ctype == "home":
        ents = data.get("entities")
        if not isinstance(ents, list) or not ents:
            raise CardError("home: data.entities muss eine nicht-leere Liste sein.")
        out = []
        for e in ents[:_MAX_ITEMS]:
            if not isinstance(e, dict) or not _s(e.get("name")):
                raise CardError("home: jede Entität braucht name, state, domain.")
            row = {"name": _s(e["name"], 120), "state": _s(e.get("state"), 120),
                   "domain": _s(e.get("domain"), 40)}
            if e.get("unit"):
                row["unit"] = _s(e["unit"], 20)
            out.append(row)
        return {"entities": out}
    if ctype == "alarm":
        if not _s(data.get("at")):
            raise CardError("alarm: data.at (ISO 8601) fehlt.")
        return {"at": _s(data["at"], 40), "label": _s(data.get("label"), 200)}
    if ctype == "weather":
        now = data.get("now")
        if not isinstance(now, dict) or now.get("condition") not in CONDITIONS:
            raise CardError("weather: data.now.condition fehlt oder ist unbekannt.")
        return data
    raise CardError(f"Unbekannter Kartentyp {ctype}.")


def validate_card(card: Any) -> dict:
    """Prüft eine Karte gegen das Schema und gibt eine bereinigte Kopie zurück."""
    if not isinstance(card, dict):
        raise CardError("Karte muss ein Objekt sein.")
    ctype = str(card.get("type") or "").strip()
    if ctype not in CARD_TYPES:
        raise CardError(f"type muss eines von {', '.join(CARD_TYPES)} sein.")
    out: dict[str, Any] = {
        "id": _s(card.get("id"), 64) or f"{ctype}-{uuid.uuid4().hex[:8]}",
        "type": ctype,
        "data": _validate_data(ctype, card.get("data")),
    }
    for k in ("title", "subtitle"):
        if card.get(k):
            out[k] = _s(card[k], 200)
    if card.get("ttl_seconds") not in (None, ""):
        try:
            out["ttl_seconds"] = max(1, min(int(card["ttl_seconds"]), 7 * 86400))
        except (TypeError, ValueError):
            raise CardError("ttl_seconds muss eine Zahl sein.") from None
    return out


def validate_board_op(op: Any) -> dict:
    """Board-Operation (add_text · add_mermaid · add_image · add_svg · add_elements)."""
    if not isinstance(op, dict):
        raise CardError("Board-Operation muss ein Objekt sein.")
    kind = str(op.get("op") or "")
    if kind == "add_text":
        text = _s(op.get("text"), _MAX_TEXT)
        if not text:
            raise CardError("add_text braucht text.")
        out: dict[str, Any] = {"op": kind, "text": text}
        for k in ("x", "y"):
            if isinstance(op.get(k), (int, float)) and not isinstance(op.get(k), bool):
                out[k] = op[k]
        return out
    if kind == "add_mermaid":
        definition = _s(op.get("definition"), _MAX_TEXT)
        if not definition:
            raise CardError("add_mermaid braucht definition.")
        return {"op": kind, "definition": definition}
    if kind == "add_image":
        src = _s(op.get("src"), 20_000_000)
        if not _image_src_ok(src):
            raise CardError("add_image braucht src (data:image/… oder https://…).")
        out = {"op": kind, "src": src}
        if op.get("caption"):
            out["caption"] = _s(op["caption"], 400)
        return out
    if kind == "add_svg":
        return {"op": kind, "svg": check_svg(op.get("svg"))}
    if kind == "add_elements":
        els = op.get("elements")
        if not isinstance(els, list) or not els or len(els) > 500:
            raise CardError("add_elements braucht elements (Liste, max. 500).")
        return {"op": kind, "elements": els}
    raise CardError(f"op muss eines von {', '.join(BOARD_OPS)} sein.")


# ─── Bausteine ────────────────────────────────────────────────────────────────
def normalize_event(e: dict) -> dict | None:
    """Google-Kalender-Ereignis (oder bereits normalisiertes) → Protokoll-Event."""
    if not isinstance(e, dict):
        return None
    if "title" in e and isinstance(e.get("start"), str):
        title, start, end = e.get("title"), e.get("start"), e.get("end") or e.get("start")
        all_day = bool(e.get("all_day"))
    else:
        st, en = e.get("start") or {}, e.get("end") or {}
        if isinstance(st, str):
            st = {"dateTime": st}
        if isinstance(en, str):
            en = {"dateTime": en}
        start = st.get("dateTime") or st.get("date")
        end = en.get("dateTime") or en.get("date") or start
        all_day = bool(st.get("date") and not st.get("dateTime"))
        title = e.get("summary") or e.get("title") or "(ohne Titel)"
    if not start:
        return None
    out = {"title": _s(title, 200), "start": str(start), "end": str(end), "all_day": all_day}
    if e.get("location"):
        out["location"] = _s(e["location"], 200)
    cal = (e.get("organizer") or {}).get("displayName") if isinstance(e.get("organizer"), dict) else None
    if cal or e.get("calendar"):
        out["calendar"] = _s(cal or e.get("calendar"), 120)
    color = e.get("color") or _GCAL_COLORS.get(str(e.get("colorId") or ""))
    if color:
        out["color"] = color
    return out


def calendar_card(events: list, *, title: str = "Heute") -> dict:
    norm = [n for n in (normalize_event(e) for e in events or []) if n]
    sub = f"{len(norm)} Termin" + ("" if len(norm) == 1 else "e") if norm else "Keine Termine"
    return {"id": f"calendar-{uuid.uuid4().hex[:8]}", "type": "calendar", "title": title,
            "subtitle": sub, "data": {"events": norm}}


def weather_card(data: dict) -> dict:
    now = data.get("now") or {}
    sub = f"{now.get('description', '')}".strip().capitalize() or None
    card = {"id": f"weather-{uuid.uuid4().hex[:8]}", "type": "weather",
            "title": f"Wetter {data.get('location') or ''}".strip(), "data": data}
    if sub:
        card["subtitle"] = sub
    return card


def home_card(values: list[dict], *, area: str = "") -> dict | None:
    ents = []
    for v in values or []:
        if not isinstance(v, dict):
            continue
        eid = str(v.get("entity_id") or "")
        row = {"name": v.get("name") or eid, "state": str(v.get("state", "")),
               "domain": v.get("domain") or (eid.split(".", 1)[0] if "." in eid else "")}
        if v.get("unit"):
            row["unit"] = v["unit"]
        ents.append(row)
    if not ents:
        return None
    return {"id": f"home-{uuid.uuid4().hex[:8]}", "type": "home",
            "title": area or "Zuhause", "data": {"entities": ents}}


# ─── Extraktion aus der Tool-Spur ─────────────────────────────────────────────
_CALENDAR_TOOLS = {"calendar_today", "google_calendar_search"}
_NO_GENERIC = ("astra_", "display_", "generate_image")
_NO_GENERIC_EXACT = {"recall_memory", "remember", "remember_fact", "request_owner_approval",
                     "check_availability", "suggest_meeting_times"}
_TITLE_KEYS = ("title", "name", "summary", "label", "subject")
_DETAIL_KEYS = ("detail", "description", "state", "status", "value", "text", "notes")


def _base_name(tool: str) -> str:
    return str(tool or "").split("__", 1)[0]


def _headline(summary: str, fallback: str) -> str:
    first = (summary or "").strip().splitlines()[0] if (summary or "").strip() else ""
    first = first.strip(" :*#")
    return first[:80] if first and len(first) <= 80 else fallback


def _generic_card(tool: str, summary: str, data: Any) -> dict | None:
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:8]):
        items = []
        for row in data[:8]:
            title = next((row[k] for k in _TITLE_KEYS if isinstance(row.get(k), (str, int, float))
                          and str(row.get(k)).strip()), None)
            if title is None:
                return None
            item = {"title": _s(title, 200)}
            detail = next((row[k] for k in _DETAIL_KEYS if isinstance(row.get(k), (str, int, float))
                           and str(row.get(k)).strip() and row.get(k) != title), None)
            if detail is not None:
                item["detail"] = _s(detail, 300)
            items.append(item)
        return {"id": f"list-{uuid.uuid4().hex[:8]}", "type": "list",
                "title": _headline(summary, tool), "data": {"items": items}}
    if isinstance(data, dict) and 0 < len(data) <= 12 and all(
            isinstance(v, (str, int, float, bool)) for v in data.values()):
        rows = [{"label": str(k).replace("_", " "), "value": ("ja" if v is True else "nein" if v is False else v)}
                for k, v in data.items()]
        return {"id": f"facts-{uuid.uuid4().hex[:8]}", "type": "facts",
                "title": _headline(summary, tool), "data": {"rows": rows}}
    return None


def _card_for_call(call: dict) -> list[dict]:
    tool = _base_name(call.get("tool", ""))
    result = call.get("result")
    if not isinstance(result, dict) or call.get("ok") is not True:
        return []
    data = result.get("data")
    if tool in _CALENDAR_TOOLS and isinstance(data, list):
        return [calendar_card(data, title="Heute" if tool == "calendar_today" else "Termine")]
    if tool == "home_state" and isinstance(data, dict):
        card = home_card(data.get("values") or data.get("matches") or [], area=str(data.get("area") or ""))
        return [card] if card else []
    if tool == "get_weather" and isinstance(data, dict) and isinstance(data.get("now"), dict):
        return [weather_card(data)]
    if tool.startswith(_NO_GENERIC) or tool in _NO_GENERIC_EXACT:
        return []
    card = _generic_card(tool, str(call.get("summary") or ""), data)
    return [card] if card else []


def extract(tool_calls: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """Karten + Aktionen aus der Tool-Spur eines Turns. Ungültiges wird still verworfen."""
    cards: list[dict] = []
    actions: list[dict] = []
    for call in tool_calls or []:
        result = call.get("result") if isinstance(call, dict) else None
        data = result.get("data") if isinstance(result, dict) else None
        explicit = data.get("display") if isinstance(data, dict) else None
        if isinstance(explicit, dict):
            # Display-Tools legen Karten/Aktionen ausdrücklich ab (Turn kam vom Display selbst).
            for c in explicit.get("cards") or []:
                try:
                    cards.append(validate_card(c))
                except CardError:
                    continue
            actions.extend(a for a in explicit.get("actions") or [] if isinstance(a, dict) and a.get("type"))
            continue
        for c in _card_for_call(call):
            try:
                cards.append(validate_card(c))
            except CardError:
                continue
    return cards[:MAX_CARDS_PER_TURN], actions
