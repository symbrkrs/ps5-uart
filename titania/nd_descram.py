#!/usr/bin/env python3
"""NAND scrambler: a 53-bit LFSR, and how to recover its keystream per row.

This is a sequencer-free route to plaintext. The hardware sequencer still refuses
to fetch descriptors, so the ECU's descrambling and ECC cannot be used -- but the
scrambler turns out to be recoverable directly from raw page reads.

WHAT IS ESTABLISHED
-------------------
The scrambler is a 53-bit LFSR over the page's bit stream, MSB-first within each
byte:

    seq[N] = seq[N-1] ^ seq[N-49] ^ seq[N-50] ^ seq[N-52] ^ seq[N-53]

Fitted by Berlekamp-Massey on CE6/CE7 row 0 over 0x280..0x800 it reproduces
11211 bits with zero mismatches. The bm implementation is validated separately
(exact width on known LFSRs of 16/31/53 bits, ~n/2 on random input, never 53).

Page layout is 8 codewords of 2048 data + 256 parity = 18432 bytes. The LFSR runs
continuously across each 2048-byte data chunk and restarts at the next one; bm
windows return 53 contiguously within a chunk and break at 0x800..0x900 and
0x1100..0x1200 on two independent rows.

The keystream is ce-independent and row-dependent:
  * CE6..CE11 return byte-identical raw data at row 0, as do CE2..CE5;
  * row 0 and row 0x800 give different sequences;
  * CE2's raw bytes at row 0 never agree with CE6's row-0 keystream -- literally
    0.0000 over 416 bytes, where chance is 1/256 -- because they are bitwise
    complements: CE2's plaintext there is 0xFF, not 0x00. Same keystream, other
    padding value.

THE DETECTOR
------------
Wherever plaintext is 0x00 (or 0xFF) the raw bytes are pure keystream (or its
complement), so bm over a window returns exactly 53. That classifies pages with
no key, no alignment and no known plaintext, and each hit yields the keystream at
that row. Validated: row 0 gives 53 on all eight reads across four CEs; data pages
give ~1536.

WHAT IS STILL MISSING
---------------------
The per-row seed. The keystream must be recovered from a page that happens to have
zero/0xFF plaintext at that row -- which is true of block-header pages but not of
image data pages. The seed is derived in silicon (the firmware decompilation has
no scrambler, seed or randomiser symbol at all), so the mapping row -> initial
state has to be reverse-engineered from samples. Two are in hand, at rows 0x000
and 0x800; their states are

    row 0x000  0x1CCDD2F2255CDE
    row 0x800  0x0F1E3403A81048

whose XOR has popcount 29, so it is not the row number loaded into the register.
Block-header pages at rows block<<11 give a systematic family of further samples.
"""
import sys
from pathlib import Path
import numpy as np

L = 53
TAPS = [1, 49, 50, 52, 53]
TAPS_INNER = [t for t in TAPS if t != L]
CHUNK = 0x800            # data bytes per codeword
PARITY = 0x100           # parity bytes per codeword
CODEWORD = CHUNK + PARITY
PAGE_TOTAL = 8 * CODEWORD        # 18432


def bm_poly(bits):
    """Berlekamp-Massey: (L, c) with bits[N] = sum_{i=1..L} c[i]*bits[N-i]."""
    n = len(bits)
    c = [0] * n
    b = [0] * n
    c[0] = b[0] = 1
    Lc, m = 0, -1
    for N in range(n):
        d = bits[N]
        for i in range(1, Lc + 1):
            d ^= c[i] & bits[N - i]
        if d:
            t = c[:]
            for i in range(N - m, n):
                c[i] ^= b[i - (N - m)]
            if Lc <= N // 2:
                Lc, m, b = N + 1 - Lc, N, t
    return Lc, c[:Lc + 1]


def linear_complexity(data):
    return bm_poly(np.unpackbits(np.asarray(data, dtype=np.uint8))
                   .astype(int).tolist())[0]


def is_pure_keystream(data, lo=0x680, hi=0x6C0):
    """True if this window is LFSR output, i.e. the plaintext there is constant."""
    if len(data) < hi:
        return False
    return linear_complexity(data[lo:hi]) == L


def keystream_from(data, lo, hi, nbytes=CHUNK):
    """Fit the LFSR on a known-constant-plaintext window and extend to the chunk.

    `lo`/`hi` bound a stretch whose plaintext is all 0x00. Returns `nbytes` of
    keystream from the start of the chunk, plus the 53-bit initial state.
    """
    bits = np.unpackbits(np.asarray(data[lo:hi], dtype=np.uint8)) \
        .astype(int).tolist()
    Lc, c = bm_poly(bits)
    if Lc != L or [i for i in range(1, Lc + 1) if c[i]] != TAPS:
        raise ValueError(f"not the scrambler LFSR here (L={Lc})")
    seq = bits[:]
    for _ in range(lo * 8):
        v = seq[L - 1]
        for t in TAPS_INNER:
            v ^= seq[L - 1 - t]
        seq.insert(0, v)
    state = int("".join(str(b) for b in seq[:L]), 2)
    return np.packbits(np.array(seq[:nbytes * 8], dtype=np.uint8)), state


def find_clean_window(data, win=0x100, step=0x40, hi=CHUNK):
    """Largest window in the chunk whose plaintext is constant, or None."""
    runs = []
    cur = None
    for o in range(0, hi - win + 1, step):
        ok = linear_complexity(data[o:o + win]) == L
        if ok:
            cur = [o, o + win] if cur is None else [cur[0], o + win]
        elif cur:
            runs.append(tuple(cur))
            cur = None
    if cur:
        runs.append(tuple(cur))
    return max(runs, key=lambda r: r[1] - r[0]) if runs else None


def descramble_chunk(data, lo=None, hi=None):
    """Descramble one 2048-byte chunk, fitting the keystream from within it."""
    if lo is None:
        w = find_clean_window(data)
        if w is None:
            raise ValueError("no constant-plaintext window; keystream unknown")
        lo, hi = w
    ks, state = keystream_from(data, lo, hi)
    n = min(len(data), CHUNK)
    return np.asarray(data[:n], dtype=np.uint8) ^ ks[:n], state


def vote(paths):
    """Majority-vote several reads of the same page to remove raw read errors."""
    arrs = [np.frombuffer(Path(p).read_bytes(), dtype=np.uint8) for p in paths]
    n = min(len(a) for a in arrs)
    st = np.stack([a[:n] for a in arrs]).astype(np.int32)
    return np.array([np.bincount(st[:, i], minlength=256).argmax()
                     for i in range(n)], dtype=np.uint8)


def main():
    """nd_descram.py <raw page file> [more reads of the same page ...]"""
    if len(sys.argv) < 2:
        print(__doc__)
        print("usage: nd_descram.py <raw.bin> [raw2.bin ...]")
        return
    data = vote(sys.argv[1:]) if len(sys.argv) > 2 else \
        np.frombuffer(Path(sys.argv[1]).read_bytes(), dtype=np.uint8)
    w = find_clean_window(data)
    print(f"{len(data)} bytes; constant-plaintext window: "
          + (f"{w[0]:#06x}..{w[1]:#06x}" if w else "none"))
    if not w:
        print("Cannot descramble: no window of constant plaintext in this chunk,")
        print("so the LFSR state for this row cannot be recovered from it alone.")
        return
    plain, state = descramble_chunk(data, *w)
    print(f"LFSR state at chunk start: {state:#015x}")
    out = Path(sys.argv[1]).with_suffix(".plain.bin")
    out.write_bytes(plain.tobytes())
    print(f"-> {out.name}")
    for o in range(0, 0x60, 16):
        txt = "".join(chr(x) if 32 <= x < 127 else "." for x in plain[o:o + 16])
        print(f"  {o:#05x}  {bytes(plain[o:o+16]).hex(' ')}  {txt}")


if __name__ == "__main__":
    main()
