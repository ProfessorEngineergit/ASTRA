"""n8n bridge — trigger arbitrary webhooks with the shared secret."""
from __future__ import annotations

import json as _json
import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.n8n")


class N8nBridgePlugin(Plugin):
    slug = "n8n"
    name = "n8n Automatisierungen"
    description = "Beliebige n8n-Webhooks aus ASTRA heraus auslösen."
    category = PluginCategory.INFRA_AI
    icon = "⚙️"
    config_fields = [
        ConfigField("base_url", "n8n URL", required=True,
                    default="http://n8n:5678", env_fallback="N8N_BASE_URL",
                    help="Basis-URL deiner n8n-Instanz"),
        ConfigField("shared_secret", "Shared Secret", FieldType.PASSWORD,
                    required=True, secret=True, env_fallback="CORTEX_SHARED_SECRET",
                    help="CORTEX_SHARED_SECRET — authentifiziert cortex ↔ n8n"),
    ]

    def _base(self) -> str:
        return (self.get("base_url") or "http://n8n:5678").rstrip("/")

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self._base()}/healthz")
            if r.status_code < 400:
                return HealthStatus.ok(f"n8n erreichbar ({self._base()}).")
            return HealthStatus.error(f"HTTP {r.status_code}")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _trigger(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            path = args.get("webhook_path", "").lstrip("/")
            if not path:
                return "webhook_path ist erforderlich."
            payload_raw = args.get("payload_json", "{}")
            try:
                payload = _json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            except _json.JSONDecodeError as e:
                return f"Ungültiges JSON: {e}"
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(
                        f"{self._base()}/webhook/{path}",
                        headers={"X-Astra-Secret": self.get("shared_secret", "")},
                        json=payload,
                    )
                    r.raise_for_status()
                try:
                    return _json.dumps(r.json(), ensure_ascii=False, indent=2)
                except Exception:
                    return r.text or "Webhook ausgeführt."
            except Exception as e:
                return f"n8n-Fehler: {e}"

        return [Tool(
            name="n8n_trigger",
            description="n8n-Webhook mit beliebigem JSON-Payload auslösen.",
            parameters={"type": "object", "properties": {
                "webhook_path": {"type": "string",
                                 "description": "Webhook-Pfad nach /webhook/ (ohne führenden /)"},
                "payload_json": {"type": "string",
                                 "description": "JSON-String als Payload (Standard: {})"},
            }, "required": ["webhook_path"]},
            handler=_trigger, owner_only=True, source=self.slug,
        )]
