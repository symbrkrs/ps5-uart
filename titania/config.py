"""Where this package looks for a dump, and how to point it somewhere else.

Nothing in `titania` names a particular machine, drive letter or capture. Every
default that touches the filesystem is decided here, in one module, so that a
release carries a *policy* for finding data rather than one operator's paths.

    TITANIA_DUMP=/media/ps5/mydump  py -m titania info
    py -m titania info --device dump:/media/ps5/mystore

Resolution order, most explicit first:

    1. an explicit argument      `DumpDevice(root)` / `--device dump:PATH`
    2. $TITANIA_DUMP             a dump tree or a page store
    3. discovery                 a `dump/` or `nand/` directory, or any page
                                 store, in the cwd or beside the checkout

Why discovery is last and narrow. It exists so that `py -m titania info` in a
directory that obviously holds one dump does the obvious thing. It
does not go hunting: it looks in two places, for a short list of names, and if
two candidates match it raises and names them both rather than picking. A tool
that silently reads a different capture than the operator meant is worse than a
tool that asks, and this project has had that bug before -- see the
`source_tree` check in `dumpdev.canonical_root`, which exists because a store
once served one dump's pages under another dump's name.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ENV_DUMP",
    "ENV_PREFER_STORE",
    "DUMP_DIR_NAMES",
    "search_roots",
    "default_root",
    "find_dumps",
    "prefer_store_enabled",
    "NoDumpFound",
    "AmbiguousDump",
]

#: A dump tree or a page store. The single variable an operator has to set.
ENV_DUMP = "TITANIA_DUMP"

#: `TITANIA_PREFER_STORE=0` stops a whole-tree open from being redirected to the
#: page store built from it. For A/B comparing the two backings from a shell;
#: not a setting anything is expected to run under.
ENV_PREFER_STORE = "TITANIA_PREFER_STORE"

#: Directory names discovery will accept for a dump tree.  Short
#: and generic: a page store is recognised by its contents (`store.is_store`)
#: and needs no name, so this list only has to cover a loose tree of page files,
#: which is the older and rarer shape.  The reference capture's own directory
#: names are not in here -- naming them would be this package asserting that one
#: operator's layout is part of the format.
DUMP_DIR_NAMES = ("dump", "nand")

#: The checkout root: the directory the package sits in.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent


class NoDumpFound(RuntimeError):
    """No dump could be located, and the message says how to name one."""


class AmbiguousDump(RuntimeError):
    """Discovery found more than one candidate and will not choose."""


def search_roots():
    """Directories discovery looks in, in order.  -> [Path]

    The cwd first: an operator standing in a directory of dumps means that one.
    The checkout second, so a source tree with a dump beside it keeps working
    from anywhere.
    """
    roots = []
    try:
        cwd = Path.cwd()
    except OSError:                              # deleted cwd; not our problem
        cwd = None
    for p in (cwd, CHECKOUT_ROOT):
        if p is not None and p not in roots:
            roots.append(p)
    return roots


def _is_store(path):
    """`store.is_store` without importing store at module import time."""
    try:
        from . import store as _store
    except (ImportError, ValueError):                    # direct execution
        import store as _store
    try:
        return _store.is_store(path)
    except OSError:
        return False


def find_dumps(roots=None):
    """Every dump tree or page store discovery can see.  -> [Path]

    A candidate is a directory that either carries one of `DUMP_DIR_NAMES` or
    is a page store (`store.is_store`). Ordered by `search_roots()` then by
    name, and de-duplicated by resolved path.

    A tree and the store built from it count once, not twice. Both are normally
    on disk side by side, they hold the same pages, and `dumpdev.canonical_root`
    redirects the tree to the store anyway -- so reporting them as two dumps
    would make the common layout look ambiguous and refuse to pick, which is the
    opposite of what the ambiguity check is for. The store is dropped and the
    tree kept, because opening the tree reaches the store while the reverse is
    not true.
    """
    found, seen = [], set()
    for root in (roots if roots is not None else search_roots()):
        try:
            entries = sorted(p for p in Path(root).iterdir() if p.is_dir())
        except OSError:
            continue
        for cand in entries:
            key = str(cand.resolve()).lower()
            if key in seen:
                continue
            if cand.name in DUMP_DIR_NAMES or _is_store(cand):
                seen.add(key)
                found.append(cand)

    names = {p.name for p in found}
    derived = set()
    for cand in found:
        src = _source_tree_name(cand)
        if src and src in names:
            derived.add(cand.name)
    return [p for p in found if p.name not in derived] or found


def _source_tree_name(path):
    """The tree a store says it was built from, or None.  Cheap and tolerant."""
    try:
        from . import dumpdev as _dd
    except (ImportError, ValueError):                    # direct execution
        try:
            import dumpdev as _dd                        # noqa: F401
        except ImportError:
            return None
    try:
        return _dd.source_tree_name(path)
    except Exception:
        return None


def default_root(required=True):
    """The dump to use when the caller named none.  -> Path or None

    Raises `NoDumpFound` / `AmbiguousDump` when `required`, because the
    alternative -- returning a path that does not exist -- surfaces later as a
    confusing "0 pages" rather than as the missing configuration it is.
    """
    env = os.environ.get(ENV_DUMP)
    if env:
        # Not checked for existence: an explicit setting that is wrong should
        # fail where it is used, naming the path the operator actually gave.
        return Path(env).expanduser()

    cands = find_dumps()
    if len(cands) == 1:
        return cands[0]
    if not cands:
        if not required:
            return None
        raise NoDumpFound(
            "no dump found. Set %s to a dump tree or page store, or pass one:\n"
            "    %s=/path/to/dump py -m titania info\n"
            "    py -m titania info --device dump:/path/to/store\n"
            "Discovery looked for %s (or any page store) in: %s"
            % (ENV_DUMP, ENV_DUMP, "/".join(DUMP_DIR_NAMES),
               ", ".join(str(r) for r in search_roots())))
    if not required:
        return None
    raise AmbiguousDump(
        "%d dumps are visible and none was named, so none is assumed:\n%s\n"
        "Choose one with %s=<path> or --device dump:<path>."
        % (len(cands), "\n".join("    " + str(c) for c in cands), ENV_DUMP))


def dump_path(*parts, **kw):
    """A path inside the dump, or None when there is no dump.  -> Path or None

        IMG = config.dump_path("img")       # <dump>/img, or None
        if IMG and IMG.exists(): ...

    Returning None is the point. Dozens of self-tests in this package are
    scaffolding over the reference capture and are written to skip when the
    media is absent -- which is the normal state of a fresh checkout, and the
    reason `pytest` can run with no dump at all. They used to spell the dump's
    name themselves (`HERE / "<some-capture>" / "img"`), which both hard-coded one
    capture's directory name and, worse, produced a path that merely did not
    exist, so the skip message blamed a missing subdirectory rather than a
    missing dump. Pass `required=True` where a caller genuinely cannot proceed.

    It resolves against the tree, not the store, when the two are both on disk.
    Callers of this function want a file -- a page, a CSV, a cache directory --
    and a page store has none of those: it is blobs and an index. So when
    discovery lands on a store that records which tree it was built from, and
    that tree is still beside it, the tree answers. `DumpDevice` goes the other
    way for the same reason (`canonical_root`): it wants pages, and the store is
    the whole of them.
    """
    root = default_root(required=kw.get("required", False))
    if root is None:
        return None
    if parts:
        src = _source_tree_name(root)
        if src:
            tree = root.parent / src
            if tree.is_dir():
                root = tree
    return root.joinpath(*parts) if parts else root


def prefer_store_enabled():
    """Whether a whole-tree open may redirect to the store built from it."""
    v = os.environ.get(ENV_PREFER_STORE)
    if v is None:
        return True
    return v.strip().lower() not in ("0", "no", "off", "false")


#: Decoded group-1 firmware image, used as ground truth by two self-tests
#: (`nd_descrambler`, `nd_deepparity`): they descramble the media and compare
#: against it byte for byte.  It lives in the checkout as `fw_0x12.bin`.  Both
#: sites used to name a copy of it by absolute path on one Windows machine --
#: verified byte-identical to this one, so nothing is lost by pointing at the
#: copy that travels with the code.  Both tests skip when it is absent, which is
#: correct: a missing reference is a missing test, not a failure.
ENV_FW_REF = "TITANIA_FW_REF"
FW_REF_NAME = "fw_0x12.bin"

#: Captured EFC DRAM image, base 0x40000000.  `pr_setfeat.verify_baked()` reparses
#: the firmware's own read-retry tables out of it and asserts they are
#: byte-identical to the ones baked into that module.  The baked TABLES are the
#: Product and are always present; this image is only the evidence, so its
#: absence disables one check rather than breaking the ladder.
ENV_DDR_IMAGE = "TITANIA_DDR_IMAGE"
DDR_IMAGE_NAME = "ddr_40000000.bin"

#: Decrypted EFC SRAM firmware, used as known plaintext by `nd_g2extract`'s
#: self-test.  Same standing: evidence for a check, not an input to the codec.
ENV_EFC_SRAM = "TITANIA_EFC_SRAM"
EFC_SRAM_NAME = "dumps/fw.sram.efc.bin"


def _artifact(env_name, default_name):
    env = os.environ.get(env_name)
    if env:
        return Path(env).expanduser()
    return CHECKOUT_ROOT.joinpath(*default_name.split("/"))


def reference_firmware():
    """The decoded group-1 image the descrambler self-tests compare to.  -> Path"""
    return _artifact(ENV_FW_REF, FW_REF_NAME)


def ddr_image():
    """The captured EFC DRAM image the ladder tables were read out of.  -> Path"""
    return _artifact(ENV_DDR_IMAGE, DDR_IMAGE_NAME)


def efc_sram_image():
    """Decrypted EFC SRAM firmware, known plaintext for a self-test.  -> Path"""
    return _artifact(ENV_EFC_SRAM, EFC_SRAM_NAME)


# There was a `trust_table_path` here, and a $TITANIA_TRUST to go with it: a
# per-directory rank table that decided which of two captures of the same page
# got served.  Both are gone.  A page either decodes or it does not, and that is
# a property of the read rather than of the directory the file landed in -- see
# the note at the top of `dumpdev`, and `dumpdev.adjudicate`, which settles 264
# of the reference store's 265 contested pages without consulting provenance at
# all.
