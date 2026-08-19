#!/usr/bin/env python3
"""Fit the 53-bit LFSR state for every row already dumped, then look for structure.

Purely offline -- reads <dump>/sd_ce*_row*.bin, no hardware, no serial.

MODEL BEING TESTED
------------------
Harvest so far said two things that only fit together one way:

  * the state takes exactly two values per page, alternating with codeword parity;
  * the "odd" value is the "even" value advanced by 16464 bits = 2058 bytes
    = exactly one codeword payload.

A 53-bit LFSR cannot have period 32928, so the keystream is not a repeating
2-codeword pattern by virtue of the LFSR wrapping. The consistent reading is:

    the scrambler reseeds once per AU (= 2 codewords), from the PBA;
    all four AUs of a page get the same seed because they share the row;
    inside an AU the LFSR runs continuously across the codeword boundary,
    skipping the 246 parity bytes.

If that holds, one 53-bit value per row is the whole secret, and the keystream is
a 4116-byte pattern repeated four times down the page.

WHAT THIS SCRIPT ANSWERS
------------------------
1. state(row) for every row on disk, fitted independently by Berlekamp-Massey.
2. Does the odd state equal the even state advanced 16464 bits?  (per-AU model)
3. Is state affine in the row?  With rows 0..8 in hand the cheapest decisive test
   is  state(0)^state(1)^state(2)^state(3) == 0, which must hold for any affine
   map because 0^1^2^3 == 0.  Ditto (0,1,4,5), (0,2,4,6), (0,1,8,9).
4. If affine, solve for the basis: state(row) = M*row ^ state(0), and print the
   53-bit column for each row bit we can observe.

Codeword geometry is the measured one: 2058 payload + 246 parity = 2304 stride,
not the 2048+256 that nd_descram.py's constants still assume.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_descram import bm_poly, L, TAPS, TAPS_INNER
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


PAYLOAD = 2058          # bytes of scrambled payload per codeword
STRIDE = 2304           # payload + 246 parity
DUMP = config.dump_path()


def unpack(data):
    return np.unpackbits(np.asarray(data, dtype=np.uint8)).astype(int).tolist()


def fit_state(data, lo, hi):
    """BM-fit on [lo,hi) and back-extrapolate to the state at bit offset 0."""
    bits = unpack(data[lo:hi])
    Lc, c = bm_poly(bits)
    if Lc != L or [i for i in range(1, Lc + 1) if c[i]] != TAPS:
        return None, Lc
    seq = bits[:]
    for _ in range(lo * 8):
        v = seq[L - 1]
        for t in TAPS_INNER:
            v ^= seq[L - 1 - t]
        seq.insert(0, v)
    return int("".join(str(b) for b in seq[:L]), 2), Lc


def clean_window(data, base, span, win=0x100, step=0x40):
    """Largest window inside [base, base+span) whose plaintext is constant."""
    runs, cur = [], None
    for o in range(base, base + span - win + 1, step):
        if len(data) < o + win:
            break
        ok = bm_poly(unpack(data[o:o + win]))[0] == L
        if ok:
            cur = [o, o + win] if cur is None else [cur[0], o + win]
        elif cur:
            runs.append(tuple(cur))
            cur = None
    if cur:
        runs.append(tuple(cur))
    return max(runs, key=lambda r: r[1] - r[0]) if runs else None


# ---- LFSR state advance -------------------------------------------------------
# The state is the 53-bit window seq[k..k+52] packed with seq[k] as the MSB, which
# is exactly what fit_state() returns. Keep every advance consistent with that
# packing: a mismatched convention silently turns a true relation into "no".
MASK = (1 << L) - 1


def step(s):
    nb = 0
    for t in TAPS:                  # seq[53-t], at bit (L-1-(53-t)) = t-1
        nb ^= (s >> (t - 1)) & 1
    return ((s << 1) | nb) & MASK


def unstep(s):
    """Inverse of step(): the LFSR is invertible, so a chunk-1-only fit can be
    walked back to the chunk-0 state and still count as a sample of the row."""
    # seq[k-1] = seq[k+52] ^ seq[k+51] ^ seq[k+3] ^ seq[k+2] ^ seq[k]
    nb = ((s >> 0) ^ (s >> 1) ^ (s >> 49) ^ (s >> 50) ^ (s >> 52)) & 1
    return (s >> 1) | (nb << (L - 1))


def retreat(s, n):
    for _ in range(n):
        s = unstep(s)
    return s


def linmap(n):
    """The advance-by-n-bits map, as the images of the 53 basis vectors."""
    base = [step(1 << j) for j in range(L)]      # one bit
    r = [1 << j for j in range(L)]               # identity
    while n:
        if n & 1:
            r = compose(base, r)
        base = compose(base, base)
        n >>= 1
    return r


def compose(a, b):
    """a after b."""
    return [apply_map(a, b[j]) for j in range(L)]


def apply_map(m, x):
    out = 0
    j = 0
    while x:
        if x & 1:
            out ^= m[j]
        x >>= 1
        j += 1
    return out


def advance(s, n):
    return apply_map(linmap(n), s)


def gap(s_from, s_to, limit):
    """Brute-force the bit distance from one state to another, or None."""
    s = s_from
    for i in range(1, limit + 1):
        s = step(s)
        if s == s_to:
            return i
    return None


def vote(paths):
    arrs = [np.frombuffer(p.read_bytes(), dtype=np.uint8) for p in paths]
    n = min(len(a) for a in arrs)
    if len(arrs) == 1:
        return arrs[0]
    st = np.stack([a[:n] for a in arrs]).astype(np.int32)
    return np.array([np.bincount(st[:, i], minlength=256).argmax()
                     for i in range(n)], dtype=np.uint8)


def main():
    ce = sys.argv[1] if len(sys.argv) > 1 else "6"
    files = sorted(DUMP.glob(f"sd_ce{ce}_row*_r0.bin"))
    if not files:
        print(f"no sd_ce{ce}_row*_r0.bin in {DUMP}")
        return
    states, both = {}, {}
    print(f"{'row':>8}  {'cw0 state':>16}  {'cw1 state':>16}  window  cont?")
    for f0 in files:
        row = int(f0.name.split("row")[1].split("_")[0], 16)
        reps = [f0] + [p for p in (f0.with_name(f0.name.replace("_r0", "_r1")),)
                       if p.exists()]
        d = vote(reps)
        w0 = clean_window(d, 0, PAYLOAD)
        s0 = c0 = None
        if w0:
            s0, c0 = fit_state(d, *w0)
        # chunk 1 is fitted in its own frame: the LFSR does not run over the 246
        # parity bytes, so back-extrapolating chunk 1 to file offset 0 would be
        # meaningless. Slice at STRIDE and extrapolate to the chunk start.
        d1 = d[STRIDE:]
        w1 = clean_window(d1, 0, min(PAYLOAD, len(d1)))
        s1 = None
        if w1:
            s1, _ = fit_state(d1, *w1)
        cont = ""
        if s0 is not None and s1 is not None:
            # per-AU model: the LFSR runs straight from chunk 0 into chunk 1,
            # so chunk 1 starts exactly one payload (16464 bits) further on.
            g = gap(s0, s1, 6 * PAYLOAD * 8)
            cont = f"gap={g}" if g else "not on orbit"
            both[row] = (s0, s1)
        if s0 is not None:
            states[row] = s0
        elif s1 is not None:
            # chunk 1 fitted but chunk 0 did not: walk back one payload to get
            # the row's own state anyway. Same sample, recovered from the far end.
            states[row] = retreat(s1, PAYLOAD * 8)
            cont = f"back-stepped -> {states[row]:014x}"
        f0s = "none" if s0 is None else format(s0, "014x")
        f1s = "none" if s1 is None else format(s1, "014x")
        print(f"{row:#8x}  {f0s:>16}  {f1s:>16}  "
              f"{'-' if not w0 else f'{w0[0]:#06x}..{w0[1]:#06x}'}  {cont}"
              + ("" if s0 is not None else f"  (L={c0})"))

    print(f"\nfitted {len(states)} rows: {[hex(r) for r in sorted(states)]}")

    # self-test of unstep(): rows where both chunks fitted must round-trip
    for row, (a, b) in both.items():
        ok = retreat(b, PAYLOAD * 8) == a
        print(f"  unstep self-test row {row:#06x}: "
              f"{'ok' if ok else 'FAILED -- back-stepped samples are invalid'}")

    # --- affineness ------------------------------------------------------------
    # Any affine map over GF(2) satisfies: if a subset of inputs XORs to 0, the
    # corresponding outputs XOR to 0 too. So enumerate every subset of the fitted
    # rows whose row values XOR to 0 -- each one is a free, decisive test.
    from itertools import combinations
    print("\naffine tests -- subsets whose rows XOR to 0 must have states XOR to 0:")
    tested = 0
    rows = sorted(states)
    for k in range(2, min(len(rows), 6) + 1):
        for sub in combinations(rows, k):
            rx = 0
            for r in sub:
                rx ^= r
            if rx:
                continue
            sx = 0
            for r in sub:
                sx ^= states[r]
            tested += 1
            print(f"  {' ^ '.join(f'{r:#06x}' for r in sub)}"
                  f"  ->  {sx:014x}   "
                  f"{'AFFINE-CONSISTENT' if sx == 0 else 'NOT AFFINE'}")
    if not tested:
        print("  (no subset of the fitted rows XORs to zero)")

    # --- is state(row+1) simply the LFSR advanced by one page of payload? ---
    print("\nis state(row+1) just the LFSR carried on from state(row)?")
    if 0 in states and 1 in states:
        for name, nb in (("one page of payload  ", 8 * PAYLOAD * 8),
                         ("one AU of payload    ", 2 * PAYLOAD * 8),
                         ("one page incl parity ", 8 * STRIDE * 8)):
            adv = advance(states[0], nb)
            print(f"  state(0) + {name} = {adv:014x}"
                  f"  {'== state(1)  MATCH' if adv == states[1] else ''}")
        print(f"  state(1) measured        = {states[1]:014x}")
        g = gap(states[0], states[1], 40 * PAYLOAD * 8)
        print(f"  brute-forced gap state(0)->state(1): "
              f"{g if g else 'not within 40 payloads -- different orbit or reseeded'}")


if __name__ == "__main__":
    main()
