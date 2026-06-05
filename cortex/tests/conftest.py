"""Shared test fixtures — in-memory replacements for the Postgres-backed db layer
so web/auth/config_store/plugin tests run without a database."""
from __future__ import annotations

import os
import tempfile

import pytest

from app import db
from app.config import get_settings


@pytest.fixture
def memdb(monkeypatch):
    """Monkeypatch the async db helpers used by auth/config_store/admin with an
    in-memory store. Returns the settings dict for assertions."""
    settings_store: dict = {}
    plugin_store: dict[tuple[str, str], dict] = {}

    async def get_setting(key, default=None):
        return settings_store.get(key, default)

    async def set_setting(key, value):
        settings_store[key] = value

    async def audit(*a, **k):
        pass

    async def plugin_config_all(slug):
        return {k: v for (s, k), v in plugin_store.items() if s == slug}

    async def plugin_config_set(slug, key, value, is_secret=False):
        plugin_store[(slug, key)] = {"value": value, "is_secret": is_secret}

    async def plugin_config_delete(slug, key):
        plugin_store.pop((slug, key), None)

    monkeypatch.setattr(db, "get_setting", get_setting)
    monkeypatch.setattr(db, "set_setting", set_setting)
    monkeypatch.setattr(db, "audit", audit)
    monkeypatch.setattr(db, "plugin_config_all", plugin_config_all)
    monkeypatch.setattr(db, "plugin_config_set", plugin_config_set)
    monkeypatch.setattr(db, "plugin_config_delete", plugin_config_delete)

    # deterministic config dir + fresh auth caches
    monkeypatch.setenv("BRAIN_DATA_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("ASTRA_CONFIG_KEY", "")
    get_settings.cache_clear()
    from app.web import auth as a
    a._serializer = None
    a._attempts.clear()
    from app import config_store as cs
    cs._store = None
    return settings_store
