#!/usr/bin/env python3
"""titania._wma_ref -- a second, independent implementation of the WMA map.

Do not merge this into `titania.ftl`, and do not make it import from it.

This is a verbatim vendored copy of `nk_wma.py`, written by the hardware agent
against the board while `titania/ftl.py` was written separately against the
firmware decompilation.  `ftl._selftest` cross-checks the two over 4000 WMAs per
configured WERU.  That check is only worth anything while the two remain
separate derivations of the same arithmetic -- the moment one imports the other,
or one is "simplified" to match, it becomes a self-comparison that passes no
matter what and tells you nothing.

It was vendored (rather than left at the repo root) so the cross-check runs on
an installed package instead of skipping everywhere except this one directory --
a check that only runs in the author's working directory is not a check.  The
root `nk_wma.py` remains the upstream; if it changes, re-copy it here verbatim.
Divergence between this file and the root copy is itself a finding.

Vendored 2026-08-06 from nk_wma.py.  No edits below this header.

Original docstring follows.
---------------------------------------------------------------------------
nk_wma.py -- the WMA <-> physical address function.

WHAT A WMA IS
-------------
A WMA names one 4 KiB media allocation unit.  It decomposes as

    weruId   = wma // 211968                  (211968 = 48 * 1104 * 4)
    wmaOff   = wma  % 211968
    page     = wmaOff // 192                  (192 = 48 * 4, the FULL stride)
    slot     = (wmaOff % 192) // 4            valid only while < blocksPerWeru
    au       = wma   % 4

The 192 stride is `cMaxBlocksPerWeru * wmasPerPage` = 48 * 4 and is constant.
A WERU that owns fewer than 48 blocks leaves the tail of every 192-WMA window
unused -- which is the whole explanation of the unexplained "+152 recurring on a
cycle of 192" in the userLutLut: 192 - 11*4 = 148, and the last step inside the
window is +4, so the observed jump is 152.

FIRMWARE PROVENANCE
-------------------
`WeruConfiguration_GetWlaOffsetFromWmaOffset` (efc_cpu2.c, sub_4030E7F8):

    q = wmaOff / D;  r = wmaOff % D
    if (blocksPerWeru * wmasPerPage) > r:
        return r + q * (blocksPerWeru * wmasPerPage)
    -> "invalid input wma"

with D = 192, wmasPerPage = 4.  That is exactly the map above, and it also says
the wla (dense) address is `page * blocksPerWeru * 4 + slot * 4 + au`.
`WeruConfiguration_GetWeruIdFromWma` is `wma / 211968` (efc_cpu2.c:23888).

`sub_51E0` picks one of six weru configurations by weruId range
(`byte_900078[20 * cfgIdx]`, +10 u16 firstWeruId, +4 u16 weruCount,
+2 u16 blocksPerWeru, +14 u8 cellMode).  blocksPerWeru is per configuration --
it is not 48 for every WERU, and assuming so is what made the arithmetic fail
before.

SLOT -> (CE, BLOCK)
-------------------
Measured on the board for the two user-LUT WERUs (nk_lutdump.py + nk_wmafit.py):

    slot = 2 * (ce - ceBase) + (block - blockBase)

i.e. the WERU's blocks are a BLOCK pair (blockBase, blockBase+1) on each of six
consecutive CEs, laid out CE-major.  `ceBase` is 6 for WERUs 14 and 20 and 0 for
WERUs 19 and 23 -- the CE split.

WHAT IS MEASURED, AND HOW HARD
------------------------------
  * WERU 14: all 11 slots recovered independently from the dump, all 11 agree
    with the closed form, and all 1212 live userLutLut entries resolve to a page
    holding exactly the LMA group the entry indexes.  Zero counterexamples.
  * WERUs 19 and 23, from the SystemLut and the SystemMetadata scans: of the
    3888 pairs where the rule's PAGE matches the scanned placement, the
    (ce, block, au) it predicts is right 3888 times and wrong 0 times.  The
    remaining pairs are LMAs whose scanned copy is newer than the snapshot the
    table was written with -- a staleness difference, not a mapping error.
  * The 9 WMAs the userLutLut's anomalous +8/+12 deltas skip inside WERU 14 all
    land on pages that exist and were read.  Two carry a sentinel aux LMA
    (0xFFFFFFFF, 0xFFFFFFFB) -- written with no LMA at all; the other seven
    carry a newer copy of a group whose snapshot entry still names the older.

THE TWELFTH BLOCK
-----------------
blocksPerWeru is 11 but twelve blocks are programmed in each region.  In WERU 14
the odd one out is (ce11, block 17): 111 programmed pages, no WMA addresses it,
and every LMA group on it also sits at its WMA-addressed home (57 older copies,
54 newer).  So nothing is lost by the WMA space not covering it -- but what it
is (mirror, journal, RAID parity) is not established here and must not be
guessed at.
"""
AUS_PER_WERU = 211968
WMA_STRIDE = 192            # cMaxBlocksPerWeru * wmasPerPage = 48 * 4
WMAS_PER_PAGE = 4
MAX_BLOCKS_PER_WERU = 48

INVALID = (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)


class WeruConfig(object):
    """One row of the firmware's six-entry weru configuration table."""

    def __init__(self, blocks_per_weru, ce_base, block_base, slc=True,
                 name=None):
        self.blocks_per_weru = blocks_per_weru
        self.ce_base = ce_base
        self.block_base = block_base
        self.slc = slc
        self.name = name

    def slot_to_physical(self, slot):
        """slot (0..blocksPerWeru-1) -> (ce, block)"""
        if not 0 <= slot < self.blocks_per_weru:
            raise ValueError("slot %d outside blocksPerWeru=%d"
                             % (slot, self.blocks_per_weru))
        ce, half = divmod(slot, 2)
        return self.ce_base + ce, self.block_base + half

    def physical_to_slot(self, ce, block):
        return 2 * (ce - self.ce_base) + (block - self.block_base)


def split_wma(wma):
    """-> (weruId, page, slot, au).  Pure arithmetic, no configuration needed."""
    weru, rem = divmod(wma, AUS_PER_WERU)
    page, r = divmod(rem, WMA_STRIDE)
    return weru, page, r // WMAS_PER_PAGE, wma % WMAS_PER_PAGE


def wma_is_addressable(wma, blocks_per_weru):
    """A WMA in the unused tail of a 192-window addresses nothing.

    The firmware's own check: the reduction prints "invalid input wma" and
    returns -1.  Reporting that as an address would be an invention.
    """
    return (wma % WMA_STRIDE) // WMAS_PER_PAGE < blocks_per_weru


def wma_to_physical(wma, config):
    """-> (ce, block, page, au), or None when the WMA addresses nothing."""
    if wma in INVALID:
        return None
    weru, page, slot, au = split_wma(wma)
    if slot >= config.blocks_per_weru:
        return None
    ce, block = config.slot_to_physical(slot)
    return ce, block, page, au


def physical_to_wma(weru, ce, block, page, au, config):
    """The inverse.  -> wma"""
    slot = config.physical_to_slot(ce, block)
    if not 0 <= slot < config.blocks_per_weru:
        raise ValueError("(ce%d, block %d) is not in this WERU" % (ce, block))
    return weru * AUS_PER_WERU + page * WMA_STRIDE + slot * WMAS_PER_PAGE + au


#: measured on this board.  ceBase/blockBase come from the hardware read;
#: blocksPerWeru comes from the observed 44-wide valid window and agrees with
#: the firmware's own bound.
CONFIGS = {
    14: WeruConfig(11, 6, 16, slc=True, name="user LUT, current head"),
    22: WeruConfig(11, 6, 20, slc=True, name="user LUT, two generations back"),
    19: WeruConfig(11, 0, 10, slc=True, name="SystemMetadata, generation -1"),
    23: WeruConfig(11, 0, 26, slc=True, name="SystemMetadata, current"),
    # WERU 20 is the ce-split twin of WERU 19: the same block pair 10/11, the
    # other six channels.  Measured on page 0, where the legacy userLutLut's
    # entries 691..701 are consecutive: ce6/b10 -> 691, ce6/b11 -> 692,
    # ce7/b10 -> 693 ... ce11/b10 -> 701.  Eleven slots, eleven exact hits, and
    # the twelfth block (ce11/b11) again carries a neighbouring entry that no
    # WMA addresses.
    20: WeruConfig(11, 6, 10, slc=True, name="user LUT, previous generation"),
}

#: The CE split, as measured.  A WERU is a BLOCK pair on six channels, and the
#: same pair can be two different WERUs on the two halves of the package:
#:      weru 19 = blocks 10/11 on CE 0..5      weru 20 = blocks 10/11 on CE 6..11
#:      weru 23 = blocks 26/27 on CE 0..5      weru 14 = blocks 16/17 on CE 6..11
#:                                             weru 22 = blocks 20/21 on CE 6..11
#: This is why `weru << n | offset` never landed inside a 17664-AU WERU: the
#: WERU is not 48 blocks here, it is 11, and the 192-WMA window is padding.
