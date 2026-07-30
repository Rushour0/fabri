import hashlib
import hmac
import os
import time
import unittest
import urllib.parse
from unittest.mock import patch

from fabri.service.slack_oauth import (
    SLACK_AUTHORIZE,
    SLACK_BOT_SCOPES,
    build_install_redirect,
    sign_state,
    verify_state,
)


class SlackOAuthStateTests(unittest.TestCase):
    def test_sign_and_verify_state_round_trip(self):
        secret = "test-signing-secret"

        state = sign_state(secret)

        self.assertTrue(verify_state(state, secret))

    def test_empty_secret_fails_closed(self):
        self.assertFalse(verify_state("malformed.state.with.extra.parts", ""))
        with self.assertRaises(ValueError):
            sign_state("")

    def test_expired_state_is_rejected(self):
        secret = "test-signing-secret"
        nonce = "test-nonce"
        expiry = int(time.time()) - 1
        payload = f"{nonce}.{expiry}"
        sig = hmac.new(
            secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

        self.assertFalse(verify_state(f"{payload}.{sig}", secret))

    def test_tampered_state_is_rejected(self):
        secret = "test-signing-secret"
        state = sign_state(secret)
        payload, sig = state.rsplit(".", 1)
        replacement = "0" if sig[-1] != "0" else "1"
        tampered_state = f"{payload}.{sig[:-1]}{replacement}"

        self.assertFalse(verify_state(tampered_state, secret))


class SlackOAuthRedirectTests(unittest.TestCase):
    def test_missing_configuration_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(build_install_redirect())

    def test_redirect_uses_pinned_scope_and_callback(self):
        secret = "test-signing-secret"
        environment = {
            "SLACK_CLIENT_ID": "client-id",
            "SLACK_SIGNING_SECRET": secret,
            "FABRI_PUBLIC_BASE_URL": "https://fabri.example/",
        }

        with patch.dict(os.environ, environment, clear=True):
            redirect = build_install_redirect()

        self.assertIsNotNone(redirect)
        parsed = urllib.parse.urlparse(redirect)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", SLACK_AUTHORIZE
        )
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["scope"], [SLACK_BOT_SCOPES])
        self.assertEqual(
            query["redirect_uri"], ["https://fabri.example/slack/oauth/callback"]
        )
        self.assertTrue(verify_state(query["state"][0], secret))
