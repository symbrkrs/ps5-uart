"""Whole-drive map: x = block 0..3999, y = CE 0..11, as one textured quad.

The map is 48000 cells. It is drawn as a single texture (uploaded only when
`state.dirty`) plus a handful of imgui widgets, never one widget per cell.

Palette discipline lives here as much as in palette.py:

* gltex.upload(..., nearest=True) is the only sampling we allow. Two adjacent
  NAND cells must never be blended -- an interpolated pixel between PROGRAMMED
  and ABSENT is a colour that means nothing, and "merged states" is exactly the
  mistake this project has already made twice.
* But nearest sampling of a minified texture drops cells, and at the default
  view 4000 block columns share ~1200 pixels, so GL would keep fewer than one
  in three and a lone SKIPPED block would appear about 30% of the time. So the
  x axis is reduced in numpy before the upload (see _reduce_columns): each
  output column is the real colour of the highest-precedence block in its bin,
  rare-and-dangerous first, exactly as blockview does for pages. Nothing is
  blended and nothing is invented -- the choice is only which block's colour
  survives, which is why it is correct in the hashed WERU/owner modes too.
* The quad is clipped to the real content extent and the uv window never leaves
  [0, 1]. With clamp-to-edge, letting uv run past 1 would smear the last block
  column across empty panel space -- painting media where no record exists.
* Every CE row is guaranteed at least one pixel of height (see _scales), so the
  dead CE0 ABSENT row can never silently vanish from the picture.
* When one pixel has to stand for several block columns the footer says so, in
  numbers, and says that the reduction is rarest-first. A map that quietly
  drops 6 of every 7 blocks while looking complete is the authoritative-looking
  lie we are trying not to build.
* The mixed-block marker degrades but never disappears: per-block ticks while
  they are legible and few, then a dot on the pure blocks (there are far fewer
  of those on this drive), then a hatch. Drawing nothing above a cap -- which
  is what this file used to do, at exactly the zoom the marker exists for --
  leaves a checkbox reading "on" over a map that is silently unmarked.

UI thread only. No store access, no page_ref, no read_page: every answer here
comes from the in-memory summary arrays.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
from imgui_bundle import imgui

from . import N_BLOCK, N_CE
from . import gltex, model, palette
from .model import PageState, Summary

# ---- tunables -----------------------------------------------------------

MIN_ZOOM = 0.02          # screen px per block column
MAX_ZOOM = 64.0
ZOOM_STEP = 1.15         # per wheel notch (ctrl+wheel; see the wheel block)

#: Screen pixels panned per wheel unit for an unmodified wheel / two-finger
#: swipe.  In PIXELS, converted to texels by the live scale, so the gesture
#: moves the same distance on screen at every zoom -- panning in texels would
#: crawl at 0.02 px per block and fly at 64.
WHEEL_PAN_PX = 90.0
FIT_MARGIN = 1.0         # px shaved off the fit width so the border is visible

GRID_MIN_PX = 5.0        # draw CE separators only once a row is this tall
VGRID_MIN_PX = 8.0       # ... and block separators once a column is this wide
MAX_GRID_LINES = 512     # vertical separators are capped; they are decoration

# A CE row never needs more than this many pixels. There are twelve of them
# and they carry no detail of their own -- all the structure is along x -- so
# letting them divide the whole panel height between them (which is what this
# file used to do) made a 12-row picture 350 px tall and pushed the block
# stripe and its counts into a sliver. 12 px still leaves every row clickable
# and above GRID_MIN_PX, so the CE separators stay visible.
#: A CE row never needs more than this many pixels -- but it must be at least
#: tall enough to hold its own "CE n" label, or the map is twelve anonymous
#: stripes. 12 px was chosen when the rows carried no label; the gutter now
#: collapses below the font height, so a hard 12 meant the labels could never
#: appear at any normal window size. Resolved at first use against the live
#: font (15 px here -> 16), and still capped so the twelve rows cannot divide a
#: whole tall panel between them, which is what made the map dominate the
#: window before.
_MAX_CE_PX_FLOOR = 12.0


def max_ce_px() -> float:
    """Row-height cap: the label height when we can afford it, else the floor."""
    try:
        return max(_MAX_CE_PX_FLOOR, imgui.get_font_size() + 1.0)
    except Exception:                                        # noqa: BLE001
        return _MAX_CE_PX_FLOOR


MAX_CE_PX = _MAX_CE_PX_FLOOR      # module-level default; callers use max_ce_px()

#: Left strip for the map's "CE n" labels, measured from the widest one drawn.
#: The map had no row labels at all: twelve identical stripes with nothing to
#: say which die each was, so a feature spotted on one row could not be named
#: without counting rows by eye. Same gutter treatment as the block stripe, and
#: measured from the font for the same reason -- "CE11" is wider than "CE1".
CE_GUTTER_MIN_PX = 34.0
#: A row shorter than this cannot hold a legible label, so the gutter collapses
#: rather than drawing text that overlaps its neighbours. The map's rows are
#: capped at MAX_CE_PX, which is small (the user asked for less
#: vertical space per CE), so the labels appear once a fit or a zoom gives the
#: rows room -- and the footer says they were dropped when they are not there.
CE_LABEL_MIN_PX = 11.0   # replaced at runtime by the real font height


def ce_gutter_px(row_h: float) -> float:
    """Gutter width for the CE labels at this row height.  0 when unlabelled."""
    # the font decides, not a constant. The hexdump work moved the UI font
    # to 15 px and an 11 px threshold then let labels draw into rows too
    # short to hold them, so twelve of them piled on top of each other.
    try:
        need = imgui.get_font_size() + 1.0
    except Exception:                                        # noqa: BLE001
        need = CE_LABEL_MIN_PX
    if row_h < max(need, CE_LABEL_MIN_PX):
        return 0.0
    try:
        w = imgui.calc_text_size(f"CE{N_CE - 1}").x
    except Exception:                                        # noqa: BLE001
        return CE_GUTTER_MIN_PX
    return max(CE_GUTTER_MIN_PX, float(w) + 8.0)

#: Cap on the numpy magnification factor (see the upload site). Bounds the
#: expanded image: 64 x the visible block count is well under a 4K canvas, and
#: at that point one block is already 64 px wide.
#:
#: Public because blockview magnifies for the same reason and must not drift to
#: a second value -- the two views would then disagree about how far a cell may
#: be stretched before the sampler is allowed back into the picture.
MAX_UPSCALE = 64
_MAX_UPSCALE = MAX_UPSCALE          # old private name, kept for this module

STIPPLE_MIN_PX = 4.0     # a mixed-block tick needs this many px to be legible
# Above this many marks the per-cell loop costs more than a frame is worth
# (~1.3 us per imgui draw-list call), so the marker degrades to an aggregate --
# it never disappears. On this drive 82% of blocks are mixed, so the aggregate
# path is the normal one at low zoom, not an edge case.
MAX_STIPPLE_MARKS = 1200
HATCH_SPACING_PX = 9.0

# ---- overlay colours ----------------------------------------------------
# Achromatic. Overlays must never be mistaken for a state colour,
# so they carry no hue that palette.STATE_RGBA uses.


def _col(r: int, g: int, b: int, a: int = 255) -> int:
    """RGBA ints -> ImU32 (imgui packs as 0xAABBGGRR)."""
    return (a << 24) | (b << 16) | (g << 8) | r


# ANYTHING DRAWN ON TOP OF A CELL IS OPAQUE, BECAUSE A BLENDED OVERLAY DOES
# Not tint a CELL, it invents a colour that is in no legend.
#
# That is the rule, and it has three routes into this file, all three measured
# on the framebuffer rather than reasoned about:
#
#  1. an alpha < 255 constant.  GRID_COL was white at alpha 40 and turned
#     skipped-orange (255,148,24) into (255,165,60) on 9,881 pixels.  PURE_COL
#     was alpha 220 and every pixel of every pure-block dot was a blend --
#     6,130 of them, in five different invented colours, not one opaque.
#     HATCH_COL was alpha 46 and laid 44,636 stray pixels in 30 invented
#     colours over 33% of the canvas.
#  2. the anti-aliased line PATH.  add_line()/add_rect() with a thickness go
#     through AddPolyline, which fades the edge of the stroke by coverage --
#     so an opaque constant still lands as a blend.  Measured: the selection
#     ring (a=255, integer coords, thickness 2) painted an exact 50% fringe and
#     only 18 pixels of it ever reached white; the mixed tick, a 1 px diagonal,
#     put 2/3 of its ink (8,100 px) into a 29%-white fringe.  add_rect_filled
#     with rounding 0 is a plain quad and has no fringe at all, so every stroke
#     here is drawn as quads (see stroke_rect) and the two genuinely diagonal
#     marks are drawn with the AA line path switched off (see _no_aa_lines).
#  3. a fractional coordinate.  A 1 px line at x.5 splits its alpha across two
#     columns, which is defect 2 by another route: at fit zoom _cell_rect
#     returns x = 407.84 and the opaque hover box came back as 16% and 84%
#     white over SKIPPED.  Every stroke is snapped to whole pixels first.
#
# A 1 px opaque line hides the cell under it, which is honest -- a blended one
# shows a cell that is not there.  app.py's --self-test reads both canvases back
# out of the framebuffer and fails on any pixel that is neither a palette colour
# nor one of the OVERLAY_COLORS declared below, so none of the three routes can
# reopen quietly.
GRID_COL = _col(62, 61, 74, 255)
BORDER_COL = _col(160, 160, 170, 255)
HOVER_COL = _col(255, 255, 255, 255)
SELECT_OUTER = _col(255, 255, 255, 255)
SELECT_INNER = _col(0, 0, 0, 255)
#: palette.STIPPLE_RGBA's hue at full alpha -- that entry is the legend key and
#: still carries its own alpha, which is fine: the legend swatch is drawn in
#: panel space, not over a cell.
STIPPLE_COL = _col(255, 255, 255, 255)
PURE_COL = _col(20, 20, 24, 255)         # the inverse marker, when mixed is dense
HATCH_COL = _col(255, 255, 255, 255)
EMPTY_COL = _col(24, 24, 28, 255)     # panel space beyond the grid

#: Every colour this module is allowed to put on the canvas that is not a
#: palette colour, as rgb.  app._self_test's canvas-purity check reads this;
#: adding an overlay without adding it here fails the build, which is the point.
OVERLAY_COLORS: tuple[tuple[int, int, int], ...] = (
    (62, 61, 74),        # GRID_COL
    (160, 160, 170),     # BORDER_COL
    (255, 255, 255),     # HOVER_COL / SELECT_OUTER / STIPPLE_COL / HATCH_COL
    (0, 0, 0),           # SELECT_INNER
    (20, 20, 24),        # PURE_COL
    (24, 24, 28),        # EMPTY_COL
)


#: Where the map quad landed last frame, in imgui screen pixels, plus the exact
#: image that was uploaded for it: (x, y, w, h) and a uint8[.., .., 4] array.
#: Written by draw(), read only by app._self_test -- the purity check has to
#: know both where the cells are and which palette colours this mode paints,
#: and neither is derivable from outside this function.
LAST_CANVAS: dict[str, object] = {}


class _no_aa_lines:
    """Draw with imgui's anti-aliased line path switched off, then restore it.

    For the two marks that are genuinely diagonal (the mixed tick and the
    aggregate hatch) a quad is not an option, and an AA'd 1 px diagonal spends
    most of its ink on a coverage fringe: MEASURED, the tick laid 8,100 fringe
    pixels against 4,080 opaque ones, in colours like (255,179,92) that are in
    no legend.  With the flag cleared, imgui emits a plain unfeathered quad per
    segment and the same tick comes back as 4,164 pixels of exact white and
    nothing else.

    The flag lives on the draw list and imgui's own widgets share it, so it is
    always put back -- including on the exception path.
    """

    __slots__ = ("_dl", "_saved")

    _OFF = ~(int(imgui.ImDrawListFlags_.anti_aliased_lines)
             | int(imgui.ImDrawListFlags_.anti_aliased_lines_use_tex))

    def __init__(self, dl):
        self._dl = dl
        self._saved = 0

    def __enter__(self):
        self._saved = int(self._dl.flags)
        self._dl.flags = self._saved & self._OFF
        return self._dl

    def __exit__(self, *_exc) -> bool:
        self._dl.flags = self._saved
        return False


def _snap(v: float) -> float:
    """A screen coordinate on a whole pixel boundary."""
    return float(math.floor(float(v) + 0.5))


def stroke_rect(dl, p0: imgui.ImVec2, p1: imgui.ImVec2, col: int,
                thickness: float = 1.0) -> None:
    """A rectangle outline as four opaque quads on whole-pixel boundaries.

    The replacement for add_rect(), which takes the AA polyline path: see rule 2
    above.  Public because blockview draws its selection ring the same way and
    the two views must not drift apart on this.

    Degenerate case: a ring needs 2*thickness in each axis to have an inside,
    and _cell_rect can hand us a 3 px cell at fit zoom.  Filling it is still
    opaque and still hard-edged -- it hides the cell, which the reader can see,
    rather than tinting it, which they cannot.
    """
    x0, y0 = _snap(min(p0.x, p1.x)), _snap(min(p0.y, p1.y))
    x1, y1 = _snap(max(p0.x, p1.x)), _snap(max(p0.y, p1.y))
    x1 = max(x1, x0 + 1.0)
    y1 = max(y1, y0 + 1.0)
    t = max(1.0, float(math.floor(float(thickness))))
    v = imgui.ImVec2
    if (x1 - x0) < 2.0 * t or (y1 - y0) < 2.0 * t:
        dl.add_rect_filled(v(x0, y0), v(x1, y1), col)
        return
    dl.add_rect_filled(v(x0, y0), v(x1, y0 + t), col)            # top
    dl.add_rect_filled(v(x0, y1 - t), v(x1, y1), col)            # bottom
    dl.add_rect_filled(v(x0, y0 + t), v(x0 + t, y1 - t), col)    # left
    dl.add_rect_filled(v(x1 - t, y0 + t), v(x1, y1 - t), col)    # right


# ---- state --------------------------------------------------------------


@dataclasses.dataclass
class MapState:
    zoom: float = 1.0                       # screen px per block column
    pan: tuple[float, float] = (0.0, 0.0)   # texel coords at the top-left px
    selected: tuple[int, int] | None = None  # (ce, block)
    hover: tuple[int, int] | None = None
    tex: gltex.Texture | None = None
    dirty: bool = True                      # set when mode or summary changed
    show_grid: bool = True                  # CE separators at high zoom
    #: Give the twelve CE rows the whole canvas height instead of stopping at
    #: MAX_CE_PX. Set by fit(), which is a two-axis fit: the grid has to land
    #: inside the canvas on x and on y, and a picture that stops 24 px short of
    #: the panel it was told to fit is not fitted, it is merely un-clipped.
    #: It does not lock the zoom -- x stays state.zoom and the wheel, the slider
    #: and focus() all keep working afterwards; only the row height follows the
    #: panel.
    fill_height: bool = False
    stipple_mixed: bool = True
    #: Paint the aggregate mixed marker (the diagonal hatch) over the map.
    #: Default off, and that is a correctness decision, not a taste one.
    #:
    #: It used to be a correctness decision about blending: the hatch was a
    #: translucent (alpha 46) anti-aliased diagonal every HATCH_SPACING_PX, and
    #: at the fit zoom -- where it is the only marker that fits, i.e. always --
    #: it laid a periodic wash over every cell. Measured on the fit view, a run
    #: of PROGRAMMED blocks came back 255,148,24 / 255,153,36 / 255,167,66 /
    #: 255,153,36 on a 9 px period: three colours that are not in the palette,
    #: on media that is one state, 44,636 stray pixels in 30 invented colours
    #: over a third of the canvas.
    #:
    #: The hatch is now opaque and unfeathered, so that specific lie is gone.
    #: It stays off by default for the remaining, smaller reason: at 9 px
    #: spacing it hides one pixel column in nine, and the map is already
    #: reducing 3.5 blocks into each column at that zoom. Hiding is honest
    #: where tinting was not, but it is still a cost, and the count the hatch
    #: stands for is printed under the map whether it is drawn or not.
    #:
    #: Nothing is lost by leaving it off. _draw_stipple still counts the mixed
    #: blocks and still returns the sentence, and draw() still prints it under
    #: the map -- so the marker degrades from a wash to words, never to silence,
    #: and the checkbox below turns the wash back on for anyone who wants it.
    hatch_dense: bool = False


@dataclasses.dataclass(frozen=True)
class MapEvent:
    hover: tuple[int, int] | None = None
    clicked: tuple[int, int] | None = None
    double_clicked: bool = False
    context: tuple[int, int] | None = None  # right-click -> 'open in block view'


# ---- per-state view geometry, remembered between calls ------------------
# focus() and cell_at() need the pixel size of the region and the resolved
# y-scale, neither of which is in their signature. draw() records them; the
# fallback below only matters before the first frame.

_DEFAULT_VIEW = (1200.0, 240.0, 0.3, 20.0)   # view_w, view_h, sx, sy
# The owner object is kept in the value so its id() cannot be recycled onto a
# different object while we still hold the entry.
_VIEW: dict[int, tuple[object, tuple[float, float, float, float]]] = {}

# palette.mixed_mask is pure and the summary is frozen, so cache the reshaped
# mask instead of recomputing 48000 rows every frame.
_MIXED: dict[int, tuple[object, np.ndarray]] = {}
# ... and the same for the per-block precedence ranks used by _reduce_columns.
_RANKS: dict[int, tuple[object, np.ndarray]] = {}

# Visibility precedence when several block columns share one screen pixel.
# Identical to blockview._RANK: rare and dangerous beats common, so a skipped
# block cannot hide behind its programmed neighbours and a hole cannot hide
# inside erased media.
_RANK = np.zeros(len(PageState), dtype=np.uint8)
_RANK[PageState.ERASED] = 0
_RANK[PageState.UNKNOWN] = 1
_RANK[PageState.PROGRAMMED] = 2
_RANK[PageState.ZERO] = 3
_RANK[PageState.ABSENT] = 4
_RANK[PageState.SKIPPED] = 5

# Private per-state caches.  MapState's field list is frozen by the contract,
# so these hang off the state object under reserved names and hold the source
# array object, which makes a stale entry impossible: a recoloured map is a new
# array.
_TEXKEY_ATTR = "_mapview_texkey"
_REDUCED_ATTR = "_mapview_reduced"
_STIPPLE_ATTR = "_mapview_stipple"


def _remember(state: MapState, view_w: float, view_h: float,
              sx: float, sy: float) -> None:
    if len(_VIEW) > 8:          # only ever one or two maps; do not leak
        _VIEW.clear()
    _VIEW[id(state)] = (state, (view_w, view_h, sx, sy))


def _view_of(state: MapState) -> tuple[float, float, float, float]:
    entry = _VIEW.get(id(state))
    if entry is None or entry[0] is not state:
        return _DEFAULT_VIEW
    return entry[1]


def _mixed_grid(summary: Summary | None) -> np.ndarray | None:
    if summary is None:
        return None
    entry = _MIXED.get(id(summary))
    if entry is not None and entry[0] is summary:
        return entry[1]
    if len(_MIXED) > 4:
        _MIXED.clear()
    grid = np.asarray(palette.mixed_mask(summary), dtype=bool).reshape(
        N_CE, N_BLOCK)
    _MIXED[id(summary)] = (summary, grid)
    return grid


def _rank_grid(summary: Summary | None) -> np.ndarray | None:
    """uint8[12, 4000] of visibility rank, one per block."""
    if summary is None:
        return None
    entry = _RANKS.get(id(summary))
    if entry is not None and entry[0] is summary:
        return entry[1]
    if len(_RANKS) > 4:
        _RANKS.clear()
    grid = _RANK[palette.block_states(summary)].reshape(N_CE, N_BLOCK)
    _RANKS[id(summary)] = (summary, grid)
    return grid


def _reduce_columns(rgba: np.ndarray, ranks: np.ndarray, b0: int, n: int,
                    out_w: int) -> np.ndarray:
    """-> uint8[12, out_w, 4]: `n` block columns from b0, reduced to out_w.

    Each output column is the colour of one real block: the highest-precedence
    block in that bin (ties go to the leftmost).  No colour is averaged and no
    colour is invented, so this is as valid in the hashed WERU/owner modes as
    in CONTENT -- what precedence decides is only which block gets to speak for
    the pixel, and a skipped or absent block always wins that argument.

    Handing the full 4000-wide texture to GL instead, as this view used to,
    lets GL_NEAREST pick one column in every 3.3 by position: ~2800 columns are
    never sampled at the default zoom, and which third survives shifts with the
    panel width.

    Why this cannot reintroduce smearing at sub-pixel-per-block zoom (the
    default view is sx = 0.29 px/block, i.e. ~3.5 blocks per pixel column):

      * The reduction is a select, never an average.  `best` is an argmax over
        a rank-major key and `pick` is a real column index, so every output
        texel is a verbatim copy of one input block's RGBA.  There is no
        arithmetic on colour bytes anywhere in this function -- a mean of
        SKIPPED and ERASED would be precisely the third colour gltex refuses to
        let GL invent, and it is just as forbidden in numpy.
      * out_w is the drawn quad width in whole pixels (see draw(): out_w =
        round(quad_w), and the reduced image is then drawn at quad_w with uv
        0..1).  So the texture is 1:1 with the destination on the x axis and
        GL_NEAREST is the identity -- there is no resampling left for the
        driver to do, blurry or otherwise.  On y the 12-row texture is
        magnified to quad_h, which under GL_NEAREST is exact pixel replication:
        every CE row is a solid band with a step at its boundary.
      * The cost of that honesty is aliasing, not blending: a block that loses
        its bin is absent from the picture, never faintly mixed into it.  That
        is what the footer line under the map reports in numbers, and why the
        precedence order puts SKIPPED/ABSENT on top -- the states you must not
        miss are the ones that always win their pixel.
    """
    n = int(n)
    out_w = int(out_w)
    if out_w >= n or out_w < 1 or n < 1:
        return np.ascontiguousarray(rgba[:, b0:b0 + max(n, 1)])
    starts = (np.arange(out_w, dtype=np.int64) * n) // out_w
    sub = ranks[:, b0:b0 + n].astype(np.int64)
    cols = np.arange(n, dtype=np.int64)[None, :]
    # rank-major key with the column index inverted, so max() gives the highest
    # rank and, among equals, the leftmost column.
    key = sub * n + (n - 1 - cols)
    best = np.maximum.reduceat(key, starts, axis=1)
    pick = (n - 1) - (best % n)
    rows = np.arange(rgba.shape[0])[:, None]
    return np.ascontiguousarray(rgba[rows, b0 + pick])


def _reduced_image(state: MapState, rgba: np.ndarray, summary: Summary | None,
                   b0: int, n: int, out_w: int,
                   ranks: np.ndarray | None = None) -> np.ndarray | None:
    """_reduce_columns, cached against the source array object.

    `ranks` lets the caller say which block gets to speak for a pixel column.
    It exists because "rarest first" is a property of what is being shown: in
    the ECC mode the rank that must win is the worst ECC outcome, not the
    rarest page state, or one uncorrectable block among three clean ones is
    dropped from the picture at any zoom under 1 px per block -- exactly the
    kind of hiding this viewer exists to prevent.
    """
    if ranks is None:
        ranks = _rank_grid(summary)
    if ranks is None or ranks.shape[0] != rgba.shape[0]:
        return None
    key = (b0, n, out_w)
    cached = getattr(state, _REDUCED_ATTR, None)
    if (cached is not None and cached[0] is rgba and cached[1] == key
            and cached[3] is ranks):
        return cached[2]
    img = _reduce_columns(rgba, ranks, b0, n, out_w)
    setattr(state, _REDUCED_ATTR, (rgba, key, img, ranks))
    return img


# ---- scales -------------------------------------------------------------


def _scales(state: MapState, view_h: float) -> tuple[float, float]:
    """(px per block column, px per CE row).

    `zoom` is the x scale by contract. The map is 4000 x 12, so a shared scale
    is useless: at fit the columns are sub-pixel while the 12 rows would be
    invisible. The rows therefore grow with the region.

    How far they grow is state.fill_height, which fit() sets:

      * fill_height (after a Fit): the twelve rows divide the whole canvas, so
        N_CE * sy == view_h exactly and the picture lands on both axes. That is
        what "Fit" was asked for -- fitting only the width left the map either
        short of the panel or taller than it, depending on the panel.
      * otherwise: capped at MAX_CE_PX, the old behaviour, so a view that was
        never fitted cannot stretch twelve rows over a whole tall panel.

    sy is floored at 1.0 px so nearest sampling can never drop a CE row --
    a missing row would read as "this CE does not exist", which is a claim,
    not a rendering artefact. It is also never below sx: once a column is wider
    than a row the cells go square, so a zoomed-in cell stays a square cell.
    That floor and that square rule are also the only two ways a fitted map can
    end up taller than its canvas, which is why fit() bounds the zoom by
    view_h / N_CE as well as by the width.
    """
    sx = float(min(max(state.zoom, MIN_ZOOM), MAX_ZOOM))
    room = view_h / N_CE
    sy = max(1.0, room if state.fill_height else min(room, max_ce_px()), sx)
    return sx, sy


def _clamp_pan(pan: tuple[float, float], view_w: float, view_h: float,
               sx: float, sy: float) -> tuple[float, float]:
    px, py = float(pan[0]), float(pan[1])
    max_x = N_BLOCK - view_w / sx
    max_y = N_CE - view_h / sy
    px = 0.0 if max_x <= 0.0 else min(max(px, 0.0), max_x)
    py = 0.0 if max_y <= 0.0 else min(max(py, 0.0), max_y)
    return (px, py)


# ---- public geometry helpers -------------------------------------------


def fit(state: MapState, avail_w: float, avail_h: float) -> None:
    """Zoom/pan so all 4000 x 12 fit the region on both axes.

    Two axes, two bounds, and the smaller wins:

      x: 4000 block columns in (avail_w - FIT_MARGIN) px.
      y: fill_height makes the twelve rows divide avail_h, so the y bound is
         not on sy but on the zoom -- _scales keeps sy >= sx (square cells once
         a column is wider than a row), so a zoom above avail_h / N_CE drags sy
         up with it and pushes CE11 out of the bottom of the canvas.

    On this geometry the width always binds (4000 columns against 12 rows), but
    the y term is what makes "fit" mean fit rather than "fit if x happens to be
    the tighter axis".  fit() is a one-shot: it sets the scale and returns, and
    the wheel, the zoom slider and focus() all still move afterwards.
    """
    usable_w = max(avail_w - FIT_MARGIN, 1.0)
    usable_h = max(avail_h, 1.0)
    state.fill_height = True
    state.zoom = min(max(min(usable_w / N_BLOCK, usable_h / N_CE), MIN_ZOOM),
                     MAX_ZOOM)
    state.pan = (0.0, 0.0)
    view_w, view_h = max(avail_w, 1.0), max(avail_h, 1.0)
    sx, sy = _scales(state, view_h)
    _remember(state, view_w, view_h, sx, sy)


def focus(state: MapState, ce: int, block: int,
          zoom: float | None = None) -> None:
    """Centre on a cell; used by 'jump to weru' and by the address box."""
    if zoom is not None:
        state.zoom = min(max(float(zoom), MIN_ZOOM), MAX_ZOOM)
    view_w, view_h, _, _ = _view_of(state)
    sx, sy = _scales(state, view_h)
    pan = (block + 0.5 - view_w / (2.0 * sx),
           ce + 0.5 - view_h / (2.0 * sy))
    state.pan = _clamp_pan(pan, view_w, view_h, sx, sy)
    _remember(state, view_w, view_h, sx, sy)


def cell_at(state: MapState, screen_x: float, screen_y: float,
            origin: tuple[float, float]) -> tuple[int, int] | None:
    """Screen px -> (ce, block), or None outside the 12 x 4000 grid."""
    view_w, view_h, sx, sy = _view_of(state)
    sx, sy = _scales(state, view_h)          # zoom may have changed since draw
    if sx <= 0.0 or sy <= 0.0:
        return None
    tx = state.pan[0] + (screen_x - origin[0]) / sx
    ty = state.pan[1] + (screen_y - origin[1]) / sy
    block = int(math.floor(tx))
    ce = int(math.floor(ty))
    if 0 <= block < N_BLOCK and 0 <= ce < N_CE:
        return (ce, block)
    return None


# ---- drawing ------------------------------------------------------------


def _tex_ref(tex: gltex.Texture):
    """gltex hands back whatever imgui.image() wants; imgui 1.92 wants an
    ImTextureRef, so wrap a bare GLuint if that is what we got."""
    ident = tex.imgui_id()
    if isinstance(ident, int):
        return imgui.ImTextureRef(ident)
    return ident


def _draw_grid(dl, origin: tuple[float, float], quad_w: float, quad_h: float,
               pan: tuple[float, float], sx: float, sy: float) -> None:
    # Separators are 1 px quads on integer coordinates, not add_line() calls.
    #
    # This is the second attempt and the first one was measured wrong. The old
    # code snapped each line to floor()+0.5 -- a pixel centre -- on the theory
    # that a centred hairline lands on one row. In this build it does the
    # opposite: style.anti_aliased_lines is on and imgui takes the textured-AA
    # path, where full coverage falls on an integer coordinate and a centred
    # line splits half its alpha into each neighbouring row. MEASURED at
    # 1800x1000 over PROGRAMMED cells (255,148,24): rows 117-118, 127-128,
    # 137-138 ... all came back (255,156,42), i.e. 2 of every 10 vertical
    # pixels painted a colour that is in no palette -- the exact third colour
    # gltex.upload(nearest=True) exists to forbid, reintroduced by the overlay
    # drawn on top of it.
    #
    # add_rect_filled with rounding 0 emits a plain quad and has no AA fringe,
    # so a separator is one hard row and the cells on either side keep their
    # exact palette bytes. GRID_COL is opaque too, so that row is GRID_COL --
    # MEASURED: 12,387 pixels of exact (62,61,74) and no fringe colour at all.
    # An earlier version of this comment said GRID_COL was "deliberately
    # translucent, so the separator row itself is a blend"; that was true and it
    # was the defect. A row that hides a cell is honest; a row that tints one is
    # a colour in no legend.
    ox, oy = origin
    if sy >= GRID_MIN_PX:
        first = int(math.floor(pan[1])) + 1
        last = int(math.ceil(pan[1] + quad_h / sy))
        for ce in range(max(first, 1), min(last, N_CE)):
            y = math.floor(oy + (ce - pan[1]) * sy)
            dl.add_rect_filled(imgui.ImVec2(ox, y),
                               imgui.ImVec2(ox + quad_w, y + 1.0), GRID_COL)
    if sx >= VGRID_MIN_PX:
        first = int(math.floor(pan[0])) + 1
        last = int(math.ceil(pan[0] + quad_w / sx))
        first = max(first, 1)
        last = min(last, N_BLOCK)
        if last - first <= MAX_GRID_LINES:
            for b in range(first, last):
                x = math.floor(ox + (b - pan[0]) * sx)
                dl.add_rect_filled(imgui.ImVec2(x, oy),
                                   imgui.ImVec2(x + 1.0, oy + quad_h), GRID_COL)


def _mark_positions(state: MapState, grid: np.ndarray, want: bool,
                    origin: tuple[float, float], pan: tuple[float, float],
                    sx: float, sy: float, b0: int, b1: int, c0: int,
                    c1: int) -> tuple[list[float], list[float]]:
    """Screen positions of every visible cell where grid == want.

    Cached on the state: while the view is static this is byte-identical frame
    after frame, and it used to be rebuilt from scratch (with two ImVec2
    allocations per mark) 60 times a second.
    """
    key = (want, b0, b1, c0, c1, round(sx, 3), round(sy, 3),
           round(pan[0], 3), round(pan[1], 3), round(origin[0], 1),
           round(origin[1], 1))
    cached = getattr(state, _STIPPLE_ATTR, None)
    if cached is not None and cached[0] is grid and cached[1] == key:
        return cached[2]
    sub = grid[c0:c1, b0:b1]
    rows, cols = np.nonzero(sub if want else ~sub)
    xs = (origin[0] + ((b0 + cols) - pan[0]) * sx).tolist()
    ys = (origin[1] + ((c0 + rows) - pan[1]) * sy).tolist()
    setattr(state, _STIPPLE_ATTR, (grid, key, (xs, ys)))
    return (xs, ys)


def _draw_hatch(dl, origin: tuple[float, float], quad_w: float,
                quad_h: float) -> None:
    """A diagonal hatch over the whole quad: the aggregate 'mixed' marker.

    Opaque white, and drawn with the AA line path off: at alpha 46 this was
    44,636 stray pixels in 30 invented colours over a third of the canvas (the
    "three colours on media that is one state" the hatch_dense note describes).
    It now hides one pixel in nine instead of tinting nine in nine, which is the
    honest form of the same marker -- and it is still off by default, because
    hiding a ninth of the cells is a real cost and the count it stands for is
    printed under the map either way.
    """
    ox, oy = origin
    step = HATCH_SPACING_PX
    with _no_aa_lines(dl):
        x = -quad_h
        while x < quad_w:
            dl.add_line(imgui.ImVec2(_snap(ox + max(x, 0.0)),
                                     _snap(oy + max(0.0, -x))),
                        imgui.ImVec2(_snap(ox + min(x + quad_h, quad_w)),
                                     _snap(oy + min(quad_h, quad_w - x))),
                        HATCH_COL, 1.0)
            x += step


def _draw_stipple(dl, state: MapState, summary: Summary | None,
                  origin: tuple[float, float], quad_w: float, quad_h: float,
                  pan: tuple[float, float], sx: float, sy: float,
                  hatch: bool = False) -> str | None:
    """Mark every visible block that holds more than one page state.

    A block's colour is a precedence pointer, not a claim of purity, so a mixed
    block must be marked -- and this drive is 82% mixed, so at the tick's own
    minimum legible zoom the count runs to several thousand on any wide panel.
    The old code returned and drew nothing above its cap, i.e. the honesty
    marker vanished exactly where the drive is most mixed, while the "Mixed"
    checkbox still read as ticked. It now degrades instead:

      <= MAX_STIPPLE_MARKS mixed   -> a white diagonal tick per mixed block
      too many, but few pure ones  -> the pure blocks get a dark dot (the
                                      inverse marker: far fewer of them)
      too many of both             -> the aggregate case: Counted and reported
                                      in words, and drawn as a diagonal hatch
                                      only when `hatch` is on

    -> a sentence for the honesty line whenever the marker is not the literal
    per-block tick, so the reader is told which of the three they are seeing.
    Below STIPPLE_MIN_PX nothing is legible per cell; that case is reported too.

    `hatch` is MapState.hatch_dense and defaults to False. The aggregate wash
    re-blends the map it is drawn over -- see the field's own note -- so the
    default aggregate marker is the returned sentence, which draw() prints
    under the map every frame. That is the whole contract: the marker may
    change form as the zoom makes a form unreadable, and it may never go quiet
    while the "Mixed" box is ticked.
    """
    grid = _mixed_grid(summary)
    if grid is None:
        return None
    b0 = max(int(math.floor(pan[0])), 0)
    b1 = min(int(math.ceil(pan[0] + quad_w / sx)) + 1, N_BLOCK)
    c0 = max(int(math.floor(pan[1])), 0)
    c1 = min(int(math.ceil(pan[1] + quad_h / sy)) + 1, N_CE)
    if b1 <= b0 or c1 <= c0:
        return None
    sub = grid[c0:c1, b0:b1]
    n_mixed = int(sub.sum())
    n_cells = int(sub.size)
    if n_mixed == 0:
        return None
    if sx < STIPPLE_MIN_PX:
        return _aggregate(dl, origin, quad_w, quad_h, hatch,
                          "%d of %d visible blocks hold more than one page "
                          "state -- at %.2f px per block a per-block tick is "
                          "not legible" % (n_mixed, n_cells, sx))

    if n_mixed <= MAX_STIPPLE_MARKS:
        xs, ys = _mark_positions(state, grid, True, origin, pan, sx, sy,
                                 b0, b1, c0, c1)
        inset_x = min(sx * 0.25, 3.0)
        inset_y = min(sy * 0.25, 3.0)
        # AA off and both ends snapped: the tick is the one mark here that is
        # genuinely diagonal, so it cannot become a quad, and the AA path spent
        # two thirds of its pixels on a 29%-white fringe over the cell it was
        # marking (see the rule at the top of this file).
        with _no_aa_lines(dl):
            for x, y in zip(xs, ys):
                dl.add_line(imgui.ImVec2(_snap(x + inset_x),
                                         _snap(y + sy - inset_y)),
                            imgui.ImVec2(_snap(x + sx - inset_x),
                                         _snap(y + inset_y)),
                            STIPPLE_COL, 1.0)
        return None

    n_pure = n_cells - n_mixed
    if n_pure <= MAX_STIPPLE_MARKS:
        xs, ys = _mark_positions(state, grid, False, origin, pan, sx, sy,
                                 b0, b1, c0, c1)
        dot_w = max(2.0, min(sx * 0.5, 6.0))
        dot_h = max(2.0, min(sy * 0.5, 6.0))
        # Snapped to whole pixels: the dot centres were fractional (cx - w/2 on
        # a sub-pixel cell), which is rule 3 -- a fill on a fractional edge is
        # safe, but keeping the whole marker on the pixel grid is what makes
        # "one dot is n px of PURE_COL" a checkable statement.
        for x, y in zip(xs, ys):
            cx = x + sx * 0.5
            cy = y + sy * 0.5
            dl.add_rect_filled(imgui.ImVec2(_snap(cx - dot_w * 0.5),
                                            _snap(cy - dot_h * 0.5)),
                               imgui.ImVec2(_snap(cx + dot_w * 0.5),
                                            _snap(cy + dot_h * 0.5)),
                               PURE_COL)
        return ("INVERTED marker: %d of %d visible blocks are mixed, so the %d "
                "PURE blocks carry the dark dot instead of ticking the rest"
                % (n_mixed, n_cells, n_pure))

    return _aggregate(dl, origin, quad_w, quad_h, hatch,
                      "%d of %d visible blocks hold more than one page state, "
                      "and so do %d of the rest -- too dense to mark either way"
                      % (n_mixed, n_cells, n_pure))


def _aggregate(dl, origin: tuple[float, float], quad_w: float, quad_h: float,
               hatch: bool, count_sentence: str) -> str:
    """The marker for "too many to mark individually": words, or words + hatch.

    Both callers reach the same place for the same reason, so they say it the
    same way. The count is stated either way -- that is the marker -- and the
    sentence names which form is on screen so a reader can never take an
    unhatched map for an unmixed one.
    """
    if hatch:
        _draw_hatch(dl, origin, quad_w, quad_h)
    return _dense_note(count_sentence, hatch)


def _dense_note(count_sentence: str, hatch: bool) -> str:
    """The aggregate marker's words.  Pure -- no draw list, no imgui.

    Separated from the drawing so _footer_height can measure the longest
    wording at import time without a frame in progress, and so the sentence
    the footer reserves for is the sentence _aggregate returns rather than a
    copy of it that can drift.
    """
    if hatch:
        return "HATCHED: " + count_sentence
    return (count_sentence + ". Counted, not drawn: the hatch is an opaque "
            "diagonal every 9 px, so drawing it would HIDE one pixel column in "
            "nine of a map that is already reducing several blocks into each "
            "one. Zoom in to mark them individually, or tick 'Hatch' to "
            "overlay it anyway")


def _cell_rect(origin: tuple[float, float], ce: int, block: int,
               pan: tuple[float, float], sx: float,
               sy: float) -> tuple[imgui.ImVec2, imgui.ImVec2]:
    x = origin[0] + (block - pan[0]) * sx
    y = origin[1] + (ce - pan[1]) * sy
    # A sub-pixel cell still needs a visible marker, hence the 3 px floor.
    w = max(sx, 3.0)
    h = max(sy, 3.0)
    return imgui.ImVec2(x, y), imgui.ImVec2(x + w, y + h)


def _hover_text(summary: Summary | None, ce: int, block: int) -> str:
    if summary is None:
        return f"ce {ce}  block {block}  (no summary loaded)"
    try:
        # describe_block reports programmed/erased/skipped/absent/zero counts
        # separately -- the tooltip must never reduce a block to one colour.
        return model.describe_block(summary, ce, block)
    except Exception as exc:                 # a tooltip must never kill a frame
        return f"ce {ce}  block {block}  (describe failed: {exc})"


# The two sentences under the map are the honesty footer, and they are the
# reason this view used to end in a scrollbar: the canvas child claimed the
# whole content region and the footer was then drawn past the bottom of the
# window, taking the last CE row with it. draw() reserves the footer's height
# before it sizes the child, and reserves it by measurement -- the sentences
# wrap, so "two text lines" is only true at some panel widths.
#
# These samples mirror the sentences drawn in draw() and _draw_stipple with
# worst-case numbers substituted, so the reservation is an upper bound on the
# real text and can never come up one wrapped line short.
#: Line 1 of the footer: the px/block sentence, which is always drawn.
_FOOTER_SCALE_SAMPLE = (
    "00.00 px/block: 0000.0 blocks share each pixel column, reduced "
    "rarest-first -- a skipped, absent or all-zero block always wins its "
    "column, so none can be dropped; hover for the exact counts")

#: Line 2: the mixed-marker sentence. Only one of these is ever on screen, so
#: the reservation is their max, not their sum. The unhatched aggregate note is
#: the long one, and it is the default one -- reserving for the old short
#: "hatched: ..." wording would put the scrollbar straight back.
_FOOTER_MIXED_SAMPLES = (
    "INVERTED marker: 48000 of 48000 visible blocks are mixed, so the 48000 "
    "PURE blocks carry the dark dot instead of ticking the rest",
    _dense_note("48000 of 48000 visible blocks hold more than one page state, "
                "and so do 48000 of the rest -- too dense to mark either way",
                False),
)


def _footer_height(view_w: float) -> float:
    """Height the honesty footer will need at this width, wrapped.

    Measured with calc_text_size at the live font, never a constant: the font
    the backend loaded is not knowable from here, and a magic 34 px is exactly
    the kind of guess that put the scrollbar there in the first place.

    The second line is reserved at its longest wording (see the samples above),
    so changing what the marker says can shorten this footer but never overflow
    it.
    """
    wrap = max(float(view_w), 32.0)
    spacing = float(imgui.get_style().item_spacing.y)
    total = float(imgui.calc_text_size(_FOOTER_SCALE_SAMPLE,
                                       wrap_width=wrap).y) + spacing
    total += max(float(imgui.calc_text_size(t, wrap_width=wrap).y)
                 for t in _FOOTER_MIXED_SAMPLES) + spacing
    return total


#: A fitted map may not lose a CE row to its own footer. MEASURED at 1024x700:
#: the map panel is 392 px wide, the two sentences wrap to 151 px there, and
#: the region left after the toolbar and the header row is ~115 px -- so the
#: whole map was replaced by "make the panel taller" and twelve CE rows went
#: missing to make room for a sentence explaining a picture that was not drawn.
#: Below this many px per row the footer therefore collapses to one trimmed
#: line per sentence, with the full wording on hover: the numbers lead both
#: sentences, so the claim survives the trim and only the explanation moves
#: into the tooltip.
#:
#: The threshold is GRID_MIN_PX and that is not arbitrary -- it is the row
#: height below which the map can no longer draw its own CE separators, i.e.
#: the point where the sentences have started eating the picture they describe.
#: MEASURED at 1280x800: the two wrapped sentences take 112 px of a 170 px
#: region, which left the twelve rows 4.83 px each; compacting the footer there
#: gives them 10.67 px and costs two tooltips.
FIT_MIN_CE_PX = GRID_MIN_PX


def _footer_plan(view_w: float, avail_h: float) -> tuple[float, bool]:
    """(px to reserve for the footer, whether it must be the compact form)."""
    full = _footer_height(view_w)
    if avail_h - full >= N_CE * FIT_MIN_CE_PX:
        return full, False
    line = float(imgui.get_text_line_height())
    spacing = float(imgui.get_style().item_spacing.y)
    return 2.0 * (line + spacing), True


def one_line(text: str, width: float) -> str:
    """`text` trimmed to what fits `width` on one line, ellipsised when cut.

    Trimming rather than wrapping is what keeps the compact footer exactly one
    text line tall, which is what _footer_plan reserved. Unwrapped text that
    overflows would set the window's horizontal scroll extent instead, i.e.
    trade a vertical scrollbar for a horizontal one.

    Public: blockview's own one-line footer has the same problem and must not
    grow a second copy of the measurement.
    """
    width = max(float(width), 32.0)
    if float(imgui.calc_text_size(text).x) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if float(imgui.calc_text_size(text[:mid] + "...").x) <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "..."


def _footer_text(text: str, view_w: float, compact: bool) -> None:
    """One footer sentence: wrapped in full, or trimmed with the rest on hover.

    The full sentence is never lost -- a trimmed line always carries the
    tooltip, so the reader can still get the whole claim without resizing the
    window.
    """
    if not compact:
        imgui.text_disabled(text)
        return
    imgui.text_disabled(one_line(text, view_w))
    if imgui.is_item_hovered():
        imgui.set_tooltip(text)


_ZOOM_SLIDER_W = 160.0


def _button_w(text: str) -> float:
    """Width imgui will give a button with this label."""
    return (float(imgui.calc_text_size(text).x)
            + float(imgui.get_style().frame_padding.x) * 2.0)


def _check_w(text: str) -> float:
    """Width of a checkbox with this label: the box is one frame height."""
    return (float(imgui.get_frame_height())
            + float(imgui.get_style().item_inner_spacing.x)
            + float(imgui.calc_text_size(text).x))


def same_line_or_wrap(next_w: float) -> None:
    """same_line(), unless the next widget would not fit on this row.

    A same_line chain is unconditional, so on a narrow panel it runs straight
    off the right edge; WIN_MAP has no HorizontalScrollbar flag, so what
    overflows is not scrolled to, it is clipped -- an invisible, unclickable
    button. This breaks the row instead. The canvas below is sized from
    get_content_region_avail() after the header, so a wrapped second row costs
    canvas height and never overflow.

    Public because app.py's mode/address toolbar is drawn into the same window
    and had the same defect: MEASURED at 1280x800, that toolbar's right edge was
    875 px inside a 631 px content region, and the Drive map window's
    scroll_max_x was 244 px -- all of it the toolbar's, none of it this header's
    (an earlier version of this docstring claimed the 244 px as its own fix; it
    was a window-level reading of a widget one function away). Two same_line
    chains in one window need one wrap rule, not two.
    """
    style = imgui.get_style()
    imgui.same_line()
    room = float(imgui.get_content_region_avail().x)
    if room < float(next_w) + float(style.item_spacing.x):
        imgui.new_line()


#: The private spelling this module used before app.py needed it too.
_same_line_or_wrap = same_line_or_wrap


def label_w(text: str) -> float:
    """Width of a plain text run, for callers measuring a same_line chain."""
    return float(imgui.calc_text_size(text).x)


def input_int_w(width: float, label: str) -> float:
    """Width imgui gives `input_int(label, ..., step, step_fast)` at
    set_next_item_width(width): the field, both step buttons, and the label."""
    style = imgui.get_style()
    button = float(imgui.get_frame_height())
    return (float(width) + (button + float(style.item_inner_spacing.x)) * 2.0
            + float(style.item_inner_spacing.x) + label_w(label))


def draw(state: MapState, rgba: np.ndarray, summary: Summary,
         label: str = "Drive map", ranks: np.ndarray | None = None) -> MapEvent:
    """Upload (only when dirty), draw one quad, hit-test in texel space.

    `ranks` is an optional uint8[12, 4000] of visibility precedence for the
    column reduction -- which block wins a pixel that several blocks share.  It
    defaults to the page-state precedence, which is right for every mode whose
    colour is a page state; a mode colouring by something else passes its own,
    so the thing that must not be missed is the thing that survives.
    """
    hover: tuple[int, int] | None = None
    clicked: tuple[int, int] | None = None
    double_clicked = False
    context: tuple[int, int] | None = None
    mixed_note: str | None = None
    reduced = None

    imgui.push_id(label)
    try:
        # ---- header widgets: a handful, never 48000 ----------------------
        imgui.text(label)
        _same_line_or_wrap(_button_w("Fit"))
        fit_now = imgui.button("Fit")
        _same_line_or_wrap(_check_w("Grid"))
        changed, state.show_grid = imgui.checkbox("Grid", state.show_grid)
        _same_line_or_wrap(_check_w("Mixed"))
        changed, state.stipple_mixed = imgui.checkbox("Mixed",
                                                      state.stipple_mixed)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Mark blocks that hold more than one page state, so a "
                "precedence colour is never read as purity.\n"
                "82% of this drive is mixed, so where individual ticks would "
                "be unreadable the marker degrades -- to a dot on the PURE "
                "blocks, or to the counted sentence under the map -- and that "
                "line always says which of the three you are looking at. It "
                "never silently draws nothing.")
        _same_line_or_wrap(_check_w("Hatch"))
        changed, state.hatch_dense = imgui.checkbox("Hatch", state.hatch_dense)
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Overlay the aggregate diagonal hatch where individual marks "
                "do not fit.\n"
                "OFF by default: the hatch is opaque (it used to be a "
                "translucent wash that turned one state into three colours in "
                "no palette), so at 9 px spacing it now HIDES one pixel column "
                "in nine rather than tinting all nine. The count it stands for "
                "is printed under the map either way.")
        _same_line_or_wrap(_ZOOM_SLIDER_W
                           + imgui.calc_text_size("zoom").x
                           + imgui.get_style().item_inner_spacing.x)
        imgui.set_next_item_width(_ZOOM_SLIDER_W)
        lz = math.log2(max(state.zoom, MIN_ZOOM))
        moved, lz = imgui.slider_float("zoom", lz,
                                       math.log2(MIN_ZOOM),
                                       math.log2(MAX_ZOOM),
                                       "%.2f px/block" % state.zoom)
        if moved:
            state.zoom = min(max(2.0 ** lz, MIN_ZOOM), MAX_ZOOM)

        if rgba is None:
            imgui.text_disabled("no colour map: the summary is unavailable")
            return MapEvent()

        arr = np.asarray(rgba)
        if arr.ndim != 3 or arr.shape[2] != 4:
            imgui.text_disabled(
                f"colour map has shape {arr.shape}; expected (12, 4000, 4)")
            return MapEvent()
        if not arr.flags["C_CONTIGUOUS"] or arr.dtype != np.uint8:
            arr = np.ascontiguousarray(arr, dtype=np.uint8)

        # ---- the canvas --------------------------------------------------
        # one subtraction, measured: the honesty footer below the child. It is
        # drawn after end_child, so a child that takes the whole region pushes
        # it out of the window and the window grows a scrollbar over the last
        # CE row.
        #
        # There is no window-padding term. The canvas child is
        # created with child_flags 0 -- neither Borders nor
        # AlwaysUseWindowPadding -- and imgui zeroes WindowPadding for such a
        # child, so the padding this used to subtract does not exist. VERIFIED:
        # the requested child height and get_content_region_avail().y inside it
        # are equal at every window size (1800x1000: 133/133, 1280x800:
        # 107/107). It cancelled out only while `room_h` was what bound; in the
        # MAX_CE_PX-bound case it made the child 16 px taller than its own
        # picture and painted that band in EMPTY_COL -- the wasted vertical
        # space this sizing pass was about.
        #
        # Whatever is left is then capped at the height the map actually wants
        # (N_CE * sy). After a Fit that cap is the room, because fill_height
        # makes the twelve rows divide it -- fitting both axes means the canvas
        # is exactly as tall as the picture and the picture exactly as tall as
        # the canvas. Unfitted, sy stops at MAX_CE_PX and the canvas stops with
        # it rather than painting an EMPTY_COL band under the map.
        region = imgui.get_content_region_avail()
        view_w = max(float(region.x), 16.0)
        avail_h = max(float(region.y), 32.0)
        # Clamped to the region, never floored above it: a floor of 48 px inside
        # 31 px of room is a child taller than its parent by construction, which
        # is the scrollbar this pass removed coming back at small window sizes
        # (MEASURED at 1024x700: scroll_max_y 17 px, exactly 48 - 31).
        footer_h, footer_compact = _footer_plan(view_w, avail_h)
        room_h = max(avail_h - footer_h, 0.0)
        _, want_sy = _scales(state, max(room_h, 16.0))
        view_h = min(room_h, N_CE * want_sy)

        if view_h < N_CE:
            # Not even one pixel per CE row after the footer. Every other
            # failure mode here hides a CE row -- behind a scrollbar, or by
            # rounding it away -- and a hidden row reads as "this CE does not
            # exist", which is the one claim this view may not make by
            # accident. So the picture is skipped and the footer says why: no
            # map is honest, a map missing a die is not.
            # Wrapped. An unwrapped sentence sets the window's horizontal
            # scroll extent, and this one is ~1050 px: the panel that is too
            # short would then also be too narrow, which is a second defect
            # reported in place of the first.
            imgui.push_text_wrap_pos(0.0)
            imgui.text_disabled(
                "the drive map needs %d px of height for its twelve CE rows "
                "and this panel has %.0f left after the footer -- drop a row "
                "and a dead CE reads as a CE that does not exist, so no map is "
                "drawn. Make the panel taller."
                % (N_CE, max(room_h, 0.0)))
            imgui.pop_text_wrap_pos()
            return MapEvent()

        if fit_now:
            fit(state, view_w, view_h)

        imgui.begin_child(
            "canvas", imgui.ImVec2(view_w, view_h), 0,
            imgui.WindowFlags_.no_scrollbar | imgui.WindowFlags_.no_scroll_with_mouse)
        try:
            inner = imgui.get_content_region_avail()
            view_w = max(float(inner.x), 16.0)
            # Floored at N_CE, not at 32: a floor above the child's real height
            # makes sy bigger than the room and pushes the last CE rows out of
            # the bottom of a canvas that reports no scrollbar. Twelve is the
            # same minimum the early return above enforces (1 px per row).
            view_h = max(float(inner.y), float(N_CE))
            cursor = imgui.get_cursor_screen_pos()

            # Reserve the CE-label gutter before the scales are computed, so the
            # grid is fitted to the room it will actually get. Taking it out
            # afterwards would push the right-hand blocks off the canvas -- the
            # same class of error as the footer that used to clip CE11.
            sy_probe = _scales(state, view_h)[1]
            gutter = ce_gutter_px(sy_probe)
            view_w = max(view_w - gutter, 16.0)
            origin = (float(cursor.x) + gutter, float(cursor.y))

            sx, sy = _scales(state, view_h)
            state.pan = _clamp_pan(state.pan, view_w, view_h, sx, sy)
            _remember(state, view_w, view_h, sx, sy)

            content_w = N_BLOCK * sx
            content_h = N_CE * sy
            # Clip the quad to the real extent: a uv beyond 1.0 would clamp and
            # smear the edge column into empty space, i.e. paint media where
            # there is no record at all.
            quad_w = max(min(view_w, content_w - state.pan[0] * sx), 1.0)
            quad_h = max(min(view_h, content_h - state.pan[1] * sy), 1.0)

            dl = imgui.get_window_draw_list()
            dl.add_rect_filled(imgui.ImVec2(*origin),
                               imgui.ImVec2(origin[0] + view_w,
                                            origin[1] + view_h),
                               EMPTY_COL)

            # CE labels down the left, one per row, in the reserved gutter.
            # Drawn, not widgets: twelve add_text calls cost nothing, and the
            # map without them was twelve anonymous stripes.
            if gutter:
                lab = imgui.get_color_u32(imgui.Col_.text_disabled.value)
                fh = imgui.get_font_size()
                first_ce = int(state.pan[1])
                for row in range(N_CE - first_ce):
                    ce = first_ce + row
                    y = origin[1] + (ce - state.pan[1]) * sy
                    if y + sy < origin[1] or y > origin[1] + view_h:
                        continue
                    dl.add_text(imgui.ImVec2(cursor.x + 2.0,
                                             y + (sy - fh) * 0.5),
                                lab, f"CE{ce}")

            # ---- reduce in numpy when a pixel would have to stand for
            # several block columns; never let GL_NEAREST choose which
            # blocks to drop.
            reduced = None
            b0 = max(int(math.floor(state.pan[0])), 0)
            span = min(int(math.ceil(quad_w / sx)) + 1, N_BLOCK - b0)
            out_w = max(1, int(round(quad_w)))
            if summary is not None and span > out_w:
                reduced = _reduced_image(state, arr, summary, b0, span, out_w,
                                         ranks)

            if reduced is not None:
                key = ("reduced", b0, span, out_w)
                image = reduced
                uv0 = imgui.ImVec2(0.0, state.pan[1] / N_CE)
                uv1 = imgui.ImVec2(1.0, (state.pan[1] + quad_h / sy) / N_CE)
            else:
                key = ("full",)
                image = arr
                uv0 = imgui.ImVec2(state.pan[0] / N_BLOCK, state.pan[1] / N_CE)
                uv1 = imgui.ImVec2((state.pan[0] + quad_w / sx) / N_BLOCK,
                                   (state.pan[1] + quad_h / sy) / N_CE)

            # ---- MAGNIFY IN NUMPY, NEVER ON THE SAMPLER -------------------
            # When one block is drawn wider than a pixel the quad magnifies the
            # texture, and then every pixel of the picture is whatever the
            # sampler decides. We ask for GL_NEAREST and the driver reports
            # GL_NEAREST -- and the frame still came back smoothly interpolated,
            # measured off the back buffer (so it is not the compositor) with
            # flat, alpha-255 texel data (so it is not the palette). Something
            # between this call and the fragment does not honour the filter.
            #
            # Rather than keep arguing with it: expand the texels here, so the
            # texture is 1:1 with the quad and there is nothing left to
            # interpolate between. np.repeat is exact -- it duplicates a texel,
            # it cannot invent a value between two of them -- which is the same
            # guarantee GL_NEAREST was supposed to give and this one does not
            # depend on a driver, a backend or a sampler honouring anything.
            #
            # Cost is bounded: the expansion is clamped to the quad, so it is at
            # most one uint8 RGBA image the size of the visible canvas, rebuilt
            # only when the view changes.
            up_x = 1
            if reduced is None and sx > 1.0:
                first = int(math.floor(state.pan[0]))
                n_vis = min(int(math.ceil(quad_w / sx)) + 1, N_BLOCK - first)
                up_x = max(1, min(int(math.ceil(sx)), _MAX_UPSCALE))
                if n_vis > 0 and up_x > 1:
                    image = np.repeat(image[:, first:first + n_vis], up_x,
                                      axis=1)
                    key = ("upx", first, n_vis, up_x)
                    # y magnifies too -- 12 CE rows over a tall canvas is the
                    # same defect on the other axis, and it is what makes the
                    # top rows fade into the panel background.
                    up_y = max(1, min(int(math.ceil(sy)), _MAX_UPSCALE))
                    if up_y > 1:
                        image = np.repeat(image, up_y, axis=0)
                        key = key + (up_y,)
                        uv0 = imgui.ImVec2(uv0.x,
                                           state.pan[1] * up_y / image.shape[0])
                        uv1 = imgui.ImVec2(
                            uv1.x,
                            min((state.pan[1] + quad_h / sy) * up_y
                                / image.shape[0], 1.0))
                    # uv now spans the expanded window, not the whole drive.
                    frac = (state.pan[0] - first) * up_x
                    uv0 = imgui.ImVec2(frac / image.shape[1],
                                       state.pan[1] / N_CE)
                    uv1 = imgui.ImVec2(
                        min((frac + quad_w / sx * up_x) / image.shape[1], 1.0),
                        (state.pan[1] + quad_h / sy) / N_CE)

            cached_key = getattr(state, _TEXKEY_ATTR, None)
            stale = (cached_key is None or cached_key[0] is not image
                     or cached_key[1] != key)
            if state.dirty or state.tex is None or stale:
                state.tex = gltex.upload(state.tex, image, nearest=True)
                setattr(state, _TEXKEY_ATTR, (image, key))
                state.dirty = False

            # Put the quad where `origin` says, i.e. Past the gutter. Reserving
            # the gutter in `origin` is not enough on its own: imgui.image()
            # draws at the cursor, which is still the child's left edge, so the
            # image was covering the gutter and painting over the CE labels
            # already drawn there. blockview sets the cursor for the same
            # reason; this view had inherited the assumption that origin and the
            # cursor were the same point, which stopped being true the moment a
            # gutter existed.
            imgui.set_cursor_screen_pos(imgui.ImVec2(origin[0], origin[1]))
            imgui.image(_tex_ref(state.tex), imgui.ImVec2(quad_w, quad_h),
                        uv0, uv1)
            LAST_CANVAS.update(rect=(origin[0], origin[1], quad_w, quad_h),
                               image=image)
            stroke_rect(dl, imgui.ImVec2(*origin),
                        imgui.ImVec2(origin[0] + quad_w, origin[1] + quad_h),
                        BORDER_COL)

            if state.show_grid:
                _draw_grid(dl, origin, quad_w, quad_h, state.pan, sx, sy)
            if state.stipple_mixed:
                mixed_note = _draw_stipple(dl, state, summary, origin, quad_w,
                                           quad_h, state.pan, sx, sy,
                                           state.hatch_dense)

            # ---- interaction: one invisible button over the whole region --
            imgui.set_cursor_screen_pos(imgui.ImVec2(*origin))
            imgui.invisible_button(
                "grid", imgui.ImVec2(view_w, view_h),
                imgui.ButtonFlags_.mouse_button_left
                | imgui.ButtonFlags_.mouse_button_right
                | imgui.ButtonFlags_.mouse_button_middle)
            hovered = imgui.is_item_hovered()
            io = imgui.get_io()
            mouse = imgui.get_mouse_pos()

            if hovered:
                wheel = float(io.mouse_wheel)
                wheel_h = float(io.mouse_wheel_h)
                if wheel != 0.0 and io.key_ctrl:
                    # Zoom about the cursor: the texel under the pointer stays
                    # under the pointer, on both axes independently.
                    tx = state.pan[0] + (mouse.x - origin[0]) / sx
                    ty = state.pan[1] + (mouse.y - origin[1]) / sy
                    new_zoom = min(max(state.zoom * (ZOOM_STEP ** wheel),
                                       MIN_ZOOM), MAX_ZOOM)
                    state.zoom = new_zoom
                    nsx, nsy = _scales(state, view_h)
                    state.pan = _clamp_pan(
                        (tx - (mouse.x - origin[0]) / nsx,
                         ty - (mouse.y - origin[1]) / nsy),
                        view_w, view_h, nsx, nsy)
                    sx, sy = nsx, nsy
                    _remember(state, view_w, view_h, sx, sy)
                elif wheel != 0.0 or wheel_h != 0.0:
                    # A trackpad two-finger swipe arrives as the wheel axes and
                    # is indistinguishable from a wheel notch, so the two
                    # gestures cannot both be "wheel": unmodified PANS, and
                    # ctrl+wheel zooms.  That costs nothing on a trackpad -- a
                    # precision-touchpad pinch is delivered as ctrl+wheel by the
                    # OS, so pinch-to-zoom lands on the branch above with no
                    # second code path -- and on a mouse it is the convention
                    # every map and browser already uses.
                    #
                    # Signs follow imgui's own window scrolling (Scroll -=
                    # wheel), so a swipe moves this canvas the same direction it
                    # moves every other panel in the app.
                    state.pan = _clamp_pan(
                        (state.pan[0] - wheel_h * WHEEL_PAN_PX / sx,
                         state.pan[1] - wheel * WHEEL_PAN_PX / sy),
                        view_w, view_h, sx, sy)

            dragging = (imgui.is_item_active()
                        and imgui.is_mouse_dragging(imgui.MouseButton_.left)) \
                or (hovered and imgui.is_mouse_dragging(imgui.MouseButton_.middle))
            if dragging:
                dx = float(io.mouse_delta.x)
                dy = float(io.mouse_delta.y)
                if dx or dy:
                    state.pan = _clamp_pan(
                        (state.pan[0] - dx / sx, state.pan[1] - dy / sy),
                        view_w, view_h, sx, sy)

            cell = cell_at(state, mouse.x, mouse.y, origin) if hovered else None
            # Outside the drawn quad there is genuinely nothing, even if the
            # region extends further: report no hover rather than the edge cell.
            if cell is not None and (mouse.x - origin[0] > quad_w
                                     or mouse.y - origin[1] > quad_h):
                cell = None
            state.hover = cell
            hover = cell

            if cell is not None:
                imgui.set_tooltip(_hover_text(summary, cell[0], cell[1]))
                p0, p1 = _cell_rect(origin, cell[0], cell[1], state.pan, sx, sy)
                stroke_rect(dl, p0, p1, HOVER_COL)
                if not dragging:
                    if imgui.is_mouse_double_clicked(imgui.MouseButton_.left):
                        clicked = cell
                        double_clicked = True
                    elif imgui.is_mouse_clicked(imgui.MouseButton_.left):
                        clicked = cell
                    if imgui.is_mouse_clicked(imgui.MouseButton_.right):
                        context = cell
                        state.selected = cell

            if clicked is not None:
                state.selected = clicked

            if state.selected is not None:
                ce, block = state.selected
                p0, p1 = _cell_rect(origin, ce, block, state.pan, sx, sy)
                # Black inside white: readable over every palette colour,
                # including the dark ABSENT hole.
                #
                # Both rings are stroke_rect, i.e. quads on whole pixels, not
                # add_rect. add_rect at thickness 2 was the single dirtiest
                # thing on the default map: an exact 50% coverage fringe all the
                # way round, six invented colours, and only 18 pixels of the
                # whole marker ever reaching white. The AA fringe was there even
                # though the colour was a=255 and the coordinates were integers
                # -- opacity alone does not save a stroked outline.
                stroke_rect(dl, imgui.ImVec2(p0.x - 1.0, p0.y - 1.0),
                            imgui.ImVec2(p1.x + 1.0, p1.y + 1.0),
                            SELECT_OUTER, 2.0)
                stroke_rect(dl, p0, p1, SELECT_INNER)
        finally:
            imgui.end_child()

        # ---- honesty line: say when a pixel stands for many blocks -------
        # Wrapped at the panel edge, which is also how _footer_height measured
        # it: unwrapped, these sentences run past the right edge and the
        # horizontal scrollbar they raise eats the row the map was sized for.
        #
        # When the panel is too short to afford the wrapped form, _footer_plan
        # said so and each sentence is trimmed to one line with the full text on
        # hover -- the alternative, measured, was a panel with no map in it.
        imgui.push_text_wrap_pos(0.0)
        if reduced is not None:
            _footer_text(
                "%.2f px/block: %.1f blocks share each pixel column, reduced "
                "rarest-first -- a skipped, absent or all-zero block always "
                "wins its column, so none can be dropped; hover for the exact "
                "counts" % (sx, 1.0 / max(sx, 1e-6)), view_w, footer_compact)
        elif sx < 1.0:
            _footer_text(
                "%.2f px/block: %.1f blocks share each pixel column -- no "
                "summary to reduce with, so not every block is painted; zoom in"
                % (sx, 1.0 / max(sx, 1e-6)), view_w, footer_compact)
        else:
            _footer_text("%.2f px/block, %.1f px/CE" % (sx, sy), view_w,
                         footer_compact)
        if mixed_note:
            _footer_text(mixed_note, view_w, footer_compact)
        imgui.pop_text_wrap_pos()
    finally:
        imgui.pop_id()

    return MapEvent(hover=hover, clicked=clicked,
                    double_clicked=double_clicked, context=context)
