"""Extraction: an MSO's logical address space, off the media, with evidence.

This is the layer that turns the rest of the package into a tool you can point
at a drive.  Everything below it answers questions about *one* address; this
answers "give me LMAs a..B of MSO N, tell me which of them you actually got,
and prove it".

    py -m titania extract --mso 32 \\
                          --lma 1149856:1149872 -o out.bin

    from titania.extract import Extractor, FtlResolver
    ex = Extractor(dev, FtlResolver(ftl), out="out.bin")
    st = ex.run(mso=32, lmas=range(1149856, 1149872))

THE FOUR PROMISES
-----------------
1.  Every AU is VERIFIED, not merely decoded.  The load-bearing check is
    that the AU's own aux LMA equals the LMA that was looked up.  That single
    equality exercises the entire chain -- LUT entry, WMA arithmetic, WERU slot
    rule, page address, cell mode, scrambler seed, ECC -- because every one of
    those being wrong lands on an AU whose aux says a different number.

    MCRC is recorded per AU as well, but MCRC is not the proof.  It is computed
    over the complemented stored bytes, i.e. before descrambling, so it attests
    that the page came off the die intact and says nothing about whether it was
    read in the right cell mode or whether the address that produced it was the
    right one.  Only the aux LMA ties the bytes to the LMA that asked for them,
    and only that match puts bytes in the output file.

2.  It resumes.  At ~47 MiB/hour off the live board a run that dies at hour
    three must not start over.  A JSONL manifest is written as the run goes, one
    record per LMA plus a run record per invocation, and re-running with the same
    arguments continues from the set of LMAs already accounted for.  Resume works
    by set membership, not by a high-water mark, so widening the range on a
    later run picks up the LMAs that were never attempted rather than skipping
    to the end.

3.  Provenance travels with the bytes.  Every record carries
    (ce, block, page, au), the cell mode the read used, how the page was
    obtained (transferred / device-reported-erased / cached), the WMA and WERU
    it came through, whether LDPC correction was needed, the page CRC, and --
    when it was not the die's default -- the read-retry level the bytes finally
    came back at.  For recovery work that is the difference between evidence
    and a blob.

4.  It never invents bytes.  A gap is a gap.  In the default `sparse`
    layout a missing LMA is simply never written -- the file has a hole there --
    and in `packed` layout it does not occupy the file at all.  `--fill` will
    synthesise 0x00 or 0xFF over gaps if a caller explicitly asks, and every
    synthesised LMA is marked `"syn": true` in the manifest.  Confusing
    "missing" with "erased" has produced wrong conclusions in this project more
    than once; the manifest exists so that it cannot happen quietly.

THE STATUS VOCABULARY
---------------------
Eight outcomes, not collapsed into "ok / not ok".  Which kind of
failure it was is usually the whole question.

    verified    decoded, MCRC good, aux LMA == the LMA asked for, and no LDPC
                correction was needed.  Bytes written.
    corrected   the same, but at least one codeword needed LDPC correction.
                Bytes written.  The data is good; the media under it is not as
                healthy, and that is worth knowing separately.
    failed      the page was read and decoded but the AU does not check out:
                MCRC bad, or the aux LMA names a different LMA.  No bytes
                written -- this is the case where plausible-looking wrong bytes
                would otherwise enter the output.
    erased      the page reads all-0xFF where the LUT expects data.  A real
                observation about the media, not the same as "missing".
    unmapped    the LUT maps this LMA nowhere (a sentinel, or past the end, or
                the unused tail of a 192-WMA window).  A real negative, from
                the firmware's own tables.
    unresolved  the LUT maps it somewhere but we cannot turn that somewhere
                into an address here -- the WERU's slot rule or its cell mode is
                not established.  It is correct, not a bug to paper over.
                No longer the common answer for user data (2026-08-07): the
                48-block slot law is measured (3568/0 against an independent
                aux-LMA route, `sd_slotlaw.py`), so a rectangular record now
                resolves.  What still lands here is a named, bounded set of
                refusals -- 101 partial records, 29 `substituted` ones whose
                four block numbers are not consecutive, 3 invalid, 2 irregular.
    unreadable  this backing does not hold the page, or the device errored.
    dead-ce     the address lands on a dead CE.  CE0 on this board is dead and
                answers, with a constant page built from just 0x00 and 0xA7 and
                containing no 0xFF at all, so it is neither programmed nor
                erased and nothing else in the stack flags it.  Detected by that
                signature as well as by CE number -- see `is_dead_ce_page`,
                which also records what the signature actually is.  Its bytes
                are never used.

`verified` and `corrected` are the only two that put bytes in the file.

WHAT THIS DOES NOT DO
---------------------
It hands back the plaintext the FTL would hand the host.  Main user data is
AES-encrypted above this layer -- the extracted payloads measure ~7.997 bits of
entropy per byte -- and turning that into anything readable needs console keys,
which are outside this tool entirely.  An extract that looks like noise is very
probably a correct extract of ciphertext; the aux-LMA count is how you tell.

THE READ-RETRY LADDER
---------------------
A third of every native-TLC block on this board -- 360 of its 1104 pages, the
`(page - 8) % 3 == 2` "upper" word lines -- does not decode at the die's default
read references.  Those pages are not damaged.  Their charge distributions have
drifted, the comparison is being made in the wrong place, and Samsung ships a
32-step ladder of alternative reference sets (SET FEATURES 0xEF) for exactly
this.  Measured across two sessions: 113 upper pages, 113 rescued, every one at
ladder step 1.  Independent levels return byte-identical CORRECTED data, so this
is recovery, not decoder luck.

So a level-0 read that does not decode is not the end of the attempt here.  The
extractor re-reads at each level of a shortlist (`RETRY_SHORTLIST`, `(1, 6, 15)`
-- level 1 has rescued 113 of 113) and takes the first that decodes.  Three
rules make it evidence rather than a fudge:

  * The verification bar does not move.  A page rescued at level 6 still
    only becomes bytes in the file when the AU's own aux LMA equals the LMA that
    was looked up.  Retry changes how the bytes were obtained, never whether
    they are believed.

  * The winning level is recorded as `"rty": n` in the manifest record.  A
    page that only came back at level n is a measurement of that block's health;
    folding it in as an ordinary read throws that measurement away.  The field
    is absent for level 0, and the run record says whether the ladder was
    available at all, so "read at the defaults" and "no ladder here" stay
    distinguishable.

  * An undecodable page is still withheld.  `read_page_retry` refuses to
    return a least-bad read and this layer does not second-guess it: if no level
    decodes, the LMA is `unreadable` and nothing reaches the file.

`--no-read-retry` turns it off; `--retry-levels` sets the shortlist.  Backings
without a ladder (`DumpDevice`) are unaffected -- `Device.supports_retry` is
False there and no walk is attempted, which is why the same code runs over a
dump.  A page that reads differently at two levels is not corrupt at either;
the syndrome count is the quality metric, not byte stability.
"""
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


if __package__ in (None, ""):                       # `py titania/extract.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import DeviceError, FULL
else:
    from . import geometry as geom
    from .device import DeviceError, FULL

#: This package's name whether this file was imported as `titania.extract` or
#: run directly as a script (in which case the header above put the parent on
#: sys.path).  Sibling layers are reached through it, so both entry points work
#: and neither needs a relative import that only one of them can satisfy.
_PKG = __package__ or "titania"


def _sib(name):
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))


def _load_opt(name):
    """A sibling that this module works without.  -> (module, None) | (None, why)."""
    try:
        return _sib(name), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)

__all__ = [
    "LMA_BYTES", "STATUSES", "WRITTEN_STATUSES", "FINAL_STATUSES",
    "RETRY_SHORTLIST", "RETRY_ALL", "parse_retry_levels",
    "Resolver", "FtlResolver", "StaticResolver",
    "Manifest", "ResumeMismatch", "Sink", "Extractor", "Stats",
    "parse_lma_range", "lma_sequence", "is_dead_ce_page", "selftest",
]

#: One LMA is one AU's user payload.
LMA_BYTES = geom.AU_DATA                            # 4096

#: Offset of the little-endian u32 LMA inside an AU's 8 aux bytes.  Mirrors
#: `titania.ftl.AUX_LMA_OFF`; read from there when available so the two cannot
#: drift apart.
AUX_LMA_OFF = 0

VERIFIED = "verified"
CORRECTED = "corrected"
FAILED = "failed"
ERASED = "erased"
UNMAPPED = "unmapped"
UNRESOLVED = "unresolved"
UNREADABLE = "unreadable"
DEAD_CE = "dead-ce"
SKIPPED = "skipped"                                 # survey-mode placeholder

STATUSES = (VERIFIED, CORRECTED, FAILED, ERASED, UNMAPPED, UNRESOLVED,
            UNREADABLE, DEAD_CE, SKIPPED)

#: The two statuses that put bytes in the output file.
WRITTEN_STATUSES = (VERIFIED, CORRECTED)

#: Statuses a plain resume will not revisit.  `unmapped` is the firmware's own
#: table speaking and will not change; `verified`/`corrected` are done.
#: Everything else can change -- a better WERU rule turns `unresolved` into an
#: address, a dump gaining pages turns `unreadable` into data -- so `--retry`
#: revisits those, and by default we do not, because a resume that silently
#: re-does work is a resume you cannot reason about.
FINAL_STATUSES = (VERIFIED, CORRECTED, UNMAPPED)

MANIFEST_KIND = "titania-extract-manifest"
MANIFEST_VERSION = 1

#: Read-retry levels to try, in order, after the die's defaults have failed.
#:
#: Not the whole 32-step ladder, and the difference is the whole point.  Every
#: extra level is a full 18432-byte re-read of a page that has already failed
#: once, so walking all 32 costs 32x the transfer on precisely the pages that
#: are slowest to begin with.  The measurement says it buys nothing: across two
#: sessions, 113 upper-word-line pages were rescued and all 113 came back at
#: step 1.  Steps 6 and 15 are here as a cheap hedge -- they are the next two
#: the firmware's own ladder reaches for -- and if a page fails all three, that
#: is itself worth knowing rather than worth grinding at.
#:
#: The order is the firmware's order and must not be sorted, interpolated or
#: averaged: each step is a calibrated seven-way shift of the reference set, and
#: the ladder is not monotonic in voltage.
#: Contradiction, flagged not fixed (2026-08-07).  The measurement this
#: shortlist rests on -- "113 upper pages, 113 rescued, every one at step 1" --
#: has been SUPERSEDED by a direct ladder walk over a far larger sample, and the
#: newer result disagrees with this tuple on both counts:
#:
#:   * order.  A/B on one 352-page band: `6,1,4` = 352 reads for 352 pages
#:     (1.000), `1,6,4` = 538 (1.528).  Widened to 819 pages / 820 reads,
#:     819/819.  Step 6 subsumes step 1 and step 4 on that band.  Step 1 is
#:     still load-bearing as the fallback -- `ce4 b3648 p145` misses 6 and wins
#:     at 1, and is the single extra read in 820 -- but it should be second.
#:   * step 15.  It has never won a page.  `NAND_RAW_IMAGE.md` records
#:     "Step 15 never won"; the shortlist that the measurement endorses is
#:     `(6, 1, 4)`, with 4 -- not 15 -- as the third rung.
#:
#: Resolved 2026-08-07: changed to `(6, 1, 4)`.  The flag above was raised while
#: a hardware session might have been in flight; the board is now idle with no
#: capture running, so the constant is corrected rather than left stale.
#:
#: To be precise about what this was and was not: `(1, 6, 15)` was not a
#: coverage bug.  Step 6 subsumes step 1 and step 4, and step 1 first catches
#: the one known page that misses 6, so `(1, 6, ...)` decodes everything
#: measured.  It was a cost bug -- 1.528 reads/page against 1.000 -- plus a dead
#: third rung.  On a bulk capture that is ~1.5x the serial time for nothing.
#:
#: Order rationale, all measured (see NAND_TOOLS.md and NAND_FORMAT_STATE.md):
#:   6 first  -- wins 819 of 820 reads over 819 pages on the widened sample.
#:   1 second -- load-bearing, not decoration: `ce4 b3648 p145` (weru 590)
#:               misses step 6 and wins at step 1.  It is the single extra read
#:               in 820.  Do not collapse this to `(6,)`.
#:   4 third  -- the rung the measurement endorses; step 15 has never won a page.
#:
#: Still true and still important: the order is the firmware's and must not be
#: sorted, interpolated or averaged -- each step is a calibrated seven-way shift
#: of the reference set and the ladder is not monotonic in voltage.  Also note
#: the per-page winner is not read-to-read stable (the same band gave 209 step-1
#: winners under one order and 166 under another), so a winning step is a
#: property of the read, not of the page.
RETRY_SHORTLIST = (6, 1, 4)

#: `retry_levels=RETRY_ALL` walks whatever ladder the device reports, in the
#: device's order.  Spelled as a sentinel rather than a hard-coded range so the
#: length comes from the die and not from this file.
RETRY_ALL = "all"


def parse_retry_levels(text):
    """'1,6,15' | 'all' | 'none' -> a tuple, RETRY_ALL, or ().

    `()` disables the walk exactly as `read_retry=False` does; both are kept
    because "I asked for no levels" and "I turned the feature off" are the same
    outcome and should not need to be spelled differently.
    """
    s = str(text).strip().lower()
    if s in ("all", "full", "ladder"):
        return RETRY_ALL
    if s in ("", "none", "off", "0"):
        return ()
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part, 0)
        if v < 0:
            raise ValueError("retry level %d is negative" % v)
        if v and v not in out:
            out.append(v)
    return tuple(out)

#: `\xa7` * 2304 then zeros -- what CE0 returns on this board.  Neither
#: programmed nor erased; a constant the die makes up.  See NAND_TOOLS.md.
#: The only two byte values a dead-but-answering CE emits.
DEAD_CE_BYTE = 0xA7
DEAD_CE_ALPHABET = (0x00, DEAD_CE_BYTE)
_ONES = {}


def _const(cache, byte, n):
    v = cache.get(n)
    if v is None:
        v = cache[n] = bytes([byte]) * n
    return v


def is_dead_ce_page(raw):
    """-> True for the constant page a dead-but-answering CE returns.

    Checked by CONTENT as well as by CE number, because the number is a policy
    and the content is a measurement.  A page that matches this is not evidence
    about anything: it is what CE0 says regardless of the address asked for.

    MEASURED, and it corrects the description this was written from.  All 71
    CE0 pages in `the dump` are period-32 and draw on exactly two byte values,
    0x00 and 0xA7, with not one 0xFF among them -- so the page is neither
    programmed nor erased and no other test in the stack recognises it.  But
    there are two variants, not one: 63 pages carry 2304 0xA7 bytes and 8 carry
    2880, and in neither case are they a contiguous prefix -- the 0xA7s are
    scattered through each 32-byte period.  "2304 x 0xA7 then 16128 x 0x00" is
    a byte count that was read as a layout; a prefix test finds 8 of the 71.

    So the test is the alphabet, which both variants satisfy and which nothing
    else does: over 3992 non-CE0 pages sampled from the same dump, not one
    consists solely of 0x00 and 0xA7.

    `titania.dumpdev.DEAD_CE_ALPHABET` is the same set, arrived at separately.
    The constant is kept local rather than imported so that this module stays
    usable without the dump backing; if the two ever disagree, dumpdev's is the
    one that sees more pages and should win.
    """
    if not raw:
        return False
    return raw.count(0x00) + raw.count(DEAD_CE_BYTE) == len(raw)


# ===========================================================================
# resolving an LMA to a place on the media
# ===========================================================================
class Resolver(object):
    """(mso, lma) -> where those 4096 bytes live.

    Behind an interface for two reasons.  The real one is `FtlResolver`, which
    walks the firmware's own tables; the selftest needs a resolver it can state
    the right answer for without a drive, and a future reader of a different
    drive's format needs somewhere to plug in.  Nothing in `Extractor` knows
    which it has.

    `resolve` returns exactly what `Ftl.lma_to_physical` returns, and the three
    cases must stay distinguishable all the way to the manifest:

        Placement  an address (not a promise the backing holds those bytes)
        None       the LUT maps this LMA nowhere -- a real negative
        UNKNOWN    mapped, but not resolvable from what we hold
    """

    def resolve(self, mso, lma):
        raise NotImplementedError

    def wma(self, mso, lma):
        """-> the intermediate WMA for the record, or None.  Optional."""
        return None

    def coverage(self, mso):
        """-> how many LMAs this MSO's address space spans, or None."""
        return None

    def describe(self):
        return type(self).__name__


class FtlResolver(Resolver):
    """The real one: `Ftl.lma_to_physical`, plus the WMA for provenance.

    Cell mode rides along on the `Placement`.  This class never looks at a
    block number to decide it -- 139 WERUs on this board are pseudo-SLC and 855
    are native TLC, interleaved, and a wrong-mode read does not error.  It
    returns ~1000 LDPC syndromes per codeword, which looks exactly like an
    unwritten block.
    """

    def __init__(self, ftl):
        self.ftl = ftl
        self._F = _sib("ftl")

    def resolve(self, mso, lma):
        return self.ftl.lma_to_physical(mso, lma)

    def wma(self, mso, lma):
        w = self.ftl.lma_to_wma(mso, lma)
        return w if isinstance(w, int) else None

    def coverage(self, mso):
        reg = self.ftl.lut_region(mso)
        if reg is None or reg is self._F.UNKNOWN:
            return None
        n = getattr(reg, "covered_lmas", None)
        return n if isinstance(n, int) else None

    def describe(self):
        return "FtlResolver over %r" % (self.ftl,)


class StaticResolver(Resolver):
    """A dict of answers.  For tests and for replaying a survey off disk."""

    def __init__(self, table, wmas=None, coverage=None):
        self.table = dict(table)                    # (mso, lma) -> answer
        self.wmas = dict(wmas or {})
        self._coverage = coverage or {}

    def resolve(self, mso, lma):
        return self.table.get((mso, lma), _sib("ftl").UNKNOWN)

    def wma(self, mso, lma):
        return self.wmas.get((mso, lma))

    def coverage(self, mso):
        return self._coverage.get(mso)


# ===========================================================================
# the manifest
# ===========================================================================
class ResumeMismatch(Exception):
    """An existing manifest describes a different extraction than this one."""


class Manifest(object):
    """Append-only JSONL: what was attempted, what came of it, and where.

    One line of JSON per record, because that is the format that survives being
    killed halfway: a truncated last line is discarded on reopen and everything
    before it is still readable, with no index to rebuild and no chance of a
    half-rewritten file.  Line 1 is the header; every invocation appends a
    `run` record; everything else is one record per LMA.

    Ordering RULE, and it matters.  Data bytes are flushed to the output file
    before the manifest lines that claim them.  So the manifest is always a
    subset of what is on disk, never a superset: a crash loses records and
    those LMAs get redone, which is safe, rather than losing bytes the manifest
    swears are there, which is not.
    """

    def __init__(self, path, header, restart=False, batch=32):
        self.path = Path(path)
        self.batch = int(batch)
        self.records = {}               # lma -> the last record for it
        self.runs = []
        self.header = dict(header)
        self._pending = []
        self._fh = None
        self._resumed = False
        self._n_appended = 0
        if self.path.exists() and self.path.stat().st_size and not restart:
            self._reopen()
        else:
            if self.path.exists() and restart:
                self.path.unlink()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
            self._emit(dict(self.header, kind=MANIFEST_KIND,
                            v=MANIFEST_VERSION))

    # -- identity ---------------------------------------------------------
    #: Fields that must agree for a manifest to be resumable.  Not
    #: the LMA range: widening a range on a later run is a legitimate thing to
    #: do and must add work rather than be refused.  `base_lma` is included,
    #: because it fixes where every byte lands in the file.
    IDENTITY = ("mso", "base_lma", "lma_bytes", "layout", "fill", "out")

    def _reopen(self):
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        tail_partial = bool(lines and lines[-1].strip())
        good, bad = [], 0
        for ln in lines:
            if not ln.strip():
                continue
            try:
                good.append(json.loads(ln.decode("utf-8")))
            except Exception:
                bad += 1
        if not good:
            raise ResumeMismatch("%s exists but holds no readable records; "
                                 "move it aside or pass --restart" % self.path)
        head = good[0]
        if head.get("kind") != MANIFEST_KIND:
            raise ResumeMismatch("%s is not a titania extract manifest (kind=%r)"
                                 % (self.path, head.get("kind")))
        diff = [(k, head.get(k), self.header.get(k))
                for k in self.IDENTITY
                if head.get(k) != self.header.get(k)]
        if diff:
            raise ResumeMismatch(
                "%s describes a different extraction:\n%s\n"
                "  resuming into it would put bytes at the wrong offsets.  Use a "
                "different -o/--manifest, or --restart to discard it."
                % (self.path, "\n".join("    %-9s manifest=%r  now=%r"
                                        % d for d in diff)))
        for rec in good[1:]:
            if rec.get("kind") == "run":
                self.runs.append(rec)
            elif "lma" in rec:
                self.records[rec["lma"]] = rec
        # Drop a torn final line before appending, so the file stays parseable.
        if tail_partial or bad:
            keep = raw.rfind(b"\n")
            with open(self.path, "r+b") as f:
                f.truncate(keep + 1 if keep >= 0 else 0)
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        self._resumed = True

    # -- properties -------------------------------------------------------
    @property
    def resumed(self):
        return self._resumed

    def status(self, lma):
        r = self.records.get(lma)
        return None if r is None else r.get("st")

    def done(self, lma, retry=False):
        """Has this LMA been accounted for by a previous run?"""
        st = self.status(lma)
        if st is None:
            return False
        return st in FINAL_STATUSES if retry else True

    def next_offset(self):
        """Highest end-of-data offset any record claims.  For packed layout."""
        end = 0
        for r in self.records.values():
            if r.get("off") is not None and r.get("st") in WRITTEN_STATUSES:
                end = max(end, r["off"] + r.get("len", LMA_BYTES))
        return end

    def tally(self):
        out = {}
        for r in self.records.values():
            st = r.get("st", "?")
            out[st] = out.get(st, 0) + 1
        return out

    def retry_tally(self):
        """-> {level: n} over the records that name a non-default read level.

        Level 0 is ABSENT rather than counted, because a record
        with no `rty` field means one of two different things -- read at the
        defaults, or read by a backing with no ladder -- and only the run record
        says which.  Inventing a `0` bucket here would merge them.
        """
        out = {}
        for r in self.records.values():
            lv = r.get("rty")
            if lv:
                out[lv] = out.get(lv, 0) + 1
        return out

    # -- writing ----------------------------------------------------------
    def begin_run(self, info):
        rec = dict(info, kind="run")
        self.runs.append(rec)               # as `_reopen` would have read it back
        self._emit(rec)
        self.flush()

    def add(self, rec):
        self.records[rec["lma"]] = rec
        self._pending.append(rec)
        return len(self._pending) >= self.batch

    def _emit(self, rec):
        self._fh.write(json.dumps(rec, separators=(",", ":"),
                                  sort_keys=False) + "\n")
        self._n_appended += 1

    def flush(self, fsync=False):
        for rec in self._pending:
            self._emit(rec)
        self._pending = []
        if self._fh is not None:
            self._fh.flush()
            if fsync:
                os.fsync(self._fh.fileno())

    def close(self):
        if self._fh is not None:
            self.flush(fsync=True)
            self._fh.close()
            self._fh = None

    def __repr__(self):
        return "<Manifest %s %d records%s>" % (
            self.path.name, len(self.records),
            " (resumed)" if self._resumed else "")


# ===========================================================================
# the output file
# ===========================================================================
class Sink(object):
    """Where extracted LMAs land.

    Two layouts, and the difference is entirely about what a gap looks like.

    `sparse`  file offset = (lma - base) * 4096.  The file is an image of the
              LMA space, which is what you want if you intend to look at
              structure in it -- but a missing LMA becomes a file hole, and a
              hole reads back as zeros.  Those zeros are the operating
              system's, not the drive's, and only the manifest distinguishes
              them from real data.  Anything reporting on this file must say
              so; the CLI does.

    `packed`  successfully extracted LMAs are concatenated in order and the
              manifest records each one's offset.  Nothing is invented at all,
              at the cost of the file no longer being an image of anything.
              Use it when the extract is evidence rather than a filesystem.

    `fill`    synthesise 0x00 or 0xFF over the gaps (sparse layout only) when
              the caller explicitly asks.  Those LMAs are marked `"syn": true`
              in the manifest, always.  It exists because sometimes you really
              do want a fixed-size image to feed something else; it is off by
              default because an unlabelled 0xFF in a NAND image reads as
              "erased" to every human who sees it.
    """

    def __init__(self, path, base_lma, layout="sparse", fill=None,
                 next_offset=0):
        self.path = Path(path) if path is not None else None
        self.base_lma = int(base_lma)
        self.layout = layout
        if fill is not None and layout == "packed":
            raise ValueError(
                "--fill and --layout packed are contradictory: packed has no "
                "offsets to fill, since only extracted LMAs occupy the file. "
                "Use sparse if you want a fixed-size image.")
        self.fill = fill                            # None / 0x00 / 0xFF
        self.n_written = 0
        self.n_synthesised = 0
        self._next = int(next_offset)
        self._fh = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            mode = "r+b" if self.path.exists() else "w+b"
            self._fh = open(self.path, mode)

    @property
    def enabled(self):
        return self._fh is not None

    def offset_for(self, lma):
        if self.layout == "packed":
            return self._next
        return (lma - self.base_lma) * LMA_BYTES

    def write(self, lma, data):
        """-> the offset the bytes went to, or None when there is no file."""
        if self._fh is None:
            return None
        if len(data) != LMA_BYTES:
            raise ValueError("LMA %d: %d bytes, expected %d"
                             % (lma, len(data), LMA_BYTES))
        off = self.offset_for(lma)
        self._fh.seek(off)
        self._fh.write(data)
        if self.layout == "packed":
            self._next = off + LMA_BYTES
        self.n_written += 1
        return off

    def synthesise(self, lma):
        """Fill a gap, if and only if the caller asked for one.  -> offset/None."""
        if self._fh is None or self.fill is None or self.layout == "packed":
            return None
        off = self.write(lma, bytes([self.fill]) * LMA_BYTES)
        self.n_written -= 1
        self.n_synthesised += 1
        return off

    def flush(self, fsync=False):
        if self._fh is not None:
            self._fh.flush()
            if fsync:
                os.fsync(self._fh.fileno())

    def size(self):
        if self._fh is None:
            return 0
        self._fh.flush()
        return os.fstat(self._fh.fileno()).st_size

    def close(self):
        if self._fh is not None:
            self.flush(fsync=True)
            self._fh.close()
            self._fh = None


# ===========================================================================
# the extractor
# ===========================================================================
class Stats(object):
    """Counts for one invocation.  `by_status` is over the LMAs this run did."""

    def __init__(self):
        self.by_status = {}
        self.attempted = 0
        self.skipped_resume = 0
        self.pages_read = 0
        self.pages_cached = 0
        self.corrected_pages = 0
        self.bytes_written = 0
        #: pages that failed at level 0 and were walked up the ladder
        self.retry_walks = 0
        #: of those, the ones a level actually rescued
        self.retry_rescued = 0
        #: {level: pages read at it}, including 0 -- here, unlike in the
        #: manifest, the run's own device is known, so a 0 bucket is a fact
        #: about this run rather than an ambiguity.
        self.by_retry_level = {}
        self.t0 = time.time()
        self.elapsed = 0.0

    def note_retry(self, level):
        self.by_retry_level[level] = self.by_retry_level.get(level, 0) + 1

    def note(self, st):
        self.by_status[st] = self.by_status.get(st, 0) + 1

    def rate_mib_h(self):
        el = self.elapsed or (time.time() - self.t0)
        if el <= 0:
            return 0.0
        return (self.bytes_written / 1048576.0) * 3600.0 / el

    def as_dict(self):
        return {"attempted": self.attempted,
                "skipped_resume": self.skipped_resume,
                "pages_read": self.pages_read,
                "pages_cached": self.pages_cached,
                "corrected_pages": self.corrected_pages,
                "bytes_written": self.bytes_written,
                "retry_walks": self.retry_walks,
                "retry_rescued": self.retry_rescued,
                "by_retry_level": dict(sorted(self.by_retry_level.items())),
                "elapsed_s": round(self.elapsed, 3),
                "by_status": dict(sorted(self.by_status.items()))}


class _PageInfo(object):
    """One decoded page, cached so four consecutive LMAs cost one read.

    A user LUT granule is 16 LMAs = 4 slots x 4 AUs, so LMAs k..k+3 of a
    granule live in one page and k+4.. live in the next block over.  Even a tiny
    cache therefore removes three reads in four; on hardware that is the
    difference between 47 MiB/hour and a quarter of it.
    """
    __slots__ = ("aus", "mcrc", "mcrc0", "corrected", "erased", "dead",
                 "source", "crc", "error", "retry", "retry_tried")

    def __init__(self, aus=None, mcrc=None, mcrc0=None, corrected=False,
                 erased=False, dead=False, source=None, crc=None, error=None,
                 retry=0, retry_tried=()):
        self.aus, self.mcrc, self.mcrc0 = aus, mcrc, mcrc0
        self.corrected, self.erased, self.dead = corrected, erased, dead
        self.source, self.crc, self.error = source, crc, error
        #: the read-retry level these bytes finally came back at.  0 is the
        #: die's default references, which is also what a backing with no
        #: ladder always reports.
        self.retry = retry
        #: the levels a ladder walk actually tried, when one happened.  Kept so
        #: a failure can say "three levels, none of them worked" rather than
        #: leaving the reader to guess whether anything was tried at all.
        self.retry_tried = tuple(retry_tried)


class Extractor(object):
    """Run an LMA range through resolve -> read -> decode -> verify -> write.

    Construct it with a `Device`, a `Resolver` and somewhere to put the output;
    call `run(mso, lmas)`.  It owns no hardware policy of its own: reads go
    through whatever `Device` it was handed, which is what makes the same code
    work over a dump and over the board.

    The verification is not optional and there is no flag to turn it off.  A
    mode that wrote unverified bytes would be indistinguishable, in the file it
    produced, from a mode that worked -- and this project has spent enough time
    disproving plausible-looking bytes.
    """

    def __init__(self, device, resolver, codec=None, out=None, manifest=None,
                 layout="sparse", fill=None, base_lma=None, restart=False,
                 retry=False, page_cache=64, dead_ces=None, survey=False,
                 batch=32, verbose=False, log=None, read_retry=True,
                 retry_levels=None):
        self.dev = device
        self.resolver = resolver
        self.layout = layout
        self.fill = fill
        #: Resume policy -- revisit LMAs previously recorded non-final.  This
        #: has nothing to do with the read-retry ladder below and the two names
        #: are unfortunately close; `retry` is about the manifest and
        #: `read_retry` is about the die.
        self.retry = bool(retry)
        #: walk the read-retry ladder when a page does not decode at level 0
        self.read_retry = bool(read_retry)
        self.retry_levels = (RETRY_SHORTLIST if retry_levels is None
                             else retry_levels)
        self.survey = bool(survey)
        self.verbose = verbose
        self.page_cache = int(page_cache)
        self.dead_ces = tuple(geom.CE_DEAD if dead_ces is None else dead_ces)
        self.log = log or (lambda s: print(s, flush=True))
        self._cache = {}
        self._cache_order = []
        self._out_path = out
        self._manifest_path = manifest
        self._base_lma = base_lma
        self._restart = restart
        self._batch = int(batch)
        self.manifest = None
        self.sink = None
        F = _sib("ftl")
        if codec is None and not survey:
            codec = F.default_codec()
        self.codec = codec
        self._F = F
        self._aux_off = getattr(F, "AUX_LMA_OFF", AUX_LMA_OFF)

    # -- setup ------------------------------------------------------------
    def open(self, mso, base_lma, header_extra=None):
        """Open (or resume into) the manifest and the output file."""
        self._base_lma = base_lma
        header = {"mso": mso, "base_lma": base_lma, "lma_bytes": LMA_BYTES,
                  "layout": self.layout,
                  "fill": None if self.fill is None else int(self.fill),
                  "out": None if self._out_path is None
                         else Path(self._out_path).name,
                  "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "tool": "titania.extract/%d" % MANIFEST_VERSION}
        header.update(header_extra or {})
        mpath = self._manifest_path
        if mpath is None:
            if self._out_path is None:
                raise ValueError("give either -o or --manifest: a run with "
                                 "neither records nothing and is not resumable")
            mpath = str(self._out_path) + ".manifest.jsonl"
        self.manifest = Manifest(mpath, header, restart=self._restart,
                                 batch=self._batch)
        self.sink = Sink(None if self.survey else self._out_path, base_lma,
                         layout=self.layout, fill=self.fill,
                         next_offset=self.manifest.next_offset())
        return self.manifest

    def close(self):
        if self.sink is not None:
            self.sink.close()
        if self.manifest is not None:
            self.manifest.close()

    # -- page level -------------------------------------------------------
    def _page(self, ce, block, page, slc, stats):
        key = (ce, block, page, bool(slc))
        hit = self._cache.get(key)
        if hit is not None:
            stats.pages_cached += 1
            return hit
        info = self._read_and_decode(ce, block, page, slc, stats)
        stats.pages_read += 1
        stats.note_retry(info.retry)
        if info.corrected:
            stats.corrected_pages += 1
        if self.page_cache:
            self._cache[key] = info
            self._cache_order.append(key)
            while len(self._cache_order) > self.page_cache:
                self._cache.pop(self._cache_order.pop(0), None)
        return info

    def _read_and_decode(self, ce, block, page, slc, stats=None):
        """Read at the die's defaults; walk the ladder only if that failed."""
        kw = {"slc": slc}
        if self.read_retry and getattr(self.dev, "supports_retry", False):
            # Assert level 0 rather than inheriting whatever the target happens
            # to be on.  A page's provenance says "read at the defaults", and
            # that has to be a fact: a stray level left by some other caller
            # would otherwise be recorded as level 0 and be invisible.  It is
            # nearly free -- `set_retry_level` writes only on a real transition,
            # and the ladder walk's own `finally` has already put the target
            # back -- so the cost is at most one 0xEF per CE per session.
            #
            # Gated on `read_retry` because --no-read-retry promises that no
            # set-feature reaches the bus, and that promise outranks this one.
            kw["retry"] = 0
        try:
            pr = self.dev.read_page_ex(ce, block, page, **kw)
        except DeviceError as exc:
            return _PageInfo(error=str(exc))
        except Exception as exc:                    # a backing may raise its own
            return _PageInfo(error="%s: %s" % (type(exc).__name__, exc))
        info = self._decode(pr.data, block, page, pr.source,
                            getattr(pr, "retry", 0))
        if self._page_ok(info):
            return info
        return self._walk_ladder(ce, block, page, slc, info, stats)

    def _decode(self, raw, block, page, source, retry=0):
        """Raw page bytes -> `_PageInfo`.  No I/O, so a retry level can reuse it."""
        crc = zlib.crc32(raw) & 0xFFFFFFFF
        if is_dead_ce_page(raw):
            return _PageInfo(dead=True, source=source, crc=crc, retry=retry)
        if raw == _const(_ONES, 0xFF, len(raw)):
            return _PageInfo(erased=True, source=source, crc=crc, retry=retry)
        # Cheap path first.  If every AU's MCRC passes uncorrected, no LDPC
        # correction was needed and we have both the bytes and that fact.
        aus, ok = self.codec.decode(raw, block, page, correct=False)
        mcrc0 = None if ok is self._F.UNKNOWN else [bool(x) for x in (ok or [])]
        if aus is not None and mcrc0 and all(mcrc0):
            return _PageInfo(aus=aus, mcrc=mcrc0, mcrc0=mcrc0, corrected=False,
                             source=source, crc=crc, retry=retry)
        aus_c, ok_c = self.codec.decode(raw, block, page, correct=True)
        mcrc = None if ok_c is self._F.UNKNOWN else [bool(x) for x in (ok_c or [])]
        if aus_c is None:
            return _PageInfo(aus=None, mcrc=mcrc, mcrc0=mcrc0, corrected=True,
                             source=source, crc=crc, retry=retry,
                             error="page did not decode even with correction")
        return _PageInfo(aus=aus_c, mcrc=mcrc, mcrc0=mcrc0, corrected=True,
                         source=source, crc=crc, retry=retry)

    # -- the read-retry ladder --------------------------------------------
    @staticmethod
    def _page_ok(info):
        """Is another read reference worth trying?  -> False if it might help.

        PAGE level only.  A bad aux LMA is not a reason to walk the ladder: it
        says the address chain landed somewhere else, and no reference voltage
        fixes a wrong address.  `_one` still applies the aux-LMA bar afterwards
        to whatever this returns, at every level, unchanged.

        `erased` and `dead` count as OK here -- not because those pages are
        good, but because they are VERDICTS rather than decode failures.  An
        all-0xFF page and CE0's constant page are what the media says; re-asking
        at a different reference would turn a clean observation into a fishing
        expedition.
        """
        if info.dead or info.erased:
            return True
        if info.aus is None:
            return False
        if info.mcrc is None:
            return True            # the codec will not say; the aux LMA decides
        return all(info.mcrc)

    def ladder(self, slc=False):
        """-> the levels this run will try after level 0, in order.  () if none.

        Intersected with what the device reports, so a shortlist naming a level
        this die does not have quietly loses that level instead of raising in
        the middle of a run.  A backing with no ladder returns () and is never
        walked -- which is what lets the identical code path run over a dump.
        """
        if not self.read_retry or not self.retry_levels:
            return ()
        if not getattr(self.dev, "supports_retry", False):
            return ()
        if not hasattr(self.dev, "read_page_retry"):
            return ()
        try:
            avail = list(self.dev.retry_levels(slc))
        except Exception:
            return ()
        if self.retry_levels == RETRY_ALL:
            return tuple(lv for lv in avail if lv)
        seen = set(avail)
        return tuple(lv for lv in self.retry_levels if lv and lv in seen)

    def _walk_ladder(self, ce, block, page, slc, base, stats=None):
        """Re-read `page` up the shortlist.  -> a rescued `_PageInfo`, or `base`.

        `base` is the level-0 attempt and is exactly what comes back when
        nothing rescues the page.  That is deliberate: a page that never decoded
        stays undecoded, `_one` records it `unreadable`, and no bytes reach the
        file.  `Device.read_page_retry` already refuses to hand back a least-bad
        read and this layer does not soften that -- a store with a hole in it is
        worth more than one with plausible garbage in it.

        The device restores level 0 in its own `finally`, including on the raise
        below, so a level cannot outlive this call.
        """
        levels = self.ladder(slc)
        if not levels:
            return base
        if stats is not None:
            stats.retry_walks += 1
        seen = [None, None]                     # last (data, _PageInfo) verified

        def verify(data):
            info = self._decode(data, block, page, FULL)
            seen[0], seen[1] = data, info
            ok = info.aus is not None and self._page_ok(info)
            return ok, {"mcrc": sum(1 for x in (info.mcrc or []) if x),
                        "ecc": 0 if info.aus is None else len(info.aus),
                        "lmas": None}

        try:
            pr = self.dev.read_page_retry(ce, block, page, slc=slc,
                                          levels=list(levels), verify=verify)
        except DeviceError:
            base.retry_tried = tuple(levels)
            return base
        except Exception as exc:                # a backing may raise its own
            base.retry_tried = tuple(levels)
            if not base.error:
                base.error = "%s: %s" % (type(exc).__name__, exc)
            return base
        # `read_page_retry` returns the PageRead whose bytes `verify` last
        # accepted, so the decode it already did is the one we want; re-decode
        # only if that identity does not hold (a backing free to copy).
        info = (seen[1] if seen[0] is pr.data
                else self._decode(pr.data, block, page, pr.source))
        info.source = pr.source
        info.retry = getattr(pr, "retry", 0)
        info.retry_tried = tuple(levels)
        if stats is not None and info.retry:
            stats.retry_rescued += 1
        if self.verbose:
            self.log("  ce%d b%d p%d: rescued at read-retry level %d (level 0 "
                     "did not decode)" % (ce, block, page, info.retry))
        return info

    # -- one LMA ----------------------------------------------------------
    def _one(self, mso, lma, stats):
        """-> the manifest record for this LMA.  Writes bytes iff it verifies."""
        F = self._F
        rec = {"lma": lma}
        wma = self.resolver.wma(mso, lma)
        if wma is not None:
            rec["wma"] = wma
            rec["weru"] = wma // geom.AUS_PER_TLC_WERU
        try:
            p = self.resolver.resolve(mso, lma)
        except Exception as exc:
            rec["st"] = UNRESOLVED
            rec["why"] = "%s: %s" % (type(exc).__name__, exc)
            return rec
        if p is None:
            rec["st"] = UNMAPPED
            rec["why"] = "the LUT maps this LMA nowhere"
            return self._gap(rec)
        if p is F.UNKNOWN:
            rec["st"] = UNRESOLVED
            rec["why"] = ("mapped, but this WERU's slot rule or cell mode is "
                          "not established here")
            return self._gap(rec)

        rec.update(ce=p.ce, blk=p.block, pg=p.page, au=p.au)
        try:
            slc = F.slc_flag(p.slc, "LMA %d (weru %s)" % (lma, rec.get("weru")))
        except Exception as exc:
            rec["st"] = UNRESOLVED
            rec["why"] = str(exc).split(".")[0]
            return self._gap(rec)
        rec["slc"] = bool(slc)
        if p.ce in self.dead_ces:
            rec["st"] = DEAD_CE
            rec["why"] = ("CE%d is dead on this board and ANSWERS with a "
                          "constant page; its bytes are not evidence" % p.ce)
            return self._gap(rec)
        if self.survey:
            rec["st"] = SKIPPED
            rec["why"] = "survey: resolved but not read"
            return rec

        info = self._page(p.ce, p.block, p.page, slc, stats)
        if info.crc is not None:
            rec["pcrc"] = info.crc
        if info.source and info.source != FULL:
            rec["src"] = info.source
        # Provenance, not decoration.  Present only when the bytes came back at
        # something other than the die's default references, so its absence
        # means "level 0" on a run whose run-record says a ladder was available
        # and "no ladder at all" on one that says it was not.  A page rescued at
        # level n is a measurement of that block's health and it is the one
        # thing a store cannot reconstruct later.
        if info.retry:
            rec["rty"] = info.retry
        elif info.retry_tried:
            # tried and none of them worked -- say which, so a later reader can
            # tell "the ladder was walked and lost" from "it was never walked"
            rec["rty_tried"] = list(info.retry_tried)
        if info.dead:
            rec["st"] = DEAD_CE
            rec["why"] = ("page is the dead-CE constant (2304 x 0xA7 then "
                          "zeros): neither programmed nor erased")
            return self._gap(rec)
        if info.erased:
            rec["st"] = ERASED
            rec["why"] = "page reads all-0xFF where the LUT expects data"
            return self._gap(rec)
        if info.aus is None:
            rec["st"] = UNREADABLE
            rec["why"] = info.error or "page did not decode"
            return self._gap(rec)
        if p.au >= len(info.aus):
            rec["st"] = UNREADABLE
            rec["why"] = "au %d is past the %d this page decoded to" % (
                p.au, len(info.aus))
            return self._gap(rec)

        au = info.aus[p.au]
        mcrc = None if info.mcrc is None else bool(info.mcrc[p.au])
        mcrc0 = None if info.mcrc0 is None else bool(info.mcrc0[p.au])
        rec["mcrc"] = mcrc
        rec["mcrc0"] = mcrc0
        aux = struct.unpack_from("<I", au, self._aux_off)[0]
        rec["aux"] = aux
        if mcrc is False:
            rec["st"] = FAILED
            rec["why"] = "MCRC does not check, even after LDPC correction"
            return self._gap(rec)
        if aux != lma:
            rec["st"] = FAILED
            rec["why"] = ("aux LMA %d != %d: the address chain landed on a "
                          "different AU, so these bytes are not this LMA's"
                          % (aux, lma))
            return self._gap(rec)

        # VERIFIED.  Correction status is a property of the page (the codec
        # corrects all eight codewords together), so it is recorded per AU as
        # "this AU's bytes came out of a page that needed correction".
        needed = bool(info.corrected and (mcrc0 is not True))
        rec["corr"] = needed
        rec["st"] = CORRECTED if needed else VERIFIED
        body = au[geom.AU_AUX:]
        off = self.sink.write(lma, body)
        if off is not None:
            rec["off"] = off
            rec["len"] = len(body)
            stats.bytes_written += len(body)
        rec["dcrc"] = zlib.crc32(body) & 0xFFFFFFFF
        return rec

    def _gap(self, rec):
        """A non-verified LMA.  Bytes are not written unless --fill says so."""
        if self.sink is not None and self.sink.fill is not None:
            off = self.sink.synthesise(rec["lma"])
            if off is not None:
                rec["off"] = off
                rec["len"] = LMA_BYTES
                rec["syn"] = True
        return rec

    # -- the run ----------------------------------------------------------
    def run(self, mso, lmas, progress_every=0, header_extra=None,
            check=None, check_every=0):
        """Extract `lmas` of `mso`.  -> Stats.  Safe to call once per instance.

        `check(tag)` is called before the first LMA, every `check_every` LMAs
        after that, and once at the end.  It exists for the canary: on hardware
        a report-list fault fakes plausible values for every later entry in a
        batch, so a batch that is not bracketed by a known answer cannot be
        trusted.  Raising from it aborts the run with everything up to that
        point already flushed and recorded, which is the outcome you want --
        the manifest then says exactly how far the evidence goes.
        """
        lmas = list(lmas)
        if not lmas:
            raise ValueError("no LMAs selected")
        if self.manifest is None:
            self.open(mso, lmas[0], header_extra=header_extra)
        st = Stats()
        self.manifest.begin_run({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lma_first": lmas[0], "lma_last": lmas[-1], "n": len(lmas),
            "device": repr(self.dev), "resolver": self.resolver.describe(),
            "retry": self.retry, "survey": self.survey,
            # Both halves, because a record with no `rty` is only readable
            # against them: `read_retry` is what was asked for and
            # `retry_ladder` is what the device could actually offer.  An empty
            # ladder with read_retry true means the backing has none, which is
            # the normal case for a dump and is not a failure.
            "read_retry": self.read_retry,
            "retry_ladder": list(self.ladder(False)),
            "retry_ladder_slc": list(self.ladder(True)),
            "argv": " ".join(sys.argv[1:])})
        try:
            if check is not None:
                check("open")
            for i, lma in enumerate(lmas):
                if self.manifest.done(lma, retry=self.retry):
                    st.skipped_resume += 1
                    continue
                rec = self._one(mso, lma, st)
                st.attempted += 1
                st.note(rec["st"])
                # Bytes before the claim about them: flush the data file, then
                # let the manifest record land.  See Manifest's ordering rule.
                if self.manifest.add(rec):
                    self.sink.flush()
                    self.manifest.flush()
                if progress_every and (i + 1) % progress_every == 0:
                    self._progress(i + 1, len(lmas), st)
                if (check is not None and check_every
                        and st.attempted % check_every == 0):
                    self.sink.flush()
                    self.manifest.flush()
                    check("after %d LMAs (last %d)" % (st.attempted, lma))
            if check is not None:
                check("close")
        finally:
            st.elapsed = time.time() - st.t0
            if self.sink is not None:
                self.sink.flush(fsync=True)
            if self.manifest is not None:
                self.manifest.flush(fsync=True)
        return st

    def _progress(self, i, n, st):
        el = time.time() - st.t0
        rate = i / el if el > 0 else 0
        eta = (n - i) / rate if rate > 0 else 0
        got = st.by_status.get(VERIFIED, 0) + st.by_status.get(CORRECTED, 0)
        self.log("  [%d/%d] %.1f LMA/s  %.1f MiB/h  verified %d  eta %s"
                 % (i, n, rate, st.rate_mib_h(), got, _hms(eta)))


def _hms(sec):
    sec = int(max(0, sec))
    return "%d:%02d:%02d" % (sec // 3600, (sec // 60) % 60, sec % 60)


# ===========================================================================
# the canary
# ===========================================================================
#: A page whose contents are known and stable, re-read between batches.
#: (ce, block, page, slc) and its CRC-32.
CANARY = (1, 26, 44, True)
CANARY_CRC = 0x6568b89a


class CanaryFailed(Exception):
    """The known-good page stopped reading correctly mid-run."""


def canary_check(dev, ref=None, tag="", canary=CANARY):
    """Re-read a page whose answer is known.  -> its CRC.  Raises on drift.

    The report-list fault trap is why this exists.  One bad address in a batch
    poisons the report list, and every later entry then comes back with
    plausible values that were never read off the die.  Nothing about those
    values looks wrong -- that is the entire problem -- so the only defence is
    an external check whose answer is known in advance, taken often enough that
    a poisoned batch cannot be mistaken for a good one.  A run that ends with a
    passing canary is a run whose batches were bracketed by a fact.
    """
    ce, blk, pg, slc = canary
    s = dev.scan_page(ce, blk, pg, slc=slc)
    if s is None:
        raw = dev.read_page(ce, blk, pg, slc=slc)
        crc = zlib.crc32(raw) & 0xFFFFFFFF
        ones = raw == _const(_ONES, 0xFF, len(raw))
    else:
        crc, ones = s.crc, s.all_ones
    if ones:
        raise CanaryFailed("canary ce%d/b%d/p%d reads erased at %s"
                           % (ce, blk, pg, tag or "?"))
    want = CANARY_CRC if ref is None else ref
    if crc != want:
        raise CanaryFailed(
            "canary ce%d/b%d/p%d CRC %#010x != %#010x at %s -- a report-list "
            "fault poisons every later entry in a batch with plausible values, "
            "so everything read since the last good canary is suspect"
            % (ce, blk, pg, crc, want, tag or "?"))
    return crc


# ===========================================================================
# selection
# ===========================================================================
def parse_lma_range(text):
    """'a:B' (half-open) | 'a-b' (inclusive) | 'a+n' (count) | 'a' -> (lo, hi).

    Half-open at the top, always, because an LMA count and a byte count should
    multiply cleanly: `A:A+16` is 16 LMAs is 64 KiB, which is exactly one user
    LUT granule.
    """
    s = str(text).strip()
    if ":" in s:
        a, b = s.split(":", 1)
        lo, hi = int(a, 0), int(b, 0)
    elif "+" in s[1:]:
        a, b = s.split("+", 1)
        lo = int(a, 0)
        hi = lo + int(b, 0)
    elif "-" in s[1:]:
        a, b = s.split("-", 1)
        lo, hi = int(a, 0), int(b, 0) + 1
    else:
        lo = int(s, 0)
        hi = lo + 1
    if hi <= lo:
        raise ValueError("empty LMA range %r (%d..%d)" % (text, lo, hi))
    return lo, hi


def lma_sequence(resolver, mso, lo=None, hi=None, all_mapped=False, weru=None,
                 count=None, sample=None):
    """-> a list of LMAs to attempt, in order.

    `weru` filters by the WMA's WERU, which costs one LUT lookup per LMA and no
    reads at all -- cheap enough to scan a large span for the handful of LMAs
    that land in one erase unit.  It is a filter, never a source of addresses:
    there is no arithmetic here that turns a WERU into a list of LMAs, because
    that direction is the LUT's job and inventing it is how a wrong mapping gets
    confidently reported.
    """
    if all_mapped:
        cov = resolver.coverage(mso)
        if cov is None:
            raise ValueError(
                "--all-mapped needs this MSO's LUT coverage and the resolver "
                "cannot give it; use --lma A:B instead")
        lo, hi = 0, cov
    if lo is None or hi is None:
        raise ValueError("say which LMAs: --lma A:B, or --all-mapped")
    span = range(lo, hi)
    if sample:
        step = max(1, len(span) // int(sample))
        span = range(lo, hi, step)
    out = []
    for lma in span:
        if weru is not None:
            w = resolver.wma(mso, lma)
            if w is None or w // geom.AUS_PER_TLC_WERU != weru:
                continue
        out.append(lma)
        if count and len(out) >= count:
            break
    return out


# ===========================================================================
# reporting
# ===========================================================================
def report(manifest, stats, sink=None, selected=None, span=None):
    """The end-of-run summary.  Says what was got and what a gap means here."""
    L = []
    tally = manifest.tally()
    total = sum(tally.values())
    got = tally.get(VERIFIED, 0) + tally.get(CORRECTED, 0)
    L.append("")
    L.append("manifest %s -- %d LMA(s) accounted for" % (manifest.path, total))
    for st in STATUSES:
        if tally.get(st):
            L.append("  %-11s %8d   %s" % (st, tally[st], _WHY.get(st, "")))
    for st in sorted(set(tally) - set(STATUSES)):
        L.append("  %-11s %8d" % (st, tally[st]))
    L.append("")
    L.append("this run: %d attempted, %d already accounted for and skipped, "
             "%d page reads (%d served from the page cache), %d page(s) needed "
             "LDPC correction"
             % (stats.attempted, stats.skipped_resume, stats.pages_read,
                stats.pages_cached, stats.corrected_pages))
    L.append("          %.1f s, %s written, %.1f MiB/h"
             % (stats.elapsed, _bytes(stats.bytes_written), stats.rate_mib_h()))
    L.extend(_retry_lines(manifest, stats))
    if selected:
        L.append("verified fraction of the LMAs SELECTED: %d of %d (%.2f%%)"
                 % (got, selected, 100.0 * got / max(1, selected)))
        # An LMA the LUT maps nowhere is a real negative -- the media says
        # nothing lives there -- so counting it against the verified fraction
        # reports the tool as failing when it is working.  On a sparse MSO
        # sampled across its whole span that is the overwhelming majority
        # (1996 of 2001 in one measured run), and the headline read "1 of 201"
        # while every mapped LMA had in fact verified.  Quote both: the second
        # number is the one that says whether the toolkit works.
        unmapped = stats.by_status.get(UNMAPPED, 0)
        if unmapped:
            mapped = selected - unmapped
            L.append("verified fraction of the LMAs the LUT actually MAPS: "
                     "%d of %d (%.2f%%)   [%d unmapped = real negatives]"
                     % (got, mapped, 100.0 * got / max(1, mapped), unmapped))
    if span and selected and (span[1] - span[0]) != selected:
        L.append("  (the selection was %d LMA(s) drawn from a span of %d, "
                 "%d..%d -- the percentage above is of what was asked for, "
                 "not of the span)"
                 % (selected, span[1] - span[0], span[0], span[1] - 1))
    if sink is not None and sink.enabled:
        L.append("")
        L.append("output %s -- %s" % (sink.path, _bytes(sink.size())))
        if sink.layout == "sparse":
            miss = total - got
            L.append("  layout=sparse: offset = (lma - %d) * %d.  %d LMA(s) in "
                     "this manifest were NOT written."
                     % (sink.base_lma, LMA_BYTES, miss))
            if not miss:
                L.append("  Every LMA in the manifest verified, so this file "
                         "has no holes in it: it is a contiguous image of the "
                         "range and every byte came off the media.")
            elif sink.fill is None:
                L.append("  THOSE RANGES ARE HOLES.  A hole reads back as "
                         "zeros, and those zeros are the filesystem's, not the "
                         "drive's -- nothing was read there and nothing is "
                         "claimed about it.  Only the manifest tells the two "
                         "apart.  Do not treat a zero run in this file as "
                         "erased media.")
            else:
                L.append("  --fill 0x%02X synthesised %d LMA(s); every one is "
                         "marked \"syn\": true in the manifest.  Those bytes "
                         "were MADE UP by this tool."
                         % (sink.fill, sink.n_synthesised))
        else:
            L.append("  layout=packed: only verified LMAs are in the file, in "
                     "order, and the manifest carries each one's offset.  "
                     "Nothing here was invented.")
    return "\n".join(L)


def _retry_lines(manifest, stats):
    """The read-retry paragraph, or nothing at all when no ladder was in play.

    Silence here is deliberate.  Over a dump there is no ladder, every page came
    off at level 0 by definition, and a line saying "0 pages needed retry" would
    read as a health measurement of media that was never asked.
    """
    L = []
    walks = getattr(stats, "retry_walks", 0)
    rescued = getattr(stats, "retry_rescued", 0)
    tally = manifest.retry_tally() if hasattr(manifest, "retry_tally") else {}
    if not walks and not tally:
        return L
    if walks:
        L.append("read-retry: %d page(s) failed at the die's default references, "
                 "%d rescued by the ladder, %d withheld"
                 % (walks, rescued, walks - rescued))
    if tally:
        L.append("  LMAs whose bytes came back at a non-default level: %s"
                 % ", ".join("level %d x%d" % (k, tally[k])
                             for k in sorted(tally)))
        L.append("  Those are recorded as \"rty\" in the manifest.  The bytes "
                 "are NOT less trustworthy for it -- every one still passed the "
                 "same aux-LMA check -- but the pages under them are drifting, "
                 "and that is what the field is there to say.")
    if walks and walks > rescued:
        L.append("  The withheld ones decoded at NO level on the shortlist and "
                 "are recorded `unreadable` with the levels tried.  Nothing was "
                 "written for them: a least-bad read is not a read.")
    return L


_WHY = {
    VERIFIED: "MCRC good, aux LMA matches, no correction needed",
    CORRECTED: "same, but LDPC correction was needed",
    FAILED: "read and decoded, but MCRC or the aux LMA disagreed -- NOT written",
    ERASED: "reads all-0xFF where the LUT expects data",
    UNMAPPED: "the LUT maps it nowhere -- a real negative",
    UNRESOLVED: "mapped, but not resolvable from what we hold",
    UNREADABLE: "this backing does not hold the page",
    DEAD_CE: "lands on a dead CE that answers with a constant page",
    SKIPPED: "survey only: resolved, not read",
}


def _bytes(n):
    for unit, div in (("MiB", 1048576), ("KiB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / float(div), unit)
    return "%d B" % n


# ===========================================================================
# selftest
# ===========================================================================
def _fake_stack(tmp):
    """A synthetic device + resolver whose right answer is known by construction.

    Builds real Titania pages with `titania.codec.encode_page`, so the decode
    path under test is the real one; only the addresses are made up.
    """
    C = _sib("codec")
    F = _sib("ftl")

    class MemDevice(object):
        writable = False
        fast_erased = True

        def __init__(self):
            self.pages = {}
            self.writes_enabled = False

        def present_ces(self):
            return sorted({k[0] for k in self.pages})

        def read_page(self, ce, block, page, lun=0, slc=False):
            return self.read_page_ex(ce, block, page, lun, slc).data

        def read_page_ex(self, ce, block, page, lun=0, slc=False):
            from titania.device import PageRead, FULL as _F
            key = (ce, block, page, bool(slc))
            if key not in self.pages:
                raise DeviceError("no page at ce%d b%d p%d %s"
                                  % (ce, block, page, "slc" if slc else "tlc"))
            return PageRead(self.pages[key], _F)

        def __repr__(self):
            return "<MemDevice %d pages>" % len(self.pages)

    dev = MemDevice()
    # Four AUs of one page, aux LMA = 1000..1003, plus a second page 1004..1007
    table, wmas = {}, {}
    for pi in range(2):
        block, page, slc = 100 + pi, 7, True
        row = geom.compose_row(block, page)
        plains = []
        for au in range(4):
            lma = 1000 + pi * 4 + au
            aux = struct.pack("<I", lma) + b"\x00\x00\x00\x00"
            body = bytes(((lma * 7 + i * 13) & 0xFF) for i in range(LMA_BYTES))
            plains.append(C.AuPlain(body, aux))
            table[(9, lma)] = F.Placement(3, block, page, au, slc=slc)
            wmas[(9, lma)] = 5 * geom.AUS_PER_TLC_WERU + page * 192 + au
        dev.pages[(3, block, page, slc)] = C.encode_page(plains, row=row)
    # a real negative, an unresolvable one, an unreadable one, a dead CE
    table[(9, 1008)] = None
    table[(9, 1009)] = F.UNKNOWN
    table[(9, 1010)] = F.Placement(3, 999, 7, 0, slc=True)      # not in MemDevice
    table[(9, 1011)] = F.Placement(0, 100, 7, 0, slc=True)      # dead CE
    table[(9, 1012)] = F.Placement(3, 100, 7, 1, slc=F.UNKNOWN)  # mode unknown
    return dev, StaticResolver(table, wmas), 1000


def _expected_body(lma):
    return bytes(((lma * 7 + i * 13) & 0xFF) for i in range(LMA_BYTES))


def _corrupt(raw, nflips, seed):
    """Flip `nflips` random bits.  4096 of them defeats the LDPC on this format.

    Measured, not guessed (`px_corrupt_probe`): at 1024 flips the code still
    corrects the page cleanly, at 4096 every MCRC fails after correction.  So
    4096 is a page the die cannot hand back at these references and 1024 is one
    it can -- which is the distinction the ladder exists to cross.
    """
    import random
    rng = random.Random(seed)
    b = bytearray(raw)
    for _ in range(nflips):
        i = rng.randrange(len(b))
        b[i] ^= 1 << rng.randrange(8)
    return bytes(b)


def _ladder_stack():
    """A device with a real retry ladder, over synthetic pages.  -> (dev, res, ...)

    A subclass of the real `HardwareDevice` with only `_read_span`
    (the wire) and `_write_retry` (the SET FEATURES) replaced.  Everything the
    ladder actually consists of -- `read_page_retry`, `_ensure_retry`, the
    level-keyed page cache, the restore in `finally` -- is the shipping code,
    so this tests the mechanism rather than a re-implementation of it.

    Three native-TLC pages, all on `(page - 8) % 3 == 2` upper word lines:

        block 100  clean at every level                 -- the control
        block 101  undecodable at 0, clean from 1 up    -- the rescue
        block 102  undecodable at every level           -- must be withheld
    """
    C = _sib("codec")
    F = _sib("ftl")
    hw = _sib("hwdev")

    PAGE = 13                       # (13 - 8) % 3 == 2 -> the "upper" class
    CE, SLC = 6, False

    class LadderDevice(hw.HardwareDevice):
        """HardwareDevice with the UART replaced by a dict.  Touches no board."""

        def __init__(self):
            hw.HardwareDevice.__init__(self, cache_pages=8, fast_erased=False,
                                       device_scan=False)
            self.pages = {}         # (ce, block, page, slc, level) -> bytes
            self.reads = []         # (block, page, level) in order
            self.feature_writes = []

        def present_ces(self):
            return sorted({k[0] for k in self.pages})

        def _connect(self):         # nothing here may reach a board
            raise AssertionError("LadderDevice must never open the link")

        def _write_retry(self, ce, level, slc):
            self.feature_writes.append((ce, int(level), bool(slc)))
            self._retry[(ce, bool(slc))] = int(level)
            self._retry_touched = True
            self.stats["retry_switches"] += 1

        def _read_span(self, ce, block, page, lun, slc, col, nbytes):
            lvl = self.current_retry_level(ce, slc)
            self.reads.append((block, page, lvl))
            key = (ce, block, page, bool(slc), lvl)
            if key not in self.pages:
                raise DeviceError("no page at ce%d b%d p%d level %d"
                                  % (ce, block, page, lvl))
            return self.pages[key][col:col + nbytes]

    dev = LadderDevice()
    table, wmas = {}, {}
    base = 2000
    for i, block in enumerate((100, 101, 102)):
        row = geom.compose_row(block, PAGE)
        plains = []
        for au in range(4):
            lma = base + i * 4 + au
            aux = struct.pack("<I", lma) + b"\x00\x00\x00\x00"
            plains.append(C.AuPlain(_expected_body(lma), aux))
            table[(9, lma)] = F.Placement(CE, block, PAGE, au, slc=SLC)
            wmas[(9, lma)] = 7 * geom.AUS_PER_TLC_WERU + PAGE * 192 + au
        clean = C.encode_page(plains, row=row)
        for lvl in range(33):
            if block == 100:
                data = clean
            elif block == 101:
                data = clean if lvl >= 1 else _corrupt(clean, 4096, 11)
            else:
                data = _corrupt(clean, 4096, 20 + lvl)
            dev.pages[(CE, block, PAGE, SLC, lvl)] = data
    return dev, StaticResolver(table, wmas), base, PAGE


def selftest(argv=None):                                # pragma: no cover
    """Exercise extract end to end, and against the three oracle files.

    SKIPPED, loudly, for anything the environment cannot support -- a missing
    dump or an FTL that cannot yet resolve user WERUs is a fact about the
    environment, not a pass and not a failure of this module.
    """
    import shutil
    import subprocess
    import tempfile

    ok = True
    skipped = []
    print("titania.extract -- selftest\n")
    root = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp(prefix="tex_"))
    try:
        # -- 1. range parsing ------------------------------------------
        cases = [("10:20", (10, 20)), ("10-19", (10, 20)), ("10+10", (10, 20)),
                 ("0x10", (16, 17))]
        good = all(parse_lma_range(a) == b for a, b in cases)
        print("[1] parse_lma_range: %s" % ("ok" if good else "WRONG"))
        ok &= good
        try:
            parse_lma_range("20:10")
            print("    empty range accepted -- WRONG")
            ok = False
        except ValueError:
            pass

        # -- 2. synthetic end to end -----------------------------------
        dev, res, base = _fake_stack(tmp)
        out = tmp / "syn.bin"
        ex = Extractor(dev, res, out=out, page_cache=8)
        ex.run(9, range(base, base + 13))
        ex.close()
        tally = ex.manifest.tally()
        want = {VERIFIED: 8, UNMAPPED: 1, UNRESOLVED: 2, UNREADABLE: 1,
                DEAD_CE: 1}
        good = tally == want
        print("[2] synthetic run: %s" % tally)
        if not good:
            print("    expected %s -- WRONG" % want)
        ok &= good
        blob = out.read_bytes()
        good = len(blob) == 8 * LMA_BYTES and all(
            blob[i * LMA_BYTES:(i + 1) * LMA_BYTES] == _expected_body(base + i)
            for i in range(8))
        print("    payload bytes: %s (%d B, expected %d)"
              % ("ok" if good else "WRONG", len(blob), 8 * LMA_BYTES))
        ok &= good

        # -- 3. no invented bytes --------------------------------------
        # LMAs 1008..1012 are gaps; the file must simply stop after 1007.
        good = len(blob) == 8 * LMA_BYTES
        print("[3] gaps are gaps: file ends at the last verified LMA -- %s"
              % ("ok" if good else "WRONG"))
        ok &= good
        # ... and with --fill they are synthesised and labelled
        out2 = tmp / "syn_fill.bin"
        ex2 = Extractor(dev, res, out=out2, fill=0x00, page_cache=8)
        ex2.run(9, range(base, base + 13))
        ex2.close()
        syn = [r for r in ex2.manifest.records.values() if r.get("syn")]
        good = (len(syn) == 5 and out2.stat().st_size == 13 * LMA_BYTES)
        print("    --fill 0x00: %d LMA(s) marked \"syn\", file %d B -- %s"
              % (len(syn), out2.stat().st_size, "ok" if good else "WRONG"))
        ok &= good

        # -- 4. resume -------------------------------------------------
        out3 = tmp / "resume.bin"
        ex3 = Extractor(dev, res, out=out3, page_cache=8, batch=1)
        ex3.run(9, range(base, base + 5))
        ex3.close()
        part = out3.read_bytes()
        ex4 = Extractor(dev, res, out=out3, page_cache=8, batch=1)
        st4 = ex4.run(9, range(base, base + 13))
        ex4.close()
        good = (ex4.manifest.resumed and st4.skipped_resume == 5 and
                st4.attempted == 8 and out3.read_bytes() == blob)
        print("[4] resume: manifest resumed=%s, %d skipped, %d attempted, "
              "output identical to the uninterrupted run: %s"
              % (ex4.manifest.resumed, st4.skipped_resume, st4.attempted,
                 "ok" if good else "WRONG"))
        ok &= good
        good = len(part) == 5 * LMA_BYTES
        print("    partial run had stopped at %d B" % len(part))
        ok &= good
        # every LMA of the range is accounted for, none silently skipped
        missing = [l for l in range(base, base + 13)
                   if l not in ex4.manifest.records]
        print("    LMAs with no record: %s" % (missing or "none"))
        ok &= not missing

        # -- 5. a mismatched manifest is refused -----------------------
        try:
            Extractor(dev, res, out=out3, layout="packed").open(9, base)
            print("[5] mismatched manifest accepted -- WRONG")
            ok = False
        except ResumeMismatch as exc:
            print("[5] mismatched manifest refused: %s"
                  % str(exc).splitlines()[0])

        # -- 6. packed layout ------------------------------------------
        out5 = tmp / "packed.bin"
        ex5 = Extractor(dev, res, out=out5, layout="packed", page_cache=8)
        ex5.run(9, range(base, base + 13))
        ex5.close()
        good = out5.stat().st_size == 8 * LMA_BYTES
        print("[6] packed layout: %d B for 8 verified LMAs -- %s"
              % (out5.stat().st_size, "ok" if good else "WRONG"))
        ok &= good

        # -- 7. a torn manifest is recovered ---------------------------
        torn = tmp / "torn.bin"
        exa = Extractor(dev, res, out=torn, page_cache=8, batch=1)
        exa.run(9, range(base, base + 6))
        exa.close()
        mp = Path(str(torn) + ".manifest.jsonl")
        with open(mp, "ab") as f:
            f.write(b'{"lma":1006,"st":"veri')       # a killed process
        exb = Extractor(dev, res, out=torn, page_cache=8, batch=1)
        exb.run(9, range(base, base + 13))
        exb.close()
        good = torn.read_bytes() == blob and 1006 in exb.manifest.records
        print("[7] torn final line: dropped and redone -- %s"
              % ("ok" if good else "WRONG"))
        ok &= good

        # -- 8. the dead-CE detector, against real CE0 pages ------------
        dumproot = config.dump_path()
        if not dumproot.exists():
            skipped.append("[8] dead-CE detector: no <dump>/")
        else:
            dd = _sib("dumpdev").DumpDevice(dumproot)
            ce0 = sorted(k for k in dd.entries if k[0] == 0)
            other = [k for k in dd.entries if k[0] != 0][:400]
            hit = sum(1 for k in ce0
                      if is_dead_ce_page(dd.read_page(k[0], k[1], k[2],
                                                      slc=k[3])))
            false_pos = sum(1 for k in other
                            if is_dead_ce_page(dd.read_page(k[0], k[1], k[2],
                                                            slc=k[3])))
            good = (ce0 and hit == len(ce0) and false_pos == 0)
            print("[8] dead-CE detector: %d/%d CE0 pages flagged, %d false "
                  "positives in %d non-CE0 pages -- %s"
                  % (hit, len(ce0), false_pos, len(other),
                     "ok" if good else "WRONG"))
            ok &= bool(good)

        # -- 8b. the read-retry ladder ---------------------------------
        try:
            ldev, lres, lbase, lpage = _ladder_stack()
        except Exception as exc:
            skipped.append("[8b] read-retry ladder: %s: %s"
                           % (type(exc).__name__, exc))
        else:
            raidmod, _e = _load_opt("raid")
            wl = (raidmod.wordline_class(lpage)[1] if raidmod else "?")
            print("[8b] read-retry ladder over synthetic pages (TLC page %d, "
                  "word-line class '%s')" % (lpage, wl))
            span = range(lbase, lbase + 12)

            # (a) retry off -- the baseline this whole feature is measured
            #     against.  Blocks 101 and 102 must both come back as gaps.
            off = tmp / "rt_off.bin"
            exo = Extractor(ldev, lres, out=off, page_cache=8,
                            read_retry=False)
            sto = exo.run(9, span)
            exo.close()
            to = exo.manifest.tally()
            good = (to.get(VERIFIED, 0) == 4 and sto.retry_walks == 0)
            print("     --no-read-retry: %s  (only block 100 verifies) -- %s"
                  % (to, "ok" if good else "WRONG"))
            ok &= good

            # (b) retry on, the shipped shortlist.  Block 101 is rescued at
            #     level 1 and says so; block 102 is still withheld.
            on = tmp / "rt_on.bin"
            exn = Extractor(ldev, lres, out=on, page_cache=8)
            stn = exn.run(9, span)
            exn.close()
            tn = exn.manifest.tally()
            recs = exn.manifest.records
            rescued = [l for l in span if recs[l].get("rty")]
            levels = {recs[l]["rty"] for l in rescued}
            # The synthetic die rescues at whichever level is tried first, so
            # pin the shortlist's head rather than a literal.  This used to
            # read `levels == {1}` and broke when the measured order changed to
            # (6, 1, 4); the check is about "the walk stopped at the first
            # working rung", not about which rung that happens to be.  The
            # tuple itself is pinned by `titania.cli --selftest`.
            good = (tn.get(VERIFIED, 0) == 8 and len(rescued) == 4
                    and levels == {RETRY_SHORTLIST[0]})
            print("     shortlist %s: %s;  %d LMA(s) carry \"rty\", levels %s "
                  "-- %s" % (list(RETRY_SHORTLIST), tn, len(rescued),
                             sorted(levels), "ok" if good else "WRONG"))
            ok &= good
            good = (stn.retry_walks == 2 and stn.retry_rescued == 1)
            print("     stats: %d page walk(s), %d rescued, by level %s -- %s"
                  % (stn.retry_walks, stn.retry_rescued,
                     dict(sorted(stn.by_retry_level.items())),
                     "ok" if good else "WRONG"))
            ok &= good

            # (c) the BAR did not move.  Every rescued LMA's bytes are the
            #     bytes that LMA is supposed to hold, and the undecodable page
            #     put nothing at all in the file.
            blob = on.read_bytes()
            good = all(blob[(l - lbase) * LMA_BYTES:(l - lbase + 1) * LMA_BYTES]
                       == _expected_body(l) for l in rescued)
            print("     rescued payloads are byte-correct: %s"
                  % ("ok" if good else "WRONG"))
            ok &= good
            withheld = [l for l in span if recs[l]["st"] in
                        (FAILED, UNREADABLE)]
            good = (len(withheld) == 4
                    and all(recs[l].get("off") is None for l in withheld)
                    and all(recs[l].get("rty_tried") == list(RETRY_SHORTLIST)
                            for l in withheld))
            print("     block 102 withheld: %d LMA(s), no offsets, each "
                  "recording rty_tried -- %s" % (len(withheld),
                                                 "ok" if good else "WRONG"))
            ok &= good

            # (d) the level did not outlive the walk, and the run record can
            #     be read back for what the ladder even was
            left = {k: v for k, v in ldev._retry.items() if v}
            run = exn.manifest.runs[-1]
            good = (not left and run.get("read_retry") is True
                    and run.get("retry_ladder") == list(RETRY_SHORTLIST))
            print("     levels left set after the run: %s;  run record says "
                  "read_retry=%s ladder=%s -- %s"
                  % (left or "none", run.get("read_retry"),
                     run.get("retry_ladder"), "ok" if good else "WRONG"))
            ok &= good

            # (e) a shortlist naming levels this die does not have loses them
            #     rather than raising mid-run
            exq = Extractor(ldev, lres, manifest=tmp / "rt_q.jsonl",
                            survey=True, retry_levels=(1, 99, 6))
            good = exq.ladder(False) == (1, 6)
            print("     shortlist (1, 99, 6) against a 32-step ladder -> %s "
                  "-- %s" % (list(exq.ladder(False)),
                             "ok" if good else "WRONG"))
            ok &= good
            # Round-trip the shortlist through its own parser instead of
            # repeating it as a literal -- the literal was "1,6,15" and went
            # stale the moment the measured order became (6, 1, 4).
            good = (parse_retry_levels(",".join(str(v) for v in RETRY_SHORTLIST))
                    == RETRY_SHORTLIST
                    and parse_retry_levels("all") is RETRY_ALL
                    and parse_retry_levels("none") == ())
            print("     parse_retry_levels: %s" % ("ok" if good else "WRONG"))
            ok &= good

            # (f) a DumpDevice has no ladder, so the same code never walks one
            if dumproot.exists():
                dd = _sib("dumpdev").DumpDevice(dumproot)
                exd = Extractor(dd, lres, manifest=tmp / "rt_d.jsonl",
                                survey=True)
                good = (dd.supports_retry is False and exd.ladder(False) == ())
                print("     DumpDevice: supports_retry=%s, ladder %s -- %s"
                      % (dd.supports_retry, list(exd.ladder(False)),
                         "ok" if good else "WRONG"))
                ok &= good

        # -- 9. The oracle: byte-compare against the three known extracts --
        oracles = [("no_user_lma1149856_64KiB_tlc.bin", 1149856, False),
                   ("no_user_lma1420576_64KiB_slc.bin", 1420576, True),
                   ("no_user_lma1419216_64KiB_slc.bin", 1419216, True)]
        have = [o for o in oracles if (root / "no_dump" / o[0]).exists()]
        if not have or not dumproot.exists():
            skipped.append("[9] oracle byte-comparison: no_dump/ or <dump>/ "
                           "is not here")
        else:
            r = subprocess.run(
                [sys.executable, "-m", "%s.extract" % _PKG, "--oracle",
                 str(tmp)], cwd=str(root))
            if r.returncode == 77:
                skipped.append("[9] oracle byte-comparison: the FTL cannot "
                               "resolve user-WERU placements in this tree yet")
            else:
                print("[9] oracle byte-comparison returned %d" % r.returncode)
                ok &= (r.returncode == 0)
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    for s in skipped:
        print("%s -- SKIPPED" % s)
    print("\nSELFTEST:", "PASS" if ok else "FAIL",
          "(%d skipped)" % len(skipped) if skipped else "")
    return 0 if ok else 1


def _oracle(tmpdir):                                    # pragma: no cover
    """Extract the three known 64 KiB granules and byte-compare.  -> rc.

    Exit 77 means SKIPPED: the FTL in this tree cannot yet resolve user-WERU
    placements, so there is nothing to compare and pretending otherwise would
    be the worst of the available answers.
    """
    root = Path(__file__).resolve().parent.parent
    F = _sib("ftl")
    DumpDevice = _sib("dumpdev").DumpDevice
    tmp = Path(tmpdir)
    dev = DumpDevice(config.dump_path(required=True))
    codec = F.default_codec()
    ftl = F.Ftl.from_device(dev, codec=codec, cache_dir=root / ".ftlcache")
    res = FtlResolver(ftl)
    cases = [("no_user_lma1149856_64KiB_tlc.bin", 1149856),
             ("no_user_lma1420576_64KiB_slc.bin", 1420576),
             ("no_user_lma1419216_64KiB_slc.bin", 1419216)]
    probe = [res.resolve(32, lma) for _n, lma in cases]
    if not any(isinstance(p, F.Placement) for p in probe):
        print("  the FTL resolves none of the three oracle LMAs to an address "
              "(%r) -- nothing to compare" % (probe,))
        return 77
    rc = 0
    for name, lma in cases:
        want = (root / "no_dump" / name).read_bytes()
        out = tmp / ("oracle_%d.bin" % lma)
        ex = Extractor(dev, res, codec=codec, out=out, page_cache=16)
        st = ex.run(32, range(lma, lma + 16))
        ex.close()
        got = out.read_bytes() if out.exists() else b""
        same = got == want
        print("  %-40s %d/16 verified  %d B  %s"
              % (name, st.by_status.get(VERIFIED, 0)
                 + st.by_status.get(CORRECTED, 0), len(got),
                 "IDENTICAL" if same else "DIFFERS"))
        if not same:
            rc = 1
            n = min(len(got), len(want))
            first = next((i for i in range(n) if got[i] != want[i]), n)
            print("     first difference at byte %d; lengths %d vs %d"
                  % (first, len(got), len(want)))
    return rc


def main(argv=None):                                    # pragma: no cover
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--oracle":
        return _oracle(argv[1] if len(argv) > 1 else ".")
    if "--selftest" in argv:
        return selftest(argv)
    cli_main = _sib("cli").main
    return cli_main(["extract"] + argv)


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
