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
    async def send_telegram(self, chat_id: str, text: str, buttons: list[dict] | None = None) -> bool:
        if not self.s.telegram_bot_token:
            log.warning("Telegram not configured.")
            return False
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        r = await self._http.post(
            f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage", json=payload
        )
        r.raise_for_status()
        return True

    # ── Direct backends ──────────────────────────────────────────────────────────
    async def _waha(self, chat_id: str, text: str) -> bool:
        r = await self._http.post(
            f"{self.s.waha_base_url}/api/sendText",
            headers={"X-Api-Key": self.s.waha_api_key},
            json={"session": self.s.waha_session, "chatId": chat_id, "text": text},
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
