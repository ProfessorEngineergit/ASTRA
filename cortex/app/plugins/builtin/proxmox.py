"""Proxmox VE — VM/LXC status and power control."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.proxmox")


class ProxmoxPlugin(Plugin):
    slug = "proxmox"
    name = "Proxmox VE"
    description = "VMs und Container auf dem Proxmox-Server überwachen und steuern."
    category = PluginCategory.INFRA_AI
    icon = "🖥️"
    config_fields = [
        ConfigField("base_url", "Proxmox URL", required=True,
                    default="https://192.168.178.100:8006",
                    help="Basis-URL des Proxmox Web-UI"),
        ConfigField("node", "Node-Name", default="pve",
                    help="Proxmox Node-Name (Standard: pve)"),
        ConfigField("token_id", "Token-ID", required=True,
                    help="Format: user@pam!tokenname"),
        ConfigField("token_secret", "Token-Secret", FieldType.PASSWORD,
                    required=True, secret=True),
    ]

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=(self.get("base_url") or "https://192.168.178.100:8006").rstrip("/"),
            headers={"Authorization": f"PVEAPIToken={self.get('token_id')}={self.get('token_secret')}"},
            verify=False,
            timeout=15,
        )

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/api2/json/version")
                r.raise_for_status()
                ver = r.json().get("data", {}).get("version", "?")
            return HealthStatus.ok(f"Proxmox {ver} erreichbar.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            node = self.get("node", "pve")
            try:
                async with self._client() as c:
                    qemu_r = await c.get(f"/api2/json/nodes/{node}/qemu")
                    lxc_r = await c.get(f"/api2/json/nodes/{node}/lxc")
                    qemu_r.raise_for_status()
                    lxc_r.raise_for_status()
                lines = ["**Proxmox VMs & Container**"]
                for vm in qemu_r.json().get("data", []):
                    cpu = round(vm.get("cpu", 0) * 100, 1)
                    mem_pct = round(vm.get("mem", 0) / max(vm.get("maxmem", 1), 1) * 100, 1)
                    lines.append(
                        f"[VM {vm['vmid']}] {vm['name']} — {vm['status']} "
                        f"CPU {cpu}% RAM {mem_pct}%"
                    )
                for ct in lxc_r.json().get("data", []):
                    cpu = round(ct.get("cpu", 0) * 100, 1)
                    mem_pct = round(ct.get("mem", 0) / max(ct.get("maxmem", 1), 1) * 100, 1)
                    lines.append(
                        f"[LXC {ct['vmid']}] {ct['name']} — {ct['status']} "
                        f"CPU {cpu}% RAM {mem_pct}%"
                    )
                return "\n".join(lines) if len(lines) > 1 else "Keine VMs/Container gefunden."
            except Exception as e:
                return f"Proxmox-Fehler: {e}"

        async def _action(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            vmid = args.get("vmid")
            action = args.get("action", "").lower()
            vtype = args.get("type", "lxc").lower()
            if not vmid or action not in ("start", "stop", "reboot"):
                return "vmid und action (start|stop|reboot) sind erforderlich."
            node = self.get("node", "pve")
            try:
                async with self._client() as c:
                    r = await c.post(
                        f"/api2/json/nodes/{node}/{vtype}/{vmid}/status/{action}"
                    )
                    r.raise_for_status()
                return f"{vtype.upper()} {vmid}: {action} ausgeführt."
            except Exception as e:
                return f"Proxmox-Aktion fehlgeschlagen: {e}"

        return [
            Tool(
                name="proxmox_status",
                description="Alle VMs und LXC-Container auf dem Proxmox-Node auflisten.",
                parameters={"type": "object", "properties": {}},
                handler=_status, owner_only=True, source=self.slug,
            ),
            Tool(
                name="proxmox_vm_action",
                description="Eine VM oder einen LXC-Container starten, stoppen oder neu starten.",
                parameters={"type": "object", "properties": {
                    "vmid": {"type": "integer", "description": "VM/Container-ID"},
                    "action": {"type": "string", "enum": ["start", "stop", "reboot"]},
                    "type": {"type": "string", "enum": ["lxc", "qemu"],
                             "description": "lxc (Standard) oder qemu"},
                }, "required": ["vmid", "action"]},
                handler=_action, owner_only=True, source=self.slug,
            ),
        ]
