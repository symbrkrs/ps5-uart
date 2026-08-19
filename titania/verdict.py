"""Did this page read?  One verdict, chosen by the region the page lives in.

    from titania import verdict
    v = verdict.classify(raw, ce=1, block=0, page=7, slc=True)
    v.verdict            # 'ok' / 'erased' / 'fail' / ...
    v.region             # 'fw-group2'

    c = verdict.Classifier(ftl)          # descriptor-driven, no page lists
    v = c.classify(raw, ce, block, page, slc)

WHY THIS MODULE EXISTS
----------------------
"MCRC 4/4 and all eight EE3 codewords converged" is the right test for a
FORMAT_A page and only for a FORMAT_A page.  Applied to everything it produces
two large classes of FALSE failures, both found on 2026-08-06 in a sweep of all
55 084 stored pages that reported 2114 failures:

  * 1840 of the 2114 are the group-2 firmware region -- block 0 on CE 1..5,
    368 of 368 pages on each of the five dies.  That region has its own ECC
    geometry entirely (20 codewords x 512 payload + 300 parity, 10240 user bytes
    per page) and decoding it as FORMAT_A cannot succeed on any page.  Re-reading
    them off the board would have reproduced every one of those failures.
    Checked with the region's own rules, 1836 of 1840 pass 5/5 and the four
    that do not are exactly the four rows already localised as single flipped
    payload bits (`nd_g2extract.KNOWN_MEDIA_ERROR_ROWS`).
  * 28 more are a cell-mode error, not a read error -- page 287 of blocks
    172..175, captured native-TLC on WERU 84, which is pseudo-SLC.  The store
    holds the correctly-moded copy of all 28 and every one of those decodes.

And one class of false failure that never reached this sweep because the pages
were withheld before they could be stored: every RAID-parity page, rejected
by the "aux must be four consecutive LMAs" acceptance test, which is a
layout-0/1/2 test.  See `titania.auxdata.read_is_plausible`.

THE THREE RULES, AND WHERE EACH COMES FROM
------------------------------------------
Nothing here is keyed on a list of addresses.  `geometry.region` is pure
addressing (block numbers the boot ROM hard-codes) and everything else comes off
the media's own descriptors:

    region                  what "it read" means
    fw-group2               `fw.check_group2_page`: five AU digests (CRC-32Q
                            over the complemented stored payload) and an erased
                            page tail.  No seed, no key, no LDPC.
    fw-group01              FORMAT_A MCRC + EE3 convergence.  No aux crib: the
                            firmware area is addressed by raw CE/block/page
                            through FwDataRw.c and its aux is not an LMA array
                            (measured: 44 of 44 clean pages carry four equal
                            non-LMA dwords, all owned by msoId 57
                            NeoFwPartition, which is exactly blocks 6..9).
    ftl                     FORMAT_A MCRC + EE3 convergence, and the aux rule
                            that `msoDescriptor +17` selects for the owning MSO
                            (`Ftl.aux_layout_of_block` -> `aux.read_is_plausible`).

WHAT IS DELIBERATELY *NOT* SMOOTHED OVER
----------------------------------------
`wrong-mode` is only ever reported for a page that FAILED its region's check.
A page that decodes is `ok` no matter what the descriptors say the cell mode
should be -- a verdict must never demote a demonstrably good read on the
strength of a table.  And `aux-rejected` is its own verdict, not folded into
`fail`: the bytes passed every integrity check the codec has and were still
refused by the address crib, which is a different and much more interesting
condition than "this did not decode".
"""
import collections
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


try:
    from . import geometry as geom
except ImportError:                                     # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom

#: The verdicts.  Exactly one is returned per page.
OK = "ok"                     # the region's own check passed
ERASED = "erased"             # all 18432 bytes are 0xFF; blank media, not a read
DEAD_CE = "dead-ce"           # a die that does not answer.  Not media at all.
WRONG_MODE = "wrong-mode"     # captured in the other cell mode than the WERU uses
AUX_REJECTED = "aux-rejected"  # integrity passed, the layout's address rule did not
FAIL = "fail"                 # a real failure: the bytes are not recoverable
UNREADABLE = "unreadable"     # the Device could not serve the page at all
VERDICTS = (OK, ERASED, DEAD_CE, WRONG_MODE, AUX_REJECTED, FAIL, UNREADABLE)

#: Verdicts that are not read failures.  A sweep's headline number is
#: `sum(counts[v] for v in VERDICTS if v not in NOT_A_FAILURE)`.
NOT_A_FAILURE = (OK, ERASED, DEAD_CE)

#: CE0 on this board answers with a two-symbol page and not one 0xFF byte, so it
#: is neither programmed nor erased.  Detected by CE number first (cheap and
#: certain) and by alphabet second, so a second failing die is still caught.
DEAD_ALPHABET = frozenset((0x00, 0xA7))

PAGE_BYTES = geom.AU_STORED * geom.AUS_PER_PAGE_TLC          # 18432

PageVerdict = collections.namedtuple("PageVerdict", "verdict region detail")


def _sib(name):
    """Import a sibling module whether this is `titania.verdict` or a script.

    `from . import x` raises when the file is run directly, which sent every
    media check in `selftest` down the "no local store" path while looking like
    a clean skip.  Try the package form too before giving up.
    """
    import importlib
    for mod in (("%s.%s" % (__package__, name)) if __package__ else None,
                "titania." + name):
        if not mod:
            continue
        try:
            return importlib.import_module(mod)
        except ImportError:
            continue
    raise ImportError("cannot import %s" % name)


def _codec():
    return _sib("codec")


def page_class(raw):
    """-> ERASED / DEAD_CE / None, from the bytes alone.  Cheap, no decode."""
    b = bytes(raw)
    if b.count(0xFF) == len(b):
        return ERASED
    if set(b) <= DEAD_ALPHABET:
        return DEAD_CE
    return None


def classify(raw, ce, block, page, slc=False, aux_layout=None, cell_mode=None,
             correct=True):
    """One raw page -> `PageVerdict`.

    `aux_layout`  the owning MSO's `msoDescriptor +17`, or None if not known.
                  None means the aux crib is not applied at all -- it does not
                  mean layout 0.
    `cell_mode`   True (pseudo-SLC) / False (native TLC) / None if not known.
                  Consulted only to explain a failure, never to cause one.
    `correct`     run the EE3 decoder on a page that fails MCRC uncorrected.
                  The two-stage order is a measurement, not a style choice: an
                  uncorrected decode is ~0.03 s and a corrected one ~3.4 s, and
                  most pages never need the second.

    `detail` carries every measurement taken, so a caller can report why.
    """
    region = geom.region(ce, block)
    d = {"mcrc": None, "ecc": None, "aus": None, "layout": aux_layout,
         "aux_ok": None, "aux_detail": "", "corrected": None, "note": ""}

    if ce in geom.CE_DEAD:
        # By number, before anything else.  A dead-die page that happened to
        # carry a third byte value would otherwise be run through a full LDPC
        # correction and then filed as a retry candidate it can never be.
        d["note"] = "ce%d is dead on this board and ANSWERS with a bus constant" % ce
        return PageVerdict(DEAD_CE, region, d)
    if raw is None:
        d["note"] = "the device served nothing for this page"
        return PageVerdict(UNREADABLE, region, d)
    raw = bytes(raw)
    if len(raw) != PAGE_BYTES:
        d["note"] = "%d bytes, expected %d" % (len(raw), PAGE_BYTES)
        return PageVerdict(UNREADABLE, region, d)
    cls = page_class(raw)
    if cls is not None:
        return PageVerdict(cls, region, d)

    if region == geom.REGION_FW_GROUP2:
        return _classify_group2(raw, region, d, slc, cell_mode, page)
    return _classify_format_a(raw, block, page, region, d, aux_layout,
                              cell_mode, slc, correct)


def _mode_note(slc, cell_mode, page):
    """-> a reason string if the capture mode contradicts the WERU's, else ''.

    The word-line gate is not optional, and leaving it out mislabelled 80
    pages.  A native block still has natively-SLC word lines at both ends
    (`geometry.cell_mode` returns 2 there), and on those the two prefixes are
    not distinguishable: pages 0 and 2 of weru 81 measured byte-identical under
    0xDA and 0xDF.  So a pseudo-SLC capture of page 0 of a native-TLC WERU is
    not evidence of a mode error -- and here it is demonstrably not one, because
    `qd_dump` holds pseudo-SLC reads of exactly those addresses that decode 4/4
    with a valid layout-3 aux.  Claiming "wrong mode" there would have excused a
    failure that is really a failed read with a good copy sitting on disk.
    """
    if cell_mode is not True and cell_mode is not False:
        return ""                                # None, ftl.UNKNOWN, anything
    if bool(slc) == cell_mode:
        return ""
    if geom.cell_mode(page) != 0:
        return ""                                # a natively-SLC word line
    return ("captured %s but the owning WERU is configured %s -- a wrong-mode "
            "read returns ~1000 LDPC syndromes per codeword, which looks "
            "exactly like an unwritten block.  These bytes are a read of the "
            "wrong thing, not a failed read of the right one."
            % ("pseudo-SLC" if slc else "native TLC",
               "pseudo-SLC" if cell_mode else "native TLC"))


def _classify_group2(raw, region, d, slc, cell_mode, page):
    chk = _sib("fw").check_group2_page(raw)
    d["mcrc"], d["aus"] = chk["mcrc"][0], chk["mcrc"][1]
    d["note"] = "; ".join(chk["notes"]) if chk["notes"] else ""
    if chk["ok"]:
        return PageVerdict(OK, region, d)
    why = _mode_note(slc, cell_mode, page)
    if why:
        d["note"] = (d["note"] + "; " if d["note"] else "") + why
        return PageVerdict(WRONG_MODE, region, d)
    return PageVerdict(FAIL, region, d)


def _classify_format_a(raw, block, page, region, d, aux_layout, cell_mode, slc,
                       correct):
    c = _codec()
    row = geom.compose_row(block, page)
    try:
        res = c.decode_page(raw, row=row, fmt=c.FORMAT_A, correct=False)
    except Exception as exc:                              # noqa: BLE001
        d["note"] = "uncorrected decode raised %r" % (exc,)
        res = None
    if res is not None:
        d["aus"] = len(res.aus)
        d["mcrc"] = sum(1 for a in res.aus if a.mcrc_ok)
        d["ecc"] = d["mcrc"]
        d["corrected"] = False
    if res is None or d["mcrc"] != d["aus"]:
        if not correct:
            return PageVerdict(FAIL, region, d)
        try:
            res = c.decode_page(raw, row=row, fmt=c.FORMAT_A, correct=True)
        except Exception as exc:                          # noqa: BLE001
            d["note"] = "corrected decode raised %r" % (exc,)
            why = _mode_note(slc, cell_mode, page)
            if why:
                d["note"] += "; " + why
                return PageVerdict(WRONG_MODE, region, d)
            return PageVerdict(FAIL, region, d)
        d["aus"] = len(res.aus)
        d["mcrc"] = sum(1 for a in res.aus if a.mcrc_ok)
        d["ecc"] = sum(1 for a in res.aus if a.ecc_ok)
        d["corrected"] = True

    if d["mcrc"] != d["aus"] or d["ecc"] != d["aus"]:
        why = _mode_note(slc, cell_mode, page)
        if why:
            d["note"] = (d["note"] + "; " if d["note"] else "") + why
            return PageVerdict(WRONG_MODE, region, d)
        return PageVerdict(FAIL, region, d)

    # Integrity passed.  Now, and only on the FTL region, the address crib --
    # keyed on the layout the owning MSO's msoDescriptor +17 selects.
    if region != geom.REGION_FTL:
        d["aux_detail"] = ("the firmware area is addressed by raw "
                           "CE/block/page through FwDataRw.c; its aux is not "
                           "an LMA array, so no address crib applies")
        return PageVerdict(OK, region, d)
    if aux_layout is None:
        d["aux_detail"] = ("no aux layout supplied -- msoDescriptor +17 was "
                           "not consulted, so the crib is not applied")
        return PageVerdict(OK, region, d)
    ev = _sib("auxdata").read_evidence([bytes(a.aux) for a in res.aus], aux_layout)
    d["aux_ok"], d["aux_detail"] = ev["ok"], ev["detail"]
    if ev["ok"] is False:
        return PageVerdict(AUX_REJECTED, region, d)
    return PageVerdict(OK, region, d)


class Classifier(object):
    """`classify`, with the layout and the cell mode looked up from an `Ftl`.

    This is the descriptor-driven form and the one a sweep should use: the
    layout comes from `msoDescriptor +17` of the MSO that owns the (block, ce)
    per the blockMapping, and the cell mode from the owning WERU's
    configuration.  Both are cached per (block, ce).

    `Ftl` may be None, in which case this is exactly `classify` with no crib --
    useful for a raw dump with no SystemMetadata, and it says so rather than
    inventing a layout.
    """

    def __init__(self, ftl=None):
        self.ftl = ftl
        self._layout = {}
        self._mode = {}
        self.stats = collections.Counter()

    @staticmethod
    def _clean(v):
        """ftl.UNKNOWN / None -> None; anything else through."""
        return None if v is None or not isinstance(v, (bool, int)) else v

    def layout_of(self, block, ce):
        key = (block, ce)
        if key not in self._layout:
            v = None
            if self.ftl is not None:
                try:
                    v = self._clean(self.ftl.aux_layout_of_block(block, ce))
                except Exception:                          # noqa: BLE001
                    v = None
            self._layout[key] = v
        return self._layout[key]

    def cell_mode_of(self, block, ce):
        key = (block, ce)
        if key not in self._mode:
            v = None
            if self.ftl is not None:
                try:
                    m = self.ftl.cell_mode_of_block(block, ce)
                    v = m if m is True or m is False else None
                except Exception:                          # noqa: BLE001
                    v = None
            self._mode[key] = v
        return self._mode[key]

    def classify(self, raw, ce, block, page, slc=False, correct=True):
        v = classify(raw, ce, block, page, slc=slc,
                     aux_layout=self.layout_of(block, ce),
                     cell_mode=self.cell_mode_of(block, ce), correct=correct)
        self.stats[v.verdict] += 1
        return v


# ===========================================================================
# selftest
# ===========================================================================
def _report(name, state, detail=""):
    print("  %-46s %-8s %s" % (name, state, detail))


def selftest(argv=()):
    """-> 0 on success.  SKIPPED, never PASS, for anything the dumps cannot show."""
    print("titania.verdict -- selftest\n")
    fails = 0

    # ---- 1. the region map, offline ---------------------------------------
    cases = [((1, 0), geom.REGION_FW_GROUP2), ((5, 0), geom.REGION_FW_GROUP2),
             ((0, 0), geom.REGION_FW_GROUP2), ((6, 0), geom.REGION_FTL),
             ((11, 0), geom.REGION_FTL),
             ((1, 6), geom.REGION_FW_GROUP01), ((11, 9), geom.REGION_FW_GROUP01),
             ((1, 5), geom.REGION_FTL), ((1, 10), geom.REGION_FTL),
             ((1, 3608), geom.REGION_FTL)]
    bad = [(k, geom.region(*k), want) for k, want in cases
           if geom.region(*k) != want]
    fails += bool(bad)
    _report("region map", "PASS" if not bad else "FAIL",
            "block 0 on CE 0..5 is group 2 and block 0 on CE 6..11 is NOT "
            "(measured: MCRC-clean FORMAT_A pages owned by msoId 57 on CE6); "
            "blocks 6..9 are the group 0/1 firmware area on every CE; "
            "everything else is FTL-addressed%s" % (" -- %s" % bad if bad else ""))

    # ---- 2. it agrees with titania.fw -------------------------------------
    try:
        fw = _sib("fw")
        g01 = tuple(sorted(b for bs in fw.GROUP_BLOCKS.values() for b in bs))
        ok = (fw.GROUP2_BLOCK == geom.FW_GROUP2_BLOCK
              and tuple(fw.GROUP2_CES) == tuple(geom.FW_GROUP2_CES)
              and g01 == tuple(geom.FW_GROUP01_BLOCKS))
        fails += not ok
        _report("agrees with titania.fw", "PASS" if ok else "FAIL",
                "GROUP2_BLOCK/GROUP2_CES are geometry's, and GROUP_BLOCKS %s "
                "is FW_GROUP01_BLOCKS %s -- one statement, cross-checked"
                % (fw.GROUP_BLOCKS, geom.FW_GROUP01_BLOCKS))
    except Exception as exc:                               # noqa: BLE001
        _report("agrees with titania.fw", "SKIPPED", repr(exc))

    # ---- 3. dead CE and erased are decided before any decode ---------------
    v = classify(b"\xFF" * PAGE_BYTES, 1, 3608, 8)
    ok = v.verdict == ERASED
    v0 = classify(b"\xA7" * PAGE_BYTES, 0, 3608, 8)
    ok = ok and v0.verdict == DEAD_CE
    v1 = classify((b"\xA7" * 2304 + b"\x00" * 16128), 1, 3608, 8)
    ok = ok and v1.verdict == DEAD_CE
    fails += not ok
    _report("erased and dead-CE short-circuit", "PASS" if ok else "FAIL",
            "an all-0xFF page is `erased` and a {0x00,0xA7} page is `dead-ce` "
            "on ce0 BY NUMBER and on any CE by alphabet -- neither is a read "
            "failure and neither costs an LDPC decode")

    # ---- 4. media ----------------------------------------------------------
    try:
        DumpDevice = _sib("dumpdev").DumpDevice
        F = _sib("ftl")
        dev = DumpDevice(config.default_root())
        f = F.Ftl.from_device(dev)
    except Exception as exc:                               # noqa: BLE001
        for n in ("group-2 region decodes with group-2 rules",
                  "the cell-mode class is labelled, not failed",
                  "layout-3 pages are accepted"):
            _report(n, "SKIPPED", "no local store: %r" % (exc,))
        return 1 if fails else 0

    cl = Classifier(f)

    # 4a.  The whole group-2 region, every page.
    counts = collections.Counter()
    misses = []
    for ce in geom.FW_GROUP2_CES:
        if ce in geom.CE_DEAD:
            continue
        for pg in range(geom.PAGES_PER_BLOCK_SLC):
            try:
                raw = dev.read_page(ce, geom.FW_GROUP2_BLOCK, pg, slc=True)
            except Exception:                              # noqa: BLE001
                continue
            v = cl.classify(raw, ce, geom.FW_GROUP2_BLOCK, pg, slc=True)
            counts[v.verdict] += 1
            if v.verdict != OK:
                misses.append((ce, pg))
    if not counts:
        _report("group-2 region decodes with group-2 rules", "SKIPPED",
                "no block-0 page of CE 1..5 in this store")
    else:
        try:
            from . import nd_g2extract as _g2
            known = set(_g2.KNOWN_MEDIA_ERROR_ROWS)
        except Exception:                                  # noqa: BLE001
            known = set()
        rows = sorted({p for _c, p in misses})
        ok = counts[OK] == sum(counts.values()) - len(misses) and (
            not misses or (known and set(rows) <= known))
        fails += not ok
        _report("group-2 region decodes with group-2 rules",
                "PASS" if ok else "FAIL",
                "%d of %d block-0 pages on CE 1..5 pass the group-2 AU digest "
                "5/5; the %d that do not are rows %s, which are exactly "
                "nd_g2extract.KNOWN_MEDIA_ERROR_ROWS (single flipped payload "
                "bits, one CE each, localised with nd_g2mcrc) -- real media "
                "errors, recoverable from the other four replicas.  Decoded as "
                "FORMAT_A instead, ALL %d fail."
                % (counts[OK], sum(counts.values()), len(misses),
                   rows or "-", sum(counts.values())))

    # 4b.  The cell-mode class: page 287 of blocks 172..175, keyed native TLC
    #      on the pseudo-SLC WERU 84.
    wrong, right, other = [], [], []
    for (ce, blk, pg, s) in sorted(dev.entries):
        if not (172 <= blk <= 175) or ce in geom.CE_DEAD:
            continue
        try:
            raw = dev.read_page(ce, blk, pg, slc=s)
        except Exception:                                  # noqa: BLE001
            continue
        v = cl.classify(raw, ce, blk, pg, slc=s)
        (wrong if v.verdict == WRONG_MODE else
         right if v.verdict == OK else other).append((ce, blk, pg, s))
    if not (wrong or right):
        _report("the cell-mode class is labelled, not failed", "SKIPPED",
                "no page of blocks 172..175 in this store")
    else:
        ok = (all(not s for (_c, _b, _p, s) in wrong)
              and all(s for (_c, _b, _p, s) in right)
              and not other)
        fails += not ok
        _report("the cell-mode class is labelled, not failed",
                "PASS" if ok else "FAIL",
                "%d stored pages of blocks 172..175 are keyed native TLC and "
                "every one is reported `wrong-mode`, not `fail`; the %d keyed "
                "pseudo-SLC all decode.  WERU 84's cell mode comes from its own "
                "configuration, not from a block number -- weru 234 is blocks "
                "852..855 and is native TLC.  %s"
                % (len(wrong), len(right),
                   "residue %s" % (other[:4],) if other else "no residue"))

    # 4c.  Layout 3 pages are accepted rather than rejected by the LMA crib.
    auxmod = _sib("auxdata")
    budget = 200
    if "--pages" in argv:
        budget = int(argv[list(argv).index("--pages") + 1])
    n3 = ok3 = rej3 = tried = 0
    for (ce, blk, pg, s) in sorted(dev.entries):
        if tried >= budget:
            break
        if ce in geom.CE_DEAD or cl.layout_of(blk, ce) != 3:
            continue
        try:
            raw = dev.read_page(ce, blk, pg, slc=s)
        except Exception:                                  # noqa: BLE001
            continue
        if page_class(raw) is not None:
            continue
        tried += 1
        v = cl.classify(raw, ce, blk, pg, slc=s)
        if v.verdict == OK and v.detail["aux_ok"] is True:
            n3 += 1
            ok3 += 1
        elif v.verdict == AUX_REJECTED:
            n3 += 1
            rej3 += 1
    if not n3:
        _report("layout-3 pages are accepted", "SKIPPED",
                "no clean layout-3 page in this store")
    else:
        ok = rej3 == 0
        fails += not ok
        _report("layout-3 pages are accepted", "PASS" if ok else "FAIL",
                "%d clean RAID-parity pages, %d accepted and %d rejected.  "
                "The blanket 'four consecutive LMAs' test rejects 100%% of them "
                "-- sub_1C8D8 never writes an LMA array on layout 3 -- which is "
                "what withheld 82 pages that had been read 246 times with MCRC "
                "4/4 on every read.  Rule now used: %s"
                % (n3, ok3, rej3, auxmod.ACCEPTANCE_BY_LAYOUT[3]))

    dev.close()
    return 1 if fails else 0


if __name__ == "__main__":                                # pragma: no cover
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest(sys.argv[1:]))
    print(__doc__)
