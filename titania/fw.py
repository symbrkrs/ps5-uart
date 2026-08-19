#!/usr/bin/env python3
"""Firmware images on Titania NAND: the FW_ID table, the headers, extraction.

    from titania import DumpDevice
    from titania import fw

    dev = DumpDevice()
    for img in fw.find_images(dev):
        print(img, img.verify()["ok"])

    g1 = fw.extract(dev, 0x12)
    open("fw_0x12.bin", "wb").write(g1.blob())

Everything here reads pages through `titania.device.Device`, so the same code
runs against a dump directory or a live board, and every address is composed
with `titania.geometry`.  Nothing in this module descrambles or ECC-corrects
anything itself -- that is the codec layer's job.


THREE FIRMWARE GROUPS, TWO COMPLETELY DIFFERENT ON-MEDIA LAYOUTS
---------------------------------------------------------------
    group 0   fw 0x10                blocks 6,7   CE 0-5 and CE 6-11   striped
    group 1   fw 0x12, 0x14, 0x15    blocks 8,9   CE 0-5 and CE 6-11   striped
    group 2   fw 1, fw 2             block 0      CE 0-5               replicated

Groups 0/1 are the EFC's own firmware area: 18432-byte pages of four 4608-byte
AUs, EE3 LDPC, 6-byte AU MCRC, scrambled -- the codec's FORMAT_A.  They are
striped across six CEs and two blocks and must be de-interleaved (below).

Group 2 is the boot ROM's EFC/eap image pair: the same NAND page, but carved
into 5 chunks of 4 x (512 payload + 300 A10R parity) with a 4-byte digest per
chunk = 10240 user bytes per page.  It is not striped -- CE 1..5 hold five
byte-identical replicas of the same block-0 rows -- and its bodies are
AES-128-CBC ciphertext, so extraction legitimately stops at the ciphertext.


THE GROUP 0/1 INTERLEAVE -- THE PART THAT IS EASY TO GET WRONG
--------------------------------------------------------------
Block 0 page 0 holds a header (identical on every CE) that the firmware parses
in `FwDataManager::ParseGroup01Headers` (`efc_cpu1.c:33914`):

    4 x u32 field_0                      byte offsets of the four elements
    4 x { u32 count; count x 3 u32 }     the four elements, 12 entries each
    u16 checksum                         sum of (lo16 + hi16) over every u32

An entry is three little-endian u32 on the wire, and the field order is not the
obvious one -- reading it as (index, ce, row) is backwards and produces a
plausible but wrong picture:

    word0  byte0 = CE   byte1 = grouping tag   byte2 = plane   byte3 = LUN
    word1  = block
    word2  = base row-in-block

The four elements are {group 0, group 1} x {bank a = CE 0-5, bank B = CE 6-11}:

    element 0   group 0   bank a   ce 0-5    blocks 6,7
    element 1   group 0   bank B   ce 6-11   blocks 6,7
    element 2   group 1   bank a   ce 0-5    blocks 8,9
    element 3   group 1   bank B   ce 6-11   blocks 8,9

and the two banks are mirrors of the same image, not two firmware versions.
Within an element the 12 entries are ordered `ce6b8 ce6b9 ce7b8 ce7b9 ...
ce11b8 ce11b9`, i.e. 6 CEs x 2 blocks, and the two blocks of a CE are
parallelism, not redundancy.

    * REASSEMBLY ORDER *

        unit  = page_in_block * 12 + entry_index          one unit = one page
        entry = unit % 12          page = unit // 12
        (ce, block) = element.entries[entry].ce / .block
        row         = geometry.compose_row(block,
                                           (entry.base_row + page) % 368)

    and the image is the units' 16384-byte user payloads concatenated in
    increasing unit order.

The hardware issues one command per two entries -- `HIBYTE(field_2) >> 5` is a
block count set from `1 << lunsPerChannel` = 2, so a payload command reads the
same page of two adjacent blocks, 32768 bytes at a time, and 12 entries give 6
commands per lap.  That pairing changes nothing about the unit order above; it
only explains why entries come in adjacent-block pairs and why the CE of an odd
entry is never consulted by the sequencer.  This module reads one page at a
time through `Device` and does not model the pairing.

A FW_ID then names a contiguous run of units inside one group (`sub_4048F3E2`,
`efc_cpu1.c:33816`) -- see `FW_IDS`.


BANK CHOICE, AND WHY "CE0 UNREADABLE" IS NOT A FORMAT PROBLEM
-------------------------------------------------------------
The firmware reads the header from bank B (`fw_pos_0` walks CE 6..0xB) but tries
the image from bank a first (`fw_pos_1 = 0`), falling back to bank B only on
failure.  On this board CE0 is dead, so bank a can never yield 2 of its 12
entries and bank B is the only complete copy of groups 0/1 -- while for group 2
the real image must be kept on CE0 and the replicas on CE 1..5 are what save us.

Either way that is a DATA availability fact about one board, not a statement
about the format.  Every result here separates the two: `verify()` reports
`missing` (pages the Device could not serve, with the dead CEs called out) apart
from `mcrc`/`checksum` (bytes that were served and did not check out).
`bank=None` picks whichever bank the Device can actually serve in full,
preferring a because that is what the firmware does.


PROVENANCE
----------
NAND_DRIVER_NOTES.md section 0b, and the memory notes nand-group01-format,
nand-group2-format, emu-fw-image-interleave, emu-fw-group2-sccmd.  Sections of
NAND_DRIVER_NOTES.md after section 0 predate it and are wrong where they
disagree (notably section 5's entry-field reading).
"""
import hashlib
import struct
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


try:
    from . import geometry as geom
    from .device import DeviceError
except ImportError:                                   # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import DeviceError

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- the codec --
# Nothing in this module reimplements a codec operation.  These are the only
# primitives it uses, and `titania.codec` and `titania_nand` both provide them --
# codec.py is preferred, titania_nand is the fall-back so `fw.py` still works in
# a checkout where the package layer is incomplete.
_CODEC_NEEDS = ("au_logical", "au_mcrc", "au_is_erased", "seed_state",
                "keystream", "ecc_available", "ecc_correct_page",
                "AU_STRIDE", "MSG_BYTES", "USER_BYTES", "AUX_BYTES")
_CODEC = None
_G2 = None


def _sibling(name):
    """Import a sibling module of this package.

    `from . import x` fails when fw.py is run as a script rather than imported
    as `titania.fw`, which silently sent the self-test down every fall-back path
    at once.  Try the package form too before giving up.
    """
    import importlib
    for mod in (__package__ + "." + name if __package__ else None,
                "titania." + name):
        if not mod:
            continue
        try:
            return importlib.import_module(mod)
        except Exception:
            continue
    return None


def _codec():
    """The AU codec: `titania.codec` when it provides everything, else the
    reference implementation `titania_nand` it is built on."""
    global _CODEC
    if _CODEC is None:
        c = _sibling("codec")
        if c is None or not all(hasattr(c, n) for n in _CODEC_NEEDS):
            if str(_ROOT) not in sys.path:
                sys.path.insert(0, str(_ROOT))
            from . import titania_nand as c          # noqa: F811
        _CODEC = c
    return _CODEC


def _g2mod():
    """Group-2 page geometry.  `nd_g2extract` until codec.py absorbs it.

    Only its pure functions are used -- descramble_page, parse_header,
    check_page, scan_images, decrypt_payload and the geometry constants.  Its
    own file-scanning entry points are not used: pages come from
    the Device.
    """
    global _G2
    if _G2 is None:
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        from . import nd_g2extract as g2
        _G2 = g2
    return _G2


# ------------------------------------------------------------------ geometry --
PAGE_USER_BYTES = geom.AU_DATA * geom.AUS_PER_PAGE_TLC     # 16384, one unit
HEADER_BLOCK = 0
HEADER_PAGE = 0
ENTRIES_PER_ELEMENT = 12
ELEMENT_COUNT = 4
CHECKSUM_OFFSET = 0x260                    # 16 + 4*(4 + 12*12) bytes of u32

#: element index -> (group, bank).  bank 0 = CE 0-5, bank 1 = CE 6-11.
ELEMENT_MAP = {0: (0, 0), 1: (0, 1), 2: (1, 0), 3: (1, 1)}
#: group -> the two blocks it is striped over
GROUP_BLOCKS = {0: (6, 7), 1: (8, 9)}
#: the CEs of each bank
BANK_CES = {0: tuple(range(0, 6)), 1: tuple(range(6, 12))}
#: bank B is where the firmware reads the header from (`fw_pos_0` = 6..0xB)
HEADER_BANK = 1

#: One definition, in `titania.geometry`, so the region map and the extractor
#: cannot drift apart.  `selftest` cross-checks GROUP_BLOCKS against it too.
GROUP2_BLOCK = geom.FW_GROUP2_BLOCK
GROUP2_CES = geom.FW_GROUP2_CES
GROUP2_PAGE_BYTES = 10240                  # 5 chunks x 4 x 512 user bytes

#: The ROM's load window per group-2 FW_ID: (start_row, pages).  These are
#: Firmware constants (NAND_DRIVER_NOTES.md section 5, from `InitForFw`), not a
#: guess read off the media, and they are what makes the extents stable: fw 1 is
#: rows 1..154 and fw 2 is rows 155..227.  Both windows run past the header's
#: declared `payload_len` -- for fw 1 the extra three rows are proven plaintext
#: image data, and for fw 2 the extra 46 rows are the region earlier notes called
#: "unexplained".  They are not unexplained; they are inside fw 2's load window.
GROUP2_LOAD = {1: (1, 154), 2: (155, 73)}
#: rows scanned for group-2 headers by default: enough to cover both windows
GROUP2_SCAN_ROWS = 232


class FwIdSpec:
    """One row of the FW_ID table: which group, and which run of units."""

    __slots__ = ("fw_id", "group", "start_unit", "units", "note")

    def __init__(self, fw_id, group, start_unit, units, note=""):
        self.fw_id, self.group = fw_id, group
        self.start_unit, self.units = start_unit, units
        self.note = note

    @property
    def size(self):
        """Bytes, for a group 0/1 id.  Group 2 ids get theirs from the header."""
        return None if self.group == 2 else self.units * PAGE_USER_BYTES

    def __repr__(self):
        return ("<FwIdSpec fw_id=%#x group=%d start=%s units=%s%s>"
                % (self.fw_id, self.group, self.start_unit, self.units,
                   " " + self.note if self.note else ""))


#: The FW_ID table.  Groups 0/1 from `sub_4048F3E2` (`efc_cpu1.c:33816`), where
#: `v3 = &stru_90C720 + 0x98 * fw_pos_1` and the group's element sits at v3+0x010
#: (group 0) or v3+0x140 (group 1) -- a flat array of four 0x98-byte struc_14 at
#: 0x10, 0xA8, 0x140, 0x1D8, i.e. elements 0..3.  Totals: group 0 = 4096 units =
#: 67,108,864 B, group 1 = 128 + 3712 + 128 = 3968 units = 65,011,712 B, both
#: matching the distributed fw_group_N.bin containers to the byte.
#: Group 2's two ids are `fw_type` values in its own page header, not units.
FW_IDS = {
    0x10: FwIdSpec(0x10, 0, 0, 4096, "whole group 0, encrypted end to end"),
    0x12: FwIdSpec(0x12, 1, 0, 128, "SLB2 container"),
    0x14: FwIdSpec(0x14, 1, 128, 3712, ""),
    0x15: FwIdSpec(0x15, 1, 3840, 128, ""),
    0x01: FwIdSpec(0x01, 2, None, None, "EFC image, block 0 row 1"),
    0x02: FwIdSpec(0x02, 2, None, None, "EAP image, block 0 row 155"),
}

#: group -> total units, for the completeness check a header must satisfy
GROUP_UNITS = {0: 4096, 1: 3968}


# --------------------------------------------------------- page-level decode --
_KS_CACHE = {}
_KS_CACHE_MAX = 64


def _keystream(row, n):
    """Cached codec keystream for a row.  Four AUs share one row state, and the
    codec's generator is a bit-at-a-time Python loop, so caching it is a 4x on
    every page and the only reason a 64 MiB extract finishes in minutes."""
    key = (row, n)
    ks = _KS_CACHE.get(key)
    if ks is None:
        c = _codec()
        ks = c.keystream(c.seed_state(row), n)
        if len(_KS_CACHE) >= _KS_CACHE_MAX:
            _KS_CACHE.clear()
        _KS_CACHE[key] = ks
    return ks


def decode_group01_page(raw, row, correct=None):
    """One raw 18432-byte group 0/1 page -> (16384 user bytes, report).

    report = {'mcrc': (n_ok, n_checked), 'erased': n, 'corrected': n|None}

    The AU MCRC is checked on the stored bytes, so it needs no seed and no ECC
    and is a genuinely independent verdict on the bytes the Device served.

    `correct` -- None (default) runs the EE3 decoder only on a page whose MCRC
    already failed, which costs nothing on clean media and repairs the rest;
    True always corrects; False never does, so a caller who wants to see the
    media exactly as stored can have that.  The two detectors are independent
    (a CRC-48 over the stored bytes and an LDPC syndrome), so a page that fails
    the first and passes it after the second has agreed with itself twice.
    """
    if correct is None:
        user, rep = decode_group01_page(raw, row, correct=False)
        c = _codec()
        if (rep["mcrc"][0] == rep["mcrc"][1]
                or not getattr(c, "ecc_available", lambda: False)()):
            return user, rep
        user2, rep2 = decode_group01_page(raw, row, correct=True)
        rep2["repaired"] = rep["mcrc"][1] - rep["mcrc"][0]
        return user2, rep2
    c = _codec()
    raw = bytes(raw)
    if len(raw) != 8 * 2304:
        raise ValueError("group 0/1 page must be 18432 bytes, got %d" % len(raw))
    ncorr = None
    if correct and getattr(c, "ecc_available", lambda: False)():
        raw, ncorr, _conv = c.ecc_correct_page(raw)

    ks = _keystream(row, c.MSG_BYTES)
    out = bytearray(PAGE_USER_BYTES)
    n_ok = n_chk = n_erased = 0
    for i in range(4):
        au = raw[i * c.AU_STRIDE:(i + 1) * c.AU_STRIDE]
        lg = c.au_logical(au)
        if c.au_is_erased(au):
            n_erased += 1
            out[i * c.USER_BYTES:(i + 1) * c.USER_BYTES] = b"\xFF" * c.USER_BYTES
            continue
        stored = lg[:c.MSG_BYTES]
        n_chk += 1
        n_ok += bytes(lg[c.MSG_BYTES:c.MSG_BYTES + 6]) == c.au_mcrc(stored, 6)
        plain = bytes(a ^ b for a, b in zip(stored, ks))
        out[i * c.USER_BYTES:(i + 1) * c.USER_BYTES] = plain[c.AUX_BYTES:]
    return bytes(out), {"mcrc": (n_ok, n_chk), "erased": n_erased,
                        "corrected": ncorr, "repaired": 0}


def decode_group2_page(raw, row):
    """One raw 18432-byte group 2 page -> (10240 user bytes, report).

    report = {'mcrc': (n_ok, n_checked), 'tail_ff': bool, 'erased': bool}
    """
    c, g2 = _codec(), _g2mod()
    raw = bytes(raw)
    chk = g2.check_page(raw)
    plain = g2.descramble_page(raw, c.seed_state(row))
    return plain, {"mcrc": chk["mcrc"], "tail_ff": chk["tail_ff"],
                   "erased": chk["erased"], "notes": chk["notes"]}


def check_group2_page(raw):
    """Did this group-2 PAGE read?  -> the `nd_g2extract.check_page` dict.

    The seed-free structural verdict for `geometry.REGION_FW_GROUP2`, and the
    one a sweep must use there instead of a FORMAT_A decode.  A group-2 page is
    20 codewords of 512 payload + 300 parity with a 4-byte CRC-32Q AU digest
    every 2048 payload bytes -- nothing at all like FORMAT_A's 4 x (4096 + 246 +
    246) -- so decoding one as FORMAT_A fails on every page.  That is what made
    the 1840 pages of block 0 on CE 1..5 (368 on each of five dies, every one of
    them good) show up as read failures in a store sweep.

    `ok` is true when the page is erased, or when its tail is erased and all
    five AU digests match with no digest inconsistency.  Measured over this
    store's 1840 group-2 pages: 1836 pass 5/5, and the four that do not are
    exactly `nd_g2extract.KNOWN_MEDIA_ERROR_ROWS` -- rows 43, 196, 245 and 287,
    one CE each, each a single flipped payload bit localised with `nd_g2mcrc`.
    Those four are real media errors, recoverable from the other four replicas
    (`nd_g2extract.check_replicas`), not read failures.

    Five further rows (13, 231, 260, 313, 337) carry a flipped bit in LDPC
    PARITY on one CE.  The AU digest cannot see those by construction, so a pass
    here is not a claim that all 18432 bytes are right -- cross-CE replica
    comparison is the only check that strong, and it needs 2+ dies.
    """
    return _g2mod().check_page(bytes(raw))


# ------------------------------------------------------------- the header ----
class FwEntry:
    """One 12-byte interleave-set entry: one erase block of one image."""

    __slots__ = ("ce", "tag", "plane", "lun", "block", "base_row", "words")

    def __init__(self, words):
        self.words = tuple(words)
        w0, w1, w2 = words
        self.ce = w0 & 0xFF
        self.tag = (w0 >> 8) & 0xFF
        self.plane = (w0 >> 16) & 0xFF
        self.lun = (w0 >> 24) & 0xFF
        self.block = w1
        self.base_row = w2

    def __repr__(self):
        return "ce%db%d" % (self.ce, self.block) + (
            "r%d" % self.base_row if self.base_row else "")


class FwElement:
    """One header element: a stripe of `count` erase blocks over 6 CEs."""

    def __init__(self, index, count, entries, field_0=None):
        self.index = index
        self.count = count
        self.entries = list(entries)
        self.field_0 = field_0
        self.group, self.bank = ELEMENT_MAP.get(index, (None, None))

    @property
    def ces(self):
        return tuple(sorted({e.ce for e in self.entries}))

    @property
    def blocks(self):
        return tuple(sorted({e.block for e in self.entries}))

    def units(self, pages_per_block=geom.PAGES_PER_BLOCK_SLC):
        return len(self.entries) * pages_per_block

    def locate(self, unit, pages_per_block=geom.PAGES_PER_BLOCK_SLC):
        """unit -> (ce, block, page).  The interleave rule; see the docstring.

        `unit = page_in_block * n_entries + entry_index`, and the page steps the
        entry's own base row modulo the block length (the `% 368` the sequencer
        does).  n_entries is taken from the header, not assumed to be 12.
        """
        n = len(self.entries)
        if not n:
            raise ValueError("element %d has no entries" % self.index)
        page, entry = divmod(unit, n)
        e = self.entries[entry]
        return e.ce, e.block, (e.base_row + page) % pages_per_block

    def __repr__(self):
        return ("<FwElement %d group=%s bank=%s %d entries ce=%s blocks=%s>"
                % (self.index, self.group, self.bank, len(self.entries),
                   list(self.ces), list(self.blocks)))


class Group01Header:
    """Block 0 page 0, parsed exactly as `ParseGroup01Headers` reads it."""

    def __init__(self, field_0, elements, checksum_stored, checksum_computed,
                 length, source_ce=None, plain=None):
        self.field_0 = field_0
        self.elements = elements
        self.checksum_stored = checksum_stored
        self.checksum_computed = checksum_computed
        self.length = length
        self.source_ce = source_ce
        self.plain = plain

    @property
    def checksum_ok(self):
        return self.checksum_stored == self.checksum_computed

    @property
    def empty(self):
        """No entries in any element.

        The zero-page trap: an all-zero page parses as four zero-count elements
        with a stored checksum of 0 that trivially equals the computed 0, so
        `checksum_ok` alone will happily bless a page that is not a header at
        all.  The firmware calls this the `is_empty` path and skips to the next
        CE; so does `read_group01_header`.  This is how block 0 of CE 0..5 --
        which is group 2's block, an entirely different format -- first passed
        itself off as a valid group 0/1 header here.
        """
        return not any(e.entries for e in self.elements)

    def element_for(self, group, bank):
        for e in self.elements:
            if e.group == group and e.bank == bank:
                return e
        return None

    def covers(self, group):
        """Does this header's element hold enough units for the whole group?

        The firmware needs `count >= ceil(units / pages_per_block)`; too small a
        count silently truncates the image and a count > 255 hangs the parser.
        """
        need = GROUP_UNITS.get(group)
        e = self.element_for(group, HEADER_BANK)
        if need is None or e is None:
            return None
        return e.units() >= need

    @classmethod
    def parse(cls, plain, source_ce=None):
        """16384 descrambled user bytes (or any prefix >= 0x262) -> header.

        Returns None if it does not parse as a header at all.  A header that
        parses but whose checksum fails is returned with `checksum_ok` False --
        that is exactly the state the firmware treats as "try the next CE".
        """
        d = bytes(plain)
        if len(d) < CHECKSUM_OFFSET + 2:
            return None
        off = 0
        ck = 0

        def u32():
            nonlocal off, ck
            v = struct.unpack_from("<I", d, off)[0]
            off += 4
            ck = (ck + (v & 0xFFFF) + (v >> 16)) & 0xFFFF
            return v

        field_0 = [u32() for _ in range(ELEMENT_COUNT)]
        elements = []
        for i in range(ELEMENT_COUNT):
            n = u32()
            if n > 255 or off + 12 * n + 2 > len(d):
                return None            # >255 hangs the real parser; refuse it
            ents = [FwEntry([u32() for _ in range(3)]) for _ in range(n)]
            elements.append(FwElement(i, n, ents, field_0[i]))
        stored = struct.unpack_from("<H", d, off)[0]
        return cls(field_0, elements, stored, ck, off + 2, source_ce, d)

    def __repr__(self):
        return ("<Group01Header ce=%s checksum %#06x/%#06x %s>"
                % (self.source_ce, self.checksum_stored, self.checksum_computed,
                   "ok" if self.checksum_ok else "BAD"))


def read_group01_header(dev, ces=None, correct=None):
    """Read and parse the group 0/1 header from the first CE that yields one.

    -> (Group01Header|None, report).  `report` lists every CE tried and why it
    was rejected, so "no header" can be told apart from "no readable CE".

    Only CE 6..0xB are searched by default, and that is deliberate.  The
    firmware reads the header there (`fw_pos_0` walks 6..0xB, advancing only on
    failure), and block 0 of CE 0..5 is group 2's block -- a completely
    different on-media format whose row 0 descrambles to a near-zero page that
    parses as an empty group 0/1 header with a self-consistent checksum.  Pass
    `ces=` explicitly if you really want to look there; `Group01Header.empty`
    is what stops it being believed.
    """
    if ces is None:
        ces = list(BANK_CES[HEADER_BANK])
    present = set(dev.present_ces())
    report = {"tried": [], "header_ce": None, "agree": None, "digests": {}}
    found = None
    for ce in ces:
        if ce not in present:
            report["tried"].append((ce, "not served by this device"))
            continue
        if ce in geom.CE_DEAD:
            report["tried"].append((ce, "CE is dead on this board"))
            continue
        try:
            raw = dev.read_page(ce, HEADER_BLOCK, HEADER_PAGE, slc=True)
        except DeviceError as e:
            report["tried"].append((ce, "read failed: %s" % e))
            continue
        try:
            plain, prep = decode_group01_page(
                raw, geom.compose_row(HEADER_BLOCK, HEADER_PAGE), correct)
        except Exception as e:                       # pragma: no cover
            report["tried"].append((ce, "decode failed: %r" % e))
            continue
        h = Group01Header.parse(plain, source_ce=ce)
        if h is None:
            report["tried"].append((ce, "does not parse as a group 0/1 header"))
            continue
        if h.empty:
            report["tried"].append(
                (ce, "parses but every element is empty -- not a header "
                     "(see Group01Header.empty)"))
            continue
        report["digests"][ce] = hashlib.sha1(plain[:h.length]).hexdigest()[:12]
        report["tried"].append(
            (ce, "checksum %s, mcrc %d/%d"
             % ("ok" if h.checksum_ok else "BAD", *prep["mcrc"])))
        if found is None and h.checksum_ok:
            found = h
            report["header_ce"] = ce
    if report["digests"]:
        report["agree"] = len(set(report["digests"].values())) == 1
    return found, report


def fw_id_table(source=None):
    """The FW_ID table, parsed.

    -> {fw_id: dict}.  With no argument you get the static table.  Give it a
    Device, a Group01Header or a descrambled header page and each group 0/1 row
    is resolved against that header: the concrete element, its CEs and blocks,
    and whether the element is large enough to hold the id's units.
    """
    header = None
    if source is not None:
        if isinstance(source, Group01Header):
            header = source
        elif isinstance(source, (bytes, bytearray, memoryview)):
            header = Group01Header.parse(source)
        else:
            header, _ = read_group01_header(source)

    out = {}
    for fw_id, spec in sorted(FW_IDS.items()):
        row = {"fw_id": fw_id, "group": spec.group, "start_unit": spec.start_unit,
               "units": spec.units, "size": spec.size, "note": spec.note,
               "elements": {}, "covered": None}
        if spec.group == 2:
            row["block"] = GROUP2_BLOCK
            row["ces"] = GROUP2_CES
            row["note"] = spec.note + "; size comes from the on-media header"
        else:
            row["blocks"] = GROUP_BLOCKS[spec.group]
            if header is not None:
                for bank in (0, 1):
                    e = header.element_for(spec.group, bank)
                    if e is None:
                        continue
                    row["elements"][bank] = {
                        "index": e.index, "entries": len(e.entries),
                        "ces": e.ces, "blocks": e.blocks,
                        "units": e.units(),
                        "enough": e.units() >= spec.start_unit + spec.units}
                row["covered"] = all(v["enough"]
                                     for v in row["elements"].values()) or None
        out[fw_id] = row
    return out


# -------------------------------------------------------------- the images ---
class FirmwareImage:
    """One firmware image located on a Device.

    Attributes
        fw_id     the id the loader uses (0x10/0x12/0x14/0x15, or 1/2)
        group     0, 1 or 2
        ces       CEs the image is read from
        blocks    blocks it occupies
        rows      (first, last) page-in-block, inclusive, that it touches
        size      declared size in bytes of what `blob()` returns
        group     see above
        encrypted True when `blob()` is ciphertext (group 0 and group 2)

    `blob()` reassembles and caches; `verify()` never needs `blob()` to have
    been called but reuses it if it has.
    """

    def __init__(self, dev, fw_id, group, **kw):
        self.dev = dev
        self.fw_id = fw_id
        self.group = group
        self.ces = kw.get("ces", ())
        self.blocks = kw.get("blocks", ())
        self.rows = kw.get("rows", (None, None))
        self.size = kw.get("size")
        self.encrypted = kw.get("encrypted", False)
        self.bank = kw.get("bank")
        self.element = kw.get("element")
        self.spec = kw.get("spec")
        self.g2_header = kw.get("g2_header")
        self.header_row = kw.get("header_row")
        self.slot_rows = kw.get("slot_rows")
        #: A dead die answers (CE0 returns a constant `a7 a7 00 00 ..` page), so
        #: a Device that holds those pages will serve them without error.  Skip
        #: them by default and count them as missing; set False to see them.
        self.skip_dead_ces = kw.get("skip_dead_ces", True)
        self._blob = None
        self._report = None

    # -- content ----------------------------------------------------------
    def blob(self, correct=None, progress=None):
        """The reassembled image bytes.

        Missing pages are zero-filled and counted in `verify()['missing']`; a
        short or zero-filled blob is therefore never silently indistinguishable
        from a good one.  `correct=True` runs LDPC correction on every group 0/1
        page (slower, and only meaningful on a damaged dump).
        """
        if self._blob is None:
            if self.group == 2:
                self._blob, self._report = self._assemble_g2()
            else:
                self._blob, self._report = self._assemble_g01(correct, progress)
        return self._blob

    def header(self):
        """Parsed header fields.

        Group 2: the on-media `fw_hdr_t` (magic/fw_type/payload_len/
        payload_off/IV).  Groups 0/1: the image has no header of its own, so
        this returns the element that locates it plus the container header at
        the front of the image when there is one (group 1's SLB2).
        """
        if self.group == 2:
            return dict(self.g2_header or {})
        out = {"element": self.element.index if self.element else None,
               "bank": self.bank,
               "entries": [repr(e) for e in (self.element.entries
                                             if self.element else [])],
               "start_unit": self.spec.start_unit, "units": self.spec.units}
        if self.group == 1 and self.spec.start_unit == 0:
            out["container"] = parse_slb2(self.blob()[:0x1000])
        return out

    def verify(self):
        """Structural check -> report dict.  Never raises.

        Keys, and the distinction that matters:
            missing      pages the Device could not serve       availability
            dead_ce      of those, ones on a CE known dead      availability
            mcrc         (ok, checked) AU digests               integrity
            erased       AUs / rows that read as erased         integrity
            checksum_ok  the group 0/1 header's own u16         integrity
            container    SLB2 self-consistency, when present    integrity
            encrypted    blob() is ciphertext; that is a result, not a failure
            ok           every integrity check passed and nothing is missing
            complete     nothing is missing (availability alone)
        """
        self.blob()
        r = dict(self._report)
        r.update({"fw_id": self.fw_id, "group": self.group, "size": len(self._blob),
                  "declared_size": self.size, "encrypted": self.encrypted})
        n_ok, n_chk = r.get("mcrc", (0, 0))
        r["complete"] = not r.get("missing")
        r["ok"] = bool(r["complete"] and n_chk and n_ok == n_chk
                       and r.get("checksum_ok", True)
                       and r.get("container_ok", True)
                       and len(self._blob) == (self.size or len(self._blob)))
        return r

    # -- group 0/1 --------------------------------------------------------
    def locate(self, index):
        """Image-relative unit index -> (ce, block, page).  Group 0/1 only."""
        if self.group == 2:
            raise ValueError("group 2 is not striped; use rows/slot_rows")
        if not 0 <= index < self.spec.units:
            raise IndexError("unit %d outside fw %#x (%d units)"
                             % (index, self.fw_id, self.spec.units))
        return self.element.locate(self.spec.start_unit + index,
                                   geom.PAGES_PER_BLOCK_SLC)

    def unit(self, index, correct=None):
        """One 16384-byte unit of a group 0/1 image, without assembling it all.

        -> (user_bytes, report).  Random access into a 64 MiB image costs one
        page read, which is what makes spot-checking the interleave cheap.

        Raises DeviceError for a unit on a CE `geometry` lists as dead: such a
        die answers -- CE0 returns a constant `a7 a7 00 00 ...` page -- so
        reading it yields plausible bytes rather than an error, and a Device
        that happens to hold those pages would otherwise put them in the image.
        """
        ce, block, page = self.locate(index)
        if self.skip_dead_ces and ce in geom.CE_DEAD:
            raise DeviceError("unit %d is on CE%d, which is dead (it answers "
                              "with a constant page)" % (index, ce))
        raw = self.dev.read_page(ce, block, page, slc=True)
        return decode_group01_page(raw, geom.compose_row(block, page), correct)

    def _assemble_g01(self, correct=None, progress=None):
        el, spec = self.element, self.spec
        ppb = geom.PAGES_PER_BLOCK_SLC
        out = bytearray(spec.units * PAGE_USER_BYTES)
        missing, dead, n_ok, n_chk, n_erased, n_corr = [], set(), 0, 0, 0, 0
        for i in range(spec.units):
            unit = spec.start_unit + i
            ce, block, page = el.locate(unit, ppb)
            if self.skip_dead_ces and ce in geom.CE_DEAD:
                missing.append((unit, ce, block, page))
                dead.add(ce)
                continue
            try:
                raw = self.dev.read_page(ce, block, page, slc=True)
            except DeviceError:
                missing.append((unit, ce, block, page))
                continue
            user, rep = decode_group01_page(
                raw, geom.compose_row(block, page), correct)
            out[i * PAGE_USER_BYTES:(i + 1) * PAGE_USER_BYTES] = user
            n_ok += rep["mcrc"][0]
            n_chk += rep["mcrc"][1]
            n_erased += rep["erased"]
            n_corr += rep["corrected"] or 0
            if progress and (i % 256 == 0 or i == spec.units - 1):
                progress(i + 1, spec.units)
        rep = {"missing": missing, "dead_ce": sorted(dead),
               "mcrc": (n_ok, n_chk),
               "erased_aus": n_erased, "corrected": n_corr,
               "units": spec.units, "ces": self.ces, "blocks": self.blocks}
        if self.group == 1 and spec.start_unit == 0:
            c = parse_slb2(bytes(out[:0x1000]))
            rep["container"] = c
            rep["container_ok"] = c["ok"] if c else True
        return bytes(out), rep

    # -- group 2 ----------------------------------------------------------
    def _g2_rows(self, staged=False):
        """(first, last) payload rows.  `staged` = the ROM's whole load window.

        The declared extent comes from the page header; the load window comes
        from GROUP2_LOAD, a firmware constant.  They differ, and both are real.
        """
        h = self.g2_header
        first = h["row"] + h["payload_off"] // GROUP2_PAGE_BYTES
        last = first + h["payload_len"] // GROUP2_PAGE_BYTES - 1
        if staged:
            win = GROUP2_LOAD.get(self.fw_id)
            end = (win[0] + win[1] - 1) if win else h.get("slot_last_row", last)
            last = max(last, end)
        return first, last

    def _assemble_g2(self, staged=False):
        h = self.g2_header
        first, last = self._g2_rows(staged)
        out = bytearray()
        missing, n_ok, n_chk = [], 0, 0
        notes, repaired = [], []
        for row in range(first, last + 1):
            raw, src, rep_ce = self._g2_read(row)
            if raw is None:
                missing.append(row)
                out += b"\x00" * GROUP2_PAGE_BYTES
                continue
            if rep_ce:
                repaired.append((row, rep_ce, src))
            user, rep = decode_group2_page(raw, row)
            out += user
            n_ok += rep["mcrc"][0]
            n_chk += rep["mcrc"][1]
            notes += ["row %d (ce%d): %s" % (row, src, n) for n in rep["notes"]]
        blob = bytes(out if staged else out[:h["payload_len"]])
        rep = {"missing": missing, "dead_ce": [c for c in self.ces
                                               if c in geom.CE_DEAD],
               "mcrc": (n_ok, n_chk), "rows": (first, last), "notes": notes,
               "replica_repairs": repaired,
               "header_row": h["row"], "iv": h["iv"].hex(),
               "zero_span_ok": h["zero_span_ok"], "erased_aus": 0,
               "ces": self.ces, "blocks": (GROUP2_BLOCK,)}
        rep["checksum_ok"] = h["zero_span_ok"]
        return blob, rep

    def _g2_read(self, row):
        """-> (raw, ce_used, replaced_ces).  Replica-aware.

        CE 1..5 hold byte-identical copies, so a row whose AU MCRC fails on one
        die is very likely readable from another -- this board has four such
        single-bit media errors in block 0.  Preferring a replica that passes
        the seed-free digest is a genuine repair, not a cosmetic one, so the CEs
        it stepped over are reported rather than hidden.
        """
        g2 = _g2mod()
        first = None
        rejected = []
        for ce in self.ces:
            if ce in geom.CE_DEAD:
                continue
            try:
                raw = self.dev.read_page(ce, GROUP2_BLOCK, row, slc=True)
            except DeviceError:
                continue
            if g2.looks_dead(raw):
                continue
            if first is None:
                first = (raw, ce)
            chk = g2.check_page(raw)
            if chk["mcrc"][1] and chk["mcrc"][0] == chk["mcrc"][1]:
                return raw, ce, rejected
            rejected.append(ce)
        if first is None:
            return None, None, rejected
        return first[0], first[1], rejected

    def staged_blob(self):
        """Group 2 only: the payload over the ROM's whole load window.

        For FW_ID 1 that is three pages longer than `payload_len` (rows
        152..154), and those rows are real image data stored in the clear past
        the end of the encrypted payload -- byte-identical to
        `dumps/fw.sram.efc.bin` at 0x177000/0x179800/0x17c000.  For FW_ID 2 it
        is 46 pages longer (rows 182..227).
        """
        if self.group != 2:
            raise ValueError("staged_blob is group 2 only")
        blob, _ = self._assemble_g2(staged=True)
        return blob

    def decrypt(self, key=None):
        """Group 2 only: AES-128-CBC decrypt the body, keep the plaintext tail.

        Needs pycryptodome.  The key ladder is
        `PBKDF2-HMAC-SHA256(OTP_AES_ROOT, KDF_SALTS[1], 1000, 16)`.
        """
        if self.group != 2:
            raise ValueError("only group 2 bodies are AES-encrypted")
        g2 = _g2mod()
        staged = self.staged_blob()
        n = self.g2_header["payload_len"]
        return g2.decrypt_payload(staged[:n], self.g2_header["iv"], key) + staged[n:]

    def header_blob(self):
        """The group-2 HEADER rows, decoded -- the bytes `blob()` omits.

        `blob()` returns exactly `payload_len` bytes starting at `payload_off`,
        and that contract is pinned by the selftest, so it is not changed here.
        But the omission is easy to trip over and matters practically:

        * The repr says `rows=1-151` while `blob()` returns 150 rows' worth.
          Both numbers are right about different things -- the image spans 151
          rows, the PAYLOAD is 150 -- but read together they look inconsistent.
        * The IV lives in the header.  A payload-only extract therefore
          cannot be decrypted even by someone holding the key, which makes it
          the wrong thing to archive.

        So: `blob()` is the payload, `header_blob()` is the header, and
        `header_blob() + blob()` is the whole on-media image.  `titania fw
        extract --with-header` writes the concatenation.

        Decoded like any other group-2 row (LDPC, descramble, MCRC), so the
        magic is readable at offset 0 -- which is also the cheapest proof that
        descrambling worked.
        """
        if self.group != 2:
            raise ValueError("only group 2 images carry an on-media fw header; "
                             "group %d images have none (see `header()` for "
                             "what locates them instead)" % self.group)
        h = self.g2_header
        nrows = h["payload_off"] // GROUP2_PAGE_BYTES
        out = bytearray()
        for row in range(h["row"], h["row"] + nrows):
            raw, _src, _rep = self._g2_read(row)
            if raw is None:
                raise LookupError(
                    "group 2 header row %d is not readable on this backing; "
                    "the payload may still be extractable with blob()" % row)
            user, _rep2 = decode_group2_page(raw, row)
            out += user
        return bytes(out)

    def __repr__(self):
        # `rows` is the image extent; `size` is the PAYLOAD (`blob()`).  For
        # group 2 those differ by the header row, so say which is which rather
        # than leaving two numbers that do not multiply out.
        extra = ""
        if self.group == 2 and self.g2_header:
            nh = self.g2_header["payload_off"] // GROUP2_PAGE_BYTES
            if nh:
                extra = " +%dhdr" % nh
        return ("<FirmwareImage fw_id=%#x group=%d bank=%s ce=%s blocks=%s "
                "rows=%s-%s payload=%s%s%s>"
                % (self.fw_id, self.group, self.bank, list(self.ces),
                   list(self.blocks), self.rows[0], self.rows[1], self.size,
                   extra, " ENCRYPTED" if self.encrypted else ""))


# ------------------------------------------------------------------- SLB2 ----
def parse_slb2(blob):
    """The SLB2 container at the front of group 1 fw 0x12.  -> dict or None.

    magic 'SLB2', u32 version, u32 flags, u32 file_count, u32 block_count, then
    file_count x {u32 start_block, u32 size, u32 res[2], char name[32]}, blocks
    of 512 bytes.  `ok` is the self-consistency of the extents against
    block_count -- an independent structural check on the reassembly.
    """
    blob = bytes(blob)
    if len(blob) < 0x20 or blob[:4] != b"SLB2":
        return None
    ver, flags, count, blocks = struct.unpack_from("<IIII", blob, 4)
    if count > 64 or 0x20 + 0x30 * count > len(blob):
        return {"magic": "SLB2", "version": ver, "file_count": count,
                "block_count": blocks, "files": [], "ok": False,
                "note": "file table does not fit"}
    files, ok, end = [], True, 1
    for i in range(count):
        off = 0x20 + 0x30 * i
        blk, size = struct.unpack_from("<II", blob, off)
        name = blob[off + 16:off + 0x30].split(b"\0")[0].decode("latin1")
        nblk = (size + 511) // 512
        files.append({"block": blk, "size": size, "name": name, "blocks": nblk})
        ok &= blk >= end
        end = blk + nblk
    ok &= end == blocks
    return {"magic": "SLB2", "version": ver, "flags": flags,
            "file_count": count, "block_count": blocks,
            "bytes": blocks * 512, "files": files, "ok": bool(ok)}


# --------------------------------------------------------------- discovery ---
def _pick_bank(dev, header, group, bank=None):
    """-> (bank, element, why).  Prefers a bank the Device can serve in full.

    The firmware's own order is bank a first, so that is the tie-break; but a
    bank whose CEs are dead or absent can never yield the image, and choosing it
    silently would turn an availability problem into a mysterious one.
    """
    present = set(dev.present_ces())
    cands = [bank] if bank is not None else [0, 1]
    best = None
    for b in cands:
        e = header.element_for(group, b)
        if e is None:
            continue
        need = set(e.ces)
        gone = sorted((need - present) | (need & set(geom.CE_DEAD)))
        if not gone:
            return b, e, "all %d CEs readable" % len(need)
        if best is None:
            best = (b, e, "CE %s unavailable" % gone)
    if best is None:
        return None, None, "no element for group %d" % group
    return best


def find_images(dev, header=None, bank=None):
    """Every firmware image this Device can locate.  -> [FirmwareImage]

    Groups 0/1 come from the block 0 header; group 2 from scanning block 0 of
    CE 0..5 for `fw_hdr_t` magic.  A group that cannot be located is simply
    absent from the list -- call `locate_report` for the reasons.
    """
    imgs, _ = find_images_report(dev, header, bank)
    return imgs


def find_images_report(dev, header=None, bank=None):
    """`find_images` plus a dict explaining everything it could not do."""
    report = {"header": None, "group2": None, "problems": []}
    imgs = []

    if header is None:
        header, hrep = read_group01_header(dev)
        report["header"] = hrep
    if header is None:
        report["problems"].append("no parseable group 0/1 header on any CE")
    else:
        for fw_id, spec in sorted(FW_IDS.items()):
            if spec.group == 2:
                continue
            b, el, why = _pick_bank(dev, header, spec.group, bank)
            if el is None:
                report["problems"].append("fw %#x: %s" % (fw_id, why))
                continue
            ppb = geom.PAGES_PER_BLOCK_SLC
            lo = el.locate(spec.start_unit, ppb)[2]
            hi = el.locate(spec.start_unit + spec.units - 1, ppb)[2]
            imgs.append(FirmwareImage(
                dev, fw_id, spec.group, ces=el.ces, blocks=el.blocks,
                rows=(min(lo, hi), max(lo, hi)), size=spec.size,
                encrypted=(spec.group == 0), bank=b, element=el, spec=spec))
            if "unavailable" in why:
                report["problems"].append("fw %#x bank %d: %s" % (fw_id, b, why))

    g2imgs, g2rep = find_group2_images(dev)
    report["group2"] = g2rep
    imgs += g2imgs
    return imgs, report


def find_group2_images(dev, ce=None, max_row=GROUP2_SCAN_ROWS):
    """Scan block 0 of CE 0..5 for group-2 firmware headers.

    -> ([FirmwareImage], report).  CE 1..5 are byte-identical replicas so one
    live CE is enough; CE0 must hold the real image but is dead on this board,
    which is why the replicas matter.  Rows past the second image are scanned
    only far enough to find the fill pattern that bounds the last slot.
    """
    g2 = _g2mod()
    c = _codec()
    present = [x for x in (dev.present_ces() if ce is None else [ce])
               if x in GROUP2_CES]
    report = {"ces": present, "live_ce": None, "rows_read": 0,
              "replicas": {}, "problems": []}
    live = None
    for x in present:
        if x in geom.CE_DEAD:
            report["problems"].append("CE%d is dead on this board" % x)
            continue
        try:
            raw = dev.read_page(x, GROUP2_BLOCK, 1, slc=True)
        except DeviceError as e:
            report["problems"].append("CE%d row 1 unreadable: %s" % (x, e))
            continue
        if g2.looks_dead(raw):
            report["problems"].append("CE%d answers with a constant page" % x)
            continue
        if live is None:
            live = x
    report["live_ce"] = live
    if live is None:
        return [], report

    plain = {}
    for row in range(0, max_row):
        try:
            raw = dev.read_page(live, GROUP2_BLOCK, row, slc=True)
        except DeviceError:
            continue
        plain[row] = g2.descramble_page(raw, c.seed_state(row))
    report["rows_read"] = len(plain)

    ces = tuple(x for x in present if x not in geom.CE_DEAD)
    imgs = []
    for h in g2.scan_images(plain):
        win = GROUP2_LOAD.get(h["fw_type"])
        slot = ((win[0], win[0] + win[1] - 1) if win
                else (h["row"], h["slot_last_row"]))
        imgs.append(FirmwareImage(
            dev, h["fw_type"], 2, ces=ces, blocks=(GROUP2_BLOCK,),
            rows=(h["row"], h["last_row"]), size=h["payload_len"],
            encrypted=True, g2_header=h, header_row=h["row"],
            slot_rows=slot, spec=FW_IDS.get(h["fw_type"])))
    return imgs, report


def extract(dev, fw_id, bank=None, header=None):
    """-> the FirmwareImage for `fw_id`, or raise KeyError/LookupError."""
    if fw_id not in FW_IDS:
        raise KeyError("unknown FW_ID %r; known: %s"
                       % (fw_id, sorted(FW_IDS)))
    for img in find_images(dev, header=header, bank=bank):
        if img.fw_id == fw_id:
            return img
    raise LookupError("FW_ID %#x is not locatable on this device" % fw_id)


# ============================================================== selftest =====
# `titania.dumpdev.DumpDevice` is the real backing and is used when it imports.
# This minimal stand-in keeps `py titania/fw.py --selftest` working in a
# checkout where dumpdev.py is absent or broken, so a self-test failure is never
# ambiguous between "fw.py is wrong" and "the device layer is wrong".
class _ScratchDumpDevice:
    """Read-only Device over <dump>/{raw,slc}.  Self-test scaffolding only."""

    writable = False

    def __init__(self, root):
        self.root = Path(root)
        self.writes_enabled = False
        self._idx = {}
        raw = self.root / "raw"
        if raw.is_dir():
            for d in sorted(raw.glob("ce[0-9][0-9]")):
                ce = int(d.name[2:])
                for f in d.glob("b[0-9][0-9][0-9]_p[0-9][0-9][0-9].bin"):
                    b = int(f.name[1:4])
                    p = int(f.name[6:9])
                    self._idx.setdefault((ce, b, p), f)
        slc = self.root / "slc"
        if slc.is_dir():
            for f in slc.glob("deep_ce*_b*_pg*.bin"):
                s = f.stem.split("_")
                ce = int(s[1][2:])
                b = int(s[2][1:])
                p = int(s[3][2:])
                self._idx.setdefault((ce, b, p), f)
        self._ces = sorted({k[0] for k in self._idx})

    def present_ces(self):
        return list(self._ces)

    def read_page(self, ce, block, page, lun=0, slc=False):
        f = self._idx.get((ce, block, page))
        if f is None:
            raise DeviceError("no page ce%d b%d p%d in %s" % (ce, block, page,
                                                              self.root))
        d = f.read_bytes()
        if len(d) != 18432:
            raise DeviceError("%s is %d bytes" % (f, len(d)))
        return d

    def __repr__(self):
        return "<_ScratchDumpDevice %s ces=%s pages=%d>" % (
            self.root, self._ces, len(self._idx))


def _selftest_device(root=None):
    root = Path(root or config.dump_path(required=True))
    if not root.is_dir():
        return None, "no dump directory at %s" % root
    why = "titania.dumpdev not importable"
    dd = _sibling("dumpdev")
    if dd is not None and hasattr(dd, "DumpDevice"):
        try:
            return dd.DumpDevice(str(root)), "titania.dumpdev.DumpDevice"
        except Exception as e:
            why = "titania.dumpdev.DumpDevice raised %r" % (e,)
    dev = _ScratchDumpDevice(root)
    if not dev.present_ces():
        return None, "no pages under %s" % root
    return dev, "_ScratchDumpDevice fall-back (%s)" % why


#: The two `*_thisboard.bin` blobs in `the dump` were written by this extractor,
#: from this dump, on an earlier run (`tfw_full.py` / the fold recorded in
#: scratchpad_kpsearch.md).  Comparing the extractor against them therefore
#: cannot confirm that the extractor is right -- only that it still produces
#: what it produced before.  That is a regression check, and this selftest says
#: so out loud rather than letting a row of PASS read as verification.
#:
#: What is independent, and is checked separately above: the AU MCRC (computed
#: over the complemented stored bytes, needs no seed, no key and no reference),
#: the SLB2 container's own self-consistency, and the group-2 header magic.
#: Those are properties of the media, not of a previous run of this code.
_SELF_DERIVED = ("<dump>/fw_group_0_thisboard.bin",
                 "<dump>/fw_group_1_thisboard.bin")


def _selftest(root=None):
    ok = True
    n_pass = n_skip = n_regr = 0

    def check(cond, label, extra="", regression=False):
        """One selftest row.

        `regression=True` marks a check whose reference is self-derived.  It is
        counted and printed as such, because a check that cannot fail for an
        independent reason must not be presented as though it could.
        """
        nonlocal ok, n_pass, n_skip, n_regr
        if regression:
            label = "REGRESSION " + label
        if cond is None:
            print("[SKIP] %-56s %s" % (label, extra))
            n_skip += 1
            return
        print("[%s] %-56s %s" % ("PASS" if cond else "FAIL", label, extra))
        n_pass += bool(cond)
        n_regr += bool(cond) and regression
        ok &= bool(cond)

    print("titania.fw -- selftest\n")
    dev, how = _selftest_device(root)
    print("  device: %s" % how)
    if dev is None:
        print("  nothing to test against")
        return 1
    print("  %r\n" % dev)
    print("  codec : %s" % _codec().__name__)
    print("  group2: %s\n" % _g2mod().__name__)

    # 1 -- the header, checked by the firmware's own checksum
    header, hrep = read_group01_header(dev)
    for ce, why in hrep["tried"]:
        print("      ce%-2d %s" % (ce, why))
    check(header is not None and header.checksum_ok,
          "group 0/1 header parses + checksum",
          "ce%s  %#06x" % (hrep["header_ce"],
                           header.checksum_stored) if header else "not found")
    if header is None:
        return 1
    # a verdict from one sample is vacuous; say SKIPPED rather than PASS
    check(hrep["agree"] if len(hrep["digests"]) >= 2 else None,
          "same header on every readable CE of bank B",
          "%d CE(s): %s%s" % (len(hrep["digests"]), sorted(hrep["digests"]),
                              "" if len(hrep["digests"]) >= 2
                              else " -- one sample proves nothing"))
    # the zero-page trap, asserted rather than assumed: block 0 of CE 0..5 is
    # group 2's block and must not be accepted as a group 0/1 header
    _, arep = read_group01_header(dev, ces=list(BANK_CES[0]))
    check(all("empty" in why or "not served" in why or "dead" in why
              or "does not parse" in why or "read failed" in why
              for _ce, why in arep["tried"]) and arep["header_ce"] is None,
          "block 0 of CE 0-5 rejected as a group 0/1 header",
          "; ".join("ce%d %s" % (c, w.split(" -- ")[0])
                    for c, w in arep["tried"]) or "no CE served")

    # 2 -- element shape
    shape_ok = True
    for e in header.elements:
        want_blocks = GROUP_BLOCKS[e.group]
        want_ces = BANK_CES[e.bank]
        good = (len(e.entries) == ENTRIES_PER_ELEMENT
                and e.blocks == want_blocks and e.ces == want_ces)
        shape_ok &= good
        print("      element %d  group %d bank %d  ce %s blocks %s  %s"
              % (e.index, e.group, e.bank, list(e.ces), list(e.blocks),
                 "ok" if good else "UNEXPECTED"))
    check(shape_ok, "4 elements x 12 entries, expected ce/blocks")
    check(all(header.covers(g) for g in (0, 1)),
          "elements cover every unit of both groups",
          "%d and %d units" % (header.element_for(0, 1).units(),
                               header.element_for(1, 1).units()))

    # 3 -- the FW_ID table
    tab = fw_id_table(header)
    tot0 = sum(t["units"] for t in tab.values() if t["group"] == 0)
    tot1 = sum(t["units"] for t in tab.values() if t["group"] == 1)
    check(tot0 * PAGE_USER_BYTES == 67108864 and tot1 * PAGE_USER_BYTES == 65011712,
          "FW_ID table totals match the containers",
          "group0 %d B, group1 %d B" % (tot0 * PAGE_USER_BYTES,
                                        tot1 * PAGE_USER_BYTES))
    ids = sorted(k for k, v in tab.items() if v["group"] != 2)
    check(ids == [0x10, 0x12, 0x14, 0x15], "group 0/1 FW_IDs",
          " ".join("%#x" % i for i in ids))
    g1 = [tab[i] for i in (0x12, 0x14, 0x15)]
    check(all(a["start_unit"] + a["units"] == b["start_unit"]
              for a, b in zip(g1, g1[1:])),
          "group 1 ids are contiguous", "0 + 128 + 3712 + 128 = 3968")

    # 4 -- discovery
    imgs, drep = find_images_report(dev, header=header)
    for i in imgs:
        print("      %r" % i)
    for p in drep["problems"]:
        print("      note: %s" % p)
    check(len(imgs) >= 1, "images located", "%d" % len(imgs))

    # 5 -- interleave: the two banks are mirrors, so a unit decoded from bank a
    #      and the same unit decoded from bank B must agree.  This is the real
    #      test of the reassembly rule: get the entry order wrong and it fails.
    ppb = geom.PAGES_PER_BLOCK_SLC
    same = diff = skipped = 0
    for group in (0, 1):
        ea, eb = header.element_for(group, 0), header.element_for(group, 1)
        for unit in (2, 3, 11, 12, 13, 60, 1201, 4415):
            if unit >= ea.units(ppb):
                continue
            pages = []
            for el in (ea, eb):
                ce, blk, pg = el.locate(unit, ppb)
                if ce in geom.CE_DEAD:
                    break
                try:
                    raw = dev.read_page(ce, blk, pg, slc=True)
                except DeviceError:
                    break
                pages.append(decode_group01_page(raw,
                                                 geom.compose_row(blk, pg))[0])
            if len(pages) != 2:
                skipped += 1
                continue
            same += pages[0] == pages[1]
            diff += pages[0] != pages[1]
    check((same > 0) or None, "bank A / bank B mirror at the same unit",
          "%d identical, %d differ, %d not available" % (same, diff, skipped))
    if same:
        check(diff == 0, "  ... with no disagreement")

    # 5b -- forcing the bank with the dead CE must degrade cleanly: every unit
    #       it can serve is byte-identical to the other bank, and the ones it
    #       cannot are reported missing rather than filled with a dead die's
    #       constant page (which reads without error and is not detectably
    #       absent -- this is the trap the skip_dead_ces flag exists for).
    try:
        a = extract(dev, 0x12, bank=0, header=header)
        b = extract(dev, 0x12, bank=1, header=header)
        ra, rb = a.verify(), b.verify()
        gone = {m[0] for m in ra["missing"]}
        served = [i for i in range(a.spec.units) if i not in gone]
        bad = sum(1 for i in served
                  if a.blob()[i * PAGE_USER_BYTES:(i + 1) * PAGE_USER_BYTES]
                  != b.blob()[i * PAGE_USER_BYTES:(i + 1) * PAGE_USER_BYTES])
        check(bad == 0 and bool(served) and ra["mcrc"][0] == ra["mcrc"][1],
              "forced bank A: served units match bank B",
              "%d/%d units served, %d differ, %d missing on CE %s"
              % (len(served), a.spec.units, bad, len(gone), ra["dead_ce"]))
        check(not ra["complete"] and not ra["ok"] and rb["ok"],
              "  ... and is reported incomplete, not merely 'ok'",
              "bank A complete=%s ok=%s / bank B ok=%s"
              % (ra["complete"], ra["ok"], rb["ok"]))
    except LookupError as e:
        check(None, "forced bank A degrades cleanly", str(e))

    # 6 -- extract fw 0x12 end to end and check its MCRCs and container
    try:
        img = extract(dev, 0x12, header=header)
    except LookupError as e:
        img = None
        print("      %s" % e)
    if img is None:
        check(None, "fw 0x12 extract", "not locatable")
    else:
        rep = img.verify()
        check(not rep["missing"], "fw 0x12: every unit readable",
              "%d units from bank %s ce %s" % (rep["units"], img.bank,
                                               list(img.ces)))
        check(rep["mcrc"][1] and rep["mcrc"][0] == rep["mcrc"][1],
              "fw 0x12: AU MCRC (seed-free, end to end)",
              "%d/%d AUs" % rep["mcrc"])
        c = rep.get("container")
        check(bool(c and c["ok"]), "fw 0x12: SLB2 container self-consistent",
              "%d files, %d blocks = %d B" % (c["file_count"], c["block_count"],
                                              c["bytes"]) if c else "no SLB2")
        check(len(img.blob()) == img.size, "fw 0x12: size",
              "%d bytes" % len(img.blob()))
        ref = config.dump_path("fw_group_1_thisboard.bin")
        if ref.exists():
            want = ref.read_bytes()[:img.size]
            check(img.blob() == want,
                  "fw 0x12 vs fw_group_1_thisboard.bin",
                  "%d/%d bytes -- self-derived reference: this detects DRIFT, "
                  "not correctness"
                  % (sum(1 for a, b in zip(img.blob(), want)
                         if a == b), img.size),
                  regression=True)
        else:
            check(None, "fw 0x12 vs a reference image", "no reference present",
                  regression=True)

    # 6b -- the interleave across the FULL span of the two big images, sampled.
    #       fw 0x12 only exercises units 0..127, i.e. pages 0..10 of the block;
    #       these samples reach unit 4095 / page 341, where a wrong entry order
    #       or a missing `% 368` would show up and nothing above would catch it.
    for fw_id, ref_name in ((0x10, "fw_group_0_thisboard.bin"),
                            (0x14, "fw_group_1_thisboard.bin")):
        ref = config.dump_path(ref_name)
        try:
            img = extract(dev, fw_id, header=header)
        except LookupError:
            img = None
        if img is None or not ref.exists():
            check(None, "fw %#x: sampled units vs %s" % (fw_id, ref_name),
                  "image not locatable" if img is None else "no reference",
                  regression=True)
            continue
        blob = ref.read_bytes()
        n = img.spec.units
        idx = sorted({0, 1, 11, 12, 13, n // 3, n // 2, n - 13, n - 12,
                      n - 2, n - 1})
        good = bad = err = 0
        for i in idx:
            try:
                user, _rep = img.unit(i)
            except DeviceError:
                err += 1
                continue
            off = (img.spec.start_unit + i) * PAGE_USER_BYTES
            good += user == blob[off:off + PAGE_USER_BYTES]
            bad += user != blob[off:off + PAGE_USER_BYTES]
        check((bad == 0 and good) if good else None,
              "fw %#x: sampled units vs %s" % (fw_id, ref_name),
              "%d/%d units exact (%d unreadable), units %s"
              % (good, good + bad, err, idx[:4] + ["..."] + idx[-2:]),
              regression=True)

    # 7 -- group 2
    g2imgs = [i for i in imgs if i.group == 2]
    check((len(g2imgs) == 2) if g2imgs else None,
          "group 2: two images (FW_ID 1 and 2)",
          " ".join("fw%d rows %d-%d" % (i.fw_id, *i.rows) for i in g2imgs)
          or "none found: %s" % drep["group2"]["problems"])
    if g2imgs:
        i1 = g2imgs[0]
        h = i1.header()
        rep = i1.verify()
        check(h["magic"] == b"\x1d\x33\x47\x77" and h["zero_span_ok"],
              "group 2 fw 1: header magic + zero span",
              "payload_len %#x (%d pages), IV %s"
              % (h["payload_len"], h["payload_pages"], h["iv"].hex()))
        check(rep["mcrc"][1] and rep["mcrc"][0] == rep["mcrc"][1],
              "group 2 fw 1: AU MCRC (CRC-32Q, seed-free)",
              "%d/%d chunks" % rep["mcrc"])
        check(i1.encrypted and len(i1.blob()) == h["payload_len"],
              "group 2 fw 1: body extracted as ciphertext",
              "%d bytes AES-128-CBC -- a complete result, not a failure"
              % len(i1.blob()))
        # the strongest cross-check available: the ROM's own staged copy
        sram = _ROOT / "dumps" / "fw.sram.efc.bin"
        staged = i1.staged_blob()
        tail = staged[h["payload_len"]:]
        if sram.exists():
            ref = sram.read_bytes()
            check(tail == ref[h["payload_len"]:h["payload_len"] + len(tail)],
                  "group 2 fw 1: plaintext tail == staged SRAM copy",
                  "%d bytes at %#x" % (len(tail), h["payload_len"]))
            try:
                dec = i1.decrypt()
                eq = sum(1 for a, b in zip(dec, ref) if a == b)
                check(eq / len(dec) > 0.99,
                      "group 2 fw 1: decrypts to the staged image",
                      "%d/%d = %.2f%% (residual = runtime-modified SRAM)"
                      % (eq, len(dec), 100.0 * eq / len(dec)))
            except ImportError:
                check(None, "group 2 fw 1: AES decrypt", "pycryptodome absent")
        else:
            check(None, "group 2 fw 1 vs the staged SRAM copy",
                  "dumps/fw.sram.efc.bin absent")

    # 8 -- dead CEs: an availability limit, kept distinct from format failure
    served = set(dev.present_ces())
    unavailable = sorted(set(geom.CE_DEAD) | (set(range(12)) - served))
    g01_banks = {i.fw_id: i.bank for i in imgs if i.group != 2}
    check(all(not (set(BANK_CES[b]) & set(unavailable))
              for b in g01_banks.values()),
          "group 0/1 extracted from a bank with no dead CE",
          "CE %s unavailable; chose bank(s) %s"
          % (unavailable, sorted(set(g01_banks.values()))))
    check(bool(g2imgs) or None,
          "group 2 read from the CE 1-5 replicas, not CE0",
          "CE0 must hold the real group 2 image and is dead on this board, so "
          "this is availability, not format")

    print("\n%s   (%d passed, %d skipped; %d of the passes are REGRESSION)"
          % ("ALL CHECKS PASS" if ok else "FAILURES ABOVE", n_pass, n_skip,
             n_regr))
    if n_regr:
        print("  The %d row(s) marked REGRESSION compare this extractor with "
              "%s," % (n_regr, " and ".join(_SELF_DERIVED)))
        print("  which THIS EXTRACTOR wrote from THIS dump on an earlier run.  "
              "They prove the")
        print("  output has not drifted; they are NOT independent confirmation "
              "that it is right.")
        print("  The independent evidence is elsewhere in this list: the AU "
              "MCRC (seed-free,")
        print("  key-free, computed off the stored bytes), the SLB2 "
              "container's own")
        print("  self-consistency, and the group-2 header magic -- those are "
              "properties of the")
        print("  media rather than of a previous run of this code.")
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        argv.remove("--selftest")
        return _selftest(argv[0] if argv else None)
    if "--table" in argv:
        for fw_id, row in sorted(fw_id_table().items()):
            print("  fw %#04x  group %d  start %-5s units %-5s size %-10s %s"
                  % (fw_id, row["group"], row["start_unit"], row["units"],
                     row["size"], row["note"]))
        return 0
    print(__doc__.strip().split("\n\n")[0])
    print("\n  py titania/fw.py --selftest [dump_dir]")
    print("  py titania/fw.py --table")
    return 0


if __name__ == "__main__":
    sys.exit(main())
