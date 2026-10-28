"""Rank named texts against a task.

Skills and tools share this so a preview shows the same order the agent
gets. With the embeddings extra installed the score is cosine similarity
(vectors are normalized, so a dot product); without it the score is the
number of words the task and the text share.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def rank(task: str, texts: Mapping[str, str]) -> tuple[list[tuple[str, float]], str]:
    """``([(name, score), ...] best first, method)`` where method is
    ``embeddings`` or ``words``. Ties keep the input order."""
    from fabri.memory.embeddings import embed, embeddings_available

    names = list(texts)
    if embeddings_available():
        task_vec = embed(task or " ")
        scores = {n: float(sum(a * b for a, b in zip(task_vec, embed(texts[n] or n)))) for n in names}
        method = "embeddings"
    else:
        task_words = words(task)
        scores = {n: float(len(task_words & words(texts[n]))) for n in names}
        method = "words"
    ordered = sorted(names, key=lambda n: -scores[n])
    return [(n, round(scores[n], 4)) for n in ordered], method
