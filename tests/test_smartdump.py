"""The smart capture's state machine, without a board and without media.

What is pinned here is the part that decides PROVENANCE, because that is what a
later reader trusts and what cannot be re-derived once the capture is over:

  * a page is read in its block's OWN cell mode, latched explicitly, never
    inherited -- the failure that makes written native-TLC pages look erased;
  * a page that only decodes at retry level N is stored under `retry-level-N`,
    because the level IS the provenance;
  * a page nothing rescues is stored under the BASE source, not dressed up as
    a rescue that did not happen;
  * levels are tried 1, 0, 2, 3... and the pass STOPS once nothing is pending,
    so a clean block never pays for the ladder.

The fake device serves per-(level, mode) bytes, so "this page decodes at level
3 and nowhere else" is expressible directly.
"""
import collections

import pytest

from titania import geometry as geom
from titania import readretry
from titania import smartdump as sd
from titania import verdict as vd

PAGE_BYTES = 18432
CE, BLOCK = 1, 40


def _erased():
    return b"\xff" * PAGE_BYTES


def _garbage(seed=1):
    # Not all-0xFF and not all-0x00, so `verdict` runs the real checks and calls
    # it FAIL -- which is exactly the "needs a retry level" state.
    b = bytearray(PAGE_BYTES)
    x = seed & 0xFF
    for i in range(PAGE_BYTES):
        x = (x * 73 + 41) & 0xFF
        b[i] = x if x not in (0x00, 0xFF) else 0x5A
    return bytes(b)


class FakeLadder:
    """Stands in for `readretry.Ladder`, recording what it was asked to do."""

    def __init__(self, pages_at):
        #: {level: {page: bytes}} -- what a read at that level returns.
        self.pages_at = pages_at
        self.applied = []          # (ce, level, slc), in order
        self.reads = []            # (ce, block, first, count, slc)
        self.level = None

    def apply(self, ce, level, slc=False, verify=None):
        self.applied.append((ce, level, bool(slc)))
        self.level = level

    def read_span(self, ce, block, first, count, slc=False, lun=0, tries=3):
        self.reads.append((ce, block, first, count, bool(slc)))
        table = self.pages_at.get(self.level, {})
        default = self.pages_at.get("default", {})
        out = bytearray()
        for pg in range(first, first + count):
            out += table.get(pg, default.get(pg, _erased()))
        return bytes(out)


class FakeWriter:
    """Enough of `StoreWriter` to record which source each page landed under."""

    def __init__(self):
        self.pages = {}            # page -> source name
        self.skipped = []
        self._n = 0

    def source(self, name, tier, reason):
        return collections.namedtuple("S", "name tier sid")(name, tier, name)

    def append(self, relpath, data):
        self._n += 1
        return 0, self._n

    def add(self, ce, block, page, slc, src, layout, blob, off, length):
        self.pages[page] = src.name

    def add_erased(self, ce, block, page, slc, src, order_key=""):
        self.pages[page] = src.name

    def add_skipped(self, ce, block, page, slc, src, order_key=""):
        self.skipped.append(page)

    def blob_size(self, name):
        return 0


def _run_block(pages_at, slc=False, top_level=4, correct=False):
    lad = FakeLadder(pages_at)
    w = FakeWriter()
    base = w.source("base", "unknown", "test")
    srcs = {}

    def retry_src(level):
        if level not in srcs:
            srcs[level] = w.source(sd.RETRY_SOURCE % level, "unknown", "t")
        return srcs[level]

    order = readretry.level_order(top_level, slc)
    st = sd._dump_block(lad, w, CE, BLOCK, slc, 0, base, retry_src, order,
                        lambda *a: None, correct)
    return lad, w, st


def test_level_order_is_one_then_zero_then_ascending():
    assert readretry.level_order(4) == [1, 0, 2, 3, 4]
    assert readretry.level_order(1) == [1, 0]
    assert readretry.level_order(0) == [0]


def test_a_clean_block_never_pays_for_the_ladder():
    """Everything resolves at the first level, so no second level is applied."""
    lad, w, st = _run_block({"default": {}})          # all erased == resolved
    assert [lv for _ce, lv, _slc in lad.applied] == [1]
    assert len(lad.reads) == 1
    assert st["erased"] == geom.pages_per_block(False)


def test_cell_mode_is_latched_explicitly_and_per_block():
    """The mode is a decision, not whatever latch the die happened to be in."""
    for slc in (False, True):
        lad, _w, _st = _run_block({"default": {}}, slc=slc)
        assert all(applied_slc is slc for _ce, _lv, applied_slc in lad.applied)
        assert all(read_slc is slc for *_x, read_slc in lad.reads)


def test_a_page_rescued_at_a_later_level_records_that_level():
    bad, good = _garbage(3), _erased()
    lad, w, st = _run_block({1: {7: bad}, 0: {7: bad}, 2: {7: bad},
                             3: {7: good}, "default": {}})
    assert w.pages[7] == sd.RETRY_SOURCE % 3
    assert st["rescued"] == 1 and st["rescued_level_3"] == 1
    # and every other page stayed on the base source
    assert {v for k, v in w.pages.items() if k != 7} == {"base"}


def test_a_page_nothing_rescues_keeps_the_base_source(caplog):
    """A failed read must not be dressed as a rescue.  Its bytes are still kept."""
    bad = _garbage(5)
    lad, w, st = _run_block({lv: {7: bad} for lv in (1, 0, 2, 3, 4)}
                            | {"default": {}})
    assert w.pages[7] == "base"
    assert st["unrescued"] == 1 and st["rescued"] == 0
    assert st["fail"] == 1


def test_only_the_still_failing_pages_are_carried_forward():
    """Two pages fail at level 1; one recovers at 0, the other at 2."""
    bad = _garbage(9)
    lad, w, st = _run_block({1: {5: bad, 6: bad},
                             0: {6: bad},          # page 5 resolves here
                             2: {},                # page 6 resolves here
                             "default": {}})
    assert w.pages[5] == sd.RETRY_SOURCE % 0
    assert w.pages[6] == sd.RETRY_SOURCE % 2
    assert st["rescued"] == 2
    # Level 3 and 4 were never needed, so they were never applied.
    assert [lv for _ce, lv, _s in lad.applied] == [1, 0, 2]


def test_retry_is_disabled_by_top_level_zero():
    bad = _garbage(11)
    lad, w, st = _run_block({0: {7: bad}, "default": {}}, top_level=0)
    assert [lv for _ce, lv, _s in lad.applied] == [0]
    assert st["unrescued"] == 1


@pytest.mark.parametrize("verdict_name", list(sd.DONE))
def test_done_and_retryable_do_not_overlap(verdict_name):
    assert verdict_name not in sd.RETRYABLE


def test_aux_rejected_is_not_retried():
    """Its integrity checks PASSED; moving the references cannot change it."""
    assert vd.AUX_REJECTED not in sd.RETRYABLE
    assert vd.AUX_REJECTED not in sd.DONE
