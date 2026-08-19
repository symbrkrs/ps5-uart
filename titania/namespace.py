"""titania.namespace -- the top of the storage stack: host LBA -> MSO + LMA.

Below this module the drive is partitions and logical media addresses.  Above
it, it is an NVMe device with namespaces and 512-byte sectors.  This module is
that last translation, and unlike everything under it, it is not a
reconstruction: the host-side conversion is a shift by a constant the firmware
computes at init from a static table, and both the table and the arithmetic are
in the decompiled image.

    from titania import Ftl, DumpDevice
    from titania.namespace import NamespaceTable

    ns = NamespaceTable.from_ftl(Ftl.from_device(DumpDevice()))
    print(ns.describe())
    lma, off = ns.lba_to_lma(1, 12345)          # nsid 1, host LBA 12345
    data     = ns.read_lba(1, 0)                # 512 bytes, or None / UNKNOWN


THE ANSWER, IN ONE LINE
-----------------------
::

    lma = lba >> 3          offset_in_lma = (lba & 7) * 512

There is no per-namespace base, no stride and no remap.  Each namespace has its
own LMA space starting at 0, and the host CPU does the shift itself before the
media CPU ever sees the command.  Everything below is the evidence.


How the shift is established (five independent instances)
---------------------------------------------------------
1. `cLBAFTable` (`efc_cpu0.c:2452-2456`) is a 2-entry static table of
   `(hlbuDataSize, hlbuTotalSize, lmuDataSize, lmuTotalSize, ..., disabled)`.
   `HostLbaFormat_Init` (`efc_cpu0.c:404DFAF2`, at `:96389-96426`; the
   `disabled` test is `:96396`, the modulus assert `:96400`, the ctz `:96410`) skips entries
   whose `disabled` byte is set -- entry 0 is disabled, entry 1 is not -- and
   for each survivor asserts
   ``(pDeviceDataFormat->lmuDataSize % pDeviceDataFormat->hlbuDataSize) == 0``
   (`HostLbaFormat.c:82`), then stores
   ``hlbusPerLmu = lmuDataSize / hlbuDataSize`` and
   ``shift = ctz(hlbusPerLmu)`` into `hostLbaFormat.formats[i]`.
   Entry 1 is `hlbuDataSize 512, lmuDataSize 4096` -> 8 and 3.

2. The read/write path.  `sub_A6B4` (`efc_cpu0.c:58194-58201`) builds the media
   message with ``startLma = sub_3F4(lba_lo, lba_hi, hostLbaFormat.formats[0]
   .field_2)`` and ``lmaCount = ((lba+nlb-1)>>shift) - startLma + 1``.
   `sub_3F4` (`efc_cpu0.c:46223-46229`) is a plain 64-bit right shift.  The
   message carries an LMA, not an LBA -- the media CPU never sees a sector
   number.

3. The Dataset-Management (trim) path does the same shift independently:
   `Queue_48_SendCmd_1` (`efc_cpu0.c:57976-57978`).

4. The media side agrees.  `MemIo.c` derives its host block count as
   ``blockCount = lmaCount * lmuDataSize / hlbuDataSize`` and its
   hlbu-per-lmu ratio as ``lmuTotalSize / hlbuTotalSize`` (`efc_cpu2.c:47055`/`:47059`
   and `:47158`), and panics on ``hlbuDataSize != 0`` (`MemIo.c:796`) --
   which is exactly why the disabled entry 0, whose `hlbuDataSize` is 0, can
   never be selected.

5. NVMe reports it.  `IdentifyControllerData` sets awupf (Identify Controller
   byte 528) to ``0x1000 / hlbuDataSize - 1`` = 7, i.e. the atomic write unit on
   power fail is 8 host blocks = one LMA (`efc_cpu0.c:90751`), and
   `IdentifyNamespace` sets lbads to ``31 - clz(hlbuDataSize)`` = 9, i.e.
   512-byte logical blocks, with MS = 0 (`efc_cpu0.c:90826`).

A sixth, weaker corroboration: Dataset Management requires every range to be a
multiple of ``0x10000 / hlbuDataSize`` = 128 LBAs (`efc_cpu0.c:57388`),
which is 64 KiB = the 16-LMA LUT granularity of every user MSO on this board.


WHAT A NAMESPACE IS
-------------------
`cMaxNumberOfMediaSpaces == 3` (`MediaSpace.c:78`, `efc_cpu0.c:72723`) and the
controller advertises nn = 3 unconditionally (`efc_cpu0.c:90754`).  A
"mediaSpace" record is 16 bytes in the SystemMetadata `mediaSpace` section and
carries the msoGroupId; the msoGroupId is the namespace.  nsid n uses
mediaSpace n-1.

A namespace's size comes from one nominated MSO (`sub_77D0`,
`efc_cpu0.c:55143-55182`)::

    nsid 1,2:  msoIdTable[0 * 6 + group]      -> type 0  = UserData
    nsid 3:    msoIdTable[10 * 6 + group]     -> type 10 = UserSlcData
    NSZE (in host LBAs) = msoDescriptor[thatMso].totalLmas
                          * cLBAFTable[msoDescriptor+2].lmuDataSize
                          / cLBAFTable[live].hlbuDataSize

The `+ 60` base and the `byte_900130` index in that function are literally
`msoIdTable + 10*6` and `mediaSpace[2].msoGroupId`; nsid 3 is sized from its SLC
partition rather than from a UserData one, and there is no UserData/NSID3 entry
in the table at all.

A namespace's MSO set is enumerated by `MediaSpace.c` (`efc_cpu0.c:72723-72740`)
as ``{ msoIdTable[t*6 + group] : t in 0..16 }`` -- 17 types, so the four
cell-mode/sentinel types 17..20 are excluded -- plus, for mediaSpace 0 only,
the three ParityData MSOs `msoIdTable[5]`, `[11]`, `[101]` (= types 0, 1 and 16
crossed with group 5).  On this board those are msoIds 34, 35 and 62, and they
are exactly the three ParityData descriptors whose `msoDescriptor +4` reads 1
while the other two (39, 54) read 0.  That is a two-way check: the code names
three, the media stamps the same three.


WHICH MSO IS AUTHORITATIVE FOR A READ
-------------------------------------
None of them, and the question dissolves.  The MSO is not part of a read
address.

`msoDescriptor +3` is the `lutId`, and the lookup table is selected by lutId and
nothing else: ``&_lut.lookupTable[msoDescriptor[msoId].lutId]``, the same
expression at `efc_cpu2.c:23031`, `:23726`, `:23778`, `:56018`, `:109633`,
`:109759`, `:109888`, `:109906`, `:109917` and `:109955`.  The array is
``mem_clr(&_lut, 8 * 296)`` (`efc_cpu2.c:110123`) -- `cMaxNumberOfLuts == 8` --
and a lookup is ``wma = pLookupTable->pTable[lma >> pLookupTable->shift]``
(`efc_cpu2.c:110349`).  Every LUT on this drive has `numberOfSegments == 1`
(asserted at `Lut.c:1696`; the only `Lut_Create` call site passes 1,
`efc_cpu2.c:111441`), so the table is flat.

So all the MSOs of a namespace -- GarbageCollection, Migration,
UserDataNandCache, BlockMove, CopySlcData, StaticMan, DirectTlcWrite, and the
dynamic sub-32 ones -- are write producers sharing one address space, not
alternative copies to choose between.  `Lut_Update(msoId, lmas, wmas, n)`
(`efc_cpu2.c:23178-23294`) resolves `lutId` from the *writer's* msoId, writes
``pTable[lma >> shift] = wma`` into that one shared table, and explicitly
invalidates the WMA it displaced -- panicking if a writer tries to install a
WMA that is already there (`Lut.c:1042`).  At most one WMA is live per
(namespace, granule) at any instant, by construction.

Measured here: for all six lutIds and 15 sampled LMAs each, every MSO sharing a
lutId returns the identical value -- 90 agree, 0 disagree, 0 unknown
(`NamespaceTable.check_lut_identity`).

What an msoId *does* tell you after the fact is which partition physically holds
the live copy: resolve the LMA to a WMA, take its WERU, and read that WERU's
owner.  `NamespaceTable.authoritative_mso` does exactly that, and it is a
derived answer, never a choice.


The lutId 2 and lutId 3 slots are not TABLES at all -- they are the LPT buffer
------------------------------------------------------------------------------
The working assumption this module was written against -- "`lutId` indexes one
LMA address space per namespace group, and every MSO of a group shares one
table" -- is half right.  The sharing is real.  The claim that the shared
table maps that namespace's LMAs to WMAs is FALSE for NSID2 and NSID3, and
`NamespaceTable.lut_coverage` measures it::

    lutId 1 (NSID1)      region covers 175085568 LMAs, totalLmas 175085568  1.00x
    lutId 4 (ParityData) region covers   4292352 LMAs, totalLmas   4292352  1.00x
    lutId 2 (NSID2)      region covers   1654784 LMAs, totalLmas  43771392 26.45x
    lutId 3 (NSID3)      region covers    114688 LMAs, totalLmas   5157888 44.97x

What those two "tables" are is now settled, and it is not a table.
`sub_40340464` (`efc_cpu2.c:111415-111458`) branches on `Mso_GetMsoGroupId` =
`unk_900830[2*msoId + 1]` (`efc_cpu2.c:43733`) and, for groups 2 and 3 only,
throws the caller's entry count away and substitutes
``count = roundup(409760 or 26048, granuleBytes) / 4``.  With the LUT allocator's
granule of one LMA that is ``ceil(409760/4096)*4096/4 = 103424`` and
``ceil(26048/4096)*4096/4 = 7168`` -- exactly the entry counts measured on
media, both of them, from the same one-line formula.  `Lut_Create` then skips
the `count = totalLmas >> shift` step for those two tags
(`efc_cpu2.c:109509-109521`), which is why the header's `max_lma` is
`n_entries - 1`.

It then hands the allocation straight to the logical-partition layer:
`sub_40340170(pTable, 1)` and `sub_403402D6(pTable, 1)` (`efc_cpu2.c:111229`,
`:111311`) store it as `pLpt` -- `dword_80C2BC` for NSID2, `dword_80C2C0` for
NSID3 -- and `mem_clr` it for 0x640A0 = 409760 and 0x65C0 = 26048 bytes.
So the lutId 2 and 3 slots hold the Logical Partition Table plus its
superblock array, borrowed through the LUT allocator.  The buffer's own
layout, from the firmware's `Debug_WriteData3` calls at `:111281-111283` and
`:111328-111330`, the `mem_clr`s at `:120500-120514`, and the accessors::

              NSID2 (409760 B)                NSID3 (26048 B)
    +0        u16 highest live lpId           same
    +2, +4    u16, u16                        same
    +8        LptEntry[16384] x 24 B          LptEntry[1024] x 24 B
    +393224   u32                             --
    +393230   u16, u16                        +24584 u16, u16
    +393234   L2S u16[8260]                   +24588 L2S u16[730]

`sub_40347A12` (`efc_cpu2.c:119192-119196`) states the two L2S extents as
16520 and 1460 bytes independently.  `sub_403403A0` (`:111374`) is
`pLpt + 8 + 24*lpIndex`, and `sub_A872` (`:25530`) is `l2sBase + 2*l2sIndex`.


THE REAL L2P FOR NSID2 AND NSID3
--------------------------------
`PerformanceReadForPhaseControl` (`efc_cpu2.c:39343-39405`) is a clean
dichotomy on `Mso_GetMsoGroupId(msoId)`: 1 takes `Lut_GetEntry`
(`sub_8058`, `:23027`), 2 and 3 take `sub_9DCE(nsIdx, lpId, lma)`
(`:39404`), and anything else panics "Invalid MSO group ID"
(`PhaseControl.c:5132`).  `sub_9DCE` (`efc_cpu2.c:24873-25080`) is the L2P::

    nsIdx        = msoGroupId - 2                            (0 = NSID2, 1 = NSID3)
    attr         = lpt[lpId].u16[0] & 3          must be 1 or 2   :24955
    l2sIndex     = lpt[lpId].u16[0] >> 2                          :24980
    slice        = l2sBase + 2*l2sIndex
    wmasPerWeru  = weruCfg[nsIdx ? 3 : 4].blocksPerWeru
                   * (cellMode == 1 ? 1472 : 4416)                :24971-24977
    sbIdx        = lma // wmasPerWeru                             :24978
    if sbIdx >= slice.u16[0] >> 4:            out of range        :24982
    if lma >= slice.u16[1]*wmasPerWeru + 16*slice.u16[2]:  past the LP  :24999
    entry        = slice.u16[3 + sbIdx]                           :25019
    weruId       = entry & 0x3FFF                                 :25065
    wma          = 211968*weruId + 16*((lma % wmasPerWeru) >> 4)  :25072

`dword_907150` is `cMaxBlocksPerWeru * 4416 = 211968` (`efc_cpu2.c:107677`) --
the same global WMA stride `weruId = wma // 211968` the rest of the toolkit
uses -- while `wmasPerWeru` is the *namespace's* WERU configuration: row 4
(48 blocks, native TLC) for NSID2 = 211968, row 3 (48 blocks, pseudo-SLC) for
NSID3 = 70656.  That is a real, independent confirmation that NSID3 is the
pseudo-SLC namespace: its size MSO is `UserSlcData` and its superblocks are
config-3 SLC WERUs.  As on the LUT path the returned WMA is granule-aligned and
the caller adds `lma & 15`; since `wmasPerWeru` is a multiple of 16 the whole
thing collapses to::

    wma = 211968 * weruId + (lma mod wmasPerWeru)

Two `entry` flags matter: 0x8000 = blank (the read is refused, `:25016`),
and 0x4000 = this superblock is currently open in an SLC write cache, in
which case the low byte is a `cacheId < 16` and the live WERU is
`*(u16 *)(pNsId2SlcCache + 212024*cacheId + 4)` (`:25053-25065`,
`Lpt.c:1400`).  A cached superblock is therefore not resolvable from persisted
state alone, and this module says UNKNOWN for it.


The host address for NSID2/NSID3 is (lpId, LBA), not LBA
---------------------------------------------------------
`sub_9DCE` needs an `lpId` that no NVMe Read command has a field for -- and the
firmware takes it from the top half of the 64-bit SLBA.  In the submission
handler (`efc_cpu0.c:56364-56412`) `field_8 = cdb.DW10` and `field_C = cdb.DW11`
are the two SLBA halves, and then, for nsid 2 and 3 only (`v16 == 1 ||
v16 == 2` where `v16 = nsid - 1`):

* the LBA is masked to 31 bits, ``DW10 & 0x7FFFFFFF``, and the extra two high
  LBA bits taken from DW11 for nsid 1 are forced to zero (`:56374-56382`);
* ``field_C >= 0x10000`` is rejected as an invalid field (`:56411`), i.e. the
  top half is a 16-bit quantity -- which is exactly `cNumOfEntries`' domain,
  and `Lpt.c:1562`/`:5619` then bound it by 16384 / 1024;
* the NSZE bound check is guarded by ``&& !v16`` (`:56395`) -- it applies to
  nsid 1 only.  A namespace whose LBA space is per-partition cannot be
  bounded by one NSZE, and the firmware does not try.

`sub_A6B4` re-applies the same ``& 0x7FFFFFFF`` before the LBA -> LMA shift and
puts `field_C` in the media message (`efc_cpu0.c:58193-58203`), and the media
side's read command carries `+14 u16 lpId` beside `+16 u32 lma`
(`efc_cpu2.c:39353-39404`).  So::

    lpId = SLBA >> 32          lba = SLBA & 0x7FFFFFFF          lma = lba >> 3

and each logical partition has its own LMA space starting at 0.
`slba_to_address` and `address_to_slba` do this; `Namespace.slba_bits` reports
which form a namespace uses.


WHERE IT PERSISTS, AND WHAT SURVIVES A DIRTY MOUNT
---------------------------------------------------
The LptEntry array is persisted; the L2S superblock array is not.

`sub_40344236` / `sub_403442B6` (`efc_cpu2.c:115632-115694`) register two
SegmentedBuffers in the Bootstrap RootStorage::

    SegmentedBuffer_Initialize("LptNsid2", hdr 6, bytes 0x60000, obj 44, elem 24)
    SegmentedBuffer_Initialize("LptNsid3", hdr 6, bytes 0x6000,  obj 74, elem 24)

`0x60000 = 16384*24` and `0x6000 = 1024*24`, and the flush marks dirty exactly
`[pLpt+8, pLpt+8+24*lpId]` (`efc_cpu2.c:116088`).  Measured on this board: the
catalogue's `LptNsid2` tag run is objects 44..69 (26 segments, 25 x 15600 +
3216 = 393216 B) and `LptNsid3` is 74..75 (15600 + 8976 = 24576 B) -- both
exact.  The 6-byte header reads `0c 00 0c 00 00 00` for NSID2, and 0x0c = 12 is
the highest live lpId, which is what `*(u16 *)dword_80C2CC` is
(`efc_cpu2.c:115602`).

Nothing in that range covers `pLpt + 393234`.  So after an unclean shutdown the
L2S has to be re-derived, and the durable source is the aux area of the media
itself.  Every group-2/3 MSO carries `msoDescriptor + 17 == 2` =
`cAuxLayoutForForNSID23` (`Mso_GetAuxLayout`, `Rebuild.c:1757` at
`efc_cpu2.c:134112`), and `WriteAuxiliaryList_InsertRebuildInformation` case 2
(`efc_cpu2.c:43052-43069`) writes, per AU::

    aux +0  u32 lma          (the lp-local lma)
    aux +4  u16 lpId
    aux +6  u16 one 16-bit slice of the 64-bit rebuild sequence number,
            a different slice in each of the page's four AUs

-- where the ordinary layout instead puts a 31-bit sequence number in +4..+7
with bit 31 the inline-RAID parity marker.  `RebuildNeo` reads it straight back
as `pAuxiliaryBuffer[i].lpId` (`efc_cpu2.c:137377`).  `L2sMap.from_media`
reproduces that: it walks the WERUs owned by a group-2/3 MSO, decodes each AU's
aux, and installs ``(nsid, lpId, lma // wmasPerWeru) -> weruId``.

Measured here, and it is the end-to-end check: of the AUs this backing can
serve inside a group-2/3 WERU, 44 of 44 satisfy
``lma mod wmasPerWeru == wma mod 211968`` across four WERUs and four distinct
lpIds -- 81 (NSID3 LP 0), 580 (NSID2 LP 9), 581 (LP 7), 582 (LP 6) -- with no
WERU claimed by two (lpId, sbIdx) keys and no key claiming two WERUs.
RETRACTED 2026-08-06: "the L2S array is not readable, so `LptEntry+2` must be
inferred from the packing", and with it the figures 24 and 66.  The array is
on media -- see the L2S array is persisted below -- and reading it settles the
count outright.

Every observed AU sits at `sbIdx == 0`, and on this drive it cannot be
anything else.  Every live NSID2 partition owns exactly one superblock, so
`sub_9DCE`'s `sbIdx >= count` bound refuses `lma >= 211968` for all twelve;
NSID3's multi-superblock partitions have blank entries past the first.  The
`lma // wmasPerWeru` step is therefore firmware-derived and unexercisable here
-- a structural negative, not a gap in the dump.  `L2sMap` reports it
(`max_sb_seen`).


THE L2S ARRAY IS PERSISTED, AND IT SETTLES THE SUPERBLOCK COUNT
---------------------------------------------------------------
The LPT+L2S buffer is allocated through the LUT allocator, so the whole
409760 / 26048 bytes -- LptEntry array, L2S array and the two accumulators --
sit in the lutId 2 / lutId 3 region of msoId 40's LMA space.  Both are held
complete in this dump (101 of 101 and 7 of 7 table LMAs).  `NamespaceTable
.lpt_buffer(nsid)` reads it and `L2sArray.from_buffer` decodes it.

One slice is `count + 4` u16, not `3 + count` (`sub_4034927C`,
`efc_cpu2.c:120655-120700`, `L2s.c`)::

    u16[0]              (numOfSuperBlocks << 4) | flags     <- the count
    u16[1]              superblocks fully written
    u16[2]              granules into the last one
    u16[3 .. 3+n-1]     n = numOfSuperBlocks entries, initialised 0x8000/0xC000
    u16[3+n]            the write slot, initialised 0x8000  <- not a superblock

The creator writes exactly `a4` entries, then one more with a different
constant, then advances the free cursor by ``a4 + 4``.  The extra slot is the
partition's open-in-SLC-cache marker: `L2s_UpdateNsid2WriteDeActivate
PartitionInfo` (`efc_cpu2.c:121954-122014`) reads `l2s[l2sIndex + count + 3]`,
migrates the cache's real weruId into the count-bounded array at its own
`sbIdx`, and resets the slot to 0x8000.  `sub_9DCE` never indexes it
(`sbIdx >= u16[0] >> 4` refuses), and `sub_4034AC88` returns `l2s[i] >> 4` as
the count.

`LptEntry + 2` is `numOfSuperBlocks` -- `sub_40344016` (`:115567-115580`)
does `HIWORD(result[0]) = numOfSuperBlocks`, the operand the panic at
`Lpt.c:2842` names.  Measured on media, nothing fitted:

* the slice count field equals `LptEntry+2` on 27 of 27 live partitions
  across both namespaces and three different values (1, 2, 35);
* the slices tile each array with no gap and no overlap at stride
  `count + 4`, ending exactly on the free cursor at `pLpt+393232` / `+24586`;
* the firmware's own running total at `pLpt+393230` / `+24584` (`+=
  numOfSuperBlocks` per create) reads 12 and 63, equal to the sum of the
  slice counts;
* NSID2's twelve entries name weruIds 577, 578, 580-589, and the aux rebuild
  finds the same twelve, partition for partition, 12/12.

RETRACTED: "12 x 2 = 24, and msoId 53 owning 24 WERUs is a second route to
it."  Both halves are wrong.  The count is 1 per NSID2 partition, and msoId
53's 24 WERUs are the twelve the L2S names (577-589) plus twelve unrelated
ones at 951-962.  It was a coincidence of two 12s, not corroboration.

One state note that is easy to misread: every live partition here carries
`attr == 3`, and `sub_9DCE` serves only 1 and 2 (`efc_cpu2.c:24955`).  So the
drive as it stands would refuse a host read of any of them until they are
re-activated -- `MediaPartitionInterface_LptDeactivateLogicalPartitions`
(`efc_cpu3.c:23158`) is a real operation and a clean shutdown runs it.  That is a
statement about the drive's state, not about the data: the partitions exist, the
superblocks exist, and this module resolves and reads them anyway.
`LogicalPartition.readable` reports the firmware's view separately so the two are
never conflated.  The name of state 3 is not established -- the firmware's own
`off_40400BB0` name table (`efc_cpu2.c:114634`) is in no image we hold.


CORRECTED COVERAGE FOR GROUPS 2 AND 3
-------------------------------------
This retracts the reachability figures `Ftl.address_coverage` reports for every
MSO of groups 2 and 3: those counts were computed through `Ftl.lma_to_wma`,
which reads the LPT buffer as if it were a LUT.  The real figures are per
logical partition, and `NamespaceTable.partition_coverage` computes them.  On
this board::

    nsid 2   12 of 16384 partitions live (lpIds 1..12), one superblock each
             = 12 superblocks x 211968 =  2,543,616 of 43,771,392 LMAs = 5.8%
             MSOs own 814 WERUs: msoId 53 BlockMove 24, msoId 44 Migration 790
    nsid 3   11 of 1024 partitions live (lpId 0 with 35 superblocks, then
             132..141 with 2 each) = 55 superblocks x 70656
             = 3,886,080 of 5,157,888 LMAs = 75.3%
             MSOs own 75 WERUs by the WeruUsageLog (msoId 48 UserSlcData 29,
             msoId 51 CopySlcData 46); the weruDescriptor says 26 + 44 = 70

So the other 94% of NSID2 is unallocated partition space, not unreadable
data -- a thin-provisioned namespace, which is what a logical-partition layer
is for.  NSID3's declared superblocks are mostly address space too: 62 of its
63 media-copy entries are blank and one is open in an SLC cache, so no NSID3
superblock has a placed WERU we can name.

The two copies of the NSID3 LPT are different generations, and the
RootStorage one is current.  The media buffer holds 15 live partitions
(0, 128..141) totalling 63 superblocks; the RootStorage SegmentedBuffer --
which is what the firmware actually restores at mount -- holds 11 (0, 132..141)
totalling 55, with lpIds 128..131 cleared to `attr == 0` and the array
compacted (lpId 132 moved from l2sIndex 63 to 39, exactly 4 x (2+4) u16).  Its
own live-partition counter reads 11, so it is self-consistent.  NSID2's two
copies agree exactly, 12/12.  (5200 NSID2 and 374 NSID3 LptEntries sit in
SegmentedBuffer segments this backing cannot serve; `Lpt.unreadable` reports it
and the media buffer covers them all.)

`titania.ftl` is not modified here; use `NamespaceTable.lut_coverage` before
trusting `Ftl.lma_to_wma` for an MSO whose lutId is 2 or 3.


WHAT THIS MODULE CANNOT DO
--------------------------
* It cannot name files.  The payload is AES ciphertext written by the console;
  addressing is the whole deliverable here.
* It cannot bound-check a namespace that has no `msoDescriptor` section -- the
  sizes are on media, not in the firmware image.
* It cannot resolve an NSID2/NSID3 address from an LBA alone.  That is not a
  gap in the reconstruction: the address is genuinely (lpId, LBA), and
  `lba_placement(2, lba)` without `lp=` returns UNKNOWN because half the
  address is missing.
* It cannot resolve a superblock that is open in an SLC write cache
  (`entry & 0x4000`): the live WERU is in DRAM, not on media.
* It cannot recover the L2S for a partition none of whose WERUs this backing
  holds -- and on this board that is most of them.
* `read_lba` is only as good as the backing device.  On this board most user
  LMAs resolve to an address in a WERU whose slot map was never measured, and
  the honest answer there is UNKNOWN, not zero bytes.
"""

import struct
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


# `py titania/namespace.py` and `py -m titania.namespace` must both work.  Run
# by path there is no parent package, so a relative import raises
# "attempted relative import with no known parent package" before anything in
# this file executes; putting the repository root on sys.path and importing by
# absolute name is the same idiom `extract.py` and `raid.py` already use.
if __package__ in (None, ""):                       # `py titania/namespace.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.ftl import (
        UNKNOWN, MSO_GROUPS, MSO_TYPES, MSOID_INVALID, LMA_BYTES,
        MSO_DESC_ENTRY, MSO_DESC_TOTAL_LMAS_OFF, MSO_DESC_LUT_ID_OFF,
        LUT_ID_NONE, MsoIdTable,
        WERU_CONFIGURATIONS, WMA_STRIDE, WMAS_PER_PAGE)
else:
    from . import geometry as geom
    from .ftl import (UNKNOWN, MSO_GROUPS, MSO_TYPES, MSOID_INVALID, LMA_BYTES,
                      MSO_DESC_ENTRY, MSO_DESC_TOTAL_LMAS_OFF,
                      MSO_DESC_LUT_ID_OFF,
                      LUT_ID_NONE, MsoIdTable,
                      WERU_CONFIGURATIONS, WMA_STRIDE, WMAS_PER_PAGE)

#: This package's name whether imported as `titania.namespace` or run as a
#: script (in which case the header above put the parent on sys.path).
_PKG = __package__ or "titania"


def _sib(name):
    """A sibling layer, reached the same way from either entry point."""
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))

__all__ = ["LbaFormat", "LBAF_TABLE", "host_lba_formats", "HOST_LBA_FORMAT",
           "LBA_BYTES", "LBAS_PER_LMA", "LBA_SHIFT", "lba_to_lma", "lma_to_lba",
           "MEDIA_SPACE_ENTRY", "MAX_MEDIA_SPACES", "MSO_DESC_LBAF_OFF",
           "MSO_DESC_GROUP_OFF", "MSO_DESC_GRANULARITY_OFF",
           "MSO_DESC_AUX_LAYOUT_OFF", "AUX_LAYOUT_NSID23",
           "SIZE_MSO_TYPE", "PARITY_ATTACH_TYPES", "MEDIA_SPACE_MSO_TYPES",
           "LOGICAL_PARTITIONS", "Namespace", "NamespaceTable",
           "LP_ID_SHIFT", "LP_LBA_MASK", "MAX_LP_ID",
           "slba_to_address", "address_to_slba",
           "LPT_ENTRY", "LPT_ARRAY_OFF", "LPT_BUFFERS", "LPT_BUFFER_BYTES",
           "L2S_OFF", "L2S_BYTES", "L2S_HEADER_U16", "L2S_TRAILER_U16",
           "L2S_SLICE_OVERHEAD_U16", "L2S_WERU_MASK",
           "L2S_IN_SLC_CACHE", "L2S_BLANK", "L2S_COUNT_SHIFT",
           "L2S_TOTAL_OFF", "L2S_NEXT_OFF", "LPT_ATTR_NAMES",
           "NS_WERU_CONFIG", "wmas_per_weru",
           "LogicalPartition", "Lpt", "L2sSlice", "L2sArray", "L2sMap"]


# ===========================================================================
# cLBAFTable -- the static host/media block-size table
# ===========================================================================
#: `efc_cpu0.c:2452-2456`, verbatim::
#:
#:     struc_16 cLBAFTable[2] = {
#:       { 0u,   0,   4096, 4096, 4096, 4096, 8, '\0', '\x01' },
#:       { 512u, 524, 4096, 4192, 4096, 4192, 8, '\f',  '\0'  } };
#:
#: The field offsets are pinned by the accesses, not guessed: `hlbuDataSize` is
#: +0 (`sub_404DFBDE` / `MemIo.c` both read `*(u32 *)pDeviceDataFormat`),
#: `lmuDataSize` is named by IDA at the third initialiser, `field_10`/`field_14`
#: are +0x10/+0x14, `field_18` is +0x18, `field_1C` is +0x1C (read as
#: `mpeccSize` at `MemIo.c` `*(u8 *)(a1 + 28)`), and `disabled` is therefore the
#: last byte, +0x1D.
#:
#: That last assignment is forced, not preferred: if `disabled` were +0x1C then
#: entry 1 (12) would be the disabled one and entry 0 would be live, and entry
#: 0's `hlbuDataSize` of 0 makes `HostLbaFormat_Init` divide by zero at
#: `HostLbaFormat.c:82` and makes `MemIo.c:796` panic.  Only one reading runs.
class LbaFormat(object):
    """One `cLBAFTable` row.  `hlbu` = host block, `lmu` = logical media unit."""

    __slots__ = ("lbaf_id", "hlbu_data", "hlbu_total", "lmu_data", "lmu_total",
                 "field_10", "field_14", "field_18", "mpecc", "disabled")

    def __init__(self, lbaf_id, hlbu_data, hlbu_total, lmu_data, lmu_total,
                 field_10, field_14, field_18, mpecc, disabled):
        self.lbaf_id = lbaf_id
        self.hlbu_data, self.hlbu_total = hlbu_data, hlbu_total
        self.lmu_data, self.lmu_total = lmu_data, lmu_total
        self.field_10, self.field_14, self.field_18 = field_10, field_14, field_18
        self.mpecc, self.disabled = mpecc, disabled

    @property
    def hlbus_per_lmu(self):
        """`lmuDataSize / hlbuDataSize`, or UNKNOWN for a format with no host
        block size at all (entry 0).  `HostLbaFormat.c:82` asserts the division
        is exact, so a non-integer result here would be a decode error."""
        if not self.hlbu_data:
            return UNKNOWN
        if self.lmu_data % self.hlbu_data:
            raise ValueError("cLBAFTable[%d]: lmuDataSize %d %% hlbuDataSize %d "
                             "!= 0, which HostLbaFormat.c:82 forbids"
                             % (self.lbaf_id, self.lmu_data, self.hlbu_data))
        return self.lmu_data // self.hlbu_data

    @property
    def shift(self):
        """`ctz(hlbusPerLmu)` -- the LBA -> LMA shift.  UNKNOWN for entry 0.

        The firmware computes it as `__clz(__rbit32(v3))`
        (`efc_cpu0.c:96410`), which is `ctz`.  A non-power-of-two ratio would
        make the shift lossy; the firmware never checks that, so we do.
        """
        n = self.hlbus_per_lmu
        if n is UNKNOWN:
            return UNKNOWN
        if n & (n - 1):
            raise ValueError("cLBAFTable[%d]: hlbusPerLmu %d is not a power of "
                             "two, so the firmware's ctz shift is lossy"
                             % (self.lbaf_id, n))
        return n.bit_length() - 1

    @property
    def lbads(self):
        """NVMe lbads: `31 - clz(hlbuDataSize)` (`efc_cpu0.c:90824`)."""
        return UNKNOWN if not self.hlbu_data else self.hlbu_data.bit_length() - 1

    def __repr__(self):
        return ("<LbaFormat %d hlbu=%d/%d lmu=%d/%d per_lmu=%s shift=%s%s>"
                % (self.lbaf_id, self.hlbu_data, self.hlbu_total, self.lmu_data,
                   self.lmu_total, self.hlbus_per_lmu, self.shift,
                   " DISABLED" if self.disabled else ""))


LBAF_TABLE = (
    LbaFormat(0, 0, 0, 4096, 4096, 4096, 4096, 8, 0, 1),
    LbaFormat(1, 512, 524, 4096, 4192, 4096, 4192, 8, 12, 0),
)


def host_lba_formats():
    """-> the live formats, in `HostLbaFormat_Init` order (`efc_cpu0.c:96380`).

    The firmware drops disabled rows and inserts by ascending `hlbuTotalSize`;
    with one survivor the order is moot, but it is reproduced so that a future
    image with two live formats decodes the same way the drive does.
    """
    out = []
    for f in LBAF_TABLE:
        if f.disabled:
            continue
        f.hlbus_per_lmu                      # raises if the modulus assert fails
        i = 0
        while i < len(out) and out[i].hlbu_total <= f.hlbu_total:
            i += 1
        out.insert(i, f)
    if len(out) > 16:
        raise ValueError("_hostLbaFormat.count < cMaxNumberOfHostLbaFormats "
                         "(16) is asserted at HostLbaFormat.c:79")
    return tuple(out)


#: The one live format.  Every LBA<->LMA conversion in the firmware goes through
#: `hostLbaFormat.formats[0]` -- literally index 0, not the command's flbas --
#: at `efc_cpu0.c:57975`, `:58097`, `:58195`, `:58378`, `:58414` and `:57740`.
HOST_LBA_FORMAT = host_lba_formats()[0]

LBA_BYTES = HOST_LBA_FORMAT.hlbu_data           # 512
LBAS_PER_LMA = HOST_LBA_FORMAT.hlbus_per_lmu    # 8
LBA_SHIFT = HOST_LBA_FORMAT.shift               # 3

if LBAS_PER_LMA * LBA_BYTES != LMA_BYTES:       # ftl.LMA_BYTES is 4096
    raise AssertionError("titania.namespace and titania.ftl disagree on the "
                         "LMA size: %d * %d != %d"
                         % (LBAS_PER_LMA, LBA_BYTES, LMA_BYTES))


def lba_to_lma(lba):
    """host LBA -> (lma, byte offset within the LMA).  Pure arithmetic.

    This is the whole translation.  `NamespaceTable.lba_to_lma` is the same
    thing with a bound check against the namespace's NSZE.
    """
    if lba < 0:
        raise ValueError("negative LBA %d" % lba)
    return lba >> LBA_SHIFT, (lba & (LBAS_PER_LMA - 1)) * LBA_BYTES


def lma_to_lba(lma):
    """LMA -> the first host LBA it holds.  Inverse of `lba_to_lma`."""
    if lma < 0:
        raise ValueError("negative LMA %d" % lma)
    return lma << LBA_SHIFT


# ===========================================================================
# the mediaSpace section and the msoDescriptor fields this layer needs
# ===========================================================================
#: `panic("mediaSpaceId < cMaxNumberOfMediaSpaces", "MediaSpace.c", 78)` guards
#: `a1 >= 3` (`efc_cpu0.c:72723`), and every loop over the array runs `i < 3`
#: (`efc_cpu0.c:96476`, `:96521`, `:96631`, `:55240`).
MAX_MEDIA_SPACES = 3

#: 16 bytes.  `byte_900110[16 * i + k]` throughout `efc_cpu0.c`.
MEDIA_SPACE_ENTRY = 16

#: The four fields, by the accesses that name them.  The firmware reads each as
#: a byte at a 4-byte-spaced offset, so the offsets are proven and the widths
#: are not -- they are decoded as u32 here because the record is 16 bytes and
#: the three upper bytes of each are zero on this drive, which is consistent
#: with either reading.
MS_GROUP_OFF = 0        # msoIdTable group index (`efc_cpu0.c:72726`, `:55164`)
MS_LBAF_OFF = 4         # -> `sub_404DFBDE` -> hostLbaFormat slot (`:96481`)
MS_STAMP_OFF = 8        # written by MediaSpace.c together with msoDesc+4 (`:72725`)
MS_VALID_OFF = 12       # "this mediaSpace exists" (`:96479`, `:96521`, `:96631`)

#: `msoDescriptor` bytes this layer reads.  `+7 u32 totalLmas` and `+3 lutId`
#: come from `titania.ftl`; these two are established here.
#: `+2` indexes `cLBAFTable` for the lmu size (`efc_cpu0.c:55173`).
MSO_DESC_LBAF_OFF = 2
#: `+4` is stamped with the owning mediaSpace's group by `MediaSpace.c`
#: (`efc_cpu0.c:72732`, `:72736-72738`).
MSO_DESC_GROUP_OFF = 4
#: `+6` is the LUT granularity in LMAs; `Lut_Create` reads it and derives
#: `log2` from it (`efc_cpu2.c:109509-109521`).  1, 4 or 16 are the only values
#: the firmware accepts (`Lut.c:348`).
MSO_DESC_GRANULARITY_OFF = 6
LUT_GRANULARITIES = (1, 4, 16)

#: Which msoType supplies a namespace's declared size (`sub_77D0`,
#: `efc_cpu0.c:55155-55174`).  nsid 3 takes the `+60` / type-10 branch.
SIZE_MSO_TYPE = {1: 0, 2: 0, 3: 10}             # UserData, UserData, UserSlcData

#: The MSO types `MediaSpace.c` walks when it claims a mediaSpace's partitions:
#: `for (result = 0; result < 17; ++result)` (`efc_cpu0.c:72728`).
MEDIA_SPACE_MSO_TYPES = 17

#: ...plus these three ParityData MSOs, and only for mediaSpaceId 0
#: (`efc_cpu0.c:72734-72738`, which index `msoIdTable` at 5, 11 and 101 =
#: types 0, 1 and 16 crossed with group 5).
PARITY_ATTACH_TYPES = (0, 1, 16)
PARITY_GROUP = 5

#: `cNumOfEntries[nsId] = { 16384, 1024 }` (`efc_cpu2.c:10450`), indexed by
#: `msoGroupId - 2`, and every caller rejects a group that is not 2 or 3
#: (`efc_cpu2.c:112063`, `:112204`, `:114800-114813`).  So NSID1 has no logical
#: partitions at all; NSID2 has 16384 (`cNumOfNs2LptEntries`, `L2s.c:632`
#: independently) and NSID3 has 1024.
LOGICAL_PARTITIONS = {2: 16384, 3: 1024}

#: `mem_clr(&_lut, 0x940)` = 8 * 296 (`efc_cpu2.c:110123`).
MAX_LUTS = 8

#: `Mso_GetAuxLayout(msoId)` is `msoDescriptor[msoId] + 17` -- read by
#: `Rebuild.c:1757` (`efc_cpu2.c:134112`), `Rebuild.c:1920` (`:133446`) and
#: `Moh.c:4183` (`:35424`).  0 = cAuxLayoutForInternalData, 1 = the ordinary
#: user layout, 2 = cAuxLayoutForForNSID23, 3 = cAuxLayoutForRaidParityMso.
#: On this board the value is a function of lutId -- 0->0, 1->1, 2->2, 3->2,
#: 4->3 -- so "which MSOs use the NSID2/3 aux" and "which MSOs share the
#: lutId 2/3 slots" are the same 42-MSO set, found two independent ways.
MSO_DESC_AUX_LAYOUT_OFF = 17
AUX_LAYOUT_INTERNAL, AUX_LAYOUT_USER = 0, 1
AUX_LAYOUT_NSID23, AUX_LAYOUT_RAID_PARITY = 2, 3


# ===========================================================================
# the logical-partition layer
# ===========================================================================
#: The host address for NSID2/NSID3 is the 64-bit SLBA split in two: the top
#: dword is the lpId and the bottom is masked to 31 bits.  Both halves are
#: pinned by `efc_cpu0.c:56364-56412` -- `field_C = cdb.DW11`, `DW10 &
#: 0x7FFFFFFF` for `nsid - 1 in (1, 2)`, and `field_C >= 0x10000` rejected --
#: and the mask is applied a second time in `sub_A6B4` (`efc_cpu0.c:58193`).
LP_ID_SHIFT = 32
LP_LBA_MASK = 0x7FFFFFFF
MAX_LP_ID = 0x10000


def slba_to_address(nsid, slba):
    """NVMe SLBA -> (lpId, lba).  `lpId` is None for a namespace without LPs.

    For nsid 1 the whole 64-bit value is the LBA (the firmware keeps two extra
    high bits from DW11, `efc_cpu0.c:56382`).  For nsid 2 and 3 the top dword is
    the logical partition and the bottom is masked to 31 bits.
    """
    if slba < 0:
        raise ValueError("negative SLBA %d" % slba)
    if nsid not in LOGICAL_PARTITIONS:
        return None, slba
    lp = slba >> LP_ID_SHIFT
    if lp >= MAX_LP_ID:
        raise ValueError("nsid %d: SLBA %#x has lpId %d, and the firmware "
                         "rejects field_C >= 0x10000 (efc_cpu0.c:56411)"
                         % (nsid, slba, lp))
    return lp, slba & LP_LBA_MASK


def address_to_slba(nsid, lba, lp=None):
    """Inverse of `slba_to_address`."""
    if nsid not in LOGICAL_PARTITIONS:
        if lp:
            raise ValueError("nsid %s has no logical partitions" % nsid)
        return lba
    if lp is None:
        raise ValueError("nsid %d needs an lpId: its host address is "
                         "(lpId, LBA)" % nsid)
    if not 0 <= lp < MAX_LP_ID:
        raise ValueError("lpId %d is outside the 16 bits the wire carries" % lp)
    if lba & ~LP_LBA_MASK:
        raise ValueError("nsid %d: LBA %d does not fit the 31 bits the "
                         "firmware masks it to (efc_cpu0.c:58193)" % (nsid, lba))
    return (lp << LP_ID_SHIFT) | lba


#: One `LptEntry`.  `sub_403403A0` (`efc_cpu2.c:111374`) is `pLpt + 8 +
#: 24*lpIndex`, and the SegmentedBuffer's `elementSize` is 0x18
#: (`efc_cpu2.c:115650`).
LPT_ENTRY = 24
LPT_ARRAY_OFF = 8
#: `attr = *(u8 *)entry & 3` (dozens of sites, e.g. `efc_cpu2.c:113020`,
#: `:114609`, `:118388`) and `l2sIndex = *(u16 *)entry >> 2` (`:24980`,
#: `:25306`, `:114612`).  The two share the first u16.
LPT_ATTR_MASK = 3
LPT_L2S_SHIFT = 2
LPT_ATTR_EMPTY = 0
#: Only 1 and 2 are readable (`efc_cpu2.c:24955`); 2 is `cLptActivatedReadWrite`
#: (`Lpt.c:5267`, `efc_cpu2.c:118388`).  The name of 3 is not established -- the
#: firmware's own `off_40400BB0` name table is not in any image we hold -- and
#: every live partition on this board carries 3, so it is a persisted state that
#: a read would reject until the host re-activates the partition.
LPT_ATTR_READABLE = (1, 2)
LPT_ATTR_NAMES = {0: "cLptEmpty", 1: "activated(1)",
                  2: "cLptActivatedReadWrite", 3: "<unnamed, the persisted state"
                                                  " of every live LP here>"}
#: `+16 u32` is an LMA and 0xFFFFFFFF is INVALID_LMA there (`efc_cpu2.c:119130`
#: compares it against `wmasPerWeru` and then stores -1).
LPT_INVALID_LMA = 0xFFFFFFFF

#: (name, firstObjectId, headerBufferSize, elementSize, maxSegments) --
#: `efc_cpu2.c:115642-115651` and `:115674-115683`.  `maxSegments` is the bound
#: the firmware panics above (`Lpt.c:2977` / `:2994`).
LPT_BUFFERS = {2: ("LptNsid2", 44, 6, LPT_ENTRY, 30),
               3: ("LptNsid3", 74, 6, LPT_ENTRY, 8)}
#: `mem_clr(pLpt, 0x640A0 / 0x65C0)` (`efc_cpu2.c:111273`, `:111321`).
LPT_BUFFER_BYTES = {2: 0x640A0, 3: 0x65C0}
#: `sub_A872` (`efc_cpu2.c:25530-25538`).
L2S_OFF = {2: 393234, 3: 24588}
#: `sub_40347A12` (`efc_cpu2.c:119192-119196`) names both extents.
L2S_BYTES = {2: 16520, 3: 1460}
#: the slice is `u16[0] count<<4|flags`, `u16[1] writeIdx`, `u16[2] granules`,
#: then the superblock array (`sub_40349BBC`, `efc_cpu2.c:25246`, is
#: `*(u16 *)(slice + 2*i + 6) & 0x3FFF`).
L2S_HEADER_U16 = 3
#: One u16 follows the `numOfSuperBlocks` entries and is not one of them:
#: `sub_4034927C` writes `v10[a4 + 3] = word_800000` after initialising exactly
#: `a4` entries, and advances the free cursor by `a4 + 4`
#: (`efc_cpu2.c:120690-120697`).  It is the partition's open-in-SLC-cache
#: marker -- `L2s_UpdateNsid2WriteDeActivatePartitionInfo` (`:121954-122014`)
#: reads `l2s[l2sIndex + count + 3]`, migrates the cached superblock into the
#: count-bounded array and resets the slot.  Counting it as a superblock is
#: exactly what produced the retracted 24 and 66.
L2S_TRAILER_U16 = 1
#: one slice occupies `L2S_SLICE_OVERHEAD_U16 + numOfSuperBlocks`.
L2S_SLICE_OVERHEAD_U16 = L2S_HEADER_U16 + L2S_TRAILER_U16
L2S_WERU_MASK = 0x3FFF
L2S_IN_SLC_CACHE = 0x4000
L2S_BLANK = 0x8000
L2S_COUNT_SHIFT = 4
#: `cNumOfNs2RwLptEntries` -- `entry & 0x3FFF` must be < 16 once the cache bit
#: is set (`Lpt.c:1400`, `efc_cpu2.c:25056`).
MAX_SLC_CACHE_SLOTS = 16
#: The two u16 accumulators the L2S allocator keeps immediately below the
#: array: `dword_40424DEC` gets `+= numOfSuperBlocks` and `dword_40424DF0`
#: gets `+= numOfSuperBlocks + 4` on every create (`sub_4034927C`,
#: `efc_cpu2.c:120696-120699`; the bases are set by `sub_4034AC88`).  So the
#: first is the namespace's TOTAL superblock count and the second is the next
#: free l2sIndex -- two independent checks on any count we derive.
L2S_TOTAL_OFF = {2: 393230, 3: 24584}
L2S_NEXT_OFF = {2: 393232, 3: 24586}

#: Which `WERU_CONFIGURATIONS` row sizes a namespace's superblock:
#: `v16 = nsIdx ? 3 : 4` (`efc_cpu2.c:24966-24969`), i.e. NSID2 uses row 4 (the
#: native-TLC main store) and NSID3 row 3 (the pseudo-SLC cache).
NS_WERU_CONFIG = {2: 4, 3: 3}
#: `dword_907154 = pagesPerBlockTlc/3 * ausPerPage` and `dword_907158 =
#: pagesPerBlockTlc * ausPerPage` (`efc_cpu2.c:107673-107674`, bodies at
#: `sub_4030C0D2` / `sub_4030C0E6`): 1472 and 4416.
WMAS_PER_BLOCK_SLC = geom.PAGES_PER_BLOCK_SLC * WMAS_PER_PAGE       # 1472
WMAS_PER_BLOCK_TLC = geom.PAGES_PER_BLOCK_TLC * WMAS_PER_PAGE       # 4416


def wmas_per_weru(nsid):
    """-> the LMA span of one superblock of this namespace.

    `v19 = blocksPerWeru * (cellMode == 1 ? 1472 : 4416)`
    (`efc_cpu2.c:24970-24977`), read out of the namespace's own WERU
    configuration row rather than hard-coded, so a drive with a different
    `blocksPerWeru` decodes correctly.  211968 for NSID2, 70656 for NSID3.
    """
    if nsid not in NS_WERU_CONFIG:
        raise ValueError("nsid %r has no logical partitions and so no "
                         "superblock size" % (nsid,))
    row = WERU_CONFIGURATIONS[NS_WERU_CONFIG[nsid]]
    blocks_per_weru, cell_mode = row[3], row[5]
    if cell_mode not in (0, 1):
        raise ValueError("WERU configuration %d has cellMode %d, which is not "
                         "interpreted" % (NS_WERU_CONFIG[nsid], cell_mode))
    return blocks_per_weru * (WMAS_PER_BLOCK_SLC if cell_mode == 1
                              else WMAS_PER_BLOCK_TLC)


class LogicalPartition(object):
    """One 24-byte `LptEntry`, as persisted in RootStorage.

    `+2` is `numOfSuperBlocks` itself, a count -- `sub_40344016` writes it
    as `HIWORD(result[0]) = numOfSuperBlocks` (`efc_cpu2.c:115567-115580`; the
    operand is named by the panic at `Lpt.c:2842`), the expand path
    accumulates into it, `*(u16 *)(entry + 2) += numOfSuperBlocks`
    (`efc_cpu2.c:117076-117077`, under the same panic at `Lpt.c:4285`) -- which
    a last-INDEX field could not be -- and the L2S slice's own count field
    `u16[0] >> 4` reads back the same value on 27 of 27 live partitions across
    both namespaces.

    RETRACTED: "+2 is the index of the last superblock, so the count is
    +2 + 1."  That reading came from the packing stride alone, with the slice
    modelled as `3 + count` u16.  The slice is `count + 4` -- there is a fourth
    fixed u16 after the entries (`L2S_TRAILER_U16`) -- so the stride is
    identical arithmetic for a count one smaller, and the stride can never tell
    the two apart.  It is what produced the retracted totals 24 and 66; the
    real ones are 12 and 55.

        nsid 2, LPs 1..12    +2 = 1     l2sIndex 0, 5, 10 ... 55   stride 5
        nsid 3, LPs 132..141 +2 = 2     l2sIndex 39, 45 ... 93     stride 6
        nsid 3, LP 0         +2 = 35    l2sIndex 0 -> 39           stride 39

    `Lpt.check_slice_packing` re-derives every stride, and `L2sArray` reads the
    count out of the array instead of inferring it.

    `counter` (+4), `field_6`, `field_8` and `field_12` stay unnamed.  `+4` is
    incremented per released superblock for NSID2 and written outright for NSID3
    (`efc_cpu2.c:119124`, `:119143`); `+8` runs 2290..2340 here, a sequence of
    some kind.  `lma` (+16) is an LMA with 0xFFFFFFFF for invalid (`:119130`).
    """

    __slots__ = ("nsid", "lp_id", "raw", "attr", "l2s_index",
                 "num_superblocks", "counter", "field_6", "field_8",
                 "field_12", "lma", "flags")

    def __init__(self, nsid, lp_id, raw):
        self.nsid, self.lp_id, self.raw = nsid, lp_id, bytes(raw)
        w0 = struct.unpack_from("<H", self.raw, 0)[0]
        self.attr = w0 & LPT_ATTR_MASK
        self.l2s_index = w0 >> LPT_L2S_SHIFT
        #: `+2` = `numOfSuperBlocks`.  A dead entry keeps its residue, so read
        #: it only through `superblock_count`, which gates on `live`.
        (self.num_superblocks, self.counter, self.field_6) = \
            struct.unpack_from("<HHH", self.raw, 2)
        (self.field_8, self.field_12, self.lma, self.flags) = \
            struct.unpack_from("<IIII", self.raw, 8)

    @property
    def superblock_count(self):
        """How many superblocks this partition owns.  None when empty."""
        return None if not self.live else self.num_superblocks

    @property
    def slice_u16(self):
        """How many u16 this partition's L2S slice occupies.  None when empty.

        `L2S_HEADER_U16 + count + L2S_TRAILER_U16` -- the quantity the packing
        stride measures, and the one that must never be confused with the
        count itself.
        """
        n = self.superblock_count
        return None if n is None else L2S_SLICE_OVERHEAD_U16 + n

    @property
    def size_lmas(self):
        """Upper bound on the partition's addressable LMAs.

        The exact figure is `l2s[+2]*wmasPerWeru + 16*l2s[+4]`
        (`efc_cpu2.c:24999`, `:114625`), which needs the L2S slice header --
        available through `L2sArray` when the media copy of the buffer is held;
        this is `superblock_count * wmasPerWeru`, i.e. the same thing rounded
        up to whole superblocks.
        """
        n = self.superblock_count
        return None if n is None else n * wmas_per_weru(self.nsid)

    @property
    def live(self):
        return self.attr != LPT_ATTR_EMPTY

    @property
    def readable(self):
        """Would the firmware serve a read from this partition right now?

        `sub_9DCE` accepts `attr` 1 and 2 only (`efc_cpu2.c:24955`).  False here
        is a statement about the drive's state, not about our decoding: the
        partitions exist and their data is on media, they are simply not in an
        activated state in the persisted table.
        """
        return self.attr in LPT_ATTR_READABLE

    def __repr__(self):
        return ("<LP nsid %s id %d attr %d (%s) l2sIndex %d>"
                % (self.nsid, self.lp_id, self.attr,
                   LPT_ATTR_NAMES.get(self.attr, "?"), self.l2s_index))


class Lpt(object):
    """The persisted Logical Partition Table of one namespace.

    Built from the RootStorage SegmentedBuffer, which covers the LptEntry array
    and nothing else -- `entry(i)` is UNKNOWN for a partition whose segment this
    backing cannot serve, never a zeroed record.
    """

    __slots__ = ("nsid", "buffer", "count", "high_water")

    def __init__(self, nsid, buffer, count):
        self.nsid, self.buffer, self.count = nsid, buffer, count
        #: `*(u16 *)dword_80C2CC`, the highest lpId ever created
        #: (`efc_cpu2.c:115602-115604`); it is the first u16 of the 6-byte
        #: SegmentedBuffer header.
        h = getattr(buffer, "header", None)
        self.high_water = (struct.unpack_from("<H", h, 0)[0]
                           if h and len(h) >= 2 else UNKNOWN)

    @classmethod
    def from_root_storage(cls, rs, nsid):
        """-> Lpt, or None when this store does not carry that buffer."""
        if nsid not in LPT_BUFFERS:
            raise ValueError("nsid %r has no logical partitions "
                             "(efc_cpu2.c:112063, :114800-114813)" % (nsid,))
        name, first, hdr, elem, max_segs = LPT_BUFFERS[nsid]
        b = rs.segmented_buffer(name, first, hdr, elem, max_segments=max_segs)
        if b is None:
            return None
        want = LOGICAL_PARTITIONS[nsid]
        if b.n_elements != want:
            raise ValueError(
                "%s holds %d elements; `SegmentedBuffer_Initialize` sizes it "
                "%d * %d = %d bytes (efc_cpu2.c:115646), so a different count "
                "means the run was mis-terminated"
                % (name, b.n_elements, want, elem, want * elem))
        return cls(nsid, b, want)

    def entry(self, lp_id):
        """-> LogicalPartition | UNKNOWN.  Raises past `cNumOfEntries[nsId]`,
        which is what `Lpt.c:1562` panics on."""
        if not 0 <= lp_id < self.count:
            raise IndexError("nsid %d: lpId %d, but cNumOfEntries[%d] is %d "
                             "(efc_cpu2.c:10450, Lpt.c:1562)"
                             % (self.nsid, lp_id, self.nsid - 2, self.count))
        raw = self.buffer.element(lp_id)
        if raw is UNKNOWN:
            return UNKNOWN
        return LogicalPartition(self.nsid, lp_id, raw)

    def live(self):
        """-> [LogicalPartition] with `attr != cLptEmpty`, over the entries this
        backing can serve.

        It is a lower bound when `unreadable` is non-zero: a SegmentedBuffer
        segment on an unservable page contributes no entries rather than zeroed
        ones.  Read `unreadable` before quoting the count.
        """
        return [e for e in (self.entry(i) for i in range(self.count))
                if e is not UNKNOWN and e.live]

    def check_slice_packing(self):
        """Is `+2` consistent with how the L2S array is packed?

        -> (agree, disagree, [(lp_id, predicted, actual)]).

        The L2S array is packed, so a live partition's slice runs from its own
        `l2sIndex` to the next live partition's, and that gap must be
        `L2S_HEADER_U16 + count + L2S_TRAILER_U16`.  Nothing here consults the
        count twice: the prediction comes from `+2` of the earlier partition
        and the measurement from the later one's `l2sIndex`.

        This test cannot, on its own, tell a count `n` with a 4-u16 overhead
        from a count `n+1` with a 3-u16 one -- both predict the same stride,
        which is exactly how the retracted 24/66 survived a 21/21 pass.  What
        settles it is `L2sArray`, which reads the count field out of the slice.
        """
        live = sorted(self.live(), key=lambda e: e.l2s_index)
        agree, bad = 0, []
        for a, b in zip(live, live[1:]):
            want = a.l2s_index + a.slice_u16
            if want == b.l2s_index:
                agree += 1
            else:
                bad.append((a.lp_id, want, b.l2s_index))
        return agree, len(bad), bad

    @property
    def superblocks(self):
        """Total superblocks over the live partitions this backing can see."""
        return sum(e.superblock_count for e in self.live())

    @property
    def unreadable(self):
        """How many LptEntries fall in a segment this backing cannot serve."""
        n = 0
        for s in self.buffer.missing_segments:
            lo = s * self.buffer.segment_size // LPT_ENTRY
            hi = min(self.count,
                     ((s + 1) * self.buffer.segment_size + LPT_ENTRY - 1)
                     // LPT_ENTRY)
            n += max(0, hi - lo)
        return n

    def __repr__(self):
        return ("<Lpt nsid %d %d entries, highest live lpId %s>"
                % (self.nsid, self.count, self.high_water))


class L2sSlice(object):
    """One partition's slice of the L2S array, read verbatim off the media."""

    __slots__ = ("nsid", "lp_id", "index", "flags", "count", "full",
                 "granules", "entries", "trailer")

    def __init__(self, nsid, lp_id, index, flags, count, full, granules,
                 entries, trailer):
        self.nsid, self.lp_id, self.index = nsid, lp_id, index
        #: the low 4 bits of `u16[0]`; `sub_4034927C` seeds them 0 or 1
        self.flags = flags
        #: `u16[0] >> 4` -- `numOfSuperBlocks`, the count `sub_4034AC88`
        #: returns and `sub_9DCE` bounds `sbIdx` by
        self.count = count
        #: `u16[1]` superblocks fully written, `u16[2]` granules into the next
        self.full, self.granules = full, granules
        self.entries = tuple(entries)
        #: the u16 after the entries -- the open-in-SLC-cache marker, never a
        #: superblock.  See `L2S_TRAILER_U16`.
        self.trailer = trailer

    @property
    def u16(self):
        return L2S_SLICE_OVERHEAD_U16 + self.count

    def weru(self, sb_idx):
        """-> weruId | None (blank) | UNKNOWN (open in an SLC write cache).

        UNKNOWN is a real answer about the drive, not about the dump: the live
        WERU of a cached superblock is in DRAM only (`efc_cpu2.c:25053-25065`).
        """
        if not 0 <= sb_idx < self.count:
            raise IndexError("nsid %d lp %d owns %d superblocks; sbIdx %d is "
                             "what sub_9DCE refuses at efc_cpu2.c:24982"
                             % (self.nsid, self.lp_id, self.count, sb_idx))
        w = self.entries[sb_idx]
        if w & L2S_BLANK:
            return None
        if w & L2S_IN_SLC_CACHE:
            return UNKNOWN
        return w & L2S_WERU_MASK

    def size_lmas(self):
        """The exact addressable size, `u16[1]*wmasPerWeru + 16*u16[2]`
        (`efc_cpu2.c:24999`) -- the bound `sub_9DCE` applies, not a rounding
        up to whole superblocks."""
        return self.full * wmas_per_weru(self.nsid) + 16 * self.granules

    def __repr__(self):
        return ("<L2sSlice nsid %d lp %d at %d: %d superblock(s), %d full + %d "
                "granules>" % (self.nsid, self.lp_id, self.index, self.count,
                               self.full, self.granules))


class L2sArray(object):
    """The L2S superblock array, read off media -- not inferred, not rebuilt.

    This retracts "the L2S array is not persisted".  Only the LptEntry
    array is in the RootStorage SegmentedBuffer, but the whole LPT+L2S buffer
    is an allocation of the LUT allocator, so all 409760 / 26048 bytes of it
    live in the lutId 2 / lutId 3 region of msoId 40's LMA space and read back
    complete (`NamespaceTable.lpt_buffer`).

    That copy is a snapshot of DRAM at the last flush of that region, so it can
    be a different generation from the SegmentedBuffer -- on this board NSID3's
    is, by four deleted partitions.  `from_buffer` therefore decodes the
    LptEntry array out of the same blob, never mixing the two; use
    `check_against` to compare.
    """

    __slots__ = ("nsid", "slices", "total_superblocks", "next_index",
                 "high_water", "live_counter", "raw")

    def __init__(self, nsid, slices, total_superblocks, next_index,
                 high_water, live_counter, raw=None):
        self.nsid = nsid
        #: lpId -> L2sSlice, for the live partitions of this generation
        self.slices = slices
        #: `pLpt+393230` / `+24584`: the allocator's running sum of
        #: `numOfSuperBlocks`.  An independent witness to the total.
        self.total_superblocks = total_superblocks
        #: `pLpt+393232` / `+24586`: the next free l2sIndex, `+= count + 4`.
        self.next_index = next_index
        #: buffer `+0` highest live lpId, `+2` live-partition counter
        self.high_water, self.live_counter = high_water, live_counter
        self.raw = raw

    @classmethod
    def from_buffer(cls, nsid, blob):
        """-> L2sArray.  `blob` is the whole LPT+L2S buffer for `nsid`."""
        if nsid not in L2S_OFF:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        want = LPT_BUFFER_BYTES[nsid]
        if len(blob) < want:
            raise ValueError("nsid %d: the LPT+L2S buffer is %d bytes "
                             "(mem_clr at efc_cpu2.c:111273), got %d"
                             % (nsid, want, len(blob)))
        hi, live_counter = struct.unpack_from("<HH", blob, 0)
        total = struct.unpack_from("<H", blob, L2S_TOTAL_OFF[nsid])[0]
        nxt = struct.unpack_from("<H", blob, L2S_NEXT_OFF[nsid])[0]
        n_u16 = L2S_BYTES[nsid] // 2
        l2s = struct.unpack_from("<%dH" % n_u16, blob, L2S_OFF[nsid])
        slices = {}
        for lp in range(LOGICAL_PARTITIONS[nsid]):
            e = LogicalPartition(
                nsid, lp, blob[LPT_ARRAY_OFF + LPT_ENTRY * lp:
                               LPT_ARRAY_OFF + LPT_ENTRY * (lp + 1)])
            if not e.live:
                continue
            i = e.l2s_index
            hdr = l2s[i]
            count = hdr >> L2S_COUNT_SHIFT
            end = i + L2S_HEADER_U16 + count + L2S_TRAILER_U16
            if end > n_u16:
                raise ValueError(
                    "nsid %d lp %d: a slice of %d superblocks at l2sIndex %d "
                    "runs past the %d-u16 array (sub_40347A12 states it as %d "
                    "bytes)" % (nsid, lp, count, i, n_u16, L2S_BYTES[nsid]))
            slices[lp] = L2sSlice(
                nsid, lp, i, hdr & ((1 << L2S_COUNT_SHIFT) - 1), count,
                l2s[i + 1], l2s[i + 2],
                l2s[i + L2S_HEADER_U16:i + L2S_HEADER_U16 + count],
                l2s[i + L2S_HEADER_U16 + count])
        return cls(nsid, slices, total, nxt, hi, live_counter, raw=bytes(blob))

    @property
    def superblocks(self):
        """The total this generation declares, summed from the slices."""
        return sum(s.count for s in self.slices.values())

    def check_packing(self):
        """Do the slices tile the array exactly?

        -> (packed, cursor, [(lp_id, expected_index, actual_index)]).

        `packed` means every slice starts where the previous one ended at
        stride `count + 4`, with no gap and no overlap, and `cursor` is where
        the last one ends -- which must equal `next_index`.  This is the test
        the stride law could not be: it consults the array's own count field,
        so a count that is one too large no longer reproduces the packing.
        """
        cursor, bad = 0, []
        for lp in sorted(self.slices, key=lambda k: self.slices[k].index):
            s = self.slices[lp]
            if s.index != cursor:
                bad.append((lp, cursor, s.index))
            cursor = s.index + s.u16
        return (not bad and cursor == self.next_index), cursor, bad

    def check_against(self, lpt):
        """Compare with a RootStorage `Lpt`. -> (agree, differ, unservable)."""
        agree = differ = miss = 0
        for lp in range(lpt.count):
            e = lpt.entry(lp)
            if e is UNKNOWN:
                miss += 1
                continue
            here = self.slices.get(lp)
            if (here is not None) == e.live and (
                    here is None or (here.count == e.superblock_count
                                     and here.index == e.l2s_index)):
                agree += 1
            else:
                differ += 1
        return agree, differ, miss

    def __repr__(self):
        return ("<L2sArray nsid %d: %d live partitions, %d superblocks "
                "(accumulator %d), next l2sIndex %d>"
                % (self.nsid, len(self.slices), self.superblocks,
                   self.total_superblocks, self.next_index))


class L2sMap(object):
    """(nsid, lpId, superblock index) -> weruId, recovered from the media AUX.

    An independent route to what `L2sArray` reads directly, and the two agree
    12/12 on NSID2 -- same partition, same weruId, no exceptions.  Keep both:
    this one needs no LUT region and works for a generation the buffer snapshot
    predates.

    The array the firmware uses lives at `pLpt + 393234` / `+ 24588` in DRAM and
    is not in the persisted SegmentedBuffer, so it cannot be read back from
    there.  It can be re-derived, and the firmware itself does exactly that:
    the aux of
    every AU of a group-2/3 MSO carries `u32 lma` then `u16 lpId`
    (`efc_cpu2.c:43052-43069`), and `RebuildNeo` reads it as
    `pAuxiliaryBuffer[i].lpId` (`:137377`).

    `.entries` is the map; `.checked` / `.mismatched` count the AUs that did and
    did not satisfy ``lma mod wmasPerWeru == wma mod 211968``, which is the same
    identity `sub_9DCE` inverts, so a non-zero `mismatched` refutes the model.
    """

    __slots__ = ("entries", "checked", "mismatched", "werus", "aus",
                 "max_sb_seen", "notes", "samples")

    def __init__(self, entries, checked, mismatched, werus, aus, max_sb_seen,
                 notes=(), samples=()):
        self.entries = entries
        self.checked, self.mismatched = checked, mismatched
        self.werus, self.aus = werus, aus
        #: (nsid, lpId, lma) triples actually observed in an aux, for a
        #: forward end-to-end test that does not re-use the map's own keys.
        self.samples = tuple(samples)
        #: the largest superblock index any AU named.  0 here means the
        #: `lma // wmasPerWeru` step is unexercised on this board.
        self.max_sb_seen = max_sb_seen
        self.notes = tuple(notes)

    @classmethod
    def from_media(cls, ftl, mso_lut_ids=None, max_pages=None, skip_ces=None):
        """Walk the backing and rebuild the map.  -> L2sMap | UNKNOWN.

        Only pages this backing already holds are read; nothing is scanned
        blind.  A WERU whose slot law is unmeasured is skipped and named in
        `notes` rather than guessed at.

        `skip_ces` defaults to `geom.CE_DEAD`.  That is not a convenience: a
        dead CE here still answers, with a page over the alphabet {0x00, 0xA7}
        that is neither programmed nor erased, so it carries no aux to read --
        and it costs ~2.7 s per page in the LDPC decoder before failing.  The
        skip is counted in `notes`, not silent.
        """
        dev = getattr(ftl, "device", None)
        codec = getattr(ftl, "codec", None)
        if dev is None or codec is None:
            return UNKNOWN
        desc = ftl.section("mso")
        if desc in (UNKNOWN, None):
            return UNKNOWN
        desc = desc.contiguous()
        lut_of = {m: desc[m * MSO_DESC_ENTRY + MSO_DESC_LUT_ID_OFF]
                  for m in range(geom.MAX_MSOS)}
        aux_of = {m: desc[m * MSO_DESC_ENTRY + MSO_DESC_AUX_LAYOUT_OFF]
                  for m in range(geom.MAX_MSOS)}
        owners = ftl.weru_owners()
        block_map = ftl.block_map()
        if owners is UNKNOWN or block_map is UNKNOWN:
            return UNKNOWN
        want = {2: 2, 3: 3} if mso_lut_ids is None else mso_lut_ids

        # (ce, block) -> weruId, from the blockMapping's own (block, ce) pairs
        where = {}
        for w, pairs in block_map.items():
            if pairs is UNKNOWN or not pairs:
                continue
            for pr in pairs:
                if pr:
                    where.setdefault((pr[1], pr[0]), w)

        entries, notes, samples = {}, [], []
        checked = mismatched = aus = 0
        werus, unmeasured, slot_of, pages = set(), set(), {}, 0
        dead = set(geom.CE_DEAD if skip_ces is None else skip_ces)
        skipped_dead = wrong_mode = 0
        for key in sorted(getattr(dev, "entries", {}) or {}):
            ce, block, page, slc = key
            w = where.get((ce, block))
            if w is None:
                continue
            if ce in dead:
                skipped_dead += 1
                continue
            owner = owners.get(w, (None,))[0]
            nsid = want.get(lut_of.get(owner))
            if nsid is None:
                continue
            if aux_of.get(owner) != AUX_LAYOUT_NSID23:
                notes.append("msoId %s has lutId %s but auxLayout %s, not %d"
                             % (owner, lut_of.get(owner), aux_of.get(owner),
                                AUX_LAYOUT_NSID23))
                continue
            werus.add(w)
            cfg = ftl.weru_config(w)
            if cfg is UNKNOWN or cfg is None:
                unmeasured.add(w)
                continue
            # Cell mode is a property of the WERU, not of the page we happen to
            # hold.  A native-TLC block read while the die was latched in
            # pseudo-SLC returns plausible garbage (titania.geometry), and the
            # decoder spends seconds failing on it, so only the WERU's own mode
            # is read.
            if cfg.slc is not slc:
                wrong_mode += 1
                continue
            if (ce, block) not in slot_of:
                slot_of[(ce, block)] = None
                for s in range(cfg.blocks_per_weru):
                    if cfg.slot_to_physical(s) == (ce, block):
                        slot_of[(ce, block)] = s
                        break
            slot = slot_of[(ce, block)]
            if slot is None:
                unmeasured.add(w)
                continue
            if max_pages is not None and pages >= max_pages:
                break
            try:
                raw = dev.read_page(ce, block, page, slc=slc)
            except Exception:
                raw = None
            if raw is None:
                continue
            decoded, ok = codec.decode(raw, block, page)
            if decoded is None:
                continue
            pages += 1
            base = (w * geom.AUS_PER_TLC_WERU + page * WMA_STRIDE
                    + slot * WMAS_PER_PAGE)
            span = wmas_per_weru(nsid)
            for au in range(len(decoded)):
                if ok is not UNKNOWN and not ok[au]:
                    continue
                lma, lp = struct.unpack_from("<IH", decoded[au], 0)
                if lma == LPT_INVALID_LMA:
                    continue
                aus += 1
                checked += 1
                if lma % span != (base + au) % geom.AUS_PER_TLC_WERU:
                    mismatched += 1
                    continue
                entries.setdefault((nsid, lp, lma // span), set()).add(w)
                samples.append((nsid, lp, lma))
        if wrong_mode:
            notes.append("skipped %d page(s) this backing holds only in the "
                         "cell mode the WERU does not use" % wrong_mode)
        if skipped_dead:
            notes.append("skipped %d page(s) on the dead CE(s) %s -- they "
                         "answer with a constant {0x00,0xA7} page carrying no "
                         "aux" % (skipped_dead, sorted(dead)))
        if unmeasured:
            notes.append("skipped %d WERU(s) whose slot law is unmeasured: %s"
                         % (len(unmeasured), sorted(unmeasured)))
        collisions = {k: sorted(v) for k, v in entries.items() if len(v) > 1}
        if collisions:
            notes.append("NOT INJECTIVE -- these keys name more than one WERU, "
                         "which the model forbids: %s" % collisions)
        flat = {k: next(iter(v)) for k, v in entries.items() if len(v) == 1}
        return cls(flat, checked, mismatched, sorted(werus), aus,
                   max((k[2] for k in flat), default=UNKNOWN), notes,
                   sorted(set(samples)))

    def weru_for(self, nsid, lp_id, sb_index):
        """-> weruId | UNKNOWN.  UNKNOWN means no AU of that superblock is in
        this backing, which is not the same as the superblock not existing."""
        return self.entries.get((nsid, lp_id, sb_index), UNKNOWN)

    def partitions(self, nsid=None):
        out = {}
        for (ns, lp, sb), w in self.entries.items():
            if nsid is None or ns == nsid:
                out.setdefault((ns, lp), set()).add(sb)
        return {k: sorted(v) for k, v in out.items()}

    def __repr__(self):
        return ("<L2sMap %d superblocks over %d partitions, %d/%d AUs verified,"
                " max sbIdx %s>"
                % (len(self.entries), len(self.partitions()),
                   self.checked - self.mismatched, self.checked,
                   self.max_sb_seen))


# ===========================================================================
# the namespace
# ===========================================================================
class Namespace(object):
    """One NVMe namespace = one mediaSpace = one msoGroupId = one lutId.

    Any field may be UNKNOWN; none is ever silently defaulted.  `nsze` is the
    number of 512-byte host blocks the controller advertises, computed the way
    `sub_77D0` computes it and not from any capacity we measured.
    """

    __slots__ = ("nsid", "group", "lbaf_id", "valid", "raw",
                 "size_mso_id", "size_mso_type", "total_lmas", "granularity_lmas",
                 "lut_id", "mso_ids", "lut_mso_ids", "note")

    def __init__(self, nsid, group, lbaf_id=UNKNOWN, valid=UNKNOWN, raw=None,
                 size_mso_id=UNKNOWN, size_mso_type=UNKNOWN, total_lmas=UNKNOWN,
                 granularity_lmas=UNKNOWN, lut_id=UNKNOWN, mso_ids=UNKNOWN,
                 lut_mso_ids=UNKNOWN, note=None):
        self.nsid, self.group = nsid, group
        self.lbaf_id, self.valid, self.raw = lbaf_id, valid, raw
        self.size_mso_id, self.size_mso_type = size_mso_id, size_mso_type
        self.total_lmas, self.granularity_lmas = total_lmas, granularity_lmas
        self.lut_id = lut_id
        #: the static MSO set, by the MediaSpace.c rule
        self.mso_ids = mso_ids
        #: everything that shares this namespace's lutId on media, which is the
        #: static set plus the dynamic sub-32 msoIds.  A superset, from media.
        self.lut_mso_ids = lut_mso_ids
        self.note = note

    # -- sizes -------------------------------------------------------------
    @property
    def lba_format(self):
        if self.lbaf_id is UNKNOWN or self.lbaf_id >= len(LBAF_TABLE):
            return UNKNOWN
        return LBAF_TABLE[self.lbaf_id]

    @property
    def lba_bytes(self):
        return LBA_BYTES

    @property
    def nsze(self):
        """NSZE in host blocks: `totalLmas * lmuDataSize / hlbuDataSize`.

        UNKNOWN without an `msoDescriptor` section -- the size is on media, not
        in the firmware image.
        """
        if self.total_lmas is UNKNOWN:
            return UNKNOWN
        lmu = self.lmu_bytes
        if lmu is UNKNOWN:
            return UNKNOWN
        return self.total_lmas * lmu // LBA_BYTES

    @property
    def lmu_bytes(self):
        """The lmu size of the size MSO's own lba format (`efc_cpu0.c:55173`).

        This is not hard-coded to 4096: `sub_77D0` reads it from
        `cLBAFTable[msoDescriptor+2]`, and a System-group MSO carries index 0.
        """
        f = self.lba_format
        return UNKNOWN if f is UNKNOWN else f.lmu_data

    @property
    def capacity_bytes(self):
        n = self.nsze
        return UNKNOWN if n is UNKNOWN else n * LBA_BYTES

    @property
    def deallocate_alignment_lbas(self):
        """Dsm range alignment: `0x10000 / hlbuDataSize` (`efc_cpu0.c:57388`)."""
        return 0x10000 // LBA_BYTES

    @property
    def logical_partitions(self):
        """-> the LP count, or None for a namespace the LP layer refuses.

        None is a real negative: `efc_cpu2.c:112063` and `:114800-114813` accept
        only msoGroupId 2 and 3.
        """
        if self.group is UNKNOWN:
            return UNKNOWN
        return LOGICAL_PARTITIONS.get(self.group)

    @property
    def slba_bits(self):
        """How this namespace reads the 64-bit SLBA.  -> a short description.

        The split is per-namespace, not per-controller: `efc_cpu0.c:56374-56411`
        gives nsid 2 and 3 a 31-bit LBA plus a 16-bit lpId in the top dword,
        and leaves nsid 1 with a plain 34-bit LBA.
        """
        if self.logical_partitions in (None, UNKNOWN):
            return "SLBA[33:0] = LBA (no logical partitions)"
        return ("SLBA[47:32] = lpId (< %d), SLBA[30:0] = LBA within that "
                "partition" % self.logical_partitions)

    @property
    def superblock_lmas(self):
        """LMAs per superblock for this namespace, or None when it has no LPs."""
        if self.nsid not in NS_WERU_CONFIG:
            return None
        return wmas_per_weru(self.nsid)

    # -- addressing --------------------------------------------------------
    def lba_to_lma(self, lba):
        """host LBA -> (lma, byte offset).  Raises past NSZE when NSZE is known.

        The NSZE bound is only applied for a namespace without logical
        partitions: `efc_cpu0.c:56395` guards the capacity check with `&& !v16`,
        so on nsid 2 and 3 the firmware bounds the address per partition (the
        `lma >= lpSize` test at `efc_cpu2.c:24999`) and not against NSZE.
        """
        n = self.nsze
        if (n is not UNKNOWN and lba >= n
                and self.logical_partitions in (None, UNKNOWN)):
            raise IndexError("nsid %s: LBA %d is past NSZE %d" % (self.nsid, lba, n))
        return lba_to_lma(lba)

    def lma_to_lba(self, lma):
        if self.total_lmas is not UNKNOWN and lma >= self.total_lmas:
            raise IndexError("nsid %s: LMA %d is past totalLmas %d"
                             % (self.nsid, lma, self.total_lmas))
        return lma_to_lba(lma)

    def __repr__(self):
        return ("<Namespace %s group %s lutId %s %s LBAs of %d B>"
                % (self.nsid, self.group, self.lut_id, self.nsze, LBA_BYTES))


class NamespaceTable(object):
    """The three namespaces, decoded from the `mediaSpace` and `mso` sections.

    Build it with `from_ftl` (needs a mounted `Ftl`) or `from_sections` (needs
    only the two section blobs and an `MsoIdTable`).  Without a `mediaSpace`
    section there is no table and `namespaces()` is UNKNOWN -- there is no
    fallback that invents three namespaces from the msoIdTable, because the
    msoIdTable would happily describe namespaces this drive does not configure.
    """

    def __init__(self, namespaces, ftl=None, notes=()):
        self._ns = namespaces               # {nsid: Namespace} or UNKNOWN
        self.ftl = ftl
        self.notes = tuple(notes)
        self._cache = {}

    # -- construction ------------------------------------------------------
    @classmethod
    def from_sections(cls, media_space, mso_desc, mso_table, ftl=None,
                      lut_members=None):
        """-> NamespaceTable from raw bytes.

        `media_space` and `mso_desc` are the section payloads; `mso_table` is a
        `titania.ftl.MsoIdTable` (or 126 raw bytes).  `lut_members` is an
        optional {lutId: [msoId]} for the media-derived superset.
        """
        notes = []
        if isinstance(mso_table, (bytes, bytearray)):
            mso_table = MsoIdTable(bytes(mso_table), None)
        if media_space is UNKNOWN or media_space is None:
            return cls(UNKNOWN, ftl, ["no mediaSpace section"])
        raw = bytes(media_space)
        if len(raw) < MAX_MEDIA_SPACES * MEDIA_SPACE_ENTRY:
            return cls(UNKNOWN, ftl,
                       ["mediaSpace section is %d bytes, need %d"
                        % (len(raw), MAX_MEDIA_SPACES * MEDIA_SPACE_ENTRY)])
        recs = [raw[i * MEDIA_SPACE_ENTRY:(i + 1) * MEDIA_SPACE_ENTRY]
                for i in range(MAX_MEDIA_SPACES)]

        # A structure that "populates all N slots" can be N copies of one
        # record.  Refuse to report three namespaces unless the three records
        # are actually distinct.
        valid = [r for r in recs if struct.unpack_from("<I", r, MS_VALID_OFF)[0]]
        if not valid:
            return cls(UNKNOWN, ftl, ["mediaSpace has no valid record; "
                                      "MediaSpace.c:94 panics on that"])
        groups = [struct.unpack_from("<I", r, MS_GROUP_OFF)[0] for r in valid]
        if len(set(groups)) != len(groups):
            return cls(UNKNOWN, ftl,
                       ["mediaSpace groups %s are not distinct -- this is one "
                        "record copied, not %d namespaces"
                        % (groups, len(groups))])

        desc = bytes(mso_desc) if mso_desc not in (None, UNKNOWN) else None
        out = {}
        for i, r in enumerate(recs):
            nsid = i + 1
            ok = struct.unpack_from("<I", r, MS_VALID_OFF)[0]
            group = struct.unpack_from("<I", r, MS_GROUP_OFF)[0]
            lbaf = struct.unpack_from("<I", r, MS_LBAF_OFF)[0]
            stamp = struct.unpack_from("<I", r, MS_STAMP_OFF)[0]
            if not ok:
                notes.append("mediaSpace %d is not valid" % i)
                continue
            if stamp != group:
                notes.append("mediaSpace %d: MediaSpace.c stamp %d != group %d"
                             % (i, stamp, group))
            ty = SIZE_MSO_TYPE.get(nsid, UNKNOWN)
            size_mso = UNKNOWN
            if ty is not UNKNOWN and mso_table is not None:
                v = mso_table.raw[ty * len(MSO_GROUPS) + group]
                size_mso = UNKNOWN if v == MSOID_INVALID else v
            total = gran = lut_id = desc_lbaf = UNKNOWN
            if desc is not None and size_mso is not UNKNOWN:
                off = size_mso * MSO_DESC_ENTRY
                if off + MSO_DESC_ENTRY <= len(desc):
                    e = desc[off:off + MSO_DESC_ENTRY]
                    total = struct.unpack_from("<I", e, MSO_DESC_TOTAL_LMAS_OFF)[0]
                    gran = e[MSO_DESC_GRANULARITY_OFF]
                    lut_id = e[MSO_DESC_LUT_ID_OFF]
                    desc_lbaf = e[MSO_DESC_LBAF_OFF]
                    if gran not in LUT_GRANULARITIES:
                        notes.append("nsid %d: LUT granularity %d is not one of "
                                     "%s (Lut.c:348)" % (nsid, gran,
                                                         LUT_GRANULARITIES))
                    if lut_id == LUT_ID_NONE:
                        notes.append("nsid %d: size MSO %d has NO LUT"
                                     % (nsid, size_mso))
                        lut_id = UNKNOWN
            # sub_77D0 sizes with the size MSO's own cLBAFTable index, not the
            # mediaSpace's.  They agree on this drive; say so if they ever do not.
            if desc_lbaf is not UNKNOWN and desc_lbaf != lbaf:
                notes.append("nsid %d: msoDescriptor+2 says lbaf %d, mediaSpace "
                             "says %d -- sizing uses the msoDescriptor one "
                             "(efc_cpu0.c:55173)" % (nsid, desc_lbaf, lbaf))
            use_lbaf = desc_lbaf if desc_lbaf is not UNKNOWN else lbaf
            msos = UNKNOWN
            if mso_table is not None:
                msos = cls._static_mso_set(mso_table, group, i)
            members = UNKNOWN
            if lut_members and lut_id is not UNKNOWN:
                members = tuple(sorted(lut_members.get(lut_id, ())))
            out[nsid] = Namespace(nsid, group, lbaf_id=use_lbaf, valid=True,
                                  raw=r, size_mso_id=size_mso, size_mso_type=ty,
                                  total_lmas=total, granularity_lmas=gran,
                                  lut_id=lut_id, mso_ids=msos,
                                  lut_mso_ids=members)
        return cls(out, ftl, notes)

    @staticmethod
    def _static_mso_set(mso_table, group, media_space_id):
        """The MediaSpace.c enumeration (`efc_cpu0.c:72728-72738`)."""
        raw = mso_table.raw
        ng = len(MSO_GROUPS)
        out = set()
        for ty in range(MEDIA_SPACE_MSO_TYPES):
            v = raw[ty * ng + group]
            if v != MSOID_INVALID:
                out.add(v)
        if media_space_id == 0:
            for ty in PARITY_ATTACH_TYPES:
                v = raw[ty * ng + PARITY_GROUP]
                if v != MSOID_INVALID:
                    out.add(v)
        return tuple(sorted(out))

    @classmethod
    def from_ftl(cls, ftl):
        """-> NamespaceTable using a mounted `Ftl` for both sections and the
        msoIdTable, and keeping the Ftl for `read_lba` / `authoritative_mso`."""
        ms = ftl.section("mediaSpace")
        mso = ftl.section("mso")
        table = getattr(ftl, "mso_table", None)
        members = None
        msos = ftl.msos()
        if msos is not UNKNOWN:
            members = {}
            for mid, m in msos.items():
                if m.lut_id is UNKNOWN or m.lut_id == LUT_ID_NONE:
                    continue
                members.setdefault(m.lut_id, []).append(mid)
        return cls.from_sections(
            ms.contiguous() if ms not in (UNKNOWN, None) else UNKNOWN,
            mso.contiguous() if mso not in (UNKNOWN, None) else UNKNOWN,
            table, ftl=ftl, lut_members=members)

    # -- the surface -------------------------------------------------------
    def namespaces(self):
        """-> {nsid: Namespace}, or UNKNOWN when no mediaSpace section was had."""
        return self._ns

    def namespace(self, nsid):
        if self._ns is UNKNOWN:
            return UNKNOWN
        if nsid not in self._ns:
            raise KeyError("nsid %r is not configured on this drive; the "
                           "configured ones are %s" % (nsid, sorted(self._ns)))
        return self._ns[nsid]

    def nn(self):
        """Number of namespaces.  The controller reports 3 unconditionally
        (`efc_cpu0.c:90754`); this returns what the media configures, so a
        mismatch is visible rather than papered over."""
        return UNKNOWN if self._ns is UNKNOWN else len(self._ns)

    def lba_to_lma(self, nsid, lba):
        """(nsid, host LBA) -> (lma, byte offset in the LMA)."""
        ns = self.namespace(nsid)
        if ns is UNKNOWN:
            return lba_to_lma(lba)          # the shift needs no media
        return ns.lba_to_lma(lba)

    def lma_to_lba(self, nsid, lma):
        """(nsid, LMA) -> the first host LBA it holds."""
        ns = self.namespace(nsid)
        return lma_to_lba(lma) if ns is UNKNOWN else ns.lma_to_lba(lma)

    # -- the logical-partition layer ---------------------------------------
    def lpt(self, nsid):
        """-> `Lpt` | None | UNKNOWN for a namespace's persisted LPT.

        `None` means the RootStorage does not carry that buffer; UNKNOWN means
        there is no RootStorage to ask.  Raises for a namespace the LP layer
        refuses (nsid 1).
        """
        if nsid not in LPT_BUFFERS:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        key = ("lpt", nsid)
        if key in self._cache:
            return self._cache[key]
        rs = getattr(self.ftl, "root", None) if self.ftl is not None else None
        out = UNKNOWN
        if rs is not None:
            try:
                out = Lpt.from_root_storage(rs, nsid)
            except (LookupError, ValueError, IndexError) as e:
                self.notes = self.notes + ("nsid %d LPT not read: %s" % (nsid, e),)
                out = UNKNOWN
        self._cache[key] = out
        return out

    def lpt_buffer(self, nsid):
        """-> the whole LPT+L2S buffer off media | UNKNOWN.

        The buffer is a LUT-allocator allocation, so it is the lutId 2 / lutId
        3 region's table area (`sub_40340464`, `efc_cpu2.c:111415-111458`).
        UNKNOWN when any of its table LMAs is not held -- never a hole filled
        with zeros, because a zeroed L2S slice decodes as a valid count of 0.
        """
        if nsid not in LPT_BUFFER_BYTES:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        key = ("lptbuf", nsid)
        if key in self._cache:
            return self._cache[key]
        out = UNKNOWN
        ftl = self.ftl
        ns = self.namespace(nsid)
        if ftl is not None and ns is not UNKNOWN and ns.size_mso_id is not UNKNOWN:
            reg = ftl.lut_region(ns.size_mso_id)
            if reg not in (UNKNOWN, None) and getattr(reg, "data_lma", None) \
                    is not None:
                n = LPT_BUFFER_BYTES[nsid]
                buf = bytearray()
                for i in range((n + LMA_BYTES - 1) // LMA_BYTES):
                    page = ftl.read_lma(reg.store_mso, reg.data_lma + i)
                    if page is UNKNOWN or page is None:
                        buf = None
                        break
                    buf += page[:LMA_BYTES]
                if buf is not None:
                    out = bytes(buf[:n])
        self._cache[key] = out
        return out

    def l2s_array(self, nsid):
        """-> `L2sArray` | UNKNOWN.  The L2S array as persisted, decoded.

        This is the authority on how many superblocks a partition owns; the
        packing stride is not (see `Lpt.check_slice_packing`).
        """
        key = ("l2sarr", nsid)
        if key in self._cache:
            return self._cache[key]
        blob = self.lpt_buffer(nsid)
        out = UNKNOWN if blob is UNKNOWN else L2sArray.from_buffer(nsid, blob)
        self._cache[key] = out
        return out

    def l2s_map(self, **kw):
        """-> `L2sMap` | UNKNOWN.  The superblock map, rebuilt from the aux.

        Cached, because it reads every group-2/3 page the backing holds.
        """
        if "l2s" not in self._cache:
            self._cache["l2s"] = (UNKNOWN if self.ftl is None
                                  else L2sMap.from_media(self.ftl, **kw))
        return self._cache["l2s"]

    def lp_lma_to_wma(self, nsid, lp, lma):
        """(nsid, lpId, LP-local LMA) -> wma | None | UNKNOWN.

        This is `sub_9DCE` (`efc_cpu2.c:24873-25080`) with the persisted LPT and
        a media-rebuilt L2S standing in for the DRAM one::

            sbIdx  = lma // wmas_per_weru(nsid)
            weruId = l2s[nsid, lp, sbIdx]
            wma    = 211968*weruId + (lma % wmas_per_weru(nsid))

        `None` is a real negative -- the partition is `cLptEmpty`, so nothing
        backs the address.  UNKNOWN means the superblock is not in the rebuilt
        map (no AU of it is in this backing), which is not the same thing.
        """
        if nsid not in LOGICAL_PARTITIONS:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        if lma < 0:
            raise ValueError("negative LMA %d" % lma)
        lpt = self.lpt(nsid)
        if lpt in (UNKNOWN, None):
            return UNKNOWN
        e = lpt.entry(lp)
        if e is UNKNOWN:
            return UNKNOWN
        if not e.live:
            return None
        span = wmas_per_weru(nsid)
        sb, off = divmod(lma, span)
        m = self.l2s_map()
        if m is UNKNOWN:
            return UNKNOWN
        weru = m.weru_for(nsid, lp, sb)
        if weru is UNKNOWN:
            return UNKNOWN
        return weru * geom.AUS_PER_TLC_WERU + off

    def lp_placement(self, nsid, lp, lma):
        """-> `Placement` | None | UNKNOWN for one LP-local LMA."""
        wma = self.lp_lma_to_wma(nsid, lp, lma)
        if wma is UNKNOWN or wma is None:
            return wma
        return self.ftl.wma_to_physical(wma)

    def _read_lp_lma(self, nsid, lp, lma):
        """-> 4096 bytes | None | UNKNOWN, aux-checked on both fields.

        `Ftl.read_lma` cannot serve this -- it resolves through the LUT, which
        for these namespaces is the LPT buffer.  The check here is stricter than
        the LUT path's: the AU's aux must carry both this LMA and this lpId
        (`efc_cpu2.c:43060`), so a wrong superblock lands on an AU that names a
        different partition and this returns UNKNOWN.
        """
        p = self.lp_placement(nsid, lp, lma)
        if p is UNKNOWN or p is None:
            return p
        dev = getattr(self.ftl, "device", None)
        codec = getattr(self.ftl, "codec", None)
        if dev is None or codec is None or p.slc not in (True, False):
            return UNKNOWN
        try:
            raw = dev.read_page(p.ce, p.block, p.page, slc=p.slc)
        except Exception:
            raw = None
        if raw is None:
            return UNKNOWN
        aus, ok = codec.decode(raw, p.block, p.page)
        if aus is None or p.au >= len(aus):
            return UNKNOWN
        if ok is not UNKNOWN and not ok[p.au]:
            return UNKNOWN
        got_lma, got_lp = struct.unpack_from("<IH", aus[p.au], 0)
        if got_lma != lma or got_lp != lp:
            return UNKNOWN
        return aus[p.au][geom.AU_AUX:]

    def partition_coverage(self, nsid):
        """What is actually addressable in this namespace?  -> dict | UNKNOWN.

        This is what replaces `Ftl.address_coverage` for groups 2 and 3, whose
        figures were computed through a table that is not a LUT.
        """
        ns = self.namespace(nsid)
        if ns is UNKNOWN:
            return UNKNOWN
        out = dict(nsid=nsid, total_lmas=ns.total_lmas,
                   partitions=ns.logical_partitions)
        if nsid not in LOGICAL_PARTITIONS:
            cov = self.lut_coverage(nsid)
            out.update(kind="lut", lut_coverage=cov)
            return out
        out["kind"] = "logical partitions"
        out["superblock_lmas"] = wmas_per_weru(nsid)
        lpt = self.lpt(nsid)
        if lpt in (UNKNOWN, None):
            out["live"] = UNKNOWN
        else:
            live = lpt.live()
            out["live"] = [e.lp_id for e in live]
            out["readable"] = [e.lp_id for e in live if e.readable]
            out["unreadable_entries"] = lpt.unreadable
            out["superblocks_declared"] = lpt.superblocks
            out["declared_lmas"] = lpt.superblocks * wmas_per_weru(nsid)
            out["high_water"] = lpt.high_water
        arr = self.l2s_array(nsid)
        if arr is not UNKNOWN:
            #: read out of the persisted L2S array, not inferred from packing
            out["superblocks_persisted"] = arr.superblocks
            out["superblocks_accumulator"] = arr.total_superblocks
            out["persisted_live"] = sorted(arr.slices)
        m = self.l2s_map()
        if m is UNKNOWN:
            out["superblocks_recovered"] = UNKNOWN
        else:
            got = {k: v for k, v in m.entries.items() if k[0] == nsid}
            out["superblocks_recovered"] = len(got)
            out["addressable_lmas"] = len(got) * wmas_per_weru(nsid)
            out["partitions_reached"] = sorted({k[1] for k in got})
        # what the media says is allocated, independent of the LPT
        if self.ftl is not None:
            owners = self.ftl.weru_owners()
            desc = self.ftl.section("mso")
            if owners is not UNKNOWN and desc not in (UNKNOWN, None):
                b = desc.contiguous()
                want = {m_ for m_ in range(geom.MAX_MSOS)
                        if b[m_ * MSO_DESC_ENTRY + MSO_DESC_LUT_ID_OFF]
                        == ns.lut_id}
                owned = {}
                for w, (o, _s) in owners.items():
                    if o in want:
                        owned[o] = owned.get(o, 0) + 1
                out["werus_owned"] = owned
                out["werus_owned_total"] = sum(owned.values())
        return out

    def lut_coverage(self, nsid):
        """Does this namespace's LUT actually cover its LMA space?

        -> (covered_lmas, total_lmas, ok) or UNKNOWN.

        `ok` False for NSID2 and NSID3 is not a shortfall to be repaired: the
        slot does not hold a LUT at all, it holds the LPT + L2S buffer, and its
        "entry count" is `roundup(409760 or 26048, 4096)/4`
        (`sub_40340464`, `efc_cpu2.c:111415-111458`).  Nothing below the LMA may
        be resolved through it; use `lp_lma_to_wma` instead.  Every LUT-path
        resolver here checks this first; a future caller adding another must too.
        """
        ns = self.namespace(nsid)
        if ns is UNKNOWN or self.ftl is None or ns.size_mso_id is UNKNOWN:
            return UNKNOWN
        if ns.total_lmas is UNKNOWN:
            return UNKNOWN
        reg = self.ftl.lut_region(ns.size_mso_id)
        if reg is UNKNOWN or reg is None:
            return UNKNOWN
        covered = reg.n_entries * reg.granularity
        return covered, ns.total_lmas, covered >= ns.total_lmas

    def _resolvable(self, nsid):
        """-> the Namespace when its LUT is a real L2P, else None."""
        cov = self.lut_coverage(nsid)
        if cov is UNKNOWN or not cov[2]:
            return None
        return self.namespace(nsid)

    def lba_placement(self, nsid, lba, lp=None):
        """-> (`Placement` | None | UNKNOWN, byte offset).

        The address the firmware would use for this sector.  `None` is a real
        negative -- nothing backs the address.  UNKNOWN means we cannot compute
        it here.

        For NSID2 and NSID3 `lp` is required: the host address is (lpId, LBA)
        and an LBA on its own is half an address, so this returns UNKNOWN
        without it rather than a plausible number.  `slba_placement` takes the
        packed 64-bit SLBA instead.
        """
        if nsid in LOGICAL_PARTITIONS:
            if lp is None:
                return UNKNOWN, UNKNOWN
            ns = self.namespace(nsid)
            lma, off = (lba_to_lma(lba) if ns is UNKNOWN else ns.lba_to_lma(lba))
            return self.lp_placement(nsid, lp, lma), off
        if lp is not None:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        ns = self._resolvable(nsid)
        if ns is None:
            return UNKNOWN, UNKNOWN
        lma, off = ns.lba_to_lma(lba)
        return self.ftl.lma_to_physical(ns.size_mso_id, lma), off

    def slba_placement(self, nsid, slba):
        """-> (`Placement` | None | UNKNOWN, byte offset) from a packed SLBA."""
        lp, lba = slba_to_address(nsid, slba)
        return self.lba_placement(nsid, lba, lp=lp)

    def read_lba(self, nsid, lba, count=1, lp=None):
        """-> `count` * 512 bytes of host data, or None, or UNKNOWN.

        * bytes    -- read and self-checked (the AU's aux must carry this LMA,
                      and for NSID2/3 this lpId too).
        * None     -- the address maps nowhere.  A real negative: never written,
                      trimmed, or -- for NSID2/3 -- a `cLptEmpty` partition.
        * UNKNOWN  -- not resolvable or not readable from this backing.  On this
                      board that is the usual answer for user data and it is
                      correct, not a gap.  For NSID2/3 it is also the answer
                      when `lp` is omitted, because half the address is missing.

        Mixed results across the LMAs of one request collapse to UNKNOWN; use
        `read_lba_map` to see them individually.
        """
        parts = self.read_lba_map(nsid, lba, count, lp=lp)
        if parts is UNKNOWN:
            return UNKNOWN
        out = bytearray()
        saw_none = False
        for _lma, chunk in parts:
            if chunk is UNKNOWN:
                return UNKNOWN
            if chunk is None:
                saw_none = True
                continue
            out += chunk
        if saw_none:
            return None if not out else UNKNOWN
        return bytes(out)

    def read_slba(self, nsid, slba, count=1):
        """`read_lba` addressed the way the NVMe command is."""
        lp, lba = slba_to_address(nsid, slba)
        return self.read_lba(nsid, lba, count, lp=lp)

    def read_lba_map(self, nsid, lba, count=1, lp=None):
        """-> [(lma, bytes | None | UNKNOWN)] per LMA touched, or UNKNOWN."""
        if count < 1:
            raise ValueError("count must be >= 1")
        if nsid in LOGICAL_PARTITIONS:
            if lp is None:
                return UNKNOWN
            out, remaining, cur = [], count, lba
            while remaining:
                lma, off = lba_to_lma(cur)
                take = min(remaining, LBAS_PER_LMA - (cur & (LBAS_PER_LMA - 1)))
                data = self._read_lp_lma(nsid, lp, lma)
                if data is UNKNOWN or data is None:
                    out.append((lma, data))
                else:
                    out.append((lma, bytes(data[off:off + take * LBA_BYTES])))
                remaining -= take
                cur += take
            return out
        if lp is not None:
            raise ValueError("nsid %r has no logical partitions" % (nsid,))
        ns = self._resolvable(nsid)
        if ns is None:
            return UNKNOWN
        n = ns.nsze
        if n is not UNKNOWN and lba + count > n:
            raise IndexError("nsid %d: LBAs [%d,%d) run past NSZE %d"
                             % (nsid, lba, lba + count, n))
        out = []
        remaining, cur = count, lba
        while remaining:
            lma, off = lba_to_lma(cur)
            take = min(remaining, LBAS_PER_LMA - (cur & (LBAS_PER_LMA - 1)))
            data = self.ftl.read_lma(ns.size_mso_id, lma)
            if data is UNKNOWN or data is None:
                out.append((lma, data))
            else:
                out.append((lma, bytes(data[off:off + take * LBA_BYTES])))
            remaining -= take
            cur += take
        return out

    def authoritative_mso(self, nsid, lba=None, lma=None, lp=None):
        """Which MSO physically holds the live copy of this address?

        -> (msoId, source) | None | UNKNOWN.  This is derived, never chosen:
        the namespace's one LUT gives a WMA, the WMA gives a WERU, and the
        WERU's owner gives the msoId.  `None` means the LUT maps the address
        nowhere, so no MSO holds it.

        `source` is which table answered the ownership question -- on a store
        whose dirty flag is set the `WeruUsageLog` outranks the
        `weruDescriptor`, and `Ftl.weru_owners` already applies that rule.

        For NSID2 and NSID3 the WMA comes from the LPT/L2S chain instead and
        `lp` is required; without it this is UNKNOWN.  Reading their lutId slot
        as a LUT produced a confident, wrong answer (msoId 57,
        NeoFwPartition/System, for user data) before `lut_coverage` was added,
        and that reading stays refused.
        """
        if nsid in LOGICAL_PARTITIONS:
            if lp is None:
                return UNKNOWN
            if lma is None:
                if lba is None:
                    raise TypeError("pass lba= or lma=")
                lma = lba_to_lma(lba)[0]
            wma = self.lp_lma_to_wma(nsid, lp, lma)
            if wma is UNKNOWN or wma is None:
                return wma
            owners = self.ftl.weru_owners()
            if owners is UNKNOWN:
                return UNKNOWN
            return owners.get(wma // geom.AUS_PER_TLC_WERU, UNKNOWN)
        ns = self._resolvable(nsid)
        if ns is None:
            return UNKNOWN
        if lma is None:
            if lba is None:
                raise TypeError("pass lba= or lma=")
            lma = ns.lba_to_lma(lba)[0]
        wma = self.ftl.lma_to_wma(ns.size_mso_id, lma)
        if wma is UNKNOWN or wma is None:
            return wma
        owners = self.ftl.weru_owners()
        if owners is UNKNOWN:
            return UNKNOWN
        return owners.get(wma // geom.AUS_PER_TLC_WERU, UNKNOWN)

    def check_lut_identity(self, samples=15, seed=7):
        """Do all the MSOs sharing a lutId return the same WMA for an LMA?

        -> {lutId: (agree, disagree, unknown, [msoId])}, or UNKNOWN.

        This is the operational form of "the MSO is not part of a read address".
        A non-zero `disagree` would refute this module's central claim.

        It says nothing about whether the shared table is an L2P -- for lutId 2
        and 3 it is not, and the MSOs agree on the same non-WMA.  Use
        `lut_coverage` for that, separately.
        """
        if self.ftl is None:
            return UNKNOWN
        import random
        msos = self.ftl.msos()
        if msos is UNKNOWN:
            return UNKNOWN
        by_lut = {}
        for mid, m in sorted(msos.items()):
            if m.lut_id is UNKNOWN or m.lut_id == LUT_ID_NONE:
                continue
            by_lut.setdefault(m.lut_id, []).append(mid)
        rnd = random.Random(seed)
        out = {}
        for lut_id, ids in sorted(by_lut.items()):
            tot = msos[ids[0]].total_lmas
            if tot is UNKNOWN or not tot:
                out[lut_id] = (0, 0, 0, ids)
                continue
            probe = [0, tot - 1] + [rnd.randrange(tot)
                                    for _ in range(max(0, samples - 2))]
            agree = dis = unk = 0
            for lma in probe:
                vals = [self.ftl.lma_to_wma(m, lma) for m in ids]
                if any(v is UNKNOWN for v in vals):
                    unk += 1
                elif len({repr(v) for v in vals}) == 1:
                    agree += 1
                else:
                    dis += 1
            out[lut_id] = (agree, dis, unk, ids)
        return out

    # -- display -----------------------------------------------------------
    def describe(self):
        lines = ["host LBA -> MSO + LMA"]
        add = lines.append
        add("=" * 72)
        add("host block        %d B (NVMe LBADS %s, MS 0)"
            % (LBA_BYTES, HOST_LBA_FORMAT.lbads))
        add("LMA               %d B" % LMA_BYTES)
        add("relation          lma = lba >> %d ; offset = (lba & %d) * %d"
            % (LBA_SHIFT, LBAS_PER_LMA - 1, LBA_BYTES))
        add("live lba format   %r" % (HOST_LBA_FORMAT,))
        if self._ns is UNKNOWN:
            add("namespaces        <unknown> -- no mediaSpace section")
        else:
            add("namespaces        %d configured (controller advertises 3)"
                % len(self._ns))
            for nsid in sorted(self._ns):
                ns = self._ns[nsid]
                name = UNKNOWN
                if ns.size_mso_type is not UNKNOWN and ns.group is not UNKNOWN:
                    name = "%s/%s" % (MSO_TYPES[ns.size_mso_type],
                                      MSO_GROUPS[ns.group])
                cap = ns.capacity_bytes
                add("  nsid %d  group %s  lutId %s  gran %s LMAs"
                    % (nsid, ns.group, ns.lut_id, ns.granularity_lmas))
                add("          size from msoId %s %s, totalLmas %s"
                    % (ns.size_mso_id, name, ns.total_lmas))
                add("          NSZE %s LBAs = %s"
                    % (ns.nsze,
                       "%.2f GB" % (cap / 1e9) if cap is not UNKNOWN else UNKNOWN))
                lp = ns.logical_partitions
                add("          logical partitions %s"
                    % ("none (the LP layer refuses this group)" if lp is None
                       else lp))
                add("          host address      %s" % ns.slba_bits)
                add("          MSOs %s" % (ns.mso_ids,))
                if ns.lut_mso_ids is not UNKNOWN:
                    add("          shares its LUT with %s" % (ns.lut_mso_ids,))
                cov = self.lut_coverage(nsid)
                if cov is UNKNOWN:
                    add("          LUT coverage <unknown>")
                elif cov[2]:
                    add("          LUT is an L2P: covers %d LMAs of %d -- "
                        "LBA -> physical RESOLVED" % (cov[0], cov[1]))
                else:
                    add("          the lutId slot is NOT a LUT: it is the "
                        "%d-byte LPT+L2S buffer" % LPT_BUFFER_BYTES[nsid])
                    add("          (a LUT reading gives %d LMAs of %d, %.1fx "
                        "short; sub_40340464 sizes it "
                        "roundup(%d,4096)/4 = %d entries)"
                        % (cov[0], cov[1], cov[1] / cov[0],
                           LPT_BUFFER_BYTES[nsid],
                           -(-LPT_BUFFER_BYTES[nsid] // LMA_BYTES) * LMA_BYTES // 4))
                if nsid in LOGICAL_PARTITIONS:
                    pc = self.partition_coverage(nsid)
                    if pc is not UNKNOWN:
                        add("          superblock        %d LMAs (WERU config "
                            "%d)" % (pc["superblock_lmas"],
                                     NS_WERU_CONFIG[nsid]))
                        live = pc.get("live")
                        # `partition_coverage` sets `live = UNKNOWN` when this
                        # namespace's LPT is not held, and `Unknown.__len__`
                        # raises by design -- so `len(live)` took the whole of
                        # `titania ns list` down on any dump missing an LPT.
                        # It survives here only because this board's store holds
                        # both.  The same branch covers `superblocks_declared`
                        # and `declared_lmas`, which are absent in that state
                        # and were being printed as "None superblocks" and
                        # "0.0% of totalLmas" -- numbers nothing measured.
                        if live is UNKNOWN or live is None:
                            add("          LPs in use        <unknown> -- this "
                                "namespace's LPT is not held here, so neither "
                                "the live logical partitions nor the declared "
                                "superblock count was measured")
                        else:
                            add("          LPs in use        %d of %d: %s (%d "
                                "entries unservable here)"
                                % (len(live), lp, live[:16],
                                   pc.get("unreadable_entries")))
                            add("          declared          %s superblocks = "
                                "%s LMAs (%.1f%% of totalLmas)"
                                % (pc.get("superblocks_declared"),
                                   pc.get("declared_lmas"),
                                   100.0 * pc.get("declared_lmas", 0)
                                   / (pc["total_lmas"] or 1)))
                        if "superblocks_persisted" in pc:
                            add("          L2S array on media: %s superblocks "
                                "over partitions %s (firmware accumulator %s)"
                                % (pc["superblocks_persisted"],
                                   pc["persisted_live"],
                                   pc["superblocks_accumulator"]))
                        add("          readable now      %s"
                            % (pc.get("readable"),))
                        add("          WERUs owned       %s (total %s)"
                            % (pc.get("werus_owned"),
                               pc.get("werus_owned_total")))
                        add("          superblocks rebuilt from the aux: %s "
                            "-> %s addressable LMAs, partitions %s"
                            % (pc.get("superblocks_recovered"),
                               pc.get("addressable_lmas"),
                               pc.get("partitions_reached")))
        for n in self.notes:
            add("note              %s" % n)
        return "\n".join(lines)

    def __repr__(self):
        return "<NamespaceTable %s>" % ("unknown" if self._ns is UNKNOWN
                                        else sorted(self._ns))


# ===========================================================================
# self-test
# ===========================================================================
def _selftest(argv):                                    # pragma: no cover
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    results = []

    def report(name, state, detail=""):
        results.append((name, state))
        print("%-9s %-34s %s" % ("[%s]" % state, name, detail))

    print("titania.namespace self-test")
    print("=" * 72)

    # ---- 1. the static table, no media needed ---------------------------
    live = host_lba_formats()
    ok = (len(live) == 1 and live[0].lbaf_id == 1 and live[0].hlbu_data == 512
          and live[0].lmu_data == 4096 and LBAF_TABLE[0].disabled == 1)
    report("cLBAFTable", "PASS" if ok else "FAIL",
           "2 rows, %d live: %r (entry 0 is disabled, and its hlbuDataSize 0 "
           "would divide by zero at HostLbaFormat.c:82 anyway)"
           % (len(live), live[0]))

    ok = (LBAS_PER_LMA == 8 and LBA_SHIFT == 3 and LBA_BYTES == 512
          and LBAS_PER_LMA * LBA_BYTES == LMA_BYTES)
    report("LBA -> LMA relation", "PASS" if ok else "FAIL",
           "lma = lba >> %d, %d LBAs of %d B per %d B LMA -- "
           "HostLbaFormat.c:82 asserts the division is exact"
           % (LBA_SHIFT, LBAS_PER_LMA, LBA_BYTES, LMA_BYTES))

    ok = (LBAF_TABLE[1].lbads == 9 and LBAF_TABLE[1].lmu_total
          == LBAF_TABLE[1].lmu_data + LBAS_PER_LMA * LBAF_TABLE[1].mpecc)
    report("corroboration: NVMe fields", "PASS" if ok else "FAIL",
           "LBADS %s (512 B blocks), AWUPF %d (= 1 LMA), DSM alignment %d LBAs "
           "(= 64 KiB), lmuTotalSize %d = %d + %d*%d"
           % (LBAF_TABLE[1].lbads, 0x1000 // LBA_BYTES - 1,
              0x10000 // LBA_BYTES, LBAF_TABLE[1].lmu_total,
              LBAF_TABLE[1].lmu_data, LBAS_PER_LMA, LBAF_TABLE[1].mpecc))

    import random
    rnd = random.Random(11)
    bad = []
    for lba in [0, 1, 7, 8, 9, 4095, 1 << 31, (1 << 40) - 1] + \
               [rnd.randrange(1 << 40) for _ in range(2000)]:
        lma, off = lba_to_lma(lba)
        if lma_to_lba(lma) + off // LBA_BYTES != lba or off >= LMA_BYTES:
            bad.append(lba)
    report("lba_to_lma round trip", "PASS" if not bad else "FAIL",
           "2008 LBAs including 0, the 8-boundary and 2^40-1; %d wrong"
           % len(bad))

    # a lossy shift must be caught, not silently rounded
    try:
        LbaFormat(9, 512, 512, 4096 + 512, 4096, 0, 0, 0, 0, 0).shift
        report("non-exact ratio refused", "FAIL", "accepted a non-multiple")
    except ValueError as e:
        report("non-exact ratio refused", "PASS", str(e)[:88])
    try:
        LbaFormat(9, 512, 512, 512 * 3, 0, 0, 0, 0, 0, 0).shift
        report("non-power-of-two refused", "FAIL", "accepted ratio 3")
    except ValueError as e:
        report("non-power-of-two refused", "PASS", str(e)[:88])

    # ---- 1b. the logical-partition arithmetic, no media needed ----------
    ok = (wmas_per_weru(2) == 211968 and wmas_per_weru(3) == 70656
          and geom.AUS_PER_TLC_WERU == 211968)
    report("superblock size per nsid", "PASS" if ok else "FAIL",
           "NSID2 uses WERU config %d -> %d LMAs, NSID3 config %d -> %d "
           "(efc_cpu2.c:24966-24977); the global WMA stride dword_907150 is "
           "%d either way"
           % (NS_WERU_CONFIG[2], wmas_per_weru(2), NS_WERU_CONFIG[3],
              wmas_per_weru(3), geom.AUS_PER_TLC_WERU))

    # sub_40340464's substituted entry count must reproduce the two counts the
    # media reports for the lutId 2 and 3 regions.  Two instances, one formula.
    got = tuple(-(-LPT_BUFFER_BYTES[n] // LMA_BYTES) * LMA_BYTES // 4
                for n in (2, 3))
    ok = got == (103424, 7168)
    report("lutId 2/3 entry count", "PASS" if ok else "FAIL",
           "roundup(%d,4096)/4 = %d and roundup(%d,4096)/4 = %d, vs the "
           "103424 and 7168 the regions measure -- so the 'short table' is the "
           "LPT buffer, not a truncated L2P (efc_cpu2.c:111424-111436)"
           % (LPT_BUFFER_BYTES[2], got[0], LPT_BUFFER_BYTES[3], got[1]))

    # the LPT+L2S buffer must tile its own allocation exactly
    det, ok = [], True
    for n in (2, 3):
        arr = LPT_ARRAY_OFF + LOGICAL_PARTITIONS[n] * LPT_ENTRY
        ok &= arr <= L2S_OFF[n]
        ok &= L2S_OFF[n] + L2S_BYTES[n] <= LPT_BUFFER_BYTES[n]
        det.append("nsid %d: 8 + %d*24 = %d, L2S at %d + %d = %d, buffer %d"
                   % (n, LOGICAL_PARTITIONS[n], arr, L2S_OFF[n], L2S_BYTES[n],
                      L2S_OFF[n] + L2S_BYTES[n], LPT_BUFFER_BYTES[n]))
    report("LPT buffer layout closes", "PASS" if ok else "FAIL", "; ".join(det))

    # the (lpId, LBA) split, both directions
    bad = []
    for lp, lba in ((0, 0), (1, 7), (12, 1552 * 8), (16383, LP_LBA_MASK),
                    (1023, 12345)):
        for n in (2, 3):
            s = address_to_slba(n, lba, lp)
            if slba_to_address(n, s) != (lp, lba):
                bad.append((n, lp, lba))
    refused = 0
    for probe in (lambda: slba_to_address(2, 0x10000 << LP_ID_SHIFT),
                  lambda: address_to_slba(2, LP_LBA_MASK + 1, 1),
                  lambda: address_to_slba(1, 0, 5)):
        try:
            probe()
        except ValueError:
            refused += 1
    ok = not bad and refused == 3 and slba_to_address(1, 1 << 33) == (None, 1 << 33)
    report("SLBA -> (lpId, LBA)", "PASS" if ok else "FAIL",
           "10 round trips, %d wrong; %d/3 out-of-range forms refused; nsid 1 "
           "keeps the whole 64-bit LBA (efc_cpu0.c:56374-56411)"
           % (len(bad), refused))

    # ---- 2. the media-derived table -------------------------------------
    #: everything below here needs media: the namespace table, the sizes and
    #: every LUT fact are on the drive, not in the firmware image.
    MEDIA_CHECKS = ("mediaSpace decode", "namespace distinct", "namespace sizes",
                    "MSO set per namespace", "LUT identity", "LUT is an L2P",
                    "nsid 2 gated", "nsid 3 gated", "read_lba",
                    "read_lba tail probes", "authoritative_mso",
                    "NSZE bound enforced",
                    "auxLayout tracks lutId", "LPT persisted", "LPT live set",
                    "LptEntry+2 packing", "L2S array read off media",
                    "superblock count settled", "L2S from the aux",
                    "LP end to end", "corrected coverage")
    fw_path = root / "dumps" / "fw.sram.efc.bin"
    raw_dir = config.dump_path("raw")
    if not fw_path.exists() or not raw_dir.exists():
        for n in MEDIA_CHECKS:
            report(n, "SKIPPED", "no dumps/fw.sram.efc.bin or <dump>/raw here")
    else:
        _ftlmod = _sib("ftl")
        _selftest_device, Ftl = _ftlmod._selftest_device, _ftlmod.Ftl
        dev = _selftest_device(root)
        if dev is None:
            for n in MEDIA_CHECKS:
                report(n, "SKIPPED", "no local raw dump device")
        else:
            ftl = Ftl.from_device(dev, firmware=fw_path,
                                  cache_dir=config.dump_path("sysmeta")
                                  / "ftl_cache")
            nt = NamespaceTable.from_ftl(ftl)
            nss = nt.namespaces()
            if nss is UNKNOWN:
                for n in ("mediaSpace decode", "namespace distinct",
                          "namespace sizes", "MSO set per namespace"):
                    report(n, "SKIPPED", "; ".join(nt.notes) or "no mediaSpace")
            else:
                ok = (len(nss) == MAX_MEDIA_SPACES
                      and sorted(nss) == [1, 2, 3]
                      and [nss[i].group for i in (1, 2, 3)] == [1, 2, 3]
                      and all(nss[i].lbaf_id == 1 for i in (1, 2, 3)))
                report("mediaSpace decode", "PASS" if ok else "FAIL",
                       "%d records, groups %s, lbaf %s, valid %s"
                       % (len(nss), [nss[i].group for i in sorted(nss)],
                          [nss[i].lbaf_id for i in sorted(nss)],
                          [nss[i].valid for i in sorted(nss)]))

                sizes = [nss[i].total_lmas for i in sorted(nss)]
                luts = [nss[i].lut_id for i in sorted(nss)]
                ok = (len(set(sizes)) == 3 and len(set(luts)) == 3
                      and UNKNOWN not in sizes)
                report("namespace distinct", "PASS" if ok else "FAIL",
                       "totalLmas %s and lutIds %s are pairwise distinct -- "
                       "three namespaces, not one record copied three times"
                       % (sizes, luts))

                det = []
                ok = True
                for i in sorted(nss):
                    ns = nss[i]
                    ok &= (ns.nsze is not UNKNOWN
                           and ns.nsze == ns.total_lmas * LBAS_PER_LMA
                           and ns.granularity_lmas in LUT_GRANULARITIES)
                    det.append("nsid %d msoId %s %.1f GB (%s LBAs)"
                               % (i, ns.size_mso_id, ns.capacity_bytes / 1e9,
                                  ns.nsze))
                report("namespace sizes", "PASS" if ok else "FAIL",
                       "; ".join(det))

                # the MediaSpace.c enumeration, and its ParityData exception
                ns1 = nss[1]
                par = {34, 35, 62}
                stamped = set()
                mso_sec = ftl.section("mso")
                if mso_sec not in (UNKNOWN, None):
                    b = mso_sec.contiguous()
                    for m in range(geom.MAX_MSOS):
                        e = b[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY]
                        if len(e) == MSO_DESC_ENTRY and e[MSO_DESC_GROUP_OFF] == 1:
                            stamped.add(m)
                ok = (ns1.mso_ids is not UNKNOWN and par <= set(ns1.mso_ids)
                      and par <= stamped
                      and all(par.isdisjoint(nss[i].mso_ids) for i in (2, 3)))
                report("MSO set per namespace", "PASS" if ok else "FAIL",
                       "nsid1 %s (MediaSpace.c walks 17 types and attaches the "
                       "ParityData MSOs of types %s ONLY for mediaSpace 0); the "
                       "media stamps msoDescriptor+4=1 on %s -- the code names "
                       "%s"
                       % (ns1.mso_ids, list(PARITY_ATTACH_TYPES),
                          sorted(stamped), sorted(par)))

            ident = nt.check_lut_identity()
            if ident is UNKNOWN:
                report("LUT identity", "SKIPPED", "no msoDescriptor")
            else:
                a = sum(v[0] for v in ident.values())
                d = sum(v[1] for v in ident.values())
                u = sum(v[2] for v in ident.values())
                report("LUT identity", "PASS" if d == 0 and a else "FAIL",
                       "%d lutIds, %d agree / %d DISAGREE / %d unknown -- every "
                       "MSO sharing a lutId reads the SAME table entry, so the "
                       "MSO is not part of a read address (whether that entry "
                       "is a WMA is a separate question: see 'LUT is an L2P')"
                       % (len(ident), a, d, u))

            if nss is UNKNOWN:
                for n in ("LUT is an L2P", "nsid 2 gated", "nsid 3 gated",
                          "read_lba", "read_lba tail probes",
                          "authoritative_mso", "NSZE bound enforced",
                          "auxLayout tracks lutId", "LPT persisted",
                          "LPT live set", "LptEntry+2 packing",
                          "L2S array read off media",
                          "superblock count settled",
                          "L2S from the aux", "LP end to end",
                          "corrected coverage"):
                    report(n, "SKIPPED", "no namespace table")
            else:
                cov = {i: nt.lut_coverage(i) for i in sorted(nss)}
                if any(c is UNKNOWN for c in cov.values()):
                    report("LUT is an L2P", "SKIPPED",
                           "LUT region chain not walkable for %s"
                           % [i for i, c in cov.items() if c is UNKNOWN])
                else:
                    ok = (cov[1][2] and not cov[2][2] and not cov[3][2])
                    report("LUT is an L2P", "PASS" if ok else "FAIL",
                           "; ".join("nsid %d %s (%d of %d LMAs%s)"
                                     % (i, "YES" if c[2] else "NO", c[0], c[1],
                                        "" if c[2] else
                                        ", %.1fx short" % (c[1] / c[0]))
                                     for i, c in sorted(cov.items()))
                           + " -- nsid 2/3 resolve no further than the LMA")
                for i in (2, 3):
                    bare = (nt.read_lba_map(i, 0) is UNKNOWN
                            and nt.lba_placement(i, 0)[0] is UNKNOWN
                            and nt.authoritative_mso(i, lba=0) is UNKNOWN)
                    refused = 0
                    try:
                        nt.read_lba_map(1, 0, lp=0)
                    except ValueError:
                        refused = 1
                    if bare and refused:
                        report("nsid %d gated" % i, "PASS",
                               "an LBA with no lpId resolves to UNKNOWN, not a "
                               "plausible number -- the host address here is "
                               "(lpId, LBA) and half of it is missing; and "
                               "nsid 1 REFUSES an lpId")
                    else:
                        report("nsid %d gated" % i, "FAIL",
                               "bare-LBA gate %s, nsid-1 lp refusal %s"
                               % (bare, refused))

                got = nt.read_lba(1, 0)
                if isinstance(got, bytes):
                    p, off = nt.lba_placement(1, 0)
                    report("read_lba", "PASS",
                           "nsid 1 LBA 0 -> LMA 0 +%d -> %r, %d bytes read and "
                           "aux-checked" % (off, p, len(got)))
                else:
                    report("read_lba", "SKIPPED",
                           "nsid 1 LBA 0 -> %r; this backing cannot serve it "
                           "(most user WERUs here have no measured slot map)"
                           % (got,))
                # the honest-negative path must not be a crash
                probes = []
                for nsid in sorted(nss):
                    ns = nss[nsid]
                    if ns.nsze is UNKNOWN:
                        continue
                    probes.append((nsid, nt.read_lba(nsid, ns.nsze - 8)))
                report("read_lba tail probes", "PASS",
                       "; ".join("nsid %d last LMA -> %s"
                                 % (n, "bytes" if isinstance(r, bytes) else r)
                                 for n, r in probes)
                       or "no bounded namespace")
                # authoritative_mso, on real mapped addresses
                import random as _r
                rnd2 = _r.Random(3)
                ns1 = nss[1]
                hits = {}
                for _ in range(4000):
                    o = nt.authoritative_mso(
                        1, lma=rnd2.randrange(ns1.total_lmas))
                    if isinstance(o, tuple):
                        hits[o] = hits.get(o, 0) + 1
                if hits:
                    report("authoritative_mso", "PASS",
                           "4000 sampled nsid-1 LMAs: %s -- the owning MSO is "
                           "DERIVED (LMA -> WMA -> WERU -> owner), never chosen"
                           % {"%s/%s" % (k[0], k[1]): v
                              for k, v in sorted(hits.items())})
                else:
                    report("authoritative_mso", "SKIPPED",
                           "no sampled nsid-1 LMA is mapped in this backing")
                try:
                    nt.read_lba(1, nss[1].nsze)
                    report("NSZE bound enforced", "FAIL", "accepted LBA == NSZE")
                except IndexError as e:
                    report("NSZE bound enforced", "PASS", str(e)[:88])

                # ---- 3. the logical-partition layer ---------------------
                sec = ftl.section("mso")
                if sec in (UNKNOWN, None):
                    report("auxLayout tracks lutId", "SKIPPED", "no mso section")
                else:
                    b = sec.contiguous()
                    by_lut = {}
                    for m in range(geom.MAX_MSOS):
                        e = b[m * MSO_DESC_ENTRY:(m + 1) * MSO_DESC_ENTRY]
                        by_lut.setdefault(e[MSO_DESC_LUT_ID_OFF], set()).add(
                            e[MSO_DESC_AUX_LAYOUT_OFF])
                    n23 = {m for m in range(geom.MAX_MSOS)
                           if b[m * MSO_DESC_ENTRY + MSO_DESC_AUX_LAYOUT_OFF]
                           == AUX_LAYOUT_NSID23}
                    lut23 = {m for m in range(geom.MAX_MSOS)
                             if b[m * MSO_DESC_ENTRY + MSO_DESC_LUT_ID_OFF]
                             in (2, 3)}
                    ok = (n23 == lut23 and by_lut.get(2) == {AUX_LAYOUT_NSID23}
                          and by_lut.get(3) == {AUX_LAYOUT_NSID23}
                          and AUX_LAYOUT_NSID23 not in by_lut.get(1, set()))
                    report("auxLayout tracks lutId", "PASS" if ok else "FAIL",
                           "msoDescriptor+17 by lutId: %s -- the %d MSOs using "
                           "cAuxLayoutForForNSID23 are EXACTLY the lutId 2/3 "
                           "set, found two independent ways"
                           % ({k: sorted(v) for k, v in sorted(by_lut.items())},
                              len(n23)))

                lpts = {}
                det, ok = [], True
                for i in (2, 3):
                    t = nt.lpt(i)
                    lpts[i] = t
                    if t in (UNKNOWN, None):
                        ok = False
                        det.append("nsid %d: %r" % (i, t))
                        continue
                    name, first, hdr, elem, _mx = LPT_BUFFERS[i]
                    det.append("nsid %d %s objects %d..%d, %d x %d B = %d, "
                               "header %s (highest live lpId %s)"
                               % (i, name, t.buffer.first_object_id,
                                  t.buffer.first_object_id + t.buffer.segments - 1,
                                  t.count, elem, t.count * elem,
                                  t.buffer.header.hex(" ") if t.buffer.header
                                  else None, t.high_water))
                    ok &= (t.count == LOGICAL_PARTITIONS[i]
                           and t.buffer.first_object_id == first)
                report("LPT persisted", "PASS" if ok else "FAIL", "; ".join(det))

                if any(t in (UNKNOWN, None) for t in lpts.values()):
                    report("LPT live set", "SKIPPED", "no LPT")
                    report("LptEntry+2 packing", "SKIPPED", "no LPT")
                else:
                    live2, live3 = lpts[2].live(), lpts[3].live()
                    ids2 = [e.lp_id for e in live2]
                    ok = (ids2 == list(range(1, len(ids2) + 1))
                          and lpts[2].high_water == max(ids2)
                          and lpts[3].high_water
                          == max(e.lp_id for e in live3)
                          and {e.attr for e in live2 + live3} == {3})
                    report("LPT live set", "PASS" if ok else "FAIL",
                           "nsid 2: %d live %s (%d entries unservable), %d "
                           "superblocks; nsid 3: %d live %s (%d unservable), "
                           "%d superblocks (%s each); the 6-byte buffer header "
                           "gives the highest live lpId as %s/%s, matching; "
                           "attr on every live LP %s -- readable() is False for "
                           "all of them (sub_9DCE wants 1 or 2)"
                           % (len(ids2), ids2[:14], lpts[2].unreadable,
                              lpts[2].superblocks, len(live3),
                              [e.lp_id for e in live3], lpts[3].unreadable,
                              lpts[3].superblocks,
                              sorted({e.superblock_count for e in live3}),
                              lpts[2].high_water, lpts[3].high_water,
                              sorted({e.attr for e in live2 + live3})))

                    a2, d2, b2 = lpts[2].check_slice_packing()
                    a3, d3, b3 = lpts[3].check_slice_packing()
                    ok = (d2 == 0 and d3 == 0 and a2 + a3 >= 20
                          and len({e.superblock_count
                                   for e in live2 + live3}) >= 3)
                    counts = sorted({e.superblock_count for e in live2 + live3})
                    report("LptEntry+2 packing", "PASS" if ok else "FAIL",
                           "the packed L2S predicts the NEXT live partition's "
                           "l2sIndex from THIS one's +2 (as count + %d): %d "
                           "agree / %d disagree %s, over %d DIFFERENT counts %s "
                           "giving strides %s -- NECESSARY BUT NOT SUFFICIENT: "
                           "count n with a %d-u16 overhead and count n+1 with a "
                           "%d-u16 one predict the same stride, which is how the "
                           "retracted 24/66 passed this 21/21.  'L2S array read "
                           "off media' is what settles it"
                           % (L2S_SLICE_OVERHEAD_U16, a2 + a3, d2 + d3,
                              (b2 + b3)[:4], len(counts), counts,
                              [L2S_SLICE_OVERHEAD_U16 + c for c in counts],
                              L2S_SLICE_OVERHEAD_U16, L2S_HEADER_U16))

                arrs = {}
                for i in (2, 3):
                    try:
                        arrs[i] = nt.l2s_array(i)
                    except ValueError as e:
                        arrs[i] = UNKNOWN
                        nt.notes = nt.notes + ("nsid %d L2S: %s" % (i, e),)
                if any(a is UNKNOWN for a in arrs.values()):
                    report("L2S array read off media", "SKIPPED",
                           "the lutId 2/3 region is not fully held here")
                    report("superblock count settled", "SKIPPED", "same")
                else:
                    det, ok = [], True
                    for i in (2, 3):
                        a = arrs[i]
                        packed, cursor, bad = a.check_packing()
                        same = sum(1 for lp, s in a.slices.items()
                                   if s.count == LogicalPartition(
                                       i, lp, a.raw[LPT_ARRAY_OFF + LPT_ENTRY * lp:
                                                    LPT_ARRAY_OFF + LPT_ENTRY
                                                    * (lp + 1)]).num_superblocks)
                        ok &= (packed and not bad
                               and same == len(a.slices)
                               and a.superblocks == a.total_superblocks
                               and a.live_counter == len(a.slices))
                        det.append(
                            "nsid %d: %d slices, count field == LptEntry+2 on "
                            "%d, tiling exact %s to cursor %d == next %d, sum "
                            "%d == accumulator %d, live counter %d"
                            % (i, len(a.slices), same, packed, cursor,
                               a.next_index, a.superblocks,
                               a.total_superblocks, a.live_counter))
                    report("L2S array read off media", "PASS" if ok else "FAIL",
                           "; ".join(det) + " -- the whole LPT+L2S buffer is "
                           "the lutId 2/3 LUT-region table, so the array IS "
                           "persisted (retracts 'not persisted')")

                    # the number itself, and the arithmetic that got it wrong
                    got = {i: arrs[i].superblocks for i in (2, 3)}
                    wrong = {i: sum(s.count + L2S_TRAILER_U16
                                    for s in arrs[i].slices.values())
                             for i in (2, 3)}
                    ok = (got[2] == 12 and got[3] == 63 and wrong[2] == 24
                          and lpts[2] is not UNKNOWN
                          and lpts[2].superblocks == 12
                          and lpts[3] is not UNKNOWN
                          and lpts[3].superblocks == 55)
                    report("superblock count settled", "PASS" if ok else "FAIL",
                           "NSID2 %d superblocks, NSID3 %d in the media "
                           "generation and %d in the current RootStorage one "
                           "(4 partitions deleted since the snapshot); the "
                           "retracted 24/66 is sum(count + %d), i.e. the "
                           "trailer u16 counted as a superblock -- %s and "
                           "55 + 11 = 66"
                           % (got[2], got[3], lpts[3].superblocks,
                              L2S_TRAILER_U16, "%d + %d = %d"
                              % (got[2], len(arrs[2].slices), wrong[2])))

                m = nt.l2s_map()
                if m is UNKNOWN:
                    report("L2S from the aux", "SKIPPED", "no backing to walk")
                else:
                    ok = (m.checked > 0 and m.mismatched == 0 and m.entries)
                    report("L2S from the aux", "PASS" if ok else "FAIL",
                           "%d/%d AUs satisfy lma%%wmasPerWeru == wma%%211968 "
                           "over WERUs %s; %d superblocks recovered %s; max "
                           "sbIdx seen %s%s; %s"
                           % (m.checked - m.mismatched, m.checked, m.werus,
                              len(m.entries), sorted(m.entries), m.max_sb_seen,
                              " -- so the lma//wmasPerWeru step is NOT "
                              "exercised on this board" if m.max_sb_seen == 0
                              else "", "; ".join(m.notes) or "no notes"))

                # End to end.  Take an (lpId, LP-local LMA) that the media
                # actually holds, convert it to the NVMe SLBA the host would
                # issue, and drive the whole chain forward from there:
                #   SLBA -> (lpId, lba) -> lma -> LPT -> L2S -> wma ->
                #   (ce, block, page, au) -> read -> aux must name both.
                hits, misses = [], []
                if m is not UNKNOWN:
                    for (nsid_, lp_, lma_) in m.samples:
                        slba = address_to_slba(nsid_, lma_ * LBAS_PER_LMA, lp_)
                        lp2, lba2 = slba_to_address(nsid_, slba)
                        got = nt.read_slba(nsid_, slba, count=LBAS_PER_LMA)
                        p = nt.slba_placement(nsid_, slba)[0]
                        if (lp2, lba2) == (lp_, lma_ * LBAS_PER_LMA) \
                                and isinstance(got, bytes) \
                                and len(got) == LMA_BYTES:
                            hits.append((nsid_, lp_, lma_, slba, p))
                        else:
                            misses.append((nsid_, lp_, lma_, got))
                if hits:
                    lines = ["%d of %d (lpId, LMA) pairs the media holds went "
                             "SLBA -> (lpId, LBA) -> LMA -> LPT -> L2S -> WMA "
                             "-> (ce, block, page, au) -> read, with the AU's "
                             "aux naming BOTH that LMA and that lpId:"
                             % (len(hits), len(hits) + len(misses))]
                    for n_, l_, lm_, s_, p_ in hits[:4]:
                        lines.append("            nsid %d SLBA %#x = LP %d LBA "
                                     "%d -> LMA %d -> %r"
                                     % (n_, s_, l_, lm_ * LBAS_PER_LMA, lm_, p_))
                    if misses:
                        lines.append("            %d not servable here: %s"
                                     % (len(misses), misses[:3]))
                    report("LP end to end", "PASS", "\n".join(lines))
                else:
                    report("LP end to end", "SKIPPED",
                           "no observed (lpId, LMA) is servable here "
                           "(%d probes)" % len(misses))

                det = []
                ok = True
                for i in (2, 3):
                    pc = nt.partition_coverage(i)
                    if pc is UNKNOWN:
                        ok = False
                        continue
                    live = pc.get("live")
                    det.append("nsid %d: %d of %d LPs in use holding %s "
                               "superblocks of %d LMAs = %s LMAs (%.1f%% of "
                               "totalLmas %s); %s WERUs owned by its MSOs %s"
                               % (i, len(live), pc["partitions"],
                                  pc.get("superblocks_declared"),
                                  pc["superblock_lmas"],
                                  pc.get("declared_lmas"),
                                  100.0 * pc.get("declared_lmas", 0)
                                  / pc["total_lmas"], pc["total_lmas"],
                                  pc.get("werus_owned_total"),
                                  pc.get("werus_owned")))
                report("corrected coverage", "PASS" if ok else "FAIL",
                       "; ".join(det))

            print()
            print(nt.describe())

    print()
    n_pass = sum(1 for _n, s in results if s == "PASS")
    n_fail = sum(1 for _n, s in results if s == "FAIL")
    n_skip = sum(1 for _n, s in results if s == "SKIPPED")
    print("SELF-TEST: %d PASS, %d FAIL, %d SKIPPED" % (n_pass, n_fail, n_skip))
    return 1 if n_fail else 0


if __name__ == "__main__":                              # pragma: no cover
    if "--selftest" in sys.argv:
        sys.exit(_selftest(sys.argv))
    print(__doc__)
