"""Sidecars the viewer rebuilds for itself.

Everything here is DERIVED from the capture and merely cached beside it.  The
msoDescriptor is a section of the drive's own Bootstrap RootStorage; the cell
map is the `slc` flag already carried on every index record.  Delete either file
and the viewer rebuilds it -- what is lost is time, not a capability, and that
is the whole difference between a cache and a dependency.

Why the files exist at all is a measurement.  Reaching the descriptor means
mounting the Bootstrap FTL, and `Ftl.from_device` measured **47.8 s** on the
reference capture (the section lookup after it is free, 0.00 s).  That is not a
cost to pay at every launch and is far too long to spend on the UI thread, so
the rule is: read the file if it is there, otherwise derive it ONCE on a worker
and write it back, leaving the next launch instant.  The cell map needs no FTL
at all -- it is a numpy reduction over the index -- but it follows the same
shape so there is one story for both.

A data directory that is read-only or full degrades to deriving once per
session and SAYS so; it never fails and never silently drops the feature.

Nothing here raises.  A sidecar that cannot be produced comes back None with a
sentence explaining which step failed, because "the FTL would not mount" and
"this drive genuinely has no such section" are different facts and the panel
that reports UNKNOWN should not merge them.
"""
from __future__ import annotations

import os

__all__ = ["MSO_SECTION", "derive_descriptor", "descriptor",
           "derive_cellmap", "cellmap", "build_summary", "summary",
           "rebuild"]

#: The RootStorage section the msoDescriptor lives in.  `titania.auxdata`'s own
#: `_descriptor_bytes` reaches for the same name; naming it once here keeps the
#: derivation and its consumer from drifting apart.
MSO_SECTION = "mso"

#: Index record layout, from `titania.store`: byte 0 is the CE, bytes 1..2 the
#: block big-endian, byte 5 the flags whose bit 0 is "this block is pseudo-SLC".
_REC = 24
_HDR = 64


#: How the summary build's own phase markers are shown to a reader, with the
#: durations measured on the reference capture (43.9M records, 12 CEs).  The
#: mount is ~95% of the wall clock and is where a progress line has to be
#: honest about the wait.
_PHASES = (
    ("streaming", "reading the store indexes (a few seconds)"),
    ("mounting FTL 1/2", "mounting the Backup FTL for block properties "
                         "(about 20 s)"),
    ("mounting FTL 2/2", "mounting the Bootstrap FTL for the block map and "
                         "msoDescriptor (about 50 s, the long one)"),
    ("sampling", "checking page 0 of every block for all-zero (about a second)"),
)


def _ensure_titania() -> None:
    from . import ensure_titania_on_path
    ensure_titania_on_path()


def _atomic_write(path: str, data: bytes) -> str | None:
    """Write `data` to `path` without ever leaving a half-file.  -> note or None.

    A truncated sidecar is worse than an absent one: absent is rebuilt, half a
    descriptor is READ, and its bytes are per-drive configuration that nothing
    downstream can sanity-check.  So write a sibling and rename.
    """
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as h:
            h.write(data)
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
        return None
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "%s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------- descriptor --
def derive_descriptor(raw_path: str) -> tuple[bytes | None, str | None]:
    """Mount the capture's Bootstrap FTL and take the msoDescriptor section.

    -> (bytes, None) or (None, why).  Roughly 48 s.  WORKER THREAD ONLY.
    """
    try:
        _ensure_titania()
        from titania.dumpdev import DumpDevice
        from titania.ftl import Ftl
    except Exception as exc:                                 # noqa: BLE001
        return None, "titania is not importable: %s: %s" % (
            type(exc).__name__, exc)

    dev = None
    try:
        dev = DumpDevice(raw_path)
        boot = Ftl.from_device(dev, progress=lambda *a: None)
        sec = boot.section(MSO_SECTION)
        buf = sec.contiguous() if hasattr(sec, "contiguous") else sec
        buf = bytes(buf) if buf else b""
        if not buf:
            return None, ("the FTL mounted but carries no %r section -- a real "
                          "absence, not a failure to look" % MSO_SECTION)
        return buf, None
    except Exception as exc:                                 # noqa: BLE001
        return None, "FTL mount failed: %s: %s" % (type(exc).__name__, exc)
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:                                # noqa: BLE001
                pass


def descriptor(raw_path: str, cache_path: str) -> tuple[bytes | None, str]:
    """The msoDescriptor: cached file if present, else derived and cached.

    -> (bytes or None, one sentence naming where it came from).  The sentence is
    for the status bar and always distinguishes the three outcomes, because
    "loaded" and "rebuilt, and here is why that took a minute" are different
    things to a reader watching the aux panel fill in.
    """
    try:
        if cache_path and os.path.isfile(cache_path):
            with open(cache_path, "rb") as h:
                buf = h.read()
            if buf:
                return buf, "msoDescriptor: %d bytes from %s" % (
                    len(buf), cache_path)
    except OSError as exc:
        pass  # fall through and derive; an unreadable cache is not an answer

    buf, why = derive_descriptor(raw_path)
    if buf is None:
        return None, "msoDescriptor unavailable -- %s" % why

    note = _atomic_write(cache_path, buf) if cache_path else "no cache path"
    if note:
        return buf, ("msoDescriptor: %d bytes derived from the capture; NOT "
                     "cached (%s), so the next launch derives it again"
                     % (len(buf), note))
    return buf, ("msoDescriptor: %d bytes derived from the capture and cached "
                 "at %s" % (len(buf), cache_path))


# ------------------------------------------------------------------ cellmap --
def derive_cellmap(raw_path: str) -> tuple[dict | None, str | None]:
    """{ce: {block: is_slc}} straight off the index.  -> (map, None) or (None, why).

    No FTL and no page reads: the cell mode is bit 0 of every record's flags
    byte.  The reduction is a sticky OR per block, which is exactly what
    `titania.summary` does ("mode is a per-block property; take it from any
    record of that block") -- deriving it any other way would let this map and
    the summary's own `slc` column disagree about the same blocks, and a
    factor-of-three page count is what rides on the answer.
    """
    try:
        import numpy as np
    except Exception as exc:                                 # noqa: BLE001
        return None, "numpy is not importable: %s" % exc

    idx = os.path.join(raw_path, "index.tix")
    try:
        raw = np.fromfile(idx, np.uint8)
    except OSError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    if raw.size < _HDR or (raw.size - _HDR) % _REC:
        return None, "%s is not a whole number of %d-byte records" % (idx, _REC)

    r = raw[_HDR:].reshape(-1, _REC)
    ce = r[:, 0].astype(np.int64)
    block = (r[:, 1].astype(np.int64) << 8) | r[:, 2].astype(np.int64)
    slc = (r[:, 5] & 1).astype(np.float64)

    key = (ce << 16) | block
    uniq, inv = np.unique(key, return_inverse=True)
    # bincount over the 0/1 flag: a positive sum is "some record of this block
    # said SLC", which is the sticky OR without a slow ufunc.at.
    any_slc = np.bincount(inv, weights=slc, minlength=uniq.size) > 0

    out: dict[int, dict[int, bool]] = {}
    for k, is_slc in zip(uniq.tolist(), any_slc.tolist()):
        out.setdefault(k >> 16, {})[k & 0xFFFF] = bool(is_slc)
    return out, None


def cellmap(raw_path: str, cache_path: str) -> tuple[dict | None, str]:
    """{ce: {block: is_slc}}: cached JSON if present, else derived and cached."""
    import json

    try:
        if cache_path and os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as h:
                raw = json.load(h)
            out = {}
            for c, blocks in raw.items():
                row = {}
                for b, mode in blocks.items():
                    row[int(b)] = (mode.lower().startswith("slc")
                                   if isinstance(mode, str) else bool(mode))
                out[int(c)] = row
            return out, "cell map: from %s" % cache_path
    except (OSError, ValueError, AttributeError):
        pass  # an unreadable cache is not an answer; derive instead

    got, why = derive_cellmap(raw_path)
    if got is None:
        return None, "cell map unavailable -- %s" % why

    if cache_path:
        blob = json.dumps(
            {str(c): {str(b): ("slc" if v else "tlc") for b, v in row.items()}
             for c, row in got.items()},
            separators=(",", ":")).encode("utf-8")
        note = _atomic_write(cache_path, blob)
        if note:
            return got, ("cell map: derived from the index; NOT cached (%s)"
                         % note)
        return got, "cell map: derived from the index and cached at %s" % cache_path
    return got, "cell map: derived from the index"


# ------------------------------------------------------------------ summary --
def build_summary(raw_path: str, decoded_path: str | None, out_path: str,
                  log=None, on_bootstrap=None) -> tuple[str | None, str | None]:
    """Rebuild the per-block summary from the stores.  -> (path, None)|(None, why).

    MINUTES, and WORKER THREAD ONLY: it streams both indexes, mounts the FTL for
    the block map and the physical block properties, and samples page 0 of every
    held block for all-0x00.

    Written through a temp file and renamed for the same reason `_atomic_write`
    exists: `np.savez` writes the destination in place, so an interrupted build
    leaves a short npz that loads far enough to colour a drive map with.

    The temp name has to END in `.npz`.  numpy APPENDS the extension when the
    name it is given lacks it, so the obvious `out + ".part"` is written as
    `out.part.npz` and the rename then looks for a file that was never created
    -- measured, not guessed: the first version of this failed exactly that way
    after a successful 76 s build.
    """
    try:
        _ensure_titania()
        from titania import summary as _summary
    except Exception as exc:                                 # noqa: BLE001
        return None, "titania is not importable: %s: %s" % (
            type(exc).__name__, exc)

    tmp = os.path.splitext(out_path)[0] + ".part.npz"
    try:
        _summary.build(raw_path, decoded_path or None, tmp,
                       log=log or (lambda *a: None),
                       on_bootstrap=on_bootstrap)
        os.replace(tmp, out_path)
        return out_path, None
    except Exception as exc:                                 # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None, "summary build failed: %s: %s" % (type(exc).__name__, exc)


def summary(raw_path: str, decoded_path: str | None,
            cache_path: str) -> tuple[str | None, str]:
    """The per-block summary npz: cached file if present, else built and cached.

    Unlike the other two this one is not merely a convenience: with no summary
    the drive map has nothing to colour at all.  Rebuilding it is still the same
    contract -- the file is a CACHE of what the stores already say, and
    `titania.summary`'s own docstring says to rebuild it whenever a store is
    rebuilt, precisely because a stale one does not look stale.
    """
    if cache_path and os.path.isfile(cache_path):
        return cache_path, "summary: %s" % cache_path
    if not cache_path:
        return None, "summary: no path to build into"

    path, why = build_summary(raw_path, decoded_path, cache_path)
    if path is None:
        return None, "summary unavailable -- %s" % why
    return path, "summary: rebuilt from the stores and cached at %s" % path


# ------------------------------------------------------------------ all ------
def rebuild(raw_path, decoded_path=None, summary_path=None, desc_path=None,
            cell_path=None, want_summary=False, want_descriptor=True,
            want_cellmap=False, log=None):
    """Produce every missing sidecar, sharing the expensive work.  -> dict

    MEASURED, and the numbers are why this function exists rather than three
    independent calls:

        summary build        78.6 s   of which the FTL mount is 75.5 s
        msoDescriptor        53.4 s   another mount of the SAME Bootstrap FTL
        cell map              2.8 s   numpy over the index, no FTL at all
        ----------------------------
        sequential          134.7 s

    The descriptor is a SECTION of the Bootstrap mount the summary build already
    pays for, so taking it through `titania.summary.build(on_bootstrap=...)`
    removes that 53 s outright rather than merely overlapping it.

    What is deliberately NOT parallelised: the two FTL mounts inside a build.
    Measured at two threads on separate DumpDevices, they take 67.0 s against
    73.5 s sequential -- 1.10x -- because the mount is GIL-bound parsing, and
    the cheap Backup mount degrades 19.7 s -> 67.0 s under the contention.  The
    cell map DOES overlap, being numpy that drops the GIL, so it runs on its own
    thread and effectively costs nothing.

    `log(str)` is called with each phase as it starts, for a caller that wants
    to show progress.  Never raises; every product is independently optional and
    a failure is reported per product.
    """
    import threading

    say = log or (lambda *_a: None)
    out: dict = {}

    # The cell map overlaps the mount for free.  Started first so its 2.8 s is
    # already spent by the time anything else finishes.
    cell_result: dict = {}

    def _cell() -> None:
        try:
            cell_result["value"], cell_result["note"] = cellmap(
                raw_path, cell_path or "")
        except Exception as exc:                             # noqa: BLE001
            cell_result["value"] = None
            cell_result["note"] = "cell map unavailable -- %s: %s" % (
                type(exc).__name__, exc)

    cell_thread = None
    if want_cellmap:
        say("reducing the index for the cell map")
        cell_thread = threading.Thread(target=_cell, name="sidecar-cellmap",
                                       daemon=True)
        cell_thread.start()

    # --- the shared mount -------------------------------------------------
    grabbed: dict = {}

    def _grab(boot) -> None:
        """Take the msoDescriptor off the summary's own Bootstrap mount."""
        sec = boot.section(MSO_SECTION)
        buf = sec.contiguous() if hasattr(sec, "contiguous") else sec
        if buf:
            grabbed["descriptor"] = bytes(buf)

    need_descriptor = want_descriptor and desc_path and not os.path.isfile(desc_path)

    if want_summary and summary_path:
        say("starting -- about 75 s in total")
        def _phases(*a) -> None:
            """Forward the build's phase markers, in the READER's words.

            Two jobs.  First, filter: the build also logs a per-chunk record
            count every few million records and a closing census, and a status
            line flickering through those reads as noise rather than as work.

            Second, translate.  The build's own wording is a developer's
            ("streaming <path>", "mounting FTL 2/2"), and the timings are this
            module's measurements, not something the build knows.  Naming the
            expected duration is the difference between a line that reassures
            and one that just proves the program has not crashed yet -- which
            matters most in the Bootstrap mount, ~50 s during which nothing is
            logged at all.
            """
            line = " ".join(str(x) for x in a).strip()
            for prefix, friendly in _PHASES:
                if line.startswith(prefix):
                    say(friendly)
                    return

        path, why = build_summary(
            raw_path, decoded_path, summary_path, log=_phases,
            on_bootstrap=_grab if need_descriptor else None)
        out["summary"] = path
        out["summary_note"] = ("summary: rebuilt from the stores and cached at %s"
                               % path) if path else ("summary unavailable -- %s"
                                                     % why)

    # --- the descriptor: free if the build above handed it over -----------
    if want_descriptor:
        if "descriptor" in grabbed:
            buf = grabbed["descriptor"]
            note = _atomic_write(desc_path, buf) if desc_path else "no cache path"
            out["descriptor"] = buf
            out["descriptor_note"] = (
                "msoDescriptor: %d bytes taken from the summary's own FTL mount "
                "(no second mount) and cached at %s" % (len(buf), desc_path)
                if not note else
                "msoDescriptor: %d bytes from the summary's FTL mount; NOT "
                "cached (%s)" % (len(buf), note))
        else:
            if need_descriptor:
                say("mounting the Bootstrap FTL for the msoDescriptor "
                "(about 50 s)")
            buf, note = descriptor(raw_path, desc_path or "")
            out["descriptor"], out["descriptor_note"] = buf, note

    if cell_thread is not None:
        cell_thread.join()
        out["cellmap"] = cell_result.get("value")
        out["cellmap_note"] = cell_result.get("note")
    return out
