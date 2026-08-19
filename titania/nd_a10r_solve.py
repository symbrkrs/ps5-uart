#!/usr/bin/env python3
"""A10R parity generation: solve A2 p = rhs, and settle the 1-bit ambiguity.

    py nd_a10r_solve.py

Offline.  Diagnostic + derivation; the result gets folded into titania_nand.

THE SITUATION
-------------
    H is (2400, 6528) over the 816-byte frame; message 516 B, parity 300 B.
    rank(H)  = 2399 of 2400   -> H is one check short of defining the code
    rank(A2) = 2399 of 2400   -> parity has a 1-dimensional kernel

So a message does not determine the parity outright: exactly two parities satisfy
every check we know, differing by the kernel vector k.  Media picks one.  This
finds which, and whether the choice follows a rule we can compute.
"""
import pickle
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import numpy as np

from . import nd_g2ldpc_a10r as A
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


NP = 300 * 8            # 2400 parity bits
NM = 516 * 8            # 4128 message bits
CACHE = HERE / "nd_a10r_solve.pkl"


def _ints(M):
    return [int.from_bytes(r.tobytes(), "big") for r in np.packbits(M, axis=1)]


def build(force=False):
    """-> (pivcols, solrows, kernel, consistency) for A2 p = rhs.

    Rows are carried as (A2 part << NP) | (selector of rhs), so eliminating the
    A2 side leaves each pivot row's low half as the linear functional of rhs that
    produces that parity bit -- i.e. a pseudo-inverse -- and any row whose A2 side
    vanishes becomes a consistency condition on rhs.
    """
    if CACHE.exists() and not force:
        return pickle.loads(CACHE.read_bytes())
    A2 = A.H()[:, NM:]
    rows = _ints(A2)
    aug = [(rows[r] << NP) | (1 << (NP - 1 - r)) for r in range(NP)]
    piv = {}
    cons = []
    for a in aug:
        cur = a
        while True:
            hi = cur >> NP
            if not hi:
                cons.append(cur & ((1 << NP) - 1))
                break
            b = hi.bit_length() - 1          # leading A2 column (from the low end)
            if b in piv:
                cur ^= piv[b]
            else:
                piv[b] = cur
                break
    # Back-substitution.  Forward elimination alone leaves the rows in echelon
    # but not reduced echelon, and the solution extraction below reads each pivot
    # row's low half as "the functional of rhs giving this parity bit" -- which is
    # only true once every pivot column is clear in all the other pivot rows.
    # Without this the solve produces a consistent-looking but wrong parity.
    for b in list(piv):
        for b2 in list(piv):
            if b2 != b and (piv[b2] >> (NP + b)) & 1:
                piv[b2] ^= piv[b]

    # kernel of A2: the one column with no pivot
    have = set(piv)
    freeb = [b for b in range(NP) if b not in have]
    ker = []
    for f in freeb:
        v = 1 << f
        for b, row in piv.items():
            if (row >> (NP + f)) & 1:
                v |= 1 << b
        ker.append(v)
    out = (piv, ker, cons)
    CACHE.write_bytes(pickle.dumps(out))
    return out


_A1 = None


def rhs_of(message516, slot):
    global _A1
    if _A1 is None:
        _A1 = _ints(A.H()[:, :NM])
    m = int.from_bytes(bytes(message516), "big")
    r = 0
    for i, row in enumerate(_A1):
        if bin(row & m).count("1") & 1:
            r |= 1 << (NP - 1 - i)
    off = int.from_bytes(np.packbits(A.offsets()[slot]).tobytes(), "big")
    return r ^ off


def candidates(message516, slot, built=None):
    """-> [parity_bytes, ...]  the (two) parities consistent with every check."""
    piv, ker, cons = built if built else build()
    rhs = rhs_of(message516, slot)
    for c in cons:
        if bin(c & rhs).count("1") & 1:
            return []                       # rhs not in the column space
    p = 0
    for b, row in piv.items():
        sel = row & ((1 << NP) - 1)
        if bin(sel & rhs).count("1") & 1:
            p |= 1 << b
    out = [p]
    for k in ker:
        out.append(p ^ k)
    return [x.to_bytes(NP // 8, "big") for x in out]


def main():
    built = build()
    piv, ker, cons = built
    print("A2 pivots %d, kernel dim %d, consistency conditions %d\n"
          % (len(piv), len(ker), len(cons)))

    # CE1, not CE0: CE0 is the dead channel and its pages are pure noise
    # (~1200 syndrome weight on every codeword).  The published A10R derivation
    # used CE1 rows 0..367 for exactly this reason.
    pages = [p for p in sorted((config.dump_path("g2")).glob("g2_ce1_*.bin"))
             if p.stat().st_size == A.PAGE_BYTES]
    if not pages:
        print("no group-2 pages -- cannot settle the ambiguity")
        return 1

    hits = {0: 0, 1: 0, None: 0}
    samples = []
    for p in pages[:8]:
        raw = np.frombuffer(p.read_bytes(), np.uint8)
        w = A.check_page(raw)
        for c in range(A.N_CHUNK):
            au = raw[c * A.AU_BYTES:(c + 1) * A.AU_BYTES]
            for j in range(4):
                if w[c * 4 + j] != 0:
                    continue
                fr = A.frame(au, j)
                want = bytes(fr[516:816])
                cand = candidates(bytes(fr[:516]), j, built)
                idx = next((i for i, x in enumerate(cand) if x == want), None)
                hits[idx if idx in (0, 1) else None] += 1
                if idx in (0, 1):
                    samples.append((bytes(fr[:516]), j, idx))
    print("which candidate matches media:")
    print("   particular solution (free bit 0) : %d" % hits[0])
    print("   particular ^ kernel              : %d" % hits[1])
    print("   neither                          : %d" % hits[None])

    total = hits[0] + hits[1] + hits[None]
    if total == 0:
        print("\nNO CLEAN CODEWORDS were reached -- this proves nothing either"
              "\nway.  Check the pages are from a live CE.")
        return 1
    if hits[None] == 0 and (hits[0] == 0 or hits[1] == 0):
        which = 0 if hits[1] == 0 else 1
        print("\nSETTLED: media always uses candidate %d -- encoding is"
              " deterministic." % which)
        (HERE / "nd_a10r_choice.txt").write_text(str(which))
        return 0
    print("\nThe choice VARIES, so it is a function of the message: fit a linear"
          "\nfunctional over the %d samples collected." % len(samples))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
