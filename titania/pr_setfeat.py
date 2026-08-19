#!/usr/bin/env python3
"""SET FEATURES / GET FEATURES over the indirect engine -- the read-retry primitive.

Read-only with respect to media.  The only opcodes this module can emit are
0xEE (GET FEATURES), 0xEF (SET FEATURES), 0x70 (READ STATUS) and 0x90 (READ ID).
0xEF writes a *volatile mode register* inside the die; it cannot alter stored
charge, and this module proves that empirically by reading the register back
with 0xEE, restoring it, and reading it back again.

WHERE EVERY REGISTER WRITE COMES FROM
-------------------------------------
Transcribed from the EFC boot ROM, `C:\\src\\ps5\\titania\\efc_cpu0_rom.bin.c`:

    HalNfCtrl_SetFeatureIm  @ 0xFFFE2720          (:2618)
        NFIF_ImSetDeviceChannel(addr)             -- +0x002 = dev | ch<<8
        flashIndAccessCmdPort1 = (row << 8) | 0xEF   -- +0x022, row = feature addr
        XFER_CNT_IND           = 8                   -- +0x02A
        flashIndAccessCfg |= 0x40 ; &= ~0x40         -- FIFO reset pulse
        flashIndAccessCfg  = (cfg & 0xFF40) | 0x91   -- arm: 1 addr cycle + write
        wait FIFO_READY(0x4000); FLASH_DATA_IND_15_0 = b0|b0<<8
                                 FLASH_DATA_IND_31_16 = b1|b1<<8
        wait FIFO_READY(0x4000); ... b2, b3

    HalNfCtrl_GetFeatureIm  @ 0xFFFE26C8          (:2584)
        identical, but cmd 0xEE and arm 0x81 (1 addr cycle, read), then two
        FIFO pops of two bytes each.

XFER_CNT is 8 for four payload bytes because the interface doubles every byte --
the same reason `Nfif.read_id` asks for 2n and keeps the evens (nd_nfif.py:128).
The ROM writes `b | b<<8` for exactly that reason.

Why 0x89 / 0x8A / 0x8D are the retry registers
----------------------------------------------
From the CPU1 firmware's Samsung part driver, `K9BKGD8JxA.c`
(`C:\\src\\pcie_hax\\software\\ps5_wifi\\test2\\efc_cpu1.c`):

    sub_55E0 / sub_5422 (:10226, :10092) index a 42-byte-per-step table by
    `retryStep`, panicking on `retryStep - 1 < MAX_TABLE_RETRY` (:10247) -- so
    the ladder is 32 steps.  Three tables, chosen by the page's cell mode:

        0x404A0570  cellMode 0  native TLC   feature 0x89 (4 bytes) + 0x8A (4)
        0x404A0AD8  cellMode 3  2-page/wl    feature 0x89 (4 bytes) + 0x8A (4)
        0x404A1040  cellMode 2  pseudo-SLC   feature 0x8D (4 bytes)

    Each table entry is a word list; `word >> 8 == 0x1C` is an address cycle
    (the feature address) and `word >> 8 == 0x04` is a payload byte -- which is
    exactly the shape of ONFI SET FEATURES, and is confirmed by
    `HalNfCtrl_SetFeatureDm` (efc_cpu1.c:15987) emitting literally
    `DmWriteCommandU8(0xEF); DmWriteAddressU8(pAddr->row); <4 data bytes>`.

    The consumer is `MioReadRetry.c` (`sub_40493BFE`, efc_cpu1.c:38632), which
    picks the table from `cellMode >> 5` and walks `currentRetryStep`.

The payload bytes are signed read-reference offsets.  SLC step 2 is all zeros --
i.e. the ladder's own "default" entry -- which is what makes restoration a
well-defined operation rather than a guess.

    py pr_setfeat.py --probe          # read the retry registers, prove volatile
    py pr_setfeat.py --tables         # decode the ladder from the DRAM image
"""
import struct
import sys
import time
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

NFIF = 0x1700E000
R_DEVCH = NFIF + 0x002
R_CFG = NFIF + 0x020
R_CMD1 = NFIF + 0x022
R_XFER = NFIF + 0x02A
R_DATA = NFIF + 0x02C

FIFO_RESET = 0x0040
FIFO_READY = 0x4000

CMD_SET_FEATURES = 0xEF
CMD_GET_FEATURES = 0xEE

#: feature addresses, from the K9BKGD8JxA.c ladder tables (see the module docstring)
FEAT_TLC = (0x89, 0x8A)      # native multi-level: 7 read references + 1 pad
FEAT_SLC = (0x8D,)           # pseudo-SLC: 1 read reference + 3 pad

#: the default level -- all offsets zero.  This is not an assumption: it is the
#: literal content of SLC ladder step 2, the firmware's own "no shift" entry.
DEFAULT_PARAM = b"\x00\x00\x00\x00"


def _bfi(f, addr, lsb, width, val):
    cur = f.r16(addr)
    mask = ((1 << width) - 1) << lsb
    f.w16(addr, (cur & ~mask) | ((val << lsb) & mask))


def _select(f, ce, dev=0):
    """NFIF_ImSetDeviceChannel: +0x002 = dev[7:0] | ch[11:8]."""
    f.set_mode(2)
    _bfi(f, R_DEVCH, 0, 8, dev)
    _bfi(f, R_DEVCH, 8, 4, ce)


def _wait_fifo(f, tries=200):
    for _ in range(tries):
        if f.r16(R_CFG) & FIFO_READY:
            return True
    return False


def get_feature(f, ce, feat, dev=0):
    """GET FEATURES (0xEE).  -> 4 bytes, or None if the FIFO never readied.

    Pure read.  Transcribed from HalNfCtrl_GetFeatureIm.
    """
    _select(f, ce, dev)
    f.w16(R_CMD1, ((feat & 0xFF) << 8) | CMD_GET_FEATURES)
    f.w16(R_XFER, 8)
    f.w16(R_CFG, f.r16(R_CFG) | FIFO_RESET)
    f.w16(R_CFG, f.r16(R_CFG) & ~FIFO_RESET)
    f.w16(R_CFG, (f.r16(R_CFG) & 0xFF40) | 0x81)
    raw = bytearray()
    for _ in range(2):
        if not _wait_fifo(f):
            return None
        raw += struct.pack("<I", f.c.read32(R_DATA))
    return bytes(raw[0::2])          # the interface doubles every byte


def set_feature(f, ce, feat, param, dev=0):
    """SET FEATURES (0xEF).  -> True once the payload has been pushed.

    VOLATILE.  This writes a mode register inside the die.  It is not a program
    command, takes no row address, and has no confirm code, so there is no
    sequence by which it can reach the array.  Transcribed from
    HalNfCtrl_SetFeatureIm.
    """
    param = bytes(param)
    if len(param) != 4:
        raise ValueError("SET FEATURES takes exactly 4 payload bytes, got %d"
                         % len(param))
    _select(f, ce, dev)
    f.w16(R_CMD1, ((feat & 0xFF) << 8) | CMD_SET_FEATURES)
    f.w16(R_XFER, 8)
    f.w16(R_CFG, f.r16(R_CFG) | FIFO_RESET)
    f.w16(R_CFG, f.r16(R_CFG) & ~FIFO_RESET)
    f.w16(R_CFG, (f.r16(R_CFG) & 0xFF40) | 0x91)
    for k in range(0, 4, 2):
        if not _wait_fifo(f):
            return False
        b0, b1 = param[k], param[k + 1]
        f.c.write32(R_DATA, b0 | (b0 << 8) | (b1 << 16) | (b1 << 24))
    time.sleep(0.001)                # tFEAT; the dm path delays 3 us
    return True


# ---------------------------------------------------------------- the ladder --
# The firmware's own read-retry tables, transcribed verbatim from the captured
# EFC DRAM image (`config.ddr_image()`, base 0x40000000) at the addresses named
# by K9BKGD8JxA.c (see the module docstring).  Baked in so nothing at run time
# depends on that image being present; `verify_baked()` re-parses the image and
# asserts these are byte-identical to it.
#
# Index 0 of each tuple is the firmware's retryStep 1.  Level 0 in this module's
# numbering is the default (all offsets zero) and is not in these tables.
#
# The bytes are signed read-reference offsets in the die's dac step unit: the
# TLC ladder shifts seven references at once (four in 0x89, three in 0x8A, one
# pad), the pseudo-SLC ladder shifts the single SLC reference in 0x8D.
LADDER = {
    'tlc': (
        {0x89: b'\xea\xf2\xf4\xe6', 0x8A: b'\xf6\xf3\xed\x00'},
        {0x89: b'\xe4\xee\xf1\xe1', 0x8A: b'\xf6\xf4\xf2\x00'},
        {0x89: b'\xe9\xf0\xf0\xe0', 0x8A: b'\xf3\xec\xeb\x00'},
        {0x89: b'\xe5\xf2\xf4\xe6', 0x8A: b'\xf7\xed\xf0\x00'},
        {0x89: b'\xe0\xef\xf1\xe2', 0x8A: b'\xef\xeb\xe7\x00'},
        {0x89: b'\xf5\xfc\xfc\xeb', 0x8A: b'\xff\xf8\xf7\x00'},
        {0x89: b'\xec\xfb\xfd\xed', 0x8A: b'\xfb\xf7\xf3\x00'},
        {0x89: b'\xe6\xf2\xf4\xe2', 0x8A: b'\xf1\xf6\xed\x00'},
        {0x89: b'\xf0\xfa\xfd\xec', 0x8A: b'\x02\x00\xfe\x00'},
        {0x89: b'\xe2\xf3\xf6\xf3', 0x8A: b'\xf5\xf3\xf2\x00'},
        {0x89: b'\n\x00\x04\xf4', 0x8A: b'\x05\x05\xfb\x00'},
        {0x89: b'\xe6\xf5\xf6\xf5', 0x8A: b'\xf5\xf4\xf4\x00'},
        {0x89: b'\xf2\xfe\x00\xed', 0x8A: b'\xfd\x01\xf9\x00'},
        {0x89: b'\x08\x0b\t\x0e', 0x8A: b'\x07\n\x0c\x00'},
        {0x89: b'\x11\xfb\x03\xea', 0x8A: b'\x05\x02\xf6\x00'},
        {0x89: b'\xee\xff\x02\xfe', 0x8A: b'\x01\xff\xfe\x00'},
        {0x89: b'\xea\xf9\xfb\xf5', 0x8A: b'\xf9\xf9\xf8\x00'},
        {0x89: b'\xf1\xfe\x00\xf1', 0x8A: b'\x03\xf9\xfc\x00'},
        {0x89: b'\xf2\x01\x02\x00', 0x8A: b'\x01\x00\x00\x00'},
        {0x89: b'\xf6\xfe\x00\xf1', 0x8A: b'\x02\xff\xf9\x00'},
        {0x89: b'\xe5\xfa\xfa\xf7', 0x8A: b'\xf8\xf9\xf9\x00'},
        {0x89: b'\xe4\xff\xfd\x04', 0x8A: b'\xf9\xfe\xda\x00'},
        {0x89: b'\xef\x07\x07\x06', 0x8A: b'\x05\x06\x07\x00'},
        {0x89: b'\xf1\x06\x06\x02', 0x8A: b'\x04\x05\x05\x00'},
        {0x89: b'\xf4\x07\x08\x05', 0x8A: b'\x05\x07\x07\x00'},
        {0x89: b'\xf6\x05\x07\x00', 0x8A: b'\x05\x05\x04\x00'},
        {0x89: b'\xe4\xfb\xfb\xfb', 0x8A: b'\xf9\xfa\xfb\x00'},
        {0x89: b'\xe8\xfb\xfc\xfa', 0x8A: b'\xf9\xfb\xfb\x00'},
        {0x89: b'\xee\xfd\xff\xf6', 0x8A: b'\x00\xfe\xfa\x00'},
        {0x89: b'\xe7\x00\x01\xfc', 0x8A: b'\x01\x00\xfe\x00'},
        {0x89: b'\xf0\x0b\t\x0f', 0x8A: b'\x05\n\xe6\x00'},
        {0x89: b'\xf8\x07\x06\n', 0x8A: b'\x05\x07\x07\x00'},
    ),
    'mlc': (
        {0x89: b'\xf0\x00\x00\x00', 0x8A: b'\xe2\x00\x00\x00'},
        {0x89: b'\xf0\x00\n\x00', 0x8A: b'\xe4\x00\x00\x00'},
        {0x89: b'\xdd\x00\x00\x00', 0x8A: b'\xe6\x00\x00\x00'},
        {0x89: b'\xec\x00\xd8\x00', 0x8A: b'\xe8\x00\x00\x00'},
        {0x89: b'\xe0\x00\xd8\x00', 0x8A: b'\xee\x00\x00\x00'},
        {0x89: b'\xe2\x00\x00\x00', 0x8A: b'\xf0\x00\x00\x00'},
        {0x89: b'\xe2\x00\xe2\x00', 0x8A: b'\xf2\x00\x00\x00'},
        {0x89: b'\xe6\x00\n\x00', 0x8A: b'\xf4\x00\x00\x00'},
        {0x89: b'\xda\x00\xf2\x00', 0x8A: b'\xf6\x00\x00\x00'},
        {0x89: b'\xec\x00\xe2\x00', 0x8A: b'\xf9\x00\x00\x00'},
        {0x89: b'\xf6\x00\xd6\x00', 0x8A: b'\xfe\x00\x00\x00'},
        {0x89: b'\xce\x00\x01\x00', 0x8A: b'\xfe\x00\x00\x00'},
        {0x89: b'\xd8\x00\n\x00', 0x8A: b'\x01\x00\x00\x00'},
        {0x89: b'\xd8\x00\x01\x00', 0x8A: b'\x06\x00\x00\x00'},
        {0x89: b'\xe2\x00\x00\x00', 0x8A: b'\n\x00\x00\x00'},
        {0x89: b'\xce\x00\xd8\x00', 0x8A: b'\x0c\x00\x00\x00'},
        {0x89: b'\xe0\x00\xce\x00', 0x8A: b'\x0e\x00\x00\x00'},
        {0x89: b'\xe4\x00\xf6\x00', 0x8A: b'\x10\x00\x00\x00'},
        {0x89: b'\xf4\x00\xec\x00', 0x8A: b'\x18\x00\x00\x00'},
        {0x89: b'\xda\x00\xd8\x00', 0x8A: b'\x1c\x00\x00\x00'},
        {0x89: b'\xd6\x00\xf4\x00', 0x8A: b'\x1e\x00\x00\x00'},
        {0x89: b'\xec\x00\xf4\x00', 0x8A: b'\xea\x00\x00\x00'},
        {0x89: b'\xd9\x00\xec\x00', 0x8A: b'\xec\x00\x00\x00'},
        {0x89: b'\xce\x00\xe6\x00', 0x8A: b'\xfa\x00\x00\x00'},
        {0x89: b'\xc8\x00\xec\x00', 0x8A: b'\xfc\x00\x00\x00'},
        {0x89: b'\xcd\x00\xca\x00', 0x8A: b'\x04\x00\x00\x00'},
        {0x89: b'\xd2\x00\xf5\x00', 0x8A: b'\x05\x00\x00\x00'},
        {0x89: b'\xd3\x00\xec\x00', 0x8A: b'\x02\x00\x00\x00'},
        {0x89: b'\xd2\x00\xe2\x00', 0x8A: b'\x06\x00\x00\x00'},
        {0x89: b'\xe5\x00\xec\x00', 0x8A: b'\x14\x00\x00\x00'},
        {0x89: b'\xd4\x00\n\x00', 0x8A: b'\x16\x00\x00\x00'},
        {0x89: b'\xd8\x00\xe2\x00', 0x8A: b'\x1a\x00\x00\x00'},
    ),
    'slc': (
        {0x8D: b'\xb5\x00\x00\x00'},
        {0x8D: b'\x00\x00\x00\x00'},
        {0x8D: b'\x05\x00\x00\x00'},
        {0x8D: b'\n\x00\x00\x00'},
        {0x8D: b'\x0f\x00\x00\x00'},
        {0x8D: b'\x14\x00\x00\x00'},
        {0x8D: b'\x19\x00\x00\x00'},
        {0x8D: b'\x1e\x00\x00\x00'},
        {0x8D: b'#\x00\x00\x00'},
        {0x8D: b'(\x00\x00\x00'},
        {0x8D: b'-\x00\x00\x00'},
        {0x8D: b'2\x00\x00\x00'},
        {0x8D: b'7\x00\x00\x00'},
        {0x8D: b'<\x00\x00\x00'},
        {0x8D: b'\xba\x00\x00\x00'},
        {0x8D: b'\xbf\x00\x00\x00'},
        {0x8D: b'\xc4\x00\x00\x00'},
        {0x8D: b'\xc9\x00\x00\x00'},
        {0x8D: b'\xce\x00\x00\x00'},
        {0x8D: b'\xd3\x00\x00\x00'},
        {0x8D: b'\xd8\x00\x00\x00'},
        {0x8D: b'\xdb\x00\x00\x00'},
        {0x8D: b'\xdd\x00\x00\x00'},
        {0x8D: b'\xe2\x00\x00\x00'},
        {0x8D: b'\xe7\x00\x00\x00'},
        {0x8D: b'\xec\x00\x00\x00'},
        {0x8D: b'\xf1\x00\x00\x00'},
        {0x8D: b'\xf6\x00\x00\x00'},
        {0x8D: b'\xfb\x00\x00\x00'},
        {0x8D: b'A\x00\x00\x00'},
        {0x8D: b'F\x00\x00\x00'},
        {0x8D: b'K\x00\x00\x00'},
    ),
}


# ------------------------------------------------------------------ ladder --
DDR_IMAGE = config.ddr_image()
DDR_BASE = 0x40000000
LADDER_BASES = {"tlc": 0x404A0570, "mlc": 0x404A0AD8, "slc": 0x404A1040}
LADDER_STRIDE = 42
MAX_TABLE_RETRY = 32


def parse_ladder(image, base, steps=MAX_TABLE_RETRY):
    """-> [ {feat: bytes(4)} ] for steps 1..32, from the firmware's own table.

    Word encoding, from sub_55E0 (efc_cpu1.c:10226): word[0] is the count, then
    count+1 words follow; `word >> 8 == 0x1C` starts a new feature address and
    `word >> 8 == 0x04` contributes one payload byte.
    """
    out = []
    for step in range(1, steps + 1):
        off = base - DDR_BASE + LADDER_STRIDE * step
        w = struct.unpack_from("<21H", image, off)
        cnt = w[0]
        if cnt == 0 or cnt > 20:
            break
        rec, cur = {}, None
        for x in w[1:1 + cnt + 1]:
            kind, b = x >> 8, x & 0xFF
            if kind == 0x1C:
                cur = b
                rec[cur] = bytearray()
            elif kind == 0x04 and cur is not None:
                rec[cur].append(b)
        out.append({k: bytes(v[:4].ljust(4, b"\x00")) for k, v in rec.items()})
    return out


def load_ladders():
    """-> {"tlc": [...], "mlc": [...], "slc": [...]}, or {} without the image."""
    if not DDR_IMAGE.exists():
        return {}
    img = DDR_IMAGE.read_bytes()
    return {k: parse_ladder(img, b) for k, b in LADDER_BASES.items()}


def verify_baked():
    """Re-parse the DRAM image and compare it to `LADDER`.  -> (ok, note).

    -> (None, ...) when the image is not present, which is not a failure: the
    baked copy is the run-time source of truth precisely so the image can be
    absent.  What this rules out is a baked copy that has silently drifted.
    """
    lad = load_ladders()
    if not lad:
        return None, "DRAM image %s not present; baked tables not re-checked" % DDR_IMAGE
    bad = [k for k in LADDER if tuple(lad.get(k, ())) != tuple(LADDER[k])]
    if bad:
        return False, "baked ladder differs from the image for: %s" % bad
    return True, "baked ladder == %s for all %d ladders x %d steps" % (
        DDR_IMAGE.name, len(LADDER), len(LADDER["tlc"]))


def levels_for(slc):
    """Level indices valid for a cell mode.  -> range; 0 is the default.

    Level n >= 1 is the firmware's retryStep n.  There is no level 0 in the
    firmware's numbering -- the firmware simply does not issue a SET FEATURES
    on the first attempt -- so level 0 here means "all offsets zero", which is
    both the power-on state and the state the ladder's own SLC step 2 writes.
    """
    return range(0, len(LADDER["slc" if slc else "tlc"]) + 1)


def params_for(level, slc):
    """-> {feature address: 4 payload bytes} for a level.  Level 0 = default."""
    feats = FEAT_SLC if slc else FEAT_TLC
    if level == 0:
        return {fa: DEFAULT_PARAM for fa in feats}
    rows = LADDER["slc" if slc else "tlc"]
    if not 1 <= level <= len(rows):
        raise ValueError("retry level %d out of range 0..%d"
                         % (level, len(rows)))
    rec = rows[level - 1]
    return {fa: rec.get(fa, DEFAULT_PARAM) for fa in feats}


def _signed(b):
    return struct.unpack("b", bytes([b]))[0]


def show_tables():
    lad = load_ladders()
    if not lad:
        print("no DRAM image at %s" % DDR_IMAGE)
        return 1
    for kind in ("tlc", "mlc", "slc"):
        rows = lad[kind]
        print("=== %s ladder: %d steps, features %s"
              % (kind.upper(), len(rows), sorted(rows[0]) if rows else []))
        for i, rec in enumerate(rows, 1):
            print("  step%-3d %s" % (i, "  ".join(
                "%02X:[%s]" % (fa, " ".join("%+d" % _signed(x) for x in p))
                for fa, p in sorted(rec.items()))))
    return 0


# ------------------------------------------------------------------- probe --
def probe():
    """Read the retry registers, change one, read it back, restore, re-read.

    Establishes -- on this board, not by assertion -- that the level is
    (a) readable, (b) writable, (c) restorable to the firmware's own zero entry.
    Nothing here touches a row address.
    """
    from .nd_nfif import connect
    from .nd_fast import FastPage
    c = connect()
    f = FastPage(c)
    f.seq_stop()
    ok = True
    try:
        cfg0 = f.r16(NFIF + 0x000)
        print("flashInterfacCfg at open = %#06x" % cfg0)

        # which CEs answer, and are they Samsung?  The ladder we are about to
        # use is a Samsung part table (K9BKGD8JxA.c); using it on another maker
        # would be a guess, and the whole point is not to guess.
        from .nd_fwpage import pna_of
        live = []
        for ce in range(12):
            f.im_setup(pna_of(ce, 0, 0, 0), 0x90, 0, 12)
            f.w16(R_CFG, 0x181)
            f.poll(0, 0x80)
            raw = f.pop(12)
            ident = bytes(raw[0::2][:6]) if len(raw) >= 12 else b""
            if len(ident) == 6 and ident[0] == 0xEC and len(set(ident)) > 2:
                live.append((ce, ident))
        print("live Samsung (maker 0xEC) CEs: %s"
              % [(ce, i.hex()) for ce, i in live[:3]])
        ok &= bool(live)
        if not live:
            print("no Samsung die answered -- the K9BKGD8JxA ladder does not "
                  "apply; STOPPING")
            return 1
        ce = live[0][0]

        print("\n-- GET FEATURES on ce%d --" % ce)
        base = {}
        for feat in (0x89, 0x8A, 0x8D, 0x01):
            v = get_feature(f, ce, feat)
            base[feat] = v
            print("  0x%02X -> %s" % (feat, v.hex() if v else "NO RESPONSE"))
        ok &= all(base[x] is not None for x in (0x89, 0x8A, 0x8D))

        # 0x01 is the ONFI timing-mode register: a control, not a retry level.
        # It is read only as a sanity anchor -- if every address returned the
        # same bytes the port would be telling us nothing.
        distinct = len({base[x] for x in base if base[x] is not None})
        print("  distinct values across the 4 addresses: %d  (1 would mean the "
              "port is not really decoding the address)" % distinct)

        print("\n-- SET FEATURES round trip on 0x8D (pseudo-SLC level) --")
        lad = load_ladders()
        # ladder step 4 = +10; a small, firmware-sanctioned shift
        probe_val = lad["slc"][3][0x8D] if lad else b"\x0a\x00\x00\x00"
        print("  writing %s (firmware SLC ladder step 4)" % probe_val.hex())
        set_feature(f, ce, 0x8D, probe_val)
        got = get_feature(f, ce, 0x8D)
        print("  readback %s  -> %s" % (got.hex() if got else "None",
                                        "TOOK" if got == probe_val else "DID NOT TAKE"))
        took = got == probe_val
        ok &= took

        print("  restoring %s" % DEFAULT_PARAM.hex())
        set_feature(f, ce, 0x8D, DEFAULT_PARAM)
        back = get_feature(f, ce, 0x8D)
        print("  readback %s  -> %s" % (back.hex() if back else "None",
                                        "RESTORED" if back == DEFAULT_PARAM
                                        else "NOT RESTORED"))
        ok &= back == DEFAULT_PARAM

        print("\n-- canary: ce1 block 26 page 44, pseudo-SLC, crc 0x6568b89a --")
        import zlib
        f.im_setup(pna_of(1, 0, 0, 0), 0xDA, 0, 0)
        f.w16(R_CFG, 0x180)
        f.poll(0, 0x80)
        row = (26 << 11) | 44
        data = f.read_page(pna_of(1, 0, 0, row), 18432)[2]
        crc = zlib.crc32(data) & 0xFFFFFFFF
        print("  %d bytes, crc %#010x  -> %s"
              % (len(data), crc, "MATCH" if crc == 0x6568B89A else "MISMATCH"))
        ok &= crc == 0x6568B89A

        cfg1 = f.r16(NFIF + 0x000)
        print("\nflashInterfacCfg at close = %#06x" % cfg1)
        ok &= cfg1 == cfg0
    finally:
        try:
            f.set_mode(2)
            f.seq_start()
            c.port.close()
        except Exception:
            pass
    print("\nPROBE:", "PASS" if ok else "FAIL")
    print("Opcodes issued: 0x90 READ ID, 0xEE GET FEATURES, 0xEF SET FEATURES, "
          "0xDA cell-mode select, 0x00/0x30 page read, 0x70 status.")
    print("No 0x80/0x10/0x85 (program) and no 0x60/0xD0 (erase) was issued.")
    return 0 if ok else 1


def main():
    if "--tables" in sys.argv:
        return show_tables()
    if "--probe" in sys.argv:
        return probe()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
