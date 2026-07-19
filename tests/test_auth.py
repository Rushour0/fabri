from __future__ import annotations

from fabri.service.auth import (
    UserStore,
    build_clear_cookie,
    build_set_cookie,
    parse_cookie,
    sign_session,
    verify_session,
)


def test_user_store_creates_and_verifies_users(tmp_path):
    store = UserStore(tmp_path / "auth.db")
    user_id = store.create_user(" User@example.com ", "correct-password")

    assert store.verify_user("user@example.com", "correct-password") == user_id
    assert store.verify_user("user@example.com", "wrong-password") is None
    assert store.verify_user("nobody@example.com", "correct-password") is None
    assert store.get_user(user_id) == {"id": user_id, "email": "user@example.com"}

    try:
        store.create_user("USER@example.com", "another-password")
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate email must fail")


def test_sessions_reject_tampering_expiry_and_malformed_tokens():
    token = sign_session("user-id", "secret", 60)

    assert verify_session(token, "secret") == "user-id"
    assert verify_session(token + "x", "secret") is None
    assert verify_session(sign_session("user-id", "secret", -1), "secret") is None
    assert verify_session("not-a-token", "secret") is None


def test_cookie_helpers_round_trip_and_secure_toggle():
    token = "user.123.signature"
    secure_cookie = build_set_cookie(token, 60, secure=True)
    insecure_cookie = build_set_cookie(token, 60, secure=False)

    assert parse_cookie(secure_cookie)["fabri_session"] == token
    assert "; Secure" in secure_cookie
    assert "; Secure" not in insecure_cookie
    assert "Max-Age=0" in build_clear_cookie(secure=True)
