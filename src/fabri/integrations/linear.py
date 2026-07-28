"""Small, synchronous Linear GraphQL connector."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fabri.tools.security.ssrf import ValidatingRedirect, validate_url

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

_opener = urllib.request.build_opener(ValidatingRedirect)

_FETCH_ISSUE_QUERY = """
query FetchIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    state {
      name
    }
  }
}
"""

_COMMENT_ISSUE_MUTATION = """
mutation CommentIssue($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
    comment {
      url
    }
  }
}
"""

_ISSUE_STATES_QUERY = """
query IssueWorkflowStates($id: String!) {
  issue(id: $id) {
    team {
      states {
        nodes {
          id
          name
        }
      }
    }
  }
}
"""

_SET_STATE_MUTATION = """
mutation SetIssueState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) {
    success
  }
}
"""


class LinearError(RuntimeError):
    """Raised when Linear rejects a request or returns an invalid response."""


def _redact_token(text: str, token: str) -> str:
    """Remove the active credential from text that may enter an exception."""
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


def _auth_header(token: str, scheme: str = "auto") -> str:
    """Return Linear's Authorization value for the requested token scheme."""
    if scheme == "auto":
        return token if token.startswith("lin_api_") else f"Bearer {token}"
    if scheme == "bearer":
        return f"Bearer {token}"
    if scheme == "bare":
        return token
    raise LinearError("unsupported Linear authentication scheme")


def _graphql(
    query: str,
    variables: dict[str, object],
    *,
    token: str,
    timeout: float = 30,
    auth_scheme: str = "auto",
) -> dict[str, object]:
    """Execute a Linear GraphQL operation and return its ``data`` object."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": _auth_header(token, auth_scheme),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        validate_url(LINEAR_GRAPHQL_URL)
    except ValueError as exc:
        message = _redact_token(str(exc), token)
        # The original exception may carry request data, so suppress it in traces.
        raise LinearError(f"Linear request refused: {message}") from None

    try:
        with _opener.open(request, timeout=timeout) as response:
            raw_body = response.read()
            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else None
    except urllib.error.HTTPError as exc:
        raise LinearError(
            f"Linear request failed with HTTP {exc.code}: [REDACTED]"
        ) from None
    except urllib.error.URLError:
        raise LinearError("Linear request failed: [REDACTED]") from None

    response_body = raw_body.decode("utf-8", errors="replace")
    if status != 200:
        raise LinearError(
            f"Linear request failed with HTTP {status}: [REDACTED]"
        )

    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        raise LinearError(
            "Linear returned invalid JSON: [REDACTED]"
        ) from None

    if not isinstance(payload, dict):
        raise LinearError("Linear returned an invalid response: [REDACTED]")
    if payload.get("errors"):
        raise LinearError("Linear GraphQL errors: [REDACTED]")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise LinearError("Linear returned an invalid data object: [REDACTED]")
    return data


def fetch_issue(
    issue_id: str,
    *,
    token: str,
    auth_scheme: str = "auto",
) -> dict[str, object]:
    """Fetch an issue and flatten its workflow state name."""
    data = _graphql(
        _FETCH_ISSUE_QUERY,
        {"id": issue_id},
        token=token,
        auth_scheme=auth_scheme,
    )
    issue = data.get("issue")
    if issue is None:
        raise LinearError("issue not found")
    if not isinstance(issue, dict):
        raise LinearError("Linear returned an invalid issue")

    state = issue.get("state")
    state_name = state.get("name") if isinstance(state, dict) else None
    return {
        "id": issue.get("id"),
        "identifier": issue.get("identifier"),
        "title": issue.get("title"),
        "description": issue.get("description"),
        "url": issue.get("url"),
        "state": state_name,
    }


def comment_issue(
    issue_id: str,
    body: str,
    *,
    token: str,
    auth_scheme: str = "auto",
) -> str:
    """Create a comment on an issue and return the comment URL."""
    data = _graphql(
        _COMMENT_ISSUE_MUTATION,
        {"issueId": issue_id, "body": body},
        token=token,
        auth_scheme=auth_scheme,
    )
    result = data.get("commentCreate")
    if not isinstance(result, dict) or not result.get("success"):
        raise LinearError("Linear comment creation failed")

    comment = result.get("comment")
    if not isinstance(comment, dict):
        raise LinearError("Linear returned an invalid comment")
    url = comment.get("url")
    if not isinstance(url, str) or not url:
        raise LinearError("Linear returned an invalid comment URL")
    return url


def set_state(
    issue_id: str,
    state_name: str,
    *,
    token: str,
    auth_scheme: str = "auto",
) -> bool:
    """Move an issue to a named workflow state."""
    data = _graphql(
        _ISSUE_STATES_QUERY,
        {"id": issue_id},
        token=token,
        auth_scheme=auth_scheme,
    )

    issue = data.get("issue")
    team = issue.get("team") if isinstance(issue, dict) else None
    states = team.get("states") if isinstance(team, dict) else None
    nodes = states.get("nodes") if isinstance(states, dict) else None

    state_id: str | None = None
    if isinstance(nodes, list):
        for state in nodes:
            state_candidate = state.get("id") if isinstance(state, dict) else None
            if (
                isinstance(state, dict)
                and state.get("name") == state_name
                and isinstance(state_candidate, str)
            ):
                state_id = state_candidate
                break

    if state_id is None:
        raise LinearError("workflow state not found")

    update_data = _graphql(
        _SET_STATE_MUTATION,
        {"id": issue_id, "stateId": state_id},
        token=token,
        auth_scheme=auth_scheme,
    )
    update = update_data.get("issueUpdate")
    if not isinstance(update, dict):
        raise LinearError("Linear returned an invalid issue update")
    return bool(update.get("success"))
