"""A store must report the CEs it holds even when its builder said nothing.

THE BUG THIS PINS DOWN, which was not cosmetic.  `DumpDevice.present_ces()`
answers from stored metadata and from nothing else, and callers begin every
sweep with `if ce not in present_ces(): continue`.  A builder that called
`finish(stats={...})` without a `ces` key therefore produced a store that read
back as holding NO CEs -- every consumer skipped the entire drive while the
records sat in the index.  It happened to a 5.4-million-record decode, and the
symptom was a viewer painting the whole drive as "no decoded record".

The fix is that `finish()` DERIVES the answer from the records it writes, so
there is no longer anything for a builder to pass or to forget.  These tests
build stores whose `stats` are deliberately empty or actively wrong.
"""
import json

import pytest

from titania import store as S
from titania.dumpdev import DumpDevice, EXCLUDED_TIERS

PAGE_BYTES = 18432


def _build(root, rows, stats):
    """rows = [(ce, tier)] -> a finished one-blob store."""
    w = S.StoreWriter(root, page_bytes=PAGE_BYTES)
    srcs = {t: w.source("src-" + t, t, "test")
            for t in {t for _ce, t in rows}}
    bid, off = w.append("b0.blob", bytes(PAGE_BYTES))
    for i, (ce, tier) in enumerate(rows):
        w.add(ce, 8, i, True, srcs[tier], "generic", bid, off, PAGE_BYTES)
    return w.finish(stats=stats)


ROWS = [(1, "unknown"), (6, "unknown"), (9, "suspect")]


@pytest.mark.parametrize("stats", [
    {},                                        # the builder said nothing
    {"strict": {}, "suspect": {}},             # ... or said it emptily
    {"strict": {"ces": []}},                   # ... or said something false
])
def test_ces_is_derived_whatever_the_builder_passed(tmp_path, stats):
    _build(tmp_path, ROWS, stats)
    with DumpDevice(tmp_path) as d:
        assert d.present_ces() == [1, 6]
    with DumpDevice(tmp_path, include_suspect=True) as d:
        assert d.present_ces() == [1, 6, 9]


def test_ces_by_tier_is_written_and_keyed_on_the_tier(tmp_path):
    """The recorded form carries no reader policy -- see `_ces_by_tier`."""
    _build(tmp_path, ROWS, {})
    meta = json.loads((tmp_path / S.META_NAME).read_text("utf-8"))
    assert meta["ces_by_tier"] == {"suspect": [9], "unknown": [1, 6]}
    # Not "strict"/"suspect": which tiers are excluded is dumpdev's policy, and
    # baking it in here is what would make one build serve only one mode.
    assert "strict" not in meta["ces_by_tier"]


def test_the_two_modes_really_differ(tmp_path):
    """Guards against a fix that just returns every CE regardless of tier."""
    _build(tmp_path, ROWS, {})
    st = S.PageStore(tmp_path, excluded_tiers=EXCLUDED_TIERS)
    strict = st.stat("ces")
    st.close()
    st = S.PageStore(tmp_path, excluded_tiers=())
    loose = st.stat("ces")
    st.close()
    assert strict == [1, 6] and loose == [1, 6, 9] and strict != loose


def test_a_store_without_ces_by_tier_still_reads(tmp_path):
    """Stores built before this existed keep working, from their own stats."""
    _build(tmp_path, ROWS, {"strict": {"ces": [1, 6]}, "suspect": {"ces": [1, 6, 9]}})
    meta_path = tmp_path / S.META_NAME
    meta = json.loads(meta_path.read_text("utf-8"))
    del meta["ces_by_tier"]                    # as an older build would leave it
    meta_path.write_text(json.dumps(meta, indent=1, sort_keys=True), "utf-8")
    with DumpDevice(tmp_path) as d:
        assert d.present_ces() == [1, 6]
    with DumpDevice(tmp_path, include_suspect=True) as d:
        assert d.present_ces() == [1, 6, 9]
