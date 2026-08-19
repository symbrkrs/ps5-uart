"""The media format: scrambler, MCRC, AU layout, and both LDPC codes.

This is the layer that turns the bytes a NAND reader pulls off the die into
plaintext, and plaintext back into the bytes the drive would have written.  It
needs no hardware, no firmware, no console keys and no seed material.

    from titania import codec

    page = dev.read_page(ce=1, block=3608, page=8)
    res  = codec.decode_page(page, row=8)          # ECC -> MCRC -> descramble
    res.aus[0].user, res.aus[0].mcrc_ok

    raw  = codec.encode_page([codec.AuPlain(user, aux)] * 4, row=8)

WHERE THE CODE ACTUALLY LIVES, AND WHY
--------------------------------------
Every function here is re-exported from `titania_nand.py` at the repository
root.  That module is the VERIFIED artefact -- `py titania_nand.py --selftest` is
its contract, a dozen scripts import it directly, and its numbers are the ones
quoted in NAND_OFFLINE_DECODE.md.  It is not moved or rewritten
into the package; this module is a documented facade over it, so that the
package has one clean surface while the thing under test stays exactly where the
tests point.  If you need something not re-exported here, import `titania_nand`
directly -- nothing is hidden.

WHAT IS VERIFIED, AND HOW
-------------------------
  * Byte-identical re-encode.  A real TLC page is decoded and re-encoded and
    comes back identical over all 4608 bytes of every AU -- message, MCRC,
    scrambling, both EE3 parities and the LDPC pipeline tail.  4/4 AUs.
  * The scrambler seed map.  `seed_state` reproduces all 16384 entries of a
    hardware-measured map exactly, from 325 bytes of tables rather than a 64 KB
    blob, and was separately confirmed on the board through the ECU's own seed
    map bypass.
  * Both LDPC codes encode and decode.  EE3 (main media, 246 B parity per
    codeword) and A10R (firmware images, 300 B) are both quasi-cyclic with
    Z = 48 over R = GF(2)[x]/(x^48-1), and both encoders are closed form,
    recomputed from H at run time with no stored encoder matrix:
      - EE3 by a direct ring inverse, `p = A2^-1 . A1 . m`;
      - A10R by a Richardson-Urbanke solve, gap = parity blocks 0..11.
    A10R regenerates 8218 / 8218 real media codewords byte-exactly, off five
    dies and all four slots.
  * Each code has a second, independent construction read straight out of
    the ECU's own config images (`nd_a10r_encsil.py`): the decoder image gives H
    in RU block form and the encoder image's 144-record tail is the gap-solve
    matrix Q.  The two constructions agree byte for byte on both codes.  What
    `phi . Q` equals is decided by the code's parity block-incidence nullity and
    is MEASURED, never assumed: A10R and code 7 are nullity 1 and get the
    singular repair S (`det S = x+1`); EE3 is nullity 0, phi is invertible, and
    the hardware stores `Q = phi^-1` with `phi . Q = I`, so EE3 carries no
    convention at all.

Honest limits, in the same breath:

  * A10R decode was never in question, but its published H is rank 2399 of 2400,
    so it verifies media without defining the code.  Of the two parities every
    message admits, which one the silicon emits is one bit of media evidence and
    is proven underivable from H.  Do not go looking for a formula.
  * `encode_au` is complete for formats A/C/D.  Format B (A10R) is decode-only
    at the AU level because the 512x4 frame container is not wired in here;
    frame-level A10R encoding is complete via `a10r_encode_frame`.
  * Format D (RAID parity) is a read-path ECU setting and does not appear on
    media; it is present for completeness.

LAYER ORDER -- IT DIFFERS BY REGION, AND GETTING IT BACKWARDS FAILS SILENTLY
---------------------------------------------------------------------------
    EE3  (main media)       parity covers the descrambled bytes -> ECC, then
                            descramble the corrected bytes
    A10R (firmware images)  parity covers the scrambled bytes

Both MCRCs are computed over complemented stored bytes, so they validate a raw
dump with no seed, no key and no ECC -- which makes `au_mcrc` the cheapest
possible "did I read this page correctly" test.  `decode_page` applies the right
order for the format it is told to use.

    The one thing this layer cannot fix for you: the firmware area (blocks
    0..27) is pseudo-SLC and must be read with a 0xDA (Samsung, Micron) or 0xA2
    (Toshiba, Hynix, SanDisk) prefix, 368 pages per block; main user data is
    native TLC, no prefix, 1104 pages per block.  Read the firmware area without
    the prefix and the bit rot looks like three unrelated bugs.  See
    `titania.geometry` and `titania.device` for the mode handling.

None of the format constants vary with the board testpoint straps or the NAND
vendor -- established by static analysis of the boot ROM and CPU1 firmware
(NAND_TP_VENDOR_VARIANTS.md).  The straps move interface mode, timing and vref
only; the five supported vendors differ in bus addressing and that one SLC
prefix byte.
"""
import sys as _sys
from pathlib import Path as _Path

# titania_nand.py sits at the repository root, one level above the package.
_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from . import titania_nand as _tn                                    # noqa: E402

#: version of the underlying verified module
BACKEND_VERSION = _tn.__version__

# --- geometry (bytes; addressing lives in titania.geometry) ---------------
PAGE_BYTES = _tn.PAGE_BYTES            # 18432, a full stored page
AU_STRIDE = _tn.AU_STRIDE              # 4608, one AU as stored (2 codewords)
CW_BYTES = _tn.CW_BYTES                # 2304, one codeword as stored
AUS_PER_PAGE = _tn.AUS_PER_PAGE        # 4
MSG_BYTES = _tn.MSG_BYTES              # 4104 = 8 aux + 4096 user
USER_BYTES = _tn.USER_BYTES            # 4096
AUX_BYTES = _tn.AUX_BYTES              # 8
EE3_PARITY = _tn.EE3_PARITY            # 246 bytes per codeword
A10R_PARITY = _tn.A10R_PARITY          # 300 bytes per codeword

FW_PAGES_PER_BLOCK = _tn.FW_PAGES_PER_BLOCK    # 368, pseudo-SLC
TLC_PAGES_PER_BLOCK = _tn.TLC_PAGES_PER_BLOCK  # 1104, native TLC
SLC_PREFIX = _tn.SLC_PREFIX                    # vendor -> pseudo-SLC command

# --- AU formats -----------------------------------------------------------
# key off GEOMETRY, never off the ecu's format index: the index -> purpose map
# is not stable across firmware revisions (2021 firmware puts raw at 18/19,
# this board at 20/21).
FORMAT_A = _tn.FORMAT_A    # main media: EE3, 6-byte MCRC, scrambled
FORMAT_B = _tn.FORMAT_B    # firmware images: 512x4, A10R, 4-byte MCRC, scrambled
FORMAT_C = _tn.FORMAT_C    # raw/long: no ECC, no MCRC, not scrambled
FORMAT_D = _tn.FORMAT_D    # RAID parity: as a but 4-byte MCRC; read-path only
FORMATS = (FORMAT_A, FORMAT_B, FORMAT_C, FORMAT_D)

# --- result / input types -------------------------------------------------
AuPlain = _tn.AuPlain          # plaintext of one AU: .user, .aux, .message()
AuResult = _tn.AuResult        # decode outcome: .user .aux .mcrc_ok .ecc_ok .ok
PageResult = _tn.PageResult    # .aus, .row, .ok, .user()

# --- page / AU level ------------------------------------------------------
decode_page = _tn.decode_page  # (stored_page, row, fmt=FORMAT_A, correct=True)
encode_page = _tn.encode_page  # (plains, row, fmt=FORMAT_A, **kw)
decode_au = _tn.decode_au      # (stored_au, row=|state=, fmt=FORMAT_A)
encode_au = _tn.encode_au      # (AuPlain, row=|state=, fmt=FORMAT_A)

# `row` is required (or an explicit `state`): the scrambler seed is a function of
# the row and of nothing else, so a page cannot be descrambled without it.

# --- AU container ---------------------------------------------------------
au_logical = _tn.au_logical              # stored 4608 -> 4110 logical, ks order
au_place_logical = _tn.au_place_logical  # logical -> stored, parity untouched
au_is_erased = _tn.au_is_erased          # all-0xFF / all-0x00 head

# --- scrambler ------------------------------------------------------------
seed_state = _tn.seed_state              # row -> 53-bit LFSR state (325 B of tables)
state_valid = _tn.state_valid            # free 1-bit sanity check on a state
keystream = _tn.keystream                # (state, nbytes) -> MSB-first keystream
apply_scrambler = _tn.apply_scrambler    # involutive: scramble == descramble
scramble = _tn.scramble
descramble = _tn.descramble
state_from_aux = _tn.state_from_aux      # recover a state from a known aux plaintext
aux_check = _tn.aux_check                # 11 spare bits of confirmation on that
LFSR_BITS = _tn.LFSR_BITS                # 53
FOLD_MASK = _tn.FOLD_MASK                # the seed map input is row & 0x3FFF

# --- MCRC -----------------------------------------------------------------
# Both polynomials factor as (x+1) * primitive -- one vendor construction at two
# widths -- and both run over the complemented stored bytes.
au_mcrc = _tn.au_mcrc                    # (stored_message, width=6) -> bytes
crc48 = _tn.crc48                        # 0x1800000000063
crc32q = _tn.crc32q                      # 0x814141AB (CRC-32Q / CRC-32-AIXM)

# --- LDPC: EE3 (main media) ----------------------------------------------
ecc_available = _tn.ecc_available        # is the LDPC stack importable?
ecc_correct_page = _tn.ecc_correct_page  # correct all 8 EE3 codewords of a page
ecc_encode_even = _tn.ecc_encode_even    # write EE3 parity into the even block
ecc_encode_odd = _tn.ecc_encode_odd      # ... and the odd one, plus the tail
ee3_parity = _tn.ee3_parity              # (info, source="ring"|"silicon") -> 246 B

# --- LDPC: A10R (firmware images) and code 7 -----------------------------
a10r_parity = _tn.a10r_parity            # (message516, slot) -> 300 parity bytes
a10r_encode_frame = _tn.a10r_encode_frame  # rewrite an 816-byte frame's parity
a10r_candidates = _tn.a10r_candidates    # both admissible parities (H alone)
a10r_branch_bit = _tn.a10r_branch_bit    # <W,P>: 0 for the parity silicon writes
code7_parity_mother = _tn.code7_parity_mother  # ECU banks 7/15; no known media

__all__ = [
    "BACKEND_VERSION",
    # geometry
    "PAGE_BYTES", "AU_STRIDE", "CW_BYTES", "AUS_PER_PAGE", "MSG_BYTES",
    "USER_BYTES", "AUX_BYTES", "EE3_PARITY", "A10R_PARITY",
    "FW_PAGES_PER_BLOCK", "TLC_PAGES_PER_BLOCK", "SLC_PREFIX",
    # formats
    "FORMAT_A", "FORMAT_B", "FORMAT_C", "FORMAT_D", "FORMATS",
    # types
    "AuPlain", "AuResult", "PageResult",
    # page / AU
    "decode_page", "encode_page", "decode_au", "encode_au",
    "au_logical", "au_place_logical", "au_is_erased",
    # scrambler
    "seed_state", "state_valid", "keystream", "apply_scrambler", "scramble",
    "descramble", "state_from_aux", "aux_check", "LFSR_BITS", "FOLD_MASK",
    # MCRC
    "au_mcrc", "crc48", "crc32q",
    # LDPC
    "ecc_available", "ecc_correct_page", "ecc_encode_even", "ecc_encode_odd",
    "ee3_parity", "a10r_parity", "a10r_encode_frame", "a10r_candidates",
    "a10r_branch_bit", "code7_parity_mother",
    # introspection
    "silicon_code", "selftest",
]


def silicon_code(name="A10R"):
    """The requested code as read out of the ECU's own config images.

    -> a `nd_a10r_encsil.SiliconCode` with `.npar`, `.tri`, `.nullity`,
    `.singular`, `.Q`, `.phi()`, `.check_gap()` and `.parity_mother()`.

    Useful when you want the geometry or want to re-run the image-only algebra
    (`check_gap()` proves the stored tail really is the gap solve for the H in
    the decoder image, using no codewords at all).  Raises if that code's images
    are not present -- A10R's come off silicon, EE3's from a firmware capture.
    """
    from . import nd_a10r_encsil as _sil
    try:
        code = {"A10R": _sil.A10R, "EE3": _sil.EE3}[name.upper()]
    except KeyError:
        raise ValueError("unknown code %r (have A10R, EE3)" % name)
    if not code.available():
        raise RuntimeError("%s config images are not present (%s, %s)"
                           % (code.name, code.enc_image, code.dec_image))
    return code.build()


def selftest():
    """Run the backend's own contract: `py titania_nand.py --selftest`.

    -> 0 on success.  It is the real check for this layer -- seed map against
    16384 hardware-measured entries, byte-identical page re-encode, both LDPC
    encoders against media and against their silicon-image twins.
    """
    return _tn._selftest()
