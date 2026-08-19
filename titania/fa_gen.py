#!/usr/bin/env python3
"""fa_gen.py -- the exact bit-level linear map behind nd_a10r_solve.candidates()[0],
plus the R-linear (QC-equivariant) generator built from it.  Offline.

    py fa_gen.py            # build + verify + cache fa_gen.npz

WHY THIS AND NOT 4128 ORACLE CALLS
    candidates()[0] is  p_b = <sel_b, rhs>,  rhs = A1.m ^ off(slot).  So the map
    m -> p is exactly  Lin[b] = sel_b^T . A1,  a GF(2) matrix product we can do in
    one shot.  Same answer as 4128 oracle calls, seconds instead of minutes, and
    exact rather than sampled.  It is verified against the oracle on media below.

COORDINATES
    Everything is exported in frame coordinates:
        Gmat[i, q] = coefficient of message frame bit i in parity frame bit q
        i in 0..4127 (message frame), q in 0..2399 (parity frame)
    nd_a10r_solve carries the message as a big-endian int, so message frame bit i
    is int bit (NM-1-i); it carries the parity as `p |= 1<<b`, so parity frame bit
    q is int bit (NP-1-q).  Getting either backwards silently transposes the code.
"""
import pickle
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import numpy as np

from . import fa_ring as F
from . import nd_g2ldpc_a10r as A
from . import nd_a10r_solve as S

NM, NP = F.NM, F.NP
CACHE = HERE / "fa_gen.npz"


def build(force=False):
    """-> (Gmat (4128,2400) uint8, Coff (4,2400) uint8, kvec (2400,) uint8)."""
    if CACHE.exists() and not force:
        d = np.load(CACHE)
        return d["G"], d["C"], d["k"]
    t0 = time.time()
    piv, ker, cons = S.build()
    assert len(ker) == 1, len(ker)

    # A1 packed by rows: A1p[i] = check row i restricted to the message, packed.
    A1p = np.packbits(A.H()[:, :NM], axis=1)            # (2400, 516)

    # sel matrix: sel_b bit (NP-1-i) selects check row i.
    sel = np.zeros((NP, NP), np.uint8)
    for b, row in piv.items():
        v = row & ((1 << NP) - 1)
        sel[b] = np.unpackbits(np.frombuffer(v.to_bytes(NP // 8, "big"), np.uint8))

    G = np.zeros((NM, NP), np.uint8)
    for b in piv:
        idx = np.flatnonzero(sel[b])                     # check rows used
        if idx.size:
            acc = np.bitwise_xor.reduce(A1p[idx], axis=0)
            G[:, NP - 1 - b] = np.unpackbits(acc)
    print("  Lin built in %.1fs" % (time.time() - t0))

    # per-slot constant parity = candidates(zero message, slot)[0]
    zero = bytes(516)
    built = (piv, ker, cons)
    C = np.zeros((4, NP), np.uint8)
    for j in range(4):
        p = S.candidates(zero, j, built)[0]
        C[j] = np.unpackbits(np.frombuffer(p, np.uint8))

    kvec = np.unpackbits(np.frombuffer(ker[0].to_bytes(NP // 8, "big"), np.uint8))
    np.savez_compressed(CACHE, G=G, C=C, k=kvec)
    return G, C, kvec


def encode_lin(G, C, msgbits, slot):
    """msgbits (n,4128) uint8 -> (n,2400) uint8, the candidates()[0] parity."""
    return (msgbits.astype(np.uint8) @ G % 2).astype(np.uint8) ^ C[slot]


# ------------------------------------------------------------------ R-linear G0

def unit_cols(G):
    """-> g[j][i] : 50 ring elements per message block column j (0..85).

    g_j = Lin(message with block j = polynomial 1).  These are the columns of an
    R-linear generator: A2 . g_j = A1 . e_j by construction, so
    G0(M) = sum_j M_j . g_j satisfies A2 . G0(M) = A1 . M for every M.
    """
    g = []
    for j in range(F.NMSG_BLK):
        row = G[F.Z * j]                                  # message frame bit 48j
        blk = []
        for i in range(F.NPAR_BLK):
            v = 0
            for c in range(F.Z):
                if row[F.Z * i + c]:
                    v |= 1 << c
            blk.append(v)
        g.append(blk)
    return g


def g0(g, M):
    out = [0] * F.NPAR_BLK
    for j in range(F.NMSG_BLK):
        mj = M[j]
        if not mj:
            continue
        gj = g[j]
        for i in range(F.NPAR_BLK):
            if gj[i]:
                out[i] ^= F.pmul(gj[i], mj)
    return out


def main():
    G, C, kvec = build("--force" in sys.argv)
    print("G %s  C %s  kernel weight %d" % (G.shape, C.shape, kvec.sum()))

    s = pickle.loads((HERE / "nd_a10r_samples.pkl").read_bytes())
    built = S.build()

    # --- verify Lin reproduces the oracle's candidate 0 exactly
    ok = 0
    N = 40
    for m, slot, t in s[:N]:
        mb = np.unpackbits(np.frombuffer(m, np.uint8))
        got = np.packbits(encode_lin(G, C, mb[None, :], slot)[0]).tobytes()
        if got == S.candidates(m, slot, built)[0]:
            ok += 1
    print("Lin == candidates()[0] on %d/%d media messages" % (ok, N))

    # --- verify the ring identity A2 . G0(M) == A1 . M
    A1, A2 = F.poly_matrix()
    g = unit_cols(G)
    rng = np.random.default_rng(1)
    badrows = 0
    for _ in range(5):
        M = [int(rng.integers(0, 1 << 48)) for _ in range(F.NMSG_BLK)]
        P = g0(g, M)
        for i in range(F.NPAR_BLK):
            acc = 0
            for j in range(F.NMSG_BLK):
                if A1[i][j] and M[j]:
                    acc ^= F.pmul(A1[i][j], M[j])
            for j in range(F.NPAR_BLK):
                if A2[i][j] and P[j]:
                    acc ^= F.pmul(A2[i][j], P[j])
            badrows += (acc != 0)
    print("A2.G0(M) ^ A1.M nonzero block-rows on 5 random messages: %d / 250"
          % badrows)

    # --- is Lin itself equivariant?  Lin(x^k e_j) vs x^k g_j
    mism = {}
    for j in range(F.NMSG_BLK):
        for k in range(1, F.Z):
            row = G[F.Z * j + k]
            want = [F.rot(v, k) for v in g[j]]
            got = []
            for i in range(F.NPAR_BLK):
                v = 0
                for c in range(F.Z):
                    if row[F.Z * i + c]:
                        v |= 1 << c
                got.append(v)
            if got != want:
                mism[j] = mism.get(j, 0) + 1
    tot = sum(mism.values())
    print("Lin equivariance: %d of %d (block,shift) pairs differ from x^k.g_j"
          % (tot, F.NMSG_BLK * (F.Z - 1)))
    print("  blocks with any mismatch: %d of %d" % (len(mism), F.NMSG_BLK))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
