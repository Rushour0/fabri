"""Thin shim kept for `python cli.py ...` from a source checkout. The real
implementation lives in the package (`agent_memory.cli`) so that the
`agent-memory` console script installed by pip points at the same code."""
from agent_memory.cli import main

if __name__ == "__main__":
    main()
