from __future__ import annotations

import logging

import pytest

from fabri.tools.credential_store import (
    CredentialNotFoundError,
    CredentialStore,
    EnvCredentialStore,
    credential_env_var,
)
from fabri.tools.secret_refs import MalformedCredentialRefError, resolve_secret


def test_env_var_mapping_is_deterministic() -> None:
    assert credential_env_var("slack", "acme-eng") == "FABRI_CRED_SLACK_ACME_ENG"
    assert (
        credential_env_var("git-hub", "Acme/prod.v2")
        == "FABRI_CRED_GIT_HUB_ACME_PROD_V2"
    )


def test_env_store_implements_protocol_and_resolves_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EnvCredentialStore()
    monkeypatch.setenv("FABRI_CRED_SLACK_ACME_ENG", "xoxb-test-secret")

    assert isinstance(store, CredentialStore)
    assert store.get("slack", "acme-eng") == "xoxb-test-secret"


def test_resolve_secret_uses_default_env_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FABRI_CRED_GITHUB_ACME", "github-test-secret")

    assert resolve_secret("github:acme") == "github-test-secret"


def test_resolve_secret_accepts_an_injected_store() -> None:
    class MemoryCredentialStore:
        def get(self, provider: str, handle: str) -> str:
            assert (provider, handle) == ("stripe", "billing")
            return "stripe-test-secret"

    assert resolve_secret("stripe:billing", MemoryCredentialStore()) == "stripe-test-secret"


@pytest.mark.parametrize(
    "ref",
    [
        "no-colon",
        ":handle",
        "provider:",
        "provider:handle:extra",
    ],
)
def test_malformed_refs_raise_typed_error_without_echoing_input(ref: str) -> None:
    with pytest.raises(MalformedCredentialRefError) as caught:
        resolve_secret(ref)

    assert not isinstance(caught.value, KeyError)
    assert ref not in str(caught.value)
    assert ref not in repr(caught.value)


def test_missing_env_var_names_safe_variable_without_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_var = "FABRI_CRED_SMTP_ACME_MAIL"
    secret = "smtp-super-secret"
    monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(CredentialNotFoundError) as caught:
        resolve_secret("smtp:acme-mail")

    assert env_var in str(caught.value)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert not isinstance(caught.value, KeyError)


def test_secret_never_appears_in_repr_errors_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "never-leak-this-secret"
    store = EnvCredentialStore()
    monkeypatch.setenv("FABRI_CRED_SLACK_PRIVATE", secret)
    caplog.set_level(logging.DEBUG)

    assert resolve_secret("slack:private", store) == secret
    assert secret not in repr(store)

    with pytest.raises(MalformedCredentialRefError) as malformed:
        resolve_secret(secret, store)
    with pytest.raises(CredentialNotFoundError) as missing:
        resolve_secret("slack:missing", store)

    assert secret not in str(malformed.value)
    assert secret not in repr(malformed.value)
    assert secret not in str(missing.value)
    assert secret not in repr(missing.value)
    assert secret not in caplog.text


from fabri.tools.credential_store import (  # noqa: E402
    BROKER_ENV_NAMES,
    BROKER_KEY_ENV,
    BROKER_SOCKET_ENV,
    env_without_credentials,
    subagent_credential_names,
    wildcard_credential_names,
)


def test_broker_names_are_stripped_by_default() -> None:
    env = {
        BROKER_SOCKET_ENV: "/tmp/b.sock",
        BROKER_KEY_ENV: "k",
        "FABRI_CRED_SLACK_X": "t",
        "PATH": "/bin",
    }
    assert env_without_credentials(env) == {"PATH": "/bin"}
    # An explicit keep of one broker name is exact and keeps nothing else.
    assert env_without_credentials(env, keep={BROKER_KEY_ENV}) == {BROKER_KEY_ENV: "k", "PATH": "/bin"}


def test_keep_broker_keeps_both_names_and_nothing_else_prefixed() -> None:
    env = {
        BROKER_SOCKET_ENV: "/tmp/b.sock",
        BROKER_KEY_ENV: "k",
        "FABRI_CRED_SLACK_X": "t",
        "PATH": "/bin",
    }
    kept = env_without_credentials(env, keep_broker=True)
    assert kept == {BROKER_SOCKET_ENV: "/tmp/b.sock", BROKER_KEY_ENV: "k", "PATH": "/bin"}


def test_subagent_names_include_broker_vars_when_both_set(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(BROKER_SOCKET_ENV, "/tmp/b.sock")
    monkeypatch.setenv(BROKER_KEY_ENV, "k")
    # The config path is never read in broker mode.
    names = subagent_credential_names(tmp_path / "does-not-exist.yaml")
    assert names == BROKER_ENV_NAMES
    assert isinstance(names, frozenset)


def test_subagent_names_add_neither_broker_var_when_only_one_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(BROKER_KEY_ENV, "k")
    monkeypatch.delenv(BROKER_SOCKET_ENV, raising=False)
    names = subagent_credential_names(tmp_path / "does-not-exist.yaml")
    assert not (names & BROKER_ENV_NAMES)


def test_wildcard_names_come_from_granted_refs_with_exact_provider() -> None:
    refs = [("slack", "acme"), ("slack_bot", "x"), ("linear", "y"), ("broker", "key")]
    assert wildcard_credential_names(["slack:*"], refs) == frozenset({"FABRI_CRED_SLACK_ACME"})
    assert wildcard_credential_names(["slack:acme"], refs) == frozenset()
    assert wildcard_credential_names(["broker:*"], refs) == frozenset()
    assert wildcard_credential_names(["slack:*", "linear:*"], refs) == frozenset(
        {"FABRI_CRED_SLACK_ACME", "FABRI_CRED_LINEAR_Y"}
    )
