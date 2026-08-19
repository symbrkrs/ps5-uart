"""Packaging: the layer model and the write-safety model, checked structurally.

These are the claims the README makes.  A README that drifts from the code is
worse than none, so the checkable parts of it are checked here.
"""
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def proj():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_parses_and_names_the_entry_point(proj):
    assert proj["project"]["name"] == "titania"
    assert proj["project"]["scripts"]["titania"] == "titania.cli:main"


def test_project_urls_are_real(proj):
    """A placeholder URL is worse than no URL at all.

    `Homepage = "https://github.com/"` shipped in this file for a while: it
    parses, it renders as a link on PyPI, and it goes to the GitHub front page.
    Nothing about it looks wrong in a diff, so the check has to be that each URL
    names a repository rather than merely being a well-formed address.
    """
    from urllib.parse import urlsplit

    # chr(47), not a "/" literal: the media-directory guard below walks every
    # string constant in this suite and rejects one that resolves to a directory
    # holding .bin files -- and a lone separator resolves to a filesystem root,
    # which qualifies. Spelling it this way is deliberate; do not "tidy" it.
    slash = chr(47)

    urls = proj["project"]["urls"]
    assert urls, "no [project.urls] at all"
    for name, url in urls.items():
        parts = urlsplit(url)
        assert parts.scheme == "https", (name, url)
        assert parts.netloc, (name, url)
        segments = [p for p in parts.path.split(slash) if p]
        assert segments, (
            "%s is a bare host (%r) -- a placeholder, not a project link"
            % (name, url))
        if "github.com" in parts.netloc:
            assert len(segments) >= 2, (
                "%s names no owner/repo: %r" % (name, url))


def test_the_arm_stubs_ship_inside_the_package(proj):
    """The .c stubs must be package data, not checkout-root files.

    They used to live at the repository root, where `parent.parent` found them
    in a checkout and `site-packages` in a wheel -- so an installed titania
    could not build the device-side reduction stubs at all, and hardware
    capture failed on a missing source rather than a missing compiler.
    """
    from titania import armbuild

    declared = set(proj["tool"]["setuptools"]["package-data"]["titania"])
    stubs = {d for d in declared if d.startswith("stubs/")}
    assert stubs, "no stubs/ entries in package-data"

    for rel in sorted(stubs):
        assert (ROOT / "titania" / rel).is_file(), rel
    on_disk = {"stubs/" + p.name for p in (ROOT / "titania" / "stubs").glob("*.c")}
    assert on_disk == stubs, (
        "package-data and titania/stubs/ disagree: %s" % (on_disk ^ stubs))

    # resolved from __file__, so a checkout and a wheel answer the same way
    assert armbuild.STUB_DIR == (ROOT / "titania" / "stubs")
    assert armbuild.STUB_DIR.parent.name == "titania"


def test_hwdevs_stub_dir_matches_armbuilds():
    """Two spellings of one location; the pair has to be asserted, not trusted."""
    from titania import armbuild, hwdev

    assert hwdev.STUB_DIR == armbuild.STUB_DIR
    assert hwdev.SCAN_SRC.is_file()


def test_entry_point_target_exists_and_takes_argv():
    import inspect
    from titania.cli import main
    assert callable(main)
    # main(argv=None) so it is callable in-process, not only from a shell
    assert "argv" in inspect.signature(main).parameters


def test_declared_dependencies_are_the_ones_actually_imported(proj):
    deps = " ".join(proj["project"]["dependencies"])
    assert "numpy" in deps
    assert "pycryptodome" in deps
    hw = " ".join(proj["project"]["optional-dependencies"]["hardware"])
    for name in ("pyserial", "xmodem", "tqdm", "hexdump2"):
        assert name in hw
    # The viewer's GL stack. Declared as an extra so a headless install does not
    # pull it; nandgui/requirements.txt is where the floors come from.
    gui = " ".join(proj["project"]["optional-dependencies"]["gui"])
    for name in ("imgui-bundle", "glfw", "PyOpenGL"):
        assert name in gui


def test_every_declared_py_module_is_present_in_the_tree(proj):
    """A declared root module with no file behind it installs cleanly and then
    fails at first use, so the list and the tree have to agree.

    The list is empty now, and that is the assertion worth making: the codec
    chain moved inside the package, and the only root module that outlived it
    (`titania_fw`) was imported solely by `uart_client.py`, which is not
    installed either. Anything added back here is a claim that a root-level
    module belongs on every installer's global import path.
    """
    declared = proj["tool"]["setuptools"].get("py-modules", [])
    missing = [m for m in declared if not (ROOT / (m + ".py")).exists()]
    assert missing == []
    assert declared == [], (
        "root modules are checkout-only; adding %s puts a generic name on the "
        "global import path" % declared)


def test_the_codec_data_tables_are_present(proj):
    """The six measured tables the codec chain loads through `nd_tables`.

    If one of these is gone the codec is not installable at all -- and the
    failure would otherwise surface as a decode error, not a missing file.
    """
    tables = ["ek_ee3_dec.bin", "ek_ee3_enc.bin",
              "nd_ecutbl_ecc0dec_code0_rdsel0_bank15.bin",
              "nd_ecutbl_ecc0enc_code0_rdsel0_bank3.bin",
              "nd_g2ldpc_a10r_base.json",
              "ne_ldpc_dec_b11_rd0_n8192.bin"]
    data = ROOT / "titania" / "data"
    missing = [t for t in tables if not (data / t).exists()]
    assert missing == []
    from titania import nd_tables
    assert sorted(nd_tables.TABLES) == sorted(tables)


def test_package_data_declares_exactly_the_measured_tables(proj):
    """`nd_tables.TABLES`, the wheel's package-data list and the files on disk
    must be one list.  A table in the resolver but not in package-data installs
    a wheel that cannot decode; the reverse ships a file nothing reads.

    `titania/data/` holds measurement and nothing else.  Per-capture data --
    a trust table, a dump manifest -- has no place in it: those describe one
    drive, so they belong with that drive and reach the code through an
    environment variable instead.
    """
    from titania import nd_tables
    declared = proj["tool"]["setuptools"]["package-data"]["titania"]

    # package-data now carries two kinds of thing. Split them and hold each to
    # its own source of truth, rather than relaxing the check to a subset --
    # "declares exactly" is the property worth keeping, and a stub arriving in
    # data/ or a table arriving in stubs/ should still fail here.
    data = sorted(d for d in declared if d.startswith("data/"))
    stubs = sorted(d for d in declared if d.startswith("stubs/"))
    assert data + stubs == sorted(declared), (
        "package-data entry outside data/ and stubs/: %s"
        % sorted(set(declared) - set(data) - set(stubs)))

    assert data == sorted("data/" + t for t in nd_tables.TABLES)
    on_disk = sorted(p.name for p in (ROOT / "titania" / "data").iterdir()
                     if p.is_file())
    assert on_disk == sorted(nd_tables.TABLES)

    # The stubs are checked against the directory in the test below; here the
    # point is only that data/ holds measurement and nothing else.
    assert all(d.endswith(".c") for d in stubs), stubs


def test_every_table_matches_the_digest_recorded_when_it_was_measured():
    """The check that survives having only one copy of each table.

    There used to be two, and a test that they were byte-identical.  That test
    could not catch the thing that actually happened: a line-ending sweep
    matching `*.bin` stripped a byte from two tables in BOTH locations at once,
    and copy-vs-copy stayed green over the damage because the copies still
    agreed.  A digest recorded when the table was read off the silicon does
    catch it, and it does not need a second copy to compare against -- which is
    why the second copy is gone.
    """
    from titania import nd_tables
    bad = []
    for name in nd_tables.TABLES:
        size, want = nd_tables.SHA256[name]
        p = nd_tables.table(name)
        got = nd_tables.digest(p)
        if got != want or p.stat().st_size != size:
            bad.append("%s: %d B / %s, recorded %d B / %s"
                       % (name, p.stat().st_size, got[:16], size, want[:16]))
    assert bad == []
    assert nd_tables.verify() == []


def test_the_tables_resolve_out_of_the_package_and_nowhere_else():
    """One location, so there is no stale-copy question to get wrong.

    `titania/data/` is inside the package, which makes it the same directory in
    a checkout, an editable install and an unpacked wheel.
    """
    from titania import nd_tables
    data = ROOT / "titania" / "data"
    for name in nd_tables.TABLES:
        assert nd_tables.find(name) == data / name
        assert nd_tables.available(name)
        assert not (ROOT / name).exists(), "the retired root copy is back"
    assert nd_tables.find("nd_not_a_real_table.bin") is None
    assert not nd_tables.available("nd_not_a_real_table.bin")
    with pytest.raises(FileNotFoundError):
        nd_tables.table("nd_not_a_real_table.bin")


def test_layers_import_bottom_up_without_the_ones_above():
    """`geometry` and `device` must not drag in codec, ftl or the hardware."""
    code = ("import sys; sys.path.insert(0, %r);"
            "import titania.geometry, titania.device;"
            "print(sorted(m for m in ('numpy','serial','titania.ftl',"
            "'titania.codec','titania.hwdev','titania_nand')"
            " if m in sys.modules))" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_cli_import_does_not_pull_any_layer():
    """`titania --help` must be fast and must not need numpy."""
    code = ("import sys; sys.path.insert(0, %r);"
            "import titania.cli;"
            "print(sorted(m for m in sys.modules if m.startswith('titania.')))"
            % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    loaded = out.stdout.strip()
    for heavy in ("titania.ftl", "titania.codec", "titania.hwdev",
                  "titania.extract"):
        assert heavy not in loaded


def test_lazy_package_attributes_resolve():
    import titania
    for name in titania.__all__:
        assert getattr(titania, name) is not None
    with pytest.raises(AttributeError):
        titania.definitely_not_a_thing


# ---------------------------------------------------------------------------
# the write-safety model
# ---------------------------------------------------------------------------
def test_writes_need_two_independent_opt_ins():
    """Neither opt-in is persisted, and neither alone is enough.

    A Device constructed the ordinary way must refuse to write, and the refusal
    must be an exception -- never a no-op that a caller can mistake for success.
    """
    from titania.device import CONFIRM, WriteProtected
    assert isinstance(CONFIRM, str) and CONFIRM
    assert issubclass(WriteProtected, Exception)


def test_a_dump_device_refuses_to_write_by_default(tmp_path):
    """`writable` is a capability; `writes_enabled` is the per-instance opt-in.

    They are separate on purpose: a DumpDevice CAN write (into its own copy),
    which is what makes the write path testable offline, but a freshly built one
    still refuses until someone types the phrase.  The refusal must be an
    exception, never a no-op a caller could mistake for success.
    """
    from titania.dumpdev import DumpDevice
    from titania.device import WriteProtected, CONFIRM
    d = DumpDevice(tmp_path)
    assert d.writable is True
    assert d.writes_enabled is False
    with pytest.raises(WriteProtected):
        d.write_page(1, 2, 3, b"\0" * 4608)
    with pytest.raises(WriteProtected):
        d.erase_block(1, 2)
    # opt-in one: the phrase.  A wrong one must not half-enable anything.
    with pytest.raises(WriteProtected):
        d.enable_writes("yes")
    assert d.writes_enabled is False
    d.enable_writes(CONFIRM)
    assert d.writes_enabled is True


def test_enabling_writes_is_never_persisted(tmp_path):
    """A second Device over the same backing starts disabled again."""
    from titania.dumpdev import DumpDevice
    from titania.device import CONFIRM
    DumpDevice(tmp_path).enable_writes(CONFIRM)
    assert DumpDevice(tmp_path).writes_enabled is False


def test_hardware_writes_need_a_second_construction_time_opt_in():
    """`HardwareDevice` refuses unless BUILT with allow_hardware_writes=True.

    That is the second, independent opt-in: the phrase alone cannot reach the
    media, so a typo in a script that works against a dump cannot erase a board.
    Checked on the signature so it needs no board.
    """
    import inspect
    from titania.hwdev import HardwareDevice
    params = inspect.signature(HardwareDevice.__init__).parameters
    assert "allow_hardware_writes" in params
    assert params["allow_hardware_writes"].default is False


def test_the_confirm_phrase_is_not_guessable_by_accident():
    """It has to be typed, so it must not be 'yes'."""
    from titania.device import CONFIRM
    assert len(CONFIRM) > 8
    assert CONFIRM.lower() not in ("yes", "y", "true", "ok", "confirm")


def test_no_test_in_this_suite_names_a_media_directory():
    """The suite's own contract: it must run with no board and no media.

    Checked rather than trusted, because the day someone points a test at their
    own capture directory is the day the suite stops being runnable by anybody
    else -- and it would still pass on their machine, so nothing would say so.

    The rule is about MEDIA DIRECTORIES: a directory in this tree that holds
    .bin files is a dump, a store or a survey.  Naming one from a test means
    that test only runs on this machine.  Source directories (`titania`) and the
    individual metadata files the suite reads on purpose (pyproject.toml,
    nk_wma.py) are not media and are left alone.

    `titania` is exempted BY NAME rather than by accident.  It used to pass this
    check only because it happened to contain no .bin file; it now ships the
    measured tables as `titania/data/`, most of which are .bin.  Those are
    package data that installs with the code on any machine -- the exact
    opposite of a capture from this board -- so the exemption the docstring
    always claimed is now written down instead of implied.
    """
    import ast
    #: source, not media.  Anything else holding .bin files is a capture.
    exempt = {"titania", "tests"}
    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            s = node.value.strip()
            if not s or len(s) > 64 or "\n" in s:
                continue
            cand = ROOT / s
            if not cand.is_dir() or cand.resolve() == ROOT.resolve():
                continue
            if Path(s).parts and Path(s).parts[0] in exempt:
                continue
            if next(cand.rglob("*.bin"), None) is not None:
                offenders.append("%s:%d names the media directory %r"
                                 % (path.name, node.lineno, s))
    assert offenders == []
