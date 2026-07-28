"""Unit tests for the Linear GraphQL connector."""

from __future__ import annotations

import json
import traceback
import urllib.error
import urllib.request

import pytest

from fabri.integrations import linear

pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("token", "scheme", "expected"),
    [
        pytest.param(
            "lin_api_unit_test",
            "auto",
            "lin_api_unit_test",
            id="auto-linear-api-token",
        ),
        pytest.param(
            "unit-test-token",
            "auto",
            "Bearer unit-test-token",
            id="auto-bearer-token",
        ),
        pytest.param(
            "lin_api_unit_test",
            "bearer",
            "Bearer lin_api_unit_test",
            id="forced-bearer",
        ),
        pytest.param(
            "unit-test-token",
            "bare",
            "unit-test-token",
            id="forced-bare",
        ),
    ],
)
def test_auth_header(token: str, scheme: str, expected: str) -> None:
    assert linear._auth_header(token, scheme) == expected


def test_auth_header_rejects_unknown_scheme() -> None:
    with pytest.raises(linear.LinearError):
        linear._auth_header("unit-test-token", "unknown")


def test_graphql_posts_json_and_returns_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    validated: list[str] = []

    def validate(url: str) -> str:
        validated.append(url)
        return url

    def open_request(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        captured.update(request=request, timeout=timeout)
        return FakeResponse({"data": {"viewer": {"id": "viewer-1"}}})

    monkeypatch.setattr(linear, "validate_url", validate)
    monkeypatch.setattr(linear._opener, "open", open_request)

    data = linear._graphql(
        "query Viewer { viewer { id } }",
        {},
        token="unit-test-token",
        timeout=12,
    )

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert validated == [linear.LINEAR_GRAPHQL_URL]
    assert captured["timeout"] == 12
    assert request.get_method() == "POST"
    assert request.full_url == linear.LINEAR_GRAPHQL_URL
    assert request.get_header("Authorization") == "Bearer unit-test-token"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data or b"") == {
        "query": "query Viewer { viewer { id } }",
        "variables": {},
    }
    assert data == {"viewer": {"id": "viewer-1"}}


def test_graphql_errors_raise_and_redact_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "token-that-must-not-leak"

    def open_request(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        return FakeResponse(
            {
                "errors": [{"message": f"rejected credential {token}"}],
                "data": None,
            }
        )

    monkeypatch.setattr(linear, "validate_url", lambda url: url)
    monkeypatch.setattr(linear._opener, "open", open_request)

    with pytest.raises(linear.LinearError) as error:
        linear._graphql("query Broken { viewer { id } }", {}, token=token)

    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_graphql_validation_failure_never_opens_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_url(url: str) -> str:
        raise ValueError("refused test URL")

    def unexpected_open(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        pytest.fail("request must not open before URL validation passes")

    monkeypatch.setattr(linear, "validate_url", reject_url)
    monkeypatch.setattr(linear._opener, "open", unexpected_open)

    with pytest.raises(linear.LinearError, match="request refused"):
        linear._graphql(
            "query Blocked { viewer { id } }",
            {},
            token="unit-test-token",
        )


def test_graphql_non_200_response_raises_and_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "non-200-token-that-must-not-leak"

    def open_request(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        return FakeResponse(
            {"message": f"rejected {token}"},
            status=503,
        )

    monkeypatch.setattr(linear, "validate_url", lambda url: url)
    monkeypatch.setattr(linear._opener, "open", open_request)

    with pytest.raises(linear.LinearError) as error:
        linear._graphql("query Broken { viewer { id } }", {}, token=token)

    assert token not in str(error.value)
    assert "HTTP 503" in str(error.value)


def test_graphql_http_error_raises_and_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "http-error-token-that-must-not-leak"
    http_error = urllib.error.HTTPError(
        linear.LINEAR_GRAPHQL_URL,
        401,
        f"Unauthorized {token}",
        hdrs=None,
        fp=None,
    )

    def open_request(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        raise http_error

    monkeypatch.setattr(linear, "validate_url", lambda url: url)
    monkeypatch.setattr(linear._opener, "open", open_request)

    with pytest.raises(linear.LinearError) as error:
        linear._graphql("query Broken { viewer { id } }", {}, token=token)

    assert token not in str(error.value)
    assert "HTTP 401" in str(error.value)
    assert token not in "".join(traceback.format_exception(error.value))


def test_graphql_url_error_raises_and_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "url-error-token-that-must-not-leak"

    def open_request(
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> FakeResponse:
        raise urllib.error.URLError(f"connection rejected {token}")

    monkeypatch.setattr(linear, "validate_url", lambda url: url)
    monkeypatch.setattr(linear._opener, "open", open_request)

    with pytest.raises(linear.LinearError) as error:
        linear._graphql("query Broken { viewer { id } }", {}, token=token)

    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert token not in "".join(traceback.format_exception(error.value))


def test_fetch_issue_returns_flat_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        assert variables == {"id": "FAB-123"}
        assert token == "unit-test-token"
        assert auth_scheme == "bare"
        return {
            "issue": {
                "id": "issue-1",
                "identifier": "FAB-123",
                "title": "Ship the Linear connector",
                "description": "Connector implementation",
                "url": "https://linear.app/example/issue/FAB-123",
                "state": {"name": "In Progress"},
            }
        }

    monkeypatch.setattr(linear, "_graphql", graphql)

    issue = linear.fetch_issue(
        "FAB-123",
        token="unit-test-token",
        auth_scheme="bare",
    )

    assert issue == {
        "id": "issue-1",
        "identifier": "FAB-123",
        "title": "Ship the Linear connector",
        "description": "Connector implementation",
        "url": "https://linear.app/example/issue/FAB-123",
        "state": "In Progress",
    }


def test_fetch_issue_missing_issue_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        return {"issue": None}

    monkeypatch.setattr(linear, "_graphql", graphql)

    with pytest.raises(linear.LinearError, match="issue not found"):
        linear.fetch_issue("FAB-404", token="unit-test-token")


def test_fetch_issue_allows_missing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        return {
            "issue": {
                "id": "issue-1",
                "identifier": "FAB-123",
                "title": "Unscheduled issue",
                "description": None,
                "url": "https://linear.app/example/issue/FAB-123",
                "state": None,
            }
        }

    monkeypatch.setattr(linear, "_graphql", graphql)

    issue = linear.fetch_issue("FAB-123", token="unit-test-token")

    assert issue["state"] is None


def test_comment_issue_returns_comment_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        assert variables == {
            "issueId": "FAB-123",
            "body": "Implementation is ready.",
        }
        return {
            "commentCreate": {
                "success": True,
                "comment": {
                    "url": "https://linear.app/example/issue/FAB-123#comment-1"
                },
            }
        }

    monkeypatch.setattr(linear, "_graphql", graphql)

    url = linear.comment_issue(
        "FAB-123",
        "Implementation is ready.",
        token="unit-test-token",
    )

    assert url == "https://linear.app/example/issue/FAB-123#comment-1"


def test_comment_issue_unsuccessful_mutation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        return {"commentCreate": {"success": False, "comment": None}}

    monkeypatch.setattr(linear, "_graphql", graphql)

    with pytest.raises(linear.LinearError, match="comment creation failed"):
        linear.comment_issue("FAB-123", "Comment", token="unit-test-token")


def test_set_state_resolves_state_and_updates_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        calls.append({"query": query, "variables": variables})
        if "issueUpdate" in query:
            return {"issueUpdate": {"success": True}}
        return {
            "issue": {
                "team": {
                    "states": {
                        "nodes": [
                            {"id": "state-backlog", "name": "Backlog"},
                            {"id": "state-done", "name": "Done"},
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(linear, "_graphql", graphql)

    assert linear.set_state(
        "FAB-123",
        "Done",
        token="unit-test-token",
    )
    assert [call["variables"] for call in calls] == [
        {"id": "FAB-123"},
        {"id": "FAB-123", "stateId": "state-done"},
    ]


def test_set_state_missing_state_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def graphql(
        query: str,
        variables: dict[str, object],
        *,
        token: str,
        timeout: float = 30,
        auth_scheme: str = "auto",
    ) -> dict[str, object]:
        return {
            "issue": {
                "team": {
                    "states": {
                        "nodes": [{"id": "state-backlog", "name": "Backlog"}]
                    }
                }
            }
        }

    monkeypatch.setattr(linear, "_graphql", graphql)

    with pytest.raises(linear.LinearError, match="workflow state not found"):
        linear.set_state("FAB-123", "Done", token="unit-test-token")
