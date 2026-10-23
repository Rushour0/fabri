"""Bundled agency scaffold templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NotRequired, TypedDict, cast

from . import blank, bug_crew, changelog, jev_router


class AgencyTemplate(TypedDict):
    FILES: Mapping[str, str]
    README: str
    # Config file the post-scaffold hint tells you to run; agent.openai.yaml when absent.
    ENTRY: NotRequired[str]


BLANK_TEMPLATE = cast(AgencyTemplate, blank.TEMPLATE)
BUG_CREW_TEMPLATE = cast(AgencyTemplate, bug_crew.TEMPLATE)
CHANGELOG_TEMPLATE = cast(AgencyTemplate, changelog.TEMPLATE)
JEV_ROUTER_TEMPLATE = cast(AgencyTemplate, jev_router.TEMPLATE)

TEMPLATES: dict[str, AgencyTemplate] = {
    "bug-crew": BUG_CREW_TEMPLATE,
    "changelog": CHANGELOG_TEMPLATE,
    "blank": BLANK_TEMPLATE,
    "jev-router": JEV_ROUTER_TEMPLATE,
}

__all__ = [
    "AgencyTemplate",
    "BLANK_TEMPLATE",
    "BUG_CREW_TEMPLATE",
    "CHANGELOG_TEMPLATE",
    "JEV_ROUTER_TEMPLATE",
    "TEMPLATES",
]
