"""OpenAILLMBackend's new optional `base_url` kwarg is the OpenRouter hook.
Both tests monkeypatch the openai SDK so the suite runs without the package
installed."""
import sys
import types


def _install_fake_openai(monkeypatch):
    """Inject a fake `openai` module exposing just enough surface for
    OpenAILLMBackend.__init__ to construct a client and capture kwargs."""
    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake = types.ModuleType("openai")
    fake.OpenAI = _FakeClient
    # OpenAILLMBackend imports `openai` lazily inside __init__; injecting into
    # sys.modules is enough.
    monkeypatch.setitem(sys.modules, "openai", fake)
    return captured


def test_openai_backend_passes_base_url_to_sdk(monkeypatch):
    captured = _install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub")
    from fabri.core.llm import OpenAILLMBackend
    OpenAILLMBackend(
        model="anthropic/claude-haiku-4-5",
        tools=[],
        max_tokens=60,
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    assert captured["api_key"] == "stub"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"


def test_openai_backend_omits_base_url_when_not_set(monkeypatch):
    """When `base_url` is None (the default OpenAI endpoint), don't pass
    the kwarg to openai.OpenAI -- the SDK fills in its own default."""
    captured = _install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "stub")
    from fabri.core.llm import OpenAILLMBackend
    OpenAILLMBackend(model="gpt-4o", tools=[], max_tokens=1024, api_key_env="OPENAI_API_KEY")
    assert "base_url" not in captured


def test_build_role_llm_openrouter_sets_pinned_base_url(monkeypatch):
    """The `openrouter` provider sugar in runtime._instantiate pins the
    OpenRouter endpoint when the role config doesn't spell one out."""
    captured = _install_fake_openai(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "stub")
    from fabri.config import _normalize_llm_roles
    from fabri.runtime import build_role_llm
    cfg = _normalize_llm_roles({
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 1024,
            "narrator": {
                "provider": "openrouter",
                "model": "anthropic/claude-haiku-4-5",
                "max_tokens": 60,
            },
        },
    })
    build_role_llm(cfg, "narrator")
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
