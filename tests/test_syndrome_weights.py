"""The numpy syndrome must equal the big-integer one it replaced.

`nd_ldpc_odd._syndrome` builds a 1968-bit Python integer by XORing rotated
48-bit chunks -- readable, and the form the EE3 work was done in.  It is also
~3850 big-integer shifts per codeword, which measured 10.7 ms of a 17.5 ms ECC
page and was the single largest cost in the viewer's map.

`syndrome_weights` gathers the same matrix in numpy.  Same answer or it is not
worth having, so: same answer, on the zero vector, on structured inputs, on
random ones, and batched against one-at-a-time.
"""
import pytest

np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def O():
    return pytest.importorskip("titania.nd_ldpc_odd")


def _weight_ref(O, mother):
    """The reference weight, via the big-integer path."""
    b = bytes(mother)
    return bin(O._syndrome(b[:O.NINFO * 6], b[O.NINFO * 6:])).count("1")


def _mothers(O, n, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(n, O.NB * 6), dtype=np.uint8)


def test_the_zero_codeword_has_weight_zero(O):
    z = np.zeros((1, O.NB * 6), np.uint8)
    assert int(O.syndrome_weights(z)[0]) == 0
    assert _weight_ref(O, z[0]) == 0


def test_it_agrees_with_the_big_integer_form_on_random_codewords(O):
    ms = _mothers(O, 8, seed=19)
    got = [int(w) for w in O.syndrome_weights(ms)]
    want = [_weight_ref(O, m) for m in ms]
    assert got == want


def test_it_agrees_on_structured_inputs_too(O):
    """All-ones and single-bit inputs: the cases where a wrong bit order or a
    wrong rotation direction still happens to give a plausible weight on random
    data, because random data has weight ~half whatever you do."""
    cases = [np.full(O.NB * 6, 0xFF, np.uint8), np.zeros(O.NB * 6, np.uint8)]
    for bit in (0, 1, 47, 48, 6527, O.NB * 48 - 1):
        m = np.zeros(O.NB * 6, np.uint8)
        m[bit // 8] = 1 << (7 - bit % 8)
        cases.append(m)
    ms = np.array(cases)
    got = [int(w) for w in O.syndrome_weights(ms)]
    want = [_weight_ref(O, m) for m in ms]
    assert got == want


def test_a_batch_equals_the_same_codewords_one_at_a_time(O):
    ms = _mothers(O, 5, seed=23)
    batched = [int(w) for w in O.syndrome_weights(ms)]
    singly = [int(O.syndrome_weights(m[None, :])[0]) for m in ms]
    assert batched == singly


def test_a_bare_one_dimensional_codeword_is_accepted(O):
    m = _mothers(O, 1, seed=29)[0]
    assert int(O.syndrome_weights(m)[0]) == _weight_ref(O, m)


def test_the_deepparity_helpers_go_through_it(O):
    """`syn_even_raw` / `syn_even_cpl` / `syn_odd_u` are what the viewer calls;
    they must give what the big-integer path gave for the same block."""
    dp = pytest.importorskip("titania.nd_deepparity")
    rng = np.random.default_rng(31)
    block = rng.integers(0, 256, size=O.STRIDE, dtype=np.uint8)
    b = bytes(block)
    assert dp.syn_even_raw(block) == bin(
        O._syndrome(b[:2056], b[2056:2302])).count("1")
    inv = bytes(x ^ 0xFF for x in b)
    assert dp.syn_even_cpl(block) == bin(
        O._syndrome(inv[:2056], inv[2056:2302])).count("1")


def test_the_rust_worker_agrees_with_both_python_forms(O):
    """Op 3 answers the same question the decoder's first iteration does.

    If it did not, a page could be called clean by the map and then need
    correcting when someone opened it -- or the reverse, which is worse.
    """
    rs = pytest.importorskip("titania.nd_ldpc_rs")
    if not rs.available():
        pytest.skip("no Rust worker: %s" % rs.why_unavailable())
    ms = _mothers(O, 6, seed=37)
    vs = [np.unpackbits(m) for m in ms]
    assert rs.syndrome_weights(vs) == [_weight_ref(O, m) for m in ms]
    assert rs.syndrome_weights([]) == []


def test_the_page_helper_batches_the_same_eight_weights(O):
    """`page_syndromes_norm` does all eight codewords in one call; it must
    still be the eight `syn_even_cpl` / `syn_odd_u` gave one at a time."""
    dp = pytest.importorskip("titania.nd_deepparity")
    rng = np.random.default_rng(41)
    a = rng.integers(0, 256, size=18432, dtype=np.uint8)
    one = []
    for au in range(4):
        b0 = a[au * 2 * dp.STRIDE:(au * 2 + 1) * dp.STRIDE]
        one.append(dp.syn_even_cpl(b0))
        one.append(dp.syn_odd_u(dp._u_of(a, au)))
    assert dp.page_syndromes_norm(a) == one


# ---------------------------------------------------------------------------
# op 4: the AU MCRC, the other half of a page's verdict
# ---------------------------------------------------------------------------
def test_the_rust_mcrc_matches_titania_nand_at_both_widths():
    """`au_mcrc` is a byte-at-a-time 48-bit register; the Rust one has to be the
    same register after every byte, at both widths the formats use."""
    rs = pytest.importorskip("titania.nd_ldpc_rs")
    tn = pytest.importorskip("titania.titania_nand")
    if not rs.available():
        pytest.skip("no Rust worker: %s" % rs.why_unavailable())
    rng = np.random.default_rng(43)
    msgs = [bytes(rng.integers(0, 256, size=tn.MSG_BYTES, dtype=np.uint8))
            for _ in range(5)]
    for width in (6, 4):
        assert rs.au_mcrcs(msgs, width) == [tn.au_mcrc(m, width) for m in msgs]


def test_the_rust_mcrc_handles_the_edges():
    rs = pytest.importorskip("titania.nd_ldpc_rs")
    tn = pytest.importorskip("titania.titania_nand")
    if not rs.available():
        pytest.skip("no Rust worker: %s" % rs.why_unavailable())
    assert rs.au_mcrcs([]) == []
    for msg in (b"\x00" * tn.MSG_BYTES, b"\xFF" * tn.MSG_BYTES):
        assert rs.au_mcrcs([msg], 6) == [tn.au_mcrc(msg, 6)]
    with pytest.raises(ValueError):
        rs.au_mcrcs([b"\x00" * 8], 5)
    with pytest.raises(ValueError):
        rs.au_mcrcs([b"\x00" * 8, b"\x00" * 9], 6)


def test_the_page_mcrc_agrees_with_the_pure_python_path():
    """`_mcrc_of_page` is what the viewer calls.  Rust on, Rust off, same four
    verdicts -- including the erased AUs, which never reach a CRC at all."""
    pagefmt = pytest.importorskip("nandgui.pagefmt")
    tn = pytest.importorskip("titania.titania_nand")
    rng = np.random.default_rng(47)
    page = bytearray(rng.integers(0, 256, size=18432, dtype=np.uint8).tobytes())
    page[2 * 4608:3 * 4608] = b"\xFF" * 4608          # one erased AU
    got = pagefmt._mcrc_of_page(tn, bytes(page), 6)

    want = []
    for i in range(4):
        au = bytes(page[i * 4608:(i + 1) * 4608])
        if tn.au_is_erased(au):
            want.append(None)
            continue
        lg = tn.au_logical(au)
        want.append(bytes(lg[tn.MSG_BYTES:tn.MSG_BYTES + 6])
                    == tn.au_mcrc(lg[:tn.MSG_BYTES], 6))
    assert list(got) == want
    assert got[2] is None
