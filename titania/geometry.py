"""Addressing and geometry for the Titania NAND stack.

One place for every constant that relates a byte on the media to a name a human
uses.  Nothing here talks to hardware or parses content; it is pure arithmetic,
so it is safe to import from anywhere and trivial to test.

THE ADDRESS LADDER
------------------
    byte in AU  ->  AU index in page  ->  page  ->  block  ->  CE  ->  channel
                                       \\-> row (the wire address: lun|block|page)
    physicalBlockId  = 12*block + ce          (ce = pbid % 12, block = pbid // 12)
    WERU             = 4 block numbers x 12 CEs = 48 physical blocks

One rung of that ladder is physical rather than logical: a CE is one die, and
four dies share one epoxy package, so the twelve CEs are three packages.
(This paragraph said "two dies ... Six packages" until 2026-08-08; the
schematics show 4 CE# per package.)  Channels are a separate grouping: 12 x 8
DQ nets means one channel per CE, so `channel_of(ce) == ce` while
`package_of(ce) == ce // 4`.  See `DIES_PER_CE` / `DIES_PER_PACKAGE` /
`package_of` / `channel_of` -- it matters to anyone planning a chip-off,
because packages, not CEs, are what come off the board.

CELL MODE IS NOT A PROPERTY OF THE BLOCK
----------------------------------------
A "native TLC" block still has natively-SLC word lines at both ends, so the page
count and the cell mode are per-PAGE decisions (`cell_mode`).  Reading a native
block while the die is latched in pseudo-SLC returns plausible-looking garbage
rather than an error -- see `titania.device` for the mode handling that avoids
this.  It has produced false "nothing is written here" sweeps before.

REGIONS: NOT EVERY PAGE ON THIS DRIVE IS AN AU PAGE
---------------------------------------------------
`region(ce, block)` names which set of rules a page obeys.  It is pure
addressing -- block numbers the boot ROM and CPU1 firmware hard-code, not
anything read off the media -- and it is the first thing a decoder must ask,
because two of the three regions are not what `codec.FORMAT_A` describes:

    REGION_FW_GROUP2   block 0 on CE 0..5.  Its own ECC geometry entirely --
                       20 codewords x 512 payload + 300 parity, 10240 user
                       bytes per page.  Decoding it as FORMAT_A fails on every
                       page, which is exactly what made 1840 perfectly good
                       pages (368 on each of five dies) look like read failures.
    REGION_FW_GROUP01  blocks 6..9, any CE.  FORMAT_A bytes, but the firmware
                       area is addressed by raw CE/block/page through
                       `FwDataRw.c` and its aux carries no LMA array -- measured
                       here, 44 of 44 MCRC-clean pages carry four equal non-LMA
                       dwords.  So the "four consecutive LMAs" crib does not
                       apply and must not be used to reject one.
    REGION_FTL         everything else: FORMAT_A, and the aux means whatever
                       the owning MSO's `msoDescriptor +17` says it means.

Two independent routes agree on the boundaries: these block numbers, and the
FTL's own ownership (msoId 57 `NeoFwPartition` owns WERUs 3..6 = blocks 6..9).
"""

# --- die / package -------------------------------------------------------
#: Chip enables on the whole device -- not per package.  The device is twelve
#: dies in six dual-die packages; see `DIES_PER_PACKAGE` and `package_of`.
CE_COUNT = 12
CE_DEAD = (0,)                      # CE0 is dead on this board; see NAND_TOOLS.md

#: Dies behind one chip enable.  READ ID = `EC 5E 98 BF 84 CC` on all 11 live
#: CEs (`0xEC` = Samsung; the package is marked K9BKGD8J1A).  Byte 2 = 0x98 and
#: its bits[1:0] = 00 means one die per CE.  The same byte's bits[3:2] = 10 say
#: 8 levels per cell = TLC, which the media independently confirms -- that is
#: what validates using this decode table at all.  Byte 3 (0xBF) does not
#: decode on that table (it would claim an 8 KB page against a real 16384 +
#: 2048 = 18432 byte page), so this part uses an extended encoding for byte 3;
#: its meaning is UNRESOLVED and nothing here reads it.
DIES_PER_CE = 1

#: Dies inside one epoxy package.  Physical -- a NAND ID encodes *organisation*
#: (dies per CE, cell levels, plane parallelism) and never the package or the
#: ballout, so this cannot be derived from READ ID.
#:
#: CORRECTED 2026-08-08 from the schematics: 4, not 2.  This read `2` with
#: `PACKAGE_COUNT = 6`, inferred from the firmware's `ce // 2` grouping.  The
#: schematic shows 4 CE per package, so with one die per CE that is four
#: dies per package and three packages.  A schematic outranks an inference
#: drawn from a placement law.
#: (The schematic's "2 CEs per channel" wording was briefly taken as an
#: electrical claim and is not one -- see `CES_PER_CHANNEL`: 12 x 8 DQ nets
#: means one channel per CE.  It does not affect the package count.)
#: (Reconciles with an earlier "2 dies per epoxy" report if that described a
#: 2-die stack and each package holds two stacks -- offered as a reading, not
#: established.)
DIES_PER_PACKAGE = 4

#: 12 dies / 4 per package.
PACKAGE_COUNT = CE_COUNT * DIES_PER_CE // DIES_PER_PACKAGE      # 3

#: CORRECTED again 2026-08-08, and this one is decisive: the DQ net count off
#: the schematic is 12 x 8.  Twelve independent 8-bit buses means one
#: channel per CE -- `CES_PER_CHANNEL = 1`, `CHANNEL_COUNT = 12`.
#:
#: This briefly read `CES_PER_CHANNEL = 2` / `CHANNEL_COUNT = 6` on the
#: schematic's "2 CEs per channel" labelling.  Twelve DQ nets refute that as an
#: Electrical claim: two CEs cannot share a bus that does not exist.  Whatever
#: the schematic's "channel" names, it is not a shared DQ bus.
#:
#: Why it matters -- it settles the CE0 fault, and in the good direction.
#: If CE0 and CE1 had shared a bus and a receiver, CE1 reading perfectly would
#: have put the fault in CE0's own die and made a chip-off pointless.  They
#: share nothing.  So the measured result stands unambiguously: capture controls
#: (`EN_DQS_GATE`, `RPTR_DLY_CTRL`, `ChanTiming`) move ch1's byte stream and
#: leave ch0 bit-identical because ch0 has its own receiver and that receiver is
#: dead.  CE0's die is very likely intact -- DdmData reads 0 media errors, 0
#: reallocations, 1% life spent -- so a chip-off should recover it.
#: The contradiction previously recorded here and in NAND_RAW_IMAGE.md is
#: Resolved; do not carry it forward as open.
CES_PER_CHANNEL = 1
CHANNEL_COUNT = CE_COUNT // CES_PER_CHANNEL                     # 12

#: Not a die count, despite the name and despite `LUN_SHIFT` below.
#:
#: The value is `nand_cfg_info.lunsPerDevice` as `NfCfgInfo_SetupDefault` sets
#: it (`efc_cpu1.c:32940-32976`).  It is kept because the FTL's block
#: arithmetic is built on that factor and that arithmetic reproduces the media
#: byte for byte.  What it is not is a second addressable die:
#:
#:   * the ID says one die per CE (`DIES_PER_CE`), and capacity agrees --
#:     12 dies x 3800 factory-usable blocks x 1104 pages x 16 KiB = 825 GB, the
#:     retail figure, exactly.  Two dies per CE would be 1650 GB;
#:   * MioInit step 5 (`sub_40491F96`) clamps `lunsPerDevice` to 1, after which
#:     `NFIF+0x100/0x102/0x104` all become 0xFFFF and the LUN field of the row
#:     resolves to 0.  Every row this project has ever composed is LUN 0;
#:   * the other hardware "2" -- `1 << lunsPerChannel` in the firmware-image
#:     reader (`titania.fw`) -- issues one command covering two adjacent BLOCK
#:     numbers.  With plane = block LSB (`efc_cpu1.c:21135`) that is a two-PLANE
#:     operation, which is also what ID byte 2 bits[5:4] = 01 says: two
#:     simultaneously programmed pages.
#:
#: Inference (basis above): the factor of two counts planes, and it is
#: carried in the BLOCK field, never the LUN field.  That is why a base WERU
#: takes 2 block numbers per CE and a data WERU 4
#: (`ftl.WERU_CONFIGURATIONS`, read off the media).
#:
#: The residual, stated plainly because it is the reason to distrust the name:
#: the firmware's own derivations
#: (`blocksPerWeru = channels * cesPerChannel * planes * diesPerCe`, the `4/d`
#: data multiplier, the `2^dieBits` divisors) reproduce the measured WERU table
#: only if exactly one of {planes, diesPerCe} is 2.  With both = 2 every
#: blocks-per-WERU comes out 2x the media's, which reads 24 for a base WERU =
#: 12 CEs x 2.  So the firmware carries two names for one real factor of two,
#: and the ID says which name is the physical one.
#:
#: Do not "fix" this to 1: `LUN_SHIFT`/`compose_row` are exercised against
#: media, and the value is a live factor in `BLOCKS_PER_LUN * LUNS_PER_CE`.
LUNS_PER_CE = 2


def channel_of(ce):
    """Which channel carries this chip enable?  -> 0..`CHANNEL_COUNT`-1.

    Identity: 12 x 8 DQ nets means one channel per CE.  Kept as a function
    rather than inlined because "channel" has been wrong twice in this project
    and a named accessor is where the correction can live.

    History, because both errors are instructive:
    * It was `package_of`, computing `ce // 2` and calling it a PACKAGE, from
      the inference that the firmware's `ce // 2` grouping was a package
      boundary.
    * Then briefly `ce // 2` again as a channel, on the schematic's "2 CEs per
      channel" wording.  The DQ net count refutes that electrically -- two
      CEs cannot share a bus that does not exist.

    `ce // 2` is real and appears in two independent RootStorage placement laws:

        Bootstrap (blocks 2..5)   stripe = base + 6*page + (ce // 2)
                                  0 violations over 5273 page/stripe pairs,
                                  44 of 44 (block, CE) combinations
        Backup    (block 1)       offset = (ce // 2) % 3

    But it is neither a package (that is `ce // 4`) nor a channel (that is
    `ce`).  Best remaining reading, and it is an inference: with 4 dies per
    package it is the die stack -- two stacks of two per package, which
    reconciles the earlier "2 dies per epoxy package" report if that described
    the epoxy-moulded stack.  A stack shares moulding and bond wires, so it is a
    plausible failure domain to spread redundancy across.  Nothing measured
    confirms it; only decapping or CE-trace work would.
    """
    return ce // CES_PER_CHANNEL


def channel_ces(channel):
    """The chip enables sharing one channel.  -> tuple, `CES_PER_CHANNEL` long.

    One CE per channel, so this is always a 1-tuple.  It exists so callers do
    not hard-code that assumption -- it has been wrong twice already.
    """
    base = channel * CES_PER_CHANNEL
    return tuple(range(base, base + CES_PER_CHANNEL))


def package_of(ce):
    """Which package carries this chip enable?  -> 0..`PACKAGE_COUNT`-1.

    From the schematics: 4 CE# per package, so CEs group as (4k .. 4k+3).
    Packages and channels are independent groupings -- there are 12 channels
    (one per CE) and 3 packages (four CEs each), so a package spans four
    channels.  An earlier draft of this docstring said "a package is two whole
    channels"; that came from reading the schematic's "2 CEs per channel" as a
    shared DQ bus and is withdrawn.

    Consequence, and it is bigger than previously stated: a chip-off of CE0's
    package removes CE0, CE1, CE2 and CE3 -- four dies, a third of the drive.
    An earlier note said "also removes CE1" under the retracted 2-die model.
    CE1 is where group 2 is read from today (the real group-2 image is on CE0;
    CE 1..5 hold replicas), so the board loses that source either way.  No data
    is lost if the whole package is read externally, but nothing on it can be
    read in-circuit afterwards.

    An adapter therefore needs at least four CE# nets broken out.  A two-CE
    adapter reads half the part.  The ballout is unconfirmed -- read it off the
    part before fabricating anything.

    Resolved -- this docstring carried an open contradiction for one day and it
    is settled.  The worry was: if two CEs shared a channel they would share a
    DQ bus and a receiver, and CE1 reads perfectly, which would put CE0's fault
    in its own die and make a chip-off pointless.  The schematic's DQ net
    count is 12 x 8, so each CE has its own bus and they share nothing.  The
    measurement therefore stands unambiguously: `EN_DQS_GATE`, `RPTR_DLY_CTRL`
    and `ChanTiming` move ch1's byte stream and leave ch0 bit-identical because
    ch0 has its own receiver and that receiver is dead.  CE0's die is very
    likely intact (DdmData: 0 media errors, 0 reallocations, 1% life spent), so
    a chip-off should recover it.  See `CES_PER_CHANNEL`.

    Precedent worth keeping: "channel" has misled twice in this codebase --
    `R_CHAN_EN` in `nd_hw.py` names a timing register, not a channel enable,
    and `ce // 2` was called a package and then a channel before being neither.
    """
    return ce // DIES_PER_PACKAGE


def package_ces(package):
    """The chip enables sharing one package.  -> tuple, `DIES_PER_PACKAGE` long."""
    base = package * DIES_PER_PACKAGE
    return tuple(range(base, base + DIES_PER_PACKAGE))


# --- block / page --------------------------------------------------------
PAGES_PER_BLOCK_SLC = 368           # pseudo-SLC (prefix 0xDA)
PAGES_PER_BLOCK_TLC = 1104          # native TLC  (nand_cfg_info.val_450 = 0x450)

#: Blocks addressable per LUN -- the address-field ceiling, not the die's block
#: count.  It is exactly `1 << (LUN_SHIFT - BLOCK_SHIFT)` = 1 << 12, so all it
#: rests on is the width of the block field in the row address.  Nothing has
#: read a block count off the die.  The real count is smaller and is known from
#: two independent places: the media's own WERU table sums to 48000 physical
#: blocks over 12 CEs = 4000 per die (`ftl.WERU_CONFIGURATIONS`, itself
#: read off RootStorage object 83), of which 3800 are factory-usable; and the
#: highest block with programmed content in any capture this project holds is
#: 3719.  Use 4096 as a bound, never as a capacity, and take the block count
#: from the die's own parameter page if you have one.
BLOCKS_PER_LUN = 4096

#: Physical blocks per die, from that same WERU table. This is the number to
#: size a per-block table with; BLOCKS_PER_LUN would leave 96 columns per die
#: that no block can ever occupy.
BLOCKS_PER_DIE = 4000

#: Blocks on the whole device, and the natural row index for a per-block table
#: is `ce * BLOCKS_PER_DIE + block`.
BLOCKS_TOTAL = CE_COUNT * BLOCKS_PER_DIE

# --- row composition (the wire address) ---------------------------------
LUN_SHIFT = 23
BLOCK_SHIFT = 11

# --- page payload --------------------------------------------------------
AU_DATA = 4096                      # user bytes per AU
AU_AUX = 8                          # aux bytes per AU
AU_STORED = 4608                    # bytes an AU occupies on the media (format A)
AUS_PER_PAGE_TLC = 4

# --- WERU / MSO ----------------------------------------------------------
BLOCKS_PER_WERU = 48                # 4 block numbers x 12 CEs.  Not 12.
AUS_PER_TLC_WERU = 211968           # 48 * 1104 * 4, matches observed capacity
AUS_PER_SLC_WERU = 17664            # 12 * 368 * 4, the metadata WERU config
MAX_MSOS = 63                       # cMaxNumberOfMsos


# --- media regions -------------------------------------------------------
#: block 0 of the six dies the boot ROM boots from.  `nd_g2extract`'s geometry.
FW_GROUP2_BLOCK = 0
FW_GROUP2_CES = (0, 1, 2, 3, 4, 5)
#: fw group 0 is striped over blocks 6/7 and group 1 over 8/9, on every CE.
#: `titania.fw.GROUP_BLOCKS` is the same statement keyed by group, and
#: `titania.fw.selftest` cross-checks the two.
FW_GROUP01_BLOCKS = (6, 7, 8, 9)

REGION_FW_GROUP2 = "fw-group2"
REGION_FW_GROUP01 = "fw-group01"
REGION_FTL = "ftl"
REGIONS = (REGION_FW_GROUP2, REGION_FW_GROUP01, REGION_FTL)


def region(ce, block):
    """Which set of decode rules does this page obey?  -> one of `REGIONS`.

    Pure addressing: no content is examined and no descriptor is consulted, so
    this is safe to ask before a page has been read and cannot be fooled by a
    bad read.  See the module docstring for what each region means and for the
    measurements behind the boundaries.

    NOTE the CE test on group 2.  Block 0 on CE 6..11 is not group 2 -- it is
    ordinary FORMAT_A content (measured: MCRC-clean FORMAT_A pages owned by
    msoId 57 on CE6) -- and a region map that ignored the CE would corrupt them
    into the group-2 decoder.
    """
    if block == FW_GROUP2_BLOCK and ce in FW_GROUP2_CES:
        return REGION_FW_GROUP2
    if block in FW_GROUP01_BLOCKS:
        return REGION_FW_GROUP01
    return REGION_FTL


def compose_row(block, page, lun=0):
    """(block, page, lun) -> the row address the NAND sees."""
    return (lun << LUN_SHIFT) | (block << BLOCK_SHIFT) | page


def split_row(row):
    """Inverse of `compose_row`.  -> (block, page, lun)"""
    return ((row >> BLOCK_SHIFT) & ((1 << (LUN_SHIFT - BLOCK_SHIFT)) - 1),
            row & ((1 << BLOCK_SHIFT) - 1),
            row >> LUN_SHIFT)


def pbid(block, ce):
    """physicalBlockId, the FTL's flat block name."""
    return CE_COUNT * block + ce


def split_pbid(physical_block_id):
    """-> (block, ce)"""
    return divmod(physical_block_id, CE_COUNT)


def weru_blocks(block_numbers):
    """The 48 (block, ce) pairs of a WERU, given its 4 block numbers.

    A WERU spans every CE, including dead ones -- callers that skip CE0 must do
    so explicitly so the omission is visible rather than silently changing the
    capacity arithmetic.
    """
    return [(b, ce) for b in block_numbers for ce in range(CE_COUNT)]


def cell_mode(page):
    """Cell mode of a page inside a native (non-pseudo-SLC) block.

    From `sub_53A6` (`efc_cpu1.c:10031`).  Returns 0 for native TLC (3 pages per
    word line) and 2 for the natively-SLC word lines that bracket the block.
    """
    if page < 4 or page >= PAGES_PER_BLOCK_TLC - 4:
        return 2
    return 0


def pages_per_block(slc):
    return PAGES_PER_BLOCK_SLC if slc else PAGES_PER_BLOCK_TLC
