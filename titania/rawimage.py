"""RawImageDevice -- a `Device` over a flat image from an external NAND reader.

    dev = RawImageDevice("mycapture")            # dir holding rawimage.json
    page = dev.read_page(ce=1, block=26, page=0, slc=True)
    res  = codec.decode_page(page, row=geom.compose_row(26, 0))

    py -m titania.rawimage --selftest
    py -m titania.rawimage <dir>                 # describe a capture
    py -m titania.rawimage <dir> --audit         # did the capture work?

Why this is a separate backing from `DumpDevice`
------------------------------------------------
`DumpDevice` serves pages that came off this drive through the EFC, one file per
page, named by (ce, block, page, cell mode) because that is what the EFC was
asked for.  An external reader -- a chip-off rig, a socket programmer, an fpga
front end -- produces something else entirely:

  * one big linear file per die, addressed by (die, block, page), because that
    is the order the NAND itself hands pages over;
  * bytes including the spare area, because it has no idea what a spare area is
    for on this part (and here it carries a third of a codeword -- see below);
  * die numbers of its own invention, with no notion of the EFC's chip-enable
    index, which is the only name the FTL knows;
  * no notion of pseudo-SLC, of WERUs, of LMAs, or of read-retry.

This module is the adapter.  Above it nothing changes: `codec`, `fw`, `aux`,
`verdict`, `ftl` and `raid` are written against `Device` and work unaltered on a
capture that never touched a PlayStation.

THE FIVE THINGS A RAW CAPTURE MUST GET RIGHT
--------------------------------------------
Written out in full, with the evidence, in `NAND_RAW_IMAGE.md`.  In brief, and
in descending order of how badly it goes wrong when it is wrong:

1. CELL mode.  The firmware and metadata region is written in pseudo-slc and
   can only be read back by first sending the vendor's pseudo-SLC prefix command
   (`0xDA` Samsung/Micron, `0xA2` Toshiba/Hynix/SanDisk) to that die.  Without
   it the die routes the row address through its native TLC page map and returns
   a different physical PAGE -- plausible-looking bytes, not an error.  Measured
   here on 28 A/B pairs where one session read the same address both ways
   (WERU 84, blocks 172..175, 7 CEs): pseudo-SLC scores MCRC 4/4 and native
   TLC 0/4, 28 times out of 28.  A reader that cannot emit a prefix command cannot capture this drive.
   `mode_check()` is the oracle; `audit()` finds it at scale.

2. The FULL PAGE, spare included, in column order.  A page is 16384 data +
   2048 spare = 18432 bytes and the format spans all of it: four 4608-byte AUs,
   so the 16384/18432 boundary falls inside AU 3's odd codeword.  A reader that
   separates data from spare, drops the spare, or reorders them destroys AU 3.

3. Read reference voltages.  On native TLC, upper word lines do not decode
   at the die's default references at all -- 0 of 716 in the broad survey, 0 of
   1010 counting the first block.  A single-pass capture at default references
   silently loses about a fifth of a TLC block even where it does not lose the
   upper pages outright.  The reader must walk its own retry ladder and hand us
   every attempt; `retry_candidates()` and `read_best()` are the offline
   substitute for the EFC's ladder.

4. Die -> CE.  Decoding needs no CE at all (the scrambler seed is a function
   of the row and nothing else), but the FTL is addressed by
   `physicalBlockId = 12*block + ce`, so a die label is required before an LMA
   means anything.  `identify_dies()` narrows it from the media itself.

5. Nothing else.  No ECC correction, no de-scrambling, no "bad block
   skipping", no page reordering by the reader.  Raw bytes, in address order.

WHAT THIS MODULE WILL NOT DO
----------------------------
The same four rules `DumpDevice` will not bend, for the same reasons:

  * a page the image does not contain raises `DeviceError`, and never reads back
    as 0xFF -- "absent from the capture" and "erased on the media" are different
    claims and conflating them has produced wrong conclusions here before;
  * cell mode is part of the address, not a hint: asking for a mode the capture
    does not hold raises, and says which mode it does hold;
  * a hole (a page the synthesiser or the reader could not fill) is recorded in
    the manifest and raises on read, rather than being served as data;
  * writes go to an overlay.  `RawImageDevice` never rewrites the capture files.

THE MANIFEST
------------
A capture is a directory holding `rawimage.json` plus one or more binary
streams.  A stream is one capture PASS: one die, one LUN, one cell mode, one
retry level, over a rectangle of (block, page).  Pages are stored back to back
in page-major order within block-major order, exactly as a reader walks them:

    offset = ((block - first_block) * pages + (page - first_page)) * page_bytes

Nothing in the manifest is inferred from the bytes.  Every field is something
the operator of the reader knew and wrote down -- which is the point, because
the fields that matter (cell mode, retry level, die identity) are exactly the
ones that cannot be recovered from the bytes after the fact.
"""
import json
import sys
from pathlib import Path

if __package__ in (None, ""):                       # allow direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import Device, DeviceError, CONFIRM, PageRead, FULL
else:
    from . import geometry as geom
    from .device import Device, DeviceError, CONFIRM, PageRead, FULL

#: The AU-bearing region of a page: 16384 data + 2048 spare, contiguous.
#: This is the whole page.  There is no part of it the format does not use.
REGION_BYTES = 18432

#: manifest file name and format tag
MANIFEST_NAME = "rawimage.json"
FORMAT_TAG = "titania-rawimage/1"

MODE_SLC = "slc"                    # pseudo-SLC: prefix command sent, 368 pg/blk
MODE_TLC = "tlc"                    # native: no prefix, 1104 pg/blk
MODES = (MODE_SLC, MODE_TLC)

#: Vendor pseudo-SLC prefix, by NAND maker ID byte.  The one command-level
#: difference between the five makers the boot ROM supports -- everything else
#: that varies (interface type, address cycles, timing, vref) is link layer and
#: cannot change a stored byte.  `efc_cpu0_rom.bin.c:4204-4275`, and
#: NAND_TP_VENDOR_VARIANTS.md ss2 for the full dispatch.
SLC_PREFIX_BY_MAKER = {
    0x98: 0xA2,     # Toshiba
    0x2C: 0xDA,     # Micron
    0xEC: 0xDA,     # Samsung   <- this drive, K9BKGD8J1A, ID ec 5E 98 bf 84 cc
    0xAD: 0xA2,     # Hynix
    0x45: 0xA2,     # SanDisk
}

#: Address cycles the ROM clocks out, by maker.  A reader's business, not ours;
#: recorded here so a capture rig has one place to look.
ADDR_CYCLES_BY_MAKER = {0x98: 5, 0x2C: 4, 0xEC: 5, 0xAD: 5, 0x45: 5}


class RawImageError(DeviceError):
    """The capture is not self-consistent, or does not hold what was asked."""


def _mode_name(slc):
    return MODE_SLC if slc else MODE_TLC


def _mode_slc(mode):
    if mode not in MODES:
        raise RawImageError("mode must be one of %s, not %r" % (MODES, mode))
    return mode == MODE_SLC


# --------------------------------------------------------------------------
# One capture pass
# --------------------------------------------------------------------------
class Stream(object):
    """One pass of the reader: one die, one LUN, one cell mode, one level.

    A stream is not allowed to mix cell modes or retry levels.
    Both are die state set by a command that persists until it is changed, so a
    file that mixes them is a file whose pages cannot be interpreted -- and the
    mixing is invisible in the bytes.  Splitting them into separate streams
    makes the operator write down what they did.
    """

    __slots__ = ("file", "die", "ce", "lun", "mode", "first_block", "blocks",
                 "first_page", "pages", "retry_level", "holes", "note",
                 "_path", "_size")

    def __init__(self, file, die, mode, first_block=0, blocks=1, first_page=0,
                 pages=None, ce=None, lun=0, retry_level=0, holes=(), note=""):
        self.file = str(file)
        self.die = int(die)
        self.ce = None if ce is None else int(ce)
        self.lun = int(lun)
        self.mode = mode
        if mode not in MODES:
            raise RawImageError("stream %s: mode %r is not one of %s"
                                % (self.file, mode, MODES))
        self.first_block = int(first_block)
        self.blocks = int(blocks)
        self.first_page = int(first_page)
        self.pages = int(pages if pages is not None
                         else geom.pages_per_block(self.slc))
        self.retry_level = int(retry_level)
        self.holes = set(tuple(h) for h in holes)
        self.note = note
        self._path = None
        self._size = None

    # -- geometry ---------------------------------------------------------
    @property
    def slc(self):
        return self.mode == MODE_SLC

    def npages(self):
        return self.blocks * self.pages

    def covers(self, block, page):
        return (self.first_block <= block < self.first_block + self.blocks
                and self.first_page <= page < self.first_page + self.pages)

    def index(self, block, page):
        return ((block - self.first_block) * self.pages
                + (page - self.first_page))

    def is_hole(self, block, page):
        return (block, page) in self.holes

    def key(self):
        return (self.ce, self.lun, self.mode, self.retry_level)

    # -- serialisation ----------------------------------------------------
    def to_json(self):
        d = {"file": self.file, "die": self.die, "ce": self.ce,
             "lun": self.lun, "mode": self.mode,
             "first_block": self.first_block, "blocks": self.blocks,
             "first_page": self.first_page, "pages": self.pages,
             "retry_level": self.retry_level,
             "holes": sorted(list(h) for h in self.holes)}
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_json(cls, d):
        return cls(file=d["file"], die=d["die"], mode=d["mode"],
                   first_block=d.get("first_block", 0),
                   blocks=d.get("blocks", 1),
                   first_page=d.get("first_page", 0),
                   pages=d.get("pages"), ce=d.get("ce"), lun=d.get("lun", 0),
                   retry_level=d.get("retry_level", 0),
                   holes=d.get("holes", ()), note=d.get("note", ""))

    def __repr__(self):
        return ("<Stream %s die%d ce=%s %s L%d b%d..%d p%d..%d %d holes>"
                % (self.file, self.die, self.ce, self.mode, self.retry_level,
                   self.first_block, self.first_block + self.blocks - 1,
                   self.first_page, self.first_page + self.pages - 1,
                   len(self.holes)))


# --------------------------------------------------------------------------
# The capture as a whole
# --------------------------------------------------------------------------
class RawImage(object):
    """A manifest plus its streams.  No bytes are read here."""

    def __init__(self, root, page_bytes=REGION_BYTES, data_offset=0,
                 streams=(), capture=None, maker=None):
        self.root = Path(root)
        self.page_bytes = int(page_bytes)
        self.data_offset = int(data_offset)
        self.streams = list(streams)
        self.capture = dict(capture or {})
        self.maker = maker
        self.validate()

    # -- validation -------------------------------------------------------
    def validate(self):
        """Everything that can be checked without opening a blob.

        Strict.  A capture is hours of somebody's time and the
        errors that matter here (a page size that silently drops the spare, a
        stream whose file is shorter than its declared rectangle) are invisible
        until an analysis built on them has already been believed.
        """
        if self.data_offset < 0:
            raise RawImageError("data_offset %d is negative" % self.data_offset)
        if self.page_bytes < self.data_offset + REGION_BYTES:
            raise RawImageError(
                "page_bytes %d with data_offset %d leaves %d bytes for the AU "
                "region, which needs %d (16384 data + 2048 spare).  A capture "
                "that dropped the spare area cannot be decoded: the format "
                "spans the whole page and AU 3's odd codeword straddles the "
                "16384 boundary."
                % (self.page_bytes, self.data_offset,
                   self.page_bytes - self.data_offset, REGION_BYTES))
        seen = {}
        for s in self.streams:
            p = self.root / s.file
            s._path = p
            if s.blocks < 1 or s.pages < 1:
                raise RawImageError("stream %s covers no pages" % s.file)
            want = s.npages() * self.page_bytes
            if p.exists():
                s._size = p.stat().st_size
                if s._size < want:
                    raise RawImageError(
                        "stream %s is %d bytes but its declared rectangle "
                        "(%d blocks x %d pages x %d B) needs %d.  Nothing is "
                        "padded to make them agree -- shorten the rectangle or "
                        "declare the missing pages as holes."
                        % (s.file, s._size, s.blocks, s.pages, self.page_bytes,
                           want))
            k = (s.die, s.lun, s.mode, s.retry_level)
            for b in range(s.first_block, s.first_block + s.blocks):
                for other in seen.get(k, ()):
                    if other.first_block <= b < other.first_block + other.blocks:
                        lo = max(s.first_page, other.first_page)
                        hi = min(s.first_page + s.pages,
                                 other.first_page + other.pages)
                        if lo < hi:
                            raise RawImageError(
                                "streams %s and %s both claim die %d lun %d "
                                "%s level %d block %d pages %d..%d.  Two "
                                "readings of one address are two STREAMS at "
                                "different retry levels or they are a mistake; "
                                "they are not one stream."
                                % (s.file, other.file, s.die, s.lun, s.mode,
                                   s.retry_level, b, lo, hi - 1))
            seen.setdefault(k, []).append(s)
        # a die must not be two CEs, and a CE must not be two dies
        d2c, c2d = {}, {}
        for s in self.streams:
            if s.ce is None:
                continue
            if d2c.setdefault(s.die, s.ce) != s.ce:
                raise RawImageError("die %d is mapped to both ce %d and ce %d"
                                    % (s.die, d2c[s.die], s.ce))
            if c2d.setdefault(s.ce, s.die) != s.die:
                raise RawImageError("ce %d is claimed by dies %d and %d"
                                    % (s.ce, c2d[s.ce], s.die))
        return True

    # -- load / save ------------------------------------------------------
    @classmethod
    def load(cls, path):
        path = Path(path)
        mpath = path / MANIFEST_NAME if path.is_dir() else path
        if not mpath.exists():
            raise RawImageError(
                "no %s at %s.  A raw capture needs a manifest: the cell mode, "
                "the retry level and the die identity are not recoverable from "
                "the bytes, so they have to be written down."
                % (MANIFEST_NAME, path))
        with open(mpath, "r") as f:
            d = json.load(f)
        fmt = d.get("format")
        if fmt != FORMAT_TAG:
            raise RawImageError("%s says format %r; this reader knows %r"
                                % (mpath, fmt, FORMAT_TAG))
        return cls(root=mpath.parent,
                   page_bytes=d.get("page_bytes", REGION_BYTES),
                   data_offset=d.get("data_offset", 0),
                   streams=[Stream.from_json(s) for s in d.get("streams", ())],
                   capture=d.get("capture"), maker=d.get("maker"))

    def to_json(self):
        return {"format": FORMAT_TAG,
                "page_bytes": self.page_bytes,
                "data_offset": self.data_offset,
                "maker": self.maker,
                "capture": self.capture,
                "streams": [s.to_json() for s in self.streams]}

    def write(self, path=None):
        path = Path(path) if path is not None else self.root
        path.mkdir(parents=True, exist_ok=True)
        with open(path / MANIFEST_NAME, "w") as f:
            json.dump(self.to_json(), f, indent=2, sort_keys=False)
            f.write("\n")
        return path / MANIFEST_NAME

    # -- lookup -----------------------------------------------------------
    def dies(self):
        return sorted({s.die for s in self.streams})

    def ces(self):
        return sorted({s.ce for s in self.streams if s.ce is not None})

    def unmapped_dies(self):
        return sorted({s.die for s in self.streams if s.ce is None})

    def retry_levels(self):
        return sorted({s.retry_level for s in self.streams})

    def find(self, ce=None, die=None, lun=0, mode=None, block=None, page=None,
             level=None):
        """Streams matching every constraint given, in manifest order."""
        out = []
        for s in self.streams:
            if ce is not None and s.ce != ce:
                continue
            if die is not None and s.die != die:
                continue
            if lun is not None and s.lun != lun:
                continue
            if mode is not None and s.mode != mode:
                continue
            if level is not None and s.retry_level != level:
                continue
            if block is not None and page is not None:
                if not s.covers(block, page):
                    continue
            elif block is not None:
                if not (s.first_block <= block < s.first_block + s.blocks):
                    continue
            out.append(s)
        return out

    def read_raw(self, stream, block, page):
        """The stream's bytes for one page -> `page_bytes`, unsliced."""
        if not stream.covers(block, page):
            raise RawImageError("%s does not cover block %d page %d"
                                % (stream.file, block, page))
        if stream.is_hole(block, page):
            raise RawImageError(
                "block %d page %d is a declared HOLE in %s -- the capture has "
                "no bytes for it.  It is not erased media."
                % (block, page, stream.file))
        off = stream.index(block, page) * self.page_bytes
        p = stream._path if stream._path is not None else self.root / stream.file
        try:
            with open(p, "rb") as f:
                f.seek(off)
                data = f.read(self.page_bytes)
        except OSError as exc:
            raise RawImageError("%s: %s" % (p, exc))
        if len(data) != self.page_bytes:
            raise RawImageError(
                "%s: short read at offset %d -- got %d of %d bytes"
                % (p, off, len(data), self.page_bytes))
        return data

    def read_region(self, stream, block, page):
        """The 18432 AU-bearing bytes for one page."""
        data = self.read_raw(stream, block, page)
        return data[self.data_offset:self.data_offset + REGION_BYTES]

    def __repr__(self):
        return ("<RawImage %s dies=%s ces=%s streams=%d levels=%s>"
                % (self.root, self.dies(), self.ces(), len(self.streams),
                   self.retry_levels()))


# --------------------------------------------------------------------------
# The Device
# --------------------------------------------------------------------------
class RawImageDevice(Device):
    """A `Device` over a raw external capture.  See the module docstring.

    Parameters
    ----------
    root : path
        directory holding `rawimage.json`, or the manifest file itself.
    image : RawImage
        an already-built image, instead of `root`.

    Which reading `read_page` serves
    --------------------------------
    The lowest captured retry level, unless `set_retry_level` says otherwise.
    That is the deliberate choice: level 0 is the die's own calibration and is
    the most faithful reading of what is on the medium, so it is what a caller
    asking for "the page" should get.  A caller that wants "whichever reading
    decodes" is asking a different question and should call `read_best`, which
    says which level it used.
    """

    writable = True

    def __init__(self, root=None, image=None):
        Device.__init__(self)
        if image is None:
            if root is None:
                raise RawImageError("RawImageDevice needs a root or an image")
            image = RawImage.load(root)
        self.image = image
        self.root = image.root
        self._overlay = {}
        self._level_pref = {}       # (ce, mode) -> preferred retry level
        self._levels = image.retry_levels()

    # -- Device interface -------------------------------------------------
    def present_ces(self):
        return sorted(set(self.image.ces()) | {k[0] for k in self._overlay})

    def _streams_for(self, ce, block, page, lun, slc):
        mode = _mode_name(slc)
        pref = self._level_pref.get((ce, mode))
        cands = [s for s in self.image.streams
                 if s.ce == ce and s.lun == lun and s.mode == mode
                 and s.covers(block, page) and not s.is_hole(block, page)]
        cands.sort(key=lambda s: (pref is not None and s.retry_level != pref,
                                  s.retry_level))
        return cands

    def read_page(self, ce, block, page, lun=0, slc=False):
        key = (ce, block, page, bool(slc))
        if key in self._overlay:
            return self._overlay[key]
        cands = self._streams_for(ce, block, page, lun, slc)
        if not cands:
            raise RawImageError(self._absent_message(ce, block, page, lun, slc))
        return self.image.read_region(cands[0], block, page)

    def read_page_ex(self, ce, block, page, lun=0, slc=False):
        key = (ce, block, page, bool(slc))
        if key in self._overlay:
            return PageRead(self._overlay[key], FULL, 0)
        cands = self._streams_for(ce, block, page, lun, slc)
        if not cands:
            raise RawImageError(self._absent_message(ce, block, page, lun, slc))
        s = cands[0]
        return PageRead(self.image.read_region(s, block, page), FULL,
                        s.retry_level)

    def scan_page(self, ce, block, page, lun=0, slc=False):
        """Exact, over every byte -- they are all in the file."""
        return _sib("dumpdev").scan_bytes(
            self.read_page(ce, block, page, lun=lun, slc=slc))

    def _absent_message(self, ce, block, page, lun, slc):
        mode = _mode_name(slc)
        other = self._streams_for(ce, block, page, lun, not slc)
        holes = [s for s in self.image.streams
                 if s.ce == ce and s.lun == lun and s.mode == mode
                 and s.covers(block, page) and s.is_hole(block, page)]
        if holes:
            return ("ce=%d block=%d page=%d (%s) is a declared HOLE in %s -- "
                    "the reader produced no bytes for it.  A hole is NOT "
                    "erased media and is not served as 0xFF."
                    % (ce, block, page, mode, holes[0].file))
        msg = ("this capture holds no %s page for ce=%d block=%d page=%d "
               "(lun %d) under %s" % (mode, ce, block, page, lun, self.root))
        if other:
            msg += ("; it IS present in %s cell mode (%s).  The two are "
                    "DIFFERENT PHYSICAL PAGES -- a pseudo-SLC page read "
                    "without the prefix command comes off a different word "
                    "line -- so it is not served in its place.  Pass slc=%s "
                    "only if that is what you meant."
                    % (_mode_name(not slc), other[0].file, not slc))
        elif ce not in self.present_ces():
            msg += ("; this capture has no die mapped to ce %d at all "
                    "(mapped: %s, unmapped dies: %s)"
                    % (ce, self.image.ces(), self.image.unmapped_dies()))
        return msg

    # -- read retry -------------------------------------------------------
    # This backing cannot move a reference voltage -- the capture is over.  What
    # it can do is choose among the settings the reader already captured, which
    # is the only form of retry available offline and is exactly as useful,
    # provided the reader was asked for more than one pass.  Saying
    # `supports_retry = True` when several levels are present and False when
    # they are not is the honest report: "this page needs a level we do not
    # have" must never be confusable with "this page read fine".

    @property
    def supports_retry(self):
        return len(self._levels) > 1

    def retry_levels(self, slc=False):
        mode = _mode_name(slc)
        return sorted({s.retry_level for s in self.image.streams
                       if s.mode == mode})

    def set_retry_level(self, level, ce=None, slc=False):
        mode = _mode_name(slc)
        have = self.retry_levels(slc)
        if level not in have:
            raise RawImageError(
                "this capture holds no %s pages at retry level %d (it holds "
                "levels %s).  An offline image cannot generate a level the "
                "reader did not capture." % (mode, level, have))
        for c in ([ce] if ce is not None else self.present_ces()):
            self._level_pref[(c, mode)] = level
        return level

    def current_retry_level(self, ce, slc=False):
        return self._level_pref.get((ce, _mode_name(slc)), 0)

    def restore_retry_levels(self):
        n = len(self._level_pref)
        self._level_pref.clear()
        return n

    def retry_candidates(self, ce, block, page, lun=0, slc=False):
        """Every captured reading of one address.  -> [(level, bytes)]

        The offline shape of the EFC's read-retry ladder.  A reader that
        captured the same page at several reference-voltage settings gives us
        several independent readings; which one decodes is a question the codec
        answers, not the reader.
        """
        mode = _mode_name(slc)
        out = []
        for s in sorted((s for s in self.image.streams
                         if s.ce == ce and s.lun == lun and s.mode == mode
                         and s.covers(block, page) and not s.is_hole(block, page)),
                        key=lambda s: s.retry_level):
            out.append((s.retry_level, self.image.read_region(s, block, page)))
        return out

    def read_best(self, ce, block, page, lun=0, slc=False, correct=True):
        """The first captured reading that passes MCRC.  -> (level, bytes)

        Two passes, and the order is a measurement rather than a style choice:
        every level is tried uncorrected first, and only then is the LDPC
        decoder brought in.  An uncorrected decode is ~0.03 s and a corrected
        one is up to ~3.4 s, and a level that reads cleanly is strictly better
        evidence about the page than one that needs correcting -- so trying
        correction early would both cost more and pick a worse reading.

        Raises `RawImageError` naming every level tried when none does, because
        "no captured setting decodes this page" is a real and reportable
        finding about the capture -- it is what tells the operator to go back to
        the rig rather than to go looking for a decoder bug.
        """
        cands = self.retry_candidates(ce, block, page, lun=lun, slc=slc)
        if not cands:
            raise RawImageError(self._absent_message(ce, block, page, lun, slc))
        tried = []
        for fix in ((False, True) if correct else (False,)):
            for level, raw in cands:
                n = _mcrc_score(raw, block, page, correct=fix)
                tried.append((level, n) if not fix else (level, n))
                if n == geom.AUS_PER_PAGE_TLC:
                    return level, raw
        raise RawImageError(
            "ce%d b%d p%d (%s): no captured retry level decodes it -- "
            "MCRC by level %s.  On native TLC this is the EXPECTED result at "
            "level 0 for an upper word line (0 of 716 decode there); capture "
            "the vendor ladder, order 6, 1, 4."
            % (ce, block, page, _mode_name(slc),
               ", ".join("L%d:%d/4" % t for t in tried)))

    # -- write path (overlay only) ----------------------------------------
    def write_page(self, ce, block, page, data, lun=0, slc=False):
        """Into an in-memory overlay.  The capture files are never rewritten.

        A raw capture is frequently the only copy of a dead drive's contents;
        there is no version of "write it back" that is worth the risk here.
        """
        self._check_write()
        data = bytes(data)
        if len(data) != REGION_BYTES:
            raise RawImageError("a page is %d bytes, not %d"
                                % (REGION_BYTES, len(data)))
        self._overlay[(ce, block, page, bool(slc))] = data

    # -- capture quality ---------------------------------------------------
    def mode_check(self, ce, block, pages=None, lun=0):
        """Which cell mode is right for this block?  -> dict (see below)

        The A/B test, and the only sound one: the same address read both ways,
        with MCRC deciding.  MCRC runs over the complemented stored bytes, so it
        needs no seed, no key and no ECC and cannot be fooled by a decoder bug.

        Measured on this drive over 28 A/B pairs -- WERU 84, blocks 172..175,
        page 287, CEs 1/3/5/8/9/10/11 -- pseudo-SLC 4/4 and native TLC 0/4 on
        28 of 28, with no third outcome.
        That is the signature a reader that forgot the prefix command leaves,
        and it is the whole reason this method exists.

        -> a dict with `verdict` in {'slc', 'tlc', 'both-fail', 'ambiguous',
        'one-sided'}, the per-mode scores, and the pages compared.
        """
        both = {}
        for slc in (True, False):
            mode = _mode_name(slc)
            cand = sorted({p for s in self.image.streams
                           if s.ce == ce and s.lun == lun and s.mode == mode
                           and s.first_block <= block < s.first_block + s.blocks
                           for p in range(s.first_page, s.first_page + s.pages)
                           if not s.is_hole(block, p)})
            both[mode] = cand
        shared = sorted(set(both[MODE_SLC]) & set(both[MODE_TLC]))
        if pages is not None:
            shared = [p for p in shared if p in set(pages)]
        out = {"ce": ce, "block": block, "pages": shared,
               "slc": 0, "tlc": 0, "written": 0}
        if not shared:
            only = MODE_SLC if both[MODE_SLC] else (
                MODE_TLC if both[MODE_TLC] else None)
            out["verdict"] = "one-sided"
            out["note"] = (
                "this capture holds only the %s reading of block %d, so there "
                "is nothing to compare.  A single-mode capture CANNOT be "
                "validated by content alone if it is empty or erased; capture "
                "at least a few pages of every block BOTH ways."
                % (only, block))
            return out
        for p in shared:
            scores = {}
            for slc in (True, False):
                raw = self.read_page(ce, block, p, lun=lun, slc=slc)
                if raw.count(0xFF) == len(raw):
                    scores[_mode_name(slc)] = None
                    continue
                scores[_mode_name(slc)] = _mcrc_score(raw, block, p)
            if scores[MODE_SLC] is None and scores[MODE_TLC] is None:
                continue
            out["written"] += 1
            out[MODE_SLC] += scores[MODE_SLC] or 0
            out[MODE_TLC] += scores[MODE_TLC] or 0
        best = geom.AUS_PER_PAGE_TLC * max(out["written"], 1)
        if out["written"] == 0:
            out["verdict"] = "both-fail"
            out["note"] = "every shared page is erased in both modes"
        elif out[MODE_SLC] > out[MODE_TLC]:
            out["verdict"] = MODE_SLC
        elif out[MODE_TLC] > out[MODE_SLC]:
            out["verdict"] = MODE_TLC
        elif out[MODE_SLC] == 0:
            out["verdict"] = "both-fail"
            out["note"] = (
                "neither mode decodes.  That is not a mode problem -- suspect "
                "the read references (native TLC upper word lines never decode "
                "at level 0) or a page/spare split in the reader.")
        else:
            out["verdict"] = "ambiguous"
        out["note"] = out.get("note", "")
        out["score"] = "%d/%d slc, %d/%d tlc" % (out[MODE_SLC], best,
                                                 out[MODE_TLC], best)
        return out

    def audit(self, ces=None, blocks=None, limit=None, classifier=None,
              correct=True, progress=None):
        """Run `titania.verdict` over the capture.  -> Audit

        The question this answers is "did my capture work?", and it is the
        first thing to run after a rig finishes and before any analysis is
        built on the result.  It is region-aware -- group 2, group 0/1 and the
        FTL region obey three different rules -- because a blanket FORMAT_A test
        reports 1840 perfectly good group-2 pages as failures.
        """
        verdict = _verdict()
        cls = classifier or verdict.Classifier(None)
        counts = {}
        by_block = {}
        problems = []
        n = 0
        for ce, lun, mode, block, page in self.iter_addresses(ces=ces,
                                                              blocks=blocks):
            if limit is not None and n >= limit:
                break
            n += 1
            slc = mode == MODE_SLC
            try:
                raw = self.read_page(ce, block, page, lun=lun, slc=slc)
            except DeviceError:
                v = "unreadable"
            else:
                v = cls.classify(raw, ce, block, page, slc=slc,
                                 correct=correct).verdict
            counts[v] = counts.get(v, 0) + 1
            b = by_block.setdefault((ce, block, mode), {})
            b[v] = b.get(v, 0) + 1
            if v not in ("ok", "erased"):
                if len(problems) < 200:
                    problems.append((ce, block, page, mode, v))
            if progress is not None and n % 200 == 0:
                progress(n)
        return Audit(counts, by_block, problems, n)

    def iter_addresses(self, ces=None, blocks=None):
        """Every (ce, lun, mode, block, page) the capture can serve, sorted."""
        seen = set()
        for s in self.image.streams:
            if s.ce is None:
                continue
            if ces is not None and s.ce not in ces:
                continue
            for b in range(s.first_block, s.first_block + s.blocks):
                if blocks is not None and b not in blocks:
                    continue
                for p in range(s.first_page, s.first_page + s.pages):
                    if s.is_hole(b, p):
                        continue
                    k = (s.ce, s.lun, s.mode, b, p)
                    if k in seen:
                        continue
                    seen.add(k)
                    yield k

    # -- reporting --------------------------------------------------------
    def describe(self):
        im = self.image
        L = []
        L.append("raw capture at %s" % im.root)
        L.append("  page %d B in file, AU region %d B at offset %d"
                 % (im.page_bytes, REGION_BYTES, im.data_offset))
        if im.maker is not None:
            pref = SLC_PREFIX_BY_MAKER.get(im.maker)
            L.append("  maker 0x%02X, pseudo-SLC prefix %s"
                     % (im.maker,
                        "0x%02X" % pref if pref else "UNKNOWN -- unsupported"))
        if im.capture:
            for k in sorted(im.capture):
                L.append("  %-14s %s" % (k + ":", im.capture[k]))
        L.append("  %d stream(s), dies %s, ces %s, retry levels %s"
                 % (len(im.streams), im.dies(), im.ces(), im.retry_levels()))
        if im.unmapped_dies():
            L.append("  UNMAPPED DIES %s -- decoding works (the scrambler seed "
                     "is a function of the ROW alone) but no FTL address means "
                     "anything until these are named; see identify_dies()"
                     % im.unmapped_dies())
        for s in im.streams:
            L.append("  %-28s die%-2d ce=%-4s %s L%-2d b%d..%d p%d..%d "
                     "%d pages, %d holes"
                     % (s.file, s.die, s.ce, s.mode, s.retry_level,
                        s.first_block, s.first_block + s.blocks - 1,
                        s.first_page, s.first_page + s.pages - 1,
                        s.npages(), len(s.holes)))
            if s.note:
                L.append("      %s" % s.note)
        return "\n".join(L)

    def __repr__(self):
        return ("<RawImageDevice %s streams=%d ces=%s>"
                % (self.root, len(self.image.streams), self.present_ces()))


class Audit(object):
    """What `RawImageDevice.audit` found."""

    def __init__(self, counts, by_block, problems, total):
        self.counts = counts
        self.by_block = by_block
        self.problems = problems
        self.total = total

    @property
    def ok(self):
        return self.counts.get("ok", 0)

    def report(self):
        L = ["%d pages: %s" % (self.total,
                               ", ".join("%s %d" % (k, self.counts[k])
                                         for k in sorted(self.counts)))]
        for (ce, block, mode), c in sorted(self.by_block.items()):
            L.append("  ce%-2d b%-5d %-3s  %s"
                     % (ce, block, mode,
                        " ".join("%s=%d" % (k, c[k]) for k in sorted(c))))
        if self.problems:
            L.append("  first problems:")
            for ce, block, page, mode, v in self.problems[:20]:
                L.append("    ce%d b%d p%d %s -> %s" % (ce, block, page, mode, v))
        return "\n".join(L)

    def __repr__(self):
        return "<Audit %d pages ok=%d>" % (self.total, self.ok)


# --------------------------------------------------------------------------
# helpers that need the codec (imported lazily -- it pulls in numpy)
# --------------------------------------------------------------------------
def _sib(name):
    """Import a sibling module whether this is `titania.rawimage` or a script.

    `from . import x` raises when the file is run directly, which is how a
    media check can look like a clean skip while actually never having run.
    """
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


def _codec():
    return _sib("codec")


def _verdict():
    return _sib("verdict")


def _mcrc_score(raw, block, page, correct=False):
    """How many of the four AUs pass MCRC.  -> 0..4

    No seed, no key, no ECC: MCRC runs over the complemented stored bytes, so
    this is the cheapest possible "did I read this page correctly" and the only
    test that is valid on a page whose scrambler seed is unknown.
    """
    codec = _codec()
    res = codec.decode_page(raw, row=geom.compose_row(block, page),
                            correct=correct)
    return sum(1 for a in res.aus if a.mcrc_ok)


# --------------------------------------------------------------------------
# Die identification -- which physical die is which chip enable?
# --------------------------------------------------------------------------
#: What `identify_dies` can conclude, and how strong each conclusion is.
IDENT_EXACT = "exact"
IDENT_PAIR = "pair"          # narrowed to {2k, 2k+1}
IDENT_BANK = "bank"          # narrowed to 0..5 or 6..11
IDENT_NONE = "none"


def identify_dies(dev, dies=None, rootstorage_pages=(0, 1, 2, 3, 4, 5)):
    """Narrow each die's chip-enable number from the media itself.

    -> {die: {'ce': int|None, 'candidates': [...], 'basis': IDENT_*, 'why': str}}

    An external reader numbers dies by socket order and knows nothing about the
    EFC's chip enables.  Decoding does not care -- the scrambler seed is a
    function of the row and of nothing else, so LDPC, descrambling, MCRC and aux
    all work on an unlabelled die -- but `physicalBlockId = 12*block + ce` is
    the FTL's only name for a block, so a label is needed before an LMA means
    anything.

    Two structural facts do most of the work, and neither needs an external
    reference file:

    * block 0 on CE 0..5 is the group-2 firmware region and on CE 6..11 it
      is not (`geometry.region`; measured -- CE6's block 0 is MCRC-clean
      FORMAT_A owned by msoId 57).  Its row 0 is a 0x5A device header and row 1
      a firmware header whose magic is `1d 33 47 77`.  So "does block 0 row 1
      carry the group-2 header" splits the twelve dies into the two banks of
      six.  CE0 additionally does not answer at all on this board.
    * blocks 2..5 are the FTL RootStorage's Bootstrap instance, whose every
      page carries its own stripe number at offset +0x02, and the placement law
      `stripe = base + 6*page + (ce // 2)` holds with 0 violations over 5273
      page/stripe pairs.  Since `base` is always a multiple of
      `368 * 6`, `(stripe - 6*page) mod 6` is `ce // 2` -- confirmed here on
      44 of 44 (block, CE) combinations.  Block 1 is the Backup instance and
      carries `(ce // 2) mod 3` instead; `_rootstorage_half` excludes it.

    Together those give the CE pair {2k, 2k+1}.  Splitting the pair needs one
    more bit and there are exactly two known routes, both documented in
    `NAND_RAW_IMAGE.md` ss4 and neither implemented here because both need
    material outside this module:

      (a) the group 0/1 interleave.  An element is 12 entries ordered
          `ce6b8 ce6b9 ce7b8 ...` with `unit = page_in_block * 12 + entry`, so
          matching a decoded block 8/9 page against a known plaintext image
          (the reference firmware image is fw 0x12 exactly, 128 units) pins the entry and
          hence the CE.  Verified 47/47 units byte-exact.
      (b) an FTL round trip: decode any written FTL page, take AU 0's aux LMA
          and ask `Ftl.lma_to_physical` where that LMA lives.  The answer names
          the CE.  Needs a mounted `Ftl`.
    """
    codec = _codec()
    out = {}
    for die in (dies if dies is not None else dev.image.dies()):
        streams = [s for s in dev.image.streams if s.die == die]
        rec = {"ce": None, "candidates": list(range(geom.CE_COUNT)),
               "basis": IDENT_NONE, "why": []}
        ce_hint = next((s.ce for s in streams if s.ce is not None), None)

        # --- bank, from block 0 ------------------------------------------
        # Any written row of block 0 answers: group 2's data region ends at
        # byte 16260 and 16260..18431 is erased 0xFF on all 368 rows, while a
        # FORMAT_A page uses every one of its 18432 bytes.
        bank = None
        for page in (0, 1, 2, 3):
            for s in streams:
                if s.mode != MODE_SLC or not s.covers(0, page):
                    continue
                try:
                    raw = dev.image.read_region(s, 0, page)
                except RawImageError:
                    continue
                if raw.count(0xFF) == len(raw):
                    continue
                if _looks_group2(raw):
                    bank = "A"
                    rec["why"].append(
                        "block 0 row %d has group 2's erased tail (16260.."
                        "18431) -> this die is one of CE 0..5" % page)
                else:
                    bank = "B"
                    rec["why"].append(
                        "block 0 row %d uses all 18432 bytes, so it is not "
                        "group 2 -> this die is one of CE 6..11" % page)
                break
            if bank is not None:
                break
        if bank == "A":
            rec["candidates"] = [c for c in rec["candidates"] if c < 6]
            rec["basis"] = IDENT_BANK
        elif bank == "B":
            rec["candidates"] = [c for c in rec["candidates"] if c >= 6]
            rec["basis"] = IDENT_BANK

        # --- ce // 2, from the RootStorage stripe law ---------------------
        half = _rootstorage_half(dev, die, rootstorage_pages)
        if half is not None:
            rec["candidates"] = [c for c in rec["candidates"]
                                 if c // 2 == half]
            rec["basis"] = IDENT_PAIR
            rec["why"].append("RootStorage stripe law gives ce // 2 == %d "
                              "((stripe - 6*page) mod 6, blocks 2..5; 44/44 "
                              "here, 0 violations over 5273 pairs)" % half)
        if len(rec["candidates"]) == 1:
            rec["ce"] = rec["candidates"][0]
            rec["basis"] = IDENT_EXACT
        if ce_hint is not None:
            rec["declared_ce"] = ce_hint
            rec["agrees"] = ce_hint in rec["candidates"]
        rec["why"] = "; ".join(rec["why"]) or "no identifying page was captured"
        out[die] = rec
    return out


#: `nd_g2extract` calls this the fw header magic; it is at payload offset 0 of
#: row 1 of block 0 on CE 0..5.
GROUP2_MAGIC = bytes((0x1D, 0x33, 0x47, 0x77))


def _looks_group2(raw):
    """Is this page the group-2 region?  Structural, no seed needed.

    Group 2 is 5 chunks of 4 codewords of (512 payload + 300 parity) plus a
    4-byte gap, ending at byte 16260, and bytes 16260..18431 are ERASED 0xFF on
    all 368 rows.  A FORMAT_A page uses every one of its 18432 bytes, so the
    erased tail alone separates them, and it costs nothing to check.
    """
    if len(raw) < REGION_BYTES:
        return False
    tail = raw[16260:REGION_BYTES]
    return tail.count(0xFF) == len(tail)


#: The RootStorage "Bootstrap" instance.  Block 1 is the "Backup" instance and
#: is excluded -- see `_rootstorage_half`.
ROOTSTORAGE_BLOCKS = (2, 3, 4, 5)
ROOTSTORAGE_K = 6


def _rootstorage_half(dev, die, pages):
    """`ce // 2` from a RootStorage page's own stripe number, or None.

    One NAND page is one stripe part; its header is `u16 21` at +0 then
    `u16 stripeNumber` at +2, and the placement law for the Bootstrap instance
    on blocks 2..5 is `stripe = base + 6*page + (ce // 2)`.  `base` is the
    object's start stripe and is not known from one die -- but it does not have
    to be, because it is always a multiple of `pages_per_block * K`
    (368 * 6 = 2208) and therefore always 0 mod 6.  So

        (stripe - 6*page) mod 6  ==  ce // 2

    MEASURED, not assumed: this holds on 44 of 44 (block, CE) combinations
    -- blocks 2, 3, 4 and 5 against all eleven live CEs -- with the two bases
    that actually occur on this drive, 0 and 6624.

    Block 1 is excluded and that is a finding, not caution.  It is the
    Backup instance with K = 3, and it does not carry `ce // 2`: measured, its
    stripe number carries `(ce // 2) mod 3` (ce6/7 read 3312, i.e. residue 0,
    where `ce // 2` is 3), and its two banks sit at different bases (0 and
    3312).  Using it would put every bank-B die in the wrong pair.
    """
    codec = _codec()
    for block in ROOTSTORAGE_BLOCKS:
        for page in pages:
            for s in dev.image.streams:
                if s.die != die or s.mode != MODE_SLC:
                    continue
                if not s.covers(block, page) or s.is_hole(block, page):
                    continue
                try:
                    raw = dev.image.read_region(s, block, page)
                except RawImageError:
                    continue
                if raw.count(0xFF) == len(raw):
                    continue
                res = codec.decode_page(raw, row=geom.compose_row(block, page),
                                        correct=False)
                if not all(a.mcrc_ok for a in res.aus):
                    continue
                head = res.aus[0].user
                if int.from_bytes(head[0:2], "little") != 21:
                    continue
                stripe = int.from_bytes(head[2:4], "little")
                if stripe == 0xFFFF:                # cInvalidStripeNumber
                    continue
                return (stripe - ROOTSTORAGE_K * page) % 6
    return None


# --------------------------------------------------------------------------
# Synthesis -- build a raw image from any Device
# --------------------------------------------------------------------------
def synthesize(dev, spec, out_dir, page_bytes=REGION_BYTES, data_offset=0,
               fill=b"\x00", capture=None, maker=0xEC, quiet=True):
    """Write a raw capture from an existing `Device`.  -> RawImage

    `spec` is a list of dicts, each one stream:

        {"die": 1, "ce": 1, "mode": "slc", "first_block": 26, "blocks": 1,
         "first_page": 0, "pages": 24, "retry_level": 0, "note": "..."}

    Pages the source device cannot serve become declared holes -- recorded in
    the manifest and raising on read -- rather than being filled with 0xFF and
    silently becoming "erased media".  That distinction is the whole reason
    this project has a synthesiser at all: it lets the raw path be exercised
    end to end against bytes whose correct decode is already known, without
    inventing a single one of them.

    This is a test and staging tool.  It does not make an EFC-sourced dump into
    an external capture; the provenance of every byte is whatever the source
    device's was.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    streams = []
    for i, sp in enumerate(spec):
        mode = sp.get("mode", MODE_SLC)
        slc = _mode_slc(mode)
        die = int(sp.get("die", sp.get("ce", i)))
        first_block = int(sp.get("first_block", 0))
        blocks = int(sp.get("blocks", 1))
        first_page = int(sp.get("first_page", 0))
        pages = int(sp.get("pages", geom.pages_per_block(slc)))
        level = int(sp.get("retry_level", 0))
        name = sp.get("file") or ("die%02d_%s_L%d_b%05d.bin"
                                  % (die, mode, level, first_block))
        holes = []
        path = out_dir / name
        pad = fill * page_bytes if len(fill) == 1 else bytes(fill)
        with open(path, "wb") as f:
            for b in range(first_block, first_block + blocks):
                for p in range(first_page, first_page + pages):
                    try:
                        raw = dev.read_page(sp.get("ce", die), b, p,
                                            lun=sp.get("lun", 0), slc=slc)
                    except DeviceError:
                        holes.append((b, p))
                        f.write(pad)
                        continue
                    if len(raw) != REGION_BYTES:
                        raise RawImageError("source served %d bytes for "
                                            "ce%s b%d p%d" % (len(raw),
                                                              sp.get("ce"), b, p))
                    buf = bytearray(pad)
                    buf[data_offset:data_offset + REGION_BYTES] = raw
                    f.write(bytes(buf))
        streams.append(Stream(file=name, die=die, ce=sp.get("ce"),
                              lun=sp.get("lun", 0), mode=mode,
                              first_block=first_block, blocks=blocks,
                              first_page=first_page, pages=pages,
                              retry_level=level, holes=holes,
                              note=sp.get("note", "")))
        if not quiet:
            print("  wrote %s (%d pages, %d holes)"
                  % (name, blocks * pages, len(holes)))
    img = RawImage(root=out_dir, page_bytes=page_bytes,
                   data_offset=data_offset, streams=streams,
                   capture=capture or {}, maker=maker)
    img.write()
    return img


def flat_image(path, die=0, ce=None, mode=MODE_SLC, first_block=0, blocks=None,
               first_page=0, pages=None, page_bytes=REGION_BYTES,
               data_offset=0, lun=0, retry_level=0, maker=None, note=""):
    """A `RawImage` over one flat file with no manifest on disk.

    For the common case of "here is a linear dump of one die".  `blocks` is
    derived from the file size when not given.  Nothing is written to disk, so
    this never turns a capture directory into something it was not.
    """
    path = Path(path)
    slc = _mode_slc(mode)
    if pages is None:
        pages = geom.pages_per_block(slc)
    if blocks is None:
        size = path.stat().st_size
        blocks = size // (pages * page_bytes)
        if blocks < 1:
            raise RawImageError(
                "%s is %d bytes, less than one block of %d pages x %d B.  "
                "Check page_bytes: a capture that dropped the 2048-byte spare "
                "area is 16384 B/page and cannot be decoded."
                % (path, size, pages, page_bytes))
    s = Stream(file=path.name, die=die, ce=ce, lun=lun, mode=mode,
               first_block=first_block, blocks=blocks, first_page=first_page,
               pages=pages, retry_level=retry_level, note=note)
    return RawImage(root=path.parent, page_bytes=page_bytes,
                    data_offset=data_offset, streams=[s], maker=maker)


# ------------------------------------------------------------------ selftest --
def _mk_spec():
    """The four regions and both cell modes, from addresses the store holds."""
    return [
        # FTL region, pseudo-SLC, aux layout 0 (RootStorage-adjacent metadata)
        dict(die=1, ce=1, mode=MODE_SLC, first_block=26, blocks=1,
             first_page=0, pages=24,
             note="FTL region, pseudo-SLC, aux is an LMA array"),
        # group 0/1 firmware images, pseudo-SLC
        dict(die=7, ce=7, mode=MODE_SLC, first_block=8, blocks=1,
             first_page=0, pages=8,
             note="fw group 1 (blocks 8/9), aux is a constant tag not an LMA"),
        # group 2, its own 20x512 ECC geometry
        dict(die=1, ce=1, mode=MODE_SLC, first_block=0, blocks=1,
             first_page=0, pages=8,
             note="fw group 2: 20 codewords x 512 + 300 parity, erased tail"),
        # native TLC, upper word lines, captured at a nonzero retry level
        dict(die=1, ce=1, mode=MODE_TLC, first_block=852, blocks=1,
             first_page=528, pages=24, retry_level=1,
             note="native TLC upper word lines; level 0 does not decode these"),
        # the cell-mode A/B pair on WERU 84
        dict(die=3, ce=3, mode=MODE_SLC, first_block=172, blocks=1,
             first_page=287, pages=1,
             note="WERU 84 is pseudo-SLC: the correct capture"),
        dict(die=3, ce=3, mode=MODE_TLC, first_block=172, blocks=1,
             first_page=287, pages=1,
             note="the SAME address captured WITHOUT the pseudo-SLC prefix"),
        # the two pages die identification reads: block 0 for the bank, and a
        # RootStorage page for `ce // 2`
        dict(die=1, ce=1, mode=MODE_SLC, first_block=2, blocks=1,
             first_page=0, pages=2, note="RootStorage Bootstrap, gives ce//2"),
        dict(die=8, ce=8, mode=MODE_SLC, first_block=0, blocks=1,
             first_page=0, pages=2,
             note="block 0 on a BANK-B die: not group 2, ordinary FORMAT_A"),
        dict(die=8, ce=8, mode=MODE_SLC, first_block=2, blocks=1,
             first_page=0, pages=2, note="RootStorage Bootstrap, gives ce//2"),
    ]


def _source_device(root=None):
    """A device for the self-test, or None when there is no dump to open.

    Returning None rather than raising is deliberate: this feeds a self-test
    that skips the media-backed checks when no media is present, and a missing
    dump is the normal state of a fresh checkout, not a failure.
    """
    dd = _sib("dumpdev")
    if root is None:
        root = dd.default_root(required=False)
        if root is None:
            return None
    try:
        if Path(root).exists():
            return dd.DumpDevice(root)
    except DeviceError:
        pass
    return None


def _selftest(argv=()):
    import shutil
    import tempfile

    quick = "--quick" in argv
    argv = [a for a in argv if a != "--quick"]
    root = argv[0] if argv else None

    ok = True
    checks = []

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        checks.append((name, bool(cond)))
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               ("  -- " + detail) if detail else ""))

    print("titania.rawimage -- selftest\n")

    # --- 1. pure-arithmetic checks, no data needed ------------------------
    print("[1] manifest validation (no media needed)")
    try:
        RawImage(root=".", page_bytes=16384)
        check("a 16384-byte page is REFUSED", False)
    except RawImageError as exc:
        check("a 16384-byte page is REFUSED", "spare" in str(exc),
              "the spare area carries part of AU 3")
    try:
        RawImage(root=".", page_bytes=REGION_BYTES, streams=[
            Stream("a.bin", die=0, ce=0, mode=MODE_SLC, first_block=0,
                   blocks=1, pages=4),
            Stream("b.bin", die=0, ce=0, mode=MODE_SLC, first_block=0,
                   blocks=1, pages=4)])
        check("two streams claiming one address are REFUSED", False)
    except RawImageError as exc:
        check("two streams claiming one address are REFUSED",
              "not one stream" in str(exc))
    try:
        RawImage(root=".", page_bytes=REGION_BYTES, streams=[
            Stream("a.bin", die=0, ce=0, mode=MODE_SLC, blocks=1, pages=1),
            Stream("b.bin", die=1, ce=0, mode=MODE_SLC, blocks=1, pages=1)])
        check("one CE claimed by two dies is REFUSED", False)
    except RawImageError as exc:
        check("one CE claimed by two dies is REFUSED", "claimed by dies" in str(exc))
    s = Stream("x.bin", die=0, ce=0, mode=MODE_TLC, first_block=10, blocks=3,
               first_page=0, pages=1104)
    check("linear addressing is block-major, page-minor",
          s.index(10, 0) == 0 and s.index(10, 1) == 1
          and s.index(11, 0) == 1104 and s.index(12, 1103) == 3 * 1104 - 1)
    check("pseudo-SLC is 368 pages/block, native TLC 1104",
          geom.pages_per_block(True) == 368
          and geom.pages_per_block(False) == 1104)
    check("the AU region is the WHOLE page, spare included",
          REGION_BYTES == geom.AU_STORED * geom.AUS_PER_PAGE_TLC == 18432
          and 16384 < geom.AU_STORED * 4)
    check("AU 3 straddles the 16384 data/spare boundary",
          3 * geom.AU_STORED < 16384 < 4 * geom.AU_STORED,
          "AU 3 is bytes 13824..18431")
    check("the vendor SLC prefix is the ONLY command-level difference",
          set(SLC_PREFIX_BY_MAKER.values()) == {0xDA, 0xA2}
          and SLC_PREFIX_BY_MAKER[0xEC] == 0xDA)

    src = _source_device(root)
    if src is None:
        print("\n[2..] no page store or dump tree here -- SKIPPED")
        print("\nSELFTEST: %d PASS, %d FAIL (media checks SKIPPED)"
              % (sum(1 for _n, c in checks if c),
                 sum(1 for _n, c in checks if not c)))
        return 0 if ok else 1

    tmp = Path(tempfile.mkdtemp(prefix="rawimage_selftest_",
                               dir=str(Path(__file__).resolve().parent.parent)))
    try:
        # --- 2. synthesise -----------------------------------------------
        print("\n[2] synthesise a raw image from real stored pages")
        spec = _mk_spec()
        img = synthesize(src, spec, tmp, maker=0xEC, capture={
            "reader": "SYNTHETIC -- pages served by titania.dumpdev",
            "note": "not an external capture; the raw PATH is what is proved"})
        dev = RawImageDevice(tmp)
        for line in dev.describe().splitlines():
            print("    " + line)
        served = sum(1 for _ in dev.iter_addresses())
        holes = sum(len(s.holes) for s in dev.image.streams)
        check("image builds and reloads from its manifest",
              len(dev.image.streams) == len(spec),
              "%d streams, %d pages servable, %d holes"
              % (len(dev.image.streams), served, holes))
        check("holes are declared, not filled with 0xFF", holes > 0,
              "%d pages the source could not serve" % holes)

        # --- 3. byte identity --------------------------------------------
        print("\n[3] every served page is byte-identical to the source")
        same = diff = 0
        for ce, lun, mode, block, page in dev.iter_addresses():
            slc = mode == MODE_SLC
            a = dev.read_page(ce, block, page, lun=lun, slc=slc)
            b = src.read_page(ce, block, page, lun=lun, slc=slc)
            if a == b and len(a) == REGION_BYTES:
                same += 1
            else:
                diff += 1
        check("round trip is byte-exact", diff == 0 and same == served,
              "%d/%d identical over all %d bytes" % (same, served, REGION_BYTES))

        # --- 4. a hole raises, and never reads as erased ------------------
        print("\n[4] the four rules this backing will not bend")
        hole = None
        for s in dev.image.streams:
            if s.holes:
                hole = (s, sorted(s.holes)[0])
                break
        if hole is None:
            check("a hole raises rather than reading 0xFF", False, "no holes")
        else:
            s, (hb, hp) = hole
            try:
                dev.read_page(s.ce, hb, hp, slc=s.slc)
                check("a hole raises rather than reading 0xFF", False)
            except DeviceError as exc:
                check("a hole raises rather than reading 0xFF",
                      "HOLE" in str(exc) and "erased" in str(exc))
        try:
            dev.read_page(1, 4095, 1103, slc=False)
            check("an absent page raises", False)
        except DeviceError as exc:
            check("an absent page raises", "\xff" not in str(exc).lower())
        try:
            dev.read_page(1, 26, 0, slc=False)          # captured SLC only
            check("the wrong cell mode raises and says so", False)
        except DeviceError as exc:
            check("the wrong cell mode raises and says so",
                  "DIFFERENT PHYSICAL PAGES" in str(exc))
        dev.enable_writes(CONFIRM)
        before = (tmp / dev.image.streams[0].file).stat().st_size
        dev.write_page(1, 26, 0, b"\x00" * REGION_BYTES, slc=True)
        after = (tmp / dev.image.streams[0].file).stat().st_size
        check("writes go to an overlay, never to the capture file",
              before == after and dev.read_page(1, 26, 0, slc=True)
              == b"\x00" * REGION_BYTES)
        dev._overlay.clear()
        dev.writes_enabled = False

        # --- 5. the cell-mode oracle -------------------------------------
        print("\n[5] cell mode: the A/B oracle a raw reader needs")
        mc = dev.mode_check(3, 172)
        check("MCRC picks pseudo-SLC over the prefix-less read",
              mc["verdict"] == MODE_SLC and mc[MODE_SLC] == 4
              and mc[MODE_TLC] == 0,
              "ce3 b172 p287: %s" % mc["score"])

        # --- 6. retry candidates -----------------------------------------
        print("\n[6] read retry, offline")
        lv = dev.retry_levels(slc=False)
        check("captured retry levels are reported per mode",
              lv == [0, 1] and dev.retry_levels(slc=True) == [0]
              and dev.supports_retry,
              "tlc levels %s, slc levels %s" % (lv, dev.retry_levels(slc=True)))
        tlc_pages = [(b, p) for (_c, _l, m, b, p) in dev.iter_addresses()
                     if m == MODE_TLC and b == 852]
        if tlc_pages:
            b, p = tlc_pages[0]
            level, raw = dev.read_best(1, b, p, slc=False)
            check("read_best returns the level that decodes",
                  level == 1 and _mcrc_score(raw, b, p, correct=True) == 4
                  and _mcrc_score(raw, b, p) < 4,
                  "ce1 b%d p%d: level %d, MCRC 0/4 raw and 4/4 after EE3 "
                  "correction -- a retry-rescued upper word line still needs "
                  "the decoder" % (b, p, level))
            pr = dev.read_page_ex(1, b, p, slc=False)
            check("read_page_ex reports the capture's retry level",
                  pr.retry == 1 and pr.source == FULL)
        try:
            dev.set_retry_level(7, slc=False)
            check("an uncaptured retry level is REFUSED", False)
        except RawImageError as exc:
            check("an uncaptured retry level is REFUSED",
                  "did not capture" in str(exc))

        # --- 7. die identification ---------------------------------------
        print("\n[7] die -> CE from the media alone")
        ident = identify_dies(dev)
        for die in sorted(ident):
            print("    die %-3d %-6s candidates=%-22s %s"
                  % (die, ident[die]["basis"], ident[die]["candidates"],
                     ident[die]["why"][:96]))
        d1, d8, d7 = ident.get(1, {}), ident.get(8, {}), ident.get(7, {})
        check("block 0's group-2 header puts die 1 in bank A, "
              "RootStorage narrows it to a pair",
              d1.get("candidates") == [0, 1] and d1.get("basis") == IDENT_PAIR
              and d1.get("agrees") is True)
        check("a bank-B die is narrowed the same way, from the same two pages",
              d8.get("candidates") == [8, 9] and d8.get("basis") == IDENT_PAIR
              and d8.get("agrees") is True)
        check("a die with no identifying page is honestly UNRESOLVED",
              d7.get("basis") == IDENT_NONE and d7.get("ce") is None,
              d7.get("why", ""))

        # --- 8. The proof: the full stack decodes the raw image -----------
        print("\n[8] end to end: LDPC -> descramble -> MCRC -> aux")
        verdict = _verdict()
        cls = None
        ftl_note = "aux layout NOT consulted (--quick)"
        if not quick:
            try:
                Ftl = _sib("ftl").Ftl
                print("    mounting the FTL off the source store "
                      "(~50 s) for the aux layout rules ...")
                cls = verdict.Classifier(Ftl.from_device(src))
                ftl_note = "aux layout from msoDescriptor +17"
            except Exception as exc:                        # noqa: BLE001
                print("    FTL mount failed (%s) -- falling back" % exc)
        if cls is None:
            cls = verdict.Classifier(None)
        aud = dev.audit(classifier=cls)
        for line in aud.report().splitlines():
            print("    " + line)
        # ce3 b172 in TLC is the bad stream -- the same address a
        # reader that forgot the pseudo-SLC prefix would have captured.  It must
        # not decode; if it ever did, this whole warning would be wrong.
        bogus = [p for p in aud.problems if (p[0], p[1], p[3]) == (3, 172, MODE_TLC)]
        real = [p for p in aud.problems
                if (p[0], p[1], p[3]) != (3, 172, MODE_TLC)]
        check("every correctly-captured page decodes (%s)" % ftl_note,
              not real and aud.ok == aud.total - 1,
              "%d of %d ok; the 1 failure is the deliberate prefix-less "
              "capture" % (aud.ok, aud.total))
        check("the prefix-less capture of a pseudo-SLC page FAILS",
              len(bogus) == 1,
              "ce3 b172 p287 read without the 0xDA prefix -> %s"
              % (bogus[0][4] if bogus else "DECODED, which would refute the "
                                           "cell-mode warning"))

        # aux, explicitly, on the FTL-region pages
        codec = _codec()
        auxmod = _sib("auxdata")
        naux = nlma = 0
        for ce, lun, mode, block, page in dev.iter_addresses():
            if geom.region(ce, block) != geom.REGION_FTL:
                continue
            raw = dev.read_page(ce, block, page, lun=lun, slc=(mode == MODE_SLC))
            res = codec.decode_page(raw, row=geom.compose_row(block, page),
                                    correct=False)
            if not all(a.mcrc_ok for a in res.aus):
                continue
            auxes = [a.aux for a in res.aus]
            layout = 0
            if cls is not None and cls.ftl is not None:
                layout = cls.layout_of(block, ce)
            ev = auxmod.read_evidence(auxes, layout)
            naux += 1
            nlma += 1 if ev["ok"] is True else 0
        check("aux passes the owning layout's own read rule",
              naux > 0 and nlma == naux,
              "%d/%d FTL-region pages" % (nlma, naux))

        # --- 9. group-2 and group-0/1 regions, decoded from the raw image --
        print("\n[9] the two non-FORMAT_A regions, from the same image")
        g2ok = sum(1 for (ce, _l, m, b, p) in dev.iter_addresses()
                   if geom.region(ce, b) == geom.REGION_FW_GROUP2
                   and cls.classify(dev.read_page(ce, b, p,
                                                  slc=(m == MODE_SLC)),
                                    ce, b, p,
                                    slc=(m == MODE_SLC)).verdict == "ok")
        g01ok = sum(1 for (ce, _l, m, b, p) in dev.iter_addresses()
                    if geom.region(ce, b) == geom.REGION_FW_GROUP01
                    and cls.classify(dev.read_page(ce, b, p,
                                                   slc=(m == MODE_SLC)),
                                     ce, b, p,
                                     slc=(m == MODE_SLC)).verdict == "ok")
        check("group 2 (20x512 + A10R) decodes from the raw image", g2ok == 8,
              "%d/8 pages" % g2ok)
        check("group 0/1 (FORMAT_A, no LMA crib) decodes", g01ok == 8,
              "%d/8 pages" % g01ok)

        # --- 10. a flat file with no manifest ----------------------------
        print("\n[10] a bare flat file, no manifest")
        flat = flat_image(tmp / dev.image.streams[0].file, die=1, ce=1,
                          mode=MODE_SLC, first_block=26, blocks=1,
                          first_page=0, pages=24)
        fdev = RawImageDevice(image=flat)
        a = fdev.read_page(1, 26, 3, slc=True)
        b = src.read_page(1, 26, 3, slc=True)
        check("flat_image serves the same bytes", a == b)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _n, c in checks if c)
    nfail = len(checks) - npass
    print("\nSELF-TEST: %d PASS, %d FAIL" % (npass, nfail))
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        argv.remove("--selftest")
        return _selftest(argv)
    want_audit = "--audit" in argv
    if want_audit:
        argv.remove("--audit")
    want_ident = "--identify" in argv
    if want_ident:
        argv.remove("--identify")
    if not argv:
        print(__doc__.strip())
        return 0
    dev = RawImageDevice(argv[0])
    print(dev.describe())
    if want_ident:
        print("\ndie -> CE:")
        for die, rec in sorted(identify_dies(dev).items()):
            print("  die %-3d ce=%-5s %-6s candidates=%s"
                  % (die, rec["ce"], rec["basis"], rec["candidates"]))
            print("      %s" % rec["why"])
    if want_audit:
        print("\naudit:")
        print(dev.audit().report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
