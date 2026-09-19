from app import outbox


def test_echo_is_recognised_by_normalised_text_and_expires():
    outbox.reset()
    outbox.remember("--ASTRA--\nHallo  Lena,\n  ich richte es aus.", now=100.0)
    assert outbox.is_echo("--astra-- hallo lena, ich richte es aus.", now=101.0)
    assert not outbox.is_echo("Etwas ganz anderes", now=101.0)
    assert not outbox.is_echo("--ASTRA-- Hallo Lena, ich richte es aus.", now=100.0 + outbox.TTL_SECONDS + 1)
    assert not outbox.is_echo("", now=101.0)


def test_pruning_keeps_memory_bounded():
    outbox.reset()
    for i in range(700):
        outbox.remember(f"msg {i}", now=1000.0 + i)
    assert len(outbox._recent) <= 500
    assert outbox.is_echo("msg 699", now=1700.0)
    outbox.reset()


def test_channels_send_remembers_texts_for_waha_and_signal(monkeypatch):
    import asyncio
    from app.channels import Channels
    outbox.reset()
    ch = Channels()

    async def fake_waha(chat_id, text):
        return True
    monkeypatch.setattr(ch, "_waha", fake_waha)
    monkeypatch.setattr(ch.s, "astra_dry_run", False, raising=False)
    asyncio.run(ch.send("waha", "4917011111111", "Antwort von ASTRA"))
    assert outbox.is_echo("Antwort von ASTRA")
    outbox.reset()
