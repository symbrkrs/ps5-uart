#!/usr/bin/env python3
"""NFIF indirect-mode driver for Titania, written from scratch.

Implements the register sequence directly over uart_shell -- no calls into EFC
firmware. The sequence mirrors what the firmware's own helpers do, which is
where the register semantics came from:

  HalNfif_SetFlashOperationMode  flashInterfacCfg[1:0] = mode
  HalNfif_SetupCmdRowAddrCount   cmd/row/count ports
  HalNfif_SetupCmdRowAddrCount_Sequencer  + dev/ch select + FIFO reset pulse
  HalNfif_ImReadData             poll FIFO_READY, 32-bit pops
  sub_B674 / sub_B596            stop / restart the sequencer around it

Only READ-type flash commands are issued here (0x90 READ ID, 0xEE GET FEATURE,
0xEC parameter page, 0x70 READ STATUS). Nothing writes or erases flash.
"""
import sys, time, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from uart_client import Client
from tool import Ps5UartEnum

B = 0x1700E000

R_CFG      = B + 0x000   # flashInterfacCfg      -- mode in bits[1:0]
R_DEVCH    = B + 0x002   # flashDirectIndAccessCfg -- dev[7:0], ch[11:8]
R_IND_CFG  = B + 0x020   # flashIndAccessCfg
R_IND_CMD1 = B + 0x022   # cmd1 | row[7:0]<<8
R_IND_CMD2 = B + 0x024   # row[15:8] | row[23:16]<<8
R_IND_CMD3 = B + 0x026   # cmd2
R_XFER_CNT = B + 0x02A   # XFER_CNT_IND
R_IND_DATA = B + 0x02C   # FLASH_DATA_IND_15_0
R_SEQ_CTRL2 = B + 0x03E  # flashSeqCtrl2
R_SEQ_CTRL1 = B + 0x040  # flashSeqCtrl1

MODE_DIRECT, MODE_INDIRECT, MODE_SEQ = 0, 1, 2

FIFO_READY = 0x4000      # flashIndAccessCfg bit14
FIFO_RESET = 0x0040      # bit6, pulsed
IND_START  = 0x0100      # bit8


class Nfif:
    def __init__(self, c, verbose=False):
        self.c = c
        self.verbose = verbose

    # --- primitives ---
    # uart_shell's read16/write16 are native 16-bit accesses -- the ARM MPU is what
    # makes that work, which is why connect() enables it before touching anything.
    # Re-measured 2026-07-31 (`nd_hwstore.py`): seed a dword BEEF5555, write16 the
    # low half to 1234, read back BEEF1234 in the ECU, NFIF and seq windows. The
    # high half survives.
    #
    # This used to emulate a halfword store with a 32-bit read-modify-write, on the
    # belief that write16 got widened. That was wrong and harmful: a 32-bit
    # write-back also rewrites the dword's other register, so every w16 to
    # flashSeqCtrl1 (+0x040) also rewrote flashSeqCmdPort1 (+0x042), and every w16
    # to flashIndAccessCfg (+0x020) also rewrote the command byte (+0x022). Command
    # and strobe ports must never be written as collateral. Use native accesses.
    def r16(self, a):
        return self.c.read16(a)

    def w16(self, a, v):
        self.c.write16(a, v & 0xFFFF)

    def set_mode(self, mode):
        self.w16(R_CFG, (self.r16(R_CFG) & 0xFFFC) | (mode & 3))

    # --- sequencer stop/start around an indirect command (sub_B674/sub_B596) ---
    def seq_stop(self):
        self.w16(R_SEQ_CTRL1, self.r16(R_SEQ_CTRL1) & ~1)
        self.w16(R_SEQ_CTRL2, self.r16(R_SEQ_CTRL2) | 8)

    def seq_start(self):
        self.w16(R_SEQ_CTRL2, self.r16(R_SEQ_CTRL2) & ~8)
        v = self.r16(R_SEQ_CTRL1)
        self.w16(R_SEQ_CTRL1, v | 1)
        self.w16(R_SEQ_CTRL1, v | 1 | 2)
        self.w16(R_SEQ_CTRL1, (v | 1) & ~2)

    # --- command setup ---
    def setup_cmd(self, ch, dev, cmd1, cmd2, row, count):
        self.set_mode(MODE_SEQ)
        v = self.r16(R_DEVCH)
        v = (v & 0xFF00) | (dev & 0xFF)
        v = (v & 0xF0FF) | ((ch & 0xF) << 8)
        self.w16(R_DEVCH, v)
        self.w16(R_IND_CMD1, (cmd1 & 0xFF) | ((row & 0xFF) << 8))
        self.w16(R_IND_CMD2, ((row >> 8) & 0xFF) | (((row >> 16) & 0xFF) << 8))
        self.w16(R_IND_CMD3, cmd2 & 0xFF)
        self.w16(R_XFER_CNT, count)
        # FIFO reset pulse
        self.w16(R_IND_CFG, self.r16(R_IND_CFG) | FIFO_RESET)
        self.w16(R_IND_CFG, self.r16(R_IND_CFG) & ~FIFO_RESET)

    def wait_fifo(self, tries=200):
        for _ in range(tries):
            if self.r16(R_IND_CFG) & FIFO_READY:
                return True
        return False

    def im_read(self, nbytes):
        """Pop nbytes from the indirect FIFO as 32-bit reads."""
        out = bytearray()
        stalled = 0
        for _ in range(nbytes // 4):
            if not self.wait_fifo():
                stalled += 1
                break
            out += struct.pack('<I', self.c.read32(R_IND_DATA))
        return bytes(out), stalled

    # --- one full read-type command ---
    def command(self, ch, dev, cmd1, count, row=0, cmd2=0, cfg=0x81,
                delay=0.002):
        self.seq_stop()
        self.setup_cmd(ch, dev, cmd1, cmd2, row, count)
        self.set_mode(MODE_SEQ)
        self.w16(R_IND_CFG, cfg)
        time.sleep(delay)
        self.w16(R_IND_CFG, self.r16(R_IND_CFG) | IND_START)
        data, stalled = self.im_read(count)
        self.seq_start()
        self.set_mode(MODE_SEQ)
        return data, stalled

    def read_id(self, ch, dev=0, n=8):
        """READ ID. Hardware doubles every byte, so ask for 2n and keep evens."""
        raw, stalled = self.command(ch, dev, 0x90, 2 * n)
        return raw[0::2][:n], raw, stalled


def connect():
    c = Client(Ps5UartEnum.titania_fw_port(), Ps5UartEnum.BaudrateEfc)
    assert c.wait_server_up(5) and c.ping(), "uart_shell not responding"
    assert c.is_efc(), "not in EFC context"
    # With the MPU off the NFIF window loses its Device typing and a halfword
    # store gets widened -- it zeroes the other half of the dword, so e.g.
    # arming flashIndAccessCfg (+0x20) wipes the command byte (+0x22). Enable
    # it before touching anything.
    # Always, not just when sctlr.M is clear: a reset leaves whatever region
    # map the boot path had, which is not necessarily one that types the NAND
    # window correctly.
    c.mpu_setup_cpu0()
    return c


def main():
    c = connect()
    n = Nfif(c)
    print(f"connected  mode=0x{n.r16(R_CFG):04X}  ind_cfg=0x{n.r16(R_IND_CFG):04X}")
    print("\nREAD ID (0x90) across ch 0..11, dev 0:")
    for ch in range(12):
        try:
            ident, raw, stalled = n.read_id(ch, 0, 8)
        except Exception as e:
            print(f"  ch{ch:2d}  ERROR {type(e).__name__}: {e}")
            continue
        print(f"  ch{ch:2d}  id={ident.hex(' ')}   raw={raw.hex()}"
              f"{'  STALLED' if stalled else ''}")
    try:
        c.port.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
