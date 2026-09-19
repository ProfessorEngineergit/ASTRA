"""Verbrauch & Kosten — jeder LLM-Aufruf wird am Gateway mitgeschrieben.

Ziel (Bahrian): klar sehen, wie viel Usage verbraucht wurde, was es gekostet hat und
WAS wie viel gekostet hat — nach Modell, Zweck (Triage/Chat/Briefing/…), Kanal und Chat.

Design:
  • Der Aufrufer sagt NICHT, was er ist. Stattdessen setzt der Code drumherum per
    ``with usage.tag(purpose="triage", channel="waha", …)`` einen Kontext (ContextVar);
    das Gateway liest ihn beim Mitschreiben. So wird nichts vergessen, und die vielen
    Aufrufstellen (agent, brain, briefing, jobs, moderation …) bleiben schlank.
  • Preise sind DATEN (Tabelle unten + Überschreibung in den Einstellungen), kein Code.
    Unbekannte Modelle bekommen bewusst KEINEN erfundenen Preis: ``cost_usd=None`` und
    die UI fordert dazu auf, ihn einzutragen. Lokale Anbieter (Ollama & Co.) kosten 0.
  • Die Preis-/Kostenrechnung ist rein (ohne I/O) und damit testbar. Geschrieben wird
    über eine austauschbare Senke — Tests brauchen keine Datenbank.
  • Budget-Bremse: Warnung bei 80 %/100 % des Monatslimits, optional harte Sperre nur
    für DRITTEN-Verkehr (Sekretär), damit ein Missbrauch nie die eigene API leer räumt.

Nicht erfasst (ehrlich): mem0-Embeddings und Whisper laufen an diesem Gateway vorbei.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger("astra.usage")

# ─── Preise (USD je 1 Mio. Token: (input, output)) ────────────────────────────
# Richtwerte, bewusst konservativ befüllt. Alles, was hier fehlt, hat KEINEN Preis
# (cost=None) statt eines geratenen. In den Einstellungen überschreibbar:
#   app_settings["usage"]["prices"] = {"modellname-oder-praefix": [in, out]}
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
}
# Diese Anbieter laufen lokal → kosten kein Geld (nur Strom).
FREE_PROVIDERS = frozenset({"ollama", "lmstudio", "local"})

_PRICE_OVERRIDES: dict[str, tuple[float, float]] = {}
_BUDGET: dict[str, Any] = {}


def set_config(cfg: dict | None) -> None:
    """Preise/Budget live aus den Einstellungen übernehmen (app_settings['usage'])."""
    global _PRICE_OVERRIDES, _BUDGET
    cfg = cfg or {}
    prices: dict[str, tuple[float, float]] = {}
    for name, val in (cfg.get("prices") or {}).items():
        try:
            prices[str(name).lower()] = (float(val[0]), float(val[1]))
        except (TypeError, ValueError, IndexError):
            continue
    _PRICE_OVERRIDES = prices
    _BUDGET = dict(cfg.get("budget") or {})


def price_for(model: str, provider: str = "") -> tuple[float, float] | None:
    """(input, output) USD/1M-Token oder None, wenn unbekannt. Längster Präfix gewinnt."""
    if provider.lower() in FREE_PROVIDERS:
        return (0.0, 0.0)
    m = (model or "").lower()
    table = {**DEFAULT_PRICES, **_PRICE_OVERRIDES}
    best: tuple[int, tuple[float, float]] | None = None
    for prefix, price in table.items():
        if m == prefix or m.startswith(prefix) or prefix in m:
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), price)
    return best[1] if best else None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  provider: str = "") -> float | None:
    p = price_for(model, provider)
    if p is None:
        return None
    return round(prompt_tokens / 1_000_000 * p[0] + completion_tokens / 1_000_000 * p[1], 6)


def rough_tokens(text: str) -> int:
    """Grobe Schätzung (~4 Zeichen/Token), nur wenn der Anbieter keine Zahlen liefert."""
    return max(1, len(text or "") // 4)


# ─── Kontext-Tagging ──────────────────────────────────────────────────────────
_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("usage_ctx", default={})


@contextlib.contextmanager
def tag(**fields: Any):
    """``with usage.tag(purpose="triage", channel="waha", thread_id=…):`` — alles, was
    darin an das Gateway geht, wird mit diesen Feldern verbucht (verschachtelt mergt)."""
    merged = {**_CTX.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CTX.set(merged)
    try:
        yield
    finally:
        _CTX.reset(token)


def current() -> dict:
    return dict(_CTX.get())


# ─── Datensatz & Senke ────────────────────────────────────────────────────────
@dataclass
class UsageEvent:
    provider: str
    model: str
    role: str = ""
    purpose: str = "other"
    channel: str = ""
    thread_id: str = ""
    chat_id: str = ""
    contact: str = ""
    principal_key: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    ok: bool = True
    estimated: bool = False
    error: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


Sink = Callable[[UsageEvent], Awaitable[None]]
_sink: Sink | None = None


def set_sink(sink: Sink | None) -> None:
    """Senke austauschen (Tests). None = Standard (Datenbank)."""
    global _sink
    _sink = sink


async def _db_sink(ev: UsageEvent) -> None:
    from . import db
    await db.usage_insert(ev)


def build_event(*, provider: str, model: str, role: str, prompt_tokens: int | None,
                completion_tokens: int | None, started: float, ok: bool = True,
                error: str = "", fallback_in: str = "", fallback_out: str = "") -> UsageEvent:
    """Aus einer Provider-Antwort ein UsageEvent bauen (schätzt, wenn Zahlen fehlen)."""
    estimated = prompt_tokens is None or completion_tokens is None
    pt = prompt_tokens if prompt_tokens is not None else rough_tokens(fallback_in)
    ct = completion_tokens if completion_tokens is not None else rough_tokens(fallback_out)
    ctx = current()
    return UsageEvent(
        provider=provider, model=model, role=role,
        purpose=str(ctx.get("purpose") or "other"), channel=str(ctx.get("channel") or ""),
        thread_id=str(ctx.get("thread_id") or ""), chat_id=str(ctx.get("chat_id") or ""),
        contact=str(ctx.get("contact") or ""), principal_key=str(ctx.get("principal") or ""),
        prompt_tokens=int(pt), completion_tokens=int(ct),
        cost_usd=estimate_cost(model, int(pt), int(ct), provider),
        latency_ms=int((time.perf_counter() - started) * 1000), ok=ok,
        estimated=estimated, error=error[:300],
    )


async def record(ev: UsageEvent) -> None:
    """Mitschreiben — darf NIE den eigentlichen Aufruf kippen."""
    try:
        await (_sink or _db_sink)(ev)
    except Exception:  # noqa: BLE001 — Buchhaltung ist nie wichtiger als die Antwort
        log.debug("usage record failed", exc_info=True)


# ─── Budget (rein) ────────────────────────────────────────────────────────────
def budget_state(spent_month_usd: float, cfg: dict | None = None) -> dict:
    """{limit, spent, pct, level: ok|warn|over, hard} — rein, ohne I/O."""
    cfg = cfg if cfg is not None else _BUDGET
    try:
        limit = float(cfg.get("monthly_usd") or 0)
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0:
        return {"limit": 0.0, "spent": spent_month_usd, "pct": 0.0, "level": "ok",
                "hard": False}
    pct = spent_month_usd / limit * 100
    level = "over" if pct >= 100 else "warn" if pct >= float(cfg.get("warn_pct") or 80) else "ok"
    return {"limit": limit, "spent": round(spent_month_usd, 4), "pct": round(pct, 1),
            "level": level, "hard": bool(cfg.get("hard_stop_third_party"))}


def hard_stop_applies(state: dict, *, third_party: bool) -> bool:
    """Harte Sperre trifft NUR fremden Verkehr, nie Bahrian selbst."""
    return bool(third_party and state.get("hard") and state.get("level") == "over")


def month_start(now: datetime | None = None) -> datetime:
    n = now or datetime.now(timezone.utc)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ─── Aggregation (rein — arbeitet auf Zeilen, egal woher sie kommen) ──────────
GROUPS = ("model", "purpose", "channel", "role", "provider", "chat_id", "day")


def group_rows(rows: list[dict], by: str) -> list[dict]:
    """Zeilen nach `by` zusammenfassen → [{key, calls, prompt, completion, tokens, cost,
    unpriced}] absteigend nach Kosten (dann Tokens). `unpriced` = Aufrufe ohne Preis."""
    if by not in GROUPS:
        raise ValueError(f"unbekannte Gruppierung: {by}")
    acc: dict[str, dict] = {}
    for r in rows:
        if by == "day":
            ts = r.get("ts")
            key = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        else:
            key = str(r.get(by) or "—")
        a = acc.setdefault(key, {"key": key, "calls": 0, "prompt": 0, "completion": 0,
                                 "cost": 0.0, "unpriced": 0})
        a["calls"] += 1
        a["prompt"] += int(r.get("prompt_tokens") or 0)
        a["completion"] += int(r.get("completion_tokens") or 0)
        cost = r.get("cost_usd")
        if cost is None:
            a["unpriced"] += 1
        else:
            a["cost"] += float(cost)
    out = list(acc.values())
    for a in out:
        a["tokens"] = a["prompt"] + a["completion"]
        a["cost"] = round(a["cost"], 6)
    if by == "day":
        return sorted(out, key=lambda a: a["key"])
    return sorted(out, key=lambda a: (-a["cost"], -a["tokens"]))


def totals(rows: list[dict]) -> dict:
    g = group_rows(rows, "model") if rows else []
    return {
        "calls": sum(a["calls"] for a in g),
        "tokens": sum(a["tokens"] for a in g),
        "prompt": sum(a["prompt"] for a in g),
        "completion": sum(a["completion"] for a in g),
        "cost": round(sum(a["cost"] for a in g), 6),
        "unpriced": sum(a["unpriced"] for a in g),
    }


def fmt_cost(v: float | None) -> str:
    if v is None:
        return "Preis fehlt"
    if v == 0:
        return "0,00 $"
    return (f"{v:.4f} $" if v < 0.01 else f"{v:.2f} $").replace(".", ",")


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".replace(".", ",") + " Mio."
    if n >= 1_000:
        return f"{n / 1_000:.1f}".replace(".", ",") + " k"
    return str(n)


# ─── Budget-Gate (I/O, gecacht) ───────────────────────────────────────────────
THIRD_PARTY_CHANNELS = frozenset({"waha", "signal", "slack", "email"})
_BUDGET_CACHE: dict[str, Any] = {"at": 0.0, "state": None}
_BUDGET_TTL = 60.0


class BudgetExceeded(RuntimeError):
    """Monatsbudget erreicht und harte Sperre für fremden Verkehr aktiv."""


async def current_budget_state(*, force: bool = False) -> dict:
    """Aktueller Budgetstand (60 s gecacht). Meldet Bahrian einmal pro Monat und Stufe."""
    now = time.monotonic()
    if not force and _BUDGET_CACHE["state"] is not None and now - _BUDGET_CACHE["at"] < _BUDGET_TTL:
        return _BUDGET_CACHE["state"]
    state = budget_state(0.0)
    try:
        if _BUDGET.get("monthly_usd"):
            from . import db
            spent = await db.usage_month_cost(month_start())
            state = budget_state(spent)
            await _maybe_notify(state)
    except Exception:  # noqa: BLE001 — ohne DB gibt es einfach keine Sperre
        log.debug("budget state unavailable", exc_info=True)
    _BUDGET_CACHE.update(at=now, state=state)
    return state


async def _maybe_notify(state: dict) -> None:
    if state["level"] == "ok":
        return
    from . import db
    key = f"{month_start():%Y-%m}:{state['level']}"
    if await db.get_setting("usage_budget_notified", "") == key:
        return
    await db.set_setting("usage_budget_notified", key)
    try:
        from . import notify as notify_mod
        text = (f"💸 LLM-Budget: {state['pct']:.0f} % des Monatslimits verbraucht "
                f"({state['spent']:.2f} $ von {state['limit']:.2f} $)."
                + (" Fremder Verkehr wird jetzt gesperrt." if state["level"] == "over"
                   and state["hard"] else ""))
        await notify_mod.notify(text, urgency="normal")
    except Exception:  # noqa: BLE001
        log.debug("budget notify failed", exc_info=True)


async def budget_gate(*, third_party: bool) -> None:
    """Vor jedem Gateway-Aufruf. Wirft nur bei harter Sperre gegen fremden Verkehr."""
    if not _BUDGET.get("monthly_usd"):
        return
    state = await current_budget_state()
    if hard_stop_applies(state, third_party=third_party):
        raise BudgetExceeded(
            f"Monatsbudget von {state['limit']:.2f} $ erreicht — Sekretär-Antworten pausiert.")


def is_third_party(ctx: dict | None = None) -> bool:
    ctx = ctx if ctx is not None else current()
    if "third_party" in ctx:
        return bool(ctx["third_party"])
    return str(ctx.get("channel") or "") in THIRD_PARTY_CHANNELS


def reset_budget_cache() -> None:
    _BUDGET_CACHE.update(at=0.0, state=None)


# ─── Zeiträume & Text-Breakdown (Werkzeug UND UI teilen sich das) ─────────────
PERIODS = {"today": "Heute", "week": "7 Tage", "month": "Dieser Monat", "all": "Gesamt"}
GROUP_LABELS = {"model": "Modell", "purpose": "Zweck", "channel": "Kanal", "role": "Stufe",
                "provider": "Anbieter", "chat_id": "Chat", "day": "Tag"}
PURPOSE_LABELS = {
    "chat": "Chat mit dir", "secretary_reply": "Sekretär-Antworten", "triage": "Triage (Eingang)",
    "briefing": "Morgen-Briefing", "digest": "Kontext-Kapseln", "reason": "Planen/Analyse",
    "prompt_review": "Prompt-Selbstprüfung", "summary": "Zusammenfassungen", "other": "Sonstiges",
}


def period_start(period: str, now: datetime | None = None, tz: str = "Europe/Berlin") -> datetime:
    """Beginn des Zeitraums als UTC-Zeitpunkt (Tag = lokaler Tag, Monat = lokaler Monat)."""
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if period == "today":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (local - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = local.replace(year=2000, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def nice_key(by: str, key: str) -> str:
    if by == "purpose":
        return PURPOSE_LABELS.get(key, key)
    if by == "chat_id" and key in ("", "—"):
        return "(kein Web-Chat)"
    return key


def summary_text(rows: list[dict], by: str = "model", period: str = "month", top: int = 8) -> str:
    """Kompakter, ehrlicher Verbrauchsbericht als Text (Chat/Telegram)."""
    tot = totals(rows)
    label = PERIODS.get(period, period)
    if not rows:
        return f"{label}: noch keine LLM-Aufrufe erfasst."
    lines = [f"{label}: {tot['calls']} Aufrufe · {fmt_tokens(tot['tokens'])} Token "
             f"({fmt_tokens(tot['prompt'])} rein / {fmt_tokens(tot['completion'])} raus) · "
             f"{fmt_cost(tot['cost'])}"]
    if tot["unpriced"]:
        lines.append(f"⚠ {tot['unpriced']} Aufrufe ohne Preis (Modell unbekannt) — nicht in der Summe.")
    lines.append("")
    lines.append(f"Nach {GROUP_LABELS.get(by, by)}:")
    grouped = group_rows(rows, by)
    for g in (grouped if by == "day" else grouped[:top]):
        cost = fmt_cost(g["cost"]) if not g["unpriced"] or g["cost"] else "Preis fehlt"
        lines.append(f"• {nice_key(by, g['key'])}: {g['calls']}× · {fmt_tokens(g['tokens'])} Token · {cost}")
    if len(grouped) > top and by != "day":
        lines.append(f"… und {len(grouped) - top} weitere")
    return "\n".join(lines)


# ─── Chat-Befehl „/verbrauch“ ─────────────────────────────────────────────────
import re as _re

_USAGE_CMD = _re.compile(r"^\s*/(?:verbrauch|usage|kosten)\b\s*(.*)$", _re.IGNORECASE)
_PERIOD_WORDS = {"heute": "today", "today": "today", "woche": "week", "week": "week", "7": "week",
                 "monat": "month", "month": "month", "gesamt": "all", "alles": "all", "all": "all"}
_BY_WORDS = {"modell": "model", "model": "model", "zweck": "purpose", "kanal": "channel",
             "channel": "channel", "chat": "chat_id", "anbieter": "provider", "stufe": "role",
             "tag": "day", "tage": "day"}


def parse_usage_command(text: str) -> tuple[str, str] | None:
    """'/verbrauch woche zweck' → ('week', 'purpose'); kein Befehl → None. Rein."""
    m = _USAGE_CMD.match(text or "")
    if not m:
        return None
    period, by = "month", "model"
    for word in m.group(1).lower().split():
        word = word.strip(",.;")
        if word in _PERIOD_WORDS:
            period = _PERIOD_WORDS[word]
        elif word in _BY_WORDS:
            by = _BY_WORDS[word]
    return period, by


async def report(period: str, by: str, tz: str = "Europe/Berlin") -> str:
    """Bericht für Chat/Telegram (liest die Datenbank)."""
    from . import db
    rows = await db.usage_rows(period_start(period, tz=tz))
    text = summary_text(rows, by=by, period=period)
    state = budget_state(await db.usage_month_cost(month_start()))
    if state["limit"]:
        text += (f"\n\nBudget: {state['pct']:.0f} % von {fmt_cost(state['limit'])} "
                 f"({fmt_cost(state['spent'])}).")
    return text
