"""Guards for the branches the media selftests never take.

Every check in here is arithmetic or a stub -- no dump, no store, no serial
port, per `conftest.py`.  They exist because a static sweep over this tree
found a family of "this could be None / this may be unbound" sites, and for
each one the answer was either

  * it IS reachable, so the code was fixed and the fix is pinned here; or
  * it is NOT reachable BECAUSE of some invariant elsewhere -- in which case
    the invariant is pinned here, so that removing it fails in this file
    instead of failing in the field a year later.

The distinction is the whole point.  A guard added to silence a checker,
with nothing recording WHY the branch could not be taken, is worth less than
the sentence it replaced.
"""
import pytest


# ==========================================================================
# _Lut -- "entries may be None, but then you MUST override both accessors"
# ==========================================================================
def test_a_lut_built_without_entries_states_its_contract(ftl):
    """`_Lut(None, g)` is legal only for a subclass that overrides `n_entries`
    AND `entry` -- which is exactly what `LutRegion` does, because the user
    region is 10.9 M entries and materialising it is 43 MB per lookup.

    The base class must SAY that.  Before this, a subclass that overrode one
    accessor and forgot the other died with `object of type 'NoneType' has no
    len()` several frames from the mistake.
    """
    lut = ftl._Lut(None, 4)
    with pytest.raises(NotImplementedError) as exc:
        lut.n_entries
    assert "_Lut" in str(exc.value)
    with pytest.raises(NotImplementedError):
        lut.entry(0)


def test_a_non_resident_lut_still_reports_UNKNOWN_for_its_histograms(ftl):
    """The contract error must not swallow the honest UNKNOWN answers.

    `weru_histogram` on a non-resident table means "reading the whole table",
    and returning UNKNOWN rather than raising is deliberate.
    """
    lut = ftl._Lut(None, 4)
    assert lut.weru_histogram() is ftl.UNKNOWN
    assert lut.sentinel_histogram() is ftl.UNKNOWN


def test_a_resident_lut_is_unaffected(ftl):
    lut = ftl._Lut((7, 8, 9), 4)
    assert lut.n_entries == 3
    assert lut.entry(1) == 8
    assert lut.covered_lmas == 12


# ==========================================================================
# raid -- `_phys` may return None, and every caller must reach a refusal first
# ==========================================================================
@pytest.fixture(scope="module")
def raid():
    return pytest.importorskip("titania.raid")


def _off_package_config(ftl):
    """A WERU whose slot 6 would land on CE 12 -- past the package.

    Not a configuration this drive has; it is the shape `_phys` returns None
    for, which is what the callers' preconditions have to survive.
    """
    return ftl.WeruConfig(11, 6, 10, True, blocks_per_ce=1)


def test_phys_refuses_a_slot_past_the_last_ce(raid, ftl):
    cfg = _off_package_config(ftl)
    assert raid._phys(cfg, 0) == (6, 10)
    assert raid._phys(cfg, 6) is None
    assert raid._phys(cfg, cfg.blocks_per_weru + 1) is None


def test_rebuild_page_refuses_before_it_dereferences_phys(raid, ftl):
    """THE INVARIANT `rebuild_page` RELIES ON.  It indexes `_phys(...)[0]`
    without a None check, and that is sound only because `stored_layer_ok`
    runs first and refuses any config with an unplaceable slot.  `dev=None`
    proves the refusal happens before a single page is read.
    """
    cfg = _off_package_config(ftl)
    ok, why = raid.stored_layer_ok(cfg)
    assert ok is False
    assert "no physical address" in why
    with pytest.raises(ValueError):
        raid.rebuild_page(None, cfg, 20, 6, 10, 0)


def test_check_page_refuses_a_user_weru_before_reading_anything(raid, ftl, geom):
    """THE REACHABLE ONE.  `titania raid check --weru <user weru>` passes a
    48-block config straight into `check_page`; its parity slot is
    `ce = 48 // 4 = 12`, one past the twelfth die, so `_phys` returns None.

    The old code read all 48 data members and only then died on
    `TypeError: 'NoneType' object is not subscriptable` -- a full page-read
    pass spent to produce a message that named neither the WERU nor the real
    answer, which is that a 48-block WERU is cRaidTypeMso and has no inline
    parity block.  `dev=None` is what proves nothing is read first.
    """
    cfg = ftl.WeruConfig(geom.BLOCKS_PER_WERU, 0, 100, False, blocks_per_ce=4)
    assert raid._phys(cfg, geom.BLOCKS_PER_WERU) is None
    with pytest.raises(ValueError) as exc:
        raid.check_page(None, cfg, 117, 0)
    msg = str(exc.value)
    assert "117" in msg and "cRaidTypeInline" in msg
    with pytest.raises(ValueError):
        raid.rebuild_message(None, cfg, 117, 0, 100, 0)


def test_inline_stripe_returns_none_rather_than_a_short_stripe(raid, ftl):
    """`selftest` used to call `st.members` straight after noting `st is
    None`, which turned a reportable FAIL into an AttributeError that aborted
    the run.  A None here has to stay producible for that fix to mean
    anything."""
    cfg = _off_package_config(ftl)
    assert raid.inline_stripe(cfg, 20, 0, 0) is None
    good = ftl.WERU_CONFIGS[14]
    st = raid.inline_stripe(good, 14, 0, 0)
    assert st is not None and st.width == good.blocks_per_weru


# ==========================================================================
# dumpdev -- `conflicts()` must be the O(contested) body, not the O(pages) one
# ==========================================================================
def test_conflicts_goes_through_contested(tmp_path):
    """Two definitions of `conflicts` existed and the SECOND -- the pre-store
    one, which sorts every entry in the index -- was the one that survived.

    On a full store that meant building and sorting every entry in the index --
    tens of thousands of `DumpEntry` objects, an order of magnitude slower --
    to reach the few thousand contested keys `_contested()` already had, which
    silently defeated the whole point of `_contested()`.  The check is
    behavioural: patch `_contested` and prove the answer came through it.
    """
    dd = pytest.importorskip("titania.dumpdev")
    a = tmp_path / "a.bin"
    a.write_bytes(b"\x00" * 16)
    b = tmp_path / "b.bin"
    b.write_bytes(b"\xff" * 16)
    key = (1, 2, 3, True)
    ent = dd.DumpEntry(a, "stub", 1, 2, 3, True)
    ent.repeats = [b]

    dev = object.__new__(dd.DumpDevice)
    dev._store = None
    dev._repointed = {}
    # Deliberately EMPTY.  The pre-store body walks `entries`, so if it were
    # still the live one this call would find nothing and the assert below
    # would fail on the result rather than on the call count.
    dev.entries = {}
    calls = []

    def _contested():
        calls.append(1)
        yield key, ent

    dev._contested = _contested
    out = dev.conflicts()
    assert calls == [1], "conflicts() no longer routes through _contested()"
    assert out == [(key, a, b)]


def test_identical_repeats_are_still_not_conflicts(tmp_path):
    """The honest half of the rule, which the dedup must not disturb: equal
    bytes prove nothing either way and are NOT reported."""
    dd = pytest.importorskip("titania.dumpdev")
    a = tmp_path / "a.bin"
    a.write_bytes(b"\x5a" * 16)
    b = tmp_path / "b.bin"
    b.write_bytes(b"\x5a" * 16)
    key = (1, 2, 3, True)
    ent = dd.DumpEntry(a, "stub", 1, 2, 3, True)
    ent.repeats = [b]
    dev = object.__new__(dd.DumpDevice)
    dev._store = None
    dev._repointed = {}
    dev.entries = {key: ent}
    assert dev.conflicts() == []


# ==========================================================================
# sv_subslot -- an unobserved position is "not derived", never a fabricated map
# ==========================================================================
class _StubWeru(object):
    def __init__(self, weru_id):
        self.weru_id = weru_id
        self.shape = "rectangular"


class _StubFtl(object):
    def __init__(self, block_map):
        self._bm = block_map

    def block_map(self):
        return self._bm

    def werus(self):
        return [_StubWeru(w) for w in self._bm]


def test_derive_perm_refuses_when_nothing_was_observed():
    """`obs` is a defaultdict and `perm` was built over `range(48)`, so a
    position no record covered was read back as an EMPTY set and indexed --
    an IndexError where the honest answer is "not derived".  With no
    rectangular records at all, every position is unobserved.
    """
    sv = pytest.importorskip("attic.sv_subslot")   # retired to attic/, still tested
    n, perm, amb, _obs = sv.derive_perm(_StubFtl({}))
    assert n == 0
    assert perm is None
    assert len(amb) == 48


def test_derive_perm_reproduces_the_closed_form(geom):
    """The positive control: one synthetic rectangle laid out by `pos_of` must
    give back exactly the closed form's permutation."""
    sv = pytest.importorskip("attic.sv_subslot")   # retired to attic/, still tested
    base = 100
    slots = [None] * geom.BLOCKS_PER_WERU
    for ce in range(geom.CE_COUNT):
        for q in range(4):
            slots[sv.pos_of(ce, q)] = (base + q, ce)
    n, perm, amb, _obs = sv.derive_perm(_StubFtl({29: slots}))
    assert n == 1
    assert amb == {}
    assert perm is not None
    assert sorted(perm) == list(range(geom.BLOCKS_PER_WERU))
    inv = [None] * geom.BLOCKS_PER_WERU
    for k, s in enumerate(perm):
        inv[s] = k
    assert inv == [sv.pos_of(s // 4, s % 4)
                   for s in range(geom.BLOCKS_PER_WERU)]
