"""An http header spec may not copy one of fabri's own secrets out of the
host environment, at validation or at resolve time."""
import pytest

from fabri.tools.http_call import HeaderSpecError, check_header_specs, resolve_headers

FABRI_NAMES = ["FABRI_CRED_BROKER_KEY", "FABRI_CRED_BROKER_SOCKET", "FABRI_CRED_OTHER_PROD",
               "FABRI_ADMIN_TOKEN", "FABRI_SERVICE_TOKEN"]


@pytest.mark.parametrize("name", FABRI_NAMES)
def test_check_header_specs_refuses_fabri_secret_env(name):
    with pytest.raises(HeaderSpecError, match="fabri credential"):
        check_header_specs({"Authorization": {"env": name, "prefix": "Bearer "}})


@pytest.mark.parametrize("name", FABRI_NAMES)
def test_resolve_headers_refuses_fabri_secret_env_without_leaking(monkeypatch, name):
    monkeypatch.setenv(name, "super-secret-value-123")
    with pytest.raises(HeaderSpecError) as exc:
        resolve_headers({"X-Key": {"env": name}})
    assert "super-secret-value-123" not in str(exc.value)


def test_ordinary_env_header_still_resolves(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    check_header_specs({"Authorization": {"env": "SLACK_BOT_TOKEN", "prefix": "Bearer "}})
    assert resolve_headers({"Authorization": {"env": "SLACK_BOT_TOKEN", "prefix": "Bearer "}}) == {
        "Authorization": "Bearer xoxb-test"}
