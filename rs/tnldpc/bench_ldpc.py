#!/usr/bin/env python3
"""bench_ldpc.py -- a reproducible timing + exactness harness for rs/tnldpc.

    py rs/tnldpc/bench_ldpc.py            # rust, 3 reps
    py rs/tnldpc/bench_ldpc.py --reps 5
    py rs/tnldpc/bench_ldpc.py --numpy    # the reference, for A/B

The selftest is a correctness check on 32 mostly-trivial decodes; it is far too
small and too noisy to optimise against.  This builds a FIXED workload out of
real codewords with a fixed RNG seed, so the same bits go in on every run, and
prints:

  * per-error-level single-decode wall time (op 1, one job, no threading),
  * batch wall time (op 2, count=4, the shape `decode_page` actually uses),
  * a DIGEST over every (vector, iters, converged, stage) in the workload.

The digest is the point: any change to the kernel that moves the digest has
changed an answer, and adjudication compares iteration counts, so that is a
regression even when every codeword still decodes.
"""
import argparse
import hashlib
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))

LEVELS = (0, 8, 32, 96, 128, 160, 176)
PER_LEVEL = 4


def workload(seed=11):
    """[(label, v0)] -- deterministic, real codewords, fixed injected errors."""
    import nd_ldpc_fix as F
    pool = F.clean_blocks(limit=8)
    if not pool:
        raise SystemExit("no clean codewords in kp_dump/img")
    free_pos = np.flatnonzero(F.FREE)
    rng = np.random.default_rng(seed)
    out = []
    for li, N in enumerate(LEVELS):
        for t in range(PER_LEVEL):
            _nm, blk = pool[(li + t) % len(pool)]
            v = F.block_to_mother(blk)
            if N:
                v[rng.choice(free_pos, size=N, replace=False)] ^= 1
            out.append((N, v))
    return out


def digest(results):
    h = hashlib.sha256()
    for v, it, ok, st in results:
        h.update(np.asarray(v, np.uint8).tobytes())
        h.update(b"%d|%d|%s;" % (it, int(bool(ok)), st.encode()))
    return h.hexdigest()[:24]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--numpy", action="store_true",
                    help="time the numpy reference instead")
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.numpy:
        os.environ["TITANIA_NO_RUST"] = "1"
    import nd_ldpc_fix as F
    W = workload()
    if a.numpy:
        def one(v):
            return F.decode_mother_numpy(v.copy())
        many = None
    else:
        import nd_ldpc_rs as R
        if not R.available():
            raise SystemExit("rust worker unavailable: %s" % R.why_unavailable())

        def one(v):
            return R.decode_mother(v.copy())
        many = R.decode_many

    # ---- warm-up (page-in the binary, JIT nothing, but settle the caches) ---
    one(W[0][1])

    per_level = {N: [] for N in LEVELS}
    totals = []
    dig = None
    for rep in range(a.reps):
        res = []
        lvl = {N: 0.0 for N in LEVELS}
        t_all = time.perf_counter()
        for N, v in W:
            t0 = time.perf_counter()
            r = one(v)
            dt = time.perf_counter() - t0
            lvl[N] += dt
            res.append(r)
        t_all = time.perf_counter() - t_all
        totals.append(t_all)
        for N in LEVELS:
            per_level[N].append(lvl[N])
        d = digest(res)
        if dig is None:
            dig = d
        elif d != dig:
            raise SystemExit("NON-DETERMINISTIC: digest changed between reps")

    if not a.quiet:
        print("serial (op 1), %d reps, %d vectors" % (a.reps, len(W)))
        for N in LEVELS:
            xs = per_level[N]
            print("  %3d err  %8.1f ms  (min %8.1f)"
                  % (N, 1000 * statistics.mean(xs) / PER_LEVEL,
                     1000 * min(xs) / PER_LEVEL))
    sd = statistics.pstdev(totals) if len(totals) > 1 else 0.0
    print("  TOTAL    %8.3f s  min %.3f  sd %.3f" %
          (statistics.mean(totals), min(totals), sd))

    if many is not None:
        vs = [v for _N, v in W]
        bt = []
        bdig = None
        for rep in range(a.reps):
            res = []
            t0 = time.perf_counter()
            for i in range(0, len(vs), a.batch):
                res += many([x.copy() for x in vs[i:i + a.batch]])
            bt.append(time.perf_counter() - t0)
            d = digest(res)
            if bdig is None:
                bdig = d
        print("  BATCH%-2d  %8.3f s  min %.3f  sd %.3f" %
              (a.batch, statistics.mean(bt), min(bt),
               statistics.pstdev(bt) if len(bt) > 1 else 0.0))
        if bdig != dig:
            raise SystemExit("BATCH DIGEST DIFFERS FROM SERIAL: %s vs %s"
                             % (bdig, dig))
    print("  DIGEST   %s" % dig)
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    finally:
        # An orphaned worker holds target/release/tnldpc.exe open and the next
        # `cargo build` then cannot replace it.  Shut it down explicitly; the
        # atexit hook is not enough when the interpreter is torn down abruptly.
        try:
            import nd_ldpc_rs
            nd_ldpc_rs.close()
        except Exception:                                   # noqa: BLE001
            pass
    sys.exit(rc)
