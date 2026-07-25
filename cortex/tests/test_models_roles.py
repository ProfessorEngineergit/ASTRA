"""Rollen-Router (W7): Anbieter × Modell frei wählbar pro Stufe, kein stiller
Fallback. Reine Auflösungslogik — testbar ohne echte API."""
import pytest

from app import models as m


@pytest.fixture(autouse=True)
def _clean():
    m.set_model_config({})
    m.set_model_override(None)
    m.set_economy(False)
    yield
    m.set_model_config({})
    m.set_model_override(None)
    m.set_economy(False)


def _cfg():
    return {
        "providers": {
            "openai": {"kind": "openai_compat", "api_key": "sk-test"},
            "openrouter": {"kind": "openai_compat", "base_url": "https://openrouter.ai/api/v1",
                           "api_key": "or-test"},
            "anthropic": {"kind": "anthropic", "api_key": "an-test", "tools": False},
        },
        "roles": {
            "small": {"provider": "openai", "model": "gpt-4o-mini"},
            "medium": {"provider": "openai", "model": "gpt-4o"},
            "heavy": {"provider": "anthropic", "model": "claude-sonnet-5"},
        },
    }


def test_each_tier_resolves_to_its_provider_and_model():
    m.set_model_config(_cfg())
    assert m.role_target("small")[1] == "gpt-4o-mini"
    assert m.role_target("medium")[0].name == "openai"
    p, model = m.role_target("heavy")
    assert (p.name, model) == ("anthropic", "claude-sonnet-5")


def test_explicitly_blank_role_fails_loudly_not_silently():
    # Kein stiller Fallback: eine leer gesetzte Rolle wirft, statt heimlich ein
    # anderes Modell zu nehmen (Bahrians Entscheidung: laut scheitern).
    m.set_model_config({"providers": {"openai": {"api_key": "sk"}},
                        "roles": {"heavy": {"provider": "", "model": ""}}})
    with pytest.raises(m.ModelError):
        m.role_target("heavy")


def test_unknown_provider_is_an_error():
    m.set_model_config({"roles": {"heavy": {"provider": "nope", "model": "x"}}})
    with pytest.raises(m.ModelError):
        m.role_target("heavy")


def test_provider_without_key_is_an_error():
    m.set_model_config({"providers": {"openai": {"kind": "openai_compat", "api_key": ""}},
                        "roles": {"medium": {"provider": "openai", "model": "gpt-4o"}}})
    with pytest.raises(m.ModelError):
        m.role_target("medium")


def test_economy_shifts_medium_down_to_small():
    m.set_model_config(_cfg())
    m.set_economy(True)
    assert m.role_target("medium")[1] == "gpt-4o-mini"


def test_code_role_falls_back_to_medium_when_unset():
    m.set_model_config(_cfg())
    assert m.role_target("code")[1] == m.role_target("medium")[1]


def test_legacy_free_text_override_still_wins_for_medium():
    m.set_model_config(_cfg())
    m.set_model_override("gpt-4o-2024-11-20")
    assert m.role_target("medium")[1] == "gpt-4o-2024-11-20"


def test_openai_compatible_provider_carries_its_base_url():
    m.set_model_config(_cfg())
    m.set_model_config({**_cfg(), "roles": {**_cfg()["roles"],
                                            "medium": {"provider": "openrouter", "model": "x/y"}}})
    p, model = m.role_target("medium")
    assert p.base_url.endswith("/api/v1") and model == "x/y"


def test_anthropic_is_marked_as_no_tool_calling():
    m.set_model_config(_cfg())
    assert m.role_target("heavy")[0].tools is False
    assert m.role_target("medium")[0].tools is True
