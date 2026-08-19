"""Region-aware page decode dispatch.

PAGE GEOMETRY varies by region and the address is what decides it.  A page in
`fw-group2` is five chunks of 2048 payload bytes each carrying its own 4-byte
CRC-32Q digest; it is not four 4104-byte AUs behind a 6-byte MCRC, and running
the FORMAT_A codec over one fails on every page.  That is not a media failure
and it must never be rendered as one: measured, 1836 of this store's 1840
group-2 pages verify 5/5, and the viewer used to paint all 1840 of them "MCRC
FAIL 0/4" in red.

This module owns two things and nothing else:

  * `region_of(ce, block)` -- a thin wrapper over `titania.geometry.region`,
    which is the only authority on the boundaries.  Never re-derive a region
    from the block number: block 0 is group 2 on CE 0..5 and ordinary FTL media
    on CE 6..11, and a map that ignored the CE would feed real FORMAT_A pages to
    the group-2 decoder.
  * `decode_for_region(...)` -- run the decoder that region actually uses, and
    report which one ran, at what MCRC width, over what unit.

It is pure: no imgui, no OpenGL, no store, and every `titania` import is lazy,
exactly as `model.py` does it, because the titania package may be served over a
network or vm share that can be asleep.  `model.py` imports this; this imports
only the package root.  The one-way chain stays one-way.

Vocabulary.  "unit" is the region's own division of a page -- an AU in the FTL
and group-0/1 regions, a chunk in group 2 -- and `unit_noun` exists so the
inspector can say "chunk 3 of 5" where that is the truth, instead of printing
"AU 3 of 4" over bytes that are not AUs.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import threading
import time

import numpy as np

from . import DATA_DIR, ensure_titania_on_path

__all__ = [
    "REGION_FW_GROUP2", "REGION_FW_GROUP01", "REGION_FTL", "REGION_UNKNOWN",
    "REGIONS", "region_of", "region_grid", "RULE", "DECODER", "GEOMETRY",
    "UNIT_NOUN",
    "UNIT_COUNT", "MCRC_WIDTH", "FORMAT_NAME", "FORMATS", "FORMAT_TABLE",
    "FORMAT_MCRC_BYTES", "AUX_LAYOUTS", "aux_layout_name", "AUX_LAYOUT_NAME",
    "AUX_UNNAMED_NOTE", "aux_layout_of", "parse_aux", "describe_region",
    "PageFormat", "describe", "region_info", "Unit", "Outcome",
    "decode_for_region", "needs_codec",
    # --- ECC: a verdict of its own, never folded into the MCRC one ---------
    "EccState", "EccFix", "EccResult", "ECC_LABEL", "ECC_SHORT",
    "ECC_SENTENCE", "ECC_SEVERITY", "ECC_CODE", "ecc_of_page", "worse_ecc",
    "ECC_DEEP_MARGIN", "describe_ecc",
]


# ---------------------------------------------------------------------------
# region names
# ---------------------------------------------------------------------------
# Spelled the same as titania.geometry.REGION_* so a value from either side is
# the same string.  They are duplicated here (not imported at module scope) only
# so that the constants exist with the share asleep; region_of() still asks
# geometry, never these.
REGION_FW_GROUP2 = "fw-group2"
REGION_FW_GROUP01 = "fw-group01"
REGION_FTL = "ftl"
#: No CE was supplied, so the region is genuinely undetermined.  It is a fourth
#: value, not a default: "we did not compute it" must not read as "ftl".
REGION_UNKNOWN = "unknown"
REGIONS = (REGION_FW_GROUP2, REGION_FW_GROUP01, REGION_FTL)


_TITANIA: dict = {}


def _titania() -> dict:
    """-> {'geometry', 'fw', 'aux', 'codec'}, imported once, lazily.

    Raises whatever the import raises; every caller in this module catches it
    and turns it into a sentence, because a dead share must land in the panel,
    not in the frame loop.
    """
    if not _TITANIA:
        ensure_titania_on_path()
        import titania.auxdata as _aux
        import titania.codec as _codec
        import titania.fw as _fw
        import titania.geometry as _geom
        _TITANIA.update(geometry=_geom, fw=_fw, aux=_aux, codec=_codec)
    return _TITANIA


def region_of(ce, block) -> str:
    """(ce, block) -> one of REGIONS, or REGION_UNKNOWN.

    `ce` of None means the caller does not know which die this is, and the only
    honest answer to "which decode rules apply" is then REGION_UNKNOWN -- the
    CE is half of the group-2 test.  Any failure to reach titania is the same
    answer for the same reason.
    """
    if ce is None or block is None:
        return REGION_UNKNOWN
    try:
        return str(_titania()["geometry"].region(int(ce), int(block)))
    except Exception:                                        # noqa: BLE001
        return REGION_UNKNOWN


def region_grid(n_ce: int, n_block: int) -> tuple[str, ...]:
    """Every (ce, block) region at once, row-major: index = ce * n_block + block.

    Same authority as region_of() -- titania.geometry.region, asked once per
    address -- but it resolves the module once instead of per call, which is the
    difference between 9 ms and 600 ms for the whole 12 x 4000 grid.  A view
    that needs to colour the drive by region needs the grid, and a view that
    re-derives it from the block number is the bug this module exists to stop.

    Never raises: a grid of REGION_UNKNOWN is the answer when titania cannot be
    reached, and callers must render that as "not computed", never as `ftl`.
    """
    n_ce, n_block = int(n_ce), int(n_block)
    try:
        region = _titania()["geometry"].region
    except Exception:                                        # noqa: BLE001
        return (REGION_UNKNOWN,) * (n_ce * n_block)
    out: list[str] = []
    try:
        for ce in range(n_ce):
            for block in range(n_block):
                out.append(str(region(ce, block)))
    except Exception:                                        # noqa: BLE001
        return (REGION_UNKNOWN,) * (n_ce * n_block)
    return tuple(out)


# ---------------------------------------------------------------------------
# what the inspector prints
# ---------------------------------------------------------------------------
#: region -> the addressing RULE that put this page in it.  The panel shows the
#: rule next to the name so the verdict is checkable from the address alone.
RULE = {
    REGION_FW_GROUP2:
        "block 0 AND ce in 0..5 -- the six dies the boot ROM boots from "
        "(geometry.FW_GROUP2_BLOCK / FW_GROUP2_CES). Block 0 on CE 6..11 is "
        "NOT group 2; it is ordinary FTL media.",
    REGION_FW_GROUP01:
        "block in 6,7 (fw group 0) or 8,9 (fw group 1), on EVERY ce "
        "(geometry.FW_GROUP01_BLOCKS).",
    REGION_FTL:
        "every other (ce, block) -- the main FTL media, which is most of the "
        "drive.",
    REGION_UNKNOWN:
        "no CE was supplied, so the region was not computed. The CE is half of "
        "the group-2 test, so nothing here is a claim about this page.",
}

#: region -> which decoder actually ran.
DECODER = {
    REGION_FW_GROUP2:
        "titania.fw.check_group2_page (structural verdict) + "
        "fw.decode_group2_page (plaintext): 5 chunks of 2048 payload bytes, "
        "each 4 x (512 payload + 300 A10R parity) with its own 4-byte CRC-32Q "
        "AU digest. The FORMAT_A codec fails on every group-2 page by "
        "construction.",
    REGION_FW_GROUP01:
        "the FORMAT_A codec (titania.ftl.default_codec().decode) -- measured "
        "MCRC-clean 4/4 here. titania.fw.decode_group01_page is the region's "
        "own decoder and describes the SAME geometry, so the codec path is "
        "kept and only the region label changes.",
    REGION_FTL:
        "the FORMAT_A codec (titania.ftl.default_codec().decode).",
    REGION_UNKNOWN:
        "the FORMAT_A codec, as a labelled fallback -- the region could not be "
        "computed, so treat the verdict below as unconfirmed.",
}

#: region -> what one page divides into, singular noun.
UNIT_NOUN = {
    REGION_FW_GROUP2: "chunk",
    REGION_FW_GROUP01: "AU",
    REGION_FTL: "AU",
    REGION_UNKNOWN: "AU",
}

#: region -> how many of them there are in a page.
UNIT_COUNT = {
    REGION_FW_GROUP2: 5,
    REGION_FW_GROUP01: 4,
    REGION_FTL: 4,
    REGION_UNKNOWN: 4,
}

#: region -> bytes of MCRC/digest per unit.  0 would mean "no MCRC at all"
#: (FORMAT_C); no region here is that.
MCRC_WIDTH = {
    REGION_FW_GROUP2: 4,
    REGION_FW_GROUP01: 6,
    REGION_FTL: 6,
    REGION_UNKNOWN: 6,
}

#: region -> the codec format its pages are stored in, as a display string.
FORMAT_NAME = {
    REGION_FW_GROUP2: "group-2 page format (A10R, 4-byte AU digest, scrambled)",
    REGION_FW_GROUP01: "FORMAT_A",
    REGION_FTL: "FORMAT_A",
    REGION_UNKNOWN: "FORMAT_A (assumed)",
}

#: The four AU formats titania.codec knows, for the reference table in the UI.
#: Five display cells per row -- (name, mcrc, ecc, scrambling, what it is for)
#: -- because the panel prints the cells verbatim; a bare 6 in the MCRC column
#: reads as a count of something rather than as a width.  "no MCRC" is spelled
#: out for FORMAT_C: a format with no MCRC cannot fail one, and that absence of
#: a failure is not evidence of a good page.
FORMATS = (
    ("FORMAT_A", "6-byte MCRC", "EE3 LDPC", "scrambled", "main media"),
    ("FORMAT_B", "4-byte MCRC", "A10R (512x4)", "scrambled", "firmware images"),
    ("FORMAT_C", "NO MCRC", "none", "not scrambled", "raw / long reads"),
    ("FORMAT_D", "4-byte MCRC", "as A (EE3 LDPC)", "scrambled",
     "RAID parity, read path only"),
)
#: Older spelling kept as an alias so a view can use either name.
FORMAT_TABLE = FORMATS

#: The same widths as numbers, for anything that needs to compute rather than
#: print.  0 means the format has no MCRC at all.
FORMAT_MCRC_BYTES = {
    "FORMAT_A": 6,
    "FORMAT_B": 4,
    "FORMAT_C": 0,
    "FORMAT_D": 4,
}

#: region -> how a page here is checked: the shape, never the verdict.  This is
#: the one line that stops "MCRC FAIL 0/4" being printed over a page that has
#: no AUs and no 6-byte MCRC to begin with.
GEOMETRY = {
    REGION_FW_GROUP2: "5 chunks x 4-byte digest",
    REGION_FW_GROUP01: "4 AUs x 6-byte MCRC",
    REGION_FTL: "4 AUs x 6-byte MCRC",
    REGION_UNKNOWN: "4 AUs x 6-byte MCRC (assumed -- the region is unknown)",
}

#: aux layout id -> the firmware's own name, or None where the image has none.
#: Layout 1 is in the enumeration and simply has no name in this firmware; the
#: renderer must say so and must never invent one.
AUX_LAYOUTS = (0, 1, 2, 3)
_AUX_LAYOUT_NAMES = {
    0: "cAuxLayoutForInternalData",
    1: None,
    2: "cAuxLayoutForForNSID23",
    3: "cAuxLayoutForRaidParityMso",
}


def aux_layout_name(layout) -> str:
    """-> a display string for an aux layout id, or for 'unknown'.

    `titania.auxdata.LAYOUT_NAMES` is asked first so the GUI cannot drift from the
    firmware table; the copy above is the fallback for a sleeping share.  A
    layout the firmware does not name renders as "1 (unnamed in this
    firmware)", never as a plausible invention, and anything that is not one of
    LAYOUTS -- including titania.auxdata.UNKNOWN, which is falsey but is not False
    -- renders as "UNKNOWN".
    """
    names = _AUX_LAYOUT_NAMES
    try:
        names = _titania()["aux"].LAYOUT_NAMES
    except Exception:                                        # noqa: BLE001
        pass
    if not isinstance(layout, int) or isinstance(layout, bool):
        return "UNKNOWN"
    if layout not in names:
        return "UNKNOWN"
    name = names.get(layout)
    if not name:
        return "%d (unnamed in this firmware)" % (layout,)
    return "%d (%s)" % (layout, name)


#: The uppercase spelling the views look for (`getattr(mod, "AUX_LAYOUT_NAME")`)
#: -- one function, two names, so a renderer can find it either way.
AUX_LAYOUT_NAME = aux_layout_name

#: The exact words for a layout the firmware defines but does not name.  It is
#: a constant so every renderer says the same thing and none of them is tempted
#: to fill the gap with a name from a sibling layout.
AUX_UNNAMED_NOTE = "unnamed in this firmware"


#: The captured DRIVE's own msoDescriptor bytes, read out of the Bootstrap
#: RootStorage of the capture itself and written beside the stores as
#: `msodescriptor.bin`. Loading it is what turns "aux layout: UNKNOWN" into the
#: real per-MSO layout, without relaxing `aux.layout_for`'s refusal to guess:
#: the bytes are that board's, not a table of what some other board had. Which
#: is exactly why the path is resolved against the data directory rather than
#: written down -- a descriptor from one drive applied to another would produce
#: confident, wrong layouts, and that is worse than UNKNOWN.
MSO_DESCRIPTOR_BIN = os.path.join(DATA_DIR, "msodescriptor.bin")

_DESC: dict = {}

#: Set when the descriptor was DERIVED rather than read, so `descriptor_status`
#: can tell the reader which of the three outcomes they are looking at: a cached
#: file, a rebuild that cost ~48 s of FTL mount, or a genuine absence.
_DESC_NOTE: str | None = None


def install_descriptor(buf: bytes | None, note: str | None = None) -> None:
    """Publish descriptor bytes the viewer derived for itself.

    `nandgui.sidecar` rebuilds the descriptor from the capture when no sidecar
    file exists, on a worker, because the FTL mount it needs measured 47.8 s.
    This is how the finished bytes reach the render path, which must never do
    filesystem or FTL work of its own.
    """
    global _DESC_NOTE
    _DESC[str(MSO_DESCRIPTOR_BIN)] = bytes(buf) if buf else None
    _DESC_NOTE = note


def descriptor_bytes(path: str | None = None) -> bytes | None:
    """-> this drive's msoDescriptor bytes, or None.  Cached, including misses.

    A miss is cached too: a viewer running without the sidecar must not stat a
    missing file on every aux render, and "no descriptor" is a stable fact for
    the life of the process.
    """
    key = str(path or MSO_DESCRIPTOR_BIN)
    if key not in _DESC:
        try:
            import pathlib

            p = pathlib.Path(key)
            _DESC[key] = p.read_bytes() if p.is_file() else None
        except Exception:                                    # noqa: BLE001
            _DESC[key] = None
    return _DESC[key]


def descriptor_status() -> str:
    """One sentence for the status bar / inspector about the layout source."""
    b = descriptor_bytes()
    if b:
        return _DESC_NOTE or (
            "msoDescriptor loaded (%d bytes) -- aux layouts come from this "
            "drive's own descriptor, byte +17 per owning MSO" % len(b))
    if _DESC_NOTE:
        return _DESC_NOTE
    return ("no msoDescriptor at %s and none derived yet -- aux layout reads "
            "UNKNOWN, which is the honest answer rather than a guessed 0"
            % MSO_DESCRIPTOR_BIN)


def aux_layout_of(owner_mso, ftl_or_descriptor=None):
    """(owning msoId, descriptor source) -> (layout int or None, name or None).

    `owner_mso` is summary.npz's `owner` column: an msoId, or -1 for "this
    block has no owning MSO".  `ftl_or_descriptor` is where the layout byte
    comes from -- an `ftl.Ftl`, the raw msoDescriptor bytes, or a Section.

    With no source the answer is (None, None), and that is the whole point.
    The layout lives at descriptor byte +17 (`aux.MSO_DESC_AUX_LAYOUT_OFF`) and
    is a per-drive configuration value; `aux.layout_for` refuses to fall back to
    a table of what some other board had, and this GUI carries no descriptor
    source today, so "layout UNKNOWN" is the MEASURED answer here, never 0.
    Layout 1 comes back as (1, None): the number is real and the missing name
    is a fact about the firmware, so the caller prints it as
    "1 (unnamed in this firmware)" -- see AUX_UNNAMED_NOTE -- and never as a
    name borrowed from a sibling layout.

    Never raises: aux.UNKNOWN is a falsey singleton that is not False, and
    aux.parse raises where the firmware halt()s.
    """
    if owner_mso is None:
        return (None, None)
    try:
        if isinstance(owner_mso, int) and owner_mso < 0:
            return (None, None)          # -1 == no owning MSO recorded
    except Exception:                                        # noqa: BLE001
        return (None, None)
    try:
        aux = _titania()["aux"]
    except Exception:                                        # noqa: BLE001
        return (None, None)
    if ftl_or_descriptor is None:
        # Fall back to the sidecar the capture carries. This is not the guess
        # aux.layout_for refuses to make: it is this drive's own msoDescriptor
        # bytes, lifted out of its Bootstrap RootStorage, so the layout still
        # comes from descriptor byte +17 of the owning MSO. With no sidecar the
        # answer stays (None, None) and the panel still says UNKNOWN.
        ftl_or_descriptor = descriptor_bytes()
    try:
        layout = aux.layout_for(owner_mso, source=ftl_or_descriptor)
    except Exception:                                        # noqa: BLE001
        return (None, None)
    # UNKNOWN by identity, before anything tries int() on it.
    if _is_unknown(layout) or isinstance(layout, bool):
        return (None, None)
    try:
        idx = int(layout)
    except Exception:                                        # noqa: BLE001
        return (None, None)
    names = getattr(aux, "LAYOUT_NAMES", _AUX_LAYOUT_NAMES)
    if idx not in names:
        return (None, None)
    name = names.get(idx)
    return (idx, str(name) if name else None)


def parse_aux(aux8: bytes, layout, au=None):
    """8 aux bytes + a layout -> a titania.auxdata.AuxEntry, or None.

    None whenever the layout is unknown, because there is no layout-free
    reading of these eight bytes: the same word is an LMA under layout 0 and a
    stripeId under layout 3, and picking one to display would be inventing the
    fact the descriptor was supposed to supply.
    """
    if layout is None or not aux8:
        return None
    try:
        aux = _titania()["aux"]
        return aux.parse(bytes(aux8), int(layout), au=au)
    except Exception:                                        # noqa: BLE001
        return None


@dataclasses.dataclass(frozen=True)
class PageFormat:
    """Everything an inspector needs to say about a page's geometry.

    Three questions in one object: which region this address is in, why (the
    rule, so the verdict is checkable from the address instead of trusted), and
    what will therefore be run over the bytes and at what width.  It carries no
    bytes and makes no claim about this particular page -- that is Outcome's
    job -- so it can be built before anything is read.
    """

    region: str
    rule: str                # the addressing rule that put the page here
    decoder: str             # which decoder decode_for_region() will call
    geometry: str            # "5 chunks x 4-byte digest" / "4 AUs x 6-byte MCRC"
    fmt: str                 # the codec format, as a display name
    mcrc_width: int          # bytes per MCRC/digest; 0 would mean none
    unit_count: int          # units per page: 4 AUs, or 5 chunks
    unit_noun: str           # singular: "AU" or "chunk"
    codec_applies: bool      # does the FORMAT_A codec path apply at all
    known: bool              # False for REGION_UNKNOWN -- nothing here is a claim

    @property
    def unit_plural(self) -> str:
        return self.unit_noun if self.unit_count == 1 else self.unit_noun + "s"

    @property
    def sentence(self) -> str:
        """The one-line version, identical to describe_region()."""
        return describe_region(self.region)


def region_info(region: str) -> PageFormat:
    """-> the PageFormat for an already-known region string.

    Built from the tables above rather than from a second copy of the facts, so
    a corrected rule or width changes both the sentence and the dataclass.
    """
    region = str(region or REGION_UNKNOWN)
    if region not in RULE:
        region = REGION_UNKNOWN
    return PageFormat(
        region=region,
        rule=RULE[region],
        decoder=DECODER[region],
        geometry=GEOMETRY[region],
        fmt=FORMAT_NAME[region],
        mcrc_width=MCRC_WIDTH[region],
        unit_count=UNIT_COUNT[region],
        unit_noun=UNIT_NOUN[region],
        codec_applies=needs_codec(region),
        known=region in REGIONS,
    )


def describe(ce, block) -> PageFormat:
    """(ce, block) -> the PageFormat for that address.  Asked, never derived.

    The CE is half of the group-2 test, so a caller that has only the block
    number gets REGION_UNKNOWN -- which claims nothing -- rather than `ftl`.
    """
    return region_info(region_of(ce, block))


def describe_region(region: str) -> str:
    """One sentence: the region, its addressing rule and its unit geometry."""
    region = str(region or REGION_UNKNOWN)
    return "%s -- %s  Pages here are %d %s%s, %d-byte MCRC (%s)." % (
        region, RULE.get(region, "no rule recorded"),
        UNIT_COUNT.get(region, 0), UNIT_NOUN.get(region, "unit"),
        "" if UNIT_COUNT.get(region, 0) == 1 else "s",
        MCRC_WIDTH.get(region, 0), FORMAT_NAME.get(region, "?"))


def needs_codec(region: str) -> bool:
    """Does this region's decoder need the FTL codec object?

    Group 2 does not: its verdict is seed-free and ECC-free, so a page there can
    be checked even when default_codec() cannot be built at all.
    """
    return str(region) != REGION_FW_GROUP2


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Unit:
    """One AU or one chunk, whichever this region divides pages into."""

    index: int
    noun: str                       # "AU" or "chunk"
    #: The MCRC/digest verdict for this unit.  None is a third value meaning no
    #: verdict exists (an erased chunk is not checked, and a codec that cannot
    #: check reports UNKNOWN); it is never rendered as a pass.
    ok: bool | None = None
    #: AU: the 4104 logical bytes, aux first.  Chunk: its 2048 plaintext
    #: payload bytes, or None when only the structural check ran.
    data: bytes | None = None
    #: Group 2 only: the 4 stored digest bytes, verbatim.
    digest: bytes | None = None
    erased: bool | None = None
    #: Group 2 only: short sha1 of the stored payload, which is how
    #: check_group2_page decides two chunks carry the same bytes.
    sha1: str = ""

    @property
    def label(self) -> str:
        return "%s%d" % (self.noun, self.index)


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What decode_for_region() found.  Never raised, always returned."""

    region: str
    decoder: str
    fmt: str
    mcrc_width: int
    unit_noun: str
    unit_count: int
    units: tuple[Unit, ...] = ()
    #: The 4 x 4104 AU plaintexts, for AU regions only.  None in group 2, where
    #: there are no AUs -- fabricating four out of five chunks is the lie this
    #: module exists to stop.
    aus: tuple[bytes, ...] | None = None
    #: Per-unit verdicts, or None for "no per-unit verdict exists" (UNKNOWN).
    mcrc: tuple | None = None
    #: The page roll-up derived from the per-unit verdicts, None when there are
    #: none to roll up.
    page_mcrc: bool | None = None
    mcrc_counts: tuple[int, int] | None = None
    #: Group 2: check_group2_page's own chunk dicts, verbatim, for a view that
    #: wants the raw fields rather than the Unit projection.
    chunks: tuple[dict, ...] | None = None
    digest_consistent: bool | None = None
    tail_ff: bool | None = None
    erased: bool | None = None
    #: Group 2: was the page the expected 18432 bytes?  A short read is a read
    #: failure, which is a different fact from a digest mismatch.
    size: bool | None = None
    #: The region checker's whole-page verdict, where it has one.  Broader than
    #: page_mcrc: it also covers the tail and digest consistency.
    region_ok: bool | None = None
    notes: tuple[str, ...] = ()
    error: str | None = None
    seconds: float = 0.0


def _blank(region: str, error: str | None = None,
           seconds: float = 0.0) -> Outcome:
    return Outcome(region=region, decoder=DECODER.get(region, ""),
                   fmt=FORMAT_NAME.get(region, ""),
                   mcrc_width=MCRC_WIDTH.get(region, 0),
                   unit_noun=UNIT_NOUN.get(region, "unit"),
                   unit_count=UNIT_COUNT.get(region, 0),
                   error=error, seconds=seconds)


def _is_unknown(value) -> bool:
    """True for a titania UNKNOWN sentinel.

    Both titania.ftl.UNKNOWN and titania.auxdata.UNKNOWN are falsey objects that
    raise on len() and iteration, so they must be recognised before anything
    tries to make a tuple of them.  They are not False: the third verdict is
    "we could not tell", and drawing that as a pass is the same class of error
    as drawing skipped media as erased.
    """
    if value is None:
        return True
    if isinstance(value, (tuple, list)):
        return False
    return type(value).__name__ in ("_Unknown", "Unknown")


# ---------------------------------------------------------------------------
# the AU regions: ftl and fw-group01
# ---------------------------------------------------------------------------
def _decode_aus(codec, raw, region, block, page, correct, t0) -> Outcome:
    if codec is None:
        return _blank(region, "no page codec available for the %s region"
                              % (region,), time.perf_counter() - t0)
    try:
        aus, mcrc = codec.decode(bytes(raw), int(block), int(page), lun=0,
                                 correct=correct)
    except Exception as exc:                                 # noqa: BLE001
        return _blank(region, "%s: %s" % (type(exc).__name__, exc),
                      time.perf_counter() - t0)
    out_aus = tuple(bytes(a) for a in aus) if aus is not None else None
    out_mcrc = None if _is_unknown(mcrc) else tuple(bool(x) for x in mcrc)
    n = len(out_aus) if out_aus is not None else (
        len(out_mcrc) if out_mcrc else 0)
    units = tuple(
        Unit(index=i, noun=UNIT_NOUN[region],
             ok=(out_mcrc[i] if out_mcrc is not None and i < len(out_mcrc)
                 else None),
             data=(out_aus[i] if out_aus is not None and i < len(out_aus)
                   else None))
        for i in range(n))
    page_mcrc = None if out_mcrc is None else all(out_mcrc)
    counts = (None if out_mcrc is None
              else (sum(1 for v in out_mcrc if v), len(out_mcrc)))
    return Outcome(
        region=region, decoder=DECODER[region], fmt=FORMAT_NAME[region],
        mcrc_width=MCRC_WIDTH[region], unit_noun=UNIT_NOUN[region],
        unit_count=UNIT_COUNT[region], units=units, aus=out_aus,
        mcrc=out_mcrc, page_mcrc=page_mcrc, mcrc_counts=counts,
        error=None if out_aus else "the codec returned no AUs for this page",
        seconds=time.perf_counter() - t0)


# ---------------------------------------------------------------------------
# fw-group2
# ---------------------------------------------------------------------------
_G2_CHUNK_PAYLOAD = 2048         # nd_g2extract.AU_PAYLOAD

#: One raw page as the media stores it, in every region.  Both decoders below
#: require exactly this; decode_for_region checks it once so neither has to.
_RAW_PAGE_BYTES = 18432


def _decode_group2(raw, block, page, t0) -> Outcome:
    region = REGION_FW_GROUP2
    try:
        mods = _titania()
        fw = mods["fw"]
    except Exception as exc:                                 # noqa: BLE001
        return _blank(region, "titania.fw is not reachable, so this group-2 "
                              "page could not be checked: %s: %s"
                              % (type(exc).__name__, exc),
                      time.perf_counter() - t0)
    try:
        chk = fw.check_group2_page(bytes(raw))
    except Exception as exc:                                 # noqa: BLE001
        return _blank(region, "%s: %s" % (type(exc).__name__, exc),
                      time.perf_counter() - t0)

    notes = [str(n) for n in (chk.get("notes") or ())]
    # The plaintext is a separate, weaker ask: it needs the row seed and the
    # scrambler.  If it fails the structural verdict above still stands, so the
    # failure is a note, never a lost verdict.
    plain = None
    try:
        row = mods["geometry"].compose_row(int(block), int(page), 0)
        plain, _rep = fw.decode_group2_page(bytes(raw), row)
    except Exception as exc:                                 # noqa: BLE001
        notes.append("plaintext not available (the digests above were still "
                     "checked, and they need no seed): %s: %s"
                     % (type(exc).__name__, exc))

    chunks = tuple(dict(c) for c in (chk.get("chunks") or ()))
    units = []
    for i, c in enumerate(chunks):
        lo = i * _G2_CHUNK_PAYLOAD
        data = (bytes(plain[lo:lo + _G2_CHUNK_PAYLOAD])
                if plain is not None and len(plain) >= lo + _G2_CHUNK_PAYLOAD
                else None)
        ok = c.get("mcrc_ok")
        units.append(Unit(index=i, noun="chunk",
                          ok=None if ok is None else bool(ok),
                          data=data,
                          digest=(bytes(c["digest"])
                                  if c.get("digest") is not None else None),
                          erased=(None if c.get("erased") is None
                                  else bool(c["erased"])),
                          sha1=str(c.get("message_sha1") or "")))

    counts = chk.get("mcrc")
    counts = (None if not isinstance(counts, (tuple, list))
              else (int(counts[0]), int(counts[1])))
    # The page roll-up is the MCRC roll-up only.  region_ok below is the wider
    # structural verdict (tail + digest consistency); keeping them apart is what
    # lets the panel say "5/5 digests match but the tail is not erased".
    checked = [u.ok for u in units if u.ok is not None]
    page_mcrc = all(checked) if checked else None
    return Outcome(
        region=region, decoder=DECODER[region], fmt=FORMAT_NAME[region],
        mcrc_width=MCRC_WIDTH[region], unit_noun="chunk",
        unit_count=len(units) or UNIT_COUNT[region],
        units=tuple(units),
        # No AUs exist here.  None, so Decoded.aux()/user() cannot slice
        # group-2 bytes as though they were 8 aux + 4096 user.
        aus=None,
        mcrc=tuple(u.ok for u in units) if units else None,
        page_mcrc=page_mcrc, mcrc_counts=counts, chunks=chunks,
        digest_consistent=(None if chk.get("digest_consistent") is None
                           else bool(chk["digest_consistent"])),
        tail_ff=(None if chk.get("tail_ff") is None
                 else bool(chk["tail_ff"])),
        erased=None if chk.get("erased") is None else bool(chk["erased"]),
        size=None if chk.get("size") is None else bool(chk["size"]),
        region_ok=None if chk.get("ok") is None else bool(chk["ok"]),
        notes=tuple(notes),
        error=None if units else "the group-2 checker returned no chunks",
        seconds=time.perf_counter() - t0)


# ---------------------------------------------------------------------------
# the dispatch
# ---------------------------------------------------------------------------
def decode_for_region(codec, raw, ce, block, page, correct=None,
                      region: str | None = None) -> Outcome:
    """Decode one raw 18432-byte page the way its region says to.

    `codec` is the FTL codec (model.get_codec) or None; group 2 does not use it.
    `region` may be passed when the caller already computed it, so the region a
    panel prints and the decoder that ran can never disagree.

    Never raises.  Every failure comes back in Outcome.error, because a codec
    exception must not cross into the frame loop -- and because "the decoder
    blew up" and "the media is bad" are different sentences.
    """
    t0 = time.perf_counter()
    if region is None:
        region = region_of(ce, block)
    region = str(region)
    if region not in RULE:
        region = REGION_UNKNOWN
    if raw is None:
        return _blank(region, "no bytes to decode", time.perf_counter() - t0)
    if len(raw) != _RAW_PAGE_BYTES:
        # Named here rather than left to the decoder: the group-2 checker
        # answers a short buffer with "no chunks" and the codec with a
        # ValueError, and neither of those sentences says why.  A truncated
        # record is a fact about the capture, not about the media.
        return _blank(region,
                      "this record is %d bytes; every decoder in this region "
                      "wants a full raw page of %d, so nothing was checked"
                      % (len(raw), _RAW_PAGE_BYTES),
                      time.perf_counter() - t0)
    if region == REGION_FW_GROUP2:
        return _decode_group2(raw, block, page, t0)
    # ftl, fw-group01 and the unknown fallback are all 4 AUs x (8 aux + 4096
    # user) behind a 6-byte MCRC -- MEASURED on this capture, including
    # ce1/b6/p0 and ce1/b8/p0 at 4/4.  Group 0/1 gets its own label, not its own
    # arithmetic.
    return _decode_aus(codec, raw, region, block, page, correct, t0)


# ===========================================================================
# ECC -- the verdict the MCRC is not
# ===========================================================================
# THE MCRC AND THE ECC ANSWER DIFFERENT QUESTIONS AND THIS VIEWER USED TO SHOW
# Only one of them.  The MCRC is a CRC over one unit's stored message; the ECC
# is an LDPC decode of the codewords that message is carried in.  A page can
#
#   * fail MCRC while every codeword is a valid codeword (the flip is in LDPC
#     PARITY, which the MCRC's message does not cover -- measured: 5 of the 10
#     dirty group-2 codewords on this drive are exactly that, and they pass
#     check_group2_page at 5/5 while carrying an uncorrected bit), or
#   * have codeword 5 alone fail while the panel reports all four AUs failing
#     (titania_nand.decode_page copies one page-level all() onto every AU), or
#   * be uncorrectable with the MCRC never meaningfully reached at all.
#
# So "MCRC FAIL" is not an ECC verdict and "ECC failed" is not an MCRC verdict.
# Both are reported, side by side, and neither is ever derived from the other.
#
# MEASURED on the reference capture, and these numbers are
# what the state names below mean:
#
#   pseudo-SLC ftl   412 pages sampled: 8 syndromes of 0 on 400/400, 0 bits
#                    changed by correction, MCRC 4/4.  0 failures in the 50,312
#                    decoded pages of that class drive-wide.
#   native TLC ftl   248 failing pages: the 29 with max syndrome 1..799 all
#                    converged 8/8 and reached MCRC 4/4 (1..548 flips); the 219
#                    with max syndrome >= 800 converged on 0 of 8 codewords and
#                    changed 0 bits.  The corrector's measured waterfall is
#                    ~160-170 bad bits per 2304-byte codeword (0.87-0.92% raw
#                    BER) and those pages imply 1.8-4.3%, i.e. 2-4x past it.
#                    They need a re-READ with the retry ladder, not a decoder:
#                    15 of 15 such addresses that have a retry capture decode
#                    clean from it.
#   fw-group2        36,800 codewords, 10 dirty, every one of syndrome weight
#                    exactly 6 == a single bit error, all 10 fixed by the column
#                    lookup below and verified three independent ways.
#   all-0x00         53,736 pages in 74 blocks.  The zero vector is a codeword
#                    under the deep convention, so its syndromes are 0 by
#                    construction and carry no information whatsoever.  ECC can
#                    neither succeed nor fail there, which is why ZERO_PAGE is
#                    its own state and why the fill is tested before any decoder
#                    runs.
#
# The complement trap.  titania_nand.ecc_correct_page() calls
# nd_deepparity.normalize() and discards its `was_inverted` flag
# (titania_nand.py:415, `norm, _inv = _dp.normalize(...)`), so when
# is_inverted() fires the corrected page comes back bitwise complemented and is
# never complemented back.  Measured: that happens on 74 of 170 failing TLC
# pages (43.5%) and on 180 of 180 all-0x00 pages.  is_inverted()'s own docstring
# promises a ~720-bit margin and a healthy page here measures exactly 720; on
# corrupted pages the margin collapses to 2..64, i.e. a coin flip.  So nothing
# in this module ever calls ecc_correct_page: it decodes the page as read,
# reports the margin, and honours a deep verdict only at ECC_DEEP_MARGIN.
ECC_DEEP_MARGIN = 200

#: The corrector's measured capability, for the panel.  From nd_ldpc_fix's own
#: table (200-300 trials per point on real codewords), reproduced by injection
#: on this drive's pages: 1..128 bad bits 100% exact, 160 99%, 168 90.7%,
#: 172 65.7%, 192 0%.
ECC_CAPABILITY = ("EE3 corrects >99% of codewords up to ~160 bad bits per "
                  "2304 bytes (0.87% raw BER) and 0% by 192 (1.04%). A10R "
                  "(group 2) holds to ~240 bits per 816-byte frame (3.68%). "
                  "Neither miscorrected once in ~6500 measured decodes, so a "
                  "failure to converge is honest -- it is not silent garbage.")


class EccState(enum.IntEnum):
    """The ECC outcomes, and they are never merged.

    NOT_MEASURED is the drive map's "nobody has run the decoder over this block
    yet" and must never be drawn as a pass; NOT_ATTEMPTED is per page and means
    the page carries no bytes to decode; UNAVAILABLE means the DECODER could not
    be reached, which is a fact about this machine and not about the media.
    """

    NOT_MEASURED = 0     # nobody ran ECC here (drive map default)
    NOT_ATTEMPTED = 1    # this page carries no bytes / no decode was asked for
    UNAVAILABLE = 2      # the LDPC modules could not be reached
    ZERO_PAGE = 3        # all 0x00: the zero vector is a codeword -- N/A
    ERASED = 4           # all 0xFF: not data
    CLEAN = 5            # every syndrome 0 before correction; nothing to do
    CORRECTED = 6        # converged, n bits flipped, and the MCRC agrees
    PARTIAL = 7          # some codewords converged, some did not
    UNCORRECTABLE = 8    # no codeword converged; 0 bits changed
    MCRC_DISAGREES = 9   # converged, MCRC still fails -- a red flag


ECC_STATES: tuple[EccState, ...] = tuple(EccState)

#: The short word the map legend and the inspector headline use.
ECC_SHORT = {
    EccState.NOT_MEASURED: "not measured",
    EccState.NOT_ATTEMPTED: "not attempted",
    EccState.UNAVAILABLE: "decoder unavailable",
    EccState.ZERO_PAGE: "N/A -- all 0x00",
    EccState.ERASED: "N/A -- erased",
    EccState.CLEAN: "clean (syndrome 0)",
    EccState.CORRECTED: "corrected",
    EccState.PARTIAL: "partly corrected",
    EccState.UNCORRECTABLE: "correction FAILED",
    EccState.MCRC_DISAGREES: "converged, MCRC disagrees",
}

#: The headline, in words, for the inspector.
ECC_LABEL = {
    EccState.NOT_MEASURED: "ECC NOT MEASURED",
    EccState.NOT_ATTEMPTED: "ECC NOT ATTEMPTED",
    EccState.UNAVAILABLE: "ECC DECODER UNAVAILABLE",
    EccState.ZERO_PAGE: "ECC DOES NOT APPLY -- all-0x00 page",
    EccState.ERASED: "ECC DOES NOT APPLY -- erased page",
    EccState.CLEAN: "ECC CLEAN -- nothing to correct",
    EccState.CORRECTED: "ECC CORRECTED",
    EccState.PARTIAL: "ECC PARTIAL -- some codewords did not converge",
    EccState.UNCORRECTABLE: "ECC CORRECTION FAILED",
    EccState.MCRC_DISAGREES: "ECC CONVERGED, MCRC DISAGREES",
}

#: The paragraph under the headline.  Every number in here is measured on this
#: capture; the survey at the top of this section is where they come from.
ECC_SENTENCE = {
    EccState.NOT_MEASURED:
        "No ECC decode has been run for this block. This is the absence of a "
        "measurement -- NOT a pass and NOT a failure. ECC is a per-page decode "
        "(4-8 ms on a clean page, up to ~95 ms on a bad one), so the drive map "
        "shows only what has actually been measured.",
    EccState.NOT_ATTEMPTED:
        "No ECC decode was run: this page carries no bytes (absent, erased, "
        "skipped or not probed yet), or no decode was requested. Absence of a "
        "decode is not a failed decode.",
    EccState.UNAVAILABLE:
        "The LDPC modules could not be reached, so no ECC verdict exists for "
        "this page. That is a fact about this machine -- titania is not "
        "installed, or the share it lives on is unreachable -- and not about "
        "the media.",
    EccState.ZERO_PAGE:
        "The page is 18432 bytes of 0x00. Under the deep convention the "
        "all-zero page IS the zero codeword, so all eight syndromes are 0 BY "
        "CONSTRUCTION and carry no information: ECC can neither succeed nor "
        "fail here. Measured drive-wide: 53,736 such pages in 74 blocks, and "
        "no page anywhere else on the drive has even one all-zero AU. This is "
        "a bus constant, NOT erased media -- and it is exactly the page "
        "titania_nand.ecc_correct_page() rewrites into 18432 bytes of 0xFF "
        "while reporting converged=True.",
    EccState.ERASED:
        "The page is 18432 bytes of 0xFF: blank media, not data. It "
        "complements to the zero codeword and would 'converge' with syndrome "
        "0, so no ECC verdict is claimed for it.",
    EccState.CLEAN:
        "Every codeword of this page was already a valid codeword before any "
        "correction: all syndromes are 0. Nothing was flipped and nothing "
        "needed to be. This is the only state in which 'ECC ok' means the "
        "decoder had nothing to do.",
    EccState.CORRECTED:
        "At least one codeword had a nonzero syndrome, the decoder converged "
        "on ALL of them, and the MCRC agrees afterwards. The flip count below "
        "is how many bits the decoder changed; the raw bytes it started from "
        "are still shown, unaltered, under 'Raw page'.",
    EccState.PARTIAL:
        "Some codewords converged and some did not, so this page is PARTLY "
        "corrected: the units behind the converged codewords are repaired and "
        "the rest are exactly as read. It is neither a clean page nor an "
        "untouched one, and merging it into either would hide which half is "
        "which.",
    EccState.UNCORRECTABLE:
        "The decoder ran on every codeword and converged on none of them. "
        "Measured on 219 such pages: 0 bits changed -- so 'nothing changed' "
        "here is honest non-convergence and NOT a decoder that failed to "
        "engage. The implied raw bit-error rate is 1.8-4.3% against a measured "
        "waterfall at 0.92%, so no hard-decision decoder can reach these "
        "bytes. The routes are a re-READ with the retry ladder (15 of 15 such "
        "addresses that have a retry capture decode clean from it), soft or "
        "multi-read combining, or raid.rebuild_page where the WERU shape "
        "allows it. The bytes are shown below regardless: a page that cannot "
        "be corrected is still a page you can look at.",
    EccState.MCRC_DISAGREES:
        "RED FLAG. Every codeword converged, yet an MCRC still fails. This "
        "combination has never been produced on this drive: 0 of 248 failing "
        "pages, 0 of 264 injection trials, 0 of 36,800 group-2 codewords. The "
        "MCRC's message is a subset of the codeword, so there is no known "
        "mechanism for it -- read this as a bug in the decode path (or a "
        "genuinely new phenomenon worth chasing), not as a verdict about the "
        "media.",
}

#: Worst-first.  A block's colour is the worst state any of its measured pages
#: reached, so nothing rare hides behind a common neighbour -- the same
#: precedence rule the CONTENT mode uses for page states.
ECC_SEVERITY = (
    EccState.MCRC_DISAGREES,
    EccState.UNCORRECTABLE,
    EccState.PARTIAL,
    EccState.CORRECTED,
    EccState.UNAVAILABLE,
    EccState.CLEAN,
    EccState.ZERO_PAGE,
    EccState.ERASED,
    EccState.NOT_ATTEMPTED,
    EccState.NOT_MEASURED,
)
_SEVERITY_RANK = {s: i for i, s in enumerate(ECC_SEVERITY)}


def worse_ecc(a, b) -> "EccState":
    """-> whichever of two EccStates is the more serious.

    A block is rolled up from its pages with this, so one uncorrectable page in
    a thousand clean ones still decides the colour: an average would hide it.
    """
    sa, sb = EccState(int(a)), EccState(int(b))
    ra = _SEVERITY_RANK.get(sa, len(ECC_SEVERITY))
    rb = _SEVERITY_RANK.get(sb, len(ECC_SEVERITY))
    return sa if ra <= rb else sb


#: region -> which ECC code its pages are actually protected by, named.
ECC_CODE = {
    REGION_FW_GROUP2:
        "A10R QC-LDPC (Z=48, 816-byte frame, 300 B parity, rate 0.632), 20 "
        "codewords per page: 5 chunks x 4 slots. Its parity covers the "
        "SCRAMBLED bytes, so a raw read is already a codeword and checking it "
        "needs no seed.",
    REGION_FW_GROUP01:
        "EE3 QC-LDPC (Z=48, base 41x385, 246 B parity per 2304-byte codeword, "
        "rate 0.894), 8 codewords per page: 4 AUs x (even + odd). Its parity "
        "covers the DESCRAMBLED bytes.",
    REGION_FTL:
        "EE3 QC-LDPC (Z=48, base 41x385, 246 B parity per 2304-byte codeword, "
        "rate 0.894), 8 codewords per page: 4 AUs x (even + odd). Its parity "
        "covers the DESCRAMBLED bytes.",
    REGION_UNKNOWN:
        "EE3 assumed -- the region could not be computed, so nothing here is a "
        "region verdict.",
}

#: Which unit a codeword index belongs to, for the per-codeword table.  EE3:
#: codeword 2i is AU i's even block, 2i+1 its odd container (b0[2302:2304] ++
#: b1 -- not a 2304-byte slice, which is the mistake that makes healthy pages
#: look like noise).
ECC_UNIT_OF_CW = {
    REGION_FW_GROUP2: lambda i: "chunk %d slot %d" % (i // 4, i % 4),
    REGION_FW_GROUP01: lambda i: "AU %d %s" % (i // 2,
                                               "even" if i % 2 == 0 else "odd"),
    REGION_FTL: lambda i: "AU %d %s" % (i // 2,
                                        "even" if i % 2 == 0 else "odd"),
    REGION_UNKNOWN: lambda i: "AU %d %s" % (i // 2,
                                            "even" if i % 2 == 0 else "odd"),
}


@dataclasses.dataclass(frozen=True)
class EccFix:
    """One bit the decoder changed, where that is knowable per bit.

    Only the group-2 single-bit lookup can name individual bits: its syndrome
    identifies them uniquely.  EE3's iterative decoder reports flip counts per
    codeword, so this list is empty there and `flips` carries the count.
    """

    codeword: int
    unit: str                  # "chunk 2 slot 1"
    bit: int                   # bit index inside that codeword's frame
    page_offset: int           # byte offset in the 18432-byte page, -1 if none
    zone: str                  # payload / parity / MCRC field / virtual


@dataclasses.dataclass(frozen=True)
class EccResult:
    """One page's ECC verdict, with everything it was derived from.

    Nothing here is rolled up into anything else.  `syndromes` are the weights
    before any correction; `converged` and `flips` are per CODEWORD and are
    never all()-collapsed the way titania_nand.decode_page collapses them onto
    every AU; `mcrc_before` / `mcrc_after` are the MCRC's separate opinion at
    the two ends of the decode.
    """

    state: EccState
    region: str
    code: str = ""                       # ECC_CODE[region]
    n_codewords: int = 0
    syndromes: tuple[int, ...] = ()      # weight per codeword, before correction
    converged: tuple[bool | None, ...] = ()
    flips: tuple[int, ...] = ()          # bits changed per codeword
    units: tuple[str, ...] = ()          # which unit each codeword belongs to
    fixes: tuple[EccFix, ...] = ()       # named bits, where they are knowable
    #: The MCRC verdicts, per unit, before and after correction.  A None entry
    #: is "not checked" (an erased unit) -- a third answer, never a pass.
    mcrc_before: tuple[bool | None, ...] = ()
    mcrc_after: tuple[bool | None, ...] = ()
    #: The corrected 18432-byte page, when a correction was actually applied.
    #: None when nothing changed, so a caller can tell "the corrected page is
    #: the raw page" from "there is no corrected page".
    page: bytes | None = None
    corrected_bits: int = 0
    #: is_inverted()'s verdict and the margin it was decided on.  The margin is
    #: reported because the verdict is only trustworthy near 720 -- see
    #: ECC_DEEP_MARGIN and the complement trap above.
    inverted: bool | None = None
    inv_margin: int = -1
    syn_raw: int = -1
    syn_cpl: int = -1
    #: True when the page really was decoded in the complemented (deep)
    #: convention, which on this drive has never yet been the right answer.
    deep: bool = False
    notes: tuple[str, ...] = ()
    error: str | None = None
    seconds: float = 0.0

    @property
    def n_converged(self) -> int:
        return sum(1 for v in self.converged if v)

    @property
    def max_syndrome(self) -> int:
        return max(self.syndromes) if self.syndromes else 0

    @property
    def attempted(self) -> bool:
        """Did a decoder actually run over these bytes?"""
        return self.state in (EccState.CLEAN, EccState.CORRECTED,
                              EccState.PARTIAL, EccState.UNCORRECTABLE,
                              EccState.MCRC_DISAGREES)

    @property
    def label(self) -> str:
        return ECC_LABEL[self.state]

    @property
    def short(self) -> str:
        return ECC_SHORT[self.state]

    @property
    def sentence(self) -> str:
        return ECC_SENTENCE[self.state]


def describe_ecc(ecc: "EccResult | None") -> str:
    """One line for a status bar or a hover: the state, then its evidence."""
    if ecc is None:
        return "ECC: %s" % ECC_SHORT[EccState.NOT_MEASURED]
    bits = ["ECC: %s" % ecc.short]
    if ecc.syndromes:
        bits.append("syndrome weights %d..%d over %d codewords"
                    % (min(ecc.syndromes), max(ecc.syndromes),
                       len(ecc.syndromes)))
    if ecc.attempted:
        bits.append("converged %d/%d" % (ecc.n_converged, ecc.n_codewords))
    if ecc.corrected_bits:
        bits.append("%d bits flipped" % ecc.corrected_bits)
    if ecc.error:
        bits.append(str(ecc.error))
    return "  ".join(bits)


# ---------------------------------------------------------------------------
# the ECC modules, imported lazily and separately from the decode ones
# ---------------------------------------------------------------------------
_ECC: dict = {}

#: The one rust LDPC PIPE.  nd_deepparity.decode_page ends up in
#: nd_ldpc_rs.decode_many, which is a single persistent subprocess driven by an
#: unsynchronised `p.stdin.write` / `p.stdout.read` protocol
#: (titania/nd_ldpc_rs.py:211).  worker.py now runs N threads over N Stores, so
#: without this lock two of them interleave on that pipe.
#:
#: MEASURED: 4 threads on the shared unlocked pipe hung for over 10 minutes and
#: had to be killed.  And a hang is the lucky outcome -- two threads reading
#: each other's 6-byte headers would assemble a page from another page's
#: corrected codewords with converged=True, which then fails the MCRC and is
#: reported as MCRC_DISAGREES: the one ECC state this project documents as
#: "never observed; treat as a bug".  A concurrency defect that manufactures the
#: red-flag state is the worst failure available here, so this lock is not an
#: optimisation choice, it is a correctness requirement.
#:
#: It is held around `dp.decode_page` only.  page_syndromes_norm, syn_even_*
#: and _mcrc_of_page stay outside it deliberately: they are in-process numpy
#: (~14 ms of the ~40 ms mean page) and that is the part threads can overlap.
#: Measured split of a mean EE3 page: 3.0 ms inversion test + 11.0 ms syndromes
#: (numpy, overlappable) + 21.2 ms decode_page (this lock).
_ECC_LOCK = threading.Lock()

#: The lazy module handles (_ECC).  Taken on every call and released
#: immediately -- an uncontended lock is tens of nanoseconds against a decode
#: measured in tens of milliseconds, so there is no fast path to protect and
#: therefore no window to get wrong.
#:
#: There is something to get wrong.  The old shape was `if not _ECC:` around a
#: multi-key `_ECC.update(dp=, tn=)`.  Under worker.py's pool a third thread
#: can arrive between the two key writes, see a truthy dict, and return a table
#: with the second key missing -- a KeyError in the middle of a decode rather
#: than merely duplicated work.
_MODS_LOCK = threading.Lock()

#: The A10R matrix build (nd_g2ldpc_a10r's unguarded `global _H` / `global
#: _OFF`, plus _A10R_TABLES).  A separate lock from _MODS_LOCK because H() is a
#: 2400x6528 python loop that takes seconds: holding one lock across both would
#: let the first group-2 page stall every EE3 decode on every other worker for
#: the duration.
#:
#: Lock order, so all three stay deadlock-free: _A10R_LOCK may be held while
#: _MODS_LOCK is taken (_build_a10r_tables -> _a10r), and never the reverse --
#: _ecc_mods() and _a10r() both release _MODS_LOCK before they return.
#: _ECC_LOCK wraps one leaf call, dp.decode_page, and is never held while
#: either of the others is taken, nor they while it is.
_A10R_LOCK = threading.Lock()


def _ecc_mods() -> dict:
    """-> {'dp', 'tn'} for EE3, plus 'a10r' on demand.  Raises on failure.

    Separate from _titania() because these are the heavy ones -- they derive
    the LDPC matrices -- and a session that never opens the ECC panel should
    never pay for them.
    """
    with _MODS_LOCK:
        if not _ECC:
            ensure_titania_on_path()
            from titania import nd_deepparity as _dp
            from titania import titania_nand as _tn
            _ECC.update(dp=_dp, tn=_tn)
        return _ECC


def _a10r():
    """The A10R module object.  Import only -- its matrices are built in
    _a10r_tables(), under the other lock.

    The A10R path touches no Rust worker at all (checked: nd_g2ldpc_a10r
    imports only sys/pathlib/numpy plus its own json tables), so it needs
    _ECC_LOCK nowhere.
    """
    mods = _ecc_mods()
    with _MODS_LOCK:
        if "a10r" not in mods:
            from titania import nd_g2ldpc_a10r as _a
            mods["a10r"] = _a
        return mods["a10r"]


def _ecc_blank(state: EccState, region: str, t0: float,
               error: str | None = None, notes=()) -> EccResult:
    return EccResult(state=state, region=region, code=ECC_CODE.get(region, ""),
                     error=error, notes=tuple(notes),
                     seconds=time.perf_counter() - t0)


def _fill(raw: bytes, value: int) -> bool:
    a = np.frombuffer(raw, dtype=np.uint8)
    return bool(a.size) and bool((a == value).all())


# ---------------------------------------------------------------------------
# EE3: the ftl and fw-group01 regions
# ---------------------------------------------------------------------------
_AU_STRIDE = 4608                # titania_nand.AU_STRIDE
_MSG_BYTES = 4104                # titania_nand.MSG_BYTES


_RUST_WORKER = None


def _rust_worker():
    """`titania.nd_ldpc_rs` if it has a worker, else None.  Probed once.

    Cached for the same reason `nd_ldpc_odd._rust` is: an in-function
    `from titania import ...` stats the filesystem on every call to decide
    whether the name is a submodule, and this checkout is reached over a
    network path.  It measured 82% of a page's prepare.
    """
    global _RUST_WORKER
    if _RUST_WORKER is None:
        try:
            from titania import nd_ldpc_rs
            _RUST_WORKER = nd_ldpc_rs if nd_ldpc_rs.available() else False
        except Exception:                                    # noqa: BLE001
            _RUST_WORKER = False
    return _RUST_WORKER or None


def _mcrc_of_page(tn, page: bytes, width: int) -> tuple[bool | None, ...]:
    """Per-AU MCRC over a stored page.  None where the AU is erased.

    Its own function, and it never looks at a syndrome: the MCRC is an
    independent opinion, and the whole point of this section is that the two
    verdicts are reported separately.
    """
    out: list[bool | None] = [None] * 4
    msgs: list[bytes] = []
    slots: list[int] = []
    stored: list[bytes] = []
    for i in range(4):
        au = bytes(page[i * _AU_STRIDE:(i + 1) * _AU_STRIDE])
        if len(au) != _AU_STRIDE:
            continue
        try:
            if tn.au_is_erased(au):
                continue
            lg = tn.au_logical(au)
        except Exception:                                    # noqa: BLE001
            continue
        msgs.append(bytes(lg[:_MSG_BYTES]))
        stored.append(bytes(lg[_MSG_BYTES:_MSG_BYTES + width]))
        slots.append(i)
    if not msgs:
        return tuple(out)
    # All four AUs' CRCs in one call.  The de-interleave above is 2 us an AU and
    # stays here; the CRC is 1041 us an AU and does not -- it is a byte-at-a-time
    # loop over 4104 bytes, which is interpreter-bound and was the last such loop
    # on this path.  `nd_ldpc_rs.au_mcrcs` complements the message itself, as
    # `titania_nand.au_mcrc` does.
    want: list[bytes] | None = None
    rs = _rust_worker()
    if rs is not None:
        try:
            want = rs.au_mcrcs(msgs, width)
        except Exception:                                    # noqa: BLE001
            want = None
    if want is None:
        try:
            want = [tn.au_mcrc(m, width) for m in msgs]
        except Exception:                                    # noqa: BLE001
            return tuple(out)
    for i, got, exp in zip(slots, stored, want):
        out[i] = bytes(got) == bytes(exp)
    return tuple(out)


def _ee3_prepare(raw: bytes, region: str, t0: float):
    """Everything before the corrector.  -> (EccResult, None) | (None, pending)

    Split out so a batch of pages can share ONE call into the corrector.  The
    first element is a finished result for every page that never reaches the
    decoder -- unavailable modules, an unreadable syndrome, or an already-clean
    page -- and the second is the state `_ee3_finish` needs for the rest.

    Everything here is numpy in this process and holds no lock, so it overlaps
    freely across worker threads; the decoder is the only serialised part.
    """
    try:
        mods = _ecc_mods()
    except Exception as exc:                                 # noqa: BLE001
        return _ecc_blank(EccState.UNAVAILABLE, region, t0,
                          "the LDPC modules could not be imported: %s: %s"
                          % (type(exc).__name__, exc)), None
    dp, tn = mods["dp"], mods["tn"]
    a = np.frombuffer(bytes(raw), dtype=np.uint8)
    units = tuple(ECC_UNIT_OF_CW[region](i) for i in range(8))
    notes: list[str] = []

    # --- the inversion question, asked out loud --------------------------
    try:
        syn_raw = int(dp.syn_even_raw(a[:2304]))
        syn_cpl = int(dp.syn_even_cpl(a[:2304]))
    except Exception as exc:                                 # noqa: BLE001
        return _ecc_blank(EccState.UNAVAILABLE, region, t0,
                          "the EE3 syndrome could not be computed: %s: %s"
                          % (type(exc).__name__, exc)), None
    inverted = syn_raw < syn_cpl
    margin = abs(syn_raw - syn_cpl)
    deep = bool(inverted and margin >= ECC_DEEP_MARGIN)
    if inverted and not deep:
        notes.append(
            "is_inverted() says this page reads bit-INVERTED, but only by %d "
            "syndrome bits on codeword 0 -- its own docstring guarantees ~720 "
            "and every healthy page here measures exactly that. At this margin "
            "the verdict is NOT trusted and the page was decoded as read. "
            "titania_nand.ecc_correct_page() would have complemented it here "
            "and never complemented it back (titania_nand.py:415 discards "
            "normalize()'s flag), which is what turns an uncorrectable page "
            "into 18432 bytes of plausible-looking garbage." % (margin,))
    work = ((~a) & 0xFF).astype(np.uint8) if deep else a
    if deep:
        notes.append(
            "This page reads bit-INVERTED at a %d-bit margin (a real deep "
            "page), so it was complemented before decoding and the correction "
            "below is in that convention. Descrambling it needs the deep seed "
            "(seed ^ 0x1FFFFFFFFFFFFF, nd_deepparity.deep_seed)." % (margin,))

    # --- syndromes before any correction ---------------------------------
    try:
        syndromes = tuple(int(s) for s in dp.page_syndromes_norm(work))
    except Exception as exc:                                 # noqa: BLE001
        return _ecc_blank(EccState.UNAVAILABLE, region, t0,
                          "the EE3 page syndromes could not be computed: "
                          "%s: %s" % (type(exc).__name__, exc)), None

    mcrc_before = _mcrc_of_page(tn, bytes(work), 6)
    common = dict(region=region, code=ECC_CODE.get(region, ""), n_codewords=8,
                  syndromes=syndromes, units=units, mcrc_before=mcrc_before,
                  inverted=inverted, inv_margin=margin, syn_raw=syn_raw,
                  syn_cpl=syn_cpl, deep=deep)

    if max(syndromes) == 0:
        # Already a valid codeword on all eight.  The corrector is not run at
        # all: there is nothing for it to do, and reporting "converged" about a
        # decode nobody needed would merge CLEAN into CORRECTED.
        return EccResult(state=EccState.CLEAN, converged=(True,) * 8,
                         flips=(0,) * 8, mcrc_after=mcrc_before,
                         corrected_bits=0, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common), None

    return None, {"work": work, "tn": tn, "notes": notes, "common": common,
                  "mcrc_before": mcrc_before, "t0": t0}


def _ee3_failed(pending: dict, exc: BaseException) -> EccResult:
    """The corrector raised.  A fact about this machine, not about the media."""
    return EccResult(state=EccState.UNAVAILABLE,
                     error="the EE3 corrector raised: %s: %s"
                           % (type(exc).__name__, exc),
                     notes=tuple(pending["notes"]),
                     seconds=time.perf_counter() - pending["t0"],
                     **pending["common"])


def _ee3_finish(pending: dict, fixed, log) -> EccResult:
    """Everything after the corrector, given what it returned."""
    tn = pending["tn"]
    notes = pending["notes"]
    common = pending["common"]
    mcrc_before = pending["mcrc_before"]
    t0 = pending["t0"]
    converged = tuple(bool(o) for _c, _n, o in log)
    flips = tuple(int(n) for _c, n, _o in log)
    total = sum(flips)
    fixed_bytes = np.asarray(fixed, dtype=np.uint8).tobytes()
    n_conv = sum(1 for v in converged if v)

    if n_conv == 0:
        notes.append(
            "0 of 8 codewords converged and %d bits changed. The decoder ran "
            "and honestly failed: nd_deepparity returns the ORIGINAL block for "
            "a codeword it could not decode, so 'nothing changed' is what "
            "non-convergence looks like, not a decoder that never engaged."
            % (total,))
        return EccResult(state=EccState.UNCORRECTABLE, converged=converged,
                         flips=flips, mcrc_after=mcrc_before,
                         corrected_bits=total, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)

    mcrc_after = _mcrc_of_page(tn, fixed_bytes, 6)
    if n_conv < 8:
        notes.append(
            "%d of 8 codewords converged. The AUs behind the converged "
            "codewords are corrected; the others are exactly as read."
            % (n_conv,))
        return EccResult(state=EccState.PARTIAL, converged=converged,
                         flips=flips, mcrc_after=mcrc_after, page=fixed_bytes,
                         corrected_bits=total, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)

    checked = [v for v in mcrc_after if v is not None]
    if checked and not all(checked):
        return EccResult(state=EccState.MCRC_DISAGREES, converged=converged,
                         flips=flips, mcrc_after=mcrc_after, page=fixed_bytes,
                         corrected_bits=total, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)
    return EccResult(state=EccState.CORRECTED, converged=converged,
                     flips=flips, mcrc_after=mcrc_after, page=fixed_bytes,
                     corrected_bits=total, notes=tuple(notes),
                     seconds=time.perf_counter() - t0, **common)


def _ecc_ee3(raw: bytes, region: str, t0: float) -> EccResult:
    """The EE3 path for ONE page: syndromes, then the corrector, then the MCRC.

    It calls nd_deepparity directly and never titania_nand.ecc_correct_page --
    see the complement trap at the top of this section -- and it keeps the
    per-codeword log whole.

    `_ECC_LOCK` covers the corrector call and nothing else: everything either
    side of it is numpy in this process and overlaps freely across workers.
    """
    done, pending = _ee3_prepare(raw, region, t0)
    if done is not None:
        return done
    try:
        with _ECC_LOCK:
            fixed, log = _ecc_mods()["dp"].decode_page(pending["work"])
    except Exception as exc:                                 # noqa: BLE001
        return _ee3_failed(pending, exc)
    return _ee3_finish(pending, fixed, log)


# ---------------------------------------------------------------------------
# A10R: the fw-group2 region
# ---------------------------------------------------------------------------
# The dense `H() @ b` in nd_g2ldpc_a10r costs ~10 ms per codeword, i.e. ~200 ms
# for one page.  Every column of H has weight exactly 6 and all 6528 column
# patterns are distinct (measured), so the syndrome is the XOR of the columns
# under the set bits -- a gather -- and a syndrome of weight 6 that matches a
# column identifies the flipped bit uniquely, by lookup, with no iteration and
# no search.  Both facts are properties of the matrix, so the tables below are
# built from H() itself and asserted, never carried as a stored copy.
_A10R_TABLES: dict = {}

_A10R_SLOT_STRIDE = 812
_A10R_AU_BYTES = 3252


def _build_a10r_tables() -> dict:
    """The derivation itself.  The caching and its lock live below it.

    It also forces nd_g2ldpc_a10r's own two lazy globals -- `_H` via H() and
    `_OFF` via offsets() -- while the lock is held, because _a10r_syndrome()
    calls mod.offsets() once per codeword (20 per group-2 page) and those
    module globals are written with a bare `global` and no guard of their own.
    Two workers building them concurrently is waste rather than corruption (the
    values are identical), but the waste is seconds of a python triple loop,
    twice.
    """
    mod = _a10r()
    h = np.asarray(mod.H(), dtype=np.uint8)
    mod.offsets()
    col_idx, row_idx = np.nonzero(h.T == 1)          # column-major order
    per = np.bincount(col_idx, minlength=h.shape[1])
    if per.min() != 6 or per.max() != 6:
        raise RuntimeError(
            "A10R H no longer has column weight 6 (%d..%d); the single-bit "
            "lookup is only valid at weight 6" % (per.min(), per.max()))
    table = row_idx.reshape(-1, 6).astype(np.int32)
    lut = {tuple(int(v) for v in table[j]): int(j)
           for j in range(table.shape[0])}
    if len(lut) != table.shape[0]:
        raise RuntimeError(
            "A10R column patterns are not distinct, so a weight-6 syndrome "
            "no longer identifies exactly one bit")
    return {"cols": table, "lut": lut}


def _a10r_tables() -> dict:
    """-> {'cols': int32[6528, 6], 'lut': {pattern -> column}}.  Cached.

    The lock is taken on every call, with no unlocked fast path, for the same
    reason as _ecc_mods(): the old shape was `if not _A10R_TABLES:` around a
    multi-key `.update(cols=, lut=)`, and under the pool a thread arriving
    between the two key writes gets a truthy dict whose 'lut' does not exist
    yet and dies on a KeyError inside a decode.  Once built this costs one
    uncontended acquire per group-2 page against a decode measured in
    milliseconds.
    """
    with _A10R_LOCK:
        if not _A10R_TABLES:
            _A10R_TABLES.update(_build_a10r_tables())
        return _A10R_TABLES


def _a10r_syndrome(mod, tables, frame816, slot: int) -> np.ndarray:
    bits = np.unpackbits(np.asarray(frame816, dtype=np.uint8))
    idx = tables["cols"][bits == 1].ravel()
    syn = (np.bincount(idx, minlength=2400) & 1).astype(np.uint8)
    return (syn ^ mod.offsets()[slot]).astype(np.uint8)


def _a10r_page_offset(chunk: int, slot: int, frame_bit: int) -> tuple[int, str]:
    """(byte offset in the 18432-byte page, zone) for one frame bit.

    The inverse of nd_g2ldpc_a10r.frame().  Frame bytes 512..515 exist only for
    slot 3, where they are the AU's four MCRC bytes; for slots 0..2 they are
    virtual -- zeros that are stored nowhere -- so a bit there has no page
    offset and is reported as such rather than as an offset into somebody
    else's data.
    """
    fb = frame_bit // 8
    base = chunk * _A10R_AU_BYTES
    o = slot * _A10R_SLOT_STRIDE
    if fb < 512:
        return base + o + fb, "payload"
    if fb < 516:
        if slot == 3:
            return base + 2948 + (fb - 512), "MCRC field"
        return -1, "virtual (not stored)"
    if slot == 3:
        return base + 2952 + (fb - 516), "parity"
    return base + o + 512 + (fb - 516), "parity"


def _g2_mcrc(raw: bytes) -> tuple[bool | None, ...]:
    """Per-chunk digest verdicts from titania.fw's own checker.

    None where a chunk was not checked -- an erased chunk has no verdict, and
    that is a third answer, never a pass.
    """
    try:
        chk = _titania()["fw"].check_group2_page(bytes(raw))
    except Exception:                                        # noqa: BLE001
        return (None,) * 5
    out: list[bool | None] = []
    for c in (chk.get("chunks") or ()):
        ok = c.get("mcrc_ok")
        out.append(None if ok is None else bool(ok))
    return tuple(out) if out else (None,) * 5


def _ecc_a10r(raw: bytes, t0: float) -> EccResult:
    region = REGION_FW_GROUP2
    try:
        mod = _a10r()
        tables = _a10r_tables()
    except Exception as exc:                                 # noqa: BLE001
        return _ecc_blank(EccState.UNAVAILABLE, region, t0,
                          "the A10R modules could not be reached: %s: %s"
                          % (type(exc).__name__, exc))
    a = np.frombuffer(bytes(raw), dtype=np.uint8)
    units = tuple(ECC_UNIT_OF_CW[region](i) for i in range(20))
    syndromes: list[int] = []
    fixes: list[EccFix] = []
    converged: list[bool] = []
    flips: list[int] = []
    page = bytearray(bytes(raw))
    notes: list[str] = []

    for chunk in range(5):
        au = a[chunk * _A10R_AU_BYTES:(chunk + 1) * _A10R_AU_BYTES]
        for slot in range(4):
            cw = chunk * 4 + slot
            try:
                syn = _a10r_syndrome(mod, tables, mod.frame(au, slot), slot)
            except Exception as exc:                         # noqa: BLE001
                return _ecc_blank(EccState.UNAVAILABLE, region, t0,
                                  "codeword %d could not be checked: %s: %s"
                                  % (cw, type(exc).__name__, exc))
            weight = int(syn.sum())
            syndromes.append(weight)
            if weight == 0:
                converged.append(True)
                flips.append(0)
                continue
            pattern = tuple(int(v) for v in np.nonzero(syn)[0])
            col = tables["lut"].get(pattern) if weight == 6 else None
            if col is None:
                # More than one bit, or a pattern no column matches.  The
                # min-sum fallback such a codeword needs is not implemented
                # here, and this says so rather than calling the page clean:
                # measured drive-wide, every one of the 10 dirty codewords on
                # this drive is a single bit, so this branch has never fired.
                converged.append(False)
                flips.append(0)
                notes.append(
                    "codeword %d (%s) has syndrome weight %d, which is not a "
                    "single-bit pattern. The 1-bit column lookup cannot "
                    "resolve it and the normalized min-sum fallback (measured "
                    "100%% exact to ~232 bad bits per frame) is not "
                    "implemented in this viewer, so the codeword is left "
                    "exactly as read." % (cw, units[cw], weight))
                continue
            off, zone = _a10r_page_offset(chunk, slot, col)
            fixes.append(EccFix(codeword=cw, unit=units[cw], bit=int(col),
                                page_offset=off, zone=zone))
            converged.append(True)
            flips.append(1)
            if off >= 0:
                page[off] ^= 1 << (7 - (col % 8))
            else:
                notes.append(
                    "codeword %d (%s): the identified bit is frame bit %d, in "
                    "one of the four VIRTUAL bytes that slots 0..2 do not "
                    "store. Nothing on the page was changed for it."
                    % (cw, units[cw], int(col)))

    corrected = bytes(page)
    changed = corrected != bytes(raw)
    n_conv = sum(1 for v in converged if v)
    total = sum(flips)
    common = dict(region=region, code=ECC_CODE[region], n_codewords=20,
                  syndromes=tuple(syndromes), units=units,
                  converged=tuple(converged), flips=tuple(flips),
                  fixes=tuple(fixes))

    # The MCRC here is the group-2 checker's own 4-byte CRC-32Q per chunk, and
    # it is asked before and after so a payload fix (which the digest sees) can
    # be told from a parity fix (which it cannot, by construction).
    mcrc_before = _g2_mcrc(bytes(raw))
    mcrc_after = _g2_mcrc(corrected) if changed else mcrc_before

    if max(syndromes, default=0) == 0:
        checked = [v for v in mcrc_before if v is not None]
        state = (EccState.MCRC_DISAGREES if checked and not all(checked)
                 else EccState.CLEAN)
        return EccResult(state=state, mcrc_before=mcrc_before,
                         mcrc_after=mcrc_after, corrected_bits=0,
                         notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)
    if n_conv == 0:
        return EccResult(state=EccState.UNCORRECTABLE,
                         mcrc_before=mcrc_before, mcrc_after=mcrc_after,
                         corrected_bits=0, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)
    if n_conv < len(converged):
        return EccResult(state=EccState.PARTIAL, mcrc_before=mcrc_before,
                         mcrc_after=mcrc_after, page=corrected,
                         corrected_bits=total, notes=tuple(notes),
                         seconds=time.perf_counter() - t0, **common)
    if any(f.zone == "parity" for f in fixes):
        notes.append(
            "A fix landed in LDPC PARITY, which the AU digest does not cover: "
            "the digest was already passing and still passes, so that bit "
            "error is invisible to the MCRC by construction. Measured on this "
            "drive: 5 of the 10 dirty codewords are exactly this, and the "
            "decoded store serves those five pages as clean with the bit still "
            "flipped.")
    checked = [v for v in mcrc_after if v is not None]
    state = (EccState.MCRC_DISAGREES if checked and not all(checked)
             else EccState.CORRECTED)
    return EccResult(state=state, mcrc_before=mcrc_before,
                     mcrc_after=mcrc_after, page=corrected,
                     corrected_bits=total, notes=tuple(notes),
                     seconds=time.perf_counter() - t0, **common)


# ---------------------------------------------------------------------------
# the dispatch
# ---------------------------------------------------------------------------
def ecc_of_page(raw, ce=None, block: int = 0, page: int = 0,
                region: str | None = None) -> EccResult:
    """Run the ECC this page's region is protected by, and report every state.

    Never raises.  An unreachable decoder is EccState.UNAVAILABLE with the
    sentence -- a fact about this machine -- and a page with no bytes is
    NOT_ATTEMPTED, a fact about the capture.  Neither is ever a verdict about
    the media.

    The order of the first two tests is load-bearing.  The all-0x00 fill is
    tested before any decoder runs, because the corrector rewrites such a page
    into 18432 bytes of 0xFF and reports converged=True (measured, 180/180),
    turning "the die returned nothing" into "the media is erased".
    """
    t0 = time.perf_counter()
    if region is None:
        region = region_of(ce, block)
    region = str(region)
    if region not in RULE:
        region = REGION_UNKNOWN
    if raw is None:
        return _ecc_blank(EccState.NOT_ATTEMPTED, region, t0,
                          "no bytes to decode")
    raw = bytes(raw)
    if len(raw) != _RAW_PAGE_BYTES:
        return _ecc_blank(
            EccState.NOT_ATTEMPTED, region, t0,
            "this record is %d bytes; every ECC decoder in this region wants a "
            "full raw page of %d, so nothing was decoded"
            % (len(raw), _RAW_PAGE_BYTES))
    if _fill(raw, 0x00):
        return _ecc_blank(EccState.ZERO_PAGE, region, t0)
    if _fill(raw, 0xFF):
        return _ecc_blank(EccState.ERASED, region, t0)
    if region == REGION_FW_GROUP2:
        return _ecc_a10r(raw, t0)
    return _ecc_ee3(raw, region, t0)


def ecc_of_pages(items) -> list[EccResult]:
    """`ecc_of_page` over many pages, with one call into the corrector.

    `items` is [(raw, ce, block, page)]; the results come back in that order and
    each is exactly what `ecc_of_page` would have returned for its page.

    Why it exists.  The corrector spreads a batch over every core, and one page
    is four independent codewords -- so decoding page by page asked a 64-core
    machine for four jobs at a time and left it idle.  A 12-page block scan is
    48 jobs, and a sweep step over eight blocks is nearly 400.

    Nothing about a verdict changes.  Every page still gets its own syndromes,
    its own MCRC-before and its own notes computed exactly as before, from its
    own bytes; only the corrector call is shared, and `nd_deepparity.decode_pages`
    is a regrouping of `decode_page`'s batches that is asserted byte-identical
    to the loop (`tests/test_decode_pages.py`).  Pages that never reach the
    decoder -- erased, all-zero, already clean, or in the A10R region, which has
    its own corrector -- are answered here without joining the batch at all.

    The one thing that IS different is `EccResult.seconds`: a page in a batch is
    charged the batch's decode, not its own share of it.  That is a
    measurement of the request, which is what it now is.
    """
    results: list[EccResult | None] = [None] * len(items)
    pend: list[tuple[int, dict]] = []
    for i, (raw, ce, block, page) in enumerate(items):
        t0 = time.perf_counter()
        region = region_of(ce, block)
        if region not in RULE:
            region = REGION_UNKNOWN
        if region == REGION_FW_GROUP2 or raw is None \
                or len(bytes(raw)) != _RAW_PAGE_BYTES \
                or _fill(bytes(raw), 0x00) or _fill(bytes(raw), 0xFF):
            # Not an EE3 decode: hand it to the single-page path, which owns
            # every one of those rules and must stay their only statement.
            results[i] = ecc_of_page(raw, ce=ce, block=block, page=page,
                                     region=region)
            continue
        done, pending = _ee3_prepare(bytes(raw), region, t0)
        if done is not None:
            results[i] = done
        else:
            pend.append((i, pending))

    if pend:
        try:
            with _ECC_LOCK:
                out = _ecc_mods()["dp"].decode_pages(
                    [p["work"] for _i, p in pend])
        except Exception as exc:                             # noqa: BLE001
            for i, p in pend:
                results[i] = _ee3_failed(p, exc)
        else:
            for (i, p), (fixed, log) in zip(pend, out):
                results[i] = _ee3_finish(p, fixed, log)
    return [r for r in results if r is not None]
