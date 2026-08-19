"""The 8-byte AU auxiliary -- every layout, every field.

    from titania import auxdata as aux
    rec = aux.parse(au.aux, aux.LAYOUT_INTERNAL_DATA)
    pg  = aux.parse_page([a.aux for a in page_aus], layout)
    lay = aux.layout_for(41, ftl)          # -> 0
    aux.read_is_plausible([a.aux for a in page_aus], lay)   # did it read?

"AUX must be four consecutive LMAs" is a layout-0/1/2 test -- use `read_is_plausible`
-------------------------------------------------------------------------------------
It is the crib that no wrong-mode read has ever passed, and it was applied to
every page regardless of layout.  A layout-3 (`cAuxLayoutForRaidParityMso`) page
has no LMA array to be consecutive, so every parity page on the drive was
rejected as a bad read -- 82 of them here, read 246 times with MCRC 4/4 every
time, then withheld.  Ask `msoDescriptor +17` first (`layout_for`), then apply
`read_is_plausible`, which carries the right rule for each layout and the
measurements behind it.

Every 4096-byte AU carries 8 bytes of metadata that the media format keeps
outside the user payload.  They are the toolkit's only self-check: `aux LMA ==
the LMA we looked up` is what proved the LUT -> WMA -> physical chain, and the
sequence number is what decides which of two copies of an LMA is the live one.
Reading a field with the wrong layout's rule therefore does not produce a
detectable error -- it produces a plausible wrong answer -- so the layout has to
come from the msoDescriptor and never from a guess.

THE SELECTOR
------------
`Mso_GetAuxLayout(msoId)` is msoDescriptor byte +17, a 28-byte-stride array
(`efc_cpu2.c:43002`, `:133444-133446`, `:134109-134112`, `:136804-136808`).
Four values exist and `WriteAuxiliaryList_InsertRebuildInformation`'s switch
(`sub_1C438`, `efc_cpu2.c:43006`) panics `"HALT()", "WriteAuxiliaryList.c", 434`
on anything else (`:43106`) -- so the set below is closed, not merely observed:

    0  cAuxLayoutForInternalData    the `default:` arm of the switch.  Named by
                                    the Moh.c panic `"auxLayout ==
                                    cAuxLayoutForInternalData"` guarding the arm
                                    that reads the LMA out of aux +0
                                    (`efc_cpu2.c:35423-35424`, Moh.c:4183): the
                                    test there is `if (auxLayout) panic`, so the
                                    constant is zero.
    1  (unnamed in this image)      `case 1:` at `:43008`.  No `cAuxLayout*`
                                    string in efc_cpu0/1/2/3.c names it; see
                                    what is still UNKNOWN.
    2  cAuxLayoutForForNSID23       `case 2:` at `:43052`.  Pinned numerically by
                                    `if (msoDescriptor[+17] != 2) panic
                                    "(Mso_GetAuxLayout(pEntry->msoId) ==
                                    cAuxLayoutForForNSID23) || ..."`
                                    (`efc_cpu2.c:136804-136808`, RebuildNeo.c:750).
    3  cAuxLayoutForRaidParityMso   `case 3:` at `:43091`, whose printf is
                                    "unsupported aux layout
                                    cAuxLayoutForRaidParityMso" (`:43096`).
                                    Also `if (msoDescriptor[+17] == 3) panic
                                    "Mso_GetAuxLayout(msoId) !=
                                    cAuxLayoutForRaidParityMso"` (`:133445`).

THE ONE STRUCTURAL FACT THAT ORGANISES ALL FOUR
------------------------------------------------
The sequence number is a PAGE field, not an AU field.  Every layout stamps
one 62/64-bit counter value across the four aux entries of a page.  The write
loop advances by `auPerPage` -- `byte_80D855 = byte_80D856 = dword_90C4C0 = 4`
(`efc_cpu2.c:162531-162532`, passed at `:38817`) -- so one counter value covers
four AUs: `v += a4` at `:43047` (layout 1), `:43069` (2), `:43152` (0) and
`:43211` (3).  The pool is then advanced by one at `:43038` (1), `:43143` (0) and
`:43203` (3) -- and not at all in layout 2.  That is why `parse()` can only
hand back this AU's *shard* and why `parse_page()` exists.

This RETRACTS the module-level model in `ftl.py` -- "AU1 additionally carries the
page sequence number in bytes 4..6 (u24)" (`AUX_SEQ_OFF, AUX_SEQ_LEN = 4, 3`).
AU1 does carry the low part, and on this board the counters are small enough
(3 862 740 is the largest, msoGroupId 1) that only 22 of its 31 bits are in use,
so a u24 read has never yet been wrong.  It is still a truncation: the field is
31 bits in AU1 plus 31 more in AU0, and it silently starts losing the top of
the counter at 2**24 -- 23 % of the way there already.

THE LAYOUTS, FIELD BY FIELD
---------------------------
Offsets marked (W) come from the writer's own byte arithmetic, (R) from an
independent reader, (S) from the decompiler naming the struct member.  Nothing
here is inferred from media alone.

layout 0 `cAuxLayoutForInternalData` and layout 1 -- identical on media.

        +0  u32   lma                                    (W `:42834`) (R `:35425`)
        +4  u31   sequence shard, bits 30..0             (W `:43031`) (R `:134227`)
        +7  bit7  inline-raid PARITY, sticky             (W `:42942`)

    per page, over the four AUs:
        AU0 shard = seq >> 31        AU1 shard = seq & 0x7FFFFFFF
        AU2 shard = 0                AU3 shard = 0

    The writer is `sub_1C438` case `default` (`:43125-43135`) / `case 1`
    (`:43030-43035`); the reader is Rebuild's `sub_4035684E`
    (`efc_cpu2.c:134226-134229`), which reassembles exactly
    ``((aux0.dw1 & 0x7FFFFFFF) << 31) | (aux1.dw1 & 0x7FFFFFFF)`` -- writer and
    reader agree bit for bit, which is what makes this a decode and not a fit.

    Bit 31 of the second dword (= byte +7 bit 7) is masked out of every
    read-modify-write of that word (`& 0x80000000 |`, `:43031`, `:43033`,
    `:43135`, `:43137`) and stripped by the readers (`& 0x7FFFFFFF`), so it is a
    sticky per-AU flag that survives rewrites.  `sub_1C3CC` (`:42935-42957`,
    panic `"(3 + auPerPage * (pageCount - 1)) < cMaxAusPerNandCommand",
    "WriteAuxiliaryList.c", 378`) is what sets it, on all four AUs of the pages
    it selects (`v7[7] |= 0x80` at +7/+15/+23/+31, `:42953-42956`).

    The two LAYOUTS differ only in what happens to an UNMAPPED AU.  Layout 1
    tests `aux.lma == 0xFFFFFFFF` first (`:43018`) and, if so, zeroes the shard
    on all four AUs *without* consuming a sequence number (`:43020-43026`);
    layout 0 has no such test and stamps every page (`:43125-43137`).  Layout 0
    additionally re-homes the descriptor it reads for `msoType == 6`
    (PhysicalMediaAccess) using AU0's LMA as a tag: `0xFFFFFFFE` selects
    `byte_900001` and `0xFFFFFFFC` selects `byte_900005` (== `Mso_GetMsoId(
    UserData, ParityData)` == 34), and anything else panics (`"halt()",
    "WriteAuxiliaryList.c", 279`, `:43109-43122`).  So on such a page **AU0's
    LMA is not an address at all -- it is a two-valued msoId redirect.**

    On media: blocks 12 and 13, two independent WERUs (18 and 21), carry 200
    such pages each, split 186 x `0xFFFFFFFE` and 14 x `0xFFFFFFFC`, with AU1,
    AU2 and AU3 all `0xFFFFFFFF` and an ordinary sequence number in AU1.  No
    third value appears in 400 pages.  This is the trap in `page_is_torn`:
    those 400 pages have AU0 and AU1 disagreeing on the invalid-LMA test and are
    not torn writes at all, which is why the torn rule demands a real address on
    one side.  Taking the XOR naively reports 400 false positives here.

layout 2 `cAuxLayoutForForNSID23`.

        +0  u32   lma                                    (R `:137341`)
        +4  u16   lpId, the logical-partition id         (W `:43061`) (S `:137378`)
        +6  u16   sequence shard                         (W `:43065`)

    per page, over the four AUs, BIG-ENDIAN in the AU index:
        AU0 = seq[63:48]   AU1 = seq[47:32]   AU2 = seq[31:16]   AU3 = seq[15:0]

    The writer (`:43065-43071`) is ambiguous on its own: IDA renders AU0's source
    as `HIWORD(v33)`, which its `defs.h` makes identical to the `WORD1(v33)`
    written to AU2.  The reader settles it -- `OnSlcCacheReadAuxDone`
    (`efc_cpu2.c:137337-137342`) builds
    ``hi = (aux0@6 << 16) | aux1@6 ; lo = (aux2@6 << 16) | aux3@6``
    and pairs them, so AU0 holds the most significant word.  The struct member
    name `pAuxiliaryBuffer[bufferindex].lpId` in the same function's error string
    (`:137377`) is what names the +4 field.

    The shard order is not testable on this board and is not claimed as
    measured.  Every layout-2 sequence here is below 65536, so AU0/AU1/AU2's
    shards are all zero on all 11 clean pages and only AU3's position is
    demonstrated.  The order above rests on the reader alone.  `selftest` reports
    this SKIPPED rather than PASS.

    Layout 2 is the only layout with no room for the parity flag: +7 is the
    top half of the sequence shard.  That collision is real, not avoided --
    `sub_1C3CC`'s gate is `pNandCommand->raidOpCode & 0xF == 2`, not the raid
    type, and its two callers cover cRaidTypeMso as well as cRaidTypeInline
    (`efc_cpu2.c:37515`, `:38735`), so msoIds 51/53 (cRaidTypeMso, layout 2) can
    receive it.  It is simply invisible here: the marker lands on the stripe's
    column 0, which is CE0, which is dead.  See the layout-3 section, and
    `selftest`, which now states this the right way round.

    Layout 2 does not increment the sequence pool (`:43058` reads it; there
    is no `++` anywhere in `case 2`), unlike layouts 0/1 (`:43038`, `:43143`)
    and layout 3 (`:43203`).  So a layout-2 sequence is not a per-page counter
    and must not be used for newest-wins ranking the way a layout-0/1 one is:
    it names the write epoch, and every page of one epoch carries the same
    number.  Measured on this board -- msoId 51's eight clean pages all read
    2812, msoId 53's three all read 2340, and those are exactly
    `rebuildSequenceNumberPool[3].next` and `[2].next`.  Two msoIds, two
    different groups, exact equality both times.

layout 3 `cAuxLayoutForRaidParityMso` -- a PAGE-level record, not an AU one.

        AU2 +4  u25 (bits 31..7)  stripeId               (W `:43197`) (R `:134170`)
        AU2 +4  u7  (bits  6..0)  sequence bits 38..32   (W `:43198-43199`)
        AU3 +4  u32              sequence bits 31..0     (W `:43200`)
                                 -- so the page sequence is `(hi7 << 32) | lo32`,
                                 settled from the instructions; see the
                                 "Seven bits" section at the end of this
                                 docstring.  The firmware's own reader
                                 reassembles them at bit 31 and is wrong above
                                 2**32.
        AU0 +7  bit7             RAID PARITY marker, as an XOR  -- see below
        AU1 +7  bit7             the same bit, same value
        everything else          not stamped here        -- see below

    Written by `sub_1C8D8` (`efc_cpu2.c:43164`), read by Rebuild
    (`efc_cpu2.c:134168-134192`).  `0x1FFFFFF` in the stripeId field is INVALID,
    and invalidating a parity AU is `*p |= 0xFFFFFF80` (`:133787`) -- exactly
    "set that 25-bit field to all ones and leave the low 7 alone".

    `titania.raid` owns the stripeId's meaning and its placement proof; this
    module owns only the bit layout, and the two agree
    (`raid.PARITY_AUX_STRIPE_AU == 2`, `PARITY_AUX_STRIPE_SHIFT == 7`).

    **AU0/AU1's second dword and all four first dwords are not written by the
    aux writer at all** on this path.  MOH copies the whole 8 bytes of every AU
    straight out of the caller's parity AUX buffer (`*v69 = *v71; v69[1] =
    v71[1]`, `efc_cpu2.c:37438-37441`) -- the DRAM buffer the ECU XOR-accumulates
    the stripe into (`Moh.c:2364` asserts `raidParityAuxBuffer != NULL`,
    `efc_cpu2.c:38727`) -- and only then does `sub_1C8D8` overwrite AU2's and
    AU3's second dword.  So those bytes are whatever the accumulator left there.
    This module reports them as UNKNOWN and does not name them.

    On media, over 82 clean pages of msoIds 34, 35 and 54 in 11 WERUs:

      * 16 carry a real stripeId and 66 carry `INVALID_STRIPE_ID`.
      * The undecoded carry and the stripeId agree perfectly, 82 of 82:
        every page with a real stripeId has `+0 == 0` on all four AUs, and every
        page with `INVALID_STRIPE_ID` has pseudo-random `+0` on all four.  No
        exceptions.  The mechanism is now traced -- see the next section.
      * `0x80000000` in AU0/AU1's second dword -- the thing `titania.raid`
        recorded from `n == 1` and warned not to lean on -- reads exactly that on
        9 of the 16 real-stripeId pages and on 1 of msoId 54's 8.
        Partly RETRACTED 2026-08-06: "it is not a rule" was measured on the
        wrong predicate.  What is not a rule is *exact equality*; the flag bit
        is one.  Bit 31 is set on AU0's and AU1's second dword on 32 of 32
        (16 pages x 2), and the seven "exceptions" carry extra low bits
        (`0x50 0x60 0x70 0xa0 0xd0 0x1f0 0x1ff0`), not a cleared flag.  AU0's is
        `0x80000000` with nothing else on 16 of 16.  Solved 2026-08-06: it is
        the RAID parity marker, arriving as an XOR -- see the next section but
        one.
      * AU2's low 7 bits are zero on 82 of 82, so the firmware's reader bug
        below is unobservable on this board.
      * 82 distinct sequences in [312930, 370354], all strictly below
        `rebuildSequenceNumberPool[5].next == 377660`, and group 5 is exactly the
        msoGroup this layout is confined to.

    The whole layout is confined to one msoGroup: MOH_Write panics
    `"Mso_GetMsoGroupId(pRequest->msoId) == 5"` (`Moh.c:658`,
    `efc_cpu2.c:37399-37401`) before taking this path, and group 5 is
    `cCoreMsoGroupParityData`.  The sequence bound above is that assertion
    checked against media.

Why `+0 == 0` and a real stripeId are the same fact -- traced 2026-08-06
------------------------------------------------------------------------
The 82-of-82 correlation above is not a coincidence and not a checksum: the
carried bytes and the stripeId are two readouts of one condition, and the
firmware writes them from two fields of the same request, gated on the same
count.  `ps_task2.py` / `ps_task2b.py` are the measurements.

The assembly, in order (both inside MOH_Write's layout-3 arm):

  1. For `i < pRequest->count`, MOH copies all 8 aux bytes of AU `i` verbatim
     from `pRequest->pAuxBuffer[addressOffset + i]` -- the DRAM parity AUX buffer
     the stripe XOR-accumulates into (`efc_cpu2.c:37432-37441`; the buffer pair
     is asserted non-NULL at `Moh.c:2363-2364`, `efc_cpu2.c:38726-38732`).  For
     `i >= count` it pads with `dw0 = 0xFFFFFFFF, dw1 = 0` (`:37443-37450`).
  2. `sub_1C8D8` then stamps AU2's second dword with
     `(old & 0x7F) | (v << 7)` where `v = pLmaList[addressOffset + 1 + i]` if
     `i < count` and `v = -1` otherwise (`efc_cpu2.c:43186-43196`).
     `-1 << 7` is `0xFFFFFF80`, i.e. exactly INVALID_STRIPE_ID in the 25-bit
     field.
  3. `pLmaList` is the ParityManager's stripeId list, and the only thing that
     fills it is `sub_12D58` (`efc_cpu2.c:33917`, fill loop `:33975-33980`),
     reached from `sub_D10C` (`Allocation.c:4127`, `efc_cpu2.c:27990`) only when
     `Mso_GetRaidType(msoId) == cRaidTypeMso` and only when
     `pCompletionInfo[2] & 8` is set.  The same allocation is what binds the
     parity AU and AUX buffers to the stripe
     (`ParityManager_OnAllocationRequest`, `efc_cpu2.c:33659`).

    So a parity AU whose buffer was never bound to a stripe has no stripeId to
    stamp and an accumulator nothing was accumulated into.  One cause, two
    symptoms.  That is the correlation.

The count is not the cause -- checked on media, not assumed.  Step 2's `-1`
also fires when `count == 0`; but step 1 would then have padded, and the carried
`dw0` would read `0xFFFFFFFF`.  It never does: 0 of the 66 INVALID pages has a
carried dword of `0xFFFFFFFF`, so `count == 4` on all of them and the INVALID
comes from the list entry, not from a short write.

The `+0 == 0` half: a closed accumulator, not a cleared buffer.  All four
first dwords read exactly zero on 16 of 16 real-stripeId pages -- consistent with
`titania.raid`'s independent measurement that the XOR of a stripe's data auxes is
zero at every AU index.  What rules out "the buffer was just memset" is the
residue: 7 of the 16 carry a small non-zero value in AU1's second dword
(`0x50 0x60 0x70 0xa0 0xd0 0x1f0 0x1ff0`, all on msoId 54's weru 576).  A cleared
buffer cannot produce those; an XOR whose members almost but not quite cancel can.

The pseudo-random half: Not a partial XOR -- it is unrelated content.  Over
the 66 INVALID pages there are 396 carried dwords (AU0/AU1 both dwords, AU2's and
AU3's first).  They are all 396 distinct, mean popcount 16.17, 215 with bit
31 set and 376 of 396 at or above 228.  Against that, the members: over 540
AUs of msoIds 43/45 (layout 1) and 51/53 (layout 2), the OR of *every* first
dword is `0x0a7fbfff` / `0x0000067f` and of *every* second dword is `0x003afeff`
/ `0x0bfc000f`.  **No member aux dword on this drive has bit 31 set, so no XOR of
them can -- yet 215 of 396 do.  The carried bytes on an INVALID page are
therefore not a partial stripe XOR at all; they are whatever the buffer held.

What this buys: a free completeness test for a parity AU.

        all four carried first dwords == 0   <=>   this parity AU's stripe was
                                                   accumulated and has an address

    82 of 82 on this board (16 / 66).  It needs no LUT lookup, no other page and
    no key, so a reconstructor can reject an unusable parity AU from the AU
    itself.  `parity_stripe_complete` is that test.

LAYOUT-3 BIT 31 IS THE RAID PARITY MARKER, XOR-ED IN -- SOLVED 2026-08-06
--------------------------------------------------------------------------
Bit 31 of AU0's and AU1's second dword -- set on 32 of 32 real-stripeId dwords --
is the same bit as `AUX_FLAG_INLINE_PARITY`, byte +7 bit 7, and it reaches the
parity AU through the ECU RAID accumulator rather than through a writer.  It is
not `sub_1C3CC` firing on the parity page itself; that earlier reading was
right to rule the direct OR out and wrong to stop there.

The ECU RAID model, which is what closes it.  `ECC_RAID_EN` (`0xF200C0D0`
bit 0, `RegEcu.h:623`) is set unconditionally at ECU init to `ECU_RAID_ENABLED`
(`HalEcu.c:160`, `:253`; retunable through `ECU_CFN_RAID`, `:418`).  The engine
owns 26 SRAM RAID buffers (`allocatedSramBufferId < cMaxSramRaidBufferId`,
`MioRaidControl.c:660`, `efc_cpu1.c:14454`; the busy bitmap is 26 bits wide,
`~dword_800F1C & 0x3FFFFFF`).  Each buffer holds one in-flight parity AU, and the
unit it accumulates is the format-7/8 message: 4096 user bytes plus the 8 aux
bytes -- which is exactly why an aux bit can be an XOR product at all.

Descriptor dw5 carries four `(RAID_ID, RAID_CMD)` pairs (`HalDescriptor.h:126-134`),
one per AU of a page, so the four AU indices of a page accumulate into four
buffers in parallel -- the four parallel stripes `titania.raid` measures.
`sub_10BF4` (`HalMioWrite.c`, `efc_cpu1.c:20814`) emits, per subpage:

    RAID_CMD3 = 0x8   the DATA descriptor, ordinary AU format, AU_SEG = seg(0,ce)
                      -- accumulate this AU into the buffer      (`:20869`)
    RAID_CMD3 = 0xB   emitted iff `mioRaid[subPage].dramSwapOut`, AU format
                      8-if-flags&0x10-else-7, AU_SEG = seg(1,ce), DRAM address
                      `dword_8013DC + word_8013D4 * mioRaid[subPage][22]`
                      -- evict the buffer to its DRAM home       (`:20885`)
    RAID_CMD3 = 0xC   emitted iff `mioRaid[subPage].dramSwapIn`, AU format
                      `field_1D - 24 + 7`, AU_SEG = seg(0,ce), DRAM index [21]
                      -- reload a partial accumulation from DRAM (`:20927`)

The three flag bits are named by MioRaidControl's own asserts:
bit 1 = `dramSwapIn` (`"pCmdSubInfo->mioRaid[pageCount].dramSwapIn == false"`,
`MioRaidControl.c:457`), bit 2 = `dramSwapOut` (`:456`) and bit 3 =
`sramReset` (`"pInfo->mioRaid[subPage].sramReset == false"`, `HalMioWrite.c:1592`).
`sub_942A` (`efc_cpu1.c:14222`) is the allocator: `byte_800EB0[2*logicalId]` holds
`[7:6]` residency (0 absent / 1 in SRAM / 2 parked in DRAM) and `[5:0]` the AUs
accumulated so far, `dword_800F28[]` is an LRU clock, and eviction sets
`v6[18] |= 4` on the victim while a reload sets `|= 2`.  So the accumulator's
state is *(residency, accumulated-AU count, SRAM buffer id, DRAM home)* and 0xB /
0xC are purely SRAM<->DRAM traffic -- which is why no AU of format 7 or 8 is
ever on media and why the parity AU that is written out is an ordinary
format-A AU.

Who sets the bit, and on what.  `sub_1C3CC` (`efc_cpu2.c:42935-42957`) ORs
`0x80` into aux bytes 7/15/23/31 -- all four AUs of the pages it selects, one per
`planes` pages of the command.  Its two call sites are both in MOH's write path
and neither is gated on cRaidTypeInline (this RETRACTS the note that they
are):

    :37515   MOH_Write LABEL_122, gated on `Mso_GetRaidType(msoId) not in (0, 3)`
             -- i.e. cRaidTypeMso OR cRaidTypeInline -- and on
             `pNandCommand->raidOpCode (+22 & 0xF) == 2`
    :38735   inside `if (Mso_GetRaidType(pMediaCommand->msoId) == 1)`, i.e.
             cRaidTypeMso explicitly, same raidOpCode gate; the `else` arm
             panics `"Mso_GetRaidType(pMediaCommand->msoId) == cRaidTypeNone"`

A ParityData MSO reads raidType 0, so no path can OR the marker onto a parity
write, and `sub_1C3CC` is the only writer of that bit anywhere in
efc_cpu0/1/2/3.c.  The marker therefore only ever exists on a DATA AU -- and the
accumulator XORs it along with the other 4103 bytes.

    parity AU's byte-7 bit-7  ==  XOR over the stripe's data members of theirs
                              ==  1 iff an odd number of members carries it

MEASURED, inline mode, where all twelve members are readable (`pq_bit31.py`,
werus 14/19/20/22/23, 847 pages with all twelve slots MCRC-clean):

    slot 0 (column 0)   flagged on 847 / 847 pages where it is readable
    slots 1..10          1-6 of 368 each
    slot 11 (parity)    flagged on 100 % of every readable block
    #flagged among the eleven data slots is odd on 847 / 847  (1, 3, 5, 7, 9)
    parity slot's flag == XOR of the eleven data slots'  -- 847 / 847, 0 wrong

The twelve-way XOR of the raw stored page is exactly zero (`titania.raid`), so the
parity block's bit is *arithmetically forced* to be that XOR: if it were an
independent OR the relation would fail in byte 7 and it does not.  This also
closes `titania.raid`'s "two slots are flagged although only one XOR relation
exists, and that discrepancy is UNRESOLVED": there is one marker per stripe, it
is written on a DATA member, and the parity member's copy is the accumulator's
output.  Slot 11 is not a second marker and never was.

And the same engine gives layout 3 its bit 31.  Three quantitative
predictions, all measured (`pq_bit31b.py`), with the unaccumulated pages as the
control the correlation needs:

  1. AU0's second dword can only be 0 or `0x80000000`.  Every member's AU0
     second dword is `(seq >> 31) | marker<<31` (layout 1) or
     `lpId | shard<<16 | marker<<31` (layout 2), and on this board every counter
     is below 231 and a stripe's members share one lpId -- so everything but
     bit 31 cancels.  **16 of 16 accumulated pages read exactly `0x80000000` and
     nothing else; 0 of the 66 unaccumulated pages lands in {0, 0x80000000}.
  2. AU0's bit 31 must equal AU1's, because they are XORs over the same 48
     member pages and `sub_1C3CC` flags all four AUs of a page together.
     16 of 16 agree on accumulated pages; 38 of 66 on unaccumulated ones --
     chance, which is what makes the 16/16 mean something.
  3. AU1's surviving low bits are then the XOR of the members' sequence shards.
     They are 0 on 9 of 16 and a multiple of 16 on 7 of 7 of the rest
     (`0x50 0x60 0x70 0xa0 0xd0 0x1f0 0x1ff0`) -- which is what a XOR over 48
     consecutively-numbered pages must give, since each low nibble then occurs
     three times and `XOR(0..15) == 0`.  Four bits x 7 pages agreeing by accident
     is 1 in 2**28.

Media cannot show the flagged member itself on this board and that is stated, not
hidden: an Mso stripe's slot 0 is `4*ce + q` with ce 0, and CE0 is dead.  Over
191 clean data pages of cRaidTypeMso WERUs (columns 1..47) and 23 layout-2 pages,
0 carry the marker -- consistent with the inline picture, where slots 1..10
carry it on ~1 % of pages, but a one-sided check.  The load-bearing evidence is
the inline measurement plus the three predictions above.

    NOTE for layout 2.  Because `sub_1C3CC` fires for cRaidTypeMso, and msoIds
    51/53 are cRaidTypeMso with auxLayout 2, the marker can land on a layout-2
    aux -- where byte +7 is the top byte of AU0's u16 sequence shard, so it reads
    as `shard | 0x8000` and not as a flag.  Nothing on this board shows it (no
    column 0 is readable), so `parse` does not special-case it; it is recorded
    here so that a future board's `shard == 0x8000` is not mistaken for a decode
    failure.  This RETRACTS "layout 2 has no room for the parity flag ... that is
    consistent rather than lucky": it has no room, and the collision is real, it
    just cannot be seen here.

THE SENTINELS
-------------
`0xFFFFFFFF` is the invalid LMA everywhere: it is what the pad entries of a
short write get (`sub_1C238`, `:42856-42860`), what the LUT hands back for an
unmapped granule alongside `0xFFFFFFFD` and `0xFFFFFFFC` (`:23080`, `:23114`,
`:33634`, `:110759`, `:144613`), and what invalidating a layout-0/1 AU writes
(`:133792`).  A layout-0/1 page is refused outright by the rebuild if AU0's or
AU1's LMA is `0xFFFFFFFF` (`:134231-134235`) -- see `page_is_torn`.

`0xFFFFFFFE` and `0xFFFFFFFC` are the msoType-6 redirect tags described above and
are also the two values the rebuild's scan special-cases (`:134607`, `:134619`).
`0xFFFFFFFB` is a real value on this board and is not any of these: one page,
`ce10/blk21/page9` in WERU 22 (msoId 40, the LUT store), carries it on all four
AUs with an ordinary sequence number in AU1 and the inline-parity flag set --
so it is neither a torn write nor a bad decode, it is a deliberate marker.  No
site in `efc_cpu0/1/2/3.c` reads or writes it.  `n == 1` here (a second sighting,
in WERU 14, is recorded at `_wma_ref.py:83`), so this module reports it as
UNKNOWN rather than inventing a meaning.  RootStorage's own AUs carry
`0xFFFFFFFF` because they are not LMA-addressed at all (`ftl.ROOTSTORAGE_LMA`);
measured here, 3920 of 3920 AUs of msoId 56.

WHAT THIS BUYS THE TOOLKIT
--------------------------
Five checks that were not available before, all free once a page is decoded:

  1. A 62-bit sequence instead of a 24-bit one (`page_sequence`).  The u24
     read is already lossy in principle and will become lossy in fact; and AU0's
     shard is a second, independent witness that a page decoded correctly -- it
     is zero on all 5723 clean pages measured, so a non-zero one is a red flag
     today and a real value later.
  2. Torn-page detection (`page_is_torn`): one of AU0/AU1 carries a real
     address and the other the invalid marker.  This is not an MCRC failure --
     MCRC is per AU over the stored bytes and passes on a page whose four AUs
     were written in different commands.  On this board the count is **0 of 5723
     clean pages**, which is a real negative for the board and not a weakness of
     the test: the same rule fires 400 times if you forget the msoType-6 tags.
  3. A sequence-continuity check (`sequence_gaps`).  The rebuild advances its
     expectation by exactly one per record and logs an event when the next number
     is not the one it wanted (`v9 = last + 1; if (v9 == seq) last = v9;`, then a
     debug record, `efc_cpu2.c:134609-134637`).  A gap over pages we hold means a
     page between them is missing or misdecoded -- a *migration in flight* or a
     hole in the dump, distinguishable from ordinary staleness.
  4. A pool bound (`sequence_is_plausible`).  A one-line falsifier for a
     misdecoded aux, and cross-layer: the bound comes from SystemMetadata and the
     value from the page.  Measured -- msoId 45's largest is 3 852 847 against
     `pool[1].next == 3 862 740`.
  5. A raidType cross-check.  The `+7` bit-7 marker can only be set by
     `sub_1C3CC`, whose callers require `Mso_GetRaidType(msoId) != cRaidTypeNone`
     -- so it can never appear on an MSO whose descriptor `+16` reads 0.
     Measured over 5632 layout-0 pages: 745 flagged, every one of them on
     msoIds 40/41/42 (raidType 2) and none of the 2992 pages of msoIds 56/57
     (raidType 0).  Two independent descriptor bytes and a media bit, agreeing
     with no exceptions.
     CORRECTED 2026-08-06: the converse does not hold and used to be claimed
     here.  raidType 1 (cRaidTypeMso) MSOs can carry it too -- call site
     `efc_cpu2.c:38735` is inside `if (Mso_GetRaidType == 1)`.  None of msoIds
     43/45/51/53's pages here shows it only because every stripe column this dump
     holds is >= 1 and the marker sits at column 0, on the dead CE0.

  6. A second, independent completeness witness for a parity AU
     (`parity_marker`).  On layout 3, AU0's and AU1's bit 31 is the XOR image of
     the same marker; on an accumulated stripe both are set and AU0's dword is
     exactly `0x80000000`.  It agrees with `parity_stripe_complete` on every
     clean page here and is derived from a different four bytes of the same aux,
     so a page that satisfies one and not the other is a decode failure.

MEASURED on this board (`pn_auxcensus.py`, `pn_auxstats.py`, `pn_auxtorn.py`)
-----------------------------------------------------------------------------
CE0 excluded throughout: it is dead and answers, so its pages are neither
programmed nor erased.  "clean" means all four AUs pass MCRC.

    layout 0   5632 clean pages, msoIds 40/41/42/56/57, 17 WERUs
    layout 1     80 clean pages, msoIds 43/45, WERUs 84 / 234 / 573 / ...
    layout 2     11 clean pages, msoIds 51/53
    layout 3     82 clean pages, msoIds 34/35/54  (see the caveat below)

Every one of those pages re-encodes byte-identically from the decoded fields
(`encode_page(parse_page(x)) == x` over all 32 aux bytes; 5723 of 5723 for
layouts 0/1/2).  Layout 3 is excluded from that claim's force because its
undecoded bytes are carried through verbatim, so its re-encode proves nothing.

RETRACTED, and worth the warning.  An earlier pass of this work reported
layout 3 as "1 clean page of 83 held; msoIds 35 and 54 are a real negative".
Both halves are withdrawn.  The store held 83 pages of those WERUs at 18:29 and
164 at 19:44 -- `<store>/index.tix` was rebuilt at 19:19 by another session and
the re-ingest added the native-TLC reads of the parity blocks.  Same WERUs, same
msoIds, twice the pages, and 82 of them decode.

The lesson is not "wait longer".  It is that a real negative that rests on
another layer's index is only as good as that index's revision, and this one
did not carry a timestamp with it.  Where a negative is stated below it names
what it rests on.

A second, independent sampling bug was found and fixed at the same time: the
first `--selftest --pages 900` run walked the store in address order and spent
the whole layout-0 budget inside msoIds 56 and 57 -- which carry no addresses
and no parity flags -- then reported a clean PASS having never looked at 40, 41
or 42.  `_media_census` now gives every (ce, block) group an equal share.  A
budget that samples one corner is worse than no budget, because it looks like
coverage.

Layout 2 is the one still thin: 11 of 112, and the other 101 are mode-mismatched
reads that no sampling change will rescue.

WHAT IS STILL UNKNOWN, AND SAID SO
-----------------------------------
* Layout 1 has no name in this firmware image.  All four decompiled CPUs
  contain exactly three `cAuxLayout*` strings and none of them is layout 1.  Its
  msoIds on this board -- 32, 36, 37, 43, 45, 49, 52 -- are exactly the seven
  whose `msoDescriptor +3` lutId is 1, and layout 2's 42 msoIds are exactly the
  lutId-2 and lutId-3 sets.  Tempting, but it is not a rule and this module does
  not encode it: layout 3 already breaks it.  Four of its five msoIds are the
  lutId-4 set and the fifth, msoId 62, has `lutId == 0xFF`, no LUT at all.
  Layout 0's lutIds are a mixture (0, 5 and 0xFF).  `LAYOUT_NAMES[1]` stays None
  rather than becoming a plausible guess.
* Layout 3's first dword, and AU0/AU1's second dword.  Solved
  2026-08-07 -- see "the carried dwords" below.  They are the XOR of the
  stripe's members' aux, they are *predictable from the members*, and both are
  now derived rather than described.
* `0xFFFFFFFB` as an aux LMA.  One page, four AUs, no firmware site.
* What NAND-command bit `+21` bit 7 does.  Two independent sites set it for
  auxLayout 1 and 2 and clear it for 0 and 3 (`efc_cpu2.c:37190-37197`,
  `:38563-38570`).  It is off-media and no consumer of it was found, so it is
  recorded as a fourth, non-media difference between the layouts and nothing more.

THE CARRIED DWORDS: SOLVED 2026-08-07 -- THEY ARE PREDICTABLE FROM THE MEMBERS
------------------------------------------------------------------------------
`rj_carry.py` is the measurement.  MOH copies all eight aux bytes of every AU
verbatim out of the parity AUX DRAM buffer the ECU XOR-accumulates the stripe
into, and only then does `sub_1C8D8` overwrite AU2's and AU3's second dword.  So
six of the eight dwords are the member XOR, and a cRaidTypeMso stripe's
members are a known set: all 48 columns of one page of one data WERU at one AU
index, `column = (wma // 4) % 48`, `slot = 4*ce + q`.

The first dword is the members' LMAs xor-ed, and it is forced to ZERO.  The
four columns of one CE are the WMAs `base + 16*ce .. +15` -- a 16-aligned WMA
granule -- and a lutId-1/2/3 LUT granule is 16 LMAs from a 16-aligned base.  So
those four members carry `G+k, G+4+k, G+8+k, G+12+k` at AU index `k`, four
values differing only in bits 2 and 3, each of which appears exactly twice:

        XOR over one CE's four columns == 0,  whatever G is

so all twelve CEs do, so the carried dw0 is 0 on all four AUs of any complete
stripe -- an identity, not a coincidence, and it needs nothing about the four
columns on the dead CE0 beyond their being a granule.  This is what
`parity_stripe_complete` has been testing all along: a non-zero carried dw0
cannot come from a closed stripe, so it means the buffer was never accumulated.
That function is now derived rather than fitted.
The pad AU `ff ff ff ff 00 00 00 00` (`sub_1C238`, `:42856-42860`) obeys no LMA
law but comes four to a CE, and four copies of `0xFFFFFFFF` also XOR to zero --
measured on WERU 590 page 37, whose whole CE10 column group is padded.

AU0'S AND AU1'S SECOND DWORD ARE THE MEMBERS' SEQUENCE SHARDS XOR-ED.  A
layout-1 member's dw1 is `seq_shard | marker<<31`, with AU0's shard `seq >> 31`
and AU1's `seq & 0x7FFFFFFF`.  Every counter on this board is below 2**31, so

    parity AU0 dw1 == markerXOR << 31 == 0x80000000            exactly
    parity AU1 dw1 == XOR of the 48 members' page sequences
                      | markerXOR << 31

A layout-2 member's dw1 is `lpId | shard<<16` with AU0's and AU1's shards
`seq>>48` and `seq>>32`, both zero here, so for a layout-2 stripe both dwords
reduce to `lpIdXOR | markerXOR<<31` and must be equal to each other.

MEASURED, and not only as a ZERO.  Census of every clean, addressed
layout-3 page in the store -- 132 pages, up from the 16 this module used to
rest on: carried dw0 zero on all four AUs 132 of 132, AU0's dw1 exactly
`0x80000000` 132 of 132, AU1's dw1 exactly `0x80000000` on 124 and carrying
low bits on 8 (`0x50 0x60 0x70 0xa0 0xd0 0x1f0 0x1ff0`), **every one a multiple
of 16** -- which is what a XOR over 48 members three of whose low nibbles repeat
must give.

And predicted, member by member, on four stripes.  For each stripe whose
members and parity page are both held, `rj_carry.py` reads the members, uses
them as MEASURED, and supplies only the columns this board cannot read from a
lattice fitted to the rest:

    seq(ce, q) = S0 + a*ce + (q & 1) + h*(q >> 1)

    weru 573 page 0    44 members  predicted 0x80000000  observed  match
    weru 590 page 37   44 members  predicted 0x80000000  observed  match
    weru 234 page 539  44 members  predicted 0x800000d0  observed  match
    weru 579 page 76   11 members  predicted 0x80000070  observed  match

4 of 4 on all three quantities (dw0, AU0's dw1, AU1's dw1).  WERU 579 is the
load-bearing one: 37 of its 48 columns are supplied by the model, the answer is
non-zero, and shifting the lattice by any of +-1, +-2, +-3 destroys it (0 of 6
reproduce it).  WERUs 573 and 590 predict zero and are therefore weak on their
own -- said so rather than counted twice; their shifted-lattice controls do
reproduce the value.  Cross-stripe control: a stripe's prediction fits a
different stripe's parity page on 2 of 12 pairs, and those two are exactly the
two that both predict `0x80000000`.

A bonus that cost nothing: This places the RAID PARITY marker in Mso mode.
On all four stripes not one readable member carries the marker, yet the parity
page's bit 31 is set -- so an odd number of the four columns on the dead CE0
carries it.  Column 0 is `slot = 4*ce + q` with ce 0.  That is the same answer
the Inline measurement gives directly (slot 0 flagged on 847/847 pages), reached
here by arithmetic on a mode where the flagged member cannot be read at all.
It upgrades "0 of 191 Mso data pages carry it, a one-sided check".

Honest LIMITS.  The lattice is a description of how this drive's write
fronts advanced, not a law of the format: WERU 590 page 37's `a=6, h=70` fit
misses CE11 by 2, because the padded CE10 in front of it consumed fewer sequence
numbers.  That is exactly why `rj_carry` models only the missing columns and
measures the rest -- CE11 is measured there, so the miss cannot flatter the
result.  Nothing here claims to predict a stripe's sequences from nothing.

SEVEN BITS: SETTLED 2026-08-07 FROM THE INSTRUCTIONS -- THE WRITER WINS
-----------------------------------------------------------------------
RETRACTS "one of the two is off by one and the decompilation cannot say
which -- so `page_sequence` refuses to pick and returns both."  It can, once
you read the encodings instead of the C: both sides are rendered faithfully,
they genuinely disagree, and the disagreement is a firmware bug in the reader.
The bits on media are `seq[38:32]`, so

    page sequence  ==  (AU2.dw1 & 0x7F) << 32  |  AU3.dw1        <- what is stored

The writer, `sub_1C8D8`, `dumps/fw.cpu2.atcm.bin` at 0x1C8D8 (Thumb-2):

    0001c954  01ebc000  add.w r0, r1, r0, lsl #3   ; r0 = pool + 24*group + 8*lane
    0001c95c  0168      ldr   r1, [r0]             ; r1 = seq[31:0]
    0001c95e  0079      ldrb  r0, [r0, #4]         ; r0 = seq[39:32]   <- plain ldrb
    0001c960  6cf3df1e  bfi   lr, ip, #7, #0x19    ; stripeId -> bits [31:7]
    0001c978  60f3060b  bfi   fp, r0, #0, #7       ; seq[38:32] -> bits [6:0]
    0001c984  0160      str   r1, [r0]             ; AU3.dw1 = seq[31:0]

  There is no shift to drop: the high part is a byte load at +4 of the pool
  entry and the `BFI` takes its low 7 bits as they are.  That the pool entry is
  a u64 -- so that byte really is bits 39..32 -- is proven in the same
  function by its own increment, at the same address arithmetic:

    0001c99c  d2e900c3  ldrd  ip, r3, [r2]
    0001c9a0  1cf1010c  adds.w ip, ip, #1
    0001c9a4  43f10003  adc   r3, r3, #0
    0001c9aa  c2e900c3  strd  ip, r3, [r2]

The reader, `sub_4035684E`, `dumps/fw.sram.efc.bin` (VA base 0x4025CD34):

    40356aae  037d      ldrb  r3, [r0, #0x14]      ; AU2.dw1 byte 0
    40356ab0  c169      ldr   r1, [r0, #0x1c]      ; AU3.dw1   -- no `bic`
    40356ab2  03f07f03  and   r3, r3, #0x7f
    40356ab6  5808      lsrs  r0, r3, #1           ; hi = b >> 1  -- together,
    40356ab8  db07      lsls  r3, r3, #0x1f        ; lo = b << 31 -- a lsl #31
    40356aba  5918      adds  r1, r3, r1
    40356abc  40eb0b00  adc.w r0, r0, fp           ; fp == 0
    40356ac0  c7e90210  strd  r1, r0, [r7, #8]

  `lsr #1` + `lsl #31` is the canonical 64-bit shift-by-31 split, so the C's
  `__PAIR64__((b&0x7F) >> 1, (b&0x7F) << 31)` is not a folded rendering of a
  shift by 32 -- a shift by 32 would compile to `hi = b, lo = 0` and Hex-Rays
  would have printed `__PAIR64__(b, 0)`.  The reader really does use 31.

And the bug has a VISIBLE origin.  The layouts-0/1 arm of the same function,
28 bytes earlier, is the same seven instructions:

    40356a7a  4268      ldr  r2, [r0, #4]          ; AU0.dw1
    40356a7c  c168      ldr  r1, [r0, #0xc]        ; AU1.dw1
    40356a7e  22f00042  bic  r2, r2, #0x80000000   <- both shards are u31:
    40356a82  21f00041  bic  r1, r1, #0x80000000      bit 31 is the parity marker
    40356a86  5308      lsrs r3, r2, #1
    40356a88  d207      lsls r2, r2, #0x1f
    40356a8a  5118      adds r1, r2, r1
    40356a8c  43eb0b03  adc.w r3, r3, fp

  There `<< 31` is correct, because the low shard is 31 bits wide.  The layout-3
  arm is that code with the two `bic`s removed -- the low part is now a full
  32-bit dword -- and the shift left at 31.  Two independent tells that this
  is a mistake rather than a definition: the `adds/adc` is an addition, so a
  field placed at bit 31 would carry into a low part that already uses bit 31;
  and the writer's own packing leaves bit 31 of the low dword in use.

It is unobservable on this board, and that is why it stayed open.  Both
readings agree for every sequence below 232.  `selftest` re-measures the
media rather than citing it: on every clean layout-3 page in the store AU2's low
7 bits are ZERO, so no page here populates the disputed field, and all six of
this board's `rebuildSequenceNumberPool` counters are below 231 anyway.  A
media answer is therefore impossible here and is reported as such -- the
disassembly is what settles it.

`parse_page` now returns the WRITER's value in `sequence`, because the writer is
what put the bytes on the media, and carries the firmware reader's value in
`sequence_alt` (with a note) whenever the two differ -- so a caller reproducing
the drive's own rebuild ordering can still get the number the drive would have
computed.
"""
import sys
import struct
import typing                     # only for the `_Unknown.__iter__` type comment
from pathlib import Path

if __package__ in (None, ""):                       # `py titania/auxdata.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
else:
    from . import geometry as geom

_PKG = __package__ or "titania"


def _sib(name):
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))


__all__ = [
    "LAYOUT_INTERNAL_DATA", "LAYOUT_1", "LAYOUT_NSID23", "LAYOUT_RAID_PARITY",
    "LAYOUTS", "LAYOUT_NAMES", "MSO_DESC_ENTRY", "MSO_DESC_AUX_LAYOUT_OFF",
    "MSO_DESC_RAID_TYPE_OFF", "AUS_PER_PAGE", "AUX_BYTES",
    "LMA_INVALID", "LMA_SENTINELS", "INVALID_STRIPE_ID",
    "AUX_FLAG_INLINE_PARITY", "AUX_FLAG_RAID_PARITY_MARKER",
    "PARITY_MARKER_AUS", "PARITY_AUX_STRIPE_AU", "PARITY_AUX_STRIPE_SHIFT",
    "PARITY_SEQ_HI_BITS", "PARITY_SEQ_HI_SHIFT", "PARITY_SEQ_READER_SHIFT",
    "SEQ_SHARD_BITS_01", "SEQ_SHARD_BITS_2", "UNKNOWN",
    "AuxEntry", "AuxPage",
    "parse", "parse_page", "encode_page", "layout_for", "layouts_from_descriptor",
    "page_sequence", "page_is_torn", "parity_stripe_complete", "parity_marker",
    "parity_carry",
    "read_is_plausible", "read_evidence", "ACCEPTANCE_BY_LAYOUT",
    "sequence_gaps", "sequence_is_plausible",
    "summary", "selftest",
]


# ===========================================================================
# the selector
# ===========================================================================
#: `sub_1C438`'s switch arms, `efc_cpu2.c:43006-43107`.  Anything outside this
#: set reaches `default:` and panics ("halt()", "WriteAuxiliaryList.c", 434,
#: `:43106`), so the enumeration is closed by the firmware, not by observation.
LAYOUT_INTERNAL_DATA = 0                 # cAuxLayoutForInternalData
LAYOUT_1 = 1                             # unnamed in this firmware image
LAYOUT_NSID23 = 2                        # cAuxLayoutForForNSID23
LAYOUT_RAID_PARITY = 3                   # cAuxLayoutForRaidParityMso
LAYOUTS = (0, 1, 2, 3)

#: The firmware's own name, or None where the image does not contain one.
#: Never fill a None in with a plausible name -- callers test for it.
LAYOUT_NAMES = {
    0: "cAuxLayoutForInternalData",
    1: None,
    2: "cAuxLayoutForForNSID23",
    3: "cAuxLayoutForRaidParityMso",
}

#: msoDescriptor geometry.  Stride and +17 from `28 * msoId + 17` at
#: `efc_cpu2.c:43002`; +16 is `Mso_GetRaidType` (`:38744`).
MSO_DESC_ENTRY = 28
MSO_DESC_AUX_LAYOUT_OFF = 17
MSO_DESC_RAID_TYPE_OFF = 16

AUX_BYTES = geom.AU_AUX                  # 8
AUS_PER_PAGE = geom.AUS_PER_PAGE_TLC     # 4 == byte_80D855 == dword_90C4C0

#: The invalid LMA.  `sub_1C238` pads with it (`efc_cpu2.c:42856-42860`) and
#: invalidation writes it (`:133792`).
LMA_INVALID = 0xFFFFFFFF
#: Values the firmware treats as "not an address".  0xFFFFFFFD and 0xFFFFFFFC
#: are LUT sentinels (`:23080`); 0xFFFFFFFE and 0xFFFFFFFC are the msoType-6
#: pseudo-LMA tags (`:43109-43122`) and the rebuild's special cases (`:134607`).
LMA_SENTINELS = (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)

#: layout 3.  `raid.INVALID_STRIPE_ID`; the bound ParityManager.c:309 asserts.
INVALID_STRIPE_ID = 0x1FFFFFF
PARITY_AUX_STRIPE_AU = 2
PARITY_AUX_STRIPE_SHIFT = 7

#: Layout 3's page sequence is split across two AUs: AU3's whole second dword is
#: the low 32 bits and AU2's low 7 are the next 7.  The high shard's true home is
#: bit 32 -- `sub_1C8D8` loads it with a plain `LDRB` from byte 4 of a u64
#: counter and BFIs its low 7 bits in (`efc_cpu2.c:43198-43199`; 0x1C95E /
#: 0x1C978 in `dumps/fw.cpu2.atcm.bin`).  See the docstring's "seven bits".
PARITY_SEQ_HI_BITS = 7
PARITY_SEQ_HI_SHIFT = 32
#: What the FIRMWARE's rebuild does with the same 7 bits: a 64-bit `LSL #31`
#: (`lsrs r0,r3,#1` / `lsls r3,r3,#0x1f` at 0x40356AB6 in `dumps/fw.sram.efc.bin`,
#: `efc_cpu2.c:134189-134192`), cloned from the layouts-0/1 arm where 31 is right
#: because the low shard is 31 bits.  It is an off-by-one: it double-counts bit 31
#: and is wrong for any sequence at or above 2**32.  Kept because `sequence_alt`
#: reports the number the drive itself would have computed.
PARITY_SEQ_READER_SHIFT = 31

#: Bit 31 of the second dword, i.e. byte +7 bit 7: the RAID PARITY marker that
#: `sub_1C3CC` ORs onto a DATA AU (`efc_cpu2.c:42935-42957`).  It is the only
#: writer of this bit in efc_cpu0/1/2/3.c and its callers require
#: `Mso_GetRaidType(msoId) != cRaidTypeNone`, so it exists on cRaidTypeInline and
#: cRaidTypeMso data.  On layouts 0/1 it is a spare bit; on layout 2 it collides
#: with the top of AU0's sequence shard; on layout 3 (the parity AU itself) it is
#: not written at all but arrives on AU0/AU1 as the ECU RAID accumulator's XOR of
#: the members' copies.  See the module docstring.
AUX_FLAG_RAID_PARITY_MARKER = 0x80000000
#: The name this module used before the marker was traced past the Inline case.
#: Same bit, same value -- kept so existing callers keep working.
AUX_FLAG_INLINE_PARITY = AUX_FLAG_RAID_PARITY_MARKER

#: Layout 3 carries the marker only on these two AUs; AU2's and AU3's second
#: dword are overwritten by `sub_1C8D8` (stripeId<<7 and the sequence), so bit 31
#: there is a stripeId bit and a sequence bit, not the marker.
PARITY_MARKER_AUS = (0, 1)

#: Sequence-shard widths, so nothing below hard-codes a mask twice.
SEQ_SHARD_BITS_01 = 31                   # `& 0x7FFFFFFF`, `efc_cpu2.c:43031`
SEQ_SHARD_BITS_2 = 16                    # u16 per AU, `:43065-43071`


class _Unknown(object):
    """A falsey singleton meaning 'this field is not decoded on this layout'.

    Not None: None is a legitimate absence and a caller that writes
    `if rec.lma is None` must not silently swallow "there is no lma field here".

    This is `titania.auxdata`'s own singleton, not `titania.ftl.UNKNOWN` -- importing
    ftl to share one would make this module depend on the whole storage layer for
    a sentinel.  Both are falsey, so `if not rec.lma` works across either; an
    identity test must name the right module.

    Falsey is the only thing it does.  Indexing, len(), iteration, `in`,
    attribute access, arithmetic and ordering all raise, and the message names
    what was attempted -- a caller that got this far believed it was holding a
    decoded field.  Those refusals are spelled out as real methods rather than
    left to Python's defaults so that a type checker stops reporting every
    `int | UNKNOWN` use site as "member does not exist": it cannot see the
    `is UNKNOWN` guard, so that diagnostic fires on the guarded sites too and is
    false at every one of them.  Declared, the member exists and its declared
    behaviour is `raise`, which is exactly the runtime contract.
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
        raise TypeError("%s an UNKNOWN value: this field is not decoded on "
                        "this layout" % what)

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
            "%r has no attribute %r: this field is not decoded on this layout"
            % (self, name))

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


UNKNOWN = _Unknown()


# ===========================================================================
# the records
# ===========================================================================
class AuxEntry(object):
    """One AU's 8 bytes, decoded under one layout.

    A field that this layout does not define is `UNKNOWN`, never 0 and never a
    plausible-looking value from a different layout's rule.

    `seq_shard` is this AU's piece of the page's sequence number and is
    meaningless on its own -- use `AuxPage.sequence`.  It is exposed because a
    single AU is often all a caller holds, and because AU2/AU3 carrying a
    non-zero shard under layout 0/1 is a decode failure the caller can see.
    """

    __slots__ = ("raw", "layout", "au", "lma", "lp_id", "seq_shard",
                 "inline_parity", "stripe_id", "dw0", "dw1")

    def __init__(self, raw, layout, au=None, lma=UNKNOWN, lp_id=UNKNOWN,
                 seq_shard=UNKNOWN, inline_parity=UNKNOWN, stripe_id=UNKNOWN):
        self.raw = bytes(raw)
        self.layout = layout
        self.au = au
        self.lma = lma
        self.lp_id = lp_id
        self.seq_shard = seq_shard
        self.inline_parity = inline_parity
        self.stripe_id = stripe_id
        self.dw0, self.dw1 = struct.unpack("<II", self.raw)

    @property
    def lma_is_sentinel(self):
        """True if `lma` is one of the four not-an-address values."""
        return self.lma is not UNKNOWN and self.lma in LMA_SENTINELS

    @property
    def erased(self):
        """All eight bytes 0xFF -- an unwritten aux, not an LMA of -1.

        `lma == LMA_INVALID` alone does not mean this: a written pad entry is
        `ff ff ff ff 00 00 00 00` (`sub_1C238`, `efc_cpu2.c:42856-42860`).
        """
        return self.raw == b"\xff" * AUX_BYTES

    def __repr__(self):
        bits = ["layout=%s" % self.layout]
        if self.au is not None:
            bits.append("au=%d" % self.au)
        if self.lma is not UNKNOWN:
            bits.append("lma=0x%08X" % self.lma)
        if self.lp_id is not UNKNOWN:
            bits.append("lp=0x%04X" % self.lp_id)
        if self.stripe_id is not UNKNOWN:
            bits.append("stripe=%s" % ("INVALID" if self.stripe_id ==
                                       INVALID_STRIPE_ID else self.stripe_id))
        if self.seq_shard is not UNKNOWN:
            bits.append("shard=%d" % self.seq_shard)
        if self.inline_parity is True:
            bits.append("PARITY")
        return "<Aux %s %s>" % (self.raw.hex(), " ".join(bits))


class AuxPage(object):
    """The four aux entries of one page, plus the fields only the page has.

    `sequence` is UNKNOWN when there are not four entries.  It is otherwise
    always the number the writer put on the media.

    `sequence_alt` is not a second opinion.  It is set only on layout 3, only
    when the firmware's own rebuild would compute a different number from the
    same bytes -- its 64-bit shift of AU2's 7 bits is 31 where the writer's is
    32 (see "seven bits" in the module docstring) -- and it carries that
    number, so a caller reproducing the drive's ordering can have it.  `None`
    means the two agree, which is every sequence below 2**32 and therefore
    every page on this board.
    """

    __slots__ = ("entries", "layout", "sequence", "sequence_alt", "stripe_id",
                 "lp_id", "inline_parity", "torn", "notes")

    def __init__(self, entries, layout, sequence=UNKNOWN, sequence_alt=None,
                 stripe_id=UNKNOWN, lp_id=UNKNOWN, inline_parity=UNKNOWN,
                 torn=UNKNOWN, notes=()):
        self.entries = list(entries)
        self.layout = layout
        self.sequence = sequence
        self.sequence_alt = sequence_alt
        self.stripe_id = stripe_id
        self.lp_id = lp_id
        self.inline_parity = inline_parity
        self.torn = torn
        self.notes = list(notes)

    @property
    def lmas(self):
        """This page's four LMAs, or UNKNOWN on a layout that has none."""
        return [e.lma for e in self.entries]

    @property
    def lmas_consecutive(self):
        """The four LMAs are n, n+1, n+2, n+3 -- the crib the scan already uses.

        UNKNOWN rather than False on a layout with no per-AU LMA, so a caller
        cannot read "not consecutive" as evidence.
        """
        v = self.lmas
        if any(x is UNKNOWN for x in v) or len(v) != AUS_PER_PAGE:
            return UNKNOWN
        if any(x in LMA_SENTINELS for x in v):
            return False
        return all(v[i] == v[0] + i for i in range(len(v)))

    def __repr__(self):
        return ("<AuxPage layout=%s seq=%s stripe=%s lp=%s torn=%s>"
                % (self.layout, self.sequence, self.stripe_id, self.lp_id,
                   self.torn))


# ===========================================================================
# parsing
# ===========================================================================
def parse(aux8, layout, au=None):
    """8 aux bytes + a layout -> `AuxEntry`.

    `au` is this AU's index in its page (0..3).  It is optional and only
    sharpens the result: layout 3 puts the stripeId in AU2 alone, so without it
    the stripeId cannot be claimed and comes back UNKNOWN rather than being read
    off an AU that does not hold one.

    Raises ValueError on a layout outside `LAYOUTS` -- the firmware panics
    there (`"HALT()", "WriteAuxiliaryList.c", 434`) and so does this.
    """
    if layout not in LAYOUTS:
        raise ValueError("aux layout %r is outside the firmware's switch %s "
                         '(default: panics "HALT()", "WriteAuxiliaryList.c", '
                         "434, efc_cpu2.c:43106)" % (layout, list(LAYOUTS)))
    raw = bytes(aux8)
    if len(raw) != AUX_BYTES:
        raise ValueError("an aux is %d bytes, got %d" % (AUX_BYTES, len(raw)))
    dw0, dw1 = struct.unpack("<II", raw)

    if layout in (LAYOUT_INTERNAL_DATA, LAYOUT_1):
        return AuxEntry(raw, layout, au,
                        lma=dw0,
                        seq_shard=dw1 & ((1 << SEQ_SHARD_BITS_01) - 1),
                        inline_parity=bool(dw1 & AUX_FLAG_INLINE_PARITY))

    if layout == LAYOUT_NSID23:
        lp, shard = struct.unpack_from("<HH", raw, 4)
        return AuxEntry(raw, layout, au, lma=dw0, lp_id=lp, seq_shard=shard)

    # layout 3.  Only AU2 and AU3's second dword are stamped by the aux writer;
    # dw0 comes through from the parity XOR accumulator and is not decoded here.
    # AU0's and AU1's dw1 also come through the accumulator, but one bit of them
    # is decodable: bit 31 is the XOR of the members' RAID parity markers.  See
    # the module docstring.
    stripe = UNKNOWN
    shard = UNKNOWN
    marker = UNKNOWN
    if au == PARITY_AUX_STRIPE_AU:
        stripe = dw1 >> PARITY_AUX_STRIPE_SHIFT
        shard = dw1 & 0x7F
    elif au == PARITY_AUX_STRIPE_AU + 1:
        shard = dw1
    elif au in PARITY_MARKER_AUS:
        marker = bool(dw1 & AUX_FLAG_RAID_PARITY_MARKER)
    return AuxEntry(raw, layout, au, stripe_id=stripe, seq_shard=shard,
                    inline_parity=marker)


def parse_page(aux_list, layout):
    """The four aux entries of one page -> `AuxPage`.

    `aux_list` may be a list of 8-byte strings, a single 32-byte string, or a
    list of `AuxEntry`.  Fewer than `AUS_PER_PAGE` entries is accepted and
    everything that needs all four comes back UNKNOWN with a note, because a
    partial page is a normal thing to hold and must not be silently completed.
    """
    if layout not in LAYOUTS:
        raise ValueError("aux layout %r is outside %s" % (layout, list(LAYOUTS)))
    if isinstance(aux_list, (bytes, bytearray, memoryview)):
        b = bytes(aux_list)
        aux_list = [b[i:i + AUX_BYTES] for i in range(0, len(b), AUX_BYTES)]
    # Always re-derive from the raw bytes at the positional index.  A caller who
    # pre-parsed without an `au` has a layout-3 entry whose stripeId is UNKNOWN
    # purely because `parse` did not know where it sat; re-keying the attribute
    # would not fix that, so the fields are recomputed instead.
    ents = [parse(a.raw if isinstance(a, AuxEntry) else a, layout, au=i)
            for i, a in enumerate(aux_list)]
    notes = []
    full = len(ents) == AUS_PER_PAGE
    if not full:
        notes.append("%d of %d AUs -- page-level fields need all four"
                     % (len(ents), AUS_PER_PAGE))

    seq = alt = UNKNOWN
    stripe = UNKNOWN
    lp = UNKNOWN
    par = UNKNOWN
    torn = UNKNOWN

    if layout in (LAYOUT_INTERNAL_DATA, LAYOUT_1):
        if full:
            seq = (ents[0].seq_shard << SEQ_SHARD_BITS_01) | ents[1].seq_shard
            if ents[2].seq_shard or ents[3].seq_shard:
                notes.append("AU2/AU3 shards are %d/%d, not 0 -- the writer "
                             "zeroes them (efc_cpu2.c:43034-43035); this page "
                             "did not decode, or was not written by this path"
                             % (ents[2].seq_shard, ents[3].seq_shard))
            # The firmware's own refusal is `aux0.lma == -1 || aux1.lma == -1`
            # (efc_cpu2.c:134231-134235).  "Torn" is the narrower case: one of
            # the two carries a real address and the other carries the invalid
            # marker.  A page whose AU0 is 0xFFFFFFFE or 0xFFFFFFFC is not torn
            # -- that is the msoType-6 redirect tag (`:43109-43124`), and 400
            # perfectly good pages of msoIds 41 and 42 on this board look
            # exactly like that.  See the module docstring.
            a0, a1 = ents[0].lma, ents[1].lma
            torn = ((a0 == LMA_INVALID) != (a1 == LMA_INVALID)
                    and a0 not in LMA_SENTINELS[1:]
                    and a1 not in LMA_SENTINELS[1:])
        flags = set(e.inline_parity for e in ents)
        par = flags.pop() if len(flags) == 1 else UNKNOWN
        if par is UNKNOWN:
            notes.append("the inline-parity flag disagrees across the page's "
                         "AUs; sub_1C3CC sets all four together "
                         "(efc_cpu2.c:42953-42956)")

    elif layout == LAYOUT_NSID23:
        if full:
            seq = 0
            for e in ents:                        # AU0 is the most significant
                seq = (seq << SEQ_SHARD_BITS_2) | e.seq_shard
        lps = set(e.lp_id for e in ents)
        lp = lps.pop() if len(lps) == 1 else UNKNOWN
        if lp is UNKNOWN:
            notes.append("lpId disagrees across the page's AUs; the writer "
                         "stamps one value on all four (efc_cpu2.c:43061-43064)")

    else:                                          # layout 3
        if len(ents) > PARITY_AUX_STRIPE_AU:
            stripe = ents[PARITY_AUX_STRIPE_AU].stripe_id
        # The RAID parity marker, as the accumulator's XOR.  AU0 and AU1 are
        # XORs over the same member pages and sub_1C3CC flags all four AUs of a
        # page together, so they must agree; a disagreement is a decode failure
        # or an unaccumulated buffer, and is reported as UNKNOWN rather than
        # resolved by picking one.
        mk = {ents[i].inline_parity for i in PARITY_MARKER_AUS if i < len(ents)}
        if len(mk) == 1:
            par = mk.pop()
        elif mk:
            notes.append("layout 3: AU0 and AU1 disagree about the RAID parity "
                         "marker (bit 31 of +4).  They are XORs over the same "
                         "member pages, so on an accumulated stripe they agree; "
                         "check parity_stripe_complete before reading either")
        if full:
            hi7 = ents[PARITY_AUX_STRIPE_AU].seq_shard
            lo32 = ents[PARITY_AUX_STRIPE_AU + 1].seq_shard
            # The writer decides what is on the media, and it puts the 7 bits at
            # bit 32: a plain `LDRB` from byte 4 of the u64 counter, `bfi ..,
            # #0, #7` (0x1C95E / 0x1C978, `efc_cpu2.c:43198-43200`).  Settled by
            # disassembly 2026-08-07; see "seven bits" in the module docstring.
            seq = (hi7 << PARITY_SEQ_HI_SHIFT) | lo32
            # What the firmware's own rebuild computes from the same bytes: a
            # 64-bit `LSL #31` (0x40356AB6, `:134189-134192`), cloned from the
            # layouts-0/1 arm without dropping the shift from 31 to 32.  Equal
            # below 2**32, so this is not a doubt about the value -- it is the
            # drive's ordering key, which a rebuild-faithful caller may want.
            alt = (hi7 << PARITY_SEQ_READER_SHIFT) + lo32
            if alt != seq:
                notes.append("layout-3 sequence: %d.  The firmware's rebuild "
                             "would read %d from these same bytes -- its 64-bit "
                             "shift is 31, not 32 (efc_cpu2.c:134189-134192, "
                             "0x40356AB6), an off-by-one against the writer at "
                             "0x1C95E.  The writer is what wrote the media, so "
                             "the first number is the page's sequence."
                             % (seq, alt))
            else:
                alt = None
        notes.append("layout 3: aux +0 on every AU, and +4 on AU0/AU1, are "
                     "carried through from the parity XOR accumulator "
                     "(efc_cpu2.c:37438-37441); +0 == 0 on all four is the "
                     "stripe-completeness signal (parity_stripe_complete) and "
                     "bit 31 of +4 on AU0/AU1 is the accumulated RAID parity "
                     "marker (parity_marker).  The remaining 31 bits are the "
                     "members' sequence shards XOR-ed together and are NOT "
                     "decoded")

    return AuxPage(ents, layout, sequence=seq, sequence_alt=alt,
                   stripe_id=stripe, lp_id=lp, inline_parity=par, torn=torn,
                   notes=notes)


def encode_page(layout, lmas=None, sequence=0, lp_id=0, inline_parity=False,
                stripe_id=None, carry=None):
    """The inverse of `parse_page`, for round-tripping the model in tests.

    Not a firmware-faithful writer -- it does not touch a sequence pool, does
    not implement layout 1's unmapped-AU shortcut, and does not know what the
    parity accumulator would have left in layout 3's undecoded bytes (`carry`
    supplies those verbatim).  It exists so `selftest` can prove the decode is a
    bijection on the fields it claims, without a device.
    """
    if layout not in LAYOUTS:
        raise ValueError("aux layout %r is outside %s" % (layout, list(LAYOUTS)))
    out = [bytearray(carry[i]) if carry else bytearray(AUX_BYTES)
           for i in range(AUS_PER_PAGE)]
    if layout in (LAYOUT_INTERNAL_DATA, LAYOUT_1):
        mask = (1 << SEQ_SHARD_BITS_01) - 1
        shards = [(sequence >> SEQ_SHARD_BITS_01) & mask, sequence & mask, 0, 0]
        for i in range(AUS_PER_PAGE):
            lma = LMA_INVALID if lmas is None else lmas[i]
            dw1 = shards[i] | (AUX_FLAG_INLINE_PARITY if inline_parity else 0)
            struct.pack_into("<II", out[i], 0, lma & 0xFFFFFFFF, dw1)
    elif layout == LAYOUT_NSID23:
        for i in range(AUS_PER_PAGE):
            lma = LMA_INVALID if lmas is None else lmas[i]
            shift = SEQ_SHARD_BITS_2 * (AUS_PER_PAGE - 1 - i)
            struct.pack_into("<IHH", out[i], 0, lma & 0xFFFFFFFF, lp_id & 0xFFFF,
                             (sequence >> shift) & 0xFFFF)
    else:
        hi7, lo32 = (sequence >> 32) & 0x7F, sequence & 0xFFFFFFFF
        sid = INVALID_STRIPE_ID if stripe_id is None else stripe_id
        struct.pack_into("<I", out[PARITY_AUX_STRIPE_AU], 4,
                         ((sid & INVALID_STRIPE_ID) << PARITY_AUX_STRIPE_SHIFT)
                         | hi7)
        struct.pack_into("<I", out[PARITY_AUX_STRIPE_AU + 1], 4, lo32)
        # The accumulator's marker.  With `carry` supplied the caller's bytes
        # already hold it and are left alone; without one this is the only part
        # of AU0/AU1 this module can synthesise, and only because the members'
        # other contributions cancel on this board.
        if carry is None and inline_parity:
            for i in PARITY_MARKER_AUS:
                dw1 = struct.unpack_from("<I", out[i], 4)[0]
                struct.pack_into("<I", out[i], 4,
                                 dw1 | AUX_FLAG_RAID_PARITY_MARKER)
    return [bytes(o) for o in out]


# ===========================================================================
# the selector, from a board rather than from a constant
# ===========================================================================
def layouts_from_descriptor(buf):
    """raw msoDescriptor bytes (63 x 28) -> {msoId: layout}.

    Only entries that are not all-zero are returned; an msoId absent from the
    result had no descriptor, which is not the same as having layout 0.
    """
    buf = bytes(buf)
    out = {}
    n = min(geom.MAX_MSOS, len(buf) // MSO_DESC_ENTRY)
    for m in range(n):
        e = buf[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY]
        if not any(e):
            continue
        out[m] = e[MSO_DESC_AUX_LAYOUT_OFF]
    return out


def _descriptor_bytes(source):
    """-> raw msoDescriptor bytes from an Ftl, a Section, or bytes.  Or None."""
    if source is None:
        return None
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if hasattr(source, "contiguous"):               # a Section
        b = source.contiguous()
        return bytes(b) if b else None
    for get in (lambda s: s.section("mso"),         # an Ftl, public accessor
                lambda s: s._section("mso")[0]):    # ... and the raw one
        try:
            b = get(source)
        except Exception:
            continue
        if b is None or isinstance(b, _Unknown) or b is False:
            continue
        if hasattr(b, "contiguous"):
            b = b.contiguous()
        try:
            b = bytes(b)
        except Exception:
            continue
        if b:
            return b
    return None


def layout_for(mso, source=None):
    """-> the aux layout of `mso`, or UNKNOWN.

    `mso` is an msoId, or anything with a `.mso_id` (an `ftl.Mso` will do).
    `source` is where the descriptor comes from: an `Ftl`, the raw msoDescriptor
    bytes, or the `Section`.  With no source the answer is UNKNOWN -- the layout
    is a per-drive configuration byte and this module will not fall back to a
    table of what some other board had.
    """
    mso_id = getattr(mso, "mso_id", mso)
    if not isinstance(mso_id, int):
        raise TypeError("layout_for wants an msoId or an object with .mso_id, "
                        "got %r" % (mso,))
    buf = _descriptor_bytes(source)
    if buf is None:
        return UNKNOWN
    return layouts_from_descriptor(buf).get(mso_id, UNKNOWN)


# ===========================================================================
# the checks this module exists to make possible
# ===========================================================================
def page_sequence(aux_list, layout):
    """-> the page's sequence number, or UNKNOWN.  Convenience over parse_page."""
    return parse_page(aux_list, layout).sequence


def page_is_torn(aux_list, layout):
    """-> True if exactly one of AU0/AU1 carries the invalid LMA.

    The firmware's rule is the OR -- `if (aux0.lma == -1 || aux1.lma == -1)` it
    throws the whole page's LMA away (`efc_cpu2.c:134231-134235`, skipped for the
    one msoId in `byte_907AC4`).  Both invalid is an ordinary unmapped or padded
    page (`sub_1C238` pads with `ff ff ff ff 00 00 00 00`, `:42856-42860`), so
    the interesting case -- and the one reported here -- is the XOR: a page whose
    two LMA-bearing AUs disagree about whether they were written.

    That is a write that did not complete, and MCRC cannot see it: MCRC is
    per AU over the stored bytes and both AUs are individually well-formed.

    UNKNOWN on a layout with no per-AU LMA (3), or on a partial page.
    """
    return parse_page(aux_list, layout).torn


def parity_stripe_complete(aux_list):
    """-> True/False/UNKNOWN for a layout-3 (RAID-parity) page.

    "Was this parity AU's stripe ever accumulated, so its 4096 bytes are a
    usable parity?"  Answered from the page's own aux, with no LUT lookup, no
    second page and no key -- which is why a reconstructor wants it.

    The test is the four AUs' first dwords, which the aux writer never touches:
    MOH copies them verbatim out of the parity XOR accumulator
    (`efc_cpu2.c:37432-37441`).  Zero means the accumulation closed; anything
    else means the buffer was written out without ever being bound to a stripe.
    See the module docstring for the trace and for why the non-zero case is not
    a partial XOR.

    Measured on this board: 82 of 82 clean layout-3 pages agree with the
    stripeId -- 16 complete and addressed, 66 incomplete and INVALID.
    `selftest` re-runs it.

    THIS IS NOT A BICONDITIONAL, AND THE COUNTEREXAMPLES ARE REAL.  RETRACTS
    "the same unbinding is what leaves INVALID_STRIPE_ID in AU2 ... One
    condition read two ways" (2026-08-07).  Fourteen MCRC-4/4 pages of WERU
    592 held outside the store are *accumulated* (all four carried dwords zero,
    AU0 and AU1 both exactly `AUX_FLAG_RAID_PARITY_MARKER`, 56 of 56 distinct
    AU payloads) and *unaddressed* (AU2 `INVALID_STRIPE_ID`).  The two fields
    come from two different objects with two different lifetimes -- the ECU's
    RAID accumulator, and the ParityManager tracker's stripeId list, which
    `sub_12D58` fills only `wmasPerPage * numberOfStripesInWriteableUnit`
    entries of per allocation (`efc_cpu2.c:33975-33980`) while `sub_1C8D8`
    stamps `-1` for any entry it never filled (`:43186-43196`) -- so they can
    and do disagree.  The independent witness is the parity LUT: it agrees
    with this function on 124 of 124 held WERU-592 pages and names no stripe for
    any of the fourteen.  `titania.raid` owns that measurement.

    So read this function as what it says -- "was anything accumulated into this
    buffer" -- and not as "does this page have an address".  For the address ask
    AU2, or better, ask the parity LUT.

    UNKNOWN unless four full 8-byte aux entries are supplied.
    """
    if aux_list is None or len(aux_list) != AUS_PER_PAGE:
        return UNKNOWN
    if any(a is None or len(a) < AUX_BYTES for a in aux_list):
        return UNKNOWN
    return all(struct.unpack_from("<I", bytes(a), 0)[0] == 0 for a in aux_list)


def parity_marker(aux_list):
    """-> True/False/UNKNOWN for a layout-3 (RAID-parity) page.

    "Did an odd number of this stripe's data members carry the RAID parity
    marker?"  -- which, on every stripe measured, means "exactly one did", and
    is therefore a second witness that this parity AU really is the accumulated
    XOR of a stripe rather than a stale buffer written out.

    The bit is `AUX_FLAG_RAID_PARITY_MARKER` on AU0's and AU1's second dword.
    Nothing writes it there: `sub_1C3CC` (`efc_cpu2.c:42935-42957`) ORs it onto
    DATA AUs only -- its callers require `Mso_GetRaidType(msoId) != 0` and a
    ParityData MSO reads 0 -- and the ECU RAID accumulator XORs the members' 8
    aux bytes along with their 4096 user bytes, so the parity AU inherits the
    parity of the count.  See the module docstring for the model and for the
    inline-mode measurement that pins it (847/847 pages).

    AU2 and AU3 are not consulted: `sub_1C8D8` overwrites their second dword, so
    bit 31 there is a stripeId bit and a sequence bit respectively.

    UNKNOWN unless AU0 and AU1 are both supplied and agree.  Read it together
    with `parity_stripe_complete`: on an unaccumulated buffer these four bytes
    are stale content and the bit means nothing -- 38 of 66 such pages here have
    AU0 and AU1 agreeing purely by chance.
    """
    if aux_list is None or len(aux_list) <= max(PARITY_MARKER_AUS):
        return UNKNOWN
    got = set()
    for i in PARITY_MARKER_AUS:
        a = aux_list[i]
        if a is None or len(a) < AUX_BYTES:
            return UNKNOWN
        got.add(bool(struct.unpack_from("<I", bytes(a), 4)[0]
                     & AUX_FLAG_RAID_PARITY_MARKER))
    return got.pop() if len(got) == 1 else UNKNOWN


def parity_carry(member_pages):
    """-> the four 8-byte auxes the ECU RAID accumulator leaves for a stripe.

    `member_pages` is one entry per stripe column: each a list of four 8-byte
    auxes (a whole data page's aux), in any order -- XOR is commutative.  The
    result is what MOH then copies verbatim into the parity AU's aux
    (`efc_cpu2.c:37432-37441`), *before* `sub_1C8D8` overwrites AU2's and AU3's
    second dword with the stripeId and the sequence.  So:

        encode_page(3, sequence=..., stripe_id=...,
                    carry=parity_carry(members))     ==  the parity page's aux

    This is a model of the hardware, not a firmware routine -- the ECU does
    it over the whole 4104-byte message and this looks only at the 8 aux bytes,
    which is the part that carries information.  What it buys is stated in the
    module docstring's "the carried dwords", and the two consequences worth
    naming here:

      * every AU's first dword comes out zero for any complete stripe,
        because each CE's four columns are one 16-aligned LUT granule and four
        LMAs differing only in bits 2 and 3 XOR to nothing.  `parity_stripe_
        complete` is that identity used as a test.
      * AU0's and AU1's second dword are the members' sequence shards XOR-ed,
        plus the RAID parity marker in bit 31 -- and on this board AU0's shard
        is always zero, so AU0's dword is `0x80000000` exactly.

    Raises ValueError on a ragged input rather than padding it: a stripe with a
    missing column produces a different accumulator state, and silently
    treating "not supplied" as "zero" would hide exactly that.
    """
    pages = list(member_pages)
    if not pages:
        raise ValueError("a stripe has at least one member")
    acc = [bytearray(AUX_BYTES) for _ in range(AUS_PER_PAGE)]
    for n, pg in enumerate(pages):
        pg = list(pg)
        if len(pg) != AUS_PER_PAGE:
            raise ValueError("member %d has %d AUs, not %d"
                             % (n, len(pg), AUS_PER_PAGE))
        for k, a in enumerate(pg):
            a = bytes(a)
            if len(a) != AUX_BYTES:
                raise ValueError("member %d AU %d is %d bytes, not %d"
                                 % (n, k, len(a), AUX_BYTES))
            for j in range(AUX_BYTES):
                acc[k][j] ^= a[j]
    return [bytes(x) for x in acc]


#: What "this aux is consistent with a clean read" means, per layout.  Quoted
#: verbatim by `read_evidence` so a caller reporting a rejection can say which
#: rule rejected it.
ACCEPTANCE_BY_LAYOUT = {
    LAYOUT_INTERNAL_DATA:
        "four consecutive LMAs, or all four LMAs a sentinel (the writer's pad "
        "and the msoType-6 redirect tags)",
    LAYOUT_1:
        "four consecutive LMAs, or all four LMAs a sentinel (the writer's pad "
        "and the msoType-6 redirect tags)",
    LAYOUT_NSID23:
        "four consecutive LMAs, or all four LMAs a sentinel (the writer's pad "
        "and the msoType-6 redirect tags)",
    LAYOUT_RAID_PARITY:
        "the carried +0 dwords being all-zero agrees with AU2 carrying a real "
        "stripeId -- there is NO LMA array on this layout",
}


def read_evidence(aux_list, layout):
    """-> {'ok', 'layout', 'rule', 'detail'}.  `read_is_plausible` with reasons.

    The bug this exists to kill.  "The four aux LMAs must be n, n+1, n+2, n+3"
    is a layout-0/1/2 test that got used as if it were universal, and a
    layout-3 (`cAuxLayoutForRaidParityMso`) page cannot pass it: `sub_1C8D8`
    stamps only AU2's and AU3's second dword and every first dword comes
    verbatim out of the ECU RAID accumulator (`efc_cpu2.c:37432-37441`), so
    there is no LMA array there to be consecutive.  On this board that test
    withheld 82 pages that had been read perfectly -- 246 reads, MCRC 4/4
    on every one -- off the ten ParityData WERUs.  Consult `msoDescriptor +17`
    (`layout_for`) first, then apply the layout's own rule.

    The rules, and what each is measured against on this board:

    layouts 0/1/2 -- the LMA-array layouts.
        `lmas_consecutive`, OR all four LMAs in `LMA_SENTINELS`.  The second
        arm is not a loophole: `sub_1C238` pads an unwritten AU with
        `ff ff ff ff 00 00 00 00` (`efc_cpu2.c:42856-42860`) and msoType 6
        writes the 0xFFFFFFFE / 0xFFFFFFFC redirect tags (`:43109-43124`).
        Measured here on MCRC-clean pages: 75 such pages (msoId 56's
        RootStorage padding on blocks 1..5, msoIds 41/42's tags on blocks
        12/13, four of msoId 43's) against 391 consecutive ones.  A page with
        some sentinels and some real addresses is not accepted -- that is the
        torn-write case `page_is_torn` reports.

    layout 3 -- the RAID parity layout, which has no LMA array at all.
        `parity_stripe_complete(aux) is True` if and only if AU2 carries a real
        stripeId.  It agrees on 83 of 83 clean layout-3 pages in the store
        -- 17 accumulated and addressed, 66 unbound buffers with
        INVALID_STRIPE_ID, zero split.  It is a real test, not a rubber stamp: a
        wrong-mode or misaddressed read leaves the carried dwords non-zero while
        AU2's top 25 bits are almost never all ones, so it lands "not complete
        but addressed" and is rejected.

        The two halves are not one condition -- see `parity_stripe_complete`
        for the fourteen accumulated-but-unaddressed pages that refute that, and
        for the parity-LUT cross-check.  This rule still refuses them, and on
        purpose: a parity AU with no stripeId names no stripe anywhere on the
        drive, and the firmware's own rebuild skips such a page outright
        (`efc_cpu2.c:134174-134180` -- the layout-3 arm turns
        `stripeField == 0x1FFFFFF` into `-1` and advances past the whole page).
        What changes is the reason: they are refused because they are
        unaddressable, not because they look like a bad read.  They are not a
        bad read; every byte of them is sound.

    UNKNOWN -- never False -- when the page is partial, when the layout is not
    known, or when a field the rule needs is not decodable.  A caller must
    reject only on an explicit False; treating UNKNOWN as a rejection is the
    same class of mistake all over again.
    """
    out = {"ok": UNKNOWN, "layout": layout,
           "rule": ACCEPTANCE_BY_LAYOUT.get(layout), "detail": ""}
    if layout is None or layout is UNKNOWN:
        out["detail"] = ("no aux layout for this page's MSO -- msoDescriptor "
                         "+17 was not consulted or the owner is unknown, so "
                         "the aux cannot be judged either way")
        return out
    if layout not in LAYOUTS:
        raise ValueError("aux layout %r is outside %s" % (layout, list(LAYOUTS)))
    if isinstance(aux_list, (bytes, bytearray, memoryview)):
        b = bytes(aux_list)
        aux_list = [b[i:i + AUX_BYTES] for i in range(0, len(b), AUX_BYTES)]
    aux_list = list(aux_list)
    if len(aux_list) != AUS_PER_PAGE:
        out["detail"] = ("%d of %d AUs -- every rule here needs the whole page"
                         % (len(aux_list), AUS_PER_PAGE))
        return out
    rec = parse_page(aux_list, layout)

    if layout == LAYOUT_RAID_PARITY:
        done = parity_stripe_complete(aux_list)
        if done is UNKNOWN or rec.stripe_id is UNKNOWN:
            out["detail"] = "the layout-3 fields are not decodable on this page"
            return out
        addressed = rec.stripe_id != INVALID_STRIPE_ID
        out["ok"] = bool(done) == addressed
        out["detail"] = (
            "carried +0 dwords all zero: %s; AU2 stripeId: %s.  Accumulated "
            "but unaddressed is a REAL state (14 such pages of WERU 592) and "
            "is refused here because such a page names no stripe anywhere on "
            "the drive, not because its bytes are suspect"
            % (done, "INVALID" if not addressed else rec.stripe_id))
        return out

    lmas = rec.lmas
    if any(x is UNKNOWN for x in lmas):
        out["detail"] = "this layout's LMA field did not decode"
        return out
    if all(x in LMA_SENTINELS for x in lmas):
        out["ok"] = True
        out["detail"] = ("all four LMAs are sentinels (%s) -- an unwritten/"
                         "padded AU or an msoType-6 redirect tag, not a bad read"
                         % " ".join("%#x" % x for x in lmas))
        return out
    out["ok"] = rec.lmas_consecutive is True
    out["detail"] = ("LMAs %s%s"
                     % (["%#x" % x for x in lmas],
                        "" if out["ok"] else " are neither consecutive nor all "
                        "sentinels"))
    return out


def read_is_plausible(aux_list, layout):
    """-> True / False / UNKNOWN.  Is this aux consistent with a CLEAN read?

    The one acceptance test a retry walk or a store sweep should use, and it is
    keyed on the layout the owning MSO's `msoDescriptor +17` selects.  See
    `read_evidence` for the per-layout rules, the measurements behind them, and
    the bug that made this necessary.

    Reject only on an explicit `False`.
    """
    return read_evidence(aux_list, layout)["ok"]


def sequence_is_plausible(sequence, group_id, pool, layout=None):
    """-> True/False/UNKNOWN.  A sequence past the pool's `next` is impossible.

    `pool` is `Ftl.rebuild_sequence_number_pool()` -- six `(next, base, end)`
    triples, one per msoGroupId (`efc_cpu2.c:34445`).  `next` is the next
    unissued number.

    The bound is strict for layouts 0, 1 and 3 and not strict for layout 2.
    Layouts 0/1/3 stamp the counter and then increment it (`efc_cpu2.c:43135`
    then `:43143`; `:43197` then `:43203`), so every value on media is `< next`.
    Layout 2 reads the pool at `:43058` and never increments it, so it stamps
    `next` itself and `<= next` is the most that can be asserted.  Measured:
    every layout-2 page in this store carries exactly its group's `next`
    (msoId 51 -> 2812 == pool[3].next on 8 pages, msoId 53 -> 2340 ==
    pool[2].next on 3 pages).  Passing `layout=None` uses the strict bound and
    will therefore reject a perfectly good layout-2 page -- pass the layout.

    UNKNOWN when the pool or the group is not available, never True by default.
    """
    if sequence is UNKNOWN or pool is None or isinstance(pool, _Unknown):
        return UNKNOWN
    try:
        nxt = pool[group_id][0]
    except (IndexError, TypeError, KeyError):
        return UNKNOWN
    return sequence <= nxt if layout == LAYOUT_NSID23 else sequence < nxt


def sequence_gaps(sequences):
    """[(a, b)] for every place a sorted sequence run skips a number.

    The rebuild advances its expectation by exactly one per record and logs a
    debug event when the next number is not the one it wanted
    (`efc_cpu2.c:134609-134637`).  Over the pages a dump actually holds a gap
    means a page between them is missing or misdecoded -- which is a different
    thing from a page being stale, and the toolkit could not tell them apart
    before.

    Duplicates are reported as a zero-width gap `(n, n)`: two pages of one MSO
    claiming one sequence number cannot both be right.
    """
    vals = sorted(v for v in sequences if v is not UNKNOWN and v is not None)
    out = []
    for a, b in zip(vals, vals[1:]):
        if b == a:
            out.append((a, a))
        elif b != a + 1:
            out.append((a, b))
    return out


# ===========================================================================
# the surface
# ===========================================================================
def summary(source=None, pool=None):
    """A human census of this board's aux layouts -> str.

    With no `source` it still prints the enumeration and its citations, marked
    as such, so the function is useful without a device.
    """
    L = []
    add = L.append
    add("AU auxiliary layouts -- msoDescriptor +17 (efc_cpu2.c:43002)")
    add("=" * 62)
    for lay in LAYOUTS:
        name = LAYOUT_NAMES[lay] or "<unnamed in this firmware image>"
        add("  %d  %-30s %s" % (lay, name, _FIELD_SUMMARY[lay]))
    add("")
    buf = _descriptor_bytes(source)
    if buf is None:
        add("no msoDescriptor supplied -- per-board assignment UNKNOWN")
        return "\n".join(L)
    lays = layouts_from_descriptor(buf)
    add("this board's %d populated descriptors:" % len(lays))
    for lay in LAYOUTS:
        ids = sorted(m for m, v in lays.items() if v == lay)
        add("  layout %d : %2d msoIds %s" % (lay, len(ids), ids))
    stray = sorted(set(lays.values()) - set(LAYOUTS))
    if stray:
        add("  !! %s are outside the firmware's switch and would panic" % stray)
    raid = {}
    for m in lays:
        e = buf[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY]
        raid.setdefault((lays[m], e[MSO_DESC_RAID_TYPE_OFF]), []).append(m)
    add("")
    add("  auxLayout x raidType (raidType 2 == cRaidTypeInline, the only one")
    add("  that can carry the +7 bit-7 parity flag):")
    for k in sorted(raid):
        add("    aux=%d raid=%d : %s" % (k[0], k[1], sorted(raid[k])))
    if pool:
        add("")
        add("  rebuildSequenceNumberPool `next` per msoGroupId -- the upper")
        add("  bound on any sequence this board can carry.  STRICT for layouts")
        add("  0/1/3, which stamp then increment; layout 2 never increments and")
        add("  lands exactly ON it (efc_cpu2.c:43058 vs :43143):")
        add("    %s" % [p[0] for p in pool])
    return "\n".join(L)


_FIELD_SUMMARY = {
    0: "+0 u32 lma, +4 u31 seq shard, +7 bit7 RAID parity marker",
    1: "as layout 0; differs only in skipping unmapped AUs",
    2: "+0 u32 lma, +4 u16 lpId, +6 u16 seq shard (AU0 = most significant)",
    3: "AU2 +4: stripeId<<7 | seq[38:32]; AU3 +4: seq[31:0]; AU0/AU1 +7 bit7: "
       "the accumulated RAID parity marker; rest undecoded",
}


# ===========================================================================
# selftest
# ===========================================================================
def _report(name, state, detail=""):
    print("  %-46s %-8s %s" % (name, state, detail))


# ---------------------------------------------------------------------------
# the 7-bit shard: decoded from the Thumb-2 encodings, not from Hex-Rays
# ---------------------------------------------------------------------------
#: `dumps/fw.sram.efc.bin` VA base.  Not trusted blindly -- the check below
#: locates the reader's 74-byte instruction run by CONTENT and requires it to
#: be unique in the image and to sit at exactly this base's file offset, so a
#: wrong base fails loudly instead of disassembling neighbouring data.
_SRAM_VA_BASE = 0x4025CD34
_READER_VA = 0x40356A7A          # sub_4035684E, both sequence arms
_WRITER_OFF = 0x1C95C            # sub_1C8D8, cpu2 ATCM is based at 0

#: `ldrd/adds/adc/strd` at 0x1C99C..0x1C9AD -- the same pool entry `sub_1C8D8`
#: takes the shard from, incremented as one 64-bit quantity.  This is what makes
#: "byte +4" mean "bits 39..32" rather than "byte 4 of something".
_U64_INC = bytes.fromhex("d2e900c3" "1cf1010c" "43f10003")


def _hw(buf, i):
    return buf[i] | (buf[i + 1] << 8)


def _dec_ldrb_imm(hw):
    """Thumb-16 `LDRB Rd, [Rn, #imm5]` -> imm5, or None."""
    return (hw >> 6) & 0x1F if (hw >> 11) == 0b01111 else None


def _dec_bfi(hw1, hw2):
    """Thumb-32 `BFI Rd, Rn, #lsb, #width` -> (lsb, width), or None."""
    if (hw1 & 0xFBF0) != 0xF360:
        return None
    lsb = (((hw2 >> 12) & 7) << 2) | ((hw2 >> 6) & 3)
    return lsb, (hw2 & 0x1F) - lsb + 1


def _dec_shift_imm(hw):
    """Thumb-16 `LSLS`/`LSRS Rd, Rm, #imm5` -> ('lsl'|'lsr', n), or None."""
    op = hw >> 11
    if op not in (0b00000, 0b00001):
        return None
    n = (hw >> 6) & 0x1F
    if op == 0b00001 and n == 0:
        n = 32                                    # Lsr #0 encodes #32
    return ("lsl" if op == 0b00000 else "lsr"), n


#: A cRaidTypeMso stripe: 48 columns, `slot = 4*ce + q`, so four consecutive
#: columns are the four blocks of one CE and are one 16-aligned LUT granule.
#: `raid.COLUMNS` / `ftl` own these; repeated here so this check does not need
#: to import the storage layer to state its own premise.
_STRIPE_COLUMNS = 48
_LUT_GRANULE = 16


def _synth_stripe(granules, s0, a, h, marker_col=0, layout=LAYOUT_1,
                  lp_id=0x1234):
    """Build one cRaidTypeMso stripe's 48 member aux pages.

    `granules[ce]` is that CE's LUT granule base LMA.  Column `4*ce + q` is the
    q-th block of that CE, so its four AUs carry `G + 4*q + k`; its page
    sequence is `s0 + a*ce + (q & 1) + h*(q >> 1)`.
    """
    pages = []
    for c in range(_STRIPE_COLUMNS):
        ce, q = divmod(c, 4)
        g = granules[ce]
        seq = s0 + a * ce + (q & 1) + h * (q >> 1)
        pg = encode_page(layout, lmas=[g + 4 * q + k for k in range(4)],
                         sequence=seq, lp_id=lp_id,
                         inline_parity=(c == marker_col))
        pages.append(pg)
    return pages


def _check_parity_carry():
    """-> 0/1.  Does the member-XOR model produce what layout 3 carries?"""
    name = "layout 3 carried dwords = member XOR"
    why = []
    gran = [_LUT_GRANULE * (7919 * (ce + 1) % 100003) for ce in range(12)]
    s0, a, h = 3300922, 4, 48
    members = _synth_stripe(gran, s0, a, h)
    carry = parity_carry(members)
    d = [struct.unpack("<II", x) for x in carry]

    if any(x[0] for x in d):
        why.append("dw0 is %s, not zero -- the 16-granule identity failed"
                   % [x[0] for x in d])
    seq_xor = 0
    for c in range(_STRIPE_COLUMNS):
        ce, q = divmod(c, 4)
        seq_xor ^= s0 + a * ce + (q & 1) + h * (q >> 1)
    if d[0][1] != AUX_FLAG_RAID_PARITY_MARKER:
        why.append("AU0 dw1 is %#x, not the marker alone" % d[0][1])
    if d[1][1] != ((seq_xor & 0x7FFFFFFF) | AUX_FLAG_RAID_PARITY_MARKER):
        why.append("AU1 dw1 is %#x, not the sequence XOR | marker" % d[1][1])

    # The whole parity aux, reassembled the way the firmware does it.
    page = encode_page(LAYOUT_RAID_PARITY, sequence=370354, stripe_id=2530368,
                       carry=carry)
    p = parse_page(page, LAYOUT_RAID_PARITY)
    if (p.stripe_id != 2530368 or p.sequence != 370354
            or parity_stripe_complete(page) is not True
            or parity_marker(page) is not True):
        why.append("the reassembled parity page does not decode back")

    # Negative control 1: a granule base that is not 16-aligned breaks dw0.
    bad = list(gran)
    bad[5] += 4
    c2 = parity_carry(_synth_stripe(bad, s0, a, h))
    if not any(struct.unpack("<II", x)[0] for x in c2):
        why.append("a mis-aligned granule still gives dw0 == 0, so the test "
                   "has no power")
    # Negative control 2: an even number of markers must clear bit 31.
    c3 = parity_carry(_synth_stripe(gran, s0, a, h, marker_col=None))
    if struct.unpack("<II", c3[0])[1] & AUX_FLAG_RAID_PARITY_MARKER:
        why.append("bit 31 survives with no flagged member")
    # Negative control 3: a wrong sequence lattice must change AU1.
    c4 = parity_carry(_synth_stripe(gran, s0 + 1, a, h))
    if struct.unpack("<II", c4[1])[1] == d[1][1]:
        why.append("shifting the lattice by 1 leaves AU1 unchanged")
    # Layout 2 members: shards are zero on this board, so AU0 == AU1 exactly.
    c5 = parity_carry(_synth_stripe(gran, s0, a, h, layout=LAYOUT_NSID23))
    if struct.unpack("<II", c5[0])[1] != struct.unpack("<II", c5[1])[1]:
        why.append("layout-2 members do not give AU0 == AU1")

    # A stripe missing a column is a different accumulator state, and the model
    # must not quietly pad it.
    try:
        parity_carry([m[:3] for m in members])
        why.append("parity_carry accepted a 3-AU member")
    except ValueError:
        pass

    ok = not why
    _report(name, "PASS" if ok else "FAIL",
            "48 synthetic members, `slot = 4*ce+q`: the carried dw0 is ZERO on "
            "all four AUs BY IDENTITY (each CE's four columns are one "
            "%d-aligned LUT granule, so their LMAs differ only in bits 2 and 3 "
            "and each appears twice), AU0's dw1 is the marker alone because "
            "every sequence here is below 2**%d, and AU1's is the members' "
            "sequence XOR.  Controls: a mis-aligned granule breaks dw0, an even "
            "marker count clears bit 31, a lattice shifted by 1 changes AU1, "
            "layout-2 members give AU0 == AU1, and a short member is refused.  "
            "`rj_carry.py` runs the same model against media: 132 clean "
            "addressed parity pages (dw0 zero 132/132, AU0 == 0x80000000 "
            "132/132) and 4 stripes predicted member by member, 4/4 on all "
            "three quantities.%s"
            % (_LUT_GRANULE, SEQ_SHARD_BITS_01,
               "  FAILURES: %s" % why if why else ""))
    return 0 if ok else 1


def _check_seven_bits():
    """-> 0/1.  Does the media's 7-bit shard belong at bit 32 or at bit 31?

    Reads the encodings out of the firmware dumps and decodes the three
    instructions the answer turns on.  SKIPPED, not PASS, without the dumps.
    """
    name = "layout 3 seq[38:32]: writer beats reader"
    root = Path(__file__).resolve().parent.parent
    try:
        atcm = (root / "dumps" / "fw.cpu2.atcm.bin").read_bytes()
        sram = (root / "dumps" / "fw.sram.efc.bin").read_bytes()
    except Exception as exc:
        _report(name, "SKIPPED", "no firmware dumps: %r" % (exc,))
        return 0

    why = []

    # -- the writer.  `ldr r1,[r0]` then `ldrb r0,[r0,#4]` off one u64 counter,
    #    then `bfi ..,#0,#7`.  The offset and the width are what matter.
    w = atcm[_WRITER_OFF:_WRITER_OFF + 0x2A]
    lo_load = _hw(w, 0) >> 11 == 0b01101 and ((_hw(w, 0) >> 6) & 0x1F) == 0
    hi_imm = _dec_ldrb_imm(_hw(w, 2))
    bfi = _dec_bfi(_hw(w, 0x1C), _hw(w, 0x1E))
    u64 = atcm[0x1C99C:0x1C99C + len(_U64_INC)] == _U64_INC
    if not lo_load:
        why.append("0x1C95C is not `ldr r1,[r0]`")
    if hi_imm != 4:
        why.append("0x1C95E is not `ldrb r0,[r0,#4]` (imm=%s)" % (hi_imm,))
    if bfi != (0, PARITY_SEQ_HI_BITS):
        why.append("0x1C978 BFI is %s, not (lsb 0, width %d)"
                   % (bfi, PARITY_SEQ_HI_BITS))
    if not u64:
        why.append("0x1C99C is not the 64-bit ldrd/adds/adc increment")

    # -- the reader.  Locate it by CONTENT so a wrong VA base cannot pass.
    off = _READER_VA - _SRAM_VA_BASE
    blob = sram[off:off + 74]
    if sram.count(blob) != 1:
        why.append("the reader's 74-byte run is not unique in fw.sram.efc.bin "
                   "(%d hits) -- the VA base is wrong" % sram.count(blob))
    # layouts 0/1 arm: two `bic Rd,Rd,#0x80000000` then lsr #1 / lsl #31
    bic0 = blob[4:8] == bytes.fromhex("22f00042")
    bic1 = blob[8:12] == bytes.fromhex("21f00041")
    sh01 = (_dec_shift_imm(_hw(blob, 12)), _dec_shift_imm(_hw(blob, 14)))
    # layout 3 arm: No bic, and the same two shifts
    l3 = blob[0x34:]
    ldrb3 = _dec_ldrb_imm(_hw(l3, 0))
    ldr3 = _hw(l3, 2) >> 11 == 0b01101 and ((_hw(l3, 2) >> 6) & 0x1F) * 4 == 0x1C
    sh3 = (_dec_shift_imm(_hw(l3, 8)), _dec_shift_imm(_hw(l3, 10)))
    want = (("lsr", 1), ("lsl", PARITY_SEQ_READER_SHIFT))
    if not (bic0 and bic1):
        why.append("the layouts-0/1 arm does not mask bit 31 off both shards")
    if sh01 != want or sh3 != want:
        why.append("shift pair is %s / %s, not %s" % (sh01, sh3, want))
    if ldrb3 != 0x14 or not ldr3:
        why.append("layout 3 arm does not load AU2 +4 byte 0 and AU3 +4")
    # the load the layout-3 arm does not mask -- the whole point
    if _dec_bfi(_hw(l3, 4), _hw(l3, 6)) is not None:
        why.append("unexpected BFI in the layout-3 read arm")
    if any(l3[i:i + 4] in (bytes.fromhex("22f00042"), bytes.fromhex("21f00041"))
           for i in range(0, len(l3) - 3)):
        why.append("the layout-3 arm masks bit 31, so its low part is 31 bits "
                   "after all and `lsl #31` would be right")

    ok = not why
    _report(name, "PASS" if ok else "FAIL",
            "SETTLED FROM THE INSTRUCTIONS.  Writer sub_1C8D8: `ldr r1,[r0]` + "
            "`ldrb r0,[r0,#4]` off a counter its own `ldrd/adds/adc/strd` "
            "proves is a u64, then `bfi #0,#7` -- no shift is dropped, so the "
            "field is seq[38:32] and belongs at bit %d.  Reader sub_4035684E: "
            "`lsr #1` + `lsl #31`, the canonical 64-bit LSL #%d (a shift by 32 "
            "would encode as hi=b, lo=0), cloned from the layouts-0/1 arm 44 "
            "bytes earlier -- which is the SAME seven instructions PLUS two "
            "`bic ..,#0x80000000` that make its low part 31 bits wide.  Layout "
            "3's arm drops the masks and keeps the shift, and adds rather than "
            "ORs, so it double-counts bit 31: an off-by-one in the READER. "
            "`sequence` is the writer's value; `sequence_alt` carries the "
            "reader's when they differ (never on this board -- see the media "
            "check).%s" % (PARITY_SEQ_HI_SHIFT, PARITY_SEQ_READER_SHIFT,
                           "  FAILURES: %s" % why if why else ""))
    return 0 if ok else 1


def selftest(argv=()):
    """-> 0 on success.  SKIPPED, never PASS, for anything the dumps cannot show.

    `--pages N` bounds the media pass at N decoded pages per layout (default
    400).  Be aware of the cost: layouts 1, 2 and 3 live almost entirely in
    native-TLC blocks, many of which this store holds only as pseudo-SLC reads,
    and every one of those costs a full LDPC correction that then fails anyway.
    A run that touches all four layouts is ~20 minutes on this share.  The
    arithmetic half (sections 1-3) needs no device and is instant.
    """
    print("titania.auxdata -- selftest\n")
    fails = 0
    budget = 400
    if "--pages" in argv:
        budget = int(argv[argv.index("--pages") + 1])

    # ---- 1. the enumeration is closed --------------------------------------
    ok = (tuple(sorted(LAYOUT_NAMES)) == LAYOUTS
          and LAYOUT_NAMES[1] is None
          and LAYOUT_NAMES[0] == "cAuxLayoutForInternalData"
          and LAYOUT_NAMES[2] == "cAuxLayoutForForNSID23"
          and LAYOUT_NAMES[3] == "cAuxLayoutForRaidParityMso")
    try:
        parse(b"\0" * 8, 4)
        ok = False
    except ValueError:
        pass
    fails += not ok
    _report("layout enumeration", "PASS" if ok else "FAIL",
            "4 arms of sub_1C438's switch (efc_cpu2.c:43006-43107); anything "
            "else reaches default: and panics WriteAuxiliaryList.c:434.  "
            "Layout 1 is unnamed in efc_cpu0/1/2/3.c and stays None.")

    # ---- 2. every claimed field round-trips --------------------------------
    ok = True
    why = []
    for lay in (0, 1):
        for seq in (0, 1, 0x7FFFFFFF, 0x80000000, (1 << 62) - 1):
            for flag in (False, True):
                enc = encode_page(lay, lmas=[10, 11, 12, 13], sequence=seq,
                                  inline_parity=flag)
                p = parse_page(enc, lay)
                if p.sequence != seq or p.inline_parity != flag:
                    ok = False
                    why.append("layout %d seq %d flag %s" % (lay, seq, flag))
                if p.lmas != [10, 11, 12, 13] or p.lmas_consecutive is not True:
                    ok = False
    for seq in (0, 1, 0xFFFF, 0x1234_5678_9ABC_DEF0):
        enc = encode_page(2, lmas=[1, 2, 3, 4], sequence=seq, lp_id=0x0BAD)
        p = parse_page(enc, 2)
        if p.sequence != seq or p.lp_id != 0x0BAD:
            ok = False
            why.append("layout 2 seq %d" % seq)
    for sid in (0, 1, INVALID_STRIPE_ID - 1, INVALID_STRIPE_ID):
        for mk in (False, True):
            # The sequence values straddle 2**32: below it the
            # writer's and the firmware reader's packings agree, above it they
            # do not, and `encode_page`/`parse_page` must be the writer's.
            for sq in (0x1234_5678, 0xFFFF_FFFF, 0x1_0000_0000,
                       (1 << 39) - 1):
                enc = encode_page(3, sequence=sq, stripe_id=sid,
                                  inline_parity=mk)
                p = parse_page(enc, 3)
                if p.stripe_id != sid or p.sequence != sq:
                    ok = False
                    why.append("layout 3 stripe %d seq %d -> %s"
                               % (sid, sq, p.sequence))
            enc = encode_page(3, sequence=0x1234_5678, stripe_id=sid,
                              inline_parity=mk)
            p = parse_page(enc, 3)
            if p.stripe_id != sid or p.sequence != 0x1234_5678:
                ok = False
                why.append("layout 3 stripe %d" % sid)
            # the marker lives on AU0/AU1 only; AU2/AU3's bit 31 belongs to the
            # stripeId and the sequence and must not be read as one
            if p.inline_parity != mk or parity_marker(enc) != mk:
                ok = False
                why.append("layout 3 marker %s" % mk)
            if ((sid >> 24) & 1) != (
                    struct.unpack_from("<I", enc[PARITY_AUX_STRIPE_AU], 4)[0]
                    >> 31):
                ok = False
                why.append("layout 3 AU2 bit31 is stripeId bit 24, not a marker")
    fails += not ok
    _report("field model round-trips", "PASS" if ok else "FAIL",
            "encode_page -> parse_page is the identity on every field this "
            "module claims, over layouts 0-3" + (" -- %s" % why if why else ""))

    # ---- 3. the bit positions match the other modules ----------------------
    try:
        raid = _sib("raid")
        ftl = _sib("ftl")
        ok = (raid.MSO_DESC_AUX_LAYOUT_OFF == MSO_DESC_AUX_LAYOUT_OFF
              and raid.AUX_LAYOUT_RAID_PARITY == LAYOUT_RAID_PARITY
              and raid.PARITY_AUX_STRIPE_AU == PARITY_AUX_STRIPE_AU
              and raid.PARITY_AUX_STRIPE_SHIFT == PARITY_AUX_STRIPE_SHIFT
              and raid.INVALID_STRIPE_ID == INVALID_STRIPE_ID
              and ftl.MSO_DESC_ENTRY == MSO_DESC_ENTRY
              and ftl.AUX_LMA_OFF == 0
              and ftl.AUX_FLAG_OFF == 7
              and ftl.AUX_FLAG_INLINE_PARITY == 0x80
              and ftl.ROOTSTORAGE_LMA == LMA_INVALID)
        fails += not ok
        _report("agrees with titania.raid and titania.ftl",
                "PASS" if ok else "FAIL",
                "selector offset, layout 3's stripe field, the +7 bit-7 flag "
                "and the invalid LMA are one definition in three modules")
    except Exception as exc:
        _report("agrees with titania.raid and titania.ftl", "SKIPPED", repr(exc))

    # ---- 3b. layout 3's 7 bits, settled from the instruction encodings ------
    fails += _check_seven_bits()

    # ---- 3c. the accumulator model for layout 3's carried dwords -----------
    fails += _check_parity_carry()

    _report("ftl.AUX_SEQ_LEN == 3 (u24)", "RETRACTED",
            "the sequence is 31 bits in AU1 plus 31 in AU0 (efc_cpu2.c:43031, "
            ":134226-134229).  A u24 read is correct on this board only because "
            "the largest counter is 3862740; it loses the top of the counter "
            "from 2**24 on.  titania.auxdata.page_sequence reads all 62.")

    # ---- 4. media ----------------------------------------------------------
    try:
        cli = _sib("cli")
        F = _sib("ftl")
        dev = cli.open_device(None)
        f = F.Ftl.from_device(dev)
    except Exception as exc:
        for n in ("layout assignment on this board",
                  "layout 0/1 sequence and flag on media",
                  "layout 2 on media", "layout 3 on media"):
            _report(n, "SKIPPED", "no local store: %r" % (exc,))
        return 1 if fails else 0

    buf = _descriptor_bytes(f)
    if not buf:
        _report("layout assignment on this board", "SKIPPED",
                "no msoDescriptor section recovered")
        return 1 if fails else 0
    lays = layouts_from_descriptor(buf)
    counts = {l: sum(1 for v in lays.values() if v == l) for l in LAYOUTS}
    ok = set(lays.values()) <= set(LAYOUTS) and all(counts[l] for l in LAYOUTS)
    fails += not ok
    _report("layout assignment on this board", "PASS" if ok else "FAIL",
            "all four layouts are in use: %s (out of %d populated descriptors); "
            "no descriptor carries a value the firmware would panic on"
            % (counts, len(lays)))

    # The marker cannot exist where raidType == cRaidTypeNone: sub_1C3CC is its
    # only writer and both its callers require a non-None raid type.  That is a
    # cross-check between two independent descriptor bytes and a media bit.
    inline = sorted(m for m in lays
                    if buf[m * MSO_DESC_ENTRY + MSO_DESC_RAID_TYPE_OFF] == 2)
    # Restrict to layout 2, which is where the marker collides with a decoded
    # field.  msoIds 0..31 all share one of two byte-identical dummy records
    # (see NAND_STORAGE_MAP), so listing every raidType-1 id would be noise.
    msoo = sorted(m for m in lays
                  if buf[m * MSO_DESC_ENTRY + MSO_DESC_RAID_TYPE_OFF] == 1
                  and lays[m] == LAYOUT_NSID23 and m >= 32)
    ok = bool(inline) and all(lays[m] == LAYOUT_INTERNAL_DATA for m in inline)
    fails += not ok
    _report("raidType-2 MSOs are all layout 0", "PASS" if ok else "FAIL",
            "msoIds with raidType 2 (cRaidTypeInline) are %s and every one is "
            "layout %d.  CORRECTED: sub_1C3CC's gate is the write command's "
            "raidOpCode & 0xF == 2, NOT the raid type -- call site "
            "efc_cpu2.c:38735 sits inside `if (Mso_GetRaidType == "
            "cRaidTypeMso)`, so raidType-1 MSOs carry the marker too -- "
            "including the %d layout-2 ones (msoIds >= 32: %s), where it "
            "COLLIDES with the top byte of AU0's u16 sequence shard and reads "
            "as `shard | 0x8000`.  Nothing here shows that: in Inline mode the "
            "marker is on slot 0 of 847/847 pages, an Mso stripe's column 0 is "
            "`4*ce+q` with ce 0, and CE0 is dead -- so 0 of the 191 Mso data "
            "pages this store holds (columns 1..47) carries it.  A one-sided "
            "check, and said so."
            % (inline, LAYOUT_INTERNAL_DATA, len(msoo), msoo[:12]))

    pool = f.rebuild_sequence_number_pool()
    stats = _media_census(f, dev, lays, budget)
    for lay in LAYOUTS:
        s = stats.get(lay)
        if not s or not s["pages"]:
            _report("layout %d on media" % lay, "SKIPPED",
                    "%d pages of a layout-%d MSO are in this store and NONE "
                    "decodes with four clean MCRCs, so the layout is a real "
                    "negative here, not a disproof (CE0 excluded: dead but "
                    "answering)" % ((s or {}).get("seen", 0), lay))
            continue
        ok = not s["bad"] and (lay == LAYOUT_RAID_PARITY or s["exact"] == s["pages"])
        fails += not ok
        _report("layout %d on media" % lay, "PASS" if ok else "FAIL", s["msg"])

    p3 = stats.get(LAYOUT_RAID_PARITY) or {}
    if p3.get("pages"):
        ok = p3["cc_split"] == 0 and p3["cc_both"] and p3["cc_neither"]
        fails += not ok
        _report("layout 3: carried +0 == 0 <=> a real stripeId",
                "PASS" if ok else "FAIL",
                "parity_stripe_complete and the AU2 stripeId field agree on "
                "%d of %d clean pages -- %d complete AND addressed, %d "
                "incomplete AND INVALID_STRIPE_ID, %d split.  TWO conditions "
                "that USUALLY coincide, not one: MOH copies the aux out of the "
                "parity accumulator while sub_1C8D8 stamps the stripeId out of "
                "the ParityManager's list (efc_cpu2.c:37432-37450, "
                ":43186-43196, :33975-33980), and 14 pages of WERU 592 outside "
                "this store are accumulated with NO stripeId -- so a zero split "
                "here is a property of what the store holds, not a law"
                % (p3["cc_agree"], p3["pages"], p3["cc_both"],
                   p3["cc_neither"], p3["cc_split"]))
    else:
        _report("layout 3: carried +0 == 0 <=> a real stripeId", "SKIPPED",
                "no clean layout-3 page in this store")

    if p3.get("acc"):
        ok = (p3["mk_set"] == p3["acc"]
              and p3["au0_exact"] == p3["acc"]
              and p3["mk_split"] == 0)
        fails += not ok
        _report("layout 3: bit 31 is the accumulated parity marker",
                "PASS" if ok else "FAIL",
                "on the %d accumulated pages (parity_stripe_complete True) "
                "parity_marker is True on %d, AU0 and AU1 never disagree (%d "
                "split), and AU0's whole second dword reads EXACTLY 0x%08X on "
                "%d -- the only value the model allows, since every member's "
                "AU0 dword is (seq>>31)|marker<<31 and no counter here reaches "
                "2**31.  On the %d UNACCUMULATED pages those bytes are stale: "
                "AU0 lands in {0, 0x%08X} on %d of them and AU0/AU1 agree on "
                "%d, which is chance.  sub_1C3CC (efc_cpu2.c:42935-42957) is "
                "the bit's ONLY writer and writes it on DATA AUs only; the ECU "
                "RAID accumulator (ECC_RAID_EN, RegEcu.h:623; RAID_CMD3 = 8 at "
                "efc_cpu1.c:20869) XORs the 8 aux bytes with the 4096 user "
                "bytes and carries it in"
                % (p3["acc"], p3["mk_set"], p3["mk_split"],
                   AUX_FLAG_RAID_PARITY_MARKER, p3["au0_exact"],
                   p3["unacc"], AUX_FLAG_RAID_PARITY_MARKER,
                   p3["au0_exact_un"], p3["mk_agree_un"]))
    else:
        _report("layout 3: bit 31 is the accumulated parity marker", "SKIPPED",
                "no accumulated layout-3 page in this store")

    # The carried AU1 dword: the members' sequence shards XOR-ed.  The FULL
    # member-by-member prediction lives in `rj_carry.py` and in
    # `titania.raid`'s media section (it needs the stripe machinery); what is
    # checkable from the parity pages alone is the shape the XOR must have.
    nm = "layout 3: AU1's carried dword is a sequence XOR"
    if p3.get("acc"):
        low = p3["au1_low"]
        bad = [v for v in low if v % 16]
        ok = not bad
        fails += not ok
        _report(nm, "PASS" if ok else "FAIL",
                "%d accumulated pages; %d carry low bits beyond the marker "
                "(%s) and EVERY one is a multiple of 16 (%d exceptions).  That "
                "is forced: the 48 members of a stripe are 48 pages whose "
                "sequence numbers run over three complete low-nibble cycles, "
                "and XOR(0..15) == 0.  The value itself is the XOR of those 48 "
                "sequences -- predicted member by member on four real stripes "
                "by rj_carry.py, 4/4, including 0x800000d0 on weru 234 page "
                "539 and 0x80000070 on weru 579 page 76 where 37 of the 48 "
                "columns came from the fitted lattice and every shifted "
                "lattice failed."
                % (len(low), sum(1 for v in low if v),
                   sorted(set("%#x" % v for v in low if v)) or "none",
                   len(bad)))
    else:
        _report(nm, "SKIPPED", "no accumulated layout-3 page in this store")

    # The media half of the seven-bit question.  It cannot PASS: a media answer
    # needs a page whose sequence crosses 2**32, and the drive has none.  Report
    # the count so the negative is measured on this store rather than quoted.
    nm = "layout 3 seq[38:32] on media: no witness"
    if p3.get("pages"):
        _report(nm, "SKIPPED" if not p3["hi7_nz"] else
                ("PASS" if not p3["seq_over32"] else "FAIL"),
                "%d of %d clean layout-3 pages populate AU2's low 7 bits "
                "(largest value %d), and %d carry a sequence at or above 2**32."
                "  With the field zero the writer's packing and the firmware "
                "reader's give the SAME number, so no page on this drive can "
                "choose between them -- which is why this stayed open and why "
                "the disassembly check above, not the media, is what settles "
                "it.  The bound is structural as well as observed: the six "
                "rebuildSequenceNumberPool counters are all below 2**31."
                % (p3["hi7_nz"], p3["pages"], p3["hi7_max"], p3["seq_over32"]))
    else:
        _report(nm, "SKIPPED", "no clean layout-3 page in this store")

    # ---- the layout-aware acceptance test ----------------------------------
    seen = [l for l in LAYOUTS if (stats.get(l) or {}).get("pages")]
    if seen:
        rej = {l: stats[l]["ok_no"] for l in seen}
        old = {l: stats[l]["old_reject"] for l in seen}
        ok = not any(rej.values())
        fails += not ok
        _report("read_is_plausible accepts every clean FTL page",
                "PASS" if ok else "FAIL",
                "MCRC-clean pages per layout %s; over the FTL-ADDRESSED region "
                "`read_is_plausible` rejects %s of them, where the blanket "
                "'four consecutive LMAs' test rejects %s.  The %d layout-3 "
                "pages CANNOT pass that test -- sub_1C8D8 never writes an LMA "
                "array there -- and rejecting them is what withheld 82 "
                "cleanly-read parity pages (246 reads, MCRC 4/4 on all 246).  "
                "The %d non-parity ones it also rescues are all-sentinel: "
                "sub_1C238's pad (efc_cpu2.c:42856) and the msoType-6 redirect "
                "tags (:43109)."
                % ({l: stats[l]["pages"] for l in seen}, rej, old,
                   (stats.get(LAYOUT_RAID_PARITY) or {}).get("pages", 0),
                   sum(stats[l]["sentinel"] for l in seen)))

        # The firmware area is excluded above, and this is the reason -- stated
        # as its own result rather than hidden in a filter.
        fwp = sum(stats[l]["fw_pages"] for l in seen)
        fwn = sum(stats[l]["fw_no"] for l in seen)
        if not fwp:
            _report("the firmware area needs the REGION, not the layout",
                    "SKIPPED", "no clean page of blocks 6..9 in this sample")
        else:
            ok = fwn == fwp
            fails += not ok
            _report("the firmware area needs the REGION, not the layout",
                    "PASS" if ok else "FAIL",
                    "%d of %d MCRC-clean pages in blocks %s are refused by the "
                    "aux rule and SHOULD be: msoId 57 NeoFwPartition declares "
                    "aux layout %d, but the firmware area is addressed by raw "
                    "CE/block/page through FwDataRw.c and carries no LMA array "
                    "-- every one of these pages holds four EQUAL non-LMA "
                    "dwords.  An aux-only predicate cannot know that, so the "
                    "exemption belongs to `geometry.region` "
                    "(%s), which is where titania.verdict applies it.  This "
                    "module deliberately does NOT special-case an msoId."
                    % (fwn, fwp, list(geom.FW_GROUP01_BLOCKS),
                       LAYOUT_INTERNAL_DATA, geom.REGION_FW_GROUP01))

        # A control, so "accepts everything" cannot be how it passes.  Random
        # aux bytes are what a wrong-mode or misaddressed read leaves behind.
        import random
        rnd = random.Random(0x1C438)
        bad_ctl = {}
        for lay in LAYOUTS:
            acc = 0
            for _ in range(2000):
                a = [bytes(rnd.randrange(256) for _ in range(AUX_BYTES))
                     for _ in range(AUS_PER_PAGE)]
                acc += read_is_plausible(a, lay) is not False
            bad_ctl[lay] = acc
        ok = not any(bad_ctl.values())
        fails += not ok
        _report("... and rejects random aux on every layout",
                "PASS" if ok else "FAIL",
                "2000 pages of random aux per layout, accepted counts %s.  "
                "Layouts 0/1/2 need four consecutive LMAs or four sentinels; "
                "layout 3 needs `all four carried +0 dwords zero` to AGREE "
                "with `AU2's top 25 bits all ones`, and random bytes satisfy "
                "neither, so they land 'not accumulated but addressed' and are "
                "refused." % bad_ctl)
    else:
        _report("read_is_plausible accepts every clean page", "SKIPPED",
                "no clean page of any layout in this store")

    if pool:
        bad = [(l, s["max_seq"]) for l, s in stats.items()
               if s["seqs"] and not any(
                   sequence_is_plausible(s["max_seq"], g, pool, l) for g in
                   range(len(pool)))]
        fails += bool(bad)
        _report("sequence <= its msoGroup's pool bound",
                "PASS" if not bad else "FAIL",
                "pool `next` per msoGroupId is %s; every layout's largest "
                "observed sequence fits under one of them.  Layouts 0/1/3 "
                "stamp-then-increment so they land strictly below; layout 2 "
                "never increments and lands exactly ON its group's next."
                % [p[0] for p in pool])

    two = stats.get(LAYOUT_NSID23) or {}
    _report("layout 2 shard ORDER on media", "SKIPPED",
            "AU0/AU1/AU2 shards are zero on all %d clean layout-2 pages here "
            "because every sequence on this board is below 65536, so only AU3's "
            "position is demonstrated.  The big-endian order rests on the "
            "reader at efc_cpu2.c:137337-137342 alone." % two.get("pages", 0))
    _report("layout 1 has a firmware name", "SKIPPED",
            "efc_cpu0/1/2/3.c contain exactly three cAuxLayout* strings and "
            "none is layout 1; LAYOUT_NAMES[1] stays None rather than becoming "
            "a plausible guess.")
    return 1 if fails else 0


def _media_census(f, dev, lays, budget):
    """Decode up to `budget` pages per layout and check the field model.

    Kept private and bounded: a full pass is 55 000 pages and native-TLC pages
    need LDPC correction, which is ~100x a plain decode.
    """
    import collections
    F = _sib("ftl")
    codec = F.default_codec()
    owners = f.weru_owners()
    bm = f.block_map()
    pos, dup = {}, set()
    for weru, slots in bm.items():
        own = owners.get(weru)
        if isinstance(own, tuple):
            own = own[0]
        if own is None or isinstance(own, type(F.UNKNOWN)):
            continue
        for slot, bc in enumerate(slots or []):
            if bc is None:
                continue
            if bc in pos and pos[bc][0] != weru:
                dup.add(bc)
            pos.setdefault(bc, (weru, slot, own))
    for bc in dup:
        pos.pop(bc, None)

    groups = collections.defaultdict(list)
    for (c, b, p, s) in dev.entries:
        groups[(c, b, s)].append(p)

    per = {l: {"seen": 0, "pages": 0, "bad": 0, "exact": 0, "consec": 0,
               "seqs": [], "flag": 0, "msos": set(), "lps": set(), "torn": 0,
               "cc_agree": 0, "cc_both": 0, "cc_neither": 0, "cc_split": 0,
               "acc": 0, "unacc": 0, "mk_set": 0, "mk_split": 0,
               # layout 3 only: how many pages populate AU2's low 7 bits, the
               # field whose home the writer and the firmware's reader disagree
               # about.  A non-zero count is the only thing media can add here.
               "hi7_nz": 0, "hi7_max": 0, "seq_over32": 0,
               # the carried AU1 dword's low 31 bits: the members' sequence
               # shards XOR-ed.  A XOR over 48 members whose low nibbles each
               # repeat three times must be a multiple of 16.
               "au1_low": [],
               "au0_exact": 0, "au0_exact_un": 0, "mk_agree_un": 0,
               # The layout-aware acceptance test, and what the old blanket
               # consecutive-LMA test would have said about the same pages.
               # `ok_*` covers the FTL-addressed region only; the firmware area
               # is counted apart because its aux is not an LMA array at all --
               # see the check in `selftest`.
               "ok_yes": 0, "ok_no": 0, "ok_unk": 0, "sentinel": 0,
               "old_reject": 0, "fw_pages": 0, "fw_no": 0}
           for l in LAYOUTS}

    # Bucket by layout first and give every (ce, block) group an equal share of
    # the budget.  Walking the store in address order instead spends the whole
    # layout-0 budget on the first few WERUs -- msoIds 56 and 57, which carry no
    # addresses and no parity flags -- and the run then reports a clean PASS
    # having never looked at 40/41/42.  A budget that samples one corner is
    # worse than no budget, because it looks like coverage.
    todo = {l: [] for l in LAYOUTS}
    for (ce, block, slc), pages in sorted(groups.items()):
        if ce in geom.CE_DEAD:                  # dead but answering
            continue
        pl = pos.get((block, ce))
        if pl is None:
            continue
        weru, slot, own = pl
        lay = lays.get(own)
        if lay is not None:
            todo[lay].append((ce, block, slc, sorted(pages), weru, own))

    for lay, groups_for_layout in todo.items():
        if not groups_for_layout:
            continue
        quota = max(1, budget // len(groups_for_layout))
        s = per[lay]
        for (ce, block, slc, pages, weru, own) in groups_for_layout:
            took = 0
            for page in pages:
                if s["pages"] >= budget or took >= quota:
                    break
                took += 1
                s["seen"] += 1
                try:
                    raw = dev.read_page(ce, block, page, slc=slc)
                    if raw is None:
                        continue
                    aus, ok = codec.decode(raw, block, page)
                except Exception:
                    continue
                if aus is None or ok is F.UNKNOWN or not all(ok):
                    continue
                auxes = [bytes(a[:AUX_BYTES]) for a in aus]
                rec = parse_page(auxes, lay)
                s["pages"] += 1
                s["msos"].add(own)
                if rec.sequence is not UNKNOWN:
                    s["seqs"].append(rec.sequence)
                s["flag"] += rec.inline_parity is True
                s["torn"] += rec.torn is True
                s["consec"] += rec.lmas_consecutive is True
                acc = read_is_plausible(auxes, lay)
                s["old_reject"] += rec.lmas_consecutive is not True
                if geom.region(ce, block) == geom.REGION_FTL:
                    s["ok_yes"] += acc is True
                    s["ok_no"] += acc is False
                    s["ok_unk"] += acc is UNKNOWN
                else:
                    s["fw_pages"] += 1
                    s["fw_no"] += acc is False
                if lay != LAYOUT_RAID_PARITY:
                    s["sentinel"] += all(
                        e.lma in LMA_SENTINELS for e in rec.entries)
                if lay == LAYOUT_NSID23 and rec.lp_id is not UNKNOWN:
                    s["lps"].add(rec.lp_id)
                if lay == LAYOUT_RAID_PARITY:
                    # Does any page on this drive populate the disputed 7 bits?
                    # If one did, the writer's and the reader's packings would
                    # give different numbers and the media would decide between
                    # them on its own.  Counted, never assumed.
                    hi7 = rec.entries[PARITY_AUX_STRIPE_AU].seq_shard \
                        if len(rec.entries) > PARITY_AUX_STRIPE_AU else UNKNOWN
                    if isinstance(hi7, int):
                        s["hi7_nz"] += hi7 != 0
                        s["hi7_max"] = max(s["hi7_max"], hi7)
                    if isinstance(rec.sequence, int):
                        s["seq_over32"] += rec.sequence >= (1 << 32)

                    # The completeness test against the stripeId it predicts.
                    done = parity_stripe_complete(auxes)
                    addressed = (rec.stripe_id is not UNKNOWN
                                 and rec.stripe_id != INVALID_STRIPE_ID)
                    if done is True and addressed:
                        s["cc_both"] += 1
                        s["cc_agree"] += 1
                    elif done is False and not addressed:
                        s["cc_neither"] += 1
                        s["cc_agree"] += 1
                    else:
                        s["cc_split"] += 1

                    # bit 31 = the accumulated RAID parity marker.  Split by
                    # whether the buffer was accumulated at all: on a stale one
                    # these four bytes are somebody else's content and any
                    # agreement is chance -- which is exactly the control the
                    # claim needs, so both sides are counted.
                    mk = parity_marker(auxes)
                    au0 = struct.unpack_from("<I", auxes[0], 4)[0]
                    if done is True:
                        s["acc"] += 1
                        s["mk_set"] += mk is True
                        s["mk_split"] += mk is UNKNOWN
                        s["au0_exact"] += au0 == AUX_FLAG_RAID_PARITY_MARKER
                        s["au1_low"].append(
                            struct.unpack_from("<I", auxes[1], 4)[0]
                            & ~AUX_FLAG_RAID_PARITY_MARKER)
                    else:
                        s["unacc"] += 1
                        s["mk_agree_un"] += mk is not UNKNOWN
                        s["au0_exact_un"] += au0 in (0,
                                                     AUX_FLAG_RAID_PARITY_MARKER)

                # The real test: put the decoded fields back and demand the
                # original 32 bytes.  A field this module has not accounted
                # for shows up here as a mismatch, not as a plausible number.
                try:
                    if lay in (LAYOUT_INTERNAL_DATA, LAYOUT_1):
                        back = encode_page(
                            lay, lmas=rec.lmas, sequence=rec.sequence,
                            inline_parity=bool(rec.inline_parity))
                    elif lay == LAYOUT_NSID23:
                        back = encode_page(lay, lmas=rec.lmas,
                                           sequence=rec.sequence,
                                           lp_id=rec.lp_id)
                    else:               # layout 3 keeps its undecoded carry
                        back = encode_page(lay, sequence=rec.sequence,
                                           stripe_id=rec.stripe_id,
                                           carry=auxes)
                except (TypeError, ValueError, struct.error):
                    back = None         # a field is UNKNOWN: not a match
                if back == auxes:
                    s["exact"] += 1
                else:
                    s["bad"] += 1

    for lay, s in per.items():
        seqs = s["seqs"]
        s["max_seq"] = max(seqs) if seqs else None
        extra = ""
        if lay in (LAYOUT_INTERNAL_DATA, LAYOUT_1):
            extra = ("; %d/%d have four consecutive LMAs; %d carry the "
                     "inline-parity flag; %d torn (AU0/AU1 LMA disagree)"
                     % (s["consec"], s["pages"], s["flag"], s["torn"]))
        elif lay == LAYOUT_NSID23:
            extra = ("; %d/%d have four consecutive LMAs; lpIds seen %s"
                     % (s["consec"], s["pages"], sorted(s["lps"])))
        else:
            extra = ("; the undecoded bytes are carried through verbatim, so "
                     "the re-encode is not evidence for THIS layout; "
                     "parity_stripe_complete agrees with the stripeId on "
                     "%d/%d (%d complete+addressed, %d incomplete+INVALID, "
                     "%d split)"
                     % (s["cc_agree"], s["pages"], s["cc_both"],
                        s["cc_neither"], s["cc_split"]))
        s["msg"] = ("%d of %d pages decode MCRC-clean over msoIds %s; %d "
                    "re-encode BYTE-IDENTICALLY from the decoded fields, %d do "
                    "not; sequences [%s, %s]%s"
                    % (s["pages"], s["seen"], sorted(s["msos"]), s["exact"],
                       s["bad"], min(seqs) if seqs else "-",
                       max(seqs) if seqs else "-", extra))
    return per


if __name__ == "__main__":                              # pragma: no cover
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest(sys.argv[1:]))
    if "--summary" in sys.argv[1:]:
        try:
            cli = _sib("cli")
            F = _sib("ftl")
            dev = cli.open_device(None)
            f = F.Ftl.from_device(dev)
            print(summary(f, f.rebuild_sequence_number_pool()))
        except Exception as exc:
            print(summary(None))
            print("\n(no board: %r)" % (exc,))
        sys.exit(0)
    print(__doc__)
