"""Data layer for nandgui.

Everything that touches a DumpDevice, the FTL codec, or a blob lives here, and
every one of those functions is worker-thread only.  The rest of the file is
pure numpy / pure Python helpers that any thread may call, because the UI
thread formats hover text and histograms from them every frame.

No imgui, no OpenGL, ever.  That is what keeps this module testable headless
and what lets palette.py assert colours against known summary rows.

The five states.  PROGRAMMED / ERASED / SKIPPED / ABSENT / ZERO are distinct
facts about the media and the capture, and this project has twice drawn a wrong
conclusion by merging them.  The classification order in probe_page() is the
same order titania.dumpdev.read_page uses (skipped is tested before erased),
because a skipped page in a block marked erased would otherwise be served
18432 bytes of 0xFF and turn "we chose not to look" into "we looked and it was
blank".  ZERO can only ever be decided with bytes in hand, so probe_page never
returns it; fetch_page promotes to it.

The region is part of the address.  Page geometry varies across this drive, so
decode_raw() does not pick a decoder -- pagefmt.decode_for_region() does, from
(ce, block).  That is why decode_raw takes a `ce`: block 0 is a five-chunk
firmware page on CE 0..5 and ordinary four-AU FTL media on CE 6..11, and running
the FORMAT_A codec over the former fails on every page.  Every Decoded therefore
carries the region it was decoded under, which decoder ran and over what unit,
so the inspector can never report a verdict without saying what it is a verdict
about.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import time

import numpy as np

from . import msonames, pagefmt
from . import (
    CELLMAP_JSON,
    DECODED_PAGE_BYTES,
    DECODED_STORE,
    N_BLOCK,
    N_CE,
    N_ROWS,
    PAGES_SLC,
    PAGES_TLC,
    RAW_PAGE_BYTES,
    RAW_STORE,
    SUMMARY_NPZ,
    WERU_BIN_COUNT,
    WERU_TABLE_JSON,
    ensure_titania_on_path,
)

__all__ = [
    "PageState", "MissingSummary", "Summary", "load_summary", "load_cellmap",
    "WeruEntry", "load_weru_table", "Stores", "open_stores", "reopen_decoded",
    "close_stores", "get_codec", "PageRecord", "probe_page", "fetch_page",
    "probe_block", "probe_block_stripe", "BlockStatus", "probe_block_status",
    "Decoded", "DecodedUnit",
    "fetch_decoded", "decode_raw", "check_group2", "region_of",
    "needs_raw_decode",
    "EccState", "EccResult", "EccFix", "ecc_of_record", "ecc_page",
    "BlockEcc", "scan_block_ecc", "describe_ecc", "ECC_SCAN_PAGES",
    "entropy", "byte_histogram", "is_all_zero", "is_all_ff",
    "hex_dump", "describe_block", "describe_page", "pages_per_block",
]


# ---------------------------------------------------------------------------
# states
# ---------------------------------------------------------------------------
class PageState(enum.IntEnum):
    """Row index into palette.STATE_RGBA.

    The five are never merged; UNKNOWN is a sixth *rendering* state meaning
    'not classified yet', and must not be drawn as any of the five -- a stripe
    still being probed must never look like erased media.
    """

    PROGRAMMED = 0
    ERASED = 1     # zero-length record that exists; media reads 0xFF
    SKIPPED = 2    # Not read, reason recorded; read_page raises
    ABSENT = 3     # no record at all -- a hole, we never looked
    ZERO = 4       # uniform 0x00 bus constant, not erased media
    UNKNOWN = 5    # not probed yet this frame


#: The message fetch_page/probe_page attach to an ABSENT page when the store
#: gave us no sentence of its own.  It says "never looked", never "blank".
ABSENT_REASON = "no record in the store -- this page was never read"


# ---------------------------------------------------------------------------
# lazy titania imports
# ---------------------------------------------------------------------------
# The titania package lives on a WSL share.  Importing it at module import time
# would make `import nandgui.model` fail whenever the share is asleep, which
# would take down the whole UI -- including the parts (summary, palette) that
# need no store at all.  So every titania import is deferred to the first store
# call, on the worker thread, where the failure has a place to land: a sentence
# in Stores.raw_error that the status bar shows.
_TITANIA = {}


def _titania():
    """-> dict with the titania modules we use, imported once, lazily.

    geometry/fw/aux/codec are here for the region work: `geometry.region` is
    the only authority on which decode rules an address obeys, and `fw` holds
    the five-chunk group-2 reader.  pagefmt.py drives them; they are in this
    dict as well so a view that already reaches for model's lazy accessor gets
    the same modules through the same one-time import.
    """
    if not _TITANIA:
        ensure_titania_on_path()
        import titania.auxdata as _aux
        import titania.codec as _codec
        import titania.device as _device
        import titania.dumpdev as _dumpdev
        import titania.ftl as _ftl
        import titania.fw as _fw
        import titania.geometry as _geom
        _TITANIA.update(device=_device, dumpdev=_dumpdev, ftl=_ftl,
                        geometry=_geom, fw=_fw, aux=_aux, codec=_codec)
    return _TITANIA


def _device_error_cls():
    """-> titania.device.DeviceError, or Exception if titania is unreachable."""
    try:
        return _titania()["device"].DeviceError
    except Exception:                                        # noqa: BLE001
        return Exception


def _is_unknown_mcrc(mcrc):
    """True for titania.ftl.UNKNOWN, the 'no evidence either way' sentinel.

    The live normalisation now happens in pagefmt (`_is_unknown`), where the
    decode actually runs; this stays as the model-side test for any caller
    holding a codec verdict of its own, and the rule below is the same rule.

    It is falsey and raises on len()/iteration, so it must be recognised by
    identity before anything tries to make a tuple of it.  It is not False:
    Decoded.mcrc becomes None for it and the inspector renders a third verdict,
    because drawing "we could not tell" as "pass" is the same class of error as
    drawing skipped as erased.
    """
    if mcrc is None:
        return True
    try:
        unknown = _titania()["ftl"].UNKNOWN
    except Exception:                                        # noqa: BLE001
        return type(mcrc).__name__ == "Unknown"
    return mcrc is unknown or type(mcrc) is type(unknown)


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
class MissingSummary(Exception):
    """.message is a full on-screen sentence; app.py renders it, not a crash."""

    def __init__(self, path: str, message: str | None = None) -> None:
        self.path = str(path)
        self.message = message or (
            "No block summary at %s. The drive map needs it; build it with the "
            "summary tool that writes summary.npz (48000 rows, ce*4000+block). "
            "The block stripe and the page inspector still work -- they read "
            "the stores directly." % (self.path,))
        Exception.__init__(self, self.message)


_SUMMARY_INT32 = ("pages", "programmed", "erased", "skipped", "absent",
                  "indexed", "dec_clean", "dec_fail", "weru", "owner",
                  "erases")
_SUMMARY_FIELDS = (("ce", np.int16), ("block", np.int16), ("slc", np.int8),
                   ("zero", np.uint8), ("binid", np.int16),
                   ("flags", np.uint8), ("defect", np.uint8))


@dataclasses.dataclass(frozen=True)
class Summary:
    path: str
    ce: np.ndarray
    block: np.ndarray
    slc: np.ndarray
    pages: np.ndarray
    programmed: np.ndarray
    erased: np.ndarray
    skipped: np.ndarray
    absent: np.ndarray
    indexed: np.ndarray
    zero: np.ndarray
    dec_clean: np.ndarray
    dec_fail: np.ndarray
    weru: np.ndarray
    owner: np.ndarray
    binid: np.ndarray
    flags: np.ndarray
    defect: np.ndarray
    erases: np.ndarray
    status_names: np.ndarray
    bin_names: np.ndarray

    def index(self, ce: int, block: int) -> int:
        return int(ce) * N_BLOCK + int(block)

    def valid(self, ce: int, block: int) -> bool:
        return 0 <= int(ce) < N_CE and 0 <= int(block) < N_BLOCK

    def pages_for(self, ce: int, block: int) -> int:
        return pages_per_block(self.is_slc(ce, block))

    def is_slc(self, ce: int, block: int) -> bool:
        if not self.valid(ce, block):
            return False
        return bool(self.slc[self.index(ce, block)])


def load_summary(path: str = SUMMARY_NPZ) -> Summary:
    """Read once, keep the arrays.  Raises MissingSummary, never anything else.

    Any shape surprise is a MissingSummary too: half a summary would paint
    48000 blocks from 40000 rows of truth, and a map that is confidently wrong
    is worse than a map that says it is missing.
    """
    path = str(path)
    if not os.path.exists(path):
        raise MissingSummary(path)
    try:
        with np.load(path, allow_pickle=True) as npz:
            data = {k: npz[k] for k in npz.files}
    except MissingSummary:
        raise
    except Exception as exc:                                 # noqa: BLE001
        raise MissingSummary(path, "Could not read the block summary at %s: "
                                   "%s. Rebuild it with the summary tool that "
                                   "writes summary.npz." % (path, exc))

    need = list(_SUMMARY_INT32) + [n for n, _ in _SUMMARY_FIELDS]
    missing = [k for k in need + ["status_names", "bin_names"]
               if k not in data]
    if missing:
        raise MissingSummary(path, "The block summary at %s is missing %s. "
                                   "Rebuild it with the summary tool that "
                                   "writes summary.npz."
                                   % (path, ", ".join(sorted(missing))))

    kw = {}
    for name in _SUMMARY_INT32:
        kw[name] = np.ascontiguousarray(data[name], dtype=np.int32)
    for name, dt in _SUMMARY_FIELDS:
        kw[name] = np.ascontiguousarray(data[name], dtype=dt)
    bad = [k for k, v in kw.items() if v.shape != (N_ROWS,)]
    if bad:
        raise MissingSummary(path, "The block summary at %s has the wrong "
                                   "shape for %s (every array must be %d rows, "
                                   "ce*4000+block). Rebuild it with the "
                                   "summary tool that writes summary.npz."
                                   % (path, ", ".join(sorted(bad)), N_ROWS))

    status = np.asarray(data["status_names"], dtype=object).ravel()
    bins = np.asarray(data["bin_names"], dtype=object).ravel()
    return Summary(path=path, status_names=status, bin_names=bins, **kw)


def _sidecar(name: str) -> str:
    """A sidecar path read from the package AT CALL TIME.

    `adopt_data_dir` re-points these once the command line is parsed, and a
    default argument captured at import would still hold the pre-parse value --
    which is the bug that made the viewer look for msodescriptor.bin in the cwd.
    """
    import importlib
    return getattr(importlib.import_module(__package__), name)


def load_cellmap(path: str | None = None) -> dict[int, dict[int, bool]]:
    """{ce: {block: is_slc}}.

    Only the fallback when summary.npz is missing; the summary's own .slc is
    authoritative when present, and two sources of cell mode disagreeing is
    exactly the kind of thing that ends up mis-sized by a factor of three.
    """
    path = path or _sidecar("CELLMAP_JSON")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: dict[int, dict[int, bool]] = {}
    for ce, blocks in raw.items():
        row: dict[int, bool] = {}
        for block, mode in blocks.items():
            if isinstance(mode, str):
                row[int(block)] = mode.strip().lower() == "slc"
            else:
                row[int(block)] = bool(mode)
        out[int(ce)] = row
    return out


@dataclasses.dataclass(frozen=True)
class WeruEntry:
    weru_id: int
    owner: int
    bin_id: int
    erase_count: int
    valid_count: int
    slc: bool
    shape: str
    blocks: tuple[tuple[int, int], ...]      # (block, ce)


def _int_or(value, default: int = -1) -> int:
    """JSON nulls are real in this table; None must become the -1 sentinel,
    which palette paints as NONE_RGBA -- never as the low end of a ramp."""
    if value is None:
        return default
    try:
        return int(value)
    except Exception:                                        # noqa: BLE001
        return default


def load_weru_table(path: str | None = None) -> dict[int, WeruEntry]:
    path = path or _sidecar("WERU_TABLE_JSON")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    # The file on this board is {"werus": [...], ...}; a bare list and an
    # id-keyed dict are both accepted so a regenerated table cannot silently
    # come back empty.
    if isinstance(raw, dict):
        entries = raw.get("werus")
        if entries is None:
            entries = list(raw.values())
    else:
        entries = raw
    out: dict[int, WeruEntry] = {}
    for ent in entries or ():
        if not isinstance(ent, dict):
            continue
        wid = _int_or(ent.get("weru_id"), -1)
        blocks = tuple((_int_or(b), _int_or(c))
                       for b, c in (ent.get("blocks") or ()))
        out[wid] = WeruEntry(
            weru_id=wid,
            owner=_int_or(ent.get("owner")),
            bin_id=_int_or(ent.get("bin_id")),
            erase_count=_int_or(ent.get("erase_count")),
            valid_count=_int_or(ent.get("valid_count")),
            slc=bool(ent.get("slc", False)),
            shape=str(ent.get("shape", "")),
            blocks=blocks,
        )
    return out


# ---------------------------------------------------------------------------
# stores -- worker thread only from here down to the pure helpers
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Stores:
    raw: object | None
    decoded: object | None
    raw_error: str | None
    decoded_error: str | None
    include_suspect: bool
    codec: object | None
    # The paths these devices were actually opened with.  They are defaulted
    # extensions to the frozen field list, and they exist because
    # reopen_decoded used to reach for the module-level DECODED_STORE constant:
    # with --decoded pointing somewhere else, the first flip of the suspect-tier
    # toggle silently swapped in a different store, and the inspector went on
    # presenting its AUs as though nothing had moved.
    raw_path: str = RAW_STORE
    decoded_path: str = DECODED_STORE


def _open_one(path: str, page_bytes: int, include_suspect: bool):
    """-> (device, None) or (None, sentence).  Never raises."""
    try:
        dumpdev = _titania()["dumpdev"]
        dev = dumpdev.DumpDevice(root=path, page_bytes=page_bytes,
                                 include_suspect=include_suspect,
                                 in_place=False, prefer_store=True)
        return dev, None
    except Exception as exc:                                 # noqa: BLE001
        return None, "%s could not be opened (page_bytes=%d): %s: %s" % (
            path, page_bytes, type(exc).__name__, exc)


def open_stores(raw_path: str = RAW_STORE, decoded_path: str = DECODED_STORE,
                include_suspect: bool = False) -> Stores:
    """Open read-only.  Never raises; a failure becomes a status-bar sentence.

    Never call while anything writes the store: the index is mmapped.
    """
    raw, raw_err = _open_one(raw_path, RAW_PAGE_BYTES, include_suspect)
    # The two stores have different page_bytes and DumpDevice refuses a
    # mismatch, so they can never be one device.
    dec, dec_err = _open_one(decoded_path, DECODED_PAGE_BYTES, include_suspect)
    return Stores(raw=raw, decoded=dec, raw_error=raw_err,
                  decoded_error=dec_err, include_suspect=bool(include_suspect),
                  codec=None, raw_path=str(raw_path),
                  decoded_path=str(decoded_path))


def reopen_decoded(stores: Stores, include_suspect: bool) -> Stores:
    """Close and reopen decoded.store with the suspect tier in or out.

    It reopens the path this Stores was opened with, not the module default:
    the toggle changes which tier is served, never which store is mounted.
    """
    if stores.decoded is not None:
        try:
            stores.decoded.close()
        except Exception:                                    # noqa: BLE001
            pass
        stores.decoded = None
    stores.include_suspect = bool(include_suspect)
    path = getattr(stores, "decoded_path", None) or DECODED_STORE
    dev, err = _open_one(path, DECODED_PAGE_BYTES, include_suspect)
    stores.decoded = dev
    stores.decoded_error = err
    return stores


def close_stores(stores: Stores) -> None:
    if stores is None:
        return
    for name in ("raw", "decoded"):
        dev = getattr(stores, name, None)
        if dev is None:
            continue
        try:
            dev.close()
        except Exception:                                    # noqa: BLE001
            pass
        setattr(stores, name, None)
    stores.codec = None


def get_codec(stores: Stores) -> object:
    """Built on first use and cached on the Stores.

    Not thread-safe, and it does not need to be: worker.py opens
    one Stores per worker thread, so a codec is only ever touched by the one
    thread that built it.

    What that costs, now that the worker is a pool of N: there are N codecs and
    N private `lru_cache(4096)` keystream caches instead of one, so each is
    about 1/N as warm across a stripe of the same rows on different CEs.  The
    old claim here -- that single ownership is what keeps one cache warm across
    a whole stripe -- is no longer true and is not what makes this correct.
    (titania_nand._KS_CACHE, the 4-entry module-level one underneath, is still
    shared by every worker; its values are immutable `bytes` and every mutation
    is a single atomic dict operation, so the worst case there is a redundant
    recompute, never a wrong keystream.)
    """
    if stores.codec is None:
        stores.codec = _titania()["ftl"].default_codec()
    return stores.codec


# ---------------------------------------------------------------------------
# page access -- worker thread only
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class PageRecord:
    ce: int
    block: int
    page: int
    slc: bool
    state: PageState
    length: int                              # -1 when unknown / not read
    provenance: tuple[str, str] | None       # (tier, reason)
    reason: str | None
    data: bytes | None                       # only from fetch_page()


def _no_store_record(ce, block, page, slc, why) -> PageRecord:
    # No store means we never looked, which is ABSENT -- not erased, and not
    # an empty PROGRAMMED page.
    return PageRecord(ce=int(ce), block=int(block), page=int(page),
                      slc=bool(slc), state=PageState.ABSENT, length=-1,
                      provenance=None, reason=why, data=None)


def _ref_provenance(ref):
    try:
        t = ref.provenance
    except Exception:                                        # noqa: BLE001
        return None
    if t is None:
        return None
    try:
        tier, why = t
    except Exception:                                        # noqa: BLE001
        return None
    return (str(tier), str(why))


#: dumpdev.page_ref() raises for exactly one thing that means "this page
#: Exists": an overlay write, which lives in memory with no backing reference.
#: Everything else it can raise -- an OSError or StoreError off the mmapped
#: index, a bad blob read, the lun guard -- is a failure to look, and inferring
#: "programmed" from a failure to look is the merge this viewer exists to
#: prevent.  nandgui opens every store with in_place=False and never writes, so
#: the overlay is permanently empty here and this test is belt and braces.
_OVERLAY_MARK = "overlay write with no backing reference"


def _is_overlay_error(exc: BaseException) -> bool:
    return _OVERLAY_MARK in str(exc)


def _classify(ref) -> tuple[PageState, str | None]:
    """ref -> (state, reason).  The ordering here is load-bearing.

    is_skipped() is asked before is_erased(), exactly as dumpdev.read_page
    does, because both encode zero bytes and only the layout tells them apart.
    Getting it backwards paints "deliberately not read" as clean media.
    """
    try:
        if ref.is_skipped():
            t = _ref_provenance(ref)
            why = t[1] if t else "recorded as skipped by the capture"
            return PageState.SKIPPED, why
    except AttributeError:
        pass
    try:
        if ref.is_erased():
            return PageState.ERASED, None
    except AttributeError:
        pass
    return PageState.PROGRAMMED, None


def probe_page(stores: Stores, ce: int, block: int, page: int,
               slc: bool = False) -> PageRecord:
    """Classify without reading bytes.  ZERO is never returned -- it needs bytes."""
    dev = stores.raw if stores is not None else None
    if dev is None:
        return _no_store_record(ce, block, page, slc,
                                stores.raw_error if stores else
                                "the raw store is not open")
    try:
        ref = dev.page_ref(ce, block, page, lun=0, slc=bool(slc))
    except Exception as exc:                                 # noqa: BLE001
        # An overlay write (memory-only, no reference) exists, so calling it
        # absent would be the hole-vs-media confusion again.  Any other failure
        # means we could not look at all: that is UNKNOWN, which has its own
        # colour and its own headline, and never PROGRAMMED -- a failing index
        # read must not be reported as real content.
        if _is_overlay_error(exc):
            state = PageState.PROGRAMMED
            why = str(exc)
        else:
            state = PageState.UNKNOWN
            why = ("could not read the index for this page: %s: %s"
                   % (type(exc).__name__, exc))
        return PageRecord(ce=int(ce), block=int(block), page=int(page),
                          slc=bool(slc), state=state,
                          length=-1, provenance=None, reason=why, data=None)
    if ref is None:
        return _no_store_record(ce, block, page, slc, ABSENT_REASON)
    state, why = _classify(ref)
    return PageRecord(ce=int(ce), block=int(block), page=int(page),
                      slc=bool(slc), state=state,
                      length=int(getattr(ref, "length", -1)),
                      provenance=_ref_provenance(ref), reason=why, data=None)


def fetch_page(stores: Stores, ce: int, block: int, page: int,
               slc: bool = False) -> PageRecord:
    """probe_page + read_page() when PROGRAMMED.  Promotes to ZERO on all-0x00.

    Never raises: a DeviceError becomes .reason with the state it implies.  An
    erased page is not read at all -- the store synthesises 0xFF for it and
    materialising 18432 of them would only re-count what the record already
    says.
    """
    rec = probe_page(stores, ce, block, page, slc=slc)
    if rec.state is not PageState.PROGRAMMED:
        return rec
    dev = stores.raw
    try:
        data = dev.read_page(ce, block, page, lun=0, slc=bool(slc))
    except Exception as exc:                                 # noqa: BLE001
        # The ref said programmed and the read disagreed; keep the sentence and
        # stay PROGRAMMED-with-a-reason rather than inventing ERASED/ABSENT,
        # which would be a claim about the media we did not make.
        return dataclasses.replace(rec, reason=str(exc), data=None)
    # ZERO is the dead-CE bus constant, not erased media, and it can only be
    # decided here, with the bytes in hand.
    state = PageState.ZERO if is_all_zero(data) else PageState.PROGRAMMED
    return dataclasses.replace(rec, state=state, length=len(data), data=data)


def probe_block(stores: Stores, ce: int, block: int, slc: bool,
                n_pages: int) -> np.ndarray:
    """-> uint8[n_pages] of PageState, ref-only (no blob reads)."""
    n_pages = int(n_pages)
    # ABSENT is the honest default for a row we could not look at: it reads as
    # a hole, never as clean media.
    out = np.full(n_pages, PageState.ABSENT, dtype=np.uint8)
    dev = stores.raw if stores is not None else None
    if dev is None or n_pages <= 0:
        return out
    page_ref = dev.page_ref
    slc = bool(slc)
    for page in range(n_pages):
        try:
            ref = page_ref(ce, block, page, lun=0, slc=slc)
        except Exception as exc:                             # noqa: BLE001
            # Only an overlay write means "it exists"; every other exception is
            # a store failure, i.e. we could not look.  Painting those pages
            # PROGRAMMED would turn a dying index into 1104 pages of "real
            # content" with nothing on screen saying otherwise.
            out[page] = (PageState.PROGRAMMED if _is_overlay_error(exc)
                         else PageState.UNKNOWN)
            continue
        if ref is None:
            continue                            # stays ABSENT
        state, _ = _classify(ref)
        out[page] = state
    return out


#: Pages probed by _detect_slc.  All five are legal in both geometries (367 is
#: the last page of an SLC block), so one geometry answering and the other not
#: is evidence about the block, not about the page number.
_DETECT_PAGES = (0, 1, 7, 64, 367)


def _has_records(dev, ce: int, block: int, slc: bool) -> bool:
    """Does the store hold anything for this block under this geometry?"""
    for page in _DETECT_PAGES:
        try:
            if dev.page_ref(ce, block, page, lun=0, slc=slc) is not None:
                return True
        except Exception as exc:                             # noqa: BLE001
            # An overlay write raises but the page exists, so this geometry is
            # the right one.  A store failure proves nothing either way, so it
            # is not evidence for this geometry.
            if _is_overlay_error(exc):
                return True
    return False


def _detect_slc(dev, ce: int, block: int, claimed: bool | None = None) -> bool:
    """Which geometry does this block actually hold records under?

    It matters more than it looks: page_ref() answers under one geometry, so
    asking a 368-page pseudo-SLC block for its pages as TLC returns None for
    every single one, and the stripe then paints 368 real records as ABSENT --
    a hole where content exists.  That is precisely the merge this viewer
    exists to prevent, and guessing from metadata is what nearly lost the boot
    block.

    So the store decides, and `claimed` (the summary's slc flag) is only the
    order the store is asked in: the claim is checked first and kept when it
    answers, which costs one page_ref for a normal block.  When it does not
    answer, the other geometry is tried before the row is written off as a
    hole.  That matters because at least one metadata source in this dataset --
    weru_table.json -- disagrees with the store on ~5% of blocks, always in the
    direction that would turn 368 real pages into a hole.

    With nothing found under either geometry the answer is the claim (or TLC,
    the wider one), and the row is honestly ABSENT: there really are no records.
    """
    if dev is None:
        return bool(claimed)
    order = (False, True) if claimed is None else (bool(claimed), not bool(claimed))
    for slc in order:
        if _has_records(dev, ce, block, slc):
            return slc
    return bool(claimed) if claimed is not None else False


def probe_block_stripe(stores: Stores, block: int, summary: Summary | None,
                       n_ce: int = N_CE) -> np.ndarray:
    """-> uint8[n_ce, max_pages] of PageState for one block across all CEs.

    Short rows (SLC 368 inside a TLC-wide 1104 stripe) and dead CE0 are padded
    with ABSENT, never with ERASED: the padding must read as "there is nothing
    here", not as "this is blank media".
    """
    n_ce = int(n_ce)
    dev = stores.raw if stores is not None else None

    present: set[int] | None = None
    if dev is not None:
        try:
            present = set(int(c) for c in dev.present_ces())
        except Exception:                                    # noqa: BLE001
            present = None

    def live(ce: int) -> bool:
        # CE0 is dead and absent from this store; probing it would be 1104
        # index misses per stripe for a row we already know is a hole.
        if ce == 0 or dev is None:
            return False
        return present is None or ce in present

    modes = []
    for ce in range(n_ce):
        if not live(ce):
            modes.append(False)             # assume TLC: the wider geometry
            continue
        # The summary's slc flag orders the probe; it never decides it.  A
        # metadata claim that the store contradicts would cost this row all of
        # its records, painted as a hole -- see _detect_slc.
        claimed = None
        if summary is not None and summary.valid(ce, block):
            claimed = bool(summary.is_slc(ce, block))
        modes.append(_detect_slc(dev, ce, block, claimed))
    counts = [pages_per_block(m) for m in modes]
    max_pages = max(counts) if counts else PAGES_TLC
    out = np.full((n_ce, max_pages), PageState.ABSENT, dtype=np.uint8)
    if dev is None:
        return out
    for ce in range(n_ce):
        if not live(ce):
            continue
        n = counts[ce]
        out[ce, :n] = probe_block(stores, ce, block, modes[ce], n)
    return out


# ---------------------------------------------------------------------------
# per-page decode status -- the block stripe's ECC/MCRC modes
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class BlockStatus:
    """The decoded store's recorded status for every page of one block.

    This is an index read, not a decode: `page_ref(...).src.name` is the source
    the record was written by, and decoded3.store names one source per decode
    status ("decoded-ecc-corrected", "decoded-ecc-not-engaged", ...) with the
    expansion in store.json["status_vocabulary"].  Measured: a whole 12-CE x
    1104-page stripe of it costs 146 ms, against roughly eight minutes for the
    13,248 live decodes it stands in for.

    It is self-describing.  `codes` indexes `names`, and `names`
    comes from the store that is actually mounted, so a status this build has
    never heard of arrives with its own name attached and lands in a class that
    says "unknown status" instead of in a verdict.  Nothing here hard-codes the
    seventeen names of decoded3.store, and final_decoded.store works the day it
    appears.

    names[0] is always "", meaning no record for that page -- which is a hole,
    not a clean page, and the palette paints it as one.
    """

    block: int
    path: str
    codes: np.ndarray                    # uint8[n_ce, max_pages] -> names index
    names: tuple[str, ...]               # source names; [0] == "" (no record)
    statuses: tuple[str, ...]            # the store's status word per name
    meanings: tuple[str, ...]            # the store's own sentence per name
    pages: np.ndarray                    # int32[n_ce] pages the store answers
    slc: np.ndarray                      # bool[n_ce] geometry it answered under
    raw_slc: np.ndarray                  # bool[n_ce] the RAW store's geometry
    error: str | None = None

    def name_at(self, ce: int, page: int) -> str:
        try:
            return self.names[int(self.codes[int(ce), int(page)])]
        except Exception:                                    # noqa: BLE001
            return ""

    def describe(self, ce: int, page: int) -> str:
        """The hover line: the recorded name verbatim, then the store's own
        words for it.  Nothing is paraphrased, so no reader has to trust this
        viewer's mapping to know what the store said."""
        try:
            i = int(self.codes[int(ce), int(page)])
        except Exception:                                    # noqa: BLE001
            return ""
        if i <= 0 or i >= len(self.names):
            return "no record in %s for this page" % (self.path,)
        head = "%s (status %s)" % (self.names[i], self.statuses[i] or "unnamed")
        meaning = self.meanings[i] if i < len(self.meanings) else ""
        return head + (("\n" + meaning) if meaning else "")


#: Guard for a store with a pathological number of sources: `codes` is uint8 so
#: it can address 255 names, and the 256th and beyond share one bucket that says
#: so rather than wrapping round and reporting the wrong status.
_STATUS_MAX_NAMES = 255


def _ref_source_name(ref) -> str | None:
    """The record's source name, or None when the device has no sources.

    A DumpDevice over a directory tree hands back a Path, which has a `.name`
    of its own (the filename) -- taking that would turn "ce07_b3596_p0000.bin"
    into a decode status.  The type is checked, not just the attribute.
    """
    src = getattr(ref, "src", None)
    if src is None or isinstance(src, os.PathLike):
        return None
    name = getattr(src, "name", None)
    if name is None or isinstance(name, (bytes, bytearray)):
        return None
    return str(name)


def probe_block_status(stores: Stores, block: int, summary: Summary | None,
                       n_ce: int = N_CE) -> BlockStatus:
    """-> BlockStatus for one block across all CEs.  Worker thread only.

    The geometry is detected against the decoded device, not the raw one: the
    two stores are separate indexes, and asking the decoded store for the TLC
    pages of a block it holds as pseudo-SLC returns None 1104 times -- a whole
    CE row reported as "not in the decoded store" while 368 records sit there.
    That is the same trap _detect_slc was written for, one store over.

    The RAW store's geometry is detected too and reported in `raw_slc`, because
    the caller has to line these statuses up with the PageState stripe and the
    only safe thing to do when the two stores disagree about a block is to
    refuse -- with a sentence naming the CE.  Measured on this capture: 0
    disagreements over 121 (ce, block) pairs across eleven blocks including the
    mixed block 1815, so this is a guard, not a common path.

    Never raises: a failure lands in .error and the stripe paints "not probed".
    """
    n_ce = int(n_ce)
    dev = stores.decoded if stores is not None else None
    raw = stores.raw if stores is not None else None
    path = str(getattr(stores, "decoded_path", "") or "") if stores else ""
    empty = np.zeros((n_ce, 1), dtype=np.uint8)
    zeros_i = np.zeros(n_ce, dtype=np.int32)
    zeros_b = np.zeros(n_ce, dtype=bool)

    def failed(why: str) -> BlockStatus:
        return BlockStatus(block=int(block), path=path, codes=empty,
                           names=("",), statuses=("",), meanings=("",),
                           pages=zeros_i, slc=zeros_b, raw_slc=zeros_b,
                           error=why)

    if dev is None:
        return failed(getattr(stores, "decoded_error", None)
                      or "the decoded store is not open")

    vocab = _status_vocabulary(path)
    present: set[int] | None = None
    try:
        present = set(int(c) for c in dev.present_ces())
    except Exception:                                        # noqa: BLE001
        present = None

    def live(ce: int) -> bool:
        # CE0 is dead and absent from this store, exactly as in the raw one.
        if ce == 0:
            return False
        return present is None or ce in present

    # A dead CE is assumed TLC, the wider geometry -- not the summary's claim.
    # That is not a guess about the media, it is probe_block_stripe's rule
    # verbatim, and matching it is load-bearing: the two arrays are lined up
    # page for page by the caller, so max_pages has to be decided the same way
    # in both or every pseudo-SLC block would come out 368 wide against a
    # 1104-wide stripe and be discarded.
    slc = np.zeros(n_ce, dtype=bool)
    raw_slc = np.zeros(n_ce, dtype=bool)
    for ce in range(n_ce):
        if not live(ce):
            continue
        claimed = None
        if summary is not None and summary.valid(ce, block):
            claimed = bool(summary.is_slc(ce, block))
        slc[ce] = _detect_slc(dev, ce, block, claimed)
        raw_slc[ce] = (_detect_slc(raw, ce, block, claimed) if raw is not None
                       else slc[ce])
    counts = np.array([pages_per_block(bool(s)) for s in slc], dtype=np.int32)
    max_pages = int(counts.max()) if counts.size else PAGES_TLC

    names: list[str] = [""]
    index: dict[str, int] = {"": 0}
    codes = np.zeros((n_ce, max_pages), dtype=np.uint8)   # 0 == no record
    overflow = 0
    page_ref = dev.page_ref
    for ce in range(n_ce):
        if not live(ce):
            continue
        is_slc = bool(slc[ce])
        row = codes[ce]
        for page in range(int(counts[ce])):
            try:
                ref = page_ref(ce, block, page, lun=0, slc=is_slc)
            except Exception:                                # noqa: BLE001
                # a failed index read is not a record and not a verdict; it
                # stays "no record", which is what the palette paints as a
                # hole.  It is never inferred into a status.
                continue
            if ref is None:
                continue
            name = _ref_source_name(ref)
            if name is None:
                return failed(
                    "%s is not a store with source records (its page refs "
                    "carry no source), so it records no per-page decode "
                    "status." % (path or "the decoded device",))
            code = index.get(name)
            if code is None:
                if len(names) >= _STATUS_MAX_NAMES:
                    overflow += 1
                    continue
                code = len(names)
                index[name] = code
                names.append(name)
            row[page] = np.uint8(code)

    statuses = [""]
    meanings = [""]
    for name in names[1:]:
        entry = vocab.get(name) or {}
        statuses.append(str(entry.get("status", "") or "").strip().lower())
        meanings.append(str(entry.get("meaning", "") or "").strip())
    error = None
    if overflow:
        error = ("this block holds more than %d distinct record sources; %d "
                 "pages could not be named" % (_STATUS_MAX_NAMES, overflow))
    return BlockStatus(block=int(block), path=path, codes=codes,
                       names=tuple(names), statuses=tuple(statuses),
                       meanings=tuple(meanings), pages=counts, slc=slc,
                       raw_slc=raw_slc, error=error)


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------
_AU_STRIDE = DECODED_PAGE_BYTES // 4         # 4104 = 8 aux + 4096 user


#: The two record sources the old decoded.store / decoded2.store distinguish,
#: verbatim from their store.json.  PageRef.provenance formats its reason as
#: "<source name>/: <why>", so the name in front of "/:" is the page-level
#: verdict the store recorded.
#:
#: These two are now the fallback, not the vocabulary.  decoded3.store names one
#: source per decode status ("decoded-ecc-corrected", "decoded-ecc-not-engaged",
#: "decoded-all-zero-retired", ...) and ships the expansion in
#: store.json["status_vocabulary"], which _status_vocabulary() below reads.  The
#: two constants stay because "decoded" still means clean in the new store, so
#: an older store keeps rendering exactly as it did.
_DECODED_SOURCE_CLEAN = "decoded"              # every AU's MCRC passed at decode time
_DECODED_SOURCE_FAILED = "decoded-mcrc-failed"  # at least one AU failed; quarantined


#: path -> {source name: {status, meaning, evidence, confidence, served, tier}}
#: or {} when the store has no vocabulary (the old two-source stores).  Read
#: once per path; the file is ~22 KB and never changes under a running GUI.
_VOCAB_CACHE: dict[str, dict] = {}


def _status_vocabulary(path: str | None) -> dict:
    """The decoded store's own status vocabulary.  {} when it has none.

    Never raises: an unreadable or vocabulary-less store.json degrades to the
    two-name fallback in _decoded_provenance, which is what decoded.store and
    decoded2.store want anyway.
    """
    if not path:
        return {}
    key = str(path)
    hit = _VOCAB_CACHE.get(key)
    if hit is not None:
        return hit
    out: dict = {}
    try:
        with open(os.path.join(key, "store.json"), "rb") as fh:
            meta = json.load(fh)
        vocab = meta.get("status_vocabulary")
        if isinstance(vocab, dict):
            out = {str(k): v for k, v in vocab.items() if isinstance(v, dict)}
    except Exception:                                            # noqa: BLE001
        out = {}
    _VOCAB_CACHE[key] = out
    return out


#: One AU or one chunk, re-exported so views need only import model.
DecodedUnit = pagefmt.Unit


@dataclasses.dataclass(frozen=True)
class Decoded:
    aus: tuple[bytes, ...] | None
    mcrc: tuple[bool, ...] | None            # None == UNKNOWN, a third verdict
    source: str
    seconds: float
    error: str | None
    #: The store's page-level MCRC roll-up for a page served out of
    #: decoded.store: True = it recorded "all four AUs passed at decode time",
    #: False = it recorded "at least one AU failed" (the quarantined suspect
    #: tier), None = no verdict is recorded anywhere.
    #:
    #: It is separate from `mcrc`, which is per-au: the store keeps
    #: no per-AU verdict, so filling mcrc in from this would invent four facts
    #: out of one.  It is equally not folded into "unknown": the
    #: inspector used to render both tiers as "MCRC unknown", which reported a
    #: store-certified pass and a store-recorded failure as the same third
    #: verdict -- and switching the suspect tier on then lost a real failure.
    page_mcrc: bool | None = None
    #: (tier, reason) from the decoded store's PageRef, for display.
    provenance: tuple[str, str] | None = None

    # --- what geometry this verdict is a verdict under ---------------------
    # Everything below is defaulted: an older caller that builds a Decoded
    # positionally is unaffected, and a view that has not learned about regions
    # yet reads "" / 0 / None, which are all "not stated" -- never a claim.
    #
    # The region is carried, not re-derived: a panel that recomputed it from the
    # block number would drop the CE test, and block 0 on CE 6..11 would then be
    # described as a firmware page while the bytes in this very object came out
    # of the FORMAT_A codec.
    #: pagefmt.REGION_* -- "" when this Decoded predates the region plumbing.
    region: str = ""
    #: The addressing rule that put this page in that region, for the UI.
    region_rule: str = ""
    #: Which decoder actually ran, named.
    decoder: str = ""
    #: The stored format, as a display string (e.g. "FORMAT_A").
    fmt: str = ""
    #: Bytes of MCRC/digest per unit: 6 for FORMAT_A, 4 for a group-2 digest.
    #: 0 means "not stated here", not "this format has no MCRC".
    mcrc_width: int = 0
    #: What a page divides into HERE: "AU" (4) or "chunk" (5).  The noun exists
    #: so the inspector can stop calling five group-2 chunks "AUs".
    unit_noun: str = "AU"
    unit_count: int = 0
    #: The per-unit projection: index, noun, verdict, bytes, digest.  It is the
    #: one shape both geometries share, so a view can iterate it blind.
    units: tuple = ()

    # --- fw-group2 only ----------------------------------------------------
    #: fw.check_group2_page's own chunk dicts, verbatim
    #: ({'digest','erased','message_sha1','mcrc_ok'}), or None off that region.
    chunks: tuple[dict, ...] | None = None
    digest_consistent: bool | None = None
    tail_ff: bool | None = None
    erased: bool | None = None
    #: Was the page the length its region expects?  A short read is a read
    #: failure and not a digest mismatch; the two must not share a verdict.
    size: bool | None = None
    #: (n_ok, n_checked) -- the roll-up as the region's own checker counted it.
    mcrc_counts: tuple[int, int] | None = None
    #: The checker's whole-page verdict, wider than page_mcrc: it also covers
    #: the erased tail and digest consistency.  Kept separate so "5/5 digests
    #: match but the tail is not erased" can be said out loud.
    region_ok: bool | None = None
    notes: tuple[str, ...] = ()

    #: Does this record's own geometry actually apply to its region?  False for
    #: a decoded.store record served for a group-2 page: that store holds every
    #: page as 4 x 4104, which for five 2048-byte chunks is a shape, not a page.
    #: needs_raw_decode() below is how a caller acts on it.
    geometry_applies: bool = True

    def aux(self, i: int) -> bytes:
        # AU-shaped only.  Group-2 Decoded objects carry aus=None precisely so
        # this cannot slice chunk bytes into a fictitious 8-byte aux.
        if not self.aus or not (0 <= i < len(self.aus)):
            return b""
        return bytes(self.aus[i][:8])

    def user(self, i: int) -> bytes:
        if not self.aus or not (0 <= i < len(self.aus)):
            return b""
        return bytes(self.aus[i][8:8 + 4096])

    def unit(self, i: int):
        """-> the i'th DecodedUnit, or None.  Works in both geometries."""
        if not self.units or not (0 <= i < len(self.units)):
            return None
        return self.units[i]

    @property
    def ok(self) -> bool | None:
        """The region checker's whole-page verdict, under the name a caller
        holding one of fw.check_group2_page's own dicts would look for.

        An alias for region_ok, not a roll-up of anything else: where no
        checker gave a whole-page verdict this is None ("not reported"), which
        is a third value and never a failure.
        """
        return self.region_ok


def _split_aus(data: bytes) -> tuple[bytes, ...]:
    return tuple(bytes(data[i * _AU_STRIDE:(i + 1) * _AU_STRIDE])
                 for i in range(4))


# ---------------------------------------------------------------------------
# regions
# ---------------------------------------------------------------------------
def region_of(ce, block) -> str:
    """(ce, block) -> pagefmt.REGION_*.  Thin, so there is one region map.

    Re-exported here only so the views can keep importing model alone.  It asks
    titania.geometry.region and nothing else: the CE is half the test (block 0
    is group 2 on CE 0..5 and ordinary FTL media on CE 6..11), so a caller that
    re-derived a region from the block number would feed real FORMAT_A pages to
    the five-chunk decoder.
    """
    return pagefmt.region_of(ce, block)


#: The decoded.store did its decoding at capture time, so a record served from
#: it names the store, not a decoder that ran just now.
_STORE_DECODER = ("decoded.store, decoded at capture time -- the store holds "
                  "every page as 4 x (8 aux + 4096 user) regardless of region")


def _store_region_kw(ce, block) -> dict:
    """Region fields for a record served OUT of decoded.store.

    `geometry_applies` is the load-bearing one.  decoded.store keeps every page
    in the same 4 x 4104 shape, which is the truth for the AU regions and a
    fiction for fw-group2, where a page is five 2048-byte chunks.  Saying so on
    the record is what lets the caller fall through to the region's own decoder
    instead of drawing a four-AU view over bytes that have no AUs -- and it does
    it without touching the store's own recorded verdict, which stays exactly as
    the tier recorded it.
    """
    region = pagefmt.region_of(ce, block)
    return dict(region=region,
                region_rule=pagefmt.RULE.get(region, ""),
                decoder=_STORE_DECODER,
                fmt=pagefmt.FORMAT_NAME.get(region, ""),
                mcrc_width=pagefmt.MCRC_WIDTH.get(region, 0),
                unit_noun=pagefmt.UNIT_NOUN.get(region, "AU"),
                unit_count=pagefmt.UNIT_COUNT.get(region, 0),
                geometry_applies=(region != pagefmt.REGION_FW_GROUP2))


def needs_raw_decode(dec: "Decoded | None") -> bool:
    """Must the caller still decode the RAW page itself?

    True when decoded.store had nothing (the old `dec is None` test), and also
    true when it had a record whose 4 x 4104 shape does not apply to this page's
    region -- a group-2 page out of the suspect tier is a real record with a
    real recorded verdict, but it is not a four-AU page and must not be drawn as
    one.
    """
    return dec is None or not getattr(dec, "geometry_applies", True)


#: Which recorded statuses assert that every unit's MCRC/digest passes on the
#: bytes the store serves, which assert one fails, and which assert neither.
#: This is the page_mcrc verdict only -- it is not the ECC verdict,
#: which is a separate measurement carried by pagefmt.ecc_of_page().  A page can
#: be ecc-corrected (ECC had work to do) and still be MCRC-true afterwards; the
#: whole point of keeping them apart is that "the MCRC passes" and "no
#: correction was needed" are different facts.
_MCRC_TRUE = frozenset({"clean", "ecc-corrected", "group2-ok",
                        "group2-corrected-parity", "group2-corrected-payload"})
_MCRC_FALSE = frozenset({"ecc-not-engaged", "ecc-diverged", "ecc-partial",
                         "ecc-ok-mcrc-disagrees", "group2-mcrc-disagrees",
                         "group2-failed", "mcrc-failed"})


def decoded_quarantines(path: str | None) -> bool | None:
    """Does this decoded store hold records in an excluded tier?

    True  -- it has a suspect/superseded tier, so the include_suspect toggle
             decides whether some pages are readable at all.
    False -- every record is served on the default read path.  decoded3.store is
             this: measured 5,453,635 served records with the toggle off and the
             same 5,453,635 with it on.  The toggle governs nothing, and telling
             the user the view is limited to "clean only" would be false.
    None  -- unknown (no store.json, or it could not be read).

    Read from store.json's own `tiers` map rather than from a source count, so a
    store that declares a suspect tier but happens to have nothing in it still
    reports True: the toggle is still meaningful there.
    """
    if not path:
        return None
    try:
        with open(os.path.join(str(path), "store.json"), "rb") as fh:
            meta = json.load(fh)
    except Exception:                                            # noqa: BLE001
        return None
    tiers = meta.get("tiers")
    if not isinstance(tiers, dict):
        return None
    return any(str(v).strip().lower() in ("suspect", "superseded")
               for v in tiers.values())


def _decoded_provenance(prov, vocab: dict | None = None) -> tuple[str, bool | None]:
    """(source sentence, page-level MCRC verdict) for a decoded-store ref.

    The store does record a verdict, and throwing it away was reporting
    store-certified clean pages and store-recorded failures as the same "MCRC
    unknown".

    Two stores are handled, and the difference is the whole point of the ECC
    work.  The old stores record the verdict in which tier the record lives, and
    have exactly two names: `decoded` and the quarantined `decoded-mcrc-failed`.
    decoded3.store records the verdict in the record's source name -- one name
    per decode status, all of them served on the default read path -- and ships
    the expansion in store.json["status_vocabulary"].  When that vocabulary has
    an entry, it wins, because it is the store's own words about its own record;
    the two-name test below is the fallback for a store without one.

    `vocab` is passed in rather than looked up here so this stays a pure
    function of its arguments and the caller decides which store it is talking
    about.
    """
    if prov is None:
        return ("decoded-store (no recorded tier)", None)
    tier, why = prov
    name = str(why).split("/:", 1)[0].strip().lower()
    # NB `tier` is the store's TRUST tier ("unknown"/"suspect"), which is about
    # provenance, not about MCRC -- the clean tier is literally
    # named "unknown".  Naming both, in that order, keeps the two apart.
    entry = (vocab or {}).get(name)
    if entry:
        status = str(entry.get("status", "")).strip().lower()
        meaning = str(entry.get("meaning", "")).strip()
        page_mcrc = (True if status in _MCRC_TRUE else
                     False if status in _MCRC_FALSE else None)
        conf = entry.get("confidence")
        head = ("decoded-store, source '%s' (tier %s, status %s%s): "
                % (name, tier, status or "unnamed",
                   "" if conf is None else ", confidence %s" % (conf,)))
        return (head + (meaning or "the store records no meaning for this "
                                   "status"), page_mcrc)
    if name == _DECODED_SOURCE_CLEAN:
        return ("decoded-store, source '%s' (tier %s): every AU's MCRC "
                "was checked at decode time and all four passed"
                % (name, tier), True)
    if name == _DECODED_SOURCE_FAILED:
        return ("decoded-store, source '%s' (tier %s): at least one AU "
                "FAILED MCRC at decode time, so this page is quarantined"
                % (name, tier), False)
    return ("decoded-store, source '%s' (tier %s): no MCRC verdict "
            "recorded" % (name, tier), None)


def fetch_decoded(stores: Stores, ce: int, block: int, page: int,
                  slc: bool = False) -> Decoded | None:
    """The cheap path out of decoded.store.  None => not there, call decode_raw.

    `mcrc` (per AU) stays None because the store keeps no per-AU verdict, and
    None is rendered as "unknown", never as pass.  The page-level verdict the
    store does keep -- which tier the record lives in -- comes back in
    `page_mcrc` and in `source`, so a page served out of the quarantined
    mcrc-failed tier is shown as failed instead of unknown.  Whether that tier
    is visible at all is still decided by the store's include_suspect, i.e. by
    reopen_decoded.

    The store's verdict is unchanged by the region work: which tier a record
    lives in is a fact the store recorded, and it is reported here exactly as
    before.  What is new is that the record now says which region it belongs to
    and whether its 4 x 4104 shape applies there -- see _store_region_kw.
    """
    t0 = time.perf_counter()
    dev = stores.decoded if stores is not None else None
    if dev is None:
        return None
    kw = _store_region_kw(ce, block)
    try:
        ref = dev.page_ref(ce, block, page, lun=0, slc=bool(slc))
    except Exception as exc:                                 # noqa: BLE001
        return Decoded(aus=None, mcrc=None, source="decoded-store",
                       seconds=time.perf_counter() - t0, error=str(exc), **kw)
    if ref is None:
        return None                          # genuinely not in that store
    prov = _ref_provenance(ref)
    source, page_mcrc = _decoded_provenance(
        prov, _status_vocabulary(getattr(stores, "decoded_path", None)))
    state, why = _classify(ref)
    if state is PageState.SKIPPED:
        # A skipped decoded page has no bytes and no claim about the media.
        return Decoded(aus=None, mcrc=None, source=source,
                       seconds=time.perf_counter() - t0,
                       error="the decoded store deliberately did not read this "
                             "page: %s" % (why,),
                       page_mcrc=page_mcrc, provenance=prov, **kw)
    try:
        data = dev.read_page(ce, block, page, lun=0, slc=bool(slc))
    except Exception as exc:                                 # noqa: BLE001
        return Decoded(aus=None, mcrc=None, source=source,
                       seconds=time.perf_counter() - t0, error=str(exc),
                       page_mcrc=page_mcrc, provenance=prov, **kw)
    if len(data) != DECODED_PAGE_BYTES:
        return Decoded(aus=None, mcrc=None, source=source,
                       seconds=time.perf_counter() - t0,
                       error="decoded page is %d bytes, expected %d"
                             % (len(data), DECODED_PAGE_BYTES),
                       page_mcrc=page_mcrc, provenance=prov, **kw)
    aus = _split_aus(data)
    # Per-AU verdicts stay None: the store keeps one verdict per PAGE, and
    # filling four in from it would invent three facts.
    units = tuple(DecodedUnit(index=i, noun=kw["unit_noun"], ok=None,
                              data=aus[i]) for i in range(len(aus)))
    error = None
    if not kw["geometry_applies"]:
        # A real record with a real recorded verdict, in a shape this region
        # does not have.  Say so instead of drawing four AUs over five chunks;
        # needs_raw_decode() sends the caller to the region's own decoder.
        error = ("this record is stored as 4 x (8 aux + 4096 user), which is "
                 "not the geometry of the %s region -- its pages are %d %ss. "
                 "The store's own verdict above still stands; the AU view does "
                 "not." % (kw["region"], kw["unit_count"], kw["unit_noun"]))
    return Decoded(aus=aus, mcrc=None, source=source,
                   seconds=time.perf_counter() - t0, error=error,
                   page_mcrc=page_mcrc, provenance=prov, units=units, **kw)


#: source= prefixes, by region, so the panel's one-line provenance says which
#: geometry produced the bytes and not merely "codec".
_RAW_SOURCE = {
    pagefmt.REGION_FW_GROUP2: "fw group-2 checker",
    pagefmt.REGION_FW_GROUP01: "codec FORMAT_A (fw group 0/1)",
    pagefmt.REGION_FTL: "codec FORMAT_A (ftl)",
    pagefmt.REGION_UNKNOWN: "codec FORMAT_A (region not determined)",
}


def decode_raw(stores: Stores, raw: bytes, block: int, page: int,
               correct: bool | None = None, ce: int | None = None) -> Decoded:
    """Decode one raw page the way its region says to.  25-86 ms.

    The decoder is chosen by pagefmt.decode_for_region(), never here, and the
    choice needs the CE: block 0 is a five-chunk firmware page on CE 0..5 and
    ordinary four-AU FTL media on CE 6..11.  `ce` is a defaulted keyword
    argument so the old positional call `decode_raw(stores, raw, block, page,
    correct=...)` still works -- but omitting it costs the region, and a page
    decoded without one is labelled "region not determined" rather than quietly
    presented as FTL.

    What each region gets, measured on this capture:
      ftl / fw-group01   the FORMAT_A codec, 4 AUs x (8 aux + 4096 user),
                         6-byte MCRC.  Group 0/1 measures 4/4 here, so it keeps
                         the codec and gains only its label.
      fw-group2          fw.check_group2_page + fw.decode_group2_page: Five
                         chunks, each with its own 4-byte digest.  FORMAT_A
                         fails on every page of that region by construction,
                         which is what used to paint 1840 good pages red.

    Worker thread only.  Never raises; failures land in .error, because a codec
    exception must not cross into the frame loop.
    """
    t0 = time.perf_counter()
    region = pagefmt.region_of(ce, block)
    source = _RAW_SOURCE.get(region, "codec")
    if correct is True and pagefmt.needs_codec(region):
        source += ", ECC-corrected"

    codec = None
    if pagefmt.needs_codec(region):
        # Group 2 never asks for the codec: its digest check is seed-free and
        # ECC-free, so a group-2 page stays checkable even when default_codec()
        # cannot be built at all.
        try:
            codec = get_codec(stores)
        except Exception as exc:                             # noqa: BLE001
            return Decoded(aus=None, mcrc=None, source=source,
                           seconds=time.perf_counter() - t0,
                           error="no page codec: %s: %s"
                                 % (type(exc).__name__, exc),
                           **_outcome_region_kw(region))

    try:
        out = pagefmt.decode_for_region(codec, raw, ce, block, page,
                                        correct=correct, region=region)
    except Exception as exc:                                 # noqa: BLE001
        # decode_for_region documents that it never raises; this is the belt to
        # that braces, because the frame loop is on the other side of a queue.
        return Decoded(aus=None, mcrc=None, source=source,
                       seconds=time.perf_counter() - t0,
                       error="%s: %s" % (type(exc).__name__, exc),
                       **_outcome_region_kw(region))
    return _decoded_from(out, source, time.perf_counter() - t0)


def _outcome_region_kw(region: str) -> dict:
    """Region fields for a Decoded that has no Outcome to take them from."""
    return dict(region=region, region_rule=pagefmt.RULE.get(region, ""),
                decoder=pagefmt.DECODER.get(region, ""),
                fmt=pagefmt.FORMAT_NAME.get(region, ""),
                mcrc_width=pagefmt.MCRC_WIDTH.get(region, 0),
                unit_noun=pagefmt.UNIT_NOUN.get(region, "AU"),
                unit_count=pagefmt.UNIT_COUNT.get(region, 0))


def _decoded_from(out, source: str, seconds: float) -> Decoded:
    """pagefmt.Outcome -> Decoded, with the three verdicts kept apart.

    `mcrc` stays per-unit and None still means UNKNOWN, the third verdict.
    `page_mcrc` stays the separate page-level roll-up.  `region_ok` is a third
    thing again -- the region checker's whole-page verdict, which for group 2
    also covers the erased tail and digest consistency -- and it is never folded
    into either of the other two.
    """
    # getattr with a default throughout: pagefmt.Outcome is another module's
    # dataclass and is still growing fields.  A missing one must degrade to "not
    # stated", never to a worker-thread AttributeError one queue away from the
    # frame loop.
    get = lambda name, default=None: getattr(out, name, default)  # noqa: E731
    region = str(get("region", pagefmt.REGION_UNKNOWN))
    mcrc = get("mcrc")
    if mcrc is not None:
        # Group 2 reports None for an erased chunk (nothing was checked), so the
        # tuple is bool|None per unit and must not be flattened to bools.
        mcrc = tuple(mcrc)
    return Decoded(
        aus=get("aus"), mcrc=mcrc, source=source, seconds=seconds,
        error=get("error"), page_mcrc=get("page_mcrc"), provenance=None,
        region=region, region_rule=pagefmt.RULE.get(region, ""),
        decoder=str(get("decoder", "")), fmt=str(get("fmt", "")),
        mcrc_width=int(get("mcrc_width", 0) or 0),
        unit_noun=str(get("unit_noun", "AU")),
        unit_count=int(get("unit_count", 0) or 0),
        units=tuple(get("units", ()) or ()),
        chunks=get("chunks"), digest_consistent=get("digest_consistent"),
        tail_ff=get("tail_ff"), erased=get("erased"), size=get("size"),
        mcrc_counts=get("mcrc_counts"),
        region_ok=get("region_ok"), notes=tuple(get("notes", ()) or ()),
        # This decode was the region's own decoder, so its geometry is by
        # definition the region's geometry.
        geometry_applies=True)


def check_group2(stores: Stores, raw: bytes, ce: int | None = None,
                 block: int = 0, page: int = 0) -> dict | None:
    """The five-chunk verdict for one raw fw-group2 page, or None.

    -> titania.fw.check_group2_page's own dict, extended with the plaintext the
    region's decoder produced where it was available:

        {'size', 'erased', 'tail_ff',
         'chunks': [{'digest', 'erased', 'message_sha1', 'mcrc_ok'}, ...],
         'payloads': [bytes | None, ...],
         'digest_consistent', 'mcrc': (n_ok, n_total), 'notes', 'ok'}

    `payloads` is the decoded user DATA, index-aligned with `chunks`: the 2048
    descrambled payload bytes of each chunk, straight off the Units
    pagefmt._decode_group2 already built from fw.decode_group2_page's 10240-byte
    plaintext.  MEASURED on ce1/block0/page0 (pseudo-SLC): five entries of 2048
    bytes, 10240 total.  An entry is None when only the structural check ran --
    the plaintext needs the row seed and the scrambler, the digests do not -- and
    None there is "no payload was produced", never "the payload is empty".

    None means no group-2 reader was reachable -- it is not a verdict about the
    page, and a caller must say so rather than fall back to the four-AU
    vocabulary that does not fit these bytes.

    `stores` is accepted (and unused) so this reads like its neighbours; the
    group-2 check needs no codec, no seed and no ECC, which is exactly why a
    page here stays checkable when the FTL codec cannot be built at all.

    Worker thread only.  Never raises -- same contract as decode_raw, for the
    same reason.
    """
    del stores                              # no store, no codec, by design
    if raw is None:
        return None
    try:
        out = pagefmt.decode_for_region(None, raw, ce, block, page,
                                        correct=False,
                                        region=pagefmt.REGION_FW_GROUP2)
    except Exception:                                        # noqa: BLE001
        return None
    chunks = getattr(out, "chunks", None)
    if not chunks:
        return None
    return {
        "size": getattr(out, "size", None),
        "erased": getattr(out, "erased", None),
        "tail_ff": getattr(out, "tail_ff", None),
        "chunks": [dict(c) for c in chunks],
        # The decoded payload, one entry per chunk, in chunk order.  It rides on
        # the Units rather than being re-sliced here: pagefmt already cut the
        # 10240-byte plaintext into five 2048-byte payloads, and a second cut
        # with its own idea of the chunk stride is how two views end up showing
        # different bytes for the same chunk.
        "payloads": [(bytes(u.data) if getattr(u, "data", None) else None)
                     for u in (getattr(out, "units", ()) or ())],
        "digest_consistent": getattr(out, "digest_consistent", None),
        # (n_ok, n_total) exactly as the checker counted it; erased chunks are
        # not counted, so n_total is 5 only when five were actually checked.
        "mcrc": getattr(out, "mcrc_counts", None),
        "notes": list(getattr(out, "notes", ()) or ()),
        "ok": getattr(out, "region_ok", None),
    }


# ---------------------------------------------------------------------------
# ECC -- WORKER THREAD ONLY
# ---------------------------------------------------------------------------
# The ECC vocabulary, the decoders and every measured number behind them live
# in pagefmt (which owns "what a page in this region is checked by"); this
# section is only the store side of it: which bytes to hand the decoder, and
# what to report for a page that has no bytes at all.
EccState = pagefmt.EccState
EccResult = pagefmt.EccResult
EccFix = pagefmt.EccFix
describe_ecc = pagefmt.describe_ecc

#: How many pages of a block a scan measures by default.  A full 1104-page
#: block would cost 5-100 s (4-8 ms a clean page, up to ~95 ms a bad one), so
#: the map measures an evenly spaced sample and says so -- the legend and the
#: hover both report how many pages of the block were actually looked at, and a
#: sampled block is never described as a whole-block verdict.
ECC_SCAN_PAGES = 12


def ecc_of_record(rec: PageRecord | None, ce: int, block: int, page: int,
                  region: str | None = None) -> EccResult:
    """A PageRecord -> its ECC verdict.  Never raises.

    The states with no bytes are answered here rather than in pagefmt, because
    the reason there are no bytes is a fact about the capture and belongs with
    the record: an ERASED page is ECC-not-applicable, while SKIPPED and ABSENT
    are "nobody asked the decoder anything".  Rendering either of those as a
    failed correction would be the same merge this viewer exists to prevent.
    """
    if rec is None:
        return pagefmt.ecc_of_page(None, ce=ce, block=block, page=page,
                                   region=region)
    state = PageState(int(rec.state))
    if state is PageState.ERASED:
        return EccResult(state=EccState.ERASED,
                         region=region or pagefmt.region_of(ce, block),
                         code=pagefmt.ECC_CODE.get(
                             region or pagefmt.region_of(ce, block), ""),
                         notes=("The store holds a zero-length ERASED record "
                                "for this page: the media reads 0xFF and there "
                                "is nothing for a decoder to decode.",))
    if rec.data is None:
        why = rec.reason or ("this page carries no bytes (%s)"
                             % pagefmt.ECC_SHORT[EccState.NOT_ATTEMPTED])
        return EccResult(state=EccState.NOT_ATTEMPTED,
                         region=region or pagefmt.region_of(ce, block),
                         code=pagefmt.ECC_CODE.get(
                             region or pagefmt.region_of(ce, block), ""),
                         error=str(why))
    return pagefmt.ecc_of_page(rec.data, ce=ce, block=block, page=page,
                               region=region)


def ecc_page(stores: Stores, ce: int, block: int, page: int,
             slc: bool = False) -> tuple[PageRecord, EccResult]:
    """fetch_page + ecc_of_record, for a caller that wants both.  Worker only."""
    rec = fetch_page(stores, ce, block, page, slc=slc)
    return rec, ecc_of_record(rec, ce, block, page)


@dataclasses.dataclass(frozen=True)
class BlockEcc:
    """What an ECC scan of one block measured.

    `state` is the worst state any sampled page reached (pagefmt.worse_ecc), so
    one uncorrectable page among a thousand clean ones still decides the
    colour.  `page_states` keeps the whole distribution by PAGE -- the colour is
    a pointer, this is the claim -- and it is keyed by page number rather than
    tallied so that two scans of the same block can be merged without counting
    the same page twice: "12 of 1104 pages measured" has to stay true after a
    re-scan, or the sample size stops meaning anything.
    """

    ce: int
    block: int
    slc: bool
    state: EccState
    page_states: dict = dataclasses.field(default_factory=dict)  # page -> state
    pages: int = 0
    corrected_bits: int = 0
    worst_syndrome: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def sampled(self) -> int:
        return len(self.page_states)

    @property
    def counts(self) -> dict:
        """state -> how many sampled pages reached it."""
        out: dict[int, int] = {}
        for st in self.page_states.values():
            out[int(st)] = out.get(int(st), 0) + 1
        return out

    def sentence(self) -> str:
        """The hover line.  Every state that occurred is named with its count;
        a state that quietly disappears is a state the reader stops looking for.
        """
        head = ("ce=%d block=%d ECC: %s -- %d of %d pages measured"
                % (self.ce, self.block, pagefmt.ECC_SHORT[self.state],
                   self.sampled, self.pages))
        if self.error:
            return head + "  (" + str(self.error) + ")"
        parts = ["%s x%d" % (pagefmt.ECC_SHORT[EccState(int(k))], v)
                 for k, v in sorted(self.counts.items(), reverse=True) if v]
        if parts:
            head += "  [" + ", ".join(parts) + "]"
        if self.worst_syndrome:
            head += "  worst syndrome %d" % self.worst_syndrome
        if self.corrected_bits:
            head += "  %d bits corrected" % self.corrected_bits
        return head


def _sample_pages(n_pages: int, sample: int) -> list[int]:
    """Evenly spaced page indices, page 0 always included.

    Evenly spaced rather than random so a re-scan of the same block measures
    the same pages: a colour that changes when nothing changed is a colour
    nobody can act on.
    """
    n_pages = max(0, int(n_pages))
    sample = max(1, int(sample))
    if n_pages == 0:
        return []
    if sample >= n_pages:
        return list(range(n_pages))
    step = n_pages / float(sample)
    out = sorted({min(n_pages - 1, int(i * step)) for i in range(sample)})
    return out


def scan_block_ecc(stores: Stores, ce: int, block: int, slc: bool,
                   sample: int = ECC_SCAN_PAGES) -> BlockEcc:
    """Measure the ECC of a sample of one block's pages.  Worker thread only.

    Never raises: a failure lands in .error with whatever was measured before
    it, because a scan that half-succeeded still knows more than the map did.
    """
    t0 = time.perf_counter()
    n_pages = pages_per_block(bool(slc))
    page_states: dict[int, int] = {}
    state = EccState.NOT_MEASURED
    corrected = 0
    worst = 0
    error: str | None = None

    # Read first, decode once.  The reads are per page and cannot be batched --
    # they are seeks into the store -- but the corrector is a batch engine, so
    # the whole sample goes through it in one call.  Page by page it was four
    # codewords at a time on a machine with 64 of them idle.
    pages, items = [], []
    for page in _sample_pages(n_pages, sample):
        try:
            rec = fetch_page(stores, ce, block, page, slc=bool(slc))
        except Exception as exc:                             # noqa: BLE001
            error = "the scan stopped reading at page %d: %s: %s" % (
                page, type(exc).__name__, exc)
            break
        # A record with no bytes is answered by `ecc_of_record` and never
        # reaches the batch.  Those verdicts are facts about the CAPTURE -- a
        # zero-length ERASED record means the media read 0xFF, while SKIPPED
        # and ABSENT mean nobody asked -- and only the record knows which.
        # Handing `rec.data` (None) to the page decoder instead turned every
        # zero-length ERASED record into NOT_ATTEMPTED, which is the exact
        # merge `ecc_of_record`'s docstring exists to prevent.
        if (rec is None or rec.data is None
                or PageState(int(rec.state)) is PageState.ERASED):
            ecc = ecc_of_record(rec, ce, block, page)
            page_states[int(page)] = int(ecc.state)
            state = pagefmt.worse_ecc(state, ecc.state)
            corrected += int(ecc.corrected_bits)
            worst = max(worst, int(ecc.max_syndrome))
            continue
        pages.append(int(page))
        items.append((rec.data, int(ce), int(block), int(page)))
    if items:
        try:
            results = pagefmt.ecc_of_pages(items)
        except Exception as exc:                             # noqa: BLE001
            # ecc_of_pages does not raise for a page -- an unreachable decoder
            # is a result, not an exception -- so this is the batch itself
            # failing and it costs the whole sample rather than one page.
            results = []
            error = "the scan stopped decoding: %s: %s" % (
                type(exc).__name__, exc)
        for page, ecc in zip(pages, results):
            page_states[page] = int(ecc.state)
            state = pagefmt.worse_ecc(state, ecc.state)
            corrected += int(ecc.corrected_bits)
            worst = max(worst, int(ecc.max_syndrome))
    return BlockEcc(ce=int(ce), block=int(block), slc=bool(slc), state=state,
                    page_states=page_states, pages=n_pages,
                    corrected_bits=corrected, worst_syndrome=worst,
                    seconds=time.perf_counter() - t0, error=error)


# ---------------------------------------------------------------------------
# pure helpers -- any thread, no I/O
# ---------------------------------------------------------------------------
def byte_histogram(data: bytes) -> np.ndarray:
    """-> uint32[256].  np.bincount over a uint8 view; no Python loop."""
    if not data:
        return np.zeros(256, dtype=np.uint32)
    arr = np.frombuffer(data, dtype=np.uint8)
    return np.bincount(arr, minlength=256).astype(np.uint32)


def entropy(data: bytes) -> float:
    """Shannon entropy in bits/byte, 0..8."""
    if not data:
        return 0.0
    hist = byte_histogram(data).astype(np.float64)
    p = hist[hist > 0] / float(len(data))
    bits = float(-np.sum(p * np.log2(p)))
    return 0.0 if bits == 0.0 else bits      # a uniform page is 0.0, not -0.0


def is_all_zero(data: bytes) -> bool:
    """The dead-CE bus constant.  Not erased media -- see PageState.ZERO."""
    if not data:
        return False
    return not np.any(np.frombuffer(data, dtype=np.uint8))


def is_all_ff(data: bytes) -> bool:
    if not data:
        return False
    return bool(np.all(np.frombuffer(data, dtype=np.uint8) == 0xFF))


_PRINTABLE = bytes((c if 32 <= c < 127 else 0x2E) for c in range(256))


def hex_dump(data: bytes, offset: int = 0, rows: int = 32,
             width: int = 16, base: int = 0) -> str:
    """Classic offset/hex/ascii text for `rows` lines starting at `offset`."""
    if not data or rows <= 0 or width <= 0:
        return ""
    offset = max(0, int(offset))
    out = []
    for r in range(int(rows)):
        start = offset + r * width
        if start >= len(data):
            break
        chunk = data[start:start + width]
        hexpart = " ".join("%02x" % b for b in chunk)
        hexpart = hexpart.ljust(width * 3 - 1)
        text = bytes(chunk).translate(_PRINTABLE).decode("ascii", "replace")
        out.append("%08x  %s  |%s|" % (base + start, hexpart, text))
    return "\n".join(out)


def pages_per_block(slc: bool) -> int:
    return PAGES_SLC if slc else PAGES_TLC


def _name_at(names: np.ndarray, idx: int, fallback: str) -> str:
    if names is None or idx < 0 or idx >= len(names):
        return fallback
    try:
        return str(names[idx])
    except Exception:                                        # noqa: BLE001
        return fallback


def describe_block(summary: Summary, ce: int, block: int) -> str:
    """The hover/status sentence.

    It reports the five page-state counts separately and always.  The map cell
    is one colour chosen by precedence, so the colour is a pointer, never the
    claim; this sentence is where the claim lives.  A count of zero is printed
    too -- a state that quietly disappears from the text is a state the reader
    stops looking for.
    """
    if summary is None:
        return "ce=%d block=%d: no summary loaded" % (ce, block)
    if not summary.valid(ce, block):
        return "ce=%d block=%d: outside the %dx%d grid" % (ce, block, N_CE,
                                                           N_BLOCK)
    i = summary.index(ce, block)
    slc = bool(summary.slc[i])
    total = int(summary.pages[i])
    prog = int(summary.programmed[i])
    erz = int(summary.erased[i])
    skip = int(summary.skipped[i])
    absent = int(summary.absent[i])
    zero = int(summary.zero[i])
    weru = int(summary.weru[i])
    owner = int(summary.owner[i])
    binid = int(summary.binid[i])
    erases = int(summary.erases[i])
    defect = int(summary.defect[i])

    parts = [
        "ce=%d block=%d %s" % (ce, block, "pseudo-SLC" if slc else "native TLC"),
        "%d pages" % total,
        "programmed=%d erased=%d skipped=%d absent=%d" % (prog, erz, skip,
                                                          absent),
        # ZERO is a per-block flag in the summary, not a count, and it must be
        # spelled out as the bus constant so it is never read as erased media.
        "all-zero=%s" % ("yes (uniform 0x00 bus constant, NOT erased media)"
                         if zero else "no"),
    ]
    parts.append("weru=%s" % (weru if weru >= 0 else "none"))
    # Name the owning partition. An msoId alone is unreadable -- 44 is
    # Migration/MediaSpace1-NSID2, which owns most of this drive, and 57 is
    # NeoFwPartition/System, which owns the firmware area. The names come from
    # the embedded msoIdTable (a firmware constant), so this cannot go stale,
    # and msonames says "dynamic ... slot N" rather than inventing a static
    # name for the ids the table leaves unassigned.
    parts.append("owner=%s" % (msonames.mso_label(owner) if owner >= 0
                               else "none"))
    # binid 53 is the WERU table's own "in no bin" sentinel, same meaning as -1.
    if binid < 0 or binid >= WERU_BIN_COUNT:
        parts.append("bin=none")
    else:
        parts.append("bin=%d (%s)" % (binid, _name_at(summary.bin_names, binid,
                                                      "?")))
    # -1 is "we never learned the erase count", which is not "erased 0 times".
    parts.append("erases=%s" % (erases if erases >= 0 else "unknown"))
    if defect:
        parts.append("defect=%d (%s)" % (defect,
                                         _name_at(summary.status_names, defect,
                                                  "?")))
    # The region is part of the address and it decides which decoder applies,
    # so it belongs in the sentence that states the address -- see the note on
    # the decoded counts below, which are only readable once you know it.
    region = pagefmt.region_of(ce, block)
    if region != pagefmt.REGION_UNKNOWN:
        parts.append("region=%s (%s)" % (region, pagefmt.GEOMETRY[region]))
    dec_clean = int(summary.dec_clean[i])
    dec_fail = int(summary.dec_fail[i])
    if dec_clean or dec_fail:
        # summary.npz is the tally of the earlier decode, the one whose only two
        # verdicts were "clean" and "mcrc-failed".  The ECC rebuild splits that
        # second number into five distinct outcomes -- corrected, not-engaged,
        # diverged, latched-repeat, all-zero -- so printing it here unqualified
        # would put a superseded verdict next to a page panel that states the
        # measured one.  The number is reported verbatim; what is added is which
        # decode it is a count of, and where the current answer lives.
        line = ("decoded (summary.npz, the EARLIER decode): %d clean / %d "
                "mcrc-failed -- open a page for this rebuild's ECC verdict"
                % (dec_clean, dec_fail))
        if dec_fail and region == pagefmt.REGION_FW_GROUP2:
            # decoded.store decoded the whole drive as 4 AUs x 6-byte MCRC, so
            # every group-2 page in it is quarantined by construction: the
            # FORMAT_A codec cannot pass a page that is five chunks behind
            # 4-byte digests. Printing that tally unqualified next to a page the
            # group-2 checker verifies 5/5 is the same false alarm this viewer
            # was fixed to stop -- one line up instead of one panel over. The
            # store's count is still reported verbatim; what is added is what it
            # is a count of.
            line += (" -- but that tally is the decoded store's, and it decoded "
                     "this block as 4 AUs x 6-byte MCRC, which is not this "
                     "region's geometry; open a page to get the five-chunk "
                     "verdict")
        parts.append(line)
    return "  ".join(parts)


_STATE_WORDS = {
    PageState.PROGRAMMED: "PROGRAMMED (real content)",
    PageState.ERASED: "ERASED (a zero-length record that EXISTS; media reads "
                      "0xFF)",
    PageState.SKIPPED: "SKIPPED (deliberately not read; reading raises)",
    PageState.ABSENT: "ABSENT (no record at all -- we never looked)",
    PageState.ZERO: "ALL-ZERO (uniform 0x00 bus constant, NOT erased media)",
    PageState.UNKNOWN: "NOT PROBED YET",
}


def describe_page(rec: PageRecord) -> str:
    if rec is None:
        return "no page selected"
    head = "ce=%d block=%d page=%d %s: %s" % (
        rec.ce, rec.block, rec.page, "SLC" if rec.slc else "TLC",
        _STATE_WORDS.get(rec.state, str(rec.state)))
    parts = [head]
    # The region is part of the address, so it belongs in the sentence that
    # states the address: which decode rules apply is not something the reader
    # should have to infer from the block number.
    region = pagefmt.region_of(rec.ce, rec.block)
    if region != pagefmt.REGION_UNKNOWN:
        parts.append("region=%s (%s)" % (region, pagefmt.GEOMETRY[region]))
    if rec.length >= 0:
        parts.append("%d bytes" % rec.length)
    if rec.provenance is not None:
        parts.append("source tier=%s: %s" % rec.provenance)
    if rec.reason:
        parts.append(str(rec.reason))
    return "  ".join(parts)
