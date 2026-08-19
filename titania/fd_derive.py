#!/usr/bin/env python3
"""fd_derive.py -- how much of Psi is forced by phi?  Offline.

Claim under test:
  * for I not in K = {1,5,6,11} the hardware's block x_I does not depend on the
    kernel choice (kappa is zero there), and the defect theorem gives
    f(rho y)_I = rho f(y)_I, so y -> x_I is a ring row.  It must therefore be
    the unique ring solution of  z^T . phi = e_I^T.
  * for I, i' in K the difference x_I + x_I' is likewise choice-free, so
    Psi_I + Psi_I' is the unique ring solution of z^T . phi = (e_I + e_I')^T.
  * only one ring row (say Psi_1) is left, and it is not determined by phi:
    z^T phi = e_1^T + r^T is solvable for every r with sigma(sum_{I in K} r_I)=1.

Everything is checked against the fitted fc_psi.pkl.
"""
import pickle
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F
from . import fc_common as C

Z = 48
MASK = F.MASK
KB = [1, 5, 6, 11]
NOTK = [i for i in range(12) if i not in KB]


def rowmap(phi):
    """576x576 GF(2) matrix Mt with (z^T phi) = z . Mt, z a ring row read as
    bits (block i, coeff k) -> index 48i+k."""
    M = np.zeros((576, 576), np.uint8)
    for i in range(12):
        for k in range(Z):
            for j in range(12):
                a = phi[i][j]
                if not a:
                    continue
                v = F.rot(a, k)
                for m in range(Z):
                    if (v >> m) & 1:
                        M[Z * i + k, Z * j + m] = 1
    return M


def solve_gf2(M, b):
    """solve z . M = b  (z unknown row).  -> (one solution or None, kernel dim)"""
    A = np.concatenate([M.T, b.reshape(-1, 1)], axis=1)   # M^T z^T = b^T
    n, m = A.shape
    A = A.copy()
    piv = []
    r = 0
    for c in range(m - 1):
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
        piv.append(c)
        r += 1
    for i in range(r, n):
        if A[i, m - 1]:
            return None, (m - 1) - r
    z = np.zeros(m - 1, np.uint8)
    for i, c in enumerate(piv):
        z[c] = A[i, m - 1]
    return z, (m - 1) - r


def bits_to_row(z):
    return [sum(int(z[Z * i + k]) << k for k in range(Z)) for i in range(12)]


def target(vec):
    """ring covector `vec` (list of 12 polys) -> its 576-bit GF(2) image of the
    'bit 0 of' convention used by rowmap: entry (block j, coeff m)."""
    b = np.zeros(576, np.uint8)
    for j in range(12):
        for m in range(Z):
            if (vec[j] >> m) & 1:
                b[Z * j + m] = 1
    return b


def main():
    basis = sys.argv[1] if len(sys.argv) > 1 else "media"
    A1, A2, order, gaprows, phi = C.prep(basis)
    Psi = pickle.load(open(HERE / ("fc_psi.pkl" if basis == "media" else "fc_psi_sil.pkl"), "rb"))
    QP = C.mmul(Psi, phi)
    r = [QP[KB[0]][j] ^ (1 if KB[0] == j else 0) for j in range(12)]
    Mt = rowmap(phi)
    print("basis %s ; kernel-support gap blocks K = %s" % (basis, KB))
    print("== rows OUTSIDE the kernel support: forced? ==")
    okc = 0
    for I in NOTK:
        e = [1 if j == I else 0 for j in range(12)]
        z, kdim = solve_gf2(Mt, target(e))
        if z is None:
            print("   I=%2d  NO SOLUTION" % I)
            continue
        row = bits_to_row(z)
        JJ = [MASK] * 12
        exact = (row == Psi[I])
        modJ = exact or (row == [Psi[I][j] ^ MASK for j in range(12)])
        print("   I=%2d  solvable, ring-kernel dim %d, == fitted Psi row: %s%s"
              % (I, kdim, "EXACT" if exact else ("mod (J,..,J)" if modJ else "NO"),
                 "" if modJ else "  diff=%s" % ["%012x" % (row[j] ^ Psi[I][j])
                                                for j in range(12)]))
        okc += modJ
    print("   -> %d of %d forced rows reproduced (mod the (J,..,J) ring-row kernel,"
          " which annihilates Y = image(phi) = 1^perp so it cannot change the"
          " encoder)" % (okc, len(NOTK)))

    print("== kernel-row DIFFERENCES: forced? ==")
    okd = 0
    for a in range(len(KB)):
        for b in range(a + 1, len(KB)):
            I, Ip = KB[a], KB[b]
            e = [1 if j in (I, Ip) else 0 for j in range(12)]
            z, kdim = solve_gf2(Mt, target(e))
            if z is None:
                print("   %d+%d NO SOLUTION" % (I, Ip))
                continue
            row = bits_to_row(z)
            want = [Psi[I][j] ^ Psi[Ip][j] for j in range(12)]
            modJ = (row == want) or (row == [want[j] ^ MASK for j in range(12)])
            print("   %2d+%2d solvable, kernel dim %d, == Psi_I+Psi_I': %s"
                  % (I, Ip, kdim,
                     "EXACT" if row == want else ("mod (J,..,J)" if modJ else "NO")))
            okd += modJ
    print("   -> %d of 6 differences reproduced (mod (J,..,J))" % okd)

    print("== the last row: is r determined by phi? ==")
    e1 = [1 if j == KB[0] else 0 for j in range(12)]
    z, kdim = solve_gf2(Mt, target(e1))
    print("   z^T.phi = e_1^T alone solvable:", z is not None)
    tgt = [e1[j] ^ r[j] for j in range(12)]
    z2, kdim2 = solve_gf2(Mt, target(tgt))
    print("   z^T.phi = e_1^T + r^T solvable:", z2 is not None, " kernel dim", kdim2)
    if z2 is not None:
        print("   matches fitted Psi row %d: %s" % (KB[0], bits_to_row(z2) == Psi[KB[0]]))
    # how many r are admissible?  test 20 random r with sigma(sum_K r)=1
    rng = np.random.default_rng(3)
    good = 0
    for _ in range(20):
        rr = [int(rng.integers(0, 1 << 48)) for _ in range(12)]
        ssum = 0
        for I in KB:
            ssum ^= rr[I]
        if not (bin(ssum).count("1") & 1):
            rr[KB[0]] ^= 1
            ssum ^= 1
        tg = [(1 if j == KB[0] else 0) ^ rr[j] for j in range(12)]
        zz, _ = solve_gf2(Mt, target(tg))
        good += zz is not None
    print("   RANDOM r with sigma(sum_K r)=1 that are admissible: %d/20" % good)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
