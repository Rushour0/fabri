"""Credential-gated live test for the Linear connector."""

from __future__ import annotations

import os

import pytest

from fabri.integrations.linear import comment_issue


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("FABRI_CRED_LINEAR_DEFAULT"),
    reason="FABRI_CRED_LINEAR_DEFAULT is not set",
)
@pytest.mark.skipif(
    not os.environ.get("FABRI_LINEAR_TEST_ISSUE"),
    reason="FABRI_LINEAR_TEST_ISSUE is not set",
)
def test_comment_issue_live() -> None:
    from fabri.tools.credential_store import resolve_secret

    token = resolve_secret("linear:default")
    issue_id = os.environ["FABRI_LINEAR_TEST_ISSUE"]

    comment_url = comment_issue(
        issue_id,
        "Fabri Linear connector live test.",
        token=token,
    )

    assert isinstance(comment_url, str)
    assert comment_url
    assert comment_url.startswith("http")
