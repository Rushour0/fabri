"""Approval floors on the run path (setups.toolset.resolve_tools).

`approval_mode: tighten` stops a layer's tools.yaml from lowering a class
below fabri's own pick, but only while the key is there. The catalog floor
for http and openapi tools does not depend on any key: it is bound to the
request URL's host, so a renamed tool, an edited method or removed markers
cannot take a Slack post out of approval."""
from __future__ import annotations

import shutil
from pathlib import Path

from fabri.setups.resolver import resolve_setup
from fabri.setups.toolset import approval_warnings, resolve_tools

FIXTURES = Path(__file__).parent / "fixtures" / "openapi"


def _pets_layer(tmp_path, extra: str):
    layer = tmp_path / "pets"
    (layer / "tools").mkdir(parents=True)
    shutil.copy(FIXTURES / "petstore.yaml", layer / "tools" / "petstore.yaml")
    (layer / "tools" / "tools.yaml").write_text(
        "sources:\n  - kind: openapi\n    name: pets\n    spec: petstore.yaml\n"
        "    approval: {pet_create: read, pet_adopt: read}\n" + extra + "allow: ['*']\n")
    return layer


def _http_layer(tmp_path, *sources: str):
    layer = tmp_path / "ops"
    (layer / "tools").mkdir(parents=True)
    (layer / "tools" / "tools.yaml").write_text("sources:\n" + "".join(sources) + "allow: ['*']\n")
    return layer


def _http(name: str, url: str, method: str, extra: str = "") -> str:
    return (f"  - kind: http\n    name: {name}\n    url: {url}\n    method: {method}\n"
            f"    headers: {{Authorization: {{env: SLACK_BOT_TOKEN, prefix: 'Bearer '}}}}\n{extra}")


def _resolve(layer):
    return resolve_tools(resolve_setup([layer]), use_cache=False)


def test_tighten_key_stops_yaml_lowering_a_class(tmp_path):
    loose = _resolve(_pets_layer(tmp_path / "a", ""))
    assert loose.tools["pet_create"].approval == "read"
    tight = _resolve(_pets_layer(tmp_path / "b", "    approval_mode: tighten\n"))
    for name in ("pet_create", "pet_adopt"):
        tool = tight.tools[name]
        assert tool.approval == tool.safe_approval != "read", name
    assert tight.tools["pet_list"].approval == "read"


def test_renamed_slack_post_with_get_stays_always_approve(tmp_path):
    # Markers kept, tool renamed so no name rule fires, method edited to GET.
    layer = _http_layer(tmp_path, _http("ops_note", "https://slack.com/api/chat.postMessage", "GET",
                                        "    approval: read\n    approval_mode: tighten\n    catalog: slack\n"))
    tool = _resolve(layer).tools["ops_note"]
    assert tool.approval == "always_approve" and tool.safe_approval == "always_approve"


def test_slack_post_with_markers_removed_stays_always_approve(tmp_path):
    layer = _http_layer(
        tmp_path,
        _http("slack_post_message", "https://slack.com/api/chat.postMessage", "POST", "    approval: read\n"),
        _http("ops_note", "https://slack.com/api/chat.postMessage", "GET", "    approval: read\n"),
        _http("ops_peek", "https://SLACK.com./api/chat.postMessage", "GET", "    approval: read\n"))
    tools = _resolve(layer).tools
    for name in ("slack_post_message", "ops_note", "ops_peek"):
        assert tools[name].approval == "always_approve", name


def test_slack_reads_stay_read_only_with_get(tmp_path):
    layer = _http_layer(
        tmp_path,
        _http("slack_channels_list", "https://slack.com/api/conversations.list", "GET"),
        _http("ops_history", "https://slack.com/api/conversations.history", "GET"),
        _http("ops_list_post", "https://slack.com/api/conversations.list", "POST", "    approval: read\n"),
        _http("ops_sneaky", "https://slack.com/api/conversations.list/../chat.postMessage", "GET"))
    tools = _resolve(layer).tools
    assert tools["slack_channels_list"].approval == "read"
    assert tools["ops_history"].approval == "read"
    assert tools["ops_list_post"].approval == "always_approve"
    assert tools["ops_sneaky"].approval == "always_approve"


def test_unrelated_host_without_markers_behaves_as_before(tmp_path):
    layer = _http_layer(tmp_path, _http("notes_add", "https://notes.test/api/notes", "POST",
                                        "    approval: read\n"))
    resolved = _resolve(layer)
    tool = resolved.tools["notes_add"]
    assert tool.approval == "read" and tool.safe_approval == "write"
    warned = [t.name for t, _ in approval_warnings(resolved)]
    assert warned == ["notes_add"]
