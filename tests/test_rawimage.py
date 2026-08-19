"""The raw-external-reader backing: addressing, and the four refusals.

None of this opens a dump, a store or a serial port -- it builds a tiny image
out of fabricated bytes in a tmpdir.  The checks against this board's real pages
(the 28 cell-mode A/B pairs, the byte-exact round trip, the full decode) live in
`titania.rawimage._selftest`, next to the evidence.

What is being defended here is the set of properties that make a raw capture
INTERPRETABLE at all, and every one of them has a corresponding way to lose data
silently:

  * a page is 18432 bytes INCLUDING the spare -- AU 3 straddles the 16384
    boundary, so a reader that drops or reorders the spare destroys it;
  * cell mode is part of the address, not a hint -- a pseudo-SLC page read
    without the prefix comes off a different word line and returns plausible
    bytes;
  * a hole is not erased media;
  * two readings of one address are two streams at different retry levels, or
    they are a mistake.
"""
import json

import pytest


@pytest.fixture(scope="session")
def ri():
    return pytest.importorskip("titania.rawimage")


PAGE = 18432


def _page(seed):
    return bytes((seed * 7 + i * 31) & 0xFF for i in range(PAGE))


@pytest.fixture()
def image(tmp_path, ri):
    """One die, two blocks x 4 pages, pseudo-SLC, with one hole."""
    holes = [(10, 2)]
    with open(tmp_path / "die01_slc.bin", "wb") as f:
        for b in (10, 11):
            for p in range(4):
                f.write(b"\x00" * PAGE if (b, p) in holes else _page(b * 8 + p))
    s = ri.Stream("die01_slc.bin", die=1, ce=1, mode=ri.MODE_SLC,
                  first_block=10, blocks=2, first_page=0, pages=4,
                  holes=holes)
    img = ri.RawImage(root=tmp_path, page_bytes=PAGE, streams=[s], maker=0xEC)
    img.write()
    return img


# ------------------------------------------------------------------ geometry --
def test_the_au_region_is_the_whole_page_spare_included(ri, geom):
    assert ri.REGION_BYTES == 18432
    assert geom.AU_STORED * geom.AUS_PER_PAGE_TLC == ri.REGION_BYTES
    # AU 3 starts before the data/spare boundary and ends after it
    assert 3 * geom.AU_STORED < 16384 < 4 * geom.AU_STORED


def test_a_capture_that_dropped_the_spare_is_refused(ri, tmp_path):
    with pytest.raises(ri.RawImageError) as exc:
        ri.RawImage(root=tmp_path, page_bytes=16384)
    assert "spare" in str(exc.value)


def test_a_data_offset_is_allowed_but_must_leave_room(ri, tmp_path):
    ri.RawImage(root=tmp_path, page_bytes=PAGE + 64, data_offset=64)
    with pytest.raises(ri.RawImageError):
        ri.RawImage(root=tmp_path, page_bytes=PAGE, data_offset=1)


def test_linear_addressing_is_block_major_page_minor(ri):
    s = ri.Stream("x.bin", die=0, mode=ri.MODE_TLC, first_block=10, blocks=3,
                  first_page=0, pages=1104)
    assert s.index(10, 0) == 0
    assert s.index(10, 1) == 1
    assert s.index(11, 0) == 1104
    assert s.index(12, 1103) == 3 * 1104 - 1
    assert s.npages() == 3 * 1104


def test_a_stream_defaults_to_its_modes_page_count(ri, geom):
    assert ri.Stream("a", die=0, mode=ri.MODE_SLC).pages == 368
    assert ri.Stream("a", die=0, mode=ri.MODE_TLC).pages == 1104
    assert geom.pages_per_block(True) == 368
    assert geom.pages_per_block(False) == 1104


def test_an_unknown_cell_mode_is_refused(ri):
    with pytest.raises(ri.RawImageError):
        ri.Stream("a", die=0, mode="mlc")


# ------------------------------------------------------------- manifest rules --
def test_two_streams_may_not_claim_one_address(ri, tmp_path):
    mk = lambda name: ri.Stream(name, die=0, ce=0, mode=ri.MODE_SLC,
                                first_block=0, blocks=1, pages=4)
    with pytest.raises(ri.RawImageError) as exc:
        ri.RawImage(root=tmp_path, streams=[mk("a.bin"), mk("b.bin")])
    assert "not one stream" in str(exc.value)


def test_the_same_address_at_two_retry_levels_is_fine(ri, tmp_path):
    a = ri.Stream("a.bin", die=0, ce=0, mode=ri.MODE_TLC, blocks=1, pages=4,
                  retry_level=0)
    b = ri.Stream("b.bin", die=0, ce=0, mode=ri.MODE_TLC, blocks=1, pages=4,
                  retry_level=6)
    img = ri.RawImage(root=tmp_path, streams=[a, b])
    assert img.retry_levels() == [0, 6]


def test_the_same_address_in_two_cell_modes_is_fine(ri, tmp_path):
    a = ri.Stream("a.bin", die=0, ce=0, mode=ri.MODE_SLC, blocks=1, pages=4)
    b = ri.Stream("b.bin", die=0, ce=0, mode=ri.MODE_TLC, blocks=1, pages=4)
    ri.RawImage(root=tmp_path, streams=[a, b])          # two physical reads


def test_a_die_cannot_be_two_ces_and_a_ce_cannot_be_two_dies(ri, tmp_path):
    with pytest.raises(ri.RawImageError):
        ri.RawImage(root=tmp_path, streams=[
            ri.Stream("a.bin", die=0, ce=0, mode=ri.MODE_SLC, pages=1),
            ri.Stream("b.bin", die=0, ce=1, mode=ri.MODE_TLC, pages=1)])
    with pytest.raises(ri.RawImageError):
        ri.RawImage(root=tmp_path, streams=[
            ri.Stream("a.bin", die=0, ce=3, mode=ri.MODE_SLC, pages=1),
            ri.Stream("b.bin", die=1, ce=3, mode=ri.MODE_TLC, pages=1)])


def test_a_short_file_is_refused_rather_than_padded(ri, tmp_path):
    (tmp_path / "short.bin").write_bytes(b"\x00" * PAGE)
    with pytest.raises(ri.RawImageError) as exc:
        ri.RawImage(root=tmp_path, streams=[
            ri.Stream("short.bin", die=0, ce=0, mode=ri.MODE_SLC, blocks=1,
                      pages=4)])
    assert "padded" in str(exc.value)


def test_a_manifest_round_trips(ri, image, tmp_path):
    again = ri.RawImage.load(tmp_path)
    assert again.page_bytes == image.page_bytes
    assert [s.to_json() for s in again.streams] == \
        [s.to_json() for s in image.streams]
    assert json.loads((tmp_path / ri.MANIFEST_NAME).read_text())["format"] \
        == ri.FORMAT_TAG


def test_a_directory_with_no_manifest_says_why(ri, tmp_path):
    with pytest.raises(ri.RawImageError) as exc:
        ri.RawImage.load(tmp_path / "nope")
    assert "not recoverable from" in str(exc.value)


# --------------------------------------------------------------- the refusals --
def test_a_present_page_reads_back_exactly(ri, image):
    dev = ri.RawImageDevice(image=image)
    assert dev.read_page(1, 10, 0, slc=True) == _page(10 * 8 + 0)
    assert dev.read_page(1, 11, 3, slc=True) == _page(11 * 8 + 3)
    assert dev.present_ces() == [1]


def test_a_hole_raises_and_is_not_erased_media(ri, image):
    dev = ri.RawImageDevice(image=image)
    with pytest.raises(ri.RawImageError) as exc:
        dev.read_page(1, 10, 2, slc=True)
    assert "HOLE" in str(exc.value)
    assert "erased" in str(exc.value)


def test_an_absent_page_raises_and_never_reads_as_ff(ri, image):
    dev = ri.RawImageDevice(image=image)
    with pytest.raises(ri.RawImageError):
        dev.read_page(1, 4095, 0, slc=True)
    with pytest.raises(ri.RawImageError):
        dev.read_page(2, 10, 0, slc=True)               # no such die


def test_the_wrong_cell_mode_raises_and_names_the_one_it_has(ri, image):
    dev = ri.RawImageDevice(image=image)
    with pytest.raises(ri.RawImageError) as exc:
        dev.read_page(1, 10, 0, slc=False)
    msg = str(exc.value)
    assert "DIFFERENT PHYSICAL PAGES" in msg and "slc=True" in msg


def test_writes_go_to_an_overlay_and_never_to_the_capture(ri, image, tmp_path):
    dev = ri.RawImageDevice(image=image)
    with pytest.raises(Exception):
        dev.write_page(1, 10, 0, b"\x00" * PAGE, slc=True)   # not enabled
    before = (tmp_path / "die01_slc.bin").read_bytes()
    dev.enable_writes(ri.CONFIRM)
    dev.write_page(1, 10, 0, b"\x5a" * PAGE, slc=True)
    assert dev.read_page(1, 10, 0, slc=True) == b"\x5a" * PAGE
    assert (tmp_path / "die01_slc.bin").read_bytes() == before


# ------------------------------------------------------------------- retries --
def test_retry_is_selection_among_captured_levels_only(ri, tmp_path):
    for name in ("l0.bin", "l6.bin"):
        (tmp_path / name).write_bytes(_page(1 if name == "l0.bin" else 2))
    img = ri.RawImage(root=tmp_path, streams=[
        ri.Stream("l0.bin", die=0, ce=0, mode=ri.MODE_TLC, blocks=1, pages=1,
                  retry_level=0),
        ri.Stream("l6.bin", die=0, ce=0, mode=ri.MODE_TLC, blocks=1, pages=1,
                  retry_level=6)])
    dev = ri.RawImageDevice(image=img)
    assert dev.supports_retry is True
    assert dev.retry_levels(slc=False) == [0, 6]
    assert dev.retry_levels(slc=True) == []
    # default serves the lowest level; read_page_ex says which
    assert dev.read_page_ex(0, 0, 0, slc=False).retry == 0
    dev.set_retry_level(6, slc=False)
    assert dev.read_page_ex(0, 0, 0, slc=False).retry == 6
    assert dev.read_page(0, 0, 0, slc=False) == _page(2)
    assert dev.restore_retry_levels() >= 1
    assert dev.read_page_ex(0, 0, 0, slc=False).retry == 0
    assert [lv for lv, _b in dev.retry_candidates(0, 0, 0, slc=False)] == [0, 6]
    with pytest.raises(ri.RawImageError) as exc:
        dev.set_retry_level(4, slc=False)
    assert "did not capture" in str(exc.value)


def test_a_single_level_capture_does_not_claim_retry_support(ri, image):
    assert ri.RawImageDevice(image=image).supports_retry is False


# -------------------------------------------------------------- vendor facts --
def test_the_slc_prefix_is_the_only_command_level_vendor_difference(ri):
    # NAND_TP_VENDOR_VARIANTS.md ss2: all five onfi_params setters are
    # byte-identical except a1[3], the pseudo-SLC prefix.
    assert set(ri.SLC_PREFIX_BY_MAKER.values()) == {0xDA, 0xA2}
    assert ri.SLC_PREFIX_BY_MAKER[0xEC] == 0xDA         # Samsung, this drive
    assert ri.SLC_PREFIX_BY_MAKER[0x2C] == 0xDA         # Micron
    assert ri.SLC_PREFIX_BY_MAKER[0x98] == 0xA2         # Toshiba
    assert ri.ADDR_CYCLES_BY_MAKER[0x2C] == 4           # Micron is the odd one
    assert set(ri.ADDR_CYCLES_BY_MAKER.values()) == {4, 5}


def test_a_skip_record_can_never_shadow_a_real_read(tmp_path):
    """What survived the removal of the trust table, and why.

    Ranks are gone: which of two READS of a page is right is settled by decoding
    them, not by where the file came from.  One ordering does survive, because
    no decode can supply it -- a record saying "nobody read this page" must
    never be served ahead of a record that IS a read of it.  That is the whole
    job of the record's `kind` byte.
    """
    store = pytest.importorskip("titania.store")
    assert store.KIND_READ < store.KIND_SKIP
    read = store.pack_record(1, 2, 3, True, store.KIND_READ, 0, 0, 0, 0, 0, 4608)
    skip = store.pack_record(1, 2, 3, True, store.KIND_SKIP, 0, 0, 0, 0, 0, 4608)
    assert read < skip

    # and the quarantine tier, which is the other thing content cannot check:
    # a page read perfectly and filed under the wrong address passes every
    # integrity test there is, because MCRC and the EE3 parity both cover the
    # stored bytes and neither covers the page number.
    dumpdev = pytest.importorskip("titania.dumpdev")
    assert dumpdev.SUSPECT in dumpdev.EXCLUDED_TIERS
    for layout, (tier, why) in dumpdev._LAYOUT_TIER.items():
        assert tier == dumpdev.SUSPECT
        assert len(why) > 40, layout
