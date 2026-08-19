"""The two MCRCs and the scrambler LFSR, against known vectors.

None of this needs media.  The one genuinely EXTERNAL check in the whole test
suite lives here: the 4-byte MCRC polynomial is CRC-32Q (a.k.a. CRC-32/AIXM), a
catalogued CRC whose check value was published by somebody who has never seen a
PS5.  If `crc32q("123456789")` is 0x3010BF7F then the polynomial, the bit order,
the init value and the absence of reflection are all confirmed from outside this
project.  Everything else here is a structural identity the docstrings state.
"""
import random

import pytest


def popcount(buf):
    return sum(bin(b).count("1") for b in buf)


# --------------------------------------------------------------------- MCRC --
def test_crc32q_catalogue_check_value(tn):
    """CRC-32Q / CRC-32-AIXM: poly 0x814141AB, init 0, no reflection, no xorout.

    Published check value for the string "123456789".  This is the only vector
    in the suite that comes from outside the project.
    """
    assert tn.crc32q(b"123456789") == 0x3010BF7F


def test_polynomials_are_the_documented_ones(tn):
    assert tn._POLY32 == 0x814141AB
    # x^48 + x^47 + x^6 + x^5 + x + 1
    assert tn._POLY48 == (1 << 48) | (1 << 47) | (1 << 6) | (1 << 5) | 2 | 1
    assert tn._POLY48 == 0x1800000000063


@pytest.mark.parametrize("crc,width", [("crc48", 6), ("crc32q", 4)])
def test_x_plus_one_factor_gives_even_codeword_weight(tn, crc, width):
    """Both polynomials factor as (x+1) * primitive -- the vendor construction.

    A consequence, and a real check on the implementation: message||crc always
    has even Hamming weight, so every odd-weight error is detected.
    """
    f = getattr(tn, crc)
    rnd = random.Random(4104)
    for _ in range(300):
        m = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 96)))
        assert (popcount(m) + popcount(f(m).to_bytes(width, "big"))) % 2 == 0


@pytest.mark.parametrize("crc", ["crc48", "crc32q"])
def test_crc_is_linear_at_init_zero(tn, crc):
    """With init=0 and no xorout a CRC is GF(2)-linear on equal-length inputs.

    crc(a ^ b) == crc(a) ^ crc(b).  If someone ever adds an init constant or an
    output xor, this is what notices.
    """
    f = getattr(tn, crc)
    rnd = random.Random(48)
    for _ in range(300):
        n = rnd.randrange(1, 96)
        a = bytes(rnd.randrange(256) for _ in range(n))
        b = bytes(rnd.randrange(256) for _ in range(n))
        c = bytes(x ^ y for x, y in zip(a, b))
        assert f(c) == f(a) ^ f(b)


def test_crc_init_parameter_is_a_real_state(tn):
    """Feeding a CRC in two halves must equal doing it in one."""
    rnd = random.Random(7)
    m = bytes(rnd.randrange(256) for _ in range(200))
    assert tn.crc48(m[100:], tn.crc48(m[:100])) == tn.crc48(m)
    assert tn.crc32q(m[100:], tn.crc32q(m[:100])) == tn.crc32q(m)


def test_au_mcrc_complements_the_stored_bytes(tn):
    """"Both MCRCs are over COMPLEMENTED stored bytes" -- spelled out."""
    rnd = random.Random(1)
    m = bytes(rnd.randrange(256) for _ in range(4104))
    inv = bytes(b ^ 0xFF for b in m)
    assert tn.au_mcrc(m, 6) == tn.crc48(inv).to_bytes(6, "big")
    assert tn.au_mcrc(m, 4) == tn.crc32q(inv).to_bytes(4, "big")
    assert len(tn.au_mcrc(m, 6)) == 6
    assert len(tn.au_mcrc(m, 4)) == 4


def test_au_mcrc_rejects_other_widths(tn):
    with pytest.raises(ValueError):
        tn.au_mcrc(b"\0" * 4104, 5)


# ---------------------------------------------------------------- scrambler --
def test_seed_map_input_folds_to_fourteen_bits(tn):
    """The seed is a function of the row's low 14 bits, and of nothing else.

    Verified globally in the module's own selftest against the 16384-entry map;
    here it is checked as the identity it is, with no map present.
    """
    assert tn.FOLD_MASK == 0x3FFF
    rnd = random.Random(53)
    for _ in range(500):
        row = rnd.randrange(1 << 24)
        assert tn.seed_state(row) == tn.seed_state(row & tn.FOLD_MASK)


def test_seed_state_is_a_53_bit_value(tn):
    assert tn.LFSR_BITS == 53
    for row in range(0, 16384, 61):
        st = tn.seed_state(row)
        assert 0 <= st < (1 << 53)


def test_every_real_seed_passes_the_free_parity_check(tn):
    """`state_valid` is the (x+1)-component: 1 for every state the map emits.

    A free 1-bit check on a recovered seed.  It has to hold for all 16384 or it
    is not a check at all, so this walks the whole domain.
    """
    bad = [r for r in range(16384) if not tn.state_valid(tn.seed_state(r))]
    assert bad == []


def test_seed_map_is_not_trivial(tn):
    """Guard against a stubbed-out map: the 16384 seeds must be well spread."""
    seeds = {tn.seed_state(r) for r in range(16384)}
    assert len(seeds) > 16000                    # near-injective, not constant


def test_keystream_obeys_the_published_taps(tn):
    """b[N] = b[N-1] ^ b[N-49] ^ b[N-50] ^ b[N-52] ^ b[N-53]."""
    assert tn._TAPS == (1, 49, 50, 52, 53)
    st = tn.seed_state(0x1234)
    ks = tn.keystream(st, 96)
    b = [(ks[i // 8] >> (7 - i % 8)) & 1 for i in range(len(ks) * 8)]
    for n in range(53, len(b)):
        assert b[n] == b[n - 1] ^ b[n - 49] ^ b[n - 50] ^ b[n - 52] ^ b[n - 53]


def test_state_is_the_first_53_keystream_bits(tn):
    """The property the whole seed-recovery route rests on."""
    for row in (0, 1, 0x1234, 0x3FFF):
        st = tn.seed_state(row)
        ks = tn.keystream(st, 8)
        bits = [(ks[i // 8] >> (7 - i % 8)) & 1 for i in range(53)]
        assert sum(bit << (52 - i) for i, bit in enumerate(bits)) == st


def test_keystream_cache_returns_equal_bytes(tn):
    """It is memoised and hands the same object to several callers.

    So it must be immutable `bytes`, not a bytearray somebody can mutate under
    the next caller.
    """
    st = tn.seed_state(9)
    a = tn.keystream(st, 4104)
    b = tn.keystream(st, 4104)
    assert a == b
    assert isinstance(a, bytes)


def test_scrambler_is_involutive(tn):
    rnd = random.Random(2)
    st = tn.seed_state(0x2A)
    d = bytes(rnd.randrange(256) for _ in range(4096))
    assert tn.apply_scrambler(tn.apply_scrambler(d, st), st) == d
    assert tn.scramble is tn.apply_scrambler
    assert tn.descramble is tn.apply_scrambler


def test_state_from_aux_recovers_the_seed(tn):
    """Aux plaintext + stored aux -> the state, with 11 spare check bits."""
    st = tn.seed_state(0x0BAD)
    plain = bytes(range(8))
    stored = bytes(a ^ b for a, b in zip(plain, tn.keystream(st, 8)))
    assert tn.state_from_aux(stored, plain) == st
    assert tn.aux_check(stored, plain) is True


def test_aux_check_rejects_a_wrong_plaintext(tn):
    """The 11 spare bits must actually reject, or the check is decoration."""
    st = tn.seed_state(0x0BAD)
    plain = bytes(range(8))
    stored = bytes(a ^ b for a, b in zip(plain, tn.keystream(st, 8)))
    wrong = bytes([plain[0] ^ 0x80]) + plain[1:]
    assert tn.aux_check(stored, wrong) is False


def test_format_tags_are_distinct(tn):
    tags = [tn.FORMAT_A, tn.FORMAT_B, tn.FORMAT_C, tn.FORMAT_D]
    assert len(set(tags)) == 4
