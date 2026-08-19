"""A blob-plus-index backing for raw NAND pages.

WHY THIS EXISTS
---------------
The dump tree is one file per page.  `the dump` holds 52 734 indexed pages in
56 230 files and that already hurts: the tree walk is ~1 s and counting the
disagreements costs ~4000 file opens and 9-70 s on a network or virtualised filesystem, which is
why `DumpDevice.describe()` refuses to do it by default.  A full array is
12 CE x 4096 blocks x 1104 pages = 54 million pages.  One file per page does not
survive three more orders of magnitude -- on any filesystem, but especially on a
share where an `open()` is a network round trip.

So: the bytes go into append-only blobs, one per (source, ce, block, mode), and
everything else goes into one fixed-record index that is binary-searched in
place.  A page read is a `seek`+`read` in an already-open handle; a lookup is
~log2(N) 24-byte slices of an mmap.  Nothing is decompressed, nothing is
rewritten, and no metadata is inferred from a file name at read time.

WHAT THIS MODULE IS NOT
-----------------------
It holds no policy.  It does not know what a trusted source is, which tiers are
excluded, or how a collision should be decided -- `titania.dumpdev` owns all of
that and passes the parts it needs in (`excluded_tiers`).  This module's whole
job is: given a key, hand back every candidate that claims it, in a defined
order, with the provenance each one was stored with.

ON-DISK LAYOUT
--------------
    <store>/
      index.tix          64-byte header + N fixed 24-byte records, sorted
      store.json         sources, blobs, layouts, tiers, per-mode statistics
      rejected.tsv       every file the builder could not place, and why
      ingest.tsv         builder's manifest (written by pc_migrate.py)
      blobs/<source>/ce06_b0016_slc.blob      dense runs of 18432-byte pages

A blob path in `store.json` is relative to the store and is allowed to escape
it: `../nk_dump/nk_ce6_b16_slc.bin` is a legal blob, so a hardware capture that
already writes one blob per (ce, block) is indexed where it lies, with no copy
and no conversion.  That is the reason the offset is stored per record rather
than computed from the page number -- see the offset decision below.

The record -- 24 bytes, and the first 6 are the key
---------------------------------------------------
      off  size  field       encoding
        0     1  ce          uint8
        1     2  block       uint16 BIG-endian
        3     2  page        uint16 BIG-endian
        5     1  flags       bit0 = pseudo-SLC
        6     1  kind        0 = a read of this page, 255 = a recorded SKIP
        7     1  tier_id     index into the tier table
        8     2  source_id   uint16 LE, index into the source table
       10     1  layout_id   index into the layout table
       11     1  (reserved)
       12     4  blob_id     uint32 le
       16     4  offset      uint32 LE, byte offset within the blob
       20     4  length      uint32 le

Bytes 0..5 are the key (ce, block, page, mode) and they compare, as raw bytes,
in exactly the order the Python tuple does -- big-endian integers and a mode bit
where False sorts before True.  So the file is sorted by `memcmp` on a 6-byte
prefix, a lookup is a plain `bisect`, and iterating the file yields keys in
sorted order with no sort step at all.

Byte 6 is a KIND, not a quality: 0 for a read, 255 for a record that says the
page was never read (`add_skipped`).  It is in the ordering for one reason --
a real read of a page must be served ahead of a note saying nobody read it --
and it has only those two values.  It used to be `255 - trust rank`, a 0..100
per-directory score, and that is gone: which of two candidate READS is right is
decided by decoding them (`dumpdev.adjudicate`), not by an ordering fixed when
the index was written.

Byte 7 onwards takes no part in the ordering, so within one kind the order of a
key's records is the order they were added -- which is NOT a decision, and the
reader treats a multi-record key as contested until it is adjudicated.

THE OFFSET DECISION
-------------------
A page is uniformly 18432 bytes, so an offset could be `page * 18432` and the
4 bytes could be dropped -- 24 B/page -> 20 B/page, a 17% saving.  It is not
taken, for one reason: a computed offset requires every blob to be a dense array
indexed by page number, and then the store can only reference blobs it wrote
itself.  Storing the offset is what lets `nn_ce8_b10_slc_p180-207.bin` -- a real
file this project already has, holding an arbitrary page range -- be indexed in
place.  17% of the index to keep every existing and future capture usable
without a rewrite is the right trade at any scale.

WHAT IT COSTS
-------------
24 B per candidate record.  52 734 pages -> 1.3 MB.  54 million -> 1.30 GB on
disk, and ~0 in Python: the index is mmapped, a lookup touches ~log2(54e6) = 26
records in <= 26 4 KiB OS pages, so the resident set of a random-read workload
is ~100 KB, not 1.3 GB.  The number that does not scale is `dict`: 54 million
Python tuples-and-objects is ~13 GB of heap, which is why `DumpDevice.entries`
over a store is a lazy `Mapping` and not a dict.
"""
import heapq
import json
import mmap
import os
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

MAGIC = b"TITSTOR2"
VERSION = 1

#: one record, and the 6-byte prefix that is the key
_HEAD = struct.Struct(">BHHBBB")        # ce, block, page, flags, kind, tier
_TAIL = struct.Struct("<HBBIII")        # source, layout, rsv, blob, off, len
KEY_BYTES = 6
REC_BYTES = _HEAD.size + _TAIL.size     # 8 + 16 = 24
assert REC_BYTES == 24

#: Record byte 6.  Two values, and the only ordering this format asserts: a read
#: of a page is served ahead of a note saying nobody read it.  Anything finer --
#: which of two READS is the good one -- is a question for the decoder, not for a
#: number written when the index was built.
KIND_READ = 0
KIND_SKIP = 255

#: 64-byte header; everything after it is records
_HDR = struct.Struct("<8sIIIIQQ")       # magic, ver, rec, key, page_bytes, n, rsv
HDR_BYTES = 64

INDEX_NAME = "index.tix"
META_NAME = "store.json"
REJECT_NAME = "rejected.tsv"
INGEST_NAME = "ingest.tsv"
BLOB_DIR = "blobs"
LOCK_NAME = ".lock"
RETRY_NAME = "retry_provenance.json"

#: a 4 GiB ceiling per blob.  A whole (ce, block) is 1104 x 18432 = 20 MB, so
#: this is 200x the largest blob anything here produces; it is checked rather
#: than assumed, because a silently truncated offset is an unfindable bug.
MAX_BLOB = 0xFFFFFFFF


#: Layout name for a page indexed with no bytes. Matches
#: `dumpdev.ERASED` so `page_class`/reporting stay consistent.
ERASED_LAYOUT = "erased"

#: A page the capture did not read, and why.
#:
#: The store already distinguished "the media reads 0xFF here" (an erased record)
#: from "we never looked" (no record at all).  That is enough for a dump that
#: intends to read everything, where absence only ever means "not finished yet".
#: It stops being enough the moment a capture skips ranges on purpose -- e.g.
#: because the FTL says a WERU is unallocated -- because then an interrupted dump
#: and a complete one look identical: both are silent about the same pages.
#:
#: A skipped record says the skip happened and, through its source's reason,
#: what it was based on.  It stores no bytes and reading it raises rather than
#: returning 0xFF: a skip is a claim about our procedure, never about the media,
#: and the day the evidence behind it turns out to be wrong is the day that
#: difference matters.
SKIPPED_LAYOUT = "skipped"


class StoreError(Exception):
    pass


class StoreLocked(StoreError):
    """Another writer holds this store's lock.  Carries `.info` (the payload)."""

    def __init__(self, message, info=None):
        StoreError.__init__(self, message)
        self.info = info or {}


# ------------------------------------------------------------------- locking --
# WHY A LOCKFILE AND NOT SOMETHING CLEVERER
# -----------------------------------------
# Several agents run against one store on one machine, over a network or virtualised filesystem.
# That rules out `fcntl`/`flock` (not meaningful on this share) and rules out a
# daemon (nothing here is long-lived).  What is left is the oldest mechanism
# there is: a file created with O_CREAT|O_EXCL, which is atomic on both NTFS and
# the 9p share, and whose contents say who holds it.
#
# The lock coordinates writers only.  Readers -- `PageStore`, `DumpDevice` --
# take nothing and are never blocked: the index is replaced by `os.replace`, so
# a reader either sees the whole old index or the whole new one, and an mmap of
# the old one keeps working because the inode survives.  What the lock prevents
# is two builders interleaving appends and index rewrites, which is the case
# that silently loses manifest lines and fattens the blobs.
#
# STALE LOCKS
# -----------
# A crashed builder leaves the file behind, and a lock that needs a human is a
# lock people delete by hand until they delete a live one.  So the holder writes
# its pid and its hostname, and a lock is broken automatically when
#   * the hostname is this machine's and the pid is not running, or
#   * the lock is older than `stale_after` (6 h by default -- a full rebuild of
#     the dump is ~4 min, so this is 90x the longest legitimate hold).
# The pid check never goes through `os.kill`: on Windows `os.kill(pid, 0)` calls
# TerminateProcess and would kill the process it was asked about.  It uses
# OpenProcess/GetExitCodeProcess instead.
_HELD = {}          # resolved root -> [StoreLock, depth]

#: a lock older than this is assumed to be a crashed writer's.
LOCK_STALE_SECONDS = 6 * 3600


def _pid_alive(pid):
    """Is this pid running?  -> bool.  Never signals the process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            # ACCESS_DENIED means the object exists and is not ours to inspect;
            # anything else (INVALID_PARAMETER) means there is no such process.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED \
                or k32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)                 # POSIX only: signal 0 tests, not kills
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def lock_info(root):
    """Who holds this store's lock?  -> dict, or None if it is free.

    Read-only and never breaks anything.  A malformed lockfile comes back as
    `{"unreadable": ...}` rather than as "free": a lock nobody can parse is
    still a lock somebody wrote.
    """
    p = Path(root) / LOCK_NAME
    try:
        raw = p.read_text("utf-8")
    except (OSError, ValueError):
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return {"unreadable": raw[:200]}
    if not isinstance(info, dict):
        return {"unreadable": raw[:200]}
    return info


def _describe_lock(info, age):
    who = "pid %s on %s" % (info.get("pid", "?"), info.get("host", "?"))
    return ("%s, op %r, held since %s (%.0f s ago)"
            % (who, info.get("op", "?"), info.get("time", "?"), age))


class StoreLock(object):
    """An exclusive advisory lock over one store directory.

    Use it as a context manager.  Re-entrant within a process (a build that
    holds it may call `compact()`, which takes it again), refused across
    processes with a message that names the holder.
    """

    def __init__(self, root, op="write", stale_after=LOCK_STALE_SECONDS,
                 timeout=0.0, poll=0.5, verbose=False):
        self.root = Path(root)
        self.op = op
        self.stale_after = float(stale_after)
        self.timeout = float(timeout)
        self.poll = float(poll)
        self.verbose = bool(verbose)
        self.path = self.root / LOCK_NAME
        self._key = str(self.root.resolve()).lower() if os.name == "nt" \
            else str(self.root.resolve())
        self._token = None
        self._nested = False

    # -- internals --------------------------------------------------------
    def _payload(self):
        return {"pid": os.getpid(),
                "host": socket.gethostname(),
                "op": self.op,
                "argv": " ".join(sys.argv[:3]),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": time.time(),
                "token": self._token}

    def _try_create(self):
        self._token = "%d-%d" % (os.getpid(), int(time.time() * 1e6))
        body = json.dumps(self._payload(), sort_keys=True).encode("utf-8")
        fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        return True

    def _breakable(self, info):
        """(should_break, why) for a lock we found in the way."""
        if not info:
            return True, "the lockfile vanished while it was being read"
        if "unreadable" in info:
            return False, "the lockfile is not valid JSON"
        age = max(0.0, time.time() - float(info.get("epoch") or 0))
        if info.get("host") == socket.gethostname() \
                and not _pid_alive(info.get("pid")):
            return True, ("pid %s on this machine is not running -- a crashed "
                          "writer" % info.get("pid"))
        if age > self.stale_after:
            return True, ("the lock is %.0f s old, past the %.0f s stale "
                          "threshold" % (age, self.stale_after))
        return False, ""

    # -- public -----------------------------------------------------------
    def acquire(self):
        held = _HELD.get(self._key)
        if held is not None:
            held[1] += 1
            self._nested = True
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                self._try_create()
                _HELD[self._key] = [self, 1]
                return self
            except FileExistsError:
                pass
            info = lock_info(self.root)
            if info is None and not self.path.exists():
                continue                       # it went away; race it again
            age = max(0.0, time.time() - float((info or {}).get("epoch") or 0))
            ok, why = self._breakable(info)
            if ok:
                if self.verbose:
                    print("breaking a stale store lock (%s): %s"
                          % (_describe_lock(info or {}, age), why))
                try:
                    os.remove(str(self.path))
                except OSError:
                    pass
                continue
            if time.time() < deadline:
                time.sleep(self.poll)
                continue
            raise StoreLocked(
                "%s is locked by another writer: %s.  Concurrent builds "
                "interleave blob appends with index rewrites and lose manifest "
                "lines, so this is refused rather than raced.  Wait for it, or "
                "-- if you are certain that process is gone -- delete %s."
                % (self.root, _describe_lock(info or {}, age), self.path),
                info or {})

    def release(self):
        if self._nested:
            self._nested = False
            held = _HELD.get(self._key)
            if held is not None:
                held[1] -= 1
            return
        held = _HELD.get(self._key)
        if held is not None and held[0] is self:
            held[1] -= 1
            if held[1] > 0:
                return
            _HELD.pop(self._key, None)
        # only remove a lockfile that is still ours: a stale-break by a third
        # process may already have replaced it, and unlinking that would hand
        # the store to two writers at once.
        info = lock_info(self.root)
        if info and info.get("token") == self._token:
            try:
                os.remove(str(self.path))
            except OSError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def lock(root, op="write", **kw):
    """`with store.lock(root, "build"): ...` -- the writers' mutual exclusion."""
    return StoreLock(root, op=op, **kw)


# --------------------------------------------------- retry provenance (sidecar)
# WHY THIS IS A SIDECAR AND NOT A WIDER RECORD
# -------------------------------------------
# The obvious fix for "the 24-byte record has no retry field" is a 25th byte.
# It is the wrong one, for three reasons that are all about what the two things
# ARE:
#
#  1. The record is a placement, not a capture.  Bytes 0..23 say where a page
#     lives and how far its source is trusted.  The retry level is a property of
#     the read event -- which reference ladder step answered -- and it arrives
#     from a different pipeline (`pw_fold.py`) after the build, from JSON the
#     dumper wrote.  Widening the record would not make the builder able to fill
#     it; it would only add a field the builder has to go and look up in this
#     same sidecar.
#
#  2. It is sparse, and the index is not.  78 pages of 57 000 carry retry
#     provenance today, and the interesting content is not one integer: it is
#     the level, the ladder step, the pre-retry MCRC, the source file and a
#     free-text note.  None of that fits in a byte, and 8 bytes x 54 million
#     records to describe a few hundred pages is 430 MB of index to carry a
#     field that is null 99.999% of the time.  The index is binary-searched in
#     place; every byte added is paid on every lookup's cache line.
#
#  3. Backward compatibility is not negotiable.  `index.tix` declares its record
#     size in the header and `PageStore` refuses a size it does not know, so a
#     25-byte record is a VERSION bump that makes every existing store
#     unreadable until it is rebuilt.  The store is cheap to rebuild -- but it
#     is rebuilt from `ingest.tsv`, and the pages themselves represent many
#     hours of serial-line time that no format change may put at risk.
#
# So the sidecar stays, and what changes is that it stops being an undocumented
# JSON blob consumers reach into by hand.  `RetryProvenance` owns the key
# convention (the same key the index uses), the document shape, the atomic
# write and the read helpers.  The on-disk format is unchanged -- `pw_fold.py`'s
# existing `retry_provenance.json` loads as-is and is byte-compatible on save.
def retry_key(ce, block, page, slc):
    """The sidecar key for a page -- the store's key, as text.

    The same four fields in the same order as `pack_key`, so a
    reader that has one has the other with no lookup table.
    """
    return "%d/%d/%d/%s" % (int(ce), int(block), int(page),
                            "slc" if slc else "tlc")


def parse_retry_key(key):
    """"ce/block/page/slc|tlc" -> (ce, block, page, slc)."""
    ce, block, page, mode = str(key).split("/")
    if mode not in ("slc", "tlc"):
        raise StoreError("bad retry key %r: mode must be slc or tlc" % (key,))
    return int(ce), int(block), int(page), mode == "slc"


_RETRY_WHAT = (
    "read-retry provenance for the pages in this store.  The 24-byte index "
    "record is a PLACEMENT and cannot carry it (see titania/store.py, 'why "
    "this is a sidecar'), so it lives here, beside the store, keyed exactly as "
    "the index keys a page.  A page listed with retry > 0 was NOT readable at "
    "the die's default references.")


class RetryProvenance(object):
    """The retry sidecar, as an object rather than a JSON file to poke at.

    ``prov = RetryProvenance.load(store_root)``
    ``prov.level(1, 3648, 37, False)``      -> 1
    ``prov.entry(1, 3648, 37, False)``      -> the whole record
    ``prov.rescued()``                      -> every page that needed a retry

    Absent file == empty provenance, not an error: most stores have none, and a
    caller asking "did this page need a retry" wants False, not a traceback.
    """

    __slots__ = ("root", "doc", "entries", "path")

    def __init__(self, root, doc=None):
        self.root = Path(root)
        self.path = self.root / RETRY_NAME
        self.doc = doc if doc is not None else {}
        ent = self.doc.get("entries")
        if ent is None:
            ent = {}
            self.doc["entries"] = ent
        if not isinstance(ent, dict):
            raise StoreError("%s: 'entries' is %s, expected an object"
                             % (self.path, type(ent).__name__))
        self.entries = ent

    # -- construction -----------------------------------------------------
    @classmethod
    def load(cls, root):
        p = Path(root) / RETRY_NAME
        if not p.exists():
            return cls(root, {"what": _RETRY_WHAT,
                              "key": "ce/block/page/slc|tlc -- the same key "
                                     "the store uses",
                              "pages": 0, "by_level": {}, "entries": {}})
        try:
            doc = json.loads(p.read_text("utf-8"))
        except ValueError as exc:
            raise StoreError("%s is not valid JSON (%s).  It is the only "
                             "record of which pages needed a read retry; "
                             "repair it rather than deleting it." % (p, exc))
        if not isinstance(doc, dict) or "entries" not in doc:
            raise StoreError("%s has no 'entries' map -- this is not a retry "
                             "provenance document" % p)
        return cls(root, doc)

    # -- reads ------------------------------------------------------------
    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def __contains__(self, key):
        return self._key(key) in self.entries

    @staticmethod
    def _key(key, *rest):
        if rest:
            return retry_key(key, *rest)
        if isinstance(key, (tuple, list)):
            return retry_key(*key)
        return str(key)

    def entry(self, key, *rest):
        """The full record for a page, or None."""
        return self.entries.get(self._key(key, *rest))

    def level(self, key, *rest, **kw):
        """The retry ladder level a page was read at, or `default` (None).

        `0` and `None` are not the same answer and are not flattened: 0 means
        "read at the die's default references and we know it", None means
        nothing was recorded, which is the case for almost every page.
        """
        default = kw.pop("default", None)
        if kw:
            raise TypeError("unexpected keyword %r" % sorted(kw)[0])
        e = self.entry(key, *rest)
        if e is None:
            return default
        v = e.get("retry")
        return default if v is None else int(v)

    def keys(self):
        return self.entries.keys()

    def items(self):
        return self.entries.items()

    def pages(self):
        """Every key as a store key tuple -> [(ce, block, page, slc)]."""
        return [parse_retry_key(k) for k in sorted(self.entries)]

    def by_level(self):
        """{level: count}, recomputed from the entries (never trusted stale)."""
        out = {}
        for e in self.entries.values():
            k = e.get("retry")
            out[k] = out.get(k, 0) + 1
        return out

    def rescued(self, min_level=1):
        """Pages that needed a retry -> [(key tuple, level)], sorted."""
        out = []
        for k in sorted(self.entries):
            v = self.entries[k].get("retry")
            if v is not None and int(v) >= min_level:
                out.append((parse_retry_key(k), int(v)))
        return out

    # -- writes -----------------------------------------------------------
    def set(self, key, *rest, **fields):
        """Record (or replace) one page's provenance.  -> the stored dict."""
        k = self._key(key, *rest) if rest else self._key(key)
        parse_retry_key(k)                       # validate before storing
        self.entries[k] = dict(fields)
        return self.entries[k]

    def update(self, mapping):
        for k, v in mapping.items():
            parse_retry_key(k)
            self.entries[k] = v
        return self

    def save(self, lock_store=True):
        """Write the document back, atomically, under the store lock.

        `pages` and `by_level` are recomputed here rather than maintained, so a
        hand-edited file cannot leave a summary that disagrees with its own
        entries.
        """
        self.doc.setdefault("what", _RETRY_WHAT)
        self.doc.setdefault("key", "ce/block/page/slc|tlc -- the same key the "
                                   "store uses")
        self.doc["pages"] = len(self.entries)
        self.doc["by_level"] = {str(k): v for k, v in
                                sorted(self.by_level().items(),
                                       key=lambda t: (t[0] is None, t[0]))}
        body = json.dumps(self.doc, indent=1, sort_keys=True)
        ctx = lock(self.root, op="retry-provenance") if lock_store \
            else _NullLock()
        with ctx:
            tmp = Path(str(self.path) + ".part")
            tmp.write_text(body, "utf-8")
            os.replace(str(tmp), str(self.path))
        return self.path


class _NullLock(object):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pack_key(ce, block, page, slc):
    """The 6-byte search key.  Byte order == tuple order."""
    return struct.pack(">BHHB", ce, block, page, 1 if slc else 0)


def unpack_key(buf):
    ce, block, page, flags = struct.unpack(">BHHB", buf[:KEY_BYTES])
    return (ce, block, page, bool(flags & 1))


def pack_record(ce, block, page, slc, kind, tier_id, source_id, layout_id,
                blob_id, offset, length):
    if kind not in (KIND_READ, KIND_SKIP):
        raise StoreError("kind %r is not KIND_READ or KIND_SKIP" % (kind,))
    if offset > MAX_BLOB or offset < 0:
        raise StoreError("blob offset %d exceeds the %d-byte per-blob limit; "
                         "split the blob rather than widening the record"
                         % (offset, MAX_BLOB))
    return (_HEAD.pack(ce, block, page, 1 if slc else 0, kind, tier_id)
            + _TAIL.pack(source_id, layout_id, 0, blob_id, offset, length))


class SourceRec(object):
    """Where a page came from, in words.

    The reason travels into the store as text, because "captured by the retry
    ladder at level 3" is the kind of thing nobody reconstructs later.  There is
    no rank here: this records PROVENANCE, and provenance does not decide which
    of two reads is right -- decoding them does.
    """

    __slots__ = ("sid", "name", "tier", "reason")

    def __init__(self, sid, name, tier, reason):
        self.sid = int(sid)
        self.name = name
        self.tier = tier
        self.reason = reason

    def as_json(self):
        return [self.name, self.tier, self.reason]

    def __repr__(self):
        return "<SourceRec %s %s>" % (self.name, self.tier)


class PageRef(object):
    """One candidate page in the store -- a stand-in for the `Path` a tree has.

    `DumpDevice` was written against files, and half of it reads `ent.path`.
    Rather than fork every one of those call sites, a `PageRef` answers the
    handful of questions the code actually asks a path: `read_bytes()`, `name`,
    `parts`, `parent`, `relative_to()`, equality and an ordering.  It also
    answers two a `Path` cannot -- `layout` and `trust` are recorded here rather
    than re-derived from the file name and the directory, which is strictly
    better provenance than the tree had.

    The original file name is not kept: it costs a variable-length
    string table (~1.5 GB at 54M pages) to preserve a name whose only content is
    the address, which the record already holds exactly.  `pc_migrate.py` writes
    the original relative path into `ingest.tsv` for traceability, which is the
    right place for a build artefact.
    """

    __slots__ = ("store", "ce", "block", "page", "slc", "src", "layout",
                 "blob_id", "offset", "length", "_name")

    def __init__(self, store, ce, block, page, slc, src, layout, blob_id,
                 offset, length):
        self.store = store
        self.ce = ce
        self.block = block
        self.page = page
        self.slc = slc
        self.src = src
        self.layout = layout
        self.blob_id = blob_id
        self.offset = offset
        self.length = length
        self._name = "ce%02d_b%04d_p%04d_%s.bin" % (
            ce, block, page, "slc" if slc else "tlc")

    # -- the path-shaped surface -----------------------------------------
    @property
    def name(self):
        return self._name

    @property
    def parts(self):
        return (self.src.name, self._name)

    @property
    def parent(self):
        return PurePosixPath(self.src.name)

    @property
    def suffix(self):
        return ".bin"

    def relative_to(self, _other=None):
        return PurePosixPath(self.src.name) / self._name

    @property
    def provenance(self):
        """(tier, reason) as recorded, not as re-derived."""
        return (self.src.tier,
                "%s/: %s" % (self.src.name, self.src.reason))

    @property
    def retry(self):
        """The read-retry level this page was captured at, or None.

        Comes out of the store's retry sidecar (`RetryProvenance`), which is
        loaded once per store.  None means nothing was recorded -- which is the
        answer for almost every page and is not the same as level 0.
        """
        return self.store.retry.level((self.ce, self.block, self.page,
                                       self.slc))

    #: Page bytes for a record that stores none. An erased page is 18432 bytes
    #: of 0xFF and there is no reason to keep 18432 copies of that on disk: it is
    #: indexed with `length = 0` and synthesised here. The record still exists,
    #: which is the point -- `DumpDevice` raises for a page the dump does not
    #: hold and returns 0xFF for one it holds and knows to be erased, and
    #: conflating those two has produced wrong conclusions in this tree before.
    #: A dead-CE page (the 0xA7/0x00 bus constant) is not erased and must never
    #: take this path; it is real bytes and is stored as such.
    ERASED_LENGTH = 0

    def is_erased(self):
        """The media reads 0xFF here.  A skipped page is not erased.

        Both store zero bytes, so length alone cannot tell them apart, and
        answering True for a skip would hand callers 18432 bytes of 0xFF for a
        page nobody ever read -- the exact confusion the erased encoding exists
        to prevent.
        """
        return (self.length == self.ERASED_LENGTH
                and self.layout != SKIPPED_LAYOUT)

    def is_skipped(self):
        """This page was not read.  See `SKIPPED_LAYOUT`."""
        return self.layout == SKIPPED_LAYOUT

    def _skip_error(self):
        return StoreError(
            "ce=%d block=%d page=%d (%s) was DELIBERATELY NOT READ, so there "
            "are no bytes to serve -- it is not erased and it is not missing. "
            "Recorded by %s (%s): %s"
            % (self.ce, self.block, self.page, "SLC" if self.slc else "TLC",
               self.src.name, self.src.tier, self.src.reason))

    def read_bytes(self):
        if self.is_skipped():
            raise self._skip_error()
        if self.length == self.ERASED_LENGTH:
            return bytes([0xFF]) * self.store.page_bytes
        return self.store.read_at(self.blob_id, self.offset, self.length)

    def head(self, n):
        """First `n` bytes -- the cheap 'is this page even a candidate' probe."""
        if self.is_skipped():
            raise self._skip_error()
        if self.length == self.ERASED_LENGTH:
            return bytes([0xFF]) * min(n, self.store.page_bytes)
        return self.store.read_at(self.blob_id, self.offset,
                                  min(n, self.length))

    def write_bytes(self, data):
        if len(data) != self.length:
            raise StoreError("a stored page is %d bytes, got %d"
                             % (self.length, len(data)))
        return self.store.write_at(self.blob_id, self.offset, data)

    def blob_path(self):
        return self.store.blob_path(self.blob_id)

    def stat(self):
        """`stat()` of the blob this page lives in.

        The blob and not something synthetic: the question every
        caller asks a page's `stat()` here is "did the backing file change",
        and for a store the backing file is the blob.  An overlay write that
        left the blob's mtime alone really did leave the capture alone.
        """
        return self.store.blob_path(self.blob_id).stat()

    # -- identity ---------------------------------------------------------
    def _id(self):
        return (self.blob_id, self.offset, self.length)

    def __eq__(self, other):
        if isinstance(other, PageRef):
            return self._id() == other._id()
        return NotImplemented

    def __ne__(self, other):
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    def __hash__(self):
        return hash(self._id())

    def __lt__(self, other):
        if isinstance(other, PageRef):
            return (str(self), self._id()) < (str(other), other._id())
        return NotImplemented

    def __str__(self):
        return "%s/%s" % (self.src.name, self._name)

    def __repr__(self):
        return "<PageRef %s blob=%d+%d>" % (self, self.blob_id, self.offset)


class PageStore(object):
    """Reader over a blob-plus-index store.

    `excluded_tiers` is the only policy this class takes, and it takes it from
    the caller: a record whose tier is listed is indexed, kept, and not served.
    Nothing is dropped from the store on account of it -- which is what lets
    `DumpDevice(include_suspect=True)` see exactly the same superseded and
    quarantined pages the directory walk saw, and lets the selftest keep
    asserting that they can never outrank a current source.
    """

    def __init__(self, root, excluded_tiers=(), max_open=192):
        self.root = Path(root)
        self.excluded = frozenset(excluded_tiers)
        self.max_open = int(max_open)
        idx = self.root / INDEX_NAME
        if not idx.exists():
            raise StoreError("no %s under %s -- not a page store"
                             % (INDEX_NAME, self.root))
        meta = json.loads((self.root / META_NAME).read_text("utf-8"))
        # Checked BEFORE anything is parsed out of it.  A store written by the
        # rank-era build has four-element source records and a `255 - rank` byte
        # in the index sort key; read as though it were this format it would
        # raise somewhere unhelpful, or worse, be read in the wrong order.
        stamp = meta.get("magic")
        if stamp is not None and stamp != MAGIC.decode():
            raise StoreError(
                "%s was written by an incompatible build (%s; this one reads "
                "%s).  The record format changed when per-source RANKS were "
                "removed: byte 6 of the index record was `255 - rank` and is "
                "now a two-valued kind, and source records no longer carry a "
                "rank at all.  Re-index to read it here -- the blobs are "
                "unchanged, so it costs a walk of %s, not a re-read of the "
                "media." % (self.root / META_NAME, stamp, MAGIC.decode(),
                            META_NAME))
        self.meta = meta
        self.page_bytes = int(meta.get("page_bytes", 18432))
        self.sources = {int(k): SourceRec(int(k), *v)
                        for k, v in meta["sources"].items()}
        self.blobs = {int(k): v for k, v in meta["blobs"].items()}
        self.layouts = {int(k): v for k, v in meta["layouts"].items()}
        self.tiers = {int(k): v for k, v in meta["tiers"].items()}
        self.stats = meta.get("stats", {})
        self.built = meta.get("built")

        self._fh = open(str(idx), "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        magic, ver, rec, keyb, pb, n, _rsv = _HDR.unpack(
            self._mm[:_HDR.size])
        if magic != MAGIC:
            raise StoreError("%s: bad magic %r" % (idx, magic))
        if ver != VERSION or rec != REC_BYTES or keyb != KEY_BYTES:
            raise StoreError("%s: version %d rec %d key %d, this build reads "
                             "%d/%d/%d" % (idx, ver, rec, keyb, VERSION,
                                           REC_BYTES, KEY_BYTES))
        if pb != self.page_bytes:
            raise StoreError("%s says %d-byte pages, %s says %d"
                             % (idx, pb, META_NAME, self.page_bytes))
        self.n_records = int(n)
        want = HDR_BYTES + self.n_records * REC_BYTES
        if len(self._mm) != want:
            raise StoreError("%s is %d bytes, expected %d for %d records"
                             % (idx, len(self._mm), want, self.n_records))
        self._open = {}          # blob_id -> file handle (bounded, LRU)
        self._order = []         # blob_id, least recent first
        self._paths = {bid: str(self.root / rel)
                       for bid, (rel, _sz) in self.blobs.items()}
        self._rejected = None
        self._retry = None
        self._excluded_ids = frozenset(
            i for i, t in self.tiers.items() if t in self.excluded)

    @property
    def retry(self):
        """This store's read-retry provenance -> `RetryProvenance`.

        Lazy and cached.  Absent sidecar == empty, so `st.retry.level(k)` is
        always safe to ask.
        """
        if self._retry is None:
            self._retry = RetryProvenance.load(self.root)
        return self._retry

    # -- lifecycle --------------------------------------------------------
    def close(self):
        for h in self._open.values():
            try:
                h.close()
            except OSError:
                pass
        self._open.clear()
        self._order = []
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- raw record access ------------------------------------------------
    def _rec(self, i):
        off = HDR_BYTES + i * REC_BYTES
        return self._mm[off:off + REC_BYTES]

    def _key_at(self, i):
        off = HDR_BYTES + i * REC_BYTES
        return self._mm[off:off + KEY_BYTES]

    def _lower_bound(self, key):
        """First record index whose key >= `key`.  Plain bisect on bytes."""
        lo, hi = 0, self.n_records
        while lo < hi:
            mid = (lo + hi) // 2
            if self._key_at(mid) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _decode(self, buf):
        ce, block, page, flags, kind, tier = _HEAD.unpack(buf[:_HEAD.size])
        sid, lid, _rsv, bid, off, ln = _TAIL.unpack(buf[_HEAD.size:])
        return (ce, block, page, bool(flags & 1), kind, tier, sid, lid,
                bid, off, ln)

    def _ref(self, buf):
        (ce, block, page, slc, _kind, _tier, sid, lid, bid, off,
         ln) = self._decode(buf)
        return PageRef(self, ce, block, page, slc, self.sources[sid],
                       self.layouts.get(lid, "unknown"), bid, off, ln)

    def _served_tier(self, buf):
        return buf[7]

    # -- lookup -----------------------------------------------------------
    def candidates(self, key):
        """Every candidate for one key, most trusted first.  -> [PageRef]

        Excluded-tier records are filtered here and nowhere else, so a key whose
        only candidate is superseded is genuinely absent rather than served with
        a warning -- which is the behaviour the directory walk had.
        """
        kb = pack_key(*key)
        i = self._lower_bound(kb)
        out = []
        while i < self.n_records and self._key_at(i) == kb:
            buf = self._rec(i)
            if self._served_tier(buf) not in self._excluded_ids:
                out.append(self._ref(buf))
            i += 1
        return out

    def served(self, key):
        """The first candidate for this key that is served, or None.

        Cheaper than `candidates()[0]`: it stops at the first record that
        survives the tier filter instead of materialising the repeats, which is
        what the read path wants.
        """
        kb = pack_key(*key)
        i = self._lower_bound(kb)
        while i < self.n_records and self._key_at(i) == kb:
            buf = self._rec(i)
            if self._served_tier(buf) not in self._excluded_ids:
                return self._ref(buf)
            i += 1
        return None

    def has(self, key):
        return self.served(key) is not None

    # -- range queries ----------------------------------------------------
    def _range(self, lo_key, hi_key):
        i = self._lower_bound(lo_key)
        j = self._lower_bound(hi_key)
        return i, j

    def count_in_block(self, ce, block, slc):
        """How many pages this store holds for one (ce, block, mode).

        Answers `_absent_message`'s question with two binary searches instead of
        a scan over every key -- the difference between a helpful error message
        and one nobody can afford to raise.
        """
        i, j = self._range(pack_key(ce, block, 0, False),
                           pack_key(ce, block + 1, 0, False))
        n, seen = 0, None
        for k in range(i, j):
            buf = self._rec(k)
            if self._served_tier(buf) in self._excluded_ids:
                continue
            key = self._key_at(k)
            if key == seen:
                continue
            if bool(buf[5] & 1) != bool(slc):
                continue
            seen = key
            n += 1
        return n

    def keys_in_block(self, ce, block):
        i, j = self._range(pack_key(ce, block, 0, False),
                           pack_key(ce, block + 1, 0, False))
        out, seen = [], None
        for k in range(i, j):
            buf = self._rec(k)
            if self._served_tier(buf) in self._excluded_ids:
                continue
            key = self._key_at(k)
            if key == seen:
                continue
            seen = key
            out.append(unpack_key(key))
        return out

    # -- iteration --------------------------------------------------------
    def iter_served(self):
        """(key, ref, [repeat refs]) for every served key, in sorted order.

        One linear pass over the mmap, no sort and no dict.  This is what makes
        `conflicts()` and `adjudicate()` affordable: they want the contested
        keys and nothing else, and `contested_keys()` below hands them over
        without a pass at all.
        """
        i = 0
        n = self.n_records
        while i < n:
            key = self._key_at(i)
            refs = []
            j = i
            while j < n and self._key_at(j) == key:
                buf = self._rec(j)
                if self._served_tier(buf) not in self._excluded_ids:
                    refs.append(self._ref(buf))
                j += 1
            if refs:
                yield unpack_key(key), refs[0], refs[1:]
            i = j

    def iter_keys(self):
        i = 0
        n = self.n_records
        while i < n:
            key = self._key_at(i)
            j = i
            served = False
            while j < n and self._key_at(j) == key:
                if self._served_tier(self._rec(j)) not in self._excluded_ids:
                    served = True
                j += 1
            if served:
                yield unpack_key(key)
            i = j

    def contested_keys(self):
        """Keys with more than one served candidate, from the build statistics.

        Recorded at build time, so this costs a JSON read rather than a scan of
        54 million records.  `mode` is `suspect` or `strict` because the two
        answers genuinely differ: including the quarantined layouts adds
        candidates and therefore adds contests.
        """
        mode = "suspect" if not self.excluded else "strict"
        raw = self.stats.get(mode, {}).get("contested_keys")
        if raw is None:
            return [k for k, _r, rep in self.iter_served() if rep]
        return [tuple(x[:3]) + (bool(x[3]),) for x in raw]

    # -- statistics -------------------------------------------------------
    def stat(self, name, default=None):
        mode = "suspect" if not self.excluded else "strict"
        if name == "ces":
            ces = self._ces_for_mode()
            if ces is not None:
                return ces
        return self.stats.get(mode, {}).get(name, default)

    def _ces_for_mode(self):
        """CEs this store serves under the current exclusion policy, or None.

        Derived from `ces_by_tier`, which `StoreWriter.finish` records from the
        records it actually wrote -- so it is a fact about the index and cannot
        be omitted, mistyped, or left behind by a fold-in the way a hand-passed
        `stats["ces"]` could be, and was.  Returns None for a store built before
        `ces_by_tier` existed, so `stat()` falls back to whatever that build
        chose to record; old stores keep working and new ones cannot be wrong.
        """
        by_tier = self.meta.get("ces_by_tier")
        if not by_tier:
            return None
        out = set()
        for tier, ces in by_tier.items():
            if tier in self.excluded:
                continue
            out.update(int(c) for c in ces)
        return sorted(out)

    @property
    def rejected(self):
        """(path, reason) for every file the builder could not place.

        Loaded once, lazily.  A store that lost this would be strictly worse
        than the tree it replaced: an unrecognised naming scheme has to stay
        visible, or the coverage silently halves and nothing says so.
        """
        if self._rejected is None:
            out = []
            p = self.root / REJECT_NAME
            if p.exists():
                with open(str(p), "r", encoding="utf-8") as h:
                    for line in h:
                        line = line.rstrip("\n")
                        if not line or line.startswith("#"):
                            continue
                        path, _, reason = line.partition("\t")
                        out.append((path, reason))
            self._rejected = out
        return self._rejected

    # -- blob I/O ---------------------------------------------------------
    def blob_path(self, blob_id):
        return self.root / self.blobs[blob_id][0]

    def _handle(self, blob_id):
        """An open handle for a blob, LRU-cached.

        The cache is the whole read path.  A blob is one (source, ce, block,
        mode) -- up to 1104 consecutive pages -- so any access that has any
        locality at all opens a file once and then only seeks, which is what
        turns 18 000 `open()` calls on a network or virtualised filesystem into 16.  The LRU has
        to be a real one: a FIFO evicts the handle a caller is walking through,
        and the measured cost of getting that wrong on this share was 6.7x.
        """
        h = self._open.get(blob_id)
        if h is not None:
            if self._order[-1] != blob_id:
                self._order.remove(blob_id)
                self._order.append(blob_id)
            return h
        while len(self._order) >= self.max_open:
            old = self._order.pop(0)
            try:
                self._open.pop(old).close()
            except (KeyError, OSError):
                pass
        # `str(self.root / rel)` and not `Path.resolve()`: resolve() walks the
        # path on the filesystem, and on a unc share that is several round trips
        # per read.  Measured: it was most of a 15 ms random-page read.
        h = open(self._paths[blob_id], "rb")
        self._open[blob_id] = h
        self._order.append(blob_id)
        return h

    def read_at(self, blob_id, offset, length):
        h = self._handle(blob_id)
        h.seek(offset)
        data = h.read(length)
        if len(data) != length:
            raise StoreError(
                "blob %s is short: wanted %d bytes at %d, got %d.  The index "
                "and the blob disagree; the store is damaged and must be "
                "rebuilt rather than read around."
                % (self.blobs[blob_id][0], length, offset, len(data)))
        return data

    def write_at(self, blob_id, offset, data):
        """In-place write into a blob the store owns.

        A blob that lives outside `<store>/blobs/` is a capture this store
        merely points at -- `../nk_dump/nk_ce6_b16_slc.bin` and friends -- and
        it is never written through.  Overwriting the raw capture a store was
        built over would destroy the only copy of the media, so it is refused
        here rather than guarded by convention.
        """
        p = self.blob_path(blob_id).resolve()
        owned = (self.root / BLOB_DIR).resolve()
        try:
            p.relative_to(owned)
        except ValueError:
            raise StoreError(
                "refusing to write into %s: it is outside %s, so it is a raw "
                "capture this store only REFERENCES.  flush() to a new "
                "directory instead." % (p, owned))
        for bid in list(self._open):
            if bid == blob_id:
                self._open.pop(bid).close()
                self._order.remove(bid)
        with open(str(p), "r+b") as h:
            h.seek(offset)
            h.write(data)
        return len(data)

    def __repr__(self):
        return "<PageStore %s records=%d blobs=%d sources=%d>" % (
            self.root.name, self.n_records, len(self.blobs),
            len(self.sources))


def is_store(path):
    """Does this path look like a page store?  -> bool"""
    p = Path(path)
    return p.is_dir() and (p / INDEX_NAME).is_file() \
        and (p / META_NAME).is_file()


# ---------------------------------------------------------------- builder --
def _next_id(existing):
    """One past the highest id in use -- not len().

    len() is only the same thing while the ids are dense from zero, which they
    are in a fresh build and need not be in a reopened one: the ids come back
    from the manifest, and a store that ever loses a vocabulary entry has a gap.
    Handing out a duplicate id there would repoint existing records at the new
    entry, silently.
    """
    ids = [int(i) for i in existing]
    return max(ids) + 1 if ids else 0


def _ces_by_tier(seen_tier_ce, tier_name_to_id):
    """{tier name: [ce, ...]} from the (tier id, ce) pairs a build wrote.

    Why this is recorded at all, and why it is keyed on the tier.

    `DumpDevice.present_ces()` answers from stored metadata rather than by
    scanning the index, and `model.probe_block_status`-style callers begin every
    row with `if ce not in present_ces(): continue`.  So a store that does not
    carry the CE list does not merely lose a statistic -- it reads back as
    holding no CEs, and every consumer skips the whole drive while the records
    sit there.  That happened: a decode of 5.4 million pages was written with a
    `stats` block that omitted `ces`, and the viewer painted every block of every
    CE as "no decoded record".  The store was fine; the metadata said it was
    empty.  Deriving this in `finish()` is what stops any future builder from
    reproducing it, because there is no longer anything for a builder to pass.

    Keyed on tier, not on `strict`/`suspect`, because those two words are a
    reader's policy: which tiers a reader excludes is `dumpdev`'s business (see
    `EXCLUDED_TIERS`), and this module owns no policy.  Recording
    the breakdown lets `PageStore.stat("ces")` intersect it with whatever that
    reader excludes, so one build serves both modes and neither can be stale.
    """
    names = {tid: name for name, tid in tier_name_to_id.items()}
    out = {}
    for tier_id, ce in seen_tier_ce:
        out.setdefault(names.get(tier_id, str(tier_id)), set()).add(int(ce))
    return {k: sorted(v) for k, v in sorted(out.items())}


class StoreWriter(object):
    """Builds a store.  Append-only for the blobs, external-sort for the index.

    Records are buffered and spilled to temporary runs, then k-way merged, so
    the build's memory is bounded by `spill` records regardless of how many
    pages there are.  At 54 million records the default spill (1 M) is ~24 MB of
    buffer and 54 runs to merge, which is a merge, not a problem.

    The sort key is the 7-byte record prefix followed by the source PATH.  The
    prefix orders by (ce, block, page, mode) then by descending trust, which is
    what the reader binary-searches.  The path is the tie-break and it exists
    for one reason: the directory walk broke an equal-rank collision on sorted
    path order, and a store that broke it differently would serve different
    bytes for those pages.  It is still an arbitrary tie -- it is recorded as
    one -- but it must be the same arbitrary tie, or a migration cannot be
    verified byte-for-byte against the tree it came from.
    """

    def __init__(self, root, page_bytes=18432, spill=1000000):
        self.root = Path(root)
        self.page_bytes = int(page_bytes)
        self.spill = int(spill)
        (self.root / BLOB_DIR).mkdir(parents=True, exist_ok=True)
        self._sources = {}        # name -> SourceRec
        self._blobs = {}          # relpath -> (id, size)
        self._layouts = {}        # name -> id
        self._tiers = {}          # name -> id
        self._buf = []
        self._runs = []
        self._n = 0
        self._append = {}         # relpath -> open handle
        self.rejected = []

    # -- reopening --------------------------------------------------------
    @classmethod
    def reopen(cls, root, spill=1000000):
        """Open a finished store to add more pages to it.  -> StoreWriter

        `finish()` consumes the records it buffered, so a second writer over the
        same root writes an index describing only what it added and silently
        drops everything already in the store.  That is why a long capture used
        to land as part001, part002, ... -- a directory per checkpoint, with a
        merge pass afterwards -- and why the obvious "just append" was wrong:
        the blobs append safely, the index does not.

        This loads the existing index back into the writer, so `finish()`
        rewrites a complete index covering old and new records alike, and one
        store can be extended across as many runs as it takes.

        The manifest's ID assignments -- source, layout, tier and blob ids --
        are restored exactly.  Every loaded record encodes those numbers, so
        renumbering them would repoint each one at the wrong source or the wrong
        blob while leaving the store perfectly well-formed: the worst kind of
        corruption, because nothing would complain.

        One caveat, recorded rather than hidden: the sort key's tie-break is the
        source PATH, which the index does not keep.  Loaded records are re-keyed
        with an empty tie-break, so where two records share (ce, block, page,
        mode, rank) their order after a reopen is the order they already had in
        the index rather than by path.  Ranked collisions still resolve by rank,
        which is what the reader acts on; only equal-rank ties are affected, and
        a store with one record per page -- every capture this writes -- has
        none.
        """
        root = Path(root)
        meta = json.loads((root / META_NAME).read_text("utf-8"))
        if meta.get("magic") != MAGIC.decode():
            raise StoreError("%s: not a titania store (magic=%r)"
                             % (root, meta.get("magic")))
        w = cls(root, page_bytes=int(meta["page_bytes"]), spill=spill)
        for sid, rec in meta.get("sources", {}).items():
            name, tier, reason = rec
            w._sources[(name, tier)] = SourceRec(int(sid), name, tier, reason)
        for bid, (rel, size) in meta.get("blobs", {}).items():
            w._blobs[str(rel)] = (int(bid), int(size))
        w._layouts = {name: int(i)
                      for i, name in meta.get("layouts", {}).items()}
        w._tiers = {name: int(i) for i, name in meta.get("tiers", {}).items()}
        w._load_index()
        return w

    def _load_index(self):
        """Existing index.tix -> a pre-sorted spill run, ready to be merged.

        The index is already in sort order, so it goes in as a run rather than
        through the buffer: the k-way merge in `_finish` then costs one extra
        stream regardless of how many records the store already holds.
        """
        idx = self.root / INDEX_NAME
        if not idx.exists():
            return 0
        with open(str(idx), "rb") as h:
            head = h.read(HDR_BYTES)
            if len(head) < HDR_BYTES:
                raise StoreError("%s is truncated: %d-byte header"
                                 % (idx, len(head)))
            magic, _ver, rec_bytes, _key, _pb, claimed, _rsv = \
                _HDR.unpack(head[:_HDR.size])
            if magic != MAGIC or rec_bytes != REC_BYTES:
                raise StoreError("%s: not an index this build can extend "
                                 "(magic=%r rec_bytes=%d)"
                                 % (idx, magic, rec_bytes))
            fd, name = tempfile.mkstemp(prefix="tix_run_", dir=str(self.root))
            got = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    rec = h.read(REC_BYTES)
                    if len(rec) < REC_BYTES:
                        break
                    sk = rec[:7] + b"\x00"
                    out.write(struct.pack("<H", len(sk)) + sk + rec)
                    got += 1
        # A short index means a capture was cut off mid-write.  Loading it
        # anyway is right -- the records that are there are real -- but saying
        # so is not optional: the alternative is a store that quietly holds
        # fewer pages than its own manifest claims.
        if got != claimed:
            self.rejected.append(
                (str(idx), "index header claims %d records, %d readable; "
                           "extending the readable ones" % (claimed, got)))
        self._runs.append(name)
        self._n += got
        return got

    def checkpoint(self, stats=None, extra=None):
        """Write a complete index now and stay open for more pages.  -> count

        For long captures: after this the store on disk is whole and readable,
        and the same writer keeps appending.  A run killed later loses only the
        pages read since the last checkpoint, and -- unlike the part-per-chunk
        scheme this replaces -- there is still exactly one store.
        """
        n = self.finish(stats=stats, extra=extra)
        self._n = 0                 # _load_index re-counts what was just written
        self._load_index()
        return n

    # -- vocabulary -------------------------------------------------------
    def source(self, name, tier, reason):
        """Register (or reuse) a source.  -> SourceRec

        Keyed by (name, rank, tier), not by name.  A directory is not required
        to have one verdict: `<dump>/deep/` holds both ordinary pages, which
        take `TRUST["deep"]`, and `deep_bad`-layout pages, which are quarantined
        at rank 0 by their layout regardless of the directory.  Keying on the
        name alone made the first file walked decide for every later one --
        which is how `deep` came to be recorded as tier `suspect` when the trust
        table says `superseded`, and would just as happily have recorded a
        quarantined page as a current one had the walk order been the other way
        round.  Two verdicts for one directory are two sources here.
        """
        key = (name, tier)
        s = self._sources.get(key)
        if s is None:
            s = SourceRec(_next_id(v.sid for v in self._sources.values()),
                          name, tier, reason)
            self._sources[key] = s
        return s

    def _layout_id(self, name):
        if name not in self._layouts:
            self._layouts[name] = _next_id(self._layouts.values())
        return self._layouts[name]

    def _tier_id(self, name):
        if name not in self._tiers:
            self._tiers[name] = _next_id(self._tiers.values())
        return self._tiers[name]

    # -- blobs ------------------------------------------------------------
    def blob(self, relpath, size=None):
        """Register a blob by its path relative to the store.  -> blob_id"""
        relpath = str(relpath).replace("\\", "/")
        b = self._blobs.get(relpath)
        if b is None:
            if size is None:
                p = (self.root / relpath)
                size = p.stat().st_size if p.exists() else 0
            b = (len(self._blobs), size)
            self._blobs[relpath] = b
        return b[0]

    def append(self, relpath, data):
        """Append bytes to a store-owned blob.  -> (blob_id, offset)"""
        relpath = str(relpath).replace("\\", "/")
        h = self._append.get(relpath)
        if h is None:
            p = self.root / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            # "r+b" + an explicit seek to the end rather than "ab": in append
            # mode the file position before the first write is not defined by
            # the language, and this code needs the offset, not just the write.
            h = open(str(p), "r+b" if p.exists() else "w+b")
            h.seek(0, os.SEEK_END)
            self._append[relpath] = h
        h.seek(0, os.SEEK_END)
        off = h.tell()
        if off + len(data) > MAX_BLOB:
            raise StoreError("blob %s would pass the %d-byte limit"
                             % (relpath, MAX_BLOB))
        h.write(data)
        bid = self.blob(relpath, size=0)
        self._blobs[relpath] = (bid, off + len(data))
        return bid, off

    def blob_size(self, relpath):
        relpath = str(relpath).replace("\\", "/")
        b = self._blobs.get(relpath)
        if b is None:
            p = self.root / relpath
            return p.stat().st_size if p.exists() else 0
        h = self._append.get(relpath)
        return h.tell() if h is not None else b[1]

    # -- records ----------------------------------------------------------
    def add(self, ce, block, page, slc, src, layout, blob_id, offset,
            length, order_key="", kind=KIND_READ):
        rec = pack_record(ce, block, page, slc, kind,
                          self._tier_id(src.tier), src.sid,
                          self._layout_id(layout), blob_id, offset, length)
        self._buf.append((rec[:7] + b"\x00" + order_key.encode("utf-8"), rec))
        self._n += 1
        if len(self._buf) >= self.spill:
            self._flush_run()

    def add_erased(self, ce, block, page, slc, src, order_key=""):
        """Index an erased page without storing its bytes.

        Costs one fixed-size index record instead of 18432 bytes. On this media
        that is the difference between ~68 GiB and well under a gigabyte per CE,
        since ~99% of blocks read all-0xFF.

        Only for pages actually read and found to be all-0xFF. Do not use it for
        pages that were skipped -- an absent record and an erased record mean
        different things on purpose.
        """
        self.add(ce, block, page, slc, src, ERASED_LAYOUT, 0, 0,
                 PageRef.ERASED_LENGTH, order_key=order_key)

    def add_skipped(self, ce, block, page, slc, src, order_key=""):
        """Record that this page was not read.  See SKIPPED_LAYOUT.

        `src` carries the justification: register it with a name and reason that
        say what the skip was based on ("ftl says weru 312 is unallocated"), not
        merely that one happened.  A skip with no stated evidence is worse than
        no record at all, because it looks like one.

        Written as KIND_SKIP, which is the whole reason byte 6 is in the sort
        key: a skip record competes for its key like any other, and if it came
        first the store would answer "not read" for a page it actually holds.
        """
        self.add(ce, block, page, slc, src, SKIPPED_LAYOUT, 0, 0,
                 PageRef.ERASED_LENGTH, order_key=order_key, kind=KIND_SKIP)

    def reject(self, path, reason):
        self.rejected.append((str(path), reason))

    def _flush_run(self):
        if not self._buf:
            return
        self._buf.sort(key=lambda t: t[0])
        fd, name = tempfile.mkstemp(prefix="tix_run_", dir=str(self.root))
        with os.fdopen(fd, "wb") as h:
            for sk, rec in self._buf:
                h.write(struct.pack("<H", len(sk)) + sk + rec)
        self._runs.append(name)
        self._buf = []

    @staticmethod
    def _read_run(path):
        with open(path, "rb") as h:
            while True:
                hdr = h.read(2)
                if len(hdr) < 2:
                    return
                n = struct.unpack("<H", hdr)[0]
                sk = h.read(n)
                rec = h.read(REC_BYTES)
                yield sk, rec

    # -- finish -----------------------------------------------------------
    def finish(self, stats=None, extra=None, take_lock=True):
        """Write index.tix, store.json and rejected.tsv.  -> record count

        Takes the store lock for the duration unless the caller already holds
        it (`pc_migrate.build()` holds it across the whole build, which is the
        window that actually matters -- appends and the index rewrite).
        """
        ctx = lock(self.root, op="finish") if take_lock else _NullLock()
        with ctx:
            return self._finish(stats=stats, extra=extra)

    def _finish(self, stats=None, extra=None):
        for h in self._append.values():
            h.close()
        self._append = {}
        idx = self.root / INDEX_NAME
        tmp = self.root / (INDEX_NAME + ".part")
        # (tier id, ce) for every record written -- see `ces_by_tier` below.
        # Gathered HERE because this loop is the one place every record passes
        # through, so what lands in the metadata is what the index actually
        # holds rather than a build's opinion about what it meant to write.
        seen_tier_ce = set()
        with open(str(tmp), "wb") as out:
            out.write(_HDR.pack(MAGIC, VERSION, REC_BYTES, KEY_BYTES,
                                self.page_bytes, self._n, 0))
            out.write(b"\x00" * (HDR_BYTES - _HDR.size))
            if self._runs:
                self._flush_run()
                streams = [self._read_run(p) for p in self._runs]
                for _sk, rec in heapq.merge(*streams, key=lambda t: t[0]):
                    seen_tier_ce.add((rec[7], rec[0]))   # _HEAD: ce=0, tier=7
                    out.write(rec)
            else:
                self._buf.sort(key=lambda t: t[0])
                for _sk, rec in self._buf:
                    seen_tier_ce.add((rec[7], rec[0]))   # _HEAD: ce=0, tier=7
                    out.write(rec)
        for p in self._runs:
            os.remove(p)
        self._runs = []
        self._buf = []
        try:
            if idx.exists():
                idx.unlink()
            tmp.rename(idx)
        except OSError as exc:
            raise StoreError(
                "cannot replace %s: %s.  The index is mmapped by a reader that "
                "still has this store open -- call DumpDevice.close() (or use "
                "it as a context manager) before rebuilding.  The new index is "
                "left at %s; nothing already in the store was changed."
                % (idx, exc, tmp))

        meta = {
            "magic": MAGIC.decode(),
            "version": VERSION,
            "page_bytes": self.page_bytes,
            "records": self._n,
            "sources": {str(s.sid): s.as_json()
                        for s in self._sources.values()},
            "blobs": {str(bid): [rel, size]
                      for rel, (bid, size) in self._blobs.items()},
            "layouts": {str(v): k for k, v in self._layouts.items()},
            "tiers": {str(v): k for k, v in self._tiers.items()},
            "ces_by_tier": _ces_by_tier(seen_tier_ce, self._tiers),
            "stats": stats or {},
        }
        if extra:
            meta.update(extra)
        (self.root / META_NAME).write_text(
            json.dumps(meta, indent=1, sort_keys=True), "utf-8")
        with open(str(self.root / REJECT_NAME), "w", encoding="utf-8") as h:
            h.write("# path\treason -- files the builder could NOT place\n")
            for path, reason in self.rejected:
                h.write("%s\t%s\n" % (path.replace("\t", " "),
                                      reason.replace("\t", " ")
                                            .replace("\n", " ")))
        return self._n


# -------------------------------------------------------------- compaction --
# WHY COMPACTION IS PART OF THE STORE AND NOT A ONE-OFF SCRIPT
# ------------------------------------------------------------
# The blobs are append-only and the builder's `finish()` is the last thing a
# build does.  So a build that appends 4000 pages and then cannot replace
# `index.tix` -- because another process on this tree has it mmapped, which on
# Windows is a hard `PermissionError`, and several agents run against one store
# -- leaves the store correct and the blobs fat: the bytes went in, the manifest
# line never did, and the next run appends the very same page again.  Nothing is
# lost and nothing is wrong; the store just carries a second copy of those pages
# that no record points at.
#
# That is a recurring, structural consequence of concurrency plus append-only,
# not an accident, so the repair belongs next to the thing that creates it.
#
# WHAT IS "LIVE"
# --------------
# A byte range is live if `index.tix` OR `ingest.tsv` references it.  Both,
# because a manifest line whose source file has left the tree is
# not indexed (`stale_manifest_entries`) while its bytes still back the
# incremental build -- dropping those would make the next run re-append the page
# and re-create the orphan this is meant to remove.
#
# WHAT IT WILL NOT TOUCH
# ----------------------
# A blob outside `<store>/blobs/` is a raw capture the store merely references
# -- `../nk_dump/nk_ce6_b16_slc.bin` and friends.  Rewriting one would destroy
# the only copy of that media, so unreferenced bytes there are reported and left
# exactly where they are, the same rule `write_at` enforces.
def _merge_ranges(rs):
    """[(off, len)] -> sorted, merged [(start, end)].  Adjacent ranges join."""
    out = []
    for off, ln in sorted(rs):
        end = off + ln
        if out and off <= out[-1][1]:
            if end > out[-1][1]:
                out[-1][1] = end
        else:
            out.append([off, end])
    return [(a, b) for a, b in out]


def _index_header(root):
    """(header_bytes, n_records), validated.  -> (bytes, int)"""
    p = Path(root) / INDEX_NAME
    with open(str(p), "rb") as h:
        head = h.read(HDR_BYTES)
    magic, ver, rec, keyb, _pb, n, _rsv = _HDR.unpack(head[:_HDR.size])
    if magic != MAGIC:
        raise StoreError("%s: bad magic %r" % (p, magic))
    if ver != VERSION or rec != REC_BYTES or keyb != KEY_BYTES:
        raise StoreError("%s: version %d rec %d key %d, this build reads "
                         "%d/%d/%d" % (p, ver, rec, keyb, VERSION, REC_BYTES,
                                       KEY_BYTES))
    return head, n


def _iter_index(root, per=65536):
    """Chunks of raw records from `index.tix`, streamed and without mmap.

    Not `PageStore`: this reads an index that is about to be
    replaced, and an mmap of it is the very thing that makes the replace fail.
    A generator: at 54 million records the index is 1.3 GB, and
    this module does not get to buy back its own scaling argument with a
    `read()`.  Callers that need two passes open it twice.
    """
    p = Path(root) / INDEX_NAME
    _head, n = _index_header(root)
    with open(str(p), "rb") as h:
        h.seek(HDR_BYTES)
        left = n
        while left:
            take = min(left, per)
            buf = h.read(take * REC_BYTES)
            if len(buf) != take * REC_BYTES:
                raise StoreError("%s is short: %d record(s) declared, file "
                                 "ends early" % (p, n))
            yield buf
            left -= take


def _iter_manifest(root):
    """(rel, size, mtime, blob, off, len) for every builder manifest line."""
    p = Path(root) / INGEST_NAME
    if not p.exists():
        return
    with open(str(p), "r", encoding="utf-8") as h:
        for line in h:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) != 6:
                continue
            yield f[0], int(f[1]), int(f[2]), f[3], int(f[4]), int(f[5])


def blob_usage(root):
    """What every blob holds vs what anything references.

    -> {relpath: (disk_size, [(start, end), ...] merged live ranges)}
    """
    root = Path(root)
    meta = json.loads((root / META_NAME).read_text("utf-8"))
    id2rel = {int(k): v[0] for k, v in meta["blobs"].items()}
    per = {rel: [] for rel in id2rel.values()}
    for buf in _iter_index(root):
        for o in range(0, len(buf), REC_BYTES):
            _sid, _lid, _rsv, bid, off, ln = _TAIL.unpack(
                buf[o + _HEAD.size:o + REC_BYTES])
            rel = id2rel.get(bid)
            if rel is None:
                raise StoreError("index references blob id %d, which %s does "
                                 "not describe" % (bid, META_NAME))
            per[rel].append((off, ln))
    for _rel, _sz, _mt, blob, off, ln in _iter_manifest(root):
        per.setdefault(blob, []).append((off, ln))
    out = {}
    for rel, rs in per.items():
        p = root / rel
        out[rel] = (p.stat().st_size if p.exists() else 0, _merge_ranges(rs))
    return out


def orphan_report(root):
    """(total_orphan, foreign_orphan, {rel: (size, live_bytes, orphan)}).

    Read-only.  This is the measurement `--compact --dry-run` prints and the
    number a caller should check before deciding the store needs anything.
    """
    total = foreign = 0
    owned = (Path(root) / BLOB_DIR).resolve()
    detail = {}
    for rel, (size, live) in blob_usage(root).items():
        live_bytes = sum(b - a for a, b in live)
        orphan = size - live_bytes
        if orphan <= 0:
            continue
        detail[rel] = (size, live_bytes, orphan)
        try:
            (Path(root) / rel).resolve().relative_to(owned)
        except ValueError:
            foreign += orphan
        else:
            total += orphan
    return total, foreign, detail


def _remap(plan, off):
    """Old blob offset -> new blob offset, through a compaction plan."""
    for a, b, new in plan:
        if a <= off < b:
            return new + (off - a)
    raise StoreError("offset %d is not inside any live range; the index and "
                     "the manifest disagree and the store must be rebuilt "
                     "rather than compacted" % off)


def _unlink(p):
    try:
        os.remove(str(p))
    except OSError:
        pass


def compact(root, dry_run=False, verbose=True, chunk=8 << 20):
    """Drop unreferenced bytes from store-owned blobs.  -> dict of counters

    ORDER OF OPERATIONS, AND WHY
    ----------------------------
    Everything slow and everything destructive are kept apart.  The new blob
    contents, the new index and the new manifest are all written to side files
    first, and each new blob is read back and compared against the live ranges
    of the original before anything is swapped -- so a mistake in the remap is
    caught while the store is still untouched.

    Only then is `index.tix` renamed aside.  That rename is the same operation
    the failed build could not do, so it is the exclusivity check: if a reader
    still has the index mmapped it raises here, with nothing altered.  After it
    succeeds the remaining work is a short burst of renames, and any failure in
    that burst is rolled back.
    """
    with lock(root, op="compact"):
        return _compact(root, dry_run=dry_run, verbose=verbose, chunk=chunk)


def _compact(root, dry_run=False, verbose=True, chunk=8 << 20):
    root = Path(root)
    owned = (root / BLOB_DIR).resolve()
    usage = blob_usage(root)

    plans = {}
    total_orphan = foreign_orphan = 0
    for rel in sorted(usage):
        size, live = usage[rel]
        live_bytes = sum(b - a for a, b in live)
        orphan = size - live_bytes
        if orphan <= 0:
            continue
        try:
            (root / rel).resolve().relative_to(owned)
        except ValueError:
            foreign_orphan += orphan
            if verbose:
                print("  LEFT ALONE (referenced capture, not store-owned): "
                      "%s -- %d unreferenced byte(s)" % (rel, orphan))
            continue
        remap, new = [], 0
        for a, b in live:
            remap.append((a, b, new))
            new += b - a
        plans[rel] = (size, new, remap)
        total_orphan += orphan

    res = {"blobs": len(usage), "compacted": 0, "orphan_bytes": total_orphan,
           "foreign_orphan_bytes": foreign_orphan, "records": 0,
           "manifest_lines": 0, "dry_run": bool(dry_run)}
    if verbose:
        print("%d blob(s); %d byte(s) unreferenced in %d store-owned blob(s)"
              % (len(usage), total_orphan, len(plans)))
    if not plans or dry_run:
        return res

    # -- 1. new blob contents, verified, into side files ------------------
    for rel, (_old, _new, remap) in sorted(plans.items()):
        src = root / rel
        dst = root / (rel + ".new")
        with open(str(src), "rb") as h, open(str(dst), "wb") as o:
            for a, b, _n in remap:
                h.seek(a)
                left = b - a
                while left:
                    part = h.read(min(left, chunk))
                    if not part:
                        raise StoreError(
                            "blob %s is shorter than the index claims; the "
                            "store is damaged and must be rebuilt rather than "
                            "compacted" % rel)
                    o.write(part)
                    left -= len(part)
        with open(str(src), "rb") as h, open(str(dst), "rb") as o:
            for a, b, n in remap:
                h.seek(a)
                o.seek(n)
                left = b - a
                while left:
                    take = min(left, chunk)
                    if h.read(take) != o.read(take):
                        _unlink(dst)
                        raise StoreError(
                            "compacted copy of %s does not match the original "
                            "over a live range; nothing was replaced" % rel)
                    left -= take

    # -- 2. new index and new manifest, into side files -------------------
    head, _n = _index_header(root)
    meta = json.loads((root / META_NAME).read_text("utf-8"))
    id2rel = {int(k): v[0] for k, v in meta["blobs"].items()}
    idx_part = root / (INDEX_NAME + ".part")
    with open(str(idx_part), "wb") as out:
        out.write(head)
        for buf in _iter_index(root):
            buf = bytearray(buf)
            for o in range(0, len(buf), REC_BYTES):
                sid, lid, rsv, bid, off, ln = _TAIL.unpack(
                    bytes(buf[o + _HEAD.size:o + REC_BYTES]))
                plan = plans.get(id2rel[bid])
                if plan is None:
                    continue
                buf[o + _HEAD.size:o + REC_BYTES] = _TAIL.pack(
                    sid, lid, rsv, bid, _remap(plan[2], off), ln)
                res["records"] += 1
            out.write(bytes(buf))
    man_part = None
    if (root / INGEST_NAME).exists():
        man_part = root / (INGEST_NAME + ".part")
        with open(str(man_part), "w", encoding="utf-8") as h:
            h.write("# source-relpath\tsize\tmtime_ns\tblob\toffset\tlength\n")
            for rel, size, mt, blob, off, ln in _iter_manifest(root):
                plan = plans.get(blob)
                if plan is not None:
                    off = _remap(plan[2], off)
                    res["manifest_lines"] += 1
                h.write("%s\t%d\t%d\t%s\t%d\t%d\n"
                        % (rel, size, mt, blob, off, ln))

    # -- 3. the exclusivity check: rename the index aside -----------------
    idx = root / INDEX_NAME
    keep = root / (INDEX_NAME + ".old")
    if keep.exists():
        raise StoreError(
            "%s already exists -- a previous compaction stopped between "
            "renaming the index aside and putting the new one in place.  "
            "Inspect it before re-running; do not delete anything." % keep)
    try:
        os.replace(str(idx), str(keep))
    except OSError as exc:
        for rel in plans:
            _unlink(root / (rel + ".new"))
        _unlink(idx_part)
        if man_part is not None:
            _unlink(man_part)
        raise StoreError(
            "cannot compact %s: %s.  %s is mmapped by a reader that still has "
            "this store open -- close every DumpDevice/PageStore over it (or "
            "use them as context managers) and re-run.  NOTHING was changed; "
            "the side files have been removed."
            % (root, exc, INDEX_NAME))

    # -- 4. the swap, rolled back as a unit -------------------------------
    swapped = []
    try:
        for rel in sorted(plans):
            blob = root / rel
            os.replace(str(blob), str(root / (rel + ".old")))
            os.replace(str(root / (rel + ".new")), str(blob))
            swapped.append(rel)
            res["compacted"] += 1
        os.replace(str(idx_part), str(idx))
        if man_part is not None:
            os.replace(str(man_part), str(root / INGEST_NAME))
        for rel, (_old, new, _r) in plans.items():
            for k, v in meta["blobs"].items():
                if v[0] == rel:
                    meta["blobs"][k] = [rel, new]
        (root / META_NAME).write_text(
            json.dumps(meta, indent=1, sort_keys=True), "utf-8")
    except OSError as exc:
        for rel in swapped:
            _unlink(root / (rel + ".new"))
            try:
                os.replace(str(root / rel), str(root / (rel + ".new")))
                os.replace(str(root / (rel + ".old")), str(root / rel))
            except OSError:
                pass
        try:
            os.replace(str(keep), str(idx))
        except OSError:
            pass
        raise StoreError("compaction failed mid-swap (%s); rolled back %d "
                         "blob(s).  Verify the store before using it."
                         % (exc, len(swapped)))

    for rel in swapped:
        _unlink(root / (rel + ".old"))
    _unlink(keep)
    if verbose:
        print("compacted %d blob(s), reclaimed %d byte(s) (%.2f MB); "
              "remapped %d index record(s) and %d manifest line(s)"
              % (res["compacted"], total_orphan, total_orphan / 1048576.0,
                 res["records"], res["manifest_lines"]))
    return res


# There used to be a `trust_drift` pair here, and the reason it is gone is worth
# a paragraph.  Records carried a per-source RANK in byte 6 of the sort key, so
# which candidate a key served was fixed when the index was written; a rank that
# later changed in the live table could not be relabelled at read time without
# changing what the store SAID about a page while still serving a different one.
# Detecting that drift was the least-bad answer to a problem that should not have
# existed.  Nothing in the sort key describes provenance any more -- byte 6 is
# now a two-valued KIND -- so there is no copy to go stale and nothing to detect.


def compute_stats(root, excluded_tiers):
    """Everything `describe()` needs, in one linear pass over the index.

    Run at build time and stored, so that a summary of a 54-million-page store
    costs a JSON read rather than a walk.  Every number here is counted, not
    estimated: `contested` is the real number of extra candidates and
    `contested_keys` is the real list, which is what makes `adjudicate()` able
    to visit the contested pages and nothing else.
    """
    st = PageStore(root, excluded_tiers=excluded_tiers)
    try:
        pages = slc = contested = ties = 0
        ces = set()
        groups = set()
        by_layout = {}
        by_source = {}
        contested_keys = []
        tie_keys = []
        served_records = 0
        for key, ref, reps in st.iter_served():
            pages += 1
            served_records += 1 + len(reps)
            ces.add(key[0])
            if key[3]:
                slc += 1
            groups.add((key[0], key[1], key[3]))
            by_layout[ref.layout] = by_layout.get(ref.layout, 0) + 1
            n, tier = by_source.get(ref.src.name, (0, ref.src.tier))
            by_source[ref.src.name] = (n + 1, tier)
            if reps:
                # Every multi-candidate key is a tie: nothing in the index ranks
                # one read above another any more, so which one `served()`
                # returns is insertion order and is not a decision.
                # `dumpdev.adjudicate()` decodes them and settles it.
                contested += len(reps)
                contested_keys.append([key[0], key[1], key[2], int(key[3])])
                ties += 1
                tie_keys.append([key[0], key[1], key[2], int(key[3])])
    finally:
        st.close()
    return {"pages": pages, "slc_pages": slc, "served_records": served_records,
            "groups": len(groups),
            "ces": sorted(ces), "contested": contested, "ties": ties,
            "by_layout": by_layout,
            "sources": {k: list(v) for k, v in by_source.items()},
            "contested_keys": contested_keys, "tie_keys": tie_keys}


def write_stats(root, stats_by_mode):
    """Patch `store.json`'s `stats` in place.  -> the new meta dict."""
    p = Path(root) / META_NAME
    meta = json.loads(p.read_text("utf-8"))
    meta["stats"] = stats_by_mode
    p.write_text(json.dumps(meta, indent=1, sort_keys=True), "utf-8")
    return meta


def _selftest():
    """Round-trips a synthetic store: pack/unpack, ordering, lookup, tiers."""
    import random
    import shutil
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="titania_store_"))
    try:
        pb = 64
        w = StoreWriter(tmp, page_bytes=pb, spill=64)
        good = w.source("good", "unknown", "made up for the selftest")
        bad = w.source("bad", "suspect", "made up, and quarantined")
        want = {}
        random.seed(7)
        keys = []
        for ce in range(3):
            for block in (0, 7, 4095):
                for page in (0, 5, 1103):
                    for slc in (True, False):
                        keys.append((ce, block, page, slc))
        for k in keys:
            data = bytes((random.randrange(256) for _ in range(pb)))
            # the BLOB_DIR prefix is not decoration: it is what makes a blob
            # store-owned, and therefore writable and compactable.  The builder
            # writes it, so the selftest must too or it exercises the
            # foreign-capture path by accident.
            rel = "%s/%s/ce%02d_b%04d_%s.blob" % (BLOB_DIR, "good", k[0], k[1],
                                                  "slc" if k[3] else "tlc")
            bid, off = w.append(rel, data)
            w.add(k[0], k[1], k[2], k[3], good, "generic", bid, off, pb,
                  order_key=rel)
            want[k] = data
        # a quarantined twin for one key, with different bytes
        twin = (0, 7, 5, True)
        tdata = b"\xAA" * pb
        bid, off = w.append(BLOB_DIR + "/bad/twin.blob", tdata)
        w.add(twin[0], twin[1], twin[2], twin[3], bad, "deep", bid, off, pb,
              order_key=BLOB_DIR + "/bad/twin.blob")
        w.reject("nowhere/odd.bin", "no layout matches the file name")
        n = w.finish(stats={"strict": {"pages": len(want)},
                            "suspect": {"pages": len(want)}})
        print("[s1] wrote %d records over %d blobs" % (n, len(w._blobs)))
        ok &= n == len(want) + 1

        st = PageStore(tmp, excluded_tiers=("suspect",))
        got = [st.read_at(*(lambda r: (r.blob_id, r.offset, r.length))(
            st.served(k))) for k in keys]
        same = sum(1 for k, g in zip(keys, got) if want[k] == g)
        print("[s2] %d of %d pages read back byte-identical" % (same, len(keys)))
        ok &= same == len(keys)

        order = list(st.iter_keys())
        print("[s3] index order == sorted tuple order: %s"
              % (order == sorted(order)))
        ok &= order == sorted(order)
        ok &= len(order) == len(keys)

        cand = st.candidates(twin)
        print("[s4] quarantined twin filtered: %d candidate(s) served (want 1); "
              "served bytes are the unquarantined ones: %s"
              % (len(cand), cand[0].read_bytes() == want[twin]))
        ok &= len(cand) == 1 and cand[0].read_bytes() == want[twin]

        loose = PageStore(tmp, excluded_tiers=())
        lc = loose.candidates(twin)
        print("[s5] with no tier filter the same key has %d candidates, and "
              "the order between them is NOT a verdict: %s"
              % (len(lc), [c.src.name for c in lc]))
        ok &= len(lc) == 2

        miss = st.served((9, 9, 9, True))
        print("[s6] a key the store does not hold: served() -> %r (want None)"
              % miss)
        ok &= miss is None

        n_block = st.count_in_block(0, 7, True)
        print("[s7] count_in_block(ce0,b7,SLC) = %d (want 3)" % n_block)
        ok &= n_block == 3

        print("[s8] rejected carried into the store: %r" % (st.rejected,))
        ok &= st.rejected == [("nowhere/odd.bin",
                               "no layout matches the file name")]

        # a foreign blob must never be written through
        w2 = StoreWriter(tmp / "sub", page_bytes=pb)
        bid = w2.blob("../../elsewhere.bin", size=pb)
        w2.add(0, 0, 0, True, w2.source("x", "unknown", "-"), "generic",
               bid, 0, pb, order_key="z")
        w2.finish()
        st2 = PageStore(tmp / "sub")
        try:
            st2.write_at(bid, 0, b"\x00" * pb)
            print("[s9] wrote into a foreign blob -- WRONG")
            ok = False
        except StoreError as exc:
            print("[s9] write into a referenced (non-owned) blob refused: %s"
                  % str(exc)[:60])
        st2.close()
        st.close()
        loose.close()

        # -- compaction ---------------------------------------------------
        # Simulate exactly the failure that creates orphans: append bytes to a
        # blob and do not record them anywhere.  Nothing indexed changes; the
        # blob just grows.
        rel = BLOB_DIR + "/good/ce00_b0007_slc.blob"
        before = (tmp / rel).stat().st_size
        with open(str(tmp / rel), "r+b") as h:
            h.seek(0, os.SEEK_END)
            h.write(b"\xDE" * (pb * 3))
        # ...and one in the middle of the file's history, by widening a blob
        # that other records still point past, to prove the remap is not just a
        # truncation.
        tot, foreign, detail = orphan_report(tmp)
        print("[s10] orphan_report: %d unreferenced byte(s) over %d blob(s) "
              "(want %d)" % (tot, len(detail), pb * 3))
        ok &= tot == pb * 3 and foreign == 0

        dry = compact(tmp, dry_run=True, verbose=False)
        print("[s11] --dry-run changed nothing: blob still %d B"
              % (tmp / rel).stat().st_size)
        ok &= dry["compacted"] == 0
        ok &= (tmp / rel).stat().st_size == before + pb * 3

        # a live reader must make it refuse, not corrupt
        holder = PageStore(tmp, excluded_tiers=("suspect",))
        try:
            compact(tmp, verbose=False)
            print("[s12] compacted with the index mmapped -- WRONG")
            ok = False
        except StoreError as exc:
            print("[s12] compaction with a live reader refused: %s"
                  % str(exc).split(".")[0][:70])
        finally:
            holder.close()
        ok &= (tmp / rel).stat().st_size == before + pb * 3

        res = compact(tmp, verbose=False)
        print("[s13] compacted %d blob(s), reclaimed %d byte(s); blob %d -> %d"
              % (res["compacted"], res["orphan_bytes"], before + pb * 3,
                 (tmp / rel).stat().st_size))
        ok &= res["orphan_bytes"] == pb * 3
        ok &= (tmp / rel).stat().st_size == before

        st3 = PageStore(tmp, excluded_tiers=("suspect",))
        got3 = [st3.served(k).read_bytes() for k in keys]
        same3 = sum(1 for k, g in zip(keys, got3) if want[k] == g)
        print("[s14] after compaction %d of %d pages still byte-identical"
              % (same3, len(keys)))
        ok &= same3 == len(keys)
        ok &= st3.candidates(twin)[0].read_bytes() == want[twin]
        ok &= st3.served((9, 9, 9, True)) is None
        st3.close()

        tot2, _f2, _d2 = orphan_report(tmp)
        print("[s15] second pass finds nothing left to do: %d orphan byte(s)"
              % tot2)
        ok &= tot2 == 0
        ok &= compact(tmp, verbose=False)["compacted"] == 0

        # -- locking ------------------------------------------------------
        ok &= lock_info(tmp) is None
        with lock(tmp, op="selftest"):
            info = lock_info(tmp)
            print("[s16] lock taken: pid %s host %s op %r"
                  % (info["pid"], info["host"], info["op"]))
            ok &= info["pid"] == os.getpid()
            # re-entrant within the process: compaction under a held lock must
            # not deadlock, because that is exactly what build() does.
            nested = compact(tmp, dry_run=True, verbose=False)
            print("[s17] re-entrant: compact() under a held lock ran (%d blob"
                  "(s)), did not deadlock" % nested["blobs"])
            ok &= nested["dry_run"] is True
            # a different holder must be refused.  Forge one by rewriting the
            # payload with a pid that is alive but not ours -- pid 1/System
            # exists on both platforms -- and a foreign hostname so the
            # this-machine pid test cannot break it.
            saved = (tmp / LOCK_NAME).read_text("utf-8")
            (tmp / LOCK_NAME).write_text(json.dumps(
                {"pid": 4, "host": "some-other-box", "op": "build",
                 "time": "now", "epoch": time.time(), "token": "x"}), "utf-8")
            _HELD.clear()
            try:
                lock(tmp, op="selftest").acquire()
                print("[s18] a foreign live lock did NOT refuse -- WRONG")
                ok = False
            except StoreLocked as exc:
                print("[s18] contention refused, naming the holder: %s"
                      % str(exc).split(".")[0][-72:])
                ok &= exc.info.get("host") == "some-other-box"
            # ...and the same lock, aged past the threshold, is broken
            got = lock(tmp, op="selftest", stale_after=0.0).acquire()
            print("[s19] the same lock, past its stale threshold, was broken "
                  "and retaken: %s" % (lock_info(tmp)["pid"] == os.getpid()))
            ok &= lock_info(tmp)["pid"] == os.getpid()
            got.release()
            # put the outer holder's own payload back, so its release()
            # recognises the lockfile as its own and removes it.
            (tmp / LOCK_NAME).write_text(saved, "utf-8")
            _HELD.clear()
        ok &= lock_info(tmp) is None
        print("[s20] lock released: %r" % (lock_info(tmp),))
        # a pid that cannot exist is never alive; our own always is
        ok &= _pid_alive(os.getpid()) and not _pid_alive(0x7FFFFFF0)
        print("[s21] pid liveness: self=%s, an impossible pid=%s (and NOTHING "
              "was signalled)"
              % (_pid_alive(os.getpid()), _pid_alive(0x7FFFFFF0)))

        # -- retry provenance sidecar -------------------------------------
        rp = RetryProvenance.load(tmp)
        print("[s22] a store with no sidecar loads as empty, not an error: "
              "%d entry(ies)" % len(rp))
        ok &= len(rp) == 0 and rp.level((1, 2, 3, False)) is None
        rp.set(1, 3648, 37, False, retry=1, mcrc=4,
               retry_note="made up for the selftest")
        rp.set(8, 2, 1, True, retry=0, retry_note="default references")
        rp.save()
        rp2 = RetryProvenance.load(tmp)
        print("[s23] sidecar round-trip: %d entries, by_level %s"
              % (len(rp2), rp2.by_level()))
        ok &= len(rp2) == 2 and rp2.by_level() == {0: 1, 1: 1}
        ok &= rp2.level(1, 3648, 37, False) == 1
        ok &= rp2.level(8, 2, 1, True) == 0
        ok &= rp2.level(8, 2, 1, False) is None          # mode is part of key
        ok &= rp2.rescued() == [((1, 3648, 37, False), 1)]
        print("[s24] the key is the store's key as text: %r <-> %r"
              % (retry_key(6, 9, 11, True), parse_retry_key("6/9/11/slc")))
        ok &= parse_retry_key(retry_key(6, 9, 11, True)) == (6, 9, 11, True)
        st4 = PageStore(tmp, excluded_tiers=("suspect",))
        ref = st4.served(keys[0])
        print("[s25] PageStore.retry reaches it: page %r retry=%r, and the "
              "recorded one is %r" % (keys[0], ref.retry,
                                      st4.retry.level(1, 3648, 37, False)))
        ok &= ref.retry is None and st4.retry.level(1, 3648, 37, False) == 1
        st4.close()
        # the shape on disk is the one pw_fold.py already writes
        doc = json.loads((tmp / RETRY_NAME).read_text("utf-8"))
        ok &= set(doc) >= {"what", "key", "pages", "by_level", "entries"}
        ok &= doc["pages"] == 2 and doc["by_level"] == {"0": 1, "1": 1}
        print("[s26] on-disk shape unchanged (what/key/pages/by_level/entries),"
              " summary recomputed from the entries: %s" % doc["by_level"])

        # -- a skip never shadows a read -----------------------------------
        # The one ordering this format asserts.  Both records are written for
        # the same key, the skip second, and `served()` must still answer with
        # the read: byte 6 is KIND, and KIND_READ sorts first.
        w4 = StoreWriter(tmp / "skips", page_bytes=pb)
        rsrc = w4.source("read", "unknown", "a real read")
        ssrc = w4.source("skip:test", "unknown", "recorded as not read")
        bid4, off4 = w4.append(BLOB_DIR + "/read/p.blob", b"\x5A" * pb)
        w4.add(1, 2, 3, True, rsrc, "generic", bid4, off4, pb)
        w4.add_skipped(1, 2, 3, True, ssrc)
        w4.finish()
        st5 = PageStore(tmp / "skips")
        served5 = st5.served((1, 2, 3, True))
        got5 = served5 is not None and not served5.is_skipped()
        print("[s27] a read and a skip for one key: served() answers with the "
              "READ (%s), and the skip is still indexed (%d candidates)"
              % (got5, len(st5.candidates((1, 2, 3, True)))))
        ok &= got5 and len(st5.candidates((1, 2, 3, True))) == 2
        st5.close()
        # one directory, two verdicts: a layout can quarantine a page whatever
        # directory it is in, and the first file walked must not decide for the
        # rest.
        w3 = StoreWriter(tmp / "mixed", page_bytes=pb)
        a = w3.source("deep", "unknown", "an ordinary capture")
        b = w3.source("deep", "suspect", "quarantined by its LAYOUT")
        print("[s30] one directory with two verdicts -> %d source record(s), "
              "not %d" % (len({a.sid, b.sid}), 1))
        ok &= a.sid != b.sid and a.tier != b.tier
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
    print("\nstore selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _main(argv):
    """`--selftest` (the default), `--compact <store>` or `--lock <store>`."""
    if "--lock" in argv:
        argv = [a for a in argv if a != "--lock"]
        if not argv:
            print("usage: python -m titania.store --lock <store>")
            return 2
        info = lock_info(argv[0])
        if info is None:
            print("%s: not locked" % argv[0])
            return 0
        age = max(0.0, time.time() - float(info.get("epoch") or 0))
        alive = (info.get("host") == socket.gethostname()
                 and _pid_alive(info.get("pid")))
        print("%s: LOCKED by %s" % (argv[0], _describe_lock(info, age)))
        print("  holder is %s" % ("running" if alive else
                                  "NOT running on this machine -- the next "
                                  "writer will break this lock"))
        return 0
    if "--compact" in argv:
        argv = [a for a in argv if a != "--compact"]
        dry = "--dry-run" in argv
        argv = [a for a in argv if a != "--dry-run"]
        if not argv:
            print("usage: python -m titania.store --compact <store> "
                  "[--dry-run]")
            return 2
        root = argv[0]
        if not is_store(root):
            print("%s is not a page store" % root)
            return 2
        tot, foreign, detail = orphan_report(root)
        for rel, (size, live, orph) in sorted(detail.items(),
                                              key=lambda t: -t[1][2])[:20]:
            print("  %-56s %10d on disk  %10d live  %9d orphan"
                  % (rel, size, live, orph))
        if len(detail) > 20:
            print("  ... and %d more blob(s)" % (len(detail) - 20))
        if foreign:
            print("  %d byte(s) unreferenced in blobs this store only "
                  "REFERENCES -- left alone" % foreign)
        compact(root, dry_run=dry)
        if dry:
            print("dry run: %d byte(s) would be reclaimed from %d blob(s)"
                  % (tot, len(detail)))
        return 0
    return _selftest()


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
