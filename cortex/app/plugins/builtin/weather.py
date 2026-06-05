"""OpenWeatherMap — current weather + 24h forecast."""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthState, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.weather")

_BASE = "https://api.openweathermap.org/data/2.5"


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

    async def _fetch_forecast(self, city: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{_BASE}/forecast", params={
                "q": city or self.get("city", "Frankfurt,DE"),
                "appid": self.get("api_key", ""),
                "units": self.get("units", "metric"),
                "lang": "de",
                "cnt": 8,
            })
            r.raise_for_status()
            return r.json()

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
                return "Plugin deaktiviert."
            city = args.get("city") or self.get("city", "Frankfurt,DE")
            units = self.get("units", "metric")
            unit_sym = "°C" if units == "metric" else "°F"
            try:
                data = await self._fetch_forecast(city)
                if str(data.get("cod")) != "200":
                    return f"Fehler: {data.get('message', 'Unbekannt')}"
                lines = [f"**Wetter für {data['city']['name']}**"]
                for item in data["list"]:
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
                return "\n".join(lines)
            except Exception as e:
                return f"Wetterabfrage fehlgeschlagen: {e}"

        return [Tool(
            name="get_weather",
            description="Aktuelles Wetter und 24-h-Vorhersage für eine Stadt abrufen.",
            parameters={"type": "object", "properties": {
                "city": {"type": "string",
                         "description": "Stadt,Ländercode (leer = konfigurierte Stadt)"},
            }},
            handler=_get_weather, owner_only=True, source=self.slug,
        )]
