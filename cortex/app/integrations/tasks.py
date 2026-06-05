"""Google Tasks — added via an n8n tool workflow.

Architecture contract: cortex owns no OAuth. It POSTs to the n8n workflow
`tool/google_tasks_add` (which holds the Google credential) with the shared
secret. Import n8n/tools/google_tasks_add.json and connect a Google credential.
"""
from __future__ import annotations

import logging

import httpx

from ..config import get_settings

log = logging.getLogger("astra.tasks")


class GoogleTasks:
    def __init__(self) -> None:
        self.s = get_settings()

    @property
    def enabled(self) -> bool:
        return self.s.google_tasks_enabled

    async def add(self, title: str, notes: str = "", due: str | None = None) -> bool:
        """Create a task. `due` is RFC-3339 (e.g. 2026-06-06T00:00:00Z) or None."""
        if not self.enabled:
            return False
        if self.s.astra_dry_run:
            log.info("[DRY_RUN] Google Task: %s (due=%s)", title, due)
            return True
        payload = {
            "list": self.s.google_tasks_list,
            "title": title,
            "notes": notes,
            "due": due,
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{self.s.n8n_base_url}/webhook/tool/google_tasks_add",
                headers={"X-Astra-Secret": self.s.cortex_shared_secret},
                json=payload,
            )
            r.raise_for_status()
            return True


_tasks: GoogleTasks | None = None


def get_tasks() -> GoogleTasks:
    global _tasks
    if _tasks is None:
        _tasks = GoogleTasks()
    return _tasks
