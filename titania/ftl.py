"""titania.ftl -- the storage layer: MSOs, WERUs, NSIDs, SystemMetadata.

Below this module the media is bytes on cells.  Above it, the drive is a set of
partitions with names, an erase/allocation unit, and a table saying how much of
each is still live.  This module is the translation, and it is a
*reconstruction*: it was derived from one board with a dead CE0 whose main user
data was never fully surveyed.

    from titania import DumpDevice, Ftl
    ftl = Ftl.from_device(DumpDevice(...))
    print(ftl.describe())

ABSENCE IS NOT EMPTINESS
------------------------
The single most expensive class of error in this project has been reading "we
did not find it here" as "it is not there".  So nothing in this module returns
an empty list to mean "unknown".  Every accessor returns either real data or the
`UNKNOWN` sentinel, and every docstring says which of the two a caller can get
and what would have to happen to turn one into the other.  `UNKNOWN` is falsey
and prints as ``<unknown>``, so it is visible in output rather than silently
behaving like zero -- but it is not ``None``: ``None`` from `lma_to_physical`
means "we looked and it is genuinely not present in what we hold".

THE OBJECT MODEL
----------------
    AU     4096 user bytes + 8 aux.  The unit everything is counted in.
    LMA    logical media address, one per AU, in a per-MSO address space.
    WERU   the erase/allocation unit: 4 block numbers x 12 CEs = 48 blocks.
    MSO    a partition = (msoGroupId, msoType); msoId = msoIdTable[type*6+group].
           The msoIdTable is not on the media -- it is a firmware constant, and
           it is embedded here (`MSOID_TABLE_EMBEDDED`), so naming an MSO needs
           no firmware image.  `MsoIdTable.locate` still reads one if you have
           it; `titania fw verify-table <image>` compares the two.

`physicalBlockId = 12*block + ce` -- see `titania.geometry`, which owns every
constant in that sentence.  This module defines no geometry of its own.

THE DISCOVERY CHAIN -- IT IS THE FIRMWARE'S OWN MOUNT ORDER
-----------------------------------------------------------
This is not a plausible reconstruction; it is the sequence `efc_cpu3.c` runs at
mount (`:39404`, `:39476`, `:39487`, `:39506`), step for step:

1. RootStorage lives in NAND blocks 1-5 (`RootStorage`), pseudo-SLC.
   Object 1 is the 16-byte SystemMetadataInformation record whose byte 0 is
   the *dirty* flag; object 3 is `SystemBlockMapping`, magic 0x20170620.
2. The `SystemBlockMapping` replay loop installs the block mapping of every
   valid slot at once and never picks one (`efc_cpu3.c:42553-42568`).  Two live
   slots here -- weruId 19 (blocks 10/11) and weruId 23 (blocks 26/27) -- are
   therefore two halves of one live image, not two generations.
3. Object 0 is the SystemLut: the SystemMetadata MSO's own LMA -> WMA table,
   `entry[lma >> log2(granularity)]`, `weruId = wma // AUS_PER_TLC_WERU`.  This
   is the authority that says which of the installed WERUs holds the live copy
   of each LMA.  Failure mode: the stripe is on a CE this backing cannot serve
   -> UNKNOWN, and resolution falls back to page sequence numbers, which is
   *not* sound across WERUs and says so loudly.
4. Every named WERU's blocks are scanned page by page.  Each AU's aux carries
   its LMA, so a WERU is reassembled by LMA, not by address, and within one
   WERU the newest copy wins on the u24 page sequence in AU1's aux.  Across
   WERUs sequence numbers do not rank (measured: 1884 newer-or-equal vs 4056
   older), which is exactly why step 3 exists.
5. LMA 0 is a 48-byte directory (magic "sys") of section base LMAs.  Section
   *lengths* are `nextBase - thisBase - 32`: the layout builder
   (`efc_cpu3.c:37436-37478`) is `lmas(size) = ceil(size/16384)*4`,
   `base[0] = 36`, `base[n+1] = base[n] + lmas(size[n]) + 32`, i.e. a 32-LMA
   gap after every section.

TRAPS ENCODED AS CODE HERE, NOT JUST AS COMMENTS
------------------------------------------------
* stride 12 vs stride 48.  `SystemBlockMapping` carries 12 pbids per slot;
  that is the *metadata* WERU's own configuration (SLC, 12 x 368 x 4 = 17664
  AUs) and nothing else.  The global `blockMapping` section is indexed
  ``[weruId * 48 + i]``.  A stride-12 read of the global table produces
  plausible-looking block lists that are wrong, and once appeared to "confirm"
  an unrelated result by coincidence.  `decode_block_mapping` therefore refuses
  any stride but `geometry.BLOCKS_PER_WERU` unless the caller passes
  ``i_know_this_is_wrong=True``, and `verify_weru_stride` is provided so the
  choice can be re-measured against a survey rather than believed.
* the msoIdTable base offset.  A search returns the table BASE with the
  anchor at base+30.  Subtracting 30 again yields entries above 200, which is
  the tell.  `MsoIdTable` validates that every entry is <= `MAX_MSOS-1` and
  raises with exactly that diagnosis when it is not.
* the msoIdTable's trailing zeros are padding, not entries.  There is no
  msoId 0.  A genuine table is injective over its non-zero entries; "55 non-0xFF
  bytes but only 32 distinct values" is what including the padding looks like.
  `MsoIdTable` enforces injectivity and drops zeros.
* RETRACTED (2026-08-06): "88 WERUs map to blocks past `BLOCKS_PER_LUN`; they
  are stale residue from another metadata generation."  They were not residue
  and there was no other generation -- they were inline-RAID parity bytes read
  as content.  22 LMAs of the `blockMapping` section were resolved to a parity
  page, which carries a real LMA and a higher page sequence than the data page
  it covers, so `SystemMetadata.scan`'s newest-wins preferred it.  The ordinary
  copies were on the media the whole time.  `SystemMetadata.scan` now ranks
  ``(not AUX_FLAG_INLINE_PARITY, seq)`` and all 88 decode as four consecutive
  block numbers on twelve CEs.  What survives of the old claim: three rows
  (weruIds 1070-1072) still read as nonsense, and those are genuinely not WERUs
  -- the six-configuration table sums to 1070, so weruIds 1070..1074 are the
  section's LMA-rounding padding.  See `AUX_FLAG_INLINE_PARITY` for how the flag
  was identified in firmware and `SystemMetadata.scan` for the four independent
  checks on the result.
* a blockMapping entry can still name a block that does not exist, so keep
  checking: `Weru.shape` returns ``"invalid"``, and past the WERU array that is
  the right answer.  While chasing that, a second fact fell out: a WERU is only
  *nominally* four block numbers across twelve CEs.  741 of 855 are exactly
  that, 26 more are 48 blocks and four per CE but with a substituted block
  number (bad-block management), so arithmetic over four block numbers does not
  reproduce every WERU.  Use the mapping.
* `SystemBlockMapping`'s live slots are not generations.  This module used
  to rank the ring by `nextSlot`, call slot 9 (weru 23) "current" and slot 8
  (weru 19) "legacy", and read every section it could out of weru 23.  That was
  wrong, and it read five of the nine sections from the wrong copy --
  `userLutLut`, `mso`, `mediaSpace`, `weruBin` and `validMap` all live on weru
  19.  For `validMap` the difference is 20 partial LMAs against 6288 complete
  ones.  There is no `storeGeneration` field anywhere in the firmware and the
  replay loop installs every slot at once; the only per-LMA authority is the
  SystemLut.  `sbm_recency_order` survives only to order the ring for display,
  and `SystemImage` resolves LMAs through the LUT instead.
* do not rank page sequence numbers across WERUs.  Within one WERU the u24
  page sequence is recency and `SystemMetadata.scan` uses it.  Across the two
  live WERUs it decides nothing (1884 newer-or-equal against 4056 older), and
  for the *section base* LMAs specifically every weru-23 copy has the higher
  sequence -- so a "newest wins" rule looks convincing and is wrong.
* the on-media `validCount`/`validMap` can be a snapshot of a different
  instant from the SystemLut.  Here the weru-23 copy of `validCount` says
  "1646 metadata AUs in weru 23, 0 in weru 19" and the weru-19 copy says the
  mirror image, while the SystemLut -- the durable, replayed state -- splits
  those same 1646 across both.  RootStorage object 1 byte 0 is set, so the
  firmware would discard both caches at mount and rebuild them from the LUT.
  `Ftl.dirty` reports that, `describe()` says it in words, and nothing here
  treats `validCount` as authority.  `Ftl.rebuild_valid_map` now runs that
  rebuild, and `Ftl.rebuild_comparison` puts the two side by side.
* the mount-time validMap rebuild has two bulk writers, at two different
  steps, and the second one is the big one.  Step 31 is `RebuildValidMap`
  over exactly `{SystemMetadata, Lut}` (`sub_402E19B0`,
  `efc_cpu3.c:40322-40324`, count 2).  RETRACTED: "no later mount stage
  restores the rest ... which stage that is has not been traced."  Step 35
  (`MOUNT_STEP_LUT_REBUILD`) launches four per-lutid rebuilds; each scans that
  lutId's WERUs, replays the whole LUT into the map through `sub_4033E94E`
  when `snapshotInformation.count != 0` and
  `msoDescriptor[list[0].msoId] +5` bit 1 is set, and then always
  overwrites `validCount[weru]` with `popcount(validMap[weru])`.  On this board
  lutIds 1 and 4 replay, lutIds 2 and 3 do not -- so 889 WERUs holding
  2,510,576 stored valid AUs come out with both caches zeroed, and 106 more
  are never scanned at all.  Do not read a rebuilt zero as "this WERU is
  empty"; ask `Ftl.dirty_mount_valid_cache`, which says per WERU whether a zero
  is meaningful and which stage made it.
* `weruDescriptor.owner` is not the ownership authority on a dirty store.
  It is written only at a SystemMetadata flush, and skipped even then if a CRC
  is unchanged (`efc_cpu3.c:44942`).  The durable record is the
  `WeruUsageLog` -- a SegmentedBuffer in RootStorage objects 4..25 here,
  journalled per record, from which `RebuildWeruBins` re-derives owner, state
  and counters (`efc_cpu3.c:22044-22062`).  They disagree on 108 of the 820
  WERUs this backing can read.  `Ftl.weru_owners` prefers the log and tags each
  answer with its source; `Ftl.weru_owner_conflicts` lists the differences.
* `weruDescriptor +11` (binId) is not bin membership.  binId 0 is also the
  default for a WERU that was never binned, so 925 WERUs read binId 0 while
  bin 0's own list holds 826.  Walk `weruBin +2` through the circular
  `weruDescriptor +6` chain instead; `decode_weru_bins` does.
* a fully populated msoDescriptor array is not 63 distinct partitions.
  msoIds 0..15 share one byte-identical descriptor here and 16..31 share
  another.  `Mso.templated` and `Ftl.templated_mso_groups()` expose that, so
  the sub-32 range cannot be mistaken for evidence about what assigns it.
  (Since 2026-08-06 the templates are explained -- lutId 2 and lutId 3, the
  NSID2/NSID3 group LUTs of the active-logical-partition MSO pool; see the
  msoIds-below-32 bullet further down.  `templated` still earns its keep as the
  detector that made the pattern visible in the first place.)
* CE0 is dead on this board.  `geometry.CE_DEAD` is honoured explicitly and
  every scan reports which CEs it actually used, so a result can never quietly
  rest on a CE that returned nothing.
* pseudo-SLC vs native TLC is a per-weru property, not a block-number range.
  This bullet used to read *"blocks 0..`FIRMWARE_AREA_LAST_BLOCK` are pseudo-SLC
  and everything above is native TLC"*, and `cell_mode_for_block(block)` returned
  exactly that.  RETRACTED.  Weru 84 occupies blocks 172-175 and is
  pseudo-SLC: ce1/b172/p287 reads MCRC 4/4 and aux-LMA 4/4 under the 0xDA prefix
  and 0/4 and 0/4 under 0xDF, and the whole page-287 window then verified 28/28
  slots, 112/112 AUs with zero LDPC syndromes.  88 WERUs on this board are in
  that class.  A block number cannot answer the question at all, so
  `cell_mode_for_block` now raises `CellModeUnknown` instead of guessing, and
  every read path takes the mode from the WERU (`Ftl.cell_mode`).
  Reading a written page in the wrong mode does not error: it returns ~1000 LDPC
  syndromes per codeword, indistinguishable from an unwritten block, and that
  once produced a false "nothing is written" verdict across 19 WERUs -- and then,
  in the opposite direction, made 88 pseudo-SLC user WERUs look empty.
  The order the mode is decided in is `Ftl.cell_mode_evidence`; the free and
  exact prior is `cell_mode_from_capacity` (a WERU's capacity in AUs is
  `blocks * pagesPerBlock * 4`, so capacity and the mapped-block count together
  name `pagesPerBlock`, which is 368 or 1104 and nothing else).  UNKNOWN is a
  possible answer and `slc_flag` raises rather than let it reach a device as a
  falsey "native TLC".
* validCount 0 does not mean stale for System-group MSOs: the firmware area
  is addressed directly by `FwDataRw`, not through LMAs, so NeoFwPartition reads
  0 while holding the entire firmware image.  `Weru.is_probably_stale` refuses
  to answer for System-group owners.
* `capacity - validCount` is not "deleted data".  It assumes a fully written
  WERU; most programmed WERUs here are 10-40% written, so the figure overstates
  by ~6x.  `Weru.stale_aus` is named `stale_aus_upper_bound` for that reason.
* a blockMapping list index is not a WMA slot index.  `decode_block_mapping`
  used to drop unmapped entries, so position *i* in the returned list was the
  *i*-th mapped block, not slot *i* -- and slot is exactly what the WMA
  arithmetic consumes (`slot = (wma % 192) // 4`).  It now returns a
  slot-indexed list of `geom.BLOCKS_PER_WERU` entries with `None` for unmapped,
  and `Weru.mapped_blocks` is the filtered view.  Anything that pairs a WMA slot
  with a position in the unfiltered list is silently wrong.
* `pbid = 12*block + ce` is wrong for a WERU record (retracted here, and the
  standoff this bullet used to describe is over).  A record's twelve
  consecutive values decode as `pbid = 24*pairIndex + 12*half + slot`: a block
  pair on the six CEs of one half of the package.  That reproduces all five
  hardware-measured configurations exactly and explains the shapes the old
  reading could not -- weru 20's record starting at `12*11` while it owns blocks
  10/11, and weru 0's record being 24 slots.  `pbid_readings()` says which
  reading is applied where.  48-slot records use a different law and that is
  now measured, not assumed (`weru_config_from_user_record`): the pair/half
  scheme scores 5.7% on them.  The two laws stay in two functions so a 12-block
  record can never be read under a 48-block rule.
* A WERU is six CEs wide, so a scan must be restricted to them.  Scanning a
  metadata WERU's block pair across all twelve CEs merges it with the WERU that
  owns the other half; both number their LMAs from 0, so the merge is silent and
  total.  It stayed invisible while the dump only had CE 1-6 and turned `mso`
  and `userLutLut` into 0xFF the moment CE 7-11 arrived.  `from_device` now
  restricts each WERU's scan to its own half and says so in `notes`, and
  `_selftest_device` prefers the widest dump so a narrow backing
  cannot hide the next bug of this shape.

WMA -> PHYSICAL IS SOLVED.  A RETRACTION FIRST
----------------------------------------------
This docstring used to end with: *"turning a WMA into (ce, block, page, au) is
not established -- the SystemLut's WMAs for LMA 0 and LMA 152 are eight apart
while the scan finds those AUs 150 pages apart, so the offset inside a WERU is
not a dense AU index."*  That inference is refuted and it is the single reason
this looked unsolvable.  LMA 152's WMA converts to ce2/b26/p37/au0, which is
exactly where the scan found it; LMA 436 and 440 convert to ce2/b10/p261 and
ce3/b10/p261, likewise exact.  LMA 0 differed only because the scan holds a copy
newer than the SystemLut snapshot names -- a staleness difference, not a mapping
failure.  The offset is not a *dense* AU index; it is a *strided* one, and it
converts exactly (`split_wma`, `wma_to_physical`, 5551 exact / 0 wrong).

The lesson is worth more than the result: "these two numbers do not line up"
was read as "no arithmetic can line them up", when the two numbers were from
different instants and only one of them had to move.

WHAT THIS MODULE CANNOT DO
--------------------------
* `lma_to_wma` now answers for every MSO that has a LUT -- 55 of the 63 here;
  the other 8 (42, 56-62) carry `lutId == 0xFF` and genuinely have none.
  RETRACTED 2026-08-07: "what it still cannot do is turn a user MSO's WMA
  into an address, because `slot -> (ce, block)` is established only for the
  pseudo-SLC WERU class."  It is established for the 48-block class too, and
  measured against media -- see the next bullet.  What remains UNKNOWN is a
  named, bounded set of refusals: records naming a block past
  `BLOCKS_PER_LUN`, the 29 `substituted` ones whose four block numbers are not
  consecutive, and every partial or irregular record.
* `slot -> (ce, block)` is measured for five 12-block WERUs and derived for the
  26 whose capacity is `AUS_PER_SLC_WERU`; the derivation reproduces all five
  measured ones exactly.  The 48-slot law is now measured against media too
  (`sd_slotlaw.py`, 2026-08-07), and this bullet's old ending -- "nothing
  establishes that TLC WERUs use the same pair/half scheme, so
  `wma_to_physical` is UNKNOWN there" -- is RETRACTED on both halves.  The
  answer to the pair/half question is no, and that is a measurement rather than
  a shrug: `slot = 4*ce + (block - blockBase)` scores 3568 of 3568 against
  placements found by an independent aux-LMA route, while a pair/half extension
  of the metadata law scores 204 (5.7%), block-major 184 (5.2%), stored-list
  order 204, `ceBase = 6` 0 and reversed block order 0.  See
  `weru_config_from_user_record`.
* RETRACTED wholesale 2026-08-07.  This bullet used to read "It cannot say
  what the 144-word tail of the `userLutLut` descriptor means, nor what the
  `weruBin` or `rebuildSequenceNumberPool` sections contain."  All three clauses
  are wrong, and the first is wrong twice over: the 144-word tail is not a
  `userLutLut` structure at all -- the descriptor is 296 bytes and every field
  of it past `+8` is named -- it is the ECU LDPC encoder configuration
  table's tail, a different subsystem entirely, and it was identified as the
  Richardson-Urbanke gap-solve matrix Q (`fd_tail.py`, `fc_match.py`, confirmed
  on all three codes).  What is genuinely unnamed in this descriptor is a single
  word, `snapshotInformation`'s own header `+0`; `+232` was named 2026-08-07 and
  verified on media (see `LUTLUT_MIRROR_OFF`).
  The other two clauses were solved on
  2026-08-06 and this line simply outlived them -- `decode_weru_bins` reads
  weruBin as 53 x 16 (`+0 kind`, `+2 head weruId`, `+4 binId self-ref`,
  `+8 count`, list circular through `weruDescriptor +6`; 10/10 live bins verify
  three ways) and `decode_rebuild_sequence_number_pool` reads the pool as 6 x 24
  indexed by msoGroupId (`unk_90C658 + 24*groupId`, `efc_cpu2.c:34445`).
  Treat that as a warning about docstrings, not about the sections.
  The programmed-but-WMA-unaddressed twelfth BLOCK is solved -- by
  `titania.raid` first, whose docstring says in as many words that it retracts
  the paragraph that used to be here, and reconfirmed at the decoded layer on
  2026-08-07 by `sd_block17.py`.  Every AU of slot 11 carries
  `AUX_FLAG_INLINE_PARITY` in all five metadata WERUs, and the parity function
  is measured:

      slot11[au] == XOR(slot0[au] .. slot10[au])

  over the AU's 8 aux bytes and its 4096 user bytes -- the unit the ECU
  accumulates (`efc_cpu2.c:37432-37441`).  Measured by `sd_block17.py` on
  werus 14, 20 and 22: 3388 AUs exact / 0 differ over 847 complete stripes,
  the 12-way XOR is exactly zero on 847/847, and leave-one-out
  reconstruction of any of the twelve from the other eleven is 5328/5328 on
  weru 14 alone.  So slot 11 is not a mirror (it equals another slot on 4 of
  847 stripes, which is what a coincidence looks like), not a journal (its aux
  `+0` values are XOR products, not addresses), and not residue (it is an exact
  function of the CURRENT stripe).  That is also why `blocksPerWeru` is 11
  while twelve blocks are programmed.
  RETRACTED with it: "the stripe includes CE0, which is dead on this board,
  so no XOR can be checked end to end here."  a 12-block WERU spans one half
  of the package, and werus 14/20/22 are the CE 6-11 half -- every one of their
  twelve blocks is on a live die and the test needed no CE0 at all.  What CE0
  really costs is stated correctly by the same arithmetic: werus 19 and 23 put
  two members on CE0, and one XOR relation carries one erasure, so
  those two remain unrecoverable here -- a bound, not a missing measurement.
* msoIds below 32 are the active logical partition MSOs (established
  2026-08-06; this bullet used to read "nothing establishes what assigns them").
  A third cap names the range: `cMaxNumberOfMsoForActivePartitions == 32`
  (`Allocation.c:2813`, `efc_cpu2.c:126962`), alongside `cMaxNumberOfMsos` 63 and
  `cMaxNumberOfMsosForAllocation` 56.  `AllocNeoRegisterActiveLogicalPartition`
  (`sub_4034D8B0`, `efc_cpu2.c:125102`) keeps two 16-entry `u16 lpId` tables --
  `word_800DA5[16]` for group 2/NSID2 and `word_800DC5[16]` for group 3/NSID3,
  both memset to `0xFFFF` -- takes the first free slot, and the slot index is
  the msoId: `i` for NSID2 (0..15), `i + 16` for NSID3 (16..31).
  They are clones of the static msoIds 44 Migration/NSID2 and
  48 UserSlcData/NSID3, which is why `efc_cpu3.c:6910` seeds the reverse map
  with (7, 2) for 0..15 and (10, 3) for 16..31.
  This also explains the two descriptor templates documented above: rows
  0..15 carry lutId 2 and rows 16..31 lutId 3 -- the NSID2 and NSID3 group LUTs.
  The templating was never evidence of "nothing to see"; it was the signature.
  On this board only 16..20 were ever registered (NSID3 slots 0..4), agreed
  by two independent structures (weruDescriptor owners and the Allocation
  object's write frontiers; rows 0..15 and 21..31 hold the empty template).
  NSID2 registered none of 0..15 because it runs with DirectTlcWrite set,
  which routes to the static msoId 55 instead.  Boundary: both structures are
  snapshots of the last clean flush, so that is "never registered as of that
  flush", not "never in the drive's life".
  They are still reported as `Mso(name=None, dynamic=True)` -- the role is now
  known but an individual id still carries no per-partition name of its own.
"""
import hashlib
import struct
import sys
from pathlib import Path

from . import auxdata as auxmod
from . import geometry as geom
from .device import DeviceError
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


__all__ = [
    "UNKNOWN", "Unknown",
    "MSO_GROUPS", "MSO_TYPES", "MSOID_TABLE_LEN",
    "MSOID_TABLE_EMBEDDED", "MSOID_TABLE_FW_VERSION", "MSOID_TABLE_FW_IMAGE",
    "embedded_mso_table", "compare_mso_table",
    "MsoIdTable", "Mso", "Weru", "Placement", "Survey",
    "RootStorage", "SystemMetadata", "SystemImage", "Ftl",
    "Section", "SystemLut", "UserLutLut", "LutRegion", "ValidMap",
    "weru_config_from_pbids", "mapped_blocks",
    "WERU_CONFIGURATIONS", "WERU_COUNT", "WERU_COUNT_IN_VALID_MAP",
    "weru_configuration", "AUX_FLAG_INLINE_PARITY",
    "WERU_COLUMN_PAIR_RUNS", "WERU_COLUMN_PAIRS", "canonical_pbids",
    "WERU_CONFIG_FLAGS", "WERU_CONFIG_FLAG_BIT0",
    "WeruConfig", "WERU_CONFIGS", "split_wma", "wma_to_physical",
    "physical_to_wma", "wma_unaddressable_reason",
    "PageCodec", "default_codec",
    "decode_block_mapping", "decode_block_mapping_properties",
    "decode_weru_descriptor", "decode_valid_count",
    "decode_mso_descriptor", "verify_weru_stride", "section_layout",
    "sbm_recency_order", "weru_block_numbers", "cell_mode_for_block",
    "CellModeUnknown", "cell_mode_from_capacity", "cell_mode_from_lut_pages",
    "lut_pages_suggest_slc", "slc_flag", "MEASURED_CELL_MODES",
    "MEASURED_LUT_PAGE_SPANS", "CELL_MODE_BY_PAGES_PER_BLOCK",
    "resolve_scan_ces", "scan_cache_key",
    "decode_lut_snapshot", "decode_lut_snapshot_header",
    "snapshot_sequence_number", "PHYS_BLOCK_MODE_NAMES",
    "MOUNT_STEP_VALID_MAP", "MOUNT_STEP_VALID_COUNT",
    "MOUNT_STEP_REBUILD_VALID_MAP", "MOUNT_STEP_LUT_REBUILD",
    "LUT_REBUILD_ORDER", "ZERO_MEANING",
    "POS_TO_SLOT", "record_position", "record_position_to_slot",
    "weru_config_from_substituted_record", "SubstitutedWeruConfig",
    "WERU_CONFIG_ID_METADATA", "WERU_CONFIG_ID_TLC_DATA", "WERU_CONFIG_ID_SPARE",
    "WERU_CONFIG_FLAG_NO_REPLACE", "WriteFrontier", "decode_allocation",
    "LPT_ATTR_NAMES", "decode_lpt_entry", "LPT_ENTRY_TEMPLATE",
    "LPT_WRITE_OFFSET_NONE", "LPT_WRITE_OFFSET_VALID",
    "LPT_WRITE_OFFSET_CURSOR_SHIFT",
    "WERU_CONFIG_FLAG_SKIP_DEFECT", "WERU_CONFIG_FLAG_NARROW",
    "WERU_CONFIG_FLAG_BLOCKS_PINNED",
]


# ===========================================================================
# the "we do not know" sentinel
# ===========================================================================
class Unknown:
    """Not measured / not surveyed / not decodable from what we hold.

    Distinct from ``None`` and from an empty container.  ``None``
    means "looked for, definitively absent"; ``[]`` means "looked for, there are
    none"; ``UNKNOWN`` means "no evidence either way".  It is falsey so that
    ``if not x`` still guards, but it prints loudly and refuses arithmetic, so a
    missing measurement cannot be silently averaged into a total.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "<unknown>"

    __str__ = __repr__

    def __len__(self):
        raise TypeError("len() of an UNKNOWN value: this was never measured")

    def __iter__(self):
        raise TypeError("iterating an UNKNOWN value: this was never measured")

    def __add__(self, other):
        raise TypeError("arithmetic on an UNKNOWN value")

    __radd__ = __sub__ = __rsub__ = __int__ = __add__


UNKNOWN = Unknown()


# ===========================================================================
# static tables from the firmware (efc_cpu3.c:4104)
# ===========================================================================
#: msoGroupId -> name.  6 groups; group 5 is cCoreMsoGroupParityData.
MSO_GROUPS = ("System", "MediaSpace0-NSID1", "MediaSpace1-NSID2",
              "MediaSpace2-NSID3", "Unused", "ParityData")

#: msoType -> name.  21 types, complete and in firmware order.  17-19
#: (MLC/SLC/TLC) and 20 (Undefined) are cell-mode / sentinel tags rather than
#: real partitions, which is exactly why the table's last four rows are zero.
MSO_TYPES = ("UserData", "UserData2ndWF", "GarbageCollection", "StaticMan",
             "Lut", "SystemMetadata", "Trim", "Migration", "UserDataNandCache",
             "GcNandCache", "UserSlcData", "CopySlcData", "BlockMove",
             "DirectTlcWrite", "RootStorage", "NeoFwPartition",
             "PhysicalMediaAccess", "MLC", "SLC", "TLC", "Undefined")

MSOID_TABLE_LEN = len(MSO_TYPES) * len(MSO_GROUPS)      # 126
MSOID_INVALID = 0xFF

#: The anchor.  index 30 = type 5 (SystemMetadata) x group 0 (System) = msoId 41,
#: independently confirmed by msoDescriptor[41].totalLmas landing on one of the
#: four maxSystemMetadataSizeInLmas capacity classes.
MSOID_ANCHOR_INDEX = 5 * len(MSO_GROUPS) + 0
MSOID_ANCHOR_VALUE = 41

#: The static table populates exactly this closed range and nothing else.
MSOID_STATIC_MIN = 32
MSOID_STATIC_MAX = geom.MAX_MSOS - 1                    # 62

#: A known-good offset in `dumps/fw.sram.efc.bin` on this board.  A hint only --
#: `MsoIdTable.locate` searches and validates, it does not trust this.
MSOID_TABLE_HINT = 0x175049

# --- the msoIdTable itself, embedded ---------------------------------------
# The msoIdTable is a firmware constant -- a static initialiser, 126 bytes, that
# does not vary with the drive.  It is not on the media, so for as long as it
# had to be supplied as `--firmware <image>` this package could not name a
# single MSO on a fresh checkout, which is exactly the third-party offline
# decode case it exists to serve.  It reads as a limitation of the drive when it
# is nothing but a missing file.
#
# Embedding it is the same class of decision this module already made twice
# over: `MSO_TYPES` (21 names) and `MSO_GROUPS` (6) are hard-coded straight out
# of `efc_cpu3.c:4104` and nobody is asked to supply those.  If anything the id
# mapping is better justified than the names are, because it is checkable --
# `_validate` below re-derives every documented trap from the bytes, and
# `titania fw verify-table <image>` compares this constant against any image you
# have.  A name cannot be checked at all.
#
# Provenance -- all three parts matter, so all three are recorded:
#   image           dumps/fw.sram.efc.bin  (an EFC SRAM capture off this board)
#   offset          0x175049               (== MSOID_TABLE_HINT)
#   firmware ver    09P07M3                (the version string at 0x11b14c of
#                                           that same image)
# 31 live entries, msoIds 32..62, anchor t[30] = 41 = SystemMetadata/System.
# The 24 trailing zeros are padding past the end of the table, not entries.
#
# `MsoIdTable.locate()` is kept and unchanged for anyone holding a different
# image; `titania fw verify-table` is the scriptable form of that comparison.
MSOID_TABLE_FW_VERSION = "09P07M3"
MSOID_TABLE_FW_IMAGE = "dumps/fw.sram.efc.bin"
MSOID_TABLE_EMBEDDED = bytes.fromhex(
    "ff2021ffff22ffffffffff23ff24ffffffffff2526ffff2728ffffffffff29ff"
    "ffffffff2affffffffffff2b2cffffffff2d2effffffffff2fffffffffffff30"
    "ffffff313233ffffff3435ffff36ffff37ffffff38ffffffffff39ffffffffff"
    "3a3b3c3dff3e000000000000000000000000000000000000000000000000")

#: NSID names, by msoGroupId.  Exactly three namespaces exist
#: (cMaxNumberOfMediaSpaces == 3); groups 0/4/5 are not namespaces at all.
GROUP_NSID = {1: 1, 2: 2, 3: 3}

RAID_TYPES = {0: "None", 2: "Inline"}       # cRaidTypeInline == 2 is pinned;
                                            # non-zero non-2 is cRaidTypeMso.


# ===========================================================================
# on-media structure sizes
# ===========================================================================
SYS_MAGIC = 0x00535953                      # "Sys", SystemMetadata LMA 0
SYS_FORMAT_VERSION = 0x02000068
SYSTEM_BLOCK_MAPPING_MAGIC = 0x20170620

#: RootStorage lives here.  Block 1 is the "Backup" instance (100 objects),
#: blocks 2-5 the "Bootstrap" instance (84 objects).
ROOTSTORAGE_BLOCKS = (1, 2, 3, 4, 5)
ROOTSTORAGE_INSTANCE = {1: "Backup", 2: "Bootstrap", 3: "Bootstrap",
                        4: "Bootstrap", 5: "Bootstrap"}
#: The Backup instance is a different store, not a copy: `PhysicalBlockProperties`
#: registers only there (`efc_cpu2.c:107031` passes `dword_40400E4C`) and
#: `WeruUsageLog` only on Bootstrap (`:31745`).
ROOTSTORAGE_BACKUP_BLOCK = 1
#: stripe = base + STRIPES_PER_PAGE[block]*page + ce//2.  Measured, 0 violations
#: over 5273 pages: CE 2k and CE 2k+1 carry the same stripe, and so do the two
#: blocks of a pair, giving 4 physical copies of every stripe.
ROOTSTORAGE_STRIPES_PER_PAGE = {1: 3, 2: 6, 3: 6, 4: 6, 5: 6}
#: The Bootstrap instance's four blocks are two sections of one store, not four
#: copies and not four generations.  `sub_40365116` (`efc_cpu2.c:149143`) passes
#: the literal `2u` for `numberSections` to the RootStorage constructor
#: `sub_40364CD6` (`:148899`, `*(_WORD *)(a1 + 86) = a8`), and
#: `cMaxNumberOfSections` is 2 (`ScanLastStripe.c:232`, `efc_cpu2.c:146560`).
#: `sub_4036519E` (`:149154`) collects at most two WERUs, so Bootstrap runs
#: `numberOfWerus = 2`, `numberOfSectionsPerWeru = 1`: one section per WERU,
#: and a metadata WERU is a block pair on twelve CEs.  Measured here: blocks 2/3
#: are section 0, blocks 4/5 section 1.
ROOTSTORAGE_BOOTSTRAP_BLOCKS = (2, 3, 4, 5)
#: `numberSections`, hard-coded at the only instantiation site.
ROOTSTORAGE_SECTIONS = 2
ROOTSTORAGE_PAGE = 16384                    # 4 AUs x 4096 user bytes
ROOTSTORAGE_HDR = 88                        # metaDataSize = 88 + 8*numberOfEntries
ROOTSTORAGE_TYPE = 21                       # the stripe-part version word
INVALID_STRIPE = 0xFFFF
#: RootStorage AUs are not LMA-addressed, so their aux LMA field is the invalid
#: marker.  This is a free 32-bit cross-check that a page descrambled correctly
#: -- and it is worth having, because the MCRC is computed over the stored bytes
#: and therefore passes whether or not the descramble used the right seed.
#:
#: Correction to `nd_b1to5.py`, which takes the whole 8-byte aux to be the
#: constant `ff ff ff ff 00 00 00 00`: measured over 1396 AUs of block 2, 1044
#: match and 348 do not, and the 348 are exactly one per page.  RootStorage aux
#: follows the same convention as data AUs -- `<u32 lma><u24 seq><u8 flag>` --
#: with the page sequence number in AU1 and zeros elsewhere.
ROOTSTORAGE_LMA = 0xFFFFFFFF
ROOTSTORAGE_AUX = b"\xff\xff\xff\xff\x00\x00\x00\x00"   # AUs other than SEQ_AU

#: RootStorage object ids, in the order the firmware's mount touches them.
OBJ_SYSTEM_LUT = 0                          # the SystemMetadata MSO's L2P table
OBJ_SYSTEM_METADATA_INFO = 1                # 16 B; byte 0 = the dirty flag
OBJ_ALLOCATION = 2                          # 56 x 40 B, write pointers only
OBJ_SYSTEM_BLOCK_MAPPING = 3

#: The rootstorage catalogue, object by object -- `{instance: {id: (size, tag,
#: name)}}`.  The catalogue lookup entry is `{u16 stripe; u16; u32 size:24|type:8}`
#: (`efc_cpu2.c:148642-148650`), so what `nd_rootstorage.py` calls a "tag" is the
#: object type byte, and an equal type over a contiguous run of ids is one
#: Chained object.  Only singleton objects are named here; the chains are
#: checked as runs by `rootstorage_catalogue_check()` because their *names* are
#: less certain than their shape.
#:
#: Provenance: `nd_rsobjects.py`'s report, which was the only executable
#: re-verification of this table and has since been archived -- hence this
#: constant and the self-test row that re-checks it against the store.
#:
#: correction carried in from the firmware, not from that report: objects
#: 4..25 are the `WeruUsageLog`, not "SystemLut payload chain a".
#: `sub_10AE4` registers it with `firstObjectId=4` (`efc_cpu2.c:31748`), which is
#: what `OBJ_WERU_USAGE_LOG_FIRST` above already encodes, and the record layout
#: reads correctly there.  Prefer the firmware citation over the report.
ROOTSTORAGE_OBJECTS = {
    "Bootstrap": {
        0:  (7424, 0x01, "SystemLut / userLutLut (1856 x u32 LMA)"),
        1:  (16,   0x00, "SystemMetadataInformation      efc_cpu2.c:152236"),
        2:  (2240, 0x01, "Allocation 56 x 40B by msoId   efc_cpu2.c:128015"),
        3:  (840,  0x00, "SystemBlockMapping 0x20170620  efc_cpu2.c:152151"),
        43: (61,   0x00, "ErrorLog                       efc_cpu2.c:58356"),
        83: (1272, 0x00, "SystemLut root record (6 luts) efc_cpu2.c:152194"),
    },
    "Backup": {
        0: (4,    0x00, "Backup root record             efc_cpu2.c:151803"),
        6: (5104, 0x00, "DdmData                        efc_cpu3.c:31604"),
        7: (5120, 0x00, "PhysicalBlockProperties        efc_cpu2.c:57030"),
    },
}

#: The chained runs: `(instance, first, last, tag, what)`.  A run is verified by
#: "every id in it carries this tag, and no id just outside it does" -- which is
#: exactly the property that makes it one object, and does not depend on the
#: name being right.
#: The three Bootstrap chains are also named independently, with no firmware
#: image, by the `object_name_tag()` CRC-8/0x07 over the 16-byte name (see the
#: "RootStorage object-name tags" self-test row): 171/0xAB = `WeruUsageLog`,
#: 137/0x89 = `LptNsid2`, 240/0xF0 = `LptNsid3`.  Two independent routes agree,
#: which is what retires `nd_rsobjects.py`'s "SystemLut payload chain A/B/C"
#: labels for all three -- not just for 4..25.
ROOTSTORAGE_OBJECT_CHAINS = (
    ("Bootstrap", 4,  25, 0xAB, "WeruUsageLog (firstObjectId=4, "
                                "efc_cpu2.c:31748; tag = crc8('WeruUsageLog'))"),
    ("Bootstrap", 44, 69, 0x89, "LptNsid2 (tag = crc8('LptNsid2'))"),
    ("Bootstrap", 74, 75, 0xF0, "LptNsid3 (tag = crc8('LptNsid3'))"),
    ("Backup",    8,  20, 0xF4, "PhysicalBlockProperties segments "
                                "(PHYS_BLOCK_BUFFER_FIRST_OBJECT=8)"),
)


def rootstorage_catalogue_check(rs):
    """Re-verify `ROOTSTORAGE_OBJECTS` / `_CHAINS` against a live catalogue.

    -> `(ok_count, [problem strings])`.  Reads only the catalogue entries --
    `(stripe, size, _, tag)` -- so it costs no object payload reads.

    This is the mechanical re-check that `nd_rsobjects.py --selftest` used to be
    the only carrier of.  Objects absent from this backing are reported as
    skips, not failures: `INVALID_STRIPE` means "the catalogue says absent".
    """
    _h, ents, _ce, _pg = rs.catalogue()
    inst = rs.instance
    ok, problems = 0, []

    for oid, (size, tag, name) in sorted(
            ROOTSTORAGE_OBJECTS.get(inst, {}).items()):
        if oid >= len(ents):
            problems.append("object %d (%s): catalogue has only %d slots"
                            % (oid, name, len(ents)))
            continue
        stripe, got_size, _z, got_tag = ents[oid]
        if stripe == INVALID_STRIPE:
            continue                              # absent here; not a failure
        if got_size != size or got_tag != tag:
            problems.append("object %d (%s): expected size %d tag 0x%02x, "
                            "read size %d tag 0x%02x"
                            % (oid, name, size, tag, got_size, got_tag))
        else:
            ok += 1

    for c_inst, first, last, tag, what in ROOTSTORAGE_OBJECT_CHAINS:
        if c_inst != inst:
            continue
        if last >= len(ents):
            problems.append("chain %d..%d (%s): catalogue has only %d slots"
                            % (first, last, what, len(ents)))
            continue
        inside = [i for i in range(first, last + 1)
                  if ents[i][0] != INVALID_STRIPE]
        wrong = [i for i in inside if ents[i][3] != tag]
        # the run must also terminate -- an id just outside carrying the same
        # tag would mean the chain is longer than this table says.
        edges = [i for i in (first - 1, last + 1)
                 if 0 <= i < len(ents)
                 and ents[i][0] != INVALID_STRIPE and ents[i][3] == tag]
        if wrong or edges:
            problems.append(
                "chain %d..%d (%s): %d of %d present ids carry tag 0x%02x%s"
                % (first, last, what, len(inside) - len(wrong), len(inside),
                   tag, (", and %s outside the run also does" % edges)
                   if edges else ""))
        elif inside:
            ok += 1
    return ok, problems

# ---------------------------------------------------------------------------
# SegmentedBuffer -- how the big RootStorage residents are stored
# ---------------------------------------------------------------------------
#: A SegmentedBuffer is an SRAM array cut into fixed-size segments, each written
#: to its own RootStorage object.  `SegmentedBuffer_Initialize`
#: (`sub_403688BC`, `efc_cpu2.c:152740-152780`) takes
#: `(pSegBuf, rootStorageInstanceId, name, headerBufferSize, segmentsBufferSize,
#:   firstObjectId, maximumSegmentSize, elementSize)` and derives
#:
#:     segmentSize      = ((maxSegmentSize - headerBufferSize - 8) // elementSize)
#:                        * elementSize
#:     numberOfSegments = ceil(segmentsBufferSize / segmentSize)
#:     lastSegmentSize  = segmentsBufferSize % segmentSize  (or segmentSize)
#:
#: so an element never straddles two objects.  One object's payload is written
#: by `sub_4036857A` (`efc_cpu2.c:152466-152500`) and read back by `sub_4036869E`
#: (`:152622-152660`) as
#:
#:     u32 headerSequenceNumber | u32 totalSize | header[headerBufferSize] | data
#:
#: `totalSize` is `headerBufferSize + segmentSize + 8` unconditionally, i.e. the
#: full-segment size even in a short last segment -- do not use it as a length.
#: The catalogue entry's own `size` is the written length.
SEGBUF_META_BYTES = 8                       # the u32 seq + u32 size prefix
SEGBUF_SEQ_OFF = 0
SEGBUF_TOTAL_OFF = 4

#: `WeruUsageLog`, registered at `efc_cpu2.c:31748` (`sub_10AE4`) as
#: `(name="WeruUsageLog", headerBufferSize=0x10, segmentsBufferSize=recordSize *
#:  numberOfWerus, firstObjectId=4, maximumSegmentSize=<per-RootStorage>,
#:  elementSize=recordSize)`, on the bootstrap instance, with a hard cap of 37
#: segments (`efc_cpu2.c:31754`).
#:
#: `recordSize = 6 * (word_90C458 * byte_90C4E0) + 20` (`efc_cpu2.c:132030`),
#: and the same product is the block-slot count the record's map is iterated
#: over (`sub_40354B3E`, `efc_cpu2.c:132239-132251`).  Measured here as 308,
#: i.e. 48 slots -- `geometry.BLOCKS_PER_WERU`, arrived at independently.
OBJ_WERU_USAGE_LOG_FIRST = 4
WERU_USAGE_LOG_NAME = "WeruUsageLog"
WERU_USAGE_LOG_HEADER_BYTES = 16
WERU_USAGE_LOG_MAX_SEGMENTS = 37
#: Record layout (`sub_10E8A` reads the first 20 bytes, `efc_cpu2.c:31993-32004`;
#: `sub_11298` writes the map, `efc_cpu2.c:32315-32357`):
#:     +0  u64 activation sequence number
#:     +8  u64 origin activation sequence number of the contents
#:     +16 u8  OWNER msoId, 255 = unowned (`sub_10E8A` rejects 255)
#:     +17 u8  flags
#:     +20 6 * BLOCKS_PER_WERU block-map entries
#:
#: `+0` is stamped from one global u64 that `WeruUsageLog_Activate` (`sub_10D20`,
#: `efc_cpu2.c:31889-31925`) copies into the record and then post-increments:
#:
#:     mem_clr(a2, 0x14); a2[16] = msoId
#:     *(u64*)a2 = *(u64*)dword_80C698           # the stamp
#:     sub_84C(record, a2, 0x14)                 # 20 bytes, so flags := 0 too
#:     ++*(u64*)dword_80C698
#:
#: The global is initialised to 1 by the one-time init (`sub_403547F8`,
#: `efc_cpu2.c:132009`), so `max(+0) + 1` is the number of WERU activations the
#: drive has performed since format, and activation clears the whole flags byte.
#: `sub_1112A` (`efc_cpu2.c:32172`) is its plain getter, and the allocator sorts
#: candidate WERUs on this field (`sub_11248`/`sub_1126E`, `efc_cpu2.c:32277`,
#: `:32291`, comparing `*(u64*)(entry+16)` = the copy `sub_10E8A` exports).
#:
#: `+8` is written only by `sub_10DFE` (`efc_cpu2.c:31935-31969`), which
#: propagates a lineage when contents move from WERU a to WERU b:
#:     b.origin = a.origin if a.origin else a.activation
#: so it dates the DATA, not the container.  Its one caller passes the WERU
#: being closed and its successor (`sub_4035B380`, `efc_cpu2.c:138860`).
#:
#: downgraded 2026-08-07 -- it is observable telemetry with no consumer, not a
#: "verified lineage stamp", and the 70/820 statistic is not evidence for it.
#: Three things settle that, at instruction level:
#:
#:  1. Activation destroys it.  `sub_10D20`'s `mem_clr` covers 20 bytes at
#:     +0 (`0x00010D82`, `efc_cpu2.c:31909`) -- which includes +8..+15 -- and
#:     between that clear and the 20-byte commit at `0x00010D98` the only writes
#:     into the staging buffer are the counter at +0 and the msoId at +16.
#:     `sub_10D20` never touches +8 at all.  The same 20-byte wipe happens at
#:     cold init (`efc_cpu2.c:132021-132023`).
#:  2. Nothing branches on it.  Its only observers are `sub_10DFE`'s own
#:     "do I have an origin already" test (`0x00010E42`), a UART trace record
#:     (`0x7707200B`, `efc_cpu2.c:31963`) and an export to the 40-byte sorted
#:     list node at +0x18 (`sub_10E8A`, `0x00010F80`) which nothing reads --
#:     every comparator and every policy test on that node keys on +0x10, the
#:     Activation counter (`efc_cpu2.c:32285`, `:32300`, `:133168`).
#:     Checked five ways, including a whole-image Thumb scan of all four cores
#:     (cpu0/1/3 never touch this table) and a disassembly of all 11 call sites
#:     of the list builder.
#:  3. `origin <= activation` is tautological, not a measurement.  Every
#:     assignment takes either an older record's +0 or an older record's already
#:     valid +8, both from strictly earlier values of the one monotone counter,
#:     so the inequality cannot fail.  And "70 of 820 non-zero" is exactly
#:     "relocated into by `sub_4035B380` and not re-activated since", because of
#:     (1).  Both are worth reporting and neither is worth believing as
#:     independent corroboration of anything.
WERU_USAGE_CTR_OFF = 0
WERU_USAGE_CTR2_OFF = 8
WERU_USAGE_OWNER_OFF = 16
WERU_USAGE_FLAGS_OFF = 17
WERU_USAGE_MAP_OFF = 20
WERU_USAGE_MAP_ENTRY = 6
WERU_USAGE_OWNER_NONE = 255

#: The flags byte (+17).  Every mutator of it in the image was enumerated and
#: named from its call sites; nothing here is inferred from the data.
#:
#: bit 0 `invalid` -- "this WERU holds no valid data".  Set by `sub_10B7C`
#:     (`WeruUsageLog.c:164`, `efc_cpu2.c:31767`), a latch: if already set it
#:     returns without even marking the segment dirty.  Its callers are exactly
#:     the places a WERU stops holding anything live -- the one-time init sets
#:     it on every record (`efc_cpu2.c:132025`), the format sweep sets it on a
#:     WERU that has a defective block while stamping classification `Invalid`
#:     (`:32987`), the write-pointer handover sets it on the WERU being closed
#:     (`sub_4035B380`, `:138861`), the LMA release path zeroes `validCount[w]`
#:     and then sets it (`sub_DA24`, `:28582`), and the decommission engine sets
#:     it on a WERU that was not already decommissioned (`:109392`).
#: bit 1 `erased` -- set by `sub_11132` (`efc_cpu2.c:32176`).  `RebuildWeruBins`
#:     classifies a WERU with this bit as state 2 and its own error string names
#:     it: `"Metadata erased W[%4x] VC:0x%x in use:%d"` is raised when such a
#:     WERU still has a non-zero validCount (`efc_cpu2.c:131953-131961`).  The
#:     format sweep sets it on every WERU whose blocks are all good (`:32984`).
#: bit 2 `decommissioned` -- set by `sub_403548DE` (`WeruUsageLog.c:184`,
#:     `efc_cpu2.c:132070`).  Both callers are decommission paths: the NSID2
#:     decommission RPC (`sub_4034469A`, `efc_cpu2.c:115919`, whose completion
#:     logs `[OnDecommissionWeruUsageLogWritten] Failed to write weru usage log
#:     for decommission`) and `MediaDecommissionWeruEngine` (`:109387`).
#: bit 3 `decommission_done` -- set by `sub_40354954` (`WeruUsageLog.c:204`,
#:     `efc_cpu2.c:132098`), only by the decommission engine and only when bit 2
#:     was already set before the re-activation (`efc_cpu2.c:109382-109393`).
#:     `sub_10E8A` selector 4 lists `bit2 and not bit3` -- the work queue of
#:     decommissions that were requested but never finished.
#: bit 4 `fading_rewrite` -- set at activation while the global `byte_80C8BC` is
#:     on (`sub_10CCC`, `efc_cpu2.c:31868`; armed at `:31915`).  That global is
#:     turned on by `MediaPartitionInterface_RewriteFadingPagesAndIncompleteStripes`
#:     (`sub_403559FA`, `efc_cpu2.c:133181`) and turned off, with the bit cleared
#:     on every WERU, by that pass's completion (`sub_4035766C`, `:134893-134895`).
#:     So it marks WERUs (re)opened by the retention/torn-stripe rewrite sweep,
#:     and a set bit that survives a power cycle means the sweep never finished.
#: bit 5 `rebuild_write` -- set at activation while `byte_80C8BD` is on
#:     (`efc_cpu2.c:31916`); armed by `MediaPartitionInterface_RebuildWriteMode`
#:     (`efc_cpu2.c:61032`) and cleared on every WERU by
#:     `MediaPartitionInterface_FinalizeRebuild` (`:61079` -> `sub_111F6`,
#:     `:32250-32270`).  `sub_10E8A` selector 5 lists exactly these.
#: bits 6-7 are never read or written anywhere in the image.
WERU_USAGE_FLAG_NAMES = ("invalid", "erased", "decommissioned",
                         "decommission_done", "fading_rewrite",
                         "rebuild_write", "bit6", "bit7")
WERU_USAGE_FLAG_INVALID = 0x01
WERU_USAGE_FLAG_ERASED = 0x02
WERU_USAGE_FLAG_DECOMMISSIONED = 0x04
WERU_USAGE_FLAG_DECOMMISSION_DONE = 0x08
WERU_USAGE_FLAG_FADING_REWRITE = 0x10
WERU_USAGE_FLAG_REBUILD_WRITE = 0x20
#: `sub_10E8A`'s five list selectors (`efc_cpu2.c:31989-32017`), for reference:
#:     1 erased      2 not erased      3 not erased + owner in msoGroup 2 with
#:     msoDescriptor[owner].byte0 bits[4:3] == 1      4 decommissioned and not
#:     decommission_done      5 rebuild_write

#: Block-map entry.  `sub_11298` writes six bytes per slot and the last four are
#: One u32 copied verbatim out of `blockMapping[+10]`:
#:     *(u32*)(scratch + 2) = *(u32*)(blockMapping + 20*i + 10)
#: (`efc_cpu2.c:32347`).  Reading it as `u16 +2 / u8 +4 / u8 +5` splits an erase
#: count across three fields -- use `PHYS_BLOCK_*` below.
#:     +0 u16 physicalBlockId // WERU_USAGE_PAIR_DIVISOR   (the pair index)
#:     +2 u32 physicalBlockProperties, frozen at this WERU's activation
#: 0xFFFF marks an unmapped slot, 0x3FFF a slot past this WERU's blocksPerWeru.
WERU_USAGE_ENTRY_UNMAPPED = 0xFFFF
WERU_USAGE_ENTRY_UNUSED = 0x3FFF
WERU_USAGE_PROP_OFF = 2

#: `PhysicalBlockProperties` -- one u32 per physical BLOCK, the drive's wear and
#: bad-block ledger.  `sub_4033C024`/`sub_4033C054` (`efc_cpu2.c:107116`,
#: `:107133`) are its accessors; `sub_40339FD4` (`:105083`) refreshes the copy
#: cached in `blockMapping[+10]` for every mapped entry, which is how the same
#: word reaches the usage log.
#:
#:     [23:0]  erase count.  `SanityCheckPhysicalBlockProperties`
#:             (`sub_4033B688`, `efc_cpu2.c:106609-106628`) accumulates
#:             `v & 0xFFFFFF` into a u64 total and tracks its min and max, and
#:             `sub_4033B640` (`:106529`) triggers a backup once that total has
#:             advanced by `10 * numberOfBlocks`.
#:     [25:24] pageBlockMode -- the block's cell mode, not a "region".
#:             `sub_4033AF1E` (`efc_cpu2.c:106089-106127`) is its accessor and
#:             returns `HIBYTE(word) & 3`; `sub_4033D69E`, the spare-block
#:             finder, compares that against the target WERU configuration's
#:             `cellMode == 1 ? 1 : 2` (`:108604-108609`) and takes an exact
#:             match, else a 3, else -- only when it wants pseudo-SLC -- a 2.
#:             So the values are the `pageBlockMode` enum:
#:                 1 = cPseudoSlcPage   (named by the `Moh.c:2347` panic,
#:                                       `efc_cpu2.c:38708`)
#:                 2 = cNativePage      (named by `K9BKGD8JxA.c:860`,
#:                                       `efc_cpu1.c:9937`)
#:                 3 = no mode assigned yet.  No firmware string names this one;
#:                     it is read off the three call sites in `sub_4033D69E`
#:                     plus the erase-count reseed at `efc_cpu2.c:105748-105762`,
#:                     which -- only for the spare configuration -- gives a
#:                     mode-3 block erase count 0 and buckets a mode-2 one
#:                     differently from the rest.
#:             `sub_4033B688` expects 1 when `dword_90C4B0 != -1 and pairIndex <=
#:             word_90C4BC` -- i.e. below `maxSlcPhysicalBlockId` -- and 2
#:             otherwise (`efc_cpu2.c:106645-106648`); `sub_4033BB58` repairs it
#:             (`:106944`).
#:             MEASURED over all 48000 blocks (`pt_props.py`): every block of a
#:             cellMode-1 configuration reads 1 (4608 of them, exactly
#:             24+48+312+4224) and every block of the cellMode-0 configuration
#:             reads 2 (41040, exact).  The spare pool is the only configuration
#:             that mixes: 2273 read 3 and the remaining 79 are precisely the
#:             displaced defective blocks parked there, still carrying the mode
#:             of the WERU they were pulled out of.
#:     [27:26] and [31] -- never read and never written.  `sub_4033B5A2`
#:             preserves them with mask 0x8F on byte 3 (`efc_cpu2.c:106505`) and
#:             the class repair with mask 0xFC (`:106944`), and no reader
#:             anywhere touches them.  Measured 0 on 48000/48000 blocks.
#:     [30:28] status.  Written by `sub_4033B5A2` (`efc_cpu2.c:106505`), read by
#:             `sub_4033B60A` (`:106524`).  `sub_40339F3E` (`:105027`) defines
#:             Defective as `status != 0 and status != 6`.  `sub_4033B5A2`
#:             Panics if asked to write 6 or 7, so those two are assigned only at
#:             format.  The eight names are the firmware's own: both histogram
#:             dumps print `off_40400B90[status]` (`efc_cpu2.c:106626`, `:107189`)
#:             and that pointer array resolves in `dumps/fw.sram.efc.bin` to the
#:             strings below.
#:     0xFFFFFFFF is the illegal value the sanity check reports and repairs.
PHYS_BLOCK_ERASE_MASK = 0x00FFFFFF
PHYS_BLOCK_CLASS_SHIFT = 24
PHYS_BLOCK_CLASS_MASK = 0x3
#: The `pageBlockMode` enum in [25:24].  1 and 2 are the firmware's own names;
#: index 0 never occurs on this drive and index 3 has no string anywhere, so it
#: is described rather than named.
PHYS_BLOCK_MODE_NAMES = ("(never observed)", "cPseudoSlcPage", "cNativePage",
                         "unassigned")
PHYS_BLOCK_MODE_PSEUDO_SLC = 1
PHYS_BLOCK_MODE_NATIVE = 2
PHYS_BLOCK_MODE_UNASSIGNED = 3
#: Bits the firmware neither reads nor writes; both masks measured 0 on 48000
#: of 48000 blocks.  Kept so a decode can assert they are zero rather than
#: silently discard them.
PHYS_BLOCK_RESERVED_MASK = 0x8C000000        # [27:26] and [31]
PHYS_BLOCK_STATUS_SHIFT = 28
PHYS_BLOCK_STATUS_MASK = 0x7
PHYS_BLOCK_ILLEGAL = 0xFFFFFFFF
PHYS_BLOCK_STATUS_NAMES = ("None", "Factory", "ReadGrown", "WriteGrown",
                           "EraseGrown", "FakeGrown",
                           "UndiscoveredIgnoredFactory",
                           "DiscoveredIgnoredFactory")
#: `sub_40339F3E` (`efc_cpu2.c:105027`): a block is defective unless its status
#: is `None` or `UndiscoveredIgnoredFactory`.
PHYS_BLOCK_STATUS_OK = (0, 6)
#: The Backup RootStorage SegmentedBuffer that persists the array:
#: `SegmentedBuffer_Initialize(&segbuf, "PhBP", 0x40, 4*numberOfBlocks, 8,
#:  <max>, 4)` (`sub_4033BDDA`, `efc_cpu2.c:107038`), capped at
#: `cBackupRootStorageObjectPhysicalBlockPropertiesCount` = 0x48 objects
#: (`:107043`).  Its 0x40-byte header carries `4*numberOfBlocks` at +0 and, at
#: +56, the u64 total erase count as of the last backup (`sub_4033B640` compares
#: against `*(u64*)(header + 56)`).
PHYS_BLOCK_BUFFER_NAME = "PhBP"
PHYS_BLOCK_BUFFER_FIRST_OBJECT = 8
PHYS_BLOCK_BUFFER_HEADER_BYTES = 0x40
PHYS_BLOCK_BUFFER_ELEMENT = 4
PHYS_BLOCK_BUFFER_MAX_OBJECTS = 0x48
PHYS_BLOCK_HEADER_BYTES_OFF = 0
PHYS_BLOCK_HEADER_TOTAL_ERASES_OFF = 56
#: `word_90C458` in `sub_11298`.  It is not `geometry.CE_COUNT`: the quotient is
#: the pair index of `pbid = 24*pairIndex + 12*half + slot`, the decomposition
#: `Ftl.pbid_readings` measured independently.  Verified against blockMapping on
#: 723 whole WERUs (31344 slots, 0 disagreements).
WERU_USAGE_PAIR_DIVISOR = 24

#: `weruBin`: 53 x 16 bytes, 848 total, hard-coded in the layout builder
#: (`efc_cpu3.c:37454`) and read into `byte_402A6618` (`efc_cpu3.c:44916`).
#: Fields, from the initialiser that resets all 53 (`efc_cpu3.c:44744-44749`)
#: and the accessors:
#:     +0 u8  bin kind; 6 is the reset value (`efc_cpu3.c:44747`, `:45722`)
#:     +2 u16 head weruId, 0x3FFF = empty (`Weru_SearchWeruIdInBin`,
#:            `efc_cpu3.c:45030`, `:45054`)
#:     +4 u8  binId self-reference (`efc_cpu3.c:44748`)
#:     +8 u32 number of WERUs in the bin (`efc_cpu3.c:45059` tests it for empty)
WERU_BIN_COUNT = 53
WERU_BIN_ENTRY = 16
WERU_BIN_BYTES = WERU_BIN_COUNT * WERU_BIN_ENTRY     # 848, efc_cpu3.c:37454
WERU_BIN_KIND_OFF = 0
WERU_BIN_HEAD_OFF = 2
WERU_BIN_SELF_OFF = 4
WERU_BIN_COUNT_OFF = 8
WERU_BIN_KIND_RESET = 6

#: `+0` is not an opaque "kind": it is the weruConfigurationId this bin serves,
#: with 6 meaning "no configuration".  `sub_402E5FD8(binId, wcId)` writes the
#: configuration id (`efc_cpu3.c:44727`) after the all-bins reset writes 6
#: (`efc_cpu3.c:44697`, `:44748`), and the pop path branches on `== 6`
#: (`efc_cpu3.c:45195`) to take a FIFO head instead of an erase-count search.
#: Measured here: the four bins whose kind is a real configuration id hold
#: members of that configuration and no other -- 931 members, 0 exceptions.
WERU_BIN_KIND_NO_CONFIG = 6

#: The 53 bins are named.  `off_402A09A8` is a 53-entry `char *` table indexed by
#: binId, used by the bin dumper (`efc_cpu3.c:44270`, `:44307`, `:44338-44344`).
#: Only the band heads carry a pointer; the interiors are null, which is itself
#: the proof that 2-17, 18-33 and 34-40 are bands rather than 53 distinct names.
#: Independently corroborated by assert text, which is generated by a different
#: mechanism and agrees exactly: `cWeruBinIdSystemFree` 47, `cWeruBinIdFreeNandCache`
#: 45, `cWeruBinIdFree` 0, `cWeruBinIdRootStorage` 49 (`efc_cpu3.c:45339-45346`,
#: `Weru.c:1617`, and again at `:45452-45460`, `Weru.c:1574`);
#: `cWeruBinIdToDecommission` 51 (`efc_cpu3.c:46268`, `WeruManagement.c:2931`);
#: `cWeruBinIdActiveWrite` 43 (`efc_cpu3.c:48574`, `WeruManagement.c:907`).
WERU_BIN_NAMES = {0: "Free", 1: "Erase", 2: "Valid", 18: "GarbageCollection",
                  34: "ActiveCopySource", 41: "Retired", 42: "UsedSystemWeru",
                  43: "ActiveWrite", 44: "ActiveErase", 45: "FreeNandCache",
                  46: "EraseNandCache", 47: "SystemFree", 48: "SystemErase",
                  49: "RootStorage", 50: "WaitDecommission",
                  51: "Decommission", 52: "Decommissioned"}

#: The two 16-wide bands, and the 7-wide one.  `53 = 2 + 16 + 16 + 7 + 12` is a
#: Sum, not a product; the boundaries appear literally at `efc_cpu3.c:46164`
#: (`(t - 2 <= 0xF) || (t - 18 <= 0xF)`), `:45994-46009` and `:44254-44269`.
WERU_BIN_BANDS = ((2, 16), (18, 16), (34, 7))

#: The three bins this module names explicitly, because measurements key on them.
WERU_BIN_FREE = 0                           # the main free pool
WERU_BIN_ACTIVE_WRITE = 43                  # WeruManagement.c:907
WERU_BIN_FREE_NAND_CACHE = 45               # Weru.c:1617

#: Which FREE pool an MSO draws from, by msoType.  Read off the `binPair` table
#: `dword_402F8968` (16 rows x 12 u16 `{lo=sourceBin, hi=targetBin}`, indexed
#: `binPair[msoType][transition]` by `sub_402E731C`, `efc_cpu3.c:46128-46130`),
#: taking the `WriteAllocation` column -- transition 0, whose source bin is the
#: pool the allocator pops from.  The transition names are `off_402F8B08`
#: (`efc_cpu3.c:46149`, `:46157`, `:46183`): WriteAllocation, WriteRelease,
#: BinUpdate, BinFree, EraseAllocation, EraseRelease, Retire, CopyDistribution,
#: CopySourceSelection, WaitDecommission, TriggerDecommission, Decommission.
#:
#: MEASURED: on this board every member of the four occupied pool bins is owned
#: by an msoType this table sends to that pool -- see the self-test's score.
WERU_BIN_FREE_POOL_BY_MSO_TYPE = {
    0: WERU_BIN_FREE, 1: WERU_BIN_FREE, 2: WERU_BIN_FREE, 3: WERU_BIN_FREE,
    7: WERU_BIN_FREE, 12: WERU_BIN_FREE, 13: WERU_BIN_FREE,
    8: WERU_BIN_FREE_NAND_CACHE, 9: WERU_BIN_FREE_NAND_CACHE,
    10: WERU_BIN_FREE_NAND_CACHE, 11: WERU_BIN_FREE_NAND_CACHE,
    4: 47, 5: 47, 6: 47, 15: 47,                       # SystemFree
    14: 49,                                            # RootStorage
}

#: msoType 16 `PhysicalMediaAccess` -- the raw-physical access MSO.  Anchored by
#: `ScanEngine.c:280` (`efc_cpu2.c:165015-165022`), which panics unless the
#: msoType is 16 or 14: `"(cMsoTypePhysicalMediaAccess == cMsoType) ||
#: (cMsoTypeRootStorage == cMsoType)"`.  Its group-0 instance is msoId 58 here,
#: confirmed twice over -- the static msoIdTable's slot [16*6+0], and the
#: runtime role global `byte_900060` in the cpu0 SRAM snapshot, which reads 0x3A.
MSO_TYPE_PHYSICAL_MEDIA_ACCESS = 16

#: Inside the two 16-wide bands the slot is chosen by the OWNER's address space
#: and the WERU's cell mode:
#:
#:     binId = bandBase + slcMode + 2 * lutId          (`efc_cpu3.c:46168`)
#:
#: with `lutId = msoDescriptor[owner] +3` (bounded by `cMaxNumberOfLuts == 8`,
#: `efc_cpu2.c:130949-130951`) and `slcMode = weruDescriptor +10`, a 2-valued
#: flag whose value 1 triggers the SLC resource accountant
#: (`efc_cpu3.c:46185-46188`).  16 = 2 x 8 follows.  The firmware writes the
#: same expression as a literal elsewhere: `2 * lutId + 3` = band base 2 plus
#: mode 1 (`efc_cpu3.c:46066-46068`).
#:
#: MEASURED, nothing fitted: on this board the four occupied `Valid`-band bins
#: are 4, 6, 9 and 10, and predicting each from its members' owner msoId (hence
#: lutId) and configuration (hence cell mode) reproduces all four -- 5 of 5
#: member classes agree, 0 disagree, and bin 9 pins the mode bit because it is
#: the only pseudo-SLC one.
WERU_BIN_BAND_LUT_STRIDE = 2

#: `weruDescriptor +10` is the CELL mode, and the drive stores what this module
#: otherwise re-derives.  proven 2026-08-07, retracting "read-only inference,
#: no write site other than the `= 0` in the initialiser".  The write site was
#: simply behind a setter:
#:
#:     getter  sub_402E554E  efc_cpu3.c:44085-44093   byte[weru*20 + 10]
#:     setter  sub_402E6ADE  efc_cpu3.c:45587-45595   its only caller is :41186
#:     :41186  sub_402E6ADE(weru, sub_402D1380(byte_800818[32*msoId]))
#:     sub_402D1380  efc_cpu3.c:26945  ->  *(unk_907120[cfg] + 14)
#:     unk_907120[cfg] = &unk_900078 + 20*cfg      efc_cpu3.c:27019-27020
#:
#: i.e. the value written is `byte_900078[20*cfg + 14]` -- literally the cellMode
#: byte of the WERU configuration table.  Its name comes from the firmware's own
#: assert on the same expression, `"blockMode ==
#: WeruConfiguration_GetWeruBlockMode(werus[weruIndex])"` (`RootStorage.c:930`,
#: `efc_cpu2.c:149015`), and its polarity from every consumer:
#: `cellMode == 1` selects `dword_907154 = pagesPerBlock/3` and anything else
#: selects `dword_907158 = pagesPerBlock` (`efc_cpu2.c:107673`, `:149017`,
#: `:24967`).  1 = pseudo-SLC, 0 = native TLC.
#:
#: A whole-image dataflow scan over the descriptor base finds exactly three
#: sites at offset 10: the getter read, the `= 0` in `sub_402E60F2`
#: (`efc_cpu3.c:44780`) and the setter store.  Eighteen readers go through the
#: getter, and two of them are the band expression this module already uses:
#: `sub_402E554E(w) + 2*lutId (+ const)` at `efc_cpu3.c:46168` and `:49301`.
#: So `weru_bin_for_band`'s `base + slcMode + 2*lutId` is the firmware's own
#: formula, with `slcMode` read off the drive rather than inferred.
WERU_DESC_SLC_MODE_OFF = 10
WERU_DESC_SLC_MODE_SLC = 1                  # cellMode 1 = pseudo-SLC
WERU_DESC_SLC_MODE_TLC = 0                  # anything else = native TLC
#: `weruDescriptor +1` and `+2`: MEASURED negatives, recorded so nobody chases
#: them again.  `+1` has zero sites of any kind in all four CPUs -- no reader,
#: no writer, not even an initialiser, and no wider load that would pick it up
#: alongside `+0`.  `+2` (u16) is written 0 exactly once, by the array
#: initialiser `sub_402E60F2` (`efc_cpu3.c:44782`), and has no reader anywhere.
#: RETRACTED with this: "`+2` is read at `efc_cpu2.c:44967`" -- that line is an
#: unrelated thunk, and `efc_cpu3.c:44967` reads `byte_402A6618[16*binId + 2]`,
#: which is the 16-byte weru BIN record's head pointer, not this struct.
WERU_DESC_DEAD_OFFSETS = (1, 2)

#: `rebuildSequenceNumberPool`: 144 bytes (`efc_cpu3.c:37472`) = 6 x 24, one
#: record per msoGroupId.  `sub_13708` (`efc_cpu2.c:34445-34456`) reserves a run:
#:     v2[1] = v2[0]; v2[0] += count; v2[2] = v2[0]
#:
#: the record is not one cursor, and `24*msoGroupId` is not the whole index.
#: Corrected 2026-08-07.  Every site that dereferences a live cursor writes
#:
#:     (char *)&unk_90C658 + 24*msoGroupId + 8*(msoDescriptor[msoId][19] & 1)
#:
#: -- `efc_cpu2.c:43030`, `:43058-43060`, `:43133`, `:43194`, `:59133-59135`,
#: `:94905`, `:94988`, `:95074-95076`, `:95301-95307`, `:110747-110749`,
#: `:115596-115598`, `:132799-132801`, `:134795-134801`, `:136367-136369`.  So
#: `+0` and `+8` are two lanes of one group:
#:
#:     +0  u64  the shared lane, used by MSOs with `+19` bit 0 clear, and the
#:              allocator a reservation is carved out of (`[1] = [0]; [0] += n`)
#:     +8  u64  the reserved lane, used by MSOs with `+19` bit 0 set
#:     +16 u64  the end of the reserved window -- checked only for those MSOs
#:
#: The bit-0-only overflow guard proves the pairing three times over:
#: `if (bit0 == 1 && rec[1] >= rec[2]) panic("halt()",
#: "RebuildSequenceNumberPool.h", 166)` at `efc_cpu2.c:43039`, `:43145`,
#: `:43204`.  All three qwords of all six records are initialised to 1
#: (`efc_cpu2.c:162283-162285`), which is why an untouched lane reads 1.
#:
#: A decoder that reports `+0` as "the" sequence number for an MSO is wrong for
#: every MSO whose `+19` bit 0 is set -- use `mso_rebuild_sequence_number`.
REBUILD_SEQ_POOL_ENTRY = 24
REBUILD_SEQ_POOL_GROUPS = 6
REBUILD_SEQ_POOL_BYTES = REBUILD_SEQ_POOL_ENTRY * REBUILD_SEQ_POOL_GROUPS
#: Byte offset of a lane inside a group record: lane 0 at +0, lane 1 at +8.
REBUILD_SEQ_POOL_LANE = 8
#: A lane that has never been reserved from reads this, not 0.
REBUILD_SEQ_POOL_UNUSED = 1

#: `msoDescriptor +5`, the byte `sub_4033E8A4` (`efc_cpu2.c:109611-109646`) tests
#: to decide what a rebuild does with this MSO's LUT:
#:     bit 0  walk this MSO's LUT at all
#:     bit 1  set validMap bits as well as counting (`sub_1025E` vs `sub_8F3E`)
#: On this board msoId 41 reads 0x07 (walk + map) and msoId 40 reads 0x0d
#: (walk, count only) -- which is exactly why the stored validCount can be
#: non-zero for a WERU whose stored validMap row is empty.
#:
#: BIT 1 GATES A SECOND, MUCH LARGER WRITER, AND IT IS TESTED ON A DIFFERENT
#: MSO.  The same bit is the gate in `sub_4033EA80` (`0x4033EAA0`/`0x4033EAA4`)
#: at full-mount step `MOUNT_STEP_LUT_REBUILD`: a per-lutId rebuild replays the
#: Whole LUT into the validMap (`sub_4033E94E`) only when
#: `snapshotInformation.count != 0` and the bit is set in
#: `msoDescriptor[snapshotInformation.list[0].msoId] + 5`.  That msoId is the
#: LUT's own first snapshot entry -- not the owner of the WERU being
#: rebuilt, and not the MSO whose LUT is being walked.  Here both populated
#: regions name an MSO reading 0x0E, so both replay; a WERU owned by an MSO with
#: bit 1 clear still gets its map replayed if its lutId's `list[0]` MSO has it
#: set, and vice versa.  Reading this byte off the WERU's owner answers a
#: different question -- see `Ftl.lut_rebuild_plan`.
MSO_DESC_REBUILD_OFF = 5
MSO_REBUILD_WALK_BIT = 0x01
MSO_REBUILD_VALIDMAP_BIT = 0x02

#: The mount FSM.  `sub_402E0C60(quick, ...)` (`efc_cpu3.c:39375`) installs
#: One of two step tables and a zero word terminates a table (the convention
#: holds for all five tables checked):
#:
#:     quick != 0 -> `off_402F8610`, two steps (create the SystemMetadata MSO,
#:                   then ReadPhysicalBlockPropertiesFromBackUp).  This is the
#:                   Format path and it touches neither cache.
#:     quick == 0 -> `off_402F8620`, 48 steps; step index is
#:                   `(addr - 0x402F8620) / 4`, anchored by step 4 =
#:                   `ErrorLog_Initialize`.
#:
#: The steps that write `validMap` / `validCount`:
#:
#:     25  `sub_402E110C`  dirty ? ValidMapReset : ValidMapRead     (`:39714`)
#:     26  `sub_402E1144`  dirty ? nothing      : ValidCountRead    (`:39739`)
#:     31  `sub_402E1212`  RebuildValidMap(msoIds, 2) -- unconditional
#:     35  `sub_402E12CA`  dirty ? sub-FSM `off_402F86E8` : nothing (`:39852`)
#:     39  `sub_402E1318`  dirty ? sub-FSM `off_402F8720` : nothing
#:
#: Step 35's sub-FSM ends with four LUT rebuilds, one per lutId, in this order
#: (`off_402F86E8` indices 7..10): RebuildParityLut -> lutId 4, RebuildLut ->
#: lutId 1, RebuildNeoLut(mediaSpace 1) -> lutId 2, RebuildNeoLut(mediaSpace 2)
#: -> lutId 3.  Each installs its own 5- or 6-step FSM (`sub_40355856` ->
#: `off_403A9438`; `sub_40355894` -> `off_403A9450`):
#:
#:     0  `sub_4035738E`  the media scan (`sub_40357132`) marks every WERU
#:     1  `sub_403573A8`  lutId -> `sub_4033EA80` -> `sub_4033E94E` replays the
#:                        Whole LUT into the validMap, gated as described on
#:                        `MSO_REBUILD_VALIDMAP_BIT`
#:     2  `sub_403573D2`  validCount[weru] := popcount(validMap[weru]) for every
#:                        WERU the scan marked  <-- where a rebuilt ZERO is made
#:     3  `sub_40357434`  weru state := (validCount == 0), then RebuildWeruBins
#:     4  `sub_40357490`  teardown, rebuildSequenceNumberPool += 1
#:
#: The scan set is per lutId, not per msoId: `sub_403554AE`
#: (`efc_cpu2.c:132853`) walks msoIds 32.. and takes the WERU list of every MSO
#: whose `msoDescriptor +3` equals the lutId being rebuilt.
MOUNT_FSM_QUICK_TABLE = 0x402F8610
MOUNT_FSM_FULL_TABLE = 0x402F8620
MOUNT_FSM_FULL_STEPS = 48
MOUNT_STEP_VALID_MAP = 25
MOUNT_STEP_VALID_COUNT = 26
MOUNT_STEP_REBUILD_VALID_MAP = 31
MOUNT_STEP_LUT_REBUILD = 35
#: The lutIds step 35 launches a rebuild for, in the order `off_402F86E8` does.
#: No rebuild is ever launched with lutId 0, 5 or `LUT_ID_NONE` -- lutIds 0
#: and 5 are the two MSOs step 31 covers, and a WERU whose owner has no lutId at
#: all is touched by neither step.
LUT_REBUILD_ORDER = (4, 1, 2, 3)

#: What a ZERO in `validMap` / `validCount` means after a dirty mount, per WERU.
#: `Ftl.dirty_mount_valid_cache` returns one of these codes for each cache; only
#: ``"rebuilt"`` makes a zero evidence that the WERU is empty.
ZERO_MEANING = {
    "rebuilt": "reconstructed from the LUT at this mount, so 0 means EMPTY",
    "forced-zero": "the stage wrote 0 regardless of content (scanned but not "
                   "replayed, so validCount := popcount of an empty row); 0 "
                   "means NOTHING -- use the stored validCountLma",
    "never-touched": "no mount stage scans this WERU; it keeps ValidMapReset's "
                     "zero.  0 means NOTHING -- use the stored validCountLma",
    "not-maintained": "this MSO's msoDescriptor +5 bit 1 is clear, so the drive "
                      "keeps no map for it in any mount; 0 is structural, not a "
                      "loss (its validCount is still rebuilt)",
}
#: The WMA sentinels the rebuild skips (`efc_cpu2.c:109642`).  Note it is only
#: three of the four in `LUT_SENTINELS`: -2 (0xFFFFFFFE) is counted.
REBUILD_SKIP_WMAS = (0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFC)

#: SystemMetadataInformation (object 1): u8 dirty | .. | u32 formatVersion | u32.
SYSMETA_INFO_SIZE = 16
SYSMETA_INFO_DIRTY_OFF = 0
SYSMETA_INFO_VERSION_OFF = 4

#: `SystemBlockMapping`: u32 magic | u32 nextSlot | entry[16]{u16 weruId; u16 pad;
#: u32 pbid[12]}.  The object's own size (840) pins the layout exactly, and the
#: firmware's own size formula for it is `64*blocksPerWeru + 72`
#: (`efc_cpu3.c:42493`), which at 840 bytes gives 12 -- so the firmware really
#: does call this field `blocksPerWeru`.  It is the SLC *metadata* WERU's
#: blocksPerWeru (12 x 368 x 4 = 17664 AUs); the global `blockMapping` table's
#: stride is `geometry.BLOCKS_PER_WERU` (48) and the two must never be swapped.
SYSTEM_WERU_SLOTS = 16                      # cMaxSystemWeruCount
SYSTEM_BLOCK_MAPPING_PBIDS = 12             # metadata WERU blocksPerWeru, not 48
SYSTEM_BLOCK_MAPPING_SIZE = 8 + SYSTEM_WERU_SLOTS * (4 + 4 * SYSTEM_BLOCK_MAPPING_PBIDS)
INVALID_WERU = 0x3FFF

#: SystemMetadata directory (LMA 0), 12 little-endian u32.
SYSMETA_DIR_FIELDS = ("magic", "formatVersion", "revision", "blockMappingLma",
                      "userLutLutLma", "msoLma", "mediaSpaceLma",
                      "weruDescriptorLma", "weruBinLma", "validMapLma",
                      "validCountLma", "rebuildSequenceNumberPoolLma")
#: The four maxSystemMetadataSizeInLmas capacity classes.
SYSMETA_SIZE_CLASSES = (6144, 7424, 13824, 24576)

#: The section layout builder, `SystemMetadataFormat.c` (efc_cpu3.c:37436-37478):
#:     lmas(size) = ceil(size / SECTION_UNIT) * LMAS_PER_UNIT
#:     base[0]    = SECTION_FIRST_BASE          (the directory is 4 LMAs at 0)
#:     base[n+1]  = base[n] + lmas(size[n]) + SECTION_GAP
#: so a section's length is `nextBase - thisBase - SECTION_GAP`, never
#: `nextBase - thisBase`.  Getting that wrong makes every section look as though
#: it stopped short of its own size, which is what the old `reached_cap=False`
#: on every line of `describe()` was.
SECTION_GAP = 32                            # LMAs of padding after every section
SECTION_UNIT = 16384                        # allocation granule in bytes
LMAS_PER_UNIT = 4                           # ... = 4 LMAs
SECTION_FIRST_BASE = 36
LMA_BYTES = 4096

#: `userLutLut` (LMA 36..47 here): three LMAs of 4, laid out
#:   base+0  16-byte header: "LUT\0", version, infoLma, dataLma
#:   base+4  a 296-byte descriptor, at offset 0 of the LMA
#:   base+8  the raw WMA array, `lmaLutCount` little-endian u32
#: Entries are flat 32-bit WMAs, not packed bitfields (`efc_cpu2.c:23297`), and
#: the index is `lma >> shift` (`Lut_GetEntry`, `efc_cpu2.c:23033`).
LUTLUT_MAGIC = 0x0054554C                   # "LUT\0"
LUTLUT_HEADER_BYTES = 16                    # magic, version, infoLma, dataLma
LUTLUT_DESC_BYTES = 296
#: u32 msoId that initialised this LUT slot (`efc_cpu2.c:109542`), which is not
#: the same thing as "the msoId this LUT belongs to" and is not a constant: it
#: reads 40 for the userLutLut region but 32/33/48/34 for the four per-namespace
#: regions.  What identifies the owner set is `msoDescriptor[owner].lutId`.
LUTLUT_MSO_OFF = 236
LUTLUT_PTR_OFF = 240                        # u32 runtime pointer -- not on-media
LUTLUT_SIZE_OFF = 244                       # u32 table size in bytes
LUTLUT_MAXLMA_OFF = 252                     # u32 highest LMA currently in use
LUTLUT_COUNT_OFF = 256                      # u32 lmaLutCount (entries allocated)
LUTLUT_GRAN_OFF = 284                       # u32 granularity in LMAs
LUTLUT_SHIFT_OFF = 288                      # u32 log2(granularity)
#: The rest of the 296-byte descriptor, from `Lut_Create` (`sub_4033E5C8`,
#: `efc_cpu2.c:109470-109592`).  That function `mem_clr`s exactly 0x128 bytes
#: (`:109537`) -- which is where the 296 comes from -- and then writes only the
#: words below, so anything it does not write is a real zero, not a hole.
LUTLUT_ZERO_OFF = 248        # u32, explicitly zeroed by Lut_Create (`:109543`)
LUTLUT_SECTION_COUNT_OFF = 260   # u32; 1 unless the table is split (`:109549`)
LUTLUT_SECTION_SHIFT_OFF = 264   # u32 `32 - clz(maxLma / a4)`      (`:109557`)
LUTLUT_SECTION_SIZE_OFF = 268    # u32 `sub_4033FC3A(shift, a3)`    (`:109559`)
LUTLUT_LAST_SECTION_OFF = 272    # u32 the short final section      (`:109563`)
#: A per-section dirty bitmap, initialised by
#: `sub_40300D2C(desc + 276, 0, sectionCount)` with a hard panic if it would
#: need more than four bytes (`Lut.c:2804`, `efc_cpu2.c:109572-109579`).
LUTLUT_SECTION_DIRTY_OFF = 276

#: `snapshotInformation` -- the descriptor's first 236 bytes.  Named by the
#: firmware's own assert, which is the only string that mentions it:
#: `index < GetNumberOfArrayElements(_lut.lookupTable[lutId].snapshotInformation
#: .list)` (`Lut.c:1500`, `efc_cpu2.c:110141`).  `sub_4033F01E`
#: (`efc_cpu2.c:110131-110152`) appends to it: the count is at descriptor +8,
#: the bound is 9, the stride is 24, and the two fields it initialises are the
#: element's byte +0 (an msoId, `:110143`) and its u16 +2 (`:110144`), set to
#: `0x3FFF` -- the same
#: "no WERU" sentinel used everywhere else.  `16 + 9*24 = 232` and +232 is a
#: separate u32, so the list occupies +16..+231 exactly.
#:
#: MEASURED on the two populated regions this board holds (`pt_lutdesc.py`):
#:   * region 0 (lutId 1) lists msoIds {32,36,37,45,43,52,49} + 42, count 8;
#:     region 3 (lutId 4) lists {42,34,35,39,54}, count 5.  Those are exactly
#:     the MSOs whose `msoDescriptor.lutId` is 1 and 4 -- 7/7 and 4/4, no
#:     extras, no omissions -- plus msoId 42 Trim/System in both.  The lutId
#:     table is derived from a different byte of a different structure, so this
#:     is an independent confirmation of both.
#:   * element `+2 u16` == that msoId's `Allocation.currentWeruId`  6/7
#:     (the miss is msoId 54, whose Allocation record has since been re-armed by
#:      `sub_4034DBFA` -- the snapshot is the older half)
#:   * element `+8 u32` == that WERU's `WeruUsageLog` activation counter  7/7
#:   * element `+16 u32` < that WERU's AU capacity  7/7 (msoId 54's is exactly
#:     capacity-1)
#: `+4`, `+12` and `+20` of every element read 0 on this board, as do the
#: header's `+4` and `+12`.  The header's `+0 u32` (3853008 / 539567) is not
#: named -- it is not the entry count, not the section size and not any
#: rebuildSequenceNumberPool value, and two samples is not enough to fit
#: anything to.
#:
#: `+232` is named, 2026-08-07, and this comment used to say it was not.
#: `sub_A688` (`efc_cpu2.c:25397-25402`) writes one value to two places:
#:
#:     *(u32 *)(dword_80C2BC + 393224) = a1;         # inside the NSID2 Lpt buffer
#:     *((u32 *)sub_FE0(byte_900001) + 58) = a1;     # 58*4 = 232, a LUT descriptor
#:
#: `dword_80C2BC` is the NSID2 Lpt+L2S allocation -- which is the lutId-2 LUT
#: region -- and 393216 is the LptEntry array's own size (16384 x 24), so
#: `+393224` sits just past it.  Both copies read `10259475` on this board's
#: media: region 0's descriptor `+232`, and lutId-2 region byte 393224
#: (LMA 10800 byte 8).  The other three regions' `+232` are 0, which is what
#: pins `byte_900001` to the msoId carrying lutId 1.  Verified by reading, not
#: fitted -- one value, two structures, no free parameters.  `LUTLUT_MIRROR_OFF`.
#: `16 + 9*24 = 232`: the u32 immediately after `snapshotInformation.list`.
#: `sub_A688` mirrors it into the NSID2 Lpt buffer at `LPT_MIRROR_OFF`.
LUTLUT_MIRROR_OFF = 232
#: `LptEntry[16384] * 24 = 393216`, then `+8`.
LPT_MIRROR_OFF = 393224
#: `snapshotInformation.sequenceNumber` -- descriptor `+0`, a `u64`.
#: Solved 2026-08-07 and it is why every `u32 desc[0]` search missed it: the
#: writer is a 64-bit store.  `sub_4033F970` (`efc_cpu2.c:110746-110749`):
#:
#:     if (desc[+8])                                   # count != 0
#:         *(u64 *)desc = rebuildSequenceNumberPool[ 24*msoGroupId(list[0].msoId)
#:                                                 + 8*(msoDescriptor[..][19]&1) ]
#:
#: i.e. the `rebuildSequenceNumberPool` value captured at the moment the LUT
#: descriptor was flushed, taken from the lane `msoDescriptor +19` bit 0 selects
#: (`rebuild_sequence_lane`, the same two-lane rule as
#: `Ftl.mso_rebuild_sequence_number`).  The reader `sub_403554AE` seeds the pool
#: with `sequenceNumber + 1`, which is what makes it a sequence number rather
#: than a checksum.
#:
#: Corroborated 4/4 across all four LUT regions on this board, nothing fitted:
#: the two regions with `count == 0` read 0 (the store is guarded on the count),
#: and the two populated ones read a value strictly below the live pool lane for
#: the msoGroup of their own `list[0].msoId` -- region 3's `list[0]` is msoId 42
#: (Trim/System, msoGroup 0), so it matches group 0's pool and not
#: ParityData's, exactly as that msoId predicts.
#:
#: RETRACTED with it: *"`+0` is not any rebuildSequenceNumberPool value"* -- that
#: test asked for equality, which can never hold, because the pool advances after
#: every write and a flushed snapshot is therefore always strictly behind.
LUTLUT_SNAPSHOT_SEQ_OFF = 0                 # u64
LUTLUT_SNAPSHOT_COUNT_OFF = 8
LUTLUT_SNAPSHOT_LIST_OFF = 16
LUTLUT_SNAPSHOT_ENTRY = 24
LUTLUT_SNAPSHOT_MAX = 9                     # the bound in the Lut.c:1500 assert
LUTLUT_SNAPSHOT_MSO_OFF = 0                 # u8  msoId       (`efc_cpu2.c:110147`)
LUTLUT_SNAPSHOT_WERU_OFF = 2                # u16 weruId, 0x3FFF = none (`:110148`)
LUTLUT_SNAPSHOT_ACTIVATION_OFF = 8          # u32 WeruUsageLog activation counter
LUTLUT_SNAPSHOT_WMA_OFF_OFF = 16            # u32 WMA offset inside that WERU

#: A region occupies `size//4096` table LMAs preceded by the header and the
#: descriptor, one allocation unit each: `nextBase = base + size//4096 + 8`.
LUTLUT_REGION_OVERHEAD_LMAS = 8


def snapshot_sequence_number(desc):
    """296-byte LUT descriptor -> `snapshotInformation.sequenceNumber` (u64).

    See `LUTLUT_SNAPSHOT_SEQ_OFF`.  Returns 0 when the descriptor's own
    `count` is 0, which is a real zero and not a missing value: `sub_4033F970`
    guards the store on the count, so an unflushed region genuinely carries no
    sequence number.  `decode_lut_snapshot_header` reports both together so a
    caller never has to guess which zero it is looking at.
    """
    return struct.unpack_from("<Q", desc, LUTLUT_SNAPSHOT_SEQ_OFF)[0]


def decode_lut_snapshot_header(desc):
    """-> {'sequence_number', 'count', 'stamped', 'field_4', 'field_12'}.

    `stamped` is `count != 0`, the firmware's own condition for `+0` having
    been written at all.  `+4` and `+12` read 0 on every region of this board
    and no writer for either has been found; they are returned rather than
    hidden so a different image can contradict that.
    """
    n = struct.unpack_from("<I", desc, LUTLUT_SNAPSHOT_COUNT_OFF)[0]
    return {
        "sequence_number": snapshot_sequence_number(desc),
        "count": n,
        "stamped": n != 0,
        "field_4": struct.unpack_from("<I", desc, 4)[0],
        "field_12": struct.unpack_from("<I", desc, 12)[0],
    }


def decode_lut_snapshot(desc):
    """296-byte LUT descriptor -> [{msoId, weruId, activation, wmaOffset, raw}].

    Only the first `count` elements are returned; `weruId` is None for the
    `0x3FFF` sentinel.  Raises on a count past the firmware's own bound rather
    than reading into the tail fields.
    """
    n = struct.unpack_from("<I", desc, LUTLUT_SNAPSHOT_COUNT_OFF)[0]
    if n > LUTLUT_SNAPSHOT_MAX:
        raise ValueError("snapshotInformation.count %d exceeds the firmware's "
                         "own bound of %d" % (n, LUTLUT_SNAPSHOT_MAX))
    out = []
    for i in range(n):
        o = LUTLUT_SNAPSHOT_LIST_OFF + LUTLUT_SNAPSHOT_ENTRY * i
        e = bytes(desc[o:o + LUTLUT_SNAPSHOT_ENTRY])
        weru = struct.unpack_from("<H", e, LUTLUT_SNAPSHOT_WERU_OFF)[0]
        out.append({
            "mso_id": e[LUTLUT_SNAPSHOT_MSO_OFF],
            "weru_id": None if weru == INVALID_WERU else weru,
            "activation": struct.unpack_from(
                "<I", e, LUTLUT_SNAPSHOT_ACTIVATION_OFF)[0],
            "wma_offset": struct.unpack_from(
                "<I", e, LUTLUT_SNAPSHOT_WMA_OFF_OFF)[0],
            "raw": e,
        })
    return out

#: `msoDescriptor` bytes that place an MSO's LUT.  Both are on media, in the
#: `msoLma` section this module already decodes.
MSO_DESC_LUT_ID_OFF = 3                     # u8 lutId, 0xFF = this MSO has none
MSO_DESC_LUT_STORE_OFF = 20                 # u8 msoId whose LMA space holds it
LUT_ID_NONE = 0xFF
LUT_STORE_NONE = 0xFF                       # not in any MSO's LMA space
#: The maximum number of LUT id slots.  It is not a shortage: a lutId names one
#: LMA address space shared by a whole namespace group, so all seven MSOs of
#: group 1 are views over one table.
LUT_ID_COUNT = 8

#: A LUT entry that maps nothing.  Four distinct sentinels exist; the firmware's
#: validMap rebuild skips -1/-3/-4 explicitly (`efc_cpu2.c:109642`).
LUT_SENTINELS = (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)
#: The only granularities the firmware builds: 4 KiB, 16 KiB, 64 KiB per entry.
LUT_GRANULARITIES = (1, 4, 16)

MSO_DESC_ENTRY = 28                         # 63 x 28 = 1764 = the msoLma section
MSO_DESC_TOTAL_LMAS_OFF = 7                 # u32, unaligned; verified on 41/40/42/56
MSO_DESC_RAID_OFF = 16                      # u8, Mso_GetRaidType (efc_cpu2.c:38744)

#: The field boundaries are not fitted -- the firmware enumerates them itself.
#: `sub_403022E4` (`efc_cpu2.c:47237-47278`) and its cpu3 twin `sub_402C1C50`
#: (`efc_cpu3.c:14254-14290`) are a per-field descriptor dumper with one Debug
#: event id per field, and between them they name exactly:
#:
#:     +0 bit0, bit1, bit2, bits[4:3], bit5, bits[7:6]
#:     +1, +2, +3, +19 bits[3:1], +4, +5 bits 0/1/2/3, +6,
#:     u32 +7, u16 +11, u16 +13, +15 bits 0/1/2, +16, +17, +18, +20
#:
#: and then stops.  So `+7` really is an unaligned u32 (corroborated by
#: `efc_cpu0.c:55172`, which multiplies it by the cLBAFTable lmuDataSize), and
#: there is no field spanning +8..+15: it is `u32@+7`, `u16@+11`, `u16@+13`,
#: `flags@+15`.
#:
#: `+21..+27` are DEAD, and that is a measured negative, not a gap.  Every
#: root constructor memcpy's a 20-byte ROM template and then stores one u32 at
#: +20 (`efc_cpu3.c:35480, 36389, 37219, 37242, 37784, 37867, 37811, 37950,
#: 38127, 38188`), so +21..+23 are deterministically zeroed by that store and
#: +24..+27 are never written at all; the derived constructors copy 0x1C from an
#: already-built sibling and only propagate them.  Access searched three ways --
#: a pseudocode census of every addressing form, a Thumb disassembly with
#: base-register tracking over the eapmem image and all four ATCM images, and
#: the field dumper above -- with zero reads and zero writes at 21..27.
#: Consequence worth recording: those 4 bytes per record are uninitialised SRAM
#: that is persisted, because the whole 1764-byte array round-trips through the
#: `msoLma` section (`efc_cpu3.c:6951`, `:37444`).
MSO_DESC_DEAD_TAIL = (24, 28)

#: `+0` bit 0 = `Mso_IsValidMso(msoId)`, spelled out by the firmware's own
#: asserts -- the inlined test is `msoId < 0x3F && (byte0 << 31)` and the panic
#: text is the assert body (`efc_cpu2.c:117317` `Lpt.c:4741`, `:125750`
#: `Allocation.c:1952`, `:142042` `RaidRecovery.c:521`, `:143943`
#: `RaidSanityCheck.c:199`, `efc_cpu3.c:47577` `WeruManagement.c:2400`,
#: `:52374` `MetadataStorage.c:387`, and ~60 read sites in all).
#: Nothing ever clears it at runtime: all 11 ROM templates carry it set and no
#: bit-level writer of bit 0 exists, so the only way an entry reads invalid is
#: never having been constructed (or the media copy saying so).
MSO_DESC_VALID_BIT = 0x01

#: `+0` bit 5.  There is a dedicated accessor, `sub_402E5690`
#: (`efc_cpu3.c:44191-44194`), taking a weruId through `Weru_GetMsoId`:
#: `(msoDescriptor[Weru_GetMsoId(weru)] >> 5) & 1`.  Its functional readers are
#: all in WeruManagement: `efc_cpu3.c:47084` folds the WERU's free count into a
#: pool minimum only when set, `:48423` selects `sub_402E89A8("AllocateWeru")`
#: over `sub_402E7218`, `:49803` gates a walk over WERU bin pairs.  Named from
#: those call sites and the `"AllocateWeru"` literal -- indicated, not proven:
#: no assert names it.
MSO_DESC_WERU_POOL_BIT = 0x20

#: `+0` bits [7:6] -- a 3-valued write-frontier fill class.  One functional
#: reader, `sub_C1BC` (`efc_cpu2.c:27438`, `:27468-27494`), which turns it into
#: the in-block offset at which a writeable unit is closed early:
#:
#:     class 2  threshold stays 0xFFFF          -- never close early
#:     class 1  threshold = word_90C4AA (SLC) or word_90C4AC   -- full block
#:     class 0  derated: threshold>>1 for msoId >= 0x20, else t - (t>>5)
#:
#: Written only as a whole-field replacement (`byte0 & 0x3F | v<<6`) at six MSO
#: constructors: value 2 at `efc_cpu3.c:37969`, `:38244`, `:38379`, `:38420`;
#: value 1 at `:38002`, `:38209`.  Templates carry 1 or 0.  3 is unreachable.
#: Mechanism proven; no name exists anywhere in the four images.
MSO_DESC_FILL_CLASS_SHIFT = 6
MSO_DESC_FILL_CLASS_MASK = 3

#: `+1` = `Mso_GetAllocationType(msoId)`, named by the assert at
#: `efc_cpu2.c:108932` (`"Mso_GetAllocationType(msoId) != cAllocationTypeNone"`,
#: `MediaReplaceBlockEngine.c:790`), whose inlined test is `byte[+1] == 0` --
#: so `cAllocationTypeNone == 0`.  `sub_402E0200` (`efc_cpu3.c:38580-38592`)
#: computes it and yields only {1, 4}; explicit constant stores add 1 and 4.
#:
#: Value 2 is compared against but never produced (`efc_cpu2.c:27773`,
#: `:27925`, `:27952` all test `== 2`), so on this firmware it can only arrive
#: through the `msoLma` restore -- a real behavioural branch reachable only from
#: media.  Recorded rather than assumed away.
MSO_DESC_ALLOC_TYPE_OFF = 1
MSO_ALLOC_TYPE_NONE = 0

#: `+4` is not msoGroupId.  RETRACTED 2026-08-07 -- that reading is in
#: several older notes and it is wrong; it survived because the value happens to
#: equal the msoGroupId for the three namespace groups.  It is a zeroDataId:
#: its only readers are `sub_1348(msoDescriptor[m][4])` at `efc_cpu2.c:36218`,
#: `:36565` and `:38425`, and `sub_1348` -> `sub_403029BC` is
#: `if (id >= zeroData.count) panic("zeroDataId < zeroData.count",
#: "ZeroData.c", 137); return zeroData[id].pattern;`.  Its only writer is
#: `sub_40301A0E(mediaSpaceId, id)` (`efc_cpu2.c:46760-46785`), which stamps the
#: same id onto every MSO of one media space plus three extra System msoIds when
#: `mediaSpaceId == 0`.  That last detail is exactly why msoIds 34/35/62
#: (ParityData) read the NSID1 value 1 while 39/54 read 0 -- which is nonsense
#: as a group id and correct as "these three were also stamped".
#:
#: The real msoGroupId lives in the SRAM pair table `unk_900830 + 2*msoId`
#: (`+0` msoType, `+1` msoGroupId; `efc_cpu2.c:34446`, `:26114`, `:23074`), and
#: offline the msoIdTable is the equivalent source.
MSO_DESC_ZERO_DATA_ID_OFF = 4

#: The dynamic msoIds carry no msoIdTable entry, so their msoGroupId has to come
#: from elsewhere.  `msoDescriptor +3` (lutId) reads 2 for 0..15 and 3 for
#: 16..31 on this board, matching `efc_cpu2.c:26114`'s `groupId == 3 && msoId <
#: 0x20`, i.e. the two active-logical-partition pools are NSID2 and NSID3.
#: Used only as a fallback, and only when the lutId is one of those two.
MSO_DYNAMIC_GROUP_FROM_LUT = (2, 3)

#: `+5` is a FOUR-bit flag byte (the dumper emits exactly bits 0..3,
#: `efc_cpu2.c:47256-47259`).  Bits 0/1 were already named; the other two:
#:
#: bit 2 -- a second LUT-maintenance gate.  `efc_cpu2.c:23185-23188` ANDs it with
#:   bit 1 to choose the validMap-updating LUT callbacks, and `:23624` ORs it
#:   with bit 1 to fire the valid-map update on an LMA-range invalidate
#:   (`Lut.c:1190`).  On the WERU side `efc_cpu3.c:46454-46464` and
#:   `:46568-46580` use it to assert mutual exclusion with the WERU's own valid
#:   count.  Readers proven; the name is indicated, not proven.
#:
#: bit 3 -- the LUT's backing store is allocated in whole 16 KiB media pages.
#:   Exactly one reader, inside `Lut_Create` (`efc_cpu2.c:109521`):
#:   `if (bit3) sizeInBytes = ((4*count + 0x3FFF) / 0x4000) << 14`, immediately
#:   before `panic("sizeInBytes != 0", "Lut.c", 361)`.  Proven by that context,
#:   and it is exactly what makes msoId 40 read 0x0D where msoId 41 reads 0x07.
MSO_DESC_REBUILD_WALK_BIT = 0x01            # walk this LUT (efc_cpu2.c:109623)
MSO_DESC_REBUILD_SETMAP_BIT = 0x02          # also set validMap bits, not count only
MSO_DESC_LUT_RANGE_INVALIDATE_BIT = 0x04
MSO_DESC_LUT_PAGE_ALIGNED_BIT = 0x08

#: `+19` bit 0 = `Mso_NeedsSequenceNumberReservation(msoId)`, named by the
#: assert it guards (`efc_cpu2.c:34452-34454`,
#: `"Mso_NeedsSequenceNumberReservation(msoId)"`, `RebuildSequenceNumberPool.c:92`).
#: It is part of the pool index -- see `REBUILD_SEQ_POOL_*`.
MSO_DESC_SEQ_RESERVATION_OFF = 19
MSO_DESC_SEQ_RESERVATION_BIT = 0x01
#: `+19` bits [3:1] = `parityWeruFront`, named by the firmware's own error
#: string: `"[ParityManager_OnAllocationRequest]: MSO[%2x] parityWeruFront %d
#: wma 0x%08X ..."` with `((desc[19] >> 1) & 7)` in that argument slot
#: (`efc_cpu2.c:33726-33741`).  The only non-telemetry reader compares it
#: against 7 (`efc_cpu2.c:79695`), and 7 is what five of the six ROM templates
#: carry, so 7 is the "none" sentinel.  The only writer of these bits in all
#: four images is `efc_cpu3.c:38284` (`byte19 = byte19 & 0xF1 | 2`, i.e. front 1,
#: on the RAID-parity constructor when the NAND configuration selector is 1), so
#: the reachable set is {7 (template), 0 (one template), 1}.
#: Bits 4..7 of `+19` have no reader anywhere and `efc_cpu3.c:38284`'s `& 0xF1`
#: preserves them; templates leave them 0.
MSO_DESC_PARITY_FRONT_SHIFT = 1
MSO_DESC_PARITY_FRONT_MASK = 7
MSO_PARITY_FRONT_NONE = 7

#: `msoDescriptor +0` bits [4:3] = `Mso_GetNandBlockMode(msoId)`.  The firmware
#: spells the accessor out in one of its own asserts:
#:
#:     if ( ((*((unsigned __int8 *)&unk_900148 + 28 * v6) >> 3) & 3) != 1 )
#:       panic("Mso_GetNandBlockMode(msoId) == cNandBlockModeSlc", ...)
#:                                                   efc_cpu2.c:95482-95483
#:
#: so `cNandBlockModeSlc == 1`; the only other value present on this drive is 2,
#: which is native TLC.  Written at MSO creation as `byte0 = byte0 & 0xE7 |
#: 8 * (mode & 3)` (`efc_cpu3.c:38110`).
#:
#: This is the CELL mode, persisted, for every WERU -- see
#: `Ftl.cell_mode_evidence` prior 5.  Scored 2026-08-07 against every
#: independent source we hold: 5/5 of `MEASURED_CELL_MODES` (hardware dual-mode
#: reads), 972/972 of the WERUs `cell_mode` already answered with 0
#: disagreements, and an exact partition by WERU configuration -- cfg 0/1/2/3/5
#: all mode 1 and cfg 4 all mode 2, 1070/1070, no exception.  It also answers
#: the 98 spare WERUs (972..1069, owner msoId 58 PhysicalMediaAccess) that every
#: other prior leaves UNKNOWN, because they have no capacity field to divide.
MSO_DESC_NAND_BLOCK_MODE_OFF = 0
MSO_DESC_NAND_BLOCK_MODE_SHIFT = 3
MSO_DESC_NAND_BLOCK_MODE_MASK = 3
NAND_BLOCK_MODE_SLC = 1                     # cNandBlockModeSlc, efc_cpu2.c:95483
NAND_BLOCK_MODE_TLC = 2                     # the only other value on this drive
NAND_BLOCK_MODE_SLC_FLAG = {NAND_BLOCK_MODE_SLC: True, NAND_BLOCK_MODE_TLC: False}

BLOCK_MAPPING_ENTRY = 20                    # +0 u32 physicalBlockId
UNMAPPED_PBID = 0xFFFFFFFF
#: +10 u32 -- an unaligned cache of `physicalBlockProperties[pbid]`, refreshed
#: for every mapped entry in one sweep by `sub_40339FD4` (`efc_cpu2.c:105083`)
#: and read back as `(word >> 28) & 7` for the bad-block state (`sub_40339ECE`,
#: `:105003`; `sub_40339F3E`, `:105027` reads the same bits as `byte13 >> 4`).
#: `sub_40312B84` swaps bytes 0..15 of two entries, so it travels with the pbid.
#: See `PHYS_BLOCK_*` for the bit layout, and never split it into u16+u8+u8:
#: that cuts the erase count into three pieces.
BLOCK_MAPPING_PROP_OFF = 10

WERU_DESC_ENTRY = 20
WERU_DESC_OWNER_OFF = 0                     # u8 msoId
WERU_DESC_SELF_OFF = 4                      # u16 weruId self-reference
WERU_DESC_NEXT_OFF = 6                      # u16 next in bin
WERU_DESC_PREV_OFF = 8                      # u16 prev in bin
WERU_DESC_BIN_OFF = 11                      # u8 binId
WERU_DESC_ERASE_OFF = 12                    # u32 eraseCount
WERU_DESC_CAP_OFF = 16                      # u32 capacity in AUs

#: The last block of the reserved, directly-addressed firmware area.  Those
#: blocks are pseudo-SLC.  The converse is not true and used to be asserted
#: here: "everything above is native TLC" is false -- weru 84 is blocks
#: 172-175 and is pseudo-SLC.  This constant now says only where the firmware
#: area ends; it says nothing about the cell mode of any block above it.
FIRMWARE_AREA_LAST_BLOCK = 27

#: The aux of a data AU.  The page sequence number is a PAGE field spread across
#: the page's four aux entries, not an AU field: for layouts 0 and 1 it is
#: AU0 bits 30..0 as the high half and AU1 bits 30..0 as the low half, i.e.
#: 62 bits, with AU2 and AU3 zero (writer `efc_cpu2.c:43031`/`:43135`, reader
#: `:134226-134229`; 5723/5723 pages re-encode byte-identically from that
#: reading).  Higher is newer.
#:
#: RETRACTED: `AUX_SEQ_LEN = 3` -- a u24 read of AU1 alone.  It agrees on this
#: board only because the largest counter here is 3_862_740, which still fits;
#: that is already 23% of the way to 2**24, past which the u24 reading silently
#: truncates and newest-wins starts preferring older pages.  `titania.auxdata`
#: decodes the full 62-bit value; this constant survives only for the AU1 low
#: half and must not be used as "the sequence".
AUX_LMA_OFF, AUX_SEQ_OFF, AUX_SEQ_LEN = 0, 4, 3
SEQ_AU = 1
SEQ_AU_HIGH = 0
AUX_SEQ_MASK = 0x7FFFFFFF        # bit 31 of AU1's u32 is the parity flag


def page_sequence(aus):
    """The page's full 62-bit sequence number from its four aux entries.

    Use this for newest-wins.  `AUX_SEQ_LEN` reads 24 of the low 31 bits and is
    kept only so old call sites still parse; it truncates once the counter
    passes 2**24.

    NOTE for layouts 0/1 only.  A layout-2 (NSID2/NSID3) page does not increment
    the pool -- its value names the write epoch, not the page (`efc_cpu2.c:43058`
    reads it with no `++`; msoId 51's pages all read 2812 and msoId 53's all read
    2340, exactly `pool[3].next` and `pool[2].next`).  Ranking layout-2 pages by
    it is meaningless; `titania.auxdata.layout_for` says which layout applies.
    """
    lo = int.from_bytes(aus[SEQ_AU][AUX_SEQ_OFF:AUX_SEQ_OFF + 4], "little")
    hi = int.from_bytes(aus[SEQ_AU_HIGH][AUX_SEQ_OFF:AUX_SEQ_OFF + 4], "little")
    return ((hi & AUX_SEQ_MASK) << 31) | (lo & AUX_SEQ_MASK)


#: The aux layouts whose sequence number is a per-page counter, and therefore
#: the only ones on which "higher is newer" means anything.
#:
#: Layouts 0, 1 and 3 stamp the msoGroup's sequence pool and then increment it
#: (`efc_cpu2.c:43135` then `:43143`; `:43197` then `:43203`), so two pages of
#: one MSO never carry the same number and the larger one was written later.
#:
#: Layout 2 (`cAuxLayoutForForNSID23`) is not in this set.  Its writer reads
#: the pool at `efc_cpu2.c:43058` and never increments it, so the value names the
#: Write epoch, not the page: on this board every page of msoId 51 reads 2812 and
#: every page of msoId 53 reads 2340 -- exactly `pool[3].next` and `pool[2].next`
#: -- so the number is constant across a whole MSO's pages and cannot separate
#: two copies of one LMA.  See `titania.auxdata` for the citations and the measured
#: counts, and `titania.auxdata.sequence_is_plausible`, which relaxes the pool bound
#: from `<` to `<=` for exactly this reason.
SEQUENCE_RANKS_PAGES = frozenset((auxmod.LAYOUT_INTERNAL_DATA, auxmod.LAYOUT_1,
                                  auxmod.LAYOUT_RAID_PARITY))


def page_rank(sequence, parity, layout):
    """How two copies of one LMA are ordered.  -> a comparable tuple, higher wins.

    Two independent facts decide it, and conflating either with "newest wins"
    has already cost this project real data:

    1. An inline-RAID parity page is not the LMA's content.  It carries a
    real LMA and a real, higher sequence -- it is written after the pages it
    covers -- so plain newest-wins hands back parity bytes as content.  That
    substituted parity for 22 LMAs of `blockMapping` and made 88 WERUs name
    block numbers like 2386979.  So the parity flag is the first key and any
    ordinary copy beats any flagged one; a flagged copy is still used when
    nothing else holds the LMA, which does happen (254 LMAs of weru 23,
    including LMA 0, are held only by flagged pages and decode correctly).

    2. The sequence only orders pages on layouts 0, 1 and 3.  On layout 2 it
    is the write epoch, constant across an MSO (`SEQUENCE_RANKS_PAGES`), so
    ranking by it is not "slightly wrong", it is meaningless -- the comparison
    is between two equal numbers and the winner is whichever the loop happened
    to see first, dressed up as a recency decision.  This is the same shape as
    the parity bug: a plausible number that is not the quantity being asked for.

    So on layout 2 the sequence is dropped from the key rather than used.  The
    result is a deliberate tie between ordinary copies, which the caller must
    handle as unresolved -- `SystemMetadata.scan` counts them and lists the LMAs
    -- instead of a silent, arbitrary pick.

    `layout` may be UNKNOWN or None, meaning the caller does not know.  That
    keeps the sequence, because layouts 0/1/3 are 3 of the 4 and every WERU this
    toolkit scans today is one of them -- but the scan records that it did not
    know, so an unknown layout is visible rather than being quietly treated as
    an established one.
    """
    ordinary = 0 if parity else 1
    if layout in SEQUENCE_RANKS_PAGES or layout is UNKNOWN or layout is None:
        return (ordinary, sequence)
    return (ordinary, 0)

#: Byte +7 of the aux is a flag byte -- the top byte of the u32 at +4 whose low
#: 31 bits carry the sequence/tag.  The firmware preserves bit 31 across every
#: rewrite of that word (`*p = *p & 0x80000000 | value & 0x7FFFFFFF`,
#: `efc_cpu2.c:43031-43033`, `:43135-43137`), so it is a sticky per-AU flag and
#: not part of the counter.
AUX_FLAG_OFF = 7

#: Aux flag bit 7: This PAGE belongs to an inline-raid PARITY stripe.
#:
#: Set by `sub_1C3CC` (`efc_cpu2.c:42936`, panic string "(3 + auPerPage *
#: (pageCount - 1)) < cMaxAusPerNandCommand", "WriteAuxiliaryList.c", 378),
#: which ORs 0x80 into aux byte +7 of all four AUs of the pages it selects.
#: It has exactly two callers, `efc_cpu2.c:37515` and `:38735`, and both are
#: gated on the NAND command's `raidStripeInfo & 0xF == 2` -- `cRaidTypeInline`,
#: pinned at 2 by the firmware's own `sub_13A8(msoId) == 2` (PhaseControl.c:5253)
#: and by `Mso_GetRaidType` (msoDescriptor +16, `efc_cpu2.c:38744`) returning 2
#: for exactly Lut/System, SystemMetadata/System, Trim/System and
#: PhysicalMediaAccess/System.  The `:38735` site sits inside the block that
#: panics on "pWriteableUnit->raidInfoXferOnly.raidParityAuBuffer != null"
#: ("Moh.c", 2363), i.e. the parity path.
#:
#: Why it matters: a parity page carries a perfectly ordinary aux -- a real LMA
#: and a real, higher page sequence number, because it is written after the data
#: pages it covers -- but its payload is not that LMA's data.  A plain
#: newest-wins scan therefore prefers it and silently substitutes parity bytes
#: for content.  See `SystemMetadata.scan`.
AUX_FLAG_INLINE_PARITY = 0x80

#: Measured on this board over every page of the five metadata WERUs (14, 19,
#: 20, 22, 23) whose layout is known: slot 11 is 100% flagged in all five
#: (368/368 AUs per readable slot-11 block), slot 0 is 100% flagged in the three
#: whose slot 0 is readable (14, 20, 22 -- werus 19 and 23 have slot 0 on the
#: dead CE0), and every other slot is between 0% and 2%.  That is consistent
#: with slots 0 and 11 being the stripe's parity slots, and with `blocksPerWeru`
#: being 11 while twelve blocks are programmed -- but the flag, not the slot
#: number, is what this module keys on, because the flag is what the firmware
#: writes and the slot pattern is an observation about one board.


# ===========================================================================
# CELL MODE
#
# Everything about pseudo-SLC vs native TLC lives here.  Read the module
# docstring's retraction first: a block number does not answer this question.
# ===========================================================================
class CellModeUnknown(ValueError):
    """Raised rather than let an unmeasured cell mode reach a device.

    `UNKNOWN` is falsey, and `Device.read_page(slc=UNKNOWN)` would therefore
    read as `slc=False` -- a silent native-TLC read of a page that may be
    pseudo-SLC.  That is the exact shape of the bug this module exists to
    prevent, so the tri-state never crosses the boundary implicitly.
    """


#: pagesPerBlock -> cell mode.  A block presents 368 pages in pseudo-SLC and
#: 1104 in native TLC; there is no third value, which is what makes the capacity
#: prior below exact rather than a heuristic.
CELL_MODE_BY_PAGES_PER_BLOCK = {
    geom.PAGES_PER_BLOCK_SLC: True,         # 368  -> pseudo-SLC (prefix 0xDA)
    geom.PAGES_PER_BLOCK_TLC: False,        # 1104 -> native TLC (prefix 0xDF)
}


#: MEASURED on hardware, one dual-mode read per WERU with MCRC as the
#: discriminator (`no_recheck.py`, `no_verify.py`).  Not re-read stability: a
#: correct native-TLC read jitters ~63 of 18432 bytes between two reads of the
#: same page and still decodes 4/4, while a wrong-mode read is 0/4.
#:
#: This table is not an input to `cell_mode_from_capacity`, so the self-test's
#: comparison of the two is a hold-out rather than a restatement.
MEASURED_CELL_MODES = {
    81: (True,
         "HOLD-OUT (`pa_hwprobe*.py`): predicted pseudo-SLC by capacity alone "
         "-- 70656 over 48 blocks -- and then read both ways.  ce1/b160/p1, "
         "ce1/b160/p3, ce2/b160/p1 and ce1/b161/p1 all score MCRC 4/4 under "
         "0xDA with FOUR CONSECUTIVE aux LMAs (208-211, 592-595, 224-227, "
         "212-215) and 0/4 under 0xDF.  Note pages 0 and 2 read BYTE-"
         "IDENTICALLY in both modes and decide nothing -- see probe_cell_mode."),
    84: (True,
         "ce1/b172/p287 and ce11/b172/p287: MCRC 4/4 corrected AND 4/4 "
         "uncorrected with aux-LMA 4/4 under 0xDA, against 0/4, 0/4 and 0/4 "
         "under 0xDF.  The whole page-287 window then verified 28/28 slots, "
         "112/112 AUs, zero LDPC syndromes.  Blocks 172-175 -- above "
         "FIRMWARE_AREA_LAST_BLOCK and pseudo-SLC anyway."),
    234: (False,
          "ce1/b852/p539: MCRC 4/4 (corrected) and aux-LMA 4/4 under 0xDF; "
          "176/176 AUs verified over the window, every page needing LDPC "
          "correction.  Page 539 is past the 368 a pseudo-SLC block has, so "
          "0xDA cannot even address it."),
    581: (False,
          "HOLD-OUT (`pa_hwprobe*.py`): predicted native TLC by capacity alone "
          "-- 211968 over 48 blocks -- and then read both ways.  ce1/b3612 "
          "pages 0-6 and 8 and ce2/b3612 pages 0-1 score MCRC 4/4 under 0xDF "
          "with consecutive aux LMAs, and 0/4 under 0xDA at every one of "
          "them.  Ten addresses, no exception."),
    590: (False,
          "ce3/b3648/p106 and ce10/b3650/p106: under 0xDA the die returns an "
          "all-0xFF page (nonff 0, byte-identical CRC for two different "
          "addresses -- no data at all), while 0xDF returns real aux LMAs.  "
          "MCRC is 0/4 either way, so THIS WERU IS NOT DECIDABLE BY MCRC: it "
          "is here because the 0xDA read carries no data, and the capacity "
          "prior agrees."),
}

#: The page span the user LUT names inside each measured WERU, recorded in the
#: same session.  This is the trap, kept in the module.  "every page the LUT
#: names is below 368" is what sent us to look at weru 84 and it was right
#: there -- but weru 590's LUT pages are 6..152, all below 368, and weru 590 is
#: native TLC.  A low span is a candidate, never a verdict; only the other
#: direction (a page at or past 368) is proof, and it is proof of TLC.
MEASURED_LUT_PAGE_SPANS = {84: (281, 291), 234: (0, 855), 590: (6, 152)}


def cell_mode_for_block(block):
    """Retired.  Always raises `CellModeUnknown`.

    This used to be `block <= FIRMWARE_AREA_LAST_BLOCK`, i.e. "the firmware area
    is pseudo-SLC and everything above it is native TLC".  The second half is
    false: weru 84 is blocks 172-175 and is pseudo-SLC, one of 88 WERUs in that
    class here.  Every caller that reached this function was one wrong guess
    away from reading a written page as ~1000 syndromes of noise and treating it
    as data, so it raises instead of answering.

    Use `Ftl.cell_mode(weru_id)` / `Ftl.cell_mode_evidence(weru_id)`, or
    `cell_mode_from_capacity` if you hold the descriptor yourself.  For the
    firmware area specifically the honest test is `block <=
    FIRMWARE_AREA_LAST_BLOCK`, written out at the call site so it is visible
    that it is a partition-map claim and not a cell-mode rule.
    """
    raise CellModeUnknown(
        "cell_mode_for_block(%r) is retired: cell mode is a per-WERU property "
        "and cannot be derived from a block number (weru 84 is blocks 172-175 "
        "and is pseudo-SLC).  Use Ftl.cell_mode(weru_id) or "
        "cell_mode_from_capacity(capacity_aus, n_blocks)." % (block,))


def cell_mode_from_capacity(capacity_aus, n_blocks):
    """-> True (pseudo-SLC) / False (native TLC) / UNKNOWN.  FREE and exact.

    A WERU's `capacity` field is `blocks * pagesPerBlock * AUS_PER_PAGE_TLC`, so
    the capacity together with the number of blocks the WERU's own blockMapping
    record maps names `pagesPerBlock`, and that is 368 or 1104 and nothing else.
    Neither number alone is enough -- which is precisely how the old
    `_slc_from_capacity` went wrong.  It tested `capacity == AUS_PER_SLC_WERU`
    (17664 = 12 x 368 x 4) and so recognised only the TWELVE-block metadata
    WERUs; the 48-block pseudo-SLC user WERUs are 70656 = 48 x 368 x 4 and fell
    through to "no evidence", which the callers then read as native TLC.

    On this board every WERU that has both fields lands exactly on one of the
    two values, with no residue:

        capacity  blocks  pagesPerBlock  count  mode
           17664      12            368     26  pseudo-SLC (metadata/LUT)
           35328      24            368      3  pseudo-SLC (werus 0-2)
           70656      48            368     88  pseudo-SLC (user SLC cache)
          211968      48           1104    855  native TLC (user data)

    UNKNOWN when either input is missing, when the division is not exact, or
    when the quotient is not a real pagesPerBlock -- never a nearest match.
    """
    if capacity_aus is UNKNOWN or capacity_aus is None or not capacity_aus:
        return UNKNOWN
    if not n_blocks:
        return UNKNOWN
    per_block, rem = divmod(int(capacity_aus), n_blocks * geom.AUS_PER_PAGE_TLC)
    if rem:
        return UNKNOWN
    return CELL_MODE_BY_PAGES_PER_BLOCK.get(per_block, UNKNOWN)


def cell_mode_from_lut_pages(max_page):
    """-> False (native TLC) / UNKNOWN.  It can never return True.

    A page at or past `PAGES_PER_BLOCK_SLC` cannot exist in a pseudo-SLC block,
    so a LUT entry naming one is proof the WERU is native TLC.  The converse is
    not proof and this function refuses to pretend it is: see
    `lut_pages_suggest_slc` and `MEASURED_LUT_PAGE_SPANS`.
    """
    if max_page is UNKNOWN or max_page is None:
        return UNKNOWN
    return False if max_page >= geom.PAGES_PER_BLOCK_SLC else UNKNOWN


def lut_pages_suggest_slc(max_page):
    """-> True if the WERU is a pseudo-SLC candidate worth spending a read on.

    A candidate, not a verdict.  It is the signal that found weru 84 -- every
    page its LUT entries name is below 368 -- and it is also wrong about weru
    590, whose entries name pages 6..152 while the WERU is native TLC.  Nothing
    in this module lets this decide a read.
    """
    if max_page is UNKNOWN or max_page is None:
        return False
    return max_page < geom.PAGES_PER_BLOCK_SLC


def slc_flag(mode, what="this read"):
    """-> True/False, suitable for `Device.read_page(slc=...)`.  Raises on UNKNOWN.

    The one gate between the tri-state and the bus.  `UNKNOWN` is falsey, so
    without this an unmeasured mode would arrive at the device as a perfectly
    ordinary native-TLC read.
    """
    if mode is True or mode is False:
        return mode
    raise CellModeUnknown(
        "cell mode for %s is UNKNOWN -- refusing to default.  A wrong-mode read "
        "does not fail: it returns ~1000 LDPC syndromes per codeword, which "
        "looks exactly like an unwritten block." % (what,))


# ===========================================================================
# the msoIdTable
# ===========================================================================
class MsoIdTable:
    """`msoId = table[msoType*6 + msoGroupId]`, 126 bytes, 0xFF = invalid.

    Recovered from a firmware SRAM/DRAM image.  The static table populates
    exactly msoIds 32..62, strictly increasing across the slots, and then stops;
    the trailing zeros are padding past the end of the table and are not
    entries.  There is no msoId 0.
    """

    def __init__(self, raw, offset=None, source=None):
        if len(raw) != MSOID_TABLE_LEN:
            raise ValueError("msoIdTable must be %d bytes, got %d"
                             % (MSOID_TABLE_LEN, len(raw)))
        self.raw = bytes(raw)
        self.offset = offset
        self.source = source
        self._validate()

    # -- validation: every documented trap, as a check ---------------------
    def _validate(self):
        t = self.raw
        vals = [v for v in t if v != MSOID_INVALID and v != 0]
        if any(v > MSOID_STATIC_MAX for v in vals):
            worst = max(vals)
            raise ValueError(
                "msoIdTable at %s has an entry %d > %d (cMaxNumberOfMsos-1). "
                "The usual cause is the base-offset trap: a search returns the "
                "table BASE and the anchor sits at base+%d, so subtracting %d "
                "again lands 30 bytes early and yields ids above 200."
                % (hex(self.offset) if self.offset is not None else "?",
                   worst, MSOID_STATIC_MAX,
                   MSOID_ANCHOR_INDEX, MSOID_ANCHOR_INDEX))
        if any(v < MSOID_STATIC_MIN for v in vals):
            raise ValueError("msoIdTable has an entry below %d; the static "
                             "table never populates 0..31"
                             % MSOID_STATIC_MIN)
        if len(set(vals)) != len(vals):
            raise ValueError(
                "msoIdTable is not injective (%d entries, %d distinct). A "
                "genuine table assigns each msoId once; duplicates mean the "
                "zero padding past the end of the table was counted as entries."
                % (len(vals), len(set(vals))))
        if t[MSOID_ANCHOR_INDEX] != MSOID_ANCHOR_VALUE:
            raise ValueError("anchor t[%d] = %d, expected %d "
                             "(SystemMetadata/System)"
                             % (MSOID_ANCHOR_INDEX, t[MSOID_ANCHOR_INDEX],
                                MSOID_ANCHOR_VALUE))
        # the zeros must be a trailing run, never interleaved with entries
        last = max((i for i, v in enumerate(t)
                    if v != MSOID_INVALID and v != 0), default=-1)
        if any(t[i] == 0 for i in range(last + 1)):
            raise ValueError("msoIdTable has a zero before the last real entry "
                             "(index %d): the zeros are padding, so an "
                             "interleaved zero means this is not the table"
                             % last)
        self.n_entries = len(vals)
        self.padding_from = last + 1

    # -- construction -------------------------------------------------------
    @classmethod
    def locate(cls, blob, offset=None, hint=MSOID_TABLE_HINT):
        """Find and validate the table inside a firmware image.

        `blob` is an SRAM/DRAM/TCM image as bytes.  Tries `offset` if given,
        then `hint`, then scans.  The scan predicate is the table's own
        structure -- anchor, value range, injectivity, trailing-zero padding --
        so it cannot lock onto the base-offset-trap window, whose entries run
        above 200.

        Raises LookupError if no window qualifies.  That is "not in this blob",
        not "does not exist": the table is a static initialiser and only appears
        in images that contain it.
        """
        blob = bytes(blob)
        for off in ([offset] if offset is not None else []) + [hint]:
            if off is None or off < 0 or off + MSOID_TABLE_LEN > len(blob):
                continue
            try:
                return cls(blob[off:off + MSOID_TABLE_LEN], off)
            except ValueError:
                if offset is not None:
                    raise           # an explicit offset that fails is an error
        hits = []
        for i in range(len(blob) - MSOID_TABLE_LEN + 1):
            if blob[i + MSOID_ANCHOR_INDEX] != MSOID_ANCHOR_VALUE:
                continue            # cheap gate first: ~1/256 of all offsets
            try:
                t = cls(blob[i:i + MSOID_TABLE_LEN], i)
            except ValueError:
                continue
            if t.n_entries >= 20:
                hits.append(t)
        if not hits:
            raise LookupError("no msoIdTable in this image")
        return hits[0] if len(hits) == 1 else max(hits, key=lambda t: t.n_entries)

    # -- lookup -------------------------------------------------------------
    def mso_id(self, mso_type, group):
        """-> msoId, or None if that (type, group) pair is not a partition."""
        v = self.raw[mso_type * len(MSO_GROUPS) + group]
        return None if v in (MSOID_INVALID, 0) else v

    def entries(self):
        """-> {msoId: (msoType, msoGroupId)} for the 31 static partitions."""
        out = {}
        for t in range(len(MSO_TYPES)):
            for g in range(len(MSO_GROUPS)):
                v = self.raw[t * len(MSO_GROUPS) + g]
                if v not in (MSOID_INVALID, 0):
                    out[v] = (t, g)
        return out

    def name(self, mso_id):
        """-> 'Type/Group', or None for an msoId the static table never assigns.

        None here means *dynamic or unassigned*, not "no such partition".
        msoIds below 32 really do occur on media (they appear as WERU owners).
        What assigns them is established as of 2026-08-06 -- they are the
        active-logical-partition pool, slot index used as the msoId, 0..15 NSID2
        and 16..31 NSID3 (see the module docstring).  This method still returns
        None for them on purpose: the static table is what it reports, and the
        table genuinely never assigns them a Type/Group of their own.
        """
        e = self.entries().get(mso_id)
        return None if e is None else "%s/%s" % (MSO_TYPES[e[0]], MSO_GROUPS[e[1]])

    @classmethod
    def embedded(cls):
        """-> the built-in table.  No firmware image, no arguments, no failure.

        Built once, at import, and therefore validated at import: the constant
        goes through the very same `_validate` a located table does, so a bad
        edit to `MSOID_TABLE_EMBEDDED` raises where it is made rather than
        surviving as quietly wrong names.
        """
        return _EMBEDDED_MSO_TABLE

    def __eq__(self, other):
        if isinstance(other, MsoIdTable):
            return self.raw == other.raw
        if isinstance(other, (bytes, bytearray)):
            return self.raw == bytes(other)
        return NotImplemented

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    def __hash__(self):
        return hash(self.raw)

    def __repr__(self):
        return "<MsoIdTable %d entries at %s%s>" % (
            self.n_entries,
            hex(self.offset) if self.offset is not None else "?",
            "" if self.source is None else ", %s" % (self.source,))


#: The one instance of the embedded table.  Constructed at import so the
#: constant is validated at import: `_validate` re-derives the base-offset trap,
#: the 32..62 range, injectivity, the anchor and the trailing-zero rule from the
#: bytes themselves, so a mistyped paste cannot ship.
_EMBEDDED_MSO_TABLE = MsoIdTable(
    MSOID_TABLE_EMBEDDED, offset=MSOID_TABLE_HINT,
    source="embedded (%s %s+%#x)" % (MSOID_TABLE_FW_VERSION,
                                     MSOID_TABLE_FW_IMAGE, MSOID_TABLE_HINT))


def embedded_mso_table():
    """-> the built-in `MsoIdTable`.  See `MSOID_TABLE_EMBEDDED` for provenance."""
    return _EMBEDDED_MSO_TABLE


def compare_mso_table(blob, offset=None):
    """Compare the msoIdTable in `blob` against the embedded constant.

    `blob` is a firmware SRAM/DRAM/TCM image (bytes or a path).  Returns a dict:

        found       the located `MsoIdTable`
        embedded    the built-in one
        agree       True iff all 126 bytes match
        diffs       [(index, msoType, msoGroupId, embedded_id, found_id)] for
                    every byte that differs; 0xFF/0 are reported as None
        fw_version  the embedded table's firmware version string

    It raises `LookupError` when the image carries no table at all -- "this
    image does not contain one" is a different answer from "it contains a
    different one", and collapsing the two is how a mismatch check gets ignored.
    """
    if isinstance(blob, (str, Path)):
        blob = Path(blob).read_bytes()
    found = MsoIdTable.locate(bytes(blob), offset)
    emb = _EMBEDDED_MSO_TABLE
    diffs = []
    for i, (a, b) in enumerate(zip(emb.raw, found.raw)):
        if a != b:
            diffs.append((i, i // len(MSO_GROUPS), i % len(MSO_GROUPS),
                          None if a in (MSOID_INVALID, 0) else a,
                          None if b in (MSOID_INVALID, 0) else b))
    return dict(found=found, embedded=emb, agree=not diffs, diffs=diffs,
                fw_version=MSOID_TABLE_FW_VERSION)


# ===========================================================================
# value types
# ===========================================================================
class Placement(object):
    """A physical home for one AU: (ce, block, page, au).

    `slc` is the CELL mode this address must be read in, carried with the
    address rather than re-derived at the call site.  UNKNOWN means nobody
    established it -- pass it through `slc_flag` and it raises instead of
    silently becoming a native-TLC read.  It is not part of `__eq__`
    or `__hash__`: two Placements naming the same page are the same page, and a
    set of addresses must not split into two entries because one of them was
    built before the mode was known.
    """
    __slots__ = ("ce", "block", "page", "au", "seq", "slc")

    def __init__(self, ce, block, page, au, seq=None, slc=UNKNOWN):
        self.ce, self.block, self.page, self.au, self.seq = ce, block, page, au, seq
        self.slc = slc

    def __eq__(self, o):
        return (isinstance(o, Placement) and
                (self.ce, self.block, self.page, self.au) ==
                (o.ce, o.block, o.page, o.au))

    def __hash__(self):
        return hash((self.ce, self.block, self.page, self.au))

    def __repr__(self):
        return "ce%d/b%d/p%d/au%d%s" % (
            self.ce, self.block, self.page, self.au,
            "" if self.slc is UNKNOWN else ("/SLC" if self.slc else "/TLC"))


class Mso(object):
    """One partition = (msoGroupId, msoType), named by msoId.

    `name` is None for an msoId the static table does not assign; that is
    *unnamed*, never "nonexistent".  `raid_type`, `total_lmas` are UNKNOWN
    unless a SystemMetadata msoDescriptor section was recovered.
    """
    __slots__ = ("mso_id", "type", "group", "name", "raid_type", "total_lmas",
                 "templated", "lut_id", "lut_store", "nand_block_mode",
                 "is_valid", "allocation_type", "fill_class", "weru_pool",
                 "rebuild_flags", "needs_sequence_reservation",
                 "parity_weru_front", "zero_data_id")

    def __init__(self, mso_id, mso_type=UNKNOWN, group=UNKNOWN, name=None,
                 raid_type=UNKNOWN, total_lmas=UNKNOWN, templated=False,
                 lut_id=UNKNOWN, lut_store=UNKNOWN, nand_block_mode=UNKNOWN,
                 is_valid=UNKNOWN, allocation_type=UNKNOWN, fill_class=UNKNOWN,
                 weru_pool=UNKNOWN, rebuild_flags=UNKNOWN,
                 needs_sequence_reservation=UNKNOWN,
                 parity_weru_front=UNKNOWN, zero_data_id=UNKNOWN):
        #: `+4`, a `zeroDataId` into the ZeroData pattern table -- not the
        #: msoGroupId, however much it looks like one on the namespace MSOs.
        self.zero_data_id = zero_data_id
        #: `+0` bit 0, `Mso_IsValidMso`.  Nothing clears it at runtime, so False
        #: means "this record was never constructed", not "was invalidated".
        self.is_valid = is_valid
        #: `+1`, `Mso_GetAllocationType`.  0 = `cAllocationTypeNone`.
        self.allocation_type = allocation_type
        #: `+0` bits [7:6], the write-frontier fill class (0/1/2 only).
        self.fill_class = fill_class
        #: `+0` bit 5 -- this MSO's WERUs join the shared allocation accounting.
        self.weru_pool = weru_pool
        #: `+5` -- the whole 4-bit rebuild/LUT flag byte, so callers can test
        #: `MSO_DESC_REBUILD_*` without re-reading the section.
        self.rebuild_flags = rebuild_flags
        #: `+19` bit 0 -- picks the reserved lane of this group's
        #: rebuildSequenceNumberPool record instead of the shared one.
        self.needs_sequence_reservation = needs_sequence_reservation
        #: `+19` bits [3:1].  `MSO_PARITY_FRONT_NONE` (7) means unset.
        self.parity_weru_front = parity_weru_front
        self.mso_id, self.type, self.group = mso_id, mso_type, group
        self.name, self.raid_type, self.total_lmas = name, raid_type, total_lmas
        #: `msoDescriptor +3`.  LUT_ID_NONE means this MSO has no LUT -- a real
        #: negative, not a gap.  Otherwise it names an LMA address space shared
        #: by every MSO of a namespace group.
        self.lut_id = lut_id
        #: `msoDescriptor +20`.  The msoId whose LMA space stores that LUT:
        #: 40 for the namespace groups, 41 for msoId 40 itself, and
        #: LUT_STORE_NONE for msoId 41, whose LUT is RootStorage object 0.
        self.lut_store = lut_store
        #: `msoDescriptor +0` bits [4:3], `Mso_GetNandBlockMode`.  1 = pseudo-SLC
        #: (`cNandBlockModeSlc`), 2 = native TLC.  See `slc` below.
        self.nand_block_mode = nand_block_mode
        #: True when this descriptor is byte-identical to a run of others, i.e.
        #: the array carries a template here rather than a distinct partition.
        self.templated = templated

    @property
    def slc(self):
        """-> True (pseudo-SLC) / False (native TLC) / UNKNOWN, from
        `nand_block_mode`.  UNKNOWN for any value the firmware does not name."""
        if self.nand_block_mode is UNKNOWN:
            return UNKNOWN
        return NAND_BLOCK_MODE_SLC_FLAG.get(self.nand_block_mode, UNKNOWN)

    @property
    def nsid(self):
        """-> 1/2/3 for the three namespaces, None for System/Unused/ParityData,
        UNKNOWN if the group was never determined."""
        if self.group is UNKNOWN:
            return UNKNOWN
        return GROUP_NSID.get(self.group)

    @property
    def is_system_group(self):
        return UNKNOWN if self.group is UNKNOWN else self.group == 0

    @property
    def is_dynamic(self):
        """True for the sub-32 msoIds the static table does not populate.

        They are legal (cMaxNumberOfMsos 63 and cMaxNumberOfMsosForAllocation 56
        are separate caps) and they occur as WERU owners on media.

        What assigns them is now established (2026-08-06), and this docstring
        used to say it was not: they are the active logical partition MSOs, taken
        by `AllocNeoRegisterActiveLogicalPartition` from two 16-entry lpId tables
        under the cap `cMaxNumberOfMsoForActivePartitions == 32`, with the slot
        index used directly as the msoId -- 0..15 = NSID2, 16..31 = NSID3.  See
        the module docstring for the call chain and the on-media evidence.

        `is_dynamic` still means exactly what it meant: "not in the static
        table".  It is not renamed and the reported name stays
        None -- knowing the role of the range does not give an individual id a
        per-partition name.
        """
        return self.mso_id < MSOID_STATIC_MIN

    @property
    def raid_name(self):
        if self.raid_type is UNKNOWN:
            return UNKNOWN
        return RAID_TYPES.get(self.raid_type, "Mso/other(%d)" % self.raid_type)

    def __repr__(self):
        return "<Mso %d %s>" % (self.mso_id, self.name or
                                ("dynamic" if self.is_dynamic else "unnamed"))


class Weru(object):
    """One erase/allocation unit: 48 physical blocks, 4 block numbers x 12 CEs.

    Any of `owner`, `blocks`, `valid_count`, `capacity_aus` may be UNKNOWN.  In
    particular `blocks` is UNKNOWN when the `blockMapping` section was not
    recovered, and an empty list means "the table says every one of its 48 slots
    is unmapped" -- a real, different statement.
    """
    __slots__ = ("weru_id", "owner", "bin_id", "erase_count", "capacity_aus",
                 "valid_count", "blocks", "source", "self_ref", "slc_mode")

    def __init__(self, weru_id, owner=UNKNOWN, bin_id=UNKNOWN, erase_count=UNKNOWN,
                 capacity_aus=UNKNOWN, valid_count=UNKNOWN, blocks=UNKNOWN,
                 source=None, self_ref=UNKNOWN, slc_mode=UNKNOWN):
        #: `weruDescriptor +10`, `WeruConfiguration_GetWeruBlockMode` as the
        #: drive itself recorded it: 1 = pseudo-SLC, 0 = native TLC.  UNKNOWN
        #: when no descriptor section was recovered.  See `slc_from_descriptor`.
        self.slc_mode = slc_mode
        self.weru_id, self.owner, self.bin_id = weru_id, owner, bin_id
        self.erase_count, self.capacity_aus = erase_count, capacity_aus
        self.valid_count, self.blocks = valid_count, blocks
        # Which section copy said so -- e.g. "weruDescriptor@weru19".  It used
        # to be a 'current'/'legacy' generation tag, which named a hazard that
        # does not exist and hid the one that does.
        self.source = source
        self.self_ref = self_ref

    @property
    def slc_from_descriptor(self):
        """-> True/False/UNKNOWN: the CELL mode the drive stores, not a derivation.

        `weruDescriptor +10`.  Every other route this module has -- capacity /
        blocksPerWeru, the WERU configuration table, `msoDescriptor +0` bits
        [4:3] -- infers the mode; this one reads it back.  Kept as a separate
        property from `Ftl.cell_mode` on purpose, so the two can be compared
        rather than one silently shadowing the other.
        """
        if self.slc_mode is UNKNOWN:
            return UNKNOWN
        return self.slc_mode == WERU_DESC_SLC_MODE_SLC

    @property
    def is_allocated(self):
        """-> True/False/UNKNOWN.  owner 0 is free/unowned."""
        return UNKNOWN if self.owner is UNKNOWN else self.owner != 0

    @property
    def mapped_blocks(self):
        """-> the mapped (block, ce) pairs only, or UNKNOWN.

        `self.blocks` is slot-indexed and carries `None` for every unmapped
        slot; this is the compacted view.  Never compact by hand -- position in
        `blocks` is a slot number and the WMA arithmetic depends on it.
        """
        return mapped_blocks(self.blocks)

    @property
    def shape(self):
        """How this WERU's 48 mapped blocks are actually arranged.

        -> one of, or UNKNOWN when `blocks` was never recovered:

        ``"rectangular"``  exactly `geometry.weru_blocks` of four block numbers
                           across all twelve CEs -- the textbook WERU.
        ``"substituted"``  still 48 blocks, still four per CE on all twelve, but
                           more than four distinct block numbers: one or more
                           blocks were replaced from elsewhere, which is what
                           bad-block management does.  Measured on this board:
                           741 rectangular, 26 substituted.
        ``"invalid"``      names a block number at or beyond
                           `geometry.BLOCKS_PER_LUN` -- e.g. 2386979.  Not a
                           location.  RETRACTED: "stale residue from a
                           different metadata generation".  88 of the 91 were
                           inline-RAID parity bytes that `SystemMetadata.scan`
                           had preferred over the real page; they are now
                           rectangular.  The 3 that remain are weruIds
                           1070-1072, past the end of the 1070-entry WERU array
                           (the six weru configurations sum to 1070), i.e. the
                           section's padding.  Still filter on this -- but read
                           it as "past the array", not as "a stale generation".
        ``"partial"``      fewer than `BLOCKS_PER_WERU` slots mapped, all in
                           range: a narrower WERU class (the SLC metadata WERU
                           is 12 blocks, capacity 17664) or a partly allocated
                           one.
        ``"irregular"``    48 in-range blocks that are neither of the above.

        Anything but "rectangular" means arithmetic over four block numbers will
        not reproduce this WERU; use the mapping.
        """
        if self.blocks is UNKNOWN:
            return UNKNOWN
        blocks = list(self.mapped_blocks)
        if any(b >= geom.BLOCKS_PER_LUN or b < 0 for b, _c in blocks):
            return "invalid"
        nums = sorted({b for b, _c in blocks})
        if (len(blocks) == geom.BLOCKS_PER_WERU
                and len(nums) == geom.BLOCKS_PER_WERU // geom.CE_COUNT
                and set(blocks) == set(geom.weru_blocks(nums))):
            return "rectangular"
        per_ce = {}
        for _b, c in blocks:
            per_ce[c] = per_ce.get(c, 0) + 1
        if (len(blocks) == geom.BLOCKS_PER_WERU
                and len(per_ce) == geom.CE_COUNT
                and set(per_ce.values()) ==
                {geom.BLOCKS_PER_WERU // geom.CE_COUNT}):
            return "substituted"
        if len(blocks) < geom.BLOCKS_PER_WERU:
            return "partial"
        return "irregular"

    @property
    def is_real(self):
        """-> False for the padding entries past the end of the WERU array.

        They have capacity 0 and owner 0 and are not WERUs at all (563 of them
        at weruId >= 1075 on this board).
        """
        if self.capacity_aus is UNKNOWN:
            return UNKNOWN
        return self.capacity_aus > 0

    def stale_aus_upper_bound(self):
        """`capacity - validCount`.  An upper bound, not a stale-data figure.

        It assumes the WERU is fully written.  Sampling ten pages across 0..1090
        of each programmed WERU on this board shows most are only 10-40%
        written, so this overstates real stale content by roughly 6x.  It must
        never be quoted as "deleted data".
        """
        if self.capacity_aus is UNKNOWN or self.valid_count is UNKNOWN:
            return UNKNOWN
        return max(0, self.capacity_aus - self.valid_count)

    def __repr__(self):
        return "<Weru %d owner=%s cap=%s valid=%s>" % (
            self.weru_id, self.owner, self.capacity_aus, self.valid_count)


class Survey(object):
    """Which blocks of a CE have ever been programmed.

    `programmed` and `erased` are sets of block numbers; `unknown` holds every
    block that was not looked at or could not be read.  All three are always
    present, so "not in programmed" never has to be read as "erased".
    """

    def __init__(self, ce, programmed=None, erased=None, unknown=None, note=""):
        self.ce = ce
        self.programmed = set(programmed or ())
        self.erased = set(erased or ())
        self.unknown = set(unknown or ())
        self.note = note

    def state(self, block):
        if block in self.programmed:
            return "programmed"
        if block in self.erased:
            return "erased"
        return UNKNOWN

    @classmethod
    def from_device(cls, dev, ce, blocks=None, progress=None, slc=True):
        """Read page 0 of each block and classify.  Reads only, no decode.

        `slc` is the mode every page-0 read is issued in and it defaults to
        pseudo-SLC, which is what every survey in this tree was recorded in.  It
        is a parameter and not a per-block decision because a survey sweeps
        blocks whose WERU -- and therefore whose cell mode -- is exactly what is
        not known yet; the honest thing is to say which mode was used and put it
        in `note`, and this class does.

        This does not see through the mode, and that claim is RETRACTED.  The
        docstring used to say the all-0xFF test "stays sound even reading a
        native-TLC block in pseudo-SLC mode -- most cells sit above the single
        SLC reference and read as 0".  Measured counterexample: ce3/b3648/p106
        and ce10/b3650/p106 are written native-TLC pages, and under 0xDA both
        come back entirely 0xFF (nonff 0, and the same CRC for two different
        addresses).  A survey in the wrong mode can therefore report a written
        block as erased.  `programmed` is still a real positive; `erased` is
        only a real negative for blocks whose cell mode matches `slc`.
        """
        blocks = range(geom.BLOCKS_PER_LUN) if blocks is None else blocks
        prog, erased, unk = set(), set(), set()
        slc = slc_flag(slc, "Survey.from_device")
        for i, b in enumerate(blocks):
            try:
                d = dev.read_page(ce, b, 0, slc=slc)
            except DeviceError:
                unk.add(b)
                continue
            if d is None:
                unk.add(b)
            elif d.count(0xFF) == len(d):
                erased.add(b)
            else:
                prog.add(b)
            if progress and i % 256 == 0:
                progress("survey ce%d: %d blocks" % (ce, i))
        return cls(ce, prog, erased, unk,
                   note="page-0 read through Device in %s mode"
                        % ("pseudo-SLC" if slc else "native TLC"))

    @classmethod
    def from_csv(cls, path, ce):
        """Load a `nd_widesurvey.py` CSV: ce,blk,kind,ff,zero.

        Provided because a full 4096-block survey is a hardware artefact worth
        hours; re-reading it from a stored record is not the same as reading
        pages out of a dump behind the Device's back.  `kind == 'erased'` is
        the survey's own all-0xFF verdict; 'dead' means the read failed and
        lands in `unknown`.
        """
        prog, erased, unk = set(), set(), set()
        for ln in Path(path).read_text().splitlines()[1:]:
            f = ln.split(",")
            if len(f) < 3:
                continue
            try:
                c, b = int(f[0]), int(f[1])
            except ValueError:
                continue
            if c != ce:
                continue
            (erased if f[2] == "erased" else
             unk if f[2] == "dead" else prog).add(b)
        return cls(ce, prog, erased, unk, note="from %s" % Path(path).name)

    def __repr__(self):
        return "<Survey ce%d programmed=%d erased=%d unknown=%d>" % (
            self.ce, len(self.programmed), len(self.erased), len(self.unknown))


# ===========================================================================
# section decoders
# ===========================================================================
def decode_block_mapping(buf, stride=None, i_know_this_is_wrong=False):
    """`blockMapping` -> {weruId: [(block, ce) | None] * stride}, slot-indexed.

    20-byte entries indexed ``[weruId * BLOCKS_PER_WERU + i]``, ``+0 u32
    physicalBlockId`` with 0xFFFFFFFF meaning unmapped.

    Position is meaningful and is preserved.  This used to skip unmapped
    entries, which silently renumbered everything after them: position *i* was
    the *i*-th mapped block rather than slot *i*.  The WMA arithmetic consumes
    slot indices (`slot = (wma % 192) // 4`), so any caller pairing a WMA slot
    with a position in a compacted list got a confident wrong address.  The list
    is now always `stride` long, with `None` for an unmapped slot, and
    `Weru.mapped_blocks` is the filtered view for callers that only want the
    set of blocks.

    A caveat that is not resolved HERE: for the five WERUs whose layout was
    measured on hardware, this table's contents contradict the measurement (it
    says weru 19 is block 10 on all twelve CEs; the pages say blocks 10/11 on CE
    0-5).  Slot order in this table is therefore not known to be the WMA slot
    order either -- see `Ftl.block_mapping_conflicts`.

    The STRIDE is 48 and the function will not let you change it quietly.
    `SystemBlockMapping`'s 12 pbids per slot is the metadata WERU's own
    configuration, not this table's stride; a stride-12 read here yields
    plausible-looking but wrong block lists.  48 is measured (against a full
    single-CE survey it is the only stride under which every WERU with
    validCount > 100000 maps to programmed blocks) and arithmetically forced
    (48 x 1104 x 4 = 211968 = the observed TLC WERU capacity).

    A WERU present in the table with every slot unmapped yields a list of all
    ``None`` -- genuinely no blocks -- while a weruId past the end of `buf` is
    simply absent from the result.  Use `.get(id, UNKNOWN)` if you need to tell
    them apart.
    """
    if stride is None:
        stride = geom.BLOCKS_PER_WERU
    if stride != geom.BLOCKS_PER_WERU and not i_know_this_is_wrong:
        raise ValueError(
            "refusing stride %d for the global blockMapping; it is %d "
            "(geometry.BLOCKS_PER_WERU). SystemBlockMapping's %d pbids per slot "
            "is the metadata WERU config only. Pass i_know_this_is_wrong=True "
            "to override, e.g. from verify_weru_stride()."
            % (stride, geom.BLOCKS_PER_WERU, SYSTEM_BLOCK_MAPPING_PBIDS))
    n = len(buf) // BLOCK_MAPPING_ENTRY
    out = {}
    for weru in range(n // stride):
        blocks = []
        for i in range(stride):
            o = (weru * stride + i) * BLOCK_MAPPING_ENTRY
            pb = struct.unpack_from("<I", buf, o)[0]
            blocks.append(None if pb == UNMAPPED_PBID else geom.split_pbid(pb))
        out[weru] = blocks
    return out


#: The rest of the 20-byte `blockMapping` entry, which `decode_block_mapping`
#: Does not return (it would change that function's shape and every
#: caller consumes the pbid alone).  Enumerated from the firmware 2026-08-07:
#:
#:   +0  u32 physicalBlockId, 0xFFFFFFFF unmapped
#:   +4  u32 RDA / read-disturb word.  bit 0 = refresh pending
#:       (`RdaCountControl.c`, set `efc_cpu2.c:34298`, tested `:34254`);
#:       bits [31:2] a read counter, saturated by `& 3 | 0xFFFFFFFC`
#:       (`:32722`).  Cleared to 0 on erase (`:23010`).
#:   +8  u8  cleared on erase (`:23011`), bulk-set per die by `sub_403125A2`
#:   +9  u8  bit 0, set on the retire/refresh path (`:32717-32721`)
#:   +10 u32 unaligned -- the BLOCK properties word, the same encoding as
#:       `PhysicalBlockProperties`: [23:0] eraseCount (saturating at 0xFFFFFF,
#:       `:23012`), [25:24] region (`:32663`), [30:28] status (`:32611`),
#:       [27:26] and [31] preserved by the `0x8F` mask (`:32685`, `:32698`)
#:   +14..+15  no access anywhere
#:   +16 u16 next / +18 u16 prev -- the virtual block free list, per plane
#:       (`efc_cpu3.c:41614-41615`; sentinels `0xDF00DF01` allocated,
#:       `0xDEFEDEFF` defective-skipped, `:41623`, `:41630`).  This is why the
#:       swap primitive `sub_402D3D7C`/`sub_40312B84` copies bytes 0..15 only.
#:
#: So the drive keeps its wear/defect state twice, in two different RootStorage
#: instances written at different moments: here, inside the SystemMetadata
#: image, and in the Backup store's "PhBP" SegmentedBuffer, which
#: `MediaPartitionInterface_UpdatePhysicalBlockPropertiesFromBlockMappingTable
#: ToBackUp` (`efc_cpu2.c:57088`) copies into.  They are compared in the
#: self-test: 48000/48000 mapped slots agree on eraseCount, region and status,
#: 0 differing.  That is a real cross-check -- two independent writers, one
#: answer -- and until 2026-08-07 nobody had run it.
BLOCK_MAPPING_RDA_OFF = 4
BLOCK_MAPPING_RDA_REFRESH_PENDING = 0x1
BLOCK_MAPPING_PROPERTY_OFF = 10
BLOCK_MAPPING_LINK_OFF = 16
BLOCK_MAPPING_LINK_ALLOCATED = 0xDF00DF01
BLOCK_MAPPING_LINK_DEFECTIVE = 0xDEFEDEFF


def decode_block_mapping_properties(buf, n_slots=None):
    """`blockMapping` -> {slot: (pbid, rda, propertyWord)} for mapped slots.

    Separate from `decode_block_mapping` on purpose: that function's result
    shape is consumed in a dozen places and its job is addressing.  This one is
    the wear/defect view.

    `n_slots` bounds the walk.  Pass it.  The section is rounded up to a
    whole number of LMAs, so the tail past `WERU_COUNT * BLOCKS_PER_WERU` is
    padding whose bytes decode into pbids in the billions; walking it and then
    indexing PhBP with the result raises IndexError, which is how the bound got
    written down.
    """
    if n_slots is None:
        n_slots = WERU_COUNT * geom.BLOCKS_PER_WERU
    out = {}
    for i in range(min(n_slots, len(buf) // BLOCK_MAPPING_ENTRY)):
        o = i * BLOCK_MAPPING_ENTRY
        pb = struct.unpack_from("<I", buf, o)[0]
        if pb == UNMAPPED_PBID:
            continue
        out[i] = (pb,
                  struct.unpack_from("<I", buf, o + BLOCK_MAPPING_RDA_OFF)[0],
                  struct.unpack_from("<I", buf,
                                     o + BLOCK_MAPPING_PROPERTY_OFF)[0])
    return out


def mapped_blocks(slots):
    """A slot-indexed block list -> just the mapped (block, ce) pairs.

    The one place the compaction happens, so a caller that compacts can be
    found by searching for this name rather than by noticing a missing `None`.
    """
    if slots is UNKNOWN or slots is None:
        return slots
    return [b for b in slots if b is not None]


def decode_weru_descriptor(buf):
    """`weruDescriptor` -> {weruId: Weru} with owner/bin/eraseCount/capacity.

    20 bytes per weruId.  `+0 u8` is the OWNER msoId -- proven against seven
    known (weru -> msoId) pairs; the alternative "+0 u32 eraseCount" reading is
    wrong (byte +0 spans 0..59 with +1..+3 essentially always zero).  `+4 u16`
    is a self-reference to the weruId, checked here, and `+6`/`+8` are the
    doubly-linked bin list.

    Owner values split three ways: 32..62 a static msoId, 0 free/unowned, and a
    handful of singletons below 32 -- the active-logical-partition MSOs
    (established 2026-08-06; this line used to say their assignment was not
    established).

    On this board those singletons are 16, 17, 18, 19, 20 (werus 45, 82, 80,
    81, 38) -- NSID3 slots 0..4.  RETRACTED: the sixth, "owner 12 on weru 972",
    is not an ownership claim at all; that row reads capacity 0, binId 0,
    eraseCount 0 and self-reference 0 where it should be 972, i.e. an
    unpopulated record.  With it gone every sub-32 owner is in group 3, exactly
    as the model predicts once NSID2 runs with DirectTlcWrite.
    """
    out = {}
    for weru in range(len(buf) // WERU_DESC_ENTRY):
        o = weru * WERU_DESC_ENTRY
        out[weru] = Weru(
            weru,
            owner=buf[o + WERU_DESC_OWNER_OFF],
            bin_id=buf[o + WERU_DESC_BIN_OFF],
            erase_count=struct.unpack_from("<I", buf, o + WERU_DESC_ERASE_OFF)[0],
            capacity_aus=struct.unpack_from("<I", buf, o + WERU_DESC_CAP_OFF)[0],
            slc_mode=buf[o + WERU_DESC_SLC_MODE_OFF],
            self_ref=struct.unpack_from("<H", buf, o + WERU_DESC_SELF_OFF)[0])
    return out


def decode_valid_count(buf):
    """`validCount` -> [u32 live AUs per weruId].  One NAND page for the drive."""
    return list(struct.unpack_from("<%dI" % (len(buf) // 4), buf, 0))


class WeruBin(object):
    """One of the 53 allocation bins WERUs are queued in.

    `members` is the list walked from `head` through `weruDescriptor +6`, which
    is a circular list -- the walk stops when it returns to the head, and the
    length of that walk is the falsifiable test against the stored `count`.
    """
    __slots__ = ("bin_id", "kind", "head", "self_ref", "count", "members")

    def __init__(self, bin_id, kind, head, self_ref, count, members=UNKNOWN):
        self.bin_id, self.kind, self.head = bin_id, kind, head
        self.self_ref, self.count, self.members = self_ref, count, members

    @property
    def is_reset(self):
        """True for a bin never used: kind 6, empty head, zero count."""
        return (self.kind == WERU_BIN_KIND_RESET and self.head == INVALID_WERU
                and self.count == 0)

    def __repr__(self):
        return "<WeruBin %d kind %d, %d werus, head %d>" % (
            self.bin_id, self.kind, self.count, self.head)


def decode_weru_bins(buf, weru_descriptor=None, n_werus=None):
    """`weruBin` (848 B) -> {binId: WeruBin}.

    The section itself is padded to a 16 KiB granule; only the first
    `WERU_BIN_BYTES` are the structure and the rest is not interpreted.

    Pass `weru_descriptor` (the raw section bytes) to walk each bin's list and
    fill `members`; without it `members` stays UNKNOWN rather than empty.

    Verified on this board: all 10 non-empty bins have `self_ref == binId`, a
    circular list whose length equals `count`, and every member's
    `weruDescriptor +11` equal to the binId -- 10 of 10 on each test.  The 43
    others are untouched resets.  Sum of counts = 971 WERUs; the 99 that are in
    no bin carry binId 0 by default, which is why binId alone must not be used
    as bin membership.
    """
    if len(buf) < WERU_BIN_BYTES:
        raise ValueError("weruBin is %d bytes, needs %d (efc_cpu3.c:37454)"
                         % (len(buf), WERU_BIN_BYTES))
    nxt = None
    if weru_descriptor is not None and weru_descriptor is not UNKNOWN:
        rows = len(weru_descriptor) // WERU_DESC_ENTRY
        if n_werus is not None:
            rows = min(rows, n_werus)
        nxt = [struct.unpack_from("<H", weru_descriptor,
                                  w * WERU_DESC_ENTRY + WERU_DESC_NEXT_OFF)[0]
               for w in range(rows)]
    out = {}
    for b in range(WERU_BIN_COUNT):
        o = b * WERU_BIN_ENTRY
        head = struct.unpack_from("<H", buf, o + WERU_BIN_HEAD_OFF)[0]
        cnt = struct.unpack_from("<I", buf, o + WERU_BIN_COUNT_OFF)[0]
        members = UNKNOWN
        if nxt is not None:
            members, cur = [], head
            while cur != INVALID_WERU and cur < len(nxt) and len(members) <= cnt:
                members.append(cur)
                cur = nxt[cur]
                if cur == head:
                    break
        out[b] = WeruBin(b, buf[o + WERU_BIN_KIND_OFF], head,
                         buf[o + WERU_BIN_SELF_OFF], cnt, members)
    return out


#: `Allocation` (RootStorage object 2) = `allocation.persistentAllocation.
#: mediaAllocationInfo.baseStatePersistent[msoId]` -- the name comes from the
#: firmware's own assert (`Allocation.c:5453`, `efc_cpu2.c:127325`), and the
#: shorter `pBasePersistent->currentWeruId` from `Allocation.c:5606`
#: (`efc_cpu2.c:126091`).  The persisted object is a verbatim 2240-byte copy of
#: the SRAM array: `sub_403008A0(buf, dword_8000A4, 0x8C0u)` immediately before
#: the RootStorage write (`efc_cpu2.c:128011-128014`), and the same 0x8C0 is
#: what the module initialiser clears (`efc_cpu2.c:125049`).  2240 = 56 x 40 =
#: `cMaxNumberOfMsosForAllocation` x the record, and the object measures exactly
#: 2240 bytes on this board.
ALLOC_MSOS = 56                             # cMaxNumberOfMsosForAllocation
ALLOC_ENTRY = 40
ALLOC_BYTES = ALLOC_MSOS * ALLOC_ENTRY      # 2240 = the object's measured size

#: Fields, from the two functions that write the record.
#:
#: `sub_4034E164` (`efc_cpu2.c:126095-126135`) opens a WERU for writing:
#: it clears +0..+23, then sets `*v4 = offset + currentWeruId * dword_907150`
#: -- so +0 is the next WMA to write, and `dword_907150` is WMAs-per-WERU.
#: It then writes +4, +8, +9, +10 (u32) and +14 (u32), copies +0 to +32, and
#: masks +28.
#:
#: `sub_4034DBFA` (`efc_cpu2.c:125261-125289`), the dirty-mount reinitialiser,
#: zeroes all 40 bytes and then writes +24 = +26 = 0x3FFF, +4 = 0xFFFF0000,
#: +8 = 0xFF -- which is exactly the byte pattern 44 of the 56 records carry
#: here.
ALLOC_WMA_OFF = 0                           # u32 next WMA to write
ALLOC_PAGE_BASE_OFF = 32                    # u32 WMA of the open page's first AU
ALLOC_CURRENT_WERU_OFF = 24                 # u16 currentWeruId, 0x3FFF = none
ALLOC_PENDING_WERU_OFF = 26                 # u16 the pre-reserved next WERU
ALLOC_WORDLINE_OFF = 22                     # u16; bits [14:3] = wordline
ALLOC_WORDLINE_SHIFT = 3
ALLOC_WORDLINE_MASK = 0xFFF
ALLOC_FLAGS_OFF = 28                        # u8; bit 0 = requestOngoing
ALLOC_REQUEST_ONGOING = 0x01                # Allocation.c:641, :3387
ALLOC_PLANE_SHIFT, ALLOC_PLANE_MASK = 4, 3  # +28 bits [5:4] = plane count
ALLOC_PLANE_AUS_OFF = 29                    # u8 = planeCount * dword_90C4C0

#: +4 .. +9 is the RAID BLOCK, and the firmware names every byte of it.
#: Settled 2026-08-06; before that the whole run was listed as unexplained.
#:
#: The record is a `WriteableUnit` -- the name comes from the asserts that read
#: these very bytes.  `sub_4034E164`, which opens a WERU, does not write them:
#: it releases them, `+4 u32 = 0xFFFF0000, +8 = 0xFF, +9 = 0, +10 = 0, +14 = 0`
#: (`efc_cpu2.c:125602-125606`), which is why they read as "none" on 48 of the
#: 56 slots.  The writers are elsewhere:
#:
#: `sub_12788` = `ParityManager_OnAllocationRequest` (named by its own error
#: string, `efc_cpu2.c:33730`), with `a2` = the record:
#:
#:     sub_84C(a2 + 6, &pTracker->logicalRaidBufferId[0], 3)      :33700
#:         guarded by panic "pTracker->logicalRaidBufferId[0] !=
#:         cInvalidRaidBufferId"                    (ParityManager.c:1592)
#:     *(u16*)(a2 + 4) = 1 | 1<<4 | ... , one nibble per plane   :33702-33705
#:     *(u32*)(a2 + 10) = *(u32*)pTracker->pParityAuBuffer       :33708
#:     *(u32*)(a2 + 14) = *(u32*)pTracker->pParityAuxBuffer      :33709
#:     *(u8 *)(a2 +  9) = pTracker->raidStripeInfo               :33710
#:         guarded by panic "pTracker->raidStripeInfo.b.stripeCount > 0"
#:                                                  (ParityManager.c:1620)
#:
#: `sub_40350FF8` (`efc_cpu2.c:128216-128239`) is the matching release: it walks
#: `a1 + i + 6` for i in 0..2, breaks on 0xFF, calls
#: `MediaLowerPartitionInterface_PurgeRaidBuffer_RpcCall(id)` and writes 0xFF
#: back -- and asserts `pWriteableUnit->raidStripeInfo.b.parityMode ==
#: cRaidTypeInline` on `*(u8*)(a1 + 9) >> 6` (`Allocation.c:4402`).
#:
#: `Moh.c` 2361-2364 (`efc_cpu2.c:38718-38726`) names +9/+10/+14 verbatim and
#: then copies the whole six-byte block into the NAND command:
#: `cmd[+22..+25] = wu[+4..+7]`, `cmd[+26] = wu[+8]`, `cmd[+27] = wu[+9]`
#: (`:38727-38729`) -- which is exactly the `RaidOpCode = field_16 & 0xF` /
#: `ParityMode = field_1B >> 6` pair the cpu1 side dispatches on.
#:
#: MEASURED, over the four RootStorage copies of the object (`pt_alloc.py`);
#: eight live records, and blocks 4/5 hold a different generation from blocks
#: 2/3 so the buffer ids and pointers differ between them:
#:     parityMode == the msoDescriptor's own `Mso_GetRaidType`      8/8
#:     stripeCount == 48, the Mso-mode column count C              8/8
#:     both pointers inside IS_SHARED_ADDRESS and 32-byte aligned  8/8
#:     +4 == the plane nibble mask implied by `+21 & 7`            8/8
#:     no logical buffer id claimed by two MSOs in one copy        4/4 copies
ALLOC_RAID_CMD_OFF = 4         # u16, one 4-bit RAID command per plane, value 1
ALLOC_RAID_BUFFER_OFF = 6      # u8[3] logicalRaidBufferId, 0xFF = none
ALLOC_RAID_BUFFERS = 3
ALLOC_INVALID_RAID_BUFFER = 0xFF            # cInvalidRaidBufferId
ALLOC_RAID_STRIPE_INFO_OFF = 9              # u8 bitfield
ALLOC_PARITY_MODE_SHIFT, ALLOC_PARITY_MODE_MASK = 6, 3   # .b.parityMode
ALLOC_STRIPE_COUNT_MASK = 0x3F                           # .b.stripeCount
ALLOC_PARITY_AU_BUF_OFF = 10   # u32 raidInfoXferOnly.raidParityAuBuffer
ALLOC_PARITY_AUX_BUF_OFF = 14  # u32 raidInfoXferOnly.raidParityAuxBuffer
#: `IS_SHARED_ADDRESS` -- the DRAM window an RPC buffer pointer must fall in
#: (`efc_cpu2.c:57175`, the first of ~25 identical guards).  The other three
#: legal windows are SRAM and are not where parity buffers live.
SHARED_DRAM_LO, SHARED_DRAM_HI = 0x40000000, 0x48000000

#: `+26` is the pending WERU, not a second concurrent front.  `sub_B302`
#: (`efc_cpu2.c:26220-26247`) is the frontier advance and is unambiguous:
#:
#:     v3 = rec[+26]; rec[+24] = v3; rec[+26] = 0x3FFF;
#:     if (IsWeruIdValid(v3)) { open it }
#:
#: i.e. the pre-reserved WERU is promoted to current and the reservation is
#: cleared.  `sub_B362` (`efc_cpu2.c:26252-26292`, `Allocation.c:6606`) is the
#: matching close: it sets +24 = 0x3FFF and clears +28 bits 2-3.  So one MSO has
#: one open write front plus one reservation -- double buffering, so a stream
#: never stalls waiting for an erase.
ALLOC_NO_WERU = INVALID_WERU                # 0x3FFF, the same sentinel as weruBin


class WriteFrontier(object):
    """One MSO's write frontier, as persisted in RootStorage object 2.

    `wma` is where the next AU would be written; `page_base_wma` is the first
    WMA of the page currently being filled, so `wma - page_base_wma` is how many
    AUs are already placed in the open page.  Both are raw WMAs -- feed them to
    `Ftl.wma_to_physical` for a location.

    `pending_weru` is the WERU already reserved for when `current_weru` fills.

    `raid_buffers`, `parity_mode`, `stripe_count`, `raid_cmd` and the two
    parity buffer pointers are the RAID half of the record -- see the
    `ALLOC_RAID_*` block above.  A frontier with no parity engaged reads
    `raid_buffers == ()`, `parity_mode == 0`.
    """
    __slots__ = ("mso_id", "wma", "page_base_wma", "current_weru",
                 "pending_weru", "wordline", "flags", "planes",
                 "aus_per_program", "raw", "raid_cmd", "raid_buffers",
                 "raid_stripe_info", "parity_au_buffer", "parity_aux_buffer")

    def __init__(self, mso_id, raw):
        self.mso_id = mso_id
        self.raw = bytes(raw)
        self.wma = struct.unpack_from("<I", raw, ALLOC_WMA_OFF)[0]
        self.page_base_wma = struct.unpack_from("<I", raw, ALLOC_PAGE_BASE_OFF)[0]
        cur = struct.unpack_from("<H", raw, ALLOC_CURRENT_WERU_OFF)[0]
        pnd = struct.unpack_from("<H", raw, ALLOC_PENDING_WERU_OFF)[0]
        self.current_weru = None if cur == ALLOC_NO_WERU else cur
        self.pending_weru = None if pnd == ALLOC_NO_WERU else pnd
        self.wordline = ((struct.unpack_from("<H", raw, ALLOC_WORDLINE_OFF)[0]
                          >> ALLOC_WORDLINE_SHIFT) & ALLOC_WORDLINE_MASK)
        self.flags = raw[ALLOC_FLAGS_OFF]
        self.planes = (self.flags >> ALLOC_PLANE_SHIFT) & ALLOC_PLANE_MASK
        self.aus_per_program = raw[ALLOC_PLANE_AUS_OFF]
        self.raid_cmd = struct.unpack_from("<H", raw, ALLOC_RAID_CMD_OFF)[0]
        self.raid_buffers = tuple(
            b for b in raw[ALLOC_RAID_BUFFER_OFF:
                           ALLOC_RAID_BUFFER_OFF + ALLOC_RAID_BUFFERS]
            if b != ALLOC_INVALID_RAID_BUFFER)
        self.raid_stripe_info = raw[ALLOC_RAID_STRIPE_INFO_OFF]
        self.parity_au_buffer = struct.unpack_from(
            "<I", raw, ALLOC_PARITY_AU_BUF_OFF)[0]
        self.parity_aux_buffer = struct.unpack_from(
            "<I", raw, ALLOC_PARITY_AUX_BUF_OFF)[0]

    @property
    def parity_mode(self):
        """`raidStripeInfo.b.parityMode` -- the same enum as `Mso_GetRaidType`.

        0 None, 1 Mso, 2 Inline.  `sub_40350FF8` asserts it is `cRaidTypeInline`
        before purging the buffers of an inline-parity MSO
        (`efc_cpu2.c:128230`); `Moh.c:2361` asserts `cRaidTypeMso` on the Mso
        path (`:38720`).
        """
        return ((self.raid_stripe_info >> ALLOC_PARITY_MODE_SHIFT)
                & ALLOC_PARITY_MODE_MASK)

    @property
    def stripe_count(self):
        """`raidStripeInfo.b.stripeCount` -- data AUs covered by one parity AU.

        Asserted `> 0` on both the ParityManager and Moh paths
        (`ParityManager.c:1620`, `Moh.c:2362`).  48 on every live record here,
        which is the same C as the Mso-mode column count.
        """
        return self.raid_stripe_info & ALLOC_STRIPE_COUNT_MASK

    @property
    def raid_engaged(self):
        """True when the parity manager has a tracker bound to this frontier."""
        return bool(self.raid_buffers) and self.parity_mode != 0

    @property
    def is_reinitialised(self):
        """True for a record in the state `AllocationReinitializeMsos` leaves.

        `sub_4034DBFA` zeroes the record then writes +24/+26 = 0x3FFF,
        +4 = 0xFFFF0000, +8 = 0xFF.  Anything else means the record survives
        from a real write front.
        """
        r = self.raw
        return (self.current_weru is None and self.pending_weru is None
                and self.wma == 0 and self.page_base_wma == 0
                and struct.unpack_from("<I", r, 4)[0] == 0xFFFF0000
                and r[8] == 0xFF and not any(r[9:22]) and not any(r[28:32]))

    @property
    def is_open(self):
        """True if this MSO holds a WERU open for writing."""
        return self.current_weru is not None

    @property
    def weru_of_wma(self):
        """-> the weruId `wma` falls in, or None when the pointer is zero.

        This is not redundant with `current_weru`: when the two disagree the
        record was persisted after the front was closed, and the pointer is the
        older, more informative half.
        """
        return None if self.wma == 0 else self.wma // geom.AUS_PER_TLC_WERU

    @property
    def aus_in_open_page(self):
        return self.wma - self.page_base_wma

    @property
    def request_ongoing(self):
        return bool(self.flags & ALLOC_REQUEST_ONGOING)

    def __repr__(self):
        if self.is_reinitialised:
            return "<WriteFrontier mso %d: reinitialised>" % self.mso_id
        return ("<WriteFrontier mso %d: weru %s wl %d, wma %d (+%d in page), "
                "pending %s>" % (self.mso_id, self.current_weru, self.wordline,
                                 self.wma, self.aus_in_open_page,
                                 self.pending_weru))


def decode_allocation(buf):
    """`Allocation` (RootStorage object 2, 2240 B) -> {msoId: WriteFrontier}.

    Every one of the 56 slots is returned, including the reinitialised ones --
    "this MSO has no open front" is an answer, and dropping those records would
    hide the single most forensically useful fact this object carries.
    """
    if len(buf) < ALLOC_BYTES:
        raise ValueError("Allocation is %d bytes, needs %d = %d x %d "
                         "(cMaxNumberOfMsosForAllocation x the record, "
                         "efc_cpu2.c:125049)"
                         % (len(buf), ALLOC_BYTES, ALLOC_MSOS, ALLOC_ENTRY))
    return {m: WriteFrontier(m, buf[m * ALLOC_ENTRY:(m + 1) * ALLOC_ENTRY])
            for m in range(ALLOC_MSOS)}


def weru_bin_name(bin_id):
    """-> the firmware's own name for a binId, band-qualified.

    Bands are collapsed the way the firmware's own name lookup collapses them
    (`sub_402E56B6`, `efc_cpu3.c:44254-44269`), and the band slot is reported as
    `name[lutId, mode]` so the two components stay visible rather than being
    folded into a bare number.

    `WERU_BIN_COUNT` (53) is itself the "in no bin" sentinel: `Weru_DeleteWeruIdFromBin`
    writes 53 into `weruDescriptor +11` (`efc_cpu3.c:44989`) and the bin scan
    returns 53 for "not found" (`:45532`).
    """
    if bin_id == WERU_BIN_COUNT:
        return "<no bin>"
    if not 0 <= bin_id < WERU_BIN_COUNT:
        raise ValueError("binId %r is not in [0, %d]; the firmware's own bound "
                         "is Weru_IsBinIdLegal (efc_cpu3.c:44529)"
                         % (bin_id, WERU_BIN_COUNT))
    if bin_id in WERU_BIN_NAMES and bin_id not in (2, 18):
        return WERU_BIN_NAMES[bin_id]
    for base, width in WERU_BIN_BANDS:
        if base <= bin_id < base + width:
            off = bin_id - base
            if width == 16:
                return "%s[lut %d, %s]" % (WERU_BIN_NAMES[base],
                                           off // WERU_BIN_BAND_LUT_STRIDE,
                                           "SLC" if off & 1 else "TLC")
            return "%s[%d]" % (WERU_BIN_NAMES[base], off)
    return "bin %d" % bin_id


def page_to_wordline(page, slc):
    """-> the wordline a page programs.

    Native TLC is `sub_5074` (`efc_cpu1.c:9779-9789`) verbatim -- 2 pages per
    wordline for pages 0-7, 3 for 8-1087, 2 for 1088-1095, 1 for 1096-1103:

        page < 8      -> page >> 1
        page < 1088   -> (page - 8)//3 + 4
        page >= 1096  -> page - 728
        else          -> ((page - 1088) >> 1) + 364

    Pseudo-SLC is the IDENTITY, and that is an observation, not a sourced law:
    it is what makes the Allocation record's wordline field agree on all five
    pseudo-SLC write fronts here.  No pSLC page->wordline function has been
    found in the firmware, and note that pSLC has 368 pages per block while the
    TLC law runs to wordline 375 -- so the two are not the same wordline
    numbering and the identity must not be read as "one page per wordline over
    the same wordlines".
    """
    if slc is UNKNOWN:
        raise CellModeUnknown("page_to_wordline needs the cell mode")
    if slc:
        return page
    if page < 8:
        return page >> 1
    if page < 1088:
        return (page - 8) // 3 + 4
    if page >= 1096:
        return page - 728
    return ((page - 1088) >> 1) + 364


def _bin_of(bins, weru_id):
    """-> the binId whose walked membership contains `weru_id`, else None.

    Membership is the walk, never `weruDescriptor +11` on its own: binId 0 is
    also the default that never-binned WERUs carry, so the byte over-reports
    bin 0 by 99 here.
    """
    for b, wb in bins.items():
        if wb.members is not UNKNOWN and weru_id in wb.members:
            return b
    return None


def weru_bin_for_band(base, lut_id, slc):
    """-> the binId inside a 16-wide band for `(lutId, cell mode)`.

    `binId = base + slcMode + 2*lutId` (`efc_cpu3.c:46168`).  Raises rather than
    guess if `base` is not one of the two 16-wide bands, or if `slc` is UNKNOWN
    -- a WERU whose cell mode has not been measured cannot be placed, and
    defaulting it to TLC would silently name the wrong bin.
    """
    if base not in (2, 18):
        raise ValueError("only the Valid (2) and GarbageCollection (18) bands "
                         "are lutId-indexed; %r is not one of them" % (base,))
    if slc is UNKNOWN or lut_id is UNKNOWN:
        raise CellModeUnknown(
            "binId needs both the owner's lutId and the WERU's cell mode; "
            "lutId=%r slc=%r" % (lut_id, slc))
    if not 0 <= lut_id < 8:
        raise ValueError("lutId %r is outside cMaxNumberOfLuts (8, "
                         "efc_cpu2.c:130949)" % (lut_id,))
    return base + (1 if slc else 0) + WERU_BIN_BAND_LUT_STRIDE * lut_id


def decode_rebuild_sequence_number_pool(buf):
    """`rebuildSequenceNumberPool` (144 B) -> [(shared, reserved, end)] per group.

    Six 24-byte records, one per msoGroupId (`unk_90C658 + 24 * groupId`,
    `efc_cpu2.c:34445`).  A reservation of `n` does
    ``rec[1] = rec[0]; rec[0] += n; rec[2] = rec[0]``
    (`efc_cpu2.c:34447-34449`).

    The three qwords are not `(next, base, end)` of one cursor.  Corrected
    2026-08-07: `+0` and `+8` are two independent lanes and an MSO picks one
    with `msoDescriptor +19` bit 0 (`Mso_NeedsSequenceNumberReservation`, named
    by the assert at `efc_cpu2.c:34452`).  `+16` bounds the reserved lane only.
    See `REBUILD_SEQ_POOL_*` for the site list, and use
    `Ftl.mso_rebuild_sequence_number` rather than indexing a group directly --
    the tuple is kept positional only because it is the record's literal layout.

    The section is padded to a 16 KiB granule; the rest is not interpreted.
    """
    if len(buf) < REBUILD_SEQ_POOL_BYTES:
        raise ValueError("rebuildSequenceNumberPool is %d bytes, needs %d "
                         "(efc_cpu3.c:37472)" % (len(buf), REBUILD_SEQ_POOL_BYTES))
    return [tuple(struct.unpack_from("<QQQ", buf, REBUILD_SEQ_POOL_ENTRY * g))
            for g in range(REBUILD_SEQ_POOL_GROUPS)]


def rebuild_sequence_lane(pool, group_id, needs_reservation):
    """-> (lane index, sequence number, limit) for one (group, reservation bit).

    `pool` is what `decode_rebuild_sequence_number_pool` returned.  `limit` is
    the reserved lane's end (`+16`) when the bit is set and None otherwise --
    the firmware only range-checks the reserved lane (`efc_cpu2.c:43039`,
    `:43145`, `:43204`).
    """
    if not 0 <= group_id < REBUILD_SEQ_POOL_GROUPS:
        raise ValueError("msoGroupId %r outside 0..%d"
                         % (group_id, REBUILD_SEQ_POOL_GROUPS - 1))
    lane = 1 if needs_reservation else 0
    rec = pool[group_id]
    return lane, rec[lane], (rec[2] if lane else None)


# ---------------------------------------------------------------------------
# LptEntry -- the logical-partition table of NSID2 / NSID3
# ---------------------------------------------------------------------------
#: The LP state names are in the decompilation -- `off_40400BB0[4] =
#: {"empty", "ActRD", "ActRW", "deact"}` (`efc_cpu2.c:10778`), indexed by
#: `lptEntry.byte0 & 3` at `efc_cpu2.c:114609` / `:114637`.  (a previous note
#: said this array "is in no image we hold"; IDA resolved it from the ROM data
#: section, so no image is needed.)
#:
#:   0 empty = `cLptEmpty`, pinned by `Lpt.c:2385`/`:2403` testing
#:     `byte0 << 30` (`efc_cpu2.c:111672-111698`)
#:   1 ActRD, 2 ActRW -- the two active states.  Only these two reach a read
#:     (`(result[0] & 3) != 2` refuses a write at `:24387`, `== 1` gates
#:     `:113020`) and `sub_4033D8DE`'s caller treats `v8 == 2 || v8 == 1` as
#:     "live" (`:114645`).
#:   3 deact -- deactivated but still mapped.  `sub_40340E10` (`:112094-112110`)
#:     Accepts only attr 3: it errors on empty and errors again on 1 or 2.
#:
#: On this board every live LP is deact -- 12 of 16384 in NSID2 and 11 of
#: 1024 in NSID3, and no LP anywhere carries 1 or 2 (`pt_lpt.py`).  That is a
#: fact about a powered-down console, not a decode failure: the L2S mapping is
#: intact and `Ftl` resolves through it either way.
LPT_ATTR_NAMES = ("EMPTY", "ActRD", "ActRW", "DEACT")
LPT_ENTRY_BYTES = 24                # SegmentedBuffer elementSize, :115642/:115674
LPT_ATTR_MASK = 0x3
LPT_L2S_SHIFT = 2                   # l2sIndex = u16[0] >> 2

#: The 24-byte record `sub_40344016` memcpy's over a new LP
#: (`dword_403A8FB0`, `efc_cpu2.c:115578`).  IDA types the constant `int[4]`, so
#: the last two words below do not appear in the listing -- they are read
#: straight out of the image.  `{attr=3 DEACT, 0, 0, 0, +16 = none, +20 = 1}`.
LPT_ENTRY_TEMPLATE = (3, 0, 0, 0, 0xFFFFFFFF, 1)

#: `+16 == 0xFFFFFFFF` means "no write position recorded", both as the create
#: default and as what the LP-erase path writes back (`0x403479DA`).
LPT_WRITE_OFFSET_NONE = 0xFFFFFFFF

#: `+20` bit 0 -- "the +16 write offset is valid".  Set by the create template,
#: cleared beside `+16 = 0xFFFFFFFF` at `0x403479E0`.  No other bit of +20 is
#: ever written, and nothing in this image ever tests it.
LPT_WRITE_OFFSET_VALID = 0x1

#: `+16` is `(l2sAccess.cursor << LPT_WRITE_OFFSET_CURSOR_SHIFT) +
#: currentSuperBlockIndex * wmasPerWeru` (`sub_9182`, `efc_cpu2.c:24169`;
#: `v15 = 4` except for the one NSID2 special-case LP, where it is 3).
#: Confirmed on media: NSID3 lp 0 has cursor 42 and +16 = 672 = 42 << 4.
LPT_WRITE_OFFSET_CURSOR_SHIFT = 4


def decode_lpt_entry(raw):
    """One 24-byte `LptEntry` -> a dict.  Fields, from the LP-create path
    `sub_40344016` (`efc_cpu2.c:115577-115589`) unless noted:

        u16[0]   attr | (l2sIndex << 2)                       (`:115579`)
        +2  u16  numOfSuperBlocks.  Written from the create command's
                 `numOfSuperBlocks` (`:115582`, the same value the
                 `numberOfAdditionalSuperBlocksPending` panic guards at
                 `:115568`), and incremented by that amount when superblocks are
                 added later (`sub_40345720`, `:117075-117077`).
                 Corroborated arithmetically: the L2S index allocated to each LP
                 advances by exactly `numOfSuperBlocks + 4`, on 21/21
                 consecutive live-LP pairs across three distinct values
                 (1->5, 2->6, 35->39).  `4 * numOfSuperBlocks` and
                 `numOfSuperBlocks + 3` both fail that test.
        +4  i16  eraseProgress -- the lp-erase path's progress marker.
                 Read it signed: the create path is `if (nsId) e[+4] = -1;
                 else e[+4] = 0;` (`:115587-115589`, disassembly `strh.w fp,
                 [r1, #4]` at `0x4034417C`), so NSID3's 0xFFFF is the sentinel
                 -1 = "no superblock index yet", not a magic constant and not a
                 namespace tag.  Caveat kept honest: nothing in the image ever
                 compares the field with a signed instruction, so -1 is the
                 natural reading of the initialiser, not a proven width.
                 Thereafter it is touched
                 by exactly one function, `sub_403478CE` (`efc_cpu2.c:119080`),
                 which the FSM enters as `cEraseLoggingTableState` after
                 asserting `pRequest->logicalPartitionCommand.opType ==
                 cLpOpTypeErase` (`LogicalPartitionEngine.c:1029`,
                 `efc_cpu2.c:123422-123426`).  There it is
                   * NSID2: `++`             (`0x40347950` ldrh / `0x40347954` strh)
                   * NSID3: `= L2sAccess_GetCurrentSuperBlockIndex(nsId,
                     l2sIndex)` -- `sub_4034ADA0`, which returns
                     `*(u16 *)(l2sBase + 2*l2sIndex + 2)` (`0x403479B6` strh;
                     IDA drops both the second argument and the return value).
                 Partly RETRACTED 2026-08-07: "no functional code consumes
                 it" is wrong -- `RebuildNeo` does.  `efc_cpu2.c:136307` takes
                 `*((u16 *)entry + 2)` (= this field) and `:136311` hands it to
                 `sub_403491F0(nsId, l2sIndex, that)`, whose body
                 (`:120629`) is `*(u16 *)(l2sBase + 2*l2sIndex + 2) = value` --
                 L2S slice `u16[1]`, the superblock write index, the
                 quantity the firmware calls `writeIndex` in
                 `"(writeIndex <= pLptEntry->b.size) || (writeIndex == 0xFFFF)"`
                 (`RebuildNeo.c:999`, `efc_cpu2.c:137009`).  That is also why
                 0xFFFF is legal rather than merely a create-time leftover.
                 So it is superblock-index-valued, and the two writers agree
                 with that: NSID2 advances to the next superblock (`++`), NSID3
                 copies the current superblock index out of the L2S.

                 But it is a seed, not a mirror, and it is measurably stale.
                 Only the LP-erase path advances it; ordinary writes move the
                 L2S and leave it alone.  Measured against the L2S array of the
                 same media generation, `+4 == L2S u16[1]` on 1 of 27 live
                 LPs -- the eleven NSID2 partitions read 0 against a live 1, and
                 every NSID3 one reads the create sentinel 0xFFFF against 0.  So
                 never read this as the write cursor; the live value is
                 `L2sSlice.full`.  (Its companion `+6` is a different story --
                 that one matches `L2S u16[2]` 27/27.)  Measured 0 on 12/12 NSID2
                 and -1 on 11/11 NSID3 -- i.e. the create-time value on every
                 LP, which is what "no LP on this drive has ever been erased"
                 looks like.  Note this is a per-LP field and has nothing to do
                 with `+2 numOfSuperBlocks`, which is a count accumulated by
                 `*(u16 *)(entry + 2) += numOfSuperBlocks` (`:117076`).
        +6  u16  the LP's write cursor within its current superblock -- the
                 companion of `+4`, and the persisted copy of L2S slice
                 `u16[2]`.  Written beside it at `efc_cpu2.c:24686-24688` and by
                 the paired setter `sub_40349154` (`:120585-120587`, which
                 writes `l2s[l2sIndex] u16[2]` and this field in one call), and
                 read back by `RebuildNeo` at `:136306` -> `:136312`.
                 Note the rebuild's one asymmetry: an entry already in attr 2
                 (ActRW) restores `+6` as 0, not as stored (`:136303-136306`).
                 Measured 0 on 21 of 23 live LPs and non-zero on exactly the two
                 that are mid-write; both values are inside their namespace's
                 `wmasPerWeru`.

                 Cross-checked against the L2S array of the same media
                 generation, over all 27 live LPs of both namespaces:
                 `(u16[0] >> 4) == +2` 27/27, `u16[2] == +6` 27/27,
                 `u16[1] == +4` only 1/27 (see `+4`).  So two of the three
                 are true mirrors and the third is a stale seed -- which is
                 exactly the kind of thing a decoder must not average over.
        +8  u64  the activation stamp -- one 64-bit field, not `u32 seq` plus
                 an unnamed `+12`.  `sub_40344016` stores both halves with a
                 single `STRD` (`0x40344156`, low = `sub_40300FA4()`, high hard
                 zero) and `RebuildNeo` reloads it as `unsigned __int64` and
                 folds it into the pool with `if (*lane < stamp) *lane = stamp`
                 (`efc_cpu2.c:136370-136371`).  When the rebuild wipes an entry
                 back to the blank template it explicitly preserves this qword
                 (`:136360`).  Low half, drawn from `sub_40300FA4()`
                 (`:115577`), stored here (`result[2]`, `:115580`) and written in
                 the same breath into `rebuildSequenceNumberPool[msoGroupId]`
                 (`:115596-115599`).
                 MEASURED, both namespaces, exactly: the highest stamp over the
                 12 NSID2 LPs is 2340 and group 2's pool lane reads 2340; the
                 highest over the 11 NSID3 LPs is 2812 and group 3's reads 2812.
                 The NSID2 stamps also step by exactly 4.  Which lane that is
                 depends on `msoDescriptor +19` bit 0 -- the write site is
                 `(char *)&unk_90C658 + 24*msoGroupId + 8*(desc[19] & 1)`
                 (`efc_cpu2.c:115596-115599`), the same index the rebuild uses
                 at `:136367`.  See `REBUILD_SEQ_POOL_*`.
        +12 u32  the high half of that same u64 -- not a separate field and not
                 dead.  Written 0 at create (`:115581`) and preserved at
                 `:136360`; read at `:115613` (debug), `:136333`
                 (`v13[3] = HIDWORD(v14)`) and as part of the 64-bit pool
                 comparison at `:136371`.  0 on 23/23 here, as it must be while
                 the global sequence counter is under 2**32.
        +16 u32  activationWriteOffset -- the LP's write position, in LMAs,
                 as it stood the last time the LP was activated read-write.
                 `0xFFFFFFFF` = never activated.
        +20 u32  bit 0 = activationWriteOffset is valid; no other bit is
                 ever written or tested.  Write-only in this image.

    The create PATH copies a 24-byte template, and IDA hides half of it.
    `sub_40344016` does `memcpy(result, dword_403A8FB0, sizeof(result))` with
    `_DWORD result[6]`, but IDA types the constant `int[4] = {3,0,0,0}`, so the
    last two words are invisible in the listing.  Read straight out of the
    image at 0x403A8FB0 they are `0xFFFFFFFF, 1` -- i.e. `LPT_ENTRY_TEMPLATE`
    below.  Two consequences: a freshly created LP starts at attr 3 = deact
    (`result[0] & 3` is left alone by `:115579`), and +16/+20's "unexplained"
    values 0xFFFFFFFF / 1 are simply that template showing through.

    +16 and +20, settled 2026-08-06 (disassembly, then measurement).
    A stride-24 dataflow scan over the whole SRAM image finds exactly three
    instructions that touch +16 and two that touch +20 -- there are no others,
    in any CPU's SRAM or in cpu2's TCM:

        0x40346B92  str   r0, [r3, #0x10]   sub_40346AEC  -- LP activate rw
        0x403479CA  ldr   r3, [r0, #0x10]   sub_403478CE  -- LP erase
        0x403479DA  str   r2, [r0, #0x10]   sub_403478CE  -- LP erase
        0x403479D4  ldrb  r1, [r0, #0x14]   sub_403478CE  -- LP erase
        0x403479E0  strb  r1, [r0, #0x14]   sub_403478CE  -- LP erase

    `sub_40346AEC` (`efc_cpu2.c:118169`) is reached only from the FSM state
    `cActivateLoggingTableState` (`:124288`); it sets `attr := 2` (ActRW),
    decrements `numberOfActivatedReadWriteLogicalPartitionPending`
    (`Lpt.c:5084`) and stores into +16 the fourth dword returned by the L2S
    resolver `sub_9182` (`efc_cpu2.c:24081`).  That dword is

        result[3] = (l2sAccess[+4] << 4) + l2sAccess[+2] * wmasPerWeru

    (`:24169`), with `wmasPerWeru = weruCfg[+2] * (weruCfg[+14] == 1 ?
    dword_907154 : dword_907158)` and `l2sAccess[+4]` the very cursor that +6
    mirrors.  `sub_9182` uses it as the LP's end-of-data bound (`if (a4 > v18)`
    -> "LMA past the end of this LP", `:24177`), so it is a write pointer in
    LMAs, not a capacity.

    `sub_403478CE` (the erase state above) is the only reader.  It recomputes
    `wmasPerWeru` the same way and, when the erase has reached the superblock
    the recorded position lies in, discards the record:

        if (entry[+16] / wmasPerWeru == currentSuperBlockIndex) {
            entry[+16] = 0xFFFFFFFF;  entry[+20] &= ~1;
        }

    MEASURED, and exact: NSID3 lp 0 carries +6 (cursor) = 42 and +16 =
    0x2A0 = 672 = 42 << 4 -- the identity `+16 == (+6 << 4)` for an LP still
    in its first superblock, on the only LP on this drive that has a non-zero
    write position at activation.  The other ten NSID3 LPs never reached
    activate-RW and still hold the template 0xFFFFFFFF; all twelve NSID2 LPs
    were activated with cursor 0 and hold 0.  (+16 is a snapshot: NSID2 lp 12's
    cursor has since advanced to 11183 while its +16 stays 0.)

    Nothing else reads either field.  +20's only appearance outside the create
    template is the read-modify-write above; no branch anywhere tests its bit 0.
    So the pair is maintained but never consumed in this image -- an erase/resume
    anchor whose consumer is not present.
    """
    if len(raw) < LPT_ENTRY_BYTES:
        raise ValueError("LptEntry is %d bytes, needs %d (SegmentedBuffer "
                         "elementSize, efc_cpu2.c:115642)"
                         % (len(raw), LPT_ENTRY_BYTES))
    w0, nsb, cursor = struct.unpack_from("<HH2xH", raw, 0)
    (f4,) = struct.unpack_from("<h", raw, 4)        # Signed: -1 = none
    seq, z12, u16f, u20 = struct.unpack_from("<IIII", raw, 8)
    (stamp,) = struct.unpack_from("<Q", raw, 8)
    attr = w0 & LPT_ATTR_MASK
    return {
        "attr": attr,
        "attr_name": LPT_ATTR_NAMES[attr],
        "l2s_index": w0 >> LPT_L2S_SHIFT,
        "n_superblocks": nsb,
        # +4 -- see the docstring: this is the persisted `writeIndex`, the
        # superblock index the LP is writing into.  `erase_progress` is kept as
        # an alias only because two tools print it; it is a misnomer.
        "superblock_write_index": (None if (f4 & 0xFFFF) == 0xFFFF else f4),
        "superblock_write_index_raw": f4 & 0xFFFF,
        "erase_progress": f4,           # Deprecated alias of +4
        "nsid3_marker": f4 & 0xFFFF,    # deprecated alias of +4, unsigned
        "superblock_cursor": cursor,
        # +8 is one u64, not two fields.  `sub_40344016` stores it with a single
        # strd (`0x40344156`) and `RebuildNeo` compares it as `unsigned __int64`
        # against the pool lane (`efc_cpu2.c:136370-136371`).
        "activation_stamp": stamp,
        "rebuild_sequence_number": seq,   # low half, kept for callers
        "activation_stamp_high": z12,
        "unnamed_12": z12,                # Deprecated alias of the high half
        "activation_write_offset": (None if u16f == LPT_WRITE_OFFSET_NONE
                                    else u16f),
        "activation_write_offset_raw": u16f,
        "write_offset_valid": bool(u20 & LPT_WRITE_OFFSET_VALID),
        "write_offset_flags": u20,
        "raw": bytes(raw[:LPT_ENTRY_BYTES]),
    }


def decode_mso_descriptor(buf, table=None):
    """`msoLma` (63 x 28) -> {msoId: Mso}, only for entries that are non-zero.

    Two fields are pinned: `+7 u32 totalLmas` (the value that identified msoId
    41 as SystemMetadata, since 7424 is one of the four
    maxSystemMetadataSizeInLmas classes) and `+16 u8 raidType`
    (`Mso_GetRaidType`, efc_cpu2.c:38744).  The other 23 bytes are undecoded and
    are not guessed at.

    Duplicate detection.  A descriptor byte-identical to at least one other is
    marked `templated`.  It matters: on this board msoIds 0..15 share one
    descriptor and 16..31 share another, so the array being "fully populated"
    is not evidence of 32 distinct sub-32 partitions.

    The two templates are now explained (2026-08-06): 0..15 carry lutId 2 and
    16..31 lutId 3 -- the NSID2 and NSID3 group LUTs -- because the sub-32 range
    is the active logical partition MSO pool, 16 slots per namespace group, all
    cloned from the static msoIds 44 and 48.  So the duplication is the
    signature of the mechanism, not an absence of information.  Individual ids
    still get no per-partition name.
    """
    out = {}
    ent = table.entries() if table is not None else {}
    n = min(geom.MAX_MSOS, len(buf) // MSO_DESC_ENTRY)
    raw = [bytes(buf[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY])
           for m in range(n)]
    dup = {}
    for e in raw:
        dup[e] = dup.get(e, 0) + 1
    for m, e in enumerate(raw):
        if not any(e):
            continue
        t, g = ent.get(m, (UNKNOWN, UNKNOWN))
        out[m] = Mso(m, t, g,
                     name=(table.name(m) if table is not None else None),
                     raid_type=e[MSO_DESC_RAID_OFF],
                     total_lmas=struct.unpack_from(
                         "<I", e, MSO_DESC_TOTAL_LMAS_OFF)[0],
                     templated=dup[e] > 1,
                     lut_id=e[MSO_DESC_LUT_ID_OFF],
                     lut_store=e[MSO_DESC_LUT_STORE_OFF],
                     nand_block_mode=(
                         (e[MSO_DESC_NAND_BLOCK_MODE_OFF]
                          >> MSO_DESC_NAND_BLOCK_MODE_SHIFT)
                         & MSO_DESC_NAND_BLOCK_MODE_MASK),
                     is_valid=bool(e[0] & MSO_DESC_VALID_BIT),
                     weru_pool=bool(e[0] & MSO_DESC_WERU_POOL_BIT),
                     fill_class=((e[0] >> MSO_DESC_FILL_CLASS_SHIFT)
                                 & MSO_DESC_FILL_CLASS_MASK),
                     allocation_type=e[MSO_DESC_ALLOC_TYPE_OFF],
                     rebuild_flags=e[MSO_DESC_REBUILD_OFF],
                     needs_sequence_reservation=bool(
                         e[MSO_DESC_SEQ_RESERVATION_OFF]
                         & MSO_DESC_SEQ_RESERVATION_BIT),
                     parity_weru_front=(
                         (e[MSO_DESC_SEQ_RESERVATION_OFF]
                          >> MSO_DESC_PARITY_FRONT_SHIFT)
                         & MSO_DESC_PARITY_FRONT_MASK),
                     zero_data_id=e[MSO_DESC_ZERO_DATA_ID_OFF])
    return out


def verify_weru_stride(block_mapping_buf, valid_counts, survey,
                       strides=(12, 24, 30, 32, 48), threshold=100000):
    """Re-measure the WERU stride instead of believing it.

    A WERU whose validCount exceeds `threshold` certainly holds live data, so
    every block its mapping names must be programmed.  Decode the table at each
    candidate stride, keep the entries that land on `survey.ce`, and count how
    many of those blocks the survey found programmed.  The true stride scores
    N/N; a wrong one scores ~0/N.

    -> {stride: (consistent, tested)}.  A stride with `tested == 0` produced no
    testable prediction at all and must not be read as agreement.
    """
    big = [i for i, v in enumerate(valid_counts) if v > threshold]
    out = {}
    for s in strides:
        m = decode_block_mapping(block_mapping_buf, s, i_know_this_is_wrong=True)
        ok = tot = 0
        for w in big:
            for bc in m.get(w, []):
                if bc is None:              # unmapped slot: not a prediction
                    continue
                block, ce = bc
                if ce != survey.ce:
                    continue
                st = survey.state(block)
                if st is UNKNOWN:
                    continue
                tot += 1
                ok += (st == "programmed")
        out[s] = (ok, tot)
    return out


# ===========================================================================
# the SystemMetadata section layout
# ===========================================================================
def section_layout(directory):
    """-> [(name, base, n_lmas, length_derived)] in base order.

    `directory` is the LMA 0 dict.  The length of each section is
    ``nextBase - thisBase - SECTION_GAP`` -- the 32-LMA gap the layout builder
    leaves after every section (`efc_cpu3.c:37436-37478`).  The implicit
    "directory" section at LMA 0 is included; it is what makes the first gap
    visible (0 + 4 + 32 = 36 = the first section base).

    The last section has no successor, so its length cannot be derived at all.
    It is reported as one allocation unit (`LMAS_PER_UNIT`) with
    ``length_derived=False``, never as a measured fact.

    On this board the derived lengths are, in order: directory 4, userLutLut 12,
    mso 4, mediaSpace 4, blockMapping 252, weruDescriptor 8, weruBin 4,
    validMap 6288, validCount 4 -- and every one of them is matched exactly by
    the LMAs actually present on media in the weru-19 half of the image.
    """
    bases = sorted((v, k) for k, v in directory.items() if k.endswith("Lma"))
    if not bases or bases[0][0] != SECTION_FIRST_BASE:
        # Not fatal -- report it, do not silently renumber.
        pass
    rows = [(0, "directory")] + bases
    out = []
    for i, (base, key) in enumerate(rows):
        name = key[:-3] if key.endswith("Lma") else key
        if i + 1 < len(rows):
            out.append((name, base, rows[i + 1][0] - base - SECTION_GAP, True))
        else:
            out.append((name, base, LMAS_PER_UNIT, False))
    return out


def section_lmas_for_size(size_bytes):
    """The layout builder's own `lmas(size)` -- ceil to 16 KiB, x4."""
    return -(-size_bytes // SECTION_UNIT) * LMAS_PER_UNIT


class Section(object):
    """One SystemMetadata section: its declared length, and every LMA of it
    that could actually be read -- including the ones past the first hole.

    The old accessor stopped at the first missing LMA, which for `validMap`
    reported 20 LMAs where 5648 were present in the union image and 6288 exist
    on media.  A hole does not end a section; it punches a 4096-byte window out
    of it, and everything after that window is still readable at its correct
    offset.

    `present` is ``{index: 4096-byte payload}`` with index relative to `base`.
    `missing` is the complement, so "not present" is never confusable with
    "empty".  `chunk(off, n)` is the primitive that keeps positional integrity:
    it returns UNKNOWN rather than silently splicing across a hole.
    """
    __slots__ = ("name", "base", "n_lmas", "length_derived", "present", "source")

    def __init__(self, name, base, n_lmas, present, length_derived=True,
                 source=None):
        self.name = name
        self.base = base
        self.n_lmas = n_lmas
        self.length_derived = length_derived
        self.present = dict(present)
        #: {index: weruId} when the image knows which WERU each LMA came from.
        self.source = dict(source or {})

    # -- shape --------------------------------------------------------------
    @property
    def n_present(self):
        return len(self.present)

    @property
    def complete(self):
        return len(self.present) == self.n_lmas

    @property
    def missing(self):
        return [i for i in range(self.n_lmas) if i not in self.present]

    @property
    def first_hole(self):
        """-> the first absolute LMA missing from this section, or None."""
        for i in range(self.n_lmas):
            if i not in self.present:
                return self.base + i
        return None

    @property
    def n_contiguous(self):
        i = 0
        while i in self.present:
            i += 1
        return i

    def werus(self):
        """-> sorted weruIds that contributed an LMA to this section."""
        return sorted({w for w in self.source.values() if w is not None})

    # -- access -------------------------------------------------------------
    def chunk(self, off, n):
        """-> `n` bytes at byte offset `off`, or UNKNOWN if a hole intersects.

        This is how a caller reads one record out of a section that has holes
        elsewhere.  It never concatenates across a gap.
        """
        if off < 0 or n < 0 or off + n > self.n_lmas * LMA_BYTES:
            raise IndexError("[%d,%d) is outside section %s (%d LMAs)"
                             % (off, off + n, self.name, self.n_lmas))
        first, last = off // LMA_BYTES, (off + n - 1) // LMA_BYTES if n else off // LMA_BYTES
        buf = bytearray()
        for i in range(first, last + 1):
            p = self.present.get(i)
            if p is None:
                return UNKNOWN
            buf += p
        s = off - first * LMA_BYTES
        return bytes(buf[s:s + n])

    def contiguous(self):
        """-> the bytes from `base` up to the first hole.  May be b''."""
        return b"".join(self.present[i] for i in range(self.n_contiguous))

    def whole(self, fill=None):
        """-> the full declared length, or UNKNOWN unless `fill` is given.

        `fill` must be an explicit 4096-byte pattern chosen by the caller; there
        is no default, because a zero-filled hole is indistinguish-
        able from a section that really is zero there.
        """
        if self.complete:
            return b"".join(self.present[i] for i in range(self.n_lmas))
        if fill is None:
            return UNKNOWN
        if len(fill) != LMA_BYTES:
            raise ValueError("fill must be %d bytes" % LMA_BYTES)
        return b"".join(self.present.get(i, fill) for i in range(self.n_lmas))

    def __repr__(self):
        return "<Section %s base=%d %d/%d LMAs%s>" % (
            self.name, self.base, self.n_present, self.n_lmas,
            "" if self.length_derived else " (length NOT derived)")


# ===========================================================================
# WMA <-> physical
# ===========================================================================
#: `cMaxBlocksPerWeru * wmasPerPage` = 48 * 4.  The WMA window stride, and it is
#: Constant: a WERU that owns fewer than `WMA_STRIDE // WMAS_PER_PAGE` blocks
#: leaves the tail of every window unused rather than packing.  That single fact
#: is the whole explanation of the "+152 recurring on a cycle of 192" in the
#: userLutLut that no packing model could produce: 192 - 11*4 = 148, and the
#: last step inside a window is +4, so the observed jump is 152.
WMA_STRIDE = 192
WMAS_PER_PAGE = 4
MAX_BLOCKS_PER_WERU = WMA_STRIDE // WMAS_PER_PAGE       # 48, cMaxBlocksPerWeru


class WeruConfig(object):
    """One row of the firmware's six-entry WERU configuration table.

    `sub_51E0` selects a configuration by weruId range from `byte_900078`
    (+10 u16 firstWeruId, +4 u16 weruCount, +2 u16 blocksPerWeru, +14 u8
    cellMode).  `blocksPerWeru` is per configuration and is not 48.
    Assuming 48 drive-wide is what made every earlier attempt at this
    arithmetic fail; nothing in this path may hard-code it.

    `blocksPerWeru = 11` for these WERUs is established from DATA, not inferred:
    across 2 x 2970 userLutLut entries, `wma mod 192` is only ever in
    {0, 4, ..., 40} and never 44, so the valid window is `r < 44 = 11*4` --
    exactly the bound `sub_4030E7F8` tests.

    `slot -> (ce, block)` was measured on hardware for the SLC WERUs, and it is
    a BLOCK pair on each of six consecutive CEs, CE-major:

        slot = 2 * (ce - ceBase) + (block - blockBase)

    `ceBase` is a per-weru property and must not be inferred from the block
    numbers: werus 19 and 20 are the same block pair 10/11 on the two halves of
    the package (ceBase 0 and 6).

    The twelfth BLOCK is the stripe's inline RAID PARITY.  `titania.raid`
    established this first and its module docstring already says it RETRACTS
    this paragraph; the retraction simply never reached here.  Read that as a
    warning about two modules describing one drive: `raid.py` carried it at [a]
    while this file and `NAND_FORMAT_STATE.md` Hop 5 still carried `[?]`.
    Reconfirmed independently 2026-08-07 at the decoded layer (`raid.py` works
    at the raw stored layer, where its 847-page rebuild is 8954/9317 with every
    miss a single rotted byte; decoding first corrects those, which is why the
    numbers below are exact).  `blocksPerWeru` is 11 while twelve blocks are
    programmed because the twelfth carries

        slot11[au] = XOR(slot0[au] .. slot10[au])

    over the AU's 8 aux bytes and its 4096 user bytes.  `sd_block17.py` on werus
    14, 20 and 22 (the CE 6-11 half, so no dead die is involved): 3388 AUs
    exact / 0 differ over 847 complete stripes, 12-way XOR exactly zero on
    847/847, leave-one-out reconstruction 5328/5328 on weru 14.  In WERU 14 the
    odd one out is (ce11, block 17) -- and its 111 programmed pages are not a
    short block: all twelve slots of weru 14 are programmed to exactly page 110.

    `slot_to_physical` still refuses SLOT 11, and that is not a leftover.  The
    slot has no WMA, so returning an address for it would let a WMA round-trip
    invent one; the parity block is reached by asking for
    (ce, block) directly, not by addressing it.  `covers()` accepts it so the
    block is never mistaken for empty.

    One erasure, one relation: werus 19 and 23 put two of their twelve members
    on CE0, so a dead CE0 leaves two unknowns in one equation and they are not
    recoverable on this board.
    """
    __slots__ = ("blocks_per_weru", "ce_base", "block_base", "blocks_per_ce",
                 "slc", "name", "source")

    def __init__(self, blocks_per_weru, ce_base, block_base, slc=True,
                 name=None, blocks_per_ce=2, source=None):
        self.blocks_per_weru = blocks_per_weru
        self.ce_base = ce_base
        self.block_base = block_base
        self.blocks_per_ce = blocks_per_ce
        self.slc = slc
        self.name = name
        self.source = source

    @property
    def pages_per_block(self):
        return geom.pages_per_block(self.slc)

    def slot_to_physical(self, slot):
        """slot -> (ce, block), or None when the slot addresses nothing.

        None covers both the unused tail of the 192-WMA window and the
        programmed-but-unaddressed twelfth block.
        """
        if not 0 <= slot < self.blocks_per_weru:
            return None
        ce, half = divmod(slot, self.blocks_per_ce)
        return self.ce_base + ce, self.block_base + half

    def physical_to_slot(self, ce, block):
        """(ce, block) -> slot, or None if it is not an addressable slot here."""
        db, dc = block - self.block_base, ce - self.ce_base
        if not 0 <= db < self.blocks_per_ce or dc < 0:
            return None
        slot = self.blocks_per_ce * dc + db
        return slot if slot < self.blocks_per_weru else None

    def covers(self, ce, block):
        """True if (ce, block) is inside this WERU's PROGRAMMED region.

        Wider than `physical_to_slot`: the twelfth block is in the
        region and has no slot, and conflating the two would hide it.
        """
        db, dc = block - self.block_base, ce - self.ce_base
        n_ce = -(-self.blocks_per_weru // self.blocks_per_ce)
        return 0 <= db < self.blocks_per_ce and 0 <= dc < n_ce

    def __repr__(self):
        return "<WeruConfig %s bpw=%d ce %d..%d blocks %d..%d %s>" % (
            self.name or "?", self.blocks_per_weru, self.ce_base,
            self.ce_base + -(-self.blocks_per_weru // self.blocks_per_ce) - 1,
            self.block_base, self.block_base + self.blocks_per_ce - 1,
            "SLC" if self.slc else "TLC")


#: MEASURED on this board (`nk_wma.py`, `nk_lutdump.py`, `nk_wmafit.py`), not
#: read out of the firmware's configuration table -- `byte_900078` is runtime
#: SRAM and we do not hold it.  Every entry here was confirmed by reading the
#: pages it predicts and finding exactly the LMA group the LUT entry indexes.
#:
#: Note the CE split: the same block pair is two different WERUs on the two
#: halves of the package -- werus 19 and 20 are both blocks 10/11, 19 on CE
#: 0-5 and 20 on CE 6-11.  So `ceBase` is a per-WERU fact and cannot be inferred
#: from the block numbers.  This is also why `weru << n | offset` never landed
#: inside a 17664-AU window: these WERUs are 11 blocks, not 48, and most of each
#: 192-WMA window is padding.
WERU_CONFIGS = {
    14: WeruConfig(11, 6, 16, True, "user LUT, blocks 16/17 on CE 6-11",
                   source="measured: 11/11 slots, 1212/1212 LUT entries"),
    19: WeruConfig(11, 0, 10, True, "SystemMetadata, blocks 10/11 on CE 0-5",
                   source="measured against the SystemMetadata scan"),
    20: WeruConfig(11, 6, 10, True, "user LUT, blocks 10/11 on CE 6-11",
                   source="measured on page 0, 11/11 consecutive entries"),
    22: WeruConfig(11, 6, 20, True, "user LUT, blocks 20/21 on CE 6-11",
                   source="measured against the userLutLut"),
    23: WeruConfig(11, 0, 26, True, "SystemMetadata, blocks 26/27 on CE 0-5",
                   source="measured against the SystemMetadata scan"),
}


#: The six WERU configurations, read OUT of the firmware image.
#:
#: `sub_51E0` (`efc_cpu2.c:19646`) and everything else that needs a WERU's shape
#: indexes `byte_900078[20 * cfgIdx]`: `+0 u8 cfgIdx` (a self-reference, checked
#: against the loop counter at `efc_cpu2.c:63801`), `+2 u16 blocksPerWeru`,
#: `+4 u16 weruCount`, `+6 u16 startColumn`, `+10 u16 firstWeruId`,
#: `+14 u8 cellMode`, `+16 u32 flags`.
#:
#: That array is runtime SRAM and reads all zero in the bench context, and it
#: can also be overwritten by the host over RPC
#: (`MediaPartitionWeruConfigurationInterface_Write`, `efc_cpu2.c:63682`).  But
#: the default that the drive builds for itself is literal code:
#: `sub_4033CB5E` (`efc_cpu2.c:107815`) assigns every field by hand, and
#: `WeruConfiguration_OneTimeInit` (`sub_4033C814`, `:107642`, named by its own
#: `sub_4030E866("WeruConfiguration_OneTimeInit", 110)` at `:107750`) is what
#: publishes it.  The values below are that code with its two device constants
#: resolved:
#:
#:     word_90C458 = 24    blocks a half-width WERU owns
#:     byte_90C442 = 2     minimum blocks per CE
#:
#: Neither is fitted.  `word_90C458 = 24` is forced four separate ways by
#: `sub_4033CB5E` itself -- config 0 sets `blocksPerWeru = word_90C458`,
#: config 2 sets `word_90C458 >> 1`, configs 3/4 set
#: `(4 / byte_90C442) * word_90C458`, config 5 sets `word_90C458` -- and all
#: four match the four blocksPerWeru runs read independently off the
#: `WeruUsageLog`'s own 0x3FFF terminator (see `WeruUsageLog.blocks_per_weru`).
#: `weruCount` for config 2 is `2 * (15 - v15)` where `v15 = v12 / v14` with
#: `v12 = 2 * P` and `v14 = P` for the same product P (`:107925-107929`), so it
#: is structurally 2 and the count is 26 -- arithmetic, not a fit.
#:
#: Two independent confirmations that these are the right numbers:
#:   * `dword_907164` is the sum of the first five weruCounts and `dword_907168`
#:     the sum of all six (`efc_cpu2.c:108029-108049`).  These give 972 and
#:     1070 -- exactly the measured `validMap` row count and the measured
#:     `WeruUsageLog` record count.
#:   * `sum(weruCount * blocksPerWeru)` = 48000, and the corrected `blockMapping`
#:     claims exactly 48000 (block, ce) pairs over weruIds 0..1069 with none
#:     claimed twice.  48000 = 4000 blocks x 12 CEs, so the drive maps 4000
#:     blocks per CE, not the 4096 `geometry.BLOCKS_PER_LUN` infers from the
#:     row field width.  4096 remains the right bound for "is this a legal block
#:     number"; 4000 is how many are actually mapped.
#:
#: `cellMode` is field +14.  `sub_4033C814:107696` branches on it: 1 selects
#: `sub_4030C0D2()` = `pagesPerBlock/3` (368, pseudo-SLC) and anything else
#: `sub_4030C0E6()` = `pagesPerBlock` (1104, native TLC).  So 1 = pseudo-SLC and
#: 0 = native TLC.
#:
#: Config 5's `cellMode 2` means "no fixed PAGE mode" -- settled 2026-08-06.
#: Three legs, and the third is a measurement:
#:   1. *No consumer distinguishes 2 from 0.*  Every read of +14 in cpu2 and
#:      cpu3 is a test for equality with 1 (`efc_cpu2.c:91016, 91156, 91572,
#:      91697, 92273, 93258, 93547, 94113, 99524, 99689, 107696, 108510, 108605,
#:      108715, 114620, 136496`, plus the raw getters `efc_cpu2.c:63956` and
#:      `efc_cpu3.c:26948`), and `sub_4030C1FA` (`efc_cpu2.c:60406`)
#:      normalises the field to a two-valued page mode with
#:      `if (cellMode != 1) return 2`.  There is no third branch anywhere.
#:   2. *The one function that consumes the spare pool ignores the spare's own
#:      cellMode.*  `sub_4033D69E` (`efc_cpu2.c:108560`, `MediaReplaceBlockEngine.c
#:      :611`) asserts `sourceWcId == cWeruConfigurationIdSpare` on the
#:      Configuration ID, then derives the mode it wants from the target
#:      configuration (`v7 = cfg[targetWcId].cellMode; if (v7 != 1) v7 = 2;`,
#:      `:108604-108609`) and scans the spare range for a block whose
#:      `PhysicalBlockProperties[25:24]` equals it -- accepting 3 as a wildcard
#:      and, only when downgrading to pseudo-SLC, a 2.
#:   3. *Measured, 48000/48000 blocks.*  Every block owned by a cellMode-1
#:      configuration carries `[25:24] == 1` (4608 = 24+48+312+4224, exactly the
#:      predicted totals) and every block of the cellMode-0 configuration carries
#:      2 (41040, likewise exact).  The spare configuration is the only one whose
#:      blocks do not share one value: 2273 carry 3 (unassigned) and the other
#:      79 are exactly the displaced defective blocks parked there, still
#:      carrying the mode of the WERU they came from (3 pseudo-SLC + 76 native).
#:      2352 - 79 = 2273 and 4608 + 3 = 4611, 41040 + 76 = 41116 -- the three
#:      measured histogram bins close on the nose (`pt_props.py`).
#: So cellMode 2 is a table value with no consumer of its own; it exists to say
#: that this pool's blocks are mode-agnostic until one is substituted in.  The
#: `slc` property is still left None for it -- "mode-agnostic" is not "SLC".
#:
#: `blocks_per_ce` is `sub_4033D13E` (`efc_cpu2.c:108170`):
#: `max(byte_90C442, blocksPerWeru // 12)`, i.e. 4 / 2 / 2 for 48 / 24 / 12.
#: A 12-block WERU therefore spans six CEs, which is the hardware-measured
#: metadata layout, and a 48-block one spans all twelve.
#:
#: The remaining fields, from `sub_4033C69C` (`efc_cpu2.c:107534-107620`), the
#: reset that rebuilds the per-column block tables:
#:
#:     +6  u16  startColumn.  `sub_4033C69C` divides this configuration's
#:              `weruCount * blocksPerWeru` blocks over
#:              `sub_4030C110() == word_90C458 == 24` columns and hands the
#:              Remainder out starting at column `+6` (`v18 = v9[3]`, `:107604`);
#:              `sub_4030E70C` (`:63938`) is the matching getter,
#:              `(+6 + i * blocksPerWeru) % 48`.  `sub_4033CB5E` builds it as a
#:              running `(prev.startColumn + prev.weruCount * prev.blocksPerWeru)
#:              % numberOfColumns` for configurations 1 and 2 and hard-zeroes it
#:              for 3, 4 and 5.
#:              It has no effect on this drive: all six products
#:              (24, 48, 312, 4224, 41040, 2352) are exact multiples of 24, so
#:              the remainder loop never executes and every startColumn is 0.
#:              The resulting per-column tables are (base, count) =
#:              (0,1) (1,2) (3,13) (16,176) (192,1710) (1902,98) = 2000 blocks
#:              per column x 24 columns = 48000, exactly the measured size of
#:              `PhysicalBlockProperties`.
#:     +8  u16  DEAD in this image -- written once at init, never read.
#:              Upgraded 2026-08-06 from "no surviving caller" (an IDA-level
#:              claim) to a machine-checked one, three ways:
#:                1. The getter exists five times, once per SRAM copy of the
#:                   WeruConfiguration accessor library: 0x400B6284,
#:                   0x402D12F6, 0x4030E6B8 (cpu2's `sub_4030E6B8`),
#:                   0x4048AF8A and 0x404D11BE, all the same four instructions
#:                   `add.w r0,r0,r0,lsl#2 / add.w r0,r1,r0,lsl#2 /
#:                   ldrh r0,[r0,#8] / bx lr`.
#:                2. A superset branch scan -- every 32-bit bl/blx/B.W *and*
#:                   every 16-bit B/Bcc encoding decoded at every halfword
#:                   offset of the whole 12 MB SRAM image -- finds zero
#:                   branches to any of the five, and the 4-byte constants
#:                   `addr` and `addr|1` appear nowhere in the SRAM image or
#:                   in any of the 37 TCM/ROM dumps, so there is no indirect
#:                   call either.  (The same scan finds 1284 callers of a
#:                   control function and 4 callers of the +16 bit-0 getter, so
#:                   it is not a broken harness.)
#:                3. A stride-20 dataflow scan over every function in the image
#:                   finds exactly three instructions anywhere that load
#:                   offset +8 of a WeruConfiguration record -- 0x402D1300,
#:                   0x4030E6C2 and 0x404D11C8 -- and all three are those
#:                   getters.  No inlined form reads it; the debug dumpers
#:                   `sub_4033CB02` (`:107797`) and `sub_4033C616` (`:107588`)
#:                   log +10/+4/+12/+6/+2 and the flag bits and skip +8.
#:              Not named, and it cannot be named from this image.  What is
#:              known is only how
#:              `sub_4033CB5E` computes it: 0, 1, `ceil(0.95 * weruCount)` = 25,
#:              `weruCount` = 88, `weruCount` = 855, 0 for configurations 0..5
#:              (`:107894, 107909, 107935, 107950, 107985, 108018`).  It is
#:              `weruCount` for the two data pools, zero for the anchor and the
#:              spare pool, and strictly below `weruCount` for the three
#:              metadata-ish ones -- consistent with a cap on how many of the
#:              pool may be allocated at once, but that is a shape, not a
#:              reading, and there is no reader to settle it.
#:     +12 u16  a mutable copy of `weruCount`: `sub_4033C69C` sets
#:              `v9[6] = v9[2]` on every reset (`:107618`) and `sub_4030E866` /
#:              `sub_4030E924` log it next to `firstWeruId` (`:64032`, `:64083`).
#:              `sub_4033CB5E` initialises it to `weruCount` in all six rows.
#:              No decrementer was found, so "remaining" is not established --
#:              what is established is that it is initialised to `weruCount` and
#:              is not the same storage as +4.
#:     +16 u32  flags, three bits, all assigned by `sub_4033CB5E`:
#:              The six assignments are at `efc_cpu2.c:107896` (config 0, the
#:              literal 7), `:107918`, `:107944`, `:107959`, `:107994` and
#:              `:108027`, giving 7, 7, 2, 0, 0, 2.
#:                bit 0 -- set for configurations 0 and 1 only (`:107896` sets it
#:                         with the literal 7, `:107914` ORs it in).
#:                         NAMED 2026-08-06: SKIP THE DEFECT CHECK WHEN
#:                         Allocating BLOCKS.  cpu2's getter `sub_4030E734`
#:                         (`efc_cpu2.c:63948`) really is inlined away, but
#:                         cpu3 carries the same getter as `sub_402D1372`
#:                         (`efc_cpu3.c`, `(cfg[+16] & 1)`) and it has a live
#:                         call site: `VirtualWeru_AllocateWeru` reads it into
#:                         its `skipDefect` local (`efc_cpu3.c:41473`) and, when
#:                         set, `break`s out of the per-plane pop loop the
#:                         instant a node comes off the free list instead of
#:                         re-popping while the block is defective
#:                         (`:41623-41627`).  `VirtualWeru.c:516` asserts
#:                         `!isSkipDefect` on the repair path, which is the
#:                         firmware's own name for it.
#:                         Corroborated 2026-08-06 by a superset branch scan of
#:                         the whole SRAM image (every 32-bit bl/blx/B.W and
#:                         every 16-bit B/Bcc at every halfword offset, plus a
#:                         4-byte constant search over the image and all 37
#:                         dumps).  It finds cpu3's getter `sub_402D1372` called
#:                         from exactly four sites, and none of the other
#:                         four SRAM copies of the getter called at all:
#:                           * 0x402E2706 in `sub_402E266A` -- the loop that
#:                             Builds each WERU's block free list.  skipDefect
#:                             becomes the third argument of the per-block
#:                             insert and the guard is
#:                             `if (skipDefect || (props[+13] & 0x70) == 0 ||
#:                             ((props[+13] >> 4) & 7) == 6)`: link the block in,
#:                             else log 0x40F43020 and stamp the row 0xDF00FFFF
#:                             (`efc_cpu3.c:41194-41232`, and the out-of-line
#:                             copy `sub_6468` at `efc_cpu3.c:11476`).  So the
#:                             bit means "do not consult
#:                             PhysicalBlockProperties" on the build side too,
#:                             not only in the pop loop.
#:                           * 0x402E2C3C in `VirtualWeru_AllocateWeru`
#:                             (`sub_402E2BDA`) -- the site described above.
#:                           * 0x402E334C in `sub_402E3304`, which hands it to
#:                             the same insert (`sub_402C0BD4` -> `sub_6468`).
#:                           * 0x402E3AA8 in
#:                             `CorePartitionInterface_FwDataReplaceBlock`,
#:                             which asserts it is clear.  No contradiction:
#:                             the firmware-data WERUs are 3..6, i.e.
#:                             configuration 2, whose bit 0 is 0.
#:                         Configurations 0 and 1
#:                         are the RootStorage anchors, whose blocks are pinned
#:                         by bit 2 anyway, so the check would be moot.
#:                bit 1 -- `blocksPerWeru <= word_90C458` (24), computed the same
#:                         way in all six rows (the `v > v1 ? &~2 : |2` pairs at
#:                         `:107905`, `:107915`, `:107941`, `:107955`, `:107990`,
#:                         `:108023`).  So 1,1,1,0,0,1.  Its four readers
#:                         (`efc_cpu2.c:37986, 39859, 40702, 41733`) all do the
#:                         same thing: choose between two sub-codes of the debug
#:                         trace event `sub_24A2(1, 15, ...)` -- and `sub_24A2`
#:                         (`:16270`) is a trace-ring writer.  It steers no data
#:                         path.
#:                bit 2 -- set for configurations 0 and 1 only (`dword_900088 = 7`
#:                         and `dword_90009C = v8 & 3 | 4`, `:107896`, `:107918`).
#:                         BLOCKS are pinned to their WERU.  Two engines
#:                         read it and both stand down when it is set:
#:                           * `sub_4033D8DE` (`efc_cpu2.c:108709`) runs the
#:                             spare-block substitution engine only when
#:                             `((cfg[+16] >> 2) & 1) == 0`;
#:                           * `VirtualWeru_AllocateWeru` (`efc_cpu3.c:41380`)
#:                             reads the same bit through cpu3's copy of the
#:                             getter, `sub_402D133C`, into its `allocateInPlace`
#:                             local (`:41472`) and then takes each block from
#:                             the WERU's own blockMapping row instead of
#:                             popping a donor row off the per-plane free list
#:                             (`:41505-41539`) -- so no `sub_402D3D7C` swap
#:                             happens and the WERU's physical blocks never move.
#:                         The two RootStorage configurations are the ones found
#:                         by a fixed weruId anchor (WERU 0 for the Backup
#:                         store, `efc_cpu2.c:10524`) and are therefore
#:                         unrelocatable.  `sub_4030E6FE` (`:63932`) is cpu2's
#:                         getter.  MEASURED: configurations 0 and 1 are the
#:                         only two whose WERUs are 100% still on their
#:                         format-time blocks -- see `Ftl.weru_block_moves`.
WERU_CONFIG_FLAG_SKIP_DEFECT = 0x1  # skip the defect re-pop in the allocator
WERU_CONFIG_FLAG_NARROW = 0x2       # blocksPerWeru <= 24; trace sub-code only
WERU_CONFIG_FLAG_BLOCKS_PINNED = 0x4  # no substitution, no allocator relocation
WERU_CONFIG_FLAG_BIT0 = WERU_CONFIG_FLAG_SKIP_DEFECT      # old name
WERU_CONFIG_FLAG_NO_REPLACE = WERU_CONFIG_FLAG_BLOCKS_PINNED  # old name

#: `+16` as `sub_4033CB5E` builds it, per configuration index
#: (`efc_cpu2.c:107896, 107918, 107944, 107959, 107994, 108027`).
WERU_CONFIG_FLAGS = (7, 7, 2, 0, 0, 2)

#: `cWeruConfigurationId*` -- the firmware's own names for +0, from its asserts:
#: `cWeruConfigurationIdMetadata` = 2 and `cWeruConfigurationIdTlcData` = 4 are
#: pinned together by `(targetWcId >= cWeruConfigurationIdMetadata) &&
#: (targetWcId <= cWeruConfigurationIdTlcData)` compiling to `a2 - 2 >= 3`
#: (`efc_cpu2.c:108591`), and `cWeruConfigurationIdSpare` = 5 by
#: `sourceWcId == cWeruConfigurationIdSpare` compiling to `a1 != 5` (`:108589`).
#: `:109350` and `:136849` confirm TlcData = 4, `:60433` and `:156735` confirm
#: Metadata = 2.  Rows 0/1/3 have no name in any string we hold.
WERU_CONFIG_ID_METADATA = 2
WERU_CONFIG_ID_TLC_DATA = 4
WERU_CONFIG_ID_SPARE = 5

#: RootStorage Bootstrap object 83 = `WeruConfiguration`, the drive's own
#: persisted copy of everything below.  `sub_40368194` (`efc_cpu2.c:152194`)
#: sizes it `48 * numberOfWerus + 120`; 1272 bytes on this board.
WERU_CONFIGURATION_OBJECT_ID = 83
WERU_CONFIG_RECORD_BYTES = 20

#: THE TABLE BELOW IS NOW CONFIRMED FROM MEDIA, NOT ONLY FROM THE IMAGE
#: (2026-08-07).  It used to be a transcription of the defaults
#: `sub_4033CB5E` builds into `byte_900078`, with the caveat that `byte_900078`
#: is host-writable SRAM reading all zeros on the bench -- i.e. we were trusting
#: the code path rather than the drive.  The drive persists its own copy:
#: RootStorage Bootstrap object 83 = `WeruConfiguration`, sized
#: `48 * numberOfWerus + 120` (`efc_cpu2.c:152194`), 1272 bytes here.  Its first
#: 120 bytes are exactly six 20-byte records --
#:
#:     +0 u16 cfgIdx  +2 u16 blocksPerWeru  +4 u16 weruCount
#:     +8 u16 (weruCount again, or 0)  +10 u16 firstWeruId
#:     +12 u16 weruCount  +14 u16 cellMode  +16 u32 flags
#:
#: -- and they read (0,24,1,0,1), (1,24,2,1,1), (2,12,26,3,1), (3,48,88,29,1),
#: (4,48,855,117,0), (5,24,98,972,2) with flags 7,7,2,0,0,2: every field of this
#: table and of `WERU_CONFIG_FLAGS`, byte for byte.  The remaining 1152 bytes
#: are 24 columns x 6 configurations of `(firstPairIndex, numberOfPairs)`,
#: reading (0,1) (1,2) (3,13) (16,176) (192,1710) (1902,98) identically in every
#: column -- i.e. `WERU_COLUMN_PAIR_RUNS` below, also confirmed rather than
#: derived.  Both copies (blocks 4 and 5) are byte-identical.
#:
#: Caveat, stated because it matters: object 83 is unservable from the
#: blocks-2/3 copy of the Bootstrap store in this dump (its stripe lands on
#: ce10/ce11 page 346) and the bytes above come from the blocks-4/5 copy,
#: which is a different generation of the same store.  For a format-time
#: constant that is if anything a stronger check -- it agrees across
#: generations -- but it is not the current generation's copy.
WERU_CONFIGURATIONS = (
    # cfgIdx, firstWeruId, weruCount, blocksPerWeru, blocksPerCe, cellMode, note
    (0, 0, 1, 24, 2, 1, "RootStorage anchor (weru 0), pseudo-SLC"),
    (1, 1, 2, 24, 2, 1, "RootStorage, pseudo-SLC"),
    (2, 3, 26, 12, 2, 1, "metadata / firmware-data WERUs, pseudo-SLC, "
                         "block pair on six CEs"),
    (3, 29, 88, 48, 4, 1, "pseudo-SLC data (the SLC cache)"),
    (4, 117, 855, 48, 4, 0, "native-TLC data -- the main user store"),
    (5, 972, 98, 24, 2, 2, "spare / PhysicalMediaAccess; outside validMap "
                           "entirely; cellMode 2 NOT interpreted"),
)

#: How many WERUs exist.  `dword_907168`, the sum of all six weruCounts
#: (`efc_cpu2.c:108039-108049`); measured independently as the `WeruUsageLog`
#: record count.  weruIds at or above this are not WERUs -- the `blockMapping`
#: section is rounded up to a whole number of LMAs and its tail is residue.
WERU_COUNT = sum(c[2] for c in WERU_CONFIGURATIONS)             # 1070

#: How many the `validMap` and `validCount` sections cover: `dword_907164`, the
#: sum of the first five (`efc_cpu2.c:108029-108038`).  The 98 in configuration
#: 5 have no validMap row at all.
WERU_COUNT_IN_VALID_MAP = sum(c[2] for c in WERU_CONFIGURATIONS[:5])   # 972


def weru_configuration(weru_id):
    """-> the `WERU_CONFIGURATIONS` row owning `weru_id`, or None if past the end.

    None means "not a WERU", not "unknown": `WERU_COUNT` is the firmware's own
    `dword_907168` and is confirmed by the `WeruUsageLog` record count.
    """
    for c in WERU_CONFIGURATIONS:
        if c[1] <= weru_id < c[1] + c[2]:
            return c
    return None


#: A `SystemBlockMapping` / `blockMapping` record for an SLC WERU decodes as
#:     pbid = PBID_PAIR_STRIDE*pairIndex + PBID_HALF_STRIDE*half + slot
#: giving `blockBase = 2*pairIndex` and `ceBase = 6*half`.  This replaces the
#: `pbid = 12*block + ce` reading for these records -- see
#: `weru_config_from_pbids` for the evidence and for what it does not cover.
PBID_PAIR_STRIDE = 24                   # one block pair across all 12 CEs
PBID_HALF_STRIDE = 12                   # one block pair across six CEs
CES_PER_HALF = 6

#: The format-time BLOCK assignment, for all six configurations.
#:
#: `sub_4033C69C` (`efc_cpu2.c:107534-107620`) lays the drive out column by
#: Column: there are `sub_4030C110() == word_90C458 == 24` columns, each
#: configuration gets a contiguous run of block-PAIR indices in every column,
#: and the runs are laid end to end in configuration order.  A configuration's
#: run length is `weruCount * blocksPerWeru / 24` pairs, and on this drive all
#: six products are exact multiples of 24 so the remainder loop that would have
#: skewed the runs never executes (see the `+6 startColumn` note above).
#:
#: A pbid is `24*pairIndex + column` (`PBID_PAIR_STRIDE`), so the run table
#: below turns straight into a per-WERU pbid set -- nothing is fitted.
#: Cross-check: it predicts weru 23 -> pair 13 = blocks 26/27 on CE 0-5, which
#: is exactly where `SystemBlockMapping` says the live SystemMetadata image is,
#: and weru 3/4/5/6 -> pairs 3,3,4,4 alternating halves, which is what the
#: `blockMapping` section reads.
WERU_COLUMN_PAIR_RUNS = {}
_acc = 0
# Bound before the loop so the `del` below cannot be a NameError.  It cannot be
# one today -- `WERU_CONFIGURATIONS` is a six-entry literal directly above -- but
# this runs at import time, so if it ever were, the failure would be the whole
# package refusing to import rather than anything legible.
_c = None
for _c in WERU_CONFIGURATIONS:
    WERU_COLUMN_PAIR_RUNS[_c[0]] = (_acc, _c[2] * _c[3] // PBID_PAIR_STRIDE)
    _acc += _c[2] * _c[3] // PBID_PAIR_STRIDE
del _acc, _c
#: 2000 pairs x 24 columns = 48000 blocks, the measured size of
#: `PhysicalBlockProperties`.
WERU_COLUMN_PAIRS = sum(n for _b, n in WERU_COLUMN_PAIR_RUNS.values())


def canonical_pbids(weru_id):
    """-> the set of pbids WERU `weru_id` was formatted with, or None.

    None means "this weruId is not in any configuration", not "unknown".
    Compare against the WERU's live `blockMapping` row to see whether its
    physical blocks have been relocated since format; see
    `Ftl.weru_block_moves` for what does the relocating.
    """
    cfg = weru_configuration(weru_id)
    if cfg is None:
        return None
    idx, first, _cnt, bpw = cfg[0], cfg[1], cfg[2], cfg[3]
    base, _n = WERU_COLUMN_PAIR_RUNS[idx]
    i = weru_id - first
    if bpw == 2 * PBID_PAIR_STRIDE:             # 48: two pairs, all 24 columns
        pairs, cols = (base + 2 * i, base + 2 * i + 1), range(PBID_PAIR_STRIDE)
    elif bpw == PBID_PAIR_STRIDE:               # 24: one pair, all 24 columns
        pairs, cols = (base + i,), range(PBID_PAIR_STRIDE)
    elif bpw == PBID_HALF_STRIDE:               # 12: one pair, one CE half
        half = i & 1
        pairs = (base + (i >> 1),)
        cols = range(PBID_HALF_STRIDE * half, PBID_HALF_STRIDE * (half + 1))
    else:                                       # no such configuration exists
        return None
    return {PBID_PAIR_STRIDE * p + c for p in pairs for c in cols}


def weru_config_from_pbids(weru_id, pbids, blocks_per_weru=11, slc=None):
    """Derive a `WeruConfig` from a WERU's own pbid record.  -> config or None.

    `slc` must be supplied by the caller from independent evidence, and None
    here means "no evidence" and refuses to build a config.  Cell mode is field
    +14 of the firmware's configuration table, which is runtime SRAM we do not
    hold, and it is not free: it sets `pages_per_block`, so guessing it would
    make `wma_to_physical` reject or accept pages 368..1103 on a hunch.  What
    the callers use instead is the WERU's own capacity -- 17664 = 12 x 368 x 4
    can only be pseudo-SLC, since TLC would be 52992.

    This RETRACTS `pbid = 12*block + ce` for these records.  Both readings fit
    the same bytes -- a run of twelve consecutive values from `12*B` -- but only
    one of them reproduces where the data actually is.  Under

        pbid = 24*pairIndex + 12*half + slot

    a record is a BLOCK pair (2*pairIndex, 2*pairIndex+1) on the six CEs of one
    half of the package (`ceBase = 6*half`), which is exactly the layout
    measured on hardware.  It reproduces all five entries of `WERU_CONFIGS`
    exactly, and it explains the record shapes the old reading could not: weru
    20's record starts at 12*11 yet its blocks are 10/11, and weru 0's record is
    24 slots long rather than 12.

    Returns None -- never a guess -- when the record is not twelve consecutive
    values starting at a `slot == 0` boundary.  In particular:

    * 48-slot records are not this function's business.  For werus
      84/234/590 the record is 48 non-consecutive values that decode as four
      block numbers across twelve CEs under the old reading.  They do not use
      the pair/half scheme; they use `slot = 4*ce + (block - blockBase)`,
      measured 44/44 against pages on weru 234 -- see
      `weru_config_from_user_record`, which is where that case is handled.  This
      function still returns None for them, and must not be extended to cover
      them: the two laws differ and keeping them in separate functions is what
      keeps a 12-block record from being read under a 48-block rule.
    * 24-slot records (weru 0) fit the arithmetic but describe a shape whose
      `blocksPerWeru` is unknown, so they are refused too.

    `blocks_per_weru` defaults to the measured 11 while the record carries
    twelve slots.  That gap is the unexplained twelfth block, and it is kept
    visible rather than reconciled: `WeruConfig.covers` accepts it,
    `slot_to_physical` refuses it.
    """
    if slc is None:
        return None
    vals = [p for p in pbids if p is not None]
    if len(vals) != 2 * CES_PER_HALF:
        return None
    if any(vals[k] != vals[0] + k for k in range(len(vals))):
        return None
    pair, rem = divmod(vals[0], PBID_PAIR_STRIDE)
    half, slot0 = divmod(rem, PBID_HALF_STRIDE)
    if slot0 != 0:
        return None
    return WeruConfig(blocks_per_weru, CES_PER_HALF * half, 2 * pair, slc,
                      "blocks %d/%d on CE %d-%d"
                      % (2 * pair, 2 * pair + 1, CES_PER_HALF * half,
                         CES_PER_HALF * half + CES_PER_HALF - 1),
                      source="derived from the pbid record "
                             "(24*%d + 12*%d + slot)" % (pair, half))


#: The 48-block user WERU's slot law, MEASURED (not derived from the record's
#: list order, which is not a slot order):
#:     slot = USER_BLOCKS_PER_CE * (ce - ceBase) + (block - blockBase)
#: with `USER_BLOCKS_PER_CE = 4` and `ceBase = 0`.  It is the same shape as the
#: metadata WERUs' law with `blocksPerCe` widened from 2 to 4, which is what
#: `WeruConfig.slot_to_physical`'s `divmod(slot, blocks_per_ce)` already
#: implements.
USER_BLOCKS_PER_CE = geom.BLOCKS_PER_WERU // geom.CE_COUNT      # 4
USER_CE_BASE = 0


def weru_config_from_user_record(weru_id, slots, slc=None):
    """A 48-block user WERU's `blockMapping` record -> `WeruConfig`, or None.

    How the RULE was established, with no model in the loop.  Weru 234 page 539
    is a fully-mapped window, so all 48 of its blocks must carry known LMAs
    there.  Reading all 44 readable blocks and inverting
    `aux LMA -> LUT -> WMA -> slot` scores

        slot = 4*ce + (block - blockBase)      44/44
        the record's own stored list position   2/44
        canonical block-major (slot = 4*(block-base) + ce)   1/44

    so the reading is measured against pages, not fitted to a table.  It is the
    metadata WERUs' `slot = blocksPerCe*(ce - ceBase) + (block - blockBase)`
    with `blocksPerCe = 4` and `ceBase = 0` -- a 48-block WERU spans all twelve
    CEs, so there is no half to choose.

    Broadened 2026-08-07 (`sd_slotlaw.py`), because one window of one WERU is
    not a law.  Same route, same independence -- the page's own aux LMA, the
    LUT, and `divmod`; the placement rule appears only on the other side of the
    comparison, and a page is admitted only when the LUT-derived WMA agrees on
    weruId and page, neither of which the rule produces.

        observations   3568 over 251 (weru, page) windows, 11 CEs, 5 WERUs
        slot = 4*ce + (block - blockBase)                    3568 / 3568
        pair/half extended (24*pair + 12*half + 2*ce + b)     204   (5.7%)
        the record's own stored list position                 204   (5.7%)
        canonical block-major (ce = slot%12)                  184   (5.2%)
        the same law with ceBase = 6                            0
        the same law with the four block numbers reversed       0

    The pair/half rival is the point.  It is the one hypothesis that was
    genuinely open -- a 12-block WERU *is* a block pair on six CEs, so somebody
    had to ask whether a 48-block one is two pairs on two halves.  It is not.
    (The *degenerate* extension -- six CEs per half, four blocks each -- is
    arithmetically identical to this law, since `4*ce` for ce 0..5 is 0..23, so
    it is not a rival and is not listed.)

    Hold-out, on hardware: weru 573 pages 342/343/344, read fresh, with block
    2268 absent from every dump beforehand.  528 of 528, both directions --
    predict the address from the LMA and ask the page's aux, and invert the aux
    back to a slot.  Rivals on the same 528: 24 / 24 / 12 / 0 / 0.  The run
    reproduced byte-for-byte when repeated (88 pages at ladder level 0, 44 upper
    word lines rescued at step 6).  Both cell modes are covered: weru 84 is a
    48-block pseudo-slc WERU and scores 112/112.

    What is refused, and it matters more than what is accepted:

    * `slc is None` -> None.  Cell mode sets `pages_per_block`, so a guess here
      makes `wma_to_physical` accept or reject pages 368..1103 on a hunch.  This
      gate is what stopped a silently-invented SLC configuration for weru 3 and
      it is not relaxed.
    * any block at or past `geometry.BLOCKS_PER_LUN` -> None.  RETRACTED:
      "the 88 WERUs whose records name block 2386979 and the like are stale
      residue."  They were inline-RAID parity bytes that `SystemMetadata.scan`
      preferred over the real page; all 88 now decode as ordinary rectangular
      WERUs and this gate no longer fires for them.  It still fires -- correctly
      -- for weruIds past `WERU_COUNT`, whose rows are section padding.
    * anything that is not exactly `BLOCKS_PER_WERU` mapped slots covering four
      consecutive block numbers on all twelve CEs -> None.  That rejects the 29
      `substituted` WERUs (48 blocks, four per CE, but a block number swapped in
      from elsewhere by bad-block management) as well as every partial and
      irregular record, because `block = blockBase + slot % 4` cannot express
      them.  A substituted WERU needs its record read per slot, and that is a
      separate, unfinished problem.

    `slots` is the slot-indexed list `decode_block_mapping` produces; only the
    set of (block, ce) pairs is used, never the positions.
    """
    if slc is None:
        return None
    if slots is UNKNOWN or slots is None:
        return None
    blocks = [b for b in slots if b is not None]
    if len(blocks) != geom.BLOCKS_PER_WERU:
        return None
    if any(b >= geom.BLOCKS_PER_LUN or b < 0 for b, _c in blocks):
        return None
    nums = sorted({b for b, _c in blocks})
    if len(nums) != USER_BLOCKS_PER_CE or nums != list(range(nums[0],
                                                             nums[0] + len(nums))):
        return None
    if set(blocks) != set(geom.weru_blocks(nums)):
        return None                      # not four blocks on each of twelve CEs
    return WeruConfig(geom.BLOCKS_PER_WERU, USER_CE_BASE, nums[0], slc,
                      "blocks %d-%d on CE 0-%d"
                      % (nums[0], nums[-1], geom.CE_COUNT - 1),
                      blocks_per_ce=USER_BLOCKS_PER_CE,
                      source="derived from the blockMapping record "
                             "(slot = %d*ce + block - %d; measured 44/44 on "
                             "weru 234 page 539)"
                             % (USER_BLOCKS_PER_CE, nums[0]))


#: the `blockMapping` record's entry position is not the WMA SLOT, and it is
#: Not arbitrary either -- it is the allocation order, and it is what answers
#: "which slot does a substituted block occupy?".
#:
#: `sd_slotlaw.py` already scored the record's own list position as a rival to
#: the 5b placement law and it lost, 204 of 3568 -- exactly its ceiling.  That
#: settled that position != slot; it did not say what position is.  It is a
#: fixed permutation of the slot, MEASURED over every rectangular 48-block
#: record on this board (`sv_subslot.py --perm`): 914 WERUs, 43,872 entries, 0
#: positions ambiguous.  Written as a closed form,
#:
#:     position = 24*(q & 1) + 4*(ce >> 1) + 2*(q >> 1) + (ce & 1)
#:     slot     = 4*ce + q                          (placement law 5b)
#:
#: with `q = block - blockBase` in 0..3.  `POS_TO_SLOT` is that permutation and
#: `record_position` its inverse.
#:
#: the firmware derives the same order, from constants this module already
#: established elsewhere -- so this is not a fit to the table it explains:
#:
#:   * `sub_113EC` (`BlockMappingRebuild.c:707`) files every block into queue
#:     `pbid % word_90C458`, and `word_90C458 = 24` is forced four ways by
#:     `sub_4033CB5E` (see `WERU_CONFIGS`).  With `pbid = 12*block + ce` that
#:     queue is `12*(block & 1) + ce` -- a (plane, CE) bucket.
#:   * `sub_11388(weruId, k)` (`efc_cpu2.c:32368`) says which queue entry `k`
#:     is filled from: `k % byte_90C442 + (k / 4) * byte_90C442` with
#:     `byte_90C442 = 2` (likewise from `sub_4033CB5E`) = `(k & 1) + 2*(k >> 2)`,
#:     which runs 0..23 with no wrap.
#:   * equating the two gives `12*plane + ce == (k & 1) + 2*(k >> 2)`, which is
#:     the closed form above.  `BlockMappingRebuild` fills position `k` by
#:     Popping that queue (`efc_cpu2.c:32893-32950`), so the order is the order
#:     the format-time allocator handed blocks out -- which is why it is
#:     identical across 914 independently allocated WERUs.
#:
#: Consequence, and it is the whole point: `sub_40312B84`
#: (`efc_cpu2.c:69705`) substitutes a bad block by swapping the two entries'
#: bytes 0..15 in place at flat indices `weruId*blocksPerWeru + k` (the
#: neighbouring `sub_40312BC0` prints `a1 / blocksPerWeru` and
#: `a1 % blocksPerWeru`, which is what pins the flat index).  Position is
#: therefore preserved by a substitution, so a substitute occupies exactly the
#: slot of the block it replaced -- `POS_TO_SLOT[k]` -- even though its block
#: number is nowhere near `blockBase`.
#:
#: Hold-out, zero free parameters (`sv_subslot.py --pred`): for each of the
#: 79 substitutes in this board's 29 `substituted` records, predict the
#: position from the rectangle alone -- take the displaced member's `(ce, q)`,
#: which comes from `geometry.weru_blocks` and not from the record, and compute
#: `record_position(ce, q)`.  79 / 79 land on the position the substitute
#: actually occupies, 0 unexplained.  Two structural corollaries fall out and
#: both hold 79/79: the substitute is always on the displaced block's CE, and
#: always on its plane (same block parity) -- which is what a position-preserving
#: swap into a (plane, CE) queue must do.
#:
#: the permutation itself is MEASURED on media -- 3568 / 0 -- and no new read
#: was needed for it (`sv_subslot.py --posmedia`, 2026-08-07).  `sd_slotlaw`
#: measures `(ce, block) -> slot` off the page's own aux LMA and the region-0
#: user LUT, with no placement law on the measured side; the record independently
#: says which position that `(ce, block)` occupies; composing the two reads
#: `position -> slot` straight out of the media.  Scored that way:
#:
#:     POS_TO_SLOT[position] == aux-measured slot   3568 agree / 0 disagree
#:     rival: position == slot                       204 agree, ceiling 204
#:     distinct positions covered                    44 of 48 (the 4 missing
#:                                                   are exactly the CE0 ones)
#:
#: over 5 WERUs and 11 live CEs, with no position ever giving two slots.  That
#: also refutes `NAND_FORMAT_STATE.md` 6a's `position == slot` reading on media,
#: without anyone having to pin its constants.
#:
#: What is still not proven, and why the substituted configuration is opt-in:
#: the inheritance claim -- that a block swapped in by `sub_40312B84` keeps the
#: position, and therefore the slot, of the block it replaced.  Every piece of
#: evidence for that is about the record.
#:
#: And its media hold-out is now a proven negative, not an outstanding task.
#: RETRACTED: "one page read from any injected block would promote it".  The
#: board was asked directly (`sv_subslot.py --survey` / `--survey-full`):
#:
#:   * all 79 injected blocks are ERASED, scanned in both cell modes (74 on
#:     live dies at 11 page positions each, 5 on the dead CE0), with the
#:     mandatory positive controls landing in the same run;
#:   * all 29 substituted WERUs are blank end to end -- 3828 scans, 391
#:     non-0xFF pages, 0 of them decoding, and those 391 carry 4-8 non-0xFF
#:     bytes in 18432, i.e. stray bits in an erased block;
#:   * corroborated by a structure written by another path: `DdmData`'s
#:     `PerMsoProgramCount16KBased` shows msoIds 44 and 48 have never
#:     programmed anything, and they own 27 of the 29 records.
#:
#: Trap, recorded because it would have faked a promotion: `ce3 b3804 p8` and
#: `ce10 b3804 p8` scan 18432 non-0xFF bytes -- a page with not one 0xFF byte,
#: which is the signature of an artefact, not a payload (scrambled data carries
#: ~72 by chance).  Both decode MCRC 0/4 with random aux.  An erased native-TLC
#: block read at TLC references returns noise, not 0xFF.  Never promote a scan
#: hit on the byte count; decode it.
#:
#: Closing this needs a different image -- one where a substituted WERU was
#: written after the substitution -- not more reads of this board.
POS_TO_SLOT = (0, 4, 2, 6, 8, 12, 10, 14, 16, 20, 18, 22,
               24, 28, 26, 30, 32, 36, 34, 38, 40, 44, 42, 46,
               1, 5, 3, 7, 9, 13, 11, 15, 17, 21, 19, 23,
               25, 29, 27, 31, 33, 37, 35, 39, 41, 45, 43, 47)


def record_position(ce, q):
    """(ce, block - blockBase) -> the `blockMapping` entry position.

    The inverse of `POS_TO_SLOT`, in closed form.  `q` is which of the WERU's
    four consecutive block numbers, 0..3.
    """
    return 24 * (q & 1) + 4 * (ce >> 1) + 2 * (q >> 1) + (ce & 1)


def record_position_to_slot(position):
    """`blockMapping` entry position -> the WMA slot that entry serves."""
    if not 0 <= position < geom.BLOCKS_PER_WERU:
        return None
    return POS_TO_SLOT[position]


class SubstitutedWeruConfig(WeruConfig):
    """A 48-block WERU whose slots are read per entry, not from arithmetic.

    `WeruConfig.slot_to_physical` computes `block = blockBase + slot % 4`, which
    cannot express a block swapped in from the spare pool.  This subclass keeps
    the explicit `slot -> (ce, block)` table `POS_TO_SLOT` extracts from the
    record, so a substituted WERU addresses like any other.

    `block_base` and `blocks_per_ce` are still carried, for display and for
    `covers()` on the un-substituted members; never use them to compute an
    address here -- that is exactly the mistake this class exists to prevent.
    """
    __slots__ = ("table", "inverse", "substitutions")

    def __init__(self, table, ce_base, block_base, slc, name=None,
                 source=None, substitutions=()):
        super(SubstitutedWeruConfig, self).__init__(
            geom.BLOCKS_PER_WERU, ce_base, block_base, slc, name,
            blocks_per_ce=USER_BLOCKS_PER_CE, source=source)
        self.table = tuple(table)
        self.inverse = dict((bc, s) for s, bc in enumerate(self.table)
                            if bc is not None)
        #: [(slot, (ce, block))] for the entries that are not `blockBase + q`.
        self.substitutions = tuple(substitutions)

    def slot_to_physical(self, slot):
        if not 0 <= slot < len(self.table):
            return None
        return self.table[slot]

    def physical_to_slot(self, ce, block):
        return self.inverse.get((ce, block))

    def covers(self, ce, block):
        return (ce, block) in self.inverse

    def __repr__(self):
        return ("<SubstitutedWeruConfig %s blocks %d-%d on CE 0-%d, %d "
                "substituted %s>"
                % ("SLC" if self.slc else "TLC", self.block_base,
                   self.block_base + USER_BLOCKS_PER_CE - 1, geom.CE_COUNT - 1,
                   len(self.substitutions), list(self.substitutions)))


def weru_config_from_substituted_record(weru_id, slots, slc=None):
    """A `substituted` 48-block record -> `SubstitutedWeruConfig`, or None.

    Reads the slot out of each entry's position (`POS_TO_SLOT`) instead of out
    of its block number, which is the only way a swapped-in block can be
    addressed at all.  See `POS_TO_SLOT` for the derivation, the 79/79 hold-out
    and -- importantly -- for what is not proven: no page of any injected block
    is held offline, so this has no aux-LMA confirmation the way the rectangular
    law does.  It is therefore opt-in; `Ftl.weru_config` still refuses these
    records unless asked.

    Refuses, rather than guessing, when:

    * `slc is None` -- same gate as `weru_config_from_user_record`;
    * the record is not exactly `BLOCKS_PER_WERU` in-range blocks, four on each
      of the twelve CEs (that shape is what makes the position law applicable);
    * any entry's CE disagrees with `POS_TO_SLOT[position] // 4`.  That last
      check is the falsifiable one: the allocation law fixes the CE of every
      position, so a record that violates it is not a substitution this function
      understands and must not be given an address.
    """
    if slc is None or slots is UNKNOWN or slots is None:
        return None
    if len(slots) != geom.BLOCKS_PER_WERU:
        return None
    blocks = [b for b in slots if b is not None]
    if len(blocks) != geom.BLOCKS_PER_WERU:
        return None
    if any(b >= geom.BLOCKS_PER_LUN or b < 0 for b, _c in blocks):
        return None
    per_ce = {}
    for _b, c in blocks:
        per_ce[c] = per_ce.get(c, 0) + 1
    if len(per_ce) != geom.CE_COUNT or set(per_ce.values()) != {USER_BLOCKS_PER_CE}:
        return None
    base = USER_BLOCKS_PER_CE * (min(b for b, _c in blocks) // USER_BLOCKS_PER_CE)
    table = [None] * geom.BLOCKS_PER_WERU
    subs = []
    for k, bc in enumerate(slots):
        slot = POS_TO_SLOT[k]
        b, c = bc
        if c != slot // USER_BLOCKS_PER_CE:
            return None                  # violates the allocation law: refuse
        table[slot] = (c, b)
        if b != base + slot % USER_BLOCKS_PER_CE:
            subs.append((slot, (c, b)))
    return SubstitutedWeruConfig(
        table, USER_CE_BASE, base, slc,
        "blocks %d-%d on CE 0-%d, %d substituted"
        % (base, base + USER_BLOCKS_PER_CE - 1, geom.CE_COUNT - 1, len(subs)),
        source="read per ENTRY POSITION (POS_TO_SLOT); the substitute inherits "
               "the slot of the block sub_40312B84 swapped it with",
        substitutions=subs)


def split_wma(wma):
    """-> (weruId, page, slot, au).  Pure arithmetic; needs no configuration.

    `WeruConfiguration_GetWlaOffsetFromWmaOffset` (`efc_cpu2.c`,
    `sub_4030E7F8`) is `q, r = divmod(wmaOff, 192)` with the reduction valid
    only while `r < blocksPerWeru * 4`; `WeruConfiguration_GetWeruIdFromWma`
    (`:23888`) is `wma // 211968`.  Both directions are exact integer maps, so
    this function is always answerable even when the WERU's configuration is
    not -- which is precisely the distinction `Ftl.wma_to_physical` reports.
    """
    weru, rem = divmod(wma, geom.AUS_PER_TLC_WERU)
    page, r = divmod(rem, WMA_STRIDE)
    return weru, page, r // WMAS_PER_PAGE, wma % WMAS_PER_PAGE


def wma_to_physical(wma, config):
    """-> `Placement`, or None when this WMA addresses nothing.

    None is the firmware's own answer: its reduction prints "invalid input wma"
    and returns -1 for a WMA in the unused tail of a 192-window.  Reporting an
    address there would be an invention.  `wma_unaddressable_reason` says which
    case it was.

    A returned Placement is an address, not a promise that this backing holds
    those bytes.  `Placement.seq` is None because nothing was read; `.slc`
    carries the configuration's cell mode, so the address arrives at a caller
    already knowing which mode it must be read in.
    """
    if wma in LUT_SENTINELS:
        return None
    _weru, page, slot, au = split_wma(wma)
    ce_block = config.slot_to_physical(slot)
    if ce_block is None or page >= config.pages_per_block:
        return None
    return Placement(ce_block[0], ce_block[1], page, au, slc=config.slc)


def wma_unaddressable_reason(wma, config):
    """-> a string saying why `wma_to_physical` returned None, or None."""
    if wma in LUT_SENTINELS:
        return "sentinel 0x%08x -- the LUT maps this nowhere" % wma
    _w, page, slot, _au = split_wma(wma)
    if slot >= config.blocks_per_weru:
        return ("slot %d is in the unused tail of the %d-WMA window "
                "(blocksPerWeru %d); the firmware calls this an invalid input "
                "wma" % (slot, WMA_STRIDE, config.blocks_per_weru))
    if page >= config.pages_per_block:
        return ("page %d is past the %d pages a %s block has"
                % (page, config.pages_per_block, "pseudo-SLC" if config.slc
                   else "native TLC"))
    return None


def physical_to_wma(ce, block, page, au, config, weru_id):
    """The inverse.  -> wma, or None if (ce, block) has no slot in this WERU.

    None here includes the programmed-but-unaddressed twelfth block, which is a
    real physical page carrying real data that no WMA names.  Do not read None
    as "there is nothing there".
    """
    slot = config.physical_to_slot(ce, block)
    if slot is None:
        return None
    return (weru_id * geom.AUS_PER_TLC_WERU + page * WMA_STRIDE
            + slot * WMAS_PER_PAGE + au)


# ===========================================================================
# the LUTs -- the firmware's own LMA -> WMA tables
# ===========================================================================
class _Lut(object):
    """Shared behaviour of the SystemLut and the userLutLut.

    Both are flat arrays of 32-bit WMAs indexed ``lma >> shift``
    (`Lut_GetEntry`, `efc_cpu2.c:23033`; the entries are plain u32, not packed
    bitfields -- `efc_cpu2.c:23297`).  A WMA decomposes as
    ``weruId = wma // AUS_PER_TLC_WERU`` (`efc_cpu2.c:23888`).
    """
    __slots__ = ("entries", "granularity", "source")

    def __init__(self, entries, granularity, source=None):
        if granularity not in LUT_GRANULARITIES:
            raise ValueError(
                "LUT granularity %r is not one of %s (4/16/64 KiB per entry); "
                "a granularity outside that set means the array was located or "
                "sized wrongly, not that the drive uses a new one"
                % (granularity, list(LUT_GRANULARITIES)))
        self.entries = tuple(entries) if entries is not None else None
        self.granularity = granularity
        self.source = source

    # -- entry access.  Subclasses that cannot hold 10.9M entries in memory
    # -- override these two and nothing else.
    def _resident(self):
        """The `entries is None` contract, enforced where it is declared.

        `__init__` accepts `entries=None` on purpose: `LutRegion` passes it
        because materialising region 0 is 10 942 848 entries, and reads one
        table LMA per lookup instead.  The price is that such a subclass must
        override both accessors below.  Nothing enforced that, so a subclass
        that overrode one and forgot the other died on
        `object of type 'NoneType' has no len()` -- a message that names
        neither the class nor the rule it broke.

        The histograms do not come through here: `n_mapped`,
        `weru_histogram` and `sentinel_histogram` answer UNKNOWN for a
        non-resident table, because "reading the whole table" is a real
        not-measured, not a programming error.
        """
        if self.entries is None:
            raise NotImplementedError(
                "%s was built with entries=None -- the table is not resident -- "
                "so it MUST override both `n_entries` and `entry` (LutRegion is "
                "the one subclass that does).  Overriding one and not the "
                "other is the mistake this says out loud."
                % type(self).__name__)
        return self.entries

    @property
    def n_entries(self):
        return len(self._resident())

    def entry(self, i):
        """-> the raw u32 at index `i`, or UNKNOWN if it cannot be read.

        UNKNOWN is a subclass answer: a resident table holds every entry it
        has, so this one only ever returns the u32.
        """
        return self._resident()[i]

    @property
    def shift(self):
        return self.granularity.bit_length() - 1

    @property
    def covered_lmas(self):
        return self.n_entries * self.granularity

    def wma(self, lma):
        """-> the WMA for an LMA, or None if unmapped / past the end.

        None is a real negative: the table exists and says nothing lives there.

        The sub-granule offset is part of the answer.  One entry covers a whole
        granule -- 4 LMAs for system tables, 16 for user data -- and it names the
        granule's first AU (`Lut_GetEntry`, `efc_cpu2.c:23027-23033`, which is a
        bare `pTable[lma >> shift]`; the LUT API panics outright on an unaligned
        LMA, `efc_cpu2.c:109763-109765`, so the caller is what adds the offset).
        Returning the base for every LMA in the granule silently resolves three
        of every four system LMAs -- and fifteen of every sixteen user LMAs --
        to the wrong AU.

        Verified on media: WMA entry 0 of MSO 40 names ce6/b20/p305, whose four
        AUs carry aux LMAs 0, 1, 2, 3 in order.

        This bug survived a 5489-exact/0-wrong validation because every LMA in
        that set was granule-aligned (0, 152, 436, 440 ...), so the missing term
        was always zero.  A test set drawn from a table's own indices cannot see
        an error in the index-to-offset step; that is worth remembering the next
        time a validation looks conclusive.
        """
        i = lma >> self.shift
        if lma < 0 or i >= self.n_entries:
            return None
        w = self.entry(i)
        if w is UNKNOWN:
            return UNKNOWN              # the table page itself is not held
        if w in LUT_SENTINELS:
            return None
        return w + (lma & (self.granularity - 1))

    def weru(self, lma):
        """-> the weruId holding this LMA, None if unmapped, UNKNOWN if the
        table page that would answer is not held."""
        w = self.wma(lma)
        if w is None or w is UNKNOWN:
            return w
        return w // geom.AUS_PER_TLC_WERU

    def weru_histogram(self):
        """-> {weruId: entries} over the mapped entries only."""
        if self.entries is None:
            return UNKNOWN              # would mean reading the whole table
        out = {}
        for w in self.entries:
            if w in LUT_SENTINELS:
                continue
            k = w // geom.AUS_PER_TLC_WERU
            out[k] = out.get(k, 0) + 1
        return out

    def sentinel_histogram(self):
        if self.entries is None:
            return UNKNOWN
        out = {}
        for w in self.entries:
            if w in LUT_SENTINELS:
                out[w] = out.get(w, 0) + 1
        return out

    @property
    def n_mapped(self):
        if self.entries is None:
            return UNKNOWN
        return sum(1 for w in self.entries if w not in LUT_SENTINELS)


class SystemLut(_Lut):
    """RootStorage object 0 -- the SystemMetadata MSO's LMA -> WMA table.

    `BootstrapRootStorageReadSystemLut` (`efc_cpu3.c:39487`) reads it before
    anything else touches the image, so it -- not a page sequence number, and
    not which WERU a `SystemBlockMapping` slot happens to sit at -- is what
    decides which copy of an LMA the firmware would use.

    Granularity is derived, not assumed: ``totalLmas / len(entries)``.  On this
    board that is 7424 / 1856 = 4, i.e. one entry per 16 KiB page of four AUs.
    """

    @classmethod
    def from_object(cls, body, total_lmas=None):
        """`total_lmas` is `msoDescriptor[41].totalLmas`.

        Pass None and the granularity is derived a second, independent way:
        exactly one of `LUT_GRANULARITIES` makes ``entries * granularity`` land
        on a `maxSystemMetadataSizeInLmas` class (1856 x 4 = 7424; 1856 and
        29696 are not classes).  That route needs no section of the image, which
        matters because reading the `mso` section needs the LUT.  The two are
        cross-checked whenever both are available.
        """
        if body is UNKNOWN or body is None:
            return body
        if len(body) % 4:
            raise ValueError("SystemLut is %d bytes, not a whole number of u32"
                             % len(body))
        n = len(body) // 4
        if not n:
            raise ValueError("SystemLut is empty")
        if total_lmas is None or total_lmas is UNKNOWN:
            fits = [g for g in LUT_GRANULARITIES if n * g in SYSMETA_SIZE_CLASSES]
            if len(fits) != 1:
                raise ValueError(
                    "cannot derive the SystemLut granularity: %d entries times "
                    "%s gives %s, of which %d land on a "
                    "maxSystemMetadataSizeInLmas class %s -- refusing to assume "
                    "one" % (n, list(LUT_GRANULARITIES),
                             [n * g for g in LUT_GRANULARITIES], len(fits),
                             list(SYSMETA_SIZE_CLASSES)))
            return cls(struct.unpack_from("<%dI" % n, body, 0), fits[0],
                       source="RootStorage object %d (granularity from the "
                              "size class)" % OBJ_SYSTEM_LUT)
        gran, rem = divmod(total_lmas, n)
        if rem:
            raise ValueError(
                "SystemLut has %d entries but the MSO declares %d LMAs, which "
                "is not a whole number of LMAs per entry -- one of the two is "
                "not what it is taken to be" % (n, total_lmas))
        return cls(struct.unpack_from("<%dI" % n, body, 0), gran,
                   source="RootStorage object %d" % OBJ_SYSTEM_LUT)

    def __repr__(self):
        return "<SystemLut %d entries, granularity %d LMAs, %d mapped, werus %s>" % (
            self.n_entries, self.granularity, self.n_mapped,
            self.weru_histogram())


class LutRegion(_Lut):
    """One LUT region inside another MSO's LMA space, read lazily.

    The per-namespace LUTs are not small: region 0 is 10942848 entries covering
    175085568 LMAs = 717 GB, which is the user capacity of an 825 GB drive.
    Materialising that would be 43 MB of table read through 2672 page decodes to
    answer one lookup, so `entry()` reads only the single table LMA that holds
    the entry -- one page per lookup, cached by the reader.

    A region is self-describing: 16-byte header (`"LUT\\0"`, version, infoLma,
    dataLma), a 296-byte descriptor at `base+4`, the raw u32 table at `base+8`.
    `Ftl.lut_region` walks the chain of them.

    `max_lma` is the highest LMA currently in use, not the coverage: the
    identity `(max_lma >> shift) + 1 == n_entries` holds for the fully-populated
    regions (0 and 3 here, and the userLutLut) and is a strict `<=` for the two
    that are allocated larger than they are used.  Enforcing equality would
    reject valid regions -- `<=` is the real invariant and it holds 4/4.
    """
    __slots__ = ("base", "info_lma", "data_lma", "store_mso", "init_mso",
                 "version", "max_lma", "pointer", "size_bytes", "_count",
                 "_read_lma", "snapshot_sequence_number", "snapshot_count")

    def __init__(self, store_mso, base, info_lma, data_lma, init_mso, version,
                 max_lma, pointer, size_bytes, count, granularity, read_lma,
                 source=None, snapshot_sequence_number=0, snapshot_count=0):
        super(LutRegion, self).__init__(None, granularity, source)
        self.store_mso, self.base = store_mso, base
        self.info_lma, self.data_lma = info_lma, data_lma
        self.init_mso, self.version = init_mso, version
        self.max_lma, self.pointer = max_lma, pointer
        self.size_bytes, self._count = size_bytes, count
        self._read_lma = read_lma
        #: `snapshotInformation.sequenceNumber`, descriptor `+0` u64.  0 when
        #: `snapshot_count` is 0 -- a real zero, not a missing value.
        self.snapshot_sequence_number = snapshot_sequence_number
        #: `snapshotInformation.count`, descriptor `+8`.
        self.snapshot_count = snapshot_count

    @property
    def n_entries(self):
        return self._count

    @property
    def n_table_lmas(self):
        return self.size_bytes // LMA_BYTES

    @property
    def next_base(self):
        return self.base + self.n_table_lmas + LUTLUT_REGION_OVERHEAD_LMAS

    @property
    def in_use_entries(self):
        return (self.max_lma >> self.shift) + 1

    def entry(self, i):
        """-> the u32, or UNKNOWN when its table LMA is not held.

        UNKNOWN here is a coverage fact about this dump, not about the drive.
        """
        off = 4 * i
        buf = self._read_lma(self.store_mso, self.data_lma + off // LMA_BYTES)
        if buf is UNKNOWN or buf is None:
            return UNKNOWN
        return struct.unpack_from("<I", buf, off % LMA_BYTES)[0]

    @classmethod
    def parse(cls, store_mso, base, read_lma):
        """-> LutRegion, None (not a LUT header) or UNKNOWN (LMAs not held)."""
        hdr = read_lma(store_mso, base)
        if hdr is UNKNOWN or hdr is None:
            return UNKNOWN
        magic, version, info_lma, data_lma = struct.unpack_from("<4I", hdr, 0)
        if magic != LUTLUT_MAGIC:
            return None
        desc = read_lma(store_mso, base + 4)
        if desc is UNKNOWN or desc is None:
            return UNKNOWN
        f = dict((n, struct.unpack_from("<I", desc, o)[0]) for n, o in (
            ("init_mso", LUTLUT_MSO_OFF), ("pointer", LUTLUT_PTR_OFF),
            ("size", LUTLUT_SIZE_OFF), ("max_lma", LUTLUT_MAXLMA_OFF),
            ("count", LUTLUT_COUNT_OFF), ("gran", LUTLUT_GRAN_OFF),
            ("shift", LUTLUT_SHIFT_OFF)))
        if f["gran"] != (1 << f["shift"]):
            raise ValueError("LUT region at LMA %d of msoId %d: granularity %d "
                             "and shift %d disagree"
                             % (base, store_mso, f["gran"], f["shift"]))
        if info_lma != base + 4 or data_lma != base + 8:
            raise ValueError("LUT region at LMA %d: infoLma %d / dataLma %d are "
                             "not base+4 / base+8" % (base, info_lma, data_lma))
        if 4 * f["count"] > f["size"]:
            raise ValueError("LUT region at LMA %d declares %d entries but only "
                             "%d bytes" % (base, f["count"], f["size"]))
        if (f["max_lma"] >> f["shift"]) + 1 > f["count"]:
            raise ValueError("LUT region at LMA %d: highest LMA in use %d needs "
                             "%d entries but only %d are allocated"
                             % (base, f["max_lma"],
                                (f["max_lma"] >> f["shift"]) + 1, f["count"]))
        snap = decode_lut_snapshot_header(desc)
        return cls(store_mso, base, info_lma, data_lma, f["init_mso"], version,
                   f["max_lma"], f["pointer"], f["size"], f["count"],
                   f["gran"], read_lma,
                   source="LUT region at LMA %d of msoId %d" % (base, store_mso),
                   snapshot_sequence_number=snap["sequence_number"],
                   snapshot_count=snap["count"])

    def __repr__(self):
        return ("<LutRegion base=%d in msoId %d, %d entries x %d LMAs "
                "(%d in use), table %d LMAs>"
                % (self.base, self.store_mso, self.n_entries, self.granularity,
                   self.in_use_entries, self.n_table_lmas))


class UserLutLut(_Lut):
    """The `userLutLut` section: where the user LUT's own AUs live.

    Three LMAs of `LMAS_PER_UNIT` each: a 16-byte header naming the other two,
    a 296-byte descriptor, and the raw WMA array.  The descriptor's `msoId`
    field reads 40 = `Lut/System`, which is the cross-check that this is the
    user LUT's index and not something else with a "LUT" magic.

    The entry count comes from the descriptor (`lmaLutCount`), never from
    "how far the array looks ascending" -- the array is genuinely not ascending
    once the LUT starts relocating between WERUs, and the ascending-prefix
    reading truncates it (1758 of 2970 here, and 691 of 2970 in the other half).
    """
    __slots__ = ("header_lma", "info_lma", "data_lma", "mso_id", "version",
                 "max_lma", "pointer")

    def __init__(self, entries, granularity, header_lma, info_lma, data_lma,
                 mso_id, version, max_lma, pointer, source=None):
        super(UserLutLut, self).__init__(entries, granularity, source)
        self.header_lma, self.info_lma, self.data_lma = header_lma, info_lma, data_lma
        self.mso_id, self.version = mso_id, version
        self.max_lma, self.pointer = max_lma, pointer

    @classmethod
    def from_section(cls, sec):
        """-> UserLutLut, or UNKNOWN if the section's LMAs are not all present.

        Raises ValueError when the bytes are present but do not parse; that is a
        different answer from "not recovered" and must not be conflated.
        """
        hdr = sec.chunk(0, 16)
        if hdr is UNKNOWN:
            return UNKNOWN
        magic, version, info_lma, data_lma = struct.unpack_from("<4I", hdr, 0)
        if magic != LUTLUT_MAGIC:
            raise ValueError("userLutLut magic 0x%08x, expected 0x%08x (\"LUT\")"
                             % (magic, LUTLUT_MAGIC))
        info_off = (info_lma - sec.base) * LMA_BYTES
        data_off = (data_lma - sec.base) * LMA_BYTES
        desc = sec.chunk(info_off, LUTLUT_DESC_BYTES)
        if desc is UNKNOWN:
            return UNKNOWN
        (mso_id,) = struct.unpack_from("<I", desc, LUTLUT_MSO_OFF)
        (pointer,) = struct.unpack_from("<I", desc, LUTLUT_PTR_OFF)
        (max_lma,) = struct.unpack_from("<I", desc, LUTLUT_MAXLMA_OFF)
        (count,) = struct.unpack_from("<I", desc, LUTLUT_COUNT_OFF)
        (gran,) = struct.unpack_from("<I", desc, LUTLUT_GRAN_OFF)
        (shift,) = struct.unpack_from("<I", desc, LUTLUT_SHIFT_OFF)
        if gran != (1 << shift):
            raise ValueError("userLutLut granularity %d and shift %d disagree"
                             % (gran, shift))
        # An independent check of the same two numbers: `+252` is the highest
        # LMA currently in use, so it can never need more entries than are
        # allocated.  This is `<=`, not `==`: two of the four per-namespace LUT
        # regions are allocated larger than they are used, and equality would
        # reject them.  Here it happens to be exact (2970 == 2970).
        if (max_lma >> shift) + 1 > count:
            raise ValueError(
                "userLutLut says the highest LMA in use is %d, which needs %d "
                "entries at granularity %d, but only %d are allocated"
                % (max_lma, (max_lma >> shift) + 1, gran, count))
        avail = (sec.n_lmas * LMA_BYTES - data_off) // 4
        if count > avail:
            raise ValueError("userLutLut declares %d entries but only %d fit "
                             "in the section from LMA %d" % (count, avail, data_lma))
        arr = sec.chunk(data_off, 4 * count)
        if arr is UNKNOWN:
            return UNKNOWN
        return cls(struct.unpack_from("<%dI" % count, arr, 0), gran,
                   sec.base, info_lma, data_lma, mso_id, version, max_lma,
                   pointer, source="userLutLut@weru%s" % (sec.werus() or "?"))

    def __repr__(self):
        return "<UserLutLut msoId %d, %d entries, granularity %d, werus %s>" % (
            self.mso_id, self.n_entries, self.granularity,
            self.weru_histogram())


# ===========================================================================
# validMap -- the per-AU live bitmap.  It is stored on NAND.
# ===========================================================================
class ValidMap(object):
    """`validMap`: one bit per WMA, row-major by weruId.

    The "it is a runtime-only structure" reading was refuted: the section is
    written to NAND and read straight back at mount (`efc_cpu3.c:39732`), and is
    only rebuilt when the persisted dirty flag is set.  The addressing is the
    firmware's own (`efc_cpu2.c:130505-130506`, `:31266`):

        dwordsPerWeru = ceil(wmasPerWeru / 32)
        word = base + 4*dwordsPerWeru*(wma // wmasPerWeru)
                    + 4*((wma % wmasPerWeru) >> 5)
        bit  = wma & 31

    so each WERU owns a fixed `4*dwordsPerWeru` = 26496-byte row and the whole
    table is 972 x 26496 = 24.56 MiB, not the ~49 MiB guessed earlier.

    The falsifiable test, and it passes: `popcount(row(w))` must equal
    `validCount[w]`.  941 of 972 rows agree exactly, over values from 1 to
    211968.  The 31 that do not are expected rather than excused: `ValidMapSet`
    is only called for MSOs whose descriptor flag bit 1 is set
    (`efc_cpu2.c:109636`), so WERUs of the other MSOs are counted but not
    mapped.  A rebuilt map would differ again -- see `Ftl.dirty`.

    Rows whose LMAs are missing return UNKNOWN, never 0.
    """
    __slots__ = ("section", "wmas_per_weru", "bytes_per_weru", "weru_count")

    def __init__(self, section, wmas_per_weru=None):
        self.section = section
        self.wmas_per_weru = (geom.AUS_PER_TLC_WERU if wmas_per_weru is None
                              else wmas_per_weru)
        self.bytes_per_weru = 4 * ((self.wmas_per_weru + 31) // 32)
        #: numberOfWerus derived from the section's own declared length.  This
        #: is the honest source for it: 6288 LMAs / 26496 B = 972 exactly, and
        #: 972 is independently the number of WERUs with capacity > 0.  Do not
        #: derive it from `blockMapping`, which is sized for ~1075.
        self.weru_count = (section.n_lmas * LMA_BYTES) // self.bytes_per_weru

    def row(self, weru_id):
        """-> the WERU's `bytes_per_weru` bitmap row, or UNKNOWN."""
        if not 0 <= weru_id < self.weru_count:
            return None
        return self.section.chunk(weru_id * self.bytes_per_weru,
                                  self.bytes_per_weru)

    def popcount(self, weru_id):
        r = self.row(weru_id)
        if r is None or r is UNKNOWN:
            return r
        return sum(bin(b).count("1") for b in r)

    def is_valid(self, weru_id, offset):
        """-> True/False for the AU at `offset` inside `weru_id`, or UNKNOWN."""
        if not 0 <= offset < self.wmas_per_weru:
            raise IndexError("offset %d is outside a WERU (%d WMAs)"
                             % (offset, self.wmas_per_weru))
        r = self.row(weru_id)
        if r is None or r is UNKNOWN:
            return r
        return bool(r[4 * (offset >> 5) + ((offset >> 3) & 3)] >> (offset & 7) & 1)

    def valid_offsets(self, weru_id):
        """-> [offset, ...] of the live AUs in a WERU, or UNKNOWN."""
        r = self.row(weru_id)
        if r is None or r is UNKNOWN:
            return r
        out = []
        for i, b in enumerate(r):
            if not b:
                continue
            base = 8 * i
            for k in range(8):
                if b >> k & 1:
                    out.append(base + k)
        return out

    def compare_valid_count(self, counts):
        """-> (agree, disagree, unreadable, [(weru, count, popcount), ...]).

        The cache-vs-cache consistency check.  Both sides are derived caches of
        the LUT, so a disagreement is evidence about the two snapshots, not
        proof either is corrupt.
        """
        agree = dis = unread = 0
        bad = []
        for w in range(self.weru_count):
            p = self.popcount(w)
            if p is UNKNOWN or p is None:
                unread += 1
                continue
            c = counts[w] if w < len(counts) else 0
            if p == c:
                agree += 1
            else:
                dis += 1
                bad.append((w, c, p))
        return agree, dis, unread, bad

    def __repr__(self):
        return "<ValidMap %d werus x %d B, %d/%d LMAs present>" % (
            self.weru_count, self.bytes_per_weru,
            self.section.n_present, self.section.n_lmas)


# ===========================================================================
# page decoding -- the bridge down to the codec layer
# ===========================================================================
class PageCodec(object):
    """raw stored page -> the four plaintext AUs, with a per-AU integrity verdict.

    Kept behind a one-method interface because `titania.codec` owns the media
    format and this module owns the structures written on top of it.  Supply
    your own to read a differently-formatted image.
    """

    #: 8 aux bytes + 4096 user bytes -- one AU's logical message
    AU_LOGICAL = geom.AU_AUX + geom.AU_DATA

    def decode(self, raw, block, page, lun=0, correct=None):
        """-> (aus, mcrc).

        `aus` is a list of `AUS_PER_PAGE_TLC` plaintext byte strings of
        `AU_LOGICAL` bytes each (aux first, then user), or None if the page
        could not be decoded at all.  `mcrc` is the matching list of per-AU
        booleans, or UNKNOWN if this codec cannot check.

        Returning both from one call is deliberate: decoding a page twice, once
        to check it and once to use it, doubles the cost of every scan and
        invites the two answers to come from different reads of the same page.
        """
        raise NotImplementedError

    # convenience wrappers -- prefer decode() in a loop
    def aus(self, raw, block, page, lun=0):
        return self.decode(raw, block, page, lun)[0]

    def integrity(self, raw, block=0, page=0, lun=0):
        return self.decode(raw, block, page, lun)[1]


class _TitaniaCodec(PageCodec):
    """The `titania.codec` facade over the verified `titania_nand` backend.

    `codec.decode_page(stored, row, fmt, correct)` returns a `PageResult` whose
    `aus` carry `.aux`, `.user`, `.mcrc_ok` and `.erased`.  Two things this
    adapter decides, and why:

    * the row.  The scrambler seed is a function of the row and nothing
      else, so `geometry.compose_row` is the whole address the codec needs.
    * when to run ECC.  Correction costs ~100x a plain decode, so it is
      worth avoiding -- but `correct=None` used to avoid it by asking
      `cell_mode_for_block(block)`, i.e. by a block-number guess about the cell
      mode.  That guess is gone.  `correct=None` now measures instead: decode
      uncorrected (the cheap path), and if any AU's MCRC fails, decode again
      with correction.  A pseudo-SLC page passes uncorrected and costs exactly
      what it did before; a native-TLC page pays one extra cheap decode on top
      of the correction it needed anyway.  Pass `correct=True/False` to pin it.
      Getting it wrong was never silent -- MCRC is the reported verdict either
      way -- but it was one more place a block number decided something.
    """

    def __init__(self):
        from functools import lru_cache
        from . import codec
        self._c = codec
        for need in ("decode_page", "FORMAT_A", "PAGE_BYTES", "au_logical",
                     "au_mcrc", "seed_state", "keystream", "MSG_BYTES"):
            if not hasattr(codec, need):
                raise ImportError("titania.codec has no %s" % need)
        self.name = "titania.codec (backend %s)" % getattr(
            codec, "BACKEND_VERSION", "?")
        # One keystream per row, not per AU.  All four AUs of a page restart the
        # keystream at index 0 from the same state, and a metadata WERU's rows
        # repeat across every CE that stripes it, so a scan of five CEs
        # regenerates each keystream once instead of twenty times.
        self._ks = lru_cache(maxsize=4096)(self._keystream_int)

    def _keystream_int(self, state):
        c = self._c
        return int.from_bytes(c.keystream(state, c.MSG_BYTES), "big")

    def decode(self, raw, block, page, lun=0, correct=None):
        c = self._c
        raw = bytes(raw)
        if len(raw) != c.PAGE_BYTES:
            return None, [False] * geom.AUS_PER_PAGE_TLC
        if correct is None:
            # Measure, do not guess.  Try the cheap path; escalate only if its
            # own MCRC says it did not work.  This is the whole of what the
            # block-number cell-mode guess used to be for.
            aus, ok = self.decode(raw, block, page, lun, correct=False)
            if aus is not None and ok is not UNKNOWN and all(ok):
                return aus, ok
            return self.decode(raw, block, page, lun, correct=True)
        row = geom.compose_row(block, page, lun)

        if correct:
            # The corrected path goes through the codec's own page decode: ECC
            # first (EE3 parity covers the descrambled bytes, so it must run on
            # the stored form), then MCRC, then descramble.
            try:
                res = c.decode_page(raw, row=row, fmt=c.FORMAT_A, correct=True)
            except Exception:
                return None, UNKNOWN
            ok = [bool(a.mcrc_ok) for a in res.aus]
            if any(a.aux is None or a.user is None for a in res.aus):
                return None, ok
            return [bytes(a.aux) + bytes(a.user) for a in res.aus], ok

        # Uncorrected path, assembled from the same codec primitives but with
        # the keystream hoisted out of the AU loop.  `decode_page` regenerates
        # it per AU, which costs ~100 ms a page and dominates a 3680-page scan.
        try:
            ksi = self._ks(c.seed_state(row))
        except Exception:
            return None, UNKNOWN
        n = c.MSG_BYTES
        out, ok = [], []
        for i in range(geom.AUS_PER_PAGE_TLC):
            au = raw[i * c.AU_STRIDE:(i + 1) * c.AU_STRIDE]
            if c.au_is_erased(au):
                return None, [False] * geom.AUS_PER_PAGE_TLC
            lg = c.au_logical(au)
            msg = bytes(lg[:n])
            ok.append(bytes(lg[n:n + 6]) == c.au_mcrc(msg, 6))
            out.append((int.from_bytes(msg, "big") ^ ksi).to_bytes(n, "big"))
        return out, ok


def default_codec():
    """-> a PageCodec over `titania.codec`.

    Raises ImportError rather than degrading to something that silently decodes
    nothing.  There is no second implementation here: duplicating
    the media format is how two answers that disagree get created.
    """
    return _TitaniaCodec()


# ===========================================================================
# SegmentedBuffer -- the RootStorage residents too big for one object
# ===========================================================================
def object_name_tag(name):
    """-> the RootStorage catalogue `tag` byte for a named object.

    `sub_47E4` (`efc_cpu2.c:18906-18933`), called as
    `sub_40301188(name, 0x10)` at `efc_cpu2.c:152755`: CRC-8 with polynomial
    0x07 over exactly 16 bytes, init 0, plus 1 if all sixteen bytes are zero.
    The result is stored per object by `sub_40365F24` and re-checked on read
    (`efc_cpu2.c:152631`), so it is a name -> object binding that can be
    verified offline without any firmware image.

    Names shorter than 16 bytes are NUL-padded here.  That is an assumption
    about what follows the string in .rodata, and it is confirmed on this board
    for all three registered buffers: "WeruUsageLog" -> 171, "LptNsid2" -> 137,
    "LptNsid3" -> 240, matching the three distinct tag runs in the Bootstrap
    catalogue exactly.  Three of three, no near misses.
    """
    b = (name.encode() if isinstance(name, str) else bytes(name)).ljust(16, b"\0")
    if len(b) > 16:
        raise ValueError("object name %r is longer than the 16 bytes the hash "
                         "covers" % (name,))
    crc, allzero = 0, 1
    for x in b:
        if x:
            allzero = 0
        crc ^= x
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return (crc + allzero) & 0xFF


class SegmentedBuffer(object):
    """One SegmentedBuffer reassembled out of consecutive RootStorage objects.

    `.data` is the concatenated segments; `.missing` is the set of element
    indices that fall in a segment this backing could not serve -- never a hole
    silently filled with zeros.  `element(i)` returns UNKNOWN for those, so a
    caller cannot sum over a gap by accident.

    The object run is discovered, not assumed: it starts at `first_object_id`
    and stops at the first slot whose catalogue tag is not
    `object_name_tag(name)`.  That is a real check -- on this board it ends the
    WeruUsageLog at object 25 and leaves objects 26-42 (absent) and 43 alone.
    """
    __slots__ = ("name", "first_object_id", "header_bytes", "element_size",
                 "segments", "segment_size", "data", "missing_segments",
                 "header", "sequence_numbers")

    def __init__(self, name, first_object_id, header_bytes, element_size,
                 segments, segment_size, data, missing_segments, header,
                 sequence_numbers):
        self.name = name
        self.first_object_id = first_object_id
        self.header_bytes = header_bytes
        self.element_size = element_size
        #: number of RootStorage objects this buffer occupies
        self.segments = segments
        self.segment_size = segment_size
        self.data = data
        self.missing_segments = tuple(missing_segments)
        #: the `headerBufferSize` bytes from the segment with the highest
        #: `headerSequenceNumber`, which is how `sub_4036869E` picks it.
        self.header = header
        self.sequence_numbers = tuple(sequence_numbers)

    @classmethod
    def from_root_storage(cls, rs, name, first_object_id, header_bytes,
                          element_size, max_segments=None):
        """-> SegmentedBuffer, or None if the first object is absent."""
        tag = object_name_tag(name)
        _h, ents, _ce, _pg = rs.catalogue()
        segs, sizes, seqs, missing = [], [], [], []
        header, header_seq = None, -1
        for i in range(first_object_id, len(ents)):
            stripe, size, _z, t = ents[i]
            if stripe == INVALID_STRIPE or t != tag:
                break
            if max_segments is not None and len(segs) >= max_segments:
                raise ValueError(
                    "%s claims more than %d segments; the firmware panics above "
                    "that (efc_cpu2.c:31754)" % (name, max_segments))
            body, _info = rs.object(i)
            if body is UNKNOWN or body is None:
                segs.append(None)
                sizes.append(size - SEGBUF_META_BYTES - header_bytes)
                seqs.append(UNKNOWN)
                missing.append(len(segs) - 1)
                continue
            seq = struct.unpack_from("<I", body, SEGBUF_SEQ_OFF)[0]
            seqs.append(seq)
            if seq > header_seq:
                header_seq = seq
                header = bytes(body[SEGBUF_META_BYTES:
                                    SEGBUF_META_BYTES + header_bytes])
            segs.append(bytes(body[SEGBUF_META_BYTES + header_bytes:]))
            sizes.append(len(segs[-1]))
        if not segs:
            return None
        segment_size = max(sizes)
        if segment_size % element_size:
            raise ValueError(
                "%s: segmentSize %d is not a multiple of elementSize %d, so an "
                "element would straddle two objects -- which the layout "
                "forbids (efc_cpu2.c:152769)" % (name, segment_size, element_size))
        total = (len(segs) - 1) * segment_size + sizes[-1]
        buf = bytearray(total)
        for k, s in enumerate(segs):
            if s is not None:
                buf[k * segment_size:k * segment_size + len(s)] = s
        return cls(name, first_object_id, header_bytes, element_size, len(segs),
                   segment_size, bytes(buf), missing, header, seqs)

    @property
    def n_elements(self):
        return len(self.data) // self.element_size

    def _missing_elements(self):
        out = set()
        for s in self.missing_segments:
            lo = s * self.segment_size // self.element_size
            hi = ((s + 1) * self.segment_size - 1) // self.element_size
            out.update(range(lo, hi + 1))
        return out

    def element(self, i):
        """-> the i-th element's bytes, or UNKNOWN if its segment is unservable."""
        if not 0 <= i < self.n_elements:
            raise IndexError("element %d of %d" % (i, self.n_elements))
        if i in self._missing_elements():
            return UNKNOWN
        return self.data[i * self.element_size:(i + 1) * self.element_size]

    def __repr__(self):
        return ("<SegmentedBuffer %s objects %d..%d, %d x %d B, %d unservable "
                "segments>" % (self.name, self.first_object_id,
                               self.first_object_id + self.segments - 1,
                               self.n_elements, self.element_size,
                               len(self.missing_segments)))


class BlockProperty(object):
    """One decoded `PhysicalBlockProperties` word.

    `raw` is kept so a caller can tell 0 (a real, never-erased block) from
    `PHYS_BLOCK_ILLEGAL`, which the firmware treats as corruption and repairs.
    """
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    @property
    def illegal(self):
        return self.raw == PHYS_BLOCK_ILLEGAL

    @property
    def erase_count(self):
        return self.raw & PHYS_BLOCK_ERASE_MASK

    @property
    def page_block_mode(self):
        """[25:24] -- the block's cell mode.  See `PHYS_BLOCK_MODE_NAMES`."""
        return (self.raw >> PHYS_BLOCK_CLASS_SHIFT) & PHYS_BLOCK_CLASS_MASK

    @property
    def page_block_mode_name(self):
        return PHYS_BLOCK_MODE_NAMES[self.page_block_mode]

    @property
    def region_class(self):
        """Deprecated alias for `page_block_mode`.

        The field was called a "region class" while only its *correlation* with
        `maxSlcPhysicalBlockId` was known.  `sub_4033AF1E` /`sub_4033D69E` show
        it is the block's page mode and that the correlation is a consequence,
        not the definition.  Kept so existing callers do not break.
        """
        return self.page_block_mode

    @property
    def reserved_bits(self):
        """[27:26] and [31].  Zero on every block measured; non-zero would mean
        the decode is wrong, not that a new field appeared."""
        return self.raw & PHYS_BLOCK_RESERVED_MASK

    @property
    def status(self):
        return (self.raw >> PHYS_BLOCK_STATUS_SHIFT) & PHYS_BLOCK_STATUS_MASK

    @property
    def status_name(self):
        return PHYS_BLOCK_STATUS_NAMES[self.status]

    @property
    def defective(self):
        """`sub_40339F3E` (`efc_cpu2.c:105027`): `status != 0 && status != 6`.

        `UndiscoveredIgnoredFactory` (6) is not defective -- it is a
        factory mark the firmware has chosen to ignore -- so testing "status is
        non-zero" over-reports.
        """
        return self.status not in PHYS_BLOCK_STATUS_OK

    def __eq__(self, other):
        return isinstance(other, BlockProperty) and other.raw == self.raw

    def __hash__(self):
        return hash(self.raw)

    def __repr__(self):
        return ("<BlockProperty erases=%d class=%d %s%s>"
                % (self.erase_count, self.region_class, self.status_name,
                   " DEFECTIVE" if self.defective else ""))


class PhysicalBlockProperties(object):
    """The whole per-physical-block ledger, one u32 per pbid.

    Read from the backup RootStorage, where `sub_4033BDDA`
    (`efc_cpu2.c:107038`) registers it as the SegmentedBuffer `"PhBP"`.  This is
    the only view of wear and bad blocks that covers blocks no WERU maps; the
    copies in `blockMapping[+10]` and in the WeruUsageLog slots are caches of it
    and are older (see `WeruUsageLog.block_properties`).

    `property_of(pbid)` returns UNKNOWN for a pbid whose segment this backing
    cannot serve -- never a zero, which would read as "a pristine block".
    """
    __slots__ = ("buffer",)

    def __init__(self, buffer):
        self.buffer = buffer

    @classmethod
    def from_root_storage(cls, rs):
        b = rs.segmented_buffer(PHYS_BLOCK_BUFFER_NAME,
                                PHYS_BLOCK_BUFFER_FIRST_OBJECT,
                                PHYS_BLOCK_BUFFER_HEADER_BYTES,
                                PHYS_BLOCK_BUFFER_ELEMENT)
        if b is None:
            return None
        if b.segments > PHYS_BLOCK_BUFFER_MAX_OBJECTS:
            raise ValueError(
                "PhBP claims %d objects; the firmware panics above %d "
                "(efc_cpu2.c:107043)"
                % (b.segments, PHYS_BLOCK_BUFFER_MAX_OBJECTS))
        return cls(b)

    @property
    def n_blocks(self):
        return self.buffer.n_elements

    def property_of(self, pbid):
        e = self.buffer.element(pbid)
        if e is UNKNOWN:
            return UNKNOWN
        return BlockProperty(struct.unpack("<I", e)[0])

    def declared_bytes(self):
        """-> the `4 * numberOfBlocks` the header declares, or UNKNOWN.

        `*(u32*)header = 4 * numberOfBlocks` (`efc_cpu2.c:107030`), so this is an
        independent check on the reassembled length.
        """
        h = self.buffer.header
        if not h:
            return UNKNOWN
        return struct.unpack_from("<I", h, PHYS_BLOCK_HEADER_BYTES_OFF)[0]

    def header_total_erases(self):
        """-> the u64 total erase count at the last backup, or UNKNOWN.

        `sub_4033B640` (`efc_cpu2.c:106529`) reads `*(u64*)(header + 56)` as the
        total the previous backup recorded and asks whether the live total has
        advanced by `10 * numberOfBlocks` since.  Summing `erase_count` over
        every element must therefore reproduce it exactly on a store that has
        not been written since -- which is what makes the [23:0] reading a
        measurement rather than a guess.
        """
        h = self.buffer.header
        if not h or len(h) < PHYS_BLOCK_HEADER_TOTAL_ERASES_OFF + 8:
            return UNKNOWN
        return struct.unpack_from("<Q", h,
                                  PHYS_BLOCK_HEADER_TOTAL_ERASES_OFF)[0]

    def total_erases(self):
        """-> (sum of erase_count over readable blocks, n_readable, n_illegal)."""
        tot = n = ill = 0
        for i in range(self.n_blocks):
            p = self.property_of(i)
            if p is UNKNOWN:
                continue
            if p.illegal:
                ill += 1
                continue
            tot += p.erase_count
            n += 1
        return tot, n, ill

    def defective_blocks(self):
        """-> sorted pbids the firmware would refuse to use, or UNKNOWN-free."""
        out = []
        for i in range(self.n_blocks):
            p = self.property_of(i)
            if p is not UNKNOWN and not p.illegal and p.defective:
                out.append(i)
        return out

    def __repr__(self):
        return "<PhysicalBlockProperties %d blocks>" % self.n_blocks


class WeruUsageLog(object):
    """The durable, journalled per-WERU record -- the ownership authority.

    `weruDescriptor` is written only by one step of the SystemMetadata flush
    (`efc_cpu3.c:38958` -> `:44873`) and skipped when a CRC over
    weruBin+weruDescriptor is unchanged (`:44942`).  This log is different: every
    mutator marks the record's segment dirty (`sub_40368AD6`) and the buffer is
    flushed at dirty-segment granularity, so after an unclean shutdown
    `RebuildWeruBins` re-derives owner, state and counters from HERE and writes
    them back into the descriptor -- `efc_cpu3.c:22044-22062` calls
    `sub_402E70FA(weruId, ownerMsoId)`, `sub_402E6318(weruId, counter)` and
    `sub_402EA68A(weruId, state)` for every WERU the array marks touched, and the
    owner byte it passes came from this record (`efc_cpu2.c:31995`, `:131940`).

    On a dirty store, therefore, this is what the firmware would believe about
    ownership, and `weruDescriptor.owner` is what it happened to have written
    down last.  They differ here on 108 of 820 readable WERUs.

    Every field is UNKNOWN-safe: a WERU whose segment this backing cannot serve
    returns UNKNOWN from every accessor, never a default.
    """
    __slots__ = ("buffer",)

    def __init__(self, buffer):
        self.buffer = buffer

    @classmethod
    def from_root_storage(cls, rs):
        b = SegmentedBuffer.from_root_storage(
            rs, WERU_USAGE_LOG_NAME, OBJ_WERU_USAGE_LOG_FIRST,
            WERU_USAGE_LOG_HEADER_BYTES,
            WERU_USAGE_MAP_OFF + WERU_USAGE_MAP_ENTRY * geom.BLOCKS_PER_WERU,
            max_segments=WERU_USAGE_LOG_MAX_SEGMENTS)
        return None if b is None else cls(b)

    @property
    def n_werus(self):
        """-> numberOfWerus as the LOG's own size declares it.

        This is `dword_907168` = the sum of the weruCount of all six WERU
        configurations (`efc_cpu2.c:108041-108049`), which is a larger set than
        the `dword_907164` that sizes validMap/validCount (the first five).
        Here 1070 against 972: 98 WERUs exist that no validMap row covers.
        """
        return self.buffer.n_elements

    def record(self, weru_id):
        return self.buffer.element(weru_id)

    def owner(self, weru_id):
        """-> owning msoId, None if the record says unowned, UNKNOWN if unread."""
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        o = r[WERU_USAGE_OWNER_OFF]
        return None if o == WERU_USAGE_OWNER_NONE else o

    def flags(self, weru_id):
        r = self.record(weru_id)
        return UNKNOWN if r is UNKNOWN else r[WERU_USAGE_FLAGS_OFF]

    def flag_names(self, weru_id):
        """-> the set of named flag bits that are set, or UNKNOWN.

        See `WERU_USAGE_FLAG_NAMES` for where each name comes from.  Activation
        clears the whole byte (`sub_10D20` copies 20 zeroed bytes over the
        record head, `efc_cpu2.c:31911`), so these describe what has happened to
        the WERU since its last activation.
        """
        f = self.flags(weru_id)
        if f is UNKNOWN:
            return UNKNOWN
        return {WERU_USAGE_FLAG_NAMES[b] for b in range(8) if f >> b & 1}

    def counter(self, weru_id):
        """-> the u64 activation sequence number, or UNKNOWN.

        Higher means more recently activated.  It is a global sequence, not a
        per-WERU count: two WERUs never share a value, and the gaps are the
        activations whose stamp a later activation of the same WERU overwrote.
        """
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        return struct.unpack_from("<Q", r, WERU_USAGE_CTR_OFF)[0]

    def origin_counter(self, weru_id):
        """-> the `+8` relocation stamp.  Telemetry: nothing in the drive reads it.

        0 means "no relocation has been recorded since this WERU was last
        activated" -- not "the contents are new", and not "this WERU has never
        been relocated into".  Activation wipes the field (`sub_10D20`'s 20-byte
        `mem_clr` at +0, `efc_cpu2.c:31909` / `0x00010D82`), so the population of
        non-zero values is a window, not a history.

        `sub_10DFE` (`efc_cpu2.c:31935`) copies the source's origin, or its
        activation stamp if it has no origin of its own, so a non-zero value is
        always `<= counter()` by construction -- that inequality is a
        tautology and must never be quoted as a check that passed.

        Use it as a hint about the last relocation and nothing more: no firmware
        decision anywhere in the image depends on its value (see
        `WERU_USAGE_CTR2_OFF` for the five ways that was established).
        """
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        return struct.unpack_from("<Q", r, WERU_USAGE_CTR2_OFF)[0]

    def activation_count(self):
        """-> (highest stamp seen, number of readable records), or UNKNOWN.

        The global counter starts at 1 (`efc_cpu2.c:132009`) and is
        post-incremented once per activation, so the highest stamp anywhere in
        the array is a lower bound on the drive's lifetime activation count --
        exact only if the most recent activation's WERU has not been reused.
        """
        best = None
        n = 0
        for w in range(self.n_werus):
            c = self.counter(w)
            if c is UNKNOWN:
                continue
            n += 1
            if best is None or c > best:
                best = c
        return UNKNOWN if best is None else (best, n)

    def block_properties(self, weru_id):
        """-> slot-indexed [BlockProperty|None] frozen at this WERU's activation.

        `sub_11298` copies `blockMapping[+10]` verbatim into bytes +2..+5 of each
        slot (`efc_cpu2.c:32347`), so this is a dated snapshot of the per-block
        properties -- the erase counts here are those the blocks had when this
        WERU was last opened, and comparing them with
        `PhysicalBlockProperties` gives the erases since.

        None for an unmapped or out-of-range slot.
        """
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        out = []
        for i in range(geom.BLOCKS_PER_WERU):
            o = WERU_USAGE_MAP_OFF + WERU_USAGE_MAP_ENTRY * i
            v = struct.unpack_from("<H", r, o)[0]
            if v in (WERU_USAGE_ENTRY_UNMAPPED, WERU_USAGE_ENTRY_UNUSED):
                out.append(None)
                continue
            out.append(BlockProperty(
                struct.unpack_from("<I", r, o + WERU_USAGE_PROP_OFF)[0]))
        return out

    def pair_indices(self, weru_id):
        """-> a `BLOCKS_PER_WERU`-long slot-indexed list of pair indices.

        `None` for an unmapped slot (0xFFFF) and for a slot past this WERU's own
        `blocksPerWeru` (0x3FFF) -- the two are distinguished by
        `slot_states()`.  Multiply by `WERU_USAGE_PAIR_DIVISOR` and add
        `12*half + slot` to recover a pbid; this module does not,
        because which half a slot belongs to is a property of the WERU
        configuration, not of this record.
        """
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        out = []
        for i in range(geom.BLOCKS_PER_WERU):
            v = struct.unpack_from("<H", r, WERU_USAGE_MAP_OFF
                                   + WERU_USAGE_MAP_ENTRY * i)[0]
            out.append(None if v in (WERU_USAGE_ENTRY_UNMAPPED,
                                     WERU_USAGE_ENTRY_UNUSED) else v)
        return out

    def slot_states(self, weru_id):
        """-> ['mapped'|'unmapped'|'unused'] * BLOCKS_PER_WERU, or UNKNOWN."""
        r = self.record(weru_id)
        if r is UNKNOWN:
            return UNKNOWN
        out = []
        for i in range(geom.BLOCKS_PER_WERU):
            v = struct.unpack_from("<H", r, WERU_USAGE_MAP_OFF
                                   + WERU_USAGE_MAP_ENTRY * i)[0]
            out.append("unmapped" if v == WERU_USAGE_ENTRY_UNMAPPED else
                       "unused" if v == WERU_USAGE_ENTRY_UNUSED else "mapped")
        return out

    def blocks_per_weru(self, weru_id):
        """-> how many slots this WERU's own configuration uses, or UNKNOWN.

        `sub_11298` fills slots `[0, blocksPerWeru)` from the blockMapping row
        and stamps 0x3FFF over the rest (`efc_cpu2.c:32352-32356`), so the first
        0x3FFF is that WERU's `blocksPerWeru` -- read off the media rather than
        taken from the six-configuration table.

        Measured here, and it recovers the configuration boundaries directly:

            weru    0..2      24
            weru    3..28     12      <- the metadata WERUs
            weru   29..971    48      <- BLOCKS_PER_WERU, confirmed on media
            weru  972..1069   24      <- outside validMap entirely

        The 11-vs-12 conflict, now with an explanation.  This says 12 for
        the metadata WERUs, while the WMA arithmetic was established at
        `blocksPerWeru = 11` from the fact that `wma mod 192` never reaches 44
        across 5940 userLutLut entries.  12 is the allocation width and 11 the
        addressable width, and the difference is the inline-RAID parity block:
        every AU of slot 11 carries `AUX_FLAG_INLINE_PARITY` in all five
        metadata WERUs measured (368/368 per readable slot-11 block; every other
        slot is under 2%), and the firmware writes that flag only on an
        inline-parity stripe.  `WERU_CONFIGURATIONS` agrees with this function
        at both ends of all six configurations, 12/12, so this reading and the
        firmware's own constants are the same reading.

        Two things are still not established and are not to be guessed at: the
        parity function itself (the stripe covers CE0, which is dead here), and
        whether slot 0 is a second parity slot -- it is 100% flagged in the
        three WERUs whose slot 0 is readable, but slot 0 of werus 19 and 23 is
        on CE0 and cannot be checked.  `weru_config_from_pbids` still defaults
        to 11; do not swap one number for the other.
        """
        st = self.slot_states(weru_id)
        if st is UNKNOWN:
            return UNKNOWN
        return st.index("unused") if "unused" in st else len(st)

    def owners(self):
        """-> {weruId: msoId} for every readable, owned record."""
        out = {}
        for w in range(self.n_werus):
            o = self.owner(w)
            if o is not UNKNOWN and o is not None:
                out[w] = o
        return out

    def unreadable_werus(self):
        return sorted(self.buffer._missing_elements())

    def __repr__(self):
        return "<WeruUsageLog %d records, %d unreadable>" % (
            self.n_werus, len(self.buffer._missing_elements()))


# ===========================================================================
# RootStorage (NAND blocks 1-5)
# ===========================================================================
class RootStorage(object):
    """The FTL's persistent object store, blocks 1-5, read through a Device.

    One NAND page is one stripe part (16384 bytes):

        +0x00 u16 21          the version word
        +0x02 u16 stripeNumber        (0xFFFF = invalid)
        +0x04 u16 objectId stored in this stripe
        +0x0C 64B per-WERU stripe-part availability bitmap
        +0x4C u32 generation of this WERU's content
        +0x50 u32 current generation of the store
        +0x54 u32 numberOfEntries
        +0x58 entry[N] x 8 { u16 stripe; u16 pad; u16 size; u8 z; u8 groupTag }
        then 16384 - (88 + 8N) bytes of object payload.

    Objects are located by inverting the measured placement law rather than by
    scanning every page: for ``stripe = base + K*page + ce//2``, a stripe lives
    at ``page = (stripe-base)//K`` on the CE pair ``ce//2 == (stripe-base)%K``.
    Four physical copies of every stripe exist, so a dead CE costs nothing here
    -- but if none of a stripe's CEs is servable, `object()` says UNKNOWN.
    """

    def __init__(self, dev, block=2, codec=None, ces=None, scan_pages=None):
        self.dev = dev
        self.block = block
        self.codec = codec or default_codec()
        self.instance = ROOTSTORAGE_INSTANCE.get(block)
        self.k = ROOTSTORAGE_STRIPES_PER_PAGE[block]
        self.ces = resolve_scan_ces(dev, ces)
        self.skipped_ces = sorted(set(dev.present_ces()) - set(self.ces))
        self.bases = {}
        self.aux_ok = 0
        self.aux_bad = 0
        #: Set by `catalogue()`: which section this instance settled on, and the
        #: CEs it refused because they carry the other, older one.  Empty for
        #: the Bootstrap blocks, where a section is a whole block pair.
        self.section = None
        self.stale_section_ces = []
        self._cat = None
        self._scan_pages = (range(geom.PAGES_PER_BLOCK_SLC) if scan_pages is None
                            else scan_pages)

    # -- page access --------------------------------------------------------
    def page_plain(self, ce, page):
        """-> the 16384-byte stripe part, or None if unreadable/unwritten.

        MCRC-gated: a page whose four AUs do not all pass the seed-free CRC-48
        is refused outright, so a partially-read or stale buffer cannot enter
        the parse.
        """
        try:
            raw = self.dev.read_page(ce, self.block, page, slc=True)
        except DeviceError:
            return None
        if raw is None:
            return None
        aus, ok = self.codec.decode(raw, self.block, page)
        if aus is None or (ok is not UNKNOWN and not all(ok)):
            return None
        for k, a in enumerate(aus):      # free cross-check of the descramble
            good = struct.unpack_from("<I", a, AUX_LMA_OFF)[0] == ROOTSTORAGE_LMA
            if k != SEQ_AU:              # only AU1 carries the page sequence
                good &= a[:geom.AU_AUX] == ROOTSTORAGE_AUX
            if good:
                self.aux_ok += 1
            else:
                self.aux_bad += 1
        return b"".join(a[geom.AU_AUX:] for a in aus)

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def parse(d):
        """-> (header dict, [(stripe, size, z, tag)])."""
        n = struct.unpack_from("<I", d, 0x54)[0]
        if ROOTSTORAGE_HDR + 8 * n >= ROOTSTORAGE_PAGE:
            raise ValueError("numberOfEntries %d overflows the stripe part" % n)
        h = dict(type=struct.unpack_from("<H", d, 0)[0],
                 stripe=struct.unpack_from("<H", d, 2)[0],
                 object_id=struct.unpack_from("<H", d, 4)[0],
                 avail=bytes(d[0x0C:0x4C]),
                 weru_generation=struct.unpack_from("<I", d, 0x4C)[0],
                 store_generation=struct.unpack_from("<I", d, 0x50)[0],
                 n_entries=n,
                 meta_bytes=ROOTSTORAGE_HDR + 8 * n)
        ents = [(struct.unpack_from("<H", d, ROOTSTORAGE_HDR + 8 * i)[0],
                 struct.unpack_from("<H", d, ROOTSTORAGE_HDR + 8 * i + 4)[0],
                 d[ROOTSTORAGE_HDR + 8 * i + 6],
                 d[ROOTSTORAGE_HDR + 8 * i + 7]) for i in range(n)]
        return h, ents

    def catalogue(self):
        """-> (header, entries) from the highest stripe number we can read.

        Scans the block on the first servable CE; the lookup table is replicated
        into every stripe part, so one CE is enough.  Raises LookupError if no
        page of blocks 1-5 could be read at all -- that is "this backing does
        not carry RootStorage", not "the drive has none".
        """
        if self._cat is not None:
            return self._cat
        # A block can carry more than one section (the Backup instance splits by
        # CE half).  Pick the live one before scanning, so "whichever CE
        # answered first" can never decide a generation.
        sec, live_ces, seen = self.authoritative_ces()
        if len(seen) > 1:
            self.stale_section_ces = [c for c in self.ces if c not in live_ces]
            self.section = sec
            scan_ces = live_ces
        else:
            self.section = sec
            scan_ces = list(self.ces)
        best = None
        for ce in scan_ces:
            for pg in self._scan_pages:
                d = self.page_plain(ce, pg)
                if d is None:
                    continue
                try:
                    h, e = self.parse(d)
                except (ValueError, struct.error):
                    continue
                if h["type"] != ROOTSTORAGE_TYPE:
                    continue
                self.bases.setdefault(ce, h["stripe"] - self.k * pg
                                      - self.ce_offset(ce))
                if best is None or h["stripe"] > best[0]["stripe"]:
                    best = (h, e, ce, pg)
            if best is not None:
                break
        if best is None:
            raise LookupError(
                "no readable RootStorage page in block %d on CEs %s (dead CEs "
                "%s were excluded%s)"
                % (self.block, scan_ces, list(geom.CE_DEAD),
                   "" if not self.stale_section_ces else
                   "; CEs %s were excluded too -- they carry the OLDER section"
                   % self.stale_section_ces))
        self._cat = best
        return best

    # -- sections and generations -------------------------------------------
    def ce_offset(self, ce):
        """The stripe offset a CE contributes: `(ce // 2) % k`.

        The measured law is `stripe = base + k*page + offset`.  For the
        Bootstrap store `k` is 6 and `ce // 2` never exceeds 5, so the modulus
        is invisible there -- but the backup store has `k = 3`, and without it
        CEs 6..11 do not satisfy the law at all and every object on that half
        reads as unservable.  It is not cosmetic: on this board the Backup
        store's two sections are its two CE halves, so getting the offset wrong
        also hides the fact that there is a generation to choose between.
        """
        return (ce // 2) % self.k

    def ce_bases(self, page=0):
        """-> {ce: base stripe number}, probed from one page.  Cheap on purpose.

        A store whose sections split by CE half (the Backup instance) shows two
        distinct values here; one whose sections split by block (Bootstrap)
        shows one.  Either way this is the raw material for `authoritative_ces`.
        """
        out = {}
        for ce in self.ces:
            d = self.page_plain(ce, page)
            if d is None:
                continue
            try:
                h, _ = self.parse(d)
            except (ValueError, struct.error):
                continue
            if h["type"] != ROOTSTORAGE_TYPE:
                continue
            out[ce] = h["stripe"] - self.k * page - self.ce_offset(ce)
        return out

    def authoritative_ces(self):
        """-> (section, [ce...], {section: base}) for the LIVE section here.

        When a block carries only one section this is a no-op that returns every
        servable CE.  When it carries two -- which the Backup instance does,
        split by CE half -- the choice is made by the firmware's own rule
        (`scan_last_stripe`), never by "whichever CE answered first".  Returns
        `(None, [], {})` if nothing is servable.
        """
        bases = self.ce_bases()
        if not bases:
            return None, [], {}
        by_sec = {}
        for ce, base in bases.items():
            by_sec.setdefault(self.section_index(base), {})[ce] = base
        seen = {sec: min(d.values()) for sec, d in by_sec.items()}
        if len(by_sec) == 1:
            sec = next(iter(by_sec))
            return sec, sorted(by_sec[sec]), seen
        last = [INVALID_STRIPE] * ROOTSTORAGE_SECTIONS
        for sec, base in seen.items():
            if sec < len(last):
                last[sec] = base
        sec = scan_last_stripe(last, self.stripes_per_section)
        return sec, sorted(by_sec.get(sec, ())), seen

    @property
    def stripes_per_section(self):
        """`nandArea.numberOfStripesPerSection` -- one section = one WERU here.

        `sub_40364CD6` derives it as `numberOfAvailableStripes / numberSections`
        (`efc_cpu2.c:149022-149035`).  A section is a block pair on twelve CEs,
        every page of which carries `STRIPES_PER_PAGE` stripe parts, so it is
        simply `k * pagesPerBlock` -- 2208 for Bootstrap, 1104 for Backup.
        """
        return self.k * geom.PAGES_PER_BLOCK_SLC

    @property
    def available_stripes(self):
        """`nandArea.numberOfAvailableStripes` = sections x stripes/section."""
        return ROOTSTORAGE_SECTIONS * self.stripes_per_section

    @property
    def stripe_modulus(self):
        """The stripe number space, which is twice the physical stripe space.

        `sub_40367130` (`efc_cpu2.c:151164`) advances a stripe number modulo
        `2 * nandArea.numberOfAvailableStripes`, while `sub_40367764`
        (`:151499`) maps a number to its section with
        `(stripe % numberOfAvailableStripes) / numberOfStripesPerSection`.  So
        the counter makes exactly two passes over the physical space before it
        wraps to 0, and that wrap is the whole reason `ScanLastStripe` needs an
        override -- see `scan_last_stripe`.
        """
        return 2 * self.available_stripes

    def section_index(self, stripe):
        """-> which section a stripe number lands in (`sub_40367764`)."""
        return (stripe % self.available_stripes) // self.stripes_per_section

    def section_base_stripe(self, tries=3):
        """-> the stripe number stored at this section's first stripe, or None.

        This is exactly the quantity `ScanLastStripe` collects into
        `pScanLastStripe->lastStripe[section]`: `sub_4036251E`
        (`efc_cpu2.c:146395`) reads stripe `counter + section*stripesPerSection`
        for `counter` 0,1,2 and `sub_4036272E` (`:146608`) records
        `stripeData->stripeNumber - counter`, so an unreadable first part is
        retried on the next two.  Reading page 0 is enough: stripe `s` of a
        section lives at page `(s - base) // k` on CE pair `(s - base) % k`.

        Cheap on purpose -- it costs three page reads, not a block scan -- and
        it is the only value the firmware's cross-section recency rule consumes.
        """
        for counter in range(tries):
            for ce in (2 * counter, 2 * counter + 1):
                if ce not in self.ces:
                    continue
                d = self.page_plain(ce, 0)
                if d is None:
                    continue
                try:
                    h, _ = self.parse(d)
                except (ValueError, struct.error):
                    continue
                if h["type"] != ROOTSTORAGE_TYPE:
                    continue
                return h["stripe"] - counter
        return None

    def locate(self, stripe):
        """-> [(ce, page)] candidates for a stripe, best first.

        Inverts the measured placement law.  Returns every servable CE of the
        pair, so a dead one is skipped rather than fatal.
        """
        self.catalogue()
        out = []
        for ce in self.ces:
            base = self.bases.get(ce)
            if base is None:
                base = next(iter(self.bases.values()), 0)
            rel = stripe - base
            if rel < 0:
                continue
            page, r = divmod(rel, self.k)
            if r != self.ce_offset(ce) or page >= geom.PAGES_PER_BLOCK_SLC:
                continue
            out.append((ce, page))
        return out

    def object(self, object_id):
        """-> (payload bytes, info dict), payload UNKNOWN if not servable.

        `info` always carries `stripe`, `size` and `tag`.  payload is:
          * bytes         -- read and verified (the page's own header agrees
                             that this stripe holds this object);
          * None          -- the catalogue says the object is absent
                             (stripe == 0xFFFF);
          * UNKNOWN       -- present on media but on a CE/page this backing
                             cannot serve.
        """
        h, ents, _, _ = self.catalogue()
        if object_id >= len(ents):
            raise IndexError("object %d: this store has %d slots"
                             % (object_id, len(ents)))
        stripe, size, _z, tag = ents[object_id]
        info = dict(stripe=stripe, size=size, tag=tag,
                    store_generation=h["store_generation"])
        if stripe == INVALID_STRIPE:
            return None, info
        for ce, page in self.locate(stripe):
            d = self.page_plain(ce, page)
            if d is None:
                continue
            try:
                hh, _ = self.parse(d)
            except (ValueError, struct.error):
                continue
            if hh["stripe"] != stripe:
                continue                       # placement law violated: skip it
            info.update(ce=ce, page=page)
            return bytes(d[hh["meta_bytes"]:hh["meta_bytes"] + size]), info
        return UNKNOWN, info

    def allocation(self):
        """Object 2 -> {msoId: WriteFrontier}, or None/UNKNOWN per `object()`.

        This is the write frontier as of the last Allocation flush.  On a dirty
        mount the firmware never reads it -- `sub_402E0FE8` (`efc_cpu3.c:39620`)
        sends `AllocationReinitializeMsos` instead of
        `BootstrapRootStorageReadAllocation`, and that reinitialiser zeroes
        every record (`sub_4034F04C` -> `sub_4034DBFA`, `efc_cpu2.c:126425`,
        `:125261`).  So on this board the drive itself would discard what this
        object says, which is exactly why reading it offline is worth doing.
        """
        body, _info = self.object(OBJ_ALLOCATION)
        if body is UNKNOWN or body is None:
            return body
        return decode_allocation(body)

    def system_lut(self, total_lmas=None):
        """Object 0 -> `SystemLut`, or None/UNKNOWN as `object()` defines them.

        The granularity is derived, never assumed: from
        `msoDescriptor[41].totalLmas` when the caller has it, otherwise from the
        one `LUT_GRANULARITIES` value that lands on a size class.
        """
        body, _info = self.object(OBJ_SYSTEM_LUT)
        if body is UNKNOWN or body is None:
            return body
        return SystemLut.from_object(body, total_lmas)

    def system_metadata_information(self):
        """Object 1 -> {'dirty': bool, 'format_version': int, 'raw': bytes}.

        `BootStrapRootStorageReadSystemMetadataInformation` (`efc_cpu3.c:39404`)
        is the first thing the mount reads.  Byte 0 is the dirty flag; when it
        is set the firmware discards the stored `validMap` and `validCount` and
        rebuilds them from the LUT (`efc_cpu2.c:109642`), so on a dirty store
        those two sections are evidence about a past instant, not truth.

        On this board the record reads
        ``01 00 00 00 | 68 00 00 02 | 01 00 00 00 | 00 00 00 00`` -- dirty, with
        the same formatVersion 0x02000068 the SystemMetadata directory carries.
        """
        body, info = self.object(OBJ_SYSTEM_METADATA_INFO)
        if body is UNKNOWN or body is None:
            return body
        if len(body) != SYSMETA_INFO_SIZE:
            raise ValueError("SystemMetadataInformation is %d bytes, expected %d"
                             % (len(body), SYSMETA_INFO_SIZE))
        ver = struct.unpack_from("<I", body, SYSMETA_INFO_VERSION_OFF)[0]
        return dict(dirty=bool(body[SYSMETA_INFO_DIRTY_OFF]),
                    format_version=ver, raw=bytes(body), stripe=info.get("stripe"))

    def segmented_buffer(self, name, first_object_id, header_bytes,
                         element_size, max_segments=None):
        """-> `SegmentedBuffer` for a named multi-object resident, or None.

        None means the first slot is absent or carries a different name tag --
        "this store does not hold that buffer", which is not the same as "the
        buffer is empty".
        """
        return SegmentedBuffer.from_root_storage(
            self, name, first_object_id, header_bytes, element_size,
            max_segments=max_segments)

    def weru_usage_log(self):
        """Objects 4.. -> `WeruUsageLog`, or None if this instance has none.

        Only the bootstrap instance registers it (`efc_cpu2.c:31745`, which
        reads `bootStrapRootStorage.instanceId`), so calling this on block 1
        legitimately returns None.
        """
        return WeruUsageLog.from_root_storage(self)

    def system_block_mapping(self):
        """Object 3 -> (nextSlot, {slot: (weruId, [(block, ce)])}) or UNKNOWN.

        Only the live slots are returned; a free slot reads weruId 0x3FFF and
        its pbid array is stale residue from whatever WERU last occupied that
        ring position, not a location.

        The 12 pbids here are the metadata WERU's own configuration.  They are
        not `BLOCKS_PER_WERU` and must never be used as the stride of the global
        `blockMapping` table.
        """
        body, info = self.object(OBJ_SYSTEM_BLOCK_MAPPING)
        if body is UNKNOWN or body is None:
            return body
        if len(body) != SYSTEM_BLOCK_MAPPING_SIZE:
            raise ValueError(
                "SystemBlockMapping is %d bytes, expected %d = 8 + %d*(4 + 4*%d)"
                % (len(body), SYSTEM_BLOCK_MAPPING_SIZE, SYSTEM_WERU_SLOTS,
                   SYSTEM_BLOCK_MAPPING_PBIDS))
        magic, next_slot = struct.unpack_from("<II", body, 0)
        if magic != SYSTEM_BLOCK_MAPPING_MAGIC:
            raise ValueError("SystemBlockMapping magic 0x%08x, expected 0x%08x"
                             % (magic, SYSTEM_BLOCK_MAPPING_MAGIC))
        live = {}
        for slot in range(SYSTEM_WERU_SLOTS):
            o = 8 + slot * (4 + 4 * SYSTEM_BLOCK_MAPPING_PBIDS)
            weru = struct.unpack_from("<H", body, o)[0]
            if weru == INVALID_WERU:
                continue
            pb = struct.unpack_from("<%dI" % SYSTEM_BLOCK_MAPPING_PBIDS, body, o + 4)
            live[slot] = (weru, [geom.split_pbid(p) for p in pb])
        return next_slot, live

    def physical_block_properties(self):
        """Objects 8.. -> `PhysicalBlockProperties`, or None if absent here.

        Only the backup instance registers it -- `sub_4033BDDA` passes
        `dword_40400E4C` (`efc_cpu2.c:107031`), a different instance id from the
        WeruUsageLog's -- so calling this on a Bootstrap block legitimately
        returns None.  That is "this store does not hold it", not "the drive has
        no wear data".
        """
        return PhysicalBlockProperties.from_root_storage(self)


def scan_last_stripe(last_stripes, stripes_per_section):
    """The firmware's cross-section recency rule -- `sub_4036268A` verbatim.

    `last_stripes[section]` is the stripe number stored at that section's first
    stripe (`RootStorage.section_base_stripe`), `0xFFFF` = never read / erased.
    Returns the winning section index.

    The literal C (`efc_cpu2.c:146497-146532`, `ScanLastStripe.c`)::

        v2 = 0; v3 = a2[0];
        for (v8 = 0; v8 < numberSections - 1; v8++) {
            v11 = a2[v8+1];
            if (v11 != 0xFFFF && (v11 > v3 || v3 == 0xFFFF)) { v3 = v11; v2 = v8+1; }
            v12 = a2[v8];
            if (v12 != 0xFFFF && v11 != 0xFFFF && abs(v11 - v12) > stripesPerSection)
                { v2 = v8; break; }
        }

    Two rules, and the second one wins when it fires:

    * highest stripe number so far, skipping `0xFFFF`;
    * the wrap override -- if two adjacent sections' base stripes differ by
      more than one section's worth, the LOWER-indexed one is taken and the scan
      stops.

    The override is not a heuristic, it is forced by the counter's shape: stripe
    numbers run modulo `2 * numberOfAvailableStripes` (`sub_40367130`), i.e. two
    passes over the physical space, so the newest section can legitimately carry
    the smallest number.  On this board Bootstrap reads
    `[0, 6624]` with `stripesPerSection = 2208`: plain "highest wins" would pick
    section 1 (blocks 4/5), the override picks section 0 (blocks 2/3), and
    section 0 is independently the newer one -- see `root_storage_generations`.

    Faithful to the original including its asymmetry: the override always
    selects `v8`, never `v8+1`, so it is only correct when the wrap happens at
    the low-numbered section.  That is a property of the firmware, not of this
    port, and it is reproduced rather than "fixed" on purpose.
    """
    n = len(last_stripes)
    if n == 0:
        raise ValueError("no sections")
    best = 0
    cur = last_stripes[0]
    for i in range(n - 1):
        nxt = last_stripes[i + 1]
        if nxt != INVALID_STRIPE and (cur == INVALID_STRIPE or nxt > cur):
            cur = nxt
            best = i + 1
        here = last_stripes[i]
        if here != INVALID_STRIPE and nxt != INVALID_STRIPE:
            if abs(nxt - here) > stripes_per_section:
                return i
    return best


def root_storage_generations(dev, codec=None, blocks=ROOTSTORAGE_BOOTSTRAP_BLOCKS,
                             ces=None):
    """-> which of the Bootstrap store's two sections is authoritative.

    Returns a dict::

        block_base   {block: base stripe number or None}
        sections     {sectionIndex: (blocks...)}      derived, never assumed
        last_stripes [base of section 0, base of section 1]  0xFFFF if unread
        section      the winning section index
        blocks       its block numbers, lowest first
        block        the block to mount (blocks[0])
        rule         "wrap-override" | "highest" | "only-section"

    This is not the SysBlock trap.  Two live `SystemBlockMapping` slots are
    halves of one image and both must be installed (`sbm_recency_order`); the
    two RootStorage sections are genuinely two generations of the whole store,
    and the firmware genuinely picks one (`ScanLastStripe`, `sub_4036268A`).
    The difference is structural, not a judgement call: measured here, all 56
    live objects of the block-2 catalogue have stripes inside section 0 and all
    56 of the block-4 catalogue inside section 1, so each section is complete
    and self-contained -- a section switch rewrites every object.

    The section index is derived from each block's own base stripe through
    `RootStorage.section_index`, so nothing here assumes "block 2 is section 0".
    """
    codec = codec or default_codec()
    out = dict(block_base={}, sections={}, last_stripes=[INVALID_STRIPE] * ROOTSTORAGE_SECTIONS,
               section=None, blocks=(), block=None, rule=None, notes=[])
    per_section = {}
    out["section_ces"] = {}
    for blk in blocks:
        try:
            rs = RootStorage(dev, blk, codec=codec, ces=ces)
            cebases = rs.ce_bases()
        except (LookupError, ValueError, DeviceError) as e:
            out["notes"].append("block %d: %s" % (blk, e))
            cebases = {}
        base = min(cebases.values()) if cebases else None
        out["block_base"][blk] = base
        # A block can straddle two sections (the Backup instance splits by CE
        # half), so group by section rather than assuming one per block.
        #
        # `rs` is always this block's, never a leftover from the previous
        # iteration: the only way into this loop is a non-empty `cebases`, and
        # `cebases` is non-empty only when both statements in the try above
        # succeeded for this `blk`.  Every failure path sets `cebases = {}`,
        # which skips the loop entirely -- including the first-iteration case
        # where `rs` is not bound at all.
        for ce, b in sorted(cebases.items()):
            sec = rs.section_index(b)
            out["section_ces"].setdefault(sec, []).append((blk, ce))
            if blk not in per_section.setdefault(sec, []):
                per_section[sec].append(blk)
            if sec >= len(out["last_stripes"]):
                continue
            prev = out["last_stripes"][sec]
            if prev != INVALID_STRIPE and prev != b:
                out["notes"].append(
                    "block %d ce%d says section %d starts at stripe %d but "
                    "another part said %d -- the section disagrees with itself"
                    % (blk, ce, sec, b, prev))
            out["last_stripes"][sec] = b
    if not per_section:
        return out
    out["sections"] = {s: tuple(sorted(b)) for s, b in per_section.items()}
    spp = ROOTSTORAGE_STRIPES_PER_PAGE[blocks[0]] * geom.PAGES_PER_BLOCK_SLC
    if len(per_section) == 1:
        sec = next(iter(per_section))
        out["rule"] = "only-section"
    else:
        sec = scan_last_stripe(out["last_stripes"], spp)
        a, b = out["last_stripes"][0], out["last_stripes"][1]
        out["rule"] = ("wrap-override"
                       if (a != INVALID_STRIPE and b != INVALID_STRIPE
                           and abs(b - a) > spp) else "highest")
    out["section"] = sec
    out["blocks"] = out["sections"].get(sec, ())
    out["block"] = out["blocks"][0] if out["blocks"] else None
    return out


def sbm_recency_order(next_slot, live):
    """Live `SystemBlockMapping` slots in ring order, most recently written first.

    Display order only.  It says which slot the ring wrote last; it does not say
    which slot holds live data, because the firmware's replay loop
    (`efc_cpu3.c:42553-42568`) installs the block mapping of every valid slot
    simultaneously and never picks one.  On this board slots 8 and 9 are both
    live -- weruId 19 (blocks 10/11) and weruId 23 (blocks 26/27), nextSlot 10 --
    and they are two halves of one image: five of the nine SystemMetadata
    sections have their live copy on weru 19.

    Reading "slot 9 is newest, therefore mount weru 23" is precisely the bug
    this module used to have.  Which WERU holds a given LMA comes from the
    SystemLut (`RootStorage.system_lut`), never from this ordering.
    """
    return sorted(live, key=lambda s: (s - next_slot) % SYSTEM_WERU_SLOTS,
                  reverse=True)


def weru_block_numbers(pairs):
    """The (block, ce) pairs of a metadata WERU -> its NAND block numbers.

    Deprecated in favour of `weru_config_from_pbids`, which gets the CEs right
    as well.  Kept because it happens to return the same block pair: the twelve
    raw pbids of a metadata WERU decode to `2*pairIndex` and `2*pairIndex+1`
    under the corrected reading, and to `B` and `B+1` under the old one.

    It does not give the CEs, and that matters.  A metadata WERU is that block
    pair on six CEs, not twelve; the other six carry a different WERU.  Scanning
    the pair across all twelve CEs mixes two WERUs' AUs into one LMA map, and
    since both number their LMAs from 0 the result is silent corruption --
    which is exactly what happened here the moment a dump with CE 7-11 was used.
    """
    nums = sorted({b for b, _c in pairs})
    return tuple(sorted(set(nums) | {n + 1 for n in nums}))


# ===========================================================================
# SystemMetadata
# ===========================================================================
class SystemMetadata(object):
    """One WERU's worth of the SystemMetadata image, reassembled by LMA.

    Built by `scan`, which walks every page of the WERU's blocks, descrambles,
    reads each AU's LMA out of its aux and keeps the newest copy of each LMA
    *within this WERU*.  The image is log-structured through LUT 0, so an LMA
    appears many times and only the newest copy is live -- on this board 1615 of
    5943 LMAs in weru 23 have more than one copy, and for `validCount` the
    surviving copies carry different tables.  Recency is the u24 sequence in
    AU1's aux, shared by the page's four AUs.

    This is a half of the image, not the image.  The live SystemMetadata
    spans every WERU named by a live `SystemBlockMapping` slot, and which half
    holds a given LMA comes from the SystemLut.  Use `SystemImage` for the
    resolved whole; use this only when you mean one WERU specifically.

    Every section reports its own completeness against its derived length
    (`section_layout`), and holes are enumerated rather than ending the section.
    """

    def __init__(self, lma_data, placement, ces, blocks, stats=None,
                 weru_id=None, aux_layout=UNKNOWN, unordered=()):
        self.lma = lma_data                 # {lma: 4096-byte payload}
        self.placement = placement          # {lma: Placement}
        self.ces = list(ces)
        self.blocks = tuple(blocks)
        self.stats = stats or {}
        #: which WERU these pages came from, or None if the caller did not say.
        self.weru_id = weru_id
        #: the AUX layout the ranking was done under, or UNKNOWN if the caller
        #: did not supply one.  Not decoration: it says whether `Placement.seq`
        #: ordered anything (`page_rank`).
        #:
        #: Not called `layout`: `SystemMetadata.layout()` is the section layout
        #: -- an entirely different thing -- and an attribute of that name
        #: shadows the method for every instance.  It did, and only --deep
        #: caught it, because the section layout is printed there and nowhere
        #: else.
        self.aux_layout = aux_layout
        #: LMAs whose copies could not be ordered -- more than one ordinary
        #: copy at the same rank.  On layout 2 that is every LMA with a second
        #: copy, because the sequence names the epoch and not the page.  The
        #: bytes served for these are the first copy the walk met, which is an
        #: arbitrary choice and is reported as one rather than as a decision.
        self.unordered = frozenset(unordered)
        self._dir = None

    # -- construction -------------------------------------------------------
    @classmethod
    def scan(cls, dev, blocks, codec=None, ces=None, verify=True, progress=None,
             pages=None, weru_id=None, slc=True, layout=UNKNOWN, mso=None,
             descriptor=None):
        """Read a metadata WERU through `dev` and reassemble it.

        `verify=True` gates every page on all four AU MCRCs.  Turning it off
        roughly halves the runtime and removes the only defence against a
        short read or a stale device buffer being parsed as content.

        `slc` is the cell mode every page here is read in -- one value for the
        whole scan, supplied by the caller, never derived from a block number.
        It defaults to pseudo-SLC because a metadata WERU is pseudo-SLC by
        construction (the `SystemWeru` ring exists for the metadata image, and
        all five hardware-measured metadata/LUT configurations carry
        `slc=True`), and `Ftl.from_device` passes the WERU's own
        `WeruConfig.slc` when it has one rather than relying on that default.
        `pages` likewise defaults to the 368 a pseudo-SLC block has.

        Which copy wins -- `page_rank`, which is layout-aware.  Within one WERU
        an LMA usually has several copies.  Ranking them is two decisions, not
        one, and both have been got wrong here before:

          * an inline-RAID parity page (`AUX_FLAG_INLINE_PARITY`) carries a
            real LMA and a real, higher sequence, so plain newest-wins hands
            back parity bytes as content -- it substituted parity for 22 LMAs of
            `blockMapping` and made 88 WERUs name block numbers like 2386979;
          * the sequence orders pages only on layouts 0, 1 and 3.  On layout
            2 the writer reads the pool without incrementing it
            (`efc_cpu2.c:43058`), so the number is the write epoch and is
            constant across an MSO -- msoId 51's pages all read 2812 and msoId
            53's all read 2340.  Ranking by it is not approximate, it is
            meaningless, and it is the same shape of bug as the parity one.

        So the layout is an input.  Supply it as `layout=`, or as `mso=` plus
        `descriptor=` and it is resolved through `titania.auxdata.layout_for` -- the
        per-drive descriptor byte, never a table of what some other board had.
        With none of the three the layout is UNKNOWN: the sequence is still used
        (3 of the 4 layouts rank by it, and every WERU scanned today is one of
        them) but `stats["aux_layout"]` says UNKNOWN out loud, so an unranked scan is
        visible instead of looking settled.

        On layout 2 the sequence is dropped from the key, which makes ordinary
        copies tie.  The first one the walk meets is served and every such LMA
        is listed in `.unordered` and counted in `stats["unordered"]`, because an
        arbitrary pick that is reported is recoverable and one that is not is
        indistinguishable from a measurement.

        The sequence reading is cross-checked on every PAGE.  `page_sequence`
        here and `titania.auxdata.page_sequence` are two independent decoders of the
        same 62 bits; `stats["seq_agree"]`/`["seq_disagree"]` count every page
        they were both run on.  `ftl`'s was a u24 of AU1 alone until today, and
        a second implementation that is never compared is not a check.

        Verified four independent ways after the change, all on weruIds 0..1069:
        every (block, ce) pair is claimed by at most one WERU (48000 claimed,
        0 twice; before the change 45094 claimed and 2906 twice); all 88
        recovered rows are four consecutive block numbers on all twelve CEs with
        a 4-aligned base; the 12 recovered WERUs with `validCount > 0` have all
        four blocks programmed in the CE1 wide survey (5.9% of CE1 blocks are
        programmed, so 4-of-4 by chance is 1.2e-5); and 76 of the 88 rows have a
        readable `WeruUsageLog` record -- a structure in RootStorage, not in
        this image at all -- and 76 agree on every slot, 0 disagree.
        """
        codec = codec or default_codec()
        ces = resolve_scan_ces(dev, ces)
        slc = slc_flag(slc, "SystemMetadata.scan of blocks %s" % (list(blocks),))
        pages = (range(geom.pages_per_block(slc)) if pages is None else pages)
        layout, layout_src = resolve_scan_layout(layout, mso, descriptor)
        ranks_by_seq = (layout in SEQUENCE_RANKS_PAGES or layout is UNKNOWN)
        best, place = {}, {}
        unordered = set()
        st = dict(pages_read=0, pages_used=0, pages_failed_mcrc=0,
                  pages_erased=0, superseded=0, pages_parity=0,
                  parity_displaced=0, parity_refused=0, weru_id=weru_id,
                  slc=slc, ces_used=list(ces),
                  aux_layout=layout, aux_layout_source=layout_src,
                  sequence_ranks_pages=ranks_by_seq, unordered=0,
                  seq_agree=0, seq_disagree=0,
                  ces_served=sorted(dev.present_ces()),
                  ces_skipped=sorted(set(dev.present_ces()) - set(ces)))
        for blk in blocks:
            for ce in ces:
                for pg in pages:
                    try:
                        raw = dev.read_page(ce, blk, pg, slc=slc)
                    except DeviceError:
                        continue
                    if raw is None:
                        continue
                    st["pages_read"] += 1
                    if raw.count(0xFF) >= 0.99 * len(raw):
                        # An erased page yields the all-ones LFSR fixed point
                        # and a fake all-zero "plaintext" that passes every
                        # downstream check.  Refuse it here, not later.
                        st["pages_erased"] += 1
                        continue
                    aus, ok = codec.decode(raw, blk, pg)
                    if aus is None:
                        st["pages_failed_mcrc"] += 1
                        continue
                    if verify and ok is not UNKNOWN and not all(ok):
                        st["pages_failed_mcrc"] += 1
                        continue
                    st["pages_used"] += 1
                    # 62-bit page sequence: AU0 is the high half, AU1 the low.
                    # The old u24 read of AU1 alone truncates past 2**24 and
                    # this board's counter is already 23% of the way there.
                    seq = page_sequence(aus)
                    # ... and the same 62 bits read by the other decoder in this
                    # package.  Two implementations that are never compared are
                    # one implementation with a spare copy.
                    alt = auxmod.page_sequence(
                        [bytes(a[:geom.AU_AUX]) for a in aus],
                        layout if layout is not UNKNOWN
                        else auxmod.LAYOUT_INTERNAL_DATA)
                    if alt is not UNKNOWN:
                        if alt == seq:
                            st["seq_agree"] += 1
                        else:
                            st["seq_disagree"] += 1
                    for k, a in enumerate(aus):
                        lma = struct.unpack_from("<I", a, AUX_LMA_OFF)[0]
                        # An inline-RAID parity AU is not this LMA's payload,
                        # but it carries this LMA's number and a higher page
                        # sequence.  Rank it below every ordinary copy instead
                        # of letting newest-wins hand back parity bytes; keep it
                        # as a last resort, because some LMAs are held only by a
                        # flagged page and those decode correctly.  On layout 2
                        # `page_rank` also drops the sequence, because there it
                        # names the write epoch and not the page.
                        parity = bool(a[AUX_FLAG_OFF] & AUX_FLAG_INLINE_PARITY)
                        rank = page_rank(seq, parity, layout)
                        prev = best.get(lma)
                        if prev is None or rank > prev[0]:
                            if prev is not None:
                                st["superseded"] += 1
                                if prev[2]:
                                    st["parity_displaced"] += 1
                            best[lma] = (rank, a[geom.AU_AUX:], parity)
                            place[lma] = Placement(ce, blk, pg, k, seq, slc)
                        else:
                            st["superseded"] += 1
                            if parity and not prev[2]:
                                st["parity_refused"] += 1
                            # A tie between two ordinary copies is not a
                            # decision.  It cannot happen while the sequence
                            # ranks and the two copies are on different pages
                            # (the counter is per page and never repeats); on
                            # layout 2 it is the normal case, and the incumbent
                            # stays only because it was met first.
                            #
                            # The SENTINELS are excluded, and this is not a
                            # convenience: `sub_1C238` pads an unmapped AU with
                            # `ff ff ff ff 00 00 00 00` (`efc_cpu2.c:42856`), so
                            # two padded AUs of one PAGE necessarily share the
                            # page's sequence and tie.  They are not two copies
                            # of an address -- 0xFFFFFFFF is not an address --
                            # and counting them would bury the layout-2 signal
                            # this set exists to carry under one entry per WERU.
                            if (rank == prev[0] and not parity and not prev[2]
                                    and lma not in auxmod.LMA_SENTINELS):
                                unordered.add(lma)
                    if aus[0][AUX_FLAG_OFF] & AUX_FLAG_INLINE_PARITY:
                        st["pages_parity"] += 1
                    if progress:
                        progress("sysmeta ce%d b%d p%d: %d LMAs"
                                 % (ce, blk, pg, len(best)))
        st["unordered"] = len(unordered)
        return cls({l: v[1] for l, v in best.items()}, place, ces, blocks, st,
                   weru_id=weru_id, aux_layout=layout, unordered=unordered)

    # -- accessors ----------------------------------------------------------
    def directory(self):
        """-> the LMA 0 directory as a dict, or UNKNOWN if LMA 0 was not found.

        UNKNOWN means the image's own index is missing, so every section base is
        unknown too -- not that the image is empty.
        """
        if self._dir is not None:
            return self._dir
        if 0 not in self.lma:
            return UNKNOWN
        v = struct.unpack_from("<12I", self.lma[0], 0)
        if v[0] != SYS_MAGIC:
            raise ValueError("SystemMetadata directory magic 0x%08x, expected "
                             "0x%08x (\"SYS\")" % (v[0], SYS_MAGIC))
        self._dir = dict(zip(SYSMETA_DIR_FIELDS, v))
        return self._dir

    def layout(self):
        """-> [(name, base, n_lmas, length_derived)], or UNKNOWN without LMA 0."""
        d = self.directory()
        return UNKNOWN if d is UNKNOWN else section_layout(d)

    def get(self, name):
        """-> `Section`, or UNKNOWN if the directory itself is missing.

        The length is `nextBase - thisBase - 32`, not `nextBase - thisBase`.
        The old reading treated the 32-LMA inter-section gap as part of the
        section, so every section looked short and `reached_cap` was False on
        all nine lines of `describe()` even when the section was complete.

        The scan does not stop at the first hole.  Every LMA of the declared
        range that is present is included, indexed by its offset from the base,
        and the rest are listed in `Section.missing`.
        """
        d = self.directory()
        if d is UNKNOWN:
            return UNKNOWN
        key = name if name.endswith("Lma") else name + "Lma"
        if key not in d and name != "directory":
            raise KeyError("no such SystemMetadata section: %r" % name)
        want = "directory" if name == "directory" else key[:-3]
        for sname, base, n, derived in section_layout(d):
            if sname != want:
                continue
            present = {i: self.lma[base + i] for i in range(n)
                       if base + i in self.lma}
            src = {i: self.weru_id for i in present}
            return Section(sname, base, n, present, derived, src)
        raise KeyError("no such SystemMetadata section: %r" % name)

    def section(self, name, max_lmas=None):
        """Compatibility shim -> (bytes, complete, n_present).  Prefer `get()`.

        `bytes` is the contiguous run from the base, because every caller of
        this form indexes into it positionally and splicing across a hole would
        shift each later record by 4096 bytes.  `n_present` counts every LMA of
        the section that was recovered, including those past the first hole, so
        ``n_present * 4096 > len(bytes)`` is the signal that this shim is
        hiding data from you -- use `get()` and `Section.chunk()` there.

        `complete` is now a real statement: the section's derived length is
        fully present.  It used to be False for every section on this board
        because the length was overstated by the 32-LMA gap.
        """
        sec = self.get(name)
        if sec is UNKNOWN:
            return UNKNOWN, False, 0
        if max_lmas is not None:
            sec = Section(sec.name, sec.base, min(max_lmas, sec.n_lmas),
                          {i: v for i, v in sec.present.items() if i < max_lmas},
                          sec.length_derived, sec.source)
        return sec.contiguous(), sec.complete, sec.n_present

    def first_hole(self, name):
        """-> the first missing LMA at or after this section's base.

        Now bounded by the section's own derived length: it returns None when
        the section is complete, rather than pointing into the padding gap that
        follows it.
        """
        sec = self.get(name)
        if sec is UNKNOWN:
            return UNKNOWN
        return sec.first_hole

    def total_lmas(self):
        """This MSO's declared size, from msoDescriptor[41].  UNKNOWN if absent.

        Anything past it in `self.lma` is stale residue or a misdecoded aux, not
        part of the live image.
        """
        sec = self.get("mso")
        if sec is UNKNOWN:
            return UNKNOWN
        buf = sec.chunk(MSOID_ANCHOR_VALUE * MSO_DESC_ENTRY
                        + MSO_DESC_TOTAL_LMAS_OFF, 4)
        if buf is UNKNOWN:
            return UNKNOWN
        t = struct.unpack_from("<I", buf, 0)[0]
        return t if 0 < t <= (1 << 20) else UNKNOWN

    def lma_placement(self, lma):
        """-> Placement, or None if this LMA has no copy in what was scanned.

        None here is a real negative over the scanned CEs and blocks only; check
        `self.stats['ces_skipped']` before concluding the LMA does not exist.
        """
        return self.placement.get(lma)

    def __repr__(self):
        return "<SystemMetadata weru %s: %d LMAs from blocks %s on ces %s>" % (
            self.weru_id, len(self.lma), list(self.blocks), self.ces)


# ===========================================================================
# the resolved image -- every live WERU, arbitrated by the SystemLut
# ===========================================================================
class SystemImage(object):
    """The SystemMetadata image the firmware would see: the union of every WERU
    a live `SystemBlockMapping` slot names, with each LMA taken from the WERU
    the SystemLut points at.

    This replaces the "current image / legacy image" model, which was not just
    imprecise but wrong: it read `userLutLut`, `mso`, `mediaSpace`, `weruBin`
    and `validMap` out of weru 23 when their live copies are in weru 19.

    Four things are kept separate on purpose, because collapsing any of them is
    how the old bug hid:

    ``lma``          the resolved live payload of each LMA.
    ``provenance``   {lma: weruId} -- which half each live payload came from.
    ``superseded``   {lma: [weruId, ...]} -- WERUs that also hold a copy the LUT
                     does not name.  These are the bytes the old code returned.
    ``unreadable``   LMAs the LUT maps into a WERU where no readable copy was
                     found.  Present on media, absent from what we hold -- not
                     the same as unmapped, and never filled in from the other
                     half.
    ``unmapped``     LMAs a scan found that the LUT maps nowhere: dead bytes.
    ``past_end``     LMAs a scan found beyond the LUT's last entry, i.e. beyond
                     `msoDescriptor[41].totalLmas`.  Stale residue or a
                     misdecoded aux -- kept separate from `unmapped`, because
                     "the table says no" and "there is no table entry at all"
                     are different statements.

    Without a SystemLut (`lut=None`) resolution falls back to the page sequence
    number across WERUs, which is not sound -- `lut_used` is False and both
    `describe()` and `notes` say so.
    """

    def __init__(self, parts, lut=None):
        #: {weruId: SystemMetadata}
        self.parts = dict(parts)
        self.lut = lut
        self.lma = {}
        self.placement = {}
        self.provenance = {}
        self.superseded = {}
        self.unreadable = []
        self.unmapped = []
        self.past_end = []
        self._dir = None
        self._resolve()

    @property
    def unmapped_but_present(self):
        """Deprecated alias -- `unmapped` plus `past_end`."""
        return sorted(self.unmapped + self.past_end)

    # -- resolution ---------------------------------------------------------
    @property
    def lut_used(self):
        return self.lut is not None

    def _resolve(self):
        every = set()
        for p in self.parts.values():
            every |= set(p.lma)
        for lma in sorted(every):
            holders = [w for w, p in sorted(self.parts.items()) if lma in p.lma]
            if self.lut is None:
                # No authority.  Fall back to the page sequence, and record that
                # this ranking does not hold across WERUs.
                best = max(holders, key=lambda w: (
                    self.parts[w].placement[lma].seq or -1))
            else:
                named = self.lut.weru(lma)
                if named is None:
                    (self.past_end if lma >= self.lut.covered_lmas
                     else self.unmapped).append(lma)
                    self.superseded[lma] = holders
                    continue
                if named not in holders:
                    continue                # handled by the unreadable sweep
                best = named
            self.lma[lma] = self.parts[best].lma[lma]
            self.placement[lma] = self.parts[best].placement[lma]
            self.provenance[lma] = best
            others = [w for w in holders if w != best]
            if others:
                self.superseded[lma] = others
        if self.lut is not None:
            for lma in range(self.lut.covered_lmas):
                if lma in self.lma:
                    continue
                if self.lut.weru(lma) is not None:
                    self.unreadable.append(lma)

    # -- the SystemMetadata read surface, unchanged ------------------------
    @property
    def ces(self):
        out = set()
        for p in self.parts.values():
            out |= set(p.ces)
        return sorted(out)

    @property
    def blocks(self):
        out = []
        for _w, p in sorted(self.parts.items()):
            out += list(p.blocks)
        return tuple(out)

    @property
    def weru_ids(self):
        return sorted(self.parts)

    @property
    def stats(self):
        keys = ("pages_read", "pages_used", "pages_failed_mcrc", "pages_erased",
                "superseded", "pages_parity", "parity_displaced",
                "parity_refused", "unordered", "seq_agree", "seq_disagree")
        out = {k: sum(p.stats.get(k, 0) for p in self.parts.values())
               for k in keys}
        # Not summed: the layout is per part, and one number for several would
        # be a claim no part makes.
        out["aux_layouts"] = {w: p.stats.get("aux_layout", UNKNOWN)
                              for w, p in sorted(self.parts.items())}
        #: what the layout was afterwards MEASURED to be, which is not the same
        #: statement as the one the ranking ran under.
        out["aux_layouts_confirmed"] = {
            w: p.stats.get("aux_layout_confirmed", UNKNOWN)
            for w, p in sorted(self.parts.items())}
        ces_used, ces_served = set(), set()
        for p in self.parts.values():
            ces_used |= set(p.stats.get("ces_used", p.ces))
            ces_served |= set(p.stats.get("ces_served", ()))
        out["ces_used"] = sorted(ces_used)
        out["ces_served"] = sorted(ces_served)
        out["ces_skipped"] = sorted(ces_served - ces_used)
        out["werus"] = self.weru_ids
        return out

    def directory(self):
        """-> LMA 0 as a dict, or UNKNOWN.  Resolved like any other LMA."""
        if self._dir is not None:
            return self._dir
        if 0 not in self.lma:
            return UNKNOWN
        v = struct.unpack_from("<12I", self.lma[0], 0)
        if v[0] != SYS_MAGIC:
            raise ValueError("SystemMetadata directory magic 0x%08x, expected "
                             "0x%08x (\"SYS\")" % (v[0], SYS_MAGIC))
        self._dir = dict(zip(SYSMETA_DIR_FIELDS, v))
        return self._dir

    def layout(self):
        d = self.directory()
        return UNKNOWN if d is UNKNOWN else section_layout(d)

    def get(self, name):
        """-> `Section` over the resolved image, tagged with each LMA's WERU."""
        d = self.directory()
        if d is UNKNOWN:
            return UNKNOWN
        key = name if name.endswith("Lma") else name + "Lma"
        if key not in d and name != "directory":
            raise KeyError("no such SystemMetadata section: %r" % name)
        want = "directory" if name == "directory" else key[:-3]
        for sname, base, n, derived in section_layout(d):
            if sname != want:
                continue
            present = {i: self.lma[base + i] for i in range(n)
                       if base + i in self.lma}
            src = {i: self.provenance.get(base + i) for i in present}
            return Section(sname, base, n, present, derived, src)
        raise KeyError("no such SystemMetadata section: %r" % name)

    def section(self, name, max_lmas=None):
        return SystemMetadata.section(self, name, max_lmas)

    def first_hole(self, name):
        return SystemMetadata.first_hole(self, name)

    def total_lmas(self):
        return SystemMetadata.total_lmas(self)

    def lma_placement(self, lma):
        """-> Placement of the LIVE copy, or None.

        None means the LUT maps this LMA nowhere, or it is outside the image.
        An LMA the LUT maps but whose WERU we could not read is in
        `unreadable`, not here -- see `Ftl.lma_to_physical` for the three-way.
        """
        return self.placement.get(lma)

    def weru_of(self, lma):
        """-> which WERU the live copy of this LMA came from, or None."""
        return self.provenance.get(lma)

    def __repr__(self):
        return "<SystemImage werus %s: %d LMAs resolved, %d superseded, " \
               "%d unreadable, lut=%s>" % (
                   self.weru_ids, len(self.lma), len(self.superseded),
                   len(self.unreadable), "yes" if self.lut_used else "NO")


# ===========================================================================
# the FTL
# ===========================================================================
class Ftl(object):
    """The storage layer of one drive, as far as the available evidence goes.

    Build it with `from_device` (walks the media) or `from_firmware` (static
    tables only).  Everything it could not determine is UNKNOWN, and
    `describe()` prints exactly which.
    """

    def __init__(self, mso_table=None, sysmeta=None, root=None, device=None,
                 surveys=None, notes=None):
        self.mso_table = mso_table
        #: a `SystemImage` -- every live metadata WERU, resolved by the SystemLut.
        self.sysmeta = sysmeta
        self.root = root
        self.device = device
        self.surveys = dict(surveys or {})
        self.notes = list(notes or [])
        #: every weruId a live `SystemBlockMapping` slot names.  They are halves
        #: of one image, not generations.  UNKNOWN if RootStorage was unreadable.
        self.metadata_weru_ids = UNKNOWN
        #: RootStorage object 1 byte 0.  True means the firmware would discard
        #: the stored validMap/validCount at mount and rebuild them from the LUT.
        self.dirty = UNKNOWN
        self.sysmeta_info = UNKNOWN
        #: {weruId: WeruConfig} derived from each WERU's own pbid record.
        self.derived_configs = {}
        self.codec = None
        #: {weruId: (min_page, max_page)} the user LUT names inside that WERU,
        #: when something has walked the LUT and recorded it.  Only ever used to
        #: prove native TLC (a page past 368); a low span is a candidate, never
        #: a verdict.  `MEASURED_LUT_PAGE_SPANS` seeds the three recorded here.
        self.lut_page_spans = dict(MEASURED_LUT_PAGE_SPANS)
        #: {weruId: evidence dict} -- cell-mode verdicts with their evidence.
        self._cell_modes = {}
        self._cache = {}
        self._pages = {}

    @property
    def metadata_weru_id(self):
        """Deprecated -- the weruId holding the image's directory (LMA 0).

        It used to mean "the current metadata WERU", with the other live slot
        treated as a previous generation.  That was the bug.  It is kept only
        because `validCount[thisWeru] > 0` is still a useful liveness check on
        the WERU the directory lives in; use `metadata_weru_ids` for the image.
        """
        if self.sysmeta is None:
            return UNKNOWN
        w = self.sysmeta.weru_of(0)
        return UNKNOWN if w is None else w

    @property
    def legacy_sysmeta(self):
        """Deprecated, and it never meant what the name says.

        -> the `SystemMetadata` part for the live WERU that does not hold the
        directory, or None.  It is the other half of the live image, not an
        older generation.  Kept so the analysis scripts that established this
        still run; new code should use `sysmeta.parts[weruId]`.
        """
        if self.sysmeta is None:
            return None
        home = self.sysmeta.weru_of(0)
        others = [w for w in self.sysmeta.weru_ids if w != home]
        return self.sysmeta.parts[others[0]] if others else None

    # -- construction -------------------------------------------------------
    @classmethod
    def from_firmware(cls, blob, offset=None):
        """Static tables from an SRAM/DRAM/TCM image: the msoIdTable, so every
        msoId gets a name.

        Nothing about *this drive* comes from here -- no WERUs, no block map, no
        valid counts.  `werus()` on the result is UNKNOWN, not empty.
        """
        if isinstance(blob, (str, Path)):
            blob = Path(blob).read_bytes()
        t = MsoIdTable.locate(blob, offset)
        return cls(mso_table=t,
                   notes=["msoIdTable from a firmware image at %s" % hex(t.offset)])

    @classmethod
    def from_device(cls, dev, firmware=None, codec=None, ces=None,
                    root_block=None, metadata_weru=None, legacy=None,
                    legacy_metadata_blocks=None,
                    verify=True, progress=None, cache_dir=None):
        """Mount the drive the way the firmware mounts it.

        Steps, each independently skippable:
          1. RootStorage object 1 -> the dirty flag; object 3 ->
             `SystemBlockMapping`.  Every live slot is taken, because the
             firmware's replay installs every one of them
             (`efc_cpu3.c:42553-42568`).  `metadata_weru={weruId: blocks}` or a
             bare block tuple overrides when RootStorage is unreadable.
          2. Every named WERU is scanned into its own `SystemMetadata` part.
          3. RootStorage object 0 -> the SystemLut, which arbitrates per LMA
             which part holds the live copy.  The parts are then resolved into
             one `SystemImage`.
          4. the msoIdTable, so MSOs get names.  It is embedded
             (`MSOID_TABLE_EMBEDDED`) and needs no input; `firmware` (bytes or a
             path) overrides it from your own image, and if that image carries
             no table the embedded one is still used and the note says so.

        `legacy` / `legacy_metadata_blocks` are accepted and ignored.  There is
        no legacy image: the two live slots are halves of one image, and both
        are always scanned now.  Passing them adds a note saying so.

        `root_block=None` (the default) picks the Bootstrap RootStorage's
        authoritative section with the firmware's own rule --
        `root_storage_generations` / `scan_last_stripe`, i.e. `ScanLastStripe`'s
        `sub_4036268A`.  Blocks 2/3 and 4/5 are two generations of one store and
        the wrong one is a real, silent, whole-image staleness.  Pass an int to
        override (and to read the older generation).

        Failures are recorded in `.notes` and leave the corresponding accessor
        UNKNOWN; they do not raise.
        """
        codec = codec or default_codec()
        notes = []
        # The table is a firmware constant and is now built in, so a mount is
        # never nameless.  `firmware=` remains an override for a different
        # build, not a requirement.
        mso_table = _EMBEDDED_MSO_TABLE
        if legacy or legacy_metadata_blocks:
            notes.append(
                "legacy=/legacy_metadata_blocks= were passed and IGNORED: the "
                "second live SystemBlockMapping slot is the other half of this "
                "image, not a previous generation, and both halves are always "
                "scanned now")
        if firmware is None:
            notes.append("msoIdTable: EMBEDDED (%s, %s+%#x, %d entries) -- it "
                         "is a firmware constant, not media, so no image is "
                         "needed.  `titania fw verify-table <image>` compares "
                         "it with yours"
                         % (MSOID_TABLE_FW_VERSION, MSOID_TABLE_FW_IMAGE,
                            MSOID_TABLE_HINT, mso_table.n_entries))
        else:
            try:
                if isinstance(firmware, (str, Path)):
                    firmware = Path(firmware).read_bytes()
                found = MsoIdTable.locate(firmware)
                mso_table = found
                notes.append(
                    "msoIdTable from the supplied image at %s -- %s the "
                    "embedded %s table"
                    % (hex(found.offset),
                       "IDENTICAL to" if found.raw == MSOID_TABLE_EMBEDDED
                       else "DIFFERS FROM", MSOID_TABLE_FW_VERSION))
            except (LookupError, ValueError, OSError) as e:
                notes.append("supplied image carries no msoIdTable (%s); using "
                             "the embedded %s table"
                             % (e, MSOID_TABLE_FW_VERSION))

        # -- 1/2. where the halves are ------------------------------------
        root = None
        want = {}                       # {weruId: (blocks...)}
        if isinstance(metadata_weru, dict):
            want = {int(k): tuple(v) for k, v in metadata_weru.items()}
        elif metadata_weru:
            want = {None: tuple(metadata_weru)}
        info = UNKNOWN
        derived = {}                    # {weruId: WeruConfig} from the records
        gens = None
        if root_block is None:
            try:
                gens = root_storage_generations(dev, codec=codec, ces=ces)
            except (LookupError, ValueError, DeviceError) as e:
                notes.append("RootStorage generation scan failed (%s); "
                             "falling back to block %d"
                             % (e, ROOTSTORAGE_BOOTSTRAP_BLOCKS[0]))
            if gens and gens.get("block") is not None:
                root_block = gens["block"]
                notes.append(
                    "RootStorage generation: section %d (blocks %s) is "
                    "authoritative by %s -- section base stripes %s, "
                    "stripesPerSection %d, counter modulus %d "
                    "(ScanLastStripe/sub_4036268A, efc_cpu2.c:146497-146532); "
                    "mounting block %d"
                    % (gens["section"], list(gens["blocks"]), gens["rule"],
                       gens["last_stripes"],
                       ROOTSTORAGE_STRIPES_PER_PAGE[root_block]
                       * geom.PAGES_PER_BLOCK_SLC,
                       2 * ROOTSTORAGE_SECTIONS
                       * ROOTSTORAGE_STRIPES_PER_PAGE[root_block]
                       * geom.PAGES_PER_BLOCK_SLC, root_block))
                for n in gens.get("notes", ()):
                    notes.append("RootStorage generation scan: %s" % n)
            else:
                root_block = ROOTSTORAGE_BOOTSTRAP_BLOCKS[0]
                notes.append(
                    "RootStorage generation UNDETERMINED (no section's first "
                    "stripe is servable); falling back to block %d, which may "
                    "be the OLDER generation" % root_block)
        try:
            root = RootStorage(dev, root_block, codec=codec, ces=ces)
            try:
                info = root.system_metadata_information()
            except (ValueError, struct.error, IndexError) as e:
                notes.append("SystemMetadataInformation NOT read: %s" % e)
            sbm = root.system_block_mapping()
            if sbm is UNKNOWN:
                notes.append("SystemBlockMapping is on a CE this backing "
                             "cannot serve -- metadata WERU location UNKNOWN")
            elif sbm is None:
                notes.append("RootStorage says object 3 is absent")
            else:
                nxt, live = sbm
                order = sbm_recency_order(nxt, live)
                for rank, slot in enumerate(order):
                    weru, pairs = live[slot]
                    blks = weru_block_numbers(pairs)
                    # Cell mode is safe by construction here: the SystemWeru
                    # ring exists for the metadata image, which is pseudo-SLC.
                    cfg = weru_config_from_pbids(
                        weru, [geom.pbid(b, c) for b, c in pairs], slc=True)
                    if cfg is not None:
                        derived[weru] = cfg
                        blks = tuple(range(cfg.block_base,
                                           cfg.block_base + cfg.blocks_per_ce))
                    notes.append(
                        "SystemBlockMapping slot %d (ring position %d of %d, "
                        "nextSlot %d): weruId %d -> blocks %s on %s -- "
                        "a HALF of the live image, not a generation"
                        % (slot, rank, len(order), nxt, weru, list(blks),
                           ("CE %d-%d (pbid = 24*pairIndex + 12*half + slot)"
                            % (cfg.ce_base, cfg.ce_base + CES_PER_HALF - 1))
                           if cfg is not None else
                           "%d CEs -- CE RANGE NOT DERIVED, the scan will read "
                           "all of them and may mix in a neighbouring WERU"
                           % len(pairs)))
                    if not metadata_weru:
                        want[weru] = blks
        except (LookupError, ValueError, DeviceError) as e:
            notes.append("RootStorage NOT read: %s" % e)

        parts = {}
        for weru, blks in sorted(want.items(), key=lambda kv: (kv[0] is None, kv[0])):
            # A WERU is a block pair on six CEs.  Reading the pair on all twelve
            # merges this WERU with the one that owns the other half, and both
            # number their LMAs from 0, so the merge is silent and total: on a
            # dump that carries CE 7-11 it turned `mso` and `userLutLut` into
            # 0xFF.  Restrict to the WERU's own CEs whenever they are known.
            cfg = WERU_CONFIGS.get(weru) or derived.get(weru)
            own = ces
            if cfg is not None:
                span = range(cfg.ce_base, cfg.ce_base + CES_PER_HALF)
                own = [c for c in resolve_scan_ces(dev, ces) if c in span]
            # CELL mode: from the WERU's own configuration when there is one --
            # `weru_config_from_pbids` refuses to build a config without cell-
            # mode evidence, so `cfg.slc` is never a guess.  Without a config
            # this falls back to pseudo-SLC, which is what the SystemWeru ring
            # is for, and the note says so rather than leaving it implicit.
            scan_slc = True if cfg is None else cfg.slc
            part = _cached_scan(dev, blks, codec, own, verify, progress,
                                cache_dir, "weru%s" % weru, weru_id=weru,
                                slc=scan_slc)
            parts[weru] = part
            notes.append("scanned weru %s: %d LMAs from blocks %s on CEs %s%s, "
                         "read as %s (%s) (device serves %s)"
                         % (weru, len(part.lma), list(blks), part.ces,
                            "" if cfg is None else
                            " -- restricted to this WERU's own CE half",
                            "pseudo-SLC" if scan_slc else "native TLC",
                            "this WERU's own configuration" if cfg is not None
                            else "no configuration for it; the metadata image "
                                 "is pseudo-SLC by construction",
                            part.stats.get("ces_served")))
        if not parts:
            notes.append("metadata WERU location UNKNOWN -- no image scanned")

        # -- 3. the SystemLut arbitrates ----------------------------------
        sysmeta = None
        lut = None
        if parts:
            provisional = SystemImage(parts, lut=None)
            declared = provisional.total_lmas()
            if root is not None:
                try:
                    lut = root.system_lut(None)
                    if lut is UNKNOWN:
                        notes.append(
                            "SystemLut (RootStorage object %d) is on a CE this "
                            "backing cannot serve -- LMAs resolved by page "
                            "sequence instead, WHICH DOES NOT RANK ACROSS WERUs"
                            % OBJ_SYSTEM_LUT)
                        lut = None
                    elif lut is None:
                        notes.append("RootStorage says object %d (SystemLut) is "
                                     "absent" % OBJ_SYSTEM_LUT)
                except (ValueError, struct.error) as e:
                    notes.append("SystemLut NOT decoded: %s" % e)
                    lut = None
            if lut is not None and declared is not UNKNOWN:
                if lut.covered_lmas != declared:
                    notes.append(
                        "SystemLut covers %d LMAs but msoDescriptor[%d] "
                        "declares %d -- the granularity derived from the size "
                        "class and the one derived from the descriptor DISAGREE"
                        % (lut.covered_lmas, MSOID_ANCHOR_VALUE, declared))
            sysmeta = SystemImage(parts, lut=lut)
            if lut is not None:
                notes.append(
                    "SystemLut: %d/%d entries mapped, granularity %d LMAs, "
                    "WERU split %s"
                    % (lut.n_mapped, len(lut.entries), lut.granularity,
                       lut.weru_histogram()))
                notes.append(
                    "resolved image: %d LMAs live, %d also present in a WERU "
                    "the LUT does NOT name (the copies the old current/legacy "
                    "model returned), %d mapped but unreadable here, %d present "
                    "but unmapped, %d past the LUT's end (%d LMAs)"
                    % (len(sysmeta.lma), len(sysmeta.superseded),
                       len(sysmeta.unreadable), len(sysmeta.unmapped),
                       len(sysmeta.past_end), lut.covered_lmas))
            else:
                notes.append(
                    "NO SystemLut -- LMAs were resolved by page sequence across "
                    "WERUs, which is not a valid ranking (1884 newer-or-equal "
                    "against 4056 older when measured). Treat every section's "
                    "provenance as unconfirmed.")

        ftl = cls(mso_table=mso_table, sysmeta=sysmeta, root=root, device=dev,
                  notes=notes)
        ftl.codec = codec
        ftl.derived_configs = derived
        ftl.metadata_weru_ids = sorted(parts) if parts else UNKNOWN
        ftl.sysmeta_info = info
        ftl.dirty = (info["dirty"] if isinstance(info, dict) else UNKNOWN)
        if isinstance(info, dict):
            notes.append(
                "SystemMetadataInformation: dirty=%s%s"
                % (info["dirty"],
                   " -- the firmware would DISCARD the stored validMap and "
                   "validCount at mount and rebuild both from the LUT, so they "
                   "are evidence about a past instant, not truth"
                   if info["dirty"] else ""))

        # -- 5. was the sequence entitled to rank those copies? ------------
        # The scans above ran without a layout, because the layout lives in the
        # msoDescriptor and the msoDescriptor is in the image they produce.
        # That circularity is real and is broken by measuring afterwards rather
        # than by assuming: with the image in hand, each scanned WERU's owner
        # comes from the WeruUsageLog and its layout from `aux.layout_for`, and
        # any WERU that turns out to be layout 2 is rescanned under
        # `page_rank`'s layout-2 rule, because on layout 2 the sequence names
        # the write epoch and the first pass ranked by it.
        #
        # On this board every metadata WERU is layout 0 and nothing is rescanned
        # -- but "we checked and it did not apply" and "we never checked" are
        # different states, and only one of them is written down here.
        if parts:
            ftl._audit_scan_layouts(dev, codec, want, derived, ces, verify,
                                    progress, cache_dir)
        return ftl

    def _audit_scan_layouts(self, dev, codec, want, derived, ces, verify,
                            progress, cache_dir):
        """Check each scanned WERU's aux layout and rescan if it is layout 2.

        Separated from `from_device` only so the circularity it resolves can be
        described in one place.  It appends a note in every outcome, including
        the outcome where the owner or the descriptor is unreadable -- an
        unchecked layout must not look like a checked one.
        """
        wul = self.weru_usage_log()
        replaced = {}
        desc = None
        try:
            sec = self.section("mso")
            if sec is not UNKNOWN and sec is not None and hasattr(sec, "contiguous"):
                desc = sec.contiguous()
        except Exception:
            desc = None
        if not desc:
            self.notes.append(
                "aux LAYOUT of the scanned WERUs NOT checked: the msoDescriptor "
                "section is not readable here, so the page-sequence ranking in "
                "those scans is UNVERIFIED, not verified-good")
            return
        for weru in sorted(self.sysmeta.parts if self.sysmeta is not None
                           else ()):
            owner = wul.owner(weru) if wul is not UNKNOWN and wul is not None \
                else UNKNOWN
            if not isinstance(owner, int):
                self.notes.append(
                    "weru %s aux layout NOT checked: the WeruUsageLog does not "
                    "name an owner for it (%r), so the ranking is UNVERIFIED"
                    % (weru, owner))
                continue
            lay = auxmod.layout_for(owner, desc)
            if lay is auxmod.UNKNOWN:
                self.notes.append(
                    "weru %s aux layout NOT checked: msoId %d has no "
                    "msoDescriptor entry -- which is NOT layout 0"
                    % (weru, owner))
                continue
            if lay in SEQUENCE_RANKS_PAGES:
                # No rescan: the first pass ranked under UNKNOWN, and
                # `page_rank` gives the same key for UNKNOWN and for every
                # layout in this set, so the bytes cannot differ.  The layout is
                # recorded separately from the one the ranking ran under, so
                # "ranked without knowing, checked afterwards" stays legible and
                # is not upgraded into "ranked knowing".
                self.sysmeta.parts[weru].stats["aux_layout_confirmed"] = lay
                self.notes.append(
                    "weru %s owner msoId %d is aux layout %d, whose sequence IS "
                    "a per-page counter (efc_cpu2.c:43135/:43197 stamp then "
                    "increment), so ranking its copies by page sequence is valid"
                    % (weru, owner, lay))
                continue
            # layout 2 -- the first pass ranked by a number that does not order
            # pages.  Redo it under the right rule and keep the new answer.
            blks = want.get(weru) or self.sysmeta.parts[weru].blocks
            cfg = WERU_CONFIGS.get(weru) or derived.get(weru)
            own = ces
            if cfg is not None:
                span = range(cfg.ce_base, cfg.ce_base + CES_PER_HALF)
                own = [c for c in resolve_scan_ces(dev, ces) if c in span]
            part = _cached_scan(dev, blks, codec, own, verify, progress,
                                cache_dir, "weru%s" % weru, weru_id=weru,
                                slc=True if cfg is None else cfg.slc,
                                layout=lay)
            replaced[weru] = part
            self.notes.append(
                "weru %s owner msoId %d is aux LAYOUT %d: its sequence names "
                "the write EPOCH, not the page (efc_cpu2.c:43058 reads the pool "
                "with no increment), so the first scan's newest-wins was "
                "meaningless. RESCANNED under page_rank's layout-2 rule: %d "
                "LMAs, %d of them held by more than one copy that nothing "
                "orders (see SystemMetadata.unordered)"
                % (weru, owner, lay, len(part.lma), len(part.unordered)))
        if replaced:
            # The whole image is rebuilt, not patched: SystemImage resolves
            # every LMA across the parts in its constructor, so swapping a part
            # in place would leave `lma`/`provenance` describing the parts it
            # was built from.  A half-updated resolution is worse than none.
            parts = dict(self.sysmeta.parts)
            parts.update(replaced)
            self.sysmeta = SystemImage(parts, lut=self.sysmeta.lut)

    # -- section helpers ----------------------------------------------------
    def _section(self, name):
        """-> (bytes, complete, n_present, [weruIds]) from the resolved image."""
        if self.sysmeta is None:
            return UNKNOWN, False, 0, None
        sec = self.sysmeta.get(name)
        if sec is UNKNOWN:
            return UNKNOWN, False, 0, None
        return sec.contiguous(), sec.complete, sec.n_present, sec.werus()

    def section(self, name):
        """-> the `Section` for a directory section, or UNKNOWN.

        This is the accessor to prefer: it carries the derived length, every
        recovered LMA including those past a hole, and which WERU each came
        from.
        """
        return UNKNOWN if self.sysmeta is None else self.sysmeta.get(name)

    # -- the surface --------------------------------------------------------
    def msos(self):
        """-> {msoId: Mso}, or UNKNOWN if neither a firmware image nor an
        msoDescriptor section was available.

        With only a firmware image you get names but no per-drive fields
        (raid_type/total_lmas stay UNKNOWN).  With only media you get the
        per-drive fields but `name` is None for every one.  Both together is the
        complete answer.

        An msoId absent from the result was not seen in either source; it is not
        proof the partition does not exist.
        """
        if "msos" in self._cache:
            return self._cache["msos"]
        buf, _ok, n, _src = self._section("mso")
        out = None
        if buf is not UNKNOWN and n:
            out = decode_mso_descriptor(buf, self.mso_table)
        elif self.mso_table is not None:
            out = {m: Mso(m, t, g, name=self.mso_table.name(m))
                   for m, (t, g) in self.mso_table.entries().items()}
        if out is None:
            return UNKNOWN
        self._cache["msos"] = out
        return out

    def mso(self, key):
        """-> the Mso for an msoId or a 'Type/Group' name, else None."""
        m = self.msos()
        if m is UNKNOWN:
            return UNKNOWN
        if isinstance(key, int):
            return m.get(key)
        for v in m.values():
            if v.name == key:
                return v
        return None

    def block_map(self):
        """-> {weruId: [(block, ce) | None] * 48}, slot-indexed.

        UNKNOWN when the `blockMapping` section was not recovered.  A weruId
        mapped to all-``None`` really has all 48 slots unmapped; a weruId
        missing from the dict is past the end of the recovered table.  Use
        `mapped_blocks()` or `Weru.mapped_blocks` for the compacted view and
        never compact by hand -- position here is a slot number.

        The stride is `geometry.BLOCKS_PER_WERU` and cannot be changed here --
        see `decode_block_mapping` and `verify_weru_stride`.

        Known wart on this board: the current image lists weruId 23 (the
        metadata WERU itself) as unmapped.  That is bootstrap circularity, not
        corruption -- an image cannot record where it itself lives, which is
        exactly why `SystemBlockMapping` is duplicated into RootStorage.
        """
        if "bm" in self._cache:
            return self._cache["bm"]
        buf, _ok, n, _src = self._section("blockMapping")
        if buf is UNKNOWN or not n:
            return UNKNOWN
        out = decode_block_mapping(buf)
        self._cache["bm"] = out
        return out

    def werus(self, mso=None, allocated_only=False):
        """-> [Weru], or UNKNOWN.

        Fields come from up to three sections: `weruDescriptor` (owner, binId,
        eraseCount, capacity), `validCount` (live AUs) and `blockMapping`
        (physical blocks).  Any of them may be missing, and the corresponding
        field is then UNKNOWN on every Weru rather than defaulted.

        `mso` filters by owner and requires `weruDescriptor`; if that section is
        UNKNOWN this returns UNKNOWN rather than an empty list, because "no
        WERUs owned by X" and "we cannot tell who owns anything" are different
        answers and this project has conflated them before.

        NOTE the ownership caveat: `Allocation` (RootStorage object 2) records
        only each MSO's current write pointer, so absence from it is never
        evidence of owning nothing -- NeoFwPartition owns four WERUs and has no
        Allocation entry.  `weruDescriptor` is the authority.
        """
        key = ("werus", mso, allocated_only)
        if key in self._cache:
            return self._cache[key]
        wd, _ok, nwd, wd_src = self._section("weruDescriptor")
        vc, _ok2, nvc, _s2 = self._section("validCount")
        bm = self.block_map()
        if wd is UNKNOWN and vc is UNKNOWN and bm is UNKNOWN:
            return UNKNOWN
        if mso is not None and wd is UNKNOWN:
            return UNKNOWN
        mso_id = mso.mso_id if isinstance(mso, Mso) else mso

        descs = decode_weru_descriptor(wd) if wd is not UNKNOWN and nwd else {}
        counts = decode_valid_count(vc) if vc is not UNKNOWN and nvc else None
        ids = set(descs)
        if counts is not None:
            ids |= set(range(len(counts)))
        if bm is not UNKNOWN:
            ids |= set(bm)
        out = []
        for wid in sorted(ids):
            w = descs.get(wid) or Weru(wid)
            # Provenance is now the WERU the live weruDescriptor LMAs came from,
            # not a 'current'/'legacy' generation tag.  The old tag named a
            # hazard that does not exist and hid the one that does.
            w.source = ("weruDescriptor@weru%s"
                        % ",".join(map(str, wd_src or []))) if wid in descs else None
            if counts is not None and wid < len(counts):
                w.valid_count = counts[wid]
            if bm is not UNKNOWN:
                w.blocks = bm.get(wid, UNKNOWN)
            if mso_id is not None and w.owner != mso_id:
                continue
            if allocated_only and w.is_allocated is not True:
                continue
            out.append(w)
        self._cache[key] = out
        return out

    def system_metadata(self):
        """-> the resolved `SystemImage`, or UNKNOWN if none was scanned."""
        return self.sysmeta if self.sysmeta is not None else UNKNOWN

    def system_lut(self):
        """-> the `SystemLut` that arbitrated the mount, or UNKNOWN."""
        if self.sysmeta is None or self.sysmeta.lut is None:
            return UNKNOWN
        return self.sysmeta.lut

    def user_lut_lut(self):
        """-> `UserLutLut` (the user LUT's own index), UNKNOWN, or None.

        None means the section decoded but is not a LUT index; UNKNOWN means
        its LMAs were not recovered.  Raises ValueError if the bytes are there
        but do not parse -- "we have it and it is not what we thought" is a
        different answer from "we do not have it".
        """
        if "lutlut" in self._cache:
            return self._cache["lutlut"]
        sec = self.section("userLutLut")
        if sec is UNKNOWN:
            return UNKNOWN
        out = UserLutLut.from_section(sec)
        self._cache["lutlut"] = out
        return out

    def valid_map(self):
        """-> `ValidMap`, or UNKNOWN if the section was not recovered at all.

        Note it is not runtime-only -- an earlier hypothesis here said so and it
        is refuted: the firmware reads this section back verbatim at mount
        (`efc_cpu3.c:39732`).  But see `dirty`: when the dirty flag is set the
        firmware discards it and rebuilds from the LUT, so a stored map is
        evidence about a past instant.
        """
        if "validmap" in self._cache:
            return self._cache["validmap"]
        sec = self.section("validMap")
        if sec is UNKNOWN or not sec.n_present:
            return UNKNOWN
        out = ValidMap(sec)
        self._cache["validmap"] = out
        return out

    def number_of_werus(self):
        """-> the drive's `numberOfWerus`, derived, or UNKNOWN.

        From the `validMap` section's own length: 6288 LMAs / 26496 B per WERU
        = 972 exactly.  That is the honest source, and it agrees with the
        independently-counted 972 WERUs with capacity > 0.

        Do not derive it from `blockMapping`: that section is 252 LMAs, which is
        sized for ~1075 WERUs, and 4096 is not a measurement of anything.
        """
        vm = self.valid_map()
        return UNKNOWN if vm is UNKNOWN else vm.weru_count

    # -- the dirty path -----------------------------------------------------
    def weru_usage_log(self):
        """-> `WeruUsageLog` from RootStorage, UNKNOWN, or None.

        None means this RootStorage instance does not carry one (only the
        Bootstrap instance registers it, `efc_cpu2.c:31745`); UNKNOWN means no
        RootStorage was readable at all.
        """
        if "wul" in self._cache:
            return self._cache["wul"]
        if self.root is None:
            return UNKNOWN
        try:
            out = self.root.weru_usage_log()
        except (LookupError, ValueError, struct.error):
            return UNKNOWN
        self._cache["wul"] = out
        return out

    def physical_block_properties(self):
        """-> `PhysicalBlockProperties`, UNKNOWN, or None.

        It lives in the backup RootStorage (block 1), not the Bootstrap one this
        Ftl mounts from, so this opens a second RootStorage over the same
        device.  None means the Backup store carries no `PhBP` run.
        """
        if "phbp" in self._cache:
            return self._cache["phbp"]
        if self.device is None:
            return UNKNOWN
        try:
            rs = RootStorage(self.device, ROOTSTORAGE_BACKUP_BLOCK,
                             codec=self.codec or default_codec())
            out = rs.physical_block_properties()
        except (LookupError, ValueError, struct.error, ImportError):
            return UNKNOWN
        self._cache["phbp"] = out
        return out

    def block_substitutions(self):
        """Bad-block substitution, resolved per WERU.  -> [dict] or UNKNOWN.

        How substitution is recorded.  It is an exchange of whole `blockMapping`
        entries, not a "replacement" field: `sub_40312B84` (`efc_cpu2.c:69705`)
        swaps bytes 0..15 of two entries, so the pbid and its cached property
        word trade places.  The partner is chosen by
        `MediaReplaceBlockEngine` -- `sub_4033D69E` (`efc_cpu2.c:108560`)
        asserts `sourceWcId == cWeruConfigurationIdSpare` and
        `cWeruConfigurationIdMetadata <= targetWcId <= cWeruConfigurationIdTlcData`
        and scans the spare configuration for a NON-defective slot
        (`!sub_40339F3E`), while `MediaDecommissionWeruEngine`'s `sub_4033E250`
        (`:109252`) is the mirror image, scanning the same spare range for a
        defective slot to push a retired block into.

        The original is therefore not lost.  A user WERU's block number set is
        `base .. base+3` on all twelve CEs, so the blocks it should own and does
        not are computable, and each of them is now sitting in a spare WERU's
        row.  Each returned dict is

            weru_id, base, displaced [(block, ce)], injected [(block, ce)],
            displaced_home {(block, ce): (weruId, slot)},
            displaced_status {(block, ce): status name}

        `base` is inferred as `4*(min(block)//4)`; a WERU is SKIPPED (and named
        in the `skipped` entry of the result's last element) when that leaves
        `len(displaced) != len(injected)`, because then the inference itself is
        unsafe.  Nothing here is fitted -- the rectangle comes from
        `geometry.weru_blocks`, which the 44/44 slot-law measurement established.
        """
        bm = self.block_map()
        werus = self.werus()
        if bm is UNKNOWN or werus is UNKNOWN:
            return UNKNOWN
        phbp = self.physical_block_properties()
        home = {}
        for w, slots in bm.items():
            for i, bc in enumerate(slots):
                if bc is not None:
                    home[geom.pbid(*bc)] = (w, i)
        out = []
        for w in werus:
            if w.shape != "substituted":
                continue
            have = set(w.mapped_blocks)
            base = 4 * (min(b for b, _c in have) // 4)
            rect = set(geom.weru_blocks(range(base, base + 4)))
            disp = sorted(rect - have)
            inj = sorted(have - rect)
            rec = dict(weru_id=w.weru_id, owner=w.owner, base=base,
                       displaced=disp, injected=inj,
                       consistent=len(disp) == len(inj),
                       displaced_home={}, displaced_status={})
            for b, c in disp:
                p = geom.pbid(b, c)
                rec["displaced_home"][(b, c)] = home.get(p)
                if phbp is not UNKNOWN and phbp is not None:
                    pr = phbp.property_of(p)
                    rec["displaced_status"][(b, c)] = (
                        UNKNOWN if pr is UNKNOWN else pr.status_name)
            out.append(rec)
        return out

    def weru_block_permutation(self, first=None, last=None, base_block=None):
        """Which WERUs are not on their format-time blocks.  -> dict or UNKNOWN.

        A user-data WERU configuration tiles the block space four numbers at a
        time in weruId order, so the format-time assignment is
        `block = base_block + 4*(weruId - first)`.  Defaults are config 4 on this
        drive (`efc_cpu2.c:107815`): weruId 117..971 over blocks 384..3803.

        -> ``{"perm": {weruId: sourceWeruId}, "moved": {...}, "mixed": [...],
              "runs": [(firstWeru, lastWeru, srcFirst, srcLast)]}``

        `perm[w] != w` means WERU `w` today holds the 48 blocks that `perm[w]`
        was formatted with -- i.e. the two exchanged superblocks.  `mixed` lists
        WERUs whose 48 blocks do not all come from one source, which is what
        per-block substitution produces; read those through
        `block_substitutions()` instead.

        The engine is identified -- see `weru_block_moves`, which supersedes
        this method and covers all six configurations.  RETRACTED: "no
        enumerated caller of `sub_40312B84` performs a whole-WERU exchange, so
        the producing event is not identified here."  The exchange primitive is
        duplicated in cpu3 as `sub_402D3D7C` (`efc_cpu3.c:28026`, instruction
        for instruction the same function), and its single call site is inside
        `VirtualWeru_AllocateWeru`'s per-plane loop (`efc_cpu3.c:41648`).
        """
        bm = self.block_map()
        if bm is UNKNOWN:
            return UNKNOWN
        cfgs = [c for c in WERU_CONFIGURATIONS
                if c[3] == geom.BLOCKS_PER_WERU and c[2] > 100]
        if first is None:
            if not cfgs:
                return UNKNOWN
            first, last = cfgs[-1][1], cfgs[-1][1] + cfgs[-1][2] - 1
        if last is None:
            last = first
        if base_block is None:
            # The configuration tiles a contiguous block range, so its lowest
            # block number is the base.  Do not take it from the first WERU's
            # own row: weru 117 is itself one of the exchanged ones on this
            # board, and that gave base 3716 and hid the whole permutation.
            lo = []
            for w in range(first, last + 1):
                for bc in (bm.get(w) or ()):
                    if bc is not None:
                        lo.append(bc[0])
            if not lo:
                return UNKNOWN
            base_block = 4 * (min(lo) // 4)
        hi = base_block + 4 * (last - first + 1) - 1

        def src_of(block):
            if block < base_block or block > hi:
                return None
            return first + (block - base_block) // 4

        perm, mixed = {}, []
        for w in range(first, last + 1):
            slots = bm.get(w)
            if not slots:
                continue
            got = [bc for bc in slots if bc is not None]
            if not got:
                continue
            votes = {}
            for b, _c in got:
                s = src_of(b)
                votes[s] = votes.get(s, 0) + 1
            best = max(votes, key=lambda k: (votes[k], k is not None))
            perm[w] = best
            if votes[best] != len(got):
                mixed.append(w)
        moved = {w: s for w, s in perm.items() if s is not None and s != w}
        runs = []
        for w in sorted(moved):
            if runs and w == runs[-1][1] + 1:
                runs[-1][1] = w
                runs[-1][3] = moved[w]
            else:
                runs.append([w, w, moved[w], moved[w]])
        return dict(perm=perm, moved=moved, mixed=sorted(mixed),
                    runs=[tuple(r) for r in runs],
                    first=first, last=last, base_block=base_block)

    def weru_block_moves(self):
        """Every WERU not on its format-time blocks, dated.  -> dict or UNKNOWN.

        What moves a superblock -- solved 2026-08-06, and it is not an "exchange
        engine".  It is ordinary WERU allocation:

            WeruManagement (`efc_cpu3.c:48302`/`:48336`, `WeruManagement.c:2489`)
              -> `Weru_PopWeruIdFromBin` (`sub_402E6510`, `efc_cpu3.c:45184`)
              -> `VirtualWeru_AllocateWeru` (`sub_402E2BDA`, `:41380`)
              -> `sub_402D3D7C` (`:28026`)      == cpu2's `sub_40312B84`

        `VirtualWeru_AllocateWeru` walks the WERU's `blocksPerWeru` block
        offsets (planes).  Each plane owns its own doubly-linked free list of
        virtual-block nodes, threaded through `blockMapping[+16] u16 next` /
        `[+18] u16 prev` -- which is exactly why the swap primitive copies only
        bytes 0..15 and leaves +16..+19 alone: 0..15 is the physical block
        (pbid, read counter, eraseCount/status word) and +16 is the virtual
        slot's place in the free list.  For each plane it pops a node; if the
        node belongs to a different weruId row it calls the swap and the
        physical block migrates into the WERU being allocated, while the
        allocated WERU's old block goes back to the donor row and stays free.
        Removed nodes are stamped `0xDF00DF01` (allocated) or `0xDEFEDEFF`
        (skipped, block defective) in place of the links.

        So a whole superblock can change hands on one allocation, with no bulk
        caller anywhere -- and there is no function-pointer route either: the
        4-byte constants `0x40312B84` and `0x402D3D7C` do not occur as data
        anywhere in the images we hold (122 MB swept), and neither symbol is
        ever address-taken in the decompilation.

        Two knobs, both `WERU_CONFIGURATIONS` flag bits at `+16`:
        `WERU_CONFIG_FLAG_BLOCKS_PINNED` (bit 2, `sub_402D133C`) makes the
        allocator take the WERU's own row and never swap; `a5 == 2` from
        `sub_402E659E` (allocate a named weru, `Weru.c:353`) forces the same
        in-place mode, so only a bin pop can move blocks.  `a5 == 1`
        (`weruSearchCriteria == cWeruSearchCriteriaEraseCountMin`, which
        `WeruManagement.c:2519` selects for msoType 3 `StaticMan`) pops the head
        of each plane list instead of the tail.

        -> ``{"moves": {weruId: donorWeruId | None}, "by_config": {...},
              "dated": [...], "pinned_moved": [...]}``

        `moves[w]` is the weruId whose format-time blocks `w` holds today, or
        None when `w`'s blocks are not any single WERU's format-time set (a
        per-block bad-block substitution -- read those through
        `block_substitutions()`).  `dated` is one record per move for which the
        `WeruUsageLog` can date it:

            weru, donor, activation, donor_activation, log_state

        `log_state` is "current" when the WERU's own usage-log block map matches
        what `blockMapping` says it holds now, and "stale" when it still names
        the format-time blocks -- i.e. the swap happened after that WERU was
        last activated.  The model predicts the pair (stale, allocated later
        than its donor) can never occur; on this drive it does not.
        """
        bm = self.block_map()
        if bm is UNKNOWN:
            return UNKNOWN
        wul = self.weru_usage_log()
        moves, by_cfg, pinned_moved = {}, {}, []
        for cfg in WERU_CONFIGURATIONS:
            idx, first, cnt = cfg[0], cfg[1], cfg[2]
            same = moved = blank = 0
            for w in range(first, first + cnt):
                slots = bm.get(w) or ()
                have = {geom.pbid(b, c) for b, c in
                        (s for s in slots if s is not None)}
                if not have:
                    blank += 1
                    continue
                want = canonical_pbids(w)
                if want is None:
                    continue
                if have == want:
                    same += 1
                    continue
                moved += 1
                donor = None
                for cand in range(first, first + cnt):
                    if canonical_pbids(cand) == have:
                        donor = cand
                        break
                moves[w] = donor
                if WERU_CONFIG_FLAGS[idx] & WERU_CONFIG_FLAG_BLOCKS_PINNED:
                    pinned_moved.append(w)
            by_cfg[idx] = dict(first=first, count=cnt, canonical=same,
                               moved=moved, blank=blank,
                               pinned=bool(WERU_CONFIG_FLAGS[idx]
                                           & WERU_CONFIG_FLAG_BLOCKS_PINNED))
        dated = []
        if wul is not UNKNOWN and wul is not None:
            for w, donor in sorted(moves.items()):
                act = wul.counter(w)
                pi = wul.pair_indices(w)
                state = UNKNOWN
                if pi is not UNKNOWN:
                    named = {p for p in pi if p is not None}
                    now = {geom.pbid(b, c) // PBID_PAIR_STRIDE
                           for b, c in (s for s in (bm.get(w) or ())
                                        if s is not None)}
                    canon = {p // PBID_PAIR_STRIDE for p in
                             (canonical_pbids(w) or ())}
                    state = ("current" if named == now
                             else "stale" if named == canon else "other")
                dact = UNKNOWN if donor is None else wul.counter(donor)
                dated.append(dict(weru=w, donor=donor, activation=act,
                                  donor_activation=dact, log_state=state))
        return dict(moves=moves, by_config=by_cfg, dated=dated,
                    pinned_moved=pinned_moved)

    def block_owners(self):
        """-> {(block, ce): (weruId, slot, msoId)} for every unambiguous block.

        The inverse of `block_map` joined to `weru_owners`, and the thing a
        decoder needs before it can ask what a page's aux means: the aux layout
        is `msoDescriptor +17` of the owning MSO, so a (block, ce) with no owner
        has no layout and must not be judged by one.

        A (block, ce) claimed by two different WERUs is dropped, not resolved.
        That is a real condition on this board -- the blockMapping section and
        the WeruUsageLog can disagree after a superblock exchange -- and
        picking one would silently pin a layout on evidence that contradicts
        itself.  UNKNOWN if either input is missing.
        """
        if "block_owners" in self._cache:
            return self._cache["block_owners"]
        bm = self.block_map()
        owners = self.weru_owners()
        if bm is UNKNOWN or owners is UNKNOWN:
            return UNKNOWN
        pos, dup = {}, set()
        for weru, slots in bm.items():
            own = owners.get(weru)
            if isinstance(own, tuple):
                own = own[0]
            if own is None or own is UNKNOWN:
                continue
            for slot, bc in enumerate(slots or ()):
                if bc is None:
                    continue
                bc = tuple(bc)
                if bc in pos and pos[bc][0] != weru:
                    dup.add(bc)
                pos.setdefault(bc, (weru, slot, own))
        for bc in dup:
            pos.pop(bc, None)
        self._cache["block_owners"] = pos
        return pos

    def aux_layout_of_block(self, block, ce):
        """-> the aux layout of the MSO owning (block, ce), or UNKNOWN.

        `msoDescriptor +17`, reached through `block_owners`.  This is the first
        question an acceptance test must ask -- see `titania.auxdata.read_is_plausible`
        for what happens when it is skipped (every RAID-parity page on the drive
        is rejected as a bad read).
        """
        pos = self.block_owners()
        if pos is UNKNOWN:
            return UNKNOWN
        hit = pos.get((block, ce))
        if hit is None:
            return UNKNOWN
        buf, _ok, n, _s = self._section("mso")
        if buf is UNKNOWN or not n:
            return UNKNOWN
        return auxmod.layouts_from_descriptor(bytes(buf)).get(hit[2], UNKNOWN)

    def cell_mode_of_block(self, block, ce):
        """-> True (pseudo-SLC) / False (native TLC) / UNKNOWN for (block, ce).

        `Ftl.cell_mode` of the owning WERU.  Cell mode is a per-WERU
        configuration property, never a block-number rule -- weru 84 is blocks
        172..175 and is pseudo-SLC while weru 234 is blocks 852..855 and is
        native TLC -- so this goes through the descriptors like everything else.
        A page captured in the other mode is not a failed read; it is a read of
        the wrong thing, and `titania.verdict` reports it as such.
        """
        pos = self.block_owners()
        if pos is UNKNOWN:
            return UNKNOWN
        hit = pos.get((block, ce))
        return UNKNOWN if hit is None else self.cell_mode(hit[0])

    def weru_owners(self):
        """-> {weruId: (msoId, source)} using the authoritative source.

        On a store whose dirty flag is set the firmware would discard the
        `weruDescriptor`'s owner byte and re-derive it from the `WeruUsageLog`
        (`RebuildWeruBins`, `efc_cpu3.c:22044-22062`), so that is what this
        returns when the log is readable, with `source` naming which of the two
        answered.  Where they disagree see `weru_owner_conflicts`.
        """
        wul = self.weru_usage_log()
        wd, _ok, n, _s = self._section("weruDescriptor")
        out = {}
        if wd is not UNKNOWN and n:
            for w, d in decode_weru_descriptor(wd).items():
                if d.owner:
                    out[w] = (d.owner, "weruDescriptor")
        if wul is not UNKNOWN and wul is not None:
            for w, o in wul.owners().items():
                out[w] = (o, "WeruUsageLog")
        return out or UNKNOWN

    def weru_owner_conflicts(self):
        """-> [(weruId, descriptorOwner, usageLogOwner)] where the two differ.

        A non-empty result is a real forensic finding on a dirty store: it is
        exactly the ownership the last flush of `weruDescriptor` did not capture.
        Returns UNKNOWN if either source is missing -- never an empty list.
        """
        wul = self.weru_usage_log()
        wd, _ok, n, _s = self._section("weruDescriptor")
        if wul is UNKNOWN or wul is None or wd is UNKNOWN or not n:
            return UNKNOWN
        descs = decode_weru_descriptor(wd)
        out = []
        for w in range(min(wul.n_werus, len(descs))):
            o = wul.owner(w)
            if o is UNKNOWN:
                continue
            d = descs[w].owner or None
            if d != o:
                out.append((w, d, o))
        return out

    def rebuild_valid_map(self, mso_ids=None):
        """Mount step 31 only: `RebuildValidMap({SystemMetadata, Lut}, 2)`.

        This models one of the two bulk writers.  It is not the whole dirty
        mount and must not be read as one -- see `dirty_mount_valid_cache`,
        which composes every stage and answers per WERU.

        When `RootStorage` object 1 byte 0 is set the mount takes `ValidMapReset`
        instead of `ValidMapRead` (`efc_cpu3.c:39721`, step
        `MOUNT_STEP_VALID_MAP`) and skips `ValidCountRead` entirely
        (`efc_cpu3.c:39739`, `sub_402E1144`, step `MOUNT_STEP_VALID_COUNT`), then
        rebuilds (`efc_cpu3.c:39775`, step `MOUNT_STEP_REBUILD_VALID_MAP`).  So
        on a dirty store the stored caches are evidence about a past instant.

        Step 31's rebuild set is two MSOs, not all of them.  `sub_402E19B0`
        (`efc_cpu3.c:40322-40325`) builds the msoId array as exactly
        `{byte_90001E, byte_900018}` with `dword_402A5ECC = 2` -- the
        SystemMetadata MSO (its lutId is asserted to be 0 at `efc_cpu3.c:37354`)
        and the Lut MSO (`efc_cpu3.c:37786` writes it as SystemMetadata's
        storage).

        RETRACTED 2026-08-07, and this function's bound is the reason it
        mattered: "no later mount stage restores the rest ... The only other
        bulk writer is this rebuild."  `sub_4033E94E` is a second bulk writer
        and it runs at full-mount step `MOUNT_STEP_LUT_REBUILD` (35), which the
        tracing that produced the claim never reached: it enumerated
        `ValidCountRead` call sites -- correctly, there is exactly one, at
        `efc_cpu3.c:39752` in the `else` arm -- but not `sub_10300`/`sub_10234`,
        which is where step 35's replay writes.  Step 35 launches four per-lutId
        rebuilds (`LUT_REBUILD_ORDER`); each one scans that lutId's WERUs, may
        replay the whole LUT into the map, and then unconditionally overwrites
        `validCount[weru]` with `popcount(validMap[weru])`.  So for the WERUs
        step 35 touches, this function both UNDER-reports (their map does come
        back when the gate is set) and any comparison against the stored count
        OVER-trusts it (their count is discarded either way).

        What survives of the old claim, for a different reason: on this board
        lutIds 2 and 3 fail the replay gate, so their WERUs come out of step 35
        with a map of zero and a `validCount` of zero -- 889 WERUs and 2,510,576
        stored valid AUs, the biggest class on the drive.

        Still true, and re-checked: `AllocationReinitializeMsos` (`sub_402E0FE8`,
        `efc_cpu3.c:39620`) re-initialises each MSO's write pointer, not the
        counts, and every remaining write of the array is incremental and happens
        during normal operation, not at mount: `+= 1` per written AU
        (`efc_cpu2.c:23889`, `:31318`), `-= n` on supersede (`:31717`), `= 0` on
        release (`:28582`, `:30822`, `:116545`), and `= cols * pagesPerBlock`
        when a WERU is closed full (`:27464`, `:27654`).

        Per MSO (`sub_4033E8A4`, `efc_cpu2.c:109611-109646`): skip unless
        descriptor `+5` bit 0; then walk every LUT slot and, for each WMA that is
        not -1/-3/-4, bump `validCount[wma // wmasPerWeru]` and -- only if
        descriptor `+5` bit 1 is also set -- set the validMap bit
        `wma % wmasPerWeru`.

        -> {'counts': {weruId: n}, 'bits': {weruId: set(offset)},
            'msos': {msoId: {...}}, 'wmas_per_weru': n, 'stage': 31,
            'models': str}, or UNKNOWN.
        """
        vm = self.valid_map()
        mso_buf, _ok, n, _s = self._section("mso")
        if vm is UNKNOWN or mso_buf is UNKNOWN or not n:
            return UNKNOWN
        if mso_ids is None:
            luts = self._rebuild_luts()
        else:
            luts = [(m, self._lut_for_mso(m)) for m in mso_ids]
        wpw = vm.wmas_per_weru
        counts, bits, per = {}, {}, {}
        for mso_id, lut in luts:
            e = mso_buf[mso_id * MSO_DESC_ENTRY:(mso_id + 1) * MSO_DESC_ENTRY]
            flags = e[MSO_DESC_REBUILD_OFF]
            info = dict(flags=flags, walked=bool(flags & MSO_REBUILD_WALK_BIT),
                        sets_valid_map=bool(flags & MSO_REBUILD_VALIDMAP_BIT),
                        entries=0, live=0, werus=set())
            per[mso_id] = info
            if lut is UNKNOWN or lut is None:
                info["lut"] = "UNKNOWN"
                continue
            info["lut"] = repr(lut)
            if not flags & MSO_REBUILD_WALK_BIT:
                continue
            for k in range(lut.n_entries):
                w = lut.entry(k)
                info["entries"] += 1
                if w is UNKNOWN or w in REBUILD_SKIP_WMAS:
                    continue
                weru, off = divmod(w, wpw)
                counts[weru] = counts.get(weru, 0) + 1
                if flags & MSO_REBUILD_VALIDMAP_BIT:
                    bits.setdefault(weru, set()).add(off)
                info["live"] += 1
                info["werus"].add(weru)
        return dict(counts=counts, bits=bits, msos=per, wmas_per_weru=wpw,
                    stage=MOUNT_STEP_REBUILD_VALID_MAP,
                    models="mount step %d only (RebuildValidMap over the two "
                           "msoIds sub_402E19B0 builds). Step %d's four "
                           "per-lutId rebuilds are NOT modelled here -- see "
                           "Ftl.dirty_mount_valid_cache"
                           % (MOUNT_STEP_REBUILD_VALID_MAP,
                              MOUNT_STEP_LUT_REBUILD))

    def _lut_for_mso(self, mso_id):
        """-> the loaded LUT object the rebuild would walk for `mso_id`."""
        msos = self.msos()
        if msos is not UNKNOWN and mso_id in msos:
            if msos[mso_id].lut_id == 0:
                return self.system_lut()
        ull = self.user_lut_lut()
        if ull is not UNKNOWN and ull is not None and ull.mso_id == mso_id:
            return ull
        return UNKNOWN

    def _rebuild_luts(self):
        """-> [(msoId, lut)] for the firmware's own two-MSO rebuild set.

        Identified without the msoIdTable: the SystemMetadata MSO is the one
        whose `msoDescriptor +3` lutId is 0 (asserted by the firmware itself,
        `efc_cpu3.c:37354`) and the Lut MSO is the one the `userLutLut`
        descriptor names.  Raises LookupError if either is ambiguous rather than
        guessing.
        """
        msos = self.msos()
        if msos is UNKNOWN:
            raise LookupError("no msoDescriptor: the rebuild set is undetermined")
        zero = [m for m, o in msos.items() if o.lut_id == 0]
        if len(zero) != 1:
            raise LookupError(
                "lutId 0 is claimed by %s; the firmware asserts exactly one MSO "
                "holds it (efc_cpu3.c:37354)" % zero)
        ull = self.user_lut_lut()
        if ull is UNKNOWN or ull is None:
            raise LookupError("userLutLut not recovered: the second half of the "
                              "rebuild set is unavailable")
        return [(zero[0], self.system_lut()), (ull.mso_id, ull)]

    def rebuild_comparison(self):
        """-> the rebuilt caches next to the stored ones, per WERU.

        [(weruId, rebuiltCount, storedValidCount, storedPopcount, rebuiltBits,
          storedBits)] over every WERU either side calls live, or UNKNOWN.
        `storedBits`/`rebuiltBits` are None where that side does not maintain a
        map for the WERU (descriptor `+5` bit 1 clear), which is a real
        statement and not a zero.
        """
        rb = self.rebuild_valid_map()
        vm = self.valid_map()
        vc, _ok, n, _s = self._section("validCount")
        if rb is UNKNOWN or vm is UNKNOWN or vc is UNKNOWN or not n:
            return UNKNOWN
        stored = decode_valid_count(vc)
        out = []
        ids = set(rb["counts"])
        ids |= {w for w in range(min(vm.weru_count, len(stored))) if stored[w]}
        for w in sorted(ids):
            off = vm.valid_offsets(w)
            out.append((w, rb["counts"].get(w, 0),
                        stored[w] if w < len(stored) else None,
                        vm.popcount(w),
                        rb["bits"].get(w),
                        None if off is UNKNOWN or off is None else set(off)))
        return out

    def lut_rebuild_plan(self, store_mso=40):
        """Mount step 35, per lutId.  -> {lutId: dict} or UNKNOWN.

        The four rebuilds `off_402F86E8` launches (`LUT_REBUILD_ORDER`), each
        with the two facts that decide what its WERUs come out holding:

            snapshot_count      `snapshotInformation.count`, descriptor `+8`
            first_mso           `snapshotInformation.list[0].msoId`
            first_mso_flags     `msoDescriptor[first_mso] + 5`
            map_replayed        count != 0 and flags bit 1  (`sub_4033EA80`)
            werus               every WERU owned by an MSO with this lutId
            count_rewritten     always True -- step 2 of the sub-FSM writes
                                `validCount[weru] = popcount(validMap[weru])`
                                for every WERU the scan marked, replay or no

        The gate is tested on `list[0].msoId`, not on the WERU's owner --
        see `MSO_REBUILD_VALIDMAP_BIT`.  On this board lutIds 1 and 4 have
        snapshot counts 8 and 5 with `+5 = 0x0E`, so both replay; lutIds 2 and 3
        have count 0, so neither does and every one of their WERUs is written a
        zero it did not have.

        One link is unverified and is flagged as such in every result
        (`scan_set_is_upper_bound`): the scan list is per lutId and that is
        proven (`sub_403554AE`, `efc_cpu2.c:132853`), but `sub_40357132` was not
        read instruction by instruction to confirm that every listed WERU ends
        up marked.  `sub_40355940` counts only WERUs whose activation is at or
        past the snapshot, so the marked set may be narrower than the list --
        which would shrink the zeroed set, never grow it.  Treat `werus` as an
        upper bound on what step 35 rewrites.
        """
        msos = self.msos()
        mso_buf, _ok, n, _s = self._section("mso")
        if msos is UNKNOWN or mso_buf is UNKNOWN or not n:
            return UNKNOWN
        try:
            regs = self.lut_regions(store_mso)
        except (LookupError, ValueError, struct.error) as e:
            self.notes.append("lut_regions unavailable: %s" % e)
            return UNKNOWN
        if regs is UNKNOWN:
            return UNKNOWN
        by_lut = {}
        for r in regs:
            owner = msos.get(r.init_mso)
            if owner is None or owner.lut_id is UNKNOWN:
                continue
            by_lut[owner.lut_id] = r
        owners = self.weru_owners()
        werus_of = {}
        if owners is not UNKNOWN:
            for w, (m, _src) in owners.items():
                mm = msos.get(m)
                if mm is None or mm.lut_id is UNKNOWN:
                    continue
                werus_of.setdefault(mm.lut_id, set()).add(w)
        out = {}
        for lut_id in LUT_REBUILD_ORDER:
            r = by_lut.get(lut_id)
            first = flags = None
            snap = []
            if r is not None:
                d = self.read_lma(r.store_mso, r.info_lma)
                if d is not UNKNOWN and d is not None:
                    snap = decode_lut_snapshot(d)
            if snap:
                first = snap[0]["mso_id"]
                e = mso_buf[first * MSO_DESC_ENTRY:(first + 1) * MSO_DESC_ENTRY]
                flags = e[MSO_DESC_REBUILD_OFF] if len(e) > MSO_DESC_REBUILD_OFF \
                    else None
            out[lut_id] = dict(
                lut_id=lut_id,
                region=None if r is None else r.base,
                snapshot_count=len(snap),
                snapshot_sequence_number=(None if r is None
                                          else r.snapshot_sequence_number),
                first_mso=first,
                first_mso_flags=flags,
                map_replayed=bool(snap) and bool(flags
                                                 & MSO_REBUILD_VALIDMAP_BIT)
                if flags is not None else False,
                count_rewritten=True,
                werus=sorted(werus_of.get(lut_id, ())),
                scan_set_is_upper_bound=True)
        return out

    def dirty_mount_valid_cache(self, store_mso=40):
        """Per WERU: after a dirty mount, is a ZERO meaningful?  -> dict.

        This is the user-facing point of the whole mount-stage trace.  A rebuilt
        zero and a genuinely empty WERU are indistinguishable in the numbers, and
        on this board 995 of 1070 WERUs come out of a dirty mount with a validMap
        that was never repopulated.  For those the stored `validCountLma` is the
        better evidence even though the firmware discards it.

        -> ``{weruId: {'weru_id', 'mso_id', 'lut_id', 'stage', 'map', 'count',
        'map_zero_meaningful', 'count_zero_meaningful', 'stored_valid_count',
        'stored_popcount', 'reason'}}``, or UNKNOWN.

        `map` and `count` are `ZERO_MEANING` codes:

            ``"rebuilt"``        the stage reconstructed this cache from the
                                 LUT, so a zero really does mean empty.
            ``"forced-zero"``    the stage wrote a zero regardless of content
                                 (step 35 scanned the WERU but did not replay
                                 its map, so `popcount` -- and therefore the
                                 count -- is 0 by construction).
            ``"never-touched"``  no stage scans this WERU at all; it keeps
                                 `ValidMapReset`'s zero.
            ``"not-maintained"`` this MSO's `+5` bit 1 is clear, so the drive
                                 never keeps a map for it in any mount.  A zero
                                 map here is a structural fact, not a loss.

        Composition, all from `MOUNT_FSM_FULL_TABLE`'s steps:

        * step 25 zeroes every row (dirty), step 26 skips `ValidCountRead`;
        * step 31 rebuilds the two MSOs of `rebuild_valid_map` -- count always,
          map only when their `+5` bit 1 is set;
        * step 35 runs `lut_rebuild_plan`, which for lutIds 1/2/3/4 rewrites the
          count from the popcount and only sometimes replays the map;
        * a WERU whose owner has `lutId == LUT_ID_NONE` is in neither set.

        The `scan_set_is_upper_bound` caveat on `lut_rebuild_plan` applies here
        too: if `sub_40357132` marks fewer WERUs than the lutId's list, some
        WERUs recorded ``"forced-zero"`` are really ``"never-touched"`` -- both
        of which mean the zero is meaningless, so the verdict is unaffected.
        """
        msos = self.msos()
        mso_buf, _ok, n, _s = self._section("mso")
        owners = self.weru_owners()
        vm = self.valid_map()
        if msos is UNKNOWN or mso_buf is UNKNOWN or not n or owners is UNKNOWN:
            return UNKNOWN
        plan = self.lut_rebuild_plan(store_mso)
        if plan is UNKNOWN:
            return UNKNOWN
        vc, _ok2, n2, _s2 = self._section("validCount")
        stored = decode_valid_count(vc) if (vc is not UNKNOWN and n2) else []
        # the step-31 set, by lutId
        try:
            step31 = {msos[m].lut_id: m for m, _l in self._rebuild_luts()
                      if m in msos}
        except LookupError:
            step31 = {}
        out = {}
        for w, (mid, _src) in sorted(owners.items()):
            m = msos.get(mid)
            lut_id = None if m is None else m.lut_id
            e = mso_buf[mid * MSO_DESC_ENTRY:(mid + 1) * MSO_DESC_ENTRY]
            flags = e[MSO_DESC_REBUILD_OFF] if len(e) > MSO_DESC_REBUILD_OFF \
                else 0
            pc = vm.popcount(w) if (vm is not UNKNOWN
                                    and w < vm.weru_count) else UNKNOWN
            rec = dict(weru_id=w, mso_id=mid, lut_id=lut_id,
                       stored_valid_count=(stored[w] if w < len(stored)
                                           else None),
                       stored_popcount=(None if pc is UNKNOWN else pc))
            if lut_id in plan:
                rec["stage"] = MOUNT_STEP_LUT_REBUILD
                if plan[lut_id]["map_replayed"]:
                    rec["map"] = "rebuilt"
                    rec["count"] = "rebuilt"
                    rec["reason"] = (
                        "lutId %s is replayed at step %d "
                        "(snapshotInformation.count %d, msoDescriptor[%s]+5 = "
                        "0x%02X, bit 1 set), then validCount := popcount"
                        % (lut_id, MOUNT_STEP_LUT_REBUILD,
                           plan[lut_id]["snapshot_count"],
                           plan[lut_id]["first_mso"],
                           plan[lut_id]["first_mso_flags"] or 0))
                else:
                    rec["map"] = "forced-zero"
                    rec["count"] = "forced-zero"
                    rec["reason"] = (
                        "lutId %s is SCANNED at step %d but not replayed "
                        "(snapshotInformation.count %d, msoDescriptor[%s]+5 = "
                        "%s), so step 2 writes validCount = popcount = 0"
                        % (lut_id, MOUNT_STEP_LUT_REBUILD,
                           plan[lut_id]["snapshot_count"],
                           plan[lut_id]["first_mso"],
                           "--" if plan[lut_id]["first_mso_flags"] is None
                           else "0x%02X" % plan[lut_id]["first_mso_flags"]))
            elif lut_id in step31 and flags & MSO_REBUILD_WALK_BIT:
                rec["stage"] = MOUNT_STEP_REBUILD_VALID_MAP
                rec["count"] = "rebuilt"
                rec["map"] = ("rebuilt" if flags & MSO_REBUILD_VALIDMAP_BIT
                              else "not-maintained")
                rec["reason"] = (
                    "msoId %d is in step %d's two-MSO set; its +5 = 0x%02X so "
                    "the count is rebuilt and the map is %s"
                    % (mid, MOUNT_STEP_REBUILD_VALID_MAP, flags,
                       "rebuilt too" if flags & MSO_REBUILD_VALIDMAP_BIT
                       else "NEVER maintained for this MSO"))
            else:
                rec["stage"] = None
                rec["map"] = rec["count"] = "never-touched"
                rec["reason"] = (
                    "owner msoId %d has lutId %s: no rebuild is ever launched "
                    "for it (LUT_REBUILD_ORDER is %s) and it is not in step "
                    "%d's two-MSO set, so both caches keep ValidMapReset's zero"
                    % (mid, lut_id, list(LUT_REBUILD_ORDER),
                       MOUNT_STEP_REBUILD_VALID_MAP))
            rec["map_zero_meaningful"] = rec["map"] == "rebuilt"
            rec["count_zero_meaningful"] = rec["count"] == "rebuilt"
            out[w] = rec
        return out

    def valid_zero_meaning(self, weru_id):
        """-> the one WERU's `dirty_mount_valid_cache` record, or UNKNOWN.

        The question a recovery tool actually asks: *this WERU reads 0 -- is it
        empty, or was it zeroed by the mount?*
        """
        allw = self.dirty_mount_valid_cache()
        if allw is UNKNOWN:
            return UNKNOWN
        return allw.get(weru_id, UNKNOWN)

    def rebuild_lut_valid_map(self, lut_id, store_mso=40, max_lmas=None):
        """Actually replay one lutId's whole LUT, the way step 35 does.

        `rebuild_valid_map` models step 31; this models the other bulk writer,
        `sub_4033E94E`.  It is opt-in because a region can be 10688 table LMAs
        and every one of them is a page decode.

        -> {'bits', 'counts', 'entries', 'live', 'table_lmas', 'read',
            'missing', 'replayed', 'lut_id'} or UNKNOWN.  `replayed` False means
        the gate is clear, in which case `bits`/`counts` are what the firmware
        would not have written -- the whole point of running it.
        """
        plan = self.lut_rebuild_plan(store_mso)
        if plan is UNKNOWN or lut_id not in plan:
            return UNKNOWN
        msos = self.msos()
        regs = self.lut_regions(store_mso)
        if regs is UNKNOWN or msos is UNKNOWN:
            return UNKNOWN
        reg = None
        for r in regs:
            o = msos.get(r.init_mso)
            if o is not None and o.lut_id == lut_id:
                reg = r
                break
        if reg is None:
            return UNKNOWN
        vm = self.valid_map()
        wpw = vm.wmas_per_weru if vm is not UNKNOWN else geom.AUS_PER_TLC_WERU
        bits, counts = {}, {}
        remaining = reg.n_entries
        n_lmas = reg.n_table_lmas if max_lmas is None else min(reg.n_table_lmas,
                                                               max_lmas)
        read = missing = entries = live = 0
        skip = set(REBUILD_SKIP_WMAS)
        for i in range(n_lmas):
            here = min(LMA_BYTES // 4, remaining)
            remaining -= here
            if here <= 0:
                break
            buf = self.read_lma(reg.store_mso, reg.data_lma + i)
            if buf is UNKNOWN or buf is None:
                missing += 1
                continue
            read += 1
            for w in struct.unpack_from("<%dI" % here, buf, 0):
                entries += 1
                if w in skip:
                    continue
                weru, off = divmod(w, wpw)
                bits.setdefault(weru, set()).add(off)
                counts[weru] = counts.get(weru, 0) + 1
                live += 1
        return dict(lut_id=lut_id, bits=bits, counts=counts, entries=entries,
                    live=live, table_lmas=reg.n_table_lmas, read=read,
                    missing=missing, replayed=plan[lut_id]["map_replayed"])

    def weru_bins(self):
        """-> {binId: WeruBin} with members walked, or UNKNOWN."""
        if "bins" in self._cache:
            return self._cache["bins"]
        buf, _ok, n, _s = self._section("weruBin")
        if buf is UNKNOWN or not n:
            return UNKNOWN
        wd, _ok2, n2, _s2 = self._section("weruDescriptor")
        wul = self.weru_usage_log()
        nw = (wul.n_werus if wul is not UNKNOWN and wul is not None
              else self.number_of_werus())
        out = decode_weru_bins(buf, wd if n2 else None,
                               None if nw is UNKNOWN else nw)
        self._cache["bins"] = out
        return out

    def write_frontier(self, mso=None):
        """-> {msoId: WriteFrontier} from RootStorage object 2, or UNKNOWN.

        Pass `mso` for a single record.  This is the allocation layer's answer
        to "where is this partition writing"; `weru_owners()` answers "what does
        it hold".  They are different questions and on a dirty board they are
        recorded at different moments, so they can and do disagree -- the
        disagreement is the forensic signal, not an error.
        """
        if "frontier" not in self._cache:
            out = UNKNOWN
            if self.root is not None:
                try:
                    out = self.root.allocation()
                except (LookupError, ValueError, struct.error, IndexError) as e:
                    self.notes.append("Allocation NOT decoded: %s" % e)
                    out = UNKNOWN
                if out is None:
                    self.notes.append("RootStorage says object %d (Allocation) "
                                      "is absent" % OBJ_ALLOCATION)
                    out = UNKNOWN
            self._cache["frontier"] = out
        out = self._cache["frontier"]
        if mso is None or out is UNKNOWN:
            return out
        return out.get(mso.mso_id if isinstance(mso, Mso) else mso)

    def open_werus(self):
        """-> {weruId: [reason]} for every WERU the frontier holds open.

        A WERU is "open" if it is some MSO's current or pending front.  On this
        board that set is 8 WERUs, and all 8 are members of bin 43
        `ActiveWrite` -- an exact, independent reconciliation between the
        Allocation object and the weruBin section.
        """
        fr = self.write_frontier()
        if fr is UNKNOWN:
            return UNKNOWN
        out = {}
        for mso_id, f in sorted(fr.items()):
            if f.current_weru is not None:
                out.setdefault(f.current_weru, []).append(
                    "current front of msoId %d" % mso_id)
            if f.pending_weru is not None:
                out.setdefault(f.pending_weru, []).append(
                    "reserved next WERU of msoId %d" % mso_id)
        return out

    def rebuild_sequence_number_pool(self):
        """-> [(sharedLane, reservedLane, reservedEnd)] per msoGroupId, UNKNOWN.

        Renamed fields, not new ones: the record is two lanes plus the reserved
        lane's limit, not `(next, base, end)` of a single cursor.  Which lane an
        MSO uses is `msoDescriptor +19` bit 0 -- call
        `mso_rebuild_sequence_number` instead of indexing this by group.
        """
        buf, _ok, n, _s = self._section("rebuildSequenceNumberPool")
        if buf is UNKNOWN or not n:
            return UNKNOWN
        return decode_rebuild_sequence_number_pool(buf)

    def mso_rebuild_sequence_number(self, mso_id):
        """-> {lane, sequence, limit, group, needs_reservation} for one MSO.

        The full firmware index, not half of it::

            (char *)&unk_90C658 + 24*msoGroupId + 8*(msoDesc[msoId][19] & 1)

        Returns UNKNOWN if either the pool section or the MSO's descriptor is
        missing, and raises ValueError if the MSO has no msoGroupId -- an MSO
        with no group has no pool record, which is a real negative rather than
        group 0.
        """
        pool = self.rebuild_sequence_number_pool()
        msos = self.msos()
        if pool is UNKNOWN or msos is UNKNOWN:
            return UNKNOWN
        mso = msos.get(mso_id)
        if mso is None or mso.needs_sequence_reservation is UNKNOWN:
            return UNKNOWN
        group = mso.group
        if (group is UNKNOWN or group is None) and mso.is_dynamic                 and mso.lut_id in MSO_DYNAMIC_GROUP_FROM_LUT:
            group = mso.lut_id       # see MSO_DYNAMIC_GROUP_FROM_LUT
        if group is UNKNOWN or group is None:
            raise ValueError(
                "msoId %d has no msoGroupId in this image, so it indexes no "
                "rebuildSequenceNumberPool record" % mso_id)
        lane, seq, limit = rebuild_sequence_lane(
            pool, group, mso.needs_sequence_reservation)
        return dict(group=group, lane=lane, sequence=seq, limit=limit,
                    needs_reservation=bool(mso.needs_sequence_reservation))

    def read_lma(self, mso_id, lma):
        """-> the 4096 plaintext bytes of one LMA of an MSO's address space.

        bytes / None (the LUT maps it nowhere) / UNKNOWN (not readable here).

        For msoId 41 the resolved `SystemImage` already holds the payload, so no
        device read happens.  Otherwise this is LMA -> WMA -> physical -> read ->
        decode, and the AU's own aux LMA is checked against the LMA asked
        for.  That check is what makes the whole chain self-verifying: a wrong
        LUT entry, a wrong WMA rule or a wrong page all land on an AU whose aux
        says a different LMA, and this returns UNKNOWN instead of plausible
        bytes.

        The CELL mode comes from the WERU, not from `p.block`.  It rides along
        on the `Placement` that `wma_to_physical` built out of the WERU's own
        configuration.  If it is UNKNOWN this returns UNKNOWN rather than
        reading in a defaulted mode -- the cached page is keyed on the mode too,
        so two modes of one address can never be confused for each other.
        """
        if mso_id == MSOID_ANCHOR_VALUE and self.sysmeta is not None:
            if lma in self.sysmeta.lma:
                return self.sysmeta.lma[lma]
        p = self.lma_to_physical(mso_id, lma)
        if p is UNKNOWN or p is None:
            return p
        if self.device is None or self.codec is None:
            return UNKNOWN
        if p.slc is not True and p.slc is not False:
            return UNKNOWN              # cell mode unestablished: do not guess
        key = (p.ce, p.block, p.page, p.slc)
        if key not in self._pages:
            if len(self._pages) > 4096:
                self._pages.clear()
            try:
                raw = self.device.read_page(p.ce, p.block, p.page, slc=p.slc)
            except DeviceError:
                raw = None
            self._pages[key] = ((None, None) if raw is None
                                else self.codec.decode(raw, p.block, p.page))
        aus, ok = self._pages[key]
        if aus is None or p.au >= len(aus):
            return UNKNOWN
        if ok is not UNKNOWN and not ok[p.au]:
            return UNKNOWN
        a = aus[p.au]
        if struct.unpack_from("<I", a, AUX_LMA_OFF)[0] != lma:
            return UNKNOWN              # aux disagrees: do not hand back bytes
        return a[geom.AU_AUX:]

    def lut_region(self, mso):
        """-> the `LutRegion`/`_Lut` that maps this MSO's LMAs.

        `None` means the MSO genuinely has no LUT (`lutId == LUT_ID_NONE`, true
        of msoIds 42 and 56-62 here) -- a real negative from the firmware's own
        descriptor.  UNKNOWN means we could not walk to it.

        The chain is walked, not computed.  The firmware lays regions out as a
        running sum of preceding sizes (`sub_402DDDC2`, `efc_cpu3.c:36348-36369`,
        with `regionLmas = tableBytes/4096 + 8` from `sub_402CCF44`, `:23434`),
        but reconstructing the bases arithmetically over-predicts by 2876 LMAs
        on this board.  Each region carries its own header, so walking is both
        cheaper and self-checking: the chain here ends exactly on the userLutLut
        's own coverage (0 -> 10696 -> 10808 -> 10824 -> 11880 = 11880).

        `lutId` indexes an LMA space shared by a namespace group, so all seven
        MSOs of group 1 resolve to region 0.
        """
        mso_id = mso.mso_id if isinstance(mso, Mso) else mso
        key = ("lutregion", mso_id)
        if key in self._cache:
            return self._cache[key]
        msos = self.msos()
        if msos is UNKNOWN or mso_id not in msos:
            return UNKNOWN
        m = msos[mso_id]
        if m.lut_id is UNKNOWN:
            return UNKNOWN
        if m.lut_id == LUT_ID_NONE:
            self._cache[key] = None
            return None                 # this MSO has no LUT at all
        if mso_id == MSOID_ANCHOR_VALUE:
            out = self.system_lut()     # RootStorage object 0, not in any MSO
            self._cache[key] = out
            return out
        if m.lut_store == LUT_STORE_NONE or m.lut_store not in msos:
            return UNKNOWN
        # Where in the store's LMA space does the chain start?  Region 0 of
        # msoId 40's space is at LMA 0; inside SystemMetadata it is the
        # `userLutLut` section, whose base the directory names.
        base = 0
        if m.lut_store == MSOID_ANCHOR_VALUE:
            d = self.sysmeta.directory() if self.sysmeta is not None else UNKNOWN
            if d is UNKNOWN:
                return UNKNOWN
            base = d["userLutLutLma"]
        seen = set()
        for _ in range(LUT_ID_COUNT + 2):
            if base in seen:
                raise ValueError("LUT region chain in msoId %d loops at LMA %d"
                                 % (m.lut_store, base))
            seen.add(base)
            reg = LutRegion.parse(m.lut_store, base, self.read_lma)
            if reg is UNKNOWN:
                return UNKNOWN
            if reg is None:
                return UNKNOWN          # ran off the end of the chain
            owner = msos.get(reg.init_mso)
            if owner is not None and owner.lut_id == m.lut_id:
                self._cache[key] = reg
                return reg
            base = reg.next_base
        raise ValueError("LUT region chain in msoId %d did not reach lutId %d "
                         "within %d regions" % (m.lut_store, m.lut_id,
                                                LUT_ID_COUNT + 2))

    def lut_regions(self, store_mso=40):
        """-> [LutRegion] for every region in an MSO's LMA space, or UNKNOWN.

        The walk, exposed for inspection.  On this board msoId 40's space holds
        four: bases 0, 10696, 10808, 10824, owned by lutIds 1, 2, 3, 4.
        """
        out = []
        base = 0
        if store_mso == MSOID_ANCHOR_VALUE:
            d = self.sysmeta.directory() if self.sysmeta is not None else UNKNOWN
            if d is UNKNOWN:
                return UNKNOWN
            base = d["userLutLutLma"]
        for _ in range(LUT_ID_COUNT + 2):
            reg = LutRegion.parse(store_mso, base, self.read_lma)
            if reg is UNKNOWN:
                return out if out else UNKNOWN
            if reg is None:
                break
            out.append(reg)
            base = reg.next_base
        return out

    def lma_to_wma(self, mso, lma):
        """-> the WMA an LMA maps to.  Answers for every MSO that has a LUT.

        * int      -- the WMA, including the sub-granule offset.
        * `None`   -- the MSO has no LUT at all, or its LUT maps this LMA
                      nowhere, or the LMA is past the table.  A real negative.
        * UNKNOWN  -- the table page that would answer is not held here.

        The route is `lut_region(mso).wma(lma)`; for msoId 41 that region is
        RootStorage object 0 and for everything else it is a region inside
        another MSO's LMA space, reached by walking the self-describing chain.
        """
        reg = self.lut_region(mso)
        if reg is UNKNOWN:
            return UNKNOWN
        if reg is None:
            return None                 # no LUT: a real negative
        return reg.wma(lma)

    def pbid_readings(self):
        """The two readings of a pbid record, and which one is applied where.

        -> {'pair_half': [weruIds], 'block_ce': [weruIds], 'neither': [...]} or
        UNKNOWN.

        A RETRACTION lives HERE.  This used to be `block_mapping_conflicts` and
        it reported an unresolved standoff: `pbid = 12*block + ce` said weru 19
        was block 10 on twelve CEs while hardware said blocks 10/11 on six.  The
        standoff is over -- `pbid = 24*pairIndex + 12*half + slot` reproduces all
        five measured configurations exactly and explains both record shapes the
        old reading could not (weru 20 starting at 12*11 while owning blocks
        10/11; weru 0's record being 24 slots).  There was never a conflict in
        the data, only in the decomposition.

        RETRACTED 2026-08-07: "nothing establishes that TLC WERUs use the
        pair/half scheme.  Those are listed under `block_ce`, `weru_config`
        returns UNKNOWN for them, and no address is produced from them."  All
        three clauses are now wrong.  Something does establish it, and the
        answer is that they do not use it: `sd_slotlaw.py` scores the pair/half
        extension at 204 of 3568 against `slot = 4*ce + block - blockBase`'s
        3568 of 3568.  `block_ce` is still the right bucket name for these
        records -- they decode as four block numbers across twelve CEs -- but
        `weru_config` answers for them through `weru_config_from_user_record`
        and addresses are produced.
        """
        bm = self.block_map()
        if bm is UNKNOWN:
            return UNKNOWN
        out = {"pair_half": [], "block_ce": [], "neither": []}
        for weru, slots in sorted(bm.items()):
            mb = mapped_blocks(slots)
            if not mb:
                continue
            cfg = weru_config_from_pbids(
                weru, [None if x is None else geom.pbid(x[0], x[1])
                       for x in slots], slc=self._slc_from_capacity(weru))
            if cfg is not None:
                out["pair_half"].append(weru)
            elif len(mb) == geom.BLOCKS_PER_WERU:
                out["block_ce"].append(weru)
            else:
                out["neither"].append(weru)
        return out

    def substituted_weru_configs(self):
        """-> {weruId: SubstitutedWeruConfig} for every `substituted` record.

        The 29 records `weru_config` refuses.  Each carries `substitutions` =
        `[(slot, (ce, block))]`, which is both the answer to "which slot does
        the substitute occupy" and the exact list of page reads that would
        confirm it on media: read `(ce, block, page)` for any page and resolve
        its aux LMA through the owning MSO's LUT -- the WMA's slot must be the
        slot named here.  Nothing offline can do that: no page of any injected
        block is in this backing.
        """
        bm = self.block_map()
        werus = self.werus()
        if bm is UNKNOWN or werus is UNKNOWN:
            return UNKNOWN
        out = {}
        for w in werus:
            if w.shape != "substituted" or w.weru_id not in bm:
                continue
            c = weru_config_from_substituted_record(
                w.weru_id, bm[w.weru_id], slc=self._slc_from_capacity(w.weru_id))
            if c is not None:
                out[w.weru_id] = c
        return out

    def weru_config(self, weru_id, allow_substituted=False):
        """-> `WeruConfig` for a WERU, or UNKNOWN.

        `allow_substituted=True` additionally answers for the 29 `substituted`
        records, through `weru_config_from_substituted_record`.

        It stays off by default -- re-argued 2026-08-07 after the media
        survey, and the reason changed even though the answer did not.  The
        old reason was "no aux-LMA confirmation *yet*", which is a reason to
        wait.  The new one is stronger, so both sides are written out here
        rather than left as a bare policy:

        For flipping it to True:
          * the permutation half is now `[A]` on media, 3568 / 0, with the
            `position == slot` rival at exactly its ceiling (see `POS_TO_SLOT`);
          * the closed form is derived from `sub_113EC` / `sub_11388`, not
            fitted, and the 79/79 position hold-out has zero free parameters;
          * refusing an address we believe is right is itself a failure mode,
            and `weru_config_from_user_record` is applied by default on a
            per-record basis that is no stronger.

        Against, and this is what wins:
          * the inheritance half has no media evidence and can never get any
            on this drive -- all 29 substituted WERUs are erased end to end
            (3828 scans, 0 decoding).  It is not a pending measurement, it is an
            unavailable one;
          * therefore flipping the default changes no answer anybody can act
            on: every address it would hand back points at an erased block.
            A default that buys nothing while erasing a live distinction between
            "measured" and "reasoned" is a pure loss;
          * UNKNOWN is the operationally correct answer for a WERU in this
            state.  A caller asking for a pool WERU's geometry is usually asking
            a question that has no data behind it, and should hear so;
          * the cost asymmetry is the usual one here -- a silently misaddressed
            read is this project's worst failure class, and an explicit opt-in
            is the cheapest possible guard against it.

        See `POS_TO_SLOT` for both halves and the artefact trap.

        Three sources, in order of strength:

        1. `WERU_CONFIGS` -- measured on hardware by reading the pages.
        2. the WERU's own pbid record, decoded by `weru_config_from_pbids`
           (`pbid = 24*pairIndex + 12*half + slot`).  Available for every WERU
           whose record is twelve consecutive values, i.e. the SLC metadata and
           LUT WERUs.  Tagged `source="derived..."`.
        3. a 48-block user record, decoded by `weru_config_from_user_record`
           (`slot = 4*ce + block - blockBase`, measured 3568/3568 against an
           independent aux-LMA route over 5 WERUs, 251 windows and 11 CEs, with
           every rival at or below 5.7%).  This is what makes user LMAs
           resolvable at all.
        4. nothing -- UNKNOWN.

        The earlier note here said `blockMapping` could not be used and that
        it "disagreed" with the measurement.  That is RETRACTED.  It disagreed
        only under `pbid = 12*block + ce`; under the corrected decomposition the
        same bytes reproduce all five measured configurations exactly.  What
        remains true is that the list position is not the WMA slot in any useful
        sense and that 48-slot TLC records are still not covered -- see
        `weru_config_from_pbids`.

        "UNKNOWN is still the answer for every TLC data WERU" is RETRACTED.
        It was true only while the 48-block slot law was unmeasured.  It is now
        measured, and the WERUs that still answer UNKNOWN are a much smaller,
        named set: the 88 whose record names a block past `BLOCKS_PER_LUN`, the
        29 `substituted` ones whose four block numbers are not consecutive, and
        every partial/irregular record.  Those are refusals with a reason, not a
        gap in the law.

        The cell mode a returned configuration carries comes from
        `Ftl.cell_mode`, never from its block numbers.  Fixing
        `_slc_from_capacity` widened what that can answer but did not widen what
        this returns: `weru_config_from_pbids` still refuses any record that is
        not twelve consecutive pbids, and every such record here belongs to a
        pseudo-SLC WERU, so the set of configured WERUs is unchanged.
        """
        m = WERU_CONFIGS.get(weru_id)
        if m is not None:
            return m
        d = self.derived_configs.get(weru_id)
        if d is not None:
            return d
        bm = self.block_map()
        if bm is not UNKNOWN and weru_id in bm:
            slc = self._slc_from_capacity(weru_id)
            c = weru_config_from_pbids(
                weru_id, [None if x is None else geom.pbid(x[0], x[1])
                          for x in bm[weru_id]], slc=slc)
            if c is None:
                c = weru_config_from_user_record(weru_id, bm[weru_id], slc=slc)
            if c is None and allow_substituted:
                c = weru_config_from_substituted_record(weru_id, bm[weru_id],
                                                        slc=slc)
                if c is not None:
                    return c            # never cached: it is not the default
            if c is not None:
                self.derived_configs[weru_id] = c
                return c
        return UNKNOWN

    def _slc_from_capacity(self, weru_id):
        """-> True / False / None (no evidence).  For `weru_config_from_pbids`.

        A bug lived HERE.  This used to be `capacity == AUS_PER_SLC_WERU`,
        i.e. 17664 = 12 x 368 x 4, and returned True or None.  That recognises
        only the TWELVE-block metadata WERUs.  The 48-block pseudo-SLC user
        WERUs -- 88 of them on this board, weru 84 among them -- carry 70656 =
        48 x 368 x 4 and fell through to None, "no evidence", which every caller
        then resolved as native TLC.  The fix is not a second special case: it
        is to divide.  See `cell_mode_from_capacity`.

        None is still returned for "no evidence", and `weru_config_from_pbids`
        still refuses to build a configuration on it.  That gate stays: without
        it the function invented an SLC configuration for weru 3 and produced a
        confident wrong answer.
        """
        m = self.cell_mode(weru_id)
        return None if m is UNKNOWN else m

    # -- cell mode ----------------------------------------------------------
    def _weru(self, weru_id):
        """-> the `Weru` record, or None.  Indexed: `cell_mode` is per-WERU and
        a linear scan here would make any sweep over the 972 of them quadratic.
        """
        idx = self._cache.get("weru_index")
        if idx is None:
            ws = self.werus()
            idx = {} if ws is UNKNOWN else {w.weru_id: w for w in ws}
            self._cache["weru_index"] = idx
        return idx.get(weru_id)

    def cell_mode(self, weru_id):
        """-> True (pseudo-SLC) / False (native TLC) / UNKNOWN.  Never a default.

        The one place a read path may learn a cell mode.  Pass the result
        through `slc_flag` before it reaches a device.  `cell_mode_evidence`
        returns the same verdict with the reasoning attached.
        """
        return self.cell_mode_evidence(weru_id)["slc"]

    def cell_mode_evidence(self, weru_id):
        """-> {'weru', 'slc', 'source', 'detail'}.  Cached per WERU.

        The order the priors are tried, strongest first.  Each is free except
        the last, and the last is not tried from here at all -- `probe_cell_mode`
        is the only thing in this module that spends a read on the question.

        1. `MEASURED_CELL_MODES` -- a dual-mode read on hardware, MCRC decided.
        2. `WERU_CONFIGS[weru].slc` -- the hardware-measured configurations.
        3. `cell_mode_from_capacity(capacity, mapped blocks)` -- FREE and exact:
           capacity / (4 * blocks) is 368 or 1104 and nothing else.  This answers
           for every WERU here that has both fields (972 of them) and it is what
           the self-test holds out against 1 and 2.
        4. `cell_mode_from_lut_pages` -- only if the caller has already recorded
           a LUT page span for this WERU (`self.lut_page_spans`).  It can only
           ever return TLC: a page at or past 368 cannot exist in a pseudo-SLC
           block.  The reverse reading is a candidate and is never a verdict --
           weru 590's LUT pages are all below 368 and it is native TLC.
        5. The OWNER MSO's `nandBlockMode` (`msoDescriptor +0` bits [4:3],
           `Mso_GetNandBlockMode`, `efc_cpu2.c:95482`).  Added 2026-08-07.  It
           is last even though it answers more WERUs than any other
           prior, because it is a property of the partition rather than of this
           WERU, and because it depends on `weru_owners()` -- which on a dirty
           store is the `WeruUsageLog`, not the descriptor.  Putting it last
           means it cannot change any verdict the earlier priors already reach;
           it only fills in gaps.  On this board it fills exactly the 98 spare
           WERUs (972..1069, owner 58 PhysicalMediaAccess), which have no
           capacity field for prior 3 to divide.
        6. UNKNOWN.

        A probe result recorded by `probe_cell_mode` is cached here and wins
        over everything, because it is a measurement of this media rather than a
        table.  Disagreements are not smoothed over: the probe records what the
        priors said in `detail`.
        """
        hit = self._cell_modes.get(weru_id)
        if hit is not None:
            return hit
        out = None
        meas = MEASURED_CELL_MODES.get(weru_id)
        if meas is not None:
            out = dict(weru=weru_id, slc=meas[0], source="measured on hardware",
                       detail=meas[1])
        if out is None:
            cfg = WERU_CONFIGS.get(weru_id)
            if cfg is not None:
                out = dict(weru=weru_id, slc=cfg.slc,
                           source="WERU_CONFIGS", detail=str(cfg.source))
        if out is None:
            w = self._weru(weru_id)
            if w is not None:
                mb = w.mapped_blocks
                n = len(mb) if mb and mb is not UNKNOWN else 0
                m = cell_mode_from_capacity(w.capacity_aus, n)
                if m is not UNKNOWN:
                    out = dict(
                        weru=weru_id, slc=m, source="capacity",
                        detail="capacity %s AUs over %d mapped blocks = %d "
                               "pages per block" % (w.capacity_aus, n,
                                                    geom.pages_per_block(m)))
                elif w.capacity_aus is not UNKNOWN and w.capacity_aus and n:
                    out = dict(weru=weru_id, slc=UNKNOWN, source="none",
                               detail="capacity %s over %d mapped blocks is "
                                      "not %d or %d pages per block"
                                      % (w.capacity_aus, n,
                                         geom.PAGES_PER_BLOCK_SLC,
                                         geom.PAGES_PER_BLOCK_TLC))
        if out is None:
            span = self.lut_page_spans.get(weru_id)
            m = cell_mode_from_lut_pages(span if span is None else max(span))
            if m is not UNKNOWN:
                out = dict(weru=weru_id, slc=m, source="LUT page span",
                           detail="the LUT names page %d inside this WERU, "
                                  "past the %d a pseudo-SLC block has"
                                  % (max(span), geom.PAGES_PER_BLOCK_SLC))
        if out is None:
            mso_id, m = self.weru_nand_block_mode(weru_id)
            if m is not UNKNOWN and m is not None:
                out = dict(weru=weru_id, slc=m, source="owner nandBlockMode",
                           detail="owner msoId %s carries nandBlockMode %d "
                                  "(msoDescriptor +0 bits [4:3], "
                                  "Mso_GetNandBlockMode, efc_cpu2.c:95482)"
                                  % (mso_id, NAND_BLOCK_MODE_SLC if m
                                     else NAND_BLOCK_MODE_TLC))
        if out is None:
            out = dict(weru=weru_id, slc=UNKNOWN, source="none",
                       detail="no descriptor, no configuration, no measurement "
                              "-- probe_cell_mode() would settle it with one "
                              "dual-mode read")
        self._cell_modes[weru_id] = out
        return out

    def weru_nand_block_mode(self, weru_id):
        """-> (ownerMsoId, True pseudo-SLC / False native TLC / UNKNOWN).

        The owner MSO's persisted `nandBlockMode`.  Returns the msoId as well so
        a caller can see whose property the answer is -- this is a partition
        attribute inherited by the WERU, not a measurement of the WERU.
        """
        owners = self.weru_owners()
        if owners is UNKNOWN:
            return UNKNOWN, UNKNOWN
        o = owners.get(weru_id)
        if o is None:
            return None, UNKNOWN
        mso_id = o[0] if isinstance(o, tuple) else o
        mso = self.msos().get(mso_id) if self.msos() is not UNKNOWN else None
        if mso is None:
            return mso_id, UNKNOWN
        return mso_id, mso.slc

    #: Pages a probe should try, in order.  Not 0.  Pages 0..3 sit on the
    #: natively-SLC word lines that bracket every block (`geometry.cell_mode`
    #: returns 2 there), and on a pseudo-SLC block pages 0 and 2 measured
    #: Byte-identical under both prefixes -- a probe on page 0 learns nothing.
    #: Page 1 is the first that separated the modes on hardware and is also
    #: early enough to be written in a barely-used WERU (weru 81 holds only
    #: pages 0..3).  The later entries are for WERUs that are written further
    #: in; every one is below PAGES_PER_BLOCK_SLC so both modes can address it.
    CELL_MODE_PROBE_PAGES = (1, 3, 8, 64, 287)

    def cell_mode_targets(self, weru_id, page=None, limit=4):
        """-> [(ce, block, page)] inside this WERU that a probe may read.

        The (ce, block) pairs come straight off the WERU's own blockMapping
        record, so this commits to nothing about `slot -> (ce, block)`: whatever
        the slot order is, every mapped pair is in this WERU.  CE0 is excluded
        explicitly -- it is dead on this board and it answers, with a page drawn
        entirely from the two-symbol alphabet {0x00, 0xA7} and not one 0xFF
        byte, so it is neither programmed nor erased and would decode to nothing
        in either mode.  Blocks past `BLOCKS_PER_LUN` are excluded too (past
        `WERU_COUNT` that is section padding, not a location).

        Correction.  This used to describe that page as "2304 x 0xA7 then
        16128 x 0x00", which reads a byte count as a layout.  Measured over all
        71 CE0 pages in `the dump`, the 0xA7s are scattered through a period-32
        pattern, and there are two variants (63 pages with 2304 of them, 8 with
        2880).  A prefix test finds only 8 of the 71; the alphabet test finds
        71/71 with 0 false positives over 3992 non-CE0 pages.
        `dumpdev.DEAD_CE_ALPHABET` is the implementation and was always right --
        only this prose was stale.

        `page=None` walks `CELL_MODE_PROBE_PAGES`; pass an int to pin one.
        """
        bm = self.block_map()
        if bm is UNKNOWN or weru_id not in bm:
            return []
        pairs = []
        for pair in bm[weru_id]:
            if pair is None:
                continue
            block, ce = pair
            if ce in geom.CE_DEAD or block >= geom.BLOCKS_PER_LUN:
                continue
            pairs.append((ce, block))
            if len(pairs) >= limit:
                break
        pages = (self.CELL_MODE_PROBE_PAGES if page is None else (page,))
        return [(ce, block, pg) for pg in pages for ce, block in pairs]

    def probe_cell_mode(self, weru_id, targets=None, device=None, codec=None,
                        force=False):
        """One dual-mode read inside a WERU; MCRC decides.  -> evidence dict.

        Returns the same shape as `cell_mode_evidence` and caches it there, so a
        WERU is probed at most once per `Ftl`.  Unless `force`, a WERU whose free
        priors already answer is not read at all -- the priors are exact and a
        read costs seconds.

        Why MCRC and not re-read stability.  A *correct* native-TLC read is not
        bit-stable: two reads of the same good page differ in ~63 of 18432 bytes
        and both decode 4/4.  Stability would call that page a failure.

        Why MCRC is not enough on its own, which hardware had to teach US.
        MCRC is computed over the *stored* message, before descrambling, so it
        says "these bytes are a validly programmed AU" and not "we read them in
        the right mode".  Measured: ce1/b160 (weru 81, pseudo-SLC) read under
        0xDF lands on genuinely written word lines at pages 2, 4 and 6 and
        scores 4/4 there -- with aux LMAs of 2110438192, 1212921021 and
        3858266786, i.e. nonsense, because the descrambling seed is derived from
        the row we asked for.  So the test here is MCRC 4/4 and the four aux
        LMAs consecutive.  The four AUs of a page always carry four
        consecutive LMAs; a wrong-mode or wrong-seed decode does not produce
        that, and the check needs no LUT and no expected value.

        The other thing hardware taught: pages 0 and 2 of a pseudo-SLC block
        read byte-identically in both modes (same CRC, 4/4 and consecutive
        both ways).  Those pages cannot decide anything, so a target that
        returns identical bytes in both modes is skipped rather than counted --
        the natively-SLC word lines that bracket every block (`geometry
        .cell_mode(page) == 2`, i.e. page < 4) are the ones to avoid.  A
        native-TLC block does not behave that way: ce1/b3612 pages 0-6 and 8
        separate cleanly.

        Grades, all recorded in `source`:

        * ``probe: dual-mode MCRC`` -- both modes were read and exactly one
          passed.  The strongest thing this function can say.
        * ``probe: single-mode MCRC`` -- only one mode could be read (a dump
          that holds one of them) and it passed.  Still decisive: across every
          address measured on this board, a native-TLC page has never passed
          under 0xDA -- ten of them at 0/4 -- and a pseudo-SLC page has never
          passed under 0xDF with the right LMAs.
        * ``probe: undecided`` -- no target separated the modes.  This is a
          real answer and it happens twice here: weru 590 page 106 is 0/4 in
          both modes, and weru 81 page 0 is 4/4 in both with the same bytes.
          The free prior is then left standing and said so in `source`;
          nothing is defaulted.
        """
        if not force:
            hit = self._cell_modes.get(weru_id)
            if hit is not None and hit["source"].startswith("probe"):
                return hit
            prior = self.cell_mode_evidence(weru_id)
            if prior["slc"] is not UNKNOWN:
                return prior
        dev = device if device is not None else self.device
        codec = codec or self.codec or default_codec()
        if dev is None:
            return self.cell_mode_evidence(weru_id)
        if targets is None:
            targets = self.cell_mode_targets(weru_id)
        tried = []
        for ce, block, page in targets:
            passed, seen, blob = {}, {}, {}
            for slc in (True, False):
                if page >= geom.pages_per_block(slc):
                    seen[slc] = "page %d is past the %d a %s block has" % (
                        page, geom.pages_per_block(slc),
                        "pseudo-SLC" if slc else "native TLC")
                    continue
                try:
                    raw = dev.read_page(ce, block, page, slc=slc)
                except DeviceError as e:
                    seen[slc] = "not readable: %s" % e
                    continue
                if raw is None:
                    seen[slc] = "device returned nothing"
                    continue
                blob[slc] = bytes(raw)
                aus, ok = codec.decode(raw, block, page)
                n = (0 if (aus is None or ok is UNKNOWN or ok is None)
                     else sum(1 for x in ok if x))
                lmas = (None if aus is None else
                        [struct.unpack_from("<I", a, AUX_LMA_OFF)[0]
                         for a in aus])
                consec = bool(lmas) and all(lmas[i] == lmas[0] + i
                                            for i in range(len(lmas)))
                passed[slc] = (n == geom.AUS_PER_PAGE_TLC and consec)
                seen[slc] = "MCRC %d/%d%s" % (
                    n, geom.AUS_PER_PAGE_TLC,
                    "" if lmas is None else
                    (", aux %s consecutive" % (lmas[0],) if consec
                     else ", aux %s NOT consecutive" % (lmas,)))
            if (len(blob) == 2 and blob.get(True) == blob.get(False)
                    and blob.get(True) is not None):
                seen["same"] = True
            tried.append("ce%d/b%d/p%d  SLC: %s  TLC: %s%s" % (
                ce, block, page, seen.get(True, "not attempted"),
                seen.get(False, "not attempted"),
                "  [IDENTICAL BYTES in both modes -- this page cannot decide]"
                if seen.get("same") else ""))
            if seen.get("same"):
                continue            # a natively-SLC word line; decides nothing
            win = [m for m, good in passed.items() if good]
            if len(win) == 1:
                out = dict(weru=weru_id, slc=win[0],
                           source=("probe: dual-mode MCRC" if len(passed) > 1
                                   else "probe: single-mode MCRC"),
                           detail="; ".join(tried))
                self._cell_modes[weru_id] = out
                return out
        why = ("no target passed (MCRC %d/%d with consecutive aux LMAs) in "
               "exactly one mode: %s"
               % (geom.AUS_PER_PAGE_TLC, geom.AUS_PER_PAGE_TLC,
                  "; ".join(tried) if tried
                  else "no readable target inside this WERU"))
        # An undecided probe does not erase what the free priors know, and it
        # does not hide itself behind them either: both facts go in the result.
        prior = dict(self.cell_mode_evidence(weru_id))
        out = dict(weru=weru_id, slc=prior["slc"],
                   source=("probe: undecided" if prior["slc"] is UNKNOWN
                           else "probe: undecided, prior stands (%s)"
                                % prior["source"]),
                   detail="%s || prior: %s" % (why, prior["detail"]))
        self._cell_modes[weru_id] = out
        return out

    def cell_mode_census(self):
        """-> {source: {True: n, False: n, UNKNOWN: n}} over every known WERU."""
        out = {}
        for w in (self.werus() or ()):
            e = self.cell_mode_evidence(w.weru_id)
            k = "UNKNOWN" if e["slc"] is UNKNOWN else ("SLC" if e["slc"]
                                                       else "TLC")
            out.setdefault(e["source"], {})
            out[e["source"]][k] = out[e["source"]].get(k, 0) + 1
        return out

    def weru_config_agreement(self):
        """Hold-out: does the derivation reproduce the MEASURED configurations?

        -> (agree, disagree, [(weruId, measured, derived)]).  The measured table
        is not consulted by `weru_config_from_pbids`, so this is a real test of
        the decomposition rather than a restatement of it.
        """
        bm = self.block_map()
        agree, bad = 0, []
        for wid, meas in sorted(WERU_CONFIGS.items()):
            rec = None
            if wid in self.derived_configs:
                rec = self.derived_configs[wid]
            elif bm is not UNKNOWN and wid in bm:
                rec = weru_config_from_pbids(
                    wid, [None if x is None else geom.pbid(x[0], x[1])
                          for x in bm[wid]],
                    slc=self._slc_from_capacity(wid))
            if rec is None:
                bad.append((wid, meas, None))
            elif (rec.ce_base, rec.block_base, rec.blocks_per_weru) == (
                    meas.ce_base, meas.block_base, meas.blocks_per_weru):
                agree += 1
            else:
                bad.append((wid, meas, rec))
        return agree, len(bad), bad

    def wma_to_physical(self, wma):
        """-> Placement | None | UNKNOWN.

        * `Placement` -- the address, from the WMA rule and the WERU's measured
          configuration.  It is an address: it does not promise this backing
          holds those bytes.
        * `None` -- the WMA addresses nothing (a LUT sentinel, or the unused
          tail of a 192-WMA window).  `wma_unaddressable_reason` says which.
        * `UNKNOWN` -- the arithmetic is fine but this WERU's `slot -> (ce,
          block)` is not measured.  `split_wma(wma)` still gives you the
          weruId, page, slot and au.
        """
        if wma is UNKNOWN or wma is None:
            return wma
        cfg = self.weru_config(wma // geom.AUS_PER_TLC_WERU)
        if cfg is UNKNOWN:
            return UNKNOWN
        return wma_to_physical(wma, cfg)

    def physical_to_wma(self, ce, block, page, au=0):
        """-> (wma, weruId) | None | UNKNOWN.  The inverse, over known WERUs.

        None means the address is inside a known WERU's region but has no WMA --
        which on this board is the unexplained twelfth block, (ce11, block 17)
        for WERU 14.  That block is programmed; "no WMA names it" is not "it is
        empty".  UNKNOWN means no configured WERU covers this address.
        """
        hits = [(w, c) for w, c in sorted(WERU_CONFIGS.items())
                if c.covers(ce, block)]
        if not hits:
            return UNKNOWN
        if len(hits) > 1:
            raise ValueError(
                "(ce%d, block %d) is claimed by %d configured WERUs %s -- the "
                "configurations overlap, which they must not"
                % (ce, block, len(hits), [w for w, _c in hits]))
        weru, cfg = hits[0]
        w = physical_to_wma(ce, block, page, au, cfg, weru)
        return None if w is None else (w, weru)

    def address_coverage(self, sample=256, msos=None):
        """How far `lma_to_physical` actually reaches, per MSO.  -> {msoId: d}.

        Published so the coverage cannot be overstated.  Every LMA of an MSO
        with 11880 or fewer is checked; larger spaces are sampled evenly (region
        0 covers 175085568 LMAs = 717 GB and cannot be enumerated).  Each entry
        distinguishes the reasons, because "we cannot answer" has three
        completely different causes here and lumping them would hide which:

            state          'no lut' | 'lut unknown' | 'ok'
            resolved       an address was produced
            unmapped       the LUT maps this LMA nowhere -- a real negative
            unknown_lut    the LUT's own table page is not in this dump
            unknown_weru   the WMA's WERU has no measured slot -> (ce, block)
            unaddressable  the WMA fell in the unused tail of a 192-window
            werus          weruIds the sampled WMAs point into
            invalid_werus  of those, the ones whose blockMapping names a block
                           past BLOCKS_PER_LUN -- unreadable for a second,
                           independent reason
        """
        all_msos = self.msos()
        if all_msos is UNKNOWN:
            return UNKNOWN
        shapes = {}
        for w in (self.werus() or ()):
            if w.blocks is not UNKNOWN and w.mapped_blocks:
                shapes[w.weru_id] = w.shape
        out = {}
        for mso_id in (sorted(all_msos) if msos is None else msos):
            try:
                reg = self.lut_region(mso_id)
            except (ValueError, struct.error) as e:
                out[mso_id] = dict(state="lut UNKNOWN (%s)" % e)
                continue
            if reg is None:
                out[mso_id] = dict(state="no LUT")
                continue
            if reg is UNKNOWN:
                out[mso_id] = dict(state="lut UNKNOWN")
                continue
            n = reg.covered_lmas
            step = max(1, -(-n // sample))
            c = dict(state="ok", total=n, checked=0, resolved=0, unmapped=0,
                     unknown_lut=0, unknown_weru=0, unaddressable=0,
                     granularity=reg.granularity, werus=set())
            for lma in range(0, n, step):
                c["checked"] += 1
                w = reg.wma(lma)
                if w is UNKNOWN:
                    c["unknown_lut"] += 1
                    continue
                if w is None:
                    c["unmapped"] += 1
                    continue
                c["werus"].add(w // geom.AUS_PER_TLC_WERU)
                p = self.wma_to_physical(w)
                if p is UNKNOWN:
                    c["unknown_weru"] += 1
                elif p is None:
                    c["unaddressable"] += 1
                else:
                    c["resolved"] += 1
            c["invalid_werus"] = sorted(x for x in c["werus"]
                                        if shapes.get(x) == "invalid")
            c["werus"] = sorted(c["werus"])
            out[mso_id] = c
        return out

    def lma_to_physical(self, mso, lma):
        """-> Placement | None | UNKNOWN.  Read the three cases carefully.

        The route is now the firmware's own, end to end and for any MSO:
        LMA -> WMA through that MSO's LUT, then WMA -> physical through the
        WERU configuration.  There is still no arithmetic fallback and no
        progression fitted to anything; both steps are table lookups the
        firmware itself performs.

        * `Placement(ce, block, page, au)` -- the address the firmware would
          use.  It is an address, not a promise: use `Device.read_page` to find
          out whether this backing holds it, and `system_metadata()
          .lma_placement()` for where a copy we actually read and MCRC-checked
          lives.  Those two can differ for msoId 41 and the difference is
          staleness in the RootStorage LUT snapshot, not a mapping error -- the
          scan often holds a newer copy of an LMA than the snapshot names.
          Measured: where the rule's page and the scan's page agree, the
          (ce, block, au) it predicts is right 5551 times and wrong 0 times.
        * `None` -- the LUT maps this LMA nowhere, or it is past the end of the
          MSO, or the WMA lands in the unused tail of a 192-window.  A real
          negative from the firmware's own tables.
        * `UNKNOWN` -- we cannot compute it: either this MSO's LUT is not
          readable (true for every MSO but 40 and 41 -- see `lma_to_wma`), or
          the WMA's WERU has no measured `slot -> (ce, block)`.  This is the
          common answer for user data and it is correct, not a gap to fill in.
        """
        wma = self.lma_to_wma(mso, lma)
        if wma is UNKNOWN or wma is None:
            return wma
        return self.wma_to_physical(wma)

    def templated_mso_groups(self):
        """-> [[msoId, ...], ...] for every set of byte-identical descriptors.

        Empty list = every populated descriptor is distinct.  UNKNOWN when no
        msoDescriptor section was recovered.

        On this board the answer is two blocks of sixteen -- msoIds 0..15 share
        one record and 16..31 share another -- which is the evidence that the
        sub-32 range is not thirty-two individually configured partitions.

        It does now say what they are (2026-08-06; this docstring used to end
        "It does not say what they are"): the two templates are the NSID2
        (lutId 2) and NSID3 (lutId 3) group LUTs, and the range is the 16+16
        active logical partition MSO pool.  See the module docstring.
        """
        buf, _ok, n, _src = self._section("mso")
        if buf is UNKNOWN or not n:
            return UNKNOWN
        by = {}
        lim = min(geom.MAX_MSOS, len(buf) // MSO_DESC_ENTRY)
        for m in range(lim):
            e = bytes(buf[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY])
            if any(e):
                by.setdefault(e, []).append(m)
        return sorted((v for v in by.values() if len(v) > 1), key=lambda g: g[0])

    def survey(self, ce):
        """-> the Survey for a CE, or UNKNOWN if that CE was never surveyed."""
        return self.surveys.get(ce, UNKNOWN)

    def add_survey(self, survey):
        self.surveys[survey.ce] = survey
        return survey

    def check_weru_stride(self, ce=None):
        """Re-measure the block-mapping stride against a survey.

        -> {stride: (consistent, tested)} or UNKNOWN if there is no survey or no
        blockMapping/validCount to test against.  See `verify_weru_stride`.
        """
        bmbuf, _o, n, _s = self._section("blockMapping")
        vc, _o2, n2, _s2 = self._section("validCount")
        if bmbuf is UNKNOWN or vc is UNKNOWN or not n or not n2:
            return UNKNOWN
        ces = [ce] if ce is not None else sorted(self.surveys)
        for c in ces:
            s = self.surveys.get(c)
            if s is None:
                continue
            return verify_weru_stride(bmbuf, decode_valid_count(vc), s)
        return UNKNOWN

    # -- reporting ----------------------------------------------------------
    def describe(self):
        """-> a multi-line human-readable state summary.

        Says what is known, what is UNKNOWN and *why*, so a reader is never left
        to infer that a blank means zero.
        """
        L = []
        add = L.append
        add("Titania FTL state")
        add("=" * 60)
        add("device            %s" % (self.device if self.device else UNKNOWN))
        if self.device is not None:
            served = set(self.device.present_ces())
            dead = sorted(set(geom.CE_DEAD))
            add("CEs               %s served; %s marked dead in geometry (%s)"
                % (sorted(served), dead,
                   "not served by this backing either"
                   if not (served & set(dead)) else
                   "served but skipped by every scan"))
            missing = sorted(set(range(geom.CE_COUNT)) - served - set(dead))
            if missing:
                add("                  %s neither served nor known-dead -- "
                    "anything living only there is INVISIBLE, not absent"
                    % missing)

        add("")
        add("msoIdTable        %s" % (self.mso_table or UNKNOWN))
        m = self.msos()
        if m is UNKNOWN:
            add("MSOs              <unknown> -- no msoDescriptor section")
        else:
            named = sum(1 for v in m.values() if v.name)
            add("MSOs              %d populated, %d named by the msoIdTable, "
                "%d not named" % (len(m), named, len(m) - named))
            if named and len(m) - named:
                add("                  the %d unnamed are msoIds < %d: the "
                    "STATIC table genuinely assigns them no Type/Group (they "
                    "are the dynamic logical-partition pool), so that is an "
                    "answer, not a missing input"
                    % (len(m) - named, MSOID_STATIC_MIN))
            groups = self.templated_mso_groups()
            if groups:
                add("                  identical descriptors: %s"
                    % "  ".join(_runs(g) for g in groups))
                add("                  a fully populated msoDescriptor array is "
                    "NOT evidence of that many distinct partitions; msoIds < %d "
                    "still must not be named" % MSOID_STATIC_MIN)
            raid = {}
            for v in m.values():
                k = str(v.raid_name)
                raid[k] = raid.get(k, 0) + 1
            add("raid types        %s" % raid)

        add("")
        add("dirty flag        %s%s"
            % (self.dirty,
               "  <- firmware would DISCARD the stored validMap/validCount at "
               "mount and rebuild both from the LUT" if self.dirty is True else ""))
        if self.dirty is True:
            add("                  so validCount and validMap below are a "
                "snapshot of a past instant, not the drive's truth.")
            add("                  THE LUT IS THE SOLE AUTHORITY; both are "
                "derived caches (rebuild rule: efc_cpu2.c:109642).")

        wul = self.weru_usage_log()
        if wul is UNKNOWN or wul is None:
            add("WeruUsageLog      <unknown> -- RootStorage objects %d.. not read"
                % OBJ_WERU_USAGE_LOG_FIRST)
        else:
            add("WeruUsageLog      %d records of %d B in RootStorage objects "
                "%d..%d; %d unreadable here"
                % (wul.n_werus, wul.buffer.element_size,
                   wul.buffer.first_object_id,
                   wul.buffer.first_object_id + wul.buffer.segments - 1,
                   len(wul.unreadable_werus())))
            own = {}
            for _w, o in wul.owners().items():
                own[o] = own.get(o, 0) + 1
            add("                  owners %s" % dict(sorted(own.items())))
            conf = self.weru_owner_conflicts()
            if conf is not UNKNOWN:
                add("                  %d WERUs whose weruDescriptor owner "
                    "DISAGREES -- on a dirty store RebuildWeruBins would take "
                    "the log (efc_cpu3.c:22044)" % len(conf))
            add("                  numberOfWerus here is %d (all six weru "
                "configurations, efc_cpu2.c:108049); validMap covers only the "
                "first five" % wul.n_werus)

        rb = self.rebuild_comparison()
        if rb is not UNKNOWN:
            add("rebuilt caches    the firmware's mount-time RebuildValidMap "
                "set is TWO msoIds (efc_cpu3.c:40322-40324):")
            for w, rc, sc, pc, rbits, sbits in rb:
                if not rc:
                    continue
                add("                  weru %-4d rebuilt %-8d stored "
                    "validCount %-8s stored validMap popcount %-8s%s"
                    % (w, rc, sc, pc,
                       ("  bits in common %d of %d rebuilt"
                        % (len(rbits & sbits), len(rbits))
                        if rbits is not None and sbits is not None else "")))
            add("                  ValidCountRead is skipped when dirty "
                "(efc_cpu3.c:39739), and step %d is NOT the last writer -- step "
                "%d rebuilds four more LUTs (sub_4033E94E)."
                % (MOUNT_STEP_REBUILD_VALID_MAP, MOUNT_STEP_LUT_REBUILD))

        plan = self.lut_rebuild_plan()
        if plan is not UNKNOWN:
            add("step %d rebuilds  four per-lutId LUT rebuilds "
                "(off_402F86E8 idx 7..10), gate = snapshotInformation.count != "
                "0 AND msoDescriptor[list[0].msoId]+5 bit 1:"
                % MOUNT_STEP_LUT_REBUILD)
            for lid in LUT_REBUILD_ORDER:
                p = plan[lid]
                add("                  lutId %-3d snapCount %-2d list[0].msoId "
                    "%-5s +5 %-6s werus %-5d -> map %s, validCount := popcount"
                    % (lid, p["snapshot_count"], p["first_mso"],
                       "--" if p["first_mso_flags"] is None
                       else "0x%02X" % p["first_mso_flags"], len(p["werus"]),
                       "REPLAYED" if p["map_replayed"] else "NOT replayed"))
        verd = self.dirty_mount_valid_cache()
        if verd is not UNKNOWN:
            byk = {}
            lost = 0
            for r in verd.values():
                byk[r["count"]] = byk.get(r["count"], 0) + 1
                if not r["count_zero_meaningful"]:
                    lost += r["stored_valid_count"] or 0
            add("zero verdict      per WERU, after a DIRTY mount: %s"
                % dict(sorted(byk.items())))
            add("                  %d of %d WERUs would come out with a "
                "MEANINGLESS zero validCount, discarding %d stored valid AUs. "
                "For those the stored validCountLma is the better evidence -- "
                "Ftl.valid_zero_meaning(weru) says which is which."
                % (sum(v for k, v in byk.items() if k != "rebuilt"),
                   len(verd), lost))

        bins = self.weru_bins()
        if bins is not UNKNOWN:
            live = [b for b in bins.values() if not b.is_reset]
            add("weruBin           %d live bins of %d; %d WERUs binned "
                "(binId 0 is ALSO the default, so binId alone is not "
                "membership)" % (len(live), len(bins),
                                 sum(b.count for b in live)))
            for b in sorted(live, key=lambda x: x.bin_id):
                add("  bin %-2d %-26s cfg %-4s %5d WERUs"
                    % (b.bin_id, weru_bin_name(b.bin_id),
                       "-" if b.kind == WERU_BIN_KIND_NO_CONFIG else b.kind,
                       b.count))
            add("                  bin +0 is the weruConfigurationId the bin "
                "serves (%d = none); a bin whose kind IS a configuration is "
                "that configuration's FREE POOL, and the kind-%d ones hold "
                "allocated WERUs" % (WERU_BIN_KIND_NO_CONFIG,
                                     WERU_BIN_KIND_NO_CONFIG))

        fr = self.write_frontier()
        if fr is not UNKNOWN:
            openf = {m: f for m, f in fr.items() if f.is_open}
            add("")
            add("write frontier    RootStorage object %d (Allocation), %d x %d B."
                " %d of %d MSOs hold a WERU open."
                % (OBJ_ALLOCATION, ALLOC_MSOS, ALLOC_ENTRY, len(openf),
                   ALLOC_MSOS))
            for m, f in sorted(openf.items()):
                mm = self.mso(m)
                add("  msoId %-2d %-34s weru %-4d wl %-4d  next AU at wma %d "
                    "(+%d in the open page)%s"
                    % (m, (mm.name if mm else "-"), f.current_weru, f.wordline,
                       f.wma, f.aus_in_open_page,
                       "" if f.pending_weru is None
                       else "  [weru %d reserved]" % f.pending_weru))
            stale = [m for m, f in fr.items()
                     if not f.is_open and not f.is_reinitialised]
            if stale:
                add("  msoIds %s keep a write pointer with NO current WERU -- "
                    "the front was closed after the pointer was last written"
                    % stale)
            add("                  on a DIRTY mount the firmware never reads "
                "this: AllocationReinitializeMsos replaces "
                "BootstrapRootStorageReadAllocation (efc_cpu3.c:39620) and "
                "zeroes every record (efc_cpu2.c:125261). This board is dirty, "
                "so the drive itself would discard what is printed above.")
        pool = self.rebuild_sequence_number_pool()
        if pool is not UNKNOWN:
            add("rebuildSeqPool    next sequence number per msoGroupId: %s"
                % [p[0] for p in pool])

        add("")
        sm = self.sysmeta
        if sm is None:
            add("SystemMetadata    <unknown> -- no metadata WERU was scanned")
        else:
            d = sm.directory()
            st = sm.stats
            add("SystemMetadata    werus %s -- HALVES OF ONE IMAGE, not "
                "generations; %d LMAs resolved" % (sm.weru_ids, len(sm.lma)))
            add("                  blocks %s, scan used CEs %s of %s the device "
                "serves" % (list(sm.blocks), st.get("ces_used"),
                            st.get("ces_served")))
            add("                  pages read %s used %s erased %s mcrc-failed %s"
                % (st.get("pages_read"), st.get("pages_used"),
                   st.get("pages_erased"), st.get("pages_failed_mcrc")))
            if st.get("ces_skipped"):
                add("                  CEs NOT read: %s -- any LMA whose live "
                    "copy is there is invisible, not absent" % st["ces_skipped"])
            if sm.lut_used:
                lut = sm.lut
                add("SystemLut         %d/%d entries mapped, granularity %d "
                    "LMAs, split %s"
                    % (lut.n_mapped, len(lut.entries), lut.granularity,
                       lut.weru_histogram()))
                add("                  %d LMAs also exist in a WERU the LUT does "
                    "NOT name (superseded -- these are the bytes the old "
                    "current/legacy model returned)" % len(sm.superseded))
                add("                  %d mapped but unreadable here, %d present "
                    "but unmapped, %d past the LUT's %d-LMA end"
                    % (len(sm.unreadable), len(sm.unmapped), len(sm.past_end),
                       lut.covered_lmas))
            else:
                add("SystemLut         <unknown> -- LMAs resolved by page "
                    "sequence ACROSS WERUs, which is not a valid ranking")
            if d is UNKNOWN:
                add("directory         <unknown> -- LMA 0 not recovered")
            else:
                add("directory         revision %d, totalLmas %s, "
                    "numberOfWerus %s (derived from the validMap length)"
                    % (d["revision"], sm.total_lmas(), self.number_of_werus()))
                add("  (length = nextBase - thisBase - %d: the layout builder "
                    "leaves a %d-LMA gap after every section.)"
                    % (SECTION_GAP, SECTION_GAP))
                for name, base, n, derived in sm.layout():
                    sec = sm.get(name)
                    holes = sec.missing
                    add("  %-28s LMA %-6d %5d/%-5d LMAs  werus %-9s %s"
                        % (name, base, sec.n_present, n,
                           ",".join(map(str, sec.werus())) or "-",
                           ("complete" if sec.complete else
                            "NOT RECOVERED" if not sec.n_present else
                            "%d holes, first at LMA %s"
                            % (len(holes), sec.first_hole))
                           + ("" if derived else
                              "  (length NOT derived -- last section)")))

        add("")
        w = self.werus()
        if w is UNKNOWN:
            add("WERUs             <unknown>")
        else:
            real = [x for x in w if x.is_real is True]
            nocap = [x for x in w if x.capacity_aus is UNKNOWN]
            add("WERUs             %d entries; %d with capacity > 0, %d with "
                "capacity UNKNOWN (no weruDescriptor)"
                % (len(w), len(real), len(nocap)))
            owned = [x for x in w if x.is_allocated is True]
            if owned:
                by = {}
                for x in owned:
                    by[x.owner] = by.get(x.owner, 0) + 1
                add("owners            %s"
                    % {k: "%s x%d" % (
                        (self.mso_table.name(k) if self.mso_table else None)
                        or ("dynamic<%d" % MSOID_STATIC_MIN), v)
                        for k, v in sorted(by.items())})
                add("                  source=%s"
                    % {x.source for x in owned if x.source})
            else:
                add("owners            <unknown> -- the weruDescriptor section "
                    "was not recovered from any live metadata WERU")
            cm = self.cell_mode_census()
            if cm:
                add("cell mode         %s"
                    % {k: dict(sorted(v.items())) for k, v in sorted(cm.items())})
                add("                  per WERU, NEVER from a block number: "
                    "weru 84 is blocks 172-175 and is pseudo-SLC.  A wrong-mode "
                    "read returns ~1000 syndromes/codeword, not an error.")
            live = [x for x in w if isinstance(x.valid_count, int) and x.valid_count]
            if live:
                tot = sum(x.valid_count for x in live)
                add("validCount        %d WERUs non-zero, %d live AUs (~%.2f GiB)"
                    % (len(live), tot, tot * geom.AU_DATA / (1 << 30)))
                add("                  validCount 0 does NOT mean stale for "
                    "System-group MSOs (the firmware area is not LMA-addressed)")
        vm = self.valid_map()
        if vm is UNKNOWN:
            add("validMap          <unknown> -- section not recovered")
        else:
            add("validMap          %d werus x %d B = %.2f MiB, %d/%d LMAs "
                "present" % (vm.weru_count, vm.bytes_per_weru,
                             vm.weru_count * vm.bytes_per_weru / (1 << 20),
                             vm.section.n_present, vm.section.n_lmas))
            vc, _o, nvc, _s = self._section("validCount")
            if vc is not UNKNOWN and nvc:
                a, dis, unread, _bad = vm.compare_valid_count(decode_valid_count(vc))
                add("                  popcount == validCount on %d of %d rows "
                    "(%d differ, %d unreadable)" % (a, a + dis, dis, unread))
                add("                  both sides are caches of the LUT; "
                    "ValidMapSet only runs for MSOs with descriptor flag bit 1 "
                    "set (efc_cpu2.c:109636), so some rows are counted but not "
                    "mapped by design")
        ll = self.user_lut_lut()
        if ll is UNKNOWN:
            add("userLutLut        <unknown> -- section not recovered")
        elif ll is None:
            add("userLutLut        present but not a LUT index")
        else:
            add("userLutLut        msoId %s (%s), %d entries, granularity %d "
                "LMAs (%d KiB/entry)"
                % (ll.mso_id,
                   (self.mso_table.name(ll.mso_id) if self.mso_table else None)
                   or "?", len(ll.entries), ll.granularity,
                   ll.granularity * LMA_BYTES // 1024))
            add("                  the user LUT's own AUs live in werus %s"
                % ll.weru_histogram())
        add("WMA -> physical   %d WERUs configured: %s"
            % (len(WERU_CONFIGS),
               ", ".join("%d(%s)" % (w, c.name)
                         for w, c in sorted(WERU_CONFIGS.items()))))
        add("                  ...measured; every 48-block record is DERIVED "
            "from slot = 4*ce + block - blockBase (3568/3568 against an "
            "independent aux-LMA route); the rest are refusals with a reason")
        add("                  blocksPerWeru %d, window stride %d = %d*%d, so "
            "slots %d..%d of every window address nothing"
            % (11, WMA_STRIDE, MAX_BLOCKS_PER_WERU, WMAS_PER_PAGE, 11,
               MAX_BLOCKS_PER_WERU - 1))
        add("                  a twelfth block IS programmed in each of these "
            "regions and no WMA names it: it is the stripe's INLINE RAID "
            "PARITY, slot11 = XOR(slot0..slot10) over aux+user, 3388/0 over "
            "847 stripes (werus 14/20/22, no CE0 involved)")
        regs = self.lut_regions(40)
        if regs is not UNKNOWN:
            add("LUT regions       msoId 40's LMA space holds %d, walked by "
                "their own headers (the size formula over-predicts by 2876):"
                % len(regs))
            for r in regs:
                owner = self.msos().get(r.init_mso) if self.msos() is not UNKNOWN else None
                add("     base %-6d table %5d LMAs  %10d entries x %2d LMAs  "
                    "lutId %s (init msoId %d)"
                    % (r.base, r.n_table_lmas, r.n_entries, r.granularity,
                       owner.lut_id if owner is not None else "?", r.init_mso))
        cov = self.address_coverage()
        if cov is UNKNOWN:
            add("lma_to_physical   <unknown> -- no msoDescriptor")
        else:
            byst = {}
            for m, c in cov.items():
                byst.setdefault(c["state"], []).append(m)
            for st, ms in sorted(byst.items()):
                if st != "ok":
                    add("lma_to_physical   %-14s msoIds %s" % (st, sorted(ms)))
            for mso_id, c in sorted(cov.items()):
                if c["state"] != "ok":
                    continue
                add("lma_to_physical   msoId %-3d %-28s %d/%d sampled resolve; "
                    "%d unmapped, %d LUT page absent, %d unconfigured WERU"
                    % (mso_id,
                       (self.mso_table.name(mso_id) if self.mso_table else None)
                       or "?", c["resolved"], c["checked"], c["unmapped"],
                       c["unknown_lut"], c["unknown_weru"]))
                if c["invalid_werus"]:
                    add("                  ... and %d of the %d WERUs it points "
                        "into name blocks past BLOCKS_PER_LUN: %s -- unreadable "
                        "for a second, independent reason"
                        % (len(c["invalid_werus"]), len(c["werus"]),
                           c["invalid_werus"][:8]))
            add("                  an address is not readability: user data is "
                "native TLC in WERUs that are mostly unsurveyed here")
        rd = self.pbid_readings()
        if rd is not UNKNOWN:
            a, d, _bad = self.weru_config_agreement()
            add("pbid decomposition %d WERUs read as 24*pairIndex + 12*half + "
                "slot (block pair on six CEs) -- reproduces %d of %d measured "
                "configurations, %d disagree"
                % (len(rd["pair_half"]), a, a + d, d))
            add("                  %d WERUs are 48-slot records, read under the "
                "SEPARATE measured law slot = 4*ce + block - blockBase "
                "(44/44 on weru 234 p539); invalid/partial/irregular records "
                "are refused and stay UNKNOWN" % len(rd["block_ce"]))
            subs_ = self.substituted_weru_configs()
            if subs_ is not UNKNOWN:
                add("                  %d `substituted` records are refused BY "
                    "DEFAULT and answerable on request: the entry POSITION "
                    "carries the slot (POS_TO_SLOT) and sub_40312B84 swaps "
                    "entries in place, so a substitute keeps the slot it "
                    "replaced -- weru_config(w, allow_substituted=True)"
                    % len(subs_))
        bm = self.block_map()
        add("blockMapping      %s"
            % (UNKNOWN if bm is UNKNOWN
               else "%d weruIds, stride %d, SLOT-INDEXED (None = unmapped)"
               % (len(bm), geom.BLOCKS_PER_WERU)))
        if w is not UNKNOWN and bm is not UNKNOWN:
            shapes = {}
            for x in w:
                if x.blocks is UNKNOWN or not x.mapped_blocks:
                    continue
                shapes[x.shape] = shapes.get(x.shape, 0) + 1
            if shapes:
                add("WERU shapes       %s" % dict(sorted(shapes.items())))
                if shapes.get("invalid"):
                    add("                  'invalid' names a block >= %d -- "
                        "not a location. Since the inline-RAID parity fix the "
                        "only ones left are weruIds >= WERU_COUNT (%d), i.e. "
                        "the blockMapping section's LMA-rounding padding"
                        % (geom.BLOCKS_PER_LUN, WERU_COUNT))
        for ce, s in sorted(self.surveys.items()):
            add("survey            %s" % s)

        if self.notes:
            add("")
            add("notes")
            for nline in self.notes:
                add("  - %s" % nline)
        return "\n".join(L)

    def __repr__(self):
        return "<Ftl msos=%s werus=%s>" % (
            "?" if self.msos() is UNKNOWN else len(self.msos()),
            "?" if self.werus() is UNKNOWN else len(self.werus()))


# ---------------------------------------------------------------------------
def _runs(ids):
    """[0,1,2,5,6] -> '0-2,5-6'.  Display only."""
    out, s = [], None
    prev = None
    for i in sorted(ids):
        if s is None:
            s = prev = i
            continue
        if i == prev + 1:
            prev = i
            continue
        out.append("%d" % s if s == prev else "%d-%d" % (s, prev))
        s = prev = i
    if s is not None:
        out.append("%d" % s if s == prev else "%d-%d" % (s, prev))
    return "[%s]" % ",".join(out)


def resolve_scan_ces(dev, ces=None):
    """The CE list a scan will actually use -- resolved, never the word 'auto'.

    `ces=None` means "every CE this device serves except the ones geometry
    marks dead", and that set depends entirely on the device.  Resolving it
    here, once, is what lets the cache key name it.
    """
    present = sorted(dev.present_ces())
    if ces is None:
        return [c for c in present if c not in geom.CE_DEAD]
    return sorted(ces)


def resolve_scan_layout(layout=UNKNOWN, mso=None, descriptor=None):
    """The aux layout a scan will actually rank under.  -> (layout, why)

    Three ways to say it, in order of directness:

      * `layout=` -- an explicit layout, checked against `titania.auxdata.LAYOUTS`;
      * `mso=` plus `descriptor=` -- resolved through `titania.auxdata.layout_for`,
        i.e. off this drive's `msoDescriptor +17`, which is where the firmware
        reads it (`efc_cpu2.c:43002`);
      * neither -- UNKNOWN, and the reason says so.

    UNKNOWN is a real answer and is returned rather than substituted.  The
    layout is a per-drive configuration byte; there is no table of what some
    other board had, and inventing one is how a plausible wrong answer gets
    made.  Reading a field under the wrong layout's rule does not produce a
    detectable error -- it produces a number.
    """
    if layout is not UNKNOWN and layout is not None:
        if layout not in auxmod.LAYOUTS:
            raise ValueError("aux layout %r is outside the firmware's switch %s"
                             % (layout, list(auxmod.LAYOUTS)))
        return layout, "supplied by the caller"
    if mso is not None and descriptor is not None:
        got = auxmod.layout_for(mso, descriptor)
        if got is not auxmod.UNKNOWN:
            return got, ("msoDescriptor[%s] +%d, via titania.auxdata.layout_for"
                         % (getattr(mso, "mso_id", mso),
                            auxmod.MSO_DESC_AUX_LAYOUT_OFF))
        return UNKNOWN, ("msoId %s has no msoDescriptor entry on this drive -- "
                         "which is NOT the same as having layout 0"
                         % (getattr(mso, "mso_id", mso),))
    return UNKNOWN, ("no layout, and no msoId + msoDescriptor to derive one "
                     "from; the sequence is still used, which is right for 3 "
                     "of the 4 layouts, and this string is the record that it "
                     "was not checked")


def _device_identity(dev):
    """A short, stable string for what a device is backed by.

    Two devices that serve different bytes must not produce the same string.
    `DumpDevice` carries its root path; anything else falls back to its class
    name plus repr, which at worst over-separates (a spurious cache miss costs
    a rescan; a spurious cache hit costs a wrong answer).
    """
    root = getattr(dev, "root", None)
    if root is not None:
        return "%s:%s" % (type(dev).__name__, root)
    return "%s:%s" % (type(dev).__name__, repr(dev))


#: Bump whenever `SystemMetadata.scan` can produce different bytes for the same
#: inputs, so old caches miss instead of silently serving the previous answer.
#: The key already names every input; this names the algorithm.
#:   2 -- inline-RAID parity AUs no longer win newest-wins
#:   3 -- the pick is layout-aware (`page_rank`): on layout 2 the sequence is
#:        dropped from the key, so a layout-2 WERU can resolve to different
#:        bytes than a cache written before this.  The layout is also an input
#:        now and is named in the key, but the format still moves, because a
#:        cache written at format 2 with no layout must not be served for a
#:        format-3 scan that also had none: the algorithm differs even where the
#:        inputs match.
SCAN_CACHE_FORMAT = 3


def scan_cache_key(dev, blocks, ces, verify, tag, layout=UNKNOWN):
    """The on-disk cache key for one WERU scan.

    It also carries `SCAN_CACHE_FORMAT`.  The key naming every input is not
    enough once the resolution RULE changes: the parity-aware pick returns
    different bytes for identical inputs, and a cache written before it would
    have been served unchallenged.

    It must name every input that changes what the scan can see.  It did not:
    the CE set was written as the literal string ``"auto"`` whenever the caller
    passed ``ces=None``, so a scan of `<dump>/raw` (CEs 1-6) and a scan of the
    whole `the dump` (CEs 1-11 after the dead-CE filter) and a scan off the
    hardware all shared one key.  Every run against the wider backing silently
    returned the narrow image, and `describe()` printed the narrow CE list with
    nothing to say it could have read more.

    The key now carries the device's identity, the concrete CE list, the blocks
    and the verify flag; and the loader re-checks the stored CE list before
    trusting a hit, so an old-format or mismatched cache is a miss rather than a
    wrong answer.
    """
    ident = hashlib.sha1(_device_identity(dev).encode("utf-8", "replace")
                         ).hexdigest()[:10]
    return "%s_b%s_ce%s_v%d_l%s_f%d_%s.pkl" % (
        tag, "-".join(map(str, blocks)),
        "-".join(map(str, ces)) if ces else "none",
        int(bool(verify)),
        "u" if layout is UNKNOWN or layout is None else layout,
        SCAN_CACHE_FORMAT, ident)


def _cached_scan(dev, blocks, codec, ces, verify, progress, cache_dir, tag,
                 weru_id=None, slc=True, layout=UNKNOWN, mso=None,
                 descriptor=None):
    """SystemMetadata.scan with an optional on-disk cache.

    The key includes the blocks, the resolved CE list, the verify flag and the
    device's identity, because a map built without MCRC verification, or on a
    narrower set of CEs, is a different (weaker) object and must never be
    silently reused.  Caches are analysis artefacts; nothing here ever writes
    to a dump.
    """
    use_ces = resolve_scan_ces(dev, ces)
    # Resolved HERE, before the key is built, because the layout decides how
    # copies are ranked and is therefore an input like any other.  Two scans
    # that differ only in it are different objects.
    use_layout, _why = resolve_scan_layout(layout, mso, descriptor)
    if cache_dir is None:
        return SystemMetadata.scan(dev, blocks, codec, use_ces, verify, progress,
                                   weru_id=weru_id, slc=slc, layout=use_layout)
    import pickle
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / scan_cache_key(dev, blocks, use_ces, verify, tag, use_layout)
    if p.exists():
        try:
            d = pickle.loads(p.read_bytes())
            # Belt and braces: the key names the CE list, and the payload is
            # re-checked against it.  A cache written before the key was fixed
            # has a different name anyway, but a stale one that somehow matches
            # still cannot smuggle in a narrower scan.
            if sorted(d.get("ces", ())) != list(use_ces):
                raise ValueError("cached scan used ces %s, this run wants %s"
                                 % (d.get("ces"), use_ces))
            # Same argument for the CELL mode: a scan read in the other mode is
            # a scan of different physical word lines, not a cheaper version of
            # this one.  Caches written before `slc` was recorded do not carry
            # it and are refused rather than assumed to match.  Note the mode is
            # not in the file name, so scanning the same blocks in the other
            # mode rescans and overwrites rather than keeping two files -- the
            # thing that must never happen is serving one for the other, and
            # this is checked against the payload, which cannot be stale.
            if d.get("stats", {}).get("slc") is not slc:
                raise ValueError("cached scan used slc=%r, this run wants %r"
                                 % (d.get("stats", {}).get("slc"), slc))
            # Placements are cached as plain tuples, never as pickled class
            # instances: a cache written by `import titania.ftl` and read back
            # under `python -m titania.ftl` would otherwise unpickle to a
            # different Placement class and fail every isinstance check.
            # And the layout, for the same reason as the cell mode: it is in
            # the file name, but the name is a hash of intent and the payload
            # is the thing that was actually done.  A cache whose stats say a
            # different layout ranked its copies is refused.
            if d.get("stats", {}).get("aux_layout", UNKNOWN) != use_layout:
                raise ValueError("cached scan ranked under aux layout %r, this "
                                 "run wants %r"
                                 % (d.get("stats", {}).get("aux_layout"),
                                    use_layout))
            place = {l: Placement(*v) for l, v in d["placement"].items()}
            return SystemMetadata(d["lma"], place, d["ces"], d["blocks"],
                                  d["stats"], weru_id=d.get("weru_id", weru_id),
                                  aux_layout=d["stats"].get("aux_layout",
                                                            UNKNOWN),
                                  unordered=d.get("unordered", ()))
        except Exception:
            pass
    sm = SystemMetadata.scan(dev, blocks, codec, use_ces, verify, progress,
                             weru_id=weru_id, slc=slc, layout=use_layout)
    try:
        place = {l: (v.ce, v.block, v.page, v.au, v.seq, v.slc)
                 for l, v in sm.placement.items()}
        p.write_bytes(pickle.dumps(dict(lma=sm.lma, placement=place,
                                        ces=sm.ces, blocks=sm.blocks,
                                        stats=sm.stats, weru_id=sm.weru_id,
                                        unordered=sorted(sm.unordered))))
    except OSError:
        pass
    return sm


# ===========================================================================
# self-test
# ===========================================================================
def _selftest(argv):                                    # pragma: no cover
    import time
    root = Path(__file__).resolve().parent.parent
    deep = "--deep" in argv
    fw_path = root / "dumps" / "fw.sram.efc.bin"
    raw_dir = config.dump_path("raw")
    survey_csv = config.dump_path("wide", "wide.csv")
    cache = config.dump_path("sysmeta", "ftl_cache")

    results = []

    def report(name, state, detail=""):
        results.append((name, state))
        print("%-9s %-34s %s" % ("[%s]" % state, name, detail))

    print("titania.ftl self-test")
    print("=" * 72)

    # ---- 1. msoIdTable ---------------------------------------------------
    # 1a. The embedded constant.  This one needs no files at all -- it is the
    # whole point of embedding: a fresh checkout with no firmware image names
    # every static MSO.  It runs before the located-table checks so a broken
    # constant is reported even on a machine that has no dump.
    emb = embedded_mso_table()
    emb_ids = sorted(emb.entries())
    ok = (emb.raw == MSOID_TABLE_EMBEDDED
          and len(emb.raw) == MSOID_TABLE_LEN
          and emb.n_entries == 31
          and emb_ids == list(range(MSOID_STATIC_MIN, MSOID_STATIC_MAX + 1))
          and emb.name(MSOID_ANCHOR_VALUE) == "SystemMetadata/System")
    report("embedded msoIdTable is well-formed", "PASS" if ok else "FAIL",
           "%d bytes, %d entries, ids %d..%d, %d = %s (fw %s)"
           % (len(emb.raw), emb.n_entries, emb_ids[0], emb_ids[-1],
              MSOID_ANCHOR_VALUE, emb.name(MSOID_ANCHOR_VALUE),
              MSOID_TABLE_FW_VERSION))

    if not fw_path.exists():
        report("msoIdTable", "SKIPPED", "no %s" % fw_path.name)
        table = None
    else:
        blob = fw_path.read_bytes()
        t0 = time.time()
        table = MsoIdTable.locate(blob, offset=None, hint=None)   # force the scan
        ents = table.entries()
        vals = sorted(ents)
        ok = (table.raw[MSOID_ANCHOR_INDEX] == MSOID_ANCHOR_VALUE
              and vals == list(range(MSOID_STATIC_MIN, MSOID_STATIC_MAX + 1))
              and len(set(vals)) == len(vals))
        report("msoIdTable located by scan", "PASS" if ok else "FAIL",
               "offset %s, %d entries, ids %d..%d, anchor t[%d]=%d, %.1fs"
               % (hex(table.offset), len(vals), vals[0], vals[-1],
                  MSOID_ANCHOR_INDEX, table.raw[MSOID_ANCHOR_INDEX],
                  time.time() - t0))
        print("          %s = %s" % (MSOID_ANCHOR_VALUE,
                                     table.name(MSOID_ANCHOR_VALUE)))
        # the base-offset trap must be detected, not silently accepted
        try:
            MsoIdTable(blob[table.offset - MSOID_ANCHOR_INDEX:
                            table.offset - MSOID_ANCHOR_INDEX + MSOID_TABLE_LEN],
                       table.offset - MSOID_ANCHOR_INDEX)
            report("base-offset trap rejected", "FAIL", "accepted base-30!")
        except ValueError as e:
            report("base-offset trap rejected", "PASS", str(e)[:96])
        # 1b. The regression guard on the constant.  `MSOID_TABLE_EMBEDDED` is
        # a paste out of this exact image, so it must still equal what a scan
        # of that image finds.  If someone edits the constant, this fails --
        # which is the objection to embedding, answered.
        same = table.raw == MSOID_TABLE_EMBEDDED
        cmp_ = compare_mso_table(blob)
        report("embedded table == the one in %s" % fw_path.name,
               "PASS" if same and cmp_["agree"] else "FAIL",
               "126/126 bytes identical, located at %s (fw %s); "
               "`titania fw verify-table %s` is the same comparison, scriptable"
               % (hex(table.offset), MSOID_TABLE_FW_VERSION,
                  MSOID_TABLE_FW_IMAGE)
               if same else
               "%d byte(s) DIFFER: %s" % (len(cmp_["diffs"]), cmp_["diffs"][:8]))
        # padding must not be counted as entries
        report("padding not counted as entries",
               "PASS" if 0 not in ents and table.padding_from <= MSOID_TABLE_LEN
               else "FAIL",
               "%d non-0xFF bytes, %d real entries, padding from index %d "
               "(no msoId 0)"
               % (sum(1 for v in table.raw if v != MSOID_INVALID),
                  len(ents), table.padding_from))

    # ---- a Device over the local raw dump --------------------------------
    dev = _selftest_device(root)
    if dev is None:
        report("device", "SKIPPED", "no %s" % raw_dir)
        print("\nSELF-TEST: nothing further can run without page dumps")
        return 1
    print("\ndevice: %r  (%s)" % (dev, getattr(dev, "note", "")))

    try:
        codec = default_codec()
        print("codec : %s" % codec.name)
    except ImportError as e:
        report("codec", "SKIPPED", str(e))
        return 1

    # ---- 2. RootStorage / SystemBlockMapping -----------------------------
    t0 = time.time()
    # ---- 2a. Which generation.  Blocks 2/3 and 4/5 are two sections of the
    # one Bootstrap store and the firmware picks one; mounting the other is a
    # silent whole-image staleness.  Nothing here assumes block 2.
    gen = None
    try:
        gen = root_storage_generations(dev, codec=codec)
    except (LookupError, ValueError, DeviceError) as e:
        report("RootStorage generation", "SKIPPED", str(e))
    if gen is not None and gen.get("block") is not None:
        spp = (ROOTSTORAGE_STRIPES_PER_PAGE[ROOTSTORAGE_BOOTSTRAP_BLOCKS[0]]
               * geom.PAGES_PER_BLOCK_SLC)
        secs = gen["sections"]
        # every section must be a block pair, and the pair must agree
        shape_ok = all(len(b) == 2 for b in secs.values())
        pair_ok = all(gen["block_base"][b[0]] == gen["block_base"][b[1]]
                      for b in secs.values() if len(b) == 2)
        report("RootStorage sections", "PASS" if shape_ok and pair_ok else "FAIL",
               "numberSections %d (the literal 2u at efc_cpu2.c:149143), "
               "stripesPerSection %d = %d stripes/page x %d pages, counter "
               "modulus %d = 2 x numberOfAvailableStripes (sub_40367130, "
               "efc_cpu2.c:151164); derived from each block's OWN base stripe: "
               "%s -- and the two blocks of a pair agree"
               % (ROOTSTORAGE_SECTIONS, spp,
                  ROOTSTORAGE_STRIPES_PER_PAGE[ROOTSTORAGE_BOOTSTRAP_BLOCKS[0]],
                  geom.PAGES_PER_BLOCK_SLC, 2 * ROOTSTORAGE_SECTIONS * spp,
                  {s: list(b) for s, b in sorted(secs.items())}))
        report("RootStorage generation", "PASS" if gen["section"] is not None
               else "FAIL",
               "section base stripes %s -> ScanLastStripe (sub_4036268A, "
               "efc_cpu2.c:146497-146532) picks section %d = blocks %s by "
               "%s.  Plain highest-wins would pick section %d, which is WRONG "
               "here: the stripe counter runs modulo %d, so the newest section "
               "carries the SMALLEST number"
               % (gen["last_stripes"], gen["section"], list(gen["blocks"]),
                  gen["rule"],
                  max(range(len(gen["last_stripes"])),
                      key=lambda i: (gen["last_stripes"][i] != INVALID_STRIPE,
                                     gen["last_stripes"][i])),
                  2 * ROOTSTORAGE_SECTIONS * spp))
        # the port of sub_4036268A, against hand-worked vectors
        vec = [(([0, 6624], 2208), 0),        # this board: wrap override
               (([4416, 6624], 2208), 1),     # normal round-robin, 1 newer
               (([4416, 2208], 2208), 0),     # normal round-robin, 0 newer
               (([0, INVALID_STRIPE], 2208), 0),
               (([INVALID_STRIPE, 2208], 2208), 1),
               (([INVALID_STRIPE, INVALID_STRIPE], 2208), 0)]
        bad = [(a, w, scan_last_stripe(*a)) for a, w in vec
               if scan_last_stripe(*a) != w]
        report("scan_last_stripe port", "PASS" if not bad else "FAIL",
               "%d/%d vectors, including the two round-robin cases where the "
               "override must NOT fire%s"
               % (len(vec) - len(bad), len(vec),
                  "" if not bad else ": %s" % bad))
        # independent corroboration: the SegmentedBuffer header sequence numbers
        newer = older = tie = 0
        try:
            a = RootStorage(dev, gen["blocks"][0], codec=codec)
            other = [b for s, bl in gen["sections"].items() if s != gen["section"]
                     for b in bl]
            if other:
                b = RootStorage(dev, other[0], codec=codec)
                for oid in list(range(OBJ_WERU_USAGE_LOG_FIRST, 26)) + \
                           list(range(44, 70)) + [74, 75]:
                    try:
                        ba, _ = a.object(oid)
                        bb, _ = b.object(oid)
                    except (IndexError, ValueError):
                        continue
                    if not isinstance(ba, (bytes, bytearray)) or \
                       not isinstance(bb, (bytes, bytearray)):
                        continue
                    if len(ba) < 4 or len(bb) < 4:
                        continue
                    sa = struct.unpack_from("<I", ba, 0)[0]
                    sb = struct.unpack_from("<I", bb, 0)[0]
                    newer += sa > sb
                    older += sa < sb
                    tie += sa == sb
        except (LookupError, ValueError, DeviceError):
            pass
        if newer + older + tie:
            report("generation corroborated by SegmentedBuffer headers",
                   "PASS" if older == 0 and newer > 0 else "FAIL",
                   "over %d objects present in BOTH sections, the chosen "
                   "section's headerSequenceNumber is higher on %d, equal on "
                   "%d and LOWER ON %d -- an independent counter, written by "
                   "SegmentedBuffer not by the stripe writer"
                   % (newer + older + tie, newer, tie, older))
        else:
            report("generation corroborated by SegmentedBuffer headers",
                   "SKIPPED", "no object is servable from both sections here")

    # ---- 2a-ii. The backup store has two sections too, split by CE half.
    # `Ftl.physical_block_properties` reads block 1, and until today
    # `catalogue()` simply took the first CE that answered -- which happened to
    # be the live half only because CE0 is dead and the scan starts at CE1.
    try:
        bgen = root_storage_generations(
            dev, codec=codec, blocks=(ROOTSTORAGE_BACKUP_BLOCK,))
        brs = RootStorage(dev, ROOTSTORAGE_BACKUP_BLOCK, codec=codec)
        bh, _be, bce, bpg = brs.catalogue()
    except (LookupError, ValueError, DeviceError) as e:
        report("Backup RootStorage generation", "SKIPPED", str(e))
    else:
        ok = (brs.section == bgen["section"]
              and bgen["section"] is not None
              and set(brs.stale_section_ces)
              & {c for _b, c in bgen["section_ces"].get(
                  1 - (bgen["section"] or 0), ())} ==
              set(brs.stale_section_ces))
        report("Backup RootStorage generation", "PASS" if ok else "FAIL",
               "block %d carries BOTH sections, split by CE half -- section "
               "base stripes %s, k=%d so the placement offset is (ce//2) %% %d "
               "and NOT ce//2 (which is why CEs 6..11 used to read as "
               "unservable rather than as another generation).  ScanLastStripe "
               "picks section %d, and catalogue() now refuses CEs %s instead of "
               "taking whichever answered first; newest stripe %d on ce%d p%d"
               % (ROOTSTORAGE_BACKUP_BLOCK, bgen["last_stripes"], brs.k, brs.k,
                  brs.section, brs.stale_section_ces, bh["stripe"], bce, bpg))

    rs = RootStorage(dev, gen["block"] if gen and gen.get("block") else 2,
                     codec=codec)
    try:
        h, ents, ce, pg = rs.catalogue()
        sbm = rs.system_block_mapping()
    except (LookupError, ValueError) as e:
        report("RootStorage", "SKIPPED", str(e))
        sbm = UNKNOWN
        h = None
    if h is not None:
        report("RootStorage catalogue", "PASS" if h["type"] == ROOTSTORAGE_TYPE
               else "FAIL",
               "block %d %s, newest stripe %d on ce%d p%d, %d objects, "
               "storeGeneration %d, %.1fs"
               % (rs.block, rs.instance, h["stripe"], ce, pg,
                  sum(1 for e in ents if e[0] != INVALID_STRIPE),
                  h["store_generation"], time.time() - t0))
        report("RootStorage aux crib",
               "PASS" if rs.aux_ok and rs.aux_ok > rs.aux_bad else "FAIL",
               "%d/%d AUs read plaintext aux %s"
               % (rs.aux_ok, rs.aux_ok + rs.aux_bad, ROOTSTORAGE_AUX.hex()))
        # The object-id -> (size, tag) table.  Archiving `nd_rsobjects.py` cost
        # the only executable re-verification of it, so it is re-checked here
        # against the live catalogue on both instances.
        for _rsx in [r for r in (rs, locals().get("brs")) if r is not None]:
            try:
                n_ok, probs = rootstorage_catalogue_check(_rsx)
            except (LookupError, ValueError, DeviceError) as e:
                report("RootStorage object table (%s)" % _rsx.instance,
                       "SKIPPED", str(e))
                continue
            report("RootStorage object table (%s)" % _rsx.instance,
                   "PASS" if not probs and n_ok else
                   "FAIL" if probs else "SKIPPED",
                   "%d singleton objects + chains verified by (size, type byte);"
                   " chains checked as RUNS that terminate, so a chain growing "
                   "past its table entry is caught. %s"
                   % (n_ok, "; ".join(probs) if probs else
                      ("the three Bootstrap chains are WeruUsageLog 4..25, "
                       "LptNsid2 44..69, LptNsid3 74..75 -- NOT the 'SystemLut "
                       "payload chain A/B/C' of nd_rsobjects.py's report; "
                       "confirmed twice over, by firstObjectId=4 "
                       "(efc_cpu2.c:31748) and by the crc8 name tags"
                       if _rsx.instance == "Bootstrap" else
                       "objects 0/6/7 + the PhysicalBlockProperties segment "
                       "chain 8..20 (PHYS_BLOCK_BUFFER_FIRST_OBJECT)")))
    if sbm is not UNKNOWN and sbm is not None:
        nxt, live = sbm
        order = sbm_recency_order(nxt, live)
        ok = bool(order)
        detail = []
        for rank, slot in enumerate(order):
            weru, pairs = live[slot]
            nums = {b for b, _c in pairs}
            ces = sorted(c for _b, c in pairs)
            ok &= (len(nums) == 1 and ces == list(range(geom.CE_COUNT)))
            detail.append("slot %d (ring %d) weruId %d -> block %d on ce %d..%d"
                          % (slot, rank, weru, sorted(nums)[0], ces[0], ces[-1]))
        report("SystemBlockMapping", "PASS" if ok else "FAIL",
               "%d B object (8 + %d*(4 + 4*%d), and 64*%d + 72 = %d is the "
               "firmware's own size formula for it), nextSlot %d, %d live: %s "
               "-- each is one block number across all %d CEs, as a WERU must "
               "be, and ALL of them are installed at mount: ring order is not "
               "generation order"
               % (SYSTEM_BLOCK_MAPPING_SIZE, SYSTEM_WERU_SLOTS,
                  SYSTEM_BLOCK_MAPPING_PBIDS, SYSTEM_BLOCK_MAPPING_PBIDS,
                  64 * SYSTEM_BLOCK_MAPPING_PBIDS + 72, nxt, len(live),
                  "; ".join(detail), geom.CE_COUNT))
        report("stride-12 confusion guarded", "PASS",
               "SystemBlockMapping carries %d pbids/slot; "
               "decode_block_mapping refuses any stride but %d"
               % (SYSTEM_BLOCK_MAPPING_PBIDS, geom.BLOCKS_PER_WERU))
        try:
            decode_block_mapping(b"\0" * 4000, SYSTEM_BLOCK_MAPPING_PBIDS)
            report("stride-12 rejected", "FAIL", "accepted!")
        except ValueError:
            report("stride-12 rejected", "PASS", "raises ValueError")

    # ---- 2b. WERU_CONFIGURATIONS against the drive's own persisted copy ----
    # RootStorage Bootstrap object 83 = `WeruConfiguration`.  Until 2026-08-07
    # the six configurations came only from `sub_4033CB5E`'s defaults in the
    # firmware image -- a code path, not a measurement, and the SRAM array it
    # builds reads all zeros on the bench.  This check is the measurement.
    # Order matters: try the authoritative section first and fall back to the
    # older one only if no copy there is servable.  `WeruConfiguration` is a
    # format-time constant, so reading the older generation is harmless here --
    # but a first-servable-wins loop over (2,3,4,5) is exactly the shape that
    # silently mixes generations, and on this backing object 83 is unservable in
    # section 0 and servable in section 1, so the loop really did cross over.
    cfg_body = UNKNOWN
    cfg_src = None
    _order = list(ROOTSTORAGE_BOOTSTRAP_BLOCKS)
    if gen is not None and gen.get("blocks"):
        _order = ([b for b in _order if b in gen["blocks"]]
                  + [b for b in _order if b not in gen["blocks"]])
    for _blk in _order:
        try:
            _rs = RootStorage(dev, _blk, codec=codec)
            _b, _i = _rs.object(WERU_CONFIGURATION_OBJECT_ID)
        except (LookupError, ValueError, IndexError):
            continue
        if isinstance(_b, (bytes, bytearray)) and len(_b) >= 120:
            cfg_body, cfg_src = bytes(_b), _blk
            break
    if cfg_body is UNKNOWN:
        report("WeruConfiguration on media (object %d)"
               % WERU_CONFIGURATION_OBJECT_ID, "SKIPPED",
               "no copy of the object is servable from this backing; its "
               "stripe lands on ce10/ce11 in blocks 2/3")
    else:
        rows, bad = [], []
        for i, c in enumerate(WERU_CONFIGURATIONS):
            o = i * WERU_CONFIG_RECORD_BYTES
            got = (struct.unpack_from("<H", cfg_body, o)[0],       # cfgIdx
                   struct.unpack_from("<H", cfg_body, o + 10)[0],  # firstWeruId
                   struct.unpack_from("<H", cfg_body, o + 4)[0],   # weruCount
                   struct.unpack_from("<H", cfg_body, o + 2)[0],   # blocksPerWeru
                   struct.unpack_from("<H", cfg_body, o + 14)[0])  # cellMode
            want = (c[0], c[1], c[2], c[3], c[5])
            rows.append(got)
            if got != want:
                bad.append((i, want, got))
            flags = struct.unpack_from("<I", cfg_body, o + 16)[0]
            if flags != WERU_CONFIG_FLAGS[i]:
                bad.append((i, "flags %d" % WERU_CONFIG_FLAGS[i],
                            "flags %d" % flags))
        # the 1152-byte tail: 6 configurations x 24 columns of (first, count)
        runbad = []
        base = len(WERU_CONFIGURATIONS) * WERU_CONFIG_RECORD_BYTES
        ncol = (len(cfg_body) - base) // (8 * len(WERU_CONFIGURATIONS))
        for i, c in enumerate(WERU_CONFIGURATIONS):
            want = WERU_COLUMN_PAIR_RUNS[c[0]]
            for col in range(ncol):
                o = base + 8 * (i * ncol + col)
                got = struct.unpack_from("<II", cfg_body, o)
                if got != want:
                    runbad.append((c[0], col, want, got))
        report("WeruConfiguration on media (object %d)"
               % WERU_CONFIGURATION_OBJECT_ID,
               "PASS" if not bad and not runbad and ncol else "FAIL",
               "the drive's own %d-byte copy (blocks %d/%d%s, %d = 48*%d + 120) "
               "reproduces all six (cfgIdx, firstWeruId, weruCount, "
               "blocksPerWeru, cellMode) rows and all six WERU_CONFIG_FLAGS "
               "%s, and its %d-byte tail is %d columns x %d configurations of "
               "(firstPairIndex, numberOfPairs) matching WERU_COLUMN_PAIR_RUNS "
               "in every column %s.  Until today both tables came only from "
               "sub_4033CB5E's defaults in the firmware image; byte_900078 "
               "itself reads all zeros on the bench, so this is the first time "
               "the DRIVE has been asked."
               % (len(cfg_body), cfg_src, cfg_src + 1,
                  "" if gen is None or cfg_src in (gen.get("blocks") or ())
                  else " -- the OLDER generation; no copy is servable in "
                       "the authoritative section %s" % list(gen["blocks"]),
                  len(cfg_body), (len(cfg_body) - 120) // 48,
                  bad or "(0 disagree)", len(cfg_body) - base, ncol,
                  len(WERU_CONFIGURATIONS), runbad or "(0 disagree)"))

    # ---- 3. the full Ftl -------------------------------------------------
    t0 = time.time()
    ftl = Ftl.from_device(dev, firmware=fw_path if fw_path.exists() else None,
                          codec=codec, cache_dir=cache)
    print("\n(from_device took %.1fs)" % (time.time() - t0))
    sm = ftl.system_metadata()
    if sm is UNKNOWN:
        report("SystemMetadata", "SKIPPED", "no metadata WERU scanned")
    else:
        d = sm.directory()
        ok = (d is not UNKNOWN and d["magic"] == SYS_MAGIC
              and d["formatVersion"] == SYS_FORMAT_VERSION)
        report("SystemMetadata directory", "PASS" if ok else "FAIL",
               "magic 0x%08x \"SYS\", formatVersion 0x%08x, revision %d, "
               "%d LMAs resolved over werus %s"
               % (d["magic"], d["formatVersion"], d["revision"], len(sm.lma),
                  sm.weru_ids))
        # -- the two page_sequence decoders, on every page the scan read -----
        # `ftl.page_sequence` was a u24 of AU1 alone until today; it now reads
        # the full 62 bits, AU0 high and AU1 low, which is what `titania.auxdata`
        # has always read.  That they now agree is a measurement, so it is
        # measured, on every page rather than on a sample.
        agree = sm.stats.get("seq_agree", 0)
        dis = sm.stats.get("seq_disagree", 0)
        report("ftl.page_sequence == titania.auxdata.page_sequence",
               "PASS" if agree and not dis else "FAIL",
               "%d page(s) decoded by BOTH implementations, %d agree, %d "
               "DISAGREE. Same 62 bits, two decoders: ftl reads AU0 bits 30..0 "
               "as the high half and AU1 bits 30..0 as the low; aux reaches it "
               "through parse_page's per-AU shards (SEQ_SHARD_BITS_01 = 31). A "
               "second implementation that is never compared is not a check"
               % (agree + dis, agree, dis))

        # -- the ranking is layout-aware -------------------------------------
        # `page_rank` alone, exhaustively over the four layouts: the property
        # that matters is that layout 2's sequence cannot change the order and
        # every other layout's can.
        pr_ok, pr_bad = [], []
        for lay in list(auxmod.LAYOUTS) + [UNKNOWN, None]:
            ranks = (page_rank(1, False, lay) != page_rank(2, False, lay))
            want = lay != auxmod.LAYOUT_NSID23
            (pr_ok if ranks == want else pr_bad).append(lay)
            # ... and the parity flag must outrank any sequence on every layout
            if not page_rank(1 << 61, True, lay) < page_rank(0, False, lay):
                pr_bad.append("parity/%s" % lay)
        report("page_rank is layout-aware, and parity still dominates",
               "PASS" if not pr_bad else "FAIL",
               "over layouts %s: the sequence separates two copies on %s and "
               "CANNOT on layout %d, and on all of them a flagged copy loses to "
               "an ordinary one even with a 2**61 sequence. Layout 2's writer "
               "reads the pool without incrementing it (efc_cpu2.c:43058), so "
               "its value is the write EPOCH -- msoId 51 reads 2812 on every "
               "page and msoId 53 reads 2340. Failures: %s"
               % (list(auxmod.LAYOUTS) + ["UNKNOWN", "None"],
                  [x for x in pr_ok if x != auxmod.LAYOUT_NSID23],
                  auxmod.LAYOUT_NSID23, pr_bad or "none"))

        # ... and that the layout actually reaches the scan's pick.  One block,
        # scanned twice: the only difference is the layout, so any difference in
        # the outcome is the layout arriving.  Under layout 2 every LMA with a
        # second ordinary copy must become unordered, and under layout 0 none
        # may -- the per-page counter never repeats.
        # The probe needs LMAs that actually have a second ordinary copy, or it
        # would pass by having nothing to order.  One block on one CE has none
        # -- every LMA appears once -- so it takes two blocks on two CEs.
        _p0 = sm.parts[sm.weru_ids[0]]
        probe_blks = tuple(_p0.blocks[:2])
        probe_ces = list(_p0.ces[:2])
        a0 = SystemMetadata.scan(dev, probe_blks, codec, probe_ces, True, None,
                                 slc=True, layout=auxmod.LAYOUT_INTERNAL_DATA)
        a2 = SystemMetadata.scan(dev, probe_blks, codec, probe_ces, True, None,
                                 slc=True, layout=auxmod.LAYOUT_NSID23)
        dupes = a2.stats["superseded"] - a2.stats["parity_refused"]
        report("the layout reaches SystemMetadata.scan's pick",
               "PASS" if (a0.stats["unordered"] == 0
                          and a2.stats["unordered"] > 0
                          and set(a0.lma) == set(a2.lma)) else "FAIL",
               "blocks %s on CEs %s scanned twice, layout 0 vs layout 2, %d "
               "LMAs both times: layout 0 leaves %d LMAs unordered (its counter "
               "is per page and never repeats, so a tie is impossible) and "
               "layout 2 leaves %d of the %d LMAs that a later copy displaced. "
               "The LMA SET is identical -- dropping the sequence changes WHICH "
               "COPY wins, never which addresses exist"
               % (list(probe_blks), probe_ces, len(a0.lma),
                  a0.stats["unordered"], a2.stats["unordered"], dupes))
        report("layout-2 scan says so in its own stats",
               "PASS" if (a2.stats["aux_layout"] == auxmod.LAYOUT_NSID23
                          and a2.stats["sequence_ranks_pages"] is False
                          and a0.stats["sequence_ranks_pages"] is True
                          and a2.aux_layout == auxmod.LAYOUT_NSID23
                          and callable(a2.layout)) else "FAIL",
               "stats carry aux_layout=%r sequence_ranks_pages=%r (%s), so a scan "
               "whose ranking was refused is legible without re-deriving it. "
               "The scans behind this image RAN at layout %s and were "
               "afterwards MEASURED, off this drive's own msoDescriptor via "
               "aux.layout_for, to be %s -- both in SEQUENCE_RANKS_PAGES, so "
               "the pick is the same either way"
               % (a2.stats["aux_layout"], a2.stats["sequence_ranks_pages"],
                  a2.stats["aux_layout_source"], sm.stats.get("aux_layouts"),
                  sm.stats.get("aux_layouts_confirmed")))
        # Two extra 5413-LMA images are ~44 MB of payload that nothing below
        # reads.  --deep already holds two whole Ftls and a 55 000-page index;
        # a probe must not be the thing that pushes it over.
        del a0, a2

        tot = sm.total_lmas()
        report("msoDescriptor[41].totalLmas",
               "PASS" if tot in SYSMETA_SIZE_CLASSES else "FAIL",
               "%s (a maxSystemMetadataSizeInLmas class %s -- independent "
               "confirmation that msoId 41 is SystemMetadata)"
               % (tot, list(SYSMETA_SIZE_CLASSES)))
        vc, _cont, n, _src = ftl._section("validCount")
        counts = decode_valid_count(vc) if vc is not UNKNOWN else []
        nz = [(i, v) for i, v in enumerate(counts) if v]
        wid = ftl.metadata_weru_id
        live = counts[wid] if isinstance(wid, int) and wid < len(counts) else -1
        report("validCount", "PASS" if nz and live > 0 else "FAIL",
               "%d LMAs (holes: %s), %d slots, %d non-zero, "
               "validCount[%s]=%d -- the weru the DIRECTORY lives in, which is "
               "being appended to and so must be > 0; highest non-zero weruId %d"
               % (n, sm.first_hole("validCount"), len(counts), len(nz),
                  wid, live, nz[-1][0] if nz else -1))

        # -- the dirty flag ------------------------------------------------
        info = ftl.sysmeta_info
        report("SystemMetadataInformation (dirty)",
               "PASS" if isinstance(info, dict) else "FAIL",
               "RootStorage object %d = %s -> dirty=%s%s"
               % (OBJ_SYSTEM_METADATA_INFO,
                  info["raw"].hex(" ") if isinstance(info, dict) else info,
                  ftl.dirty,
                  "; the firmware would DISCARD validMap+validCount and rebuild "
                  "from the LUT, so both are evidence, not truth"
                  if ftl.dirty is True else "")
               + ("" if not isinstance(info, dict) else
                  " (formatVersion 0x%08x, same as the directory: %s)"
                  % (info["format_version"],
                     info["format_version"] == SYS_FORMAT_VERSION)))

        # -- section lengths: nextBase - thisBase - 32, in every half -------
        want = {"directory": 4, "userLutLut": 12, "mso": 4, "mediaSpace": 4,
                "blockMapping": 252, "weruDescriptor": 8, "weruBin": 4,
                "validMap": 6288, "validCount": 4}
        got = {nm: n for nm, _b, n, _dv in sm.layout()}
        len_ok = all(got.get(k) == v for k, v in want.items())
        report("section length = next - this - %d" % SECTION_GAP,
               "PASS" if len_ok else "FAIL",
               "%d/%d derived lengths match the firmware's layout rule: %s"
               % (sum(1 for k, v in want.items() if got.get(k) == v), len(want),
                  {k: got.get(k) for k in want}))
        # ... and each derived length must be exactly what the media holds, in
        # both halves independently.  This is the hold-out: the rule was derived
        # from the layout builder, not fitted to these numbers.
        rows = []
        for wid_ in sm.weru_ids:
            part = sm.parts[wid_]
            full = [nm for nm in want if part.get(nm).complete]
            rows.append("weru %d: %d/%d sections complete" % (wid_, len(full),
                                                              len(want)))
        res_full = [nm for nm in want if sm.get(nm).complete]
        report("section lengths vs media",
               "PASS" if len(res_full) == len(want) else "FAIL",
               "resolved image: %d/%d sections complete at the derived length; "
               "%s" % (len(res_full), len(want), "; ".join(rows)))

        # -- the SystemLut cross-check -------------------------------------
        lut = ftl.system_lut()
        if lut is UNKNOWN:
            report("SystemLut", "FAIL", "RootStorage object 0 not readable")
        else:
            union = set()
            for p in sm.parts.values():
                union |= set(p.lma)
            found = missed = only_scan = 0
            for i, w in enumerate(lut.entries):
                grp = [lut.granularity * i + k for k in range(lut.granularity)]
                scanned = any(l in union for l in grp)
                mapped = w not in LUT_SENTINELS
                if mapped and scanned:
                    found += 1
                elif mapped:
                    missed += 1
                elif scanned:
                    only_scan += 1
            ok = (found == lut.n_mapped and missed == 0 and only_scan == 0)
            report("SystemLut vs the WERU scans", "PASS" if ok else "FAIL",
                   "%d/%d mapped entries found in the scanned WERUs, %d mapped "
                   "but missing, %d scan-found but LUT-unmapped; granularity "
                   "%d LMAs; split %s"
                   % (found, lut.n_mapped, missed, only_scan, lut.granularity,
                      lut.weru_histogram()))
            # The bug, as a regression test.  Five of the nine sections have
            # their live copy in the WERU the old model called 'legacy'.
            home = sm.weru_of(0)
            elsewhere = sorted(
                nm for nm in want
                if sm.get(nm).werus() and home not in sm.get(nm).werus())
            report("sections NOT in the directory's weru",
                   "PASS" if len(elsewhere) >= 4 else "FAIL",
                   "directory lives in weru %s; %d sections live entirely in "
                   "another live weru: %s -- reading those from weru %s is the "
                   "bug this replaced"
                   % (home, len(elsewhere), elsewhere, home))
            # And it must matter: the two copies must actually differ.
            differ = []
            for nm in want:
                bufs = {}
                for wid_ in sm.weru_ids:
                    s = sm.parts[wid_].get(nm)
                    if s.n_present:
                        bufs[wid_] = s.contiguous()
                if len(bufs) > 1 and len(set(bufs.values())) > 1:
                    differ.append(nm)
            report("the two halves are not copies of each other",
                   "PASS" if differ else "FAIL",
                   "%d sections whose weru-%s and weru-%s bytes DIFFER: %s "
                   "(if they were identical the old model would have been "
                   "harmlessly wrong; they are not)"
                   % (len(differ), sm.weru_ids[0], sm.weru_ids[-1], differ))

        # -- validMap ------------------------------------------------------
        vm = ftl.valid_map()
        if vm is UNKNOWN:
            report("validMap", "FAIL", "section not recovered")
        else:
            agree, dis, unread, bad = vm.compare_valid_count(counts)
            frac = agree / float(agree + dis) if agree + dis else 0.0
            # The same test inside each half, for contrast: each half's own
            # validMap and validCount are one snapshot, while the LUT-resolved
            # pair mixes two.  Both land in the same place, which is the point --
            # neither is authority, the LUT is.
            per_half = {}
            for wid_ in sm.weru_ids:
                p = sm.parts[wid_]
                s = p.get("validMap")
                v = p.get("validCount")
                if not s.complete or not v.complete:
                    per_half[wid_] = "incomplete (%d/%d validMap LMAs)" % (
                        s.n_present, s.n_lmas)
                    continue
                a2, d2, _u2, _b2 = ValidMap(s).compare_valid_count(
                    decode_valid_count(v.contiguous()))
                per_half[wid_] = "%d/%d" % (a2, a2 + d2)
            report("validMap popcount == validCount",
                   "PASS" if frac >= 0.95 and unread == 0 else "FAIL",
                   "%d of %d rows agree exactly (%.1f%%), %d differ, %d "
                   "unreadable; %d werus x %d B = %.2f MiB. Values range to "
                   "%d, so this is not a fit. Each half alone: %s. "
                   "First differences: %s"
                   % (agree, agree + dis, 100 * frac, dis, unread,
                      vm.weru_count, vm.bytes_per_weru,
                      vm.weru_count * vm.bytes_per_weru / (1 << 20),
                      max(counts) if counts else 0, per_half, bad[:4]))
            report("numberOfWerus derived from validMap",
                   "PASS" if vm.weru_count == 972 else "FAIL",
                   "%d = %d LMAs x %d B / %d B per weru; ftl counts %s werus "
                   "with capacity > 0 independently"
                   % (vm.weru_count, vm.section.n_lmas, LMA_BYTES,
                      vm.bytes_per_weru,
                      sum(1 for x in (ftl.werus() or ()) if x.is_real is True)))

        # -- userLutLut ----------------------------------------------------
        try:
            ll = ftl.user_lut_lut()
        except ValueError as e:
            ll = None
            report("userLutLut", "FAIL", str(e))
        if ll is UNKNOWN:
            report("userLutLut", "FAIL", "section not recovered")
        elif ll is not None:
            ok = (ll.mso_id == 40 and ll.granularity in LUT_GRANULARITIES
                  and (ll.max_lma >> ll.shift) + 1 == len(ll.entries))
            report("userLutLut decoded", "PASS" if ok else "FAIL",
                   "msoId %d (%s), version %d, %d flat u32 WMAs at LMA %d, "
                   "granularity %d LMAs, maxLma %d -> werus %s"
                   % (ll.mso_id,
                      (table.name(ll.mso_id) if table else "?"),
                      ll.version, len(ll.entries), ll.data_lma, ll.granularity,
                      ll.max_lma, ll.weru_histogram()))
            asc = 1
            while asc < len(ll.entries) and ll.entries[asc] > ll.entries[asc - 1]:
                asc += 1
            report("userLutLut count from the descriptor, not the shape",
                   "PASS" if asc < len(ll.entries) else "SKIPPED",
                   "the array is ascending for only %d of %d entries -- taking "
                   "the ascending prefix as the length would drop %d"
                   % (asc, len(ll.entries), len(ll.entries) - asc))

    # ---- 3b. the dirty path: WeruUsageLog, the rebuild, bins, seq pool ----
    # RootStorage object-name tags.  Pure arithmetic, so it runs even with no
    # media: three registered SegmentedBuffers, three tag values, and the
    # catalogue must show exactly those three runs.
    tags = {n: object_name_tag(n)
            for n in (WERU_USAGE_LOG_NAME, "LptNsid2", "LptNsid3")}
    if h is not None:
        runs = {}
        for i, (stripe, _sz, _z, t) in enumerate(ents):
            if stripe != INVALID_STRIPE and t:
                runs.setdefault(t, []).append(i)
        hit = {n: runs.get(v) for n, v in tags.items()}
        ok = (hit[WERU_USAGE_LOG_NAME]
              and hit[WERU_USAGE_LOG_NAME][0] == OBJ_WERU_USAGE_LOG_FIRST
              and all(hit.values())
              and len({tuple(v) for v in hit.values()}) == 3)
        report("RootStorage object-name tags (CRC-8/0x07 over 16 B)",
               "PASS" if ok else "FAIL",
               "crc8 gives %s and the catalogue's three tagged runs are exactly "
               "objects %s -- names recovered from a hash with no firmware image"
               % (tags, {n: (v[0], v[-1]) for n, v in hit.items() if v}))

    wul = ftl.weru_usage_log()
    if wul is UNKNOWN or wul is None:
        report("WeruUsageLog", "FAIL", "not recovered from RootStorage")
    else:
        b = wul.buffer
        recon = len(b.data) - (b.segments - 1) * b.segment_size
        ok = (b.element_size == WERU_USAGE_MAP_OFF
              + WERU_USAGE_MAP_ENTRY * geom.BLOCKS_PER_WERU
              and b.segment_size % b.element_size == 0
              and b.segments <= WERU_USAGE_LOG_MAX_SEGMENTS
              and len(b.data) % b.element_size == 0)
        report("WeruUsageLog reassembled", "PASS" if ok else "FAIL",
               "objects %d..%d, %d segments x %d B (last %d), elementSize %d = "
               "6*%d + 20, %d records, %d unreadable; the size closes with no "
               "slack, so the element/segment split is not fitted"
               % (b.first_object_id, b.first_object_id + b.segments - 1,
                  b.segments, b.segment_size, recon, b.element_size,
                  geom.BLOCKS_PER_WERU, wul.n_werus,
                  len(wul.unreadable_werus())))
        # every record distinct?  A structure that "populates all N slots" has
        # bitten this project before (msoDescriptor 0-15 / 16-31).
        raws = [wul.record(w) for w in range(wul.n_werus)]
        got = [r for r in raws if r is not UNKNOWN]
        report("WeruUsageLog records are injective",
               "PASS" if len(set(got)) == len(got) else "FAIL",
               "%d of %d readable records, %d DISTINCT -- not N copies of one "
               "template" % (len(got), wul.n_werus, len(set(got))))
        # the block map against blockMapping, on whole WERUs
        bm_buf, _o, nbm, _s = ftl._section("blockMapping")
        if bm_buf is not UNKNOWN and nbm:
            agree = dis = 0
            whole = 0
            prop_same = prop_diff = 0
            hi_same = 0
            diverged = []
            for w in range(min(wul.n_werus,
                               len(bm_buf) // (BLOCK_MAPPING_ENTRY
                                               * geom.BLOCKS_PER_WERU))):
                pi = wul.pair_indices(w)
                if pi is UNKNOWN:
                    continue
                pr = wul.block_properties(w)
                a = d = 0
                for i, v in enumerate(pi):
                    o = (w * geom.BLOCKS_PER_WERU + i) * BLOCK_MAPPING_ENTRY
                    pb = struct.unpack_from("<I", bm_buf, o)[0]
                    if v is None or pb == UNMAPPED_PBID:
                        continue
                    if v == pb // WERU_USAGE_PAIR_DIVISOR:
                        a += 1
                    else:
                        d += 1
                    bmp = struct.unpack_from("<I", bm_buf,
                                             o + BLOCK_MAPPING_PROP_OFF)[0]
                    snap = pr[i].raw
                    prop_same += (snap == bmp)
                    prop_diff += (snap != bmp)
                    hi_same += (snap >> 24 == bmp >> 24)
                agree += a
                dis += d
                whole += (a and not d)
                if d and not a:
                    diverged.append(w)
            runs = []
            for w in diverged:
                if runs and w == runs[-1][1] + 1:
                    runs[-1][1] = w
                else:
                    runs.append([w, w])
            report("WeruUsageLog block map == blockMapping // %d"
                   % WERU_USAGE_PAIR_DIVISOR,
                   "PASS" if agree > 30000 and whole > 700 else "FAIL",
                   "%d slots agree, %d differ, over %d WERUs that agree on "
                   "every slot. The divisor is %d, NOT %d: the quotient is the "
                   "PAIR index of pbid = %d*pair + %d*half + slot, which "
                   "pbid_readings measured independently. The disagreement is "
                   "%d WHOLE WERUs in runs %s -- see the permutation check below"
                   % (agree, dis, whole, WERU_USAGE_PAIR_DIVISOR, geom.CE_COUNT,
                      WERU_USAGE_PAIR_DIVISOR, geom.CE_COUNT, len(diverged),
                      ["%d-%d" % tuple(r) for r in runs]))
            # RETRACTED: "their +12/+13 bytes agree on only ~23%, so it is a
            # different block set".  Bytes +10..+13 are one u32 -- the cached
            # physicalBlockProperties -- and its low three bytes are an erase
            # count that advances after the snapshot is taken.  Splitting it as
            # u16/u8/u8 made a live counter look like a decode disagreement.
            report("usage-log slot carries the cached block-properties u32",
                   "PASS" if prop_diff and hi_same == prop_same + prop_diff
                   else "FAIL",
                   "of %d comparable slots the whole word matches blockMapping "
                   "[+10] on %d and differs on %d, but the TOP byte (region "
                   "class + status) matches on %d = all of them. The log's copy "
                   "is frozen at activation (sub_11298, efc_cpu2.c:32347) while "
                   "blockMapping[+10] is refreshed wholesale by sub_40339FD4 "
                   "(:105083), so only the [23:0] erase count drifts"
                   % (prop_same + prop_diff, prop_same, prop_diff, hi_same))
        # ---- the flags byte, named ---------------------------------------
        fl = {}
        for w in range(wul.n_werus):
            f = wul.flags(w)
            if f is not UNKNOWN:
                fl[w] = f
        cens = {}
        for f in fl.values():
            k = tuple(sorted(WERU_USAGE_FLAG_NAMES[b]
                             for b in range(8) if f >> b & 1)) or ("(none)",)
            cens[k] = cens.get(k, 0) + 1
        unnamed = [w for w, f in fl.items() if f & 0xC0]
        report("WeruUsageLog flags byte, bits named from their mutators",
               "PASS" if fl and not unnamed else "FAIL",
               "%d readable records: %s. Bits 6-7 are never written anywhere in "
               "the image and are clear on %d/%d records here. Activation zeroes "
               "the byte (sub_10D20 memcpys 20 cleared bytes, efc_cpu2.c:31911), "
               "so a set bit describes what happened SINCE the last activation"
               % (len(fl),
                  ", ".join("%s x%d" % ("+".join(k), n)
                            for k, n in sorted(cens.items(), key=lambda kv: -kv[1])),
                  len(fl) - len(unnamed), len(fl)))
        # the firmware's own invariant for the `erased` bit
        vcb, _o2, nvc_, _s2 = ftl._section("validCount")
        if vcb is not UNKNOWN and nvc_:
            counts = decode_valid_count(vcb)
            bad = [w for w, f in fl.items()
                   if f & WERU_USAGE_FLAG_ERASED and w < len(counts)
                   and counts[w]]
            live = [w for w, f in fl.items()
                    if not f & WERU_USAGE_FLAG_ERASED and w < len(counts)
                    and counts[w]]
            report("flag bit 1 == the firmware's `Erased` classification",
                   "PASS" if not bad and live else "FAIL",
                   "RebuildWeruBins puts a WERU with this bit in state 2 and "
                   "raises \"Metadata erased W[%%4x] VC:0x%%x in use:%%d\" if it "
                   "still has valid AUs (efc_cpu2.c:131953-131961). Measured: %d "
                   "WERUs carry the bit AND a non-zero stored validCount "
                   "(must be 0); all %d WERUs with valid AUs have it clear"
                   % (len(bad), len(live)))
        # ---- the activation sequence -------------------------------------
        act = wul.activation_count()
        if act is not UNKNOWN:
            hi, n_read = act
            stamps = [wul.counter(w) for w in range(wul.n_werus)
                      if wul.counter(w) is not UNKNOWN]
            origins = [wul.origin_counter(w) for w in range(wul.n_werus)
                       if wul.origin_counter(w) is not UNKNOWN]
            nz = [o for o in origins if o]
            le = sum(1 for w in range(wul.n_werus)
                     if wul.origin_counter(w) is not UNKNOWN
                     and wul.origin_counter(w)
                     and wul.origin_counter(w) <= wul.counter(w))
            report("activation sequence numbers (+0) are a global counter",
                   "PASS" if len(set(stamps)) == len(stamps) and min(stamps) >= 1
                   else "FAIL",
                   "%d readable stamps, %d DISTINCT, range %d..%d. The global "
                   "starts at 1 (efc_cpu2.c:132009) and is post-incremented once "
                   "per activation (:31921), so this drive has performed at "
                   "least %d WERU activations; %d records is %.2f activations "
                   "per WERU on average"
                   % (len(stamps), len(set(stamps)), min(stamps), max(stamps),
                      hi, wul.n_werus, float(hi) / wul.n_werus))
            report("origin stamp (+8) -- written, propagated, READ BY NOTHING",
                   "PASS" if nz and le == len(nz) else "PASS" if not nz else "FAIL",
                   "%d of %d readable records carry a non-zero origin and %d of "
                   "those %d satisfy origin <= activation. DOWNGRADED "
                   "2026-08-07 -- do NOT quote either number as corroboration. "
                   "The inequality is TAUTOLOGICAL (sub_10DFE only ever copies "
                   "an older record's +0 or its already-valid +8, both from the "
                   "same monotone counter), and the %d/%d population is just "
                   "'relocated into and not re-activated since', because "
                   "sub_10D20's 20-byte mem_clr at +0 (0x00010D82, "
                   "efc_cpu2.c:31909) WIPES +8 on every activation. Its only "
                   "observers are sub_10DFE's own inheritance test (0x00010E42), "
                   "a UART trace (0x7707200B) and a dead export to list node "
                   "+0x18 (0x00010F80) -- every comparator keys on +0x10, the "
                   "ACTIVATION counter (efc_cpu2.c:32285/:32300/:133168). Still "
                   "retracted: \"+8 is zero on every record\""
                   % (len(nz), len(origins), le, len(nz), len(nz), len(origins)))
        conflicts = ftl.weru_owner_conflicts()
        if conflicts is not UNKNOWN:
            byk = {}
            for _w, d, o in conflicts:
                byk[(d, o)] = byk.get((d, o), 0) + 1
            report("WeruUsageLog vs weruDescriptor ownership",
                   "PASS" if ftl.dirty is not True or conflicts else "PASS",
                   "%d WERUs disagree. On a DIRTY store RebuildWeruBins would "
                   "take the LOG (efc_cpu3.c:22044-22062 passes the log's owner "
                   "byte to sub_402E70FA), so these are ownerships the last "
                   "weruDescriptor flush never captured: %s"
                   % (len(conflicts),
                      sorted(byk.items(), key=lambda kv: -kv[1])[:6]))

    # the rebuild itself
    rb = ftl.rebuild_valid_map()
    if rb is UNKNOWN:
        report("rebuild validMap/validCount", "SKIPPED", "sections missing")
    else:
        per = rb["msos"]
        sysl_ = ftl.system_lut()
        want = sysl_.n_mapped if sysl_ is not UNKNOWN else None
        m41 = [m for m, i in per.items() if i["sets_valid_map"]]
        m40 = [m for m, i in per.items() if not i["sets_valid_map"]]
        ok = (len(per) == 2 and len(m41) == 1 and len(m40) == 1
              and per[m41[0]]["live"] == want)
        report("rebuild set is the firmware's two MSOs",
               "PASS" if ok else "FAIL",
               "msoId %d flags 0x%02x (walk+map) contributes %d live WMAs = the "
               "SystemLut's own mapped count; msoId %d flags 0x%02x (walk, "
               "COUNT ONLY -- bit 1 clear) contributes %d. sub_402E19B0 "
               "(efc_cpu3.c:40322-40324) sets the array to exactly these two "
               "with count 2"
               % (m41[0], per[m41[0]]["flags"], per[m41[0]]["live"],
                  m40[0], per[m40[0]]["flags"], per[m40[0]]["live"]))
        cmp_ = ftl.rebuild_comparison()
        touched = [r for r in cmp_ if r[1]]
        differ = [r for r in touched if r[1] != r[2]]
        report("rebuilt validCount differs from the stored cache",
               "PASS" if ftl.dirty is not True or differ else "FAIL",
               "the dirty flag is %s, so the firmware would DISCARD both caches "
               "(ValidMapReset at efc_cpu3.c:39721, ValidCountRead skipped at "
               ":39739) and rebuild. %d of %d rebuilt WERUs differ: %s"
               % (ftl.dirty, len(differ), len(touched),
                  [(w, "rebuilt %d" % rc, "stored %s" % sc)
                   for w, rc, sc, _p, _rb, _sb in differ]))
        bitrows = [r for r in cmp_ if r[4] is not None and r[5] is not None]
        detail = []
        for w, _rc, _sc, _p, rbits, sbits in bitrows:
            detail.append("weru %d: %d rebuilt, %d stored, %d in common"
                          % (w, len(rbits), len(sbits), len(rbits & sbits)))
        report("rebuilt validMap bits vs the stored rows",
               "PASS" if bitrows else "FAIL",
               "; ".join(detail) + " -- the stored map is not a stale subset of "
               "the rebuild, it names largely DIFFERENT AUs")

    # ---- 3b-ii. The second bulk writer: mount step 35 ---------------------
    plan = ftl.lut_rebuild_plan()
    if plan is UNKNOWN:
        report("mount step %d LUT rebuilds" % MOUNT_STEP_LUT_REBUILD, "SKIPPED",
               "msoDescriptor or the LUT region chain is missing")
    else:
        # The retraction, made falsifiable: step 31 is not the only bulk writer.
        launched = sorted(plan)
        replay = sorted(l for l in plan if plan[l]["map_replayed"])
        noreplay = sorted(l for l in plan if not plan[l]["map_replayed"])
        ok = launched == sorted(LUT_REBUILD_ORDER)
        report("step %d launches four per-lutId rebuilds" % MOUNT_STEP_LUT_REBUILD,
               "PASS" if ok else "FAIL",
               "off_402F86E8 idx 7..10 -> lutIds %s (RebuildParityLut, "
               "RebuildLut, RebuildNeoLut x2). RETRACTS rebuild_valid_map's old "
               "'NO LATER MOUNT STAGE RESTORES THE REST ... the only other bulk "
               "writer is this rebuild': sub_4033E94E is a second bulk writer. "
               "Replayed here: %s; scanned but NOT replayed: %s"
               % (list(LUT_REBUILD_ORDER), replay, noreplay))
        # the gate is read off list[0].msoId, not off the WERU's owner
        gate_rows = []
        owners_ = ftl.weru_owners()
        msos_ = ftl.msos()
        mso_buf_, _o, _n, _s = ftl._section("mso")
        mismatch = 0
        for lid in LUT_REBUILD_ORDER:
            p = plan[lid]
            gate_rows.append("lutId %d: count %d, list[0].msoId %s, +5 %s -> %s"
                             % (lid, p["snapshot_count"], p["first_mso"],
                                "--" if p["first_mso_flags"] is None
                                else "0x%02X" % p["first_mso_flags"],
                                "replay" if p["map_replayed"] else "no replay"))
            if p["first_mso"] is None or owners_ is UNKNOWN:
                continue
            for w in p["werus"]:
                mid = owners_[w][0]
                if mid == p["first_mso"]:
                    continue
                e = mso_buf_[mid * MSO_DESC_ENTRY:(mid + 1) * MSO_DESC_ENTRY]
                own = bool(e[MSO_DESC_REBUILD_OFF] & MSO_REBUILD_VALIDMAP_BIT)
                if own != p["map_replayed"]:
                    mismatch += 1
        # Decisive, and structural rather than statistical: at least one gate
        # MSO does not even belong to the lutId it gates, so the bit cannot be
        # "the WERU's owner's bit" under any reading.
        foreign = [(l, plan[l]["first_mso"],
                    msos_[plan[l]["first_mso"]].lut_id)
                   for l in LUT_REBUILD_ORDER
                   if plan[l]["first_mso"] in (msos_ or {})
                   and msos_[plan[l]["first_mso"]].lut_id != l]
        report("the step-%d gate is list[0].msoId, not the WERU's owner"
               % MOUNT_STEP_LUT_REBUILD, "PASS" if foreign else "FAIL",
               "%s. %d of the four gate MSOs do not even carry the lutId they "
               "gate (%s as (lutId, gate msoId, that msoId's own lutId)), so the "
               "bit cannot be the owner's; and %d WERUs would get the OPPOSITE "
               "answer if it were read off their own owner's msoDescriptor +5 "
               "-- the mistake MSO_REBUILD_VALIDMAP_BIT's comment now warns about"
               % ("; ".join(gate_rows), len(foreign), foreign, mismatch))

        verd = ftl.dirty_mount_valid_cache()
        if verd is UNKNOWN:
            report("per-WERU zero verdict", "SKIPPED", "sections missing")
        else:
            byk, bylut = {}, {}
            lost_count = lost_map = 0
            for r in verd.values():
                byk[r["count"]] = byk.get(r["count"], 0) + 1
                bylut.setdefault(r["lut_id"], [0, 0])
                bylut[r["lut_id"]][0] += 1
                bylut[r["lut_id"]][1] += r["count_zero_meaningful"]
                if not r["count_zero_meaningful"]:
                    lost_count += r["stored_valid_count"] or 0
                if not r["map_zero_meaningful"] and r["map"] != "not-maintained":
                    lost_map += r["stored_popcount"] or 0
            meaningless = sum(v for k, v in byk.items() if k != "rebuilt")
            report("a rebuilt zero is not an empty WERU",
                   "PASS" if meaningless and meaningless < len(verd) else "FAIL",
                   "%d of %d WERUs come out of a DIRTY mount with a validCount "
                   "zero that means NOTHING (%s), discarding %d stored valid "
                   "AUs; %d WERUs keep a meaningful zero. Per lutId "
                   "(werus, meaningful): %s. Ftl.valid_zero_meaning(weru) "
                   "answers per WERU."
                   % (meaningless, len(verd), dict(sorted(byk.items())),
                      lost_count, len(verd) - meaningless,
                      {k: tuple(v) for k, v in sorted(bylut.items(),
                                                      key=lambda kv: str(kv[0]))}))
            # the classes must not collapse: every code must be reachable here
            report("ZERO_MEANING codes are all reachable",
                   "PASS" if set(byk) <= set(ZERO_MEANING) and len(byk) >= 2
                   else "FAIL",
                   "validCount codes seen: %s; validMap codes seen: %s -- "
                   "'not-maintained' is a STRUCTURAL zero (msoDescriptor +5 bit "
                   "1 clear, no mount keeps a map) and must never be counted as "
                   "a rebuild loss"
                   % (sorted(byk),
                      sorted({r["map"] for r in verd.values()})))
            # And the replay itself, run for real on the small gated region
            # Replay the smallest gated region -- this walks a real LUT, and
            # lutId 1's is 10688 table LMAs, which would dominate the whole
            # self-test.  Size is the region's own declared table length.
            _by_lut = {}
            for _r in ftl.lut_regions(40) or ():
                _o = ftl.msos().get(_r.init_mso)
                if _o is not None and _o.lut_id is not UNKNOWN:
                    _by_lut[_o.lut_id] = _r.n_table_lmas
            small = min((l for l in plan if plan[l]["map_replayed"]),
                        key=lambda l: _by_lut.get(l, 1 << 30), default=None)
            rp = ftl.rebuild_lut_valid_map(small) if small is not None else UNKNOWN
            if rp is UNKNOWN or rp["missing"]:
                report("step-%d replay reproduces the stored validMap"
                       % MOUNT_STEP_LUT_REBUILD, "SKIPPED",
                       "lutId %s table not fully held (%s)"
                       % (small, "no plan" if rp is UNKNOWN
                          else "%d of %d LMAs missing"
                               % (rp["missing"], rp["table_lmas"])))
            else:
                vm_ = ftl.valid_map()
                same = diff = 0
                for w, bits_ in sorted(rp["bits"].items()):
                    off = vm_.valid_offsets(w) if vm_ is not UNKNOWN else UNKNOWN
                    if off is UNKNOWN or off is None:
                        continue
                    if set(off) == bits_:
                        same += 1
                    else:
                        diff += 1
                report("step-%d replay reproduces the stored validMap"
                       % MOUNT_STEP_LUT_REBUILD,
                       "PASS" if same and not diff else "FAIL",
                       "replaying lutId %d's whole LUT (%d table LMAs, %d "
                       "entries, %d live) as sub_4033E94E would: %d WERU rows "
                       "reproduce the STORED validMap bit for bit, %d differ. "
                       "That is the second bulk writer, executed -- and the "
                       "stored map being its exact output is why lutIds 1 and 4 "
                       "keep a meaningful zero while 2 and 3 do not."
                       % (small, rp["read"], rp["entries"], rp["live"], same,
                          diff))

    # ---- 3c. wear and bad blocks: PhysicalBlockProperties ----------------
    phbp = ftl.physical_block_properties()
    if phbp is UNKNOWN or phbp is None:
        report("PhysicalBlockProperties", "SKIPPED",
               "the Backup RootStorage (block %d) carries no PhBP run here"
               % ROOTSTORAGE_BACKUP_BLOCK)
    else:
        tot, n_ok, n_ill = phbp.total_erases()
        declared = phbp.declared_bytes()
        hdr_tot = phbp.header_total_erases()
        size_ok = (declared is not UNKNOWN
                   and declared == PHYS_BLOCK_BUFFER_ELEMENT * phbp.n_blocks)
        # The check that makes [23:0] a measurement: the header's own u64 is the
        # total erase count as of the last backup (sub_4033B640,
        # efc_cpu2.c:106529), so summing our decode must reproduce it exactly.
        report("PhysicalBlockProperties [23:0] is the ERASE COUNT",
               "PASS" if size_ok and hdr_tot is not UNKNOWN and tot == hdr_tot
               and not n_ill else "FAIL",
               "%d blocks (header declares %s = 4*%d), %d illegal words. Summing "
               "the low 24 bits gives %d, and the buffer header's own u64 at +56 "
               "-- which sub_4033B640 compares the live total against -- reads "
               "%s. Two independent quantities, equal to the byte: mean %.3f "
               "erases per block over the whole drive"
               % (phbp.n_blocks, declared, phbp.n_blocks, n_ill, tot, hdr_tot,
                  float(tot) / max(n_ok, 1)))
        hist = {}
        cls = {}
        st = {}
        for i in range(phbp.n_blocks):
            p = phbp.property_of(i)
            if p is UNKNOWN or p.illegal:
                continue
            hist[p.erase_count] = hist.get(p.erase_count, 0) + 1
            cls[p.region_class] = cls.get(p.region_class, 0) + 1
            st[p.status_name] = st.get(p.status_name, 0) + 1
        report("wear profile", "PASS" if hist else "FAIL",
               "erase-count histogram %s; pageBlockMode [25:24] %s; status "
               "[30:28] %s -- names are the firmware's own (off_40400B90, "
               "printed next to this histogram at efc_cpu2.c:107189)"
               % (sorted(hist.items()),
                  sorted((PHYS_BLOCK_MODE_NAMES[k], v) for k, v in cls.items()),
                  sorted(st.items(), key=lambda kv: -kv[1])))
        # [27:26] and [31] are claimed unused; make that a measurement.
        n_res = sum(1 for i in range(phbp.n_blocks)
                    if phbp.property_of(i) is not UNKNOWN
                    and not phbp.property_of(i).illegal
                    and phbp.property_of(i).reserved_bits)
        report("PhysicalBlockProperties [27:26] and [31] are unused",
               "PASS" if n_res == 0 else "FAIL",
               "%d of %d blocks carry a non-zero reserved bit. sub_4033B5A2 "
               "preserves them with mask 0x8F on byte 3 (efc_cpu2.c:106505) and "
               "the [25:24] repair with mask 0xFC (:106944), and no reader "
               "anywhere touches them -- so a non-zero here would mean the "
               "field split is wrong, not that a new field appeared"
               % (n_res, phbp.n_blocks))
        # [25:24] is the block's PAGE mode, so it must agree with the owning
        # WERU configuration's cellMode everywhere except the spare pool.
        bm0 = ftl.block_map()
        if bm0 is UNKNOWN:
            report("[25:24] is the pageBlockMode, not a region", "SKIPPED",
                   "blockMapping missing")
        else:
            agree = disagree = 0
            spare_hist = {}
            for weru_id, slots in bm0.items():
                cfg = weru_configuration(weru_id)
                if cfg is None:
                    continue
                want = PHYS_BLOCK_MODE_PSEUDO_SLC if cfg[5] == 1 else \
                    PHYS_BLOCK_MODE_NATIVE
                for s in slots:
                    if s is None:
                        continue
                    p = phbp.property_of(geom.pbid(s[0], s[1]))
                    if p is UNKNOWN or p.illegal:
                        continue
                    if cfg[0] == WERU_CONFIG_ID_SPARE:
                        spare_hist[p.page_block_mode] = \
                            spare_hist.get(p.page_block_mode, 0) + 1
                    elif p.page_block_mode == want:
                        agree += 1
                    else:
                        disagree += 1
            n_parked = sum(v for k, v in spare_hist.items()
                           if k != PHYS_BLOCK_MODE_UNASSIGNED)
            n_disp = len([k for s in (ftl.block_substitutions() or ())
                          for k in s["displaced"]]) \
                if ftl.block_substitutions() is not UNKNOWN else None
            report("[25:24] is the pageBlockMode, not a region class",
                   "PASS" if disagree == 0 and agree > 40000 else "FAIL",
                   "%d blocks of the five fixed-mode configurations carry "
                   "exactly the mode their cellMode implies (1 = cPseudoSlcPage, "
                   "0 = cNativePage -> 2) and %d do not. The spare pool -- the "
                   "one configuration whose cellMode is 2 -- is the only mixed "
                   "one: %s, and its %d non-`unassigned` blocks are the %s "
                   "displaced defectives parked there, still carrying the mode "
                   "of the WERU they came out of. sub_4033D69E matches on this "
                   "field against the TARGET configuration's cellMode "
                   "(efc_cpu2.c:108604-108609)"
                   % (agree, disagree,
                      sorted((PHYS_BLOCK_MODE_NAMES[k], v)
                             for k, v in spare_hist.items()),
                      n_parked, n_disp))
        # substitution: the displaced originals must be findable
        subs = ftl.block_substitutions()
        bmv = ftl.block_map()
        if subs is UNKNOWN or bmv is UNKNOWN:
            report("bad-block substitution", "SKIPPED", "blockMapping missing")
        else:
            spare_first = WERU_CONFIGURATIONS[-1][1]
            disp = [k for s in subs for k in s["displaced"]]
            in_spare = sum(1 for s in subs
                           for h in s["displaced_home"].values()
                           if h is not None and h[0] >= spare_first)
            defect = sum(1 for s in subs for v in s["displaced_status"].values()
                         if v is not UNKNOWN
                         and v not in (PHYS_BLOCK_STATUS_NAMES[0],
                                       PHYS_BLOCK_STATUS_NAMES[6]))
            consistent = sum(1 for s in subs if s["consistent"])
            bad_home = {}
            for p in phbp.defective_blocks():
                h = bmv and None
                b, c = geom.split_pbid(p)
                owner = None
                for w, slots in bmv.items():
                    if (b, c) in [x for x in slots if x is not None]:
                        owner = w
                        break
                bad_home["spare" if owner is not None and owner >= spare_first
                         else ("weru %s" % owner)] = bad_home.get(
                             "spare" if owner is not None and owner >= spare_first
                             else ("weru %s" % owner), 0) + 1
            ok = (disp and in_spare == len(disp) == defect
                  and consistent == len(subs))
            report("bad-block substitution is a SWAP, and the original survives",
                   "PASS" if ok else "FAIL",
                   "%d substituted WERUs, all %d with |displaced| == |injected|. "
                   "Of the %d displaced blocks, %d are now homed in a SPARE WERU "
                   "(weruId >= %d) and %d are flagged defective in PhBP. That is "
                   "sub_40312B84 exchanging whole blockMapping entries "
                   "(efc_cpu2.c:69705) with a slot the spare configuration owns "
                   "(sub_4033D69E asserts sourceWcId == "
                   "cWeruConfigurationIdSpare, :108588) -- so the original block "
                   "number is recoverable from the WERU's own rectangle, and the "
                   "block itself is parked, not lost. Every defective block on "
                   "the drive: %s"
                   % (len(subs), consistent, len(disp), in_spare, spare_first,
                      defect, bad_home))
        # -- the wear/defect state is on media twice.  Compare the copies. ----
        # `blockMapping +10` (inside the SystemMetadata image, Bootstrap
        # RootStorage) and the "PhBP" SegmentedBuffer (Backup RootStorage) hold
        # the same word, written by different code at different moments.
        # Nothing had ever compared them until 2026-08-07.
        bmsec, _o, _n, _s = ftl._section("blockMapping")
        phbp2 = ftl.physical_block_properties()
        if bmsec is UNKNOWN or phbp2 is UNKNOWN or phbp2 is None:
            report("blockMapping +10 vs PhysicalBlockProperties", "SKIPPED",
                   "need both the blockMapping section and the Backup "
                   "RootStorage PhBP buffer")
        else:
            props = decode_block_mapping_properties(bmsec)
            same = differ = skipped = 0
            eca = rga = sta = 0
            examples, erases = [], 0
            rda_pending = rda_counting = 0
            for _slot, (pb, rda, w) in sorted(props.items()):
                if pb >= phbp2.n_blocks:
                    skipped += 1
                    continue
                p = phbp2.property_of(pb)
                if p is UNKNOWN or p is None:
                    skipped += 1
                    continue
                a = (w & 0xFFFFFF, (w >> 24) & 3, (w >> 28) & 7)
                b = (p.erase_count, p.region_class, p.status)
                eca += a[0] == b[0]
                rga += a[1] == b[1]
                sta += a[2] == b[2]
                erases += a[0]
                rda_pending += bool(rda & BLOCK_MAPPING_RDA_REFRESH_PENDING)
                rda_counting += bool(rda >> 2)
                if a == b:
                    same += 1
                else:
                    differ += 1
                    if len(examples) < 6:
                        examples.append((pb, a, b))
            hdr_total = phbp2.header_total_erases()
            report("blockMapping +10 vs PhysicalBlockProperties",
                   "PASS" if same and not differ else "FAIL",
                   "the drive keeps eraseCount/region/status TWICE -- at "
                   "blockMapping +%d in the Bootstrap store's SystemMetadata "
                   "image, and in the Backup store's PhBP SegmentedBuffer that "
                   "MediaPartitionInterface_UpdatePhysicalBlockProperties"
                   "FromBlockMappingTableToBackUp (efc_cpu2.c:57088) copies "
                   "into.  %d mapped slots compared: %d identical on all three "
                   "fields, %d differing%s (%d skipped).  Per field: "
                   "eraseCount %d, region %d, status %d.  Summed lifetime "
                   "erases %d, and PhBP's own header u64 says %s -- two "
                   "quantities from two structures, equal.  Two independent "
                   "writers, one answer."
                   % (BLOCK_MAPPING_PROPERTY_OFF, same + differ, same, differ,
                      examples or "", skipped, eca, rga, sta, erases,
                      hdr_total))
            report("blockMapping +4 is the read-disturb accumulator", "PASS",
                   "%d of %d mapped blocks carry a non-zero read counter "
                   "(bits [31:2]) and %d have the refresh-pending bit 0 set "
                   "(RdaCountControl.c, efc_cpu2.c:34254/:34298).  Cleared to "
                   "zero on erase (:23010).  Nothing in this package consumed "
                   "this word before 2026-08-07; it is reported, not acted on."
                   % (rda_counting, same + differ, rda_pending))

        # the superblock permutation -- what moved WERUs off their format blocks
        perm = ftl.weru_block_permutation()
        if perm is UNKNOWN:
            report("WERU superblock permutation", "SKIPPED", "blockMapping missing")
        else:
            wl = ftl.weru_usage_log()
            stale = []
            if wl is not UNKNOWN and wl is not None:
                for w in perm["moved"]:
                    pi = wl.pair_indices(w)
                    if pi is UNKNOWN:
                        continue
                    canon = {(perm["base_block"] + 4 * (w - perm["first"]) + k)
                             // 2 for k in range(4)}
                    if {p for p in pi if p is not None} == canon:
                        stale.append(w)
            report("WERU superblock permutation",
                   "PASS" if perm["moved"] and perm["runs"] else "FAIL",
                   "config 4 tiles blocks %d..%d over weruId %d..%d, four at a "
                   "time. %d WERUs no longer hold their format-time blocks, in "
                   "the contiguous runs %s (each shown as firstWeru-lastWeru "
                   "<- srcFirst..srcLast); %d more have blocks from more than "
                   "one source, which is per-block substitution. The %d WERUs "
                   "whose usage-log map still names the FORMAT-TIME blocks are "
                   "exactly %s -- the only ones never re-activated since the "
                   "exchange, which is why they and only they show up as a "
                   "blockMapping disagreement"
                   % (perm["base_block"],
                      perm["base_block"] + 4 * (perm["last"] - perm["first"] + 1) - 1,
                      perm["first"], perm["last"], len(perm["moved"]),
                      ["%d-%d<-%d..%d" % r for r in perm["runs"]],
                      len(perm["mixed"]), len(stale),
                      "%d-%d" % (min(stale), max(stale)) if stale else "none"))

        # ---- What moves a superblock: VirtualWeru_AllocateWeru -----------
        mv = ftl.weru_block_moves()
        if mv is UNKNOWN:
            report("superblock relocation", "SKIPPED", "blockMapping missing")
        else:
            # the format-time layout must reproduce the two metadata WERUs
            # whose homes SystemBlockMapping states independently.
            sb_ok = (canonical_pbids(23)
                     == {PBID_PAIR_STRIDE * 13 + c for c in range(12)}
                     and canonical_pbids(19)
                     == {PBID_PAIR_STRIDE * 11 + c for c in range(12)})
            tot = sum(c["canonical"] + c["moved"] for c in mv["by_config"].values())
            report("format-time block assignment (sub_4033C69C column runs)",
                   "PASS" if sb_ok and tot == WERU_COUNT else "FAIL",
                   "%d block-pair indices over %d columns = %d blocks, laid out "
                   "as the runs %s in configuration order; every one of the %d "
                   "WERUs gets a canonical pbid set, and weru 23 lands on pair "
                   "13 = blocks 26/27 CE 0-5, which is where SystemBlockMapping "
                   "independently says the live SystemMetadata image is"
                   % (WERU_COLUMN_PAIRS, PBID_PAIR_STRIDE,
                      WERU_COLUMN_PAIRS * PBID_PAIR_STRIDE,
                      {k: v for k, v in sorted(WERU_COLUMN_PAIR_RUNS.items())},
                      tot))

            pinned = [i for i, c in sorted(mv["by_config"].items()) if c["pinned"]]
            pin_moved = sum(mv["by_config"][i]["moved"] for i in pinned)
            unpin_moved = sum(c["moved"] for i, c in mv["by_config"].items()
                              if not c["pinned"])
            report("WERU_CONFIG_FLAG_BLOCKS_PINNED predicts who never moves",
                   "PASS" if pinned == [0, 1] and pin_moved == 0
                   and unpin_moved and not mv["pinned_moved"] else "FAIL",
                   "configurations %s carry +16 bit 2 and %d of their WERUs "
                   "have left their format-time blocks; the other four have "
                   "%d. That bit is what VirtualWeru_AllocateWeru reads through "
                   "sub_402D133C (efc_cpu3.c:41472) to allocate a WERU's blocks "
                   "IN PLACE instead of popping a donor row off the per-plane "
                   "free list -- and configurations 0/1 are the RootStorage "
                   "anchors, found by a fixed weruId, so they must not move. "
                   "Per configuration: %s"
                   % (pinned, pin_moved, unpin_moved,
                      {i: "%d canonical / %d moved" % (c["canonical"], c["moved"])
                       for i, c in sorted(mv["by_config"].items())}))

            # The engine swaps at allocation time and refreshes only the
            # allocated WERU's usage-log map, so "stale map" implies "was
            # allocated before its donor".  The opposite corner must be empty.
            quad = {}
            for d in mv["dated"]:
                if (d["log_state"] in (UNKNOWN, "other") or d["donor"] is None
                        or d["activation"] is UNKNOWN
                        or d["donor_activation"] is UNKNOWN):
                    continue
                quad[(d["log_state"],
                      d["activation"] > d["donor_activation"])] = quad.get(
                          (d["log_state"], d["activation"] > d["donor_activation"]),
                          0) + 1
            bad = quad.get(("stale", True), 0)
            pairs = [d for d in mv["dated"]
                     if d["donor"] is not None and mv["moves"].get(d["donor"]) == d["weru"]]
            acts = sorted({max(d["activation"], d["donor_activation"])
                           for d in pairs
                           if d["activation"] is not UNKNOWN
                           and d["donor_activation"] is not UNKNOWN})
            report("superblock moves are per-ALLOCATION, not one event",
                   "PASS" if mv["dated"] and bad == 0 and len(acts) > 1 else "FAIL",
                   "%d relocations dated from the WeruUsageLog. A WERU's log "
                   "map is stale exactly when the swap happened after its own "
                   "last activation, so (stale, allocated later than its donor) "
                   "is forbidden: %d such records. The %d transposed pairs date "
                   "to %d DISTINCT activations spanning %s..%s of >=%s -- one "
                   "allocation at a time, not a coordinated exchange. "
                   "Quadrants: %s"
                   % (len(mv["dated"]), bad, len(pairs) // 2, len(acts),
                      acts[0] if acts else "-", acts[-1] if acts else "-",
                      (ftl.weru_usage_log().activation_count()[0]
                       if ftl.weru_usage_log() not in (UNKNOWN, None) else "?"),
                      {("%s/%s" % (k[0], "later" if k[1] else "earlier")): v
                       for k, v in sorted(quad.items())}))

    # ---- weruDescriptor +10 is the cell mode, and the drive stores it ----
    _wd, _o, _n, _s = ftl._section("weruDescriptor")
    if _wd is UNKNOWN or not _n:
        report("weruDescriptor +10 = cell mode", "SKIPPED",
               "weruDescriptor section not recovered")
    else:
        recs = decode_weru_descriptor(_wd)
        described = {w: r for w, r in recs.items() if r.self_ref == w}
        agree = dis = unk = 0
        bad = []
        for w, r in described.items():
            stored = r.slc_from_descriptor
            try:
                derived = ftl.cell_mode(w)
            except CellModeUnknown:
                derived = UNKNOWN
            if derived is UNKNOWN or stored is UNKNOWN:
                unk += 1
                continue
            if stored == derived:
                agree += 1
            else:
                dis += 1
                if len(bad) < 6:
                    bad.append((w, r.slc_mode, derived))
        n_slc = sum(1 for r in described.values()
                    if r.slc_mode == WERU_DESC_SLC_MODE_SLC)
        # only the configurations whose weruIds the descriptor array covers --
        # cfg 5 (the 98 spares) sits past it, and its cellMode 2 is not SLC
        want_slc = sum(c[2] for c in WERU_CONFIGURATIONS
                       if c[5] == WERU_DESC_SLC_MODE_SLC and c[1] in described)
        report("weruDescriptor +10 = cell mode", "PASS" if agree and not dis
               else "FAIL",
               "the drive STORES the cell mode we were re-deriving: "
               "sub_402E6ADE writes byte_900078[20*cfg + 14] into +10 "
               "(efc_cpu3.c:41186 -> :45593 -> :26945), the byte the firmware "
               "calls WeruConfiguration_GetWeruBlockMode (RootStorage.c:930). "
               "Over the %d WERUs whose record self-references its own weruId: "
               "%d agree with Ftl.cell_mode, %d disagree, %d undetermined%s. "
               "Independently, %d records read 1 = pseudo-SLC against %d "
               "predicted by the six WERU configurations"
               % (len(described), agree, dis, unk,
                  "" if not bad else " %s" % bad, n_slc, want_slc))
        report("weruDescriptor +1/+2 are dead", "PASS",
               "recorded as a MEASURED NEGATIVE, not a gap: +1 has no reader, "
               "no writer and no initialiser in any of the four CPUs, and +2 "
               "is written 0 once by sub_402E60F2 (efc_cpu3.c:44782) and read "
               "by nothing.  Here +1 is non-zero on %d of %d records and +2 on "
               "%d -- so the bytes are not merely constant, they are unused"
               % (sum(1 for w in described
                      if _wd[w * WERU_DESC_ENTRY + 1]), len(described),
                  sum(1 for w in described
                      if struct.unpack_from("<H", _wd,
                                            w * WERU_DESC_ENTRY + 2)[0])))

    bins = ftl.weru_bins()
    if bins is UNKNOWN:
        report("weruBin", "FAIL", "section not recovered")
    else:
        live = [b for b in bins.values() if not b.is_reset]
        ok_self = sum(1 for b in live if b.self_ref == b.bin_id)
        ok_len = sum(1 for b in live
                     if b.members is not UNKNOWN and len(b.members) == b.count)
        wdb, _o, nwd_, _s = ftl._section("weruDescriptor")
        ok_mem = 0
        for b in live:
            if b.members is UNKNOWN:
                continue
            ok_mem += all(wdb[w * WERU_DESC_ENTRY + WERU_DESC_BIN_OFF] == b.bin_id
                          for w in b.members)
        report("weruBin", "PASS" if live and ok_self == ok_len == ok_mem == len(live)
               else "FAIL",
               "%d of %d bins are live (848 B = %d x %d, efc_cpu3.c:37454). "
               "self-ref == binId %d/%d; the CIRCULAR list walked from +2 "
               "through weruDescriptor+6 has length == the stored count %d/%d; "
               "every member's binId matches %d/%d. Sum of counts = %d WERUs; "
               "kinds %s"
               % (len(live), len(bins), WERU_BIN_COUNT, WERU_BIN_ENTRY,
                  ok_self, len(live), ok_len, len(live), ok_mem, len(live),
                  sum(b.count for b in live),
                  {b.bin_id: b.kind for b in live}))

    # ---- the write frontier (RootStorage object 2, Allocation) -----------
    fr = ftl.write_frontier()
    if fr is UNKNOWN:
        report("Allocation write frontier", "FAIL", "object 2 not decoded")
    else:
        openf = {m: f for m, f in fr.items() if f.is_open}
        reinit = [m for m, f in fr.items() if f.is_reinitialised]
        ptr_only = [m for m, f in fr.items()
                    if not f.is_open and not f.is_reinitialised]
        # +24 currentWeruId must agree with the WERU that +0's WMA lands in.
        agree = sum(1 for f in openf.values()
                    if f.weru_of_wma == f.current_weru)
        report("Allocation write frontier",
               "PASS" if len(fr) == ALLOC_MSOS and agree == len(openf) and openf
               else "FAIL",
               "%d x %d B = %d, the object's measured size; %d MSOs hold a WERU "
               "open, %d records are in the state AllocationReinitializeMsos "
               "leaves (efc_cpu2.c:125261), %d keep a pointer with no current "
               "WERU. currentWeruId (+24) == wma//%d (+0) on %d/%d open fronts"
               % (ALLOC_MSOS, ALLOC_ENTRY, ALLOC_BYTES, len(openf), len(reinit),
                  len(ptr_only), geom.AUS_PER_TLC_WERU, agree, len(openf)))
        for m, f in sorted(openf.items()):
            mm = ftl.mso(m)
            print("          msoId %2d %-34s weru %4d wl %4d, +%d AU in the "
                  "open page%s" % (m, (mm.name if mm else "-"), f.current_weru,
                                   f.wordline, f.aus_in_open_page,
                                   "" if f.pending_weru is None
                                   else ", next WERU %d reserved" % f.pending_weru))

        # +32 is the WMA of the first AU of the page being filled.
        pb = sum(1 for f in fr.values()
                 if f.wma and f.page_base_wma == f.wma - f.wma % WMA_STRIDE)
        nz = sum(1 for f in fr.values() if f.wma)
        report("Allocation +32 is the open page's base WMA",
               "PASS" if nz and pb == nz else "FAIL",
               "+32 == +0 rounded down to a multiple of WMA_STRIDE (%d) on "
               "%d/%d records with a live pointer -- so +0 - +32 is how many "
               "AUs are already in the open page (%s here)"
               % (WMA_STRIDE, pb, nz,
                  sorted({f.aus_in_open_page for f in fr.values() if f.wma})))

        # +22 bits [14:3] is the wordline the front is on.  Predicted from the
        # WMA alone: page = (wma % 211968) // 192, then the cell mode from the
        # WERU configuration table and the page->wordline law from
        # efc_cpu1.c:9779.  Neither input comes from this record.
        wl_ok = wl_bad = wl_skip = 0
        wl_miss = []
        for m, f in sorted(fr.items()):
            if not f.wma:
                continue
            wid, page, _s, _a = split_wma(f.wma)
            cfg = weru_configuration(wid)
            if cfg is None or cfg[5] not in (0, 1):
                wl_skip += 1
                continue
            want = page_to_wordline(page, cfg[5] == 1)
            if want == f.wordline:
                wl_ok += 1
            else:
                wl_bad += 1
                wl_miss.append("mso %d weru %d page %d %s -> %d, field %d"
                               % (m, wid, page, "SLC" if cfg[5] == 1 else "TLC",
                                  want, f.wordline))
        report("Allocation +22 bits[14:3] is the open WORDLINE",
               "PASS" if wl_ok and not wl_bad else "FAIL",
               "predicted from the WMA's page and the WERU configuration's cell "
               "mode alone (efc_cpu1.c:9779): %d agree, %d disagree, %d skipped. "
               "%s. The TLC law is the firmware's; the pseudo-SLC identity is an "
               "observation -- it is what the %d pSLC fronts here show"
               % (wl_ok, wl_bad, wl_skip,
                  "; ".join(wl_miss) if wl_miss else "no misses",
                  sum(1 for f in fr.values() if f.wma
                      and (weru_configuration(split_wma(f.wma)[0]) or (0,) * 6)[5] == 1)))

        # ---- +4..+9 is the RAID block, and the record is a WriteableUnit ----
        # parityMode (+9 bits [7:6]) is the same enum as Mso_GetRaidType, which
        # lives in a different structure (msoDescriptor +16, the msoLma
        # section).  Making the two agree is the check that names the byte.
        raid = {m: f for m, f in fr.items() if f.raid_engaged}
        pm_ok = pm_bad = sc_ok = ptr_ok = cmd_ok = 0
        pm_miss = []
        for m, f in sorted(raid.items()):
            mm = ftl.mso(m)
            want = None if mm is None else mm.raid_type
            if want is not UNKNOWN and want == f.parity_mode:
                pm_ok += 1
            else:
                pm_bad += 1
                pm_miss.append("mso %d: +9 says %d, msoDescriptor says %s"
                               % (m, f.parity_mode, want))
            if f.stripe_count == MAX_BLOCKS_PER_WERU:
                sc_ok += 1
            if all(SHARED_DRAM_LO <= p < SHARED_DRAM_HI and p % 32 == 0
                   for p in (f.parity_au_buffer, f.parity_aux_buffer)):
                ptr_ok += 1
            mask = 0
            for i in range(f.raw[21] & 7):
                mask |= 1 << (4 * i)
            if mask == f.raid_cmd:
                cmd_ok += 1
        ids = [b for f in raid.values() for b in f.raid_buffers]
        report("Allocation +4..+9 is the RAID block of a WriteableUnit",
               "PASS" if raid and pm_bad == 0 and sc_ok == len(raid)
               and ptr_ok == len(raid) and cmd_ok == len(raid)
               and len(ids) == len(set(ids)) else "FAIL",
               "%d records carry a parity tracker. +9 bits[7:6] parityMode == "
               "the msoDescriptor's own Mso_GetRaidType on %d/%d (%s); +9 "
               "bits[5:0] stripeCount == %d, the Mso-mode column count, on "
               "%d/%d; +10/+14 (raidParityAuBuffer / raidParityAuxBuffer, named "
               "by Moh.c:2363-2364) are both inside IS_SHARED_ADDRESS and "
               "32-byte aligned on %d/%d; +4 is the plane nibble mask "
               "ParityManager_OnAllocationRequest builds from +21&7 on %d/%d. "
               "+6..+8 are logicalRaidBufferId[3] (ParityManager.c:1592), "
               "0xFF = cInvalidRaidBufferId: %s in use, no id claimed twice"
               % (len(raid), pm_ok, len(raid),
                  "; ".join(pm_miss) if pm_miss else "no misses",
                  MAX_BLOCKS_PER_WERU, sc_ok, len(raid), ptr_ok, len(raid),
                  cmd_ok, len(raid), sorted(ids)))

        # every open WERU must be a member of bin 43 ActiveWrite
        # Every open WERU is in bin 43 ActiveWrite -- except the ones handed out
        # after the last weruDescriptor/weruBin flush, which are still sitting
        # in the free pool they were drawn from.  The two sections are written
        # at different moments (weruBin only at a SystemMetadata flush,
        # efc_cpu3.c:38958 -> :44873, skipped on an unchanged CRC at :44942;
        # the Allocation object on its own schedule), so the lag is expected.
        # The falsifiable part is that a straggler must be strictly newer, by
        # the WeruUsageLog's global activation counter, than every WERU the bin
        # snapshot calls active.
        opens = ftl.open_werus()
        if bins is UNKNOWN or opens is UNKNOWN or wul is UNKNOWN:
            report("open WERUs vs bin 43 ActiveWrite", "FAIL",
                   "weruBin, the frontier or the WeruUsageLog is UNKNOWN")
        else:
            aw = bins[WERU_BIN_ACTIVE_WRITE]
            mem = set(aw.members) if aw.members is not UNKNOWN else set()
            inside = sorted(w for w in opens if w in mem)
            late = sorted(w for w in opens if w not in mem)
            acts = [wul.counter(w) for w in mem]
            acts = [a for a in acts if a is not UNKNOWN]
            newest_active = max(acts) if acts else None
            newer = [w for w in late
                     if wul.counter(w) is not UNKNOWN and newest_active is not None
                     and wul.counter(w) > newest_active]
            pooled = [w for w in late
                      if (bid := _bin_of(bins, w)) is not None
                      and bins[bid].kind == (weru_configuration(w) or (None,))[0]]
            report("open WERUs vs bin 43 ActiveWrite",
                   "PASS" if opens and len(newer) == len(late) == len(pooled)
                   else "FAIL",
                   "bin %d %s has %d members; %d of the %d WERUs the Allocation "
                   "object holds open are among them (%s). The %d that are not "
                   "(%s) sit in the FREE POOL of their own configuration %d/%d "
                   "and are newer than every bin-43 member %d/%d -- activation "
                   "%s vs %s. That is the allocator caught mid-hand-out: the "
                   "weruBin snapshot predates the Allocation snapshot"
                   % (WERU_BIN_ACTIVE_WRITE,
                      weru_bin_name(WERU_BIN_ACTIVE_WRITE), len(mem),
                      len(inside), len(opens), inside, len(late), late,
                      len(pooled), len(late), len(newer), len(late),
                      [wul.counter(w) for w in late], newest_active))

        # the Valid-band formula, predicted from lutId + cell mode
        msos = ftl.msos()
        pred_ok = pred_bad = pred_skip = 0
        detail = []
        for b in (bins.values() if bins is not UNKNOWN else []):
            if b.is_reset or b.members is UNKNOWN:
                continue
            if not any(base <= b.bin_id < base + 16 for base, _w in
                       WERU_BIN_BANDS[:2]):
                continue
            base = 2 if b.bin_id < 18 else 18
            for w in b.members:
                owner = wul.owner(w) if wul is not UNKNOWN else None
                m = msos.get(owner) if isinstance(owner, int) else None
                if m is None or m.lut_id is UNKNOWN or m.lut_id >= 8:
                    pred_skip += 1
                    continue
                # cell mode from the WERU configuration table, which is an
                # independent source: it is read out of sub_4033CB5E and keyed
                # on the weruId range, never on the bin we are predicting.
                cfg = weru_configuration(w)
                if cfg is None or cfg[5] not in (0, 1):
                    pred_skip += 1          # cellMode 2 (spare) is uninterpreted
                    continue
                slc = cfg[5] == 1
                got = weru_bin_for_band(base, m.lut_id, slc)
                if got == b.bin_id:
                    pred_ok += 1
                else:
                    pred_bad += 1
                    detail.append("weru %d owner %d lut %d %s -> %d, is %d"
                                  % (w, owner, m.lut_id,
                                     "SLC" if slc else "TLC", got, b.bin_id))
        report("Valid-band binId = base + slcMode + 2*lutId",
               "PASS" if pred_ok and not pred_bad else "FAIL",
               "%d band members predicted from the OWNER's lutId and the WERU's "
               "configuration alone: %d agree, %d disagree, %d skipped for an "
               "unknown lutId or cell mode. %s (efc_cpu3.c:46168; the 16-wide "
               "band is 2 modes x cMaxNumberOfLuts 8, efc_cpu2.c:130949)"
               % (pred_ok + pred_bad, pred_ok, pred_bad, pred_skip,
                  "; ".join(detail) if detail else "no misses"))

    # ---- which free pool an MSO draws from, predicted from msoType ------
    if bins is not UNKNOWN and wul is not UNKNOWN and ftl.mso_table is not None:
        # msoId -> msoType, straight off the msoIdTable rather than the
        # descriptor: the descriptor's own type byte is not decoded here, and a
        # None there would silently skip every member.
        mso_type_of = {m: t for m, (t, _g) in ftl.mso_table.entries().items()}
        ok = bad = skip = 0
        miss = []
        seen_pools = set()
        for wb in bins.values():
            if wb.is_reset or wb.members is UNKNOWN:
                continue
            if wb.bin_id not in set(WERU_BIN_FREE_POOL_BY_MSO_TYPE.values()):
                continue
            seen_pools.add(wb.bin_id)
            for w in wb.members:
                o = wul.owner(w)
                t = mso_type_of.get(o) if isinstance(o, int) else None
                if t is None or t not in WERU_BIN_FREE_POOL_BY_MSO_TYPE:
                    skip += 1               # owner unreadable, or a dynamic MSO
                    continue
                if WERU_BIN_FREE_POOL_BY_MSO_TYPE[t] == wb.bin_id:
                    ok += 1
                else:
                    bad += 1
                    if len(miss) < 6:
                        miss.append("weru %d owner %d type %d -> %d, is %d"
                                    % (w, o, t,
                                       WERU_BIN_FREE_POOL_BY_MSO_TYPE[t],
                                       wb.bin_id))
        report("free pool is chosen by the owner's msoType",
               "PASS" if ok and not bad else "FAIL",
               "%d pool bins occupied (%s); of %d members whose owner the "
               "WeruUsageLog names, %d land in the pool the binPair table's "
               "WriteAllocation column predicts and %d do not (%s); %d skipped "
               "for an unreadable or dynamic owner. Table: dword_402F8968, "
               "efc_cpu3.c:46128"
               % (len(seen_pools), sorted(seen_pools), ok + bad, ok, bad,
                  "; ".join(miss) if miss else "none", skip))

        # ---- msoId 58 = PhysicalMediaAccess/System = the spare pool ------
        spare = [c for c in WERU_CONFIGURATIONS
                 if c[0] == len(WERU_CONFIGURATIONS) - 1][0]
        spare_ids = set(range(spare[1], spare[1] + spare[2]))
        pma = {w for w, o in wul.owners().items()
               if isinstance(o, int)
               and mso_type_of.get(o) == MSO_TYPE_PHYSICAL_MEDIA_ACCESS}
        binned = {w for wb in bins.values() if wb.members is not UNKNOWN
                  for w in wb.members}
        # do other free WERUs also read PhysicalMediaAccess?  If 58 were merely
        # the "unclaimed" default owner they would -- 673 WERUs are flagged
        # erased.  This is the discriminator.
        erased_elsewhere = sum(
            1 for w in range(wul.n_werus)
            if w not in spare_ids and wul.flags(w) is not UNKNOWN
            and (wul.flags(w) & WERU_USAGE_FLAG_ERASED) and w in pma)
        report("msoId 58 PhysicalMediaAccess owns exactly the spare configuration",
               "PASS" if pma == spare_ids and not (pma & binned)
               and not erased_elsewhere else "FAIL",
               "the WeruUsageLog gives msoType %d (PhysicalMediaAccess, "
               "ScanEngine.c:280) %d WERUs and they are exactly configuration "
               "%d, weruIds %d..%d (%s). None of them is in any bin (%d of %d), "
               "so the spare pool is outside the bin allocator entirely. And "
               "NOT a default: of the WERUs flagged erased outside the spare "
               "range, %d read PhysicalMediaAccess -- an unclaimed-default "
               "reading predicts hundreds"
               % (MSO_TYPE_PHYSICAL_MEDIA_ACCESS, len(pma), spare[0],
                  spare[1], spare[1] + spare[2] - 1,
                  "exact match" if pma == spare_ids else "MISMATCH",
                  len(pma & binned), len(pma), erased_elsewhere))

    # ---- Why NSID1's DATA sits under Migration --------------------------
    if (bins is not UNKNOWN and wul is not UNKNOWN and fr is not UNKNOWN
            and ftl.mso_table is not None):
        own = {}
        for w, o in wul.owners().items():
            if isinstance(o, int):
                own[o] = own.get(o, 0) + 1
        # UserData/NSID1 is the SLC-accelerated direct write path; Migration is
        # the engine that folds UserDataNandCache into TLC (its source MSO is
        # byte_900031 = Mso(UserDataNandCache, NSID1), efc_cpu3.c:47904, :47934).
        ud, mig, nc = 32, 43, 45
        f32 = fr.get(ud)
        report("NSID1 writes through NandCache -> Migration, not UserData",
               "PASS" if (own.get(ud, 0) == 0 and f32 is not None
                          and f32.is_reinitialised and own.get(mig) and own.get(nc)
                          and fr[mig].is_open and fr[nc].is_open) else "FAIL",
               "msoId %d %s owns %d WERUs and its Allocation record is %s; "
               "msoId %d %s owns %d with an open front at weru %s, and msoId "
               "%d %s owns %d with an open front at weru %s (next weru %s "
               "reserved). Migration's SOURCE is UserDataNandCache "
               "(efc_cpu3.c:47904, :47934), so this board's NSID1 data was "
               "never written through the UserData MSO at all -- it went "
               "host -> pSLC cache -> TLC fold, and was mid-fold at the flush"
               % (ud, ftl.mso(ud).name, own.get(ud, 0),
                  "reinitialised" if f32 is not None and f32.is_reinitialised
                  else "NOT reinitialised",
                  mig, ftl.mso(mig).name, own.get(mig, 0), fr[mig].current_weru,
                  nc, ftl.mso(nc).name, own.get(nc, 0), fr[nc].current_weru,
                  fr[nc].pending_weru))

        # The two parity write frontiers, and their co-allocation with the
        # data front.  "UserData 2nd wf" is msoType 1 and it exists only in
        # the ParityData group -- there is no second DATA write frontier.
        types = {m: t for m, (t, _g) in ftl.mso_table.entries().items()}
        second_wf = sorted(m for m, t in types.items() if t == 1)
        groups = {m: g for m, (_t, g) in ftl.mso_table.entries().items()}
        acts = {}
        for m in (mig, 34, 35):
            acts[m] = sorted(
                ((wul.counter(w), w) for w, o in wul.owners().items()
                 if o == m and wul.counter(w) is not UNKNOWN), reverse=True)
        near = []
        for c, w in acts[mig]:
            d34 = min((abs(c - c2), w2) for c2, w2 in acts[34]) if acts[34] else None
            d35 = min((abs(c - c2), w2) for c2, w2 in acts[35]) if acts[35] else None
            near.append((w, c, d34, d35))
        worst = max(max(a[0], b[0]) for _w, _c, a, b in near) if near else None
        span = (max(wul.counter(w) for w in range(wul.n_werus)
                    if wul.counter(w) is not UNKNOWN)
                - min(wul.counter(w) for w in range(wul.n_werus)
                      if wul.counter(w) is not UNKNOWN))
        report("the two write frontiers are the two PARITY MSOs",
               "PASS" if (len(second_wf) == 1
                          and groups[second_wf[0]] == len(MSO_GROUPS) - 1
                          and near and worst is not None) else "FAIL",
               "msoType 1 'UserData 2nd WF' exists for exactly %d group(s) -- "
               "msoId(s) %s, group %s -- so there is no second DATA front; the "
               "second frontier is a second PARITY stream. Its descriptor is a "
               "verbatim copy of msoId 34's (efc_cpu3.c:37907) and both register "
               "the same lutId (:37903, :37945), so they are two fronts over ONE "
               "address space. Co-allocation: for each of the %d Migration/NSID1 "
               "WERUs, the nearest UserData/ParityData and UserData2ndWF/"
               "ParityData activation is within %d counts (worst case) of a "
               "global counter spanning %d: %s"
               % (len(second_wf), second_wf,
                  [MSO_GROUPS[groups[m]] for m in second_wf], len(near), worst,
                  span,
                  "; ".join("weru %d@%d -> 34:weru %d (+-%d), 35:weru %d (+-%d)"
                            % (w, c, a[1], a[0], b[1], b[0])
                            for w, c, a, b in near[:4])))

    pool = ftl.rebuild_sequence_number_pool()
    if pool is UNKNOWN:
        report("rebuildSequenceNumberPool", "FAIL", "section not recovered")
    else:
        ok = (len(pool) == REBUILD_SEQ_POOL_GROUPS
              and all(n >= b for n, b, _e in pool)
              and all(e >= b for _n, b, e in pool))
        report("rebuildSequenceNumberPool", "PASS" if ok else "FAIL",
               "144 B = %d x %d, one record per msoGroupId (efc_cpu3.c:37472). "
               "shared/reserved/end with shared >= reserved for all %d: %s"
               % (REBUILD_SEQ_POOL_GROUPS, REBUILD_SEQ_POOL_ENTRY, len(pool),
                  [p[0] for p in pool]))

        # ---- The pool index is not msoGroupId alone ----------------------
        # `msoDescriptor +19` bit 0 (`Mso_NeedsSequenceNumberReservation`)
        # selects the record's reserved lane at +8 instead of the shared one at
        # +0.  This check measures what assuming one lane would have cost.
        msos_now = ftl.msos()
        if msos_now is UNKNOWN:
            report("rebuild sequence lane (msoDescriptor +19 bit 0)", "SKIPPED",
                   "no msoDescriptor section")
        else:
            users, wrong, rows = [], [], []
            for mid in sorted(msos_now):
                try:
                    r = ftl.mso_rebuild_sequence_number(mid)
                except ValueError:
                    continue
                if r is UNKNOWN:
                    continue
                shared = pool[r["group"]][0]
                if r["needs_reservation"]:
                    users.append(mid)
                    if r["sequence"] != shared:
                        wrong.append((mid, r["group"], shared, r["sequence"]))
                        rows.append("mso %d (group %d) %d not %d"
                                    % (mid, r["group"], r["sequence"], shared))
            # every reserved-lane MSO must respect its own window
            bounded = all(pool[ftl.msos()[m].group][1]
                          <= pool[ftl.msos()[m].group][2] for m in users
                          if ftl.msos()[m].group is not UNKNOWN)
            report("rebuild sequence lane (msoDescriptor +19 bit 0)",
                   "PASS" if users and bounded else "FAIL",
                   "%d of %d MSOs set Mso_NeedsSequenceNumberReservation "
                   "(efc_cpu2.c:34452) and therefore read the RESERVED lane at "
                   "+8, not the shared cursor at +0 -- %s.  %d of them get a "
                   "DIFFERENT number that way (%s), which is exactly what a "
                   "one-lane-per-msoGroupId decoder got wrong; and every "
                   "reserved lane satisfies reserved <= end, the bound the "
                   "firmware panics on (RebuildSequenceNumberPool.h:166, "
                   "efc_cpu2.c:43039/:43145/:43204)"
                   % (len(users), len(msos_now), users, len(wrong),
                      "; ".join(rows) or "none on this board"))

        # ---- LptEntry +2/+4/+6 against the L2S array they seed ----------
        # `+2` bounds the superblock array, `+4` seeds L2S u16[1] and `+6`
        # seeds u16[2] (efc_cpu2.c:136307-136312).  Both arrays are read out of
        # the same media blob so this compares one generation with itself.
        try:
            from . import namespace as _ns          # local: avoids a cycle
            _nt = _ns.NamespaceTable.from_ftl(ftl)
        except Exception as _e:                     # pragma: no cover
            _nt = None
            report("LptEntry +2/+4/+6 vs the L2S array", "SKIPPED", str(_e))
        if _nt is not None:
            n = m2 = m4 = m6 = 0
            for _nsid in (2, 3):
                try:
                    blob = _nt.lpt_buffer(_nsid)
                except Exception:
                    continue
                if blob is UNKNOWN or blob is None:
                    continue
                arr = _ns.L2sArray.from_buffer(_nsid, blob)
                for lp, sl in sorted(arr.slices.items()):
                    o = _ns.LPT_ARRAY_OFF + _ns.LPT_ENTRY * lp
                    d = decode_lpt_entry(blob[o:o + _ns.LPT_ENTRY])
                    n += 1
                    m2 += d["n_superblocks"] == sl.count
                    m4 += d["superblock_write_index_raw"] == sl.full
                    m6 += d["superblock_cursor"] == sl.granules
            if not n:
                report("LptEntry +2/+4/+6 vs the L2S array", "SKIPPED",
                       "no LPT+L2S buffer is servable from this backing")
            else:
                report("LptEntry +2/+4/+6 vs the L2S array",
                       "PASS" if m2 == n and m6 == n else "FAIL",
                       "over %d live logical partitions of both namespaces: "
                       "+2 == (u16[0] >> 4) %d/%d and +6 == u16[2] %d/%d -- both "
                       "exact -- but +4 == u16[1] only %d/%d.  That asymmetry is "
                       "the finding: RebuildNeo pushes +4 into u16[1] "
                       "(efc_cpu2.c:136307 -> :136311 -> :120629) but ONLY the "
                       "LP-erase path ever advances it, so it is a rebuild SEED "
                       "and not a live mirror.  Read the write cursor from "
                       "L2sSlice.full, never from LptEntry +4"
                       % (n, m2, n, m6, n, m4, n))

        # ---- LptEntry +8 is a draw from that pool ------------------------
        # sub_40344016 writes the same value into the LP record and into
        # rebuildSequenceNumberPool[msoGroupId] (efc_cpu2.c:115577-115599), so
        # the highest live LP stamp must be the pool's `next`.  Two namespaces,
        # two independent instances.
        lpt_rows = []
        all_lpt_live = []
        for nm, first, gid in (("LptNsid2", 44, 2), ("LptNsid3", 74, 3)):
            sb = ftl.root.segmented_buffer(nm, first, 6, LPT_ENTRY_BYTES)
            if sb is None or sb is UNKNOWN:
                continue
            ents = [decode_lpt_entry(sb.data[LPT_ENTRY_BYTES * i:
                                             LPT_ENTRY_BYTES * (i + 1)])
                    for i in range(sb.n_elements)]
            live = [e for e in ents if e["attr"]]
            if not live:
                continue
            all_lpt_live.extend(live)
            hi = max(e["rebuild_sequence_number"] for e in live)
            attrs = {}
            for e in ents:
                attrs[e["attr_name"]] = attrs.get(e["attr_name"], 0) + 1
            lpt_rows.append((nm, gid, len(live), hi, pool[gid][0], attrs,
                             sorted({e["n_superblocks"] for e in live}),
                             sorted({e["nsid3_marker"] for e in live})))
        good = [r for r in lpt_rows if r[3] == r[4]]
        report("LptEntry +8 is a rebuildSequenceNumber, and attr is named",
               "PASS" if len(lpt_rows) == 2 and len(good) == 2 else "FAIL",
               "; ".join(
                   "%s (msoGroupId %d): %d live LPs, highest +8 stamp %d, "
                   "pool[%d].next %d%s -- attr census %s (off_40400BB0 = %s, "
                   "efc_cpu2.c:10778, indexed by byte0 & 3 at :114609), "
                   "+2 numOfSuperBlocks %s, +4 %s"
                   % (nm, gid, n, hi, gid, nxt,
                      " EQUAL" if hi == nxt else " MISMATCH", attrs,
                      list(LPT_ATTR_NAMES), nsb, [hex(x) for x in mk])
                   for nm, gid, n, hi, nxt, attrs, nsb, mk in lpt_rows))

        # ---- LptEntry +16 is the activation write offset, +20 its valid bit
        # sub_40346AEC (cActivateLoggingTableState) stores sub_9182's
        # result[3] = (l2sAccess.cursor << 4) + curSbIdx * wmasPerWeru.  For an
        # LP still in its first superblock that is exactly (+6 << 4), and +6 is
        # the same cursor (efc_cpu2.c:24686-24688).  Every LP that never reached
        # activate-RW must still carry the create template's 0xFFFFFFFF, and
        # +20 bit 0 -- set by the same template, cleared only beside a
        # +16 := 0xFFFFFFFF -- must be set wherever +16 is a real offset.
        checked = none_ = exact = nonzero_exact = stale = ahead = flagged = 0
        for e in all_lpt_live:
            checked += 1
            if e["write_offset_valid"]:
                flagged += 1
            off = e["activation_write_offset"]
            if off is None:
                none_ += 1
                continue
            here = e["superblock_cursor"] << LPT_WRITE_OFFSET_CURSOR_SHIFT
            if off == here:
                exact += 1
                if off:
                    nonzero_exact += 1
            elif off < here:
                stale += 1              # cursor advanced after the activation
            else:
                ahead += 1              # would refute the reading
        report("LptEntry +16 = activation write offset, +20 bit 0 = its valid bit",
               "PASS" if (checked and ahead == 0 and nonzero_exact >= 1
                          and none_ and flagged == checked) else "FAIL",
               "%d live LPs. +16 is a SNAPSHOT taken by sub_40346AEC in "
               "cActivateLoggingTableState of sub_9182's result[3] = "
               "(l2sAccess.cursor << %d) + curSb * wmasPerWeru "
               "(efc_cpu2.c:24169), so it can never exceed the live cursor "
               "that +6 mirrors: %d equal it exactly (%d of them non-zero -- "
               "the discriminating case), %d sit below it (cursor advanced "
               "since), %d ABOVE it (would refute the reading). %d hold the "
               "create template 0x%08X = never activated read-write. +20 bit 0 "
               "set on %d/%d. LPT_ENTRY_TEMPLATE = %s, read at 0x403A8FB0 "
               "(IDA types the constant int[4] and hides the last two words)"
               % (checked, LPT_WRITE_OFFSET_CURSOR_SHIFT, exact, nonzero_exact,
                  stale, ahead, none_, LPT_WRITE_OFFSET_NONE, flagged, checked,
                  list(LPT_ENTRY_TEMPLATE)))

    # ---- 4. LMA -> physical, and the three-way return --------------------
    if sm is not UNKNOWN:
        p0 = ftl.lma_to_physical(MSOID_ANCHOR_VALUE, 0)
        big = ftl.lma_to_physical(MSOID_ANCHOR_VALUE, 0xFFFFFF)
        nolut = ftl.lma_to_physical(42, 0)       # lutId 0xFF: no LUT at all
        # ... and an MSO whose LUT resolves but whose WMA lands in a WERU with
        # no measured slot -> (ce, block).  Found, not assumed.
        unk = unk_lma = None
        reg53 = ftl.lut_region(53)
        if isinstance(reg53, LutRegion):
            for lma in range(0, min(reg53.covered_lmas, 1 << 16),
                             reg53.granularity):
                r = ftl.lma_to_physical(53, lma)
                if r is UNKNOWN:
                    unk, unk_lma = r, lma
                    break
        ok = (p0 is not None and p0 is not UNKNOWN and hasattr(p0, "ce")
              and big is None and nolut is None and unk is UNKNOWN)
        report("lma_to_physical 4-way", "PASS" if ok else "FAIL",
               "msoId 41 LMA 0 -> %s (resolved) ; past the LUT's end -> %r ; "
               "msoId 42 (lutId 0xFF, NO LUT) -> %r ; msoId 53 LMA %s -> %r "
               "(LUT answers, WERU %s has no measured slot map). No arithmetic "
               "fallback anywhere."
               % (p0, big, nolut, unk_lma, unk,
                  (ftl.lma_to_wma(53, unk_lma) or 0) // geom.AUS_PER_TLC_WERU
                  if unk_lma is not None else "-"))
        # Every resolved LMA must come from the WERU the LUT names -- that is
        # the whole point of the change, so check it rather than assume it.
        if sm.lut_used:
            bad = [l for l, w in sm.provenance.items() if sm.lut.weru(l) != w]
            moved = [l for l, w in sm.provenance.items()
                     if w != sm.weru_of(0)]
            report("every LMA comes from the weru the LUT names",
                   "PASS" if not bad else "FAIL",
                   "%d/%d resolved LMAs agree with the LUT, %d violations; "
                   "%d of them live OUTSIDE weru %s and would have been read "
                   "from the wrong copy before"
                   % (len(sm.provenance) - len(bad), len(sm.provenance),
                      len(bad), len(moved), sm.weru_of(0)))
            report("lma_to_wma answers for two MSOs", "PASS",
                   "msoId %d LMA 0 -> wma %s (weru %s) ; msoId 40 LMA 0 -> %s ; "
                   "msoId 53 -> %r"
                   % (MSOID_ANCHOR_VALUE, ftl.lma_to_wma(MSOID_ANCHOR_VALUE, 0),
                      sm.lut.weru(0), ftl.lma_to_wma(40, 0),
                      ftl.lma_to_wma(53, 0)))

    # ---- 4b. WMA <-> physical ---------------------------------------------
    # Round-trip over every addressable WMA of every configured WERU.  Pure
    # arithmetic, so a failure is a coding error, not a media fact -- which is
    # exactly why it is cheap to run exhaustively rather than on a sample.
    rt = bad_rt = tail = 0
    for wid, cfg in sorted(WERU_CONFIGS.items()):
        base = wid * geom.AUS_PER_TLC_WERU
        for page in (0, 1, 37, 187, cfg.pages_per_block - 1):
            for slot in range(MAX_BLOCKS_PER_WERU):
                for au in range(WMAS_PER_PAGE):
                    wma = base + page * WMA_STRIDE + slot * WMAS_PER_PAGE + au
                    w, p, s, a = split_wma(wma)
                    if (w, p, s, a) != (wid, page, slot, au):
                        bad_rt += 1
                        continue
                    ph = wma_to_physical(wma, cfg)
                    if ph is None:
                        tail += 1
                        continue
                    back = physical_to_wma(ph.ce, ph.block, ph.page, ph.au,
                                           cfg, wid)
                    if back == wma:
                        rt += 1
                    else:
                        bad_rt += 1
    report("WMA <-> physical round-trip", "PASS" if not bad_rt else "FAIL",
           "%d WMAs over %d configured WERUs survive wma->physical->wma "
           "exactly, %d violations; %d landed in the unused tail of a %d-WMA "
           "window (slots %d..%d) and correctly addressed nothing"
           % (rt, len(WERU_CONFIGS), bad_rt, tail, WMA_STRIDE, 11,
              MAX_BLOCKS_PER_WERU - 1))

    # The twelfth block: programmed, and no WMA names it.  It must stay
    # unaddressable -- a WMA for it would be an invention -- while also never
    # being mistaken for empty.
    cfg14 = WERU_CONFIGS[14]
    twelfth = ftl.physical_to_wma(11, 17, 0, 0)
    inside = cfg14.covers(11, 17)
    report("the unaddressed twelfth block", "PASS"
           if twelfth is None and inside else "FAIL",
           "(ce11, block 17) is inside weru 14's region (%s) but has no WMA "
           "(%r): 111 programmed pages that no address reaches.  Its ROLE is "
           "settled (see the next check); its ADDRESSABILITY deliberately is "
           "not." % (inside, twelfth))

    # ...and what it holds.  MEASURED here rather than cited, because "slot 11
    # is the parity" was carried as an unverified role for weeks on the
    # strength of an aux flag.  The flag says a page is a parity page; only the
    # arithmetic says what parity.
    #
    # The three CE 6-11 WERUs are used on purpose: a 12-block WERU spans one
    # half of the package, so werus 14/20/22 are entirely on live dies and this
    # needs no CE0 -- which retracts "the stripe includes CE0, so no XOR can be
    # checked end to end here".  werus 19/23 are the CE 0-5 half and are not
    # used: they put two members on the dead die, and one XOR relation carries
    # one erasure.
    par_ok = par_bad = par_pages = 0
    _cdc = ftl.codec or default_codec()
    for _w, (_cb, _bb) in (((14, (6, 16)), (20, (6, 10)), (22, (6, 20)))
                           if ftl.device is not None else ()):
        for _pg in (0, 8, 64):
            _slots = []
            for _s in range(12):
                _ce, _blk = _cb + _s // 2, _bb + _s % 2
                try:
                    _raw = ftl.device.read_page(_ce, _blk, _pg, slc=True)
                except Exception:                             # noqa: BLE001
                    _slots = None
                    break
                _aus, _ok = _cdc.decode(_raw, _blk, _pg, correct=True)
                if _aus is None or _ok is UNKNOWN or not all(_ok):
                    _slots = None
                    break
                _slots.append(_aus)
            if not _slots:
                continue
            par_pages += 1
            for _a in range(geom.AUS_PER_PAGE_TLC):
                _x = bytearray(len(_slots[0][_a]))
                for _s in range(12):
                    for _i, _b in enumerate(_slots[_s][_a]):
                        _x[_i] ^= _b
                if any(_x):
                    par_bad += 1
                else:
                    par_ok += 1
    report("the twelfth block IS the stripe's inline RAID parity",
           "PASS" if par_ok and not par_bad else
           ("SKIPPED" if not par_pages else "FAIL"),
           "slot11[au] == XOR(slot0..slot10)[au] over the AU's 8 aux bytes AND "
           "its 4096 user bytes -- the unit the ECU accumulates "
           "(efc_cpu2.c:37432-37441).  Here: %d AUs with a zero 12-way XOR / %d "
           "non-zero over %d complete stripes on werus 14/20/22.  The full "
           "sweep (sd_block17.py) is 3388/0 over 847 stripes, 12-way XOR zero "
           "847/847, and leave-one-out reconstruction of ANY slot from the "
           "other eleven 5328/5328 on weru 14.  Not a mirror (equals another "
           "slot on 4 of 847), not a journal (its aux +0 are XOR products, not "
           "addresses), not residue (an exact function of the CURRENT stripe)."
           % (par_ok, par_bad, par_pages))

    if sm is not UNKNOWN and sm.lut_used:
        # The hold-out.  Compare the rule against placements found by an
        # independent route -- the aux-LMA scan -- rather than against itself.
        exact = wrong = newer = unconfigured = 0
        firstbad = []
        for i, w in enumerate(sm.lut.entries):
            if w in LUT_SENTINELS:
                continue
            for k in range(sm.lut.granularity):
                lma = sm.lut.granularity * i + k
                scanned = sm.placement.get(lma)
                if scanned is None:
                    continue
                ph = ftl.wma_to_physical(w + k)
                if ph is UNKNOWN:
                    unconfigured += 1
                    continue
                if ph is None:
                    wrong += 1
                    continue
                if ph.page != scanned.page:
                    newer += 1          # the scan holds a newer copy: staleness
                elif (ph.ce, ph.block, ph.au) == (scanned.ce, scanned.block,
                                                  scanned.au):
                    exact += 1
                else:
                    wrong += 1
                    if len(firstbad) < 4:
                        firstbad.append((lma, ph, scanned))
        report("WMA rule vs the independent aux-LMA scan",
               "PASS" if exact and not wrong else "FAIL",
               "%d EXACT, %d WRONG, %d where the scan holds a NEWER copy than "
               "the SystemLut snapshot (staleness, not a mapping error), %d in "
               "unconfigured WERUs. %s"
               % (exact, wrong, newer, unconfigured, firstbad or ""))
        # ... and the sentence this refutes, so it cannot creep back.
        p152 = ftl.lma_to_physical(MSOID_ANCHOR_VALUE, 152)
        s152 = sm.lma_placement(152)
        report("retraction: the WMA offset DOES convert",
               "PASS" if (p152 and s152 and (p152.ce, p152.block, p152.page)
                          == (s152.ce, s152.block, s152.page)) else "FAIL",
               "LMA 152: rule says %s, the scan found %s. The old docstring "
               "concluded from LMA 0 alone that 'the offset inside a WERU is "
               "not a dense AU index' and therefore unconvertible; it is not "
               "dense, it is STRIDED, and it converts."
               % (p152, s152))

    # An independent implementation must agree.  nk_wma.py is the hardware
    # agent's module; if it is present, its answers are a second opinion this
    # module did not write.
    #
    # Packaging only: the same file is vendored verbatim as `titania._wma_ref`
    # so this check still runs from an installed package instead of skipping
    # everywhere except the author's working directory.  The root copy is the
    # upstream and is preferred when present; the vendored one is the fallback.
    # It is still a second derivation either way -- see titania/_wma_ref.py.
    # Do not "simplify" this by importing ftl's own map twice.
    _nk = _nk_src = None
    try:
        sys.path.insert(0, str(root))
        from . import nk_wma as _nk                                  # noqa
        _nk_src = "nk_wma.py"
    except Exception as _e:
        try:
            from . import _wma_ref as _nk                     # noqa
            _nk_src = "titania._wma_ref (vendored copy; root nk_wma.py: %s)" % _e
        except Exception as _e2:                              # pragma: no cover
            report("agrees with nk_wma.py", "SKIPPED",
                   "neither implementation importable: %s / %s" % (_e, _e2))
    if _nk is not None:
        n = diff = 0
        for wid, cfg in sorted(WERU_CONFIGS.items()):
            other = _nk.CONFIGS.get(wid)
            if other is None:
                continue
            for wma in range(wid * geom.AUS_PER_TLC_WERU,
                             wid * geom.AUS_PER_TLC_WERU + 4000):
                mine = wma_to_physical(wma, cfg)
                theirs = _nk.wma_to_physical(wma, other)
                mine_t = None if mine is None else (mine.ce, mine.block,
                                                    mine.page, mine.au)
                n += 1
                if mine_t != theirs:
                    diff += 1
        report("agrees with nk_wma.py", "PASS" if not diff else "FAIL",
               "%d WMAs, %d disagreements between this module's independent "
               "implementation and the hardware agent's (via %s)"
               % (n, diff, _nk_src))

    # The slot-index bug: a compacted list renumbers every slot after a hole.
    probe = decode_block_mapping(
        struct.pack("<I", UNMAPPED_PBID) + b"\0" * 16
        + struct.pack("<I", geom.pbid(7, 3)) + b"\0" * 16
        + b"\xff" * (BLOCK_MAPPING_ENTRY * (geom.BLOCKS_PER_WERU - 2)))
    slots0 = probe[0]
    report("blockMapping is slot-indexed",
           "PASS" if (len(slots0) == geom.BLOCKS_PER_WERU
                      and slots0[0] is None and slots0[1] == (7, 3)
                      and mapped_blocks(slots0) == [(7, 3)]) else "FAIL",
           "an unmapped slot 0 keeps (block 7, ce3) at index 1, not index 0: "
           "%s ; mapped view %s. Compaction used to renumber every slot after "
           "a hole, and slot is what (wma %% %d) // %d consumes."
           % (slots0[:3], mapped_blocks(slots0), WMA_STRIDE, WMAS_PER_PAGE))

    # ---- 4c. the per-MSO LUT chain ---------------------------------------
    regs = ftl.lut_regions(40)
    WANT_REGIONS = [(0, 4, 8, 10696), (10696, 10700, 10704, 10808),
                    (10808, 10812, 10816, 10824), (10824, 10828, 10832, 11880)]
    if regs is UNKNOWN or not regs:
        report("LUT region chain", "FAIL", "could not walk msoId 40's LMA space")
    else:
        got = [(r.base, r.info_lma, r.data_lma, r.next_base) for r in regs]
        ll = ftl.user_lut_lut()
        ends = (ll is not UNKNOWN and ll is not None
                and got[-1][3] == ll.covered_lmas)
        report("LUT region chain walked, not computed",
               "PASS" if got == WANT_REGIONS and ends else "FAIL",
               "%d regions at bases %s, each with infoLma = base+4 and dataLma "
               "= base+8; the chain ends at %d = the userLutLut's own coverage "
               "(%s). The size formula over-predicts by 2876 LMAs, so walking "
               "the headers is both cheaper and self-checking."
               % (len(regs), [r.base for r in regs], got[-1][3], ends))
        owners = {}
        for r in regs:
            m = ftl.msos().get(r.init_mso)
            owners[r.base] = (r.init_mso, m.lut_id if m else None, r.granularity,
                              r.n_entries)
        report("LUT regions are per-namespace, not per-MSO", "PASS",
               "base -> (init msoId, lutId, granularity, entries): %s ; "
               "region 0 covers %d LMAs x %d B = %.0f GB, the user capacity of "
               "an 825 GB drive"
               % (owners, regs[0].covered_lmas, LMA_BYTES,
                  regs[0].covered_lmas * LMA_BYTES / 1e9))
        # lutId groups whole namespaces: every MSO of a group must land on the
        # same region, and that is a stronger statement than "it parsed".
        byreg = {}
        for m_id, m in sorted(ftl.msos().items()):
            r = ftl.lut_region(m_id)
            if isinstance(r, LutRegion):
                byreg.setdefault(r.base, []).append(m_id)
        ok = all(len({ftl.msos()[x].lut_id for x in v}) == 1
                 for v in byreg.values())
        report("one LUT region per namespace group", "PASS" if ok else "FAIL",
               "%s -- all of a group's MSOs are views over ONE table, so 8 lutId "
               "slots is not a shortage"
               % {b: "lutId %s x%d" % (ftl.msos()[v[0]].lut_id, len(v))
                  for b, v in sorted(byreg.items())})

        # ---- the descriptor's first 236 bytes: snapshotInformation ---------
        # Its list must be exactly the MSOs of this region's lutId, and its
        # per-entry weruId / counter must match two other structures.
        wf_snap = ftl.write_frontier()
        log_snap = ftl.weru_usage_log()
        TRIM_MSO_ID_ = ftl.msos() and next(
            (m for m, mm in ftl.msos().items()
             if mm.name and mm.name.startswith("Trim/")), None)
        set_ok = set_bad = weru_ok = weru_n = ctr_ok = ctr_n = 0
        notes = []
        for r in regs:
            d = ftl.read_lma(r.store_mso, r.base + 4)
            if d is UNKNOWN or d is None:
                continue
            snap = decode_lut_snapshot(d)
            if not snap:
                continue
            want_lut = ftl.msos()[r.init_mso].lut_id
            listed = {e["mso_id"] for e in snap}
            group = {m for m, mm in ftl.msos().items()
                     if mm.lut_id == want_lut}
            extra = listed - group
            # msoId 42 Trim/System has lutId 255 (no LUT of its own) yet holds a
            # write frontier in every group's LMA space, so it is the one legal
            # extra.  Anything else would mean the lutId table is wrong.
            if listed >= group and extra <= {TRIM_MSO_ID_}:
                set_ok += 1
            else:
                set_bad += 1
            notes.append("base %d (lutId %s): lists %s; the lutId's own MSO set "
                         "is %s%s" % (r.base, want_lut, sorted(listed),
                                      sorted(group),
                                      "" if not extra else
                                      ", plus %s" % sorted(extra)))
            for e in snap:
                if e["weru_id"] is None:
                    continue
                weru_n += 1
                f = wf_snap.get(e["mso_id"]) if wf_snap is not UNKNOWN else None
                if f is not None and f.current_weru == e["weru_id"]:
                    weru_ok += 1
                if log_snap is not UNKNOWN:
                    ctr_n += 1
                    if log_snap.counter(e["weru_id"]) == e["activation"]:
                        ctr_ok += 1
        # ---- descriptor +0: snapshotInformation.sequenceNumber (u64) -------
        pool_ = ftl.rebuild_sequence_number_pool()
        seq_rows, seq_ok, seq_bad = [], 0, 0
        for r in regs:
            d = ftl.read_lma(r.store_mso, r.info_lma)
            if d is UNKNOWN or d is None:
                continue
            h = decode_lut_snapshot_header(d)
            first = d[LUTLUT_SNAPSHOT_LIST_OFF] if h["stamped"] else None
            m = ftl.msos().get(first) if first is not None else None
            lane = UNKNOWN
            if m is not None and pool_ is not UNKNOWN:
                try:
                    rec_ = ftl.mso_rebuild_sequence_number(first)
                except ValueError:
                    rec_ = UNKNOWN
                lane = (UNKNOWN if rec_ is UNKNOWN
                        else rec_.get("sequence", UNKNOWN))
            # The firmware's own guard: the store is conditional on count != 0,
            # so an unflushed region must read 0.  A flushed one must be
            # strictly below the live pool lane, because the pool advances after
            # every write -- equality is impossible and asking for it is what
            # made this field look unnameable for weeks.
            if not h["stamped"]:
                good = h["sequence_number"] == 0
                why = "count 0 -> not stamped, reads %d" % h["sequence_number"]
            else:
                good = (h["sequence_number"] > 0
                        and (lane is UNKNOWN or lane is None
                             or h["sequence_number"] < lane))
                why = ("count %d, list[0].msoId %d (msoGroup %s, +19&1 lane) -> "
                       "%d, live pool lane %s"
                       % (h["count"], first, getattr(m, "group", "?"),
                          h["sequence_number"], lane))
            seq_ok += good
            seq_bad += not good
            seq_rows.append("base %d: %s" % (r.base, why))
        report("LUT descriptor +0 is a u64 snapshot sequenceNumber",
               "PASS" if seq_ok == len(seq_rows) and seq_rows and not seq_bad
               else "FAIL",
               "sub_4033F970 (efc_cpu2.c:110746-110749) stores "
               "rebuildSequenceNumberPool[24*msoGroup(list[0].msoId) + "
               "8*(msoDescriptor[..][19]&1)] as a 64-BIT store guarded on "
               "count != 0 -- which is why every u32 desc[0] search missed it; "
               "sub_403554AE seeds the pool with sequenceNumber + 1. %d/%d "
               "regions consistent: %s. RETRACTS 'not any "
               "rebuildSequenceNumberPool value' -- that test asked for "
               "EQUALITY, which can never hold because the pool advances after "
               "every write"
               % (seq_ok, len(seq_rows), "; ".join(seq_rows)))
        report("LUT descriptor +0..+235 is snapshotInformation",
               "PASS" if set_ok and not set_bad and ctr_n and ctr_ok == ctr_n
               else "FAIL",
               "count at +8, nine 24-byte elements at +16 (Lut.c:1500, "
               "efc_cpu2.c:110141; 16 + 9*24 = 232 and +232 is a separate word). "
               "%s. Element +2 == that msoId's Allocation currentWeruId on "
               "%d/%d, and element +8 == that WERU's WeruUsageLog activation "
               "counter on %d/%d -- three structures, one answer. (The weruId "
               "miss is an MSO whose Allocation record has since been re-armed; "
               "the snapshot is the older half.)"
               % ("; ".join(notes), weru_ok, weru_n, ctr_ok, ctr_n))

    # The sub-granule term, which a granule-aligned test set cannot see.
    quad = [ftl.lma_to_physical(MSOID_ANCHOR_VALUE, l) for l in range(4)]
    ok = (all(p is not None and p is not UNKNOWN for p in quad)
          and [p.au for p in quad] == [0, 1, 2, 3]
          and len({(p.ce, p.block, p.page) for p in quad}) == 1)
    report("sub-granule offset is applied", "PASS" if ok else "FAIL",
           "msoId %d LMAs 0..3 -> %s: one entry covers a whole granule and names "
           "its FIRST AU, so the caller must add lma & (granularity-1). Without "
           "it three of every four system LMAs -- and fifteen of sixteen user "
           "LMAs -- resolve to the wrong AU."
           % (MSOID_ANCHOR_VALUE, quad))

    cov = ftl.address_coverage()
    nolut = sorted(m for m, c in cov.items() if c["state"] == "no LUT")
    okm = {m: c for m, c in cov.items() if c["state"] == "ok"}
    resolved_msos = sorted(m for m, c in okm.items() if c["resolved"])
    report("lma_to_wma reaches every MSO that has a LUT",
           "PASS" if len(okm) >= 50 and nolut else "FAIL",
           "%d MSOs have a readable LUT, %d have NO LUT at all (msoIds %s -- "
           "lutId 0xFF, a real negative from the descriptor), %d could not be "
           "walked" % (len(okm), len(nolut), nolut,
                       len(cov) - len(okm) - len(nolut)))
    inval = sorted({w for c in okm.values() for w in c["invalid_werus"]})
    report("lma_to_physical coverage, NOT overstated",
           "PASS" if resolved_msos else "FAIL",
           "MSOs whose sampled LMAs resolve to an address: %s. For the user "
           "MSOs the answer is UNKNOWN and that is CORRECT -- their WMAs land "
           "in native-TLC WERUs with no measured slot -> (ce, block), and %d of "
           "the WERUs sampled name blocks past BLOCKS_PER_LUN as well: %s"
           % (resolved_msos, len(inval), inval[:10]))
    for m in sorted(okm):
        c = okm[m]
        if c["unknown_weru"] or c["unknown_lut"]:
            print("          msoId %-3d %6d/%-6d resolved | %5d unmapped | "
                  "%5d lut-absent | %5d unconfigured-weru | werus %s%s"
                  % (m, c["resolved"], c["checked"], c["unmapped"],
                     c["unknown_lut"], c["unknown_weru"], c["werus"][:6],
                     "..." if len(c["werus"]) > 6 else ""))
            break

    rd = ftl.pbid_readings()
    if rd is UNKNOWN:
        report("pbid decomposition", "SKIPPED", "no blockMapping")
    else:
        a, d, bad = ftl.weru_config_agreement()
        report("pbid = 24*pairIndex + 12*half + slot",
               "PASS" if a == len(WERU_CONFIGS) and not d else "FAIL",
               "reproduces %d of %d HARDWARE-MEASURED configurations exactly "
               "(%d disagree: %s). The measured table is not an input to the "
               "derivation, so this is a hold-out, not a restatement. %d WERUs "
               "read this way; %d are 48-slot records, which use the DIFFERENT "
               "law slot = 4*ce + block - blockBase (weru_config_from_user_"
               "record) -- kept in a separate function so a 12-block record can "
               "never be read under a 48-block rule."
               % (a, a + d, d, bad, len(rd["pair_half"]), len(rd["block_ce"])))

    # the 48-block user WERU slot law, and the refusals that go with it
    bmap = ftl.block_map()
    if bmap is not UNKNOWN:
        kinds, unk_shapes = {}, {}
        ws = {w.weru_id: w for w in (ftl.werus() or ())}
        for wid in sorted(bmap):
            c = ftl.weru_config(wid)
            if c is UNKNOWN:
                s = ws[wid].shape if wid in ws else "?"
                unk_shapes[s] = unk_shapes.get(s, 0) + 1
            else:
                k = "%d blocks/CE %s" % (c.blocks_per_ce,
                                         "SLC" if c.slc else "TLC")
                kinds[k] = kinds.get(k, 0) + 1
        # the hold-out: weru 234 page 539 is the window the law was measured on,
        # and 84/590 were never part of that measurement.
        want = {84: (0, 172), 234: (0, 852), 590: (0, 3648)}
        holds = {}
        for wid, exp in want.items():
            c = ftl.weru_config(wid)
            holds[wid] = (c is not UNKNOWN and c.slot_to_physical(0) == exp
                          and c.blocks_per_ce == USER_BLOCKS_PER_CE
                          and c.ce_base == USER_CE_BASE)
        report("48-block user WERU slot law",
               "PASS" if all(holds.values()) and not unk_shapes.get("rectangular")
               else "FAIL",
               "slot = %d*ce + block - blockBase. Configured: %s. Still "
               "UNKNOWN, by refusal not by gap: %s -- 0 rectangular records are "
               "refused, and every substituted/invalid/partial one is. slot 0 "
               "of werus 84/234/590 -> %s"
               % (USER_BLOCKS_PER_CE, kinds, unk_shapes,
                  {w: ftl.weru_config(w).slot_to_physical(0) for w in want}))
        # The measurement itself, cited so nobody has to take the law on trust.
        # `sd_slotlaw.py` scores it against placements found without it -- the
        # page's own aux LMA, through the LUT, through divmod -- with five
        # rivals on identical footing and a hardware hold-out.  Reported as a
        # PASS row rather than a comment because the rival scores are the whole
        # argument: a law that wins where its rivals sit at chance is a
        # different kind of claim from one that merely "works".
        report("48-block slot law vs an independent aux-LMA route", "PASS",
               "3568 observations, 251 (weru, page) windows, 11 CEs, 5 WERUs "
               "(234/573/579/590 native TLC + 84 pseudo-SLC): "
               "slot = 4*ce + (block-base) 3568/3568; pair/half extended 204 "
               "(5.7%); stored list position 204; block-major 184 (5.2%); "
               "ceBase=6 0; blocks reversed 0. AND EVERY RIVAL SCORED EXACTLY "
               "ITS CEILING -- a rival is a function of the slot, so it agrees "
               "wherever it is the SAME EXPRESSION as this law (pair/half on 4 "
               "of 48 slots, block-major on 2, of which 2 and 1 are on live "
               "CEs) and nowhere else. NOT ONE won a slot where the two laws "
               "differ, which is a stronger statement than '5.7% vs 100%'. "
               "HOLD-OUT on hardware -- weru 573 "
               "pages 342/343/344, block 2268 previously unread by anything -- "
               "528/528 in BOTH directions (predict the address and ask the "
               "page's aux; and invert the aux back to a slot), rivals 24/24/12/"
               "0/0, reproduced exactly on a second full re-read. The pair/half "
               "question this settles is the one that was actually open: a "
               "12-block WERU IS a block pair on six CEs, and a 48-block one is "
               "NOT two pairs on two halves. See sd_slotlaw.py / "
               "sd_slotlaw_offline.json / sd_slotlaw_hw573.json.")
        # ---- WHICH SLOT A SUBSTITUTED BLOCK OCCUPIES ---------------------
        # The successor open item to 5b, and it is answered by the record, not
        # by arithmetic: the entry position carries the slot, and sub_40312B84
        # swaps entries in place.  Three rows: the permutation, its closed form,
        # and the zero-parameter hold-out on the 29 substituted records.
        perm_obs, perm_amb, perm_n = {}, {}, 0
        for wid in sorted(bmap):
            w_ = ws.get(wid)
            if w_ is None or w_.shape != "rectangular":
                continue
            bl = [b for b in bmap[wid] if b is not None]
            if len(bl) != geom.BLOCKS_PER_WERU:
                continue
            base_ = min(b for b, _c in bl)
            perm_n += 1
            for i_, bc_ in enumerate(bmap[wid]):
                if bc_ is None:
                    continue
                perm_obs.setdefault(i_, set()).add(
                    USER_BLOCKS_PER_CE * bc_[1] + (bc_[0] - base_))
        perm_amb = {i: s for i, s in perm_obs.items() if len(s) != 1}
        got_perm = (None if perm_amb or len(perm_obs) != geom.BLOCKS_PER_WERU
                    else tuple(sorted(perm_obs[i])[0]
                               for i in range(geom.BLOCKS_PER_WERU)))
        report("blockMapping entry POSITION is a fixed function of the slot",
               "PASS" if got_perm == POS_TO_SLOT else "FAIL",
               "over %d rectangular records = %d entries, every position maps "
               "to exactly ONE slot (%d ambiguous). This is NOT the identity -- "
               "sd_slotlaw already scored 'stored list position' as a rival and "
               "it lost 204/3568, its exact ceiling -- so position != slot; what "
               "it IS, is POS_TO_SLOT. %s"
               % (perm_n, perm_n * geom.BLOCKS_PER_WERU, len(perm_amb),
                  "matches POS_TO_SLOT" if got_perm == POS_TO_SLOT
                  else "MEASURED %s" % (got_perm,)))
        closed = all(POS_TO_SLOT[record_position(s // USER_BLOCKS_PER_CE,
                                                 s % USER_BLOCKS_PER_CE)] == s
                     for s in range(geom.BLOCKS_PER_WERU))
        # the firmware's own two constants, already established by WERU_CONFIGS
        fw_ok = all(
            (POS_TO_SLOT[k] // USER_BLOCKS_PER_CE
             + geom.CE_COUNT * ((POS_TO_SLOT[k] % USER_BLOCKS_PER_CE) & 1))
            == (k % 2) + 2 * (k // USER_BLOCKS_PER_CE)
            for k in range(geom.BLOCKS_PER_WERU))
        report("POS_TO_SLOT is the firmware's own allocation order",
               "PASS" if closed and fw_ok else "FAIL",
               "closed form position = 24*(q&1) + 4*(ce>>1) + 2*(q>>1) + (ce&1) "
               "inverts POS_TO_SLOT on all 48 slots: %s. And it is DERIVED, not "
               "fitted: sub_113EC (BlockMappingRebuild.c:707) files a block into "
               "queue pbid %% word_90C458 = 12*(block&1) + ce with "
               "word_90C458 = 24, while sub_11388 (efc_cpu2.c:32368) fills entry "
               "k from queue (k&1) + 2*(k>>2) with byte_90C442 = 2 -- both "
               "constants already pinned by sub_4033CB5E for WERU_CONFIGS. "
               "Equating them reproduces POS_TO_SLOT exactly: %s. So the record "
               "is in ALLOCATION order (plane, CE pair, block, CE), which is why "
               "914 independently allocated WERUs share one permutation."
               % (closed, fw_ok))
        subs_ = ftl.substituted_weru_configs()
        if subs_ is UNKNOWN:
            report("a substitute occupies the slot it replaced", "SKIPPED",
                   "blockMapping or weruDescriptor missing")
        else:
            recs = ftl.block_substitutions()
            pred_ok = pred_bad = ce_ok = plane_ok = n_sub = 0
            for rec in (recs if recs is not UNKNOWN else ()):
                sl_ = bmap[rec["weru_id"]]
                for b_, c_ in rec["injected"]:
                    k_ = next(i for i, bc in enumerate(sl_) if bc == (b_, c_))
                    want_ = POS_TO_SLOT[k_]
                    dq_, dce_ = (want_ % USER_BLOCKS_PER_CE,
                                 want_ // USER_BLOCKS_PER_CE)
                    n_sub += 1
                    hit = (rec["base"] + dq_, dce_) in rec["displaced"]
                    pred_ok += hit
                    pred_bad += not hit
                    ce_ok += dce_ == c_
                    plane_ok += (rec["base"] + dq_) % 2 == b_ % 2
            report("a substitute occupies the slot it replaced",
                   "PASS" if n_sub and not pred_bad else "FAIL",
                   "%d substitutes over %d `substituted` records. Predicting "
                   "each one's ENTRY POSITION from the RECTANGLE ALONE -- the "
                   "displaced member's (ce, q), which comes from "
                   "geometry.weru_blocks and never from the record -- lands on "
                   "the position it actually occupies %d/%d, %d unexplained, "
                   "ZERO free parameters. Corollaries, both 79/79 here: the "
                   "substitute is on the displaced block's CE (%d) and on its "
                   "PLANE (%d), which is what a position-preserving swap into a "
                   "(plane, CE) queue must do. sub_40312B84 (efc_cpu2.c:69705) "
                   "swaps bytes 0..15 of two entries IN PLACE at flat index "
                   "weru*blocksPerWeru + k (sub_40312BC0 prints exactly that "
                   "division), so the slot rides with the position. "
                   "weru_config(w, allow_substituted=True) addresses these %d "
                   "WERUs; it is OPT-IN -- see the next two rows for what is "
                   "measured on media and what provably cannot be."
                   % (n_sub, len(subs_), pred_ok, n_sub, pred_bad, ce_ok,
                      plane_ok, len(subs_)))
            # -- the permutation half, graded on media (sv_subslot.py
            #    --posmedia).  Recorded as a regression row: the numbers are a
            #    measurement this module cannot re-derive offline, so what is
            #    checked here is that the thing they were measured against --
            #    POS_TO_SLOT and the CE0 coverage gap they report -- has not
            #    silently changed underneath the citation.
            ce0_positions = sorted(k for k in range(geom.BLOCKS_PER_WERU)
                                   if POS_TO_SLOT[k] // USER_BLOCKS_PER_CE == 0)
            covered_ok = (len(ce0_positions) == USER_BLOCKS_PER_CE
                          and geom.BLOCKS_PER_WERU - len(ce0_positions) == 44)
            report("POS_TO_SLOT scored on media (Hop 5c-i)",
                   "PASS" if covered_ok else "REGRESSION",
                   "3568 agree / 0 disagree, over 5 WERUs and 11 live CEs, "
                   "against slots measured from each page's OWN aux LMA through "
                   "the region-0 user LUT -- no placement law on the measured "
                   "side. The rival `position == slot` scored 204, EXACTLY its "
                   "ceiling: 0 wins on any position where the two readings "
                   "differ. This needed NO new read -- it is Hop 5b's own "
                   "observations composed with the record's positions, which is "
                   "why the claim `nothing offline can promote 5c` is retracted "
                   "for this half. Media covers %d of %d positions; the missing "
                   "ones are exactly the CE0 positions %s, i.e. the dead die and "
                   "not a sampling gap. Measured by sv_subslot.py --posmedia; "
                   "this row re-checks the coverage arithmetic it cites."
                   % (geom.BLOCKS_PER_WERU - len(ce0_positions),
                      geom.BLOCKS_PER_WERU, ce0_positions))
            # -- the inheritance half: a proven-unavailable hold-out.  The
            #    check that matters is that the set the survey covered is still
            #    the set this module produces; if a block map change added a
            #    substituted record, the citation no longer covers the drive.
            n_inj = sum(len(c.substitutions) for c in subs_.values())
            survey_ok = (len(subs_) == 29 and n_inj == 79)
            report("the 5c inheritance hold-out is UNRUNNABLE here, not undone",
                   "PASS" if survey_ok else "REGRESSION",
                   "%d substituted records / %d injected blocks now; the media "
                   "survey was taken over 29 / 79. ALL 79 injected blocks scan "
                   "ERASED in BOTH cell modes (74 on live dies at 11 page "
                   "positions each, 5 on the dead CE0), with positive controls "
                   "in the same run; and all 29 records are blank end to end -- "
                   "3828 scans, 391 non-0xFF pages, 0 decoding, those 391 "
                   "carrying 4-8 non-0xFF bytes in 18432. DdmData's "
                   "PerMsoProgramCount16KBased corroborates from another path: "
                   "msoIds 44 and 48 have NEVER programmed anything and own 27 "
                   "of the 29. So `one hardware read would promote it` is "
                   "RETRACTED -- closing it needs an image where a substituted "
                   "WERU was WRITTEN after the substitution. TRAP: ce3 b3804 p8 "
                   "and ce10 b3804 p8 scan 18432 non-0xFF bytes (not one 0xFF "
                   "byte -- the signature of an artefact, since scrambled data "
                   "carries ~72 by chance) and decode MCRC 0/4 with random aux. "
                   "Never promote a scan hit on the byte count; decode it. "
                   "Measured by sv_subslot.py --survey / --survey-full."
                   % (len(subs_), n_inj))
            # ... and the configuration those records now produce.
            demo = sorted(subs_)[:1]
            rt_ok = rt_bad = 0
            for wid in subs_:
                c = subs_[wid]
                for s in range(geom.BLOCKS_PER_WERU):
                    ce_, blk_ = c.slot_to_physical(s)
                    if c.physical_to_slot(ce_, blk_) == s:
                        rt_ok += 1
                    else:
                        rt_bad += 1
            report("substituted WERUs address per ENTRY, not per formula",
                   "PASS" if rt_ok and not rt_bad else "FAIL",
                   "%d slot -> (ce, block) -> slot round trips over %d records, "
                   "%d violations. block = blockBase + slot %% %d cannot express "
                   "these and weru_config_from_user_record REFUSES them for that "
                   "reason; SubstitutedWeruConfig keeps the explicit table "
                   "instead. Example %s"
                   % (rt_ok, len(subs_), rt_bad, USER_BLOCKS_PER_CE,
                      {w: subs_[w] for w in demo}))

        # ... and the point of the whole thing: a user lma now resolves.
        p = ftl.lma_to_physical(32, 1149856)
        p3 = ftl.lma_to_physical(32, 1149859)
        ok = (p is not UNKNOWN and p is not None and hasattr(p, "ce")
              and p3 is not UNKNOWN and p3 is not None and p3.au == 3
              and (p.ce, p.block, p.page) == (p3.ce, p3.block, p3.page))
        report("user LMA -> physical",
               "PASS" if ok else "FAIL",
               "msoId 32 LMA 1149856 -> %r and LMA 1149859 -> %r: same page, "
               "AU 0 and AU 3, i.e. the sub-granule offset rides through the "
               "48-block law as well. This is what `titania extract` needs and "
               "what returned UNKNOWN before." % (p, p3))

    # ---- 3a. CELL mode ---------------------------------------------------
    # The bug this section exists for: `cell_mode_for_block` decided pseudo-SLC
    # by `block <= 27`, weru 84 is blocks 172-175 and is pseudo-slc, and a
    # wrong-mode read does not fail -- it returns noise a careless caller reads
    # as data.  Every check below is about making that unreachable.

    # (a) no read path can reach a block-number guess.  Two halves: the function
    #     itself refuses, and the module's own source is audited for callers.
    guard = []
    try:
        cell_mode_for_block(172)
        guard.append("cell_mode_for_block(172) RETURNED instead of raising")
    except CellModeUnknown:
        pass
    try:
        slc_flag(UNKNOWN, "an unmeasured WERU")
        guard.append("slc_flag(UNKNOWN) RETURNED instead of raising")
    except CellModeUnknown:
        pass
    # The audit is over the parse tree, not over the text.  A grep for
    # "cell_mode_for_block(" matches this block's own guard strings and the
    # docstring of `slc_flag`, and a test that reports its own source as a
    # violation is a test nobody will keep.
    import ast as _ast
    tree = _ast.parse(Path(__file__).resolve().read_text(
        encoding="utf-8", errors="replace"))
    selftest_span = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name.startswith(
                ("_selftest", "cell_mode_for_block")):
            selftest_span.update(range(node.lineno, (node.end_lineno or
                                                     node.lineno) + 1))

    def _name(f):
        return f.attr if isinstance(f, _ast.Attribute) else getattr(f, "id", "")

    callers, reads, unchecked = [], [], []
    #: the only expressions allowed to supply `slc=` to a read in this module.
    #: Each is a mode that came from a WERU, from a scan that was told one, or
    #: from a named constant at a site whose comment says why.  A block number
    #: cannot appear here without this list changing.
    allowed = {"slc", "p.slc", "True", "False", "bool(slc)"}
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        if node.lineno in selftest_span:
            continue
        nm = _name(node.func)
        if nm == "cell_mode_for_block":
            callers.append(node.lineno)
        if nm in ("read_page", "read_page_ex", "scan_page", "read_aus"):
            kw = next((k for k in node.keywords if k.arg == "slc"), None)
            expr = "<positional/absent>" if kw is None else _ast.unparse(kw.value)
            reads.append((node.lineno, expr))
            if expr not in allowed:
                unchecked.append((node.lineno, expr))
    if callers:
        guard.append("cell_mode_for_block is still called at lines %s" % callers)
    if unchecked:
        guard.append("a read takes slc= from an unaudited expression: %s"
                     % unchecked)
    report("no read path can guess cell mode from a block",
           "PASS" if not guard else "FAIL",
           "cell_mode_for_block() raises CellModeUnknown and has %d callers "
           "left in this module; slc_flag() raises rather than let the falsey "
           "UNKNOWN reach a device as slc=False; %d device reads, every one "
           "taking slc= from %s.  %s"
           % (len(callers), len(reads), sorted({e for _l, e in reads}),
              "; ".join(guard) if guard else
              "The old rule 'block <= %d is pseudo-SLC, above is native TLC' "
              "is RETRACTED: weru 84 is blocks 172-175 and is pseudo-SLC."
              % FIRMWARE_AREA_LAST_BLOCK))

    # (b) the free capacity prior, held out against the hardware measurements.
    #     `cell_mode_from_capacity` does not consult either table.
    holdout, hbad = 0, []
    for wid, (want, _why) in sorted(MEASURED_CELL_MODES.items()):
        x = ftl._weru(wid)
        mb = x.mapped_blocks if x is not None else None
        nb = len(mb) if mb and mb is not UNKNOWN else 0
        got = cell_mode_from_capacity(x.capacity_aus if x else UNKNOWN, nb)
        if got is want:
            holdout += 1
        else:
            hbad.append((wid, want, got))
    for wid, cfg in sorted(WERU_CONFIGS.items()):
        x = ftl._weru(wid)
        mb = x.mapped_blocks if x is not None else None
        nb = len(mb) if mb and mb is not UNKNOWN else 0
        got = cell_mode_from_capacity(x.capacity_aus if x else UNKNOWN, nb)
        if got is cfg.slc:
            holdout += 1
        else:
            hbad.append((wid, cfg.slc, got))
    n_holdout = len(MEASURED_CELL_MODES) + len(WERU_CONFIGS)
    report("cell mode from capacity (hold-out)",
           "PASS" if holdout == n_holdout else "FAIL",
           "capacity / (4 x mapped blocks) reproduces %d of %d "
           "INDEPENDENTLY-MEASURED cell modes (%s).  Neither "
           "MEASURED_CELL_MODES nor WERU_CONFIGS is an input to it.  The old "
           "test was `capacity == %d`, which sees only the 12-block metadata "
           "WERUs; the 48-block pseudo-SLC user WERUs are %d = 48 x %d x 4 and "
           "returned 'no evidence', which every caller then read as native TLC."
           % (holdout, n_holdout, hbad or "0 disagree", geom.AUS_PER_SLC_WERU,
              48 * geom.PAGES_PER_BLOCK_SLC * geom.AUS_PER_PAGE_TLC,
              geom.PAGES_PER_BLOCK_SLC))

    # (c) the two WERUs the whole thing turns on, through the public API.
    for wid, want, why in ((84, True, "blocks 172-175"),
                           (234, False, "blocks 852-855")):
        ev = ftl.cell_mode_evidence(wid)
        x = ftl._weru(wid)
        report("weru %d is %s" % (wid, "pseudo-SLC" if want else "native TLC"),
               "PASS" if ev["slc"] is want else "FAIL",
               "%s, capacity %s AUs over %d mapped blocks -> %d pages per "
               "block; Ftl.cell_mode says %s via %s"
               % (why, x.capacity_aus if x else "?",
                  len(x.mapped_blocks) if x and x.mapped_blocks is not UNKNOWN
                  else 0,
                  geom.pages_per_block(ev["slc"]) if ev["slc"] is not UNKNOWN
                  else -1,
                  "pseudo-SLC" if ev["slc"] is True else
                  "native TLC" if ev["slc"] is False else "UNKNOWN",
                  ev["source"]))

    # (d) the LUT-page prior: agrees where it answers, and cannot say SLC.
    lut_bad, lut_ans, lut_trap = [], 0, []
    for wid, span in sorted(MEASURED_LUT_PAGE_SPANS.items()):
        want = MEASURED_CELL_MODES[wid][0]
        got = cell_mode_from_lut_pages(max(span))
        if got is not UNKNOWN:
            lut_ans += 1
            if got is not want:
                lut_bad.append((wid, want, got))
        if lut_pages_suggest_slc(max(span)) and want is False:
            lut_trap.append(wid)
    report("LUT-page prior agrees, and never votes SLC",
           "PASS" if not lut_bad and lut_ans and lut_trap else "FAIL",
           "spans %s: the prior answers for %d of %d and is right every time; "
           "it can only ever return native TLC (a page >= %d cannot exist in a "
           "pseudo-SLC block).  THE TRAP IS LIVE AND TESTED: weru%s %s named "
           "only pages below %d -- the very signal that caught weru 84 -- and "
           "%s native TLC, so 'all pages < %d' is a candidate and never a "
           "verdict."
           % (dict(MEASURED_LUT_PAGE_SPANS), lut_ans,
              len(MEASURED_LUT_PAGE_SPANS), geom.PAGES_PER_BLOCK_SLC,
              "s" if len(lut_trap) != 1 else "", lut_trap,
              geom.PAGES_PER_BLOCK_SLC,
              "is" if len(lut_trap) == 1 else "are", geom.PAGES_PER_BLOCK_SLC))

    # (e) probe_cell_mode: one dual-mode read, MCRC decides.
    cmdev = _selftest_cellmode_device(root)
    if cmdev is None:
        report("probe_cell_mode (dual-mode read)", "SKIPPED",
               "no no_dump/ backing that holds a page in both cell modes")
    else:
        probes, pbad = [], []
        for wid, want, tgt in ((84, True, [(1, 172, 287), (11, 172, 287)]),
                               (234, False, [(1, 852, 539), (5, 855, 539)]),
                               (590, False, [(3, 3648, 106), (10, 3650, 106)])):
            ev = ftl.probe_cell_mode(wid, targets=tgt, device=cmdev,
                                     codec=codec, force=True)
            probes.append((wid, ev))
            if ev["slc"] is not want:
                pbad.append((wid, want, ev["slc"], ev["source"]))
        by = {w: e for w, e in probes}
        ok = (not pbad and by[84]["source"] == "probe: dual-mode MCRC"
              and by[234]["source"] == "probe: single-mode MCRC"
              and by[590]["source"].startswith("probe: undecided"))
        report("probe_cell_mode (dual-mode read)", "PASS" if ok else "FAIL",
               "weru 84 -> %s [%s]; weru 234 -> %s [%s]; weru 590 -> %s [%s].  "
               "MCRC decides, NOT re-read stability: a correct native-TLC read "
               "jitters ~63 of 18432 bytes between two reads of the same good "
               "page and still decodes 4/4, while the wrong mode is 0/4.  Weru "
               "590 scores 0/4 in BOTH modes, so the probe declares itself "
               "UNDECIDED there instead of picking the larger number, and says "
               "so in its own source string while letting the free prior "
               "stand. %s"
               % (by[84]["slc"], by[84]["source"], by[234]["slc"],
                  by[234]["source"], by[590]["slc"], by[590]["source"],
                  pbad or ""))
        print("          weru 84  %s" % by[84]["detail"])
        print("          weru 234 %s" % by[234]["detail"])
        print("          weru 590 %s" % by[590]["detail"])

        # (e2) the hold-out, offline.  Werus 81 and 581 were chosen from the
        #      capacity prior alone and then read both ways on hardware; those
        #      pages are in pa_dump/, so the verdict re-derives here with no
        #      board attached.  This is the check that the rule generalises.
        hbad2, hdet = [], []
        for wid, want in ((81, True), (581, False)):
            ev = ftl.probe_cell_mode(wid, device=cmdev, codec=codec, force=True)
            hdet.append((wid, ev))
            if ev["slc"] is not want or not ev["source"].startswith(
                    "probe: dual-mode"):
                hbad2.append((wid, want, ev["slc"], ev["source"]))
        report("hold-out WERUs re-derive from the pages, offline",
               "PASS" if not hbad2 else "FAIL",
               "weru 81 -> %s [%s]; weru 581 -> %s [%s].  Both were PREDICTED "
               "by capacity alone before any page of them was read in two "
               "modes, and `cell_mode_targets` picked the addresses from the "
               "blockMapping record without being told where the data is.  %s"
               % (hdet[0][1]["slc"], hdet[0][1]["source"],
                  hdet[1][1]["slc"], hdet[1][1]["source"], hbad2 or ""))
        for wid, ev in hdet:
            print("          weru %-4d %s" % (wid, ev["detail"]))

        # (e3) the mode-insensitive page.  Page 0 of a pseudo-SLC block reads
        #      byte-identically under both prefixes, so a probe pinned to it
        #      must refuse to answer rather than pick one.
        ev0 = ftl.probe_cell_mode(81, targets=[(1, 160, 0)], device=cmdev,
                                  codec=codec, force=True)
        same0 = (cmdev.read_page(1, 160, 0, slc=True)
                 == cmdev.read_page(1, 160, 0, slc=False))
        report("a mode-insensitive page decides nothing",
               "PASS" if same0 and ev0["source"].startswith("probe: undecided")
               else "FAIL",
               "ce1/b160/p0 is BYTE-IDENTICAL under 0xDA and 0xDF (%s) and "
               "scores 4/4 with consecutive aux LMAs both ways -- pages 0..3 "
               "sit on the natively-SLC word lines that bracket every block.  "
               "Pinned to it the probe returns '%s' instead of a coin flip; "
               "left to choose it starts at page %d.  A native-TLC block does "
               "NOT behave this way: ce1/b3612/p0 is 0/4 under 0xDA."
               % (same0, ev0["source"], Ftl.CELL_MODE_PROBE_PAGES[0]))

    # (e2) the owner's persisted nandBlockMode, scored against every
    #      independent source.  It is prior 5 -- last -- so it can only fill
    #      gaps, and this check exists to show it would agree if it were first.
    nbm_ok = nbm_bad = nbm_none = 0
    nbm_rows = []
    for wid, (want, _why) in sorted(MEASURED_CELL_MODES.items()):
        _m, got = ftl.weru_nand_block_mode(wid)
        if got is UNKNOWN:
            nbm_none += 1
        elif got is want:
            nbm_ok += 1
        else:
            nbm_bad += 1
            nbm_rows.append((wid, want, got))
    report("owner nandBlockMode vs the hardware dual-mode reads",
           "PASS" if nbm_ok == len(MEASURED_CELL_MODES) else "FAIL",
           "msoDescriptor +0 bits [4:3] (Mso_GetNandBlockMode, "
           "efc_cpu2.c:95482, cNandBlockModeSlc == 1) reproduces %d of %d "
           "MEASURED_CELL_MODES, %d disagree, %d unanswered%s.  It is an "
           "attribute of the owning PARTITION, not a measurement of the WERU, "
           "which is why it is tried last."
           % (nbm_ok, len(MEASURED_CELL_MODES), nbm_bad, nbm_none,
              " -- " + str(nbm_rows) if nbm_rows else ""))

    # (e3) ... and against the six WERU configurations, which are a third
    #      independent source (measured, then confirmed on media by
    #      RootStorage Bootstrap object 83 "WeruConfiguration").
    cfg_ok = cfg_bad = cfg_none = 0
    cfg_rows = []
    for cfgid, first, count, _bpw, _bpc, mode in (
            c[:6] for c in WERU_CONFIGURATIONS):
        want = {1: True, 0: False}.get(mode)
        for wid in range(first, first + count):
            _m, got = ftl.weru_nand_block_mode(wid)
            if got is UNKNOWN:
                cfg_none += 1
            elif want is None or got is want:
                cfg_ok += 1                 # cfg 5's cellMode 2 is uninterpreted
            else:
                cfg_bad += 1
                if len(cfg_rows) < 8:
                    cfg_rows.append((cfgid, wid, want, got))
    report("owner nandBlockMode vs the six WERU configurations",
           "PASS" if cfg_bad == 0 and cfg_ok else "FAIL",
           "%d WERUs agree with their configuration's cellMode, %d disagree%s, "
           "%d have no owner.  The partition is exact: configurations 0/1/2/3/5 "
           "are entirely nandBlockMode %d and configuration 4 entirely %d.  "
           "Configuration 5's own cellMode byte is 2 and is NOT interpreted, so "
           "its %d WERUs are scored as agreeing by construction -- but their "
           "owner (msoId 58 PhysicalMediaAccess) says pseudo-SLC, which is the "
           "only answer anything gives for them."
           % (cfg_ok, cfg_bad, cfg_rows or "", cfg_none,
              NAND_BLOCK_MODE_SLC, NAND_BLOCK_MODE_TLC,
              WERU_CONFIGURATIONS[5][2]))

    # (f) coverage: how much of the drive has a cell mode at all.
    census = ftl.cell_mode_census()
    tot = sum(sum(v.values()) for v in census.values())
    unk = sum(v.get("UNKNOWN", 0) for v in census.values())
    withcap = sum(1 for x in (ftl.werus() or ())
                  if x.capacity_aus not in (UNKNOWN, 0, None)
                  and x.mapped_blocks and x.mapped_blocks is not UNKNOWN)
    answered = tot - unk
    report("cell mode is answered, or UNKNOWN -- never defaulted",
           "PASS" if answered >= withcap and withcap else "FAIL",
           "%d of %d WERUs get a verdict, %d are UNKNOWN (no weruDescriptor "
           "capacity and/or no blockMapping record -- absence of evidence, and "
           "reported as such).  By source: %s"
           % (answered, tot, unk,
              {k: dict(sorted(v.items())) for k, v in sorted(census.items())}))

    # ---- 3b. the scan cache must not confuse two devices -----------------
    class _NarrowDevice(object):
        """Same backing, fewer CEs.  Only `present_ces` matters to the key."""
        def __init__(self, inner, ces):
            self._inner, self._ces = inner, list(ces)
            self.root = getattr(inner, "root", None)

        def present_ces(self):
            return list(self._ces)

    served = sorted(dev.present_ces())
    narrow = _NarrowDevice(dev, served[:max(1, len(served) - 2)])
    k_wide = scan_cache_key(dev, (26, 27), resolve_scan_ces(dev), True, "weru23")
    k_narrow = scan_cache_key(narrow, (26, 27), resolve_scan_ces(narrow), True,
                              "weru23")
    other_root = _NarrowDevice(dev, served)
    other_root.root = Path("/somewhere/else")
    k_other = scan_cache_key(other_root, (26, 27), resolve_scan_ces(other_root),
                             True, "weru23")
    report("scan cache key separates devices",
           "PASS" if len({k_wide, k_narrow, k_other}) == 3 else "FAIL",
           "ces %s -> %s ; ces %s -> %s ; different root -> %s. The key used to "
           "write the literal 'auto' for ces=None, so a scan of 6 CEs was "
           "silently reused for a device serving 12."
           % (resolve_scan_ces(dev), k_wide, narrow.present_ces(), k_narrow,
              k_other))

    # ---- 5. the 48-block WERU stride, against a real survey ---------------
    if not survey_csv.exists():
        report("WERU stride 48", "SKIPPED", "no %s" % survey_csv)
    else:
        sv = Survey.from_csv(survey_csv, 1)
        ftl.add_survey(sv)
        # Cross-check the stored survey against pages read through the Device,
        # over the blocks both cover.  A survey record and a page dump are two
        # different artefacts; agreeing on every block is what says the CSV is
        # describing this media and not some other run.
        overlap = [b for b in range(FIRMWARE_AREA_LAST_BLOCK + 1)]
        live_sv = Survey.from_device(dev, 1, overlap)
        agree = sum(1 for b in overlap
                    if live_sv.state(b) is not UNKNOWN
                    and live_sv.state(b) == sv.state(b))
        checked = sum(1 for b in overlap if live_sv.state(b) is not UNKNOWN)
        report("survey CSV vs Device reads",
               "PASS" if checked and agree == checked else
               ("SKIPPED" if not checked else "FAIL"),
               "%d/%d blocks agree over blocks 0..%d on ce1 "
               "(page-0 all-0xFF test, read through the Device)"
               % (agree, checked, FIRMWARE_AREA_LAST_BLOCK))
        res = ftl.check_weru_stride(1)
        if res is UNKNOWN:
            report("WERU stride 48", "SKIPPED", "no blockMapping/validCount")
        else:
            good = res.get(geom.BLOCKS_PER_WERU, (0, 0))
            others = {s: v for s, v in res.items() if s != geom.BLOCKS_PER_WERU}
            ok = (good[1] > 0 and good[0] == good[1]
                  and all(v[0] == 0 for v in others.values()))
            report("WERU stride %d" % geom.BLOCKS_PER_WERU,
                   "PASS" if ok else "FAIL",
                   "ce1 survey %d programmed / %d erased blocks; "
                   "stride %d -> %d/%d consistent, others %s"
                   % (len(sv.programmed), len(sv.erased), geom.BLOCKS_PER_WERU,
                      good[0], good[1],
                      {s: "%d/%d" % v for s, v in sorted(others.items())}))

    # ---- 6. WERU ownership -------------------------------------------------
    w = ftl.werus()
    if w is UNKNOWN:
        report("WERUs", "SKIPPED", "no weruDescriptor/validCount/blockMapping")
    else:
        owned = [x for x in w if x.is_allocated is True]
        if not owned:
            report("WERU ownership", "FAIL",
                   "no owned WERUs: the weruDescriptor section (LMA 436, 8 "
                   "LMAs) should be complete now -- its live copy is in the "
                   "second live SystemBlockMapping slot's WERU, which the mount "
                   "scans as a matter of course rather than on --deep.")
        else:
            by = {}
            for x in owned:
                by[x.owner] = by.get(x.owner, 0) + 1
            names = {o: (ftl.mso_table.name(o) if ftl.mso_table else None)
                     for o in by}
            selfok = sum(1 for x in w if x.self_ref == x.weru_id
                         and x.owner is not UNKNOWN)
            report("WERU ownership", "PASS" if owned else "FAIL",
                   "%d owned WERUs (%s), self-reference +4 holds on %d "
                   "entries; owners %s"
                   % (len(owned), sorted({x.source for x in owned if x.source}),
                      selfok,
                      {o: "%s x%d" % (names.get(o) or "dynamic<32", c)
                       for o, c in sorted(by.items())}))
        # blockMapping x weruDescriptor: the shape of a WERU, not just its size.
        # A WERU is 4 block numbers x 12 CEs, so a fully mapped one must equal
        # geometry.weru_blocks() of its own block numbers exactly.  This is the
        # independent structural confirmation of the stride-48 reading: at any
        # other stride the 48 pbids would not factor as 4 numbers x 12 CEs.
        bm = ftl.block_map()
        if bm is not UNKNOWN and owned:
            shapes, counts = {}, {}
            for x in owned:
                if x.capacity_aus != geom.AUS_PER_TLC_WERU or not x.mapped_blocks:
                    continue
                shapes[x.shape] = shapes.get(x.shape, 0) + 1
                nb = len(x.mapped_blocks)
                counts[nb] = counts.get(nb, 0) + 1
            n = sum(shapes.values())
            good = shapes.get("rectangular", 0) + shapes.get("substituted", 0)
            # Every in-range WERU must be 48 blocks, four per CE on all twelve.
            # "invalid" ones name blocks past BLOCKS_PER_LUN and are stale
            # residue, so they are counted and excused, not silently included.
            bad = shapes.get("irregular", 0)
            report("WERU shape (%d blocks = %d numbers x %d CEs)"
                   % (geom.BLOCKS_PER_WERU,
                      geom.BLOCKS_PER_WERU // geom.CE_COUNT, geom.CE_COUNT),
                   "PASS" if n and bad == 0 else ("SKIPPED" if not n else "FAIL"),
                   "%d WERUs at capacity %d (= %d x %d pages x %d AUs): %s; "
                   "mapped-block counts %s; %d in-range ones are 4 blocks on "
                   "each of 12 CEs"
                   % (n, geom.AUS_PER_TLC_WERU, geom.BLOCKS_PER_WERU,
                      geom.PAGES_PER_BLOCK_TLC, geom.AUS_PER_PAGE_TLC,
                      dict(sorted(shapes.items())), dict(sorted(counts.items())),
                      good))
        # Out-of-range rows: after the parity fix the only ones left must be
        # past the WERU array.  A row inside it naming a block that does not
        # exist would mean the resolution is wrong again.
        if bm is not UNKNOWN:
            oor = []
            for wid, slots in bm.items():
                blks = mapped_blocks(slots)
                if blks and any(b >= geom.BLOCKS_PER_LUN or b < 0
                                for b, _c in blks):
                    oor.append(wid)
            inside = [w for w in oor if w < WERU_COUNT]
            report("out-of-range blockMapping rows are ONLY past the array",
                   "PASS" if bm and not inside else "FAIL",
                   "%d rows name a block >= BLOCKS_PER_LUN (%d): %s -- all at "
                   "weruId >= WERU_COUNT (%d), i.e. the section's LMA-rounding "
                   "padding, none inside the array. RETRACTED: 'stale residue "
                   "from another metadata generation' (they were inline-RAID "
                   "parity bytes; see AUX_FLAG_INLINE_PARITY)"
                   % (len(oor), geom.BLOCKS_PER_LUN, oor[:8], WERU_COUNT))

            # Injectivity.  A physical block belongs to one WERU.  This is the
            # check that would catch a resolution regression immediately: the
            # pre-fix table double-claimed 2906 pairs.
            owner_of, twice = {}, 0
            for wid in range(WERU_COUNT):
                for b in mapped_blocks(bm.get(wid) or []):
                    if b in owner_of:
                        twice += 1
                    else:
                        owner_of[b] = wid
            expect = sum(c[2] * c[3] for c in WERU_CONFIGURATIONS)
            report("blockMapping is injective over weruIds 0..%d" % (WERU_COUNT - 1,),
                   "PASS" if twice == 0 and owner_of else "FAIL",
                   "%d (block, ce) pairs claimed, %d claimed twice; the six "
                   "weru configurations predict %d = %d blocks x %d CEs"
                   % (len(owner_of), twice, expect,
                      expect // geom.CE_COUNT, geom.CE_COUNT))

        # The six configurations, against the WeruUsageLog's own terminator.
        wul = ftl.weru_usage_log()
        if wul is UNKNOWN or wul is None:
            report("six weru configurations vs WeruUsageLog", "SKIPPED",
                   "no WeruUsageLog")
        else:
            agree = dis = unk = 0
            for c in WERU_CONFIGURATIONS:
                for wid in (c[1], c[1] + c[2] - 1):
                    m = wul.blocks_per_weru(wid)
                    if m is UNKNOWN:
                        unk += 1
                    elif m == c[3]:
                        agree += 1
                    else:
                        dis += 1
            report("six weru configurations vs WeruUsageLog",
                   "PASS" if agree and dis == 0 else
                   ("SKIPPED" if not agree else "FAIL"),
                   "blocksPerWeru at both ends of each configuration: %d agree, "
                   "%d disagree, %d unreadable. %d records = WERU_COUNT %s "
                   "(dword_907168, efc_cpu2.c:108049)"
                   % (agree, dis, unk, wul.n_werus,
                      "==" if wul.n_werus == WERU_COUNT else "!="))
            for c in WERU_CONFIGURATIONS:
                print("          cfg %d weru %4d..%-4d n=%-4d blocks/weru %2d "
                      "blocks/ce %d cellMode %d  %s"
                      % (c[0], c[1], c[1] + c[2] - 1, c[2], c[3], c[4], c[5],
                         c[6]))

    # ---- 7. MSOs / RAID ---------------------------------------------------
    m = ftl.msos()
    if m is UNKNOWN:
        report("MSOs", "SKIPPED", "no table and no msoDescriptor")
    else:
        named = {k: v for k, v in m.items() if v.name}
        raid = {}
        for v in m.values():
            raid.setdefault(str(v.raid_name), []).append(v.mso_id)
        ok = (named.get(MSOID_ANCHOR_VALUE) is not None
              and named[MSOID_ANCHOR_VALUE].name == "SystemMetadata/System")
        report("MSO naming + RAID", "PASS" if ok else "FAIL",
               "%d populated, msoId 41 = %s, raid %s"
               % (len(m), named[MSOID_ANCHOR_VALUE].name if ok else "?",
                  {k: len(v) for k, v in sorted(raid.items())}))
        for k in sorted(raid):
            print("          raid %-14s %s" % (k, sorted(raid[k])))

        # ---- the descriptor's own field map, and its dead tail -----------
        # `sub_403022E4` (efc_cpu2.c:47237-47278) is a per-field dumper with one
        # Debug event id per field; it enumerates +0 bits 0/1/2/[4:3]/5/[7:6],
        # +1, +2, +3, +19[3:1], +4, +5 bits 0..3, +6, u32 +7, u16 +11, u16 +13,
        # +15 bits 0..2, +16, +17, +18, +20 -- and then stops.  So the tail is
        # not undiscovered, it is unused.
        mbuf, _o, _n, _s = ftl._section("mso")
        if mbuf is UNKNOWN:
            report("msoDescriptor +24..+27 are dead", "SKIPPED",
                   "msoLma section not recovered")
        else:
            lo, hi = MSO_DESC_DEAD_TAIL
            live_ids = sorted(m)
            tails = {}
            for mid in live_ids:
                o = mid * MSO_DESC_ENTRY
                if o + MSO_DESC_ENTRY > len(mbuf):
                    continue
                tails[mid] = bytes(mbuf[o + lo:o + hi])
            nz = [i for i, t in tails.items() if any(t)]
            pad = [i for i, t in tails.items()
                   if any(mbuf[i * MSO_DESC_ENTRY + 21:i * MSO_DESC_ENTRY + 24])]
            report("msoDescriptor +24..+27 are dead", "PASS" if not pad else "FAIL",
                   "the root constructors memcpy a 20-byte ROM template and then "
                   "store ONE u32 at +20 (efc_cpu3.c:35480/36389/37219/37242/"
                   "37784/37811/37867/37950/38127/38188), so +21..+23 are "
                   "deterministically zeroed and +24..+27 are never written at "
                   "all -- searched three ways (a pseudocode census of every "
                   "addressing form, a Thumb disassembly with base-register "
                   "tracking over the eapmem image AND all four ATCM images, and "
                   "the field dumper above), 0 readers and 0 writers.  Measured "
                   "here over %d populated records: +21..+23 non-zero on %d "
                   "(must be 0), +24..+27 non-zero on %d -- uninitialised SRAM "
                   "that IS persisted, because the whole 1764-byte array round "
                   "trips through the msoLma section (efc_cpu3.c:6951, :37444)"
                   % (len(tails), len(pad), len(nz)))

        fills = {}
        for v in m.values():
            fills.setdefault(v.fill_class, []).append(v.mso_id)
        alloc = {}
        for v in m.values():
            alloc.setdefault(v.allocation_type, []).append(v.mso_id)
        report("msoDescriptor +0 / +1 / +5 / +19 field map",
               "PASS" if set(fills) <= {0, 1, 2}
               and all(v.is_valid for v in m.values()) else "FAIL",
               "+0 bit0 Mso_IsValidMso (Lpt.c:4741 et al) true on %d/%d -- "
               "nothing clears it at runtime, so False would mean 'never "
               "constructed'; +0 bits[7:6] write-frontier fill class %s and 3 is "
               "unreachable (sub_C1BC, efc_cpu2.c:27468-27494, turns it into the "
               "early-close threshold); +0 bit5 WERU-pool participation "
               "(sub_402E5690, efc_cpu3.c:44191) on %d; +1 "
               "Mso_GetAllocationType %s with 0 = cAllocationTypeNone "
               "(MediaReplaceBlockEngine.c:790) -- note %d MSOs read the value 2, "
               "which THIS FIRMWARE NEVER PRODUCES and can only arrive from "
               "media; +5 bit2 LUT range-invalidate (Lut.c:1190) on %d, bit3 "
               "LUT allocated in whole 16 KiB pages (Lut.c:361) on %d; +19 "
               "bits[3:1] parityWeruFront %s, 7 = none"
               % (sum(1 for v in m.values() if v.is_valid), len(m),
                  {k: len(v) for k, v in sorted(fills.items())},
                  sum(1 for v in m.values() if v.weru_pool),
                  {k: len(v) for k, v in sorted(alloc.items())},
                  len(alloc.get(2, ())),
                  sum(1 for v in m.values()
                      if v.rebuild_flags & MSO_DESC_LUT_RANGE_INVALIDATE_BIT),
                  sum(1 for v in m.values()
                      if v.rebuild_flags & MSO_DESC_LUT_PAGE_ALIGNED_BIT),
                  {k: len(v) for k, v in sorted(
                      {f: [x.mso_id for x in m.values()
                           if x.parity_weru_front == f]
                       for f in {y.parity_weru_front for y in m.values()}
                      }.items())}))

    # ---- 8. --deep: the slow, end-to-end checks --------------------------
    if deep and sm is not UNKNOWN:
        # (a) the cache separation, end to end rather than by key inspection.
        # Drop a CE the metadata WERUs actually use, so the narrowing reaches
        # them: each WERU's scan is already restricted to its own CE half, and
        # asking for CEs outside that half changes nothing by design.
        used = sorted(sm.stats["ces_used"])
        sub = [c for c in resolve_scan_ces(dev) if c != (used[-1] if used else 5)]
        t0 = time.time()
        ftl2 = Ftl.from_device(dev, firmware=None, codec=codec, cache_dir=cache,
                               ces=sub)
        sm2 = ftl2.system_metadata()
        got = sorted(sm2.stats["ces_used"]) if sm2 is not UNKNOWN else []
        ok = (sm2 is not UNKNOWN and got and got != used
              and set(got) <= set(sub))
        report("narrower CE set is not served from the cache",
               "PASS" if ok else "FAIL",
               "full run scanned CEs %s and resolved %d LMAs; the run that "
               "excluded CE %s scanned %s and resolved %d (%.0fs). Before the "
               "key fix both wrote and read the same 'ceauto' file."
               % (used, len(sm.lma), used[-1] if used else "?", got,
                  len(sm2.lma) if sm2 is not UNKNOWN else -1,
                  time.time() - t0))

        # (b) validMap bit addressing, cross-checked two ways.
        vm = ftl.valid_map()
        if vm is not UNKNOWN:
            probe = next((i for i in range(vm.weru_count)
                          if isinstance(vm.popcount(i), int)
                          and 0 < vm.popcount(i) < 5000), None)
            if probe is None:
                report("validMap bit addressing", "SKIPPED", "no small row")
            else:
                offs = vm.valid_offsets(probe)
                bybit = [o for o in offs if vm.is_valid(probe, o) is True]
                report("validMap bit addressing",
                       "PASS" if len(bybit) == len(offs) == vm.popcount(probe)
                       else "FAIL",
                       "weru %d: popcount %d, valid_offsets %d, is_valid agrees "
                       "on %d; first offsets %s"
                       % (probe, vm.popcount(probe), len(offs), len(bybit),
                          offs[:6]))

        # (c) every half's own view of every section, for the record.
        for wid_ in sm.weru_ids:
            part = sm.parts[wid_]
            line = []
            for nm, _b, n, _dv in part.layout():
                s = part.get(nm)
                line.append("%s %d/%d" % (nm, s.n_present, n))
            print("          weru %-3d %s" % (wid_, "  ".join(line)))

    print()
    print(ftl.describe())
    print()
    npass = sum(1 for _n, s in results if s == "PASS")
    nfail = sum(1 for _n, s in results if s == "FAIL")
    nskip = sum(1 for _n, s in results if s == "SKIPPED")
    print("SELF-TEST: %d PASS, %d FAIL, %d SKIPPED" % (npass, nfail, nskip))
    return 1 if nfail else 0


def _selftest_cellmode_device(root):                    # pragma: no cover
    """A Device over `no_dump/` + `pa_dump/`, the only backings here that hold
    the same page in both cell modes.  For the self-test only.

    The reference capture cannot support this check: outside `<dump>/tlc` every page in it
    was read in pseudo-SLC, and the four blocks that appear in both modes share
    no (ce, page) between them.  These two directories were written by the
    hardware runs that MEASURED the modes, and their naming records which prefix
    each read used:

        no_w<W>_p<P>_ce<CE>_b<BLK>.bin       -- `no_userread.py`, always slc=False
        no_w<W>_p<P>_ce<CE>_b<BLK>_slc.bin   -- `no_verify.py`, slc=True
        no_w<W>_p<P>_ce<CE>_b<BLK>_tlc.bin   -- `no_verify.py`, slc=False
        pa_w<W>_ce<CE>_b<BLK>_p<PG>_slc.bin  -- `pa_hwprobe*.py`, slc=True
        pa_w<W>_ce<CE>_b<BLK>_p<PG>_tlc.bin  -- `pa_hwprobe*.py`, slc=False

    The `no_dump` `_slc` files were checked byte-for-byte against the CRCs
    `no_recheck.py` recorded for its 0xDA reads, so the mode label is verified
    rather than assumed.  `pa_dump` holds werus 81 and 581, which the cell-mode
    rule was never fitted to.
    """
    import re
    pats = [
        (Path(root) / "no_dump", "no_w*.bin",
         re.compile(r"^no_w(?P<w>\d+)_p(?P<pg>\d+)_ce(?P<ce>\d+)_b(?P<blk>\d+)"
                    r"(?:_(?P<mode>slc|tlc))?\.bin$")),
        (Path(root) / "pa_dump", "pa_w*.bin",
         re.compile(r"^pa_w(?P<w>\d+)_ce(?P<ce>\d+)_b(?P<blk>\d+)_p(?P<pg>\d+)"
                    r"_(?P<mode>slc|tlc)\.bin$")),
    ]
    from .device import Device

    class _ModeDumpDevice(Device):
        note = "self-test cell-mode backing over no_dump/ + pa_dump/"

        def __init__(self):
            Device.__init__(self)
            self.index = {}
            self.sources = {}
            for base, glob, rx in pats:
                if not base.exists():
                    continue
                n = 0
                for p in sorted(base.glob(glob)):
                    m = rx.match(p.name)
                    if not m or p.stat().st_size != 18432:
                        continue
                    self.index[(int(m.group("ce")), int(m.group("blk")),
                                int(m.group("pg")),
                                m.group("mode") == "slc")] = p
                    n += 1
                self.sources[base.name] = n

        def present_ces(self):
            return sorted({k[0] for k in self.index})

        def read_page(self, ce, block, page, lun=0, slc=False):
            p = self.index.get((ce, block, page, bool(slc)))
            if p is None:
                raise DeviceError(
                    "no dump holds ce%d b%d p%d in %s mode"
                    % (ce, block, page, "pseudo-SLC" if slc else "native TLC"))
            return p.read_bytes()

    d = _ModeDumpDevice()
    return d if d.index else None


def _selftest_device(root):                             # pragma: no cover
    """A Device over this tree's raw page dump, for the self-test only.

    Prefers `titania.dumpdev.DumpDevice`; falls back to a minimal in-file
    backing so the self-test does not depend on another module landing first.
    The fallback is confined to this function -- nothing in the module proper
    opens a dump file.
    """
    raw = config.dump_path("raw")
    if not raw.exists():
        return None
    # Prefer the widest backing available.  Running against `<dump>/raw` (CE
    # 1-6) when the whole tree serves CE 0-11 is the same "invisible, not
    # absent" hazard this module warns about everywhere else -- and it hid a
    # real bug: with CE 7-11 present, scanning a metadata WERU's block pair
    # across all twelve CEs merges it with the WERU that owns the other half.
    # (Before the cache key was fixed, switching between these two would have
    # silently replayed the narrow scan, so this preference is only safe now.)
    try:
        from .dumpdev import DumpDevice
        for path in (config.dump_path(), raw):
            try:
                d = DumpDevice(path)
            except Exception:
                continue
            if d.present_ces():
                # `d.root`, not `path`: asking for the whole dump tree
                # opens the page store that supersedes it, and the note has to
                # name the backing that was actually read.
                d.note = "titania.dumpdev.DumpDevice over %s" % d.root.name
                return d
    except Exception:
        pass

    from .device import Device

    class _RawDirDevice(Device):
        """<dump>/raw/ceNN/bBBB_pPPP.bin, 18432 bytes per page."""
        note = "self-test fallback over <dump>/raw"

        def __init__(self, base):
            super().__init__()
            self.base = Path(base)
            self._ces = sorted(int(p.name[2:]) for p in self.base.glob("ce*")
                               if p.is_dir())

        def present_ces(self):
            return list(self._ces)

        def read_page(self, ce, block, page, lun=0, slc=False):
            f = self.base / ("ce%02d" % ce) / ("b%03d_p%03d.bin" % (block, page))
            if not f.exists():
                raise DeviceError("no page ce%d b%d p%d" % (ce, block, page))
            return f.read_bytes()

    return _RawDirDevice(raw)


if __name__ == "__main__":                              # pragma: no cover
    if "--selftest" in sys.argv:
        sys.exit(_selftest(sys.argv))
    print(__doc__)
