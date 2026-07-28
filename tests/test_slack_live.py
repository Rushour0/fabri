import os
import re

import pytest

from fabri.tools.recipes import slack_post


@pytest.mark.live
def test_slack_live_smoke():
    if not os.environ.get("FABRI_CRED_SLACK_DEFAULT"):
        pytest.skip("FABRI_CRED_SLACK_DEFAULT unset")
    channel = os.environ.get("FABRI_SLACK_TEST_CHANNEL")
    if not channel:
        pytest.skip("FABRI_SLACK_TEST_CHANNEL unset")
    out = slack_post.post_message(
        {"channel": channel, "text": "fabri repo-run live smoke"}
    )
    assert out["ok"] is True, out
    assert re.match(r"^\d+\.\d+$", out["result"]["ts"])
