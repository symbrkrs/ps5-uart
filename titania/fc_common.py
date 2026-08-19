#!/usr/bin/env python3
"""fc_common.py -- shared helpers for the fc_* (third-pass) A10R work.  Offline.

Imports the established objects rather than re-deriving them:
    fa_ring.poly_matrix()  -- media-basis A1 (50x86) / A2 (50x50) over R
    fb_gap.peel()          -- the RU peeling with gap = parity block cols 0..11
    fb_schur.build_phi()   -- phi = B_gap ^ T_gap.T_tri^-1.B_tri
    fb_qrows.pkl           -- the 8 exactly-recovered ring rows of the gap map
    fa_gen.build()         -- exact linear map to the media parity (ground truth)

Adds:  every plausible 48-bit word/byte convention for the 144-word tail, and a
batch (y, P_gap) generator so hypotheses can be tested functionally against the
oracle instead of by pattern matching.
"""
import pickle
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F
from . import fa_phi as P12
from . import fb_gap as GP
from . import fb_schur as SC

Z = 48
J = F.MASK
GAP = list(range(12))


# ---------------------------------------------------------------- RU objects

def prep(basis="media"):
    """-> A1, A2, order, gaprows, phi   (gap = parity block columns 0..11)."""
    if basis == "media":
        A1, A2 = F.poly_matrix()
    else:
        from . import fb_ru as RU
        A1, _, A2 = RU.silicon_poly()
    n = len(A2)
    rowcols = [set(j for j in range(n) if A2[i][j]) for i in range(n)]
    order, resid = GP.peel(rowcols, n, set(GAP))
    assert resid == 0, resid
    gaprows = [r for r in range(n) if r not in set(x for x, _ in order)]
    phi = SC.build_phi(A2, GAP, order, gaprows)
    return A1, A2, order, gaprows, phi


# ---------------------------------------------------------------- tail reads

def tail_words(path, ncol):
    w = P12.words(HERE / path)
    t = 17 + 5 * ncol + 17
    return [(w[t + 5 * r], w[t + 5 * r + 1], w[t + 5 * r + 2]) for r in range(144)]


def conv_values(trip):
    """All plausible 48-bit readings of a (w0,w1,w2) triple -> dict name->int."""
    out = {}
    for bo, bname in ((0, "wLSW"), (1, "wMSW")):
        ws = trip if bo == 0 else trip[::-1]
        b = b"".join(x.to_bytes(2, "little") for x in ws)
        for eo, ename in (("big", "BE"), ("little", "LE")):
            v = int.from_bytes(b, eo)
            out["%s.%s" % (bname, ename)] = v
            out["%s.%s.brev" % (bname, ename)] = F.brev(v)
    return out


def tails():
    """-> {code: {convention: [144 ints]}}"""
    res = {}
    for name, path, ncol in P12.TABLES:
        trips = tail_words(path, ncol)
        cols = {}
        for tr in trips:
            for k, v in conv_values(tr).items():
                cols.setdefault(k, []).append(v)
        res[name] = cols
    return res


def as12(v, order):
    if order == "row":
        return [[v[i * 12 + j] for j in range(12)] for i in range(12)]
    return [[v[j * 12 + i] for j in range(12)] for i in range(12)]


# ---------------------------------------------------------------- ring linalg

def mmul(A, B):
    n, m, p = len(A), len(B), len(B[0])
    out = [[0] * p for _ in range(n)]
    for i in range(n):
        Ai = A[i]
        oi = out[i]
        for k in range(m):
            a = Ai[k]
            if not a:
                continue
            Bk = B[k]
            for j in range(p):
                if Bk[j]:
                    oi[j] ^= F.pmul(a, Bk[j])
    return out


def mvmul(A, x):
    out = []
    for i in range(len(A)):
        acc = 0
        for j in range(len(x)):
            if A[i][j] and x[j]:
                acc ^= F.pmul(A[i][j], x[j])
        out.append(acc)
    return out


def sigma_mat(M):
    return [[F.sigma(x) for x in r] for r in M]


def rc(x):
    """canonical rotation class."""
    return min(F.rot(x, r) for r in range(Z)) if x else 0


# ---------------------------------------------------------------- media data

def ydata(NS=200, basis="media"):
    """-> (Y, PG, phi, gaprows): NS pairs of RU gap rhs y (12 blocks) and the
    true parity restricted to gap block columns 0..11."""
    from . import fa_gen as GEN
    from . import nd_g2ldpc_a10r as A
    A1, A2, order, gaprows, phi = prep(basis)
    off = A.offsets()
    offb = [[sum(1 << c for c in range(Z) if off[j][Z * i + c]) for i in range(50)]
            for j in range(4)]
    s = pickle.loads((HERE / "nd_a10r_samples.pkl").read_bytes())
    G, C, kvec = GEN.build()
    msg = np.load(HERE / "fb_msgbits.npy")
    slot = np.array([sl for _, sl, _ in s], np.int64)
    tt = np.array([x for _, _, x in s], np.uint8)
    P0 = ((msg[:NS].astype(np.float32) @ G.astype(np.float32)) % 2).astype(np.uint8) \
        ^ C[slot[:NS]]
    PT = P0 ^ (tt[:NS, None] * kvec[None, :])
    Y, PG = [], []
    for i in range(NS):
        M = F.blocks_of(s[i][0])
        sv = []
        for r in range(50):
            acc = offb[slot[i]][r]
            for j in range(86):
                if A1[r][j] and M[j]:
                    acc ^= F.pmul(A1[r][j], M[j])
            sv.append(acc)
        p = [0] * 50
        for r, c in order:
            acc = sv[r]
            for j in range(50):
                if j != c and A2[r][j] and p[j]:
                    acc ^= F.pmul(A2[r][j], p[j])
            p[c] = F.pmul(SC.minv(A2[r][c]), acc)
        yv = []
        for r in gaprows:
            acc = sv[r]
            for j in range(50):
                if A2[r][j] and p[j]:
                    acc ^= F.pmul(A2[r][j], p[j])
            yv.append(acc)
        Y.append(yv)
        PG.append([sum(1 << c for c in range(Z) if PT[i][Z * b + c]) for b in range(12)])
    return Y, PG, phi, gaprows, kvec
