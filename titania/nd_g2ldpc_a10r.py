#!/usr/bin/env python3
"""nd_g2ldpc_a10r.py -- the PS5 Titania NAND "A10R" LDPC code (fw_group 2 /
firmware-image regions).  Offline only; never touches hardware.

Status: Solved and VERIFIED against real media.  H*c == s(slot) exactly, all 2400
checks, for 8218 of 8220 raw codewords read off five dies (CE 1..5).  The 2 misses
are raw pages with genuine uncorrected bit flips.

The code ("A10R", ECU code-select 1 / bank-select 3)
  quasi-cyclic LDPC, circulant size Z = 48 (same as EE3)
  mother codeword   822 B = 6576 bits = 137 x 48   (ECU ITR_CW_LEN0  = 822)
  parity            300 B = 2400 bits =  50 x 48   (ECU ITR_PARITY_LEN0 = 300)
  mother message    522 B = 4176 bits =  87 x 48
  one 48-bit information block column is shortened and not stored, so the stored
  frame is 136 x 48 = 6528 bits = 816 B, of which 516 B are information.
  base matrix on the stored frame: 50 x 136, 816 circulants
  column weight exactly 6 on every one of the 136 block columns
  row weight 15 (x3), 16 (x28), 17 (x19)
  rank(H) = 2399 (the usual even-column-weight dependency: all 2400 rows sum to 0)

On-media layout (AU = 3252 B, 5 AUs per 18432-byte page, rest 0xFF)
  cw0   AU+   0 .. 811    812 B = 512 data           + 300 parity
  cw1   AU+ 812 ..1623    812 B = 512 data           + 300 parity
  cw2   AU+1624 ..2435    812 B = 512 data           + 300 parity
  cw3   AU+2436 ..3251    816 B = 512 data + 4 aux   + 300 parity
        payload3 AU+2436..2947 ; AU MCRC AU+2948..2951 ; parity3 AU+2952..3251
  * the 4-byte AU MCRC precedes parity 3; it is part of cw3's message. *
  (nd_g2extract.py's AU_DIGEST_OFF = 3248 points at the tail of cw3's parity.)

Common frame used here -- 816 B, so all four slots live in one code:
      bytes   0..511   stored (scrambled) payload
      bytes 512..515   AU MCRC for slot 3, ZERO for slots 0..2
      bytes 516..815   the 300 stored parity bytes
  block column c = bytes 6c..6c+5, MSB first (as in EE3).
  block cols 0..85 are information (and form an information set); 86..135 parity.

THE ECC COVERS THE SCRAMBLED BYTES
  Unlike the group 0/1 note, the A10R parity is computed over the data as stored,
  i.e. After scrambling.  A raw read is therefore already a codeword -- decoding
  needs no LFSR seed at all, and the seed is only needed afterwards to turn the
  corrected payload into plaintext.

AFFINE OFFSET -- IT IS JUST THE COMPLEMENT CONVENTION (2026-08-05)
  The stored codewords form a coset: H*c equals a constant s depending only on the
  slot type (slots 0/1/2 share one, slot 3 has another).  That constant is not
  data, it is derived, and the derivation says what the coset means:

      off[3] = H . 1                                   (1 = all 6528 ones)
      off[0] = off[1] = off[2] = H . (1 with frame bytes 512..515 zeroed)

  i.e. the A10R codeword is the complement of the stored frame -- the same
  convention EE3 uses -- except that the 4 MCRC bytes, which exist on media only
  for slot 3, are 0x00 rather than 0xFF in the other three slots' mother frames.
  Corollary, checked: off[3] is the all-ones polynomial J on exactly the 22
  layers of odd block-row weight (row weights are 15 x3, 16 x28, 17 x19; 3+19 =
  22), and off[0]^off[3] is one block column's worth, weight 192 on 6 layers.
  Differences of two same-slot-type codewords satisfy H*(c1^c2) == 0 exactly.

  So `offsets()` computes rather than loads, and `H()` is rebuilt from the 6.7 KB
  base matrix instead of a 1.9 MB bit dump.  Both are verified byte-identical to
  the retired files by `py nd_g2ldpc_a10r.py --verify` (the files now live in
  attic/; the historical derivation scripts nd_g2ldpc_base/const/lin/sparse.py
  read them from there).
"""
import sys
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent.parent
Z = 48
NB = 816 * 8
NBLK = NB // Z              # 136
NMSG_BLK = 516 * 8 // Z     # 86
AU_BYTES = 3252
PAGE_BYTES = 18432
N_CHUNK = 5

_H = _OFF = None


def H():
    """(2400, 6528) uint8 parity-check matrix over the common 816-byte frame.

    Expanded from the base matrix: layer i, block column c with shift s puts a 1
    at (i*Z + t, c*Z + (t+s) mod Z) for every t.  Identical to the retired
    nd_g2ldpc_a10r_Hrows.npy -- 0 differing entries of 15667200."""
    global _H
    if _H is None:
        M = np.zeros((50 * Z, NB), np.uint8)
        for i, (cols, shifts) in enumerate(base_matrix()):
            for c, s in zip(cols, shifts):
                for t in range(Z):
                    M[i * Z + t, c * Z + ((t + s) % Z)] = 1
        _H = M
    return _H


def offsets():
    """(4, 2400) uint8 syndrome constants, indexed by slot.

    Derived, not stored -- see the module docstring: the codeword is the
    complement of the stored frame, so the offset is H times the all-ones vector,
    with the four MCRC bytes cleared for the slots that do not carry them."""
    global _OFF
    if _OFF is None:
        v = np.ones((4, NB), np.uint8)
        v[0:3, 512 * 8:516 * 8] = 0
        _OFF = (v @ H().T) & 1
    return _OFF


def base_matrix():
    """[(block_cols, shifts)] x 50; shifts are relative to each layer's first column."""
    import json
    from . import nd_tables
    d = json.load(open(nd_tables.table("nd_g2ldpc_a10r_base.json")))
    return [(tuple(c), tuple(s)) for c, s in d["layers"]]


def _verify():
    """Compare the derived H and offsets with the retired .npy files."""
    ok = True
    for name, got in (("Hrows", H()), ("offsets", offsets())):
        f = next((p for p in (HERE / ("nd_g2ldpc_a10r_%s.npy" % name),
                              HERE / "attic" / ("nd_g2ldpc_a10r_%s.npy" % name))
                  if p.exists()), None)
        if f is None:
            print("%-8s: retired file absent -- nothing to compare" % name)
            continue
        ref = np.load(f)
        if name == "Hrows":
            ref = np.unpackbits(ref, axis=1)[:, :NB]
        bad = int((ref != got).sum())
        print("%-8s: %d differing entries of %d  (%s)"
              % (name, bad, got.size, f.parent.name + "/" + f.name))
        ok &= bad == 0
    return ok


def frame(au, slot):
    """AU bytes (3252) + slot 0..3 -> the 816-byte common frame."""
    au = np.frombuffer(bytes(au), np.uint8) if not isinstance(au, np.ndarray) else au
    o = slot * 812
    v = np.empty(816, np.uint8)
    v[:512] = au[o:o + 512]
    if slot == 3:
        v[512:516] = au[2948:2952]
        v[516:] = au[2952:3252]
    else:
        v[512:516] = 0
        v[516:] = au[o + 512:o + 812]
    return v


def syndrome(frame816, slot):
    """(2400,) uint8 syndrome, already corrected by the affine offset.  Zero == valid."""
    b = np.unpackbits(np.asarray(frame816, np.uint8))
    s = (H() @ b) & 1
    return (s ^ offsets()[slot]).astype(np.uint8)


def check_page(raw):
    """raw 18432-byte group-2 page -> list of 20 syndrome weights (0 == clean)."""
    raw = np.frombuffer(bytes(raw), np.uint8)
    out = []
    for c in range(N_CHUNK):
        au = raw[c * AU_BYTES:(c + 1) * AU_BYTES]
        for j in range(4):
            out.append(int(syndrome(frame(au, j), j).sum()))
    return out


if __name__ == "__main__":
    if "--verify" in sys.argv:
        raise SystemExit(0 if _verify() else 1)
    print(__doc__)
    print(f"H {H().shape}, row weights {H().sum(1).min()}..{H().sum(1).max()}")
    for p in sys.argv[1:]:
        w = check_page(Path(p).read_bytes())
        print(f"{Path(p).name}: {sum(1 for x in w if x == 0)}/20 codewords clean; "
              f"syndrome weights {w}")
