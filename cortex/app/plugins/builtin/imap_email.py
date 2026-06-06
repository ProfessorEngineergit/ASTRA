"""Generic IMAP — unread digest for any mailbox (Nextcloud, mailbox.org, …)."""
from __future__ import annotations

import asyncio
import email
import imaplib
from email.header import decode_header

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


def _decode(raw: str) -> str:
    out = ""
    for txt, enc in decode_header(raw or ""):
        out += txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt
    return out


class ImapEmailPlugin(Plugin):
    slug = "imap_email"
    name = "IMAP E-Mail"
    description = "Beliebiges IMAP-Postfach: ungelesene E-Mails als Digest."
    category = PluginCategory.COMMS
    icon = "📬"
    config_fields = [
        ConfigField("host", "IMAP-Server", required=True, help="z. B. imap.mailbox.org"),
        ConfigField("port", "Port", type=FieldType.NUMBER, default=993),
        ConfigField("username", "Benutzername", required=True),
        ConfigField("password", "Passwort", required=True, secret=True),
        ConfigField("mailbox", "Postfach", default="INBOX"),
        ConfigField("max_items", "Max. E-Mails", type=FieldType.NUMBER, default=8),
    ]

    def _digest_sync(self) -> list[dict]:
        m = imaplib.IMAP4_SSL(self.get("host"), int(self.get("port", 993)))
        try:
            m.login(self.get("username"), self.get("password"))
            m.select(self.get("mailbox", "INBOX"))
            _t, data = m.search(None, "UNSEEN")
            ids = data[0].split()[-int(self.get("max_items", 8)):]
            out = []
            for i in reversed(ids):
                _t, d = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                msg = email.message_from_bytes(d[0][1])
                out.append({"from": _decode(msg.get("From", "")),
                            "subject": _decode(msg.get("Subject", "(kein Betreff)"))})
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            items = await asyncio.to_thread(self._digest_sync)
            return HealthStatus.ok(f"Verbunden — {len(items)} ungelesen.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            items = await asyncio.to_thread(self._digest_sync)
        except Exception:  # noqa: BLE001
            return None
        if not items:
            return None
        lines = "\n".join(f"  • {i['subject']} — {i['from']}" for i in items[:5])
        return f"📬 {len(items)} ungelesene E-Mails:\n{lines}"

    def tools(self) -> list[Tool]:
        async def _read(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "IMAP-Plugin ist deaktiviert."
            items = await asyncio.to_thread(self._digest_sync)
            if not items:
                return "Keine ungelesenen E-Mails."
            return "\n".join(f"• {i['subject']} — {i['from']}" for i in items)

        return [Tool(
            name="imap_unread",
            description="Liste ungelesene E-Mails im IMAP-Postfach.",
            parameters={"type": "object", "properties": {}},
            handler=_read, owner_only=True, source=self.slug,
        )]
