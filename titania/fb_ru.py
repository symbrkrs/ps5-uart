#!/usr/bin/env python3
"""fb_ru.py -- Richardson-Urbanke greedy triangulation of A2 (50x50 over R).

Offline.  Provides:
    silicon_poly()  -> (A1, Ashort, A2) in the silicon basis (137 mother cols)
    greedy(A2pat, order) -> (rows_tri, cols_tri, gapcols, gaprows)

Peeling model (erasure-decoder view):  unknowns = 50 parity block columns,
equations = 50 check block rows.  Declaring a column "gap" removes it from the
residual graph.  A row with residual degree 1 solves its remaining column.
Stall => must promote more columns to the gap.  Final gap size = |gapcols|.
Every non-zero block of A2 is a monomial, hence a unit of R, so a triangular
selection is back-substitutable over R with no invertibility caveat.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F

Z = 48
SHORTCOL = 86


def silicon_poly(reciprocal=True):
    d = np.load(HERE / "nd_a10r_silicon_H.npz")
    H = d["H"]
    nbc = H.shape[1] // Z
    out = []
    for i in range(H.shape[0] // Z):
        row = []
        for j in range(nbc):
            B = H[i * Z, j * Z:(j + 1) * Z]
            v = 0
            for k in range(Z):
                if B[k]:
                    v |= 1 << k
            row.append(F.recip(v) if reciprocal else v)
        out.append(row)
    A1 = [r[:SHORTCOL] for r in out]
    As = [r[SHORTCOL] for r in out]
    A2 = [r[SHORTCOL + 1:] for r in out]
    return A1, As, A2


def greedy(A2, colpick="minres", rng=None, verbose=False):
    """RU greedy.  Returns dict with tri order and gap sets."""
    m = len(A2)
    n = len(A2[0])
    rowcols = [set(j for j in range(n) if A2[i][j]) for i in range(m)]
    colrows = [set(i for i in range(m) if A2[i][j]) for j in range(n)]
    live_rows = set(range(m))
    live_cols = set(range(n))
    gap = []
    tri = []                      # list of (row, col) in elimination order
    while live_cols:
        # peel: rows with residual degree 1
        prog = True
        while prog:
            prog = False
            for i in sorted(live_rows):
                res = rowcols[i] & live_cols
                if len(res) == 1:
                    c = next(iter(res))
                    tri.append((i, c))
                    live_rows.discard(i)
                    live_cols.discard(c)
                    prog = True
                    break
                if len(res) == 0:
                    live_rows.discard(i)
                    prog = True
                    break
        if not live_cols:
            break
        # stalled -> promote columns to gap
        degs = [(len(rowcols[i] & live_cols), i) for i in live_rows]
        degs = [d for d in degs if d[0] > 1]
        if not degs:
            break
        d, i = min(degs)
        res = sorted(rowcols[i] & live_cols)
        # choose which column to keep (become the triangle pivot)
        if colpick == "minres":
            keep = min(res, key=lambda c: len(colrows[c] & live_rows))
        elif colpick == "maxres":
            keep = max(res, key=lambda c: len(colrows[c] & live_rows))
        elif colpick == "rand":
            keep = res[rng.integers(len(res))]
        else:
            keep = res[0]
        for c in res:
            if c != keep:
                gap.append(c)
                live_cols.discard(c)
        if verbose:
            print("  stall: row %d deg %d -> gap += %s" % (i, d, [c for c in res if c != keep]))
    gaprows = sorted(live_rows)
    return dict(tri=tri, gap=sorted(gap), gaprows=gaprows,
                ntri=len(tri), ngap=len(gap))


def main():
    A1s, As, A2s = silicon_poly()
    A1m, A2m = F.poly_matrix()
    for name, A2 in (("silicon", A2s), ("media", A2m)):
        print("== %s ==" % name)
        best = None
        rng = np.random.default_rng(0)
        for pick in ("minres", "maxres", "first"):
            r = greedy(A2, pick)
            print("   %-8s tri=%d gap=%d gaprows=%d" % (pick, r["ntri"], r["ngap"], len(r["gaprows"])))
            if best is None or r["ngap"] < best["ngap"]:
                best = r
        for _ in range(300):
            r = greedy(A2, "rand", rng)
            if r["ngap"] < best["ngap"]:
                best = r
        print("   best over 300 random restarts: gap=%d tri=%d" % (best["ngap"], best["ntri"]))
        print("   gap cols:", best["gap"])
        print("   gap rows:", best["gaprows"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
