"""`python -m nandgui` -- the package entry point named in the contract.

Run from the directory containing the package, or use main.py, which puts
that directory on sys.path for you.
"""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
