"""Bundled agency scaffold templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict, cast

from . import blank, bug_crew, changelog


class AgencyTemplate(TypedDict):
    FILES: Mapping[str, str]
    README: str


BLANK_TEMPLATE = cast(AgencyTemplate, blank.TEMPLATE)
BUG_CREW_TEMPLATE = cast(AgencyTemplate, bug_crew.TEMPLATE)
CHANGELOG_TEMPLATE = cast(AgencyTemplate, changelog.TEMPLATE)

TEMPLATES: dict[str, AgencyTemplate] = {
    "bug-crew": BUG_CREW_TEMPLATE,
    "changelog": CHANGELOG_TEMPLATE,
    "blank": BLANK_TEMPLATE,
}

__all__ = [
    "AgencyTemplate",
    "BLANK_TEMPLATE",
    "BUG_CREW_TEMPLATE",
    "CHANGELOG_TEMPLATE",
    "TEMPLATES",
]
