"""Transport layer — puts bytes on each messaging channel.

Two backends (config ASTRA_SEND_BACKEND):
  • "direct" — cortex calls WAHA / signal-cli / Telegram APIs itself (easy first run).
  • "n8n"    — cortex POSTs to the visual tool/send_* workflows (the n8n layer you like).
Telegram (your control channel) is always sent directly for low latency + buttons.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger("astra.channels")


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """Split a long message into <=limit chunks, preferring paragraph then line breaks."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class Channels:
    def __init__(self) -> None:
        self.s = get_settings()
        self._http = httpx.AsyncClient(timeout=20)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Public API ─────────────────────────────────────────────────────────────
    async def send(self, channel: str, to: str, text: str) -> bool:
        if self.s.astra_dry_run:
            log.info("[DRY_RUN] → %s/%s: %s", channel, to, text)
            return True
        try:
            if channel == "telegram":
                return await self.send_telegram(to, text)
            if self.s.astra_send_backend == "n8n":
                return await self._via_n8n(channel, to, text)
            if channel == "waha":
                return await self._waha(to, text)
            if channel == "signal":
                return await self._signal(to, text)
            log.warning("Unknown channel %s", channel)
            return False
        except Exception as e:  # noqa: BLE001
            log.error("send failed (%s → %s): %s", channel, to, e)
            return False

    # ── Telegram (control + approvals) ───────────────────────────────────────────
    async def send_telegram(
        self, chat_id: str, text: str, buttons: list[dict] | None = None,
        *, parse_mode: str | None = None,
    ) -> bool:
        if self.s.astra_dry_run:
            log.info("[DRY_RUN] → telegram/%s: %s", chat_id, text)
            return True
        if not self.s.telegram_bot_token:
            log.warning("Telegram not configured.")
            return False
        # Telegram caps a message at 4096 chars. Split on paragraph/line/hard
        # boundaries so a long briefing still gets through in order.
        chunks = _split_message(text) if len(text) > 4096 else [text]
        ok = True
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if buttons and i == len(chunks) - 1:   # buttons only on the last chunk
                payload["reply_markup"] = {"inline_keyboard": [buttons]}
            ok = await self._post_telegram(payload) and ok
        return ok

    async def _post_telegram(self, payload: dict[str, Any]) -> bool:
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        r = await self._http.post(url, json=payload)
        # A stray '_' or '*' in dynamic text can 400 under Markdown — never drop the
        # message for cosmetics: retry once as plain text so it always arrives.
        if r.status_code == 400 and payload.get("parse_mode"):
            log.warning("Telegram %s under %s — retrying as plain text.",
                        r.status_code, payload["parse_mode"])
            payload = {k: v for k, v in payload.items() if k != "parse_mode"}
            r = await self._http.post(url, json=payload)
        r.raise_for_status()
        return True

    # ── Direct backends ──────────────────────────────────────────────────────────
    @staticmethod
    def _waha_chat_id(to: str) -> str:
        """Accept a bare phone number and turn it into WAHA's <number>@c.us chatId.
        Existing JIDs (…@c.us / …@g.us) and group ids pass through untouched."""
        to = (to or "").strip()
        if "@" in to:
            return to
        digits = "".join(ch for ch in to if ch.isdigit())
        return f"{digits}@c.us" if digits else to

    async def _waha(self, chat_id: str, text: str) -> bool:
        r = await self._http.post(
            f"{self.s.waha_base_url}/api/sendText",
            headers={"X-Api-Key": self.s.waha_api_key},
            json={"session": self.s.waha_session, "chatId": self._waha_chat_id(chat_id), "text": text},
        )
        r.raise_for_status()
        return True

    async def _signal(self, recipient: str, text: str) -> bool:
        r = await self._http.post(
            f"{self.s.signal_base_url}/v2/send",
            json={
                "number": self.s.signal_phone_number,
                "recipients": [recipient],
                "message": text,
            },
        )
        r.raise_for_status()
        return True

    # ── n8n tool/send_* backend ────────────────────────────────────────────────
    async def _via_n8n(self, channel: str, to: str, text: str) -> bool:
        r = await self._http.post(
            f"{self.s.n8n_base_url}/webhook/tool/send_{channel}",
            headers={"X-Astra-Secret": self.s.cortex_shared_secret},
            json={"to": to, "text": text},
        )
        r.raise_for_status()
        return True


_channels: Channels | None = None


def get_channels() -> Channels:
    global _channels
    if _channels is None:
        _channels = Channels()
    return _channels
