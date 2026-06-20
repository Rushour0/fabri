"""Thin shim kept for `python cli.py ...` from a source checkout. The real
implementation lives in the package (`fabri.cli`) so that the
`fabri` console script installed by pip points at the same code."""
from fabri.cli import main

if __name__ == "__main__":
    main()
