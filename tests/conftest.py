import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The whole suite talks to a single Qdrant service (a container in CI, a local
# `docker compose up` otherwise). Tests here construct their own store against
# this URL, so the fixtures below key off the same default.
QDRANT_URL = "http://localhost:6333"


@pytest.fixture(scope="session")
def qdrant_client():
    """A shared Qdrant client for the session, or ``None`` if Qdrant is down.

    Connecting is attempted exactly once per session (with a short timeout) so
    that when Qdrant is unreachable the whole suite fails fast instead of
    paying a per-test connection timeout. Tests that need Qdrant still build
    their own stores; this client exists only for the isolation sweep below and
    for the report header.
    """
    from qdrant_client import QdrantClient

    try:
        client = QdrantClient(url=QDRANT_URL, timeout=2)
        client.get_collections()
        return client
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _isolate_qdrant_collections(qdrant_client):
    """Guarantee per-test isolation of the shared Qdrant service.

    Several test modules share a module-level collection name and clean up by
    hand at the end of each test. A mid-test assertion skips that cleanup, so
    residue leaks into the next test and surfaces as flaky, order-dependent
    ``assert 2 == 1`` accumulating-count failures — the shape of the July 2026
    CI reds. Rather than rework 15 test files, snapshot the set of collections
    before each test and drop any created during it, so every test starts from
    a clean slate regardless of run order or where a prior test bailed out.

    Every test that builds a store inside its body creates a fresh collection,
    so this correctly reclaims it; tests that reuse a module-level collection
    name get it recreated clean on their next store construction.

    Assumes serial execution (the CI runs ``pytest tests/ -q`` with no xdist).
    Under ``-n`` the before/after snapshot would race across workers sharing the
    one Qdrant service and could delete a sibling worker's live collection, so
    key collection names by worker id before enabling parallel runs.
    """
    client = qdrant_client
    if client is None:
        # Qdrant not running: nothing to isolate. Non-Qdrant tests run
        # unaffected; tests that need Qdrant surface their own connection error.
        yield
        return

    try:
        before = {c.name for c in client.get_collections().collections}
    except Exception as e:
        # Snapshot failed (e.g. Qdrant hiccup mid-run). Isolation is disabled
        # for this test -- warn loudly rather than silently, since a silent
        # miss lets residue leak into the next test and re-introduces exactly
        # the order-dependent flakiness this fixture exists to kill.
        warnings.warn(f"qdrant isolation disabled for this test: snapshot failed: {e}", stacklevel=2)
        yield
        return

    yield

    try:
        after = {c.name for c in client.get_collections().collections}
        for name in after - before:
            client.delete_collection(name)
    except Exception as e:
        # Best-effort cleanup; never let teardown mask a test result, but make
        # a failure to reclaim collections visible so leaks don't accumulate
        # unnoticed.
        warnings.warn(f"qdrant isolation sweep failed to drop collections: {e}", stacklevel=2)


def pytest_report_header():
    """Surface Qdrant availability up front so a missing service reads as one
    clear line instead of dozens of opaque connection tracebacks."""
    from qdrant_client import QdrantClient

    try:
        QdrantClient(url=QDRANT_URL, timeout=2).get_collections()
        return f"qdrant: reachable at {QDRANT_URL}"
    except Exception:
        return (
            f"qdrant: UNREACHABLE at {QDRANT_URL} — memory/store tests will fail. "
            f"Start it with `docker compose up -d`."
        )
