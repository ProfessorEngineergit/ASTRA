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

from . import db
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
        self._last_errors: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Public API ─────────────────────────────────────────────────────────────
    async def send(self, channel: str, to: str, text: str) -> bool:
        self._last_errors.pop(channel, None)
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
            message = str(e)[:500] or type(e).__name__
            self._last_errors[channel] = message
            log.error("send failed (%s): %s", channel, message)
            return False

    def last_error(self, channel: str) -> str:
        """Last safe transport error for a user-facing failure explanation."""
        return self._last_errors.get(channel, "")

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
            # WAHA documents @c.us for outbound messages even when some engines
            # expose the same contact as @s.whatsapp.net.
            return to.replace("@s.whatsapp.net", "@c.us")
        digits = "".join(ch for ch in to if ch.isdigit())
        return f"{digits}@c.us" if digits else to

    async def _waha_runtime_config(self) -> tuple[str, str, str]:
        """Prefer the Secretary UI installation; fall back to environment values."""
        installation: dict[str, Any] = {}
        try:
            appset = await db.get_setting("app_settings", {}) or {}
            secretary = appset.get("secretary") if isinstance(appset, dict) else {}
            installs = secretary.get("installations") if isinstance(secretary, dict) else {}
            candidate = installs.get("waha") if isinstance(installs, dict) else {}
            if isinstance(candidate, dict):
                installation = candidate
        except Exception:  # noqa: BLE001
            log.debug("Could not load Secretary WAHA installation.", exc_info=True)
        base_url = str(installation.get("base_url") or self.s.waha_base_url or "").rstrip("/")
        session = str(installation.get("session") or self.s.waha_session or "default")
        api_key = str(installation.get("api_key") or self.s.waha_api_key or "")
        return base_url, session, api_key

    @staticmethod
    def _waha_http_error(response: httpx.Response, *, session: str) -> str:
        status = response.status_code
        if status in {401, 403}:
            return f"WAHA lehnt den API-Key ab (HTTP {status})."
        if status == 404:
            return f"WAHA-Session '{session}' wurde nicht gefunden (HTTP 404)."
        if status == 422:
            detail = ""
            try:
                data = response.json()
                raw = data.get("message") or data.get("error") if isinstance(data, dict) else ""
                if isinstance(raw, (str, int, float)):
                    detail = " " + " ".join(str(raw).split())[:240]
            except (ValueError, TypeError):
                pass
            return f"WAHA lehnt die Nachricht ab (HTTP 422).{detail}"
        return f"WAHA antwortet mit HTTP {status}."

    async def _waha(self, chat_id: str, text: str) -> bool:
        base_url, session, api_key = await self._waha_runtime_config()
        if not base_url:
            raise RuntimeError("WAHA Base URL fehlt.")
        if not api_key:
            raise RuntimeError("WAHA API-Key fehlt.")
        headers = {"X-Api-Key": api_key}

        target = self._waha_chat_id(chat_id)
        if not target or "@" not in target:
            raise RuntimeError(
                f"Für '{chat_id}' ist keine gültige WhatsApp-Nummer hinterlegt.")
        r = await self._http.post(
            f"{base_url}/api/sendText",
            headers=headers,
            json={"session": session, "chatId": target, "text": text},
        )
        if not r.is_success:
            message = self._waha_http_error(r, session=session)
            # Scoped WAHA keys may allow sending but not reading session data.
            # Query the state only after a failed send and treat it as optional.
            if r.status_code in {404, 409, 422}:
                try:
                    status_response = await self._http.get(
                        f"{base_url}/api/sessions/{session}", headers=headers)
                    if status_response.is_success:
                        status_data = status_response.json()
                        state = str(
                            status_data.get("status") or status_data.get("state") or ""
                        ).upper()
                        if state and state not in {"WORKING", "CONNECTED", "AUTHENTICATED"}:
                            message = (
                                f"WAHA-Session '{session}' ist {state} "
                                f"(HTTP {r.status_code}).")
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError(message)
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
