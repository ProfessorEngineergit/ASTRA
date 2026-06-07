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
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import get_settings
from .plugins.base import ConfigField

log = logging.getLogger("astra.config_store")

# Placeholder the admin form shows for an already-set secret; submitting it back
# unchanged means "keep the existing value".
SECRET_SENTINEL = "__keep__"
INSTALLATIONS_KEY = "__installations"
DEFAULT_INSTALLATION_ID = "default"


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

    def _installation_slug(self, plugin_cls: type, install_id: str) -> str:
        return plugin_cls.slug if install_id == DEFAULT_INSTALLATION_ID else f"{plugin_cls.slug}:{install_id}"

    def _installation_label(self, plugin_cls: type, raw: dict, index: int) -> str:
        return str(raw.get("name") or raw.get("label") or f"Installation {index + 1}").strip()

    def _serialize_secret(self, value: Any) -> str:
        return self.encrypt(str(value))

    def _deserialize_secret(self, value: Any) -> str:
        return self.decrypt(str(value)) if value else ""

    async def load_installations(self, plugin_cls: type) -> list[dict[str, Any]]:
        """Return every configured installation for one plugin.

        The default installation reuses the historical flat plugin_config rows.
        Extra installations are stored as one JSON document so old deployments do
        not need a schema migration.
        """
        base = await self.load(plugin_cls)
        base.update({
            "__base_slug": plugin_cls.slug,
            "__runtime_slug": plugin_cls.slug,
            "__installation_id": DEFAULT_INSTALLATION_ID,
            "__installation_name": "Standard",
            "__master_enabled": bool(base.get("__enabled")),
        })
        stored = await db.plugin_config_all(plugin_cls.slug)
        raw_items = stored.get(INSTALLATIONS_KEY, {}).get("value") or []
        installs = [base]
        if not isinstance(raw_items, list):
            raw_items = []
        master_enabled = bool(base.get("__enabled"))
        for i, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            install_id = str(item.get("id") or uuid4().hex[:8]).strip()
            cfg: dict[str, Any] = {}
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            secrets = item.get("secrets") if isinstance(item.get("secrets"), dict) else {}
            for f in plugin_cls.config_fields:
                if f.secret:
                    raw = self._deserialize_secret(secrets.get(f.key))
                    if raw in (None, ""):
                        raw = f.default
                else:
                    raw = values.get(f.key, f.default)
                cfg[f.key] = f.coerce(raw)
            enabled = bool(item.get("enabled", False)) and master_enabled
            cfg.update({
                "__enabled": enabled,
                "__instance_enabled": bool(item.get("enabled", False)),
                "__master_enabled": master_enabled,
                "__base_slug": plugin_cls.slug,
                "__runtime_slug": self._installation_slug(plugin_cls, install_id),
                "__installation_id": install_id,
                "__installation_name": self._installation_label(plugin_cls, item, i),
            })
            installs.append(cfg)
        return installs

    async def installation_meta(self, plugin_cls: type, install_id: str) -> dict[str, bool]:
        if install_id == DEFAULT_INSTALLATION_ID:
            return await self.stored_meta(plugin_cls)
        stored = await db.plugin_config_all(plugin_cls.slug)
        raw_items = stored.get(INSTALLATIONS_KEY, {}).get("value") or []
        item = next((x for x in raw_items if isinstance(x, dict) and str(x.get("id")) == install_id), {})
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        secrets = item.get("secrets") if isinstance(item.get("secrets"), dict) else {}
        return {f.key: bool((secrets if f.secret else values).get(f.key)) for f in plugin_cls.config_fields}

    async def save_installation(
        self,
        plugin_cls: type,
        install_id: str,
        values: dict[str, Any],
        enabled: bool,
        *,
        name: str = "",
    ) -> str:
        if install_id in ("", DEFAULT_INSTALLATION_ID):
            await self.save(plugin_cls, values, enabled)
            return DEFAULT_INSTALLATION_ID
        stored = await db.plugin_config_all(plugin_cls.slug)
        raw_items = stored.get(INSTALLATIONS_KEY, {}).get("value") or []
        if not isinstance(raw_items, list):
            raw_items = []
        if install_id == "__new__":
            install_id = uuid4().hex[:8]
            raw_items.append({"id": install_id, "name": name or "Neue Installation", "enabled": enabled})
        item = next((x for x in raw_items if isinstance(x, dict) and str(x.get("id")) == install_id), None)
        if item is None:
            item = {"id": install_id}
            raw_items.append(item)
        item["name"] = (name or item.get("name") or "Installation").strip()
        item["enabled"] = bool(enabled)
        item.setdefault("values", {})
        item.setdefault("secrets", {})
        for f in plugin_cls.config_fields:
            submitted = values.get(f.key)
            if f.secret:
                if submitted in (None, "", SECRET_SENTINEL):
                    continue
                item["secrets"][f.key] = self._serialize_secret(submitted)
            else:
                item["values"][f.key] = f.coerce(submitted)
        await db.plugin_config_set(plugin_cls.slug, INSTALLATIONS_KEY, raw_items, False)
        try:
            await db.audit(
                "config_change", actor="owner",
                detail={"plugin": plugin_cls.slug, "installation": install_id, "enabled": bool(enabled)},
            )
        except Exception:  # noqa: BLE001
            pass
        return install_id

    async def delete_installation(self, plugin_cls: type, install_id: str) -> None:
        if install_id == DEFAULT_INSTALLATION_ID:
            return
        stored = await db.plugin_config_all(plugin_cls.slug)
        raw_items = stored.get(INSTALLATIONS_KEY, {}).get("value") or []
        if not isinstance(raw_items, list):
            return
        raw_items = [x for x in raw_items if not (isinstance(x, dict) and str(x.get("id")) == install_id)]
        await db.plugin_config_set(plugin_cls.slug, INSTALLATIONS_KEY, raw_items, False)

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
