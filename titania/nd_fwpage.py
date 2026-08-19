#!/usr/bin/env python3
"""Page read in Python, transcribed from the firmware's own code.

Disassembled live off the device (cpu1 TCM via the axi alias 0x0A000000):

  HalNfCtrl_ReadPageIm  @ 0x0000BE34
      ImSetup(pna, cmd1=0x00, cmd2=0x30, count=0)
      NFIF+0x20 = 0x1A6 ;  wait (NFIF+0x20 & 0x80) == 0
      rc = WaitStatus(pna, mask=0x20)          <- rc 3 = timed out
      ImSetup(pna, cmd1=0, cmd2=0, count=N)
      NFIF+0x20 = 0x180 ;  ImReadData(dst, N)
      SetMode(2)

  ImSetup (sub_BC12)(pna, cmd1, cmd2, count)
      SetMode(1)
      +0x02[7:0]=pna[1] dev   +0x02[11:8]=pna[0] ce
      +0x22[7:0]=cmd1         +0x22[15:8]=pna[2]   addr0
      +0x24[7:0]=pna[3]       +0x24[15:8]=pna[4]
      +0x26[7:0]=pna[5]       +0x26[15:8]=pna[6]
      +0x28[7:0]=cmd2         +0x2A=count
      pulse +0x20 bit6 (FIFO reset)

  HalNfCtrl_ReadStatusIm (sub_BCDA): ImSetup(pna,0x70,0,2); +0x20=0x180;
      wait arm clear; wait FIFO_READY; return *(u8*)(NFIF+0x2C)

Two deviations from the firmware, both measured:
  * mode 1 wedges the interface from this context (the shell stops answering and
    needs a full reset). Everything here stays in mode 2, which is the mode the
    firmware leaves the part in.
  * an 8-bit read of the FIFO port returns junk over uart_shell (0x01 where the
    32-bit read gives 0000E0E0), so all pops are 32-bit.

Only 0x00/0x30 (read), 0x70 (READ STATUS), 0x90 (READ ID) are ever issued.
The program (0x80/0x10) and erase (0x60/0xD0) variants at sub_BE9E / sub_BF04
are not implemented.

  py nd_fwpage.py [ce] [row] [nbytes] [--id] [--nostop]
"""
import sys, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from .nd_nfif import connect, Nfif, R_CFG

NFIF = 0x1700E000
CFG = NFIF + 0x20
DATA = NFIF + 0x2C


class FwPage(Nfif):
    def r16(self, a):
        return self.c.read16(a)

    def w16(self, a, v):
        self.c.write16(a, v & 0xFFFF)

    def bfi(self, off, lsb, width, val):
        cur = self.r16(NFIF + off)
        mask = ((1 << width) - 1) << lsb
        self.w16(NFIF + off, (cur & ~mask) | ((val << lsb) & mask))

    def im_setup(self, pna, cmd1, cmd2, count):
        self.set_mode(2)
        self.bfi(0x02, 0, 8, pna[1])
        self.bfi(0x02, 8, 4, pna[0])
        self.bfi(0x22, 0, 8, cmd1)
        self.bfi(0x22, 8, 8, pna[2])
        self.bfi(0x24, 0, 8, pna[3])
        self.bfi(0x24, 8, 8, pna[4])
        self.bfi(0x26, 0, 8, pna[5])
        self.bfi(0x26, 8, 8, pna[6])
        self.bfi(0x28, 0, 8, cmd2)
        self.w16(NFIF + 0x2A, count & 0xFFFF)
        self.w16(CFG, self.r16(CFG) | 0x40)
        self.w16(CFG, self.r16(CFG) & ~0x40)

    def poll(self, want, mask, tries=60):
        for _ in range(tries):
            if (self.r16(CFG) & mask) == want:
                return True
        return False

    def pop(self, nbytes, tries=60):
        out = bytearray()
        for _ in range(nbytes // 4):
            if not self.poll(0x4000, 0x4000, tries):
                break
            out += struct.pack('<I', self.c.read32(DATA))
        return bytes(out)

    def status(self, pna):
        self.im_setup(pna, 0x70, 0, 4)
        self.w16(CFG, 0x180)
        self.poll(0, 0x80)
        raw = self.pop(4)
        return (raw[0] if raw else None), raw

    def read_page(self, pna, nbytes, do_status=True):
        self.im_setup(pna, 0x00, 0x30, 0)
        self.w16(CFG, 0x1A6)
        armed = self.poll(0, 0x80)
        st = None
        if do_status:
            for _ in range(20):
                st, _ = self.status(pna)
                if st is not None and (st & 0x20):
                    break
        self.im_setup(pna, 0, 0, nbytes)
        self.w16(CFG, 0x180)
        return armed, st, self.pop(nbytes)


def pna_of(ce, dev, col, row):
    return [ce & 0xF, dev & 0xFF, col & 0xFF, (col >> 8) & 0xFF,
            row & 0xFF, (row >> 8) & 0xFF, (row >> 16) & 0xFF, 0]


ID_PAT = bytes.fromhex("ec5e5e9898bf")


def show(data, limit=128):
    for off in range(0, min(len(data), limit), 16):
        b = data[off:off + 16]
        txt = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
        print(f"    {off:04X}  {b.hex(' '):<47s}  {txt}")


def main():
    ce = int(sys.argv[1], 0) if len(sys.argv) > 1 else 1
    row = int(sys.argv[2], 0) if len(sys.argv) > 2 else 2
    n = int(sys.argv[3], 0) if len(sys.argv) > 3 else 256

    c = connect()
    p = FwPage(c)
    saved = c.read32(R_CFG & ~3)
    pna = pna_of(ce, 0, 0, row)
    stopped = "--nostop" not in sys.argv
    try:
        if stopped:
            p.seq_stop()

        if "--id" in sys.argv:
            ident, _, _ = p.read_id(ce, 0, 6)
            print(f"READ ID: {ident.hex(' ')}")

        st, raw = p.status(pna)
        print(f"ce{ce} status = {raw.hex(' ') if raw else 'none'}")

        armed, st2, data = p.read_page(pna, n)
        tag = ("empty" if not data
               else "STALE ID" if data.startswith(ID_PAT[:6])
               else "all-status" if len(set(data)) == 1
               else "*** DATA ***")
        print(f"ce{ce} row {row}: armed={int(armed)} status_after="
              f"{'None' if st2 is None else hex(st2)} bytes={len(data)} "
              f"distinct={len(set(data))}  {tag}")
        show(data)
    finally:
        p.set_mode(2)
        if stopped:
            try:
                p.seq_start()
            except Exception as e:
                print(f"seq_start failed: {e}")
        c.write32(R_CFG & ~3, saved)
        try:
            c.port.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
