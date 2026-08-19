#!/usr/bin/env python3
"""fa_ring.py -- shared ring/QC utilities for the A10R equivariance work. Offline.

Everything here lives in the ORACLE's basis, i.e. the 816-byte stored frame used
by nd_g2ldpc_a10r / nd_a10r_solve, not the 822-byte mother frame of the silicon
H.  Reason: the ground truth (`nd_a10r_solve.candidates(m,slot)[t]`) is defined
in that basis, so mixing bases is the documented way to waste a day.

Frame:  136 block columns of Z=48 bits.  cols 0..85 message (516 B),
        cols 86..135 parity (300 B).  Block column c = frame bits 48c..48c+47,
        which are bytes 6c..6c+5 MSB first.

Ring convention.  A block is carried as a 48-bit int whose bit k is frame bit
(48c + k)  --  i.e. brev() of the big-endian byte value.  Multiplication `pmul`
is cyclic convolution.  Rotation by r is multiplication by x^r.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import numpy as np

Z = 48
MASK = (1 << Z) - 1
NBLK = 136
NMSG_BLK = 86
NPAR_BLK = 50
NM = 516 * 8
NP = 300 * 8

_BREV = None


def brev(v):
    """Reverse the 48 bits of v."""
    global _BREV
    if _BREV is None:
        t = [0] * 256
        for i in range(256):
            r = 0
            for b in range(8):
                if (i >> b) & 1:
                    r |= 1 << (7 - b)
            t[i] = r
        _BREV = t
    r = 0
    for i in range(6):
        r |= _BREV[(v >> (8 * i)) & 0xFF] << (8 * (5 - i))
    return r


def pmul(a, b):
    """Product in R = GF(2)[x]/(x^48-1)."""
    r = 0
    while b:
        i = (b & -b).bit_length() - 1
        r ^= ((a << i) | (a >> (Z - i))) & MASK if i else a
        b &= b - 1
    return r


def rot(a, r):
    r %= Z
    return ((a << r) | (a >> (Z - r))) & MASK if r else a


def sigma(a):
    """Coefficient sum of a polynomial (rotation invariant)."""
    return bin(a).count("1") & 1


def recip(a):
    """a(x) -> a(x^-1):  coefficient k moves to (-k) mod 48."""
    r = a & 1
    for k in range(1, Z):
        if (a >> k) & 1:
            r |= 1 << (Z - k)
    return r


def blocks_of(byts):
    """bytes -> list of 48-bit ring elements, one per block column."""
    n = len(byts) // 6
    return [brev(int.from_bytes(byts[6 * i:6 * i + 6], "big")) for i in range(n)]


def bytes_of(blks):
    return b"".join(brev(v).to_bytes(6, "big") for v in blks)


def rot_bytes(byts, r):
    return bytes_of([rot(v, r) for v in blocks_of(byts)])


# ---------------------------------------------------------------- H as polys

def poly_matrix(reciprocal=True):
    """-> (A1, A2) lists of polynomials over the oracle frame.

    A1 is 50 x 86 (message), A2 is 50 x 50 (parity).

    Derivation of the reciprocal convention (not a guess):  row s of a circulant
    block is row 0 rolled right by s, so H[iZ+s, jZ+k] = h[k-s].  The syndrome
    bit is  sum_k h[k-s] c[k] = sum_u h[u] c[u+s], a correlation, and that is the
    coefficient of x^s in  h(x^-1) * c(x).  Hence the block acts as h(x^-1).
    """
    from . import nd_g2ldpc_a10r as A
    H = A.H()
    out = []
    for i in range(NPAR_BLK):
        row = []
        for j in range(NBLK):
            B = H[i * Z, j * Z:(j + 1) * Z]
            v = 0
            for k in range(Z):
                if B[k]:
                    v |= 1 << k
            row.append(recip(v) if reciprocal else v)
        out.append(row)
    A1 = [r[:NMSG_BLK] for r in out]
    A2 = [r[NMSG_BLK:] for r in out]
    return A1, A2


def is_circulant(H, i, j):
    B = H[i * Z:(i + 1) * Z, j * Z:(j + 1) * Z]
    r0 = B[0]
    for k in range(1, Z):
        if not (B[k] == np.roll(r0, k)).all():
            return False
    return True
