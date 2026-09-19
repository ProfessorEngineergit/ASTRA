"""Signal-Empfang — signal-cli pusht im json-rpc-Modus KEINE Webhooks (README: offener Punkt).

Stattdessen hört cortex den WebSocket `ws://signal-cli:8080/v1/receive/<nummer>` ab und
schickt jede Nachricht durch dieselbe Normalisierung wie der Webhook `/ingress/signal`.
So kommt Signal endlich in den Kontext (Journal, Kapseln, Sekretär).

`normalize()` ist rein und deckt die Fälle ab, die signal-cli liefert:
  • Nachricht einer anderen Person (dataMessage) — Einzel- und Gruppenchat
  • eigene, vom Handy gesendete Nachricht (syncMessage.sentMessage) → `force_owner`, damit
    ASTRA wie bei WhatsApp `fromMe` zurücktritt, statt in Bahrians Konversation zu grätschen
  • @Erwähnungen (in Signal ein Platzhalterzeichen im Text) → wird zu „@Bahrian“, damit der
    Gruppen-Trigger „nur bei @Erwähnung“ greift
  • Antwort auf eine ASTRA-Nachricht (quote) → gilt als angesprochen
Empfangsquittungen, Tippen-Anzeigen usw. haben keinen Text und werden verworfen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote

log = logging.getLogger("astra.signal")

_MENTION_CHAR = "￼"


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))[-9:]


def _is_us(ref: dict, own_number: str, own_uuid: str = "") -> bool:
    nums = {_digits(ref.get("number")), _digits(ref.get("authorNumber")), _digits(ref.get("author"))}
    uuids = {str(ref.get("uuid") or ""), str(ref.get("authorUuid") or "")}
    return (bool(_digits(own_number)) and _digits(own_number) in nums) or \
        (bool(own_uuid) and own_uuid in uuids)


def expand_mentions(text: str, mentions: list[dict], own_number: str, owner_name: str) -> tuple[str, bool]:
    """Erwähnungs-Platzhalter durch „@Name“ ersetzen. → (Text, ist ASTRA/Bahrian erwähnt?)"""
    mentioned = False
    out = text
    for m in sorted(mentions or [], key=lambda x: int(x.get("start") or 0), reverse=True):
        start, length = int(m.get("start") or 0), int(m.get("length") or 1)
        us = _is_us(m, own_number)
        mentioned = mentioned or us
        label = f"@{owner_name}" if us else f"@{m.get('name') or 'jemand'}"
        if 0 <= start <= len(out):
            out = out[:start] + label + out[start + length:]
    return out.replace(_MENTION_CHAR, "").strip() if _MENTION_CHAR in out else out, mentioned


def normalize(body: dict, own_number: str, owner_name: str = "Bahrian") -> dict | None:
    """signal-cli-Envelope → Argumente für `brain.handle_inbound` (oder None = ignorieren)."""
    env = (body or {}).get("envelope") or {}
    data = env.get("dataMessage")
    sent = (env.get("syncMessage") or {}).get("sentMessage")
    from_me = False
    if sent:
        msg, from_me = sent, True
    elif data:
        msg = data
    else:
        return None
    text = msg.get("message") or ""
    text, mentioned = expand_mentions(text, msg.get("mentions") or [], own_number, owner_name)
    if not text.strip():
        return None

    group = msg.get("groupInfo") or {}
    group_id = group.get("groupId") or group.get("group_id")
    source = env.get("sourceNumber") or env.get("source") or ""
    if from_me:
        peer = group_id or msg.get("destinationNumber") or msg.get("destination") or ""
        participant = own_number
        display = None
    else:
        peer = group_id or source
        participant = source
        display = env.get("sourceName")
    if not peer:
        return None

    quote_ref = msg.get("quote") or {}
    replied_to_us = bool(quote_ref) and _is_us(quote_ref, own_number)
    return {
        "channel": "signal",
        "sender_handle": peer,
        "text": text,
        "sender_display": group.get("name") or display,
        "force_owner": True if from_me else None,
        "thread_meta": {
            "is_group": bool(group_id), "group_id": group_id, "group_name": group.get("name"),
            "participant_handle": participant, "participant_display": display,
            "participant_username": env.get("sourceUuid") or display,
            "username": env.get("sourceUuid") or display,
            "own_id": own_number, "mentioned_us": mentioned, "reply_to_us": replied_to_us or mentioned,
            "source_tag": "from Signal",
        },
    }


async def listener() -> None:
    """Hintergrund-Task: WebSocket abhören, Nachrichten an brain schicken, bei Abbruch neu verbinden."""
    from . import brain
    from .config import get_settings
    s = get_settings()
    number = (s.signal_phone_number or "").strip()
    if not number:
        return
    try:
        import websockets
    except Exception:  # noqa: BLE001
        log.warning("Signal-Empfang aus: Paket 'websockets' fehlt.")
        return
    base = s.signal_base_url.rstrip("/")
    url = ("wss" if base.startswith("https") else "ws") + base[base.index("://"):] + \
        f"/v1/receive/{quote(number, safe='')}"
    backoff = 3
    await asyncio.sleep(15)                     # signal-cli hochkommen lassen
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30, max_size=8_000_000) as ws:
                log.info("Signal-Empfang verbunden (%s).", number)
                backoff = 3
                async for raw in ws:
                    try:
                        kwargs = normalize(json.loads(raw), number, s.astra_owner_name)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not kwargs:
                        continue
                    try:
                        await brain.handle_inbound(**kwargs)
                    except Exception:  # noqa: BLE001 — eine kaputte Nachricht darf den Empfang nie beenden
                        log.exception("Signal-Nachricht konnte nicht verarbeitet werden.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Signal-Empfang getrennt (%s) — neuer Versuch in %ss.", str(e)[:120], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
