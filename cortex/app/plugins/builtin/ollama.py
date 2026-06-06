"""Ollama — query a local LLM (generate) + list models."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, HealthStatus, Plugin, PluginCategory


class OllamaPlugin(Plugin):
    slug = "ollama"
    name = "Ollama"
    description = "Lokales LLM auf deinem Server abfragen (datenschutzfreundlich)."
    category = PluginCategory.INFRA_AI
    icon = "🤖"
    config_fields = [
        ConfigField("base_url", "Ollama-URL", required=True, default="http://192.168.178.189:11434",
                    help="z. B. http://<server>:11434"),
        ConfigField("model", "Standard-Modell", default="llama3.2",
                    help="z. B. llama3.2, mistral, qwen2.5"),
    ]

    def _base(self) -> str:
        return self.get("base_url", "").rstrip("/")

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self._base()}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            return (HealthStatus.ok(f"Erreichbar — Modelle: {', '.join(models[:5]) or 'keine'}.")
                    if r.status_code == 200 else HealthStatus.error(f"HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _ask(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Ollama ist deaktiviert."
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(f"{self._base()}/api/generate",
                                 json={"model": args.get("model") or self.get("model"),
                                       "prompt": args.get("prompt", ""), "stream": False})
            return r.json().get("response", "(keine Antwort)") if r.status_code == 200 \
                else f"Fehler HTTP {r.status_code}"

        return [Tool(
            name="ollama_ask",
            description="Stelle dem lokalen Ollama-Modell eine Frage (z. B. für private Daten).",
            parameters={"type": "object", "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string", "description": "optional, sonst Standard-Modell"}},
                "required": ["prompt"]},
            handler=_ask, owner_only=True, source=self.slug,
        )]
