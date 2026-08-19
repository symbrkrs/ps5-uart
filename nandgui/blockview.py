"""Block stripe view: one block number across all 12 CEs.

x = page, y = CE, drawn as one textured quad (12 x pages), so a 1104-page TLC
stripe costs one upload instead of 13k widgets.

This module owns two async probes, and it submits both to the Worker and
consumes their Results.  It never touches a DumpDevice -- everything in here
came off the worker thread:

  ("stripe",)         model.probe_block_stripe -- the five PageStates, from the
                      RAW store's index.  Ref-only: no bytes are read.
  ("stripe-status",)  model.probe_block_status -- the decoded store's recorded
                      per-page decode status.  Also ref-only (page_ref ->
                      src.name), which is why a whole 12 x 1104 stripe of it
                      costs 129 ms (measured, block 3596) instead of the ~8
                      minutes 13k live ECC decodes would.

Two keys, not one, and two independent seq guards.  Scrubbing the block slider
must coalesce each probe on its own, a mode flip must not discard the PageState
stripe, and -- the part that would be a silent lie -- a status result must never
be accepted against the PageState stripe's seq or a new block's statuses paint
under the old block's states.

The stripe honours the drive map's colour mode.  The map colours a BLOCK; this
colours a PAGE, which is the whole point: the map can say "this block's ECC is
not clean" and finding which page used to mean opening them one at a time.
Measured on block 3596: 12,144 pages, and exactly two of them are the
ecc-diverged ones.  palette.stripe() owns the per-mode classification and says,
in one sentence per mode, what the colours mean; this file draws it, counts it
exactly, and prints that sentence in the footer every frame.

Six of the nine modes (CELL, WERU, OWNER, BIN, ERASES, DEFECT) are per-BLOCK
properties.  The choice made here, stated so it cannot be mistaken for a bug:
the whole CE row is painted with the block's value, and the footer says in words
that the mode does not vary by page.  It is honest -- every page of the block
really does carry that block's WERU / owner / erase count -- and it turns the
stripe into a twelve-CE comparison of one block's metadata, which no other view
offers.  The alternative (grey the stripe out) throws away a real view to avoid
a misreading that one sentence prevents.

Palette-rule enforcement lives in four places in this file, and all four are
deliberate:

1.  While a probe is in flight the stripe is filled with PageState.UNKNOWN,
    which has its own colour.  An unprobed page must never be painted as
    erased media -- that is exactly the merge this project has already made
    twice.
2.  Structural fill (an SLC row's pages 368..1103 inside a TLC-wide stripe,
    and dead CE0) is PageState.ABSENT, never ERASED: "no record" is a hole,
    not clean media.
3.  When the view is zoomed out past one pixel per page, the stripe is
    downsampled by precedence (rare-and-dangerous wins), not by point
    sampling.  Point sampling a 1104-page block into 900 px would silently
    drop a lone SKIPPED page; _reduce_states makes that page win its pixel.
4.  Per-class counts for every class of the CURRENT mode are printed above the
    stripe every frame, even at zero, so the picture is only ever a pointer and
    the numbers are always the claim -- but they are labelled for what they are.
    In CONTENT mode the stripe is built by model.probe_block_stripe, which is
    ref-only: it asks the index whether each page exists and how long it is, and
    never reads a byte.  PageState.ZERO needs bytes, so it can never appear
    there, and printing "all-zero 0" under that would be "190 MB of zeros
    reported as content" rebuilt inside the viewer written to prevent it.  So
    the ZERO count reads "n/a" instead of "0", and when the summary flags this
    block all-zero on any CE a banner says so above the stripe.  (In ECC mode
    the same pages do appear, as "N/A -- all 0x00": the decoded store recorded
    `all-zero-retired` for them.  That is not a contradiction, it is a second
    source with bytes behind it, and the footer names which source is on
    screen.)
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
from imgui_bundle import imgui

from . import N_BLOCK, N_CE, PAGES_SLC, PAGES_TLC, gltex, model, palette
from .mapview import (GRID_COL, MAX_UPSCALE as _MAX_UPSCALE, one_line,
                      same_line_or_wrap, stroke_rect)
from .model import PageState, Summary
from .worker import JOB_BLOCK_STRIPE, PRIORITY_BACKGROUND, Result, Worker

# The coalescing keys for this view: scrubbing the block slider replaces the
# pending job instead of queueing 100 of them.  Two keys, both under this
# view's JOB_BLOCK_STRIPE prefix so no other view can collide with either, and
# separate from each other so that a mode flip (which only needs the status)
# cannot discard the PageState stripe, and vice versa.
_KEY = (JOB_BLOCK_STRIPE,)
_STATUS_KEY = (JOB_BLOCK_STRIPE + "-status",)

_ZOOM_MIN = 0.02          # px per page; below this a whole TLC block is a smear
_ZOOM_MAX = 64.0
_ZOOM_STEP = 1.15

#: Screen pixels panned per wheel unit for an unmodified wheel / two-finger
#: swipe, converted to pages by the live zoom so the gesture covers the same
#: screen distance whether one page is 0.02 px wide or 64.
_WHEEL_PAN_PX = 90.0
# There is no minimum row height. There used to be one (4 px, "a
# CE row thinner than this is unclickable") and it was applied as a floor on
# row_h, which meant that in a canvas with less than 48 px of room the image
# was taller than the canvas by construction and CE11 went under a scrollbar --
# an unclickable row is a nuisance, a row you cannot see reads as a CE that
# does not exist. The rows now divide whatever room there is, floored at 1 px
# so none of them can round away.
_ROW_MAX_PX = 64.0
#: Left strip for the "CE n" labels. MEASURED from the widest label actually
#: drawn, not guessed: at 34 px a two-digit "CE10"/"CE11" ran under the stripe,
#: so the quad overlapped the text it was supposed to sit beside. Computed once
#: per frame from the live font, because the hexdump font change moved it.
_GUTTER_MIN_PX = 34.0


def _gutter_px() -> float:
    """Width the CE gutter needs for the widest label, plus breathing room."""
    try:
        w = imgui.calc_text_size(f"CE{N_CE - 1}").x
    except Exception:                                        # noqa: BLE001
        return _GUTTER_MIN_PX
    # 2 px indent on the left (where the label is drawn) + 6 px before the quad.
    return max(_GUTTER_MIN_PX, float(w) + 8.0)

# ---- overlay colours -------------------------------------------------------
# ANYTHING DRAWN ON TOP OF A CELL IS OPAQUE, BECAUSE A BLENDED OVERLAY DOES NOT
# Tint a CELL, it invents a colour that is in no legend.  The long form of the
# rule, and the three routes a blend gets in by, are in mapview.py where the map
# overlay constants live; this file had two of the three:
#
#  * The CE separators used imgui.get_color_u32(Col_.border), and that style
#    colour is (110,110,128,128) -- fifty per cent.  Over ABSENT (14,12,20) the
#    blend is (62,61,74), which is coincidentally the exact value of
#    mapview.GRID_COL, so it passed an eyeball check and a code comment claiming
#    it left "the palette bytes above and below untouched" for years.  Over
#    ERASED and PROGRAMMED the same call wrote (83,111,168) and (84,143,112) --
#    2,124 and 1,423 pixels of colours in no legend, and 11,698 blended pixels
#    over all 11 separator rows.  It is now the same opaque value the drive map
#    uses, so the two views agree by construction rather than by coincidence.
#  * The selection ring was add_rect(..., ImVec4(1,1,1,0.9), 0.0, 2.0): alpha
#    230 (so its best pixel was a 90% blend and it could never paint an opaque
#    one) drawn through the AA polyline path from fractional coordinates
#    (x0 from a fractional cell_w, y0 from row_h = 27.83).  Measured: exactly 1
#    pixel of the whole ring reached white, against 89 in ten blend shades.  It
#    is now mapview.stroke_rect -- four opaque quads on whole pixels.
SEP_COL = GRID_COL                    # (62,61,74) opaque; shared with the map
SELECT_COL = 0xFFFFFFFF               # opaque white

#: Every colour this module may put on the stripe that is not part of the
#: picture, as rgb.  app._self_test's canvas-purity check reads the stripe quad
#: back off the framebuffer and fails on any pixel that is neither a colour of
#: the array this view uploaded (LAST_CANVAS["image"], which covers all nine
#: colour modes including the hashed WERU hues) nor one of these.
OVERLAY_COLORS: tuple[tuple[int, int, int], ...] = (
    (62, 61, 74),        # SEP_COL
    (255, 255, 255),     # SELECT_COL
)

#: Where the stripe quad landed last frame, in imgui screen pixels:
#: (x, y, w, h), plus `image`, the exact RGBA array that was uploaded for it.
#: Written by _draw_canvas, read only by app._self_test.  The gutter is
#: Not in the rect: the CE labels drawn there are text at a
#: fractional y in a translucent grey, which is fine -- they are panel space,
#: not cells, and suppressing them changed the canvas census by zero pixels.
#:
#: `image` is what makes the purity check mode-aware.  It used to be enough to
#: allow palette.STATE_RGBA there, because the stripe only ever painted page
#: states; it now paints WERU hashes and erase ramps too, and no static table
#: can enumerate a hashed hue.  So the check compares the back buffer with the
#: colours of the array this view actually uploaded -- exactly what mapview has
#: always done -- and an overlay that blends still fails, which is the point.
LAST_CANVAS: dict[str, object] = {}

#: True when the stripe's own window (or a child of it) had keyboard focus on
#: the last frame it drew.  app.draw_legend reads it: when the stripe is what
#: the reader is looking at, the mode legend has to describe what the stripe is
#: showing, which for six of the nine modes is a different thing from what the
#: map is showing.
LAST_FOCUSED: dict[str, bool] = {"focused": False}

# Visibility precedence used when several pages share one screen pixel.
# Highest rank wins.  Same principle as palette.block_states: rare and
# dangerous beats common, so a skipped page cannot hide inside a run of
# programmed ones and an absent hole cannot hide inside erased media.
#
# The rank is per mode, and it has to be: reducing an ECC stripe by PageState
# precedence would rank `ecc-diverged` against `erased` by the wrong yardstick
# and a lone diverged page would lose its pixel to a neighbour at fit zoom --
# the same defect mapview fixed with palette.ecc_ranks. Every mode's rank now
# rides on the palette.Stripe (built from palette.STATE_SEVERITY,
# STRIPE_ECC_SEVERITY or STRIPE_MCRC_SEVERITY), so the ordering lives once,
# next to the colours it orders. These two remain as the CONTENT case, because
# they are exactly palette.STATE_SEVERITY and the assertion below proves it.
_RANK = np.zeros(len(PageState), dtype=np.uint8)
_RANK[PageState.ERASED] = 0
_RANK[PageState.UNKNOWN] = 1
_RANK[PageState.PROGRAMMED] = 2
_RANK[PageState.ZERO] = 3       # cannot arrive from a ref-only probe; see
                                # _draw_counts. Ranked anyway so that a future
                                # byte-reading stripe needs no edit here.
_RANK[PageState.ABSENT] = 4
_RANK[PageState.SKIPPED] = 5
_UNRANK = np.zeros(len(PageState), dtype=np.uint8)
for _state_value, _rank_value in enumerate(_RANK):
    _UNRANK[_rank_value] = _state_value

# One ordering, two spellings: if these ever diverge, the CONTENT stripe and
# every other view's idea of "rare beats common" have diverged with them.
assert tuple(int(v) for v in _RANK) == tuple(
    int(v) for v in palette._invert_ranks(palette.STATE_SEVERITY,
                                          len(PageState))[0])

# Private per-state caches.  BlockState's field list is frozen by the contract,
# so these live as attributes under reserved names (the same pattern as
# pageview._SCROLL_ATTR) and each holds the array object it was derived from,
# so a stale entry cannot survive a new stripe.
_TEXSIG_ATTR = "_blockview_texsig"
#: The decoded store's per-page status for this block: a _Status holding the
#: model.BlockStatus, its seq, and whether a probe is in flight.  Reserved
#: attribute for the same reason as the others.
_STATUS_ATTR = "_blockview_status"
#: The palette.Stripe built for (states, status, mode, block), cached against
#: the array objects it came from so a new probe result invalidates it by
#: identity -- there is no id() to recycle and no invalidation call to forget.
_STRIPE_ATTR = "_blockview_stripe"
#: The last mode draw() was called with, so request() knows whether the new
#: block needs a status probe before the next draw can ask for one.
_MODE_ATTR = "_blockview_mode"
# True once the stripe has been fitted to a canvas that had a real width; see
# _draw_canvas. False marks a fit made before the dock layout existed, which
# has to be redone or the stripe stays a 64 px sliver forever.
_FITTED_ATTR = "_blockview_fitted"
# Last frame's scroll_max_y for the canvas child, in pixels. Anything above
# zero means the stripe is taller than the room it was given and a CE row is
# hidden under a scrollbar -- the exact regression this sizing pass fixed, so
# --self-test can assert on it (app._self_test).
_OVERFLOW_ATTR = "_blockview_overflow"


@dataclasses.dataclass
class BlockState:
    block: int = 0
    states: np.ndarray | None = None        # uint8[12, max_pages] of PageState
    tex: gltex.Texture | None = None
    zoom: float = 1.0
    pan: tuple[float, float] = (0.0, 0.0)
    hover: tuple[int, int] | None = None    # (ce, page)
    selected: tuple[int, int] | None = None
    loading: bool = False
    seq: int = -1                           # worker seq this state is waiting on
    error: str | None = None
    weru_id: int = -1                       # from summary, for the title
    #: Give the twelve CE rows the whole canvas height instead of stopping at
    #: _ROW_MAX_PX. Set by the Fit button and by the initial auto-fit, which
    #: fit both axes: the pages have to land inside the canvas on x and the
    #: twelve CE rows on y. It does not lock anything -- `zoom` is still the x
    #: scale and the wheel still moves it.
    fill_height: bool = False


@dataclasses.dataclass(frozen=True)
class BlockEvent:
    hover: tuple[int, int] | None = None
    clicked: tuple[int, int] | None = None      # (ce, page) -> page inspector
    requested_block: int | None = None          # user typed/stepped a block


# ---------------------------------------------------------------- placeholder


def _row_pages(summary: Summary | None, block: int) -> np.ndarray:
    """Pages per CE row for `block` -> int32[N_CE].  Vectorised: the summary is
    indexed ce*N_BLOCK + block, so one strided slice is the whole column."""
    if summary is None:
        # No summary: assume the wider geometry, so we never invent an ABSENT
        # tail for pages that may well exist.
        return np.full(N_CE, PAGES_TLC, dtype=np.int32)
    try:
        slc = np.asarray(summary.slc)[block::N_BLOCK]
    except Exception:
        return np.full(N_CE, PAGES_TLC, dtype=np.int32)
    if slc.shape[0] < N_CE:                    # short/odd summary: assume wide
        slc = np.concatenate([slc, np.zeros(N_CE - slc.shape[0], slc.dtype)])
    return np.where(slc[:N_CE].astype(bool), PAGES_SLC, PAGES_TLC).astype(np.int32)


def _placeholder(summary: Summary | None, block: int) -> np.ndarray:
    """uint8[N_CE, max_pages] shown until the probe lands.

    Everything real is UNKNOWN -- a distinct colour, never erased.  Only two
    kinds of cell are asserted, and both are structural facts rather than
    guesses about content: row 0 is the dead CE0, which has no records at all,
    and each row's tail past its own page count does not exist in that block's
    geometry.  Both are ABSENT, i.e. a hole.
    """
    rows = _row_pages(summary, block)
    max_pages = int(rows.max())
    out = np.full((N_CE, max_pages), PageState.UNKNOWN, dtype=np.uint8)
    tail = np.arange(max_pages)[None, :] >= rows[:, None]
    out[tail] = PageState.ABSENT
    out[0, :] = PageState.ABSENT              # CE0 is dead and absent, always
    return out


def _zero_flagged_ces(summary: Summary | None, block: int) -> tuple[int, ...]:
    """CEs whose summary row flags this block as uniform 0x00.

    The stripe itself cannot see ZERO (see the module docstring), so this is
    the only place the fact can reach this panel -- and it is reported as what
    it is, a per-BLOCK flag out of the summary, not painted onto pages.
    """
    if summary is None:
        return ()
    try:
        col = np.asarray(summary.zero)[block::N_BLOCK]
    except Exception:
        return ()
    return tuple(int(c) for c in np.flatnonzero(col[:N_CE].astype(bool)))


def _weru_for(summary: Summary | None, block: int) -> int:
    """First WERU id claiming this block column, or -1.  A WERU is a stripe of
    the same block across CEs, so the column normally carries a single id."""
    if summary is None:
        return -1
    try:
        col = np.asarray(summary.weru)[block::N_BLOCK]
    except Exception:
        return -1
    hit = np.flatnonzero(col >= 0)
    return int(col[hit[0]]) if hit.size else -1


# -------------------------------------------------------------- async plumbing


@dataclasses.dataclass
class _Status:
    """The decoded store's per-page status for one block, and its job state.

    It lives under a reserved attribute on BlockState rather than as a field,
    the same pattern the caches above use, because BlockState's field list is
    frozen by the view contract.
    """

    block: int = -1
    data: object | None = None          # model.BlockStatus
    seq: int = -1                       # its own seq, never state.seq
    loading: bool = False
    error: str | None = None


def _status_of(state: BlockState) -> _Status:
    st = getattr(state, _STATUS_ATTR, None)
    if not isinstance(st, _Status):
        st = _Status()
        setattr(state, _STATUS_ATTR, st)
    return st


def _mode_of(state: BlockState, mode=None) -> palette.Mode:
    """The mode this stripe is being drawn in.  CONTENT until told otherwise.

    Remembered on the state so request() (which is called from app.select, with
    no mode in hand) can queue the status probe for the new block immediately
    instead of waiting for the next draw to notice.
    """
    if mode is not None:
        try:
            mode = palette.Mode(mode)
        except Exception:                                    # noqa: BLE001
            mode = palette.Mode.CONTENT
        setattr(state, _MODE_ATTR, mode)
        return mode
    got = getattr(state, _MODE_ATTR, None)
    return got if isinstance(got, palette.Mode) else palette.Mode.CONTENT


def request_status(state: BlockState, worker: Worker,
                   summary: Summary | None) -> None:
    """Submit the decoded-store status probe for state.block, if needed.

    Coalesced on its own key at PRIORITY_BACKGROUND, and idempotent: calling it
    every frame (which draw() does) queues at most one job per block, because a
    job already in flight for this block is neither replaced nor duplicated.
    """
    st = _status_of(state)
    if st.block == state.block and (st.loading or st.data is not None):
        return
    if worker is None:                      # headless use / tests
        return
    block = int(state.block)
    st.block = block
    st.data = None
    st.error = None
    st.loading = True

    def job(ctx, _block=block, _summary=summary):
        # ctx is the Stores built by the worker's setup hook; that thread is
        # the only one that ever touches a DumpDevice.
        return model.probe_block_status(ctx, _block, _summary, N_CE)

    st.seq = worker.submit(_STATUS_KEY, job, PRIORITY_BACKGROUND)


def _ensure_status(state: BlockState, worker: Worker, summary: Summary | None,
                   mode: palette.Mode) -> None:
    """Request or cancel the status probe as the mode requires.

    Cancelling matters: flipping away from ECC while a 130 ms probe is queued
    leaves a job nobody will look at ahead of the page fetch the user is
    waiting for.  A job already running is not interrupted -- it lands, and
    apply() files it for whenever the mode comes back.
    """
    if mode in palette.STRIPE_STATUS_MODES:
        request_status(state, worker, summary)
        return
    st = _status_of(state)
    if st.loading and worker is not None:
        worker.cancel(_STATUS_KEY)
        st.loading = False


def request(state: BlockState, worker: Worker, block: int,
            summary: Summary | None) -> None:
    """Set state.block and submit the stripe probe at PRIORITY_BACKGROUND."""
    block = max(0, min(N_BLOCK - 1, int(block)))
    changed = block != state.block
    state.block = block
    state.weru_id = _weru_for(summary, block)
    state.error = None

    # A new block invalidates the old stripe: leaving the previous block's
    # states under a new title would be a lie with authority behind it.
    # UNKNOWN is the honest fill.
    if changed or state.states is None:
        state.states = _placeholder(summary, block)
        state.selected = None
        state.hover = None

    # ... and it invalidates the old block's per-page statuses in exactly the
    # same way, for exactly the same reason. There is no honest fill for those:
    # the mode simply reports "not probed" until the new ones land.
    st = _status_of(state)
    if changed or st.data is None:
        st.data = None
        st.error = None
        st.block = -1

    if worker is None:                      # headless use / tests
        state.loading = False
        return

    def job(ctx, _block=block, _summary=summary):
        # ctx is the Stores built by the worker's setup hook; that thread is
        # the only one that ever touches a DumpDevice.
        return model.probe_block_stripe(ctx, _block, _summary, N_CE)

    state.loading = True
    state.seq = worker.submit(_KEY, job, PRIORITY_BACKGROUND)
    # Queue the status for the new block now rather than next frame, but only
    # for a mode that needs it: the last mode draw() saw is the only mode this
    # function can know about.
    _ensure_status(state, worker, summary, _mode_of(state))


def _apply_status(state: BlockState, result: Result) -> bool:
    """Consume a ("stripe-status",) Result.  Its own seq gate, never state.seq.

    The two guards must not be crossed: accepting a status result against the
    PageState stripe's seq would let a new block's statuses paint under the old
    block's states, which is the same lie one layer over from the one the seq
    stamp exists to prevent.
    """
    st = _status_of(state)
    if result.seq < st.seq:
        return True                          # ours, but stale: drop it
    st.loading = False
    if result.error:
        st.error = result.error
        st.data = None
        return True
    data = result.value
    if data is None or getattr(data, "codes", None) is None:
        st.error = "the per-page status probe returned nothing"
        st.data = None
        return True
    # The block is taken from the result, not from state.block: a result that
    # arrives after the user has moved on describes the block it was asked
    # about, and _stripe_for refuses to draw it against any other.
    st.block = int(getattr(data, "block", state.block))
    st.error = getattr(data, "error", None)
    st.data = data
    return True


def apply(state: BlockState, result: Result) -> bool:
    """Consume our Result if it is not stale.  Returns True if consumed."""
    if result is None:
        return False
    if result.key == _STATUS_KEY:
        return _apply_status(state, result)
    if result.key != _KEY:
        return False
    if result.seq < state.seq:
        # A slow probe for a block the user has already left.  Dropping it is
        # the entire point of the seq stamp.
        return False

    state.loading = False
    if result.error:
        state.error = result.error
        # states is left as-is (placeholder or previous stripe for this block):
        # a failed probe must not repaint anything as erased.
        return True

    value = result.value
    if value is None:
        state.error = "stripe probe returned nothing"
        return True

    arr = np.asarray(value)
    if arr.ndim != 2 or arr.shape[0] < 1 or arr.shape[1] < 1:
        state.error = f"stripe probe returned shape {arr.shape}, expected (ce, pages)"
        return True

    state.error = None
    state.states = np.ascontiguousarray(arr, dtype=np.uint8)
    return True


# ------------------------------------------------------------------ rendering


def _status_for(state: BlockState) -> object | None:
    """The BlockStatus for this block, or None.

    The block test is the guard: a status probe that lands after the user has
    moved on carries the block it was asked about, and drawing it under another
    block's number is precisely the "older page under a newer address" lie this
    viewer exists to avoid.
    """
    st = _status_of(state)
    if st.data is None or int(getattr(st.data, "block", -1)) != int(state.block):
        return None
    return st.data


def _geometry_conflict(status) -> str | None:
    """Do the two stores disagree about this block's geometry?  -> a sentence.

    They are two separate indexes.  If the decoded store answers for a CE as
    pseudo-SLC while the raw store answers for it as native TLC, then page 5 of
    one is not page 5 of the other and nothing can be lined up -- so the
    statuses are refused for the whole stripe rather than shifted onto pages
    they do not describe.  Measured on this capture: 0 disagreements over 121
    (ce, block) pairs, including the mixed block 1815 where CE7 alone is SLC.
    """
    try:
        slc = np.asarray(status.slc, dtype=bool)
        raw_slc = np.asarray(status.raw_slc, dtype=bool)
    except Exception:                                        # noqa: BLE001
        return None
    bad = np.flatnonzero(slc != raw_slc)
    if not bad.size:
        return None
    parts = ["CE%d (raw %s, decoded %s)"
             % (ce, "SLC" if raw_slc[ce] else "TLC",
                "SLC" if slc[ce] else "TLC") for ce in bad[:4]]
    return ("the two stores disagree about this block's cell mode on %s, so "
            "their page numbers do not describe the same pages"
            % ", ".join(parts))


def _stripe_for(state: BlockState, summary: Summary | None,
                mode: palette.Mode, states: np.ndarray) -> palette.Stripe:
    """The palette.Stripe for this frame, cached against its inputs.

    The signature holds the array / result objects, so a new probe result
    invalidates the cache by identity: there is no id() to recycle and no
    invalidation call to forget, which matters because a stale stripe is
    exactly the kind of lie this view exists to prevent.
    """
    status = _status_for(state)
    conflict = None if status is None else _geometry_conflict(status)
    sig = (states, status, mode, int(state.block), summary, conflict)
    cached = getattr(state, _STRIPE_ATTR, None)
    if (cached is not None and cached[0][0] is sig[0]
            and cached[0][1] is sig[1] and cached[0][2:] == sig[2:]):
        return cached[1]

    if conflict is not None and mode in palette.STRIPE_STATUS_MODES:
        built = palette._pending_stripe(
            mode, states,
            "%s per page was DISCARDED: %s." % (palette.MODE_LABELS[mode],
                                                conflict),
            conflict, source=str(getattr(status, "path", "")))
    else:
        built = palette.stripe(mode, states, status=status, summary=summary,
                               block=int(state.block),
                               group2=palette.group2_row(int(state.block),
                                                         states.shape[0]))
    setattr(state, _STRIPE_ATTR, (sig, built))
    return built


def _reduce_states(sub: np.ndarray, out_w: int, ranks: np.ndarray | None = None,
                   unranks: np.ndarray | None = None) -> np.ndarray:
    """Downsample uint8 class columns to `out_w` by precedence.

    Point sampling -- which is what GL_NEAREST would do for us if we handed it
    the full-width stripe -- drops rare states: one skipped page in a 1104-page
    block would simply vanish at fit zoom.  Reducing over the rank LUT means
    the rarest/most dangerous state in each pixel's bin is the one drawn.

    Why this cannot reintroduce smearing below one pixel per page:

      * It reduces over class codes through the rank LUT and maps the winner
        back with its inverse, so every output cell is a real class that some
        page in that bin actually has.  No colour is touched here at all;
        Stripe.colors() runs after the reduction, on codes.  Averaging would
        produce a code that names no class, which is a worse version of the
        same lie GL_LINEAR would tell in colour space.
      * The LUTs are the CURRENT MODE's (palette.Stripe.ranks/unranks), so the
        winner is the rarest/most serious thing in that mode.  Ranking an ECC
        stripe by PageState precedence would let a lone ecc-diverged page lose
        its pixel to a neighbour -- the pixel this feature exists to show.
      * out_w is the drawn width in whole pixels (see _draw_canvas: out_w =
        round(draw_w), and the texture is then drawn at draw_w), so the stripe
        texture is 1:1 with its destination on x and GL_NEAREST is the identity
        there.  On y the 12-row texture is magnified to row_h px per CE, which
        under GL_NEAREST is exact replication -- hard band edges.
      * So the visible artefact at 18 pages/px is a step function of exact
        palette colours, never a ramp between two states.  The per-state counts
        printed above the stripe remain the claim; the picture is a pointer.
    """
    rank = _RANK if ranks is None else np.asarray(ranks)
    unrank = _UNRANK if unranks is None else np.asarray(unranks)
    n = sub.shape[1]
    if out_w >= n or out_w < 1:
        return sub
    # out_w <= n, so floor(i*n/out_w) is strictly increasing -- which is what
    # np.maximum.reduceat requires for its segment starts.
    starts = (np.arange(out_w, dtype=np.int64) * n) // out_w
    return unrank[np.maximum.reduceat(rank[sub], starts, axis=1)]


def _tex_ref(tex: gltex.Texture):
    """gltex hands back whatever imgui.image() wants; imgui-bundle 1.92 wants
    an ImTextureRef, so accept both that and a bare GLuint."""
    ident = tex.imgui_id()
    if isinstance(ident, imgui.ImTextureRef):
        return ident
    return imgui.ImTextureRef(int(ident))


def _vec4(rgba) -> imgui.ImVec4:
    return imgui.ImVec4(*palette.imgui_color(rgba))


def _colored(col: imgui.ImVec4, text: str) -> None:
    """Coloured text via text_unformatted: imgui's Text() is printf-style, and
    a stray '%' in a store error message would otherwise corrupt the frame."""
    imgui.push_style_color(imgui.Col_.text.value, col)
    imgui.text_unformatted(text)
    imgui.pop_style_color()


def _tooltip(text: str) -> None:
    imgui.begin_tooltip()
    imgui.text_unformatted(text)
    imgui.end_tooltip()


def _draw_toolbar(state: BlockState) -> int | None:
    """Block chooser.  Returns a requested block number, or None."""
    requested: int | None = None

    if imgui.arrow_button("##prev", imgui.Dir.left) and state.block > 0:
        requested = state.block - 1
    imgui.same_line()
    imgui.set_next_item_width(110.0)
    changed, value = imgui.input_int("##blocknum", int(state.block), 1, 16)
    if changed:
        requested = value
    imgui.same_line()
    if imgui.arrow_button("##next", imgui.Dir.right) and state.block < N_BLOCK - 1:
        requested = state.block + 1
    imgui.same_line()
    imgui.set_next_item_width(-140.0)
    slid, value = imgui.slider_int("##blockslider", int(state.block),
                                   0, N_BLOCK - 1, "block %d")
    if slid:
        requested = value
    imgui.same_line()
    if imgui.small_button("Fit"):
        state.zoom = 0.0                    # sentinel: refit inside the canvas
        state.pan = (0.0, state.pan[1])
        # Both axes: the sentinel above fits the pages across the canvas width,
        # and this fits the twelve CE rows into its height. Fitting only x left
        # the stripe either short of the panel (rows capped at _ROW_MAX_PX) or
        # taller than it (rows floored at the old 4 px minimum, CE11 under a
        # scrollbar), depending on the panel -- neither of which is a fit.
        state.fill_height = True

    if requested is not None:
        requested = max(0, min(N_BLOCK - 1, int(requested)))
        if requested == state.block:
            requested = None
    return requested


def _draw_counts(stripe: palette.Stripe) -> None:
    """Per-CLASS page counts for the current mode, all named, always -- even at
    zero.  The picture below may be a precedence reduction; these numbers are
    not, and a class that can disappear from this row is a class the reader
    stops looking for.

    The one "n/a" is CONTENT's all-zero, and it is not a special case for the
    sake of it: that stripe is index-derived and the index cannot see ZERO --
    the probe reads no bytes, so PageState.ZERO is structurally impossible in
    that array.  Printing "all-zero 0" would be a measurement claim the data
    cannot support, and a false one: the summary flags 74 blocks that are 100%
    0x00 and whose pages all appear there as PROGRAMMED.  In ECC mode those
    same pages do get counted, because the decoded store recorded
    `all-zero-retired` for them with the bytes in hand.
    """
    n = int(stripe.table.shape[0])
    # Every class of the five-state vocabulary keeps its own swatch and number
    # in CONTENT, always, at zero as well.  The mode stripes have up to
    # thirteen classes and the ECC/owner labels are long: MEASURED at 1024x700,
    # printing all of them cost 226 px of text and put CE11 under a scrollbar,
    # which loses a whole CE to keep a row of zeros on screen.  So a class with
    # no pages in this block is named in one line below instead of taking a
    # row -- it is still named, still counted (at zero), and the stripe legend
    # in the Legend panel still gives every class its own swatch and count.
    shown = [i for i in range(n)
             if stripe.mode == palette.Mode.CONTENT
             or int(stripe.counts[i]) > 0]
    absent_classes = [stripe.labels[i] for i in range(n) if i not in shown]
    for pos, idx in enumerate(shown):
        value = int(stripe.counts[idx]) if idx < stripe.counts.shape[0] else 0
        zero_na = (stripe.mode == palette.Mode.CONTENT
                   and idx == int(PageState.ZERO) and value == 0)
        text = "%s %s" % (stripe.labels[idx], "n/a" if zero_na else value)
        # Wrapped, not chained: six counts are ~514 px of text and the stripe
        # panel is 376 px wide at 1024x700, so an unconditional same_line ran
        # the last two counts off the right edge -- MEASURED as 138 px of
        # window scroll extent, i.e. the claim these numbers exist to make was
        # the part that got clipped. A wrapped second row costs one text line
        # and the canvas below is sized from what is left. The mode stripes
        # have up to thirteen classes, so this matters more, not less.
        if pos:
            same_line_or_wrap(imgui.calc_text_size(text).x)
        _colored(_vec4(_rgba_tuple(stripe.table[idx])), text)
        if imgui.is_item_hovered():
            if zero_na:
                # Not measurable from the index; "0" would be a claim about
                # bytes nobody read.
                _tooltip(
                    "all-zero: not measurable in this mode.\n"
                    "The CONTENT stripe is built from the index alone (no "
                    "bytes are read), and a page of 0x00 is indistinguishable "
                    "from any other programmed page until you read it. Switch "
                    "to ECC mode, which reports the decoded store's recorded "
                    "`all-zero-retired` status per page, or open a page in the "
                    "inspector.")
            else:
                _tooltip("%s: %s" % (stripe.labels[idx], stripe.help[idx]))
    # Both of the lines below are TRIMMED to the panel width with the whole
    # sentence on hover, never wrapped: a wrapped sentence takes its extra
    # lines out of the CE rows below it, and an unwrapped one makes the panel
    # scroll sideways. Same rule, and the same one_line(), as the pages/px
    # footer under the stripe.
    room = max(120.0, float(imgui.get_content_region_avail().x))
    if absent_classes:
        # Named, not dropped: these are the classes with 0 pages in this block.
        text = "0 pages here: " + ", ".join(absent_classes)
        _colored(_vec4(palette.NEUTRAL_RGBA), one_line(text, room))
        if imgui.is_item_hovered():
            _tooltip("Classes of this mode with no page in this block. They "
                     "are part of the vocabulary and keep their own swatch, at "
                     "count 0, in the stripe legend:\n  "
                     + "\n  ".join(absent_classes))
    imgui.text_disabled(one_line(stripe.note, room))
    if imgui.is_item_hovered():
        _tooltip(stripe.note + (("\n\nsource: " + stripe.source)
                                if stripe.source else ""))


def _rgba_tuple(row) -> tuple[int, int, int, int]:
    r = np.asarray(row)
    return (int(r[0]), int(r[1]), int(r[2]),
            int(r[3]) if r.shape[0] > 3 else 255)


def _draw_zero_banner(summary: Summary | None, block: int) -> None:
    """Say when the summary flags this block all-zero on some CE.

    Without it the three views contradict each other on the same block: the
    drive map paints ce1/block1815 magenta ZERO, the inspector calls page 0
    all-zero, and the stripe paints 1104 green PROGRAMMED cells whose legend
    says "real content -- a record with bytes in it".  The banner does not
    repaint the stripe: inheriting a per-block metadata flag onto 1104
    individual pages would be the same metadata-driven claim that nearly lost
    the boot block.  The pages stay as measured; the flag is stated as a flag.
    """
    ces = _zero_flagged_ces(summary, block)
    if not ces:
        return
    _colored(_vec4(palette.STATE_RGBA[PageState.ZERO]),
             "summary flags block %d as ALL-ZERO on CE %s"
             % (block, ", ".join(str(c) for c in ces)))
    if imgui.is_item_hovered():
        _tooltip(
            "uniform 0x00 -- the dead-CE bus constant, NOT erased media.\n"
            "In Content mode the stripe below is index-derived and paints "
            "those pages as 'programmed', because existence is all the index "
            "records. In ECC mode it paints them 'N/A -- all 0x00', which is "
            "the decoded store's recorded `all-zero-retired` status, read with "
            "the bytes in hand. Open a page in the inspector to confirm.")


def _hover_text(state: BlockState, summary: Summary | None,
                stripe: palette.Stripe, ce: int, page: int,
                state_value: int) -> str:
    """The tooltip: the address, the PAGE state always, and then what the
    CURRENT mode is showing for this page -- including, for the ECC and MCRC
    modes, the store's recorded status name verbatim and the store's own
    sentence about it, so nobody has to trust this viewer's mapping.
    """
    slc = None
    if summary is not None:
        try:
            if summary.valid(ce, state.block):
                slc = summary.is_slc(ce, state.block)
        except Exception:
            slc = None
    geom = "" if slc is None else ("  pseudo-SLC" if slc else "  native TLC")
    weru = "" if state.weru_id < 0 else f"  weru {state.weru_id}"
    label = (palette.STATE_LABELS[state_value]
             if state_value < len(palette.STATE_LABELS) else "?")
    help_ = (palette.STATE_HELP[state_value]
             if state_value < len(palette.STATE_HELP) else "")
    out = [f"CE {ce}  block {state.block}  page {page}{geom}{weru}",
           f"{label}: {help_}"]
    if stripe.mode != palette.Mode.CONTENT:
        try:
            code = int(stripe.codes[ce, page])
        except Exception:                                    # noqa: BLE001
            code = -1
        if 0 <= code < len(stripe.labels):
            out.append("%s: %s" % (palette.MODE_LABELS[stripe.mode],
                                   stripe.labels[code]))
            out.append(stripe.help[code])
    status = _status_for(state)
    if status is not None and stripe.mode in palette.STRIPE_STATUS_MODES:
        try:
            recorded = status.describe(ce, page)
        except Exception:                                    # noqa: BLE001
            recorded = ""
        if recorded:
            out.append("recorded: " + recorded)
    return "\n".join(out)


def draw(state: BlockState, summary: Summary,
         label: str = "Block stripe", mode=None,
         worker: Worker | None = None) -> BlockEvent:
    """Draw the stripe.  UI thread only -- no store access anywhere in here.

    `mode` is the app's selected palette.Mode; the stripe honours it, which is
    the whole feature (the map colours a block, this colours its pages).  With
    `worker` in hand the modes that need the decoded store's per-page status
    request it here, coalesced on their own key -- and cancel it when the mode
    no longer needs it, so a probe nobody will look at cannot sit in front of
    the page fetch the user is waiting for.
    """
    hover: tuple[int, int] | None = None
    clicked: tuple[int, int] | None = None
    mode = _mode_of(state, mode)

    imgui.push_id(label)
    try:
        requested = _draw_toolbar(state)

        # TRIMMED and wrapped, both measured: the mode name added to this line
        # made the panel 80 px wider than itself at 1024x700, i.e. the view
        # reported itself as wider than the panel it had just been fitted to.
        # one_line() trims (whole text on hover) and same_line_or_wrap() puts
        # the probe chip on a second row instead of off the right edge.
        room = max(120.0, float(imgui.get_content_region_avail().x))
        title = f"{label}: block {state.block} x {N_CE} CEs"
        if state.weru_id >= 0:
            title += f"  (WERU {state.weru_id})"
        title += "  |  mode: %s" % palette.MODE_LABELS[mode]
        imgui.text_unformatted(one_line(title, room))
        if imgui.is_item_hovered():
            _tooltip(title)
        if state.error:
            chip = (imgui.ImVec4(1.0, 0.45, 0.35, 1.0),
                    f"probe failed: {state.error}")
        elif state.loading:
            chip = (imgui.ImVec4(0.75, 0.75, 0.35, 1.0),
                    "probing (stripe shows 'not probed')")
        else:
            chip = (imgui.ImVec4(0.6, 0.6, 0.6, 1.0), "probed")
        chip = (chip[0], one_line(chip[1], room))
        same_line_or_wrap(imgui.calc_text_size(chip[1]).x)
        _colored(*chip)

        states = state.states
        if states is None:
            states = _placeholder(summary, state.block)
            state.states = states

        _ensure_status(state, worker, summary, mode)
        _draw_status_line(state, mode)

        if states.ndim != 2 or states.shape[1] < 1:
            _colored(_vec4(palette.ABSENT_RGBA),
                     "no pages in this block -- no records at all")
            return BlockEvent(hover=None, clicked=None,
                              requested_block=requested)

        stripe = _stripe_for(state, summary, mode, states)
        _draw_counts(stripe)
        _draw_zero_banner(summary, state.block)

        # begin/end_child is paired in a finally: if anything inside the canvas
        # throws, imgui's window stack still has to unwind, or the next frame
        # asserts somewhere unrelated and hides the real bug.
        imgui.begin_child("##stripe_canvas", imgui.ImVec2(0.0, 0.0),
                          imgui.ChildFlags_.borders.value)
        try:
            hover, clicked = _draw_canvas(state, summary, states, stripe)
        finally:
            imgui.end_child()
    finally:
        # In the finally so the early return above cannot leave a stale flag:
        # a stripe that draws nothing but "no records at all" is still the
        # window the reader is looking at, and the legend has to follow.
        LAST_FOCUSED["focused"] = bool(
            imgui.is_window_focused(
                imgui.FocusedFlags_.root_and_child_windows.value))
        imgui.pop_id()

    return BlockEvent(hover=hover, clicked=clicked, requested_block=requested)


def _draw_status_line(state: BlockState, mode: palette.Mode) -> None:
    """One line about the second probe, whenever the mode depends on it.

    A mode whose data is still in flight says so, in the same place and the
    same words as the PageState probe does, rather than showing a stripe of
    "not probed" with nothing to explain it.
    """
    if mode not in palette.STRIPE_STATUS_MODES:
        return
    st = _status_of(state)
    data = _status_for(state)
    if st.error:
        col, text = (imgui.ImVec4(1.0, 0.45, 0.35, 1.0),
                     "per-page status unavailable: %s" % st.error)
    elif st.loading or data is None:
        col, text = (imgui.ImVec4(0.75, 0.75, 0.35, 1.0),
                     "reading the decoded store's per-page status "
                     "(stripe shows 'not probed')")
    else:
        col, text = (imgui.ImVec4(0.6, 0.6, 0.6, 1.0),
                     "per-page status from %s"
                     % (getattr(data, "path", "") or "the decoded store"))
    # Trimmed, whole sentence on hover: same rule as every other line in this
    # panel -- it must not make the panel wider than itself.
    room = max(120.0, float(imgui.get_content_region_avail().x))
    _colored(col, one_line(text, room))
    if imgui.is_item_hovered():
        _tooltip(text)


def stripe_legend(state: BlockState, summary: Summary | None,
                  mode=None) -> tuple[str, list[palette.LegendEntry], str]:
    """(heading, entries, note) describing what the stripe is showing.

    app.draw_legend calls this when the stripe has focus.  It is built from the
    same palette.Stripe the pixels came from -- not from a parallel table that
    could drift -- so the legend cannot describe a picture the view is not
    drawing.
    """
    mode = _mode_of(state, mode)
    states = state.states
    if states is None or states.ndim != 2:
        return ("block stripe: %s" % palette.MODE_LABELS[mode], [], "")
    stripe = _stripe_for(state, summary, mode, states)
    heading = ("block stripe (block %d), %s -- per PAGE"
               % (state.block, palette.MODE_LABELS[mode]))
    if stripe.per_block:
        heading = ("block stripe (block %d), %s -- per BLOCK, one value per CE"
                   % (state.block, palette.MODE_LABELS[mode]))
    return (heading, palette.stripe_legend(stripe), stripe.note)


def canvas_overflow(state: BlockState) -> float:
    """Pixels of stripe the canvas child could not show last frame.

    Zero is the contract: the stripe is sized to its room, footer included, so
    anything above zero means a CE row is under a scrollbar.
    """
    return float(getattr(state, _OVERFLOW_ATTR, 0.0))


def _draw_canvas(state: BlockState, summary: Summary | None,
                 states: np.ndarray,
                 stripe: palette.Stripe) -> tuple[tuple[int, int] | None,
                                                  tuple[int, int] | None]:
    """The textured quad plus hit-testing.  Returns (hover, clicked).

    `stripe` carries the CURRENT MODE's class codes, colour table and
    precedence; `states` stays the PageState array, because the hit-test, the
    geometry and the always-shown page-state half of the tooltip are the same
    in every mode.
    """
    hover: tuple[int, int] | None = None
    clicked: tuple[int, int] | None = None
    n_ce, n_pages = states.shape

    avail = imgui.get_content_region_avail()
    room_w = float(avail.x) - _gutter_px()
    canvas_w = max(64.0, room_w)
    # room_w below the floor means the dock layout does not exist yet -- see
    # the provisional-fit note below.
    laid_out = room_w > 64.0

    # zoom <= 0 is the "fit" sentinel for the x axis, and tex is None only on
    # the first draw ever -- so the width fit needs no extra field. The height
    # half of a fit is state.fill_height, set here too: an auto-fit is a fit,
    # and a fit is both axes or it is not a fit.
    if state.zoom <= 0.0 or state.tex is None:
        state.zoom = canvas_w / float(n_pages)
        state.fill_height = True            # the initial auto-fit, both axes
        setattr(state, _FITTED_ATTR, laid_out)
    elif laid_out and not getattr(state, _FITTED_ATTR, True):
        # The first frames run before the dockspace has a layout, and avail.x
        # collapses to the 64 px floor. Fitting there pinned zoom at
        # 64/1104 for the whole session: a 1104-page stripe drawn 64 px wide
        # inside a 1140 px panel, which looked like a broken probe rather than
        # a broken fit. Redo that provisional fit once, when a real width
        # arrives -- and only once, or every window resize would silently
        # throw away the zoom the user chose.
        state.zoom = canvas_w / float(n_pages)
        state.fill_height = True
        setattr(state, _FITTED_ATTR, True)
    state.zoom = float(min(_ZOOM_MAX, max(_ZOOM_MIN, state.zoom)))

    # The "n pages/px" footer is drawn inside this child, below the image, so
    # its line has to come out of the height the CE rows may use. It used to be
    # placed at image_h + 2 with image_h already equal to the full region: the
    # sentence landed past the bottom edge, the child scrolled, and CE11 sat
    # under the scrollbar. Reserved from the real text metrics, and only when
    # that footer is actually drawn (zoom < 1.0, decided just above and
    # independent of the height, so this cannot oscillate). It is one line by
    # construction -- the sentence at the bottom of this function goes through
    # one_line(), which trims it to the canvas width instead of letting it wrap
    # (a second line out of the CE rows) or overhang (horizontal scroll).
    footer_h = (imgui.get_text_line_height_with_spacing() + 2.0
                if state.zoom < 1.0 else 0.0)
    # Never floored above the room the child actually has: a 24 px floor inside
    # 18 px of canvas is an image taller than its parent by construction, which
    # is the scrollbar over CE11 all over again. n_ce px (1 px per row) is the
    # smallest height at which twelve rows can all still exist.
    canvas_h = max(float(n_ce), float(avail.y) - footer_h)
    # The height of a CE row, and the second half of "Fit":
    #   fitted     -> the twelve rows divide the canvas exactly, so
    #                 image_h == canvas_h and nothing is short or clipped.
    #   unfitted   -> the old cap, so a view nobody fitted cannot stretch
    #                 twelve rows over a whole tall panel.
    # Floored at 1 px either way (a row that rounds to zero reads as "this CE
    # does not exist") and never above canvas_h / n_ce, which is what keeps
    # image_h <= canvas_h -- the old 4 px minimum did the opposite and pushed
    # the image past the bottom of a 40 px canvas.
    cap = canvas_h / n_ce if state.fill_height else min(_ROW_MAX_PX,
                                                        canvas_h / n_ce)
    row_h = max(1.0, min(cap, canvas_h / n_ce))
    image_h = row_h * n_ce

    visible = min(float(n_pages), canvas_w / state.zoom)
    pan_x = float(min(max(0.0, state.pan[0]), max(0.0, n_pages - visible)))
    state.pan = (pan_x, state.pan[1])

    # Build the texture from the VISIBLE page window only: one upload of at
    # most 12 x canvas_w texels however far in we are zoomed, and no reliance
    # on uv clamping, which would smear an edge texel into space it does not
    # describe.
    first = int(pan_x)
    count = max(1, min(n_pages - first, int(np.ceil(visible)) + 1))
    draw_w = min(canvas_w, count * state.zoom)
    out_w = max(1, int(round(draw_w)))
    # Reduce + colourise + upload only when the inputs actually changed.  They
    # are a pure function of (stripe array, visible window, output width), all
    # of which are constant on an idle frame, and redoing them every frame cost
    # ~0.5 ms forever.  The signature holds the array object, so a new stripe
    # (request(), apply(), or the placeholder fallback in draw()) invalidates it
    # by identity -- there is no id() to recycle and no invalidation call to
    # forget, which matters because a stale stripe is exactly the kind of lie
    # this view exists to prevent.
    # The vertical magnification is the whole picture here: the texture is
    # exactly n_ce (12) rows tall and it is drawn at image_h, which is ~550 px
    # on a 4K panel -- a 46x stretch. The x axis stretches too whenever one page
    # is drawn wider than a pixel (out_w > count).
    up_y = max(1, min(int(np.ceil(image_h / max(n_ce, 1))), _MAX_UPSCALE))
    up_x = max(1, min(int(np.ceil(out_w / max(count, 1))), _MAX_UPSCALE))
    # The mode is in the signature through the Stripe object: switching mode
    # builds a new Stripe, so the texture is rebuilt by identity exactly as a
    # new probe result rebuilds it. Nothing has to remember to invalidate.
    sig = (stripe, first, count, out_w, up_x, up_y)
    cached = getattr(state, _TEXSIG_ATTR, None)
    fresh = (state.tex is not None and cached is not None
             and cached[0] is stripe and cached[1:] == sig[1:])
    if not fresh:
        sub = _reduce_states(stripe.codes[:, first:first + count], out_w,
                             stripe.ranks, stripe.unranks)
        rgba = np.ascontiguousarray(stripe.colors(sub), dtype=np.uint8)
        # Expand the texels ourselves rather than trusting the sampler.
        #
        # gltex asks for GL_NEAREST and the driver reports GL_NEAREST, and the
        # frame still came back interpolated -- measured on the drive map off
        # the back buffer (so not the compositor) with flat alpha-255 texel data
        # (so not the palette). Whatever sits between imgui.image() and the
        # fragment does not honour the filter on this stack.
        #
        # np.repeat cannot invent a value between two texels; it can only
        # duplicate one. So once the texture is 1:1 with the quad there is
        # nothing left to interpolate between, and the guarantee no longer
        # depends on a driver, a backend or a sampler keeping its word.
        if up_y > 1:
            rgba = np.repeat(rgba, up_y, axis=0)
        if up_x > 1:
            rgba = np.repeat(rgba, up_x, axis=1)
        rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
        state.tex = gltex.upload(state.tex, rgba, nearest=True)
        setattr(state, _TEXSIG_ATTR, sig)
        # The exact array that was uploaded, for app's canvas-purity check:
        # the stripe now paints hashed WERU hues and erase ramps, which no
        # static table can enumerate, so the check compares the back buffer
        # with this -- and a blended overlay still fails it.
        LAST_CANVAS["image"] = rgba

    origin = imgui.get_cursor_screen_pos()
    draw_list = imgui.get_window_draw_list()

    # CE labels in the left gutter: drawn, not widgets, so 12 of them cost no
    # layout. The gutter is measured from the widest label ("CE11"), so the quad
    # starts clear of the text instead of running under it.
    gutter = _gutter_px()
    if row_h >= imgui.get_font_size() + 1.0:
        col = imgui.get_color_u32(imgui.Col_.text_disabled.value)
        for ce in range(n_ce):
            y = origin.y + ce * row_h + (row_h - imgui.get_font_size()) * 0.5
            draw_list.add_text(imgui.ImVec2(origin.x + 2.0, y), col, f"CE{ce}")

    img_x = origin.x + gutter
    imgui.set_cursor_screen_pos(imgui.ImVec2(img_x, origin.y))
    imgui.image(_tex_ref(state.tex), imgui.ImVec2(draw_w, image_h))
    # An invisible button over the same rect, so a left DRAG can pan without
    # costing the left CLICK its selection.  `imgui.image` is not an interactive
    # item, so is_item_active() -- the only thing that separates "this view is
    # being dragged" from "a button is down somewhere on screen" -- is not
    # available until a real item covers the image.  The map has always had this;
    # the stripe did not, which is why it panned only from the middle button.
    imgui.set_cursor_screen_pos(imgui.ImVec2(img_x, origin.y))
    imgui.invisible_button("stripe",
                           imgui.ImVec2(max(1.0, draw_w), max(1.0, image_h)),
                           imgui.ButtonFlags_.mouse_button_left
                           | imgui.ButtonFlags_.mouse_button_middle)
    hovered_img = imgui.is_item_hovered()
    active_img = imgui.is_item_active()
    LAST_CANVAS.update(rect=(float(img_x), float(origin.y),
                             float(draw_w), float(image_h)))

    # CE separators, only when a row is thick enough that a hairline is not
    # itself most of a cell.
    if row_h >= 6.0:
        sep = SEP_COL
        for ce in range(1, n_ce):
            # A 1 px quad on an integer row, not add_line() on a half pixel.
            #
            # The previous version snapped to floor()+0.5 -- a pixel centre --
            # and its comment claimed that produced one hard row. Measured in
            # this build it produces exactly the two-row wash it was meant to
            # cure: imgui takes the textured-AA line path (anti_aliased_lines
            # and ..._use_tex both on), where full coverage lands on an integer
            # coordinate, so a centred hairline puts half its alpha in each
            # neighbouring row. MEASURED at 1800x1000 (row_h 27.8 px): rows
            # 436-437, 464-465, 492-493 ... came back (38,37,47) against cells
            # of exact palette ABSENT (14,12,20). add_rect_filled with rounding
            # 0 is a plain quad with no AA fringe. The texture is GL_NEAREST
            # for exactly this reason; the overlay must not undo it.
            #
            # That fixed the GEOMETRY and left the colour: this call used to
            # take imgui's translucent border style, so the one hard row it
            # drew was still a 50% blend of whatever cell was under it -- see
            # SEP_COL at the top of this file for the measured numbers. One row
            # is necessary; one opaque row is the claim.
            y0 = math.floor(origin.y + ce * row_h)
            draw_list.add_rect_filled(imgui.ImVec2(img_x, y0),
                                      imgui.ImVec2(img_x + draw_w, y0 + 1.0),
                                      sep)

    io = imgui.get_io()
    # Computed before the click test below: a drag must not also select, or
    # panning would re-open whichever page happened to sit under the press.
    dragging = bool(active_img
                    and imgui.is_mouse_dragging(imgui.MouseButton_.left))
    if hovered_img:
        mx, my = float(io.mouse_pos.x), float(io.mouse_pos.y)
        # One mapping for both regimes: draw_w pixels span `count` pages,
        # whether or not the columns were precedence-reduced.
        page = first + int((mx - img_x) * count / max(1.0, draw_w))
        ce = int((my - origin.y) / row_h)
        if 0 <= ce < n_ce and first <= page < min(first + count, n_pages):
            hover = (ce, page)
            state.hover = hover
            _tooltip(_hover_text(state, summary, stripe, ce, page,
                                 int(states[ce, page])))
            if imgui.is_mouse_clicked(0) and not dragging:
                clicked = (ce, page)
                state.selected = clicked
        else:
            state.hover = None

        wheel = float(io.mouse_wheel)
        wheel_h = float(io.mouse_wheel_h)
        if wheel and io.key_ctrl:
            # Zoom about the cursor: the page under the pointer stays put.
            anchor = first + (mx - img_x) * count / max(1.0, draw_w)
            state.zoom = float(min(_ZOOM_MAX, max(
                _ZOOM_MIN, state.zoom * (_ZOOM_STEP ** wheel))))
            new_visible = min(float(n_pages), canvas_w / state.zoom)
            new_pan = anchor - (mx - img_x) / state.zoom
            state.pan = (float(min(max(0.0, new_pan),
                                   max(0.0, n_pages - new_visible))),
                         state.pan[1])
        elif wheel or wheel_h:
            # Unmodified wheel / two-finger swipe pans; ctrl+wheel zooms.  See
            # the matching block in mapview for why the gestures split that way.
            #
            # The stripe has ONE axis, so BOTH wheel axes drive it: a vertical
            # swipe over a view that can only scroll sideways should move it
            # rather than do nothing, which is what a reader expects from a
            # strip of 1104 pages several screens wide.
            new_pan = pan_x - (wheel_h + wheel) * _WHEEL_PAN_PX / state.zoom
            state.pan = (float(min(max(0.0, new_pan),
                                   max(0.0, n_pages - visible))),
                         state.pan[1])
    elif not imgui.is_window_hovered():
        state.hover = None

    # Middle-drag pans, and so does left-drag.  Left was once a pure select on
    # the reasoning that "a click never slides the stripe out from under the page
    # the user meant to open" -- which the drag THRESHOLD already guarantees, and
    # which cost the stripe the click-and-drag panning the map has always had.
    # The select above is skipped while a drag is in flight, so the two never
    # fight over the same button.
    middle = imgui.MouseButton_.middle.value
    if imgui.is_window_hovered() and imgui.is_mouse_dragging(middle):
        delta = imgui.get_mouse_drag_delta(middle)
        if delta.x:
            new_pan = pan_x - float(delta.x) / state.zoom
            state.pan = (float(min(max(0.0, new_pan),
                                   max(0.0, n_pages - visible))),
                         state.pan[1])
            imgui.reset_mouse_drag_delta(middle)

    if dragging:
        # io.mouse_delta, not get_mouse_drag_delta: the delta is consumed every
        # frame, which is what the map does, so there is nothing to reset and no
        # accumulated offset to lose if a frame is dropped.
        dx = float(io.mouse_delta.x)
        if dx:
            new_pan = pan_x - dx / state.zoom
            state.pan = (float(min(max(0.0, new_pan),
                                   max(0.0, n_pages - visible))),
                         state.pan[1])

    if state.selected is not None:
        sce, spage = state.selected
        if 0 <= sce < n_ce and first <= spage < first + count:
            cell_w = draw_w / float(count)
            x0 = img_x + (spage - first) * cell_w
            y0 = origin.y + sce * row_h
            # stroke_rect, not add_rect: four opaque quads snapped to whole
            # pixels. See SELECT_COL at the top of this file -- the old call was
            # alpha 230 down the AA polyline path from two fractional
            # coordinates, and exactly one pixel of it ever reached white.
            stroke_rect(draw_list, imgui.ImVec2(x0 - 1.0, y0),
                        imgui.ImVec2(x0 + max(1.0, cell_w) + 1.0, y0 + row_h),
                        SELECT_COL, 2.0)

    # Below one pixel per page the picture is a precedence reduction, not a
    # per-page rendering.  Say so, rather than let the reader assume purity --
    # the exact counts above the stripe remain the claim.
    #
    # TRIMMED to the canvas width, with the whole sentence on hover. Unwrapped
    # it is ~490 px of text under a stripe that may be 342 px wide, and imgui
    # counts that overhang as content: MEASURED, this one line was the entire
    # horizontal scroll extent of the canvas child -- 148 px at 1280x800 and
    # 379 px at 1024x700 -- so the view that had just been fitted to the panel
    # reported itself as wider than the panel. Wrapping it instead would take a
    # second and third line out of the CE rows, which is the axis this pass is
    # protecting.
    if state.zoom < 1.0:
        imgui.set_cursor_screen_pos(
            imgui.ImVec2(img_x, origin.y + image_h + 2.0))
        note = (f"{1.0 / state.zoom:.1f} pages/px -- the rarest state wins "
                f"each pixel; the counts above are exact for what the index "
                f"records")
        _colored(imgui.ImVec4(0.6, 0.6, 0.6, 1.0), one_line(note, canvas_w))
        if imgui.is_item_hovered():
            _tooltip(note)

    # Recorded on the way out: if this child ever scrolls, a CE row is hidden
    # behind a scrollbar and nothing else in the app can tell.
    setattr(state, _OVERFLOW_ATTR, float(imgui.get_scroll_max_y()))

    return hover, clicked
