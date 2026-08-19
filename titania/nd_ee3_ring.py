#!/usr/bin/env python3
"""nd_ee3_ring.py -- the EE3 encoder as pure algebra.  Offline, no blobs.

    py nd_ee3_ring.py            # derive + self-check (~3 s)

WHAT THIS REPLACES
    `nd_deepparity_a2inv.pkl`, 489 KB holding the bitwise inverse of a 1968x1968
    GF(2) matrix.  It is not needed: EE3 is quasi-cyclic, so that matrix is a
    41x41 matrix over R = GF(2)[x]/(x^48-1) whose inverse is 41x41 = 10 KB of
    polynomials -- and even that is not stored, it is derived from H in 0.3 s.
    Expanding the derived ring inverse bitwise reproduces the discarded pickle
    row for row, all 1968 of them, so the replacement is exact on all 2^1968
    inputs rather than on a sample.  (`--vs-pkl` re-runs that comparison if you
    still have the file.)

The algebra, in full -- you can re-implement EE3 encoding from this paragraph
    EE3's parity-check matrix (`nd_ldpc.ee3_layers`, `nd_ldpc_odd.LAYERS`) is
    41 layers x 385 mother block columns, every entry a single circulant
    P^s of size Z = 48.  Carry a block as a 48-bit integer whose bit k is the
    coefficient of x^k; then `nd_ldpc_odd._contrib`'s rotate-left-by-s is
    multiplication by x^s, and H becomes a 41 x 385 matrix over
    R = GF(2)[x]/(x^48-1).  Split it as [A1 | A2] at column NINFO = 344:

        A1  41 x 344 over R   (information)     2656 monomial entries
        A2  41 x  41 over R   (parity)           205 monomial entries, 5/row

    A codeword satisfies  A1 . m + A2 . p = 0, so

        p = A2^-1 . A1 . m                                              (*)

    A2 is a unit of M_41(R): the 1968x1968 GF(2) matrix it induces is bijective
    (nd_deepparity's original observation), and the inverse of a bijective
    R-module map is again R-linear, so A2^-1 exists over R.  Computing it is a
    plain Gauss-Jordan over R -- the only subtlety is that R has zero divisors
    (x^48-1 = ((x+1)(x^2+x+1))^16), so a pivot must be a unit, not merely
    non-zero.  `a` is a unit iff gcd(a, x^48+1) = 1.  For EE3 every pivot that
    comes up is a unit on the first try (0 fixups), because every entry of A2
    starts as a monomial and enough of the structure survives elimination.

    R is a product of exactly two local rings, GF(2)[x]/((x+1)^16) and
    GF(2)[x]/((x^2+x+1)^16).  If some column of an invertible matrix has no unit
    entry it must have one entry outside each of the two maximal ideals, and
    their sum is then a unit -- so at most one row addition always unsticks a
    column.  `ring_inverse` does that automatically; A10R's A2, which is not
    invertible, needs the heavier Richardson-Urbanke treatment instead
    (fd_ruenc.py).

    Left multiplication by A2^-1 in (*) is applied to the syndrome of the
    information, which `_contrib` already computes, so `solve_parity` is a drop-in
    for the old one: same 246-byte output, same byte order.

  Trap that has bitten this codebase five times: GF(2) back-substitution must
  walk pivots ascending.  There is no back-substitution here at all -- the
  elimination is full Gauss-Jordan, clearing above and below every pivot -- so
  the ordering question does not arise.  Do not "fix" it into a triangular solve.

  Trap: this file's ring convention is bit k = coefficient of x^k, taken
  straight from `_contrib`.  It is not `fa_ring`'s, which works in the A10R
  stored-frame basis and uses the reciprocal of the extracted block.  The two
  are different bases for different codes; do not mix them.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from .nd_ldpc_odd import COLENT, NB, MB, NINFO, Z, MASK, _contrib
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


MOD = (1 << Z) | 1                       # x^48 + 1
NP_BITS = MB * Z                         # 1968


# ------------------------------------------------- GF(2)[x] and R arithmetic --
def pmul(a, b):
    """product in R = GF(2)[x]/(x^48-1) -- cyclic convolution."""
    r = 0
    while b:
        i = (b & -b).bit_length() - 1
        r ^= ((a << i) | (a >> (Z - i))) & MASK if i else a
        b &= b - 1
    return r


def _pmul_full(a, b):
    r = 0
    while b:
        i = (b & -b).bit_length() - 1
        r ^= a << i
        b &= b - 1
    return r


def _pdivmod(a, b):
    db = b.bit_length() - 1
    q = 0
    while a and a.bit_length() - 1 >= db:
        s = a.bit_length() - 1 - db
        q ^= 1 << s
        a ^= b << s
    return q, a


def _egcd(a, b):
    r0, r1, s0, s1 = a, b, 1, 0
    while r1:
        q, r = _pdivmod(r0, r1)
        r0, r1 = r1, r
        s0, s1 = s1, s0 ^ _pmul_full(q, s1)
    return r0, s0


def is_unit(a):
    """a is invertible in R iff gcd(a, x^48+1) == 1."""
    if not a:
        return False
    x, y = a, MOD
    while y:
        x, y = y, _pdivmod(x, y)[1]
    return x == 1


def rinv(a):
    g, u = _egcd(a, MOD)
    if g != 1:
        raise ValueError("not a unit: %012x" % a)
    return _pdivmod(u, MOD)[1] & MASK


# ------------------------------------------------------------ H as ring matrix --
def poly_matrices():
    """-> (A1, A2), H's information and parity halves over R.

    A1 is 41 x 344, A2 is 41 x 41.  Entry [i][c] = sum of x^s over every
    circulant P^s that layer i carries in block column c."""
    A1 = [[0] * NINFO for _ in range(MB)]
    A2 = [[0] * MB for _ in range(MB)]
    for c in range(NB):
        for s, i in COLENT[c]:
            if c < NINFO:
                A1[i][c] ^= 1 << s
            else:
                A2[i][c - NINFO] ^= 1 << s
    return A1, A2


def ring_inverse(A):
    """Gauss-Jordan inverse of a square matrix over R.  -> (inverse, n_fixups).

    Pivots must be units of R, not merely non-zero.  If a column offers none,
    two of its entries are summed first -- always enough, because R is a product
    of exactly two local rings (see the module docstring)."""
    n = len(A)
    M = [list(A[i]) + [1 if i == j else 0 for j in range(n)] for i in range(n)]
    where = [-1] * n
    used = set()
    todo = list(range(n))
    fixups = 0
    while todo:
        progress = False
        rest = []
        for j in todo:
            cand = [i for i in range(n) if i not in used and is_unit(M[i][j])]
            if not cand:
                free = [i for i in range(n) if i not in used]
                pair = next(((a, b) for x, a in enumerate(free)
                             for b in free[x + 1:] if is_unit(M[a][j] ^ M[b][j])),
                            None)
                if pair is None:
                    rest.append(j)
                    continue
                fixups += 1
                p, q = pair
                M[p] = [M[p][k] ^ M[q][k] for k in range(2 * n)]
                cand = [p]
            p = min(cand, key=lambda i: bin(M[i][j]).count("1"))
            used.add(p)
            where[j] = p
            iv = rinv(M[p][j])
            if iv != 1:
                M[p] = [pmul(iv, v) if v else 0 for v in M[p]]
            Mp = M[p]
            for i in range(n):
                if i == p:
                    continue
                f = M[i][j]
                if f:
                    Mi = M[i]
                    for k in range(2 * n):
                        if Mp[k]:
                            Mi[k] ^= pmul(f, Mp[k])
            progress = True
        todo = rest
        if not progress:
            raise ValueError("no unit pivot available in columns %s" % todo)
    return [[M[where[j]][n + i] for i in range(n)] for j in range(n)], fixups


# ------------------------------------------------------------------- the maps --
_RINV = None
_BITS = None


def a2_inverse_ring():
    """A2^-1 as a 41x41 matrix over R (10086 bytes if you ever store it)."""
    global _RINV
    if _RINV is None:
        _RINV, _fixups = ring_inverse(poly_matrices()[1])
    return _RINV


def a2_inverse():
    """A2^-1 expanded to the bitwise form nd_deepparity's pickle used to hold:
    1968 ints, `popcount(inv[k] & syndrome) & 1` = bit k of the parity stream.

    Syndrome bit layout is `_contrib`'s: layer i at offset i*Z, coefficient of
    x^b at i*Z + b.  Parity stream bit k = 48j+t is the coefficient of x^(47-t)
    of parity block j (the block is 6 bytes big-endian).  Hence the (j,i) cell
    of the bit matrix is the 48x48 Hankel circulant of INV[j][i], and rotating
    it by one t step is one cyclic shift -- which is all the loop below does."""
    global _BITS
    if _BITS is None:
        INV = a2_inverse_ring()
        rows = [0] * NP_BITS
        for j in range(MB):
            base = []
            for i in range(MB):
                c, B = INV[j][i], 0
                while c:
                    a = (c & -c).bit_length() - 1
                    B |= 1 << ((Z - 1 - a) % Z)
                    c &= c - 1
                base.append(B)
            for t in range(Z):
                m = 0
                for i, B in enumerate(base):
                    if B:
                        m |= (((B >> t) | (B << (Z - t))) & MASK if t else B) << (Z * i)
                rows[Z * j + t] = m
        _BITS = rows
    return _BITS


def syndrome_blocks(info_bytes):
    """information bytes (<= NINFO*6, zero padded) -> 41 ring elements."""
    b = bytes(info_bytes).ljust(NINFO * 6, b"\0")
    S = 0
    for c in range(NINFO):
        v = int.from_bytes(b[c * 6:c * 6 + 6], "big")
        if v:
            S ^= _contrib(c, v)
    return [(S >> (i * Z)) & MASK for i in range(MB)]


def solve_parity_ring(info_bytes):
    """p = A2^-1 . (A1 . m), done entirely in R.  -> the 246 parity bytes."""
    s = syndrome_blocks(info_bytes)
    INV = a2_inverse_ring()
    out = bytearray()
    for j in range(MB):
        acc = 0
        row = INV[j]
        for i in range(MB):
            if row[i] and s[i]:
                acc ^= pmul(row[i], s[i])
        out += acc.to_bytes(6, "big")
    return bytes(out)


def solve_parity(info_bytes, inv=None):
    """Drop-in replacement for nd_deepparity.solve_parity, blob-free.

    Mother information bytes (<= 2064, zero padded) -> the 246 true parity."""
    inv = inv if inv is not None else a2_inverse()
    b = bytes(info_bytes).ljust(NINFO * 6, b"\0")
    s = 0
    for c in range(NINFO):
        v = int.from_bytes(b[c * 6:c * 6 + 6], "big")
        if v:
            s ^= _contrib(c, v)
    x = 0
    for k in range(NP_BITS):
        if bin(inv[k] & s).count("1") & 1:
            x |= 1 << (NP_BITS - 1 - k)
    return x.to_bytes(NP_BITS // 8, "big")


# ------------------------------------------------------------------ selftest --
def _matmul(A, B):
    n = len(A)
    return [[_dot(A[i], [B[k][j] for k in range(n)]) for j in range(n)]
            for i in range(n)]


def _dot(a, b):
    r = 0
    for u, v in zip(a, b):
        if u and v:
            r ^= pmul(u, v)
    return r


def selftest(verbose=True):
    import os
    import time
    ok = True
    t = time.time()
    A1, A2 = poly_matrices()
    INV, fx = ring_inverse(A2)
    dt = time.time() - t
    ent = sum(1 for r in A2 for v in r if v)
    if verbose:
        print("A2 over R: %dx%d, %d non-zero entries, all monomials: %s"
              % (MB, MB, ent, all(bin(v).count("1") == 1
                                  for r in A2 for v in r if v)))
        print("A2^-1 derived from H in %.2f s, %d unit-pivot fixups" % (dt, fx))
    I = [[1 if i == j else 0 for j in range(MB)] for i in range(MB)]
    good = _matmul(A2, INV) == I and _matmul(INV, A2) == I
    if verbose:
        print("A2 . A2^-1 == A2^-1 . A2 == I over R: %s" % good)
    ok &= good

    # both solve paths must agree, and both must reproduce a random codeword's
    # own syndrome-zero property
    n = 0
    for k in range(6):
        info = b"\xff" * 2056 if k == 0 else os.urandom(NINFO * 6)
        p1 = solve_parity(info)
        p2 = solve_parity_ring(info)
        from .nd_ldpc_odd import _syndrome
        n += (p1 == p2) and _syndrome(info, p1) == 0
    if verbose:
        print("bitwise and ring solves agree AND give syndrome 0: %d/6" % n)
    ok &= n == 6
    return ok


def _vs_pkl():
    import pickle
    f = HERE / "nd_deepparity_a2inv.pkl"
    if not f.exists():
        print("nd_deepparity_a2inv.pkl is gone (that was the point) -- nothing "
              "to compare against")
        return True
    ref = pickle.loads(f.read_bytes())
    got = a2_inverse()
    bad = sum(1 for k in range(NP_BITS) if ref[k] != got[k])
    print("derived bit-inverse vs the retired pickle: %d/%d rows differ"
          % (bad, NP_BITS))
    return bad == 0


def _media(limit=250):
    """Regenerate the parity of real media codewords and compare byte for byte.

    Needs no seed and no plaintext: EE3 parity is over the complemented stored
    bytes, so a raw page is already a codeword.  Even codeword: information is
    ~block[0:2056], parity ~block[2056:2302].  Odd codeword: information is
    ~b0[2302:2304] ++ ~b1[0:2052], parity ~b1[2052:2298] (nd_ldpc_odd)."""
    from .nd_ldpc_odd import STRIDE, _inv, _syndrome
    files = sorted((config.dump_path("img")).glob("img_ce*.bin"))
    if not files:
        print("no <dump>/img pages -- skipped")
        return True
    ev = od = evb = odb = skip = 0
    for p in files[:limit]:
        raw = p.read_bytes()
        if len(raw) < 8 * STRIDE:
            continue
        for au in range(4):
            b0 = raw[au * 2 * STRIDE:(au * 2 + 1) * STRIDE]
            b1 = raw[(au * 2 + 1) * STRIDE:(au * 2 + 2) * STRIDE]
            if b0.count(0xFF) == STRIDE or b1.count(0xFF) == STRIDE:
                skip += 1
                continue
            i0, p0 = _inv(b0[:2056]), _inv(b0[2056:2302])
            if _syndrome(i0, p0):
                skip += 1                       # uncorrected media error
            else:
                ev += 1
                evb += solve_parity(i0) == p0
            i1 = _inv(b0[2302:2304]) + _inv(b1[:2052])
            p1 = _inv(b1[2052:2298])
            if _syndrome(i1, p1):
                skip += 1
            else:
                od += 1
                odb += solve_parity(i1) == p1
    print("media re-encode, %d pages: even %d/%d exact, odd %d/%d exact "
          "(%d erased or uncorrected, skipped)" % (min(limit, len(files)),
                                                   evb, ev, odb, od, skip))
    return evb == ev and odb == od and ev > 0


if __name__ == "__main__":
    r = selftest()
    if "--vs-pkl" in sys.argv:
        r &= _vs_pkl()
    if "--media" in sys.argv:
        r &= _media()
    print("nd_ee3_ring:", "PASS" if r else "FAIL")
    raise SystemExit(0 if r else 1)
