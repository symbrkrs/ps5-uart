#!/usr/bin/env python3
"""fd_code7.py -- item 3: a full Richardson-Urbanke encoder for code 7
(banks 7/15), derived from the silicon schedule alone, in the fc_enc.py style.

Code 7: 19 layers, 105 mother block columns, 86 message / 19 parity, Z = 48,
every column weight 6.  header[1] = 7 = 19 - 12, so the gap is again g = 12 and
the triangle is 7 steps.

Everything is self-consistency-checked because no media using code 7 is known:
  * A2 is block-circulant, rank 911/912, kernel = J on the incidence-nullspace
    blocks
  * gap G = {0..11} peels with residual 0 and a 7-step triangle with monomial
    pivots
  * phi is 12x12, GF(2) rank 575 (forced)
  * Gauss-Jordan over R: 11 unit pivots + one pivot of (x+1)-valuation 1 ->
    serial accumulator, exactly as for A10R
  * encode random messages and verify H . c == 0 on the real GF(2) H, all
    912 checks, plus the two-solution structure (c and c + kappa both valid).
OFFLINE."""
import sys
import time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import numpy as np
from . import fa_ring as F
from . import fa_phi as P12
from . import fb_gap as GP
from . import fb_schur as SC
from . import fd_ring as RG
from . import fd_elim as EL
from . import nd_code7 as C7

Z = 48
MASK = F.MASK
NCOL, NLAY, NMSG = 105, 19, 86
NPAR = NCOL - NMSG          # 19


def wt(x):
    return bin(x).count("1")


def poly_matrix(H):
    out = []
    for i in range(NLAY):
        row = []
        for j in range(NCOL):
            B = H[i * Z, j * Z:(j + 1) * Z]
            v = 0
            for k in range(Z):
                if B[k]:
                    v |= 1 << k
            row.append(F.recip(v))
        out.append(row)
    return [r[:NMSG] for r in out], [r[NMSG:] for r in out]


def gf2_rank_np(M):
    piv = {}
    for r in np.packbits(M, axis=1):
        cur = int.from_bytes(r.tobytes(), "big")
        while cur:
            b = cur.bit_length() - 1
            if b in piv:
                cur ^= piv[b]
            else:
                piv[b] = cur
                break
    return len(piv)


def incidence_null(A2):
    n = len(A2)
    rows = []
    for i in range(n):
        v = 0
        for j in range(n):
            if A2[i][j]:
                v |= 1 << j
        rows.append(v)
    piv = {}
    for i, x in enumerate(rows):
        cur, sel = x, 1 << i
        while cur:
            b = cur.bit_length() - 1
            if b in piv:
                cur ^= piv[b][0]
                sel ^= piv[b][1]
            else:
                piv[b] = (cur, sel)
                break
    # right nullspace of the incidence matrix
    cols = []
    for j in range(n):
        v = 0
        for i in range(n):
            if A2[i][j]:
                v |= 1 << i
        cols.append(v)
    piv2 = {}
    null = []
    for j, x in enumerate(cols):
        cur, sel = x, 1 << j
        while cur:
            b = cur.bit_length() - 1
            if b in piv2:
                cur ^= piv2[b][0]
                sel ^= piv2[b][1]
            else:
                piv2[b] = (cur, sel)
                break
        else:
            null.append(sel)
    return null


def main():
    lay = C7.layers()
    H = C7.build_H(lay)
    print("code 7 H: %s   GF(2) rank %d" % (H.shape, gf2_rank_np(H)))
    A1, A2 = poly_matrix(H)
    print("A1 %dx%d   A2 %dx%d over R" % (len(A1), len(A1[0]), len(A2), len(A2[0])))
    nzA2 = sum(1 for r in A2 for x in r if x)
    mono = sum(1 for r in A2 for x in r if x and (x & (x - 1)) == 0)
    print("A2: %d non-zero blocks, %d of them monomial" % (nzA2, mono))

    # kernel from the block-incidence nullspace
    nul = incidence_null(A2)
    print("incidence nullspace dim %d" % len(nul))
    KBLK = sorted(j for j in range(NPAR) if (nul[0] >> j) & 1) if nul else []
    print("kernel blocks (J placed there):", KBLK)
    kap = [MASK if j in KBLK else 0 for j in range(NPAR)]
    chk = [0] * NLAY
    for i in range(NLAY):
        acc = 0
        for j in range(NPAR):
            if A2[i][j] and kap[j]:
                acc ^= F.pmul(A2[i][j], kap[j])
        chk[i] = acc
    print("A2 . kappa == 0 :", all(x == 0 for x in chk))

    # --- RU decomposition, gap = {0..11}
    G = list(range(12))
    rowcols = [set(j for j in range(NPAR) if A2[i][j]) for i in range(NLAY)]
    order, resid = GP.peel(rowcols, NPAR, set(G))
    print("\ngap G = 0..11 : peel residual %d, triangle %d steps" % (resid, len(order)))
    assert resid == 0
    gaprows = [r for r in range(NLAY) if r not in set(x for x, _ in order)]
    print("gap rows:", gaprows)
    print("triangle pivots all monomial:",
          all((A2[r][c] & (A2[r][c] - 1)) == 0 for r, c in order))
    phi = SC.build_phi(A2, G, order, gaprows)
    rk = P12.gf2_rank(P12.expand(phi))
    print("phi 12x12, GF(2) rank %d / 576, zero entries %d/144"
          % (rk, sum(1 for r in phi for x in r if x == 0)))
    kv = P12.nullvec(P12.expand(phi))
    kb = [(kv >> (Z * i)) & MASK for i in range(12)] if kv else []
    KG = [i for i in range(12) if kb and kb[i]]
    print("phi right-null blocks:", KG, " all-ones:",
          all(kb[i] == MASK for i in KG))

    # --- elimination
    piv, M, stalled, used = EL.elim(phi)
    print("\nGauss-Jordan over R: %d unit pivots, stalled columns %s"
          % (len(piv), stalled))
    fr = [i for i in range(12) if i not in used]
    print("unused rows:", fr)
    fc = stalled[0]
    g = M[fr[0]][fc]
    print("residual pivot g = %012x  wt %d  v1 %d  v3 %d"
          % (g, wt(g), RG.val1(g), RG.val3(g)))
    print("other non-zero cols in the residual row:",
          [j for j in range(12) if j != fc and M[fr[0]][j]])
    assert RG.val1(g) == 1 and RG.val3(g) == 0, "not a (1+x) pivot"
    u = RG.pdivmod(g, 0b11)[0]
    uinv = RG.rinv(u)
    Nfree = M[fr[0]][12:]
    rows = [(j, M[p][12:], M[p][fc]) for p, j in piv]

    # --- encoder
    def sh(a):
        return a.bit_length() - 1

    def rotm(a, s):
        return ((a << s) | (a >> (Z - s))) & MASK if s else a

    A1s = [[(j, sh(A1[i][j])) for j in range(NMSG) if A1[i][j]] for i in range(NLAY)]
    A2s = [[(j, sh(A2[i][j])) for j in range(NPAR) if A2[i][j]] for i in range(NLAY)]
    tri = [(r, c, [(j, s) for j, s in A2s[r] if j != c], (-sh(A2[r][c])) % Z)
           for r, c in order]
    gap = [(r, A2s[r]) for r in gaprows]

    def encode(Mblk, branch=0):
        s = []
        for i in range(NLAY):
            acc = 0
            for j, sft in A1s[i]:
                if Mblk[j]:
                    acc ^= rotm(Mblk[j], sft)
            s.append(acc)
        p = [0] * NPAR
        for r, c, rest, inv in tri:
            acc = s[r]
            for j, sft in rest:
                if p[j]:
                    acc ^= rotm(p[j], sft)
            p[c] = rotm(acc, inv)
        y = []
        for r, lst in gap:
            acc = s[r]
            for j, sft in lst:
                if p[j]:
                    acc ^= rotm(p[j], sft)
            y.append(acc)
        b = 0
        for j in range(12):
            if Nfree[j] and y[j]:
                b ^= F.pmul(Nfree[j], y[j])
        c0 = RG.accum(b)
        if branch:
            c0 ^= MASK
        xf = F.pmul(uinv, c0)
        p[fc] = xf
        for j, Nrow, coef in rows:
            acc = 0
            for k in range(12):
                if Nrow[k] and y[k]:
                    acc ^= F.pmul(Nrow[k], y[k])
            if coef and xf:
                acc ^= F.pmul(coef, xf)
            p[j] = acc
        for r, c, rest, inv in tri:
            acc = s[r]
            for j, sft in rest:
                if p[j]:
                    acc ^= rotm(p[j], sft)
            p[c] = rotm(acc, inv)
        return p

    print("\n--- verification against the real GF(2) H ---")
    rng = np.random.default_rng(5)
    ok = ok2 = solvable = 0
    N = 200
    t0 = time.time()
    for _ in range(N):
        Mblk = [int(rng.integers(0, 1 << 48)) for _ in range(NMSG)]
        for branch in (0, 1):
            p = encode(Mblk, branch)
            c = np.zeros(NCOL * Z, np.uint8)
            for j in range(NMSG):
                for k in range(Z):
                    if (Mblk[j] >> k) & 1:
                        c[Z * j + k] = 1
            for j in range(NPAR):
                for k in range(Z):
                    if (p[j] >> k) & 1:
                        c[Z * (NMSG + j) + k] = 1
            syn = (H @ c) & 1
            if branch == 0 and not syn.any():
                ok += 1
            if branch == 1 and not syn.any():
                ok2 += 1
        solvable += 1
    print("H . c == 0  (all %d checks):  branch0 %d/%d   branch1 %d/%d   (%.1fs)"
          % (H.shape[0], ok, N, ok2, N, time.time() - t0))

    # the two branches must differ by exactly kappa
    Mblk = [int(rng.integers(0, 1 << 48)) for _ in range(NMSG)]
    p0, p1 = encode(Mblk, 0), encode(Mblk, 1)
    diff = [p0[j] ^ p1[j] for j in range(NPAR)]
    print("branch0 ^ branch1 == kappa (J on blocks %s): %s" % (KBLK, diff == kap))

    # linearity / additivity  (accum is GF(2)-linear, so this must hold)
    nadd = neq = 0
    for _ in range(40):
        Ma = [int(rng.integers(0, 1 << 48)) for _ in range(NMSG)]
        Mb = [int(rng.integers(0, 1 << 48)) for _ in range(NMSG)]
        Mc = [Ma[i] ^ Mb[i] for i in range(NMSG)]
        pa, pb, pc = encode(Ma), encode(Mb), encode(Mc)
        nadd += all((pa[j] ^ pb[j]) == pc[j] for j in range(NPAR))
        Mr = [F.rot(x, 1) for x in Ma]
        pr = encode(Mr)
        neq += all(F.rot(pa[j], 1) == pr[j] for j in range(NPAR))
    print("additive over 40 pairs: %d/40  (must be 40 -- accum is GF(2)-linear)" % nadd)
    print("rotation-equivariant  : %d/40  (must NOT be 40 -- the standing theorem"
          " forbids an R-linear encoder)" % neq)

    # --- single-bit error: syndrome weight == column weight, unique column
    print("\n--- decoder round-trip ---")
    Mblk = [int(rng.integers(0, 1 << 48)) for _ in range(NMSG)]
    p = encode(Mblk)
    c = np.zeros(NCOL * Z, np.uint8)
    for j in range(NMSG):
        for k in range(Z):
            if (Mblk[j] >> k) & 1:
                c[Z * j + k] = 1
    for j in range(NPAR):
        for k in range(Z):
            if (p[j] >> k) & 1:
                c[Z * (NMSG + j) + k] = 1
    good = uniq = 0
    for pos in rng.choice(NCOL * Z, 40, replace=False):
        cc = c.copy()
        cc[pos] ^= 1
        syn = (H @ cc) & 1
        if syn.sum() == 6:
            good += 1
        match = [q for q in range(NCOL * Z) if np.array_equal((H[:, q] & 1), syn)]
        uniq += (match == [int(pos)])
    print("single-bit error -> syndrome weight 6 (= column weight): %d/40" % good)
    print("syndrome identifies the erred column UNIQUELY            : %d/40" % uniq)

    # --- Psi / d for code 7, derived exactly as for A10R
    from . import fd_derive as DV
    KBG = [i for i in range(12) if kb and kb[i]]
    Mt = DV.rowmap(phi)
    forced = 0
    for I in range(12):
        if I in KBG:
            continue
        e = [1 if j == I else 0 for j in range(12)]
        z, kd = DV.solve_gf2(Mt, DV.target(e))
        forced += z is not None
    print("\ncode 7: ring rows z^T.phi = e_I^T solvable for I outside K=%s : %d/%d"
          " (kernel dim 1, the harmless (J,..,J))"
          % (KBG, forced, 12 - len(KBG)))
    for I in KBG:
        e = [1 if j == I else 0 for j in range(12)]
        z, kd = DV.solve_gf2(Mt, DV.target(e))
        print("   I=%d in K : z^T.phi = e_I^T solvable = %s  (must be False --"
              " that row needs the +r^T offset)" % (I, z is not None))
        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
