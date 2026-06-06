"""SMTP — send email through any SMTP server (Gmail, iCloud, mailbox.org, …)."""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

# Convenience presets so you don't have to know host/port.
PRESETS = {
    "gmail.com": ("smtp.gmail.com", 465),
    "googlemail.com": ("smtp.gmail.com", 465),
    "icloud.com": ("smtp.mail.me.com", 587),
    "me.com": ("smtp.mail.me.com", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587),
    "hotmail.com": ("smtp-mail.outlook.com", 587),
    "mailbox.org": ("smtp.mailbox.org", 465),
    "gmx.net": ("mail.gmx.net", 465),
    "web.de": ("smtp.web.de", 587),
}


class SmtpPlugin(Plugin):
    slug = "smtp"
    name = "E-Mail senden (SMTP)"
    description = "Versende E-Mails über einen beliebigen SMTP-Server (Gmail, iCloud, …)."
    category = PluginCategory.COMMS
    icon = "✉️"
    config_fields = [
        ConfigField("email", "Absender-Adresse", required=True, type=FieldType.TEXT,
                    help="Bekannte Anbieter (gmail/icloud/outlook/…) werden automatisch erkannt"),
        ConfigField("password", "Passwort / App-Passwort", required=True, secret=True,
                    help="Bei Gmail/iCloud zwingend ein App-Passwort, nicht das normale Passwort"),
        ConfigField("host", "SMTP-Server", help="Leer lassen für Auto-Erkennung"),
        ConfigField("port", "Port", type=FieldType.NUMBER, default=465,
                    help="465 (SSL) oder 587 (STARTTLS) — bei Auto-Erkennung egal"),
        ConfigField("from_name", "Anzeigename", default="ASTRA"),
    ]

    def _resolve(self) -> tuple[str, int]:
        host = self.get("host")
        port = int(self.get("port", 465) or 465)
        if not host:
            domain = (self.get("email", "").split("@")[-1] or "").lower()
            if domain in PRESETS:
                host, port = PRESETS[domain]
        return host, port

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        host, port = self._resolve()
        if not host:
            raise RuntimeError("Kein SMTP-Server – Host eintragen oder bekannten Anbieter nutzen.")
        msg = EmailMessage()
        msg["From"] = f"{self.get('from_name', 'ASTRA')} <{self.get('email')}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                s.login(self.get("email"), self.get("password"))
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(self.get("email"), self.get("password"))
                s.send_message(msg)

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        host, port = self._resolve()
        if not host:
            return HealthStatus.not_configured("SMTP-Server nicht erkannt — bitte Host eintragen.")

        def _probe():
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=12) as s:
                    s.login(self.get("email"), self.get("password"))
            else:
                with smtplib.SMTP(host, port, timeout=12) as s:
                    s.starttls(context=ctx)
                    s.login(self.get("email"), self.get("password"))

        try:
            await asyncio.to_thread(_probe)
            return HealthStatus.ok(f"Login auf {host}:{port} erfolgreich.")
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _send(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "SMTP ist deaktiviert."
            await asyncio.to_thread(self._send_sync, args["to"], args.get("subject", ""),
                                    args.get("body", ""))
            return f"E-Mail an {args['to']} gesendet."

        return [Tool(
            name="send_email",
            description="Sende eine E-Mail über SMTP.",
            parameters={"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"},
                "body": {"type": "string"}}, "required": ["to", "body"]},
            handler=_send, owner_only=True, source=self.slug,
        )]
