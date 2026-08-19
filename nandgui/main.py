"""Launcher for the Titania NAND viewer.

    py -3 path/to/nandgui/main.py

This file exists so the viewer can be started by path, without the caller
having to know that ``nandgui`` is a package that must be imported from its
parent directory.  Running ``main.py`` puts that parent on sys.path and
then hands straight over to ``nandgui.app.main``.

Importing this module must never open a window -- everything below the
``__main__`` guard is import-safe, which is what makes

    py -3 -c "import sys; sys.path.insert(0, 'path/to/nandgui'); import main"

a usable check that the whole tree still imports.

Options are ``nandgui.app.main``'s:

    --summary PATH      block summary npz     ($TITANIA_NAND_DIR/summary.npz)
    --raw PATH          all.store             (page_bytes 18432)
    --decoded PATH      decoded.store         (page_bytes 16416)
    --include-suspect   put the "decoded-mcrc-failed" pages back in the default
                        read path (the count is per capture; the Data menu
                        reads it out of summary.npz at runtime)
    --workers N         worker threads, each with its own pair of open stores
                        (default: $NANDGUI_WORKERS, else min(8, cpu_count)).
                        An explicit --workers beats the environment variable.
                        It is not a core count and more is not faster: measured
                        on this machine, ECC throughput is flat from 1 to 16
                        threads (all within the run-to-run spread), because the
                        decode is half behind one Rust subprocess and half
                        GIL-bound. What the pool buys is concurrency: with two
                        background ECC scans running, an interactive stripe
                        probe is answered in 131 ms at 4 workers against 497 ms
                        at 1. Do not set it to 2 -- that is the worst case,
                        1024 ms, because the worker count then equals the
                        number of background jobs and none is left spare.
    --self-test N       run N frames and exit, for verifying a build

``python -m nandgui`` and ``python -m nandgui.app`` do the same thing from the
parent directory.
"""

from __future__ import annotations

import os
import sys

# The package directory is this file's directory; the package is imported from
# its parent.  Both are inserted ahead of everything else so a stray nandgui/
# elsewhere on the path cannot win.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def main(argv: list[str] | None = None) -> int:
    # Imported inside main() so that `import main` costs nothing but this file:
    # nandgui.app pulls in imgui_bundle, OpenGL and (lazily) the WSL-hosted
    # titania package, none of which should run at import time in a smoke test.
    from nandgui.app import main as app_main

    return app_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
