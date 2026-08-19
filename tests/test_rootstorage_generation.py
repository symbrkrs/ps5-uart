"""The Bootstrap RootStorage's two SECTIONS, and picking the live one.

Blocks 2/3 and 4/5 are not four copies and not four generations: they are the
two sections of ONE store (`numberSections` is the literal `2u` passed at
`efc_cpu2.c:149143`, and `cMaxNumberOfSections` is 2), one section per WERU,
each section a block pair on twelve CEs.  A section switch rewrites every
object, so mounting the wrong one is a silent whole-image staleness.

The firmware picks with `ScanLastStripe`'s `sub_4036268A`
(`efc_cpu2.c:146497-146532`): highest section base stripe wins, EXCEPT that two
adjacent sections differing by more than one section's worth means the counter
has wrapped and the LOWER-indexed section is taken.  The wrap is real, not
defensive: `sub_40367130` (`:151164`) advances a stripe number modulo
`2 * numberOfAvailableStripes`, so the counter makes exactly two passes over the
physical stripe space before returning to 0.

Everything here is arithmetic and needs no media.  The measurement against this
board -- section base stripes `[0, 6624]`, override fires, and the chosen
section's SegmentedBuffer header sequence numbers are higher on 18 objects and
lower on 0 -- lives in `titania.ftl.selftest`, next to the evidence.
"""
import pytest

from titania import ftl


SPS = 2208                      # 6 stripes/page x 368 pseudo-SLC pages
MODULUS = 2 * ftl.ROOTSTORAGE_SECTIONS * SPS            # 8832


def test_this_board_takes_section_0_by_the_wrap_override():
    # section 0 holds stripes 0.., section 1 holds 6624.. -- one full pass apart
    assert ftl.scan_last_stripe([0, 6624], SPS) == 0


def test_plain_highest_wins_would_have_taken_the_stale_section():
    """The check that makes the override load-bearing rather than decorative."""
    last = [0, 6624]
    naive = max(range(len(last)), key=lambda i: last[i])
    assert naive == 1
    assert ftl.scan_last_stripe(last, SPS) != naive


@pytest.mark.parametrize("last,want", [
    ([4416, 2208], 0),          # ordinary round robin, section 0 newer
    ([4416, 6624], 1),          # ordinary round robin, section 1 newer
    ([2208, 4416], 1),
])
def test_the_override_does_not_fire_on_an_ordinary_round_robin(last, want):
    """Adjacent sections one section apart must fall through to highest-wins."""
    assert abs(last[1] - last[0]) == SPS        # exactly at the bound, not over
    assert ftl.scan_last_stripe(last, SPS) == want


@pytest.mark.parametrize("last,want", [
    ([0, ftl.INVALID_STRIPE], 0),
    ([ftl.INVALID_STRIPE, 2208], 1),
    ([ftl.INVALID_STRIPE, ftl.INVALID_STRIPE], 0),
])
def test_erased_sections_read_0xffff_and_are_skipped(last, want):
    assert ftl.scan_last_stripe(last, SPS) == want


def test_a_single_section_is_answered_without_comparing():
    assert ftl.scan_last_stripe([1234], SPS) == 0


def test_no_sections_is_an_error_not_a_default():
    with pytest.raises(ValueError):
        ftl.scan_last_stripe([], SPS)


def test_the_ported_asymmetry_is_reproduced_deliberately():
    """`sub_4036268A`'s override always takes `v8`, never `v8 + 1`.

    That is only correct when the wrap happens at the LOW-numbered section.  We
    reproduce the firmware rather than "fixing" it, so that our answer is the
    drive's answer; this test pins that choice so nobody silently improves it.
    """
    assert ftl.scan_last_stripe([6624, 0], SPS) == 0     # NOT 1


def test_every_stripe_number_maps_into_a_real_section():
    """`sub_40367764`: section = (stripe % availableStripes) / stripesPerSection."""
    avail = ftl.ROOTSTORAGE_SECTIONS * SPS
    for stripe in (0, 1, 2207, 2208, 4415, 4416, 6624, 8826, MODULUS - 1):
        sec = (stripe % avail) // SPS
        assert 0 <= sec < ftl.ROOTSTORAGE_SECTIONS


def test_the_counter_space_is_twice_the_physical_space():
    """Which is precisely why a wrap can make the newest number the smallest."""
    avail = ftl.ROOTSTORAGE_SECTIONS * SPS
    assert MODULUS == 2 * avail
    # one full pass apart -> different sections, and the later pass wraps to 0
    assert (0 % avail) // SPS == 0
    assert (6624 % avail) // SPS == 1
    assert (6624 + SPS) % MODULUS == 8832 % MODULUS == 0


def test_bootstrap_blocks_and_section_count_are_the_firmware_literals():
    assert ftl.ROOTSTORAGE_SECTIONS == 2
    assert ftl.ROOTSTORAGE_BOOTSTRAP_BLOCKS == (2, 3, 4, 5)
    # every Bootstrap block carries the same stripes-per-page, so one section's
    # stripe span is a property of the store, not of which block you looked at
    assert len({ftl.ROOTSTORAGE_STRIPES_PER_PAGE[b]
                for b in ftl.ROOTSTORAGE_BOOTSTRAP_BLOCKS}) == 1


# ------------------------------------------------ the rebuild-sequence lanes --
def test_the_pool_index_includes_the_reservation_bit():
    """`24*msoGroupId + 8*(msoDescriptor[msoId][19] & 1)` -- not the group alone."""
    pool = [(100, 10, 20)] * ftl.REBUILD_SEQ_POOL_GROUPS
    assert ftl.rebuild_sequence_lane(pool, 1, False) == (0, 100, None)
    assert ftl.rebuild_sequence_lane(pool, 1, True) == (1, 10, 20)


def test_the_reserved_window_bound_applies_only_to_the_reserved_lane():
    """The firmware panics on `[1] >= [2]` ONLY when bit 0 is set."""
    pool = [(5, 5, 5)] * ftl.REBUILD_SEQ_POOL_GROUPS
    assert ftl.rebuild_sequence_lane(pool, 0, False)[2] is None
    assert ftl.rebuild_sequence_lane(pool, 0, True)[2] == 5


def test_an_out_of_range_group_is_an_error_not_a_wrap():
    pool = [(1, 1, 1)] * ftl.REBUILD_SEQ_POOL_GROUPS
    with pytest.raises(ValueError):
        ftl.rebuild_sequence_lane(pool, ftl.REBUILD_SEQ_POOL_GROUPS, False)


# --------------------------------------------------- msoDescriptor bit fields --
def test_mso_descriptor_bits_decode_off_a_synthetic_record():
    raw = bytearray(ftl.MSO_DESC_ENTRY * ftl.geom.MAX_MSOS)
    # msoId 1: valid, weru-pool, fill class 2, allocType 4, rebuild 0x0d,
    #          +19 = reservation bit set and parityWeruFront 3
    o = ftl.MSO_DESC_ENTRY
    raw[o + 0] = (ftl.MSO_DESC_VALID_BIT | ftl.MSO_DESC_WERU_POOL_BIT
                  | (2 << ftl.MSO_DESC_FILL_CLASS_SHIFT))
    raw[o + ftl.MSO_DESC_ALLOC_TYPE_OFF] = 4
    raw[o + ftl.MSO_DESC_REBUILD_OFF] = 0x0D
    raw[o + ftl.MSO_DESC_SEQ_RESERVATION_OFF] = (
        ftl.MSO_DESC_SEQ_RESERVATION_BIT | (3 << ftl.MSO_DESC_PARITY_FRONT_SHIFT))
    m = ftl.decode_mso_descriptor(bytes(raw))[1]
    assert m.is_valid is True
    assert m.weru_pool is True
    assert m.fill_class == 2
    assert m.allocation_type == 4
    assert m.rebuild_flags & ftl.MSO_DESC_LUT_PAGE_ALIGNED_BIT
    assert not m.rebuild_flags & ftl.MSO_DESC_REBUILD_SETMAP_BIT
    assert m.needs_sequence_reservation is True
    assert m.parity_weru_front == 3


def test_an_all_zero_descriptor_is_not_reported_as_a_partition():
    """`decode_mso_descriptor` skips empty records -- absence, not msoId 0."""
    assert ftl.decode_mso_descriptor(bytes(ftl.MSO_DESC_ENTRY * 4)) == {}


def test_parity_front_7_is_the_none_sentinel():
    raw = bytearray(ftl.MSO_DESC_ENTRY * 2)
    raw[ftl.MSO_DESC_ENTRY] = ftl.MSO_DESC_VALID_BIT
    raw[ftl.MSO_DESC_ENTRY + ftl.MSO_DESC_SEQ_RESERVATION_OFF] = 0x0E
    m = ftl.decode_mso_descriptor(bytes(raw))[1]
    assert m.parity_weru_front == ftl.MSO_PARITY_FRONT_NONE
    assert m.needs_sequence_reservation is False


# ------------------------------------------------------- weruDescriptor +10 --
def test_weru_descriptor_10_is_the_cell_mode():
    raw = bytearray(ftl.WERU_DESC_ENTRY * 2)
    for w in (0, 1):
        o = w * ftl.WERU_DESC_ENTRY
        raw[o + ftl.WERU_DESC_SELF_OFF] = w
    raw[ftl.WERU_DESC_SLC_MODE_OFF] = ftl.WERU_DESC_SLC_MODE_SLC
    raw[ftl.WERU_DESC_ENTRY + ftl.WERU_DESC_SLC_MODE_OFF] = ftl.WERU_DESC_SLC_MODE_TLC
    d = ftl.decode_weru_descriptor(bytes(raw))
    assert d[0].slc_from_descriptor is True
    assert d[1].slc_from_descriptor is False


def test_an_unset_weru_descriptor_cell_mode_is_never_defaulted_to_slc():
    """0 means native TLC because the firmware says so, but an ABSENT record
    must still come back UNKNOWN rather than silently TLC."""
    w = ftl.Weru(7)
    assert w.slc_mode is ftl.UNKNOWN
    assert w.slc_from_descriptor is ftl.UNKNOWN


# -------------------------------------------------------------- LptEntry u64 --
def test_lpt_entry_8_is_one_u64_not_two_fields():
    raw = bytearray(ftl.LPT_ENTRY_BYTES)
    raw[0] = 3                                   # attr DEACT
    raw[8:16] = (0x1_0000_2222).to_bytes(8, "little")
    d = ftl.decode_lpt_entry(bytes(raw))
    assert d["activation_stamp"] == 0x1_0000_2222
    assert d["rebuild_sequence_number"] == 0x0000_2222
    assert d["activation_stamp_high"] == 1
    # the low half alone would compare WRONG against a pool lane past 2**32
    assert d["activation_stamp"] > d["rebuild_sequence_number"]


def test_lpt_entry_4_reports_its_sentinel_as_absent():
    raw = bytearray(ftl.LPT_ENTRY_BYTES)
    raw[4:6] = (0xFFFF).to_bytes(2, "little")
    d = ftl.decode_lpt_entry(bytes(raw))
    assert d["superblock_write_index"] is None
    assert d["superblock_write_index_raw"] == 0xFFFF
