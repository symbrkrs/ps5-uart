"""A capture that respects the format, the cell mode, and the ECC.

    py -m titania.smartdump --ce 1,6 --out mydump

A plain sweep reads every page of every block at the die's default references
and stores whatever comes back.  It is simple, it is slow, and on this media it
is wrong in two ways that look identical to worn-out flash:

  1. It reads native-TLC blocks in whatever latch the die is in.  The
     on-target read path emits 0xDA for pseudo-SLC and never emits 0xDF, so a
     native-TLC block read after anything touched SLC comes back ~2.5% 0xFF with
     every MCRC failing.  Worse, a written native-TLC page read in the wrong
     latch can come back entirely 0xFF -- which a capture records as erased
     media, and no later pass will ever revisit.
  2. It accepts a page that does not decode.  Those pages are usually not
     lost; the die's read references have drifted and the firmware's own retry
     ladder moves them back.  Measured on one drive: 316,949 pages that no
     default-reference read could decode came back clean at a retry level, and
     14,251 addresses the base capture had recorded as ERASED turned out to hold
     real data.

This module does four things a plain sweep does not:

  * Chooses the cell mode per block from the cell map and latches it
    explicitly (`readretry.Ladder.apply`) before reading, so the mode is a
    decision and not an inheritance.
  * Reads only blocks that may hold data, by the union of two rules that
    fail differently -- a page-0 probe, which asks the media, and the FTL's
    allocated-WERU list, which asks the firmware.  See `select_blocks`.
  * Judges every page with `titania.verdict`, which is region-aware: block 0
    on the group-2 dies has its own ECC geometry entirely, and testing it as an
    ordinary page reports ~1840 false failures that would otherwise each be run
    through the whole 33-level ladder for nothing.
  * Escalates only the pages that failed, level by level, until they decode
    or the ladder runs out, recording the level in the source name because the
    level is the provenance.

Read-only.  See `readretry` -- there is no program or erase opcode anywhere in
the call graph, and the die's references are restored to level 0 on every exit
path, verified by 0xEE readback.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

from . import geometry as geom
from . import nanddump
from . import readretry
from . import verdict as vd
from .device import DeviceError
from .store import StoreWriter

__all__ = ["select_blocks", "smart_dump_ce", "PageOutcome", "RETRY_SOURCE"]

PAGE_BYTES = nanddump.PAGE_BYTES

#: Source-name prefix for a page a retry level rescued.  `dumpdev` gives this
#: prefix its own trust rule (rank 60), above a default-reference capture at 50
#: because for these pages it is the only read that decoded at all, and below
#: the 94 of a cross-checked ladder sweep because this stops at the first level
#: that works and so measures recovery, not reproducibility.
RETRY_SOURCE = "retry-level-%d"

#: Verdicts that mean the page is finished and no retry level would help.
#:   OK          the region's own check passed
#:   ERASED      all 0xFF -- but see the note in `_retry_block`: at level 0 in
#:               the correct latch this is believable; it was not before.
#:   DEAD_CE     a die that does not answer.  Not media at all.
DONE = (vd.OK, vd.ERASED, vd.DEAD_CE)

#: Verdicts worth spending ladder levels on.  `AUX_REJECTED` is not
#: here: its integrity checks passed and only the address rule failed, so the
#: bytes were read correctly and moving the references cannot change the answer.
RETRYABLE = (vd.FAIL, vd.WRONG_MODE, vd.UNREADABLE)

PageOutcome = collections.namedtuple("PageOutcome", "page verdict level")


# ---------------------------------------------------------------- selection --
def select_blocks(dev, ce, modes, lun=0, ftl=None, weru_table=None,
                  log=print, every=500):
    """Which blocks to read in full.  -> (blocks, page0, stats)

    The union of two rules that FAIL differently, which is the whole design:

      * A PAGE-0 probe asks the media.  Measured on a CE that was later captured
        in full: all 145 blocks holding data have a programmed page 0, and the
        probe would have missed none.  It costs 4000 pages instead of 3,989,120
        -- 27.7x less reading.  It cannot be defeated by stale firmware metadata.
      * The ftl's allocated WERUs ask the firmware.  This catches a block whose
        data does not start at page 0, which the probe would miss.

    Neither is sufficient.  Metadata alone has counterexamples on this media:
    three blocks were found fully PROGRAMMED inside WERUs whose owner is 0, i.e.
    already released -- stale content in freed-but-not-yet-erased WERUs, which
    `is_allocated` drops silently.  The probe alone misses late-starting blocks.
    Taking the union costs almost nothing because the extra blocks are few.

    The page-0 bytes are returned, not thrown away: they were really read, and a
    caller stores them as evidence rather than re-reading them.
    """
    st = {"probed": 0, "answered": 0, "forced": 0, "unreadable": 0}
    forced = set()
    if ftl is not None:
        forced |= set(_ftl_blocks(ftl, ce, log=log))
    if weru_table:
        forced |= set(nanddump._forced_blocks(weru_table, ce, log=log))
    if not forced:
        log("    no FTL and no --weru-table: selection runs on the page-0 probe "
            "ALONE, so a block written from page 1 on will be missed")
    st["forced"] = len(forced)

    page0, hits = {}, []
    t0 = time.time()
    order = sorted(modes, key=int)
    for blk in order:
        slc = modes[blk] == "slc"
        try:
            data = dev.read_page(ce, int(blk), 0, lun=lun, slc=slc)
        except DeviceError as exc:
            log("    ce%d blk%s: probe read failed: %s" % (ce, blk, exc))
            st["unreadable"] += 1
            continue
        page0[blk] = data
        st["probed"] += 1
        if data.count(0xFF) != len(data) or int(blk) in forced:
            hits.append(blk)
        if int(blk) % every == 0 and st["probed"]:
            rate = st["probed"] / max(time.time() - t0, 1e-6)
            log("    ce%d probe blk%s: %d read, %d selected, %.1f blk/s"
                % (ce, blk, st["probed"], len(hits), rate))
    st["answered"] = len(hits)
    log("    ce%d selection: %d of %d blocks (%d forced by metadata) in %.0fs"
        % (ce, len(hits), st["probed"], len(forced), time.time() - t0))
    return hits, page0, st


def _ftl_blocks(ftl, ce, log=print):
    """Blocks on `ce` claimed by an allocated WERU with live AUs.  -> [block]"""
    from .ftl import UNKNOWN as FTL_UNKNOWN
    out = set()
    try:
        bm = ftl.block_map()
        owners = ftl.weru_owners()
    except Exception as exc:                                 # noqa: BLE001
        log("    FTL block map unavailable (%s); metadata half skipped" % exc)
        return []
    if bm in (None, FTL_UNKNOWN):
        return []
    for weru, slots in bm.items():
        owner = owners.get(weru) if owners not in (None, FTL_UNKNOWN) else None
        if owner in (None, FTL_UNKNOWN, 0):
            continue
        for cb in slots or ():
            if cb is None:
                continue
            blk, c = cb
            if int(c) == int(ce):
                out.add(int(blk))
    log("    FTL: %d blocks on ce%d are claimed by an allocated WERU" % (len(out), ce))
    return sorted(out)


# -------------------------------------------------------------- the capture --
def smart_dump_ce(dev, ce, root, modes, lun=0, ftl=None, weru_table=None,
                  top_level=None, log=print, source_name="smart-dump",
                  blocks=None, correct=True, checkpoint_blocks=40):
    """Capture one CE.  -> stats dict

    `modes`       {block: 'slc'|'tlc'} -- the cell map.  Not optional: reading a
                  block in the wrong mode is the failure this exists to prevent,
                  and guessing is not better than being told.
    `top_level`   highest retry level to try, or None for the whole ladder.
                  0 disables retry entirely and makes this a mode-correct sweep.
    `correct`     let the ECC try before calling a page failed.  Leaving it True
                  is what stops the ladder being spent on pages the corrector
                  would have fixed anyway.
    """
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "store.json").exists():
        w = StoreWriter.reopen(root)
    else:
        w = StoreWriter(root, page_bytes=PAGE_BYTES)
    base_src = w.source(source_name, nanddump.UNKNOWN,
                        nanddump._REASON)
    retry_srcs = {}
    st = collections.Counter()
    t0 = time.time()

    def retry_src(level):
        if level not in retry_srcs:
            from .dumpdev import RETRY_PROVENANCE
            retry_srcs[level] = w.source(
                RETRY_SOURCE % level, RETRY_PROVENANCE.tier,
                RETRY_PROVENANCE.reason)
        return retry_srcs[level]

    if blocks is None:
        blocks, page0, sel = select_blocks(dev, ce, modes, lun=lun, ftl=ftl,
                                           weru_table=weru_table, log=log)
        st.update(sel)
    else:
        page0 = {}

    order = readretry.level_order(top_level)
    log("  ce%d: %d blocks, retry levels %s"
        % (ce, len(blocks), order if len(order) > 1 else "(retry disabled)"))

    with readretry.Ladder(dev, log=log) as lad:
        for i, blk in enumerate(blocks):
            slc = modes[blk] == "slc"
            try:
                res = _dump_block(lad, w, ce, int(blk), slc, lun, base_src,
                                  retry_src, order, log, correct)
            except DeviceError as exc:
                log("    ce%d blk%s: %s; block NOT indexed" % (ce, blk, exc))
                st["bad_blocks"] += 1
                continue
            for k, v in res.items():
                st[k] += v
            if checkpoint_blocks and (i + 1) % checkpoint_blocks == 0:
                w.checkpoint()
                log("    ce%d: %d/%d blocks, %d rescued, %d unrescued"
                    % (ce, i + 1, len(blocks), st["rescued"], st["unrescued"]))

        # Blocks the selection dismissed: page 0 was read, so it is stored as
        # the evidence it is, and pages 1.. are recorded as a deliberate skip.
        # An interrupted capture and a partial one must not look
        # alike, which is the whole reason `add_skipped` exists.
        st.update(_record_dismissed(w, ce, modes, page0, set(blocks), base_src))

    st["seconds"] = int(time.time() - t0)
    w.finish(stats=nanddump._stats(ce, lun, dict(st), capture="smart-dump",
                                   retry_levels=order))
    return dict(st)


def _dump_block(lad, w, ce, blk, slc, lun, base_src, retry_src, order, log,
                correct):
    """One block: read at each level in turn, keeping only what each rescues."""
    st = collections.Counter()
    ppb = geom.pages_per_block(slc)
    pending = list(range(ppb))
    best = {}                      # page -> (data, verdict, level)

    for level in order:
        if not pending:
            break
        lad.apply(ce, level, slc=slc)
        data = lad.read_span(ce, blk, 0, ppb, slc=slc, lun=lun)
        if data is None:
            st["unread_spans"] += 1
            break
        still = []
        for pg in pending:
            raw = bytes(data[pg * PAGE_BYTES:(pg + 1) * PAGE_BYTES])
            v = vd.classify(raw, ce, blk, pg, slc=slc, correct=correct)
            if v.verdict in DONE:
                best[pg] = (raw, v, level)
            elif v.verdict in RETRYABLE:
                # Keep the first reading as the fallback, so a page nothing
                # rescues still lands in the store with its bytes rather than
                # vanishing.  A later level does not overwrite it unless it
                # actually decoded -- an equally-failed read is not an
                # improvement and swapping to it would lose the provenance.
                best.setdefault(pg, (raw, v, level))
                still.append(pg)
            else:
                best[pg] = (raw, v, level)      # AUX_REJECTED and friends
        if len(still) != len(pending):
            log("      ce%d b%d level %-2d: %d of %d pages resolved"
                % (ce, blk, level, len(pending) - len(still), len(pending)))
        pending = still

    unrescued = set(pending)
    first_level = order[0] if order else 0
    for pg, (raw, v, level) in sorted(best.items()):
        if pg in unrescued:
            # Nothing decoded it.  Stored under the BASE source and at the base
            # rank: this run has no more claim on the page than an ordinary
            # capture does, and dressing a failed read in a retry-level source
            # would assert a rescue that did not happen.
            st["unrescued"] += 1
            src = base_src
        elif level != first_level:
            # It took a level the first pass did not use, so the level is the
            # provenance and goes in the source name.
            st["rescued"] += 1
            st["rescued_level_%d" % level] += 1
            src = retry_src(level)
        else:
            src = base_src
        st["stored_bytes"] += nanddump._put_page(w, src, ce, blk, pg, slc, raw)
        st[str(v.verdict)] += 1
        st["pages"] += 1
    return st


def _record_dismissed(w, ce, modes, page0, kept, src):
    """Page 0 as evidence, pages 1.. as an explained skip.  -> counter"""
    from .dumpdev import SKIP_PREFIX, SKIP_PROVENANCE
    st = collections.Counter()
    why = ("page 0 of this block read as erased in the block's own cell mode "
           "and no allocated WERU with live AUs claims it")
    skip_src = w.source(SKIP_PREFIX + "page0-blank",
                        SKIP_PROVENANCE.tier, why)
    for blk, data in page0.items():
        if blk in kept:
            continue
        slc = modes[blk] == "slc"
        nanddump._put_page(w, src, ce, int(blk), 0, slc, data)
        for pg in range(1, geom.pages_per_block(slc)):
            w.add_skipped(ce, int(blk), pg, slc, skip_src)
            st["skipped"] += 1
        st["dismissed_blocks"] += 1
    return st


# --------------------------------------------------------------------- cli --
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ce", required=True, help="comma-separated CE numbers")
    ap.add_argument("--out", required=True, help="store directory to write")
    ap.add_argument("--cellmap", required=True,
                    help="JSON {ce: {block: 'slc'|'tlc'}} -- the cell map")
    ap.add_argument("--weru-table", default=None,
                    help="weru_table.json for the metadata half of selection")
    ap.add_argument("--max-level", type=int, default=None,
                    help="highest retry level to try (0 disables retry)")
    ap.add_argument("--no-correct", action="store_true",
                    help="judge a page on its uncorrected MCRC only.  Faster, "
                         "and spends ladder levels on pages the ECC would have "
                         "fixed -- measure before using it")
    ap.add_argument("--lun", type=int, default=0)
    a = ap.parse_args(argv)

    from .gbedev import GbeDevice
    cellmap = json.loads(pathlib.Path(a.cellmap).read_text("utf-8"))
    ces = [int(x) for x in a.ce.split(",")]
    dev = GbeDevice(ces=ces, log=print)
    rc = 0
    try:
        for ce in ces:
            modes = cellmap.get(str(ce)) or cellmap.get(ce)
            if not modes:
                print("ce%d: no cell map entry -- SKIPPED, because reading a "
                      "block in the wrong mode is the failure this tool exists "
                      "to prevent" % ce)
                rc = 1
                continue
            st = smart_dump_ce(dev, ce, a.out, modes, lun=a.lun,
                               weru_table=a.weru_table, top_level=a.max_level,
                               correct=not a.no_correct)
            print("ce%d: %s" % (ce, json.dumps(st, sort_keys=True)))
    finally:
        # `Ladder.__exit__` has already restored level 0 on every die it
        # touched, verified.  Nothing else here owns board state.
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
