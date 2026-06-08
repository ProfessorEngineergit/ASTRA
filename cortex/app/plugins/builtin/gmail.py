"""Gmail — native Google OAuth, with app-password IMAP/SMTP as fallback."""
from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import smtplib
from email.header import decode_header
from email.message import EmailMessage

from ...google_oauth import google_api, google_oauth_fields, has_google_connection
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


def _decode(raw: str) -> str:
    parts = decode_header(raw or "")
    out = ""
    for txt, enc in parts:
        out += txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt
    return out


def _header(headers: list[dict], name: str) -> str:
    lname = name.lower()
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == lname), "")


class GmailPlugin(Plugin):
    slug = "gmail"
    name = "Gmail"
    description = "Ungelesene E-Mails lesen und senden, nativ per Google OAuth."
    category = PluginCategory.COMMS
    icon = "📧"
    google_scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    config_fields = [
        ConfigField("backend", "Backend", FieldType.SELECT, default="native",
                    options=["native", "app_password"],
                    help="native = Google OAuth; app_password = alter IMAP/SMTP-Fallback."),
        *google_oauth_fields(),
        ConfigField("email", "Gmail-Adresse", required=False, type=FieldType.TEXT),
        ConfigField("app_password", "App-Passwort", required=False, secret=True,
                    help="Nur fuer Backend app_password."),
        ConfigField("max_items", "Max. E-Mails im Digest", type=FieldType.NUMBER, default=8),
    ]

    def _backend(self) -> str:
        return str(self.get("backend") or "native")

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

    async def unread(self, limit: int | None = None) -> list[dict]:
        if self._backend() == "app_password":
            return await asyncio.to_thread(self._digest_sync)
        max_items = int(limit or self.get("max_items", 8) or 8)
        r = await google_api(
            self,
            "GET",
            f"{GMAIL_API}/users/me/messages",
            params={"q": "is:unread", "maxResults": max(1, min(max_items, 50))},
        )
        messages = r.json().get("messages", [])
        out = []
        for item in messages:
            detail = await google_api(
                self,
                "GET",
                f"{GMAIL_API}/users/me/messages/{item['id']}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            payload = detail.json().get("payload", {})
            headers = payload.get("headers", [])
            out.append({
                "id": item["id"],
                "from": _header(headers, "From"),
                "subject": _header(headers, "Subject") or "(kein Betreff)",
                "date": _header(headers, "Date"),
            })
        return out

    async def send_mail(self, to: str, subject: str, body: str) -> dict:
        if self._backend() == "app_password":
            await asyncio.to_thread(self._send_sync, to, subject, body)
            return {"to": to, "backend": "app_password"}
        msg = EmailMessage()
        sender = self.get("account_email") or self.get("email") or "me"
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        r = await google_api(
            self,
            "POST",
            f"{GMAIL_API}/users/me/messages/send",
            json={"raw": raw},
        )
        return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            items = await self.unread(3)
            who = self.get("account_email") or self.get("email") or "Gmail"
            return HealthStatus.ok(f"{who} verbunden; {len(items)} ungelesene E-Mails geprüft.")
        except Exception as e:  # noqa: BLE001
            if self._backend() == "native" and not has_google_connection(self.cfg):
                return HealthStatus.not_configured("Google OAuth noch nicht verbunden.")
            return HealthStatus.error(str(e))

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            items = await self.unread(5)
        except Exception:  # noqa: BLE001
            return None
        if not items:
            return None
        lines = "\n".join(f"  • {i['subject']} — {i['from']}" for i in items[:5])
        return f"📧 {len(items)} ungelesene E-Mails:\n{lines}"

    def tools(self) -> list[Tool]:
        async def _read(args: dict, ctx: ToolContext) -> str:
            items = await self.unread(int(args.get("limit") or self.get("max_items", 8) or 8))
            if not items:
                return tool_result(ok=True, summary="Keine ungelesenen E-Mails.", data=[], source=self.slug)
            return tool_result(
                ok=True,
                summary="\n".join(f"- {i['subject']} — {i['from']}" for i in items),
                data=items,
                source=self.slug,
            )

        async def _send(args: dict, ctx: ToolContext) -> str:
            data = await self.send_mail(args["to"], args.get("subject", ""), args.get("body", ""))
            return tool_result(
                ok=True,
                summary=f"E-Mail an {args['to']} gesendet.",
                data=data,
                source=self.slug,
            )

        return [
            Tool(
                name="gmail_unread",
                description="Liste ungelesene Gmail-E-Mails.",
                parameters={"type": "object", "properties": {"limit": {"type": "number"}}},
                handler=_read, owner_only=True, source=self.slug,
                safety="private_read", intents=["list", "status"],
            ),
            Tool(
                name="gmail_send",
                description="Sende eine E-Mail ueber Gmail.",
                parameters={"type": "object", "properties": {
                    "to": {"type": "string"}, "subject": {"type": "string"},
                    "body": {"type": "string"}}, "required": ["to", "body"]},
                handler=_send, owner_only=True, source=self.slug,
                safety="external_send", intents=["send"],
            ),
        ]
