#!/usr/bin/env python3
"""nd_deepparity.py -- why "deep" NAND pages never verified.  Solved.  Offline.

THE ANSWER
    Deep pages are read bit-inverted.  The whole 18432-byte page, data and
    parity alike, comes off the media as the ones' complement of what a
    shallow page gives.  Nothing about the AU format, the codeword stride,
    the parity length or the parity offset differs.

    raw_deep = ~raw_shallow_convention

    Fix the read, everything falls into place:
      * the LDPC syndrome drops from ~800/1968 to ~300, i.e. to the ~45 real
        bit errors a deep read actually has;
      * the min-sum corrector (nd_ldpc_fix) converges on every codeword in
        ~16 iterations and ~35 ms;
      * the corrected user data equals the reference firmware image bit
        for bit -- 0 errors
        in 16384 bytes on each of pages 4, 5 and 6;
      * the MCRC of all four AUs then checks.
    Deep pages are fully recoverable.  There is no missing layout.

Why it hid for so long -- the scrambler absorbed the inversion
    The scrambler is a 53-bit LFSR with taps [1,49,50,52,53] -- five taps, an
    odd number.  The all-ones state is therefore a fixed point, the all-ones
    sequence is in the LFSR's span, and

        keystream(seed ^ 0x1FFFFFFFFFFFFF) == ~keystream(seed)          (checked)

    So an inverted page still descrambles perfectly -- against the complementary
    seed.  The row->seed harvest fitted whatever seed made the plaintext come
    out, and for rows >= 8 that was silently the complementary seed.  Result:
    the user data looked right (only the genuine ~0.29% read BER showed) while
    the LDPC, which has no such symmetry because all-ones is not a codeword,
    looked like noise.  That is the whole "0.287% vs 7.5%" asymmetry: the data
    path had a hidden degree of freedom that soaked up the inversion and the
    parity path did not.

HOW IT WAS FOUND
    A2 (the 41x41 circulant block of H over parity columns 344..384, 1968x1968)
    is full rank, so the true parity is computable from the true information:
    p = A2^-1 A1 info.  Doing that for all 12 even codewords of pages 4,5,6 --
    information taken from the reference firmware image and the row seed,
    aux = 06 00*7 --
    gave a difference D = p_true ^ p_read of weight ~1020/1968 that was the same
    vector every time: pairwise XOR weight 0..3 bits across three pages and four
    AUs (those few bits are the real read errors).  A constant, not noise.

    Identifying D closed it.  D == ~P(all-ones information), so with 1 = all
    ones and P = A2^-1 A1,

        p_read = P(i) ^ 1 ^ P(1)  =>  ~p_read = P(~i)  =>  stored_p = P(stored_i)

    i.e. on deep pages the codeword is the stored bytes with no complement,
    while on shallow pages it is their complement.  Complementing the whole page
    turns one into the other.

EVIDENCE (main() reproduces all of it from <dump>/deep_raw and the reference
firmware image)
    page  raw syndromes (8 cw)              after inverting     after decoding
      0   0 0 0 0 0 0 0 0                   720 768 x4          --
      4   794 842 811 824 784 845 810 822   322 258 355 330 ..  0 0 0 0 0 0 0 0
      5   823 861 790 841 797 848 800 835   315 333 238 309 ..  0 0 0 0 0 0 0 0
      6   789 850 833 835 828 845 801 832   237 320 409 269 ..  0 0 0 0 0 0 0 0
    and the decoded user data matches the reference with 0 bit errors.
    Page 0 going 0 -> 720 under inversion is the control: the inversion is
    specific to rows >= 8, it is not a global convention change.

Negatives -- what this is not
    * not a different AU format.  The stride is 2304, the parity is 246 bytes at
      block[2056:2302] / block[2052:2298], the information is shortened by 8/10
      zero bytes, exactly as on shallow pages.  No offset sweep was needed.
    * Not a media/analogue problem.  A deep read does carry ~45 genuine bit
      errors per 2304-byte block (0.28% BER, uniform, stable -- only 231
      unstable bits in 147456 over 15 reads), which is consistent with the
      shared-word-line story, but that is 3.5x inside the corrector's ~160-bit
      capability and it decodes on the first try.  The 7.5% "parity error rate"
      was an artefact of comparing against the wrong convention.
    * Not a coset offset in the code, a rotated block column, a QC automorphism
      or a shifted parity.  Those were the standing hypotheses; the constant D
      is real but it is not intrinsic to H -- it is 1 ^ P(1), the fingerprint of
      a complement, and it disappears entirely once the page is inverted.
    * The residual error is not concentrated in the parity -- the opposite.
      Aggregated over 24 corrected codewords the flips are
          block bytes    0..2047   1135 / 394560 bits = 0.288%
          block bytes 2048..2303     19 /  47232 bits = 0.040%
      a clean 6.5x step exactly at offset 2048, flat on either side.  2048 + 256
      is the main/spare split of the media page (8 x 2048 = 16384 main,
      8 x 256 = 2048 spare), so each 2304-byte block is 2048 main bytes followed
      by its 256 spare bytes, and the deep-read degradation hits only the main
      array.  The parity happens to sit in the spare area (2056..2302), which is
      why it is the cleanest part of a deep page, not the dirtiest.  The
      "~7.5% parity BER" that started this investigation was entirely the
      complement artefact.

USING IT
    from .nd_deepparity import normalize, decode_page
    raw, inverted = normalize(raw_page)       # inverts if the page is deep
    fixed, info   = decode_page(raw)          # LDPC-corrects all 8 codewords
    # if inverted, the row's scrambler seed is seed_harvested ^ 0x1FFFFFFFFFFFFF
    # (equivalently: descramble `fixed` with ~keystream(seed_harvested))

    py nd_deepparity.py            # full evidence run over <dump>/deep_raw
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_ldpc_odd import (STRIDE, _syndrome, _inv, MB, NINFO, Z,
                          syndrome_weights)
from .nd_ldpc_fix import block_to_mother, mother_to_block, decode_mother
from .nd_kpplain import keystream
from .nd_kporbit import L, TAPS
from .nd_mcrc_solve import check_au
from . import config

HERE = Path(__file__).resolve().parent.parent
RAW = config.dump_path("deep_raw")
REF = config.reference_firmware()
ENTRIES = [(ce, b) for ce in range(6, 12) for b in (8, 9)]
AULEN, PAGE = 4116, 16384
SEED_FLIP = (1 << L) - 1                # 0x1FFFFFFFFFFFFF
AUX = bytes.fromhex("0600000000000000")


def row_of(page):
    return 2 * page if page <= 4 else 3 * page - 4


# ------------------------------------------------------------ the inversion
def _mother(info, parity):
    """`info ++ parity` as the mother codeword's bytes, zero-padded."""
    return np.frombuffer(
        bytes(info).ljust(NINFO * 6, b"\0") + bytes(parity), np.uint8)


def _weight(info, parity):
    """One syndrome weight, through the numpy path."""
    return int(syndrome_weights(_mother(info, parity))[0])


def syn_even_raw(b):
    """Syndrome of an even codeword without the usual complement."""
    return _weight(bytes(b[:2056]), bytes(b[2056:2302]))


def syn_even_cpl(b):
    return _weight(_inv(bytes(b[:2056])), _inv(bytes(b[2056:2302])))


def is_inverted(raw):
    """True if this page reads bit-inverted (a 'deep' page).

    Decided on codeword 0 alone: the wrong convention costs ~720 syndrome bits
    (that is weight(A2 * (1 ^ P(1))) ), so the two are never close."""
    b = np.asarray(raw, np.uint8)[:STRIDE]
    return syn_even_raw(b) < syn_even_cpl(b)


def normalize(raw):
    """(page in the shallow-page convention, was_inverted)."""
    a = np.frombuffer(raw, np.uint8) if isinstance(raw, (bytes, bytearray)) \
        else np.asarray(raw, np.uint8)
    if is_inverted(a):
        return ((~a) & 0xFF).astype(np.uint8), True
    return a.copy(), False


def deep_seed(seed):
    """Harvested seed -> the seed that goes with the inverted (normalized) page."""
    return seed ^ SEED_FLIP


# -------------------------------------------------- odd-codeword bit mapping
# The odd codeword's container is b0[2302:2304] ++ b1, i.e. a 2306-byte array u:
#   information mother byte m (0..2053) -> u[m]        (2054..2063 are shortened)
#   parity      mother byte j (0..245)  -> u[2054 + j] (== b1[2052 + j])
def _odd_mapping():
    n = 385 * Z
    byte = np.full(n, -1, np.int32)
    for c in range(343):
        for t in range(Z):
            b = 6 * c + t // 8
            if b < 2054:
                byte[c * Z + t] = b
    for j in range(MB):
        for t in range(Z):
            byte[(NINFO + j) * Z + t] = 2054 + 6 * j + t // 8
    bit = np.tile(np.arange(Z) % 8, 385).astype(np.int32)
    return byte, bit


_OB, _OBIT = _odd_mapping()
FREE_O = _OB >= 0
POS_O = _OB[FREE_O] * 8 + _OBIT[FREE_O]


def _u_of(raw, au):
    b0 = raw[au * 2 * STRIDE:(au * 2 + 1) * STRIDE]
    b1 = raw[(au * 2 + 1) * STRIDE:(au * 2 + 2) * STRIDE]
    return np.concatenate([b0[2302:2304], b1]).astype(np.uint8)


def correct_odd(u):
    """Correct the 2306-byte odd container.  -> (fixed u, flips, converged)."""
    bits = np.unpackbits((~u) & 0xFF)
    v0 = np.zeros(385 * Z, np.uint8)
    v0[FREE_O] = bits[POS_O]
    v, _, ok, _ = decode_mother(v0)
    if not ok:
        return u.copy(), 0, False
    bits[POS_O] = v[FREE_O]
    return ((~np.packbits(bits)) & 0xFF).astype(np.uint8), int((v != v0).sum()), True


def syn_odd_u(u):
    b = _inv(bytes(u))
    return _weight(b[:2054], b[2054:2300])


# --------------------------------------------------------------- whole page
def _decode_batch(v0s):
    """Decode independent mother vectors, in parallel where that is available.

    -> [(v, converged)] in order.  Falls back to the serial numpy decoder, one
    at a time, when the Rust worker is not there -- same answers, same order,
    only slower.  Nothing here changes what is decoded: the vectors handed in
    are independent by construction (see `decode_page`).
    """
    try:
        from . import nd_ldpc_rs
        if nd_ldpc_rs.available():
            return [(v, ok) for v, _it, ok, _st
                    in nd_ldpc_rs.decode_many(v0s)]
    except Exception:                                       # noqa: BLE001
        pass
    out = []
    for v0 in v0s:
        v, _it, ok, _st = decode_mother(v0)
        out.append((v, ok))
    return out


def decode_pages(raws):
    """`decode_page` over many pages at once, with the batches merged.

    -> [(fixed 18432 bytes, [(cw, flips, converged)] x8)] in the order given.

    The same work and the same answers as calling `decode_page` in a loop, in
    two round trips instead of two per page.  Pages are independent of one
    another, so every page's four EVEN codewords go in one batch and then every
    page's four ODD containers go in a second.  The phase order WITHIN a page is
    untouched -- phase 2 of all pages is still built after phase 1 of all pages
    has been corrected and written back, which is the ordering `decode_page`'s
    docstring explains and the only ordering there is.

    It exists because the corrector is a batch engine and one page is four jobs.
    A 12-page block scan is 48, which fills a 64-core machine; asking for four
    at a time left it idle.
    """
    pages = [np.array(r, np.uint8) for r in raws]
    if not pages:
        return []
    logs = [[None] * 8 for _ in pages]

    # -- phase 1: every page's four even codewords, one batch ---------------
    blks = [[p[au * 2 * STRIDE:(au * 2 + 1) * STRIDE] for au in range(4)]
            for p in pages]
    v0s = [block_to_mother(b) for pb in blks for b in pb]
    for j, (v, ok) in enumerate(_decode_batch(v0s)):
        pi, au = divmod(j, 4)
        if ok:
            pages[pi][au * 2 * STRIDE:(au * 2 + 1) * STRIDE] = \
                mother_to_block(v, blks[pi][au])
        logs[pi][2 * au] = (2 * au, int((v != v0s[j]).sum()) if ok else 0, ok)

    # -- phase 2: every page's four odd containers, off the corrected evens --
    bits, ov0 = [], []
    for p in pages:
        for au in range(4):
            b = np.unpackbits((~_u_of(p, au)) & 0xFF)
            bits.append(b)
            v0 = np.zeros(385 * Z, np.uint8)
            v0[FREE_O] = b[POS_O]
            ov0.append(v0)
    for j, (v, ok) in enumerate(_decode_batch(ov0)):
        pi, au = divmod(j, 4)
        if not ok:
            logs[pi][2 * au + 1] = (2 * au + 1, 0, False)
            continue
        bits[j][POS_O] = v[FREE_O]
        u2 = ((~np.packbits(bits[j])) & 0xFF).astype(np.uint8)
        base = au * 2 * STRIDE
        pages[pi][base + 2302:base + 2304] = u2[:2]
        pages[pi][(au * 2 + 1) * STRIDE:(au * 2 + 2) * STRIDE] = u2[2:]
        logs[pi][2 * au + 1] = (2 * au + 1, int((v != ov0[j]).sum()), True)
    return list(zip(pages, logs))


def decode_page(raw):
    """LDPC-correct all 8 codewords of a normalized page.

    -> (fixed 18432 bytes, [(cw, flips, converged)] x8)

    Two phases, and the order between them is load-bearing.  The four even
    codewords are independent of each other and of everything else, so they go
    in one batch.  The four odd containers are independent of each other too --
    but each one begins in its own au's even BLOCK (`b0[2302:2304] ++ b1`), so
    it must be built after that block has been corrected and written back.
    Hence phase 1, write back, phase 2.  This is a regrouping of the identical
    work, not a change to it: nothing in phase 1 ever read phase 2's output.

    `decode_pages` is this over a list, with the two batches merged across
    pages; prefer it wherever more than one page is in hand.
    """
    a = np.array(raw, np.uint8)
    log = [None] * 8
    # -- phase 1: the four even codewords ---------------------------------
    blks = [a[au * 2 * STRIDE:(au * 2 + 1) * STRIDE] for au in range(4)]
    v0s = [block_to_mother(b) for b in blks]
    for au, (v, ok) in enumerate(_decode_batch(v0s)):
        if ok:
            a[au * 2 * STRIDE:(au * 2 + 1) * STRIDE] = \
                mother_to_block(v, blks[au])
        log[2 * au] = (2 * au, int((v != v0s[au]).sum()) if ok else 0, ok)
    # -- phase 2: the four odd containers, off the CORRECTED even blocks ---
    us = [_u_of(a, au) for au in range(4)]
    bits = [np.unpackbits((~u) & 0xFF) for u in us]
    ov0 = []
    for b in bits:
        v0 = np.zeros(385 * Z, np.uint8)
        v0[FREE_O] = b[POS_O]
        ov0.append(v0)
    for au, (v, ok) in enumerate(_decode_batch(ov0)):
        if not ok:
            log[2 * au + 1] = (2 * au + 1, 0, False)
            continue
        bits[au][POS_O] = v[FREE_O]
        u2 = ((~np.packbits(bits[au])) & 0xFF).astype(np.uint8)
        a[au * 2 * STRIDE + 2302:au * 2 * STRIDE + 2304] = u2[:2]
        a[(au * 2 + 1) * STRIDE:(au * 2 + 2) * STRIDE] = u2[2:]
        log[2 * au + 1] = (2 * au + 1, int((v != ov0[au]).sum()), True)
    return a, log


def page_mothers(a):
    """The 8 mother codewords of a normalized page, as (8, NB*6) bytes.

    Even codeword 2k and odd container 2k+1 of each AU, in the order
    `page_syndromes_norm` reports them, both complemented as the code expects.
    """
    ms = []
    for au in range(4):
        b0 = bytes(a[au * 2 * STRIDE:(au * 2 + 1) * STRIDE])
        inv = _inv(b0)
        ms.append(_mother(inv[:2056], inv[2056:2302]))
        u = _inv(bytes(_u_of(a, au)))
        ms.append(_mother(u[:2054], u[2054:2300]))
    return np.array(ms)


def page_syndromes_norm(a):
    """8 syndrome weights of a normalized page (shallow convention).

    All eight in ONE call.  They were eight separate calls, which is eight
    round trips to the corrector (or eight numpy gathers) for work that batches
    perfectly -- and this is the hottest thing the ECC map does, once per page
    of every block it colours.
    """
    return [int(w) for w in syndrome_weights(page_mothers(a))]


# ------------------------------------- the derivation: solve A2 p = A1 info
# A2 is the 41x41 circulant block of H over the parity columns, 1968x1968 and
# full rank, so the true parity of a codeword is computable from its true
# information.  This is what turned "the parity reads badly" into "the parity is
# off by a constant", and then into the complement.
#
# 2026-08-05: the 489 KB `nd_deepparity_a2inv.pkl` that used to cache the
# bitwise inverse is gone.  Because the code is quasi-cyclic, that 1968x1968
# GF(2) matrix is a 41x41 matrix over R = GF(2)[x]/(x^48-1), and its ring
# inverse is derived from H in 0.3 s -- see nd_ee3_ring.py, which also proves
# the derived object expands to the retired pickle row for row (0 of 1968
# differ).  These two names are kept as thin shims so old callers still work.
NP_BITS = MB * Z


def a2_inverse():
    """A2^-1, bitwise -- derived, not loaded.  See nd_ee3_ring.a2_inverse()."""
    from . import nd_ee3_ring
    return nd_ee3_ring.a2_inverse()


def solve_parity(info_bytes, inv=None):
    """Mother information bytes (<= 2064, zero padded) -> the 246 true parity."""
    from . import nd_ee3_ring
    return nd_ee3_ring.solve_parity(info_bytes, inv)


def rederive(ref):
    """Reproduce the constant D and show D == ~parity(all-ones information)."""
    inv = a2_inverse()
    print(f"    A2 is full rank ({NP_BITS}), so the true parity is computable")
    Ds = {}
    for f in sorted(RAW.glob("deepraw_ce*_b*_pg00[456].bin")):
        pg = int(f.stem.split("_pg")[1])
        a = np.frombuffer(f.read_bytes(), np.uint8)
        seed = seed_for((8 << 11) | row_of(pg))
        ks = np.frombuffer(keystream(seed, AULEN), np.uint8)
        unit = pg * 12 + ENTRIES.index((6, 8))
        t = np.frombuffer(ref[unit * PAGE:(unit + 1) * PAGE], np.uint8)
        for au in range(4):
            b0 = a[au * 2 * STRIDE:(au * 2 + 1) * STRIDE]
            stored = np.empty(2056, np.uint8)
            stored[0:8] = np.frombuffer(AUX, np.uint8) ^ ks[0:8]
            stored[8:2056] = t[au * 4096:au * 4096 + 2048] ^ ks[8:2056]
            ptrue = np.frombuffer(solve_parity(_inv(bytes(stored)), inv), np.uint8)
            pread = np.frombuffer(_inv(bytes(b0[2056:2302])), np.uint8)
            Ds[(pg, au)] = ptrue ^ pread
    base = Ds[(4, 0)]
    ws = [int(np.unpackbits(v ^ base).sum()) for v in Ds.values()]
    print(f"    D = p_true ^ p_read over {len(Ds)} codewords: weight "
          f"{int(np.unpackbits(base).sum())}/1968, and D ^ D[4,0] weighs "
          f"{min(ws)}..{max(ws)} bits")
    print("    -> D is ONE CONSTANT VECTOR (the 0..3 bits are the real read "
          f"errors), not noise")
    vote = np.packbits((np.stack([np.unpackbits(v) for v in Ds.values()])
                        .sum(0) * 2 > len(Ds)).astype(np.uint8))
    p1 = np.frombuffer(solve_parity(b"\xff" * 2056, inv), np.uint8)
    print(f"    majority-voted D vs parity(all-ones info): they differ in "
          f"{int(np.unpackbits(vote ^ p1).sum())}/1968 bits "
          f"-> D == ~parity(all-ones), exactly")
    print("    p_read = P(i) ^ 1 ^ P(1)  =>  ~p_read = P(~i)  =>  "
          "stored_p = P(stored_i):")
    print("    the deep-page codeword is the stored bytes with NO complement, "
          "i.e. the page\n    is inverted.")


# ------------------------------------------------------------- evidence run
def seed_for(row):
    """The row's 53-bit scrambler state.  Folded; CE-independent."""
    from .titania_nand import seed_state
    return seed_state(row & ~0xC000)


def main():
    ref = REF.read_bytes() if REF.exists() else b""

    print("[0] the scrambler's hidden symmetry")
    print(f"    LFSR taps {TAPS} -- {len(TAPS)} of them, ODD, so all-ones is a "
          f"fixed point")
    s = seed_for((8 << 11) | 8)
    k1 = np.frombuffer(keystream(s, 128), np.uint8)
    k2 = np.frombuffer(keystream(s ^ SEED_FLIP, 128), np.uint8)
    print(f"    keystream(seed ^ 0x{SEED_FLIP:x}) == ~keystream(seed): "
          f"{bool((k1 ^ k2 == 0xFF).all())}")
    print("    -> an inverted page still descrambles, against the complementary"
          f" seed.  That is\n       why the deep rows' data looked fine while "
          f"their parity looked like noise.")

    if "--rederive" in sys.argv:
        print("\n[0b] rederivation from first principles (A2 inverse, slow the "
              "first time)")
        rederive(ref)

    files = sorted(RAW.glob("deepraw_ce*_b*_pg*.bin"))
    print(f"\n[1] convention test on {len(files)} saved raw pages")
    print("    page  row   as read (cw0..7)                       "
          "inverted (cw0..7)")
    for f in files:
        pg = int(f.stem.split("_pg")[1])
        a = np.frombuffer(f.read_bytes(), np.uint8)
        asis = page_syndromes_norm(a)
        inv = page_syndromes_norm((~a) & 0xFF)
        print(f"    {pg:4d} {row_of(pg):4d}   {str(asis):38s}  {inv}")
    print("    page 0 is the control: it is clean AS READ and breaks to 720/768")
    print("    under inversion.  Pages 4,5,6 are the exact mirror image.")

    print(f"\n[2] decode the inverted deep pages and compare with {REF.name}")
    for f in files:
        pg = int(f.stem.split("_pg")[1])
        ce, blk = 6, 8
        a0 = np.frombuffer(f.read_bytes(), np.uint8)
        a, inverted = normalize(a0)
        fixed, log = decode_page(a)
        syn = page_syndromes_norm(fixed)
        mc = [check_au(fixed, au) for au in range(4)]
        seed = seed_for((blk << 11) | row_of(pg))
        ks = np.frombuffer(keystream(seed, AULEN), np.uint8)
        if inverted:
            ks = (~ks) & 0xFF                      # == keystream(seed ^ flip)
        unit = pg * 12 + ENTRIES.index((ce, blk))
        err = None
        if ref and (unit + 1) * PAGE <= len(ref):
            t = np.frombuffer(ref[unit * PAGE:(unit + 1) * PAGE], np.uint8)
            err = 0
            for au in range(4):
                b0 = fixed[au * 2 * STRIDE:(au * 2 + 1) * STRIDE]
                b1 = fixed[(au * 2 + 1) * STRIDE:(au * 2 + 2) * STRIDE]
                user = np.concatenate([b0[8:2056] ^ ks[8:2056],
                                       b0[2302:2304] ^ ks[2056:2058],
                                       b1[0:2046] ^ ks[2058:4104]])
                err += int(np.unpackbits(user ^ t[au * 4096:(au + 1) * 4096]).sum())
        flips = [n for _, n, _ in log]
        print(f"    page {pg} (row {row_of(pg):2d})  inverted={inverted}  "
              f"flips {flips}")
        print(f"              syndromes after decoding {syn}   mcrc {mc}   "
              f"user bit errors vs reference: {err}/131072")

    print("\n[3] where do the REAL read errors live?  corrector flips per "
          f"128-byte segment\n    of the 2304-byte block, aggregated over 24 "
          f"codewords (pages 4,5,6)")
    acc = np.zeros(STRIDE, np.int64)
    n = 0
    for f in files:
        pg = int(f.stem.split("_pg")[1])
        if pg == 0:
            continue
        a, _ = normalize(np.frombuffer(f.read_bytes(), np.uint8))
        fixed, _ = decode_page(a)
        acc += np.unpackbits(a ^ fixed).reshape(-1, 8).sum(1).astype(np.int64) \
            .reshape(8, STRIDE).sum(0)
        n += 8
    for i in range(0, STRIDE, 128):
        c = int(acc[i:i + 128].sum())
        print(f"      {i:5d}..{i+127:5d}  {c:5d}   {c/(128*8*n)*100:6.3f}%  "
              f"{'#' * (c // 3)}")
    a_lo, a_hi = int(acc[:2048].sum()), int(acc[2048:].sum())
    print(f"      0..2047 {a_lo/(2048*8*n)*100:.3f}%   2048..2303 "
          f"{a_hi/(256*8*n)*100:.3f}%  -- a 6.5x step exactly at 2048")
    print("      2048 main + 256 spare is the media page split (8x2048 = 16384"
          " main,\n      8x256 = 2048 spare).  The deep-read degradation is in "
          "the MAIN array only;\n      the parity, which lives in the spare "
          "bytes, is the cleanest part of the page.")


if __name__ == "__main__":
    main()
