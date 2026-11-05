"""B7 -- runtime YAML-override binding for the embeddable service.

One immutable *template* config plus a small per-run *overrides* dict yields a
fresh on-disk run config the agent subprocess loads with the ordinary
``fabri run --config``. This is the multi-tenancy seam: a host keeps a single
reviewed template and only varies the volatile bits per run -- the memory
collection / qdrant url it should write to, the model to use, a cost ceiling.

The merge reuses :func:`fabri.config._deep_merge` (the same recursion +
scalar-over-mapping guard the loader uses) so an override deep-merges exactly
the way a user's ``agent.yaml`` would merge onto the defaults. We merge onto the
template's *raw* YAML (not the normalized/defaulted config): the written run
yaml is then loaded by the subprocess through the normal :func:`load_config`
path, so default behaviour is byte-identical to running the template directly
when ``overrides`` is empty.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from fabri.config import ConfigError, _deep_merge


def _load_template(template_path: str | Path | None) -> dict:
    """Read the template config's raw mapping. ``None`` -> ``{}`` (the run
    inherits the framework defaults, same as ``fabri run`` with no ``--config``)."""
    if template_path is None:
        return {}
    try:
        with open(template_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ConfigError(f"template config not found: {template_path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {template_path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(
            f"top-level of {template_path} must be a mapping "
            f"(got {type(data).__name__})."
        )
    return data


def merge_overrides(base: dict, overrides: dict | None) -> dict:
    """Deep-merge a per-run ``overrides`` mapping onto a ``base`` config mapping.

    Thin, side-effect-free wrapper over :func:`fabri.config._deep_merge` so the
    service merges identically to the config loader. ``overrides=None`` returns a
    shallow copy of ``base`` unchanged.
    """
    if not overrides:
        return dict(base)
    if not isinstance(overrides, dict):
        raise ConfigError(
            f"overrides must be a mapping (got {type(overrides).__name__})."
        )
    return _deep_merge(base, overrides)


def bind_run_config(
    template_path: str | Path | None,
    overrides: dict | None,
    out_path: str | Path,
) -> Path:
    """Write a per-run config to ``out_path`` from ``template_path`` + ``overrides``.

    Returns the path written. The file is plain YAML the agent subprocess loads
    with ``fabri run --config <out_path>``; it carries only the merged user-level
    keys (the subprocess re-applies framework defaults on load).
    """
    merged = merge_overrides(_load_template(template_path), overrides)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(merged, sort_keys=True, default_flow_style=False))
    return out


# Config keys a request's "overrides" may not set. Each one either starts a
# process (MCP server commands, sub-agent configs, the repair verifier), loads
# tools from somewhere new (manifest dirs, tool sources and their policy), moves
# the sandbox, or lifts a narrowing the template applied (tools.enabled). Only
# the reviewed template or catalog config may set them. Paths are dotted.
_REFUSED_OVERRIDE_KEYS = (
    "tools.sources",
    "tools.policy",
    "tools.mcp_servers",
    "tools.manifest_dir",
    "tools.sandbox_root",
    "tools.agents",
    "tools.enabled",
    "agent.repair.verify_command",
    "agent.repair.verify_cwd",
)


# Key names a request's overrides may not set at any depth: under llm, under a
# role block such as llm.planner or llm.roles.main, or anywhere else. They pick
# where a provider key is sent (base_url, api_base), which environment variable
# is read and handed to the run as that key (api_key_env), or an address, file
# or command the run would reach. Matching is by the last segment of the name.
# The credential names (secret_ref, credential_grants, header_source and any
# name ending in _ref) are refused too: a request may never pick which stored
# connection or credential a run uses, or widen what its policy grants.
_REFUSED_OVERRIDE_NAMES = frozenset({
    "base_url", "api_base", "api_key", "api_key_env",
    "secret_ref", "credential_grants", "header_source",
})
_REFUSED_OVERRIDE_SUFFIXES = ("_url", "_endpoint", "_path", "_dir", "_command", "_ref")

# Whole sections a request may not touch at all, except the keys listed in
# _ALLOWED_SECTION_KEYS. memory points the run at a vector store or a database
# file, and observability at an export endpoint, so both stay with the template.
_REFUSED_OVERRIDE_SECTIONS = ("memory", "observability")

# The one key inside a refused section that a request may set: which memory
# collection the run uses. It must be a short plain name.
_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _refused_name(name) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    return lowered in _REFUSED_OVERRIDE_NAMES or lowered.endswith(_REFUSED_OVERRIDE_SUFFIXES)


def _refused_names_within(node, path: str, found: list[str]) -> None:
    """Add the dotted path of every refused key name inside ``node``. Lists are
    walked too, so a key inside a list item is still seen."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if _refused_name(key):
                found.append(here)
            _refused_names_within(value, here, found)
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _refused_names_within(item, f"{path}[{index}]", found)


def refused_override_keys(overrides) -> list[str]:
    """The keys in `overrides` that a request is not allowed to set.

    Any presence counts, including null or an empty value, because an empty
    tools.enabled or a null manifest_dir still changes what the run gets. A
    non-mapping section is left to the config merge, which rejects it. Three
    rules apply: the exact dotted paths above, the refused key names at any
    depth, and any key under the memory or observability sections except a valid
    memory.collection name."""
    if not isinstance(overrides, Mapping):
        return []
    found = []
    for dotted in _REFUSED_OVERRIDE_KEYS:
        node = overrides
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, Mapping) else None
        if isinstance(node, Mapping) and parts[-1] in node:
            found.append(dotted)
    _refused_names_within(overrides, "", found)
    for section in _REFUSED_OVERRIDE_SECTIONS:
        if section not in overrides:
            continue
        value = overrides[section]
        if isinstance(value, Mapping) and value:
            for key, item in value.items():
                if section == "memory" and key == "collection" and (
                    isinstance(item, str) and _COLLECTION_NAME.fullmatch(item)
                ):
                    continue
                found.append(f"{section}.{key}")
        else:
            found.append(section)
    # A key can match more than one rule; name it once, in first-seen order.
    return list(dict.fromkeys(found))
