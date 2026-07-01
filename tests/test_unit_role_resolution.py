"""Per-role LLM resolution: legacy compat, per-role provider overrides, and
the multi-key api-key pre-flight. These exercise `config._normalize_llm_roles`
+ `runtime._resolve_role_cfg` end-to-end without hitting a real provider SDK."""
from fabri.config import _normalize_llm_roles
from fabri.runtime import (
    _resolve_role_cfg,
    build_decompose_llm,
    build_narrator_llm,
    build_role_llm,
    find_bedrock_roles_missing_region,
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


def test_role_dict_with_gemini_provider_resolves_default_api_key():
    """A role flipped to `gemini` without an explicit api_key_env falls back to
    the conventional GEMINI_API_KEY rather than leaking the parent's key."""
    cfg = _normalize_llm_roles(_flat({
        "decompose": {"provider": "gemini", "model": "gemini-2.5-flash"},
    }))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["provider"] == "gemini"
    assert role["model"] == "gemini-2.5-flash"
    assert role["api_key_env"] == "GEMINI_API_KEY"


def test_build_role_llm_routes_gemini_to_gemini_backend(monkeypatch):
    """`provider: gemini` dispatches to GeminiLLMBackend. Patched so we don't
    need the google-genai SDK installed."""
    from fabri import runtime
    captured = {}

    class _Stub:
        def __init__(self, model, tools, max_tokens, api_key_env):
            captured.update(locals())

    monkeypatch.setattr(runtime, "GeminiLLMBackend", _Stub)
    cfg = _normalize_llm_roles(_flat({
        "decompose": {"provider": "gemini", "model": "gemini-2.5-pro"},
    }))
    backend = build_role_llm(cfg, "decompose")
    assert isinstance(backend, _Stub)
    assert captured["model"] == "gemini-2.5-pro"
    assert captured["api_key_env"] == "GEMINI_API_KEY"


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


# --------------------------------------------------------------------------- #
# AWS Bedrock provider: dispatch, aws_region threading, no api_key_env
# --------------------------------------------------------------------------- #
def test_bedrock_main_resolves_no_api_key_and_carries_region():
    """A bedrock parent resolves api_key_env to None (creds via the AWS chain)
    and threads aws_region onto every role, including main."""
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "bedrock",
            "model": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "aws_region": "us-east-1",
            "max_tokens": 2048,
        },
    })
    main = cfg["llm"]["roles"]["main"]
    assert main["provider"] == "bedrock"
    assert main["api_key_env"] is None
    assert main["aws_region"] == "us-east-1"


def test_bedrock_role_override_inherits_parent_region():
    """A role flipped to bedrock without its own aws_region inherits the parent's."""
    cfg = _normalize_llm_roles(_flat({
        "aws_region": "eu-west-1",
        "decompose": {"provider": "bedrock", "model": "us.anthropic.claude-3-5-haiku-20241022-v1:0"},
    }))
    role = cfg["llm"]["roles"]["decompose"]
    assert role["provider"] == "bedrock"
    assert role["api_key_env"] is None
    assert role["aws_region"] == "eu-west-1"


def test_bedrock_role_explicit_region_wins_over_parent():
    cfg = _normalize_llm_roles(_flat({
        "aws_region": "eu-west-1",
        "decompose": {"provider": "bedrock", "model": "m", "aws_region": "ap-south-1"},
    }))
    assert cfg["llm"]["roles"]["decompose"]["aws_region"] == "ap-south-1"


def test_build_role_llm_routes_bedrock_to_bedrock_backend(monkeypatch):
    """`provider: bedrock` dispatches to BedrockLLMBackend with model + region
    and NO api_key_env. Patched so we don't need boto3."""
    from fabri import runtime
    captured = {}

    class _Stub:
        def __init__(self, model, tools, max_tokens, region):
            captured.update(locals())

    monkeypatch.setattr(runtime, "BedrockLLMBackend", _Stub)
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "bedrock",
            "model": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "aws_region": "us-east-1",
            "max_tokens": 2048,
        },
    })
    backend = build_role_llm(cfg, "main")
    assert isinstance(backend, _Stub)
    assert captured["model"] == "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert captured["region"] == "us-east-1"


def test_provider_enum_coerce_and_membership():
    """The Provider enum is the canonical provider set. coerce is
    case-insensitive, returns None for unknown/absent, and members compare +
    hash as their lowercase string (so they work as dict keys and == checks)."""
    from fabri.config import _PROVIDER_DEFAULT_API_KEY_ENV
    from fabri.core.llm import Provider

    assert Provider.coerce("BEDROCK") is Provider.BEDROCK
    assert Provider.coerce("bedrock") == "bedrock"
    assert Provider.coerce("bogus") is None
    assert Provider.coerce(None) is None
    # StrEnum members look up a string-keyed-or-enum-keyed dict by plain string
    assert _PROVIDER_DEFAULT_API_KEY_ENV.get("anthropic") == "ANTHROPIC_API_KEY"
    assert _PROVIDER_DEFAULT_API_KEY_ENV.get(Provider.GEMINI) == "GEMINI_API_KEY"
    assert Provider.BEDROCK not in _PROVIDER_DEFAULT_API_KEY_ENV  # chain-auth


def test_normalized_provider_is_plain_str_yaml_safe():
    """The stored role `provider` must be a plain str (not a StrEnum instance)
    so a normalized config round-trips through yaml.safe_dump without a custom
    representer."""
    import yaml
    cfg = _normalize_llm_roles({"llm": {"provider": "bedrock", "model": "m", "aws_region": "ap-south-1"}})
    prov = cfg["llm"]["roles"]["main"]["provider"]
    assert type(prov) is str  # exactly str, not a Provider subclass instance
    yaml.safe_dump(cfg)  # must not raise a RepresenterError


def test_bedrock_forces_api_key_env_none_even_when_inherited():
    """Regression: DEFAULT_CONFIG ships api_key_env=GEMINI_API_KEY; once it's
    deep-merged into a bedrock config that scalar leaks into llm.api_key_env. A
    bedrock provider must still resolve api_key_env to None so the pre-flight
    doesn't demand an unrelated key. Covers both parent and role-override."""
    parent = _normalize_llm_roles({
        "llm": {
            "provider": "bedrock",
            "model": "moonshot.kimi-k2-thinking",
            "api_key_env": "GEMINI_API_KEY",  # the leaked default
            "aws_region": "ap-south-1",
            "narrator": {"provider": "bedrock", "model": "moonshotai.kimi-k2.5", "api_key_env": "GEMINI_API_KEY"},
        },
    })
    assert parent["llm"]["roles"]["main"]["api_key_env"] is None
    assert parent["llm"]["roles"]["narrator"]["api_key_env"] is None


def test_find_missing_role_api_keys_ignores_bedrock(monkeypatch):
    """A bedrock role has no api_key_env, so the api-key pre-flight reports
    nothing for it even with no AWS env set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "bedrock",
            "model": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            "aws_region": "us-east-1",
            "max_tokens": 1024,
        },
    })
    assert find_missing_role_api_keys(cfg) == {}


def test_find_bedrock_roles_missing_region_flags_when_unset(monkeypatch):
    """No aws_region and no AWS_REGION env -> the bedrock role is flagged."""
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    cfg = _normalize_llm_roles({
        "llm": {"provider": "bedrock", "model": "us.anthropic.claude-3-5-sonnet-20241022-v2:0", "max_tokens": 1024},
    })
    assert "main" in find_bedrock_roles_missing_region(cfg)


def test_find_bedrock_roles_missing_region_satisfied_by_config(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    cfg = _normalize_llm_roles({
        "llm": {"provider": "bedrock", "model": "m", "aws_region": "us-east-1", "max_tokens": 1024},
    })
    assert find_bedrock_roles_missing_region(cfg) == []


def test_find_bedrock_roles_missing_region_satisfied_by_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    cfg = _normalize_llm_roles({
        "llm": {"provider": "bedrock", "model": "m", "max_tokens": 1024},
    })
    assert find_bedrock_roles_missing_region(cfg) == []


def test_find_bedrock_roles_missing_region_ignores_non_bedrock(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    cfg = _normalize_llm_roles(_flat())  # anthropic main
    assert find_bedrock_roles_missing_region(cfg) == []


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


def test_main_provider_gemini_resolves_default_api_key():
    """Gemini as the parent/main provider gets GEMINI_API_KEY by default, and
    every role inherits it unless overridden."""
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "max_tokens": 1024,
        },
    })
    main = cfg["llm"]["roles"]["main"]
    assert main["provider"] == "gemini"
    assert main["model"] == "gemini-2.5-pro"
    assert main["api_key_env"] == "GEMINI_API_KEY"


def test_find_missing_role_api_keys_reports_gemini_key(monkeypatch):
    """A mixed Anthropic-main + Gemini-narrator config with both envs unset
    reports GEMINI_API_KEY alongside ANTHROPIC_API_KEY in one pass."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = _normalize_llm_roles(_flat({
        "narrator": {"provider": "gemini", "model": "gemini-2.5-flash-lite", "max_tokens": 60},
    }))
    missing = find_missing_role_api_keys(cfg)
    assert set(missing.keys()) == {"ANTHROPIC_API_KEY", "GEMINI_API_KEY"}
    assert "narrator" in missing["GEMINI_API_KEY"]


def test_find_missing_role_api_keys_satisfied_when_gemini_key_set(monkeypatch):
    """All-Gemini config with GEMINI_API_KEY present has nothing missing."""
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "max_tokens": 1024,
            "narrator": {"model": "gemini-2.5-flash-lite", "max_tokens": 60},
        },
    })
    assert find_missing_role_api_keys(cfg) == {}


def test_build_run_llms_wires_all_gemini_roles(monkeypatch):
    """End-to-end: a gemini main + gemini decompose/narrator config builds the
    backends run_agent consumes, keyed by its kwarg names. Patched backend so we
    don't need the google-genai SDK."""
    from fabri import runtime
    built = []

    class _Stub:
        def __init__(self, model, tools, max_tokens, api_key_env):
            built.append({"model": model, "api_key_env": api_key_env})

    monkeypatch.setattr(runtime, "GeminiLLMBackend", _Stub)
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "max_tokens": 1024,
            "decompose": {"model": "gemini-2.5-flash"},
            "narrator": {"model": "gemini-2.5-flash-lite", "max_tokens": 60},
        },
    })
    llms = runtime.build_run_llms(cfg, tool_defs=[])
    assert isinstance(llms["llm"], _Stub)          # main always present
    assert isinstance(llms["decompose_llm"], _Stub)
    assert isinstance(llms["narrator_llm"], _Stub)
    assert llms["planner_llm"] is None             # unset -> falls back at runtime
    models = sorted(b["model"] for b in built)
    assert models == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]
    assert all(b["api_key_env"] == "GEMINI_API_KEY" for b in built)


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
