"""Central configuration. All values come from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Infra ────────────────────────────────────────────────────────────────
    database_url: str = "postgresql://astra:astra@localhost:5432/cortex"
    redis_url: str = "redis://localhost:6379/0"
    n8n_base_url: str = "http://localhost:5678"
    waha_base_url: str = "http://localhost:3000"
    signal_base_url: str = "http://localhost:8080"
    cortex_shared_secret: str = "dev-secret"

    # ── OpenAI (only LLM for now; swap/extend via models.py gateway) ───────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_model_small: str = "gpt-4o-mini"
    openai_embed_model: str = "text-embedding-3-small"

    # ── Telegram (owner control + approval channel) ────────────────────────────
    telegram_bot_token: str = ""
    telegram_owner_chat_id: str = ""
    astra_telegram_mode: str = "poll"  # poll | webhook

    # ── WAHA / Signal ──────────────────────────────────────────────────────────
    waha_api_key: str = ""
    waha_session: str = "default"
    signal_phone_number: str = ""

    # ── Behaviour ───────────────────────────────────────────────────────────────
    astra_defer_seconds: int = 120
    astra_dry_run: bool = False
    # How replies leave the building: "direct" (cortex calls WAHA/Signal APIs — easy
    # bootstrap) or "n8n" (cortex POSTs to the visual tool/send_* workflows).
    astra_send_backend: str = "direct"
    astra_owner_name: str = "Bahrian"
    astra_timezone: str = "Europe/Berlin"
    astra_domain: str = "localhost"
    log_level: str = "INFO"

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
