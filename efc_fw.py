#!/usr/bin/env python3
from efc_fw_event import EVENT_FORMATS
from pathlib import Path
from serial import Serial
from threading import Thread
from queue import Queue
from hexdump2 import hexump
import sys
import struct


class Efc:
    def __init__(self):
        self.debug = False
        import datetime

        self.log_file = (
            Path(__file__)
            .parent.joinpath(f"efc_logs/efc_log_{datetime.datetime.now()}.txt")
            .open("a")
        )
        # self.port = pyftdi.serialext.serial_for_url('ftdi://ftdi:232:AD5V3R0X/1', baudrate=460800)
        self.port = Serial("COM19", baudrate=460800)
        self.port.timeout = 0.05
        self.rx_queue = Queue()
        self.rx_buf = bytes()
        Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            data = self.port.read(0x1000)
            if data is None or len(data) == 0:
                continue
            self.rx_queue.put(data)

    def _rx_sync(self, min_bytes):
        while len(self.rx_buf) < min_bytes:
            self.rx_buf += self.rx_queue.get()

    def _rx_discard_all(self):
        while True:
            try:
                self.rx_queue.get(block=False)
            except:
                break

    def _dump_stream(self):
        print("rx_buf")
        hexdump(self.rx_buf)
        sys.stdout.flush()

        while True:
            buf = self.rx_queue.get()
            hexdump(buf)
            sys.stdout.flush()

    def _read(self, size):
        self._rx_sync(size)
        data = self.rx_buf[:size]
        self.rx_buf = self.rx_buf[size:]
        return data

    def _read_word(self):
        return self._read(4)

    def _read32(self):
        return int.from_bytes(self._read_word(), "big")

    def _read_str(self, term=b"\0"):
        null_term = term == b"\0"
        data = b""
        while True:
            # bit of a hack because newline-terminated
            # strings don't pad their length
            data += self._read(4 if null_term else 1)
            term_pos = data.find(term)
            if term_pos < 0:
                continue
            data = data[:term_pos]
            try:
                data = data.decode("ascii")
            except:
                data = "DECODE_FAILED(" + data.hex() + ")"
            return data

    def _read_line(self):
        return self._read_str(b"\r\n")

    def _read_event_hdr(self):
        event_id = self._read32()
        timestamp = self._read32()
        if self.debug:
            print(f"> event: {event_id:#8x} ts:{timestamp:8x}")
        return event_id

    def _read_event(self):
        event_id = self._read_event_hdr()
        event_fmt = EVENT_FORMATS.get(event_id)
        if event_fmt is None:
            print(f"unknown event {event_id:#8x}", flush=True)
            # self._dump_stream()
            # try to find next known event and resync
            while True:
                event_id = self._read32()
                event_fmt = EVENT_FORMATS.get(event_id)
                if event_fmt is not None:
                    # discard timestamp
                    timestamp = self._read32()
                    # print(f'RESYNC {event_id:#8x} @ {timestamp:x}', flush=True)
                    break
                print(f"{event_id:8x}", flush=True)

        parsers = (self._read32, self._read_str)
        args = []
        for i in range(event_fmt.argc):
            args.append(parsers[(event_fmt.arg_types >> i) & 1]())

        if event_fmt.fmt is None:
            pretty_args = []
            for arg in args:
                if isinstance(arg, str):
                    pretty_args.append(f'"{arg}"')
                else:
                    pretty_args.append(f"{arg:#8x}")
            pretty_args = ",".join(pretty_args)
            print(f"event {event_id:#8x} [{pretty_args}]", flush=True)
        else:
            formatted = event_fmt.fmt.format(*args)
            print(formatted, end="", flush=True)
            print(formatted, end="", flush=True, file=self.log_file)

        if event_id == 0x65A14003:
            # MemPrintf will spew to uart
            # note: for AHB periph port they forgot the newline...
            for i in range(4):
                l = self._read_line()
                print(l, flush=True)
                if l.find("External Abort") >= 0:
                    l = self._read_line()
                    print(l, flush=True)

        return event_id

    def _process_events(self, term=None):
        while True:
            event_id = self._read_event()
            if event_id == term:
                break

    def cmd(self, cmd):
        cmd += "\r"
        self.port.write(bytes(cmd, "ascii"))
        # discard echo (and extra newline that efc adds)
        echo = self._read(len(cmd) + 1)
        print("echo", echo.hex())
        # wait for prompt...gets sent twice
        self._process_events(0x10A82000)
        self._process_events(0x10A82000)

    def help(self):
        self.cmd("help")

    def info(self):
        self.cmd("info")

    def show_debug(self):
        self.cmd("showdebug")

    def read_id(self):
        self.cmd("id")

    def read_param_page(self, addr=0x40, size=0x100):
        # arg2 is dst addr
        self.cmd(f"rdpp {addr:x} {size:x}")

    def _indirect_read_write_erase(self, op, ch, dev, row, dst=None):
        l = f"idrw {op:x} {ch:x} {dev:x} {row:x}"
        if dst is not None:
            l += f" {dst:x}"
        self.cmd(l)

    def indirect_read(self, ch, dev, row, dst=None):
        # HalNfCtrl_ReadPageIm(addr={ch, dev, col=0, row}, dst, count=0x1000)
        self._indirect_read_write_erase(0, ch, dev, row, dst)

    def pkg_cfg_auto(self):
        self.cmd("pkgcfg")

    def pkg_cfg(self, data_if, xfer_mode):
        # data_if: NfIfData_t, xfer_mode: NfXferMode_t
        self.cmd(f"pkgcfg 1 {data_if:x} {xfer_mode:x}")

    def read_mem(self, addr, count, width):
        self.cmd(f"rmem {addr:x} {count:x} {width:x}")

    def write_mem(self, addr, val, width):
        self.cmd(f"wmem {addr:x} {val:x} {width:x}")

    def mem_read32(self, addr):
        self.read_mem(addr, 1, 4)

    def mem_read_buf(self, addr, size):
        self.read_mem(addr, size, 1)

    def mem_write8(self, addr, val: int):
        self.write_mem(addr, val, 1)

    def mem_write32(self, addr, val: int):
        self.write_mem(addr, val, 4)

    def mem_write_buf(self, addr, buf: bytes):
        num_dwords = len(buf) // 4
        for i in range(num_dwords):
            o = i * 4
            self.mem_write32(addr + o, struct.unpack_from("<I", buf, o)[0])
        for i in range(len(buf) - (len(buf) % 4), len(buf)):
            self.mem_write8(addr + i, buf[i])

    def core_info(self):
        self.cmd("cinfo")

    def host_info(self):
        self.cmd("hinfo")

    def get_log_info(self):
        self.cmd("getloginfo")

    def errlog_dump(self):
        self.cmd("errlogdump")

    def dump_block_map(self):
        self.cmd("dumpblkmp")

    def dump_block_map_planes(self):
        self.cmd("dumpblkmpplane")

    def dump_block_map_werus(self):
        self.cmd("dumpblkmpweru")

    def media_info(self):
        self.cmd("minfo")

    def fw_info(self):
        self.cmd("finfo")

    def call(self, addr, *args):
        assert len(args) <= 4
        args = list(args) + [0] * (4 - len(args))
        argstr = " ".join([f"{arg:08x}" for arg in args])
        self.cmd(f"ca {addr:8x} {argstr}")

    def dmac_copy(self, dst, src, size):
        self.call(0x5B4E | 1, dst, src, size)

    def dmac_set_addr_zones(self, dst, src):
        # ll 1: src
        # ll 3: dst
        self.mem_write32(0x1410A054, ((dst << 12) & 0xF) | (src & 0xF))

    def _open_close_nand_access(self, op):
        self.cmd(f"vsc_ocna {op}")

    def nand_open(self):
        self._open_close_nand_access(0)

    def nand_open_safe(self):
        self._open_close_nand_access(2)

    def nand_close(self):
        self._open_close_nand_access(1)

    def bcm_cmd(self, cmd, *args):
        BCM_BASE = 0x19000000
        for i, arg in enumerate(args):
            self.mem_write32(BCM_BASE + i * 4, arg)
        # for i in range(len(args), 16): self.mem_write32(BCM_BASE + i * 4, 0)
        self.mem_write32(BCM_BASE + 0x40, cmd)
        # b0 should be set
        # self.mem_read32(BCM_BASE + 0xc8)

        # for i in range(1, 0x11):
        #    self.mem_read32(BCM_BASE + 0x80 + i * 4)
        self.read_mem(BCM_BASE + 0x80, 0x11, 4)

        self.mem_write32(BCM_BASE + 0xC8, 0xFFFFFFFF)

    # NOTE for some reason, bcm can't do dma when booted into efc mode. missing some magic regs pokes or something
    def bcm_get_version_info(self, dst=0):
        dst0, dst1 = 0, 0
        if dst != 0:
            dst0 = dst
            dst1 = dst + 0x100
        self.bcm_cmd(2, dst0, dst1)

    def bcm_exploit(self):
        src = 0x41000000
        dst = src + 0x100
        self.bcm_cmd(56, 1, 1, 0x100 // 4, src, dst)
