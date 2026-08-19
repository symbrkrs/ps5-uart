"""Build the per-block summary the viewer renders from.  -> summary.npz

    py -m titania.summary                          # discover the dump, write beside it
    py -m titania.summary --raw raw.store --decoded decoded.store -o summary.npz

Why this exists.  A whole-drive index is tens of millions of records and
`PageStore` answers it by binary search, which is right for "give me this page"
and hopeless for "give me every block's state" -- the analysis passes that did
the latter took minutes each.  The records are fixed 24 bytes and already
sorted, so numpy can stream the whole index and reduce it in seconds.
Everything the drive map colours by ends up in one table the viewer loads
instantly.

Record layout (`titania.store`): _HEAD ">bhhbbb" = ce u8, block u16be, page
u16be, flags u8 (bit0 = slc), 255-rank u8, tier u8; _TAIL "<hbbiii" = source
u16, layout u8, rsv u8, blob u32, offset u32, length u32.

The four states stay four states.  programmed / erased / skipped are counted
separately and absent is `pages_total - the three`, because a viewer that merges
them rebuilds the exact confusion the store format exists to prevent.  `zero` is
carried as its own column for the same reason -- all-0x00 is not erased media.

This is a CACHE.  Rebuild it whenever you rebuild a store.  The map shades from
these arrays and, unlike the block panel, carries no label saying the numbers
are old, so a stale summary does not look like a missing feature -- it looks
like the map disagreeing with the page inspector beside it.
"""
import argparse
import json
import pathlib
import sys
import time

import numpy as np

from . import config
from . import geometry as geom

#: Geometry the summary is laid out over.  From `titania.geometry`, so a build
#: for a differently shaped part cannot disagree with the rest of the package.
CE_COUNT = geom.CE_COUNT
BLOCKS = geom.BLOCKS_PER_DIE        # 4000: the capacity, not the 4096 bound

#: What counts as a decode that produced usable plaintext, in the decoded
#: store's source-name vocabulary.  Correction is not failure: an `ecc-corrected` page
#: converged on 8/8 codewords and passes the MCRC afterwards, two independent
#: detectors agreeing, so it belongs with `decoded` and not with the failures.
#: On the reference capture that class holds 546,525 of 5,453,635 records, so
#: filing it as failure would report a tenth of the readable drive as broken.
DEC_OK = ("decoded", "decoded-ecc-corrected", "decoded-group2",
          "decoded-group2-corrected-payload", "decoded-group2-corrected-parity")

#: Decodes that did not produce trustworthy plaintext.  `ecc-unattempted` is in
#: here deliberately: it is a statement about the tool, not the media, but it is
#: equally not a page anyone may read as data.
DEC_BAD = ("decoded-ecc-not-engaged", "decoded-ecc-diverged",
           "decoded-ecc-mcrc-disagrees", "decoded-ecc-unattempted",
           "decoded-group2-failed", "decoded-group2-mcrc-disagrees")

#: Not counted in either: `decoded-erased` (no data by definition),
#: `decoded-all-zero-retired` (the die returned nothing and ECC is structurally
#: blind -- the all-zero page is the zero codeword) and `decoded-latched-repeat`
#: (a capture artefact: the same bytes latched and re-served, which says the
#: Read failed, not that the media did).  Folding any of those into "failed"
#: would report media as broken that isn't.

NCELL = geom.BLOCKS_TOTAL
#: The map, the viewer and this file all index `ce * 4000 + block`; a build that
#: sized itself off BLOCKS_PER_LUN would shift every CE past 0 by 96 rows.
assert NCELL == CE_COUNT * BLOCKS == 48000, NCELL
HDR_BYTES = 64

REC = np.dtype([("ce", "u1"), ("block", ">u2"), ("page", ">u2"),
                ("flags", "u1"), ("nrank", "u1"), ("tier", "u1"),
                ("src", "<u2"), ("layout", "u1"), ("rsv", "u1"),
                ("blob", "<u4"), ("off", "<u4"), ("len", "<u4")])
assert REC.itemsize == 24, REC.itemsize


def stream_index(store, chunk=4_000_000):
    """Yield chunks of the index as a structured array. Sequential, not seeks."""
    idx = pathlib.Path(store) / "index.tix"
    meta = json.loads((pathlib.Path(store) / "store.json").read_text())
    layouts = {int(k): v for k, v in meta["layouts"].items()}
    sources = {int(k): v[0] for k, v in meta["sources"].items()}
    with open(idx, "rb") as h:
        h.seek(HDR_BYTES)
        while True:
            buf = h.read(chunk * 24)
            if not buf:
                break
            if len(buf) % 24:
                buf = buf[:len(buf) // 24 * 24]      # never half a record
            yield np.frombuffer(buf, dtype=REC), layouts, sources


def cell_of(a):
    """(ce, block) -> a flat 0..47999 index."""
    return a["ce"].astype(np.int64) * BLOCKS + a["block"].astype(np.int64)


def tally(store, want_layouts=None, want_sources=None):
    """-> {name: int64[48000]} counts per (ce, block), by layout or source."""
    out = {}
    t0 = time.time()
    seen = 0
    for arr, layouts, sources in stream_index(store):
        seen += len(arr)
        cell = cell_of(arr)
        if want_layouts:
            for name in want_layouts:
                ids = [i for i, n in layouts.items() if n == name]
                if not ids:
                    continue
                m = np.isin(arr["layout"], ids)
                out.setdefault(name, np.zeros(NCELL, np.int64))
                out[name] += np.bincount(cell[m], minlength=NCELL)
        if want_sources:
            for name in want_sources:
                ids = [i for i, n in sources.items() if n == name]
                if not ids:
                    continue
                m = np.isin(arr["src"], ids)
                out.setdefault(name, np.zeros(NCELL, np.int64))
                out[name] += np.bincount(cell[m], minlength=NCELL)
        out.setdefault("_total", np.zeros(NCELL, np.int64))
        out["_total"] += np.bincount(cell, minlength=NCELL)
        # mode is a per-block property; take it from any record of that block
        out.setdefault("_slc", np.zeros(NCELL, np.int8))
        slc = (arr["flags"] & 1).astype(np.int8)
        out["_slc"][cell] = np.where(slc > 0, 1, out["_slc"][cell])
        print(f"    {seen:,} records  {time.time()-t0:.0f}s", flush=True)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", default=None,
                    help="raw page store (default: the discovered dump)")
    ap.add_argument("--decoded", default=None,
                    help="decoded page store.  Optional: without it the "
                         "dec_clean/dec_fail columns are zero and the viewer "
                         "says so, rather than showing numbers from nowhere")
    ap.add_argument("-o", "--out", default=None,
                    help="output .npz (default: summary.npz beside --raw)")
    a = ap.parse_args(argv)

    raw = pathlib.Path(a.raw) if a.raw else config.default_root()
    dec = pathlib.Path(a.decoded) if a.decoded else None
    out = pathlib.Path(a.out) if a.out else raw.parent / "summary.npz"
    return build(raw, dec, out)


def build(raw_store, decoded_store=None, out=None, log=print,
          on_bootstrap=None):
    """Stream both stores and write the summary.  -> Path

    `on_bootstrap`, if given, is called with the mounted Bootstrap `Ftl` before
    it is used.  That mount is the expensive half of this build -- MEASURED at
    54 s of 79 s, against 2.2 s to stream the whole 43.9M-record index and 0.8 s
    to sample page 0 of every held block -- and it is the same mount anything
    wanting the msoDescriptor, the WERU table or the block map would otherwise
    have to pay for itself.  Handing it out lets one caller take several
    products for the price of one; `nandgui.sidecar` uses it to get the
    descriptor free, which took the pair from 134.7 s to ~79 s.

    The hook is advisory: it must not be able to break a summary build, so an
    exception from it is logged and swallowed.
    """
    raw_store = pathlib.Path(raw_store)
    out = pathlib.Path(out or raw_store.parent / "summary.npz")
    log(f"  streaming {raw_store}")
    a = tally(raw_store, want_layouts=("programmed", "erased", "skipped"))
    prog = a.get("programmed", np.zeros(NCELL, np.int64))
    eras = a.get("erased", np.zeros(NCELL, np.int64))
    skip = a.get("skipped", np.zeros(NCELL, np.int64))
    total = a["_total"]
    slc = a["_slc"]

    dec_clean = dec_fail = np.zeros(NCELL, np.int64)
    if decoded_store and (pathlib.Path(decoded_store) / "index.tix").exists():
        log(f"  streaming {decoded_store}")
        d = tally(decoded_store, want_sources=DEC_OK + DEC_BAD)
        zero = np.zeros(NCELL, np.int64)
        dec_clean = sum((d.get(n, zero) for n in DEC_OK), zero.copy())
        dec_fail = sum((d.get(n, zero) for n in DEC_BAD), zero.copy())

    # -- FTL: two instances, they hold different things --------------------
    from titania.dumpdev import DumpDevice
    from titania.ftl import Ftl, UNKNOWN, WERU_BIN_NAMES
    from titania import geometry as geom

    weru = np.full(NCELL, -1, np.int32)
    owner = np.full(NCELL, -1, np.int32)
    binid = np.full(NCELL, -1, np.int16)
    flags = np.zeros(NCELL, np.uint8)
    erases = np.full(NCELL, -1, np.int32)
    defect = np.zeros(NCELL, np.uint8)          # index into status names
    status_names = ["?"]

    # Split into two logged phases, not one: the mount is ~95% of this build's
    # wall clock, so a caller showing progress has nothing to show without it.
    log("  mounting FTL 1/2: Backup (physical block properties)")
    dev = DumpDevice(raw_store)
    try:
        bak = Ftl.from_device(dev, root_block=1, progress=lambda *x: None)
        phbp = bak.physical_block_properties()
        log("  mounting FTL 2/2: Bootstrap (block map, WERU bins, msoDescriptor)")
        boot = Ftl.from_device(dev, progress=lambda *x: None)
        if on_bootstrap is not None:
            try:
                on_bootstrap(boot)
            except Exception as exc:                          # noqa: BLE001
                log("  on_bootstrap hook raised %s: %s -- the summary itself is "
                    "unaffected" % (type(exc).__name__, exc))
        bm = boot.block_map()
        bins = boot.weru_bins()
        wul = boot.weru_usage_log()
        owners = boot.weru_owners()
        bin_of = {}
        if bins not in (None, UNKNOWN):
            for bid, b in bins.items():
                if b.members not in (None, UNKNOWN):
                    for w in b.members:
                        bin_of[w] = bid
        if bm not in (None, UNKNOWN):
            for w, slots in bm.items():
                for cb in slots:
                    if cb is None:
                        continue
                    b, c = cb
                    if not (0 <= c < CE_COUNT and 0 <= b < BLOCKS):
                        continue
                    i = c * BLOCKS + b
                    weru[i] = w
                    o = owners.get(w) if owners not in (None, UNKNOWN) else None
                    owner[i] = -1 if o in (None, UNKNOWN) else int(
                        o[0] if isinstance(o, tuple) else o)
                    binid[i] = bin_of.get(w, -1)
                    # blockMapping names WERUs the usage log does not carry
                    # (it holds 1070 elements while the map reaches higher), so
                    # a missing record is normal here and must not abort the
                    # build -- flags stay 0 = "nothing recorded".
                    if wul not in (None, UNKNOWN):
                        try:
                            f = wul.flags(w)
                        except (IndexError, KeyError, ValueError):
                            f = None
                        flags[i] = 0 if f in (None, UNKNOWN) else int(f)
        if phbp not in (None, UNKNOWN):
            from titania.ftl import PHYS_BLOCK_STATUS_NAMES
            status_names = list(PHYS_BLOCK_STATUS_NAMES)
            for c in range(CE_COUNT):
                for b in range(BLOCKS):
                    bp = phbp.property_of(geom.pbid(b, c))
                    if bp in (None, UNKNOWN):
                        continue
                    i = c * BLOCKS + b
                    erases[i] = bp.erase_count
                    defect[i] = bp.status
    finally:
        dev.close()

    # -- all-zero: page 0 only, which is what the census showed suffices ----
    log("  sampling page 0 of every held block for all-0x00")
    zero = np.zeros(NCELL, np.uint8)
    with DumpDevice(raw_store) as dev2:
        for c in dev2.present_ces():
            for b in range(BLOCKS):
                i = c * BLOCKS + b
                if prog[i] == 0:
                    continue
                r = dev2.page_ref(c, b, 0, slc=bool(slc[i]))
                if r is None or r.is_erased() or r.is_skipped():
                    continue
                d0 = r.read_bytes()
                if len(set(d0)) == 1 and d0[0] == 0:
                    zero[i] = 1
            print(f"    ce{c} done", flush=True)

    ce_ix = np.repeat(np.arange(CE_COUNT, dtype=np.int16), BLOCKS)
    blk_ix = np.tile(np.arange(BLOCKS, dtype=np.int16), CE_COUNT)
    pages = np.where(slc > 0, 368, 1104).astype(np.int32)
    absent = np.maximum(pages - (prog + eras + skip), 0)

    np.savez_compressed(
        out, ce=ce_ix, block=blk_ix, slc=slc, pages=pages,
        programmed=prog.astype(np.int32), erased=eras.astype(np.int32),
        skipped=skip.astype(np.int32), absent=absent.astype(np.int32),
        indexed=total.astype(np.int32), zero=zero,
        dec_clean=dec_clean.astype(np.int32),
        dec_fail=dec_fail.astype(np.int32),
        weru=weru, owner=owner, binid=binid, flags=flags,
        erases=erases, defect=defect,
        status_names=np.array(status_names, dtype=object),
        bin_names=np.array([WERU_BIN_NAMES.get(i, str(i))
                            for i in range(53)], dtype=object),
    )
    held = prog + eras + skip
    log(f"\n  wrote {out}  ({out.stat().st_size/2**20:.1f} MiB)")
    log(f"    blocks with any record : {(held > 0).sum()}")
    log(f"    programmed pages       : {prog.sum():,}")
    log(f"    erased pages           : {eras.sum():,}")
    log(f"    skipped pages          : {skip.sum():,}")
    log(f"    all-zero blocks        : {int(zero.sum())}")
    log(f"    decoded clean / failed : {dec_clean.sum():,} / {dec_fail.sum():,}")
    return out


if __name__ == "__main__":
    sys.exit(main() and 0)
