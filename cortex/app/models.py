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

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from . import usage
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
    requires_key: bool = False

    @property
    def configured(self) -> bool:
        if self.requires_key or self.kind == "anthropic":
            return bool(self.api_key)
        return bool(self.api_key) or self.kind == "openai_compat" and bool(self.base_url)


class ModelError(RuntimeError):
    """Anbieter fehlt, ist nicht konfiguriert oder kann das Verlangte nicht."""


def _builtin_providers() -> dict[str, Provider]:
    s = get_settings()
    return {
        "openai": Provider("openai", "openai_compat", "", s.openai_api_key, True, True),
        "openrouter": Provider("openrouter", "openai_compat", s.openrouter_base_url,
                               s.openrouter_api_key, True, True),
        # Ollama spricht seit v0.2 den OpenAI-kompatiblen /v1-Pfad; der Key ist ein Dummy.
        "ollama": Provider("ollama", "openai_compat", s.ollama_base_url, "ollama", True, False),
        "anthropic": Provider("anthropic", "anthropic", "", s.anthropic_api_key, False, True),
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
_SECRET_PREFIX = "fernet:"


def protect_api_key(value: str) -> str:
    """Encrypt a provider key before it is persisted in the general settings JSON."""
    value = str(value or "").strip()
    if not value:
        return ""
    from .config_store import get_config_store
    return _SECRET_PREFIX + get_config_store().encrypt(value)


def _reveal_api_key(value: Any) -> str:
    raw = str(value or "")
    if not raw.startswith(_SECRET_PREFIX):
        return raw  # backward compatibility for existing installs
    from .config_store import get_config_store
    return get_config_store().decrypt(raw[len(_SECRET_PREFIX):])


def set_model_config(cfg: dict | None) -> None:
    """Anbieter- und Rollenzuordnung live aus dem Admin übernehmen."""
    global _PROVIDER_OVERRIDES, _ROLE_OVERRIDES
    cfg = cfg or {}
    _PROVIDER_OVERRIDES = dict(cfg.get("providers") or {})
    _ROLE_OVERRIDES = {k: dict(v) for k, v in (cfg.get("roles") or {}).items()
                       if isinstance(v, dict)}
    gateway = globals().get("_gateway")
    if gateway is not None:
        gateway._clients.clear()


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
            api_key=_reveal_api_key(raw.get("api_key")) or (base.api_key if base else ""),
            tools=bool(raw.get("tools", base.tools if base else True)),
            requires_key=bool(raw.get("requires_key", base.requires_key if base else False)),
        )
    return out


def model_config_snapshot() -> dict[str, Any]:
    """Secret-free data for the model picker and ASTRA's own status output."""
    role_rows = {**_default_roles(), **_ROLE_OVERRIDES}
    return {
        "roles": {role: dict(role_rows.get(role) or {}) for role in ROLES},
        "providers": {
            name: {
                "kind": provider.kind,
                "base_url": provider.base_url,
                "tools": provider.tools,
                "requires_key": provider.requires_key,
                "configured": provider.configured,
            }
            for name, provider in providers().items()
        },
    }


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


def _oa_usage(resp: Any) -> tuple[int | None, int | None]:
    """Token-Zahlen aus einer OpenAI-kompatiblen Antwort (None = Anbieter liefert keine)."""
    u = getattr(resp, "usage", None)
    if not u:
        return None, None
    return getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None)


def resolve_pick(pick: dict | None, default_role: str) -> tuple[Provider, str, str]:
    """(Anbieter, Modell, Rollenlabel) für eine Modellwahl.

    `pick` kommt aus der UI (pro Chat / pro Kontakt) und ist entweder
    {"tier": "small|medium|heavy|code|osint"} oder {"provider": "openrouter", "model": "x/y"}.
    Leer/None = die Standardrolle des Aufrufs. Ein unbekannter Anbieter wirft laut."""
    pick = pick or {}
    tier = str(pick.get("tier") or "").strip().lower()
    if tier in ROLES:
        prov, model = role_target(tier)
        return prov, model, tier
    name, model = str(pick.get("provider") or "").strip().lower(), str(pick.get("model") or "").strip()
    if name and model:
        prov = providers().get(name)
        if prov is None:
            raise ModelError(f"Unbekannter Anbieter '{name}'.")
        if not prov.configured:
            raise ModelError(f"Anbieter '{name}' ist nicht konfiguriert (Key/URL fehlt).")
        return prov, model, "custom"
    prov, model = role_target(default_role)
    return prov, model, default_role


class TriageResult(BaseModel):
    mode: str          # auto | defer | ask
    sensitivity: str   # none | freebusy | details
    reason: str = ""


# Konfigurationsfehler (fehlender Key, unbekannter Anbieter, Budget) sind nach dem
# ersten Versuch nicht heilbar — nur echte Netz-/API-Fehler lohnen einen Retry.
_RETRY = retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8),
               reraise=True, retry=retry_if_not_exception_type(ModelError))


def _msgs_text(messages: list[dict[str, Any]]) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(messages)


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

    @staticmethod
    async def _gate() -> None:
        """Budget-Bremse: sperrt nur FREMDEN Verkehr, nie Bahrian selbst."""
        try:
            await usage.budget_gate(third_party=usage.is_third_party())
        except usage.BudgetExceeded as e:
            raise ModelError(str(e)) from e

    async def _fail(self, provider: Provider, model: str, role: str, started: float,
                    err: Exception) -> None:
        await usage.record(usage.build_event(
            provider=provider.name, model=model, role=role, prompt_tokens=0,
            completion_tokens=0, started=started, ok=False, error=str(err)))

    # ── Tool-calling chat (used by the agent loop) ────────────────────────────
    @_RETRY
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.4,
        role: str = MEDIUM,
        pick: dict | None = None,
    ) -> Any:
        """`pick` (pro Chat/Kontakt aus der UI) schlägt die Rolle; `model` schlägt beides."""
        await self._gate()
        provider, role_model, label = resolve_pick(pick, role)
        if tools and not provider.tools:
            raise ModelError(
                f"'{provider.name}' kann in diesem Loop kein Tool-Calling. Wähle für diesen Chat "
                "ein Modell eines OpenAI-kompatiblen Anbieters (OpenAI, OpenRouter, Ollama …)."
            )
        client = self._openai_client(provider)
        used_model = model or role_model
        kwargs: dict[str, Any] = {"model": used_model, "messages": messages,
                                  "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        started = time.perf_counter()
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            await self._fail(provider, used_model, label, started, e)
            raise
        msg = resp.choices[0].message
        pt, ct = _oa_usage(resp)
        await usage.record(usage.build_event(
            provider=provider.name, model=used_model, role=label, prompt_tokens=pt,
            completion_tokens=ct, started=started, fallback_in=_msgs_text(messages),
            fallback_out=(getattr(msg, "content", "") or "")))
        return msg

    # ── Structured triage (cheap pre-step) ────────────────────────────────────
    @_RETRY
    async def triage(self, system: str, user: str) -> TriageResult:
        await self._gate()
        provider, model = role_target(SMALL)
        client = self._openai_client(provider)
        started = time.perf_counter()
        parsed = None
        pt = ct = None
        try:
            completion = await client.beta.chat.completions.parse(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format=TriageResult,
                temperature=0,
            )
            parsed = completion.choices[0].message.parsed
            pt, ct = _oa_usage(completion)
        except Exception:  # noqa: BLE001
            # Structured-output parsing is an OpenAI extension; local/other backends
            # may not have it. Fall back to plain JSON so triage still works there.
            log.debug("structured triage unsupported on %s — plain JSON fallback", provider.name)
            try:
                resp = await client.chat.completions.create(
                    model=model, temperature=0,
                    messages=[{"role": "system", "content": system +
                               '\nAntworte NUR als JSON: {"mode":"auto|defer|ask",'
                               '"sensitivity":"none|freebusy|details","reason":"…"}'},
                              {"role": "user", "content": user}],
                )
            except Exception as e:  # noqa: BLE001
                await self._fail(provider, model, SMALL, started, e)
                raise
            pt, ct = _oa_usage(resp)
            raw = (resp.choices[0].message.content or "").strip().strip("`")
            raw = raw.removeprefix("json").strip()
            try:
                parsed = TriageResult(**json.loads(raw))
            except Exception:  # noqa: BLE001
                parsed = None
        await usage.record(usage.build_event(
            provider=provider.name, model=model, role=SMALL, prompt_tokens=pt,
            completion_tokens=ct, started=started, fallback_in=system + user, fallback_out="{}"))
        return parsed or TriageResult(mode="defer", sensitivity="details", reason="parse-fallback")

    # ── Single-shot Text einer Rolle (heavy/code/osint, auch Anthropic) ───────
    @_RETRY
    async def complete(self, role: str, system: str, user: str, *, max_tokens: int = 2000,
                       pick: dict | None = None) -> str:
        """Single-shot Textantwort. Weg, auf dem auch Anbieter ohne Tool-Calling
        (Anthropic) nutzbar sind."""
        await self._gate()
        provider, model, label = resolve_pick(pick, role)
        started = time.perf_counter()
        if provider.kind == "anthropic":
            try:
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
                    data = r.json()
            except Exception as e:  # noqa: BLE001
                await self._fail(provider, model, label, started, e)
                raise
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            u = data.get("usage") or {}
            await usage.record(usage.build_event(
                provider=provider.name, model=model, role=label,
                prompt_tokens=u.get("input_tokens"), completion_tokens=u.get("output_tokens"),
                started=started, fallback_in=system + user, fallback_out=text))
            return text
        client = self._openai_client(provider)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
            )
        except Exception as e:  # noqa: BLE001
            await self._fail(provider, model, label, started, e)
            raise
        text = (resp.choices[0].message.content or "").strip()
        pt, ct = _oa_usage(resp)
        await usage.record(usage.build_event(
            provider=provider.name, model=model, role=label, prompt_tokens=pt,
            completion_tokens=ct, started=started, fallback_in=system + user, fallback_out=text))
        return text

    async def reason(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        """Das große Gehirn (Rolle `heavy`) — Planen, Analyse, HomeLab-Jobs."""
        with usage.tag(purpose="reason"):
            return await self.complete(HEAVY, system, user, max_tokens=max_tokens)

    # ── Rolling summary (cheap; keeps long threads in budget) ─────────────────
    @_RETRY
    async def summarize(self, prior_summary: str, new_turns: str) -> str:
        await self._gate()
        provider, model = role_target(SMALL)
        client = self._openai_client(provider)
        prompt = (
            "Fasse den Gesprächsverlauf kompakt zusammen (max. 8 Sätze). Behalte Namen, "
            "offene Fragen, Zusagen und Fakten. Bisherige Zusammenfassung:\n"
            f"{prior_summary or '(keine)'}\n\nNeue Nachrichten:\n{new_turns}"
        )
        started = time.perf_counter()
        try:
            resp = await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}], temperature=0.2)
        except Exception as e:  # noqa: BLE001
            await self._fail(provider, model, SMALL, started, e)
            raise
        text = (resp.choices[0].message.content or "").strip()
        pt, ct = _oa_usage(resp)
        with usage.tag(purpose=usage.current().get("purpose") or "summary"):
            await usage.record(usage.build_event(
                provider=provider.name, model=model, role=SMALL, prompt_tokens=pt,
                completion_tokens=ct, started=started, fallback_in=prompt, fallback_out=text))
        return text


_gateway: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
