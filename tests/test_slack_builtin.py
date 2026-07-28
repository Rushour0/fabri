from fabri.runtime import build_tools
from fabri.tools.recipes import slack_post


def test_slack_post_builtin_is_discovered_and_enabled(tmp_path):
    reg = build_tools(
        {
            "manifest_dir": "builtin",
            "sandbox_root": str(tmp_path),
            "enabled": ["slack_post"],
        }
    )

    assert "slack_post" in reg.tools
    manifest = reg.tools["slack_post"]
    assert manifest.name == "slack_post"
    required = manifest.input_schema.get("required", [])
    assert "channel" in required and "text" in required


def test_slack_post_invoke_shape(monkeypatch):
    # Exercise the entrypoint the shim delegates to, network-free.
    monkeypatch.setenv("FABRI_CRED_SLACK_DEFAULT", "xoxb-builtin-test")
    monkeypatch.setattr(slack_post, "validate_url", lambda url: url)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true, "ts": "1.2", "channel": "C1"}'

    monkeypatch.setattr(
        slack_post._opener, "open", lambda request, timeout: _Response()
    )

    out = slack_post.post_message({"channel": "C1", "text": "hi"})

    assert out == {"ok": True, "result": {"ts": "1.2", "channel": "C1"}}
