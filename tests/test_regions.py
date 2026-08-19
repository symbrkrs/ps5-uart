"""Regions and the layout-aware acceptance test -- the two false-failure classes.

A store sweep that decoded every page as `codec.FORMAT_A` and accepted only
pages with four consecutive aux LMAs reported 2114 failures, and 1868 of them
were not read failures:

  * 1840 were block 0 on CE 1..5 -- the group-2 firmware region, which has its
    own 20x512 ECC geometry and cannot decode as FORMAT_A on any page;
  * 28 were page 287 of blocks 172..175, captured native-TLC on WERU 84, which
    is pseudo-SLC.

And the same acceptance test rejected EVERY RAID-parity page on the drive,
because layout 3 carries no LMA array at all.

Everything below is arithmetic and needs no media.  The measurements against
this board's actual pages live in `titania.verdict.selftest`, next to the
evidence.
"""
import random

import pytest


# --------------------------------------------------------------- the region map
def test_group2_is_block_0_on_the_first_six_ces(geom):
    for ce in geom.FW_GROUP2_CES:
        assert geom.region(ce, 0) == geom.REGION_FW_GROUP2


def test_block_0_on_ce_6_to_11_is_not_group_2(geom):
    """The CE test is load-bearing, not decoration.

    Block 0 on CE 6..11 holds ordinary FORMAT_A content (measured: MCRC-clean
    pages owned by msoId 57 on CE6).  A region map that keyed on the block
    number alone would push them into the group-2 decoder.
    """
    for ce in range(6, geom.CE_COUNT):
        assert geom.region(ce, 0) == geom.REGION_FTL


def test_group01_is_blocks_6_to_9_on_every_ce(geom):
    for ce in range(geom.CE_COUNT):
        for block in (6, 7, 8, 9):
            assert geom.region(ce, block) == geom.REGION_FW_GROUP01


def test_everything_else_is_ftl(geom):
    for ce in range(geom.CE_COUNT):
        for block in (1, 5, 10, 27, 28, 172, 852, 3608, 4095):
            assert geom.region(ce, block) == geom.REGION_FTL


def test_regions_are_exhaustive_and_named(geom):
    for ce in range(geom.CE_COUNT):
        for block in range(0, 64):
            assert geom.region(ce, block) in geom.REGIONS


def test_geometry_and_fw_agree_on_the_boundaries(geom):
    fw = pytest.importorskip("titania.fw")
    assert fw.GROUP2_BLOCK == geom.FW_GROUP2_BLOCK
    assert tuple(fw.GROUP2_CES) == tuple(geom.FW_GROUP2_CES)
    flat = tuple(sorted(b for bs in fw.GROUP_BLOCKS.values() for b in bs))
    assert flat == tuple(geom.FW_GROUP01_BLOCKS)


# ------------------------------------------------- the layout-aware acceptance
def test_consecutive_lmas_are_accepted_on_the_lma_layouts(auxmod):
    for lay in (0, 1, 2):
        page = auxmod.encode_page(lay, lmas=[100, 101, 102, 103], sequence=7)
        assert auxmod.read_is_plausible(page, lay) is True


def test_a_scrambled_lma_order_is_refused(auxmod):
    for lay in (0, 1, 2):
        page = auxmod.encode_page(lay, lmas=[100, 103, 101, 102], sequence=7)
        assert auxmod.read_is_plausible(page, lay) is False


def test_all_sentinel_lmas_are_a_pad_not_a_bad_read(auxmod):
    """`sub_1C238` pads an unwritten AU with `ff ff ff ff 00 00 00 00`.

    Rejecting those would throw away the RootStorage padding pages and the
    msoType-6 redirect tags -- 75 MCRC-clean pages in this store alone.
    """
    for lay in (0, 1, 2):
        page = auxmod.encode_page(lay, lmas=None, sequence=0)
        assert auxmod.read_is_plausible(page, lay) is True
        for sentinel in auxmod.LMA_SENTINELS:
            page = auxmod.encode_page(lay, lmas=[sentinel] * 4, sequence=0)
            assert auxmod.read_is_plausible(page, lay) is True


def test_a_half_written_page_is_still_refused(auxmod):
    """One real address and three sentinels is a torn write, not a pad."""
    for lay in (0, 1, 2):
        page = auxmod.encode_page(
            lay, lmas=[7, auxmod.LMA_INVALID, auxmod.LMA_INVALID,
                       auxmod.LMA_INVALID], sequence=0)
        assert auxmod.read_is_plausible(page, lay) is False


def test_layout_3_has_no_lma_array_and_is_judged_on_its_own_terms(auxmod):
    """The bug this whole file exists for.

    `sub_1C8D8` stamps only AU2's and AU3's SECOND dword; every first dword
    comes verbatim out of the ECU RAID accumulator.  So a parity page can never
    show four consecutive LMAs, and the blanket crib rejects 100% of them.
    """
    page = auxmod.encode_page(3, sequence=5, stripe_id=1234,
                              inline_parity=True)
    assert auxmod.parse_page(page, 3).lmas_consecutive is auxmod.UNKNOWN
    assert auxmod.read_is_plausible(page, 3) is True


def test_layout_3_rejects_a_carried_state_that_contradicts_the_stripe_id(auxmod):
    """Accumulated but UNADDRESSED cannot happen: one condition, two readouts."""
    page = auxmod.encode_page(3, sequence=5, stripe_id=None)   # -> INVALID
    assert auxmod.parity_stripe_complete(page) is True
    assert auxmod.read_is_plausible(page, 3) is False


def test_random_aux_is_refused_on_every_layout(auxmod):
    """The control.  Without this, "accepts everything" would also pass."""
    rnd = random.Random(0x1C438)
    for lay in auxmod.LAYOUTS:
        for _ in range(500):
            page = [bytes(rnd.randrange(256) for _ in range(auxmod.AUX_BYTES))
                    for _ in range(auxmod.AUS_PER_PAGE)]
            assert auxmod.read_is_plausible(page, lay) is False


def test_no_layout_means_unknown_never_a_default(auxmod):
    page = auxmod.encode_page(0, lmas=[1, 2, 3, 4], sequence=0)
    assert auxmod.read_is_plausible(page, None) is auxmod.UNKNOWN
    assert auxmod.read_is_plausible(page, auxmod.UNKNOWN) is auxmod.UNKNOWN


def test_a_partial_page_is_unknown_not_false(auxmod):
    page = auxmod.encode_page(0, lmas=[1, 2, 3, 4], sequence=0)[:2]
    assert auxmod.read_is_plausible(page, 0) is auxmod.UNKNOWN


def test_every_layout_has_a_stated_rule(auxmod):
    assert set(auxmod.ACCEPTANCE_BY_LAYOUT) == set(auxmod.LAYOUTS)
    assert all(auxmod.ACCEPTANCE_BY_LAYOUT[l] for l in auxmod.LAYOUTS)


def test_an_out_of_range_layout_still_raises(auxmod):
    page = auxmod.encode_page(0, lmas=[1, 2, 3, 4], sequence=0)
    with pytest.raises(ValueError):
        auxmod.read_is_plausible(page, 4)


# ------------------------------------------------------------------- verdicts
def test_erased_and_dead_ce_cost_no_decode(verdict, geom):
    page = b"\xFF" * verdict.PAGE_BYTES
    assert verdict.classify(page, 1, 3608, 8).verdict == verdict.ERASED
    dead = b"\xA7" * 2304 + b"\x00" * (verdict.PAGE_BYTES - 2304)
    assert verdict.classify(dead, 5, 3608, 8).verdict == verdict.DEAD_CE
    # ...and CE0 by NUMBER, whatever the bytes look like
    assert verdict.classify(page, 0, 3608, 8).verdict == verdict.DEAD_CE


def test_a_short_page_is_unreadable_not_a_failure(verdict):
    assert verdict.classify(b"\x00" * 16, 1, 3608, 8).verdict \
        == verdict.UNREADABLE
    assert verdict.classify(None, 1, 3608, 8).verdict == verdict.UNREADABLE


def test_the_region_is_reported_on_every_verdict(verdict, geom):
    page = b"\xFF" * verdict.PAGE_BYTES
    assert verdict.classify(page, 1, 0, 7, slc=True).region \
        == geom.REGION_FW_GROUP2
    assert verdict.classify(page, 1, 8, 7, slc=True).region \
        == geom.REGION_FW_GROUP01
    assert verdict.classify(page, 1, 3608, 8).region == geom.REGION_FTL


def test_not_a_failure_is_a_subset_of_the_verdicts(verdict):
    assert set(verdict.NOT_A_FAILURE) <= set(verdict.VERDICTS)
    assert verdict.FAIL not in verdict.NOT_A_FAILURE
    assert verdict.WRONG_MODE not in verdict.NOT_A_FAILURE
    assert verdict.AUX_REJECTED not in verdict.NOT_A_FAILURE


def test_classifier_without_an_ftl_applies_no_crib(verdict):
    """No descriptors means no layout and no cell mode -- not a guessed one."""
    c = verdict.Classifier(None)
    assert c.layout_of(3608, 1) is None
    assert c.cell_mode_of(3608, 1) is None


def test_a_wrong_mode_capture_never_demotes_a_page_that_decodes(verdict,
                                                                monkeypatch):
    """`wrong-mode` explains a FAILURE.  It must not overrule a good read.

    A verdict that let a configuration table demote demonstrably good bytes
    would be exactly the kind of table-over-measurement error the cell-mode
    work exists to prevent.
    """
    page = b"\xFF" * verdict.PAGE_BYTES
    v = verdict.classify(page, 1, 172, 287, slc=False, cell_mode=True)
    assert v.verdict == verdict.ERASED          # decided on the bytes, first
