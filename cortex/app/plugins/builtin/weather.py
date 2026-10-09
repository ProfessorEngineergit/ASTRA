"""OpenWeatherMap — current weather + 24h forecast.

`get_weather` liefert neben dem Text eine strukturierte `WeatherData` (für Karten
auf dem OpenBoard-Display): now / hourly / daily mit normalisierten `condition`-
Codes (clear, partly, cloudy, fog, drizzle, rain, snow, sleet, thunder, wind).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthState, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.weather")

_BASE = "https://api.openweathermap.org/data/2.5"


# ─── Normalisierung (rein) ────────────────────────────────────────────────────
CONDITIONS = ("clear", "partly", "cloudy", "fog", "drizzle", "rain", "snow", "sleet", "thunder", "wind")
_PRECIP = ("thunder", "snow", "sleet", "rain", "drizzle")      # nach Schwere
WINDY_KMH = 50.0


def condition_from_owm(code: int | str | None, wind_kmh: float | None = None) -> str:
    """OpenWeatherMap-Wetter-ID → normalisierter Zustand.

    https://openweathermap.org/weather-conditions — 2xx Gewitter, 3xx Niesel, 5xx Regen
    (511 gefrierender Regen = sleet), 6xx Schnee (611–616 Schneeregen = sleet), 7xx Sicht
    (771 Böen, 781 Tornado = wind), 800 klar, 801–802 teils bewölkt, 803–804 bewölkt.
    Bei ruhigem Himmel, aber Sturm (≥ 50 km/h) gilt „wind“."""
    try:
        c = int(code or 0)
    except (TypeError, ValueError):
        c = 0
    if 200 <= c < 300:
        cond = "thunder"
    elif 300 <= c < 400:
        cond = "drizzle"
    elif c == 511:
        cond = "sleet"
    elif 500 <= c < 600:
        cond = "rain"
    elif 611 <= c <= 616:
        cond = "sleet"
    elif 600 <= c < 700:
        cond = "snow"
    elif c in (771, 781):
        cond = "wind"
    elif 700 <= c < 800:
        cond = "fog"
    elif c == 800:
        cond = "clear"
    elif c in (801, 802):
        cond = "partly"
    elif c in (803, 804):
        cond = "cloudy"
    else:
        cond = "cloudy"
    if cond in ("clear", "partly", "cloudy") and wind_kmh is not None and wind_kmh >= WINDY_KMH:
        return "wind"
    return cond


def _wind_kmh(speed: float | None, units: str) -> float | None:
    if speed is None:
        return None
    return round(float(speed) * (1.609344 if units == "imperial" else 3.6), 1)


def _is_day(icon: str | None = None, pod: str | None = None) -> bool:
    if pod:
        return pod == "d"
    return not str(icon or "").endswith("n")


def build_weather_data(current: dict, forecast: dict, *, units: str = "metric",
                       now: datetime | None = None) -> dict:
    """OWM /weather + /forecast → WeatherData laut Display-Protokoll. Rein, ohne I/O."""
    offset = int((forecast.get("city") or {}).get("timezone")
                 or current.get("timezone") or 0)
    tz = timezone(timedelta(seconds=offset))
    now = (now or datetime.now(timezone.utc)).astimezone(tz)

    def local(ts: int) -> datetime:
        return datetime.fromtimestamp(int(ts), tz)

    items = forecast.get("list") or []
    hourly, by_day = [], {}
    for it in items:
        w = (it.get("weather") or [{}])[0]
        when = local(it.get("dt", 0))
        wind = _wind_kmh((it.get("wind") or {}).get("speed"), units)
        cond = condition_from_owm(w.get("id"), wind)
        pop = round(float(it.get("pop") or 0), 2)
        temp = round(float((it.get("main") or {}).get("temp", 0)))
        if len(hourly) < 8:
            hourly.append({"time": when.isoformat(), "temp": temp, "condition": cond,
                           "is_day": _is_day(w.get("icon"), (it.get("sys") or {}).get("pod")),
                           "pop": pop})
        main = it.get("main") or {}
        by_day.setdefault(when.date(), []).append({
            "hour": when.hour, "min": main.get("temp_min", main.get("temp")),
            "max": main.get("temp_max", main.get("temp")), "cond": cond, "pop": pop})

    cw = (current.get("weather") or [{}])[0]
    cmain = current.get("main") or {}
    cur_wind = _wind_kmh((current.get("wind") or {}).get("speed"), units)
    cur_temp = cmain.get("temp")
    today = by_day.get(now.date(), [])
    temps_today = [t for e in today for t in (e["min"], e["max"]) if t is not None]
    if cur_temp is not None:
        temps_today.append(cur_temp)
    daily = []
    for day in sorted(by_day)[:6]:
        entries = by_day[day]
        mid = min(entries, key=lambda e: abs(e["hour"] - 13))
        cond = mid["cond"]
        # Regnet es am Tag wahrscheinlich (≥ 50 %), zählt der schwerste Niederschlag.
        wet = [e["cond"] for e in entries if e["pop"] >= 0.5 and e["cond"] in _PRECIP]
        if wet:
            cond = min(wet, key=_PRECIP.index)
        daily.append({
            "date": day.isoformat(),
            "min": round(min(float(e["min"]) for e in entries if e["min"] is not None)),
            "max": round(max(float(e["max"]) for e in entries if e["max"] is not None)),
            "condition": cond,
            "pop": max(e["pop"] for e in entries),
        })
    updated = local(current["dt"]).isoformat() if current.get("dt") else now.isoformat()
    return {
        "location": current.get("name") or (forecast.get("city") or {}).get("name") or "",
        "updated": updated,
        "unit": "F" if units == "imperial" else "C",
        "now": {
            "temp": round(float(cur_temp)) if cur_temp is not None else None,
            "feels_like": round(float(cmain["feels_like"])) if cmain.get("feels_like") is not None else None,
            "condition": condition_from_owm(cw.get("id"), cur_wind),
            "is_day": _is_day(cw.get("icon")),
            "description": str(cw.get("description") or ""),
            "humidity": cmain.get("humidity"),
            "wind_kmh": cur_wind,
            "high": round(max(float(t) for t in temps_today)) if temps_today else None,
            "low": round(min(float(t) for t in temps_today)) if temps_today else None,
        },
        "hourly": hourly,
        "daily": daily,
    }


class WeatherPlugin(Plugin):
    slug = "weather"
    name = "Wetter (OpenWeatherMap)"
    description = "Aktuelles Wetter und 24-h-Vorhersage via OpenWeatherMap."
    category = PluginCategory.MEDIA
    icon = "🌤️"
    config_fields = [
        ConfigField("api_key", "API-Key", FieldType.PASSWORD, required=True, secret=True,
                    env_fallback="OPENWEATHER_API_KEY"),
        ConfigField("city", "Stadt", default="Frankfurt,DE",
                    help="Stadt,Ländercode — z.B. Berlin,DE"),
        ConfigField("units", "Einheiten", FieldType.SELECT, default="metric",
                    options=["metric", "imperial"],
                    help="metric = °C, imperial = °F"),
    ]

    def _params(self, city: str | None) -> dict:
        return {"q": city or self.get("city", "Frankfurt,DE"), "appid": self.get("api_key", ""),
                "units": self.get("units", "metric"), "lang": "de"}

    async def _fetch_forecast(self, city: str | None = None, *, cnt: int = 8) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_BASE}/forecast", params={**self._params(city), "cnt": cnt})
            r.raise_for_status()
            return r.json()

    async def _fetch_current(self, city: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_BASE}/weather", params=self._params(city))
            r.raise_for_status()
            return r.json()

    async def weather_data(self, city: str | None = None) -> dict:
        """Strukturierte WeatherData (jetzt, nächste 24 h in 3-h-Schritten, 5 Tage)."""
        current, forecast = await asyncio.gather(
            self._fetch_current(city), self._fetch_forecast(city, cnt=40))
        return build_weather_data(current, forecast, units=self.get("units", "metric"))

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            data = await self._fetch_forecast()
            if str(data.get("cod")) != "200":
                return HealthStatus.error(f"API-Fehler: {data.get('message', 'unbekannt')}")
            return HealthStatus.ok(f"Verbunden — Stadt: {data.get('city', {}).get('name')}")
        except Exception as e:
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            data = await self._fetch_forecast()
            item = data["list"][0]
            desc = item["weather"][0]["description"].capitalize()
            temp = round(item["main"]["temp"])
            unit = "°C" if self.get("units") == "metric" else "°F"
            city = data.get("city", {}).get("name", self.get("city"))
            return f"🌤️ Wetter {city}: {desc}, {temp}{unit}"
        except Exception as e:
            log.warning("Weather briefing failed: %s", e)
            return None

    def tools(self) -> list[Tool]:
        async def _get_weather(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, summary="Plugin deaktiviert.", source=self.slug)
            city = args.get("city") or self.get("city", "Frankfurt,DE")
            units = self.get("units", "metric")
            unit_sym = "°C" if units == "metric" else "°F"
            try:
                current, data = await asyncio.gather(
                    self._fetch_current(city), self._fetch_forecast(city, cnt=40))
                if str(data.get("cod")) != "200":
                    return tool_result(ok=False, source=self.slug,
                                       summary=f"Fehler: {data.get('message', 'Unbekannt')}")
                structured = build_weather_data(current, data, units=units)
                lines = [f"**Wetter für {data['city']['name']}**"]
                for item in data["list"][:8]:
                    dt = datetime.fromtimestamp(item["dt"]).strftime("%d.%m %H:%M")
                    desc = item["weather"][0]["description"]
                    temp = round(item["main"]["temp"])
                    feels = round(item["main"]["feels_like"])
                    rain = item.get("rain", {}).get("3h", 0)
                    rain_str = f", 🌧 {rain:.1f}mm" if rain else ""
                    lines.append(
                        f"{dt}: {desc}, {temp}{unit_sym} "
                        f"(fühlt sich an wie {feels}{unit_sym}){rain_str}"
                    )
                return tool_result(ok=True, summary="\n".join(lines), data=structured,
                                   source=self.slug)
            except Exception as e:
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Wetterabfrage fehlgeschlagen: {e}",
                                   error={"type": type(e).__name__, "message": str(e)})

        return [Tool(
            name="get_weather",
            description="Aktuelles Wetter und 24-h-Vorhersage für eine Stadt abrufen.",
            parameters={"type": "object", "properties": {
                "city": {"type": "string",
                         "description": "Stadt,Ländercode (leer = konfigurierte Stadt)"},
            }},
            handler=_get_weather, owner_only=True, source=self.slug,
        )]
