"""HTTP-Schnittstelle „ASTRA-Display-Protokoll v1“ unter /display/v1/*.

Einziger Client ist der OpenBoard-Controller (Node, Server-zu-Server im LAN) mit
`Authorization: Bearer <Display-Token>`. Admin-Cookie und X-Astra-Secret zählen
hier ausdrücklich NICHT. Der Pfad wird bewusst nicht über Caddy veröffentlicht.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__
from . import service, speech
from .hub import Subscriber, format_sse, get_hub

log = logging.getLogger("astra.display.api")

PING_SECONDS = 20.0
_FAIL_MAX = 20
_FAIL_WINDOW = 300.0
_failures: dict[str, list[float]] = {}


def _peer(request: Request) -> str:
    return request.client.host if request.client else "?"


async def require_display(request: Request) -> None:
    """Bearer-Token prüfen (zeitkonstant). Fehlversuche pro IP werden begrenzt."""
    ip = _peer(request)
    now = time.monotonic()
    hist = [t for t in _failures.get(ip, []) if now - t < _FAIL_WINDOW]
    _failures[ip] = hist
    if len(hist) >= _FAIL_MAX:
        raise HTTPException(status_code=429, detail="Zu viele Fehlversuche — später erneut.")
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not await service.check_token(token.strip()):
        hist.append(now)
        raise HTTPException(status_code=401, detail="Display-Token fehlt oder ist falsch.",
                            headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/display/v1", tags=["display"], dependencies=[Depends(require_display)])


@router.get("/hello")
async def hello():
    return service.hello()


@router.post("/message")
async def message(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"detail": "JSON erwartet."}, status_code=400)
    try:
        return await service.handle_message(body)
    except service.BadRequest as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status)


@router.get("/glance")
async def glance():
    return await service.glance()


@router.post("/tts")
async def tts(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = str((body or {}).get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return JSONResponse({"detail": "text fehlt."}, status_code=400)
    if len(text) > 4000:
        return JSONResponse({"detail": "text ist zu lang (max. 4000 Zeichen)."}, status_code=413)
    if not speech.available():
        return JSONResponse({"detail": "Sprachausgabe ist nicht eingerichtet (kein OpenAI-Key)."},
                            status_code=503)
    out = await speech.synthesize(text)
    if out is None:
        return JSONResponse({"detail": "Sprachausgabe fehlgeschlagen."}, status_code=502)
    return out


async def event_stream(request: Request, *, ping_seconds: float = PING_SECONDS):
    """SSE-Generator: `hello` zuerst, dann Hub-Ereignisse, `ping` alle 20 s bei Ruhe."""
    hub = get_hub()
    sub: Subscriber = hub.subscribe(peer=_peer(request), agent=request.headers.get("user-agent", ""))
    try:
        yield format_sse("hello", {"version": __version__})
        while True:
            try:
                event, data = await asyncio.wait_for(sub.queue.get(), timeout=ping_seconds)
            except TimeoutError:
                if await request.is_disconnected():
                    break
                yield format_sse("ping", {})
                continue
            yield format_sse(event, data)
    finally:
        hub.unsubscribe(sub)


@router.get("/events")
async def events(request: Request):
    return StreamingResponse(
        event_stream(request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
