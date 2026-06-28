"""B7 -- incremental file-sync hooks.

The service materializes a per-run workspace (its ``FABRI_HOME``) and may need
to seed it from, and flush results back to, a host's storage backend (S3 /
MinIO / NFS / a local volume). That ferrying is *host policy*, not framework
concern, so the service only defines the seam -- mirroring
:class:`fabri.sandbox.Sandbox`'s ``sync_in`` / ``sync_out`` so a host can reuse
the same backend object for both.

The default :class:`NoOpSyncHook` ferries nothing: the run reads and writes the
local ``FABRI_HOME`` directly, which is correct for single-host deployments and
keeps the disabled-feature path byte-identical to launching ``fabri run`` by
hand.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class FileSyncHook(ABC):
    """Pull a run's workspace in before launch; push dirty paths back after.

    Intentionally the same shape as :meth:`fabri.sandbox.Sandbox.sync_in` /
    ``sync_out`` so a host that already wired a storage backend for the sandbox
    can hand the same object to the service.
    """

    @abstractmethod
    def sync_in(self, run_id: str, workspace: Path) -> None:
        """Seed ``workspace`` with the run's prior state before the agent starts."""

    @abstractmethod
    def sync_out(self, run_id: str, workspace: Path, dirty_paths: list[str]) -> None:
        """Flush the listed paths (relative to ``workspace``) back to storage."""


class NoOpSyncHook(FileSyncHook):
    """Default: ferry nothing. The run uses its local ``FABRI_HOME`` directly."""

    def sync_in(self, run_id: str, workspace: Path) -> None:
        return None

    def sync_out(self, run_id: str, workspace: Path, dirty_paths: list[str]) -> None:
        return None
