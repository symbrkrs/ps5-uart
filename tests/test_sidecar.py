"""nandgui.sidecar -- derived data cached beside the stores, never an input.

The FTL derivation itself is not exercised here: it mounts the Bootstrap FTL,
which measured 47.8 s against the reference capture and needs real media.  What
IS tested is everything around it, because that is what decides whether a
missing file costs a second or costs the feature: which source wins, that a
cache hit never reaches for the FTL at all, and that a half-written sidecar can
never be left behind for the next run to read as authoritative.
"""
import json
import os
import struct
import sys

import pytest

sidecar = pytest.importorskip("nandgui.sidecar")

_HDR = 64
_REC = 24


def _index(path, records):
    """A minimal index.tix: 64-byte header then 24-byte records.

    Only the fields derive_cellmap reads are filled -- byte 0 the CE, bytes 1..2
    the block big-endian, byte 5 the flags whose bit 0 is pseudo-SLC.
    """
    os.makedirs(path, exist_ok=True)
    buf = bytearray(b"TITSTOR2" + b"\x00" * (_HDR - 8))
    for ce, block, slc in records:
        rec = bytearray(_REC)
        rec[0] = ce
        struct.pack_into(">H", rec, 1, block)
        rec[5] = 1 if slc else 0
        buf += rec
    with open(os.path.join(path, "index.tix"), "wb") as h:
        h.write(bytes(buf))
    return path


def test_cellmap_derives_from_the_index(tmp_path):
    store = _index(str(tmp_path / "raw.store"),
                   [(1, 7, True), (1, 8, False), (2, 7, False)])
    got, why = sidecar.derive_cellmap(store)
    assert why is None
    assert got == {1: {7: True, 8: False}, 2: {7: False}}


def test_cellmap_reduction_is_a_sticky_or(tmp_path):
    """titania.summary takes the mode "from any record of that block".

    Deriving it any other way lets this map and the summary's own slc column
    disagree about the same block, and a factor-of-three page count rides on it.
    """
    store = _index(str(tmp_path / "raw.store"),
                   [(3, 11, False), (3, 11, True), (3, 11, False)])
    got, why = sidecar.derive_cellmap(store)
    assert why is None
    assert got == {3: {11: True}}


def test_cellmap_writes_and_then_prefers_its_cache(tmp_path):
    store = _index(str(tmp_path / "raw.store"), [(1, 2, True), (1, 3, False)])
    cache = str(tmp_path / "cellmap.json")

    first, note = sidecar.cellmap(store, cache)
    assert first == {1: {2: True, 3: False}}
    assert "derived" in note and os.path.isfile(cache)

    # Corrupt the STORE, not the cache: a second call that still answers proves
    # the cached file was used and nothing was re-derived.
    open(os.path.join(store, "index.tix"), "wb").close()
    second, note2 = sidecar.cellmap(store, cache)
    assert second == first
    assert cache in note2


def test_a_truncated_index_is_reported_not_guessed(tmp_path):
    store = str(tmp_path / "raw.store")
    os.makedirs(store)
    with open(os.path.join(store, "index.tix"), "wb") as h:
        h.write(b"\x00" * (_HDR + 5))          # 5 bytes is not a whole record
    got, why = sidecar.derive_cellmap(store)
    assert got is None
    assert "record" in why


def test_unreadable_cache_falls_through_to_deriving(tmp_path):
    """A corrupt sidecar must not be an answer -- it must be replaced."""
    store = _index(str(tmp_path / "raw.store"), [(4, 1, True)])
    cache = str(tmp_path / "cellmap.json")
    with open(cache, "w", encoding="utf-8") as h:
        h.write("{ this is not json")
    got, note = sidecar.cellmap(store, cache)
    assert got == {4: {1: True}}
    assert "derived" in note
    assert json.load(open(cache, encoding="utf-8"))      # rewritten, and valid


def test_atomic_write_leaves_no_partial_file(tmp_path):
    target = str(tmp_path / "msodescriptor.bin")
    assert sidecar._atomic_write(target, b"\xaa" * 32) is None
    assert open(target, "rb").read() == b"\xaa" * 32
    assert not os.path.exists(target + ".part")
    assert [p.name for p in tmp_path.iterdir()] == ["msodescriptor.bin"]


def test_atomic_write_reports_failure_instead_of_raising(tmp_path):
    """A read-only or full data directory degrades; it does not crash the app."""
    note = sidecar._atomic_write(str(tmp_path / "no" / "such" / "dir" / "x.bin"),
                                 b"data")
    assert note and "Error" in note


def test_descriptor_cache_hit_never_touches_the_ftl(tmp_path, monkeypatch):
    """The whole point of the file: a launch that has it pays nothing.

    derive_descriptor is replaced with a landmine, so reaching it fails the test
    rather than quietly costing 48 s.
    """
    cache = tmp_path / "msodescriptor.bin"
    cache.write_bytes(b"\x01" * 16384)

    def _boom(_raw):
        raise AssertionError("derived despite a usable cache")

    monkeypatch.setattr(sidecar, "derive_descriptor", _boom)
    buf, note = sidecar.descriptor(str(tmp_path / "raw.store"), str(cache))
    assert buf == b"\x01" * 16384
    assert str(cache) in note


def test_descriptor_caches_what_it_derived(tmp_path, monkeypatch):
    cache = tmp_path / "msodescriptor.bin"
    monkeypatch.setattr(sidecar, "derive_descriptor",
                        lambda _raw: (b"\x7f" * 64, None))
    buf, note = sidecar.descriptor(str(tmp_path / "raw.store"), str(cache))
    assert buf == b"\x7f" * 64
    assert cache.read_bytes() == b"\x7f" * 64
    assert "derived" in note and "cached" in note


def test_descriptor_failure_explains_which_step_failed(tmp_path, monkeypatch):
    """"The FTL would not mount" and "this drive has no such section" are
    different facts, and the panel reporting UNKNOWN must not merge them."""
    monkeypatch.setattr(sidecar, "derive_descriptor",
                        lambda _raw: (None, "FTL mount failed: OSError: nope"))
    buf, note = sidecar.descriptor(str(tmp_path / "raw.store"),
                                   str(tmp_path / "d.bin"))
    assert buf is None
    assert "FTL mount failed" in note


# ------------------------------------------------------------------ summary --
def test_summary_cache_hit_never_rebuilds(tmp_path, monkeypatch):
    """A 64 s rebuild must not happen when the file is already there."""
    npz = tmp_path / "summary.npz"
    npz.write_bytes(b"not really an npz, but it exists")

    def _boom(*a, **k):
        raise AssertionError("rebuilt despite a usable cache")

    monkeypatch.setattr(sidecar, "build_summary", _boom)
    path, note = sidecar.summary(str(tmp_path / "raw.store"), None, str(npz))
    assert path == str(npz)
    assert str(npz) in note


def test_summary_reports_a_failed_build_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar, "build_summary",
                        lambda *a, **k: (None, "summary build failed: boom"))
    path, note = sidecar.summary(str(tmp_path / "raw.store"), None,
                                 str(tmp_path / "summary.npz"))
    assert path is None
    assert "boom" in note


def test_summary_temp_name_ends_in_npz(tmp_path, monkeypatch):
    """numpy APPENDS .npz to a name that lacks it.

    `out + ".part"` is therefore written as `out.part.npz`, and the rename then
    looks for a file that was never created -- which is exactly how the first
    version of this failed, after a successful 76 s build.  The regression test
    is that whatever build_summary hands to titania.summary already ends .npz.
    """
    seen = {}

    class _FakeSummary:
        @staticmethod
        def build(raw, decoded, out, log=None, on_bootstrap=None):
            seen["out"] = str(out)
            with open(str(out), "wb") as h:      # write exactly what we were given
                h.write(b"npz")
            return out

    monkeypatch.setattr(sidecar, "_ensure_titania", lambda: None)
    monkeypatch.setitem(sys.modules, "titania", type(sys)("titania"))
    monkeypatch.setitem(sys.modules, "titania.summary", _FakeSummary)
    sys.modules["titania"].summary = _FakeSummary

    out = str(tmp_path / "summary.npz")
    path, why = sidecar.build_summary(str(tmp_path / "raw.store"), None, out)

    assert why is None and path == out
    assert seen["out"].endswith(".npz"), seen["out"]
    assert seen["out"] != out, "must build through a temp, not in place"
    assert os.path.isfile(out)
    assert not os.path.exists(seen["out"]), "temp should have been renamed away"


def test_summary_build_failure_leaves_no_temp(tmp_path, monkeypatch):
    class _Boom:
        @staticmethod
        def build(raw, decoded, out, log=None, on_bootstrap=None):
            with open(str(out), "wb") as h:
                h.write(b"half")
            raise RuntimeError("interrupted")

    monkeypatch.setattr(sidecar, "_ensure_titania", lambda: None)
    monkeypatch.setitem(sys.modules, "titania", type(sys)("titania"))
    monkeypatch.setitem(sys.modules, "titania.summary", _Boom)
    sys.modules["titania"].summary = _Boom

    out = str(tmp_path / "summary.npz")
    path, why = sidecar.build_summary(str(tmp_path / "raw.store"), None, out)
    assert path is None and "interrupted" in why
    # the half-written file must not survive under EITHER name: a short npz
    # loads far enough to colour a drive map with.
    assert not os.path.exists(out)
    assert [p.name for p in tmp_path.iterdir()] == []


# ----------------------------------------------------------- progress -------
def test_phase_table_matches_the_builds_real_markers():
    """The friendly phase text is keyed on titania.summary's log lines.

    Those markers are a real coupling: reword one in the build and the viewer
    silently stops reporting that phase, leaving a status line frozen through
    the ~50 s Bootstrap mount -- the exact stretch the text exists for.
    """
    src = pytest.importorskip("titania.summary")
    import inspect
    body = inspect.getsource(src.build)
    for prefix, friendly in sidecar._PHASES:
        # The marker is logged two-space indented; the call is sometimes an
        # f-string, so match the marker text rather than the call syntax.
        assert ("  " + prefix) in body, (
            "no log line in titania.summary.build starts with %r" % prefix)
        assert friendly.strip()


def test_every_phase_names_how_long_it_takes():
    """A phase without a duration proves the program is alive and nothing more.

    "a few seconds" counts: what is being ruled out is a phase that says only
    what it is doing, leaving the reader with no idea whether to wait.
    """
    for _prefix, friendly in sidecar._PHASES:
        has_number = any(ch.isdigit() for ch in friendly)
        has_word = "second" in friendly or "minute" in friendly
        assert has_number or has_word, friendly


def test_wait_text_is_none_when_nothing_is_running():
    app = pytest.importorskip("nandgui.app")
    app._ui["sidecar_running"] = False
    assert app._sidecar_wait_text() is None


def test_wait_text_counts_up_independently_of_phase_messages():
    """The elapsed count must come from the clock, not from the last message.

    The Bootstrap mount logs nothing for ~50 s; a timer that only advanced when
    a phase arrived would sit still for most of the wait and read as a hang.
    """
    import time
    app = pytest.importorskip("nandgui.app")
    app._ui["sidecar_running"] = True
    app._ui["sidecar_phase"] = "mounting the Bootstrap FTL"
    app._ui["sidecar_started"] = time.time() - 42.0
    try:
        line = app._sidecar_wait_text()
        assert "42" in line, line
        assert "mounting the Bootstrap FTL" in line
    finally:
        app._ui["sidecar_running"] = False
