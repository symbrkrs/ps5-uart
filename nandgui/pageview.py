"""Page inspector: raw hex, the decoded page in its own geometry, the aux
bytes, entropy and the byte histogram.

Every byte on this panel arrived from the worker thread inside a PageBundle.
This module never opens a store, never calls read_page/page_ref/decode, and
never blocks -- it only formats what is already in memory.

The panel is where the five page states are stated in *words*, not just colour:
a colour can be misread, a sentence cannot.  PROGRAMMED / ERASED / SKIPPED /
ABSENT / all-zero each get their own headline, their own palette colour and
their own explanation, and the not-yet-loaded case is a sixth headline so an
unread page can never be mistaken for erased media.

GEOMETRY is per-region, and this panel says which one it used.
The media does not have one page layout, it has three regions with two
geometries, and rendering the wrong one is not a cosmetic error -- it paints a
provably perfect page as corrupt:

  fw-group2   (block 0 on CE 0..5)  five chunks, each with its own 4-byte
                                    digest.  There is no 4-AU view of this page
                                    and there is no 6-byte MCRC in it.
  fw-group01  (blocks 6..9, any CE) 4 AUs x 6-byte MCRC, same as the FTL region.
  ftl         (everything else,     4 AUs x 6-byte MCRC.  NB block 0 on CE 6..11
               incl. block 0 on     is FTL, not group 2 -- the region depends on
               CE 6..11)            the CE as well as the block, so it is only
                                    ever asked for, never re-derived here.

So the decoded section leads with the region, the addressing rule that put this
page in it, the decoder that ran and that decoder's MCRC geometry, and then
draws the shape that region actually has.  The words "MCRC FAIL" belong to a
failed 6-byte MCRC on a 4-AU page and to nothing else: a group-2 page is never
described in the vocabulary of a geometry it does not have.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math

import numpy as np
from imgui_bundle import imgui

from . import model, msonames, pagefmt, palette
from .model import PageState
from .worker import JOB_PAGE, PRIORITY_INTERACTIVE, Result, Worker

# Hex layout is fixed at 16 bytes/row: 18432 raw bytes -> 1152 rows, 4096 user
# bytes -> 256 rows, and both are clipped so only visible rows are formatted.
HEX_WIDTH = 16

# ASCII column: printable stays, everything else becomes '.'.  A translate
# table beats a per-byte Python conditional and keeps the visible-row cost flat.
_ASCII_TABLE = bytes(b if 32 <= b < 127 else 0x2E for b in range(256))

# draw_hex() scrolls to state.hex_offset only when that offset *changed*;
# otherwise the user could never scroll away from it.  The "last applied"
# value lives on the state object under a private attribute rather than in a
# module dict keyed by id(), which would leak and could alias a freed state.
_SCROLL_ATTR = "_pageview_scroll_applied"

_CORRECT_LABELS = (
    "measure (correct=None)",
    "no correction (correct=False)",
    "full correction (correct=True, ~100x)",
)
_CORRECT_VALUES = (None, False, True)

# The headline for each state.  Wording matters as much as colour here: these
# five are the states this project has already merged twice, so each line says
# what the state is and what it is not.
_STATE_HEADLINE = {
    PageState.PROGRAMMED: "PROGRAMMED",
    PageState.ERASED: "ERASED",
    PageState.SKIPPED: "SKIPPED",
    PageState.ABSENT: "ABSENT",
    PageState.ZERO: "ALL-ZERO",
    PageState.UNKNOWN: "NOT PROBED",
}

_STATE_SENTENCE = {
    PageState.PROGRAMMED: "real content read from the media.",
    PageState.ERASED: (
        "a zero-length record that EXISTS -- the media reads 0xFF. "
        "This is clean media we looked at, not a hole."
    ),
    PageState.SKIPPED: (
        "deliberately not read, with a recorded reason. read_page() RAISES "
        "for this page; nothing here says what the media holds."
    ),
    PageState.ABSENT: (
        "no record at all -- we never looked. A HOLE in the dump, NOT clean "
        "media and NOT erased."
    ),
    PageState.ZERO: (
        "uniform 0x00 (the dead-CE bus constant), NOT erased media. Erased "
        "NAND reads 0xFF; a page of zeros is either real zero content or a "
        "bus that answered nothing."
    ),
    PageState.UNKNOWN: (
        "not probed yet -- the worker has not answered. Nothing is known "
        "about this page; do not read the blank panel as empty media."
    ),
}


# --------------------------------------------------------------------------
# region vocabulary -- all of it comes from pagefmt
# --------------------------------------------------------------------------
# pagefmt owns the region map, the addressing rules, the decoder names, the
# unit nouns and the MCRC widths, and model.py already depends on it.  This
# panel keeps no copy of any of that: a second table of "which region is block
# 0 in" is exactly how a renderer ends up disagreeing with the decoder it is
# supposed to be reporting on.  What this module owns is the wording around
# those facts and where they land on screen.
REGION_FW_GROUP2 = pagefmt.REGION_FW_GROUP2
REGION_FW_GROUP01 = pagefmt.REGION_FW_GROUP01
REGION_FTL = pagefmt.REGION_FTL
REGION_UNKNOWN = pagefmt.REGION_UNKNOWN


def region_known(region: str) -> bool:
    """False for "", for pagefmt.REGION_UNKNOWN and for anything unrecognised.

    An unknown region is drawn as unknown.  There is no default: "assume ftl"
    is the assumption that painted 1840 verified group-2 pages red.
    """
    return bool(region) and region in pagefmt.REGIONS


# --------------------------------------------------------------------------
# monospace font (V4)
# --------------------------------------------------------------------------
# The font itself is loaded and owned by the shell; mono_scope() (below, next
# to draw_hex) is the one place that pushes it.  Hex is a column format: in a
# proportional font the ASCII gutter does not line up with the bytes above it
# and an offset cannot be read off by eye, so every hex and ASCII run on this
# panel -- the aux bytes and the group-2 digests as much as the two hexdumps --
# goes through here.


def _mono_text(text: str) -> None:
    """One line of fixed-pitch text.  Uses mono_scope() rather than a second
    push of its own: two font stacks in one module is how one of them ends up
    unbalanced."""
    with mono_scope():
        imgui.text_unformatted(text)




@dataclasses.dataclass
class InspectorState:
    target: tuple[int, int, int, bool] | None = None   # (ce, block, page, slc)
    record: model.PageRecord | None = None
    decoded: model.Decoded | None = None
    entropy: float = -1.0
    histogram: np.ndarray | None = None                # uint32[256]
    hex_offset: int = 0
    au: int = 0                                        # which AU is expanded
    chunk: int = 0                                     # which group-2 chunk is expanded
    show_user: bool = True                             # user bytes vs aux only
    show_payload: bool = True                          # group-2 payload hexdump
    correct: bool | None = None                        # codec correct= override
    loading: bool = False
    seq: int = -1
    error: str | None = None
    #: geometry.region(ce, block) for the selected page, resolved on the worker
    #: thread.  "" means nobody could tell us -- rendered as "region unknown",
    #: never as a default region: guessing "ftl" is exactly how a group-2 page
    #: gets read with the wrong geometry.
    region: str = ""
    #: The group-2 verdict (five chunks x 4-byte digest), normalised.  None for
    #: every other region -- and also for a group-2 page whose reader was not
    #: reachable, which is why the panel says so instead of falling back to a
    #: 4-AU view of bytes that are not four AUs.
    group2: dict | None = None
    #: {'layout', 'layout_why', 'entries'} for the aux bytes.  layout None means
    #: UNKNOWN: no MSO descriptor source is loaded, so the 8 bytes stay bytes.
    aux: dict | None = None
    #: Was a decoder asked about this page at all?  False for every state that
    #: carries no bytes -- ABSENT, ERASED, SKIPPED, not probed -- and for a
    #: caller that requested no decode.  Without it "we did not ask" and "we
    #: asked and the reader could not be reached" are the same None, and the
    #: group-2 panel printed a tooling failure for pages nobody had asked it
    #: about (all 368 pages of CE0's block 0 are ABSENT, and every one of them
    #: is a group-2 address).
    decode_attempted: bool = False
    #: The ECC verdict for this page -- separate from the MCRC one, always, and
    #: never derived from it.  None means the bundle predates the ECC panel;
    #: "no ECC verdict" is EccState.NOT_ATTEMPTED, which is a state with words.
    ecc: model.EccResult | None = None
    #: Run the ECC decoder on selection.  On by default: hiding the verdict is
    #: what this panel was rewritten to stop.  It is a toggle because an
    #: uncorrectable page costs ~90-215 ms to prove uncorrectable, and someone
    #: stepping through a bad block may want that back.
    run_ecc: bool = True
    #: Show the ecc-corrected raw page instead of the page as read.  Off by
    #: default: the bytes the drive returned are the primary evidence, and a
    #: corrected page must never be mistaken for them.
    show_corrected: bool = False


@dataclasses.dataclass(frozen=True)
class InspectorEvent:
    requested: tuple[int, int, int, bool] | None = None  # user stepped page
    copy_hex: bool = False
    save_raw: bool = False


@dataclasses.dataclass(frozen=True)
class PageBundle:
    record: model.PageRecord
    decoded: model.Decoded | None
    entropy: float
    histogram: np.ndarray
    # Defaulted so an older/newer producer of this bundle still constructs.
    region: str = ""
    group2: dict | None = None
    aux: dict | None = None
    #: True when a decoder was actually run for this page.  See
    #: InspectorState.decode_attempted -- it is the discriminator that keeps
    #: "not decoded" from being reported as "the reader is broken".
    decode_attempted: bool = False
    #: The ECC verdict, measured on the worker thread with the rest of the page
    #: so the panel never shows an ECC state from one address next to bytes
    #: from another.
    ecc: model.EccResult | None = None


# --------------------------------------------------------------------------
# worker-thread resolution: which region, which geometry, which aux layout
# --------------------------------------------------------------------------
# Everything in this section runs inside the job closure, on the worker thread.
# titania lives on a WSL share and is imported lazily by model for exactly that
# reason, so it is only ever reached here through model's own accessor -- never
# imported at the top of a view module, and never on the UI thread.

def region_of(ce: int, block: int, dec=None) -> str:
    """-> pagefmt.REGION_* for this address.  Worker thread; never raises.

    Asked, never derived.  Block 0 is group 2 on CE 0..5 and plain FTL media on
    CE 6..11, so any rule that reads the block number alone mis-files six CEs
    worth of block 0 -- which is precisely the failure this panel exists to
    stop.  A decode that already carries a region label wins, because that is
    the region the bytes on screen were actually run through.
    """
    if dec is not None:
        named = getattr(dec, "region", "")
        if named:
            return str(named)
    try:
        return str(model.region_of(ce, block))
    except Exception:                                        # noqa: BLE001
        return ""


def _as_mapping(obj):
    """A real mapping, or None.  Dicts only -- never an object's __dict__.

    Decoded is a plain (non-slotted) dataclass, so its __dict__ is a perfectly
    good-looking mapping with the wrong keys: `mcrc` there is the per-unit
    verdict tuple (True, True, ...), not check_group2's (n_ok, n_total), and
    reading it as counts turned five verified chunks into "1/1 verify" on
    screen.  Same-named, different-meaning fields is exactly why this only
    accepts something that really is the checker's dict.
    """
    return obj if isinstance(obj, dict) else None


def _payloads_of(res, src, n: int) -> list:
    """The decoded user DATA, one entry per chunk, index-aligned with `chunks`.

    Two producers, one shape.  pagefmt._decode_group2 hangs the 2048 descrambled
    payload bytes of each chunk on its Unit.data (that is where
    fw.decode_group2_page's 10240-byte plaintext ends up), and
    model.check_group2 re-exports the same bytes as a 'payloads' list for the
    path where only the dict survives.  Neither is re-sliced here: a second cut
    with its own idea of the 2048-byte stride is how two panels come to show
    different bytes for the same chunk.

    A missing or short entry is None, which means no PAYLOAD was produced -- the
    plaintext needs the row seed and the scrambler while the digests need
    neither, so a structural verdict can stand with no payload behind it. None
    is never "the payload is empty".
    """
    out: list = [None] * int(n)
    units = getattr(res, "units", None) or ()
    for i, u in enumerate(units):
        if i >= len(out):
            break
        data = getattr(u, "data", None)
        if data:
            out[i] = bytes(data)
    if any(p is not None for p in out):
        return out
    for i, p in enumerate(src.get("payloads") or ()):
        if i >= len(out):
            break
        if p:
            out[i] = bytes(p)
    return out


def _normalize_group2(res) -> dict | None:
    """fw.check_group2_page()'s dict -> the shape _draw_group2 renders.

    Anything missing stays None ("not reported"), never False: "the digests do
    not agree" and "nobody told us whether they agree" are different verdicts
    and only one of them is a defect.

    The decoded PAYLOAD travels with the verdict, and its entropy and byte
    histogram are computed HERE -- on the worker thread, once per page -- for the
    same reason the raw page's are: the frame loop formats what is already in
    memory and measures nothing.
    """
    src = _as_mapping(res)
    if src is None:
        # A Decoded (or a pagefmt.Outcome) carrying the fields as attributes.
        # Its whole-page verdict is called region_ok there and ok in
        # check_group2's dict; both mean "the region's own checker was happy".
        if res is not None and getattr(res, "chunks", None):
            src = {
                "chunks": getattr(res, "chunks", None),
                "digest_consistent": getattr(res, "digest_consistent", None),
                "tail_ff": getattr(res, "tail_ff", None),
                "erased": getattr(res, "erased", None),
                "size": getattr(res, "size", None),
                "ok": getattr(res, "region_ok", getattr(res, "ok", None)),
                "notes": getattr(res, "notes", ()),
                "mcrc": getattr(res, "mcrc_counts", None),
            }
        else:
            return None
    raw_chunks = src.get("chunks") or ()
    payloads = _payloads_of(res, src, len(raw_chunks))
    chunks = []
    for i, c in enumerate(raw_chunks):
        cm = _as_mapping(c)
        if cm is None:
            cm = {"digest": getattr(c, "digest", None),
                  "mcrc_ok": getattr(c, "mcrc_ok", None),
                  "erased": getattr(c, "erased", None),
                  "message_sha1": getattr(c, "message_sha1", None)}
        payload = payloads[i] if i < len(payloads) else None
        chunks.append({
            "digest": cm.get("digest"),
            "mcrc_ok": cm.get("mcrc_ok"),
            "erased": cm.get("erased"),
            "message_sha1": cm.get("message_sha1"),
            # None = no payload was produced. b"" would be a payload of zero
            # bytes, which is a different (and here impossible) fact.
            "payload": payload,
            "entropy": (model.entropy(payload) if payload else -1.0),
            "hist": (model.byte_histogram(payload) if payload else None),
        })
    if not chunks:
        return None
    payload_total = sum(len(c["payload"]) for c in chunks if c["payload"])
    payload_n = sum(1 for c in chunks if c["payload"])
    counts = src.get("mcrc")
    if counts is None:
        counts = src.get("mcrc_counts")
    try:
        counts = (int(counts[0]), int(counts[1])) if counts is not None else None
    except Exception:                                        # noqa: BLE001
        counts = None
    if counts is None:
        # Count only the chunks that were checked. len(chunks) would claim five
        # digests were verified whenever the checker reported no counts of its
        # own, so five erased chunks came out as "0/5 verify" -- five failures
        # invented out of five checks that never ran. mcrc_ok is None for an
        # erased chunk precisely so it can be excluded here.
        checked = [c for c in chunks if c["mcrc_ok"] is not None]
        counts = (sum(1 for c in checked if c["mcrc_ok"] is True), len(checked))
    notes = src.get("notes") or ()
    return {
        "chunks": tuple(chunks),
        "digest_consistent": src.get("digest_consistent"),
        "tail_ff": src.get("tail_ff"),
        "erased": src.get("erased"),
        "size": src.get("size"),
        "ok": src.get("ok"),
        "counts": counts,
        "notes": tuple(str(n) for n in notes),
        #: Total decoded payload for the page, and how many chunks contributed
        #: it. MEASURED on ce1/block0/page0: 5 chunks x 2048 = 10240 bytes out of
        #: an 18432-byte raw page. The two numbers are kept apart so "10240 from
        #: five chunks" cannot be confused with "10240 from one".
        "payload_total": payload_total,
        "payload_chunks": payload_n,
    }


def check_group2(stores, raw: bytes, ce: int, block: int, page: int,
                 dec=None) -> dict | None:
    """The group-2 verdict for this page, or None if no reader was reachable.

    Worker thread; never raises.  None is not "the page is bad" -- it means the
    five-chunk reader could not be reached, and the panel says that rather than
    describing the page with the four-AU vocabulary it does not fit.
    """
    got = _normalize_group2(dec)
    if got is not None:
        return got
    try:
        res = model.check_group2(stores, raw, ce, block, page)
    except Exception:                                        # noqa: BLE001
        # model.check_group2 documents "never raises", but a panel that trusts
        # that and is wrong loses the whole page, not just the verdict.
        res = None
    return _normalize_group2(res)


#: The one sentence for "we have no descriptor, so the layout is not known".
#: pagefmt.aux_layout_of() is what answers (None, None) here; the wording is
#: this panel's, and it names where the answer would have come from so the gap
#: reads as a missing input rather than as a broken decode.
_AUX_NO_SOURCE = (
    "no MSO descriptor source is loaded, so pagefmt.aux_layout_of() answers "
    "UNKNOWN. The layout is descriptor byte +17 of the OWNING MSO "
    "(aux.MSO_DESC_AUX_LAYOUT_OFF) and is per-drive configuration, so there is "
    "nothing to fall back to")


def _aux_view(dec, owner=None) -> dict:
    """{'layout', 'layout_name', 'layout_why', 'entries'} for the aux fields.

    All four layouts are 8 bytes wide, so the bytes cannot tell you which one
    they are in -- the owning MSO's descriptor does, and with no descriptor
    source pagefmt.aux_layout_of() answers (None, None).  UNKNOWN is then what
    this reports.  The 8 bytes are still shown; what is withheld is a set of
    confident field names for fields that may not be there.

    Worker thread: pagefmt.parse_aux reaches titania.auxdata.
    """
    view = {"layout": None, "layout_name": "UNKNOWN",
            "layout_why": _AUX_NO_SOURCE, "entries": None,
            # The owning partition is what selects the layout, so name it here
            # rather than leaving the reader to map an msoId by hand: byte +17
            # of this mso's descriptor is the whole answer.
            "owner": None if owner is None else int(owner),
            "owner_label": (None if owner is None
                            else msonames.mso_label(owner))}
    if dec is None:
        return view
    layout = getattr(dec, "aux_layout", None)
    if layout is None and owner is not None:
        try:
            layout, _name = pagefmt.aux_layout_of(owner, None)
        except Exception:                                    # noqa: BLE001
            layout = None
    if layout is None:
        return view
    view["layout"] = int(layout)
    view["layout_name"] = pagefmt.aux_layout_name(int(layout))
    view["layout_why"] = (str(getattr(dec, "aux_layout_why", "")) or
                          "from the owning MSO's descriptor (byte +17)")
    aus = getattr(dec, "aus", None)
    if aus:
        entries = []
        for i in range(len(aus)):
            entries.append(pagefmt.parse_aux(dec.aux(i), view["layout"], au=i))
        view["entries"] = tuple(entries)
    return view


# --------------------------------------------------------------------------
# worker plumbing
# --------------------------------------------------------------------------

def request(state: InspectorState, worker: Worker,
            target: tuple[int, int, int, bool], decode: bool = True,
            owner: int | None = None) -> None:
    """Submit key (JOB_PAGE,) at PRIORITY_INTERACTIVE: fetch_page, then the
    decode this page's region calls for. One job returns a PageBundle so the
    panel never shows half a page.

    The region is resolved first because it decides which decoder is even
    meaningful.  For fw-group2 the four-AU paths are skipped outright: both of
    them split any record into 4 x 4104 unconditionally, so decoded.store's
    cached record for a group-2 page is a four-AU view of bytes that are five
    chunks -- and with the suspect tier on it comes back stamped
    "mcrc-failed" for a page whose five digests all verify.
    """
    ce, block, page, slc = int(target[0]), int(target[1]), int(target[2]), bool(target[3])
    correct = state.correct          # captured now: the closure must not read
                                     # mutable UI state from the worker thread.
    run_ecc = bool(state.run_ecc)    # same reason
    # The aux layout is a property of the owning MSO, and the owner lives in the
    # summary, which is UI-thread state -- so it is captured here and passed in.
    # Without it `_aux_view` answered UNKNOWN before ever consulting the
    # descriptor, which is why the panel still said "no MSO descriptor source is
    # loaded" after the descriptor was on disk and loading correctly.
    owner_id = None if owner is None else int(owner)

    def job(ctx: object) -> PageBundle:
        stores = ctx
        rec = model.fetch_page(stores, ce, block, page, slc)
        dec = None
        group2 = None
        attempted = False
        region = region_of(ce, block)
        # Only pages that actually carry bytes can be decoded.  ERASED /
        # SKIPPED / ABSENT are not decode failures, they are non-reads, and
        # asking the codec about them would manufacture a verdict out of
        # nothing.
        if decode and rec.state in (PageState.PROGRAMMED, PageState.ZERO):
            if region == REGION_FW_GROUP2:
                if rec.data is not None:
                    # Straight to the raw bytes: decoded.store's record for a
                    # group-2 page is a 4 x 4104 split of bytes that are five
                    # chunks, and fetch_decoded would hand back that fabricated
                    # shape stamped with the quarantined tier's "mcrc-failed".
                    attempted = True
                    dec = _decode_raw(stores, rec.data, ce, block, page,
                                      correct)
                    group2 = check_group2(stores, rec.data, ce, block, page,
                                          dec)
            else:
                attempted = True
                dec = model.fetch_decoded(stores, ce, block, page, slc)
                # needs_raw_decode() is the region-aware "is that record usable
                # here": a cached record whose geometry does not apply to this
                # address has to be decoded again, not drawn.
                if model.needs_raw_decode(dec) and rec.data is not None:
                    dec = _decode_raw(stores, rec.data, ce, block, page,
                                      correct)
                group2 = _normalize_group2(dec)
        if not region:
            region = region_of(ce, block, dec)
        # The ECC is its own measurement and it runs for every state.  It is
        # not gated on `decode`, because "this page has no bytes" and "this
        # page is 18432 zeros" are ECC answers in their own right (NOT_ATTEMPTED
        # and ZERO_PAGE) and the panel has to be able to say them.  It goes
        # through model.ecc_of_record, which decides those from the record, and
        # never through titania_nand.ecc_correct_page -- see the complement trap
        # in pagefmt.
        if run_ecc:
            ecc = model.ecc_of_record(rec, ce, block, page, region=region)
        else:
            ecc = model.EccResult(
                state=model.EccState.NOT_ATTEMPTED, region=region,
                code=pagefmt.ECC_CODE.get(region, ""),
                error="the ECC decoder was switched off for this selection "
                      "(the 'run ECC' checkbox), so nothing was measured")
        data = rec.data
        if data:
            ent = model.entropy(data)
            hist = model.byte_histogram(data)
        else:
            ent = -1.0
            hist = np.zeros(256, dtype=np.uint32)
        return PageBundle(record=rec, decoded=dec, entropy=ent, histogram=hist,
                          region=region, group2=group2,
                          aux=_aux_view(dec, owner_id),
                          decode_attempted=attempted, ecc=ecc)

    state.target = (ce, block, page, slc)
    # Drop the old page's bytes immediately: showing the previous page's hex
    # under a new address would be a lie the user cannot detect.
    state.record = None
    state.decoded = None
    state.entropy = -1.0
    state.histogram = None
    state.hex_offset = 0
    # The chunk selector belongs to the page that is going away: chunk 3 of the
    # previous page is not chunk 3 of this one, and a page with fewer chunks
    # would leave the selector pointing past its own data.
    state.chunk = 0
    state.error = None
    state.region = ""
    state.group2 = None
    state.aux = None
    state.decode_attempted = False
    # The previous page's ECC verdict belongs to the previous page.  Keeping it
    # for one frame would caption these bytes with another address's outcome --
    # the same class of error as showing its hex.
    state.ecc = None
    state.show_corrected = False
    state.loading = True
    state.seq = worker.submit((JOB_PAGE,), job, priority=PRIORITY_INTERACTIVE)


def _decode_raw(stores, raw: bytes, ce: int, block: int, page: int,
                correct: bool | None):
    """model.decode_raw, with the ce.

    One line, but it exists so that every raw decode in this file goes through
    a call that cannot be written without a CE.  Block 0 is fw-group2 on CE 0..5
    and ordinary FTL media on CE 6..11, so a decode_raw call that omits `ce`
    does not fail -- it silently answers "region not determined" and runs
    FORMAT_A, which is the exact bug this panel was rewritten to stop.
    """
    return model.decode_raw(stores, raw, block, page, correct=correct, ce=ce)


def apply(state: InspectorState, result: Result) -> bool:
    """Consume our Result (a PageBundle) if not stale; returns True if used."""
    if result.key != (JOB_PAGE,):
        return False
    if state.seq < 0 or result.seq < state.seq:
        # A decode that lands after the user moved on must never overwrite the
        # newer selection.
        return False
    state.loading = False
    if result.error is not None:
        state.error = result.error
        state.record = None
        state.decoded = None
        state.entropy = -1.0
        state.histogram = None
        # The region verdict belongs to the page that failed to load; keeping
        # it would caption the next page with the previous one's geometry.
        state.region = ""
        state.group2 = None
        state.aux = None
        state.decode_attempted = False
        state.ecc = None
        return True
    bundle = result.value
    if not isinstance(bundle, PageBundle):
        state.error = f"page job returned {type(bundle).__name__}, expected PageBundle"
        return True
    state.error = None
    state.record = bundle.record
    state.decoded = bundle.decoded
    state.entropy = bundle.entropy
    state.histogram = bundle.histogram
    state.region = getattr(bundle, "region", "") or ""
    state.group2 = getattr(bundle, "group2", None)
    state.aux = getattr(bundle, "aux", None)
    state.decode_attempted = bool(getattr(bundle, "decode_attempted", False))
    state.ecc = getattr(bundle, "ecc", None)
    return True


# --------------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _id_scope(label: str):
    """push_id/pop_id that survives an exception in the body.

    Without this the panel's push_id at the top of draw() was never popped when
    anything below it raised: app._draw_page catches the exception into an
    on-screen panel, so the frame carries on with an unbalanced ID stack and
    imgui.end() for the host window asserts ("Missing PopID()") -- taking down
    the pinned five-state legend and the status bar, in a way _draw_view_error
    could not report because the raise happened after it.  mapview.draw and
    blockview.draw already unwind this way.
    """
    imgui.push_id(label)
    try:
        yield
    finally:
        imgui.pop_id()


@contextlib.contextmanager
def _child_scope(*args, **kwargs):
    """begin_child/end_child that survives an exception in the body.

    end_child() must run even on the error path or imgui's own recovery fires
    somewhere unrelated, several widgets later, hiding the real traceback.
    """
    imgui.begin_child(*args, **kwargs)
    try:
        yield
    finally:
        imgui.end_child()


def _vec4(rgba) -> imgui.ImVec4:
    r, g, b, a = palette.imgui_color(tuple(int(v) for v in rgba))
    return imgui.ImVec4(r, g, b, a)


def _state_color(st: PageState) -> imgui.ImVec4:
    # Straight out of the palette: the inspector and the map must never
    # disagree about what a state looks like.
    return _vec4(palette.STATE_RGBA[int(st)])


def _swatch(col: imgui.ImVec4, tip: str) -> None:
    h = imgui.get_text_line_height()
    imgui.color_button("##swatch", col, 0, imgui.ImVec2(h, h))
    if imgui.is_item_hovered():
        imgui.set_tooltip(tip)


def _hex_row(data: bytes, row: int, width: int, base: int) -> str:
    chunk = data[row * width:(row + 1) * width]
    hex_part = chunk.hex(" ").upper()
    pad = width * 3 - 1
    ascii_part = chunk.translate(_ASCII_TABLE).decode("ascii")
    return f"{base + row * width:06X}  {hex_part:<{pad}}  {ascii_part}"


def _addr_text(target: tuple[int, int, int, bool] | None) -> str:
    if target is None:
        return "no page selected"
    ce, block, page, slc = target
    return f"CE{ce} block {block} page {page} ({'SLC' if slc else 'TLC'})"


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

def draw(state: InspectorState, label: str = "Page") -> InspectorEvent:
    """Header line is the PageRecord state in its palette colour and words:
    SKIPPED shows the recorded reason and the source tier, ABSENT says 'no
    record -- never read', ZERO says 'uniform 0x00 (bus constant), not erased'.
    Per-AU MCRC renders three-valued: pass / FAIL / unknown; a None mcrc is
    never drawn as pass. UI thread only.

    Like mapview.draw and blockview.draw this emits widgets only: app.frame()
    owns the begin()/end() for the host window.  Opening one here as well
    nested the window inside itself, which imgui tolerates but which put this
    panel's contents in a second window stack entry for no reason."""
    requested: tuple[int, int, int, bool] | None = None
    copy_hex = False
    save_raw = False

    with _id_scope(label):
        requested = _draw_address(state) or requested
        imgui.separator()

        if state.error is not None:
            imgui.text_colored(_vec4(palette.NONE_RGBA), "page job failed")
            imgui.text_wrapped(state.error)
            imgui.separator()

        rec = state.record
        if state.target is None:
            imgui.text_disabled("Click a page in the block stripe, or type an address above.")
        elif rec is None:
            # Loading is its own headline, in the UNKNOWN colour.  A blank panel
            # would read as "nothing there", which is exactly the ABSENT/ERASED
            # confusion this project keeps making.
            st = PageState.UNKNOWN
            _swatch(_state_color(st), palette.STATE_HELP[int(st)])
            imgui.same_line()
            imgui.text_colored(_state_color(st), _STATE_HEADLINE[st])
            imgui.text_wrapped(_STATE_SENTENCE[st])
        else:
            _draw_header(state, rec)
            imgui.separator()
            copy_hex, save_raw = _draw_actions(state, rec)
            _draw_stats(state)
            imgui.separator()
            req = _draw_decoded(state)
            if req is not None:
                requested = req
            imgui.separator()
            _draw_raw(state, rec)

    return InspectorEvent(requested=requested, copy_hex=copy_hex, save_raw=save_raw)


def _draw_address(state: InspectorState) -> tuple[int, int, int, bool] | None:
    """CE / block / page / SLC steppers.  Returns a new target when the user
    moved, so app.py owns the actual re-request."""
    ce, block, page, slc = state.target or (0, 0, 0, False)
    changed = False

    imgui.set_next_item_width(90.0)
    ch, ce = imgui.input_int("CE", int(ce), 1, 1)
    changed |= ch
    imgui.same_line()
    imgui.set_next_item_width(120.0)
    ch, block = imgui.input_int("block", int(block), 1, 100)
    changed |= ch
    imgui.same_line()
    imgui.set_next_item_width(120.0)
    ch, page = imgui.input_int("page", int(page), 1, 16)
    changed |= ch
    imgui.same_line()
    ch, slc = imgui.checkbox("SLC", bool(slc))
    changed |= ch

    n_pages = model.pages_per_block(bool(slc))
    if imgui.small_button("<< page"):
        page -= 1
        changed = True
    imgui.same_line()
    if imgui.small_button("page >>"):
        page += 1
        changed = True
    imgui.same_line()
    imgui.text_disabled(f"{n_pages} pages in a {'pseudo-SLC' if slc else 'native TLC'} block")

    if not changed:
        return None
    ce = max(0, min(11, int(ce)))
    block = max(0, min(3999, int(block)))
    page = max(0, min(n_pages - 1, int(page)))
    target = (ce, block, page, bool(slc))
    return target if target != state.target else None


def _draw_header(state: InspectorState, rec: model.PageRecord) -> None:
    st = PageState(int(rec.state))
    col = _state_color(st)
    _swatch(col, palette.STATE_HELP[int(st)])
    imgui.same_line()
    imgui.text_colored(col, _STATE_HEADLINE[st])
    imgui.same_line()
    imgui.text_disabled(f"-- {palette.STATE_LABELS[int(st)]}")
    imgui.text_wrapped(_STATE_SENTENCE[st])

    if rec.length >= 0:
        imgui.text(f"record length: {rec.length} bytes")
    else:
        imgui.text_disabled("record length: unknown (no bytes read)")

    # SKIPPED carries the reason that justifies the skip; hiding it here is how
    # a metadata-driven skip nearly lost the boot block.
    if rec.reason:
        imgui.text_colored(col, "reason:")
        imgui.same_line()
        imgui.text_wrapped(rec.reason)
    if rec.provenance is not None:
        tier, why = rec.provenance
        imgui.text(f"source tier: {tier}")
        if why:
            # Its own wrapped line: the store's reason runs to ~250 characters
            # ("gbe-dump/: level-0 read over the GbE transport ..."), and
            # text_disabled does not wrap, so on one same_line it set the whole
            # Page window's horizontal scroll extent -- MEASURED 769 px of it,
            # which is what pushed the hexdump out of view.
            _wrapped_disabled(f"({why})")

    # Wrapped, not text_disabled(): this sentence is ~1100 px wide for a typical
    # PROGRAMMED record, and an unwrapped line sets the whole Page window's
    # horizontal scroll extent -- which is what pushed the hexdump off the
    # right edge and made it look truncated.
    _wrapped_disabled(model.describe_page(rec))


def _draw_actions(state: InspectorState, rec: model.PageRecord) -> tuple[bool, bool]:
    copy_hex = False
    save_raw = False
    has_bytes = bool(rec.data)

    if not has_bytes:
        imgui.begin_disabled()
    if imgui.button("Copy hex"):
        copy_hex = True
        if rec.data:
            # Clipboard writing is pure UI-thread work (no I/O), so do it here
            # and still report the event for the status bar.
            imgui.set_clipboard_text(
                model.hex_dump(rec.data, offset=0,
                               rows=(len(rec.data) + HEX_WIDTH - 1) // HEX_WIDTH,
                               width=HEX_WIDTH))
    imgui.same_line()
    if imgui.button("Save raw"):
        save_raw = True
    if not has_bytes:
        imgui.end_disabled()
        imgui.same_line()
        imgui.text_disabled("(no bytes: this state has none to copy)")
    return copy_hex, save_raw


def _draw_stats(state: InspectorState) -> None:
    if state.histogram is None:
        imgui.text_disabled("entropy / histogram: no bytes")
        return
    draw_histogram(state.histogram, state.entropy)


_PASS_COL = imgui.ImVec4(0.35, 0.85, 0.45, 1.0)
_FAIL_COL = imgui.ImVec4(0.95, 0.35, 0.35, 1.0)


def _mcrc_widget(decoded: model.Decoded | None, au: int) -> None:
    """Three-valued verdict for one AU.  mcrc is None means no per-au verdict
    exists -- a third verdict, never rendered as pass.

    The page-level verdict, when the store recorded one, is drawn once by
    _page_mcrc_line() above this row rather than repeated under all four AUs:
    "at least one of the four failed" is one fact, not four.
    """
    if decoded is None or decoded.aus is None:
        imgui.text_colored(_vec4(palette.NONE_RGBA), "no decode")
        return
    if decoded.mcrc is None or au >= len(decoded.mcrc):
        imgui.text_colored(_vec4(palette.NONE_RGBA), "per-AU: not recorded")
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "No PER-AU verdict exists for this page. This is NOT a pass "
                "and NOT a failure for this AU.\n"
                "The page-level verdict is on the line above; re-decode with "
                "the codec (pick a correction mode) to get all four."
            )
        return
    if decoded.mcrc[au]:
        imgui.text_colored(_PASS_COL, "MCRC pass")
    else:
        imgui.text_colored(_FAIL_COL, "MCRC FAIL")


def _page_mcrc_line(decoded: model.Decoded, ecc=None) -> None:
    """The PAGE-level MCRC verdict: pass / FAIL / not recorded.

    decoded.store keeps one verdict per page -- which tier the record lives in
    -- and this panel used to drop it and print "MCRC unknown" for both tiers.
    That reported a store-certified clean page and a quarantined failed page as
    the same third verdict, and turning the suspect tier on (which is meant to
    show more) replaced a real codec FAIL with "unknown".
    """
    verdict = decoded.page_mcrc
    if verdict is None:
        imgui.text_colored(_vec4(palette.NONE_RGBA), "page MCRC: not recorded")
        tip = ("Neither the store nor the codec gave a verdict for this page. "
               "NOT a pass and NOT a failure.")
    elif verdict:
        imgui.text_colored(_PASS_COL, "page MCRC: PASS (all AUs)")
        tip = ("Every AU of this page passed MCRC. From decoded.store this is "
               "the tier the record lives in ('decoded': checked at decode "
               "time); from the codec it is the roll-up of the four verdicts "
               "below.")
    else:
        imgui.text_colored(_FAIL_COL, "page MCRC: FAIL (at least one AU)")
        tip = ("At least one AU of this page failed MCRC. From decoded.store "
               "this page comes out of the quarantined 'decoded-mcrc-failed' "
               "tier, which records that a failure happened but not which AU.")
    if imgui.is_item_hovered():
        imgui.set_tooltip(tip)
    if decoded.provenance is not None:
        tier, why = decoded.provenance
        imgui.same_line()
        imgui.text_disabled(f"  (record source tier: {tier})")
        if imgui.is_item_hovered():
            imgui.set_tooltip(why)
    # The ECC verdict is repeated here, next to the MCRC one, because this is
    # the line a reader glances at.  "MCRC FAIL" on its own has been read as
    # "the ECC failed" and as "the media is gone"; it is neither until the line
    # below says so.
    if ecc is not None:
        imgui.text("ECC (the other verdict):")
        imgui.same_line()
        imgui.text_colored(_ecc_color(ecc.state), ecc.short)
        imgui.same_line()
        imgui.text_disabled("-- see the ECC section above")
        if ecc.state in (pagefmt.EccState.ZERO_PAGE, pagefmt.EccState.ERASED):
            # The codec computes an MCRC over whatever bytes it is given, so a
            # bus constant gets a verdict too.  Say what that verdict is about
            # before it is read as damage.
            _wrapped_disabled(
                "These bytes are not data (%s), so the MCRC verdict above is a "
                "CRC over a constant, not evidence about media. Nothing was "
                "lost here and nothing failed."
                % (pagefmt.ECC_SHORT[ecc.state],))
        if ecc.corrected_bits and decoded.provenance is not None:
            # The AU bytes below came out of decoded.store's own decode, done
            # at capture time; the correction above was measured on the RAW
            # page just now.  Two decodes, and only one of them is on screen.
            _wrapped_disabled(
                "Note: the %d-bit correction above was measured on the RAW "
                "page. The AU bytes below were served by decoded.store from "
                "its own decode at capture time, so they are that decode's "
                "output and not this correction's. Use 'ECC-corrected' under "
                "Raw page to see the bytes this correction produced."
                % (ecc.corrected_bits,))
        if verdict is False and ecc.state in (pagefmt.EccState.CLEAN,
                                              pagefmt.EccState.CORRECTED,
                                              pagefmt.EccState.MCRC_DISAGREES):
            imgui.text_wrapped(
                "This page's MCRC fails while its codewords are valid. That is "
                "NOT the ordinary 'MCRC FAIL' of a damaged page -- read the "
                "ECC section: the two checks cover different bytes.")


def _wrapped_disabled(text: str) -> None:
    """text_disabled that wraps.  text_disabled() does not, and one long
    sentence then sets the whole Page window's horizontal scroll extent -- which
    is what pushes the hexdump off the right edge."""
    imgui.push_style_color(imgui.Col_.text,
                           imgui.get_style_color_vec4(imgui.Col_.text_disabled))
    try:
        imgui.text_wrapped(text)
    finally:
        imgui.pop_style_color()


def _wrapped_colored(col: imgui.ImVec4, text: str) -> None:
    """text_colored that wraps.  Same reason as _wrapped_disabled: imgui's
    coloured text does not wrap, and one long verdict sentence then sets the
    whole Page window's horizontal scroll extent -- which pushes the hexdump
    the panel exists to show off the right edge."""
    imgui.push_style_color(imgui.Col_.text, col)
    try:
        imgui.text_wrapped(text)
    finally:
        imgui.pop_style_color()


def _tri(value, yes: str, no: str, unset: str = "not reported") -> tuple[imgui.ImVec4, str]:
    """A three-valued fact and its colour.  None is its own answer: "nobody
    reported this" is not "no", and painting it as a failure invents a defect
    the same way painting it as a pass hides one."""
    if value is None:
        return _vec4(palette.NONE_RGBA), unset
    return (_PASS_COL, yes) if value else (_FAIL_COL, no)


_REGION_COL = imgui.ImVec4(0.55, 0.75, 1.0, 1.0)


def _draw_region_banner(state: InspectorState) -> None:
    """Region, the addressing rule that put this page in it, the decoder that
    ran, and that decoder's check geometry -- all four, above everything else.

    This is the header that was missing while the panel reported every page in
    the vocabulary of FORMAT_A.  With the rule on screen the verdict below can
    be checked against the address instead of taken on faith, and a reader who
    knows block 0 on CE 6..11 is ordinary media can see immediately whether
    this panel knows it too.
    """
    region = state.region or ""
    known = region_known(region)
    info = pagefmt.region_info(region if known else pagefmt.REGION_UNKNOWN)

    imgui.text("region:")
    imgui.same_line()
    if known:
        imgui.text_colored(_PASS_COL if region == REGION_FTL else _REGION_COL,
                           region)
    else:
        imgui.text_colored(_vec4(palette.NONE_RGBA), "UNKNOWN")
    imgui.same_line()
    imgui.text_disabled("(geometry.region)")

    if not known:
        _wrapped_disabled(
            "The region could not be computed for this address, so this panel "
            "cannot say which page geometry applies. Nothing below is a region "
            "verdict: read the decode as unlabelled rather than as FTL.")

    _wrapped_disabled(f"rule: {info.rule}")
    _wrapped_disabled(f"decoder: {info.decoder}")
    imgui.text(f"geometry: {info.geometry}")
    _wrapped_disabled(
        f"{info.unit_count} {info.unit_plural} per page, "
        f"{info.mcrc_width}-byte "
        f"{'digest' if info.unit_noun == 'chunk' else 'MCRC'} each, "
        f"stored as {info.fmt}")

    if imgui.collapsing_header("codec page formats"):
        _wrapped_disabled(
            "titania.codec's four formats, for scale. The MCRC width is what "
            "says how wide a verdict is: a 4-byte and a 6-byte MCRC are not "
            "the same check, and FORMAT_C has none at all -- a format with no "
            "MCRC cannot fail one, and that is not evidence of a good page.")
        for row in pagefmt.FORMATS:
            name, mcrc, ecc, scram, note = (list(row) + [""] * 5)[:5]
            imgui.bullet_text(f"{name}: {mcrc}, ECC {ecc}, {scram} -- {note}")


#: The per-codeword table, header and rows through one format so the columns
#: cannot drift apart.
_CW_ROW_FMT = "%-4s %-16s %10s %8s %-11s"

#: How many codewords are listed before the table is folded away.  Both real
#: geometries (8 for EE3, 20 for A10R) are under it, so the table is normally
#: whole; the cap exists so a future geometry cannot turn the panel into a
#: thousand-line scroll.
_CW_TABLE_MAX = 32


def _ecc_color(st) -> imgui.ImVec4:
    """The ECC state's colour, straight out of the palette, so the inspector
    and the drive map's ECC mode can never disagree about what a state looks
    like -- the same rule _state_color follows for the five page states."""
    idx = int(st)
    if idx < 0 or idx >= palette.ECC_RGBA.shape[0]:
        return _vec4(palette.NONE_RGBA)
    return _vec4(palette.ECC_RGBA[idx])


def _verdict_tally(values) -> str:
    """(n pass, n checked, n not checked) as text.  A None is never counted as
    a pass: it is 'not checked', and it is reported as its own number."""
    if not values:
        return "no verdict"
    checked = [v for v in values if v is not None]
    unchecked = len(values) - len(checked)
    out = "%d/%d pass" % (sum(1 for v in checked if v), len(checked))
    if unchecked:
        out += ", %d not checked" % unchecked
    return out


def _draw_ecc(state: InspectorState) -> bool:
    """The ECC verdict, in words, and never in the MCRC's vocabulary.

    This section answers "what did the error-correcting code do", which is a
    different question from "does the CRC over the message match".  The two are
    drawn as two verdicts, side by side, and the panel says out loud when they
    disagree -- because "ECC converged, MCRC disagrees" is a fifth state, not a
    failed MCRC, and it has never been observed on this drive (0 of 248 failing
    pages, 0 of 264 injections, 0 of 36,800 group-2 codewords).

    Returns True when the user toggled the ECC run, so the caller can
    re-request the page.
    """
    ecc = state.ecc
    imgui.separator_text("ECC")

    changed, run = imgui.checkbox("run ECC", bool(state.run_ecc))
    if changed:
        state.run_ecc = bool(run)
    imgui.same_line()
    _wrapped_disabled(
        "the decoder is run on the page as READ (4-8 ms clean, up to ~215 ms "
        "to prove a page uncorrectable); nothing here calls "
        "titania_nand.ecc_correct_page, which discards the inversion flag and "
        "can hand back a complemented page")

    if ecc is None:
        st = pagefmt.EccState.NOT_MEASURED
        _swatch(_ecc_color(st), pagefmt.ECC_SENTENCE[st])
        imgui.same_line()
        imgui.text_colored(_ecc_color(st), pagefmt.ECC_LABEL[st])
        _wrapped_disabled(pagefmt.ECC_SENTENCE[st])
        return changed

    st = ecc.state
    _swatch(_ecc_color(st), ecc.sentence)
    imgui.same_line()
    imgui.text_colored(_ecc_color(st), ecc.label)
    imgui.same_line()
    imgui.text_disabled("-- %s" % ecc.short)
    imgui.text_wrapped(ecc.sentence)

    if ecc.code:
        _wrapped_disabled("code: %s" % ecc.code)

    # --- the evidence, as numbers ---------------------------------------
    if ecc.syndromes:
        imgui.text("syndrome weights before correction: max %d, min %d over "
                   "%d codewords" % (ecc.max_syndrome, min(ecc.syndromes),
                                     len(ecc.syndromes)))
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "The syndrome is the honest detector: an MCRC-clean page can "
                "still carry a bit error in LDPC parity, which the MCRC's "
                "message does not cover. 0 means this codeword was already a "
                "valid codeword.")
    if ecc.attempted:
        line = "converged: %d of %d codewords" % (ecc.n_converged,
                                                  ecc.n_codewords)
        col = (_PASS_COL if ecc.n_converged == ecc.n_codewords else _FAIL_COL)
        if ecc.state is pagefmt.EccState.CLEAN:
            line += "  (the corrector was not run: there was nothing to do)"
        _wrapped_colored(col, line)
        imgui.text("bits changed by ECC: %d" % ecc.corrected_bits)
        if ecc.state is pagefmt.EccState.UNCORRECTABLE:
            # Wrapped, on its own line: an unwrapped tail here sets the whole
            # Page window's horizontal scroll extent and pushes the hexdump off
            # the right edge -- the same defect the header lines were fixed for.
            _wrapped_disabled(
                "0 bits changed is what honest non-convergence looks like, not "
                "a decoder that never engaged: nd_deepparity hands back the "
                "ORIGINAL block for a codeword it could not decode.")
    imgui.text_disabled("%.1f ms" % (ecc.seconds * 1000.0))

    # --- the other verdict, kept apart ----------------------------------
    if ecc.mcrc_before or ecc.mcrc_after:
        imgui.spacing()
        imgui.text("MCRC (a separate check, over the message only):")
        imgui.text("   before correction: %s" % _verdict_tally(ecc.mcrc_before))
        imgui.same_line()
        imgui.text_disabled("|")
        imgui.same_line()
        imgui.text("after: %s" % _verdict_tally(ecc.mcrc_after))
        _ecc_vs_mcrc(ecc)

    # --- the inversion guard --------------------------------------------
    if ecc.inv_margin >= 0:
        col = _PASS_COL if ecc.inv_margin >= pagefmt.ECC_DEEP_MARGIN else _FAIL_COL
        imgui.spacing()
        imgui.text_colored(col, "inversion check: codeword 0 syndrome %d as "
                                "read vs %d complemented (margin %d)"
                            % (ecc.syn_raw, ecc.syn_cpl, ecc.inv_margin))
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "nd_deepparity.is_inverted() decides 'deep page' on codeword 0 "
                "alone and its docstring promises a ~720-bit margin; a healthy "
                "page here measures exactly 720. Under %d the verdict is a coin "
                "flip and this viewer does NOT act on it."
                % (pagefmt.ECC_DEEP_MARGIN,))
        _wrapped_disabled("decoded in the %s convention"
                          % ("COMPLEMENTED (deep) " if ecc.deep
                             else "page-as-read (shallow)"))

    _draw_ecc_table(ecc)

    for fix in ecc.fixes:
        col = _PASS_COL if fix.zone == "payload" else _REGION_COL
        imgui.text_colored(col, "fixed: codeword %d (%s) bit %d"
                           % (fix.codeword, fix.unit, fix.bit))
        imgui.same_line()
        if fix.page_offset >= 0:
            imgui.text_disabled("-> page offset %d (0x%X), zone: %s"
                                % (fix.page_offset, fix.page_offset, fix.zone))
        else:
            imgui.text_disabled("-> no page offset: %s" % (fix.zone,))

    for note in ecc.notes:
        imgui.bullet()
        imgui.text_wrapped(note)
    if ecc.error:
        imgui.text_colored(_vec4(palette.NONE_RGBA), "ECC note:")
        imgui.same_line()
        imgui.text_wrapped(ecc.error)
    _wrapped_disabled(pagefmt.ECC_CAPABILITY)
    return changed


def _ecc_vs_mcrc(ecc) -> None:
    """The one line that stops two different verdicts reading as one.

    Every combination that can occur gets its own sentence.  The dangerous one
    is ECC-converged-with-a-failing-MCRC, which the old panel could only render
    as "MCRC FAIL" -- a wording that says the media is bad, when what the
    numbers actually say is that something in the decode path is wrong.
    """
    after = [v for v in ecc.mcrc_after if v is not None]
    mcrc_ok = all(after) if after else None
    if ecc.state is pagefmt.EccState.MCRC_DISAGREES:
        _wrapped_colored(_ecc_color(ecc.state),
                         "ECC CONVERGED and the MCRC still disagrees.")
        imgui.text_wrapped(
            "Read this as 'the two checks disagree', NOT as 'MCRC FAIL': every "
            "codeword is a valid codeword, so the bytes are self-consistent "
            "under the ECC and not under the CRC. No mechanism is known for "
            "that -- the MCRC's message is a subset of the codeword -- so it "
            "is a bug indicator, not a media verdict.")
        return
    if ecc.state is pagefmt.EccState.CORRECTED and mcrc_ok:
        _wrapped_colored(_PASS_COL,
                         "The two checks AGREE: the ECC repaired the page and "
                         "the MCRC passes on the repaired bytes.")
        if any(v is False for v in ecc.mcrc_before):
            _wrapped_disabled(
                "The MCRC was failing BEFORE the correction, and that failure "
                "is now explained rather than ignored: the decoder found the "
                "bad bits, flipped them, and the CRC agrees with the result.")
        elif all(v for v in ecc.mcrc_before if v is not None) and ecc.fixes:
            imgui.text_wrapped(
                "The MCRC was passing BEFORE the correction as well: the bit "
                "that was flipped is outside the message the MCRC covers "
                "(LDPC parity), so the MCRC alone would have called this page "
                "clean while it still carried a bit error.")
        return
    if ecc.state is pagefmt.EccState.UNCORRECTABLE and mcrc_ok is False:
        _wrapped_colored(_FAIL_COL,
                         "Both checks fail, and they fail for the same "
                         "reason: these bytes are not a codeword.")
        imgui.text_wrapped(
            "The MCRC failure here is a CONSEQUENCE, not an independent second "
            "opinion -- and it is not evidence about which bytes are wrong. "
            "The bytes are still shown below.")
        return
    if ecc.state is pagefmt.EccState.CLEAN and mcrc_ok is False:
        _wrapped_colored(_ecc_color(pagefmt.EccState.MCRC_DISAGREES),
                         "Every codeword is valid, yet an MCRC fails -- the "
                         "same red flag as 'converged, MCRC disagrees'.")
        return
    if ecc.state is pagefmt.EccState.CLEAN and mcrc_ok:
        imgui.text_disabled("The two checks agree: nothing to correct and the "
                            "MCRC passes.")


def _draw_ecc_table(ecc) -> None:
    """Per codeword: syndrome, flips, converged.  Never an all() collapse.

    titania_nand.decode_page copies one page-level all() onto all four AUs and
    dumps the whole page's flip count on AU0, so a page where codeword 5 alone
    failed reads there as four failed AUs and three AUs with zero corrections.
    This table is the raw log, which is where the truth is.
    """
    if not ecc.syndromes:
        return
    n = min(len(ecc.syndromes), _CW_TABLE_MAX)
    label = "per-codeword ECC log (%d codewords)" % (len(ecc.syndromes),)
    flags = (0 if ecc.state in (pagefmt.EccState.CLEAN,)
             else int(imgui.TreeNodeFlags_.default_open))
    if not imgui.collapsing_header(label, flags):
        return
    _mono_text(_CW_ROW_FMT % ("cw", "unit", "syndrome", "flips", "converged"))
    for i in range(n):
        conv = ecc.converged[i] if i < len(ecc.converged) else None
        flips = ecc.flips[i] if i < len(ecc.flips) else 0
        unit = ecc.units[i] if i < len(ecc.units) else ""
        word = "yes" if conv else ("NO" if conv is False else "not run")
        row = _CW_ROW_FMT % (i, unit, ecc.syndromes[i], flips, word)
        if conv is False:
            with mono_scope():
                imgui.text_colored(_FAIL_COL, row)
        elif flips:
            with mono_scope():
                imgui.text_colored(_ecc_color(pagefmt.EccState.CORRECTED), row)
        else:
            _mono_text(row)
    if len(ecc.syndromes) > n:
        imgui.text_disabled("... %d more codewords"
                            % (len(ecc.syndromes) - n,))


#: The chunk table's column widths, used for the HEADER and the rows alike so
#: the two cannot drift.  "%-6s" takes the index as a string: one format, one
#: set of column starts, whatever is passed in.
_CHUNK_ROW_FMT = "%-6s %-13s %-13s"


def _digest_hex(digest) -> str:
    if digest is None:
        return "(no digest reported)"
    try:
        return bytes(digest).hex(" ").upper()
    except Exception:                                        # noqa: BLE001
        return str(digest)


def _draw_group2(state: InspectorState) -> None:
    """The firmware group-2 page: Five chunks, each with its own 4-byte digest.

    There is no four-AU view of this page and no 6-byte MCRC in it, so none is
    drawn -- not even a greyed-out one.  A page whose five digests verify reads
    as verified here, which is the whole point: the old panel ran FORMAT_A over
    these bytes, got 0/4, and painted a perfect page red.
    """
    g = state.group2
    if g is None:
        if not state.decode_attempted:
            # Nobody asked. ABSENT / ERASED / SKIPPED / not probed carry no
            # bytes, so request() hands them to no checker -- and
            # every page of CE0's block 0 is such a page at a group-2 address.
            # Reporting that as "the reader could not be reached" states a
            # tooling failure for a call that was never made; the non-group-2
            # branch has always said this correctly and this branch used not to.
            rec = state.record
            st = PageState(int(rec.state)) if rec is not None else PageState.UNKNOWN
            imgui.text_colored(_state_color(st),
                               f"not decoded -- this page is {_STATE_HEADLINE[st]}")
            _wrapped_disabled(
                "This address is five chunks with one 4-byte digest each, but "
                "no decoder was asked about it: " + _STATE_SENTENCE[st] +
                " Absence of a decode is not a failed decode, and it is not a "
                "statement about the reader either.")
            return
        imgui.text_colored(_vec4(palette.NONE_RGBA),
                           "group-2 reader not reachable")
        _wrapped_disabled(
            "This page is five chunks with one 4-byte digest each, and it has "
            "bytes. The reader that checks that shape (fw.check_group2_page) "
            "could not be reached, so NOTHING here has checked this page. A "
            "four-AU MCRC verdict does not apply to this geometry and is "
            "deliberately not shown: it would be a verdict about a layout these "
            "bytes do not have.")
        return

    chunks = g.get("chunks") or ()
    counts = g.get("counts")
    n_ok, n_total = counts if counts is not None else (0, len(chunks))
    # Three-valued, like every other verdict on this panel. An erased chunk is
    # not checked, so a page whose five chunks all read as erased comes back
    # (0, 0): nothing failed, because nothing was checked. Painting that red
    # contradicted the five greyed "(erased -- not checked)" rows immediately
    # below it and the green "page check: OK" above -- the same
    # absence-is-not-failure error _tri() exists to prevent.
    if n_total <= 0:
        imgui.text_colored(_vec4(palette.NONE_RGBA),
                           "chunk digests: none checked")
        tip = ("Every chunk of this page reads as erased, and an erased chunk "
               "carries no digest to check. NOT a pass and NOT a failure: no "
               "digest was verified and none failed.")
    elif n_ok == n_total:
        imgui.text_colored(_PASS_COL,
                           f"chunk digests: {n_ok}/{n_total} verify")
        tip = ("Each chunk carries its own 4-byte digest, and this is the "
               "roll-up of the checks that ran. It is NOT an MCRC: the 6-byte "
               "MCRC belongs to the 4-AU geometry of the FTL region. Erased "
               "chunks are not counted here, so a total below five means some "
               "chunk was erased, not that a check went missing.")
    else:
        imgui.text_colored(_FAIL_COL,
                           f"chunk digests: {n_ok}/{n_total} verify")
        tip = ("At least one chunk's 4-byte digest did not match its payload. "
               "This is NOT an MCRC verdict: the 6-byte MCRC belongs to the "
               "4-AU geometry of the FTL region. Erased chunks are not counted "
               "in either number.")
    if imgui.is_item_hovered():
        imgui.set_tooltip(tip)

    col, word = _tri(g.get("ok"), "OK", "NOT OK")
    imgui.text("page check:")
    imgui.same_line()
    imgui.text_colored(col, word)
    imgui.same_line()
    imgui.text_disabled("(fw.check_group2_page)")

    for key, label, yes, no in (
            ("digest_consistent", "digests consistent across chunks", "yes", "NO"),
            ("tail_ff", "tail is 0xFF", "yes", "no"),
            ("size", "page size as expected", "yes", "NO"),
            ("erased", "erased", "yes", "no")):
        if key not in g:
            continue
        col, word = _tri(g.get(key), yes, no)
        imgui.text(f"{label}:")
        imgui.same_line()
        imgui.text_colored(col, word)

    imgui.spacing()
    # The header goes through the same font scope as the rows below it. It is a
    # column header for a monospace table, and drawn in the proportional UI face
    # it does not sit over its own columns: MEASURED, "chunk  " is 49 px in the
    # default font and 56 px in Consolas 15, so the first column starts 7 px out
    # and the last one drifts ~42 px. The dimmed look comes from a pushed style
    # colour rather than text_disabled(), the pattern _wrapped_disabled uses,
    # because text_disabled() would take the font choice with it.
    imgui.push_style_color(imgui.Col_.text,
                           imgui.get_style_color_vec4(imgui.Col_.text_disabled))
    try:
        _mono_text(_CHUNK_ROW_FMT % ("chunk", "digest", "sha1"))
        # The verdict column is drawn per row by text_colored in the UI face
        # after a same_line, so its header is placed the same way -- appending
        # the word to the mono run would put it one item_spacing to the left of
        # the values it labels.
        imgui.same_line()
        imgui.text_unformatted("verdict")
    finally:
        imgui.pop_style_color()
    for i, c in enumerate(chunks):
        sha1 = str(c.get("message_sha1") or "")
        # One fixed-pitch run per row: the digests are meant to be compared
        # down the column, and in a proportional font they do not line up.
        _mono_text(_CHUNK_ROW_FMT % (i, _digest_hex(c.get("digest")),
                                     sha1 or "-"))
        imgui.same_line()
        col, word = _tri(c.get("mcrc_ok"), "digest ok", "digest FAIL")
        imgui.text_colored(col, word)
        if c.get("erased"):
            imgui.same_line()
            imgui.text_disabled("(erased -- not checked)")

    notes = g.get("notes") or ()
    if notes:
        imgui.spacing()
        imgui.text_disabled("notes from the reader:")
        for note in notes:
            imgui.bullet()
            imgui.same_line()
            imgui.text_wrapped(str(note))

    _draw_group2_payload(state, g)


#: What the payload is, once, next to the bytes.  A group-2 chunk is 4 x (512
#: payload + 300 A10R parity), so the 2048 bytes below are the payload halves
#: only: the parity is not user data and is not in this dump.  The
#: raw page above is 18432 bytes and this is 10240 of them; the rest is parity
#: and the erased tail, which is why the two hexdumps do not agree byte for byte
#: and must not be read as the same bytes in two forms.
_G2_PAYLOAD_HELP = (
    "fw.decode_group2_page descrambles the page with the row seed and returns "
    "10240 plaintext bytes: five chunks of 2048. Each chunk is stored as "
    "4 x (512 payload + 300 A10R parity) and only the payload halves are here -- "
    "parity is not user data. This is NOT a re-formatting of the raw page below: "
    "18432 raw bytes carry 10240 payload bytes, and the difference is parity and "
    "the erased tail.")


def _draw_group2_payload(state: InspectorState, g: dict) -> None:
    """The decoded user DATA of a group-2 page: per-chunk payload hexdump.

    An addition to the structural verdict above, never a replacement for it.
    The verdict says whether the five digests match; this says what the bytes
    they cover actually are, which is the whole reason to decode a page at all.

    Every number here was measured on the worker thread and is only formatted
    here -- the payload, its entropy and its histogram all arrive inside the
    PageBundle, exactly like the raw page's.
    """
    chunks = g.get("chunks") or ()
    total = int(g.get("payload_total") or 0)
    n_with = int(g.get("payload_chunks") or 0)

    imgui.spacing()
    imgui.separator_text("Decoded user data")

    if total <= 0:
        # The digests need no seed; the plaintext does.  So "no payload" is a
        # statement about the descrambler, not about the page -- the verdict
        # above stands on its own and is not retracted here.
        imgui.text_colored(_vec4(palette.NONE_RGBA), "no decoded payload")
        _wrapped_disabled(
            "The five-chunk structural check ran (its verdict is above), but no "
            "plaintext came back for this page. The digest check is seed-free "
            "and the plaintext is not: it needs the row seed and the scrambler, "
            "so a missing payload is a statement about the descrambler and not "
            "about the media. Any reason the reader gave is in the notes above.")
        return

    imgui.text(f"total decoded payload: {total} bytes")
    imgui.same_line()
    imgui.text_disabled(f"({n_with}/{len(chunks)} chunks x 2048)")
    _wrapped_disabled(_G2_PAYLOAD_HELP)

    # One radio per chunk, the same shape as the AU selector above -- the two
    # geometries get the same interaction so the only thing that differs on
    # screen is the noun and the count.
    for i in range(len(chunks)):
        if i:
            imgui.same_line()
        if imgui.radio_button(f"chunk {i}##g2sel", state.chunk == i):
            state.chunk = i
    imgui.same_line()
    _ch, state.show_payload = imgui.checkbox("show payload bytes",
                                             state.show_payload)

    idx = min(max(int(state.chunk), 0), len(chunks) - 1)
    state.chunk = idx
    c = chunks[idx]
    payload = c.get("payload")

    # Which chunk's verdict these bytes sit under, so the dump is attributable
    # without scrolling back to the table.  The table stays where it is.
    col, word = _tri(c.get("mcrc_ok"), "digest ok", "digest FAIL")
    imgui.text(f"chunk {idx}:")
    imgui.same_line()
    _mono_text(_digest_hex(c.get("digest")))
    imgui.same_line()
    imgui.text_colored(col, word)
    if c.get("erased"):
        imgui.same_line()
        imgui.text_disabled("(erased -- not checked)")

    if not payload:
        imgui.text_colored(_vec4(palette.NONE_RGBA),
                           f"chunk {idx}: no payload bytes")
        _wrapped_disabled(
            "The other chunks of this page decoded, this one produced no "
            "plaintext. Its digest verdict above is unaffected: the digest is "
            "checked on the STORED bytes and needs no descrambling.")
        return

    imgui.text_disabled(f"payload[{idx}]: {len(payload)} bytes")
    hist = c.get("hist")
    if hist is None:
        # Nothing is computed here to fill the gap: the histogram belongs to the
        # worker thread, and "not measured" is a true statement while a
        # UI-thread recount would quietly move that work into the frame loop.
        imgui.text_disabled("entropy / histogram: not measured for this chunk")
    else:
        # Same widget, same log scale and same 0x00 / 0xFF call-outs as the raw
        # page's histogram: a payload that is 99% zero must look the same here
        # as it does up there, or the two panels describe the page in two
        # languages.
        draw_histogram(hist, float(c.get("entropy", -1.0)))

    if state.show_payload:
        # Its own id scope, like "au-hex" and "raw-hex": draw_hex's child is
        # always "##hexdump", and two of them under one id would merge into a
        # single scrolling region holding 2048 decoded bytes and 18432 raw ones.
        #
        # scroll=False because "goto offset" belongs to the RAW page below.  The
        # scroll is applied by whichever dump draws first after the offset
        # changes, and this one draws before the raw dump -- so honouring it here
        # would silently take the raw page's own control away from it.
        with _id_scope("g2-chunk-hex"):
            draw_hex(payload, state,
                     height=imgui.get_text_line_height_with_spacing() * 12.0,
                     scroll=False)


def _aux_value(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(" ").upper()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value} (0x{value:X})"
    return str(value)


#: aux.AuxEntry is not a dataclass; __slots__ is its field list, in order.
_AUX_SKIP = ("raw",)          # already on screen above, as hex
_AUX_EXTRA = ("erased", "lma_is_sentinel")


def _aux_fields(entry) -> list[tuple[str, str]]:
    names = [n for n in (getattr(entry, "__slots__", ()) or ())]
    if not names:
        names = [n for n in dir(entry)
                 if not n.startswith("_") and not callable(getattr(entry, n, None))]
    for extra in _AUX_EXTRA:
        if extra not in names and hasattr(entry, extra):
            names.append(extra)
    out = []
    for name in names:
        if name in _AUX_SKIP:
            continue
        try:
            out.append((name, _aux_value(getattr(entry, name))))
        except Exception:                                    # noqa: BLE001
            continue
    return out


def _draw_aux(state: InspectorState, dec: model.Decoded, au: int) -> None:
    """The 8 aux bytes, and what they mean under the owning MSO's layout.

    All four layouts are 8 bytes wide, so the bytes never tell you which layout
    they are in -- the owning MSO's descriptor (byte +17) does, and without a
    descriptor source aux.layout_for() answers UNKNOWN.  UNKNOWN is then what
    this says.  Defaulting to layout 0 would render one particular reading of
    the bytes as though it were the reading.
    """
    aux = dec.aux(au)
    imgui.text(f"aux[{au}] ({len(aux)} bytes):")
    imgui.same_line()
    _mono_text(aux.hex(" ").upper())

    view = state.aux or {}
    layout = view.get("layout")
    if layout is None:
        imgui.text("aux layout:")
        imgui.same_line()
        imgui.text_colored(_vec4(palette.NONE_RGBA), "UNKNOWN")
        _wrapped_disabled(
            str(view.get("layout_why") or _AUX_NO_SOURCE) +
            ". All four layouts are 8 bytes wide, so the bytes above stay "
            "bytes: reading them under a guessed layout would put confident "
            "field names on fields that may not be there.")
        return

    imgui.text("aux layout:")
    imgui.same_line()
    imgui.text_colored(_REGION_COL,
                       str(view.get("layout_name") or
                           pagefmt.aux_layout_name(layout)))
    owner_label = view.get("owner_label")
    if owner_label:
        # Which partition selected it. The layout is a property of the OWNER,
        # not of the page, so naming the owner here is what makes the layout
        # line checkable rather than something to take on faith.
        _wrapped_disabled("selected by the owning partition: %s"
                          % owner_label)
    why = view.get("layout_why")
    if why:
        _wrapped_disabled(f"({why})")
    entries = view.get("entries") or ()
    entry = entries[au] if au < len(entries) else None
    if entry is None:
        _wrapped_disabled(
            "the layout is known, but aux.parse produced nothing for these 8 "
            "bytes -- titania.auxdata was unreachable, or it refused these bytes "
            "under this layout. The bytes above are unchanged; only their "
            "reading is missing.")
        return
    for name, value in _aux_fields(entry):
        imgui.text(f"  {name}:")
        imgui.same_line()
        _mono_text(value)


def _draw_aus(state: InspectorState, dec: model.Decoded) -> None:
    """The 4-AU geometry: per-AU 6-byte MCRC, aux, and the selected AU's user
    bytes.  Reached for the ftl and fw-group01 regions, both of which really
    are 4 AUs x 6-byte MCRC -- and never for fw-group2."""
    _page_mcrc_line(dec, state.ecc)
    if dec.aus is None:
        imgui.text_disabled("the codec returned no AUs for this page")
        # A page whose codec returned nothing is not a page with nothing to
        # show: the raw bytes are below, and the ECC section above says what
        # happened to them.
        return

    n_aus = len(dec.aus)
    # A summary row first: all four verdicts visible at once, so picking an AU
    # is never how you discover a failure.
    for i in range(n_aus):
        if i:
            imgui.same_line()
        imgui.begin_group()
        imgui.text(f"AU{i}")
        _mcrc_widget(dec, i)
        imgui.end_group()

    imgui.spacing()
    for i in range(n_aus):
        if i:
            imgui.same_line()
        if imgui.radio_button(f"AU {i}##sel", state.au == i):
            state.au = i
            state.hex_offset = 0
    imgui.same_line()
    ch, state.show_user = imgui.checkbox("show user bytes", state.show_user)

    au = min(max(state.au, 0), n_aus - 1)
    state.au = au
    _draw_aux(state, dec, au)

    if state.show_user:
        user = dec.user(au)
        imgui.text_disabled(f"user[{au}]: {len(user)} bytes")
        # draw_hex()'s child is always called "##hexdump", and this panel draws
        # two of them (decoded user bytes here, the raw page below). Without a
        # distinct id scope both resolve to the same child window id and imgui
        # merges them into one scrolling region -- 4096 decoded bytes and 18432
        # raw bytes in the same box, which is the one place in this viewer where
        # the two must never be confused.
        with _id_scope("au-hex"):
            draw_hex(user, state,
                     height=imgui.get_text_line_height_with_spacing() * 12.0)


def _correction_warning(state: InspectorState) -> None:
    """What the codec's own `correct=True` path does that the ECC panel does not.

    The combo above drives titania_nand.decode_page(correct=True), which calls
    ecc_correct_page() -- and that function throws away
    nd_deepparity.normalize()'s inversion flag (titania_nand.py:415), so on any
    page where is_inverted() fires it returns the bitwise complement and never
    complements it back. Measured: 74 of 170 failing TLC pages and 180 of 180
    all-0x00 pages. The ECC section above does not go through it, so the two can
    and do disagree -- and when they do, the reader has to be told which one is
    showing the complement rather than left to notice that the hex changed.
    """
    if state.correct is not True:
        return
    ecc = state.ecc
    _wrapped_disabled(
        "'full correction' runs the codec's own path, which calls "
        "titania_nand.ecc_correct_page() -- the function that discards "
        "nd_deepparity.normalize()'s inversion flag (titania_nand.py:415). The "
        "ECC section above never calls it; it decodes the page as read.")
    if ecc is None or ecc.inv_margin < 0:
        return
    if ecc.inverted and ecc.inv_margin < pagefmt.ECC_DEEP_MARGIN:
        _wrapped_colored(
            _ecc_color(pagefmt.EccState.MCRC_DISAGREES),
            "ON THIS PAGE THAT BUG FIRES: is_inverted() is True at a margin of "
            "only %d (it guarantees ~720), so the AUs below were decoded from "
            "a page ecc_correct_page() complemented and did not complement "
            "back. Every byte of them is inverted. Read the ECC section above "
            "and the raw bytes below instead." % (ecc.inv_margin,))
    elif ecc.inverted:
        _wrapped_colored(
            _REGION_COL,
            "This page really does read bit-inverted (margin %d), so the "
            "complement is correct here -- but ecc_correct_page() still does "
            "not undo it, and descrambling needs the deep seed."
            % (ecc.inv_margin,))


def _draw_decoded(state: InspectorState) -> tuple[int, int, int, bool] | None:
    """The decoded page in its own GEOMETRY.  Returns a target when the
    correction mode changed, so app.py can re-submit the decode with the new
    correct= setting.

    Three regions, two geometries, one banner that says which is which.  The
    four-AU body below is reached only for the regions that have four AUs.
    """
    requested: tuple[int, int, int, bool] | None = None
    dec = state.decoded
    # The region decides; the second test is a backstop for the case where the
    # region could not be computed but the decode came back as chunks anyway.
    # Whatever else happens, a page with chunks and no AUs is never sent to the
    # four-AU renderer.
    group2 = (state.region == REGION_FW_GROUP2 or
              (state.group2 is not None and (dec is None or dec.aus is None)))

    imgui.separator_text("Decoded page")
    _draw_region_banner(state)
    imgui.spacing()

    # The ECC verdict comes first and it is its own section: it is what
    # happened to the bytes, and the MCRC below is a check on the result.
    # Putting it under the MCRC is how "correction failed" ended up being read
    # as "MCRC FAIL" -- one verdict standing in for two.
    if _draw_ecc(state) and state.target is not None:
        requested = state.target
    imgui.spacing()

    if group2:
        # The correction combo drives codec.decode(correct=...), which is the
        # 4-AU LDPC path.  It has no meaning for the five-chunk reader, so it
        # is disabled rather than silently ignored.
        imgui.begin_disabled()
        imgui.set_next_item_width(260.0)
        imgui.combo("correction", 0, list(_CORRECT_LABELS))
        imgui.end_disabled()
        _wrapped_disabled(
            "codec ECC correction is not part of this geometry: it drives the "
            "FORMAT_A LDPC path, and group 2 is A10R parity checked by a "
            "4-byte digest per chunk.")
    else:
        cur = _CORRECT_VALUES.index(state.correct) if state.correct in _CORRECT_VALUES else 0
        imgui.set_next_item_width(260.0)
        ch, cur = imgui.combo("correction", cur, list(_CORRECT_LABELS))
        if ch:
            state.correct = _CORRECT_VALUES[cur]
            if state.target is not None:
                requested = state.target
        _correction_warning(state)

    if group2:
        if dec is not None:
            imgui.text_wrapped(
                f"source: {dec.source}   {dec.seconds * 1000.0:.1f} ms")
        _draw_group2(state)
        if dec is not None and dec.error:
            imgui.text_colored(_vec4(palette.NONE_RGBA), "decode error:")
            imgui.same_line()
            imgui.text_wrapped(dec.error)
        return requested

    if dec is None:
        _wrapped_disabled(
            "not decoded -- this page carries no bytes, or decoding was not "
            "requested. Absence of a decode is not a failed decode.")
        return requested

    imgui.text_wrapped(f"source: {dec.source}   {dec.seconds * 1000.0:.1f} ms")
    if dec.error:
        imgui.text_colored(_vec4(palette.NONE_RGBA), "decode error:")
        imgui.same_line()
        imgui.text_wrapped(dec.error)
    _draw_aus(state, dec)
    return requested


def _draw_raw(state: InspectorState, rec: model.PageRecord) -> None:
    """The bytes, always.

    A page the ECC could not correct still has bytes and they are still shown:
    hiding them behind a verdict is what this panel was rewritten to stop. When
    a correction was applied the corrected page is offered as a second view,
    explicitly labelled, never as a silent replacement.
    """
    imgui.separator_text("Raw page")
    if not rec.data:
        # Naming the state again here keeps "no hex" from reading as "empty
        # page": erased, skipped and absent all show no hex for different
        # reasons.
        st = PageState(int(rec.state))
        imgui.text_colored(_state_color(st),
                           f"no raw bytes: page is {_STATE_HEADLINE[st]}")
        imgui.text_wrapped(_STATE_SENTENCE[st])
        return

    ecc = state.ecc
    fixed = getattr(ecc, "page", None) if ecc is not None else None
    data = rec.data
    if fixed is not None and len(fixed) == len(rec.data):
        if imgui.radio_button("as read##rawsel", not state.show_corrected):
            state.show_corrected = False
        imgui.same_line()
        if imgui.radio_button("ECC-corrected##rawsel", state.show_corrected):
            state.show_corrected = True
        imgui.same_line()
        imgui.text_disabled("(%d bits differ, in %d of %d codewords)"
                            % (ecc.corrected_bits,
                               sum(1 for f in ecc.flips if f),
                               ecc.n_codewords))
        if state.show_corrected:
            data = fixed
            _wrapped_colored(_ecc_color(ecc.state),
                             "showing the ECC-CORRECTED bytes, not the bytes "
                             "the drive returned")
    elif ecc is not None and ecc.state in (pagefmt.EccState.UNCORRECTABLE,
                                           pagefmt.EccState.PARTIAL):
        _wrapped_colored(_ecc_color(ecc.state),
                         "these are the bytes as read: %s" % (ecc.short,))
        _wrapped_disabled(
            "The correction failed, so there is no corrected page to show -- "
            "but the page itself is right here. A page that cannot be "
            "corrected is still a page you can read, hash, carve and compare "
            "against a re-read.")

    imgui.set_next_item_width(140.0)
    ch, off = imgui.input_int("goto offset", int(state.hex_offset), HEX_WIDTH, HEX_WIDTH * 16)
    if ch:
        state.hex_offset = max(0, min(len(data) - 1, int(off)))
    imgui.same_line()
    imgui.text_disabled(f"{len(data)} bytes")
    with _id_scope("raw-hex"):   # distinct from the decoded-AU hex above
        draw_hex(data, state, height=_fill_height(_RAW_HEX_MIN_ROWS))


#: Rows the raw hexdump is always given, whatever room is left.  This was the
#: whole fixed height before; it is now the floor.
_RAW_HEX_MIN_ROWS = 18


def _fill_height(min_rows: int) -> float:
    """Height for the last child in the panel: the room left, but never less
    than `min_rows` rows.  Grow-only, and that asymmetry is the point.

    The raw hexdump scrolls inside its own child regardless of the height it
    gets (1152 rows of an 1152-row page), so the height only decides how much
    of the page you see at once.

      * Where the panel is taller than the sections above it -- a tall window,
        an ERASED page with no decoded section -- the old fixed 18 lines left
        real room unused with the dump ending in the middle of the panel.  Fill
        it.
      * Where it is shorter, shrinking the dump would not remove the Page
        window's scrollbar (the sections above already overflow on their own);
        it would only mean scrolling and seeing fewer bytes.  So the floor
        holds and the window scrolls, which is the honest outcome: the panel
        really is too short for its contents at this size.

    Measured at 1800x1000 on an FTL page: the sections above the raw dump come
    to ~1120 px in a ~740 px dock, so this returns the floor there.
    """
    style = imgui.get_style()
    line_h = float(imgui.get_text_line_height_with_spacing())
    room = (float(imgui.get_content_region_avail().y)
            - float(style.item_spacing.y))
    return max(room, line_h * float(min_rows))


@contextlib.contextmanager
def mono_scope():
    """push_font/pop_font for the hexdump font, unwound even on an exception.

    The one place this module pushes a font.  An unbalanced font stack outlives
    the widget that broke it and re-skins every panel drawn afterwards, so the
    pop is in a finally and there is no second pusher to get out of step with.

    The font is owned by the shell (app._load_mono_font): it has to be in the
    atlas before the backend builds its texture, and only the shell runs that
    early.  app is imported HERE, not at module scope -- app imports pageview,
    so a top-level import would be a cycle.  By the time anything draws, app is
    fully imported and this is a sys.modules lookup.

    The size comes from the shell too (app.mono_font_size()) rather than being
    0.0 / "keep the current size": one size for every hex block on the panel,
    and the same metrics draw_hex measures its clipper rows with.

    app.mono_font() answering None is a normal outcome, not an error -- it means
    no monospace ttf was found and imgui's own fixed-pitch built-in stays in
    force.  Then nothing is pushed and nothing must be popped, which is what
    `yield False` tells the body.
    """
    font = None
    size = 0.0
    try:
        from . import app as _app                              # noqa: PLC0415
        font = _app.mono_font()
        size = _app.mono_font_size()
    except Exception:                                          # noqa: BLE001
        font = None
    if font is None:
        yield False
        return
    imgui.push_font(font, float(size))
    try:
        yield True
    finally:
        imgui.pop_font()


def draw_hex(data: bytes, state: InspectorState, height: float = 0.0,
             scroll: bool = True) -> None:
    """Clipped monospace hex via imgui list clipper -- 18432 bytes is 1152 rows
    and only the visible ones are formatted.

    `scroll=False` opts a dump OUT of state.hex_offset.  "goto offset" addresses
    the RAW page, and the offset is consumed by whichever dump draws first after
    it changes -- so a dump of some other byte range (the group-2 payload, which
    is 2048 bytes of a different sequence) must not take it, or the raw page's
    own control would move a panel the user was not looking at.

    The font is pushed around everything that measures or draws a row.  line_h
    is read inside that scope: the monospace font is a different
    size from the UI font, and a clipper whose row height disagrees with the
    height of the rows it steps over mis-places every jump, every scroll and the
    'goto offset' landing row.
    """
    rows = (len(data) + HEX_WIDTH - 1) // HEX_WIDTH

    with mono_scope():
        line_h = imgui.get_text_line_height_with_spacing()
        if height <= 0.0:
            height = line_h * min(rows + 1, 18)

        with _child_scope("##hexdump", imgui.ImVec2(0.0, height),
                          imgui.ChildFlags_.borders,
                          imgui.WindowFlags_.horizontal_scrollbar):
            if rows == 0:
                imgui.text_disabled("(0 bytes)")
            else:
                want = getattr(state, _SCROLL_ATTR, None)
                if scroll and want != state.hex_offset:
                    imgui.set_scroll_y(float(state.hex_offset // HEX_WIDTH) * line_h)
                    setattr(state, _SCROLL_ATTR, state.hex_offset)
                clipper = imgui.ListClipper()
                clipper.begin(rows, line_h)
                while clipper.step():
                    for row in range(clipper.display_start, clipper.display_end):
                        imgui.text_unformatted(_hex_row(data, row, HEX_WIDTH, 0))
                clipper.end()


def draw_histogram(hist: np.ndarray, entropy: float) -> None:
    values = np.asarray(hist, dtype=np.float64)
    total = float(values.sum())
    if entropy >= 0.0:
        overlay = f"entropy {entropy:.3f} bits/byte"
    else:
        overlay = "entropy n/a"
    # log1p: a page that is 99% 0x00 or 0xFF would otherwise flatten every
    # other bucket to invisible, and "looks empty" is precisely the misreading
    # this panel exists to prevent.
    plot = np.log1p(values).astype(np.float32, copy=False)
    # scale_max is never allowed to equal scale_min: an all-zero histogram
    # would otherwise normalise by zero inside imgui.
    scale_max = max(float(plot.max()) if plot.size else 1.0, 1.0)
    imgui.plot_histogram("##bytehist", plot, 0, overlay + "  (log scale)",
                         0.0, scale_max, imgui.ImVec2(-1.0, 70.0))

    if total <= 0.0:
        imgui.text_disabled("no bytes counted")
        return
    zeros = float(values[0])
    ffs = float(values[255])
    distinct = int(np.count_nonzero(values))
    imgui.text(f"{int(total)} bytes, {distinct}/256 distinct values")
    # 0x00 and 0xFF get called out by name because they are the two constants
    # that get mistaken for states: 0xFF is what erased media reads, 0x00 is
    # the dead-CE bus constant -- neither is proof of either state on its own.
    imgui.text_disabled(
        f"0x00: {int(zeros)} ({100.0 * zeros / total:.1f}%)   "
        f"0xFF: {int(ffs)} ({100.0 * ffs / total:.1f}%)")
    if zeros == total:
        imgui.text_colored(_state_color(PageState.ZERO),
                           "uniform 0x00 -- bus constant, NOT erased media")
    elif ffs == total:
        imgui.text_colored(_state_color(PageState.ERASED),
                           "uniform 0xFF -- reads like erased media, but this "
                           "record EXISTS (it is not absent)")
    if not math.isfinite(entropy):
        imgui.text_disabled("entropy: not finite")
