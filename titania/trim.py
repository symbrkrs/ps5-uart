"""The Trim layer: which LMAs the host discarded, and why the LUT cannot say.

    from titania import DumpDevice
    from titania.ftl import Ftl
    from titania import trim

    dev = DumpDevice()
    ftl = Ftl.from_device(dev, firmware="dumps/fw.sram.efc.bin")
    log = trim.TrimLog.scan(dev, ftl)          # finds the Trim MSO's WERUs
    print(log.summary())
    log.is_trimmed(mso=34, lma=198720)         # -> TRIMMED / LIVE / UNKNOWN

THE HEADLINE, BECAUSE IT REFRAMES EVERY COVERAGE NUMBER
-------------------------------------------------------
A trimmed LMA's LUT entry is set to 0xFFFFFFFF -- byte-identical to an LMA
that was never written.  `Trim` (type 0) reaches `sub_8A8E`
(`efc_cpu2.c:23598-23628`), which calls `sub_8082(lutId, lma>>shift,
count>>shift)` (`:23627`); `sub_8082` (`:23036-23045`) is a `memset(.., 0xFF,
4*count)` through `sub_9B8` (`:13085`).  There is no distinct "trimmed"
sentinel.  Measured here: of 10672 LUT entries the log says were trimmed, 10668
read 0xFFFFFFFF -- and so does every one of a 4000-entry control sample the log
does not name (9200/9200 in a wider scratch sweep).  The sentinel has zero
discriminative power.

So "866 MB of mapped user data" cannot be split into live-versus-discarded by
looking at the LUT.  Everything the LUT still maps is, by construction, not
trimmed; everything it does not map is trimmed *or* never written, and only the
trim log distinguishes them -- for as long as the log entry survives.

RETRACTED, from this module's own brief
---------------------------------------
* "The LUT sentinels are 0xFFFFFFFF/fd/fc/fe ... one of those sentinels may
  *be* trimmed."  0xFFFFFFFE and 0xFFFFFFFC are not LUT/WMA values at all --
  they are *LMA*-space markers that appear only in the aux of a trim-log page
  (below).  The LUT-space sentinels that exist are:

      0xFFFFFFFF  -1  unmapped.  What Trim leaves behind, and also the initial
                      state and what DeleteReservation restores (`:110960`).
      0xFFFFFFFD  -3  reserved.  Written by `sub_4033FCC8` a4!=1 (`:110971`);
                      turned back into -1 by the a4==1 branch (`:110960`).
      0xFFFFFFFC  -4  written by the Rebuild "incomplete SLC track" path
                      (`efc_cpu2.c:134439-134440` -> `Lut_Update`, `sub_8256`).
      0xFFFFFFFE  -2  no writer found.  Every LUT-entry writer in cpu1/2/3 was
                      enumerated -- `sub_8082` (0xFF fill), `sub_4033FCC8`
                      (-3/-1), `sub_81F6` (-1, `:23119`) and `Lut_Update`
                      `sub_8256` (`:23294`, arbitrary WMA from the caller) --
                      and none of them produces -2.  The rebuild rule's
                      inclusion of -2 (`efc_cpu2.c:109645` skips -1/-3/-4 and
                      therefore counts -2) appears to be vacuous on this
                      firmware.  Stated as a negative search result, not proof.

* "It has no LUT at all ... whatever addresses it is something else" -- correct,
  and the answer is that nothing addresses it.  See below.

WHERE THE TRIM STATE LIVES
--------------------------
Not in RootStorage.  The complete set of `SegmentedBuffer_Initialize`
(`sub_403688BC`, `efc_cpu2.c:152741`) call sites is five, reached through
exactly two production wrappers -- `sub_40368164` for Bootstrap
(`efc_cpu2.c:152093`) and `sub_40367C30` for Backup (`:151756`):

    WeruUsageLog  Bootstrap  firstObjectId 4     efc_cpu2.c:31748
    LptNsid2      Bootstrap  firstObjectId 0x2C  efc_cpu2.c:115642
    LptNsid3      Bootstrap  firstObjectId 0x4A  efc_cpu2.c:115674
    PhBP          Backup     firstObjectId 8     efc_cpu2.c:107037
    Test          (test code, never registered)  efc_cpu2.c:97512

None is a trim structure, and the CRC-8 name check confirms it on media: this
board's Bootstrap catalogue carries only tags {0, 1, 137, 171, 240} and the
Backup one only {0, 244}, and `object_name_tag("PhBP") == 244` accounts for the
one run that was previously unexplained (Backup objects 8..20).  That is a
fourth confirmation of the name-tag rule after WeruUsageLog/LptNsid2/
LptNsid3.  Trying "Trim"-ish names against the tags is not evidence either way
-- an 8-bit hash has 227 plausible-name collisions on 244 alone -- so the
registration enumeration above, not the hash, is what closes this.

The trim state is a log of whole pages written into the Trim MSO's own WERUs,
identified by a sentinel in their aux, and found by scanning.  There is no
index, no LUT and no root object, which is exactly why `lutId == 0xFF`:

  * `TrimLog_WriteEntry` (`sub_1248A`, `efc_cpu2.c:33486-33574`) writes one
    entry as one page.  `GetSizeOfEntryInBytes` (`sub_12704`, `:33647-33652`) is
    `auFormat[msoDescriptor[trimMso].auSel].field+8 * ausPerPage` = 4096 * 4 =
    16384, and the write asserts `ausPerPage == targetLmaList.count`
    (`:33523-33529`).
  * The four target LMAs are set at `:33515-33522` to
    ``[sentinel, -1, -1, -1]`` where

        sentinel = 0xFFFFFFFC  if msoGroupId(entry.msoId) == 5 (ParityData)
                   0xFFFFFFFE  otherwise

    (`unk_900830 + 2*msoId + 1` is the msoGroupId, built from the msoIdTable at
    `efc_cpu3.c:6920-6928`; `+0` is the msoType.)
  * Those LMAs go into the aux verbatim -- `WriteAuxiliaryList` dispatches on
    them at `efc_cpu2.c:43112-43124`, where `msoType == 6` (Trim) selects the
    sequence-number pool of `byte_900001` (UserData/NSID1) for -2 and
    `byte_900005` (UserData/ParityData) for -4, and panics on anything else.
  * At mount the Rebuild scan is what finds them: `sub_40356F8A`
    (`efc_cpu2.c:134467-134536`) branches on `lma == -2 || lma == -4`, re-checks
    the sentinel against `off_8013EC+4` (panic "Rebuild.c", 1329), and calls the
    replay `sub_4035B7B4` (`:134533`) if the entry's own msoId shares the lutId
    being rebuilt (`:134531`).

MEASURED on this board, and it matches the firmware exactly
-----------------------------------------------------------
The Trim MSO is 42 (`msoIdTable[6*6 + 0]`, i.e. `byte_900024`, passed to
`sub_402CF722` at `efc_cpu3.c:36004` and `:37808`, which is the only writer of
`byte_907AC4`, the trim log's msoId).  It owns exactly one WERU, 21 -- agreed
independently by `weruDescriptor +0` and by `WeruUsageLog +16`.  WERU 21 is
configuration 2: blocks 12/13 on CE 0-5, pseudo-SLC.

Reading all 3680 of its pages (CE 1-5; CE0 is dead) gives 868 programmed pages,
every one of them 4/4 MCRC and every one of them a trim-log page:

    aux LMA[0] = 0xFFFFFFFE  814 pages     aux LMA[1..3] = 0xFFFFFFFF  on all
    aux LMA[0] = 0xFFFFFFFC   54 pages

and the sentinel/group rule holds 868/868 with no exceptions:

    0xFFFFFFFC <-> msoId 34, 35   (UserData, UserData2ndWF / ParityData, group 5)
    0xFFFFFFFE <-> msoId 45       (UserDataNandCache / NSID1,            group 1)

The ENTRIES are mirrored, not striped.  Each entry appears twice: block 12
and block 13 of the *same* CE, byte-identical including the page sequence
number.  434 (ce, page) positions hold both blocks, 434/434 identical, 0
lone.  So 868 pages are 434 entries.  Do not count them twice, and do not read
the second copy as inline parity: the aux parity flag (bit 31 of the second aux
dword, `sub_1C3CC`, `efc_cpu2.c:42936`) is set on 100% of AUs of all ten
readable slots of WERU 21, against the control WERU 23 where it is 100% on
slot 11 and 1.0-2.1% elsewhere.  On this WERU the flag does not identify a
parity slot, and with two of twelve members on the dead CE0 the XOR relation
cannot be tested -- so whether WERU 21 also carries inline parity is UNKNOWN
and this module does not claim either way.  What is established is that the two
copies of an entry are equal, which is all the decoder needs.

THE RECORD FORMAT
-----------------
`TrimParameters_t` is 12 bytes and `LmaRange_t` is 6 -- forced by the firmware's
own size assertion, `sizeof(TrimParameters_t) + sizeof(LmaRange_t)*n <=
GetSizeOfEntryInBytes()` compiled as `entrySize < 6*n + 12`
(`efc_cpu2.c:33537-33543`).  Field offsets are read off the accesses in
`sub_1248A` (`:33510`, `:33545`, `:33552`) and confirmed independently by the
replay `sub_4035B7B4` (`:139178-139180`, `:139191`) and by
`sub_40356F8A` (`:134531`, which reads the msoId at `+8`):

    +0   u8  type            0 Trim, 1 Reserve, 2 DeleteReservation, 4 Invalid
    +1   u8  } zero on 868/868 records here
    +2   u16 }
    +4   u32 reservation sequence  (0 for type 0; see below)
    +8   u8  msoId           the target MSO, not the Trim MSO
    +9   u8  zero on 868/868
    +10  u16 numberOfLmaRanges   < 256 (cTrimCommandMaxLmaRangeCount)
    +12  LmaRange_t[n] { u32 first; u16 count }

Type names are the firmware's: `cTrimTypeDeleteReservation == 2`
(`efc_cpu2.c:33176-33182`), `cTrimTypeInvalid == 4` (the reset value at
`:33216` and `:139146`).  The 256 bound is the panic at `:33085-33091` plus the
1536-byte = 256*6 range buffer at `:33084` and `:139142`.

The range units depend on the type.  This is not a detail.

    type 0 (Trim)               (first, count) are LMAs.  `sub_8A8E`
                                (`:23598`) asserts `lma % lutGranularity == 0`
                                and `count % lutGranularity == 0` ("Lut.c",
                                1190/1191) and then divides by the granularity.
    type 1 (Reserve)            (first, count) are LUT entry indices.
    type 2 (DeleteReservation)  `sub_8B84`/`sub_8BAE` (`:23659`, `:23666`) pass
                                them straight to `sub_8980` (`:23533`) and
                                `sub_4033EDC2` (`:109991`), both of which index
                                `lutTable + 4*first` and loop `count` times with
                                no shift and no alignment check.

Measured, and it is a clean separation: 27 of 27 type-0 ranges are
granularity-aligned; 0 of 414 type-1/2 ranges are.  Reading a type-1 range as
an LMA range would violate the firmware's own assertion 414 times.

`+4` is not a sequence number -- it is `sub_A67C()` (`efc_cpu2.c:25391`), a
read of the Lpt's reserved-space accumulator at `lpt + 393224`.  `sub_4034AF88`
(`:122322-122331`) adds a signed delta to it and refuses to let it go negative,
and `sub_4034AFAA` (`:122335`) divides it by pages*blocks to get a WERU count,
so it counts reserved LUT entries, not events.  It is taken only when the type
is non-zero (`v2 = type ? sub_A67C() : type`, `:33304-33307`) and restored by
`sub_A688` at replay (`:139219` -> `:25398`).  On media: type 0 -> 0 on 54/54;
types 1/2 -> 10259442..10259501 on 814/814, and it decreases 200 times in
write order, which is what settles that it is an accumulator.  `sub_A688` also
writes it into the NSID1 group LUT's descriptor at `+232`
(`*((_DWORD *)sub_FE0(byte_900001) + 58) = a1`, `:25401`) -- see below, that is
what names the previously-unnamed `+232` field.

Write order is `wmaOffset`, not `page_sequence`
-----------------------------------------------
A trim-log page's aux sequence comes from the rebuildSequenceNumber pool of
the entry's target group, not from one global counter: `WriteAuxiliaryList`
picks `byte_900001` (UserData/NSID1) for sentinel -2 and `byte_900005`
(UserData/ParityData) for -4 (`efc_cpu2.c:43112-43124`).  The two pools are
independent, so ordering the whole log by `page_sequence` interleaves them
wrongly.  Measured here: 23 inversions when the two are mixed, and ZERO
inversions inside each pool (sentinel -4: 370346..377370 over 50 pages;
sentinel -2: 3304390..3862254 over 732 pages), each strictly rising with
`wmaOffset`.  This module therefore sorts by `wma_offset` and treats it as the
write order.

THE RETIREMENT WATERMARK -- IT IS ON MEDIA, AND IT IS EXACT
------------------------------------------------------------
`MediaPartitionInterface_InvalidateTrimLogEntries_RpcCall` (`efc_cpu2.c:60817`,
server `:60804` -> `sub_12614` `:33583`) only posts to the trim-engine FSM; the
work is `sub_4035B94E` (`:139255`), and what it retires from is not a private
runtime pointer -- it is the LUT `snapshotInformation` list, which is written
to media inside every 296-byte LUT descriptor.

`sub_4035B94E` walks all eight lutIds (`sub_4033F07C(i) = &_lut + 296*i`,
`:110157`), takes every list element whose msoId is the Trim MSO's, and keeps
the lexicographic minimum of `(u64 activation, u32 wmaOffset)` (`:139311`).
Then, for the Trim MSO's WERU chain, it fully invalidates every WERU ahead of
that one and clears the validMap of every AU of the watermark WERU below
`base + wmaOffset` (`:139322-139351`).  If any list element for the Trim MSO
still carries the `0x3FFF` "no WERU" sentinel the whole search aborts
(`:139303-139309`) and nothing is retired at all.

The STRIDE is 24 and the element BASE is `+16`.  IDA folds the `+16` array
base into the element accesses, which makes the record look as though it spans
`+16..+39` of a 24-byte stride -- it does not.  Verified from the writing code
at the instruction level (`dumps/fw.sram.efc.bin`, VA base 0x4025CD34):

    sub_4033F01E  the appender      efc_cpu2.c:110127
        add.w r2, r0, r0, lsl #2 / add.w r2, r2, r0, lsl #5
        add.w r4, r1, r2, lsl #3        ; lut = base + 296*lutId
        ldr r5, [r4, #8] / cmp r5, #9 / blo               ; count at +8, bound 9
        add.w r0, r5, r5, lsl #1 / add.w r0, r4, r0, lsl #3   ; +24*i   <-- STRIDE
        strb r6, [r0, #0x10]            ; element +0  = msoId
        strh r5, [r0, #0x12]            ; element +2  = 0x3FFF
        adds r0, #0x18 / stm.w r0, {r1,r2,r3}   ; element +8/+12/+16 = 0

    sub_4033F970  the field writer  efc_cpu2.c:110733
        add.w r5, r6, r0, lsl #3        ; lut + 24*i
        ldrb.w sb, [r5, #0x10]          ; element +0  msoId
        blx  sub_40300D5C               ; -> that MSO's write-front WMA
        udiv/uxth  strh r0, [r5, #0x12] ; element +2  = wma / wmasPerWeru
        mls        str  r1, [r5, #0x20] ; element +16 = wma % wmasPerWeru
        blx  sub_40300F38 / strd r0, r1, [r5, #0x18]   ; element +8 = u64

    sub_4035B94E  the reader        efc_cpu2.c:139255
        movs r2, #0x18 / blx memcpy     ; the accumulator is 24 bytes
        add.w r4, r7, r2, lsl #3        ; lut + 24*i, fields at +0x10/+0x12/+0x18

and independently by the accumulator's initialiser `dword_1D3F8`, 24 bytes at
cpu2 ATCM 0x1D3F8:

    00 00 ff 3f 00 00 00 00 | ff ff ff ff ff ff ff ff | 00 00 00 00 00 00 00 00
      ^ weruId 0x3FFF          ^ activation = UINT64_MAX   ^ offset 0

i.e. the sentinel is built for a minimum search, which also proves `+8` is a
64-bit key.  `16 + 9*24 = 232`, and `+232` is a separate u32 -- the list
occupies `+16..+231` exactly.  Element layout:

    +0   u8  msoId
    +2   u16 weruId, 0x3FFF = none
    +8   u64 that WERU's WeruUsageLog activation stamp
    +16  u32 WMA offset inside that WERU

WHAT THAT SAYS ABOUT THIS BOARD -- THE LOG IS TRUNCATED, AND BY HOW MUCH
-------------------------------------------------------------------------
Both populated LUT descriptors on media carry the same Trim record:
msoId 42, weruId 21, activation 2642, wmaOffset 15775 (`retirement_
watermark()`).  WERU 21 is the Trim MSO's only WERU, so nothing precedes it in
the chain and retirement happens entirely inside it.

    wmaOffset 15775 = weru 21, page 82, slot 7, au 3 = ce3 / block 13 / page 82

Three independent on-media facts agree with it, none of them used to derive it:

  1. `validCount[21] == 25`, and exactly 25 trim-log entries have
     `wmaOffset > 15775` -- 21 readable plus the 4 that sit on the dead CE0
     (page rows 83..86).  That is the retirement loop's own arithmetic.
  2. The last entry at or below the watermark ends at exactly 15775 and its
     reservation accumulator `+4` reads 10259475, which is byte-for-byte
     the `+232` field of the same LUT descriptor.
  3. The first entry above the watermark (`wmaOffset 15776`) is the type-0 Trim
     whose LUT effects this module already found un-applied on media -- its
     first four LUT entries still hold live WMAs.  That is what "written after
     the snapshot, not yet folded in" has to look like.

So, quantitatively, on this drive:

    521 entries were written into WERU 21   (434 readable + 87 on the dead CE0)
    496 of them are retired   -- below the watermark; the firmware considers
                                 them gone and will erase them with the WERU
     25 are still live        -- the replay set, == validCount[21]
    the written region is wmaOffset 0..16551 of 70656 = page rows 0..86 of
    368, contiguous  (the readable copies' AU0s span 8..16544; offsets 0..7 of
    every row are the CE0 pair)

Nothing has been erased off the front of WERU 21: the surviving run starts
at `wmaOffset 0` (page row 0; its two lowest slots are CE0 and unreadable, and
the lowest readable offset is 8) and has no gap other than the dead CE0.  A
WERU erases whole, and WERU 21 still holds 25 valid entries, so it cannot be
recycled yet -- the 496 retired entries survive only by luck of timing.

But the log is not a history.  Everything the Trim MSO wrote before WERU
21's activation (global activation stamp 2642 of the drive's >= 2819) went
with the predecessor WERU, which has been erased and re-owned.  `WeruUsageLog
[21].origin` is 0 and its flag byte is clear, so no lineage record points at
that predecessor and its size cannot be recovered from this image.

RETRACTED, from this module's own earlier text: "the reservation ledger proves
the truncation directly -- 27 of 70 LUT entries have a negative balance".  The
deficit is real (27 indices, total 44, worst -15) but it is not proof: 87
entries of this WERU sit on CE0 blocks 12/13 and CE0 is dead on this board, so
a hole big enough to explain it is inside the WERU we can otherwise account
for.  The truncation is established by the watermark and by WERU 21's
activation stamp, not by the ledger.

WHAT IS ON THIS BOARD
---------------------
    434 entries   27 Trim, 185 Reserve, 222 DeleteReservation

    TRIM  msoId 34  UserData/ParityData        23 ranges  36800 LMAs  143.8 MiB
    TRIM  msoId 35  UserData2ndWF/ParityData    4 ranges   5888 LMAs   23.0 MiB
    RESERVE/delete  msoId 45 UserDataNandCache/NSID1 only

Zero LMAs of any host-visible namespace MSO are recorded as trimmed.  Every
surviving Trim entry names a ParityData write front, and 21 of the 27 have
`count == 1472` = 368 pages * 4 AUs = one pseudo-SLC block of parity (2 more are
4416 = 3*1472).  So on this drive the trim log is being used to release parity
blocks, not to record host deallocations -- and the forensic split of the user
data is not changed by it.  This independently confirms the two readings in
NAND_STORAGE_MAP.md that were flagged as leads: msoId 0x22/0x23 (34/35) are the
ParityData MSOs, and 0x2d is 45 = UserDataNandCache/NSID1.

End-to-end check against the LUT: of the 10672 group-5 LUT entries the 27 Trim
ranges cover, 10668 read 0xFFFFFFFF.  The 4 exceptions are entries 82800..82803
-- the first four entries of the newest Trim entry (lma 331200, page sequence
377370) -- which still hold live WMAs, i.e. that trim had not been fully applied
when the drive stopped.  That is what a *pending* journal looks like.
"""

import collections
import struct
import sys
import typing                     # only for the `_Unknown.__iter__` type comment
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


# `py titania/trim.py` and `py -m titania.trim` must both work.  Run by path
# there is no parent package, so a relative import raises "attempted relative
# import with no known parent package" before anything in this file executes;
# putting the repository root on sys.path and importing by absolute name is the
# same idiom `extract.py` and `raid.py` already use.
if __package__ in (None, ""):                       # `py titania/trim.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
else:
    from . import geometry as geom

#: This package's name whether imported as `titania.trim` or run as a script
#: (in which case the header above put the parent on sys.path).  Every deferred
#: sibling import below goes through it, so neither entry point needs a
#: relative import the other cannot satisfy.
_PKG = __package__ or "titania"


def _sib(name):
    """A sibling layer, reached the same way from either entry point."""
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))


# ===========================================================================
# constants -- every one carries where it came from
# ===========================================================================
#: msoType of the Trim MSO.  `unk_900830 + 2*msoId + 0` is the msoType and `+1`
#: the msoGroupId, both built from the msoIdTable (`efc_cpu3.c:6920-6928`);
#: `WriteAuxiliaryList` selects the trim-log aux path on `msoType == 6`
#: (`efc_cpu2.c:43112`).
TRIM_MSO_TYPE = 6
SYSTEM_GROUP_ID = 0
PARITY_GROUP_ID = 5

#: The aux LMA of AU0 of a trim-log page.  Not LUT/WMA values -- see the module
#: docstring.  `efc_cpu2.c:33515-33522`, dispatched at `:43112-43124`, replayed
#: at `:134488`.
SENTINEL_DATA = 0xFFFFFFFE          # msoGroupId != 5
SENTINEL_PARITY = 0xFFFFFFFC        # msoGroupId == 5
SENTINELS = (SENTINEL_DATA, SENTINEL_PARITY)
#: AUs 1..3 of a trim-log page are padded with -1 (`sub_1C238`, `:42857-42859`).
PAD_LMA = 0xFFFFFFFF

#: Trim types.  `cTrimTypeDeleteReservation == 2` (`efc_cpu2.c:33176-33182`);
#: `cTrimTypeInvalid == 4` is the reset value (`:33216`, `:139146`).  The replay
#: `sub_4035B7B4` (`:139181-139217`) accepts 0/1/2 and HALTs on anything else.
TRIM = 0
RESERVE = 1
DELETE_RESERVATION = 2
INVALID = 4
TYPE_NAMES = {TRIM: "Trim", RESERVE: "Reserve",
              DELETE_RESERVATION: "DeleteReservation", INVALID: "Invalid"}

#: `cTrimCommandMaxLmaRangeCount`.  Panic at `efc_cpu2.c:33085-33091`, and the
#: 0x600-byte = 256*6 range buffer at `:33084` / `:139142`.
MAX_LMA_RANGES = 256

HEADER_BYTES = 12                   # sizeof(TrimParameters_t), `:33537`
RANGE_BYTES = 6                     # sizeof(LmaRange_t),       `:33545-33549`

#: `GetSizeOfEntryInBytes` (`sub_12704`, `efc_cpu2.c:33647-33652`): the AU
#: format's field+8 (4096 for format 24) times ausPerPage (4).  One whole page.
ENTRY_BYTES = geom.AU_DATA * geom.AUS_PER_PAGE_TLC

#: LUT-entry sentinels, in WMA space.  See the module docstring for the writer
#: of each and for why -2 has none.
LUT_UNMAPPED = 0xFFFFFFFF           # -1  what Trim leaves behind
LUT_RESERVED = 0xFFFFFFFD           # -3  Reserve;  `efc_cpu2.c:110971`
LUT_LOST = 0xFFFFFFFC               # -4  incomplete SLC track; `:134439`
LUT_UNEXPLAINED = 0xFFFFFFFE        # -2  no writer found in cpu1/2/3
#: `Lut_IsLutWmaInvalid` -- the set the firmware itself treats as "no data"
#: (`efc_cpu2.c:33634`, and the same triple at `:23080`, `:109645`, `:109774`).
LUT_INVALID = (LUT_UNMAPPED, LUT_RESERVED, LUT_LOST)

#: LUT granularity in LMAs, by msoGroupId.  Measured three ways for group 1 and
#: recorded in `nand-ftl-mount-path`: groups 1/2/3 are 16 LMAs, groups 0 and 5
#: are 4.  Used only to convert type-1/2 entry indices to LMAs, and never
#: guessed for a group not listed.
GROUP_GRANULARITY = {0: 4, 1: 16, 2: 16, 3: 16, 5: 4}

#: `is_trimmed` verdicts.  Three-way on purpose.
TRIMMED = "trimmed"
LIVE = "live"

# --- the LUT snapshotInformation list: where the watermark lives ------------
#: All four offsets are verified at the instruction level in the module
#: docstring, from the two writers and the reader, plus the 24-byte `.rodata`
#: initialiser `dword_1D3F8`.  Do not re-derive them from the decompiler: it
#: folds `SNAP_LIST_OFF` into the element accesses.
LUT_DESC_BYTES = 296                # `mem_clr(desc, 0x128)`, efc_cpu2.c:109537
SNAP_COUNT_OFF = 8                  # u32 count            `sub_4033F01E` :110138
SNAP_LIST_OFF = 16                  # the array base       `:110142`
SNAP_ENTRY = 24                     # STRIDE               `add.w r0, r4, r0, lsl #3`
SNAP_MAX = 9                        # `cmp r5, #9 / blo`   Lut.c:1500
SNAP_MSO_OFF = 0                    # u8                   `strb r6, [r0, #0x10]`
SNAP_WERU_OFF = 2                   # u16, 0x3FFF = none   `strh r5, [r0, #0x12]`
SNAP_ACTIVATION_OFF = 8             # u64                  `strd r0,r1,[r5,#0x18]`
SNAP_WMA_OFF = 16                   # u32                  `str r1, [r5, #0x20]`
#: `+232` -- not part of the list (`16 + 9*24 == 232`).  `sub_A688` writes the
#: Lpt reserved-space accumulator here for the NSID1 group's LUT
#: (`efc_cpu2.c:25401`), which is the same value a Reserve/DeleteReservation
#: entry carries at its `+4`.  On this board 10259475 / 0.
LUT_DESC_RESERVATION_OFF = 232
#: the "no WERU" sentinel, shared with `SystemBlockMapping` (`efc_cpu2.c:110144`)
INVALID_WERU = 0x3FFF
#: `dword_907150`, WMAs per WERU: `WeruConfiguration_GetWeruIdFromWma` is
#: `wma // 211968` (`efc_cpu2.c:23888`).  Same constant as `geom.AUS_PER_TLC_WERU`.
WMAS_PER_WERU = geom.AUS_PER_TLC_WERU
#: `WeruConfiguration_GetWlaOffsetFromWmaOffset`: `divmod(wmaOffset, 192)`.
WMA_STRIDE = 192
WMAS_PER_PAGE = 4


class _Unknown(object):
    """Distinct from False and from None: 'this backing cannot say'.

    The local twin of `titania.ftl.Unknown`, and it must refuse everything that
    one refuses -- see `_unknown()` below: whichever of the two you end up
    holding, the same use has to fail the same way.

    Falsey, so `if not x` still guards.  Every other use -- indexing, len(),
    iteration, `in`, attribute access, arithmetic, ordering -- raises, and the
    message names what was attempted, because a caller that got this far
    believed it was holding a measurement.  Nothing here softens the sentinel:
    it still refuses, it just refuses out loud.

    Spelling the refusals out as real methods is also what stops a type checker
    reporting every `Foo | UNKNOWN` use site.  Left undeclared, `UNKNOWN[i]`
    and `UNKNOWN.field` are "member does not exist" errors that fire at every
    site in the union including the guarded ones, which is hundreds of false
    positives and no true one -- the checker cannot see `_is_unknown()`.
    Declared, the member exists and its declared behaviour is `raise`, which is
    the runtime contract stated exactly.
    """
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super(_Unknown, cls).__new__(cls)
        return cls._inst

    def __bool__(self):
        return False
    __nonzero__ = __bool__

    def __repr__(self):
        return "UNKNOWN"

    __str__ = __repr__

    def _refuse(self, what):
        raise TypeError("%s an UNKNOWN value: this was never measured" % what)

    def __len__(self):
        self._refuse("len() of")

    def __iter__(self):
        # type: () -> "typing.Iterator[typing.Any]"
        # Annotated, not inferred: a body that only raises infers `NoReturn`,
        # and a checker then reports "not iterable" because NoReturn has no
        # `__next__`.  It never returns at all, so the annotation costs nothing.
        self._refuse("iterating")

    def __getitem__(self, key):
        self._refuse("subscripting")

    def __contains__(self, item):
        self._refuse("membership in")

    def __getattr__(self, name):
        # AttributeError, not TypeError, and deliberately: `hasattr`, three-arg
        # `getattr`, `copy` and `pickle` all probe for optional attributes and
        # only swallow AttributeError.  Raising anything else here would turn a
        # deepcopy of a record that merely contains an UNKNOWN into a crash.
        raise AttributeError(
            "%r has no attribute %r: this was never measured" % (self, name))

    def _refuse_arith(self, *_a, **_k):
        raise TypeError("arithmetic on an UNKNOWN value")

    __add__ = __radd__ = __sub__ = __rsub__ = _refuse_arith
    __mul__ = __rmul__ = __floordiv__ = __rfloordiv__ = _refuse_arith
    __truediv__ = __rtruediv__ = __mod__ = __rmod__ = _refuse_arith
    __lshift__ = __rshift__ = __and__ = __or__ = __xor__ = _refuse_arith
    __neg__ = __abs__ = __int__ = __index__ = _refuse_arith
    # Ordering only.  `__eq__`/`__ne__`/`__hash__` stay object's: the sentinel
    # is a singleton and `x is UNKNOWN` / `x == UNKNOWN` / putting it in a set
    # all have to keep working.
    __lt__ = __le__ = __gt__ = __ge__ = _refuse_arith


def _unknown():
    """The package-wide UNKNOWN if ftl is importable, else a local twin."""
    try:
        return _sib("ftl").UNKNOWN
    except Exception:                                   # pragma: no cover
        return _Unknown()


UNKNOWN = _unknown()


# ===========================================================================
# decoding one entry
# ===========================================================================
class TrimEntry(object):
    """One TrimLogEntry, exactly as it sits in the AU payload of one page.

    `ranges` is the raw `[(first, count)]` pair list.  It is not
    normalised to LMAs here, because the unit depends on `type` -- use
    `lma_ranges(granularity)` for type 0 and `entry_ranges()` for types 1/2, or
    `covered_lmas(granularity)` which refuses the combination it cannot express.
    """

    __slots__ = ("type", "sequence", "mso_id", "ranges", "sentinel",
                 "placement", "page_sequence", "parity_flags", "raw_header",
                 "wma", "weru_id")

    def __init__(self, type, sequence, mso_id, ranges, sentinel=None,
                 placement=None, page_sequence=None, parity_flags=None,
                 raw_header=None, wma=None, weru_id=None):
        self.type = type
        #: the reserved-space accumulator, not a sequence -- see the module
        #: docstring.  0 for type 0.
        self.sequence = sequence
        self.mso_id = mso_id
        self.ranges = list(ranges)
        self.sentinel = sentinel
        #: `(ce, block, page)` of the copy this came from, if known.
        self.placement = placement
        #: the WMA of AU0 of this copy, and the WERU it is in.  `None` when the
        #: copy sits in a block the WERU configuration gives no slot (the
        #: twelfth block), which is a real fact about the address space, not a
        #: read failure.  `wma_offset` is what the watermark compares against.
        self.wma = wma
        self.weru_id = weru_id
        #: the 62-bit rebuildSequenceNumber from aux AU0/AU1
        #: (`efc_cpu2.c:43135-43137`); the write order key.
        self.page_sequence = page_sequence
        self.parity_flags = parity_flags
        self.raw_header = raw_header

    # -- derived ------------------------------------------------------------
    @property
    def wma_offset(self):
        """-> the WMA offset inside its WERU, or None.  The write-order key."""
        if self.wma is None:
            return None
        return self.wma % WMAS_PER_WERU

    @property
    def is_trim(self):
        """True only for type 0, the one that actually unmaps LMAs."""
        return self.type == TRIM

    @property
    def type_name(self):
        return TYPE_NAMES.get(self.type, "type%d" % self.type)

    @property
    def expected_sentinel_group(self):
        """-> the msoGroupId the sentinel implies, or None if the sentinel is
        not one of the two.  `efc_cpu2.c:33515-33518`."""
        if self.sentinel == SENTINEL_PARITY:
            return PARITY_GROUP_ID
        if self.sentinel == SENTINEL_DATA:
            return None             # "not group 5" -- the firmware's else-branch
        return None

    def lma_ranges(self):
        """type 0 only -> `[(lma, count)]` in LMA units.  Raises otherwise."""
        if self.type != TRIM:
            raise ValueError(
                "entry type %s carries LUT ENTRY INDICES, not LMAs -- "
                "sub_8980/sub_4033EDC2 index lutTable + 4*first with no shift "
                "(efc_cpu2.c:23533, :109991).  Use entry_ranges()."
                % self.type_name)
        return list(self.ranges)

    def entry_ranges(self):
        """types 1/2 only -> `[(lutEntryIndex, count)]`.  Raises otherwise."""
        if self.type == TRIM:
            raise ValueError(
                "a Trim entry carries LMAs, not LUT entry indices "
                "(sub_8A8E asserts lma %% lutGranularity == 0, Lut.c:1215).  "
                "Use lma_ranges().")
        return list(self.ranges)

    def covered_lmas(self, granularity=None):
        """-> the set of LMAs this entry names, or UNKNOWN.

        For type 0 that is the ranges verbatim.  For types 1/2 it needs the
        MSO's LUT granularity to turn entry indices into LMAs; without one this
        returns UNKNOWN rather than pretending the index is an LMA.
        """
        if self.type == TRIM:
            out = set()
            for first, count in self.ranges:
                out.update(range(first, first + count))
            return out
        if not granularity:
            return UNKNOWN
        out = set()
        for first, count in self.ranges:
            out.update(range(first * granularity,
                             (first + count) * granularity))
        return out

    def n_lmas(self, granularity=None):
        """-> how many LMAs this entry names, or UNKNOWN (types 1/2, no gran)."""
        if self.type == TRIM:
            return sum(c for _f, c in self.ranges)
        if not granularity:
            return UNKNOWN
        return sum(c for _f, c in self.ranges) * granularity

    def __repr__(self):
        return ("<TrimEntry %s mso %s n=%d seq=%s sentinel=%s at %s>"
                % (self.type_name, self.mso_id, len(self.ranges),
                   self.sequence,
                   "None" if self.sentinel is None else "0x%08X" % self.sentinel,
                   self.placement))


def decode_entry(body, sentinel=None, placement=None, page_sequence=None,
                 parity_flags=None, strict=True, wma=None, weru_id=None):
    """The 16384-byte AU payload of a trim-log page -> `TrimEntry`.

    `strict` applies the firmware's own bounds, so a page that is not a trim
    entry raises instead of decoding into plausible nonsense:
      * `type in (0, 1, 2)`             -- the replay HALTs otherwise
                                           (`efc_cpu2.c:139196-139209`)
      * `numberOfLmaRanges < 256`       -- `:33085-33091`
      * `12 + 6*n <= len(body)`         -- `:33537-33543`
    It does not check the padding bytes, which are zero on 868/868 records here
    but are not asserted anywhere in the firmware.
    """
    if len(body) < HEADER_BYTES:
        raise ValueError("a trim entry is at least %d bytes, got %d"
                         % (HEADER_BYTES, len(body)))
    typ = body[0]
    seq = struct.unpack_from("<I", body, 4)[0]
    mso = body[8]
    n = struct.unpack_from("<H", body, 10)[0]
    if strict:
        if typ not in (TRIM, RESERVE, DELETE_RESERVATION):
            raise ValueError("trim entry type %d is not one the replay accepts "
                             "(efc_cpu2.c:139196)" % typ)
        if n >= MAX_LMA_RANGES:
            raise ValueError("numberOfLmaRanges %d >= cTrimCommandMaxLmaRange"
                             "Count %d (efc_cpu2.c:33087)" % (n, MAX_LMA_RANGES))
        if HEADER_BYTES + RANGE_BYTES * n > len(body):
            raise ValueError("12 + 6*%d overruns the %d-byte entry "
                             "(efc_cpu2.c:33539)" % (n, len(body)))
    ranges = [struct.unpack_from("<IH", body, HEADER_BYTES + RANGE_BYTES * i)
              for i in range(n)]
    return TrimEntry(typ, seq, mso, ranges, sentinel=sentinel,
                     placement=placement, page_sequence=page_sequence,
                     parity_flags=parity_flags,
                     raw_header=bytes(body[:HEADER_BYTES]),
                     wma=wma, weru_id=weru_id)


def is_trim_log_aux(lmas):
    """-> True if this page's four aux LMAs are a trim-log page's.

    The exact shape the writer produces: `[sentinel, -1, -1, -1]`
    (`efc_cpu2.c:33515-33522` for the sentinel, `sub_1C238` `:42857-42859` for
    the -1 padding).  Requiring the padding too is what keeps an ordinary page
    whose LMA happens to be 0xFFFFFFFE out of the log.
    """
    return (len(lmas) >= 1 and lmas[0] in SENTINELS
            and all(v == PAD_LMA for v in lmas[1:]))


def page_sequence_from_aux(tags):
    """-> the 62-bit rebuildSequenceNumber a trim-log page carries.

    AU0's tag holds `seq >> 31` and AU1's holds `seq & 0x7FFFFFFF`, each in the
    low 31 bits with bit 31 reserved for the parity flag
    (`efc_cpu2.c:43135-43137`).  AUs 2/3 are cleared to the flag alone.
    """
    if len(tags) < 2:
        return UNKNOWN
    return ((tags[0] & 0x7FFFFFFF) << 31) | (tags[1] & 0x7FFFFFFF)


# ===========================================================================
# finding the log
# ===========================================================================
def trim_mso_id(mso_table):
    """-> the Trim/System msoId, or UNKNOWN without an msoIdTable.

    `msoIdTable[msoType*6 + msoGroupId]` with msoType 6 (Trim) and group 0
    (System) -- which is `byte_900024`, the one value `sub_402CF722`
    (`efc_cpu3.c:25513`) ever stores into `byte_907AC4`, the trim log's msoId
    (`efc_cpu3.c:36004`, `:37808`).
    """
    if not mso_table:
        return UNKNOWN
    try:
        v = mso_table.mso_id(TRIM_MSO_TYPE, SYSTEM_GROUP_ID)
    except Exception:
        return UNKNOWN
    return UNKNOWN if v in (None, 0xFF) else v


def mso_group_id(mso_table, mso_id):
    """-> the msoGroupId of `mso_id`, or UNKNOWN.  Inverts the msoIdTable."""
    if not mso_table:
        return UNKNOWN
    try:
        for t in range(len(mso_table.raw) // 6):
            row = mso_table.raw[t * 6:(t + 1) * 6]
            for g, v in enumerate(row):
                if v == mso_id and not (not any(row)
                                        or all(x == 0xFF for x in row)):
                    return g
    except Exception:
        return UNKNOWN
    return UNKNOWN


def granularity_for(mso_table, mso_id):
    """-> LUT granularity in LMAs for `mso_id`, or UNKNOWN.

    From the msoGroupId only, and only for groups whose granularity is
    measured.  A group not in `GROUP_GRANULARITY` gives UNKNOWN, not a default.
    """
    g = mso_group_id(mso_table, mso_id)
    if g is UNKNOWN:
        return UNKNOWN
    return GROUP_GRANULARITY.get(g, UNKNOWN)


def trim_werus(ftl_obj):
    """-> ({weruId: [sources]}, notes) -- the WERUs the Trim MSO owns.

    Two independent owner records are consulted and both are reported, because
    they are known to disagree elsewhere on this board (108 of 820 WERUs):
    `weruDescriptor +0` and `WeruUsageLog +16`.  A weruId named by only one
    source is still returned, tagged with which.
    """
    notes = []
    mso = trim_mso_id(getattr(ftl_obj, "mso_table", None))
    if mso is UNKNOWN:
        notes.append("no msoIdTable: the Trim msoId is UNKNOWN, so no WERU can "
                     "be attributed to it")
        return {}, notes
    found = collections.defaultdict(list)

    try:
        decode_weru_descriptor = _sib("ftl").decode_weru_descriptor
        sec = ftl_obj.sysmeta.get("weruDescriptor")
        recs = decode_weru_descriptor(sec.whole(fill=b"\xff"))
        for wid, w in recs.items():
            if w.owner == mso:
                found[wid].append("weruDescriptor")
    except Exception as e:
        notes.append("weruDescriptor unavailable: %s" % e)

    try:
        log = ftl_obj.root.weru_usage_log()
        for wid, owner in log.owners().items():
            if owner == mso:
                found[wid].append("WeruUsageLog")
    except Exception as e:
        notes.append("WeruUsageLog unavailable: %s" % e)

    if not found and not notes:
        notes.append("both owner records were read and neither names a WERU for "
                     "msoId %d -- a real negative for THIS image, not proof the "
                     "drive has no trim log" % mso)
    return dict(found), notes


# ===========================================================================
# the retirement watermark
# ===========================================================================
def decode_snapshot_list(desc):
    """296-byte LUT descriptor -> `[{mso_id, weru_id, activation, wma_offset}]`.

    Only the first `count` elements, and only up to the firmware's own bound.
    `weru_id` is None for the `0x3FFF` sentinel -- which is not "no record":
    `sub_4035B94E` treats a sentinel weruId as "this LUT has never been
    snapshotted, so retire nothing" and aborts (`efc_cpu2.c:139303-139309`).
    """
    if len(desc) < SNAP_LIST_OFF + SNAP_ENTRY * SNAP_MAX:
        raise ValueError("a LUT descriptor is %d bytes, got %d"
                         % (LUT_DESC_BYTES, len(desc)))
    n = struct.unpack_from("<I", desc, SNAP_COUNT_OFF)[0]
    if n > SNAP_MAX:
        raise ValueError("snapshotInformation.count %d exceeds the firmware's "
                         "own bound of %d (Lut.c:1500)" % (n, SNAP_MAX))
    out = []
    for i in range(n):
        o = SNAP_LIST_OFF + SNAP_ENTRY * i
        weru = struct.unpack_from("<H", desc, o + SNAP_WERU_OFF)[0]
        out.append({
            "index": i,
            "mso_id": desc[o + SNAP_MSO_OFF],
            "weru_id": None if weru == INVALID_WERU else weru,
            "activation": struct.unpack_from("<Q", desc,
                                             o + SNAP_ACTIVATION_OFF)[0],
            "wma_offset": struct.unpack_from("<I", desc, o + SNAP_WMA_OFF)[0],
        })
    return out


def lut_descriptors(ftl_obj, store_mso=40):
    """-> `[(label, raw296)]` for every LUT descriptor this backing can read.

    One per LUT region in `store_mso`'s LMA space, walked by
    `Ftl.lut_regions`.  A region whose descriptor LMA is not held is skipped
    with a note rather than guessed at.
    """
    out, notes = [], []
    try:
        regs = ftl_obj.lut_regions(store_mso=store_mso)
    except Exception as e:                                  # pragma: no cover
        return [], ["lut_regions unavailable: %s" % e]
    if regs is UNKNOWN or not regs:
        return [], ["no LUT region readable in msoId %d" % store_mso]
    for reg in regs:
        d = ftl_obj.read_lma(store_mso, reg.base + 4)
        if d is UNKNOWN or d is None:
            notes.append("LUT region at LMA %d: descriptor not held here"
                         % reg.base)
            continue
        out.append(("msoId %d LMA %d (granularity %d, initMso %d)"
                    % (store_mso, reg.base, reg.granularity, reg.init_mso),
                    bytes(d[:LUT_DESC_BYTES])))
    return out, notes


class Watermark(object):
    """The trim log's retirement point, as `sub_4035B94E` computes it.

    Everything with `wma_offset` strictly greater than this is still live and
    must be replayed at mount; everything at or below it has had its validMap
    bit cleared and is retired -- present on media only until the WERU is
    recycled.
    """
    __slots__ = ("mso_id", "weru_id", "activation", "wma_offset", "sources",
                 "candidates", "aborted", "notes")

    def __init__(self, mso_id, weru_id, activation, wma_offset, sources,
                 candidates, aborted=False, notes=None):
        self.mso_id = mso_id
        self.weru_id = weru_id
        self.activation = activation
        self.wma_offset = wma_offset
        #: the descriptor label(s) that carry this exact record
        self.sources = list(sources)
        #: every Trim record found, so a disagreement is visible not hidden
        self.candidates = list(candidates)
        #: True when a snapshot carries the 0x3FFF sentinel, which makes the
        #: firmware retire nothing (`efc_cpu2.c:139303`).
        self.aborted = aborted
        self.notes = list(notes or [])

    @property
    def wma(self):
        if self.weru_id is None:
            return None
        return self.weru_id * WMAS_PER_WERU + self.wma_offset

    def split(self):
        """-> `(weruId, page, slot, au)` of the watermark WMA, or None."""
        if self.weru_id is None:
            return None
        rem = self.wma_offset
        page, r = divmod(rem, WMA_STRIDE)
        return (self.weru_id, page, r // WMAS_PER_PAGE, r % WMAS_PER_PAGE)

    def __repr__(self):
        return ("<Watermark msoId %s weru %s activation %s wmaOffset %s%s>"
                % (self.mso_id, self.weru_id, self.activation, self.wma_offset,
                   " ABORTED" if self.aborted else ""))


def retirement_watermark(ftl_obj, mso_id=None, store_mso=40):
    """-> `Watermark`, or UNKNOWN when no LUT descriptor can be read.

    Reproduces `sub_4035B94E` (`efc_cpu2.c:139280-139321`) exactly: scan every
    LUT descriptor's `snapshotInformation` list, keep the elements whose msoId
    is the Trim MSO's, and take the minimum of `(activation, wmaOffset)` --
    with the firmware's abort on a `0x3FFF` weruId.
    """
    if mso_id is None:
        mso_id = trim_mso_id(getattr(ftl_obj, "mso_table", None))
        if mso_id is UNKNOWN:
            return UNKNOWN
    descs, notes = lut_descriptors(ftl_obj, store_mso=store_mso)
    if not descs:
        return UNKNOWN
    cands = []
    for label, d in descs:
        try:
            snap = decode_snapshot_list(d)
        except ValueError as e:
            notes.append("%s: %s" % (label, e))
            continue
        for e in snap:
            if e["mso_id"] == mso_id:
                e = dict(e)
                e["source"] = label
                cands.append(e)
    if not cands:
        notes.append("no LUT snapshotInformation list names msoId %d, so the "
                     "firmware has no retirement point for it" % mso_id)
        return Watermark(mso_id, None, None, None, [], [], notes=notes)
    blank = [c for c in cands if c["weru_id"] is None]
    if blank:
        notes.append("%d snapshot record(s) carry the 0x3FFF sentinel; "
                     "sub_4035B94E aborts on the first one and retires NOTHING "
                     "(efc_cpu2.c:139303)" % len(blank))
        b = blank[0]
        return Watermark(mso_id, None, b["activation"], b["wma_offset"],
                         [b["source"]], cands, aborted=True, notes=notes)
    best = min(cands, key=lambda c: (c["activation"], c["wma_offset"]))
    same = [c["source"] for c in cands
            if (c["weru_id"], c["activation"], c["wma_offset"])
            == (best["weru_id"], best["activation"], best["wma_offset"])]
    if len(same) < len(cands):
        notes.append("the %d LUT snapshots DISAGREE about the Trim write front; "
                     "the firmware's minimum was taken and every candidate is "
                     "in .candidates" % len(cands))
    return Watermark(mso_id, best["weru_id"], best["activation"],
                     best["wma_offset"], same, cands, notes=notes)


def _wma_of(ce, block, page, config, weru_id):
    """-> the WMA of AU0 of (ce, block, page), or None if it has no slot.

    Pure arithmetic (`ftl.physical_to_wma`), reimplemented here so this module
    keeps working when `ftl` is not importable.  None is the honest answer for
    the twelfth block, which `WeruConfig.slot_to_physical` refuses.
    """
    if config is None or weru_id is None:
        return None
    try:
        slot = config.physical_to_slot(ce, block)
    except Exception:                                       # pragma: no cover
        return None
    if slot is None:
        return None
    return (weru_id * WMAS_PER_WERU + page * WMA_STRIDE + slot * WMAS_PER_PAGE)


# ===========================================================================
# the log
# ===========================================================================
class TrimLog(object):
    """Every trim-log entry physically present in the WERUs that were read.

    Not a history and not the pending set -- see the module docstring.  The
    only three things it will say about an LMA are TRIMMED, LIVE and UNKNOWN.
    """

    def __init__(self, entries=None, pages=None, mirrors=None, werus=None,
                 mso_table=None, notes=None, coverage=None, watermark=None):
        #: deduplicated entries, in write order == ascending `wma_offset`.
        #: Not `page_sequence`: two independent sequence pools feed this log
        #: (see the module docstring), so that key interleaves them wrongly.
        self.entries = list(entries or [])
        #: the `Watermark`, when the caller supplied an `Ftl` to find it with.
        self.watermark = watermark
        #: how many programmed trim-log pages were seen (entries x copies).
        self.pages = 0 if pages is None else pages
        #: {(ce, page): n_copies} -- the mirror census.
        self.mirrors = dict(mirrors or {})
        self.werus = dict(werus or {})
        self.mso_table = mso_table
        self.notes = list(notes or [])
        #: {'pages_read':, 'erased':, 'undecodable':, 'absent':, 'foreign':}
        self.coverage = dict(coverage or {})

    # -- construction -------------------------------------------------------
    @classmethod
    def scan_weru(cls, dev, config, weru_id, codec=None, pages=None,
                  mso_table=None, notes=None):
        """Read every page of one WERU and collect the trim-log entries.

        Every page is MCRC-gated (all four AUs must pass the seed-free CRC-48)
        before its aux is believed, so a partial or mis-descrambled read cannot
        enter the log.  Slots the backing cannot serve are counted, not
        silently skipped -- `coverage['absent']`.
        """
        if codec is None:
            codec = _sib("ftl").default_codec()
        n_pages = (geom.PAGES_PER_BLOCK_SLC if config.slc
                   else geom.PAGES_PER_BLOCK_TLC)
        page_range = range(n_pages) if pages is None else pages
        cov = collections.Counter()
        raw = []
        # Walk the PROGRAMMED region, not the addressable slots.  A metadata
        # WERU allocates twelve blocks and only eleven have a slot
        # (`slot_to_physical` returns None for the twelfth), and on this board
        # the twelfth block carries a full 86-page share of the log -- iterating
        # slots loses it and makes every one of its twins look unmirrored.
        n_ce = -(-config.blocks_per_weru // config.blocks_per_ce)
        for ce in range(config.ce_base, config.ce_base + n_ce):
            if ce in geom.CE_DEAD:
                cov["dead_ce_blocks"] += config.blocks_per_ce
            for block in range(config.block_base,
                               config.block_base + config.blocks_per_ce):
              for pg in page_range:
                try:
                    buf = dev.read_page(ce, block, pg, slc=config.slc)
                except Exception:
                    cov["absent"] += 1
                    continue
                if buf is None:
                    cov["absent"] += 1
                    continue
                if buf == b"\xff" * len(buf):
                    cov["erased"] += 1
                    continue
                aus, ok = codec.decode(buf, block, pg)
                if aus is None or (ok is not UNKNOWN and not all(ok)):
                    cov["undecodable"] += 1
                    continue
                cov["pages_read"] += 1
                lmas = [struct.unpack_from("<I", a, 0)[0] for a in aus]
                tags = [struct.unpack_from("<I", a, 4)[0] for a in aus]
                if not is_trim_log_aux(lmas):
                    cov["foreign"] += 1
                    continue
                body = b"".join(a[geom.AU_AUX:] for a in aus)
                try:
                    e = decode_entry(
                        body, sentinel=lmas[0], placement=(ce, block, pg),
                        page_sequence=page_sequence_from_aux(tags),
                        parity_flags=[bool(t >> 31) for t in tags],
                        wma=_wma_of(ce, block, pg, config, weru_id),
                        weru_id=weru_id)
                except ValueError:
                    cov["malformed"] += 1
                    continue
                raw.append(e)
        return cls._from_raw(raw, cov, {weru_id: config}, mso_table, notes)

    @classmethod
    def scan(cls, dev, ftl_obj, codec=None, weru_ids=None, pages=None,
             watermark=True):
        """Find the Trim MSO's WERUs through `ftl_obj` and scan them all.

        `watermark=True` also reads the retirement watermark out of the LUT
        descriptors, so `completeness()` can answer without a second mount.
        """
        notes = []
        if weru_ids is None:
            found, n = trim_werus(ftl_obj)
            notes.extend(n)
            weru_ids = sorted(found)
            for wid, src in sorted(found.items()):
                notes.append("weru %d attributed to the Trim MSO by %s"
                             % (wid, " and ".join(src)))
        raw, cov, cfgs = [], collections.Counter(), {}
        for wid in weru_ids:
            try:
                cfg = ftl_obj.weru_config(wid)
            except Exception as e:
                notes.append("weru %d: no configuration (%s)" % (wid, e))
                continue
            part = cls.scan_weru(dev, cfg, wid, codec=codec, pages=pages,
                                 mso_table=getattr(ftl_obj, "mso_table", None))
            raw.extend(part._raw_entries)
            cov.update(part.coverage)
            cfgs[wid] = cfg
        wm = None
        if watermark:
            try:
                wm = retirement_watermark(ftl_obj)
            except Exception as e:                          # pragma: no cover
                notes.append("retirement watermark unavailable: %s" % e)
            if wm is UNKNOWN:
                notes.append("no LUT descriptor readable, so the retirement "
                             "watermark is UNKNOWN")
                wm = None
        return cls._from_raw(raw, cov, cfgs,
                             getattr(ftl_obj, "mso_table", None), notes,
                             watermark=wm)

    @classmethod
    def _from_raw(cls, raw, cov, cfgs, mso_table, notes, watermark=None):
        """Deduplicate the mirror copies and order the survivors."""
        notes = list(notes or [])
        by_pos = collections.defaultdict(list)
        for e in raw:
            ce, _blk, pg = e.placement
            by_pos[(ce, pg)].append(e)
        mirrors, kept, disagree = {}, [], 0
        for pos, group in by_pos.items():
            mirrors[pos] = len(group)
            # Keep a copy that has a WMA.  The twelfth block of a metadata WERU
            # has no slot, so its copy carries wma=None; picking it would lose
            # the write-order key for that entry.
            head = next((g for g in group if g.wma is not None), group[0])
            for other in group:
                if other is head:
                    continue
                if (other.raw_header != head.raw_header
                        or other.ranges != head.ranges
                        or other.page_sequence != head.page_sequence):
                    disagree += 1
            kept.append(head)
        if disagree:
            notes.append("%d mirrored copies DISAGREE with their twin -- the "
                         "first copy was kept and this is a real anomaly, not "
                         "noise" % disagree)
        # Write order = ascending wmaOffset.  See the module docstring for why
        # page_sequence is the wrong key across sentinel classes.
        kept.sort(key=lambda e: (e.wma if isinstance(e.wma, int) else -1,
                                 e.placement))
        obj = cls(kept, pages=len(raw), mirrors=mirrors, werus=cfgs,
                  mso_table=mso_table, notes=notes, coverage=dict(cov),
                  watermark=watermark)
        obj._raw_entries = raw
        return obj

    _raw_entries = ()

    # -- queries ------------------------------------------------------------
    def of_type(self, type):
        return [e for e in self.entries if e.type == type]

    def trims(self):
        """Only the type-0 entries -- the ones that actually unmap LMAs."""
        return self.of_type(TRIM)

    def trimmed_ranges(self, mso_id=None):
        """-> `[(mso_id, lma, count)]` from the type-0 entries, in write order."""
        out = []
        for e in self.trims():
            if mso_id is not None and e.mso_id != mso_id:
                continue
            for lma, count in e.lma_ranges():
                out.append((e.mso_id, lma, count))
        return out

    def trimmed_lmas(self, mso_id):
        """-> the set of LMAs the surviving type-0 entries name for one MSO."""
        out = set()
        for _m, lma, count in self.trimmed_ranges(mso_id):
            out.update(range(lma, lma + count))
        return out

    def reservation_balance(self):
        """-> {(msoId, lutEntryIndex): net} over types 1 and 2, in write order.

        A negative value is a DeleteReservation with no matching Reserve in
        what was read.  That is not by itself evidence of truncation -- on
        this board 87 entries of the Trim WERU sit on the dead CE0 and cannot
        be read at all, which is a large enough hole to account for the whole
        deficit.  Use `completeness()`, which reads the watermark, for the
        truncation verdict.  The units are LUT entry indices, not LMAs.
        """
        net = collections.Counter()
        for e in self.entries:
            if e.type == RESERVE:
                sign = 1
            elif e.type == DELETE_RESERVATION:
                sign = -1
            else:
                continue
            for first, count in e.entry_ranges():
                for i in range(first, first + count):
                    net[(e.mso_id, i)] += sign
        return dict(net)

    def completeness(self, watermark=None):
        """-> a dict saying how much of this log has been retired, or UNKNOWN.

        The keys, all facts rather than adjectives:

            watermark        the `Watermark` used
            weru             the WERU it points into
            retired          entries at or below it -- the firmware has already
                             cleared their validMap bits
            live             entries above it -- the replay set
            unreadable       entries that are physically present but on a dead
                             CE, split into `unreadable_retired`/`_live`
            span             (lowest, highest) wmaOffset actually read
            starts_at_zero   True when the surviving run reaches the first WMA
                             of the WERU, i.e. Nothing has been erased off the
                             front of this WERU
            gaps             page rows inside the span with no entry at all
            capacity         the WERU's WMA capacity, and how much is used
            truncated_before the activation stamp everything older went with

        `unreadable` assumes one entry per (ce, page row) -- the mirror shape
        this module measures on the readable CEs.  `selftest` checks that
        assumption against the persisted `validCount`, which is derived from
        neither.
        """
        wm = watermark if watermark is not None else self.watermark
        if wm is None or wm is UNKNOWN or wm.weru_id is None:
            return UNKNOWN
        cfg = self.werus.get(wm.weru_id)
        offs = [e.wma_offset for e in self.entries if e.wma_offset is not None]
        if not offs:
            return UNKNOWN
        rows = sorted(set(e.placement[2] for e in self.entries))
        retired = [e for e in self.entries
                   if e.wma_offset is not None and e.wma_offset <= wm.wma_offset]
        live = [e for e in self.entries
                if e.wma_offset is not None and e.wma_offset > wm.wma_offset]

        # entries on a dead CE: physically present, unreadable, one per row
        dead_lo = dead_hi = 0
        if cfg is not None:
            n_ce = -(-cfg.blocks_per_weru // cfg.blocks_per_ce)
            for ce in range(cfg.ce_base, cfg.ce_base + n_ce):
                if ce not in geom.CE_DEAD:
                    continue
                slot = cfg.blocks_per_ce * (ce - cfg.ce_base)
                for p in rows:
                    o = p * WMA_STRIDE + slot * WMAS_PER_PAGE
                    if o <= wm.wma_offset:
                        dead_lo += 1
                    else:
                        dead_hi += 1

        pages_per_block = geom.PAGES_PER_BLOCK_SLC
        if cfg is not None and not cfg.slc:
            pages_per_block = geom.PAGES_PER_BLOCK_TLC
        capacity = pages_per_block * WMA_STRIDE
        gaps = [p for p in range(rows[0], rows[-1] + 1) if p not in set(rows)]
        return {
            "watermark": wm,
            "weru": wm.weru_id,
            "retired": len(retired),
            "live": len(live),
            "unreadable_retired": dead_lo,
            "unreadable_live": dead_hi,
            "unreadable": dead_lo + dead_hi,
            "total_written": len(self.entries) + dead_lo + dead_hi,
            "span": (min(offs), max(offs)),
            "page_rows": (rows[0], rows[-1]),
            "starts_at_zero": min(offs) < WMA_STRIDE,
            "gaps": gaps,
            "capacity": capacity,
            "used_fraction": (max(offs) + 1) / float(capacity),
            "truncated_before": wm.activation,
        }

    def completeness_report(self, watermark=None):
        """-> the same thing as lines of English.  Says UNKNOWN where it is."""
        c = self.completeness(watermark)
        if c is UNKNOWN:
            return ["trim-log completeness: UNKNOWN -- no retirement watermark "
                    "could be read from any LUT descriptor"]
        wm = c["watermark"]
        L = ["retirement watermark: weru %d, activation %d, wmaOffset %d "
             "(%s)" % (wm.weru_id, wm.activation, wm.wma_offset,
                       "agreed by %d LUT snapshot(s)" % len(wm.sources))]
        s = wm.split()
        if s:
            L.append("   = weru %d page %d slot %d au %d" % s)
        L.append("   TRUNCATED: everything the Trim MSO wrote before WERU %d's "
                 "activation (stamp %d) went with the predecessor WERU, which "
                 "has been erased and re-owned; its size is NOT recoverable "
                 "from this image" % (c["weru"], c["truncated_before"]))
        L.append("   of %d entries written into weru %d: %d RETIRED "
                 "(validMap cleared), %d live"
                 % (c["total_written"], c["weru"],
                    c["retired"] + c["unreadable_retired"],
                    c["live"] + c["unreadable_live"]))
        if c["unreadable"]:
            L.append("   %d of those are on a dead CE -- present, unreadable "
                     "(%d retired, %d live)"
                     % (c["unreadable"], c["unreadable_retired"],
                        c["unreadable_live"]))
        L.append("   span wmaOffset %d..%d of %d (%.1f%% of the WERU), "
                 "page rows %d..%d, %d gap(s)"
                 % (c["span"][0], c["span"][1], c["capacity"],
                    100.0 * c["used_fraction"], c["page_rows"][0],
                    c["page_rows"][1], len(c["gaps"])))
        L.append("   the surviving run %s the first WMA of the WERU, so %s "
                 "erased off the FRONT of this WERU"
                 % ("reaches" if c["starts_at_zero"] else "does NOT reach",
                    "nothing has been" if c["starts_at_zero"]
                    else "entries HAVE been"))
        for n in wm.notes:
            L.append("   note: %s" % n)
        return L

    def is_trimmed(self, mso, lma, lut=None):
        """-> TRIMMED, LIVE or UNKNOWN.  Never a guess.

        TRIMMED  a surviving type-0 entry names this LMA and, if `lut` was
                 given, the LUT does not map it any more.
        LIVE     `lut` maps it to a real WMA.  A mapped LMA cannot be trimmed:
                 Trim clears the entry to 0xFFFFFFFF (`sub_8082`), so anything
                 still holding a WMA has been written since.
        UNKNOWN  everything else -- and that is the common case.  The log is a
                 bounded window, so "not named here" is not "not trimmed", and
                 the LUT's 0xFFFFFFFF means unmapped, which is trimmed and
                 never-written with no way to tell them apart.

        `lut` is anything with `.wma(lma)` returning an int or a sentinel --
        `ftl.LutRegion`, `ftl.SystemLut`, `ftl.UserLutLut`.
        """
        named = False
        for _m, first, count in self.trimmed_ranges(mso):
            if first <= lma < first + count:
                named = True
                break
        mapped = None
        if lut is not None:
            try:
                w = lut.wma(lma)
            except Exception:
                w = UNKNOWN
            if isinstance(w, int):
                mapped = w not in LUT_INVALID and w != LUT_UNEXPLAINED
        if mapped:
            return LIVE
        if named and mapped is False:
            return TRIMMED
        if named and mapped is None:
            return TRIMMED
        return UNKNOWN

    # -- reporting ----------------------------------------------------------
    def summary(self):
        """-> a multi-line report.  Says UNKNOWN where it does not know."""
        L = []
        L.append("trim log: %d pages -> %d entries in WERUs %s"
                 % (self.pages, len(self.entries),
                    sorted(self.werus) or "(none scanned)"))
        if self.mirrors:
            hist = collections.Counter(self.mirrors.values())
            L.append("  copies per (ce, page): %s"
                     % dict(sorted(hist.items())))
        if self.coverage:
            L.append("  coverage: %s" % dict(sorted(self.coverage.items())))
        types = collections.Counter(e.type for e in self.entries)
        L.append("  types: %s"
                 % {TYPE_NAMES.get(k, k): v for k, v in sorted(types.items())})
        sent = collections.Counter(e.sentinel for e in self.entries)
        L.append("  aux sentinels: %s"
                 % {"0x%08X" % k: v for k, v in sorted(sent.items())})

        per = collections.defaultdict(lambda: [0, 0])
        for e in self.trims():
            per[e.mso_id][0] += 1
            per[e.mso_id][1] += e.n_lmas()
        if per:
            L.append("  TRIMMED (type 0), by target MSO:")
            for mso, (nr, nl) in sorted(per.items()):
                name = "?"
                if self.mso_table:
                    try:
                        name = self.mso_table.name(mso) or "?"
                    except Exception:
                        pass
                L.append("    msoId %2d %-30s %3d ranges, %8d LMAs = %8.1f MiB"
                         % (mso, name, nr, nl, nl * geom.AU_DATA / 1048576.0))
        else:
            L.append("  TRIMMED (type 0): none in what was read")

        others = collections.defaultdict(lambda: collections.Counter())
        for e in self.entries:
            if e.type in (RESERVE, DELETE_RESERVATION):
                others[e.mso_id][e.type] += sum(c for _f, c in e.ranges)
        for mso, c in sorted(others.items()):
            gran = granularity_for(self.mso_table, mso)
            L.append("  reservations msoId %2d: %d reserved, %d released "
                     "(LUT ENTRIES; granularity %s LMAs)"
                     % (mso, c[RESERVE], c[DELETE_RESERVATION], gran))
        bal = self.reservation_balance()
        neg = sum(1 for v in bal.values() if v < 0)
        if bal:
            L.append("  reservation ledger: %d entries touched, %d with a "
                     "negative balance (a Reserve that is not in what was READ "
                     "-- see reservation_balance(): this is NOT proof of "
                     "truncation)" % (len(bal), neg))
        for line in self.completeness_report():
            L.append("  " + line)
        L.append("  a trimmed LMA's LUT entry is 0xFFFFFFFF, identical to "
                 "never-written -- the LUT cannot distinguish them")
        for n in self.notes:
            L.append("  note: %s" % n)
        return "\n".join(L)

    def __repr__(self):
        return ("<TrimLog %d entries (%d pages), %d Trim, werus %s>"
                % (len(self.entries), self.pages, len(self.trims()),
                   sorted(self.werus)))


# ===========================================================================
# self-test
# ===========================================================================
_RESULTS = []


def _report(name, status, detail=""):                   # pragma: no cover
    _RESULTS.append((name, status))
    print("%-9s %-40s %s" % ("[%s]" % status, name, detail))


def selftest(argv=()):                                  # pragma: no cover
    """Everything checkable from this tree.  SKIPPED, never a fake PASS."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    print("titania.trim self-test")
    print("=" * 78)

    # ---- 1. pure decoding, no media --------------------------------------
    body = bytearray(ENTRY_BYTES)
    body[0] = TRIM
    struct.pack_into("<I", body, 4, 0)
    body[8] = 34
    struct.pack_into("<H", body, 10, 2)
    struct.pack_into("<IH", body, 12, 198720, 1472)
    struct.pack_into("<IH", body, 18, 4120128, 4416)
    e = decode_entry(bytes(body), sentinel=SENTINEL_PARITY)
    ok = (e.type == TRIM and e.mso_id == 34 and len(e.ranges) == 2
          and e.n_lmas() == 5888 and e.is_trim)
    _report("decode_entry round trip", "PASS" if ok else "FAIL",
            "12-byte header + 2 x 6-byte LmaRange, %d LMAs" % e.n_lmas())

    bad = bytearray(body)
    bad[0] = 3
    try:
        decode_entry(bytes(bad))
        _report("bad type rejected", "FAIL", "type 3 accepted")
    except ValueError as ex:
        _report("bad type rejected", "PASS", str(ex)[:60])
    bad = bytearray(body)
    struct.pack_into("<H", bad, 10, MAX_LMA_RANGES)
    try:
        decode_entry(bytes(bad))
        _report("range-count bound enforced", "FAIL", "256 accepted")
    except ValueError as ex:
        _report("range-count bound enforced", "PASS", str(ex)[:60])

    ok = (is_trim_log_aux([SENTINEL_DATA, PAD_LMA, PAD_LMA, PAD_LMA])
          and is_trim_log_aux([SENTINEL_PARITY, PAD_LMA, PAD_LMA, PAD_LMA])
          and not is_trim_log_aux([SENTINEL_DATA, 0, PAD_LMA, PAD_LMA])
          and not is_trim_log_aux([PAD_LMA, PAD_LMA, PAD_LMA, PAD_LMA])
          and not is_trim_log_aux([0xFFFFFFFD, PAD_LMA, PAD_LMA, PAD_LMA]))
    _report("aux signature discriminates", "PASS" if ok else "FAIL",
            "[sentinel, -1, -1, -1]; -3 and a bare -1 page are refused")

    ok = (page_sequence_from_aux([0x80000000, 0x8032753E]) == 0x0032753E
          and page_sequence_from_aux([0x80000001, 0x80000000]) == 1 << 31)
    _report("page sequence split 31/31", "PASS" if ok else "FAIL",
            "AU0 = seq>>31, AU1 = seq & 0x7FFFFFFF (efc_cpu2.c:43135)")

    # the type-dependent units must be refused, not coerced
    r = TrimEntry(RESERVE, 1, 45, [(738553, 1)])
    try:
        r.lma_ranges()
        _report("type-dependent units refused", "FAIL", "coerced")
    except ValueError:
        ok = r.covered_lmas() is UNKNOWN and r.covered_lmas(16) == set(
            range(738553 * 16, 738554 * 16))
        _report("type-dependent units refused", "PASS" if ok else "FAIL",
                "Reserve ranges are LUT entry indices; UNKNOWN without a "
                "granularity")

    # the snapshotInformation element, against the firmware's own initialiser
    d = bytearray(LUT_DESC_BYTES)
    struct.pack_into("<I", d, SNAP_COUNT_OFF, 2)
    # element 0: the `dword_1D3F8` sentinel shape, cpu2 ATCM 0x1D3F8
    struct.pack_into("<24s", d, SNAP_LIST_OFF,
                     bytes.fromhex("0000ff3f00000000"
                                   "ffffffffffffffff"
                                   "0000000000000000"))
    d[SNAP_LIST_OFF] = 42
    # element 1 at +24: a real record
    struct.pack_into("<BxH4xQI4x", d, SNAP_LIST_OFF + SNAP_ENTRY,
                     42, 21, 2642, 15775)
    s = decode_snapshot_list(bytes(d))
    ok = (len(s) == 2 and s[0]["weru_id"] is None
          and s[0]["activation"] == 0xFFFFFFFFFFFFFFFF
          and s[1] == {"index": 1, "mso_id": 42, "weru_id": 21,
                       "activation": 2642, "wma_offset": 15775})
    _report("snapshot element is 24 B at +16", "PASS" if ok else "FAIL",
            "stride 24 and base +16 verified at the instruction level in "
            "sub_4033F01E / sub_4033F970 / sub_4035B94E; sentinel weruId "
            "0x3FFF and activation UINT64_MAX are dword_1D3F8 verbatim")
    over = bytearray(d)
    struct.pack_into("<I", over, SNAP_COUNT_OFF, SNAP_MAX + 1)
    try:
        decode_snapshot_list(bytes(over))
        _report("snapshot count bound enforced", "FAIL", "10 accepted")
    except ValueError as ex:
        _report("snapshot count bound enforced", "PASS", str(ex)[:60])

    # ---- 2. the media ----------------------------------------------------
    dump = config.dump_path()
    fw = root / "dumps" / "fw.sram.efc.bin"
    if not dump.exists():
        _report("Trim MSO located", "SKIPPED", "no <dump>/ in this tree")
        _report("trim log scanned", "SKIPPED", "no <dump>/")
        return _finish()
    try:
        DumpDevice = _sib("dumpdev").DumpDevice
        Ftl = _sib("ftl").Ftl
        dev = DumpDevice(dump)
        f = Ftl.from_device(dev, firmware=fw if fw.exists() else None)
    except Exception as ex:
        _report("Trim MSO located", "SKIPPED", "cannot mount: %s" % ex)
        _report("trim log scanned", "SKIPPED", "cannot mount")
        return _finish()

    mso = trim_mso_id(f.mso_table)
    if mso is UNKNOWN:
        _report("Trim MSO located", "SKIPPED",
                "no msoIdTable (needs dumps/fw.sram.efc.bin)")
    else:
        _report("Trim MSO located", "PASS" if mso == 42 else "FAIL",
                "msoIdTable[6*6+0] = %d = %s" % (mso, f.mso_table.name(mso)))

    found, notes = trim_werus(f)
    both = [w for w, s in found.items() if len(s) >= 2]
    if not found:
        _report("Trim WERUs", "SKIPPED", "; ".join(notes) or "none found")
        _report("trim log scanned", "SKIPPED", "no WERU to scan")
        return _finish()
    _report("Trim WERUs agreed by both owner records",
            "PASS" if both else "FAIL",
            "weru(s) %s; weruDescriptor and WeruUsageLog agree on %d of %d"
            % (sorted(found), len(both), len(found)))

    log = TrimLog.scan(dev, f)
    if not log.entries:
        _report("trim log scanned", "SKIPPED",
                "no trim-log page decoded from %s" % sorted(found))
        return _finish()
    _report("trim log scanned", "PASS",
            "%d pages -> %d entries" % (log.pages, len(log.entries)))

    # every programmed page of the Trim WERU is a trim-log page
    cov = log.coverage
    ok = cov.get("foreign", 0) == 0 and cov.get("malformed", 0) == 0
    _report("every programmed page is a log page", "PASS" if ok else "FAIL",
            "%d read, %d foreign, %d malformed, %d erased"
            % (cov.get("pages_read", 0), cov.get("foreign", 0),
               cov.get("malformed", 0), cov.get("erased", 0)))

    # the mirror
    hist = collections.Counter(log.mirrors.values())
    ok = set(hist) == {2}
    _report("entries are mirrored 2x on one CE", "PASS" if ok else "FAIL",
            "copies per (ce,page): %s -- block 12 and block 13 of the same CE, "
            "byte-identical" % dict(sorted(hist.items())))

    # sentinel <-> msoGroupId, the firmware's own rule
    good = bad_ = 0
    for e in log.entries:
        g = mso_group_id(f.mso_table, e.mso_id)
        if g is UNKNOWN:
            continue
        want = SENTINEL_PARITY if g == PARITY_GROUP_ID else SENTINEL_DATA
        if e.sentinel == want:
            good += 1
        else:
            bad_ += 1
    if good + bad_ == 0:
        _report("sentinel matches msoGroupId", "SKIPPED", "no msoIdTable")
    else:
        _report("sentinel matches msoGroupId", "PASS" if not bad_ else "FAIL",
                "%d/%d obey `group==5 ? -4 : -2` (efc_cpu2.c:33515-33518)"
                % (good, good + bad_))

    # the type-dependent range units, on media
    al = mis = al_r = mis_r = 0
    for e in log.entries:
        g = granularity_for(f.mso_table, e.mso_id)
        if g is UNKNOWN:
            continue
        for first, count in e.ranges:
            aligned = (first % g == 0 and count % g == 0)
            if e.type == TRIM:
                al, mis = (al + 1, mis) if aligned else (al, mis + 1)
            else:
                al_r, mis_r = (al_r + 1, mis_r) if aligned else (al_r, mis_r + 1)
    if al + mis == 0:
        _report("type 0 ranges are LMAs", "SKIPPED", "no msoIdTable")
    else:
        _report("type 0 ranges are LMAs", "PASS" if mis == 0 else "FAIL",
                "%d/%d granularity-aligned, as Lut.c:1190/1191 requires"
                % (al, al + mis))
        _report("type 1/2 ranges are LUT indices",
                "PASS" if al_r == 0 and mis_r else "FAIL",
                "%d/%d are NOT granularity-aligned, so they cannot be LMAs "
                "(sub_8980 indexes lutTable + 4*first, efc_cpu2.c:23533)"
                % (mis_r, al_r + mis_r))

    # the sequence field is zero exactly for type 0
    z = [e for e in log.entries if e.sequence == 0]
    ok = all(e.type == TRIM for e in z) and all(
        e.sequence != 0 for e in log.entries if e.type != TRIM)
    _report("entry .sequence is 0 iff type 0", "PASS" if ok else "FAIL",
            "%d zero-sequence entries, all type 0 (efc_cpu2.c:33304-33307)"
            % len(z))

    # the trimmed LMAs against the group-5 LUT, with a control
    try:
        regs = [r for r in f.lut_regions(store_mso=40) if r.granularity == 4]
    except Exception:
        regs = []
    if not regs:
        _report("trimmed LMAs read as a LUT sentinel", "SKIPPED",
                "no granularity-4 LUT region readable from this backing")
    else:
        reg = regs[0]
        h = collections.Counter()
        idx = set()
        for _m, lma, count in log.trimmed_ranges():
            for i in range(lma >> 2, (lma + count) >> 2):
                if i >= reg.n_entries:
                    h["out of range"] += 1
                    continue
                idx.add(i)
                try:
                    h["%08X" % reg.entry(i)] += 1
                except Exception:
                    h["unreadable"] += 1
        n_un = h.get("%08X" % LUT_UNMAPPED, 0)
        tot = sum(h.values())
        _report("trimmed LMAs read as LUT 0xFFFFFFFF",
                "PASS" if tot and n_un >= tot - 8 else "FAIL",
                "%d/%d unmapped; the rest are LMAs rewritten since the trim"
                % (n_un, tot))
        ctl = collections.Counter()
        took = 0
        for i in range(0, reg.n_entries, 7):
            if i in idx:
                continue
            try:
                ctl["%08X" % reg.entry(i)] += 1
            except Exception:
                ctl["unreadable"] += 1
            took += 1
            if took >= 4000:
                break
        same = ctl.get("%08X" % LUT_UNMAPPED, 0)
        _report("LUT sentinel does NOT identify trim",
                "PASS" if same else "FAIL",
                "%d/%d NON-trimmed entries also read 0xFFFFFFFF -- the "
                "sentinel has no discriminative power" % (same, took))

    # three-way honesty
    trims = log.trims()
    if trims:
        m, lma, _c = log.trimmed_ranges()[0]
        v1 = log.is_trimmed(m, lma)
        v2 = log.is_trimmed(m, lma + 1 << 40)
        ok = v1 == TRIMMED and v2 is UNKNOWN
        _report("is_trimmed is three-way", "PASS" if ok else "FAIL",
                "named -> %s, unnamed -> %r (not False)" % (v1, v2))

    # ---- 3. the retirement watermark -------------------------------------
    wm = log.watermark
    if wm is None or wm is UNKNOWN or wm.weru_id is None:
        _report("retirement watermark recovered", "SKIPPED",
                "no LUT descriptor on this backing carries a snapshot record "
                "for msoId %s" % mso)
        _report("watermark agrees with validCount", "SKIPPED", "no watermark")
        _report("watermark agrees with the reservation accumulator", "SKIPPED",
                "no watermark")
    else:
        _report("retirement watermark recovered", "PASS",
                "weru %d, activation %d, wmaOffset %d; %d of %d LUT snapshot "
                "record(s) carry it (sub_4035B94E takes the minimum, "
                "efc_cpu2.c:139311)"
                % (wm.weru_id, wm.activation, wm.wma_offset,
                   len(wm.sources), len(wm.candidates)))

        c = log.completeness()
        # (a) validCount is derived from neither the watermark nor the scan
        live = c["live"] + c["unreadable_live"]
        vc = UNKNOWN
        try:
            decode_valid_count = _sib("ftl").decode_valid_count
            sec = f.sysmeta.get("validCount")
            tbl = decode_valid_count(sec.whole(fill=b"\x00"))
            vc = tbl.get(wm.weru_id) if isinstance(tbl, dict) \
                else tbl[wm.weru_id]
        except Exception as ex:
            _report("watermark agrees with validCount", "SKIPPED",
                    "validCount unreadable: %s" % ex)
        if vc is not UNKNOWN:
            _report("watermark agrees with validCount",
                    "PASS" if vc == live else "FAIL",
                    "validCount[%d] = %s and exactly %d entries sit above the "
                    "watermark (%d readable + %d on a dead CE) -- two "
                    "independent structures, same number"
                    % (wm.weru_id, vc, live, c["live"], c["unreadable_live"]))

        # (b) the last absorbed entry ends exactly on the watermark, and its
        #     reservation accumulator equals the descriptor's +232
        want = None
        for label, d in lut_descriptors(f)[0]:
            snap = decode_snapshot_list(d)
            if any(e["mso_id"] == mso and e["weru_id"] == wm.weru_id
                   and e["wma_offset"] == wm.wma_offset for e in snap):
                v = struct.unpack_from("<I", d, LUT_DESC_RESERVATION_OFF)[0]
                if v:
                    want = v
                    break
        under = [e for e in log.entries
                 if e.wma_offset is not None
                 and e.wma_offset <= wm.wma_offset and e.type != TRIM]
        last = under[-1] if under else None
        # Both bound together, both consumed together: the report below is
        # reached only when `last is not None`, which is exactly when these two
        # are set.  Pre-binding says so instead of leaving the correlation to be
        # rediscovered -- and if it were ever broken, the row would print "None"
        # rather than dying with a NameError inside a `%` tuple.
        end = copies = None
        if last is not None:
            # an entry occupies WMAS_PER_PAGE WMAs in each of its mirror
            # copies, and they are consecutive slots of the same page row
            copies = log.mirrors.get((last.placement[0], last.placement[2]), 1)
            end = last.wma_offset + WMAS_PER_PAGE * copies - 1
        if want is None or last is None:
            _report("watermark agrees with the reservation accumulator",
                    "SKIPPED", "no descriptor +232 value to compare against")
        else:
            _report("watermark agrees with the reservation accumulator",
                    "PASS" if (end == wm.wma_offset and last.sequence == want)
                    else "FAIL",
                    "the last absorbed entry's %d mirror copies end at "
                    "wmaOffset %d (watermark %d) and its +4 reads %d == the "
                    "same LUT descriptor's +232 (sub_A688, efc_cpu2.c:25401)"
                    % (copies, end, wm.wma_offset, last.sequence))

        # (c) the front of the WERU is intact
        _report("nothing erased off the front of the Trim WERU",
                "PASS" if c["starts_at_zero"] and not c["gaps"] else "FAIL",
                "surviving run reaches wmaOffset %d (< one page row) with %d "
                "gap(s) in page rows %d..%d, %.1f%% of the WERU used"
                % (c["span"][0], len(c["gaps"]), c["page_rows"][0],
                   c["page_rows"][1], 100.0 * c["used_fraction"]))

    # ---- 4. write order is wmaOffset, not page_sequence -------------------
    pools = collections.defaultdict(list)
    for e in log.entries:
        if e.wma_offset is not None and isinstance(e.page_sequence, int):
            pools[e.sentinel].append((e.wma_offset, e.page_sequence))
    inv_in = 0
    for v in pools.values():
        v.sort()
        inv_in += sum(1 for i in range(len(v) - 1) if v[i][1] > v[i + 1][1])
    allv = sorted(x for v in pools.values() for x in v)
    inv_all = sum(1 for i in range(len(allv) - 1)
                  if allv[i][1] > allv[i + 1][1])
    if not allv:
        _report("write order is wmaOffset, not page sequence", "SKIPPED",
                "no entry carries both a WMA and a page sequence")
    else:
        _report("write order is wmaOffset, not page sequence",
                "PASS" if (inv_in == 0 and inv_all > 0) else "FAIL",
                "0 inversions inside each of the %d sequence pools, %d when "
                "they are mixed -- WriteAuxiliaryList picks the pool from the "
                "sentinel (efc_cpu2.c:43112-43124)" % (len(pools), inv_all))

    print()
    print(log.summary())
    print()
    _report("RootStorage carries a Trim object", "SKIPPED",
            "there is none to find: the five SegmentedBuffer_Initialize call "
            "sites in this firmware are WeruUsageLog/LptNsid2/LptNsid3/PhBP/"
            "Test, and this board's catalogues carry only those tags "
            "(PhBP == 244 accounts for Backup objects 8..20).  An 8-bit name "
            "hash cannot prove a negative, so this stays SKIPPED rather than "
            "being reported as a PASS.")
    _report("WERU 21 inline-parity slot identified", "SKIPPED",
            "the aux parity flag is 100% on all ten readable slots of weru 21 "
            "(vs 100%/1-2% on the control weru 23), so it names no slot here, "
            "and weru 21 puts TWO of its twelve members on the dead CE0 "
            "against ONE parity relation.  Needs a board with a live CE0.")
    _report("size of the RETIRED history", "SKIPPED",
            "the watermark says this log is TRUNCATED and says exactly where "
            "(above), but HOW MUCH went before WERU 21's activation is not "
            "recoverable: the predecessor WERU was erased and re-owned, "
            "WeruUsageLog[21].origin is 0, and no lineage record points at it. "
            "Needs an image captured before that activation, not more analysis "
            "of this one.")
    return _finish()


def _finish():                                          # pragma: no cover
    n = collections.Counter(s for _n, s in _RESULTS)
    print()
    print("SELF-TEST: %d PASS, %d FAIL, %d SKIPPED"
          % (n["PASS"], n["FAIL"], n["SKIPPED"]))
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":                              # pragma: no cover
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest(sys.argv[1:]))
    print(__doc__)
