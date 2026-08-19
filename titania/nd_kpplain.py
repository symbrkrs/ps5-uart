#!/usr/bin/env python3
"""Descramble a full raw page into its 16464-byte plaintext payload. Offline.

Now that the keystream span is settled -- one 53-bit state per row, reseeded to
the same value at each of the four AUs, running continuously across the two
codewords inside an AU -- a whole page follows from a single fitted state:

    page   = 8 codewords x 2304 bytes           = 18432 raw
    payload= 8 x 2058, parity 8 x 246 discarded = 16464 descrambled
    AU     = 2 codewords = 4116 bytes = 4096 user + 20 aux/MCRC

The keystream is therefore a 4116-byte block repeated four times down the page.
Verified on full_ce6/ce7 rows 0x0000 and 0x0800, all 8 codewords each.

No ECC is applied. On healthy media the descrambled payload already is the data;
LDPC parity only matters for correcting rare bit errors, and multiple reads voted
together (which this does) removes the read-path noise that dominates instead.

    py nd_kpplain.py <dump>/full_ce6_row0x0000_r0.bin [...]
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_kporbit import (L, PAYLOAD, STRIDE, clean_window, fit_state,
                        step, vote)
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


AU_CW = 2                       # codewords per AU; the LFSR reseeds every AU
# CORRECTED 2026-07-30.  The old constants here were U0 = 2056 at block0[0:2056] and
# U1 = 2040 at block1[0:2040].  That is wrong twice over: it swallows the 8-byte aux
# head into the user data and drops the two user bytes that WRAP the parity.  The
# verified layout (4096/4096 against the reference, 1304/1304 AUs rebuilt) is
#     user = b0[8:2056] ++ b0[2302:2304] ++ b1[0:2046]
# See NAND_DRIVER_NOTES.md section 0c for the whole byte map.
AUX = 8                         # b0[0:8], keystream 0..7
U0 = 2056                       # end of the first user run in block 0
WRAP = (2302, 2304)             # the 2 user bytes stored after block 0's parity
U1 = 2046                       # user bytes at the front of block 1


def keystream(state, nbytes):
    """Generate nbytes of keystream from a 53-bit state, MSB-first per byte."""
    out = bytearray()
    s = state
    bits = format(s, f"0{L}b")
    buf = [int(b) for b in bits]
    # the state is the next 53 output bits; keep stepping for the rest
    while len(buf) < nbytes * 8:
        s = step(s)
        buf.append(s & 1)
    return bytes(np.packbits(np.array(buf[:nbytes * 8], dtype=np.uint8)))


def descramble_page(raw):
    """Return (plaintext payload, state) for one 18432-byte raw page."""
    ncw = len(raw) // STRIDE
    # fit the state from whichever codeword offers a constant-plaintext window
    state = None
    for cw in range(0, ncw, AU_CW):
        frame = raw[cw * STRIDE:cw * STRIDE + PAYLOAD]
        w = clean_window(frame, 0, len(frame))
        if w:
            s, _ = fit_state(frame, *w)
            if s is not None:
                state = s
                break
    if state is None:
        return None, None
    ks = np.frombuffer(keystream(state, AU_CW * PAYLOAD), dtype=np.uint8)
    out = bytearray()
    for au in range(ncw // AU_CW):
        b0 = np.asarray(raw[(2 * au) * STRIDE:(2 * au + 1) * STRIDE],
                        dtype=np.uint8)
        b1 = np.asarray(raw[(2 * au + 1) * STRIDE:(2 * au + 2) * STRIDE],
                        dtype=np.uint8)
        if len(b0) < STRIDE or len(b1) < STRIDE:
            break
        # user data is not block[0:2058] twice: the scrambled span is split
        # around the parity, 2056 bytes at the front of block 0 and 2040 at the
        # front of block 1, which is exactly 4096.
        out += bytes(b0[:U0] ^ ks[:U0])
        out += bytes(b1[:U1] ^ ks[PAYLOAD:PAYLOAD + U1])
    return bytes(out), state


def aux_of(raw, state):
    """The real 8-byte aux field: 2 bytes then 6, both after the parity."""
    ks = np.frombuffer(keystream(state, AU_CW * PAYLOAD + 8), dtype=np.uint8)
    outs = []
    for au in range(len(raw) // (AU_CW * STRIDE)):
        b0 = np.asarray(raw[(2 * au) * STRIDE:(2 * au + 1) * STRIDE],
                        dtype=np.uint8)
        b1 = np.asarray(raw[(2 * au + 1) * STRIDE:(2 * au + 2) * STRIDE],
                        dtype=np.uint8)
        a = bytes(b0[2302:2304] ^ ks[2056:2058])
        b = bytes(b1[2298:2304] ^ ks[PAYLOAD:PAYLOAD + 6])
        outs.append(a + b)
    return outs


def main():
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        paths = sorted((config.dump_path()).glob("full_ce*_r0.bin"))
    for p in paths:
        reps = [p] + [q for q in (p.with_name(p.name.replace("_r0", "_r1")),)
                      if q.exists()]
        raw = vote(reps)
        plain, state = descramble_page(raw)
        if plain is None:
            print(f"{p.name}: no constant-plaintext window; state unknown")
            continue
        out = p.with_name(p.name.replace("_r0.bin", ".page.bin"))
        out.write_bytes(plain)
        # AU view: 4096 user + 20 aux per AU
        print(f"\n{p.name}  state {state:014x}  -> {out.name} ({len(plain)} B)")
        auxes = aux_of(raw, state)
        for au in range(len(plain) // 4096):
            user = plain[au * 4096:(au + 1) * 4096]
            nz = sum(1 for b in user if b)
            print(f"  AU{au}: user nonzero {nz:5d}/4096  "
                  f"aux {auxes[au].hex(' ') if au < len(auxes) else '-'}")
        for o in range(0, 0x40, 16):
            b = plain[o:o + 16]
            t = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
            print(f"    {o:#06x}  {b.hex(' ')}  {t}")


if __name__ == "__main__":
    main()
