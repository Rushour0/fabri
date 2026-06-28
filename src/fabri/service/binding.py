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
