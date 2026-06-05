"""Ollama Lokales LLM — stub."""
from ..base import ConfigField, HealthState, HealthStatus, Plugin, PluginCategory


class OllamaPlugin(Plugin):
    slug = "ollama"
    name = "Ollama Lokales LLM"
    description = "Lokale LLM-Modelle via Ollama abfragen."
    category = PluginCategory.INFRA_AI
    icon = "🤖"
    config_fields = [ConfigField("placeholder", "Noch nicht konfigurierbar")]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(HealthState.NOT_CONFIGURED, "Kommt bald — noch nicht implementiert.")
