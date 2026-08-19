#!/usr/bin/env python3
"""fd_ring.py -- ring arithmetic in R = GF(2)[x]/(x^48-1) beyond fa_ring:
units, inverses, crt split into a = GF(2)[x]/((x+1)^16) and
B = GF(2)[x]/((x^2+x+1)^16), and the (1+x)-accumulator.  Offline."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from . import fa_ring as F

Z = 48
MASK = F.MASK
MOD = (1 << Z) | 1          # x^48 + 1


def pdeg(a):
    return a.bit_length() - 1


def pmul_full(a, b):
    r = 0
    while b:
        i = (b & -b).bit_length() - 1
        r ^= a << i
        b &= b - 1
    return r


def pdivmod(a, b):
    db = pdeg(b)
    q = 0
    while a and pdeg(a) >= db:
        s = pdeg(a) - db
        q ^= 1 << s
        a ^= b << s
    return q, a


def pgcd(a, b):
    while b:
        a, b = b, pdivmod(a, b)[1]
    return a


def egcd(a, b):
    """-> (g, u, v) with u*a + v*b = g over GF(2)[x]."""
    r0, r1 = a, b
    s0, s1 = 1, 0
    t0, t1 = 0, 1
    while r1:
        q, r = pdivmod(r0, r1)
        r0, r1 = r1, r
        s0, s1 = s1, s0 ^ pmul_full(q, s1)
        t0, t1 = t1, t0 ^ pmul_full(q, t1)
    return r0, s0, t0


def is_unit(a):
    return a != 0 and pgcd(a, MOD) == 1


def rinv(a):
    g, u, _ = egcd(a, MOD)
    assert g == 1, "not a unit"
    return pdivmod(u, MOD)[1] & MASK


def val1(a):
    """multiplicity of (x+1) in a (as a polynomial), or -1 for a == 0."""
    if a == 0:
        return -1
    k = 0
    while True:
        q, rem = pdivmod(a, 0b11)
        if rem:
            return k
        a = q
        k += 1


def val3(a):
    if a == 0:
        return -1
    k = 0
    while True:
        q, rem = pdivmod(a, 0b111)
        if rem:
            return k
        a = q
        k += 1


def accum(b):
    """the (1+x) pseudo-inverse a hardware serial accumulator computes:
    (1+x) . accum(b) = b  whenever wt(b) is even; accum(b) has bit 0 == 0."""
    run = 0
    out = 0
    for c in range(1, Z):
        run ^= (b >> c) & 1
        if run:
            out |= 1 << c
    return out


def rdiv1x(b):
    """solve (1+x).a = b ; returns the bit0==0 branch, or None if unsolvable."""
    if bin(b).count("1") & 1:
        return None
    return accum(b)


def rsolve(g, b):
    """solve g.a = b in R for g with val1(g) == 1, val3(g) == 0 (i.e.
    g = (1+x).u with u a unit).  Returns the branch whose bit 0 of u.a is 0."""
    u = pdivmod(g, 0b11)[0]
    assert is_unit(u), "g/(1+x) is not a unit"
    c = rdiv1x(b)
    if c is None:
        return None
    return F.pmul(rinv(u), c)
