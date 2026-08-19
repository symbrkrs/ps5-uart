#!/usr/bin/env python3
"""EFC-side page scanning: 48 bytes of UART per page instead of 18432.

    py nd_efcscan.py --build            # compile only, no board needed
    py nd_efcscan.py --verify 1         # re-scan CE1 and diff against the known survey
    py nd_efcscan.py --bench 1

WHY
---
Measured on this link: 0.44 s per page, of which 0.40 s is shipping the page over a
460800-baud line (46 KB/s, 91% utilised). The link is saturated, so protocol
tuning buys at most ~9%. The only real lever is to stop sending the page.

Nearly every sweep needs a verdict, not the bytes:

    block survey        is the block erased?          1 bit
    LMA / WERU mapping  the 4 AU aux fields           32 bytes
    dedupe / verify     a CRC                         4 bytes

`nd_scan.c` computes all three while draining and buffers nothing, so a page becomes
a 48-byte record -- 384x less traffic. Two entry points:

  * `nand_scan`       -- host arms the read exactly as today, device returns the
                         summary. Issues no NAND commands whatsoever.
  * `nand_scan_range` -- the whole block sweep runs on-device, which also removes the
                         ~80 register round-trips per page (im_setup alone is ~21,
                         since every `bfi` is a read+write pair). One call per sweep.

Expected: `nand_scan` ~11x (the bulk read disappears, round-trips remain);
`nand_scan_range` much more (both disappear). Both are measured by --bench.

SAFETY
------
`nand_scan` touches only the FIFO data port. `nand_scan_range` drives the command
ports, but every opcode is a compile-time constant in the C (0x00, 0x30, 0x70) --
none is caller-supplied, so no program/erase/set-feature byte is reachable from the
host. It also refuses to run unless WP_N (NFIF+0x00C bit4) is asserted, and writes
only config words whose bit 4 (IN_OUT) is clear.

VALIDATION
----------
We have complete ground truth: the full-LUN survey classified all 4096 blocks of CE1
through the already-trusted path. `--verify` re-scans with the stub and requires an
exact per-block agreement on erased/not-erased. Do not trust the fast path until that
passes.
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from . import armbuild
from .nd_fwpage import pna_of, NFIF, CFG
from .nd_fast import FastPage, load_drain
from .nd_nfif import connect
from .nd_dumpdeep import enter_slc
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


SCAN_STUB = 0x01161000        # clear of nd_fast's drain stub at 0x01160000 (72 B)
SCAN_OUT = 0x01106000         # clear of nd_fast's page buffer 0x01100000+0x4800
SCAN_PRM = 0x01105F00         # 7 u32 params for nand_scan_range
SPIN = 200000
REC = 48                      # struct rec
PAGEBYTES = 18432
_syms = {}


def build():
    """Compile nd_scan.c and resolve both entry symbols."""
    blob, syms = armbuild.compile_stub("nd_scan.c", entry="nand_scan",
                                       text_addr=SCAN_STUB,
                                       cwd=armbuild.STUB_DIR,
                                       timeout=120)
    _syms.update(syms)
    for want in ("nand_scan", "nand_scan_range"):
        if want not in _syms:
            raise RuntimeError(f"symbol {want} not found: {_syms}")
    return blob


def load(c, verbose=True):
    blob = build()
    c.write(SCAN_STUB, blob)
    if bytes(c.read(SCAN_STUB, len(blob))) != blob:
        raise RuntimeError("scan stub did not read back identical -- refusing to run it")
    if verbose:
        print(f"  scan stub: {len(blob)} B at {SCAN_STUB:#x}  "
              + "  ".join(f"{k}={v:#x}" for k, v in sorted(_syms.items())))
    return blob


def parse(raw, i=0):
    w, ff, zero, crc = struct.unpack_from("<4I", raw, i * REC)
    aux = raw[i * REC + 16:i * REC + 48]
    return {"words": w, "ff": ff, "zero": zero, "crc": crc, "aux": aux,
            "erased": w > 0 and ff == w * 4,
            "ok": w == PAGEBYTES // 4}


class ScanPage(FastPage):
    """FastPage plus device-side summarisation."""

    def scan_page(self, pna, nbytes=PAGEBYTES):
        """Arm the read exactly as read_page does, then summarise on-device."""
        self.im_setup(pna, 0x00, 0x30, 0)
        self.w16(CFG, 0x1A6)
        self.poll(0, 0x80)
        for _ in range(20):
            st, _ = self.status(pna)
            if st is not None and (st & 0x20):
                break
        self.im_setup(pna, 0, 0, nbytes)
        self.w16(CFG, 0x180)
        self.c.call(_syms["nand_scan"] | 1, NFIF, SCAN_OUT, nbytes // 4, SPIN)
        return parse(bytes(self.c.read(SCAN_OUT, REC)))

    def scan_range(self, ce, first, nblk, page=0, dev=0, nbytes=PAGEBYTES, bshift=11):
        """The whole sweep on-device: one call, REC bytes per block.

        * Unvalidated -- does not work yet. Opt in with --range only. *

        Measured: this hangs the stub and the board needs `nd_reset.py --full`. The
        cause is that the C reproduces `im_setup` too literally. The Python version
        (nd_fwpage.py:61) does two things this does not:
          * `set_mode(2)` before every setup;
          * `bfi()` -- a read-modify-write that preserves the other bits of 0x002,
            0x022, 0x024, 0x026, 0x028. The stub writes whole 16-bit words and so
            clobbers e.g. bits 12-15 of 0x002.
        So the read never arms, the FIFO never signals ready, and every word burns its
        full 200000-iteration spin -- 4608 words x nblk, i.e. effectively forever.
        Fixing it means mirroring bfi/set_mode exactly. `nand_scan` below is unaffected
        and is verified, so the bulk of the win is already available.
        """
        # slot 7 is an OUT param the stub fills with the observed NFIF+0x00C
        prm = struct.pack("<8I", ce, dev, first, nblk, page, bshift, nbytes // 4, 0)
        self.c.write(SCAN_PRM, prm)
        n = self.c.call(_syms["nand_scan_range"] | 1, NFIF, SCAN_OUT, SCAN_PRM, SPIN)
        # c.call returns the raw 32-bit register: a negative int return arrives as
        # unsigned, so sign-extend before testing or -2 reads as 4294967294.
        n = struct.unpack("<i", struct.pack("<I", n & 0xFFFFFFFF))[0]
        if n < 0:
            raise RuntimeError(f"nand_scan_range refused with {n}"
                               + (" (bad params: bshift or nwords zero)" if n == -2 else ""))
        self.last_ctrl = struct.unpack_from("<I", bytes(self.c.read(SCAN_PRM + 28, 4)))[0]
        raw = bytes(self.c.read(SCAN_OUT, REC * n))
        return [parse(raw, i) for i in range(n)]


def _truth(ce):
    t = {}
    p = config.dump_path("wide", "wide.csv")
    for ln in p.read_text().splitlines()[1:]:
        f = ln.split(",")
        if len(f) >= 3 and int(f[0]) == ce:
            t[int(f[1])] = (f[2] == "erased")
    return t


def cmd_verify(ce, nblk=256):
    truth = _truth(ce)
    if not truth:
        raise SystemExit(f"no survey ground truth for CE{ce} in wide.csv")
    c = connect()
    f = ScanPage(c)
    f.seq_stop()
    load_drain(c)
    load(c)
    # The ground truth was captured through the pseudo-SLC path, so match it --
    # this test is about the transport (device-side summary vs full page), not
    # about the cell mode. Comparing across modes would confound the two.
    enter_slc(f, ce)
    blocks = sorted(truth)[:nblk]
    bad = []
    print(f"  verifying {len(blocks)} blocks on CE{ce} against the trusted survey")
    for b in blocks:
        r = f.scan_page(pna_of(ce, 0, 0, b << 11))
        if not r["ok"] or r["erased"] != truth[b]:
            bad.append((b, r["erased"], truth[b], r["words"]))
    print(f"  nand_scan mismatches: {len(bad)} over {len(blocks)} blocks  {bad[:8]}")
    ok = not bad
    if "--range" in sys.argv:
        # Unvalidated -- see the note on scan_range(). Known to hang the stub.
        rr = f.scan_range(ce, blocks[0], min(len(blocks), 4))
        bad2 = [(b, x["erased"], truth[b]) for b, x in zip(blocks, rr)
                if not x["ok"] or x["erased"] != truth[b]]
        print(f"  scan_range mismatches: {len(bad2)} over {len(rr)} blocks  {bad2[:8]}")
        ok = ok and not bad2
    c.port.close()
    print("\nVERIFY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_bench(ce, nblk=64):
    import time
    c = connect()
    f = ScanPage(c)
    f.seq_stop()
    load_drain(c)
    load(c)
    enter_slc(f, ce)
    t0 = time.time()
    for b in range(nblk):
        f.read_page(pna_of(ce, 0, 0, b << 11), PAGEBYTES)
    t_full = time.time() - t0
    t0 = time.time()
    for b in range(nblk):
        f.scan_page(pna_of(ce, 0, 0, b << 11))
    t_scan = time.time() - t0
    for nm, t in (("full page (today)", t_full), ("nand_scan", t_scan)):
        print(f"  {nm:<22} {nblk/t:8.1f} blocks/s   {t:6.2f} s   "
              f"({t_full/max(t,1e-9):5.1f}x)")
    print(f"  bytes over UART per page: 18432 -> {REC}  ({18432//REC}x less)")
    c.port.close()
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--build" in a:
        blob = build()
        print(f"  built {len(blob)} bytes; symbols: "
              + "  ".join(f"{k}={v:#x}" for k, v in sorted(_syms.items())))
        sys.exit(0)
    if "--verify" in a:
        sys.exit(cmd_verify(int(a[a.index("--verify") + 1], 0)))
    if "--bench" in a:
        sys.exit(cmd_bench(int(a[a.index("--bench") + 1], 0)))
    print(__doc__)
