"""Which surfaces this server is currently speaking.

An adapter registers only when its credentials are configured -- the same
posture as ``slack_oauth.build_install_redirect`` returning None when
unconfigured -- so an unconfigured surface is a missing route rather than a
half-working endpoint.
"""
from __future__ import annotations

from fabri.service.surfaces.base import SurfaceAdapter


class SurfaceRegistry:
    """Adapters by name, and the webhook paths they answer on."""

    def __init__(self) -> None:
        self._by_name: dict[str, SurfaceAdapter] = {}

    def register(self, adapter: SurfaceAdapter) -> SurfaceAdapter:
        if not adapter.name:
            raise ValueError("a surface adapter needs a name")
        self._by_name[adapter.name] = adapter
        return adapter

    def get(self, name: str | None) -> SurfaceAdapter | None:
        return self._by_name.get(name) if name else None

    def routes(self) -> dict[str, SurfaceAdapter]:
        """``{webhook_path: adapter}``, including each path's trailing-slash twin.

        The HTTP layer matches both spellings today; keeping that here means the
        server stays a loop over this dict.
        """
        routes: dict[str, SurfaceAdapter] = {}
        for adapter in self._by_name.values():
            path = adapter.webhook_path
            if not path:
                continue
            routes[path] = adapter
            routes[path.rstrip("/") + "/"] = adapter
        return routes

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)
