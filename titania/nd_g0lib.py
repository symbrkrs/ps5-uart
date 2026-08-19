#!/usr/bin/env python3
"""Seed-free primitives for structural analysis of the group 0/1 SLC dump.  Offline.

Everything here works on RAW media bytes and needs no LFSR state, because of the
residual identity

    r(b)[N] = b[N] ^ b[N-1] ^ b[N-49] ^ b[N-50] ^ b[N-52] ^ b[N-53]

r is linear over GF(2) and r(keystream) == 0, so r(raw) == r(plain) exactly.

The two things this module adds over nd_kpdup.py / nd_kpent.py:

1. It uses the whole contiguous keystream span of an AU, not just block0[0:2056].
   Per NAND_DRIVER_NOTES.md sec 0c the AU's scrambled payload is

       b0[0:2056] ++ b0[2302:2304] ++ b1[0:2046]   ==  ks[0:4104]

   i.e. 4104 contiguous keystream bytes, twice the span the old tools used.
   (b1[2046:2052] is the MCRC and is stored unscrambled; b1[2298:2304] is a
   post-scrambler copy of b1[0:6].  Neither belongs in the span.)

2. residual() is computed with byte-wise shifts instead of unpackbits, which is
   ~30x faster and lets the whole 8832-page dump be residualised in seconds.
   Output is 4097 whole bytes per 4104-byte span -- not packbits, so
   the trailing-partial-byte zero padding trap cannot occur.

Byte j of residual(P) holds stream bits 56+8j .. 63+8j.  Therefore for any two
byte-aligned windows of the same stream the residuals are the same bytes shifted
by the same amount: residual substring search is a plain byte search.
"""
import re
from pathlib import Path

import numpy as np
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
DUMP = config.dump_path()
SLC = DUMP / "slc"

STRIDE = 2304                    # stored bytes per LDPC block
PAGEB = 8 * STRIDE               # 18432 stored bytes per page
PAY = 4104                       # contiguous keystream bytes per AU
RES = PAY - 7                    # 4097 residual bytes per AU
L = 53
TAPS = (1, 49, 50, 52, 53)

GROUP0_BLOCKS = (6, 7)
GROUP1_BLOCKS = (8, 9)
CES = tuple(range(6, 12))
NPAGE = 368

_SLC_RE = re.compile(r"deep_ce(\d+)_b(\d+)_pg(\d+)\.bin$")


# ----------------------------------------------------------------- geometry
def slc_path(ce, blk, pg):
    return SLC / f"deep_ce{ce}_b{blk:02d}_pg{pg:03d}.bin"


def entry_of(ce, blk):
    """Interleave entry index inside a group-0/1 element.

    nand-group01-format: an element is 6 CEs x 2 blocks, ordered
    ce6b8 ce6b9 ce7b8 ce7b9 ... ce11b8 ce11b9.
    """
    return 2 * (ce - 6) + (blk & 1)


def unit_of(ce, blk, pg):
    """Linear unit (16384-byte logical page) index inside the group image."""
    return pg * 12 + entry_of(ce, blk)


def row_of(blk, pg):
    """Physical NAND row in SLC mode (row == page in block)."""
    return (blk << 11) | pg


def folded(row):
    """The scrambler ignores row bits 14 and 15."""
    return row & ~0xC000


# ----------------------------------------------------------------- payload
def au_payload(pages):
    """(N, 18432) uint8 -> (N, 4, 4104) uint8, the AU scrambled payload streams."""
    p = np.asarray(pages, np.uint8).reshape(-1, 8, STRIDE)
    b0 = p[:, 0::2, :]
    b1 = p[:, 1::2, :]
    out = np.empty((p.shape[0], 4, PAY), np.uint8)
    out[:, :, 0:2056] = b0[:, :, 0:2056]
    out[:, :, 2056:2058] = b0[:, :, 2302:2304]
    out[:, :, 2058:4104] = b1[:, :, 0:2046]
    return out


# ----------------------------------------------------------------- residual
_S7 = np.uint8(7)
_S6 = np.uint8(6)
_S5 = np.uint8(5)
_S4 = np.uint8(4)
_S3 = np.uint8(3)
_S2 = np.uint8(2)
_S1 = np.uint8(1)


def residual(P):
    """Byte-wise residual.  (..., n) uint8 -> (..., n-7) uint8.

    t = 8q+s contributes (b[j+6-q] << (8-s)) ^ (b[j+7-q] >> s):
        t=0  -> b[j+7]
        t=1  -> b[j+6]<<7 ^ b[j+7]>>1
        t=49 -> b[j+0]<<7 ^ b[j+1]>>1
        t=50 -> b[j+0]<<6 ^ b[j+1]>>2
        t=52 -> b[j+0]<<4 ^ b[j+1]>>4
        t=53 -> b[j+0]<<3 ^ b[j+1]>>5
    """
    P = np.asarray(P, np.uint8)
    m = P.shape[-1] - 7
    a0, a1, a6, a7 = (P[..., k:k + m] for k in (0, 1, 6, 7))
    r = a7.copy()
    r ^= np.left_shift(a6, _S7)
    r ^= np.right_shift(a7, _S1)
    r ^= np.left_shift(a0, _S7)
    r ^= np.right_shift(a1, _S1)
    r ^= np.left_shift(a0, _S6)
    r ^= np.right_shift(a1, _S2)
    r ^= np.left_shift(a0, _S4)
    r ^= np.right_shift(a1, _S4)
    r ^= np.left_shift(a0, _S3)
    r ^= np.right_shift(a1, _S5)
    return r


def residual_bits(buf):
    """Reference bit-level residual (nd_kpdup.py's), for self-tests only."""
    b = np.unpackbits(np.frombuffer(bytes(buf), np.uint8))
    r = b[53:].copy()
    for t in TAPS:
        r ^= b[53 - t:len(b) - t]
    return r


# ----------------------------------------------------------------- loading
def load_pages(ce, blk, pgs=None):
    """(len(pgs), 18432) uint8 for one (CE, block)."""
    pgs = range(NPAGE) if pgs is None else pgs
    return np.stack([np.fromfile(slc_path(ce, blk, p), np.uint8) for p in pgs])


def iter_slc(blocks=None, ces=None):
    """Yield (ce, blk, ndarray(368,18432)) for every requested (CE, block)."""
    for blk in (blocks or (6, 7, 8, 9)):
        for ce in (ces or CES):
            yield ce, blk, load_pages(ce, blk)


# ----------------------------------------------------------------- run finding
def zero_runs(row):
    """Maximal runs of zero bytes in a 1-D uint8 array -> [(start, length), ...]."""
    nz = np.flatnonzero(row)
    if nz.size == 0:
        return [(0, row.size)]
    out = []
    prev = -1
    for i in nz:
        if i - prev > 1:
            out.append((prev + 1, i - prev - 1))
        prev = i
    if row.size - prev > 1:
        out.append((prev + 1, row.size - prev - 1))
    return out


def longest_zero_run(R):
    """(N, m) uint8 -> (starts, lengths) of the longest zero-byte run per row."""
    N, m = R.shape
    nz = R != 0
    # position of the previous nonzero byte, per element
    idx = np.where(nz, np.arange(m)[None, :], -1)
    prev = np.maximum.accumulate(idx, axis=1)
    run = np.arange(m)[None, :] - prev            # length of the zero run ending here
    run = np.where(nz, 0, run)
    end = run.argmax(axis=1)
    ln = run[np.arange(N), end]
    return end - ln + 1, ln


# ----------------------------------------------------------------- self-test
def _selftest():
    rng = np.random.default_rng(1234)
    buf = rng.integers(0, 256, 4104, dtype=np.uint8)
    ref = np.packbits(residual_bits(buf)[3:])
    got = residual(buf)
    assert got.shape == (RES,), got.shape
    assert (got == ref[:RES]).all(), "byte-wise residual != bit-level reference"

    # r(keystream) must be identically zero
    import sys
    sys.path.insert(0, str(HERE))
    from .nd_kpplain import keystream
    ks = np.frombuffer(keystream(0x1CCDD2F2255CDE, 4104), np.uint8)
    assert not residual(ks).any(), "r(keystream) != 0"
    # and invariant under complement
    assert (residual(ks ^ 0xFF) == residual(ks)).all()
    assert (residual(buf ^ 0xFF) == residual(buf)).all(), "not complement-invariant"

    # slicing property: residual of a window == window of the residual
    assert (residual(buf[100:2000]) == residual(buf)[100:2000 - 7]).all()

    # 2-D matches 1-D
    M = rng.integers(0, 256, (7, 4104), dtype=np.uint8)
    assert (residual(M) == np.stack([residual(M[i]) for i in range(7)])).all()

    s, l = longest_zero_run(np.array([[1, 0, 0, 0, 5, 0, 0], [0, 0, 0, 0, 0, 0, 0]],
                                     np.uint8))
    assert list(s) == [1, 0] and list(l) == [3, 7], (s, l)
    print("nd_g0lib self-test OK")


if __name__ == "__main__":
    _selftest()
