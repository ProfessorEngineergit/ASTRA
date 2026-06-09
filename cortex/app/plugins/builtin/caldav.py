"""CalDAV calendar — iCloud, Nextcloud, Radicale, mailbox.org, …

Implemented via raw httpx (WebDAV + iCal) — no external library needed.
Tools: calendar_list, calendar_add, calendar_search, calendar_delete.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

# ── iCal / CalDAV helpers ─────────────────────────────────────────────────────

_ICLOUD_BASE = "https://caldav.icloud.com"

_REPORT_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
    '<C:filter><C:comp-filter name="VCALENDAR">'
    '<C:comp-filter name="VEVENT">'
    '<C:time-range start="{start}" end="{end}"/>'
    "</C:comp-filter></C:comp-filter></C:filter>"
    "</C:calendar-query>"
)
_PROPFIND_HOME = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    "<D:prop><C:calendar-home-set/><D:displayname/></D:prop></D:propfind>"
)
_PROPFIND_CALS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    "<D:prop><D:displayname/><D:resourcetype/>"
    "<C:supported-calendar-component-set/></D:prop></D:propfind>"
)


def _ical_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_ical_dt(value: str) -> datetime | None:
    value = value.split(";")[-1].replace("Z", "").strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _get_prop(lines: list[str], key: str) -> str:
    for ln in lines:
        if ln.startswith(key + ":") or ln.startswith(key + ";"):
            return ln.split(":", 1)[-1].strip()
    return ""


def _parse_vevents(ical_text: str) -> list[dict]:
    unfolded: list[str] = []
    for raw in ical_text.splitlines():
        if raw.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw.strip())
    events: list[dict] = []
    in_event = False
    cur: list[str] = []
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = []
        elif line == "END:VEVENT" and in_event:
            in_event = False
            start_raw = _get_prop(cur, "DTSTART")
            end_raw = _get_prop(cur, "DTEND")
            start_dt = _parse_ical_dt(start_raw) if start_raw else None
            events.append({
                "uid": _get_prop(cur, "UID"),
                "summary": _get_prop(cur, "SUMMARY"),
                "start": start_dt.isoformat() if start_dt else start_raw,
                "end": (_parse_ical_dt(end_raw).isoformat() if end_raw else ""),
                "description": _get_prop(cur, "DESCRIPTION"),
                "location": _get_prop(cur, "LOCATION"),
                "start_dt": start_dt,
            })
        elif in_event:
            cur.append(line)
    return events


def _build_vevent(summary: str, start: datetime, end: datetime,
                  description: str = "", location: str = "") -> tuple[str, str]:
    uid = str(uuid.uuid4()) + "@astra"
    ical = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ASTRA//CalDAV//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\n"
        f"DTSTART:{_ical_dt(start)}\r\nDTEND:{_ical_dt(end)}\r\n"
        f"DESCRIPTION:{description}\r\nLOCATION:{location}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return uid, ical


# ── Plugin ────────────────────────────────────────────────────────────────────

class CalDavPlugin(Plugin):
    slug = "caldav"
    name = "CalDAV Kalender"
    description = "Kalender lesen/schreiben via CalDAV — iCloud, Nextcloud, Radicale, mailbox.org."
    category = PluginCategory.PRODUCTIVITY
    icon = "📆"
    coming_soon = False
    config_fields = [
        ConfigField("url", "CalDAV-URL",
                    help="iCloud: https://caldav.icloud.com  |  Nextcloud: https://…/remote.php/dav"),
        ConfigField("username", "Benutzername / Apple-ID"),
        ConfigField("password", "Passwort / App-Passwort", secret=True,
                    help="iCloud: App-spezifisches Passwort von appleid.apple.com"),
        ConfigField("calendar_name", "Kalender-Namen (kommagetrennt, leer = alle)", default=""),
        ConfigField("days_ahead", "Tage voraus", type=FieldType.NUMBER, default=14),
    ]

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.get("username", ""), self.get("password", ""))

    def _base(self) -> str:
        return (self.get("url") or _ICLOUD_BASE).rstrip("/")

    def _wanted_names(self) -> list[str]:
        raw = (self.get("calendar_name") or "").strip()
        return [n.strip().lower() for n in raw.split(",") if n.strip()] if raw else []

    async def _discover_calendar_urls(self, client: httpx.AsyncClient) -> list[tuple[str, str]]:
        """Return list of (url, display_name) for all matching calendars."""
        base = self._base()
        r = await client.request(
            "PROPFIND", f"{base}/",
            content=_PROPFIND_HOME.encode(),
            headers={"Depth": "0", "Content-Type": "application/xml"},
        )
        home_match = re.search(r"<[^:>]*:?href[^>]*>([^<]+calendars[^<]+)</", r.text)
        if home_match:
            home = home_match.group(1).strip()
        else:
            uid_match = re.search(r"/(\d{6,})/", r.text)
            uid = uid_match.group(1) if uid_match else ""
            home = f"{base}/{uid}/calendars/" if uid else f"{base}/"
        if not home.startswith("http"):
            home = base + home

        r2 = await client.request(
            "PROPFIND", home,
            content=_PROPFIND_CALS.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        wanted = self._wanted_names()
        hrefs = re.findall(r"<[^:>]*:?href[^>]*>([^<]+)</", r2.text)
        names = re.findall(r"<[^:>]*:?displayname[^>]*>([^<]*)</", r2.text)
        results: list[tuple[str, str]] = []
        for i, href in enumerate(hrefs):
            if href == home:
                continue
            if "principals" in href or "notification" in href.lower():
                continue
            display = names[i].strip() if i < len(names) else ""
            if wanted and display.lower() not in wanted:
                continue
            url = href if href.startswith("http") else base + href
            results.append((url, display))
        return results

    async def _discover_calendar_url(self, client: httpx.AsyncClient) -> str | None:
        """Return first matching calendar URL (used for add/delete)."""
        urls = await self._discover_calendar_urls(client)
        return urls[0][0] if urls else None

    async def _fetch_events(self, cal_url: str, client: httpx.AsyncClient,
                            days: int = 14) -> list[dict]:
        now = datetime.now(timezone.utc)
        body = _REPORT_BODY.format(start=_ical_dt(now), end=_ical_dt(now + timedelta(days=days)))
        r = await client.request(
            "REPORT", cal_url,
            content=body.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        blocks = re.findall(
            r"<[^:>]*:?calendar-data[^>]*>(.*?)</[^:>]*:?calendar-data>",
            r.text, re.DOTALL,
        )
        events: list[dict] = []
        for block in blocks:
            block = block.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            events.extend(_parse_vevents(block))
        events.sort(key=lambda e: e.get("start_dt") or datetime.max.replace(tzinfo=timezone.utc))
        return events

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        if not self.get("url") or not self.get("username"):
            return HealthStatus.error("URL und Benutzername eintragen.")
        try:
            async with httpx.AsyncClient(auth=self._auth(), timeout=12,
                                         follow_redirects=True) as c:
                cal_urls = await self._discover_calendar_urls(c)
            if cal_urls:
                names = ", ".join(n or u.rstrip("/").split("/")[-1] for u, n in cal_urls)
                return HealthStatus.ok(f"Kalender verbunden: {names}")
            return HealthStatus.error("Kein Kalender gefunden — URL / Zugangsdaten prüfen.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e)[:200])

    def tools(self) -> list[Tool]:

        async def _list_events(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "CalDAV ist deaktiviert."
            days = int(args.get("days") or self.get("days_ahead") or 14)
            try:
                async with httpx.AsyncClient(auth=self._auth(), timeout=15,
                                             follow_redirects=True) as c:
                    cal_urls = await self._discover_calendar_urls(c)
                    if not cal_urls:
                        return "Kein Kalender gefunden."
                    events: list[dict] = []
                    for url, cal_name in cal_urls:
                        for ev in await self._fetch_events(url, c, days):
                            ev["_calendar"] = cal_name
                            events.append(ev)
            except Exception as e:  # noqa: BLE001
                return f"Fehler: {e}"
            events.sort(key=lambda e: e.get("start_dt") or datetime.max.replace(tzinfo=timezone.utc))
            if not events:
                return f"Keine Termine in den nächsten {days} Tagen."
            lines = [f"Termine (nächste {days} Tage):"]
            for ev in events[:40]:
                start = (ev.get("start") or "?")[:16].replace("T", " ").replace("+00:00", "")
                loc = f" · {ev['location']}" if ev.get("location") else ""
                cal = f" [{ev['_calendar']}]" if ev.get("_calendar") else ""
                lines.append(f"• {start} — {ev.get('summary', '?')}{loc}{cal}")
            return "\n".join(lines)

        async def _add_event(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "CalDAV ist deaktiviert."
            summary = str(args.get("summary") or "").strip()
            if not summary:
                return "Bitte 'summary' (Titel) angeben."
            try:
                start_s = str(args.get("start") or "")
                end_s = str(args.get("end") or "")
                start = datetime.fromisoformat(start_s).replace(tzinfo=timezone.utc) if start_s \
                    else datetime.now(timezone.utc).replace(minute=0, second=0) + timedelta(hours=1)
                end = datetime.fromisoformat(end_s).replace(tzinfo=timezone.utc) if end_s \
                    else start + timedelta(hours=1)
            except ValueError as e:
                return f"Ungültiges Datum: {e}"
            uid, ical = _build_vevent(
                summary, start, end,
                description=str(args.get("description") or ""),
                location=str(args.get("location") or ""),
            )
            try:
                async with httpx.AsyncClient(auth=self._auth(), timeout=15,
                                             follow_redirects=True) as c:
                    cal_url = await self._discover_calendar_url(c)
                    if not cal_url:
                        return "Kein Kalender gefunden."
                    r = await c.put(
                        f"{cal_url}{uid}.ics",
                        content=ical.encode(),
                        headers={"Content-Type": "text/calendar; charset=utf-8"},
                    )
                if r.status_code in (201, 204):
                    dt_str = start.strftime("%d.%m.%Y %H:%M")
                    return f"Termin angelegt: '{summary}' am {dt_str} UTC"
                return f"Fehler: HTTP {r.status_code} — {r.text[:200]}"
            except Exception as e:  # noqa: BLE001
                return f"Fehler: {e}"

        async def _search_events(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "CalDAV ist deaktiviert."
            query = str(args.get("query") or "").lower().strip()
            days = int(args.get("days") or 90)
            try:
                async with httpx.AsyncClient(auth=self._auth(), timeout=15,
                                             follow_redirects=True) as c:
                    cal_urls = await self._discover_calendar_urls(c)
                    if not cal_urls:
                        return "Kein Kalender gefunden."
                    events: list[dict] = []
                    for url, cal_name in cal_urls:
                        for ev in await self._fetch_events(url, c, days):
                            ev["_calendar"] = cal_name
                            events.append(ev)
            except Exception as e:  # noqa: BLE001
                return f"Fehler: {e}"
            if query:
                events = [e for e in events if
                          query in (e.get("summary") or "").lower()
                          or query in (e.get("description") or "").lower()
                          or query in (e.get("location") or "").lower()]
            if not events:
                return f'Keine Termine fuer "{query}" in den naechsten {days} Tagen.'
            lines = [f'Treffer fuer "{query}":']
            for ev in events[:20]:
                start = (ev.get("start") or "?")[:16].replace("T", " ").replace("+00:00", "")
                uid_short = (ev.get("uid") or "")[:20]
                cal = f" [{ev['_calendar']}]" if ev.get("_calendar") else ""
                lines.append(f"• {start} — {ev.get('summary','?')}{cal} [UID: {uid_short}]")
            return "\n".join(lines)

        async def _delete_event(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "CalDAV ist deaktiviert."
            uid = str(args.get("uid") or "").strip()
            if not uid:
                return "Bitte 'uid' angeben (aus calendar_search)."
            status_code = 0
            try:
                async with httpx.AsyncClient(auth=self._auth(), timeout=15,
                                             follow_redirects=True) as c:
                    cal_url = await self._discover_calendar_url(c)
                    if not cal_url:
                        return "Kein Kalender gefunden."
                    for path in [f"{cal_url}{uid}.ics", f"{cal_url}{uid}"]:
                        r = await c.delete(path)
                        status_code = r.status_code
                        if r.status_code in (200, 204):
                            return f"Termin {uid[:24]}… gelöscht."
                return f"Termin nicht gefunden (HTTP {status_code})."
            except Exception as e:  # noqa: BLE001
                return f"Fehler: {e}"

        return [
            Tool(name="calendar_list",
                 description="Zeige bevorstehende Kalendertermine (iCloud/CalDAV). Optional: days=Tage.",
                 parameters={"type": "object", "properties": {
                     "days": {"type": "integer", "description": "Tage voraus (Standard: 14)"}}},
                 handler=_list_events, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status", "list"]),
            Tool(name="calendar_add",
                 description="Lege einen neuen Kalendertermin an (iCloud/CalDAV).",
                 parameters={"type": "object", "properties": {
                     "summary": {"type": "string", "description": "Titel"},
                     "start": {"type": "string", "description": "ISO-8601, z.B. 2026-06-20T15:00:00"},
                     "end": {"type": "string", "description": "ISO-8601 Ende (Standard: +1h)"},
                     "description": {"type": "string"},
                     "location": {"type": "string"}},
                     "required": ["summary", "start"]},
                 handler=_add_event, owner_only=True, source=self.slug,
                 safety="mutation", intents=["control"]),
            Tool(name="calendar_search",
                 description="Suche Kalendertermine nach Stichwort.",
                 parameters={"type": "object", "properties": {
                     "query": {"type": "string"},
                     "days": {"type": "integer", "description": "Tage voraus (Standard: 90)"}},
                     "required": ["query"]},
                 handler=_search_events, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status"]),
            Tool(name="calendar_delete",
                 description="Lösche einen Kalendertermin per UID (aus calendar_search).",
                 parameters={"type": "object", "properties": {
                     "uid": {"type": "string", "description": "Event-UID"}},
                     "required": ["uid"]},
                 handler=_delete_event, owner_only=True, source=self.slug,
                 safety="mutation", intents=["control"]),
        ]
