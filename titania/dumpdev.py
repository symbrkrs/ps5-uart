"""DumpDevice -- a `Device` backed by raw page files on disk.

    dev = DumpDevice()            # the whole media -> `the store`
    dev = DumpDevice("<dump>/slc")        # or just one dumper's directory
    page = dev.read_page(ce=6, block=8, page=0, slc=True)

`DumpDevice()` opens `the store`, the blob-plus-index store built from
that tree, because the tree is a dump directory that gets pruned as pages are
proven byte-for-byte to be in the store, while the store stays whole.  See
`STORE_FOR_TREE` and `canonical_root()`; `prefer_store=False` reads the files.

WHY DISCOVERY RATHER THAN A FIXED LAYOUT
----------------------------------------
This tree was written by a dozen different dumpers over several weeks and every
one of them picked its own file name.  Rather than bless one, `DumpDevice` walks
the directory, matches each file against the conventions that actually exist
(`LAYOUTS` below), and builds an index.  Anything it cannot place is recorded in
`self.rejected` with a reason and shown by `describe()`, so an unrecognised
naming scheme is visible instead of silently halving the coverage.

FOUR RULES THIS CLASS WILL NOT BEND
-----------------------------------
1. A missing page is a `DeviceError`, never 0xFF.  Confusing "absent from
   the dump" with "erased on the media" has produced wrong conclusions in this
   project more than once, so there is no code path here that can invent an
   erased page.  `read_page` says how many pages the dump *does* hold for that
   block, which is usually enough to tell a gap from a typo.

2. Cell mode is part of the key.  A pseudo-SLC page and a native-TLC page of
   the same (ce, block, page) are different bytes off different word lines.  Most
   dumpers recorded the mode only in the directory name, so that is where it is
   read from (`_MODE_BY_DIR`); a file name that carries the mode explicitly
   (`..._slc.bin` / `..._tlc.bin`) is believed over the directory, because a
   recorded mode beats an inferred one.  Asking for the wrong mode raises rather
   than returning the other one's bytes.

3. A collision is decided by decoding both candidates, never by path sort order
   and never by provenance.  See below.

4. Writes never touch the files they were opened from.  See write safety.

HOW A COLLISION IS DECIDED
--------------------------
Two files can claim the same (ce, block, page, mode).  Originally the winner was
whichever path sorted first, which is not a policy -- it is how `<dump>/deep`
came to outrank the good capture for 157 of 251 overlapping pages before anyone
noticed.  That was replaced by a per-directory rank table, and the rank table has
since been replaced by the only thing that actually answers the question:

    decode both, and keep the one that reads.

`adjudicate()` compares, in order: the AU MCRCs uncorrected (computed over the
complemented stored bytes, so they need no seed, no key and no ECC); whether the
LDPC decode CONVERGES at all; the MCRCs after correction; and the correction
count, which sees the EE3 parity region that MCRC does not cover.  On the
reference store that settles 264 of 265 contested pages.  The one it leaves is
CE0 -- a dead die returning a bus constant, so neither candidate is a
measurement and there is nothing to settle.

Where content genuinely runs out, the answer is UNRESOLVED and the page is
reported as contested.  There is no tiebreak of last resort, because the last
one -- a table of directory ranks -- turned out on measurement to decide nothing
that content could not decide better.

`conflict_report()` returns one `Adjudication` per disagreement naming both
paths, the winner, the basis and the reason.  Conflicts are never resolved
silently and never by "newer", "larger" or "first".

WRITE SAFETY
------------
`writable = True`, but a write still needs `enable_writes(CONFIRM)` and lands in
an in-memory copy-on-write overlay.  The originals are only modified if the
caller also passed `in_place=True` to the constructor -- two independent opt-ins,
the same shape as `HardwareDevice`.  `flush(dir)` materialises the overlay into a
new directory, which is the intended way to produce a modified dump.

    py -m titania.dumpdev --selftest
    py -m titania.dumpdev [root] --conflicts      # every disagreement, and why
    py titania/dumpdev.py --selftest [root]
"""
import hashlib
import json
import os
import re
import sys
from collections import namedtuple
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):                       # allow direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import (Device, DeviceError, WriteProtected, CONFIRM,
                                PageRead, PageScan, FULL)
else:
    from . import geometry as geom
    from .device import (Device, DeviceError, WriteProtected, CONFIRM,
                         PageRead, PageScan, FULL)

#: bytes in a raw page as the NAND hands it over: 16384 data + 2048 spare.
PAGE_BYTES = 18432

def _config():
    """`titania.config`, importable under both package and direct execution."""
    try:
        from . import config as _cfg
    except (ImportError, ValueError):                   # direct execution
        import config as _cfg                           # noqa: F401
    return _cfg


def default_root(required=True):
    """The dump to open when the caller named none.  -> Path (or None)

    A function and not a constant, because the answer depends on the
    environment and on the working directory, and a constant computed at import
    would freeze whichever of those happened to be true when the first module
    imported this one.  See `titania.config` for the resolution order.
    """
    return _config().default_root(required=required)

#: Explicit whole-tree root -> page store overrides, by directory name, keyed on
#: the root's own name.  Empty by default: the redirect below is now derived from
#: what the stores themselves record, so nothing has to be listed here.  It
#: remains as an escape hatch for a store whose `source_tree` was written wrong
#: or not at all, where the pairing genuinely cannot be discovered.
#:
#: Why the redirect exists.  A page store is built from a dump tree and serves
#: the same page keys out of blobs, so a whole-tree read has two backings that
#: must agree -- and `titania.dumpdev --selftest` [9j] proves page-for-page that
#: they do.  A reclaim then removes from the tree only files whose exact bytes it
#: has just re-read out of the store, which makes the tree a shrinking subset of
#: the store while the store stays whole.  Every caller that opened the tree to
#: read *pages* therefore wants the store; without this it would silently start
#: raising `DeviceError` for pages that are still on disk, and a self-test
#: written to skip on a read failure would quietly turn PASS into SKIPPED.
#:
#: What is *not* redirected, and why it is keyed on the root's own name:
#:   * a subtree (`<dump>/slc`, `<dump>/raw`, `<dump>/g2`) -- those callers asked
#:     for one dumper's output, not for the media, and the subsetting is
#:     the point;
#:   * a single file;
#:   * a root already opened `in_place=True` (see `DumpDevice.__init__`);
#:   * anything opened `prefer_store=False`, which is how the tools that audit
#:     the tree itself keep getting the tree.
STORE_FOR_TREE = {}


def _env_prefer_store():
    return _config().prefer_store_enabled()


def discover_store_for_tree(root):
    """The store beside `root` that records itself as built from it.  -> Path/None

    This replaces a hardcoded one-entry map from a tree name to a store name,
    and it is not merely a
    generalisation of it -- it is the same test.  The old map was already only a
    hint: `canonical_root` accepted the redirect only after `source_tree_name`
    confirmed the candidate said it came from this tree, and rejected it
    otherwise.  Deriving the candidate from that same recorded field just removes
    the step where a human had to write the pair down.

    A store that does not name its `source_tree`, or names a different one, is
    not returned.  Two stores naming the same tree is not resolved here: the
    first by sorted name wins and `describe()` reports which backing was opened,
    because a silent choice between two claimants is the failure this whole
    check exists to prevent, and refusing to open at all would strand a dump for
    a reason the operator can fix by passing the store directly.
    """
    root = Path(root)
    try:
        siblings = sorted(p for p in root.parent.iterdir() if p.is_dir())
    except OSError:
        return None
    for cand in siblings:
        if cand.name == root.name:
            continue
        if source_tree_name(cand) == root.name:
            return cand
    return None


def source_tree_name(store_path):
    """The basename of the tree a store records itself as built from, or None.

    The basename and not the path.  `store.json` records
    `source_tree` as it was spelled by the build that wrote it, and a dump is
    routinely written from one OS and read from another -- a Windows build
    records a unc or drive path where the Linux reader sees a POSIX one. Those
    two strings name the same directory and no `Path.resolve()` can turn either
    into the other, so comparing whole paths silently answers "no, they are
    unrelated" on one of the two platforms.  MEASURED: that is exactly what made
    `titania.dumpdev --selftest` [9j] print SKIPPED under Linux while it ran
    under Windows.  The pairing that actually needs proving here is "this store
    beside this tree", and the directory it lives in already carries the rest.
    """
    _store = _store_module()
    try:
        if not _store.is_store(store_path):
            return None
        meta = json.loads(
            (Path(store_path) / _store.META_NAME).read_text("utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    src = meta.get("source_tree")
    if not src:
        return None
    return PurePosixPath(str(src).replace("\\", "/")).name


def canonical_root(root=None, prefer_store=True):
    """Which backing a whole-media request should actually open.  -> Path

    Maps a whole-tree root to the page store that supersedes it, and leaves
    everything else exactly as it was handed in.  The candidate comes from
    `STORE_FOR_TREE` if the operator named one and otherwise from
    `discover_store_for_tree`, and either way it is VERIFIED against the store's
    own metadata: it has to be a real store (`store.is_store`) and its recorded
    `source_tree` has to be this tree.  A store built from somewhere else, or a
    directory that merely has the right name, does not redirect -- so this can
    never quietly serve one dump's pages in another dump's name.

    It does not require the tree to still exist.  The store is the
    canonical index; a tree that has been reclaimed away entirely is not a
    reason to fail a read that the store can serve.
    """
    root = Path(root) if root is not None else default_root()
    if not prefer_store or not _env_prefer_store():
        return root
    name = STORE_FOR_TREE.get(root.name)
    if name is not None:
        cand = root.parent / name
        return cand if source_tree_name(cand) == root.name else root
    cand = discover_store_for_tree(root)
    return cand if cand is not None else root


# --------------------------------------------------------------------------
# The naming conventions that exist in this tree.
#
# Each entry is (name, compiled regex, field order, note).  The regex is matched
# against the file name; `dir_ce` layouts additionally take the CE from the
# parent directory.  Groups are named so the order of the fields in the file
# name does not have to be remembered.
# --------------------------------------------------------------------------
LAYOUTS = [
    # nd_dumpdeep.py -> <dump>/slc, <dump>/deep      (the main verified dump)
    ("deep", re.compile(r"^deep_ce(?P<ce>\d+)_b(?P<block>\d+)_pg(?P<page>\d+)\.bin$"),
     "verified page dump (nd_dumpdeep)"),
    # nd_dumpdeep quarantine: written when a page never verified.  Excluded by
    # default -- serving it as if it were good is exactly the mistake the
    # quarantine name exists to prevent.
    ("deep_bad", re.compile(r"^BAD_ce(?P<ce>\d+)_b(?P<block>\d+)_pg(?P<page>\d+)\.bin$"),
     "QUARANTINE: never passed MCRC/LDPC (nd_dumpdeep)"),
    ("deepraw", re.compile(r"^deepraw_ce(?P<ce>\d+)_b(?P<block>\d+)_pg(?P<page>\d+)\.bin$"),
     "uncorrected raw read"),
    # nd_g2dump.py -> <dump>/g2.  Group 2 lives in block 0 and the dumper
    # names the file by row; in SLC mode row == page, so page = row, block = 0.
    ("g2", re.compile(r"^g2_ce(?P<ce>\d+)_row(?P<page>\d+)\.bin$"),
     "group 2, block 0, page == row"),
    # pb_fold.py -> <dump>/no_user.  The mode is in the name, so it does not
    # have to be inferred from the directory -- the only layout here of which
    # that is true, and the reason this one is listed before "generic".
    ("mode_named",
     re.compile(r"^ce(?P<ce>\d+)_b(?P<block>\d+)_p(?P<page>\d+)"
                r"_(?P<mode>slc|tlc)\.bin$"),
     "ce/block/page with the CELL MODE recorded in the name, not inferred"),
    # nd_tlc.py -> <dump>/tlc ; nd_widesurvey/nd_blocksurvey -> wide, survey_blocks
    ("generic", re.compile(r"^ce(?P<ce>\d+)_b(?P<block>\d+)_p(?:g)?(?P<page>\d+)\.bin$"),
     "ce/block/page"),
    # nd_dumpfull.py -> <dump>/raw/ce01/b000_p000.bin   (CE from the directory)
    ("dir_ce", re.compile(r"^b(?P<block>\d+)_p(?P<page>\d+)\.bin$"),
     "ce from the parent directory name"),
    # attic/nd_dumpimage.py -> <dump>/img.  Excluded by default: the trailing
    # number is an index into the rows 0,2,4,6 that dumper believed held data,
    # not a page number, and that belief was measured on the wrong block.
    ("img", re.compile(r"^img_ce(?P<ce>\d+)_b(?P<block>\d+)_p(?P<idx>\d+)\.bin$"),
     "SUSPECT: trailing field is a row index, not a page (attic/nd_dumpimage)"),
    # <dump>/full_ce6_row0x0000_r0.bin -- repeated reads of one row
    ("full_rep", re.compile(r"^full_ce(?P<ce>\d+)_row0x(?P<page_hex>[0-9a-fA-F]+)"
                            r"_r(?P<rep>\d+)\.bin$"),
     "repeated reads of one row; r0 is used, the rest recorded as repeats"),
]

#: layouts that are indexed but not served unless the caller asks for them
SUSPECT_LAYOUTS = ("deep_bad", "img")


# --------------------------------------------------------------------------
# WHAT DECIDES A COLLISION, AND WHY IT IS NOT PROVENANCE
#
# Two files can claim the same (ce, block, page, mode).  Until 2026-08-17 that
# was settled by a per-directory RANK: a table of one operator's capture
# directories, each with a number, and the higher number won.  It is gone.
#
# It is gone because the codec answers the question directly.  A page either
# passes its region's integrity check -- the AU MCRCs over the complemented
# stored bytes, and an LDPC decode that converges -- or it does not, and that
# is a property of the READ rather than of the directory the file landed in.
# Measured on the reference store: of 265 pages claimed twice with different
# bytes, content settles 264.  The one it does not is CE0, a dead die whose
# bus constant is not a measurement of anything, so there is nothing to settle.
#
# The rank table looked load-bearing right up until it was measured.  It
# "decided" 108 of those 265 -- but every one of those 108 is a page where one
# candidate's LDPC decode converged and the other's did not, and the ladder in
# `_decide` was skipping that comparison because a non-convergence leaves the
# correction count None.  A measured failure was being read as an unmeasured
# one.  Fixing the ladder took the rank table's real contribution to zero.
#
# WHAT SURVIVES, AND WHY IT IS DIFFERENT IN KIND
#
# Tiers.  Not a ranking -- a two-state answer to "is this record a reading of
# the page it claims to be?", which no integrity check can give you.  Both MCRC
# and the EE3 parity are computed over the STORED bytes, so a page that was read
# perfectly and then filed under the wrong page number passes every content test
# there is.  That is what quarantine is for, and it is keyed on the file naming
# convention -- a property of the dumper's output format, recognisable anywhere
# -- rather than on anyone's directory names.
# --------------------------------------------------------------------------
UNKNOWN = "unknown"
SUSPECT = "suspect"

#: tiers that are not served unless `include_suspect=True`
EXCLUDED_TIERS = (SUSPECT,)

_TIERS = (UNKNOWN, SUSPECT)

#: A record's provenance: the tier and the sentence justifying it.  There is no
#: rank -- see the note above.
Provenance = namedtuple("Provenance", "tier reason")

DEFAULT_REASON = ("an ordinary capture: indexed and served, with no claim made "
                  "about its quality.  What the page is worth is decided by "
                  "decoding it, not by which directory it sits in")

#: Layouts whose ADDRESS is not to be trusted.  Keyed on a layout -- a file
#: naming convention this module can recognise in any tree -- because these are
#: statements about a dumper's output format, not about one tree's directories.
_LAYOUT_TIER = {"deep_bad": (SUSPECT,
                             "QUARANTINE: nd_dumpdeep wrote this because the "
                             "page NEVER verified.  Serving it as if it were "
                             "good is the exact mistake the quarantine name "
                             "exists to prevent."),
                "img": (SUSPECT,
                        "the trailing number in this dumper's file names is an "
                        "index into the rows it believed held data, NOT a page "
                        "number.  The BYTES may be fine; the ADDRESS is wrong, "
                        "which is worse, because a mislabelled page is "
                        "indistinguishable from a real one -- and no integrity "
                        "check can catch it, because MCRC and the EE3 parity "
                        "both cover the stored bytes and neither covers the "
                        "address.")}


#: Source names that record a skip rather than a capture.
#:
#: A skip source names its own evidence after the prefix -- "skip:ftl-weru-free",
#: "skip:operator-range" -- because the reason is per-source: one shared name
#: would collapse every skip in a store onto whichever justification was
#: registered first.
SKIP_PREFIX = "skip:"

#: Tier `UNKNOWN` and not `SUSPECT`: suspect tiers are excluded from the default
#: read path, and an excluded skip record reads back as plain absence, which
#: throws away the one thing it exists to say.  A skip is not a doubtful
#: capture; it is a reliable statement that no capture happened.  `read_page`
#: raises on it rather than serving bytes, so it can never be mistaken for data.
SKIP_PROVENANCE = Provenance(
    UNKNOWN,
    "no read was performed for this page -- the capture skipped it on the "
    "evidence named in this source, and the skip is recorded so that a "
    "deliberate gap cannot be mistaken for an unfinished dump")


#: Source names of the GbE read-retry ladder: `retry-level-0` .. `retry-level-32`.
#:
#: A prefix rule rather than 33 entries, for the same reason `skip:` is one: the
#: level is part of the name because the level is the provenance, so the set of
#: names is open-ended by construction and enumerating it would only be a list
#: to forget to extend.
RETRY_PREFIX = "retry-level-"
RETRY_PROVENANCE = Provenance(
    UNKNOWN,
    "rescued by the GbE read-retry ladder at the level named in this source: "
    "the die was latched native multi-level (0xDF) and its read references "
    "shifted by SET FEATURES on 0x89/0x8A -- verified by 0xEE readback when the "
    "level was applied -- and the page then read back MCRC 4/4.  The level "
    "named is the first that DECODED, not necessarily the lowest that would "
    "have: the sweep tries 1, 0, 2, 3, ... and stops there.")

# --------------------------------------------------------------------------
# CE0 is DEAD and answers.
#
# It returns a bus constant built from exactly two byte values and containing no
# 0xFF at all: canonically 2304 x 0xA7 followed by 16128 x 0x00.  Different
# dumpers catch it at different phases, so the order varies while the alphabet
# does not -- <dump>/g2 has 2880 x 0xA7 and survey_blocks has the canonical
# 2304, in scattered positions, and neither is the canonical page.
#
# Such a page is neither programmed nor erased, and it must be allowed to look
# like neither.  It is legitimately present in the dumps, so it is indexed and
# served; `page_class()` names it, and a conflict between two of them is recorded
# UNRESOLVED rather than being handed to whichever directory ranks higher --
# there is nothing to win.
# --------------------------------------------------------------------------
DEAD_CE_ALPHABET = frozenset((0xA7, 0x00))
DEAD_CE_PAGE = b"\xA7" * 2304 + b"\x00" * 16128

PROGRAMMED = "programmed"
ERASED = "erased"
DEAD_CE = "dead-ce"


def page_class(raw):
    """What kind of page these bytes are.  -> PROGRAMMED | ERASED | DEAD_CE

    The three are not interchangeable and the difference has been lost before:
    an erased page is real media reading all-0xFF, a dead-CE page is a bus
    constant off a die that does not answer, and only PROGRAMMED is data.
    """
    if not raw:
        return ERASED
    if raw.count(0xFF) == len(raw):
        return ERASED
    if set(raw) <= DEAD_CE_ALPHABET:
        return DEAD_CE
    return PROGRAMMED


#: cell mode by directory name.  Everything in this tree was dumped in
#: pseudo-SLC (the dumpers all call `nd_dumpdeep.enter_slc`) except <dump>/tlc,
#: which is the only one produced through `nd_tlc.exit_slc`.  A file name that
#: states the mode outright wins over this -- see `_mode_for`.
_MODE_BY_DIR = {"tlc": False}          # False == native TLC, True == pseudo-SLC

_DIR_CE = re.compile(r"^ce(\d+)$")


def _layout_of(path):
    """Which LAYOUTS entry matches this file name, or None.

    A `store.PageRef` carries its layout as a recorded field rather than as
    something to be re-derived from a name, so it is believed outright: the
    store was built from the tree's own verdict and re-parsing a synthetic file
    name would only be a chance to disagree with it.
    """
    lay = getattr(path, "layout", None)
    if lay is not None:
        return lay
    for layout, rx, _note in LAYOUTS:
        if rx.match(path.name):
            return layout
    return None


def _wrap(text, width):
    """Minimal greedy wrap -- `textwrap` for one purpose is not worth an import."""
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = (line + " " + word) if line else word
    if line:
        out.append(line)
    return out


def scan_bytes(data):
    """The page reduction, in pure Python.  -> PageScan

    This is the reference for what `tdev_scan.c` computes on the EFC: the same
    four quantities over exactly the same bytes.  Keeping it here, in the
    hardware-free module, is what makes the device-side version checkable --
    `hwdev`'s selftest runs both over the same page and requires all four fields
    to agree, so a payload bug shows up as a mismatch instead of as a quietly
    wrong verdict.

    `crc` is CRC-32 (reflected, poly 0xEDB88320) -- the same one `zlib.crc32`
    computes, which is asserted in the selftest rather than assumed.
    """
    import zlib
    nonff = len(data) - data.count(0xFF)
    first = None
    if nonff:
        for i, b in enumerate(data):
            if b != 0xFF:
                first = i
                break
    return PageScan(all_ones=(nonff == 0), nonff_bytes=nonff,
                    first_nonff=first, crc=zlib.crc32(data) & 0xFFFFFFFF)


#: One resolved disagreement.  `basis` says what decided it and `reason` says
#: why, in words; `evidence` carries the numbers the basis was read off.
Adjudication = namedtuple("Adjudication",
                          "key winner loser basis reason evidence")

#: bases, strongest first.  Every one of them is a measurement of the read.
BY_MCRC = "mcrc"          # one passes MCRC on more AUs, uncorrected or corrected
BY_ECC = "ecc"            # one converges and the other does not, or needs fewer
                          # LDPC corrections
UNRESOLVED = "unresolved"  # nothing can decide, and that is the finding


class DumpEntry(object):
    """One raw page in the dump."""

    __slots__ = ("path", "layout", "ce", "block", "page", "slc", "repeats",
                 "tier", "why")

    def __init__(self, path, layout, ce, block, page, slc,
                 tier=UNKNOWN, why=DEFAULT_REASON):
        self.path = path
        self.layout = layout
        self.ce = ce
        self.block = block
        self.page = page
        self.slc = slc
        self.tier = tier
        self.why = why
        self.repeats = []

    def key(self):
        return (self.ce, self.block, self.page, self.slc)

    def __repr__(self):
        return "<DumpEntry ce%d b%d p%d %s %s %s>" % (
            self.ce, self.block, self.page,
            "SLC" if self.slc else "TLC", self.path.name, self.tier)


def _store_module():
    """`titania.store`, importable under both package and direct execution."""
    try:
        from . import store as _store
    except (ImportError, ValueError):                       # direct execution
        import store as _store                              # noqa: F401
    return _store


class _StoreEntries(object):
    """`DumpDevice.entries` over a page store -- a Mapping, never a dict.

    A dict is the thing that does not scale.  52 734 `DumpEntry` objects in a
    dict is ~15 MB and nobody notices; 54 million is ~13 GB of heap before a
    single page has been read, which would make the index itself the bottleneck
    the blob store exists to remove.  So keys come off the mmapped index by
    binary search and a `DumpEntry` is built only for the key that was asked
    for.

    `adjudicate(apply=True)` mutates the entry it decided -- repoints it at the
    content winner -- and a freshly-built entry would forget that.  Those, and
    only those, are retained in `_repointed`, which is bounded by the number of
    contested pages rather than by the size of the store.
    """

    def __init__(self, dev):
        self._dev = dev
        self._store = dev._store

    def _build(self, key, ref, reps):
        tier, why = ref.provenance
        ent = DumpEntry(ref, ref.layout, key[0], key[1], key[2], key[3],
                        tier, why)
        ent.repeats = list(reps)
        return ent

    def __getitem__(self, key):
        key = (key[0], key[1], key[2], bool(key[3]))
        hit = self._dev._repointed.get(key)
        if hit is not None:
            return hit
        refs = self._store.candidates(key)
        if not refs:
            raise KeyError(key)
        return self._build(key, refs[0], refs[1:])

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        key = (key[0], key[1], key[2], bool(key[3]))
        return key in self._dev._repointed or self._store.has(key)

    def __len__(self):
        return self._store.stat("pages", 0)

    def __iter__(self):
        return self._store.iter_keys()

    def keys(self):
        return list(self)

    def items(self):
        rep = self._dev._repointed
        for key, ref, reps in self._store.iter_served():
            hit = rep.get(key)
            yield (key, hit if hit is not None else self._build(key, ref, reps))

    def values(self):
        for _k, v in self.items():
            yield v

    def __repr__(self):
        return "<_StoreEntries %d pages (lazy, mmapped)>" % len(self)


class DumpDevice(Device):
    """A `Device` over raw NAND pages -- from a file tree OR a page store.

    TWO BACKINGS, ONE INTERFACE
    ---------------------------
    * a directory tree of one file per page, discovered by `LAYOUTS`; and
    * a blob-plus-index store (`titania.store`), detected by the presence of
      `index.tix`, where the bytes live in append-only blobs and everything else
      lives in one sorted fixed-record index.

    They coexist on purpose.  A store is built from a tree by `pc_migrate.py`,
    which never touches the tree, and both are readable at once so the two can
    be compared page-for-page instead of being trusted.  Everything below this
    line -- the trust ranking, content adjudication, explicit erased pages, and
    a missing page raising rather than reading 0xFF -- is backing-independent
    and behaves identically over either.

    ASKING FOR THE WHOLE TREE GETS YOU THE STORE
    --------------------------------------------
    `DumpDevice()` opens `the store`.  That is not a shortcut: the tree
    is a *dump directory* that `pz_reclaim.py` prunes as pages are proven, byte
    for byte, to be inside the store, so the tree is a shrinking subset while
    the store stays whole.  Roughly two hundred call sites in this tree open the
    whole media that way, and the alternative to one redirect here is two
    hundred literals that drift apart.  The mapping is `STORE_FOR_TREE`, it is
    verified against the store's own recorded `source_tree`, and `self.root`
    always says which backing you actually got (`requested_root` says what you
    asked for, `redirected` says whether they differ).  Subtrees, single files,
    `in_place=True` and `prefer_store=False` are never redirected.

    Parameters
    ----------
    root : path
        a store directory, a tree directory (walked recursively), or a single
        file.
    prefer_store : bool
        Let a whole-tree root resolve to the store that supersedes it (see
        above).  On by default.  Pass False when the files are the subject --
        `pc_migrate.py --verify`, the [9j] cross-backing check -- rather than
        the pages they hold.
    include_suspect : bool
        Also serve the layouts listed in `SUSPECT_LAYOUTS` -- quarantined pages
        and the img/ dump whose page numbering is known to be wrong.  Off by
        default; when on, `entry.layout` still says which is which.
    in_place : bool
        Allow `flush()` and writes to modify the original files.  Off by
        default; a write goes to the in-memory overlay instead.
    page_bytes : int
        Size a file must have to be accepted as a raw page.  18432 here; files
        of other sizes are rejected with a reason rather than truncated, since
        several directories in this tree hold decoded (16384 B) or partial
        (2304 / 4096 B) data that is not a page.
    adjudicate : bool
        Run `adjudicate()` at construction, so every collision is decided on
        content before the first read.  Off by default only because it reads
        both sides of every collision -- ~4000 file opens and ~9 s on a
        network or virtualised filesystem for the dump -- which would be paid by callers that
        never touch a contested page.  It is exact either way; this flag only
        moves when it is paid.
    """

    writable = True

    def __init__(self, root=None, include_suspect=False, in_place=False,
                 page_bytes=PAGE_BYTES, follow_symlinks=False,
                 adjudicate=False, prefer_store=True):
        Device.__init__(self)
        self.requested_root = Path(root) if root is not None else default_root()
        # `in_place` means "modify the files I named".  A redirect would send
        # those writes into the store's blobs instead, which is a different
        # object with a different meaning, so the two never combine: naming a
        # tree and asking to write it in place gets the tree.
        self.root = canonical_root(self.requested_root,
                                   prefer_store and not in_place)
        self.redirected = self.root != self.requested_root
        self.include_suspect = bool(include_suspect)
        self.in_place = bool(in_place)
        self.page_bytes = int(page_bytes)
        self.follow_symlinks = follow_symlinks

        self.entries = {}          # key -> DumpEntry  (a Mapping over a store)
        self.rejected = []         # (path, reason)
        self.by_layout = {}        # layout name -> count
        self.ties = set()          # keys whose collision rank did not separate
        self._overlay = {}         # key -> bytes   (copy-on-write)
        self._erased = set()       # (ce, block, slc) marked erased in the overlay
        self._adjudged = None      # cached [Adjudication]; see adjudicate()
        self._store = None         # store.PageStore, when this is a store
        self._repointed = {}       # key -> DumpEntry, adjudication survivors
        self._ces = None           # cached present_ces for a store

        if not self.root.exists():
            raise DeviceError("no such dump path: %s" % self.root)
        if self._looks_like_store():
            self._open_store()
        else:
            self._scan()
        if adjudicate:
            self.adjudicate()

    # -- backing selection ------------------------------------------------
    def _looks_like_store(self):
        try:
            return _store_module().is_store(self.root)
        except Exception:                                   # noqa: BLE001
            return False

    def _open_store(self):
        """Open the blob-plus-index backing.  No blob is touched here.

        The index is mmapped and the statistics come out of `store.json`, so
        opening a store is O(1) in the number of pages -- which is the whole
        difference between this and walking 56 230 directory entries.
        """
        _store = _store_module()
        excluded = () if self.include_suspect else EXCLUDED_TIERS
        try:
            self._store = _store.PageStore(self.root, excluded_tiers=excluded)
        except _store.StoreError as exc:
            raise DeviceError("cannot open the page store at %s: %s"
                              % (self.root, exc))
        if self._store.page_bytes != self.page_bytes:
            raise DeviceError(
                "%s holds %d-byte pages; this DumpDevice was constructed for "
                "%d.  Nothing is truncated to make them agree."
                % (self.root, self._store.page_bytes, self.page_bytes))
        # There used to be a drift check here: the store recorded a rank per
        # source, ranks were part of the index sort key, and a rank that had
        # since changed in the live table silently reordered which candidate was
        # served.  Both halves of that are gone -- no ranks, and nothing about
        # provenance in the sort key -- so there is no copy left to go stale.
        self.entries = _StoreEntries(self)
        self.rejected = list(self._store.rejected)
        self.by_layout = dict(self._store.stat("by_layout", {}) or {})
        self._ces = list(self._store.stat("ces", []) or [])
        # Every key with more than one candidate is a tie until `adjudicate()`
        # decodes it, on a store exactly as on a tree.  The set is carried in the
        # store's own statistics so the "the incumbent is arbitrary" warning
        # survives a change of backing.
        self.ties = set(
            tuple(k[:3]) + (bool(k[3]),)
            for k in (self._store.stat("tie_keys", []) or []))

    # -- discovery --------------------------------------------------------
    def _scan(self):
        """Walk the tree.

        `os.scandir` rather than `Path.rglob` + `Path.stat`, because the dumps
        usually live on a network or virtualised filesystem where an individual `stat()` costs
        ~2.4 ms: 45 000 files x 2 stats is four minutes, versus half a second
        when the size comes from the directory entry the enumeration already
        returned.  Measured, not guessed.
        """
        if self.root.is_file():
            st = self.root.stat()
            self._place(self.root, st.st_size)
            return
        stack = [self.root]
        found = []
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=self.follow_symlinks):
                                stack.append(Path(e.path))
                                continue
                            if not e.is_file(
                                    follow_symlinks=self.follow_symlinks):
                                continue
                        except OSError:
                            continue
                        if not e.name.endswith(".bin"):
                            continue
                        try:
                            size = e.stat(
                                follow_symlinks=self.follow_symlinks).st_size
                        except OSError:
                            size = -1
                        found.append((e.path, size))
            except OSError as exc:
                self.rejected.append((d, "scandir failed: %s" % exc))
        for path, size in sorted(found):
            self._place(Path(path), size)

    def _place(self, path, size):
        name = path.name
        parent = path.parent.name
        for layout, rx, _note in LAYOUTS:
            m = rx.match(name)
            if not m:
                continue
            # Quarantine is keyed on the LAYOUT, never on the directory.  A
            # directory-name blocklist came from the trust table and went with
            # it: it was one tree's names, and a name is not evidence.  A layout
            # is a naming convention this module recognises in any tree, and
            # what it says -- "the number in this file name is not a page
            # number" -- is a fact about the dumper that wrote it.
            if not self.include_suspect and layout in SUSPECT_LAYOUTS:
                tier, why = _LAYOUT_TIER[layout]
                self.rejected.append(
                    (path, "%s layout %r: %s (pass include_suspect=True)"
                     % (tier, layout, why)))
                return
            g = m.groupdict()
            if layout == "dir_ce":
                dm = _DIR_CE.match(parent)
                if not dm:
                    self.rejected.append(
                        (path, "b*_p*.bin outside a ce<N>/ directory"))
                    return
                ce = int(dm.group(1))
            else:
                ce = int(g["ce"])
            block = int(g["block"]) if g.get("block") is not None else 0
            if g.get("page_hex") is not None:
                page = int(g["page_hex"], 16)
            elif g.get("page") is not None:
                page = int(g["page"])
            else:                                   # img: idx, not a page
                page = int(g["idx"])
            if size != self.page_bytes:
                self.rejected.append(
                    (path, "%d bytes, not a %d-byte raw page" %
                     (size, self.page_bytes)))
                return
            slc = self._mode_for(path, m.groupdict())
            tier, why = self._tier_for(path, layout)
            ent = DumpEntry(path, layout, ce, block, page, slc, tier, why)
            key = ent.key()
            old = self.entries.get(key)
            if old is not None:
                # A second file for the same page: another dumper's copy, or a
                # repeated read (`full_*_r1`).  One is served and the other is
                # kept in `repeats` -- `repeats()` is how a caller compares
                # independent reads, which is the only sound staleness test.
                # (Identical repeats prove nothing either way: constant stored
                # data reads identically every time.)
                #
                # WHICH ONE IS SERVED IS NOT DECIDED HERE, and reading no bytes
                # is the point: 52 000 files cannot each be decoded during a
                # scan.  So the incumbent stays, the key goes in `self.ties`, and
                # `adjudicate()` settles it on content when someone asks.  The
                # scan used to pick by directory rank, which looked like a
                # decision and was not one.
                self.ties.add(key)
                old.repeats.append(path)
                return
            self.entries[key] = ent
            self.by_layout[layout] = self.by_layout.get(layout, 0) + 1
            return
        # not a page file at all -- only worth reporting for .bin
        if path.suffix == ".bin":
            self.rejected.append((path, "no layout matches the file name"))

    def _tier_for(self, path, layout):
        """-> (tier, reason) for a file.

        Two states, not an order: SUSPECT means the ADDRESS this file claims is
        not to be believed, which is the one defect no amount of decoding can
        find.  Everything else is UNKNOWN, which is not a demerit -- it says
        this record makes no claim about its own quality, and what the page is
        worth comes from decoding it.
        """
        rec = getattr(path, "provenance", None)
        if rec is not None:
            # A store records the tier and the reason with the page.  Re-deriving
            # them from a synthetic path would lose the reason text, which is
            # the evidence.
            return rec
        if layout in SUSPECT_LAYOUTS:
            return _LAYOUT_TIER[layout]
        return UNKNOWN, DEFAULT_REASON

    def _mode_for(self, path, groups=None):
        """-> True for pseudo-SLC, False for native TLC.

        A file name that states the mode is believed first: it was recorded by
        the dumper that knew, rather than reconstructed from where the file
        happened to land.  Otherwise it comes from the directory, which is the
        only other place the mode was ever written down.  Unknown directories
        default to SLC: every dumper in this tree except nd_tlc.py calls
        `enter_slc()`.
        """
        if groups and groups.get("mode"):
            return groups["mode"] == "slc"
        for part in reversed(path.parts[:-1]):
            if part in _MODE_BY_DIR:
                return _MODE_BY_DIR[part]
        return True

    # -- Device interface -------------------------------------------------
    def present_ces(self):
        if self._ces is not None:
            return sorted(set(self._ces) | {k[0] for k in self._overlay})
        return sorted({e.ce for e in self.entries.values()}
                      | {k[0] for k in self._overlay})

    def _ref_for(self, key):
        """The served page reference for a key, or None.

        The read path, and the reason it is separate from `entries.get()`: over
        a store this is one binary search that stops at the first surviving
        candidate, instead of materialising the repeats a reader never asked
        for.  An adjudicated entry still wins, because a decision made on
        content must outlive the object it was made on.
        """
        hit = self._repointed.get(key)
        if hit is not None:
            return hit.path
        if self._store is not None:
            return self._store.served(key)
        ent = self.entries.get(key)
        return ent.path if ent is not None else None

    def read_page(self, ce, block, page, lun=0, slc=False):
        if lun:
            raise DeviceError(
                "this dump records no LUN; every file in it is lun 0 "
                "(asked for lun %d)" % lun)
        key = (ce, block, page, bool(slc))
        if key in self._overlay:
            return self._overlay[key]
        ref = self._ref_for(key)
        if ref is None:
            raise DeviceError(self._absent_message(ce, block, page, slc))
        # Before the block-level erased shortcut, not after. A
        # skipped page has no bytes and no claim about the media, so serving it
        # 0xFF from a block marked erased would turn "we chose not to look" into
        # "we looked and it was blank" -- silently, and for whole blocks at a
        # time.
        if getattr(ref, "is_skipped", lambda: False)():
            tier, why = ref.provenance
            raise DeviceError(
                "ce=%d block=%d page=%d (%s) was NOT READ; the capture skipped "
                "it and recorded why. It is neither erased nor missing. "
                "Evidence: %s (%s)"
                % (ce, block, page, "SLC" if slc else "TLC", why, tier))
        if (ce, block, bool(slc)) in self._erased:
            return b"\xff" * self.page_bytes
        try:
            data = ref.read_bytes()
        except Exception as exc:                            # noqa: BLE001
            if isinstance(exc, DeviceError):
                raise
            raise DeviceError("%s could not be read: %s" % (ref, exc))
        if len(data) != self.page_bytes:
            raise DeviceError("%s shrank to %d bytes since the scan"
                              % (ref, len(data)))
        return data

    def page_ref(self, ce, block, page, lun=0, slc=False):
        """The reference that would serve this page, unread.  -> ref or None

        For bulk work: a store's `PageRef` answers `is_erased()` and `length`
        without touching the blob, so a copy pass can move an erased page as one
        index record instead of materialising 18432 bytes of 0xFF only to count
        them again at the far end.  Over a file tree it is the `Path`.

        Raises rather than lying about an overlaid page.  An overlay write has
        no reference behind it -- it exists only in memory -- so returning None
        would read as "absent" and a bulk copy would silently drop exactly the
        pages someone had just written.
        """
        if lun:
            raise DeviceError(
                "this dump records no LUN; every file in it is lun 0 "
                "(asked for lun %d)" % lun)
        key = (ce, block, page, bool(slc))
        if key in self._overlay:
            raise DeviceError(
                "ce=%d block=%d page=%d (%s) is an overlay write with no "
                "backing reference; read it with read_page()"
                % (ce, block, page, "SLC" if slc else "TLC"))
        return self._ref_for(key)

    def _absent_message(self, ce, block, page, slc):
        mode = "SLC" if slc else "TLC"
        other = self._ref_for((ce, block, page, not bool(slc)))
        if self._store is not None:
            have = self._store.count_in_block(ce, block, bool(slc))
        else:
            have = sum(1 for k in self.entries
                       if k[0] == ce and k[1] == block and k[3] == bool(slc))
        msg = ("no %s page in the dump for ce=%d block=%d page=%d "
               "(the dump holds %d %s page(s) for that block, under %s)"
               % (mode, ce, block, page, have, mode, self.root))
        if other is not None:
            msg += ("; it IS present as %s (%s) -- pass slc=%s if that is what "
                    "you meant" % ("SLC" if not slc else "TLC",
                                   other.name, not slc))
        return msg

    # -- acceleration -----------------------------------------------------
    # a dump already has every byte, so there is nothing to accelerate and,
    # more importantly, nothing to infer.  These overrides exist so that a
    # caller written against the fast interface gets exact answers here -- which
    # is what makes DumpDevice the right place to validate a sampling rule that
    # HardwareDevice has to trust.

    def read_page_ex(self, ce, block, page, lun=0, slc=False):
        """Always `FULL`: every byte came out of a file, nothing was guessed."""
        return PageRead(self.read_page(ce, block, page, lun=lun, slc=slc), FULL)

    def scan_page(self, ce, block, page, lun=0, slc=False):
        """Exact, over every byte -- they are all right here.  -> PageScan

        Raises `DeviceError` for a page the dump does not have, rather than
        returning None: "I have no file for this" is not the same claim as
        "I have no reduction primitive", and only one of them is true here.
        """
        return scan_bytes(self.read_page(ce, block, page, lun=lun, slc=slc))

    # -- write path (overlay by default) ----------------------------------
    def write_page(self, ce, block, page, data, lun=0, slc=False):
        self._check_write()
        if lun:
            raise DeviceError("this dump records no LUN (asked for lun %d)" % lun)
        data = bytes(data)
        if len(data) != self.page_bytes:
            raise DeviceError("a page is %d bytes, got %d"
                              % (self.page_bytes, len(data)))
        key = (ce, block, page, bool(slc))
        self._overlay[key] = data
        self._erased.discard((ce, block, bool(slc)))
        if self.in_place:
            ref = self._ref_for(key)
            if ref is None:
                raise DeviceError(
                    "in_place write to a page the dump does not have a file for "
                    "(ce=%d block=%d page=%d); flush(dir) to a new directory "
                    "instead" % (ce, block, page))
            try:
                ref.write_bytes(data)
            except Exception as exc:                        # noqa: BLE001
                if isinstance(exc, DeviceError):
                    raise
                raise DeviceError("in_place write to %s refused: %s"
                                  % (ref, exc))
        return len(data)

    def erase_block(self, ce, block, lun=0):
        """Mark every page this dump has for the block as erased (0xFF).

        Does not conjure pages the dump never had: after an erase,
        a read of an unknown page still raises `DeviceError`.  A real erase does
        of course affect the whole block, but a dump cannot represent that
        without inventing content, and inventing 0xFF is the one thing this
        class refuses to do.
        """
        self._check_write()
        if lun:
            raise DeviceError("this dump records no LUN (asked for lun %d)" % lun)
        n = 0
        have = (self._store.keys_in_block(ce, block)
                if self._store is not None else None)
        for slc in (True, False):
            if have is not None:
                keys = [k for k in have if k[3] == slc]
            else:
                keys = [k for k in self.entries if k[0] == ce and k[1] == block
                        and k[3] == slc]
            keys += [k for k in self._overlay if k[0] == ce and k[1] == block
                     and k[3] == slc]
            if not keys:
                continue
            self._erased.add((ce, block, slc))
            for k in set(keys):
                self._overlay[k] = b"\xff" * self.page_bytes
                n += 1
        if n == 0:
            raise DeviceError("the dump holds no pages for ce=%d block=%d, so "
                              "there is nothing to erase" % (ce, block))
        return n

    def flush(self, out_dir=None, layout="deep"):
        """Materialise the overlay.  -> list of paths written.

        With `out_dir` it writes a fresh directory and never touches the
        originals.  Without one it requires `in_place=True` and rewrites the
        original files, which is why that is not the default.
        """
        if out_dir is None:
            if not self.in_place:
                raise WriteProtected(
                    "flush() without out_dir would rewrite the original dump; "
                    "construct with in_place=True if you really mean it")
            written = []
            for key, data in self._overlay.items():
                ref = self._ref_for(key)
                if ref is None:
                    raise DeviceError("no original file for %r" % (key,))
                ref.write_bytes(data)
                written.append(ref)
            return written
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for (ce, block, page, slc), data in sorted(self._overlay.items()):
            sub = out if slc else (out / "tlc")
            sub.mkdir(parents=True, exist_ok=True)
            p = sub / ("deep_ce%d_b%02d_pg%03d.bin" % (ce, block, page)
                       if slc else "ce%02d_b%04d_p%04d.bin" % (ce, block, page))
            p.write_bytes(data)
            written.append(p)
        return written

    # -- lifecycle --------------------------------------------------------
    def close(self):
        """Release the backing.  Harmless on a tree, required before a rebuild.

        A store's index is mmapped and its blobs are held open, and Windows will
        not let a mapped file be replaced -- so `pc_migrate.py` rebuilding a
        store that some other object still has open fails with a bare
        `PermissionError` unless the reader lets go first.  `DumpDevice` is also
        a context manager, which is the tidier way to say the same thing.
        """
        if self._store is not None:
            self._store.close()
            self._store = None
        self.entries = {}
        self._repointed = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- extras -----------------------------------------------------------
    def entry(self, ce, block, page, slc=True):
        return self.entries.get((ce, block, page, bool(slc)))

    def repeats(self, ce, block, page, slc=True):
        """Other files holding the same page -- independent reads, if any.

        Comparing them is the sound way to test read stability.  Note that
        identical repeats are not evidence of a stale buffer: constant stored
        data reads identically every time.  Differing repeats prove instability;
        identical ones prove nothing either way.
        """
        ent = self.entry(ce, block, page, slc)
        return list(ent.repeats) if ent else []

    def _contested(self):
        """(key, entry) for every page a second source claims.  In key order.

        The scaling fix for `conflicts()` and `adjudicate()`.  Over a tree this
        is a walk of the whole index because that is where the repeat lists
        live; over a store the contested keys were counted at build time, so
        this visits only the pages that are actually contested and never touches
        the ~99% that are not.  Both produce the same keys in the same order.
        """
        if self._store is None:
            for key, ent in sorted(self.entries.items()):
                if ent.repeats:
                    yield key, ent
            return
        for key in self._store.contested_keys():
            ent = self.entries.get(key)
            if ent is not None and ent.repeats:
                yield key, ent

    def conflicts(self, limit=200):
        """Pages two dumpers disagree about.  -> list of (key, served, other)

        The legacy shape, kept because callers (`pb_conflicts.py`,
        `pb_conflict_detail.py`, `pc_bench.py`) read it.  `adjudicate()` is the
        one that says who won and why; this only says a disagreement exists.

        Only differing repeats are reported.  Identical repeats are omitted on
        purpose and prove nothing: constant stored data reads identically every
        time, so "the two reads matched" is not evidence of a good read, and a
        blunt "repeated => stale" rule is equally wrong in the other direction.
        A difference, though, is real evidence that one of the two is wrong.

        Goes through `_contested()`, and that is the whole point of the method.
        A second definition of `conflicts` -- the pre-store one, which sorted
        `self.entries.items()` -- used to sit below `conflict_report()` and
        silently shadowed this one, so the scaling fix was dead code.  Measured
        on `the store` as it is served by default: 56 810 `DumpEntry` objects
        built and sorted (0.34 s) to reach the 2274 contested keys
        `_contested()` hands over in 0.04 s -- and at the size this store was
        built for, materialising the index is the 13 GB of heap `_StoreEntries`
        exists to avoid.  It also made `pc_bench.py` time `conflicts()` against
        an `adjudicate()` that was already on the fast path, so the two rows it
        prints side by side were not measuring the same algorithm.
        `tests/test_latent_guards.py` pins the routing so the two cannot drift
        apart again.
        """
        out = []
        for key, ent in self._contested():
            try:
                mine = ent.path.read_bytes()
            except OSError:
                continue
            for other in ent.repeats:
                try:
                    theirs = other.read_bytes()
                except OSError:
                    continue
                if theirs != mine:
                    out.append((key, ent.path, other))
                    if len(out) >= limit:
                        return out
        return out

    # -- adjudication: deciding a collision on EVIDENCE ---------------------
    def _page_evidence(self, raw, block, page):
        """What the CONTENT of one candidate says about itself.  -> dict

        Two independent measurements, in the order they are worth trusting:

        `mcrc`   how many of the page's AUs pass MCRC with no correction
                 applied.  MCRC runs over the complemented stored bytes, so it
                 needs no seed, no key and no ECC -- the cheapest honest "did I
                 read this page correctly" test that exists, and a page that
                 passes it 4/4 was read cleanly off the die.
        `ecc`    how many symbols the LDPC decoder had to change to reach a
                 valid codeword, or None if it did not converge (a count from a
                 decode that never converged means nothing and is not reported
                 as a small number).  This sees the whole codeword including the
                 PARITY region, which MCRC does not cover -- and a flipped parity
                 byte is exactly the case where both candidates pass MCRC 4/4 and
                 MCRC therefore cannot decide.
        `mcrc_c` MCRC after that correction.  The native-tlc pages need this: a
                 TLC page arrives with 25-240 syndromes per codeword and cannot
                 pass MCRC uncorrected at all, so `mcrc` is 0/4 on a perfectly
                 good one and says nothing.  It costs no extra LDPC work -- it is
                 read off the bytes `ecc_correct_page` already returned.

        Everything is wrapped: the codec is an optional layer, and a dump must
        stay readable on a checkout that has no LDPC stack.  A missing measure
        comes back as None and the caller falls through to the next basis rather
        than treating "could not measure" as "measured zero".
        """
        ev = {"class": page_class(raw), "mcrc": None, "aus": None, "ecc": None,
              "mcrc_c": None, "conv": None, "fixed": None}
        try:
            from . import codec as _codec
            from . import geometry as _geom
        except Exception:                                   # noqa: BLE001
            try:
                import codec as _codec                      # direct execution
                import geometry as _geom
            except Exception:                               # noqa: BLE001
                return ev
        if ev["class"] is not PROGRAMMED:
            # Neither an erased page nor a dead-CE bus constant is data, and
            # both decode to nonsense that would be reported as "MCRC 0/4" --
            # a verdict that says nothing about the read.  Do not produce it.
            return ev
        row = _geom.compose_row(block, page, 0)

        def _mcrc(buf):
            res = _codec.decode_page(buf, row=row, fmt=_codec.FORMAT_A,
                                     correct=False)
            ok = [bool(a.mcrc_ok) for a in res.aus]
            return sum(ok), len(ok)

        try:
            ev["mcrc"], ev["aus"] = _mcrc(raw)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            if _codec.ecc_available():
                fixed, ncorr, conv = _codec.ecc_correct_page(raw)
                ev["conv"] = bool(conv)
                if conv:
                    ev["ecc"] = ncorr
                    # The CORRECTED bytes, digested.  Two reads of one address
                    # can differ in raw bits and still be the same page -- that
                    # is what error correction is for -- and telling that apart
                    # from two genuinely different pages under one address is
                    # the difference between "nothing to choose here" and "your
                    # capture has an addressing fault".  Neither is visible in
                    # the counts, so the digest is carried.
                    ev["fixed"] = hashlib.sha1(bytes(fixed)).hexdigest()[:12]
                    try:
                        ev["mcrc_c"] = _mcrc(fixed)[0]
                    except Exception:                       # noqa: BLE001
                        pass
        except Exception:                                   # noqa: BLE001
            pass
        return ev

    @staticmethod
    def _decodes(ev):
        """Does this candidate yield usable plaintext at all?  -> bool or None"""
        if ev["aus"] is None:
            return None
        if ev["mcrc"] == ev["aus"]:
            return True
        if ev["mcrc_c"] is None:
            return None if ev["ecc"] is None else False
        return ev["mcrc_c"] == ev["aus"]

    def _decide(self, key, a_ent, a_raw, b_path, b_raw):
        """Weigh two candidates for one key.  -> Adjudication

        Content only, strongest evidence first.  Nothing about where either file
        came from enters, and where the content is equal the answer is that it
        is equal.
        """
        ce, block, page, _slc = key
        ea = self._page_evidence(a_raw, block, page)
        eb = self._page_evidence(b_raw, block, page)
        ev = {"A": ea, "B": eb}

        def nm(p):
            # the relative path, not the basename: pb_fold.py gives
            # the same page the same file name in three different capture
            # directories, so a basename does not identify a candidate.
            try:
                return str(p.relative_to(self.root))
            except ValueError:
                return str(p)

        # 0. neither candidate is media.  CE0 is dead and answers, so two
        #    dumpers can "disagree" about a page that was never data.  There is
        #    nothing to win here and pretending otherwise would dress a bus
        #    artefact up as a measurement.
        if ea["class"] is DEAD_CE and eb["class"] is DEAD_CE:
            return Adjudication(
                key, None, None, UNRESOLVED,
                "UNRESOLVED -- both candidates are dead-CE bus constants "
                "(alphabet {0x00,0xA7}, zero 0xFF bytes) off ce%d, which is a "
                "dead die.  Neither is programmed and neither is erased, so "
                "neither is a measurement of media and there is no winner to "
                "pick.  %s keeps the key so the page stays visible; it must "
                "not be read as data." % (ce, nm(a_ent.path)), ev)

        naus = ea["aus"] or eb["aus"]

        # 1. MCRC, uncorrected.  The strongest cheap evidence there is: it is a
        #    property of the read and needs no seed, no key and no ECC.
        if ea["mcrc"] is not None and eb["mcrc"] is not None \
                and ea["mcrc"] != eb["mcrc"]:
            win, lose = ((a_ent.path, b_path) if ea["mcrc"] > eb["mcrc"]
                         else (b_path, a_ent.path))
            hi, lo = max(ea["mcrc"], eb["mcrc"]), min(ea["mcrc"], eb["mcrc"])
            return Adjudication(
                key, win, lose, BY_MCRC,
                "%s passes MCRC %d/%d AUs UNCORRECTED; %s passes only %d/%d and "
                "needs EE3 correction to reach the same plaintext.  MCRC is "
                "computed over the complemented stored bytes, so this verdict "
                "uses no seed, no key and no ECC -- it is a property of the "
                "READ, not of which directory the file is in."
                % (nm(win), hi, naus, nm(lose), lo, naus), ev)

        # 2. One decode converged and the other did not.  This has to come
        #    before the comparisons below, and it is the case they cannot make:
        #    `ecc` and `mcrc_c` are only filled when the decode converged, so a
        #    non-convergence leaves them None and every `x is not None and
        #    y is not None` test below silently declines to look.  That is a
        #    MEASURED failure being treated as an unmeasured one -- on the
        #    reference store it sent all 108 of these to the trust table under
        #    the words "content did not separate these", which was false: one
        #    read is repairable and the other is not, which is as separated as
        #    two candidates get.
        if ea["conv"] is not None and eb["conv"] is not None \
                and ea["conv"] != eb["conv"]:
            win, lose = ((a_ent.path, b_path) if ea["conv"]
                         else (b_path, a_ent.path))
            wev, lev = ((ea, eb) if ea["conv"] else (eb, ea))
            return Adjudication(
                key, win, lose, BY_ECC,
                "%s LDPC-converges (%d correction(s), MCRC %s/%s after) and %s "
                "does NOT converge at all, so it is not repairable to valid "
                "codewords and is not usable data.  One read is recoverable and "
                "the other is not; nothing about which directory either sits in "
                "enters this."
                % (nm(win), wev["ecc"], wev["mcrc_c"], naus, nm(lose)), ev)

        # 3. MCRC after correction.  A native-tlc page cannot pass MCRC
        #    uncorrected at all -- it arrives with 25-240 syndromes per codeword
        #    -- so step 1 reads 0/4 on both and decides nothing.  Post-correction
        #    MCRC is the same test asked at the point where it means something:
        #    one candidate is recoverable and the other is not.
        if ea["mcrc_c"] is not None and eb["mcrc_c"] is not None \
                and ea["mcrc_c"] != eb["mcrc_c"]:
            win, lose = ((a_ent.path, b_path) if ea["mcrc_c"] > eb["mcrc_c"]
                         else (b_path, a_ent.path))
            hi = max(ea["mcrc_c"], eb["mcrc_c"])
            lo = min(ea["mcrc_c"], eb["mcrc_c"])
            return Adjudication(
                key, win, lose, BY_MCRC,
                "neither passes MCRC uncorrected (%s vs %s -- expected on a "
                "native-TLC page), but after EE3 correction %s recovers %d/%d "
                "AUs and %s only %d/%d.  One is repairable and the other is not."
                % (ea["mcrc"], eb["mcrc"], nm(win), hi, naus, nm(lose), lo,
                   naus), ev)

        # 4. LDPC correction count.  Sees the PARITY region, which MCRC cannot,
        #    so this is what decides a flipped parity byte -- the case where
        #    both sides pass MCRC 4/4 and still differ.
        if ea["ecc"] is not None and eb["ecc"] is not None \
                and ea["ecc"] != eb["ecc"]:
            win, lose = ((a_ent.path, b_path) if ea["ecc"] < eb["ecc"]
                         else (b_path, a_ent.path))
            lo, hi = min(ea["ecc"], eb["ecc"]), max(ea["ecc"], eb["ecc"])
            return Adjudication(
                key, win, lose, BY_ECC,
                "MCRC cannot separate these (uncorrected %s/%s both, corrected "
                "%s vs %s), so the difference is somewhere MCRC does not look "
                "-- the EE3 PARITY region.  The LDPC decoder does look there: "
                "%s needs %d correction(s) and %s needs %d.  Fewer corrections "
                "is the cleaner read; a perfect one needs none."
                % (ea["mcrc"], naus, ea["mcrc_c"], eb["mcrc_c"],
                   nm(win), lo, nm(lose), hi), ev)

        # 5. The evidence is equal on every axis it has.  There is no further
        #    tiebreak, and inventing one would be the whole mistake this module
        #    was rebuilt to stop making: for five sessions a per-directory rank
        #    table stood here, and on the reference store it decided 108 of 265
        #    conflicts -- every one of which step 2 above settles on content.
        #    Where content really does run out, "contested" is the finding.
        # Equal counts do not mean equal outcomes.  Both candidates can decode
        # cleanly and still carry DIFFERENT data, and that is a much sharper
        # finding than "no evidence": it means one address holds two different
        # pages, so one of these files is misfiled.  The corrected digests are
        # what tells the two apart.
        both_ok = (self._decodes(ea) is True and self._decodes(eb) is True)
        if both_ok and ea["fixed"] and eb["fixed"] and ea["fixed"] != eb["fixed"]:
            return Adjudication(
                key, None, None, UNRESOLVED,
                "UNRESOLVED, and this is the interesting kind.  BOTH candidates "
                "decode cleanly -- MCRC %s/%s each, both LDPC-converged (%s and "
                "%s corrections) -- and the CORRECTED pages still differ (%s vs "
                "%s).  Two self-consistent reads that disagree are not one page "
                "read twice; they are TWO PAGES under one address, so one of "
                "these files is filed under the wrong (ce, block, page, mode).  "
                "No integrity check can say which: MCRC and the EE3 parity both "
                "cover the stored bytes and neither covers the address.  %s is "
                "served only because it was indexed first."
                % (ea["mcrc_c"], ea["aus"], ea["ecc"], eb["ecc"],
                   ea["fixed"], eb["fixed"], nm(a_ent.path)), ev)

        caveat = ""
        if ea["conv"] is False and eb["conv"] is False:
            caveat = ("  Neither candidate's LDPC decode CONVERGED, so neither "
                      "is repairable to valid codewords and neither is usable "
                      "data.  A re-read is what this page needs.")
        elif self._decodes(ea) is False and self._decodes(eb) is False:
            caveat = ("  Neither candidate decodes -- both fail MCRC even after "
                      "correction, so both reads are unusable.")
        elif ea["aus"] is None and eb["aus"] is None:
            caveat = ("  The codec could not read either candidate at all "
                      "(no AU count), so there is no content evidence here to "
                      "weigh -- not equal evidence, ABSENT evidence.")
        elif both_ok and ea["fixed"] and ea["fixed"] == eb["fixed"]:
            caveat = ("  Both decode to the SAME corrected page, so the raw "
                      "difference is bits the ECC repairs and there is nothing "
                      "to choose: either file yields identical data.")
        return Adjudication(
            key, None, None, UNRESOLVED,
            "UNRESOLVED -- content does not separate them (MCRC %s vs %s "
            "uncorrected, %s vs %s corrected; EE3 %s vs %s).  %s is being "
            "served only because it was indexed first, which is not a reason.%s"
            % (ea["mcrc"], eb["mcrc"], ea["mcrc_c"], eb["mcrc_c"], ea["ecc"],
               eb["ecc"], nm(a_ent.path), caveat), ev)

    def adjudicate(self, apply=True, limit=None):
        """Decide every collision on content.  -> list of `Adjudication`

        One record per differing pair, naming both paths, the winner, the basis
        and the reason.  With `apply` (the default) the entry is repointed at
        the winner, so `read_page` returns the page the evidence says was read
        correctly rather than whichever candidate the scan happened to meet
        first.

        Identical repeats are not recorded and are not conflicts: constant
        stored data reads identically every time, so agreement between two reads
        is not evidence of a good read -- but a difference is real evidence that
        one of them is wrong, which is what this looks at.

        Cached: the second call is free.  `adjudicate(apply=False)` re-reads.
        """
        if self._adjudged is not None and apply:
            return self._adjudged
        out = []
        for key, ent in self._contested():
            try:
                mine = ent.path.read_bytes()
            except OSError:
                continue
            for other in list(ent.repeats):
                try:
                    theirs = other.read_bytes()
                except OSError:
                    continue
                if theirs == mine:
                    continue
                adj = self._decide(key, ent, mine, other, theirs)
                out.append(adj)
                if apply:
                    # A store builds entries on demand, so an adjudicated one
                    # has to be retained or the decision evaporates on the next
                    # lookup.  Bounded by the contested pages, not by the store.
                    self._repointed[key] = ent
                if apply and adj.winner is not None \
                        and adj.winner != ent.path:
                    # the evidence names a different file: serve that one
                    losers = [p for p in ent.repeats if p != adj.winner]
                    ent.repeats = losers + [ent.path]
                    ent.path = adj.winner
                    lay = _layout_of(adj.winner) or ent.layout
                    if lay != ent.layout:
                        self.by_layout[ent.layout] -= 1
                        self.by_layout[lay] = self.by_layout.get(lay, 0) + 1
                        ent.layout = lay
                    ent.tier, ent.why = self._tier_for(adj.winner, ent.layout)
                    mine = theirs
                if limit and len(out) >= limit:
                    if apply:
                        self._adjudged = out
                    return out
        if apply:
            self._adjudged = out
        return out

    def conflict_report(self, indent="  "):
        """`adjudicate()` as text -> str.  Every conflict, both paths, and why."""
        adj = self.adjudicate()
        if not adj:
            return indent + ("no conflicts: no two files in this tree claim the "
                             "same page with different bytes")
        lines = ["%s%d conflicting page(s)" % (indent, len(adj))]
        by_basis = {}
        for a in adj:
            by_basis[a.basis] = by_basis.get(a.basis, 0) + 1
        lines.append("%s  resolved by: %s"
                     % (indent, ", ".join("%s=%d" % kv
                                          for kv in sorted(by_basis.items()))))
        for a in adj:
            ce, block, page, slc = a.key
            lines.append("%s  ce%d b%d p%d %s  [%s]"
                         % (indent, ce, block, page,
                            "SLC" if slc else "TLC", a.basis))
            if a.winner is not None:
                lines.append("%s    winner %s" % (indent, a.winner))
                lines.append("%s    loser  %s" % (indent, a.loser))
            else:
                lines.append("%s    NO WINNER; serving %s"
                             % (indent, self.entries[a.key].path))
            for chunk in _wrap(a.reason, 68):
                lines.append("%s    %s" % (indent, chunk))
        return "\n".join(lines)

    def sources(self):
        """Indexed page count per source directory.  -> {dir: (n, tier)}

        "Which capture is actually carrying this index" -- the number that makes
        a fold measurable, and the one that shows an unindexed dump as the zero
        it is.  A count and a tier, and no ordering: this says where the pages
        came from, not which of them is better.
        """
        if self._store is not None:
            # Counted at build time over exactly the records this instance
            # serves, so it is the same number the walk would produce -- without
            # the walk.
            return {k: (v[0], v[-1])
                    for k, v in (self._store.stat("sources", {}) or {}).items()}
        out = {}
        for ent in self.entries.values():
            name = ent.path.parent.name
            n, tier = out.get(name, (0, ent.tier))
            out[name] = (n + 1, tier)
        return out

    def blocks(self, ce=None, slc=None):
        """{(ce, block, mode): page count}.  A full pass over the index.

        Over a store this streams the mmap rather than building a dict of
        entries, but it is still O(pages) -- `describe()` therefore uses the
        build-time group count instead of calling this, because a summary line
        must not cost a scan of the whole array.
        """
        out = {}
        for (c, b, p, s) in self.entries:
            if ce is not None and c != ce:
                continue
            if slc is not None and s != bool(slc):
                continue
            out.setdefault((c, b, s), 0)
            out[(c, b, s)] += 1
        return out

    def describe(self, conflicts=None):
        """A human summary -> str.  Says what was rejected and why.

        `conflicts` controls the one expensive line:
          None   report it if `adjudicate()` has already run, otherwise report
                 how many pages are contested (free -- it is just the repeat
                 count) and say plainly that the byte comparison has not been
                 done.  A number that has not been measured is never printed as
                 if it had been.
          True   run `adjudicate()` now.  ~4000 file opens on the dump, ~9 s on a
                 network or virtualised filesystem, which is why it is not the default for a
                 command as routine as `titania info`.
          False  skip it entirely.
        """
        st = self._store
        lines = ["dump: %s%s" % (self.root,
                                 "   [blob+index store]" if st else ""),
                 "  %d raw pages, %d CEs present: %s"
                 % (len(self.entries), len(self.present_ces()),
                    self.present_ces())]
        if self.redirected:
            # Never silent: a caller that asked for one directory and got
            # another has to be able to see that in the output it already
            # prints, or the redirect becomes the invisible thing this class
            # exists to avoid.
            lines.append("  (asked for %s -- the tree it supersedes; "
                         "prefer_store=False reads the files instead)"
                         % self.requested_root)
        if st is not None:
            lines.append("  %d index record(s) over %d blob(s), built %s"
                         % (st.n_records, len(st.blobs),
                            st.built or "at an unrecorded time"))
        for layout, _rx, note in LAYOUTS:
            n = self.by_layout.get(layout)
            if n:
                lines.append("  %-10s %6d  %s" % (layout, n, note))
        # Both of the next two are O(pages) over a tree and O(1) over a store:
        # they were counted once, at build time, over exactly the records this
        # instance serves.  A summary must not cost a walk of the array.
        nslc = (st.stat("slc_pages", 0) if st is not None
                else sum(1 for k in self.entries if k[3]))
        lines.append("  cell mode: %d pseudo-SLC, %d native TLC"
                     % (nslc, len(self.entries) - nslc))
        ngroups = (st.stat("groups", 0) if st is not None
                   else len(self.blocks()))
        lines.append("  %d (ce, block, mode) groups" % ngroups)

        # -- where the pages came from.  A census, not a league table.
        src = self.sources()
        if src:
            lines.append("  sources:")
            for name, (n, tier) in sorted(src.items(),
                                          key=lambda kv: (-kv[1][0], kv[0])):
                lines.append("      %-15s %6d pages  %s" % (name, n, tier))
            sus = [n for n, (_c, t) in src.items() if t == SUSPECT]
            if sus:
                lines.append("      (%d source(s) QUARANTINED -- indexed but not "
                             "served, the address is not to be believed: %s)"
                             % (len(sus), ", ".join(sorted(sus))))

        # -- contested pages.  Never silent, never invented.
        contested = (st.stat("contested", 0) if st is not None
                     else sum(len(e.repeats) for e in self.entries.values()))
        if contested:
            if conflicts is True and self._adjudged is None:
                self.adjudicate()
            if self._adjudged is not None and conflicts is not False:
                n = len(self._adjudged)
                nres = sum(1 for a in self._adjudged
                           if a.basis == UNRESOLVED)
                lines.append("  %d contested page(s) (a second file claims the "
                             "same key); %d DIFFER" % (contested, n))
                bases = {}
                for a in self._adjudged:
                    bases[a.basis] = bases.get(a.basis, 0) + 1
                lines.append("      resolved by: %s%s"
                             % (", ".join("%s=%d" % kv
                                          for kv in sorted(bases.items())),
                                "" if not nres
                                else "   <-- %d with NO defensible winner"
                                     % nres))
            elif conflicts is not False:
                lines.append("  %d contested page(s) (a second file claims the "
                             "same key); how many DIFFER is NOT MEASURED here "
                             "-- call adjudicate() or `--conflicts`" % contested)
        if self.ties:
            lines.append("      %d collision(s) were a RANK TIE at scan time; "
                         "the incumbent is arbitrary until adjudicated"
                         % len(self.ties))
        if st is not None:
            held = st.n_records - st.stat("served_records", st.n_records)
            if held:
                lines.append("  %d record(s) held at an EXCLUDED TIER (%s) -- "
                             "in the store, indexed, NOT served; pass "
                             "include_suspect=True to see them"
                             % (held, "/".join(EXCLUDED_TIERS)))
        if self.rejected:
            reasons = {}
            for _p, r in self.rejected:
                # collapse only the leading size, so the explanatory numbers in
                # the quarantine reasons survive
                key = re.sub(r"^\d+ bytes", "<N> bytes", r)
                reasons[key] = reasons.get(key, 0) + 1
            lines.append("  %d files not indexed:" % len(self.rejected))
            for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]:
                lines.append("      %5d  %s" % (n, r))
        if self._overlay:
            lines.append("  overlay: %d modified page(s), %s"
                         % (len(self._overlay),
                            "IN PLACE" if self.in_place else "memory only"))
        return "\n".join(lines)

    def __repr__(self):
        return "<DumpDevice %s pages=%d ces=%s writes=%s>" % (
            self.root.name, len(self.entries), self.present_ces(),
            "ON" if self.writes_enabled else "off")


# ------------------------------------------------------------------ selftest --
def find_erased_page(dev, limit=40000, ces=None):
    """First genuinely all-0xFF page in the index, or None.

    Filtered on a 4096-byte prefix first, so the vast majority of pages cost one
    short read rather than 18432 bytes.  The prefix is only ever used to skip a
    page (a non-0xFF prefix proves the page is not erased); a page that passes it
    is then read in full before being called erased.  Cheap in the safe
    direction only -- which is the whole rule.
    """
    n = 0
    # A store's index is sorted, so iterating it is already in key order and
    # materialising a 54-million-entry list to re-sort it would be the exact
    # cost this backing exists to remove.  A dict is not, so it is sorted.
    keys = dev.entries if dev._store is not None else sorted(dev.entries)
    for key in keys:
        if ces is not None and key[0] not in ces:
            continue
        ref = dev._ref_for(key)
        if ref is None:
            continue
        n += 1
        if n > limit:
            return None
        try:
            if _head(ref, 4096).count(0xFF) != 4096:
                continue
            full = ref.read_bytes()
        except OSError:
            continue
        if full.count(0xFF) == len(full):
            return key
    return None


def _head(ref, n):
    """First `n` bytes of a page, from a file or from a blob.

    A `store.PageRef` has its own `head()` -- a seek and a short read inside an
    already-open blob -- so the cheap prefix filter stays cheap under both
    backings instead of quietly becoming a full read.
    """
    fn = getattr(ref, "head", None)
    if fn is not None:
        return fn(n)
    with open(str(ref), "rb") as h:
        return h.read(n)


def _selftest(root=None):
    ok = True
    root = Path(root) if root else default_root()
    print("titania.dumpdev -- selftest\n")

    if not root.exists():
        print("[0] no dump tree at %s -- SKIPPED (nothing here can run)" % root)
        print("\nSELFTEST: SKIPPED")
        return 0
    # The one place that does not take the redirect.  Every other
    # caller asking for `the dump` wants the media and gets `the store`; this
    # self-test's subject is the file walk -- the layout regexes, the rejected
    # list, the cell-mode rule, the rank ties -- and running it against the
    # store would quietly stop testing all of it.  [9j] then proves the store
    # serves everything this walk found, which is the property the redirect
    # rests on.
    dev = DumpDevice(root, prefer_store=False)
    print("[1] discovery")
    for line in dev.describe().splitlines():
        print("    " + line)
    ok &= len(dev.entries) > 0

    # 2. a real page reads back at full size
    ent = None
    for key in sorted(dev.entries):
        if dev.entries[key].layout == "deep":
            ent = dev.entries[key]
            break
    if ent is None:
        ent = dev.entries[sorted(dev.entries)[0]]
    data = dev.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
    print("[2] read ce%d b%d p%d (%s): %d B  sha1=%s"
          % (ent.ce, ent.block, ent.page, "SLC" if ent.slc else "TLC",
             len(data), hashlib.sha1(data).hexdigest()[:12]))
    ok &= len(data) == PAGE_BYTES

    # 3. an absent page must raise, and must not be 0xFF
    miss = (ent.ce, 4095, 1103)
    try:
        bad = dev.read_page(miss[0], miss[1], miss[2], slc=ent.slc)
        print("[3] absent page returned %d bytes -- WRONG" % len(bad))
        ok = False
    except DeviceError as exc:
        blank = "\xff" in str(exc)
        print("[3] absent page raises DeviceError (not 0xFF): %s"
              % str(exc).split(";")[0][:90])
        ok &= not blank

    # 3b. the wrong cell mode must raise too, and name the mode it does have
    try:
        dev.read_page(ent.ce, ent.block, ent.page, slc=not ent.slc)
        print("[3b] wrong cell mode was served silently -- WRONG")
        ok = False
    except DeviceError as exc:
        hint = "pass slc=" in str(exc)
        print("[3b] wrong cell mode raises, hint present: %s" % hint)
        ok &= hint

    # 4. read_block skips gaps instead of faking them
    got = list(dev.read_block(ent.ce, ent.block, slc=ent.slc))
    have = len([k for k in dev.entries
                if k[0] == ent.ce and k[1] == ent.block and k[3] == ent.slc])
    print("[4] read_block yielded %d of %d indexed pages (%d-page block)"
          % (len(got), have, geom.pages_per_block(ent.slc)))
    ok &= len(got) == min(have, geom.pages_per_block(ent.slc))

    # 5. writes are refused until both opt-ins are satisfied
    fresh = DumpDevice(root, prefer_store=False)      # same backing as `dev`
    steps = []
    try:
        fresh.write_page(ent.ce, ent.block, ent.page, b"\x00" * PAGE_BYTES,
                         slc=ent.slc)
        steps.append("write with writes disabled SUCCEEDED -- WRONG")
        ok = False
    except WriteProtected:
        steps.append("write refused while writes disabled")
    try:
        fresh.enable_writes("please")
        steps.append("enable_writes took a wrong phrase -- WRONG")
        ok = False
    except WriteProtected:
        steps.append("enable_writes refused the wrong phrase")
    fresh.enable_writes(CONFIRM)
    steps.append("enable_writes(CONFIRM) accepted")
    print("[5] write guards: " + "; ".join(steps))

    # 6. a write lands in the overlay and not in the original file
    before = ent.path.read_bytes()
    before_sha = hashlib.sha1(before).hexdigest()
    before_mtime = ent.path.stat().st_mtime_ns
    payload = bytes((i * 7 + 3) & 0xFF for i in range(PAGE_BYTES))
    fresh.write_page(ent.ce, ent.block, ent.page, payload, slc=ent.slc)
    back = fresh.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
    after_sha = hashlib.sha1(ent.path.read_bytes()).hexdigest()
    after_mtime = ent.path.stat().st_mtime_ns
    untouched = (after_sha == before_sha and after_mtime == before_mtime)
    print("[6] overlay write: read-back identical=%s, original file untouched=%s "
          "(sha1 %s)" % (back == payload, untouched, before_sha[:12]))
    ok &= (back == payload) and untouched
    # and the original device still sees the original bytes
    print("    a second DumpDevice over the same root still reads the original: "
          "%s" % (dev.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
                  == before))
    ok &= dev.read_page(ent.ce, ent.block, ent.page, slc=ent.slc) == before

    # 7. erase_block, also overlay-only
    n = fresh.erase_block(ent.ce, ent.block)
    erased = fresh.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
    still = hashlib.sha1(ent.path.read_bytes()).hexdigest() == before_sha
    print("[7] erase_block marked %d page(s) 0xFF in the overlay; page now "
          "all-0xFF=%s; original file untouched=%s"
          % (n, erased == b"\xff" * PAGE_BYTES, still))
    ok &= erased == b"\xff" * PAGE_BYTES and still
    # an erase does not conjure pages the dump never had
    try:
        fresh.read_page(ent.ce, ent.block, 4095, slc=ent.slc)
        print("    erase invented a page the dump never had -- WRONG")
        ok = False
    except DeviceError:
        print("    a page the dump never had still raises after the erase")

    # 8. flush to a new directory, then read it back through a new DumpDevice
    import tempfile
    with tempfile.TemporaryDirectory(prefix="titania_dump_") as tmp:
        written = fresh.flush(tmp)
        rt = DumpDevice(tmp)
        rt_ok = (len(written) > 0 and len(rt.entries) == len(written))
        print("[8] flush wrote %d file(s) to a new directory; re-scan indexed "
              "%d, round-trip=%s" % (len(written), len(rt.entries), rt_ok))
        ok &= rt_ok

    # 9. flush over the originals is refused without in_place
    try:
        fresh.flush()
        print("[9] flush() rewrote the originals without in_place -- WRONG")
        ok = False
    except WriteProtected:
        print("[9] flush() without out_dir refused (in_place is not set)")

    # 9b. the reduction: exact on a real written page and a real ERASED page,
    #     and the accelerated path must return the same bytes as the plain one
    import zlib
    written = dev.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
    s = dev.scan_page(ent.ce, ent.block, ent.page, slc=ent.slc)
    good = (s.all_ones is False
            and s.nonff_bytes == PAGE_BYTES - written.count(0xFF)
            and s.first_nonff == next(i for i, b in enumerate(written)
                                      if b != 0xFF)
            and s.crc == zlib.crc32(written) & 0xFFFFFFFF)
    print("[9b] scan of a real WRITTEN page ce%d b%d p%d: all_ones=%s nonff=%d "
          "first=%d crc=%#010x  exact=%s"
          % (ent.ce, ent.block, ent.page, s.all_ones, s.nonff_bytes,
             s.first_nonff, s.crc, good))
    ok &= good

    er = find_erased_page(dev)
    if er is None:
        print("[9c] no all-0xFF page in this tree -- ERASED-page check uses one "
              "made by erase_block instead")
        tmpdev = DumpDevice(root, prefer_store=False)  # same backing as `dev`
        tmpdev.enable_writes(CONFIRM)
        tmpdev.erase_block(ent.ce, ent.block)
        blank = tmpdev.read_page(ent.ce, ent.block, ent.page, slc=ent.slc)
        sb = tmpdev.scan_page(ent.ce, ent.block, ent.page, slc=ent.slc)
        src = "overlay"
    else:
        blank = dev.read_page(er[0], er[1], er[2], slc=er[3])
        sb = dev.scan_page(er[0], er[1], er[2], slc=er[3])
        src = "%s" % dev.entries[er].path.name
    good = (sb.all_ones is True and sb.nonff_bytes == 0
            and sb.first_nonff is None
            and sb.crc == zlib.crc32(blank) & 0xFFFFFFFF
            and blank == b"\xff" * PAGE_BYTES)
    print("[9c] scan of an ERASED page (%s): all_ones=%s nonff=%d first=%s "
          "crc=%#010x  exact=%s" % (src, sb.all_ones, sb.nonff_bytes,
                                    sb.first_nonff, sb.crc, good))
    ok &= good

    # the same bytes whichever path is taken -- for a dump the fast path is the
    # plain path, which is the point: nothing here is ever inferred
    pairs = 0
    same = 0
    picks = [k for k in sorted(dev.entries)][:1] + \
            [k for k in sorted(dev.entries)][len(dev.entries) // 2:
                                             len(dev.entries) // 2 + 2]
    if er is not None:
        picks.append(er)
    for k in picks:
        a = dev.read_page_ex(k[0], k[1], k[2], slc=k[3])
        dev.fast_erased = False
        b = dev.read_page(k[0], k[1], k[2], slc=k[3])
        dev.fast_erased = True
        pairs += 1
        same += (a.data == b and a.source == FULL)
    print("[9d] fast vs full path over %d page(s) (incl. %s erased): %d "
          "identical, every source == FULL"
          % (pairs, "1" if er is not None else "0", same))
    ok &= same == pairs

    # 9e. Every conflict, enumerated and resolved with a stated reason.
    #     This is the check that the resolution is a policy and not an accident:
    #     each one names both files, the winner, and what decided it.
    adj = dev.adjudicate()
    print("[9e] conflicts: %d page(s) claimed twice with DIFFERENT bytes"
          % len(adj))
    if not adj:
        print("     none in this tree -- SKIPPED (nothing to resolve here)")
    else:
        bases = {}
        for a in adj:
            bases[a.basis] = bases.get(a.basis, 0) + 1
        print("     resolved by: %s"
              % ", ".join("%s=%d" % kv for kv in sorted(bases.items())))
        for a in adj:
            ce, block, page, slc = a.key
            # split on ". " -- a bare "." also occurs inside every file name
            first = re.split(r"\.\s", a.reason)[0]
            print("     ce%-2d b%-5d p%-4d %s [%-10s] %s"
                  % (ce, block, page, "SLC" if slc else "TLC", a.basis,
                     first[:94]))
            # every record must name both candidates and give a reason
            named = (a.winner is not None and a.loser is not None) or \
                    a.basis == UNRESOLVED
            ok &= named and len(a.reason) > 40
        # a resolution must never be by "first path" -- every one has a basis,
        # and every basis is a measurement of the read
        ok &= all(a.basis in (BY_MCRC, BY_ECC, UNRESOLVED) for a in adj)
        content = [a for a in adj if a.basis in (BY_MCRC, BY_ECC)]
        served_ok = all(dev.entries[a.key].path == a.winner for a in content)
        print("     %d resolved on CONTENT (MCRC/EE3), and the served file is "
              "the content winner in every one: %s" % (len(content), served_ok))
        ok &= served_ok
        # Nothing may be decided by provenance, because there is no longer any
        # way to: this asserts the absence rather than assuming it.
        unres = [a for a in adj if a.basis == UNRESOLVED]
        print("     %d left UNRESOLVED -- content ran out, and no rank, "
              "directory or path order was used to break the tie" % len(unres))

    # 9f. a dead-ce page is neither data nor erased, and must be classified as
    #     neither.  CE0 answers with a bus constant; if that ever starts looking
    #     like an erased page, a sweep of CE0 reads as "nothing is written here".
    dead = [a for a in adj if "dead-CE" in a.reason]
    ent0 = dev.entry(0, 0, 0, slc=True)
    if ent0 is None:
        print("[9f] no ce0 b0 p0 in this tree -- SKIPPED (dead-CE check needs it)")
    else:
        raw0 = ent0.path.read_bytes()
        cls = page_class(raw0)
        good = (cls == DEAD_CE and raw0.count(0xFF) == 0
                and set(raw0) <= DEAD_CE_ALPHABET)
        print("[9f] ce0 b0 p0 classifies as %r (not %r, not %r); 0xFF bytes=%d; "
              "%d dead-CE conflict(s) recorded UNRESOLVED: %s"
              % (cls, PROGRAMMED, ERASED, raw0.count(0xFF), len(dead),
                 all(a.basis == UNRESOLVED for a in dead)))
        ok &= good and all(a.basis == UNRESOLVED for a in dead)

    # 9g. The quarantine invariant: a SUSPECT record -- one whose ADDRESS is not
    #     to be believed -- must never be what an ordinary key is served from
    #     while an unquarantined candidate for the same key exists.  This is the
    #     one thing content cannot check for itself, so it is checked here.
    loose = DumpDevice(root, include_suspect=True, prefer_store=False)
    served_bad = [e for e in loose.entries.values()
                  if e.tier in EXCLUDED_TIERS and e.repeats
                  and any(loose._tier_for(p, e.layout)[0] == UNKNOWN
                          for p in e.repeats)]
    print("[9g] with include_suspect=True, %d quarantined file(s) are served "
          "while an unquarantined file claims the same key (want 0)"
          % len(served_bad))
    ok &= not served_bad
    extra = len(loose.entries) - len(dev.entries)
    print("     include_suspect adds %d page(s): %s"
          % (extra, {k: v for k, v in loose.by_layout.items()
                     if k in SUSPECT_LAYOUTS}))
    ok &= extra >= 0

    # 9j. The two backings agree.  A tree and the store built from it must be
    #     the same device or the migration is not a migration.  Bounded on
    #     purpose -- `pc_migrate.py --verify` compares every page and is the
    #     exhaustive check; this one runs on every selftest and catches a
    #     regression in the store path without costing two minutes.
    ok &= _cross_backing(dev, root, adj, er)

    # 9h. a folded page range round-trips: every page pb_fold.py wrote is
    #     indexed under the address its name claims, and reads back identical to
    #     the slice of the source blob at that page's offset.  This is the check
    #     that the nl_fold.py assumption has not crept back in -- it would show
    #     up here as a page whose contents belong to a different page number.
    _fold_roundtrip(dev, root)

    # 9i. counts: what each source contributes, so a fold is measurable.
    src = dev.sources()
    print("[9i] indexed pages by source:")
    for name, (n, rank, tier) in sorted(src.items(),
                                        key=lambda kv: (-kv[1][0], kv[0])):
        print("       %-15s %6d  rank %2d  %s" % (name, n, rank, tier))
    folded = sum(n for k, (n, _r, _t) in src.items()
                 if k in ("nk_lut", "no_verify", "no_recheck", "no_userread"))
    print("     %d of %d indexed pages come from a FOLDED loose dump; without "
          "those folds the index would hold %d"
          % (folded, len(dev.entries), len(dev.entries) - folded))
    ok &= len(dev.entries) > 0

    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _twin_of(dev, root):
    """The other backing for the same pages, if one exists.  -> path or None

    A tree looks for a store beside it; a store reads the tree it was built
    from out of its own metadata.  Either way the pairing is recorded rather
    than guessed, so this never compares two unrelated dumps and calls the
    result agreement.
    """
    root = Path(root)
    if dev._store is not None:
        src = dev._store.meta.get("source_tree")
        if not src:
            return None
        # The recorded path was spelled by whichever platform built the store
        # (this one is a network path) and may be unopenable from
        # the platform reading it.  The tree that matters is the one beside the
        # store, so the recorded value is used as a name.
        p = root.parent / PurePosixPath(str(src).replace("\\", "/")).name
        return p if p.exists() else None
    # A tree does not know its stores, so the stores are asked: the pairing is
    # whatever `store.json` records as its source tree.  Guessing a name would
    # eventually compare two unrelated dumps and report the disagreement as a
    # bug in the store.  The declared partner (`STORE_FOR_TREE`, the one the
    # redirect uses) is tried first so [9j] proves the same pairing every other
    # caller silently gets, rather than some other store that also names this
    # tree -- `the store.bak-*` does.
    declared = canonical_root(root, prefer_store=True)
    if declared != root:
        return declared
    try:
        for cand in sorted(root.parent.iterdir()):
            if cand == root:
                continue
            if source_tree_name(cand) == root.name:
                return cand
    except OSError:
        pass
    return None


def _cross_backing(dev, root, adj, erased_key):
    """[9j] -- the tree and its store are the same device.  -> bool

    Checks the pages where a storage refactor would actually break something:
    every contested page (the adjudicated ones, where the served candidate is a
    decision and not an accident), the dead-CE page, an erased page, and a
    spread of ordinary ones.  Plus the two rules that must survive any backing:
    a missing page raises, and it does not raise with 0xFF in its hands.
    """
    twin_path = _twin_of(dev, root)
    if twin_path is None:
        print("[9j] no second backing beside %s -- SKIPPED (build one with "
              "`py pc_migrate.py %s %s_store`)"
              % (root, root, Path(root).name))
        return True
    try:
        # prefer_store: the twin is named explicitly here, so it must be opened
        # exactly as named -- otherwise a tree twin would redirect back to the
        # store and this check would compare a backing with itself and call the
        # perfect agreement a pass.
        twin = DumpDevice(twin_path, prefer_store=False)
    except DeviceError as exc:
        print("[9j] second backing %s did not open: %s -- FAIL"
              % (twin_path, exc))
        return False
    kind = "store" if twin._store is not None else "tree"
    # Not an equality.  `pz_reclaim.py` deletes a tree file only after re-reading
    # its exact bytes back out of the store, so the tree is a shrinking subset
    # of the store by design and an equality here would turn every reclaim into
    # a self-test failure.  The invariant that actually protects the reader is
    # directional: the store must serve everything the tree does.  It is checked
    # both ways round -- as a count here, and page-for-page below, where `miss`
    # (a key present in `dev` and absent from `twin`) must be 0.
    store_side, tree_side = ((twin, dev) if twin._store is not None
                             else (dev, twin))
    ok = len(store_side.entries) >= len(tree_side.entries)
    print("[9j] second backing (%s) %s: %d key(s) vs %d here%s"
          % (kind, twin_path, len(twin.entries), len(dev.entries),
             "" if len(store_side.entries) == len(tree_side.entries)
             else "  -- the store holds %d key(s) the tree no longer has "
                  "(reclaimed files; the store is the superset)"
                  % (len(store_side.entries) - len(tree_side.entries))))

    # Sample from the tree side, always.  `miss` then means "the store has lost
    # a page the tree still holds", which is a real fault; sampling the other
    # way round would only rediscover the reclaim.
    keys = list(tree_side.entries)
    if tree_side._store is None:
        keys.sort()
    picks = keys[:40] + keys[len(keys) // 2:len(keys) // 2 + 40] + keys[-40:]
    picks += [a.key for a in adj]                   # every contested page
    picks.append((0, 0, 0, True))                   # the dead-CE page
    if erased_key is not None:
        picks.append(erased_key)
    seen, tried, same, miss = set(), 0, 0, 0
    for k in picks:
        if k in seen:
            continue
        seen.add(k)
        try:
            a = tree_side.read_page(k[0], k[1], k[2], slc=k[3])
        except DeviceError:
            continue          # not in the tree at all -- nothing to compare
        tried += 1
        try:
            b = store_side.read_page(k[0], k[1], k[2], slc=k[3])
        except DeviceError:
            miss += 1
            continue
        same += (a == b)
    print("     %d of %d sampled key(s) are readable on the tree side (incl. "
          "all %d adjudicated, the dead-CE page and an erased one): %d "
          "byte-identical in the store, %d ABSENT from the store"
          % (tried, len(seen), len(adj), same, miss))
    ok &= (same == tried - miss) and miss == 0 and tried > 0

    # the absence rule, on the other backing
    try:
        bad = twin.read_page(0, 4095, 1103, slc=True)
        print("     an absent page returned %d bytes from the %s -- WRONG"
              % (len(bad), kind))
        ok = False
    except DeviceError as exc:
        blank = "\xff" in str(exc)
        print("     an absent page raises on the %s too, with no 0xFF in the "
              "message: %s" % (kind, not blank))
        ok &= not blank

    # the quarantine rule, on the other backing: a record whose address is not
    # to be believed must never be what an ordinary key is served from.
    bad_tier = []
    for k in [a.key for a in adj][:20]:
        e = twin.entries.get(k)
        if e is not None and e.tier in EXCLUDED_TIERS:
            bad_tier.append(k)
    print("     adjudicated pages served from a QUARANTINED source on "
          "the %s: %d (want 0)" % (kind, len(bad_tier)))
    ok &= not bad_tier

    # And the redirect every other caller silently takes lands on the backing
    # this check just proved.  Without this line the equivalence above is about
    # two paths nobody uses; with it, `DumpDevice(<tree>)` is covered.
    tree_root = Path(tree_side.requested_root)
    want = Path(store_side.root)
    if not _env_prefer_store():
        # The redirect was switched off for this process on purpose.  Saying
        # FAIL here would report the operator's own A/B switch as a defect.
        print("     DumpDevice(%s) -> the store: NOT CHECKED, %s=0 is set for "
              "this process" % (tree_root.name, _config().ENV_PREFER_STORE))
    else:
        landed = canonical_root(tree_root)
        print("     DumpDevice(%s) resolves to %s (want %s): %s"
              % (tree_root.name, landed.name, want.name, landed == want))
        ok &= landed == want
    return ok


def _fold_roundtrip(dev, root):
    """[9h] -- a folded page range still means what its file names say.

    Takes a source blob whose name carries a page range, re-slices it, and
    checks each page against the index.  Prints SKIPPED, not PASS, when the
    local tree has no blob to check against: a check that did not run must not
    read as a check that passed.
    """
    src = Path(root).parent / "nn_dump"
    blobs = sorted(src.glob("nn_ce*_b*_slc_p*-*.bin")) if src.exists() else []
    if not blobs:
        print("[9h] no range blob under %s -- SKIPPED (fold round-trip needs "
              "one)" % src)
        return
    rx = re.compile(r"^nn_ce(\d+)_b(\d+)_slc_p(\d+)-(\d+)\.bin$")
    # the smallest MULTI-page blob: a one-page repair blob would round-trip
    # trivially and its shifted-by-one control would have nothing to shift, so
    # it cannot catch an off-by-N fold, which is the whole point of this check.
    # Filtered by `rx`, not by the glob.  `nn_ce*_b*_slc_p*-*.bin` is looser
    # than the regex -- the trailing `*` swallows anything, so a name like
    # `nn_ce6_b10_slc_p0-3_retry.bin` globs but does not match -- and
    # `rx.match(...)` then returned None, which the `m.group(i)` below turned
    # into an AttributeError that killed the whole selftest.  A blob this
    # function cannot parse is one it must skip, exactly like no blob at all.
    # The match is carried, not recomputed: re-running `rx.match` on the winner
    # is a second chance to get None, which is the bug this filter removes.
    multi = [(p, m) for p, m in ((p, rx.match(p.name)) for p in blobs)
             if m is not None and p.stat().st_size > PAGE_BYTES]
    if not multi:
        print("[9h] no multi-page blob under %s whose name parses as a page "
              "RANGE -- SKIPPED (an off-by-N fold cannot be detected with one "
              "page, and cannot be checked at all without the range)" % src)
        return
    blob, m = min(multi, key=lambda pm: pm[0].stat().st_size)
    ce, block, p0, p1 = (int(m.group(i)) for i in (1, 2, 3, 4))
    raw = blob.read_bytes()
    n = p1 - p0 + 1
    if len(raw) != n * PAGE_BYTES:
        print("[9h] %s is %d bytes, not %d for p%d..p%d -- SKIPPED"
              % (blob.name, len(raw), n * PAGE_BYTES, p0, p1))
        return
    same = missing = 0
    for i, pg in enumerate(range(p0, p1 + 1)):
        want = raw[i * PAGE_BYTES:(i + 1) * PAGE_BYTES]
        try:
            got = dev.read_page(ce, block, pg, slc=True)
        except DeviceError:
            missing += 1
            continue
        same += (got == want)
    print("[9h] fold round-trip %s (p%d..p%d): %d of %d pages read back "
          "identical through the index, %d not indexed"
          % (blob.name, p0, p1, same, n, missing))
    # the address is what is under test: an off-by-N fold would land the bytes
    # of page p0+k under some other page number, so shifting by one must FAIL.
    shifted = 0
    for i, pg in enumerate(range(p0, p1)):
        want = raw[(i + 1) * PAGE_BYTES:(i + 2) * PAGE_BYTES]
        try:
            if dev.read_page(ce, block, pg, slc=True) == want:
                shifted += 1
        except DeviceError:
            pass
    print("     shifted-by-one control: %d page(s) also match the NEXT page's "
          "bytes (want 0 unless the block is constant)" % shifted)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        argv.remove("--selftest")
        return _selftest(argv[0] if argv else None)
    want_conf = "--conflicts" in argv
    if want_conf:
        argv.remove("--conflicts")
    suspect = "--include-suspect" in argv
    if suspect:
        argv.remove("--include-suspect")
    dev = DumpDevice(argv[0] if argv else None, include_suspect=suspect)
    print(dev.describe(conflicts=want_conf or None))
    if want_conf:
        print("")
        print("conflicts:")
        print(dev.conflict_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
