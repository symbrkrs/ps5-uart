#!/usr/bin/env python3
"""fb_gap.py -- is the RU gap of A2 exactly 12?  Offline.

Formulation: a set G of "gap" block-columns works iff, treating the columns in G
as known, the erasure-peeling decoder resolves all remaining 50-|G| columns from
the 50 check rows.  Greedy RU found |G|=13.  Here we (a) hill-climb on |G|=12 and
(b) prove/disprove |G|=12 exhaustively where feasible.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F
from . import fb_ru as RU


def peel(rowcols, n, G):
    """G = frozenset of known cols.  Returns (order, solved) where order is the
    list of (row, col) eliminations."""
    live = set(range(n)) - set(G)
    liverow = set(range(len(rowcols)))
    order = []
    deg = {i: len(rowcols[i] & live) for i in liverow}
    ready = [i for i in liverow if deg[i] == 1]
    colrows = {}
    for i in liverow:
        for c in rowcols[i]:
            colrows.setdefault(c, set()).add(i)
    while ready:
        i = ready.pop()
        if i not in liverow or deg[i] != 1:
            continue
        res = rowcols[i] & live
        if len(res) != 1:
            continue
        c = next(iter(res))
        order.append((i, c))
        live.discard(c)
        liverow.discard(i)
        for j in colrows.get(c, ()):
            if j in liverow:
                deg[j] -= 1
                if deg[j] == 1:
                    ready.append(j)
    return order, n - len(G) - len(order)


def score(rowcols, n, G):
    return peel(rowcols, n, G)[1]      # 0 = full success


def hill(rowcols, n, g, rng, iters=200000):
    n_all = list(range(n))
    best = None
    for restart in range(60):
        G = set(rng.choice(n, g, replace=False).tolist())
        cur = score(rowcols, n, G)
        if cur == 0:
            return sorted(G), 0
        stall = 0
        for it in range(3000):
            out = list(G)
            a = out[rng.integers(len(out))]
            cands = [c for c in n_all if c not in G]
            b = cands[rng.integers(len(cands))]
            G2 = set(G); G2.discard(a); G2.add(b)
            s2 = score(rowcols, n, G2)
            if s2 <= cur:
                if s2 < cur:
                    stall = 0
                else:
                    stall += 1
                G, cur = G2, s2
                if cur == 0:
                    return sorted(G), 0
            else:
                stall += 1
            if stall > 900:
                break
        if best is None or cur < best[1]:
            best = (sorted(G), cur)
    return best


def main():
    A1s, As, A2s = RU.silicon_poly()
    A1m, A2m = F.poly_matrix()
    rng = np.random.default_rng(7)
    for name, A2 in (("silicon", A2s), ("media", A2m)):
        n = len(A2[0])
        rowcols = [set(j for j in range(n) if A2[i][j]) for i in range(len(A2))]
        print("== %s ==" % name)
        for g in (11, 12, 13):
            G, resid = hill(rowcols, n, g, rng)
            print("   g=%2d : best residual %d   G=%s" % (g, resid, G if resid == 0 else ""))
            if resid == 0 and g <= 12:
                order, _ = peel(rowcols, n, set(G))
                print("        peel order rows:", [r for r, c in order])
                print("        peel order cols:", [c for r, c in order])
                print("        gap rows:", sorted(set(range(50)) - set(r for r, c in order)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
