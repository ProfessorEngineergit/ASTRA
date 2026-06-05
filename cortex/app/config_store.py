"""Runtime config store for plugins.

Resolves each plugin's config with precedence **DB > .env fallback > field default**
so existing `.env`-driven deployments keep working while the web admin becomes the
primary path. Secret fields are encrypted at rest with Fernet; the key lives in
`ASTRA_CONFIG_KEY` or is generated once and persisted to the brain_data volume.

Public API:
    store = get_config_store()
    cfg   = await store.load(PluginClass)              # merged, decrypted, coerced
    await store.save(PluginClass, form_values, enabled) # encrypt + persist + audit
    meta  = await store.stored_meta(PluginClass)        # which keys are set (for UI)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import get_settings
from .plugins.base import ConfigField

log = logging.getLogger("astra.config_store")

# Placeholder the admin form shows for an already-set secret; submitting it back
# unchanged means "keep the existing value".
SECRET_SENTINEL = "__keep__"


class ConfigStore:
    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    # ── encryption key management ────────────────────────────────────────────
    def _key(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        s = get_settings()
        raw = (s.astra_config_key or "").strip()
        if not raw:
            raw = self._load_or_create_key_file()
        try:
            self._fernet = Fernet(raw.encode() if isinstance(raw, str) else raw)
        except Exception as e:  # noqa: BLE001
            log.error("Invalid ASTRA_CONFIG_KEY (%s); generating ephemeral key.", e)
            self._fernet = Fernet(Fernet.generate_key())
        return self._fernet

    def _load_or_create_key_file(self) -> str:
        path = Path(get_settings().brain_data_dir) / ".config_key"
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            path.write_text(key, encoding="utf-8")
            try:
                path.chmod(0o600)
            except OSError:
                pass
            log.warning("Generated new config encryption key at %s — back it up.", path)
            return key
        except Exception as e:  # noqa: BLE001
            log.error("Could not persist config key (%s); using ephemeral key.", e)
            return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._key().encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._key().decrypt(token.encode()).decode()
        except (InvalidToken, Exception) as e:  # noqa: BLE001
            log.warning("Secret decrypt failed: %s", e)
            return ""

    # ── load / save ──────────────────────────────────────────────────────────
    def _env_fallback(self, field: ConfigField) -> Any:
        if not field.env_fallback:
            return None
        return getattr(get_settings(), field.env_fallback, None) or None

    async def load(self, plugin_cls: type) -> dict[str, Any]:
        """Merged, decrypted, coerced config dict (incl. synthetic __enabled)."""
        stored = await db.plugin_config_all(plugin_cls.slug)
        cfg: dict[str, Any] = {}
        required = [f for f in plugin_cls.config_fields if f.required]
        required_satisfied = bool(required)
        for f in plugin_cls.config_fields:
            if f.key in stored:
                raw = stored[f.key]["value"]
                if f.secret and raw:
                    raw = self.decrypt(raw)
            else:
                raw = self._env_fallback(f)
                if raw is None:
                    raw = f.default
            cfg[f.key] = f.coerce(raw)
            if f.required and not cfg[f.key]:
                required_satisfied = False

        # __enabled: explicit DB toggle wins; else default ON only when the plugin
        # HAS required fields and they're already satisfied (back-compat with
        # pre-plugin .env deployments). Plugins without required fields (e.g.
        # google_tasks) default OFF until toggled in the UI.
        if "__enabled" in stored:
            cfg["__enabled"] = bool(stored["__enabled"]["value"])
        else:
            cfg["__enabled"] = required_satisfied
        return cfg

    async def stored_meta(self, plugin_cls: type) -> dict[str, bool]:
        """{field_key: is_set} for the admin UI (secrets shown as '•••• set')."""
        stored = await db.plugin_config_all(plugin_cls.slug)
        meta: dict[str, bool] = {}
        for f in plugin_cls.config_fields:
            if f.key in stored and stored[f.key]["value"] not in (None, ""):
                meta[f.key] = True
            elif self._env_fallback(f):
                meta[f.key] = True
            else:
                meta[f.key] = False
        return meta

    async def save(self, plugin_cls: type, values: dict[str, Any], enabled: bool) -> None:
        """Persist submitted form values. Empty secret + SECRET_SENTINEL = unchanged."""
        changed: list[str] = []
        for f in plugin_cls.config_fields:
            submitted = values.get(f.key)
            if f.secret:
                if submitted in (None, "", SECRET_SENTINEL):
                    continue  # keep existing secret
                await db.plugin_config_set(plugin_cls.slug, f.key, self.encrypt(str(submitted)), True)
                changed.append(f.key)
            else:
                coerced = f.coerce(submitted)
                await db.plugin_config_set(plugin_cls.slug, f.key, coerced, False)
                if coerced not in (None, ""):
                    changed.append(f.key)
        await db.plugin_config_set(plugin_cls.slug, "__enabled", bool(enabled), False)
        try:
            await db.audit(
                "config_change", actor="owner",
                detail={"plugin": plugin_cls.slug, "enabled": bool(enabled), "fields": changed},
            )
        except Exception:  # noqa: BLE001
            pass


_store: ConfigStore | None = None


def get_config_store() -> ConfigStore:
    global _store
    if _store is None:
        _store = ConfigStore()
    return _store
