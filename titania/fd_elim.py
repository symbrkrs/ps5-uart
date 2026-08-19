#!/usr/bin/env python3
"""fd_elim.py -- Gaussian elimination of phi over R, with the last (non-unit)
pivot handled by the (1+x) serial accumulator.  Goal: produce the hardware's
gap solve from H alone.  Offline."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from . import fa_ring as F
from . import fc_common as C
from . import fd_ring as RG

Z = 48
MASK = F.MASK
KB = [1, 5, 6, 11]


def wt(x):
    return bin(x).count("1")


def unit_profile(phi):
    print("unit map of phi (U=unit, digits = (v1,v3) valuations):")
    for i in range(12):
        row = []
        for j in range(12):
            a = phi[i][j]
            if a == 0:
                row.append(" . ")
            elif RG.is_unit(a):
                row.append(" U ")
            else:
                row.append("%d%d " % (RG.val1(a), RG.val3(a)))
        print("   " + "".join(row))


def elim(phi, colorder=None, rowpick="first"):
    """forward elimination with unit pivots; returns (pivots, M, L) where M is
    the reduced 12x24 augmented matrix [phi | I]."""
    n = 12
    M = [[phi[i][j] for j in range(n)] + [1 if i == j else 0 for j in range(n)]
         for i in range(n)]
    used = set()
    pivots = []
    cols = colorder if colorder is not None else list(range(n))
    todo = list(cols)
    # Retry sweep: a column with no unit pivot yet may acquire one after later
    # eliminations, so keep sweeping until a whole pass makes no progress.  (a
    # single pass stalls on 2 of 12 columns in the silicon basis.)
    while True:
        progress = False
        rest = []
        for j in todo:
            cand = [i for i in range(n) if i not in used and RG.is_unit(M[i][j])]
            if not cand:
                rest.append(j)
                continue
            if rowpick == "first":
                p = cand[0]
            else:
                p = min(cand, key=lambda i: wt(M[i][j]))
            used.add(p)
            pivots.append((p, j))
            inv = RG.rinv(M[p][j])
            M[p] = [F.pmul(inv, v) for v in M[p]]
            for i in range(n):
                if i == p:
                    continue
                f = M[i][j]
                if f:
                    M[i] = [M[i][k] ^ F.pmul(f, M[p][k]) for k in range(2 * n)]
            progress = True
        todo = rest
        if not progress or not todo:
            break
    return pivots, M, todo, used


def main():
    for basis in ("media", "silicon"):
        print("=========== %s ===========" % basis)
        A1, A2, order, gaprows, phi = C.prep(basis)
        unit_profile(phi)
        piv, M, stalled, used = elim(phi)
        print("pivots (row,col):", piv)
        print("stalled columns:", stalled)
        left = [i for i in range(12) if i not in used]
        print("unused rows:", left)
        for i in left:
            for j in stalled:
                a = M[i][j]
                print("   residual M[%d][%d] = %012x  wt %d  v1 %d v3 %d unit %s"
                      % (i, j, a, wt(a), RG.val1(a), RG.val3(a), RG.is_unit(a)))
        # the non-pivot part of the residual row must be zero for a clean solve
        for i in left:
            rest = [j for j in range(12) if j not in stalled and M[i][j]]
            print("   row %d other nonzero cols: %s" % (i, rest))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
