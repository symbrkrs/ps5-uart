#!/usr/bin/env python3
"""The NAND descrambler, in software.  No hardware, no seed map.

    py nd_descrambler.py --selftest

WHY THIS WORKS, AND WHY THE SEED MAP TURNED OUT TO BE UNNECESSARY
----------------------------------------------------------------
The scrambler is a 53-bit LFSR over the page bit stream, MSB-first within each
byte:

    seq[N] = seq[N-1] ^ seq[N-49] ^ seq[N-50] ^ seq[N-52] ^ seq[N-53]

Each physical row has its own 53-bit initial state, and for five sessions the
problem was framed as "recover the hardware's PBA -> state seed map", which
resisted every attack: not affine in the row, not affine in the block, not an
LFSR jump, dense ANF, and group 0's plaintext is AES ciphertext so no candidate
state can be scored.

The map is not needed.  Two facts collide:

  * the 8-byte AU aux field is stored at keystream index 0..7 -- it is the
    very first thing the keystream covers (NAND_DRIVER_NOTES.md sec 0c);
  * the 53-bit state is, by definition, the first 53 bits of the keystream.

So if the aux plaintext is known, the state falls straight out of the stored
bytes, for any row, with 11 bits left over as a free consistency check:

    state = first53( stored_aux XOR aux_plaintext )

And the aux plaintext is a per-image constant.  It was not guessed: it was
measured on the rows solvable by other means (known plaintext against the
reference firmware image, and the constant-plaintext padding tails), came out
unanimous, and then reproduced every one of those rows exactly.

    blocks 6,7  (group 0, fw_group 0)   aux = 03 00 00 00 00 00 00 00
    blocks 8,9  (group 1, fw_group 1)   aux = 06 00 00 00 00 00 00 00

Validation (all reproduced independently by this file's --selftest)
  1. Group 1 reconstructs to 2,097,152 bytes byte-identical to the reference
     firmware image, and begins with the container magic "SLB2".
  2. All 1472 recovered states satisfy the 11 spare aux bits, and every one of
     them equals `titania_nand.seed_state` for its row -- two solutions found
     years apart, sharing no code, agreeing bit for bit.
  3. The states lie in an affine subspace of GF(2)^53 of dimension exactly 32 --
     random or wrong states would give 53.  That confirms the old conjecture
     that the hardware seed register is 32 bits wide and expands affinely.

Group 0 is now fully descrambled.  It is still AES-encrypted underneath
(entropy 8.0000 bits/byte) -- descrambling was necessary, not sufficient.
"""
import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

L = 53
TAPS = (1, 49, 50, 52, 53)
MASK = (1 << L) - 1

# Aux plaintext per firmware group, indexed by NAND block.
#
# What this actually is (identified 2026-07-31): word 0 is
# FirmwareDataAreaProgramCountForGroupN -- a per-group program count that the
# writer bumps on each reprogram (`efc_cpu2.c:157095`), stamps into the 8-byte AU
# metadata of every AU (`:157102`), programs to media (`:157727`) and reads back at
# boot (`:161498`), publishing it as `FirmwareDataAreaProgramCountForGroup{0,1,2}`
# (`efc_cpu3.c:29364`). So these are not arbitrary tags -- group 0 has been
# programmed 3 times and group 1 six times on this board.
#
# Measured on media: constant across all four AUs of a page, all pages, and equal on
# both mirror halves (CE 0-5 and CE 6-11), which is what tells the firmware the
# mirrors are in sync. It is compared with equality, never ordering
# (`efc_cpu2.c:157563`), so it gates failover and repair -- it is not an A/B version
# selector. Being constant per group is exactly why it works as a descrambling crib.
AUX = {6: b"\x03\x00\x00\x00\x00\x00\x00\x00", 7: b"\x03\x00\x00\x00\x00\x00\x00\x00",
       8: b"\x06\x00\x00\x00\x00\x00\x00\x00", 9: b"\x06\x00\x00\x00\x00\x00\x00\x00"}


@lru_cache(maxsize=4096)
def keystream(state, nbytes):
    """MSB-first keystream bytes from a 53-bit state.

    Cached: one row's state is shared by all six CEs that stripe it, so extracting
    a whole image reuses each keystream six times.

    Bits b[0], b[1], ... come out MSB-first within each byte, and
        b[N] = b[N-1] ^ b[N-49] ^ b[N-50] ^ b[N-52] ^ b[N-53]   for N >= 53.
    The state is b[0..52], b[0] being its most significant bit -- so the state
    integer, read with b[N-1] in bit 0, is already the correct initial register.
    """
    r = state & MASK
    bits = [(state >> (L - 1 - i)) & 1 for i in range(L)]      # b[0..52]
    need = nbytes * 8
    while len(bits) < need:
        nb = (r ^ (r >> 48) ^ (r >> 49) ^ (r >> 51) ^ (r >> 52)) & 1
        bits.append(nb)
        r = ((r << 1) | nb) & MASK
    out = bytearray(nbytes)
    for i in range(nbytes):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i * 8 + j]
        out[i] = v
    return bytes(out)


def state_from_aux(stored_aux8, aux_plain8):
    """The state is the first 53 keystream bits; aux sits at keystream index 0."""
    x = int.from_bytes(bytes(a ^ b for a, b in zip(stored_aux8, aux_plain8)), "big")
    return x >> (64 - L)                      # keep the top 53 of 64 bits


def aux_check(stored_aux8, aux_plain8):
    """The 11 keystream bits past the state must be reproduced by that state.
    Returns True if consistent -- a free 11-bit validation on every row."""
    st = state_from_aux(stored_aux8, aux_plain8)
    ks = keystream(st, 8)
    return bytes(a ^ b for a, b in zip(stored_aux8, aux_plain8)) == ks


def descramble(payload, state):
    """XOR a contiguous keystream-aligned span with the keystream."""
    ks = keystream(state, len(payload))
    return bytes(a ^ b for a, b in zip(payload, ks))


# --------------------------------------------------------------------------
def _selftest():
    from . import nd_g0lib as G
    from .titania_nand import seed_state

    ok = True

    # ---- 1. aux-derived states vs the closed form --------------------------
    # Two independent routes to the same 53 bits: this module reads them out of
    # a page's aux field, `seed_state` computes them from the ROM constants.
    # They were solved years apart and share no code, so agreement is evidence.
    bad = miss = checked = 0
    for blk in (6, 7, 8, 9):
        for pg in range(G.NPAGE):
            p = G.slc_path(G.CES[0], blk, pg)
            if not p.exists():
                miss += 1
                continue
            raw8 = p.read_bytes()[:8]
            st = state_from_aux(raw8, AUX[blk])
            row = G.folded(G.row_of(blk, pg))
            checked += 1
            if not aux_check(raw8, AUX[blk]) or st != seed_state(row):
                bad += 1
    if checked:
        print(f"[1] aux-derived vs seed_state: {checked - bad}/{checked} rows agree"
              f"{f', {miss} pages absent' if miss else ''}")
        ok &= bad == 0
    else:
        print("[1] aux-derived vs seed_state: no group 0/1 pages present -- skipped")

    # ---- 2. affine rank of the state space ---------------------------------
    # The states are not 53 free bits: they span an affine subspace of dimension
    # 32, which is what says the hardware seed register is 32 bits wide and
    # expands affinely.  Run over the whole row space, not just the rows media
    # happens to cover.
    def _span(vs):
        base, basis = vs[0], []
        for v in vs[1:]:
            v ^= base
            for x in basis:
                v = min(v, v ^ x)
            if v:
                basis.append(v)
                basis.sort(reverse=True)
        return len(basis)

    dim = _span([seed_state(r) for r in range(0x4000)])
    print(f"[2] affine span dimension = {dim}  (expect 32; random -> 53)")
    ok &= dim == 32

    # ---- 3. end-to-end: group 1 vs the known reference ---------------------
    # a unit (one 16384-byte logical page of the image) is one NAND page: its 4
    # AUs each carry 8 aux bytes then 4096 user bytes, and all four share the same
    # keystream, so all four are descrambled with the same state from index 0.
    # Units are interleaved: unit = pg*12 + entry, entry = 2*(ce-6) + (blk & 1).
    from . import config
    ref = config.reference_firmware()
    if ref.exists():
        want = ref.read_bytes()
        nunits = len(want) // 16384
        got = bytearray(len(want))
        done = 0
        for u in range(nunits):
            pg, entry = divmod(u, 12)
            ce, blk = 6 + entry // 2, 8 + (entry & 1)
            p = G.slc_path(ce, blk, pg)
            if not p.exists():
                continue
            aus = G.au_payload(G.load_pages(ce, blk, [pg]))[0]   # (4, 4104)
            st = state_from_aux(bytes(aus[0][:8]), AUX[blk])
            ks = keystream(st, G.PAY)
            unit_bytes = bytearray()
            for k in range(4):
                pl = bytes(a ^ b for a, b in zip(bytes(aus[k]), ks))
                unit_bytes += pl[8:]                              # drop aux, 4096 B
            got[u * 16384:(u + 1) * 16384] = unit_bytes
            done += 1
        if done:
            same = sum(1 for i in range(len(want)) if got[i] == want[i])
            print(f"[3] group 1 vs {ref.name}: {same}/{len(want)} bytes match "
                  f"({100*same/len(want):.4f}%) over {done}/{nunits} units")
            print(f"    reconstructed head: {bytes(got[:4])!r}  (expect b'SLB2')")
            ok &= (same == len(want) and done == nunits)
        else:
            print("[3] group 1 vs reference: no group 1 pages present -- skipped")
    else:
        print(f"[3] reference firmware image ({ref.name}) not found -- skipped")

    print("\nSELF-TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
