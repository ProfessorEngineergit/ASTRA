"""Durable knowledge: seeding into the data dir + append, with the dir pointed at
a tmp path so updates/rebuilds never touch the real volume in tests."""
from __future__ import annotations

import importlib


def _fresh_knowledge(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    from app import config
    config.get_settings.cache_clear()
    knowledge = importlib.import_module("app.knowledge")
    importlib.reload(knowledge)
    return knowledge


def test_seed_creates_templates(tmp_path, monkeypatch):
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    knowledge.ensure_seeded()
    for name in ("persona.md", "facts.md", "routines.md", "people.md"):
        assert (tmp_path / name).exists()


def test_seed_does_not_overwrite_existing(tmp_path, monkeypatch):
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    (tmp_path / "facts.md").write_text("MEINE EIGENEN FAKTEN", encoding="utf-8")
    knowledge.ensure_seeded()
    assert (tmp_path / "facts.md").read_text(encoding="utf-8") == "MEINE EIGENEN FAKTEN"


def test_append_fact_and_owner_context(tmp_path, monkeypatch):
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    knowledge.ensure_seeded()
    assert knowledge.append_fact("Ich mag Filterkaffee", file="facts.md")
    ctx = knowledge.owner_context()
    assert "Filterkaffee" in ctx


def test_append_fact_rejects_unknown_file_falls_back(tmp_path, monkeypatch):
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    knowledge.ensure_seeded()
    assert knowledge.append_fact("x", file="../../etc/passwd")
    assert "x" in (tmp_path / "facts.md").read_text(encoding="utf-8")
