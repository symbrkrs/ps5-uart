"""The background ECC sweep: which blocks it picks, and how fast it picks them.

Three properties, each of which was wrong at some point and each of which is
visible to whoever is watching the map fill:

  * it goes FORWARD in address order, not outward in both directions at once;
  * it crosses CE boundaries, so one press covers the drive;
  * it does not decode a block the summary already proves has no data -- on the
    reference drive that is 46386 of 48000 blocks.

And one that is not about correctness but about the thing being usable: the
candidate search runs on the UI thread every frame, so it has to stay far
inside a frame even when the map is nearly full.  It did not: as a Python loop
over the map it cost 79 ms a frame at 99.9% measured.
"""
import time

import pytest

np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def app_mod():
    return pytest.importorskip("nandgui.app")


class FakeSummary:
    """The handful of members the sweep and the hover line read off a Summary."""

    def __init__(self, n_ce, n_block, programmed_at=()):
        self._nce, self._nb = n_ce, n_block
        n = n_ce * n_block
        self.programmed = np.zeros(n, np.int64)
        self.erased = np.full(n, 100, np.int64)
        self.slc = np.ones(n, np.uint8)
        for i in programmed_at:
            self.programmed[i] = 12
            self.erased[i] = 0

    def index(self, ce, block):
        return int(ce) * self._nb + int(block)

    def valid(self, ce, block):
        return 0 <= int(ce) < self._nce and 0 <= int(block) < self._nb

    def is_slc(self, ce, block):
        return bool(self.slc[self.index(ce, block)])


class FakeApp:
    summary = None
    selected = (0, 0)


@pytest.fixture()
def clean_map(app_mod):
    """A fresh, empty ECC map, restored afterwards."""
    saved = dict(app_mod._ui)
    app_mod._ui["ecc"] = np.zeros(app_mod.N_CE * app_mod.N_BLOCK, np.uint8)
    app_mod._ui["ecc_blocks"] = {}
    yield app_mod._ui["ecc"]
    app_mod._ui.clear()
    app_mod._ui.update(saved)


def test_the_sweep_walks_forward_and_never_backward(app_mod, clean_map):
    a = FakeApp()
    got = app_mod._ecc_next_unmeasured(a, 3, 100, 6)
    assert [b for _ce, b, _slc in got] == [100, 101, 102, 103, 104, 105]
    assert all(ce == 3 for ce, _b, _s in got)


def test_the_sweep_crosses_a_ce_boundary(app_mod, clean_map):
    """The bug this replaced ended the sweep at the end of one CE, leaving
    eleven untouched and reporting 'finished'."""
    a = FakeApp()
    nb = app_mod.N_BLOCK
    got = app_mod._ecc_next_unmeasured(a, 2, nb - 2, 4)
    assert [(ce, b) for ce, b, _s in got] == [
        (2, nb - 2), (2, nb - 1), (3, 0), (3, 1)]


def test_the_sweep_wraps_to_the_start_and_terminates(app_mod, clean_map):
    a = FakeApp()
    nb, nce = app_mod.N_BLOCK, app_mod.N_CE
    got = app_mod._ecc_next_unmeasured(a, nce - 1, nb - 1, 3)
    assert [(ce, b) for ce, b, _s in got] == [(nce - 1, nb - 1), (0, 0), (0, 1)]
    clean_map[:] = 5                                   # everything measured
    assert app_mod._ecc_next_unmeasured(a, 0, 0, 8) == []


def test_measured_blocks_are_skipped(app_mod, clean_map):
    a = FakeApp()
    clean_map[:] = 5
    for i in (7, 9, 4321):
        clean_map[i] = 0
    got = app_mod._ecc_next_unmeasured(a, 0, 0, 8)
    assert [ce * app_mod.N_BLOCK + b for ce, b, _s in got] == [7, 9, 4321]


def test_blocks_with_no_programmed_page_never_reach_the_decoder(app_mod,
                                                                clean_map):
    """The big one: a block the summary says holds no data is settled from the
    summary, not by reading and decoding twelve pages of erased media."""
    model = pytest.importorskip("nandgui.model")
    nb = app_mod.N_BLOCK
    a = FakeApp()
    s = FakeSummary(app_mod.N_CE, nb, programmed_at=(nb * 1 + 5,))
    a.summary = s

    cand = [(1, b, True) for b in range(4, 8)]
    rest = app_mod._ecc_settle_without_decoding(a, cand)
    assert rest == [(1, 5, True)], "only the block with data needs decoding"
    # and the others are recorded, so the sweep will not offer them again
    for b in (4, 6, 7):
        assert clean_map[nb + b] == int(model.EccState.ERASED)
    assert clean_map[nb + 5] == 0


def test_a_settled_block_reads_back_as_erased_not_unmeasured(app_mod,
                                                             clean_map):
    """The hover line and the colour have to agree.  A settled block has no
    BlockEcc behind it, and the fallback used to say 'nobody has looked'."""
    model = pytest.importorskip("nandgui.model")
    a = FakeApp()
    nb = app_mod.N_BLOCK
    s = FakeSummary(app_mod.N_CE, nb)
    a.summary = s
    app_mod._ecc_settle_without_decoding(a, [(2, 11, True)])
    assert clean_map[2 * nb + 11] == int(model.EccState.ERASED)
    line = app_mod.ecc_hover(a, 2, 11)
    assert "erased" in line.lower()
    assert "nobody has looked" not in line.lower()


def test_the_candidate_search_stays_inside_a_frame_when_nearly_full(app_mod,
                                                                    clean_map):
    """It ran as a Python loop over the map and cost 79 ms a frame at this fill
    level, which is 12 fps of looking for zeros.  Generous bound: the point is
    that it is not linear-in-Python, not the exact microseconds."""
    a = FakeApp()
    clean_map[:] = 5
    clean_map[-1] = 0                       # the one hole, as far away as it gets
    t0 = time.perf_counter()
    for _ in range(10):
        app_mod._ecc_next_unmeasured(a, 0, 0, app_mod.ECC_SWEEP_CANDIDATES)
    per_call = (time.perf_counter() - t0) / 10
    assert per_call < 0.005, "%.1f ms per candidate draw" % (per_call * 1000)
