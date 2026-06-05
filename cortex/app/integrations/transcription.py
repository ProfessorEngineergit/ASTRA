"""Whisper transcription for Telegram voice notes.

Reuses the OpenAI client/key already in cortex. Off when no key is set.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from ..config import get_settings

log = logging.getLogger("astra.transcription")


class Transcriber:
    def __init__(self) -> None:
        self.s = get_settings()
        self._client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=self.s.openai_api_key) if self.s.voice_enabled else None
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Return transcribed text, or '' on failure / when disabled."""
        if not self._client:
            return ""
        try:
            resp = await self._client.audio.transcriptions.create(
                model=self.s.whisper_model,
                file=(filename, audio),
            )
            return (getattr(resp, "text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("transcription failed: %s", e)
            return ""


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = Transcriber()
    return _transcriber
