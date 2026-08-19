"""The WMA <-> physical map, and the two independent implementations of it.

`titania.ftl` derived this from the firmware decompilation; `nk_wma.py` (vendored
as `titania._wma_ref`) derived it from the board.  The value of the cross-check
is entirely in the two being separate derivations -- see titania/_wma_ref.py.

`titania.ftl._selftest` already checks the two against each other over 20000
WMAs, but only where a dump is present.  This file re-runs the comparison and
the round trips with no media at all.
"""
import pytest


@pytest.fixture(scope="module")
def ref():
    from titania import _wma_ref
    return _wma_ref


def test_split_wma_is_exact_arithmetic(ftl, geom):
    """split_wma answers for every WMA, configured or not."""
    for wma in (0, 1, 3, 4, 191, 192, 193, 211967, 211968, 211968 * 14 + 777):
        weru, page, slot, au = ftl.split_wma(wma)
        assert 0 <= au < ftl.WMAS_PER_PAGE
        assert 0 <= slot < ftl.MAX_BLOCKS_PER_WERU
        # the decomposition must rebuild the input exactly
        assert (weru * geom.AUS_PER_TLC_WERU
                + page * ftl.WMA_STRIDE
                + slot * ftl.WMAS_PER_PAGE + au) == wma


def test_stride_is_the_max_not_the_actual(ftl):
    """192 = cMaxBlocksPerWeru * wmasPerPage, and it is CONSTANT.

    The tail of every window is padding when blocksPerWeru < 48.  Hard-coding 48
    as the real block count is the mistake this constant exists to prevent, so
    the two must stay distinguishable.
    """
    assert ftl.WMA_STRIDE == ftl.MAX_BLOCKS_PER_WERU * ftl.WMAS_PER_PAGE == 192
    assert all(c.blocks_per_weru < ftl.MAX_BLOCKS_PER_WERU
               for c in ftl.WERU_CONFIGS.values())


def test_wma_round_trip_over_configured_werus(ftl):
    """wma -> (ce, block, page, au) -> wma, for every addressable WMA."""
    import titania.geometry as geom
    n = 0
    for wid, cfg in sorted(ftl.WERU_CONFIGS.items()):
        base = wid * geom.AUS_PER_TLC_WERU
        for wma in range(base, base + 3 * ftl.WMA_STRIDE):
            p = ftl.wma_to_physical(wma, cfg)
            if p is None:
                assert ftl.wma_unaddressable_reason(wma, cfg)
                continue
            back = ftl.physical_to_wma(p.ce, p.block, p.page, p.au, cfg, wid)
            assert back == wma, (wid, wma, p)
            n += 1
    assert n > 0


def test_the_unused_tail_addresses_nothing(ftl, geom):
    """A slot past blocksPerWeru is 'invalid input wma', not an address.

    Returning an address there would be an invention -- and it is the padding
    that explains the userLutLut's +152 jumps, so it must stay visible.
    """
    wid, cfg = 14, ftl.WERU_CONFIGS[14]
    base = wid * geom.AUS_PER_TLC_WERU
    addressable = unaddressable = 0
    for slot in range(ftl.MAX_BLOCKS_PER_WERU):
        wma = base + slot * ftl.WMAS_PER_PAGE
        p = ftl.wma_to_physical(wma, cfg)
        if slot < cfg.blocks_per_weru:
            assert p is not None
            addressable += 1
        else:
            assert p is None
            reason = ftl.wma_unaddressable_reason(wma, cfg)
            assert "invalid input wma" in reason
            unaddressable += 1
    assert addressable == cfg.blocks_per_weru == 11
    assert unaddressable == 48 - 11 == 37
    # the observed +152: 192 - 11*4 = 148 of padding, plus the last +4 step
    assert ftl.WMA_STRIDE - cfg.blocks_per_weru * ftl.WMAS_PER_PAGE == 148


def test_sentinels_map_nowhere(ftl):
    cfg = ftl.WERU_CONFIGS[14]
    assert ftl.LUT_SENTINELS == (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)
    for s in ftl.LUT_SENTINELS:
        assert ftl.wma_to_physical(s, cfg) is None
        assert "sentinel" in ftl.wma_unaddressable_reason(s, cfg)


def test_slot_to_physical_is_ce_major_block_pair(ftl):
    """slot = 2*(ce - ceBase) + (block - blockBase): a block PAIR on six CEs."""
    cfg = ftl.WERU_CONFIGS[14]                  # blocks 16/17 on CE 6..11
    got = [cfg.slot_to_physical(s) for s in range(cfg.blocks_per_weru)]
    assert got[0] == (6, 16)
    assert got[1] == (6, 17)
    assert got[2] == (7, 16)
    assert got[-1] == (11, 16)                  # 11 slots, so the pair is odd
    assert cfg.slot_to_physical(cfg.blocks_per_weru) is None


def test_ce_split_twins_are_distinct_werus(ftl):
    """The same block pair on the other six channels is a DIFFERENT WERU.

    19 and 20 are both blocks 10/11; they differ only by ceBase.  Collapsing
    them is what made `weru << n | offset` never land inside a WERU.
    """
    a, b = ftl.WERU_CONFIGS[19], ftl.WERU_CONFIGS[20]
    assert (a.block_base, b.block_base) == (10, 10)
    assert a.ce_base != b.ce_base
    assert set(a.slot_to_physical(s) for s in range(a.blocks_per_weru)).isdisjoint(
        set(b.slot_to_physical(s) for s in range(b.blocks_per_weru)))


# ---------------------------------------------------------------------------
# the independent second implementation
# ---------------------------------------------------------------------------
def test_vendored_reference_is_not_the_same_code(ref, ftl):
    """The cross-check is only worth something while the two are separate.

    If `_wma_ref` ever starts importing `titania.ftl`, the agreement test below
    becomes a tautology.  Catch that here rather than trusting a green tick.
    """
    import inspect
    src = inspect.getsource(ref)
    assert "import titania" not in src
    assert "from titania" not in src
    assert "from . import" not in src
    # and it must not simply BE ftl's function object
    assert ref.wma_to_physical is not ftl.wma_to_physical
    assert ref.WeruConfig is not ftl.WeruConfig


def test_two_implementations_agree(ref, ftl, geom):
    """Same answer for every WMA in the first pages of every shared WERU."""
    n = diff = 0
    shared = sorted(set(ftl.WERU_CONFIGS) & set(ref.CONFIGS))
    assert len(shared) >= 4
    for wid in shared:
        mine_cfg, theirs_cfg = ftl.WERU_CONFIGS[wid], ref.CONFIGS[wid]
        base = wid * geom.AUS_PER_TLC_WERU
        for wma in range(base, base + 4 * ftl.WMA_STRIDE):
            mine = ftl.wma_to_physical(wma, mine_cfg)
            theirs = ref.wma_to_physical(wma, theirs_cfg)
            mine_t = None if mine is None else (mine.ce, mine.block,
                                                mine.page, mine.au)
            n += 1
            diff += mine_t != theirs
    assert diff == 0
    assert n >= 3000


def test_vendored_copy_has_not_drifted_from_upstream(ref):
    """`titania/_wma_ref.py` is a verbatim copy of `nk_wma.py` below its header.

    Divergence is itself a finding, so it is checked rather than assumed.  If
    the root file is absent (installed package, no checkout) this skips.
    """
    import inspect
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    upstream = root / "titania" / "nk_wma.py"       # moved into the package
    if not upstream.exists():
        upstream = root / "nk_wma.py"               # pre-move checkout
    if not upstream.exists():
        import pytest as _p
        _p.skip("nk_wma.py not in this tree")
    marker = "Original docstring follows."
    vendored = Path(inspect.getfile(ref)).read_text(encoding="utf-8")
    tail = vendored.split(marker, 1)[1]
    tail = tail.split("\n", 2)[2]              # drop the rule and the restated title line
    up = upstream.read_text(encoding="utf-8")
    body = "AUS_PER_WERU ="
    assert tail[tail.index(body):] == up[up.index(body):]
