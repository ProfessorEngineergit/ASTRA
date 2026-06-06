"""Apple HomeKit — trigger Home scenes/automations via an Apple Shortcuts webhook.

ASTRA can't speak HomeKit directly (it's local-only/Apple-signed), but a Shortcuts
"personal automation" can listen on a URL (e.g. via a Shortcuts-compatible webhook
relay) and run any Home scene. This plugin just POSTs the scene name to that URL.
"""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


class HomeKitPlugin(Plugin):
    slug = "homekit"
    name = "Apple HomeKit"
    description = "HomeKit-Szenen über einen Apple-Shortcuts-Webhook auslösen."
    category = PluginCategory.SMART_HOME
    icon = "🏡"
    config_fields = [
        ConfigField("webhook_url", "Shortcuts-Webhook-URL", required=True, secret=True,
                    help="URL, die deine Shortcuts-Automation triggert (z. B. via Pushcut/Webhook)"),
        ConfigField("method", "HTTP-Methode", type=FieldType.SELECT, options=["POST", "GET"], default="POST"),
    ]

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        if not self.get("webhook_url"):
            return HealthStatus.not_configured()
        return HealthStatus.ok("Webhook gesetzt — Auslösen sendet an die Shortcuts-URL.")

    def tools(self) -> list[Tool]:
        async def _scene(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "HomeKit ist deaktiviert."
            scene = args.get("scene", "")
            url = self.get("webhook_url")
            async with httpx.AsyncClient(timeout=12) as c:
                if self.get("method") == "GET":
                    r = await c.get(url, params={"scene": scene})
                else:
                    r = await c.post(url, json={"scene": scene})
            return f"Szene '{scene}' ausgelöst." if r.status_code < 400 else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="homekit_scene",
            description="Löse eine HomeKit-Szene/Automation aus (per Name).",
            parameters={"type": "object", "properties": {"scene": {"type": "string"}},
                        "required": ["scene"]},
            handler=_scene, owner_only=True, source=self.slug,
        )]
