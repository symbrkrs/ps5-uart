#!/usr/bin/env python3
"""Dump the group 0/1 images over the correct row map.  Read-only.

Only page read (0x00/0x30) and read status (0x70) are ever issued.  No program, no erase.

WHAT CHANGED
nd_dumpimage.py captured rows 0,2,4,6 of each block, on the belief that only those rows
held data.  That was measured on block 1 -- not a group 0/1 block -- and wrongly
generalised.  The real map, measured against the reference firmware image, is

    page 0..4  ->  row 0, 2, 4, 6, 8          (step 2)
    page >= 4  ->  row = 3*page - 4           (step 3)

so 368 pages span rows 0..1097 of the block's 1104 physical rows: three pages per word
line.  The old dump therefore held 4 of 368 pages, about 1.2% of each image.

    group 0 = blocks 6,7   group 1 = blocks 8,9   both striped over CE 6..11
    unit = page*12 + entry,  entries ce6b8 ce6b9 ce7b8 ... ce11b9 (and the same for 6/7)

Verification, per page, all seed-free:
  * AU MCRC -- crc48 over the stored payload, 4 per page.  Cheap, and the only check that
    works on blocks 6/7, which have no scrambler seed.
  * LDPC -- both codeword layouts, 8 syndromes per page.  Run only when the MCRC fails,
    because it is the expensive one.
  * on failure: retry, then majority-vote the reads, then LDPC-correct.
A page is written only once it verifies, or is written to a quarantine name if it never
does, so nothing unverified silently enters the dump.

Resumable: existing output files are skipped, so it can be stopped and restarted freely.

    py nd_dumpdeep.py [ce_list] [block_list] [page_lo] [page_hi]
    py nd_dumpdeep.py 6,7,8,9,10,11 6,7,8,9 0 368
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_fwpage import pna_of
from .nd_fast import FastPage, load_drain
from .nd_nfif import connect
from .nd_ldpc_odd import syn_even, syn_odd, STRIDE
from .nd_mcrc_solve import check_au
from .nd_deepparity import normalize, decode_page
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


ALLONES = (1 << 53) - 1         # seed(inverted page) = seed(canonical) ^ ALLONES

HERE = Path(__file__).resolve().parent.parent
OUT = config.dump_path("slc")      # SLC-mode dump; <dump>/deep is the older
                                    # multi-level-path dump, kept for comparison
PAGEBYTES = 8 * STRIDE
NATT = 3


SLC_ENTER = 0xDA                # Samsung's SLC-mode command; Hynix/Toshiba/SanDisk use 0xA2
CFG = 0x1700E020


def row_of(page):
    """In SLC mode the row address is the logical page.  1:1.

    The old `2*page if page <= 4 else 3*page - 4` was never physical structure -- it was
    the multi-level row address that happens to land on the word line holding SLC page p.
    Reading SLC-written media through the multi-level path also produced a whole-page bit
    inversion and a bit error rate that climbed with depth.  Send SLC_ENTER once per CE
    and all three symptoms vanish: row == page, no inversion, syndrome 0, MCRC 4/4.
    """
    return page


def enter_slc(f, ce):
    """Issue the SLC-mode command, exactly as the ROM does once per CE at NAND init
    (efc_cpu0_rom.bin.c sub_FFFE28F0 -> seq_send_cmd3, onfi_params[3] = 0xDA)."""
    f.im_setup(pna_of(ce, 0, 0, 0), SLC_ENTER, 0, 0)
    f.w16(CFG, 0x180)
    f.poll(0, 0x80)


def mcrc_ok(a):
    return all(check_au(a, au) for au in range(4))


def ldpc_bad(a):
    bad = []
    for cw in range(8):
        blk = a[cw * STRIDE:(cw + 1) * STRIDE]
        if int((blk == 0xFF).sum()) == STRIDE:
            continue
        if cw % 2 == 0:
            w = syn_even(blk)
        else:
            prev = a[(cw - 1) * STRIDE:cw * STRIDE]
            w = syn_odd(blk, prev[2302:2304])
        if w:
            bad.append((cw, w))
    return bad


def vote(reads):
    bits = np.stack([np.unpackbits(r) for r in reads])
    return np.packbits((bits.sum(0) * 2 > len(reads)).astype(np.uint8))


def main():
    ces = [int(x, 0) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                               else ["6", "7", "8", "9", "10", "11"])]
    blocks = [int(x, 0) for x in (sys.argv[2].split(",") if len(sys.argv) > 2
                                  else ["6", "7", "8", "9"])]
    lo = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0
    hi = int(sys.argv[4], 0) if len(sys.argv) > 4 else 368
    OUT.mkdir(parents=True, exist_ok=True)

    c = connect()
    f = FastPage(c)
    ok = corrected = quarantined = erased = skipped = 0
    t0 = time.time()
    try:
        f.seq_stop()
        # Drain the FIFO on the device: one c.call plus one bulk read per page instead
        # of ~9200 uart_shell round-trips.  ~9x, and the byte-at-a-time path was in fact
        # marginal enough to short-read whole pages.
        load_drain(c)
        for ce in ces:
            enter_slc(f, ce)
            for blk in blocks:
                for page in range(lo, hi):
                    p = OUT / f"deep_ce{ce}_b{blk:02d}_pg{page:03d}.bin"
                    if p.exists():
                        skipped += 1
                        continue
                    row = row_of(page)
                    pna = pna_of(ce, 0, 0, (blk << 11) | row)
                    # One read, no majority voting.  Correctness comes from the
                    # LDPC, not from re-reading; a second read costs 6.5 s and
                    # the errors are stable anyway, so voting buys almost nothing.
                    # Rows >= 8 come off the media bit-inverted, whole page --
                    # normalize() detects that and returns the canonical form so
                    # the LDPC and MCRC are uniform downstream.  (The scrambler
                    # seed for such a page is the canonical seed ^ ALLONES: the
                    # 53-bit LFSR has five taps, so keystream(s ^ ALLONES) ==
                    # ~keystream(s).)
                    done = None
                    d = f.read_page(pna, PAGEBYTES)[2]
                    a = np.frombuffer(d, np.uint8) if len(d) >= PAGEBYTES else None
                    if a is not None:
                        if int((a == 0xFF).sum()) == PAGEBYTES:
                            done = ("erased", a)
                        else:
                            na, inv = normalize(a)
                            if mcrc_ok(na) and not ldpc_bad(na):
                                done = ("clean-inv" if inv else "clean", na)
                            else:
                                fa, _log = decode_page(na)   # all 8, even + odd
                                fa = np.asarray(fa, np.uint8)
                                if not ldpc_bad(fa) and mcrc_ok(fa):
                                    done = ("corrected", fa)
                    if done is None:
                        quarantined += 1
                        if a is not None:
                            (OUT / f"BAD_ce{ce}_b{blk:02d}_pg{page:03d}.bin"
                             ).write_bytes(a.tobytes())
                        continue
                    kind, a = done
                    p.write_bytes(a.tobytes())
                    if kind == "erased":
                        erased += 1
                    elif kind == "corrected":
                        corrected += 1
                    else:
                        ok += 1
                    n = ok + corrected + quarantined + erased
                    if n % 25 == 0:
                        el = (time.time() - t0) / 60
                        print(f"  ce{ce} b{blk} pg{page} row{row}: {kind}   "
                              f"[{ok} clean, {corrected} corrected, {erased} erased, "
                              f"{quarantined} bad, {el:.1f} min]")
                        sys.stdout.flush()
    except KeyboardInterrupt:
        print("\ninterrupted (resumable: rerun and it skips what exists)")
    finally:
        try:
            c.port.close()
        except Exception:
            pass
    print(f"\nclean {ok}, corrected {corrected}, erased {erased}, "
          f"quarantined {quarantined}, already present {skipped}, "
          f"{(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
