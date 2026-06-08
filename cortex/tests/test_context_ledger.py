from __future__ import annotations

import asyncio

from app.config import get_settings
from app.context_ledger import record_interaction


def test_group_participant_gets_own_context_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    asyncio.run(record_interaction(
        channel="waha",
        thread_id="waha:klasse@g.us",
        handle="klasse@g.us",
        role="user",
        text="Kann Bahrian morgen?",
        display="Klassenchat",
        meta={
            "is_group": True,
            "group_id": "klasse@g.us",
            "participant_handle": "49123@c.us",
            "participant_display": "Max",
            "participant_username": "max",
            "relationship": "Mitschueler",
        },
    ))

    root = tmp_path / "secretary"
    assert (root / "groups" / "waha_klasse@g.us.md").exists()
    person_file = root / "contacts" / "waha_49123@c.us.md"
    assert person_file.exists()
    text = person_file.read_text(encoding="utf-8")
    assert "Max" in text
    assert "max" in text
    assert "Mitschueler" in text
    assert "Known From Groups" in text
    assert "Context Capsule" in text
