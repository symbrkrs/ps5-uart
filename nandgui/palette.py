"""Colour truth for the NAND viewer.

This module is the single place where a page state or a block class becomes a
colour.  It is pure numpy: no imgui, no OpenGL, no I/O, so every claim it makes
can be asserted headless in a test.

The palette RULE (the correctness requirement of the whole project):

    programmed  real content
    erased      the media reads 0xFF -- a zero-length record that exists
    skipped not read, reason recorded -- reading raises
    absent      no record at all: we never looked
    all-zero    uniform 0x00, the dead-CE bus constant, not erased media

These five are never merged, never share a colour, and never fall off the
legend.  This project has twice drawn wrong conclusions by merging them (190 MB
of zeros reported as content; a metadata-driven skip nearly lost the boot
block), so the colours below are chosen for maximum separation -- different
hue and different lightness -- rather than for prettiness:

    programmed  saturated green      content, the common case, calm
    erased      strong blue          clean media, also common, calm
    skipped     hot orange           rare and dangerous: must shout
    absent      near-black violet    a hole in the record, not media
    all-zero    magenta              alarming, because a bus constant is
    not probed  slate grey           "we have not looked yet", distinct from
                                     all five so a loading stripe can never be
                                     mistaken for erased media

SEPARATION IS AN INVARIANT, NOT A STYLE CHOICE, AND IT SPANS THE MODE TABLES
Too.  app.draw_legend pins the five-state legend on screen in every mode, so a
mode colour sitting next to a state swatch is compared with it by eye.  The
mode colours below (CELL_*, MCRC_*, DEFECT_RGBA, _RAMP_STOPS) are therefore all
held at least MIN_STATE_DELTA_E CIELab units away from every STATE_RGBA entry
and from NONE_RGBA; _check_mode_color_separation() asserts it and app.py's
--self-test runs that assertion.  Three colours used to fail it -- an MCRC
"clean" green 9.7 from PROGRAMMED, a TLC blue 10.4 from ERASED and a defect
orange 11.3 from SKIPPED -- which put "erased" under the majority of the drive
in cell mode and "programmed" under every clean MCRC block.
"""

from __future__ import annotations

import dataclasses
import enum

import numpy as np

from . import N_BLOCK, N_CE, N_ROWS, PAGES_SLC, PAGES_TLC, WERU_BIN_COUNT
from . import msonames, pagefmt
from .model import PageState, Summary

RGBA = tuple[int, int, int, int]


# --------------------------------------------------------------------------
# the five page states (+ the sixth rendering-only state)
# --------------------------------------------------------------------------
# Row index == PageState value.  Indexing the table by the enum is what makes
# it impossible to add a state without also giving it a colour.
STATE_RGBA: np.ndarray = np.array(
    [
        (58, 176, 96, 255),    # PROGRAMMED  green
        (56, 112, 208, 255),   # ERASED      blue
        (255, 148, 24, 255),   # SKIPPED     hot orange -- rare and dangerous
        (14, 12, 20, 255),     # ABSENT      a hole: darker than anything else
        (226, 48, 146, 255),   # ZERO        magenta -- a bus constant, not media
        (92, 98, 116, 255),    # UNKNOWN     slate: not classified yet
    ],
    dtype=np.uint8,
)

STATE_LABELS: tuple[str, ...] = (
    "programmed",
    "erased",
    "skipped",
    "absent",
    "all-zero",
    "not probed",
)

STATE_HELP: tuple[str, ...] = (
    "real content -- a record with bytes in it",
    "the media reads 0xFF: a zero-length record that EXISTS",
    "deliberately not read, with a recorded reason -- reading RAISES",
    "no record at all -- we never looked (a hole, not clean media)",
    "uniform 0x00: the dead-CE bus constant, NOT erased media",
    "not probed yet -- the worker has not classified these pages",
)

# Sentinel colour for "this attribute does not apply / is unknown":
# weru == -1, owner == -1, binid == -1 or 53, erases == -1.
# It is a neutral light grey: no ramp in this file is grey at any point and no
# categorical colour is grey either, so a sentinel can never be misread as a
# real value.  Crucially it is not the low end of the erase ramp -- an unknown
# erase count must never paint as "zero erases".
NONE_RGBA: RGBA = (200, 196, 188, 255)

# The hole colour, re-exported so mapview can stipple with the same value.
ABSENT_RGBA: RGBA = tuple(int(v) for v in STATE_RGBA[PageState.ABSENT])  # type: ignore[assignment]

# The mixed-block marker.  mapview draws a white diagonal tick over the
# precedence colour; the legend swatch for "mixed" must be that colour and not
# a PageState colour, or the legend key means something the map never draws.
# (It used to borrow STATE_RGBA[UNKNOWN], i.e. the same slate the always-pinned
# state legend labels "not probed", two rows higher in the same panel.)
STIPPLE_RGBA: RGBA = (255, 255, 255, 190)

# A neutral for legend rows that are neither a page state nor a real class
# (e.g. "N more bins").  Never index STATE_RGBA for those.
NEUTRAL_RGBA: RGBA = (150, 150, 158, 255)

# Minimum CIELab distance a mode colour must keep from every STATE_RGBA entry
# and from NONE_RGBA.  For calibration: the closest pair within STATE_RGBA is
# 38.6, and anything under ~15 is a colour match rather than a distinction.
MIN_STATE_DELTA_E = 25.0


# --------------------------------------------------------------------------
# colour modes
# --------------------------------------------------------------------------
class Mode(enum.IntEnum):
    CONTENT = 0
    CELL = 1
    WERU = 2
    OWNER = 3
    BIN = 4
    ERASES = 5
    DEFECT = 6
    MCRC = 7
    ECC = 8


MODES: tuple[Mode, ...] = tuple(Mode)

MODE_LABELS: dict[Mode, str] = {
    Mode.CONTENT: "Content",
    Mode.CELL: "Cell mode",
    Mode.WERU: "WERU",
    Mode.OWNER: "Owner",
    Mode.BIN: "WERU bin",
    Mode.ERASES: "Erase count",
    Mode.DEFECT: "Defect / status",
    Mode.MCRC: "Decoded MCRC",
    Mode.ECC: "ECC outcome",
}

MODE_HELP: dict[Mode, str] = {
    Mode.CONTENT: "Page states per block by precedence: skipped and absent beat programmed, so nothing rare hides.",
    Mode.CELL: "Pseudo-SLC (368 pages) vs native TLC (1104 pages).",
    Mode.WERU: "Wear/erase unit id, hashed to a stable hue; blocks with no WERU use the sentinel colour.",
    Mode.OWNER: "Owning MSO; -1 (no owner) uses the sentinel colour.",
    Mode.BIN: "WERU bin id; bin 53 means 'in no bin' and uses the sentinel colour, not a bin colour.",
    Mode.ERASES: "Erase-count heat; -1 (unknown) is the sentinel colour, never the low end of the ramp.",
    Mode.DEFECT: "Defect / status code from the WERU table.",
    Mode.MCRC: ("Decoded verdict per block: none decoded / all clean / mixed / "
                "all failed -- plus fw-group2, whose pages the decoded store "
                "ran through the wrong geometry, so its tally there is not a "
                "verdict about the media."),
    Mode.ECC: ("What the ECC DECODER did, per block, on the pages actually "
               "measured: clean / corrected / partly corrected / correction "
               "FAILED / not applicable (all-0x00 or erased). This is not the "
               "MCRC: a page can fail MCRC with every codeword valid, and a "
               "page can be uncorrectable with the MCRC never reached. "
               "Unmeasured blocks say so -- ECC is a per-page decode, so the "
               "map shows only what has been run."),
}

# CELL: two categories only, so pick two far-apart hues that are also far from
# every state colour (this map is often flipped to and from CONTENT, and the
# state legend is pinned under it in both).  TLC is violet, not the blue it
# used to be: that blue was 10.4 dE from ERASED, so the majority of the drive
# was painted the colour the legend directly above it calls "erased".
CELL_SLC_RGBA: RGBA = (247, 202, 88, 255)      # 33.6 dE from the nearest state
CELL_TLC_RGBA: RGBA = (138, 96, 224, 255)      # 30.9

# MCRC: five classes.  "no decoded pages" is the hole colour, not a pale shade
# of pass -- absence of evidence must not look like weak evidence.  "clean" is
# teal rather than green for the same reason as CELL_TLC: the old green was 9.7
# dE from PROGRAMMED.
MCRC_NONE_RGBA: RGBA = ABSENT_RGBA
MCRC_CLEAN_RGBA: RGBA = (40, 196, 200, 255)    # 40.9
MCRC_MIXED_RGBA: RGBA = (240, 190, 60, 255)    # 28.8
MCRC_FAIL_RGBA: RGBA = (216, 56, 66, 255)      # 45.3

# The fifth class, and the reason this mode needed one: a block whose pages the
# decoded store ran through the wrong GEOMETRY.  decoded.store decoded the whole
# drive as 4 AUs x 6-byte MCRC; in the fw-group2 region a page is five chunks
# behind 4-byte digests, so FORMAT_A cannot pass one and every such page is
# quarantined by construction.  Measured on this capture: 1835 of the 1840
# group-2 pages verify 5/5 under fw.check_group2_page, and all five of their
# blocks were painted MCRC_FAIL_RGBA and counted under "all failed".
#
# Plum: 31.6 dE from the nearest state colour, and not a shade of
# the failure red or the clean teal -- it keys "this tally is not a verdict",
# which is a third thing, exactly as NONE_RGBA keys "no value" elsewhere.
MCRC_GEOM_RGBA: RGBA = (150, 90, 140, 255)     # 31.6

# ECC: ten outcomes, one colour each, and none of them is derived from an MCRC
# verdict.  Row index == pagefmt.EccState, exactly as STATE_RGBA is indexed by
# PageState, so a state cannot be added without a colour.
#
# Four of the ten reuse a colour that already means the same thing
# elsewhere in this viewer, which is why they are excluded from the separation
# check below (as MCRC_NONE_RGBA is, for the same reason):
#
#   NOT_MEASURED   the "not probed" slate: we have not looked yet.
#   NOT_ATTEMPTED  the none sentinel: no verdict exists, and none was asked for.
#   ZERO_PAGE      the all-zero magenta: the same bus constant the CONTENT mode
#                  paints, and ECC provably cannot say anything about it.
#   ERASED         the erased blue: the same blank media.
#
# The other six are checked, and the three new ones are held >= 25 dE from every
# state colour and from each other:
#
#   CLEAN          the MCRC mode's teal -- "checked and good" reads the same way
#                  in both modes.
#   CORRECTED      yellow-green, 40.3 dE from PROGRAMMED: repaired is not the
#                  same as untouched and must not borrow the content green.
#   PARTIAL        the MCRC mode's amber -- "some of it".
#   UNCORRECTABLE  the MCRC mode's red.
#   MCRC_DISAGREES bright orchid, 39.6 dE from the nearest state.  It is
#                  Not a shade of the failure red: it keys "this
#                  should be impossible", which is a different claim from "this
#                  page is bad" -- measured 0 times in 248 failing pages, 264
#                  injections and 36,800 group-2 codewords.
ECC_UNAVAILABLE_RGBA: RGBA = (126, 112, 84, 255)    # 29.2 dE from the nearest
ECC_CORRECTED_RGBA: RGBA = (170, 220, 70, 255)      # 40.3
ECC_FLAG_RGBA: RGBA = (252, 150, 255, 255)          # 39.6

ECC_RGBA: np.ndarray = np.array(
    [
        tuple(int(v) for v in STATE_RGBA[PageState.UNKNOWN]),  # NOT_MEASURED
        NONE_RGBA,                                             # NOT_ATTEMPTED
        ECC_UNAVAILABLE_RGBA,                                  # UNAVAILABLE
        tuple(int(v) for v in STATE_RGBA[PageState.ZERO]),     # ZERO_PAGE
        tuple(int(v) for v in STATE_RGBA[PageState.ERASED]),   # ERASED
        MCRC_CLEAN_RGBA,                                       # CLEAN
        ECC_CORRECTED_RGBA,                                    # CORRECTED
        MCRC_MIXED_RGBA,                                       # PARTIAL
        MCRC_FAIL_RGBA,                                        # UNCORRECTABLE
        ECC_FLAG_RGBA,                                         # MCRC_DISAGREES
    ],
    dtype=np.uint8,
)

# DEFECT: small fixed table so a given code keeps its colour between sessions.
# Code 0 (healthy) is a cool desaturated teal, so the eye is drawn
# to the warm codes, which are the ones that mean something is wrong.  Code 2 is
# brown, not the orange it used to be (11.3 dE from SKIPPED), and code 7 is
# light violet, not near-white (18.2 dE from the NONE_RGBA sentinel).
DEFECT_RGBA: np.ndarray = np.array(
    [
        (46, 118, 110, 255),    # 27.9
        (232, 196, 64, 255),    # 35.2
        (156, 96, 32, 255),     # 40.2
        (220, 62, 62, 255),     # 49.5
        (202, 62, 192, 255),    # 28.6
        (146, 92, 224, 255),    # 34.9
        (62, 182, 222, 255),    # 40.0
        (214, 168, 255, 255),   # 37.7
    ],
    dtype=np.uint8,
)

# Control points for ramp(): a constant-hue (CIELab h 99 deg), constant-chroma
# path with L* rising 40 -> 88.  Monotone in lightness, so a greyscale reader
# still sees an ordering, and every sample along it -- not just these stops --
# stays >= 40 dE from all six state colours and from NONE_RGBA.
#
# It replaces viridis, which was perceptual but ran straight through the state
# palette: viridis stop 6 (53,183,121) is 10.3 dE from PROGRAMMED, so the hot
# end of the erase-count ramp was painted "programmed" green.
_RAMP_STOPS = np.array(
    [
        (103, 96, 9),
        (119, 111, 28),
        (135, 126, 43),
        (151, 141, 58),
        (168, 157, 73),
        (185, 173, 88),
        (202, 189, 104),
        (219, 206, 119),
        (236, 222, 135),
    ],
    dtype=np.float64,
)
_RAMP_X = np.linspace(0.0, 1.0, _RAMP_STOPS.shape[0])

_GOLDEN = 0.6180339887498949


# --------------------------------------------------------------------------
# block classification
# --------------------------------------------------------------------------
def _hole_mask(summary: Summary) -> np.ndarray:
    """-> bool[48000], True where the store holds no record for this block.

    `indexed` is the field that answers this: it is programmed + erased +
    skipped, i.e. how many pages of this block the capture actually landed.
    `pages` is the geometric page count (368 or 1104) and is never 0, so the
    `pages <= 0` test this module used to make was dead code -- and dead code
    whose comment asserts a safety property is worse than no code, because the
    reader auditing the palette rule believes the property is enforced.

    On the real summary this is exactly the 4000 rows of dead CE0.  It is not
    hard-coded to row 0 on purpose: a live-CE block that was never read is the
    same kind of hole, and its WERU/owner/erase-count metadata (which CE0
    carries in full) must not be painted as if it were an observation.
    """
    hole = summary.indexed <= 0
    return hole | (summary.pages <= 0)


def block_states(summary: Summary) -> np.ndarray:
    """-> uint8[48000] of PageState, one per block.

    The precedence is fixed and rare-beats-common on purpose: a block holding a
    single skipped page must colour as SKIPPED, because a skip is a place we
    chose not to look and the whole point of this viewer is that such places
    stay visible.  Likewise a block with any absent page is a hole, not media.
    The precise per-state counts live in model.describe_block(); the colour is
    a pointer to look, never the claim itself.
    """
    n = int(summary.pages.shape[0])
    out = np.full(n, int(PageState.ERASED), dtype=np.uint8)

    programmed = summary.programmed
    # Applied in reverse precedence order so the earliest rule wins the write.
    out[programmed > 0] = int(PageState.PROGRAMMED)
    out[summary.zero.astype(bool)] = int(PageState.ZERO)
    out[summary.absent > 0] = int(PageState.ABSENT)
    out[summary.skipped > 0] = int(PageState.SKIPPED)
    out[_hole_mask(summary)] = int(PageState.ABSENT)
    return out


def mixed_mask(summary: Summary) -> np.ndarray:
    """-> bool[48000], True where a block holds more than one page state.

    mapview stipples these.  Without the stipple the precedence colour above
    would read as purity: a 1103-page programmed block with one skipped page
    and a wholly skipped block would be the same flat orange.
    """
    parts = (
        (summary.programmed > 0).astype(np.uint8)
        + (summary.erased > 0).astype(np.uint8)
        + (summary.skipped > 0).astype(np.uint8)
        + (summary.absent > 0).astype(np.uint8)
    )
    mixed = parts > 1
    # An all-zero block whose pages are otherwise "programmed" is still one
    # state; but a zero-flagged block that also has erased/skipped/absent pages
    # is mixed, and that is already caught by the sum above.
    mixed &= ~_hole_mask(summary)
    return mixed


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------
def page_colors(states: np.ndarray) -> np.ndarray:
    """uint8[...] of PageState -> uint8[..., 4]. One np.take."""
    idx = np.asarray(states)
    # Clip rather than raise: an out-of-range value paints as "not probed",
    # which is honest, instead of crashing a frame or silently aliasing onto
    # one of the five real states.
    idx = np.clip(idx.astype(np.intp), 0, STATE_RGBA.shape[0] - 1)
    return np.ascontiguousarray(STATE_RGBA[idx])


def _hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorised hsv -> uint8 rgb (h, s, v all in 0..1, same shape)."""
    h6 = (h % 1.0) * 6.0
    i = np.floor(h6).astype(np.intp)
    f = h6 - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i = i % 6
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [v, q, p, p, t], default=v)
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [t, v, v, q, p], default=p)
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4], [p, p, t, v, v], default=q)
    return np.stack(
        [
            np.clip(r * 255.0 + 0.5, 0, 255).astype(np.uint8),
            np.clip(g * 255.0 + 0.5, 0, 255).astype(np.uint8),
            np.clip(b * 255.0 + 0.5, 0, 255).astype(np.uint8),
        ],
        axis=-1,
    )


def hash_colors(ids: np.ndarray, sentinel: int = -1) -> np.ndarray:
    """int ids -> uint8[..., 4]; `sentinel` -> NONE_RGBA.

    The golden-ratio hue step guarantees that consecutive ids land almost
    antipodally on the hue circle, which is what makes two adjacent WERUs
    obviously different rather than two shades of the same green.  A two-step
    value ladder separates ids whose hues happen to wrap close together.
    """
    arr = np.asarray(ids)
    flat = arr.astype(np.int64, copy=False)
    mask = flat == sentinel

    key = np.abs(flat).astype(np.float64)
    hue = (key * _GOLDEN + 0.11) % 1.0
    sat = np.full(flat.shape, 0.62, dtype=np.float64)
    val = np.where((np.abs(flat) % 2) == 0, 0.94, 0.74)

    rgb = _hsv_to_rgb(hue, sat, val)
    out = np.empty(flat.shape + (4,), dtype=np.uint8)
    out[..., :3] = rgb
    out[..., 3] = 255
    if mask.any():
        out[mask] = np.array(NONE_RGBA, dtype=np.uint8)
    return np.ascontiguousarray(out)


def ramp(values: np.ndarray, lo: float, hi: float, unknown: int = -1) -> np.ndarray:
    """Perceptual heat ramp -> uint8[..., 4]; values == unknown -> NONE_RGBA.

    `unknown` is masked before normalisation, never clamped into the ramp: an
    unknown erase count painted at the ramp's low end would read as a pristine
    block, which is exactly the kind of merge this project has been burned by.
    """
    arr = np.asarray(values)
    mask = arr == unknown
    x = arr.astype(np.float64, copy=True)
    span = float(hi) - float(lo)
    if span <= 0.0:
        # Degenerate range (every value equal): put everything mid-ramp rather
        # than dividing by zero or implying an ordering that is not there.
        t = np.full(x.shape, 0.5, dtype=np.float64)
    else:
        t = np.clip((x - float(lo)) / span, 0.0, 1.0)

    out = np.empty(x.shape + (4,), dtype=np.uint8)
    for ch in range(3):
        out[..., ch] = np.clip(
            np.interp(t, _RAMP_X, _RAMP_STOPS[:, ch]) + 0.5, 0, 255
        ).astype(np.uint8)
    out[..., 3] = 255
    if mask.any():
        out[mask] = np.array(NONE_RGBA, dtype=np.uint8)
    return np.ascontiguousarray(out)


def _erase_range(erases: np.ndarray,
                 live: np.ndarray | None = None) -> tuple[float, float]:
    """Contrast range for the erase ramp, ignoring the -1 unknowns.

    `live` excludes blocks with no records: they are painted as holes, so their
    metadata erase counts must not stretch the ramp the visible blocks share.
    """
    known = erases[erases >= 0] if live is None else erases[(erases >= 0) & live]
    if known.size == 0:
        return 0.0, 1.0
    lo = float(np.percentile(known, 2.0))
    hi = float(np.percentile(known, 98.0))
    if hi <= lo:
        lo = float(known.min())
        hi = float(known.max())
    return lo, hi


def _bin_ids(summary: Summary) -> np.ndarray:
    """binid with both 'no bin' spellings folded onto -1.

    WERU_BIN_COUNT (53) is itself the "in no bin" sentinel, so leaving it as a
    bin id would invent a 54th bin colour that means "no bin".
    """
    b = summary.binid.astype(np.int64, copy=True)
    b[b >= WERU_BIN_COUNT] = -1
    b[b < 0] = -1
    return b


def _defect_colors(defect: np.ndarray) -> np.ndarray:
    d = defect.astype(np.int64, copy=False)
    out = hash_colors(d, sentinel=-2)  # -2 never occurs: everything gets a hue
    known = d < DEFECT_RGBA.shape[0]
    if known.any():
        out[known] = DEFECT_RGBA[d[known]]
    return out


#: bool[48000], True where geometry.region(ce, block) is fw-group2.  Built once
#: (48000 calls into pagefmt, ~40 ms) and cached: the region map is a property
#: of the die's addressing, not of any summary, so it cannot go stale.
_G2_MASK: np.ndarray | None = None


def group2_mask() -> np.ndarray:
    """-> bool[48000], True for every fw-group2 (ce, block).

    Asked, never derived.  Block 0 is group 2 on CE 0..5 and ordinary FTL media
    on CE 6..11, so a mask built from the block number alone would mis-file six
    CEs' worth of block 0 -- the same error, one view over, that this class
    exists to correct.

    All-False when titania cannot be reached: pagefmt.region_of then answers
    REGION_UNKNOWN for every address, and a region we could not compute must
    not be asserted.  The MCRC mode then behaves exactly as it did before this
    class existed, which is the honest degradation -- and describe_block, which
    asks the same authority, goes quiet about the region on the same frame.
    """
    global _G2_MASK
    if _G2_MASK is None:
        try:
            grid = np.array(pagefmt.region_grid(N_CE, N_BLOCK), dtype=object)
            mask = np.asarray(grid == pagefmt.REGION_FW_GROUP2, dtype=bool)
            if mask.shape[0] != N_ROWS:
                mask = np.zeros(N_ROWS, dtype=bool)
        except Exception:  # a colour must never take the frame down
            mask = np.zeros(N_ROWS, dtype=bool)
        _G2_MASK = mask
    return _G2_MASK


def group2_row(block: int, n_ce: int = N_CE) -> np.ndarray:
    """-> bool[n_ce], True where (ce, block) is fw-group2.  For the stripe.

    Per CE, never per block: block 0 is group 2 on CE 0..5 and ordinary FTL
    media on CE 6..11, so a stripe that took one answer for the whole column
    would mis-file six of its own rows.
    """
    mask = group2_mask()
    out = np.zeros(int(n_ce), dtype=bool)
    if mask.shape[0] != N_ROWS:
        return out
    idx = np.arange(int(n_ce), dtype=np.int64) * N_BLOCK + int(block)
    keep = idx < N_ROWS
    out[keep] = mask[idx[keep]]
    return out


def _mcrc_class(summary: Summary) -> np.ndarray:
    """0 none decoded, 1 all clean, 2 mixed, 3 all failed, 4 wrong geometry.

    Class 4 is region-aware and it outranks the other four wherever it applies.
    summary.dec_clean/dec_fail are decoded.store's own tallies and that store
    decoded the whole drive as 4 AUs x 6-byte MCRC; over the fw-group2 region
    that is not the page geometry, so its "failed" count there is an artefact of
    the decoder and not a fact about the media.  Leaving those blocks in class 3
    painted five blocks of verified firmware in the same red as genuine damage
    and counted them in the legend under "all failed" -- while describe_block,
    on hover, printed the correction for the very same block.
    """
    clean = summary.dec_clean
    fail = summary.dec_fail
    cls = np.zeros(clean.shape[0], dtype=np.uint8)
    cls[(clean > 0) & (fail == 0)] = 1
    cls[(clean > 0) & (fail > 0)] = 2
    cls[(clean == 0) & (fail > 0)] = 3
    g2 = group2_mask()
    if g2.shape[0] == cls.shape[0]:
        # Only where something was decoded: a group-2 block with no records at
        # all is "no decoded pages" (and is painted as a hole anyway), and
        # calling that "wrong geometry" would invent a decode nobody ran.
        cls[g2 & ((clean > 0) | (fail > 0))] = 4
    return cls


_MCRC_TABLE = np.array(
    [MCRC_NONE_RGBA, MCRC_CLEAN_RGBA, MCRC_MIXED_RGBA, MCRC_FAIL_RGBA,
     MCRC_GEOM_RGBA], dtype=np.uint8
)


# --------------------------------------------------------------------------
# the map
# --------------------------------------------------------------------------
def ecc_states(ecc: np.ndarray | None, n: int) -> np.ndarray:
    """-> uint8[n] of pagefmt.EccState, defaulting to NOT_MEASURED.

    The default is the whole point: a block nobody has run the decoder over is
    NOT_MEASURED, which has its own colour and its own legend row.  Painting it
    as clean would be inventing a measurement, and painting it as failed would
    be inventing a defect.
    """
    out = np.zeros(int(n), dtype=np.uint8)      # 0 == EccState.NOT_MEASURED
    if ecc is None:
        return out
    arr = np.asarray(ecc).ravel()
    if arr.shape[0] != int(n):
        return out
    out[:] = np.clip(arr.astype(np.int64), 0, ECC_RGBA.shape[0] - 1)
    return out


#: EccState -> visibility rank for the map's column reduction, worst highest.
#: Where several blocks share one pixel column the worst outcome wins it, so a
#: single uncorrectable block among clean neighbours is never the one dropped.
#: Built from pagefmt.ECC_SEVERITY so the map's precedence and the block
#: roll-up cannot disagree about which state is more serious.
_ECC_RANK: np.ndarray = np.zeros(len(pagefmt.ECC_STATES), dtype=np.uint8)
for _i, _state in enumerate(pagefmt.ECC_SEVERITY):
    _ECC_RANK[int(_state)] = len(pagefmt.ECC_SEVERITY) - _i


def ecc_ranks(ecc: np.ndarray | None) -> np.ndarray:
    """-> uint8[12, 4000] of ECC visibility precedence, for mapview.draw."""
    states = ecc_states(ecc, N_ROWS)
    return _ECC_RANK[states].reshape(N_CE, N_BLOCK)


def colorize(summary: Summary, mode: Mode,
             ecc: np.ndarray | None = None) -> np.ndarray:
    """-> uint8[12, 4000, 4], C-contiguous, ready for gltex.upload().

    `ecc` is the per-block ECC state array (uint8[48000] of pagefmt.EccState)
    the ECC mode paints; None means "nothing measured yet", which that mode
    draws as its own NOT_MEASURED colour rather than as any verdict.
    """
    n = int(summary.pages.shape[0])

    if mode == Mode.CONTENT:
        flat = page_colors(block_states(summary))
    elif mode == Mode.CELL:
        flat = np.where(
            (summary.slc.astype(bool))[:, None],
            np.array(CELL_SLC_RGBA, dtype=np.uint8),
            np.array(CELL_TLC_RGBA, dtype=np.uint8),
        ).astype(np.uint8)
    elif mode == Mode.WERU:
        flat = hash_colors(summary.weru, sentinel=-1)
    elif mode == Mode.OWNER:
        flat = hash_colors(summary.owner, sentinel=-1)
    elif mode == Mode.BIN:
        flat = hash_colors(_bin_ids(summary), sentinel=-1)
    elif mode == Mode.ERASES:
        lo, hi = _erase_range(summary.erases, ~_hole_mask(summary))
        flat = ramp(summary.erases, lo, hi, unknown=-1)
    elif mode == Mode.DEFECT:
        flat = _defect_colors(summary.defect)
    elif mode == Mode.MCRC:
        flat = _MCRC_TABLE[_mcrc_class(summary)]
    elif mode == Mode.ECC:
        flat = ECC_RGBA[ecc_states(ecc, n)]
    else:  # pragma: no cover - Mode is exhaustive
        flat = page_colors(np.full(n, int(PageState.UNKNOWN), dtype=np.uint8))

    flat = np.ascontiguousarray(flat, dtype=np.uint8)

    # A block with no records at all is a hole in every mode, not a WERU
    # colour or a cell type inherited from metadata.  The metadata may well
    # claim an owner for a block we never read; painting that claim as if it
    # were an observation is how a metadata-driven skip nearly lost the boot
    # block.  CONTENT already does this via block_states().
    hole = _hole_mask(summary)
    if mode != Mode.CONTENT and hole.any():
        flat[hole] = np.array(ABSENT_RGBA, dtype=np.uint8)

    img = flat.reshape(N_CE, N_BLOCK, 4)
    # CE0 is dead and absent from the store: it is a hole in every mode.
    # Black or blank would read as clean media.
    img[0, :, :] = np.array(ABSENT_RGBA, dtype=np.uint8)
    return np.ascontiguousarray(img)


# --------------------------------------------------------------------------
# the block stripe: the same modes, one colour per PAGE
# --------------------------------------------------------------------------
# The drive map colours a BLOCK.  The stripe colours a PAGE, and that is the
# whole point of giving it the modes: the map can say "this block's ECC is not
# clean" and until now the only way to find which page was to open 1104 of them.
# Measured on ce7/block3596, this is what the stripe is looking for: 12,144
# pages, of which 11,951 erased, 163 ecc-not-engaged, 25 ecc-corrected, 3 clean
# and two ecc-diverged.  Two needles in twelve thousand.
#
# Nine modes, two kinds, and which kind is on screen is stated in words every
# frame (Stripe.note), never left to be inferred from the picture:
#
#   per-PAGE   CONTENT   the five PageStates, from the raw store's index
#              ECC       the decoded store's recorded per-page decode status
#              MCRC      the same record, read for its MCRC claim
#   per-BLOCK  CELL WERU OWNER BIN ERASES DEFECT
#
# The per-block choice, and why it is not "grey the stripe out": a per-block
# mode paints the whole CE row with that block's value.  It is honest -- every
# page of the block really does carry that block's WERU / owner / erase count --
# and it is useful: the stripe becomes a twelve-CE comparison of one block's
# metadata, which no other view offers.  The one misreading it allows ("the
# colour varies along x") is prevented by one sentence in the footer, and
# throwing away a real view to avoid a misreading that a sentence prevents is a
# bad trade.  What a per-block colour may never do is paint a hole: the
# structural ABSENT cells (dead CE0, each row's tail past its own page count,
# and any block with no records at all) survive in every mode.
#
# The ECC/MCRC data is an index read of the decoded store (page_ref -> src.name,
# no bytes), which is why a 1104-page x 12-CE stripe of it costs 146 ms
# (measured, warm, block 3596) instead of the ~8 minutes 13k live decodes would.

#: Two stripe-only classes, because four recorded statuses do not map onto any
#: EccState and forcing them would be exactly the merge this project bans.
#:
#: Artefact -- `latched-repeat` (352,352 pages, 6.5% of the store) and
#: `erased-aus` (1).  These are not ECC outcomes at all: a latched-repeat page
#: is one page the capture returned at many addresses, so the store makes no
#: independent claim about the media there.  Painting them CLEAN would certify
#: 352k pages nobody decoded.  Deep indigo: dark and calm, because it is common,
#: and 47.6 dE from every other colour any mode can paint.
#:
#: NO_STATE -- `group2-failed` (0 records on this capture) and any status name a
#: future store introduces that this build has never heard of.  group2-failed's
#: own definition is ambiguous between PARTIAL and UNCORRECTABLE ("at least one
#: A10R codeword could not be resolved, OR a chunk digest still fails after
#: correction"), and an honest open question beats a rounded number.  Vivid
#: violet: rare, so it may shout, and not a shade of the failure
#: red -- "we cannot tell" is not "this page is bad".
STRIPE_ARTEFACT_RGBA: RGBA = (16, 0, 80, 255)     # 47.6 dE from the nearest
STRIPE_NOSTATE_RGBA: RGBA = (128, 0, 248, 255)    # 46.4

# The stripe's ECC class space is pagefmt.EccState extended, not replaced: codes
# 0..9 are the EccStates (asserted below, so a state added there cannot silently
# shift a colour here), and three codes follow.
_ECC_N = len(ECC_RGBA)
assert tuple(int(s) for s in pagefmt.ECC_STATES) == tuple(range(_ECC_N))

#: The decoded store holds no record for this page.  It is the hole colour, the
#: same one MCRC_NONE_RGBA uses for "nothing decoded here", because absence of
#: evidence must not look like weak evidence -- and it is not the slate, which
#: on this stripe means one specific thing (below).
STRIPE_ECC_NO_RECORD = _ECC_N
STRIPE_ECC_ARTEFACT = _ECC_N + 1
STRIPE_ECC_NO_STATE = _ECC_N + 2

STRIPE_ECC_RGBA: np.ndarray = np.vstack(
    [ECC_RGBA,
     np.array([ABSENT_RGBA, STRIPE_ARTEFACT_RGBA, STRIPE_NOSTATE_RGBA],
              dtype=np.uint8)]
).astype(np.uint8)

STRIPE_ECC_LABELS: tuple[str, ...] = tuple(
    pagefmt.ECC_SHORT[s] for s in pagefmt.ECC_STATES
) + ("no decoded record", "capture artefact", "status with no ECC state")

#: What the colour is keyed on, per class.  The first ten differ from the map's
#: _ECC_LEGEND_TAIL in one way that matters: on the stripe these come from the
#: decoded store's recorded status, not from a decode this session ran.
_STRIPE_ECC_TAIL = (
    "the per-page status probe has not landed yet -- nothing is claimed",
    "the store recorded that no decode was attempted for this page",
    "the LDPC modules could not be reached from here",
    "the store recorded this page as all-0x00: the zero vector IS a codeword",
    "the store recorded this page as erased: 0xFF is not data",
    "the store recorded a clean decode: no correction was needed",
    "the store recorded a corrected decode: the decoder converged",
    "the store recorded a partial decode: some codewords did not converge",
    "the store recorded that correction FAILED on this page",
    "the store recorded converged-but-MCRC-disagrees -- treat as a bug",
    "the decoded store has no record for this page: we never looked here, "
    "which is not the same as a clean one",
    "a capture artefact, not an ECC outcome: the store makes no claim about "
    "the media at this address (latched-repeat, erased-aus)",
    "the store recorded a status this build has no exact ECC state for -- "
    "naming the gap instead of rounding it into a verdict",
)

STRIPE_ECC_HELP: tuple[str, ...] = tuple(
    "%s\n\n%s" % (_STRIPE_ECC_TAIL[i], pagefmt.ECC_SENTENCE[pagefmt.EccState(i)])
    for i in range(_ECC_N)
) + tuple(_STRIPE_ECC_TAIL[_ECC_N:])

#: Visibility precedence for the stripe's x-reduction, worst/rarest first.
#: Filtered down to the EccStates it is exactly pagefmt.ECC_SEVERITY (asserted),
#: so the stripe and the map can never disagree about which outcome is more
#: serious -- the three stripe-only classes are inserted, never reordered.
STRIPE_ECC_SEVERITY: tuple[int, ...] = (
    int(pagefmt.EccState.MCRC_DISAGREES),
    int(pagefmt.EccState.UNCORRECTABLE),
    int(pagefmt.EccState.PARTIAL),
    STRIPE_ECC_NO_STATE,
    int(pagefmt.EccState.CORRECTED),
    int(pagefmt.EccState.UNAVAILABLE),
    STRIPE_ECC_NO_RECORD,
    STRIPE_ECC_ARTEFACT,
    int(pagefmt.EccState.CLEAN),
    int(pagefmt.EccState.ZERO_PAGE),
    int(pagefmt.EccState.ERASED),
    int(pagefmt.EccState.NOT_ATTEMPTED),
    int(pagefmt.EccState.NOT_MEASURED),
)
assert tuple(c for c in STRIPE_ECC_SEVERITY if c < _ECC_N) == tuple(
    int(s) for s in pagefmt.ECC_SEVERITY)

# MCRC on the stripe: the store's per-page record, read for its MCRC claim.
# Six classes, all six already in this file's vocabulary -- "no record" is the
# hole, "no verdict recorded" is the sentinel, and neither is the slate.
STRIPE_MCRC_UNKNOWN = 0        # the probe has not landed
STRIPE_MCRC_NO_RECORD = 1      # nothing in the decoded store for this page
STRIPE_MCRC_NO_VERDICT = 2     # a record that makes no MCRC claim
STRIPE_MCRC_CLEAN = 3
STRIPE_MCRC_FAILED = 4
STRIPE_MCRC_GEOM = 5           # decoded under the wrong geometry: Not a verdict

STRIPE_MCRC_RGBA: np.ndarray = np.array(
    [tuple(int(v) for v in STATE_RGBA[PageState.UNKNOWN]),
     ABSENT_RGBA, NONE_RGBA, MCRC_CLEAN_RGBA, MCRC_FAIL_RGBA, MCRC_GEOM_RGBA],
    dtype=np.uint8)

STRIPE_MCRC_LABELS: tuple[str, ...] = (
    "not probed", "no decoded record", "no MCRC verdict recorded",
    "MCRC clean", "MCRC failed", "wrong geometry (NOT a verdict)")

STRIPE_MCRC_HELP: tuple[str, ...] = (
    "the per-page status probe has not landed yet -- nothing is claimed",
    "the decoded store has no record for this page: we never looked here",
    "the store holds a record for this page whose status makes no claim "
    "about the MCRC (erased, latched-repeat, all-zero-retired, not read). "
    "That is a third verdict, and it is not a pass.",
    "the store's record for this page asserts every unit's MCRC/digest "
    "passed on the bytes it serves",
    "the store's record for this page asserts at least one unit FAILED",
    "fw-group2: a page here is FIVE chunks behind 4-byte digests, and the "
    "mounted store decoded this one under some other geometry, so its "
    "verdict is an artefact of the decoder and says nothing about the media",
)

#: Precedence, worst/rarest first.  Same rule as everywhere else in this file.
STRIPE_MCRC_SEVERITY: tuple[int, ...] = (
    STRIPE_MCRC_FAILED, STRIPE_MCRC_GEOM, STRIPE_MCRC_NO_RECORD,
    STRIPE_MCRC_NO_VERDICT, STRIPE_MCRC_CLEAN, STRIPE_MCRC_UNKNOWN)

#: The decoded store's own status vocabulary -> a stripe ECC class.  Keyed on
#: the status (the store's word for what it did), never on the source name, so
#: a store that renames a source keeps its meaning.  Names this table does not
#: know land in STRIPE_ECC_NO_STATE, which says exactly that.
STATUS_ECC_CLASS: dict[str, int] = {
    "clean": int(pagefmt.EccState.CLEAN),
    "group2-ok": int(pagefmt.EccState.CLEAN),
    "ecc-corrected": int(pagefmt.EccState.CORRECTED),
    "group2-corrected-parity": int(pagefmt.EccState.CORRECTED),
    "group2-corrected-payload": int(pagefmt.EccState.CORRECTED),
    # MEASURED, not assumed: ce7/b3596 p30 and p39 carry `ecc-diverged` in the
    # store and decode live to "partly corrected"; p10 and p13 carry
    # `ecc-not-engaged` and decode live to "correction FAILED".
    "ecc-diverged": int(pagefmt.EccState.PARTIAL),
    "ecc-partial": int(pagefmt.EccState.PARTIAL),
    "ecc-not-engaged": int(pagefmt.EccState.UNCORRECTABLE),
    "ecc-ok-mcrc-disagrees": int(pagefmt.EccState.MCRC_DISAGREES),
    "group2-mcrc-disagrees": int(pagefmt.EccState.MCRC_DISAGREES),
    "ecc-unattempted": int(pagefmt.EccState.NOT_ATTEMPTED),
    "not-read": int(pagefmt.EccState.NOT_ATTEMPTED),
    "unreadable": int(pagefmt.EccState.NOT_ATTEMPTED),
    "erased": int(pagefmt.EccState.ERASED),
    "all-zero-retired": int(pagefmt.EccState.ZERO_PAGE),
    "latched-repeat": STRIPE_ECC_ARTEFACT,
    "erased-aus": STRIPE_ECC_ARTEFACT,
    "group2-failed": STRIPE_ECC_NO_STATE,
    # decoded.store / decoded2.store's only verdict is an MCRC tier, which is
    # not an ECC measurement of anything.  Saying so is the honest answer;
    # calling it "clean" would report an MCRC pass as a zero syndrome.
    "mcrc-failed": STRIPE_ECC_NO_STATE,
}

#: Statuses whose meaning does not depend on the page GEOMETRY at all: a page
#: that was erased, latched or never read is that regardless of whether it is
#: four AUs or five chunks.  Every other ECC status is a verdict from a decoder
#: that ran under one specific geometry, so in the fw-group2 region only the
#: group2-* ones apply -- see _status_ecc_lut.
_GEOMETRY_FREE = frozenset({"erased", "erased-aus", "all-zero-retired",
                            "latched-repeat", "not-read", "unreadable",
                            "ecc-unattempted"})


def _status_ecc_lut(names, statuses, group2: bool) -> np.ndarray:
    """name index -> stripe ECC class.  names[0] is "no record", always.

    `group2` builds the variant for a CE row in the fw-group2 region: an EE3
    ECC verdict is a verdict about four AUs behind 6-byte MCRCs, and a group-2
    page is five chunks behind 4-byte digests, so an ECC status that is neither
    a group2-* one nor geometry-free is not a verdict about this page.  It lands
    in NO_STATE, which names the gap, rather than being painted as a decode.
    """
    n = max(1, len(names))
    out = np.full(n, STRIPE_ECC_NO_STATE, dtype=np.uint8)
    out[0] = STRIPE_ECC_NO_RECORD
    for i in range(1, n):
        status = str(statuses[i] if i < len(statuses) else "").strip().lower()
        if not status:
            # No vocabulary entry: the store records a tier, not a decode
            # status (decoded.store and decoded2.store are exactly this).  An
            # MCRC tier is not an ECC measurement, so it says so.
            continue
        cls = STATUS_ECC_CLASS.get(status)
        if cls is None:
            continue
        if (group2 and status not in _GEOMETRY_FREE
                and not status.startswith("group2")):
            continue
        out[i] = np.uint8(cls)
    return out


def _status_mcrc_lut(names, statuses, group2: bool) -> np.ndarray:
    """name index -> stripe MCRC class.  Uses the STORE's own status words.

    The two-name fallback is what the old stores need: they record the verdict
    in which tier a record lives, and their two source names are exactly
    `decoded` (all four AUs passed) and `decoded-mcrc-failed` (at least one
    failed).  model._MCRC_TRUE/_MCRC_FALSE is the same table fetch_decoded
    reads, so the stripe and the page inspector cannot disagree.
    """
    from .model import _MCRC_FALSE, _MCRC_TRUE     # local: pure data, no I/O
    n = max(1, len(names))
    out = np.full(n, STRIPE_MCRC_NO_VERDICT, dtype=np.uint8)
    out[0] = STRIPE_MCRC_NO_RECORD
    for i in range(1, n):
        status = str(statuses[i] if i < len(statuses) else "").strip().lower()
        name = str(names[i]).strip().lower()
        if status:
            cls = (STRIPE_MCRC_CLEAN if status in _MCRC_TRUE else
                   STRIPE_MCRC_FAILED if status in _MCRC_FALSE else
                   STRIPE_MCRC_NO_VERDICT)
        elif name == "decoded":
            cls, status = STRIPE_MCRC_CLEAN, "clean"
        elif name == "decoded-mcrc-failed":
            cls, status = STRIPE_MCRC_FAILED, "mcrc-failed"
        else:
            cls = STRIPE_MCRC_NO_VERDICT
        if (group2 and cls != STRIPE_MCRC_NO_VERDICT
                and not status.startswith("group2")):
            # The mounted store checked this page under a geometry it does not
            # have.  Measured: true for all 1,840 group-2 pages of
            # decoded.store, and false for every page of decoded3.store, which
            # decoded that region as five chunks.
            cls = STRIPE_MCRC_GEOM
        out[i] = np.uint8(cls)
    return out


def _invert_ranks(order: tuple[int, ...], n: int) -> tuple[np.ndarray, np.ndarray]:
    """(severity order, class count) -> (rank LUT, inverse LUT).

    The reduction in blockview takes np.maximum over ranks and maps the winner
    back, so the ranks must be a permutation: two classes sharing a rank would
    make the inverse pick one of them arbitrarily and the picture would report a
    class no page in that pixel has.
    """
    ranks = np.zeros(n, dtype=np.uint8)
    for i, code in enumerate(order):
        ranks[int(code)] = np.uint8(n - 1 - i)
    unrank = np.zeros(n, dtype=np.uint8)
    for code in range(n):
        unrank[int(ranks[code])] = np.uint8(code)
    return ranks, unrank


#: PageState visibility precedence, worst/rarest first -- the CONTENT mode's
#: rank, shared with blockview so there is one ordering, not two.
STATE_SEVERITY: tuple[int, ...] = (
    int(PageState.SKIPPED), int(PageState.ABSENT), int(PageState.ZERO),
    int(PageState.PROGRAMMED), int(PageState.UNKNOWN), int(PageState.ERASED))


@dataclasses.dataclass(frozen=True)
class Stripe:
    """What the block stripe draws for one mode: codes, colours, and words.

    `codes` is uint8[n_ce, n_pages] indexing `table`/`labels`/`help`/`ranks`.
    Nothing here is a colour until `table[codes]`, so the precedence reduction
    still runs on class codes and can never average two classes into a third.
    """

    mode: Mode
    codes: np.ndarray
    table: np.ndarray                    # uint8[k, 4]
    labels: tuple[str, ...]
    help: tuple[str, ...]
    ranks: np.ndarray                    # uint8[k], highest wins a pixel
    unranks: np.ndarray                  # uint8[k], the inverse of `ranks`
    counts: np.ndarray                   # int64[k], exact, over the whole array
    per_block: bool
    ready: bool                          # False: the datum is not here yet
    note: str                            # the footer sentence, drawn always
    source: str = ""                     # provenance, for ECC/MCRC

    def colors(self, codes: np.ndarray | None = None) -> np.ndarray:
        """codes -> uint8[..., 4].  One np.take, clipped rather than raising."""
        idx = np.asarray(self.codes if codes is None else codes)
        idx = np.clip(idx.astype(np.intp), 0, self.table.shape[0] - 1)
        return np.ascontiguousarray(self.table[idx])


def _pending_stripe(mode: Mode, states: np.ndarray, note: str,
                    why: str, source: str = "") -> Stripe:
    """The honest stripe for a mode whose per-page datum is not available.

    Two classes only: the structural hole, and "not probed".  A mode with no
    data renders as not probed and never as a state that means something else --
    which is the whole reason this function exists rather than a fall-through to
    the PageState colours.
    """
    states = np.asarray(states)
    codes = np.where(states == int(PageState.ABSENT), np.uint8(0),
                     np.uint8(1)).astype(np.uint8)
    table = np.array([ABSENT_RGBA,
                      tuple(int(v) for v in STATE_RGBA[PageState.UNKNOWN])],
                     dtype=np.uint8)
    labels = ("absent (no record)", "not probed")
    helps = ("no record at all -- we never looked (a hole, not clean media)",
             why)
    # The hole wins a shared pixel, exactly as ABSENT beats UNKNOWN in CONTENT.
    ranks, unranks = _invert_ranks((0, 1), 2)
    counts = np.bincount(codes.ravel(), minlength=2).astype(np.int64)
    return Stripe(mode=mode, codes=codes, table=table, labels=labels,
                  help=helps, ranks=ranks, unranks=unranks, counts=counts,
                  per_block=False, ready=False, note=note, source=source)


def _block_column(summary: Summary, mode: Mode, block: int,
                  n_ce: int = N_CE) -> tuple[np.ndarray, tuple[str, ...],
                                             tuple[str, ...]] | None:
    """(uint8[n_ce, 4], per-CE label, per-CE help) for a per-BLOCK mode.

    The colours are computed exactly as colorize() computes them for the map --
    same hash, same ramp range (drive-wide, so a stripe's colours mean the same
    thing as the map's), same sentinels -- so a block that is one colour on the
    map is that colour on the stripe.
    """
    try:
        idx = np.arange(n_ce, dtype=np.int64) * N_BLOCK + int(block)
        idx = idx[idx < int(summary.pages.shape[0])]
        if idx.shape[0] < n_ce:
            return None
    except Exception:                                        # noqa: BLE001
        return None

    if mode == Mode.CELL:
        slc = summary.slc[idx].astype(bool)
        rgba = np.where(slc[:, None], np.array(CELL_SLC_RGBA, dtype=np.uint8),
                        np.array(CELL_TLC_RGBA, dtype=np.uint8)).astype(np.uint8)
        labels = tuple("pseudo-SLC" if s else "native TLC" for s in slc)
        helps = tuple("%d pages: block programmed %s"
                      % (PAGES_SLC if s else PAGES_TLC,
                         "one bit per cell" if s else "three bits per cell")
                      for s in slc)
        return rgba, labels, helps

    if mode == Mode.WERU:
        col = summary.weru[idx].astype(np.int64)
        rgba = hash_colors(col, sentinel=-1)
        labels = tuple(("weru %d" % v) if v >= 0 else "no WERU" for v in col)
        helps = tuple("wear/erase unit id from the WERU table; the colour is a "
                      "stable hash of the id, not an ordering" for _ in col)
        return rgba, labels, helps

    if mode == Mode.OWNER:
        col = summary.owner[idx].astype(np.int64)
        rgba = hash_colors(col, sentinel=-1)
        labels = tuple(msonames.mso_label(int(v)) if v >= 0 else "no owner"
                       for v in col)
        helps = tuple("owning MSO from the WERU table; -1 is unowned or "
                      "unknown, not owner 0" for _ in col)
        return rgba, labels, helps

    if mode == Mode.BIN:
        col = _bin_ids(summary)[idx]
        rgba = hash_colors(col, sentinel=-1)
        labels = tuple(_bin_name(summary, int(v)) if v >= 0
                       else "in no bin" for v in col)
        helps = tuple("WERU bin id; %d and -1 both mean 'in no bin'"
                      % WERU_BIN_COUNT for _ in col)
        return rgba, labels, helps

    if mode == Mode.ERASES:
        # The drive-wide range, not this block's twelve values: a ramp rescaled
        # per block would give the same colour two different meanings on two
        # blocks, and the map's legend would stop describing the stripe.
        lo, hi = _erase_range(summary.erases, ~_hole_mask(summary))
        col = summary.erases[idx].astype(np.int64)
        rgba = ramp(col, lo, hi, unknown=-1)
        labels = tuple(("%d erases" % v) if v >= 0 else "unknown erase count"
                       for v in col)
        helps = tuple("ramp clipped at the drive-wide 2nd/98th percentile "
                      "(%d..%d); -1 is 'we do not know', which is NOT zero"
                      % (int(lo), int(hi)) for _ in col)
        return rgba, labels, helps

    if mode == Mode.DEFECT:
        col = summary.defect[idx].astype(np.int64)
        rgba = _defect_colors(col)
        labels = tuple(_status_name(summary, int(v)) for v in col)
        helps = tuple("defect / status code %d from the WERU table" % int(v)
                      for v in col)
        return rgba, labels, helps

    return None


#: Which modes need the decoded store's per-page status probe.
STRIPE_STATUS_MODES: tuple[Mode, ...] = (Mode.ECC, Mode.MCRC)

#: Which modes are a property of the BLOCK, not of the page.
STRIPE_BLOCK_MODES: tuple[Mode, ...] = (Mode.CELL, Mode.WERU, Mode.OWNER,
                                        Mode.BIN, Mode.ERASES, Mode.DEFECT)

_PER_BLOCK_NOTE = (
    "%s is a per-BLOCK property: all %d rows are one colour each, one value "
    "per CE. It does not vary by page -- the stripe is a %d-CE comparison of "
    "this block's metadata, and the holes (dead CE0, and each row's tail past "
    "its own page count) are still holes.")


def stripe(mode: Mode, states: np.ndarray, *, status=None,
           summary: Summary | None = None, block: int = 0,
           group2: np.ndarray | None = None) -> Stripe:
    """Build what the stripe draws for `mode`.  Pure numpy; no I/O.

    `states` is the PageState array from model.probe_block_stripe (the shape of
    the stripe is always its shape).  `status` is a model.BlockStatus from
    probe_block_status, needed only by the ECC and MCRC modes; `group2` is
    bool[n_ce], true where this (ce, block) is in the fw-group2 region.

    A mode whose datum is missing comes back with ready=False and a note that
    says which datum -- never with a colour borrowed from another mode.
    """
    mode = Mode(mode)
    states = np.ascontiguousarray(np.asarray(states), dtype=np.uint8)
    if states.ndim != 2:
        states = states.reshape(1, -1)
    n_ce, n_pages = states.shape

    if mode == Mode.CONTENT:
        ranks, unranks = _invert_ranks(STATE_SEVERITY, STATE_RGBA.shape[0])
        counts = np.bincount(states.ravel(),
                             minlength=STATE_RGBA.shape[0]).astype(np.int64)
        return Stripe(
            mode=mode, codes=states, table=np.asarray(STATE_RGBA),
            labels=STATE_LABELS, help=STATE_HELP, ranks=ranks,
            unranks=unranks, counts=counts, per_block=False, ready=True,
            note="page states from the RAW store's index: existence and length "
                 "only, no bytes read.",
            source="all.store index (page_ref per page, no blob reads)")

    if mode in STRIPE_BLOCK_MODES:
        column = None if summary is None else _block_column(summary, mode,
                                                            block, n_ce)
        if column is None:
            return _pending_stripe(
                mode, states,
                "%s comes from summary.npz, which is not loaded: the stripe "
                "shows the page states' holes and nothing else."
                % MODE_LABELS[mode],
                "no summary.npz, so this block's %s is unknown here"
                % MODE_LABELS[mode])
        rgba, labels, helps = column
        # A block with no records at all is a hole in every mode -- the same
        # rule colorize() applies to the map. The metadata may well claim an
        # owner for a block nobody ever read; painting that claim as if it were
        # an observation is how a metadata-driven skip nearly lost the boot
        # block.
        hole_row = np.zeros(n_ce, dtype=bool)
        try:
            hole = _hole_mask(summary)
            idx = np.arange(n_ce, dtype=np.int64) * N_BLOCK + int(block)
            hole_row = hole[idx]
        except Exception:                                    # noqa: BLE001
            pass
        hole_row[0] = True                       # CE0 is dead and absent
        # grouped by value, not one class per CE.  Twelve CEs of one block
        # normally carry one value (block 3596: weru 576 on all eleven live
        # rows), so a class per CE would print the same number twelve times --
        # and with the owner names that is 226 px of text, measured, which
        # pushed CE11 under a scrollbar at 1024x700. Grouping also says
        # something the per-CE form does not: Which CEs differ, which is the
        # comparison this mode exists to make.
        groups: dict[str, int] = {}
        row_code = np.zeros(n_ce, dtype=np.uint8)
        group_ces: list[list[int]] = []
        group_rgba: list[np.ndarray] = []
        group_label: list[str] = []
        group_help: list[str] = []
        for ce in range(n_ce):
            if hole_row[ce]:
                continue
            key = "%s|%s" % (labels[ce],
                             ",".join(str(int(v)) for v in rgba[ce]))
            code = groups.get(key)
            if code is None:
                code = len(group_label) + 1
                groups[key] = code
                group_ces.append([])
                group_rgba.append(np.asarray(rgba[ce], dtype=np.uint8))
                group_label.append(str(labels[ce]))
                group_help.append(str(helps[ce]))
            row_code[ce] = np.uint8(code)
            group_ces[code - 1].append(ce)
        codes = np.tile(row_code[:, None], (1, n_pages))
        codes[states == int(PageState.ABSENT)] = 0
        rows = [np.array([ABSENT_RGBA], dtype=np.uint8)]
        rows += [c.reshape(1, 4) for c in group_rgba]
        table = np.vstack(rows).astype(np.uint8)
        all_labels = ("absent (no record)",) + tuple(group_label)
        all_help = ("no record at all -- we never looked (a hole, not clean "
                    "media); this is structural and survives every mode",) + \
            tuple("block %d on CE %s: %s"
                  % (block, ", ".join(str(c) for c in group_ces[i]),
                     group_help[i]) for i in range(len(group_label)))
        # The hole beats a value colour where several pages share a pixel: a
        # hole hiding inside a run of metadata colour is the same loss as a
        # skipped page hiding inside programmed media.
        k = len(all_labels)
        ranks, unranks = _invert_ranks(tuple(range(k)), k)
        counts = np.bincount(codes.ravel(), minlength=k).astype(np.int64)
        return Stripe(
            mode=mode, codes=codes, table=table, labels=all_labels,
            help=all_help, ranks=ranks, unranks=unranks, counts=counts,
            per_block=True, ready=True,
            note=_PER_BLOCK_NOTE % (MODE_LABELS[mode], n_ce, n_ce),
            source="summary.npz row ce*%d+%d" % (N_BLOCK, block))

    if mode in STRIPE_STATUS_MODES:
        return _status_stripe(mode, states, status, group2, block)

    return _pending_stripe(mode, states,
                           "no stripe rendering is defined for %s."
                           % MODE_LABELS.get(mode, str(mode)),
                           "this mode has no per-page datum here")


def _status_stripe(mode: Mode, states: np.ndarray, status,
                   group2: np.ndarray | None, block: int) -> Stripe:
    """The ECC / MCRC stripe, from the decoded store's recorded status."""
    label = MODE_LABELS[mode]
    if status is None:
        return _pending_stripe(
            mode, states,
            "%s per page comes from the decoded store's recorded status; the "
            "probe has not landed yet." % label,
            "the decoded store's per-page status has not been read yet")
    error = getattr(status, "error", None)
    codes_in = getattr(status, "codes", None)
    if error or codes_in is None:
        return _pending_stripe(
            mode, states,
            "%s per page is unavailable: %s" % (label, error or "no data"),
            "the decoded store could not answer for this block: %s"
            % (error or "no data"), source=str(getattr(status, "path", "")))

    codes_in = np.asarray(codes_in)
    if codes_in.shape != states.shape:
        # Never resized, never padded: a one-row or one-column shift is a
        # stripe that is wrong everywhere and looks completely fine.
        return _pending_stripe(
            mode, states,
            "%s per page was DISCARDED: the decoded store answers for a %s "
            "block and this stripe is %s. The two stores disagree about this "
            "block's geometry, so no page of it can be lined up." %
            (label, "x".join(str(v) for v in codes_in.shape),
             "x".join(str(v) for v in states.shape)),
            "the decoded store's geometry for this block does not match the "
            "raw store's, so its statuses cannot be aligned to these pages",
            source=str(getattr(status, "path", "")))

    names = tuple(getattr(status, "names", ()) or ("",))
    statuses = tuple(getattr(status, "statuses", ()) or ())
    n_ce = states.shape[0]
    g2 = np.zeros(n_ce, dtype=bool)
    if group2 is not None:
        g2v = np.asarray(group2, dtype=bool).ravel()
        if g2v.shape[0] >= n_ce:
            g2 = g2v[:n_ce]

    if mode == Mode.ECC:
        lut_n = _status_ecc_lut(names, statuses, False)
        lut_g = _status_ecc_lut(names, statuses, True)
        table, labels, helps = (STRIPE_ECC_RGBA, STRIPE_ECC_LABELS,
                                STRIPE_ECC_HELP)
        severity = STRIPE_ECC_SEVERITY
        note = ("ECC per page is the decoded store's OWN RECORDED decode "
                "status, one index read per page. The drive map's ECC colour "
                "is a different measurement -- a log of what THIS SESSION "
                "decoded -- so the two may differ, and neither is derived "
                "from the other.")
    else:
        lut_n = _status_mcrc_lut(names, statuses, False)
        lut_g = _status_mcrc_lut(names, statuses, True)
        table, labels, helps = (STRIPE_MCRC_RGBA, STRIPE_MCRC_LABELS,
                                STRIPE_MCRC_HELP)
        severity = STRIPE_MCRC_SEVERITY
        note = ("MCRC per page is the decoded store's own record. The drive "
                "map's MCRC colour is summary.npz's tally of the EARLIER "
                "decode (decoded.store, whole drive as 4 AUs x 6-byte MCRC), "
                "so on a fw-group2 block the map and this stripe can disagree "
                "-- and here the stripe has the better evidence.")

    # Clipped, not trusted: a code past the end of the name table would be an
    # IndexError inside a frame, and the honest answer for one is "no record".
    safe = np.clip(codes_in.astype(np.intp), 0, lut_n.shape[0] - 1)
    codes = np.where(g2[:, None], lut_g[safe], lut_n[safe])
    codes = np.ascontiguousarray(codes, dtype=np.uint8)
    # A page the RAW store has no record for cannot have a decoded one either,
    # and it is a hole in every mode: assert it rather than trust two indexes
    # to agree. (They did agree on every block measured, which is why this is
    # cheap insurance and not a workaround.)
    no_record = (STRIPE_ECC_NO_RECORD if mode == Mode.ECC
                 else STRIPE_MCRC_NO_RECORD)
    codes[states == int(PageState.ABSENT)] = np.uint8(no_record)
    ranks, unranks = _invert_ranks(severity, table.shape[0])
    counts = np.bincount(codes.ravel(),
                         minlength=table.shape[0]).astype(np.int64)
    path = str(getattr(status, "path", "") or "the decoded store")
    return Stripe(mode=mode, codes=codes, table=np.asarray(table),
                  labels=labels, help=helps, ranks=ranks, unranks=unranks,
                  counts=counts, per_block=False, ready=True, note=note,
                  source="per-page status recorded in %s (index read, no "
                         "bytes)" % path)


def stripe_legend(st: Stripe | None) -> list[LegendEntry]:
    """The legend for what the stripe is showing, with exact counts.

    Every class is listed at its count including zero -- the same rule the map's
    legend follows, and for the same reason: a class that can disappear from
    the legend is a class the reader stops looking for, and the two that would
    vanish first here are `partly corrected` and `correction FAILED`.
    """
    if st is None:
        return []
    out: list[LegendEntry] = []
    for i in range(st.table.shape[0]):
        count = int(st.counts[i]) if i < st.counts.shape[0] else 0
        out.append(LegendEntry(st.labels[i], _rgba_of(st.table[i]), count,
                               st.help[i]))
    return out


# --------------------------------------------------------------------------
# legends
# --------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class LegendEntry:
    label: str
    rgba: RGBA
    count: int  # blocks in this class, -1 for a continuous ramp
    help: str


def _rgba_of(row: np.ndarray) -> RGBA:
    return (int(row[0]), int(row[1]), int(row[2]), int(row[3]))


def state_legend() -> list[LegendEntry]:
    """The five states + 'not probed', with no counts.

    app.py pins this under every mode: the page-state vocabulary is on screen
    at all times, so a reader is never in a mode where 'skipped' has quietly
    stopped existing.
    """
    return [
        LegendEntry(STATE_LABELS[i], _rgba_of(STATE_RGBA[i]), -1, STATE_HELP[i])
        for i in range(STATE_RGBA.shape[0])
    ]


def _counts(values: np.ndarray) -> dict[int, int]:
    uniq, cnt = np.unique(values, return_counts=True)
    return {int(u): int(c) for u, c in zip(uniq, cnt)}


def _status_name(summary: Summary, code: int) -> str:
    """Name for a defect code, from status_names in either shape it may take."""
    names = summary.status_names
    try:
        if names is None or len(names) == 0:
            return f"defect {code}"
        if len(names) == N_ROWS:
            hits = np.nonzero(summary.defect.astype(np.int64) == code)[0]
            if hits.size:
                value = names[int(hits[0])]
                if value:
                    return str(value)
            return f"defect {code}"
        if 0 <= code < len(names):
            value = names[code]
            if value:
                return str(value)
    except Exception:  # a legend must never take a frame down
        pass
    return f"defect {code}"


def _bin_name(summary: Summary, bin_id: int) -> str:
    names = summary.bin_names
    try:
        if names is not None and 0 <= bin_id < len(names) and names[bin_id]:
            return f"{bin_id}: {names[bin_id]}"
    except Exception:
        pass
    return f"bin {bin_id}"


def legend(summary: Summary, mode: Mode,
           ecc: np.ndarray | None = None) -> list[LegendEntry]:
    """The legend for the CURRENT mode.

    Counts are taken over the blocks the picture actually colours by that mode.
    colorize() paints every block with no records as a hole regardless of mode,
    so a legend that tallied those blocks under "native TLC" or "has a WERU"
    would be a number contradicting the pixels above it -- and CE0's 4000
    recordless blocks do carry a full set of WERU / owner / erase-count claims,
    so the contradiction is not hypothetical.  The hole count gets its own
    trailing row instead, in every non-CONTENT mode.
    """
    live = ~_hole_mask(summary)
    entries = _mode_legend(summary, mode, live, ecc)
    if mode != Mode.CONTENT:
        n_hole = int((~live).sum())
        entries.append(
            LegendEntry(
                "no records (painted as a hole)",
                ABSENT_RGBA,
                n_hole,
                "no page of this block is in the store -- we never looked. "
                "These blocks are drawn as holes in every mode, so they are "
                "counted here and not under any class above.",
            )
        )
    return entries


def _mode_legend(summary: Summary, mode: Mode, live: np.ndarray,
                 ecc: np.ndarray | None = None) -> list[LegendEntry]:
    if mode == Mode.CONTENT:
        states = block_states(summary)
        counts = _counts(states)
        # Every one of the five is listed even at count 0: a state that can
        # disappear from the legend can disappear from the reader's thinking.
        out = [
            LegendEntry(
                STATE_LABELS[i],
                _rgba_of(STATE_RGBA[i]),
                counts.get(i, 0),
                STATE_HELP[i],
            )
            for i in range(5)
        ]
        out.append(
            LegendEntry(
                "mixed (stippled)",
                # The colour of the tick mapview draws, not a page state: this
                # row keys a marker, and borrowing the "not probed" slate for it
                # gave one colour two meanings in two legends drawn a few pixels
                # apart.
                STIPPLE_RGBA,
                int(mixed_mask(summary).sum()),
                "white diagonal tick over the block: it holds more than one "
                "state; the colour underneath is the rarest one, not the whole "
                "story",
            )
        )
        return out

    if mode == Mode.CELL:
        slc = summary.slc.astype(bool)
        return [
            LegendEntry(
                "pseudo-SLC (368 pages)",
                CELL_SLC_RGBA,
                int((slc & live).sum()),
                "block programmed one bit per cell",
            ),
            LegendEntry(
                "native TLC (1104 pages)",
                CELL_TLC_RGBA,
                int((~slc & live).sum()),
                "block programmed three bits per cell",
            ),
        ]

    if mode == Mode.WERU:
        weru = summary.weru
        has = (weru >= 0) & live
        n_weru = int(np.unique(weru[has]).size) if has.any() else 0
        return [
            LegendEntry(
                f"{n_weru} WERUs (hue = hashed id)",
                _rgba_of(hash_colors(np.array([0], dtype=np.int64))[0]),
                int(has.sum()),
                "colour is a stable hash of the WERU id, not an ordering",
            ),
            LegendEntry(
                "no WERU",
                NONE_RGBA,
                int((~has & live).sum()),
                "block belongs to no wear/erase unit in the table",
            ),
        ]

    if mode == Mode.OWNER:
        owner = summary.owner
        counts = _counts(owner[(owner >= 0) & live])
        # One sort, one position map: the ordering of `keys` is what hash_colors
        # was fed, so pos[] indexes `colors` directly.
        keys = sorted(counts)
        pos = {k: i for i, k in enumerate(keys)}
        colors = hash_colors(np.array(keys, dtype=np.int64)) if counts else None
        out: list[LegendEntry] = []
        for oid in sorted(counts, key=lambda k: -counts[k])[:24]:
            rgba = _rgba_of(colors[pos[oid]]) if colors is not None else NONE_RGBA
            # Name the partition. "owner 44" is unreadable; the same number is
            # Migration/MediaSpace1-NSID2, which holds 37,920 of this drive's
            # 48,000 blocks. The names come from the embedded msoIdTable, a
            # firmware constant, so this costs nothing and cannot go stale.
            out.append(
                LegendEntry(
                    msonames.mso_label(oid),
                    rgba,
                    counts[oid],
                    "owning MSO from the WERU table"
                    + ("" if msonames.mso_name(oid)
                       else "; the static table does not assign this id"),
                )
            )
        out.append(
            LegendEntry(
                "no owner",
                NONE_RGBA,
                int(((owner < 0) & live).sum()),
                "owner == -1: unowned or unknown, not owner 0",
            )
        )
        return out

    if mode == Mode.BIN:
        bins = _bin_ids(summary)
        counts = _counts(bins[(bins >= 0) & live])
        ordered = sorted(counts, key=lambda k: -counts[k])
        keys = sorted(counts)
        pos = {k: i for i, k in enumerate(keys)}
        colors = hash_colors(np.array(keys, dtype=np.int64)) if counts else None
        out = []
        for bid in ordered[:16]:
            rgba = _rgba_of(colors[pos[bid]]) if colors is not None else NONE_RGBA
            out.append(LegendEntry(_bin_name(summary, bid), rgba, counts[bid], "WERU bin"))
        rest = sum(counts[b] for b in ordered[16:])
        if rest:
            out.append(
                LegendEntry(
                    f"{len(ordered) - 16} more bins",
                    # Not a page-state colour: this row keys "several hues", so
                    # it gets the neutral, never a state swatch.
                    NEUTRAL_RGBA,
                    int(rest),
                    "smaller bins, each still drawn with its own hashed hue",
                )
            )
        out.append(
            LegendEntry(
                f"in no bin (-1 or {WERU_BIN_COUNT})",
                NONE_RGBA,
                int(((bins < 0) & live).sum()),
                f"bin id {WERU_BIN_COUNT} is the 'no bin' sentinel, not a real bin",
            )
        )
        return out

    if mode == Mode.ERASES:
        erases = summary.erases
        lo, hi = _erase_range(erases, live)
        known = erases[(erases >= 0) & live]
        return [
            LegendEntry(
                f"{int(lo)} erases (low)",
                _rgba_of(ramp(np.array([lo]), lo, hi)[0]),
                -1,
                "low end of the erase-count ramp",
            ),
            LegendEntry(
                f"{int((lo + hi) / 2)} erases",
                _rgba_of(ramp(np.array([(lo + hi) / 2]), lo, hi)[0]),
                -1,
                "mid ramp",
            ),
            LegendEntry(
                f"{int(hi)} erases (high)",
                _rgba_of(ramp(np.array([hi]), lo, hi)[0]),
                -1,
                f"ramp clipped at the 2nd/98th percentile of {known.size} known counts",
            ),
            LegendEntry(
                "unknown erase count",
                NONE_RGBA,
                int(((erases < 0) & live).sum()),
                "erases == -1: we do not know, which is NOT the same as zero",
            ),
        ]

    if mode == Mode.DEFECT:
        defect = summary.defect.astype(np.int64)
        counts = _counts(defect[live])
        out = []
        for code in sorted(counts):
            if code < DEFECT_RGBA.shape[0]:
                rgba = _rgba_of(DEFECT_RGBA[code])
            else:
                rgba = _rgba_of(hash_colors(np.array([code], dtype=np.int64))[0])
            out.append(
                LegendEntry(
                    _status_name(summary, code), rgba, counts[code], f"defect / status code {code}"
                )
            )
        return out

    if mode == Mode.MCRC:
        cls = _mcrc_class(summary)
        counts = _counts(cls[live])
        labels = (
            ("no decoded pages", "nothing in decoded.store for this block -- absence of evidence"),
            ("all AUs clean", "every decoded page passed MCRC"),
            ("mixed", "this block has both clean and failed decoded pages"),
            ("all failed", "every decoded page failed MCRC (suspect tier)"),
            ("decoded under the wrong geometry (NOT a verdict)",
             "fw-group2: a page here is FIVE chunks with a 4-byte digest each, "
             "but decoded.store decoded the whole drive as 4 AUs x 6-byte "
             "MCRC. Every page of these blocks is quarantined by construction, "
             "so the store's tally says nothing about the media -- measured, "
             "1835 of these 1840 pages verify 5/5 under the region's own "
             "checker. Open a page for the five-chunk verdict."),
        )
        return [
            LegendEntry(labels[i][0], _rgba_of(_MCRC_TABLE[i]), counts.get(i, 0), labels[i][1])
            for i in range(5)
        ]

    if mode == Mode.ECC:
        states = ecc_states(ecc, int(summary.pages.shape[0]))
        counts = _counts(states[live])
        # Every state gets a row, at count 0 as well.  A state that can
        # disappear from the legend is a state the reader stops looking for --
        # and the two that would vanish first here are the two this whole panel
        # exists for: "correction FAILED" and "converged, MCRC disagrees".
        # The label is the short name only: the legend is a 3-4 column table
        # and a long label is clipped by the column, which would silently trim
        # the one word that distinguishes two states.  What the colour is keyed
        # on leads the tooltip instead.
        return [
            LegendEntry(
                pagefmt.ECC_SHORT[pagefmt.EccState(i)],
                _rgba_of(ECC_RGBA[i]),
                counts.get(i, 0),
                "%s\n\n%s" % (_ECC_LEGEND_TAIL[i],
                              pagefmt.ECC_SENTENCE[pagefmt.EccState(i)]),
            )
            for i in range(ECC_RGBA.shape[0])
        ]

    return []


#: The half-line after each ECC state name in the legend: what the colour is
#: keyed on, not what the state means (the tooltip carries the meaning).
_ECC_LEGEND_TAIL = (
    "no ECC decode has been run over this block",
    "measured, but no page carried bytes to decode",
    "the LDPC modules could not be reached from here",
    "all-0x00 pages: the zero vector IS a codeword, so ECC says nothing",
    "erased pages: 0xFF is not data",
    "every syndrome 0 before correction; nothing to fix",
    "nonzero syndrome, all codewords converged, MCRC agrees after",
    "some codewords converged and some did not",
    "the decoder ran on every codeword and converged on none",
    "converged yet an MCRC still fails -- never observed; treat as a bug",
)


def imgui_color(rgba: RGBA) -> tuple[float, float, float, float]:
    """0..255 ints -> 0..1 floats for imgui swatches."""
    r, g, b, a = rgba
    return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)


# --------------------------------------------------------------------------
# the separation invariant, as a runnable check
# --------------------------------------------------------------------------
def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """uint8/float rgb (..., 3) -> CIELab (..., 3), D65."""
    c = np.asarray(rgb, dtype=np.float64)[..., :3] / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ]
    )
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    d = 6.0 / 29.0
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4.0 / 29.0)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def _check_mode_color_separation() -> list[str]:
    """-> a list of violation sentences; empty means the invariant holds.

    Every colour a colour mode can paint is checked against every STATE_RGBA
    entry and against NONE_RGBA.  The ramp is sampled densely, not just at its
    control points, because ramp() interpolates between them and an
    intermediate value is just as visible as a stop.

    app.py's --self-test calls this, so a colour edited to look nicer cannot
    quietly reintroduce "the majority of the drive is painted the colour the
    pinned legend calls erased".
    """
    targets = np.vstack(
        [np.asarray(STATE_RGBA, dtype=np.float64)[:, :3],
         np.array([NONE_RGBA[:3]], dtype=np.float64)]
    )
    target_names = list(STATE_LABELS) + ["none/sentinel"]
    target_lab = _srgb_to_lab(targets)

    samples: list[tuple[str, np.ndarray]] = [
        ("CELL_SLC_RGBA", np.array(CELL_SLC_RGBA[:3], dtype=np.float64)),
        ("CELL_TLC_RGBA", np.array(CELL_TLC_RGBA[:3], dtype=np.float64)),
        ("MCRC_CLEAN_RGBA", np.array(MCRC_CLEAN_RGBA[:3], dtype=np.float64)),
        ("MCRC_MIXED_RGBA", np.array(MCRC_MIXED_RGBA[:3], dtype=np.float64)),
        ("MCRC_FAIL_RGBA", np.array(MCRC_FAIL_RGBA[:3], dtype=np.float64)),
        ("MCRC_GEOM_RGBA", np.array(MCRC_GEOM_RGBA[:3], dtype=np.float64)),
        # The three colours the ECC mode introduces.  The other seven rows of
        # ECC_RGBA are deliberate reuses of colours that already mean the same
        # thing (see the table's comment), so they are excluded here exactly as
        # MCRC_NONE_RGBA is -- an intended identity is not a collision.
        ("ECC_UNAVAILABLE_RGBA",
         np.array(ECC_UNAVAILABLE_RGBA[:3], dtype=np.float64)),
        ("ECC_CORRECTED_RGBA",
         np.array(ECC_CORRECTED_RGBA[:3], dtype=np.float64)),
        ("ECC_FLAG_RGBA", np.array(ECC_FLAG_RGBA[:3], dtype=np.float64)),
        # The two colours the BLOCK stripe introduces.  They are on screen next
        # to the pinned five-state legend exactly as the map's are, so they are
        # held to the same bar -- and the stripe puts them in one legend with
        # all ten ECC colours, which the pairwise loop below covers.
        ("STRIPE_ARTEFACT_RGBA",
         np.array(STRIPE_ARTEFACT_RGBA[:3], dtype=np.float64)),
        ("STRIPE_NOSTATE_RGBA",
         np.array(STRIPE_NOSTATE_RGBA[:3], dtype=np.float64)),
    ]
    for i in range(DEFECT_RGBA.shape[0]):
        samples.append((f"DEFECT_RGBA[{i}]", DEFECT_RGBA[i, :3].astype(np.float64)))
    dense = ramp(np.linspace(0.0, 1.0, 64), 0.0, 1.0, unknown=-99)
    for i in range(dense.shape[0]):
        samples.append((f"_RAMP_STOPS @ t={i / 63.0:.2f}", dense[i, :3].astype(np.float64)))

    # MCRC_NONE_RGBA is ABSENT_RGBA (absence of evidence is drawn
    # as the hole), so it is excluded: it is not a collision, it is the point.
    out: list[str] = []

    # The ECC legend puts all ten of its states on screen at once, so they must
    # also be told apart from each other, not merely from the state palette.
    # This is the check that stops "corrected" and "correction FAILED" from
    # drifting into two shades of the same thing.
    # Both tables are checked whole, and the stripe's is a superset of the
    # map's: the stripe draws one legend with all ten ECC colours plus its own
    # three, and the MCRC stripe draws six at once.  Two classes in one legend
    # that look alike are two classes the reader will merge.
    for label, tbl in (("ECC_RGBA", ECC_RGBA),
                       ("STRIPE_ECC_RGBA", STRIPE_ECC_RGBA),
                       ("STRIPE_MCRC_RGBA", STRIPE_MCRC_RGBA)):
        lab = _srgb_to_lab(np.asarray(tbl, dtype=np.float64)[:, :3])
        for i in range(tbl.shape[0]):
            for j in range(i + 1, tbl.shape[0]):
                d = float(np.linalg.norm(lab[i] - lab[j]))
                if d < MIN_STATE_DELTA_E:
                    out.append(
                        "%s[%d] and %s[%d] are only %.1f dE apart "
                        "(minimum %.1f); that legend shows both at once"
                        % (label, i, label, j, d, MIN_STATE_DELTA_E))
    for name, rgb in samples:
        d = np.linalg.norm(_srgb_to_lab(rgb)[None, :] - target_lab, axis=-1)
        j = int(np.argmin(d))
        if float(d[j]) < MIN_STATE_DELTA_E:
            out.append(
                "%s %s is only %.1f dE from the '%s' state colour (minimum %.1f)"
                % (name, tuple(int(v) for v in rgb), float(d[j]), target_names[j],
                   MIN_STATE_DELTA_E)
            )
    return out
