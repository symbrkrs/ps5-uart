#!/usr/bin/env python3
"""`Device` backed by the Ethernet transport and the on-target page loop.

WHY THIS EXISTS
`HardwareDevice` drives the NFIF from the host: every page costs ~30 register
round trips of setup before any data moves, plus the FIFO drain. That is fine
over UART, where the link is the wall anyway (42.1 KB/s measured against a
46.1 KB/s line rate). Over Ethernet the round trips become the wall instead, so
the whole per-page sequence was moved onto the target and the drain now writes
NAND words straight into the MAC's frame buffers -- the reply to the command is
the page data. Measured on this bench:

    UART, host-driven                    ~42 KB/s
    GbE, staged (cmd 18 + bulk read)    ~290 KB/s
    GbE, fused stream (cmd 21)         ~1760 KB/s
    ... with burst=16 FIFO pops        ~2588 KB/s

`read_block` is where the win is: one command per block instead of 1104.

WHAT IT DOES NOT CHANGE
Read-only, exactly like the paths it replaces: the only opcodes issued are
0x00/0x30 (read), 0x70 (READ STATUS) and the 0xDA pseudo-SLC prefix. There is no
program or erase path here at all, so `write_page`/`erase_block` stay unimplemented
rather than write-protected.

CELL MODE IS NOT OPTIONAL
A written native-TLC page reads entirely 0xFF under the 0xDA prefix, and a
pseudo-SLC page reads entirely 0xFF without it, so `slc=` selects what you get and
a wrong value looks exactly like an erased block. Take it from
`Ftl.cell_mode_of_block(block, ce)` -- it is a per-WERU configuration property,
not a block-number rule -- and only treat a block as erased when both modes agree.

NOT AVAILABLE, AND WHY
The NAND DMA/sequencer path is unusable from here. The sequencer only fetches
descriptors in ROM-time context, and the NAND is not readable at all in ROM-time
context; see the `nand-seq-fetch-blocker` and `nand-seq-descriptor-stall` notes,
and `nand-open-items` which states the pair as an impossibility. Cache read
(0x31/0x3F) is implemented on the target and measures identically to plain reads
(1759 vs 1763 KB/s) because tR is not what the drain waits on -- that retracts the
note in `HardwareDevice.read_block` calling it merely unexposed.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from . import geometry as geom
from .device import Device, DeviceError

PAGE_BYTES = 18432
NFIF = 0x1700E000
SLC_BIT = 0x40000000
LUN_SHIFT = geom.LUN_SHIFT
BLOCK_SHIFT = geom.BLOCK_SHIFT

#: Words popped per FIFO-ready check. The poll and the pop are one NFIF access
#: each (~261 cycles) and together they were 3.88 ms of a 10.2 ms page. 16 is
#: 1.9x faster than checking every word and validated two ways: the byte
#: distribution is unchanged (longest repeated run 9 at every burst -- popping an
#: empty FIFO would show as runs of a stale word) and an erased page, whose
#: content is deterministic, returns exactly 100% 0xFF at burst 1 and 16 alike.
BURST = 16

#: CE0 is dead on this board (`a7 00 ...`) and must never be targeted.
DEAD_CES = (0,)


class GbeDevice(Device):
    """Read-only `Device` over the GbE transport."""

    #: Per-attempt patience for one `nand_stream`, in seconds.
    #:
    #: MEASURED, not guessed. A healthy 1104-page block streams in ~9 s. When
    #: the transport wedges it returns nothing -- "short read 0/20348928" -- and
    #: only a relink recovers it; waiting does not. At timeout=90 one wedge on
    #: ce7 cost 2880.0 s before the first retry (the inner loop burns ~32 full
    #: timeouts), then relinked in 29 s and read the very same block in 12.7 s.
    #: The 54-minute "stall" that looked like slow flash on ce1 was this, twice
    #: hidden by a running-average progress line.
    #:
    #: So the patience is the bug, not the retry count. 25 s is ~2.8x headroom
    #: over a healthy block -- enough that a merely slow read is not abandoned --
    #: while cutting the cost of a wedge by 3.6x. Raise it only with evidence
    #: that a good read needed longer; a wedge never gets better on its own.
    WEDGE_TIMEOUT = 25

    def __init__(self, ces=None, spin=200000, burst=BURST,
                 timeout=WEDGE_TIMEOUT, bringup=True, cache_blocks=3,
                 log=None):
        super().__init__()
        self.spin = spin
        self.burst = burst
        self.timeout = timeout
        #: Where retries and reconnects are reported. They used to be silent,
        #: and that hid a 54-minute stall inside an otherwise healthy dump: the
        #: log showed a gently falling running average and nothing else, because
        #: `_stream` swallows up to 4 attempts of `timeout` seconds each plus a
        #: reconnect between them. An unattended multi-hour walk has to say when
        #: it is fighting the transport.
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.retries = 0
        self.reconnects = 0
        self.retry_seconds = 0.0
        self._ces = ces
        self._dev = None
        self._bringup = bringup
        #: Whole blocks promoted out of sequential access. A block is 1104 x
        #: 18432 = 20 MB, so keep few.
        self._cache = {}
        self._last = None
        #: the uart_shell client, kept so the retry ladder can reach the NFIF
        #: registers over the same link the page data comes down.
        self._client = None
        self._nfif_obj = None
        #: {(ce, slc): level} -- what each die's read references are set to now,
        #: so restore_retry_levels() knows which dies it actually touched.
        self._retry = {}
        self.cache_blocks = cache_blocks

    # -- connection -------------------------------------------------------
    def _connect(self):
        if self._dev is not None:
            return self._dev
        import tool
        import uart_client
        import titania_gbe as tg
        from . import gbebringup

        # Attach to the shell, resetting the board only if it will not answer.
        try:
            c = uart_client.Client(
                tool.Ps5UartEnum.get_port(tool.Ps5UartEnum.TitaniaFw),
                tool.Ps5UartEnum.BaudrateEfc)
            alive = c.ping()
        except Exception:                                    # noqa: BLE001
            c, alive = None, False
        if not alive:
            if not self._bringup:
                raise DeviceError("uart_shell not responding, and recovery is "
                                  "disabled (bringup=False)")
            self.log("  uart_shell not responding; resetting the board")
            c = gbebringup.recover(log=self.log)
        c.port.reset_input_buffer()
        # The NFIF window must be Device-typed or a halfword store gets widened
        # and arming +0x20 wipes the command byte at +0x22. The MPU is per-core;
        # this sets up cpu0, which is where the command handler runs.
        c.mpu_setup_cpu0()
        # Bring the MAC up if it is not up. Idempotent -- it reads MDIO_CLK
        # first and returns immediately when the link is already running -- so
        # there is no separate bring-up step to remember or to get wrong.
        if self._bringup and not gbebringup.ensure_link(c, log=self.log):
            raise DeviceError(
                "the GbE MAC did not come up. If the board has just been reset "
                "the BARs are unassigned; gbebringup.ensure_link assigns them, "
                "so a failure here is the link itself, not the configuration.")
        # Park the sequencer: it shares the flash pins with the indirect engine.
        c.write16(NFIF + 0x040, c.read16(NFIF + 0x040) & ~1)
        c.write16(NFIF + 0x03E, c.read16(NFIF + 0x03E) | 8)
        with c.PortTimeout(c.port, 60):
            self._dev, _link = tg.start_gbe(c, rx_arm=0x02000800, timeout=30.0)
        # Keep the client. The GbE device carries page DATA; the read-retry
        # ladder is SET FEATURES over the NFIF registers, which only the
        # uart_shell client can reach. Both must go down the same link -- a
        # second Client on this serial port would fight this one for the shell.
        # Without this the ladder is unreachable from here and the 238,478 pages
        # that only a retry level can read stay unread.
        self._client = c
        return self._dev

    def present_ces(self):
        if self._ces is not None:
            return sorted(self._ces)
        return [ce for ce in range(geom.CHANNEL_COUNT) if ce not in DEAD_CES]

    # -- addressing -------------------------------------------------------
    @staticmethod
    def compose_row(block, page, lun=0):
        """The wire row. A linear row sweep does not walk the device: rows
        0..65535 are blocks 0..31 of one LUN, and running off the programmed
        blocks looks exactly like the die ending."""
        return (lun << LUN_SHIFT) | (block << BLOCK_SHIFT) | page

    # -- read-retry ladder -------------------------------------------------
    # The same ladder HardwareDevice exposes, over the GbE link instead of the
    # UART. It is worth having here rather than only there because a whole-drive
    # retry pass is 238,478 pages: at the UART's ~2.4 KB/s that is weeks, and
    # over GbE it is hours.
    #
    # MEASURED, on this very board (`qc_ladder_run1.log`, ce1 block 852): pages
    # that fail at level 0 come back with MCRC 4/4 at step 1, and the ones that
    # miss step 1 land at step 6. So "level 0 cannot read it" is a statement
    # about the read, not about the media -- which is why this exists.
    #
    # Read-only. SET FEATURES (0xEF) writes a VOLATILE mode register inside the
    # die; it cannot alter stored charge. `pr_setfeat` proves that empirically by
    # reading the register back with 0xEE, restoring it, and re-reading.
    @staticmethod
    def _ladder():
        from . import pr_setfeat
        return pr_setfeat

    def _nfif(self):
        """An Nfif over the client this device already holds."""
        from .nd_nfif import Nfif

        self._connect()                      # ensures self._client exists
        if self._client is None:
            raise DeviceError("no uart_shell client: the retry ladder needs "
                              "register access, not just the GbE data path")
        if self._nfif_obj is None:
            self._nfif_obj = Nfif(self._client)
        return self._nfif_obj

    def retry_levels(self, slc=False):
        """-> range of selectable levels.  0 is the die's default references."""
        return self._ladder().levels_for(bool(slc))

    def retry_params(self, level, slc=False):
        """-> {feature address: 4 payload bytes} for one level."""
        return self._ladder().params_for(int(level), bool(slc))

    #: Device.supports_retry is the flag callers branch on; this backing has a
    #: real ladder, so it says so rather than letting a caller discover it by
    #: catching the base class's refusal.
    supports_retry = True

    def set_retry_level(self, level, ce=None, slc=False):
        """Select a read-retry level.  -> level.  Signature from `Device`.

        (level, ce, slc) is the base class's order, not (ce, level) -- a backing
        that swapped the first two arguments would still run and would point a
        die at the wrong references, silently, for every caller written against
        the interface.

        `ce=None` means every CE this device serves, matching the base contract.

        A failed SET FEATURES is not ignored: the die is then in an unknown read
        state and every later read on it is suspect, so this raises rather than
        letting a run continue against references nobody knows.
        """
        pf = self._ladder()
        f = self._nfif()
        targets = self.present_ces() if ce is None else [int(ce)]
        for c in targets:
            for feat, param in sorted(self.retry_params(level, slc).items()):
                if not pf.set_feature(f, c, feat, param):
                    raise DeviceError(
                        "SET FEATURES 0x%02X did not complete on ce%d; that "
                        "die's read references are now of UNKNOWN state -- "
                        "restore level 0 and re-read" % (feat, c))
            self._retry[(c, bool(slc))] = int(level)
        return int(level)

    def restore_retry_levels(self):
        """Put every touched die back on level 0.  -> number restored.

        Must run after a retry read. A die left shifted silently changes every
        later read on that CE, including reads this run never touches, and
        nothing downstream would have any way to know.
        """
        n = 0
        for (c, s), lvl in list(self._retry.items()):
            if lvl:
                self.set_retry_level(0, c, s)
                n += 1
        return n

    def read_page_ladder(self, ce, block, page, slc=False, lun=0,
                         accept=None, levels=None):
        """Read a page, walking the retry ladder until it decodes.

        -> (level, bytes) for the level that satisfied `accept`, or (None, None)
        when no level did. `accept(raw)` decides; the default asks the codec for
        a full MCRC pass, because "it decoded" is the only test that means the
        references were right.

        The die is always restored to level 0 before returning, on every path.
        """
        if accept is None:
            from . import codec

            def accept(raw):
                try:
                    r = codec.decode_page(raw, geom.compose_row(block, page,
                                                                lun),
                                          fmt=codec.FORMAT_A, correct=True)
                    return all(getattr(a, "mcrc_ok", False) for a in r.aus)
                except Exception:                            # noqa: BLE001
                    return False

        seq = self.retry_levels(slc) if levels is None else levels
        try:
            for level in seq:
                try:
                    self.set_retry_level(level, ce, slc)
                    raw = self.read_page(ce, block, page, lun=lun, slc=slc)
                except DeviceError:
                    continue
                if accept(raw):
                    return int(level), raw
            return None, None
        finally:
            try:
                self.restore_retry_levels()
            except Exception:                                # noqa: BLE001
                pass

    # -- reads ------------------------------------------------------------
    def _reconnect(self):
        """Drop the link and hand over again.

        The transport wedges occasionally. A multi-hour walk has to survive it,
        and waiting it out does not work: an unattended dump stalled at block 79
        with the process alive, using no CPU, blocked inside a 300 s timeout with
        the output file frozen. Tearing the link down and re-handing-over is the
        recovery that works.
        """
        self._dev = None
        self._bringup = True
        return self._connect()

    def _stream(self, ce, row, npages, slc, tries=4):
        spin = self.spin | (SLC_BIT if slc else 0)
        last = None
        t0 = time.time()
        for attempt in range(tries):
            try:
                dev = self._connect()
                d = dev.nand_stream(row, npages, PAGE_BYTES, 1 << ce, 0, 0,
                                    NFIF, spin=spin, timeout=self.timeout,
                                    burst=self.burst)
                if len(d) == npages * PAGE_BYTES:
                    if attempt:
                        self.retry_seconds += time.time() - t0
                        self.log(f"    ce{ce} row {row:#x}: recovered on "
                                 f"attempt {attempt + 1} after "
                                 f"{time.time() - t0:.1f}s")
                    return d
                last = f"short read {len(d)}/{npages * PAGE_BYTES}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            self.retries += 1
            self.log(f"    ce{ce} row {row:#x} x{npages}: attempt "
                     f"{attempt + 1}/{tries} failed at {time.time() - t0:.1f}s: "
                     f"{last}")
            if attempt + 1 < tries:
                try:
                    self._reconnect()
                    self.reconnects += 1
                    self.log(f"    ce{ce} row {row:#x}: relinked at "
                             f"{time.time() - t0:.1f}s")
                except Exception as e:
                    last = f"reconnect failed: {type(e).__name__}: {e}"
                    self.log(f"    ce{ce} row {row:#x}: {last}")
        self.retry_seconds += time.time() - t0
        raise DeviceError(f"ce{ce} row {row:#x} x{npages}: {last}")

    def read_page(self, ce, block, page, lun=0, slc=False):
        """One page -- served from a cached block once access looks sequential.

        The FTL walk (`Ftl.from_device`, the RootStorage and SystemMetadata
        readers) asks for pages one at a time, which is the one pattern the fast
        backend does nothing for: the win is `read_block`, one command per 1104
        pages instead of 1104 commands. So the second consecutive read inside a
        block promotes it -- the whole block is fetched once and the rest of the
        walk is served from memory.

        Promotion is not on the first read. A survey that samples
        page 0 of every block would otherwise pull 1104 pages to answer each one,
        which is 1104x the work for no benefit. Two reads in the same block is
        the cheapest signal that a walk, not a sample, is in progress.
        """
        key = (ce, lun, block, bool(slc))
        hit = self._cache.get(key)
        if hit is not None:
            if page < len(hit):
                return hit[page]
            raise DeviceError(f"page {page} beyond {len(hit)} cached")

        if self._last == key:
            ppb = geom.pages_per_block(slc)
            if page < ppb:
                data = self._stream(ce, self.compose_row(block, 0, lun), ppb,
                                    slc)
                pages = [data[i * PAGE_BYTES:(i + 1) * PAGE_BYTES]
                         for i in range(ppb)]
                self._cache[key] = pages
                while len(self._cache) > self.cache_blocks:
                    self._cache.pop(next(iter(self._cache)))
                return pages[page]
        self._last = key
        return self._stream(ce, self.compose_row(block, page, lun), 1, slc)

    def read_row(self, ce, row, slc=False):
        return self._stream(ce, row, 1, slc)

    def read_block(self, ce, block, lun=0, slc=False, pages=None):
        """One command for the whole block, instead of one per page.

        This is the entire point of the backend: `HardwareDevice` re-issues the
        address phase per page and pays a host round trip for each, while the
        target here walks the pages itself and streams them back as one reply.
        Yields `(page_index, data)` so it drops into the existing callers.
        """
        n = pages if pages is not None else geom.pages_per_block(slc)
        data = self._stream(ce, self.compose_row(block, 0, lun), n, slc)
        for p in range(n):
            yield p, data[p * PAGE_BYTES:(p + 1) * PAGE_BYTES]

    def drop_cache(self):
        self._cache.clear()
        self._last = None

    def read_pages(self, ce, block, first, count, lun=0, slc=False):
        """A run of pages inside one block, as one command. Runs must not cross
        a block boundary: page and block are separate fields of the row."""
        data = self._stream(ce, self.compose_row(block, first, lun), count, slc)
        return [data[i * PAGE_BYTES:(i + 1) * PAGE_BYTES] for i in range(count)]

    def block_is_erased(self, ce, block, lun=0, slc=False):
        """True only if every page reads 0xFF. Note this is a single-mode
        verdict: the other cell mode reads 0xFF for this block's data too, so a
        block is only genuinely erased when both modes agree."""
        for _p, d in self.read_block(ce, block, lun=lun, slc=slc):
            if d.count(0xFF) != PAGE_BYTES:
                return False
        return True
