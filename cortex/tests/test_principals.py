"""Mandanten-Naht (W2): der Default-Principal verhält sich exakt wie vorher —
gleiche Settings-Keys, gleiches Datenverzeichnis. Zusätzliche Principals werden
sauber getrennt. Nur die reinen (I/O-freien) Abbildungen werden hier geprüft."""
from __future__ import annotations

import importlib

from app import db


def test_default_principal_uses_flat_setting_keys():
    # Der Default-Principal ('') darf die historischen Keys NICHT umbenennen,
    # sonst würde jede bestehende Einstellung unsichtbar.
    assert db._principal_setting_key("app_settings", "") == "app_settings"


def test_named_principal_is_namespaced():
    assert db._principal_setting_key("app_settings", "lena") == "principal:lena:app_settings"


def test_principal_dir_default_is_the_historical_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    from app import config
    config.get_settings.cache_clear()
    knowledge = importlib.import_module("app.knowledge")
    importlib.reload(knowledge)
    # Default principal → /srv/data selbst (nichts zieht um).
    assert knowledge.principal_dir("") == tmp_path
    # Benannter Principal → Unterordner, dateisystem-sicher normalisiert.
    assert knowledge.principal_dir("Lena K.") == tmp_path / "principals" / "lena_k"


def test_tool_context_defaults_to_default_principal():
    from app.tools import ToolContext
    ctx = ToolContext(thread_id="t", channel="web", contact={})
    assert ctx.principal == ""
