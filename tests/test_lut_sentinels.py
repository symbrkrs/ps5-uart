"""Sentinel handling in the LUT and the block mapping.

Two separate "this means nothing is here" encodings, and getting either wrong
moves real data to the wrong address rather than reporting a gap:

  * LUT_SENTINELS -- four WMA values the LUT uses for "maps nowhere".  Note the
    asymmetry the ftl docstring records: three of the four are treated as
    unmapped by the valid-count, but -2 (0xFFFFFFFE) IS counted.
  * UNMAPPED_PBID -- an empty slot in a WERU's block mapping.  The list is
    SLOT-INDEXED: an unmapped slot 0 must leave the next block at index 1.
    Compacting the list renumbers every slot after a hole, and `slot` is exactly
    what `(wma % 192) // 4` consumes, so a compaction here silently shifts every
    address in the WERU.
"""
import struct

import pytest


def test_sentinels_are_the_top_four_values(ftl):
    assert ftl.LUT_SENTINELS == (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)
    assert all(s > 0xFFFFFF00 for s in ftl.LUT_SENTINELS)
    # they must be outside any WMA a real WERU can produce
    import titania.geometry as geom
    assert min(ftl.LUT_SENTINELS) > 63 * geom.AUS_PER_TLC_WERU


def test_unmapped_pbid_is_not_a_legal_pbid(ftl, geom):
    """0xFFFFFFFF must be unreachable as a real physicalBlockId."""
    assert ftl.UNMAPPED_PBID == 0xFFFFFFFF
    biggest = geom.pbid(geom.BLOCKS_PER_LUN * geom.LUNS_PER_CE - 1,
                        geom.CE_COUNT - 1)
    assert biggest < ftl.UNMAPPED_PBID


def _mapping(entries):
    """Build a raw blockMapping buffer from a list of pbid-or-None."""
    import titania.ftl as ftl
    buf = b""
    for pb in entries:
        buf += struct.pack("<I", ftl.UNMAPPED_PBID if pb is None else pb)
        buf += b"\0" * (ftl.BLOCK_MAPPING_ENTRY - 4)
    return buf


def test_block_mapping_is_slot_indexed_not_compacted(ftl, geom):
    """A hole keeps its slot.  This is the bug the ftl selftest names."""
    raw = _mapping([None, geom.pbid(7, 3)])
    raw += b"\xff" * (ftl.BLOCK_MAPPING_ENTRY * (geom.BLOCKS_PER_WERU - 2))
    slots = ftl.decode_block_mapping(raw)[0]
    assert len(slots) == geom.BLOCKS_PER_WERU
    assert slots[0] is None
    assert slots[1] == (7, 3)
    assert ftl.mapped_blocks(slots) == [(7, 3)]


def test_block_mapping_holes_in_the_middle_do_not_shift(ftl, geom):
    entries = [geom.pbid(1, 0), None, geom.pbid(2, 5), None, geom.pbid(3, 11)]
    raw = _mapping(entries)
    raw += b"\xff" * (ftl.BLOCK_MAPPING_ENTRY
                      * (geom.BLOCKS_PER_WERU - len(entries)))
    slots = ftl.decode_block_mapping(raw)[0]
    assert slots[0] == (1, 0)
    assert slots[1] is None
    assert slots[2] == (2, 5)
    assert slots[3] is None
    assert slots[4] == (3, 11)
    assert ftl.mapped_blocks(slots) == [(1, 0), (2, 5), (3, 11)]


def test_an_all_unmapped_mapping_is_empty_not_missing(ftl, geom):
    """Every slot unmapped is a MEASURED emptiness: [] , never UNKNOWN.

    The distinction is the whole point of the sentinel -- see test_unknown.py.
    """
    raw = _mapping([None] * geom.BLOCKS_PER_WERU)
    slots = ftl.decode_block_mapping(raw)[0]
    assert slots == [None] * geom.BLOCKS_PER_WERU
    assert ftl.mapped_blocks(slots) == []
    assert ftl.mapped_blocks(slots) is not ftl.UNKNOWN


def test_block_mapping_entry_stride(ftl):
    """20 bytes per entry, u32 pbid first.  A wrong stride reads pure noise."""
    assert ftl.BLOCK_MAPPING_ENTRY == 20


def test_lut_entry_counting_skips_sentinels(ftl, geom):
    """A LUT's live-entry count must not include the maps-nowhere values."""
    live = [14 * geom.AUS_PER_TLC_WERU + 4 * i for i in range(6)]
    entries = live + list(ftl.LUT_SENTINELS)
    counted = sum(1 for w in entries if w not in ftl.LUT_SENTINELS)
    assert counted == len(live) == 6


@pytest.mark.parametrize("sentinel", [0xFFFFFFFF, 0xFFFFFFFE,
                                      0xFFFFFFFD, 0xFFFFFFFC])
def test_every_sentinel_is_unaddressable_in_every_config(ftl, sentinel):
    for cfg in ftl.WERU_CONFIGS.values():
        assert ftl.wma_to_physical(sentinel, cfg) is None
        assert "sentinel" in ftl.wma_unaddressable_reason(sentinel, cfg)
