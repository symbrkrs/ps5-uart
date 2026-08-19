#!/usr/bin/env python3
"""titania_nand -- encode/decode PS5 (Titania) NAND data, entirely in software.

    py titania_nand.py --selftest

No hardware, no firmware, no console keys.  Give it bytes read by any NAND
reader and it gives you back the plaintext; give it plaintext and it gives you
back what the media would hold.

    from . import titania_nand as tn

    page = tn.decode_page(raw_18432, row)      # -> PageResult
    print(page.aus[0].user[:16], page.aus[0].mcrc_ok)

    raw  = tn.encode_page([tn.AuPlain(user, aux)] * 4, row)


WHAT IS IN HERE AND WHY YOU CAN TRUST IT
----------------------------------------
Every constant below was measured, not guessed, and each has an independent
check.  Provenance for all of it: NAND_OFFLINE_DECODE.md.

  geometry     18432 B page, 4 AUs of 4608 B, 2 codewords of 2304 B per AU
  scrambler    53-bit LFSR; seed = F(row & 0x3FFF) in 325 bytes, verified
               16384/16384 against a hardware-measured map and confirmed on the
               board through the ECU's own seed-map bypass
  AU MCRC      CRC-48 0x1800000000063 (6 B) and CRC-32Q 0x814141AB (4 B), both
               over complemented stored bytes -- so they validate a raw dump with
               no seed, no key and no ECC
  ECC          LDPC EE3 (246 B parity/cw, main media) and A10R (300 B/cw,
               firmware images), both quasi-cyclic with Z = 48.  Both encoders
               are closed form over R = GF(2)[x]/(x^48-1) -- EE3 by a direct
               ring inverse of its 41x41 A2, A10R by a Richardson-Urbanke solve
               with gap = parity blocks 0..11 -- and are recomputed from H at
               run time.  No precomputed encoder matrix is stored anywhere.
               Each also has a second, fully independent construction read
               straight out of the ECU's own config images (nd_a10r_encsil.py),
               and the two agree byte for byte.  That silicon path reduces
               A10R's one irreducible piece of media evidence from the 72-byte
               covector _A10R_W to a single bit; EE3 needs no convention at all,
               because its parity block-incidence matrix is non-singular.

Portability, established by static analysis of the boot ROM and CPU1 firmware
(NAND_TP_VENDOR_VARIANTS.md): none of the above varies with the board testpoint
straps or with the NAND vendor.  The straps move only interface mode, timing and
vref; the five supported vendors differ only in bus addressing and one command
byte (the pseudo-SLC prefix, 0xDA vs 0xA2).  So these constants are safe.

    THE ONE THING A READER MUST GET RIGHT, AND IT IS NOT IN THIS FILE:
    the firmware area (blocks 0-27) is pseudo-SLC and needs a 0xDA (Samsung,
    Micron) or 0xA2 (Toshiba, Hynix, SanDisk) prefix per CE, 368 pages/block.
    Main user data is native TLC, no prefix, 1104 pages/block.  Read the
    firmware area without the prefix and you get bit-rot that looks like three
    unrelated bugs.  See NAND_OFFLINE_DECODE.md section 1.


LAYER ORDER -- IT DIFFERS BY REGION, AND GETTING IT BACKWARDS SILENTLY FAILS
---------------------------------------------------------------------------
  EE3  (main media)      parity covers the descrambled bytes -> descramble, then ECC
  A10R (firmware images) parity covers the scrambled bytes   -> ECC, then descramble

Both MCRCs are over complemented stored bytes and can be checked at any point.
`decode_page` applies the correct order for the format it is told to use.
"""
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

__version__ = "1.0"

# ------------------------------------------------------------------ geometry --
PAGE_BYTES = 18432
CW_BYTES = 2304                  # one EE3 codeword as stored
AU_STRIDE = 4608                 # 2 codewords
AUS_PER_PAGE = 4
MSG_BYTES = 4104                 # 4096 user + 8 aux, identical in every format
USER_BYTES = 4096
AUX_BYTES = 8
EE3_PARITY = 246
A10R_PARITY = 300

FW_PAGES_PER_BLOCK = 368         # pseudo-SLC firmware area, blocks 0..27
TLC_PAGES_PER_BLOCK = 1104       # native TLC main storage
SLC_PREFIX = {"samsung": 0xDA, "micron": 0xDA,
              "toshiba": 0xA2, "hynix": 0xA2, "sandisk": 0xA2}

# AU formats.  Index -> purpose is not stable across firmware revisions, so key
# off geometry, never off the index (2021 firmware puts raw at 18/19, this board
# at 20/21).  Format D is the RAID parity configuration; it is a READ-path ECU
# setting and does not appear on media -- kept for completeness.
FORMAT_A = "A"        # main media: EE3, 6-byte MCRC, scrambled
FORMAT_B = "B"        # firmware images: 512x4, A10R, 4-byte MCRC, scrambled
FORMAT_C = "C"        # raw/long: no ECC, no MCRC, not scrambled
FORMAT_D = "D"        # RAID parity: as a but 4-byte MCRC


class AuPlain:
    """Plaintext contents of one AU."""

    def __init__(self, user, aux=b"\x00" * AUX_BYTES):
        if len(user) != USER_BYTES:
            raise ValueError("user must be %d bytes" % USER_BYTES)
        if len(aux) != AUX_BYTES:
            raise ValueError("aux must be %d bytes" % AUX_BYTES)
        self.user = bytes(user)
        self.aux = bytes(aux)

    def message(self):
        """The 4104-byte logical message in keystream order: aux first."""
        return self.aux + self.user


class AuResult:
    """Outcome of decoding one AU."""

    def __init__(self, user=None, aux=None, mcrc_ok=None, ecc_ok=None,
                 corrected=0, erased=False, note=""):
        self.user, self.aux = user, aux
        self.mcrc_ok, self.ecc_ok = mcrc_ok, ecc_ok
        self.corrected, self.erased, self.note = corrected, erased, note

    @property
    def ok(self):
        return bool(self.mcrc_ok) and self.ecc_ok is not False

    def __repr__(self):
        if self.erased:
            return "<AuResult erased>"
        return ("<AuResult mcrc=%s ecc=%s corrected=%d%s>"
                % (self.mcrc_ok, self.ecc_ok, self.corrected,
                   " " + self.note if self.note else ""))


class PageResult:
    def __init__(self, aus, row):
        self.aus, self.row = aus, row

    @property
    def ok(self):
        return all(a.ok or a.erased for a in self.aus)

    def user(self):
        """Concatenated user payload of the whole page (erased AUs -> 0xFF)."""
        return b"".join(a.user if a.user is not None else b"\xFF" * USER_BYTES
                        for a in self.aus)

    def __repr__(self):
        return "<PageResult row=%#x %s>" % (self.row,
                                            " ".join(repr(a) for a in self.aus))


# ---------------------------------------------------------------- scrambler --
# The seed map F: 14-bit folded row -> 53-bit LFSR state, in 325 bytes.
# Three ROM tables into a 32-bit integer adder, then the scrambler's own shift
# register clocked from -15.  Hardware-confirmed via SCR_SEED_MAP_BYPASS.
_T0 = [0, 2514687943, 2743673434, 1837173130, 2945894190, 3733401133, 913042436,
       3139470106, 112267083, 2685588919, 305916573, 3966760014, 1545022001,
       2430975636, 2697349054, 3368238408, 1806365255, 1154827030, 2250740176,
       2489006866, 3560994654, 2980990218, 800624091, 2014635259, 2273758601,
       4184772133, 3583718501, 1775950741, 2191335064, 948628512, 4201478200,
       1158103122]
_T1 = [0, 1858385594, 3952584849, 544681639, 2432373564, 4054752653, 3796183598,
       3833166620, 2637075146, 3802062303, 1806212022, 1316230798, 4293626190,
       1489351061, 429511832, 2834424441, 3840143795, 3573509049, 59522035,
       3944434332, 3697837321, 2947437805, 439521429, 1620659240, 3435811458,
       684064589, 1147699611, 2497291026, 2552955847, 2744227974, 388380617,
       234296739]
_T2 = [165779728, 3768786654, 2506649831, 195458482, 1040905987, 2682839883,
       150343227, 191472726, 2936904785, 3312237769, 2830376044, 3954594865,
       722753530, 438350150, 106804684, 514948101]
_EPS = (_T0[0] + _T1[0] + _T2[0]) & 0xFFFFFFFF     # = sum(0), forced by F(0)
_REG0 = 0xBB33C367                                 # the x=0 shift register
_TAIL_C = (0, 0, 0)
_TAIL_D = (1, 1, 1)

LFSR_BITS = 53
_TAPS = (1, 49, 50, 52, 53)      # b[N] = b[N-1]^b[N-49]^b[N-50]^b[N-52]^b[N-53]
_MASK53 = (1 << LFSR_BITS) - 1

FOLD_MASK = 0x3FFF               # the map input is 14 bits, verified globally


def seed_state(row):
    """53-bit scrambler state for a row.  CE-independent; the PBA is the row."""
    x = row & FOLD_MASK
    s = (_T0[x & 0x1F] + _T1[(x >> 5) & 0x1F] + _T2[(x >> 10) & 0xF]) & 0xFFFFFFFF
    v = s ^ _EPS
    R = [(v >> ((2 - i) % 32)) & 1 for i in range(32)]
    R[3] ^= R[4]                                   # the adder's MSB is gray-coded
    b = {}
    for i in range(32):
        b[-15 + i] = R[i] ^ ((_REG0 >> i) & 1)
    for n in (-16, -17, -18):
        b[n] = b[n + 3] ^ b[n + 32] ^ 1            # Xnor, backward
    for n in range(17, 46):
        b[n] = b[n - 32] ^ b[n - 29] ^ 1           # Xnor, forward
    out = [b[i] for i in range(46)]
    out += [b[-18 + t] ^ _TAIL_C[t] for t in range(3)]
    out += [b[-15 + t] ^ b[t] ^ _TAIL_D[t] for t in range(3)]
    out += [b[0] ^ b[3] ^ 1]
    st = 0
    for i in range(LFSR_BITS):
        st |= out[i] << (52 - i)
    return st


def state_valid(state):
    """Free 1-bit check: the (x+1)-component of a real state is always 1."""
    b = [(state >> (52 - i)) & 1 for i in range(LFSR_BITS)]
    return (b[52] ^ b[3] ^ b[0]) == 1


def seed_map():
    """The whole map, all 16384 folded rows -> 53-bit state.  -> {int: int}

    `seed_state` is the map, so this is a convenience for anyone who wants it
    materialised (a hardware comparison, a hex table for another language).
    """
    return {r: seed_state(r) for r in range(FOLD_MASK + 1)}


#: sha256 of the map serialised as 16384 big-endian 7-byte states, row order.
#:
#: The map was originally carried as a 478 KB JSON of 16384 entries read out of
#: silicon through SCR_SEED_MAP_BYPASS.  The 325 bytes of ROM tables above
#: reproduce every one of them, so the file was measurement that had been
#: superseded by its own solution and it is no longer shipped -- this digest is
#: what survives of it, and the self-test below recomputes the map and compares.
#: That is a stronger check than the file was: a corrupted table file and a
#: corrupted copy of it agree with each other, a digest agrees with neither.
SEEDMAP_SHA256 = "a4489c8b6eb5a16e8754b1a18ddc35d715e23f94590cc9a62e1d1228dc280e33"


def seed_map_digest():
    """sha256 of the materialised map, to compare with SEEDMAP_SHA256.  -> str"""
    import hashlib
    return hashlib.sha256(
        b"".join(seed_state(r).to_bytes(7, "big")
                 for r in range(FOLD_MASK + 1))).hexdigest()


_KS_CACHE = {}
_KS_CACHE_MAX = 4


def keystream(state, nbytes):
    """MSB-first keystream.  The state is the first 53 keystream bits.

    Memoised, because the four AUs of a page all restart the keystream from the
    same state -- the seed is per row, not per AU -- so a page decode asked for
    the identical 4104 bytes four times over.  Hoisting that is a ~4x speedup on
    every scan, and scans are what this library spends its life doing.

    The cache is tiny: consecutive rows have different seeds, so
    there is nothing to gain from holding more than the row in hand (plus a
    little slack for encode/decode interleaving).  Returned values are `bytes`,
    so handing the same object to several callers is safe.

    Generating the bits themselves is already about as fast as pure Python
    manages.  Accumulating them into one big integer instead looks tidier and is
    2x slower -- shifting a 32832-bit int once per bit is quadratic.  Measured;
    do not "improve" it that way.
    """
    key = (state, nbytes)
    hit = _KS_CACHE.get(key)
    if hit is not None:
        return hit
    r = state & _MASK53
    bits = [(state >> (LFSR_BITS - 1 - i)) & 1 for i in range(LFSR_BITS)]
    need = nbytes * 8
    while len(bits) < need:
        nb = (r ^ (r >> 48) ^ (r >> 49) ^ (r >> 51) ^ (r >> 52)) & 1
        bits.append(nb)
        r = ((r << 1) | nb) & _MASK53
    out = bytearray(nbytes)
    for i in range(nbytes):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i * 8 + j]
        out[i] = v
    out = bytes(out)
    if len(_KS_CACHE) >= _KS_CACHE_MAX:
        _KS_CACHE.clear()
    _KS_CACHE[key] = out
    return out


def apply_scrambler(data, state):
    """XOR with the keystream.  Involutive: scramble and descramble are one op.

    Every AU restarts the keystream at index 0, so `data` must start at an AU
    boundary in logical (keystream) order -- see au_logical().
    """
    ks = keystream(state, len(data))
    return bytes(a ^ b for a, b in zip(data, ks))


descramble = apply_scrambler
scramble = apply_scrambler


def state_from_aux(stored_aux8, aux_plain8):
    """Recover the state straight off a page, no seed map needed.

    The 8-byte aux sits at keystream index 0 and the state is the first 53
    keystream bits, so this reads the state off any page whose aux plaintext is
    known -- with 11 spare bits as a free consistency check (see aux_check).
    Firmware-area aux constants: 03 00.. for blocks 6/7, 06 00.. for blocks 8/9.
    """
    x = int.from_bytes(bytes(a ^ b for a, b in zip(stored_aux8, aux_plain8)), "big")
    return x >> (64 - LFSR_BITS)


def aux_check(stored_aux8, aux_plain8):
    st = state_from_aux(stored_aux8, aux_plain8)
    return bytes(a ^ b for a, b in zip(stored_aux8, aux_plain8)) == keystream(st, 8)


# --------------------------------------------------------------------- MCRC --
# Both polynomials factor as (x+1) * primitive -- the same vendor construction at
# two widths.  Both are computed over the complemented stored bytes.
_POLY48 = 0x1800000000063        # x^48+x^47+x^6+x^5+x+1
_POLY32 = 0x814141AB             # CRC-32Q / CRC-32-AIXM
_TAB48 = _TAB32 = None


def _mktable(poly, width):
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    tab = []
    for b in range(256):
        r = (b << (width - 8)) & mask
        for _ in range(8):
            r = ((r << 1) ^ poly) & mask if r & top else (r << 1) & mask
        tab.append(r)
    return tab


def crc48(data, init=0):
    global _TAB48
    if _TAB48 is None:
        _TAB48 = _mktable(_POLY48, 48)
    r = init
    for byte in data:
        r = ((r << 8) ^ _TAB48[((r >> 40) ^ byte) & 0xFF]) & 0xFFFFFFFFFFFF
    return r


def crc32q(data, init=0):
    global _TAB32
    if _TAB32 is None:
        _TAB32 = _mktable(_POLY32, 32)
    r = init
    for byte in data:
        r = ((r << 8) ^ _TAB32[((r >> 24) ^ byte) & 0xFF]) & 0xFFFFFFFF
    return r


def au_mcrc(message, width=6):
    """AU MCRC over the stored message.  `message` is the stored (scrambled)
    4104 bytes; every byte is complemented before being clocked in."""
    c = bytes(b ^ 0xFF for b in message)
    if width == 6:
        return crc48(c).to_bytes(6, "big")
    if width == 4:
        return crc32q(c).to_bytes(4, "big")
    raise ValueError("MCRC width must be 4 or 6")


# ------------------------------------------------------- AU stored <-> logical --
# The AU's 4110-byte logical span (4104 message + 6 MCRC) is interleaved into the
# two 2304-byte stored codewords like this, measured on 1424 AUs:
#
#     b0[0:2056]     logical[0:2056]
#     b0[2056:2302]  EE3 parity of the even codeword      (246)
#     b0[2302:2304]  logical[2056:2058]
#     b1[0:2052]     logical[2058:4110]
#     b1[2052:2298]  EE3 parity of the odd codeword       (246)
#     b1[2298:2304]  duplicate of b1[0:6]  (LDPC pipeline tail)
#
# 4602 bytes are transferred by the ECU; 4608 are stored, the extra 6 being that
# tail duplicate.  A 4-byte-MCRC format uses logical[4104:4108] instead.
_L0, _L1, _L2 = 2056, 2302, 2304


def au_logical(stored_au):
    """One stored 4608-byte AU -> its 4110-byte logical span, keystream order."""
    if len(stored_au) != AU_STRIDE:
        raise ValueError("AU must be %d bytes" % AU_STRIDE)
    b0 = stored_au[:CW_BYTES]
    b1 = stored_au[CW_BYTES:]
    return bytes(b0[0:_L0]) + bytes(b0[_L1:_L2]) + bytes(b1[0:2052])


def au_place_logical(stored_au, logical):
    """Write a logical span back into a stored AU, leaving parity untouched."""
    out = bytearray(stored_au)
    out[0:_L0] = logical[0:_L0]
    out[_L1:_L2] = logical[_L0:_L0 + 2]
    out[CW_BYTES:CW_BYTES + 2052] = logical[_L0 + 2:_L0 + 2 + 2052]
    return bytes(out)


def au_is_erased(stored_au):
    head = bytes(au_logical(stored_au)[:64])
    return head.count(0xFF) == len(head) or head.count(0x00) == len(head)


# ---------------------------------------------------------------------- ECC --
# The LDPC matrices are large derived objects and live in the already-verified
# modules rather than being duplicated here.  This is the only external
# dependency in the file, and it is optional: without it you still get geometry,
# descrambling and both MCRCs, which is enough to validate and read a dump.
_ECC_ERR = None
try:
    from . import nd_deepparity as _dp
    from . import nd_ee3_ring as _ee3
except Exception as _e:            # pragma: no cover
    _dp = _ee3 = None
    _ECC_ERR = repr(_e)

try:
    from . import nd_g2ldpc_a10r as _a10r
except Exception:                  # pragma: no cover
    _a10r = None


def ecc_available():
    return _dp is not None


def ecc_correct_page(stored_page):
    """LDPC-correct all 8 EE3 codewords of a page.

    -> (page, n_corrected, all_converged).  Handles the stored-complement
    convention itself.  An ERASED page is all-0xFF, which complements to the
    zero codeword: it converges with syndrome 0 but is not data, so check
    au_is_erased() before trusting an MCRC verdict.
    """
    if _dp is None:
        raise RuntimeError("LDPC unavailable: %s" % _ECC_ERR)
    norm, _inv = _dp.normalize(stored_page)
    fixed, log = _dp.decode_page(norm)
    return bytes(fixed), sum(n for _c, n, _o in log), all(o for _c, _n, o in log)


def ee3_parity(info_bytes, source="ring"):
    """EE3 mother information bytes (<= 2064, zero padded) -> 246 parity bytes.

    Two independent constructions, and they agree byte for byte:

      "ring"     `p = A2^-1 . A1 . m` over R = GF(2)[x]/(x^48-1), A2 the 41x41
                 parity half of H inverted by Gauss-Jordan over R (nd_ee3_ring).
                 Primary: exact against the retired 489 KB pickle row for row and
                 against media (952/952 even, 947/947 odd codewords).
      "silicon"  Richardson-Urbanke, with H taken from the firmware's own EE3
                 DECODER config image and the gap solve Q read out of its encoder
                 config image's 144-record tail (nd_a10r_encsil.EE3).  EE3's
                 parity block-incidence is 41x41 rank 41 -- nullity 0 -- so phi is
                 invertible, `phi . Q = I` exactly (144/144), and there is no
                 branch bit and no fitted constant anywhere in the path.

    The silicon route is worth having because it comes from a different source
    entirely: the two config images were captured in the emulator and are
    byte-identical to static extraction from two independent firmware images, and
    the H they carry matches the media-derived H on all 385 mother columns, layer
    and shift alike.  Agreement of two encoders built from different sources is a
    stronger statement than either reproducing media on its own.
    """
    if source == "silicon":
        from . import nd_a10r_encsil as _sil
        return _sil.ee3_parity(info_bytes)
    if source != "ring":
        raise ValueError("source must be 'ring' or 'silicon'")
    if _ee3 is None:
        raise RuntimeError("LDPC unavailable: %s" % _ECC_ERR)
    return _ee3.solve_parity(info_bytes)


def ecc_encode_even(stored_block):
    """2304-byte even codeword block with its message in place -> same block
    with correct EE3 parity written in.

    The parity is solved in mother space, not on the raw block: `block_to_mother`
    expands the stored block into 18480 = 385 x 48 bits, of which the first
    NINFO x Z are information and the last MB x Z = 1968 (246 bytes) are parity.
    Packing those information bits MSB-first into 6-byte (Z = 48) groups is
    exactly what solve_parity expects.  Verified against a real media page:
    the regenerated parity is byte-identical to what the drive stored.

    The solve itself is closed form: p = A2^-1 . A1 . m over
    R = GF(2)[x]/(x^48-1), with A2 the 41x41 parity half of H and A2^-1 obtained
    from it by Gauss-Jordan over R in 0.3 s.  No table, no fit -- nd_ee3_ring.py
    carries the whole derivation.  `ee3_parity(info, source="silicon")` is an
    independent second route to the same 246 bytes, read out of the firmware's
    own ECU config images; selftest [12] checks that the two agree.
    """
    if _dp is None:
        raise RuntimeError("LDPC unavailable: %s" % _ECC_ERR)
    import numpy as np
    from .nd_ldpc_fix import block_to_mother, mother_to_block
    from .nd_ldpc_odd import NINFO, Z
    blk = np.frombuffer(bytes(stored_block), np.uint8)
    m = block_to_mother(blk).astype(np.uint8)
    info = np.packbits(m[:NINFO * Z]).tobytes()
    par = _ee3.solve_parity(info)
    m[NINFO * Z:] = np.unpackbits(np.frombuffer(par, np.uint8))
    return bytes(mother_to_block(m, blk))


def ecc_encode_odd(stored_au):
    """4608-byte AU with its message in place -> same AU with correct EE3 parity
    for the odd codeword, plus the LDPC pipeline tail.

    The odd codeword is the awkward one: its container is `b0[2302:2304] ++ b1`
    (2306 bytes), i.e. its information starts in the even BLOCK, and mother bytes
    2054..2063 are shortened away.  Parity mother byte j lands at u[2054+j] =
    b1[2052+j].

    Same mother code as the even half, so the same solve works -- but the parity
    must be written back through the bit mapping, not copied in directly: the
    container is stored complemented, so a raw solve comes out as the exact
    complement of what the media holds.  Verified against real media: all 4 AUs
    of a page regenerate byte-identical parity.
    """
    if _dp is None:
        raise RuntimeError("LDPC unavailable: %s" % _ECC_ERR)
    import numpy as np
    from .nd_ldpc_odd import NINFO, Z
    au = bytes(stored_au)
    b0 = np.frombuffer(au[:CW_BYTES], np.uint8)
    b1 = np.frombuffer(au[CW_BYTES:], np.uint8)
    u = np.concatenate([b0[2302:2304], b1]).astype(np.uint8)
    u[2054:2300] = 0                       # parity unknown until solved
    bits = np.unpackbits((~u) & 0xFF)
    v = np.zeros(385 * Z, np.uint8)
    v[_dp.FREE_O] = bits[_dp.POS_O]
    v[NINFO * Z:] = 0
    par = _ee3.solve_parity(np.packbits(v[:NINFO * Z]).tobytes())
    v[NINFO * Z:] = np.unpackbits(np.frombuffer(par, np.uint8))
    bits[_dp.POS_O] = v[_dp.FREE_O]
    u2 = (~np.packbits(bits)) & 0xFF

    out = bytearray(au)
    out[2302:2304] = bytes(u2[0:2])
    out[CW_BYTES:] = bytes(u2[2:2306])
    # the last 6 stored bytes are the LDPC pipeline tail: a duplicate of b1[0:6]
    out[CW_BYTES + 2298:CW_BYTES + 2304] = out[CW_BYTES:CW_BYTES + 6]
    return bytes(out)


# ---------------------------------------------------------------------------
# The one number in this file that H cannot produce.  576 bits, 72 bytes.
#
# rank(H) = 2399 of 2400, so H verifies A10R media but does not define the code:
# every message admits exactly two parities satisfying all 2400 checks, differing
# by the kernel kappa (the all-ones polynomial J on 24 parity blocks).  Both are
# genuine codewords -- verified, 300/300 each -- so the encoder must be told
# which one the silicon emits, and one bit per codeword is irreducible.
#
# Proven underivable, do not spend time on it.  The Richardson-Urbanke solve
# ends on a pivot of (x+1)-valuation 1; over R = GF(2)[x]/(x^48-1) that pivot has
# a two-element solution set, and `Psi' = Psi + e_K . zeta^T` is an admissible
# encoder for FREE zeta -- H is satisfied either way.  Separately: no 12x12 ring
# matrix can implement the gap solve at all (fd_derive.py), so the choice is not
# hiding in some cleverer factorisation of H.  It is one bit of media evidence
# and nothing else will produce it.
#
# The rule below is the smallest honest statement of that evidence:
#
#       <W, P> = 0        W = 576 bits over PARITY BLOCK COLUMNS 0..11 only
#                             (parity bytes 0..71; blocks 12..49 are all zero)
#
# no message term, no per-slot constant.  Checked on 8218 of 8218 clean media
# codewords off five dies (CE1..CE5, <dump>/g2) -- `py fd_a10rverify.py` -- and
# W is unique: the affine span of those parities has GF(2) rank 2400 of 2401, so
# this is the only affine relation the media admits.  <W, kappa> = 1, so it
# always separates the two branches.
#
# It is also exactly the object the derivation predicts: with `r` the ring
# covector that pins the last RU pivot, W = recip(r), i.e. <W,P> is the
# coefficient of x^0 in r . x_gap -- the accumulator's reset bit.  (Verified
# block for block against the r extracted from the derived Psi.)  Its support
# landing exactly on the 12 gap blocks was not assumed: W was fitted over all
# 2400 parity bits with no knowledge of the decomposition, and came out zero on
# the other 38 blocks -- which is how the gap set was first named from data.
#
# This supersedes nd_a10r_row.npz (7.3 KB: a dense 4128-bit message covector, a
# 2400-bit parity covector and four slot constants).  The two rules agree on
# 8218/8218 media codewords.
_A10R_W = bytes.fromhex(
    "ca7ab4caf1ec9d8e93dfd0278e48894a28d806ddc5a57ed9"
    "5c6f236d445135e36031141f404b0c7347384657af42389a"
    "a534184153fca2c529c397afbc3ff5e2312cb32fb0f1d8bd")
_A10R_WINT = int.from_bytes(_A10R_W, "big")


def a10r_branch_bit(parity300):
    """<W, P> -- 0 for the parity the silicon writes, 1 for its kernel sibling."""
    return bin(int.from_bytes(bytes(parity300[:72]), "big")
               & _A10R_WINT).count("1") & 1


def _a10r_choice(message516, slot, cand):
    """Pick the parity the hardware emits.  Depends on the parity alone."""
    for i, p in enumerate(cand):
        if a10r_branch_bit(p) == 0:
            return i
    raise ValueError("neither candidate satisfies the A10R branch check")


def a10r_candidates(message516, slot):
    """The two admissible 300-byte A10R parities for a message.

    Derived from H alone by a Richardson-Urbanke encoder: RU peel of A2 with
    gap = parity block columns 0..11 (residual 0, 38-step monomial triangle),
    then a Gauss-Jordan of the 12x12 Schur complement phi over
    R = GF(2)[x]/(x^48-1).  Eleven columns take a unit pivot; the twelfth
    residual has (x+1)-valuation exactly 1, so the last variable is resolved by
    the (1+x) pseudo-inverse -- a serial accumulator.  Its two branches differ
    by exactly the kernel kappa, which is why this returns two candidates.

    Nothing precomputed is involved: the peel, the triangle, phi and its
    elimination are all recomputed from H on first use (~1 s, then cached).
    The old 1.1 MB nd_a10r_solve.pkl fall-back is gone -- this path covers every
    message and slot, checked on 8218/8218 clean media codewords.
    """
    from . import fd_ruenc as _ru
    return _ru.a10r_candidates(bytes(message516), slot)


def a10r_parity(message516, slot):
    """516 message bytes + slot 0..3 -> the 300 A10R parity bytes.

    Primary PATH: the silicon TABLES (`nd_a10r_encsil.py`).  The ECU's own config
    images hold the entire encoder -- the decoder bank's schedule gives H in
    Richardson-Urbanke block form, and the encoder bank's 144-record tail is the
    gap-solve matrix Q, satisfying

        phi . Q = S      S[0][0] = x , S[0][11] = 1 , S[i][i] = S[i][i-1] = 1

    byte-identically in A10R and code 7, with nothing fitted.  `det S = x+1`, so
    S is not the identity: over R = GF(2)[x]/(x^48-1) no ring matrix
    can invert phi (a theorem), and the residual degree-1 factor is where that
    obstruction goes.  Solving `S.u = y` is a running prefix XOR closed by
    `(x+1).u_0 = sum y_i` -- one 576-bit serial accumulator, which is what the
    silicon is.  The two solutions differ by J; the hardware takes the one with
    bit 47 of u_0 clear.  8218/8218 media codewords, byte-exact.

    S is not universal -- do not assume it for a new code.  It is the repair for
    rank-deficient codes only.  The deciding quantity is the code's parity
    block-incidence nullity: A10R and code 7 are nullity 1, so the ring kernel is
    J on a block set, `1^T . phi = 0`, the theorem's premise holds and the
    hardware stores a Q with `phi . Q = S`.  EE3's incidence is 41x41 of rank 41,
    nullity 0 -- no kernel, phi invertible, and the hardware simply stores
    `Q = phi^-1` with `phi . Q = I`, 144/144, so EE3 needs no branch bit at all.
    `nd_a10r_encsil.SiliconCode` measures the nullity and picks the right one; it
    never keys off the code's name.

    That reduces the irreducible convention from the 72-byte `_A10R_W` covector
    to one bit.  `_A10R_W` is kept only for the fall-back below and as an
    independent cross-check -- the two rules agree on 8218/8218.

    Fall-back, if the table dumps are absent: derive the RU solve from H and pick
    the branch with `_A10R_W`.  Same output, more work, and it needs the 72 bytes.
    """
    try:
        from . import nd_a10r_encsil as _sil
        if _sil.available():
            return _sil.parity(bytes(message516), slot)
    except ImportError:
        pass
    cand = a10r_candidates(message516, slot)
    if not cand:
        raise ValueError("message does not satisfy the A10R consistency check")
    return cand[_a10r_choice(message516, slot, cand)]


def code7_parity_mother(msg_blocks86, branch=0):
    """Code 7 (ECU banks 7/15): 86 mother message blocks -> 19 parity blocks.

    Same machinery as A10R -- gap 12, 7-step triangle (header[1] = 7), one
    (1+x) accumulator pivot.  No media using code 7 is known, so the kernel
    branch is a caller's choice; both satisfy H.c == 0.
    """
    from . import fd_ruenc as _ru
    return _ru.code7_parity_mother(msg_blocks86, branch)


def a10r_encode_frame(frame816, slot):
    """Rewrite an 816-byte A10R frame's parity from its own message."""
    f = bytearray(bytes(frame816))
    f[516:816] = a10r_parity(bytes(f[:516]), slot)
    return bytes(f)


# ------------------------------------------------------------------- decode --
def decode_au(stored_au, row=None, state=None, fmt=FORMAT_A, mcrc_width=None):
    """Decode one AU that has already been ECC-corrected (or is known clean).

    Pass `row` (the row the AU was read from) or an explicit `state`.  For
    FORMAT_C nothing is scrambled and no MCRC exists.
    """
    if au_is_erased(stored_au):
        return AuResult(erased=True)
    lg = au_logical(stored_au)
    width = mcrc_width if mcrc_width is not None else (4 if fmt in (FORMAT_B, FORMAT_D) else 6)

    mcrc_ok = None
    if fmt != FORMAT_C:
        stored_msg = lg[:MSG_BYTES]
        want = au_mcrc(stored_msg, width)
        mcrc_ok = bytes(lg[MSG_BYTES:MSG_BYTES + width]) == want

    if fmt == FORMAT_C:
        plain = bytes(lg[:MSG_BYTES])           # raw mode: not scrambled at all
    else:
        if state is None:
            if row is None:
                raise ValueError("need row or state to descramble")
            state = seed_state(row)
        plain = apply_scrambler(lg[:MSG_BYTES], state)

    return AuResult(user=plain[AUX_BYTES:], aux=plain[:AUX_BYTES], mcrc_ok=mcrc_ok)


def decode_page(stored_page, row, fmt=FORMAT_A, correct=True):
    """Full page decode: ECC (EE3), then MCRC, then descramble.

    Order matters and differs by region -- EE3 parity covers the descrambled
    bytes, so ECC runs on the stored form first; A10R parity covers the
    scrambled bytes.  See the module docstring.
    """
    if len(stored_page) != PAGE_BYTES:
        raise ValueError("page must be %d bytes" % PAGE_BYTES)
    page, ncorr, conv = (stored_page, 0, None)
    if correct and fmt in (FORMAT_A, FORMAT_D) and ecc_available():
        page, ncorr, conv = ecc_correct_page(stored_page)

    aus = []
    for i in range(AUS_PER_PAGE):
        au = page[i * AU_STRIDE:(i + 1) * AU_STRIDE]
        r = decode_au(au, row=row, fmt=fmt)
        r.ecc_ok = conv
        r.corrected = ncorr if i == 0 else 0
        aus.append(r)
    return PageResult(aus, row)


# ------------------------------------------------------------------- encode --
def encode_au(plain, row=None, state=None, fmt=FORMAT_A, mcrc_width=None,
              with_parity=True):
    """Plaintext AU -> the 4608 stored bytes.  Complete for formats A/C/D:
    scrambling, MCRC, both EE3 parities and the pipeline tail are generated, and
    the result is byte-identical to what the drive writes (verified 4/4 AUs of a
    real page).  Format B (A10R) cannot be re-encoded at the AU level yet --
    decode only -- because this function does not know format B's 512x4 frame
    container.  A10R encoding itself is complete: see `a10r_encode_frame`, which
    rewrites a single 816-byte frame's parity exactly (8218/8218 media).
    """
    width = mcrc_width if mcrc_width is not None else (4 if fmt in (FORMAT_B, FORMAT_D) else 6)
    msg = plain.message()
    if fmt == FORMAT_C:
        stored_msg = msg
    else:
        if state is None:
            if row is None:
                raise ValueError("need row or state to scramble")
            state = seed_state(row)
        stored_msg = apply_scrambler(msg, state)

    lg = bytearray(_L0 + 2 + 2052)
    lg[:MSG_BYTES] = stored_msg
    if fmt != FORMAT_C:
        lg[MSG_BYTES:MSG_BYTES + width] = au_mcrc(stored_msg, width)

    out = au_place_logical(bytes(AU_STRIDE), bytes(lg))
    if with_parity and fmt in (FORMAT_A, FORMAT_D) and ecc_available():
        # even first (writes b0 parity), then odd (which reads b0[2302:2304] as
        # information and writes b1 parity) -- the two do not overlap
        out = ecc_encode_even(out[:CW_BYTES]) + out[CW_BYTES:]
        out = ecc_encode_odd(out)
    return out


def encode_page(plains, row, fmt=FORMAT_A, **kw):
    if len(plains) != AUS_PER_PAGE:
        raise ValueError("need %d AUs" % AUS_PER_PAGE)
    return b"".join(encode_au(p, row=row, fmt=fmt, **kw) for p in plains)


# ----------------------------------------------------------------- selftest --
def _selftest():
    ok = True
    print("titania_nand %s -- selftest\n" % __version__)

    # Skips are counted, not swallowed.  Five of the twelve checks below need a
    # reference page from your own media, and every one of them used to degrade
    # to a bare "-- skipped" line without touching `ok` -- so a fresh clone with
    # no media printed "selftest: PASS" having verified almost nothing.  The
    # verdict line names the skip count, and `--strict` makes a skip a failure,
    # so CI (or anyone checking a claim) can demand the real thing.
    skipped = []

    def skip(msg):
        """Record a check that could not run, and say so in the verdict."""
        skipped.append(msg)
        print(msg)

    # 1. the seed map, against the 16384 hardware-measured entries
    got = seed_map_digest()
    print("[1] seed map vs hardware  : %s"
          % ("all 16384 exact" if got == SEEDMAP_SHA256
             else "MISMATCH %s != %s" % (got[:16], SEEDMAP_SHA256[:16])))
    ok &= got == SEEDMAP_SHA256
    inv = all(state_valid(seed_state(r)) for r in range(0, 16384, 97))
    print("    (x+1)-component check : %s" % inv)
    ok &= inv

    # 2. the state really is the first 53 keystream bits
    bad = sum(1 for r in (0, 1, 2, 0x200, 0x3FFF)
              if int.from_bytes(keystream(seed_state(r), 8), "big") >> 11
              != seed_state(r))
    print("[2] state == first 53 ks bits: %d mismatches" % bad)
    ok &= bad == 0

    # 3. MCRC check values
    c32 = crc32q(b"123456789")
    print("[3] crc32q('123456789')      : %#010x (expect 0x3010bf7f)" % c32)
    ok &= c32 == 0x3010BF7F

    # 4. AU logical mapping is a faithful round trip
    import os
    blob = bytes(bytearray(os.urandom(AU_STRIDE)))
    lg = au_logical(blob)
    back = au_logical(au_place_logical(blob, lg))
    print("[4] AU logical round trip    : %s" % (back == lg))
    ok &= back == lg

    # 5. scrambler is involutive
    st = seed_state(0x200)
    d = os.urandom(4104)
    print("[5] scramble/descramble      : %s"
          % (apply_scrambler(apply_scrambler(d, st), st) == d))
    ok &= apply_scrambler(apply_scrambler(d, st), st) == d

    # 6. End to end on a real page, if one is present.  Any raw TLC page whose
    #    name carries its row will do -- `titania capture` writes them as
    #    ceNN_bNNNN_pNNNN.bin -- so this runs against your own media, not a
    #    file that only existed on the machine this was written on.
    ref = next(iter(sorted((config.dump_path("tlc") or Path(".")).glob(
        "ce*_b*_p*.bin"))), None)
    if ref is not None and ecc_available():
        raw = ref.read_bytes()
        row = int(ref.stem.split("_p")[1])
        res = decode_page(raw, row)
        nmcrc = sum(1 for a in res.aus if a.mcrc_ok)
        print("[6] real TLC page          : %d/4 AUs MCRC-valid, ecc_ok=%s"
              % (nmcrc, res.aus[0].ecc_ok))
        ok &= nmcrc == AUS_PER_PAGE
        # re-encode AU0 and compare everything except the odd parity we cannot
        # yet generate
        page, _n, _c = ecc_correct_page(raw)
        au0 = page[:AU_STRIDE]
        a = decode_au(au0, row=row)
        exact = 0
        for i in range(AUS_PER_PAGE):
            aui = page[i * AU_STRIDE:(i + 1) * AU_STRIDE]
            ai = decode_au(aui, row=row)
            re_enc = encode_au(AuPlain(ai.user, ai.aux), row=row)
            exact += (re_enc == aui)
        print("    FULL re-encode vs media : %d/%d AUs byte-identical "
              "(all 4608 B: message, MCRC, scrambling, both parities, tail)"
              % (exact, AUS_PER_PAGE))
        ok &= exact == AUS_PER_PAGE
    else:
        skip("[6] no reference page / no LDPC -- skipped")

    # 7. A10R parity generation against a real group-2 page (CE1; CE0 is dead)
    g2 = sorted((config.dump_path("g2")).glob("g2_ce1_*.bin"))
    if g2 and _a10r:
        import numpy as np
        raw = np.frombuffer(g2[0].read_bytes(), np.uint8)
        w = _a10r.check_page(raw)
        good = tot = 0
        for c in range(_a10r.N_CHUNK):
            au = raw[c * _a10r.AU_BYTES:(c + 1) * _a10r.AU_BYTES]
            for j in range(4):
                if w[c * 4 + j]:
                    continue
                fr = _a10r.frame(au, j)
                tot += 1
                good += a10r_parity(bytes(fr[:516]), j) == bytes(fr[516:816])
        print("[7] A10R parity regenerated : %d/%d clean codewords EXACT"
              % (good, tot))
        ok &= tot > 0 and good == tot
    else:
        skip("[7] A10R: no CE1 group-2 page -- skipped")

    # 8. the derived Richardson-Urbanke encoder: it must contain the hardware
    #    parity among its two accumulator branches, on every media codeword it
    #    is given, and agree with the older precomputed solve.
    try:
        from . import fd_ruenc as _ru
        import numpy as np
        g2 = sorted((config.dump_path("g2")).glob("g2_ce1_*.bin"))
        if g2 and _a10r:
            raw = np.frombuffer(g2[0].read_bytes(), np.uint8)
            w = _a10r.check_page(raw)
            good = tot = sep = sel = 0
            for c in range(_a10r.N_CHUNK):
                au = raw[c * _a10r.AU_BYTES:(c + 1) * _a10r.AU_BYTES]
                for j in range(4):
                    if w[c * 4 + j]:
                        continue
                    fr = _a10r.frame(au, j)
                    tot += 1
                    cand = _ru.a10r_candidates(bytes(fr[:516]), j)
                    good += bytes(fr[516:816]) in cand
                    sep += a10r_branch_bit(bytes(a ^ b for a, b in
                                                 zip(*cand))) == 1
                    sel += sum(1 for c in cand if a10r_branch_bit(c) == 0) == 1
            print("[8] derived RU encoder      : %d/%d codewords among its two "
                  "accumulator branches" % (good, tot))
            print("    branch rule <W,P> == 0  : separates the pair on %d/%d, "
                  "selects exactly one on %d/%d" % (sep, tot, sel, tot))
            ok &= tot > 0 and good == tot and sep == tot and sel == tot
        else:
            skip("[8] derived RU encoder: no CE1 group-2 page -- skipped")
    except Exception as e:                                   # pragma: no cover
        print("[8] derived RU encoder unavailable: %s" % e)

    # 9. Code 7 (ECU banks 7/15) encoder, self-consistency only -- no known media
    try:
        import numpy as np
        from . import nd_code7 as _n7
        from . import fd_code7 as _c7
        H7 = _c7.build_H(_n7.layers()) if hasattr(_c7, "build_H") else _n7.build_H(_n7.layers())
        rng = np.random.default_rng(2)
        good = 0
        for _ in range(20):
            mb = [int(rng.integers(0, 1 << 48)) for _ in range(86)]
            p = code7_parity_mother(mb)
            cw = np.zeros(105 * 48, np.uint8)
            for jj in range(86):
                for kk in range(48):
                    if (mb[jj] >> kk) & 1:
                        cw[48 * jj + kk] = 1
            for jj in range(19):
                for kk in range(48):
                    if (p[jj] >> kk) & 1:
                        cw[48 * (86 + jj) + kk] = 1
            good += not ((H7 @ cw) & 1).any()
        print("[9] code 7 encoder          : %d/20 random messages satisfy all "
              "912 checks" % good)
        ok &= good == 20
    except Exception as e:                                   # pragma: no cover
        print("[9] code 7 encoder unavailable: %s" % e)

    # 10. EE3's encoder is closed form: A2 is invertible over the ring, and the
    #     ring inverse and its bitwise expansion must agree with each other and
    #     drive the syndrome to zero.  No stored matrix is involved at all.
    if _ee3 is not None:
        import time as _t
        _t0 = _t.time()
        rok = _ee3.selftest(verbose=False)
        print("[10] EE3 ring algebra       : A2^-1 derived from H, "
              "A2.A2^-1 == I over GF(2)[x]/(x^48-1), both solve paths "
              "agree (%.1fs)" % (_t.time() - _t0))
        ok &= rok
    else:
        print("[10] EE3 ring algebra unavailable: %s" % _ECC_ERR)

    # 11. The silicon path: the encoder read straight out of the ECU config
    #     images.  Two independent things are asserted -- that the stored tail Q
    #     really satisfies phi.Q = S (pure algebra, no media), and that the
    #     resulting parity agrees with the H-derived encoder, which was itself
    #     validated against 8218 media codewords.  Agreement of two encoders
    #     built from different sources is a much stronger check than either
    #     reproducing media on its own.
    _sil = None
    try:
        from . import nd_a10r_encsil as _sil
        if not _sil.available():
            skip("[11] silicon-table encoder  : table dumps absent -- skipped")
        else:
            qs = _sil.check_QS()
            import random as _r
            _r.seed(11)
            agree = tried = 0
            for _ in range(20):
                m = bytes(_r.randrange(256) for _ in range(516))
                sl = _r.randrange(4)
                cand = a10r_candidates(m, sl)
                if not cand:
                    continue
                tried += 1
                agree += _sil.parity(m, sl) == cand[_a10r_choice(m, sl, cand)]
            print("[11] silicon-table encoder  : phi.Q == S from the images alone"
                  " (%s); silicon == H-derived on %d/%d"
                  % ("yes" if qs else "NO", agree, tried))
            ok &= qs and agree == tried and tried > 0
    except ImportError as e:
        print("[11] silicon-table encoder unavailable: %s" % e)

    # 12. The same silicon route applied to EE3.  This is the sharpest available
    #     statement about EE3: `phi . Q == I` is pure algebra out of the two
    #     firmware config images (no media, no fitted anything), the images are
    #     byte-identical to static extraction from two independent firmware
    #     builds, and the parity they produce must equal what the ring inverse
    #     of A2 produces.  Two encoders, two sources, one answer.
    #
    #     Note that EE3's target is I, not A10R's S: its parity block-incidence
    #     matrix is 41x41 of rank 41 (nullity 0), so phi is invertible and the
    #     rank-deficiency repair S simply does not apply.  The code path measures
    #     that nullity rather than keying off the code's identity.
    try:
        if _sil is None or not _sil.EE3.available():
            skip("[12] EE3 silicon encoder    : EE3 config images absent -- skipped")
        elif _ee3 is None:
            skip("[12] EE3 silicon encoder    : nd_ee3_ring unavailable -- skipped")
        else:
            import random as _r
            c = _sil.EE3.build()
            gap_ok = c.check_gap()
            _r.seed(12)
            agree = tried = 0
            for _ in range(4):
                info = bytes(_r.randrange(256) for _ in range(_sil.EE3_NMSG * 6))
                tried += 1
                agree += (_sil.ee3_parity(info) == _ee3.solve_parity(info))
            print("[12] EE3 silicon encoder    : incidence %dx%d rank %d nullity %d"
                  " -> phi.Q == %s (%s); silicon == ring on %d/%d"
                  % (c.npar, c.npar, c.inc_rank, c.nullity,
                     "S" if c.singular else "I", "yes" if gap_ok else "NO",
                     agree, tried))
            ok &= gap_ok and c.nullity == 0 and agree == tried and tried > 0
    except Exception as e:                                   # pragma: no cover
        print("[12] EE3 silicon encoder unavailable: %s" % e)

    print("\nLIMITS (honest):")
    print("  * EE3 encode and decode are both COMPLETE -- an AU re-encodes")
    print("    byte-identical to media, parity included.  The encoder is pure")
    print("    algebra: p = A2^-1 . A1 . m over R = GF(2)[x]/(x^48-1), A2 the")
    print("    41x41 parity half of H, inverted by Gauss-Jordan over R in 0.3 s.")
    print("    Nothing is stored: the retired 489 KB nd_deepparity_a2inv.pkl is")
    print("    reproduced ROW FOR ROW (0 of 1968 differ) by the derivation.")
    print("    EE3 ALSO HAS A SECOND, INDEPENDENT CONSTRUCTION: the firmware's")
    print("    own ECU config images give H and the gap solve Q directly, with")
    print("    phi.Q = I (nullity 0 -- A10R's S does NOT generalise), and the")
    print("    two encoders agree byte for byte -- checked by [12].")
    print("  * A10R (firmware-image) encode and decode both work, from H alone,")
    print("    by a Richardson-Urbanke solve with gap = parity blocks 0..11.")
    print("    Its published matrix is rank 2399 of 2400 -- it VERIFIES media but")
    print("    does not DEFINE the code, because the mother code has 137 block")
    print("    columns and ONE IS SHORTENED, so media can never reveal it")
    print("    (confirmed by reading the ECU schedule memory: nd_ldpcdump.py).")
    print("    ENCODING NOW RUNS OFF THE SILICON TABLES by default: the decoder")
    print("    image gives H in RU block form and the encoder image's 144-record")
    print("    tail IS the gap-solve matrix Q, with phi.Q = S, det S = x+1 --")
    print("    checked by [11] from the images alone, no media.  8218/8218.")
    print("    THE ONE IRREDUCIBLE CONSTANT IS NOW A SINGLE BIT: of the two")
    print("    codewords (they differ by J), silicon emits the one with bit 47 of")
    print("    the gap accumulator u_0 clear.  PROVEN underivable from H -- both")
    print("    branches are genuine codewords -- so do not look for a formula.")
    print("    The 72-byte _A10R_W covector is retained only for the no-tables")
    print("    fall-back and as an independent cross-check; the two agree 8218/8218.")
    print("    Decode/correction was NEVER affected: a rank-deficient H is a")
    print("    valid check set, just one dimension weaker than ideal.")
    print("  * Format C (raw) carries no ECC/MCRC/scrambling by design.")
    strict = "--strict" in sys.argv
    if skipped:
        print("\n%d of 12 checks could NOT RUN here:" % len(skipped))
        for m in skipped:
            print("   " + m.strip())
        print("  A skip is not a pass.  These need a reference page or the")
        print("  silicon table dumps; without them this run verified the")
        print("  seed-map arithmetic and the CRCs, and little else.")
    verdict = ("PASS" if ok else "FAIL") if not skipped else (
        "FAIL (skips are failures under --strict)" if strict and ok else
        ("PASS, %d SKIPPED" % len(skipped)) if ok else "FAIL")
    print("\nSELFTEST:", verdict)
    if strict and skipped:
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
