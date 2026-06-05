"""Uptime Kuma — monitor status overview."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.uptime_kuma")


class UptimeKumaPlugin(Plugin):
    slug = "uptime_kuma"
    name = "Uptime Kuma"
    description = "Monitor-Status und Ausfälle aus Uptime Kuma anzeigen."
    category = PluginCategory.INFRA_AI
    icon = "📊"
    config_fields = [
        ConfigField("base_url", "Uptime-Kuma-URL", required=True,
                    help="z.B. http://192.168.178.100:3001"),
        ConfigField("api_key", "API-Key", FieldType.PASSWORD, required=True, secret=True,
                    help="Settings → API Keys in Uptime Kuma erstellen"),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=(self.get("base_url") or "").rstrip("/"),
            headers={"Authorization": f"Bearer {self.get('api_key', '')}"},
            timeout=10,
        )

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/api/status-page")
            if r.status_code < 400:
                return HealthStatus.ok("Uptime Kuma erreichbar.")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            async with self._client() as c:
                r = await c.get("/api/status-page/heartbeat/all")
                r.raise_for_status()
            data = r.json()
            down = []
            for monitor_id, beats in data.get("heartbeatList", {}).items():
                if beats and beats[-1].get("status") == 0:
                    name = data.get("monitorList", {}).get(monitor_id, {}).get("name", monitor_id)
                    down.append(name)
            if down:
                return f"📊 Uptime Kuma: {len(down)} DOWN — {', '.join(down)}"
            return "📊 Uptime Kuma: Alle Monitore OK."
        except Exception as e:
            log.warning("Uptime Kuma briefing failed: %s", e)
            return None

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with self._client() as c:
                    r = await c.get("/api/status-page/heartbeat/all")
                    r.raise_for_status()
                data = r.json()
                monitors = data.get("monitorList", {})
                heartbeats = data.get("heartbeatList", {})
                lines = ["**Uptime Kuma Monitor-Status**"]
                for mid, info in monitors.items():
                    beats = heartbeats.get(mid, [])
                    last = beats[-1] if beats else {}
                    state = "✅ UP" if last.get("status") == 1 else "❌ DOWN"
                    ms = last.get("ping", "—")
                    lines.append(f"{state} {info.get('name', mid)} ({ms} ms)")
                return "\n".join(lines) if len(lines) > 1 else "Keine Monitore gefunden."
            except Exception as e:
                return f"Uptime Kuma Fehler: {e}"

        return [Tool(
            name="uptime_status",
            description="Alle Uptime-Kuma-Monitore mit Up/Down-Status anzeigen.",
            parameters={"type": "object", "properties": {}},
            handler=_status, owner_only=True, source=self.slug,
        )]
