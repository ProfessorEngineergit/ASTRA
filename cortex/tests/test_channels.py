from __future__ import annotations

import asyncio

from app.channels import Channels, _split_message


def test_short_message_is_one_chunk():
    assert _split_message("hallo") == ["hallo"]


def test_long_message_splits_under_limit():
    text = "\n\n".join(f"Absatz {i} " + "x" * 500 for i in range(30))
    chunks = _split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "Absatz 0" in chunks[0] and "Absatz 29" in chunks[-1]


def test_split_prefers_paragraph_boundary():
    text = "A" * 4000 + "\n\n" + "B" * 4000
    chunks = _split_message(text)
    assert chunks[0].endswith("A")
    assert chunks[1].startswith("B")


class _Response:
    def __init__(self, status: int, data: dict | None = None):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self._data = data or {}

    def json(self):
        return self._data


class _Http:
    def __init__(self, *, send_status: int = 200, send_data: dict | None = None,
                 get_data: dict | None = None):
        self.calls = []
        self.send_status = send_status
        self.send_data = send_data
        self.get_data = get_data or {"status": "WORKING"}

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(200, self.get_data)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(self.send_status, self.send_data)

    async def aclose(self):
        return None


def _channels_with_http(http: _Http) -> Channels:
    channels = Channels()
    asyncio.run(channels._http.aclose())
    channels._http = http
    return channels


def test_waha_send_uses_secretary_ui_installation(memdb):
    memdb["app_settings"] = {
        "secretary": {
            "installations": {
                "waha": {
                    "base_url": "http://waha-ui:3000",
                    "session": "secretary-session",
                    "api_key": "ui-secret",
                }
            }
        }
    }
    http = _Http()
    channels = _channels_with_http(http)

    ok = asyncio.run(channels.send("waha", "+49 170 1234567", "Test"))

    assert ok is True
    assert http.calls[0][1] == "http://waha-ui:3000/api/contacts/check-exists"
    assert http.calls[1][1] == "http://waha-ui:3000/api/sendText"
    assert http.calls[1][2]["headers"]["X-Api-Key"] == "ui-secret"
    assert http.calls[1][2]["json"] == {
        "session": "secretary-session",
        "chatId": "491701234567@c.us",
        "text": "Test",
    }


def test_waha_send_does_not_bypass_ui_installation_when_n8n_is_global_backend(memdb):
    memdb["app_settings"] = {
        "secretary": {"installations": {"waha": {
            "base_url": "http://waha-ui:3000", "session": "default", "api_key": "ui-secret",
        }}}
    }
    http = _Http()
    channels = _channels_with_http(http)
    object.__setattr__(channels.s, "astra_send_backend", "n8n")

    ok = asyncio.run(channels.send("waha", "+49 170 1234567", "Bestätigt"))

    assert ok is True
    assert http.calls[1][1] == "http://waha-ui:3000/api/sendText"


def test_waha_self_send_uses_live_session_identity(memdb):
    memdb["app_settings"] = {
        "secretary": {"installations": {"waha": {
            "base_url": "http://waha:3000", "session": "mine", "api_key": "secret",
        }}}
    }
    http = _Http(get_data={"id": "123456@lid"})
    channels = _channels_with_http(http)

    ok = asyncio.run(channels.send("waha", "__self__", "Test"))

    assert ok is True
    assert http.calls[0][0:2] == ("GET", "http://waha:3000/api/sessions/mine/me")
    assert http.calls[1][2]["json"]["chatId"] == "123456@lid"


def test_waha_phone_send_uses_canonical_chat_id(memdb):
    memdb["app_settings"] = {
        "secretary": {"installations": {"waha": {
            "base_url": "http://waha:3000", "session": "default", "api_key": "secret",
        }}}
    }
    http = _Http(get_data={"numberExists": True, "chatId": "987654@lid"})
    channels = _channels_with_http(http)

    ok = asyncio.run(channels.send("waha", "+49 173 3620260", "Test"))

    assert ok is True
    assert http.calls[0][2]["params"] == {"phone": "491733620260", "session": "default"}
    assert http.calls[1][2]["json"]["chatId"] == "987654@lid"


def test_waha_send_exposes_actionable_http_error(memdb):
    memdb["app_settings"] = {
        "secretary": {
            "installations": {
                "waha": {
                    "base_url": "http://waha:3000",
                    "session": "default",
                    "api_key": "secret",
                }
            }
        }
    }
    channels = _channels_with_http(_Http(
        send_status=422, send_data={"message": "Invalid chatId"}))

    ok = asyncio.run(channels.send("waha", "491701234567@s.whatsapp.net", "Test"))

    assert ok is False
    assert channels.last_error("waha") == (
        "WAHA lehnt die Nachricht ab (HTTP 422). Invalid chatId")
