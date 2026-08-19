"""The device abstraction: one interface, several backings.

Everything above this layer -- AU decode, firmware images, the FTL -- is written
against `Device` alone, so the same analysis runs against a live board, a raw
dump file, or a synthetic image built for a test.

    dev = DumpDevice("<dump>/g2")        # or HardwareDevice()
    page = dev.read_page(ce=1, block=8, page=0)      # raw, exactly as stored
    aus  = codec.decode_page(page)

WRITE SAFETY -- READ THIS BEFORE ADDING A CALLER
------------------------------------------------
`write_page` and `erase_block` are part of the interface because a NAND tool that
cannot write is only half a tool.  They are also irreversible against real
hardware and can destroy the only copy of the data this project exists to
recover.  So:

  * every Device starts with `writes_enabled = False`;
  * `enable_writes()` must be called explicitly, with the confirmation phrase,
    and it is a per-instance decision that is never persisted;
  * `DumpDevice` writes only into its own in-memory or on-disk copy;
  * `HardwareDevice` additionally refuses unless constructed with
    `allow_hardware_writes=True`, so a typo cannot reach the media.

The intent is that the write path is fully implemented, fully testable against a
dump, and impossible to fire at the board by accident.
"""
import abc
import collections

from . import geometry as geom

CONFIRM = "yes, write to the media"

#: How a page's bytes were obtained.  `read_page` hides this; `read_page_ex`
#: exposes it, because how a claim was established is part of the claim.
FULL = "full"                 # every byte crossed the wire
ERASED_DEVICE = "erased-device"     # exact all-ones verdict computed on the EFC
CACHED = "cached"
PARTIAL = "partial"           # only the requested AUs are valid

#: `retry` is the read-retry level the bytes were finally obtained at: 0 is the
#: die's default read references, n >= 1 is the n'th step of the vendor ladder.
#: It defaults to 0 so that every existing two-argument construction keeps its
#: exact meaning -- a backing with no retry ladder always read at the default.
#: It is provenance, not decoration: a page that only came back at level 17 is a
#: measurement of that block's health, and folding it into a store without
#: saying so throws that measurement away.
PageRead = collections.namedtuple("PageRead", "data source retry")
PageRead.__new__.__defaults__ = (0,)

#: Result of a device-side reduction over a full page.
#:
#: `all_ones` is exact -- every byte of the page was examined, on the EFC, and
#: only the verdict crossed the wire.  There is no sampling anywhere in this
#: design and there must not be: an inferred "blank" is exactly the error that
#: produced a false "nothing is written" sweep across 19 WERUs, and a tool whose
#: fast path can lie about emptiness is not usable for recovery.
#:
#: `nonff_bytes` and `first_nonff` cost nothing extra to compute device-side and
#: turn a boolean into a diagnosis: a page with a handful of programmed bytes is
#: a very different object from a cleanly erased one, and distinguishing them is
#: often the whole question.
PageScan = collections.namedtuple("PageScan", "all_ones nonff_bytes first_nonff crc")


class DeviceError(Exception):
    pass


class WriteProtected(DeviceError):
    pass


class Device(abc.ABC):
    """A source of (and optionally sink for) raw NAND pages.

    `read_page` returns the page exactly as stored -- scrambled, ECC-bearing,
    uncorrected.  Decoding is the codec layer's job, deliberately: keeping the
    raw bytes available is what lets a damaged page be inspected at all.
    """

    #: True when this backing can, in principle, be written
    writable = False

    def __init__(self):
        self.writes_enabled = False

    # -- capability -------------------------------------------------------
    def enable_writes(self, confirm=""):
        if not self.writable:
            raise WriteProtected("%s is read-only" % type(self).__name__)
        if confirm != CONFIRM:
            raise WriteProtected(
                "refusing to enable writes without the confirmation phrase %r"
                % CONFIRM)
        self.writes_enabled = True

    def _check_write(self):
        if not self.writes_enabled:
            raise WriteProtected(
                "writes are disabled on this %s; call enable_writes(%r) if you "
                "really mean it" % (type(self).__name__, CONFIRM))

    # -- required ---------------------------------------------------------
    @abc.abstractmethod
    def read_page(self, ce, block, page, lun=0, slc=False):
        """-> bytes, the stored page.  Raises DeviceError if unreadable."""

    @abc.abstractmethod
    def present_ces(self):
        """-> sorted list of CEs this backing can serve."""

    # -- optional ---------------------------------------------------------
    def write_page(self, ce, block, page, data, lun=0, slc=False):
        self._check_write()
        raise NotImplementedError

    def erase_block(self, ce, block, lun=0):
        self._check_write()
        raise NotImplementedError

    # -- acceleration -----------------------------------------------------
    # Reads dominate every workload here, and on the live board a page is
    # ~18 KB over a UART, so the difference between "transfer everything" and
    # "transfer what is needed" is the difference between a usable tool and an
    # overnight job.  Backings override these; the defaults are always correct,
    # merely slow, so a backing that implements none of them still works.

    #: Whether to short-circuit a read when the device reports the page erased.
    #: Safe to leave on: the verdict is exact, not inferred.  Turn it off only
    #: when you want the raw bytes of an erased page for their own sake (bit-rot
    #: studies, or checking that "erased" really reads as 0xFF on this die).
    fast_erased = True

    def read_page_ex(self, ce, block, page, lun=0, slc=False):
        """Like `read_page`, but reports how the bytes were obtained.

        Use this whenever the distinction matters -- surveys, provenance
        records, anything that will be written down as evidence.  `read_page`
        is the convenience wrapper for when it does not.
        """
        return PageRead(self.read_page(ce, block, page, lun=lun, slc=slc), FULL)

    # -- read retry -------------------------------------------------------
    # a NAND cell's stored charge is read by comparing it against a set of
    # reference voltages.  Those references are calibrated for a fresh die; as
    # charge leaks and neighbouring cells disturb each other the distributions
    # move, and a page that is perfectly intact can become unreadable purely
    # because the comparison is being made in the wrong place.  Every vendor
    # ships a ladder of alternative reference sets for exactly this, and the
    # drive firmware walks it before declaring a page lost.
    #
    # The ladder is device state, not a per-read parameter, in the same way the
    # cell-mode latch is: setting it is a command, it persists until changed,
    # and leaving it set changes what every later read returns.  So a backing
    # that implements it must also restore it.
    #
    # The default implementation says "not supported" rather than silently
    # pretending level 0 succeeded, because "this page needs a retry level we do
    # not have" and "this page read fine" are the two things a recovery tool
    # must never confuse.

    #: True when this backing can change the die's read reference voltages.
    supports_retry = False

    def retry_levels(self, slc=False):
        """Retry levels this backing can select.  -> sequence; () if none.

        Level 0 is always the die's default references.  Levels >= 1 are the
        vendor ladder, in the vendor's own order -- which is not monotonic in
        voltage and must not be reordered or interpolated.
        """
        return ()

    def set_retry_level(self, level, ce=None, slc=False):
        """Select a read-retry level.  Raises unless `supports_retry`."""
        if level:
            raise DeviceError(
                "%s has no read-retry ladder; only level 0 (the die's default "
                "read references) exists here" % type(self).__name__)
        return 0

    def current_retry_level(self, ce, slc=False):
        return 0

    def restore_retry_levels(self):
        """Put every touched target back on level 0.  -> number restored."""
        return 0

    def scan_page(self, ce, block, page, lun=0, slc=False):
        """Exact full-page reduction, computed as close to the media as possible.

        -> PageScan, or None if this backing has no reduction primitive.

        The point is to move the *decision* to the device and leave the bytes
        where they are.  On hardware the page is read into EFC SRAM and reduced
        there, so all 18432 bytes are examined and one word crosses the UART --
        exact and ~4 orders of magnitude cheaper than transferring the page.

        Never implement this by sampling.  An approximate "blank" is worse than
        no fast path at all, because the error is silent and self-confirming:
        a survey that skips pages it wrongly believes empty reports exactly the
        emptiness it assumed.
        """
        return None

    def probe_erased(self, ce, block, page, lun=0, slc=False):
        """Is the page erased?  -> True / False / None.

        Exact where `scan_page` is implemented; None means this backing cannot
        answer without a full transfer, in which case the caller should just
        read the page.
        """
        s = self.scan_page(ce, block, page, lun=lun, slc=slc)
        return None if s is None else s.all_ones

    def read_aus(self, ce, block, page, aus, lun=0, slc=False):
        """Read only the listed AU indices.  -> {index: bytes}

        A caller that wants one AU of a page should not pay for four.  The
        default reads the whole page and slices, so it is correct everywhere;
        backings with column-addressed reads should override it.
        """
        data = self.read_page(ce, block, page, lun=lun, slc=slc)
        return {i: data[i * geom.AU_STORED:(i + 1) * geom.AU_STORED]
                for i in aus}

    def block_is_erased(self, ce, block, lun=0, slc=False):
        """Guess whether a whole block is erased.  -> True / False / None.

        Cheap because NAND is programmed in page order within a block: if the
        first and last pages are both erased, the block almost certainly is.
        'Almost' is why this returns a guess and why the docstring says guess --
        an interrupted program leaves a partially written block, and that is
        exactly the case a recovery tool must not skip.  Returns None when it
        cannot tell.
        """
        last = geom.pages_per_block(slc) - 1
        a = self.probe_erased(ce, block, 0, lun=lun, slc=slc)
        if a is not True:
            return a
        return self.probe_erased(ce, block, last, lun=lun, slc=slc)

    # -- convenience ------------------------------------------------------
    def read_row(self, ce, row, slc=False):
        block, page, lun = geom.split_row(row)
        return self.read_page(ce, block, page, lun=lun, slc=slc)

    def read_block(self, ce, block, lun=0, slc=False, pages=None):
        """Iterate (page, bytes) over a block, skipping unreadable pages.

        Unreadable pages are SKIPPED rather than yielded as None so that callers
        cannot accidentally treat a read failure as erased (all-0xFF) content --
        a confusion that has produced wrong conclusions in this project before.
        Use `read_page` directly when you need to see the failures.
        """
        n = pages if pages is not None else geom.pages_per_block(slc)
        for p in range(n):
            try:
                yield p, self.read_page(ce, block, p, lun=lun, slc=slc)
            except DeviceError:
                continue

    def __repr__(self):
        return "<%s ces=%s writes=%s>" % (
            type(self).__name__, self.present_ces(),
            "ON" if self.writes_enabled else "off")
