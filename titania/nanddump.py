#!/usr/bin/env python3
"""Whole-CE raw dumps, built on `GbeDevice`.

    py -m titania.nanddump --ce 1,6 --out E:\\titania_nand

WHAT MAKES A RAW IMAGE CORRECT
The file is addressed by offset, so page N of block B must land at exactly
`(lun*blocks + B)*pages_per_block + N` pages into it. Two things follow, and both
have been got wrong here before:

* Compose the row, never walk it linearly. The wire row is
  `(lun << 23) | (block << 11) | page`, so a linear sweep of rows 0..65535 is
  blocks 0..31 of one LUN -- 0.7% of a CE -- and running off the programmed
  blocks looks exactly like the die ending. Only 1104 of a block's 2048 page
  slots exist for TLC (368 for pseudo-SLC), so a linear sweep also spends nearly
  half its time on slots that are not there.
* A block that cannot be read must still occupy its place. Shortening the
  file shifts every later block and silently corrupts the whole image, so an
  unreadable block is padded with 0xFF -- what erased media reads as -- and the
  gap is recorded in the log and returned in the summary.

CELL MODE
`slc=` selects what you get, not whether the read succeeds: a written native-TLC
page reads entirely 0xFF under the 0xDA prefix, and a pseudo-SLC page reads
entirely 0xFF without it. So a single-mode dump records the other mode's blocks
as erased. Either take the mode per block from `Ftl.cell_mode_of_block` (a
per-WERU configuration property, not a block-number rule) and read each block
once, or dump both modes and treat a block as erased only where they agree.

LUN IS ALWAYS 0 -- DO NOT SWEEP TWO LUNS
`LUNS_PER_CE = 2` is a misnomer and `geometry.py` says so at length: MioInit step
5 (`sub_40491F96`) clamps `lunsPerDevice` to 1, after which the LUN field of the
row resolves to 0, and "every row this project has ever composed is LUN 0". The
real factor of two is planes, carried in the BLOCK field (plane = block LSB),
never the LUN field. The ID says one die per CE and capacity agrees exactly:
12 dies x 3800 usable blocks x 1104 pages x 16 KiB = 825 GB, the retail figure.

So a CE is covered by `--lun 0` alone, and the mapped dump's 68.5 GiB per CE
matches 3800 x 1104 x 16 KiB = 68.8 GB. Sweeping lun 0 and 1 doubles the runtime
and adds nothing -- this was done here before the geometry was read properly.

RESUMING
Progress is a block boundary. `--start-lun`/`--start-block` continue an existing
file, and only the LUN being resumed into starts part-way -- applying the offset
to every LUN would silently skip the later LUNs' early blocks.
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

from . import geometry as geom
from .device import DeviceError
from .gbedev import GbeDevice, PAGE_BYTES
from .store import StoreWriter, BLOB_DIR, MAX_BLOB
from .dumpdev import UNKNOWN


def _ces_in(root, ce):
    """The CE list a store should advertise after adding `ce`.  -> sorted list

    `DumpDevice.present_ces()` reads `stats[mode]["ces"]` and nothing else, so a
    store that overwrites that list on each write advertises only the last CE --
    and every tool that enumerates CEs then sees a one-die device holding six.
    Folding several CEs into one store has to accumulate.
    """
    prior = []
    meta = pathlib.Path(root) / "store.json"
    if meta.exists():
        try:
            s = json.loads(meta.read_text())["stats"]
            prior = (s.get("strict") or s.get("suspect") or {}).get("ces", [])
        except Exception:                                   # noqa: BLE001
            prior = []
    return sorted({int(c) for c in prior} | {int(ce)})


def _stats(ce, lun, st, **extra):
    """Store stats, nested by mode -- which is not optional.

    `PageStore.stat(name)` looks in `stats[mode]`, where mode is "suspect" or
    "strict" depending on whether suspect sources are excluded. A flat dict
    therefore reads back as nothing at all, and the visible symptom is
    `DumpDevice.present_ces()` returning [] for a store that plainly has pages
    in it -- every tool that enumerates CEs then sees an empty device.

    Both modes get the same numbers here because this capture has exactly one
    source; a store with several would legitimately differ between them.
    """
    body = {"ces": [ce], "ce": ce, "lun": lun}
    body.update({k: v for k, v in st.items()})
    body.update(extra)
    return {"suspect": dict(body), "strict": dict(body)}


_REASON = ("level-0 read over the GbE transport, this session; no capture-time "
           "verification and no cross-check against another capture -- "
           "unmeasured by construction")
# The source verdict a resumed run re-registers. It must match the triple the
# first run used exactly: `StoreWriter.source` keys on (name, tier), so a
# drift in any of the three would give the continuation its own source id and
# split one capture into two provenances that a reader then has to adjudicate
# between.
_SOURCE = ("gbe-dump", UNKNOWN, _REASON)


def _store_writer(root, name="gbe-dump"):
    """A StoreWriter plus the source record every page needs.

    The tier is UNKNOWN, and there is nothing else to say: this is a plain
    level-0 read with no per-page verification, and raw TLC is not even
    bit-stable here (~0.2% of bits differ between two reads of a lower page,
    ~2.1% of an upper one).  Nothing about that has to be encoded as a score,
    because nothing about it decides anything -- if this capture and another
    both claim a page, `dumpdev.adjudicate()` decodes the two and keeps the one
    that reads.  The source name and reason exist so a reader can see where the
    bytes came from, not so they can be ranked.
    """
    w = StoreWriter(root, page_bytes=PAGE_BYTES)
    src = w.source(name, UNKNOWN, _REASON)
    return w, src


def _blob_shard(w, prefix=BLOB_DIR + "/pages", headroom=64 << 20):
    """The blob to append to, rolled over before the 4 GiB record limit.

    A record's offset field is 32 bits, so one blob cannot exceed MAX_BLOB and
    `append` refuses past it -- correctly, since a wider record would break
    every index already written. One CE is ~1.2 GiB of stored pages, so a store
    spanning all twelve passes that limit at the fourth, which is exactly where
    the combined store first hit it.

    Sharding is the fix the store itself prescribes ("split the blob rather than
    widening the record"): blobs are addressed by id, a store may hold any
    number, and a reader neither knows nor cares which file a page came from.

    `headroom` leaves room for the page being written plus slack, so the roll
    happens before a page would straddle the boundary rather than after a failed
    append. The current shard is cached on the writer -- `blob_size` is cheap
    but not free, and this runs once per page.
    """
    n = getattr(w, "_nd_shard", 0)
    while w.blob_size(f"{prefix}_{n:03d}.bin") >= MAX_BLOB - headroom:
        n += 1
    w._nd_shard = n
    return f"{prefix}_{n:03d}.bin"


def _put_page(w, src, ce, block, page, slc, data, blob=None):
    """One page into a store, storing no bytes when it is erased.

    ~99% of blocks on this media read all-0xFF, so keeping the bytes costs ~68
    GiB per CE to say "nothing here" 3.9 million times. An erased page becomes a
    single fixed-size index record instead.

    The blob path must start with BLOB_DIR. That prefix is what makes a blob
    store-owned, and therefore writable and compactable: a blob outside it is
    treated as a foreign capture, `PageStore` refuses to write to it, and
    `compact`/`orphan_report` count it as foreign. Reads work either way, which
    is exactly why writing to "blob/pages.bin" instead of "blobs/pages.bin" went
    unnoticed here until the store was inspected.

    A dead-ce page is not erased -- it is the 0xA7/0x00 bus constant off a die
    that does not answer, and it is real evidence -- so only an all-0xFF page
    takes this path.
    """
    if data.count(0xFF) == len(data):
        w.add_erased(ce, block, page, slc, src)
        return 0
    blob_id, off = w.append(blob or _blob_shard(w), data)
    w.add(ce, block, page, slc, src, "programmed", blob_id, off, len(data))
    return len(data)


def blocks_held(root, ce, modes, lun=0, count_skipped=True):
    """Which cell-map blocks a store already holds every page of.  -> set

    Resume position read from the store rather than passed in by hand. The
    hand-passed version (`--first-index`) is a footgun that has already gone
    off here: it is an index into the cell map, it applies to every CE in the
    run, and pointing it at a CE that had nothing dumped yet would have silently
    skipped that CE's first thousand blocks.

    A block counts as held only when the store has all of its pages, in the mode
    the cell map gives it. A partly-written block -- the checkpoint landed
    mid-block, or the run died -- is re-read, which is cheap and unambiguous.

    `count_skipped` decides whether a SKIPPED page counts as held.
    True is the resume answer: the run already decided not to read it and a
    resume should not quietly overturn that. False is how you go back and read
    the skipped ranges after all -- when the evidence behind the skip is in
    doubt, which is the whole reason skips are recorded rather than left as
    silent absence.
    """
    from .dumpdev import DumpDevice

    root = pathlib.Path(root)
    if not (root / "store.json").exists():
        return set()
    held = set()
    with DumpDevice(root) as d:
        store = getattr(d, "_store", None)
        for blk in modes:
            slc = modes[blk] == "slc"
            want = geom.pages_per_block(slc)
            if store is not None and count_skipped:
                have = store.count_in_block(ce, int(blk), slc)
            else:
                # page_ref, not count_in_block: the count cannot tell a skipped
                # record from a read one, and here that is exactly the question.
                have = 0
                for p in range(want):
                    r = d.page_ref(ce, int(blk), p, slc=slc)
                    if r is None:
                        continue
                    if not count_skipped and getattr(
                            r, "is_skipped", lambda: False)():
                        continue
                    have += 1
            if have >= want:
                held.add(str(blk))
    return held


def dump_ce_store(dev, ce, root, modes, lun=0, log=print, every=25,
                  first_index=0, chunk_blocks=250, resume=False,
                  reread_skipped=False):
    """Dump one CE straight into one `titania.store`, sparsely.

    Same walk as `dump_ce_mapped` -- every block once, in its own cell mode --
    but the output is the module's canonical blob+index store rather than a flat
    image, so `DumpDevice` and everything built on it reads it with no adapter,
    and erased pages cost an index record instead of 18432 bytes.
    """
    st = {"pages": 0, "blank": 0, "stored": 0, "bad_blocks": 0,
          "checkpoints": 0, "skipped": 0}
    order = sorted(modes, key=int)[first_index:]
    root = pathlib.Path(root)

    if resume:
        # Read the resume point out of the store, then dump only what is
        # missing -- which also makes a re-run over a finished store a no-op
        # rather than a second copy of every page.
        held = blocks_held(root, ce, modes, lun=lun,
                           count_skipped=not reread_skipped)
        if held:
            order = [b for b in order if b not in held]
            st["skipped"] = len(held)
            log(f"    ce{ce}: {len(held)} blocks already in the store; "
                f"{len(order)} to go")

    total = sum(geom.pages_per_block(modes[b] == "slc") for b in order)
    t0 = time.time()

    # One store, checkpointed -- not a part per chunk.
    #
    # The index is only written by finish(), so a run killed at hour 19 with no
    # checkpoint leaves blobs and temp runs with no index: nineteen hours for
    # zero readable pages. That is why this checkpoints at all.
    #
    # It used to checkpoint by starting a new store every chunk, because only
    # the blobs append -- finish() consumes its buffered records, so a second
    # finish() over one root would write an index describing only what came
    # after the first and silently drop everything before it. `StoreWriter`
    # now reloads the existing index when it reopens, so a checkpoint rewrites
    # a complete index and the capture stays a single store. The fragmentation
    # and the merge pass it forced are both gone.
    #
    # chunk_blocks now trades checkpoint cost against re-read cost: a checkpoint
    # rewrites the whole index (24 B/page, ~96 MB for a full CE), and a crash
    # costs the time to re-read the blocks since the last one. 250 blocks is
    # ~20 min of re-reading against a few seconds of index rewrite.
    if (root / "store.json").exists():
        w = StoreWriter.reopen(root)
        src = w.source(*_SOURCE)
        log(f"    ce{ce}: extending existing store ({w._n} records)")
    else:
        w, src = _store_writer(root)

    def checkpoint():
        w.checkpoint(stats=_stats(ce, lun, st))
        st["checkpoints"] += 1

    since = 0
    for blk in order:
        if since >= chunk_blocks:
            checkpoint()
            since = 0
            log(f"    ce{ce}: checkpoint at blk{blk} ({w._n} records)")
        slc = modes[blk] == "slc"
        ppb = geom.pages_per_block(slc)
        tb = time.time()
        try:
            pages = dev.read_pages(ce, int(blk), 0, ppb, lun=lun, slc=slc)
        except DeviceError as e:
            # An unreadable block is not recorded as erased: absent and erased
            # mean different things, and a reader must be able to tell "we never
            # got this" from "the media says 0xFF here".
            log(f"    ce{ce} blk{blk}: {e}; block NOT indexed")
            st["bad_blocks"] += 1
            continue
        # A block is ~9 s at the healthy rate. Anything far above that is the
        # transport fighting, and it has to be visible per block: the progress
        # line reports a running average, which smears one 54-minute stall into
        # a gentle decline across every later line and looks like the flash
        # slowly getting slower rather than one discrete event.
        dt = time.time() - tb
        if dt > 30:
            st["slow_blocks"] = st.get("slow_blocks", 0) + 1
            st["slow_seconds"] = st.get("slow_seconds", 0.0) + dt
            log(f"    ce{ce} blk{blk}: SLOW -- {dt:.1f}s for {ppb} pages "
                f"({ppb*PAGE_BYTES/dt/1024:.0f} KB/s)")
        for pgno, data in enumerate(pages):
            n = _put_page(w, src, ce, int(blk), pgno, slc, data)
            st["stored"] += n
            st["blank"] += (n == 0)
        st["pages"] += ppb
        since += 1
        if int(blk) % every == 0:
            el = max(time.time() - t0, 1e-6)
            rate = st["pages"] * PAGE_BYTES / el
            log(f"    ce{ce} blk{blk} ({modes[blk]}): "
                f"{st['pages']*PAGE_BYTES/2**30:7.2f} GiB read "
                f"{st['stored']/2**30:6.2f} GiB stored "
                f"{rate/1024:7.1f} KB/s "
                f"blank {100.0*st['blank']/max(st['pages'],1):5.1f}% "
                f"eta {(total-st['pages'])*PAGE_BYTES/max(rate,1)/3600:5.1f} h")
    # "ces" is not decoration: DumpDevice.present_ces() reads it straight
    # out of the store stats, so a store without it reports no CEs and every
    # tool that enumerates them silently sees an empty device.
    st["seconds"] = time.time() - t0
    w.finish(stats=_stats(ce, lun, st))
    return st


def dump_ce_survey(dev, ce, root, modes, lun=0, log=print, every=500,
                   also_blocks=(), reason=None):
    """Read page 0 of every block; read in FULL only the blocks that answer.

    MEASURED on ce1, whose 4000 blocks were captured in full first: all 145
    blocks holding data have a programmed page 0, and a page-0 survey would have
    missed none. 4000 survey pages plus 140208 in the blocks that answered is
    144208 against 3989120 -- 27.7x less reading.

    Why this RULE and not the firmware's TABLES. Metadata-driven skipping was
    tried against the same capture and has counterexamples: ce1 blocks 1992,
    1993 and 1995 are fully programmed inside WERUs whose owner is 0, i.e. free
    -- stale content in released-but-not-yet-erased WERUs. Skipping on
    `is_allocated` drops it silently. A page-0 survey asks the media instead of
    the firmware's opinion of the media, so a stale or cleared descriptor cannot
    defeat it. Pass such blocks in `also_blocks` to take the union of the two
    rules, which is what the caller should normally do: the extra blocks are
    nearly free and the two rules fail in different ways.

    What is recorded, and why it is not just "the DATA". Page 0 of a skipped
    block was read, so it is stored as the erased page it is -- real evidence.
    Only pages 1.. are `add_skipped`, carrying this rule as their reason, so the
    store says "we chose not to read these, on this basis" instead of falling
    silent. That distinction is the whole point: an interrupted dump and a
    partial one must not look alike.

    This does not produce a FULL capture and the store will say so. Validate the
    rule on a CE swept in full before trusting it on a CE that never will be.
    """
    from .dumpdev import SKIP_PREFIX, SKIP_PROVENANCE

    st = {"pages": 0, "blank": 0, "stored": 0, "bad_blocks": 0, "skipped": 0,
          "surveyed": 0, "hit": 0}
    root = pathlib.Path(root)
    if (root / "store.json").exists():
        w = StoreWriter.reopen(root)
        src = w.source(*_SOURCE)
    else:
        w, src = _store_writer(root)
    why = reason or ("page 0 of this block read as erased and no allocated "
                     "WERU with live AUs claims it; measured on ce1, where all "
                     "145 blocks holding data have a programmed page 0")
    skip_src = w.source(SKIP_PREFIX + "page0-blank",
                        SKIP_PROVENANCE.tier, why)

    order = sorted(modes, key=int)
    forced = {str(b) for b in also_blocks}
    t0 = time.time()

    # -- pass 1: page 0 of every block ------------------------------------
    page0, hits = {}, []
    for blk in order:
        slc = modes[blk] == "slc"
        try:
            d = dev.read_page(ce, int(blk), 0, lun=lun, slc=slc)
        except DeviceError as e:
            log(f"    ce{ce} blk{blk}: survey read failed: {e}; NOT indexed")
            st["bad_blocks"] += 1
            continue
        page0[blk] = d
        st["surveyed"] += 1
        if d.count(0xFF) != len(d) or blk in forced:
            hits.append(blk)
        if int(blk) % every == 0:
            el = max(time.time() - t0, 1e-6)
            log(f"    ce{ce} survey blk{blk}: {st['surveyed']} read, "
                f"{len(hits)} hits, {st['surveyed']/el:.1f} blk/s")
    log(f"    ce{ce} survey done: {len(hits)} of {st['surveyed']} blocks answer "
        f"({len(forced)} forced by metadata) in {time.time()-t0:.0f}s")

    # -- pass 2: the blocks that answered, in full ------------------------
    hitset = set(hits)
    for blk in order:
        if blk not in page0:
            continue
        slc = modes[blk] == "slc"
        ppb = geom.pages_per_block(slc)
        if blk not in hitset:
            # page 0 is real evidence; the rest is an honest, explained gap
            st["stored"] += _put_page(w, src, ce, int(blk), 0, slc, page0[blk])
            st["blank"] += 1
            for pg in range(1, ppb):
                w.add_skipped(ce, int(blk), pg, slc, skip_src)
            st["skipped"] += ppb - 1
            st["pages"] += 1
            continue
        try:
            pages = dev.read_pages(ce, int(blk), 0, ppb, lun=lun, slc=slc)
        except DeviceError as e:
            log(f"    ce{ce} blk{blk}: {e}; block NOT indexed")
            st["bad_blocks"] += 1
            continue
        for pgno, data in enumerate(pages):
            n = _put_page(w, src, ce, int(blk), pgno, slc, data)
            st["stored"] += n
            st["blank"] += (n == 0)
        st["pages"] += ppb
        st["hit"] += 1
        if st["hit"] % 10 == 0:
            log(f"    ce{ce}: {st['hit']}/{len(hits)} hit blocks read, "
                f"{st['stored']/2**20:.1f} MiB stored")
    st["seconds"] = time.time() - t0
    w.finish(stats=_stats(ce, lun, st, capture="page0-survey",
                          survey_rule=why, hit_blocks=[int(b) for b in hits]))
    return st


def convert_mapped(bin_path, cellmap_modes, ce, root, lun=0, log=print):
    """Migrate a flat mapped image into a sparse store, without the device.

    The flat file has no index of its own -- block order and length come from
    the cell map, blocks being 368 or 1104 pages -- so a truncated file is read
    only as far as it holds whole blocks. A partial block at the tail is dropped
    rather than guessed at.
    """
    w, src = _store_writer(root, name="gbe-dump-migrated")
    st = {"pages": 0, "blank": 0, "stored": 0, "blocks": 0}
    size = pathlib.Path(bin_path).stat().st_size
    off = 0
    with open(bin_path, "rb") as f:
        for blk in sorted(cellmap_modes, key=int):
            slc = cellmap_modes[blk] == "slc"
            ppb = geom.pages_per_block(slc)
            if off + ppb * PAGE_BYTES > size:
                log(f"  stopping at block {blk}: file holds no complete block "
                    f"beyond offset {off}")
                break
            for pgno in range(ppb):
                data = f.read(PAGE_BYTES)
                n = _put_page(w, src, ce, int(blk), pgno, slc, data)
                st["stored"] += n
                st["blank"] += (n == 0)
            off += ppb * PAGE_BYTES
            st["pages"] += ppb
            st["blocks"] += 1
    w.finish(stats=_stats(ce, lun, st, migrated_from=str(bin_path)))
    return st


def absorb_store(dest_root, source_root, ce, modes, lun=0, log=print,
                 every=400, overwrite=False):
    """Fold one store's pages into another, in place.  -> dict of counters

    The consolidation step. `merge_stores` builds a third store from N sources,
    which means copying pages that were already in the right place; this extends
    the destination with only what it is missing, so folding a 250-block part
    into a 1100-block store costs the 250 blocks.

    Pages the destination already holds are kept, not replaced -- `overwrite`
    inverts that -- because the destination is the canonical store and the thing
    being folded in is, by construction, the later and less-established capture.

    Erasedness is carried across as erasedness: a zero-length record is added as
    one rather than expanded to 18432 bytes of 0xFF and re-detected, which is
    both faster and the only way the sparse encoding survives a consolidation.
    """
    from .dumpdev import DumpDevice

    dest_root = pathlib.Path(dest_root)
    st = {"pages": 0, "blank": 0, "stored": 0, "kept": 0, "absent": 0}
    if (dest_root / "store.json").exists():
        w = StoreWriter.reopen(dest_root)
        src = w.source(*_SOURCE)
    else:
        w, src = _store_writer(dest_root)

    have = set() if overwrite else _keys_held(dest_root, ce, modes)
    t0 = time.time()
    with DumpDevice(source_root) as d:
        for blk in sorted(modes, key=int):
            slc = modes[blk] == "slc"
            for pg in range(geom.pages_per_block(slc)):
                if (int(blk), pg, slc) in have:
                    st["kept"] += 1
                    continue
                ref = d.page_ref(ce, int(blk), pg, slc=slc)
                if ref is None:
                    st["absent"] += 1
                    continue
                if getattr(ref, "is_skipped", lambda: False)():
                    # Carry the original source across, not this run's. A skip
                    # is only meaningful with the evidence behind it, and that
                    # evidence lives in the source's reason -- re-stamping it
                    # with the merging run's source would turn "the FTL said
                    # this WERU was unallocated" into an unexplained hole.
                    o = ref.src
                    w.add_skipped(ce, int(blk), pg, slc,
                                  w.source(o.name, o.tier, o.reason))
                    st["skipped"] = st.get("skipped", 0) + 1
                elif getattr(ref, "is_erased", lambda: False)():
                    w.add_erased(ce, int(blk), pg, slc, src)
                    st["blank"] += 1
                else:
                    data = ref.read_bytes()
                    st["stored"] += _put_page(w, src, ce, int(blk), pg, slc,
                                              data)
                    st["blank"] += (data.count(0xFF) == len(data))
                st["pages"] += 1
            if int(blk) % every == 0 and st["pages"]:
                el = max(time.time() - t0, 1e-6)
                log(f"    absorb ce{ce} blk{blk}: +{st['pages']} pages, "
                    f"{st['kept']} already held, {st['stored']/2**20:.1f} MiB "
                    f"copied, {st['pages']/el:.0f} pages/s")
    st["seconds"] = time.time() - t0
    w.finish(stats=_stats(ce, lun, st, absorbed=str(source_root),
                          ces=_ces_in(dest_root, ce)))
    return st


def _keys_held(root, ce, modes):
    """{(block, page, slc)} the store at `root` already has records for."""
    from .dumpdev import DumpDevice

    root = pathlib.Path(root)
    if not (root / "store.json").exists():
        return set()
    held = set()
    with DumpDevice(root) as d:
        for blk in modes:
            slc = modes[blk] == "slc"
            for pg in range(geom.pages_per_block(slc)):
                if d.page_ref(ce, int(blk), pg, slc=slc) is not None:
                    held.add((int(blk), pg, slc))
    return held


def merge_stores(sources, out_root, ce, modes, lun=0, log=print, every=400):
    """Combine several part-stores into one canonical store for a CE.

    A dump lands in parts because `StoreWriter.finish()` consumes its buffered
    records -- see `dump_ce_store` -- so the parts have to be joined afterwards.
    `pc_migrate` builds a store from a file tree, not from other stores, so this
    is the missing step rather than a duplicate of it.

    Sources are tried in order and the first that holds a page wins, so pass the
    most trustworthy first. Erasedness is preserved rather than re-derived: a
    page served from a zero-length record is written back as one, so the merge
    does not silently inflate a sparse store into a dense one.

    Coverage is reported, not assumed. A page no source holds is left ABSENT --
    it is not written as erased, because "we never read this" and "the media says
    0xFF" must stay distinguishable.
    """
    from .dumpdev import DumpDevice

    devs = []
    for r in sources:
        try:
            devs.append((pathlib.Path(r), DumpDevice(r)))
        except Exception as e:                              # noqa: BLE001
            log(f"  source {r}: cannot open ({type(e).__name__}: {e}); skipped")
    if not devs:
        raise DeviceError("no readable source stores")

    w, src = _store_writer(out_root, name="gbe-dump-merged")
    st = {"pages": 0, "blank": 0, "stored": 0, "absent": 0}
    from_src = {}
    t0 = time.time()
    for blk in sorted(modes, key=int):
        slc = modes[blk] == "slc"
        for pg in range(geom.pages_per_block(slc)):
            got = None
            for root, d in devs:
                try:
                    got = d.read_page(ce, int(blk), pg, slc=slc)
                except Exception:
                    continue
                from_src[root.name] = from_src.get(root.name, 0) + 1
                break
            if got is None:
                st["absent"] += 1
                continue
            st["stored"] += _put_page(w, src, ce, int(blk), pg, slc, got)
            st["blank"] += (got.count(0xFF) == len(got))
            st["pages"] += 1
        if int(blk) % every == 0:
            el = max(time.time() - t0, 1e-6)
            log(f"    merge ce{ce} blk{blk}: {st['pages']} pages, "
                f"{st['absent']} absent, {st['stored']/2**20:.1f} MiB stored, "
                f"{st['pages']/el:.0f} pages/s")
    w.finish(stats=_stats(ce, lun, st, sources=[str(r) for r, _ in devs],
                          pages_from=from_src))
    st["seconds"] = time.time() - t0
    st["from"] = from_src
    return st


def dump_ce_mapped(dev, ce, path, modes, lun=0, log=print, every=25,
                   index_path=None):
    """Dump only the blocks the media says exist, each in its own cell mode.

    `modes` is {block: "slc"|"tlc"} from `Ftl.cell_mode_of_block` -- a per-WERU
    configuration property, not a block-number rule. This is both faster and
    more correct than sweeping in one mode: on this board every CE resolves to
    580 pseudo-SLC and 3420 native TLC blocks, so a plain-read sweep records 580
    blocks per CE as erased, and an SLC block is 368 pages rather than 1104.

    Blocks are variable length here, so the file is not addressable by a fixed
    stride. An index is written alongside naming the offset, mode and page count
    of every block -- without it the image cannot be interpreted.
    """
    st = {"pages": 0, "blank": 0, "padded": 0, "bad_blocks": 0, "slc": 0,
          "tlc": 0}
    index = []
    total = sum(geom.pages_per_block(m == "slc") for m in modes.values())
    t0 = time.time()
    with open(path, "ab") as f:
        off = f.tell()
        for blk in sorted(modes, key=int):
            slc = modes[blk] == "slc"
            ppb = geom.pages_per_block(slc)
            try:
                data = dev.read_pages(ce, int(blk), 0, ppb, lun=lun, slc=slc)
                pages = data
            except DeviceError as e:
                log(f"    ce{ce} blk{blk}: {e}; PADDING {ppb} pages 0xFF")
                pages = [bytes([0xFF]) * PAGE_BYTES] * ppb
                st["padded"] += ppb
                st["bad_blocks"] += 1
            for pageb in pages:
                f.write(pageb)
                if pageb.count(0xFF) == PAGE_BYTES:
                    st["blank"] += 1
            index.append({"block": int(blk), "lun": lun,
                          "mode": "slc" if slc else "tlc",
                          "offset": off, "pages": ppb})
            off += ppb * PAGE_BYTES
            st["pages"] += ppb
            st["slc" if slc else "tlc"] += 1
            if int(blk) % every == 0:
                el = max(time.time() - t0, 1e-6)
                rate = st["pages"] * PAGE_BYTES / el
                left = (total - st["pages"]) * PAGE_BYTES
                log(f"    ce{ce} blk{blk} ({modes[blk]}): "
                    f"{st['pages']*PAGE_BYTES/2**30:7.2f} GiB "
                    f"{rate/1024:7.1f} KB/s "
                    f"blank {100.0*st['blank']/max(st['pages'],1):5.1f}% "
                    f"eta {left/max(rate,1)/3600:5.1f} h")
    if index_path:
        pathlib.Path(index_path).write_text(json.dumps(index, indent=1))
    st["seconds"] = time.time() - t0
    return st


def dump_ce(dev, ce, path, slc=False, blocks=None, luns=None, start_lun=0,
            start_block=0, log=print, every=25):
    """Walk one CE into `path`. -> dict of counters.

    Appends, so an interrupted dump resumes by passing the block it stopped at.
    """
    ppb = geom.pages_per_block(slc)
    blocks = geom.BLOCKS_PER_LUN if blocks is None else blocks
    luns = geom.LUNS_PER_CE if luns is None else luns
    total = sum(blocks - (start_block if lun == start_lun else 0)
                for lun in range(start_lun, luns)) * ppb
    st = {"pages": 0, "blank": 0, "padded": 0, "bad_blocks": 0}
    t0 = time.time()
    with open(path, "ab") as f:
        for lun in range(start_lun, luns):
            first = start_block if lun == start_lun else 0
            for blk in range(first, blocks):
                try:
                    data = dev.read_pages(ce, blk, 0, ppb, lun=lun, slc=slc)
                except DeviceError as e:
                    log(f"    ce{ce} lun{lun} blk{blk}: {e}; PADDING {ppb} "
                        f"pages 0xFF")
                    f.write(bytes([0xFF]) * (ppb * PAGE_BYTES))
                    st["padded"] += ppb
                    st["bad_blocks"] += 1
                    st["pages"] += ppb
                    continue
                for pageb in data:
                    f.write(pageb)
                    if pageb.count(0xFF) == PAGE_BYTES:
                        st["blank"] += 1
                st["pages"] += ppb
                if blk % every == 0:
                    el = max(time.time() - t0, 1e-6)
                    rate = st["pages"] * PAGE_BYTES / el
                    left = (total - st["pages"]) * PAGE_BYTES
                    log(f"    ce{ce} {'slc' if slc else 'tlc'} lun{lun} "
                        f"blk{blk}: {st['pages']*PAGE_BYTES/2**30:7.2f} GiB "
                        f"{rate/1024:7.1f} KB/s "
                        f"blank {100.0*st['blank']/max(st['pages'],1):5.1f}% "
                        f"eta {left/max(rate,1)/3600:5.1f} h")
    st["seconds"] = time.time() - t0
    return st


def _survey_complete(root, ce, modes):
    """Records in an already-complete survey store at `root`, else 0.

    Complete means one record per page of the cell map -- a survey indexes every
    page, as either a read or a skip, so anything short of that is an
    interrupted run and is redone rather than topped up.
    """
    root = pathlib.Path(root)
    meta = root / "store.json"
    if not meta.exists():
        return 0
    try:
        n = json.loads(meta.read_text())["records"]
    except Exception:                                       # noqa: BLE001
        return 0
    want = sum(geom.pages_per_block(m == "slc") for m in modes.values())
    return n if n >= want else 0


def _forced_blocks(table_path, ce, log=print):
    """Blocks on `ce` an allocated WERU with live AUs claims.  -> [block]

    The metadata half of the union rule. Its job is not to be trusted on its own
    -- it demonstrably misses freed-but-written WERUs -- but to catch anything a
    page-0 survey could miss for the opposite reason: a block whose data does not
    start at page 0. Two rules that fail differently, both cheap.

    Returns [] and says so when there is no table, rather than silently
    degrading the union to the survey alone.
    """
    if not table_path:
        log("    no --weru-table: the survey runs WITHOUT the metadata union, "
            "so a block written from page 1 on would be missed")
        return []
    t = json.loads(pathlib.Path(table_path).read_text())
    out = set()
    for w in t.get("werus", []):
        if w.get("owner") in (None, 0):
            continue
        vc = w.get("valid_count")
        if not isinstance(vc, int) or vc <= 0:
            continue
        for b, c in (w.get("blocks") or []):
            if int(c) == ce:
                out.add(int(b))
    log(f"    weru table: {len(out)} blocks on ce{ce} are claimed by an "
        f"allocated WERU with live AUs")
    return sorted(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ce", default="1,6")
    ap.add_argument("--modes", default="tlc", help="tlc, slc, or tlc,slc")
    ap.add_argument("--blocks", type=int, default=4000)
    ap.add_argument("--luns", type=int, default=None)
    ap.add_argument("--start-lun", type=int, default=0)
    ap.add_argument("--start-block", type=int, default=0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-bringup", action="store_true")
    ap.add_argument("--cellmap", default=None,
                    help="cellmap.json -- read each block once in its own mode")
    ap.add_argument("--lun", type=int, default=0)
    ap.add_argument("--store", action="store_true",
                    help="write a sparse titania.store instead of a flat image")
    ap.add_argument("--first-index", type=int, default=0,
                    help="skip this many blocks of the cell map; applies to "
                         "EVERY --ce in the run, so prefer --resume")
    ap.add_argument("--survey", action="store_true",
                    help="page-0 survey: read in full only the blocks whose "
                         "page 0 is programmed (27.7x less reading on ce1). "
                         "NOT a full capture -- the rest is recorded as skipped")
    ap.add_argument("--weru-table", default=None,
                    help="weru_table.json; blocks claimed by an allocated WERU "
                         "with live AUs are read even if page 0 is blank")
    ap.add_argument("--resume", action="store_true",
                    help="skip the blocks the store already holds in full, "
                         "per CE, read from the store itself")
    a = ap.parse_args(argv)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # No --out: write beside the working directory, stamped, rather than to a
    # fixed volume that exists on one machine.  The CE list is in the name so two
    # concurrent captures of different dies cannot land in one directory.
    out = pathlib.Path(a.out or ("titania_nand_%s_ce%s"
                                 % (stamp, a.ce.replace(",", "-"))))
    out.mkdir(parents=True, exist_ok=True)
    logf = (out / "dump.log").open("a")

    def log(m):
        print(m, flush=True)
        logf.write(m + "\n")
        logf.flush()

    ces = [int(x) for x in a.ce.split(",")]
    dev = GbeDevice(ces=ces, bringup=not a.no_bringup, log=log)
    log(f"# titania.nanddump {stamp}  ces={ces} modes={a.modes} "
        f"blocks={a.start_block}..{a.blocks-1} start_lun={a.start_lun}")

    if a.cellmap:
        cm = json.loads(pathlib.Path(a.cellmap).read_text())
        for ce in ces:
            modes = cm.get(str(ce))
            if not modes:
                log(f"  ce{ce}: not in the cell map; skipping")
                continue
            if a.store:
                # One store per CE, extended in place across runs -- see
                # `StoreWriter.reopen`. A resumed run appends to it rather than
                # starting a part beside it.
                root = out / f"ce{ce}.lun{a.lun}.store"
                if a.survey:
                    # A survey has no partial-resume: re-running it over a
                    # finished store would add a second record for every page,
                    # equal-ranked, which is waste at best and an arbitrary
                    # tie-break at worst. So a complete store is skipped
                    # outright, which is what makes a multi-CE run restartable.
                    done = _survey_complete(root, ce, modes)
                    if done:
                        log(f"  ce{ce}: already surveyed ({done} records); "
                            f"skipping. Delete the store to redo it.")
                        continue
                    forced = _forced_blocks(a.weru_table, ce, log)
                    log(f"  ce{ce} -> sparse store {root.name} "
                        f"(PAGE-0 SURVEY, {len(forced)} blocks forced by the "
                        f"weru table)")
                    st = dump_ce_survey(dev, ce, root, modes, lun=a.lun,
                                        log=log, also_blocks=forced)
                    log(f"  ce{ce} survey DONE: {st['hit']} blocks read in "
                        f"full, {st['skipped']} pages skipped, "
                        f"{st['stored']/2**30:.3f} GiB stored, "
                        f"{st['bad_blocks']} unreadable, "
                        f"{st['seconds']/60:.1f} min")
                    continue
                log(f"  ce{ce} -> sparse store {root.name} "
                    + ("(resuming from the store)" if a.resume
                       else f"(from block index {a.first_index})"))
                st = dump_ce_store(dev, ce, root, modes, lun=a.lun, log=log,
                                   first_index=a.first_index, resume=a.resume)
                on_disk = sum(f.stat().st_size for f in root.rglob("*")
                              if f.is_file())
                log(f"  ce{ce} store DONE: {st['pages']} pages, "
                    f"{st['blank']} erased ({100.0*st['blank']/max(st['pages'],1):.1f}%), "
                    f"{on_disk/2**30:.3f} GiB on disk vs "
                    f"{st['pages']*PAGE_BYTES/2**30:.1f} GiB raw, "
                    f"{st['bad_blocks']} unreadable blocks, "
                    f"{st['seconds']/3600:.2f} h")
                continue
            path = out / f"ce{ce}.lun{a.lun}.mapped.bin"
            idx = out / f"ce{ce}.lun{a.lun}.index.json"
            log(f"  ce{ce} mapped ({len(modes)} blocks) -> {path.name}")
            st = dump_ce_mapped(dev, ce, path, modes, lun=a.lun, log=log,
                                index_path=idx)
            log(f"  ce{ce} mapped DONE: {path.stat().st_size/2**30:.2f} GiB in "
                f"{st['seconds']/3600:.2f} h, {st['slc']} slc + {st['tlc']} tlc "
                f"blocks, {st['blank']}/{st['pages']} all-0xFF, "
                f"{st['bad_blocks']} padded")
        logf.close()
        return

    for ce in ces:
        for mode in a.modes.split(","):
            slc = mode == "slc"
            path = out / f"ce{ce}.{mode}.bin"
            log(f"  ce{ce} {mode} -> {path.name}")
            st = dump_ce(dev, ce, path, slc=slc, blocks=a.blocks, luns=a.luns,
                         start_lun=a.start_lun, start_block=a.start_block,
                         log=log)
            log(f"  ce{ce} {mode} DONE: {path.stat().st_size/2**30:.2f} GiB in "
                f"{st['seconds']/3600:.2f} h, {st['blank']}/{st['pages']} "
                f"all-0xFF, {st['bad_blocks']} blocks padded")
    logf.close()


if __name__ == "__main__":
    sys.exit(main())
