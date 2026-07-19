"""Parse the machine-memory suffix from an agent's final response."""

from __future__ import annotations

import re


AGENT_MEMORY_MARKER = "<!-- AGENT_MEMORY -->"
_MEMORY_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ ]*):\s?(.*)$")
_MEMORY_LIST_RE = re.compile(r"^[-*]\s+(.*)$")


def split_agent_output(text: str) -> tuple[str, dict[str, object] | None]:
    """Return the human-facing prose and parsed machine-memory block."""
    if AGENT_MEMORY_MARKER not in text:
        return text, None

    prose, _, block = text.partition(AGENT_MEMORY_MARKER)
    return prose.rstrip(), _parse_memory_block(block)


def _parse_memory_block(block: str) -> dict[str, object] | None:
    """Parse ``KEY: value`` lines and nested list items below the marker."""
    memory: dict[str, object] = {}
    current_key: str | None = None

    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue

        list_match = _MEMORY_LIST_RE.match(line)
        if list_match and current_key is not None:
            existing = memory.get(current_key)
            if not isinstance(existing, list):
                existing = [] if existing in (None, "") else [existing]
                memory[current_key] = existing
            existing.append(list_match.group(1).strip())
            continue

        key_match = _MEMORY_KEY_RE.match(line)
        if key_match:
            current_key = key_match.group(1).strip()
            memory[current_key] = key_match.group(2).strip()
            continue

        if current_key is not None and isinstance(memory.get(current_key), str):
            sep = " " if memory[current_key] else ""
            memory[current_key] = f"{memory[current_key]}{sep}{line}"

    return memory or None


def format_agent_memory(memory: dict[str, object]) -> str:
    """Render a memory mapping as a marker-fenced machine-readable block."""
    lines = [AGENT_MEMORY_MARKER]
    for key, value in memory.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
