"""Signal-Empfang: signal-cli-Envelopes → handle_inbound-Argumente."""
from __future__ import annotations

from app import signal_events as sig

OWN = "+4915212345678"


def _dm(text="Hallo", source="+4917011111111", name="Max"):
    return {"envelope": {"source": source, "sourceNumber": source, "sourceName": name,
                         "dataMessage": {"message": text}}}


def test_direct_message_is_normalized():
    k = sig.normalize(_dm(), OWN)
    assert k["channel"] == "signal" and k["sender_handle"] == "+4917011111111" and k["text"] == "Hallo"
    assert k["sender_display"] == "Max" and k["force_owner"] is None
    assert k["thread_meta"]["is_group"] is False and k["thread_meta"]["own_id"] == OWN


def test_group_message_uses_the_group_as_peer_and_the_sender_as_participant():
    body = _dm("Hi Leute")
    body["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "abc123==", "name": "Astroclub"}
    k = sig.normalize(body, OWN)
    assert k["sender_handle"] == "abc123==" and k["sender_display"] == "Astroclub"
    assert k["thread_meta"]["is_group"] and k["thread_meta"]["participant_handle"] == "+4917011111111"
    assert k["thread_meta"]["group_name"] == "Astroclub"


def test_own_message_from_the_phone_becomes_force_owner_so_astra_steps_back():
    body = {"envelope": {"source": OWN, "sourceNumber": OWN, "syncMessage": {"sentMessage": {
        "destinationNumber": "+4917011111111", "message": "ich antworte selbst"}}}}
    k = sig.normalize(body, OWN)
    assert k["force_owner"] is True and k["sender_handle"] == "+4917011111111"
    assert k["text"] == "ich antworte selbst"


def test_own_message_in_a_group_is_attributed_to_the_group():
    body = {"envelope": {"source": OWN, "syncMessage": {"sentMessage": {
        "message": "kurz", "groupInfo": {"groupId": "grp=="}}}}}
    k = sig.normalize(body, OWN)
    assert k["sender_handle"] == "grp==" and k["force_owner"] is True


def test_mention_of_us_becomes_at_name_and_counts_as_addressed():
    body = _dm("￼ bist du da?")
    body["envelope"]["dataMessage"]["groupInfo"] = {"groupId": "g=="}
    body["envelope"]["dataMessage"]["mentions"] = [{"start": 0, "length": 1, "number": OWN, "name": "x"}]
    k = sig.normalize(body, OWN, "Bahrian")
    assert k["text"] == "@Bahrian  bist du da?".replace("  ", " ") or k["text"].startswith("@Bahrian")
    assert k["thread_meta"]["mentioned_us"] and k["thread_meta"]["reply_to_us"]


def test_mention_of_someone_else_is_not_us():
    body = _dm("￼ hallo")
    body["envelope"]["dataMessage"]["mentions"] = [{"start": 0, "length": 1, "number": "+4919999", "name": "Tom"}]
    k = sig.normalize(body, OWN)
    assert "@Tom" in k["text"] and not k["thread_meta"]["mentioned_us"]


def test_quote_of_our_message_counts_as_addressed():
    body = _dm("ja genau")
    body["envelope"]["dataMessage"]["quote"] = {"author": OWN, "id": 1, "text": "vorher"}
    assert sig.normalize(body, OWN)["thread_meta"]["reply_to_us"] is True
    body["envelope"]["dataMessage"]["quote"] = {"author": "+4911111", "id": 1}
    assert sig.normalize(body, OWN)["thread_meta"]["reply_to_us"] is False


def test_receipts_typing_and_empty_messages_are_ignored():
    assert sig.normalize({"envelope": {"source": "+49", "receiptMessage": {"type": "DELIVERY"}}}, OWN) is None
    assert sig.normalize({"envelope": {"source": "+49", "typingMessage": {"action": "STARTED"}}}, OWN) is None
    assert sig.normalize(_dm("   "), OWN) is None
    assert sig.normalize({}, OWN) is None and sig.normalize(None, OWN) is None


def test_message_without_any_peer_is_dropped():
    assert sig.normalize({"envelope": {"dataMessage": {"message": "x"}}}, OWN) is None
