"""Layered setups: agents, skills, context, data, tools and plays resolved
from an ordered list of directories the caller supplies.

    from fabri.setups import Setups

    setups = Setups(["base", "packs/sales", "orgs/acme", "orgs/acme/projects/q3"],
                    policy=ToolPolicy.from_file("allow.yaml"))
    resolved = setups.resolve()
    config, registry = setups.agent_config("chat", {"provider": "anthropic", "model": "claude-sonnet-4-6"})

See docs/dynamic-setups.md.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from fabri.setups.agent import build_agent_config
from fabri.setups.importer import ImportReport, import_skills
from fabri.setups.layers import DEFAULT_BUDGETS, Layer, LayerLike, SetupError, as_layers
from fabri.setups.lint import LintFinding, has_errors, lint_setup
from fabri.setups.resolver import ResolvedSetup, clear_cache, resolve_setup
from fabri.setups.store import (
    KINDS,
    PutResult,
    Validator,
    delete_item,
    item_history,
    item_revision,
    put_item,
    read_item,
    set_disabled,
    validate_item,
)
from fabri.setups.skill_tool import LOAD_SKILL, register_load_skill
from fabri.setups.toolset import ResolvedTool, ResolvedToolSet, diff_snapshots, resolve_tools
from fabri.tools.credential_store import CredentialStore
from fabri.tools.policy import ToolPolicy


class Setups:
    """A layer stack plus the caller's policy, with every operation in one
    place. Layer names given to put/delete must be in the stack."""

    def __init__(
        self,
        layers: Iterable[LayerLike],
        *,
        policy: ToolPolicy | None = None,
        validators: Mapping[str, Validator] | None = None,
        transport: httpx.BaseTransport | None = None,
        store: CredentialStore | None = None,
    ):
        self.layers = as_layers(layers)
        self.policy = policy or ToolPolicy.permissive()
        self.validators = dict(validators or {})
        self.transport = transport
        self.store = store

    def layer(self, name: str) -> Layer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise SetupError(f"no layer named {name!r}")

    def resolve(self) -> ResolvedSetup:
        return resolve_setup(self.layers)

    def tools(self, *, dry_run: bool = False) -> ResolvedToolSet:
        return resolve_tools(self.resolve(), policy=self.policy, transport=self.transport,
                             store=self.store, dry_run=dry_run)

    def agent_config(self, agent_type: str, base_llm_cfg: Mapping[str, Any] | None = None, **kwargs: Any):
        return build_agent_config(self.resolve(), agent_type, base_llm_cfg, tool_set=self.tools(), **kwargs)

    def put(self, layer: str, kind: str, name: str, content: Any, *, actor: str | None = None) -> PutResult:
        return put_item(self.layer(layer).path, kind, name, content, policy=self.policy,
                        validators=self.validators, actor=actor)

    def delete(self, layer: str, kind: str, name: str, *, actor: str | None = None) -> bool:
        return delete_item(self.layer(layer).path, kind, name, actor=actor)

    def history(self, layer: str, kind: str, name: str) -> list[dict[str, Any]]:
        return item_history(self.layer(layer).path, kind, name)

    def disable(self, layer: str, kind: str, name: str, disabled: bool = True) -> Path:
        return set_disabled(self.layer(layer).path, kind, name, disabled)

    def import_skills(self, source: str | Path, layer: str, *, fmt: str = "skill-md",
                      dry_run: bool = False) -> ImportReport:
        return import_skills(source, self.layer(layer).path, fmt=fmt, dry_run=dry_run, policy=self.policy)

    def lint(self) -> list[LintFinding]:
        tools = self.tools()
        return lint_setup(self.resolve(), tool_names=[n for t in tools.tools.values() for n in [t.name, *t.aliases]],
                          policy=self.policy, tool_set=tools)


__all__ = [
    "DEFAULT_BUDGETS", "KINDS", "LOAD_SKILL", "ImportReport", "Layer", "LintFinding", "PutResult", "ResolvedSetup",
    "ResolvedTool", "ResolvedToolSet", "SetupError", "Setups", "build_agent_config", "clear_cache",
    "delete_item", "diff_snapshots", "has_errors", "import_skills", "item_history", "item_revision", "lint_setup", "put_item", "read_item",
    "register_load_skill", "resolve_setup", "resolve_tools", "set_disabled", "validate_item",
]
