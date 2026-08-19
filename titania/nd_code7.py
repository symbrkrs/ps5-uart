#!/usr/bin/env python3
"""Characterise code 7 -- the second on-die LDPC code.  Offline.

    py nd_code7.py

Banks 7 (enc) / 15 (dec) hold a code that is neither A10R nor EE3:
19 layers, 630 entries, 105 mother columns, every column weight 6, one shortened
column, so 104 stored columns = 624 B frame, 86 message blocks = 516 B (the same
message size as A10R) and 19 parity blocks = 114 B.

This runs A10R's checks against it: is it block-circulant over
GF(2)[x]/(x^48 - 1), is it rank-deficient in the same way, and does its kernel
have the same all-ones-polynomial structure?  If the answers match, the family
behaves uniformly and the A10R findings generalise rather than being a quirk.
"""
import struct
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import numpy as np

from . import nd_ldpc as L

Z = 48
from . import nd_tables                                                # noqa: E402

DEC7 = nd_tables.table("nd_ecutbl_ecc0dec_code0_rdsel0_bank15.bin")
NCOL, NLAY, NMSG = 105, 19, 86


def layers():
    b = DEC7.read_bytes()
    w = list(struct.unpack("<%dH" % (len(b) // 2), b))
    lay = L.parse_schedule(w, titania=False, hdr=16, Zc=Z)
    return L.absolutize([list(x) for x in lay], Z)


def build_H(lay, direction=+1):
    H = np.zeros((len(lay) * Z, NCOL * Z), np.uint8)
    for li, ent in enumerate(lay):
        for (col, sh) in ent:
            for t in range(Z):
                H[li * Z + t, col * Z + ((t + direction * sh) % Z)] = 1
    return H


def gf2_rank(M):
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


def main():
    lay = layers()
    print("code 7 schedule: %d layers, %d entries, %d columns"
          % (len(lay), sum(len(x) for x in lay),
             len(set(c for lz in lay for c, _ in lz))))
    cw = Counter(c for lz in lay for c, _ in lz)
    print("column weights: %s\n" % sorted(Counter(cw.values()).items()))

    H = build_H(lay)
    print("H: %s" % (H.shape,))

    # block-circulant?
    def is_circ(B):
        for r in range(1, Z):
            if not np.array_equal(B[r], np.roll(B[r - 1], 1)):
                return False
        return True
    tot = circ = zero = 0
    for i in range(NLAY):
        for j in range(NCOL):
            B = H[i * Z:(i + 1) * Z, j * Z:(j + 1) * Z]
            tot += 1
            if not B.any():
                zero += 1
            elif is_circ(B):
                circ += 1
    print("blocks: %d total, %d zero, %d circulant, %d NEITHER"
          % (tot, zero, circ, tot - zero - circ))

    r = gf2_rank(H)
    print("\nGF(2) rank of H: %d of %d rows  -> deficiency %d"
          % (r, H.shape[0], H.shape[0] - r))
    print("code dimension: %d - %d = %d bits = %.1f bytes"
          % (H.shape[1], r, H.shape[1] - r, (H.shape[1] - r) / 8))
    print("message is %d blocks = %d bits = %d bytes" % (NMSG, NMSG * Z, NMSG * 6))

    # parity part and its kernel, as for A10R
    A2 = H[:, NMSG * Z:]
    print("\nA2 (parity cols %d..%d): %s" % (NMSG, NCOL - 1, (A2.shape,)))
    rA2 = gf2_rank(A2)
    print("rank(A2) = %d of %d  -> kernel dim %d" % (rA2, A2.shape[0],
                                                     A2.shape[0] - rA2))
    if A2.shape[0] - rA2 == 1:
        print("  same 1-dimensional ambiguity as A10R")
    # is the all-ones polynomial J in the kernel?  test J on every parity block
    J = np.ones(Z, np.uint8)
    for nb in range(1, 4):
        pass
    # build candidate: J on each parity block in turn, see which sums annihilate
    cols = A2.shape[1] // Z
    contrib = []
    for j in range(cols):
        v = np.zeros(A2.shape[1], np.uint8)
        v[j * Z:(j + 1) * Z] = J
        contrib.append(((A2 @ v) & 1))
    same = [j for j in range(cols) if not contrib[j].any()]
    print("parity blocks where J alone is annihilated: %s" % same)
    print("(for A10R the kernel was J on 24 of 50 blocks -- a SUBSET sums to zero,"
          "\n which is why no single block shows up here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
