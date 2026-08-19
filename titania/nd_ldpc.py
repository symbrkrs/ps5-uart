#!/usr/bin/env python3
"""
nd_ldpc.py -- Titania (Marvell 88SS1098/1099) LDPC code: parity-check matrix.

Status: Solved and VERIFIED against real media.  H*(c1^c2) == 0 exactly (all
1968 checks) for 26 pairs of real codewords read off the PS5 NAND.

THE CODE ("EE3")
  quasi-cyclic LDPC, circulant size Z = 48
  base matrix Mb x Nb = 41 x 385 block rows/cols, 2861 circulants
  row weight 69..70 ; column weight 18 (cols 0..71) and 5 (cols 72..384)
  mother codeword 385*48 = 18480 bits = 2310 B
  parity          41*48  =  1968 bits =  246 B
  mother info    344*48  = 16512 bits = 2064 B
  one 48-bit information block column (mother col 343) is shortened to zero and
  not stored, so the on-media codeword is 384*48 = 18432 bits = 2304 B with
  2058 B of information.  8 codewords = one 18432-byte page.

Table format (in the Titania firmware / Eldora HalLdpcSchDb.c)
  16 u16 header, then a stream of 2-u16 entries.
    Titania: (flag, code)      Eldora: (code, flag)
    code = colhi<<8 | delta
    col  = ((flag & 1) << 8) | colhi          9-bit block column index
    flag bit3 = end of layer (= end of a block row)
  * the 6-bit field is a delta rotation, not the circulant exponent *
  absolute exponent  s(i_k, j) = (d_1 + d_2 + ... + d_k) mod 48
  where i_1<i_2<...<i_w are the layers containing column j.  Per column the
  deltas sum to 0 mod 48 (verified: 385/385 columns), which is what makes a
  layered decoder's column memory return to its original rotational frame.

Bit mapping (verified)
  H block (i, j) = P^s where P is the 48x48 cyclic shift, and check row r of
  layer i touches bit ((r + s) mod 48) of block column j.
  block column c holds 6 bytes, MSB-first.
  AU block 0 (even codeword index) on-media layout:
      bytes[   0 .. 2055]  info block cols 0..341 + first 4 B of col 342
      bytes[2056 .. 2301]  parity -> block cols 344..384
      bytes[2302 .. 2303]  last 2 B of info block col 342
      block col 343        all-zero, not stored (shortened)
  AU block 1 (odd codeword index) layout is not yet pinned down; see report.

NOTE ON RAW READS
  Raw media is not a codeword: H*raw == H_info * S, a nonzero constant that
  depends only on the scrambler sequence S, not on the data.  Two codewords
  sharing S therefore XOR to a true codeword.  This also means H*raw yields
  1968 linear equations on the scrambler stream -- a direct route to the seed.
"""
import os, numpy as np

from . import nd_tables

Z = 48

#: The EE3 decoder's ECU config image.  The schedule is read out of it below,
#: so the code is not carried as a table of its own.
EE3_DEC_IMAGE = "ek_ee3_dec.bin"

#: What that image must yield.  A silently wrong H decodes silently wrong data,
#: so the shape is asserted rather than assumed.
EE3_MB, EE3_NB, EE3_ENTRIES = 41, 385, 2861

_EE3 = None


def ee3_layers():
    """EE3's 41 layers of (block column, absolute circulant exponent).

    The image is 16 header u16 followed by the (code, flag) entry stream this
    module already parses, so the derivation is just `parse_schedule` +
    `absolutize` -- and it reproduces the 41 x 2861 table exactly.
    """
    global _EE3
    if _EE3 is None:
        w = np.frombuffer(nd_tables.table(EE3_DEC_IMAGE).read_bytes(), "<u2")
        lay = absolutize([list(L) for L in parse_schedule(w, titania=False)], Z)
        nb = max(c for L in lay for c, _ in L) + 1
        n = sum(len(L) for L in lay)
        if (len(lay), nb, n) != (EE3_MB, EE3_NB, EE3_ENTRIES):
            raise ValueError(
                "%s: schedule parsed as %d layers / %d block columns / %d "
                "circulants, expected %d / %d / %d"
                % (EE3_DEC_IMAGE, len(lay), nb, n,
                   EE3_MB, EE3_NB, EE3_ENTRIES))
        _EE3 = [[tuple(e) for e in L] for L in lay]
    return _EE3


# ---------------------------------------------------------------- table parsing
def parse_schedule(words, titania=True, hdr=16, Zc=Z):
    """Parse a raw u16 schedule table into layers of (block_col, delta)."""
    body = list(words[hdr:])
    layers, cur, i = [], [], 0
    while i + 1 < len(body):
        a, b = int(body[i]), int(body[i + 1])
        code, flag = (b, a) if titania else (a, b)
        if flag not in (0, 1, 8, 9) or (code & 0xFF) >= Zc:
            break
        cur.append((((flag & 1) << 8) | (code >> 8), code & 0xFF))
        if flag & 8:
            layers.append(cur); cur = []
        i += 2
    return layers


def absolutize(layers, Zc=Z):
    """delta rotations -> absolute circulant exponents (inclusive cumulative)."""
    acc, out = {}, []
    for L in layers:
        row = []
        for c, d in L:
            acc[c] = (acc.get(c, 0) + d) % Zc
            row.append((c, acc[c]))
        out.append(row)
    return out


def check_delta_invariant(layers, Zc=Z):
    """Per-column deltas must sum to 0 mod Z.  Returns (ok_columns, total)."""
    tot = {}
    for L in layers:
        for c, d in L:
            tot[c] = tot.get(c, 0) + d
    return sum(1 for v in tot.values() if v % Zc == 0), len(tot)


def load_from_image(path, offset, titania=True):
    """Extract a schedule from a raw memory image (e.g. Titania DRAM dump).
    Titania EE3 decoder: load_from_image(ddr, 0x4A3908)   [VA 0x404A3908]"""
    buf = open(path, "rb").read()
    words = np.frombuffer(buf, dtype="<u2", offset=offset,
                          count=min(20000, (len(buf) - offset) // 2))
    return parse_schedule(words, titania=titania)


# --------------------------------------------------------------------- the code
class Code:
    def __init__(self, layers_abs, Zc=Z):
        self.Z = Zc
        self.layers = layers_abs
        self.Mb = len(layers_abs)
        self.Nb = max(c for L in layers_abs for c, _ in L) + 1
        cols, shs = [], []
        for L in layers_abs:
            for c, s in L:
                cols.append(c); shs.append(s)
        cols = np.asarray(cols); shs = np.asarray(shs)
        r = np.arange(self.Z)
        self.idx = (cols[:, None] * self.Z + (r[None, :] + shs[:, None]) % self.Z).astype(np.int32)
        self.starts = np.cumsum([0] + [len(L) for L in layers_abs])[:-1]
        self.n_bits = self.Nb * self.Z
        self.k_bits = (self.Nb - self.Mb) * self.Z

    def syndrome(self, v):
        """v: (Nb*Z,) uint8 bit vector of the mother codeword. -> (Mb, Z) uint8"""
        return np.bitwise_xor.reduceat(v[self.idx], self.starts, axis=0)

    def syndrome_weight(self, v):
        return int(self.syndrome(v).sum())


# ------------------------------------------------------- on-media bit placement
def mother_from_block0(cw2304):
    """AU block 0 (even codeword index): 2304 stored bytes -> mother bit vector."""
    a = np.frombuffer(cw2304, np.uint8)
    assert len(a) == 2304
    info = list(range(0, 2056)) + [2302, 2303]          # 2058 information bytes
    v = np.zeros(385 * Z, np.uint8)
    k = 0
    for c in range(344):
        if c == 343:                                     # shortened, stays zero
            continue
        v[c * Z:(c + 1) * Z] = np.unpackbits(a[info[k * 6:(k + 1) * 6]])
        k += 1
    for j in range(41):                                  # parity -> cols 344..384
        o = 2056 + 6 * j
        v[(344 + j) * Z:(345 + j) * Z] = np.unpackbits(a[o:o + 6])
    return v


def ee3():
    """The verified Titania EE3 code."""
    return Code(ee3_layers(), Z)


if __name__ == "__main__":
    import sys, itertools
    C = ee3()
    print(f"EE3: Z={C.Z} base {C.Mb}x{C.Nb}, {C.idx.shape[0]} circulants, "
          f"n={C.n_bits} k={C.k_bits} parity={C.Mb*C.Z} bits")
    if len(sys.argv) > 1:                    # nd_ldpc.py page1.bin [page2.bin ...]
        cws = []
        for p in sys.argv[1:]:
            raw = open(p, "rb").read()
            for i in range(len(raw) // 2304):
                a = np.frombuffer(raw[i * 2304:(i + 1) * 2304], np.uint8)
                if not (a == 0xFF).all() and not (a == 0).all() and i % 2 == 0:
                    cws.append((f"{os.path.basename(p)}/cw{i}", a))
        print(f"{len(cws)} even (AU block 0) codewords")
        ok = 0
        for (n1, a1), (n2, a2) in itertools.combinations(cws, 2):
            if not (a1 ^ a2).any():
                continue
            w = C.syndrome_weight(mother_from_block0((a1 ^ a2).tobytes()))
            if w == 0:
                ok += 1
                print(f"  VERIFIED  {n1} ^ {n2}: syndrome 0/{C.Mb*C.Z}")
        print(f"{ok} pairs with exactly zero syndrome")
