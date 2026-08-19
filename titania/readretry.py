"""Read-retry: move the die's read references so a page decodes.

A NAND page that will not decode is not always lost.  The die reads a cell by
comparing its charge against fixed reference voltages, and those references
drift -- with wear, with retention, with temperature.  When they sit in the
wrong place the raw bit error rate goes past what the ECC can correct and the
read fails, while the charge on the cells is still fine.  Shifting the
references and reading again recovers the page.

The controller's own firmware does this, and its ladder tables are in this
package (`pr_setfeat.LADDER`, transcribed from the firmware image and checked
byte-for-byte against it by `pr_setfeat.verify_baked`).  32 steps for native
multi-level cells and 32 for pseudo-SLC, each a set of signed offsets in the
die's dac step unit, applied with SET FEATURES on 0x89/0x8A (multi-level) or
0x8D (pseudo-SLC).  Level 0 in this module means the die's defaults.

Two things go wrong with an undecodable PAGE, not one, and fixing either alone
does not work.  Measured on one native-TLC page against a known-good capture:

    read with the die latched pseudo-SLC     74,175 of 147,456 bits wrong
    after issuing 0xDF (correct latch)        2,837 bits wrong, MCRC 0/4
    at retry level 1 as well                    620 bits wrong, MCRC 4/4

So `Ladder` always sets the CELL mode before it sets a level.  A read that
inherits whatever latch the die happens to be in is not a weaker read, it is a
read of the wrong thing -- and its failure looks exactly like worn media.

Read-only, structurally.  The only opcodes reachable from this module are
0x00/0x30 (read), 0x70 (READ STATUS), 0xEE/0xEF (get/SET FEATURES, a VOLATILE
die register that cannot alter stored charge) and the 0xDA/0xDF cell-mode
selects, which carry no address, no data and no confirm code.  There is no
program (0x80/0x10) or erase (0x60/0xD0) path in the call graph, and adding one
would be a change to what this module is.

Restore is not optional.  A die left shifted silently changes every later read
on that CE, including reads by tools that know nothing about this module.  Use
`Ladder` as a context manager, or call `restore()` from a `finally`.  The
restore is verified by 0xEE readback and reports failure rather than assuming.
"""
from __future__ import annotations

import time

from . import pr_setfeat as pf
from .device import DeviceError
from .nd_fwpage import FwPage, pna_of, CFG
from .padclient import PadClient

__all__ = ["Ladder", "MODE_SLC", "MODE_TLC", "level_order", "max_level"]

#: The cell-mode select opcodes.  Standalone bytes: no address, no data.
MODE_TLC = 0xDF          # native multi-level
MODE_SLC = 0xDA          # pseudo-SLC


def max_level(slc=False):
    """Highest retry level the firmware's table defines.  -> int"""
    return len(pf.LADDER["slc" if slc else "tlc"])


def level_order(top=None, slc=False):
    """The order to try levels in.  -> [int]

    1 first, then 0, then the rest ascending, and the order is measured rather
    than tidy:

      * Level 1 rescues the overwhelming majority.  Two independent runs put it
        at 282 of 294 and 2671 of 2682 pages.  Trying it first means the one
        expensive pass over a block usually finishes the block.
      * Level 0 -- the die's default references, but now with the cell mode
        correct -- comes second, because shifting references is not free.  A
        preflight found 13 pages that level 1 fails and a default-reference read
        does not.  Skipping 0 because "it already failed" is wrong: it failed in
        the wrong latch, which is a different read.
      * The rest of the ladder then runs over whatever is left, which is small.
    """
    top = max_level(slc) if top is None else min(int(top), max_level(slc))
    return [lv for lv in [1, 0] if lv <= top] + list(range(2, top + 1))


class Ladder:
    """Owns the die state a retry pass depends on.

    One invariant makes a PASS valid: every page must be read with the die
    latched in the mode we chose and its references at the level we believe they
    are at.  That is harder than it sounds, because the transport can reconnect
    under you -- `GbeDevice._stream` reconnects on a wedge and a reconnect
    resets the board, leaving the die at level 0 with an unknown latch.  The
    bytes still come back and still look like a successful read.  `read_span`
    watches `dev.reconnects` across every read for exactly that reason: an
    unnoticed reset produces a whole block captured at the wrong references and
    stored as though it were a retry-level read, which is worse than no data.

        with Ladder(dev, log=print) as lad:
            lad.apply(ce=1, level=1, slc=False)
            data = lad.read_span(ce=1, block=852, first=0, count=1104)
        # restore() has run, and was verified

    `recover` is a callable invoked when the board wedges; it should power-cycle
    or re-link and return.  None means "just try to reconnect", which is right
    for a link that does not have an out-of-band reset.
    """

    def __init__(self, dev, log=print, recover=None, verify=True):
        self.dev = dev
        self.log = log
        self._recover = recover
        self.verify = bool(verify)
        self._reg = None
        #: ce -> level currently programmed
        self.applied = {}
        #: (ce, slc) known to be latched on this link.  The mode select is
        #: sticky per die, so re-arming it before every level would cost real
        #: register traffic to re-assert something that cannot have changed.
        #: Cleared on connect, because a reconnect resets the board.
        self.latched = set()
        #: Dies whose references this object has moved, and so must restore.
        self.touched = set()
        self.reapplies = 0
        self.recoveries = 0
        self.read_failures = 0

    # -- lifecycle --------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    @property
    def reg(self):
        """`FwPage` over the CURRENT client, rebuilt when that client changes.

        A property and not an attribute.  Recovery replaces the
        device's client, and a cached FwPage would go on issuing register writes
        through the superseded handle.  The failure mode is not an exception --
        it is a level that was never programmed while `applied[ce]` says it was,
        which means a block read at the wrong references and stored as if it
        were a retry-level read.

        The reads are padded.  See `padclient`: on a GbE link a reply under 256
        bytes waits on the transport's retry backoff, and one feature operation
        is 8-10 two-byte reads.  Carrying each inside a 512-byte DRAM read took
        a retry-level apply from 35-90 s to 0.7-7 s on the reference board.  The
        register sequence itself is unchanged.
        """
        c = getattr(self.dev, "_dev", None)
        if c is None:
            return None
        if self._reg is None or getattr(self._reg.c, "c", None) is not c:
            pc = PadClient(c)
            pc.warmup()        # the first exchange after bring-up gets lost
            self._reg = FwPage(pc)
        return self._reg

    # -- die state --------------------------------------------------------
    def select_mode(self, ce, slc=False):
        """Latch the die: 0xDF native multi-level, or 0xDA pseudo-SLC."""
        reg = self._require_reg()
        reg.im_setup(pna_of(ce, 0, 0, 0), MODE_SLC if slc else MODE_TLC, 0, 0)
        reg.w16(CFG, 0x1A6)
        return reg.poll(0, 0x80)

    def set_level(self, ce, level, slc=False, verify=None):
        """Program one retry level on one die.  -> level

        Verified by 0xEE readback.  A die that accepts the command but still
        reads back as level 0 is the dangerous case and is raised on: it means
        every subsequent read is at the default references while this object
        believes otherwise.  A readback that merely disagrees is logged, because
        some fields are not read back verbatim by every part.
        """
        verify = self.verify if verify is None else verify
        want = pf.params_for(level, slc)
        reg = self._require_reg()
        for fa, pv in sorted(want.items()):
            if not pf.set_feature(reg, ce, fa, pv):
                raise DeviceError("SET FEATURES 0x%02X did not complete on ce%d"
                                  % (fa, ce))
        self.touched.add(ce)
        if verify:
            got = {fa: pf.get_feature(reg, ce, fa) for fa in sorted(want)}
            bad = [("%02X" % fa, want[fa].hex(), (got[fa] or b"").hex())
                   for fa in sorted(want)
                   if got[fa] is not None and bytes(got[fa]) != want[fa]]
            if bad:
                self.log("    !! ce%d level %d READBACK MISMATCH %s"
                         % (ce, level, bad))
                if level and all(bytes(got[fa] or b"") == pf.DEFAULT_PARAM
                                 for fa in want):
                    raise DeviceError(
                        "ce%d ignored the level-%d SET FEATURES and still reads "
                        "as level 0 -- every read from here would be at the "
                        "default references" % (ce, level))
        return level

    def apply(self, ce, level, slc=False, verify=None):
        """Cell mode then level, in that order.  Both, always.  See the header."""
        t = time.time()
        armed = "already"
        if (ce, bool(slc)) not in self.latched:
            armed = self.select_mode(ce, slc)
            self.latched.add((ce, bool(slc)))
        self.set_level(ce, level, slc=slc, verify=verify)
        self.applied[ce] = (level, bool(slc))
        self.log("    ce%d: 0x%02X armed=%s, level %d set in %.1f s"
                 % (ce, MODE_SLC if slc else MODE_TLC, armed, level,
                    time.time() - t))

    def reapply_all(self):
        """Re-assert every die's state after a reset.  Not verified: speed."""
        for ce, (lv, slc) in list(self.applied.items()):
            self.select_mode(ce, slc)
            self.latched.add((ce, slc))
            self.set_level(ce, lv, slc=slc, verify=False)

    # -- reading ----------------------------------------------------------
    def read_span(self, ce, block, first, count, slc=False, lun=0, tries=3):
        """One read request.  -> bytes, or None if it could not be delivered.

        Guarantees the bytes were read in the state this object set: if the
        transport reconnected during the read, the die was reset, the state is
        gone, and the data is discarded and re-read rather than returned.
        """
        for _attempt in range(tries):
            before = getattr(self.dev, "reconnects", 0)
            try:
                data = self.dev._stream(
                    ce, self.dev.compose_row(block, first, lun), count, slc)
            except DeviceError as exc:
                self.log("    ce%d b%d p%d+%d: %s"
                         % (ce, block, first, count, exc))
                self.recover()
                continue
            if getattr(self.dev, "reconnects", 0) != before:
                self.reapplies += 1
                self.log("    ce%d b%d: transport reconnected mid-read -- the "
                         "die was reset; re-applying mode + level and re-reading"
                         % (ce, block))
                self.reapply_all()
                continue
            return data
        self.read_failures += 1
        return None

    # -- recovery and restore ---------------------------------------------
    def recover(self):
        """Board wedge -> the caller's recovery hook -> relink -> re-apply."""
        self.recoveries += 1
        self.log("    !! recovery %d" % self.recoveries)
        if self._recover is not None:
            try:
                self._recover()
            except Exception as exc:                         # noqa: BLE001
                self.log("       recovery hook failed: %r" % (exc,))
        try:
            self.dev._dev = None
            self.dev._bringup = True
            self.dev._connect()
            self._reg = None
            self.latched.clear()      # the board was reset; the latch is gone
            self.reapply_all()
            self.log("       relinked and die state re-applied")
        except Exception as exc:                             # noqa: BLE001
            self.log("       relink FAILED: %r" % (exc,))

    def restore(self):
        """Level 0 on every die touched, VERIFIED by 0xEE readback.  -> bool

        Returns False rather than raising when a die cannot be restored, so a
        caller in a `finally` still completes -- but it says so, loudly, because
        a die left shifted changes every later read on that CE and nothing else
        in the system would notice.
        """
        if self.reg is None or not self.touched:
            self.log("  restore: no die was ever set; nothing to restore")
            return True
        allok = True
        for ce in sorted(self.touched):
            _lv, slc = self.applied.get(ce, (0, False))
            feats = pf.FEAT_SLC if slc else pf.FEAT_TLC
            ok, rb = False, None
            for _try in range(3):
                try:
                    self.set_level(ce, 0, slc=slc, verify=False)
                    rb = {("%02X" % fa): (pf.get_feature(self.reg, ce, fa)
                                          or b"").hex() for fa in feats}
                    ok = all(v == pf.DEFAULT_PARAM.hex() for v in rb.values())
                    if ok:
                        break
                except Exception as exc:                     # noqa: BLE001
                    self.log("  restore ce%d attempt raised: %r" % (ce, exc))
                    self.recover()
            allok &= ok
            self.log("  restore ce%-2d level 0: %s  0xEE readback %s"
                     % (ce, "VERIFIED" if ok else "*** NOT VERIFIED ***", rb))
        return allok

    # -- internals --------------------------------------------------------
    def _require_reg(self):
        reg = self.reg
        if reg is None:
            raise DeviceError(
                "no register path: the device is not connected.  Call "
                "dev._connect() (or GbeDevice with bringup=True) first.")
        return reg
