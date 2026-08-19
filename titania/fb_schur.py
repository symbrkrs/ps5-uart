#!/usr/bin/env python3
"""fb_schur.py -- build the RU Schur complement phi = B_gap ^ T_gap.T_tri^-1.B_tri
for a peeling decomposition of A2, and compare it with the 144-word encoder tail.

Offline.  Key prediction: T_tri is triangular with monomial (=unit) diagonal, so
det(A2) ~ det(T_tri).det(phi); since rank(A2) = 2399 of 2400, phi must have GF(2)
rank 575 of 576.  The silicon tail is rank 576 -- so the tail can only be the
inverse of a repaired phi, which is exactly the seeding convention we are after.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F
from . import fb_ru as RU
from . import fb_gap as GP
from . import fa_phi as P12

Z = 48


def minv(a):
    """inverse of a monomial x^s in R."""
    assert a and (a & (a - 1)) == 0, hex(a)
    s = a.bit_length() - 1
    return 1 << ((-s) % Z)


def decomp(A2, G):
    """G = sorted list of 12 gap columns.  -> (order, gaprows)"""
    n = len(A2[0])
    rowcols = [set(j for j in range(n) if A2[i][j]) for i in range(len(A2))]
    order, resid = GP.peel(rowcols, n, set(G))
    assert resid == 0, resid
    gaprows = [r for r in range(len(A2)) if r not in set(x for x, _ in order)]
    return order, gaprows


def solve_tri(A2, order, P):
    """forward-substitute the triangle, given P already holding the gap columns
    (and zero elsewhere) and zero rhs.  Mutates and returns P."""
    for r, c in order:
        acc = 0
        for j in range(len(A2[0])):
            if j != c and A2[r][j] and P[j]:
                acc ^= F.pmul(A2[r][j], P[j])
        P[c] = F.pmul(minv(A2[r][c]), acc)
    return P


def build_phi(A2, G, order, gaprows):
    n = len(A2[0])
    phi = [[0] * len(G) for _ in gaprows]
    for k, gc in enumerate(G):
        P = [0] * n
        P[gc] = 1
        solve_tri(A2, order, P)
        for ri, r in enumerate(gaprows):
            acc = 0
            for j in range(n):
                if A2[r][j] and P[j]:
                    acc ^= F.pmul(A2[r][j], P[j])
            phi[ri][k] = acc
    return phi


def describe(name, M):
    flat = [x for r in M for x in r]
    z = sum(1 for x in flat if x == 0)
    wts = sorted(bin(x).count("1") for x in flat)
    print("   %-28s zero %3d/%d  weight med %d max %d"
          % (name, z, len(flat), wts[len(wts) // 2], wts[-1]))


def main():
    A1s, As, A2s = RU.silicon_poly()
    A1m, A2m = F.poly_matrix()
    for name, A2, G in (("silicon", A2s, [2, 3, 4, 7, 8, 9, 10, 12, 13, 16, 18, 22]),
                        ("media", A2m, [2, 5, 6, 7, 8, 9, 10, 12, 13, 18, 22, 29])):
        order, gaprows = decomp(A2, G)
        print("== %s ==  gap cols %s" % (name, G))
        print("   gap rows %s" % gaprows)
        diag_mono = all((A2[r][c] & (A2[r][c] - 1)) == 0 for r, c in order)
        print("   triangle diagonal all monomial: %s (38 steps)" % diag_mono)
        phi = build_phi(A2, G, order, gaprows)
        describe("phi", phi)
        E = P12.expand(phi)
        rk = P12.gf2_rank(E)
        print("   phi expanded GF(2) rank %d / 576  (deficiency %d)" % (rk, 576 - rk))
        if rk < 576:
            kv = P12.nullvec(E)
            kb = [(kv >> (Z * i)) & F.MASK for i in range(12)]
            print("   right-null: %d all-ones, %d zero, %d partial"
                  % (sum(1 for x in kb if x == F.MASK), sum(1 for x in kb if x == 0),
                     sum(1 for x in kb if x not in (0, F.MASK))))
        np.save(HERE / ("fb_phi_%s.npy" % name), np.array(phi, dtype=object), allow_pickle=True)
    # reference: the tail's inverse profile
    v, w = P12.tail("nd_ecutbl_ecc0enc_code0_rdsel0_bank3.bin", 137)
    print("\ntail popcounts: med %d, min %d, max %d"
          % (sorted(bin(x).count("1") for x in v)[72],
             min(bin(x).count("1") for x in v), max(bin(x).count("1") for x in v)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
