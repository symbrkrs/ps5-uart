"""nandgui -- an interactive viewer for the Titania NAND dump.

This module is the package root and does almost nothing: it resolves the
filesystem paths, re-exports the drive geometry from `titania.geometry` under
the names the views use, and is the only place that puts `titania` on the import
path. Every other module takes its paths and geometry from here, so a relocated
store is a one-line change and the viewer can never disagree with the decoder
about the shape of the part.

It imports nothing from the sibling nandgui modules. pagefmt.py imports this,
model.py imports pagefmt, palette.py imports model, the views import palette --
a strict one-way chain with no cycles, which is also what lets pagefmt.py,
model.py and palette.py be imported headless (no imgui, no GL) for tests.

Geometry note for reviewers: the page-state vocabulary (programmed / erased /
skipped / absent / all-zero) lives in model.PageState and palette.STATE_RGBA,
not here. What lives here are the numbers those states are counted over --
notably PAGES_SLC vs PAGES_TLC, because a stripe row that is 368 pages long
inside a 1104-wide texture must be padded with ABSENT ("we never looked"), and
that padding is only correct if both lengths come from one authority.
"""

from __future__ import annotations

import os
import sys

__version__ = "0.1.0"

__all__ = [
    "TITANIA_PKG",
    "DATA_DIR",
    "RAW_STORE",
    "DECODED_STORE",
    "SUMMARY_NPZ",
    "CELLMAP_JSON",
    "WERU_TABLE_JSON",
    "adopt_data_dir",
    "resolve_paths",
    "N_CE",
    "N_BLOCK",
    "N_ROWS",
    "PAGES_SLC",
    "PAGES_TLC",
    "RAW_PAGE_BYTES",
    "DECODED_PAGE_BYTES",
    "AUS_PER_PAGE",
    "AU_AUX",
    "AU_DATA",
    "WERU_BIN_COUNT",
    "ensure_titania_on_path",
    "__version__",
]

# --------------------------------------------------------------------------
# Filesystem
#
# Nothing HERE names a machine. Every path is resolved at import from the
# environment and from what is actually on disk, because this module used to
# carry one operator's drive letter and one WSL share -- which made the viewer
# unrunnable anywhere else and, worse, made it fail with "0 records" rather
# than with "I do not know where your data is".
#
#   $TITANIA_NAND_DIR   the directory holding the stores and summary.npz
#   $TITANIA_PKG        the titania checkout, if it is not an installed package
#
# Resolution never raises and never blocks at import: an unresolvable path
# stays a plausible-looking default, the GUI reports that it could not be read,
# and --raw / --decoded / --summary override all of it.
# --------------------------------------------------------------------------

#: Where the titania package lives when it is not installed. Empty means "it is
#: an ordinary import", which is the normal case after `pip install -e .`.
TITANIA_PKG = os.environ.get("TITANIA_PKG", "")

#: Raw pages, as the NAND hands them over: 16384 data + 2048 spare.
RAW_PAGE_BYTES_EXPECTED = 18432
#: Decoded pages: 4 AU x (8 aux + 4096 user).
DECODED_PAGE_BYTES_EXPECTED = 16416


def _data_dir() -> str:
    """The directory holding the stores.  -> str, forward slashes.

    $TITANIA_NAND_DIR, else the working directory. Not validated: a wrong
    setting must fail where it is used, naming the path the operator gave,
    rather than silently falling back to somewhere that happens to exist.
    """
    return (os.environ.get("TITANIA_NAND_DIR") or os.getcwd()).replace("\\", "/")


def _store_page_bytes(path: str):
    """`store.json`'s page_bytes for a directory, or None if it is not a store."""
    try:
        with open(os.path.join(path, "store.json"), "rb") as h:
            # Only the head is needed and these files can be large; page_bytes
            # is written near the front by `StoreWriter.finish`.
            import json
            return int(json.loads(h.read().decode("utf-8"))["page_bytes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


#: Every store in DATA_DIR that could have served each role, {role: [paths]}.
#: Filled by `_find_store` and reported by the UI, because a directory holding
#: several generations of a decode is normal during analysis and picking one
#: silently is how a viewer ends up disagreeing with the tool beside it.
STORE_CANDIDATES: dict = {"raw": [], "decoded": []}


def _find_store(role: str, env_var: str, data_dir: str, want_bytes: int,
                *preferred: str) -> str:
    """The store to use for a role.  -> str

    Order, most explicit first:
      1. `$env_var`, used verbatim and never second-guessed;
      2. a conventional name, in the order given;
      3. any directory in `data_dir` whose `store.json` has the right
         page_bytes -- classified by shape, since a store's name is whatever
         the build that made it chose.

    Raw and decoded pages are different sizes and `DumpDevice` refuses to open a
    store whose page_bytes disagrees, so a shape match cannot confuse the two
    roles. It can confuse two generations of the same role: a working directory
    routinely holds `decoded.store`, `decoded2.store`, `decoded3.store`. Every
    match is therefore recorded in `STORE_CANDIDATES` so the UI can say which
    was chosen and what else was there, and the operator can pin it with the
    environment variable or with --raw / --decoded.

    Never raises. When nothing matches it returns the conventional path, so the
    error the operator sees names a file rather than an empty search.
    """
    try:
        entries = sorted(os.listdir(data_dir))
    except OSError:
        entries = []
    found = [os.path.join(data_dir, n).replace("\\", "/") for n in entries
             if os.path.isdir(os.path.join(data_dir, n))
             and _store_page_bytes(os.path.join(data_dir, n)) == want_bytes]
    STORE_CANDIDATES[role] = found

    pinned = os.environ.get(env_var)
    if pinned:
        return pinned.replace("\\", "/")
    for name in preferred:
        cand = os.path.join(data_dir, name).replace("\\", "/")
        if cand in found:
            return cand
    return found[0] if found else os.path.join(
        data_dir, preferred[0]).replace("\\", "/")


DATA_DIR = _data_dir()

#: The raw capture: one record per address, source name preserved per record
#: ("smart-dump", "retry-level-1", ...).
RAW_STORE = _find_store("raw", "TITANIA_RAW_STORE", DATA_DIR,
                        RAW_PAGE_BYTES_EXPECTED,
                        "raw.store", "final.store", "all.store")

#: The decode of the raw store, by one classifier over every record. The
#: record's source name is the decode status, expanded by the store's own
#: `store.json["status_vocabulary"]`, so this viewer needs no built-in list of
#: decode outcomes and a store that adds one is read without a code change.
DECODED_STORE = _find_store("decoded", "TITANIA_DECODED_STORE", DATA_DIR,
                            DECODED_PAGE_BYTES_EXPECTED,
                            "decoded.store", "final_decoded.store")

#: The per-block summary the drive map renders from. Built by
#: `titania.summary`; rebuild it whenever a store is rebuilt. It is a cache of
#: what the stores say, and a stale one is not a missing feature -- the map
#: shades from it and, unlike the block panel, carries no label saying the
#: numbers are old, so it quietly disagrees with the page inspector beside it.
SUMMARY_NPZ = os.path.join(DATA_DIR, "summary.npz").replace("\\", "/")
CELLMAP_JSON = os.path.join(DATA_DIR, "cellmap.json").replace("\\", "/")
WERU_TABLE_JSON = os.path.join(DATA_DIR, "weru_table.json").replace("\\", "/")


def adopt_data_dir(*hints: str) -> str:
    """Point the sidecar lookups at the directory the stores really live in.

    --raw / --decoded / --summary override their own paths and nothing else,
    while `cellmap.json`, `weru_table.json` and `msodescriptor.bin` resolve
    against DATA_DIR, which defaults to the WORKING directory.  Running the
    viewer from the checkout against a dump on another volume therefore looked
    for the sidecars next to the SOURCE and reported "no msoDescriptor sidecar"
    for a file that exists, sitting beside the stores the operator named.

    The directory holding the stores is the directory holding their sidecars,
    so derive it from whatever was named.  Each hint is a store directory or a
    file inside the data directory; `dirname` is right for both, because a
    store is itself a directory one level down.

    $TITANIA_NAND_DIR still wins -- it is the explicit pin and an operator who
    set it meant it.  Never raises; returns the directory in use.
    """
    global DATA_DIR, CELLMAP_JSON, WERU_TABLE_JSON, SUMMARY_NPZ
    if os.environ.get("TITANIA_NAND_DIR"):
        return DATA_DIR
    for hint in hints:
        if not hint:
            continue
        d = os.path.dirname(os.path.abspath(str(hint)))
        if not os.path.isdir(d):
            continue
        DATA_DIR = d.replace("\\", "/")
        SUMMARY_NPZ = os.path.join(DATA_DIR, "summary.npz").replace("\\", "/")
        CELLMAP_JSON = os.path.join(DATA_DIR, "cellmap.json").replace("\\", "/")
        WERU_TABLE_JSON = os.path.join(
            DATA_DIR, "weru_table.json").replace("\\", "/")
        # pagefmt captured its path by value at import, so rebind it here, and
        # drop the descriptor cache: a MISS is cached too, and the miss on
        # record was measured against the directory we just replaced.
        try:
            from . import pagefmt
            pagefmt.MSO_DESCRIPTOR_BIN = os.path.join(
                DATA_DIR, "msodescriptor.bin").replace("\\", "/")
            pagefmt._DESC.clear()
        except Exception:                                    # noqa: BLE001
            pass
        break
    return DATA_DIR


def resolve_paths(raw=None, decoded=None, summary=None):
    """Fill in whichever of raw / decoded / summary the operator did not name.

    -> (raw, decoded, summary), all three absolute.

    ORDER IS THE WHOLE POINT.  The module-level RAW_STORE / DECODED_STORE /
    SUMMARY_NPZ are resolved at IMPORT, against the working directory, which is
    only the right answer when the viewer is run from the dump directory.  Used
    as argparse defaults they are captured before the command line is read, so
    `--raw E:/dump/raw.store` with no `--summary` left the summary pointing into
    the SOURCE CHECKOUT -- where it was not found, and where the rebuild job
    would then have written a fresh 64 s summary.npz.

    So: adopt the data directory from whatever WAS named, then resolve the rest
    against it.  Naming any one of the three is enough to place the other two.
    """
    adopt_data_dir(*[p for p in (raw, decoded, summary) if p])
    # Both branches run _find_store even when the path was given, because
    # filling STORE_CANDIDATES is its other job and _report_store_choice prints
    # from it.  Skipping it for a named store left the candidate list holding
    # the IMPORT-TIME scan of the working directory, so starting the viewer
    # against a dump on E: announced "1 other store of this shape here" and
    # named a store in the source checkout.
    found_raw = _find_store("raw", "TITANIA_RAW_STORE", DATA_DIR,
                            RAW_PAGE_BYTES_EXPECTED,
                            "titania_raw.store", "raw.store", "final.store",
                            "all.store")
    found_decoded = _find_store("decoded", "TITANIA_DECODED_STORE", DATA_DIR,
                                DECODED_PAGE_BYTES_EXPECTED,
                                "titania_decoded.store", "decoded.store",
                                "final_decoded.store")
    if raw is None:
        raw = found_raw
    if decoded is None:
        decoded = found_decoded
    if summary is None:
        summary = SUMMARY_NPZ
    return raw, decoded, summary

# --------------------------------------------------------------------------
# sys.path
# --------------------------------------------------------------------------

_PATH_HELP = (
    "The titania package could not be imported.\n"
    "Install it (`pip install -e .` from the titania checkout), or set "
    "$TITANIA_PKG to the directory that CONTAINS the `titania` package -- for "
    "a checkout served over a network or VM share, that is the share path.\n"
    "{extra}"
    "Without it nandgui can still show summary.npz, but no page can be read or "
    "decoded."
)


def ensure_titania_on_path() -> None:
    """Make `import titania` work.  Idempotent -- call it freely.

    An ordinary import first. This used to insert one hard-coded share path and
    nothing else, which meant the viewer could only run on the machine that
    path existed on, and meant an installed titania was ignored in favour of a
    checkout that might not be there.

    $TITANIA_PKG is the escape hatch for a checkout that is not installed. It is
    probed with `isdir` before being used, because an unreachable share fails
    much later as a bare ModuleNotFoundError from inside a worker job, where the
    real cause -- the vm is not running -- is invisible.

    Raises ImportError with a human sentence when neither route works.
    """
    import importlib.util

    if importlib.util.find_spec("titania") is not None:
        return

    extra = ""
    if TITANIA_PKG:
        # isdir returns False for a dead network path rather than raising, but a
        # transient error can raise OSError; both mean "not reachable".
        try:
            reachable = os.path.isdir(TITANIA_PKG)
        except OSError:                 # pragma: no cover - depends on the share
            reachable = False
        if reachable:
            if TITANIA_PKG not in sys.path:
                sys.path.insert(0, TITANIA_PKG)
            importlib.invalidate_caches()
            if importlib.util.find_spec("titania") is not None:
                return
            extra = ("$TITANIA_PKG=%s exists but holds no `titania` package.\n"
                     % TITANIA_PKG)
        else:
            extra = ("$TITANIA_PKG=%s is not reachable. If it is a VM or network "
                     "share, the host must be running.\n" % TITANIA_PKG)
    raise ImportError(_PATH_HELP.format(extra=extra))


# --------------------------------------------------------------------------
# Drive geometry
# --------------------------------------------------------------------------

# Every number here comes from titania.geometry, which is the package that owns
# the drive's shape. The viewer keeps its own spellings because the views read
# better with them (N_CE, N_BLOCK, a row index into a 48000-row table), but not
# its own values -- a second copy of the geometry would let the map and the
# decoder disagree about the part.
#
# titania.geometry imports nothing heavy, not even numpy. titania.ftl costs
# ~0.15 s, which is nothing against the OpenGL and imgui this GUI loads anyway,
# and WERU_BIN_COUNT is an allocation-layer fact that belongs to ftl rather than
# to the drive's shape.
ensure_titania_on_path()
from titania import geometry as _geom          # noqa: E402
from titania import ftl as _ftl                # noqa: E402

# CE0 is dead and has no records in the store. It stays in the geometry rather
# than being shifted away, so a summary row index is literally
# ce * N_BLOCK + block and every view can paint row 0 as ABSENT -- a hole we
# never looked into, not blank media.
N_CE = _geom.CE_COUNT
N_BLOCK = _geom.BLOCKS_PER_DIE      # 4000 real blocks, not the 4096 address ceiling
N_ROWS = _geom.BLOCKS_TOTAL         # summary row = ce * N_BLOCK + block

# Two block shapes coexist on this die, so a block's page count is never
# assumed: it is chosen per block from the summary's .slc flag.
PAGES_SLC = _geom.PAGES_PER_BLOCK_SLC
PAGES_TLC = _geom.PAGES_PER_BLOCK_TLC

# The two stores have different record sizes and DumpDevice refuses to open a
# store whose page_bytes disagrees, so one device can never open both.
AUS_PER_PAGE = _geom.AUS_PER_PAGE_TLC
AU_AUX = _geom.AU_AUX
AU_DATA = _geom.AU_DATA
RAW_PAGE_BYTES = AUS_PER_PAGE * _geom.AU_STORED       # 18432
DECODED_PAGE_BYTES = AUS_PER_PAGE * (AU_AUX + AU_DATA)  # 16416

# 53 bins exist and 53 is the "in no bin" sentinel, alongside -1 for "no bin id
# recorded at all". Both must map to palette.NONE_RGBA rather than to the low
# end of a colour ramp: an unbinned block is not bin zero.
WERU_BIN_COUNT = _ftl.WERU_BIN_COUNT

# The store-shape constants the path resolver used before titania was reachable
# must agree with the geometry, or _find_store classifies by the wrong size.
assert RAW_PAGE_BYTES == RAW_PAGE_BYTES_EXPECTED
assert DECODED_PAGE_BYTES == DECODED_PAGE_BYTES_EXPECTED
