"""Parse the machine-memory suffix from an agent's final response."""

from __future__ import annotations

import re


AGENT_MEMORY_MARKER = "<AGENT_MEMORY>"
_AGENT_MEMORY_CLOSE_TAG = "</AGENT_MEMORY>"
_AGENT_MEMORY_OPEN_RE = re.compile(
    r"(?P<comment><!--\s*AGENT(?:_|\s+)?MEMORY\s*-->)"
    r"|(?P<tag><\s*AGENT(?:_|\s+)?MEMORY\s*>)",
    re.IGNORECASE,
)
_AGENT_MEMORY_CLOSE_RE = re.compile(
    r"</\s*AGENT(?:_|\s+)?MEMORY\s*>",
    re.IGNORECASE,
)
_MEMORY_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ ]*):\s?(.*)$")
_MEMORY_INLINE_KEY_RE = re.compile(
    r"(?<!\S)(?:TASK|OUTCOME|INSIGHTS|OPEN LOOPS|CHANGES):[ \t]*"
)
_MEMORY_LIST_RE = re.compile(r"^[-*]\s+(.*)$")


def split_agent_output(text: str) -> tuple[str, dict[str, object] | None]:
    """Return the human-facing prose and parsed machine-memory block."""
    marker = _AGENT_MEMORY_OPEN_RE.search(text)
    if marker is None:
        return text, None

    block_end = len(text)
    suffix = ""
    if marker.lastgroup == "tag":
        close_tag = _AGENT_MEMORY_CLOSE_RE.search(text, marker.end())
        if close_tag is not None:
            block_end = close_tag.start()
            suffix = text[close_tag.end():]

    prose = text[:marker.start()] + suffix
    block = text[marker.end():block_end]
    return prose.rstrip(), _parse_memory_block(block)


def _parse_memory_block(block: str) -> dict[str, object] | None:
    """Parse ``KEY: value`` lines and nested list items below the marker."""
    memory: dict[str, object] = {}
    current_key: str | None = None

    raw_lines: list[str] = []
    for raw in block.splitlines():
        inline_keys = list(_MEMORY_INLINE_KEY_RE.finditer(raw))
        if len(inline_keys) < 2:
            raw_lines.append(raw)
            continue
        raw_lines.extend(
            raw[match.start():next_match.start()]
            for match, next_match in zip(inline_keys, inline_keys[1:])
        )
        raw_lines.append(raw[inline_keys[-1].start():])

    for raw in raw_lines:
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
    """Render a memory mapping inside canonical agent-memory tags."""
    lines = [AGENT_MEMORY_MARKER]
    for key, value in memory.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append(_AGENT_MEMORY_CLOSE_TAG)
    return "\n".join(lines)
