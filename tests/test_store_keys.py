"""`titania.store` record packing.

The store is a flat sorted index over a few hundred thousand pages, so the key's
BYTE order has to equal its TUPLE order -- a binary search that sorts differently
from the caller finds the wrong page and reports it confidently.  That property
is checkable in memory with no store on disk.
"""
import random
import struct

import pytest


def test_key_round_trip(store):
    rnd = random.Random(6)
    for _ in range(2000):
        ce = rnd.randrange(12)
        block = rnd.randrange(4096)
        page = rnd.randrange(1104)
        slc = bool(rnd.getrandbits(1))
        assert store.unpack_key(store.pack_key(ce, block, page, slc)) == \
            (ce, block, page, slc)


def test_key_byte_order_equals_tuple_order(store):
    """The comment says "byte order == tuple order, deliberately".  Prove it."""
    rnd = random.Random(7)
    keys = []
    for _ in range(400):
        t = (rnd.randrange(12), rnd.randrange(4096), rnd.randrange(1104),
             bool(rnd.getrandbits(1)))
        keys.append((store.pack_key(*t), t))
    by_bytes = [t for _k, t in sorted(keys, key=lambda kv: kv[0])]
    by_tuple = sorted(t for _k, t in keys)
    assert by_bytes == by_tuple


def test_key_is_big_endian_fixed_width(store):
    k = store.pack_key(1, 2, 3, True)
    assert k == struct.pack(">BHHB", 1, 2, 3, 1)
    assert len(k) == store.KEY_BYTES == 6


def test_slc_flag_is_part_of_the_key(store):
    """The same (ce, block, page) read in the two cell modes is two DIFFERENT
    pages, and conflating them is how a native block read in pseudo-SLC gets
    filed as if it were the real thing."""
    assert store.pack_key(1, 2, 3, True) != store.pack_key(1, 2, 3, False)


def test_a_read_sorts_before_a_skip_for_the_same_key(store):
    """The only ordering the record format asserts.

    Both records claim the page; one is a read of it and the other is a note
    saying nobody read it.  `served()` returns the first, so the read has to be
    first, or the store answers "not read" for a page it holds.
    """
    read = store.pack_record(1, 2, 3, True, store.KIND_READ,
                             0, 0, 0, 0, 0, 4608)
    skip = store.pack_record(1, 2, 3, True, store.KIND_SKIP,
                             0, 0, 0, 0, 0, 4608)
    assert read < skip


def test_pack_record_takes_only_the_two_kinds(store):
    """Byte 6 is a kind, not a score.  A rank passed to it must not be accepted
    as though the old 0..100 scale still meant something."""
    for bad in (1, 50, 90, 254, -1, 256):
        with pytest.raises(store.StoreError):
            store.pack_record(1, 2, 3, True, bad, 0, 0, 0, 0, 0, 4608)


def test_pack_record_rejects_an_offset_past_the_blob_ceiling(store):
    """A silently truncated offset is an unfindable bug, so it is checked."""
    with pytest.raises(store.StoreError) as e:
        store.pack_record(1, 2, 3, True, store.KIND_READ, 0, 0, 0, 0,
                          store.MAX_BLOB + 1, 4608)
    assert "split the blob" in str(e.value)
    with pytest.raises(store.StoreError):
        store.pack_record(1, 2, 3, True, store.KIND_READ, 0, 0, 0, 0, -1, 4608)


def test_blob_ceiling_is_far_above_the_largest_real_blob(store):
    """A whole (ce, block) at native TLC is 1104 * 18432 bytes."""
    biggest = 1104 * 18432
    assert store.MAX_BLOB > 100 * biggest


def test_record_starts_with_the_key(store):
    """A record must be searchable by the same 6 bytes `pack_key` produces, or
    the index and the lookup disagree."""
    rec = store.pack_record(4, 300, 77, False, store.KIND_READ,
                            0, 0, 0, 0, 0, 4608)
    assert rec[:store.KEY_BYTES] == store.pack_key(4, 300, 77, False)


def test_source_rec_carries_the_reason_and_no_rank(store):
    """Provenance is a sentence, not a score.

    The reason has to survive into the store, because "rescued by the ladder at
    level 3" is not reconstructible later.  A rank must NOT: which of two reads
    is right is settled by decoding them, and a number here would only be a
    second, worse answer to that question.
    """
    s = store.SourceRec(1, "a-loose-dump", "unknown",
                        "provenance never measured")
    assert s.as_json() == ["a-loose-dump", "unknown",
                           "provenance never measured"]
    assert "a-loose-dump" in repr(s)
    assert not hasattr(s, "rank")
