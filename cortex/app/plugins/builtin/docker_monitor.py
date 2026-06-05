"""Docker — container status and lifecycle control via Docker API."""
from __future__ import annotations

import logging

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.docker")


def _make_client(docker_url: str) -> httpx.AsyncClient:
    """Create an AsyncClient for Docker — unix socket or TCP."""
    if docker_url.startswith("http+unix://"):
        socket_path = docker_url.replace("http+unix://", "")
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://localhost",
            timeout=15,
        )
    return httpx.AsyncClient(base_url=docker_url, timeout=15)


class DockerMonitorPlugin(Plugin):
    slug = "docker"
    name = "Docker Monitor"
    description = "Container-Status überwachen und Container starten/stoppen/neustarten."
    category = PluginCategory.INFRA_AI
    icon = "🐳"
    config_fields = [
        ConfigField("docker_url", "Docker URL",
                    default="http+unix:///var/run/docker.sock",
                    help="Unix socket (http+unix:///var/run/docker.sock) oder tcp://host:2375"),
        ConfigField("tls_verify", "TLS verifizieren", FieldType.BOOL, default=False),
    ]

    def _client(self) -> httpx.AsyncClient:
        return _make_client(self.get("docker_url") or "http+unix:///var/run/docker.sock")

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            async with self._client() as c:
                r = await c.get("/info")
                r.raise_for_status()
            name = r.json().get("Name", "?")
            containers = r.json().get("Containers", "?")
            return HealthStatus.ok(f"Docker ({name}) — {containers} Container.")
        except Exception as e:
            return HealthStatus.error(str(e))

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            try:
                async with self._client() as c:
                    r = await c.get("/containers/json", params={"all": "1"})
                    r.raise_for_status()
                containers = r.json()
                if not containers:
                    return "Keine Container gefunden."
                lines = ["**Docker Container**"]
                for ct in containers:
                    names = ", ".join(n.lstrip("/") for n in ct.get("Names", []))
                    status = ct.get("Status", "?")
                    image = ct.get("Image", "?")
                    lines.append(f"- {names}: {status} ({image})")
                return "\n".join(lines)
            except Exception as e:
                return f"Docker-Fehler: {e}"

        async def _action(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Plugin deaktiviert."
            name = args.get("container_name", "").strip()
            action = args.get("action", "").lower()
            if not name or action not in ("start", "stop", "restart"):
                return "container_name und action (start|stop|restart) sind erforderlich."
            try:
                async with self._client() as c:
                    r = await c.post(f"/containers/{name}/{action}")
                if r.status_code in (204, 200):
                    return f"Container '{name}': {action} ausgeführt."
                return f"Docker-Fehler: HTTP {r.status_code} — {r.text[:200]}"
            except Exception as e:
                return f"Docker-Fehler: {e}"

        return [
            Tool(
                name="docker_status",
                description="Alle Docker-Container mit Status und Image anzeigen.",
                parameters={"type": "object", "properties": {}},
                handler=_status, owner_only=True, source=self.slug,
            ),
            Tool(
                name="docker_action",
                description="Docker-Container starten, stoppen oder neu starten.",
                parameters={"type": "object", "properties": {
                    "container_name": {"type": "string",
                                       "description": "Container-Name oder -ID"},
                    "action": {"type": "string",
                               "enum": ["start", "stop", "restart"]},
                }, "required": ["container_name", "action"]},
                handler=_action, owner_only=True, source=self.slug,
            ),
        ]
