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

    # ── Durable, editable knowledge (lives in a Docker volume, survives updates) ─
    # Markdown files here are loaded into ASTRA's system prompt. NOT in git.
    brain_data_dir: str = "/srv/data"

    # ── Web admin (plugin catalog + config) ──────────────────────────────────────
    # Admin password: set here OR via the first-run wizard on first opening /admin.
    astra_admin_password: str = ""
    # Fernet key for encrypting plugin secrets at rest. Blank → generated once and
    # persisted to {brain_data_dir}/.config_key (survives updates).
    astra_config_key: str = ""

    # ── Voice (Whisper transcription of Telegram voice notes) ────────────────────
    astra_voice_transcription: bool = True
    whisper_model: str = "whisper-1"

    # ── Home Assistant (active control + status) ─────────────────────────────────
    home_assistant_base_url: str = ""        # e.g. http://homeassistant.local:8123
    home_assistant_token: str = ""           # long-lived access token

    # ── EduPage (school timetable) ───────────────────────────────────────────────
    edupage_subdomain: str = ""              # e.g. "gymnasium-xy"  → gymnasium-xy.edupage.org
    edupage_username: str = ""
    edupage_password: str = ""

    # ── RMV (public transport, HAFAS open API) ───────────────────────────────────
    rmv_api_key: str = ""                    # accessId for https://www.rmv.de/hapi
    rmv_home_stop_id: str = ""               # default origin (station extId)
    rmv_school_stop_id: str = ""             # default destination

    # ── Google Tasks (added via an n8n tool workflow that holds the OAuth) ────────
    google_tasks_enabled: bool = False
    google_tasks_list: str = "@default"

    # ── Morning briefing ─────────────────────────────────────────────────────────
    astra_briefing_enabled: bool = False
    astra_briefing_time: str = "07:00"       # local time HH:MM
    astra_briefing_chat_id: str = ""         # defaults to telegram_owner_chat_id

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def voice_enabled(self) -> bool:
        return self.astra_voice_transcription and self.openai_enabled

    @property
    def ha_enabled(self) -> bool:
        return bool(self.home_assistant_base_url and self.home_assistant_token)

    @property
    def edupage_enabled(self) -> bool:
        return bool(self.edupage_subdomain and self.edupage_username and self.edupage_password)

    @property
    def rmv_enabled(self) -> bool:
        return bool(self.rmv_api_key)

    @property
    def briefing_chat(self) -> str:
        return self.astra_briefing_chat_id or self.telegram_owner_chat_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
