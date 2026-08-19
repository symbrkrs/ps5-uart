#!/usr/bin/env python3
"""Hard-decision LDPC *corrector* for the Titania EE3 code (we only had a detector).

Offline.  Reads <dump>/img/*.bin, never touches hardware.

WHAT THIS ADDS
--------------
nd_ldpc.py gives a syndrome, i.e. a yes/no "this 2304-byte block is clean".
Every dump so far dealt with a dirty read by re-reading until the syndrome
closed (nd_dumpimage.py) or by majority-voting several reads.  This file turns
the same parity-check matrix into a decoder: given one read with flipped bits it
returns the corrected block.

    from .nd_ldpc_fix import correct
    fixed, nflips, ok = correct(block2304)      # even codewords (0,2,4,6)

Result (measured by main(); uniformly random bit errors in real, verified
codewords off <dump>/img; 200-300 trials per point)

    N errors    exact     miscorrect   no-converge   ms/block
       1..128   100.0%       0.0%          0.0%       1.8 .. 55
       160       99.0%       0.0%          1.0%        90
       164       97.3%       0.0%          2.7%       105
       168       90.7%       0.0%          9.3%       136
       172       65.7%       0.0%         34.3%       192
       192        0.0%       0.0%        100.0%       319

  >99% correction up to ~160 bad bits per 2304-byte block = 0.87% raw BER, with
  a sharp waterfall at ~170 (0.92%).  For reference the bsc capacity of a
  rate-0.894 code is 1.25% BER, so this sits at ~70% of Shannon -- about what
  hard-decision min-sum should give, and there is not much more to win without
  real soft reads out of the NAND.
  0.6 ms to confirm a clean block (2.4 ms/page over the 4 even codewords);
  2-90 ms to fix a dirty one.
  Miscorrection was never observed: 0 in ~3000 decodes, including 700 at
  N >= 176 where the decoder mostly fails.  With 1968 parity bits the failure
  mode is honest non-convergence, not a wrong answer that looks right.  That is
  a measurement, not a proof.

So a single read can absorb ~160 bad bits where before it needed a retry.  A
healthy MLC page is nowhere near that, so in practice this should remove read
retries entirely for the even codewords.

ALGORITHM
---------
Nothing is materialised as a dense 1968x18480 matrix.  The code object already
carries `idx` (2861 circulants x 48 check rows -> bit index) and `starts`
(layer boundaries), so the whole H fits in a (41 layers, 70 circulants, 48)
edge tensor, 137760 edges, and every decoder step is one vectorised numpy pass
over it.

  stage a  parallel bit flipping (Gallager-B majority): flip every bit whose
           unsatisfied-check count exceeds half its column weight (dv is 5 for
           block cols 72..384, 18 for 0..71).  ~0.6 ms/iteration, clears N <= 8
           in two iterations; capped at 12 iterations because past its cliff it
           is only burning time before stage B has to run anyway.
  stage B  normalized min-sum on the hard bits: channel llr = +-1, scaling
           alpha = 0.45, flooding schedule, up to 80 iterations, ~4 ms per
           iteration.  This is what gives the correction capability above;
           stage a is only a fast path for the common near-clean case.

Min-sum on hard decisions is legitimate: with a bsc the channel llr magnitude is
the same for every bit, so it factors out and only the sign matters.  We get no
extra information from the NAND (no soft reads plumbed through yet) but we do
get the full benefit of belief propagation over the graph.

GEOMETRY / TRAPS
----------------
* The codeword is the complement of the stored bytes (nd_kpaudit.syn()).  A
  flipped bit is at the same position either way, but correct() takes and
  returns *stored* bytes, so the complement is internal.
* 64 mother bits are structurally known and must never be treated as received:
  block column 343 (48 bits, the shortened column) and the last 2 bytes of
  block column 342 (16 bits, the two implied-zero info bytes).  They get llr
  +50 (pinned to 0).  Letting the decoder move them was the first bug here: it
  "converged" to a codeword that no longer maps back to any 2304-byte block.
* Stored bytes 2302..2303 are not in the codeword at all.  Bit errors there are
  invisible to the LDPC and correct() passes them through untouched.  16 of
  18432 stored bits have zero protection.  (nd_ldpc.py's mother_from_block0()
  puts information there instead; the layout that actually verifies on media is
  nd_kpaudit's, with those two bytes outside the code.  This file follows
  nd_kpaudit -- checked equal syndrome weights on all 8 codewords of a page.)
* c[byte_indices] = ... with repeated byte indices silently drops 7 of every 8
  bit writes (fancy-index assignment, last write wins).  The bit<->byte
  round-trip here goes through unpackbits/packbits for that reason.

NEGATIVE RESULTS
----------------
* Bit flipping alone is weak: a hard cliff between N=16 (95% exact) and N=24
  (2%).  Do not ship stage a on its own.  It is 1/40th of what min-sum does.
* The usual "penalise bits that already moved" term (score = 2u - dv - w*d)
  makes bit flipping strictly worse here: 38/40 at N=16 with w=0, 23/40 with
  w=1.  Same for probabilistic flipping (p<1).  Both were tried and dropped.
* The min-sum scaling factor is not the textbook 0.75-0.8.  Measured at N=176,
  20 trials: alpha 0.75 -> 0/20, 0.7 -> 2/20, 0.6 -> 0/20 (with 60 iters),
  0.55 -> 1/20, 0.45..0.5 -> 7/20, 0.35 -> 2/20, 0.3 -> 0/20.  alpha >= 0.9
  fails outright even at N=128.  0.45 it is; 0.4-0.5 is the plateau.
* Running the same word through several alphas (0.45/0.55/0.35) and taking the
  first success buys nothing -- the failures coincide.  It is the graph, not
  the schedule.  Dropped.
* More iterations do not help either: 100 -> 200 iterations at N=128/alpha=0.8
  moved 16/20 to 17/20.  Everything that converges does so within ~60.
* All of this applies to even codewords (0,2,4,6) only.  The odd-codeword
  layout is still unsolved, so half of every AU remains uncorrectable, and no
  decoder can change that until the layout is.
* A zero syndrome after decoding is not the same proof of correctness a zero
  syndrome on a raw read is: the decoder can land on a different valid
  codeword.  main() counts those separately.  It never happened once in ~3000
  decodes, but that is a measurement at these noise levels, not a guarantee.
"""
import sys, time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_ldpc import ee3, Z
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
IMG = config.dump_path("img")
STRIDE = 2304


# ------------------------------------------------------------------ bit mapping
def _mapping():
    """mother bit index -> (stored byte, bit-in-byte), byte = -1 for known zeros.

    Mirrors nd_kpaudit.syn() exactly:
      info   block col c in 0..342  <- complemented bytes 6c..6c+5, MSB first,
             but only where 6c+k < 2056; the rest are the implied zero bytes
      col    343                    <- all zero, not stored (shortened)
      parity block col 344+j        <- complemented bytes 2056+6j .. +5
    """
    n = 385 * Z
    byte = np.full(n, -1, np.int32)
    for c in range(343):                                    # information
        for t in range(Z):
            b = 6 * c + t // 8
            if b < 2056:
                byte[c * Z + t] = b
    for j in range(41):                                     # parity
        for t in range(Z):
            byte[(344 + j) * Z + t] = 2056 + 6 * j + t // 8
    bit = np.tile(np.arange(Z) % 8, 385).astype(np.int32)   # unpackbits: MSB 1st
    return byte, bit


BYTE_OF, BIT_OF = _mapping()
FREE = BYTE_OF >= 0                            # 18416 of 18480
POS = BYTE_OF[FREE] * 8 + BIT_OF[FREE]         # -> index into unpackbits(block)
assert len(np.unique(POS)) == len(POS) == 18416


def block_to_mother(a):
    """2304 stored bytes -> 18480-bit mother vector."""
    bits = np.unpackbits((~np.asarray(a[:STRIDE], np.uint8)) & 0xFF)
    v = np.zeros(385 * Z, np.uint8)
    v[FREE] = bits[POS]
    return v


def mother_to_block(v, a):
    """Mother bits written back over a copy of the stored block."""
    bits = np.unpackbits((~np.asarray(a[:STRIDE], np.uint8)) & 0xFF)
    bits[POS] = v[FREE]
    return (~np.packbits(bits)) & 0xFF


# ---------------------------------------------------------------------- decoder
class Decoder:
    """Edge-tensor view of H: (Mb layers, Wmax circulants, Z check rows)."""

    def __init__(self, code=None):
        C = self.code = code or ee3()
        self.n = C.n_bits
        self.flat = C.idx.ravel().astype(np.int32)
        lens = [len(L) for L in C.layers]
        self.lay = np.repeat(np.arange(C.Mb), lens)         # circulant -> layer
        self.dv = np.bincount(self.flat, minlength=self.n).astype(np.int16)
        self.free = FREE
        self.fixed = ~FREE
        W = self.W = max(lens)
        self.eidx = np.zeros((C.Mb, W, C.Z), np.int32)
        em = np.zeros((C.Mb, W), bool)
        for i, st in enumerate(C.starts):
            k = lens[i]
            self.eidx[i, :k] = C.idx[st:st + k]
            em[i, :k] = True
        self.emask = em[:, :, None]
        self.eflat = self.eidx.ravel()
        self.arangeW = np.arange(W)[None, :, None]

    # -- primitives ---------------------------------------------------------
    def syndrome(self, v):
        return self.code.syndrome(v)

    def unsat(self, v, s=None):
        """per-bit count of unsatisfied checks; only touched bits cost work."""
        if s is None:
            s = self.syndrome(v)
        return np.bincount(self.flat[s[self.lay].ravel() != 0], minlength=self.n)

    # -- stage a: parallel Gallager-B majority ------------------------------
    def bitflip(self, v0, iters=12):
        v = v0.copy()
        hist = []
        for it in range(1, iters + 1):
            s = self.syndrome(v)
            sw = int(s.sum())
            if sw == 0:
                return v, it, True
            hist.append(sw)
            cand = (2 * self.unsat(v, s) > self.dv) & self.free
            if not cand.any():
                return v, it, False
            v[cand] ^= 1
            if len(hist) >= 8 and len(set(hist[-8:])) <= 2:      # oscillating
                return v, it, False
        return v, iters, False

    # -- stage B: normalized min-sum ---------------------------------------
    def minsum(self, v0, iters=80, alpha=0.45, pin=50.0, big=1e3):
        ch = np.where(v0 > 0, -1.0, 1.0).astype(np.float32)
        ch[self.fixed] = pin                                 # known-zero bits
        R = np.zeros((self.code.Mb, self.W, self.code.Z), np.float32)
        tot = ch.copy()
        for it in range(1, iters + 1):
            v = (tot < 0).astype(np.uint8)
            if self.code.syndrome_weight(v) == 0:
                return v, it, True
            Q = np.where(self.emask, tot[self.eidx] - R, big)
            aQ = np.abs(Q)
            part = np.partition(aQ, 1, axis=1)
            m1, m2 = part[:, 0, :], part[:, 1, :]
            am = np.argmin(aQ, axis=1)
            mag = np.where(self.arangeW == am[:, None, :],
                           m2[:, None, :], m1[:, None, :])
            sg = np.where(Q >= 0, np.float32(1), np.float32(-1))
            R = np.where(self.emask,
                         alpha * np.prod(sg, axis=1)[:, None, :] * sg * mag,
                         np.float32(0)).astype(np.float32)
            tot = ch + np.bincount(self.eflat, weights=R.ravel(),
                                   minlength=self.n).astype(np.float32)
        v = (tot < 0).astype(np.uint8)
        return v, iters, self.code.syndrome_weight(v) == 0


_DEC = None


def decoder():
    global _DEC
    if _DEC is None:
        _DEC = Decoder()
    return _DEC


_RUST = None


def _rust():
    """The Rust corrector, or None.  Probed once.

    This decoder is the hot loop of `DumpDevice.adjudicate()` -- 55.7 s of a
    62.3 s run -- and it is genuinely compute-bound, so the fast path is
    `rs/tnldpc`, which reproduces the code below operation for operation (see
    `nd_ldpc_rs.py`).  Everything here stays as the reference and as the
    fall-back: no toolchain, no binary, or `TITANIA_NO_RUST=1` and the numpy
    path runs, unchanged.
    """
    global _RUST
    if _RUST is None:
        try:
            from . import nd_ldpc_rs
            _RUST = nd_ldpc_rs if nd_ldpc_rs.available() else False
        except Exception:                                   # noqa: BLE001
            _RUST = False
    return _RUST or None


def decode_mother(v0, full=True):
    """18480-bit received vector -> (vector, iterations, converged, stage)."""
    R = _rust()
    if R is not None and full:
        try:
            return R.decode_mother(v0)
        except RuntimeError:
            pass                       # worker gone: fall through to numpy
    D = decoder()
    if D.code.syndrome_weight(v0) == 0:
        return v0, 0, True, "clean"
    v, it, ok = D.bitflip(v0)
    if ok:
        return v, it, True, "A"
    if not full:
        return v0, it, False, "fail"
    v2, it2, ok2 = D.minsum(v0)
    if ok2:
        return v2, it + it2, True, "B"
    return v0, it + it2, False, "fail"


def decode_mother_numpy(v0, full=True):
    """`decode_mother` with the Rust path bypassed -- the reference path."""
    D = decoder()
    if D.code.syndrome_weight(v0) == 0:
        return v0, 0, True, "clean"
    v, it, ok = D.bitflip(v0)
    if ok:
        return v, it, True, "A"
    if not full:
        return v0, it, False, "fail"
    v2, it2, ok2 = D.minsum(v0)
    if ok2:
        return v2, it + it2, True, "B"
    return v0, it + it2, False, "fail"


def correct(block2304, full=True):
    """Correct one stored 2304-byte AU block (even codeword layout).

    Returns (corrected_bytes, n_bits_flipped, converged).  `converged` means the
    syndrome closed -- see the miscorrection caveat in the module docstring.
    On failure the original block is returned with converged=False.
    """
    a = np.frombuffer(block2304, np.uint8) if isinstance(
        block2304, (bytes, bytearray)) else np.asarray(block2304, np.uint8)
    v0 = block_to_mother(a)
    v, _, ok, _ = decode_mother(v0, full)
    if not ok:
        return bytes(a[:STRIDE]), 0, False
    return bytes(mother_to_block(v, a)), int((v != v0).sum()), True


def correct_verbose(block2304, full=True):
    """As correct(), plus (stage, iterations)."""
    a = np.frombuffer(block2304, np.uint8) if isinstance(
        block2304, (bytes, bytearray)) else np.asarray(block2304, np.uint8)
    v0 = block_to_mother(a)
    v, it, ok, stage = decode_mother(v0, full)
    if not ok:
        return bytes(a[:STRIDE]), 0, False, stage, it
    return bytes(mother_to_block(v, a)), int((v != v0).sum()), True, stage, it


# ------------------------------------------------------------------ validation
def clean_blocks(limit=40, per_page=1):
    """Real even-codeword blocks off the dump whose syndrome is already zero."""
    D = decoder()
    out = []
    for p in sorted(IMG.glob("img_ce*_b*_p*.bin")):
        raw = np.frombuffer(p.read_bytes(), np.uint8)
        if len(raw) < 8 * STRIDE:
            continue
        got = 0
        for cw in (0, 2, 4, 6):
            blk = raw[cw * STRIDE:(cw + 1) * STRIDE]
            if int((blk == 0xFF).sum()) == STRIDE:
                continue
            if D.code.syndrome_weight(block_to_mother(blk)) == 0:
                out.append((f"{p.stem}/cw{cw}", np.array(blk)))
                got += 1
                if got >= per_page:
                    break
        if len(out) >= limit:
            break
    return out[:limit]


def real_error_survey():
    D = decoder()
    tot = ok = erased = 0
    bad = []
    for p in sorted(IMG.glob("img_ce*_b*_p*.bin")):
        raw = np.frombuffer(p.read_bytes(), np.uint8)
        if len(raw) < 8 * STRIDE:
            continue
        for cw in (0, 2, 4, 6):
            blk = raw[cw * STRIDE:(cw + 1) * STRIDE]
            if int((blk == 0xFF).sum()) == STRIDE:
                erased += 1
                continue
            tot += 1
            w = D.code.syndrome_weight(block_to_mother(blk))
            if w == 0:
                ok += 1
            else:
                bad.append((p.stem, cw, w))
    return tot, ok, erased, bad


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--pool", type=int, default=40)
    ap.add_argument("--n", type=int, nargs="*",
                    default=[1, 2, 4, 8, 16, 32, 64, 128, 160, 176, 192, 256])
    args = ap.parse_args()

    D = decoder()
    print(f"EE3: n={D.n} bits, {int(D.free.sum())} free "
          f"({int(D.fixed.sum())} structural zeros), "
          f"parity {D.code.Mb * Z} bits, rate {1 - D.code.Mb / D.code.Nb:.4f}; "
          f"edge tensor {D.code.Mb}x{D.W}x{D.code.Z}")

    print("\n[1] real dump: even codewords that fail the syndrome as stored")
    tot, ok, er, bad = real_error_survey()
    print(f"    {ok}/{tot} clean, {er} erased, {len(bad)} failing")
    for nm, cw, w in bad[:10]:
        print(f"      {nm} cw{cw}: syndrome weight {w}")
    if not bad:
        print("    (expected: nd_dumpimage.py retried until each verified, so"
              " the stored dump is clean by construction --")
        print("     the decoder's value is in NOT needing those retries)")

    print("\n[2] synthetic: flip N random bits in a verified codeword, decode")
    pool = clean_blocks(limit=args.pool)
    print(f"    pool = {len(pool)} verified even codewords from distinct pages")
    free_pos = np.flatnonzero(FREE)
    rng = np.random.default_rng(1)
    T = args.trials
    print("\n      N   trials    exact  miscorrect  no-converge   A%   "
          f"mean iters   ms/block")
    for N in args.n:
        exact = mis = fail = a_stage = 0
        iters = []
        t0 = time.time()
        for t in range(T):
            _, blk = pool[t % len(pool)]
            v = block_to_mother(blk)
            v[rng.choice(free_pos, size=N, replace=False)] ^= 1
            noisy = mother_to_block(v, blk)
            out, nf, conv, stage, it = correct_verbose(noisy)
            if not conv:
                fail += 1
            else:
                iters.append(it)
                a_stage += stage == "A"
                if bytes(out) == bytes(blk):
                    exact += 1
                else:
                    mis += 1
        dt = (time.time() - t0) / T * 1e3
        mi = f"{np.mean(iters):.1f}" if iters else "-"
        print(f"    {N:3d}   {T:6d}   {exact*100.0/T:6.1f}%  {mis*100.0/T:9.1f}%"
              f"  {fail*100.0/T:10.1f}%  {a_stage*100.0/T:4.0f}  {mi:>10}"
              f"   {dt:8.2f}")

    print("\n[3] clean-block cost (the common case: syndrome only)")
    _, blk = pool[0]
    t0 = time.time()
    for _ in range(100):
        correct(blk)
    print(f"    {(time.time()-t0)/100*1e3:.3f} ms per already-good block "
          f"({(time.time()-t0)/100*1e3*4:.1f} ms per page, 4 even codewords)")

    print("\n[4] what raw bit error rate does that buy?  (binomial over 18416"
          " coded bits)")
    print("      raw BER     mean errors/block   inside the corrector?")
    for ber in (1e-5, 1e-4, 3e-4, 1e-3, 3e-3, 8.7e-3, 1.2e-2):
        m = ber * 18416
        tag = ("yes, comfortably" if m < 100 else
               "marginal (waterfall is ~170)" if m < 170 else "no")
        print(f"      {ber:8.1e}   {m:15.1f}   {tag}")
    print("    the corrector's >99% point, N=160, is 8.7e-3 raw BER; the BSC"
          " capacity of a")
    print("    rate-0.894 code is 1.25e-2, so we are at ~70% of Shannon on hard"
          " decisions.")


if __name__ == "__main__":
    main()
