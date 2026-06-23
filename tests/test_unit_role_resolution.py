"""Per-role LLM resolution: legacy compat, per-role provider overrides, and
the multi-key api-key pre-flight. These exercise `config._normalize_llm_roles`
+ `runtime._resolve_role_cfg` end-to-end without hitting a real provider SDK."""
from fabri.config import _normalize_llm_roles
from fabri.runtime import (
    _resolve_role_cfg,
    build_decompose_llm,
    build_narrator_llm,
    build_role_llm,
    find_missing_role_api_keys,
)


def _flat(extra=None):
    cfg = {
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 1024,
            "cache_messages": False,
        },
    }
    if extra:
        cfg["llm"].update(extra)
    return cfg


def test_legacy_decompose_model_string_lifts_into_role_with_parent_provider():
    cfg = _normalize_llm_roles(_flat({"decompose_model": "claude-haiku-4-5"}))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["provider"] == "anthropic"
    assert role["model"] == "claude-haiku-4-5"
    assert role["api_key_env"] == "ANTHROPIC_API_KEY"


def test_legacy_narrator_model_and_max_tokens_lift_together():
    cfg = _normalize_llm_roles(_flat({
        "narrator_model": "claude-haiku-4-5",
        "narrator_max_tokens": 80,
    }))
    role = cfg["llm"]["roles"]["narrator"]
    assert role["model"] == "claude-haiku-4-5"
    assert role["max_tokens"] == 80


def test_role_dict_with_only_model_inherits_provider_and_api_key():
    cfg = _normalize_llm_roles(_flat({"decompose": {"model": "claude-haiku-4-5"}}))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["provider"] == "anthropic"
    assert role["model"] == "claude-haiku-4-5"
    assert role["api_key_env"] == "ANTHROPIC_API_KEY"


def test_role_string_form_coerces_to_model_only_dict():
    cfg = _normalize_llm_roles(_flat({"decompose": "claude-haiku-4-5"}))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["model"] == "claude-haiku-4-5"
    assert role["provider"] == "anthropic"


def test_role_dict_can_override_provider_with_default_api_key_env():
    """When a role flips to a different provider but doesn't spell out
    api_key_env, fall back to that provider's conventional env var rather
    than leaking the parent's key into a different vendor's request."""
    cfg = _normalize_llm_roles(_flat({"decompose": {"provider": "openai", "model": "gpt-4o-mini"}}))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["provider"] == "openai"
    assert role["model"] == "gpt-4o-mini"
    assert role["api_key_env"] == "OPENAI_API_KEY"


def test_role_dict_with_openrouter_provider_resolves():
    cfg = _normalize_llm_roles(_flat({
        "narrator": {
            "provider": "openrouter",
            "model": "anthropic/claude-haiku-4-5",
            "max_tokens": 60,
        },
    }))
    role = cfg["llm"]["roles"]["narrator"]
    assert role["provider"] == "openrouter"
    assert role["model"] == "anthropic/claude-haiku-4-5"
    assert role["api_key_env"] == "OPENROUTER_API_KEY"


def test_explicit_role_dict_wins_over_legacy_key():
    """During an incremental migration the user may have BOTH the legacy
    flat key and the new role dict in the same yaml. The new dict wins."""
    cfg = _normalize_llm_roles(_flat({
        "narrator_model": "claude-haiku-4-5",
        "narrator": {"provider": "openai", "model": "gpt-4o-mini"},
    }))
    role = cfg["llm"]["roles"]["narrator"]
    assert role["provider"] == "openai"
    assert role["model"] == "gpt-4o-mini"


def test_role_explicitly_null_disables_backend():
    cfg = _normalize_llm_roles(_flat({"narrator": None}))
    assert cfg["llm"]["roles"]["narrator"] is None
    assert build_narrator_llm(cfg) is None


def test_decompose_role_absent_means_no_backend():
    cfg = _normalize_llm_roles(_flat())
    assert cfg["llm"]["roles"]["decompose"] is None
    assert build_decompose_llm(cfg) is None


def test_main_role_always_present_and_mirrors_parent():
    cfg = _normalize_llm_roles(_flat())
    main = cfg["llm"]["roles"]["main"]
    assert main["provider"] == "anthropic"
    assert main["model"] == "claude-sonnet-4-6"
    assert main["api_key_env"] == "ANTHROPIC_API_KEY"


def test_build_role_llm_routes_openrouter_to_openai_backend_with_base_url(monkeypatch):
    """`provider: openrouter` shares the OpenAI backend class but pins the
    base_url. Patched OpenAILLMBackend so we don't need the openai SDK."""
    from fabri import runtime
    captured = {}

    class _Stub:
        def __init__(self, model, tools, max_tokens, api_key_env, base_url=None):
            captured.update(locals())

    monkeypatch.setattr(runtime, "OpenAILLMBackend", _Stub)
    cfg = _normalize_llm_roles(_flat({
        "narrator": {
            "provider": "openrouter",
            "model": "anthropic/claude-haiku-4-5",
            "max_tokens": 60,
        },
    }))
    backend = build_role_llm(cfg, "narrator")
    assert isinstance(backend, _Stub)
    assert captured["model"] == "anthropic/claude-haiku-4-5"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key_env"] == "OPENROUTER_API_KEY"


def test_build_role_llm_unknown_provider_raises():
    import pytest
    cfg = _normalize_llm_roles(_flat({"decompose": {"provider": "bogus", "model": "m"}}))
    with pytest.raises(ValueError, match="unknown llm provider"):
        build_role_llm(cfg, "decompose")


def test_find_missing_role_api_keys_reports_all(monkeypatch):
    """A multi-provider config with two distinct envs, both missing -- both
    are reported in one pass (the CLI then prints them together)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _normalize_llm_roles(_flat({
        "narrator": {
            "provider": "openrouter",
            "model": "anthropic/claude-haiku-4-5",
            "api_key_env": "OPENROUTER_API_KEY",
        },
    }))
    missing = find_missing_role_api_keys(cfg)
    assert set(missing.keys()) == {"ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"}
    assert "main" in missing["ANTHROPIC_API_KEY"]
    assert "narrator" in missing["OPENROUTER_API_KEY"]


def test_find_missing_role_api_keys_ignores_disabled_roles(monkeypatch):
    """A null narrator + an unset OPENROUTER_API_KEY shouldn't show up as
    missing -- the role won't instantiate, so its env var doesn't matter."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stub")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _normalize_llm_roles(_flat({"narrator": None}))
    assert find_missing_role_api_keys(cfg) == {}


def test_resolve_role_cfg_normalizes_on_demand():
    """Programmatic callers that hand a flat dict to `build_*` (bypassing
    load_config) get implicit normalization, so legacy code paths keep
    working."""
    raw = _flat({"narrator_model": "claude-haiku-4-5"})
    # Note: we DID NOT call _normalize_llm_roles on `raw`.
    role = _resolve_role_cfg(raw, "narrator")
    assert role is not None
    assert role["model"] == "claude-haiku-4-5"


def test_legacy_config_unchanged_backend_selection(monkeypatch):
    """Pin a v0.7.6-shape yaml and assert the resolver picks the same
    backend selection as before (main=anthropic Sonnet, decompose=anthropic
    Haiku via legacy key, narrator=anthropic Haiku via legacy key)."""
    from fabri import runtime
    captured = []

    class _Stub:
        def __init__(self, model, tools, max_tokens, api_key_env, **kwargs):
            captured.append({"model": model, "api_key_env": api_key_env})

    monkeypatch.setattr(runtime, "AnthropicLLMBackend", _Stub)
    v076_cfg = _normalize_llm_roles({
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 1024,
            "decompose_model": "claude-haiku-4-5",
            "narrator_model": "claude-haiku-4-5",
            "narrator_max_tokens": 60,
        },
    })
    build_role_llm(v076_cfg, "main")
    build_decompose_llm(v076_cfg)
    build_narrator_llm(v076_cfg)
    models = sorted(c["model"] for c in captured)
    assert models == [
        "claude-haiku-4-5", "claude-haiku-4-5", "claude-sonnet-4-6",
    ]
    assert all(c["api_key_env"] == "ANTHROPIC_API_KEY" for c in captured)
