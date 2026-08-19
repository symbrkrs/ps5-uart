#!/usr/bin/env python3
import struct, time
from hexdump2 import hexdump
import serial
import hashlib
from tqdm import trange
from pathlib import Path
from tool import load_bin
from functools import cache

STATUS_NAMES = (
    'STATUS_FAILURE',
    'STATUS_PENDING',
    'STATUS_NO_RESOURCES',
    'STATUS_BAD_DEVICE',
    'STATUS_SHORT_XFR',
    'STATUS_OFFLINE',
    'STATUS_NO_REQUESTS_PENDING',
    'STATUS_UNSUPPORTED_FUNCTION',
    'STATUS_UNSUPPORTED_PARAMETER',
    'STATUS_IN_USE',
    'STATUS_CANCELED',
    'STATUS_NULL_BUFFER',
    'STATUS_ILLEGAL_BLOCK_SIZE',
    'STATUS_BUFFER_NOT_ALIGNED',
    'STATUS_PARAMETER_OUT_OF_RANGE',
    'STATUS_NULL_POINTER',
    'STATUS_CRC_ERROR',
    'STATUS_ECC_ERROR',
    'STATUS_PARITY_ERROR',
    'STATUS_FRAMING_ERROR',
    'STATUS_OVERRUN_ERROR',
    'STATUS_OFFSET_ERROR',
    'STATUS_IN_PROGRESS',
    'STATUS_RETRY_COUNT_EXCEEDED',
    'STATUS_BAD_ENGINE_OP',
    'STATUS_BAD_CACHE_ID',
    'STATUS_LIFECYCLE_SPENT',
    'STATUS_PERMISSION_DENIED',
    'STATUS_DEVICE_CORRUPTED',
    'STATUS_MODE_ERROR',
    'STATUS_BAD_THREAD_ID',
    'STATUS_BAD_TRANSFER_SIZE',
    'STATUS_CONVERGENCE_ERROR',
    'STATUS_FATAL_INTERNAL_ERROR',
    'STATUS_ECB_MODE_WITH_PARTIAL_CODEWORD',
    'STATUS_NO_KEY',
    'STATUS_INVALID_BINDING',
    'STATUS_INVALID_SIGNATURE',
    'STATUS_SIGNATURE_REPRESENTATIVE_OUT_OF_RANGE',
    'STATUS_HASH_NOT_SUPPORTED',
    'STATUS_INTENDED_ENCODED_MESSAGE_LENGTH_TOO_SHORT',
    'STATUS_INTEGER_TOO_LARGE',
    'STATUS_ENGINE_CONTEXT_MISMATCH',
    'STATUS_ILLEGAL_KEY',
    'STATUS_DMA_TIMEOUT',
    'STATUS_DMA_BUS_ERROR',
    'STATUS_DMA_PARITY_ERROR',
    'STATUS_DMA_LINKED_LIST_ACCESS_ERROR',
    'STATUS_DMA_PAUSE_COMPLETION_TIMEOUT',
    'STATUS_DMA_IDIOPATHIC_ERROR',
    'STATUS_HASH_TIMEOUT',
    'STATUS_AES_TIMEOUT',
    'STATUS_ZMODP_TIMEOUT',
    'STATUS_EC_TIMEOUT',
    'STATUS_DES_TIMEOUT',
    'STATUS_RC4_TIMEOUT',
    'STATUS_MD5_TIMEOUT',
    'STATUS_MCT_TIMEOUT',
    'STATUS_EBG_TIMEOUT',
    'STATUS_OTP_TIMEOUT',
    'STATUS_HASH_MESSAGE_OVERFLOW',
    'STATUS_UNSUPPORTED_DIGEST_TYPE',
    'STATUS_BUS_ERROR',
    'STATUS_RSA_MODULUS_TOO_SHORT',
    'STATUS_MESSAGE_REPRESENTATIVE_OUT_OF_RANGE',
    'STATUS_DIGEST_MISMATCH',
    'STATUS_INSUFFICIENT_PRIVILEGE',
    'STATUS_ZERO_DIVISOR',
    'STATUS_RDSA_PUBLIC_EXPONENT_OUT_OF_RANGE',
    'STATUS_RDSA_INVALID_KEY_LENGTH',
    'STATUS_PRIVATE_KEY_TOO_SMALL',
    'STATUS_GENERATE_PRIME_FAILURE',
    'STATUS_XPXQ_DISTANCE_INVALID',
    'STATUS_PQ_DISTANCE_INVALID',
    'STATUS_RDSA_PUBLIC_EXPONENT_EVEN_ERROR',
    'STATUS_ECDSA_INVALID_CURVE_TYPE',
    'STATUS_ECDSA_HASH_NOT_SUPPORTED',
    'STATUS_ECDSA_HASH_ERROR',
    'STATUS_ECDSA_VALID_SIGNATURE',
    'STATUS_ECDSA_INVALID_SIGNATURE',
    'STATUS_ECDSA_INVALID_POINT_FORMAT',
    'STATUS_ECDSA_INVALID_COMPRESSED_POINT',
    'STATUS_ECDSA_INVALID_HYBRID_POINT',
    'STATUS_ECDSA_INVALID_UNCOMPRESSED_POINT',
    'STATUS_ECDSA_OCTET2POINT_ERROR',
    'STATUS_ECDSA_VALID_PUBLIC_KEY',
    'STATUS_ECDSA_PUBLIC_KEY_NOT_ON_CURVE',
    'STATUS_ECDSA_PUBLIC_KEY_OUT_OF_RANGE',
    'STATUS_UNEXPECTED_IRQ',
    'STATUS_UNEXPECTED_FIQ',
    'STATUS_UNEXPECTED_UNDEFINED_INST',
    'STATUS_UNEXPECTED_DATA_ABORT',
    'STATUS_UNEXPECTED_PREFETCH_ABORT',
    'STATUS_UNEXPECTED_SWI',
    'STATUS_UNEXPECTED_UNUSED',
    'STATUS_ZERO_INPUT_TO_INVERTER',
    'STATUS_ZERO_SCALAR_OUTPUT',
    'STATUS_ZERO_KEY_RECEIVED_FOR_SCALAR_OPERATION',
    'STATUS_WRONG_IROM_VERSION',
    'STATUS_BAD_ENGINE_ID',
    'STATUS_FUNCTION_OUT_OF_RANGE',
    'STATUS_READ_VERIFICATION_FAILED',
    'STATUS_SELF_TEST_ERROR',
    'STATUS_INCORRECT_SYSTEM_STATE',
    'STATUS_INVALID_TOKEN',
    'STATUS_DRBG_RESEED_REQUIRED',
    'STATUS_UNSUPPORTED_ALGORITHM',
    'STATUS_SLEEP_CONTEXT_PREVIOUSLY_RESTORED',
    'STATUS_SLEEP_CONTEXT_INVALID',
    'STATUS_DMA_FIFO_PARITY_ERROR',
    'STATUS_EROM_ALREADY_LOADED',
    'STATUS_BIU_MAILBOX_OVERRUN',
    )

PANIC_NAMES = (
    'PANIC_UNEXPECTED_IRQ',
    'PANIC_UNEXPECTED_FIQ',
    'PANIC_UNDEFINED_INSTRUCTION',
    'PANIC_DATA_ABORT_TRAP',
    'PANIC_PREFETCH_ABORT_TRAP',
    'PANIC_UNEXPECTED_SWI',
    'PANIC_UNEXPECTED_NMI',
    'PANIC_UNEXPECTED_HARD_FAULT',
    'PANIC_UNEXPECTED_MMU_EXCEPTION',
    'PANIC_UNEXPECTED_USAGE_FAULT',
    'PANIC_UNEXPECTED_BUS_FAULT',
    )
PANIC_NAMES2 = (
    'PANIC_SPIKE','PANIC_STATE_CORRUPTED','PANIC_RED_ZONE_OVERFLOW','PANIC_YELLOW_ZONE_OVERFLOW','PANIC_MEMORY_ALLOCATION','PANIC_WATCHDOG_TIMEOUT','PANIC_CORRUPTED_DISPATCH_TABLE',
    'PANIC_SYSTEM_EXIT',
    'PANIC_UNUSED_HANDLER_INVOKED',
    'PANIC_BAD_ENGINE_ID',
    'PANIC_BAD_EROM_TRANSFER_ADDRESS','PANIC_UNEXPECTED_EXCEPTION','PANIC_UNSUPPORTED_INTERNAL_FUNCTION','PANIC_UNSUPPORTED_FUNCTION','PANIC_DRBG_SELFTEST_FAILURE','PANIC_HASH_SELFTEST_FAILURE','PANIC_AES_SELFTEST_FAILURE','PANIC_ZMODP_SELFTEST_FAILURE','PANIC_DMA_SELFTEST_FAILURE',
    'PANIC_DES_SELFTEST_FAILURE','PANIC_RC4_SELFTEST_FAILURE','PANIC_EC_SELFTEST_FAILURE','PANIC_MTC_SELFTEST_FAILURE','PANIC_ENTROPY_SOURCE_SELFTEST_FAILURE',
    'PANIC_INIT_FAILED','PANIC_LIFECYCLE_SPENT','PANIC_LUCASTEST_VALUE_NOT_FOUND','PANIC_SYSTEM_TIMER_DEAD','PANIC_OTP_CORRUPTED',
    'PANIC_PLATFORM_SELFTEST_FAILURE','PANIC_UNCORRECTABLE_ECC_ERROR','PANIC_ENGINE_SELFTEST_FAILURE','PANIC_UNCORRECTABLE_FAILURE_DURING_POWERDOWN_SEQUENCE','PANIC_UNCORRECTABLE_FAILURE_DURING_STATE_TRANSITION_SEQUENCE','PANIC_CPU_SELF_TEST_FAILURE','PANIC_ROM_CHECK_FAILURE',
    'PANIC_RAM_CHECK_FAILURE','PANIC_RSA_SELFTEST_FAILURE','PANIC_SIDE_CHANNEL_ATTACK',
    'PANIC_INTERNAL_ADDRESS_RANGE','PANIC_DRBG_FAILURE','PANIC_AES2_SELFTEST_FAILURE','PANIC_SPURIOUS_INTERRUPT','PANIC_AXI_SLAVE_BUS_ERROR',
    'PANIC_AXI_ADDRESS_ACCESS_ERROR','PANIC_AXI_INPUT_SLAVE_FIFO_PARITY_ERROR','PANIC_RESERVED_OR_UNASSIGNED_IRQ_ACTIVE','PANIC_NULL_POINTER',
    )

STATUS_MAP = {0: 'STATUS_SUCCESS'}
STATUS_MAP.update([(10 + i, val) for i, val in enumerate(PANIC_NAMES)])
STATUS_MAP.update([(32 + i, val) for i, val in enumerate(PANIC_NAMES2)])
STATUS_MAP.update([(255 + i, val) for i, val in enumerate(STATUS_NAMES)])

x = STATUS_NAMES[0x124-255]

def dump_path(name):
    path = Path(__file__).parent.joinpath('dumps')
    path.mkdir(parents=True, exist_ok=True)
    return path.joinpath(name + '.bin')

class Reg:
    DBGDRAR = 0
    DBGDSAR = 1
    DBGPRCR = 2
    MIDR = 3
    SCTLR = 4
    ACTLR = 5
    VBAR = 6
    CPSR = 7
    BTCMREGION = 8
    ATCMREGION = 9
    TCMSELECTION = 10
    MPIDR = 11

class Client:
    CMD_PING = 0
    CMD_MEM_ACCESS = 1
    CMD_REG_READ = 2
    CMD_REG_WRITE = 3
    CMD_INT_DISABLE = 4
    CMD_INT_ENABLE = 5
    CMD_DABORT_STATUS = 6
    CMD_CP15_ACCESS = 7
    CMD_CORE_N_CMD = 8
    CMD_CALL = 9
    CMD_MEM_FILL = 12
    CMD_IRQ_TEST = 14
    CMD_CRC16 = 15
    CMD_MMIO_DRAIN = 16
    CMD_GBE_START = 17
    CMD_NAND_READ_PAGES = 18
    CMD_NAND_START = 19
    CMD_NAND_WAIT = 20
    CMD_NAND_STREAM = 21

    def __init__(self, port, baudrate=230400*2):
        # A port name, or any object with the pyserial methods used here.
        # titania_gbe.GbePort is the latter, carrying the same bytes over
        # Ethernet.
        if isinstance(port, str):
            self.port = serial.Serial(port, baudrate=baudrate, timeout=1)
        else:
            self.port = port

    def open(self):
        if self.port.is_open:
            self.port.close()
        self.port.open()

    def _read_fmt(self, fmt):
        size = struct.calcsize(fmt)
        data = self.port.read(size)
        #print('<', data.hex())
        vals = struct.unpack(fmt, data)
        return vals[0] if len(vals) == 1 else vals

    def _write_fmt(self, fmt, *val):
        #hexdump(struct.pack(fmt, val))
        self.port.write(struct.pack(fmt, *val))

    def _read8(self) -> int: return self._read_fmt('<B')
    def _read16(self) -> int: return self._read_fmt('<H')
    def _read32(self) -> int: return self._read_fmt('<I')
    def _write8(self, val): self._write_fmt('<B', val)
    def _write16(self, val): self._write_fmt('<H', val)
    def _write32(self, val): self._write_fmt('<I', val)

    def ping(self):
        magic = 0xa5a5a5a5
        self._write32(self.CMD_PING)
        self._write32(magic)
        return self._read32() == magic + 1

    def _write_mem_access(self, addr, count, stride, is_write):
        opt = (1 if is_write else 0) << 31 | stride
        data = struct.pack('<3I', addr, count, opt)
        self._write32(self.CMD_MEM_ACCESS)
        self.port.write(data)

    def reg_read(self, reg: int):
        self._write32(self.CMD_REG_READ)
        self._write8(reg)
        return self._read32()

    def reg_write(self, reg: int, val):
        self._write32(self.CMD_REG_WRITE)
        self._write8(reg)
        self._write32(val)

    def int_disable(self):
        self._write32(self.CMD_INT_DISABLE)

    def int_enable(self):
        self._write32(self.CMD_INT_ENABLE)

    def dabort_status(self):
        self._write32(self.CMD_DABORT_STATUS)
        iaddr, daddr, status = self._read_fmt('<III')
        if all(map(lambda x: x == 0xffffffff, (iaddr, daddr, status))): return None
        return iaddr, daddr, status

    def check_dabort(self):
        dabort = self.dabort_status()
        if dabort is None:
            return False
        insn = dabort[0]
        dfar = dabort[1]
        dfsr = dabort[2]
        access = 'w' if (dfsr >> 11) & 1 else 'r'
        src = ((dfsr >> 6) & 0x10) | (dfsr & 0xf)
        src_name = {
            0b00001: 'align',
            0b00000: 'background',
            0b01101: 'perm',
            0b00010: 'debug',
            0b01000: 'sync ext',
            0b11001: 'sync parity',
            0b10110: 'async ext',
            0b11000: 'async parity',
        }.get(src, 'unknown')
        print(f'DABORT {dfar:08x} {dfsr:08x} {access} {src_name} {insn:08x}')
        return True

    UNDEF_SENTINEL = 0xDEADDEAD

    def cp15_read(self, op1, crn, crm, op2, cpnum=15):
        cp = 0 if cpnum == 15 else cpnum
        params = (op1 & 7) | ((crn & 0xF) << 3) | ((crm & 0xF) << 7) | ((op2 & 7) << 11) | ((cp & 0xF) << 15)
        self._write32(self.CMD_CP15_ACCESS)
        self._write32(params)
        return self._read32()

    def cp15_write(self, op1, crn, crm, op2, val, cpnum=15):
        cp = 0 if cpnum == 15 else cpnum
        params = (op1 & 7) | ((crn & 0xF) << 3) | ((crm & 0xF) << 7) | ((op2 & 7) << 11) | (1 << 14) | ((cp & 0xF) << 15)
        self._write32(self.CMD_CP15_ACCESS)
        self._write32(params)
        self._write32(val)
        return self._read32()

    SCTLR = (0, 1, 0, 0)
    MPUIR = (0, 0, 0, 4)
    MPU_IDX = (0, 6, 2, 0)
    MPU_BASE = (0, 6, 1, 0)
    MPU_SIZE_EN = (0, 6, 1, 4)
    MPU_ACCESS = (0, 6, 1, 2)

    def read_insn_set_attrs(self):
        id_isar = [self.cp15_read(0, 0, 2, i) for i in range(8)]
        for i, reg in enumerate(id_isar):
            print(f'ID_ISAR{i} {reg:08x}')
        return id_isar

    def mpu_num_regions(self):
        dregion = (self.cp15_read(*self.MPUIR) >> 8) & 0xff
        return dregion

    def mpu_entry_set(self, idx, base, size_en, access_control):
        self.cp15_write(*self.MPU_IDX, idx)
        self.cp15_write(*self.MPU_BASE, base)
        self.cp15_write(*self.MPU_SIZE_EN, size_en)
        self.cp15_write(*self.MPU_ACCESS, access_control)

    def mpu_enable(self, enable):
        sctlr = self.cp15_read(*self.SCTLR)
        if enable:
            sctlr |= 1
        else:
            sctlr &= ~1
        self.cp15_write(*self.SCTLR, sctlr)

    def mpu_setup_cpu0(self):
        # MPU_SIZE_EN and MPU_ACCESS are named backwards: on Cortex-R5 c6,c1,2
        # is DRSR (size+enable) and c6,c1,4 is DRACR (access control). The
        # tuple order below is right for the hardware, so leave it alone --
        # swapping it to match the names hangs mpu_enable().
        rom_mpu_entries = [
            (0, 0x1305, 0x3F),              # 0x00000000 : 0x100000000 XN,full access,S,B
            (0xF0000000, 0x304, 0x37),      # 0xf0000000 : 0x100000000 full access,S
            (0xE0000000, 0x302, 0x37),      # 0xE0000000 : 0xF0000000 full access,C
            (0, 0x30C, 0x3D),               # 0x00000000 : 0x80000000 full access,TEX=001,S  (DRAM will land here)
            (0x10000000, 0x304, 0x35),      # 0x10000000 : 0x18000000 full access,S
            (0x18000000, 0x304, 0x2F),      # 0x18000000 : 0x19000000 random mem. full access,S
            (0x19000000, 0x304, 0x31),      # 0x19000000 : 0x1B000000 bcm. full access,S
            (0x80000000, 0x304, 0x3B),      # 0x80000000 : 0xC0000000 full access,S
            (0xC0000000, 0x304, 0x39),      # 0xC0000000 : 0xE0000000 full access,S
            (0, 0x308, 0x2F),               # 0x00000000 : 0x01000000 local tcms. full access,TEX=001
            (0xFFFE0000, 0x302, 0x21),      # rom. full access,C
        ]
        self.mpu_enable(False)
        for i, rom_entry in enumerate(rom_mpu_entries):
            self.mpu_entry_set(i, *rom_entry)
        idx = len(rom_mpu_entries)
        #self.mpu_entry_set(idx, 0, 0, 0)
        #idx += 1
        for i in range(idx, self.mpu_num_regions()):
            self.mpu_entry_set(i, 0, 0, 0)
        self.mpu_enable(True)
        # dummy write to trigger dsb/isb
        self.cp15_write(*self.MPU_IDX, 0)

    class CoreNCmd:
        CoprocRead = 1
        CoprocWrite = 2
        Memcpy = 3
        Read32 = 4
        Write32 = 5

    def core_n_cmd(self, cpu: int, func: int, *args):
        self._write32(self.CMD_CORE_N_CMD)
        assert cpu > 0 and func > 0
        self._write32(cpu)
        self._write32(func)
        assert len(args) <= 3
        for arg in args:
            self._write32(arg)
        for _ in range(len(args), 3):
            self._write32(0)
        return [self._read32() for _ in range(4)]

    def core_n_coproc_rw(self, cpu: int, op1: int, crn: int, crm: int, op2: int, cmd: int, val: int=0, cpnum: int=15) -> int | None:
        params = (op2 & 7) << 11 | (crm & 0xF) << 7 | (crn & 0xF) << 3 | op1 & 7 | ((cpnum & 0xF) << 15)
        rv = self.core_n_cmd(cpu, cmd, params, val)
        # check undef
        if rv[1]: return None
        return rv[2]

    def core_n_coproc_read(self, cpu: int, op1: int, crn: int, crm: int, op2: int, cpnum: int=15) -> int | None:
        return self.core_n_coproc_rw(cpu, op1, crn, crm, op2, self.CoreNCmd.CoprocRead, cpnum=cpnum)

    def core_n_coproc_write(self, cpu: int, op1: int, crn: int, crm: int, op2: int, val: int, cpnum: int=15) -> int | None:
        return self.core_n_coproc_rw(cpu, op1, crn, crm, op2, self.CoreNCmd.CoprocWrite, val, cpnum=cpnum)

    def core_n_read32(self, cpu: int, addr: int) -> int:
        rv = self.core_n_cmd(cpu, self.CoreNCmd.Read32, addr)
        return rv[1]

    def core_n_write32(self, cpu: int, addr: int, val: int):
        self.core_n_cmd(cpu, self.CoreNCmd.Write32, addr, val)

    def call(self, func: int, *args, **kwargs) -> int:
        args = list(args) + [0] * (7 - len(args))
        assert len(args) == 7
        self._write32(self.CMD_CALL)
        self._write32(func)
        for arg in args:
            self._write32(arg)

        # XXX some calls will write to uart errlog
        generates_spew = kwargs.get('generates_spew', False)
        if generates_spew:
            waiting = self.port.in_waiting
            while True:
                time.sleep(.3)
                now = self.port.in_waiting
                if now == waiting:
                    break
                waiting = now
            if waiting > 8:
                extra = self.port.read(waiting - 8)
                #print('log:', str(extra, 'ascii'))
                print('log:')
                hexdump(extra)

        paborted = self._read32()
        rv = self._read32()
        assert paborted in (0, 1)
        if paborted: raise Exception(f'PABORT calling {func:8x}')
        return rv

    def debug_reset(self):
        self.reg_write(Reg.DBGPRCR, 2)

    def set_breakpoint(self, index, addr):
        # XXX these addrs are for EAP. EFC has debug in nearby addrspace.
        # DBGBCRn clear
        self.write32(0x18130000 + 0x140 + index * 4, 0)
        # DBGBVRn
        self.write32(0x18130000 + 0x100 + index * 4, addr & ~0b11)
        # DBGBCRn
        BT = 0b0000
        BAS = 0b1111
        self.write32(0x18130000 + 0x140 + index * 4, (BT << 20) | (BAS << 5) | 1)

    def read_fmt(self, addr, fmt):
        size = struct.calcsize(fmt)
        self._write_mem_access(addr, 1, size, 0)
        val = self._read_fmt(fmt)
        #self.check_dabort()
        return val

    def read_array_fmt(self, addr, fmt, size):
        stride = struct.calcsize(fmt)
        count = size//stride
        self._write_mem_access(addr, count, stride, 0)
        for _ in range(count):
            yield addr, self._read_fmt(fmt)
            addr += stride

    def read_regs32(self, addr, count):
        return [val for addr, val in self.read_array_fmt(addr, '<I', 4 * count)]

    def read_regs32_str(self, addr, count):
        return ' '.join([f'{val:8x}' for val in self.read_regs32(addr, count)])

    def write_fmt(self, addr, fmt, val):
        size = struct.calcsize(fmt)
        self._write_mem_access(addr, 1, size, 1)
        self._write_fmt(fmt, val)
        #self.check_dabort()

    def write_array_fmt(self, addr, fmt, vals):
        stride = struct.calcsize(fmt)
        assert stride in (1,2,4)
        if isinstance(vals, bytes):
            old = vals
            vals = []
            for i in range(0, len(old), stride):
                vals.append(struct.unpack_from('<I', old, i)[0])
        self._write_mem_access(addr, len(vals), stride, 1)
        for val in vals: self._write_fmt(fmt, val)
        #self.check_dabort()

    def read8(self, addr: int) -> int: return self.read_fmt(addr, '<B')
    def read16(self, addr): return self.read_fmt(addr, '<H')
    def read32(self, addr: int) -> int: return self.read_fmt(addr, '<I')
    def write8(self, addr: int, val: int): self.write_fmt(addr, '<B', val)
    def write16(self, addr, val): self.write_fmt(addr, '<H', val)
    def write32(self, addr: int, val: int): self.write_fmt(addr, '<I', val)

    def read(self, addr, size):
        # Use a 32-bit stride where the range allows it. The target does one
        # uncached bus read per element, ~3.2 us, so a byte stride costs 13.6 s
        # for 4 MB and caps throughput around 300 KB/s. Same bytes on the wire.
        if addr % 4 == 0 and size % 4 == 0:
            self._write_mem_access(addr, size // 4, 4, 0)
        else:
            self._write_mem_access(addr, size, 1, 0)
        data = self.port.read(size)
        assert len(data) == size
        return data

    def write(self, addr, data):
        self._write_mem_access(addr, len(data), 1, 1)
        self.port.write(data)

    def read_str(self, addr):
        data = []
        while True:
            val = self.read8(addr)
            if val == 0: break
            data.append(val)
            addr += 1
        return str(bytes(data), 'ascii')

    def wait_server_up(self, timeout=1):
        self.port.reset_output_buffer()
        start = time.time()
        while time.time() - start < timeout:
            self.port.reset_input_buffer()
            try:
                if self.ping(): return True
                time.sleep(.2)
            except: pass
        return False

    @cache
    def is_efc(self):
        return self.reg_read(Reg.MIDR) == 0x411fc153

    def is_eap(self):
        return not self.is_efc()

    def _old_ipc_jump(self, cpu_index, addr):
        # TODO might change if sending cpu isn't cpu0?
        ipc_regs = {
            0: 0x14000000,
            1: 0x14000000,
            2: 0x14002000,
            3: 0x14004000,
        }[cpu_index]
        ack_reg = ipc_regs + 0x38
        fifo_reg = ipc_regs + 0x60
        status_reg = ipc_regs + 0x80
        self.write32(fifo_reg, 0x55) # cIpcStatusRequest
        self.write32(fifo_reg, 2) # cIpcCmdGo
        self.write32(fifo_reg, addr)
        i = 0
        while True:
            j = 0
            while self.read32(ack_reg) == 0:
                j += 1
                if j > 100:
                    print('inner timeout')
                    break
                pass
            # cIpcStatusSuccess
            if self.read32(status_reg) == 16:
                break
            i += 1
            if i > 100:
                print('outer timeout')
                break

    def spin_other_cpus(self):
        code_addr = self.SRAM_BASE
        self.write(code_addr, load_bin('efc_stcm_counter'))
        for i in range(1, 4):
            self.ipc_jump(i, code_addr)


    BCM_BASE = 0x19000000

    class HashMode:
        Sha1 = 0
        Sha224 = 1
        Sha256 = 2
        Sha384 = 3
        Sha512 = 4
        Md5 = 5

    def bcm_reg_read32(self, offset: int):
        return self.read32(self.BCM_BASE + offset)

    def bcm_reg_write32(self, offset, val):
        self.write32(self.BCM_BASE + offset, val)

    @property
    def bcm_fifo_status(self):
        return self.bcm_reg_read32(0xc4)

    @property
    def bcm_rom_initalized(self):
        return (self.bcm_fifo_status >> 8) & 1

    @property
    def bcm_fatal_error(self):
        return (self.bcm_fifo_status >> 10) & 1

    @property
    def bcm_host_intp_reg(self):
        # b16: host address range exception
        # b17: host queue full access
        # b18: host queue full
        return self.bcm_reg_read32(0xc8)

    @bcm_host_intp_reg.setter
    def bcm_host_intp_reg(self, val):
        self.bcm_reg_write32(0xc8, val)

    @property
    def bcm_host_address_range_exception(self):
        return (self.bcm_host_intp_reg >> 16) & 1

    @property
    def bcm_host_queue_full_access(self):
        return (self.bcm_host_intp_reg >> 17) & 1

    @property
    def bcm_host_queue_full(self):
        return (self.bcm_host_intp_reg >> 18) & 1

    def bcm_read_status(self, idx: int):
        return self.bcm_reg_read32(0x84 + 4 * idx)

    def bcm_dump_regs(self):
        for addr, status in self.read_array_fmt(self.BCM_BASE + 0x80, '<I', 0xe0 - 0x80):
            print(f'{addr:8x} {status:8x}')

    def bcm_cmd(self, cmd, *args, **kwargs):
        assert len(args) <= 16
        args = list(args) + [0] * (16 - len(args))
        ll_in = kwargs.get('ll_in')
        if ll_in is not None:
            assert args[14] == 0
            args[14] = ll_in
        ll_out = kwargs.get('ll_out')
        if ll_out is not None:
            assert args[15] == 0
            args[15] = ll_out
        self.write_array_fmt(self.BCM_BASE, '<I', args)
        self.write32(self.BCM_BASE + 0x40, cmd)

        # b0 should be set upon completion
        # rom waits up to 100us * 50000 = 5secs
        start = time.time()
        while (self.bcm_host_intp_reg & 1) == 0:
            if time.time() - start > 5:
                print(f'cmd {cmd:2d} timeout {self.bcm_host_intp_reg:8x}')
                break

        #if self.bcm_fifo_status != 0x300:
        #    print('weird fifo status!')
        #    self.bcm_dump_regs()
        return_status = self.read32(self.BCM_BASE + 0x80)
        #'''
        if return_status not in (0,307):
            print(f'cmd {cmd:2d} return_status {return_status} {STATUS_MAP.get(return_status)}')
            if self.bcm_fatal_error:
                print(f'fatal error: {STATUS_MAP.get(self.bcm_reg_read32(0x84))}')
            if not kwargs.get('silent', False):
                self.bcm_dump_regs()
        #'''

        self.bcm_host_intp_reg = 0xffffffff
        return return_status

    class PortTimeout:
        def __init__(self, port, timeout):
            self.port = port
            self.timeout_saved = None
            self.temp_timeout = timeout
        def __enter__(self):
            self.timeout_saved = self.port.timeout
            self.port.timeout = self.temp_timeout
            return None
        def __exit__(self, exc_type, exc, tb):
            self.port.timeout = self.timeout_saved

    def crc16(self, addr: int, size: int) -> int:
        self._write_fmt('<3I', self.CMD_CRC16, addr, size)
        with self.PortTimeout(self.port, 10):
            return self._read32()

    def crc16_self(self) -> int:
        return self.crc16(0, 0xffffffff)

    def mmio_drain(self, fifo_addr: int, dst: int, count: int,
                   stat_addr: int = 0, ready_mask: int = 0,
                   spin_limit: int = 100000, timeout: int = 30):
        """Pop `count` 32-bit words from `fifo_addr` into `dst`, on-target.

        Each word is gated on (*stat_addr & ready_mask) when ready_mask != 0.
        Returns (done, status); status 0 = ok, 1 = timed out waiting for ready.

        Doing this from the host costs ~9200 round trips per page and caps page
        reads at ~2.4 KB/s. Here a page is one mmio_drain plus one bulk read.

        The target reads the FIFO exactly `count` times, never speculatively --
        a read pops it, so a probe would shift every word after it. Accesses are
        32-bit: the NAND window is Strongly-ordered with the MPU on and a
        narrower access data-aborts.
        """
        self._write32(self.CMD_MMIO_DRAIN)
        self._write_fmt('<6I', fifo_addr, stat_addr, ready_mask, count, dst,
                        spin_limit)
        with self.PortTimeout(self.port, timeout):
            done = self._read32()
            status = self._read32()
        return done, status

    def nand_read_pages(self, dst, row, npages, page_bytes=18432, ce=1, dev=0,
                        col=0, nfif=0x1700E000, spin=200000, timeout=120):
        """Read whole NAND pages into target memory, setup included, on-device.

        Setup is ~30 register accesses per page (ImSetup, arm, status poll,
        ImSetup, arm). As host round trips that dominates a dump, so the whole
        per-page sequence runs on the target and the host issues one command
        plus one bulk read per N pages.

        Call mpu_setup_cpu0() first. Without the MPU the NFIF window is not
        Device-typed, halfword stores get widened, and arming +0x20 wipes the
        command byte at +0x22. Park the sequencer (Nfif.seq_stop()) as well --
        it shares the flash pins with the indirect engine.

        Returns (pages_done, bytes, status_last, err) where err is
        0 ok / 1 arm timeout / 2 status timeout / 3 FIFO timeout.
        """
        self._write32(self.CMD_NAND_READ_PAGES)
        self._write_fmt("<9I", nfif, dst, row, npages, page_bytes, ce, dev, col,
                        spin)
        with self.PortTimeout(self.port, timeout):
            raw = self.port.read(32)
        if len(raw) != 32:
            raise IOError(f"nand_read_pages: short reply ({len(raw)} B)")
        # pages, bytes, status_last, err, status_tries, fifo_spins, cycles, pad
        return struct.unpack("<8I", raw)


    # Ping-pong: run the drain on another core so it overlaps the transfer.
    # Pack the job with nand_job_bytes(), write it as one mem_access block,
    # start it, pull the previous batch over the wire, then wait. Use cpu 1;
    # cpu3 is not running core_n_server and probing it wedges the shell.
    NAND_JOB_FMT = "<9I"

    @staticmethod
    def nand_job_bytes(dst, row, npages, page_bytes=18432, ce=1, dev=0, col=0,
                       nfif=0x1700E000, spin=200000):
        """The request half of NandJob, for one blocking write."""
        return struct.pack(Client.NAND_JOB_FMT, nfif, dst, row, npages,
                           page_bytes, ce, dev, col, spin)

    def nand_start(self, job, desc, cpu=1, timeout=30):
        """Trigger a job on `cpu` and return at once -- does NOT wait."""
        self._write32(self.CMD_NAND_START)
        self._write_fmt("<3I", cpu, job, desc)
        with self.PortTimeout(self.port, timeout):
            raw = self.port.read(4)
        if len(raw) != 4:
            raise IOError("nand_start: no reply")
        return struct.unpack("<I", raw)[0]

    def nand_wait(self, job, cpu=1, spin=0, timeout=120):
        """Block until the job publishes `done`, then report its counters.

        Returns (pages, bytes, status_last, err, status_tries, fifo_spins,
        timed_out, pad).
        """
        self._write32(self.CMD_NAND_WAIT)
        self._write_fmt("<3I", cpu, job, spin)
        with self.PortTimeout(self.port, timeout):
            raw = self.port.read(32)
        if len(raw) != 32:
            raise IOError(f"nand_wait: short reply ({len(raw)} B)")
        return struct.unpack("<8I", raw)

    def nand_stream(self, row, npages, page_bytes=18432, ce_mask=0x040, dev=0,
                    col=0, nfif=0x1700E000, spin=200000, timeout=120, burst=1):
        """Stream NAND pages straight off the wire; the reply is the data.

        The staged path copies every payload word across the bus several times
        (FIFO -> DRAM -> tx payload -> ring). Here the drain writes into the
        MAC's frame buffers directly, so a word costs one FIFO read and one ring
        write, with no destination buffer and no second bulk read.

        ce_mask selects the channels. Pages come back round-interleaved, one per
        channel per row, ascending. Use a single channel: interleaving measured
        98.4 pages/s on one, 18.3 on two, 4.6 on four, because switching channel
        costs more than the tR it hides. Only dev0 is populated and ch0 is dead.
        """
        # On a short reply, resume from the first gap rather than re-running the
        # batch; keep the contiguous prefix and re-read from the gap on.
        nce = bin(ce_mask).count("1")
        want = npages * nce * page_bytes
        out = bytearray()
        base = row
        left = npages
        early = hasattr(self.port, "give_up_early")
        for _ in range(8):
            if early:
                self.port.give_up_early(True)
            self._write32(self.CMD_NAND_STREAM)
            self._write_fmt("<9I", nfif, base, left, page_bytes, ce_mask, dev,
                            col, spin, burst)
            with self.PortTimeout(self.port, timeout):
                data = self.port.read(left * nce * page_bytes)
            if not early:
                return bytes(data)
            # Round down to a whole ROUND (one page per CE), so a resume still
            # starts on a row boundary for every chip.
            rnd = page_bytes * nce
            good = (len(data) // rnd) * rnd
            out += data[:good]
            if len(out) >= want:
                break
            base = row + len(out) // rnd
            left = npages - len(out) // rnd
            if left <= 0:
                break
        if early:
            self.port.give_up_early(False)
        return bytes(out[:want])

    def mem_fill(self, dst: int=0, buf: bytes=b'', **kwargs):
        size = len(buf)
        count = kwargs.get('count', 0 if size == 0 else 1)
        self._write32(self.CMD_MEM_FILL)
        self._write_fmt('<3I', dst, size, count)
        self.port.write(buf)
        # can take a while
        with self.PortTimeout(self.port, 10):
            ack = self._read32()
            if ack != 1:
                raise Exception(f"mem_fill: bad ack {ack}")

    def bcm_wait_boot(self):
        saw_early_boot = False
        while self.bcm_fifo_status != 0x300:
            saw_early_boot = True
        return saw_early_boot

    def bcm_get_version_info(self, dst=0):
        dst0, dst1 = 0, 0
        if dst != 0:
            dst0 = dst
            dst1 = dst + 0x100
        rv = self.bcm_cmd(2, dst0, dst1)
        #if dst != 0: return self.read_str(dst0), self.read_str(dst1)
        return rv

    def bcm_get_system_state(self):
        self.bcm_cmd(7)
        # both efc and eap get [0, 0, 0, 1835009, 1, 258, 1536, 16843008, 2264, 0, 0, 0, 0, 0, 0, 0]
        return [val for addr, val in self.read_array_fmt(self.BCM_BASE + 0x84, '<I', 0x10 * 4)]

    def bcm_aes_init(self, direction, key_bitlen, cipher_mode):
        return self.bcm_cmd(12, direction, key_bitlen, cipher_mode)

    def bcm_aes_zeroize(self):
        return self.bcm_cmd(13)

    def bcm_aes_process(self, src, dst, size, flag, flag2):
        return self.bcm_cmd(14, src, dst, size, flag, 0, 0, 0, 0, flag2)

    def bcm_aes_load_iv(self, iv):
        return self.bcm_cmd(15, iv)

    # key_bitlen in (128, 192, 256)
    # key_sel ignored?
    def bcm_aes_load_key(self, key_bitlen, key, key_sel):
        return self.bcm_cmd(16, key_bitlen, key, key_sel)

    def bcm_aes_key_gen(self, bitlen, dst):
        return self.bcm_cmd(17, bitlen, dst)

    def bcm_aesx_load_key(self, key_bitlen, key, key_sel, engine_id):
        return self.bcm_cmd(67, key_bitlen, key, key_sel, engine_id)

    def bcm_aesx_load_iv(self, iv, engine_id):
        return self.bcm_cmd(68, iv, engine_id)

    # iv, lba, and ctr args don't seem to do anything
    def bcm_aesx_init(self, direction, key_bitlen, cipher_mode, engine_id,
        iv_mode, iv_mask, lba_hi, lba_lo, interleave, endian, ctr_modulus):
        return self.bcm_cmd(69, direction, key_bitlen, cipher_mode, engine_id,
            iv_mode, iv_mask, lba_hi, lba_lo, interleave, endian, ctr_modulus)

    def bcm_aesx_process(self, src, dst, size, flag, num_sectors, engine_id):
        return self.bcm_cmd(71, src, dst, size, flag, num_sectors, engine_id)

    def drbg_gen_bits(self, num_bits, dst):
        # max is 0x10000*8 bits
        # dst must not be 0, but will be aligned down by 8
        # doesn't have the infleak from old bcm :(
        # this outputs data even if drbg is not in "instatiated" state - might be different generator altogether
        return self.bcm_cmd(28, num_bits, dst)

    def drbg_reseed(self):
        # requires drbg_instantiate
        addr = 0x100
        return self.bcm_cmd(30, addr, addr)

    def drbg_instantiate(self, num_bits):
        # [128, 192, 256]
        # 128: 0x20, 192: 0x28, 256: 0x30
        # reseed needs instatiate first
        # cmds that return "STATUS_INVALID_TOKEN" means STATUS_DRBG_RESEED_REQUIRED. status code mismatch
        # cmds working after instantiate: 30, 300, 65
        addr = 0x100
        return self.bcm_cmd(64, num_bits, addr, addr)

    def drbg_generate(self):
        # requires drbg_instantiate
        # arg0: buf_in, 0x20byte (with instantiate num_bits=128)
        # arg1: buf1_out, 0x10byte (with instantiate num_bits=128,arg4=0)
        # arg2: buf2_out, 0x10byte (with instantiate num_bits=128)
        # arg3: buf3_out, 0x10byte (with instantiate num_bits=128)
        # arg4: num_dwords_buf1_out. buf1 is 4 dwords if arg4=0. [0,0x10000]//4
        # arg5:
        # arg6:
        # arg7: [0,0x20] has no effect?
        # arg8:
        # arg9: device?
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x2000 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [b'\0'*buf_len for i in range(len(addrs))]
        #for addr, arg in zip(addrs, args): self.write(addr, arg)
        rv = self.bcm_cmd(65, 1, 1, 1, 1, 0x10000//4,
                            0, 0, 0, 0, 0)
        print('rv', rv)
        return rv

    def otp_read(self, index):
        # it seems to only ever return "07 07 00 01 00 00 00 00"
        # no longer takes index
        dst = 0x1000
        pattern = 0xdeadbeef.to_bytes(4, 'little')
        self.write(dst, pattern * (0x1000//4))
        rv = self.bcm_cmd(32, dst, index, index, index, index)
        if rv != 0:
            print(f'otp_read error {rv} {STATUS_MAP.get(rv)}')
            return b''
        # length varies
        buf = self.read(dst, 0x1000)
        return buf[:buf.find(pattern)]

    def bcm_hmac_init(self, alg, arg2, arg3):
        return self.bcm_cmd(22, alg, arg2, arg3)

    def bcm_hmac_zeroize(self):
        return self.bcm_cmd(23)

    def bcm_hmac_final(self, msg_addr, digest_addr, msg_len, flag):
        return self.bcm_cmd(25, msg_addr, digest_addr, msg_len, flag)

    def bcm_hmac_load_key(self, key_len, key_addr, alg, arg4=0):
        # no longer seems to support RKEK with addr == -1
        # aes also doesn't support RKEK
        return self.bcm_cmd(26, key_len, key_addr, alg, arg4)

    def bcm_hmac_test(self, key_len):
        self.bcm_hmac_zeroize()
        alg = 5
        digest_lens = {0: 0x14, 1: 0x1c, 2: 0x20, 3: 0x30, 4: 0x40, 5: 0x10}
        self.bcm_hmac_load_key(key_len, 0, alg)
        self.bcm_hmac_init(alg, 0, 0) # last arg does something (bool)
        self.bcm_hmac_final(0, 0x200, 0, 1)
        digest = self.read(0x200, digest_lens.get(alg))
        return digest

    def aes_test(self):
        buf_addr = 0x100
        buf_len = 0x100
        self.write(buf_addr, b'\x00' * buf_len)

        key_bitlen = 128
        engine = 2
        # 0: ecb, 1: cbc, 2: ctr, 3: xts, 4: kwp?, 5: ofb
        # 3 sector size is controlled by ... process(flag)?
        # 4 only completes once
        cipher_mode = 4
        is_encrypt = True
        assert 0 == self.bcm_aesx_load_iv(buf_addr, engine)
        assert 0 == self.bcm_aesx_load_key(key_bitlen, buf_addr, 0, engine)
        assert 0 == self.bcm_aesx_init(0 if is_encrypt else 1, key_bitlen, cipher_mode, engine, 0, 0, 0, 0, 0, 0, 0)

        src = buf_addr
        dst = buf_addr
        rv = self.bcm_aesx_process(src, dst, buf_len, 0, 0, engine)
        assert rv == 0

        rb = self.read(dst, buf_len)
        hexdump(rb)

    SRAM_BASE = 0x01000000
    SRAM_SIZE = 0x001e0000
    SRAM_END = 0x011e0000
    def exploit(self, val):
        src = 0x1000
        dst = 0x100000
        size = val
        num_dwords = size//4
        aligned_size = num_dwords * 4
        #self.write(src, b'\0' * 0x1000)
        #self.write(src, val.to_bytes(size, 'big'))
        #self.write(src, bytes([2]) * aligned_size)
        buf = b''.join([(0xaabb0000+x).to_bytes(4, 'little') for x in range(num_dwords)])
        self.write(src, buf)
        self.write(dst, b'\0' * aligned_size)
        #print('src')
        #hexdump(self.read(src, 0x1000))
        self.bcm_cmd(56, 0, 0, num_dwords, src, dst)
        print('dst')
        result = self.read(dst, aligned_size)
        hexdump(result)
        print(hashlib.md5(result).hexdigest())

    def scan_cmds(self):
        for i in range(400):
            # these timeout
            # 58 just takes longer (than 200 wait), will complete
            #if i in (15, 35, 56, 58, 75): continue
            if i in (6, 35, 56, 58): continue
            rv = self.bcm_cmd(i)
            # ignore STATUS_UNSUPPORTED_FUNCTION
            if rv in (262,): continue
            print(f'cmd {i} return_status {rv} {STATUS_MAP.get(rv)}')
        # cmds which definitely take ll_in (might be missing some): 14, 20, 21, 24, 25, 34, 311, 312, 313, 314
        # same but for ll_out: 14

    def bcm_zmodp_zeroize(self):
        return self.bcm_cmd(54)
    # the hw seems to expect you to generate some "precomputed param" from the modulus,
    # then pass it along to successive operations.
    # the hw takes values as LE
    # if arg6=0,arg7=0,arg8=1, arg2 is bitlen of output
    # if arg6=0,arg7=0,arg8=0, arg2 is ignored and arg1==1 causes different output
    #   arg1==1 causes arg7 to be used as some size? it seems related to input value (arg4)
    #   arg1==1 also uses arg8 somehow
    # if arg6==1, doesn't seem to write to output buffer
    # zeroize is needed beforehand. otherwise (e.g. if zmodp_mul is previous), the output will be mixed with modulus value
    def bcm_zmodp_precomp_param(self, op, op_size, num_dwords, modulus, modulus_pc, arg6, arg7, arg8):
        return self.bcm_cmd(56, op, op_size, num_dwords, modulus, modulus_pc, arg6, arg7, arg8)
    def bcm_zmodp_inv(self, op, op_size, num_dwords, modulus, modulus_pc, value, result, arg7, arg8, arg9):
        return self.bcm_cmd(58, op, op_size, num_dwords, modulus, modulus_pc, value, result, arg7, arg8, arg9)
    def bcm_zmodp_mul(self, op, op_size, num_dwords, modulus, modulus_pc, a, b, result, arg9, arg10, arg11):
        return self.bcm_cmd(59, op, op_size, num_dwords, modulus, modulus_pc, a, b, result, arg9, arg10, arg11)
    def bcm_zmodp_exp(self, op, op_size, num_dwords, modulus, base, modulus_pc, exponent, result, arg9, arg10, arg11):
        return self.bcm_cmd(60, op, op_size, num_dwords, modulus, base, modulus_pc, exponent, result, arg9, arg10, arg11)
    def bcm_zmodp_add(self, op, op_size, num_dwords, modulus, value, modulus_pc, result, arg8, arg9, arg10, arg11, arg12):
        return self.bcm_cmd(62, op, op_size, num_dwords, modulus, value, modulus_pc, result, arg8, arg9, arg10, arg11, arg12)
    def bcm_zmodp_sub(self, op, op_size, num_dwords, modulus, value, modulus_pc, result, arg8, arg9, arg10, arg11):
        return self.bcm_cmd(63, op, op_size, num_dwords, modulus, value, modulus_pc, result, arg8, arg9, arg10, arg11)

    class BcmDmaLLBuf:
        def __init__(self, addr, num_dwords, unk):
            # the last dword (unk) seems to be ignored
            # top 4 bits of num_dwords seems ignored
            self.addr, self.num_dwords, self.unk = addr, num_dwords, unk
    class BcmDmaLL:
        def __init__(self):
            self.bufs: list[Client.BcmDmaLLBuf] = []
        def add(self, addr, size, unk=0):
            assert size % 4 == 0
            num_dwords = size // 4
            self.bufs.append(Client.BcmDmaLLBuf(addr, num_dwords, unk))
        def get(self, ll_addr):
            # TODO try with ll_addr noncontig/weird places/circular/etc
            ll = b''
            next_entry = ll_addr + 0x10
            for i, buf in enumerate(self.bufs):
                is_last = i + 1 == len(self.bufs)
                ll += struct.pack('<4I', buf.addr, buf.num_dwords, 0 if is_last else next_entry, buf.unk)
                next_entry += 0x10
            return ll

    def _bcm_rsassa_pkcs1_v15_init(self, bit_len, n, e, hashmode):
        # arg0: bit_len. accepted range depends on hashmode
        # arg1: n
        # arg2: e
        # arg3: hashmode
        # n,e not read in at this point(?)
        return self.bcm_cmd(33, bit_len, n, e, hashmode)

    def _bcm_rsassa_pkcs1_v15_update(self, msg_addr, msg_len):
        # the msg_len must be multiple of some block size (0x40 bytes)
        return self.bcm_cmd(34, msg_addr, msg_len)

    def _bcm_rsassa_pkcs1_v15_final(self, sig_addr, sig_len, msg_addr=0, msg_len=0):
        # arg0: sig_addr. aligned down by 4
        # arg1: sig_len. error 52 if doesn't match bit_len provided to init
        # arg2: msg
        # arg3: msg_len
        # takes LL_IN for msg
        # hardfaults if sig_len==0 AND init hasn't been called
        return self.bcm_cmd(35, sig_addr, sig_len, msg_addr, msg_len)

    def _bcm_rsassa_pkcs1_v15_verify(self, rsa_len_bits, modulus_addr, exponent_addr, hashmode, msg_addr, msg_len, sig_addr, rsa_len, buf_in_ll_addr=None):
        return self.bcm_cmd(36, rsa_len_bits, modulus_addr, exponent_addr, hashmode, msg_addr, msg_len, sig_addr, rsa_len, ll_in=buf_in_ll_addr)

    def bcm_rsassa_pkcs1_v15_verify_test(self):
        rsa_len_bits = 1024
        rsa_len = rsa_len_bits // 8
        hashmode = 2
        from Crypto.PublicKey import RSA
        from Crypto.Signature import PKCS1_v1_5
        from Crypto.Hash import SHA256
        key = RSA.generate(rsa_len_bits)
        msg = b'A' * 0x10
        sig = PKCS1_v1_5.new(key).sign(SHA256.new(msg))
        print(f'n {key.n:x}')
        print(f'e {key.e:x}')
        print(f'sig {sig.hex()}')
        n_addr = 0x2000
        e_addr = 0x4000
        sig_addr = 0x6000
        msg_addr = 0x8000
        ll_addr = 0xa000
        self.write(n_addr, key.n.to_bytes(rsa_len, 'little'))
        self.write(e_addr, key.e.to_bytes(rsa_len, 'little'))
        self.write(sig_addr, sig[::-1])
        self.write(msg_addr, msg)
        link_list = self.BcmDmaLL()
        link_list.add(msg_addr, 0x10)
        #link_list.add(msg_addr+4, 4)
        #link_list.add(msg_addr+8, 4)
        #link_list.add(msg_addr+12, 4)
        ll = link_list.get(ll_addr)
        self.write(ll_addr, ll)
        print('ll')
        hexdump(ll)
        rv = self._bcm_rsassa_pkcs1_v15_verify(rsa_len_bits, n_addr, e_addr, hashmode, msg_addr, len(msg), sig_addr, rsa_len, ll_addr)
        print('rsa_verifiy', rv)

    def bcm_cmd_130(self, buf_len):
        # arg0: buf_in
        # arg1: buf_in_num_dwords. must be multiple of 2(8bytes)
        # arg2: buf_out
        # arg3: buf_out. last 16 bytes from arg0
        # arg4: engine_id. must be zero?
        # if len isn't multiple of 4(16), semi-weird outputs
        # if len > 0xc00/4, semi-weird outputs
        buf_in_addr = 0
        iv_out_addr = buf_in_addr
        buf_out_addr = 0x1e0000-buf_len
        buf_in = b'\xcc'*buf_len
        #buf_len = len(buf_in)
        num_dwords = buf_len // 4
        self.write(buf_in_addr, buf_in)
        self.write(buf_out_addr, b'\xff'*buf_len)
        rv = self.bcm_cmd(130, buf_in_addr, num_dwords, buf_out_addr, iv_out_addr, 0)
        buf_out = self.read(buf_out_addr, buf_len)
        #print(rv, buf_out.hex())
        return rv, buf_out

    def bcm_cmd_131(self, buf_len):
        # arg0: buf_in
        # arg1: size
        # arg2: buf_out
        # arg3: buf_out
        # arg4: engine_id
        # similar(inverse?) to cmd 130
        buf_in_addr = 0
        iv_out_addr = buf_in_addr
        buf_out_addr = 0x1e0000-buf_len
        buf_in = b'\xcc'*buf_len
        num_dwords = buf_len // 4
        self.write(buf_in_addr, buf_in)
        self.write(buf_out_addr, b'\xff'*buf_len)
        rv = self.bcm_cmd(131, buf_in_addr, num_dwords, buf_out_addr, iv_out_addr, 0)
        buf_out = self.read(buf_out_addr, buf_len)
        #print(rv, buf_out.hex())
        return rv, buf_out

    def bcm_cmd_134(self):
        from Crypto.PublicKey import RSA
        from Crypto.Signature import PKCS1_v1_5
        from Crypto.Hash import SHA1
        key = RSA.generate(1024)
        msg = b'A'*0x10
        # generates rsa pkcs1.5 signature sig=Encode(Hash(msg)^d%n)
        # takes LL_IN. for msg
        # arg0: bit_len. must be multiple of 32 [0,0x2000]. accepted range changes by hashmode.
        # arg1: n_addr. aligned down by 4. sized by bit_len.
        # arg2: d_addr. aligned down by 4. sized by bit_len.
        # arg3: hashmode
        # arg4: msg. aligned down by 4. sized by arg5
        # arg5: msg_bytelen
        # arg6: sig_out
        n_addr = 0
        d_addr = n_addr + key.size_in_bytes()
        msg_addr = d_addr + key.size_in_bytes()
        result_addr = msg_addr + len(msg)
        self.write(n_addr, key.n.to_bytes(key.size_in_bytes(), 'little'))
        self.write(d_addr, key.d.to_bytes(key.size_in_bytes(), 'little'))
        self.write(msg_addr, msg)
        rv = self.bcm_cmd(134, key.size_in_bits(), n_addr, d_addr, 0, msg_addr, len(msg), result_addr)
        output = self.read(result_addr, key.size_in_bytes())
        assert PKCS1_v1_5.new(key).verify(SHA1.new(msg), output[::-1])
        print(rv)
        return rv, output

    def bcm_cmd_162(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x2000 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [buf_for_arg(i) for i in range(len(addrs))]
        # always seems to return 564. maybe another cmd must setup some internal state first
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        rv = self.bcm_cmd(162, addrs[0], addrs[1], addrs[2], addrs[3], addrs[4], addrs[5], addrs[6], addrs[7],
                     addrs[8], addrs[9], addrs[10], addrs[11], addrs[12], addrs[13])
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb[:len(arg)] == arg[:len(rb)]: continue
            print(i, rb.hex())

    def bcm_cmd_300(self):
        # arg0: device? must be 0
        # requires drbg_instantiate
        # the cmd seems to be something like drbg_zeroize
        return self.bcm_cmd(300, 0)

    def bcm_gen_rand(self, buf_out, num_dwords):
        return self.bcm_cmd(301, buf_out, num_dwords)

    def bcm_cmd_307(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x100 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [buf_for_arg(i) for i in range(len(addrs))]
        args[0] = b'\0' * buf_len
        args[1] = b'\0' * buf_len
        args[7] = b'\xff' * buf_len
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        # buf_in1, buf_in2, buf_in_len(read from both), other_size [4,0xfc], arg5, hashmode, num_dwords_out, buf_out
        # arg3: xfer size inputs. [1,0x40] rounded down to multiple of 4?
        # arg4: xfer size? [4,0xfc] multiples of 4 only
        # arg5: if out of range, enters weird errorstate where timer is in return_status
        # arg6: hashmode. if out of range, get encoded error in status[1]
        # arg7: return 563 if 0. odd errors. hardfault if return 832 >= once
        #  if hashmode==md5 && other_size==0xfc, [0x11,0x40] hardfaults
        # arg8: output
        rv = self.bcm_cmd(307, addrs[0], addrs[1], 1,
                          4, # something to do with size of inputs as well
                          0x100, # arg5. some delay/counter?
                          0, # hashmode
                          0x10, addrs[7])
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb == arg[:len(rb)]: continue
            print(i, rb.hex())
        return rv

    def bcm_cmd_308(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x100 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [buf_for_arg(i) for i in range(len(addrs))]
        args[4] = b'\xff'*buf_len
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        # buf_in, num_dwords_in [1,0xff], hashmode, num_dwords_out [1,0x100], buf_out
        rv = self.bcm_cmd(308, addrs[0], 1, 4, 0x100, addrs[4])
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb == arg[:len(rb)]: continue
            print(i, rb.hex())
        return rv

    def bcm_cmd_309(self, num_bytes, addr):
        # num_bytes, buf_out
        # another random byte generator?
        return self.bcm_cmd(309, num_bytes, addr)

    def bcm_cmd_310(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x100 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [buf_for_arg(i) for i in range(len(addrs))]
        args[0] = bytes(range(buf_len))
        args[1] = b'\xff'*buf_len
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        # buf_in, buf_out, num_dwords [1,0x100]
        # copies dwords in reverse from buf_in to buf_out
        rv = self.bcm_cmd(310, addrs[0], addrs[1], 0x100)
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb == arg[:len(rb)]: continue
            print(i, rb.hex())
        return rv

    def bcm_rsapss_sign_prepare(self, bit_len, hashmode, buf1, buf2=b''):
        # 311: bit_len <= 0x2000, hashmode, buf_in1, buf_in1_len, buf_in2, buf_in2_len, buf_out, buf_out, buf_out, buf_out, buf_out, buf_out, buf_out
        # bit_len should be multiple of 32, else PANIC_UNEXPECTED_USAGE_FAULT(recoverable)
        # sha1: if bit_len >= 0x40 && <= 0xa0, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # sha224: if bit_len >= 0x60 && <= 0xe0, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # sha256: if bit_len >= 0x60 && <= 0x100, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # sha384: if bit_len >= 0x80 && <= 0x180, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # sha512: if bit_len >= 0xa0 && <= 0x200, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # md5: if bit_len >= 0x40 && <= 0x80, STATUS_FATAL_INTERNAL_ERROR(HardFault)
        # input buffer lens are somehow related to bit_len
        # LL_IN is supported. used in place of buf2 only
        buf_in1 = 0x1000
        buf_in2 = 0x3000
        bufs_out = 0x5000
        self.write(buf_in1, buf1)
        if len(buf2) > 0: self.write(buf_in2, buf2)
        #self.write(bufs_out, b'\xff'*0x7000)
        '''outputs:
        0: X || Hash(outputs[2]) || 0xbc
        1: Hash(buf2)
        2: pad || Hash(buf2) || buf1
        3: Hash(outputs[2])
        4: "remainder"?
        5: MGF1(seed=outputs[3], hash_gen=Hash) XXX how is length determined?
        6: X. derived from outputs[4], outputs[5]
        '''
        rv = self.bcm_cmd(311, bit_len, hashmode, buf_in1, len(buf1), buf_in2, len(buf2),
                          bufs_out, bufs_out+0x1000, bufs_out+0x2000, bufs_out+0x3000, bufs_out+0x4000, bufs_out+0x5000, bufs_out+0x6000)
        return rv#, [self.read(bufs_out+0x1000*i, (bit_len+7)//8) for i in range(7)]

    def bcm_cmd_312(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x100 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [b'\xff'*buf_len for i in range(len(addrs))]
        # rsapss sign
        # takes LL_IN (for msg)
        # 0: n_addr
        # 1: d_addr
        # 2: hashmode
        # 3: msg_addr
        # 4: msg_bytelen
        # 5: salt_addr
        # 6: salt_bytelen
        # 7: output. sig^d % n
        # 8: rsa_bitlen
        # 9: output. EMSA-PSS sig plaintext
        # 10: output[9], bytereversed
        from Crypto.PublicKey import RSA
        key = RSA.generate(1024)
        key = RSA.construct((0xa36473e48c7b08e8d9330412569b2e4844b6f6d8983e3e80ee1e9e061231dee4d5f5a5448d18b88e6fdd100f5eed09a862b7cccc768f7551bd41d57d4d26a380027d31d0a1fea7fc33e05d04758742272be6fca1255ca0b2ff7d2030b8f70f2de9739aeb6f688b0c0d0fea1ba8b320b4a36877e108a6934372b16f3ce09ea27f, 0x10001, 0x7e365a4f713498ed44d9e38b46db18253aef1a3eb55b5f6cb8836e0dc280436d00d97f90346da42262c6a0613f325b03773e0334ed60637f80721a37702128a9bb16dbe1c113e1d69caf6bab1f52f579e3cf54759de632dd406952a86b50d5eabfae5310140fe6761455b65965172012be2f7990f33a8ce35fb84fc2fa6f041))
        bit_len = key.size_in_bits()
        byte_len = key.size_in_bytes()
        args[0] = key.n.to_bytes(byte_len, 'little')
        args[1] = key.d.to_bytes(byte_len, 'little')
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        from Crypto.Signature import pss
        from Crypto.Hash import SHA1
        sig = pss.new(key).sign(SHA1.new())
        print(hex(key.n))
        print(hex(key.d))
        print(sig.hex())
        rv = self.bcm_cmd(312, addrs[0], addrs[1], 0, addrs[3], 0, addrs[5], 0, addrs[7], bit_len, addrs[9], addrs[10])
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb[:len(arg)] == arg[:len(rb)]: continue
            print(i, rb.hex())
        return rv

    def bcm_cmd_313(self):
        buf_len = 0x100
        dwords_max = buf_len//4
        addrs = [0x100 + buf_len*i for i in range(14)]
        def buf_for_arg(index):
            return b''.join([(0xaab00000 + x + (index << 16)).to_bytes(4, 'little') for x in range(dwords_max)])
        args = [bytes([0xff])*buf_len for i in range(len(addrs))]
        args[6] = b'\6'*buf_len
        args[2] = b'\2'*buf_len
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        # takes LL_IN (for msg)
        # arg1: bitlen
        # arg2: hashmode
        # arg3: salt?
        # arg4: salt_bytelen
        # arg5: msg
        # arg6: msg_len
        # arg7: buf_in3
        # arg8: output Hash(msg)
        # arg9: output 8zeros || Hash(msg) || Unk(len=salt_bytelen)
        # arg10: output
        # arg11: output MGF1(buf_in3)
        # arg12: output
        from Crypto.Signature import pss
        from Crypto.Hash import SHA1
        print(pss.MGF1(bytes.fromhex('0606060606060606060606060606060606060606'), 0x20, SHA1).hex())
        rv = self.bcm_cmd(313, 1024, 0, addrs[2], 0x10, addrs[4], 0, addrs[6],
                          addrs[7], addrs[8], addrs[9], addrs[10], addrs[11], addrs[12])
        print('rv', rv)
        args_rb = [self.read(addr, buf_len) for addr in addrs]
        for i, (rb, arg) in enumerate(zip(args_rb, args)):
            if rb == arg[:len(rb)]: continue
            print(i, rb.hex())
        return rv

    def bcm_cmd_314(self):
        buf_len = 0x100
        addrs = [0x100 + buf_len*i for i in range(14)]
        args = [bytes([0])*buf_len for i in range(len(addrs))]
        # some sort of signature check func
        # TODO didnt it write to arg11 somehow?
        # takes LL_IN (for msg)
        # arg1: n_addr
        # arg2: e_addr
        # arg3: hashmode
        # arg4: msg_addr
        # arg5: msg_bytelen
        # arg6: salt_addr. EMSA-PSS salt value to compare
        # arg7: salt_bytelen [0,0x80]
        # arg8: sig_addr
        # arg9: rsa_bitlen
        # arg10: output: sig^e % n. written even if verify fails
        from Crypto.PublicKey import RSA
        key = RSA.generate(1024)
        bit_len = key.size_in_bits()
        byte_len = key.size_in_bytes()
        msg = b'A' * 0
        precomp = bytes.fromhex('7ccb54222079c84c343b0ab16307273b36359229bd3dfdeca9fe8054ad1ef31944758a673b7c70c2facb6fe912690ee26df58975585a78c2723f0c7150535c808f0868f6ca94f36cfb079fbb9126286d5eeca3caaca12593033a0d64136a7a72d605080a6cf68b6dda0ae6a35d1688a60ac69fd53e44428bfd380e94db9176bc')
        sig = pow(int.from_bytes(precomp, 'big'), key.d, key.n)
        salt = b''
        args[0] = key.n.to_bytes(byte_len, 'little')
        args[1] = key.e.to_bytes(byte_len, 'little')
        args[3] = msg
        args[5] = salt
        args[7] = sig.to_bytes(byte_len, 'little')
        for addr, arg in zip(addrs, args): self.write(addr, arg)
        # hard fault: (something to do with digest len and sig len again)
        rv = self.bcm_cmd(314, addrs[0], addrs[1], 5, addrs[3], len(msg), addrs[5], 0, addrs[7], 128, addrs[9])
        #rv = self.bcm_cmd(314, addrs[0], addrs[1], 0, addrs[3], len(msg), addrs[5], len(salt), addrs[7], bit_len, addrs[9])
        return rv, self.read(addrs[9], buf_len)

    def bcm_cmd_315(self):
        # just puts some fuse vals in status regs
        # fucky because efc says "BCM eFuse key read fail" if it fails. maybe populates some engine's key?
        rv = self.bcm_cmd(315)
        flag1 = self.bcm_read_status(1) & 3
        flag2 = self.bcm_read_status(2) & 1
        flag3 = self.bcm_read_status(3)
        return flag1, flag2, flag3

    def bcm_bignum_write(self, addr, val: int, num_dwords):
        self.write(addr, val.to_bytes(num_dwords * 4, 'little'))

    # if arg3 is zero, dma breaks for some stuff (e.g. aes_load_iv)
    def bcm_configure_dma(self, a1, a2, a3, a4, a5):
        # arg3 >= 0x20: starts timing out. bits 7,8 set causes timeout
        # arg2, arg4 should be bools
        # arg1 is bool == 0x40. width? if != 0x40, only xfers 32bits every 64bits
        # arg5 nonzero makes it weird
        # arg2 is dword swap (dw0, dw1 swapped)
        # arg4 is endianness (across 8bytes). effect depends on arg1 and arg2
        # 1 more arg
        return self.bcm_cmd(6, a1, a2, a3, a4, a5)

    def bcm_configure_dma_default(self):
        # seems to be how it's setup when eap booted
        self.bcm_configure_dma(64, 0, 1, 0, 0)

    def bcm_zmodp_selftest(self):
        # from eldora bcm irom
        modulus = 0x1fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffa51868783bf2f966b7fcc0148f709a5d03bb5c9b8899c47aebb6fb71e91386409
        modulus_pc = 0x7f7382da1a9b5012b82a73f3128fbd00099ddf2cc48f17cde4070400f85a24c4faeabf3393570c807ac8e94d91dd3f7b3c0ac45838086267b5ef2f556e23410efe
        a = 0x4bd32c305f9297ddcf19d3925b0500824d23c645e5304eb87ccd72b7692119d24bc98f73928d865f9bbd574b8688569fb089bcf320d83359dd1b56c56c1679a6
        b = 0x19a5b5a3afe8c44383d2d8e03d1492d0d455bcc6d61a8e567bccff3d142b7756e3a4fb35b72d34027055d4dd6d30791d9dc18354a564374a6421163115a61c64ca7
        expected = 0x1aa7e21a06d41c29e083ad0268072f11685f9406496415821992d6a000a29727aacd7e7fd7f89b5a3cb12cb268d8c8f22c791383078fe5a9a4f0313a3e765c905eb
        # note op_size 3 == 32 dwords
        # op and op_size _seem_ ignored on titania
        m_addr = 0x100
        mpc_addr = 0x180
        a_addr = 0x200
        b_addr = 0x280
        result_addr = 0x300
        num_dwords = 17
        size = num_dwords * 4
        self.bcm_bignum_write(m_addr, modulus, num_dwords)
        self.bcm_bignum_write(mpc_addr, modulus_pc, num_dwords)
        self.bcm_bignum_write(a_addr, a, num_dwords)
        self.bcm_bignum_write(b_addr, b, num_dwords)
        self.write(result_addr, b'\0' * size)
        rv = self.bcm_zmodp_mul(1, 3, num_dwords, m_addr, mpc_addr, a_addr, b_addr, result_addr, 0, 0, 0)
        result = self.read(result_addr, size)
        result = int.from_bytes(result, 'little')
        print(hex(result))
        assert result == expected
        assert (a * modulus_pc) % modulus == expected # TODO fix arg order

        #self.bcm_zmodp_zeroize()

        # matches "b" value at arg2=[521,...]
        size = 32*4
        num_dwords = size//4
        m_addr = 0x1000
        mpc_addr = 0x100000
        self.bcm_bignum_write(m_addr, modulus, num_dwords)
        for i in range(1, 538):
            if i in (98,106, 109, 230,238,246,251,537, 538): continue
            self.write(mpc_addr, b'\0' * size)
            self.bcm_zmodp_precomp_param(0, i, num_dwords, m_addr, mpc_addr, 0, 0, 1)
            result = self.read(mpc_addr, size)
            result = int.from_bytes(result, 'little')
            print(i, hex(result), result == modulus_pc, result == b, result == a)

    def reinstall_hax(self):
        #self.write(0x58000000, Path('eap_uart_shell').read_bytes())
        self.write(0x58800000,
            struct.pack('<2I', 0x58800000 + 4 * 2 + 0x1c, 0x0000D274) +
            struct.pack('<9I', 4, 0xB0AC, 0xC50078, 7, 8, 9, 10, 0x58800000 + 4 * (2 + 9 * 1) + 0x1c, 0x0000D274) +
            struct.pack('<9I', 4, 0xA344, 6, 7, 8, 9, 10, 0x58800000 + 4 * (2 + 9 * 2) + 0x1c, 0x0000D274) +
            struct.pack('<9I', 4, 0x58000000, 6, 7, 8, 9, 10, 0x58800000 + 4 * (2 + 9 * 3) + 0x1c, 0x0000D274)
            )

    def fill_mem(self):
        def make_buf(offset, size):
            return b''.join([(0xaab00000 + offset + x).to_bytes(4, 'little') for x in range(size//4)])
        chunk_len = 0x1000
        for i in trange(self.SRAM_BASE, self.SRAM_END, chunk_len):
            self.write(i, make_buf(i, chunk_len))
        if self.is_eap():
            return
        for i in trange(0, 0x18000, chunk_len, desc='atcm.cpu0'):
            self.write(i, make_buf(i, chunk_len))
        for i in trange(0x800000, 0x810000, chunk_len, desc='btcm.cpu0'):
            self.write(i, make_buf(i, chunk_len))
        for i in trange(0x900000, 0x910000, chunk_len, desc='stcm'):
            self.write(i, make_buf(i, chunk_len))

    def dump_mem(self, tag: str, **kwargs):
        is_eap = self.is_eap()
        cpu_name = 'eap' if is_eap else 'efc'
        chunk_len = 0x1000

        dump_sram = kwargs.get('sram', False)
        dump_spm = kwargs.get('spm', False)
        dump_atcm = kwargs.get('atcm', False)
        dump_btcm = kwargs.get('btcm', False)
        dump_tcm_cpu = kwargs.get('tcm_cpu', None)
        dump_stcm = kwargs.get('stcm', False)

        def dump_one(name: str, start: int, size: int):
            with dump_path(name).open('wb') as f:
                for i in trange(start, start + size, chunk_len, desc=name):
                    f.write(self.read(i, chunk_len))

        if dump_sram:
            dump_one(f'{tag}.sram.{cpu_name}', self.SRAM_BASE, self.SRAM_SIZE)

        if is_eap:
            return

        if dump_spm:
            '''
            0x18305000 0x1000 sig0
            0x18307000 0x1000 sig1
            0x18308000 0x3000 spm
            '''
            dump_one(f'{tag}.18305000', 0x18305000, 0x6000)

        ''' ??
        dump_one(f'{tag}.1b000000', 0x1b000000, 0x20000)
        dump_one(f'{tag}.1b100000', 0x1b100000, 0x123000)
        #'''

        for cpu_id in range(4):
            if dump_tcm_cpu is not None and dump_tcm_cpu != cpu_id:
                continue

            atcm_base = 0x8000000 + cpu_id * 0x2000000
            # check should vary based on cpu access comes from
            if cpu_id == 0:
                atcm_base = 0
            btcm_base = atcm_base + 0x800000
            atcm_size = 0x18000
            if cpu_id == 2: atcm_size += 0x6000
            if cpu_id == 3: atcm_size -= 0x6000
            btcm_size = 0x10000

            if dump_atcm:
                dump_one(f'{tag}.cpu{cpu_id}.atcm', atcm_base, atcm_size)
            if dump_btcm:
                dump_one(f'{tag}.cpu{cpu_id}.btcm', btcm_base, btcm_size)

        if dump_stcm:
            dump_one(f'{tag}.stcm', 0x900000, 0x10000)

    def ipc_desc_regs(self, src_cpu: int, dst_cpu: int) -> int:
        # titania has IpcFifo hw @ 14000000 which is new compared to eldora, but this seems to be rom/rom-handoff only
        # normal efc fw still uses Ipc hw @ 10114000
        # the ipc irq comes through vic (as in eldora)

        # each desc is 8bytes IpcPacket_t
        ipcDescOffset = [
            [0, 0, 2, 4],
            [6, 6, 8, 0xA],
            [0xC, 0xE, 0xE, 0x10],
            [0x12, 0x14, 0x16, 0x16]
        ]
        return 0x10114000 + 0x100 + 8 * ipcDescOffset[src_cpu][dst_cpu]

    def ipc_write_cmd(self, src_cpu: int, dst_cpu: int, cmd: int, wParam: int, lParam: int):
        desc_addr = self.ipc_desc_regs(src_cpu, dst_cpu)
        self.write32(desc_addr + 4, lParam)
        self.write32(desc_addr + 0, wParam << 16 | cmd << 2 | src_cpu)

    def ipc_jump(self, dst_cpu: int, addr: int):
        # XXX HalIpc_JumpToAddress uses some state in stcm and rIpc.INT_PEND_CLR before sending the cmd
        # it seems only for overriding timed-out funcs tho?
        #for i in range(4): print(f'{i} {self.read_regs32_str(0x9060A0+0x10*i, 4)}')
        self.ipc_write_cmd(0, dst_cpu, 0x10, 0, addr)

    def ipcfifo_get_regs(self, src_cpu: int, dst_cpu: int) -> int:
        # cpu0 uses this for initial JumpToAddress only.
        ipcfifo_reg_map_boot = [
            [0, 1, 3, 5],
            [0, 0, 7, 9],
            [2, 6, 0, 0xB],
            [4, 8, 0xA, 0],
        ]
        idx_boot = ipcfifo_reg_map_boot[dst_cpu][0]
        # cpu0 uses this for all other IpcFifo calls, but none of them actually have callers.
        ipcfifo_reg_map_fw = [
            [0, 1, 3, 0],
            [0, 7, 2, 6],
            [0, 0, 0, 0],
            [0, 0, 0, 0],
        ]
        idx_fw = ipcfifo_reg_map_fw[dst_cpu][0]
        return 0x14000000 + 0x1000 * idx_boot

    def test_rom_ctrl(self, cpu_index):
        APB_BASE = 0x10115000
        rom_ctrl = {
            0: APB_BASE + 0x10,
            1: APB_BASE + 0x20,
            2: APB_BASE + 0x30,
            3: APB_BASE + 0x284,
        }[cpu_index]
        ctrl = 0x01500000
        # (1 << 13) # error status reset. clears bit 9 once set. if ecc_enable[15]==1.
        #ctrl |= 1 << 14 # test data select. takes 38bit input from regs
        ctrl |= 1 << 15 # ecc enable. doesn't seem to have effect. reg+8 is somehow input. can't use test data input?
        #ctrl |= 1 << 31 # powerdown enable
        self.write32(rom_ctrl, ctrl)
        self.write32(rom_ctrl, ctrl | (1 << 13))
        self.write32(rom_ctrl, ctrl)
        self.write32(rom_ctrl + 4, 0)
        self.write32(rom_ctrl + 8, 0) # no effect on status bits when using test data input(?). bit 31 is write-only, read as 0.
        print(self.read_regs32_str(rom_ctrl, 3))

        for i in range(0x10):
            self.write32(rom_ctrl + 4, i)
            self.write32(rom_ctrl + 8, (1 << 31) | i)
            print(self.read_regs32_str(rom_ctrl, 3), end='')
            status = (self.read32(rom_ctrl) >> 8) & 0b11
            if status & 1: print(' corrected', end='')
            if status & 2: print(' errdet', end='')
            print()
            self.write32(rom_ctrl, ctrl | (1 << 13))
            self.write32(rom_ctrl, ctrl | i)

    def disable_predictors(self):
        actlr = self.reg_read(Reg.ACTLR)
        DEOLP = 1 << 21 # disable end of loop prediction
        DBHE = 1 << 20 # disable branch history
        FRCDIS = 1 << 19 # disable fetch rate control
        RSDIS = 1 << 17 # disable return stack
        BP_TAKEN = 0b01 << 15
        BP_NOT_TAKEN = 0b10 << 15
        actlr &= ~(0b11 << 15) # BP
        actlr |= DEOLP | DBHE | FRCDIS | RSDIS | BP_TAKEN
        self.reg_write(Reg.ACTLR, actlr)

# TODO get unexpected response from bcm when sending cmd during chip reset, investigate

def read_regs(addr, size):
    return b''.join([x.to_bytes(4, 'little') for x in c.read_regs32(addr, size//4)])

if __name__ == '__main__':
    from tool import Ps5UartEnum, Ucmd
    ROM_BAUD = 230400*2
    EFC_FW_BAUD = 230400*2
    EAP_FW_BAUD = 230400*3
    gpio_is_eap = Ucmd().fc_cpu_get()
    gpio_is_rom = Ucmd().fc_bootmode_get()
    if gpio_is_rom:
        port = Ps5UartEnum.titania_rom_port()
        baudrate = ROM_BAUD
    else:
        port = Ps5UartEnum.titania_fw_port()
        baudrate = EAP_FW_BAUD if gpio_is_eap else EFC_FW_BAUD
    #Ucmd().load_eap()
    c = Client(port, baudrate)
    assert c.ping()
    print(f"connected to {'eap' if gpio_is_eap else 'efc'} {'rom' if gpio_is_rom else 'fw'}")


'''
hexdump(read_regs(0x10115000, 0x1000), offset=0x10115000)
exit()

for i in range(0, 1<<32, 0x100000):
    x = c.read32(i + 0x1bffc)
    if x == 0: continue
    print(f'{i:8x}')
    hexdump(read_regs(i + 0x1bfc0, 0x40))

exit()

#c.fill_mem()
#c.reinstall_hax()
#c.dump_mem()

rv, output=c.bcm_cmd_314()
print(rv, output.hex())

# TODO try 315/reset/crypto stuff to detect if key gets loaded
print(c.bcm_cmd_315())

for i in range(0, 0x100, 4):
    print(f'{i:8x} {c.read32(0x18180000 + i):8x}')

#'''

# 01.02.00
# bcm for TITANIA_1 RCASP IROM 01.04.17A release at Mar 12 2019
'''
if not c.bcm_get_version_info() == 0:
    print('version failed')
    exit()
#'''

# clocking
# min required for eap to not die
# bcm needs bit 17
#c.write32(0x10112090, 0x18102800)

'''eap
midr    410fc075 ARM Cortex-A7 r0p5
dbgdrar  18120003
dbgdsar     10003
vbar        9880
'''
# VICs @ PA 0xFFFFE000, 0xFFFFF000
# vic0 handles 32-63, vic1 handles 0-31
'''
       0   1e0000
 1000000  11e0000
1010a000 1010d000
1010f000 10110000
10111000 10119000
died on 1011a000
10120000 10122000
10123000 10124000
10128000 1012a000
10200000 10300000
11000000 11002000
11010000 11020000
11100000 11200000
12000000 died on 12010000
18100000 died on 18102000
18110000 18181000
died on 18190000
181a0000 181a1000
died on 181b0000
18305000 1830b000
19000000 died on 1b000000 (thru 1b222000 dies)
40000000 60000000
80400000 80401000
80410000 80411000
80420000 80421000
80430000 80431000
80480000 80482000
80483000 8048b000
80497000 80498000
8049e000 804a3000
804a4000 804a8000
804a9000 804ac000
804b0000 804b5000
804c0000 804c1000
804c2000 804c9000
804cb000 804ce000
804d1000 804d2000
80600000 80604000
80610000 80620000
806f0000 80704000
80708000 80709000
80710000 80711000
80742000 80745000
80746000 80747000
80750000 80751000
80760000 80762000
80764000 80765000
80766000 8076b000
80780000 80781000
80790000 80791000
80800000 8092c000
80a00000 80b04000
80b0c000 80b12000
died on 80b3c000...restart @ 80c00000
died on cfffe000...restart @ d0000000
ffffe000 ffffffff
'''

#c.exploit(0x1404)
