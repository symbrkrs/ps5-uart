"""A pool of worker threads, a keyed coalescing queue, and a non-blocking drain.

This is still the only concurrency primitive in nandgui, and it is still the
only place that starts a thread -- but it is no longer one thread, and the
reasoning the single-thread version rested on has to be replaced rather than
patched.  What follows is that replacement.

Why a pool at all, said honestly.  It is for concurrency, not throughput, and
both halves of that were measured through this pool (64-core machine, E:
stores, offline, scratchpad/pool_ecc_bench.py and scratchpad/pool_latency.py).

Throughput: there is none to speak of.  41 programmed TLC pages of
ce7/block3596, one key per page so nothing coalesces, 3 runs each:

    workers      best s    mean s    best-speedup    mean-speedup   verdicts
        1          3.01      3.23           1.00x           1.00x   (base)
        2          2.69      3.18           1.12x           1.02x   identical
        4          2.48      3.05           1.21x           1.06x   identical
        8          2.62      2.98           1.15x           1.08x   identical
       16          2.89      3.27           1.04x           0.99x   identical

Read the spread, not the best column: individual runs at 4 workers ranged
2.48-3.81 s while 1 worker ranged 3.01-3.58 s, so the configurations overlap
and the honest summary is "no measurable throughput gain from threads".  That
is expected -- roughly half of a mean ECC page is behind nd_ldpc_rs's one
persistent Rust subprocess (serialised by pagefmt._ECC_LOCK, see below) and
most of the rest is GIL-bound: titania_nand.apply_scrambler is a pure-Python
`bytes(a ^ b for a, b in zip(...))` over 4104 bytes x 4 AUs per page.  Real ECC
throughput needs processes, not threads; that is a separate change and this
file does not pretend to deliver it.  Every configuration returned identical
verdicts for all 41 pages, which is the part that had to be true.

Concurrency: this is the whole return on the change, and it is large.  Two
background 12-page ECC scans in flight (what "keep measuring" submits), then
one interactive stripe probe -- time from submit to Result, 5 trials:

    workers     min ms   median ms   max ms
        1        482.0       497.5    524.4    waits out a whole scan
        2        981.0      1023.7   1054.6    worst: both workers are inside
                                               a scan and the two scans
                                               contend, so each takes twice as
                                               long and the click waits for one
        4        126.2       131.2    138.8    3.8x better than 1 worker
        8        131.3       141.2    147.5

So the requirement is not "more workers is better" -- it is that N must exceed
the number of background jobs that can be in flight at once.  At N equal to
that number the pool is worse than a single thread, because every worker is
busy and they contend.  app.py has exactly two self-limiting background
submitters (blockview's stripe probe, and _ecc_sweep_step which is gated on
ecc_busy), so N >= 4 is what makes the click land in 131 ms instead of 497 ms.

And the counter-case, recorded because it is real: under a load the app never
generates -- 3 ECC decodes per worker, so the background work scales with N --
the interactive probe goes the other way, 140 ms at 1 worker to 327 ms at 8,
because it then shares the GIL with N-1 decoders instead of running alone.
More workers is not free; it is right for this load.

Four guarantees, and the specific race each one prevents.  All four were free
with one thread; none of them is free with N.

1.  Coalescing by key.  `_jobs[key]` is the truth about "what is this key's
    newest job"; heap entries are lazy tombstones.  Dragging the block slider
    for 100 frames leaves one decode queued, not 100.  Unchanged under N: a
    replaced submit leaves a stale uid behind, whichever uid pops first runs
    the newest fn, and the leftover finds an empty dict slot and is skipped.

2.  Priority, stated precisely because it is weaker than it looks.  One shared
    heap ordered by (-priority, seq), so equal priorities stay FIFO and no
    worker ever *starts* a background job while an interactive job is queued.
    It is not true that an interactive job never waits: jobs are never
    preempted, so if all N workers are already inside background jobs it waits
    for the first to finish -- and that wait is the whole latency, measured at
    497 ms (1 worker) and 1024 ms (2 workers) in the table above.  What keeps
    it to 131 ms is having a spare worker, not the priority: the only
    background submitters are the stripe probe (1 job) and the ECC sweep (1
    block, gated on ecc_busy), so at most 2 background jobs are ever in flight
    and N >= 4 always leaves one free.  If a third background submitter is ever
    added, this stops being true at N=4 and the default has to move with it;
    the fix after that is one reserved interactive-only worker, not a
    preemption scheme.

3.  Result order per key -- the drop gate, and the reason this file exists in
    this shape.  A newer job for a key that is already running is queued
    normally and may run concurrently with the older one on another worker.
    It is not dropped and it does not wait: making an interactive re-select
    queue behind the 215 ms decode it supersedes is exactly the stall the pool
    is here to delete.  The jobs are pure reads and each worker owns its own
    ctx, so concurrent same-key execution is safe -- the only thing it breaks
    is result order, and `_delivered` closes that:

        worker a starts (page,) seq=7, an 86 ms uncorrectable page.  The user
        moves.  Worker B starts (page,) seq=8, a 4 ms clean page, and finishes
        first.  The inspector paints seq 8.  Then a appends seq 7 and the
        panel shows PAGE 7's bytes under PAGE 8's address.

    So a Result whose seq is older than the newest already appended for that
    key is discarded here, before poll() can ever see it.  The views' own
    `result.seq < state.seq` tests stay as an independent second gate, but the
    gate here is load-bearing for the keys that have no view gate at all:
    app._dispatch acts on ("ecc-scan",) and ("stores-status",) unconditionally,
    with no seq comparison anywhere.  Two overlapping ECC scans would otherwise
    let the older one land last, merge stale BlockEcc entries and clear
    ecc_busy at the wrong moment.  That hole is created by the pool and closed
    here.

4.  One ctx per worker.  `setup` runs on every worker thread and each keeps
    its own return value; teardown runs N times, each on the thread that
    opened its own.  Sharing one Stores is not "undocumented", it is provably
    wrong -- titania/store.py:1145:

        def read_at(self, blob_id, offset, length):
            h = self._handle(blob_id)   # mutates the _open dict and _order LRU
            h.seek(offset)              # <-- shared file object
            data = h.read(length)

    Two threads on one device interleave seek/read and thread a gets thread
    B's page bytes, at the right length, with no exception: the same "older
    page bytes under a newer address" lie as (3), one layer lower and
    undetectable.  `_handle`'s LRU can also close a handle another thread is
    mid-read on, and DumpDevice mmaps index.tix per device.

What the pool costs elsewhere, recorded here because it is not visible from
the call sites:

* `model.get_codec` caches the codec on the ctx, so there are now N codecs and
  N private `lru_cache(4096)` keystream caches, each about 1/N as warm across a
  stripe of the same rows.  Correct, just less cached.
* `titania.titania_nand._KS_CACHE` is a module-level 4-entry dict shared by all
  workers.  Its values are immutable `bytes` and every mutation is a single
  atomic dict op, so the worst case is a redundant recompute, not corruption.
* `pagefmt` must serialise `nd_deepparity.decode_page`.  Without that the pool
  does not merely go slow -- MEASURED, 4 threads on the shared unlocked Rust
  pipe hung for over 10 minutes and had to be killed -- and if it did not hang,
  two threads reading each other's 6-byte headers would assemble a page out of
  another page's corrected codewords with converged=True, which then fails the
  MCRC and reports MCRC_DISAGREES: the one state this project documents as
  "never observed; treat as a bug".  A concurrency bug that manufactures the
  red-flag state is the worst failure available here, so the lock is mandatory.

A job that raises is caught and returned as Result.error.  No worker thread
ever dies and no exception ever crosses into imgui.  If `setup` fails on any
worker (an unreachable store, a mmapped index someone else is writing) the
threads stay alive and every job on every worker returns that sentence, so the
UI can say what is missing instead of silently showing empty views.
"""

from __future__ import annotations

import dataclasses
import heapq
import itertools
import os
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Hashable

__all__ = [
    "Result",
    "Worker",
    "JOB_MAP_SUMMARY",
    "JOB_BLOCK_STRIPE",
    "JOB_PAGE",
    "JOB_DECODE",
    "PRIORITY_INTERACTIVE",
    "PRIORITY_BACKGROUND",
    "DEFAULT_MAX_WORKERS",
    "WORKERS_ENV",
    "default_workers",
]

# Key prefixes, so two views can never collide in the coalescing dict. A key
# is normally a tuple starting with one of these, e.g. (JOB_PAGE,).
JOB_MAP_SUMMARY = "map"
JOB_BLOCK_STRIPE = "stripe"
JOB_PAGE = "page"
JOB_DECODE = "decode"

# The page inspector is what the user is staring at; the stripe prefetch is
# speculative and must yield to it.
PRIORITY_INTERACTIVE = 10
PRIORITY_BACKGROUND = 0

#: Env override for the worker count, so a measurement can name the number that
#: produced it without editing code.
WORKERS_ENV = "NANDGUI_WORKERS"

#: Not os.cpu_count().  This machine has 64 cores and MEASURED, thread count
#: buys no ECC throughput at any setting (1.00x / 1.02x / 1.06x / 1.08x / 0.99x
#: mean at 1/2/4/8/16, all within the run-to-run spread) -- the work is behind
#: one Rust pipe and the GIL, not behind the core count.  64 ctxs would also
#: mean 64 mmapped index copies and 64 blob-handle LRUs.
#:
#: 8 is a concurrency budget: enough spare workers that an interactive job
#: never waits out a background one.  Measured, interactive latency under the
#: app's own load: 497 ms at 1 worker, 1024 ms at 2 (the pathological case --
#: workers == background jobs), 131 ms at 4, 141 ms at 8.  Anything from 4 up
#: is flat, so 8 is chosen for headroom against a third background submitter
#: rather than because 8 measured better than 4.
DEFAULT_MAX_WORKERS = 8


def default_workers(requested: int | None = None) -> int:
    """-> how many worker threads to run.

    Precedence: an explicit argument wins, then $NANDGUI_WORKERS, then
    min(DEFAULT_MAX_WORKERS, os.cpu_count()).  Explicit beats ambient because a
    `--workers 4` typed on the command line of a machine whose environment
    happens to carry the variable must mean 4, and because a benchmark that
    cannot pin the number cannot report one.

    Never raises and never returns < 1: a malformed env value is ignored rather
    than being allowed to stop the viewer from starting.
    """
    if requested is None:
        raw = (os.environ.get(WORKERS_ENV) or "").strip()
        if raw:
            try:
                requested = int(raw)
            except ValueError:
                requested = None
    if requested is None:
        requested = min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1)
    return max(1, int(requested))


@dataclasses.dataclass(frozen=True)
class Result:
    key: Hashable            # the caller's coalescing key
    seq: int                 # monotonic; views drop results older than theirs
    value: object | None
    error: str | None        # formatted exception text; the worker never dies
    seconds: float


@dataclasses.dataclass
class _Broadcast:
    """Bookkeeping for one job pinned to every worker.

    N executions, one Result: the callers of broadcast() are changing state
    that every ctx has a copy of (reopening the decoded store, say), and N
    identical Results would make app._dispatch act N times on one user action.
    """

    key: Hashable
    seq: int
    remaining: int
    values: list
    errors: list
    started: float


@dataclasses.dataclass
class _Job:
    key: Hashable
    seq: int
    priority: int
    fn: Callable[[object], object]
    #: set only for broadcast jobs; a normal job posts its own Result
    bcast: _Broadcast | None = None


def _format_error(exc: BaseException) -> str:
    """One line for the status bar, plus the traceback tail for the log.

    The status bar has room for a sentence, not a traceback, but a bare
    repr() of a DeviceError loses which page it was about. Keep the type and
    message first so a truncated display still names the failure.
    """
    head = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    tb = "".join(traceback.format_exception(exc)).rstrip()
    return head if tb == head else f"{head}\n{tb}"


class Worker:
    """N worker threads sharing one keyed priority queue.

    `setup` runs once per worker, on that worker's own thread, before any job
    it runs, and its return value (that worker's ctx) is passed to every job
    that worker executes.  Teardown is discovered from the ctx or from the
    setup callable, in this order, first hit wins:

        setup.teardown(ctx)   -- e.g. `setup.teardown = model.close_stores`
        ctx.teardown()
        ctx.close()

    and runs once per ctx, on the thread that opened it.  That keeps worker.py
    free of any import from model.py (no cycles, and the worker stays
    unit-testable with a dummy ctx), while still guaranteeing that
    close_stores() runs on the thread that owns each mmapped index.

    A job must not mutate its ctx unless it goes through `broadcast()`.  A
    submit() lands on exactly one worker, so a job that reopens a store fixes
    one of N ctxs and leaves the other N-1 serving the old tier with nothing on
    screen saying so.  This is not hypothetical -- it is what
    app._set_include_suspect does -- and broadcast() exists for it.
    """

    def __init__(self, name: str = "nandgui",
                 setup: Callable[[], object] | None = None,
                 workers: int | None = None) -> None:
        self._name = name
        self._setup = setup
        self._n = default_workers(workers)

        # One lock guards the queue, the coalescing dict, the per-worker pinned
        # deques, the result deque and the small status fields. Hold times are
        # microseconds, so poll() from frame() never actually waits on a job.
        self._cv = threading.Condition(threading.Lock())
        self._heap: list[tuple[int, int, int]] = []   # (-priority, seq, uid)
        self._jobs: dict[Hashable, _Job] = {}         # key -> newest job only
        self._uid_of_key: dict[int, Hashable] = {}    # heap uid -> key
        self._uids = itertools.count(1)
        self._seq = itertools.count(1)

        # One deque per worker slot, drained before the shared heap. Only
        # broadcast() writes here: a pinned job is addressed to one specific
        # thread's ctx, so it cannot go through the shared queue at all.
        self._pinned: list[deque[_Job]] = [deque() for _ in range(self._n)]

        self._results: deque[Result] = deque()
        #: worker slot -> the job it is inside right now. A dict rather than
        #: the old scalar _running_key, and keyed by SLOT rather than by key,
        #: because two workers may legitimately be inside the same key at once
        #: (see guarantee 3) and a key-keyed dict would lose one of them.
        self._running: dict[int, _Job] = {}
        #: key -> the highest seq already appended to _results. The drop gate.
        #: Bounded by the number of distinct coalescing keys, which is a small
        #: fixed set by construction -- a key space that grew without bound
        #: would already have defeated coalescing, which is the whole point of
        #: a key.
        self._delivered: dict[Hashable, int] = {}
        #: how many stale results the gate discarded. Not cosmetic: it is the
        #: count of times the pool did overtake itself, i.e. how often the old
        #: single-thread assumption would have painted the wrong page.
        self._stale = 0
        self._ctxs: dict[int, object] = {}            # slot -> ctx, for stats
        self._last_error: str | None = None
        self._setup_error: str | None = None
        self._stop = False
        self._started = False
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ------------------------------------------------------
    @property
    def workers(self) -> int:
        """How many threads this pool runs (and therefore how many ctxs)."""
        return self._n

    def start(self) -> None:
        with self._cv:
            if self._started:
                return
            self._started = True
            self._stop = False
            self._threads = [
                threading.Thread(target=self._run, args=(i,),
                                 name=f"{self._name}-{i}", daemon=True)
                for i in range(self._n)
            ]
            threads = list(self._threads)
        # Start outside the lock so the threads can immediately take it.
        for thread in threads:
            thread.start()

    def shutdown(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop after the running jobs, tear every ctx down on its own thread,
        join them all.

        Never close a DumpDevice from the UI thread: its index is mmapped and
        the only safe closer is its owner. app.py calls gltex.destroy_all()
        first (GL context still current) and this second.

        `timeout` is the TOTAL budget, not per thread: the joins share one
        deadline, so shutdown(timeout=5) means 5 s for the whole pool rather
        than 5 s x N while a window waits to close.
        """
        with self._cv:
            if not self._started:
                self._stop = True
                return
            self._stop = True
            # Queued work is abandoned; a pending stripe probe has no value
            # once the window is closing. Pinned (broadcast) jobs go too --
            # _next_job checks _stop before draining them, so they would never
            # run anyway, and holding the references would keep a dead ctx's
            # closure alive past teardown.
            self._heap.clear()
            self._jobs.clear()
            self._uid_of_key.clear()
            for pinned in self._pinned:
                pinned.clear()
            self._cv.notify_all()
            threads = list(self._threads)
        if not wait:
            return
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            if not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))

    # ---- submission -----------------------------------------------------
    def submit(self, key: Hashable, fn: Callable[[object], object],
               priority: int = 0) -> int:
        """Queue fn(ctx) on one worker, replacing any pending job with the
        same key.

        The returned seq is what the caller compares its own state against.  A
        job whose key is already running is not held back: it is queued and may
        overlap, and the drop gate in _execute is what keeps the older of the
        two from landing last.
        """
        with self._cv:
            if self._stop:
                # Post-shutdown submits are dropped; -1 can never match a real
                # seq, so a view waiting on it simply keeps its old data.
                return -1
            seq = next(self._seq)
            uid = next(self._uids)
            # Replacement is lazy: the old heap entry stays but its uid no
            # longer maps to this key's current job, so the runner skips it.
            self._jobs[key] = _Job(key=key, seq=seq, priority=priority, fn=fn)
            self._uid_of_key[uid] = key
            # seq as the heap tiebreaker makes equal priorities FIFO.
            heapq.heappush(self._heap, (-priority, seq, uid))
            self._cv.notify()
            return seq

    def post_external(self, key: Hashable, value: object | None = None,
                      error: str | None = None, seconds: float = 0.0) -> int:
        """Deliver one Result from a thread that is NOT in the pool.  -> seq.

        For work that must not occupy a pool slot.  The sidecar rebuilds take
        MINUTES (a summary build measured 64 s, an msoDescriptor derivation 48 s)
        and a worker sitting inside one delays every `broadcast` queued behind
        it -- which is exactly how an `--include-suspect` toggle ends up applied
        to N-1 of N ctxs while the last one keeps serving the old store, with
        nothing on screen saying so.  Such a job runs on its own thread, opens
        whatever it needs itself, and hands the answer back through here, so the
        UI thread still receives exactly one Result on the ordinary poll() path
        and `_dispatch` needs no second delivery mechanism.

        Goes through the same drop gate as everything else, so a stale external
        Result cannot overtake a newer one for the same key.
        """
        with self._cv:
            if self._stop:
                return -1
            seq = next(self._seq)
            self._post(key, seq, value, error, float(seconds))
            self._cv.notify_all()
        return seq

    def broadcast(self, key: Hashable, fn: Callable[[object], object],
                  priority: int = PRIORITY_INTERACTIVE) -> int:
        """Run fn(ctx) once on every worker, and post one Result when all are
        done.

        This is the only correct way to change something that lives in the
        ctx.  With one thread `submit` was enough; with N, a submitted
        `model.reopen_decoded` reopens the decoded store on exactly one worker
        and the other N-1 keep serving the old tier -- two CEs of the same
        stripe can then be probed against different stores, and nothing on
        screen says so.

        The Result carries the last worker's return value (the callers return
        a description of the new state, which is by construction the same on
        every ctx) and, if any worker raised, every error joined; a broadcast
        that half-failed must not read as a success.

        Pinned jobs are drained ahead of the shared heap on each worker, so a
        broadcast overtakes queued reads rather than waiting behind them --
        those reads would otherwise be answered from the ctx it is replacing.
        """
        if not self._started:
            # A state change must not be silently dropped just because it
            # arrived before start(); starting here is cheaper than making
            # every caller order the two.
            self.start()
        with self._cv:
            if self._stop:
                return -1
            seq = next(self._seq)
            n = len(self._threads)
            if n <= 0:
                return -1
            bcast = _Broadcast(key=key, seq=seq, remaining=n, values=[],
                               errors=[], started=time.perf_counter())
            for slot in range(n):
                self._pinned[slot].append(
                    _Job(key=key, seq=seq, priority=priority, fn=fn,
                         bcast=bcast))
            # notify_all, not notify: this has to reach every worker, not one.
            self._cv.notify_all()
            return seq

    def cancel(self, key: Hashable) -> None:
        with self._cv:
            self._jobs.pop(key, None)

    def cancel_all(self) -> None:
        """Drop every queued read.  Pinned broadcast jobs are
        left alone: they are state changes, not speculative reads, and
        cancelling half of one would leave the workers disagreeing about what
        is mounted."""
        with self._cv:
            self._heap.clear()
            self._jobs.clear()
            self._uid_of_key.clear()

    # ---- draining -------------------------------------------------------
    def poll(self, max_results: int = 16) -> list[Result]:
        """Non-blocking drain, once per frame from the UI thread.

        Bounded so a burst of finished jobs cannot turn one frame into a
        stall; whatever is left is picked up next frame.
        """
        out: list[Result] = []
        with self._cv:
            while self._results and len(out) < max_results:
                out.append(self._results.popleft())
        return out

    def pending(self) -> int:
        """Jobs queued and not yet started (pinned broadcasts included)."""
        with self._cv:
            return len(self._jobs) + sum(len(p) for p in self._pinned)

    def running(self) -> int:
        """How many workers are inside a job right now."""
        with self._cv:
            return len(self._running)

    def busy(self) -> bool:
        with self._cv:
            return bool(self._running)

    def stale_dropped(self) -> int:
        """Results the drop gate discarded because a newer one for the same key
        had already been delivered.  Nonzero means the pool overtook itself and
        the gate did its job."""
        with self._cv:
            return int(self._stale)

    def stats(self) -> dict:
        """One snapshot for the status bar and the bench, taken under one lock
        so the numbers in it are consistent with each other."""
        with self._cv:
            return {
                "workers": self._n,
                "live": len(self._threads),
                "ctxs": len(self._ctxs),
                "pending": len(self._jobs) + sum(len(p) for p in self._pinned),
                "running": len(self._running),
                "results": len(self._results),
                "stale_dropped": int(self._stale),
                "setup_error": self._setup_error,
            }

    def last_error(self) -> str | None:
        with self._cv:
            return self._setup_error or self._last_error

    # ---- worker threads -------------------------------------------------
    def _run(self, slot: int) -> None:
        """One worker.  Opens its own ctx, serves jobs, closes its own ctx."""
        ctx: object | None = None
        if self._setup is not None:
            try:
                ctx = self._setup()
            except BaseException as exc:      # noqa: BLE001 - never die here
                msg = _format_error(exc)
                with self._cv:
                    # A setup failure is a store-level fact -- an unreachable
                    # path, a store being written -- so it is the same for
                    # every worker and the first sentence is kept rather than
                    # having N copies of it race for the status bar.
                    if self._setup_error is None:
                        self._setup_error = f"worker setup failed: {msg}"
                ctx = None
        with self._cv:
            self._ctxs[slot] = ctx
        try:
            while True:
                job = self._next_job(slot)
                if job is None:
                    return                      # stop requested
                self._execute(job, ctx, slot)
        finally:
            with self._cv:
                self._ctxs.pop(slot, None)
            self._teardown(ctx)

    def _next_job(self, slot: int) -> _Job | None:
        """Block until a live job is available, or None when stopping."""
        with self._cv:
            while True:
                if self._stop:
                    return None
                pinned = self._pinned[slot]
                if pinned:
                    # Broadcasts first: they replace what the queued reads
                    # below would be answered from.
                    job = pinned.popleft()
                    self._running[slot] = job
                    return job
                while self._heap:
                    _, _, uid = heapq.heappop(self._heap)
                    key = self._uid_of_key.pop(uid, None)
                    if key is None:
                        continue
                    # The dict, not the heap entry, is the truth for "what is
                    # this key's newest job". A replaced submit leaves a stale
                    # heap entry behind; whichever entry is popped first runs
                    # the newest fn once, and the leftover entry then finds an
                    # empty dict slot and is skipped. That is the coalescing
                    # guarantee: 100 dragged frames -> one decode.
                    job = self._jobs.pop(key, None)
                    if job is None:
                        continue                # cancelled or already run
                    self._running[slot] = job
                    return job
                self._cv.wait()

    def _execute(self, job: _Job, ctx: object | None, slot: int) -> None:
        started = time.perf_counter()
        value: object | None = None
        error: str | None = None
        with self._cv:
            setup_error = self._setup_error
        if setup_error is not None:
            # Fail fast with the setup sentence rather than calling a job with
            # ctx=None and letting it raise AttributeError somewhere deeper.
            error = setup_error
        else:
            try:
                value = job.fn(ctx)
            except BaseException as exc:       # noqa: BLE001 - never die here
                error = _format_error(exc)
        seconds = time.perf_counter() - started
        with self._cv:
            self._running.pop(slot, None)
            if error is not None:
                self._last_error = error
            if job.bcast is not None:
                self._finish_broadcast(job.bcast, value, error)
            else:
                self._post(job.key, job.seq, value, error, seconds)

    def _post(self, key: Hashable, seq: int, value: object | None,
              error: str | None, seconds: float) -> None:
        """Append one Result -- unless a newer one for this key already went.

        Called with self._cv held.  This is the drop gate; see guarantee 3 in
        the module docstring.  Dropping is the whole answer: there is nothing
        useful to do with an out-of-date read, and handing it to the views and
        hoping they all compare seq is how a page's bytes end up under another
        page's address.
        """
        if seq < self._delivered.get(key, 0):
            self._stale += 1
            return
        self._delivered[key] = seq
        self._results.append(Result(key=key, seq=seq, value=value,
                                    error=error, seconds=seconds))

    def _finish_broadcast(self, bcast: _Broadcast, value: object | None,
                          error: str | None) -> None:
        """One worker finished its copy; the last one posts the single Result.

        Called with self._cv held.
        """
        bcast.values.append(value)
        if error is not None:
            bcast.errors.append(error)
        bcast.remaining -= 1
        if bcast.remaining > 0:
            return
        joined = "\n".join(bcast.errors) if bcast.errors else None
        if bcast.errors and len(bcast.errors) < len(bcast.values):
            # Half a broadcast succeeding is worse than none of it: the ctxs
            # now disagree, and that has to be said rather than averaged away.
            joined = ("broadcast %r failed on %d of %d workers, so the workers "
                      "no longer agree:\n%s"
                      % (bcast.key, len(bcast.errors), len(bcast.values),
                         joined))
        self._post(bcast.key, bcast.seq,
                   bcast.values[-1] if bcast.values else None,
                   joined, time.perf_counter() - bcast.started)

    def _teardown(self, ctx: object | None) -> None:
        """close_stores() must run here, on the thread that opened them.

        Once per ctx, so with N workers this runs N times -- which is the
        point: there are N DumpDevice pairs and N mmapped index copies, and
        each one has exactly one thread allowed to close it.
        """
        hook = getattr(self._setup, "teardown", None)
        try:
            if callable(hook):
                hook(ctx)
                return
            if ctx is None:
                return
            for name in ("teardown", "close"):
                fn = getattr(ctx, name, None)
                if callable(fn):
                    fn()
                    return
        except BaseException as exc:           # noqa: BLE001 - never die here
            with self._cv:
                self._last_error = f"worker teardown failed: {_format_error(exc)}"
