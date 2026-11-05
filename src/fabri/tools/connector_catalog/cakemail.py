"""Cakemail API: a curated set of 28 operations from a pinned, trimmed spec.

The connector writes one openapi source that points at a copy of
``specs/cakemail.json`` (trimmed from Cakemail API 1.30.2 by
``scripts/trim_cakemail_spec.py``). It exposes 21 reads and 7 writes over
lists, contacts, segments, tags, senders, templates and campaigns. Token,
user, account, MFA, password, DKIM, sender changes, exports, workflows,
forms and webhooks are left out of the spec and also denied by name.

Every write needs approval. The floor is bound to the request target:
``HOSTS`` claims api.cakemail.dev and ``READS`` lists the 21 curated GET
paths, so any other call to that host is always_approve, whatever the
tool is called and whether or not the YAML still carries
``approval_mode`` and ``catalog``. ``minimum`` adds the name-keyed floor
for sources that keep the ``catalog: cakemail`` marker.

Tool names are the spec's readable names (``parse_operations`` with
``naming="readable"``) prefixed with the source name, written to the
source's ``names`` map so they stay stable whatever the YAML filters do.
"""
from __future__ import annotations

import functools
import json
from importlib import resources
from typing import Any

from fabri.tools.connector_catalog import Connector, ConnectorError, _credential

BASE_URL = "https://api.cakemail.dev"
SPEC_FILE = "cakemail.json"

READ_METHODS = frozenset({"GET"})

# What the trim script keeps from the full spec, and what the source denies
# at build time as a second check.
TRIM_METHODS = ("GET", "POST", "PATCH")
DROP_PATH_PREFIXES = ("/token", "/users", "/accounts", "/brands")
DROP_PATH_PARTS = ("pats",)
# The only calls kept under a dropped prefix: reading the sender list.
KEEP_UNDER_DROPPED = frozenset({("GET", "/brands/default/senders"),
                                ("GET", "/brands/default/senders/{sender_id}")})
DENY = (
    "createToken", "refreshToken", "createTokenFromChallenge", "*Password*", "*password*", "*Mfa*", "*mfa*",
    "*pat_endpoint*", "*User*", "*user*", "*Account*", "*account*", "*dkim*", "*Dkim*", "patchDomains",
    "convert*", "createSender", "patchSender", "confirmSender", "resendConfirmationEmail", "*Export*",
    "*export*", "createAction", "patchAction", "submitForm", "*Logo*", "*SystemEmails*", "*Webhook*",
    "*Form*", "*form*", "*Workflow*", "*workflow*", "*Action*",
)

CURATED_READS = (
    "listLists", "getList", "listContactsOfList", "getContact", "listSegments", "getSegment",
    "listContactsOfSegment", "listTags", "showTag", "listSenders", "getSender", "listTemplates",
    "getTemplate", "renderTemplate", "listCampaigns", "getCampaign", "renderCampaign", "getCampaignLogs",
    "getCampaignStats", "getListStats", "listCampaignBlueprints",
)
CURATED_WRITES = (
    "createCampaign", "patchCampaign", "scheduleCampaign", "unscheduleCampaign", "sendTestEmail",
    "createContact", "patchContact",
)
CURATED = CURATED_READS + CURATED_WRITES

# Any request to this host is always_approve unless it is listed in READS,
# whatever the tool is called and whatever the YAML says. One pattern per
# curated GET path; a path parameter may not start with a dot or a percent
# sign, so ".." and encoded dot segments do not match.
HOSTS = ("api.cakemail.dev",)
_P = r"[A-Za-z0-9_{][^/]*"
READS = tuple(("GET", pattern) for pattern in (
    r"/lists",
    r"/lists/" + _P,
    r"/lists/" + _P + r"/contacts",
    r"/lists/" + _P + r"/contacts/" + _P,
    r"/lists/" + _P + r"/segments",
    r"/lists/" + _P + r"/segments/" + _P,
    r"/lists/" + _P + r"/segments/" + _P + r"/contacts",
    r"/tags",
    r"/tags/" + _P,
    r"/brands/default/senders",
    r"/brands/default/senders/" + _P,
    r"/templates",
    r"/templates/" + _P,
    r"/templates/" + _P + r"/render",
    r"/campaigns",
    r"/campaigns/" + _P,
    r"/campaigns/" + _P + r"/render",
    r"/logs/campaigns/" + _P,
    r"/reports/campaigns/" + _P,
    r"/reports/lists/" + _P,
    r"/campaign-blueprints",
))

_NAME_MAX = 64


@functools.lru_cache(maxsize=1)
def _spec_ops() -> tuple[tuple[str, str, str], ...]:
    """(operationId, method, readable name) for every operation in the
    packaged spec, in the order ``parse_operations`` returns them."""
    from fabri.tools.openapi import parse_operations

    text = resources.files(__package__).joinpath("specs", SPEC_FILE).read_text(encoding="utf-8")
    ops = parse_operations(json.loads(text), naming="readable", strip_prefixes=("api",))
    return tuple((op.operation_id, op.method, op.name) for op in ops)


@functools.lru_cache(maxsize=32)
def _tool_names(name: str) -> tuple[tuple[str, str, str], ...]:
    """(operationId, method, tool name) for every packaged operation, with
    tool names prefixed by the source name."""
    out = []
    for op_id, method, readable in _spec_ops():
        tool = f"{name}_{readable}"
        if len(tool) > _NAME_MAX:
            raise ConnectorError(f"source name {name!r} is too long: tool {tool} is over {_NAME_MAX} characters")
        out.append((op_id, method, tool))
    return tuple(out)


ENTRY = Connector(
    "cakemail", "Cakemail",
    "Cakemail API: lists, contacts, segments, tags, templates and campaigns; every write needs approval.",
    ("openapi",), "CAKEMAIL_TOKEN", "cakemail", BASE_URL,
    spec_file=SPEC_FILE,
    default_allow=tuple(f"cakemail_{n}" for i, m, n in _spec_ops() if i in CURATED),
    verify_path="/users/self",
)


def expand(name: str, *, token_env: str | None = None, workspace: str | None = None, remote: bool = False,
           url: str | None = None, all_tools: bool = False) -> dict[str, Any]:
    ops = _tool_names(name)
    by_id = {op_id: (method, tool) for op_id, method, tool in ops}
    missing = [op_id for op_id in CURATED if op_id not in by_id]
    if missing:
        raise ConnectorError(f"cakemail: the packaged spec lacks curated operations {', '.join(missing)}")
    # Spec order, so the curated allow list equals ENTRY.default_allow.
    chosen = [op for op in ops if all_tools or op[0] in CURATED]
    source: dict[str, Any] = {
        "kind": "openapi",
        "name": name,
        "spec": f"specs/{name}.json",
        "base_url": BASE_URL,
        "methods": list(TRIM_METHODS),
        "headers": {"Authorization": _credential(ENTRY, token_env, workspace)},
        "deny": list(DENY),
        "approval_mode": "tighten",
        "catalog": "cakemail",
        # For the reviewer's eyes; the host floor does not depend on it.
        "approval": {tool: "always_approve" for _, method, tool in chosen if method not in READ_METHODS},
        "names": {op_id: tool for op_id, _, tool in chosen},
    }
    allow = [f"{name}_*"] if all_tools else [tool for _, _, tool in chosen]
    return {"sources": [source], "allow": allow}


def minimum(tool_name: str, method: str | None) -> str | None:
    """Every call that is not a GET needs approval."""
    if (method or "").upper() in READ_METHODS:
        return None
    return "always_approve"
