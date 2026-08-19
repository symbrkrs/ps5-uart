"""Shared fixtures.

These tests must run for a contributor with NO BOARD and NO DUMP.  That is the
whole point of them: the `--selftest` blocks inside each module are integration
checks against real media and they cannot run for anyone who does not have this
board, so nothing about the arithmetic was checkable by an outside contributor
until this directory existed.

The rule, and please keep it: **a test in here may not open a dump, a store or a
serial port.**  If a check needs real media it belongs in the owning module's
`_selftest()`, next to the evidence that justifies it.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Work from a plain checkout as well as from `pip install -e .`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_report_header(config):
    try:
        import numpy
        nv = numpy.__version__
    except Exception as exc:                                # pragma: no cover
        nv = "MISSING (%s)" % exc
    return ["titania: repo root %s" % ROOT, "numpy: %s" % nv]


@pytest.fixture(scope="session")
def geom():
    from titania import geometry
    return geometry


@pytest.fixture(scope="session")
def ftl():
    return pytest.importorskip("titania.ftl")


@pytest.fixture(scope="session")
def auxmod():
    return pytest.importorskip("titania.auxdata")


@pytest.fixture(scope="session")
def verdict():
    return pytest.importorskip("titania.verdict")


@pytest.fixture(scope="session")
def extract():
    return pytest.importorskip("titania.extract")


@pytest.fixture(scope="session")
def store():
    return pytest.importorskip("titania.store")


@pytest.fixture(scope="session")
def tn():
    """The codec.  Needs numpy and the eight measured data tables.

    The chain moved into the package, so this imports `titania.titania_nand`;
    the bare top-level name it used to probe no longer exists and would skip
    this whole file silently rather than fail.
    """
    return pytest.importorskip(
        "titania.titania_nand",
        reason="codec chain unavailable (needs numpy and the measured tables "
               "resolvable through titania.nd_tables)")
