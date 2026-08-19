#!/usr/bin/env python3
"""fd_ruenc.py -- Richardson-Urbanke encoders for both on-die LDPC codes,
derived from the parity-check matrix alone.  Offline, importable, no blobs.

    a10r_candidates(message516, slot)  -> [parity300, parity300]
    code7_parity_mother(msg_blocks86)  -> 19 parity blocks (mother frame)

Everything below is recomputed from H on first use (~1 s) and cached in memory.
The retired alternative was nd_a10r_solve.pkl, 1.1 MB of bitwise pseudo-inverse;
it is no longer imported by anything on the product path.


The derivation in FULL -- enough to re-implement without reading the code
------------------------------------------------------------------------
Ring.  Both codes are quasi-cyclic with Z = 48, so H is a matrix over
R = GF(2)[x]/(x^48-1) once a block is carried as a 48-bit integer.  Split H at
the message/parity boundary:  A1 . m + A2 . p = s(slot), with

    A10R    A1 50 x 86,  A2 50 x 50      code 7    A1 19 x 86,  A2 19 x 19

`s(slot)` is the affine offset of the stored frame (the shortened column carries
a fixed non-zero fill), zero for code 7 which has no media.

  Convention trap.  `fa_ring.poly_matrix` takes the reciprocal of each extracted
  block: row s of a circulant is row 0 rolled right by s, so the syndrome is a
  correlation, and the block acts as h(x^-1).  The as-extracted convention fails
  43 of 50 layers.  (nd_ee3_ring uses the opposite, x^+s, convention because it
  reads H through `_contrib` instead of through an expanded bit matrix.  Two
  codes, two bases -- mixing them is the documented way to lose a day.)

Why A2 cannot simply be inverted.  Every non-zero block of A2 is a single
shifted identity, so its polynomial is x^s, which evaluates to 1 at x = 1.  With
J = 1+x+...+x^47 and J.f = f(1).J, that gives  A2 . (J (x) u) = J . (Inc . u)
with Inc the binary block-incidence matrix.  Inc is singular for both codes
(nullity 1), so A2 has kernel kappa = J placed on the null block set --
24 blocks for A10R, 10 for code 7 -- and rank(H) is 2399 of 2400 (911 of 912).
The kernel is predictable from the incidence pattern in milliseconds; no
2400x2400 elimination is needed to find it.

Step 1, the RU peel.  Choose the gap G = parity block columns {0..11} and peel
the remaining columns greedily: repeatedly take a layer whose only unpeeled
non-gap column is a single column c, and assign that layer to c.  Residual 0 and
a triangle of 50-12 = 38 steps for A10R, 19-12 = 7 for code 7 -- and 38 and 7 are
literally header words [1] of the two ECU encoder tables.  g = 12 also without
the header: greedy alone stalls at 13, hill-climbing reaches 12, and g = 11 is
infeasible (best residual 24 of 39 over 60 restarts).  Every triangle pivot is a
monomial, i.e. a unit, so back-substitution needs no inverses.

Step 2, the Schur complement.  phi = B_gap + T_gap . T_tri^-1 . B_tri, 12x12
over R, fully dense (0 of 144 entries zero).  Its GF(2) rank is 575 of 576 --
forced, because rank(H) is one short and T is invertible.  Right null space is
kappa restricted to the gap, i.e. J on blocks K = {1,5,6,11}; left null space is
the all-ones covector.

Step 3, eliminate phi over R (fd_elim.elim).  Pivots must be units of R, not
merely non-zero, because x^48-1 = ((x+1)(x^2+x+1))^16 makes R a ring with zero
divisors.  A column with no unit pivot yet may acquire one after later columns
are cleared, so the elimination sweeps repeatedly until a pass makes no progress
-- a single pass stalls on 2 of 12 columns in the silicon basis.  Eleven columns
take a unit pivot.

Step 4, the last variable.  Exactly one column is left, with residual g of
(x+1)-valuation 1 and (x^2+x+1)-valuation 0, i.e. g = (1+x).u with u a unit.
Solving g.x = b is therefore: divide by (1+x), which is a running prefix XOR --
a serial accumulator, `fd_ring.accum` -- then multiply by u^-1.  (1+x) has a
one-dimensional kernel {0, J}, so there are two solutions, and they propagate to
two whole parities differing by kappa.  `a10r_candidates` returns both, in
accumulator-branch order.

  That non-R-linear accumulator is not a modelling shortcut, it is forced.  No
  12x12 matrix over R can implement the gap solve: any stored Q with phi.Q.y = y
  on all valid y forces phi.Q + I = z.1^T; being a ring matrix each block of z is
  0 or J; but 1^T.phi = 0 forces 1^T z = 1 while 1^T z = 48|u| = 0 mod 2.  See
  fd_derive.py.  Corollary: the 144-word tail of the ECU encoder table cannot be
  that matrix in any orientation.

Step 5, encode.  syndrome -> triangle back-substitution -> gap residual y ->
accumulator -> the 12 gap blocks -> re-run the triangle.  `_encode` below.

What H does not give you.  Which of the two branches the silicon writes.  Both
are genuine codewords (300/300 checked each), and Psi' = Psi + e_K . zeta^T is an
admissible encoder for free zeta, so the branch is provably not a function of H.
One bit of media evidence is irreducible; it is carried as the 72-byte covector
`titania_nand._A10R_W` with the rule <W,P> = 0, verified on 8218/8218 clean media
codewords from five dies.  Do not look for a way to compute it.
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from . import fa_ring as F
from . import fb_gap as GP
from . import fb_schur as SC
from . import fd_ring as RG
from . import fd_elim as EL

Z = 48
MASK = F.MASK
_CACHE = {}


def _prep(A1, A2, gapsize=12):
    npar = len(A2[0])
    G = list(range(gapsize))
    rowcols = [set(j for j in range(npar) if A2[i][j]) for i in range(len(A2))]
    order, resid = GP.peel(rowcols, npar, set(G))
    if resid:
        raise ValueError("gap 0..%d does not peel (residual %d)" % (gapsize - 1, resid))
    gaprows = [r for r in range(len(A2)) if r not in set(x for x, _ in order)]
    phi = SC.build_phi(A2, G, order, gaprows)
    piv, M, stalled, used = EL.elim(phi)
    if len(stalled) != 1:
        raise ValueError("expected exactly one non-unit pivot, got %s" % (stalled,))
    fc = stalled[0]
    fr = [i for i in range(12) if i not in used][0]
    g = M[fr][fc]
    if RG.val1(g) != 1 or RG.val3(g) != 0:
        raise ValueError("residual pivot is not (1+x).unit")
    uinv = RG.rinv(RG.pdivmod(g, 0b11)[0])

    def sh(a):
        return a.bit_length() - 1

    nmsg = len(A1[0])
    A1s = [[(j, sh(A1[i][j])) for j in range(nmsg) if A1[i][j]] for i in range(len(A1))]
    A2s = [[(j, sh(A2[i][j])) for j in range(npar) if A2[i][j]] for i in range(len(A2))]
    tri = [(r, c, [(j, s) for j, s in A2s[r] if j != c], (-sh(A2[r][c])) % Z)
           for r, c in order]
    gap = [(r, A2s[r]) for r in gaprows]
    rows = [(j, M[p][12:], M[p][fc]) for p, j in piv]
    return dict(nlay=len(A2), npar=npar, nmsg=nmsg, A1s=A1s, tri=tri, gap=gap,
                fc=fc, uinv=uinv, Nfree=M[fr][12:], rows=rows, phi=phi)


def _encode(P, mblocks, offb=None, branch=0):
    rot = F.rot
    s = []
    for i in range(P["nlay"]):
        acc = offb[i] if offb else 0
        for j, sft in P["A1s"][i]:
            if mblocks[j]:
                acc ^= rot(mblocks[j], sft)
        s.append(acc)
    p = [0] * P["npar"]
    for r, c, rest, inv in P["tri"]:
        acc = s[r]
        for j, sft in rest:
            if p[j]:
                acc ^= rot(p[j], sft)
        p[c] = rot(acc, inv)
    y = []
    for r, lst in P["gap"]:
        acc = s[r]
        for j, sft in lst:
            if p[j]:
                acc ^= rot(p[j], sft)
        y.append(acc)
    b = 0
    for j in range(12):
        if P["Nfree"][j] and y[j]:
            b ^= F.pmul(P["Nfree"][j], y[j])
    c0 = RG.accum(b)
    if branch:
        c0 ^= MASK
    xf = F.pmul(P["uinv"], c0)
    p[P["fc"]] = xf
    for j, Nrow, coef in P["rows"]:
        acc = 0
        for k in range(12):
            if Nrow[k] and y[k]:
                acc ^= F.pmul(Nrow[k], y[k])
        if coef and xf:
            acc ^= F.pmul(coef, xf)
        p[j] = acc
    for r, c, rest, inv in P["tri"]:
        acc = s[r]
        for j, sft in rest:
            if p[j]:
                acc ^= rot(p[j], sft)
        p[c] = rot(acc, inv)
    return p


def _a10r():
    if "a10r" not in _CACHE:
        from . import nd_g2ldpc_a10r as A
        A1, A2 = F.poly_matrix()
        P = _prep(A1, A2)
        off = A.offsets()
        P["offb"] = [[sum(1 << c for c in range(Z) if off[j][Z * i + c])
                      for i in range(50)] for j in range(4)]
        _CACHE["a10r"] = P
    return _CACHE["a10r"]


def a10r_candidates(message516, slot):
    """-> the two admissible 300-byte parities, in accumulator-branch order."""
    P = _a10r()
    mb = F.blocks_of(bytes(message516))
    return [F.bytes_of(_encode(P, mb, P["offb"][slot], br)) for br in (0, 1)]


def _code7():
    if "code7" not in _CACHE:
        from . import nd_code7 as N7
        from . import fd_code7 as C7
        H = N7.build_H(N7.layers())
        A1, A2 = C7.poly_matrix(H)
        _CACHE["code7"] = (_prep(A1, A2), H)
    return _CACHE["code7"]


def code7_parity_mother(msg_blocks, branch=0):
    """86 message blocks (mother frame) -> 19 parity blocks."""
    P, _H = _code7()
    return _encode(P, list(msg_blocks), None, branch)


def main():
    import pickle
    import numpy as np
    from . import fa_gen as GEN
    samples = pickle.loads((HERE / "nd_a10r_samples.pkl").read_bytes())
    Gm, Cm, kvec = GEN.build()
    msg = np.load(HERE / "fb_msgbits.npy")
    slots = np.array([sl for _, sl, _ in samples], np.int64)
    tt = np.array([x for _, _, x in samples], np.uint8)
    P0 = ((msg.astype(np.float32) @ Gm.astype(np.float32)) % 2).astype(np.uint8) ^ Cm[slots]
    PT = P0 ^ (tt[:, None] * kvec[None, :])
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    n = min(n, len(samples))
    ok = 0
    for i in range(n):
        truth = np.packbits(PT[i]).tobytes()
        if truth in a10r_candidates(samples[i][0], samples[i][1]):
            ok += 1
    print("a10r_candidates contains the hardware parity: %d / %d" % (ok, n))
    P, H = _code7()
    import numpy as np
    rng = np.random.default_rng(1)
    good = 0
    for _ in range(50):
        mb = [int(rng.integers(0, 1 << 48)) for _ in range(86)]
        p = code7_parity_mother(mb)
        c = np.zeros(105 * Z, np.uint8)
        for j in range(86):
            for k in range(Z):
                if (mb[j] >> k) & 1:
                    c[Z * j + k] = 1
        for j in range(19):
            for k in range(Z):
                if (p[j] >> k) & 1:
                    c[Z * (86 + j) + k] = 1
        good += not ((H @ c) & 1).any()
    print("code7_parity_mother satisfies H.c == 0: %d / 50" % good)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
