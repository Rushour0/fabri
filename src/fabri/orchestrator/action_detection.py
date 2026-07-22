"""Shadow-only ActionMemory proposal detection for manager orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from fabri.config import load_config
from fabri.orchestrator.retrieval import propose_actions


ConfigLoader = Callable[[str], dict]


def build_current_state(
    agents_entries: Iterable[Mapping[str, object]],
    task: str,
    *,
    company: str,
    agency: str,
    config_loader: ConfigLoader = load_config,
) -> dict:
    """Build current state from a manager's child agent configs.

    ``roles_config`` is keyed by the child node name, rather than an LLM
    backend slot. An unreadable or malformed child config is omitted so a
    single stale child entry cannot prevent shadow proposal detection.
    """
    roles_config: dict[str, dict[str, int]] = {}
    for entry in agents_entries:
        name = entry.get("name")
        config_path = entry.get("config")
        if not isinstance(name, str) or not name or not isinstance(config_path, str) or not config_path:
            continue

        try:
            cfg = config_loader(config_path)
        except Exception:  # noqa: BLE001 -- a child config failure is intentionally skipped
            continue

        if not isinstance(cfg, dict):
            continue
        llm = cfg.get("llm", {})
        if not isinstance(llm, dict):
            continue
        max_tokens = llm.get("max_tokens")
        if isinstance(max_tokens, int):
            roles_config[name] = {"max_tokens": max_tokens}

    return {
        "company": company,
        "agency": agency,
        "roles_config": roles_config,
        "task": task,
    }


def detect_proposed_actions(
    store: object,
    agents_entries: Iterable[Mapping[str, object]],
    task: str,
    *,
    company: str,
    agency: str,
    config_loader: ConfigLoader = load_config,
    top_n: int = 1,
) -> list[dict]:
    """Return applicable ActionMemory proposals without applying them."""
    current_state = build_current_state(
        agents_entries,
        task,
        company=company,
        agency=agency,
        config_loader=config_loader,
    )
    return propose_actions(store, current_state, top_n=top_n)


def derive_scope_from_collection(collection: str | None) -> tuple[str | None, str | None]:
    """Best-effort ``(namespace, node_id)`` parsing of ``memory.collection``.

    Company and agency are not persisted in compiled YAML, so this helper only
    recovers the namespace and final node identifier from ``{namespace}_{node}``.
    """
    if not collection:
        return None, None
    namespace, separator, node_id = collection.rpartition("_")
    if not separator or not namespace or not node_id:
        return None, None
    return namespace, node_id
