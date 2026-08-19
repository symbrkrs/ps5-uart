"""`decode_pages` is `decode_page` in a loop, and must stay that way.

The batched form exists only to fill the machine: one page is four independent
codewords, and handing four at a time to a corrector that spreads a batch over
every core left a 64-core box idle.  Merging the batches across pages is a
REGROUPING of identical work, so the two forms have to agree on both halves of
the result:

  * the corrected bytes, which are what gets read back as data;
  * the per-codeword log, which is what the map turns into CLEAN / CORRECTED /
    PARTIAL -- a difference there is a difference in what the viewer paints,
    even when every byte matches.

No media: the pages are fabricated, and most of their codewords will fail to
converge.  That is fine and is arguably the sharper test -- the two forms have
to agree about failure too, and the failure path is the one with the early
`continue` that a regrouping could get wrong.
"""
import pytest

np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def dp():
    mod = pytest.importorskip("titania.nd_deepparity")
    if not hasattr(mod, "decode_pages"):
        pytest.skip("this build has no decode_pages")
    return mod


def _pages(n, seed=11):
    """`n` fabricated 18432-byte pages, deterministic."""
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 256, size=18432, dtype=np.uint8) for _ in range(n)]


def test_the_batched_form_matches_the_loop_byte_for_byte(dp):
    raws = _pages(3)
    one = [dp.decode_page(r) for r in raws]
    many = dp.decode_pages(raws)
    assert len(many) == len(one)
    for i, ((fa, la), (fb, lb)) in enumerate(zip(one, many)):
        assert np.array_equal(np.asarray(fa), np.asarray(fb)), \
            "page %d: corrected bytes differ" % i
        assert list(la) == list(lb), "page %d: per-codeword log differs" % i


def test_the_batched_form_does_not_mutate_its_input(dp):
    """`decode_page` copies its argument; the batched one must too.

    A scan hands over pages it read from the store and then reports on the
    ORIGINAL bytes -- syndromes, MCRC-before.  Correcting in place would make
    those numbers describe the repaired page and quietly turn every measurement
    into a tautology.
    """
    raws = _pages(2, seed=5)
    before = [r.copy() for r in raws]
    dp.decode_pages(raws)
    for i, (a, b) in enumerate(zip(before, raws)):
        assert np.array_equal(a, b), "page %d was modified in place" % i


def test_an_empty_batch_is_empty_and_not_an_error(dp):
    assert dp.decode_pages([]) == []


def test_one_page_batched_equals_that_page_alone(dp):
    """The n=1 case is the one a caller reaches by accident, so it is pinned."""
    raw = _pages(1, seed=3)[0]
    (fa, la), = dp.decode_pages([raw])
    fb, lb = dp.decode_page(raw)
    assert np.array_equal(np.asarray(fa), np.asarray(fb))
    assert list(la) == list(lb)


# ---------------------------------------------------------------------------
# the layer above: pagefmt.ecc_of_pages, which is what the block scan calls
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pagefmt():
    mod = pytest.importorskip("nandgui.pagefmt")
    if not hasattr(mod, "ecc_of_pages"):
        pytest.skip("this build has no ecc_of_pages")
    return mod


def _items(pagefmt):
    """A spread that reaches every branch of the dispatch, without media."""
    raws = _pages(4, seed=17)
    out = [(bytes(r), 3, 100 + i, i) for i, r in enumerate(raws)]
    out.append((b"\xFF" * 18432, 3, 200, 0))       # erased: no decode
    out.append((b"\x00" * 18432, 3, 200, 1))       # all-zero: no decode
    out.append((None, 3, 200, 2))                  # no bytes at all
    out.append((b"\xAB" * 99, 3, 200, 3))          # wrong length
    out.append((bytes(raws[0]), 1, 0, 0))          # the A10R region
    return out


def test_ecc_of_pages_matches_ecc_of_page_field_for_field(pagefmt):
    """The batched scan path must paint exactly what the per-page path did.

    `EccResult` is what the map colours and what the inspector prints, so every
    field is compared -- except `seconds`, which is legitimately different: a
    page decoded in a batch is charged the batch, and that is the honest number
    now that a request is the unit of work.
    """
    import dataclasses
    items = _items(pagefmt)
    one = [pagefmt.ecc_of_page(r, ce=c, block=b, page=p) for r, c, b, p in items]
    many = pagefmt.ecc_of_pages(items)
    assert len(many) == len(one)
    for (_r, c, b, p), ra, rb in zip(items, one, many):
        da = {k: v for k, v in dataclasses.asdict(ra).items() if k != "seconds"}
        db = {k: v for k, v in dataclasses.asdict(rb).items() if k != "seconds"}
        assert da == db, "ce%s block%s page%s differs" % (c, b, p)


def test_ecc_of_pages_keeps_the_caller_s_order(pagefmt):
    """Results are positional.  A page answered without decoding sits between
    two that were decoded, so a batch that returned only its own members in
    completion order would silently reattribute every verdict after it."""
    items = _items(pagefmt)
    many = pagefmt.ecc_of_pages(items)
    assert len(many) == len(items)
    # the erased and all-zero fills are at fixed positions and must stay there
    assert many[4].state == pagefmt.EccState.ERASED
    assert many[5].state == pagefmt.EccState.ZERO_PAGE


def test_an_empty_page_list_is_empty(pagefmt):
    assert pagefmt.ecc_of_pages([]) == []
