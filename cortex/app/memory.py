"""Long-term memory via Mem0 (pgvector-backed).

Degrades gracefully to a no-op if mem0 isn't installed or OpenAI isn't configured,
so the rest of ASTRA always runs. The rolling *thread* summary lives in db.py /
brain.py; this is the cross-conversation semantic memory (facts about people).
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from .config import get_settings

log = logging.getLogger("astra.memory")


class MemoryLayer:
    def __init__(self) -> None:
        self._mem = None
        s = get_settings()
        if not s.openai_enabled:
            log.info("Memory layer disabled (no OpenAI key) — running no-op.")
            return
        try:
            from mem0 import Memory  # type: ignore

            u = urlparse(s.database_url)
            config = {
                "llm": {
                    "provider": "openai",
                    "config": {"model": s.openai_model_small, "api_key": s.openai_api_key},
                },
                "embedder": {
                    "provider": "openai",
                    "config": {"model": s.openai_embed_model, "api_key": s.openai_api_key},
                },
                "vector_store": {
                    "provider": "pgvector",
                    "config": {
                        "dbname": (u.path or "/cortex").lstrip("/") or "cortex",
                        "user": u.username or "astra",
                        "password": u.password or "",
                        "host": u.hostname or "localhost",
                        "port": u.port or 5432,
                        "collection_name": "astra_memory",
                    },
                },
            }
            self._mem = Memory.from_config(config)
            log.info("Mem0 memory layer initialised (pgvector).")
        except Exception as e:  # noqa: BLE001 — never let memory block boot
            log.warning("Mem0 unavailable, memory layer is a no-op: %s", e)
            self._mem = None

    @property
    def enabled(self) -> bool:
        return self._mem is not None

    async def recall(self, query: str, user_id: str, limit: int = 5) -> list[str]:
        if not self._mem:
            return []
        try:
            res = await asyncio.to_thread(
                self._mem.search, query=query, user_id=user_id, limit=limit
            )
            items = res.get("results", []) if isinstance(res, dict) else (res or [])
            return [i.get("memory", "") for i in items if isinstance(i, dict) and i.get("memory")]
        except Exception as e:  # noqa: BLE001
            log.warning("memory.recall failed: %s", e)
            return []

    async def write(self, content: str, user_id: str, metadata: dict | None = None) -> None:
        if not self._mem:
            return
        try:
            await asyncio.to_thread(
                self._mem.add, content, user_id=user_id, metadata=metadata or {}
            )
        except Exception as e:  # noqa: BLE001
            log.warning("memory.write failed: %s", e)


_memory: MemoryLayer | None = None


def get_memory() -> MemoryLayer:
    global _memory
    if _memory is None:
        _memory = MemoryLayer()
    return _memory
