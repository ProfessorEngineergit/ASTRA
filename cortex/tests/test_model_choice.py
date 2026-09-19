from app import model_choice as mc

SNAP = {
    "roles": {"small": {"provider": "openai", "model": "gpt-4o-mini"},
              "medium": {"provider": "openai", "model": "gpt-4o"},
              "heavy": {"provider": "anthropic", "model": "claude-sonnet-5"},
              "code": {}, "osint": {}},
    "providers": {"openai": {"kind": "openai_compat", "configured": True},
                  "anthropic": {"kind": "anthropic", "configured": False}},
}


def test_clean_pick_accepts_tier_and_model_and_rejects_junk():
    assert mc.clean_pick({"tier": "Heavy"}) == {"tier": "heavy"}
    assert mc.clean_pick({"provider": "OpenRouter", "model": "x/y-1"}) == {"provider": "openrouter", "model": "x/y-1"}
    assert mc.clean_pick({"provider": "x y", "model": "m"}) == {}
    assert mc.clean_pick({"provider": "a", "model": "<script>"}) == {}
    assert mc.clean_pick("heavy") == {} and mc.clean_pick(None) == {}


def test_encode_decode_roundtrip():
    for pick in ({}, {"tier": "small"}, {"provider": "openrouter", "model": "anthropic/claude-sonnet-5"}):
        assert mc.decode(mc.encode(pick)) == pick
    assert mc.decode("tier:nonsense") == {}


def test_options_disable_unconfigured_and_keep_code_fallback():
    opts = {o["value"]: o for o in mc.options(SNAP, [{"provider": "openai", "model": "gpt-4o"},
                                                     {"provider": "openai", "model": "gpt-4o"}])}
    assert opts["tier:small"]["disabled"] is False
    assert opts["tier:heavy"]["disabled"] is True          # Anthropic-Key fehlt
    assert opts["tier:osint"]["disabled"] is True
    assert opts["tier:code"]["disabled"] is False          # fällt auf medium zurück
    assert list(opts).count("model:openai|gpt-4o") == 1     # keine Dubletten


def test_label_names_the_resolved_model():
    assert "openai/gpt-4o-mini" in mc.label({"tier": "small"}, SNAP)
    assert mc.label({"provider": "openrouter", "model": "a/b"}) == "openrouter/a/b"
    assert mc.label({}, SNAP).startswith("Standard (openai/gpt-4o")


def test_command_parsing():
    assert mc.parse_command("hallo", SNAP)[0] is False
    assert mc.parse_command("/modell schwer", SNAP)[:2] == (True, {"tier": "heavy"})
    assert mc.parse_command("/Model KLEIN", SNAP)[:2] == (True, {"tier": "small"})
    assert mc.parse_command("/modell auto", SNAP)[:2] == (True, {})
    assert mc.parse_command("/modell openrouter:anthropic/claude-sonnet-5", SNAP)[:2] == (
        True, {"provider": "openrouter", "model": "anthropic/claude-sonnet-5"})
    assert mc.parse_command("/modell gpt-4.1", SNAP)[:2] == (True, {"provider": "openai", "model": "gpt-4.1"})
    assert mc.parse_command("/modell", SNAP)[:2] == (True, None)
    ok, pick, msg = mc.parse_command("/modell ????? was", SNAP)
    assert ok and pick is None and "Beispiele" in msg
