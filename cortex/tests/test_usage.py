"""Verbrauch & Kosten: Preisrechnung, Aggregation, Budget-Bremse und die Verbuchung
am Gateway. Alles ohne Datenbank und ohne echte API (Fake-Client + Test-Senke)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import models, usage


@pytest.fixture(autouse=True)
def _clean():
    usage.set_config({})
    usage.set_sink(None)
    usage.reset_budget_cache()
    models.set_model_config({})
    yield
    usage.set_config({})
    usage.set_sink(None)
    usage.reset_budget_cache()
    models.set_model_config({})


# ─── Preise ───────────────────────────────────────────────────────────────────
def test_known_model_price_and_cost():
    assert usage.price_for("gpt-4o-mini") == (0.15, 0.60)
    # 1 Mio Prompt + 1 Mio Completion
    assert usage.estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000, "openai") == 0.75


def test_longest_prefix_wins():
    # "gpt-4o-mini" darf nicht als das teurere "gpt-4o" abgerechnet werden.
    assert usage.price_for("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert usage.price_for("gpt-4o-2024-11-20") == (2.50, 10.00)


def test_unknown_model_has_no_invented_price():
    assert usage.price_for("mystery-model-9") is None
    assert usage.estimate_cost("mystery-model-9", 1000, 1000, "openai") is None


def test_local_provider_is_free_even_for_unknown_model():
    assert usage.estimate_cost("dolphin-mistral", 5000, 5000, "ollama") == 0.0


def test_user_price_override_beats_default():
    usage.set_config({"prices": {"gpt-4o-mini": [1.0, 2.0], "mystery-model": [3, 4]}})
    assert usage.price_for("gpt-4o-mini") == (1.0, 2.0)
    assert usage.price_for("mystery-model-9") == (3.0, 4.0)


def test_garbage_price_override_is_ignored():
    usage.set_config({"prices": {"x": ["a", "b"], "y": []}})
    assert usage.price_for("x") is None


# ─── Tagging ──────────────────────────────────────────────────────────────────
def test_tag_nests_and_restores():
    assert usage.current() == {}
    with usage.tag(purpose="triage", channel="waha"):
        with usage.tag(thread_id="t1"):
            assert usage.current() == {"purpose": "triage", "channel": "waha", "thread_id": "t1"}
        assert "thread_id" not in usage.current()
    assert usage.current() == {}


def test_third_party_detection():
    assert usage.is_third_party({"channel": "waha"}) is True
    assert usage.is_third_party({"channel": "web"}) is False
    assert usage.is_third_party({"channel": "waha", "third_party": False}) is False


# ─── Aggregation ──────────────────────────────────────────────────────────────
def _rows():
    t = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    return [
        {"ts": t, "model": "gpt-4o", "purpose": "chat", "channel": "web", "chat_id": "c1",
         "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.0075},
        {"ts": t, "model": "gpt-4o-mini", "purpose": "triage", "channel": "waha", "chat_id": "",
         "prompt_tokens": 2000, "completion_tokens": 100, "cost_usd": 0.00036},
        {"ts": t, "model": "mystery", "purpose": "chat", "channel": "web", "chat_id": "c1",
         "prompt_tokens": 10, "completion_tokens": 10, "cost_usd": None},
    ]


def test_group_by_model_sorted_by_cost():
    g = usage.group_rows(_rows(), "model")
    assert [a["key"] for a in g][0] == "gpt-4o"
    assert g[0]["tokens"] == 1500


def test_unpriced_calls_are_counted_not_hidden():
    g = {a["key"]: a for a in usage.group_rows(_rows(), "model")}
    assert g["mystery"]["unpriced"] == 1 and g["mystery"]["cost"] == 0.0


def test_group_by_purpose_and_totals():
    g = {a["key"]: a for a in usage.group_rows(_rows(), "purpose")}
    assert g["chat"]["calls"] == 2 and g["triage"]["calls"] == 1
    t = usage.totals(_rows())
    assert t["calls"] == 3 and t["unpriced"] == 1
    assert t["cost"] == pytest.approx(0.00786)


def test_group_by_day_is_chronological():
    rows = _rows()
    rows[1]["ts"] = datetime(2026, 8, 30, tzinfo=timezone.utc)
    assert [a["key"] for a in usage.group_rows(rows, "day")] == ["2026-08-30", "2026-09-01"]


def test_unknown_group_raises():
    with pytest.raises(ValueError):
        usage.group_rows(_rows(), "password")


# ─── Budget ───────────────────────────────────────────────────────────────────
def test_budget_levels():
    cfg = {"monthly_usd": 10}
    assert usage.budget_state(2, cfg)["level"] == "ok"
    assert usage.budget_state(8.5, cfg)["level"] == "warn"
    assert usage.budget_state(10, cfg)["level"] == "over"


def test_no_budget_means_no_limit():
    assert usage.budget_state(999, {})["level"] == "ok"


def test_hard_stop_only_hits_third_parties():
    st = usage.budget_state(11, {"monthly_usd": 10, "hard_stop_third_party": True})
    assert usage.hard_stop_applies(st, third_party=True) is True
    assert usage.hard_stop_applies(st, third_party=False) is False   # Bahrian nie


def test_hard_stop_needs_the_flag():
    st = usage.budget_state(11, {"monthly_usd": 10})
    assert usage.hard_stop_applies(st, third_party=True) is False


# ─── Gateway verbucht wirklich ────────────────────────────────────────────────
class _FakeCompletions:
    def __init__(self, fail=False):
        self.fail = fail

    async def create(self, **kw):
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
            choices=[SimpleNamespace(message=SimpleNamespace(content="hi", tool_calls=None))])


def _fake_client(fail=False):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(fail)))


def _setup_gateway(monkeypatch, fail=False):
    models.set_model_config({
        "providers": {"openai": {"kind": "openai_compat", "api_key": "sk-x"}},
        "roles": {"medium": {"provider": "openai", "model": "gpt-4o"}},
    })
    gw = models.ModelGateway()
    monkeypatch.setattr(gw, "_openai_client", lambda provider: _fake_client(fail))
    seen: list = []

    async def sink(ev):
        seen.append(ev)
    usage.set_sink(sink)
    return gw, seen


def test_gateway_records_tokens_cost_and_context(monkeypatch):
    gw, seen = _setup_gateway(monkeypatch)

    async def go():
        with usage.tag(purpose="chat", channel="web", chat_id="c42"):
            await gw.chat([{"role": "user", "content": "hallo"}])
    asyncio.run(go())
    ev = seen[0]
    assert (ev.provider, ev.model, ev.role) == ("openai", "gpt-4o", "medium")
    assert (ev.prompt_tokens, ev.completion_tokens) == (120, 30)
    assert ev.purpose == "chat" and ev.chat_id == "c42" and ev.channel == "web"
    assert ev.cost_usd == pytest.approx(120 / 1e6 * 2.5 + 30 / 1e6 * 10)
    assert ev.ok is True and ev.estimated is False


def test_gateway_records_failures_too(monkeypatch):
    gw, seen = _setup_gateway(monkeypatch, fail=True)

    async def go():
        with pytest.raises(RuntimeError):
            # retry macht 3 Versuche mit Backoff — direkt die ungewrappte Funktion nehmen
            await gw.chat.__wrapped__(gw, [{"role": "user", "content": "x"}])
    asyncio.run(go())
    assert seen and seen[0].ok is False and "boom" in seen[0].error


def test_per_call_pick_overrides_role(monkeypatch):
    gw, seen = _setup_gateway(monkeypatch)
    models.set_model_config({
        "providers": {"openai": {"kind": "openai_compat", "api_key": "sk-x"}},
        "roles": {"medium": {"provider": "openai", "model": "gpt-4o"},
                  "small": {"provider": "openai", "model": "gpt-4o-mini"}},
    })

    async def go():
        await gw.chat([{"role": "user", "content": "hi"}], pick={"tier": "small"})
        await gw.chat([{"role": "user", "content": "hi"}],
                      pick={"provider": "openai", "model": "gpt-4.1"})
    asyncio.run(go())
    assert [(e.model, e.role) for e in seen] == [("gpt-4o-mini", "small"), ("gpt-4.1", "custom")]


def test_pick_with_unknown_provider_fails_loud():
    with pytest.raises(models.ModelError):
        models.resolve_pick({"provider": "nope", "model": "x"}, "medium")


def test_tool_chat_on_toolless_provider_is_refused(monkeypatch):
    gw, seen = _setup_gateway(monkeypatch)
    models.set_model_config({
        "providers": {"anthropic": {"kind": "anthropic", "api_key": "k", "tools": False}}})

    async def go():
        with pytest.raises(models.ModelError):
            await gw.chat.__wrapped__(gw, [{"role": "user", "content": "x"}],
                                      tools=[{"type": "function"}],
                                      pick={"provider": "anthropic", "model": "claude-sonnet-5"})
    asyncio.run(go())


def test_missing_provider_usage_is_estimated():
    ev = usage.build_event(provider="ollama", model="llama", role="medium",
                           prompt_tokens=None, completion_tokens=None, started=0.0,
                           fallback_in="a" * 400, fallback_out="b" * 80)
    assert ev.estimated is True and ev.prompt_tokens == 100 and ev.completion_tokens == 20
    assert ev.cost_usd == 0.0     # lokal → gratis


def test_formatting():
    assert usage.fmt_cost(None) == "Preis fehlt"
    assert usage.fmt_cost(0.5) == "0,50 $"
    assert usage.fmt_cost(0.00123) == "0,0012 $"
    assert usage.fmt_tokens(1500) == "1,5 k"
    assert usage.fmt_tokens(2_500_000) == "2,50 Mio."


# ─── Zeiträume & Bericht ──────────────────────────────────────────────────────
def test_period_starts_use_the_local_day_and_month():
    now = datetime(2026, 9, 18, 22, 30, tzinfo=timezone.utc)      # = 19.09. 00:30 in Berlin
    assert usage.period_start("today", now, "Europe/Berlin") == datetime(2026, 9, 18, 22, 0, tzinfo=timezone.utc)
    assert usage.period_start("month", now, "Europe/Berlin") == datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc)
    assert usage.period_start("week", now, "Europe/Berlin") == datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
    assert usage.period_start("all", now).year <= 2000


def test_summary_text_reports_totals_breakdown_and_unpriced_warning():
    text = usage.summary_text(_rows(), by="purpose", period="month")
    assert "3 Aufrufe" in text and "Chat mit dir" in text and "Triage (Eingang)" in text
    assert "ohne Preis" in text


def test_summary_text_for_no_data():
    assert "noch keine" in usage.summary_text([], "model", "today")


def test_usage_command_parsing():
    assert usage.parse_usage_command("hallo") is None
    assert usage.parse_usage_command("/verbrauch") == ("month", "model")
    assert usage.parse_usage_command("/Verbrauch heute zweck") == ("today", "purpose")
    assert usage.parse_usage_command("/kosten woche chat") == ("week", "chat_id")
    assert usage.parse_usage_command("/usage gesamt tag") == ("all", "day")
