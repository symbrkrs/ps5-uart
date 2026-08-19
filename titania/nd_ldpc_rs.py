#!/usr/bin/env python3
"""nd_ldpc_rs.py -- the Rust EE3 corrector, driven from Python.

    py nd_ldpc_rs.py --build        # cargo build --release, once
    py nd_ldpc_rs.py --selftest     # Rust vs Python on real codewords

WHY
---
`DumpDevice.adjudicate()` decides a contested page on content, and the strongest
content evidence is the LDPC correction count -- so it decodes 8 codewords per
candidate.  Measured on the store: 62.3 s for 19 conflicts, of which 55.7 s
was `nd_ldpc_fix.Decoder.minsum` and 8.9 s the syndrome.  That is arithmetic,
not I/O and not interpreter overhead: 137 760 edges x up to 80 iterations, with
numpy allocating a full (41, 70, 48) temporary for each of `partition`, `abs`,
`argmin`, `where` and `prod` on every single iteration.  Micro-optimising the
numpy would move the constant; it would not move the shape.

WHAT THIS IS NOT
----------------
It is not a second decoder.  `rs/tnldpc/src/main.rs` reproduces
`nd_ldpc_fix.py` operation for operation, in the same precision and the same
order, because adjudication compares correction counts between two candidate
reads -- a decoder that reached the same codeword by a different path and
reported a different iteration count would change verdicts.  The two places
where that is delicate are documented in the Rust and re-checked by
`--selftest`, which runs both implementations over real codewords with injected
errors and requires the corrected bytes, the flip count, the stage and the
iteration count to agree exactly.

IT IS OPTIONAL
--------------
No Rust toolchain, no binary, `TITANIA_NO_RUST=1` in the environment, or a
worker that dies -> `nd_ldpc_fix` falls back to the numpy decoder and every
answer is the same, only slower.  A dump must stay decodable on a checkout with
nothing but Python and numpy.

TRANSPORT
---------
One persistent worker over a pipe, not a process per call: a decode is ~1 ms
and a process spawn on this share is ~20 ms, so per-call spawning would cost
more than the decoder does.  The code description (H as the same `idx`/`lens`
the numpy decoder uses) is handed over once at start-up, so there is no second
copy of the parity-check matrix to drift.
"""
import atexit
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
CRATE = HERE / "rs" / "tnldpc"
MAGIC = b"TNLD"

_STATE = {"proc": None, "why": None, "tried": False}
STAGES = {0: "clean", 1: "A", 2: "B", 3: "fail"}


def binary_path():
    """The worker binary.  `TNLDPC_BIN` overrides it.

    The override exists so a build under test can be benchmarked without
    replacing the binary everyone else is running: on Windows the live one is
    locked while it runs, and a half-swapped binary under a selftest reads as a
    regression in whatever that selftest was deciding.  Unset -- which is the
    normal case -- this is exactly the old path.
    """
    override = os.environ.get("TNLDPC_BIN")
    if override:
        return Path(override)
    exe = "tnldpc.exe" if os.name == "nt" else "tnldpc"
    return CRATE / "target" / "release" / exe


def build(verbose=True):
    """`cargo build --release`, offline.  -> the binary path, or None."""
    if not CRATE.exists():
        return None
    cmd = ["cargo", "build", "--release", "--offline",
           "--manifest-path", str(CRATE / "Cargo.toml")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        if verbose:
            print("cargo not runnable: %s" % exc)
        return None
    if r.returncode != 0:
        if verbose:
            print(r.stdout[-2000:])
            print(r.stderr[-2000:])
        return None
    p = binary_path()
    return p if p.exists() else None


def _spawn():
    """Start the worker and hand it the code.  -> Popen, or None with a reason."""
    if os.environ.get("TITANIA_NO_RUST"):
        _STATE["why"] = "TITANIA_NO_RUST is set"
        return None
    exe = binary_path()
    if not exe.exists():
        if os.environ.get("TNLDPC_BIN"):
            # An explicit override that is not there is a mistake to report,
            # not a reason to `cargo build` -- that would write the shared
            # binary, which is the exact thing the override exists to avoid.
            _STATE["why"] = "TNLDPC_BIN=%s does not exist" % exe
            return None
        exe = build(verbose=False)
    if exe is None or not Path(exe).exists():
        _STATE["why"] = "no %s and cargo could not build it" % binary_path()
        return None
    from . import nd_ldpc_fix as F
    D = F.decoder()
    C = D.code
    idx = np.ascontiguousarray(C.idx, np.int32)
    lens = np.array([len(L) for L in C.layers], np.uint32)
    free = np.ascontiguousarray(D.free.astype(np.uint8))
    try:
        p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE)
        p.stdin.write(MAGIC + struct.pack("<5I", C.Z, C.Mb, C.Nb,
                                          idx.shape[0], D.n))
        p.stdin.write(idx.tobytes())
        p.stdin.write(lens.tobytes())
        p.stdin.write(free.tobytes())
        p.stdin.flush()
        if p.stdout.read(1) != b"K":
            _STATE["why"] = "the worker did not acknowledge the code"
            p.kill()
            return None
    except (OSError, ValueError) as exc:
        _STATE["why"] = "worker failed to start: %r" % (exc,)
        return None
    _STATE["why"] = None
    return p


def worker():
    """The running worker, or None.  Started once, reused, never retried after
    a failure -- a toolchain that is not there will not appear mid-run, and
    retrying would pay the failure cost on every codeword."""
    if _STATE["proc"] is None and not _STATE["tried"]:
        _STATE["tried"] = True
        _STATE["proc"] = _spawn()
    return _STATE["proc"]


def available():
    return worker() is not None


def why_unavailable():
    worker()
    return _STATE["why"]


def close():
    p = _STATE["proc"]
    if p is not None:
        try:
            p.stdin.write(b"\x00")
            p.stdin.flush()
            p.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            try:
                p.kill()
            except OSError:
                pass
    _STATE["proc"] = None
    _STATE["tried"] = False


# A worker outlives its parent if the parent is killed rather than exited, and
# an orphan holds the binary open so the next `cargo build` cannot replace it.
# Shut it down explicitly on the way out.
atexit.register(close)


def decode_mother(v0):
    """18480-bit received vector -> (vector, iterations, converged, stage).

    Exactly `nd_ldpc_fix.decode_mother`'s contract.  Raises `RuntimeError` if
    the worker is not available, so the caller falls back rather than guessing.
    """
    p = worker()
    if p is None:
        raise RuntimeError("Rust LDPC worker unavailable: %s" % _STATE["why"])
    a = np.ascontiguousarray(v0, np.uint8)
    try:
        p.stdin.write(b"\x01")
        p.stdin.write(a.tobytes())
        p.stdin.flush()
        head = p.stdout.read(6)
        if len(head) != 6:
            raise RuntimeError("the LDPC worker died mid-decode")
        ok, iters, stage = head[0], struct.unpack("<I", head[1:5])[0], head[5]
        body = p.stdout.read(a.size)
        if len(body) != a.size:
            raise RuntimeError("the LDPC worker returned a short vector")
    except (OSError, ValueError) as exc:
        close()
        raise RuntimeError("the LDPC worker failed: %r" % (exc,))
    return (np.frombuffer(body, np.uint8).copy(), int(iters), bool(ok),
            STAGES.get(stage, "fail"))


def syndrome_weights(vs):
    """Syndrome weight of each mother vector, with no decode.  -> [int]

    The question the ECC map asks of every page it colours -- "is this a
    codeword" -- and the only part of a decode a CLEAN page needs.  It is the
    same `syndrome_weight` the corrector runs on its first iteration, so the two
    can never disagree about whether a page needed correcting.

    Raises `RuntimeError` if the worker is unavailable, so the caller falls back
    to the numpy path rather than guessing.
    """
    vs = list(vs)
    if not vs:
        return []
    p = worker()
    if p is None:
        raise RuntimeError("Rust LDPC worker unavailable: %s" % _STATE["why"])
    try:
        p.stdin.write(b"" + struct.pack("<I", len(vs)))
        for v in vs:
            p.stdin.write(np.ascontiguousarray(v, np.uint8).tobytes())
        p.stdin.flush()
        body = p.stdout.read(4 * len(vs))
        if len(body) != 4 * len(vs):
            raise RuntimeError("the LDPC worker returned short weights")
    except (OSError, ValueError) as exc:
        close()
        raise RuntimeError("the LDPC worker failed: %r" % (exc,))
    return [int(w) for w in struct.unpack("<%dI" % len(vs), body)]


def au_mcrcs(messages, width=6):
    """`titania_nand.au_mcrc` over many AU messages at once.  -> [bytes]

    Each message is the stored 4104-byte AU payload; the complement is applied
    here, as the definition requires.  Raises `RuntimeError` if the worker is
    unavailable so the caller falls back to the Python CRC.

    The last interpreter-bound loop on the ECC path: `crc48` clocks 4104 bytes
    one at a time through a 48-bit register, 1.04 ms an AU and 4.1 ms a page,
    and no thread count helps a pure-Python loop.
    """
    messages = [bytes(m) for m in messages]
    if not messages:
        return []
    if width not in (4, 6):
        raise ValueError("MCRC width must be 4 or 6")
    n = len(messages[0])
    if any(len(m) != n for m in messages):
        raise ValueError("every message in a batch must be the same length")
    p = worker()
    if p is None:
        raise RuntimeError("Rust LDPC worker unavailable: %s" % _STATE["why"])
    try:
        p.stdin.write(b"" + struct.pack("<IIB", len(messages), n, width))
        for m in messages:
            p.stdin.write(m)
        p.stdin.flush()
        body = p.stdout.read(width * len(messages))
        if len(body) != width * len(messages):
            raise RuntimeError("the LDPC worker returned short MCRCs")
    except (OSError, ValueError) as exc:
        close()
        raise RuntimeError("the LDPC worker failed: %r" % (exc,))
    return [body[i * width:(i + 1) * width] for i in range(len(messages))]


def decode_many(vs):
    """Decode several independent mother vectors at once, in parallel.

    -> [(vector, iterations, converged, stage)] in the order given.

    The four AUs of a page are independent, so `nd_deepparity.decode_page`
    hands over its four even codewords as one batch and, once those are written
    back, its four odd containers as another.  Each job is ~15 ms of pure
    arithmetic on its own working set, so this is very close to linear in the
    cores available.  Raises `RuntimeError` if the worker is unavailable.
    """
    vs = list(vs)
    if not vs:
        return []
    p = worker()
    if p is None:
        raise RuntimeError("Rust LDPC worker unavailable: %s" % _STATE["why"])
    n = vs[0].size if hasattr(vs[0], "size") else len(vs[0])
    try:
        p.stdin.write(b"\x02" + struct.pack("<I", len(vs)))
        for v in vs:
            p.stdin.write(np.ascontiguousarray(v, np.uint8).tobytes())
        p.stdin.flush()
        out = []
        for _ in vs:
            head = p.stdout.read(6)
            if len(head) != 6:
                raise RuntimeError("the LDPC worker died mid-batch")
            ok, iters, stage = (head[0], struct.unpack("<I", head[1:5])[0],
                                head[5])
            body = p.stdout.read(n)
            if len(body) != n:
                raise RuntimeError("the LDPC worker returned a short vector")
            out.append((np.frombuffer(body, np.uint8).copy(), int(iters),
                        bool(ok), STAGES.get(stage, "fail")))
    except (OSError, ValueError) as exc:
        close()
        raise RuntimeError("the LDPC worker failed: %r" % (exc,))
    return out


# ------------------------------------------------------------------ selftest --
def _selftest(trials=24, seed=11):
    """Rust against numpy on real codewords, over the whole error range.

    Agreement is required on the corrected bytes, the flip count, the stage and
    the iteration count -- not just on "both converged".  A decoder that landed
    on the same codeword after a different number of iterations would still
    change an adjudication that separates two candidates by correction count.
    """
    import time
    from . import nd_ldpc_fix as F
    ok = True
    print("nd_ldpc_rs -- selftest\n")
    if not available():
        print("[SKIP] Rust worker unavailable: %s" % why_unavailable())
        print("       the numpy decoder is used and every answer is unchanged")
        return 0
    print("  worker: %s" % binary_path())
    pool = F.clean_blocks(limit=8)
    print("  pool  : %d verified even codewords off <dump>/img" % len(pool))
    if not pool:
        print("[SKIP] no clean codewords available")
        return 0
    free_pos = np.flatnonzero(F.FREE)
    rng = np.random.default_rng(seed)
    t_py = t_rs = 0.0
    n = same = 0
    corpus = []
    for ni, N in enumerate((0, 1, 8, 32, 96, 128, 160, 176)):
        agree = 0
        for t in range(trials // 8 + 1):
            _nm, blk = pool[(ni + t) % len(pool)]
            v = F.block_to_mother(blk)
            if N:
                v[rng.choice(free_pos, size=N, replace=False)] ^= 1
            corpus.append(v.copy())
            t0 = time.time()
            pv, pit, pok, pst = F.decode_mother_numpy(v.copy())
            t_py += time.time() - t0
            t0 = time.time()
            rv, rit, rok, rst = decode_mother(v.copy())
            t_rs += time.time() - t0
            n += 1
            hit = (bool(pok) == bool(rok) and pst == rst and pit == rit
                   and np.array_equal(np.asarray(pv), rv))
            agree += hit
            same += hit
        print("[%s] %3d injected error(s): %d/%d identical (vector, "
              "converged, stage AND iteration count)"
              % ("PASS" if agree == trials // 8 + 1 else "FAIL", N, agree,
                 trials // 8 + 1))
        ok &= agree == trials // 8 + 1
    print("\n[%s] %d/%d decodes agree exactly" % ("PASS" if same == n else
                                                  "FAIL", same, n))
    print("      numpy %.2f s   rust %.2f s   speed-up %.1fx"
          % (t_py, t_rs, t_py / t_rs if t_rs else 0))
    ok &= same == n
    ok &= _selftest_simd(corpus)
    print("\nnd_ldpc_rs selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _selftest_simd(corpus):
    """The simd path against the scalar path, on the same corpus.  -> bool

    The worker picks its kernel width at start-up from `is_x86_feature_detected`
    and falls back to plain SSE2 where AVX2 is not there, so the same dump could
    be adjudicated on two different code paths on two different machines.  Those
    paths must agree bit for bit, including the iteration count -- a verdict
    that depended on the host CPU would be worse than a slow decoder.  AVX2
    changes no fp semantics (and `+fma` is not enabled, so nothing
    is ever contracted), which is the reason to expect agreement; this is the
    reason to know it.
    """
    if not corpus:
        return True
    base = [decode_mother(v.copy()) for v in corpus]
    close()
    prev = os.environ.get("TNLDPC_NO_SIMD")
    os.environ["TNLDPC_NO_SIMD"] = "1"
    try:
        if not available():
            print("[SKIP] scalar-path worker unavailable: %s"
                  % why_unavailable())
            return True
        alt = [decode_mother(v.copy()) for v in corpus]
    finally:
        close()
        if prev is None:
            os.environ.pop("TNLDPC_NO_SIMD", None)
        else:
            os.environ["TNLDPC_NO_SIMD"] = prev
    agree = sum(1 for (bv, bi, bo, bs), (av, ai, ao, as_) in zip(base, alt)
                if bi == ai and bo == ao and bs == as_
                and np.array_equal(bv, av))
    print("[%s] SIMD vs scalar kernel: %d/%d identical (vector, converged, "
          "stage AND iteration count)"
          % ("PASS" if agree == len(corpus) else "FAIL", agree, len(corpus)))
    return agree == len(corpus)


def main(argv):
    if "--build" in argv:
        p = build()
        print("built %s" % p if p else "build FAILED")
        return 0 if p else 1
    return _selftest()


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        close()
