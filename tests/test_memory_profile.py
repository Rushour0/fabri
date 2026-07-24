import yaml
import pytest

from fabri.config import ConfigError, load_config

pytestmark = pytest.mark.unit

EVOLVING_DEFAULTS = {
    "guideline_max_tokens": 120,
    "tiering_enabled": True,
    "convention_mining_enabled": True,
    "record_postmortems": True,
    "success_pattern_requires_evidence": True,
    "memory_action_enabled": True,
    "memory_action_apply_enabled": False,
}


def _load(tmp_path, text: str) -> dict:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(text)
    return load_config(str(config_path))


def _config_bytes(config: dict) -> bytes:
    return yaml.safe_dump(config, sort_keys=False).encode()


def test_evolving_sets_each_profile_key_when_absent(tmp_path) -> None:
    config = _load(tmp_path, "memory:\n  profile: evolving\n")

    assert {
        key: config["memory"][key]
        for key in EVOLVING_DEFAULTS
    } == EVOLVING_DEFAULTS
    assert "profile" not in config["memory"]


def test_explicit_memory_values_win_over_evolving_profile(tmp_path) -> None:
    config = _load(
        tmp_path,
        """\
memory:
  profile: evolving
  guideline_max_tokens: 64
  convention_mining_enabled: false
""",
    )

    assert config["memory"]["guideline_max_tokens"] == 64
    assert config["memory"]["convention_mining_enabled"] is False
    assert config["memory"]["tiering_enabled"] is True


def test_standard_and_absent_profiles_match_today_byte_for_byte(tmp_path) -> None:
    absent = _load(tmp_path, "{}\n")
    standard = _load(tmp_path, "memory:\n  profile: standard\n")
    today = load_config(None)

    assert _config_bytes(standard) == _config_bytes(absent) == _config_bytes(today)


def test_unknown_profile_names_allowed_values(tmp_path) -> None:
    with pytest.raises(ConfigError) as caught:
        _load(tmp_path, "memory:\n  profile: experimental\n")

    message = str(caught.value)
    assert "memory.profile" in message
    assert "'standard'" in message
    assert "'evolving'" in message
    assert "'experimental'" in message


def test_evolving_does_not_enable_human_authority_gates(tmp_path) -> None:
    baseline = _load(tmp_path, "{}\n")["memory"]
    evolving = _load(tmp_path, "memory:\n  profile: evolving\n")["memory"]

    for key in (
        "convention_approvals",
        "convention_trusted_sources",
        "convention_core_enabled",
        "memory_action_apply_enabled",
    ):
        assert evolving[key] == baseline[key]


def test_evolving_preserves_explicit_human_authority_gate_values(tmp_path) -> None:
    config = _load(
        tmp_path,
        """\
memory:
  profile: evolving
  convention_approvals: [{scope: agent, key: status}]
  convention_trusted_sources: [company_config]
  convention_core_enabled: true
  memory_action_apply_enabled: true
""",
    )

    memory = config["memory"]
    assert memory["convention_approvals"] == [{"scope": "agent", "key": "status"}]
    assert memory["convention_trusted_sources"] == ["company_config"]
    assert memory["convention_core_enabled"] is True
    assert memory["memory_action_apply_enabled"] is True
