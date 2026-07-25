"""HomeLab-Ausführung — Befehle auf Allow-List-Hosts, hinter der Command-Policy.

Bahrians Regel, hier umgesetzt: **Allow-List läuft autonom, alles andere fragt,
Destruktives wird blockiert.** Die Entscheidung selbst trifft `ops_policy` — dasselbe
Modul, das auch der Job-Planer benutzt, damit es nur EINE Grenze gibt und nicht zwei
Meinungen darüber, was gefährlich ist.

Standardmäßig aus. Ohne SSH-Bibliothek oder ohne konfigurierte Hosts ist alles ein
sauberer No-op statt eines halb funktionierenden Zustands.
"""
from __future__ import annotations

import asyncio
import logging
import shlex

from ... import db, ops_policy
from ...config import get_settings
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.ops")

try:  # optional — ohne asyncssh bleibt das Plugin lesbar deaktiviert
    import asyncssh  # type: ignore
except Exception:  # noqa: BLE001
    asyncssh = None


class OpsExecPlugin(Plugin):
    slug = "ops_exec"
    name = "HomeLab Ausführung"
    description = "Befehle auf freigegebenen Hosts ausführen — Allow-List autonom, Rest fragt."
    category = PluginCategory.INFRA_AI
    icon = "🛠️"
    config_fields = [
        ConfigField("hosts", "Erlaubte Hosts", required=True,
                    help="Kommagetrennt: name=user@host:port, z. B. pve=root@192.168.178.10"),
        ConfigField("ssh_key_path", "Pfad zum SSH-Key", required=False,
                    default="/srv/data/.ssh/id_ed25519",
                    help="Im brain_data-Volume ablegen, damit er Updates überlebt"),
        ConfigField("password", "SSH-Passwort (falls kein Key)", FieldType.PASSWORD,
                    required=False, secret=True),
        ConfigField("timeout", "Timeout (Sekunden)", FieldType.NUMBER, default=30),
    ]

    # ── Host-Verzeichnis ─────────────────────────────────────────────────────
    def hosts(self) -> dict[str, str]:
        """{name: 'user@host:port'} aus dem Konfigurationsfeld."""
        out: dict[str, str] = {}
        for entry in str(self.get("hosts") or "").split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            name, target = entry.split("=", 1)
            if name.strip() and target.strip():
                out[name.strip().lower()] = target.strip()
        return out

    def resolve_host(self, name: str) -> tuple[str, str, int] | None:
        """'pve' → (user, host, port). None wenn nicht freigegeben."""
        target = self.hosts().get((name or "").strip().lower())
        if not target:
            return None
        user, _, rest = target.partition("@")
        if not rest:
            user, rest = "root", target
        host, _, port = rest.partition(":")
        return user, host, int(port) if port.isdigit() else 22

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        if asyncssh is None:
            return HealthStatus.error(
                "asyncssh ist nicht installiert — Ausführung deaktiviert. "
                "In cortex/pyproject.toml als Extra 'ops' vorgesehen."
            )
        names = list(self.hosts())
        if not names:
            return HealthStatus.not_configured("Keine Hosts freigegeben.")
        return HealthStatus.ok(f"{len(names)} Host(s) freigegeben: {', '.join(names)}")

    # ── Ausführung ───────────────────────────────────────────────────────────
    async def run(self, host_name: str, command: str) -> dict:
        """Führt aus. Prüft NICHT die Policy — das tut der Aufrufer bewusst vorher."""
        if asyncssh is None:
            return {"ok": False, "output": "asyncssh nicht installiert."}
        target = self.resolve_host(host_name)
        if not target:
            return {"ok": False, "output": f"Host '{host_name}' ist nicht freigegeben."}
        if get_settings().astra_dry_run:
            log.info("[DRY_RUN] ops %s: %s", host_name, command)
            return {"ok": True, "output": f"[Trockenlauf] {command}"}
        user, host, port = target
        opts: dict = {"username": user, "port": port, "known_hosts": None}
        if key := self.get("ssh_key_path"):
            opts["client_keys"] = [key]
        if pw := self.get("password"):
            opts["password"] = pw
        try:
            async with asyncssh.connect(host, **opts) as conn:
                res = await asyncio.wait_for(
                    conn.run(command, check=False), timeout=float(self.get("timeout") or 30))
            out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
            return {"ok": res.exit_status == 0, "output": out.strip()[:4000],
                    "exit": res.exit_status}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "output": f"SSH-Fehler: {e}"}

    def tools(self) -> list[Tool]:
        async def _hosts(args: dict, ctx: ToolContext) -> str:
            names = self.hosts()
            if not names:
                return tool_result(ok=False, source=self.slug,
                                   summary="Keine Hosts freigegeben.")
            return tool_result(ok=True, source=self.slug,
                               summary="Freigegebene Hosts: " + ", ".join(sorted(names)),
                               data={"hosts": sorted(names)})

        async def _check(args: dict, ctx: ToolContext) -> str:
            """Was WÜRDE mit diesem Befehl passieren — ohne ihn auszuführen."""
            command = str(args.get("command") or "")
            decision, reason = ops_policy.classify(command)
            return tool_result(ok=True, source=self.slug,
                               summary=f"{command!r}: {ops_policy.describe(command)}",
                               data={"decision": decision, "reason": reason})

        async def _exec(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug,
                                   summary="HomeLab-Ausführung ist nicht aktiviert.")
            host = str(args.get("host") or "").strip()
            command = str(args.get("command") or "").strip()
            if not host or not command:
                return tool_result(ok=False, source=self.slug,
                                   summary="'host' und 'command' sind erforderlich.")
            if not self.resolve_host(host):
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Host '{host}' ist nicht freigegeben. Erlaubt: "
                            f"{', '.join(sorted(self.hosts())) or '(keine)'}.")

            decision, reason = ops_policy.classify(command)
            if decision == ops_policy.BLOCK:
                await db.audit("ops_blocked", detail={"host": host, "command": command,
                                                      "reason": reason})
                return tool_result(
                    ok=False, source=self.slug,
                    summary=f"Blockiert: {reason}. Diesen Befehl führe ich nicht aus — "
                            "wenn du das wirklich willst, mach es selbst auf der Konsole.",
                    data={"decision": "block", "reason": reason})

            if decision == ops_policy.APPROVE:
                # Reuse the existing approval + Telegram-button machinery.
                approval_id = await db.create_approval(
                    thread_id=None, contact_id=None, kind="ops_exec",
                    question=f"{host}: {command}",
                    payload={"host": host, "command": command, "plugin": self.slug},
                )
                s = get_settings()
                if s.telegram_enabled and s.telegram_owner_chat_id:
                    from ...channels import get_channels
                    await get_channels().send_telegram(
                        s.telegram_owner_chat_id,
                        f"🛠️ ASTRA möchte auf *{host}* ausführen:\n`{command}`\n\n"
                        f"Grund der Rückfrage: {reason}",
                        buttons=[{"text": "✅ Ausführen", "callback_data": f"apv:{approval_id}:yes"},
                                 {"text": "❌ Nein", "callback_data": f"apv:{approval_id}:no"}],
                    )
                    return tool_result(
                        ok=True, source=self.slug,
                        summary=f"Freigabe angefragt ({reason}). Nach deinem ✅ führe ich "
                                f"`{command}` auf {host} aus.",
                        data={"decision": "approve", "approval_id": str(approval_id)})
                return tool_result(
                    ok=False, source=self.slug,
                    summary="Telegram ist nicht konfiguriert — ohne Bestätigungskanal "
                            "führe ich nichts aus, das eine Freigabe braucht.")

            result = await self.run(host, command)
            await db.audit("ops_exec", detail={"host": host, "command": command,
                                               "ok": result["ok"]})
            return tool_result(ok=result["ok"], source=self.slug,
                               summary=f"{host}$ {command}\n{result['output'][:1500]}",
                               data={"decision": "allow", "exit": result.get("exit")})

        return [
            Tool(name="ops_hosts", description="Liste die für Ausführung freigegebenen Hosts.",
                 parameters={"type": "object", "properties": {}},
                 handler=_hosts, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status", "list"]),
            Tool(name="ops_check",
                 description="Prüfe OHNE Ausführung, wie ein Befehl eingestuft wird "
                             "(autonom / Freigabe / blockiert).",
                 parameters={"type": "object", "properties": {"command": {"type": "string"}},
                             "required": ["command"]},
                 handler=_check, owner_only=True, source=self.slug,
                 safety="private_read", intents=["status"]),
            Tool(name="ops_exec",
                 description=(
                     "Führe einen Befehl auf einem freigegebenen HomeLab-Host aus. "
                     "Harmlose Statusbefehle laufen sofort; alles andere fragt Bahrian per "
                     "Telegram; Destruktives wird abgelehnt. host=Name aus ops_hosts."
                 ),
                 parameters={"type": "object", "properties": {
                     "host": {"type": "string"}, "command": {"type": "string"}},
                     "required": ["host", "command"]},
                 handler=_exec, owner_only=True, source=self.slug,
                 safety="destructive", intents=["control"]),
        ]

    async def world_nodes(self) -> list:
        """Hosts als Weltmodell-Knoten — damit „starte den Container auf dem Proxmox"
        über denselben Resolver läuft wie Räume und Geräte."""
        from ... import world
        if not self.enabled:
            return []
        return [world.Node(id=f"host:{name}", kind="host", names=(name,),
                           caps=("exec",), source=self.slug)
                for name in self.hosts()]
