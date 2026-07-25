"""Telegram message splitting (W5): a long briefing must arrive in order, in <=4096
char chunks, split on natural boundaries."""
from app.channels import _split_message


def test_short_message_is_one_chunk():
    assert _split_message("hallo") == ["hallo"]


def test_long_message_splits_under_limit():
    text = "\n\n".join(f"Absatz {i} " + "x" * 500 for i in range(30))
    chunks = _split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    # nichts geht verloren (bis auf zusammengefasste Whitespaces an den Nähten)
    assert "Absatz 0" in chunks[0] and "Absatz 29" in chunks[-1]


def test_split_prefers_paragraph_boundary():
    text = "A" * 4000 + "\n\n" + "B" * 4000
    chunks = _split_message(text)
    assert chunks[0].endswith("A")
    assert chunks[1].startswith("B")
