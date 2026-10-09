"""Sprachausgabe (TTS) für das Wand-Display — OpenAI `audio.speech`, MP3 als base64.

Nutzt denselben OpenAI-Key wie Chat und Whisper (Admin-Override vor .env). Ohne Key
ist alles ein No-op: `synthesize()` liefert None, das Display zeigt dann nur Text.
Modell, Stimme und optionale Sprechanweisung kommen aus `app_settings["display"]`,
sonst aus ASTRA_TTS_MODEL / ASTRA_TTS_VOICE.
"""
from __future__ import annotations

import base64
import logging
import re
import time

from openai import AsyncOpenAI

from .. import db, usage
from ..config import get_settings

log = logging.getLogger("astra.display.speech")

MIME = "audio/mpeg"
MAX_CHARS = 1500           # ein Display-Satz, kein Hörbuch (API-Limit wäre 4096)
VOICES = ("nova", "alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "sage", "shimmer", "verse")
DEFAULT_INSTRUCTIONS = ("Sprich natürliches, warmes Deutsch in ruhigem Tempo — wie ein persönlicher "
                        "Assistent zu Hause, nicht wie eine Ansage.")

_clients: dict[str, AsyncOpenAI] = {}


def openai_key() -> str:
    try:
        from ..models import providers
        key = providers()["openai"].api_key
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    return get_settings().openai_api_key


def available() -> bool:
    return bool(openai_key())


def _client() -> AsyncOpenAI | None:
    key = openai_key()
    if not key:
        return None
    if key not in _clients:
        _clients.clear()
        _clients[key] = AsyncOpenAI(api_key=key)
    return _clients[key]


async def voice_settings() -> dict:
    s = get_settings()
    try:
        appset = await db.get_setting("app_settings", {}) or {}
    except Exception:  # noqa: BLE001
        appset = {}
    cfg = appset.get("display") if isinstance(appset.get("display"), dict) else {}
    return {
        "model": str(cfg.get("tts_model") or s.astra_tts_model or "gpt-4o-mini-tts"),
        "voice": str(cfg.get("tts_voice") or s.astra_tts_voice or "nova"),
        "instructions": str(cfg.get("tts_instructions") or DEFAULT_INSTRUCTIONS),
    }


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]")


def speakable(text: str) -> str:
    """Markdown, Links und Emojis raus — so, wie man es laut sagen würde."""
    t = _MD_LINK.sub(r"\1", str(text or ""))
    t = _URL.sub("", t)
    t = re.sub(r"```.*?```", "", t, flags=re.DOTALL)
    t = re.sub(r"^\s{0,3}(#{1,6}|[-*•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = t.replace("**", "").replace("__", "").replace("`", "").replace("*", "").replace("_", " ")
    t = _EMOJI.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        t = cut[: max(cut.rfind(". "), cut.rfind("\n"), MAX_CHARS // 2) + 1].strip()
    return t


async def synthesize(text: str) -> dict | None:
    """Text → {mime, b64} (MP3) oder None (kein Key, leerer Text, API-Fehler)."""
    spoken = speakable(text)
    client = _client()
    if not spoken or client is None:
        return None
    cfg = await voice_settings()
    started = time.perf_counter()
    kwargs = {"model": cfg["model"], "voice": cfg["voice"], "input": spoken, "response_format": "mp3"}
    if cfg["instructions"] and not cfg["model"].startswith("tts-1"):
        # extra_body statt Keyword: funktioniert auch mit älteren SDK-Versionen.
        kwargs["extra_body"] = {"instructions": cfg["instructions"]}
    try:
        resp = await client.audio.speech.create(**kwargs)
        audio = resp.content if hasattr(resp, "content") else bytes(resp)
    except Exception as e:  # noqa: BLE001
        log.warning("TTS fehlgeschlagen: %s", e)
        return None
    try:
        await usage.record(usage.build_event(
            provider="openai", model=cfg["model"], role="tts", prompt_tokens=None,
            completion_tokens=0, started=started, fallback_in=spoken))
    except Exception:  # noqa: BLE001
        pass
    return {"mime": MIME, "b64": base64.b64encode(audio).decode()}
