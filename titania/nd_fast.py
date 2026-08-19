#!/usr/bin/env python3
"""FwPage with the FIFO drain done on the device.  Import this, don't edit nd_fwpage.

WHY
`FwPage.pop()` polls the FIFO status and pops 4 bytes per iteration, and each of those is
a uart_shell round-trip -- about 9200 per 18432-byte page.  That is the whole ~6 s per
page; the serial link's bandwidth is nowhere near saturated.  `nd_drain.c` moves the loop
onto Titania, so a page costs one `c.call()` plus one bulk read.

USE
    from .nd_fast import FastPage, load_drain
    c = connect(); f = FastPage(c); load_drain(c)
`FastPage` overrides only `pop()`, so `read_page`, `status` and `im_setup` behave exactly
as before and the two are directly comparable -- which is what `main()` checks.

Falls back to the byte-at-a-time path automatically if the stub is not loaded, so nothing
silently reads garbage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from . import armbuild
from .nd_fwpage import FwPage, pna_of, NFIF
from .nd_nfif import connect

HERE = armbuild.STUB_DIR
STUB = 0x01160000            # same slot the existing stubs use (nand_nuclear.py)
BUF = 0x01100000             # scratch known good; 0x01300000 swallows writes silently
SPIN = 200000
_loaded = False


def build():
    """Compile nd_drain.c for cortex-r5 thumb, linked at STUB.  -> blob bytes"""
    blob, _ = armbuild.compile_stub("nd_drain.c", entry="nand_drain",
                                    text_addr=STUB, cwd=HERE, timeout=60)
    return blob


def load_drain(c, verbose=True):
    """Compile, upload and verify the drain stub.  Idempotent."""
    global _loaded
    blob = build()
    c.write(STUB, blob)
    back = bytes(c.read(STUB, len(blob)))
    if back != blob:
        raise RuntimeError("stub did not read back identical -- refusing to run it")
    _loaded = True
    if verbose:
        print(f"  drain stub: {len(blob)} bytes at {STUB:#x}, buffer {BUF:#x}, verified")
    return blob


class FastPage(FwPage):
    def pop(self, nbytes, tries=60):
        if not _loaded:
            return super().pop(nbytes, tries)
        nw = nbytes // 4
        got = self.c.call(STUB | 1, NFIF, BUF, nw, SPIN)
        if got != nw:
            return b""                     # short drain: let the caller retry
        return bytes(self.c.read(BUF, nw * 4))


def main():
    import time
    import numpy as np
    from .nd_ldpc_odd import syn_even, STRIDE
    from .nd_mcrc_solve import check_au

    c = connect()
    slow = FwPage(c)
    fast = FastPage(c)
    try:
        slow.seq_stop()
        # SLC mode, as the ROM does once per CE
        fast.im_setup(pna_of(6, 0, 0, 0), 0xDA, 0, 0)
        fast.w16(NFIF + 0x20, 0x180)
        fast.poll(0, 0x80)
        load_drain(c)

        pna = pna_of(6, 0, 0, (8 << 11) | 4)
        n = 8 * STRIDE
        t0 = time.time()
        a = slow.read_page(pna, n)[2]
        t_slow = time.time() - t0
        t0 = time.time()
        b = fast.read_page(pna, n)[2]
        t_fast = time.time() - t0

        print(f"\n  byte-at-a-time : {len(a):6d} bytes in {t_slow:6.2f} s")
        print(f"  device drain   : {len(b):6d} bytes in {t_fast:6.2f} s"
              f"   ({t_slow / max(t_fast, 1e-9):.1f}x)")
        if len(a) == len(b) == n:
            same = sum(1 for x, y in zip(a, b) if x == y)
            arr = np.frombuffer(b, np.uint8)
            print(f"  identical      : {same}/{n}"
                  f"   syndrome {syn_even(arr[:STRIDE])}"
                  f"   mcrc {sum(check_au(arr, i) for i in range(4))}/4")
            print("  " + ("MATCH -- safe to use" if a == b else
                          "MISMATCH -- do NOT use the fast path"))
    finally:
        try:
            c.port.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
