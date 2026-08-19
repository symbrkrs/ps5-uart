"""Where the measured tables live -- in this checkout and in an installed wheel.

WHAT IS A TABLE HERE
--------------------
Six files, and every one of them is a config image read OUT of the controller --
the ECU's own LDPC encoder and decoder RAM for the three codes the silicon
implements, plus A10R's base matrix in the stored-frame basis.  They describe
Marvell silicon, not any particular drive: the same bytes are correct on every
Titania, and nothing in them came off a NAND die.  That is why they ship.

Anything that could be computed from them was removed rather than carried:

    nd_ldpc_ee3.json    EE3's 41 x 2861 schedule.  `nd_ldpc.ee3_layers()`
                        parses it straight out of ek_ee3_dec.bin and gets the
                        same table entry for entry.
    seedmap_full.json   16384 scrambler seeds read through SCR_SEED_MAP_BYPASS.
                        `titania_nand.seed_state()` reproduces all of them from
                        325 bytes of ROM constants; the digest of the whole map
                        is kept in that module and the self-test recomputes it.

A10R's base matrix stays: the decoder image gives the mother schedule, but the
mapping onto the shortened stored frame -- which column is dropped, and the
column permutation that goes with it -- has not been derived, so the file is
still a measurement rather than a cached computation.

ONE COPY, IN THE PACKAGE
------------------------
`titania/data/<name>`, resolved relative to `__file__`.  That is the same
directory in a checkout, under `pip install -e .` and in an unpacked wheel, so
there is one location and one code path.

There used to be two.  The checkout root was searched first and the package data
second, because the codec chain lived at the repository root and a wheel has
nowhere to put a data file beside a TOP-LEVEL module.  The chain moved inside the
package, which removed the reason, but the second copy outlived it -- along with
a test asserting the two were byte-identical and a README rule saying to re-copy
a regenerated table or the suite would fail.  A duplicate that has to be kept in
step by hand is a defect waiting for someone to be busy.

What survives the merge is the better half of that check.  Every table is
verified against a digest recorded when it was read off the silicon, so damage is
caught even when there is only one copy -- which copy-vs-copy could never do,
because two copies corrupted the same way agree with each other.  That is not
hypothetical; it is what a careless sweep over `*.bin` produced here once.

WHAT THIS MODULE MUST NOT DO
----------------------------
It resolves paths and checks digests.  It does not parse or interpret a table,
and it holds no knowledge of what any of them contain -- the modules that were
verified against media keep every byte of that.
"""
import hashlib
from pathlib import Path

#: where the tables live, in every install shape
DATA_DIR = "data"
DATA = Path(__file__).resolve().parent / DATA_DIR

#: name -> (size, sha256).  The tables, and what they must be.
SHA256 = {
    "ek_ee3_dec.bin": (
        19468, "59ae120d9b7d6467deb530faf20a29fafd978c7c001ff2ceac5bd175c05aed11"),
    "ek_ee3_enc.bin": (
        6804, "b893baee4ec362c7a37886b05b7f79c0dc42a4c3899a44b4ae07100b5f50f4fe"),
    "nd_ecutbl_ecc0dec_code0_rdsel0_bank15.bin": (
        10240, "f2aa634be78836c6a09b0c1b751a2372cc8f7196d663861cb395fe670a758551"),
    "nd_ecutbl_ecc0enc_code0_rdsel0_bank3.bin": (
        4096, "1e1c7d6cf7639b76cfff8f0d8c4837134334e256081e39e4647f684d9e523194"),
    "nd_g2ldpc_a10r_base.json": (
        6712, "e190ec40dea7ab55e811b4cd57221380346bd593f64492b9bd0cd62f1cd639dd"),
    "ne_ldpc_dec_b11_rd0_n8192.bin": (
        16384, "c6fe43cb8e41ca87e813a42465c0cd996459e304a1f9b75bdf6b8c5adb4a2205"),
}

#: the tables, in a fixed order, so packaging can enumerate them
TABLES = tuple(sorted(SHA256))


def find(name):
    """The table `name`, or None if it is not there.  -> Path | None

    A plain `__file__`-relative path.  `importlib.resources` would be the
    careful answer if this project were ever installed zipped, and it is not --
    every caller does `.read_bytes()` or `open()` on the result, so a zipimport
    would fail loudly here rather than silently read the wrong bytes.
    """
    p = DATA / name
    return p if p.exists() else None


def table(name):
    """The table `name`.  -> Path.  Raises FileNotFoundError if it is missing."""
    p = find(name)
    if p is not None:
        return p
    raise FileNotFoundError(
        "%s: measured table not found in %s.  These ship with the package, so "
        "an install missing one is an install to reinstall -- they are read out "
        "of the controller and are not something the tool can regenerate."
        % (name, DATA))


def available(name):
    """Is this table present?  -> bool."""
    return find(name) is not None


def digest(path):
    """sha256 of a file.  -> str"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(name=None):
    """Check tables against `SHA256`.  -> [complaint], empty when all is well.

    `name` checks one table; the default checks every one that resolves.  A
    table that is absent is reported, because "missing" and "wrong" are the same
    problem to a caller that is about to decode with it.
    """
    bad = []
    for n in ([name] if name else TABLES):
        size, want = SHA256[n]
        p = find(n)
        if p is None:
            bad.append("%s: absent" % n)
            continue
        got = digest(p)
        if got != want:
            bad.append("%s: %s is %d B / %s, expected %d B / %s"
                       % (n, p, p.stat().st_size, got[:16], size, want[:16]))
    return bad


def _selftest():
    ok = True
    missing = [t for t in TABLES if not available(t)]
    print("[1] %d of %d table(s) resolvable: %s"
          % (len(TABLES) - len(missing), len(TABLES),
             "all" if not missing else "MISSING " + ", ".join(missing)))
    ok &= not missing
    for t in TABLES[:1] + TABLES[-1:]:
        print("    %-44s -> %s" % (t, table(t)))
    bad = verify()
    print("[2] digests: %s" % ("all match" if not bad else "\n    ".join(bad)))
    ok &= not bad
    try:
        table("nd_definitely_not_a_table.bin")
        print("[3] a missing table did NOT raise -- WRONG")
        ok = False
    except FileNotFoundError as exc:
        named = str(DATA) in str(exc)
        print("[3] a missing table raises, naming where it looked: %s"
              % ("named" if named else "INCOMPLETE MESSAGE"))
        ok &= named
    print("\nnd_tables selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
