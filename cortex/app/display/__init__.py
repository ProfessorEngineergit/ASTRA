"""OpenBoard-Display-Kanal — ASTRA auf dem Wand-Touch-Display.

    hub      In-Prozess-Ereignisbus (SSE), drop-oldest pro Verbindung
    cards    Kartenschema, Board-Operationen, Extraktion aus Tool-Ergebnissen
    speech   TTS (OpenAI audio.speech, MP3/base64)
    service  Token, Einstellungen, Sitzungen, Gesprächs-Turn, Glance, Alarme
    api      FastAPI-Router /display/v1/* (Bearer-Token, LAN only)

Protokoll: „ASTRA-Display-Protokoll v1“ (OpenBoard docs/ARCHITECTURE.md).
"""
from .hub import get_hub  # noqa: F401
