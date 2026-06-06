"""Gmail — unread digest (IMAP) + send (SMTP) via an App-Password.

Uses an app-specific password (myaccount.google.com → Sicherheit → App-Passwörter),
so no OAuth dance is needed. All blocking imap/smtp work runs in a thread.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
from email.header import decode_header
from email.message import EmailMessage

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory


def _decode(raw: str) -> str:
    parts = decode_header(raw or "")
    out = ""
    for txt, enc in parts:
        out += txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt
    return out


class GmailPlugin(Plugin):
    slug = "gmail"
    name = "Gmail"
    description = "Ungelesene E-Mails zusammenfassen und versenden (App-Passwort)."
    category = PluginCategory.COMMS
    icon = "📧"
    config_fields = [
        ConfigField("email", "Gmail-Adresse", required=True, type=FieldType.TEXT),
        ConfigField("app_password", "App-Passwort", required=True, secret=True,
                    help="myaccount.google.com → Sicherheit → App-Passwörter (NICHT dein normales Passwort)"),
        ConfigField("max_items", "Max. E-Mails im Digest", type=FieldType.NUMBER, default=8),
    ]

    def _digest_sync(self) -> list[dict]:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        try:
            m.login(self.get("email"), self.get("app_password"))
            m.select("INBOX")
            _typ, data = m.search(None, "UNSEEN")
            ids = data[0].split()[-int(self.get("max_items", 8)):]
            out = []
            for i in reversed(ids):
                _t, d = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                msg = email.message_from_bytes(d[0][1])
                out.append({"from": _decode(msg.get("From", "")),
                            "subject": _decode(msg.get("Subject", "(kein Betreff)"))})
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.get("email")
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(self.get("email"), self.get("app_password"))
            s.send_message(msg)

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            items = await asyncio.to_thread(self._digest_sync)
            return HealthStatus.ok(f"Verbunden — {len(items)} ungelesene E-Mails.")
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
        return f"📧 {len(items)} ungelesene E-Mails:\n{lines}"

    def tools(self) -> list[Tool]:
        async def _read(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Gmail ist deaktiviert."
            items = await asyncio.to_thread(self._digest_sync)
            if not items:
                return "Keine ungelesenen E-Mails."
            return "\n".join(f"• {i['subject']} — {i['from']}" for i in items)

        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Gmail ist deaktiviert."
            await asyncio.to_thread(self._send_sync, args["to"], args.get("subject", ""),
                                    args.get("body", ""))
            return f"E-Mail an {args['to']} gesendet."

        return [
            Tool(name="gmail_unread",
                 description="Liste ungelesene Gmail-E-Mails (Betreff + Absender).",
                 parameters={"type": "object", "properties": {}},
                 handler=_read, owner_only=True, source=self.slug),
            Tool(name="gmail_send",
                 description="Sende eine E-Mail über Gmail.",
                 parameters={"type": "object", "properties": {
                     "to": {"type": "string"}, "subject": {"type": "string"},
                     "body": {"type": "string"}}, "required": ["to", "body"]},
                 handler=_send, owner_only=True, source=self.slug),
        ]
