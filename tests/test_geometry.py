"""titania.geometry -- pure addressing arithmetic.

Nothing here needs media, which is exactly why it is worth testing: every one of
these functions sits under something that DOES need media, so an error here
shows up as "that block is empty" three layers away.
"""
import random

import pytest


def test_row_round_trip(geom):
    rnd = random.Random(20260806)
    for _ in range(2000):
        block = rnd.randrange(geom.BLOCKS_PER_LUN)
        page = rnd.randrange(geom.PAGES_PER_BLOCK_TLC)
        lun = rnd.randrange(geom.LUNS_PER_CE)
        row = geom.compose_row(block, page, lun)
        assert geom.split_row(row) == (block, page, lun)


def test_row_fields_do_not_overlap(geom):
    """A page number must never bleed into the block field.

    PAGES_PER_BLOCK_TLC is 1104 and the page field is 11 bits (2048), so the
    fields are disjoint -- but only because 1104 < 2048.  If someone ever raises
    the page count this assert is the thing that notices.
    """
    assert geom.PAGES_PER_BLOCK_TLC <= 1 << geom.BLOCK_SHIFT
    assert geom.PAGES_PER_BLOCK_SLC <= 1 << geom.BLOCK_SHIFT
    assert geom.BLOCKS_PER_LUN <= 1 << (geom.LUN_SHIFT - geom.BLOCK_SHIFT)
    # highest legal page must not set a block bit
    row = geom.compose_row(0, geom.PAGES_PER_BLOCK_TLC - 1, 0)
    assert geom.split_row(row)[0] == 0


def test_pbid_round_trip(geom):
    for block in range(0, 400, 7):
        for ce in range(geom.CE_COUNT):
            p = geom.pbid(block, ce)
            assert geom.split_pbid(p) == (block, ce)
            # the identity the docstring states, spelled out
            assert ce == p % geom.CE_COUNT
            assert block == p // geom.CE_COUNT


def test_weru_blocks_spans_every_ce_including_dead(geom):
    """A WERU spans every CE, dead ones included.

    `geometry` must not quietly drop CE0: capacity arithmetic that skips it
    silently is how a 48-block WERU becomes a 44-block one and every WMA after
    the first lands on the wrong page.
    """
    blocks = geom.weru_blocks([10, 11, 26, 27])
    assert len(blocks) == geom.BLOCKS_PER_WERU == 48
    assert len(set(blocks)) == 48
    for ce in range(geom.CE_COUNT):
        assert sum(1 for _b, c in blocks if c == ce) == 4
    for dead in geom.CE_DEAD:
        assert any(c == dead for _b, c in blocks)


def test_cell_mode_brackets(geom):
    """Natively-SLC word lines bracket a native block; the middle is TLC."""
    last = geom.PAGES_PER_BLOCK_TLC - 1
    for p in (0, 1, 2, 3):
        assert geom.cell_mode(p) == 2
    for p in (last, last - 1, last - 2, last - 3):
        assert geom.cell_mode(p) == 2
    for p in (4, 5, 500, last - 4):
        assert geom.cell_mode(p) == 0
    # exactly 8 of them, four at each end -- not "about 8"
    assert sum(1 for p in range(geom.PAGES_PER_BLOCK_TLC)
               if geom.cell_mode(p) == 2) == 8


def test_pages_per_block(geom):
    assert geom.pages_per_block(True) == geom.PAGES_PER_BLOCK_SLC == 368
    assert geom.pages_per_block(False) == geom.PAGES_PER_BLOCK_TLC == 1104
    # pseudo-SLC is exactly one third of native TLC: 3 pages per word line
    assert geom.PAGES_PER_BLOCK_TLC == 3 * geom.PAGES_PER_BLOCK_SLC


@pytest.mark.parametrize("name,expect", [
    ("AUS_PER_TLC_WERU", 48 * 1104 * 4),
    ("AUS_PER_SLC_WERU", 12 * 368 * 4),
])
def test_capacity_constants_are_their_own_arithmetic(geom, name, expect):
    """The two WERU capacities must equal the product they document.

    They are written as literals so they can be grepped; this is the check that
    keeps the literal and the comment from drifting apart.
    """
    assert getattr(geom, name) == expect


def test_au_layout_constants(geom):
    assert geom.AU_DATA == 4096
    assert geom.AU_AUX == 8
    # 4608 stored bytes is two 2304-byte media codewords
    assert geom.AU_STORED == 2 * 2304
    assert geom.AU_STORED > geom.AU_DATA + geom.AU_AUX


def test_geometry_imports_nothing_heavy():
    """`geometry` promises it is safe to import from anywhere."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);"
         "import titania.geometry;"
         "print(int('numpy' in sys.modules), int('serial' in sys.modules))"
         % str(root)],
        capture_output=True, text=True, cwd=str(root))
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["0", "0"], out.stdout
