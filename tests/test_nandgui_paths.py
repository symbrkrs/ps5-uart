"""Where the viewer looks for its sidecars, and how wide a per-block table is.

Both bugs these cover were silent.  The sidecar one reported "no msoDescriptor
sidecar" for a file that was sitting right beside the stores the operator named
on the command line; the table-width one would have shifted every CE past 0 by
96 rows in the drive map and only crashed by luck.  Neither raises, neither is
visible in a smoke test, and both are one constant away from coming back.
"""
import importlib
import os
import pathlib

import pytest


def _fwd(p):
    """A path as the package stores it: forward slashes.

    as_posix() rather than a replace() of one separator with the other, because
    test_packaging walks every string constant in this suite and rejects one
    that resolves to a directory holding .bin files -- and a lone separator,
    either flavour, resolves to a filesystem root, which qualifies.  Doing the
    conversion with no separator literal at all sidesteps the whole question.
    """
    return pathlib.PurePath(p).as_posix()


@pytest.fixture
def gui(monkeypatch):
    """`nandgui` with its module-level paths restored afterwards.

    adopt_data_dir mutates package globals by design -- it exists to re-point
    them once the command line is parsed -- so a test that calls it has to put
    them back or it leaks into every test that runs after.
    """
    monkeypatch.delenv("TITANIA_NAND_DIR", raising=False)
    ng = importlib.import_module("nandgui")
    pf = importlib.import_module("nandgui.pagefmt")
    saved = (ng.DATA_DIR, ng.CELLMAP_JSON, ng.WERU_TABLE_JSON,
             pf.MSO_DESCRIPTOR_BIN, dict(pf._DESC))
    yield ng, pf
    (ng.DATA_DIR, ng.CELLMAP_JSON, ng.WERU_TABLE_JSON,
     pf.MSO_DESCRIPTOR_BIN) = saved[:4]
    pf._DESC.clear()
    pf._DESC.update(saved[4])


def test_adopt_data_dir_follows_the_named_store(gui, tmp_path):
    """--raw names a store; the sidecars live beside it, not in the cwd.

    This is the whole bug: --raw/--decoded/--summary overrode their own paths
    and nothing else, so running the viewer from the source checkout against a
    dump on another volume looked for msodescriptor.bin next to the SOURCE.
    """
    ng, pf = gui
    store = tmp_path / "titania_raw.store"
    store.mkdir()
    (tmp_path / "msodescriptor.bin").write_bytes(b"\x00" * 64)

    ng.adopt_data_dir(str(store))

    assert ng.DATA_DIR == _fwd(tmp_path)
    for path in (ng.CELLMAP_JSON, ng.WERU_TABLE_JSON, pf.MSO_DESCRIPTOR_BIN):
        assert os.path.dirname(path) == _fwd(tmp_path)
    # and the descriptor is actually reachable through the normal accessor
    assert pf.descriptor_bytes() == b"\x00" * 64


def test_adopt_data_dir_clears_the_cached_miss(gui, tmp_path):
    """A MISS is cached too, so re-pointing must drop the cache.

    Without the clear, any code that asked for the descriptor before the
    command line was parsed would pin None for the life of the process.
    """
    ng, pf = gui
    assert pf.descriptor_bytes() is None or True   # prime the cache either way
    store = tmp_path / "raw.store"
    store.mkdir()
    (tmp_path / "msodescriptor.bin").write_bytes(b"\xab" * 32)
    ng.adopt_data_dir(str(store))
    assert pf.descriptor_bytes() == b"\xab" * 32


def test_explicit_env_pin_still_wins(gui, tmp_path, monkeypatch):
    """$TITANIA_NAND_DIR is the operator saying it out loud; it outranks a hint."""
    ng, _pf = gui
    monkeypatch.setenv("TITANIA_NAND_DIR", str(tmp_path / "pinned"))
    before = ng.DATA_DIR
    store = tmp_path / "raw.store"
    store.mkdir()
    assert ng.adopt_data_dir(str(store)) == before
    assert ng.DATA_DIR == before


def test_bad_hints_are_skipped_not_fatal(gui, tmp_path):
    """Never raises: a wrong --raw must fail where it is USED, naming the path."""
    ng, _pf = gui
    store = tmp_path / "raw.store"
    store.mkdir()
    ng.adopt_data_dir("", None, str(tmp_path / "nope" / "gone.store"), str(store))
    assert ng.DATA_DIR == _fwd(tmp_path)


def test_sidecar_loaders_resolve_at_call_time(gui, tmp_path):
    """`def f(path=CELLMAP_JSON)` binds at IMPORT, which is before the parse.

    That default is what made re-pointing the package constant have no effect
    on the loaders, so the fix is only real if the loaders read it live.
    """
    ng, _pf = gui
    import nandgui.model as model
    store = tmp_path / "raw.store"
    store.mkdir()
    (tmp_path / "cellmap.json").write_text('{"1": {"7": "slc"}}', encoding="utf-8")
    ng.adopt_data_dir(str(store))
    assert model.load_cellmap() == {1: {7: True}}


def test_per_block_table_is_4000_wide_not_4096():
    """summary.py must size itself off the CAPACITY, not the 4096 bound.

    geometry says it plainly: BLOCKS_PER_LUN is a bound to be used "never as a
    capacity".  The map, the viewer and summary.py all index ce * 4000 + block,
    so a 49152-row summary shifts every CE past 0 by 96 rows -- and renders,
    quietly showing the wrong block everywhere except CE 0.
    """
    from titania import geometry as geom
    from titania import summary

    assert summary.BLOCKS == geom.BLOCKS_PER_DIE == 4000
    assert summary.NCELL == geom.BLOCKS_TOTAL == 48000
    assert summary.NCELL == summary.CE_COUNT * summary.BLOCKS


# ------------------------------------------------------- resolve_paths ------
def _store(root, name, page_bytes):
    d = root / name
    d.mkdir()
    (d / "store.json").write_text('{"page_bytes": %d}' % page_bytes,
                                  encoding="utf-8")
    return d


def _dump_dir(tmp_path):
    root = tmp_path / "dump"
    root.mkdir()
    _store(root, "titania_raw.store", 18432)
    _store(root, "titania_decoded.store", 16416)
    (root / "summary.npz").write_bytes(b"npz")
    (root / "msodescriptor.bin").write_bytes(b"\x00" * 16)
    return root


def test_naming_only_raw_places_everything_else(gui, tmp_path):
    """The bug: an import-time default is resolved against the CWD.

    `--raw /elsewhere/raw.store` with no --summary left the summary pointing
    into the source checkout, where it was not found -- and where the rebuild
    would then have written a fresh 64 s summary.npz.
    """
    ng, pf = gui
    root = _dump_dir(tmp_path)
    raw, decoded, summary = ng.resolve_paths(
        raw=str(root / "titania_raw.store"))

    assert raw == str(root / "titania_raw.store")
    assert _fwd(decoded) == _fwd(root / "titania_decoded.store")
    assert _fwd(summary) == _fwd(root / "summary.npz")
    assert _fwd(pf.MSO_DESCRIPTOR_BIN) == _fwd(root / "msodescriptor.bin")


def test_naming_only_the_summary_places_the_stores(gui, tmp_path):
    ng, _pf = gui
    root = _dump_dir(tmp_path)
    raw, decoded, summary = ng.resolve_paths(summary=str(root / "summary.npz"))
    assert _fwd(raw) == _fwd(root / "titania_raw.store")
    assert _fwd(decoded) == _fwd(root / "titania_decoded.store")
    assert summary == str(root / "summary.npz")


def test_an_explicit_path_is_never_overridden(gui, tmp_path):
    ng, _pf = gui
    root = _dump_dir(tmp_path)
    other = tmp_path / "somewhere_else.npz"
    other.write_bytes(b"npz")
    _raw, _dec, summary = ng.resolve_paths(
        raw=str(root / "titania_raw.store"), summary=str(other))
    assert summary == str(other)


def test_candidates_are_rescanned_in_the_adopted_directory(gui, tmp_path):
    """_find_store's other job is filling STORE_CANDIDATES, which the startup
    banner prints from.  Skipping it for a named store left that list holding
    the import-time scan of the working directory, so the viewer announced
    other stores "here" that were in the source checkout."""
    ng, _pf = gui
    root = _dump_dir(tmp_path)
    _store(root, "older_decode.store", 16416)
    ng.resolve_paths(raw=str(root / "titania_raw.store"),
                     decoded=str(root / "titania_decoded.store"))
    found = [_fwd(p) for p in ng.STORE_CANDIDATES["decoded"]]
    assert _fwd(root / "older_decode.store") in found
    assert all(_fwd(root) in p for p in found), found
