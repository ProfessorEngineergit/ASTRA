"""Model gateway — ein Engpass für allen LLM-Zugriff, mit Rollen statt Modellnamen.

Der Code fragt nie nach „gpt-4o", sondern nach einer ROLLE. Nur die Zuordnung
Rolle → (Anbieter, Modell) ist konfigurierbar; damit ist „neuer Anbieter" ein
Datensatz und kein Code.

    small  — Triage jeder eingehenden Nachricht, Zusammenfassungen, Briefing-Intro
    medium — normales Gespräch MIT Tool-Calling (der heiße Pfad)
    heavy  — Planen, Analyse, HomeLab-Jobs
    code   — Code schreiben/patchen (z. B. OpenAI Codex)
    osint  — Recherche mit einem bewusst weniger restriktiven Modell

Portabilitäts-Vertrag: **OpenAI-kompatibel**. OpenRouter, Ollama, Groq, DeepSeek,
Together, LM Studio und vLLM sprechen alle `/v1/chat/completions`, also genügt ein
Client mit `base_url` + Key für sie alle. Anthropic spricht ein anderes Format und
läuft deshalb über einen eigenen Pfad — und kann (noch) kein Tool-Calling in
diesem Loop, weil `agent.py` OpenAI-geformte Messages baut. Das Gateway weigert
sich, eine Tool-Anfrage dorthin zu routen, statt kryptisch zu scheitern.

Kein stiller Fallback: fällt der konfigurierte Anbieter aus, scheitert der Aufruf
laut (Bahrians Entscheidung — Vorhersagbarkeit vor Verfügbarkeit).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import get_settings

log = logging.getLogger("astra.models")

SMALL, MEDIUM, HEAVY, CODE, OSINT = "small", "medium", "heavy", "code", "osint"
ROLES = (SMALL, MEDIUM, HEAVY, CODE, OSINT)


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str            # "openai_compat" | "anthropic"
    base_url: str = ""   # leer = offizieller OpenAI-Endpoint
    api_key: str = ""
    tools: bool = True   # unterstützt OpenAI-Tool-Calling in diesem Loop?

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self.kind == "openai_compat" and bool(self.base_url)


class ModelError(RuntimeError):
    """Anbieter fehlt, ist nicht konfiguriert oder kann das Verlangte nicht."""


def _builtin_providers() -> dict[str, Provider]:
    s = get_settings()
    return {
        "openai": Provider("openai", "openai_compat", "", s.openai_api_key, True),
        "openrouter": Provider("openrouter", "openai_compat", s.openrouter_base_url,
                               s.openrouter_api_key, True),
        # Ollama spricht seit v0.2 den OpenAI-kompatiblen /v1-Pfad; der Key ist ein Dummy.
        "ollama": Provider("ollama", "openai_compat", s.ollama_base_url, "ollama", True),
        "anthropic": Provider("anthropic", "anthropic", "", s.anthropic_api_key, False),
    }


def _default_roles() -> dict[str, dict[str, str]]:
    s = get_settings()
    heavy = ({"provider": "anthropic", "model": s.anthropic_model} if s.anthropic_enabled
             else {"provider": "openai", "model": s.openai_model})
    return {
        SMALL: {"provider": "openai", "model": s.openai_model_small},
        MEDIUM: {"provider": "openai", "model": s.openai_model},
        HEAVY: heavy,
        # Bewusst leer: den Codex-/Coding-Modellnamen wählt Bahrian selbst,
        # statt dass ich hier einen rate. Leer → fällt auf medium zurück.
        CODE: {"provider": "", "model": ""},
        OSINT: {"provider": "ollama", "model": s.osint_model},
    }


# Live aus den Web-Einstellungen gesetzt (app_settings["models"]).
_PROVIDER_OVERRIDES: dict[str, dict] = {}
_ROLE_OVERRIDES: dict[str, dict[str, str]] = {}


def set_model_config(cfg: dict | None) -> None:
    """Anbieter- und Rollenzuordnung live aus dem Admin übernehmen."""
    global _PROVIDER_OVERRIDES, _ROLE_OVERRIDES
    cfg = cfg or {}
    _PROVIDER_OVERRIDES = dict(cfg.get("providers") or {})
    _ROLE_OVERRIDES = {k: dict(v) for k, v in (cfg.get("roles") or {}).items()
                       if isinstance(v, dict)}


def providers() -> dict[str, Provider]:
    out = _builtin_providers()
    for name, raw in _PROVIDER_OVERRIDES.items():
        if not isinstance(raw, dict):
            continue
        base = out.get(name)
        out[name] = Provider(
            name=name,
            kind=str(raw.get("kind") or (base.kind if base else "openai_compat")),
            base_url=str(raw.get("base_url") or (base.base_url if base else "")),
            api_key=str(raw.get("api_key") or (base.api_key if base else "")),
            tools=bool(raw.get("tools", base.tools if base else True)),
        )
    return out


def role_target(role: str) -> tuple[Provider, str]:
    """(Anbieter, Modell) für eine Rolle. Wirft ModelError, wenn nichts passt."""
    roles = {**_default_roles(), **_ROLE_OVERRIDES}
    entry = roles.get(role) or {}
    name, model = str(entry.get("provider") or ""), str(entry.get("model") or "")
    if role == MEDIUM and _MODEL_OVERRIDE:      # historisches Freitextfeld gewinnt
        model = _MODEL_OVERRIDE
    if role == CODE and not name:               # kein Coding-Modell gesetzt → medium
        return role_target(MEDIUM)
    if _ECONOMY and role == MEDIUM:             # Sparmodus: eine Stufe runter
        return role_target(SMALL)
    if not name or not model:
        raise ModelError(f"Für die Rolle '{role}' ist kein Anbieter/Modell konfiguriert.")
    provider = providers().get(name)
    if provider is None:
        raise ModelError(f"Unbekannter Anbieter '{name}' für Rolle '{role}'.")
    if not provider.configured:
        raise ModelError(f"Anbieter '{name}' ist nicht konfiguriert (Key/URL fehlt).")
    return provider, model


def describe_roles() -> str:
    """Für die Selbstauskunft im Admin/Chat."""
    rows = []
    for role in ROLES:
        try:
            p, m = role_target(role)
            rows.append(f"{role}: {p.name}/{m}" + ("" if p.tools else "  (ohne Tool-Calling)"))
        except ModelError as e:
            rows.append(f"{role}: — ({e})")
    return "\n".join(rows)

# Runtime model override set from the web settings (DB) — wins over the .env default.
_MODEL_OVERRIDE: str | None = None
# Sparmodus: when on, ordinary chat runs on the small model. Until now this toggle
# was stored and displayed but read by nothing at all.
_ECONOMY = False


def set_model_override(model: str | None) -> None:
    """Pick the chat model live from the admin UI (None → fall back to .env)."""
    global _MODEL_OVERRIDE
    _MODEL_OVERRIDE = (model or "").strip() or None


def get_model_override() -> str | None:
    return _MODEL_OVERRIDE


def set_economy(enabled: bool) -> None:
    global _ECONOMY
    _ECONOMY = bool(enabled)


def get_economy() -> bool:
    return _ECONOMY


class TriageResult(BaseModel):
    mode: str          # auto | defer | ask
    sensitivity: str   # none | freebusy | details
    reason: str = ""


class ModelGateway:
    def __init__(self) -> None:
        self._s = get_settings()
        self._clients: dict[str, AsyncOpenAI] = {}

    @property
    def enabled(self) -> bool:
        """True when at least the medium role can be served."""
        try:
            role_target(MEDIUM)
            return True
        except ModelError:
            return False

    def _openai_client(self, provider: Provider) -> AsyncOpenAI:
        """Cached client per provider — one class covers every OpenAI-compatible host."""
        if provider.name not in self._clients:
            kwargs: dict[str, Any] = {"api_key": provider.api_key or "none"}
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
            self._clients[provider.name] = AsyncOpenAI(**kwargs)
        return self._clients[provider.name]

    # ── Tool-calling chat (used by the agent loop) ────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.4,
        role: str = MEDIUM,
    ) -> Any:
        provider, role_model = role_target(role)
        if tools and not provider.tools:
            raise ModelError(
                f"Rolle '{role}' zeigt auf {provider.name}, der in diesem Loop kein "
                "Tool-Calling kann. Wähle dort einen OpenAI-kompatiblen Anbieter."
            )
        client = self._openai_client(provider)
        kwargs: dict[str, Any] = {
            "model": model or role_model,
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
        provider, model = role_target(SMALL)
        client = self._openai_client(provider)
        try:
            completion = await client.beta.chat.completions.parse(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format=TriageResult,
                temperature=0,
            )
            parsed = completion.choices[0].message.parsed
        except Exception:  # noqa: BLE001
            # Structured-output parsing is an OpenAI extension; local/other backends
            # may not have it. Fall back to plain JSON so triage still works there.
            log.debug("structured triage unsupported on %s — plain JSON fallback", provider.name)
            resp = await client.chat.completions.create(
                model=model, temperature=0,
                messages=[{"role": "system", "content": system +
                           '\nAntworte NUR als JSON: {"mode":"auto|defer|ask",'
                           '"sensitivity":"none|freebusy|details","reason":"…"}'},
                          {"role": "user", "content": user}],
            )
            import json as _json
            raw = (resp.choices[0].message.content or "").strip().strip("`")
            raw = raw.removeprefix("json").strip()
            try:
                parsed = TriageResult(**_json.loads(raw))
            except Exception:  # noqa: BLE001
                parsed = None
        return parsed or TriageResult(mode="defer", sensitivity="details", reason="parse-fallback")

    # ── Role: reason (the "big brain" for HomeLab jobs & hard analysis) ───────
    # Deliberately single-shot (no tool loop): the agent loop still speaks OpenAI's
    # message shape, and porting it is a separate, riskier change. This gives us a
    # second provider today without touching that hot path.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def complete(self, role: str, system: str, user: str, *, max_tokens: int = 2000) -> str:
        """Single-shot Textantwort einer Rolle. Deckt heavy/code/osint ab und ist der
        Weg, auf dem auch Anbieter ohne Tool-Calling (Anthropic) nutzbar sind."""
        provider, model = role_target(role)
        if provider.kind == "anthropic":
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": provider.api_key,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": model, "max_tokens": max_tokens, "system": system,
                          "messages": [{"role": "user", "content": user}]},
                )
                r.raise_for_status()
                blocks = r.json().get("content") or []
                return "".join(b.get("text", "") for b in blocks
                               if b.get("type") == "text").strip()
        client = self._openai_client(provider)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return (resp.choices[0].message.content or "").strip()

    async def reason(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        """Das große Gehirn (Rolle `heavy`) — Planen, Analyse, HomeLab-Jobs."""
        return await self.complete(HEAVY, system, user, max_tokens=max_tokens)

    # ── Rolling summary (cheap; keeps long threads in budget) ─────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    async def summarize(self, prior_summary: str, new_turns: str) -> str:
        provider, model = role_target(SMALL)
        client = self._openai_client(provider)
        prompt = (
            "Fasse den Gesprächsverlauf kompakt zusammen (max. 8 Sätze). Behalte Namen, "
            "offene Fragen, Zusagen und Fakten. Bisherige Zusammenfassung:\n"
            f"{prior_summary or '(keine)'}\n\nNeue Nachrichten:\n{new_turns}"
        )
        resp = await client.chat.completions.create(
            model=model,
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
