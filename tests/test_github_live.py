"""Live smoke test for GitHub App authentication."""
from __future__ import annotations

import os

import pytest

from fabri.repo.github_auth import AppAuth


@pytest.mark.live
def test_github_app_live_token() -> None:
    required = (
        "FABRI_CRED_GITHUB_APP_ID",
        "FABRI_CRED_GITHUB_INSTALLATION_ID",
        "FABRI_CRED_GITHUB_PRIVATE_KEY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip(f"GitHub App credentials unset: {', '.join(missing)}")

    token = AppAuth().get_token()
    assert isinstance(token, str)
    assert token
    assert token.startswith("ghs_")
