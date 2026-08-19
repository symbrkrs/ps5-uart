"""Bring the Salina GbE up from EFC: BARs, block clocks, link.

This used to be three scripts run by hand in order, and `GbeDevice` shelled out
to them. They are register sequences with no reason to be separate processes, so
they live here as functions and `GbeDevice._connect` calls them.

    ensure_link(client)     bring the MAC up if it is not already up
    recover()               reset the board and reload uart_shell -> Client
    assign_bars(client)     PCIe BARs and bridge windows
    enable_blocks(client)   clock/reset the blocks behind the MAC

`ensure_link` is idempotent and cheap: it reads MDIO_CLK first and does nothing
when the MAC is already running, so it can be called on every connect rather
than being a step someone has to remember.

Order matters and the failure is misleading. After a reset every BAR on bus 3
reads 0x0000000C -- unassigned -- and the block bring-up then reads 0x00000270
from every register, the undecoded pattern, and spins forever on main-fab. The
documented recipe of bring-up-then-PHY only ever worked because BARs left over
from a previous normal boot were still in place. Assign the BARs first.

The PHY is not done here. uart_shell runs it inside MAC init, after the clock
enable and before ctrl/serdes/mode, because doing it from the host beforehand
leaves those writes going to a PHY configured against the old settings.
"""
from __future__ import annotations

import time

from .device import DeviceError

__all__ = ["ensure_link", "recover", "assign_bars", "enable_blocks",
           "mac_is_up", "GBE_BAR0"]

# --- PCIe config access, through the EFC's own config window ---------------
CFG_CTRL, CFG_DATA = 0x10121000, 0xCFFFF000
TYPE0, TYPE1, SELF = 4, 5, 0x200004

#: func0 BAR0: the GbE MAC.
GBE_BAR0 = 0x90008000

#: Salina's BAR layout. (function, bar index, address).
BARS = [
    (0, 0, 0x90008000),   # GbE MAC          4 KB
    (1, 0, 0x90000000),   #                 16 KB
    (2, 0, 0x90004000),   #                 16 KB
    (3, 0, 0x90009000),   #                  4 KB
    (3, 2, 0x90400000),   #                  2 MB -> main-fab at +0x140000
    (3, 4, 0x90600000),   #                512 KB -> ckgen0/1, efuse
    (4, 0, 0x90200000),   #                  2 MB
]

WIN_BASE, WIN_LIMIT = 0x90000000, 0x907FFFFF

BRIDGES = [("RC self  00:00.0", 0, 0, 0, SELF),
           ("link brg 00:00.0", 0, 0, 0, TYPE0),
           ("bridge   02:00.0", 2, 0, 0, TYPE1)]

# Blocks carved out of func3's BAR4/BAR2, from the func3 probe.
CKGEN0, CKGEN1, EFUSE = 0x90607400, 0x90607C00, 0x90604000
MAIN_FAB, PERVASIVE1 = 0x90540000, 0x90580000

MDIO_CLK = 0x7C          # reference clock, reported but not used as a gate

#: What an unassigned window reads back. Before the BARs are assigned every
#: register in the block returns the same undecoded pattern, which is the
#: condition bring-up exists to fix and the one worth testing for.
UNDECODED = 0x00000270


def _nolog(_m):
    pass


# --------------------------------------------------------------- liveness --
def mac_is_up(c):
    """Does the GbE register window decode?  -> bool

    Four consecutive registers are read and the answer is "yes" when they are
    not all the same value. An unassigned BAR makes every register in the block
    return one constant (`UNDECODED`), and an absent device returns all-ones, so
    a block that varies is a block that is answering.

    This is not a link test. Whether the PHY has negotiated is
    `start_gbe`'s business; all this decides is whether the BARs and block
    clocks still need doing, so that bring-up can be attempted on every connect
    without being paid for every time.

    An earlier version tested `MDIO_CLK == 25 MHz`, on the strength of a comment
    in the original bring-up script. Measured on the board, after a full reset
    and a successful bring-up by both this code and the original scripts, that
    register reads 1099 -- so the test rejected a MAC that was in fact decoding.
    """
    try:
        vals = [c.read32(GBE_BAR0 + 4 * k) for k in range(4)]
    except Exception:                                        # noqa: BLE001
        return False
    if len(set(vals)) == 1:
        return False                       # undecoded, or absent
    return True


def ensure_link(c, log=_nolog, force=False):
    """Bring the MAC up if it is not already up.  -> bool, True if it is up now.

    Idempotent. Does nothing when MDIO_CLK already reads the reference
    frequency, so this is safe to call on every connect and there is no
    separate bring-up step to forget.
    """
    if not force and mac_is_up(c):
        log("  GbE: MAC window already decodes; nothing to bring up")
        return True
    log("  GbE: assigning BARs and releasing block clocks")
    assign_bars(c, log=log)
    enable_blocks(c, log=log)
    up = mac_is_up(c)
    log("  GbE: MAC window %s (LINK_STS 0x%08X, MDIO_CLK %d)"
        % ("decodes" if up else "STILL UNDECODED",
           c.read32(GBE_BAR0 + 0x04), c.read32(GBE_BAR0 + MDIO_CLK)))
    return up


# ----------------------------------------------------------------- recover --
def recover(log=_nolog, settle=3.0):
    """Reset the board, reload uart_shell, enable the MPU.  -> Client

    Returns a new client: the reset invalidates any existing one. Raises
    DeviceError rather than returning a client that cannot be talked to.
    """
    import tool
    import uart_client

    fw_port = tool.Ps5UartEnum.get_port(tool.Ps5UartEnum.TitaniaFw)
    log("  recover: EMC %s, TitaniaFw %s"
        % (tool.Ps5UartEnum.emc_port(), fw_port))

    emc = tool.Ucmd()
    emc.screset()
    emc.unlock()
    if not emc.unlock_efc(True):
        raise DeviceError("unlock_efc did not report success; the board did not "
                          "come back with uart_shell loaded")

    # The other cores complain a few seconds later; let them settle before
    # talking to cpu0.
    time.sleep(settle)

    c = uart_client.Client(fw_port, tool.Ps5UartEnum.BaudrateEfc)
    if not c.ping():
        raise DeviceError("uart_shell did not answer after reset")
    # The MPU must be on before any peripheral access: with it off, halfword
    # stores to NFIF are widened and zero the other half of the dword, and the
    # NAND window is not typed Device.
    c.mpu_setup_cpu0()
    if not c.ping():
        raise DeviceError("uart_shell stopped answering during MPU setup")
    log("  recover: shell up, MPU on")
    return c


# ------------------------------------------------------------- PCIe config --
def _sel(c, bus, dev, fn, reg, kind):
    if kind == TYPE0:
        v = c.read32(CFG_CTRL + 0x10)
        c.write32(CFG_CTRL + 0x10, (v & ~0x0020001F) | TYPE0)
    else:
        c.write32(CFG_CTRL + 0x10, kind)
    c.write32(CFG_CTRL + 0x00,
              (bus << 24) | (dev << 19) | (fn << 16) | (reg & 0xF80))
    c.write32(CFG_CTRL + 0x04, 0)


def cfg_read(c, bus, dev, fn, reg, kind=TYPE1):
    _sel(c, bus, dev, fn, reg, kind)
    return c.read32(CFG_DATA + (reg & 0x7C))


def cfg_write(c, bus, dev, fn, reg, val, kind=TYPE1):
    _sel(c, bus, dev, fn, reg, kind)
    c.write32(CFG_DATA + (reg & 0x7C), val)


def assign_bars(c, log=_nolog):
    """Assign Salina's BARs and open the bridge windows on the path.

    Run this after every reset. Without it the blocks behind the MAC do not
    decode and `enable_blocks` spins on main-fab forever.
    """
    mem_win = ((WIN_LIMIT >> 16) & 0xFFF0) << 16 | ((WIN_BASE >> 16) & 0xFFF0)
    pref_win = mem_win | 0x00010001     # same range, marked 32-bit prefetchable

    for label, bus, dev, fn, kind in BRIDGES:
        ident = cfg_read(c, bus, dev, fn, 0x00, kind)
        if ident in (0, 0xFFFFFFFF):
            log("    %s: absent" % label)
            continue
        cfg_write(c, bus, dev, fn, 0x20, mem_win, kind)
        cfg_write(c, bus, dev, fn, 0x24, pref_win, kind)
        cmd = cfg_read(c, bus, dev, fn, 0x04, kind)
        cfg_write(c, bus, dev, fn, 0x04, (cmd & 0xFFFF0000) | (cmd & 0xFFFF) | 0x06,
                  kind)
        log("    %s: %04x:%04x window open" % (label, ident & 0xFFFF, ident >> 16))
        if not c.ping():
            raise DeviceError("uart_shell died configuring %s" % label)

    for fn, idx, addr in BARS:
        reg = 0x10 + 4 * idx
        cmd = cfg_read(c, 3, 0, fn, 0x04)
        # Decode must be off while the address is written, or the device
        # responds at a half-written address.
        cfg_write(c, 3, 0, fn, 0x04, (cmd & 0xFFFF) & ~0x02)
        old = cfg_read(c, 3, 0, fn, reg)
        cfg_write(c, 3, 0, fn, reg, addr | (old & 0xF))
        cfg_write(c, 3, 0, fn, reg + 4, 0)          # upper half of a 64-bit BAR
        cfg_write(c, 3, 0, fn, 0x04, (cmd & 0xFFFF) | 0x06)   # memory + master
        back = cfg_read(c, 3, 0, fn, reg)
        if (back & ~0xF) != addr:
            raise DeviceError("03:00.%d BAR%d did not take 0x%08X (reads 0x%08X)"
                              % (fn, idx, addr, back))
        if not c.ping():
            raise DeviceError("uart_shell died assigning 03:00.%d BAR%d" % (fn, idx))
    log("    %d BARs assigned" % len(BARS))


# ------------------------------------------------------------ block clocks --
def _rmw(c, addr, clear=0, set_=0):
    v = c.read32(addr)
    c.write32(addr, (v & ~clear) | set_)
    return c.read32(addr)


def enable_blocks(c, log=_nolog, spin_limit=200):
    """Clock and release the blocks behind the MAC.

    The sequence is the firmware's own, and the order is not negotiable: the
    main-fab spin in step 1 has to clear before anything else is touched.
    """
    log("    main-fab enable")
    _rmw(c, MAIN_FAB + 0x2020, set_=0x10)
    for spins in range(spin_limit + 1):
        if (c.read32(MAIN_FAB + 0x2028) & 0x1F0) == 0:
            break
        if spins == spin_limit:
            raise DeviceError(
                "main-fab did not settle (0x%08X & 0x1F0 stayed set after %d "
                "polls). The usual cause is unassigned BARs -- every register "
                "reads the undecoded pattern." % (MAIN_FAB + 0x2028, spin_limit))
    _rmw(c, MAIN_FAB + 0x2020, set_=0x1)

    _rmw(c, PERVASIVE1 + 0x20, set_=0x3)
    _rmw(c, CKGEN0 + 0xC8, set_=0x1)

    log("    ckgen1 ramp")
    for bit in (1, 2, 4, 8, 0x10):
        _rmw(c, CKGEN1 + 0x28, set_=bit)

    log("    release")
    _rmw(c, MAIN_FAB + 0x2020, clear=0x1)
    _rmw(c, MAIN_FAB + 0x2020, clear=0x10)
    _rmw(c, PERVASIVE1 + 0x20, clear=0x3)
    _rmw(c, CKGEN1 + 0x28, clear=0x8)
    _rmw(c, CKGEN1 + 0x28, clear=0x1)
    _rmw(c, PERVASIVE1 + 0x20, set_=0x1)
    _rmw(c, PERVASIVE1 + 0x20, clear=0x1)
    _rmw(c, CKGEN1 + 0x28, set_=0x1)
    _rmw(c, CKGEN1 + 0x28, set_=0x8)

    time.sleep(0.05)
    if not c.ping():
        raise DeviceError("uart_shell died during block bring-up")


# ---------------------------------------------------------------------- cli --
def main(argv=None):
    """`py -m titania.gbebringup [--reset] [--force]`

    `GbeDevice` calls these functions itself, so this exists for bringing a
    board up by hand and for seeing where it stops when it will not come up.
    """
    import argparse
    import tool
    import uart_client

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reset", action="store_true",
                    help="reset the board and reload uart_shell first")
    ap.add_argument("--force", action="store_true",
                    help="re-run bring-up even if the MAC already reads as up")
    a = ap.parse_args(argv)

    if a.reset:
        c = recover(log=print)
    else:
        c = uart_client.Client(
            tool.Ps5UartEnum.get_port(tool.Ps5UartEnum.TitaniaFw),
            tool.Ps5UartEnum.BaudrateEfc)
        if not c.ping():
            print("uart_shell is not answering; retry with --reset")
            return 1
        c.mpu_setup_cpu0()

    up = ensure_link(c, log=print, force=a.force)
    print("MAC up: %s" % up)
    return 0 if up else 1


if __name__ == "__main__":
    raise SystemExit(main())
