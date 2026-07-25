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


def test_owner_context_is_core_only_facts_are_retrieved(tmp_path, monkeypatch):
    """W1.5: owner_context is the always-on core (persona+routines); facts.md is no
    longer dumped wholesale — it is surfaced on demand by relevant_facts()."""
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    knowledge.ensure_seeded()
    assert knowledge.append_fact("Ich mag Filterkaffee", file="facts.md")
    # No longer in the always-on block …
    assert "Filterkaffee" not in knowledge.owner_context()
    # … but a bullet parsed into a retrievable candidate.
    candidates = knowledge.markdown_facts()
    assert any("Filterkaffee" in f.value for f in candidates)
    chosen = knowledge.score_facts(candidates, "was trinkt er, kaffee?", limit=8)
    assert any("Filterkaffee" in f.value for f in chosen)


def test_append_fact_rejects_unknown_file_falls_back(tmp_path, monkeypatch):
    knowledge = _fresh_knowledge(tmp_path, monkeypatch)
    knowledge.ensure_seeded()
    assert knowledge.append_fact("x", file="../../etc/passwd")
    assert "x" in (tmp_path / "facts.md").read_text(encoding="utf-8")
