"""Model gateway.

Single choke point for all LLM access. Today it is OpenAI-only; tomorrow we can
add a local Ollama path or Claude behind the SAME interface without touching the
agent loop. Swap the import to `langfuse.openai` to get automatic tracing.
"""
from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

log = logging.getLogger("astra.models")


class TriageResult(BaseModel):
    mode: str          # auto | defer | ask
    sensitivity: str   # none | freebusy | details
    reason: str = ""


class ModelGateway:
    def __init__(self) -> None:
        s = get_settings()
        self._s = s
        self._client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=s.openai_api_key) if s.openai_enabled else None
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _require(self) -> AsyncOpenAI:
        if self._client is None:
            raise RuntimeError("OPENAI_API_KEY not set — model gateway disabled.")
        return self._client

    # ── Tool-calling chat (used by the agent loop) ────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.4,
    ) -> Any:
        client = self._require()
        kwargs: dict[str, Any] = {
            "model": model or self._s.openai_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message

    # ── Structured triage (cheap pre-step) ────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def triage(self, system: str, user: str) -> TriageResult:
        client = self._require()
        completion = await client.beta.chat.completions.parse(
            model=self._s.openai_model_small,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=TriageResult,
            temperature=0,
        )
        parsed = completion.choices[0].message.parsed
        return parsed or TriageResult(mode="defer", sensitivity="details", reason="parse-fallback")

    # ── Rolling summary (cheap; keeps long threads in budget) ─────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def summarize(self, prior_summary: str, new_turns: str) -> str:
        client = self._require()
        prompt = (
            "Fasse den Gesprächsverlauf kompakt zusammen (max. 8 Sätze). Behalte Namen, "
            "offene Fragen, Zusagen und Fakten. Bisherige Zusammenfassung:\n"
            f"{prior_summary or '(keine)'}\n\nNeue Nachrichten:\n{new_turns}"
        )
        resp = await client.chat.completions.create(
            model=self._s.openai_model_small,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()


_gateway: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
