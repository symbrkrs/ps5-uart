# How data is stored on PS5 (Titania) NAND

The on-media data formats of the Marvell 88SS1098/88SS1099 ("Titania") SSD
controller as fitted to the PlayStation 5, from a host LBA down to the word line.
A format reference only: no transport, no tooling, no exploit.

**The split matters more than anything else here.** The *media and codec layer*
(page geometry, the AU container, scrambler, MCRC, both LDPC codes) is **solved and
verified byte-exact against silicon** — a real AU taken off the die re-encodes to the
identical 4608 stored bytes. The *storage layer* above it (MSOs, WERUs, LUTs,
RootStorage, the block map) is a **reconstruction** from firmware disassembly and from
one drive's structures: much of it is measured against media, some is parsed but
untested, some is unknown. Do not flatten the two. Every claim carries a grade.

```
  host  NVMe namespace, 512-byte LBA          -> Part 1
  FTL   LMA -> WMA: LUT, or LPT + L2S         -> Part 2
  RAID  WERU stripe, one parity AU            -> Part 3
  page  18432 B = 4 AUs (5 in the boot region)-> Part 4      aux -> Part 5
  AU    8 aux + 4096 user + MCRC, 2 codewords -> Parts 6 (MCRC), 7 (ECC), 8 (scrambler)
  phys  CE, block, page, word line, cell mode -> Part 9
```

---

## 0. Evidence markings

Every claim in this document carries one of these. They are not decoration.

| mark | meaning |
|---|---|
| **[M]** measured | checked against real media or real silicon. An outside fact. |
| **[D]** disassembly | read out of the EFC firmware or boot-ROM decompilation. True of the firmware; still an inference about the media. |
| **[C]** self-consistent | a regression against a reference this project produced. Catches a change in behaviour; it is not independent evidence that the behaviour is right. |
| **[P]** parsed only | the field decodes and round-trips, but nothing tests what it *means*. |
| **[?]** unknown | said so on purpose. Not a gap to be filled with a plausible guess. |

Two traps that generalise, recorded because both were walked into:

* A test set drawn from a table's own indices cannot see an error in the
  index-to-offset step. A "5489 exact / 0 wrong" LUT validation passed while the
  sub-granule offset term was missing, because every address in that set was
  granule-aligned.
* A budget that samples one corner is worse than no budget, because it looks like
  coverage.

Counts quoted below (`3568 right / 0 wrong`, `8218/8218`, and so on) are the
evidence for a format claim, not an inventory of any particular drive.

---

# Part 1 — the host-visible layer

## 1.1 Namespaces

The device exposes **three NVMe namespaces**, NSID 1, 2 and 3. `cMaxNumberOfMediaSpaces`
is 3 and Identify Controller reports `NN = 3` unconditionally **[D]**. Each namespace
maps to one *media space*, and each media space to one **msoGroupId**:

| NSID | media space | msoGroupId | role |
|---|---|---|---|
| 1 | 0 | 1 | main user data |
| 2 | 1 | 2 | logical-partition space, TLC-backed |
| 3 | 2 | 3 | logical-partition space, pseudo-SLC-backed |

The msoGroupId space itself has six members and **group 0 is `System`, not media
space 0**: `System, MediaSpace0-NSID1, MediaSpace1-NSID2, MediaSpace2-NSID3,
Unused, ParityData` **[D]**.

The media-space record is 16 bytes: `+0` the msoIdTable group index, `+4` the
`cLBAFTable` index, `+8` a format stamp, `+12` a valid flag. The firmware reads each
as a byte at a 4-byte stride, so the offsets are proven and the widths are not
**[D]** for the offsets, **[P]** for the widths.

## 1.2 Sector size and the LBA format table

`cLBAFTable` is a static two-row table in firmware **[D]**:

```
row 0 = { 0,   0,   4096, 4096, 4096, 4096, 8, 0,  1 }   disabled
row 1 = { 512, 524, 4096, 4192, 4096, 4192, 8, 12, 0 }   the live row
          hlbuData
               hlbuTotal
                    lmuData
                          lmuTotal ...              disabled flag at byte +0x1D
```

so:

| constant | value |
|---|---|
| host LBA (`hlbuDataSize`) | **512 bytes** |
| host block stored with metadata (`hlbuTotalSize`) | 524 bytes |
| logical media unit (`lmuDataSize`) | **4096 bytes** — one AU's user payload |
| LMU stored with metadata | 4192 bytes |
| `LBAS_PER_LMA` | **8** |
| `LBA_SHIFT` | **3** |

NVMe-visible consequences **[D]**: `LBADS = 9` (512-byte formatted LBA), `MS = 0`,
`AWUPF = 7` (atomic write unit = 8 LBAs = one LMA), and Dataset Management requires
each range to be a multiple of 128 LBAs (64 KiB, the 16-LMA LUT granule of every
user MSO).

**Every conversion uses `cLBAFTable` index 0 of `hostLbaFormat.formats[]`, i.e. the
single initialised format — not the command's FLBAS field** **[D]**, at six
independent sites.

## 1.3 Host LBA to internal logical address

```
lma            = lba >> 3
offset_in_lma  = (lba & 7) * 512
lmaCount       = ((lba + nlb - 1) >> 3) - (lba >> 3) + 1
```

There is no per-namespace base, no stride and no remap: each namespace's LMA space
starts at zero. The host-side CPU performs the shift before the media CPU sees the
command, so the internal message carries an **LMA, not an LBA** **[D]**, five
independent firmware instances; the arithmetic itself is **[C]**.

**The SLBA split is per namespace** **[D]**:

```
NSID 1        LBA = SLBA[33:0]                       (DW10 plus 2 bits of DW11)
NSID 2 and 3  lpId = SLBA >> 32       rejected if >= 0x10000
              lba  = SLBA & 0x7FFFFFFF
              lma  = lba >> 3
```

Constants: `LP_ID_SHIFT = 32`, `LP_LBA_MASK = 0x7FFFFFFF`, `MAX_LP_ID = 0x10000`,
`cNumOfEntries[nsIdx] = {16384, 1024}` — 16384 logical partitions on NSID 2, 1024 on
NSID 3. The NSZE bound check is applied to NSID 1 only; NSID 2 and 3 are bounded per
logical partition instead. The internal read command carries `+14 u16 lpId` beside
`+16 u32 lma` **[D]**.

## 1.4 Namespace capacity

Namespace size is taken from **one nominated MSO** **[D]**:

```
NSID 1, 2   msoIdTable[ 0*6 + group]   -> msoType 0  UserData
NSID 3      msoIdTable[10*6 + group]   -> msoType 10 UserSlcData
NSZE (in LBAs) = msoDescriptor[thatMso].totalLmas * lmuDataSize / hlbuDataSize
               = totalLmas * 8
```

Note that NSID 3 is sized from its *pseudo-SLC* partition; there is no
`UserData/NSID3` entry in the msoIdTable at all.

**The MSO is not part of a read address.** A namespace's LUT is selected by
`msoDescriptor +3` (`lutId`) and by nothing else; all the MSOs of a namespace —
GarbageCollection, Migration, UserDataNandCache, BlockMove, CopySlcData, StaticMan,
DirectTlcWrite and the dynamic sub-32 partition MSOs — are *write producers sharing
one address space*. `Lut_Update` invalidates the WMA it displaces and panics if a
writer installs a WMA already present, so there is at most one live WMA per
(namespace, granule). Measured: over 6 lutIds × 15 sampled LMAs, every MSO sharing a
lutId returns the identical entry, 90 agree / 0 disagree **[M]**. The owning msoId is
*derived after the fact* (LMA → WMA → WERU → owner), never chosen.

---

# Part 2 — the FTL / storage layer

Everything in Part 2 is a **reconstruction**. The structures are real and most of
their fields are read out of firmware that manipulates them, but the layer as a
whole was assembled from one drive, and an empty result frequently means "not found
here" rather than "not present".

## 2.1 The object model

| term | meaning |
|---|---|
| **AU** | allocation unit: 4096 bytes of user data plus 8 bytes of auxiliary. The unit everything is counted in. |
| **LMA** | logical media address, one per AU, in a per-LUT address space |
| **WMA** | write media address, one per AU, in a flat device-wide physical-ish space |
| **WERU** | write/erase relocation unit: the erase and allocation unit, normally 4 block numbers × 12 CEs = **48 physical blocks** |
| **MSO** | media storage object — a partition, identified by `(msoType, msoGroupId)` |
| **pbid** | physicalBlockId, `12*block + ce` |

Capacity constants **[M]**, from `geometry`:

```
BLOCKS_PER_WERU    48                 = 4 block numbers x 12 CEs
AUS_PER_TLC_WERU   211968             = 48 * 1104 * 4
AUS_PER_SLC_WERU   17664              = 12 *  368 * 4   (the 12-block metadata shape)
MAX_MSOS           63                 = cMaxNumberOfMsos
```

`blocksPerWeru = 48` is measured, not assumed: it is forced arithmetically
(48 × 1104 TLC pages × 4 AUs = 211968, the observed TLC WERU capacity) and it is
what a full survey of programmed blocks agrees with at stride 48 and at no other
stride.

> **Do not reuse the 12-block stride for the global block map.** `SystemBlockMapping`
> stores 12 pbids per slot because that is the *metadata* WERU configuration; the
> global `blockMapping` table's stride is 48. A stride-12 read of the global table
> produces plausible-looking block lists that are wrong.

## 2.2 The msoIdTable

`msoId = msoIdTable[msoType * 6 + msoGroupId]`. The table is a **firmware constant,
126 bytes, not on the media** **[D]**. `0xFF` marks an invalid combination; the
populated entries are exactly msoIds 32..62, strictly increasing. The 24 trailing
zeros are padding past the end of the table, not entries — an earlier decode read
them as real and invented a nonexistent "msoId 0".

Decoded (`--` = 0xFF, no such MSO):

| msoType | System | NSID1 | NSID2 | NSID3 | Unused | ParityData |
|---|---|---|---|---|---|---|
| 0  UserData | – | **32** | **33** | – | – | **34** |
| 1  UserData2ndWF | – | – | – | – | – | **35** |
| 2  GarbageCollection | – | **36** | – | – | – | – |
| 3  StaticMan | – | **37** | **38** | – | – | **39** |
| 4  Lut | **40** | – | – | – | – | – |
| 5  SystemMetadata | **41** | – | – | – | – | – |
| 6  Trim | **42** | – | – | – | – | – |
| 7  Migration | – | **43** | **44** | – | – | – |
| 8  UserDataNandCache | – | **45** | **46** | – | – | – |
| 9  GcNandCache | – | – | **47** | – | – | – |
| 10 UserSlcData | – | – | – | **48** | – | – |
| 11 CopySlcData | – | **49** | **50** | **51** | – | – |
| 12 BlockMove | – | **52** | **53** | – | – | **54** |
| 13 DirectTlcWrite | – | – | **55** | – | – | – |
| 14 RootStorage | **56** | – | – | – | – | – |
| 15 NeoFwPartition | **57** | – | – | – | – | – |
| 16 PhysicalMediaAccess | **58** | **59** | **60** | **61** | – | **62** |
| 17 MLC / 18 SLC / 19 TLC / 20 Undefined | – | – | – | – | – | – |

Types 17..20 are cell-mode and sentinel tags rather than partitions, which is why the
last four rows are empty. The anchor that pins the table's base is
`t[5*6 + 0] == 41` = SystemMetadata/System **[M]**, independently corroborated by
`msoDescriptor[41].totalLmas` landing on one of the four
`maxSystemMetadataSizeInLmas` capacity classes `(6144, 7424, 13824, 24576)`.

### msoIds 0..31 — the active logical-partition MSOs

There is a third cap besides `cMaxNumberOfMsos = 63` and
`cMaxNumberOfMsosForAllocation = 56`: **`cMaxNumberOfMsoForActivePartitions = 32`**
**[D]**. `AllocNeoRegisterActiveLogicalPartition` keeps two 16-entry `u16 lpId`
tables — one for msoGroupId 2, one for msoGroupId 3, both memset to `0xFFFF` at init
— takes the first free slot, and **the slot index is the msoId**:

```
msoId = slotIndex        for msoGroupId 2  ->  msoIds  0..15
msoId = slotIndex + 16   for msoGroupId 3  ->  msoIds 16..31
```

The inverse is a linear search returning `cInvalidMsoId` (255) when the partition is
not active. An operation naming the *static* msoId 44 (Migration/NSID2) or 48
(UserSlcData/NSID3) together with an lpId is redirected to that partition's dynamic
msoId. One escape exists: for group 2, if the **DirectTlcWrite** enable is set, the
lookup returns the static msoId **55** (DirectTlcWrite/NSID2) instead, and NSID 2
bypasses the per-partition MSO entirely. **[D]**

This is why the firmware seeds a reverse map with `(Migration, NSID2)` for msoIds
0..15 and `(UserSlcData, NSID3)` for 16..31 before filling in the static table: the
dynamic ids are clones of 44 and 48, and `Mso_GetMsoType` / `Mso_GetMsoGroupId` must
answer for them. Corroborating structure on media **[M]**: `msoDescriptor` rows 0..15
are one byte-identical template with `lutId 2` and rows 16..31 another with `lutId 3`.

## 2.3 msoDescriptor

A 28-byte-stride array, 63 entries, 1764 bytes. Fields used by the decode chain
**[D]** for the offsets, **[M]** for the values:

Field boundaries are not fitted: a per-field descriptor dumper in the firmware
emits one debug-event id per field, and between two copies of it the fields below
are exactly what it enumerates before stopping **[D]**.

| off | width | field |
|---|---|---|
| +0 | u8 | flags. bit 0 = valid (nothing clears it at runtime); **bits[4:3] `nandBlockMode`** — 1 = `cNandBlockModeSlc`, 2 = native TLC; bit 5 = this MSO's WERUs join shared allocation accounting; **bits[7:6] write-frontier fill class**, 3-valued (2 = never close a writeable unit early, 1 = threshold is a full block, 0 = derated) — mechanism proven, **no name anywhere**. Bits 1 and 2 are dumped and unnamed **[?]** |
| +1 | u8 | allocation type; 0 = `cAllocationTypeNone`. Computed values are {1, 4}; value 2 is compared against but never produced, so it can only arrive through a restore |
| +2 | u8 | dumped, unnamed **[?]** (a `cLBAFTable` index in one reading) |
| +3 | u8 | **`lutId`** — names an LMA address space **shared by a whole namespace group**; `0xFF` = this MSO has no LUT. `cMaxNumberOfLuts = 8` |
| +4 | u8 | `zeroDataId`, an index into a ZeroData pattern table. **Not `msoGroupId`** — it happens to equal the group for the three namespace groups only, and the sole readers bound-check it against the pattern table's count |
| +5 | u8 | LUT/rebuild flags: bit 0 = walk this MSO's LUT during a rebuild; bit 1 = set validMap bits as well as counting; bit 2 = a second LUT-maintenance gate; bit 3 = LUT backing store allocated in whole 16 KiB media pages |
| +6 | u8 | dumped, unnamed **[?]**; the LUT granularity in LMAs is carried here in one reading (only 1, 4 and 16 are accepted) |
| +7 | u32, **unaligned** | `totalLmas` |
| +11, +13 | u16 | dumped, unnamed **[?]** |
| +15 | u8 | flags, bits 0/1/2 dumped, unnamed **[?]** |
| +16 | u8 | **`raidType`** — 0 None, 2 Inline (pinned by name); non-zero non-2 = Mso |
| +17 | u8 | **`auxLayout`** — 0..3, see §5 |
| +18 | u8 | dumped, unnamed **[?]** |
| +19 | u8 | bit 0 = which lane of the rebuild-sequence pool this MSO draws from; bits[3:1] = parity WERU front, 7 = "none"; bits[7:4] have no reader |
| +20 | u8 | `lutStore` — the msoId in whose LMA space this MSO's LUT is stored. `0xFF` for the SystemMetadata MSO, whose LUT is a RootStorage object |
| +21..+27 | 7 B | **dead — a measured negative, not a gap.** Every root constructor memcpys a 20-byte template then stores one u32 at +20, so +21..+23 are zeroed by that store and +24..+27 are never written; three independent searches find zero reads and zero writes. Consequence: **4 bytes per record of uninitialised RAM are persisted**, because the whole 1764-byte array round-trips through its media section. **[D]** |

## 2.4 LMA to WMA, route 1: the LUT (NSID 1 and the system MSOs)

```
i = lma >> shift                      # shift = log2(granularity), one of 0/2/4
w = entry(i)                          # a PLAIN little-endian u32, not a bitfield
if w in LUT_SENTINELS: unmapped
wma = w + (lma & (granularity - 1))   # <-- the sub-granule term
```

The LUT entry names the granule's **first** AU; adding the sub-granule offset is the
*caller's* job. The firmware's own `Lut_GetEntry` is a bare `pTable[lma >> shift]`
and the LUT API panics on an unaligned LMA **[D]**, which is exactly why the missing
offset term was invisible to a granule-aligned test set.

**Sentinels** (LUT/WMA space) **[D]**:

| value | meaning |
|---|---|
| `0xFFFFFFFF` | unmapped. The initial state, what TRIM leaves behind, and what DeleteReservation restores. |
| `0xFFFFFFFE` | no writer was found anywhere in the four decompiled cores. The rebuild rule's inclusion of it appears vacuous on this firmware. Stated as a negative search result, not as proof. **[?]** |
| `0xFFFFFFFD` | RESERVED |
| `0xFFFFFFFC` | written by the rebuild's "incomplete SLC track" path |

`weruId = wma // 211968`.

**A trimmed LMA is byte-identical to an LMA that was never written** **[D]**: TRIM
reaches a `memset(..., 0xFF, 4*count)` over the LUT range. There is no distinct
"trimmed" sentinel, so the LUT cannot distinguish discarded from never-written data.
Only the trim log can, and only for as long as its entry survives.

Evidence for this route **[M]**: the sub-granule term verified by an MSO whose LMAs
0..3 land on AUs 0,1,2,3 of one page; the system LUT versus independent WERU scans
gives 0 mapped-but-missing and 0 found-but-unmapped; every LMA comes from the WERU
the LUT names.

### The LUT region descriptor

A LUT region on media is three LUT-granule steps of the hosting MSO's LMA space:

```
base + 0   16-byte header:  "LUT\0" (0x0054554C), version, infoLma, dataLma
base + 4   a 296-byte descriptor, at offset 0 of that LMA
base + 8   the raw WMA array: `lmaLutCount` little-endian u32
```

Descriptor fields **[D]** offsets, **[M]** values:

| off | field |
|---|---|
| +0 | **u64** `snapshotInformation.sequenceNumber` — the rebuild-sequence pool value captured when this descriptor was flushed, from the lane `msoDescriptor +19` bit 0 selects. The store is guarded on `count != 0`, so 0 with count 0 is a real zero. It is a **64-bit** store, which is why every u32 search for it missed |
| +8 | u32 `snapshotInformation.count`, bound 9 |
| +16..+231 | the snapshot list, stride 24, 9 elements exactly. Element `+0` u8 msoId; `+2` u16 weruId (`0x3FFF` = none); `+8` u64 that WERU's usage-log **activation stamp**; `+16` u32 WMA offset inside that WERU. Elements' `+4`, `+12`, `+20` read 0 everywhere held **[?]** |
| +232 | u32 — a **mirror** of the logical-partition reserved-space accumulator. One value is written to two places, here and in the partition table; both copies read the same value on media, so this is one value in two structures with no free parameters |
| +236 | u32 msoId that *initialised* this slot — not the same as "the msoId this LUT belongs to", and not constant across regions |
| +240 | u32 runtime pointer — **not meaningful on media** |
| +244 | u32 table size in bytes |
| +248 | u32, explicitly zeroed |
| +252 | u32 `maxLma`, highest LMA currently in use. The invariant is `(maxLma >> shift) + 1 <= lmaLutCount` — **`<=`, not `==`**; some regions are allocated larger than they are used, and enforcing equality rejects them |
| +256 | u32 `lmaLutCount`, entries allocated |
| +260 | u32 section count (1 unless the table is split) |
| +264 | u32 section shift |
| +268 | u32 section size |
| +272 | u32 short final section |
| +276 | per-section dirty bitmap, at most 4 bytes |
| +284 | u32 granularity in LMAs |
| +288 | u32 log2(granularity) |

Use `lmaLutCount` for the array length, never "how far the array happens to ascend".
The region chain is **walked, not computed** — an arithmetic reconstruction of the
region bases over-predicts **[M]**, so the walk is what the code does.

Measured region shape on the reference capture **[M]**: `snapshotInformation`'s msoId
list is exactly the set of MSOs whose `msoDescriptor.lutId` equals the region's
lutId — 7/7 and 4/4, no extras, no omissions — plus msoId 42 (Trim/System) in both.
The lutId byte comes from a different byte of a different structure, so this is an
independent confirmation of both.

## 2.5 LMA to WMA, route 2: the LPT and L2S array (NSID 2 and NSID 3)

The two logical-partition namespaces do not use a flat LUT. Their lutId slots hold a
**logical-partition table (LPT)** plus a **logical-to-superblock (L2S)** array
**[D]**:

```
              NSID2 (409760 B)              NSID3 (26048 B)
+0      u16   highest live lpId             same
+2      u16   live-partition counter        same
+4      u16   (unnamed)                     same
+8            LptEntry[16384] x 24 B        LptEntry[1024] x 24 B
+393224 u32   RESERVED-SPACE ACCUMULATOR    (NSID3 offset not established)
              -- the same value the trim log's entry +4 carries, and the same
              value the LUT region descriptor mirrors at its +232
+393230 u16   running sum of superblocks    +24584
+393232 u16   next free l2sIndex            +24586
+393234       L2S u16[8260] = 16520 B       +24588   u16[730] = 1460 B
```

**`LptEntry`, 24 bytes** (`elementSize = 0x18`) **[D]**:

| off | width | field |
|---|---|---|
| +0 | u16 | `attr = w & 3`, `l2sIndex = w >> 2` |
| +2 | u16 | **`numOfSuperBlocks`** — a count. Corroborated arithmetically: the L2S index allocated to each partition advances by exactly `numOfSuperBlocks + 4`, 21/21 across three distinct values; `4*n` and `n+3` both fail that test **[M]** |
| +4 | **i16** | superblock write index — **read it SIGNED**, so `0xFFFF` is the sentinel −1. It is a **seed, not a mirror, and it is measurably stale**: only the partition-erase path advances it, so it matches the live L2S value on 1 of 27 live partitions. **Never read it as the write cursor.** **[M]** |
| +6 | u16 | the partition's write cursor **within** its current superblock — a true mirror of the live L2S value, 27/27 **[M]** |
| +8 | u64 | activation stamp. **One u64**, stored with a single 64-bit store and reloaded as one — not a u32 plus an unnamed `+12`. Measured exactly in both namespaces: the highest stamp over a namespace's partitions equals that group's rebuild-sequence pool lane **[M]** |
| +16 | u32 | the partition's write position **in LMAs** as of its last read-write activation; `0xFFFFFFFF` = never activated. A **snapshot** — a cursor can advance far past it |
| +20 | u32 | bit 0 = "`+16` is valid". Maintained and **never consumed** — no branch anywhere tests it **[?]** |

`attr` values are named by a four-entry string table: 0 = empty, 1 = activated
read-only, 2 = activated read-write, 3 = deactivated. **Only 1 and 2 reach a read**
**[D]**. The create template is `{3, 0, 0, 0, 0xFFFFFFFF, 1}`, so a freshly created
partition starts at attr 3 and `+16`/`+20`'s otherwise puzzling values are simply the
template showing through.

> **Two modules in this tree read `+4`, `+8` and `+16` differently** — one names them
> as above with the measured cross-checks, the other declines to name them and reports
> `+8` as an unnamed u32 and `+16` as "an LMA". The named reading carries measured
> corroboration and is the one given here; the conservative reading is not wrong about
> the bytes, only silent about their meaning.

**L2S slice**, little-endian u16, stride `count + 4` **[D]** + **[M]**:

```
u16[0]          (numOfSuperBlocks << 4) | flags     <- the count
u16[1]          superblocks fully written
u16[2]          granules into the last one
u16[3 .. 3+n-1] n = numOfSuperBlocks superblock entries
u16[3+n]        the WRITE slot (open-in-SLC-cache marker) -- NOT a superblock
```

Superblock entry bits: `weruId = e & 0x3FFF`; `0x4000` = the superblock is open in an
SLC write cache, in which case the live weruId is DRAM-only and unknowable from
persisted state; `0x8000` = blank.

> **Retraction worth carrying.** The trailer u16 was once counted as a superblock,
> which inflated the per-namespace superblock totals. The slice stride is `count + 4`
> and the trailer is the fourth fixed u16, not a member. **[M]**, checked four ways:
> the slice count field against `LptEntry+2`, the tiling against the allocator's free
> cursor, the sum against the firmware's own running total, and the weruIds against
> an independent rebuild from media aux.

**The L2P step** for these namespaces:

```
wmasPerWeru = weruConfig[nsIdx ? 3 : 4].blocksPerWeru * (cellMode == 1 ? 1472 : 4416)
sbIdx       = lma // wmasPerWeru
weruId      = l2s_slice[3 + sbIdx] & 0x3FFF
wma         = 211968 * weruId + (lma % wmasPerWeru)
```

`1472 = 368*4` pseudo-SLC AUs per block, `4416 = 1104*4` native-TLC AUs per block. So
**NSID 2 → WERU configuration 4 → `wmasPerWeru = 211968`** (48 blocks, native TLC) and
**NSID 3 → configuration 3 → 70656** (48 blocks, pseudo-SLC). That is an independent
confirmation that NSID 3 is the pseudo-SLC namespace. **[D]**

Persistence: only the **LptEntry array** is a RootStorage object
(`SegmentedBuffer_Initialize("LptNsid2", …, 0x60000 B, firstObjectId 44, elem 24)`
and `("LptNsid3", …, 0x6000 B, firstObjectId 74, elem 24)`; `0x60000 = 16384*24`,
`0x6000 = 1024*24`). The **L2S array is also persisted**, inside the lutId 2/3 region
of the Lut MSO's LMA space, and reads back complete **[M]** — this retracts an
earlier "the L2S array is not persisted".

Independent second route **[M]**: rebuilding `(nsid, lpId, lma // wmasPerWeru) →
weruId` purely from the per-AU aux of group-2/3 MSOs (whose `auxLayout` is 2, so the
aux carries both an LMA and an lpId) agrees with the parsed array 12/12 on NSID 2,
and 44 of 44 AUs satisfy `lma mod wmasPerWeru == wma mod 211968`.

> **Known structural gap.** Every superblock index observed is 0, so the
> `lma // wmasPerWeru` division is **unexercised** on the reference capture. That is a
> property of the drive, not a coverage failure of the analysis — and the code prints
> it rather than passing quietly. **[?]** for the multi-superblock step.

## 2.6 WMA to (WERU, page, slot, AU)

```
weru, rem = divmod(wma, 211968)      # AUS_PER_TLC_WERU
page, r   = divmod(rem, 192)         # WMA_STRIDE
slot      = r // 4
au        = wma % 4
```

**`WMA_STRIDE = 192 = 48 blocks × 4 AUs is constant** — a WERU that owns fewer than
48 blocks leaves the tail of the window unaddressable rather than repacking. That one
fact explains a "+152 on a cycle of 192" step seen in an 11-block metadata region:
`192 - 11*4 = 148`, and the last in-window step is `+4`, so 152.

Grade: **[C]** in isolation (an exhaustive round trip proves it only against itself),
but **[M] by consequence** — the 48-block placement measurement below consumes this
function's `page` and `slot` on every one of its 3568 observations, and a wrong
`divmod` would have shown up as a mass page rejection or a slot permutation the law
could not match.

## 2.7 WERU configurations and where a WERU's blocks come from

The WERU width is **per configuration, not globally 48**. Six configurations exist.
They are built by firmware **[D]** and the drive **persists its own copy** as a
RootStorage object sized `48*numberOfWerus + 120`, whose first 120 bytes are six
20-byte records **[M]**:

```
+0  u16  cfgIdx (self-reference)
+2  u16  blocksPerWeru
+4  u16  weruCount
+6  u16  startColumn        -- a remainder-distribution start; 0 when every
                               weruCount*blocksPerWeru is a multiple of 24
+8  u16  DEAD IN THIS IMAGE -- written at init, never read.  Machine-checked
                               three ways, zero callers.  NOT NAMED         [?]
+10 u16  firstWeruId
+12 u16  mutable copy of weruCount; no decrementer found
+14 u8   cellMode: 1 = pseudo-SLC, 0 = native TLC, 2 = "no fixed page mode"
+16 u32  flags
```

| cfg | firstWeruId | WERUs | blocks/WERU | blocks/CE | cellMode | flags | role |
|---|---|---|---|---|---|---|---|
| 0 | 0 | 1 | 24 | 2 | 1 | 7 | RootStorage anchor (WERU 0) |
| 1 | 1 | 2 | 24 | 2 | 1 | 7 | RootStorage |
| 2 | 3 | 26 | **12** | 2 | 1 | 2 | metadata / firmware-data; a block **pair on six CEs** |
| 3 | 29 | 88 | 48 | 4 | 1 | 0 | pseudo-SLC data (the SLC cache) |
| 4 | 117 | 855 | 48 | 4 | 0 | 0 | **native-TLC main user store** |
| 5 | 972 | 98 | 24 | 2 | 2 | 2 | spare / raw-access pool; outside the valid map |

Configurations 0/1/5 are 24 blocks = a pair on all twelve CEs;
`blocks_per_ce = max(2, blocksPerWeru // 12)`. Derived totals **[M]**:

```
WERU_COUNT              1070   sum of all six weruCounts
                               = the WeruUsageLog record count, independently
WERU_COUNT_IN_VALID_MAP  972   sum of the first FIVE
                               = validMap section length / 26496, independently
sum(weruCount * blocksPerWeru) = 48000 = 4000 blocks x 12 CEs
```

**`cellMode 2` means "no fixed page mode"**, settled three ways **[D]** + **[M]**:
no consumer distinguishes 2 from 0 (every read of `+14` tests `== 1`, and the
normaliser is `if (cellMode != 1) return 2`); the spare-consuming function derives
the mode it wants from the *target* configuration; and measured over all 48000
blocks, the spare pool is the only configuration whose blocks do not share one
`pageBlockMode` value.

Configuration flag bits **[D]** + **[M]**:

```
bit 0  SKIP_DEFECT   "isSkipDefect" -- do not consult PhysicalBlockProperties,
                     on both the free-list BUILD side and the allocator's POP
                     side.  Set for cfg 0/1 only.
bit 1  NARROW        computed as `blocksPerWeru <= 24`.  Its only readers select
                     between two sub-codes of a debug trace event; it steers no
                     data path.
bit 2  BLOCKS_PINNED blocks are pinned to their WERU.  Set for cfg 0/1 only, and
                     two engines stand down when it is set: the spare-block
                     substitution engine, and the allocator (which then takes the
                     WERU's OWN blockMapping row and never swaps).
```

Named configuration ids from the firmware's own asserts: metadata = 2,
TLC data = 4, spare = 5. Rows 0, 1 and 3 have **no name in any image held** **[?]**.

The format-time block assignment is **arithmetic, not a table** **[D]**: there are 24
columns, `pbid = 24*pairIndex + column`, and each configuration takes a contiguous run
of block-*pair* indices in every column:

```
cfg 0  pairs    0..   0        cfg 3  pairs   16.. 191
cfg 1  pairs    1..   2        cfg 4  pairs  192..1901      <- the main TLC store
cfg 2  pairs    3..  15        cfg 5  pairs 1902..1999
                        2000 pairs x 24 columns = 48000 blocks = 4000 per die
```

For a 48-block WERU this is `pairs {base+2i, base+2i+1}` on all 24 columns. The rule
predicts, with nothing fitted, that the WERU carrying the live system-metadata image
sits on a specific block pair — and `SystemBlockMapping` says exactly that **[M]**.

Blocks move afterwards. Each of a WERU's block offsets (planes) owns its own
doubly-linked free list of virtual-block nodes, threaded **through the blockMapping
entry itself**; allocating a WERU pops one node per plane, and if the node belongs to
another WERU's row the two entries are swapped, so the physical block migrates. That
is why the swap primitive copies **bytes 0..15 only** and leaves the link words in
place **[D]**. Two flag bits per configuration: **bit 2 = blocks are pinned** (set for
configurations 0 and 1 only, which are the RootStorage anchors located by a fixed
weruId and therefore must be unrelocatable) and **bit 0 = skip the defect re-pop**
(also 0/1 only). Measured consequence: the only two configurations with zero block
movement drive-wide are exactly the two the firmware flags pinned **[M]**.

On-media footprint of one such move, for identifying future ones **[D]** + **[M]**:
two `blockMapping` entries trade bytes 0..15 (`+16..+19` do not move); the *allocated*
WERU's `WeruUsageLog` record gets a fresh activation stamp and a fresh block map,
while the donor's silently goes stale; `PhysicalBlockProperties` is unchanged, because
the erase count and status ride inside bytes 0..15 and travel with the block.

## 2.8 Slot to (CE, block)

**Two different laws, deliberately in two functions**, so that a 12-block record can
never be read under a 48-block rule.

**12-block metadata / pseudo-SLC WERUs:**

```
slot  = 2*(ce - ceBase) + (block - blockBase)          slot in 0..11
pbid  = 24*pairIndex + 12*half + slot
```

The CE split *cannot* be inferred from block numbers — two different WERUs can be the
same block pair on opposite halves of the CE range. `pbid = 24*pairIndex + 12*half +
slot` reproduces all five hardware-measured configurations, which are not an input to
it — a real hold-out **[M]**.

**48-block user WERUs:**

```
slot  = 4*ce + (block - blockBase)      ceBase = 0,  block in blockBase..blockBase+3
```

Grade **[M]**, and this is one of the strongest storage-layer results:

* rule-predicted `(ce, block, au)` versus placements found by an **independent
  aux-LMA scan**: **3568 right / 0 wrong**, over 251 `(weru, page)` windows, 11 CEs
  and 5 WERUs, in both cell modes;
* rivals scored on identical footing: pair/half-extended 204, stored-list position
  204, block-major 184, `ceBase = 6` 0, block order reversed 0. **Read those against
  their ceilings, not against zero** — a rival agrees wherever it is the *same
  expression*, which is 4, 4 and 2 of the 48 slots respectively, and **every rival
  scored exactly its ceiling and not one observation more**. A rival above its ceiling
  would mean the law had lost a slot.
* hardware hold-out: three pages of a WERU whose fourth block was absent from every
  earlier capture read fresh, **528/528 in both directions** (predict `(ce, block,
  page, au)` from the LMA and ask the page's own aux; and invert the aux back to a
  slot), reproduced exactly on a second full re-read;
* a vendored second derivation, written against the board while the primary was
  written against the decompilation, agrees over 4000 WMAs per WERU. That is an
  independent-*implementation* cross-check (**[M]+[C]** hybrid), not a second media
  check, and the two are forbidden from importing each other so that it cannot decay
  into a self-comparison.

### The block map (`blockMapping`)

A flat array of 20-byte entries, indexed `[weruId * 48 + position]` **[M]**:

| off | width | field |
|---|---|---|
| +0 | u32 | `physicalBlockId`; `0xFFFFFFFF` = unmapped |
| +4 | u32 | read-disturb accounting; bit 0 = refresh pending |
| +10 | u32, **unaligned** | a cache of `PhysicalBlockProperties[pbid]`, refreshed for every mapped entry in one sweep. Never split this into u16+u8+u8 — that cuts the erase count into three pieces. |
| +16 | u16 | `next` — this virtual slot's place in its plane's free list |
| +18 | u16 | `prev` |

Sentinels written over the link words when a slot is taken: `0xDF00DF01` = allocated,
`0xDEFEDEFF` = skipped because the block is defective **[D]**.

**The entry POSITION carries the slot, not the block number.** A `blockMapping` record
whose four block numbers are not consecutive cannot be expressed as
`block = blockBase + slot % 4`; the answer is a fixed permutation:

```
position = 24*(q & 1) + 4*(ce >> 1) + 2*(q >> 1) + (ce & 1)      q = block - blockBase
slot     = 4*ce + q                                              q in 0..3
slot     = POS_TO_SLOT[position]

POS_TO_SLOT = ( 0, 4, 2, 6, 8,12,10,14,16,20,18,22,
               24,28,26,30,32,36,34,38,40,44,42,46,
                1, 5, 3, 7, 9,13,11,15,17,21,19,23,
               25,29,27,31,33,37,35,39,41,45,43,47)
```

The permutation is **derived from firmware, not fitted** **[D]**: the rebuild files
each block into queue `pbid % 24` — a (plane, CE) bucket — and the fill routine says
entry `k` is filled from queue `(k & 1) + 2*(k >> 2)`. Equating the two *gives* the
closed form. It is also **measured on media** **[M]**: composing the aux-LMA-measured
`(ce, block) → slot` with the record's own `position → (ce, block)` yields
`position → slot` straight out of the media, and `POS_TO_SLOT` matches **3568 / 0**,
44 of 48 positions covered (the missing four are one dead die, not a sampling gap),
with no position ever giving two different slots. The `position == slot` rival scores
exactly its 204 ceiling and not one observation more.

The **inheritance** consequence — that a block swapped in to replace a defective one
keeps the position, hence the slot, of the block it replaced — follows from the swap
being an in-place exchange of whole 16-byte entries **[D]**, and is confirmed on the
record by a zero-free-parameter hold-out (every substitute lands on the position it
actually occupies, and the two structural corollaries of a position-preserving swap
into a (plane, CE) queue — the substitute is on the displaced block's CE, and on its
plane — hold as well) **[M] on the record**. A **media** hold-out of the inheritance
claim is not available from a drive on which no substituted WERU has ever been
written; that needs a different image, not more reads of one. **[?] on media.**

### Record shapes

A `blockMapping` record classifies as one of five shapes, and anything but the first
means arithmetic over four block numbers will not reproduce the WERU — use the
mapping. **[M]**

```
rectangular   48 blocks = 4 consecutive block numbers x 12 CEs.  The textbook WERU.
substituted   48 blocks, 4 per CE on all twelve, but MORE than four distinct block
              numbers -- bad-block management swapped one in.
invalid       names a block at or past the address-field ceiling.  Read this as
              "past the array", NOT as "a stale generation".
partial       fewer than 48 slots mapped, all in range: the 12-block metadata class,
              or a partly allocated WERU.
irregular     48 in-range blocks that are none of the above.
```

### The 12-block class allocates 12 and addresses 11

A 12-block metadata WERU **allocates twelve blocks but addresses only eleven**; the
difference is the inline RAID parity block at slot 11 (Part 3). **[M]**

* **Allocation width 12**: the usage-log writer fills slots `[0, blocksPerWeru)` from
  the `blockMapping` row and stamps `0x3FFF` over the rest, so **the first `0x3FFF`
  in a usage-log record reads that WERU's own `blocksPerWeru` straight off the
  media** — 24 / 12 / 48 / 24 across the four weruId ranges.
* **Addressable width 11**: across roughly 5940 LUT entries into that class,
  `wma mod 192` is only ever in `{0, 4, …, 40}` and never 44, so the valid window is
  `r < 44 = 11*4` — exactly the bound the firmware's own reduction tests. Slot 11 has
  no WMA, and a slot-to-physical map should refuse it rather than let a round trip
  invent one.

**Consequence for any decoder that ranks copies by recency:** a parity page carries a
perfectly ordinary aux — a real LMA and a real, *higher* page sequence, because it is
written *after* the pages it covers. Plain newest-wins therefore hands back parity
bytes as content. The correct rank key is `(not parity, sequence)`. **[M]**

## 2.9 RootStorage — where the FTL bootstraps

RootStorage occupies **NAND blocks 1..5**, pseudo-SLC, and is two *different stores*,
not copies of one **[D]**:

| block(s) | instance | objects |
|---|---|---|
| 1 | **Backup** | 100 |
| 2, 3, 4, 5 | **Bootstrap** | 84 |

`PhysicalBlockProperties` registers only on Backup, `WeruUsageLog` only on Bootstrap.
The Bootstrap instance's four blocks are **two sections of one store**, not four
copies and not four generations: `numberSections = 2` is a literal at the only
construction site, and one section lives per metadata WERU (a block pair on twelve
CEs), so blocks 2/3 are section 0 and blocks 4/5 section 1 **[D]** + **[M]**.

**Stripe placement** — measured, **0 violations over 5273 page/stripe pairs**, 44 of
44 (block, CE) combinations **[M]**:

```
stripe = base + STRIPES_PER_PAGE[block] * page + (ce // 2)
STRIPES_PER_PAGE = { block 1: 3, blocks 2..5: 6 }
```

CE `2k` and CE `2k+1` carry the same stripe, and so do the two blocks of a pair, so
every stripe exists in four physical copies. (`ce // 2` is real and appears in two
independent placement laws — the Backup instance uses `offset = (ce // 2) % 3` — but
it is neither a package nor a channel; see §9.1.)

The Backup instance's two sections split by **CE half** rather than by block pair.
Derived stripe-space quantities **[D]**:

```
stripesPerSection    = STRIPES_PER_PAGE * 368
availableStripes     = 2 * stripesPerSection
stripe NUMBER space  = 2 * availableStripes   -- the counter makes TWO passes over
                                                 the physical space before wrapping
sectionIndex(stripe) = (stripe % availableStripes) // stripesPerSection
```

Cross-section recency is the firmware's own rule — compare each section's
first-stripe number, with an override for the wrap — not "highest wins".

### The stripe part

One NAND page is one **stripe part**: 16384 bytes, the page's 4 × 4096 user bytes.
**[D]** + **[M]**

```
+0x00 u16  21          the version / type word
+0x02 u16  stripeNumber                     (0xFFFF = INVALID_STRIPE)
+0x04 u16  objectId stored in THIS stripe
+0x0C 64 B per-WERU stripe-part availability bitmap
+0x4C u32  generation of this WERU's content
+0x50 u32  current generation of the store
+0x54 u32  numberOfEntries N
+0x58      entry[N], 8 bytes each: { u16 stripe; u16 pad; u16 size; u8 z; u8 tag }
           then 16384 - (88 + 8N) bytes of object payload
metaDataSize = 88 + 8 * numberOfEntries
```

The catalogue entry is `{u16 stripe; u16; u32 size:24 | type:8}` **[D]**, so the byte
many tools call a "tag" is the object **type** byte, and **an equal type over a
contiguous run of object ids is one chained object**, not a run of singletons.

### Object names are verifiable offline, with no firmware image

The stored name tag is a **CRC-8, polynomial 0x07, init 0, over exactly 16 NUL-padded
bytes, plus 1 if all sixteen bytes are zero** **[D]**. It is stored per object and
re-checked on read, so it is a name-to-object binding a third party can verify
offline. Confirmed 4/4 on media **[M]**: `WeruUsageLog → 0xAB`, `LptNsid2 → 0x89`,
`LptNsid3 → 0xF0`, `PhBP → 0xF4`.

### SegmentedBuffer — how the large residents are stored

Objects too large for one stripe part are chained through a segmented-buffer
abstraction whose parameters derive as **[D]**:

```
segmentSize      = ((maxSegmentSize - headerBufferSize - 8) // elementSize) * elementSize
numberOfSegments = ceil(segmentsBufferSize / segmentSize)
lastSegmentSize  = segmentsBufferSize % segmentSize   (or segmentSize)
```

so **an element never straddles two objects**. One object's payload is
`u32 headerSequenceNumber | u32 totalSize | header[headerBufferSize] | data`, and the
live header is the one from the segment with the highest `headerSequenceNumber`.
**`totalSize` is the full-segment size unconditionally, even in a short final
segment — do not use it as a length**; the catalogue entry's own `size` is the
written length.

The complete set of registered segmented buffers in the image is **five** — the
usage log, the two logical-partition tables, the physical-block properties, and one
that is test code and never registered. **None of them is a trim structure**, and
that enumeration is what establishes where trim state does *not* live. **[D]**

**Objects** (id → size, type, what) **[D]** + **[M]**:

| instance | id | size | type | object |
|---|---|---|---|---|
| Bootstrap | 0 | 7424 | 0x01 | `SystemLut` / `userLutLut` — the SystemMetadata MSO's L2P table |
| Bootstrap | 1 | 16 | 0x00 | `SystemMetadataInformation`; byte 0 is the DIRTY flag |
| Bootstrap | 2 | 2240 | 0x01 | `Allocation`, 56 × 40 B by msoId |
| Bootstrap | 3 | 840 | 0x00 | `SystemBlockMapping`, magic `0x20170620` |
| Bootstrap | 4..25 | — | 0xAB | `WeruUsageLog` (chained; tag = CRC-8/0x07 of the name) |
| Bootstrap | 43 | 61 | 0x00 | `ErrorLog` |
| Bootstrap | 44..69 | — | 0x89 | `LptNsid2` (chained) |
| Bootstrap | 74..75 | — | 0xF0 | `LptNsid3` (chained) |
| Bootstrap | 83 | 1272 | 0x00 | SystemLut root record, 6 LUTs |
| Backup | 0 | 4 | 0x00 | Backup root record |
| Backup | 6 | 5104 | 0x00 | `DdmData` — the drive's SMART/health counters |
| Backup | 7 | 5120 | 0x00 | `PhysicalBlockProperties` |
| Backup | 8..20 | — | 0xF4 | `PhBP` segments |

The chain tags are a **CRC-8 (poly 0x07) over the 16-byte object name**, which names
three Bootstrap chains with no firmware image at all — two independent routes agreeing
**[M]**.

**RootStorage AUs are not LMA-addressed**, so their aux LMA field is the invalid
marker `0xFFFFFFFF`. That is a free 32-bit cross-check that a page descrambled
correctly, and it is worth having, because the MCRC is computed over the *stored*
bytes and passes whether or not the descramble used the right seed. Correction worth
recording: the whole 8-byte aux is *not* the constant `ff ff ff ff 00 00 00 00` —
RootStorage follows the same convention as data AUs, with the page sequence number in
AU1 and zeros elsewhere **[M]**.

**`SystemBlockMapping`**, the object that locates the metadata WERU:

```
u32 magic = 0x20170620
u32 nextSlot
entry[16] { u16 weruId ; u16 pad ; u32 pbid[12] }        -> 8 + 16*(4 + 48) = 840
```

The 840-byte object size pins the layout exactly, and the firmware's own size formula
for it is `64*blocksPerWeru + 72`, which at 840 gives 12 — so the firmware really does
call that field `blocksPerWeru`, and it is the **metadata** WERU's, not the global 48.
`0x3FFF` in a slot is the *weruId* field, not a block number; an earlier reading took
it for a block and was right only by coincidence. A free slot's pbid array is stale
residue, not a location. **[M]**

**The replay loop installs the block mapping of EVERY valid slot at once and never
picks one** **[D]**. Multiple live slots are therefore **multiple halves of one live
image**, not multiple generations — and there is no store-generation field anywhere in
the firmware. A reader that treats a second live slot as an older image will look for
sections in the wrong WERU, because the nine SystemMetadata sections are split across
the halves.

**`SystemMetadataInformation`** (Bootstrap object 1, 16 bytes) is the **first** thing
the mount reads: `+0 u8 DIRTY flag`, `+4 u32 formatVersion`, `+12 u32`. When the dirty
byte is set the firmware **discards** the stored `validMap` and `validCount` and
rebuilds them (§2.13), so on a dirty store those two sections are evidence about a past
instant, not truth. **[D]** + **[M]**

### The bootstrap order

The firmware's own mount sequence, and the reason a decoder must follow it **[D]**:

1. read `SystemMetadataInformation` — the dirty flag;
2. read `SystemBlockMapping` and replay **every** valid slot's block mapping;
3. read the **SystemLut** (Bootstrap object 0) — the SystemMetadata MSO's own
   LMA→WMA table, and the authority on which installed WERU holds the live copy of
   each LMA;
4. scan every named WERU page by page; each AU's aux carries its LMA, so a WERU is
   reassembled **by LMA, not by address**, newest-wins *within* one WERU on the page
   sequence;
5. LMA 0 is the 48-byte directory (§2.10).

**Sequence numbers do not rank across WERUs** — measured, 1884 newer-or-equal against
4056 older — which is exactly why step 3 exists. For the section-base LMAs in
particular, one WERU's copies all happen to carry the higher sequence, so a
"newest wins" rule looks convincing and is wrong. **[M]**

**`Allocation`** (Bootstrap object 2, 56 × 40 B, current WERU pair at `+0x18`) records
only each MSO's current **write pointer**, not everything it owns. An MSO can own
WERUs and have no Allocation entry. Never read "absent from Allocation" as "owns
nothing". **[M]**

## 2.10 The SystemMetadata image

The SystemMetadata MSO (msoId 41) holds a log-structured image addressed by LMA
through LUT region 0. **LMA 0 holds a 48-byte directory** of twelve little-endian u32
**[M]**:

```
magic = 0x00535953 ("SYS\0")
formatVersion = 0x02000068
revision
blockMappingLma      userLutLutLma        msoLma
mediaSpaceLma        weruDescriptorLma    weruBinLma
validMapLma          validCountLma        rebuildSequenceNumberPoolLma
```

Section layout, from the firmware's own format routine **[D]**:

```
lmas(size) = ceil(size / 16384) * 4                       # SECTION_UNIT, LMAS_PER_UNIT
base[0]    = 36                                           # SECTION_FIRST_BASE
base[n+1]  = base[n] + lmas(size[n]) + 32                 # SECTION_GAP after EVERY section
```

so a section's **length** is `nextBase - thisBase - SECTION_GAP`, never
`nextBase - thisBase`. `LMA_BYTES = 4096`.

**The image is log-structured.** One LMA appears many times and only the newest copy
is live. Recency is the **page sequence number** carried in the aux (§5), and taking
whichever copy you scanned last silently returns stale data. **[M]**

Section layouts:

| section | layout |
|---|---|
| `blockMapping` | 20 B per entry, indexed `[weruId*48 + position]` — §2.8 |
| `weruDescriptor` | 20 B per weruId — below |
| `weruBin` | 53 × 16 B: a doubly-linked list head per lifecycle state — below |
| `mso` | 63 × 28 = 1764 B — §2.3 |
| `validCount` | u32 per weruId, covering 972 WERUs |
| `validMap` | 972 × 26496 B = 24.56 MiB — below |
| `rebuildSequenceNumberPool` | 144 B = 6 × 24, one record per msoGroupId — below |
| `userLutLut` | three allocation units — §2.4 |
| `mediaSpace` | not decoded **[?]** |

Every section is padded up to a whole 16 KiB granule and the tail past the structure
is **not** interpreted. A section decoder walking past `WERU_COUNT * BLOCKS_PER_WERU`
reads padding bytes into pbids in the billions.

**`weruDescriptor`, 20 bytes per weruId** **[M]** unless noted:

| off | width | field |
|---|---|---|
| +0 | u8 | **owner msoId** |
| +1 | — | no reader, no writer, no initialiser anywhere in four cores — a measured negative |
| +2 | u16 | written 0 once by the array initialiser, no reader — a measured negative |
| +4 | u16 | `weruId`, a self-reference |
| +6 | u16 | next in bin |
| +8 | u16 | prev in bin |
| +10 | u8 | **cellMode: 1 = pseudo-SLC, anything else = native TLC** **[D]** |
| +11 | u8 | binId |
| +12 | u32 | eraseCount |
| +16 | u32 | capacity in AUs |

Owner is `+0` as a **u8**: byte +0 spans 0..59 with bytes +1..+3 essentially always
zero, which rules out an alternative "+0 u32 eraseCount" reading **[M]**. The
`+6`/`+8` links chain consistently, as a doubly-linked bin list must.

**`rebuildSequenceNumberPool`**, 144 bytes = 6 × 24, one record per msoGroupId. The
record is **not one cursor**: every live dereference is
`pool + 24*msoGroupId + 8*(msoDescriptor[msoId][19] & 1)` **[D]**, so

```
+0  u64   the SHARED lane   (MSOs with +19 bit 0 clear); also the allocator
+8  u64   the RESERVED lane (MSOs with +19 bit 0 set)
+16 u64   the end of the reserved window -- checked only for those MSOs
```

The bit-0-only overflow guard pins the pairing at three separate panic sites. All
three qwords of all six records initialise to **1**, so an untouched lane reads 1, not
0. **A decoder that reports `+0` as "the" sequence number is wrong for every MSO whose
`+19` bit 0 is set.**

### weruBin — 53 allocation bins

`53 × 16 = 848 bytes` **[D]** + **[M]**:

```
+0 u8   bin KIND = the weruConfigurationId this bin serves; 6 = "no configuration"
+2 u16  HEAD weruId, 0x3FFF = empty
+4 u8   binId self-reference
+8 u32  number of WERUs in the bin
```

The list is **circular** through `weruDescriptor +6`, and the falsifiable test —
the walk terminates back at the head with length equal to the stored count, and every
member's `+11` equals the binId — passes on every non-empty bin **[M]**.

Bin names come from a 53-entry pointer table indexed by binId; only band heads carry a
pointer, and the NULL interiors are themselves the proof that three of these are
*bands* **[D]**:

```
 0 Free              1 Erase             2..17 Valid[band]
18..33 GarbageCollection[band]           34..40 ActiveCopySource[band]
41 Retired          42 UsedSystemWeru    43 ActiveWrite      44 ActiveErase
45 FreeNandCache    46 EraseNandCache    47 SystemFree       48 SystemErase
49 RootStorage      50 WaitDecommission  51 Decommission     52 Decommissioned
53 = the "in no bin" sentinel written into weruDescriptor +11 on removal
```

`53 = 2 + 16 + 16 + 7 + 12` is a **sum**, not a product, and the boundaries appear
literally in the code. Inside the two 16-wide bands the slot is **[D]** + **[M]**:

```
binId = bandBase + slcMode + 2 * lutId
```

with `lutId` from `msoDescriptor +3` and `slcMode` from `weruDescriptor +10`; the
factor 16 = 2 × 8 follows from `cMaxNumberOfLuts == 8`. Which free pool an MSO draws
from is a per-msoType table **[D]**: `Free` for UserData, UserData2ndWF,
GarbageCollection, StaticMan, Migration, BlockMove and DirectTlcWrite;
`FreeNandCache` for the two NandCache types, UserSlcData and CopySlcData;
`SystemFree` for Lut, SystemMetadata, Trim and NeoFwPartition; `RootStorage` for
RootStorage.

### validMap — addressing

The per-AU liveness bitmap **is written to NAND** and read straight back at mount; it
is rebuilt only when the persisted dirty flag is set (what a rebuilt value means is
§2.13). The firmware's own addressing **[D]** + **[M]**:

```
dwordsPerWeru = ceil(wmasPerWeru / 32) = ceil(211968/32) = 6624
word = base + 4*dwordsPerWeru*(wma // wmasPerWeru) + 4*((wma % wmasPerWeru) >> 5)
bit  = wma & 31
```

Each WERU owns a fixed `4 * 6624 = 26496`-byte row, so the whole table is
`972 × 26496` = 24.56 MiB. The falsifiable test — `popcount(row(w)) == validCount[w]`
— passes on 941 of 972 rows over values from 1 to 211968; the 31 exceptions are
structural, not corruption, because the map-setting call is made only for MSOs whose
`msoDescriptor +5` bit 1 is set, so the rest are **counted but not mapped** **[M]**.

`validCount == 0` does **not** mean stale for System-group MSOs: the firmware area is
addressed directly, not through LMAs, so the firmware partition reads 0 while holding
the entire firmware image. And `capacity − validCount` is an **upper bound**, not
"deleted data" — it assumes a fully written WERU. **[M]**

### Allocation — the write frontier (RootStorage Bootstrap object 2)

`2240 = 56 × 40` = the allocation-MSO cap times the record size, a verbatim copy of
the runtime array **[D]**:

```
+0  u32  the NEXT WMA to write
+4  u16  RAID command, one 4-bit nibble per plane
+6  u8[3] logical RAID buffer ids, 0xFF = invalid
+9  u8   raidStripeInfo: bits[7:6] parityMode, bits[5:0] stripeCount
+10 u32  RAID parity AU buffer   (a DRAM pointer, meaningless off-device)
+14 u32  RAID parity AUX buffer  (likewise)
+22 u16  bits[14:3] = WORDLINE
+24 u16  currentWeruId,  0x3FFF = none
+26 u16  PENDING (pre-reserved next) weruId, 0x3FFF = none
+28 u8   bit 0 requestOngoing; bits[5:4] plane count
+29 u8   planeCount * AUs per plane
+32 u32  page_base_wma -- the first WMA of the page currently being filled
```

so `wma − page_base_wma` is how many AUs are already placed in the open page. **`+26`
is a pre-reservation, not a second concurrent front**: the frontier advance moves
`+26` into `+24` and resets `+26` to `0x3FFF`. One MSO has one open write front plus
one reservation. **[D]**

Measured across four stored copies of the object **[M]**: `parityMode` equals the
owner's `raidType` 8/8; `stripeCount == 48` (the Mso-mode column count) 8/8; both
buffer pointers are 32-byte aligned and inside the shared-DRAM window 8/8; `+4` equals
the plane-nibble mask implied by the plane count 8/8; and no logical buffer id is
claimed by two MSOs in one copy, 4/4 copies.

## 2.11 The WERU usage log

`WeruUsageLog` is a Bootstrap RootStorage segmented buffer (16-byte header, hard cap
of 37 segments). Its record size is derived, not chosen **[D]**:

```
recordSize = 6 * BLOCKS_PER_WERU + 20 = 6*48 + 20 = 308 bytes
segmentsBufferSize = recordSize * numberOfWerus      (numberOfWerus = 1070)
```

One record per WERU **[D]**:

```
+0  u64   ACTIVATION SEQUENCE NUMBER
+8  u64   ORIGIN activation sequence number of the contents
+16 u8    OWNER msoId, 255 = unowned
+17 u8    flags
+20       48 block-map entries of 6 bytes each
```

Block-map entry, 6 bytes **[D]**:

```
+0 u16   physicalBlockId // 24        (the PAIR INDEX; 0xFFFF unmapped, 0x3FFF unused)
+2 u32   physicalBlockProperties, frozen at this WERU's activation
```

`+2` is one u32 copied verbatim out of `blockMapping[+10]`; reading it as u16+u8+u8
splits an erase count across three fields.

`+0` is stamped from **one global u64** which is copied into the record and then
post-incremented, so `max(+0) + 1` is the number of WERU activations the drive has
performed since format. Activation `mem_clr`s 20 bytes first, so it also **clears the
flags byte and destroys `+8`**. The pair-index divisor 24 is the same 24 as the column
count; the decomposition was verified against `blockMapping` on 723 whole WERUs,
31344 slots, **0 disagreements** **[M]**.

`+8` is written only by the relocation path (`b.origin = a.origin ? a.origin :
a.activation`), so it dates the *data* rather than the container. It is
**observable telemetry with no consumer**, not a verified lineage stamp: activation
destroys it, nothing branches on it, and `origin <= activation` is tautological
because every assignment takes a strictly earlier value of one monotone counter.
Report it; do not believe it as corroboration of anything. **[D]**

**The flags byte** (+17), every mutator enumerated from its call sites **[D]**:

| bit | name | set by |
|---|---|---|
| 0 | `invalid` | a latch, set wherever a WERU stops holding anything live: one-time init on every record, the format sweep on a WERU with a defective block, the write-pointer handover on the WERU being closed, the LMA release path, the decommission engine |
| 1 | `erased` | the format sweep, on every WERU whose blocks are all good |
| 2 | `decommissioned` | the decommission RPC and the decommission engine |
| 3 | `decommission_done` | the decommission engine only, and only when bit 2 was already set before re-activation |
| 4 | `fading_rewrite` | set at activation while the retention/torn-stripe rewrite sweep is armed; a set bit surviving a power cycle means the sweep never finished |
| 5 | `rebuild_write` | set at activation while rebuild-write mode is armed |
| 6, 7 | — | never read and never written anywhere in the image |

**This log, not `weruDescriptor`, is the ownership authority on a dirty store.** Every
mutator marks its record's segment dirty and the buffer is flushed at dirty-segment
granularity, whereas `weruDescriptor` is written only by one step of the
SystemMetadata flush and skipped even then on an unchanged CRC. The bin rebuild
re-derives owner, state and counters from here. The two structures disagree on a
substantial fraction of WERUs, and the log is what the firmware would believe.
**[D]** + **[M]**

## 2.12 Physical block properties

`PhysicalBlockProperties` is **one u32 per physical block**, the drive's wear and
bad-block ledger, persisted as Backup RootStorage object 7 plus a `PhBP` segmented
chain (`0x40`-byte header, 4-byte elements, up to 0x48 objects; the header carries
`4*numberOfBlocks` at +0 and, at +56, the u64 total erase count as of the last
backup) **[D]**.

| bits | field |
|---|---|
| [23:0] | **erase count** |
| [25:24] | **`pageBlockMode`** — 1 = `cPseudoSlcPage`, 2 = `cNativePage`, 3 = no mode assigned yet, 0 never observed |
| [27:26], [31] | never read and never written; measured 0 on 48000 of 48000 blocks |
| [30:28] | **status** |

Status names are the firmware's own **[D]**: `None, Factory, ReadGrown, WriteGrown,
EraseGrown, FakeGrown, UndiscoveredIgnoredFactory, DiscoveredIgnoredFactory`. **A
block is defective unless its status is `None` (0) or `UndiscoveredIgnoredFactory`
(6)**. The writer panics if asked to store 6 or 7, so those two are assigned only at
format. `0xFFFFFFFF` is the illegal value the sanity check reports and repairs.

`pageBlockMode` is measured over all 48000 blocks **[M]**: every block of a
pseudo-SLC configuration reads 1 and every block of the native-TLC configuration reads
2, exactly; the spare pool is the only configuration that mixes, and the mode-carrying
exceptions there are precisely the displaced defective blocks parked in it, still
carrying the mode of the WERU they were pulled out of. The spare-block finder compares
this field against the *target* configuration's mode, taking an exact match, else a 3,
else — only when it wants pseudo-SLC — a 2. **[D]**

Testing "status is non-zero" **over-reports defects**, because status 6 is a factory
mark the firmware deliberately ignores.

**Three copies of this word exist on media**, written by different paths at different
moments: the segmented buffer (the only view that covers blocks no WERU maps — this is
the authority), the `blockMapping +10` cache inside the SystemMetadata image, and the
usage-log block-map entries, frozen at each WERU's activation. The first two agree
**48000/48000** on erase count, mode and status — two independent writers, one answer
**[M]**. Differencing the frozen copies against the live ones gives the erases since
each WERU was last opened.

## 2.13 What a zero in validMap / validCount means

`validMap` (layout in §2.10) and `validCount` are the FTL's own accounting of how much
of a WERU is live. **After a dirty mount they are evidence, not truth.**

The 48-step full-mount FSM **[D]**:

| step | what it does |
|---|---|
| 25 | dirty ? `ValidMapReset` : `ValidMapRead` |
| 26 | dirty ? nothing : `ValidCountRead` |
| 31 | `RebuildValidMap` over two msoIds (lutId 0 and lutId 5) — unconditional, both paths |
| **35** | dirty only: a sub-FSM ending in four LUT rebuilds, one per lutId, in the order 4, 1, 2, 3. Each installs a 5/6-step FSM whose step 1 **replays the whole LUT into the map** and whose step 2 sets `validCount[weru] := popcount(validMap[weru])` for every WERU the scan marked. |

Step 35 is where a WERU whose map was *not* repopulated acquires a **rebuilt zero** —
scanned, counted, and counted as empty. Which WERUs get their map back is gated by
`msoDescriptor +5` bit 1 of the **LUT's own `snapshotInformation.list[0].msoId`** —
*not* the owner of the WERU being rebuilt — so the bit cannot be read off the WERU's
owner, and a reader that tries gets the wrong answer for some regions **[D]**. The
scan set is **per lutId, not per msoId**.

Three meanings a zero can have, and only one of them means "empty":

| code | meaning |
|---|---|
| `rebuilt` | reconstructed from the LUT at this mount, so 0 means EMPTY |
| `forced-zero` | the stage wrote 0 regardless of content (scanned but not replayed) — 0 means nothing |
| `never-touched` | no mount stage scans this WERU; it keeps the reset's zero — 0 means nothing |
| `not-maintained` | this MSO's `+5` bit 1 is clear, so no map is kept in any mount; 0 is structural |

The second bulk writer at step 35 was found only after an earlier exhaustive
enumeration of the *wrong symbol* concluded that no later stage restores the map. The
selftest now **runs** the second writer rather than citing it: replaying one lutId's
whole LUT reproduces stored validMap rows bit for bit **[M]**.

> **Consequence for anyone reading this format: for a WERU whose zero is
> `forced-zero` or `never-touched`, the STORED `validCountLma` is better evidence
> than the rebuilt one** — even though the firmware itself discards it. Do not read a
> rebuilt zero as "this WERU is empty."

A second trap: **`validCount` counts LUT granules, and the granule differs by lutId.**
Measured directly — every set validMap bit of a granularity-4 WERU is 4-aligned with
minimum gap 4, every set bit of a granularity-16 WERU is 16-aligned with minimum gap
16, over 16 WERUs with 0 exceptions **[M]**. A third unit exists: a closed-full
assignment writes `validCount[w] = columns * wmasPerBlock = 48*4416 = 211968`, i.e.
AUs. Never compare two validCounts without checking both owners' granularity.

## 2.14 The trim log

Trim state is **not** in RootStorage. The complete set of segmented-buffer
registrations is five, and none of them is a trim structure; the CRC-8 name tags on
media confirm it independently **[D]** + **[M]**.

**The trim state is a log of whole pages written into the Trim MSO's own WERUs,
identified by a sentinel in their aux, and found by scanning.** There is no index, no
LUT and no root object, which is exactly why the Trim MSO's `lutId` is `0xFF`. One
entry is one page; the entry size is `4096 * ausPerPage = 16384`, and the write
asserts `ausPerPage == targetLmaList.count`. The four target LMAs are set to
`[sentinel, -1, -1, -1]` where **[D]**:

```
sentinel = 0xFFFFFFFC   if msoGroupId(entry.msoId) == 5 (ParityData)
           0xFFFFFFFE   otherwise
```

> **`0xFFFFFFFE` and `0xFFFFFFFC` are LMA-space markers, not LUT/WMA sentinels.**
> They appear in the aux of a trim-log page. The LUT-space sentinels are the four
> listed in §2.4, and the two spaces must not be conflated.

**The entry record.** `TrimParameters_t` is 12 bytes and `LmaRange_t` is 6, forced by
the firmware's own size assertion compiled as `entrySize < 6*n + 12` **[D]** + **[M]**:

```
+0   u8   type   0 Trim, 1 Reserve, 2 DeleteReservation, 4 Invalid
+1   u8   } zero on every measured record
+2   u16  }
+4   u32  RESERVED-SPACE ACCUMULATOR, not a sequence number.  A signed delta is
          added to it, it is refused a negative value, and dividing it by
          pages*blocks yields a WERU count -- it counts reserved LUT entries, not
          events.  Taken only when type != 0.  Measured: it DECREASES 200 times in
          write order, which is what settles it as an accumulator.
+8   u8   msoId -- the TARGET MSO, not the Trim MSO
+9   u8   zero on every measured record
+10  u16  numberOfLmaRanges, < 256
+12  LmaRange_t[n]  { u32 first; u16 count }
```

**The range units depend on the type, and this is not a detail** **[D]** + **[M]**:

```
type 0 Trim               (first, count) are LMAs; the writer asserts both are
                          multiples of the LUT granularity and then divides.
type 1 Reserve            (first, count) are LUT ENTRY INDICES.
type 2 DeleteReservation  likewise -- passed straight to code that indexes
                          lutTable + 4*first, with no shift and no alignment check.
```

Measured, a clean separation: **27/27 type-0 ranges are granularity-aligned, and
0/414 type-1/2 ranges are.** Reading a type-1 range as an LMA range would violate the
firmware's own assertion 414 times. **[M]**

**Write order is the WMA offset, not the page sequence.** A trim-log page's aux
sequence comes from the rebuild-sequence pool of the entry's **target** group, so
ordering the whole log by page sequence interleaves two independent pools wrongly —
measured, 23 inversions mixed and **zero inversions inside each pool**, each strictly
rising with WMA offset. **[M]**

**Each entry appears twice**, in two blocks of the *same* CE, byte-identical including
the page sequence (434/434 positions holding both, 0 lone). Do not count them twice —
and do not read the second copy as inline parity: on this WERU the aux parity flag is
set on 100% of the AUs of *all* readable slots, against a control WERU where it is
100% on slot 11 and 1–2% elsewhere, so on this WERU the flag does not identify a
parity slot. Whether the Trim WERU carries inline parity at all is **[?]**. **[M]**

**The retirement watermark**, on media and exact **[D]** + **[M]**: the retirement
pass walks all lutIds, takes every LUT-descriptor snapshot element whose msoId is the
Trim MSO's, and keeps the **lexicographic minimum of `(u64 activation, u32 wmaOffset)`**.
It then fully invalidates every WERU ahead of that one in the Trim MSO's chain and
clears the valid map of every AU of the watermark WERU below `base + wmaOffset`. **If
any element for the Trim MSO still carries the `0x3FFF` "no WERU" sentinel the whole
search aborts and nothing is retired.** Three independent on-media facts corroborate
the watermark, none of them used to derive it: the Trim WERU's `validCount` equals the
count of entries above the watermark; the last entry at or below it ends exactly on
the watermark and its `+4` accumulator matches the LUT descriptor's mirror of the same
value byte for byte; and the first entry *above* it is the one whose LUT effects are
still un-applied on media — which is what a pending journal looks like.

The size of the *retired* trim history is not recoverable from a single image: the
watermark says the log is truncated and says exactly where, but what preceded the
truncation was erased and re-owned. That needs an image captured earlier, not more
analysis of one image. **[?]**

## 2.15 DdmData

Backup RootStorage object 7 (5120 bytes) is the persistence image of five firmware
objects, one per 1024-byte slot **[D]**:

| slot | offset | size | manager | version |
|---|---|---|---|---|
| 0 | 0 | 0x50 | HostStatistics | 2 |
| 1 | 1024 | 0x64 | DeviceHealth | 4 |
| 2 | 2048 | 0x0C | DeviceInformation | 1 |
| 3 | 3072 | 0x20 | MediaHealth | 1 |
| 4 | 4096 | 0x2E0 | MediaStatistics | 4 |

The five `version` words are the **only** integrity mechanism in the whole 5120 bytes
— no magic, no length, no CRC, no checksum — and the inter-slot gaps are zero because
the serialiser clears 5120 bytes and memcpys five short structs into it. Each loader
compares `*(u32*)block` against a literal version and falls back to `SetDefaults()` on
mismatch. Two internal identities make it self-validating on real data **[M]**:
`sum(PerMsoProgramCount16KBased[0..63])` equals the sum of the SLC, TLC and Metadata
`DataAreaProgramCount16KBased` totals **exactly**, which pins the base of the 64-entry
array, its length and the three totals at once; and `HostStatistics.DataUnitsWritten`
(raw 512-byte sectors) divided by 32 gives host 16 KiB writes.

The host's only view of any of this is NVMe Get Log Page LID 2 (SMART / Health
Information); there is no vendor opcode and no Set Features route that writes the blob
**[D]**.

---

# Part 3 — the RAID / stripe layer

RAID mode is per MSO, from `msoDescriptor + 16` **[D]**:

```
0  cRaidTypeNone      no parity at all
1  cRaidTypeMso       parity in a separate MSO
2  cRaidTypeInline    parity inside the WERU
```

The ParityData-group MSOs are themselves `cRaidTypeNone`, as they must be.

**Common to both modes:** the unit that is XOR-accumulated is the AU's **8 aux bytes
plus its 4096 user bytes** — the 4104-byte message, not the stored codeword **[D]**.
The ECU owns 26 SRAM RAID buffers, and the accumulate is a hardware operation over
that message. This is why an *aux* bit can be an XOR product (§5.5).

## 3.1 Inline RAID — solved and measured

Geometry: a 12-block WERU (configuration 2), twelve blocks over six consecutive CEs,
pseudo-SLC. **Eleven slots are WMA-addressable; the twelfth is not** — it has no WMA
at all, and slot 11 is `(ceBase+5, blockBase+1)`.

**A stripe is one AU index of one page across all twelve slots.** The four AU indices
of a page are four independent stripes, which matches the four ECU RAID buffers the
write descriptor selects.

The measured relation:

```
XOR over ALL TWELVE slots of AU(slot, page, au) == 0        per (page, au)
```

over the AU's 8 aux + 4096 user bytes. Grade **[M]**, on three independent 12-block
regions:

* the 12-way XOR is exactly zero on **847/847** complete stripes; **3388 AUs exact,
  0 differ**;
* leave-one-out reconstruction of any of the twelve from the other eleven:
  **5328/5328**;
* every held-out single-slot rebuild is byte-exact over all 18432 stored bytes except
  on pages carrying a raw NAND bit error, each of which differs by exactly one byte
  and satisfies the relation exactly after LDPC correction;
* it is **not a mirror** (equals another slot on 4 of 847 — what a coincidence looks
  like), **not a journal** (its aux `+0` values are XOR products, not addresses), and
  **not residue** (it is an exact function of the *current* stripe).

**The relation is symmetric**, and that is what makes it useful: nothing in the XOR
says which slot is "the parity", so any one of the twelve can be rebuilt from the
other eleven and recovery does not depend on the identification.

**There is exactly ONE relation — the load-bearing negative.** Two slots (0 and 11)
carry the aux parity flag on 164/164 decoded AUs each while the other ten carry it on
0..4 of 164, which invites "10 data + 2 parity, so two erasures are correctable". That
reading is wrong. Taking the twelve pages as vectors over GF(2) and computing the
**kernel** settles it: on the pages where the kernel is exactly one-dimensional the
sole relation is over all twelve slots. A genuine second parity would hold on every
page and force kernel dimension ≥ 2 everywhere. **One parity means one erasure. Two is
not a partial answer.** **[M]**

Why two slots are flagged, solved: there is **one marker per stripe and it is written
on a DATA member**; the parity member's copy is the accumulator's output, not a second
write. Measured over 847 pages **[M]**: slot 0 flagged 847/847; the number of flagged
*data* slots is **odd on 847/847**; and **slot 11's flag equals the XOR of the eleven
data slots' flags, 847/847, 0 exceptions**. That identity is forced by the twelve-way
XOR being exactly zero — were slot 11's bit an independent OR, the relation would fail
in byte 7, and it does not.

### Two layers, and when each is legal

The relation always holds at the **plaintext** layer (aux + user). It *also* holds
byte for byte over the **raw stored** bytes — MCRC and both EE3 parities included —
but only under a condition that must be **checked, not assumed**:

> The scrambler seed is a function of the row and of nothing else, so XOR-ing a set of
> stored AUs XORs their keystreams. The keystreams cancel exactly when **every row
> occurs an even number of times in the full stripe.**

For the 12-block shape each of the two block numbers occurs six times, so the
keystreams cancel; dropping any single member leaves that member's own row an odd
number of times and the stored-layer XOR reproduces it exactly. That is why a raw-byte
rebuild in this mode needs no seed, no ECC and no keys.

> **A test that looks decisive and is vacuous — do not repeat it.** EE3 is linear and
> both AU MCRCs run over *complemented* stored bytes, so for an **odd** number of
> operands the complement cancels and the XOR of any odd number of valid AUs has zero
> syndromes and a valid MCRC, stripe or no stripe. The aux is linear too, so such a
> XOR also always yields four consecutive LMAs. "I XORed eleven pages and got a clean
> codeword with consecutive LMAs" is evidence of nothing. Any subset test must use an
> **even** member count, or compare against bytes that are independently known.

## 3.2 Mso RAID — a stripe across a 48-block WERU

```
P = wmasPerPage = pageSizeBytes >> 12        = 4
C = columns     = 24 * 2                     = 48
P * C = 192                                  = WMA_STRIDE

stripeId          = (wma // (P*C)) * P
firstWmaOfStripe  = (stripeId // P) * P * C
column            = (wma // P) % C            == the WERU slot of §2.8
auIndex           = wma % P
stripeBaseWma     = wma & ~(P-1)
```

So **a stripe is one page across every column of the WERU: 48 data AUs and one parity
AU**, and the four AU indices of a page partition its 192 WMAs into four disjoint
stripes. `column` being identical to the FTL's independently-derived slot is the
cross-check that `P*C` really is the 192-WMA page window. Code rate **48:1** — per full
data WERU, 1104 parity pages (4416 parity AUs) back 211968 data AUs, exactly 48.0000.

**The parity does not live inside the data WERU.** A parity AU is an ordinary
LUT-addressed AU in a shared LMA space (the ParityData lutId), and **its LMA is the
stripeId**:

```
stripeLma(wma) = stripeId(wma) + (wma % P) = 4*(wma // 192) + (wma % 4)
parityWma      = lut[stripeLma >> 2] + (stripeLma & 3)      # granularity 4
```

Five independent things pin this **[D]** + **[M]**:

1. **The rebuild reads the stripeId out of the parity AU's own aux**: on the
   `auxLayout == 3` branch the "LMA" it hands back is bits [31:7] of the second dword
   of **AU 2** of the parity page, plus the AU index. `0x1FFFFFF` in that field is
   `INVALID_STRIPE_ID`, and invalidating a parity AU is `*p |= 0xFFFFFF80` — precisely
   "set that 25-bit field to all ones and leave the low 7 alone". **[D]**
2. **The write puts it there**, from a list the parity manager fills with consecutive
   stripeIds starting at `stripeId(wma)`. Write and rebuild are the same field read
   the same way. **[D]**
3. **The table is sized to the stripeId space, exactly**: on media the ParityData LUT
   region has granularity 4 and `n_entries` such that
   `4 * (972 * 211968 // 192) == maxLma + 1` and `(maxLma+1)/4 == n_entries`, with 972
   the same WERU count that sizes the valid map. **[M]**
4. **The media agrees with the table**: for every held parity page carrying the
   layout-3 aux shape, the stripeId in its own aux is the stripeId the table filed it
   under — **10 exact, 0 wrong**, plus 6 whose stripeId the table maps nowhere (a
   superseded parity AU, the ordinary state of a log-structured store). Controls: 0
   neighbouring stripes resolve to the same page, 0 other parity pages collide. A
   25-bit field agreeing is 1 in 2²⁵ per page. **[M]**
5. **Every live entry in the whole table is a parity AU**: all 15737 — 0 not
   granule-aligned, 0 target WMAs claimed twice, 0 landing outside a ParityData-owned
   WERU. And for all ten parity WERUs, the set of validMap bits is *exactly* the set
   of WMA offsets the table names there — 15737/15737 matched, 0 named-but-not-set, 0
   set-but-not-named — two separate on-media structures neither of which was derived
   from the other. **[M]**

**`cRaidTypeMso` is not per-partition parity.** There is one global parity LMA space
keyed by a globally computed stripeId; the several ParityData msoIds are *write
fronts* into it, not per-partition parity partitions. Classified over all 15737 live
entries, one parity WERU carries stripes for data WERUs owned by two different data
MSOs, and the relation is many-to-many in both directions, so **no data-msoId →
parity-msoId function exists** **[M]**. This retracts an earlier "contradiction" that
the msoIdTable's ParityData column names no parity MSO for the data MSOs — that column
was never the data→parity map; it names which ParityData write front an msoType
allocates from.

### What the Mso parity does and does not cover

The parity relation covers the **4096 user bytes**. Six of the eight aux dwords are
*also* the member XOR, because the write path copies all 32 aux bytes of the page
verbatim out of the accumulator and only then stamps AU2's and AU3's second dword:

```
parity AU_k dw0 == XOR of the 48 members' LMAs == 0  BY IDENTITY
parity AU0 dw1 == markerXOR << 31 == 0x80000000
parity AU1 dw1 == XOR of the 48 members' page sequences | markerXOR << 31
```

The dw0 identity needs no measurement: each CE contributes four consecutive columns,
which are one 16-aligned LUT granule, so those four LMAs differ only in bits 2 and 3
and each value appears twice. Census over 132 clean addressed parity pages: dw0 zero
on all four AUs 132/132; AU0 exactly `0x80000000` 132/132; AU1 exactly `0x80000000` on
124 and carrying low bits on 8, every one a multiple of 16, as a XOR over 48
consecutively-numbered pages must give. Predicted member by member on four stripes,
4/4 on all three quantities. **[M]**

AU2's and AU3's second dwords are **not** recoverable from the parity — they are the
stripeId and the sequence, stamped after the accumulate. So a rebuilt data AU's LMA
must come from the LUT and the stripe geometry, never from the parity.

**The raw-stored shortcut does NOT carry over to Mso mode.** A rectangular user WERU
gives each of its four block numbers 12 of the 48 data columns (even), but the parity
AU lives in a *different* WERU, so its row occurs exactly once and the raw XOR over the
49 members is not zero — it is the parity row's keystream. Reconstruction in Mso mode
therefore works at the plaintext layer only.

### What is not established, stated exactly

**The Mso XOR itself has never been verified on media, and is reported as SKIPPED,
never as PASS.** The *placement* is measured (points 1–5 above). The XOR is not, and
it cannot be from a drive with a dead die: a stripe is 48 columns with **one** parity
AU, and `slot = 4*ce + q` puts **four** columns on any single CE — four erasures
against a code that corrects one. Enumerated rather than argued: of 15737 live parity
entries, all 15737 name a data WERU with exactly four columns on the dead die, and 0
stripes sit at 0 or 1 erasure. Bad-block substitution moves the *block*, never the
*CE*, so substituted WERUs do not help.

The residual-entropy test that "looks decisive" was run and **retracted**: matched
parity gives residual entropy 7.9508 b/B and a deliberately wrong parity page gives
7.9560. Every readable byte is ciphertext, so "four AUs of plaintext" and "45
unrelated AUs" are the same distribution, and **no statistic of a ciphertext XOR can
discriminate**. Coverage is not the obstacle — a full stripe was read, 45 of 45 at
MCRC 4/4 — the erasure count is. **[?]**

---

# Part 4 — page layout

## 4.1 The raw page

```
page = 16384 data bytes + 2048 spare (OOB) = 18432 bytes, ONE FLAT COLUMN SPACE
```

That the column space is flat and contiguous across the nominal data/spare boundary is
**measured** **[M]**: a read starting at column `0x10` is byte for byte `bytes 0x10..`
of the column-0 read.

**The format uses every one of those bytes**, and the 16384 boundary is not a
structural boundary:

```
4 AUs x 4608 bytes = 18432          AU k at [k*4608, (k+1)*4608)
8 codewords x 2304 bytes            AU k is codewords 2k and 2k+1
```

so **AU 3 occupies bytes 13824..18431** and codeword 7 occupies 16128..18431 — the
nominal data/spare boundary falls *inside* codeword 7. There is no spare area in the
ONFI sense on this drive: no bad-block marker, no separate ECC region, no metadata
bytes. It is codeword. Measured consequence **[M]**: the first "spare" byte carries
scrambled user data on 4322 of 4334 written pages (99.72%), so the ONFI bad-block
heuristic marks nearly every written block bad. The FTL's own block map is the only
authority on bad blocks.

One structural inference, labelled as such: the LDPC error rate on deeply-programmed
pages shows a clean 6.5× step at offset 2048 *within every 2304-byte codeword*, flat on
either side, which reads naturally as the die's internal sector organisation being
8 × (2048 main + 256 spare). That is a **[?] reading of a [M] measurement**; it does
not change the flat column addressing above, and nothing in the format depends on it.

## 4.2 AU formats

Four AU configurations exist in the controller's own format table; three appear on
media.

| format | used by | layout | ECC | AU MCRC | scrambler |
|---|---|---|---|---|---|
| **A** | main media, and firmware groups 0/1 | 4096 user + 8 aux, **2 codewords/AU** | EE3, 246 B/CW | **6 bytes** | on |
| **B** | boot firmware images (group 2) | `812 + 812 + 812 + 816`, **4 codewords/AU** | A10R, 300 B/CW | **4 bytes** | on |
| **C** | raw / long | 4096 verbatim | **none** | none | **off** |
| **D** | RAID parity | as A | EE3, 246 B/CW | **4 bytes** | on |

Format D is a **read-path ECU setting and does not appear on media** — its
`META_SIZE = 20` is inert because every populated slot runs in AUX mode, and the
firmware's own computed transfer size confirms it
(`4600 = 4096 + 8 + 4 + 2*246`, with no meta contribution). The only on-media
difference between A and D would be the MCRC width. A reading of it as "A plus 20
bytes of META" would push a decoder 20 bytes out of alignment and is wrong. **[D]**

> **Do not key a decoder off the ECU's AU-format index.** Index-to-purpose is not
> stable across firmware revisions — one 2021 image puts the raw format at indices
> 18/19 and a later one at 20/21, with the same *set* of geometries. **Key off
> geometry**: a 4608-byte AU stride is format A/D, a 3252-byte one is format B.

### The ECU AU-format record

For completeness, the controller's per-format configuration is a **packed 16-byte
record**, 32 slots, read as a 128-bit little-endian integer **[D]**:

| bits | w | field |
|---|---|---|
| 0–4 | 5 | `index` (self-index) |
| 5 | 1 | `auxMetaDataMode` — 1 = AUX mode, 0 = META mode |
| 6–21 | 16 | `xferSizePerAu` — recomputed at init |
| 22–25 | 4 | `auxSize` |
| 26 | 1 | `fit2PhyPage` |
| 27 | 1 | `eccInvMode` |
| 28 | 1 | `mcrcInvMode` |
| 29 | 1 | `eccLdpcDis` |
| 30 | 1 | `eccDecBypassEn` |
| 31–32 | 2 | `eccItrCodeSel` — 0 = EE3 (246 B/CW), 1 = A10R (300 B/CW) |
| 33 | 1 | `mpeccEn` |
| 34–35 | 2 | `ecuHlbaMode` |
| 36–37 | 2 | `ecuHlbaStripMode` |
| 38 | 1 | `ecuHlbaInsertOption` |
| 39 | 1 | `bcrcEn` |
| 41–54 | 14 | `hostSectorSize` |
| 55–64 | 10 | `ecu2HostRatio` |
| 65–74 | 10 | `metaSize` |
| 75 | 1 | `scrMetaInvEn` |
| 76 | 1 | `scrUserEn` |
| 77–79 | 3 | `scrUserSeedSrc` — 0 zero, 1 register, **2 PBA**, 3 PBA^MASK, 4 PBA^~MASK, 5 AUX/META |
| 80–82 | 3 | `cwPerAu` |
| 83 | 1 | `auMcrcEn` |
| 86 | 1 | `auMcrcSizeOpt` — **0 → 4-byte MCRC, 1 → 6-byte MCRC** |
| 89–103 | 15 | `phyPageSize` |
| 104 | 1 | `metaMcrcEn` |
| 105–114 | 10 | `threshold` — per-codeword corrected-bit warning limit only |
| 116–127 | 12 | `eraseThreshold` |

Transfer size, validated against media **[D]**:

```
xfer = hostSectorSize*ratio + 4*bcrcEn*ratio + hlbaBytes
     + (auxMetaMode ? auxSize : metaSize)
     + auMcrcEn*(2*auMcrcSizeOpt + 4) + 4*metaMcrcEn
     + cwPerAu * (eccLdpcDis ? 0 : ldpcParity[eccItrCodeSel])
xfer += xfer & 1
```

For format A that is `4096 + 8 + 6 + 2*246 = 4602`, against **4608 stored** — the
extra 6 being the LDPC pipeline tail (§4.3). For format B,
`2048 + 4 + 4*300 = 3252`, which the firmware also hard-codes as that format's
`PHY_PAGE_SIZE`.

Two bits in the record have no name in any header, are set in every populated slot,
and are also set by the boot ROM, so they are almost certainly controller-specific
enables — but their meaning is **[?]**.

## 4.3 Format A — the AU byte map

One 4608-byte AU, `b0` = the even 2304-byte codeword, `b1` = the odd. Measured on 1424
AUs and closed by a byte-identical re-encode of all 4608 bytes **[M]**:

| span | n | role | logical (keystream) index |
|---|---|---|---|
| `b0[0:8]` | 8 | **aux** (scrambled) | `logical[0:8]` |
| `b0[8:2056]` | 2048 | user 0..2047 (scrambled) | `logical[8:2056]` |
| `b0[2056:2302]` | 246 | **EE3 parity, even codeword** | — |
| `b0[2302:2304]` | 2 | user 2048..2049 (scrambled) | `logical[2056:2058]` |
| `b1[0:2046]` | 2046 | user 2050..4095 (scrambled) | `logical[2058:4104]` |
| `b1[2046:2052]` | 6 | **AU MCRC, stored unscrambled** | `logical[4104:4110]` |
| `b1[2052:2298]` | 246 | **EE3 parity, odd codeword** | — |
| `b1[2298:2304]` | 6 | verbatim copy of `b1[0:6]` — the **LDPC pipeline tail**, in no codeword | — |

So the AU's **logical span is 4110 bytes** — `MSG_BYTES = 4104` of message (8 aux +
4096 user) followed by the 6-byte MCRC — and the user payload is *split around the
even codeword's parity*: `user = b0[8:2056] ++ b0[2302:2304] ++ b1[0:2046]`
= 2048 + 2 + 2046 = 4096.

The `au_logical` transform is exactly:

```
logical = b0[0:2056] ++ b0[2302:2304] ++ b1[0:2052]          (4110 bytes)
```

Three consequences a reader must respect:

* **A reader that reorders anything is fatal.** The odd codeword's information field
  starts *inside the even block*.
* The **last 6 bytes of every odd codeword** are a byte-exact copy of its first 6 — one
  Z = 48 circulant — on **1424 of 1424** odd codewords. They are a pipeline flush
  artefact, in no codeword, not in the logical stream, and an exact re-encoder must
  write them explicitly.
* **The MCRC field is not scrambled and is not covered by parity**; it is part of the
  odd codeword's *message*.

## 4.4 Format B — the boot-firmware AU

```
codeword  512 payload + 300 A10R parity           =  812 B
AU        4 codewords, with a 4-byte MCRC          = 3252 B   (812 + 812 + 812 + 816)
page      5 AUs                                    = 16260 B
          bytes 16260..18431 are erased 0xFF       (verified on all 368 rows of a block)
user      20 x 512                                 = 10240 B per page
```

The AU is **`812 + 812 + 812 + 816`, not `4*812 + 4`**: the fourth frame is
`512 payload + 4 MCRC + 300 parity`, so the MCRC sits at **AU+2948**, immediately
*before* parity 3 and inside codeword 3's message. **[M]**

Payload run map, for `k = 0..19` per page:

```
PAGE_RUNS[k] = ( page_off = k*812 + (k//4)*4 , ks_index = (k%4)*512 ,
                 payload_off = k*512 , length = 512 )
```

The erased tail at 16260..18431 is also a free die-identification primitive: a format-A
page uses all 18432 bytes, so the tail alone separates the boot region from ordinary
content with no seed and no decode **[M]**.

---

# Part 5 — the aux field

Every AU carries 8 bytes of metadata outside the user payload. They are the only
self-check the whole chain has: **an AU is verified when its own aux LMA equals the
LMA that was looked up**, and that one equality exercises the LUT entry, the WMA
arithmetic, the WERU slot rule, the page address, the cell mode, the scrambler seed
and the ECC at once, because any of them being wrong lands on an AU whose aux says a
different number.

**MCRC is recorded but is not that proof.** It is computed over the complemented
stored bytes, so it attests that the page came off the die intact and says nothing
about whether the address that produced it was the right one.

## 5.1 The selector

`Mso_GetAuxLayout(msoId)` is **`msoDescriptor` byte +17**, 28-byte stride. **The set
{0,1,2,3} is closed by the firmware, not by observation**: the writer's switch panics
on anything else **[D]**.

| value | firmware name |
|---|---|
| 0 | `cAuxLayoutForInternalData` — the `default:` arm; the constant is proven to be 0 by a guard of the form `if (auxLayout) panic` |
| 1 | **no name exists in this firmware image.** There are exactly three `cAuxLayout*` strings across all four cores and none is layout 1. **[?]** — deliberately not guessed |
| 2 | `cAuxLayoutForForNSID23` |
| 3 | `cAuxLayoutForRaidParityMso` |

Reading a field with the wrong layout's rule does not produce a detectable error — it
produces a plausible wrong answer — so the layout has to come from the descriptor and
never from a guess.

Both dwords are little-endian: `dw0` at +0, `dw1` at +4.

## 5.2 The one structural fact

**The sequence number is a PAGE field, not an AU field.** Every layout stamps one
62-, 64- or 39-bit counter value across the **four** aux entries of a page; the write
loop advances by `auPerPage = 4` **[D]**. This retracts an older model in which "AU1
carries the sequence in bytes 4..6 as a u24" — that read is a silent truncation which
starts losing the top of the counter at 2²⁴.

## 5.3 Layouts 0 and 1 — identical on media

| offset | width | field |
|---|---|---|
| +0 | u32 | **LMA** |
| +4 | u31 (dw1 bits 30..0) | **sequence shard** |
| +7 bit 7 (dw1 bit 31) | 1 | **RAID parity marker**, sticky |

Page reassembly **[D]**:

```
AU0 shard = seq >> 31        AU1 shard = seq & 0x7FFFFFFF       AU2 = AU3 = 0
sequence  = ((aux0.dw1 & 0x7FFFFFFF) << 31) | (aux1.dw1 & 0x7FFFFFFF)     # 62 bits
```

Writer and reader agree bit for bit, which is what makes this a decode and not a fit.
Bit 31 is masked out of every read-modify-write of that word and stripped by every
reader, so it is a **sticky per-AU flag that survives rewrites**. Its only writer's
call sites both require `raidType != cRaidTypeNone`, so it fires on **data** writes;
measured over 5632 layout-0 pages, every flagged page is on a RAID-enabled msoId and
none of the 2992 `cRaidTypeNone` pages carries it **[M]**.

**The two layouts differ only in what happens to an unmapped AU** **[D]**: layout 1
tests `lma == 0xFFFFFFFF` first and, if so, zeroes the shard on all four AUs *without
consuming a sequence number*; layout 0 has no such test and stamps every page.

Layout 0 has one further special case: for `msoType == 6` (PhysicalMediaAccess) it
re-homes the descriptor it reads using **AU0's LMA as a two-valued tag** — `0xFFFFFFFE`
selects one msoId and `0xFFFFFFFC` another — and anything else panics. **So on such a
page AU0's LMA is not an address at all.** On media: 400 such pages, split between the
two values, with AU1/AU2/AU3 all `0xFFFFFFFF` and an ordinary sequence in AU1, and no
third value **[M]**. This is the trap in torn-page detection: those pages have AU0 and
AU1 disagreeing on the invalid-LMA test and are not torn writes, so a naive XOR test
reports 400 false positives.

## 5.4 Layout 2 — `cAuxLayoutForForNSID23`

| offset | width | field |
|---|---|---|
| +0 | u32 | **LMA** (partition-local) |
| +4 | u16 | **`lpId`**, the logical-partition id |
| +6 | u16 | **sequence shard** |

Page reassembly, **big-endian in the AU index** **[D]**:

```
AU0 = seq[63:48]   AU1 = seq[47:32]   AU2 = seq[31:16]   AU3 = seq[15:0]
```

**The shard order rests on the reader alone and is not claimed as measured.** Every
layout-2 sequence on the reference capture is below 65536, so AU0/AU1/AU2's shards are
all zero and only AU3's position is demonstrated. The selftest reports this SKIPPED
rather than PASS. **[D], not [M].**

Two traps:

* **Layout 2 has no room for the parity flag**: +7 is the top half of AU0's u16 shard.
  The collision is real — the flag-setting routine is gated on the RAID *opcode*, not
  on the raid type, and its callers cover `cRaidTypeMso` as well as inline, so a
  layout-2 AU0 can receive it and would read as `shard | 0x8000` rather than as a
  flag. **[D]**
* **Layout 2 does not increment the sequence pool.** It stamps the pool's current
  `next` value, so a layout-2 sequence **names a write epoch, not a per-page counter,
  and must not be used for newest-wins ranking** the way a layout-0/1 sequence is.
  Measured: two different MSOs' clean pages each read exactly their group's pool
  `next` **[M]**.

## 5.5 Layout 3 — `cAuxLayoutForRaidParityMso`, a page-level record

| where | width | field |
|---|---|---|
| AU2 +4, bits 31..7 | u25 | **stripeId**; `INVALID_STRIPE_ID = 0x1FFFFFF` |
| AU2 +4, bits 6..0 | u7 | sequence bits 38..32 |
| AU3 +4 | u32 | sequence bits 31..0 |
| AU0 +7 bit 7, AU1 +7 bit 7 | 1 | RAID parity marker, **arriving as an XOR** |
| all four `+0`, and AU0/AU1 `+4` bits 30..0 | | **not written by the aux writer** — carried verbatim out of the ECU RAID accumulator |

`page sequence = ((AU2.dw1 & 0x7F) << 32) | AU3.dw1`, a **39-bit** value. This was
settled from the instruction encodings, and the finding is that **the firmware's own
reader is buggy**: the writer loads the high shard with a plain `LDRB` from byte 4 of a
counter its own `ldrd/adds/adc/strd` proves is a u64, then `BFI #0,#7`, so no shift is
dropped; the reader is a clone of the layouts-0/1 arm 44 bytes earlier — the same
instructions minus two `bic` masks that make *its* low part 31 bits wide — and so it
shifts by 31 and double-counts bit 31. The two agree below 2³², which is why no media
answer is possible. **[D]**

There is **no LMA array on a layout-3 page**, and that is the important structural
fact: the four `+0` dwords are XOR products, not addresses.

The carried dwords are solved, not unknown (§3.2). What remains **[?]** is the
residual low bits of AU0/AU1's second dword beyond the marker and the sequence XOR;
the code reports them rather than naming them.

## 5.6 Sentinels

`LMA_SENTINELS = (0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC)` **[D]**.

`0xFFFFFFFF` is the invalid LMA — pad entries, LUT miss, explicit invalidation. The
writer's pad AU is exactly `ff ff ff ff 00 00 00 00`, so **`lma == 0xFFFFFFFF` is not
the same as an erased aux** (all eight bytes 0xFF). `0xFFFFFFFE` and `0xFFFFFFFC` are
the layout-0 msoType-6 redirect tags and the trim-log markers.

**`0xFFFFFFFB` is UNKNOWN** **[?]**: a real value on media — one page, all four AUs, an
ordinary sequence, the parity flag set, so neither a torn write nor a bad decode — with
no site anywhere in the four decompiled cores that reads or writes it.

## 5.7 The acceptance rule

The layout must come from `msoDescriptor +17` **first**; the rule is then per layout,
and it returns `True` / `False` / **UNKNOWN**. **Reject only on an explicit `False`** —
treating UNKNOWN as rejection is the original bug.

| layout | rule |
|---|---|
| 0, 1, 2 | the four LMAs are consecutive (`n, n+1, n+2, n+3`) **or** all four are sentinels. Some sentinels *and* some real addresses is refused — that is a torn page. |
| 3 | accepted iff AU2 carries a real stripeId. Equivalently: `all four carried dw0 == 0` iff `stripeId != INVALID_STRIPE_ID` — agrees on **83 of 83** clean layout-3 pages, zero split. |

> **The retraction that cost real data.** The blanket "four consecutive LMAs" test
> rejects **100%** of layout-3 pages, because the writer never writes an LMA array on
> layout 3. That withheld 82 parity pages which had been read 246 times with MCRC 4/4
> on every read.

The layout-3 rule is explicitly **not a biconditional**: pages exist that are
*accumulated* (all carried dw0 zero, AU0 and AU1 both exactly `0x80000000`) yet
*unaddressed* (AU2 = `INVALID_STRIPE_ID`) — two objects with two lifetimes, the ECU
accumulator and the parity manager's stripeId list. The rule still refuses them,
because they are unaddressable, not because their bytes are suspect. **[M]**

**Torn-page detection is separate, and it is something MCRC cannot see** — MCRC is per
AU over the stored bytes and passes on a page whose four AUs were written in different
commands. The rule is: exactly one of AU0/AU1 carries `0xFFFFFFFF`, **and** neither is
one of the other three sentinels. **[M]**

Evidence for the whole aux model: every clean page re-encodes byte-identically from
the decoded fields over all 32 aux bytes — **5723 / 5723** for layouts 0/1/2 **[M]** —
with 2000 random-aux pages per layout rejected as a negative control **[C]**. Layout 3
is excluded from that claim on purpose: its undecoded bytes are carried through
verbatim, so its re-encode would prove nothing.

## 5.8 Region-specific aux behaviour

The aux does not always carry an LMA array:

* **The firmware group 0/1 region** (blocks 6..9) is format A but is addressed by raw
  CE/block/page rather than through LMAs, and its aux carries **no LMA array** —
  measured, 44 of 44 MCRC-clean pages carry four *equal* non-LMA dwords **[M]**. The
  consecutive-LMA crib does not apply there and must not be used to reject such a page.
  Word 0 of that aux is a per-group firmware program count, bumped on each reprogram
  and compared by **equality, never ordering**; being constant per group is exactly why
  it works as a descrambler crib.
* **RootStorage AUs** carry `0xFFFFFFFF` in the LMA field, as above.

---

# Part 6 — integrity: the AU MCRC

## 6.1 Two widths of one construction

Both variants are a plain, unreflected, MSB-first CRC over the AU's **stored
(scrambled)** bytes, with every byte **complemented** as it is clocked in, init 0, no
final XOR. Both generators factor as **`(x+1) × primitive`** — the textbook
maximal-length inner LFSR times `(x+1)` — one vendor construction at two widths.

| | **6-byte** (format A) | **4-byte** (format B, and format D) |
|---|---|---|
| `AU_MCRC_SIZE_OPT` | 1 | 0 |
| width | 48 bits | 32 bits |
| generator | `0x1800000000063` = x⁴⁸+x⁴⁷+x⁶+x⁵+x+1 = `(x+1)(x⁴⁷+x⁵+1)` | `0x814141AB` = **CRC-32Q / CRC-32-AIXM** = `(x+1)·0x80C0C099`, the cofactor irreducible and primitive |
| message | **4104 stored bytes** = aux 8 + user 4096, i.e. `logical[0:4104]` | **2048 stored bytes** = 4 × 512, `raw[AU + j*812 : +512]` for j = 0..3, concatenated |
| field | `logical[4104:4110]` = `b1[2046:2052]` = **AU+4350** | **AU+2948** = `chunk*3252 + 2948` |
| per page | **4** (one per AU) | **5** (one per AU) |

The ECU's own arithmetic agrees: `auMcrcBytes = auMcrcEn * (2*AU_MCRC_SIZE_OPT + 4)`
gives 4 and 6 **[D]**.

Parameters, in `reveng` terms, *after* the input bytes are complemented:

```
width=48|32  poly=<above>  init=0  refin=false  refout=false  xorout=0
register form: AUGMENTED / shift-in,  crc = rem( ~msg(x) * x^width , p )
implementation: c = (c << 8) ^ T[(c >> (width-8)) ^ b]
digest stored BIG-ENDIAN
```

**The one genuinely external vector in the whole project**: `crc32q("123456789") ==
0x3010BF7F`, the published CRC-32Q check value, which confirms polynomial, bit order,
init and reflection from outside **[M, external]**.

## 6.2 Why "complemented stored bytes" is not a fudge factor

The decisive identity, measured **[M]**:

```
crc32q(msg)        ^ field  ==  0x8D4F75D1     (one constant, over every AU)
crc32q(0xFF * 2048)         ==  0x8D4F75D1     (identical)
crc32q(~msg)       ^ field  ==  0x00000000
```

The residual for the uncomplemented message is *exactly* the CRC of an all-ones
message of the same length — which is what "clocked in complemented, init 0, no final
XOR" means, and that form generalises to any message length while the constant does
not. The 48-bit variant shows the same thing.

The complement is on the **input data, not the digest**: `MCRC_INV_MODE = 0` in every
populated format slot, and there is no final XOR **[D]**.

## 6.3 Why it needs no key

Three properties, each measured:

* **The message is the STORED (scrambled) image, not plaintext.** Discriminated on
  fill rows that repeat with plaintext period 8 while their keystreams differ: over 660
  (row, row+8) AU pairs — same plaintext 660/660, same raw bytes 0/660, same digest
  0/660. And `crc(~plaintext) ^ field` takes **369 distinct values** (one per row)
  against **1** for the stored bytes. **[M]**
* **No address input.** One and the same constant explains every AU on every row and
  every CE. **[M]**
* **The field is excluded from its own message**, and **parity is not covered.** Of
  nine single-bit disagreements found by comparing replicas, the MCRC flags all four
  that land in payload and none of the five that land in LDPC parity. Negative control:
  `payload+parity` as the message gives 1640 distinct residuals instead of 1. **[M]**

So a raw page can be integrity-checked **the instant it comes off the media** — no
scrambler seed, no plaintext, no key, no ECC decode. That also states the limit
plainly: **the MCRC guards against corrupted bytes but not against a misdirected
read.** MCRC 4/4 does not prove you read the page you asked for, or that you read it
in the right cell mode.

## 6.4 Verification

| variant | result |
|---|---|
| 4-byte | **9196 / 9200** AU instances exact. Generator fitted from 4 AU pairs (20 AUs); the strict hold-out of the other 9180 verifies 9176. The four failures each differ from the majority copy in exactly one byte, one bit, and each verifies once that bit is taken from the majority; one of them is the same payload bit the A10R LDPC independently locates. **[M]** |
| 6-byte | **1419 / 1424** non-erased AUs; the five exceptions cross-check as single-bit media errors the LDPC independently flags in the same AU. **[M]** |

Negative controls for the 4-byte variant, each giving distinct-residual counts instead
of 1 **[M]**: descrambled plaintext 369; `payload[0:1024]` 1640; payload+parity 1640;
message byte-reversed 1640; field read at the wrong offset 1640.

## 6.5 How the generator was found, and two traps

Polynomial GCD, not search. For any polynomial CRC of fixed-length messages, two AUs
differing by `D` whose digests differ by `E` satisfy `p(x) | D(x)·x^w + E(x)`, and
**init, final XOR, the bytewise complement and the message length all cancel**, so the
test prejudges nothing. A 432-configuration sweep (6 message spans × 6 message orders
× 6 digest orders × 2 register forms) returns exactly one polynomial of degree ≥ 8, and
it is degree exactly 32. **[M]**

Two traps, recorded so they are not hit again:

* pairs must be drawn from **high-entropy** rows. With pairs from fill rows,
  `gcd(D_i)` alone is degree ~22 and the sweep reports a dozen bogus hits in every
  ordering.
* pairs must be taken **across rows**: two AUs of the same page share a keystream, so
  their scrambled and descrambled differences are identical and the GCD cannot separate
  the scrambled-versus-plaintext hypotheses.

## 6.6 Retractions and open items

* **Retracted in full**: an earlier note concluding that the 4-byte digest "is not a
  classical polynomial CRC … likely a wide-datapath hardware digest whose bit ordering
  is not a byte permutation." That analysis read the field at AU+3248, which is the
  **last 4 bytes of codeword 3's LDPC parity** — also GF(2)-linear in the message,
  which is why it looked self-consistent. Re-run at AU+2948, the very same GCD test
  returns the generator from the first four pairs. There was never a wide-datapath
  mystery. Also retracted from the same note: "the digest is covered by the last
  codeword's LDPC parity" — it is not covered by parity, it **is part of codeword 3's
  message**.
* **[?] Why the input is complemented.** Format A's media additionally stores the
  bitwise complement of the codeword; the tidy explanation (the ECU computes an
  ordinary CRC over its internal pre-inversion data and the media inverts on the way
  out) stays a hypothesis, because for A10R the raw stored bytes are already a valid
  codeword and a global inversion is unobservable.
* **[?] Format D's 4-byte MCRC.** No media in that format exists. `0x814141AB` over
  that format's message is the obvious first guess, untested.
* **[?] `MCRC_INV_MODE = 1`** is never exercised by any populated slot; its effect
  (presumably a final complement of the digest) is inferred from the register name only.

---

# Part 7 — ECC

Two LDPC codes are used on media. Both are quasi-cyclic over GF(2) with circulant size
**Z = 48**, both store the **complement** of the codeword, and both have their parity
computed over the **stored, i.e. scrambled** bytes.

> ### A correction the code forces, and it matters to any third-party reader
>
> Several notes in this tree state that *"EE3 parity covers the DESCRAMBLED bytes"*,
> and one of them goes further and instructs "descramble first, then ECC". **The
> verified code does the opposite**, and the code is the artefact that reproduces
> media: the encoder computes both EE3 parities from the message *after* scrambling
> and the result is byte-identical to media on 4/4 AUs of a real page; the decoder
> corrects the **stored** page and only then descrambles; the ring encoder regenerates
> media parity byte-exactly from raw pages. So **both codes' parity covers the stored
> scrambled image, and neither needs a scrambler seed in order to decode.** The stale
> claim also survives in one module's "raw media is NOT a codeword" note, which
> predates the layout being pinned. Treat the operational rule as: **ECC first,
> descramble second, for every region.**

## 7.1 EE3 — the main-media code

| | value |
|---|---|
| lifting size Z | **48** |
| base matrix | **41 × 385** block rows/columns, **2861 circulants** |
| row weight | 70 (×32), 69 (×9) |
| column weight | **18** on block columns 0..71, **5** on columns 72..384 |
| mother codeword | 385 × 48 = 18480 bits = **2310 B** |
| mother information | 344 × 48 = 16512 bits = **2064 B** |
| parity | 41 × 48 = 1968 bits = **246 B** |
| mother rate | 1 − 41/385 = **0.89351** |
| shortening | **one 48-bit information block column (mother column 343)** forced to zero and not stored → on-media codeword 384 × 48 = 18432 bits = **2304 B** |
| H rank | full 1968 — the parity half is a *unit*, so H **defines** the code; no branch bit, no convention |

Ring form: with `R = GF(2)[x]/(x⁴⁸−1)`, split H as `[A1 | A2]` at block column
`NINFO = 344`; `A1` is 41 × 344 (2656 monomial entries) and `A2` is 41 × 41 (205
entries, 5 per row). Encoding is closed form:

```
p = A2^-1 . A1 . m
```

`R` factors as `x⁴⁸+1 = ((x+1)(x²+x+1))¹⁶`, i.e. a product of exactly two local rings,
so Gauss–Jordan pivots must be **units** (`gcd(a, x⁴⁸+1) = 1`), not merely non-zero.
For EE3 every pivot is a unit on the first try — **0 fixups** — and `A2⁻¹` is 41 × 41
ring elements recomputed from H at run time. Its bitwise expansion reproduces a
retired 1968 × 1968 GF(2) inverse **row for row, 1968/1968** **[M]**.

**On-media codeword mapping** — the two codewords of an AU **[M]**:

```
even codeword:  information = ~b0[0:2056] ++ 8 zero bytes      (shortened by 8)
                parity      = ~b0[2056:2302]
odd  codeword:  information = ~b0[2302:2304] ++ ~b1[0:2052] ++ 10 zero bytes
                parity      = ~b1[2052:2298]
b1[2298:2304]:  the pipeline tail -- in NO codeword
```

The odd codeword's information field **starts in the even block**. A control confirms
the two cross-block prefix bytes are load-bearing: replacing them with a constant gives
0 zero syndromes of 148 and mean weight 135 **[M]**.

Coverage: 2304 + 2298 = **4602 of 4608 stored bytes per AU are ECC-covered**. The only
unprotected stored bytes are the 6-byte tail — 24 bytes per page.

**Bit conventions, both codes** **[M]**:

* a block column is **6 consecutive bytes, MSB first** (byte 0's MSB is bit t = 0);
* `H` block (i, j) = `P^s`, and **check row r of layer i touches bit `(r+s) mod 48` of
  block column j**;
* the codeword is the **complement of the stored bytes**. An erased page (all 0xFF)
  complements to the zero codeword, so it passes the syndrome and must be excluded by
  an erase test, not by ECC.
* **bit-order trap**: in the ring representation, bit `k` of the 48-bit block integer is
  the coefficient of `x^k`, i.e. the polynomial index runs *opposite* to the on-media
  MSB-first order (frame bit `t` ↔ coefficient `47 − t`). Two bases coexist in the
  literature on this code and mixing them fails most layers.

**Table format** (the schedule as it appears in vendor source and in a DRAM image)
**[D]**, verified by decoding **[M]**: 16 × u16 header, then a stream of 2-u16
entries; `code = colhi<<8 | delta`; `col = ((flag & 1) << 8) | colhi` (9-bit block
column); `flag` bit 3 = end of layer. **The 6-bit low field is a DELTA rotation, not
the circulant exponent**: `s(i_k, j) = (d₁ + … + d_k) mod 48` over the layers
containing column j. Per column the deltas sum to **0 mod 48** — verified 385/385
columns, which is what makes a layered decoder's column memory return to its original
rotational frame.

## 7.2 A10R — the boot-firmware code

| | bits | bytes | blocks |
|---|---|---|---|
| mother codeword | 6576 | **822** | 137 × 48 |
| parity | 2400 | **300** | 50 × 48 |
| mother message | 4176 | 522 | 87 × 48 |
| **stored** frame | 6528 | **816** | 136 × 48 |
| stored message | 4128 | 516 | 86 × 48 |

* base matrix over the stored frame is **50 × 136 = 816 circulants**, every entry a
  single `P^s`;
* **column weight exactly 6 on all 136 stored block columns**; row weights 15 (×3),
  16 (×28), 17 (×19); 816 = 136 × 6;
* block columns 0..85 are an information set — the code is systematic there;
* **one 48-bit message block column is shortened and not stored**; the silicon encoder
  fixes it at mother block column 86, and its fill is **zero** in the mother codeword;
* rate: stored 4128/6528 = **0.6324**.

**`rank(H) = 2399 of 2400`** — all 2400 rows sum to zero (the even-column-weight
dependency). H therefore **verifies** A10R media without **defining** the code: every
message admits exactly **two** parities differing by the kernel, and both are genuine
codewords. Which one the silicon emits is **one bit of media evidence and is proven
underivable from H**. Do not go looking for a formula. **[M]** for the rank, **[D]** for
the underivability.

That one bit is stated two equivalent ways, and both are checked on **8218/8218** clean
media codewords off five dies **[M]**: the silicon path is "bit 47 of the accumulator's
first word is clear"; the fall-back is a 72-byte covector supported entirely on parity
block columns 0..11 with `<W, P> = 0`, unique because the affine span of those parities
has GF(2) rank 2400 of 2401. W's support landing exactly on the 12 gap blocks was not
assumed — it was fitted over all 2400 parity bits and came out zero on the other 38,
which is how the gap set was first named from data.

**A10R is an affine coset, and the offsets are not data.** `H·c = s(slot)`, with slots
0/1/2 sharing one constant and slot 3 another, measured with zero variance over
1839–1840 codewords per slot. The constants are computed from H alone:
`off[3] = H·1` and `off[0..2] = H·(1 with frame bytes 512..515 zeroed)` — i.e. the
codeword is simply the complement of the stored frame, with the MCRC field at `0x00`
rather than `0xFF` where it is not carried. **[M]**

**Frame layout** used to put all four slots in one code:

```
frame[  0:512]   stored payload
frame[512:516]   the AU MCRC for slot 3 / ZERO for slots 0..2
frame[516:816]   300 parity bytes
```

Encoding is a **Richardson–Urbanke** solve with **gap = parity block columns 0..11**:
syndrome `s = A1·M` plus the per-slot offset; RU peel of `A2` with residual 0 and
monomial (unit) pivots, so no inverse table is needed; a 12 × 12 Schur complement `phi`
reduced by Gauss–Jordan with unit pivots, where eleven columns take one and the twelfth
residual has `(x+1)`-valuation exactly 1, so the last variable is resolved by the
`(1+x)` pseudo-inverse — a **serial accumulator**. That single step is the whole of the
encoder's non-ring behaviour, and it is forced: `rank(phi) = 575 of 576` for every valid
gap set, so no 12 × 12 ring matrix can do the solve.

## 7.3 A second, independent construction from silicon

Each code has a second construction read straight out of the controller's own ECU
config images, and the two agree byte for byte **[M]**. The decoder image gives H split
in Richardson–Urbanke form `[T B; E D]` (gap = the last 12 encoder rows, table parity
column *i* ↔ H parity column `(i+12) mod npar`), with `T` lower-triangular with a
monomial diagonal, and `phi = D + E·T⁻¹·B`. The encoder image's **144-record tail, read
column-major and reciprocal, is the gap-solve matrix `Q`**.

What `phi · Q` equals is **measured, never assumed** — it is decided by the code's
parity block-incidence nullity:

| code | nullity | result |
|---|---|---|
| EE3 | 0 | `phi` is invertible, the hardware stores `Q = phi⁻¹`, and **`phi·Q = I`, 144/144**. EE3 carries **no convention at all**. |
| A10R, and the third silicon code | 1 | no ring `Q` can satisfy `phi·Q = I`; the hardware stores a `Q` with `phi·Q = S`, where `S[0][0] = x`, `S[0][11] = 1`, `S[i][i] = S[i][i−1] = 1`, `det S = x+1` — byte-identical in both codes, nothing fitted. |

The impossibility theorem ("no ring `Q` satisfies `phi·Q = I`") **still stands** and is
precisely *why* `S` exists; the earlier error was concluding "therefore not stored"
from "therefore not an inverse".

**Encoder-image record format** **[M]**, measured by streaming `0xFFFF` into a writable
bank and reading back the implemented-bit mask: a 32-word header, then 5-word records
with implemented mask `ffff ffff 00ff ffff ffff` — **an 8-bit hole at record bits
24..31**, which is why every earlier sweep for six *contiguous* 12-bit fields failed.
Entries are 12-bit slots (`entry = (shift << 6) | row`, 6-bit row, 6-bit shift,
blank = `0x03F`); a mother column consumes `6·ceil(w/6)` slots, so weight-6 codes
collapse to one 6-slot record per column. The stream is 14 preamble slots, then six
entries per mother block column, in the order: message columns, then parity columns
12..npar−1, then parity columns 0..11.

## 7.4 How many codes there are

**Six are documented; two are on media.** Three live in Titania silicon — **EE3**
(main media), **A10R** (boot firmware images) and a third code in two ECU banks used by
no known region. Three more ship as literal hex reference tables in the vendor's
LDPC schedule database source, each with an encoder and a decoder macro; those are
vendor reference codes, not something this controller writes. They matter only because
six independent images are what pinned the encoder-header format *structurally* rather
than by curve-fitting three.

**A third-party reader needs only EE3 and A10R.**

Neither Titania code's schedule table exists in any *firmware* image — a ~2 GB,
~48900-file sweep found none — but both are readable out of **silicon**, from the ECU's
config memory. This is the same story as the scrambler seed map: hard-wired, must be
derived from media or from silicon, and there is no table in an image to dump.

## 7.5 Decoding

**EE3 — hard decision only** **[M]**. No soft reads are plumbed out of the NAND, so
channel LLRs are ±1 (a BSC, where the magnitude factors out and only the sign matters).
Two stages: parallel Gallager-B bit flipping (flip every free bit whose unsatisfied
check count exceeds half its column weight, ≤ 12 iterations, with an oscillation
cut-out), then normalized min-sum on the hard decisions, flooding schedule, **α = 0.45**,
≤ 80 iterations. **64 mother bits are structurally known and must be pinned** (the
shortened block column's 48 bits plus the 16 bits of the implied-zero information
bytes) — letting the decoder move them produces a "converged" word that maps back to no
2304-byte block.

Measured performance **[M]**: ≥ 99% exact to **160 flipped bits per 2304-byte block
(0.87% raw BER)**, waterfall at ≈ 170 syndromes; 100% to 128; 0% at 192. The BSC
capacity of a rate-0.894 code is 1.25% BER, so this is about 70% of Shannon.
Miscorrection was never observed in about 3000 decodes including 700 at ≥ 176 — a
measurement, not a proof.

Negative results worth publishing **[M]**: the textbook α = 0.75–0.8 **fails** here
(0/20 at 176 flips; the plateau is 0.4–0.5); multi-α retries buy nothing because the
failures coincide; doubling the iteration limit moves 16/20 to 17/20; penalising
already-moved bits makes bit flipping strictly worse.

**A10R** ships no iterative decoder: engagement is a syndrome check plus single-bit
location. A syndrome of weight 6 — the column weight — matches exactly one H column,
and flipping that bit zeroes the syndrome. Two real media errors were found and fixed
this way.

**"Correction needed" at page level**: a page decode returns
`(page, bits_corrected, all_converged)`, and the layer above distinguishes `verified`
(syndrome clean as read) from `corrected` (LDPC was needed) from `failed`, treating
`erased` as an observation rather than a missing value. A zero syndrome *after*
decoding is weaker evidence than a zero syndrome on a raw read.

## 7.6 What a byte-exact re-encoder must reproduce

* **EE3**: `b1[2298:2304]` — a byte-for-byte duplicate of `b1[0:6]`, 6 bytes per odd
  block, 24 per page, in no codeword and not in the logical stream. It must be written
  explicitly.
* EE3 shortening pads that are never stored: 8 zero mother bytes for the even codeword,
  10 for the odd. Feed them as zeros.
* **Encode order matters for EE3**: even codeword first (it writes `b0` parity), then
  odd (it *reads* `b0[2302:2304]` as information and writes `b1` parity plus the tail).
* **A10R**: mother message bytes 516..521 are never stored and are zero in the
  codeword; frame bytes 512..515 are the MCRC for slot 3 and `0x00` in the mother
  frames of slots 0–2; page bytes 16260..18431 are `0xFF`.
* Both: the whole stored frame is the complement of the codeword.

## 7.7 Verification totals

* **EE3**: 374 pages × 8 codewords — even 1424 verified / 0 failed / 72 erased; odd
  1415 verified / 9 failed / 72 erased, and **all nine failures have syndrome weight
  exactly 5 or exactly 18** (the two column weights of this code) and localise to one
  flipped bit each — real media errors, not layout defects. Media parity regeneration:
  byte-exact **952/952 even and 947/947 odd**. **[M]**
* **A10R**: 8220 raw codewords across five dies — 8218 syndrome-clean, 2 with weight
  exactly 6 (single-bit, located and fixed). **Strict hold-out**: the code was derived
  from 228 pages of one die and verified 2800/2800 on the rest of that die and
  380/380, 160/160, 160/160, 160/160 on four dies never used in the derivation. Parity
  regeneration from the silicon tables: **8218/8218 byte-exact**, all four slots.
  **[M]**
* **The single strongest claim in the whole document**: a real AU taken off the die
  **re-encodes to the identical 4608 bytes** — message, MCRC, scrambling, both EE3
  parities and the pipeline tail. That is not "we can read it"; it is "we know what the
  controller would have written." **[M]**

Everything here is from one controller. The codes are hardware constants, so they
should be universal — but that is an assumption, not a measurement.

---

# Part 8 — scrambling

## 8.1 The keystream

A **53-bit LFSR**, taps `(1, 49, 50, 52, 53)` **[M]**:

```
seq[N] = seq[N-1] ^ seq[N-49] ^ seq[N-50] ^ seq[N-52] ^ seq[N-53]      N >= 53
```

Bit-order convention, which must be got right or true relations silently read as false:
the keystream bits `b[0], b[1], …` come out **MSB-first within each byte**, and **the
state IS `b[0..52]`**, with `b[0]` as its most significant bit. In register form:

```
nb = (r ^ (r>>48) ^ (r>>49) ^ (r>>51) ^ (r>>52)) & 1
r  = ((r << 1) | nb) & MASK53
```

Free 1-bit sanity check on any candidate state **[M]**: the `(x+1)`-component of a real
state is always 1, i.e. `b[52] ^ b[3] ^ b[0] == 1`.

The tap count is odd, which has a consequence worth knowing: **all-ones is a fixed
point**, and `keystream(s ^ ones) == ~keystream(s)`. An inverted page therefore
descrambles ~99.7% "correctly" against the complementary seed while the LDPC looks like
noise. That asymmetry is the signature of an inversion, not of media damage.

## 8.2 The seed

**The seed is a function of the ROW and of nothing else. It is CE-independent** —
`state(row) == state(row & 0x3FFF)` **[M]** — which is why the LDPC, descrambling, MCRC
and aux all work on a completely unlabelled die.

```
row   = (lun << 23) | (block << 11) | page
seed  = F(row & 0x3FFF)                        # FOLD_MASK, so F has 16384 entries
```

The fold is settled **globally**, not locally: bits 14..31 were driven at eight
different base points plus scattered patterns and 13 of 13 returned the folded answer.
Earlier evidence only flipped upper bits one at a time from a single base row, which
proves local, not global, deadness. **[M]**

`F` is solved in closed form from **325 bytes of silicon ROM tables** rather than a
64 KB blob — three tables into a 32-bit **integer adder**, a gray-code fixup, then the
scrambler's own shift register clocked from index −15:

```
f0 = x & 0x1F      f1 = (x >> 5) & 0x1F      f2 = (x >> 10) & 0xF
SUM  = ( T0[f0] + T1[f1] + T2[f2] )  mod 2^32          <- a REAL integer adder
v    = SUM ^ EPS                    EPS = SUM(0) = T0[0]+T1[0]+T2[0]
R[i] = v bit ((2 - i) mod 32)       bit-reverse + rotate-left-3
R[3] ^= R[4]                        the adder's MSB is GRAY-CODED
b[-15+i] = R[i] ^ (REG0 bit i)      REG0 = 0xBB33C367, the x = 0 register
```

`b[-15..16]` is then the 32-stage XNOR LFSR with taps {3, 32} — the scrambler's own
polynomial — so clock it: `b[n] = b[n-32] ^ b[n-29] ^ 1` forward to `b[45]`, and
`b[n] = b[n+3] ^ b[n+32] ^ 1` backward to `b[-18]`. The 53-bit state is a splice of
that one keystream span:

```
b[0..45]  direct
b[46+t] = b[-18+t]                       t = 0,1,2
b[49+t] = b[-15+t] ^ b[t] ^ 1            t = 0,1,2
b[52]   = b[0] ^ b[3] ^ 1   ( = b[32] )
```

Evidence **[M]**: this reproduces **all 16384 entries** of a hardware-measured seed map
exactly, four times over by three independent re-derivations, and was separately
confirmed on silicon through the ECU's own seed-map bypass — which makes the
intermediate 32-bit value observable, something fitting the composition alone could
never prove. The adder is genuinely an adder: 12686 of 16384 rows carry past 2³², the
carry-free functional space has dimension 1 of 32, and an exact affine solve over all
16384 rows fits 0 of 32 output bits. **This is the strongest-attested layer in the whole
stack.**

Two further measured facts about the seed **[M]**: driving the network directly shows
the raw seed input *is* the row — 31 of 31 single-bit and mixed rows give `PBA == row`
exactly, with no shift, no field re-packing and no plane or AU bits; and across CEs the
value is byte-identical, so it carries **no CE bits in any of its 32**, not merely in
the 14 the map keeps.

> **Do not try to recover the row by inverting the seed map.** It is **impossible**,
> not merely hard: the map is non-linear, ANF degree 11–12 of 12 on the full subcube,
> image rank 32 — a cipher-like expander. No GF(2) polynomial of degree 1–4 fits any of
> the 53 output bits over 3155 clean seeds (0/53 at every degree), and
> `state(3) != state(1) ^ state(2)` kills affinity outright. **[M]**

## 8.3 A shortcut that needs no seed map at all

The 8-byte aux sits at **keystream index 0**, and the state *is* the first 53 keystream
bits, so:

```
state = first53( stored_aux XOR aux_plaintext )
```

with **11 spare bits as a free consistency check**. For regions whose aux plaintext is
a per-image constant this reads the state straight off the page. Independently, all
1472 states recovered this way lie in an affine subspace of GF(2)⁵³ of dimension exactly
32 — random or wrong states would give 53 — confirming that the hardware seed register
is 32 bits wide and expands affinely to 53 **[M]**.

## 8.4 How the keystream is applied

**XOR, and involutive**: scramble and descramble are one operation.

**Format A** — the covered span is `logical[0:4104]` **only**: the 8 aux bytes
(indices 0..7) and the 4096 user bytes (8..4103). The MCRC at `logical[4104:4110]` is
**not** scrambled (it is computed over the already-scrambled message and written
verbatim), and the 2 × 246 parity bytes are **not in the keystream span at all** — the
LFSR skips them. This is closed by the byte-identical re-encode. **[M]**

**All four AUs of a page restart the keystream from the same state.** The seed is per
row, not per AU, so a page decode asks for the identical 4104 keystream bytes four
times. Inside an AU the LFSR runs continuously across the codeword boundary, skipping
the 246 parity bytes — which is why the odd codeword's fitted state is the even one
advanced by exactly one codeword payload, and which resolved an earlier "two states per
page alternating with codeword parity" observation. **[M]**

**Format B is different, and this is the one thing that separates the boot region**:
the keystream is a **2048-byte block that restarts for every 3252-byte AU**, where
groups 0/1 reuse one block across the page's four AUs. Within an AU,
`ks_index = (k % 4) * 512` for codeword k; the 300-byte parity islands and the 4-byte
MCRC are skipped. Getting this wrong yields output that is correct for 2048 bytes and
garbage after. **[M]**

**Format C is not scrambled at all.**

The seed source is an ECU setting (`SCR_SEED_SRC`), and every populated format slot
selects **2 = PBA** **[D]**. Note that `SEED_SRC = 0` feeds input 0 *through* the map;
it is not a bypass.

---

# Part 9 — the physical layer

## 9.1 Dies, chip enables, channels, packages

| quantity | value | grade |
|---|---|---|
| chip enables on the device | **12** | **[D]** firmware |
| dies per CE | **1** | **[M]** READ ID byte 2 bits[1:0] = `00`, and capacity closes on it |
| dies per package | **4** | schematic |
| packages | **3**, `package_of(ce) = ce // 4` | arithmetic on the two above |
| channels | **12**, `channel_of(ce) = ce` | schematic DQ net count 12 × 8 = 96 |
| planes | **2**, carried in the **block LSB**, never in the LUN field | **[M]** ID byte 2 bits[5:4] = `01`, plus **[D]** plane = block LSB |
| blocks per LUN | **4096** — an **address-field ceiling**, not a die block count | arithmetic: `1 << (LUN_SHIFT − BLOCK_SHIFT)` |
| physical blocks per die | **4000** | **[M]** the media's own WERU configuration table sums to 48000 over 12 CEs |
| factory-usable blocks per die | **3800** | **[D]** the spare-pool formula |

Capacity closure **[M]**: `12 dies × 3800 blocks × 1104 pages × 16 KiB = 824.8 GB`, the
retail figure. Two dies per CE would give 1650 GB and no such product exists.

`LUNS_PER_CE` reads 2 in the firmware's configuration and is **not** a die count: the
ID says one die per CE, and the flash-interface init clamps it to 1, after which the
LUN field of every row resolves to 0. Every row this project has ever composed is
LUN 0. The firmware's own derivations reproduce the measured WERU table only if exactly
one of {planes, diesPerCe} is 2, so the firmware carries two names for one real factor
of two and the ID says which name is physical. **[D]** + labelled inference.

**`ce // 2`** is a real grouping — it appears in two independent RootStorage placement
laws (§2.9) — but it is **neither a package (that is `ce // 4`) nor a channel (that is
`ce`)**. Best remaining reading, and it is an **inference**, not a measurement: with
four dies per package it is the die **stack**, two stacks of two per package. Nothing
here decapped a part. This is recorded because a plausible name for it was twice
asserted as fact — first "package", then "channel" — and both were withdrawn; the DQ
net count settles it electrically, because two CEs cannot share a bus that does not
exist.

## 9.2 The row address, and pbid

```
row = (lun << 23) | (block << 11) | page          LUN_SHIFT = 23, BLOCK_SHIFT = 11
```

| field | bits | width | range |
|---|---|---|---|
| `page` | [10:0] | 11 | 0..1103 (TLC) or 0..367 (pSLC); field ceiling 2048 |
| `block` | [22:11] | 12 | field ceiling 4096 |
| `lun` | [23]+ | — | always 0 in practice |

The FTL's flat block name is **radix-12 arithmetic, not a bit field**:

```
pbid  = 12*block + ce          ce = pbid % 12      block = pbid // 12
```

A decoder that masks low bits gets the wrong CE.

## 9.3 Cell modes

Pseudo-SLC is **a volatile die mode entered by a prefix command, not a property of the
block**.

| | prefix | pages/block | row ↔ page |
|---|---|---|---|
| pseudo-SLC | `0xDA` (Samsung, Micron) / `0xA2` (Toshiba, Hynix, SanDisk) | **368** | `row == page`, 1:1 |
| native TLC | `0xDF` (the multi-level counterpart) | **1104** | 3 pages share a word line |

`368 = 1104/3` exactly **[D]**. The prefix is per die, does not leak between CEs, does
**not** survive a NAND `0xFF` reset, and must be re-issued per CE after every reset
**[M]**.

**Wrong-mode reads return plausible bytes, not errors.** Measured, 28 of 28 A/B pairs
across seven CEs: the pseudo-SLC read gives MCRC 4/4 and the prefix-less read MCRC 0/4,
with no third outcome **[M]**. Read the firmware area without the prefix and the damage
looks like three unrelated bugs — an apparent `row = 3*page − 4` page mapping,
whole-page bit inversion at deeper rows, and a bit error rate that climbs with page
depth — all from the one cause.

**Cell mode is a property of the WERU**, not of the block number. Applying a
block-number rule ("blocks 0..27 are pseudo-SLC, everything above is TLC") is wrong:
pseudo-SLC WERUs exist well above the firmware area. The `weruDescriptor +10` cellMode
byte uses **1 = pseudo-SLC, 0 = native TLC** — a *different* numbering from the die's
and from the retry tables', which must not be conflated (§9.7).

An offline rule that is free and exact **[M]**: `divmod(capacity_in_AUs,
n_blocks * 4)` gives pages per block, and `{368: SLC, 1104: TLC}` is a **lookup, never
a nearest match** — a non-zero remainder means UNKNOWN. Predicting two WERUs' modes
offline from capacity alone *before* reading them in both modes is the strongest
cell-mode evidence available **[M]**.

## 9.4 Word lines

The firmware's page→word-line law for a native block, transcribed verbatim **[D]**:

```
page <  8     ->  page >> 1                  # 2 pages/WL, WL 0..3
page < 1088   ->  (page - 8)//3 + 4          # 3 pages/WL, WL 4..363
page >= 1096  ->  page - 728                 # 1 page /WL, WL 368..375
else          ->  ((page - 1088) >> 1) + 364 # 2 pages/WL, WL 364..367
```

Closure: `4·2 + 360·3 + 4·2 + 8·1 = 1104` pages over **376 word lines**. Pseudo-SLC is
the identity — but note 376 ≠ 368, so the two are **not the same word-line numbering**,
and the pSLC identity is an observation, not a sourced law.

Position within a TLC word line, and the measured readability that goes with it
**[D]** for the geometry, **[M]** for the ranks:

| predicate | name | EE3 syndromes per codeword | at default references |
|---|---|---|---|
| `page < 8` | pre-TLC (2 pages/WL) | 0..28 | decodes 4/4 |
| `(page-8) % 3 == 0` | **lower** | 150..600 | decodes 4/4 |
| `(page-8) % 3 == 1` | **middle** | 370..900 | borderline |
| `(page-8) % 3 == 2` | **upper** | 950..1030 | **never decodes** |

EE3's waterfall is ≈ 170 syndromes per codeword, so an upper page is not marginal — it
is uncorrectable without read-retry.

A second firmware routine brackets a native block differently, returning a
"natively-SLC" class for `page < 4` or `page >= 1100` and native TLC otherwise **[D]**.
**The two routines do not agree on the boundary** (0..3 / 1100..1103 versus 0..7 /
1088..1103), both are in the firmware, and nothing here resolves which the die
physically implements. Recorded as a **[?]** at the level of "which is true of the
array"; each is **[D]** for its own routine.

## 9.5 Regions, and block 0

Region membership is **pure addressing** — block numbers the boot ROM and CPU1
firmware hard-code, not anything read off the media — and it is the first thing a
decoder must ask, because two of the three regions are not what format A describes:

| region | where | what |
|---|---|---|
| `fw-group2` | **block 0 on CE 0..5** | its own ECC geometry entirely: format B, 20 codewords × (512 payload + 300 parity), 10240 user bytes per page |
| `fw-group01` | **blocks 6..9, any CE** | format A bytes, but addressed by raw CE/block/page, and the aux carries **no LMA array** |
| `ftl` | everything else | format A, and the aux means whatever the owning MSO's `msoDescriptor +17` says it means |

**Note the CE test on the boot region.** Block 0 on CE 6..11 is *not* group 2 — it is
ordinary format-A content owned by the firmware-partition MSO, measured MCRC-clean —
and a region map that ignored the CE would corrupt those pages into the wrong decoder
**[M]**. Two independent routes agree on the boundaries: these block numbers, and the
FTL's own ownership records.

**Why block 0 is different:** it is the region the boot ROM boots from, so it predates
the FTL and predates the controller's runtime AU format. It has a different codeword
size, a different LDPC code, a different MCRC width and a different keystream blocking.

Contents of block 0 as a format, measured over all 368 rows **[M]**: row 0 a device
header, row 1 a firmware header with magic `1d 33 47 77`, then an encrypted payload, a
short tail in the clear, a second header and a second encrypted payload, then a
trailing region and a 140-page fill pattern of exact period 8. **The five copies on
CE 1..5 are byte-identical replicas, not an interleave** — one CE suffices to read it.
The full row census, and the one part of it that is not closed:

```
row     0        device header, plaintext 5a 01 5b 00 then zero
row     1        firmware header, magic 1d 33 47 77, fw_type 1
rows    2..151   fw 1 payload, AES-128-CBC, payload_len 0x177000
rows  152..154   3 pages IN THE CLEAR, past the declared payload
row   155        firmware header, fw_type 2
rows  156..181   fw 2 payload, AES-128-CBC, payload_len 0x41000
rows  182..227   46 pages, past the declared payload      <- see below
rows  228..367   140 pages of an 8-page fill pattern, exact period 8
```

No row of block 0 is erased.

**Rows 182..227 — placement explained, content unverified.** The boot ROM's load
window per group-2 image is a firmware constant: rows 1..154 for fw 1 and rows
155..227 for fw 2 **[D]**. Both windows run PAST the length their own header
declares, so those 46 rows are the part of fw 2's window that the ROM stages
beyond `payload_len` — they are there on purpose, and an older note calling the
run *unexplained* is superseded on that point.

What they **contain** is a different question and is still open **[?]**. They
carry residual density 0.50, duplicate no other row, and decrypt under neither
header's IV **[M]**. Fw 1's overrun is the encouraging precedent and also the
measure of what is missing: its three rows were proven byte-identical to the
staged SRAM image at `0x177000 / 0x179800 / 0x17c000`, and that identity is
re-checked on every self-test run — while the equivalent check for fw 2's 46
rows has never been run. Treat the run as staged firmware whose decoding is
unproven, not as a mystery region and not as a solved one.

Neighbouring regions, for orientation **[D]** cross-checked against FTL ownership
**[M]**: blocks 1..5 RootStorage; blocks 6..9 the firmware image groups (group 0 striped
over 6/7, group 1 over 8/9, on every CE); blocks 12/13 the trim log. Blocks 0..27 are
treated as the pseudo-SLC bootstrap region; **27 is a safe upper bound, not a proven
boundary**, and it says nothing about the cell mode of any block above it.

### Firmware image layout, for completeness

Groups 0 and 1 are **striped across six CEs and two blocks** and must be
de-interleaved; group 2 is **replicated**, not striped. The group 0/1 header lives at
block 0 page 0 and parses as **[D]**:

```
4 x u32                      byte offsets of the four elements
4 x { u32 count; count x 3 u32 }   the four elements, 12 entries each
u16 checksum                 sum of (lo16 + hi16) over every u32
```

An entry is three little-endian u32, and the field order is **not** the obvious one:

```
word0   byte0 = CE   byte1 = grouping tag   byte2 = plane   byte3 = LUN
word1   = block
word2   = base row-in-block
```

The four elements are {group 0, group 1} × {CE 0..5, CE 6..11}, and the two CE banks
are **mirrors of the same image**, not two firmware versions. Within an element the 12
entries are ordered `ce6b8 ce6b9 ce7b8 ce7b9 …`, i.e. 6 CEs × 2 blocks, and the two
blocks of a CE are **parallelism, not redundancy**. Reassembly:

```
unit  = page_in_block * 12 + entry_index          # one unit = one page
entry = unit % 12       page = unit // 12
(ce, block) = element.entries[entry].ce / .block
row         = compose_row(block, (entry.base_row + page) % 368)
```

and the image is the units' 16384-byte user payloads concatenated in increasing unit
order. A FW_ID names a contiguous run of units inside one group.

## 9.6 Read-retry — what it changes and what it cannot

An intact page can be unreadable purely because the reference voltages are being
compared in the wrong place. On native-TLC upper word lines this is not theory:
**0 of 1010 upper pages decode at the die's default references**, across many CEs and
WERUs, and it generalises **[M]**.

**Mechanism** **[D]**, exercised on silicon **[M]**: ONFI **SET FEATURES `0xEF`** with a
one-cycle feature address and exactly **4 payload bytes**; **GET FEATURES `0xEE`** reads
them back. The payload bytes are **signed read-reference offsets in the die's DAC step
unit** — they shift comparison voltages and nothing else. `0xEF` writes a **volatile
mode register**: no row address, no confirm code, no path to the array, proven by
write → read back → restore → read back. **It cannot alter stored charge.**

The interface **doubles every byte** on this path (8 transferred for 4 payload bytes;
the writer emits `b | b<<8` and the reader keeps the even bytes) — also true of READ ID
and READ STATUS, by die design.

**Which feature address, by cell mode** **[D]**:

| die cellMode | feature addresses | references shifted |
|---|---|---|
| **0** — native TLC (3 pages/WL) | `0x89` (4 B) + `0x8A` (4 B) | **7** references + 1 pad (`0x8A[3]` always 0) |
| **3** — 2 pages/WL | `0x89` + `0x8A` | **3** references (`0x89[0]`, `0x89[2]`, `0x8A[0]`) |
| **2** — pseudo-SLC | `0x8D` (4 B) | **1** reference + 3 pad |

7 references = 8 levels = TLC, 3 = 4 levels, 1 = 2 levels — internally consistent with
the tables' own byte occupancy.

**Table encoding** **[D]**: **42 bytes per step** (21 × u16), entry for step *n* at
`base + 42*n`, ladder length **32 steps**. Word[0] is a count, then `count+1` words:
`word >> 8 == 0x1C` starts a feature address (`word & 0xFF`), `word >> 8 == 0x04`
contributes one payload byte. The builders live in the part-specific driver, so
**the ladder values are part-specific**, not controller-specific.

There is no "level 0" in the firmware's numbering — it simply does not issue SET
FEATURES on the first attempt. "Level 0" here means all offsets zero, which is both the
power-on state and the literal content of pseudo-SLC ladder step 2, which is what makes
"restore the default" a defined operation rather than a guess.

### The ladders

**Native TLC** — `0x89` = 4 bytes, `0x8A` = 4 bytes (last byte always `00`). Each byte
is a signed DAC-step offset.

| step | 0x89 | 0x8A | | step | 0x89 | 0x8A |
|---|---|---|---|---|---|---|
| 1 | `EA F2 F4 E6` | `F6 F3 ED 00` | | 17 | `EA F9 FB F5` | `F9 F9 F8 00` |
| 2 | `E4 EE F1 E1` | `F6 F4 F2 00` | | 18 | `F1 FE 00 F1` | `03 F9 FC 00` |
| 3 | `E9 F0 F0 E0` | `F3 EC EB 00` | | 19 | `F2 01 02 00` | `01 00 00 00` |
| 4 | `E5 F2 F4 E6` | `F7 ED F0 00` | | 20 | `F6 FE 00 F1` | `02 FF F9 00` |
| 5 | `E0 EF F1 E2` | `EF EB E7 00` | | 21 | `E5 FA FA F7` | `F8 F9 F9 00` |
| 6 | `F5 FC FC EB` | `FF F8 F7 00` | | 22 | `E4 FF FD 04` | `F9 FE DA 00` |
| 7 | `EC FB FD ED` | `FB F7 F3 00` | | 23 | `EF 07 07 06` | `05 06 07 00` |
| 8 | `E6 F2 F4 E2` | `F1 F6 ED 00` | | 24 | `F1 06 06 02` | `04 05 05 00` |
| 9 | `F0 FA FD EC` | `02 00 FE 00` | | 25 | `F4 07 08 05` | `05 07 07 00` |
| 10 | `E2 F3 F6 F3` | `F5 F3 F2 00` | | 26 | `F6 05 07 00` | `05 05 04 00` |
| 11 | `0A 00 04 F4` | `05 05 FB 00` | | 27 | `E4 FB FB FB` | `F9 FA FB 00` |
| 12 | `E6 F5 F6 F5` | `F5 F4 F4 00` | | 28 | `E8 FB FC FA` | `F9 FB FB 00` |
| 13 | `F2 FE 00 ED` | `FD 01 F9 00` | | 29 | `EE FD FF F6` | `00 FE FA 00` |
| 14 | `08 0B 09 0E` | `07 0A 0C 00` | | 30 | `E7 00 01 FC` | `01 00 FE 00` |
| 15 | `11 FB 03 EA` | `05 02 F6 00` | | 31 | `F0 0B 09 0F` | `05 0A E6 00` |
| 16 | `EE FF 02 FE` | `01 FF FE 00` | | 32 | `F8 07 06 0A` | `05 07 07 00` |

**Two-pages-per-word-line ladder** — only `0x89[0]`, `0x89[2]` and `0x8A[0]` carry
values; `0x89[1]`, `0x89[3]` and `0x8A[1..3]` are `00` throughout.

| step | `0x89[0]` | `0x89[2]` | `0x8A[0]` | | step | `0x89[0]` | `0x89[2]` | `0x8A[0]` |
|---|---|---|---|---|---|---|---|---|
| 1 | `F0` | `00` | `E2` | | 17 | `E0` | `CE` | `0E` |
| 2 | `F0` | `0A` | `E4` | | 18 | `E4` | `F6` | `10` |
| 3 | `DD` | `00` | `E6` | | 19 | `F4` | `EC` | `18` |
| 4 | `EC` | `D8` | `E8` | | 20 | `DA` | `D8` | `1C` |
| 5 | `E0` | `D8` | `EE` | | 21 | `D6` | `F4` | `1E` |
| 6 | `E2` | `00` | `F0` | | 22 | `EC` | `F4` | `EA` |
| 7 | `E2` | `E2` | `F2` | | 23 | `D9` | `EC` | `EC` |
| 8 | `E6` | `0A` | `F4` | | 24 | `CE` | `E6` | `FA` |
| 9 | `DA` | `F2` | `F6` | | 25 | `C8` | `EC` | `FC` |
| 10 | `EC` | `E2` | `F9` | | 26 | `CD` | `CA` | `04` |
| 11 | `F6` | `D6` | `FE` | | 27 | `D2` | `F5` | `05` |
| 12 | `CE` | `01` | `FE` | | 28 | `D3` | `EC` | `02` |
| 13 | `D8` | `0A` | `01` | | 29 | `D2` | `E2` | `06` |
| 14 | `D8` | `01` | `06` | | 30 | `E5` | `EC` | `14` |
| 15 | `E2` | `00` | `0A` | | 31 | `D4` | `0A` | `16` |
| 16 | `CE` | `D8` | `0C` | | 32 | `D8` | `E2` | `1A` |

**Pseudo-SLC** — `0x8D`, byte 0 only; a clean ±5-step ladder spanning −75..+75.

| step | byte | signed | | step | byte | signed | | step | byte | signed |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `B5` | −75 | | 12 | `32` | +50 | | 23 | `DD` | −35 |
| 2 | `00` | **0 (default)** | | 13 | `37` | +55 | | 24 | `E2` | −30 |
| 3 | `05` | +5 | | 14 | `3C` | +60 | | 25 | `E7` | −25 |
| 4 | `0A` | +10 | | 15 | `BA` | −70 | | 26 | `EC` | −20 |
| 5 | `0F` | +15 | | 16 | `BF` | −65 | | 27 | `F1` | −15 |
| 6 | `14` | +20 | | 17 | `C4` | −60 | | 28 | `F6` | −10 |
| 7 | `19` | +25 | | 18 | `C9` | −55 | | 29 | `FB` | −5 |
| 8 | `1E` | +30 | | 19 | `CE` | −50 | | 30 | `41` | +65 |
| 9 | `23` | +35 | | 20 | `D3` | −45 | | 31 | `46` | +70 |
| 10 | `28` | +40 | | 21 | `D8` | −40 | | 32 | `4B` | +75 |
| 11 | `2D` | +45 | | 22 | `DB` | −37 | | | | |

### Measured behaviour of the ladder

* **Upper word lines never decode at default references** — 0 of 1010. **[M]**
* The winning set is **{1, 4, 6}**, and *which* member wins is positional and per-die
  and not stable. **[M]**
* Levels {0, 1} alone cover 565 of 715 upper pages — they lose **21% silently**. Adding
  step 15 changes nothing; **step 15 has never won a page**. **[M]**
* Best order is **6, 1, 4**: 1.000 reads per page over 819 pages (820 reads, 819/819).
  A direct A/B on the same band gives `1, 6, 4` at 1.528 reads per page. Step 6
  subsumes steps 1 and 4 on that band, but **step 1 is still load-bearing** — one page
  in 820 misses 6 and wins at 1. **[M]**
* **Pseudo-SLC needs no ladder: 1779 of 1779 pages decode at level 0.** **[M]** An
  earlier claim that 232 pages were "rescued by the pseudo-SLC ladder" is **retracted**
  — all 232 were page 0 of a *native-TLC* block read under the pseudo-SLC prefix, i.e. a
  cross-mode read. The ladder works; those pages cost nothing if read in the right cell
  mode.
* Near an open block's **write front**, lower and middle word lines fail too. The front
  is a property of the **WERU, not the die**, so one probe answers for a whole
  superblock, ±1 word line. **[M]**

> Costing an ordering **requires** measuring every step on every page. A walk that
> stops at the first success cannot price an ordering — for a step-1 page it learns
> nothing about step 6 — so coverage figures from such a walk are a lower bound, not a
> measurement.

## 9.7 Three different "cellMode" numberings

They must not be conflated:

| where | values |
|---|---|
| the die / retry-table selector | 0 = native TLC (3 pages/WL), 2 = pseudo-SLC, 3 = 2 pages/WL |
| the per-page word-line class routine | 0 = native TLC, 2 = natively-SLC bracket |
| `weruDescriptor +10` | 1 = pseudo-SLC, 0 = native TLC, 2 = no fixed page mode |

Also **[?]**: the retry-table selector indexes with `cellMode >> 5` while the documented
values are 0/2/3, all of which shift to 0. That is transcribed as-is and is not
reconciled.

## 9.8 Vendor and part variants

The part fitted is a Samsung TLC device; READ ID is identical on every live CE **[M]**.
The controller's package list is a linear `memcmp` scan by id length with a one-byte
generic maker fallback, so an unlisted part of the same maker still matches **[D]**.

ID decode for byte 2 **[M]**: bits[1:0] = `00` → one die per CE; bits[3:2] = `10` →
8 levels per cell = TLC, which the media independently confirms and which is what
validates using the decode table at all; bits[5:4] = `01` → two simultaneously
programmed pages. **Byte 3 does not decode on the classic table** — it would claim an
8 KB page against a real 18432-byte page — so this part uses an extended encoding, its
meaning is **[?]**, and nothing here reads it. **No NAND ID encodes packaging or
ballout**; that must come from the part marking.

The boot ROM's vendor dispatch **[D]**; unrecognised makers fail hard:

| vendor | maker ID | address cycles | pseudo-SLC prefix |
|---|---|---|---|
| Toshiba | `0x98` | 5 | `0xA2` |
| Micron | `0x2C` | **4** | `0xDA` |
| Samsung | `0xEC` | 5 | `0xDA` |
| Hynix | `0xAD` | 5 | `0xA2` |
| SanDisk | `0x45` | 5 | `0xA2` |

All five per-vendor parameter setters are byte-for-byte identical **except** the
pseudo-SLC prefix byte.

**Controller-specific, vendor-invariant, safe to hard-code** **[D]**, from static
analysis of the boot ROM and CPU1 firmware: the AU formats A/B/C/D, both LDPC codes,
both AU MCRCs, the 53-bit scrambler and its seed map, the 4608-byte AU stride and the
18432-byte page. **No board strap and no NAND vendor changes the AU layout, the ECC,
the scrambler, the MCRC or the page geometry.**

**Vendor-specific** (a *reader* must parameterise these; a pure offline decoder needs
none of them): the pseudo-SLC prefix byte, the address-cycle count, LUNs per device
(row composition), and the part's pages-per-block and blocks-per-die.

**Board straps** are link-layer only **[D]**: two latched test pins select a NAND
toggle mode and an extended interface configuration, flipping one interface register
bit each and together selecting a 28-register timing profile. They move interface mode,
timing and VREF only. A decompiler comment guessing that those GPIOs indicate the NAND
vendor is **wrong** — the vendor comes from the maker ID.

**[?]** Whether the read-retry ladders differ per vendor was not examined; the baked
ladder is one part's table.

---

# Part 10 — evidence summary

| layer | strongest assertion | grade |
|---|---|---|
| SLBA → (lpId, LBA) | round trip plus refusals | **[D]** constants, **[C]** arithmetic |
| LBA → LMA | derived from `cLBAFTable`, cross-asserted against `LMA_BYTES` | **[D]** + **[C]** |
| LMA → WMA (LUT) | sub-granule test; system LUT versus independent WERU scans | **[M]** |
| LMA → WMA (LPT + L2S) | L2S rebuilt from media aux, 12/12; end-to-end read requiring both LMA and lpId in the aux | **[M]**; the multi-superblock division step untested |
| WMA → (weru, page, slot, au) | exhaustive round trip, plus 3568 media observations that consume its `page` and `slot` | **[C]** in isolation, **[M]** by consequence |
| slot → (ce, block), 12-block | aux-LMA hold-out, 3888 right / 0 wrong | **[M]** |
| slot → (ce, block), 48-block | 3568 right / 0 wrong over 5 WERUs, 251 windows, 11 CEs; every rival at exactly its ceiling; hardware hold-out 528/528 on a previously unread block | **[M]** |
| record position → slot | one permutation over 914 WERUs / 43872 entries, 0 ambiguous; derived from firmware, not fitted; 3568/0 on media | **[M]** + **[D]** derivation |
| substituted block → slot | zero-parameter hold-out 79/79 on the record | **[M]** on the record, **[?]** on media |
| inline RAID parity | 12-way XOR zero 847/847; leave-one-out 5328/5328; kernel dimension 1 | **[M]** |
| Mso RAID placement | 25-bit stripeId agreement 10 exact / 0 wrong; 15737/15737 structural; 15737/15737 validMap cross-check | **[M]** |
| **Mso RAID XOR** | **not established — SKIPPED, never PASS.** Structurally unrunnable at 4 erasures against a 1-erasure code; the residual statistic is a proven non-discriminator | **[?]** |
| validMap after a dirty mount | second bulk writer located; stored rows reproduced bit for bit by replaying a LUT | **[M]** measurement, **[D]** stage map |
| cell mode | offline hold-out: two WERUs predicted from capacity alone before being read in two modes | **[M]** |
| read + retry | 819 pages / 820 reads at order `6, 1, 4`; 0 of 1010 upper pages at default references | **[M]** |
| LDPC | 8218/8218 media codewords regenerated, plus a silicon-image twin construction that agrees byte for byte | **[M]** |
| descramble | 16384/16384 hardware seed map reproduced from 325 bytes, plus hardware bypass confirmation | **[M]** — strongest |
| MCRC | byte-identical 4608-byte AU re-encode; external CRC-32Q check value | **[M]** |
| aux | 5723/5723 byte-identical aux re-encode, plus a random-aux negative control | **[M]** + **[C]** |

---

# Part 11 — what is unknown, said so on purpose

These are **not** gaps to be filled with a plausible guess. Several were filled with a
plausible guess once already.

* **The `cRaidTypeMso` parity function.** Where it lives is solved; the XOR itself
  cannot be closed from an image with a dead die, and no statistic of a ciphertext XOR
  can substitute.
* **The inheritance of a slot by a substituted block, on media.** The record-side
  evidence is complete; a media hold-out needs an image in which a substituted WERU was
  *written after the substitution*.
* **The WERU configuration record's `+8`.** Machine-checked as having zero callers in
  this image; its construction formula is known, its meaning is not.
* **`msoDescriptor` bytes `+2`, `+6`, `+11`, `+13`, `+15`, `+18`, and `+0` bits 1–2.**
  Emitted by the firmware's own per-field dumper, so they are real fields — unnamed.
* **`msoDescriptor +21..+27`** is proven dead (never read, never written) and yet
  persisted, so four bytes per record of uninitialised RAM go to media.
* **`weruDescriptor +1` and `+2`** are measured negatives: no reader, no writer, not
  even an initialiser for `+1`, in any of the four cores.
* **LUT descriptor snapshot-element fields `+4`, `+12`, `+20`**, and header `+4` and
  `+12` — read 0 everywhere held.
* **`PhysicalBlockProperties` `pageBlockMode` value 3** is described (no mode assigned
  yet) but named by no firmware string; value 0 is never observed.
* **The `mediaSpace` SystemMetadata section** is not decoded.
* **Whether slot 0 of a 12-block WERU is a second inline-parity slot**, and **whether
  the Trim MSO's WERU carries inline parity at all**.
* **`LptEntry +20` bit 0** is maintained and never consumed — no branch tests it.
* **The pseudo-SLC page→word-line function.** The identity mapping is an observation
  that fits the persisted word-line field, not a sourced law, and 368 pseudo-SLC pages
  against the TLC law's 376 word lines mean the two numberings are not the same.
* **Aux layout 1 has no firmware name.** Three `cAuxLayout*` strings exist across all
  four cores and none is layout 1. The name stays absent rather than becoming a
  plausible guess.
* **`0xFFFFFFFB` as an aux LMA** — a real value on media with no reading or writing
  site anywhere.
* **Layout 2's shard order** rests on the reader disassembly alone; every sequence
  observed is below 65536, so only AU3's position is demonstrated.
* **The residual low bits of a layout-3 parity AU's AU0/AU1 second dword**, beyond the
  marker and the sequence XOR.
* **The size of the retired trim history.** The watermark says the log is truncated and
  says exactly where; what preceded the truncation is not recoverable from one image.
* **Which word-line bracket the array actually implements** — two firmware routines
  disagree (§9.4).
* **The retry-table selector's `>> 5`** against documented cellMode values 0/2/3.
* **NAND ID byte 3** does not decode on the classic table; the extended encoding's
  meaning is unread.
* **Two unnamed ECU AU-format record bits** that are set in every populated slot and by
  the boot ROM, so they are almost certainly controller-specific enables.
* **Format D's MCRC** and `MCRC_INV_MODE = 1`, neither exercised by any populated slot.
* **A10R's mother base matrix** is recovered only on its 136 stored block columns; six
  of the 50 layers carry one extra circulant in the missing column, and which six is
  unknown. It does not affect decoding.
* **What the 46-page run at rows 182..227 of the boot block CONTAINS.** Its placement
  is settled — inside fw 2's ROM load window, past that image's declared payload
  length — so it is not an unexplained region. Its bytes are another matter: they
  decrypt under neither header's IV, and the check that closed the equivalent overrun
  for fw 1 (byte-identical to the staged SRAM image) has never been run for fw 2.
* **Whether read-retry ladders differ per vendor.**
* **Universality.** Everything here is from one controller and one drive. The codec
  constants are hardware and should be universal; that is an assumption, not a
  measurement.

---

# Part 12 — known contradictions in the source material

Recorded because a cross-check that is looked at and not resolved quietly becomes "we
checked". Where a note and the verified code disagree, **the code is ground truth** —
it is the artefact that reproduces media byte for byte.

1. **"EE3 parity covers the descrambled bytes."** Asserted in several notes, and two of
   them disagree with each other about the operational order it implies ("descramble,
   then ECC" versus "ECC, then descramble"). The encoder, the decoder and the media all
   say parity covers the **stored, scrambled** bytes for **both** codes, and that ECC
   runs first. See the box at the head of Part 7.
2. **The even codeword's information span.** One module's legacy mapping places
   `b0[2302:2304]` into the *even* codeword; feeding those bytes there gives syndrome
   weight 35, and forcing them to zero gives 0. They are the **odd** codeword's first
   two information bytes. The same module's "raw media is not a codeword" note is stale
   for the same reason.
3. **A superseded AU model** survives in two early scrambler modules: a 4116-byte AU,
   a 2058-byte per-codeword payload, and the aux read out of the tail bytes. The
   verified layout is a 4110-byte logical span with the aux at `b0[0:8]`, and
   `b1[2298:2304]` is a pipeline-tail duplicate, not a field. Do not use those two
   modules as a layout source.
4. **A retired 4-byte-digest offset** of AU+3248 points into codeword 3's parity, not
   at the MCRC at AU+2948. Analyses built on it were run against parity bytes, which are
   linear in the message, which is why they looked self-consistent.
5. **The aux sequence field** is described in one module as "AU1 bytes 4..6, u24". It is
   31 bits in AU1 plus 31 more in AU0; the u24 read is a truncation that starts losing
   the top of the counter at 2²⁴.
6. **`ce // 2` was called a package, then a channel.** Both are withdrawn: the package
   is `ce // 4` and the channel is `ce`. Any document naming `ce // 2` either is
   superseded. A stale comment to that effect still sits above the CE-count constant in
   the geometry module while the constants three lines below it are correct.
7. **The twelfth block of a metadata WERU** was carried as "unexplained" in one module
   for weeks after another module had established it as inline RAID parity at full
   strength, and the retraction never propagated. Two modules describing one drive
   disagreed about how well a fact was known. **That is its own class of bug**, and
   neither module was wrong on its own terms.

   **It recurred here.** The group-2 extractor's block-0 row census printed
   `UNEXPLAINED` against rows 182..227 long after the firmware-image module had
   retracted that word and given the reason — they sit inside fw 2's ROM load window.
   The first draft of *this* document inherited the stale census rather than the
   retraction, which is how the class of bug survives being written about. Both are
   now corrected; if you are holding an older copy of either, the load-window reading
   is the current one.
8. **"No later mount stage restores the valid map"** was asserted on the strength of an
   exhaustive search of the wrong symbol. There is a second bulk writer, at mount step
   35. An exhaustive search of the wrong symbol is still exhaustive, and still wrong.
9. **Retracted numbers, in case an older copy is quoted at you**: the retry order
   (`1,6,4` → `6,1,4`); the pseudo-SLC "232 rescues" (a cross-mode artefact); the
   NSID2/NSID3 superblock totals (`24/66` → `12/55`, the trailer u16 counted as a
   superblock); "the L2S array is not persisted" (it is); "the blanket four-consecutive-
   LMAs rule applies to every layout" (it rejects 100% of layout-3 pages).
10. **A parity PAGE is not a parity COPY.** A parity page is one `(weru, page)` row; a
    WERU is 48 columns wide, so one parity page has up to 48 stored copies. Summing
    copies inflates any inventory. Whenever a page count looks surprisingly large, check
    whether it is rows or column-copies — and check the cell mode.

The storage-layer notes in this tree predate the code in several places. Where they
disagree, the code is what the decoder does and what was checked against media:

11. **`blocksPerWeru = 48` as a drive-wide constant.** It is per configuration —
    24/24/12/48/48/24 — and 48 applies to two of the six. The notes' 211968 arithmetic
    and stride survey are about the *TLC data* class and are correct there, as is their
    warning against reusing the 12-block stride for the global table.
12. **The pbid decomposition inside a 12-block record.** The notes read a metadata
    WERU's twelve consecutive pbids as one block number across all twelve CEs. The code
    retracts that for records: they decode as `24*pairIndex + 12*half + slot`, a block
    **pair on the six CEs of one half**. `pbid = 12*block + ce` remains correct as the
    flat encoding; it is the *record* reading that was wrong.
13. **How many `SystemBlockMapping` slots are live.** The notes say exactly one. Two
    are, and the replay installs every valid slot at once — they are two halves of one
    live image, and five of the nine SystemMetadata sections live on the other half.
    The related note that a second block pair holds "a complete OLDER metadata image"
    is the same error: there is no store-generation field anywhere in the firmware, and
    the per-LMA authority is the SystemLut.
14. **`Allocation` field naming.** `+0x18` is `currentWeruId` and `+0x1A` is the
    **pending, pre-reserved** weruId — one double-buffered front, not two concurrent
    ones. The notes' warning that Allocation records only write pointers is correct.
15. **Ownership authority.** The notes prove `weruDescriptor +0` is the owner field and
    then treat it as authoritative. The field identification is right; the authority is
    not, on a dirty store (§2.11). The notes do not mention the usage log at all.
16. **`binId` as bin membership.** `weruDescriptor +11` is the binId, but 0 is also the
    default a never-binned WERU carries, so membership must come from walking the bin
    head through the descriptor chain.
17. **Cell mode by block number.** The notes tabulate "blocks 0..27 pseudo-SLC, main
    user data native TLC" as if it were a block-range rule. It is a per-WERU property,
    with a measured counterexample well above block 27. The trap in the other direction
    — filter to blocks above 27 before concluding anything about main storage — is
    still sound.
18. **Entry position versus WMA slot.** The notes do not distinguish them. Position is
    the format-time allocation order, a fixed permutation of the slot; any tool pairing
    a WMA slot with a position in the stored list is silently wrong.
19. **The "144-word tail of the LUT descriptor".** There is no such thing — the
    descriptor is 296 bytes and every field past `+8` is named. That tail belongs to
    the ECU LDPC **encoder** configuration table (it is the RU gap-solve matrix), a
    different subsystem entirely.

---

# Appendix — constant reference

```
PAGE_BYTES            18432        16384 data + 2048 spare, one flat column space
AU_STRIDE              4608        format A/D: 2 EE3 codewords
CW_BYTES               2304        one stored EE3 codeword
AUS_PER_PAGE              4        format A/D  (format B: 5 AUs of 3252 B)
MSG_BYTES              4104        8 aux + 4096 user
AU logical span        4110        MSG_BYTES + 6-byte MCRC
USER_BYTES             4096
AUX_BYTES                 8
EE3_PARITY              246        per codeword
A10R_PARITY             300        per codeword

PAGES_PER_BLOCK_SLC     368        pseudo-SLC, prefix 0xDA / 0xA2
PAGES_PER_BLOCK_TLC    1104        native TLC, prefix 0xDF
LUN_SHIFT                23        row = (lun<<23) | (block<<11) | page
BLOCK_SHIFT              11
BLOCKS_PER_LUN         4096        address-field ceiling, NOT a block count
CE_COUNT                 12        pbid = 12*block + ce

BLOCKS_PER_WERU          48        4 block numbers x 12 CEs
AUS_PER_TLC_WERU     211968        48 * 1104 * 4
AUS_PER_SLC_WERU      17664        12 *  368 * 4   (12-block metadata shape)
WMA_STRIDE              192        48 blocks * 4 AUs -- CONSTANT
MAX_MSOS                 63

LFSR_BITS                53        taps (1, 49, 50, 52, 53)
FOLD_MASK            0x3FFF        seed = F(row & 0x3FFF), 16384 entries
CRC-48 poly    0x1800000000063     6-byte AU MCRC
CRC-32Q poly        0x814141AB     4-byte AU MCRC; check("123456789") = 0x3010BF7F

SYS_MAGIC            0x00535953    "SYS\0", SystemMetadata directory at LMA 0
SYS_FORMAT_VERSION   0x02000068
SYSTEM_BLOCK_MAPPING_MAGIC 0x20170620
LUTLUT_MAGIC         0x0054554C    "LUT\0"
LUT sentinels        0xFFFFFFFF / FE / FD / FC
INVALID_WERU             0x3FFF
INVALID_STRIPE_ID     0x1FFFFFF
LMA_BYTES                4096
```
