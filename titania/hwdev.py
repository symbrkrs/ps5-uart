"""HardwareDevice -- a `Device` over the live Titania board.

    dev = HardwareDevice()                 # read-only, CE0 excluded
    page = dev.read_page(ce=6, block=8, page=0, slc=True)

Everything below the class is somebody else's driver: `nd_hw.py` brings the
controller up from any register state, `nd_fwpage.py` + `nd_fast.py` do the page
reads, `nd_dumpdeep.enter_slc` / `nd_tlc.exit_slc` flip the die's cell mode.
This module contributes three things those drivers do not have: a device-state
model of the cell-mode latch, an honest erased fast path, and a write path that
takes two independent opt-ins to arm.

THE CELL-MODE LATCH IS DEVICE STATE, NOT A PER-CALL FLAG
--------------------------------------------------------
A Samsung die here answers to `0xDA` (pseudo-SLC, 368 pages/block) and `0xDF`
(native multi-level, 1104 pages/block).  Both are sticky per die, and reading in
the wrong one does not fail -- it returns plausible-looking bytes.  A native-TLC
page read while the die is latched in SLC comes back ~2.5% 0xFF with every MCRC
failing and ~1000 LDPC syndromes per codeword, which reads exactly like "this
region was never written".  That mistake produced a false blank verdict across
19 WERUs in this project.

So the mode is tracked per (ce, lun) as `self._mode`, starts UNKNOWN, and
`_ensure_mode` issues the select only on a genuine transition.  Every read goes
through it.  There is no code path in this class that reaches the media without
the latch being known-correct first.

READ RETRY IS DEVICE STATE TOO, AND IT IS ALWAYS PUT BACK
---------------------------------------------------------
A NAND cell is read by comparing its stored charge against reference voltages
calibrated when the die was fresh.  As charge leaks and neighbours disturb one
another the distributions drift, and an intact page becomes unreadable purely
because the comparison is being made in the wrong place.  On this board that is
not a theory: within a native-TLC block, readability is a function of the page's
position on its word line, and the upper pages fail at the default references
while the lower pages of the same block read cleanly.

Samsung's answer, and the firmware's, is SET FEATURES (0xEF) to feature
addresses 0x89 / 0x8A (multi-level) or 0x8D (pseudo-SLC), carrying signed
offsets to the reference voltages.  `pr_setfeat.py` documents where every byte
of that comes from and holds the firmware's own 32-step ladder.

It is modelled exactly like the cell-mode latch, for exactly the same reason:
setting it is a command, it persists until changed, and leaving it set silently
changes every later read.  So:

  * `self._retry` tracks the level per (ce, cell mode); the two modes use
    different feature registers inside the die and do not interact;
  * a level is written only on a genuine transition;
  * the state is observable, not merely tracked -- `probe_retry_level` reads it
    back with 0xEE and matches it against the ladder;
  * `close()` restores every touched target to level 0, and so does the
    `finally` of every retry walk.  A session that raises still leaves the die
    on its default references.

`read_page_ex(..., retry=n)` reads at one level.  `read_page_retry` walks the
ladder until the page decodes and reports which level won -- which is itself a
measurement of that block's health, and is carried in `PageRead.retry` so a
caller folding the page into a store cannot lose it.

CE0 IS DEAD ON THIS BOARD
-------------------------
`chan_map[0] == 0`, READ ID returns `a7 00 00 ...`.  It is excluded from
`present_ces()` by default, but `ce_status()` and `describe()` say so out loud
rather than letting a silently-shortened CE list quietly change an average.  A
glob that sorted CE0 first has already produced one bogus PBA and one verdict
computed from zero samples.

WRITE SAFETY -- TWO OPT-INS, PLUS A DRY RUN
-------------------------------------------
`write_page` and `erase_block` are implemented in full, but arming them takes
both `HardwareDevice(allow_hardware_writes=True)` and `enable_writes(CONFIRM)`,
neither of which is persisted anywhere.  `dry_run=True` (the default whenever
hardware writes were not requested) builds the exact command sequence, appends
it to `self.command_log`, and issues nothing -- which is how the sequence is
tested without touching irreplaceable media.

    py titania/hwdev.py --selftest          # read-only; needs the board
    py titania/hwdev.py --selftest --offline    # dry-run only, no board
"""
import sys
import time
from collections import OrderedDict
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from titania import geometry as geom
    from titania.device import (Device, DeviceError, WriteProtected, CONFIRM,
                                PageRead, PageScan, FULL, ERASED_DEVICE,
                                CACHED)
    from titania.dumpdev import scan_bytes
else:
    from . import geometry as geom
    from .device import (Device, DeviceError, WriteProtected, CONFIRM,
                         PageRead, PageScan, FULL, ERASED_DEVICE, CACHED)
    from .dumpdev import scan_bytes

#: where the drivers live -- the working directory, one level up from the package
DRIVER_DIR = Path(__file__).resolve().parent.parent

PAGE_BYTES = 18432
NFIF = 0x1700E000
CFG = NFIF + 0x20

# --- the EFC-side page reduction (tdev_scan.c) -------------------------------
# Addresses chosen clear of everything else this tree loads: nd_fast's drain stub
# is at 0x01160000 (72 B) with its page buffer at 0x01100000+0x4800, and
# nd_efcscan's is at 0x01161000 with params/output at 0x01105F00 / 0x01106000.
#: The device-side scan stub, which ships INSIDE the package. Spelled out from
#: `__file__` rather than imported from `armbuild`, so this module keeps its
#: lazy `from . import armbuild` -- the toolchain layer is only wanted when
#: something is about to build. `tests/test_packaging.py` asserts this agrees
#: with `armbuild.STUB_DIR`, because two spellings of one location is exactly
#: the kind of pair that drifts.
STUB_DIR = Path(__file__).resolve().parent / "stubs"
SCAN_SRC = STUB_DIR / "tdev_scan.c"
SCAN_STUB = 0x01162000       # 360 bytes of thumb
SCAN_OUT = 0x01107000        # 64-byte struct trec
SCAN_REC = 64
SCAN_SPIN = 200000
#: struct trec: words, nonff, first_nonff, crc, zero, aux[8], pad[3]
SCAN_FMT = "<16I"
NO_NONFF = 0xFFFFFFFF

#: cell-mode select opcodes.  Samsung/Micron; Hynix/Toshiba/SanDisk use 0xA2 for
#: the SLC side.  Both are standalone mode bytes -- no address, no data, no
#: confirm code -- so neither can alter stored charge.
CMD_ENTER_SLC = 0xDA
CMD_EXIT_SLC = 0xDF

#: the only opcodes this module will ever put on a command port for a read
READ_OPCODES = (0x00, 0x30, 0x70, 0x90, CMD_ENTER_SLC, CMD_EXIT_SLC,
                0xEE, 0xEF)
#: ... and the ones it will only ever put there with both write opt-ins armed
WRITE_OPCODES = (0x80, 0x10, 0x85)
ERASE_OPCODES = (0x60, 0xD0)

MODE_SLC = "slc"
MODE_TLC = "tlc"

#: flashIndAccessCfg arm words, from nd_fwpage / nd_hw.  Bit 4 is IN_OUT: the
#: read arms have it clear, the program/erase arms have it set, and that single
#: bit is the difference between 0x1A6 and 0x1B6.
CFG_READ_CMD = 0x1A6      # col + row + wait-ready + arm + start   (0x00 .. 0x30)
CFG_DATA_OUT = 0x180      # arm + start, data phase
CFG_ROW_ONLY = 0x1A4      # row cycles + wait + arm + start
CFG_PROGRAM = 0x1B6       # ... with IN_OUT set: data in
CFG_ERASE = 0x1B4

#: Get / SET FEATURES.  0xEF writes a VOLATILE mode register inside the die --
#: one address cycle, four payload bytes, no row address, no confirm code, so
#: there is no sequence by which it reaches the array -- and it is how the
#: shipping firmware moves the read reference voltages.  `pr_setfeat.py` carries
#: the register sequence (EFC boot ROM `HalNfCtrl_SetFeatureIm`, :2618) and the
#: 32-step ladder (CPU1 firmware `K9BKGD8JxA.c`, efc_cpu1.c:10226).
CMD_GET_FEATURES = 0xEE
CMD_SET_FEATURES = 0xEF

#: the level every session starts and ends on: the die's calibrated defaults
RETRY_DEFAULT = 0


class Cmd(object):
    """One flash command, as a record.

    The write path builds these instead of driving registers directly, so the
    sequence can be inspected, asserted on and diffed without a board anywhere
    near it.
    """

    __slots__ = ("op", "ce", "lun", "row", "col", "cfg", "nbytes", "note")

    def __init__(self, op, ce, lun=0, row=None, col=0, cfg=None, nbytes=0,
                 note=""):
        self.op = op
        self.ce = ce
        self.lun = lun
        self.row = row
        self.col = col
        self.cfg = cfg
        self.nbytes = nbytes
        self.note = note

    def is_destructive(self):
        return self.op in WRITE_OPCODES or self.op in ERASE_OPCODES

    def __repr__(self):
        parts = ["op=%#04x" % self.op, "ce=%d" % self.ce]
        if self.lun:
            parts.append("lun=%d" % self.lun)
        if self.row is not None:
            parts.append("row=%#x" % self.row)
        if self.col:
            parts.append("col=%d" % self.col)
        if self.cfg is not None:
            parts.append("cfg=%#05x" % self.cfg)
        if self.nbytes:
            parts.append("n=%d" % self.nbytes)
        s = "<Cmd %s>" % " ".join(parts)
        return s + ("  # " + self.note if self.note else "")


def build_scan_stub(stub_addr=SCAN_STUB):
    """Compile `tdev_scan.c` for cortex-r5 thumb, linked at `stub_addr`.

    -> (blob, entry_addr).  Needs an ARM bare-metal toolchain, the same way
    `nd_fast.build()` does -- run directly if it is on PATH, else through WSL;
    see `titania.armbuild`.  Raises `DeviceError` if neither is reachable, and
    the caller then falls back.
    """
    from . import armbuild
    if not SCAN_SRC.exists():
        raise DeviceError("missing %s" % SCAN_SRC)
    try:
        blob, syms = armbuild.compile_stub(
            SCAN_SRC.name, entry="tdev_scan", text_addr=stub_addr,
            cwd=SCAN_SRC.parent)
    except (armbuild.ToolchainMissing, OSError) as exc:
        raise DeviceError("cannot build the scan stub: %s" % exc)
    if "tdev_scan" not in syms:
        raise DeviceError("tdev_scan symbol not found in the build")
    return blob, syms["tdev_scan"]


def parse_scan(raw):
    """A 64-byte `struct trec` -> (PageScan, words, zero, aux).

    `words` is how far the drain actually got.  A short drain is a FAILED read,
    never an erased page: the caller must raise rather than report a verdict,
    because "the transfer stopped early" and "the page is blank" are the two
    things this whole design exists to keep apart.
    """
    import struct
    v = struct.unpack(SCAN_FMT, raw)
    words, nonff, first, crc, zero = v[0], v[1], v[2], v[3], v[4]
    aux = bytes(raw[20:52])
    scan = PageScan(all_ones=(nonff == 0),
                    nonff_bytes=nonff,
                    first_nonff=(None if first == NO_NONFF else first),
                    crc=crc)
    return scan, words, zero, aux


class HardwareDevice(Device):
    """A `Device` over the live board.

    Parameters
    ----------
    allow_hardware_writes : bool
        First of the two opt-ins the write path needs.  Without it `writable` is
        False and `enable_writes` refuses outright.
    dry_run : bool
        Build command sequences and log them instead of issuing them.  Forced on
        when `allow_hardware_writes` is False, so an un-armed instance can still
        exercise the whole write path safely.
    ces : iterable or None
        CEs to serve.  None means "every CE that answers READ ID, minus the dead
        ones"; see `exclude_dead`.
    exclude_dead : bool
        Drop `geometry.CE_DEAD` (CE0 here) from `present_ces()`.  The exclusion
        is always reported by `describe()`, never silent.
    cache_pages : int
        Bounded LRU of whole pages.  8 pages is 147 KB; a whole block is 20 MB,
        so this is small.  0 disables it.
    fast_erased : bool
        Short-circuit a read when the EFC-side scan says the page is all-ones.
        Safe to leave on -- that verdict is exact, computed over all 18432 bytes
        on the device.  Turn it off only when the raw bytes of an erased page
        are themselves the object of study.
    device_scan : bool
        Use the EFC-side reduction at all.  False forces every question to be
        answered by transferring the page, which is the reference behaviour the
        selftest checks the fast path against.
    """

    writable = False            # promoted per-instance by allow_hardware_writes

    def __init__(self, allow_hardware_writes=False, dry_run=None, ces=None,
                 exclude_dead=True, cache_pages=8, fast_erased=True,
                 device_scan=True, connect_now=False, verbose=False,
                 timing="fast"):
        Device.__init__(self)
        self.allow_hardware_writes = bool(allow_hardware_writes)
        self.writable = bool(allow_hardware_writes)
        self.dry_run = (not self.allow_hardware_writes) if dry_run is None \
            else bool(dry_run)
        self.exclude_dead = bool(exclude_dead)
        self.fast_erased = bool(fast_erased)
        self.device_scan = bool(device_scan)
        self.verbose = verbose
        self.timing = timing
        self.cache_pages = int(cache_pages)

        self._requested_ces = None if ces is None else sorted(set(ces))
        self._client = None
        self._fp = None                  # FastPage (or FwPage) instance
        self._drain_loaded = False
        self._scan_entry = None          # tdev_scan entry address once loaded
        self._scan_kind = None           # "tdev" / "nd_scan" / None
        self._mode = {}                  # (ce, lun) -> MODE_SLC / MODE_TLC
        self._retry = {}                 # (ce, is_slc) -> level, 0 = default
        self._retry_touched = False      # has any 0xEF been issued this session
        self._ids = None                 # ce -> READ ID bytes
        self._cache = OrderedDict()
        self.command_log = []            # list[Cmd] -- everything a dry run built
        self.stats = {"pages_full": 0, "pages_erased_device": 0,
                      "pages_cached": 0, "au_reads": 0, "mode_switches": 0,
                      "scans": 0, "bytes_transferred": 0,
                      "retry_switches": 0, "retry_walks": 0,
                      "retry_rescued": 0}
        if connect_now:
            self._connect()

    # -- connection -------------------------------------------------------
    def _connect(self):
        """Open the link, enable the MPU, park the sequencer, load the drain stub.

        Lazy: constructing a dry-run instance must not need a
        board, which is what makes the write path testable offline.
        """
        if self._fp is not None:
            return self._fp
        if str(DRIVER_DIR) not in sys.path:
            sys.path.insert(0, str(DRIVER_DIR))
        try:
            from .nd_nfif import connect as nfif_connect
            from .nd_fast import FastPage, load_drain
        except Exception as exc:                       # pragma: no cover
            raise DeviceError("cannot import the NAND drivers from %s: %s"
                              % (DRIVER_DIR, exc))
        try:
            # connect() calls mpu_setup_cpu0() unconditionally.  With the MPU off
            # the NFIF window loses its Device typing and a halfword store zeroes
            # the other half of the dword -- which is how "dead hardware" was
            # faked for weeks.  Do not skip it, and do not add a conditional.
            c = nfif_connect()
        except Exception as exc:
            raise DeviceError("no board: %s: %s" % (type(exc).__name__, exc))
        f = FastPage(c)
        # The indirect engine and the sequencer share the flash pins.  Park the
        # sequencer before driving commands by hand; restart it in close().
        f.seq_stop()
        try:
            load_drain(c, verbose=self.verbose)
            self._drain_loaded = True
        except Exception as exc:
            # FastPage falls back to the byte-at-a-time pop automatically, so
            # this is slow but not wrong.  Say so instead of pretending.
            if self.verbose:
                print("  drain stub unavailable (%s); using the slow pop path"
                      % exc)
        self._client = c
        self._fp = f
        if self.device_scan:
            self._load_scan_stub()
        return f

    def _load_scan_stub(self):
        """Upload the EFC-side reduction, verifying the readback.

        Three tiers, and which one is in use is reported by `describe()` rather
        than left implicit:

          "tdev"    tdev_scan.c -- all four PageScan fields
          "nd_scan" nd_efcscan's stub -- exact, but no `first_nonff`
          None      no reduction; every question costs a full page transfer

        A stub that does not read back byte-identical is never called: an
        upload that half-landed would execute as whatever was there before.
        """
        c = self._client
        try:
            blob, entry = build_scan_stub()
            c.write(SCAN_STUB, blob)
            if bytes(c.read(SCAN_STUB, len(blob))) != blob:
                raise DeviceError("tdev_scan stub did not read back identical")
            self._scan_entry, self._scan_kind = entry, "tdev"
            if self.verbose:
                print("  scan stub: %d B at %#x (tdev_scan)"
                      % (len(blob), entry))
            return
        except Exception as exc:
            if self.verbose:
                print("  tdev_scan unavailable (%s); trying nd_efcscan" % exc)
        try:
            from . import nd_efcscan
            nd_efcscan.load(c, verbose=self.verbose)
            self._scan_entry = nd_efcscan._syms["nand_scan"]
            self._scan_kind = "nd_scan"
            return
        except Exception as exc:
            if self.verbose:
                print("  nd_efcscan unavailable too (%s); scans will cost a "
                      "full page transfer" % exc)
        self._scan_entry, self._scan_kind = None, None

    def close(self, restart_sequencer=True):
        """Leave the board the way a driver expects to find it.

        Read-retry levels are restored first and unconditionally.  A die left on
        a shifted reference would make every later read in every later session
        quietly wrong with nothing reporting an error, which is the worst
        failure mode available here -- so it is undone before anything else, and
        before any step that could itself throw.
        """
        if self._fp is None:
            return
        try:
            self.restore_retry_levels()
        except Exception as exc:
            print("WARNING: could not restore read-retry levels (%s); the dies "
                  "may be left on a shifted read reference.  Run "
                  "`py nd_reset.py --full`." % exc)
        try:
            self._fp.set_mode(2)
            if restart_sequencer:
                self._fp.seq_start()
        except Exception:
            pass
        try:
            self._client.port.close()
        except Exception:
            pass
        self._fp = None
        self._client = None

    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- cell mode --------------------------------------------------------
    def _select_mode(self, ce, lun, mode):
        """Issue the standalone mode byte.  No address, no data, no confirm."""
        f = self._connect()
        if str(DRIVER_DIR) not in sys.path:
            sys.path.insert(0, str(DRIVER_DIR))
        from .nd_fwpage import pna_of
        op = CMD_ENTER_SLC if mode == MODE_SLC else CMD_EXIT_SLC
        # pna_of's second field is the device select, not the LUN.  The LUN
        # lives in the row (bit 23, geometry.LUN_SHIFT), so dev is always 0
        # here; passing lun there would select a nonexistent device and leave
        # the row's lun field to do the work twice.
        f.im_setup(pna_of(ce, 0, 0, 0), op, 0, 0)
        f.w16(CFG, CFG_DATA_OUT)
        f.poll(0, 0x80)
        self._mode[(ce, lun)] = mode
        self.stats["mode_switches"] += 1
        if self.verbose:
            print("  ce%d lun%d -> %s (%#04x)" % (ce, lun, mode, op))

    def _ensure_mode(self, ce, lun, slc):
        """Make the die's latch match what the caller asked for.

        Elided when the latch is already known to be right -- which is the whole
        point of tracking it -- but not elided when it is unknown.  Unknown means
        unknown: a reset re-runs the ROM's NAND init, which issues 0xDA itself,
        so a fresh session is *probably* SLC, and "probably" is not good enough
        to skip a two-round-trip command.
        """
        want = MODE_SLC if slc else MODE_TLC
        if self._mode.get((ce, lun)) != want:
            self._select_mode(ce, lun, want)

    def current_mode(self, ce, lun=0):
        return self._mode.get((ce, lun))

    def resync_modes(self):
        """Forget every latch.  Call after anything that may have reset the part."""
        self._mode.clear()

    # -- read retry -------------------------------------------------------
    supports_retry = True

    @staticmethod
    def _ladder():
        if str(DRIVER_DIR) not in sys.path:
            sys.path.insert(0, str(DRIVER_DIR))
        from . import pr_setfeat
        return pr_setfeat

    def retry_levels(self, slc=False):
        """-> range of selectable levels.  0 is the die's default references.

        Levels 1..32 are the firmware's own `retryStep` values, in the
        firmware's order.  That order is not monotonic in voltage and the steps
        are not interpolatable: each one is a calibrated seven-way (TLC) or
        one-way (pseudo-SLC) shift chosen by the part vendor.  Walk them as
        given; do not sort them, do not average them, do not invent one.
        """
        return self._ladder().levels_for(bool(slc))

    def retry_params(self, level, slc=False):
        """-> {feature address: 4 payload bytes} that `level` would write."""
        return self._ladder().params_for(int(level), bool(slc))

    def _write_retry(self, ce, level, slc):
        """Issue the SET FEATURES for one level on one target."""
        f = self._connect()
        pf = self._ladder()
        for feat, param in sorted(self.retry_params(level, slc).items()):
            if not pf.set_feature(f, ce, feat, param):
                raise DeviceError(
                    "SET FEATURES 0x%02X did not complete on ce%d (the FIFO "
                    "never readied); the die's read references are now of "
                    "UNKNOWN state -- resync_retry() and re-read" % (feat, ce))
        self._retry[(ce, bool(slc))] = int(level)
        self._retry_touched = True
        self.stats["retry_switches"] += 1
        if self.verbose:
            print("  ce%d %s -> retry level %d"
                  % (ce, "SLC" if slc else "TLC", level))

    def probe_retry_level(self, ce, slc=False):
        """Read the die's actual retry registers back.  -> (level or None, raw)

        `level` is the ladder step whose payload matches, 0 for the default, or
        None when the die holds something not in the ladder.  This is what makes
        the tracked state checkable rather than merely asserted: the ladder is
        applied blind by every other tool, and a tracker that cannot be
        contradicted is not evidence.
        """
        f = self._connect()
        pf = self._ladder()
        feats = pf.FEAT_SLC if slc else pf.FEAT_TLC
        raw = {}
        for feat in feats:
            v = pf.get_feature(f, ce, feat)
            if v is None:
                return None, raw
            raw[feat] = v
        for lvl in self.retry_levels(slc):
            if self.retry_params(lvl, slc) == raw:
                return lvl, raw
        return None, raw

    def set_retry_level(self, level, ce=None, slc=False):
        """Select a retry level on one CE or on all present ones.  -> level.

        Written only on a genuine transition, exactly like the cell-mode latch.
        The one case where it is not elided is a level of 0 on a target this
        session has never touched: that is the state the die is believed to
        already be in, so nothing is issued, and `probe_retry_level` is there
        for when belief is not good enough.
        """
        level = int(level)
        if level not in self.retry_levels(slc):
            raise DeviceError("retry level %d is not in the ladder for %s "
                              "(levels %s)"
                              % (level, "pseudo-SLC" if slc else "native-TLC",
                                 list(self.retry_levels(slc))[:1] + ["..."] +
                                 list(self.retry_levels(slc))[-1:]))
        ces = self.present_ces() if ce is None else [int(ce)]
        for c in ces:
            key = (c, bool(slc))
            if self._retry.get(key) == level:
                continue
            if level == RETRY_DEFAULT and key not in self._retry \
                    and not self._retry_touched:
                # nothing in this session has moved the references, so the die
                # is on the defaults it powered up with.  Record, issue nothing.
                self._retry[key] = RETRY_DEFAULT
                continue
            self._write_retry(c, level, slc)
        return level

    def current_retry_level(self, ce, slc=False):
        return self._retry.get((int(ce), bool(slc)), RETRY_DEFAULT)

    def resync_retry(self):
        """Forget every tracked level.  Call after anything that reset the part."""
        self._retry.clear()

    def restore_retry_levels(self):
        """Put every target this session moved back on level 0.  -> count.

        Called by `close()`, and by the `finally` of every ladder walk, so a
        level cannot outlive the operation that wanted it -- not through an
        exception, not through a KeyboardInterrupt, and not through a caller who
        forgot.  Left-behind references are the nastiest possible failure here:
        every later read in every later session would be quietly wrong, and
        nothing would report an error.
        """
        n = 0
        for (ce, slc), lvl in sorted(self._retry.items()):
            if lvl != RETRY_DEFAULT:
                self._write_retry(ce, RETRY_DEFAULT, slc)
                n += 1
        return n

    def _ensure_retry(self, ce, level, slc):
        if level is None:
            return self.current_retry_level(ce, slc)
        self.set_retry_level(level, ce=ce, slc=slc)
        return level

    # -- addressing -------------------------------------------------------
    @staticmethod
    def _row(block, page, lun, slc):
        n = geom.pages_per_block(slc)
        if not 0 <= page < n:
            raise DeviceError("page %d is out of range for a %d-page %s block"
                              % (page, n, "pseudo-SLC" if slc else "native-TLC"))
        return geom.compose_row(block, page, lun)

    # -- raw transfers ----------------------------------------------------
    def _read_span(self, ce, block, page, lun, slc, col, nbytes):
        """One column-addressed read.  -> bytes (may be short).

        `nd_fwpage.read_page` re-issues 0x00 + col + row + 0x30 with the column
        taken from the pna, which is exactly what `nd_hw.read_page_bulk` does to
        walk a page in chunks -- so column addressing is a proven primitive here,
        not a new command.  The FIFO pops four bytes at a time, so both the
        column and the length are rounded out to a word and the result trimmed.
        """
        f = self._connect()
        from .nd_fwpage import pna_of
        self._ensure_mode(ce, lun, slc)
        row = self._row(block, page, lun, slc)
        lo = col & ~3
        hi = (col + nbytes + 3) & ~3
        want = hi - lo
        data = f.read_page(pna_of(ce, 0, lo, row), want)[2]   # dev=0; lun is in row
        self.stats["bytes_transferred"] += len(data)
        if len(data) < want:
            return data[col - lo:] if len(data) > (col - lo) else b""
        return data[col - lo:col - lo + nbytes]

    # -- Device interface -------------------------------------------------
    def present_ces(self):
        """CEs that answer READ ID, minus the dead ones.

        Never silently short: `describe()` and `ce_status()` report exactly which
        CEs were dropped and why.
        """
        if self._requested_ces is not None:
            ces = list(self._requested_ces)
        else:
            ces = sorted(ce for ce, ident in self.ce_status().items()
                         if ident is not None)
        if self.exclude_dead:
            ces = [ce for ce in ces if ce not in geom.CE_DEAD]
        return sorted(ces)

    def ce_status(self, refresh=False):
        """-> {ce: id bytes or None}.  A READ ID sweep of all 12 channels."""
        if self._ids is not None and not refresh:
            return self._ids
        f = self._connect()
        from .nd_fwpage import pna_of
        out = {}
        for ce in range(geom.CE_COUNT):
            try:
                f.im_setup(pna_of(ce, 0, 0, 0), 0x90, 0, 12)
                f.w16(CFG, 0x181)
                f.poll(0, 0x80)
                raw = f.pop(12)
            except Exception:
                raw = b""
            ident = bytes(raw[0::2][:6]) if len(raw) >= 12 else None
            # ch0 answers a7 00 00 ... -- present on the bus, not a working die
            if ident is not None and (len(set(ident)) <= 2 or ident[0] != 0xEC):
                ident = None
            out[ce] = ident
        self._ids = out
        return out

    def read_page(self, ce, block, page, lun=0, slc=False, retry=None):
        return self.read_page_ex(ce, block, page, lun=lun, slc=slc,
                                 retry=retry).data

    def read_page_ex(self, ce, block, page, lun=0, slc=False, retry=None):
        """The honest read.  -> PageRead(data, source)

        `source` distinguishes 18432 bytes that came off the die (`FULL`) from a
        page synthesised after sampling 96 of them (`ERASED_SAMPLED`) from one
        served out of the LRU (`CACHED`).  Anything recording evidence should
        read this field; anything concluding "blank" should set
        `fast_erased = False` so the field can only ever be FULL.

        `retry` selects a read-retry level for this read, or None to use
        whatever level the target is already on.  It is part of the CACHE key:
        the same page at two levels is two different measurements and must never
        be served for one another.
        """
        level = self._ensure_retry(ce, retry, slc)
        key = (ce, lun, block, page, bool(slc), level)
        if self.cache_pages and key in self._cache:
            self._cache.move_to_end(key)
            self.stats["pages_cached"] += 1
            return PageRead(self._cache[key], CACHED, level)
        if self.fast_erased and self.device_scan:
            s = self.scan_page(ce, block, page, lun=lun, slc=slc)
            if s is not None and s.all_ones:
                # Exact: the EFC examined all 18432 bytes.  Nothing was inferred
                # and nothing was skipped -- only the verdict crossed the wire.
                self.stats["pages_erased_device"] += 1
                return PageRead(b"\xff" * PAGE_BYTES, ERASED_DEVICE, level)
        data = self._read_span(ce, block, page, lun, slc, 0, PAGE_BYTES)
        if len(data) != PAGE_BYTES:
            raise DeviceError(
                "short read on ce%d block %d page %d (%s, retry %d): %d of %d "
                "bytes" % (ce, block, page, "SLC" if slc else "TLC", level,
                           len(data), PAGE_BYTES))
        self.stats["pages_full"] += 1
        self._cache_put(key, data)
        return PageRead(data, FULL, level)

    # -- the decode-driven ladder walk ------------------------------------
    def page_decodes(self, data, page, slc=False, aux_layout=None, ce=None,
                     block=None):
        """Did this page really read?  -> (ok, detail)

        MCRC alone is not enough and must not be used alone.  The MCRC is
        computed over the complemented stored bytes, before descrambling, so it
        tells you the codeword is self-consistent -- not that the reference
        voltages were right.  The working test is MCRC 4/4 and the LDPC
        converging on all eight codewords, plus -- where the caller can supply
        the owning MSO's aux layout -- the address crib that layout defines.

        The crib is per-layout.  It used to be "the four aux LMAs must be
        consecutive" for every page, which is a layout-0/1/2 rule: a layout-3
        (`cAuxLayoutForRaidParityMso`) page has no LMA array at all, so that
        test rejected every RAID-parity page on the drive -- 82 of them
        here, read 246 times with MCRC 4/4 on all 246, then withheld from the
        store as bad reads.  `titania.auxdata.read_is_plausible` carries the right
        rule for each layout; pass `aux_layout` from
        `Ftl.aux_layout_of_block(block, ce)`, which reads `msoDescriptor +17`.

        `ce`/`block` are optional and only sharpen the verdict: with both, the
        region is known, and block 0 of CE 0..5 is the group-2 firmware region,
        whose 20x512 geometry a FORMAT_A decode can never satisfy.  Without
        them this keeps its old FORMAT_A-only behaviour, so existing callers are
        unaffected.
        """
        try:
            from titania import codec
        except Exception as exc:                       # pragma: no cover
            raise DeviceError("codec unavailable, so a retry walk cannot tell "
                              "a good read from a bad one: %s" % exc)
        if ce is not None and block is not None:
            from titania import verdict as vd
            v = vd.classify(data, ce, block, page, slc=slc,
                            aux_layout=aux_layout)
            detail = dict(v.detail)
            detail["region"] = v.region
            detail["verdict"] = v.verdict
            detail["lmas"] = detail.get("aux_ok")
            return v.verdict == vd.OK, detail
        res = codec.decode_page(data, page)
        n_mcrc = sum(1 for a in res.aus if a.mcrc_ok)
        n_ecc = sum(1 for a in res.aus if a.ecc_ok)
        detail = {"mcrc": n_mcrc, "ecc": n_ecc, "lmas": None}
        ok = (n_mcrc == len(res.aus) and n_ecc == len(res.aus))
        if ok and aux_layout is not None:
            try:
                from titania import auxdata as auxmod
                acc = auxmod.read_is_plausible([a.aux for a in res.aus],
                                               aux_layout)
                detail["lmas"] = acc
                if acc is False:
                    ok = False
            except Exception:
                detail["lmas"] = "unavailable"
        return ok, detail

    def read_page_retry(self, ce, block, page, lun=0, slc=False, levels=None,
                        aux_layout=None, verify=None, on_step=None):
        """Read a page, walking the retry ladder until it decodes.  -> PageRead

        Level 0 first, then the ladder in the firmware's order.  `PageRead.retry`
        carries the level that actually worked, which is the point: a page that
        needed level 17 is evidence about that block, and a caller that folds it
        into a store without recording that has thrown the evidence away.

        Raises `DeviceError` if the ladder is exhausted.  It does
        not return the least-bad read: an undecodable page must be withheld, not
        folded in with a hopeful provenance, because a store that contains
        plausible garbage is worse than one with a hole in it.

        The level is restored to 0 on the way out no matter how this exits.
        """
        levels = list(self.retry_levels(slc)) if levels is None else list(levels)
        verify = verify or (lambda d: self.page_decodes(
            d, page, slc=slc, aux_layout=aux_layout, ce=ce, block=block))
        self.stats["retry_walks"] += 1
        tried = []
        try:
            for lvl in levels:
                pr = self.read_page_ex(ce, block, page, lun=lun, slc=slc,
                                       retry=lvl)
                ok, detail = verify(pr.data)
                tried.append((lvl, detail))
                if on_step is not None:
                    on_step(lvl, ok, detail)
                if ok:
                    if lvl != RETRY_DEFAULT:
                        self.stats["retry_rescued"] += 1
                    return pr
            raise DeviceError(
                "ce%d block %d page %d (%s) did not decode at any of %d retry "
                "levels; best seen %s -- WITHHELD rather than returned"
                % (ce, block, page, "SLC" if slc else "TLC", len(levels),
                   max((d for _, d in tried),
                       key=lambda d: (d["mcrc"], d["ecc"]), default=None)))
        finally:
            # a level must not outlive the walk, including on the raise above
            try:
                self.set_retry_level(RETRY_DEFAULT, ce=ce, slc=slc)
            except Exception:
                pass

    def _cache_put(self, key, data):
        if not self.cache_pages:
            return
        self._cache[key] = data
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_pages:
            self._cache.popitem(last=False)

    def scan_page(self, ce, block, page, lun=0, slc=False):
        """Exact full-page reduction, computed on the EFC.  -> PageScan or None

        The page is read through the ordinary path -- same 0x00 / 0x30 arm, same
        status poll as `read_page` -- and then `tdev_scan.c` drains the FIFO on
        the device, examining every one of the 18432 bytes and buffering none of
        them.  64 bytes come back over the UART instead of 18432: 288x less
        traffic for an answer that is not weaker in any way.

        Not sampled, and it must never become sampled.  The counts and the CRC
        are over the whole page; `all_ones` is a measurement, not a guess.

        A short drain raises.  `words < 4608` means the transfer stopped early,
        which is a read failure -- and a read failure that reported "all the
        bytes I saw were 0xFF" would be exactly the false-blank result this
        design exists to prevent.

        -> None when no reduction stub could be loaded, per the `Device`
        contract, so the caller falls back to a full transfer.
        """
        if not self.device_scan:
            return None
        f = self._connect()
        if self._scan_entry is None:
            return None
        from .nd_fwpage import pna_of
        self._ensure_mode(ce, lun, slc)
        row = self._row(block, page, lun, slc)
        pna = pna_of(ce, 0, 0, row)            # dev=0; the lun is in the row
        nwords = PAGE_BYTES // 4
        # Arm exactly as FwPage.read_page does, then reduce instead of draining.
        f.im_setup(pna, 0x00, 0x30, 0)
        f.w16(CFG, CFG_READ_CMD)
        f.poll(0, 0x80)
        for _ in range(20):
            st, _ = f.status(pna)
            if st is not None and (st & 0x20):
                break
        f.im_setup(pna, 0, 0, PAGE_BYTES)
        f.w16(CFG, CFG_DATA_OUT)
        f.c.call(self._scan_entry | 1, NFIF, SCAN_OUT, nwords, SCAN_SPIN)
        raw = bytes(f.c.read(SCAN_OUT, SCAN_REC))
        self.stats["scans"] += 1
        self.stats["bytes_transferred"] += SCAN_REC
        if self._scan_kind == "nd_scan":
            # nd_efcscan's record is words, ff, zero, crc, aux[8] -- exact, but
            # it does not track where the first non-0xFF byte was.
            import struct
            words, ff, zero, crc = struct.unpack_from("<4I", raw, 0)
            if words != nwords:
                raise DeviceError(
                    "short device scan on ce%d block %d page %d: %d of %d words"
                    % (ce, block, page, words, nwords))
            nonff = words * 4 - ff
            return PageScan(all_ones=(nonff == 0), nonff_bytes=nonff,
                            first_nonff=None, crc=crc)
        scan, words, _zero, _aux = parse_scan(raw)
        if words != nwords:
            raise DeviceError(
                "short device scan on ce%d block %d page %d: %d of %d words "
                "(a truncated transfer is a FAILURE, not an erased page)"
                % (ce, block, page, words, nwords))
        return scan

    def read_aus(self, ce, block, page, aus, lun=0, slc=False):
        """Column-addressed: read only the AUs asked for.  -> {index: bytes}

        One AU costs a quarter of a page, and contiguous runs are coalesced into
        a single command, so `aus=[1, 2]` is one transfer of 9216 bytes rather
        than two of 4608 or one of 18432.
        """
        aus = sorted(set(int(a) for a in aus))
        if not aus:
            return {}
        if aus[0] < 0 or aus[-1] >= geom.AUS_PER_PAGE_TLC:
            raise DeviceError("AU index out of range: %r" % (aus,))
        key = (ce, lun, block, page, bool(slc),
               self.current_retry_level(ce, slc))
        if self.cache_pages and key in self._cache:
            self._cache.move_to_end(key)
            d = self._cache[key]
            return {i: d[i * geom.AU_STORED:(i + 1) * geom.AU_STORED]
                    for i in aus}
        out = {}
        run = [aus[0]]
        for a in aus[1:] + [None]:
            if a is not None and a == run[-1] + 1:
                run.append(a)
                continue
            col = run[0] * geom.AU_STORED
            nb = len(run) * geom.AU_STORED
            data = self._read_span(ce, block, page, lun, slc, col, nb)
            if len(data) != nb:
                raise DeviceError(
                    "short AU read on ce%d block %d page %d AUs %s: %d of %d"
                    % (ce, block, page, run, len(data), nb))
            for j, idx in enumerate(run):
                out[idx] = data[j * geom.AU_STORED:(j + 1) * geom.AU_STORED]
            self.stats["au_reads"] += len(run)
            if a is not None:
                run = [a]
        return out

    def read_block(self, ce, block, lun=0, slc=False, pages=None):
        """Walk a block, settling the cell-mode latch once for the whole walk.

        Not a NAND cache read (0x31/0x3F): neither `nd_hw.py` nor `nd_fwpage.py`
        exposes one, and the sequencer path that would is the open blocker
        documented in nand-seq-fetch-blocker.  So the address is still re-issued
        per page; what this override removes is the per-page mode check and the
        repeated lazy-connect, not the address phase.
        """
        self._connect()
        self._ensure_mode(ce, lun, slc)
        n = pages if pages is not None else geom.pages_per_block(slc)
        for p in range(n):
            try:
                yield p, self.read_page(ce, block, p, lun=lun, slc=slc)
            except DeviceError:
                continue

    # -- write path -------------------------------------------------------
    # Fully implemented, and armed only by two independent opt-ins.  Neither is
    # persisted; both are per-instance.
    def _check_hw_write(self):
        self._check_write()
        if not self.allow_hardware_writes:
            raise WriteProtected(
                "this HardwareDevice was not constructed with "
                "allow_hardware_writes=True, so the media cannot be reached "
                "even with writes enabled")

    def _program_sequence(self, ce, block, page, data, lun=0, slc=False):
        """The command sequence for a page program.  -> list[Cmd]

        Built as data so it can be asserted on without a board.  Serial page
        program, following the firmware's HalNfCtrl_WritePageIm:

            0x80 + 2 column cycles + 3 row cycles, arm 0x1B6 (IN_OUT set)
            <18432 data bytes in>
            0x10 confirm, wait ready
            0x70 read status, bit0 == 0 means programmed

        Never executed against hardware, HERE OR anywhere.  What is verified is
        the shape: the opcodes, the address composition and the fact that
        0x1B6 differs from the read arm 0x1A6 in exactly the IN_OUT bit -- all
        asserted by the offline selftest.  What is not verified is the confirm
        phase's arm word (`CFG_ROW_ONLY`): the read path never needs a
        row-cycles-only command, so that value is inferred from the bit model in
        `nd_hw.py` rather than measured.  Anyone arming this for real should
        settle that on a sacrificial die first, not on this one.
        """
        row = self._row(block, page, lun, slc)
        return [
            Cmd(0x80, ce, lun, row=row, col=0, cfg=CFG_PROGRAM,
                nbytes=len(data), note="serial data input + address"),
            Cmd(0x10, ce, lun, row=row, cfg=CFG_ROW_ONLY,
                note="program confirm; wait R/B"),
            Cmd(0x70, ce, lun, cfg=CFG_DATA_OUT, nbytes=4,
                note="read status; bit0 == 0 -> pass"),
        ]

    def _erase_sequence(self, ce, block, lun=0):
        """Block erase.  -> list[Cmd]

            0x60 + 3 row cycles (block address, page bits ignored)
            0xD0 confirm, wait ready
            0x70 read status

        Same standing as `_program_sequence`: never executed, shape verified,
        confirm-phase arm word inferred.  Note also that MioInit table entry 16
        is a block erase over all 12 channels at row 0 -- so an erase reaching
        the media from any path in this tree is a known, catalogued hazard, not
        a hypothetical one.
        """
        row = geom.compose_row(block, 0, lun)
        return [
            Cmd(0x60, ce, lun, row=row, cfg=CFG_ERASE,
                note="erase setup; row selects the block"),
            Cmd(0xD0, ce, lun, row=row, cfg=CFG_ROW_ONLY,
                note="erase confirm; wait R/B"),
            Cmd(0x70, ce, lun, cfg=CFG_DATA_OUT, nbytes=4,
                note="read status; bit0 == 0 -> pass"),
        ]

    def _execute(self, cmds, payload=None):
        """Issue a built sequence, or log it in a dry run.  -> status byte or None.

        The dry run is not a stub: it is the tested path.  `command_log` holds
        every Cmd it built, so a test can assert on opcodes, rows and arm words
        without a single register write.
        """
        self.command_log.extend(cmds)
        if self.dry_run:
            if self.verbose:
                for c in cmds:
                    print("  DRY RUN  %r" % c)
            return None
        f = self._connect()
        from .nd_fwpage import pna_of
        st = None
        for c in cmds:
            pna = pna_of(c.ce, 0, c.col, c.row or 0)  # dev=0; lun is in the row
            if c.op == 0x80:
                f.im_setup(pna, 0x80, 0, c.nbytes)
                f.w16(CFG, c.cfg)
                f.poll(0, 0x80)
                self._push_payload(f, payload)
            elif c.op in (0x10, 0xD0, 0x60):
                f.im_setup(pna, c.op, 0, 0)
                f.w16(CFG, c.cfg)
                f.poll(0, 0x80)
                time.sleep(0.005)
            elif c.op == 0x70:
                st = f.status(pna)[0]
        return st

    def _push_payload(self, f, payload):
        """Write the data phase of a program.

        Not reachable in a dry run, and unreachable at all without both write
        opt-ins.  It is written out rather than left as a stub because a write
        path that has never been written down cannot be reviewed -- but writing
        it down is not the same as having run it, and this has never run.
        18432 bytes at one 32-bit store per word is also ~4600 uart_shell
        round-trips; a real programmer would want a device-side fill stub, the
        mirror image of `tdev_scan.c`.
        """
        if payload is None:
            raise DeviceError("program command with no payload")
        import struct
        for off in range(0, len(payload), 4):
            word = struct.unpack_from("<I", payload, off)[0]
            f.c.write32(NFIF + 0x2C, word)

    def write_page(self, ce, block, page, data, lun=0, slc=False):
        """Program one page.  Needs both opt-ins; dry-runs unless armed.

        -> status byte, or None in a dry run.  A non-None status with bit0 set
        means the die reported a program failure.
        """
        self._check_hw_write()
        data = bytes(data)
        if len(data) != PAGE_BYTES:
            raise DeviceError("a page is %d bytes, got %d"
                              % (PAGE_BYTES, len(data)))
        if not self.dry_run:
            self._ensure_mode(ce, lun, slc)
        cmds = self._program_sequence(ce, block, page, data, lun=lun, slc=slc)
        st = self._execute(cmds, payload=data)
        for k in [k for k in self._cache
                  if k[:5] == (ce, lun, block, page, bool(slc))]:
            self._cache.pop(k, None)          # every retry level of that page
        return st

    def erase_block(self, ce, block, lun=0):
        """Erase one block.  Needs both opt-ins; dry-runs unless armed."""
        self._check_hw_write()
        cmds = self._erase_sequence(ce, block, lun=lun)
        st = self._execute(cmds)
        for k in [k for k in self._cache if k[:3] == (ce, lun, block)]:
            self._cache.pop(k, None)
        return st

    # -- reporting --------------------------------------------------------
    def describe(self):
        lines = []
        try:
            ids = self.ce_status()          # connects, so ask before reporting
        except DeviceError as exc:
            lines.append("hardware: Titania NAND over uart_shell (no board)")
            lines.append("  %s" % exc)
            return "\n".join(lines)
        lines.append("hardware: Titania NAND over uart_shell (%s)"
                     % ("connected" if self._fp is not None
                        else "not connected"))
        alive = sorted(ce for ce, i in ids.items() if i is not None)
        dead = sorted(ce for ce, i in ids.items() if i is None)
        lines.append("  CEs answering READ ID : %s" % alive)
        lines.append("  CEs NOT answering     : %s%s"
                     % (dead, "  (CE0 is known dead on this board)"
                        if 0 in dead else ""))
        lines.append("  present_ces()         : %s%s"
                     % (self.present_ces(),
                        "   [%s excluded as dead]"
                        % list(geom.CE_DEAD) if self.exclude_dead else ""))
        lines.append("  cell-mode latches     : %s"
                     % ({"ce%d.%d" % k: v for k, v in self._mode.items()}
                        or "none selected yet"))
        try:
            nlev = len(self.retry_levels(False))
            lines.append("  read-retry ladder     : %d levels TLC / %d SLC "
                         "(0 = die default)"
                         % (nlev, len(self.retry_levels(True))))
        except Exception as exc:
            lines.append("  read-retry ladder     : UNAVAILABLE (%s)" % exc)
        lines.append("  retry levels held     : %s%s"
                     % ({"ce%d.%s" % (c, "slc" if s else "tlc"): v
                         for (c, s), v in sorted(self._retry.items())}
                        or "none selected yet",
                        "" if not any(self._retry.values())
                        else "   <- NOT all default; close() will restore"))
        lines.append("  drain stub            : %s"
                     % ("loaded" if self._drain_loaded else "NOT loaded (slow)"))
        lines.append("  device-side scan      : %s"
                     % {"tdev": "tdev_scan.c -- exact, all 4 PageScan fields",
                        "nd_scan": "nd_efcscan -- exact, no first_nonff",
                        None: "none; a scan costs a full page transfer"}
                     [self._scan_kind])
        lines.append("  fast erased path      : %s"
                     % ("ON -- erased pages synthesised from an EXACT "
                        "device-side verdict" if self.fast_erased
                        else "OFF -- every byte transferred"))
        lines.append("  writes                : allow_hardware_writes=%s "
                     "writes_enabled=%s dry_run=%s"
                     % (self.allow_hardware_writes, self.writes_enabled,
                        self.dry_run))
        lines.append("  counters              : %s" % self.stats)
        return "\n".join(lines)

    def __repr__(self):
        return "<HardwareDevice writes=%s dry_run=%s fast_erased=%s>" % (
            "ON" if self.writes_enabled and self.allow_hardware_writes else "off",
            self.dry_run, self.fast_erased)


# ------------------------------------------------------------------ selftest --
def _offline_checks():
    """Everything that needs no board.  -> (ok, lines)"""
    ok = True
    out = []
    dev = HardwareDevice()               # not armed, so dry_run is forced on
    out.append("[o1] default instance: writable=%s dry_run=%s"
               % (dev.writable, dev.dry_run))
    ok &= (dev.writable is False and dev.dry_run is True)

    try:
        dev.enable_writes(CONFIRM)
        out.append("[o2] enable_writes succeeded on an unarmed device -- WRONG")
        ok = False
    except WriteProtected:
        out.append("[o2] enable_writes refused without allow_hardware_writes")

    armed = HardwareDevice(allow_hardware_writes=True, dry_run=True)
    try:
        armed.write_page(6, 8, 0, b"\x00" * PAGE_BYTES, slc=True)
        out.append("[o3] write_page ran with writes_enabled=False -- WRONG")
        ok = False
    except WriteProtected:
        out.append("[o3] write_page still refused (enable_writes not called)")
    try:
        armed.enable_writes("nope")
        out.append("[o4] enable_writes took a wrong phrase -- WRONG")
        ok = False
    except WriteProtected:
        out.append("[o4] enable_writes refused a wrong confirmation phrase")

    armed.enable_writes(CONFIRM)
    st = armed.write_page(6, 8, 5, b"\xa5" * PAGE_BYTES, slc=True)
    cmds = armed.command_log[:]
    ops = [c.op for c in cmds]
    row = geom.compose_row(8, 5, 0)
    good = (ops == [0x80, 0x10, 0x70] and st is None
            and cmds[0].cfg == CFG_PROGRAM and cmds[0].row == row
            and cmds[0].nbytes == PAGE_BYTES)
    out.append("[o5] dry-run program sequence: %s  cfg=%#05x row=%#x n=%d "
               "-> issued nothing (%s)"
               % ([hex(o) for o in ops], cmds[0].cfg, cmds[0].row,
                  cmds[0].nbytes, "OK" if good else "WRONG"))
    ok &= good

    armed.command_log = []
    st = armed.erase_block(6, 8)
    cmds = armed.command_log[:]
    ops = [c.op for c in cmds]
    good = (ops == [0x60, 0xD0, 0x70] and st is None
            and cmds[0].row == geom.compose_row(8, 0, 0))
    out.append("[o6] dry-run erase sequence  : %s  row=%#x -> issued nothing (%s)"
               % ([hex(o) for o in ops], cmds[0].row, "OK" if good else "WRONG"))
    ok &= good

    # the read arms and the write arms differ in exactly one bit -- IN_OUT
    good = (CFG_PROGRAM ^ CFG_READ_CMD) == 0x10 and (CFG_ERASE & 0x10) == 0x10
    out.append("[o7] read arm %#05x vs program arm %#05x differ only in IN_OUT "
               "(bit4): %s" % (CFG_READ_CMD, CFG_PROGRAM, good))
    ok &= good

    # the reduction, checked against a brute-force reference over whole pages
    import zlib
    blank = b"\xff" * PAGE_BYTES
    written = bytes((i * 31 + 7) & 0xFF for i in range(PAGE_BYTES))
    sneaky = bytearray(blank)
    sneaky[17000] = 0x00       # One programmed byte, 92% of the way in
    sneaky = bytes(sneaky)
    s_blank, s_written, s_sneaky = (scan_bytes(blank), scan_bytes(written),
                                    scan_bytes(sneaky))
    good = (s_blank.all_ones is True and s_blank.nonff_bytes == 0
            and s_blank.first_nonff is None
            and s_written.all_ones is False
            and s_sneaky.all_ones is False and s_sneaky.nonff_bytes == 1
            and s_sneaky.first_nonff == 17000)
    out.append("[o8] full-page reduction: erased->all_ones=%s  written->%s  "
               "one byte at 17000->all_ones=%s nonff=%d first=%s  (%s)"
               % (s_blank.all_ones, s_written.all_ones, s_sneaky.all_ones,
                  s_sneaky.nonff_bytes, s_sneaky.first_nonff,
                  "OK" if good else "WRONG"))
    out.append("     a single programmed byte 92% into the page is caught -- "
               "which no sampling rule can promise, and is why there is none")
    ok &= good

    # the C computes CRC-32 reflected poly 0xEDB88320; assert that is zlib's
    good = (s_written.crc == zlib.crc32(written) & 0xFFFFFFFF
            and zlib.crc32(b"123456789") & 0xFFFFFFFF == 0xCBF43926)
    out.append("[o9] scan CRC is standard CRC-32 (zlib): check value for "
               "'123456789' = %#010x  (%s)"
               % (zlib.crc32(b"123456789") & 0xFFFFFFFF,
                  "OK" if good else "WRONG"))
    ok &= good

    # the struct the payload writes must parse back to the same PageScan
    import struct
    rec = struct.pack(SCAN_FMT, PAGE_BYTES // 4, 1, 17000, s_sneaky.crc, 1,
                      *([0] * 11))
    parsed, words, _z, _a = parse_scan(rec)
    good = (parsed == s_sneaky and words == PAGE_BYTES // 4)
    out.append("[o10] struct trec round trip: parse_scan(record) == "
               "scan_bytes(page)  (%s)" % ("OK" if good else "WRONG"))
    ok &= good
    rec_all_ff = struct.pack(SCAN_FMT, PAGE_BYTES // 4, 0, NO_NONFF,
                             zlib.crc32(blank) & 0xFFFFFFFF, 0, *([0] * 11))
    p2 = parse_scan(rec_all_ff)[0]
    ok &= (p2.all_ones is True and p2.first_nonff is None)
    out.append("     first_nonff sentinel %#x decodes to None, not offset 0"
               % NO_NONFF)

    # device_scan=False must make the shortcut unreachable, with no board
    strict = HardwareDevice(device_scan=False)
    out.append("[o11] device_scan=False -> scan_page %r / probe_erased %r "
               "without touching the board"
               % (strict.scan_page(6, 8, 0, slc=True),
                  strict.probe_erased(6, 8, 0, slc=True)))
    ok &= strict.scan_page(6, 8, 0, slc=True) is None
    ok &= strict.probe_erased(6, 8, 0, slc=True) is None

    # opcode inventory: nothing destructive can be produced by a read path
    ro = HardwareDevice()
    ro.dry_run = True
    _ = ro._program_sequence, ro._erase_sequence      # they exist, and are guarded
    out.append("[o12] destructive opcodes %s / %s are reachable ONLY through "
               "write_page/erase_block, both behind _check_hw_write()"
               % ([hex(o) for o in WRITE_OPCODES],
                  [hex(o) for o in ERASE_OPCODES]))
    src = SCAN_SRC.read_text() if SCAN_SRC.exists() else ""
    bad = [t for t in ("0x80", "0x10u", "0x60", "0xD0", "0xEF", "0xD5")
           if ("REG16(nfif + 0x022) = (uint16_t)" + t) in src]
    out.append("[o13] %s issues no NAND command at all (no write to a command "
               "port); destructive opcodes found in it: %s"
               % (SCAN_SRC.name, bad or "none"))
    ok &= not bad

    # -- the retry ladder, all of which is checkable with no board ----------
    rl = HardwareDevice()
    tlc_lv, slc_lv = list(rl.retry_levels(False)), list(rl.retry_levels(True))
    good = (tlc_lv[0] == 0 and slc_lv[0] == 0 and len(tlc_lv) == 33
            and len(slc_lv) == 33)
    out.append("[o14] retry ladder: %d TLC levels %s..%s, %d SLC -- level 0 is "
               "the die default (%s)"
               % (len(tlc_lv), tlc_lv[0], tlc_lv[-1], len(slc_lv),
                  "OK" if good else "WRONG"))
    ok &= good

    p0 = rl.retry_params(0, slc=False)
    p1 = rl.retry_params(1, slc=False)
    ps = rl.retry_params(1, slc=True)
    good = (set(p0) == {0x89, 0x8A} and set(ps) == {0x8D}
            and all(v == b"\x00\x00\x00\x00" for v in p0.values())
            and p1 != p0 and all(len(v) == 4 for v in p1.values()))
    out.append("[o15] level 0 writes %s to 0x89/0x8A; level 1 writes %s; "
               "pseudo-SLC uses 0x8D alone (%s)"
               % (p0[0x89].hex(), p1[0x89].hex(), "OK" if good else "WRONG"))
    ok &= good

    # every ladder step must be a real 4-byte payload for every declared
    # feature -- a short or missing one would be silently padded with zeros,
    # i.e. would read as "default" and quietly do nothing
    holes = [(m, lv) for m in (False, True)
             for lv in rl.retry_levels(m)
             for v in rl.retry_params(lv, m).values() if len(v) != 4]
    out.append("[o16] all %d ladder payloads are exactly 4 bytes: %s"
               % (2 * 33, "yes" if not holes else "NO -- %s" % holes[:4]))
    ok &= not holes

    pf = rl._ladder()
    vb, note = pf.verify_baked()
    out.append("[o17] baked ladder vs the DRAM image: %s" % note)
    ok &= (vb is not False)

    try:
        rl.retry_params(33, slc=False)
        out.append("[o18] level 33 was accepted -- WRONG")
        ok = False
    except ValueError:
        out.append("[o18] a level past the end of the ladder is refused, not "
                   "clamped to the last step")

    # an unarmed device must refuse a level rather than reach the bus
    try:
        rl.set_retry_level(99)
        out.append("[o19] an out-of-ladder level was accepted -- WRONG")
        ok = False
    except DeviceError:
        out.append("[o19] set_retry_level(99) refused before touching the bus")

    # level 0 on a fresh instance issues nothing: the die is on its power-on
    # references, so a plain read-only session emits no 0xEF at all
    fresh = HardwareDevice(ces=[6])
    fresh.set_retry_level(0, ce=6, slc=True)
    good = (fresh.stats["retry_switches"] == 0
            and fresh._retry_touched is False)
    out.append("[o20] level 0 on an untouched target issues no SET FEATURES "
               "(switches=%d touched=%s) -- a read-only session stays byte-for-"
               "byte what it was (%s)"
               % (fresh.stats["retry_switches"], fresh._retry_touched,
                  "OK" if good else "WRONG"))
    ok &= good

    # the base Device must say "not supported", not silently answer 0
    from titania.dumpdev import DumpDevice as _DD
    good = (_DD.supports_retry is False and HardwareDevice.supports_retry is True)
    out.append("[o21] supports_retry: DumpDevice=%s HardwareDevice=%s (%s)"
               % (_DD.supports_retry, HardwareDevice.supports_retry,
                  "OK" if good else "WRONG"))
    ok &= good

    # PageRead grew a field; every two-argument construction must still mean
    # "read at the default level"
    good = (PageRead(b"", FULL).retry == 0
            and PageRead(b"", FULL, 7).retry == 7)
    out.append("[o22] PageRead.retry defaults to 0, so existing 2-arg "
               "constructions keep their exact meaning (%s)"
               % ("OK" if good else "WRONG"))
    ok &= good
    return ok, out


def _selftest(offline=False, live_ce=None):
    ok = True
    print("titania.hwdev -- selftest\n")
    o_ok, lines = _offline_checks()
    for ln in lines:
        print(ln)
    ok &= o_ok

    if offline:
        print("\n[live] --offline given -- hardware checks SKIPPED")
        print("\nSELFTEST:", "PASS (offline subset)" if ok else "FAIL")
        return 0 if ok else 1

    # ---- live, read-only -------------------------------------------------
    print("")
    dev = HardwareDevice(verbose=True)
    try:
        dev._connect()
    except DeviceError as exc:
        print("[l1] no board reachable (%s) -- hardware checks SKIPPED" % exc)
        print("\nSELFTEST:", "PASS (offline subset)" if ok else "FAIL")
        return 0 if ok else 1

    try:
        ids = dev.ce_status()
        alive = sorted(c for c, i in ids.items() if i is not None)
        print("[l1] READ ID: %d/%d channels answer, alive=%s dead=%s"
              % (len(alive), geom.CE_COUNT, alive,
                 sorted(set(range(geom.CE_COUNT)) - set(alive))))
        print("     present_ces() = %s  (CE0 excluded: %s)"
              % (dev.present_ces(), 0 not in dev.present_ces()))
        ok &= 0 not in dev.present_ces()
        ok &= len(alive) >= 6

        # [l2] cross-validate a full page against the dump that recorded it
        from titania.dumpdev import DumpDevice
        root = config.dump_path()
        dd = DumpDevice(root / "slc") if (root / "slc").exists() else None
        if dd is None:
            print("[l2] no <dump>/slc -- cross-validation SKIPPED")
        else:
            cand = [k for k in sorted(dd.entries)
                    if k[0] in dev.present_ces() and k[3]][:1]
            cand += [k for k in sorted(dd.entries) if k[0] in dev.present_ces()
                     and k[3] and k[2] in (100, 200)][:2]
            match = 0
            for (ce, block, page, slc) in cand:
                want = dd.read_page(ce, block, page, slc=slc)
                got = dev.read_page_ex(ce, block, page, slc=slc)
                same = got.data == want
                match += bool(same)
                diff = sum(1 for a, b in zip(got.data, want) if a != b)
                print("[l2] ce%d b%d p%d SLC: hw==dump %s (source=%s, %d/%d "
                      "bytes differ)" % (ce, block, page, same, got.source,
                                         diff, PAGE_BYTES))
            print("     %d/%d pages byte-identical to the stored dump"
                  % (match, len(cand)))
            ok &= match == len(cand)

        # [l3] column-addressed AU read must equal the slice of the full page
        ce, block, page, slc = cand[0] if dd is not None else \
            (dev.present_ces()[0], 8, 0, True)
        full = dev.read_page(ce, block, page, slc=slc)
        dev._cache.clear()
        aus = dev.read_aus(ce, block, page, [1], slc=slc)
        same = aus[1] == full[geom.AU_STORED:2 * geom.AU_STORED]
        print("[l3] read_aus([1]) == page slice: %s  (%d bytes moved instead of "
              "%d)" % (same, geom.AU_STORED, PAGE_BYTES))
        ok &= same

        # [l4] the device-side reduction against the page's actual bytes
        dev._cache.clear()
        print("[l4] scan stub in use: %s" % dev._scan_kind)
        s = dev.scan_page(ce, block, page, slc=slc)
        ref = scan_bytes(full)
        if s is None:
            print("     no reduction stub loaded -- device scan SKIPPED")
        else:
            fields = ["all_ones", "nonff_bytes", "crc"]
            if dev._scan_kind == "tdev":
                fields.append("first_nonff")
            agree = all(getattr(s, f) == getattr(ref, f) for f in fields)
            print("     device : all_ones=%s nonff=%d first=%s crc=%#010x"
                  % (s.all_ones, s.nonff_bytes, s.first_nonff, s.crc))
            print("     host   : all_ones=%s nonff=%d first=%s crc=%#010x"
                  % (ref.all_ones, ref.nonff_bytes, ref.first_nonff, ref.crc))
            print("     %s over %s -- 64 B on the wire instead of %d (%dx)"
                  % ("AGREE" if agree else "DISAGREE", fields, PAGE_BYTES,
                     PAGE_BYTES // SCAN_REC))
            ok &= agree

        # [l4b] a genuinely ERASED page, if the dump knows of one on a live CE
        er = None
        erdev = None
        if root.exists():
            from titania.dumpdev import find_erased_page
            erdev = DumpDevice(root)
            er = find_erased_page(erdev, ces=set(dev.present_ces()))
        if er is None:
            print("[l4b] no known-erased page on a live CE -- SKIPPED")
        else:
            ce_e, b_e, p_e, s_e = er
            se = dev.scan_page(ce_e, b_e, p_e, slc=s_e)
            pr = dev.read_page_ex(ce_e, b_e, p_e, slc=s_e)
            strict = HardwareDevice(fast_erased=False, cache_pages=0)
            strict._fp, strict._client = dev._fp, dev._client
            strict._scan_entry, strict._scan_kind = (dev._scan_entry,
                                                     dev._scan_kind)
            strict._mode = dev._mode
            slow = strict.read_page_ex(ce_e, b_e, p_e, slc=s_e)
            same = pr.data == slow.data
            print("[l4b] ERASED page ce%d b%d p%d: device all_ones=%s  "
                  "fast(%s) == full(%s): %s"
                  % (ce_e, b_e, p_e, se.all_ones if se else None,
                     pr.source, slow.source, same))
            ok &= same and (se is None or se.all_ones is True)

        # [l5] mode elision: a second read of the same CE must not re-select
        before = dev.stats["mode_switches"]
        dev._cache.clear()
        dev.read_page(ce, block, page, slc=slc)
        print("[l5] mode switches during a second same-mode read: %d (expect 0)"
              % (dev.stats["mode_switches"] - before))
        ok &= dev.stats["mode_switches"] == before

        # [l6] a native-TLC page, which requires the 0xDF transition
        tlc_dir = root / "tlc"
        tlc = DumpDevice(tlc_dir) if tlc_dir.exists() else None
        if tlc is None or not tlc.entries:
            print("[l6] no <dump>/tlc reference -- TLC check SKIPPED")
        else:
            tk = [k for k in sorted(tlc.entries) if k[0] in dev.present_ces()]
            if not tk:
                print("[l6] no TLC reference on a live CE -- SKIPPED")
            else:
                ce2, b2, p2, _ = tk[0]
                want = tlc.read_page(ce2, b2, p2, slc=False)
                sw = dev.stats["mode_switches"]
                dev._cache.clear()
                got = dev.read_page(ce2, b2, p2, slc=False)
                diff = sum(1 for a, b in zip(got, want) if a != b)
                switched = dev.stats["mode_switches"] - sw
                print("[l6] native TLC ce%d b%d p%d: %d/%d raw bytes differ "
                      "from the dump, %d mode switch(es)"
                      % (ce2, b2, p2, diff, PAGE_BYTES, switched))
                # Byte equality is the wrong test here, and asserting it was a
                # bug in this selftest.  A native-TLC raw read on this board is
                # not bit-stable: two consecutive reads of the same page differ
                # by ~60 of 18432 bytes (measured), and nd_tlc.py's own read
                # path differs from this one by the same amount -- i.e. the
                # jitter is the media, not the driver.  ECC absorbs it.
                #
                # MCRC is the discriminator that matters, and it is sharp:
                # right mode -> 4/4 with a few tens of corrected bits; wrong
                # mode -> 0/4 with ~1000 syndromes per codeword.  So this
                # asserts MCRC and a small raw delta, not equality.
                ok &= switched >= 1               # the 0xDF is not optional
                ok &= diff < PAGE_BYTES // 100    # jitter, not a wrong page
                try:
                    from titania import codec
                    r_hw = codec.decode_page(got, p2)
                    r_dump = codec.decode_page(want, p2)
                    n_hw = sum(1 for a in r_hw.aus if a.mcrc_ok)
                    n_dp = sum(1 for a in r_dump.aus if a.mcrc_ok)
                    same_user = all(a.user == b.user for a, b
                                    in zip(r_hw.aus, r_dump.aus))
                    print("     MCRC hw %d/4, dump %d/4; DECODED user data "
                          "identical: %s" % (n_hw, n_dp, same_user))
                    print("     (raw TLC reads jitter; the corrected content is "
                          "what must match, and MCRC 4/4 is what proves the "
                          "cell mode was right -- a wrong-mode read gives 0/4)")
                    ok &= (n_hw == 4 and same_user)
                except Exception as exc:
                    print("     codec unavailable (%s) -- decoded comparison "
                          "SKIPPED; raw delta alone was checked" % exc)

        # [l6b] the read-retry ladder, end to end, on a native-TLC upper page
        rce = dev.present_ces()[0]
        lvl0, raw0 = dev.probe_retry_level(rce, slc=False)
        print("[l6b] ce%d retry registers at rest: %s -> level %s"
              % (rce, {"%02X" % k: v.hex() for k, v in raw0.items()}, lvl0))
        ok &= lvl0 == 0
        # pages 8..1087 sit three to a word line; (p-8) % 3 == 2 is the upper
        # page, the one that does not read at the default references
        upper = 10
        try:
            bad = dev.read_page_ex(rce, 3608, upper, slc=False, retry=0)
            d0_ok, d0 = dev.page_decodes(bad.data, upper)
            got = dev.read_page_retry(rce, 3608, upper, slc=False,
                                      levels=[0, 1, 6, 15])
            g_ok, g = dev.page_decodes(got.data, upper)
            print("     upper page 10: level 0 -> mcrc %d/4 ecc %d/4 (%s); "
                  "ladder -> level %d, mcrc %d/4 ecc %d/4 (%s)"
                  % (d0["mcrc"], d0["ecc"], "decodes" if d0_ok else "FAILS",
                     got.retry, g["mcrc"], g["ecc"],
                     "decodes" if g_ok else "FAILS"))
            ok &= g_ok and got.retry > 0 and not d0_ok
            print("     PageRead.retry carries the level that worked, so a "
                  "caller folding this page cannot lose the provenance")
        except DeviceError as exc:
            print("     ladder walk skipped/failed: %s" % exc)
        lvl1, raw1 = dev.probe_retry_level(rce, slc=False)
        print("     after the walk, GET FEATURES reads %s -> level %s "
              "(restored by the walk's own finally)"
              % ({"%02X" % k: v.hex() for k, v in raw1.items()}, lvl1))
        ok &= lvl1 == 0

        # [l7] leave every touched die back in pseudo-SLC, the resting state
        for (c_, l_), m in list(dev._mode.items()):
            if m != MODE_SLC:
                dev._select_mode(c_, l_, MODE_SLC)
        print("[l7] cell-mode latches on exit: %s"
              % {"ce%d.%d" % k: v for k, v in dev._mode.items()})
        ok &= all(v == MODE_SLC for v in dev._mode.values())
        print("     counters: %s" % dev.stats)
    finally:
        dev.close()
        print("[l8] sequencer restarted, mode 2 restored, port closed")

    print("\nNo program (0x80/0x10) or erase (0x60/0xD0) opcode was issued: the "
          "\n     write path ran dry-run only.")
    print("\nSELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest(offline="--offline" in argv)
    dev = HardwareDevice()
    try:
        print(dev.describe())
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
