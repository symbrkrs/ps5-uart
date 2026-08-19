#!/usr/bin/env python3
"""Titania NAND — a complete, self-contained driver.

Everything here is written from scratch in Python. It calls no EFC firmware
routine and reads no firmware-owned data structure: the only inputs are MMIO
registers this module programs itself, plus constants transcribed from the
bootrom and firmware *code*. Given power, it will bring the flash interface up
from any state.

Transport is `uart_client` (32/16-bit MMIO over uart_shell); that is the debug
link, not NAND logic.

WHAT IT DOES NOT DO
    No command that can alter stored data is implemented. The only opcodes ever
    written to a command port are 0xFF (RESET), 0x90 (READ ID), 0x70 (read
    status) and 0x00/0x30 (read). Program (0x80/0x10) and erase (0x60/0xD0) are
    absent by design.

CONTROLLER BRING-UP
    `Nand.init_controller()` replays the bootrom's cold NAND initialisation,
    transcribed instruction-by-instruction from `sub_FFFE10C2` and its callees
    in the EFC cpu0 ROM. Read-modify-write masks are preserved exactly, so the
    result does not depend on what was in the registers beforehand.

    phase a  sub_FFFE10C2
        mode 0
        +0x300 &= 0xF000                        pad power-down field
        +0x080 &= 0xFF00 ; +0x082 &= ~0x000F
        +0x390 = 86,96,97,95,9D                 PHY strobe               sub_FFFE104E
        per CE: +0x280+4n &= 0xEFFE, |= 0x1000, &= 0xEFFE   reset pulse  sub_FFFE0EAA
        +0x302 = 0xA958                         pad pull up/down         sub_FFFE25A8
        per CE: +0x210+2n = (v & 0x8888) | 0x3333                        sub_FFFE0DCE
        per CE: +0x442+6n = (v & 0x8000) | 0x318C   x2, then 0x018C      sub_FFFE0BB2
        +0x32C/+0x324/+0x326/+0x328 = 0                                  sub_FFFE24F8
        +0x000 |= 0x40 ; &= ~0x40               interface reset pulse
        +0x000 &= ~0x280
        +0x080 = 0 ; +0x082 = 0
        per CE: +0x402+2n = (v & 0xF000) + 0x832    channel timing
        per CE: +0x280+4n |= 0x400
        +0x304 = (v & ~0x288) | 0xF800
        +0x008 = 0x0FFF                         FlashInterfacTmCtrl3: slowest
                                                async strobe timing (not a
                                                channel enable -- see below)
        +0x03E = 0x00A1
        +0x3C2 = (v & ~7) + 4
        +0x392 = 0x45
    phase B  +0x390 = 8E,9E,9F,9D,9D  (sub_FFFE12B2) then the +0x210 / +0x442 /
             +0x32C group again
    phase C  +0x100/+0x102/+0x104 = 0x2222      luns per device

    Then `+0x000` is set to the operating mode word and every die is RESET, so
    the dies are in their power-on state too.

flashIndAccessCfg (+0x020) bit model, from how the firmware composes it:
    bit0  send 1 address cycle (addr0)     0x181 = READ ID
    bit1  send the 2 column cycles         0x1A6 = read page: col+row+wait
    bit2  send the 3 row cycles
    bit4  direction = write / command-only 0x190 = RESET
    bit5  wait for the die to be ready
    bit6  FIFO reset (pulse)
    bit7  armed / busy, self-clearing
    bit8  start
    bit14 FIFO_READY
"""
import struct, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from uart_client import Client
from tool import Ps5UartEnum

NFIF = 0x1700E000

# --- register offsets used below ---------------------------------------------
R_MODE      = 0x000    # flashInterfacCfg: mode[1:0], FLASH_TYPE[9:8]
R_DEVCH     = 0x002    # dev[7:0], ce[11:8]
#: RETRACTED name.  `R_CHAN_EN` is a misnomer and the "12 channel enable bits"
#: reading of 0x0FFF was wrong.  NFIF+0x008 is
#: `FlashInterfacTmCtrl3AsynchroDeviceAnd` (canonical definition in
#: `nd_regmap.py:1493`; used correctly by `nd_eldorainit.py:328-331`), an
#: Indirect strobe TIMING register.  Writing 0x0FFF sets the slowest asynchronous
#: timing, which is what the bring-up wants, so the behaviour below was always
#: right for the wrong reason -- do not "fix" the value.  The old name is kept as
#: an alias only so no in-flight hardware script breaks; prefer R_FI_TMCTRL3.
R_FI_TMCTRL3 = 0x008
R_CHAN_EN    = R_FI_TMCTRL3    # deprecated alias, see above
R_IND_CFG   = 0x020
R_CMD1      = 0x022    # cmd1 | addr0<<8
R_CMD2      = 0x024    # addr1 | addr2<<8
R_CMD3      = 0x026    # addr3 | addr4<<8
R_CMD4      = 0x028    # cmd2
R_XFER      = 0x02A
R_DATA      = 0x02C
R_SEQ_CTRL2 = 0x03E
R_SEQ_CTRL1 = 0x040

CFG_ADDR1   = 0x0001
CFG_COL     = 0x0002
CFG_ROW     = 0x0004
CFG_CMDONLY = 0x0010
CFG_WAITRDY = 0x0020
CFG_FIFORST = 0x0040
CFG_ARM     = 0x0080
CFG_START   = 0x0100
CFG_READY   = 0x4000

# --- geometry (Samsung K9BKGD8J1A) -------------------------------------------
PAGE_DATA      = 0x4000
PAGE_OOB       = 0x800
PAGE_TOTAL     = PAGE_DATA + PAGE_OOB
PAGES_PER_BLOCK = 368
BLOCK_SHIFT    = 11        # row bit position of the block field
LUN_SHIFT      = 23
N_CE           = 12
SAMSUNG_ID     = bytes.fromhex("ec5e98bf84cc")

# Operating mode word. bits[1:0]=2 (sequencer-arbitrated), bits[9:8]=3
# (FLASH_TYPE 3, the DDR-capable strobe timing this board's dies answer on).
MODE_WORD = 0x038E

# The mode word the bootrom runs the sequencer at. sub_FFFE0B8C picks one of
# three interface-type fields -- 0x10C, 0x104 or 0x100 -- and this board takes
# the 0x10C branch; sub_FFFE07C0 then ors in the mode. So 0x010E = type 0x10C
# plus mode 2, and MODE_WORD above is the same thing with bits 7 and 9 added
# (the speed grade the firmware later moves to). The sequencer path is only
# known to work at the ROM's operating point, so it uses this one.
ROM_MODE_WORD = 0x010E

# Two PHY/timing sets. "rom" is what the bootrom programs on a cold part: slow
# async values, and the mode word it ends on has FLASH_TYPE 0. "fast" is the
# operating point that goes with FLASH_TYPE 3. These are constants this module
# writes itself, so neither depends on what any earlier code left behind -- the
# difference is only which speed grade the part is driven at.
TIMING = {
    "rom": {
        "r210": 0x3333,
        "r402": 0x0832,
        "r442": (0x318C, 0x318C, 0x018C),
        "r304": 0xF800,
        "r03e": 0x00A1,
        "strobe_b": (0x8E, 0x9E, 0x9F, 0x9D, 0x9D),
    },
    "fast": {
        "r210": 0x4444,
        "r402": 0x0450,
        "r442": (0x3DEF, 0x3DEF, 0x01EF),
        "r304": 0xF8AA,
        "r03e": 0x80A1,
        "strobe_b": (0x8E, 0x9E, 0x9F, 0x9D, 0x19D),
    },
}


def compose_row(block, page_in_block, lun=0):
    """row = (block << 11) | (lun << 23) | (page % 368)"""
    return ((block << BLOCK_SHIFT)
            | (lun << LUN_SHIFT)
            | (page_in_block % PAGES_PER_BLOCK))


class Nand:
    def __init__(self, client, verbose=False):
        self.c = client
        self.verbose = verbose

    # ---- primitives ---------------------------------------------------------
    # Halfword accesses are only honoured with the MPU enabled; connect() below
    # guarantees that. 32-bit reads are used for the FIFO port because an 8-bit
    # read of it returns junk over this transport.
    def r16(self, off):
        return self.c.read16(NFIF + off)

    def w16(self, off, val):
        self.c.write16(NFIF + off, val & 0xFFFF)

    def rmw(self, off, and_mask, or_val=0):
        self.w16(off, (self.r16(off) & and_mask) | or_val)

    def r32(self, off):
        return self.c.read32(NFIF + off)

    def set_mode(self, mode):
        self.rmw(R_MODE, 0xFFFC, mode & 3)

    # ---- controller bring-up ------------------------------------------------
    def _phy_strobe(self, values):
        for v in values:
            self.w16(0x390, v)

    def _channel_timing(self, t):
        """The +0x210 / +0x442 / +0x32C group, run twice by the ROM."""
        for n in range(N_CE):                       # sub_FFFE0DCE
            self.rmw(0x210 + 2 * n, 0x8888, t["r210"])
        for n in range(N_CE):                       # sub_FFFE0BB2
            base = 0x442 + 6 * n
            self.rmw(base + 0, 0x8000, t["r442"][0])
            self.rmw(base + 2, 0x8000, t["r442"][1])
            self.rmw(base + 4, 0x8000, t["r442"][2])
        self.rmw(0x32C, 0xF888, 0x0000)             # sub_FFFE24F8
        for off in (0x324, 0x326, 0x328):
            self.w16(off, 0x0000)

    def init_controller(self, mode_word=MODE_WORD, timing="fast"):
        t = TIMING[timing]
        # -- phase a ----------------------------------------------------------
        self.set_mode(0)
        self.rmw(0x300, 0xF000)                     # sub_FFFE1086
        self.rmw(0x080, 0xFF00)
        self.rmw(0x082, ~0x000F & 0xFFFF)
        self._phy_strobe((0x86, 0x96, 0x97, 0x95, 0x9D))
        for n in range(N_CE):                       # sub_FFFE0EAA
            off = 0x280 + 4 * n
            self.rmw(off, 0xEFFE)
            self.rmw(off, 0xFFFF, 0x1000)
            self.rmw(off, 0xEFFE)
        self.w16(0x302, 0xA958)                     # sub_FFFE25A8
        self._channel_timing(t)

        self.rmw(R_MODE, 0xFFFF, 0x0040)            # interface reset pulse
        self.rmw(R_MODE, ~0x0040 & 0xFFFF)
        self.rmw(R_MODE, ~0x0280 & 0xFFFF)
        self.w16(0x080, 0)
        self.w16(0x082, 0)
        for n in range(N_CE):
            self.rmw(0x402 + 2 * n, 0xF000, t["r402"])
        for n in range(N_CE):
            self.rmw(0x280 + 4 * n, 0xFFFF, 0x0400)
        self.rmw(0x304, ~0x0288 & 0xFFFF, t["r304"])
        self.w16(R_FI_TMCTRL3, 0x0FFF)   # slowest async strobe timing
        self.w16(R_SEQ_CTRL2, t["r03e"])
        self.rmw(0x3C2, ~0x0007 & 0xFFFF, 0x0004)
        self.w16(0x392, 0x0045)
        self.set_mode(0)

        # -- phase B ----------------------------------------------------------
        self._phy_strobe(t["strobe_b"])                     # sub_FFFE12B2
        self._channel_timing(t)

        # -- phase C ----------------------------------------------------------
        for off in (0x100, 0x102, 0x104):           # nfif_set_luns_per_device
            self.w16(off, 0x2222)

        # -- operating point --------------------------------------------------
        self.w16(R_MODE, mode_word)
        # The +0x03E write above cleared the sequencer-stop bit, so the
        # sequencer is live again and would arbitrate against hand-driven
        # commands. Park it; callers restart it with seq_start() if they want it.
        self.seq_stop()

    # ---- sequencer arbitration ---------------------------------------------
    # The sequencer and the indirect engine share the flash pins. Park the
    # sequencer before driving commands by hand and restart it afterwards.
    def seq_stop(self):
        self.rmw(R_SEQ_CTRL1, ~1 & 0xFFFF)
        self.rmw(R_SEQ_CTRL2, 0xFFFF, 8)

    def seq_start(self):
        """Enable the sequencer. This is HalNfif's sub_B596 verbatim:

            flashSeqCtrl2 &= ~8      un-park
            flashSeqCtrl1 |= 1       enable
            flashSeqCtrl1 |= 2       pulse
            flashSeqCtrl1 &= ~2

        +0x040 bit0 really is the enable -- the firmware leaves it set
        (+0x040 = 0x00C1 in its resident state). The ROM only pulses bit1
        because on a cold part bit0 is already whatever its reset value is; do
        not read "the ROM never writes bit0" as "bit0 must stay clear".
        """
        self.rmw(R_SEQ_CTRL2, ~8 & 0xFFFF)
        self.rmw(R_SEQ_CTRL1, 0xFFFF, 1)
        self.rmw(R_SEQ_CTRL1, 0xFFFF, 2)
        self.rmw(R_SEQ_CTRL1, ~2 & 0xFFFF)

    def seq_pulse(self):
        """Just the +0x040 bit1 pulse, without touching the enable.

        This is what sub_13F70 (the firmware's sequencer restart) and the ROM's
        HalSeq_OneTimeInit both do, between waiting for seq+0x010 bit0 to clear
        and asserting the seq reset/enable bits.
        """
        self.rmw(R_SEQ_CTRL1, 0xFFFF, 2)
        self.rmw(R_SEQ_CTRL1, ~2 & 0xFFFF)

    def nfif_seq_program(self):
        """Program the NFIF side of the sequencer (sub_FFFE1620..sub_FFFE1582).

        Nothing here is touched by the indirect-command path, which is why raw
        page reads worked without it while the sequencer refused to fetch. The
        ROM runs this block twice -- once before HalSeq_OneTimeInit and again
        immediately before the first doorbell -- and writes byte-identical
        values both times, so it is idempotent for a given package and clock.

        The values are the ROM's, read off its own write trace. The masks are
        from the disassembly: the function loads a per-package byte table and
        merges each field into the live register.

            +0x008 = table                       (whole register)
            +0x00A = (v & 0xF000) | table
            +0x402+2n = (v & 0xFC00) | table[5+n]   n = 0..11
            +0x032, +0x036, +0x038 = table       (whole register)
            +0x034 = (v & 0xC000) | table
            +0x304 &= 0xAF77                     (0xF800 -> 0xA800)
            +0x070, +0x076, +0x078 via sub_FFFE1582(index, ...)
        """
        self.w16(0x008, 0x0D89)
        self.rmw(0x00A, 0xF000, 0x0D80)
        for n in range(N_CE):
            self.rmw(0x402 + 2 * n, 0xFC00, 0x003B)
        self.w16(0x032, 0x9999)
        self.rmw(0x034, 0xC000, 0x2051)
        self.w16(0x036, 0x3573)
        self.w16(0x038, 0x3203)
        self.rmw(0x304, 0xAF77)
        self.w16(0x070, 0x6222)
        self.w16(0x076, 0x2929)
        self.w16(0x078, 0x0453)

    # ---- indirect command engine -------------------------------------------
    def setup(self, ce, dev, cmd1, cmd2, addr, count):
        """Program the command/address ports. `addr` is up to 5 cycles."""
        a = list(addr) + [0] * (5 - len(addr))
        self.set_mode(MODE_WORD & 3)
        self.rmw(R_DEVCH, 0xFF00, dev & 0xFF)
        self.rmw(R_DEVCH, 0xF0FF, (ce & 0xF) << 8)
        self.w16(R_CMD1, (cmd1 & 0xFF) | (a[0] << 8))
        self.w16(R_CMD2, a[1] | (a[2] << 8))
        self.w16(R_CMD3, a[3] | (a[4] << 8))
        self.w16(R_CMD4, cmd2 & 0xFF)
        self.w16(R_XFER, count & 0xFFFF)
        self.drain()

    def drain(self, limit=16):
        """Pulse the FIFO reset and make sure nothing is left in it.

        The reset pulse alone is not enough: a previous command's unread bytes
        survive it, and the next command then returns data shifted by however
        many were left over (seen as a READ ID coming back as
        `98 bf 84 cc e0 e0` -- two stale status bytes ahead of the real ID)."""
        self.rmw(R_IND_CFG, 0xFFFF, CFG_FIFORST)
        self.rmw(R_IND_CFG, ~CFG_FIFORST & 0xFFFF)
        for _ in range(limit):
            if not (self.r16(R_IND_CFG) & CFG_READY):
                return True
            self.r32(R_DATA)
        return False

    def wait_cfg(self, want, mask, tries=60):
        for _ in range(tries):
            if (self.r16(R_IND_CFG) & mask) == want:
                return True
        return False

    def pop(self, nbytes, tries=60):
        out = bytearray()
        for _ in range(nbytes // 4):
            if not self.wait_cfg(CFG_READY, CFG_READY, tries):
                break
            out += struct.pack('<I', self.r32(R_DATA))
        return bytes(out)

    # ---- commands -----------------------------------------------------------
    def reset_die(self, ce, dev=0):
        """NAND RESET (0xFF). Volatile state only — touches no stored data."""
        self.setup(ce, dev, 0xFF, 0, [], 0)
        self.w16(R_IND_CFG, CFG_CMDONLY | CFG_ARM | CFG_START)
        self.wait_cfg(0, CFG_ARM)
        time.sleep(0.002)          # tRST, generously
        return self.status(ce, dev)

    def status(self, ce, dev=0):
        self.setup(ce, dev, 0x70, 0, [], 4)
        self.w16(R_IND_CFG, CFG_ARM | CFG_START)
        self.wait_cfg(0, CFG_ARM)
        raw = self.pop(4)
        return raw[0] if raw else None

    def read_id(self, ce, dev=0, n=6):
        """READ ID. Each byte comes back doubled, so ask for 2n and keep evens."""
        self.setup(ce, dev, 0x90, 0, [0x00], 2 * n)
        self.w16(R_IND_CFG, CFG_ADDR1 | CFG_ARM | CFG_START)
        raw = self.pop(2 * n)
        return raw[0::2][:n], raw

    def read_page(self, ce, row, nbytes, col=0, dev=0, tr_tries=20):
        """Read (0x00 .. 0x30). Returns (status, data)."""
        addr = [col & 0xFF, (col >> 8) & 0xFF,
                row & 0xFF, (row >> 8) & 0xFF, (row >> 16) & 0xFF]
        self.setup(ce, dev, 0x00, 0x30, addr, 0)
        self.w16(R_IND_CFG, CFG_COL | CFG_ROW | CFG_WAITRDY | CFG_ARM | CFG_START)
        self.wait_cfg(0, CFG_ARM)
        st = None
        for _ in range(tr_tries):
            st = self.status(ce, dev)
            if st is not None and (st & 0x20):
                break
        # cmd1 = 0x00 with no address cycles puts the die back into data output
        self.setup(ce, dev, 0x00, 0, [], nbytes)
        self.w16(R_IND_CFG, CFG_ARM | CFG_START)
        return st, self.pop(nbytes)

    def read_page_bulk(self, ce, row, nbytes, col=0, dev=0, chunk=512,
                       progress=None):
        """Read a long span by re-issuing the command per chunk."""
        data = bytearray()
        while len(data) < nbytes:
            want = min(chunk, nbytes - len(data))
            _, part = self.read_page(ce, row, want, col=col + len(data), dev=dev)
            data += part
            if len(part) != want:
                break
            if progress:
                progress(len(data), nbytes)
        return bytes(data)

    # ---- diagnostics --------------------------------------------------------
    # Reading these has side effects: +0x02C/+0x02E pop the indirect FIFO, and
    # +0x00E is the direct-mode data port. A register dump must skip them.
    VOLATILE = (0x00E, 0x02C, 0x02E)

    def dump_regs(self, lo=0x000, hi=0x500):
        """Halfword reads only. With the MPU on the NAND window is
        Strongly-ordered, so a byte-stride block read alignment-faults, and an
        abort desynchronises the uart_shell protocol -- after which every read
        returns 0 and the block looks dead."""
        return {off: self.r16(off) for off in range(lo, hi, 2)
                if off not in self.VOLATILE}

    def identify_all(self, dev=0):
        out = {}
        for ce in range(N_CE):
            ident, _ = self.read_id(ce, dev)
            out[ce] = ident
        return out


def connect(init=False, mode_word=MODE_WORD, timing="fast"):
    """Open the link, enable the MPU, optionally bring the controller up."""
    c = Client(Ps5UartEnum.titania_fw_port(), Ps5UartEnum.BaudrateEfc)
    assert c.wait_server_up(5) and c.ping(), "uart_shell not responding"
    assert c.is_efc(), "not in EFC context"
    # With the MPU off, the NAND window loses its Device typing and halfword
    # stores are widened -- writing one half zeroes the other. Unconditional:
    # a reset leaves whatever region map the boot path had.
    c.mpu_setup_cpu0()
    n = Nand(c)
    if init:
        n.seq_stop()
        n.init_controller(mode_word, timing)
    return c, n


def sweep(n, ce=1):
    """Find which (timing set, mode word) the dies answer READ ID on."""
    print("timing  mode    ce{0} id                     doubled?".format(ce))
    hits = []
    for tname in ("fast", "rom"):
        for mw in (0x038E, 0x030E, 0x0302, 0x0202, 0x0102, 0x000E, 0x0002):
            n.seq_stop()
            n.init_controller(mw, tname)
            n.reset_die(ce)
            _, raw = n.read_id(ce, 0, 6)
            direct = raw[:6]
            evens = raw[0::2][:6]
            tag = ("DOUBLED" if evens == SAMSUNG_ID else
                   "DIRECT" if direct == SAMSUNG_ID else "")
            print(f"  {tname:5s} 0x{mw:04X}  {raw.hex(' ')}  {tag}")
            if tag:
                hits.append((tname, mw, tag))
    print(f"\n{len(hits)} working combination(s): {hits}")
    return hits


def main():
    do_init = "--init" in sys.argv
    c, n = connect(init=False)
    try:
        if "--cmp" in sys.argv:
            # Read ID on the interface as the firmware left it, re-init from
            # scratch, read ID again, and diff every register. An empty diff
            # with a failing read means the problem is latched PHY state, not a
            # register value.
            ce = 1
            n.seq_stop()
            ident0, _ = n.read_id(ce)
            regs0 = n.dump_regs()
            print(f"before init: ce{ce} id = {ident0.hex(' ')}")

            tname = "rom" if "--rom" in sys.argv else "fast"
            mw = MODE_WORD
            n.init_controller(mw, tname)
            ident1, raw1 = n.read_id(ce)
            regs1 = n.dump_regs()
            print(f"after  init ({tname}, mode 0x{mw:04X}): "
                  f"ce{ce} id = {ident1.hex(' ')}  raw={raw1.hex()}")

            diff = [(o, regs0[o], regs1[o]) for o in sorted(regs0)
                    if regs0[o] != regs1[o]]
            print(f"\n{len(diff)} registers differ from the working state:")
            for o, a, b in diff:
                print(f"    +0x{o:03X}  was {a:04X}  now {b:04X}")

            print("\nrestoring the registers that differ, then re-reading ID")
            for o, a, _ in diff:
                n.w16(o, a)
            n.seq_stop()
            ident2, _ = n.read_id(ce)
            print(f"  ce{ce} id = {ident2.hex(' ')}")
            return

        if "--baseline" in sys.argv:
            # No init at all: does this module's own command engine work against
            # the interface as the firmware left it? Isolates driver bugs from
            # bring-up bugs.
            print(f"mode word = 0x{n.r16(R_MODE):04X}  "
                  f"seq1 = 0x{n.r16(R_SEQ_CTRL1):04X}  "
                  f"seq2 = 0x{n.r16(R_SEQ_CTRL2):04X}")
            n.seq_stop()
            for ce in range(N_CE):
                ident, raw = n.read_id(ce)
                print(f"  ce{ce:2d}  {ident.hex(' '):<20s} raw={raw.hex()}")
            print(f"  ce1 status = {n.status(1)}")
            return

        if "--sweep" in sys.argv:
            sweep(n, int(sys.argv[sys.argv.index("--sweep") + 1], 0)
                  if len(sys.argv) > sys.argv.index("--sweep") + 1 else 1)
            return
        before = n.dump_regs()
        print(f"mode word = 0x{n.r16(R_MODE):04X}  "
              f"tmctrl3 = 0x{n.r16(R_FI_TMCTRL3):04X}")

        if do_init:
            print("\nrunning full controller init from scratch ...")
            n.seq_stop()
            n.init_controller()
            after = n.dump_regs()
            diff = [(o, before[o], after[o]) for o in sorted(before)
                    if before[o] != after[o]]
            print(f"  {len(diff)} of {len(before)} registers changed")
            for o, b, a in diff[:60]:
                print(f"    +0x{o:03X}  {b:04X} -> {a:04X}")
            if len(diff) > 60:
                print(f"    ... {len(diff) - 60} more")

            print("\nresetting every die ...")
            for ce in range(N_CE):
                st = n.reset_die(ce)
                print(f"  ce{ce:2d} status after RESET = "
                      f"{'None' if st is None else hex(st)}")

        print("\nREAD ID:")
        good = []
        for ce in range(N_CE):
            ident, raw = n.read_id(ce)
            ok = ident == SAMSUNG_ID
            good += [ce] if ok else []
            print(f"  ce{ce:2d}  {ident.hex(' ')}  {'OK' if ok else ''}")
        print(f"  {len(good)}/{N_CE} channels identified: {good}")

        if good:
            ce = good[0]
            row = compose_row(0, 1)
            st, data = n.read_page(ce, row, 64)
            print(f"\npage read ce{ce} row {row:#x}: status="
                  f"{'None' if st is None else hex(st)} bytes={len(data)} "
                  f"distinct={len(set(data))}")
            print(f"  {data[:32].hex(' ')}")
    finally:
        try:
            c.port.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
