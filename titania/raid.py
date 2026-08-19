"""The RAID layer: where the parity is, and what it can put back.

Every other layer in this package reads what is *readable*.  This one is the
only thing that recovers what is not: an AU the LDPC cannot correct, an AU on
the dead CE0, an AU in a block that never dumped.

    from titania import DumpDevice
    from titania.ftl import WERU_CONFIGS
    from titania import raid

    dev = DumpDevice()
    cfg = WERU_CONFIGS[20]                       # an Inline-RAID WERU
    st  = raid.inline_stripe(cfg, 20, page=137, au=2)
    st.members                                   # the 11 data placements
    st.parity                                    # the parity placement
    raid.rebuild_page(dev, cfg, ce=8, block=11, page=137)   # -> 18432 bytes

WHAT IS ESTABLISHED, AND HOW
----------------------------
Titania keeps parity in one of three modes, per MSO, from
``msoDescriptor[msoId] + 16`` (`Mso_GetRaidType`, `efc_cpu2.c:43749-43752`):

    0  cRaidTypeNone     no parity at all         (efc_cpu2.c:38744-38746)
    1  cRaidTypeMso      parity in a separate MSO (efc_cpu2.c:38717-38720)
    2  cRaidTypeInline   parity inside the WERU   (efc_cpu2.c:39035-39036)

On this board the Inline MSOs are exactly 40 (Lut/System), 41
(SystemMetadata/System), 42 (Trim/System) and 58 (PhysicalMediaAccess/System);
the ParityData-group MSOs are themselves cRaidTypeNone, as they must be.

The msoIdTable is `msoId = table[msoType*6 + msoGroupId]` over 17 types and 6
groups (`Mso_GetMsoId`, `efc_cpu2.c:46773-46777`, `:99223`; group names
`efc_cpu3.c:4104-4157`).  Group 0 is System, not MediaSpace0 -- the groups
run System, MediaSpace0-NSID1, MediaSpace1-NSID2, MediaSpace2-NSID3, Unused,
ParityData -- and the types run UserData, UserData 2ndWF, GarbageCollection,
StaticMan, Lut, SystemMetadata, Trim, Migration, UserDataNandCache, GcNandCache,
UserSlcData, CopySlcData, BlockMove, DirectTlcWrite, RootStorage,
NeoFwPartition, PhysicalMediaAccess.  Column 5 of that table on this board is
{34, 35, 39, 54, 62} = UserData/, UserData2ndWF/, StaticMan/, BlockMove/ and
PhysicalMediaAccess/ParityData -- 62 is the raw-access alias for the parity
group (`byte_900060[g] == Mso_GetMsoId(PhysicalMediaAccess, g)`,
`efc_cpu2.c:142047`), not a fifth parity partition.  The firmware's own
device-wide parity MSO is `byte_900005 == Mso_GetMsoId(UserData, ParityData)`
== 34, used unconditionally by `LastContiguousWmaParity` (`efc_cpu2.c:79192`)
and `RebuildParityLut` (`efc_cpu2.c:133111`), with `byte_90000B` == 35 for the
second write front.

Inline is solved and MEASURED.  A metadata WERU is firmware configuration 2
(`sub_4033CB5E`, `efc_cpu2.c:107815`): 26 WERUs from firstWeru 3,
blocksPerWeru 12, blocksPerCe 2, pseudo-SLC -- so twelve blocks over six
consecutive CEs.  Eleven slots are WMA-addressable; the twelfth is not.  The
measured relation is

    XOR over all twelve slots of AU(slot, page, au)  ==  0

per (page, au), which is the same thing as saying slot 11 is the parity of the
other eleven.

Measured on WERUs 14, 20 and 22 (three independent regions, 12 physical blocks
each): every held-out single-slot reconstruction is byte-exact over all 4608
stored bytes except on the handful of pages carrying a raw NAND bit error, and
every one of those differs by exactly one byte and satisfies the relation
exactly after LDPC correction.  At the plaintext layer the relation is exact
everywhere tested, aux included.  `--selftest` prints the current counts.

The relation is symmetric, and that is what makes it useful.  What is
measured is `XOR over all twelve blocks == 0`, so any one of the twelve can be
rebuilt from the other eleven; nothing in the XOR itself says which one is "the
parity".  Recovery does not depend on that identification at all.

And there is exactly one such relation -- this is the load-bearing negative.
Two slots, 0 and 11, carry the aux parity flag on 164/164 decoded AUs each while
the other ten carry it on 0-4/164 (measured here on werus 20 and 22), which
invites the reading "10 data + 2 parity, so two erasures are correctable".  That
reading is wrong.  Taking the twelve pages as vectors over GF(2) and computing
the kernel settles it: on the pages where the kernel is exactly one-dimensional
(4 of 368 in weru 20, 7 of 368 in weru 22) the sole relation is over all
twelve slots.  A genuine second parity would hold on every page and force
kernel dimension >= 2 everywhere; the higher dimensions seen on other pages are
artefacts of LUT content being XOR-structured and are not stable page to page.

    One parity  ->  one erasure recoverable.  Two is not a partial answer.

Why two slots are flagged -- solved 2026-08-06, and it was never a second
parity.  There is one marker per stripe and it is written on a DATA member;
the parity member's copy is the XOR accumulator's output, not a second write.

`sub_1C3CC` (`efc_cpu2.c:42935-42957`) is the only writer of aux byte-7 bit-7 in
efc_cpu0/1/2/3.c, and both its callers (`:37515`, `:38735`) require
`Mso_GetRaidType(msoId) != cRaidTypeNone` -- so it fires on DATA writes only.
The ECU RAID engine accumulates the format-7/8 message, which is 4096 user bytes
plus the 8 aux bytes, so the parity AU inherits `XOR of the members' markers`.
Measured over 847 pages of werus 14/19/20/22/23 whose twelve slots all decode
(`pq_bit31.py`):

    slot 0 flagged on 847/847 (where readable), slots 1..10 on 1-6 of 368 each
    #flagged among the eleven data slots is odd on 847/847 (1, 3, 5, 7, 9)
    slot 11's flag == XOR of the eleven data slots' -- 847/847, 0 exceptions

That identity is forced by the twelve-way XOR being exactly zero: were slot 11's
bit an independent OR, the relation would fail in byte 7, and it does not.  See
`titania.auxdata` for the full ECU model (`ECC_RAID_EN`, RAID_CMD 8 / 0xB / 0xC,
`dramSwapIn` / `dramSwapOut` / `sramReset`) and for the layout-3 readout of the
same bit.

This RETRACTS "Why two slots are flagged when only one relation exists is not
established."  The kernel result above is untouched: still one relation, still
one erasure.

This RETRACTS `ftl.WeruConfig`'s "the twelfth BLOCK is unexplained ... whether
it is a mirror, a journal, RAID parity or something else is not established".
It is RAID parity, the WERU's owner msoId 40 reads cRaidTypeInline, and its aux
carries the parity flag on every decoded AU.

A test that looks decisive and is vacuous -- do not repeat it
------------------------------------------------------------
EE3 is linear and both AU MCRCs run over complemented stored bytes, so for an
odd number of operands the complement cancels and the XOR of any odd number of
valid AUs has zero syndromes and a valid MCRC, stripe or no stripe.  The aux is
linear too, so such a XOR also always yields four consecutive LMAs (the low two
bits are the au index, surviving once).  "I XORed eleven pages and got a clean
codeword with consecutive LMAs" is therefore evidence of nothing.  Any subset
test must use an even member count, or compare against bytes that are
independently known.

TWO LAYERS, AND WHEN EACH IS LEGAL
----------------------------------
The parity holds over the *plaintext* -- 8 aux + 4096 user bytes -- and that
statement is always true.  It also holds byte-for-byte over the raw stored
bytes, including MCRC and both EE3 parities, but only under a condition this
module checks rather than assumes:

    the scrambler seed is a function of the row and of nothing else, so XOR-ing
    a set of stored AUs XORs their keystreams; the keystreams cancel exactly
    when every row occurs an even number of times in the full stripe.

For the SLC WERU shape (2 blocks on each of 6 CEs) each of the two rows occurs
six times, so removing any single member leaves that member's own row an odd
number of times and the stored-layer XOR reproduces it exactly.  That is why
`rebuild_page` can work on raw bytes and needs no seed, no ECC and no keys.
`stored_layer_ok` reports the condition; when it is False, use
`rebuild_message`, which is unconditional.

cRaidTypeMso -- solved: the parity is a LUT lookup, keyed by stripeId
---------------------------------------------------------------------
`Raid_GetStripeIdForWma` / `Raid_GetWmaForStripeId` are an exact inverse pair
(`efc_cpu2.c:34167-34189`, thunks `:44940` and `:45636`):

    P = wmasPerPage   = pageSizeBytes >> 12   (`efc_cpu2.c:104016`)  = 4 here
    C = columns       = word_90C458 * byte_90C4E0 (`:103991`, `:104026`) = 48

    stripeId          = (wma // (P*C)) * P
    firstWmaOfStripe  = (stripeId // P) * P * C
    column            = (wma // P) % C           (`efc_cpu2.c:143820`)
    auIndex           = wma % P
    stripeBaseWma     = wma & ~(P-1)             (`efc_cpu2.c:142051`)

`P*C = 192` is `ftl.WMA_STRIDE` and `column` is `ftl.split_wma`'s slot, arrived
at by a completely independent route -- so a stripe is one page across every
column of the WERU, the same shape the Inline case was measured to have.  The
assert `(raidStripeId % AddressMapping_GetNumberOfWmasPerPage()) == 0`
(`efc_cpu2.c:33967`) is just `stripeId` being a multiple of P.

    stripeCount = numberOfStripesInWriteableUnit(pageBlockMode) * P * planes
    (`efc_cpu2.c:26085-26088`), packed into `nandCommand+27` bits [5:0] with
    parityMode in [7:6] (`efc_cpu2.c:37519-37530`), so at most 63.

The placement RULE.  A parity AU is an ordinary LUT-addressed AU, and its
LMA *is* the stripeId:

    stripeLma(wma) = stripeId(wma) + wma % P
                   = 4 * (wma // 192) + wma % 4
    parityWma      = lut4[stripeLma >> 2] + (stripeLma & 3)
                   = Ftl.lma_to_wma(<any ParityData msoId>, stripeLma)

`lut4` is the lutId-4 LMA space -- the one all four group-5 (ParityData) MSOs
34/35/39/54 share, exactly as the seven group-1 MSOs share lutId 1 (the LUT is
selected by `msoDescriptor` byte +3 and indexed `pTable[lma >> shift]`,
`Lut_GetEntry` = `sub_8058`, `efc_cpu2.c:23027-23032`).  Nothing here is fitted;
the four independent things that pin it are:

  1. The rebuild reads the stripeId out of the parity AU's own aux.
     `sub_4035684E` (`Rebuild.c`, `efc_cpu2.c:134076`) is the rebuild's aux-scan
     iterator; it reads `auxLayout` at `:134109` and branches on `== 3`
     (`:134161`, `:134189`; `cAuxLayoutForRaidParityMso`).  On that branch the
     "LMA" it hands back at `:134192` is

         lma = (auxTag[ sub_40355C4C(i, wma) ] >> 7) + wma % P   `:134170`, `:134175`
         sub_40355C4C(i, wma) = i - (wma % wmasPerPage) + 2      `:133310-133313`

     i.e. bits [31:7] of the tag dword of AU 2 of the parity page, plus the
     AU index.  `0x1FFFFFF` in that field is INVALID_STRIPE_ID -- the same bound
     the ParityManager asserts against (`"stripeIdLast < INVALID_STRIPE_ID"`,
     ParityManager.c:309, `efc_cpu2.c:139457`) -- and invalidating a parity AU is
     `*p |= 0xFFFFFF80` (`:133787`), which is precisely "set that 25-bit field to
     INVALID".
  2. The write puts the stripeId there.  `sub_1C8D8` (`WriteAuxiliaryList.c`,
     `efc_cpu2.c:43164`) walks a caller-supplied list and stores
     `tag[k + 2] = (tag & 0x7F) | (list[k] << 7)` (`:43197`), with
     `tag[k + 2] & 0x7F` taking the high 7 bits of the rebuild sequence number
     (`:43198-43199`) and `tag[k + 3]` its low 32 (`:43200`).  The list it is
     given is `parityBuffer.pStripeIdList`, which `sub_12D58` fills with
     consecutive stripeIds starting at `Raid_GetStripeIdForWma(wma)`
     (`efc_cpu2.c:33945`, assert `ParityManager.c:748` at `:33968`, fill loop
     `:33975-33980`).  So write and rebuild are the same field, read the same way.
  3. The table's size is the stripeId space, exactly.  On this board's own
     media the lutId-4 region is `base LMA 10824` in msoId 40's LMA space,
     `granularity 4`, `1073088` entries, `maxLma 4292351`.  And

         4 * (WERU_COUNT_IN_VALID_MAP * 211968 // 192) == 4292352 == maxLma + 1
         4292352 // 4                                 == 1073088 == n_entries

     with 972 = `dword_907164` (`efc_cpu2.c:108038`, the sum of the first five
     WERU configurations), the same WERU count that sizes validMap.  A region
     sized to the stripeId space to the last entry is not a coincidence;
     `selftest` checks that identity.
  4. The media agrees with the table, on ten pages and never once wrongly.
     For every parity page held in native-TLC mode that carries the parity aux
     shape, the stripeId in its own aux is the stripeId the lutId-4 table filed
     it under: 10 exact, 0 wrong, plus 6 whose stripeId the table maps
     nowhere -- which is a superseded parity AU, the ordinary state of a
     log-structured store, not a disagreement.  A 25-bit field agreeing with an
     independently-derived stripeId is 1 in 2^25 per page.  `selftest` runs it
     with the controls -- no neighbouring stripe (+-1 stripe, +-1 page) and no
     other held parity page resolves to the same address, 0 and 0.

     Nine of those ten are new on 2026-08-06 and were read for this purpose: the
     eight page-0 parity AUs of blocks 3596-3599 on CE1 and CE6, and
     ce1/blk3674/page 2, whose aux reads stripeId 2530368 == 4 * (573*1104 +
     0) exactly -- the stripe of weru 573 page 0, whose 44 live data columns
     were read in the same session and all decode MCRC 4/4.  It was 2 before.
  5. Every live entry in the whole table is a parity AU.  All 15737 of them:
     0 not granule-aligned, 0 target WMAs claimed twice, 0 landing outside a
     ParityData-owned WERU (owners hit 34 x13681, 35 x74, 54 x1982, all inside
     the auxLayout-3 set).  If this LMA space were anything other than the
     stripeId space, a table of 1073088 slots would not have that shape.

Which PARITY MSO covers which DATA MSO?  None of them -- the question is
malformed, and that is what resolves the msoIdTable anomaly.  There is one
parity LMA space for the whole drive, keyed by a stripeId that is itself global
(`stripeId = 4 * (wma // 192)` over the global WMA space).  msoIds 34/35/39/54
are *write fronts* into that one space, not per-partition parity partitions:
`byte_900005 == Mso_GetMsoId(UserData, ParityData)` == 34 is the front
`RebuildParityLut` is invoked on (`efc_cpu2.c:133111`, `efc_cpu3.c:39977`) and
`byte_90000B` == 35 the second, and the rebuild's own scan admits every msoId
whose lutId matches the front's (`sub_40355940`, `efc_cpu2.c:133121`, test at
`:133154`).

Measured on this board's own LUT, over all 15737 live entries, classifying each
end by its WERU's `weruDescriptor` owner:

    data msoId 43 (Migration/NSID1)  ->  parity msoId 34 :   601 stripes
    data msoId 43                    ->  parity msoId 54 :  1982
    data msoId 45 (UserDataNandCache)->  parity msoId 35 :    74
    data msoId 51 (CopySlcData/NSID3)->  parity msoId 34 :     4
    data msoId 53 (BlockMove/NSID2)  ->  parity msoId 34 : 13076

One data MSO (43) feeds two parity MSOs; one parity MSO (34) is fed by three
data MSOs; and parity WERU 592 alone carries stripes for data WERUs owned by
both 53 and 43.  The relation is many-to-many in both directions, so no
data-msoId -> parity-msoId function exists to be found.

    This RETRACTS "On this board the MSOs that actually own user WERUs -- 43/44,
    45, 48/51 -- sit in msoType rows whose msoIdTable column 5 is 0xFF, so the
    table names no parity MSO for them ... that contradiction is unresolved."
    There is no contradiction: column 5 of the msoIdTable was never the
    data -> parity map.  It names which ParityData MSO a given msoType's write
    front allocates from, and a data MSO not having one simply means it does not
    own a parity write front.  Its stripes are still covered, through the shared
    lutId-4 space.

"The validCounts show 1:1, not 48:1" -- settled, and 48:1 survives
-------------------------------------------------------------------
Weru 592 (parity, msoId 34) reads `validCount` 13222 and weru 579 (data,
msoId 43) reads 13219.  That looks like a 1:1 refutation of the 48:1 ratio
above.  It is not.  48:1 is right; the two numbers are simply not comparable,
for three independent reasons, any one of which alone breaks the comparison.
Measured offline with `ps_task1.py` / `ps_task1c.py` / `ps_task1d.py`.

  (a) the unit is not the same.  validCount counts LUT granules, and the
     granule differs by lutId.  A validMap bit is `wma % wmasPerWeru` and the
     counter is `++validCount[wma / wmasPerWeru]` -- one bit and one count per
     call (`sub_1025E`, `efc_cpu2.c:31290-31320`) -- so what the callers pass
     decides the unit, and the media shows it directly: every set validMap bit
     of a lutId-4 WERU is 4-aligned with minimum gap exactly 4, and every set bit
     of a lutId-1 WERU is 16-aligned with minimum gap exactly 16.  16 WERUs, 0
     exceptions (10 parity: 592/576/575/574/938/117/935/597/602/950; 6 data:
     579/234/573/590/936/84).  Those are precisely the two LUT granularities the
     media's own region descriptors declare -- lutId 4 gran 4, lutId 1/2/3 gran
     16 -- reached by a completely different route.  So

         weru 592: 13222 granules x  4 AUs =  52888 parity AUs (24.95% of the WERU)
         weru 579: 13219 granules x 16 AUs = 211504 data   AUs (99.78% of the WERU)

     A factor of four is hiding in the printed numbers.
  (b) they are not the pair.  A parity WERU serves 48 data WERUs, so per-WERU
     counts of the same magnitude are exactly what 48:1 predicts.  A parity
     WERU has `211968 // 4 == 52992` page slots and one page of parity closes one
     stripe, so it holds `52992 // 1104 == 48` data WERUs' worth.  Weru 592
     currently carries stripes for 14 data WERUs (577, 578, 580-590, 936) --
     eleven of them at the full 1104 pages, 587 at 932, 590 at 143 and 936 at 3,
     summing to exactly 13222.  The near-equality is then pure arithmetic:
     granules in one full data WERU `= 211968/16 = 13248` and parity pages for
     one full data WERU `= 211968/192 = 1104`, and `13248 == 12 * 1104` -- so a
     parity WERU holding twelve data WERUs' worth reads the same number as one
     full data WERU.  592 is at 13222 of 52992, i.e. 24.95% -- just under twelve
     forty-eighths.  The coincidence is the 48:1 ratio, seen sideways.
  (c) and they are not even connected.  Weru 592 carries none of weru 579's
     stripes: 0 of its 13222 entries name weru 579.  All 1104 of 579's stripes
     are in parity weru 576 (with 878 of weru 234's).  The full map is
     printed by `ps_task1d.py`.

    Mapped onto the four candidate explanations this question was posed with:
    the answer is possibility 2 -- "592 and 579 are not the pair implied" --
    primarily, which is (b); possibility 3 compounds it in the form (a);
    possibility 4 turns out to be literally true as well for this exact
    pair, which is (c); and possibility 1, "the 48:1 reading is wrong", is
    refuted -- see the next paragraph.  Possibility 3's original wording,
    "validCount counts something different for a ParityData MSO", is right in
    substance but wrong in attribution: validCount always counts live LUT
    granules; it is the granule size that differs, and it differs by lutId, not
    by ParityData-ness.  A lutId-2 MSO is different again -- see the caveat
    below.

The ratio, MEASURED in one unit.  For a data WERU whose stripes are all
covered, its 1104 parity pages (4416 parity AUs) back the WERU's 211968 data
AUs: `211968 / 4416 = 48.0000`.  Twelve data WERUs on this board carry the
full 1104-entry complement -- 577, 578, 579, 580-586, 588, 589 -- and none of
the 21 exceeds it.  Against LIVE counts rather than capacity, weru 579 gives
`211504 / 4416 = 47.89`, the shortfall being its 29 dead granules.  Other data
WERUs give lower live ratios (234: 36.6, 573: 6.8) because parity AUs and data
AUs are invalidated independently -- a parity page stays live until its stripe
is rewritten, whatever happens to the 48 data pages under it.  Do not read a
per-WERU live ratio as the code rate.

A new and independent confirmation of the placement RULE, free from the
above.  For all ten parity WERUs, the set of validMap bits is exactly the set
of WMA offsets the lutId-4 table names there: 15737 of 15737 entries matched,
0 named-but-not-set, 0 set-but-not-named, 10 of 10 WERUs.  These are two
separate on-media structures -- the LUT is stored in msoId 40's LMA space, the
validMap is its own SystemMetadata section -- and msoId 34's `msoDescriptor +5`
is 0x0e, whose bit 0 is clear, so msoId 34 is not in the dirty-mount rebuild
set (`efc_cpu3.c:40322-40325`) and neither this module nor the firmware derived
one from the other.  Nothing but the placement rule makes them agree.

Caveats, so none of this is over-read.

  * The stored counts are a last-flushed snapshot: the board's dirty flag is set,
    so `ValidCountRead` is skipped at mount (`efc_cpu3.c:39739`, `:39752`) and no
    stage restores it.  It shows: validCount matches the validMap popcount
    exactly on 8 of the 10 parity WERUs, and drifts on two (575 reads 405 against
    399 bits, 574 reads 103 against 74).  The map is the better witness of the
    two -- it agrees with the LUT on all ten.
  * A lutId-2 validCount is in AUs, not granules, and that is a third unit.
    Werus 577/578/580-586/588/589 (msoId 53, lutId 2) read exactly 211968 --
    which is 16x their granule capacity and therefore cannot be granules.  It is
    the closed-full assignment `validCount[w] = GetCols() * wmasPerBlock`
    = 48 * 4416 (`efc_cpu2.c:27464`, `:27654`; `GetCols` = `word_90C458 *
    byte_90C4E0` = 48, `:70112`).  msoId 53's descriptor `+5` is 0x08 -- neither
    walk nor validMap -- so there is no map to cross-check it with, and its
    popcount reads 0, which means "no map maintained", not "empty".
    Never compare two validCounts without first checking both owners' lutId
    granularity and whether either WERU was closed full.

`ParityManager_OnAllocationRequest` (`efc_cpu2.c:33659`, printf at `:33727`)
allocates no media address, and that is still true: it hands the writeable unit a
DRAM parity AU buffer and a parity AUX buffer (`writeableUnit+10`/`+14` -> the
write command's auxList `+192`/`+196`, `efc_cpu2.c:38730-38732`) and the XOR
accumulates there across the stripe.  The address is assigned only when the
completed parity AU is written out through a ParityData write front, and *that*
write is what stamps the stripeId into the aux and the WMA into lutId 4.

Two more things about parity AUs are on-media checkable, and this module uses
them:

  * `Mso_GetAuxLayout(msoId)` is msoDescriptor byte +17
    (`efc_cpu2.c:133444-133446`, Rebuild.c:1920; `:134109-134112`,
    Rebuild.c:1757), and `cAuxLayoutForRaidParityMso == 3`.  On this board's own
    msoDescriptor that is exactly msoIds 34, 35, 39, 54 and 62 -- the same
    five that column 5 of the msoIdTable names, reached independently and with
    no firmware image involved.  62 belongs in that set; a four-element
    {34,35,39,54} is short one.  Note 62 is the one of the five with no LUT at
    all (`lutId == 0xFF`), which is exactly what "raw-access alias" means.
  * a parity AU's aux is marked by bit 0x80 of byte 7, i.e. bit 31 of the
    second aux dword (`sub_1C3CC`, `efc_cpu2.c:42935-42957`), which the read
    paths strip with `& 0x7FFFFFFF` (`efc_cpu2.c:141509`).  On the one Mso-mode
    parity page in this dump that decodes with 4/4 MCRC the aux is
    `lma = 0` and `tag = 0x80000000` on AUs 0 and 1, `tag = stripeId << 7` on AU
    2 and the sequence number on AU 3 -- so the flag is set for Mso parity too.
    Solved 2026-08-06 and now n = 17: no firmware path sets it for a ParityData
    write and none needs to.  It is the XOR image of the marker `sub_1C3CC`
    puts on a DATA member (call site `efc_cpu2.c:38735` is inside
    `if (Mso_GetRaidType == cRaidTypeMso)`, which RETRACTS "both callers are
    gated on cRaidTypeInline"), carried by the accumulator.  On all 17
    accumulated layout-3 pages here AU0's tag is exactly `0x80000000` and AU0's
    and AU1's bit 31 always agree, against 0 of 66 and 38 of 66 on the
    unaccumulated ones.  `titania.auxdata.parity_marker` reads it; use it together
    with `parity_stripe_complete`, never alone.

THE LAYER QUESTION, WHICH DOES NOT INHERIT FROM INLINE
-----------------------------------------------------------
Inline RAID works at the raw stored layer because every block-row of the stripe
occurs an even number of times, so the row-keyed keystreams cancel.  A 48-wide
Mso stripe does not have that property and this module does not pretend it
does.  A rectangular user WERU has `slot = 4*ce + q`, so each of its four block
numbers occurs 12 times among the 48 data columns -- even, fine -- but the parity
AU lives in a different WERU and its row therefore occurs exactly once.  The raw
XOR over the 49 members is not zero, it is the parity row's keystream.
`mso_stored_layer_ok` computes the row multiset from the real placements and
returns False with that reason; use `mso_rebuild_message`, which works at the
plaintext layer.

AND THE AUX IS NOT IN THE RELATION EITHER -- MEASURED 2026-08-06
----------------------------------------------------------------
Inline mode's parity holds over the whole 4104-byte message, aux included, and
`mso_rebuild_message` used to inherit that.  It does not carry over, and the
measurement is on this board's own media.  On the stripe read for this purpose
(weru 573 page 0, 44 live columns, all MCRC 4/4):

    XOR of the 44 held data auxes  ==  00 00 00 00 00 00 00 00   at every AU index
    the parity AU's aux            ==  ...00000080 on AUs 0 and 1,
                                       stripeId << 7 on AU 2, a sequence on AU 3

and AU 2's field reads 2530368, which is exactly the stripeId of that very
stripe.  A XOR of forty-eight per-AU sequence numbers cannot equal the
stripeId; so the parity AU's aux is its own `cAuxLayoutForRaidParityMso`
metadata, written by the write path, and is not a XOR product.

    THE MSO PARITY RELATION COVERS THE 4096 USER BYTES.  IT DOES NOT COVER THE
    8 AUX bytes.

`mso_rebuild_message` therefore returns the 4096-byte user payload and
refuses to invent an aux -- a rebuilt data AU's LMA and tag have to come from
the LUT and the stripe geometry, not from the parity.

    Refined 2026-08-06, and the refinement is why it must stay that way.
    `titania.auxdata` has since traced the layout-3 write: MOH copies all 32 aux
    bytes of the page verbatim out of the parity XOR accumulator and only then
    stamps AU2's and AU3's second dword (`efc_cpu2.c:37432-37450`, `:43186`).
    So 24 of the 32 bytes are the member XOR -- and on all 16 clean, real-stripeId
    parity pages held here every one of the four first dwords reads exactly zero,
    which is the aux relation closing.  The two stamped dwords are not, and they
    are the two that carry the address.  A rebuilt aux would therefore be
    zeroes plus two fabricated dwords, which is worse than no aux at all.  See
    `titania.auxdata` for the mechanism and for the completeness test it buys.

THE CARRIED AUX IS THE MEMBER XOR, AND IT IS PREDICTABLE -- 2026-08-07
--------------------------------------------------------------------------
The two statements above and this one do not conflict; read them as one.  Six of
a parity page's eight aux dwords are the accumulator's output and six of six
are now reproduced from the members; the two `sub_1C8D8` overwrites (AU2's and
AU3's second dword, the stripeId and the sequence) are not, which is exactly the
`aux_stripe_lma` result above.  `rj_carry.py` is the measurement and
`_check_carry_on_media` re-runs it; `titania.auxdata.parity_carry` is the model and
its docstring the derivation.

    parity AU_k dw0 == XOR of the 48 members' LMAs  ==  0, by identity

Each CE's four columns are the WMAs `base + 16*ce .. +15`, one 16-aligned LUT
granule, so their LMAs are `G+k, G+4+k, G+8+k, G+12+k` -- four values differing
only in bits 2 and 3, each appearing twice.  It needs nothing about CE0 beyond
its being a granule, which is why the dead die does not obstruct it.

    parity AU0 dw1 == markerXOR << 31 == 0x80000000   (AU0's shard is seq>>31)
    parity AU1 dw1 == XOR of the 48 members' page sequences | markerXOR << 31

Census over every clean, addressed layout-3 page held -- 132 pages: dw0 zero
on all four AUs 132/132, AU0 exactly `0x80000000` 132/132, AU1 carrying low bits
on 8 and every one a multiple of 16.  Member-by-member on the four stripes whose
members and parity are both held: 4/4 on all three quantities, the decisive
one being weru 579 page 76, where 37 of 48 columns come from a fitted lattice,
the answer is `0x80000070`, and every shifted lattice fails.

And it places the marker in Mso mode.  Not one readable member of those four
stripes carries the byte-7 bit-7 marker, yet all four parity pages do -- so an
odd number of the four columns on CE0 must, and `slot = 4*ce + q` makes columns
0..3 CE0's.  That is the Inline measurement's answer (slot 0, 847/847) reached
by arithmetic in a mode where the flagged member cannot be read at all, and it
upgrades the old one-sided "0 of 191 Mso data pages carry it".

What it would buy a drive with twelve LIVE CEs, stated as a consequence and
not as something measured here: a missing member's aux is then recoverable too
-- `XOR(the other 47) ^ the parity's carried dwords` -- for all four first
dwords and for AU0's and AU1's second, i.e. its LMA and its page sequence.  Only
AU2's and AU3's second dword are unrecoverable, and on a layout-0/1 member the
writer zeroes exactly those (`efc_cpu2.c:43034-43035`).  A hold-out test of that
on this board would prove nothing -- one linear relation cannot both fix the
parity and independently confirm a member -- so it is not offered as evidence.

WHAT THIS BOARD CANNOT DO WITH IT
---------------------------------
Mso-mode reconstruction is implemented and addressable, and it is not usable on
this console:

    CE0 is dead and a stripe is 48 columns wide with one parity AU.
    `slot = 4*ce + q` puts columns 0..3 of every rectangular user WERU on CE0, so
    every Mso stripe on this drive starts with four erasures against a code that
    corrects one.  This is structural, not a coverage gap, and no choice of
    stripe escapes it: the slot -> CE map is arithmetic, and bad-block
    substitution moves the BLOCK, never the CE.

    Enumerated rather than argued (`pp_task3.py`, 2026-08-06): of the 15737
    live lutId-4 entries, all 15737 name a data WERU with exactly 4 columns on
    CE0 -- 0 stripes at 0 or 1 erasure, which are the only counts a single
    parity AU can close.  The 26 "substituted" WERUs do not help; none of the
    15737 is even non-rectangular.

Coverage, which used to be the second reason, is no longer one: the columns were
read.  On 2026-08-06 the stripe `weru 573 page 0` was read off the media in full
-- 44 live columns and its parity AU (ce1/blk3674/page 2), 45 of 45 at MCRC
4/4 -- so what remains is the four erasures, and only those.

    No real user-data AU can be reconstructed on this board.  Not "we have not
    tried".  What the placement rule buys is the *other* half of the problem: on
    a drive with twelve live CEs, `mso_stripe` names the 48 data AUs and the
    parity AU of any WMA, and `mso_rebuild_message` closes it.

The residual test is not a discriminator -- run, and RETRACTED 2026-08-06
-------------------------------------------------------------------------
This module used to say that XOR-ing the held data AUs against their parity AU
"would turn the placement rule from strongly-evidenced into measured", because
the residual "must be non-zero and must have exactly the structure of four AUs'
worth of plaintext".  That test was run and it decides nothing.

    matched parity   residual user: entropy 7.9508 b/B, 14/4096 zero, 256 values
    a wrong parity   residual user: entropy 7.9560 b/B, 10/4096 zero, 256 values

Every readable byte on this drive is AES ciphertext, so "four AUs of plaintext"
and "forty-five unrelated AUs" are the same distribution.  The XOR moves, the
statistics do not.  The controls all behave (drop-one changes the residual;
duplicating a column equals dropping it; each of the three mismatched AU indices
differs) -- they just cannot separate a right parity page from a wrong one, and
neither can any statistic of a ciphertext XOR.

    A residual whose only property is "it looks random" is not EVIDENCE.

What did move the placement rule to measured is item 4 above: the parity AU's own
25-bit stripeId field agreeing with the independently-derived stripeId, now on
ten pages with zero counterexamples, up from two.

The readability class of a native-tlc PAGE -- measured 2026-08-06
-----------------------------------------------------------------
A fact about the media that decides which stripes can be studied at all, and it
is not about RAID: on this board a native-TLC page's readability is a function
of its position in the word line (`efc_cpu1.c:9780`: 2 pages/wl below page 8,
3 pages/wl from 8 to 1087).  Measured over blocks 3596-3599, 3648-3651 and
2268-2271:

    page < 8               2 pages/wl   syndromes    0..28    decodes 4/4
    (page - 8) % 3 == 0    "lower"      syndromes  150..600   decodes 4/4
    (page - 8) % 3 == 1    "middle"     syndromes  370..900   borderline
    (page - 8) % 3 == 2    "upper"      syndromes  950..1030  never decodes

The EE3 waterfall is around 170 syndromes per codeword, so an upper page is not
a marginal read -- it is uncorrectable without the 7-level 0x89/0x8A read-retry
ladder, which writes volatile feature registers and which a read-only session
does not do.  `wordline_class` is that rule.

    This is why weru 234 page 539 was the wrong stripe, and it is not the
    reason anyone expected.  Its DATA end is fine: `(539 - 8) % 3 == 0`, a lower
    page, and all 44 held columns decode MCRC 4/4.  Its PARITY end is
    ce5/blk3599 page 34, and `(34 - 8) % 3 == 2` -- an upper page.  That
    page was read on hardware: it is programmed (18389 of 18432 bytes not 0xFF),
    it is in the right cell mode (the pseudo-SLC read of the same address
    returns all 0xFF, so the mode is not in doubt), and it comes back at
    953..1002 syndromes per codeword.  One page out of forty-nine is what
    blocked that stripe.  weru 590 page 37 -- the stripe the one MCRC-4/4
    parity page in the dump covers -- fails the other way round: page 37 is an
    upper page and 0 of its 48 data columns decode.

    So with a board in the room, coverage is not the axis that matters; the word
    line is, at both ends.  `pp_pick.py` in the working tree is the search that
    inverts it, and 2487 of the 15737 live parity entries have both ends in the
    readable class.  The stripe finally used, weru 573 page 0 -> ce1/blk3674
    page 2, is one of them.

Descriptors -- for anyone re-deriving this from the write path
--------------------------------------------------------------
`sub_10BF4` (`HalMioWrite.c`, `efc_cpu1.c:20814`) is the RAID descriptor
builder.  It pushes 40-byte descriptors whose dw5 top nibble is the RAID
command: 0x8 on the data descriptor (`efc_cpu1.c:20869`, `:20903`), 0xB on
parity descriptor #1 (`:20885`) and 0xC on parity descriptor #2 (`:20927`).
The parity descriptors select AU_FORMAT 7 or 8 (`:20881`, `:20923`) -- the
ECU's format-D pair.  Those descriptors are the DRAM<->SRAM parity-buffer
traffic, not a NAND program: the XOR accumulates in the ECU's RAID buffers
(`ECC_RAID_EN`, `RegEcu.h:623`; 26 SRAM buffers, `efc_cpu1.c:14206`).
"""
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


if __package__ in (None, ""):                       # `py titania/raid.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
else:
    from . import geometry as geom

#: This package's name whether imported as `titania.raid` or run as a script.
_PKG = __package__ or "titania"


def _sib(name):
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))

__all__ = ["RAID_TYPE_NONE", "RAID_TYPE_MSO", "RAID_TYPE_INLINE", "RAID_NAMES",
           "PARITY_GROUP_ID", "AUX_LAYOUT_RAID_PARITY", "MSO_DESC_AUX_LAYOUT_OFF",
           "Stripe", "inline_parity_slot", "inline_stripe",
           "inline_parity_of", "inline_covered_by", "stored_layer_ok",
           "rebuild_page", "rebuild_au", "rebuild_message", "check_page",
           "parity_mso_id", "mso_parity_map", "parity_msos",
           "parity_msos_from_table", "aux_is_parity",
           "stripe_id", "stripe_first_wma", "stripe_column",
           "parity_run", "parity_run_index",
           "PARITY_RUN_PAGES", "PARITY_RUN_PER_CE",
           "INVALID_STRIPE_ID", "PARITY_AUX_STRIPE_AU", "PARITY_AUX_STRIPE_SHIFT",
           "mso_stripe_lma", "mso_stripe_data_wmas", "aux_stripe_lma",
           "parity_lut_mso", "mso_parity_wma", "mso_parity_of", "mso_stripe",
           "mso_stored_layer_ok", "mso_rebuild_message",
           "wordline_class", "WORDLINE_CLASSES",
           "WERU_CONFIG_TABLE", "kernel_dim", "parity_relations", "selftest"]

#: `Mso_GetRaidType` values, efc_cpu2.c:38717 / :38744-38746 / :39035-39036.
RAID_TYPE_NONE = 0
RAID_TYPE_MSO = 1
RAID_TYPE_INLINE = 2
RAID_NAMES = {0: "None", 1: "Mso", 2: "Inline"}

#: `cCoreMsoGroupParityData`, the msoGroupId whose MSOs hold parity.
#: `efc_cpu3.c:4111` (group-name table index 5), `efc_cpu3.c:46780`.
PARITY_GROUP_ID = 5

#: msoGroupIds per msoType in the msoIdTable, `msoId = table[type*6 + group]`.
MSO_GROUPS_PER_TYPE = 6

#: `Mso_GetAuxLayout` = msoDescriptor byte +17 (`efc_cpu2.c:133444`, `:134109`).
MSO_DESC_AUX_LAYOUT_OFF = 17
AUX_LAYOUT_DATA = 1
AUX_LAYOUT_NSID23 = 2
AUX_LAYOUT_RAID_PARITY = 3               # cAuxLayoutForRaidParityMso

#: WMA decomposition used by `Raid_GetStripeIdForWma` (`efc_cpu2.c:34167`).
#: `P*C == ftl.WMA_STRIDE == 192`, and `column` is `ftl.split_wma`'s slot.
WMAS_PER_PAGE = 4                        # P, `pageSizeBytes >> 12`
COLUMNS = 192 // WMAS_PER_PAGE           # C = 48

#: cRaidTypeMso parity aux.  The stripeId lives in bits [31:7] of the tag dword
#: of AU 2 of the parity page: `sub_40355C4C(i, wma) = i - wma%P + 2`
#: (`efc_cpu2.c:133310`) selects the entry, `>> 7` the field
#: (`:134170`).  `0x1FFFFFF` is INVALID_STRIPE_ID -- the bound the ParityManager
#: asserts against (`:139457`) and the value `*p |= 0xFFFFFF80` writes when a
#: parity AU is invalidated (`:133787`).
PARITY_AUX_STRIPE_AU = 2
PARITY_AUX_STRIPE_SHIFT = 7
INVALID_STRIPE_ID = 0x1FFFFFF

#: `Mso_GetMsoId(UserData, ParityData)` -- `byte_900005`, the parity write front
#: `RebuildParityLut` is invoked on (`efc_cpu2.c:133111`).  Used only as a
#: default handle on the shared lutId-4 LMA space; `parity_lut_mso` prefers to
#: derive it from the board's own descriptors.
PARITY_LUT_ANCHOR_MSO = 34


#: The six WERU configurations, read out of the firmware image by
#: `sub_4033CB5E` (`efc_cpu2.c:107815`).  `blocksPerWeru` is the allocated block
#: count; for the metadata class (config 2) eleven of the twelve are
#: WMA-addressable and the twelfth is the inline parity.
#:      (firstWeru, count, blocksPerWeru, blocksPerCe, cellMode)
WERU_CONFIG_TABLE = (
    (0, 1, 24, 2, "pSLC"),
    (1, 2, 24, 2, "pSLC"),
    (3, 26, 12, 2, "pSLC"),          # metadata -- the Inline-RAID class
    (29, 88, 48, 4, "pSLC"),
    (117, 855, 48, 4, "TLC"),        # main user store
    (972, 98, 24, 2, "?"),
)


#: `wordline_class` names, worst last.  The syndrome ranges are what was
#: measured on this board (blocks 3596-3599, 3648-3651, 2268-2271, 2216-2327),
#: not a datasheet claim, and the EE3 waterfall is around 170 per codeword.
WORDLINE_CLASSES = (
    (0, "pre-TLC", "2 pages per word line; syndromes 0..28; decodes"),
    (1, "lower", "syndromes 150..600; decodes"),
    (2, "middle", "syndromes 370..900; borderline"),
    (3, "upper", "syndromes 950..1030; NEVER decodes without read-retry"),
)


def wordline_class(page, slc=False):
    """How readable is this native-TLC page?  -> (rank, name)

    Lower rank is more readable.  `efc_cpu1.c:9780` gives the geometry -- 2
    pages per word line below page 8, 3 from 8 to 1087 -- and the ranks are the
    measured decode behaviour on this board, see the module docstring.

    A pseudo-SLC block has one page per word line and no such gradient, so this
    returns class 0 for every page of one; passing `slc=True` says so rather
    than letting a caller apply a TLC rule to an SLC block.

    This is not a licence to skip middle pages.  Class 2 is borderline, not
    dead: it was measured decoding at 370 syndromes and failing at 800.  Use it
    to choose between equivalent candidates, never to declare a page unreadable
    without reading it.
    """
    if slc:
        return 0, "pre-TLC"
    if page < 8:
        return 0, "pre-TLC"
    off = (page - 8) % 3
    return (1, "lower") if off == 0 else (2, "middle") if off == 1 else (3, "upper")


def kernel_dim(vectors):
    """Number of independent XOR relations among `vectors`.  Gauss-Jordan.

    The honest way to ask "how many parities are there".  Feed it the twelve
    members of a stripe: dimension 1 means single parity and a second erasure
    is unrecoverable, whatever the aux flags suggest.  Members that are
    byte-identical (or all erased) manufacture dimensions that mean nothing --
    the caller must exclude them, and `parity_relations` does.
    """
    basis, dim = [], 0
    for v in vectors:
        val = int.from_bytes(v, "big")
        for b in basis:
            if val ^ b < val:
                val ^= b
        if val:
            basis.append(val)
            basis.sort(reverse=True)
        else:
            dim += 1
    return dim


def parity_relations(vectors):
    """-> (dimension, [slot sets]) or None when the input is degenerate."""
    if len({bytes(v) for v in vectors}) != len(vectors):
        return None
    rows = [(int.from_bytes(v, "big"), 1 << i) for i, v in enumerate(vectors)]
    basis, ker = [], []
    for val, combo in rows:
        for pval, pcombo in basis:
            if val ^ pval < val:
                val ^= pval
                combo ^= pcombo
        if val:
            basis.append((val, combo))
            basis.sort(key=lambda x: -x[0].bit_length())
        else:
            ker.append(sorted(i for i in range(len(vectors))
                              if combo >> i & 1))
    return len(ker), ker


def stripe_id(wma, p=WMAS_PER_PAGE, c=COLUMNS):
    """`Raid_GetStripeIdForWma`, efc_cpu2.c:34167-34172.  -> stripeId."""
    return (wma // (p * c)) * p


def stripe_first_wma(sid, p=WMAS_PER_PAGE, c=COLUMNS):
    """`Raid_GetWmaForStripeId`, efc_cpu2.c:34174-34189.  -> first WMA."""
    return (sid // p) * p * c


def stripe_column(wma, p=WMAS_PER_PAGE, c=COLUMNS):
    """Which column (== WERU slot) of its stripe this WMA is.

    `efc_cpu2.c:143820`.  Identical to `ftl.split_wma(wma)[2]`, reached by an
    independent route -- which is the cross-check that P*C is really the
    192-WMA page window.
    """
    return (wma // p) % c


#: Pages of a parity run: 12 CEs x (2 block numbers x 3 pages of one TLC
#: wordline).  See `parity_run_index`.
PARITY_RUN_PAGES = 72
PARITY_RUN_PER_CE = 6


def parity_run(block, page, base_block):
    """-> (wordlineStart, half) -- which parity write run this page belongs to.

    `base_block` is the first of the WERU's four block numbers on a CE.  See
    `parity_run_index` for what a run is and how it was measured.
    """
    q = block - base_block
    if not 0 <= q < 4:
        raise ValueError("block %d is not one of %d..%d"
                         % (block, base_block, base_block + 3))
    return (8 + 3 * ((page - 8) // 3) if page >= 8 else 0), q >> 1


def parity_run_index(ce, block, page, base_block):
    """Position 0..71 of a parity page inside its run.  MEASURED, not assumed.

    A `cAuxLayoutForRaidParityMso` WERU is not written page-row by page-row.
    `sub_1C8D8` stamps AU3's second dword from the group-5 rebuild sequence pool
    and bumps the pool once per PAGE (`efc_cpu2.c:43200-43203`), and AU2's from
    the ParityManager tracker's stripeId list, so both counters are readable on
    media and the order is directly measurable.  Over WERU 592 (blocks
    3656..3659, 124 held pages, `rh_stripe.py --model`) it is

        k = PARITY_RUN_PER_CE*ce + 2*(page - wordlineStart) + (block & 1)

    with the four block numbers split into two independent 72-page runs -- the
    pair {base, base+1} and the pair {base+2, base+3} -- and one run covering
    one TLC wordline (3 pages) of both its blocks on all 12 CEs.  Within a run
    both `sequence` and `stripeId // WMAS_PER_PAGE` are `base + k`.

    Leave-one-out over 18 runs: the sequence is exact on 122 of 122, every
    held page including the unaddressed ones.  The stripeId is exact on 47 of
    53, and the six exceptions are all one run (wordline 8, half 0) whose
    stripeId base jumps by 1148 between k=11 and k=28 -- a data-side write-front
    jump mid-run.  So `k` may be used to place a page in its run, and the
    sequence may be predicted from it, but the stripeId may not be interpolated
    or extrapolated across an unsampled gap.  That is why the fourteen
    unaddressed pages of WERU 592 cannot simply be given an address.

    THE RUN IS THE UNIT OF FAILURE, WHICH IS WHY THE BOUNDARY FALLS BETWEEN
    BLOCK numbers.  Cross-checked against the parity LUT, which is complete in
    this dump (1048 of 1048 table LMAs held) and names 13222 pages of WERU 592:

        a healthy run          the LUT names 63..71 of its 72 pages; the
                               unnamed ones are its last few (k = 69, 70, 71)
        a collapsed run        the LUT names 0..12 of 72

    and the six withheld pages at wordline 98 and the six at wordline 188 sit in
    runs the LUT names 3 of 72 of, while both halves of wordlines 8/53/143
    and the other half of 98/188 are healthy.  So the fault is a property of the
    72-page run, not of the six pages we happen to hold, and it lands on a
    block-number boundary because the block pairs are the runs.
    """
    return (PARITY_RUN_PER_CE * ce
            + 2 * (page - parity_run(block, page, base_block)[0])
            + ((block - base_block) & 1))


def aux_is_parity(aux):
    """Is this 8-byte aux marked as a RAID parity AU?  -> bool.

    `sub_1C3CC` ORs 0x80 into byte 7 of every aux entry of a RAID write
    (`efc_cpu2.c:42935-42957`, WriteAuxiliaryList.c:378); the read paths strip
    it with `& 0x7FFFFFFF` (`efc_cpu2.c:141509-141519`).
    """
    return bool(aux[7] & 0x80)


def parity_msos(mso_descriptor_bytes, entry=28):
    """-> sorted msoIds whose aux layout is cAuxLayoutForRaidParityMso.

    Read from the board's own msoDescriptor section, so it needs no firmware
    image.  On this board it returns [34, 35, 39, 54, 62], agreeing exactly
    with column 5 of the msoIdTable -- two independent routes to the same set.
    """
    out = []
    for m in range(len(mso_descriptor_bytes) // entry):
        e = mso_descriptor_bytes[m * entry:(m + 1) * entry]
        if any(e) and e[MSO_DESC_AUX_LAYOUT_OFF] == AUX_LAYOUT_RAID_PARITY:
            out.append(m)
    return out


class Stripe(object):
    """One RAID stripe: the data AUs it protects and the parity AU that does.

    `members` and `parity` are ``(ce, block, page, au)`` tuples -- physical
    addresses, not a promise that any of them is readable.  `slots` gives the
    WERU slot number of each member in the same order, so a caller that knows a
    slot is unreadable can name it without re-deriving the map.

    For `RAID_TYPE_MSO` the stripe spans 48 columns of one WERU and the parity
    AU lives in a different WERU entirely, so `member_wmas`/`parity_wma` are also
    filled in and `stripe_lma` names the LUT key.  A member may be `None` there:
    that means the WMA is legal but this dump has no measured `slot -> (ce,
    block)` for its WERU, which is a coverage fact, not a hole in the stripe.
    """
    __slots__ = ("weru_id", "page", "au", "slots", "members", "parity",
                 "parity_slot", "mode", "source",
                 "member_wmas", "parity_wma", "stripe_lma")

    def __init__(self, weru_id, page, au, slots, members, parity, parity_slot,
                 mode=RAID_TYPE_INLINE, source=None, member_wmas=None,
                 parity_wma=None, stripe_lma=None):
        self.weru_id, self.page, self.au = weru_id, page, au
        self.slots, self.members = slots, members
        self.parity, self.parity_slot = parity, parity_slot
        self.mode, self.source = mode, source
        self.member_wmas, self.parity_wma = member_wmas, parity_wma
        self.stripe_lma = stripe_lma

    @property
    def width(self):
        """Data units per parity unit."""
        return len(self.members)

    def __contains__(self, addr):
        return tuple(addr) in self.members or tuple(addr) == self.parity

    def __repr__(self):
        return ("<Stripe weru %s page %d au %d: %d data + 1 parity at slot %d>"
                % (self.weru_id, self.page, self.au, self.width,
                   self.parity_slot))


# ===========================================================================
# cRaidTypeInline -- solved
# ===========================================================================
def inline_parity_slot(config):
    """-> the slot number of the parity block of an Inline WERU.

    It is `blocksPerWeru`: the WMA space addresses slots 0..blocksPerWeru-1
    (`ftl.wma_unaddressable_reason` calls anything above that an invalid wma),
    and the one further block each such region physically owns sits at the next
    index of the same slot formula.  For the five measured SLC configurations
    that is slot 11 = `(ceBase + 5, blockBase + 1)`.
    """
    return config.blocks_per_weru


def _phys(config, slot):
    """slot -> (ce, block), reaching past the WMA-addressable range.

    `WeruConfig.slot_to_physical` refuses the parity slot, because
    no WMA names it and that module must not invent a meaning for it.  Here the
    meaning is established, so the arithmetic is repeated rather than the
    refusal weakened.
    """
    if slot < 0 or slot > config.blocks_per_weru:
        return None
    ce = config.ce_base + slot // config.blocks_per_ce
    block = config.block_base + slot % config.blocks_per_ce
    if ce >= geom.CE_COUNT:
        return None
    return ce, block


def _phys_required(config, weru_id, slot):
    """`_phys`, but a None is refused with a reason instead of dereferenced.

    `_phys` returns None for a slot whose CE falls off the end of the package,
    and that is not hypothetical: an Inline WERU is `blocksPerWeru + 1` blocks,
    so the parity slot of a 48-block user WERU (blocksPerCe 4, ceBase 0) is
    `ce = 48 // 4 = 12` -- one past the twelfth die.  Those WERUs are
    cRaidTypeMso, not Inline; they have no inline parity block at all.

    `titania raid check --weru <a user weru>` reached exactly that
    (`cli.py`'s `raid check` passes whatever `ftl.weru_config` returns, and for
    weruIds 117.. that is the 48-block shape), and the caller's `cb[0]` turned
    the refusal into
    `TypeError: 'NoneType' object is not subscriptable` -- a message that says
    nothing about the real answer, which is that the Inline relation does not
    apply to this shape.  `rebuild_page` never saw it because `stored_layer_ok`
    runs first and refuses the same configs with the same reason.
    """
    cb = _phys(config, slot)
    if cb is None:
        raise ValueError(
            "weru %s slot %d has no physical address: ce %d is past the "
            "%d-CE package, so this %d-block/%d-per-CE shape has no inline "
            "parity block and the cRaidTypeInline relation does not apply to "
            "it (a 48-block user WERU is cRaidTypeMso -- see mso_stripe)"
            % (weru_id, slot, config.ce_base + slot // config.blocks_per_ce,
               geom.CE_COUNT, config.blocks_per_weru, config.blocks_per_ce))
    return cb


def inline_stripe(config, weru_id, page, au):
    """-> `Stripe` for one (page, au) of an Inline WERU, or None.

    The stripe is one AU index of one page across every data slot; the four AU
    indices of a page are four independent stripes, which is exactly what the
    four RAID buffers of descriptor dw5 accumulate in parallel
    (`HalDescriptor.h:126-134`).
    """
    if not (0 <= page < config.pages_per_block):
        return None
    if not (0 <= au < WMAS_PER_PAGE):
        return None
    slots, members = [], []
    for s in range(config.blocks_per_weru):
        cb = _phys(config, s)
        if cb is None:
            return None
        slots.append(s)
        members.append((cb[0], cb[1], page, au))
    ps = inline_parity_slot(config)
    cb = _phys(config, ps)
    if cb is None:
        return None
    return Stripe(weru_id, page, au, slots, members, (cb[0], cb[1], page, au),
                  ps, RAID_TYPE_INLINE, source="WERU_CONFIGS")


def inline_parity_of(config, weru_id, ce, block, page, au):
    """Forward map: which parity AU covers this data AU?  -> tuple or None.

    None when (ce, block) is not a data slot of this WERU -- including when it
    is the parity block, which covers nothing.
    """
    slot = config.physical_to_slot(ce, block)
    if slot is None or slot >= config.blocks_per_weru:
        return None
    cb = _phys(config, inline_parity_slot(config))
    return None if cb is None else (cb[0], cb[1], page, au)


def inline_covered_by(config, weru_id, ce, block, page, au):
    """Inverse map: which data AUs does this parity AU cover?  -> list or None.

    None when (ce, block) is not this WERU's parity block.
    """
    cb = _phys(config, inline_parity_slot(config))
    if cb is None or (ce, block) != cb:
        return None
    st = inline_stripe(config, weru_id, page, au)
    return None if st is None else list(st.members)


# ===========================================================================
# the two layers
# ===========================================================================
def stored_layer_ok(config):
    """May the XOR be taken over RAW stored bytes?  -> (bool, explanation).

    The scrambler seed is a function of the row alone, so XOR-ing stored AUs
    XORs their keystreams.  They cancel iff every row occurs an even number of
    times over the whole stripe (data + parity); dropping one member then
    leaves that member's own row an odd number of times, which is exactly the
    keystream the reconstruction needs.
    """
    rows = {}
    for s in range(config.blocks_per_weru + 1):
        cb = _phys(config, s)
        if cb is None:
            return False, "slot %d has no physical address" % s
        rows[cb[1]] = rows.get(cb[1], 0) + 1
    odd = sorted(b for b, n in rows.items() if n % 2)
    if odd:
        return False, ("blocks %s occur an odd number of times in the stripe, "
                       "so their keystreams do not cancel -- use the message "
                       "layer" % odd)
    return True, ("every block number occurs an even number of times (%s), so "
                  "the keystreams cancel and the raw XOR is exact"
                  % ", ".join("blk %d x%d" % kv for kv in sorted(rows.items())))


def _read(dev, ce, block, page, slc):
    return dev.read_page(ce=ce, block=block, page=page, slc=slc)


def _xor(bufs):
    """XOR equal-length byte strings.  Big-int arithmetic, not a Python loop:
    a stripe is 12 x 18432 bytes and this runs over every page of a block."""
    n = len(bufs[0])
    acc = 0
    for b in bufs:
        if len(b) != n:
            raise ValueError("cannot XOR %d bytes with %d" % (len(b), n))
        acc ^= int.from_bytes(b, "big")
    return acc.to_bytes(n, "big")


def rebuild_page(dev, config, weru_id, ce, block, page):
    """Rebuild one unreadable page of an Inline WERU.  -> 18432 raw bytes.

    Reads the other eleven members of the WERU at the same page and XORs them.
    Works on the bytes as stored -- no seed, no ECC, no keys -- but only when
    `stored_layer_ok` says the keystreams cancel, and this raises if they do
    not rather than returning plausible garbage.

    Raises `ValueError` if (ce, block) is not one of the twelve blocks, or if
    any *other* member cannot be read: a single-parity code corrects exactly one
    erasure and a second one is not a partial answer, it is no answer.
    """
    ok, why = stored_layer_ok(config)
    if not ok:
        raise ValueError("raw-byte XOR is not valid for this WERU shape: %s"
                         % why)
    target = config.physical_to_slot(ce, block)
    if target is None:
        par = _phys(config, inline_parity_slot(config))
        if par == (ce, block):
            target = inline_parity_slot(config)
        else:
            raise ValueError("(ce %d, block %d) is not in weru %s"
                             % (ce, block, weru_id))
    bufs = []
    for s in range(config.blocks_per_weru + 1):
        if s == target:
            continue
        # `_phys` cannot be None here: `stored_layer_ok` above walks the same
        # `range(blocks_per_weru + 1)` and returns False -- which raised -- for
        # any slot without a physical address.  That is the precondition, and
        # `tests/test_latent_guards.py` pins it with `dev=None`.
        cb = _phys(config, s)
        bufs.append(_read(dev, cb[0], cb[1], page, config.slc))
    return _xor(bufs)


def rebuild_au(dev, config, weru_id, ce, block, page, au):
    """As `rebuild_page`, but returns only the one 4608-byte stored AU."""
    pg = rebuild_page(dev, config, weru_id, ce, block, page)
    return pg[au * geom.AU_STORED:(au + 1) * geom.AU_STORED]


def rebuild_message(dev, config, weru_id, ce, block, page, codec=None):
    """Rebuild a page at the plaintext layer.  -> [bytes] x 4, aux + user.

    Unconditional: it decodes every other member (LDPC correction, MCRC,
    descrambling) and XORs the 4104-byte messages, so it does not depend on the
    keystreams cancelling and it is immune to raw bit errors the ECC can fix.
    Slower, and it needs the codec.
    """
    if codec is None:
        codec = _sib("codec")
    target = config.physical_to_slot(ce, block)
    if target is None and _phys(config, inline_parity_slot(config)) == (ce, block):
        target = inline_parity_slot(config)
    if target is None:
        raise ValueError("(ce %d, block %d) is not in weru %s"
                         % (ce, block, weru_id))
    # Addresses first, reads second.  There is no `stored_layer_ok` gate on
    # this path -- it is unconditional at the message layer on purpose -- so
    # the refusal has to be made here, and it has to be made before any I/O.
    # Resolving inside the read loop would decode forty-odd pages and only then
    # discover that the shape has no parity block, which is the same wasted
    # work `rebuild_page` avoids by checking up front.
    members = [(s, _phys_required(config, weru_id, s))
               for s in range(config.blocks_per_weru + 1) if s != target]
    acc = [bytearray(codec.MSG_BYTES) for _ in range(codec.AUS_PER_PAGE)]
    for _s, cb in members:
        res = codec.decode_page(_read(dev, cb[0], cb[1], page, config.slc),
                                row=geom.compose_row(cb[1], page),
                                fmt=codec.FORMAT_A, correct=True)
        for i, a in enumerate(res.aus):
            msg = bytes(a.aux) + bytes(a.user)
            for j, v in enumerate(msg):
                acc[i][j] ^= v
    return [bytes(a) for a in acc]


def check_page(dev, config, weru_id, page):
    """Verify the stripe without rebuilding anything.  -> (differing, total).

    (0, 18432) means the twelve members satisfy the parity relation exactly.
    A small non-zero count is a raw bit error somewhere in the twelve, not a
    broken model -- re-check with `rebuild_message`, which corrects first.
    """
    # Every address resolved before the first read.  `titania raid check --weru
    # <a user weru>` lands here with a 48-block config whose parity slot is off
    # the package: the old code read all 48 data members and only then died on
    # `NoneType is not subscriptable`, so it cost a full page-read pass to
    # produce a message that said nothing.
    members = [_phys_required(config, weru_id, s)
               for s in range(config.blocks_per_weru + 1)]
    bufs = [_read(dev, cb[0], cb[1], page, config.slc) for cb in members]
    acc = _xor(bufs)
    return sum(1 for v in acc if v), len(acc)


# ===========================================================================
# cRaidTypeMso -- the parity MSO table
# ===========================================================================
def parity_mso_id(mso_id, table):
    """-> the ParityData-group msoId for `mso_id`'s msoType, or None.

    `table` is the 126-byte msoIdTable, `msoId = table[msoType*6 + msoGroupId]`.
    Column `PARITY_GROUP_ID` of the same msoType row is that type's parity MSO.
    None means the table names none -- which on this board is the case for
    every MSO that actually owns user WERUs, and is an open contradiction with
    their msoDescriptor saying cRaidTypeMso.  It is not a licence to guess.
    """
    for t in range(len(table) // MSO_GROUPS_PER_TYPE):
        row = table[t * MSO_GROUPS_PER_TYPE:(t + 1) * MSO_GROUPS_PER_TYPE]
        if mso_id in row[:PARITY_GROUP_ID]:
            p = row[PARITY_GROUP_ID]
            return None if p == 0xFF else p
    return None


def _real_rows(table):
    """The msoType rows that are real, skipping the padding past the table end.

    The 126 bytes run 17 populated types and then zeros, and those zeros are
    padding, not entries -- reading them as real is what once produced a bogus
    "msoId 0 = Undefined/ParityData" (NAND_STORAGE_MAP.md section 1).  A row of
    all-zero or all-0xFF is not a type.
    """
    for t in range(len(table) // MSO_GROUPS_PER_TYPE):
        row = table[t * MSO_GROUPS_PER_TYPE:(t + 1) * MSO_GROUPS_PER_TYPE]
        if not any(row) or all(v == 0xFF for v in row):
            continue
        yield t, row


def mso_parity_map(table):
    """-> {data msoId: parity msoId or None} for every real entry."""
    out = {}
    for _t, row in _real_rows(table):
        p = row[PARITY_GROUP_ID]
        for g in range(PARITY_GROUP_ID):
            if row[g] != 0xFF:
                out[row[g]] = None if p == 0xFF else p
    return out


def parity_msos_from_table(table):
    """-> sorted msoIds in column 5 (ParityData) of the msoIdTable.

    Distinct from `mso_parity_map`'s values: a parity MSO whose msoType row has
    no data MSOs -- msoId 35, UserData2ndWF/ParityData -- is in this set and
    not in those values.  Missing it is how {34,35,39,54,62} silently becomes
    a four-element set.
    """
    return sorted({row[PARITY_GROUP_ID] for _t, row in _real_rows(table)
                   if row[PARITY_GROUP_ID] != 0xFF})


# ===========================================================================
# cRaidTypeMso -- placement
# ===========================================================================
def mso_stripe_lma(wma, p=WMAS_PER_PAGE, c=COLUMNS):
    """-> the lutId-4 LMA that names this data WMA's parity AU.

    `stripeId(wma) + wma % P`.  `stripeId` is `Raid_GetStripeIdForWma`
    (`efc_cpu2.c:34167`) and is always a multiple of P; the `+ au` term is the
    one `sub_4035684E` adds when it reads the field back out of a parity AU's
    aux (`efc_cpu2.c:134175`), and it is what makes the four AUs of a page four
    independent stripes -- the same shape as Inline mode.
    """
    return stripe_id(wma, p, c) + wma % p


def mso_stripe_data_wmas(lma, p=WMAS_PER_PAGE, c=COLUMNS):
    """-> the C data WMAs one parity AU covers.  Pure arithmetic, no media.

    All 48 columns of one page of one WERU, at the AU index `lma % P`.  This is
    the inverse of `mso_stripe_lma` and the two are checked against each other
    over the whole WMA space in `selftest`.
    """
    au = lma % p
    first = stripe_first_wma(lma - au, p, c)
    return [first + p * col + au for col in range(c)]


def aux_stripe_lma(aus, au=None):
    """-> the stripeId a PARITY page's own aux carries, or None.

    `aus` is a decoded page's AU list (anything with `.aux`).  The field is bits
    [31:7] of the tag dword of AU `PARITY_AUX_STRIPE_AU`; pass `au` to get the
    LMA of that one AU of the page rather than the page's base stripeId.

    Returns None when the aux is absent or the field reads INVALID_STRIPE_ID --
    which is what an unwritten or invalidated parity AU carries
    (`efc_cpu2.c:133787`), not an error.
    """
    import struct
    if len(aus) <= PARITY_AUX_STRIPE_AU:
        return None
    a = aus[PARITY_AUX_STRIPE_AU]
    raw = getattr(a, "aux", a)
    if raw is None:
        return None
    tag = struct.unpack_from("<I", bytes(raw), 4)[0]
    sid = tag >> PARITY_AUX_STRIPE_SHIFT
    if sid == INVALID_STRIPE_ID:
        return None
    return sid + (0 if au is None else au)


def parity_lut_mso(ftl):
    """-> an msoId whose LUT *is* the shared parity (lutId-4) LMA space.

    Any group-5 MSO with a LUT resolves to the same region, so this returns the
    first that does and falls back to `PARITY_LUT_ANCHOR_MSO`.  msoId 62 is
    skipped: its lutId is 0xFF, it has no LUT at all, and that is
    exactly what makes it the raw-access alias rather than a fifth partition.
    """
    UNKNOWN = _sib("ftl").UNKNOWN
    cand = []
    try:
        buf = ftl._section("mso")[0]
        cand = parity_msos(bytes(buf))
    except Exception:
        cand = []
    for m in cand:
        try:
            reg = ftl.lut_region(m)
        except Exception:
            continue
        if reg is not None and reg is not UNKNOWN and reg.init_mso in cand:
            return m
    return PARITY_LUT_ANCHOR_MSO


def mso_parity_wma(ftl, wma, mso_id=None):
    """-> the WMA of the parity AU covering this data WMA.

    * int -- the address.
    * `None` -- the parity LUT maps this stripe nowhere.  On a log-structured
      store that is the ordinary state of a stripe that was never completed, or
      whose parity was invalidated; it is a real negative, not a failure.
    * UNKNOWN -- the LUT table page that would answer is not in this dump.

    The lookup is `Ftl.lma_to_wma`, i.e. the firmware's own route, including the
    sub-granule term: granularity is 4 for the parity space, so entry `lma >> 2`
    plus `lma & 3` lands on the right AU of the parity page.
    """
    if mso_id is None:
        mso_id = parity_lut_mso(ftl)
    return ftl.lma_to_wma(mso_id, mso_stripe_lma(wma))


def mso_parity_of(ftl, wma, mso_id=None):
    """Forward map at the physical layer: -> Placement | None | UNKNOWN."""
    w = mso_parity_wma(ftl, wma, mso_id)
    UNKNOWN = _sib("ftl").UNKNOWN
    if w is None or w is UNKNOWN:
        return w
    return ftl.wma_to_physical(w)


def mso_stripe(ftl, wma, mso_id=None):
    """-> `Stripe` for the cRaidTypeMso stripe containing `wma`, or None.

    48 data AUs + 1 parity AU.  `members` are physical placements where this
    dump can resolve them and `None` where it cannot; `member_wmas` is always
    complete because it is arithmetic.  Returns None when the parity LUT names
    no parity for the stripe, and UNKNOWN when the LUT page is not held --
    the caller must distinguish those two, which is why neither is an exception.

    This does not promise the stripe is recoverable.  One parity AU corrects one
    erasure; on a drive with a dead CE it will normally be short several.
    """
    UNKNOWN = _sib("ftl").UNKNOWN
    lma = mso_stripe_lma(wma)
    pw = mso_parity_wma(ftl, wma, mso_id)
    if pw is None or pw is UNKNOWN:
        return pw
    wmas = mso_stripe_data_wmas(lma)
    members, slots = [], []
    for w in wmas:
        p = ftl.wma_to_physical(w)
        if p is UNKNOWN or p is None:
            members.append(None)
        else:
            members.append((p.ce, p.block, p.page, p.au))
        slots.append(stripe_column(w))
    pp = ftl.wma_to_physical(pw)
    parity = None if (pp is UNKNOWN or pp is None) \
        else (pp.ce, pp.block, pp.page, pp.au)
    weru = wmas[0] // geom.AUS_PER_TLC_WERU
    return Stripe(weru, (wmas[0] % geom.AUS_PER_TLC_WERU) // 192, lma % WMAS_PER_PAGE,
                  slots, members, parity, stripe_column(pw),
                  mode=RAID_TYPE_MSO, source="lutId-4 parity LUT",
                  member_wmas=wmas, parity_wma=pw, stripe_lma=lma)


def mso_stored_layer_ok(stripe):
    """May a cRaidTypeMso stripe be XORed at the RAW stored layer?

    -> (bool, explanation).  The answer is normally False and that is the
    point of asking.  The keystream is a function of the NAND row alone, so it
    cancels only when every row occurs an even number of times over the whole
    stripe.  A rectangular user WERU gives each of its four block numbers 12 of
    the 48 data columns -- even -- but the parity AU is in a different WERU, so
    its row occurs exactly once and does not cancel.  Inline mode's raw-byte
    shortcut therefore does not carry over; use `mso_rebuild_message`.
    """
    rows = {}
    for m in list(stripe.members) + [stripe.parity]:
        if m is None:
            return False, ("at least one member has no resolved physical "
                           "address, so the row multiset cannot be computed")
        rows[(m[1], m[2])] = rows.get((m[1], m[2]), 0) + 1
    odd = sorted(k for k, n in rows.items() if n % 2)
    if odd:
        return False, ("rows %s occur an odd number of times over the 48 data "
                       "AUs plus the parity AU, so their keystreams do not "
                       "cancel -- the raw XOR is not zero, it is that "
                       "keystream.  Use the message layer."
                       % ", ".join("blk %d pg %d" % k for k in odd))
    return True, ("every (block, page) row occurs an even number of times, so "
                  "the keystreams cancel and the raw XOR is exact")


def mso_rebuild_message(dev, ftl, stripe, target_wma, codec=None):
    """Rebuild one AU of a cRaidTypeMso stripe at the plaintext layer.

    -> the 4096-byte user payload of `target_wma`.  Not the 4104-byte
    message: the aux is not in the relation.

    Measured 2026-08-06 on weru 573 page 0 (44 live columns, all MCRC 4/4): the
    XOR of the held data auxes is exactly zero at every AU index, while the
    parity AU's aux is `cAuxLayoutForRaidParityMso` metadata carrying that
    stripe's own stripeId in AU 2 -- a value no XOR of per-AU sequence numbers
    could produce.  So the parity covers the user bytes and nothing else, and
    this returns only what it can actually justify.  A rebuilt AU's LMA and tag
    come from the LUT and the stripe geometry; ask `ftl`, not the parity.

    Unconditional with respect to the scrambler: it decodes every other member
    and XORs the payloads, so it does not depend on keystreams cancelling (they
    do not, see `mso_stored_layer_ok`) and it is immune to raw bit errors the
    LDPC can fix.

    Raises `ValueError` if `target_wma` is not in the stripe or if a member's
    WMA has no resolvable physical address, and lets the device's own read error
    through unchanged when a member is simply not present -- that message names
    the exact missing page, which is what the caller needs.  Either way it
    refuses rather than returning a partial answer: single parity corrects
    exactly one erasure, and a second is not half a recovery.
    """
    if codec is None:
        codec = _sib("codec")
    UNKNOWN = _sib("ftl").UNKNOWN
    all_wmas = list(stripe.member_wmas) + [stripe.parity_wma]
    if target_wma not in all_wmas:
        raise ValueError("wma %d is not in stripe %s" % (target_wma, stripe))
    acc = bytearray(codec.USER_BYTES)
    for w in all_wmas:
        if w == target_wma:
            continue
        p = ftl.wma_to_physical(w)
        if p is UNKNOWN or p is None:
            raise ValueError("wma %d of this stripe has no resolvable physical "
                             "address; a second erasure is not recoverable" % w)
        raw = dev.read_page(ce=p.ce, block=p.block, page=p.page, slc=p.slc is True)
        res = codec.decode_page(raw, row=geom.compose_row(p.block, p.page),
                                fmt=codec.FORMAT_A, correct=True)
        a = res.aus[p.au]
        for j, v in enumerate(bytes(a.user)):
            acc[j] ^= v
    return bytes(acc)


# ===========================================================================
# selftest
# ===========================================================================
def _report(name, status, detail=""):
    print("   %-46s %-8s %s" % (name, status, detail))


#: The stripes on this board whose 48 members and whose parity page are both
#: held and decode.  Found by `rj_carry.py`, which walks every parity WERU; kept
#: as a list here so `selftest` costs four stripes and not five hundred pages.
#: `(data weru, data page, parity ce, parity block, parity page)`.
_CARRY_STRIPES = (
    (573, 0, 1, 3674, 2),
    (590, 37, 1, 3656, 8),
    (234, 539, 5, 3599, 34),
    (579, 76, 6, 3599, 0),
)
#: `slot = 4*ce + q`, so four consecutive columns are one CE's four blocks and
#: one 16-aligned LUT granule (`ftl` lutId 1/2/3 granularity).
_LUT_GRANULE = 16


def _check_carry_on_media(dev, ftl):
    """-> 0/1.  Is a parity page's carried aux the XOR of its members' aux?

    Reads each stripe's members, uses them as measured, and supplies only
    the columns this board cannot read from a lattice fitted to the rest:

        seq(ce, q) = S0 + a*ce + (q & 1) + h*(q >> 1)

    `S0`, `a` and `h` are solved from three of the measured columns, so the
    other forty-odd are a check.  The LMA half needs no lattice at all: a wholly
    missing CE is one 16-aligned granule and its four members XOR to zero.
    """
    import struct
    A = _sib("auxdata")
    codec = _sib("codec")
    UNKNOWN = _sib("ftl").UNKNOWN
    name = "layout 3's carried aux == the member XOR"

    def rd(ce, block, page):
        try:
            raw = dev.read_page(ce=ce, block=block, page=page, slc=False)
            r = codec.decode_page(raw, row=geom.compose_row(block, page),
                                  fmt=codec.FORMAT_A, correct=True)
        except Exception:
            return None
        if sum(1 for a in r.aus if a.mcrc_ok) != WMAS_PER_PAGE:
            return None
        return [bytes(a.aux) for a in r.aus]

    done, hit, why = 0, 0, []
    for (dw, dp, pce, pblk, ppg) in _CARRY_STRIPES:
        st = mso_stripe(ftl, dw * geom.AUS_PER_TLC_WERU
                        + dp * WMAS_PER_PAGE * COLUMNS)
        paux = rd(pce, pblk, ppg)
        if st is None or st is UNKNOWN or paux is None:
            continue
        pd = [struct.unpack("<II", x) for x in paux]
        members, seqs, lmas = {}, {}, {}
        for c, m in enumerate(st.members):
            if m is None:
                continue
            aux = rd(m[0], m[1], m[2])
            if aux is None:
                continue
            members[c] = aux
            d = [struct.unpack("<II", x) for x in aux]
            seqs[(c // 4, c % 4)] = (((d[0][1] & 0x7FFFFFFF)
                                      << A.SEQ_SHARD_BITS_01)
                                     | (d[1][1] & 0x7FFFFFFF))
            lmas[c] = [x[0] for x in d]
        if len(members) < 11:
            continue
        # solve the lattice from three columns; the rest are the check
        live = {k: v for k, v in seqs.items()
                if not all(x == 0xFFFFFFFF for x in lmas[4 * k[0] + k[1]])}
        q0 = sorted((ce, s) for (ce, q), s in live.items() if q == 0)
        q2 = sorted((ce, s) for (ce, q), s in live.items() if q == 2)
        if len(q0) < 2 or not q2:
            continue
        a = (q0[1][1] - q0[0][1]) // (q0[1][0] - q0[0][0])
        s0 = q0[0][1] - a * q0[0][0]
        h = q2[0][1] - (s0 + a * q2[0][0])
        seq_at = lambda ce, q: s0 + a * ce + (q & 1) + h * (q >> 1)  # noqa: E731

        # the LMA law, taken from the columns we hold
        pads = {c for c, v in lmas.items() if all(x == 0xFFFFFFFF for x in v)}
        real = {c: v for c, v in lmas.items() if c not in pads}
        c0 = min(real)
        l0 = real[c0][0] - 4 * c0
        run = all(v == [l0 + 4 * c + k for k in range(WMAS_PER_PAGE)]
                  for c, v in real.items())

        missing = [c for c in range(COLUMNS) if c not in members]
        mdw0 = [0] * WMAS_PER_PAGE
        mlo = 0
        skip = False
        for ce in range(COLUMNS // 4):
            grp = [c for c in range(4 * ce, 4 * ce + 4) if c in missing]
            if grp:
                if run:
                    for c in grp:
                        for k in range(WMAS_PER_PAGE):
                            mdw0[k] ^= l0 + 4 * c + k
                elif len(grp) != 4 or l0 % _LUT_GRANULE:
                    skip = True             # a partly-missing granule: refuse
                for c in grp:
                    mlo ^= seq_at(ce, c % 4) & 0x7FFFFFFF
        if skip:
            why.append("weru %d page %d: %d columns missing from a granule we "
                       "cannot model" % (dw, dp, len(missing)))
            continue
        meas = [struct.unpack("<II", x) for x in A.parity_carry(
            list(members.values()))]
        mk = A.AUX_FLAG_RAID_PARITY_MARKER if 0 in missing else 0
        pred_dw0 = [meas[k][0] ^ mdw0[k] for k in range(WMAS_PER_PAGE)]
        pred_au0 = meas[0][1] | mk
        pred_au1 = (meas[1][1] ^ mlo) | mk
        done += 1
        good = (pred_dw0 == [x[0] for x in pd] and not any(pred_dw0)
                and pred_au0 == pd[0][1] and pred_au1 == pd[1][1])
        hit += good
        if not good:
            why.append("weru %d page %d: predicted dw0 %s au0 %#x au1 %#x, "
                       "observed %s %#x %#x"
                       % (dw, dp, pred_dw0, pred_au0, pred_au1,
                          [x[0] for x in pd], pd[0][1], pd[1][1]))
    if not done:
        _report(name, "SKIPPED",
                "no stripe in this dump has both its members and its parity "
                "page; `rj_carry.py` finds them%s"
                % ("  " + "; ".join(why) if why else ""))
        return 0
    ok = hit == done
    _report(name, "PASS" if ok else "FAIL",
            "%d of %d stripes reproduce their parity page's SIX carried dwords "
            "exactly.  dw0 is zero by identity -- each CE's four columns are "
            "one %d-aligned LUT granule, so their LMAs differ only in bits 2 "
            "and 3 and cancel, which holds for the unreadable CE0 group too.  "
            "AU0's dw1 is the RAID parity marker alone (every sequence here is "
            "below 2**31, so its shard is 0) and AU1's is the members' sequence "
            "XOR.  The decisive one is weru 579 page 76: 37 of 48 columns come "
            "from the lattice, the answer is NON-zero (0x80000070), and "
            "shifting the lattice by any of +-1,2,3 destroys it.  The other "
            "three predict 0x80000000 and are weak on their own.  Also note "
            "what this does NOT contradict: the four dwords sub_1C8D8 "
            "OVERWRITES are not in the XOR, which is the check above.%s"
            % (hit, done, _LUT_GRANULE, "  FAILURES: %s" % why if why else ""))
    return 0 if ok else 1


def selftest():
    """-> 0 on success.  Prints SKIPPED for anything the local dumps cannot
    support, and never turns a missing dump into a PASS."""
    print("titania.raid -- selftest\n")
    fails = 0

    WERU_CONFIGS = _sib("ftl").WERU_CONFIGS

    # ---- 1. pure arithmetic, no media ---------------------------------
    ok = True
    for wid, cfg in sorted(WERU_CONFIGS.items()):
        ps = inline_parity_slot(cfg)
        if ps != cfg.blocks_per_weru:
            ok = False
        if cfg.slot_to_physical(ps) is not None:
            ok = False                       # ftl must still refuse it
        if _phys(cfg, ps) != (cfg.ce_base + 5, cfg.block_base + 1):
            ok = False
        st = inline_stripe(cfg, wid, 0, 0)
        if st is None:
            # `inline_stripe` returns None for a shape with no placeable parity
            # slot.  Everything below dereferences `st`, so noting the failure
            # and falling through raised an AttributeError that aborted the
            # selftest instead of reporting a FAIL -- the one thing a selftest
            # must never do.  Nothing further can be asked of a stripe that
            # does not exist.
            ok = False
            continue
        if st.width != cfg.blocks_per_weru:
            ok = False
        if st.parity in st.members:
            ok = False
        # forward and inverse must agree on every member
        for (ce, blk, pg, au) in st.members:
            if inline_parity_of(cfg, wid, ce, blk, pg, au) != st.parity:
                ok = False
        cov = inline_covered_by(cfg, wid, st.parity[0], st.parity[1], 0, 0)
        if cov != st.members:
            ok = False
        if inline_covered_by(cfg, wid, st.members[0][0], st.members[0][1],
                             0, 0) is not None:
            ok = False
    fails += not ok
    _report("stripe geometry, forward and inverse", "PASS" if ok else "FAIL",
            "%d configured WERUs; parity slot = blocksPerWeru = %d, one stripe "
            "per (page, au), %d data + 1 parity"
            % (len(WERU_CONFIGS), WERU_CONFIGS[14].blocks_per_weru,
               WERU_CONFIGS[14].blocks_per_weru))

    ok, why = stored_layer_ok(WERU_CONFIGS[14])
    fails += not ok
    _report("raw-byte layer is legal for this shape", "PASS" if ok else "FAIL",
            why)

    # ---- 1a. the parity write order tiles a run exactly once -----------
    # Pure arithmetic: over one TLC wordline and one block pair, `k` must be a
    # bijection onto 0..71.  If it were not, "the run is the unit of failure"
    # would be unusable -- two pages sharing a k, or a k nobody occupies, and
    # the run-level accounting against the parity LUT would be meaningless.
    ok, seen, base = True, {}, 3656
    for half in (0, 1):
        got = set()
        for ce in range(12):
            for dp in range(3):
                for q in (0, 1):
                    blk, pg = base + 2 * half + q, 143 + dp
                    k = parity_run_index(ce, blk, pg, base)
                    if parity_run(blk, pg, base) != (143, half):
                        ok = False
                    if k in got:
                        ok = False
                    got.add(k)
                    seen[(ce, blk, pg)] = k
        if got != set(range(PARITY_RUN_PAGES)):
            ok = False
    # the two halves must be different runs of the same wordline, and the
    # wordline must be the TLC 3-page one
    if parity_run(base, 145, base) == parity_run(base + 2, 145, base):
        ok = False
    if parity_run(base, 145, base)[0] != 143 or \
            parity_run(base, 146, base)[0] != 146:
        ok = False
    fails += not ok
    _report("parity run index tiles 0..71 exactly once", "PASS" if ok else "FAIL",
            "k = %d*ce + 2*(page-wordlineStart) + (block&1) over 12 CEs x 3 "
            "pages x 2 blocks is a bijection onto 0..%d for both halves, and "
            "the halves are distinct runs.  Measured against media in "
            "`parity_run_index`: sequence exact on 122 of 122 leave-one-out, "
            "and the parity LUT names 63..71 of 72 pages of a healthy run "
            "against 3 of 72 for each of the two runs holding the withheld "
            "WERU-592 pages"
            % (PARITY_RUN_PER_CE, PARITY_RUN_PAGES - 1))

    # ---- 1b. the stripe-id arithmetic against ftl.split_wma ------------
    _F = _sib("ftl")
    WMA_STRIDE, split_wma = _F.WMA_STRIDE, _F.split_wma
    ok = WMAS_PER_PAGE * COLUMNS == WMA_STRIDE
    n = 0
    for wma in range(0, 5 * WMA_STRIDE, 7):
        if stripe_column(wma) != split_wma(wma)[2]:
            ok = False
        if stripe_first_wma(stripe_id(wma)) != wma - wma % WMA_STRIDE:
            ok = False
        if stripe_id(wma) % WMAS_PER_PAGE:
            ok = False
        n += 1
    fails += not ok
    _report("stripeId arithmetic == ftl WMA window", "PASS" if ok else "FAIL",
            "P*C = %d*%d = %d = ftl.WMA_STRIDE; over %d WMAs "
            "Raid_GetStripeIdForWma's column IS split_wma's slot, and every "
            "stripeId is a multiple of P"
            % (WMAS_PER_PAGE, COLUMNS, WMA_STRIDE, n))

    # ---- 1b2. the wordline readability rule, against what was measured ----
    #  Not a datasheet: these are pages this board was actually read at.  The
    #  rule is only useful if it reproduces them, so it is checked against them.
    MEASURED = [  # (page, class, what the read did)
        (0, 0, "4/4"), (1, 0, "4/4"), (2, 0, "4/4"), (3, 0, "4/4"),
        (4, 0, "4/4"), (8, 1, "4/4"), (32, 1, "4/4"), (35, 1, "4/4"),
        (539, 1, "4/4 x44"),                 # weru 234's data columns
        (24, 2, "4/4 at 370 syn"), (33, 2, "0/4"), (36, 2, "0/4"),
        (16, 3, "0/4"), (34, 3, "0/4"), (40, 3, "0/4"), (37, 3, "0/4 x44"),
    ]
    ok = all(wordline_class(p)[0] == c for p, c, _w in MEASURED)
    # every class-3 page measured must have FAILED, and every class-0/1 passed
    ok = ok and all(w.startswith("0/4") for p, c, w in MEASURED
                    if c == 3 and w.startswith(("0/4", "4/4")))
    ok = ok and all(w.startswith("4/4") for p, c, w in MEASURED if c <= 1)
    ok = ok and wordline_class(500, slc=True) == (0, "pre-TLC")
    fails += not ok
    _report("wordline readability class", "PASS" if ok else "FAIL",
            "page<8 / (page-8)%%3 == 0,1,2 -> %s.  Reproduces all %d pages this "
            "board was read at: every class-0/1 page decoded 4/4 and every "
            "class-3 page failed, including weru 234 page 539 and its parity"
            % ([n for _r, n, _d in WORDLINE_CLASSES], len(MEASURED)))

    # ---- 1c. the Mso-mode placement arithmetic, forward and inverse ----
    ok, n = True, 0
    for wma in list(range(0, 3 * WMA_STRIDE)) + \
            list(range(7 * WMA_STRIDE, 7 * WMA_STRIDE + 9)) + \
            [geom.AUS_PER_TLC_WERU * 590 + 37 * WMA_STRIDE + 4 * s + a
             for s in (0, 1, 17, 47) for a in range(4)]:
        lma = mso_stripe_lma(wma)
        cov = mso_stripe_data_wmas(lma)
        if len(cov) != COLUMNS or len(set(cov)) != COLUMNS:
            ok = False
        if wma not in cov:
            ok = False                      # the AU must be in its own stripe
        if any(mso_stripe_lma(w) != lma for w in cov):
            ok = False                      # ...and every member must agree
        if lma % WMAS_PER_PAGE != wma % WMAS_PER_PAGE:
            ok = False
        if len({w // WMA_STRIDE for w in cov}) != 1:
            ok = False                      # one page of one WERU
        n += 1
    # the four AU indices of a page must be four disjoint stripes
    base = geom.AUS_PER_TLC_WERU * 590 + 37 * WMA_STRIDE
    fours = [set(mso_stripe_data_wmas(mso_stripe_lma(base + a))) for a in range(4)]
    if set.union(*fours) != set(range(base, base + WMA_STRIDE)):
        ok = False
    if any(fours[i] & fours[j] for i in range(4) for j in range(i + 1, 4)):
        ok = False
    fails += not ok
    _report("Mso stripe membership, forward and inverse",
            "PASS" if ok else "FAIL",
            "over %d WMAs: stripeLma = 4*(wma//192) + wma%%4 and its inverse "
            "name the same %d-column set, every member maps back to the same "
            "LMA, and the four AU indices of a page partition its 192 WMAs "
            "into four disjoint stripes" % (n, COLUMNS))

    # ---- 2. the msoIdTable -> parity MSO map --------------------------
    root = Path(__file__).resolve().parent.parent
    sram = root / "dumps" / "fw.sram.efc.bin"
    tbl_set = None
    if not sram.exists():
        _report("data mso -> parity mso (msoIdTable)", "SKIPPED",
                "no %s -- the msoIdTable is not available offline" % sram)
    else:
        tbl = sram.read_bytes()[0x175049:0x175049 + 126]
        m = mso_parity_map(tbl)
        # Not `anchor`.  This is a boolean verdict; section 4 below binds
        # `anchor` to an msoId.  Two meanings under one name in one function is
        # how a bool ends up being passed to `lma_to_physical` as a partition
        # id, and the checker could not tell the two apart either.
        tbl_ok = (m.get(32) == 34 and m.get(33) == 34 and m.get(53) == 54
                  and m.get(59) == 62 and m.get(43) is None)
        fails += not tbl_ok
        named = {k: v for k, v in m.items() if v is not None}
        tbl_set = parity_msos_from_table(tbl)
        tbl_ok = tbl_ok and tbl_set == [34, 35, 39, 54, 62]
        _report("msoIdTable column 5 (ParityData write fronts)",
                "PASS" if tbl_ok else "FAIL",
                "column 5 names %s ; %d data msoIds sit in a row that names one "
                "(%s).  This is NOT the data -> parity map -- it names which "
                "ParityData write front an msoType allocates from, and 43/44/45/"
                "48/51 having none simply means they own no parity front"
                % (tbl_set, len(named), sorted(named)))

    # ---- 3. the media round trip --------------------------------------
    dump = config.dump_path()
    if not dump.exists():
        _report("held-out reconstruction on real media", "SKIPPED",
                "no %s" % dump)
        _report("mso-mode round trip", "SKIPPED", "needs media")
        return 1 if fails else 0

    DumpDevice = _sib("dumpdev").DumpDevice
    dev = DumpDevice(dump)

    # ---- 2b. the parity MSOs, from the board's own msoDescriptor -------
    try:
        buf = _sib("ftl").Ftl.from_device(dev)._section("mso")[0]
        got = parity_msos(bytes(buf))
    except Exception as exc:
        got = None
        _report("parity MSOs from msoDescriptor +17", "SKIPPED",
                "msoDescriptor unreadable here: %s" % exc)
    if got is not None:
        agree = tbl_set is None or got == tbl_set
        fails += not (got and agree)
        _report("parity MSOs from msoDescriptor +17",
                "PASS" if (got and agree) else "FAIL",
                "auxLayout == cAuxLayoutForRaidParityMso for msoIds %s%s"
                % (got, "" if tbl_set is None
                   else " -- %s the msoIdTable's column-5 set %s"
                        % ("AGREES with" if agree else "DISAGREES with",
                           tbl_set)))

    # NOTE on what is and is not independent.  Rebuilding slot t is
    # `XOR(all 12) ^ page[t]`, so "all eleven held-out rebuilds are exact" and
    # "the twelve-way XOR is zero" are the same fact, once per page.  The
    # independent evidence is the 18432 bytes per page, not the eleven
    # hold-outs -- both numbers are printed so neither can be misread.  The
    # hold-out is still what is run, because it is what a recovery does.
    tot_ok = tot_bad = tot_deg = 0
    per = []
    for wid in (14, 20, 22):
        cfg = WERU_CONFIGS[wid]
        blocks = [_phys(cfg, s) for s in range(cfg.blocks_per_weru + 1)]
        ok_w = bad_w = deg = worst = 0
        for page in range(cfg.pages_per_block):
            try:
                pages = [_read(dev, c, b, page, cfg.slc) for c, b in blocks]
            except Exception:
                continue
            if all(p.count(0xFF) == len(p) for p in pages):
                deg += 1
                continue                     # degenerate: nothing to prove
            full = _xor(pages)
            for t in range(cfg.blocks_per_weru):
                if _xor([full, pages[t]]) == pages[t]:
                    ok_w += 1
                else:
                    bad_w += 1
            worst = max(worst, sum(1 for v in full if v))
        per.append((wid, ok_w, bad_w, deg, worst))
        tot_ok += ok_w
        tot_bad += bad_w
        tot_deg += deg
    npages = (tot_ok + tot_bad) // WERU_CONFIGS[14].blocks_per_weru
    good = tot_ok > 0 and tot_bad <= tot_ok // 20
    fails += not good
    _report("held-out reconstruction on real media", "PASS" if good else "FAIL",
            "%d/%d single-slot rebuilds byte-exact over all 18432 raw bytes, "
            "across %d written pages of werus %s (%d all-erased pages skipped "
            "as degenerate)"
            % (tot_ok, tot_ok + tot_bad, npages, [p[0] for p in per], tot_deg))
    for wid, o, b, d, w in per:
        print("          weru %-3d %5d exact, %4d off; worst page differs in "
              "%d of 18432 bytes (a raw NAND bit error in one of the twelve "
              "members; the message layer resolves it)" % (wid, o, b, w))

    # ---- 3b. How many parities?  the whole CE0 question turns on this --
    dim1 = {}
    flagged = {}
    codec = _sib("codec")
    for wid in (20, 22):
        cfg = WERU_CONFIGS[wid]
        blocks = [_phys(cfg, s_) for s_ in range(cfg.blocks_per_weru + 1)]
        for page in range(cfg.pages_per_block):
            try:
                v = [_read(dev, c, b, page, cfg.slc) for c, b in blocks]
            except Exception:
                continue
            r = parity_relations(v)
            if r is None:
                continue
            if r[0] == 1:
                dim1.setdefault(wid, []).append((page, r[1][0]))
        for s_, (ce, blk) in enumerate(blocks):
            hit = tot = 0
            for page in range(0, cfg.pages_per_block, 9):
                try:
                    pg = _read(dev, ce, blk, page, cfg.slc)
                    if pg.count(0xFF) == len(pg):
                        continue
                    res = codec.decode_page(
                        pg, row=geom.compose_row(blk, page),
                        fmt=codec.FORMAT_A, correct=True)
                except Exception:
                    continue
                for a in res.aus:
                    if a.aux is not None:
                        tot += 1
                        hit += (bytes(a.aux)[7] >> 7) & 1
            flagged[(wid, s_)] = (hit, tot)
    allslots = list(range(WERU_CONFIGS[20].blocks_per_weru + 1))
    single = bool(dim1) and all(rel == allslots
                                for v in dim1.values() for _p, rel in v)
    fails += not single
    _report("EXACTLY ONE parity relation per stripe",
            "PASS" if single else "FAIL",
            "on the pages whose 12 members are linearly independent bar one "
            "relation (%s), that sole relation is over ALL TWELVE slots -- so "
            "a SECOND erasure is not correctable"
            % ", ".join("weru %d: %d pages" % (w, len(v))
                        for w, v in sorted(dim1.items())))
    par_slots = sorted({s_ for (w, s_), (h, t) in flagged.items()
                        if t and h == t})
    other = max((h * 100 // t) for (w, s_), (h, t) in flagged.items()
                if t and s_ not in par_slots) if flagged else 0
    _report("aux parity flag (byte 7 bit 7)", "PASS" if par_slots else "FAIL",
            "slots %s are flagged on 100%% of decoded AUs, every other slot on "
            "<=%d%%.  RESOLVED 2026-08-06: slot 0 is the DATA member sub_1C3CC "
            "marks and slot %d's copy is the ECU RAID accumulator's XOR of it "
            "(the 12-way XOR is exactly zero, so it cannot be an independent "
            "write).  ONE marker, ONE relation, ONE erasure -- still not a "
            "second parity" % (par_slots, other,
                               WERU_CONFIGS[20].blocks_per_weru))

    # ---- 4. cRaidTypeMso: the parity LUT, on this board's own media ----
    _F = _sib("ftl")
    UNKNOWN = _F.UNKNOWN
    ftl = anchor = reg = None
    try:
        ftl = _F.Ftl.from_device(dev)
        anchor = parity_lut_mso(ftl)
        reg = ftl.lut_region(anchor)
    except Exception as exc:
        reg = None
        _report("parity LUT (lutId 4) sized to the stripeId space", "SKIPPED",
                "could not walk to it: %s" % exc)
    if reg is not None and reg is not UNKNOWN:
        # `reg` is the witness for the other two.  It is assigned only on the
        # last line of the try, after `ftl` and `anchor`, so a non-None `reg`
        # proves both are bound -- which is why the whole block below may use
        # them.  Stated rather than implied, because the three used to be bound
        # only inside the try and every reader (and the checker) had to
        # reconstruct this argument to know the block was safe.
        assert ftl is not None and anchor is not None
        want_lmas = WMAS_PER_PAGE * (_F.WERU_COUNT_IN_VALID_MAP
                                     * geom.AUS_PER_TLC_WERU // WMA_STRIDE)
        good = (reg.granularity == WMAS_PER_PAGE
                and reg.max_lma + 1 == want_lmas
                and reg.n_entries == want_lmas // WMAS_PER_PAGE)
        fails += not good
        _report("parity LUT (lutId 4) sized to the stripeId space",
                "PASS" if good else "FAIL",
                "msoId %d resolves to the region at base LMA %d of msoId %d: "
                "granularity %d, %d entries, maxLma %d.  P*(%d werus * %d / %d) "
                "= %d = maxLma+1 and /P = the entry count, both exact"
                % (anchor, reg.base, reg.store_mso, reg.granularity,
                   reg.n_entries, reg.max_lma, _F.WERU_COUNT_IN_VALID_MAP,
                   geom.AUS_PER_TLC_WERU, WMA_STRIDE, want_lmas))

        # 4b.  Every parity page this dump holds in native-TLC mode: does the
        #      stripeId in each page's own aux agree with the LUT that filed it
        #      there?  Two independent structures, one answer.
        #
        #      Four outcomes, and collapsing any two of them would be a lie:
        #        hit    the LUT files that stripeId at exactly this page
        #        bit1   one flipped bit away from it, on a page the MCRC rejects
        #        nomap  the LUT maps that stripeId nowhere -- a superseded parity
        #               AU, which is the ordinary state of a log-structured store
        #               and is not a disagreement
        #        bad    the LUT files it at a different page.  This is the only
        #               outcome that refutes anything, and it must stay 0.
        codec = _sib("codec")
        cases = [(1, 3656, p) for p in (8, 64, 256, 600)]
        # read 2026-08-06 for this test: the page-0 parity AUs of the two
        # ParityData block runs, plus the parity of weru 573 page 0
        cases += [(ce, blk, 0) for ce in (1, 6) for blk in
                  (2272, 2273, 2274, 2275, 3596, 3597, 3598, 3599)]
        cases += [(1, 3674, 2)]
        hit = bit1 = bad = skip = nomap = 0
        ctrl_neigh = ctrl_other = 0
        detail = []
        seen = []
        for ce, blk, pg in cases:
            try:
                raw = dev.read_page(ce=ce, block=blk, page=pg, slc=False)
            except Exception:
                skip += 1
                continue
            res = codec.decode_page(raw, row=geom.compose_row(blk, pg),
                                    fmt=codec.FORMAT_A, correct=True)
            sid = aux_stripe_lma(res.aus)
            if sid is None:
                skip += 1
                continue
            mcrc = sum(1 for a in res.aus if a.mcrc_ok)
            # Only pages that actually carry the cAuxLayoutForRaidParityMso
            # shape are being asked the question.  Page 0 of a ParityData-owned
            # block often does not (66 of the 80 re-read on 2026-08-06 do not),
            # and reading a stripeId out of an ordinary aux would be inventing
            # a datum, not testing one.
            import struct as _st
            lmas = [_st.unpack_from("<I", bytes(a.aux), 0)[0]
                    if a.aux is not None else None for a in res.aus]
            if lmas != [0, 0, 0, 0]:
                skip += 1
                continue
            here = (ce, blk, pg)
            seen.append((here, sid, mcrc))

            def _at(lma):
                """-> (ce, block, page) the parity LUT files `lma` at, or None."""
                p = ftl.lma_to_physical(anchor, lma)
                if p is UNKNOWN or p is None:
                    return None
                return (p.ce, p.block, p.page)

            if _at(sid) == here:
                hit += 1
                detail.append("ce%d/b%d/p%d ok (mcrc %d/4)" % (ce, blk, pg, mcrc))
            elif _at(sid) is None:
                # The table maps this stripeId nowhere.  On a log-structured
                # store that is a superseded parity AU, not a contradiction --
                # counting it as `bad` would manufacture a refutation.
                nomap += 1
            else:
                # Is the aux field a single flipped bit away from a stripe the
                # LUT does file here?  Only meaningful on a page the MCRC
                # rejects, so the mcrc count is printed alongside.
                near = None
                for b in range(25):
                    if _at(sid ^ (1 << b)) == here:
                        near = b
                        break
                if near is not None:
                    bit1 += 1
                    detail.append("ce%d/b%d/p%d off by bit %d (mcrc %d/4)"
                                  % (ce, blk, pg, near, mcrc))
                else:
                    bad += 1
                    detail.append("ce%d/b%d/p%d aux %d, lut files it elsewhere "
                                  "(mcrc %d/4)" % (ce, blk, pg, sid, mcrc))
        # Controls: a neighbouring stripe must not resolve to the same page, and
        # no other held parity page's stripeId may resolve to this one either.
        def _at2(lma):
            p = ftl.lma_to_physical(anchor, lma)
            if p is UNKNOWN or p is None:
                return None
            return (p.ce, p.block, p.page)
        for here, sid, _m in seen:
            for d in (-WMAS_PER_PAGE, WMAS_PER_PAGE,
                      -WMAS_PER_PAGE * COLUMNS, WMAS_PER_PAGE * COLUMNS):
                if sid + d >= 0 and _at2(sid + d) == here:
                    ctrl_neigh += 1
            for h2, s2, _m2 in seen:
                if h2 != here and _at2(s2) == here:
                    ctrl_other += 1
        good = hit > 0 and bad == 0 and ctrl_neigh == 0 and ctrl_other == 0
        fails += not good
        _report("parity page aux stripeId == its LUT key",
                "PASS" if good else ("FAIL" if seen else "SKIPPED"),
                "%d EXACT, %d WRONG, %d off by a single bit, %d whose stripeId "
                "the table maps nowhere (a superseded parity AU, not a "
                "disagreement), %d of the %d candidate addresses carrying no "
                "parity-aux shape or not held -- %s.  A 25-bit field agreeing "
                "with an independently-derived stripeId is 1 in 2^25 per page"
                % (hit, bad, bit1, nomap, skip, len(cases),
                   "; ".join(detail) or "none"))
        _report("  control: neighbouring stripe -> same page",
                "PASS" if ctrl_neigh == 0 else "FAIL",
                "%d (must be 0); also %d for any other held parity page"
                % (ctrl_neigh, ctrl_other))

        # 4b2. Every live entry of the whole table, structurally.  If the LMA
        #      space were not the stripeId space these would not hold: the
        #      targets would not all be granule-aligned, they would collide, and
        #      they would not land exclusively inside ParityData-owned WERUs.
        import struct as _struct
        wmap = {}
        for w_ in (ftl.werus() or ()):
            wmap[w_.weru_id] = w_.owner
        pset = set(parity_msos(bytes(ftl._section("mso")[0])))
        live = {}
        for i in range(reg.n_table_lmas):
            buf = ftl.read_lma(reg.store_mso, reg.data_lma + i)
            if buf is UNKNOWN or buf is None:
                continue
            for j in range(0, len(buf), 4):
                e = i * (len(buf) // 4) + j // 4
                if e >= reg.n_entries:
                    break
                v = _struct.unpack_from("<I", buf, j)[0]
                if v not in _F.LUT_SENTINELS:
                    live[e] = v
        misalign = sum(1 for v in live.values() if v % WMAS_PER_PAGE)
        collide = len(live) - len(set(live.values()))
        owners = {}
        for v in live.values():
            owners[wmap.get(v // geom.AUS_PER_TLC_WERU)] = \
                owners.get(wmap.get(v // geom.AUS_PER_TLC_WERU), 0) + 1
        stray = sum(n for o, n in owners.items() if o not in pset)
        good = (live and misalign == 0 and collide == 0 and stray == 0)
        fails += not good
        _report("every parity LUT entry is a parity AU",
                "PASS" if good else "FAIL",
                "%d live entries: %d not granule-aligned, %d target WMAs "
                "claimed twice, %d landing outside a ParityData-owned WERU.  "
                "Owners hit: %s, all in the auxLayout-3 set %s"
                % (len(live), misalign, collide, stray,
                   dict(sorted((k, v) for k, v in owners.items())),
                   sorted(pset)))

        # 4c.  end to end: mso_stripe on a real WMA
        try:
            probe = geom.AUS_PER_TLC_WERU * 590 + 37 * WMA_STRIDE
            st = mso_stripe(ftl, probe, anchor)
            # `mso_stripe` documents three returns and two of them are not a
            # Stripe.  The detail string below is a `%` tuple, and Python
            # evaluates it whichever branch the status picks -- so on a dump
            # that does not hold this stripe, `st.stripe_lma` raised an
            # AttributeError, the outer handler caught it, and the row read
            # "FAIL AttributeError(...)".  That destroyed the diagnostic and
            # called a page nobody holds a refutation.  The three cases are now
            # kept apart, which is the same distinction the docstring insists
            # the caller must make.
            if st is UNKNOWN:
                _report("mso_stripe end to end", "SKIPPED",
                        "the parity LUT page for wma %d is not held in this "
                        "dump, so the stripe was never resolved -- not "
                        "measured, which is not the same as wrong" % probe)
            elif st is None:
                fails += 1
                _report("mso_stripe end to end", "FAIL",
                        "the parity LUT is readable here but maps stripeLma "
                        "%d NOWHERE, so wma %d has no parity AU"
                        % (mso_stripe_lma(probe), probe))
            else:
                ok = (st.width == COLUMNS
                      and st.parity_wma == ftl.lma_to_wma(
                          anchor, mso_stripe_lma(probe))
                      and st.parity not in st.members)
                layer, why = mso_stored_layer_ok(st) if ok else (None, "n/a")
                fails += not (ok and layer is False)
                _report("mso_stripe end to end",
                        "PASS" if (ok and layer is False) else "FAIL",
                        "wma %d -> stripeLma %d -> parity wma %s = %s ; %d "
                        "data AUs + 1 parity.  stored-layer XOR is correctly "
                        "refused: %s"
                        % (probe, st.stripe_lma, st.parity_wma, st.parity,
                           st.width, why))
        except Exception as exc:
            fails += 1
            _report("mso_stripe end to end", "FAIL", repr(exc))

        # 4d.  The AUX is not in the MSO PARITY relation.  Measured on the one
        #      stripe whose 44 live columns and parity AU were all read at MCRC
        #      4/4 (weru 573 page 0 / ce1 blk 3674 page 2, 2026-08-06).
        DW, DP, DBASE = 573, 0, 2268
        PAR = (1, 3674, 2)
        try:
            cols = [(c, DBASE + q, DP) for c in range(1, geom.CE_COUNT)
                    for q in range(4)]
            aux = {}
            mcrc4 = 0
            for (c, b, p_) in cols + [PAR]:
                raw = dev.read_page(ce=c, block=b, page=p_, slc=False)
                r = codec.decode_page(raw, row=geom.compose_row(b, p_),
                                      fmt=codec.FORMAT_A, correct=True)
                mcrc4 += (sum(1 for x in r.aus if x.mcrc_ok) == 4)
                aux[(c, b)] = [bytes(x.aux) for x in r.aus]
            xors = []
            for au in range(WMAS_PER_PAGE):
                acc = bytearray(8)
                for k in cols:
                    for j, v in enumerate(aux[(k[0], k[1])][au]):
                        acc[j] ^= v
                xors.append(bytes(acc))
            par_aux = aux[(PAR[0], PAR[1])]
            data_zero = all(x == b"\x00" * 8 for x in xors)
            par_nonzero = all(a != b"\x00" * 8 for a in par_aux)
            sid_here = aux_stripe_lma(
                [type("A", (), {"aux": a})() for a in par_aux])
            sid_want = stripe_id(DW * geom.AUS_PER_TLC_WERU + DP * WMA_STRIDE)
            good = (mcrc4 == len(cols) + 1 and data_zero and par_nonzero
                    and sid_here == sid_want)
            fails += not good
            _report("Mso parity does NOT cover the aux", "PASS" if good
                    else "FAIL",
                    "weru %d page %d: %d/%d pages MCRC 4/4; the XOR of the %d "
                    "held data auxes is ZERO at all four AU indices (%s) while "
                    "the parity aux is not (%s); and the parity's AU-2 stripeId "
                    "field reads %s == 4*(%d*1104+%d) == %s, which no XOR of "
                    "per-AU sequence numbers could produce.  So the relation "
                    "covers the 4096 USER bytes and NOT the 8 aux bytes"
                    % (DW, DP, mcrc4, len(cols) + 1, len(cols), data_zero,
                       par_aux[0].hex(), sid_here, DW, DP, sid_want))
        except Exception as exc:
            _report("Mso parity does NOT cover the aux", "SKIPPED",
                    "the stripe read on 2026-08-06 is not in this dump: %r" % exc)

        # 4e.  ...But the PARITY AUX *is* the members' AUX xor-ed, and it is
        #      predictable from them.  The two statements do not conflict: the
        #      four dwords `sub_1C8D8` overwrites (AU2's and AU3's second) are
        #      the stripeId and the sequence and are not the XOR; the six it
        #      leaves alone are.  See `titania.auxdata`'s "the carried dwords".
        fails += _check_carry_on_media(dev, ftl)

    _report("cRaidTypeMso XOR verified on media", "SKIPPED",
            "PLACEMENT is measured -- see the stripeId test above, now 10 exact "
            "and 0 wrong -- but the XOR ITSELF still is not, and must not be "
            "reported as if it were.  It CANNOT be, on this board: a stripe is "
            "48 columns wide with ONE parity AU and `slot = 4*ce + q` puts four "
            "of those columns on the dead CE0, so every Mso stripe here carries "
            "four erasures against a code that corrects one.  The full stripe "
            "weru 573 page 0 was read on 2026-08-06 -- 44 live columns and the "
            "parity AU, 45/45 at MCRC 4/4 -- and the residual is non-zero with "
            "entropy 7.9508 b/B; a DELIBERATELY WRONG parity page gives 7.9560, "
            "so the residual is not a discriminator and no statistic of a "
            "ciphertext XOR can be.  Coverage is no longer the obstacle; the "
            "dead die is.")
    _report("CE0 reconstruction", "SKIPPED",
            "not possible on this board: WERU config 2 is blocksPerWeru 12 / "
            "blocksPerCe 2 (efc_cpu2.c:107815), so the only Inline WERUs that "
            "span CE0 (19 and 23) put TWO of their twelve members on it, "
            "against one parity relation.  TLC WERUs are worse: 4 of 48.")
    return 1 if fails else 0


if __name__ == "__main__":                              # pragma: no cover
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    print(__doc__)
