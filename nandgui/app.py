"""nandgui.app -- the shell.

Window + docking layout, colour-mode selector, the permanent legend, the
address bar, the status bar, and the one place the frame loop wires the views
to the worker.  This is the only module with a ``main()``.

Two invariants this file is responsible for, and which the rest of the UI
cannot enforce on its own:

1.  The five page states (programmed / erased / skipped / absent / all-zero)
    are on screen at all times.  ``draw_legend`` always emits
    ``palette.state_legend()`` first, in every colour mode, so the reader's
    vocabulary can never quietly shrink to whatever the current mode happens
    to colour.  This project has twice concluded the wrong thing by merging
    those states; the legend is the standing correction.

2.  The UI thread performs no store I/O.  Every byte comes from a
    ``worker.Result``.  ``frame()`` polls, hands each result to the views, and
    otherwise only reads the in-memory summary arrays.
"""

from __future__ import annotations

import argparse
import dataclasses
import math
import os
import sys
import threading
import time
import traceback

import numpy as np

from imgui_bundle import hello_imgui, imgui
from imgui_bundle.imgui import internal as imgui_internal

from . import (
    DECODED_PAGE_BYTES,
    DECODED_STORE,
    N_BLOCK,
    N_CE,
    N_ROWS,
    RAW_PAGE_BYTES,
    RAW_STORE,
    STORE_CANDIDATES,
    SUMMARY_NPZ,
    ensure_titania_on_path,
    resolve_paths,
)
from . import (blockview, gltex, mapview, model, pagefmt, pageview,
               palette, sidecar)
from .model import Summary
from .worker import (
    DEFAULT_MAX_WORKERS,
    PRIORITY_BACKGROUND,
    PRIORITY_INTERACTIVE,
    WORKERS_ENV,
    Result,
    Worker,
)

__all__ = [
    "App",
    "build",
    "set_mode",
    "select",
    "frame",
    "draw_legend",
    "draw_status",
    "mono_font",
    "mono_font_size",
    "mono_font_name",
    "shutdown",
    "main",
]


# Window names.  They double as the dock-builder keys and as the labels handed
# to the view draw() functions, so the two can never drift apart.
WIN_MAP = "Drive map"
WIN_BLOCK = "Block stripe"
WIN_PAGE = "Page"
WIN_LEGEND = "Legend"

# Monospace font for the hexdumps.  The plain-glfw backend loads no font of its
# own, so the atlas would otherwise hold only imgui's built-in ProggyClean.
# Candidates are tried in order and the first one that exists on disk wins; if
# none does, the built-in font stays and the inspector simply never pushes.
# Nothing here may raise: a missing font is a cosmetic problem, and a viewer
# that will not start is a real one.
MONO_FONT_CANDIDATES = (
    ("Consolas", "C:/Windows/Fonts/consola.ttf"),
    ("Courier New", "C:/Windows/Fonts/cour.ttf"),
)
# Read by pageview (via mono_font_size()) so the clipper row height matches the
# rows it actually draws, and available to the dock sizing so a full 16-byte hex
# line fits horizontally.  Measured here with calc_text_size on the real 73-char
# row "%06X  " + 16 hex pairs + ASCII gutter:
#
#     built-in ProggyClean 13   511 px      Consolas 13   511 px
#     Consolas 14              584 px      Consolas 15   584 px
#
# 14 and 15 share an advance of 8 px, so 15 is the larger font for the same
# width and there is nothing between 511 and 584 to pick.  At 584 px the Page
# dock needs ~632 px of width including its chrome (window padding 16 + child
# border 2 + child padding 16 + the vertical scrollbar the 1152-row hexdump
# always shows), i.e. a right-hand split of ~0.35 at 1800 px rather than 0.34.
# Anything sizing that dock should measure a row instead of copying a number.
MONO_FONT_SIZE = 15.0

# app.py is not a view, so it does not own one of worker.py's JOB_* prefixes.
# This private key still cannot collide with them: the views key on
# ("map",)/("stripe",)/("page",)/("decode",) and this one is a distinct string.
_JOB_STORES = ("stores-status",)
#: The ECC scan.  One key, so a click while a scan is running replaces it
#: rather than queueing a second sweep behind it (worker.py coalesces by key).
_JOB_ECC = ("ecc-scan",)
#: Rebuilding the derived sidecars.  Background: the descriptor derivation
#: mounts the FTL (~48 s) and nothing on screen is blocked waiting for it.
_JOB_SIDECAR = ("sidecars",)
#: Phase updates from that thread.  Its own key so the drop gate keeps only the
#: newest, and so _dispatch cannot mistake a progress line for a finished job.
_JOB_SIDECAR_PROGRESS = ("sidecars", "progress")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class App:
    summary: Summary | None
    summary_error: str | None  # MissingSummary.message, shown on-screen
    worker: Worker
    mode: palette.Mode = palette.Mode.CONTENT
    rgba: np.ndarray | None = None  # cached colorize() for `mode`
    map: mapview.MapState = dataclasses.field(default_factory=mapview.MapState)
    block: blockview.BlockState = dataclasses.field(
        default_factory=blockview.BlockState
    )
    page: pageview.InspectorState = dataclasses.field(
        default_factory=pageview.InspectorState
    )
    selected: tuple[int, int] | None = None  # (ce, block)
    include_suspect: bool = False  # decoded-mcrc-failed in/out
    show_legend: bool = True
    status: str = ""
    stores_status: str = ""  # raw_error / decoded_error, if any


# Per-process UI bookkeeping that is not part of the frozen App contract.
# One process runs one App, so module state is honest here and keeps the
# dataclass exactly as specified.
_ui: dict[str, object] = {
    # The paths this process was actually started with.  App's field list is
    # frozen by the contract, so they live here -- but they must live
    # somewhere: File > Reload summary used to re-read the module default,
    # so `--summary alt.npz` plus one menu click silently repainted the whole
    # drive map from a file the user never asked for.
    "summary_path": SUMMARY_NPZ,
    "raw_path": RAW_STORE,
    "decoded_path": DECODED_STORE,
    "legend": None,  # palette.legend(summary, mode), recomputed only in set_mode
    "layout_built": False,
    "fit_map": True,  # fit on the first frame the map region has a size
    "runner": None,  # hello_imgui.RunnerParams, for the Quit menu item
    "cellmap": None,  # degraded-mode SLC/TLC map when summary.npz is absent
    "cellmap_error": None,
    "show_palette_help": False,
    "show_ecc_help": False,
    "last_saved": "",
    "view_errors": {},  # view name -> last traceback drawn by _draw_view_error
    "mono_font": None,  # ImFont for the hexdumps, or None = built-in font
    "mono_font_name": "",  # what was actually loaded, said out loud in the status bar
    # --- the ECC map ---------------------------------------------------
    # uint8[48000] of pagefmt.EccState, one per block, and 0 is NOT_MEASURED.
    # It is a measurement log, not a prediction: a block is only ever coloured
    # by pages this session actually ran the decoder over. Nothing here is
    # derived from summary.npz's dec_clean/dec_fail, which are the decoded
    # store's MCRC tallies under FORMAT_A and say nothing about ECC.
    "ecc": None,
    "ecc_blocks": {},   # (ce, block) -> model.BlockEcc, for hover and counts
    "ecc_sweep": False,  # keep measuring unmeasured blocks in the background
    "ecc_busy": False,   # a scan job is in flight
    "ecc_status": "",
}

#: Blocks per background sweep request.  ONE, and the measurement says so.
#:
#: The batching that matters happens INSIDE a block: `scan_block_ecc` hands all
#: 12 sampled pages to the corrector in one call, which is 48 codewords instead
#: of 4.  Putting several blocks in one REQUEST adds nothing on top of that --
#: measured at 0.15 s a block either way -- because the request is not the unit
#: the corrector parallelises over.
#:
#: What it does change is how often the map moves.  At eight the reader sees a
#: block of colour appear about once a second; at one they see a block land
#: every ~0.15 s, which is the same throughput and looks like progress instead
#: of like a stall.  When two numbers measure the same, take the one that is
#: visibly working.
ECC_SWEEP_BLOCKS = 1

#: How many unmeasured blocks one sweep step looks at.  Most of them will need
#: no decoding at all -- on this drive 46386 of 48000 blocks have no programmed
#: page -- and those are settled here, on the UI thread, at roughly a numpy
#: store each.  Drawing 512 at a time crosses the empty stretches in a handful
#: of frames while keeping the per-frame cost far under a frame; the cap is what
#: stops a fully erased drive from trying to walk all 48000 in one.
ECC_SWEEP_CANDIDATES = 512


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------


def _load_mono_font(size: float = MONO_FONT_SIZE) -> object | None:
    """Add a monospace font to the atlas.  Call after create_context().

    Returns the ImFont, or None when neither candidate could be loaded -- in
    which case the caller has nothing to do: `mono_font()` answers None and the
    inspector keeps the built-in font.  This function never raises.

    Order matters twice over:

    * imgui makes the first font added the UI default, so the built-in font is
      added first (when the atlas is still empty) and the monospace font second.
      Add them the other way round and the whole shell -- menus, legend, status
      -- silently switches to Consolas.
    * the font must be in the atlas before the backend uploads its texture.  On
      imgui_bundle 1.92 the glfw backend advertises `renderer_has_textures` and
      rebuilds lazily, so before or after `GlfwRenderer(...)` both work and
      there is no `refresh_font_texture()` to call (it does not exist on this
      backend -- calling it would AttributeError).  Both runners still add the
      font before constructing the renderer, which is correct on either.
    """
    if _ui.get("mono_font") is not None:
        return _ui["mono_font"]
    try:
        io = imgui.get_io()
        atlas = io.fonts
        if len(atlas.fonts) == 0:
            # Keep imgui's own font as font #0 / the UI default.
            atlas.add_font_default()
        for name, path in MONO_FONT_CANDIDATES:
            if not os.path.exists(path):
                continue
            font = atlas.add_font_from_file_ttf(path, float(size))
            if font is None:
                continue
            _ui["mono_font"] = font
            _ui["mono_font_name"] = f"{name} {size:g}px"
            print(f"hexdump font: {name} ({path}) at {size:g}px", file=sys.stderr)
            return font
    except Exception:                                           # noqa: BLE001
        # a font that will not load must not cost us the session.
        traceback.print_exc()
    _ui["mono_font"] = None
    _ui["mono_font_name"] = "imgui built-in (no monospace TTF found)"
    print("hexdump font: " + str(_ui["mono_font_name"]), file=sys.stderr)
    return None


def mono_font():
    """The monospace ImFont for hexdumps, or None if none was loaded.

    pageview must treat None as "do not push" rather than as an error: the
    built-in ProggyClean is itself fixed-pitch, so the columns still line up.
    """
    return _ui.get("mono_font")


def mono_font_size() -> float:
    """Size the monospace font was loaded at, for `imgui.push_font(font, size)`.

    0.0 when there is no monospace font -- which is also imgui's "keep the
    current size", so passing it through is harmless.
    """
    return float(MONO_FONT_SIZE) if _ui.get("mono_font") is not None else 0.0


def mono_font_name() -> str:
    """Human-readable name of the font in use, for the status bar."""
    return str(_ui.get("mono_font_name") or "")


# ---------------------------------------------------------------------------
# build / mode / selection
# ---------------------------------------------------------------------------


def build(
    summary_path: str = SUMMARY_NPZ,
    *,
    raw_path: str = RAW_STORE,
    decoded_path: str = DECODED_STORE,
    include_suspect: bool = False,
    workers: int | None = None,
) -> App:
    """Load the summary, start the worker pool, return a live App.

    Never raises for missing data.  ``summary.npz`` is built by a separate
    tool and may legitimately not exist yet; in that case the UI still comes
    up, says so where the map would be, and the block/page views keep working
    because they read the stores directly.
    """
    _ui["summary_path"] = str(summary_path)
    _ui["raw_path"] = str(raw_path)
    _ui["decoded_path"] = str(decoded_path)

    summary: Summary | None = None
    summary_error: str | None = None
    try:
        summary = model.load_summary(summary_path)
    except model.MissingSummary as exc:
        summary_error = getattr(exc, "message", None) or str(exc)
    except Exception as exc:  # a corrupt npz must not stop the app either
        summary_error = f"Could not load {summary_path}: {exc!r}"

    path_error: str | None = None
    try:
        ensure_titania_on_path()
    except Exception as exc:
        # The WSL share being down is a store problem, not a fatal one: the
        # summary view still works entirely from numpy.
        path_error = str(exc)

    # setup= runs once per worker thread, so each worker gets its own pair of
    # DumpDevices and its own codec, constructed, used and closed by the one
    # thread that owns them.  Not an optimisation: titania/store.py's read_at()
    # does `h = self._handle(id); h.seek(off); h.read(n)` on a shared file
    # object, so two threads sharing one Stores interleave and one of them gets
    # the other's page bytes -- at the right length and with no exception.
    def _open(raw=raw_path, decoded=decoded_path, suspect=include_suspect):
        return model.open_stores(raw, decoded, suspect)

    # worker._teardown looks for `setup.teardown` first, then falls back to
    # ctx.teardown() / ctx.close().  Stores is a plain dataclass with neither,
    # so without this attribute nothing would ever close the DumpDevices and
    # the mmapped indexes would stay open until the process died.  It has to
    # be close_stores and it has to run on the worker thread -- the UI thread
    # must never close a store whose index is mmapped.  It now runs once per
    # ctx, each on the thread that opened that ctx.
    _open.teardown = model.close_stores

    worker = Worker(name="nandgui", setup=_open, workers=workers)
    worker.start()

    app = App(
        summary=summary,
        summary_error=summary_error,
        worker=worker,
        include_suspect=include_suspect,
    )
    if path_error:
        app.stores_status = path_error

    # The cell map and the msoDescriptor are DERIVED data merely cached beside
    # the stores: a missing file is rebuilt rather than costing the viewer a
    # feature.  Both go to the worker -- the descriptor derivation mounts the
    # FTL and takes ~48 s -- so neither delays the first frame.  The cell map is
    # wanted only in the degraded path; summary.slc is authoritative when the
    # summary exists.
    _request_sidecars(app, want_cellmap=summary is None,
                      want_summary=summary is None)

    set_mode(app, app.mode)
    _request_stores_status(app)

    # CE0 is dead and has no records at all, so the initial cursor starts on
    # CE1 -- landing on CE0 would show a full row of ABSENT and read as a bug.
    select(app, 1, 0)
    return app


def set_mode(app: App, mode: palette.Mode) -> None:
    """Recompute app.rgba via palette.colorize and set app.map.dirty."""
    app.mode = palette.Mode(mode)
    if app.summary is None:
        app.rgba = None
        _ui["legend"] = None
    else:
        # ~5 ms of pure numpy on the UI thread; cached until the mode changes,
        # so panning and zooming never recolour.
        app.rgba = palette.colorize(app.summary, app.mode, ecc_map(app))
        # Same reasoning for the legend, which costs up to 1.3 ms of the 16.7 ms
        # frame budget (block_states + np.unique + two np.percentile over 48000
        # rows) and depends on exactly the same two things.  draw_legend used to
        # rebuild a byte-identical string list every frame, including while the
        # app sat idle.
        _ui["legend"] = palette.legend(app.summary, app.mode, ecc_map(app))
    app.map.dirty = True


def select(app: App, ce: int, block: int, page: int | None = None) -> None:
    """The single selection path.

    Every view event and the address bar funnel through here, so map / stripe
    / inspector can never disagree about what is selected.
    """
    ce = max(0, min(int(ce), 11))
    block = max(0, min(int(block), 3999))

    app.selected = (ce, block)
    app.map.selected = (ce, block)

    if app.block.block != block or app.block.states is None:
        blockview.request(app.block, app.worker, block, app.summary)
    app.block.selected = (ce, page) if page is not None else app.block.selected

    if page is not None:
        slc = _is_slc(app, ce, block)
        n_pages = model.pages_per_block(slc)
        page = max(0, min(int(page), n_pages - 1))
        target = (ce, block, page, slc)
        app.page.target = target
        pageview.request(app.page, app.worker, target, decode=True,
                         owner=_owner_of(app, ce, block))

    if app.summary is not None:
        app.status = model.describe_block(app.summary, ce, block)
    else:
        app.status = f"ce {ce} block {block} (no summary loaded)"


# ---------------------------------------------------------------------------
# the ECC map -- a measurement log, never a prediction
# ---------------------------------------------------------------------------
# The map only ever shows pages somebody actually decoded.  There is no
# drive-wide ECC measurement anywhere on disk: summary.npz carries dec_clean /
# dec_fail, which are decoded.store's MCRC tallies under FORMAT_A and say
# nothing about what the LDPC decoder does.  Deriving an ECC colour from them
# would be reasoning, not measuring, and the two disagree by construction --
# 172 of 250 sampled "failed" ftl pages are fully repaired by the decoder that
# is already in the package.  So every block starts NOT_MEASURED, with its own
# colour and its own legend row, and turns into a verdict only when a page of
# it has been run.


def ecc_map(app: App | None = None) -> np.ndarray:
    """-> uint8[48000] of pagefmt.EccState, created on first use."""
    arr = _ui.get("ecc")
    if not isinstance(arr, np.ndarray) or arr.shape != (N_ROWS,):
        arr = np.zeros(N_ROWS, dtype=np.uint8)     # 0 == NOT_MEASURED
        _ui["ecc"] = arr
    return arr


def _ecc_ranks(app: App) -> np.ndarray:
    """The map's ECC precedence grid, rebuilt only when a scan changed it.

    Cached because mapview caches its column reduction against the ranks
    object: a fresh array every frame would re-reduce every frame, which is
    cheap but pointless, and it would make the cache impossible to reason
    about.
    """
    grid = _ui.get("ecc_ranks")
    if not isinstance(grid, np.ndarray):
        grid = palette.ecc_ranks(ecc_map(app))
        _ui["ecc_ranks"] = grid
    return grid


def _ecc_blocks() -> dict:
    blocks = _ui.get("ecc_blocks")
    if not isinstance(blocks, dict):
        blocks = {}
        _ui["ecc_blocks"] = blocks
    return blocks


def _ecc_merge(app: App, entry: model.BlockEcc) -> None:
    """Fold one block's scan into the map, WORST-state-wins, counts kept.

    Two scans of the same block accumulate rather than replace: a later
    12-page sample must not erase the uncorrectable page an earlier one found.
    """
    if entry is None:
        return
    if not (0 <= int(entry.ce) < 12 and 0 <= int(entry.block) < N_BLOCK):
        return
    blocks = _ecc_blocks()
    key = (int(entry.ce), int(entry.block))
    old = blocks.get(key)
    if old is not None:
        # Merge by PAGE, so re-scanning a block cannot inflate "n of m pages
        # measured", and worst-state-wins so a later clean sample cannot erase
        # an uncorrectable page an earlier one found.
        merged = dict(old.page_states)
        merged.update(entry.page_states)
        entry = dataclasses.replace(
            entry,
            state=pagefmt.worse_ecc(old.state, entry.state),
            page_states=merged,
            corrected_bits=old.corrected_bits + entry.corrected_bits,
            worst_syndrome=max(old.worst_syndrome, entry.worst_syndrome),
            seconds=old.seconds + entry.seconds,
            error=entry.error or old.error)
    blocks[key] = entry
    ecc_map(app)[key[0] * N_BLOCK + key[1]] = int(entry.state)
    # The precedence grid is derived from the map, so it dies with it.
    _ui["ecc_ranks"] = None


def _ecc_note_page(app: App, ce: int, block: int, page: int, ecc) -> None:
    """One inspected page counts as a measurement of its block.

    It is recorded as exactly what it is -- one page of 368 or 1104 -- so the
    hover says "1 of 1104 pages measured" and nobody reads a single page as a
    block verdict.

    Two things are not recorded.  A page whose ECC was not run
    (NOT_ATTEMPTED -- no bytes, or the run-ECC toggle off) is not a
    measurement, and writing it to the map would claim one; and a page already
    counted is not counted twice, because re-requesting the same address (the
    correction combo, a re-select) would otherwise inflate "n of m pages
    measured" without measuring anything new.
    """
    if ecc is None or app.summary is None or not app.summary.valid(ce, block):
        return
    state = model.EccState(int(ecc.state))
    if state in (model.EccState.NOT_MEASURED, model.EccState.NOT_ATTEMPTED):
        return
    seen = _ui.get("ecc_pages")
    if not isinstance(seen, set):
        seen = set()
        _ui["ecc_pages"] = seen
    key = (int(ce), int(block), int(page))
    if key in seen:
        return
    seen.add(key)
    entry = model.BlockEcc(
        ce=int(ce), block=int(block), slc=_is_slc(app, ce, block), state=state,
        page_states={int(page): int(state)},
        pages=model.pages_per_block(_is_slc(app, ce, block)),
        corrected_bits=int(getattr(ecc, "corrected_bits", 0)),
        worst_syndrome=int(getattr(ecc, "max_syndrome", 0)),
        seconds=float(getattr(ecc, "seconds", 0.0)))
    _ecc_merge(app, entry)
    _repaint_ecc(app)


def _repaint_ecc(app: App) -> None:
    """Recolour only when the ECC map is what is on screen."""
    if app.mode == palette.Mode.ECC:
        set_mode(app, palette.Mode.ECC)


def ecc_hover(app: App, ce: int, block: int) -> str:
    """The ECC sentence for a block, for the hover line under the map."""
    entry = _ecc_blocks().get((int(ce), int(block)))
    if entry is not None:
        return entry.sentence()
    # In the map but with no scan behind it: the sweep settled this block from
    # the summary because it has no programmed page, so no decoder ran and
    # there is no per-page distribution to report.  Say which of the two it is
    # rather than falling through to "nobody has looked", which the colour
    # would then contradict.
    state = int(ecc_map(app)[int(ce) * N_BLOCK + int(block)])
    npages = model.pages_per_block(_is_slc(app, ce, block))
    if state == int(model.EccState.ERASED):
        return ("ce=%d block=%d ECC: %s -- all %d pages read erased, so there "
                "is no codeword to correct. Not a decode result; taken from "
                "the summary." % (ce, block,
                                  pagefmt.ECC_SHORT[model.EccState.ERASED],
                                  npages))
    if state == int(model.EccState.NOT_ATTEMPTED):
        return ("ce=%d block=%d ECC: %s -- this block holds no page the "
                "capture read, so there was nothing to decode. Not a decode "
                "result; taken from the summary."
                % (ce, block, pagefmt.ECC_SHORT[model.EccState.NOT_ATTEMPTED]))
    return ("ce=%d block=%d ECC: %s -- 0 of %d pages measured. Use "
            "'Measure ECC' (or open a page) to run the decoder here."
            % (ce, block, pagefmt.ECC_SHORT[model.EccState.NOT_MEASURED],
               npages))


def _ecc_scan_request(app: App, blocks: list[tuple[int, int, bool]],
                      sample: int, why: str,
                      priority: int = PRIORITY_INTERACTIVE) -> None:
    """Submit an ECC scan of `blocks` (worker thread does the reading)."""
    if not blocks:
        return

    def job(stores: object, todo=tuple(blocks), n=int(sample)) -> list:
        # Never raises: model.scan_block_ecc catches per page, and anything it
        # cannot catch would come back as Result.error and be shown, not lost.
        return [model.scan_block_ecc(stores, ce, block, slc, sample=n)
                for ce, block, slc in todo]

    _ui["ecc_busy"] = True
    _ui["ecc_status"] = why
    app.worker.submit(_JOB_ECC, job, priority=priority)


def _ecc_apply_results(app: App, value: object) -> None:
    entries = value if isinstance(value, list) else [value]
    n = 0
    last = None
    for entry in entries:
        if isinstance(entry, model.BlockEcc):
            _ecc_merge(app, entry)
            last = entry
            n += 1
    _ui["ecc_busy"] = False
    if last is not None:
        _repaint_ecc(app)
        # The merged entry is what the map now shows, so that is what the
        # status line reports -- the scan's own numbers would understate a
        # block that an earlier scan already found something in.
        merged = _ecc_blocks().get((last.ce, last.block), last)
        _ui["ecc_status"] = "measured %d block%s; last: %s" % (
            n, "" if n == 1 else "s", merged.sentence())
        app.status = _ui["ecc_status"]


def _ecc_next_unmeasured(app: App, ce: int, block: int,
                         want: int) -> list[tuple[int, int, bool]]:
    """Up to `want` unmeasured blocks, walking FORWARD from (ce, block).

    Forward, in address order, and straight through the end of a CE into the
    next one.  It used to walk OUTWARD -- block+delta and block-delta together
    -- and stop dead at the CE it started in, which made the sweep do two
    things a reader has to squint at: the measured region grew in both
    directions at once from wherever the selection happened to be, and the
    whole drive could never be covered from one press because CE 1 finishing
    ended the sweep with eleven CEs untouched.

    Wrapping to CE 0 at the end means the sweep covers everything from any
    starting point, and `_ecc_sweep_step` stops when a full lap finds nothing.

    In numpy, and that is not a micro-optimisation.  This was a Python loop over
    the map skipping measured entries one at a time, which is fine while the map
    is empty and ruinous once it is not: at 99.9% measured one call cost 6.5 ms
    because it stepped over ~48000 measured blocks to reach the next hole, and a
    frame's worth of calls measured 79 ms -- 12 fps, on the UI thread, spent
    looking for zeros.  `flatnonzero` is the same search in C, ~20 us.
    """
    arr = ecc_map(app)
    total = N_CE * N_BLOCK
    holes = np.flatnonzero(arr[:total] == 0)
    if holes.size == 0:
        return []
    # forward from the selection, wrapping: the holes at or after `start`
    # first, then the ones before it.
    start = (int(ce) * N_BLOCK + int(block)) % total
    k = int(np.searchsorted(holes, start))
    picked = np.concatenate((holes[k:], holes[:k]))[:max(0, int(want))]
    return [(int(i // N_BLOCK), int(i % N_BLOCK),
             _is_slc(app, int(i // N_BLOCK), int(i % N_BLOCK)))
            for i in picked]


def _ecc_settle_without_decoding(app: App,
                                 todo: list[tuple[int, int, bool]]
                                 ) -> list[tuple[int, int, bool]]:
    """Answer the blocks that need no decoder at all.  -> the rest

    A block with no PROGRAMMED page has nothing for the corrector to do, and the
    summary already says so -- per block, counted when it was built, no read and
    no decode.  Running the sweep over those was most of its work on this drive:
    ~99% of blocks read all-0xFF, and every one of them was costing a store
    read and eight LDPC decodes to be told that erased media is erased.

    They are recorded with the state that is true of them (ERASED for erased
    media, NOT_ATTEMPTED where the pages are skipped or absent) rather than
    left NOT_MEASURED, so the map distinguishes "nothing to measure here" from
    "nobody has looked yet" and the sweep never offers them again.
    """
    s = app.summary
    if s is None:
        return todo
    rest = []
    arr = ecc_map(app)
    touched = False
    for ce, block, slc in todo:
        i = s.index(ce, block)
        if int(s.programmed[i]):
            rest.append((ce, block, slc))
            continue
        state = (model.EccState.ERASED if int(s.erased[i])
                 else model.EccState.NOT_ATTEMPTED)
        # Straight into the map, and NOT through `_ecc_merge`: there are ~46000
        # of these on this drive and each merge builds a BlockEcc, rewrites a
        # dict entry and drops the cached rank grid.  A block with no page to
        # measure has no per-page distribution to carry, so `ecc_hover` builds
        # its sentence from the summary instead -- see `ecc_hover`.
        arr[int(ce) * N_BLOCK + int(block)] = int(state)
        touched = True
    if touched:
        _ui["ecc_ranks"] = None
    return rest


def _ecc_sweep_step(app: App) -> None:
    """One background scan per idle frame, while the sweep is on.

    One block per request: the batching that pays happens inside the block, in
    `scan_block_ecc`, which hands all 12 sampled pages to the corrector at once.
    See `ECC_SWEEP_BLOCKS` for why more blocks per request is not a speed-up.

    Blocks with nothing to decode are settled here, on the UI thread, without
    reaching a worker at all -- see `_ecc_settle_without_decoding`.  The loop
    keeps pulling candidates until it has real work or the drive is done, so a
    long run of erased blocks is crossed in one frame instead of one per frame.
    """
    if not _ui.get("ecc_sweep") or _ui.get("ecc_busy"):
        return
    if app.mode != palette.Mode.ECC or app.summary is None:
        return
    ce, block = app.selected if app.selected else (1, 0)
    cand = _ecc_next_unmeasured(app, ce, block, ECC_SWEEP_CANDIDATES)
    if not cand:
        _ui["ecc_sweep"] = False
        _ui["ecc_status"] = ("sweep finished: every block on every CE is "
                             "measured or has nothing to measure")
        _repaint_ecc(app)
        return
    rest = _ecc_settle_without_decoding(app, cand)
    if len(rest) != len(cand):
        _repaint_ecc(app)          # something was settled; show it this frame
    todo = rest[:ECC_SWEEP_BLOCKS]
    if not todo:
        # The whole draw was blocks with nothing to decode.  They are recorded
        # now, so the next frame draws past them; no worker was involved.
        return
    _ecc_scan_request(app, todo, model.ECC_SCAN_PAGES,
                      "sweeping from CE%d block %d (%d block%s with data)"
                      % (todo[0][0], todo[0][1], len(todo),
                         "" if len(todo) == 1 else "s"),
                      priority=PRIORITY_BACKGROUND)


def _owner_of(app: App, ce: int, block: int) -> int | None:
    """The owning msoId for a block, or None.  UI thread, summary only.

    The aux layout is descriptor byte +17 of the owning MSO, so the inspector
    cannot resolve a layout without knowing the owner -- and the owner is in the
    summary, which the worker does not get. Passing None here is what made the
    panel report "no MSO descriptor source is loaded" even with the descriptor
    on disk: `_aux_view` returned UNKNOWN before the lookup was ever reached.
    """
    if app.summary is None or not app.summary.valid(ce, block):
        return None
    try:
        owner = int(app.summary.owner[ce * N_BLOCK + block])
    except Exception:                                        # noqa: BLE001
        return None
    return None if owner < 0 else owner


def _is_slc(app: App, ce: int, block: int) -> bool:
    """SLC/TLC decides 368 vs 1104 pages.  Summary first, cellmap as fallback,
    and False (TLC) last -- guessing TLC over-reads rather than truncating a
    block, which is the safer error for a viewer that must not hide pages."""
    if app.summary is not None and app.summary.valid(ce, block):
        return bool(app.summary.is_slc(ce, block))
    cellmap = _ui.get("cellmap")
    if isinstance(cellmap, dict):
        row = cellmap.get(ce)
        if isinstance(row, dict) and block in row:
            return bool(row[block])
    return False


# ---------------------------------------------------------------------------
# store status (worker-thread only fields)
# ---------------------------------------------------------------------------


def _stores_sentence(stores: object) -> str:
    """Runs on the worker thread: Stores lives there and nowhere else."""
    bits: list[str] = []
    raw_error = getattr(stores, "raw_error", None)
    decoded_error = getattr(stores, "decoded_error", None)
    if raw_error:
        bits.append(f"raw store: {raw_error}")
    if decoded_error:
        bits.append(f"decoded store: {decoded_error}")
    if not bits:
        suspect = bool(getattr(stores, "include_suspect", False))
        # Ask the store itself whether it quarantines anything, instead of
        # describing the toggle.  decoded3.store serves every record on the
        # default path, so "clean only" there would report a complete view as a
        # filtered one -- the exact hiding the ECC rebuild removed.
        quarantines = model.decoded_quarantines(
            getattr(stores, "decoded_path", None))
        if quarantines is False:
            bits.append("stores open; decoded store serves ALL records "
                        "(no suspect tier; status is the source name)")
        else:
            bits.append(
                "stores open; decoded tier = "
                + ("clean + suspect (mcrc-failed included)" if suspect
                   else "clean only")
            )
    return "; ".join(bits)


def _request_sidecars(app: App, want_cellmap: bool) -> None:
    """Make the derived sidecars available, rebuilding any that are missing.

    On a WORKER and at background priority, because deriving the msoDescriptor
    mounts the Bootstrap FTL and that measured 47.8 s -- see `nandgui.sidecar`
    for why the files are a cache and not an input.  Nothing here is required
    for the viewer to start: the aux panel reads UNKNOWN until this lands, which
    is what it already says when no descriptor exists at all.
    """
    from . import CELLMAP_JSON          # read live: adopt_data_dir re-points it
    desc_path = pagefmt.MSO_DESCRIPTOR_BIN
    cell_path = CELLMAP_JSON

    def job(ctx: object) -> dict:
        raw_path = getattr(ctx, "raw_path", None) or RAW_STORE
        out: dict = {}
        buf, note = sidecar.descriptor(raw_path, desc_path)
        out["descriptor"], out["descriptor_note"] = buf, note
        if want_cellmap:
            got, cnote = sidecar.cellmap(raw_path, cell_path)
            out["cellmap"], out["cellmap_note"] = got, cnote
        return out

    app.worker.submit(_JOB_SIDECAR, job, priority=PRIORITY_BACKGROUND)


def _request_sidecars(app: App, want_cellmap: bool,
                      want_summary: bool = False) -> None:
    """Make the derived sidecars available, rebuilding any that are missing.

    On a WORKER and at background priority, because deriving the msoDescriptor
    mounts the Bootstrap FTL and that measured 47.8 s -- see `nandgui.sidecar`
    for why these files are caches and not inputs.  Nothing here is required for
    the viewer to start: the aux panel reads UNKNOWN and the map says it has no
    summary until this lands, which is exactly what each already says when the
    input is genuinely absent.

    A run that is missing BOTH the summary and the descriptor mounts the FTL
    twice, once per derivation.  Left that way on purpose: sharing one mount
    would mean `titania.summary` handing its internals back out, and this is a
    once-per-dataset cost on a path that is already minutes long.
    """
    from . import CELLMAP_JSON          # read live: adopt_data_dir re-points it
    desc_path = pagefmt.MSO_DESCRIPTOR_BIN
    cell_path = CELLMAP_JSON
    summary_path = str(_ui.get("summary_path") or SUMMARY_NPZ)
    raw_path = str(_ui.get("raw_path") or RAW_STORE)
    decoded_path = str(_ui.get("decoded_path") or DECODED_STORE)

    _ui["sidecar_started"] = time.time()
    _ui["sidecar_phase"] = "starting"
    _ui["sidecar_running"] = True

    def run() -> None:
        def say(msg: object) -> None:
            app.worker.post_external(_JOB_SIDECAR_PROGRESS, str(msg))

        try:
            out = sidecar.rebuild(
                raw_path, decoded_path, summary_path=summary_path,
                desc_path=desc_path, cell_path=cell_path,
                want_summary=want_summary, want_descriptor=True,
                want_cellmap=want_cellmap, log=say)
        except Exception as exc:                             # noqa: BLE001
            app.worker.post_external(
                _JOB_SIDECAR, error="%s: %s" % (type(exc).__name__, exc))
            return
        app.worker.post_external(_JOB_SIDECAR, out)

    # A DEDICATED thread, NOT the pool.  Measured: these derivations run 48-68 s
    # and a pool worker inside one holds up every broadcast() behind it, so a
    # store-swapping toggle would reach some ctxs a minute before the others.
    # Nothing here wants a pool ctx anyway -- `sidecar` opens its own
    # DumpDevice, and the paths it needs are captured above on this thread.
    threading.Thread(target=run, name="nandgui-sidecars", daemon=True).start()


def _request_stores_status(app: App) -> None:
    app.worker.submit(_JOB_STORES, _stores_sentence, priority=PRIORITY_INTERACTIVE)


def _set_include_suspect(app: App, include: bool) -> None:
    """Toggling the suspect tier reopens decoded.store, which is a
    worker-thread operation (the index is mmapped and must never be closed
    from the UI thread).

    Broadcast, not submit.  Every worker owns its own Stores, so a submitted
    reopen would land on exactly one of them: the other N-1 would keep serving
    the tier the user just switched away from, two CEs of the same stripe could
    be probed against different stores, and nothing on screen would say so.
    worker.broadcast runs it on every ctx and posts one Result, so _dispatch
    still acts once on one user action.
    """
    app.include_suspect = bool(include)

    def job(stores: object, flag: bool = app.include_suspect) -> str:
        model.reopen_decoded(stores, flag)
        return _stores_sentence(stores)

    app.worker.broadcast(_JOB_STORES, job, priority=PRIORITY_INTERACTIVE)
    # The currently displayed page came from the other tier; re-fetch it so the
    # panel never shows a verdict from a store that is no longer mounted.
    if app.page.target is not None:
        t = app.page.target
        pageview.request(app.page, app.worker, t, decode=True,
                         owner=_owner_of(app, int(t[0]), int(t[1])))


# ---------------------------------------------------------------------------
# frame
# ---------------------------------------------------------------------------


def frame(app: App) -> None:
    """One UI frame.  No I/O, no locks, no store access."""
    # 1. Drain the worker and let the views claim what is theirs.
    for result in app.worker.poll():
        _dispatch(app, result)

    # 1b. Keep the ECC sweep going, one block per frame at background
    # priority, and only while the ECC map is what is on screen.
    _ecc_sweep_step(app)

    # 2. Menu bar.
    _draw_menu_bar(app)

    # 3. Dockspace + the four windows.
    dock_id = imgui.get_id("NandGuiDockSpace")
    existing = imgui_internal.dock_builder_get_node(dock_id)
    first_layout = existing is None and not _ui["layout_built"]
    imgui.dock_space_over_viewport(dock_id, imgui.get_main_viewport())
    if first_layout:
        _build_layout(dock_id)
        _ui["layout_built"] = True

    # imgui.begin() returns (visible, open) in imgui-bundle, not a bare bool: a
    # tuple is always truthy, so `if imgui.begin(...)` would draw into a
    # collapsed window every frame.  Unpack it.  end() stays unconditional --
    # begin/end must balance whatever visible says.
    visible, _ = imgui.begin(WIN_MAP)
    if visible:
        # The mode selector and address bar live at the top of the map window:
        # both act on the map, and keeping them in the same window means the
        # controls travel with the view when the layout is rearranged.
        _draw_toolbar(app)
        imgui.separator()
        _draw_map(app)
        _record_overflow(WIN_MAP)
    imgui.end()

    visible, _ = imgui.begin(WIN_BLOCK)
    if visible:
        _draw_block(app)
        _record_overflow(WIN_BLOCK, blockview.canvas_overflow(app.block))
    imgui.end()

    visible, _ = imgui.begin(WIN_PAGE)
    if visible:
        _draw_page(app)
        _record_overflow(WIN_PAGE)
    imgui.end()

    visible, _ = imgui.begin(WIN_LEGEND)
    if visible:
        if app.show_legend:
            draw_legend(app)
            imgui.separator()
        draw_status(app)
    imgui.end()

    _draw_palette_help()
    _draw_ecc_help()


def _record_overflow(window: str, extra_y: float = 0.0) -> None:
    """Remember how far this window's content overran the room it was given.

    Called from inside the window, so it sees that window's scroll extents.
    Nothing in the UI reacts to it -- it exists because "the bottom is cut off
    and a scrollbar appears" was a real defect that every smoke test passed
    through: a scrollbar is not an exception, so --self-test could only find it
    if the number were written down. `extra_y` carries an inner child's
    overflow, which the parent window's own scroll extents cannot see.
    """
    over = _ui.get("overflow")
    if not isinstance(over, dict):
        over = {}
        _ui["overflow"] = over
    over[window] = (max(float(imgui.get_scroll_max_y()), float(extra_y)),
                    float(imgui.get_scroll_max_x()))


def _dispatch(app: App, result: Result) -> None:
    """Hand every Result to every view; they ignore what is not theirs."""
    try:
        if blockview.apply(app.block, result):
            return
        if pageview.apply(app.page, result):
            # An inspected page is an ECC measurement of its block, so it goes
            # into the map: opening a page and then seeing the drive map still
            # claim "not measured" for that very block would be the map
            # contradicting the panel next to it.
            target = app.page.target
            if target is not None and app.page.ecc is not None:
                _ecc_note_page(app, int(target[0]), int(target[1]),
                               int(target[2]), app.page.ecc)
            return
    except Exception as exc:  # a view bug must not take the shell down
        app.status = f"result dispatch failed: {exc!r}"
        return

    if result.key == _JOB_ECC:
        _ui["ecc_busy"] = False
        if result.error:
            _ui["ecc_status"] = "ECC scan failed: %s" % result.error
            app.status = _ui["ecc_status"]
        else:
            _ecc_apply_results(app, result.value)
        return

    if result.key == _JOB_STORES:
        if result.error:
            app.stores_status = result.error
        elif isinstance(result.value, str):
            app.stores_status = result.value
        return

    if result.key == _JOB_SIDECAR_PROGRESS:
        if isinstance(result.value, str):
            _ui["sidecar_phase"] = result.value
        return

    if result.key == _JOB_SIDECAR:
        _ui["sidecar_running"] = False
        if result.error:
            _ui["sidecar_note"] = "sidecars: %s" % result.error
            return
        v = result.value or {}
        note = v.get("descriptor_note")
        if v.get("summary_note"):
            _ui["sidecar_note"] = v["summary_note"]
        if v.get("summary"):
            # _reload_summary re-reads _ui["summary_path"] and calls set_mode,
            # so the map recolours from the file that was just built.
            _reload_summary(app)
        if note:
            _ui["sidecar_note"] = note
        if v.get("descriptor"):
            pagefmt.install_descriptor(v["descriptor"], note)
            # Any page already on screen resolved its aux layout BEFORE this
            # arrived, so it is showing UNKNOWN for a layout we can now name.
            # Re-fetch it rather than leaving the panel to contradict itself
            # until the user happens to click elsewhere.
            if app.page.target is not None:
                t = app.page.target
                pageview.request(app.page, app.worker, t, decode=True,
                                 owner=_owner_of(app, int(t[0]), int(t[1])))
        if v.get("cellmap"):
            _ui["cellmap"] = v["cellmap"]
            _ui["cellmap_error"] = None
        elif v.get("cellmap_note"):
            _ui["cellmap_error"] = v["cellmap_note"]
        return

    if result.error:
        app.status = f"worker error ({result.key}): {result.error}"


# The Page dock has to be wide enough for a whole hexdump line -- the 6-digit
# offset, sixteen hex pairs and the ASCII gutter -- or the inspector's own
# panel scrolls sideways over the bytes it exists to show. That width is a
# measurement, not a ratio someone liked the look of: it depends on the font
# the backend actually loaded, and the monospace face is wider per character
# than the default one at the same size.
_HEX_SAMPLE_BYTES = bytes(range(0x30, 0x40))     # 16 bytes, all printable


def _hex_sample_row() -> str:
    """One representative hexdump line, formatted by the inspector's own
    formatter so the measurement cannot drift from what is drawn."""
    try:
        return pageview._hex_row(_HEX_SAMPLE_BYTES, 0, pageview.HEX_WIDTH, 0)
    except Exception:
        # Same shape: 6 offset digits, two spaces, the hex pairs, two spaces,
        # the ASCII gutter.
        return "%06X  %s  %s" % (
            0,
            " ".join("%02X" % b for b in _HEX_SAMPLE_BYTES),
            _HEX_SAMPLE_BYTES.decode("ascii"),
        )


def _measure_hex_row(text: str) -> float:
    """Width of `text` in the widest font it may be rendered in.

    The hexdump is drawn in the monospace face when one was loaded and in the
    default face otherwise; the two are not the same width, so measure both and
    size for the wider. Never raises -- a failed push must not cost the layout.
    """
    width = float(imgui.calc_text_size(text).x)
    font = mono_font()
    if font is None:
        return width
    try:
        # The same (font, size) pair pageview pushes around the hex rows, so
        # this measures the row that will actually be drawn.
        imgui.push_font(font, mono_font_size())
    except Exception:
        return width
    try:
        width = max(width, float(imgui.calc_text_size(text).x))
    finally:
        imgui.pop_font()
    return width


def _page_dock_ratio(total_w: float) -> float:
    """Fraction of the viewport the Page dock needs for a full hex line."""
    style = imgui.get_style()
    chrome = (
        float(style.window_padding.x) * 2.0     # the Page window
        + float(style.window_padding.x) * 2.0   # the hexdump child inside it
        + float(style.child_border_size) * 2.0
        + float(style.scrollbar_size)           # 1152 rows: it always scrolls
        + float(style.item_spacing.x)
    )
    want = _measure_hex_row(_hex_sample_row()) + chrome
    # Clamped: the inspector may not eat the drive map, and on a very narrow
    # viewport a full hex line simply does not fit at any split.
    return min(max(want / max(float(total_w), 1.0), 0.20), 0.60)


# The legend strip is permanent and its content does not change size, so its
# share of the height stays a constant; everything else is measured.
_BOTTOM_RATIO = 0.22


def _stripe_dock_ratio(rest_h: float) -> float:
    """Fraction of the left column the block stripe gets.

    The drive map is twelve CE rows of at most mapview.MAX_CE_PX each -- a
    small fixed height -- plus its toolbar and its two-sentence footer. The old
    split handed it 58% of the column, which it could not use: the rows
    stretched to fill it and the stripe, its six state counts and its all-zero
    banner were squeezed into what was left. So the map is given what it needs
    (plus one spare text line, so a rounding error costs slack rather than row
    height) and the stripe takes the remainder.
    """
    style = imgui.get_style()
    frame_h = float(imgui.get_frame_height_with_spacing())
    text_h = float(imgui.get_text_line_height_with_spacing())
    map_h = (
        float(style.window_padding.y) * 2.0         # the Drive map window
        + frame_h                                   # mode + address toolbar
        + float(style.item_spacing.y) * 2.0 + 2.0   # the separator under it
        + frame_h                                   # the map's own header row
        + float(mapview.N_CE) * mapview.MAX_CE_PX   # the canvas itself
        + float(style.window_padding.y) * 2.0       # ... and its child padding
        + text_h * 3.0                              # the wrapped honesty footer
        + text_h                                    # one spare line of slack
    )
    return min(max(1.0 - map_h / max(float(rest_h), 1.0), 0.30), 0.80)


def _build_layout(dock_id: int) -> None:
    """Default dock layout, built once.  Later runs restore imgui.ini instead,
    which is why this only fires when the node does not already exist."""
    viewport = imgui.get_main_viewport()
    imgui_internal.dock_builder_remove_node(dock_id)
    imgui_internal.dock_builder_add_node(
        dock_id, int(imgui_internal.DockNodeFlagsPrivate_.dock_space)
    )
    imgui_internal.dock_builder_set_node_size(dock_id, viewport.work_size)

    # Legend across the bottom: it is permanent, so it gets its own strip and
    # is never a tab hidden behind another window.
    rest, bottom = _split(dock_id, imgui.Dir.down, _BOTTOM_RATIO)
    rest, right = _split(rest, imgui.Dir.right, _page_dock_ratio(viewport.work_size.x))
    top, lower = _split(
        rest, imgui.Dir.down,
        _stripe_dock_ratio(float(viewport.work_size.y) * (1.0 - _BOTTOM_RATIO)))

    imgui_internal.dock_builder_dock_window(WIN_MAP, top)
    imgui_internal.dock_builder_dock_window(WIN_BLOCK, lower)
    imgui_internal.dock_builder_dock_window(WIN_PAGE, right)
    imgui_internal.dock_builder_dock_window(WIN_LEGEND, bottom)
    imgui_internal.dock_builder_finish(dock_id)


def _split(node: int, direction: int, ratio: float) -> tuple[int, int]:
    """-> (remaining, new).  dock_builder_split_node_py returns
    (parent, at_dir, other)."""
    _parent, at_dir, other = imgui_internal.dock_builder_split_node_py(
        node, direction, ratio
    )
    return other, at_dir


# ---------------------------------------------------------------------------
# menu bar / toolbar
# ---------------------------------------------------------------------------


def _draw_menu_bar(app: App) -> None:
    if not imgui.begin_main_menu_bar():
        return
    if imgui.begin_menu("File"):
        if imgui.menu_item("Reload summary", "", False)[0]:
            _reload_summary(app)
        if imgui.menu_item("Save selected page (raw)", "", False)[0]:
            _save_raw(app)
        imgui.separator()
        if imgui.menu_item("Quit", "Alt+F4", False)[0]:
            runner = _ui.get("runner")
            if runner is not None:
                runner.app_shall_exit = True
        imgui.end_menu()

    if imgui.begin_menu("View"):
        for mode in palette.MODES:
            selected = mode == app.mode
            if imgui.menu_item(palette.MODE_LABELS[mode], "", selected)[0]:
                set_mode(app, mode)
            if imgui.is_item_hovered():
                imgui.set_tooltip(palette.MODE_HELP[mode])
        imgui.separator()
        changed, value = imgui.menu_item("Legend", "", app.show_legend)
        if changed:
            app.show_legend = value
        changed, value = imgui.menu_item("Grid", "", app.map.show_grid)
        if changed:
            app.map.show_grid = value
        changed, value = imgui.menu_item(
            "Stipple mixed blocks", "", app.map.stipple_mixed
        )
        if changed:
            # Stippling is what stops a precedence colour from being read as
            # purity; turning it off is a deliberate, reversible choice.
            app.map.stipple_mixed = value
        imgui.separator()
        if imgui.menu_item("Fit map", "", False)[0]:
            _ui["fit_map"] = True
        imgui.end_menu()

    if imgui.begin_menu("Data"):
        changed, value = imgui.menu_item(
            "Include suspect decoded pages (mcrc-failed)", "", app.include_suspect
        )
        if changed:
            _set_include_suspect(app, value)
        if imgui.is_item_hovered():
            imgui.set_tooltip(_suspect_tooltip(app))
        imgui.separator()
        if imgui.menu_item("Cancel pending jobs", "", False)[0]:
            app.worker.cancel_all()
        imgui.end_menu()

    if imgui.begin_menu("Help"):
        if imgui.menu_item("The five page states", "", False)[0]:
            _ui["show_palette_help"] = True
        if imgui.menu_item("The ten ECC outcomes", "", False)[0]:
            _ui["show_ecc_help"] = True
        imgui.end_menu()

    # Right-hand side of the menu bar: the numbers that say whether the UI is
    # waiting on the disk.  Running as well as queued, because with a pool
    # "0 queued" no longer means "nothing in flight" -- N jobs can all be
    # running with an empty queue, and reporting only the queue would show an
    # idle-looking bar during the busiest moment there is.
    stats = app.worker.stats()
    if stats["pending"] or stats["running"]:
        imgui.same_line()
        imgui.text_disabled("  |  worker: %d running / %d queued"
                            % (stats["running"], stats["pending"]))
    imgui.end_main_menu_bar()


def _decoded_tier_words(app: App) -> str:
    """What the status bar should say about which decoded records are readable.

    "clean only" is the right words for a store that quarantines pages behind a
    suspect tier, and the wrong words for decoded3.store, which serves every
    record on the default path and carries the decode status in the record's
    source name instead.  Saying "clean only" there would report a complete view
    as a filtered one -- the same hiding the ECC work removed.
    """
    path = _ui.get("decoded_path")
    quarantines = model.decoded_quarantines(path)
    if quarantines is False:
        return "all records served (no suspect tier)"
    if quarantines is None:
        return "clean + suspect" if app.include_suspect else "clean only"
    return "clean + suspect" if app.include_suspect else "clean only"


def _suspect_tooltip(app: App) -> str:
    """What the suspect tier actually contains, counted from this capture.

    Two corrections to the sentence this used to hardcode:

    * the total is read from summary.dec_fail rather than pasted in, so
      `--summary other.npz` cannot leave a number from a different capture on
      screen;
    * the fw-group2 share is named. Those pages are quarantined by construction
      -- decoded.store decoded the whole drive as 4 AUs x 6-byte MCRC, which is
      not that region's geometry -- so counting them as pages "the codec did not
      vouch for" is the same false alarm model.describe_block already corrects
      on hover. Worse, the toggle does not even govern them: needs_raw_decode()
      re-decodes every group-2 record from raw in either tier.
    """
    path = _ui.get("decoded_path")
    if model.decoded_quarantines(path) is False:
        return (
            "This toggle governs NOTHING for the store now open.\n\n"
            "%s declares no suspect tier: every record is served on the\n"
            "default read path, and a page ECC could not correct is served\n"
            "WITH its status rather than quarantined. Measured: 5,453,635\n"
            "records readable with this off, and the same 5,453,635 with it on.\n\n"
            "The decode status is the record's SOURCE NAME, expanded by\n"
            "store.json['status_vocabulary'] -- ecc-corrected,\n"
            "ecc-not-engaged, all-zero-retired, latched-repeat, and so on.\n"
            "Read it in the inspector's ECC and decoded-store sections."
            % (str(path or "the decoded store"),)
        )
    summary = app.summary
    if summary is None:
        return ("decoded.store holds pages whose MCRC failed; they are excluded "
                "from the default read path. Load summary.npz for the counts.")
    try:
        total = int(np.asarray(summary.dec_fail).sum())
        g2 = int(np.asarray(summary.dec_fail)[palette.group2_mask()].sum())
    except Exception:                                           # noqa: BLE001
        return ("decoded.store holds pages whose MCRC failed; they are excluded "
                "from the default read path.")
    text = (
        "decoded.store holds %s pages whose MCRC failed.\n"
        "They are excluded from the default read path; including them\n"
        "means a page may be shown that the codec did not vouch for."
        % (f"{total:,}",)
    )
    if g2:
        text += (
            "\n\nBut %s of those are fw-group2 pages, and that count is\n"
            "decoded.store's own: it decoded the drive as 4 AUs x 6-byte MCRC,\n"
            "which is not that region's geometry, so those pages are\n"
            "quarantined by construction rather than damaged. Open one for the\n"
            "five-chunk verdict -- the inspector re-decodes them from raw in\n"
            "either tier, so this toggle does not govern them at all."
            % (f"{g2:,}",)
        )
    text += (
        "\n\nNo page is hidden by this toggle in the inspector: it reads\n"
        "all.store, decodes the region's own way, and reports the ECC outcome\n"
        "in words -- clean / corrected / partly corrected / correction FAILED\n"
        "-- with the bytes on screen either way. MEASURED on 250 sampled ftl\n"
        "pages of this quarantined tier: 172 are fully repaired by the decoder\n"
        "already in the package, and the other 78 are past its waterfall and\n"
        "need a re-READ, not a tier."
    )
    return text


def _draw_toolbar(app: App) -> None:
    """Mode selector + address bar.

    Every same_line here goes through mapview.same_line_or_wrap. This toolbar is
    drawn into the Drive map window, which carries no HorizontalScrollbar flag,
    so a row that overruns the panel is not scrolled to -- it is clipped.
    MEASURED at 1280x800 before this change: the toolbar's right edge was 875 px
    inside a 631 px content region and the window's scroll_max_x was 244 px, and
    what fell off the edge was the "Go" button and the SLC/TLC label. It fit at
    1800x1000 by luck, which is why the self-test stayed green over it.
    """
    labels = [palette.MODE_LABELS[m] for m in palette.MODES]
    current = list(palette.MODES).index(app.mode)
    imgui.set_next_item_width(220.0)
    changed, index = imgui.combo("mode", current, labels)
    if changed:
        set_mode(app, palette.MODES[index])
    if imgui.is_item_hovered():
        imgui.set_tooltip(palette.MODE_HELP[app.mode])

    mapview.same_line_or_wrap(mapview.label_w("|"))
    imgui.text_disabled("|")

    ce, block = app.selected if app.selected else (1, 0)
    page = app.page.target[2] if app.page.target else 0
    slc_label = (
        f"  {'SLC (368 pages)' if _is_slc(app, ce, block) else 'TLC (1104 pages)'}"
    )

    mapview.same_line_or_wrap(mapview.input_int_w(90.0, "ce"))
    imgui.set_next_item_width(90.0)
    ce_changed, ce_value = imgui.input_int("ce", int(ce), 1, 1)
    mapview.same_line_or_wrap(mapview.input_int_w(120.0, "block"))
    imgui.set_next_item_width(120.0)
    blk_changed, blk_value = imgui.input_int("block", int(block), 1, 100)
    mapview.same_line_or_wrap(mapview.input_int_w(120.0, "page"))
    imgui.set_next_item_width(120.0)
    pg_changed, pg_value = imgui.input_int("page", int(page), 1, 16)
    mapview.same_line_or_wrap(
        mapview.label_w("Go") + imgui.get_style().frame_padding.x * 2.0)
    go = imgui.button("Go")

    if ce_changed or blk_changed or pg_changed or go:
        select(app, ce_value, blk_value, pg_value)
        if go:
            mapview.focus(app.map, max(0, min(ce_value, 11)), max(0, min(blk_value, 3999)))

    mapview.same_line_or_wrap(mapview.label_w(slc_label))
    imgui.text_disabled(slc_label)

    if app.mode == palette.Mode.ECC:
        _draw_ecc_controls(app, ce, block)


def _draw_ecc_controls(app: App, ce: int, block: int) -> None:
    """The ECC mode's own row: measure this block, measure a neighbourhood,
    or keep measuring in the background.

    The map cannot show an ECC outcome nobody has measured, and there is no
    drive-wide ECC measurement on disk -- so the controls that produce one are
    part of the mode, next to the picture they fill in.
    """
    busy = bool(_ui.get("ecc_busy"))
    slc = _is_slc(app, ce, block)
    if busy:
        imgui.begin_disabled()
    if imgui.button("Measure ECC: this block"):
        _ecc_scan_request(app, [(ce, block, slc)], model.ECC_SCAN_PAGES,
                          "measuring ce%d block %d" % (ce, block))
    mapview.same_line_or_wrap(mapview._button_w("Measure ECC: 32 blocks here"))
    if imgui.button("Measure ECC: 32 blocks here"):
        todo = [(ce, b, _is_slc(app, ce, b))
                for b in range(max(0, block - 16), min(N_BLOCK, block + 16))]
        _ecc_scan_request(app, todo, 4,
                          "measuring 32 blocks around ce%d block %d"
                          % (ce, block))
    if busy:
        imgui.end_disabled()
    mapview.same_line_or_wrap(mapview._check_w("keep measuring"))
    changed, sweep = imgui.checkbox("keep measuring",
                                    bool(_ui.get("ecc_sweep")))
    if changed:
        _ui["ecc_sweep"] = bool(sweep)
    if imgui.is_item_hovered():
        imgui.set_tooltip(
            "Scan unmeasured blocks of this CE outward from the selection, "
            "one block per frame at background priority. A 12-page sample of a "
            "block costs 0.1-1.5 s (4-8 ms a clean page, up to ~215 ms to "
            "prove one uncorrectable), so the whole drive is hours of work -- "
            "which is exactly why unmeasured blocks say 'not measured' instead "
            "of being coloured in from the store's MCRC tallies.")
    mapview.same_line_or_wrap(200.0)
    if busy:
        imgui.text_disabled("measuring...")
    else:
        done = int(np.count_nonzero(ecc_map(app)))
        imgui.text_disabled("%d of %d blocks measured" % (done, N_ROWS))


# ---------------------------------------------------------------------------
# the three views
# ---------------------------------------------------------------------------


def _draw_map(app: App) -> None:
    if app.summary is None or app.rgba is None:
        _draw_missing_summary(app)
        return

    if _ui["fit_map"]:
        avail = imgui.get_content_region_avail()
        if avail.x > 1.0 and avail.y > 1.0:
            mapview.fit(app.map, avail.x, avail.y)
            _ui["fit_map"] = False

    try:
        # In the ECC mode the colour is an ECC state, so the reduction that
        # decides which block owns a shared pixel column must rank by ECC
        # severity too -- otherwise a measured "correction FAILED" block loses
        # its pixel to a neighbour with a rarer PAGE state and disappears.
        ranks = _ecc_ranks(app) if app.mode == palette.Mode.ECC else None
        event = mapview.draw(app.map, app.rgba, app.summary, WIN_MAP, ranks)
    except Exception:
        _draw_view_error("map", traceback.format_exc())
        return

    if event.hover is not None:
        # Hover text names the counts, never a single colour: the block colour
        # is a precedence pointer, not the claim.
        app.status = model.describe_block(app.summary, *event.hover)
        if app.mode == palette.Mode.ECC:
            # In the ECC mode the colour is an ECC state, so the sentence has
            # to be the ECC one -- including "0 of 1104 pages measured", which
            # is what most of this map honestly says.
            app.status = ecc_hover(app, *event.hover) + "   |   " + app.status
    if event.clicked is not None:
        ce, block = event.clicked
        select(app, ce, block, 0 if event.double_clicked else None)
    if event.context is not None:
        ce, block = event.context
        select(app, ce, block)
        blockview.request(app.block, app.worker, block, app.summary)


def _sidecar_wait_text() -> str | None:
    """One line describing the rebuild in flight, or None.  UI thread.

    The elapsed count is computed HERE rather than carried on the progress
    messages, so it ticks every frame instead of freezing between phases -- the
    Bootstrap mount alone runs ~50 s without logging anything, and a timer that
    sits still for that long is indistinguishable from a hang.
    """
    if not _ui.get("sidecar_running"):
        return None
    started = _ui.get("sidecar_started")
    secs = int(time.time() - started) if started else 0
    phase = str(_ui.get("sidecar_phase") or "starting")
    return "Rebuilding derived data (%ds): %s" % (secs, phase)


def _draw_missing_summary(app: App) -> None:
    """summary.npz is optional; say exactly what is missing and what still
    works, because a blank panel would read as 'no data on the drive'."""
    imgui.push_text_wrap_pos(0.0)
    wait = _sidecar_wait_text()
    if wait:
        # The rebuild is the answer to "why is there no map", so it goes ABOVE
        # the explanation of what is missing rather than below it.
        imgui.text_colored(palette.imgui_color(palette.NONE_RGBA),
                           "Building the drive map -- this is a one-time cost.")
        imgui.text_wrapped(wait)
        imgui.text_disabled(
            "summary.npz, msodescriptor.bin and cellmap.json are derived from "
            "the stores and are being rebuilt because they are not on disk. "
            "About 75 s in total, almost all of it mounting the FTL. They are "
            "cached when finished, so this happens once per dump, not once per "
            "launch. The stripe and the page inspector work meanwhile.")
        imgui.separator()
    imgui.text_colored(
        palette.imgui_color(palette.NONE_RGBA), "No drive map: summary.npz not loaded."
    )
    imgui.separator()
    if app.summary_error:
        imgui.text_wrapped(app.summary_error)
    else:
        imgui.text_wrapped(f"{SUMMARY_NPZ} could not be read.")
    imgui.separator()
    imgui.text_wrapped(
        "The block stripe and the page inspector still work: they read "
        "all.store and decoded.store directly. Only the 12 x 4000 overview "
        "needs the pre-built summary."
    )
    if _ui.get("cellmap_error"):
        imgui.text_wrapped(str(_ui["cellmap_error"]))
    elif isinstance(_ui.get("cellmap"), dict):
        imgui.text_disabled(
            "cellmap.json loaded: SLC/TLC page counts are still correct."
        )
    imgui.pop_text_wrap_pos()


def _draw_block(app: App) -> None:
    try:
        # The stripe honours the same colour mode as the drive map, so a block
        # the map flags (ECC not clean, say) can be opened and the offending
        # pages seen directly instead of paged through one at a time. The
        # worker goes with it: the ECC/MCRC modes need the decoded store's
        # per-page status, which is a job on its own coalescing key.
        event = blockview.draw(app.block, app.summary, WIN_BLOCK,
                               mode=app.mode, worker=app.worker)
    except Exception:
        _draw_view_error("block stripe", traceback.format_exc())
        return

    if event.hover is not None:
        ce, page = event.hover
        app.status = f"ce {ce} block {app.block.block} page {page}"
    if event.requested_block is not None:
        ce = app.selected[0] if app.selected else 1
        select(app, ce, event.requested_block)
    if event.clicked is not None:
        ce, page = event.clicked
        select(app, ce, app.block.block, page)


def _draw_page(app: App) -> None:
    try:
        event = pageview.draw(app.page, WIN_PAGE)
    except Exception:
        _draw_view_error("page inspector", traceback.format_exc())
        return

    if event.requested is not None:
        ce, block, page, _slc = event.requested
        select(app, ce, block, page)
    if event.copy_hex:
        rec = app.page.record
        if rec is not None and rec.data:
            imgui.set_clipboard_text(model.hex_dump(rec.data, 0, len(rec.data) // 16))
            app.status = f"copied {len(rec.data)} bytes as hex"
        else:
            app.status = "nothing to copy: the selected page has no bytes"
    if event.save_raw:
        _save_raw(app)


def _state_color(state: int) -> tuple[float, float, float, float]:
    """STATE_RGBA is a uint8 numpy row; imgui_color takes the 4-int tuple."""
    return palette.imgui_color(tuple(int(v) for v in palette.STATE_RGBA[state]))


def _draw_view_error(what: str, text: str) -> None:
    """A view raising must not kill the frame -- but it must also not be
    silent, or the panel would look like empty media.

    It is also recorded, not just drawn.  Catching a view exception into a
    tidy on-screen panel is what let a TypeError in the map's selection
    rectangle run on every frame while every smoke test still reported
    success: the frame loop never raised, so nothing above it could tell.
    The counter is what --self-test checks and what the status bar reports.
    """
    errors = _ui.get("view_errors")
    if not isinstance(errors, dict):
        errors = {}
        _ui["view_errors"] = errors
    errors[what] = text
    imgui.text_colored(
        _state_color(model.PageState.SKIPPED),
        f"{what} failed to draw",
    )
    imgui.push_text_wrap_pos(0.0)
    imgui.text_wrapped(text)
    imgui.pop_text_wrap_pos()


def _save_raw(app: App) -> None:
    rec = app.page.record
    if rec is None or not rec.data:
        app.status = "nothing to save: the selected page has no bytes"
        return
    name = f"ce{rec.ce}_blk{rec.block}_pg{rec.page}{'_slc' if rec.slc else ''}.bin"
    path = os.path.join(os.getcwd(), name)
    try:
        with open(path, "wb") as fh:
            fh.write(rec.data)
    except OSError as exc:
        app.status = f"save failed: {exc}"
        return
    _ui["last_saved"] = path
    app.status = f"wrote {len(rec.data)} bytes to {path}"


# ---------------------------------------------------------------------------
# legend + status
# ---------------------------------------------------------------------------


def draw_legend(app: App) -> None:
    """The five page states always, then the current mode's own classes.

    Drawing state_legend() unconditionally is the requirement, not a
    preference: in ERASES or WERU mode the map is coloured by something else
    entirely, and a reader who has lost the state vocabulary is exactly the
    reader who reports 190 MB of 0x00 as content.

    The mode legend describes the view the reader is looking at.  The map and
    the stripe now share the colour modes but not the classes: the map paints a
    block per cell and the stripe paints a page, so in ECC mode the map's rows
    are a log of what this session decoded while the stripe's are the decoded
    store's recorded status -- ten classes against thirteen, same vocabulary,
    different provenance.  Showing the map's legend under a focused stripe
    would put the wrong counts next to the right colours, so when the stripe
    has focus its own legend is what is drawn, headed with which view it
    describes.
    """
    imgui.text_disabled("page states (always shown)")
    _legend_row(palette.state_legend(), "states")

    if blockview.LAST_FOCUSED.get("focused"):
        heading, entries, note = blockview.stripe_legend(app.block, app.summary,
                                                         app.mode)
        if entries:
            imgui.separator()
            imgui.text_disabled(heading)
            _legend_row(entries, "stripe")
            if note:
                imgui.push_text_wrap_pos(0.0)
                imgui.text_disabled(note)
                imgui.pop_text_wrap_pos()
            return

    if app.summary is not None:
        entries = _ui.get("legend")
        if not isinstance(entries, list):        # first frame after a reload
            entries = palette.legend(app.summary, app.mode, ecc_map(app))
            _ui["legend"] = entries
        if entries:
            imgui.separator()
            imgui.text_disabled(f"drive map, mode: {palette.MODE_LABELS[app.mode]}"
                                " -- per BLOCK")
            _legend_row(entries, "mode")


def _legend_row(entries: list[palette.LegendEntry], key: str) -> None:
    columns = 3 if len(entries) <= 9 else 4
    if not imgui.begin_table(f"legend-{key}", columns):
        return
    for i, entry in enumerate(entries):
        imgui.table_next_column()
        imgui.push_id(f"{key}-{i}")
        imgui.color_button(
            entry.label, palette.imgui_color(entry.rgba), 0, imgui.ImVec2(16, 16)
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip(f"{entry.label}\n{entry.help}")
        imgui.same_line()
        # count == -1 marks a continuous ramp, where "how many blocks" is
        # meaningless; a 0 count is still printed, so a state that is currently
        # absent from the drive stays in the reader's vocabulary.
        if entry.count < 0:
            imgui.text(entry.label)
        else:
            imgui.text(f"{entry.label}  {entry.count:,}")
        if imgui.is_item_hovered():
            imgui.set_tooltip(entry.help)
        imgui.pop_id()
    imgui.end_table()


def draw_status(app: App) -> None:
    """Selection address, worker state, store errors, tier toggle, fps."""
    if app.selected is not None:
        ce, block = app.selected
        addr = f"ce {ce} / block {block}"
        if app.page.target is not None:
            addr += f" / page {app.page.target[2]}"
            addr += " (slc)" if app.page.target[3] else " (tlc)"
    else:
        addr = "no selection"
    imgui.text(addr)

    imgui.same_line()
    # The pool's own numbers, taken in one snapshot so they are consistent with
    # each other.  `stale` is the count of results the worker's per-key drop
    # gate discarded -- i.e. how many times a slow job finished after a newer
    # job for the same key, which under one thread could not happen and under N
    # is the difference between the right page and a lie.  It is on screen
    # because a silent correctness gate is one nobody notices has stopped
    # working.
    stats = app.worker.stats()
    imgui.text_disabled(
        f"  |  worker {stats['running']}/{stats['workers']} busy,"
        f" {stats['pending']} queued, {stats['stale_dropped']} stale dropped"
        f"  |  textures {gltex.texture_count()}"
        f"  |  decoded tier: {_decoded_tier_words(app)}"
        f"  |  hex font: {mono_font_name() or 'not loaded'}"
        f"  |  {imgui.get_io().framerate:.0f} fps"
    )

    if app.stores_status:
        imgui.text_disabled(app.stores_status)
    view_errors = _ui.get("view_errors")
    if isinstance(view_errors, dict) and view_errors:
        # A view that is drawing its error panel is a view that is showing no
        # data; say so here too, where the reader is already looking.
        imgui.text_colored(
            _state_color(model.PageState.SKIPPED),
            "view errors: " + ", ".join(sorted(view_errors)),
        )
    werr = app.worker.last_error()
    if werr:
        imgui.text_colored(_state_color(model.PageState.SKIPPED), f"last worker error: {werr}")
    wait = _sidecar_wait_text()
    if wait:
        # Also in the status bar: the map panel carries the full explanation,
        # but a user who is looking at the stripe or the inspector while this
        # runs would otherwise see no sign that anything is happening.
        imgui.text_colored(_state_color(model.PageState.PROGRAMMED), wait)
    if app.status:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped(app.status)
        imgui.pop_text_wrap_pos()


def _draw_palette_help() -> None:
    if not _ui["show_palette_help"]:
        return
    imgui.set_next_window_size(imgui.ImVec2(560, 320), imgui.Cond_.first_use_ever)
    opened, keep = imgui.begin("The five page states", True)
    if opened:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped(
            "These five are never merged. Merging them is how this dump was "
            "twice read wrong: 190 MB of 0x00 reported as content, and a "
            "metadata-driven skip that nearly lost the boot block."
        )
        imgui.separator()
        for state in (
            model.PageState.PROGRAMMED,
            model.PageState.ERASED,
            model.PageState.SKIPPED,
            model.PageState.ABSENT,
            model.PageState.ZERO,
            model.PageState.UNKNOWN,
        ):
            imgui.color_button(
                palette.STATE_LABELS[state], _state_color(state), 0, imgui.ImVec2(16, 16)
            )
            imgui.same_line()
            imgui.text_wrapped(
                f"{palette.STATE_LABELS[state]} -- {palette.STATE_HELP[state]}"
            )
        imgui.pop_text_wrap_pos()
    imgui.end()
    _ui["show_palette_help"] = bool(keep)


def _draw_ecc_help() -> None:
    """The ECC vocabulary, on demand, with the colour beside every state.

    It is a separate window from the page-state one because they are separate
    vocabularies: a page state says what the capture holds, an ECC state says
    what the DECODER did with it, and the pair "erased" appears in both meaning
    two different things (a zero-length record vs 0xFF bytes that no decoder
    will be asked about).
    """
    if not _ui.get("show_ecc_help"):
        return
    imgui.set_next_window_size(imgui.ImVec2(680, 460), imgui.Cond_.first_use_ever)
    opened, keep = imgui.begin("The ten ECC outcomes", True)
    if opened:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_wrapped(
            "The ECC is not the MCRC. The MCRC is a CRC over one unit's stored "
            "message; the ECC is an LDPC decode of the codewords that message "
            "is carried in. They cover different bytes and can disagree in "
            "both directions, so neither is ever computed from the other."
        )
        imgui.separator()
        for state in pagefmt.ECC_STATES:
            imgui.color_button(
                pagefmt.ECC_SHORT[state],
                palette.imgui_color(
                    tuple(int(v) for v in palette.ECC_RGBA[int(state)])),
                0, imgui.ImVec2(16, 16))
            imgui.same_line()
            imgui.text_wrapped("%s -- %s" % (pagefmt.ECC_LABEL[state],
                                             pagefmt.ECC_SENTENCE[state]))
            imgui.spacing()
        imgui.separator()
        imgui.text_wrapped(pagefmt.ECC_CAPABILITY)
        imgui.pop_text_wrap_pos()
    imgui.end()
    _ui["show_ecc_help"] = bool(keep)


def _reload_summary(app: App) -> None:
    # The path this process was started with, not the module default: reloading
    # must re-read the file the map is already showing, and an error message
    # must name that file too.
    path = str(_ui.get("summary_path") or SUMMARY_NPZ)
    try:
        app.summary = model.load_summary(path)
        app.summary_error = None
        app.status = f"summary reloaded from {path}"
    except model.MissingSummary as exc:
        app.summary = None
        app.summary_error = getattr(exc, "message", None) or str(exc)
        app.status = app.summary_error
    except Exception as exc:
        app.summary = None
        app.summary_error = f"Could not load {path}: {exc!r}"
        app.status = app.summary_error
    set_mode(app, app.mode)


# ---------------------------------------------------------------------------
# shutdown / main
# ---------------------------------------------------------------------------


def shutdown(app: App) -> None:
    """Fixed order: GL first (context still current on this thread), then the
    worker, which closes the mmapped stores on the thread that opened them."""
    try:
        gltex.destroy_all()
    except Exception:
        traceback.print_exc()
    app.map.tex = None
    app.block.tex = None
    try:
        app.worker.shutdown(wait=True, timeout=5.0)
    except Exception:
        traceback.print_exc()


def _report_store_choice(args) -> None:
    """Say which stores were opened, and what else was there.

    A working directory routinely holds several generations of a decode --
    `decoded.store`, `decoded2.store`, `decoded3.store` -- and the resolver in
    `nandgui/__init__.py` picks one by convention.  Picking silently is how a
    viewer ends up quietly disagreeing with the tool beside it, so the choice
    and the alternatives are printed whenever there was more than one.
    """
    for role, chosen in (("raw", args.raw), ("decoded", args.decoded)):
        cands = STORE_CANDIDATES.get(role) or []
        others = [c for c in cands if c != str(chosen).replace("\\", "/")]
        if others:
            print("%s store: using %s  (%d other store%s of this shape here: "
                  "%s).  Pin one with --%s or $TITANIA_%s_STORE."
                  % (role, chosen, len(others), "" if len(others) == 1 else "s",
                     ", ".join(os.path.basename(o) for o in others),
                     role, role.upper()), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nandgui", description="Titania NAND dump viewer"
    )
    # All three default to None and are resolved AFTER parsing, by
    # resolve_paths: an import-time default is computed against the working
    # directory, so naming only --raw used to leave the summary pointing at the
    # source checkout.  Naming any one of the three now places the other two.
    parser.add_argument(
        "--summary", default=None,
        help="per-block summary npz (default: summary.npz beside the stores; "
             "rebuilt from them if absent)")
    parser.add_argument(
        "--raw", default=None,
        help="raw page store, page_bytes %d (default: $TITANIA_RAW_STORE, else "
             "raw.store in the data directory, else any store of that shape)"
             % RAW_PAGE_BYTES)
    parser.add_argument(
        "--decoded", default=None,
        help="decoded page store, page_bytes %d (default: "
             "$TITANIA_DECODED_STORE, else decoded.store in the data "
             "directory, else any store of that shape)" % DECODED_PAGE_BYTES)
    parser.add_argument("--include-suspect", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=("worker threads, each with its own pair of open stores "
              "(default: $%s, else min(%d, cpu_count)). NOT the core count: "
              "measured, ECC throughput is flat from 1 to 16 threads. The pool "
              "is for concurrency -- an interactive stripe probe under two "
              "background ECC scans lands in 131 ms at 4 workers vs 497 ms at "
              "1 and 1024 ms at 2. Do not use 2."
              % (WORKERS_ENV, DEFAULT_MAX_WORKERS)),
    )
    parser.add_argument(
        "--self-test",
        type=int,
        default=0,
        metavar="N",
        help="draw N frames, then exit (verifies the build without a session)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    # Places the data directory from whatever was named and fills in the rest.
    # Must run before build(): everything downstream, including the sidecar
    # rebuilds, writes relative to the directory this settles on.
    args.raw, args.decoded, args.summary = resolve_paths(
        args.raw, args.decoded, args.summary)
    _report_store_choice(args)

    app = build(
        args.summary,
        raw_path=args.raw,
        decoded_path=args.decoded,
        include_suspect=args.include_suspect,
        workers=args.workers,
    )
    print("worker pool: %d threads, %d store pairs"
          % (app.worker.workers, app.worker.workers), file=sys.stderr)

    if args.self_test > 0:
        return _self_test(app, args.self_test)

    params = hello_imgui.RunnerParams()
    _ui["runner"] = params
    params.app_window_params.window_title = "Titania NAND"
    params.app_window_params.window_geometry.size = (1800, 1000)
    params.app_window_params.restore_previous_geometry = True

    # frame() builds its own dockspace and its own main menu bar, so
    # hello_imgui must not provide either.
    params.imgui_window_params.default_imgui_window_type = (
        hello_imgui.DefaultImGuiWindowType.no_default_window
    )
    params.imgui_window_params.show_menu_bar = False
    params.imgui_window_params.show_status_bar = False
    params.imgui_window_params.enable_viewports = False

    # Idling would delay the frame that consumes a worker Result, which is
    # exactly the frame the user is waiting on. A 5090 can afford vsync.
    params.fps_idling.enable_idling = False

    params.ini_filename = "nandgui.ini"

    # The hexdump must not depend on which runner started. hello_imgui loads its
    # own default font in this callback, so chain rather than replace: dropping
    # its loader would leave the shell with no default font at all.
    try:
        _hello_fonts = params.callbacks.load_additional_fonts

        def _load_fonts() -> None:
            if _hello_fonts is not None:
                _hello_fonts()
            _load_mono_font()

        params.callbacks.load_additional_fonts = _load_fonts
    except Exception:                                           # noqa: BLE001
        traceback.print_exc()

    params.callbacks.post_init = lambda: _enable_docking()
    # The only point at which the back buffer holds our finished frame and the
    # compositor has not touched it. See `_f12_check`.
    params.callbacks.before_swap = lambda: _capture_pending()
    params.callbacks.show_gui = lambda: _safe_frame(app)
    # before_exit still has a live GL context; before_exit_post_cleanup does not.
    params.callbacks.before_exit = lambda: shutdown(app)

    # hello_imgui positions its window against monitor #0 and aborts with
    # "could not use non existent monitor #0!" when glfw enumerates none. That
    # is the normal state of an rdp session: measured here, get_monitors() is
    # empty while a windowed create_window succeeds and renders on the real gpu
    # (nvidia rtx 5090, 549 fps). So an empty monitor list says nothing about
    # whether we can draw -- only that this runner cannot place a window.
    #
    # Falling back is not a degraded mode: _run_glfw drives the same frame(),
    # the same dockspace and the same backend the self-test already exercises
    # for hundreds of frames. Only the window placement differs.
    try:
        import glfw as _glfw
        _glfw.init()
        monitors = len(_glfw.get_monitors())
    except Exception:                                       # noqa: BLE001
        monitors = 0
    if monitors == 0:
        print("no monitors enumerated (typical over RDP) -- using the direct "
              "glfw runner instead of hello_imgui", file=sys.stderr)
        return _run_glfw(app)

    try:
        hello_imgui.run(params)
    except Exception:
        traceback.print_exc()
        shutdown(app)
        return 1
    return 0


def _save_framebuffer(gl, width: int, height: int,
                      path: str = "nandgui_frame.png") -> None:
    """Dump the back buffer to a PNG.  The arbiter for "is the blur ours".

    What reaches the screen goes through the desktop compositor and, on a remote
    session, a video codec. Neither is under this program's control, and both
    can blend colours that we drew as hard-edged cells. This writes the pixels
    as we rendered them, so the two can be compared instead of argued about.
    """
    import struct
    import zlib

    try:
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
        # Front, not back. This is called at the start of a frame, and by then
        # the runner has already cleared the back buffer -- reading it gave a
        # uniformly black PNG ("~1 distinct colours"), which looks like a
        # catastrophic rendering failure and is really just an empty canvas.
        # The front buffer holds the frame currently on screen, which is exactly
        # the picture the user pressed the key about.
        img = None
        for which in (gl.GL_BACK, gl.GL_FRONT):
            gl.glReadBuffer(which)
            buf = gl.glReadPixels(0, 0, width, height, gl.GL_RGB,
                                  gl.GL_UNSIGNED_BYTE)
            cand = np.frombuffer(buf, np.uint8).reshape(height, width, 3)[::-1]
            # An all-one-colour read means we caught a cleared buffer; try the
            # other one rather than writing a black file and calling it evidence.
            if len(set(map(tuple, cand.reshape(-1, 3)[::1021].tolist()))) > 1:
                img = cand
                break
        if img is None:
            print("screenshot: both buffers read as a single colour -- nothing "
                  "was rendered when the key was pressed", file=sys.stderr)
            return
        raw = b"".join(b"\x00" + img[y].tobytes() for y in range(height))

        def chunk(tag, data):
            c = struct.pack(">I", len(data)) + tag + data
            return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        with open(path, "wb") as h:
            h.write(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                                 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 6))
                    + chunk(b"IEND", b""))
        uniq = len(set(map(tuple, img.reshape(-1, 3)[::37].tolist())))
        print("wrote %s (%dx%d, ~%d distinct colours sampled)"
              % (path, width, height, uniq), file=sys.stderr)
    except Exception as exc:                                 # noqa: BLE001
        print("screenshot failed: %s: %s" % (type(exc).__name__, exc),
              file=sys.stderr)


def _run_glfw(app: App) -> int:
    """Visible window on the plain glfw backend, for sessions hello_imgui quits.

    The same loop as `_self_test`, minus the frame cap and the
    hidden-window hint, and using `_safe_frame` so a view exception shows in the
    status line rather than taking the window down mid-session.
    """
    import glfw
    import OpenGL.GL as gl
    from imgui_bundle.python_backends.glfw_backend import GlfwRenderer

    if not glfw.init():
        print("glfw could not initialise", file=sys.stderr)
        shutdown(app)
        return 2
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, gl.GL_TRUE)
    window = glfw.create_window(1800, 1000, "Titania NAND", None, None)
    if not window:
        glfw.terminate()
        print("no GL 3.3 context available", file=sys.stderr)
        shutdown(app)
        return 2
    glfw.make_context_current(window)
    glfw.swap_interval(1)            # vsync; the 5090 is not the bottleneck

    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= int(imgui.ConfigFlags_.docking_enable)
    io.set_ini_filename("nandgui.ini")
    _load_mono_font()                # before the backend builds its atlas texture
    impl = GlfwRenderer(window)

    # Say what we are actually drawing on. A window whose framebuffer is bigger
    # than its logical size (display scaling) is presented by stretching, and
    # the compositor's stretch is bilinear -- which looks exactly like the
    # texture filtering this viewer refuses to do, except we cannot switch it
    # off from in here. So the numbers go on stdout where they can be compared
    # against the picture rather than guessed at.
    ww, wh = glfw.get_window_size(window)
    fw_, fh = glfw.get_framebuffer_size(window)
    try:
        sx, sy = glfw.get_window_content_scale(window)
    except Exception:                                        # noqa: BLE001
        sx = sy = 1.0
    print("GL_RENDERER: %s" % (gl.glGetString(gl.GL_RENDERER) or b"?").decode(),
          file=sys.stderr)
    print("window %dx%d  framebuffer %dx%d  content scale %.2fx%.2f"
          % (ww, wh, fw_, fh, sx, sy), file=sys.stderr)
    if (fw_, fh) != (ww, wh):
        print("NOTE: framebuffer != window, so the desktop is SCALING this "
              "window. Cell colours will look blended no matter what filter we "
              "set -- that stretch happens after we are done drawing.",
              file=sys.stderr)
    print("F12 saves the raw framebuffer to nandgui_frame.png -- if that file "
          "is crisp but the screen is not, the blur is in the display path "
          "(RDP/scaling), not in this program.", file=sys.stderr)

    try:
        while not glfw.window_should_close(window):
            glfw.poll_events()
            impl.process_inputs()
            imgui.new_frame()
            _safe_frame(app)
            imgui.render()
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            impl.render(imgui.get_draw_data())
            _capture_pending()          # back buffer, before the swap
            glfw.swap_buffers(window)
    finally:
        shutdown(app)
        impl.shutdown()
        glfw.terminate()
    return 0


#: Pixels shaved off every edge of a canvas rect before it is read back.  The
#: quads are drawn at fractional widths and heights, so whether the boundary
#: pixel column is covered by the canvas or by the panel behind it depends on a
#: sub-pixel; one pixel in from every edge is unambiguously canvas.
_PURITY_INSET = 1

#: How many offending colours a failure message lists before it stops.
_PURITY_REPORT = 6


def _pack_rgb(rgb: np.ndarray) -> np.ndarray:
    """uint8[..., 3] -> uint32[...]: exact set membership on colours."""
    a = np.asarray(rgb, dtype=np.uint32)
    return (a[..., 0] << 16) | (a[..., 1] << 8) | a[..., 2]


def _read_canvas(gl, rect, fb_w: int, fb_h: int) -> np.ndarray | None:
    """-> uint8[h, w, 3] of the back buffer under an imgui screen rect.

    Call after impl.render() and before swap_buffers: that is the only moment
    the frame exists as pixels.  imgui screen coordinates run down from the top
    of the framebuffer and GL reads up from the bottom, hence the flip.
    """
    x, y, w, h = (float(v) for v in rect)
    x0 = max(int(math.floor(x + 0.5)) + _PURITY_INSET, 0)
    y0 = max(int(math.floor(y + 0.5)) + _PURITY_INSET, 0)
    x1 = min(int(math.floor(x + w + 0.5)) - _PURITY_INSET, fb_w)
    y1 = min(int(math.floor(y + h + 0.5)) - _PURITY_INSET, fb_h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
    raw = gl.glReadPixels(x0, fb_h - y1, x1 - x0, y1 - y0,
                          gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    arr = (np.frombuffer(raw, dtype=np.uint8)
           if isinstance(raw, (bytes, bytearray))
           else np.asarray(raw, dtype=np.uint8))
    if arr.size < (x1 - x0) * (y1 - y0) * 4:
        return None
    return arr[:(x1 - x0) * (y1 - y0) * 4].reshape(y1 - y0, x1 - x0, 4)[::-1, :, :3]


def _canvas_purity_problems(gl, fb_w: int, fb_h: int) -> list[str]:
    """Read the two canvases back and name every pixel that is not a colour.

    The RULE this enforces: anything drawn on top of a cell is opaque, because
    a blended overlay does not tint a cell, it invents a colour that is in no
    legend.  gltex.check_all() already proves GL is not interpolating the
    textures; this proves the overlays are not doing it either, which is the
    same defect one layer up and is how it came back three times -- as an alpha
    constant, as an anti-aliased stroke, and as a 1 px line on a half pixel.

    A canvas pixel must be one of:

      * a colour the picture itself paints -- for both views, the exact set of
        colours in the array that view uploaded this frame, which is why every
        colour mode is covered, hashed WERU hues and erase ramps included. The
        stripe used to be checked against palette.STATE_RGBA instead, because
        its texture was only ever page_colors(); it now honours the map's
        colour modes per page, so no static table can enumerate what it may
        legitimately paint. It falls back to STATE_RGBA if it ever publishes a
        rect with no image, which is the strictest of the two rules and never
        the more permissive one.
      * one of the overlay colours the view declares, in mapview.OVERLAY_COLORS
        and blockview.OVERLAY_COLORS.

    Anything else is a blend, and a blend is a measurement the reader cannot
    look up.  An overlay added without declaring it fails here too, which is
    deliberate: the declaration is the legend's copy of the truth.
    """
    specs: list[tuple[str, object, np.ndarray, tuple, str]] = []

    canvas = mapview.LAST_CANVAS
    image = canvas.get("image")
    if canvas.get("rect") is not None and image is not None:
        img = np.asarray(image)
        if img.ndim == 3 and img.shape[2] >= 3:
            specs.append((WIN_MAP, canvas["rect"],
                          np.unique(img.reshape(-1, img.shape[2])[:, :3], axis=0),
                          mapview.OVERLAY_COLORS,
                          "a colour of the map image uploaded this frame"))

    canvas = blockview.LAST_CANVAS
    if canvas.get("rect") is not None:
        image = canvas.get("image")
        img = None if image is None else np.asarray(image)
        if img is not None and img.ndim == 3 and img.shape[2] >= 3:
            specs.append((WIN_BLOCK, canvas["rect"],
                          np.unique(img.reshape(-1, img.shape[2])[:, :3], axis=0),
                          blockview.OVERLAY_COLORS,
                          "a colour of the stripe image uploaded this frame"))
        else:
            specs.append((WIN_BLOCK, canvas["rect"],
                          np.asarray(palette.STATE_RGBA)[:, :3],
                          blockview.OVERLAY_COLORS,
                          "a palette.STATE_RGBA colour"))

    out: list[str] = []
    for name, rect, picture, overlays, what in specs:
        px = _read_canvas(gl, rect, fb_w, fb_h)
        if px is None:
            continue
        allowed = np.vstack([np.asarray(picture, dtype=np.uint8),
                             np.asarray(overlays, dtype=np.uint8)])
        keys = _pack_rgb(px.reshape(-1, 3))
        bad = ~np.isin(keys, _pack_rgb(allowed))
        if not bad.any():
            continue
        vals, counts = np.unique(keys[bad], return_counts=True)
        order = np.argsort(-counts)
        lines = []
        for j in order[:_PURITY_REPORT]:
            rgb = np.array([(int(vals[j]) >> 16) & 255,
                            (int(vals[j]) >> 8) & 255,
                            int(vals[j]) & 255], dtype=np.int64)
            d = np.linalg.norm(allowed.astype(np.int64) - rgb, axis=1)
            k = int(np.argmin(d))
            near = tuple(int(v) for v in allowed[k])
            lines.append("      (%d,%d,%d) x%d -- nearest legal colour %s, "
                         "%.0f away" % (rgb[0], rgb[1], rgb[2], int(counts[j]),
                                        near, float(d[k])))
        out.append(
            "%s canvas has %d pixels in %d colours that are neither %s nor a "
            "declared opaque overlay -- i.e. an overlay blended with the cells "
            "under it and invented a colour that is in no legend:\n%s"
            % (name, int(bad.sum()), int(vals.size), what, "\n".join(lines)))
    return out


#: (frame, (ce, block, page)) -- one address per measured ECC outcome.  They
#: are real addresses on this capture, and each was verified by hand:
#:   1/0/0      fw-group2, 20/20 syndromes 0            -> CLEAN
#:   1/0/43     fw-group2, one payload bit, digest 4/5  -> CORRECTED
#:   1/10/40    pseudo-SLC ftl, 8 syndromes 0           -> CLEAN
#:   4/3642/918 native TLC ftl, syndrome 5, 1 flip      -> CORRECTED
#:   1/852/0    native TLC ftl, syndromes ~1000         -> UNCORRECTABLE
#:   1/1992/0   an all-0x00 block                       -> ZERO_PAGE
_SELF_TEST_PAGES = (
    (3, (1, 0, 0)),
    (8, (1, 0, 43)),
    (14, (1, 10, 40)),
    (20, (4, 3642, 918)),
    (28, (1, 852, 0)),
    (38, (1, 1992, 0)),
)
_SELF_TEST_SCAN_FRAME = 44


#: Keys used only by _pool_self_check.  They are distinct from every view key
#: and from _JOB_ECC / _JOB_STORES, so the checks cannot disturb -- or be
#: disturbed by -- real work already in the queue.
_JOB_TEST = "pool-self-check"


def _pool_self_check(app: App) -> list[str]:
    """Prove the pool's three load-bearing guarantees, on the real worker.

    None of these could fail with one thread, and all three can with N, so they
    are checked rather than argued about:

    1.  Every submitted key gets exactly one Result.  N keys in, N out.
    2.  Per-key result order.  A slow job and a fast job are put on the same
        key with the slow one already running, so with more than one worker
        they overlap and the slow one finishes last.  The invariant asserted is
        the one the views actually depend on: for a given key, the seqs
        reaching poll() are strictly increasing and the last one is the newest.
        A view that painted an out-of-order Result would be showing one page's
        bytes under another page's address, which is the failure this whole
        viewer exists to prevent.

        It is not asserted that the older Result is dropped.  At
        workers=1 the two serialise, the slow one is delivered before the fast
        one is even started, and both arriving in order is correct -- there is
        nothing stale to drop.  At N>1 the drop gate is what produces the same
        invariant, and `stale_dropped` counts the difference.  Asserting the
        mechanism instead of the invariant would fail a correct pool.
    3.  broadcast() reaches every ctx and posts exactly one Result.  A
        submitted ctx mutation would reach 1 of N and leave the rest serving
        the old store with nothing on screen saying so.
    """
    problems: list[str] = []
    worker = app.worker
    n = worker.workers

    def take(out: list[Result]) -> None:
        """Poll once, keeping our Results and giving the app back its own.

        This check runs before the frame loop, so it is the only thing polling
        the worker -- and build() has already queued the store-status job.
        Simply discarding what came back would leave the status bar reporting
        "stores: unknown" for a session in which the stores opened perfectly:
        the check would be silently degrading the thing it is checking.
        """
        for result in worker.poll(max_results=256):
            if isinstance(result.key, tuple) and result.key[:1] == (_JOB_TEST,):
                out.append(result)
            else:
                _dispatch(app, result)

    def drain(seconds: float = 20.0) -> list[Result]:
        """Collect Results until the pool is idle and the queue is empty."""
        out: list[Result] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            take(out)
            stats = worker.stats()
            if not stats["pending"] and not stats["running"] and not stats["results"]:
                # One more pass: a result can land between the poll and the
                # stats read, and losing it would look like a dropped job.
                time.sleep(0.02)
                take(out)
                if not worker.stats()["pending"] and not worker.stats()["running"]:
                    return out
            time.sleep(0.005)
        take(out)
        return out

    drain(5.0)  # clear whatever build() already queued

    # --- 1. N keys in, N out --------------------------------------------
    keys = [(_JOB_TEST, "fan", i) for i in range(max(4, n * 2))]
    for i, key in enumerate(keys):
        worker.submit(key, (lambda _ctx, v=i: v), priority=PRIORITY_BACKGROUND)
    got = {r.key: r for r in drain() if r.key in set(keys)}
    if len(got) != len(keys):
        problems.append(
            "pool: submitted %d distinct keys and %d Results came back -- a "
            "job was lost or a key was coalesced against a different key"
            % (len(keys), len(got)))
    wrong = [k for k in keys if k in got and got[k].value != k[2]]
    if wrong:
        problems.append("pool: %d keys came back with another key's value: %r"
                        % (len(wrong), wrong[:4]))

    # --- 2. per-key result order ----------------------------------------
    order_key = (_JOB_TEST, "order")
    stale_before = worker.stale_dropped()
    slow_seq = worker.submit(order_key,
                             (lambda _ctx: (time.sleep(0.40), "SLOW")[1]),
                             priority=PRIORITY_INTERACTIVE)
    # Wait for a worker to actually pick it up. Submitting the second job
    # before that would coalesce the two (which is also correct behaviour, and
    # would test nothing), so the overlap has to be forced.
    deadline = time.monotonic() + 5.0
    while worker.stats()["running"] < 1 and time.monotonic() < deadline:
        time.sleep(0.002)
    fast_seq = worker.submit(order_key, (lambda _ctx: "FAST"),
                             priority=PRIORITY_INTERACTIVE)
    seqs = [r.seq for r in drain() if r.key == order_key]
    if slow_seq > 0 and fast_seq > slow_seq:
        if seqs != sorted(set(seqs)):
            problems.append(
                "pool: results for %r reached poll() OUT OF ORDER (%r, with "
                "the 0.40 s job at seq %d and the fast one at seq %d) -- a "
                "view would paint the older page's bytes under the newer "
                "page's address"
                % (order_key, seqs, slow_seq, fast_seq))
        if not seqs or seqs[-1] != fast_seq:
            problems.append(
                "pool: the NEWEST result for %r (seq %d) was not the last one "
                "delivered; got %r" % (order_key, fast_seq, seqs))
    dropped = worker.stale_dropped() - stale_before
    print("self-test: pool %d workers -- per-key order check dropped %d stale "
          "result(s) (0 is correct at workers=1, where the two serialise)"
          % (n, dropped))

    # --- 3. broadcast reaches every ctx ---------------------------------
    hit: list[int] = []
    hit_lock = threading.Lock()

    def _touch(_ctx, seen=hit, lk=hit_lock):
        with lk:
            seen.append(threading.get_ident())
        return len(seen)

    bcast_key = (_JOB_TEST, "broadcast")
    worker.broadcast(bcast_key, _touch, priority=PRIORITY_INTERACTIVE)
    results = [r for r in drain() if r.key == bcast_key]
    with hit_lock:
        idents = list(hit)
    if len(idents) != n or len(set(idents)) != n:
        problems.append(
            "pool: broadcast ran on %d of %d workers (%d distinct threads) -- "
            "a ctx mutation would leave the others serving the old store"
            % (len(idents), n, len(set(idents))))
    if len(results) != 1:
        problems.append(
            "pool: broadcast posted %d Results, not 1 -- app._dispatch acts "
            "once per Result, so N of them means one user action applied N "
            "times" % (len(results),))
    return problems


def _self_test(app: App, frames: int) -> int:
    """Draw `frames` real frames into a hidden window, then exit.

    A build check, not a demo.  It does not go through
    hello_imgui: that runner enumerates monitors and refuses to start in a
    session that has none (a service account, a detached ssh shell, CI), which
    would make the check unrunnable exactly where it is most wanted.  A hidden
    glfw window plus the python imgui backend exercises everything that
    matters anyway -- the dock builder, the menu bar, every widget in all
    three views, both texture uploads, the worker round trip and shutdown.

    frame() is called directly rather than through _safe_frame() so that a
    raised exception fails the test instead of being turned into a status
    line.
    """
    import glfw
    import OpenGL.GL as gl
    from imgui_bundle.python_backends.glfw_backend import GlfwRenderer

    # Colour separation is a correctness property, not a matter of taste: the
    # five-state legend is pinned in every mode, so a mode colour that matches
    # a state swatch by eye degrades the vocabulary the whole viewer rests on.
    # Check it before drawing anything.
    violations = palette._check_mode_color_separation()
    if violations:
        for line in violations:
            print("self-test FAILED: palette separation: " + line,
                  file=sys.stderr)
        shutdown(app)
        return 1

    # The worker pool's guarantees, before a single frame is drawn. They are
    # invisible from the picture -- a stale Result paints a plausible page, not
    # a broken one -- so nothing later in this test could catch them.
    pool_problems = _pool_self_check(app)
    if pool_problems:
        for line in pool_problems:
            print("self-test FAILED: " + line, file=sys.stderr)
        shutdown(app)
        return 1

    if not glfw.init():
        print("self-test: glfw could not initialise", file=sys.stderr)
        shutdown(app)
        return 2
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, gl.GL_TRUE)
    window = glfw.create_window(1900, 1050, "nandgui self-test", None, None)
    if not window:
        glfw.terminate()
        print("self-test: no GL 3.3 context available", file=sys.stderr)
        shutdown(app)
        return 2
    glfw.make_context_current(window)

    imgui.create_context()
    io = imgui.get_io()
    io.config_flags |= int(imgui.ConfigFlags_.docking_enable)
    io.set_ini_filename("")          # never write an ini from a test
    # The self-test draws the same hexdumps the session does; loading the font
    # here is what makes the push/pop in pageview covered by the check.
    _load_mono_font()
    impl = GlfwRenderer(window)

    failure: str | None = None
    # V1 regression gate: the map and the stripe must sample with hard edges.
    # Checked inside the loop because shutdown() -> gltex.destroy_all() runs in
    # the finally below, and a destroyed texture has no sampler state left to
    # measure. Reading it back out of GL (rather than trusting the upload call)
    # is the only way a driver default or a stray glTexParameteri elsewhere
    # would ever be caught: GL_LINEAR here averages two adjacent NAND cells
    # into a colour that belongs to no page state.
    sampler_problems: list[str] = []
    sampler_frame = -1
    # V2 regression gate, one layer above the sampler check: no pixel of either
    # canvas may be a blend. See _canvas_purity_problems. Started a few frames
    # in, once the dockspace has a layout and both views have drawn a real quad.
    purity_problems: list[str] = []
    purity_frame = -1
    purity_first_frame = 5
    try:
        for i in range(int(frames)):
            glfw.poll_events()
            impl.process_inputs()
            imgui.new_frame()
            # Walk every colour mode: colorize() and legend() have a branch per
            # mode and a mode that raises would otherwise pass unnoticed.
            if i and i % 2 == 0:
                set_mode(app, palette.MODES[(i // 2) % len(palette.MODES)])
            # Walk one page of every ECC outcome this drive is known to hold,
            # so the ECC panel is drawn for each of them rather than only for
            # whichever page happens to be selected. These addresses are
            # MEASURED (see pagefmt's ECC survey): group-2 clean, group-2
            # single-bit corrected, EE3 clean, EE3 corrected, EE3
            # uncorrectable, and an all-0x00 page where ECC does not apply.
            for at, target in _SELF_TEST_PAGES:
                if i == at:
                    select(app, *target)
            # NB the help windows are not opened here: they float
            # over the stripe canvas, and the purity gate below reads that
            # canvas back expecting only palette colours. A floating window
            # over it fails the gate for a reason that has nothing to do with
            # the map. They are covered by the scratch harness instead.
            if i == _SELF_TEST_SCAN_FRAME:
                # And one real ECC scan, so the map's ECC mode is exercised
                # with something measured in it instead of an all-NOT_MEASURED
                # picture that every colour would pass.
                _ecc_scan_request(app, [(1, 0, True), (1, 852, False)], 3,
                                  "self-test scan")
            try:
                frame(app)
            except Exception:
                failure = traceback.format_exc()
                imgui.end_frame()
                break
            imgui.render()
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            impl.render(imgui.get_draw_data())
            if not sampler_problems:
                # ~6 glGetTexParameteriv per live texture (there are 2-3), so
                # running it every frame is free and covers every mode switch,
                # every re-upload and every reallocation the loop above makes.
                sampler_problems = gltex.check_all()
                if sampler_problems:
                    sampler_frame = i
            if not purity_problems and i >= purity_first_frame:
                fb_w, fb_h = glfw.get_framebuffer_size(window)
                purity_problems = _canvas_purity_problems(gl, int(fb_w),
                                                          int(fb_h))
                if purity_problems:
                    purity_frame = i
            glfw.swap_buffers(window)
            time.sleep(0.01)         # let worker results land between frames
    finally:
        shutdown(app)
        impl.shutdown()
        glfw.terminate()

    werr = app.worker.last_error()
    view_errors = _ui.get("view_errors") or {}
    print("self-test: %d frames, summary %s, stores: %s"
          % (frames, "loaded" if app.summary is not None else "MISSING",
             app.stores_status or "unknown"))
    if failure:
        print("self-test FAILED:\n" + failure, file=sys.stderr)
        return 1
    if view_errors:
        # frame() itself never raises for these -- _draw_map/_draw_block/
        # _draw_page catch a view exception and draw it. Without this check a
        # view that drew nothing but a traceback would still "pass".
        for what, text in sorted(view_errors.items()):
            print("self-test FAILED: %s drew an error panel:\n%s" % (what, text),
                  file=sys.stderr)
        return 1
    if werr:
        print("self-test FAILED: worker error:\n" + werr, file=sys.stderr)
        return 1
    if sampler_problems:
        for line in sampler_problems:
            print("self-test FAILED: soft texture filtering at frame %d: %s"
                  % (sampler_frame, line), file=sys.stderr)
        return 1
    if purity_problems:
        for line in purity_problems:
            print("self-test FAILED: blended overlay at frame %d: %s"
                  % (purity_frame, line), file=sys.stderr)
        return 1

    # Layout overflow. A view whose bottom is under a scrollbar draws no error
    # and raises nothing, so it passed every check above; the default layout
    # has to fit the default window or the reader loses CE11 and the last
    # honesty line without being told. Vertical overflow of the two map/stripe
    # views is a failure. Horizontal overflow is reported but not fatal: the
    # Page panel's unwrapped header lines are a separate defect with a separate
    # owner, and turning them into a red build failure here would only stop
    # everyone else's work.
    overflow = _ui.get("overflow")
    overflow = overflow if isinstance(overflow, dict) else {}
    for name in (WIN_MAP, WIN_BLOCK, WIN_PAGE):
        over_y, over_x = overflow.get(name, (0.0, 0.0))
        if over_x > 0.5:
            print("self-test: %s scrolls horizontally by %.0f px (content is "
                  "wider than the panel)" % (name, over_x))
    clipped = [(name, overflow.get(name, (0.0, 0.0))[0])
               for name in (WIN_MAP, WIN_BLOCK)
               if overflow.get(name, (0.0, 0.0))[0] > 0.5]
    if clipped:
        for name, over_y in clipped:
            print("self-test FAILED: %s is %.0f px taller than its region -- "
                  "its bottom is cut off behind a scrollbar in the default "
                  "layout" % (name, over_y), file=sys.stderr)
        return 1
    print("self-test OK")
    return 0


def _enable_docking() -> None:
    io = imgui.get_io()
    io.config_flags |= int(imgui.ConfigFlags_.docking_enable)


def _diagnostics_once() -> None:
    """Print what we are actually drawing on. Called from the frame, so it runs
    in both runners.

    It used to live in `_run_glfw` only -- and `_run_glfw` is the fallback, taken
    only when glfw enumerates no monitors. The moment this session started
    reporting a monitor again the app went back to hello_imgui and the
    diagnostics silently stopped existing. A diagnostic that is only present on
    one of two code paths is worse than none: its absence reads as "nothing to
    report".

    `display_framebuffer_scale` is the number that matters for the blur
    complaint: if it is not 1.0 the whole UI is rendered at one size and
    presented at another, and that resample is bilinear no matter what filter we
    set on our own textures.
    """
    if _ui.get("diag_done"):
        return
    _ui["diag_done"] = True
    try:
        import OpenGL.GL as gl

        io = imgui.get_io()
        fbs = io.display_framebuffer_scale
        print("GL_RENDERER: %s"
              % (gl.glGetString(gl.GL_RENDERER) or b"?").decode(),
              file=sys.stderr)
        print("display_size %.0fx%.0f  display_framebuffer_scale %.2fx%.2f  "
              "font_global_scale %.2f"
              % (io.display_size.x, io.display_size.y, fbs.x, fbs.y,
                 getattr(io, "font_global_scale", 1.0)), file=sys.stderr)
        if abs(fbs.x - 1.0) > 0.001 or abs(fbs.y - 1.0) > 0.001:
            print("NOTE: framebuffer scale is not 1.0 -- the UI is rendered at "
                  "one resolution and presented at another. That resample is "
                  "bilinear and happens after we finish drawing, so it will "
                  "blur cell colours whatever filter our textures carry.",
                  file=sys.stderr)
        print("F12 saves the raw framebuffer to nandgui_frame.png. If that file "
              "is crisp while the screen is not, the blur is in the display "
              "path, not in this program.", file=sys.stderr)
    except Exception as exc:                                 # noqa: BLE001
        print("diagnostics failed: %s" % exc, file=sys.stderr)


def _f12_check() -> None:
    """Arm a framebuffer dump when F12 is pressed.  Both runners.

    It only arms.  The capture itself happens in `_capture_pending`, called
    before the swap, and that distinction is the whole point:

    * at the start of a frame the back buffer has already been cleared, so
      reading it gives a black PNG;
    * the front buffer of a VISIBLE window belongs to the desktop compositor.
      Measured here: the same scene read from a hidden window's back buffer has
      234 distinct colours, and from a visible window 311 -- the extra ones are
      the compositor's resample, not ours. A capture from the front buffer
      therefore photographs the display path and looks exactly like a rendering
      bug in this program, which is precisely the wrong conclusion.

    Before the swap, the back buffer holds what we drew and nothing else.
    """
    try:
        if imgui.is_key_pressed(imgui.Key.f12, False):
            _ui["capture_pending"] = True
    except Exception:                                        # noqa: BLE001
        pass


def _capture_pending() -> None:
    """Write the armed dump from the back buffer, before the swap."""
    if not _ui.get("capture_pending"):
        return
    _ui["capture_pending"] = False
    try:
        import OpenGL.GL as gl

        io = imgui.get_io()
        fbs = io.display_framebuffer_scale
        w = int(io.display_size.x * (fbs.x or 1.0))
        h = int(io.display_size.y * (fbs.y or 1.0))
        _save_framebuffer(gl, w, h)
        print("captured from the BACK buffer, before the compositor sees it -- "
              "this is what this program drew", file=sys.stderr)
    except Exception as exc:                                 # noqa: BLE001
        print("capture failed: %s" % exc, file=sys.stderr)


def _safe_frame(app: App) -> None:
    """A raised exception inside show_gui leaves imgui's window stack
    unbalanced and takes the process down; the shell would rather show the
    traceback in the status line and keep the last good frame."""
    _diagnostics_once()
    _f12_check()
    try:
        frame(app)
    except Exception:
        app.status = "frame error:\n" + traceback.format_exc()
        print(app.status, file=sys.stderr)
        time.sleep(0.05)  # do not spin at 500 fps printing the same traceback


if __name__ == "__main__":
    raise SystemExit(main())
