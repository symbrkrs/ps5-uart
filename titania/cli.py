"""titania -- command-line front end.

    py -m titania info  
    py -m titania read   --device hw --ce 6 --block 8 --page 0 --slc -o page.bin
    py -m titania scan   --device hw --ce 1 --blocks 0-63 --slc
    py -m titania verify --ce 6 --block 8 --pages 0-16
    py -m titania decode page.bin --row 0
    py -m titania dump   --device hw --ce 6 --blocks 6-9 --slc -o out/
    py -m titania ftl   
    py -m titania fw     list
    py -m titania extract --mso 32 --lma a:B -o out.bin
    py -m titania trim   --mso 34
    py -m titania ns     placement --nsid 1 --lba 8192
    py -m titania raid   info
    py -m titania aux    page --ce 6 --block 8 --page 0 \\
                         --slc --layout 0

THE DEVICE SELECTOR
-------------------
`--device dump:<path>` reads raw page files; `--device hw` reads the live board.
Everything else in the CLI is written against `titania.Device`, so every command
works identically over either -- which is also what makes `verify --against` a
one-line cross-check of a dump against the media it came from.

WHY THE IMPORTS ARE LAZY
------------------------
`codec`, `ftl`, `fw`, `trim`, `namespace`, `raid` and `aux` are separate layers
with heavier dependencies (numpy, the LDPC tables) and separate development
lives.  The CLI imports each one only when a command needs it and degrades to a
clear message when a symbol is missing, so a half-finished layer can never be
the reason `titania info` stops working.

THREE ANSWERS, NOT TWO
----------------------
Every command here distinguishes resolved from a real negative (the drive's own
tables say nothing lives there) from UNKNOWN (this toolkit cannot answer from
what it holds).  Collapsing the third into either of the first two is how a
coverage number becomes a lie, and on this board UNKNOWN is the common and
correct answer for user data.  `_three_way` is the one place that formatting
lives so a new caller cannot accidentally print a guess.

READS THAT INFER
----------------
`HardwareDevice` can answer "is this page erased" with an exact verdict computed
on the EFC, and then synthesise the erased page rather than transfer 18 KB of
0xFF.  It is exact, but it is still a different provenance, so every command here
that reports on content counts the pages that came back `erased-device` and
prints the count.  `--no-fast-erased` turns the shortcut off entirely.
"""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import DeviceError, WriteProtected, ERASED_DEVICE
else:
    from . import geometry as geom
    from .device import DeviceError, WriteProtected, ERASED_DEVICE

PAGE_BYTES = 18432


# --------------------------------------------------------------------- lazy --
#: The package name, whether this file was imported as `titania.cli` or run
#: directly as a script (in which case the header put the parent on sys.path).
_PKG = __package__ or "titania"


def _sib(name):
    """Import a sibling module of this package.  Works either way this file is
    entered -- `py -m titania` and `py titania/cli.py` both have to work."""
    import importlib
    return importlib.import_module("%s.%s" % (_PKG, name))


def _load(name):
    """Import a sibling layer.  -> (module, None) or (None, message)."""
    try:
        return _sib(name), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def _need(name, *symbols):
    """The module, or a SystemExit explaining exactly what is missing.

    Not a silent fallback: a command that quietly produces less
    because a layer is half-written is worse than one that says so.
    """
    mod, err = _load(name)
    if mod is None:
        raise SystemExit("%s.%s is not usable yet (%s)\n"
                         "  this command needs it; the rest of the CLI does not"
                         % (_PKG, name, err))
    missing = [s for s in symbols if not hasattr(mod, s)]
    if missing:
        raise SystemExit("%s.%s does not define %s yet\n"
                         "  it is still being written; try again later"
                         % (_PKG, name, ", ".join(missing)))
    return mod


def _decoder():
    """The best available page decoder.  -> (callable, description)

    Prefers `titania.codec`; falls back to the standalone `titania_nand.py`,
    which is the same implementation one layer down.  Returns None if neither is
    importable, so the caller can degrade rather than crash.
    """
    mod, _err = _load("codec")
    if mod is not None and hasattr(mod, "decode_page"):
        return mod, "titania.codec"
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from . import titania_nand as tn
        return tn, "titania_nand (fallback -- titania.codec unavailable)"
    except Exception:
        return None, None


# -------------------------------------------------------------- device open --
def open_device(spec, writable=False, fast_erased=True, verbose=False):
    """`dump:<path>` | `dump` | `hw` | None -> a Device.

    `dump` with no path -- which is also what `spec=None` and the default mean --
    resolves through `titania.config`: $TITANIA_DUMP if it is set, otherwise a
    dump discovered beside the working directory or the checkout.  If a whole
    dump tree is named, given or discovered, `DumpDevice` opens the page store
    built from it instead, in one place (`dumpdev.canonical_root`), because the
    store is the canonical whole-media backing and stays whole while the tree is
    pruned.  Anything more specific -- `dump:<tree>/slc`, `dump:<tree>/raw` -- is
    one dumper's output and is opened exactly as written.

    The default used to be the literal `dump:the dump`: one capture's directory
    name compiled into the tool, so a fresh checkout ran every command against a
    path that did not exist and reported the result as an empty dump.  Discovery
    now either finds a dump or says, once, how to name one.
    """
    spec = spec if spec is None else str(spec)
    if spec is not None and (spec == "hw" or spec.startswith("hw:")):
        HardwareDevice = _sib("hwdev").HardwareDevice
        arg = spec[3:] if spec.startswith("hw:") else ""
        ces = [int(x, 0) for x in arg.split(",")] if arg else None
        dev = HardwareDevice(allow_hardware_writes=writable, ces=ces,
                             fast_erased=fast_erased, verbose=verbose)
        return dev
    if spec is None or spec == "dump" or spec.startswith("dump:"):
        path = spec[5:] if (spec or "").startswith("dump:") else ""
        if not path:
            cfg = _sib("config")
            try:
                path = cfg.default_root()
            except (cfg.NoDumpFound, cfg.AmbiguousDump) as exc:
                raise SystemExit(str(exc))
        dev = _sib("dumpdev").DumpDevice(path)
        dev.fast_erased = fast_erased
        return dev
    raise SystemExit("unknown --device %r; use dump:<path> or hw" % spec)


def close_device(dev):
    if hasattr(dev, "close"):
        try:
            dev.close()
        except Exception:
            pass


# ------------------------------------------------------------------ parsing --
def parse_range(text, lo=0, hi=None):
    """'6', '6-9', '6,8,11', '6-9,20' -> sorted list of ints."""
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, b = part.split("-", 1)
            out.extend(range(int(a, 0), int(b, 0) + 1))
        else:
            out.append(int(part, 0))
    out = sorted(set(out))
    if hi is not None:
        bad = [x for x in out if not lo <= x <= hi]
        if bad:
            raise SystemExit("out of range %d..%d: %s" % (lo, hi, bad[:8]))
    return out


def targets(args, dev):
    """-> list of (ce, block, page) from the address options."""
    ces = (parse_range(args.ces) if args.ces else
           ([args.ce] if args.ce is not None else dev.present_ces()))
    if not ces:
        raise SystemExit("no CEs: the backing reports none present")
    npages = geom.pages_per_block(args.slc)
    blocks = (parse_range(args.blocks) if args.blocks else
              ([args.block] if args.block is not None else None))
    if blocks is None:
        raise SystemExit("say which block: --block N or --blocks A-B")
    pages = (parse_range(args.pages, 0, npages - 1) if args.pages else
             ([args.page] if args.page is not None else list(range(npages))))
    return [(c, b, p) for c in ces for b in blocks for p in pages]


def hexdump(data, limit=256, base=0):
    out = []
    for off in range(0, min(len(data), limit), 16):
        b = data[off:off + 16]
        txt = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
        out.append("  %06X  %-47s  %s" % (base + off, b.hex(" "), txt))
    if len(data) > limit:
        out.append("  ... %d more bytes" % (len(data) - limit))
    return "\n".join(out)


class Provenance(object):
    """Counts how each page in a run was obtained, so a report can say."""

    def __init__(self):
        self.n = {}

    def add(self, source):
        self.n[source] = self.n.get(source, 0) + 1

    def line(self):
        if not self.n:
            return "no pages read"
        parts = ["%d %s" % (v, k) for k, v in sorted(self.n.items())]
        s = "pages by provenance: " + ", ".join(parts)
        if self.n.get(ERASED_DEVICE):
            s += ("\n  (%d page(s) were NOT transferred: the EFC examined all "
                  "%d bytes and reported them all-0xFF. Exact, but inferred at "
                  "the host -- use --no-fast-erased to transfer them)"
                  % (self.n[ERASED_DEVICE], PAGE_BYTES))
        return s


# ----------------------------------------------------------------- commands --
def cmd_info(args):
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        print(dev.describe() if hasattr(dev, "describe") else repr(dev))
        print("")
        print("layers:")
        for name in ("codec", "fw", "ftl", "extract"):
            mod, err = _load(name)
            if mod is None:
                print("  %-6s UNAVAILABLE  %s" % (name, err))
            else:
                pub = getattr(mod, "__all__", None)
                print("  %-6s ok           %s"
                      % (name, "%d public names" % len(pub) if pub
                         else "(no __all__)"))
        _codec, how = _decoder()
        print("  decode via %s" % (how or "NOTHING -- decode/verify unavailable"))
    finally:
        close_device(dev)
    return 0


def cmd_read(args):
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    prov = Provenance()
    try:
        tg = targets(args, dev)
        if args.out and len(tg) > 1 and not args.out.endswith("/"):
            raise SystemExit("more than one page: give -o a directory (trailing /)")
        blobs = []
        errors = 0
        for ce, block, page in tg:
            try:
                pr = dev.read_page_ex(ce, block, page, slc=args.slc)
            except DeviceError as exc:
                errors += 1
                print("ce%d b%d p%d: %s" % (ce, block, page, exc))
                continue
            prov.add(pr.source)
            blobs.append(((ce, block, page), pr))
        for (ce, block, page), pr in blobs:
            row = geom.compose_row(block, page)
            if args.out and args.out.endswith("/"):
                d = Path(args.out)
                d.mkdir(parents=True, exist_ok=True)
                p = d / ("ce%02d_b%04d_p%04d.bin" % (ce, block, page))
                p.write_bytes(pr.data)
                print("ce%d b%d p%d row=%#x -> %s (%s)"
                      % (ce, block, page, row, p, pr.source))
            elif args.out:
                Path(args.out).write_bytes(pr.data)
                print("ce%d b%d p%d row=%#x -> %s (%d B, %s)"
                      % (ce, block, page, row, args.out, len(pr.data),
                         pr.source))
            else:
                print("ce%d b%d p%d row=%#x  %d B  source=%s"
                      % (ce, block, page, row, len(pr.data), pr.source))
                if args.decode:
                    _print_decoded(pr.data, row, args)
                else:
                    print(hexdump(pr.data, args.limit))
        print("\n" + prov.line())
        if errors:
            print("%d page(s) unreadable" % errors)
        return 1 if errors and not blobs else 0
    finally:
        close_device(dev)


def _print_decoded(raw, row, args):
    codec, how = _decoder()
    if codec is None:
        print("  (no decoder available -- titania.codec and titania_nand both "
              "failed to import)")
        return
    fmt = getattr(codec, "FORMAT_B" if args.group2 else "FORMAT_A", None)
    try:
        res = codec.decode_page(raw, row, fmt) if fmt is not None \
            else codec.decode_page(raw, row)
    except Exception as exc:
        print("  decode failed (%s): %s" % (how, exc))
        return
    aus = getattr(res, "aus", [])
    print("  decoded with %s: %d AU(s), %d MCRC-valid"
          % (how, len(aus), sum(1 for a in aus if getattr(a, "mcrc_ok", False))))
    for i, a in enumerate(aus):
        user = getattr(a, "user", b"")
        print("   AU%d mcrc=%s ecc=%s aux=%s"
              % (i, getattr(a, "mcrc_ok", "?"), getattr(a, "ecc_ok", "?"),
                 getattr(a, "aux", b"").hex()))
        print(hexdump(user, min(args.limit, 128)))


def cmd_decode(args):
    # `decode` takes a file, not a device, but it goes through `common()` and so
    # accepts `--device` / `--no-fast-erased` and reads neither.  Say so rather
    # than letting `titania decode p.bin --device hw` look like it read the
    # board.
    ignored = [n for n, v in (("--device", getattr(args, "device", None)
                               not in (None, "dump", "")),
                              ("--no-fast-erased", args.no_fast_erased))
               if v]
    if ignored:
        print("note: %s ignored -- `decode` reads the file named on the command "
              "line and never opens a device" % " and ".join(ignored))
    raw = Path(args.file).read_bytes()
    if len(raw) != PAGE_BYTES:
        print("warning: %s is %d bytes, not a %d-byte page"
              % (args.file, len(raw), PAGE_BYTES))
    row = args.row if args.row is not None else 0
    print("%s  %d B  row=%#x" % (args.file, len(raw), row))
    scan_bytes = _sib("dumpdev").scan_bytes
    s = scan_bytes(raw)
    print("  all_ones=%s nonff=%d first_nonff=%s crc=%#010x"
          % (s.all_ones, s.nonff_bytes, s.first_nonff, s.crc))
    _print_decoded(raw, row, args)
    return 0


def cmd_scan(args):
    """The cheap survey: an exact verdict per page, computed device-side."""
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        if not args.pages and args.page is None:
            args.page = 0        # a block survey looks at page 0 by default
        tg = targets(args, dev)
        n_er = n_wr = n_none = n_err = 0
        for ce, block, page in tg:
            try:
                s = dev.scan_page(ce, block, page, slc=args.slc)
            except DeviceError as exc:
                n_err += 1
                print("ce%2d b%4d p%4d  ERROR %s" % (ce, block, page, exc))
                continue
            if s is None:
                n_none += 1
                continue
            if s.all_ones:
                n_er += 1
            else:
                n_wr += 1
            if not args.quiet:
                print("ce%2d b%4d p%4d  %-7s nonff=%-6d first=%-8s crc=%08x"
                      % (ce, block, page, "erased" if s.all_ones else "data",
                         s.nonff_bytes,
                         "-" if s.first_nonff is None else s.first_nonff,
                         s.crc))
        print("\n%d scanned: %d erased, %d with data, %d errors"
              % (len(tg), n_er, n_wr, n_err))
        if n_none:
            print("%d page(s) could not be scanned: this backing has no "
                  "reduction primitive" % n_none)
        print("every verdict above is EXACT over all %d bytes -- no sampling"
              % PAGE_BYTES)
        return 0
    finally:
        close_device(dev)


def _page_crc(dev, ce, block, page, slc):
    """CRC-32 of one page.  -> (crc, how)

    Prefers the backing's own reduction, which on hardware means the EFC reads
    the page and returns four bytes: a whole-block comparison against a dump
    then costs no page transfers at all (measured 7.4x).  Falls back to
    transferring and hashing, which is what a backing without a reduction has to
    do anyway.
    """
    scan_bytes = _sib("dumpdev").scan_bytes
    s = dev.scan_page(ce, block, page, slc=slc)
    if s is not None:
        return s.crc, "reduced by the backing"
    return scan_bytes(dev.read_page(ce, block, page, slc=slc)).crc, "transferred"


def cmd_verify(args):
    """MCRC / ECC over a range, or a CRC cross-check against another backing.

    `fast_erased` is forced off here.  A verification pass must not be handed
    its answer by the same shortcut it is checking -- so when a full page is
    read, it really is read.

    `--crc-only` is the cheap sweep: it asks each backing for a page CRC instead
    of the page.  That is exact (the EFC examines all 18432 bytes) and it is the
    only mode in which a whole-block hardware-vs-dump comparison is practical.
    """
    dev = open_device(args.device, fast_erased=False, verbose=args.verbose)
    other = open_device(args.against, fast_erased=False) if args.against else None
    codec, how = _decoder()
    if args.crc_only:
        return _verify_crc(args, dev, other)
    try:
        tg = targets(args, dev)
        ok = bad = miss = 0
        crc_same = crc_diff = 0
        scan_bytes = _sib("dumpdev").scan_bytes
        for ce, block, page in tg:
            row = geom.compose_row(block, page)
            try:
                raw = dev.read_page(ce, block, page, slc=args.slc)
            except DeviceError as exc:
                miss += 1
                if not args.quiet:
                    print("ce%2d b%4d p%4d  UNREADABLE %s" % (ce, block, page, exc))
                continue
            line = "ce%2d b%4d p%4d" % (ce, block, page)
            if other is not None:
                try:
                    ref = other.read_page(ce, block, page, slc=args.slc)
                    same = scan_bytes(raw).crc == scan_bytes(ref).crc
                    crc_same += bool(same)
                    crc_diff += (not same)
                    line += "  vs %s: %s" % (args.against,
                                             "MATCH" if same else "DIFFER")
                except DeviceError:
                    line += "  vs %s: absent" % args.against
            if codec is not None:
                fmt = getattr(codec, "FORMAT_B" if args.group2 else "FORMAT_A",
                              None)
                try:
                    res = (codec.decode_page(raw, row, fmt) if fmt is not None
                           else codec.decode_page(raw, row))
                    aus = getattr(res, "aus", [])
                    good = sum(1 for a in aus if getattr(a, "mcrc_ok", False))
                    ecc = sum(1 for a in aus if getattr(a, "ecc_ok", False))
                    line += "  mcrc %d/%d  ecc %d/%d" % (good, len(aus), ecc,
                                                         len(aus))
                    if good == len(aus) and aus:
                        ok += 1
                    else:
                        bad += 1
                except Exception as exc:
                    bad += 1
                    line += "  decode failed: %s" % exc
            if not args.quiet:
                print(line)
        print("")
        if codec is None:
            print("no decoder available -- MCRC/ECC verification SKIPPED "
                  "(titania.codec and titania_nand both failed to import)")
        else:
            print("MCRC: %d pages fully valid, %d not, %d unreadable (via %s)"
                  % (ok, bad, miss, how))
        if other is not None:
            print("CRC vs %s: %d match, %d differ"
                  % (args.against, crc_same, crc_diff))
        print("fast_erased was OFF for this run: every byte was transferred, so "
              "nothing here was inferred")
        return 0 if bad == 0 and crc_diff == 0 else 1
    finally:
        close_device(dev)
        close_device(other)


def _verify_crc(args, dev, other):
    """`--crc-only`: compare page CRCs, computed as close to each medium as
    possible.  No page transfers where the backing can reduce for itself."""
    try:
        tg = targets(args, dev)
        if other is None:
            raise SystemExit("--crc-only needs --against <device> to compare "
                             "with; on its own a CRC has nothing to check")
        same = diff = miss = 0
        hows = set()
        for ce, block, page in tg:
            try:
                a, how_a = _page_crc(dev, ce, block, page, args.slc)
            except DeviceError as exc:
                miss += 1
                if not args.quiet:
                    print("ce%2d b%4d p%4d  UNREADABLE %s" % (ce, block, page, exc))
                continue
            try:
                b, how_b = _page_crc(other, ce, block, page, args.slc)
            except DeviceError:
                miss += 1
                if not args.quiet:
                    print("ce%2d b%4d p%4d  absent from %s"
                          % (ce, block, page, args.against))
                continue
            hows.add(how_a)
            hows.add(how_b)
            same += (a == b)
            diff += (a != b)
            if not args.quiet:
                print("ce%2d b%4d p%4d  %08x vs %08x  %s"
                      % (ce, block, page, a, b, "MATCH" if a == b else "DIFFER"))
        print("\nCRC vs %s: %d match, %d differ, %d unavailable"
              % (args.against, same, diff, miss))
        print("CRCs obtained by: %s.  'reduced by the backing' means all %d "
              "bytes were examined where they live -- on the EFC for hardware, "
              "in the file for a dump -- and only the CRC was returned. Nothing "
              "was sampled either way."
              % (", ".join(sorted(hows)) or "-", PAGE_BYTES))
        return 0 if diff == 0 else 1
    finally:
        close_device(dev)
        close_device(other)


def cmd_capture(args):
    """`titania capture` -- the smart acquisition.  See `titania.smartdump`."""
    return _sib("smartdump").main([
        "--ce", args.ce, "--out", args.out, "--cellmap", args.cellmap,
        "--lun", str(args.lun),
    ] + (["--weru-table", args.weru_table] if args.weru_table else [])
      + (["--max-level", str(args.max_level)]
         if args.max_level is not None else [])
      + (["--no-correct"] if args.no_correct else []))


def cmd_dump(args):
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    out = Path(args.out or "titania_dump")
    out.mkdir(parents=True, exist_ok=True)
    prov = Provenance()
    try:
        tg = targets(args, dev)
        written = skipped = 0
        for i, (ce, block, page) in enumerate(tg):
            p = out / ("ce%02d_b%04d_p%04d.bin" % (ce, block, page))
            if p.exists() and not args.force:
                skipped += 1
                continue
            try:
                pr = dev.read_page_ex(ce, block, page, slc=args.slc)
            except DeviceError as exc:
                print("ce%d b%d p%d: %s" % (ce, block, page, exc))
                continue
            p.write_bytes(pr.data)
            prov.add(pr.source)
            written += 1
            if args.verbose or (i % 50 == 0):
                print("  [%d/%d] %s (%s)" % (i + 1, len(tg), p.name, pr.source))
        print("\n%d page(s) written to %s, %d already present (use --force to "
              "overwrite)" % (written, out, skipped))
        print(prov.line())
        if not args.slc:
            print("NOTE: written as native-TLC pages.  A re-scan of this "
                  "directory only sees that if it is named 'tlc' -- "
                  "titania.dumpdev takes the cell mode from the directory name.")
        return 0
    finally:
        close_device(dev)


def cmd_ftl(args):
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        try:
            # Goes through `_mount_ftl`, the file's one mount site.  This used
            # to be a bare `Ftl.from_device(dev)`, which bypassed it and dropped
            # the msoIdTable input; the table is embedded now, so this command
            # names its MSOs on a checkout with no firmware image anywhere.
            _ftlmod, f = _mount_ftl(args, dev)
        except Exception as exc:
            print("Ftl.from_device failed: %s: %s" % (type(exc).__name__, exc))
            return 1
        if hasattr(f, "describe"):
            print(f.describe())
        else:
            print(repr(f))
        return 0
    finally:
        close_device(dev)


def _fw_verify_table(args):
    """`fw verify-table <image>` -- is the embedded msoIdTable this image's one?

    This is the answer to the one real objection to embedding the table: a
    different firmware revision could carry a different one.  So it is a real
    command with a real exit code (0 agree, 1 differ, 2 no table in the image),
    not a note, and a mismatch names the differing (msoType, msoGroupId) entries
    with both msoIds rather than saying "differs".

    It touches no device: the comparison is a firmware image against a constant.
    """
    ftlmod = _need("ftl", "compare_mso_table", "embedded_mso_table")
    if not getattr(args, "fw_image", None):
        raise SystemExit(
            "verify-table needs an image: `titania fw verify-table <image>`\n"
            "  e.g. titania fw verify-table dumps/fw.sram.efc.bin\n"
            "  (there is no default: comparing the embedded table with itself "
            "would prove nothing)")
    path = Path(args.fw_image)
    if not path.exists():
        raise SystemExit("verify-table needs a firmware image path; %s does not "
                         "exist" % path)
    emb = ftlmod.embedded_mso_table()
    print("embedded msoIdTable : %d bytes, %d entries, firmware %s "
          "(from %s at %#x)"
          % (len(emb.raw), emb.n_entries, ftlmod.MSOID_TABLE_FW_VERSION,
             ftlmod.MSOID_TABLE_FW_IMAGE, ftlmod.MSOID_TABLE_HINT))
    print("image               : %s (%d bytes)" % (path, path.stat().st_size))
    try:
        res = ftlmod.compare_mso_table(path)
    except LookupError as exc:
        print("\nNO msoIdTable IN THIS IMAGE: %s" % exc)
        print("That is not a mismatch -- it is an image that does not contain "
              "the table at all (a partial capture, or a region that never held "
              "it).  The embedded table is unaffected and still in use.")
        return 2
    except ValueError as exc:
        print("\nthe candidate in this image FAILED VALIDATION: %s" % exc)
        return 2
    found = res["found"]
    print("located at          : %#x, %d entries" % (found.offset,
                                                     found.n_entries))
    if res["agree"]:
        print("\nAGREE: all %d bytes identical." % len(emb.raw))
        print("This image's msoIdTable is the one titania has built in, so "
              "every MSO name this tool prints is that image's own.")
        return 0
    print("\nDIFFER: %d of %d byte(s).  The embedded table is firmware %s; if "
          "your image is a different build, IT is the authority for your drive."
          % (len(res["diffs"]), len(emb.raw), ftlmod.MSOID_TABLE_FW_VERSION))
    print("\n  %-4s %-24s %-22s %8s %8s"
          % ("idx", "msoType", "msoGroup", "embedded", "image"))
    for idx, t, g, a, b in res["diffs"]:
        print("  %-4d %-24s %-22s %8s %8s"
              % (idx,
                 "%d %s" % (t, ftlmod.MSO_TYPES[t] if t < len(ftlmod.MSO_TYPES)
                            else "?"),
                 "%d %s" % (g, ftlmod.MSO_GROUPS[g]
                            if g < len(ftlmod.MSO_GROUPS) else "?"),
                 "-" if a is None else a, "-" if b is None else b))
    print("\n'-' means that (type, group) is not a partition in that table "
          "(0xFF, or padding past the end).")
    return 1


def cmd_fw(args):
    # `verify-table` compares a file with a constant.  It must not open a
    # device: doing so would make an offline check fail for want of a dump.
    if args.fw_action == "verify-table":
        return _fw_verify_table(args)
    fw = _need("fw")
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        if args.fw_action == "list":
            fn = getattr(fw, "find_images_report", None) or \
                getattr(fw, "find_images", None)
            if fn is None:
                raise SystemExit("titania.fw has neither find_images_report nor "
                                 "find_images yet")
            res = fn(dev)
            if isinstance(res, str):
                print(res)
            else:
                for x in (res or []):
                    print("  %r" % (x,))
                if not res:
                    print("  no images found (which on this backing may mean "
                          "'not surveyed', not 'not present')")
            return 0
        if args.fw_action == "extract":
            if args.fw_id is None:
                raise SystemExit("extract needs --fw-id N")
            fn = getattr(fw, "extract", None)
            if fn is None:
                raise SystemExit("titania.fw.extract does not exist yet")
            # `fw.extract` returns a FirmwareImage descriptor, not bytes --
            # its own docstring says so.  This used to do
            # `getattr(blob, "data", blob)` then `bytes(...)`, and since a
            # FirmwareImage has no `.data` the fallback handed the descriptor
            # itself to `bytes()`:
            #     TypeError: cannot convert 'FirmwareImage' object to bytes
            # i.e. `fw extract` FAILED for every valid fw_id and had never
            # worked.  `.blob()` is the accessor that reassembles the image.
            try:
                img = fn(dev, args.fw_id)
            except KeyError:
                # Raised for an unknown id.  Report it the way `fw list` prints
                # ids -- hex -- instead of letting a bare KeyError traceback out
                # with a decimal list the listing never showed.
                known = sorted(getattr(fw, "FW_IDS", ()) or ())
                raise SystemExit(
                    "unknown --fw-id %#x.  Known: %s\n"
                    "(`titania fw list` shows which are locatable here.)"
                    % (args.fw_id, ", ".join("%#x" % k for k in known)))
            except LookupError as exc:
                raise SystemExit("%s\n(it is a known FW_ID but not present on "
                                 "this backing -- try `titania fw list`.)" % exc)
            # Default for group 2 (fw_id 1 and 2): Decrypt.  The key is
            # derivable -- `image_key()` is PBKDF2-hmac-SHA256 over a known OTP
            # root -- so ciphertext is not the best this tool can do, and
            # handing back ciphertext by default made it look as though the
            # descramble had failed.  `decrypt()` returns the plaintext body
            # with the header row already excluded.
            #
            # `--on-media` gives the byte-exact image instead: header row plus
            # undecrypted payload, i.e. what is actually stored.
            on_media = getattr(args, "on_media", False)
            kind = ""
            if getattr(img, "group", None) == 2 and not on_media:
                try:
                    data = img.decrypt()
                except ImportError:
                    raise SystemExit(
                        "decrypting group 2 needs pycryptodome "
                        "(pip install pycryptodome).\n"
                        "Use --on-media to write the stored image without "
                        "decrypting.")
                kind = "  [decrypted]"
            elif getattr(img, "group", None) == 2 and on_media:
                try:
                    data = img.header_blob() + img.blob()
                except (ValueError, LookupError) as exc:
                    raise SystemExit("cannot read the group 2 header row: %s"
                                     % exc)
                kind = "  [on-media: header + payload, not decrypted]"
            else:
                data = img.blob() if hasattr(img, "blob") else img
            if not isinstance(data, (bytes, bytearray)):
                raise SystemExit(
                    "titania.fw.extract returned %s, and it carries no blob() --"
                    " the CLI cannot turn that into bytes"
                    % type(data).__name__)
            p = Path(args.out or ("fw_%#x.bin" % args.fw_id))
            p.write_bytes(data)
            # `blob()` zero-fills missing pages and counts them, so say whether
            # this image is complete rather than only how big it is.
            rep = getattr(img, "_report", None) or {}
            miss = rep.get("missing")
            note = "" if not miss else "  ** %d page(s) MISSING, zero-filled **" % miss
            print("fw_id %#x -> %s (%d bytes)%s%s"
                  % (args.fw_id, p, len(data), kind, note))
            return 0
        raise SystemExit("fw: use 'list', 'extract' or 'verify-table'")
    finally:
        close_device(dev)


def _open_ftl(args, dev):
    """Mount the FTL for `extract`.  -> (Ftl, resolver, the device it mounted).

    `--ftl-device` exists because the two jobs are not always best served by the
    same backing: the metadata scan wants the WERUs the mount reads, the payload
    reads want wherever the user pages are.  Default is the same device, which
    is the honest arrangement -- one drive, one answer -- and anything else is
    recorded in the manifest so a later reader can see that two backings were
    involved.

    It goes through `_mount_ftl` and not through `Ftl.from_device` directly.
    Building the mount here by hand is what dropped `--firmware` in `cmd_ftl`;
    this was the second copy of that call and it dropped it the same way, so
    there is now exactly one place in this file that mounts an FTL.
    """
    exmod = _need("extract", "Extractor", "FtlResolver")
    mdev = dev
    if getattr(args, "ftl_device", None):
        mdev = open_device(args.ftl_device, fast_erased=False)
    _ftlmod, f = _mount_ftl(args, mdev)
    return f, exmod.FtlResolver(f), mdev


def cmd_extract(args):
    """Pull an MSO's LMAs off the media, verified, resumable, with provenance."""
    ex = _need("extract", "Extractor", "FtlResolver", "lma_sequence",
               "parse_lma_range", "ResumeMismatch", "CanaryFailed",
               "canary_check", "CANARY", "report", "VERIFIED", "CORRECTED")
    if args.mso is None:
        raise SystemExit("extract needs --mso N (the MSO whose LMA space to "
                         "read); `titania ftl` lists them")
    # `--survey` used to be accepted as a third alternative here, but
    # `Extractor.open` demands one of the two output paths regardless -- so
    # `extract --survey` alone got past this guard and died in a traceback from
    # extract.py:853 instead of the sentence this line exists to give.
    if not args.out and not args.manifest:
        raise SystemExit("extract needs -o FILE (and writes FILE.manifest.jsonl "
                         "beside it) or --manifest FILE.%s  A run with neither "
                         "records nothing and is not resumable."
                         % ("  --survey does not write data, but it still "
                            "records what it resolved, so it needs "
                            "--manifest FILE." if args.survey else ""))
    # A verification pass must not be handed its answer by the shortcut it is
    # checking, and a synthesised all-0xFF page would be reported as `erased`
    # without a byte having crossed the wire.  `erased` is a claim about the
    # media, so make it the expensive way.
    dev = open_device(args.device, fast_erased=False, verbose=args.verbose)
    hw = args.device and args.device.startswith("hw")
    mdev = None
    try:
        if hw:
            # Two independent facts, asserted before anything reaches the bus.
            if dev.writable is not False or dev.dry_run is not True:
                raise SystemExit("refusing to touch the board: writable=%r "
                                 "dry_run=%r -- this instance is armed"
                                 % (dev.writable, dev.dry_run))
            print("SAFETY: writable=%s writes_enabled=%s dry_run=%s  "
                  "(read-only: no program, no erase)"
                  % (dev.writable, dev.writes_enabled, dev.dry_run))
            # Say the one non-read command out loud rather than letting a
            # blanket "read-only" cover it.  SET FEATURES 0xEF writes the die's
            # read reference registers -- volatile, no cell is touched, nothing
            # on the media changes -- but it is a command, it persists until
            # changed, and a run that left one set would make every later read
            # quietly wrong.  `read_page_retry` restores level 0 in its own
            # `finally` and `close()` restores again on the way out.
            if not args.no_read_retry:
                print("        read-retry is ON: SET FEATURES 0x%02X may be "
                      "issued to move the READ REFERENCE voltages (volatile; "
                      "no cell is programmed or erased), and every level is "
                      "restored to 0 by the walk's finally and again by "
                      "close().  --no-read-retry forbids even that."
                      % 0xEF)
        ftl, resolver, mdev = _open_ftl(args, dev)
        lo = hi = None
        if args.lma:
            lo, hi = ex.parse_lma_range(args.lma)
        try:
            lmas = ex.lma_sequence(resolver, args.mso, lo, hi,
                                   all_mapped=args.all_mapped, weru=args.weru,
                                   count=args.count, sample=args.sample)
        except ValueError as exc:
            raise SystemExit("extract: %s" % exc)
        if not lmas:
            print("no LMAs selected -- nothing to do")
            return 0
        fill = None if args.fill in (None, "none") else int(args.fill, 0)
        try:
            levels = (None if args.retry_levels is None
                      else ex.parse_retry_levels(args.retry_levels))
        except ValueError as exc:
            raise SystemExit("extract: --retry-levels: %s" % exc)
        extractor = ex.Extractor(
            dev, resolver, out=args.out, manifest=args.manifest,
            layout=args.layout, fill=fill, restart=args.restart,
            retry=args.retry, page_cache=args.page_cache, survey=args.survey,
            read_retry=not args.no_read_retry, retry_levels=levels,
            verbose=args.verbose)
        try:
            extractor.open(args.mso, lmas[0], header_extra={
                "device": args.device,
                "ftl_device": args.ftl_device or args.device,
                "selection": args.lma or ("all-mapped" if args.all_mapped
                                          else "?"),
                "weru": args.weru})
        except ex.ResumeMismatch as exc:
            raise SystemExit("extract: %s" % exc)
        m = extractor.manifest
        print("mso %d: %d LMA(s) selected, %d..%d%s"
              % (args.mso, len(lmas), lmas[0], lmas[-1],
                 ";  manifest %s holds %d record(s) already"
                 % (m.path.name, len(m.records)) if m.resumed else ""))
        ladder = list(extractor.ladder(False))
        if extractor.read_retry and not ladder:
            print("read-retry: requested, but this backing has no ladder "
                  "(supports_retry=%s) -- every page will be read at the die's "
                  "defaults and a page that does not decode there is withheld"
                  % getattr(dev, "supports_retry", False))
        elif ladder:
            print("read-retry: ladder %s (TLC) / %s (pseudo-SLC).  A page that "
                  "fails at the defaults is re-read up these and the winning "
                  "level is recorded per LMA as \"rty\"."
                  % (ladder, list(extractor.ladder(True))))
        # `check` was a name bound to None on one path and to a `def` on the
        # other, which is a type error and reads as one.  Define the closure,
        # then choose.
        check = None
        check_every = 0
        if hw and not args.no_canary:
            ref = [None]

            def _canary(tag):
                ref[0] = ex.canary_check(dev, ref[0], tag)
                if args.verbose or tag in ("open", "close"):
                    print("  canary ce%d/b%d/p%d CRC %#010x  [%s]"
                          % (ex.CANARY[0], ex.CANARY[1], ex.CANARY[2],
                             ref[0], tag))
            check = _canary
            check_every = args.canary_every or 0
        st = extractor.run(args.mso, lmas,
                           progress_every=args.progress,
                           check=check, check_every=check_every)
        print(ex.report(m, st, extractor.sink, selected=len(lmas),
                        span=(lmas[0], lmas[-1] + 1)))
        if hw:
            print("\nhardware: %s" % (getattr(dev, "stats", {}),))
            print("no media-modifying command was issued: this instance was "
                  "never armed (writable=False, dry_run=True), so write_page "
                  "and erase_block could not reach the bus.")
            try:
                left = {"ce%d %s" % (c, "SLC" if s else "TLC"): v
                        for (c, s), v in sorted(dev._retry.items()) if v}
                print("read-retry levels still set on exit: %s%s"
                      % (left or "none",
                         "  -- close() restores these too" if left else ""))
            except Exception:
                pass
        got = (st.by_status.get(ex.VERIFIED, 0)
               + st.by_status.get(ex.CORRECTED, 0))
        return 0 if (got or st.skipped_resume or args.survey) else 1
    except ex.CanaryFailed as exc:
        print("\nABORTED: %s" % exc)
        print("everything recorded before the last good canary stands; "
              "everything after it does not.  Reset and re-run -- the manifest "
              "will resume from what is already accounted for.")
        return 2
    finally:
        close_device(dev)
        if mdev is not None and mdev is not dev:
            close_device(mdev)


# ------------------------------------------------- the four analysis layers --
# `trim`, `ns`, `raid` and `aux` are finished modules that had no command line
# at all.  They are wired the same lazy way as everything above: `_need` turns a
# half-written or unimportable layer into one clear sentence instead of a
# traceback, so a broken layer can never be the reason `titania info` stops
# working.
#
# All four keep the toolkit's three-way answer -- resolved / real negative /
# UNKNOWN -- and none of them collapses the third into either of the first two.
# `_three_way` is the single place that formatting lives, so a new caller cannot
# accidentally print a guess.
_UNKNOWNS = None


def _unknown_sentinels():
    global _UNKNOWNS
    if _UNKNOWNS is None:
        out = []
        for name in ("ftl", "auxdata", "trim", "namespace"):
            mod, _err = _load(name)
            u = getattr(mod, "UNKNOWN", None) if mod is not None else None
            if u is not None and not any(u is x for x in out):
                out.append(u)
        _UNKNOWNS = tuple(out)
    return _UNKNOWNS


def _is_unknown(v):
    """Is this the UNKNOWN sentinel?  -> bool.

    There are two distinct ones in this package -- `ftl.UNKNOWN` (class
    `Unknown`, repr `<unknown>`) and `aux.UNKNOWN` (class `_Unknown`, repr
    `UNKNOWN`) -- and they are not the same object.  A caller that identity-
    checks only one prints the other as though it were a real answer, which is
    exactly the failure the three-way convention exists to prevent.  Identity
    against every layer's sentinel first, then the class name as a backstop for
    a layer that grows a third.
    """
    if v is None or isinstance(v, (int, float, bytes, bytearray, str)):
        return False
    if any(v is u for u in _unknown_sentinels()):
        return True
    return type(v).__name__ in ("Unknown", "_Unknown")


def _three_way(v, resolved, negative, unknown):
    """Format a resolved / None / UNKNOWN answer.  -> str.

    `None` is a real negative -- the media's own tables say nothing lives there
    -- and is not the same claim as UNKNOWN, which says only that this toolkit
    cannot answer from what it holds.  Printing them the same way is how a
    coverage number becomes a lie.
    """
    # `% (x,)` throughout: a tuple answer (a raw (ce, block, page, au) address
    # is one) would otherwise be unpacked as format arguments and raise.
    if _is_unknown(v):
        return "UNKNOWN   %s" % (unknown,)
    if v is None:
        return "no        %s" % (negative,)
    return "yes       %s" % (resolved,)


def _mount_ftl(args, dev, need_codec=True):
    """-> (the ftl module, a mounted `Ftl`) for the analysis commands.

    Shares `--ftl-cache` with `extract` so the two do not each pay for a scan.

    There is no `--firmware` any more.  The msoIdTable is a firmware constant
    and is now embedded in `titania.ftl` (`MSOID_TABLE_EMBEDDED`, fw 09P07M3),
    so every mount names its MSOs with no input at all -- which is what the
    offline use case needs.  The old flag turned a missing file into a row of
    UNKNOWNs that read like a limitation of the drive; `titania fw verify-table
    <image>` is where an image gets compared now and with an exit
    code, instead of silently deciding what a mount can say.
    """
    ftlmod = _need("ftl", "Ftl")
    cache = getattr(args, "ftl_cache", None)
    codec = ftlmod.default_codec() if need_codec else None
    f = ftlmod.Ftl.from_device(
        dev, codec=codec, cache_dir=Path(cache) if cache else None)
    return ftlmod, f


def cmd_trim(args):
    """What the host discarded -- and why the LUT cannot be asked instead."""
    trim = _need("trim", "TrimLog", "TRIMMED", "LIVE")
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        _ftlmod, ftl = _mount_ftl(args, dev)
        try:
            log = trim.TrimLog.scan(dev, ftl)
        except Exception as exc:
            print("trim log scan failed: %s: %s" % (type(exc).__name__, exc))
            return 1

        if args.lma is not None:
            if args.mso is None:
                raise SystemExit("--lma needs --mso N: an LMA is only an "
                                 "address inside one MSO's space")
            lut = None
            try:
                reg = ftl.lut_region(args.mso)
                lut = None if _is_unknown(reg) else reg
            except Exception:
                lut = None
            verdict = log.is_trimmed(args.mso, args.lma, lut=lut)
            print("mso %d lma %d: %s" % (args.mso, args.lma, verdict))
            print({
                trim.TRIMMED: "  a surviving type-0 entry names this LMA and "
                              "the LUT no longer maps it.",
                trim.LIVE: "  the LUT still maps it to a real WMA, and Trim "
                           "clears the entry to 0xFFFFFFFF -- so it has been "
                           "written since.",
            }.get(verdict,
                  "  UNKNOWN, and that is the COMMON answer.  The log is a "
                  "bounded window, so 'not named here' is not 'not trimmed'; "
                  "and a trimmed LUT entry is 0xFFFFFFFF, byte-identical to "
                  "one never written.  The sentinel has ZERO discriminative "
                  "power -- see titania.trim's headline."))
            print("  LUT consulted: %s" % ("yes" if lut is not None else
                                           "no (this MSO has no readable LUT "
                                           "region here)"))
            return 0

        if args.mso is not None:
            rows = log.trimmed_ranges(args.mso)
            print("mso %d: %d surviving Trim range(s) in the log" %
                  (args.mso, len(rows)))
            for _m, lma, count in rows[:args.limit or 40]:
                print("  lma %-10d + %-8d  (%d..%d)"
                      % (lma, count, lma, lma + count - 1))
            if len(rows) > (args.limit or 40):
                print("  ... %d more" % (len(rows) - (args.limit or 40)))
            n = len(log.trimmed_lmas(args.mso))
            print("  %d distinct LMA(s) named.  Everything NOT named is live "
                  "or never-written or trimmed-and-forgotten: the log is a "
                  "window, not a ledger." % n)
            return 0

        print(log.summary())
        if args.balance:
            bal = log.reservation_balance()
            neg = {k: v for k, v in bal.items() if v < 0}
            print("\nreservation balance: %d (msoId, lutEntryIndex) key(s), "
                  "%d negative" % (len(bal), len(neg)))
            print("  A negative net is a DeleteReservation whose Reserve is no "
                  "longer on media -- direct evidence the log was truncated.  "
                  "The units are LUT ENTRY INDICES, not LMAs.")
            for k, v in sorted(neg.items())[:args.limit or 20]:
                print("    mso %d entry %d: %+d" % (k[0], k[1], v))
        return 0
    finally:
        close_device(dev)


def cmd_ns(args):
    """The namespaces, and the host-LBA -> media address chain through them."""
    nsmod = _need("namespace", "NamespaceTable", "LOGICAL_PARTITIONS")
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        _ftlmod, ftl = _mount_ftl(args, dev)
        try:
            nst = nsmod.NamespaceTable.from_ftl(ftl)
        except Exception as exc:
            print("NamespaceTable.from_ftl failed: %s: %s"
                  % (type(exc).__name__, exc))
            return 1

        if args.ns_action == "list":
            print(nst.describe() if hasattr(nst, "describe") else repr(nst))
            ns = nst.namespaces()
            if _is_unknown(ns):
                print("\nnamespaces(): UNKNOWN -- there is no mediaSpace "
                      "section here.  There is deliberately NO fallback that "
                      "invents three namespaces from the msoIdTable.")
            return 0

        if args.nsid is None or args.lba is None:
            raise SystemExit("%s needs --nsid N and --lba N" % args.ns_action)
        lp = args.lp
        if args.nsid in nsmod.LOGICAL_PARTITIONS and lp is None:
            print("nsid %d addresses host data as (lpId, LBA).  Without --lp "
                  "the address is half an address, and every answer below will "
                  "be UNKNOWN by construction rather than by accident."
                  % args.nsid)

        if args.ns_action == "placement":
            try:
                p, off = nst.lba_placement(args.nsid, args.lba, lp=lp)
            except Exception as exc:
                print("lba_placement: %s: %s" % (type(exc).__name__, exc))
                return 1
            lma, loff = nst.lba_to_lma(args.nsid, args.lba)
            print("nsid %d lba %d%s -> lma %d + %d bytes"
                  % (args.nsid, args.lba,
                     "" if lp is None else " (lp %d)" % lp, lma, loff))
            print("  placement: %s"
                  % _three_way(p,
                               "%r  (offset %s in the AU)" % (p, off),
                               "the LUT maps this LMA nowhere -- never "
                               "written, trimmed, or an empty partition",
                               "mapped, but not resolvable from what is held "
                               "here (usually the WERU's slot rule or its cell "
                               "mode); this is the normal answer for user data "
                               "on this board and it is CORRECT"))
            return 0

        if args.ns_action == "read":
            try:
                data = nst.read_lba(args.nsid, args.lba, args.count or 1, lp=lp)
            except Exception as exc:
                print("read_lba: %s: %s" % (type(exc).__name__, exc))
                return 1
            if _is_unknown(data):
                print("nsid %d lba %d x%d: UNKNOWN -- not resolvable or not "
                      "readable from this backing.  Nothing is claimed."
                      % (args.nsid, args.lba, args.count or 1))
                return 1
            if data is None:
                print("nsid %d lba %d x%d: nothing backs this address.  A REAL "
                      "NEGATIVE from the drive's own tables, not a read failure."
                      % (args.nsid, args.lba, args.count or 1))
                return 0
            print("nsid %d lba %d x%d: %d bytes, self-checked (the AU's aux "
                  "carries this LMA%s)"
                  % (args.nsid, args.lba, args.count or 1, len(data),
                     " and this lpId" if lp is not None else ""))
            if args.out:
                Path(args.out).write_bytes(data)
                print("  -> %s" % args.out)
            else:
                print(hexdump(data, args.limit or 256))
            return 0
        raise SystemExit("ns: use 'list', 'placement' or 'read'")
    finally:
        close_device(dev)


def cmd_raid(args):
    """Stripes, parity location, and whether a stripe actually checks out."""
    # Every symbol this handler goes on to touch, not a sample of them.  The
    # point of `_need` is one clear sentence instead of a traceback, and it can
    # only give that for names it was told about: `RAID_NAMES` and friends were
    # used below but never declared here, so a layer missing one of those got a
    # bare AttributeError -- the failure mode `_need` exists to prevent.
    raid = _need("raid", "Stripe", "check_page", "wordline_class",
                 "WERU_CONFIG_TABLE", "RAID_NAMES", "WORDLINE_CLASSES",
                 "PARITY_GROUP_ID", "parity_msos", "parity_msos_from_table",
                 "mso_stripe", "mso_parity_wma", "inline_stripe",
                 "inline_parity_of", "stored_layer_ok")
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        if args.raid_action == "info":
            print("RAID types (Mso_GetRaidType): %s"
                  % ", ".join("%d=%s" % (k, v)
                              for k, v in sorted(raid.RAID_NAMES.items())))
            print("\nWERU configuration classes (first, count, blocks, "
                  "blocks/ce, mode):")
            for row in raid.WERU_CONFIG_TABLE:
                print("  %r" % (row,))
            print("\nnative-TLC word-line classes -- MEASURED on this board, "
                  "not a datasheet claim:")
            for rank, name, detail in raid.WORDLINE_CLASSES:
                print("  %d  %-8s %s" % (rank, name, detail))
            print("  A class-3 page is the one the read-retry ladder exists "
                  "for.  Class 2 is BORDERLINE, not dead -- never use these "
                  "ranks to declare a page unreadable without reading it.")
            # Two independent routes to the same set, printed separately so a
            # disagreement is visible rather than averaged: the board's own
            # msoDescriptor (read off this drive) and column 5 of the msoIdTable
            # (the embedded firmware constant).  Either being UNKNOWN is said
            # out loud; the second no longer depends on the caller supplying an
            # image, so a disagreement here is now always a real disagreement.
            print("")
            try:
                _ftlmod, ftl = _mount_ftl(args, dev)
            except Exception as exc:
                print("parity MSOs: UNKNOWN -- no FTL here (%s: %s)"
                      % (type(exc).__name__, exc))
                return 0
            desc = None
            try:
                sec = ftl.section("mso")
                if sec is not None and not _is_unknown(sec):
                    desc = sec.contiguous()
            except Exception:
                desc = None
            print("parity MSOs from this board's msoDescriptor: %s"
                  % (raid.parity_msos(bytes(desc)) if desc else
                     "UNKNOWN (no readable mso section)"))
            tbl = getattr(ftl, "mso_table", None)
            raw = getattr(tbl, "raw", None)
            print("parity MSOs from column %d of the msoIdTable  : %s"
                  % (raid.PARITY_GROUP_ID,
                     raid.parity_msos_from_table(bytes(raw)) if raw else
                     "UNKNOWN -- the mount carries no msoIdTable, which should "
                     "not happen now that it is embedded"))
            return 0

        _ftlmod, ftl = _mount_ftl(args, dev)

        if args.raid_action == "stripe":
            if args.wma is not None:
                st = raid.mso_stripe(ftl, args.wma, mso_id=args.mso)
                print("cRaidTypeMso stripe for wma %d:" % args.wma)
                if st is None:
                    print("  none -- this WMA is not in a stripe this tree can "
                          "name.  A real negative, not a read failure.")
                    return 0
                print("  %r" % (st,))
                pw = raid.mso_parity_wma(ftl, args.wma, mso_id=args.mso)
                print("  parity WMA: %s" % _three_way(
                    pw, pw, "the msoIdTable names no ParityData MSO for this "
                            "type -- and on this board that is true of every "
                            "MSO that owns user WERUs.  NOT a licence to guess",
                    "not derivable from what is mounted here"))
                return 0
            if args.weru is None or args.page is None:
                raise SystemExit("raid stripe needs either --wma N (cRaidTypeMso) "
                                 "or --weru N --page N [--au N] (Inline)")
            cfg = ftl.weru_config(args.weru)
            if cfg is None or _is_unknown(cfg):
                print("weru %s: config UNKNOWN -- no slot map is established "
                      "here, so no stripe can be named." % args.weru)
                return 1
            st = raid.inline_stripe(cfg, args.weru, args.page, args.au or 0)
            if st is None:
                print("no stripe at weru %s page %s au %s (out of range for "
                      "this WERU's geometry)"
                      % (args.weru, args.page, args.au or 0))
                return 1
            print("Inline stripe, weru %s page %d au %d:"
                  % (args.weru, args.page, args.au or 0))
            for slot, m in zip(st.slots, st.members):
                print("  slot %-3s %s" % (slot, m if m is not None else
                                          "UNKNOWN (WMA legal, but no measured "
                                          "slot -> (ce, block) for this WERU: "
                                          "a coverage fact, not a hole)"))
            print("  parity   %s" % (st.parity,))
            okxor, why = raid.stored_layer_ok(cfg)
            print("  XOR over RAW STORED bytes is %s: %s"
                  % ("legitimate" if okxor else "NOT legitimate", why))
            return 0

        if args.raid_action == "parity":
            if None in (args.weru, args.ce, args.block, args.page):
                raise SystemExit("raid parity needs --weru --ce --block --page "
                                 "[--au]")
            cfg = ftl.weru_config(args.weru)
            if cfg is None or _is_unknown(cfg):
                print("weru %s: config UNKNOWN" % args.weru)
                return 1
            p = raid.inline_parity_of(cfg, args.weru, args.ce, args.block,
                                      args.page, args.au or 0)
            print("parity AU covering ce%d b%d p%d au%d of weru %s:"
                  % (args.ce, args.block, args.page, args.au or 0, args.weru))
            print("  %s" % _three_way(
                p, p, "(ce %d, block %d) is not a DATA slot of this WERU -- "
                      "including the case where it IS the parity block, which "
                      "covers nothing" % (args.ce, args.block),
                "no slot map for this WERU"))
            return 0

        if args.raid_action == "check":
            if args.weru is None or args.page is None:
                raise SystemExit("raid check needs --weru N --page N")
            cfg = ftl.weru_config(args.weru)
            if cfg is None or _is_unknown(cfg):
                print("weru %s: config UNKNOWN" % args.weru)
                return 1
            try:
                differing, total = raid.check_page(dev, cfg, args.weru,
                                                   args.page)
            except Exception as exc:
                print("check_page: %s: %s" % (type(exc).__name__, exc))
                return 1
            print("weru %s page %d: %d of %d byte position(s) violate the "
                  "parity relation" % (args.weru, args.page, differing, total))
            if differing == 0:
                print("  the members satisfy it EXACTLY.")
            else:
                print("  a small non-zero count is a raw bit error somewhere "
                      "in the members, NOT a broken model -- re-check with "
                      "rebuild_message, which corrects before XOR-ing.")
            return 0
        raise SystemExit("raid: use 'info', 'stripe', 'parity' or 'check'")
    finally:
        close_device(dev)


def cmd_aux(args):
    """A page's 8-byte-per-AU auxiliary, under the layout that page really uses."""
    auxmod = _need("auxdata", "parse_page", "summary", "LAYOUTS", "layout_for",
                   "LAYOUT_NAMES", "read_is_plausible")
    dev = open_device(args.device, fast_erased=not args.no_fast_erased,
                      verbose=args.verbose)
    try:
        if args.aux_action == "layouts":
            src = None
            try:
                _ftlmod, ftl = _mount_ftl(args, dev)
                src = ftl
            except Exception as exc:
                print("(no msoDescriptor: %s: %s -- the enumeration below is "
                      "still exact; only the per-board assignment is UNKNOWN)"
                      % (type(exc).__name__, exc))
            print(auxmod.summary(src))
            if args.mso is not None and src is not None:
                lay = auxmod.layout_for(args.mso, src)
                print("\nmso %d aux layout: %s" % (args.mso, lay))
            return 0

        if args.aux_action == "page":
            if None in (args.ce, args.block, args.page):
                raise SystemExit("aux page needs --ce N --block N --page N")
            layout = args.layout
            if layout is None and args.mso is not None:
                # Say why --mso did not yield a layout.  This used to be a bare
                # `except Exception: layout = None`, so a failed mount and an
                # MSO whose descriptor genuinely says nothing produced the same
                # "say which aux layout" message -- and the first of those is
                # fixable by the caller while the second is not.
                try:
                    _ftlmod, ftl = _mount_ftl(args, dev)
                except Exception as exc:
                    print("--mso %d could not be resolved: the FTL would not "
                          "mount here (%s: %s), so the msoDescriptor that "
                          "carries the layout was never read"
                          % (args.mso, type(exc).__name__, exc))
                    ftl = None
                if ftl is not None:
                    try:
                        lay = auxmod.layout_for(args.mso, ftl)
                    except Exception as exc:
                        print("--mso %d: layout_for raised %s: %s"
                              % (args.mso, type(exc).__name__, exc))
                        lay = None
                    if _is_unknown(lay):
                        print("--mso %d: this board's msoDescriptor does not "
                              "name a layout for that MSO -- a real UNKNOWN, "
                              "not a failure to look" % args.mso)
                        layout = None
                    else:
                        layout = lay
            if layout is None:
                raise SystemExit(
                    "say which aux layout: --layout N, or --mso N to take it "
                    "from that MSO's descriptor.  There is no default: the "
                    "same 8 bytes mean different things under each layout, and "
                    "guessing one produces a plausible wrong answer")
            codecmod, how = _decoder()
            if codecmod is None:
                raise SystemExit("no page decoder available; aux needs one to "
                                 "get at the AUs")
            raw = dev.read_page(args.ce, args.block, args.page, slc=args.slc)
            row = geom.compose_row(args.block, args.page)
            res = codecmod.decode_page(raw, row)
            auxes = [a.aux for a in res.aus]
            ap = auxmod.parse_page(auxes, layout)
            print("ce%d b%d p%d (%s), aux layout %d = %s   [decoded via %s]"
                  % (args.ce, args.block, args.page,
                     "pseudo-SLC" if args.slc else "native-TLC", layout,
                     auxmod.LAYOUT_NAMES.get(layout) or "<unnamed>", how))
            print("  MCRC %d/%d, ECC %d/%d"
                  % (sum(1 for a in res.aus if a.mcrc_ok), len(res.aus),
                     sum(1 for a in res.aus if a.ecc_ok), len(res.aus)))
            for i, e in enumerate(ap.entries):
                print("  au%d  %s  lma=%-12s lp=%-8s parity=%s"
                      % (i, bytes(e.raw).hex(" "), e.lma, e.lp_id,
                         e.inline_parity))
            # `sequence_alt` is only meaningful where `sequence` came back
            # UNKNOWN: that is the case where the layout admits two readings and
            # they disagreed, and printing the loser beside a settled answer
            # would suggest a doubt that does not exist.
            alt = ap.sequence_alt
            if _is_unknown(ap.sequence) and alt is not None \
                    and not _is_unknown(alt):
                print("  page sequence : UNKNOWN -- the layout's two readings "
                      "disagree; the other one is %s.  Neither is claimed."
                      % (alt,))
            else:
                print("  page sequence : %s" % (ap.sequence,))
            print("  stripe id     : %s" % (ap.stripe_id,))
            print("  torn          : %s" % (ap.torn,))
            cons = getattr(ap, "lmas_consecutive", None)
            print("  LMAs consecutive: %s" % (cons,))
            print("  accepted as a clean read: %s"
                  % (auxmod.read_is_plausible(auxes, layout),))
            print("  MCRC 4/4 ALONE IS NOT A GOOD READ -- it is computed over "
                  "the complemented STORED bytes, before descrambling.  The "
                  "working test is MCRC 4/4 AND the address crib THIS LAYOUT "
                  "defines (aux.read_is_plausible).  'Four consecutive LMAs' "
                  "is a layout-0/1/2 rule and rejects every layout-3 parity "
                  "page, none of which carries an LMA array at all.")
            for n in (ap.notes or ()):
                print("  note: %s" % n)
            return 0
        raise SystemExit("aux: use 'layouts' or 'page'")
    finally:
        close_device(dev)


def cmd_selftest(args):
    """Run every layer's own selftest, reporting SKIPPED honestly."""
    import subprocess
    here = Path(__file__).resolve().parent
    mods = args.modules or ["dumpdev", "hwdev", "codec", "fw", "ftl",
                            "verdict", "extract"]
    rc = 0
    for name in mods:
        p = here / (name + ".py")
        if not p.exists():
            print("== %-8s SKIPPED (not written yet)" % name)
            continue
        extra = ["--offline"] if (name == "hwdev" and args.offline) else []
        print("== %s" % name)
        # `-m titania.<name>`, not the file path.  Run by path, a module using
        # `from . import x` has no parent package and dies with "attempted
        # relative import with no known parent package" -- which `ftl`,
        # `trim` and `namespace` all do, and `ftl` is in the default list
        # above.  `titania selftest` was therefore reporting a failure for a
        # layer whose own selftest passes.
        r = subprocess.run([sys.executable, "-m", "%s.%s" % (_PKG, name),
                            "--selftest"] + extra, cwd=str(here.parent))
        rc |= (r.returncode != 0)
    return rc


# --------------------------------------------------------------------- main --
def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        # `or ""`: `py -OO` strips docstrings, and a CLI that cannot build its
        # own parser under -oo is a real (if rare) AttributeError.
        prog="titania", description=(__doc__ or "").split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="--device dump:<path> reads files; --device hw reads the board.")
    p.add_argument("--device", default=None,
                   help="dump:<path>, dump (discover one), or hw.  Default: "
                        "$TITANIA_DUMP, else a dump found beside the CWD or "
                        "the checkout")
    # There is no `--firmware` here any more.  The msoIdTable is a
    # firmware constant, not per-drive data, and it is embedded in titania.ftl
    # (`MSOID_TABLE_EMBEDDED`, fw 09P07M3).  While it had to be supplied, a
    # checkout with no image printed `msoIdTable <unknown>` and "63 populated, 0
    # named", which reads like a limitation of the drive.  `main` still
    # recognises the flag and says where it went -- see `_firmware_flag_gone`.
    p.add_argument("--no-fast-erased", action="store_true",
                   help="never synthesise an erased page; transfer every byte")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        """Accept the global options after the subcommand too.

        argparse insists a parent option precedes the subcommand, which is a
        surprising thing to be told off for. `SUPPRESS` means an unused copy
        leaves the parent's value alone rather than resetting it to a default.
        """
        sp.add_argument("--device", default=argparse.SUPPRESS)
        sp.add_argument("--no-fast-erased", action="store_true",
                        default=argparse.SUPPRESS)
        sp.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS)
        sp.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS)

    def ftl_opts(sp):
        """The input an FTL mount takes, in one place.

        It used to be two.  `--firmware` is gone: the msoIdTable it carried is a
        firmware constant, is embedded in `titania.ftl`, and needs no file.  Its
        removal also removes the trap it came with -- a subparser option with an
        ordinary default silently overwrites the parent's value once the verb is
        seen, so five subcommands used to reset `titania --firmware FW.bin trim`
        back to None.  `_flag_audit` still enforces the SUPPRESS rule for every
        remaining flag.

        `--ftl-cache` stays here because `_mount_ftl` reads it with
        `getattr(args, "ftl_cache", None)`, which cannot tell a subcommand that
        never declared it from one that declared it and was given nothing.
        """
        sp.add_argument("--ftl-cache", default=".ftlcache",
                        help="directory for the mount's scan cache, shared "
                             "with the other FTL commands")

    def addr(sp, pages=True):
        common(sp)
        sp.add_argument("--ce", type=lambda x: int(x, 0))
        sp.add_argument("--ces", help="e.g. 6,7 or 6-11")
        sp.add_argument("--block", type=lambda x: int(x, 0))
        sp.add_argument("--blocks", help="e.g. 6-9")
        if pages:
            sp.add_argument("--page", type=lambda x: int(x, 0))
            sp.add_argument("--pages", help="e.g. 0-367 (default: whole block)")
        sp.add_argument("--slc", action="store_true",
                        help="pseudo-SLC, 368 pages/block (the firmware area). "
                             "Without it, native TLC, 1104 pages/block.")

    sp = sub.add_parser("info", help="summarise the device and the layers")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("read", help="read pages, raw or decoded")
    addr(sp)
    sp.add_argument("-o", "--out", help="file, or a directory (trailing /)")
    sp.add_argument("--decode", action="store_true")
    sp.add_argument("--group2", action="store_true",
                    help="format B (firmware images) instead of format A")
    sp.add_argument("--limit", type=int, default=256)
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("scan", help="EXACT erased/data verdict per page, "
                                     "computed device-side")
    addr(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("decode", help="decode a raw page file")
    common(sp)
    sp.add_argument("file")
    sp.add_argument("--row", type=lambda x: int(x, 0))
    sp.add_argument("--group2", action="store_true")
    sp.add_argument("--limit", type=int, default=256)
    sp.set_defaults(func=cmd_decode)

    sp = sub.add_parser("verify", help="MCRC/ECC over a range; --against "
                                       "cross-checks two backings by CRC")
    addr(sp)
    sp.add_argument("--against", help="a second --device spec to compare with")
    sp.add_argument("--crc-only", action="store_true")
    sp.add_argument("--group2", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("dump", help="bulk read to disk")
    addr(sp)
    sp.add_argument("-o", "--out", default="titania_dump")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser(
        "capture", help="acquire from the BOARD: cell-mode aware, "
                        "format-aware, ECC-gated read retry",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
`dump` extracts pages from a device you already have.  `capture` ACQUIRES them
from the board, and differs from a plain sweep in four ways that each exist
because the plain sweep got something wrong:

  * the cell mode is chosen per block from the cell map and LATCHED EXPLICITLY,
    so a native-TLC block is never read in a pseudo-SLC latch -- which makes a
    written page come back all-0xFF and be recorded as erased media;
  * only blocks that may hold data are read in full, by the UNION of a page-0
    probe (asks the media) and the FTL's allocated WERUs (asks the firmware),
    because each misses what the other catches;
  * every page is judged by `titania.verdict`, which knows that block 0 on the
    group-2 dies has its own ECC geometry and is not a failure;
  * a page that does not decode is re-read up the firmware's own retry ladder
    until it does, and the level it needed goes in its source name.

READ-ONLY.  No program or erase opcode is reachable, and the die's read
references are restored to level 0 and verified on every exit path.

  titania capture --ce 1,6 --out mydump --cellmap cellmap.json
  titania capture --ce 1 --out mydump --cellmap cellmap.json --max-level 0
""")
    sp.add_argument("--ce", required=True, help="comma-separated CE numbers")
    sp.add_argument("-o", "--out", required=True, help="store directory")
    sp.add_argument("--cellmap", required=True,
                    help="JSON {ce: {block: 'slc'|'tlc'}}")
    sp.add_argument("--weru-table", default=None,
                    help="weru_table.json, the metadata half of selection")
    sp.add_argument("--max-level", type=int, default=None,
                    help="highest retry level (0 disables retry entirely)")
    sp.add_argument("--no-correct", action="store_true",
                    help="judge on uncorrected MCRC only -- faster, and spends "
                         "ladder levels on pages the ECC would have fixed")
    sp.add_argument("--lun", type=int, default=0)
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("ftl", help="storage-layer summary")
    common(sp)
    ftl_opts(sp)
    sp.set_defaults(func=cmd_ftl)

    sp = sub.add_parser(
        "fw", help="firmware images; and verify-table, the msoIdTable check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
verify-table <image> compares the msoIdTable EMBEDDED in titania (a firmware
constant -- 126 bytes, 31 entries, fw 09P07M3) with the one in a firmware image
of your own.  It reads no device.  Exit 0 = identical, 1 = differs (each
differing (msoType, msoGroupId) is printed with both msoIds), 2 = that image
carries no table.  This is where an image is compared now; there is no
--firmware flag on the other commands any more, because none is needed.
""")
    common(sp)
    sp.add_argument("fw_action", choices=["list", "extract", "verify-table"])
    sp.add_argument("fw_image", nargs="?",
                    help="verify-table: the firmware image to compare against "
                         "the embedded msoIdTable")
    sp.add_argument("--fw-id", type=lambda x: int(x, 0))
    sp.add_argument("--on-media", action="store_true",
                    help="group 2 (fw_id 1, 2): write the image exactly as "
                         "stored -- header row plus payload, NOT decrypted. "
                         "The default decrypts and drops the header")
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_fw)

    sp = sub.add_parser(
        "extract", help="pull an MSO's LMAs off the media: verified, "
                        "resumable, with provenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Every AU is checked by its own aux LMA against the LMA that was looked up --
that is what proves the whole chain, not that a page decoded.  Bytes reach the
file only when it matches.

Resume is automatic: the manifest beside the output records one line per LMA
and a re-run with the same arguments continues from what is already accounted
for.  --restart discards it; --retry also revisits LMAs that were unresolved,
unreadable, erased or failed.

Gaps are gaps.  In the default sparse layout a missing LMA is a HOLE, which
reads back as zeros that belong to the filesystem and not to the drive; only
the manifest tells them apart.  Use --layout packed for a file with nothing
invented in it at all, or --fill to synthesise gaps on purpose (every
synthesised LMA is marked in the manifest).

READ RETRY is on by default where the backing has a ladder.  A third of every
native-TLC block -- the (page-8)%3==2 upper word lines -- does not decode at the
die's default read references and is NOT damaged; the shortlist 1,6,15 re-reads
at shifted references and the winning level is recorded per LMA as "rty".  The
verification bar is unchanged: a rescued page still only becomes bytes when its
aux LMA matches, and a page that decodes at no level is still withheld.

  titania extract --mso 32 --lma 1149856:1149872 -o u.bin
  titania extract --device hw --mso 32 --lma 1149856+256 -o u.bin --progress 16
  titania extract --mso 32 --all-mapped --sample 4096 \\
                  --survey --manifest mso32.survey.jsonl
""")
    common(sp)
    sp.add_argument("--mso", type=lambda x: int(x, 0),
                    help="the MSO whose LMA space to read")
    sp.add_argument("--lma", help="A:B (half-open), A-B, A+N, or A")
    sp.add_argument("--all-mapped", "--span", dest="all_mapped",
                    action="store_true",
                    help="the MSO's whole LMA SPAN, mapped or not (use with "
                         "--count or --sample).  A sparse MSO is mostly "
                         "unmapped, so a --sample of the span is mostly real "
                         "negatives -- that is the media, not a failure")
    sp.add_argument("--weru", type=lambda x: int(x, 0),
                    help="keep only LMAs whose WMA lands in this WERU")
    sp.add_argument("--count", type=int, help="stop after N selected LMAs")
    sp.add_argument("--sample", type=int,
                    help="take N evenly spaced LMAs from the span")
    sp.add_argument("-o", "--out", help="output file")
    sp.add_argument("--manifest", help="default: <out>.manifest.jsonl")
    sp.add_argument("--layout", choices=["sparse", "packed"], default="sparse")
    sp.add_argument("--fill", default="none",
                    help="none (default) | 0x00 | 0xFF -- synthesise gaps, "
                         "recorded as synthesised in the manifest")
    sp.add_argument("--restart", action="store_true",
                    help="discard an existing manifest instead of resuming")
    sp.add_argument("--retry", action="store_true",
                    help="RESUME policy: also revisit LMAs previously recorded "
                         "unresolved / unreadable / erased / failed.  Nothing "
                         "to do with --retry-levels, which is the die's ladder")
    sp.add_argument("--no-read-retry", action="store_true",
                    help="never change the die's read reference voltages; a "
                         "page that does not decode at the defaults is simply "
                         "withheld.  Costs the upper word lines: a third of "
                         "every native-TLC block does not decode at level 0")
    sp.add_argument("--retry-levels", default=None,
                    help="read-retry ladder steps to try after the defaults: "
                         "'1,6,15' (default), 'all' for the device's whole "
                         "ladder, or 'none'.  Level 1 has rescued 113 of 113 "
                         "upper pages measured, so the default is a shortlist "
                         "and not the full 32 steps")
    sp.add_argument("--survey", action="store_true",
                    help="resolve only: no page reads, no data output")
    sp.add_argument("--page-cache", type=int, default=64,
                    help="decoded pages to keep (4 LMAs share a page)")
    sp.add_argument("--progress", type=int, default=0,
                    help="print a rate/ETA line every N LMAs")
    sp.add_argument("--ftl-device", help="mount the FTL from a different "
                                         "backing than the one read")
    ftl_opts(sp)
    sp.add_argument("--no-canary", action="store_true",
                    help="hardware: skip the known-good page bracket "
                         "(NOT advised -- it is the only check on a "
                         "report-list fault)")
    sp.add_argument("--canary-every", type=int, default=64,
                    help="hardware: re-read the canary every N LMAs")
    sp.set_defaults(func=cmd_extract)

    # -- the four analysis layers --------------------------------------
    sp = sub.add_parser(
        "trim", help="what the host discarded, and what the LUT cannot say",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
A trimmed LMA's LUT entry is 0xFFFFFFFF -- BYTE-IDENTICAL to one that was never
written.  There is no distinct trimmed sentinel, so "mapped user data" cannot be
split into live-versus-discarded by reading the LUT; only the trim log names the
difference, and only for as long as its entries survive.  UNKNOWN is therefore
the common and CORRECT answer here.

  titania trim
  titania trim --mso 34
  titania trim --mso 34 --lma 198720
""")
    common(sp)
    sp.add_argument("--mso", type=lambda x: int(x, 0),
                    help="restrict to one MSO's Trim ranges")
    sp.add_argument("--lma", type=lambda x: int(x, 0),
                    help="with --mso: is this LMA trimmed / live / UNKNOWN")
    sp.add_argument("--balance", action="store_true",
                    help="Reserve vs DeleteReservation net, per LUT entry: a "
                         "negative net is evidence the log was truncated")
    sp.add_argument("--limit", type=int, default=40)
    ftl_opts(sp)
    sp.set_defaults(func=cmd_trim)

    sp = sub.add_parser(
        "ns", help="namespaces: LBA -> LMA -> media address, and read a sector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
NSID 2 and 3 address host data as (lpId, LBA).  An LBA alone is half an address
there, so --lp is REQUIRED and its absence yields UNKNOWN rather than a
plausible number.

  titania ns list      
  titania ns placement  --nsid 1 --lba 8192
  titania ns read       --nsid 1 --lba 8192 --count 8
""")
    common(sp)
    sp.add_argument("ns_action", nargs="?", default="list",
                    choices=["list", "placement", "read"])
    sp.add_argument("--nsid", type=lambda x: int(x, 0))
    sp.add_argument("--lba", type=lambda x: int(x, 0))
    sp.add_argument("--lp", type=lambda x: int(x, 0),
                    help="logical partition id -- REQUIRED for nsid 2 and 3")
    sp.add_argument("--count", type=int, default=1, help="512-byte sectors")
    sp.add_argument("-o", "--out")
    sp.add_argument("--limit", type=int, default=256)
    ftl_opts(sp)
    sp.set_defaults(func=cmd_ns)

    sp = sub.add_parser(
        "raid", help="stripes, parity location, and whether a stripe checks out",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Two RAID types and they are not interchangeable.  cRaidTypeInline puts the
parity in one BLOCK of the same WERU: address it with --weru/--page/--au.
cRaidTypeMso puts it in a different MSO entirely: address it with --wma.

`info` also prints the native-TLC word-line classes, which is where the
read-retry ladder comes from: class 3 ("upper", (page-8)%3==2) never decodes at
the die's default references and is a third of every block.

  titania raid info   
  titania raid stripe  --weru 26 --page 44 --au 0
  titania raid parity  --weru 26 --ce 1 --block 26 --page 44
  titania raid check   --weru 26 --page 44
""")
    common(sp)
    sp.add_argument("raid_action", nargs="?", default="info",
                    choices=["info", "stripe", "parity", "check"])
    sp.add_argument("--weru", type=lambda x: int(x, 0))
    sp.add_argument("--wma", type=lambda x: int(x, 0),
                    help="cRaidTypeMso: the data WMA whose stripe to name")
    sp.add_argument("--mso", type=lambda x: int(x, 0))
    sp.add_argument("--ce", type=lambda x: int(x, 0))
    sp.add_argument("--block", type=lambda x: int(x, 0))
    sp.add_argument("--page", type=lambda x: int(x, 0))
    sp.add_argument("--au", type=lambda x: int(x, 0))
    ftl_opts(sp)
    sp.set_defaults(func=cmd_raid)

    sp = sub.add_parser(
        "aux", help="parse a page's AU auxiliary, and report the layouts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The same 8 bytes mean different things under each of the four layouts, so there
is no default: give --layout N, or --mso N to take it from that MSO's own
msoDescriptor.  A field the chosen layout does not define comes back UNKNOWN,
never as a plausible value borrowed from a different layout.

  titania aux layouts
  titania aux page    --ce 6 --block 8 --page 0 --slc \\
                      --layout 0
""")
    common(sp)
    sp.add_argument("aux_action", nargs="?", default="layouts",
                    choices=["layouts", "page"])
    sp.add_argument("--ce", type=lambda x: int(x, 0))
    sp.add_argument("--block", type=lambda x: int(x, 0))
    sp.add_argument("--page", type=lambda x: int(x, 0))
    sp.add_argument("--slc", action="store_true")
    sp.add_argument("--layout", type=lambda x: int(x, 0),
                    help="0, 1, 2 or 3 -- the firmware's own switch arms")
    sp.add_argument("--mso", type=lambda x: int(x, 0),
                    help="take the layout from this MSO's msoDescriptor")
    ftl_opts(sp)
    sp.set_defaults(func=cmd_aux)

    sp = sub.add_parser("selftest", help="run every layer's own selftest")
    sp.add_argument("modules", nargs="*")
    sp.add_argument("--offline", action="store_true",
                    help="skip hwdev's live-board checks")
    sp.set_defaults(func=cmd_selftest)
    return p


#: The flag that was removed, and what to say about it.  Deleting an option
#: argparse used to accept turns every script and every set of fingers that
#: carries it into `error: unrecognized arguments: --firmware ...`, which tells
#: the user nothing about where the capability went.  This is not a hidden alias
#: -- the flag does not work; it is a signpost with an exit code.
_FIRMWARE_GONE = (
    "--firmware has been REMOVED, and nothing is missing as a result.\n"
    "\n"
    "It existed to supply the msoIdTable, which is not on the media.  That\n"
    "table is a firmware CONSTANT, not per-drive data, so it is now built in\n"
    "(titania.ftl.MSOID_TABLE_EMBEDDED -- 126 bytes, 31 entries, from\n"
    "%s at %#x, firmware %s).  Every command names its MSOs\n"
    "with no image at all; drop the flag and re-run.\n"
    "\n"
    "To check YOUR image against the built-in table:\n"
    "    titania fw verify-table %s\n"
    "It prints any differing (msoType, msoGroupId) entries and exits non-zero\n"
    "if they disagree, so it is usable from a script -- which is what passing\n"
    "an image to every command silently was not.")


def _firmware_flag_gone(argv):
    """-> the message if `--firmware` is in `argv`, else None.

    Matched before parsing, because argparse would otherwise answer with a bare
    `unrecognized arguments` and a usage block that does not mention the table.
    """
    for i, a in enumerate(argv):
        if a == "--firmware" or a.startswith("--firmware="):
            given = (a.split("=", 1)[1] if "=" in a
                     else (argv[i + 1] if i + 1 < len(argv) else "<image>"))
            try:
                from . import ftl as _ftl
                src, off, ver = (_ftl.MSOID_TABLE_FW_IMAGE,
                                 _ftl.MSOID_TABLE_HINT,
                                 _ftl.MSOID_TABLE_FW_VERSION)
            except Exception:               # ftl unimportable: still answer
                src, off, ver = "dumps/fw.sram.efc.bin", 0x175049, "09P07M3"
            return _FIRMWARE_GONE % (src, off, ver,
                                     given if not given.startswith("-")
                                     else "<image>")
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    gone = _firmware_flag_gone(argv)
    if gone:
        print(gone, file=sys.stderr)
        return 2
    if "--selftest" in argv and (not argv or argv[0].startswith("-")):
        argv = ["selftest"] + [a for a in argv if a != "--selftest"]
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        p.print_help()
        return 0
    for name in ("ce", "ces", "block", "blocks", "page", "pages", "slc",
                 "group2", "quiet", "no_fast_erased", "limit"):
        if not hasattr(args, name):
            setattr(args, name, None if name not in ("slc", "group2", "quiet",
                                                     "no_fast_erased") else False)
    try:
        return args.func(args)
    except (DeviceError, WriteProtected) as exc:
        print("error: %s" % exc)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


# ------------------------------------------------------------------ selftest --
def _subcommands(p):
    """-> {name: subparser}.  Was `p._subparsers._group_actions[0].choices`,
    which is three optional attributes deep and raises on a parser with no
    subcommands."""
    import argparse
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return dict(a.choices or {})
    return {}


def _flag_audit(p):
    """-> list of complaints.  The regression test for this class of bug.

    Two faults, both invisible at runtime and both found in the wild:

      * a subcommand that redeclares a global option without
        `argparse.SUPPRESS` silently resets it, so `titania --firmware FW trim`
        arrived as `firmware=None`;
      * `_mount_ftl` reads `--ftl-cache` through `getattr`, which cannot tell
        "declared and not given" from "never declared", so a subcommand that
        mounts an FTL without declaring it drops it.

    `--firmware` is the flag both faults were found on, and it is gone: the
    msoIdTable it carried is embedded now.  The audit is not -- `--ftl-cache`
    has exactly the same shape, and the next flag added will too.
    """
    import argparse
    import ast
    bad = []
    gdests = {a.dest for a in p._actions if a.dest != "help"}
    # Which handlers reach `_mount_ftl` -- read off this file rather than kept
    # as a list, so a new mounting command is covered the day it is written and
    # not the day someone remembers to add it here.
    tree = ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))
    body = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    mounts = set()
    for name, node in body.items():
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        if "_mount_ftl" in called:
            mounts.add(name)
    for _ in range(3):                       # transitive: cmd_extract-> _open_ftl
        for name, node in body.items():
            called = {c.func.id for c in ast.walk(node)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            if called & mounts:
                mounts.add(name)
    for name, sp in sorted(_subcommands(p).items()):
        dests = {}
        for a in sp._actions:
            if a.dest == "help":
                continue
            dests[a.dest] = a
            if a.dest in gdests and a.default is not argparse.SUPPRESS:
                bad.append("%s: --%s resets the global one to %r"
                           % (name, a.dest.replace("_", "-"), a.default))
        fn = getattr(sp.get_default("func"), "__name__", "")
        if fn in mounts:
            for need in ("ftl_cache",):
                if need not in dests:
                    bad.append("%s mounts an FTL but does not declare --%s"
                               % (name, need.replace("_", "-")))
    return bad


def _selftest():
    """Exercise the CLI itself: parsing, degradation, and one real command."""
    ok = True
    print("titania.cli -- selftest\n")

    got = parse_range("6-9,20,0x10")
    print("[1] parse_range('6-9,20,0x10') = %s" % got)
    ok &= got == [6, 7, 8, 9, 16, 20]
    try:
        parse_range("500", 0, 367)
        print("    out-of-range accepted -- WRONG")
        ok = False
    except SystemExit:
        print("    out-of-range rejected")

    p = build_parser()
    cmds = sorted(_subcommands(p))
    print("[2] commands: %s" % " ".join(cmds))
    ok &= {"info", "read", "scan", "decode", "verify", "dump", "ftl", "fw",
           "extract", "trim", "ns", "raid", "aux", "selftest"} <= set(cmds)

    # 3. every layer is imported lazily -- none of them at module import time
    import importlib
    stale = [m for m in ("titania.ftl", "titania.fw") if m in sys.modules]
    print("[3] layers imported by `import titania.cli`: %s" % (stale or "none"))
    ok &= not stale

    # 4. a missing layer degrades to a message, not a crash
    import types
    saved = sys.modules.get("titania.nosuch")
    try:
        _need("nosuch_layer")
        print("[4] _need on a missing layer did not exit -- WRONG")
        ok = False
    except SystemExit as exc:
        print("[4] missing layer -> %s" % str(exc).splitlines()[0])
    _ = saved, types, importlib

    # 5. a real end-to-end command against the dump, if one is here
    root = _sib("config").dump_path("slc")
    if root is None or not root.exists():
        print("[5] no <dump>/slc -- end-to-end command SKIPPED")
    else:
        DumpDevice = _sib("dumpdev").DumpDevice
        d = DumpDevice(root)
        key = sorted(d.entries)[0]
        rc = main(["--device", "dump:%s" % root, "scan", "--ce", str(key[0]),
                   "--block", str(key[1]), "--page", str(key[2]), "--slc",
                   "-q"])
        print("[5] `scan` over the dump returned %d" % rc)
        ok &= rc == 0
        # crc-only against the same dump: the identity check, which is the one
        # case where the expected answer is known without reading anything
        rc = main(["--device", "dump:%s" % root, "verify", "--ce", str(key[0]),
                   "--block", str(key[1]), "--page", str(key[2]), "--slc",
                   "--crc-only", "--against", "dump:%s" % root, "-q"])
        print("[6] `verify --crc-only --against <itself>` returned %d" % rc)
        ok &= rc == 0
        rc = main(["--device", "dump:%s" % root, "verify", "--ce", str(key[0]),
                   "--block", str(key[1]), "--page", str(key[2]), "--slc", "-q"])
        print("[6b] `verify` (MCRC/ECC) returned %d" % rc)
        ok &= rc == 0

    # 7. the decoder resolution path
    codec, how = _decoder()
    print("[7] decoder: %s" % (how or "NONE -- decode/verify will say so"))

    # 8. the four analysis subcommands parse, default sensibly, and refuse a
    #    half address rather than filling it in
    p = build_parser()
    checks = [
        (["trim"], "func", cmd_trim),
        (["ns"], "ns_action", "list"),
        (["raid"], "raid_action", "info"),
        (["aux"], "aux_action", "layouts"),
        (["ns", "read", "--nsid", "1", "--lba", "0"], "count", 1),
        (["raid", "stripe", "--weru", "26", "--page", "44"], "weru", 26),
        (["aux", "page", "--layout", "3"], "layout", 3),
    ]
    bad = []
    for argv, attr, want in checks:
        try:
            a = p.parse_args(argv)
        except SystemExit:
            bad.append("%s did not parse" % " ".join(argv))
            continue
        if getattr(a, attr, None) != want:
            bad.append("%s: %s=%r, expected %r"
                       % (" ".join(argv), attr, getattr(a, attr, None), want))
    print("[8] trim/ns/raid/aux parse and default: %s" % (bad or "ok"))
    ok &= not bad

    # 9. read-retry reaches the extractor as two separate knobs, and --retry
    #    (the resume policy) is still its own unrelated flag
    a = p.parse_args(["extract", "--mso", "32", "--lma", "0:1", "-o", "x.bin"])
    good = (a.no_read_retry is False and a.retry_levels is None
            and a.retry is False)
    a2 = p.parse_args(["extract", "--mso", "32", "--lma", "0:1", "-o", "x.bin",
                       "--no-read-retry", "--retry-levels", "1,6"])
    good &= (a2.no_read_retry is True and a2.retry_levels == "1,6"
             and a2.retry is False)
    print("[9] extract --no-read-retry / --retry-levels are distinct from the "
          "--retry resume flag: %s" % ("ok" if good else "WRONG"))
    ok &= good
    exmod, err = _load("extract")
    if exmod is None:
        print("    titania.extract unavailable (%s) -- shortlist check SKIPPED"
              % err)
    else:
        # (6, 1, 4) since 2026-08-07 -- measured 1.000 reads/page against
        # 1.528 for (1, 6, 4); step 15 has never won a page.  Step 1 must stay
        # second, not be dropped: one page misses 6 and wins at 1.
        good = (exmod.RETRY_SHORTLIST == (6, 1, 4)
                and exmod.parse_retry_levels("1,6") == (1, 6)
                and exmod.parse_retry_levels("none") == ())
        print("    shortlist %s, parse_retry_levels: %s"
              % (list(exmod.RETRY_SHORTLIST), "ok" if good else "WRONG"))
        ok &= good

    # 10. an input the CLI accepts must reach the thing that uses it.  Both
    #     halves of the bug that motivated this check were invisible at
    #     runtime: `titania --firmware FW ftl` parsed fine and printed
    #     `msoIdTable <unknown>` with 63 MSOs "not named".  That flag is gone
    #     now (10b), but the audit is about the shape, not that one flag.
    p = build_parser()
    bad = _flag_audit(p)
    print("[10] every subcommand's flags reach a reader: %s" % (bad or "ok"))
    ok &= not bad

    # 10b. `--firmware` is gone, and a removal is only done when the removed
    #      spelling still answers.  Three things, all of them failure modes
    #      seen on other tools: no subparser may still declare it (that would
    #      be a flag accepted and ignored); every verb that carries it must
    #      exit non-zero with the signpost rather than argparse's bare
    #      "unrecognized arguments"; and `--firmware=PATH` must be caught too.
    declared = sorted(n for n, spx in _subcommands(p).items()
                      if any(a.dest == "firmware" for a in spx._actions))
    declared += ["<global>"] if any(a.dest == "firmware"
                                    for a in p._actions) else []
    stray = []
    for verb in sorted(_subcommands(p)):
        argv = ["--firmware", "FW.bin", verb]
        argv += {"decode": ["f.bin"], "fw": ["list"]}.get(verb, [])
        try:
            rc = main(argv)
        except SystemExit as e:
            rc = e.code
        if rc == 0 or rc is None:
            stray.append(verb)
    rc_eq = main(["--firmware=FW.bin", "ftl"])
    good = not declared and not stray and rc_eq == 2
    print("[10b] --firmware removed: declared by %s; %d verb(s) let it through: "
          "%s; `--firmware=PATH` -> rc %s (want 2): %s"
          % (declared or "nothing", len(stray), stray or "none", rc_eq,
             "ok" if good else "WRONG"))
    ok &= good

    # 10c. exactly one place in this file mounts an FTL.  A second copy of
    #      `Ftl.from_device` is how `--firmware` got dropped twice.
    import re
    src = Path(__file__).resolve().read_text(encoding="utf-8")
    # `(?<!["'])` so this line and the message below -- which quote the name --
    # are not themselves counted as call sites.
    pat = re.compile(r"""(?<!["'])\bFtl\.from_device\(""")
    sites = [i for i, ln in enumerate(src.splitlines(), 1)
             if pat.search(ln) and not ln.lstrip().startswith("#")]
    print("[10c] Ftl.from_device call sites: %s (must be exactly one, in "
          "_mount_ftl)" % sites)
    ok &= len(sites) == 1

    # 10d. the reason the flag could go: the table is embedded and valid with
    #      no input at all.  A checkout with no firmware image anywhere must
    #      still name every static MSO -- that is the offline use case.
    ftlmod, err = _load("ftl")
    if ftlmod is None:
        print("[10d] titania.ftl unavailable (%s) -- embedded table SKIPPED"
              % err)
    else:
        t = ftlmod.embedded_mso_table()
        good = (t.n_entries == 31
                and t.name(41) == "SystemMetadata/System"
                and len(t.raw) == ftlmod.MSOID_TABLE_LEN)
        print("[10d] embedded msoIdTable (fw %s): %d entries, 41 = %s: %s"
              % (ftlmod.MSOID_TABLE_FW_VERSION, t.n_entries, t.name(41),
                 "ok" if good else "WRONG"))
        ok &= good

    # 10e. `fw verify-table` is where an image is compared now, so it must
    #      actually agree with the image the constant was pasted from -- and
    #      it must exit 0.  Same assertion as titania.ftl's selftest row, made
    #      through the command a script would call.
    fwimg = Path(__file__).resolve().parent.parent / "dumps" / "fw.sram.efc.bin"
    if not fwimg.exists():
        print("[10e] `fw verify-table` SKIPPED -- no %s" % fwimg.name)
    else:
        import contextlib
        import io
        buf = io.StringIO()
        refused = False
        with contextlib.redirect_stdout(buf):
            rc = main(["fw", "verify-table", str(fwimg)])
            try:
                main(["fw", "verify-table"])
            except SystemExit:
                refused = True
        agreed = "AGREE:" in buf.getvalue()
        good = rc == 0 and agreed and refused
        print("[10e] `fw verify-table %s` -> rc %s, said AGREE %s; with no "
              "image it refuses %s: %s"
              % (fwimg.name, rc, agreed, refused, "ok" if good else "WRONG"))
        ok &= good

    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:] and len(sys.argv) == 2:
        sys.exit(_selftest())
    sys.exit(main())
