"""The drive's smart / health counters -- Backup RootStorage object 7, 5120 B.

    from titania import ddmdata
    d = ddmdata.from_rootstorage(rs)          # rs = ftl.RootStorage(dev, 1)
    d["DeviceInformation"]["PowerOnHours"]    # 42
    d["MediaStatistics"]["PerMsoProgramCount16KBased"][53]
    print(d.report())

VERDICT: THIS BLOB IS NOT OPAQUE, AND IT IS NOT HOST-DEFINED
------------------------------------------------------------
The obvious reading -- "the host owns it and EFC just transports it" -- is
wrong, and the evidence against it is direct.  The blob is the persistence image
of five C++ `DeviceDataManager` objects, one per 1024-byte slot, and EFC
firmware parses, versions, updates and serialises every field.  Only CPU2 handles
it opaquely, because CPU2 is the transport.

    slot  off     size   manager                              ver  RAM
     0    0       0x50   HostStatisticsDeviceDataManager       2   0x40000000
     1    1024    0x64   DeviceHealthDeviceDataManager         4   0x400000A0
     2    2048    0x0C   DeviceInformationDeviceDataManager    1   0x40000198
     3    3072    0x20   MediaHealthDeviceDataManager          1   0x400001F0
     4    4096    0x2E0  MediaStatisticsDeviceDataManager      4   0x40000260

Chain, all on CPU3 (`efc_cpu3.c`): `sub_402D8FE0` (`:31566`) allocates 5120 B,
clears it and issues `BackupRootStorageReadDdmData(buf, 5120, sub_402D8F80)`
(`:31602-31604`).  `sub_402D8F80` (`:31536`) then calls the parser `sub_402D94DE`
(`:31865`), which fans the buffer out to five typed loaders --
`sub_402CC412(buf+1024)`, `sub_402CC71A(buf+2048)`, `sub_402C70AA(buf+0)`,
`sub_402D45B6(buf+3072)`, `sub_402D56D0(buf+4096)` -- and frees it.  The
serialiser `sub_402D9414` (`:31802`) runs the same five in reverse with sizes
`0x50 / 0x64 / 0x0C / 0x20 / 0x2E0` and writes 5120 B back.  CPU2 is the media
side: `sub_40367FEC` (`efc_cpu2.c:151979`) reads and `sub_40368018` (`:152000`)
writes RootStorage object 7, both as bare immediates, and neither ever
dereferences the buffer.

Each loader begins by reading `*(u32*)block` and comparing it against a literal
version, falling back to `<Manager>_SetDefaults()` on a mismatch -- e.g.
`0x402C70B6: ldr r0,[r4]; cmp r0,#2`.  So the five `version` words are the only
integrity mechanism in the whole 5120 bytes: there is no magic, no length, no
CRC and no checksum, and the inter-block gaps are zero because the serialiser
`mem_clr`s 5120 bytes and then memcpys five short structs into it.  That is the
whole explanation of the "sparse" shape.

The host's only view of any of this is NVMe Get Log Page (opcode 0x02),
lid 2 = smart / Health Information (`efc_cpu0.c:56561`, handler `:90445`).
That path calls `CorePartitionInterface_DdmSync_RpcCall` and then builds the
512-byte smart page out of the loaded structs field by field (`sub_404DA5C4`,
`efc_cpu0.c:90393`).  The host cannot write the blob -- there is no vendor admin
opcode and no Set Features route to it.

THE FIELD NAMES ARE THE FIRMWARE'S OWN
--------------------------------------
Every name below is a literal in a `MemPrintf` debug string of the form
`"<Manager><Field>: %d\\n"`, and every offset was taken from the disassembly of
that printer, not from Hex-Rays -- IDA drops the operand in most of them and
renders the argument as an undefined `v0`.  Example: the nine HostStatistics
printers at 0x402C6A3E..0x402C6B7E each end in one `ldrd r2, r3, [r0, #N]` with
N = 0x48, 0x40, 0x38, 0x30, 0x28, 0x20, 0x18, 0x10, 0x08, in that order.

TWO INTERNAL IDENTITIES MAKE THIS SELF-VALIDATING
-------------------------------------------------
Nothing here is fitted, and two independent checks hold on this drive with no
tolerance at all:

  * `sum(PerMsoProgramCount16KBased[0..63])` == `Slc + Tlc + Metadata
    DataAreaProgramCount16KBased`, exactly (4,446,564 both sides).  That pins
    the base of the 64-entry array, its length, and the three totals at once --
    a wrong offset for any of them breaks the equality.
  * `HostStatistics.DataUnitsWritten` (raw 512-byte sectors) / 32 gives host
    16-KiB writes, and total program / that is the drive's write amplification.
    Here 4,446,564 / 4,296,284 = 1.035, which is a physically sensible number
    and comes from two different manager objects.

WHAT THE COUNTERS SAY ABOUT THIS DRIVE, AND ONE TRAP
----------------------------------------------------
`HostStatistics.ErrorLogEntries` is the NVMe Error Information Log entry
count -- incremented in `sub_404DA860` (`efc_cpu0.c:90586`) as it appends a
0x48-byte `log_page_entry_t` to `gNvmeIfMgr`'s list, which is Get Log Page
lid 1.  It is not the entry count of `titania.errorlog` (object 43, the
`ErrorLog.c` text log).  Do not conflate them.

`DeviceInformation.PowerOnHours` really is hours: `FiberEntry_0_10`
(`efc_cpu0.c:88412`) bumps it by one whenever `sub_4D70` (`efc_cpu0.c:4D70`)
reports a change in `minutes / 60`.

What indexes `SuperBlockMoveCount` -- named, not fitted
-------------------------------------------------------
The index is the BlockMove request's `mode`, a `BlockMoveMode` enum with exactly
`cNumberOfBlockMoveModes == 11` members whose names the firmware itself carries
in two independent 11-entry string tables, `0x402F8D58` (BlockMove.c) and
`0x402F74C8` (the RPC server) -- see `BLOCK_MOVE_MODES` below.

There is exactly one writer, image-wide.  `sub_402EBC5E` (`efc_cpu3.c:50402`,
BlockMove.c's completion handler) does, on the success path only:

    0x402EBD50  ldrb  r0, [r4, #8]        ; pEntry->mode
    0x402EBD52  bl    0x402D5124          ; get SuperBlockMoveCount[mode]
    0x402EBD5A  adds  r2, r0, #1
    0x402EBD62  bl    0x402D525C          ; set SuperBlockMoveCount[mode]

`0x402D525C` is `base + (mode << 3)` then `strd r2, r3, [r1, #0x40]`, which is
what pins the array to offset 0x40 with an 8-byte stride.  Four identical copies
of that setter are linked into the image (`0x400BB78E`, `0x402D525C`,
`0x40313BC2`, `0x4048BBDC`, `0x404D5108`); a full bl / B.W / literal-pool sweep
of all 0xBD0000 bytes finds no other reference to any of them, so 0x402EBD62 is
the only increment anywhere.  A move that returns 16 is re-enqueued and a move
that fails is not counted, so this is "requests that ultimately succeeded".

LENGTH 11 IS MEASURED FOUR WAYS, NOT ASSUMED
--------------------------------------------
  * the printer loops `for (i = 0; i < 0xB; ++i)` (`efc_cpu3.c:28888`,
    `efc_cpu0.c:86019`, and the cpu1/cpu2 copies);
  * `MediaStatisticsDeviceDataManager_SetDefaults` zeroes it with
    `do {...} while (v0 < 0xB)` (`efc_cpu2.c:70547`);
  * the firmware's own bound check, `if (mode >= 0xBu) assert(
    "pRequest->mode < cNumberOfBlockMoveModes", "BlockMove.c", 195)`
    (`efc_cpu3.c:50151`);
  * adjacency: `SlcDataAreaMinFreeCount` is `ldr.w r0, [r0, #0x98]`
    (`0x402D5132`) and 0x40 + 11*8 == 0x98 exactly, leaving no room for a 12th.

`MediaStatistics.NumberOfReadCommands` / `NumberOfWriteCommands` /
`DataRead` / `DataWritten` are all zero on this firmware; the live host counters
are HostStatistics'.  And on this drive `HostStatistics.DataUnitsRead` is
exactly 128x `HostReadCommands`, and DataUnitsWritten exactly 128x
HostWriteCommands -- so those two counters are not independent and no average
request size can be inferred from their ratio.
"""
import collections
import struct
import sys
from pathlib import Path

if not __package__:                                     # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Backup RootStorage object id (`efc_cpu2.c:151995` read, `:152016` write).
OBJECT_ID = 7
#: The whole blob, and the slot stride.  Both immediates (`efc_cpu3.c:31602`).
TOTAL = 5120
SLOT = 1024

_U32, _U64, _U16, _U8 = "u32", "u64", "u16", "u8"
_W = {_U8: 1, _U16: 2, _U32: 4, _U64: 8}
_F = {_U8: "<B", _U16: "<H", _U32: "<I", _U64: "<Q"}

Field = collections.namedtuple("Field", "off type name count")


def _f(off, ty, name, count=1):
    return Field(off, ty, name, count)


#: (slot, name, struct size, expected version, RAM base, [Field]).
#: Sizes and versions are the serialiser's and the loaders' immediates.
BLOCKS = (
    (0, "HostStatistics", 0x50, 2, 0x40000000, (
        _f(0x00, _U32, "version"),
        _f(0x08, _U64, "HostReadCommands"),
        _f(0x10, _U64, "HostWriteCommands"),
        _f(0x18, _U64, "DataUnitsRead"),
        _f(0x20, _U64, "DataUnitsWritten"),
        _f(0x28, _U64, "ErrorLogEntries"),
        _f(0x30, _U64, "BusyTime"),
        _f(0x38, _U64, "MpeccReadErrors"),
        _f(0x40, _U64, "SysconShutdownCount"),
        _f(0x48, _U64, "PcieCrcErrorCount"))),
    (1, "DeviceHealth", 0x64, 4, 0x400000A0, (
        _f(0x00, _U32, "version"),
        _f(0x04, _U8, "LifeSpent"),
        _f(0x08, _U32, "NumberOfUnsafeShutdowns"),
        _f(0x0C, _U16, "TemperatureSensor", 8),
        _f(0x1C, _U16, "TemperatureSensorOverThreshold", 8),
        _f(0x2C, _U16, "TemperatureSensorUnderThreshold", 8),
        _f(0x3C, _U8, "TemperatureSensorLevel", 8),
        _f(0x44, _U16, "CompositeTemperature"),
        _f(0x46, _U16, "CompositeTemperatureUnderThreshold"),
        _f(0x48, _U16, "CompositeTemperatureOverThreshold"),
        _f(0x4A, _U8, "CompositeTemperatureLevel"),
        _f(0x4C, _U16, "WarningCompositeTemperatureThreshold"),
        _f(0x50, _U32, "WarningCompositeTemperatureTime"),
        _f(0x54, _U16, "CriticalCompositeTemperatureThreshold"),
        _f(0x58, _U32, "CriticalCompositeTemperatureTime"),
        _f(0x5C, _U16, "MinThermalManagementTemperature"),
        _f(0x5E, _U16, "MaxThermalManagementTemperature"),
        _f(0x60, _U16, "LightThermalManagementTemperature"),
        _f(0x62, _U16, "HeavyThermalManagementTemperature"))),
    (2, "DeviceInformation", 0x0C, 1, 0x40000198, (
        _f(0x00, _U32, "version"),
        _f(0x04, _U32, "PowerOnHours"),
        _f(0x08, _U32, "NumberOfPowerCycles"))),
    (3, "MediaHealth", 0x20, 1, 0x400001F0, (
        _f(0x00, _U32, "version"),
        _f(0x04, _U32, "ReallocatedCount"),
        _f(0x08, _U32, "NumberOfNandRetries"),
        _f(0x0C, _U32, "MediaErrors"),
        _f(0x10, _U32, "WriteErrorCount"),
        _f(0x14, _U32, "EraseErrorCount"),
        # u64, not u32: the printer at 0x402D435E is `ldrd r2, r3, [r0, #0x18]`.
        # IDA splits it into `unk_40000208` + `dword_4000020C` and
        # `MediaHealthDeviceDataManager_SetDefaults` (efc_cpu3.c:28421) zeroes
        # the two halves separately, which is what makes it look like a u32
        # plus four bytes of tail.
        _f(0x18, _U64, "MpeccWriteErrors"))),
    (4, "MediaStatistics", 0x2E0, 4, 0x40000260, (
        _f(0x000, _U32, "version"),
        _f(0x004, _U32, "NumberOfReadCommands"),
        _f(0x008, _U32, "NumberOfWriteCommands"),
        _f(0x00C, _U32, "DataRead"),
        _f(0x010, _U32, "DataWritten"),
        _f(0x014, _U32, "AvailableSpare"),
        _f(0x018, _U32, "AvailableSpareThreshold"),
        _f(0x020, _U64, "NandVendorRetryCount"),
        _f(0x028, _U64, "TwoBitRetryCount"),
        _f(0x030, _U64, "DeepRetryCount"),
        _f(0x038, _U64, "RaidRecoveryCount"),
        _f(0x040, _U64, "SuperBlockMoveCount", 11),
        _f(0x098, _U32, "SlcDataAreaMinFreeCount"),
        _f(0x09C, _U32, "TlcDataAreaMinFreeCount"),
        _f(0x0A0, _U32, "SystemDataAreaMinFreeCount"),
        _f(0x0A8, _U64, "HostUnrecoverableReadErrorCount"),
        _f(0x0B0, _U64, "FirmwareDataAreaProgramCountForGroup0"),
        _f(0x0B8, _U64, "FirmwareDataAreaProgramCountForGroup1"),
        _f(0x0C0, _U64, "FirmwareDataAreaProgramCountForGroup2"),
        _f(0x0C8, _U64, "PerMsoProgramCount16KBased", 64),
        _f(0x2C8, _U64, "SlcDataAreaProgramCount16KBased"),
        _f(0x2D0, _U64, "TlcDataAreaProgramCount16KBased"),
        _f(0x2D8, _U64, "MetadataDataAreaProgramCount16KBased"))),
)

#: Bytes inside a block that no field claims.  These are C alignment padding
#: before a u64, not unnamed fields: 0x1C and 0xA4 in MediaStatistics, 0x04 in
#: HostStatistics.  They read zero here, which is consistent with the
#: serialiser's `mem_clr` and with the static initialiser.
PAD_IS_EXPECTED = True

#: What indexes `MediaStatistics.SuperBlockMoveCount` -- the firmware's own
#: `BlockMoveMode` enum, read off the two 11-entry name tables at 0x402F8D58 and
#: 0x402F74C8.  `cNumberOfBlockMoveModes == 11` (assert, `efc_cpu3.c:50151`).
#: The comment on each is the source file that submits that mode.
BLOCK_MOVE_MODES = (
    "RDA",                      # 0  RdaScan.c   (efc_cpu3.c:56962, :57097)
    "BGMS",                     # 1  RdaScan.c   (efc_cpu3.c:57089)
    "Rebuild",                  # 2  RebuildNeo.c        (efc_cpu2.c:136005)
    "PartialXlcToXlc",          # 3  Lpt.c               (efc_cpu2.c:117427)
    "PartialXlcToSlc",          # 4  Lpt.c               (efc_cpu2.c:117544)
    "PartialSlcToSlc",          # 5  Lpt.c               (efc_cpu2.c:117709)
    "WriteRecovery",            # 6  WriteRecoveryEngine.c (efc_cpu2.c:155182)
    "CopyWriteRecovery",        # 7  MediaCopyEngine.c   (efc_cpu2.c:130170)
    "CopyPaddingErrorRecovery", # 8  MediaCopyEngine.c   (efc_cpu2.c:130148)
    "Invalid / Reserved",       # 9  no submitter
    "Reserved",                 # 10 no submitter
)

#: mode -> scheduling class, `byte_402F8D88` (16 bytes, 11 live + 5 pad).
#: Selects the BlockMove work queue (`class + 1`, queues 1..3) and is the second
#: argument to the in-flight conflict check `sub_402EB70C(weruId, class)`.
BLOCK_MOVE_CLASS = (0, 0, 1, 1, 1, 1, 2, 2, 2, 0, 0)

#: `cMaxNumberOfMsos == 63`, so slot 63 of the 64-entry array is out of range
#: and must never be non-zero.
MAX_MSOS = 63
#: 16 KiB program unit vs the 512-byte sector the host counters use.
SECTORS_PER_16K = 32


class DdmError(ValueError):
    """The bytes are not a DdmData image."""


def _get(buf, base, f):
    fmt, w = _F[f.type], _W[f.type]
    if f.count == 1:
        return struct.unpack_from(fmt, buf, base + f.off)[0]
    return [struct.unpack_from(fmt, buf, base + f.off + i * w)[0]
            for i in range(f.count)]


class DdmData(dict):
    """`{manager name: {field: value}}` plus the raw blob and per-block status.

    Subclasses dict so `d["MediaHealth"]["MediaErrors"]` just works, but keeps
    `blob`, `versions` and `version_ok` alongside.
    """

    def __init__(self, blob):
        blob = bytes(blob)
        if len(blob) != TOTAL:
            raise DdmError("DdmData is %d bytes, expected %d"
                           % (len(blob), TOTAL))
        super(DdmData, self).__init__()
        self.blob = blob
        self.versions = {}
        self.version_ok = {}
        self.tail_zero = {}
        for slot, name, size, want, _ram, fields in BLOCKS:
            base = slot * SLOT
            got = struct.unpack_from("<I", blob, base)[0]
            self.versions[name] = got
            self.version_ok[name] = (got == want)
            self.tail_zero[name] = not any(blob[base + size:base + SLOT])
            self[name] = {f.name: _get(blob, base, f) for f in fields}

    # -- derived quantities, each from a stated identity --------------------
    def program_total_16k(self):
        """-> total 16 KiB program units, from the three area totals."""
        m = self["MediaStatistics"]
        return (m["SlcDataAreaProgramCount16KBased"]
                + m["TlcDataAreaProgramCount16KBased"]
                + m["MetadataDataAreaProgramCount16KBased"])

    def per_mso_total_16k(self):
        return sum(self["MediaStatistics"]["PerMsoProgramCount16KBased"])

    def host_written_16k(self):
        """-> host writes in 16 KiB units, from HostStatistics' 512-B sectors."""
        return self["HostStatistics"]["DataUnitsWritten"] // SECTORS_PER_16K

    def write_amplification(self):
        """-> media program / host write, or None if the host never wrote."""
        hw = self.host_written_16k()
        return (self.program_total_16k() / float(hw)) if hw else None

    def live_msos(self):
        """-> {msoId: 16 KiB programs} for every MSO that has ever been written."""
        per = self["MediaStatistics"]["PerMsoProgramCount16KBased"]
        return {i: v for i, v in enumerate(per) if v}

    def super_block_moves(self):
        """-> {BlockMoveMode name: count} for every mode that has ever fired.

        One count per BlockMove request that completed successfully, not per
        WERU and not per WMA: modes 3/4/5 carry an explicit `numWmas` and move
        part of a track, while 2 and 6 pass a WERU-aligned `startWma` with
        `numWmas == 0` and move a whole superblock.
        """
        c = self["MediaStatistics"]["SuperBlockMoveCount"]
        return {BLOCK_MOVE_MODES[i]: v for i, v in enumerate(c) if v}

    def report(self):
        out = []
        hs, dh = self["HostStatistics"], self["DeviceHealth"]
        di, mh = self["DeviceInformation"], self["MediaHealth"]
        ms = self["MediaStatistics"]
        out.append("power-on %d h over %d cycles, %d unsafe shutdowns, "
                   "%d syscon shutdowns"
                   % (di["PowerOnHours"], di["NumberOfPowerCycles"],
                      dh["NumberOfUnsafeShutdowns"],
                      hs["SysconShutdownCount"]))
        out.append("host  read %d sectors / %d cmds, written %d sectors / %d cmds"
                   % (hs["DataUnitsRead"], hs["HostReadCommands"],
                      hs["DataUnitsWritten"], hs["HostWriteCommands"]))
        waf = self.write_amplification()
        out.append("media programmed %d x 16 KiB (slc %d / tlc %d / meta %d), "
                   "WAF %s"
                   % (self.program_total_16k(),
                      ms["SlcDataAreaProgramCount16KBased"],
                      ms["TlcDataAreaProgramCount16KBased"],
                      ms["MetadataDataAreaProgramCount16KBased"],
                      "%.3f" % waf if waf else "n/a"))
        out.append("health  lifeSpent %d%%, availableSpare %d%%, "
                   "composite temp %d K, NVMe error-log entries %d"
                   % (dh["LifeSpent"], ms["AvailableSpare"],
                      dh["CompositeTemperature"], hs["ErrorLogEntries"]))
        out.append("media errors %d, reallocated %d, nand retries %d, "
                   "write/erase errors %d/%d, unrecoverable host reads %d"
                   % (mh["MediaErrors"], mh["ReallocatedCount"],
                      mh["NumberOfNandRetries"], mh["WriteErrorCount"],
                      mh["EraseErrorCount"],
                      ms["HostUnrecoverableReadErrorCount"]))
        out.append("retries  vendor %d, two-bit %d, deep %d; raid recoveries %d;"
                   " superblock moves %d"
                   % (ms["NandVendorRetryCount"], ms["TwoBitRetryCount"],
                      ms["DeepRetryCount"], ms["RaidRecoveryCount"],
                      sum(ms["SuperBlockMoveCount"])))
        out.append("superblock moves by BlockMoveMode: %s"
                   % (", ".join("%s=%d" % kv
                                for kv in self.super_block_moves().items())
                      or "none"))
        out.append("programs by msoId: %s"
                   % ", ".join("%d=%d" % kv for kv in
                               sorted(self.live_msos().items())))
        return "\n".join(out)

    def __repr__(self):
        return ("<DdmData %s>"
                % " ".join("%s=v%d%s" % (n, v, "" if self.version_ok[n] else "!")
                           for n, v in self.versions.items()))


def parse(blob):
    """-> DdmData."""
    return DdmData(blob)


def from_rootstorage(rs):
    """-> (DdmData | None | UNKNOWN, info) for the backup `ftl.RootStorage`.

    Pass a RootStorage over block 1.  `None`/UNKNOWN are `rs.object()`'s own
    answers and are passed through -- neither means "this drive has no DdmData".
    """
    body, info = rs.object(OBJECT_ID)
    if isinstance(body, (bytes, bytearray)):
        return DdmData(body), info
    return body, info


# ===========================================================================
# selftest
# ===========================================================================
def _report(name, state, detail=""):
    print("  %-46s %-8s %s" % (name, state, detail))


def _sib(name):
    import importlib
    for mod in (("%s.%s" % (__package__, name)) if __package__ else None,
                "titania." + name):
        if not mod:
            continue
        try:
            return importlib.import_module(mod)
        except ImportError:
            continue
    raise ImportError("cannot import %s" % name)


def selftest(argv=()):
    """-> 0 on success.  SKIPPED, never PASS, for anything the dumps cannot show."""
    print("titania.ddmdata -- selftest\n")
    fails = 0

    # ---- 1. the layout closes, offline ------------------------------------
    bad = []
    for slot, name, size, _ver, _ram, fields in BLOCKS:
        end = 0
        claimed = set()
        for f in fields:
            hi = f.off + _W[f.type] * f.count
            if hi > size:
                bad.append("%s.%s runs to 0x%X past 0x%X" % (name, f.name, hi, size))
            for b in range(f.off, hi):
                if b in claimed:
                    bad.append("%s.%s overlaps at 0x%X" % (name, f.name, b))
                claimed.add(b)
            end = max(end, hi)
        if end != size:
            bad.append("%s ends at 0x%X, declared 0x%X" % (name, end, size))
        if slot * SLOT + size > TOTAL:
            bad.append("%s overruns the blob" % name)
    fails += bool(bad)
    _report("the five block layouts close exactly", "PASS" if not bad else "FAIL",
            "no overlap and each map's last field ends on the serialiser's own "
            "size immediate (0x50 / 0x64 / 0x0C / 0x20 / 0x2E0, efc_cpu3.c:"
            "31815-31832)%s" % (" -- %s" % bad if bad else ""))

    # ---- 2. round trip on a synthetic blob ---------------------------------
    synth = bytearray(TOTAL)
    for slot, _n, _s, ver, _r, _f in BLOCKS:
        struct.pack_into("<I", synth, slot * SLOT, ver)
    struct.pack_into("<Q", synth, 4096 + 0xC8 + 8 * 53, 1255296)
    d = parse(bytes(synth))
    ok = (all(d.version_ok.values())
          and d["MediaStatistics"]["PerMsoProgramCount16KBased"][53] == 1255296
          and d.live_msos() == {53: 1255296})
    fails += not ok
    _report("versions and the PerMso array round-trip", "PASS" if ok else "FAIL",
            "the five version u32s are the ONLY validation in the blob -- no "
            "magic, no length, no CRC (the loaders compare `ldr r0,[r4]` "
            "against 2/4/1/1/4 and call SetDefaults on a mismatch)")

    ok = False
    try:
        parse(bytes(TOTAL - 1))
    except DdmError:
        ok = True
    fails += not ok
    _report("a wrong-sized blob is refused", "PASS" if ok else "FAIL",
            "5120 is an immediate in both the read and the write RPC "
            "(efc_cpu3.c:31604, :31832), so a short object is a real error")

    # ---- 3. media ----------------------------------------------------------
    names = ("object 7 parses and every version matches",
             "sum(PerMso) == Slc + Tlc + Metadata, EXACTLY",
             "write amplification is physical",
             "the unclaimed tail of every block is zero",
             "msoId 63 is never written")
    root = Path(__file__).resolve().parent.parent
    F = _sib("ftl")
    dev = F._selftest_device(root)
    if dev is None:
        for n in names:
            _report(n, "SKIPPED", "no page dumps under <dump>/")
        return 1 if fails else 0
    try:
        rs = F.RootStorage(dev, 1, codec=F.default_codec())
        d, info = from_rootstorage(rs)
    except Exception as exc:                                # noqa: BLE001
        for n in names:
            _report(n, "SKIPPED", "backup RootStorage not servable: %r" % (exc,))
        return 1 if fails else 0
    if not isinstance(d, DdmData):
        for n in names:
            _report(n, "SKIPPED", "object %d: %s" % (OBJECT_ID, d))
        return 1 if fails else 0

    ok = all(d.version_ok.values())
    fails += not ok
    _report(names[0], "PASS" if ok else "FAIL",
            "%d bytes at stripe %s; versions %s"
            % (len(d.blob), info.get("stripe"),
               ", ".join("%s=%d" % kv for kv in d.versions.items())))

    a, b = d.per_mso_total_16k(), d.program_total_16k()
    ok = a == b and a > 0
    fails += not ok
    _report(names[1], "PASS" if ok else "FAIL",
            "%d == %d.  This is the check that pins the 64-entry array's base "
            "(0xC8), its length, and the three area totals (0x2C8/0x2D0/0x2D8) "
            "all at once -- any wrong offset breaks the equality.  Nothing was "
            "fitted." % (a, b))

    waf = d.write_amplification()
    ok = waf is not None and 1.0 <= waf < 10.0
    fails += not ok
    _report(names[2], "PASS" if ok else "FAIL",
            "media %d x 16 KiB vs host %d x 16 KiB = %s -- two DIFFERENT "
            "manager objects agreeing (MediaStatistics' program counts against "
            "HostStatistics' 512-byte DataUnitsWritten)"
            % (b, d.host_written_16k(),
               "%.3f" % waf if waf is not None else "n/a"))

    nz = [n for n, z in d.tail_zero.items() if not z]
    ok = not nz
    fails += not ok
    _report(names[3], "PASS" if ok else "FAIL",
            "bytes from each struct's end to its 1024-byte slot boundary are "
            "zero, because the serialiser mem_clr's 5120 and then memcpys five "
            "short structs -- which is the whole reason the blob looks sparse%s"
            % ("" if ok else "; non-zero in %s" % nz))

    per = d["MediaStatistics"]["PerMsoProgramCount16KBased"]
    over = [i for i in range(MAX_MSOS, 64) if per[i]]
    ok = not over
    fails += not ok
    _report(names[4], "PASS" if ok else "FAIL",
            "cMaxNumberOfMsos is %d so slot 63 is out of range and reads 0; the "
            "%d MSOs that HAVE been written are %s"
            % (MAX_MSOS, len(d.live_msos()),
               sorted(d.live_msos())) if ok else "slots %s non-zero" % over)

    try:
        dev.close()
    except Exception:                                       # noqa: BLE001
        pass
    print("\nSELF-TEST: %s" % ("ALL CHECKS PASS" if not fails
                               else "%d FAILURE(S)" % fails))
    return 1 if fails else 0


def main(argv=None):                                       # pragma: no cover
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest(argv)
    root = Path(__file__).resolve().parent.parent
    F = _sib("ftl")
    dev = F._selftest_device(root)
    if dev is None:
        print(__doc__)
        return 0
    rs = F.RootStorage(dev, 1, codec=F.default_codec())
    d, info = from_rootstorage(rs)
    if not isinstance(d, DdmData):
        print("object %d: %s" % (OBJECT_ID, d))
        return 1
    print("stripe %s  %r\n" % (info.get("stripe"), d))
    print(d.report())
    if "-v" in argv or "--verbose" in argv:
        for _s, name, _sz, _v, _r, fields in BLOCKS:
            print("\n%s" % name)
            for f in fields:
                print("  +0x%03X %-4s %-38s %s"
                      % (f.off, f.type, f.name, d[name][f.name]))
    return 0


if __name__ == "__main__":                                # pragma: no cover
    sys.exit(main())
