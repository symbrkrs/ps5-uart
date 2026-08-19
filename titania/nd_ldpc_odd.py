#!/usr/bin/env python3
"""nd_ldpc_odd.py -- the odd (AU block 1) LDPC codeword layout.  Solved.

Until now only the even codewords (0,2,4,6) of a page were ever ECC-checked, so
half of every AU's user data -- block1[0:2046] -- was unverified.  This module
supplies the missing container map and checks it.

THE ANSWER
  The codeword is the same EE3 code (nd_ldpc.ee3(), Z=48, base 41x385) and the
  same conventions as the even codeword (bytes stored bitwise inverted, 6 bytes
  per block column MSB-first, parity in block columns 344..384 in order).  Only
  the container differs, and it differs in one structural way nobody guessed:

      * the odd codeword's information field starts in the even BLOCK *

  With b0 = ~block[2*k] and b1 = ~block[2*k+1] (both complemented):

      information (mother block columns 0..343, 2064 bytes) =
              b0[2302:2304]          2 bytes   <-- from the previous block
           ++ b1[   0:2052]       2052 bytes
           ++ 10 zero bytes       shortened, not stored
      parity     (block columns 344..384, 246 bytes) = b1[2052:2298]
      b1[2298:2304]                 6 bytes   spare, not part of the codeword

  Compare the even codeword, whose map is
      information = b0[0:2056] ++ 8 zero bytes ;  parity = b0[2056:2302] ;
      b0[2302:2304] spare with respect to CW0 -- because those two bytes are
      carried by CW1 instead.

  So an AU's 4110-byte payload stream (8 aux + 4096 user + 6 MCRC) is cut
  2056 / 2054 between the two codewords, and the cut does not fall on the
  2304-byte block boundary: it falls two bytes before the end of block 0.  The
  two codewords each shorten their information field to 2064 bytes, by 8 bytes
  and by 10 bytes respectively.

How it was found (and what was ruled out)
  * The published even-codeword model was re-derived first.  block0[2302:2304]
    is not part of CW0: feeding it into block column 342 gives weight 35, forcing
    those 16 bits to zero gives 0.  nd_ldpc.mother_from_block0()'s docstring is
    wrong on this point; nd_kpaudit.syn() is right.
  * A full sweep of "all 2058 non-parity bytes are information" over every parity
    start 0..2058 x every shortened column 0..343 x complement on/off (708k
    combinations, nd_oddprobe.py) reached a best weight of 879/1968 -- random.
    That model is wrong for the even codeword too, so it could not work here.
  * Under the correct zero-padded model, info = b0[2302:2304] ++ b1[0:2046] with
    parity at b1[2052:2298] fell to 90..135 of 1968.  A GF(2) solve for the 18
    remaining information bytes (144 unknowns, 1968 equations) was inconsistent,
    which ruled that variant out as stated.
  * The residual was then localised: it lived in exactly 10 of the 41 layers, in
    two groups of five with a constant weight inside each group.  A weight-5
    block column touches exactly 5 layers and contributes rot(e, s) to each, so
    that signature says "exactly two block columns are wrong".  The layer sets
    {2,6,14,17,39} and {0,8,16,25,29} identify them as columns 341 and 342, and
    rotating the syndrome back recovered their true contents: b1[2044:2050] and
    b1[2050:2052]++zeros.  i.e. the information simply runs on to b1[2052].
  * Ruled out along the way (all ~980-1010, i.e. random): information starting at
    b1[0] with no cross-block prefix, at any parity start; the 6 bytes at
    b1[2298:2304] as information; b1[2046:2052] as an MCRC placed after the
    parity; parity at b1[2046:2292].

Result over the whole dump (374 pages, 8 codewords each)
  even codewords : 1424 verified, 0 failed, 72 erased
  odd  codewords : 1415 verified, 9 failed, 72 erased
  All nine "failures" have syndrome weight exactly 5 or exactly 18, which are the
  two column weights of this code, and locate_errors() pins each of them to one
  flipped bit (five in the stored parity, four in user data).  They are real
  single-bit media errors -- exactly what the LDPC parity exists to correct --
  not a layout defect.  So the layout is confirmed on 1424/1424 odd codewords.

  Control: forcing the two cross-block prefix bytes to a constant instead of
  taking them from block0[2302:2304] gives 0 zeros out of 148 and a mean weight
  of 135.  The cross-block prefix is load bearing, not a coincidence.

CONSEQUENCE
  b1[2298:2304] -- 6 bytes per odd block, 24 per page -- is the only part of a
  page that no codeword covers.  Everything else, including all 4096 user bytes
  of every AU, is now ECC-verified.
"""
import sys
from collections import defaultdict
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
IMG = config.dump_path("img")
STRIDE = 2304
Z = 48
MASK = (1 << Z) - 1

from . import nd_ldpc as _ldpc                                         # noqa: E402

LAYERS = _ldpc.ee3_layers()
NB = max(c for L in LAYERS for c, _ in L) + 1          # 385
MB = len(LAYERS)                                       # 41
NINFO = NB - MB                                        # 344 information columns

# block column -> [(circulant shift, layer index)]
COLENT = [[] for _ in range(NB)]
for _i, _L in enumerate(LAYERS):
    for _c, _s in _L:
        COLENT[_c].append((_s, _i))


def _contrib(c, n):
    """Syndrome contribution of block column c holding the 48-bit value n.

    Layer i of a column with circulant P^s contributes rot_left(n, s); the whole
    1968-bit syndrome is packed with layer i at bit offset i*48."""
    if not n:
        return 0
    v = 0
    for s, i in COLENT[c]:
        v ^= (((n << s) | (n >> (Z - s))) & MASK) << (i * Z)
    return v


def _syndrome(info, parity):
    """info: <= 2064 bytes (zero padded).  parity: 246 bytes.  -> 1968-bit int.

    The reference form, kept because it is the readable one and because
    `syndrome_weights` is checked against it.  It is also slow -- ~3850
    `_contrib` calls of Python big-integer shifting per codeword, 1.2 ms -- so
    nothing on a hot path should call it.  Use `syndrome_weights`, which is the
    same matrix gathered in numpy and agrees exactly.
    """
    b = bytes(info).ljust(NINFO * 6, b"\0")
    v = 0
    for c in range(NINFO):
        v ^= _contrib(c, int.from_bytes(b[c * 6:c * 6 + 6], "big"))
    for j in range(MB):
        v ^= _contrib(NINFO + j, int.from_bytes(parity[j * 6:j * 6 + 6], "big"))
    return v


_NP_IDX = None


def _np_tables():
    """(gather index, layer starts) for the numpy syndrome.  Built once."""
    global _NP_IDX
    if _NP_IDX is None:
        import numpy as _np
        cols, shs = [], []
        for L in LAYERS:
            for c, s in L:
                cols.append(c)
                shs.append(s)
        cols = _np.asarray(cols)
        shs = _np.asarray(shs)
        r = _np.arange(Z)
        idx = (cols[:, None] * Z + (r[None, :] + shs[:, None]) % Z).astype(
            _np.int32)
        starts = _np.cumsum([0] + [len(L) for L in LAYERS])[:-1]
        _NP_IDX = (_np.ascontiguousarray(idx.ravel()), starts, idx.shape)
    return _NP_IDX


_RUST = None


def _rust():
    """The Rust worker module, or None.  Probed once and CACHED.

    The cache is not a micro-optimisation.  `from . import nd_ldpc_rs` inside a
    hot function is not free even when the module is already imported: the
    fromlist handling asks the import system whether `nd_ldpc_rs` is a submodule
    of the package, which stats the filesystem.  On a checkout reached over a
    network path -- a UNC checkout, which is how this is developed -- that
    measured 82% of a page's prepare, more than every syndrome and CRC in it put
    together.
    """
    global _RUST
    if _RUST is None:
        try:
            from . import nd_ldpc_rs
            _RUST = nd_ldpc_rs if nd_ldpc_rs.available() else False
        except Exception:                                   # noqa: BLE001
            _RUST = False
    return _RUST or None


def syndrome_weights(mothers):
    """Syndrome WEIGHT of each mother codeword.  -> numpy int array

    `mothers` is (n, NB*6) uint8 -- the mother codeword as BYTES, block column
    c at bytes 6c..6c+5, MSB first, which is the layout `_syndrome`'s
    `info ++ parity` already has.

    The same matrix as `_syndrome` and the same answer -- asserted against it in
    `tests/test_syndrome_weights.py` -- but gathered and XOR-reduced in numpy
    instead of built up as a 1968-bit Python integer.  That is the difference
    between 1.2 ms and 0.1 ms a codeword when a batch is handed over at once,
    and the ECC map's cost was almost entirely here: 10.7 ms of a 17.5 ms page.
    """
    import numpy as _np
    m = _np.ascontiguousarray(mothers, _np.uint8)
    if m.ndim == 1:
        m = m[None, :]
    v = _np.unpackbits(m, axis=1)
    rs = _rust()
    if rs is not None:
        try:
            return _np.asarray(rs.syndrome_weights(v), _np.int64)
        except Exception:                                   # noqa: BLE001
            pass                 # worker died mid-batch: numpy answers below
    flat, starts, shape = _np_tables()
    g = v[:, flat].reshape(m.shape[0], shape[0], shape[1])
    return _np.bitwise_xor.reduceat(g, starts, axis=1).sum(axis=(1, 2))


def _inv(b):
    return bytes(x ^ 0xFF for x in b)


def syn_even(block2304):
    """Syndrome weight of an even (AU block 0) codeword.  0 == verified.

    information = ~block[0:2056] ++ 8 zero bytes;  parity = ~block[2056:2302]."""
    b = _inv(block2304[:STRIDE])
    return bin(_syndrome(b[:2056], b[2056:2302])).count("1")


def syn_odd(block2304, prev_tail):
    """Syndrome weight of an odd (AU block 1) codeword.  0 == verified.

    block2304 : the odd block, raw as read off the media.
    prev_tail : the last two bytes of the even block of the same AU, raw
                (block[2*k][2302:2304]).  They are the first two information
                bytes of this codeword -- the odd codeword cannot be checked
                without them.

    information = ~prev_tail ++ ~block[0:2052] ++ 10 zero bytes
    parity      = ~block[2052:2298]
    ~block[2298:2304] is spare and not covered."""
    b = _inv(block2304[:STRIDE])
    return bin(_syndrome(_inv(prev_tail[-2:]) + b[:2052], b[2052:2298])).count("1")


def locate_errors(v):
    """Bits whose every parity check is unsatisfied -- for a syndrome of weight
    5 or 18 this is the single flipped bit.  -> [(block col, bit 0..47)]."""
    out = []
    for c in range(NB):
        ent = COLENT[c]
        for q in range(Z):
            # check row r = (q-s) mod Z ; packed bit index is i*Z + (47-r)
            if all((v >> (i * Z + Z - 1 - (q - s) % Z)) & 1 for s, i in ent):
                out.append((c, q))
    return out


def media_bit(c, q):
    """Mother (block column, bit) -> where it lives on media, for an odd block."""
    if c < NINFO:
        m = c * 6 + q // 8
        if m >= 2054:
            return f"shortened pad byte {m}"
        src = f"block0[{2302 + m}]" if m < 2 else f"block1[{m - 2}]"
    else:
        src = f"block1[{2052 + (c - NINFO) * 6 + q // 8}] (parity)"
    return f"{src} bit {7 - q % 8}"


def odd_syndrome(block2304, prev_tail):
    b = _inv(block2304[:STRIDE])
    return _syndrome(_inv(prev_tail[-2:]) + b[:2052], b[2052:2298])


def page_syndromes(page18432):
    """[(codeword index, weight or None if erased)] for one 18432-byte page."""
    out = []
    for cw in range(len(page18432) // STRIDE):
        blk = page18432[cw * STRIDE:(cw + 1) * STRIDE]
        if blk.count(0xFF) == STRIDE:
            out.append((cw, None))
        elif cw % 2 == 0:
            out.append((cw, syn_even(blk)))
        else:
            prev = page18432[(cw - 1) * STRIDE:cw * STRIDE]
            out.append((cw, syn_odd(blk, prev[2302:2304])))
    return out


def main():
    files = sorted(IMG.glob("img_ce*_b*_p*.bin"))
    if len(sys.argv) > 1:
        files = [Path(p) for p in sys.argv[1:]]
    print(f"LDPC verification of ALL EIGHT codewords of {len(files)} pages\n")
    tot = defaultdict(lambda: [0, 0, []])
    for p in files:
        raw = p.read_bytes()
        if len(raw) < 8 * STRIDE:
            continue
        for cw, w in page_syndromes(raw):
            if w is None:
                tot[cw][1] += 1
            elif w == 0:
                tot[cw][0] += 1
            else:
                tot[cw][2].append((w, p.name))
    print("     cw  parity   verified  erased   failed   worst weight")
    for cw in sorted(tot):
        ok, er, bad = tot[cw]
        worst = f"{max(w for w, _ in bad):9d}" if bad else "        -"
        print(f"     {cw}   {'even' if cw % 2 == 0 else 'ODD ':4s}   {ok:8d} "
              f"{er:7d} {len(bad):8d} {worst}")
    if any(tot[cw][2] for cw in tot):
        print("\n     residual failures, with the flipped bit located "
              "(column weight is 18 for cols 0..71, 5 for 72..384, so a\n"
              "     syndrome of weight 18 or 5 is one single-bit error):")
        for cw in sorted(tot):
            for w, n in tot[cw][2]:
                raw = (IMG / n).read_bytes()
                v = odd_syndrome(raw[cw * STRIDE:(cw + 1) * STRIDE],
                                 raw[cw * STRIDE - 2:cw * STRIDE])
                loc = locate_errors(v)
                where = "; ".join(f"col {c} bit {q} = {media_bit(c, q)}"
                                  for c, q in loc)
                print(f"       cw{cw} {n}: weight {w}/{MB*Z}  -> "
                      f"{where or 'NOT a single bit'}")
    odd_ok = sum(tot[c][0] for c in tot if c % 2)
    odd_bad = sum(len(tot[c][2]) for c in tot if c % 2)
    even_ok = sum(tot[c][0] for c in tot if c % 2 == 0)
    even_bad = sum(len(tot[c][2]) for c in tot if c % 2 == 0)
    print(f"\n     even: {even_ok} verified / {even_bad} failed")
    print(f"     odd : {odd_ok} verified / {odd_bad} failed")
    if odd_ok and not odd_bad:
        print("     -> the odd-codeword layout is confirmed; the whole dump is "
              "ECC-clean\n        except block1[2298:2304], which no codeword covers.")


if __name__ == "__main__":
    main()
