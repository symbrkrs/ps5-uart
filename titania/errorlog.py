"""The drive's persisted firmware log -- Bootstrap RootStorage object 43.

    from titania import errorlog
    rec = errorlog.from_rootstorage(rs)          # rs = ftl.RootStorage(dev, 2)
    rec.version                                  # b'09P07M3 '
    rec.entries                                  # [Entry(...)]
    rec.entries[0].category                      # 'firmware'
    rec.errors()                                 # [] -- see below

WHAT THIS OBJECT IS
-------------------
`ErrorLog.c` keeps one append-only text buffer in shared SRAM at 0x40183100 and
mirrors it to NAND after every append.  All four CPUs link the same module and
all four write the same SRAM window; the shared control block lives at 0x900968.

The media record is the SRAM buffer verbatim -- a 32-byte header followed by
`dataLength` bytes of ASCII:

    +0x00 u64   0x0000DEADBEEFCAFE      magic, checked as one 64-bit load
    +0x08 u16   2                       record format version
    +0x0A char[9] "09P07M3 \\0"          the firmware version that owns the log
    +0x13 --                            never written
    +0x14 u32   dataLength              bytes of payload that follow
    +0x18 u8    isFull                  1 once an append did not fit
    +0x19 --                            never written
    +0x20 ...   dataLength bytes of newline-terminated ASCII

`sub_402C3098` (`efc_cpu3.c:15392-15402`, and the same code at `efc_cpu0.c:74532`,
`efc_cpu1.c:27677`, `efc_cpu2.c:48523`) stamps `+0x14`/`+0x18` and then asks
`BootstrapRootStorageWriteErrorLog` for `dataLength + 32` bytes -- which is
exactly the object size on media, and the reason `HEADER + dataLength == size` is
a hard check here rather than a convention.

THE FIELD WIDTHS ARE PROVEN BY THE MEDIA, NOT ASSUMED
-----------------------------------------------------
IDA renders every one of those stores as `MEMORY[addr] = value` with no width.
Two of the three widths are settled by the bytes themselves: this drive's record
carries 0x87 at +0x13 and seven non-zero bytes at +0x19..+0x1F, so the `+0x14`
store cannot be wider than 32 bits and the `+0x18` store cannot be wider than 8.
Those gaps are uninitialised SRAM, not fields -- no site in any of the four
CPUs writes them.  The third width came from disassembly: at 0x402C3622 the
stamper is `strh r0, [r4, #8]` and at 0x402C3E38 the validator is
`ldrh r0, [r2, #8]; cmp r0, #2`, so `+8` is 16 bits.  A 32-bit read there would
compare against the first two characters of the version string and could never
match, so no valid record would ever validate.

THERE IS EXACTLY ONE RECORD FORMAT VERSION
------------------------------------------
`+8` is written as a literal 2 in all four CPUs and read back as `== 2` in all
four.  No other value is written or accepted anywhere in the image -- this is an
enumerated negative, not an unsearched field.

CAPACITY, AND WHY "ONE LINE" IS A REAL FINDING
----------------------------------------------
The reserve step (`sub_402C3346`, `ErrorLog.c:713`, `efc_cpu3.c:15584`) refuses
an append when `65532 - reservedLength < len` and latches `isFull`.  So the log
holds up to `MAX_PAYLOAD` = 65532 bytes -- hundreds of lines -- and `isFull`
records whether anything was ever dropped.

Every line is written by `sub_402C34C6` (`ErrorLog_Write`) and every caller
formats it with a leading `"%010d:"` uptime field.  The firmware image contains
one `FIRMWARE:` format and 190-odd `ERROR:` formats (`"%010d:ERROR:..."`,
grep-able in all four `efc_cpu*.c`), so the categories below are the firmware's
own vocabulary.

`"%010d:FIRMWARE:%s\\n"` is a banner, not a fault.  It is written from exactly two
places: `sub_402C35E8` (`efc_cpu3.c:15760`), the completion of ErrorLog
initialisation, and `sub_402C3198` (`:15465`), the reset that follows an erase.
The initialiser appends it only when the persisted record's version string
differs from the running firmware's -- `sub_402C3DF0` (`efc_cpu3.c:16262-16276`)
sets the "append" flag from `memcmp(...) != 0`, and leaves it set when there is
no valid record at all.  So a banner marks "this firmware version started
writing this log", and one banner with nothing after it means the drive logged
no error.

WHERE THE UPTIME COMES FROM, AND WHAT IT CANNOT TELL YOU
--------------------------------------------------------
The `%010d` field is `sub_402C0738() / 1000`.  That is
`sub_395C = (dword_9066E4 - dword_11020014) >> 1` (`efc_cpu3.c:8861`), a
free-running HalTimer millisecond count.  It is seconds of uptime inside one
power cycle -- not power-on hours, not a date, not a global sequence.  It has
no epoch on media, it wraps every ~49.7 days, and nothing in the record carries
an activation counter, a store generation or a sequence number.  `Entry.uptime_s`
is therefore reported as a duration and this module offers no
conversion to any absolute time.

The drive's own power-on-hours counter is a different object entirely --
`DdmData`, Backup RootStorage object 7, DeviceInformation block `+4`.

WHEN THE LOG IS INITIALISED
---------------------------
`ErrorLog_Initialize` (`sub_402C38F4`) is step 4 of the full mount FSM.
`sub_402E0C60` (`efc_cpu3.c:39375-39392`) selects `off_402F8620` for a normal
mount, and that table reads
`{sub_402DE79C, sub_402E0CAE, sub_402E0CDC, sub_402C2FC8, sub_402C38F4, ...}` in
the image at 0x402F8620 -- ErrorLog init immediately after
`BootStrapRootStorageReadSystemMetadataInformation`.  The short-mount table
`off_402F8610` has only four steps and never touches it.  The logical-format FSM
`off_402F8270` runs it too, at step 0 and again at step 22.
"""
import collections
import re
import struct
import sys
from pathlib import Path

if not __package__:                                     # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Bootstrap RootStorage object id.  A bare immediate in the RPC servers:
#: `sub_40365F24(..., 43, 0, ...)` writes and `sub_40365F90(..., 43, 0, ...)`
#: reads (`efc_cpu2.c:58356` / `:58421`).
OBJECT_ID = 43

#: The SRAM window.  Header, then payload; `sub_402C34C6` copies an append to
#: `0x40183120 + offset` (`efc_cpu3.c:15641`, the literal 1075327264).
SRAM_BASE = 0x40183100
SRAM_PAYLOAD = 0x40183120

HEADER = 0x20                     # payload starts here, in SRAM and on media
MAGIC = 0x0000DEADBEEFCAFE        # one 64-bit load, `ldrd r3, r0, [r2]`
FORMAT_VERSION = 2                # u16 at +8; the only value in the image
VERSION_LEN = 9                   # "09P07M3 " + nul, compared with memcmp(9)

#: `65532 - reservedLength >= len` gates every append (`efc_cpu3.c:15584`).
MAX_PAYLOAD = 65532
#: The read path refuses a media object larger than this (`efc_cpu3.c:16231`,
#: rendered by IDA as `&dword_7144[9143]` = 0x7144 + 9143*4).
MAX_OBJECT = 0x10020              # 65568 = 32 + 65536

#: Bytes the firmware never writes.  Non-zero here, which is what proves the
#: `+0x14` and `+0x18` stores are 32- and 8-bit.
UNWRITTEN = ((0x13, 0x14), (0x19, 0x20))

#: The category vocabulary, taken from the firmware's own format strings.
CAT_FIRMWARE = "FIRMWARE"         # the version banner; not a fault
CAT_ERROR = "ERROR"               # a real fault or diagnostic
CAT_EVENT = "WriteEventNotificationWithLabel"
CATEGORIES = (CAT_FIRMWARE, CAT_ERROR, CAT_EVENT)

_LINE = re.compile(r"^(\d{10}):([^:]+):?(.*)$", re.S)

Entry = collections.namedtuple("Entry", "uptime_s category text raw")


class ErrorLogError(ValueError):
    """The bytes are not an ErrorLog record."""


class ErrorLog(object):
    """A parsed object-43 record.

    Attributes:
      magic, format_version, version, data_length, is_full  -- the header
      payload   -- the `data_length` ASCII bytes
      entries   -- [Entry], one per newline-terminated line
      padding   -- {offset: bytes} for the two never-written gaps
      size      -- len(body); always `HEADER + data_length`
    """

    __slots__ = ("magic", "format_version", "version", "data_length",
                 "is_full", "payload", "entries", "padding", "size", "body")

    def __init__(self, body):
        body = bytes(body)
        if len(body) < HEADER:
            raise ErrorLogError("record is %d bytes, shorter than the %d-byte "
                                "header" % (len(body), HEADER))
        self.body = body
        self.size = len(body)
        self.magic = struct.unpack_from("<Q", body, 0)[0]
        if self.magic != MAGIC:
            raise ErrorLogError("magic 0x%016X, expected 0x%016X"
                                % (self.magic, MAGIC))
        self.format_version = struct.unpack_from("<H", body, 8)[0]
        if self.format_version != FORMAT_VERSION:
            raise ErrorLogError(
                "record format version %d; the firmware writes and accepts only "
                "%d (enumerated in all four CPUs)"
                % (self.format_version, FORMAT_VERSION))
        raw_ver = body[0x0A:0x0A + VERSION_LEN]
        self.version = raw_ver.split(b"\x00", 1)[0]
        self.data_length = struct.unpack_from("<I", body, 0x14)[0]
        self.is_full = body[0x18]
        if HEADER + self.data_length != self.size:
            raise ErrorLogError(
                "dataLength %d + %d-byte header != object size %d; the writer "
                "asks for exactly dataLength+32 bytes (efc_cpu3.c:15399)"
                % (self.data_length, HEADER, self.size))
        self.payload = body[HEADER:HEADER + self.data_length]
        self.padding = {lo: body[lo:hi] for lo, hi in UNWRITTEN}
        self.entries = parse_entries(self.payload)

    # -- views --------------------------------------------------------------
    def errors(self):
        """-> the error lines.  An empty list is the interesting answer."""
        return [e for e in self.entries if e.category == CAT_ERROR]

    def banners(self):
        """-> the `FIRMWARE:` version banners."""
        return [e for e in self.entries if e.category == CAT_FIRMWARE]

    def is_banner_only(self):
        """-> True iff nothing but version banners was ever logged."""
        return bool(self.entries) and not [e for e in self.entries
                                           if e.category != CAT_FIRMWARE]

    def summary(self):
        return ("ErrorLog v%d  fw %s  %d byte%s of payload, %d entr%s "
                "(%d ERROR), isFull=%d"
                % (self.format_version, self.version.decode("latin-1"),
                   self.data_length, "" if self.data_length == 1 else "s",
                   len(self.entries), "y" if len(self.entries) == 1 else "ies",
                   len(self.errors()), self.is_full))

    def __repr__(self):
        return "<%s>" % self.summary()


def parse_entries(payload):
    """-> [Entry] from the raw payload bytes.

    Every writer formats `"%010d:<CATEGORY>:..."`, so the split is on the
    firmware's own convention, not on a guess.  A line that does not match is
    kept with `category=None` rather than dropped -- an unparsed line is
    evidence, and silently discarding it is how a log looks empty.
    """
    out = []
    for raw in bytes(payload).split(b"\n"):
        if not raw:
            continue
        m = _LINE.match(raw.decode("latin-1"))
        if not m:
            out.append(Entry(None, None, raw.decode("latin-1"), raw))
            continue
        secs, cat, rest = m.group(1), m.group(2), m.group(3)
        out.append(Entry(int(secs), cat, rest, raw))
    return out


def parse(body):
    """-> ErrorLog.  Raises ErrorLogError if `body` is not one."""
    return ErrorLog(body)


def from_rootstorage(rs):
    """-> (ErrorLog | None | UNKNOWN, info) for a `ftl.RootStorage`.

    `None` means the catalogue says the object is absent; the module's UNKNOWN
    sentinel means present on media but on a stripe this backing cannot serve.
    Both are passed through unchanged -- neither is "the drive has no log".
    """
    body, info = rs.object(OBJECT_ID)
    if isinstance(body, (bytes, bytearray)):
        return ErrorLog(body), info
    return body, info


def format_seconds(s):
    """-> 'HHh MMm SSs' for an uptime, with no pretence of an absolute time."""
    if s is None:
        return "?"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return "%dh %02dm %02ds" % (h, m, sec)


# ===========================================================================
# selftest
# ===========================================================================
def _report(name, state, detail=""):
    print("  %-46s %-8s %s" % (name, state, detail))


def _synthetic(payload, version=b"09P07M3 ", is_full=0, fmt=FORMAT_VERSION,
               magic=MAGIC, pad13=0x87, pad19=b"\x82`p\x06\x00x\x18"):
    """Build a record the way `sub_402C3098` would, padding included."""
    h = bytearray(HEADER)
    struct.pack_into("<Q", h, 0, magic)
    struct.pack_into("<H", h, 8, fmt)
    h[0x0A:0x0A + VERSION_LEN] = (version + b"\x00" * VERSION_LEN)[:VERSION_LEN]
    h[0x13] = pad13
    struct.pack_into("<I", h, 0x14, len(payload))
    h[0x18] = is_full
    h[0x19:0x20] = pad19
    return bytes(h) + bytes(payload)


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
    print("titania.errorlog -- selftest\n")
    fails = 0

    # ---- 1. the structure, offline ----------------------------------------
    line = b"0000080140:FIRMWARE:09P07M3 \n"
    rec = parse(_synthetic(line))
    ok = (rec.data_length == len(line) and rec.size == HEADER + len(line)
          and rec.version == b"09P07M3 " and rec.is_full == 0
          and len(rec.entries) == 1
          and rec.entries[0].category == CAT_FIRMWARE
          and rec.entries[0].uptime_s == 80140)
    fails += not ok
    _report("header + one line round-trips", "PASS" if ok else "FAIL",
            "32-byte header then dataLength bytes; the writer asks for exactly "
            "dataLength+32 (efc_cpu3.c:15399), so size is a CHECK not a guess")

    # ---- 2. every rejection the firmware itself performs -------------------
    bad = []
    #: spelled as thunks rather than `_synthetic(line, **kw)`: splatting a
    #: `dict[str, int]` makes a type checker test that int against every
    #: keyword, including the two `bytes` ones, for two errors that cannot
    #: happen at run time.  Same two calls, same order.
    for why, make in (("magic", lambda: _synthetic(line, magic=MAGIC ^ 1)),
                      ("format version", lambda: _synthetic(line, fmt=3))):
        try:
            parse(make())
            bad.append(why)
        except ErrorLogError:
            pass
    try:                                   # size / dataLength disagreement
        parse(_synthetic(line)[:-1])
        bad.append("truncated")
    except ErrorLogError:
        pass
    try:
        parse(_synthetic(line))            # positive control
    except ErrorLogError as e:
        bad.append("positive control: %s" % e)
    fails += bool(bad)
    _report("rejects what the firmware rejects", "PASS" if not bad else "FAIL",
            "u64 magic (ldrd at 0x402C3E1C), u16 format version (ldrh at "
            "0x402C3E38 -- a 32-bit read there would compare against '09' and "
            "never match), and dataLength+32 == size%s"
            % (" -- %s" % bad if bad else ""))

    # ---- 3. the category vocabulary is the firmware's ----------------------
    got = parse_entries(b"0000000001:ERROR:Null Pointer Access @0x1 sp=0x2\n"
                        b"0000000002:FIRMWARE:09P07M3 \n"
                        b"not a log line\n")
    ok = ([e.category for e in got] == [CAT_ERROR, CAT_FIRMWARE, None]
          and got[0].uptime_s == 1 and got[2].raw == b"not a log line")
    fails += not ok
    _report("entry split follows '%010d:CATEGORY:'", "PASS" if ok else "FAIL",
            "one FIRMWARE format and ~190 ERROR formats exist in the image; an "
            "unparsed line is KEPT with category None, never dropped")

    # ---- 4. media ----------------------------------------------------------
    root = Path(__file__).resolve().parent.parent
    F = _sib("ftl")
    dev = F._selftest_device(root)
    if dev is None:
        for n in ("the record is on media, in both instances",
                  "isFull is clear -- nothing was dropped",
                  "no ERROR entry was ever logged",
                  "it has not been rewritten since the store began"):
            _report(n, "SKIPPED", "no page dumps under <dump>/")
        return 1 if fails else 0

    codec = F.default_codec()
    found = {}
    for blk in (2, 4):
        rs = F.RootStorage(dev, blk, codec=codec)
        try:
            body, info = from_rootstorage(rs)
        except (LookupError, ErrorLogError) as exc:
            found[blk] = ("ERR", repr(exc), None)
            continue
        found[blk] = (body, info, rs)

    live = {b: v for b, v in found.items()
            if isinstance(v[0], ErrorLog)}
    if not live:
        for n in ("the record is on media, in both instances",
                  "isFull is clear -- nothing was dropped",
                  "no ERROR entry was ever logged",
                  "it has not been rewritten since the store began"):
            _report(n, "SKIPPED",
                    "object %d not servable from this backing: %s"
                    % (OBJECT_ID, {b: v[0] for b, v in found.items()}))
        return 1 if fails else 0

    bodies = {b: v[0].body for b, v in live.items()}
    one = next(iter(live.values()))[0]
    ok = len(live) == 2 and len(set(bodies.values())) == 1
    fails += not ok
    _report("the record is on media, in both instances",
            "PASS" if ok else ("FAIL" if len(live) == 2 else "SKIPPED"),
            "blocks 2/3 and 4/5 both serve object %d; %d byte%s, %s"
            % (OBJECT_ID, one.size, "" if one.size == 1 else "s",
               "byte-identical" if len(set(bodies.values())) == 1
               else "THEY DIFFER: %s" % {b: v.hex() for b, v in bodies.items()}))

    ok = one.is_full == 0 and one.data_length <= MAX_PAYLOAD
    fails += not ok
    _report("isFull is clear -- nothing was dropped", "PASS" if ok else "FAIL",
            "the buffer holds %d bytes and %d are used, so the %d-byte payload "
            "is the WHOLE log and not a truncated tail"
            % (MAX_PAYLOAD, one.data_length, one.data_length))

    errs = one.errors()
    ok = not errs and one.is_banner_only()
    fails += not ok
    _report("no ERROR entry was ever logged", "PASS" if ok else "FAIL",
            "the only entry is the %s banner at uptime %s -- a banner is "
            "written when the persisted version string differs from the running "
            "one (sub_402C3DF0, efc_cpu3.c:16273), NOT on a fault%s"
            % (one.version.decode("latin-1"),
               format_seconds(one.entries[0].uptime_s if one.entries else None),
               "" if ok else "; ERROR lines: %r" % (errs,)))

    # 4d.  Recency, from the store's own stripe order.
    rs = next(iter(live.values()))[2]
    _h, ents, _ce, _pg = rs.catalogue()
    mine = ents[OBJECT_ID][0]
    older = sorted(o for o, e in enumerate(ents)
                   if e[0] != F.INVALID_STRIPE and e[0] < mine)
    newer = [o for o, e in enumerate(ents)
             if e[0] != F.INVALID_STRIPE and e[0] > mine]
    ok = len(older) <= 2 and len(newer) >= 40
    fails += not ok
    _report("it has not been rewritten since the store began",
            "PASS" if ok else "FAIL",
            "object %d sits at stripe %d, older than %d of %d live objects and "
            "newer than only %s -- a stripe number is the store's own write "
            "order, so the log has been untouched for this instance's whole "
            "life.  (It dates the COPY, not the content: a rotation rewrites "
            "live objects verbatim, which is why the two neighbours carry a "
            "SegmentedBuffer headerSequenceNumber of 2.)"
            % (OBJECT_ID, mine, len(newer), len(newer) + len(older) + 1,
               older or "nothing"))

    # 4e.  The never-written gaps really are unwritten.
    gaps = one.padding
    nz = [hex(k) for k, v in gaps.items() if set(v) - {0}]
    ok = bool(nz)
    _report("the padding gaps carry SRAM residue",
            "PASS" if ok else "SKIPPED",
            "+0x13 = %s and +0x19..0x1F = %s are non-zero, which is what proves "
            "the +0x14 store is 32-bit and the +0x18 store 8-bit -- IDA renders "
            "both widthless.  No site in any of the four CPUs writes them."
            % (gaps[0x13].hex(), gaps[0x19].hex())
            if ok else "this record's gaps are zero; the width argument needs "
                       "the disassembly instead")

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
    codec = F.default_codec()
    for blk in (2, 4):
        rs = F.RootStorage(dev, blk, codec=codec)
        try:
            rec, info = from_rootstorage(rs)
        except (LookupError, ErrorLogError) as exc:
            print("block %d: %r" % (blk, exc))
            continue
        print("block %d  stripe %s  %r" % (blk, info.get("stripe"), rec))
        if isinstance(rec, ErrorLog):
            for e in rec.entries:
                print("    +%-12s %-9s %s"
                      % (format_seconds(e.uptime_s), e.category or "?",
                         e.text.rstrip()))
    return 0


if __name__ == "__main__":                                # pragma: no cover
    sys.exit(main())
