#!/usr/bin/env python3
"""fa_phi.py -- goal B lever, driven by the goal a result.  Offline.

    py fa_phi.py

Goal a showed the hardware encoder is GF(2)-linear but not R-linear.  The only
natural way a QC engine does that is a Richardson-Urbanke encoder whose gap
matrix phi is singular over R: the g x g solve then has to be done at the GF(2)
level, and the fixed pseudo-inverse burned into silicon is a GF(2) object with no
reason to commute with rotation.  That predicts something checkable in the config
table: the 144-word tail, read as a 12x12 matrix over R, should expand to a
576 x 576 GF(2) matrix of rank 575 -- deficiency exactly 1, matching the code's.

Tests both codes (bank3 = A10R 137 cols, bank7 = code 7, 105 cols) and both bit
orders, because the ring convention already bit this project once.
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import numpy as np

from . import fa_ring as F

Z = 48
TABLES = [("bank3 (A10R)", "nd_ecutbl_ecc0enc_code0_rdsel0_bank3.bin", 137),
          ("bank7 (code7)", "nd_ecutbl_ecc0enc_code0_rdsel0_bank7.bin", 105)]


def words(p):
    b = p.read_bytes()
    return list(struct.unpack("<%dH" % (len(b) // 2), b))


def tail(path, ncol):
    w = words(HERE / path)
    t = 17 + 5 * ncol + 17
    assert t + 5 * 144 <= len(w), (t, len(w))
    return [w[t + 5 * r] | (w[t + 5 * r + 1] << 16) | (w[t + 5 * r + 2] << 32)
            for r in range(144)], w


def expand(M12):
    """12x12 over R -> 576x576 over GF(2).  Block (i,j) is the circulant of
    M12[i][j] acting as multiplication: column c of the block is x^c * poly."""
    out = np.zeros((12 * Z, 12 * Z), np.uint8)
    for i in range(12):
        for j in range(12):
            a = M12[i][j]
            if not a:
                continue
            for c in range(Z):
                v = F.rot(a, c)
                for r in range(Z):
                    if (v >> r) & 1:
                        out[i * Z + r, j * Z + c] = 1
    return out


def gf2_rank(A):
    A = A.copy()
    n, m = A.shape
    r = 0
    for c in range(m):
        p = None
        for i in range(r, n):
            if A[i, c]:
                p = i
                break
        if p is None:
            continue
        A[[r, p]] = A[[p, r]]
        rows = np.flatnonzero(A[:, c])
        rows = rows[rows != r]
        A[rows] ^= A[r]
        r += 1
        if r == n:
            break
    return r


def nullvec(A):
    """One non-trivial null vector of a (right kernel), or None."""
    n, m = A.shape
    aug = [(int("".join(str(int(b)) for b in A[:, c]), 2), 1 << c)
           for c in range(m)]
    piv = {}
    for cur, sel in aug:
        while cur:
            b = cur.bit_length() - 1
            if b in piv:
                cur ^= piv[b][0]
                sel ^= piv[b][1]
            else:
                piv[b] = (cur, sel)
                break
        else:
            return sel
    return None


def main():
    for name, path, ncol in TABLES:
        v, w = tail(path, ncol)
        print("== %s ==" % name)
        print("   header : %s" % " ".join("%04x" % x for x in w[:17]))
        nz = sum(1 for x in v if x)
        print("   tail   : %d entries, %d non-zero, mean popcount %.1f"
              % (len(v), nz, sum(bin(x).count("1") for x in v) / 144.0))
        for order in ("row", "col"):
            for rev in (False, True):
                M = [[(F.brev(v[i * 12 + j]) if rev else v[i * 12 + j])
                      if order == "row" else
                      (F.brev(v[j * 12 + i]) if rev else v[j * 12 + i])
                      for j in range(12)] for i in range(12)]
                E = expand(M)
                rk = gf2_rank(E)
                tag = "%s-major%s" % (order, " brev" if rev else "")
                extra = ""
                if rk < 576:
                    kv = nullvec(E)
                    if kv is not None:
                        kb = [(kv >> (Z * i)) & F.MASK for i in range(12)]
                        allo = sum(1 for x in kb if x == F.MASK)
                        zer = sum(1 for x in kb if x == 0)
                        extra = ("  null: %d all-ones / %d zero / %d partial blocks"
                                 % (allo, zer, 12 - allo - zer))
                print("   %-16s expanded GF(2) rank %3d / 576  (deficiency %d)%s"
                      % (tag, rk, 576 - rk, extra))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
