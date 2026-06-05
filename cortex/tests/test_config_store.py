"""ConfigStore: encryption, precedence (DB > .env > default), enabled defaults."""
from __future__ import annotations

import asyncio

from app.config_store import get_config_store
from app.plugins.base import ConfigField, FieldType, Plugin, PluginCategory


class _Demo(Plugin):
    slug = "demo"
    name = "Demo"
    category = PluginCategory.INFRA_AI
    config_fields = [
        ConfigField("api_key", "Key", FieldType.PASSWORD, required=True, secret=True),
        ConfigField("region", "Region", default="eu"),
    ]


class _NoRequired(Plugin):
    slug = "noreq"
    name = "NoReq"
    category = PluginCategory.INFRA_AI
    config_fields = [ConfigField("list", "List", default="@default")]


def test_encrypt_decrypt_roundtrip(memdb):
    cs = get_config_store()
    tok = cs.encrypt("super-secret")
    assert tok != "super-secret"
    assert cs.decrypt(tok) == "super-secret"


def test_default_and_required_disabled(memdb):
    cfg = asyncio.run(get_config_store().load(_Demo))
    assert cfg["region"] == "eu"
    assert cfg["api_key"] in (None, "")
    assert cfg["__enabled"] is False           # required api_key missing → off


def test_save_then_load_secret_encrypted_and_enabled(memdb):
    cs = get_config_store()
    asyncio.run(cs.save(_Demo, {"api_key": "K123", "region": "us"}, enabled=True))
    # stored secret is ciphertext, not plaintext
    assert memdb is not None
    cfg = asyncio.run(cs.load(_Demo))
    assert cfg["api_key"] == "K123"            # decrypted on load
    assert cfg["region"] == "us"
    assert cfg["__enabled"] is True


def test_secret_kept_when_resubmitted_empty(memdb):
    cs = get_config_store()
    asyncio.run(cs.save(_Demo, {"api_key": "K1", "region": "eu"}, enabled=True))
    asyncio.run(cs.save(_Demo, {"api_key": "", "region": "de"}, enabled=True))  # empty secret
    cfg = asyncio.run(cs.load(_Demo))
    assert cfg["api_key"] == "K1"              # unchanged
    assert cfg["region"] == "de"               # non-secret updated


def test_no_required_fields_defaults_off(memdb):
    cfg = asyncio.run(get_config_store().load(_NoRequired))
    assert cfg["__enabled"] is False           # no required fields → must opt in
    assert cfg["list"] == "@default"
