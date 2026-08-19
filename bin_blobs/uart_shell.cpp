#include <arm_acle.h>
#include <array>
#include <cstdint>
#include <cstring>

#define WORD_ALIGN  //__attribute__((aligned(4)))

using u8 = uint8_t WORD_ALIGN;
using u16 = uint16_t WORD_ALIGN;
using u32 = uint32_t WORD_ALIGN;
using u64 = uint64_t WORD_ALIGN;
using vu8 = volatile u8;
using vu16 = volatile u16;
using vu32 = volatile u32;
using vu64 = volatile u64;

// UART0: EFC firmware, EAP firmware
// UART1: Titania bootroms, APU, ...
#define UART0_BASE 0x11010000
#define UART1_BASE 0x11010100
#if !defined(ROM_ENV)
#define UART_BASE UART0_BASE
#else
#define UART_BASE UART1_BASE
#endif

void* memset(void* ptr, int value, size_t num) {
  auto p = (u8*)ptr;
  while (num--) {
    *p++ = (u8)value;
  }
  return ptr;
}

static u16 crc16_ccitt(const u8* data, size_t length) {
  u16 crc = 0xFFFF;

  for (size_t i = 0; i < length; i++) {
    crc ^= (u16)data[i] << 8;
    for (u8 bit = 0; bit < 8; bit++) {
      if (crc & 0x8000) {
        crc = (crc << 1) ^ 0x1021;
      } else {
        crc <<= 1;
      }
    }
  }
  return crc;
}

static inline u32 arm_cpsr_read() {
  u32 cpsr;
  asm volatile("mrs %0, cpsr" : "=r"(cpsr));
  return cpsr;
}

struct ArmSR {
  u32 coproc{};
  u32 op1{};
  u32 crn{};
  u32 crm{};
  u32 op2{};
};

constexpr ArmSR DBGDRAR{14, 0, 1, 0, 0};
constexpr ArmSR DBGDSAR{14, 0, 2, 0, 0};
constexpr ArmSR DBGPRCR{14, 0, 1, 4, 4};
constexpr ArmSR MIDR{15, 0, 0, 0, 0};
constexpr ArmSR MPIDR{15, 0, 0, 0, 5};
constexpr ArmSR SCTLR{15, 0, 1, 0, 0};
constexpr ArmSR ACTLR{15, 0, 1, 0, 1};
constexpr ArmSR VBAR{15, 0, 12, 0, 0};
constexpr ArmSR DFSR{15, 0, 5, 0, 0};
constexpr ArmSR DFAR{15, 0, 6, 0, 0};

constexpr ArmSR BTCMREGION{15, 0, 9, 1, 0};
constexpr ArmSR ATCMREGION{15, 0, 9, 1, 1};
constexpr ArmSR TCMSELECTION{15, 0, 9, 2, 0};

constexpr inline void arm_dsb() {
  asm volatile("dsb" ::: "memory");
}

constexpr inline void arm_isb() {
  asm volatile("isb" ::: "memory");
}

constexpr inline void arm_wfi() {
  asm volatile("wfi");
}

constexpr inline u32 arm_rsr(const ArmSR& sr) {
  return __arm_mrc(sr.coproc, sr.op1, sr.crn, sr.crm, sr.op2);
}

constexpr inline void arm_wsr(const ArmSR& sr, u32 value) {
  __arm_mcr(sr.coproc, sr.op1, value, sr.crn, sr.crm, sr.op2);
  arm_dsb();
  arm_isb();
}

u32 cpu_id() {
  // core id is in aff1. aff0 is 0 for all cores.
  return (arm_rsr(MPIDR) >> 8) & 0xff;
}

struct DAbortRecord {
  u32 iaddr{UINT32_MAX};
  u32 daddr{UINT32_MAX};
  u32 status{UINT32_MAX};
};

// Trampoline for runtime-generated MRC/MCR instructions.
// Two words: [0]=MRC/MCR instruction, [1]=BX LR.
// With MPU and I-cache disabled, executing from RAM works.
static u32 cp15_trampoline[2] __attribute__((aligned(4)));

// Set nonzero by the UNDEF handler when a CP15 probe faults.
extern "C" {
vu32 g_paborted;
vu32 g_irq_fired;
volatile u32 g_undef_caught;
// IRQ handler jumps here instead of returning to the interrupted instruction.
volatile u32 g_irq_recovery;
// Exception register dump — filled by exception handlers, read by exec_rom
struct ExcDump {
  u32 r[16];  // R0-R15 (R15 = faulting PC)
  u32 cpsr;
  u32 dfar, dfsr;
  u32 ifar, ifsr;
  u32 exc_type;  // 0=reset,1=undef,2=swi,3=pabort,4=dabort,5=irq,6=fiq
  u32 lr_exc;    // LR value in the exception mode
} g_exc_dump;
}
static DAbortRecord g_abort_status;

extern "C" void exception_irq();

// Arbitrary CP15 read/write via runtime-generated MRC/MCR.
// Protocol: read u32 with fields:
//   [2:0]   op1
//   [6:3]   CRn
//   [10:7]  CRm
//   [13:11] op2
//   [14]    is_write
//   [18:15] cpnum (coprocessor number, default 15)
// If write: read another u32 (the value to write).
// Returns u32: result value, or 0xDEADDEAD if UNDEF (register doesn't exist).

void coproc_read(u32* undef, u32* val, u32 params) {
  u8 op1 = (params >> 0) & 0x7;
  u8 crn = (params >> 3) & 0xF;
  u8 crm = (params >> 7) & 0xF;
  u8 op2 = (params >> 11) & 0x7;
  u8 cpnum = (params >> 15) & 0xF;
  if (cpnum == 0)
    cpnum = 15;  // backwards compat: 0 means p15

  // MRC cpnum, op1, R0, CRn, CRm, op2
  cp15_trampoline[0] = 0xEE100010u | ((u32)op1 << 21) | ((u32)crn << 16) |
                       ((u32)cpnum << 8) | ((u32)op2 << 5) | crm;
  cp15_trampoline[1] = 0xE12FFF1Eu;
  arm_dsb();
  arm_isb();

  g_undef_caught = 0;

  auto func = (u32 (*)())(void*)cp15_trampoline;
  *val = func();
  *undef = g_undef_caught;
}

void coproc_write(u32* undef, u32 params, u32 val) {
  u8 op1 = (params >> 0) & 0x7;
  u8 crn = (params >> 3) & 0xF;
  u8 crm = (params >> 7) & 0xF;
  u8 op2 = (params >> 11) & 0x7;
  u8 cpnum = (params >> 15) & 0xF;
  if (cpnum == 0)
    cpnum = 15;

  // MCR cpnum, op1, R0, CRn, CRm, op2
  cp15_trampoline[0] = 0xEE000010u | ((u32)op1 << 21) | ((u32)crn << 16) |
                       ((u32)cpnum << 8) | ((u32)op2 << 5) | crm;
  cp15_trampoline[1] = 0xE12FFF1Eu;
  arm_dsb();
  arm_isb();

  g_undef_caught = 0;

  auto func = (void (*)(u32))(void*)cp15_trampoline;
  func(val);
  *undef = g_undef_caught;
}

struct Uart {
  // Not available over UART: the fused path writes payload straight into MAC
  // frame buffers, and there are none here. 0 tells the host to use the staged
  // path (cmd 18) instead.
  u32 nand_stream_frames(u32, u32, u32, u32, u32, u32, u32, u32, u32) { return 0; }

  static constexpr u8 UART_FCR_FIFOE{1 << 0};
  static constexpr u8 UART_FCR_FIFO64{1 << 5};
  static constexpr u8 UART_LSR_RX_READY{1 << 0};
  static constexpr u8 UART_LSR_TEMT{1 << 6};

  enum class Timeout : u32 {
    Infinite = 0xffffffff,
  };

  struct Regs {
    // XXX titania specific
    vu32 reg0;
    vu32 reg1;
    vu32 reg2;
    vu32 reg3;
    vu32 reg4;
    vu32 reg5;
    vu32 reg6;
    vu32 reg7;
    vu32 reg8;
  };

  Uart() {
    // eap_kbl normally inits/uses uart1
    // On EAP, we can use uart0 (which hasn't really been setup) as-is,
    // but the baudrate seems to be ~700000
    regs_ = (Regs*)UART_BASE;
  }
  u32 fifo_max_len() const {
    u32 len = 1;
    // u8 fcr = regs_->fcr;
    // if (fcr & UART_FCR_FIFOE) {
    //     len = (fcr & UART_FCR_FIFO64) ? 64 : 16;
    // }
    return len;
  }
  inline bool rx_ready() const {
    // regs_->lsr & UART_LSR_RX_READY
    return (regs_->reg3 >> 4) & 1;
  }
  inline bool tx_ready() const {
    // regs_->lsr & UART_LSR_TEMT
    return (regs_->reg3 >> 5) & 1;
  }
  void wait_tx_ready() const {
    while (!tx_ready()) {
    }
  }
  bool wait_rx_ready(Timeout timeout) const {
    u32 rem = (u32)timeout;
    while (!rx_ready()) {
      if (timeout == Timeout::Infinite) {
        continue;
      }
      if (rem == 0) {
        return false;
      }
      rem--;
    }
    return true;
  }
  void write(const u8* buf, u32 len) {
    const u32 fifo_len = fifo_max_len();
    const u8* p = buf;
    while (len) {
      wait_tx_ready();
      for (u32 i = 0; i < fifo_len && len; i++, len--) {
        regs_->reg1 = *p++;
      }
    }
  }
  void write(const char* buf) { write((const u8*)buf, strlen(buf)); }
  template <typename T>
  void write(const T& data) {
    write((const u8*)&data, sizeof(T));
  }
  bool read_byte(u8* data, Timeout timeout) {
    if (!wait_rx_ready(timeout)) {
      return false;
    }
    *data = regs_->reg0;
    return true;
  }
  bool read(u8* data, u32 len, Timeout timeout = (Timeout)1000000) {
    auto p = data;
    while (p != data + len) {
      if (!read_byte(p++, timeout)) {
        return false;
      }
    }
    return true;
  }
  template <typename T>
  bool read(T* data) {
    return read((u8*)data, sizeof(T));
  }

  Regs* regs_{};
};

struct BcmMbox {
  // write-only
  // arg 14: linked-list dma host-read  pointer
  // arg 15: linked-list dma host-write pointer
  std::array<vu32, 16> args;
  vu32 cmd;
  vu32 reserved[60 / 4];
  // read-only
  vu32 return_status;
  std::array<vu32, 16> status;
  vu32 fifo_status;
  // write-to-clear
  vu32 host_intp_reg;
  vu32 host_intp_mask;
  vu32 host_exct_addr;
  // read-only
  vu32 sp_trust_reg;
  vu32 wtm_id;
  vu32 wtm_revision;
};

struct Bcm {
  u32 send_cmd(u32 cmd, u32 timeout = 10000) {
    mbox.cmd = cmd;
    if (!wait(timeout)) {
      return UINT32_MAX;
    }
    return mbox.return_status;
  }
  bool wait(u32 timeout) {
    u32 retries = 0;
    while ((mbox.host_intp_reg & 1) == 0) {
      if (retries++ >= timeout) {
        return false;
      }
    }
    // try to ack everything
    mbox.host_intp_reg = 0xffffffff;
    return true;
  }
  void aes_process(u8* buf_in, u8* buf_out, u32 len, u32 flag, u32 flag2) {
    mbox.args[0] = (u32)buf_in;
    mbox.args[1] = (u32)buf_out;
    mbox.args[2] = len;
    // 0: use previous context, 1: new ctx
    mbox.args[3] = flag;
    mbox.args[4] = 0;
    mbox.args[5] = 0;
    mbox.args[6] = 0x10000000;  // ?
    mbox.args[7] = 0;
    mbox.args[8] = flag2;  // ?
    // hmmm (dma linked list)
    mbox.args[14] = 0;
    mbox.args[15] = 0;
    send_cmd(14);
  }
  BcmMbox& mbox{*(BcmMbox*)0x19000000};
};

struct Ipc {
  vu32* desc_regs(u8 src_cpu, u8 dst_cpu) const {
    const u32 ipcDescOffset[4][4] = {
        {0, 0, 2, 4},
        {6, 6, 8, 0xA},
        {0xC, 0xE, 0xE, 0x10},
        {0x12, 0x14, 0x16, 0x16},
    };
    u32 addr = 0x10114000 + 0x100 + 8 * ipcDescOffset[src_cpu][dst_cpu];
    return (vu32*)addr;
  }

  void write_cmd(u8 src_cpu,
                 u8 dst_cpu,
                 u16 cmd,
                 u16 wParam,
                 u32 lParam) const {
    vu32* desc_addr = desc_regs(src_cpu, dst_cpu);
    desc_addr[1] = lParam;
    desc_addr[0] = wParam << 16 | cmd << 2 | src_cpu;
  }

  void jump(u8 dst_cpu, u32 addr) const {
    write_cmd(cpu_id(), dst_cpu, 0x10, 0, addr);
  }
};

struct VicRegs {
  vu32 VICIRQSTATUS;
  vu32 VICFIQSTATUS;
  vu32 VICRAWINTR;
  vu32 VICINTSELECT;
  vu32 VICINTENABLE;
  vu32 VICINTENCLEAR;
  vu32 VICSOFTINT;
  vu32 VICSOFTINTCLEAR;
  vu32 VICPROTECTION;
  vu32 VICSWPRIORITYMASK;
  vu8 _pad[0x100 - 0x28];
  vu32 VICVECTADDR[32];
  vu8 _pad2[0x200 - 0x180];
  vu32 VICVECTPRIORITY[32];
  vu8 _pad3[0xf00 - 0x280];
  vu32 VICADDRESS;
  vu8 _pad4[0xfe0 - 0xf04];
  vu32 VICPERIPHID[4];
  vu32 VICPCELLID[4];

  void init() {
    VICINTSELECT = 0;
    VICINTENCLEAR = 0xffffffff;
  }
  void set_enabled(u32 idx, bool enable) {
    u32 mask = 1 << idx;
    VICINTENCLEAR = mask;
    // eap_kbl doesn't store addrs into vic (stores 0 here instead)
    VICVECTADDR[idx] = 0;
    if (enable) {
      VICINTENABLE |= mask;
    }
  }
  void enable(u32 idx) { set_enabled(idx, true); }
  void disable(u32 idx) { set_enabled(idx, false); }
};

struct Vic {
  void init() {
    vic0.init();
    vic1.init();
  }
  void set_enabled(u32 idx, bool enable) {
    if (idx < 32) {
      vic0.set_enabled(idx, enable);
      return;
    }
    idx -= 32;
    if (idx < 32) {
      vic1.set_enabled(idx, enable);
      return;
    }
  }
  void enable(u32 idx) { set_enabled(idx, true); }
  void disable(u32 idx) { set_enabled(idx, false); }
  VicRegs& vic0{*(VicRegs*)0xfffff000};
  VicRegs& vic1{*(VicRegs*)0xffffe000};
};

struct Apb {
  void hpet_reset() {
    auto rst = (vu32*)(base + 0x424);
    *rst = 1;
    *rst &= ~1;
  }
  void bcm_reset() {
    auto rst = (vu32*)(base + 0x458);
    *rst = 1;
    *rst = 0;
  }
  const u32 base{0x10115000};
};

struct HpetTimerRegs {
  vu64 cfg_cap;
  vu64 comparator_value;
  vu64 int_route;  // not impl?
  vu8 _pad[0x20 - 0x18];
};
static_assert(sizeof(HpetTimerRegs) == 0x20, "");

struct HpetRegs {
  vu64 cap_id;
  vu16 _pad;
  vu64 cfg;
  vu16 _pad2;
  vu64 int_status;
  vu8 _pad3[0xf0 - 0x28];
  vu64 main_counter;
  HpetTimerRegs timers[4];
};

struct Hpet {
  void reset() {
    auto apb = Apb();
    apb.hpet_reset();
    regs.cfg |= 1;
  }
  HpetRegs& regs{*(HpetRegs*)0x10111000};
};

// 16-bit counter that counts down
// the counter stays at zero once reached.
// alarm fires when counter == compare
// timeout fires when counter == 0
// reboot firing *does* reset the soc/core
// prescaler divides the input clock (counter updates slower). divisors are: 1,
// 4, 16, 64, 256, larger divisors stay at 256 the input clock seems to have
// period of 256ns (~3.906 MHz) so prescaler = 1 means divisor=4, and counter
// period is 256ns*4 = 1.024us
struct WdtRegs {
  vu32 compare;     // 0x00 [15:0] compare value
  vu32 counter;     // 0x04 [15:0] current count (read/write)
  vu32 ctrl;        // 0x08 b7=EN, b2=ALM_INTR_EN, b1=TO_INTR_EN, b0=REBOOT_EN,
                    // b[5:3]=prescaler
  vu32 int_status;  // 0x0C b0=ALM_INTR, b1=TO_INTR, b2=RBTD (write 1 to clear)
};

struct Wdt {
  void alarm_start(bool use_irq,
                   u16 counter,
                   u8 prescaler = 0,
                   u16 compare_val = 0) {
    regs.ctrl = 0;
    int_ack_all();
    int_enable(use_irq);
    regs.counter = counter;
    regs.compare = compare_val;
    // alarm generates vic int 39 (32+7)
    // timeout interrupt only shows status on wdt, not vic
    regs.ctrl = (1 << 7) | ((prescaler & 7) << 3) | (1 << 2);
  }
  void reset(u16 counter = 0) { regs.counter = counter; }
  u16 time_since(u16 start) { return start - regs.counter; }
  bool reboot_fired() { return (regs.int_status >> 2) & 1; }
  bool timeout_fired() { return (regs.int_status >> 1) & 1; }
  bool alarm_fired() { return regs.int_status & 1; }
  void int_ack_all() { regs.int_status = 0b111; }
  void int_enable(bool enable) {
    // alarm (timeout/reboot aren't routed to vic it seems)
    vic.set_enabled(39, enable);
  }
  WdtRegs& regs{*(WdtRegs*)0x10116000};
  Vic vic;
};

static void enable_caches(bool enable) {
  auto sctlr = arm_rsr(SCTLR);
  // icache, dcache
  u32 mask = (1 << 12) | (1 << 2);
  if (enable) {
    sctlr |= mask;
  } else {
    sctlr &= mask;
  }
  arm_wsr(SCTLR, sctlr);
}

static void flush_l1_data_cache() {
  unsigned int ccsidr, sets, ways;
  unsigned int way, set;

  // Select L1 Data Cache in CSSELR (Cache Size Selection Register)
  __asm__ volatile("mcr p15, 2, %0, c0, c0, 0" : : "r"(0));
  // Read CCSIDR
  __asm__ volatile("mrc p15, 1, %0, c0, c0, 0" : "=r"(ccsidr));

  ways = ((ccsidr & 0x1FF8) >> 3) + 1;
  sets = ((ccsidr >> 13) & 0x7FFF) + 1;

  for (way = 0; way < ways; way++) {
    for (set = 0; set < sets; set++) {
      // Construct address format for DCISW (Data cache clean and invalidate by
      // set/way)
      unsigned int val = (way << 30) | (set << 5);
      // Write to CP15 c7 (Clean and Invalidate by Set/Way)
      __asm__ volatile("mcr p15, 0, %0, c7, c6, 2" : : "r"(val));
    }
  }
  arm_dsb();
  arm_isb();
}

// Command handlers reach the wire only through |uart_|.read()/.write(), so the
// transport is a template parameter and a GbE transport with the same shape as
// Uart gets every command below unchanged. Templated rather than virtual: the
// payload is position-sensitive and there is one instantiation per build.
// Named Io because the handlers already use T for their member templates.
//
// The ROM build has a hard size ceiling and no PCIe bring-up behind it, so GbE
// is compiled out there.
#if !defined(ROM_ENV)

// TX-side loss counters. A fragment can go missing because the MAC never sent
// it (a swallowed kick: tx_queue kicks per frame but only the batch end is
// verified) or because it was dropped on the wire. g_tx_stall counts bursts
// where the cursor never reached the last queued descriptor, g_tx_short counts
// the descriptors left behind.
static u32 g_tx_stall;
static u32 g_tx_short;

// ---------------------------------------------------------------------------
// GbE transport -- Salina func0 (104D:9104), driven from EFC.
//
// Bring-up stays on the host over UART: PCIe enumeration, BAR assignment,
// clock/reset and the PHY init are done in Python
// (gbe_bringup.py, phy_init.py). This code assumes the MAC is alive with the
// link up and does only what the reference bring-up does -- rings, station
// address, enable -- plus a polled service loop.
//
// Rings must live in DRAM. SRAM_PROT (0x1012202C) is narrowed and locked by
// firmware and denies the PCIe master write access to SRAM, so rings placed
// there are read by the engine but never written back, and no PCIe error is
// raised. The host passes 0x58100000 by default.
// ---------------------------------------------------------------------------
struct GbeMac {
  // RXBUF (+0x30) is written 0x10100, and 0x100 == 256 is the ring length the
  // hardware assumes. Build fewer than that and the engine runs off the end
  // into unarmed memory and halts after one frame.
  static constexpr u32 N_RX = 64;
  // tx_drain() waits for completion once per burst, so ring depth sets how
  // often that wait is paid; 16 stalled every 15 frames. TX descriptors have
  // 0x1000 (64 entries is 0x400) and TX buffers run ring+0x40000..0x60000,
  // clear of the RX buffers ending at ring+0x30000.
  static constexpr u32 N_TX = 64;
  // Arm the whole ring. A short ring fills from ambient traffic within seconds,
  // and once full the engine drops everything and the frame counter stops.
  static constexpr u32 RX_USED = N_RX;
  static constexpr u32 DESC_SZ = 16;
  static constexpr u32 BUFSZ = 2048;       // stride; suits both formats
  static constexpr u32 RX_LEN = 0x600;
  static constexpr u32 RX_LEN_MASK = 0x7FF;
  static constexpr u32 DESC_DONE = 0x80000000;
  static constexpr u32 DESC_WRAP = 0x40000000;
  static constexpr u32 DESC_SOP = 0x20000000;
  static constexpr u32 DESC_EOP = 0x10000000;
  // A third bit alongside SOP|EOP, and it is required:
  // without it the engine consumed 7 of 16 kicks, with it 16 of 16.
  static constexpr u32 DESC_TX_RDY = 0x08000000;
  static constexpr u32 DESC_W2_ARM = 0xFFFF0000;
  static constexpr u32 DESC_W2_TX = 4;
  // The reference path acks BIT(5) for RX and BIT(7) for TX, but this board sets
  // BIT(6) instead (ISR reads 0x00040044 after a receive). Ack both and treat
  // either as RX-done.
  static constexpr u32 ISR_RX_DONE_FW = 0x20;   // what the reference path acks
  static constexpr u32 ISR_RX_DONE_HW = 0x40;    // what the hardware sets here
  static constexpr u32 ISR_RX_DONE = ISR_RX_DONE_FW | ISR_RX_DONE_HW;
  static constexpr u32 ISR_TX_DONE = 0x80;
  // Arms a descriptor for receive: the reference path recycles with the size
  // and nothing else (| DESC_WRAP on the last entry) and drops frames longer
  // than that. Received length is word0[15:0]; errors are word0 & 0x30000.
  static constexpr u32 RX_ARM_FW = BUFSZ;       // 0x800
  static constexpr u32 RX_ERR_MASK = 0x30000;
  // Frames land at offset 0 here and the descriptor length covers the frame
  // from the destination MAC. Some MACs pad by 2 to align an IP header; check
  // before changing this, because a wrong offset shifts every parse and the
  // ethertype silently stops matching.
  static constexpr u32 RX_PAD = 0;

  vu32& reg(u32 off) { return *(vu32*)(bar_ + off); }

  // ---- PHY -----------------------------------------------------------------
  // Order: read the PHY ID over MDIO, reset the MAC (+0xAC = 9), then run the
  // calibration from the salina efuse before declaring the link up.
  // PHY bring-up belongs inside MAC init, after the clock enable and before
  // CTRL/SERDES/MODE. Running it from the host beforehand leaves those writes
  // going to a PHY configured against the old settings and RX never starts.
  // PCS/SerDes registers are in Salina Glue (func3) BAR4 + 0x4000.
  static constexpr u32 SMI_BUSY = 0x8000;
  static constexpr u32 SMI_DONE = 0x8000;

  void spin(u32 us) {
    // Each MMIO read crosses PCIe and costs on the order of a microsecond, so
    // this is approximate but never shorter than asked.
    for (u32 i = 0; i < us; i++) {
      u32 t = reg(0x50);
      asm volatile("" : : "r"(t));   // keep the read
    }
  }

  u32 pcs_rd(u32 off) { return *(vu32*)(pcs_ + off); }

  bool smi_wait() {
    for (u32 i = 0; i < 20000; i++) {
      if (reg(0x00) & SMI_DONE) {
        return true;
      }
    }
    return false;
  }

  u16 cl22_read(u32 r) {
    reg(0x00) = SMI_BUSY;
    reg(0x00) = ((r & 0x1F) << 8) | 0x4000;
    if (!smi_wait()) {
      return 0;
    }
    return (u16)(reg(0x00) >> 16);
  }

  void smi_write(u32 r, u16 val) {
    reg(0x00) = SMI_BUSY;
    reg(0x00) = ((u32)val << 16) | ((r & 0x1F) << 8) | 0x2000;
    smi_wait();
  }

  bool cl45_addr(u32 sel) {
    reg(0x00) = SMI_BUSY;
    reg(0x00) = (sel & 0xFFFF0000) | ((sel & 0x1F) << 8) | 0x20;
    return smi_wait();
  }

  u16 cl45_read(u32 sel) {
    if (!cl45_addr(sel)) {
      return 0;
    }
    reg(0x00) = SMI_BUSY;
    reg(0x00) = ((sel & 0x1F) << 8) | 0xE0;
    if (!smi_wait()) {
      return 0;
    }
    return (u16)(reg(0x00) >> 16);
  }

  void cl45_write(u32 sel, u16 val) {
    if (!cl45_addr(sel)) {
      return;
    }
    reg(0x00) = SMI_BUSY;
    reg(0x00) = ((u32)val << 16) | ((sel & 0x1F) << 8) | 0x60;
    smi_wait();
  }

  void smi_paged(u16 a, u16 b, u16 c) {
    u16 page = cl22_read(0x1F);
    smi_write(0x1F, 0x52B5);
    smi_write(0x11, a);
    smi_write(0x12, b);
    smi_write(0x10, c);
    smi_write(0x1F, page);
  }

  static u8 cal_lut_idx(u32 field) {
    static const u8 lut[64] = {
        0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f,
        0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f, 0x7f,
        0x7b, 0x7a, 0x75, 0x73, 0x70, 0x67, 0x64, 0x62,
        0x57, 0x55, 0x53, 0x51, 0x48, 0x46, 0x44, 0x42,
        0x40, 0x37, 0x35, 0x34, 0x32, 0x31, 0x30, 0x26,
        0x24, 0x23, 0x22, 0x21, 0x20, 0x16, 0x15, 0x14,
        0x13, 0x12, 0x11, 0x10, 0x07, 0x06, 0x05, 0x04,
        0x03, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,
    };
    field &= 0x7F;
    if (field < 2) {
      field = 1;
    }
    if (field >= 0x40) {
      field = 0x40;
    }
    return lut[field - 1];
  }

  void cal_lane(u32 sel, u8 cal, bool high) {
    u16 cur = cl45_read(sel);
    if (high) {
      cl45_write(sel, (u16)((cur & 0x00FF) | ((u32)cal << 8) | 0x8000));
    } else {
      cl45_write(sel, (u16)((cur & 0xFF00) | cal | 0x80));
    }
  }

  static u16 eq_preset(u32 field) {
    u32 f = field & 0x3F;
    if (f > 0x3C) {
      f = 0x3C;
    }
    return (u16)((f + 3) & 0xFF);
  }

  void phy_calibrate() {
    u32 v;
    v = pcs_rd(0x68);
    cl45_write(0x000E001E, (u16)((v & 0x3F) << 8));
    v = pcs_rd(0x68);
    cl45_write(0x0115001F, (u16)((v >> 6) & 7));

    v = pcs_rd(0x60);
    cal_lane(0x0174001E, cal_lut_idx(v), true);
    v = pcs_rd(0x60);
    cal_lane(0x0174001E, cal_lut_idx(v >> 0x13), false);
    v = pcs_rd(0x64);
    cal_lane(0x0175001E, cal_lut_idx(v >> 6), true);
    v = pcs_rd(0x64);
    cal_lane(0x0175001E, cal_lut_idx(v >> 0x19), false);

    v = pcs_rd(0x5C);
    cl45_write(0x0172001E, (u16)((cl45_read(0x0172001E) & 0xC0FF) | ((v >> 0xC) & 0x3F00)));
    v = pcs_rd(0x60);
    cl45_write(0x0172001E, (u16)((cl45_read(0x0172001E) & 0xFFC0) | ((v >> 7) & 0x3F)));
    v = pcs_rd(0x60);
    cl45_write(0x0173001E, (u16)((cl45_read(0x0173001E) & 0xC0FF) | ((v >> 0x12) & 0x3F00)));
    v = pcs_rd(0x64);
    cl45_write(0x0173001E, (u16)((cl45_read(0x0173001E) & 0xFFC0) | ((v >> 0xD) & 0x3F)));

    v = pcs_rd(0x5C) >> 0x1A;
    cl45_write(0x0012001E, (u16)((v << 0xA) | v));
    cl45_write(0x0016001E, (u16)(((u32)eq_preset(v) << 0xA) | v));
    v = pcs_rd(0x60) >> 0xD;
    cl45_write(0x0017001E, (u16)(((v & 0x3F) << 8) | (v & 0x3F)));
    cl45_write(0x0018001E, (u16)(((u32)eq_preset(v) << 8) | (v & 0x3F)));
    v = pcs_rd(0x64);
    cl45_write(0x0019001E, (u16)(((v & 0x3F) << 8) | (v & 0x3F)));
    cl45_write(0x0020001E, (u16)(((u32)eq_preset(v) << 8) | (v & 0x3F)));
    v = pcs_rd(0x64) >> 0x13;
    cl45_write(0x0021001E, (u16)(((v & 0x3F) << 8) | (v & 0x3F)));
    cl45_write(0x0022001E, (u16)(((u32)eq_preset(v) << 8) | (v & 0x3F)));

    cl45_write(0x0096001E, 0x8000);
    cl45_write(0x0037001E, 0x0033);
    cl45_write(0x0039001E, (u16)(cl45_read(0x0039001E) & 0xB7FF));
    cl45_write(0x0107001F, (u16)(cl45_read(0x0107001F) & 0xEFFF));
    cl45_write(0x0171001E, (u16)(cl45_read(0x0171001E) | 0x0180));
    cl45_write(0x0039001E, (u16)(cl45_read(0x0039001E) | 0x2000));
    cl45_write(0x0039001E, (u16)(cl45_read(0x0039001E) & 0xDFFF));
    spin(50);
    cl45_write(0x0171001E, (u16)(cl45_read(0x0171001E) & 0xFE7F));
  }

  // Path B in the driver -- "PS5", chip 0x110000 with a revision that is
  // neither 0x0100 nor 0x0200. It stops early and skips the path-D tail.
  void phy_static_init() {
    cl45_write(0x003E001E, 0xF8F8);
    cl45_write(0x0189001E, 0x0110);
    smi_paged(0xB90A, 0x006F, 0x8F82);
    smi_paged(0xBAEF, 0x002E, 0x968C);

    smi_write(0x1F, 0x0003);   // page-3 LED config
    smi_write(0x1C, 0x0C92);
    smi_write(0x1F, 0x0000);

    reg(0x78 + 4) = 25000000;  // RX_PCODE + 4

    cl45_write(0x0122001E, 0xFFFF);
    cl45_write(0x0234001E, 0x0180);
    smi_paged(0x2E00, 0x000E, 0x8FB0);

    cl45_write(0x0238001E, 0x0120);
    cl45_write(0x0120001E, 0x9014);   // chip == 0x110000
    cl45_write(0x0239001E, 0x0117);
    smi_paged(0x0001, 0x0004, 0x96A2);

    reg(0x78) = reg(0x78) & ~1u;

    cl45_write(0x003C0007, 0x0000);
    cl45_write(0x0033001E, (u16)(cl45_read(0x0033001E) & 0xEFFF));
    cl45_write(0x0268001F, 0x07F4);

    smi_paged(0x0004, 0x0000, 0x9686);
    smi_paged(0x0671, 0x0006, 0x8FAE);
    smi_paged(0x55A0, 0x0000, 0x83AA);

    cl45_write(0x0123001E, 0xFFFF);

    smi_paged(0x8670, 0x0001, 0x96A6);
    smi_paged(0x0072, 0x0000, 0x96B6);
    smi_paged(0x3210, 0x0000, 0x96B8);
    smi_paged(0x024A, 0x0000, 0x96A8);

    smi_paged(0x5010, 0x0000, 0x96A0);
    smi_paged(0x3028, 0x0000, 0x969E);
    smi_paged(0x504D, 0x0000, 0x9698);
    smi_paged(0x194F, 0x0002, 0x969A);
    cl45_write(0x014A001E, 0xEE20);
    cl45_write(0x019B001E, 0x0111);

    cl45_write(0x003E001E, 0x0000);
  }

  void phy_init() {
    cl22_read(0x02);   // PHYSID1
    cl22_read(0x03);   // PHYSID2

    reg(0xAC) = 9;
    spin(1000);

    // Calibration is only meaningful once both SerDes lanes have locked.
    if ((~pcs_rd(0x6C)) & 0x80800000) {
      // not locked; skip calibration, as the driver does
    } else {
      phy_calibrate();
    }
    phy_static_init();

    smi_write(0x04, (u16)(cl22_read(0x04) & 0xF3FF));
    reg(0x04) = reg(0x04) & 0x7FFFCFFF;

    cl22_read(0x04);
    cl22_read(0x09);
    smi_write(0x00, 0x0800);   // BMCR power down

    smi_write(0x04, (u16)((cl22_read(0x04) & 0xFE1F) | 0x01E0));
    smi_write(0x09, (u16)((cl22_read(0x09) & 0xFCFF) | 0x0200));
    smi_write(0x00, 0x1340);
    smi_write(0x00, (u16)(cl22_read(0x00) | 0x1200));   // AN enable + restart
  }

  // Descriptor rings and the ring registers. Shared by both register tails so
  // one can be swapped for the other without the descriptor format changing
  // underneath it.
  void build_rings(const u8* mac) {
    for (u32 i = 0; i < N_RX; i++) {
      u32 d = rx_desc_ + i * DESC_SZ;
      *(vu32*)(d + 4) = rx_buf_ + i * BUFSZ;
      *(vu32*)(d + 8) = 0;
      *(vu32*)(d + 12) = 0;
      dma_wmb();
      // Same value the firmware recycles with: the buffer size, nothing else.
      *(vu32*)(d + 0) = RX_ARM_FW | ((i == N_RX - 1) ? DESC_WRAP : 0);
    }
    dma_wmb();
    for (u32 i = 0; i < N_TX; i++) {
      u32 d = tx_desc_ + i * DESC_SZ;
      *(vu32*)(d + 4) = tx_buf_ + i * BUFSZ;
      *(vu32*)(d + 8) = DESC_W2_ARM;
      *(vu32*)(d + 12) = 0;
      // TX descriptors must be left CPU-OWNED, not zeroed. word0 == 0 reads as
      // "ready to send a zero-length frame", and because TX and RX share the DMA
      // engine that takes the RX path down with it: measured 0 RX frames with
      // word0 = 0 versus 64 with DESC_DONE. This single line is what made RX
      // look like it halted after one frame for the entire investigation.
      *(vu32*)(d + 0) = DESC_DONE | ((i == N_TX - 1) ? DESC_WRAP : 0);
    }
    reg(0x14) = (u32)mac[1] | ((u32)mac[0] << 8);
    reg(0x18) = ((u32)mac[2] << 24) | ((u32)mac[3] << 16) | ((u32)mac[4] << 8) |
                (u32)mac[5];
  }

  // The ring base registers: the high register of each pair goes first. They are written after the config registers, not with the
  // station address.
  void ring_regs() {
    reg(0x44) = tx_desc_;
    reg(0x3C) = tx_desc_;
    reg(0x48) = rx_desc_;
    reg(0x40) = rx_desc_;
  }

  // Follows the reference MAC bring-up rather than the ps5-linux mts driver.
  // The reference sequence configures the MAC from scratch and inherits no
  // prior register state, so the registers it never writes need no writing.
  void init(u32 bar, u32 ring_base, const u8* mac, u32 pcs, u32 rx_arm) {
    bar_ = bar;
    pcs_ = pcs;
    // bit31 of the parameter is the "mask IER off" flag and must NOT reach the
    // descriptor: arming with DESC_DONE set makes every slot look complete.
    ier_off_ = (rx_arm & 0x80000000u) != 0;
    kick_on_release_ = (rx_arm & 0x40000000u) == 0;   // default on
    fw_regs_ = (rx_arm & 0x20000000u) != 0;
    fw_fmt_ = (rx_arm & 0x02000000u) != 0;
    quiet_release_ = (rx_arm & 0x01000000u) != 0;
    skip_phy_ = (rx_arm & 0x00800000u) != 0;
    advance_cursor_ = (rx_arm & 0x00400000u) != 0;
    // bit21: arm RX word2 with the TX 'slot free' sentinel instead of 0. Kept
    // only as a switch: the reference path writes word0 and word1 and leaves
    // word2 alone, so 0 is what it effectively arms with.
    rx_w2_ = (rx_arm & 0x00200000u) ? DESC_W2_ARM : 0u;
    rx_en_ = (rx_arm & 0x10000000u) != 0;
    filt_ctrl_ = (rx_arm >> 26) & 3u;
    u32 a = rx_arm & 0x001FFFFFu;
    // Default to the value the firmware arms with, not RX_LEN. A non-zero
    // parameter still overrides so a sweep can be run.
    rx_arm_ = a ? a : RX_ARM_FW;
    // 256 * 16 = 0x1000 per descriptor ring; 256 * 0x600 = 0x60000 per buffer
    // pool. All well inside the DRAM window verified by dramfind.py.
    rx_desc_ = ring_base;                 // .. +0x1000
    tx_desc_ = ring_base + 0x1000;        // .. +0x2000
    rx_buf_ = ring_base + 0x10000;        // 64 * 0x600 = 0x18000
    tx_buf_ = ring_base + 0x40000;        // 16 * 0x600 = 0x6000
    rx_idx_ = 0;
    tx_idx_ = 0;

    // Firmware order exactly: reset, clock, PHY, then the MAC registers.
    // skip_phy_ leaves the PHY to the host's phy_init.py, the proven
    // 105-transaction replay. The ported phy_init() skips calibration entirely
    // if the SerDes lock check fails, so it is not necessarily equivalent.
    if (!skip_phy_) {
      reg(0xAC) = 9;                     // RESET
      spin(1000);
      reg(0x08) = reg(0x08) | 0x7597C00; // CLK_CTRL, GBE_CLK_ENABLE
      phy_init();                        // must precede CTRL/SERDES/MODE
    }
    // The reference bring-up sequence, in order. Registers it does not touch
    // are not written here either, even though the ps5-linux driver writes
    // some of them (0x0C, 0x10, 0x9C, 0x204, 0x54, 0x1C4, 0x1D4).
    build_rings(mac);                    // descriptors + station address
    // 0x00300000 | 0x4277 for subsystem id 0x00110300, which is what this board
    // reports. The 0x3277 variant belongs to subsystem 0x00110100.
    reg(0x74) = 0x00304277;
    // Write MAC_CFG absolutely, and twice, as the firmware does. Do not OR into
    // it to preserve the clock-enable bits from before phy_init: that sets bits
    // the firmware never sets, and the TX DMA channel then refuses to latch its
    // enable (0x34 reads back 0 after writing 1) so kicks are never consumed.
    // The front end needs the second write.
    reg(0x08) = 0x02197C00;              // MAC_CFG
    reg(0x1B8) = 1;                      // plain write, not read-modify-write
    reg(0x08) = 0x02597C00;              // MAC_CFG, second write
    reg(0x30) = 0x00010000;              // RXBUF; the driver's 0x10100 is not this
    ring_regs();
    reg(0x34) = DMA_EN;                  // TX enable, no kick during init
    reg(0x38) = DMA_EN;                  // RX enable, no kick during init
    reg(0x50) = 0;                       // clear INT_STS outright
  }

  // Plain writes, never read-modify-write: +0x34/+0x38 read back derived
  // status (0x08), not the last command, so `reg | bit` feeds status bits
  // back in as a command. Write enable|kick outright.
  static constexpr u32 DMA_EN = 1;
  static constexpr u32 DMA_KICK = 4;
  // The firmware orders the descriptor body before the ownership word; the
  // flags write. Caches are off here, but the R5 still has a write buffer:
  // without a barrier the flags store can reach DRAM before the body, or sit
  // buffered while the engine reads a stale descriptor and stalls.
  static void dma_wmb() { asm volatile("dsb" ::: "memory"); }

  // Push a range out to DRAM so the engine can see it. dma_wmb() is only a dsb:
  // it orders our stores against each other but does not make them visible to a
  // bus master that reads DRAM directly, and a read-back can be answered from
  // the write buffer without draining. That gap is what stalls TX -- the kick
  // reaches the engine over PCIe MMIO before the descriptor reaches DRAM, the
  // engine scans, still sees the CPU-owned word0 and finds nothing to send.
  // (Measured: after tx() the descriptor was correct at 0x3000003C with the
  // cursor parked; a bare kick from the host, touching nothing else, then
  // transmitted it.) DCCMVAC is a no-op when the D-cache is off, so this is safe
  // either way.
  // Invalidate a range so the CPU sees what the engine wrote, rather than a
  // stale line. Needed on the RX side once the D-cache is on: the DMA engine
  // writes received frames straight to DRAM, behind the cache's back.
  static void dcache_inv(u32 addr, u32 len) {
    for (u32 p = addr & ~31u; p < addr + len; p += 32)
      asm volatile("mcr p15, 0, %0, c7, c6, 1" ::"r"(p) : "memory");
    asm volatile("dsb" ::: "memory");
  }

  static void dcache_clean(u32 addr, u32 len) {
    for (u32 p = addr & ~31u; p < addr + len; p += 32)
      asm volatile("mcr p15, 0, %0, c7, c10, 1" ::"r"(p) : "memory");
    asm volatile("dsb" ::: "memory");
  }

  // Kick RX with a read-modify-write that only sets the kick bit. A plain
  // write of DMA_EN|DMA_KICK (5) clobbers the rest of the register, and on
  // this board +0x38 reads back as 8, so a store would clear bit 3. TX is
  // kicked the same way at +0x34.
  void kick_rx() { reg(0x38) = reg(0x38) | DMA_KICK; }

  // Arm the whole RX ring and reset our cursor. Split out of init() so a trial
  // can be repeated without re-running the PHY bring-up.
  void arm_rx() {
    for (u32 i = 0; i < N_RX; i++) {
      u32 d = rx_desc_ + i * DESC_SZ;
      *(vu32*)(d + 8) = rx_w2_;
      *(vu32*)(d + 12) = 0;
      dma_wmb();
      // Same value build_rings() and rx_release() use. These three had drifted
      // apart: init armed with 0x800 while arm_rx re-armed with rx_arm_ (0x600
      // by default), so a serviced ring no longer matched what the engine was
      // originally given.
      *(vu32*)(d + 0) = RX_ARM_FW | ((i == N_RX - 1) ? DESC_WRAP : 0);
    }
    dma_wmb();
    rx_idx_ = 0;
    // Mirror init()'s tail. Without it, nothing re-enables the engine once it
    // has halted.
    reg(0x48) = rx_desc_;
    reg(0x40) = rx_desc_;
    reg(0x34) = DMA_EN;
    reg(0x38) = DMA_EN;
    reg(0x50) = 0;
    kick_rx();
  }

  // Count landed frames by destination class, without consuming them. Done on
  // the target so the host never manipulates descriptors while the engine is
  // running -- host-side re-arming is what made the baseline irreproducible.
  void census(u32* uni, u32* bcast, u32* mcast, u32* other, const u8* mac) {
    *uni = *bcast = *mcast = *other = 0;
    for (u32 i = 0; i < N_RX; i++) {
      if (!(*(vu32*)(rx_desc_ + i * DESC_SZ) & DESC_DONE)) {
        continue;
      }
      const u8* d = (const u8*)(rx_buf_ + i * BUFSZ);
      bool is_b = true;
      bool is_u = true;
      for (u32 k = 0; k < 6; k++) {
        if (d[k] != 0xFF) {
          is_b = false;
        }
        if (d[k] != mac[k]) {
          is_u = false;
        }
      }
      if (is_b) {
        (*bcast)++;
      } else if (is_u) {
        (*uni)++;
      } else if (d[0] & 1) {
        (*mcast)++;
      } else {
        (*other)++;
      }
    }
  }

  // Frame pointer/length for the current RX descriptor, or nullptr. The caller
  // must copy the payload out and then call rx_release(): re-arming hands the
  // buffer straight back to the engine.
  u8* rx_peek(u32* len) {
    u32 w = *(vu32*)(rx_desc_ + rx_idx_ * DESC_SZ);
    if (!(w & 0x80000000)) {
      // Service RX-complete by acking and re-kicking
      if (reg(0x50) & ISR_RX_DONE) {
        reg(0x50) = ISR_RX_DONE;
        kick_rx();
      }
      return nullptr;
    }
    // Two descriptor encodings exist: a 16-bit length with the frame 2 bytes
    // into the buffer, and an 11-bit length starting at offset 0. The first is
    // the one measured to sustain reception on this board.
    if (fw_fmt_) {
      u32 n = w & 0xFFFF;
      if (((w >> 16) & 3) || n <= RX_PAD || n < 60 || n > BUFSZ) {
        return nullptr;
      }
      *len = n - RX_PAD;
      dcache_inv(rx_buf_ + rx_idx_ * BUFSZ, n);
      return (u8*)(rx_buf_ + rx_idx_ * BUFSZ + RX_PAD);
    }
    u32 n = w & RX_LEN_MASK;
    if (n < 60 || n > RX_LEN) {
      return nullptr;  // runt or oversize; caller still releases
    }
    *len = n;
    return (u8*)(rx_buf_ + rx_idx_ * BUFSZ);
  }

  bool rx_ready() {
    return (*(vu32*)(rx_desc_ + rx_idx_ * DESC_SZ) & DESC_DONE) != 0;
  }

  void rx_release() {
    // The recycle sequence: claim the slot with DESC_DONE, restore
    // the buffer pointer, then re-arm with the buffer size (| DESC_WRAP on the
    // last entry). It *does* rewrite the buffer pointer on every release -- an
    // earlier note here said the opposite, which came from reading gbe_rx_poll
    // in the ps5-linux driver rather than the firmware.
    u32 d = rx_desc_ + rx_idx_ * DESC_SZ;
    *(vu32*)(d + 0) = DESC_DONE;
    *(vu32*)(d + 4) = rx_buf_ + rx_idx_ * BUFSZ;
    dma_wmb();
    *(vu32*)(d + 0) = RX_ARM_FW | ((rx_idx_ == N_RX - 1) ? DESC_WRAP : 0);
    dma_wmb();
    rx_idx_ = (rx_idx_ + 1) % N_RX;
    // +0x40 is a base address, not a tail pointer, and is not written again
    // after init. Ack then kick, in that order, both unconditional plain
    // writes; do not read INT_STS first.
    reg(0x50) = ISR_RX_DONE;
    kick_rx();
  }

  u8* tx_frame() { return (u8*)(tx_buf_ + tx_idx_ * BUFSZ); }

  // Claim the slot with DESC_DONE, set the buffer pointer,
  // then write word0 = len | 0x38000000 (| DESC_WRAP on the last of 16) and
  // finally clear DESC_DONE to hand it to the engine. Kick with +0x34 = 5 and
  // wait for INT_STS 0x80, acking it with a plain write of 0x80.
  // Queue a frame without waiting for it, and drain the queue when it is full.
  // The completion wait is ~13 ms of PCIe polling, and paying it per frame is the
  // entire throughput limit; the ring holds 16 descriptors, so paying it once per
  // burst amortises it across up to 15 frames.
  void tx_queue(u32 len) {
    tx_write_desc(len);
    // Kick immediately but do NOT wait. The engine takes one frame per kick --
    // queuing several descriptors behind a single kick sent only the first and
    // multi-fragment responses came back short. What costs time is the
    // completion wait, not the kick, so issue the kick here and let tx_drain()
    // pay the wait once for the whole burst.
    reg(0x34) = reg(0x34) | DMA_KICK;
    tx_idx_ = (tx_idx_ + 1) % N_TX;
    if (++pending_ >= N_TX - 1) {
      tx_drain();
    }
  }

  // Kick once and wait for the engine to catch up with everything queued.
  void tx_drain() {
    if (!pending_) {
      return;
    }
    u32 want_end = tx_desc_ + tx_idx_ * DESC_SZ;
    for (u32 attempt = 0; attempt < 16; attempt++) {
      reg(0x34) = reg(0x34) | DMA_KICK;   // read-modify-write, not a store
      dma_wmb();
      for (u32 i = 0; i < 200000; i++) {
        if (reg(0x50) & 0x80) {
          reg(0x50) = 0x80;
        }
        if (reg(0x3C) == want_end) {
          break;
        }
      }
      if (reg(0x3C) == want_end) {
        break;
      }
    }
    // Did the engine actually take everything? If not, this burst silently
    // dropped frames -- exactly the "one fragment missing" signature.
    u32 cur = reg(0x3C);
    if (cur != want_end) {
      g_tx_stall++;
      u32 left = (want_end >= cur) ? (want_end - cur) : (want_end + N_TX * DESC_SZ - cur);
      g_tx_short += left / DESC_SZ;
    }
    pending_ = 0;
    // The shared engine needs a gap after transmitting before reception behaves.
    for (u32 i = 0; i < 20000; i++) asm volatile("" ::: "memory");
  }

  void tx(u32 len) {
    tx_queue(len);
    tx_drain();
  }

  // Write the descriptor for one frame at tx_idx_. No kick and no wait: the
  // caller decides when to hand a batch to the engine. Uses the software index,
  // not the hardware cursor -- while a batch is outstanding the cursor lags
  // behind on purpose, so following it here would overwrite queued frames.
  void tx_write_desc(u32 len) {
    u32 d = tx_desc_ + tx_idx_ * DESC_SZ;
    *(vu32*)(d + 4) = tx_buf_ + tx_idx_ * BUFSZ;
    *(vu32*)(d + 8) = DESC_W2_TX;
    *(vu32*)(d + 12) = 0;
    dma_wmb();
    u32 w = (len & 0xFFFF) | DESC_SOP | DESC_EOP | DESC_TX_RDY;
    if (tx_idx_ == N_TX - 1) {
      w |= DESC_WRAP;
    }
    *(vu32*)(d + 0) = w;
    dma_wmb();
    // Both the frame bytes and the descriptor must reach DRAM before the engine
    // reads them; dma_wmb() is only a dsb and does not make our stores visible
    // to a bus master.
    dcache_clean(tx_buf_ + tx_idx_ * BUFSZ, len);
    dcache_clean(d, DESC_SZ);
  }

  u32 pending_{};

  u32 bar_{};
  u32 pcs_{};
  u32 rx_arm_{RX_LEN};
  bool ier_off_{};
  bool kick_on_release_{true};
  bool fw_regs_{};
  bool fw_fmt_{};
  bool quiet_release_{};
  bool skip_phy_{};
  bool advance_cursor_{};
  u32 rx_w2_{};
  bool rx_en_{};
  u32 filt_ctrl_{};
  u32 rx_desc_{}, tx_desc_{}, rx_buf_{}, tx_buf_{};
  u32 rx_idx_{}, tx_idx_{};
};

static GbeMac g_gbe;

// Packet transport with the same shape as Uart. Framing matters beyond speed:
// the notes record that a data abort desynchronises the byte-stream protocol so
// that "every later read returns 0 and the whole block looks dead". A packet
// protocol resynchronises at the next frame boundary, and the sequence number
// lets the host discard a stale response instead of sliding out of step.
// Wire telemetry for the server. Once the shell hands over, UART is gone and the
// only way to see why a request was rejected is to send the counts back out on
// the heartbeat. Each counts how far a received frame got through recv().
static u32 g_dbg_deliv;    // rx_peek() returned a frame
static u32 g_dbg_body;     // cleared the ethertype / UDP demux
static u32 g_dbg_magic;    // MAGIC matched
static u32 g_dbg_len;      // MAGIC matched but the length checks rejected it
struct Gbe {
  enum class Timeout : u32 { Infinite = 0xffffffff };

  static constexpr u16 ETHERTYPE = 0x88B5;  // local experimental
  static constexpr u32 MAGIC = 0x50533547;  // 'PS5G'
  static constexpr u16 FLAG_FINAL = 1;
  // "the next N bytes are all 0xFF". One page in three is erased on this part, so
  // sending 18432 bytes of 0xFF is pure waste; the marker carries the expanded
  // length as its only payload and the host reconstitutes it.
  static constexpr u16 FLAG_ERASED = 2;
  static constexpr u32 MTU = 1400;
  static constexpr u32 ETH_HDR = 14;
  static constexpr u32 UDP_PORT = 20533;  // ingress when frames arrive as UDP

  // Header layout, byte offsets from the start of our header:
  //   0 magic u32 | 4 seq u16 | 6 flags u16 | 8 len u16 | 10 rsvd u16
  static constexpr u32 HDR_LEN = 12;
  static constexpr u32 HDR = ETH_HDR + HDR_LEN;

  // Everything after the 14-byte Ethernet header sits at a 2-mod-4 address, so
  // a struct overlay there produces unaligned 32-bit accesses. The MPU leaves
  // this memory on the background region, which is Strongly-ordered, and
  // unaligned access to Strongly-ordered memory faults regardless of SCTLR.A --
  // which is precisely what was killing the payload. Byte-wise only.
  // vu8, not u8: with a plain u8* the compiler merges adjacent byte stores into
  // a word store, and the merged store lands at frame offset 6 or 14 -- both
  // 2-mod-4. That unaligned store is what faults; bisecting hello() one
  // statement at a time put the fault squarely in build_frame(), whose only
  // novel act is writing the frame. volatile forces real byte accesses.
  static void put32(vu8* p, u32 v) {
    p[0] = (u8)v;
    p[1] = (u8)(v >> 8);
    p[2] = (u8)(v >> 16);
    p[3] = (u8)(v >> 24);
  }
  static void put16(vu8* p, u16 v) {
    p[0] = (u8)v;
    p[1] = (u8)(v >> 8);
  }
  static u32 get32(const vu8* p) {
    return (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
  }
  static u16 get16(const vu8* p) { return (u16)((u32)p[0] | ((u32)p[1] << 8)); }
  // Copy into the frame buffer with 32-bit stores, not byte stores.
  //
  // This is the transport's throughput limit. The destination is uncached DRAM
  // reached across the bus, and `vu8*` forces one bus transaction per byte with
  // no coalescing -- 1400 of them per frame, measured at ~4.4 ms, which is
  // exactly the observed 313 KB/s (229 frames/s of 1400 B). The source is
  // ordinary cached memory, so it is only the writes that matter.
  //
  // The frame starts at buffer offset 0 and the payload at HDR = 26, so `d`
  // arrives 2-mod-4: step to alignment with two byte stores, then write whole
  // words. The word value is assembled from byte loads so the source may be
  // unaligned without relying on unaligned access being permitted.
  static void copy_bytes(vu8* d, const u8* s, u32 n) {
    while (n && ((u32)d & 3)) {
      *d++ = *s++;
      n--;
    }
    while (n >= 4) {
      *(vu32*)d = (u32)s[0] | ((u32)s[1] << 8) | ((u32)s[2] << 16) |
                  ((u32)s[3] << 24);
      d += 4;
      s += 4;
      n -= 4;
    }
    while (n--) {
      *d++ = *s++;
    }
  }
  static void copy_from(u8* d, const vu8* s, u32 n) {
    while (n--) {
      *d++ = *s++;
    }
  }

  void set_mac(const u8* self) {
    for (u32 i = 0; i < 6; i++) {
      self_[i] = self[i];
      peer_[i] = 0xFF;  // until a request teaches us the host's address
    }
  }

  void flush(bool final_) {
    u32 total = build_frame(final_);
    // Queue the fragment; only the last one waits. build_frame() writes into
    // tx_frame(), which indexes by the same tx_idx_ that tx_queue() then advances
    // -- so consecutive fragments land in consecutive ring slots and the engine
    // sends the whole burst on one kick. A 1 MB response is ~750 fragments; at
    // one completion wait per frame that is ~13 ms each, which was the entire
    // throughput limit.
    g_gbe.tx_queue(total);
    if (final_) {
      g_gbe.tx_drain();
    }
    txlen_ = 0;
  }

  // Header-only build: the payload is ALREADY at f + HDR, put there by whoever
  // produced it. Used by the fused NAND stream, where the drain writes NAND words
  // straight into the frame buffer and there is nothing to copy.
  u32 build_frame_inplace(bool final_, u32 len) {
    return build_frame_flags(final_ ? FLAG_FINAL : 0, len);
  }

  u32 build_frame_flags(u16 flags, u32 len) {
    vu8* f = (vu8*)g_gbe.tx_frame();
    for (u32 i = 0; i < 6; i++) {
      f[i] = 0xFF;
      f[6 + i] = self_[i];
    }
    f[12] = (u8)(ETHERTYPE >> 8);
    f[13] = (u8)(ETHERTYPE & 0xFF);
    vu8* h = f + ETH_HDR;
    put32(h + 0, MAGIC);
    put16(h + 4, seq_);
    put16(h + 6, flags);
    put16(h + 8, (u16)len);
    put16(h + 10, frag_);
    frag_++;
    u32 total = HDR + len;
    if (total < 60) {
      for (u32 i = total; i < 60; i++) {
        f[i] = 0;
      }
      total = 60;
    }
    return total;
  }

  // Fused NAND read: FIFO straight into the frame buffer, then onto the wire.
  //
  // The staged path moves every payload word across the bus five times -- FIFO
  // read, DRAM write, source read, txpay_ write, txpay_ read, ring write -- for
  // about 985 cycles per word. Here the drain writes where the MAC will read
  // from, so a word costs one FIFO read plus one ring write, ~257 cycles. There
  // is no DRAM staging buffer and no separate bulk read at all: the reply to this
  // command IS the page data.
  //
  // This is the zero-copy that is actually available. The MAC cannot gather a
  // frame from two descriptors (mode 18 proved that), but nothing stops us from
  // producing the payload in place.
  u32 nand_stream_frames(u32 nfif, u32 row0, u32 npages, u32 page_bytes,
                         u32 ce_mask, u32 dev, u32 col, u32 spin, u32 burst) {
    const u32 CFG = 0x20, DATA = 0x2C;
    const u32 base = nfif;
    const bool cache_read = (spin & 0x80000000u) != 0;
    const bool slc = (spin & 0x40000000u) != 0;
    spin &= 0x3FFFFFFFu;
    const u32 sp = spin ? spin : 200000;
    const u32 spin_short = 4000;
    auto r16 = [&](u32 o) { return *(volatile u16*)(base + o); };
    auto w16 = [&](u32 o, u32 v) { *(volatile u16*)(base + o) = (u16)v; };
    auto bfi = [&](u32 o, u32 lsb, u32 width, u32 val) {
      u16 cur = r16(o);
      u16 mask = (u16)(((1u << width) - 1) << lsb);
      w16(o, (u16)((cur & ~mask) | ((val << lsb) & mask)));
    };
    // Now takes the CE: interleaving means switching chip between operations,
    // which is just the CE field of the dev/channel register.
    auto im_setup = [&](u32 ce, u32 row, u32 cmd1, u32 cmd2, u32 count) {
      w16(0x00, (r16(0x00) & ~3u) | 2u);
      bfi(0x02, 0, 8, dev & 0xFF);
      bfi(0x02, 8, 4, ce & 0xF);
      bfi(0x22, 0, 8, cmd1);
      bfi(0x22, 8, 8, col & 0xFF);
      bfi(0x24, 0, 8, (col >> 8) & 0xFF);
      bfi(0x24, 8, 8, row & 0xFF);
      bfi(0x26, 0, 8, (row >> 8) & 0xFF);
      bfi(0x26, 8, 8, (row >> 16) & 0xFF);
      bfi(0x28, 0, 8, cmd2);
      w16(0x2A, count & 0xFFFF);
      w16(CFG, r16(CFG) | 0x40);
      w16(CFG, r16(CFG) & ~0x40);
    };
    auto wait_clear = [&](u32 mask, u32 bound) {
      for (u32 i = 0; i < bound; i++) {
        if ((r16(CFG) & mask) == 0) {
          return true;
        }
      }
      return false;
    };
    // Arm a read and return -- the chip then spends tR filling its page register
    // while we go and do something else. This is the whole point: tR is ~8 ms of
    // the ~10 ms a page costs, against ~1.9 ms to actually move its data.
    // Pseudo-SLC blocks need a vendor prefix command before the read (0xDA on
    // this Samsung part, 0xA2 on Toshiba/Hynix/SanDisk). It matters for coverage,
    // not just speed: a written native-TLC page reads entirely 0xFF under 0xDA,
    // and an SLC page reads entirely 0xFF without it -- so a single-mode dump
    // silently records the other mode's blocks as erased. Only agreement in both
    // modes means genuinely erased.
    auto issue_read = [&](u32 ce, u32 row) {
      if (slc) {
        im_setup(ce, row, 0xDA, 0, 0);
        w16(CFG, 0x1A6);
        wait_clear(0x80, spin_short);
      }
      im_setup(ce, row, 0x00, 0x30, 0);
      w16(CFG, 0x1A6);
      return wait_clear(0x80, spin_short);
    };
    auto wait_ready = [&](u32 ce, u32 row) {
      for (u32 t = 0; t < 400; t++) {
        im_setup(ce, row, 0x70, 0, 4);
        w16(CFG, 0x180);
        if (!wait_clear(0x80, spin_short)) {
          continue;
        }
        for (u32 i = 0; i < spin_short; i++) {
          if (r16(CFG) & 0x4000) {
            if (*(volatile u32*)(base + DATA) & 0x20) {
              return true;
            }
            break;
          }
        }
      }
      return false;
    };

    u32 ces[12];
    u32 nce = 0;
    for (u32 i = 0; i < 12 && nce < 12; i++) {
      if (ce_mask & (1u << i)) {
        ces[nce++] = i;
      }
    }
    if (!nce) {
      return 0;
    }

    // Stream frames out AS THEY FILL, overlapping the transmit with the rest of
    // the page still coming out of the FIFO.
    //
    // Erased-page skipping was built here and removed. It works -- one page in
    // three is all-0xFF and travelled as a 4-byte marker -- but it requires
    // holding a whole page across ring slots before deciding, which serialises
    // the transmit behind the read and measured 105.9 KB/s against 1765.8 KB/s
    // streaming. The trade is wrong on this system: the flash side binds at
    // ~1765 KB/s while the transport can do ~9.6 MB/s, so removing wire bytes
    // cannot make a dump faster. It would only pay if the link were the limit.
    // FLAG_ERASED and the host-side expansion are left in place for that case.
    u32 fill = 0;
    u8* dst = (u8*)(g_gbe.tx_frame() + HDR);
    u32 pages = 0;

    for (u32 k = 0; k < nce; k++) {
      issue_read(ces[k], row0);
    }

    // ONFI sequential cache read. `31h` moves the page register to the data
    // register AND starts the next page's array read at the same time, so tR
    // overlaps the data-out of the page before it. This is the pipelining that
    // channel interleaving could not provide -- it works within one die, which is
    // the only place there is anything to overlap. `3Fh` ends the sequence.
    // Read-only opcodes; nothing here can alter stored data.
    if (cache_read && nce == 1) {
      u32 ce = ces[0];
      if (!wait_ready(ce, row0)) {
        goto done;
      }
      for (u32 pg = 0; pg < npages; pg++) {
        bool last = (pg + 1 == npages);
        im_setup(ce, row0 + pg, last ? 0x3F : 0x31, 0, 0);
        w16(CFG, 0x1A6);
        if (!wait_clear(0x80, spin_short)) {
          goto done;
        }
        if (!wait_ready(ce, row0 + pg)) {
          goto done;
        }
        im_setup(ce, row0 + pg, 0, 0, page_bytes);
        w16(CFG, 0x180);
        u32 words = page_bytes / 4;
        for (u32 i = 0; i < words; i++) {
          u32 t = 0;
          while (!(r16(CFG) & 0x4000)) {
            if (++t > sp) {
              goto done;
            }
          }
          *(vu32*)(dst + fill) = *(volatile u32*)(base + DATA);
          fill += 4;
          if (fill == MTU) {
            u32 total = build_frame_flags(0, MTU);
            g_gbe.tx_queue(total);
            fill = 0;
            dst = (u8*)(g_gbe.tx_frame() + HDR);
          }
        }
        pages++;
      }
      goto done;
    }

    for (u32 pg = 0; pg < npages; pg++) {
      for (u32 k = 0; k < nce; k++) {
        u32 ce = ces[k];
        u32 row = row0 + pg;
        if (!wait_ready(ce, row)) {
          goto done;
        }
        im_setup(ce, row, 0, 0, page_bytes);
        w16(CFG, 0x180);
        // One FIFO-ready check per BURST words, not per word. The poll and the
        // pop are one NFIF access each at ~261 cycles, so they are half the
        // per-word cost and 3.88 ms of a 10.2 ms page; dropping most of the polls
        // removes most of that. burst = 1 reproduces the old behaviour exactly,
        // which is what it is compared against.
        u32 words = page_bytes / 4;
        u32 b = burst ? burst : 1;
        for (u32 i = 0; i < words;) {
          u32 t = 0;
          while (!(r16(CFG) & 0x4000)) {
            if (++t > sp) {
              goto done;
            }
          }
          u32 take = (words - i < b) ? (words - i) : b;
          for (u32 j = 0; j < take; j++) {
            *(vu32*)(dst + fill) = *(volatile u32*)(base + DATA);
            fill += 4;
            if (fill == MTU) {
              u32 total = build_frame_flags(0, MTU);
              g_gbe.tx_queue(total);
              fill = 0;
              dst = (u8*)(g_gbe.tx_frame() + HDR);
            }
          }
          i += take;
        }
        pages++;
        if (pg + 1 < npages) {
          issue_read(ce, row0 + pg + 1);
        }
      }
    }
  done:
    u32 total = build_frame_flags(FLAG_FINAL, fill);
    g_gbe.tx_queue(total);
    g_gbe.tx_drain();
    tx_started_ = false;
    return pages;
  }

  // Returns the on-wire length. Split out of flush() so the frame build can be
  // exercised without touching the DMA engine.
  u32 build_frame(bool final_) {
    vu8* f = (vu8*)g_gbe.tx_frame();
    // Reply to broadcast, not to peer_. Requests arrive as UDP broadcasts from
    // the host's Hyper-V vSwitch, so peer_ is the vSwitch's MAC, and the reply
    // is a raw 0x88B5 frame -- a non-IP ethertype unicast into a virtual switch,
    // which is under no obligation to forward it to the physical adapter Npcap
    // captures on. Broadcast is the one delivery the whole path is known to
    // honour: it is how requests reach the board in the first place.
    for (u32 i = 0; i < 6; i++) {
      f[i] = 0xFF;
      f[6 + i] = self_[i];
    }
    f[12] = (u8)(ETHERTYPE >> 8);
    f[13] = (u8)(ETHERTYPE & 0xFF);
    vu8* h = f + ETH_HDR;
    put32(h + 0, MAGIC);
    put16(h + 4, seq_);
    put16(h + 6, final_ ? FLAG_FINAL : 0);
    put16(h + 8, (u16)txlen_);
    // Fragment index, in what used to be the reserved halfword. Without it a
    // response is an unlabelled byte stream: a duplicated fragment corrupts
    // reassembly, so a lost reply could only be recovered by re-running the whole
    // request and hoping. With it the host can ignore duplicates and fill gaps,
    // which makes re-issuing an idempotent read a safe merge instead of a gamble.
    put16(h + 10, frag_);
    frag_++;
    copy_bytes(f + HDR, txpay_, txlen_);
    u32 total = HDR + txlen_;
    if (total < 60) {
      for (u32 i = total; i < 60; i++) {
        f[i] = 0;
      }
      total = 60;
    }
    return total;
  }

  // Broadcast a one-frame beacon. Once the shell is on GbE there is no UART to
  // report through, so this is the only way to see that the loop is alive.
  // Build the beacon frame in the TX buffer but do NOT hand it to the engine,
  // so the frame construction can be tested apart from the transmit.
  void stage_payload() {
    static const char msg[] = "PS5G-READY";
    txlen_ = 0;
    for (u32 i = 0; i < sizeof(msg); i++) {
      txpay_[i] = (u8)msg[i];
    }
    txlen_ = sizeof(msg);
  }

  void hello() {
    stage_payload();
    build_frame(true);
    tx_started_ = false;
    g_gbe.tx(60);
  }

  bool recv() {
    u32 spins = 0;
    while (1) {
      u32 n = 0;
      u8* p = g_gbe.rx_peek(&n);
      if (!p) {
        if (g_gbe.rx_ready()) {
          g_gbe.rx_release();  // error frame; drop and move on
        }
        // Idle watchdog. There is deliberately no periodic kick elsewhere:
        // rx_peek() already kicks on 0x20, and kicking mid-receive restarts the
        // engine at the ring base and aborts the frame in progress. But TX and
        // RX share one DMA engine and it stays parked on the TX ring after a
        // transmit, so a server that sends its hello first then receives
        // nothing. Kicking only after a long idle cannot abort a receive,
        // because there is none in progress.
        //
        // The frame carries recv() telemetry (delivered / demuxed / magic /
        // len_rejected), which is the only visibility left once the shell has
        // taken over the UART. Keep it rare: at 200k spins it starves reception,
        // at 4M the transport sustains ~114 KB/s.
        if (++spins >= 4000000) {
          spins = 0;
          u32 dbg[4] = {g_dbg_deliv, g_dbg_body, g_dbg_magic, g_dbg_len};
          txlen_ = 0;
          write((const u8*)dbg, sizeof(dbg));
          flush(true);
        }
        continue;
      }
      // Two ingress shapes. The raw 0x88B5 frame is preferred, but Npcap
      // injection does not physically transmit on this host's adapter, and the
      // MAC drops unicast addressed to its own station address -- so the only
      // path that reaches the board is a UDP broadcast from the Windows IP
      // stack. Accept that too and read our header out of the UDP payload.
      g_dbg_deliv++;
      const u8* body = nullptr;
      u32 bodylen = 0;
      u32 et = ((u32)p[12] << 8) | p[13];
      if (et == ETHERTYPE && n >= HDR) {
        body = p + ETH_HDR;
        bodylen = n - ETH_HDR;
      } else if (et == 0x0800 && n > ETH_HDR + 28 && p[ETH_HDR + 9] == 17) {
        u32 ihl = (u32)(p[ETH_HDR] & 0xF) * 4;
        u32 uoff = ETH_HDR + ihl;
        if (uoff + 8 < n) {
          u32 dport = ((u32)p[uoff + 2] << 8) | p[uoff + 3];
          if (dport == UDP_PORT) {
            body = p + uoff + 8;
            bodylen = n - (uoff + 8);
          }
        }
      }
      if (body && bodylen >= HDR_LEN) {
        g_dbg_body++;
        const vu8* h = (const vu8*)body;
        u32 hlen = get16(h + 8);
        if (get32(h + 0) == MAGIC) {
          g_dbg_magic++;
          if (!(hlen <= MTU && HDR_LEN + hlen <= bodylen)) {
            g_dbg_len++;
          }
        }
        if (get32(h + 0) == MAGIC && hlen <= MTU && HDR_LEN + hlen <= bodylen) {
          for (u32 i = 0; i < 6; i++) {
            peer_[i] = p[6 + i];  // reply to whoever asked
          }
          seq_ = get16(h + 4);
          frag_ = 0;   // new request: restart fragment numbering
          copy_from(rxpay_, (const vu8*)(body + HDR_LEN), hlen);
          rxlen_ = hlen;
          rxpos_ = 0;
          g_gbe.rx_release();
          return true;
        }
      }
      g_gbe.rx_release();
    }
  }

  bool read(u8* data, u32 len, Timeout = Timeout::Infinite) {
    while (len) {
      if (rxpos_ == rxlen_) {
        // Request fully consumed: the response for it is complete, so close the
        // frame before blocking for the next one.
        if (tx_started_) {
          flush(true);
          tx_started_ = false;
        }
        if (!recv()) {
          return false;
        }
      }
      u32 n = rxlen_ - rxpos_;
      if (n > len) {
        n = len;
      }
      memcpy(data, rxpay_ + rxpos_, n);
      rxpos_ += n;
      data += n;
      len -= n;
    }
    return true;
  }
  template <typename T>
  bool read(T* data) {
    return read((u8*)data, sizeof(T));
  }

  // Payload is staged in txpay_ and copied into the frame buffer once, by
  // build_frame(). Writing it straight into the frame buffer here was tried and
  // is SLOWER (721 vs ~800 KB/s): write() is called once per 4-byte word by
  // write_from_mem, so the direct form pays copy_bytes' alignment preamble 350
  // times per frame where staging pays it once for the whole 1400 bytes. The
  // extra copy is into cached memory and costs less than that overhead.
  void write(const u8* buf, u32 len) {
    tx_started_ = true;
    while (len) {
      u32 space = MTU - txlen_;
      u32 n = len < space ? len : space;
      memcpy(txpay_ + txlen_, buf, n);
      txlen_ += n;
      buf += n;
      len -= n;
      if (txlen_ == MTU) {
        flush(false);  // more fragments follow
      }
    }
  }
  void write(const char* buf) { write((const u8*)buf, strlen(buf)); }
  template <typename T>
  void write(const T& data) {
    write((const u8*)&data, sizeof(T));
  }

  u8 self_[6]{};
  u8 peer_[6]{};
  u16 seq_{};
  u16 frag_{};
  u32 erased_pages_{};
  u32 rxlen_{}, rxpos_{};
  u32 txlen_{};
  bool tx_started_{};
  u8 rxpay_[MTU]{};
  u8 txpay_[MTU]{};
};

// Used only by the diagnostic path in gbe_start(), which must not touch the
// server's own transport instance.
static Gbe g_gbe_diag;

[[noreturn]] void gbe_server_run(const u8* mac);
#endif  // !ROM_ENV

// A NAND read job, in memory both cores can see. cpu0 fills the request half,
// whichever core runs the job fills the result half. Laid out flat and written
// by the host in one mem_access block so setting up a batch costs one round trip
// rather than fifteen.
struct NandJob {
  u32 nfif, dst, row, npages, page_bytes, ce, dev, col, spin;   // request
  u32 pages_done, bytes, status_last, err, status_tries, fifo_spins, done;
};

// The whole per-page sequence: ImSetup, arm, status poll, ImSetup, drain. Free
// function with the core_n_server op-6 signature so it can run on cpu0 directly
// (cmd 18) or be dispatched to another core (cmd 19), from the same code.
// The MPU is per-core, and mpu_setup_cpu0() only programs cpu0. A job dispatched
// to cpu1 therefore ran with cpu1's own (unconfigured) map, the NFIF window lost
// its Device typing, the halfword arm write got widened, and the arm never
// cleared -- err=1 on the very first page. Same table as the host's
// rom_mpu_entries, as (base, DRACR, DRSR); tuple order matters, c6,c1,2 is DRSR
// and c6,c1,4 is DRACR on Cortex-R5.
static void mpu_setup_this_core() {
  static const u32 ent[][3] = {
      {0x00000000, 0x1305, 0x3F}, {0xF0000000, 0x0304, 0x37},
      {0xE0000000, 0x0302, 0x37}, {0x00000000, 0x030C, 0x3D},
      {0x10000000, 0x0304, 0x35}, {0x18000000, 0x0304, 0x2F},
      {0x19000000, 0x0304, 0x31}, {0x80000000, 0x0304, 0x3B},
      {0xC0000000, 0x0304, 0x39}, {0x00000000, 0x0308, 0x2F},
      {0xFFFE0000, 0x0302, 0x21},
  };
  u32 sctlr = arm_rsr(SCTLR);
  arm_wsr(SCTLR, sctlr & ~1u);            // MPU off while reprogramming
  asm volatile("dsb" ::: "memory");
  asm volatile("isb" ::: "memory");
  for (u32 i = 0; i < sizeof(ent) / sizeof(ent[0]); i++) {
    asm volatile("mcr p15, 0, %0, c6, c2, 0" ::"r"(i) : "memory");
    asm volatile("mcr p15, 0, %0, c6, c1, 0" ::"r"(ent[i][0]) : "memory");
    asm volatile("mcr p15, 0, %0, c6, c1, 4" ::"r"(ent[i][1]) : "memory");
    asm volatile("mcr p15, 0, %0, c6, c1, 2" ::"r"(ent[i][2]) : "memory");
  }
  asm volatile("dsb" ::: "memory");
  asm volatile("isb" ::: "memory");
  arm_wsr(SCTLR, arm_rsr(SCTLR) | 1u);    // MPU on
  asm volatile("dsb" ::: "memory");
  asm volatile("isb" ::: "memory");
}

u32 nand_read_job(u32 job_ptr, u32, u32, u32) {
  mpu_setup_this_core();
  auto j = (volatile NandJob*)job_ptr;
  const u32 base = j->nfif;
  const u32 CFG = 0x20, DATA = 0x2C;
  const u32 spin = j->spin ? j->spin : 200000;
  const u32 spin_short = 4000;
  auto r16 = [&](u32 o) { return *(volatile u16*)(base + o); };
  auto w16 = [&](u32 o, u32 v) { *(volatile u16*)(base + o) = (u16)v; };
  auto bfi = [&](u32 o, u32 lsb, u32 width, u32 val) {
    u16 cur = r16(o);
    u16 mask = (u16)(((1u << width) - 1) << lsb);
    w16(o, (u16)((cur & ~mask) | ((val << lsb) & mask)));
  };
  auto im_setup = [&](u32 row, u32 cmd1, u32 cmd2, u32 count) {
    w16(0x00, (r16(0x00) & ~3u) | 2u);
    bfi(0x02, 0, 8, j->dev & 0xFF);
    bfi(0x02, 8, 4, j->ce & 0xF);
    bfi(0x22, 0, 8, cmd1);
    bfi(0x22, 8, 8, j->col & 0xFF);
    bfi(0x24, 0, 8, (j->col >> 8) & 0xFF);
    bfi(0x24, 8, 8, row & 0xFF);
    bfi(0x26, 0, 8, (row >> 8) & 0xFF);
    bfi(0x26, 8, 8, (row >> 16) & 0xFF);
    bfi(0x28, 0, 8, cmd2);
    w16(0x2A, count & 0xFFFF);
    w16(CFG, r16(CFG) | 0x40);
    w16(CFG, r16(CFG) & ~0x40);
  };
  auto wait_clear = [&](u32 mask, u32 bound) {
    for (u32 i = 0; i < bound; i++) {
      if ((r16(CFG) & mask) == 0) {
        return true;
      }
    }
    return false;
  };

  j->pages_done = 0; j->bytes = 0; j->err = 0;
  j->status_tries = 0; j->fifo_spins = 0;
  u32 dst = j->dst;
  for (u32 pg = 0; pg < j->npages; pg++) {
    u32 row = j->row + pg;
    im_setup(row, 0x00, 0x30, 0);
    w16(CFG, 0x1A6);
    if (!wait_clear(0x80, spin_short)) { j->err = 1; break; }
    bool ready = false;
    for (u32 t = 0; t < 400 && !ready; t++) {
      j->status_tries = j->status_tries + 1;
      im_setup(row, 0x70, 0, 4);
      w16(CFG, 0x180);
      if (!wait_clear(0x80, spin_short)) { continue; }
      for (u32 i = 0; i < spin_short; i++) {
        if (r16(CFG) & 0x4000) {
          u32 st = *(volatile u32*)(base + DATA);
          j->status_last = st;
          ready = (st & 0x20) != 0;
          break;
        }
      }
    }
    if (!ready) { j->err = 2; break; }
    im_setup(row, 0, 0, j->page_bytes);
    w16(CFG, 0x180);
    u32 words = j->page_bytes / 4;
    bool ok = true;
    for (u32 i = 0; i < words; i++) {
      u32 t = 0;
      while (!(r16(CFG) & 0x4000)) {
        if (++t > spin) { ok = false; break; }
      }
      if (!ok) { break; }
      *(vu32*)dst = *(volatile u32*)(base + DATA);
      dst += 4;
      j->fifo_spins = j->fifo_spins + t;
    }
    if (!ok) { j->err = 3; break; }
    j->pages_done = j->pages_done + 1;
    j->bytes = j->bytes + j->page_bytes;
  }
  asm volatile("dsb" ::: "memory");
  j->done = 1;          // publish LAST: the poller keys off this
  asm volatile("dsb" ::: "memory");
  return j->err;
}

template <typename Io>
struct Server {
  bool ping() {
    u32 val;
    if (!uart_.read(&val)) {
      return false;
    }
    uart_.write(val + 1);
    return true;
  }
  template <typename T>
  bool read_into_mem(u32 addr, u32 count) {
    while (count--) {
      T val;
      if (!uart_.read(&val)) {
        return false;
      }
      *(T*)addr = val;
      addr += sizeof(T);
    }
    return true;
  }
  template <typename T>
  void write_from_mem(u32 addr, u32 count) {
    while (count--) {
      T val = *(T*)addr;
      addr += sizeof(T);
      uart_.write(val);
    }
  }
  bool mem_access() {
    struct {
      u32 addr;
      u32 count;
      u32 opt;
      u8 stride() const { return opt & 0x7fffffff; }
      bool is_write() const { return (opt >> 31) & 1; }
    } req{};
    if (!uart_.read(&req)) {
      return false;
    }
    bool ok = true;
    if (req.is_write()) {
      switch (req.stride()) {
      case 1:
        ok = read_into_mem<u8>(req.addr, req.count);
        break;
      case 2:
        ok = read_into_mem<u16>(req.addr, req.count);
        break;
      case 4:
        ok = read_into_mem<u32>(req.addr, req.count);
        break;
      }
    } else {
      switch (req.stride()) {
      case 1:
        write_from_mem<u8>(req.addr, req.count);
        break;
      case 2:
        write_from_mem<u16>(req.addr, req.count);
        break;
      case 4:
        write_from_mem<u32>(req.addr, req.count);
        break;
      }
    }
    return ok;
  }
  enum CpReg : u8 {
    kDBGDRAR,
    kDBGDSAR,
    kDBGPRCR,
    kMIDR,
    kSCTLR,
    kACTLR,
    kVBAR,
    kCPSR,
    kBTCMREGION,
    kATCMREGION,
    kTCMSELECTION,
    kMPIDR,
  };
  bool reg_read() {
    u8 reg{};
    if (!uart_.read(&reg)) {
      return false;
    }
    u32 val{};
    switch (reg) {
#define SR_READ(name)    \
  case k##name:          \
    val = arm_rsr(name); \
    break;
      SR_READ(DBGDRAR);
      SR_READ(DBGDSAR);
      SR_READ(DBGPRCR);
      SR_READ(MIDR);
      SR_READ(SCTLR);
      SR_READ(ACTLR);
      SR_READ(VBAR);
      SR_READ(BTCMREGION);
      SR_READ(ATCMREGION);
      SR_READ(TCMSELECTION);
      SR_READ(MPIDR);
    case kCPSR:
      val = arm_cpsr_read();
      break;
    default:
      val = 0xcacacaca;
      break;
#undef SR_READ
    }
    uart_.write(val);
    return true;
  }
  bool reg_write() {
    u8 reg{};
    if (!uart_.read(&reg)) {
      return false;
    }
    // NOTE: At least EAP requires alignment (if reading as packed struct, the
    // load of |val| into a register to perform the MCR would die)
    u32 val{};
    if (!uart_.read(&val)) {
      return false;
    }
    switch (reg) {
#define SR_WRITE(name)  \
  case k##name:         \
    arm_wsr(name, val); \
    break;
      SR_WRITE(DBGPRCR);
      SR_WRITE(SCTLR);
      SR_WRITE(ACTLR);
      SR_WRITE(VBAR);
      SR_WRITE(BTCMREGION);
      SR_WRITE(ATCMREGION);
      SR_WRITE(TCMSELECTION);
    default:
      return false;
#undef SR_WRITE
    }
    return true;
  }
  bool int_disable() {
    asm volatile("cpsid aif" : : : "memory");
    return true;
  }
  bool int_enable() {
    asm volatile("cpsie aif" : : : "memory");
    return true;
  }
  bool dabort_status() {
    uart_.write(g_abort_status);
    g_abort_status = {};
    return true;
  }
  bool cp15_access() {
    // Arbitrary coprocessor read/write via runtime-generated MRC/MCR.
    // Protocol: read u32 with fields:
    //   [2:0]   op1
    //   [6:3]   CRn
    //   [10:7]  CRm
    //   [13:11] op2
    //   [14]    is_write
    //   [18:15] cpnum (0 = p15 for backwards compat)
    // If write: read another u32 (the value to write).
    // Returns u32: result value, or 0xDEADDEAD if UNDEF.
    u32 params{};
    if (!uart_.read(&params))
      return false;

    u8 op1 = (params >> 0) & 0x7;
    u8 crn = (params >> 3) & 0xF;
    u8 crm = (params >> 7) & 0xF;
    u8 op2 = (params >> 11) & 0x7;
    bool is_write = (params >> 14) & 1;
    u8 cpnum = (params >> 15) & 0xF;
    if (cpnum == 0)
      cpnum = 15;

    u32 write_val{};
    if (is_write) {
      if (!uart_.read(&write_val))
        return false;
    }

    u32 instr;
    if (is_write) {
      instr = 0xEE000010u | ((u32)op1 << 21) | ((u32)crn << 16) |
              ((u32)cpnum << 8) | ((u32)op2 << 5) | crm;
    } else {
      instr = 0xEE100010u | ((u32)op1 << 21) | ((u32)crn << 16) |
              ((u32)cpnum << 8) | ((u32)op2 << 5) | crm;
    }

    cp15_trampoline[0] = instr;
    cp15_trampoline[1] = 0xE12FFF1Eu;  // BX LR
    arm_dsb();
    arm_isb();

    g_undef_caught = 0;

    u32 result;
    if (is_write) {
      auto func = (void (*)(u32))(void*)cp15_trampoline;
      func(write_val);
      result = g_undef_caught ? 0xDEADDEADu : 0;
    } else {
      auto func = (u32 (*)())(void*)cp15_trampoline;
      result = func();
      if (g_undef_caught)
        result = 0xDEADDEADu;
    }

    uart_.write(result);
    return true;
  }
  bool core_n_cmd() {
    struct {
      u32 cpu;
      u32 func;
      u32 args[3];
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
#if defined(CPU_EFC) && !defined(ROM_ENV)
    auto req = (vu32*)(0x8800000 + 0x02000000 * cmd.cpu);
    auto resp = (vu32*)(0x800010 + 0x10 * (cmd.cpu - 1));
    resp[0] = 0;
    req[1] = cmd.args[0];
    req[2] = cmd.args[1];
    req[3] = cmd.args[2];
    req[0] = cmd.func;
    while (!resp[0]) {
    }
    for (size_t i = 0; i < 4; i++) {
      uart_.write(resp[i]);
    }
#else
    for (size_t i = 0; i < 4; i++) {
      uart_.write(0xdeadbeef);
    }
#endif

    return true;
  }
  bool call() {
    struct {
      u32 func;
      u32 args[7];
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    using func_t = u32(u32, u32, u32, u32, u32, u32, u32);
    g_paborted = 0;
    u32 rv =
        ((func_t*)cmd.func)(cmd.args[0], cmd.args[1], cmd.args[2], cmd.args[3],
                            cmd.args[4], cmd.args[5], cmd.args[6]);
    uart_.write(g_paborted);
    g_paborted = 0;
    uart_.write(rv);
    return true;
  }
  static inline u32 read_ccnt() {
    u32 val;
    asm volatile("mrc p15, 0, %0, c9, c13, 0" : "=r"(val));
    return val;
  }
  static inline void enable_ccnt() {
    u32 pmcr = 0x5;
    asm volatile("mcr p15, 0, %0, c9, c12, 0" ::"r"(pmcr));
    u32 en = (1u << 31);
    asm volatile("mcr p15, 0, %0, c9, c12, 1" ::"r"(en));
  }

  bool mem_fill() {
    struct {
      u32 dst;
      u32 size;
      u32 count;
    } cmd{};
    if (!uart_.read(&cmd))
      return false;

    enable_caches(true);

    // fill sram with pattern
    for (u32 addr = 0x01000000; addr < 0x011e0000; addr += 4) {
      *(vu32*)addr = 0xca000000 | (addr >> 2);
    }
    // fill most of dram besides ourselves
    for (u32 addr = 0x40000000; addr < 0x58000000; addr += 4) {
      *(vu32*)addr = 0xda000000 | (addr >> 2);
    }
    for (u32 addr = 0x58800000 + 0x1100; addr < 0x60000000; addr += 4) {
      *(vu32*)addr = 0xda000000 | (addr >> 2);
    }

    if (cmd.size && cmd.count) {
      if (!read_into_mem<u8>(cmd.dst, cmd.size)) {
        return false;
      }
      u32 dst = cmd.dst + cmd.size;
      for (size_t i = 0; i < cmd.count - 1; i++) {
        memcpy((void*)dst, (void*)cmd.dst, cmd.size);
        dst += cmd.size;
      }
    }

    flush_l1_data_cache();

    u32 ack = 1;
    uart_.write(ack);
    return true;
  }

  bool irq_test() {
    g_irq_fired = 0;
    auto vic = Vic();
    vic.init();
    auto wdt = Wdt();
    wdt.int_enable(false);
    int_enable();
    enable_caches(true);

    struct {
      u64 ticks[4];
    } resp{};

    for (size_t i = 0; i < 4; i++) {
      auto& ticks = resp.ticks[i];
      wdt.alarm_start(true, 1000);
      auto start = Hpet().regs.main_counter;
      arm_wfi();
      ticks = Hpet().regs.main_counter - start;
    }
    int_disable();

    uart_.write(resp);
    return true;
  }
  bool crc16() {
    struct {
      u32 addr;
      u32 size;
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    auto addr = (u8*)cmd.addr;
    u32 size = cmd.size;
#if !defined(ROM_ENV)
    if (size == UINT32_MAX) {
      extern char __start_text[];
      extern char __stop_data[];
      size_t const_size = __stop_data - __start_text;
      addr = (u8*)__start_text;
      size = const_size;
    }
#endif
    u32 crc = crc16_ccitt(addr, size);
    uart_.write(crc);
    return true;
  }
  // Drain a hardware FIFO into memory without a round trip per word.
  //
  // The NAND page read pops NFIF+0x2C a dword at a time, gated on FIFO_READY.
  // From the host that is ~9200 round trips per page and caps the transfer at
  // ~2.4 KB/s. On-target it costs one round trip plus a bulk read of |dst|.
  //
  // Accesses are strictly 32-bit: with the MPU on the NAND window is
  // Strongly-ordered and anything narrower alignment-faults, which also
  // desynchronises this protocol.
  //
  // |fifo_addr| is read exactly |count| times and never speculatively, since a
  // read pops the FIFO. |ready_mask| == 0 skips gating.
  bool mmio_drain() {
    struct {
      u32 fifo_addr;   // register to pop (e.g. NFIF+0x2C)
      u32 stat_addr;   // status register to poll (e.g. NFIF+0x20)
      u32 ready_mask;  // bit meaning "data available" (e.g. 1 << 14)
      u32 count;       // number of 32-bit words to move
      u32 dst;         // destination buffer
      u32 spin_limit;  // per-word poll iterations before giving up
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    struct {
      u32 done;    // words actually moved
      u32 status;  // 0 = ok, 1 = timed out waiting for ready
    } resp{};
    auto fifo = (volatile u32*)cmd.fifo_addr;
    auto stat = (volatile u32*)cmd.stat_addr;
    auto dst = (volatile u32*)cmd.dst;
    for (u32 i = 0; i < cmd.count; i++) {
      if (cmd.ready_mask) {
        u32 spins = 0;
        while (!(*stat & cmd.ready_mask)) {
          if (++spins > cmd.spin_limit) {
            resp.done = i;
            resp.status = 1;
            uart_.write(resp);
            return true;
          }
        }
      }
      dst[i] = *fifo;
    }
    resp.done = cmd.count;
    uart_.write(resp);
    return true;
  }

  // cmd 18: read whole NAND pages on-device, setup included.
  //
  // mmio_drain moves the FIFO pop loop on-target, but a page read is ~30 more
  // register accesses of setup, and as host round trips those dominate a dump
  // over GbE. This runs the whole per-page sequence here and loops over pages,
  // so the host pays one command plus one bulk read per N pages.
  //
  // The sequence is the firmware's HalNfCtrl_ReadPageIm, also transcribed in
  // titania/nd_fwpage.py:
  //
  //   ImSetup(pna, 0x00, 0x30, 0) ; CFG = 0x1A6 ; wait CFG&0x80 == 0
  //   WaitStatus(pna, 0x20)       via ImSetup(pna,0x70,0,4) ; CFG = 0x180
  //   ImSetup(pna, 0, 0, N)       ; CFG = 0x180 ; pop N bytes
  //
  // Not the DMA/sequencer path: the sequencer does not fetch descriptors in
  // firmware context, and where it does DES_PROC pins at 1 and nothing reaches
  // the destination.
  //
  // Read-only. The only opcodes issued are 0x00/0x30 (READ) and 0x70 (READ
  // STATUS); no program (0x80/0x10) or erase (0x60/0xD0).
  bool nand_read_pages() {
    struct {
      u32 nfif;        // NFIF base, 0x1700E000
      u32 dst;         // destination buffer (DRAM)
      u32 row;         // first page (row address)
      u32 npages;      // pages to read
      u32 page_bytes;  // bytes per page incl. spare, e.g. 18432
      u32 ce;
      u32 dev;
      u32 col;
      u32 spin;        // per-poll bound; 0 = default
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    struct {
      u32 pages_done;
      u32 bytes;
      u32 status_last;
      u32 err;         // 0 ok, 1 arm timeout, 2 status timeout, 3 FIFO timeout
      u32 status_tries;  // total status attempts across all pages
      u32 fifo_spins;    // total FIFO poll iterations across all pages
      u32 cycles;        // CCNT for the whole batch
      u32 pad;
    } resp{};
    enable_ccnt();
    u32 t_start = read_ccnt();

    const u32 base = cmd.nfif;
    const u32 CFG = 0x20, DATA = 0x2C;
    const u32 spin = cmd.spin ? cmd.spin : 200000;
    auto r16 = [&](u32 o) { return *(volatile u16*)(base + o); };
    auto w16 = [&](u32 o, u32 v) { *(volatile u16*)(base + o) = (u16)v; };
    auto bfi = [&](u32 o, u32 lsb, u32 width, u32 val) {
      u16 cur = r16(o);
      u16 mask = (u16)(((1u << width) - 1) << lsb);
      w16(o, (u16)((cur & ~mask) | ((val << lsb) & mask)));
    };
    // ImSetup. Stays in mode 2 throughout: mode 1 wedges the interface from this
    // context and the shell stops answering.
    auto im_setup = [&](u32 row, u32 cmd1, u32 cmd2, u32 count) {
      w16(0x00, (r16(0x00) & ~3u) | 2u);
      bfi(0x02, 0, 8, cmd.dev & 0xFF);
      bfi(0x02, 8, 4, cmd.ce & 0xF);
      bfi(0x22, 0, 8, cmd1);
      bfi(0x22, 8, 8, cmd.col & 0xFF);
      bfi(0x24, 0, 8, (cmd.col >> 8) & 0xFF);
      bfi(0x24, 8, 8, row & 0xFF);
      bfi(0x26, 0, 8, (row >> 8) & 0xFF);
      bfi(0x26, 8, 8, (row >> 16) & 0xFF);
      bfi(0x28, 0, 8, cmd2);
      w16(0x2A, count & 0xFFFF);
      w16(CFG, r16(CFG) | 0x40);    // FIFO reset pulse
      w16(CFG, r16(CFG) & ~0x40);
    };
    // Short waits get a SHORT bound. The arm-clear and the status FIFO settle in
    // microseconds; giving them the same 200k bound as the data FIFO meant one
    // stuck poll cost ~240 ms (200k MMIO reads at ~1.2 us), and a single such
    // timeout per page is exactly the 124 ms/page worst case and the ~4x
    // batch-to-batch variance that was showing up. Retries are cheap -- the
    // expensive thing was the bound, not the retrying.
    const u32 spin_short = 4000;
    auto wait_clear = [&](u32 mask, u32 bound) {
      for (u32 i = 0; i < bound; i++) {
        if ((r16(CFG) & mask) == 0) {
          return true;
        }
      }
      return false;
    };

    u32 dst = cmd.dst;
    for (u32 pg = 0; pg < cmd.npages; pg++) {
      u32 row = cmd.row + pg;
      im_setup(row, 0x00, 0x30, 0);
      w16(CFG, 0x1A6);
      if (!wait_clear(0x80, spin_short)) {
        resp.err = 1;
        break;
      }
      // Wait for the part to report ready (status bit 0x20).
      // More attempts, each cheap, instead of few attempts each able to burn
      // 240 ms. A busy part just needs time: tR is tens of microseconds, so this
      // normally succeeds on the first or second look.
      bool ready = false;
      for (u32 t = 0; t < 400 && !ready; t++) {
        resp.status_tries++;
        im_setup(row, 0x70, 0, 4);
        w16(CFG, 0x180);
        if (!wait_clear(0x80, spin_short)) {
          continue;
        }
        for (u32 i = 0; i < spin_short; i++) {
          if (r16(CFG) & 0x4000) {
            u32 st = *(volatile u32*)(base + DATA);
            resp.status_last = st;
            ready = (st & 0x20) != 0;
            break;
          }
        }
      }
      if (!ready) {
        resp.err = 2;
        break;
      }
      // Transfer the page.
      im_setup(row, 0, 0, cmd.page_bytes);
      w16(CFG, 0x180);
      u32 words = cmd.page_bytes / 4;
      bool ok = true;
      for (u32 i = 0; i < words; i++) {
        u32 t = 0;
        while (!(r16(CFG) & 0x4000)) {
          if (++t > spin) {
            ok = false;
            break;
          }
        }
        resp.fifo_spins += t;
        if (!ok) {
          break;
        }
        *(vu32*)dst = *(volatile u32*)(base + DATA);
        dst += 4;
      }
      if (!ok) {
        resp.err = 3;
        break;
      }
      resp.pages_done++;
      resp.bytes += cmd.page_bytes;
    }
    resp.cycles = read_ccnt() - t_start;
    uart_.write(resp);
    return true;
  }

  // cmd 21: stream NAND pages straight to the wire. The reply IS the data --
  // there is no destination buffer and no follow-up bulk read.
  bool nand_stream() {
    struct {
      u32 nfif, row, npages, page_bytes, ce, dev, col, spin, burst;
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    u32 pages = uart_.nand_stream_frames(cmd.nfif, cmd.row, cmd.npages,
                                         cmd.page_bytes, cmd.ce, cmd.dev,
                                         cmd.col, cmd.spin, cmd.burst);
    // The page data has already gone out as this request's reply, flagged FINAL,
    // so nothing further is written here -- a trailing status word would land in
    // the host's byte stream as if it were payload.
    (void)pages;
    return true;
  }

  // cmd 19: START a NAND job on another core and return immediately.
  // cmd 20: wait for it and report.
  //
  // This is the ping-pong. The drain and the transfer are comparable in size but
  // the server loop is single-threaded, so today they serialise: drain a batch,
  // send it, drain the next. Dispatching the drain to another R5 core lets the
  // host pull batch N-1 over the wire while batch N is still being read out of
  // the NAND.
  //
  // core_n_server op 6 calls a function on that core from a descriptor
  // {func, arg0..arg3} and sets resp[0] when it returns. core_n_cmd (cmd 8)
  // blocks on that flag, which is what must not happen here, so these two
  // commands split the trigger from the wait.
  //
  // cpu1 and cpu2 run core_n_server and answer; cpu3 does not, and probing it
  // wedges the shell because the mailbox wait never completes. Use cpu 1.
  bool nand_start() {
    struct { u32 cpu; u32 job; u32 desc; } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    auto d = (vu32*)cmd.desc;
    d[0] = (u32)&nand_read_job;
    d[1] = cmd.job;
    d[2] = 0; d[3] = 0; d[4] = 0;
    ((volatile NandJob*)cmd.job)->done = 0;
    asm volatile("dsb" ::: "memory");
    auto req = (vu32*)(0x8800000 + 0x02000000 * cmd.cpu);
    auto resp = (vu32*)(0x800010 + 0x10 * (cmd.cpu - 1));
    resp[0] = 0;
    req[1] = cmd.desc;
    req[0] = 6;                      // trigger; do NOT wait
    u32 ok = 1;
    uart_.write(ok);
    return true;
  }

  bool nand_wait() {
    struct { u32 cpu; u32 job; u32 spin; } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    auto j = (volatile NandJob*)cmd.job;
    u32 bound = cmd.spin ? cmd.spin : 200000000;
    u32 waited = 0;
    while (!j->done && waited < bound) {
      waited++;
    }
    struct {
      u32 pages_done, bytes, status_last, err, status_tries, fifo_spins,
          timed_out, pad;
    } resp{j->pages_done, j->bytes, j->status_last, j->err, j->status_tries,
           j->fifo_spins, (u32)(j->done ? 0 : 1), 0};
    uart_.write(resp);
    return true;
  }

#if !defined(ROM_ENV)
  // cmd 17: configure the GbE rings and hand the shell over to the packet
  // transport. The reply goes out over the *current* transport first, so the
  // host knows the MAC came up before it switches. There is no way back short
  // of a board reset, which is already the established recovery path.
  bool gbe_start() {
    struct {
      u32 bar;         // func0 BAR0, e.g. 0x90008000
      u32 ring_base;   // MUST be DRAM -- SRAM is denied to the PCIe master
      u8 mac[6];       // station address
      u8 pad[2];
      u32 enter_loop;  // 0 = configure and send one beacon, but stay on UART
      u32 pcs;         // Salina Glue (func3) BAR4 + 0x4000 -- PCS/SerDes
      u32 rx_arm;      // RX descriptor arming value; 0 = the driver's RX_LEN
    } cmd{};
    if (!uart_.read(&cmd)) {
      return false;
    }
    // Modes 11 and 12 operate on an already-initialised MAC: re-running init()
    // would re-do the PHY bring-up and wipe the ring under the trial.
    // Mode 13: arm, then run the real rx_peek/rx_release service loop for a
    // fixed spin and report how many frames were actually consumed. A census
    // taken after the fact cannot tell "engine halted" from "no frame arrived";
    // this services the ring continuously the way the server does, while the
    // host sends traffic into the same window.
    // 13 = arm then service; 14 = service only, relying on init()'s arming.
    // Isolates arm_rx() as the thing that stops reception.
    if (cmd.enter_loop == 13 || cmd.enter_loop == 14) {
      if (cmd.enter_loop == 13) {
        g_gbe.arm_rx();
      }
      u32 got = 0, dropped = 0, first_w = 0, first_b = 0, matched = 0;
      // Long window: the only configuration ever seen to take multiple frames
      // did so from slow ambient traffic, so give a slow sender time to work.
      for (u32 i = 0; i < 12000000; i++) {
        // Capture the descriptor exactly as the hardware left it, before
        // rx_peek's checks and before re-arming, so a rejected frame can be
        // inspected rather than guessed at.
        u32 w_now = *(vu32*)(g_gbe.rx_desc_ + g_gbe.rx_idx_ * GbeMac::DESC_SZ);
        if ((w_now & 0x80000000u) && !first_w) {
          first_w = w_now;
          first_b = *(vu32*)(g_gbe.rx_desc_ + g_gbe.rx_idx_ * GbeMac::DESC_SZ + 4);
        }
        u32 n = 0;
        u8* p = g_gbe.rx_peek(&n);
        if (p) {
          got++;
          // Count frames that would actually be ACCEPTED, not merely delivered.
          // "got" only means rx_peek returned non-null, which stays perfect even
          // when every frame is parsed at the wrong offset -- that is exactly how
          // an RX_PAD of 2 hid while the server answered nothing. matched is the
          // number that clear the ethertype/UDP demux and the MAGIC check.
          u32 et = ((u32)p[12] << 8) | p[13];
          const u8* body = nullptr;
          u32 bodylen = 0;
          if (et == Gbe::ETHERTYPE && n >= Gbe::HDR) {
            body = p + Gbe::ETH_HDR;
            bodylen = n - Gbe::ETH_HDR;
          } else if (et == 0x0800 && n > Gbe::ETH_HDR + 28 &&
                     p[Gbe::ETH_HDR + 9] == 17) {
            u32 ihl = (u32)(p[Gbe::ETH_HDR] & 0xF) * 4;
            u32 uoff = Gbe::ETH_HDR + ihl;
            if (uoff + 8 < n) {
              u32 dport = ((u32)p[uoff + 2] << 8) | p[uoff + 3];
              if (dport == Gbe::UDP_PORT) {
                body = p + uoff + 8;
                bodylen = n - (uoff + 8);
              }
            }
          }
          u32 m = body ? ((u32)body[0] | ((u32)body[1] << 8) |
                          ((u32)body[2] << 16) | ((u32)body[3] << 24))
                       : 0;
          if (body && bodylen >= Gbe::HDR_LEN && m == Gbe::MAGIC) {
            matched++;
          }
          g_gbe.rx_release();
        } else if (g_gbe.rx_ready()) {
          dropped++;
          g_gbe.rx_release();
        }
      }
      struct {
        u32 mode;
        u32 got;
        u32 dropped;
        u32 pad0;
        u32 pad1;
        u32 total;
        u32 link;
        u32 rxc;
        u32 cursor;
        u32 rx_en;
        u32 int_sts;
        u32 frames;
      } r{cmd.enter_loop, got, dropped,
          first_w, first_b,
          got + dropped,
          g_gbe.reg(0x04), matched, g_gbe.reg(0x40),
          g_gbe.reg(0x1D4), g_gbe.reg(0x50), g_gbe.reg(0x100)};
      uart_.write(r);
      return true;
    }

    // Mode 16: score board-issued tx() on an init-once ring. Mode 7 re-runs
    // init() before its single transmit, and a re-init per kick is known to wedge
    // the engine, so mode 7 says nothing about tx() itself. This inits once (the
    // shared path below already did) and then transmits repeatedly, counting the
    // ones the engine actually took -- the cursor moving off the slot. Directly
    // comparable to the host-driven 16/16 on an init-once ring.
    if (cmd.enter_loop == 16) {
      g_gbe_diag.set_mac(cmd.mac);
      u32 sent = 0, first_fail = 0;
      for (u32 i = 0; i < 16; i++) {
        g_gbe_diag.build_frame(true);
        u32 before = g_gbe.reg(0x3C);
        g_gbe.tx(60);
        if (g_gbe.reg(0x3C) != before) {
          sent++;
        } else if (!first_fail) {
          first_fail = i + 1;  // 1-based, 0 = none failed
        }
      }
      u32 r[12] = {16, sent, first_fail, g_gbe.reg(0x3C), g_gbe.reg(0x34),
                   *(vu32*)g_gbe.tx_desc_, g_gbe.reg(0x04), g_gbe.reg(0x50),
                   g_gbe.reg(0x08), g_gbe.reg(0x128), g_gbe.reg(0x44), 0};
      uart_.write(r);
      return true;
    }

    // Mode 17: run the server transport's own hello() -- the exact first thing
    // gbe_server_run() does -- but stay on UART so the result is reportable. The
    // handover replies ok, and then the wire is silent: no hello, no heartbeat.
    // Since tx() itself scores 16/16 from the board (mode 16), the question is
    // whether g_gbe_server.uart_ specifically can transmit, or whether the server
    // dies before it gets there.
    if (cmd.enter_loop == 17) {
      u32 before = g_gbe.reg(0x3C);
      u32 cnt0 = g_gbe.reg(0x128);
      g_gbe_diag.set_mac(cmd.mac);
      g_gbe_diag.hello();
      u32 r[12] = {17, before, g_gbe.reg(0x3C), cnt0, g_gbe.reg(0x128),
                   *(vu32*)g_gbe.tx_desc_, g_gbe.reg(0x34), g_gbe.reg(0x04),
                   (u32)&g_gbe_diag, g_gbe.tx_buf_, g_gbe.tx_desc_, 0};
      uart_.write(r);
      return true;
    }

    // Mode 18: can one frame span two descriptors? True zero-copy needs it --
    // header in one descriptor, payload pointed straight at the source memory in
    // the next, so the DMA engine reads the payload and the CPU never touches
    // it. That would remove the ~4.7 us-per-word uncached source read that is the
    // current throughput limit. Split a 60-byte frame 26 + 34 across two
    // descriptors (SOP on the first, EOP on the second) and see whether one
    // correct 60-byte frame reaches the wire.
    if (cmd.enter_loop == 18) {
      g_gbe_diag.set_mac(cmd.mac);
      g_gbe_diag.build_frame(true);          // 60-byte frame in the TX buffer
      u32 i0 = g_gbe.tx_idx_;
      u32 i1 = (i0 + 1) % GbeMac::N_TX;
      u32 d0 = g_gbe.tx_desc_ + i0 * GbeMac::DESC_SZ;
      u32 d1 = g_gbe.tx_desc_ + i1 * GbeMac::DESC_SZ;
      u32 buf = g_gbe.tx_buf_ + i0 * GbeMac::BUFSZ;
      u32 before = g_gbe.reg(0x3C);
      u32 cnt0 = g_gbe.reg(0x128);
      *(vu32*)(d0 + 4) = buf;
      *(vu32*)(d0 + 8) = 4;
      *(vu32*)(d0 + 12) = 0;
      *(vu32*)(d0 + 0) = 26 | GbeMac::DESC_SOP | GbeMac::DESC_TX_RDY;
      *(vu32*)(d1 + 4) = buf + 26;
      *(vu32*)(d1 + 8) = 4;
      *(vu32*)(d1 + 12) = 0;
      *(vu32*)(d1 + 0) = 34 | GbeMac::DESC_EOP | GbeMac::DESC_TX_RDY |
                         ((i1 == GbeMac::N_TX - 1) ? GbeMac::DESC_WRAP : 0);
      g_gbe.dcache_clean(buf, 60);
      g_gbe.dcache_clean(d0, 32);
      g_gbe.reg(0x34) = g_gbe.reg(0x34) | GbeMac::DMA_KICK;
      for (u32 i = 0; i < 400000; i++) asm volatile("" ::: "memory");
      u32 r[12] = {18, before, g_gbe.reg(0x3C), cnt0, g_gbe.reg(0x128),
                   *(vu32*)d0, *(vu32*)d1, g_gbe.reg(0x50), g_gbe.reg(0x34),
                   i0, i1, 0};
      uart_.write(r);
      g_gbe.tx_idx_ = (i1 + 1) % GbeMac::N_TX;
      return true;
    }

    // Mode 19: time the source read directly, with the cycle counter, instead of
    // inferring it from end-to-end throughput. `bar` is the address, `ring_base`
    // the word count, and bit 0 of `pcs` turns the caches on first. Reads the
    // range twice: if caching is actually happening the second pass is far
    // cheaper, and if the region is non-cacheable both passes cost the same
    // however SCTLR is set. That distinguishes "cache does not help" from "cache
    // is not being used", which end-to-end numbers cannot.
    if (cmd.enter_loop == 19) {
      if (cmd.pcs & 1) {
        enable_caches(true);
      }
      u32 src = cmd.bar;
      u32 n = cmd.ring_base ? cmd.ring_base : 4096;
      enable_ccnt();
      u32 sum = 0;
      u32 t0 = read_ccnt();
      for (u32 i = 0; i < n; i++) sum += *(vu32*)(src + i * 4);
      u32 t1 = read_ccnt();
      for (u32 i = 0; i < n; i++) sum += *(vu32*)(src + i * 4);
      u32 t2 = read_ccnt();
      u32 r[12] = {19, t1 - t0, t2 - t1, n, sum, arm_rsr(SCTLR),
                   cmd.pcs, src, 0, 0, 0, 0};
      uart_.write(r);
      return true;
    }

    // Mode 20: what does one MMIO access to the GbE actually cost? The source
    // read turned out to be only ~6% of a frame (mode 19: 246 cycles/word), so
    // the remaining ~94% has to be somewhere else, and every frame does a
    // read-modify-write of the kick register across PCIe. Times reads and writes
    // separately -- a read has to traverse the link and come back, a write is
    // posted and should be nearly free.
    if (cmd.enter_loop == 20) {
      u32 n = cmd.ring_base ? cmd.ring_base : 1000;
      enable_ccnt();
      u32 acc = 0;
      u32 t0 = read_ccnt();
      for (u32 i = 0; i < n; i++) acc += g_gbe.reg(0x04);
      u32 t1 = read_ccnt();
      for (u32 i = 0; i < n; i++) g_gbe.reg(0x50) = 0;
      u32 t2 = read_ccnt();
      // and a DRAM word read for scale, same loop shape
      u32 t3 = read_ccnt();
      for (u32 i = 0; i < n; i++) acc += *(vu32*)(0x40000000 + (i & 0xFF) * 4);
      u32 t4 = read_ccnt();
      // NFIF sits on a different bus to the GbE BAR. The drain does two NFIF
      // accesses per payload word (FIFO-ready poll + data pop), so if these are
      // far dearer than the 133-cycle GbE access, the drain is MMIO-bound and the
      // flash itself is not the ceiling at all.
      u32 t5 = read_ccnt();
      for (u32 i = 0; i < n; i++) acc += *(volatile u16*)(0x1700E020);
      u32 t6 = read_ccnt();
      u32 r[12] = {20, t1 - t0, t2 - t1, t4 - t3, n, acc, t6 - t5, 0, 0, 0, 0, 0};
      uart_.write(r);
      return true;
    }

    // Mode 21 (region programming REMOVED -- it was wrong and dangerous).
    //
    // Does nothing on purpose. The source range is already cacheable: the MPU
    // is running and region 3 covers 0x00000000+2 GB with C=1. Only SCTLR.C was
    // clear, and setting it makes streaming reads slower (239.8 cyc/word
    // against 137.7), because a sequential dump has no reuse and every miss
    // pulls a 32-byte line to serve one word.
    //
    // Programming regions 0..2 here, as this used to, destroys the live mapping
    // of 0xE0000000 and 0xF0000000 and cannot help anyway: on ARMv7-R the
    // highest-numbered matching region wins, so low entries are shadowed by
    // regions 3..10. Read the map with mode 22 before touching it.

    // Mode 15: a bare TX kick and nothing else, on an already-initialised MAC.
    // The host sets the descriptor up first, so this isolates the one operation
    // that behaves differently depending on who issues it -- the same store of 5
    // to +0x34 transmits from the host's mem_access path and is swallowed from
    // inside tx(). Reports the cursor and KICK_TX either side of the store.
    if (cmd.enter_loop == 15) {
      u32 before = g_gbe.reg(0x3C);
      u32 k0 = g_gbe.reg(0x34);
      g_gbe.reg(0x34) = 5;
      for (u32 i = 0; i < 200000; i++) asm volatile("" ::: "memory");
      u32 r[12] = {15, before, k0, g_gbe.reg(0x3C), g_gbe.reg(0x34),
                   *(vu32*)g_gbe.tx_desc_, g_gbe.reg(0x04), g_gbe.reg(0x50),
                   g_gbe.reg(0x08), g_gbe.reg(0x128), g_gbe.reg(0x30), 0};
      uart_.write(r);
      return true;
    }

    if (cmd.enter_loop == 11 || cmd.enter_loop == 12) {
      u32 uni = 0, bcast = 0, mcast = 0, other = 0;
      if (cmd.enter_loop == 12) {
        g_gbe.arm_rx();
      } else {
        g_gbe.census(&uni, &bcast, &mcast, &other, cmd.mac);
      }
      struct {
        u32 mode;
        u32 uni;
        u32 bcast;
        u32 mcast;
        u32 other;
        u32 total;
        u32 link;
        u32 filt_ctrl;
        u32 filt_data;
        u32 rx_en;
        u32 int_sts;
        u32 frames;
      } r{cmd.enter_loop,
          uni, bcast, mcast, other, uni + bcast + mcast + other,
          g_gbe.reg(0x04), g_tx_stall, g_tx_short,
          g_gbe.reg(0x1D4), g_gbe.reg(0x50), g_gbe.reg(0x100)};
      uart_.write(r);
      return true;
    }

    g_gbe.init(cmd.bar, cmd.ring_base, cmd.mac, cmd.pcs, cmd.rx_arm);

    if (cmd.enter_loop != 1) {
      // Diagnostic modes, each stopping one step further into hello(), all
      // replying over UART so the shell stays reachable:
      //   0 = init only        2 = build the frame   3 = build and transmit
      // enter_loop == 1 is the real handover.
      // Levels 4..7 are cumulative, each one statement further in; 0 = init
      // only. Mode 8 is exclusive -- when it shared the ladder it inherited
      // stage_payload()'s 11 leftover bytes and prepended them to the pattern.
      if (cmd.enter_loop >= 4 && cmd.enter_loop <= 7) {
        g_gbe_diag.set_mac(cmd.mac);
        if (cmd.enter_loop >= 5) {
          g_gbe_diag.stage_payload();
        }
        if (cmd.enter_loop >= 6) {
          g_gbe_diag.build_frame(true);
        }
        if (cmd.enter_loop == 7) {
          g_gbe.tx(60);
        }
      }
      if (cmd.enter_loop == 8) {
        // Emit a known 3000-byte pattern through the real transport path, so it
        // fragments at MTU exactly as a bulk response would. Lets the framing
        // and the host reassembler be validated board->host, which is the
        // direction that transmits reliably.
        g_gbe_diag.set_mac(cmd.mac);
        g_gbe_diag.seq_ = 0x1234;
        g_gbe_diag.txlen_ = 0;
        u8 chunk[100];
        for (u32 i = 0; i < 100; i++) {
          chunk[i] = (u8)i;
        }
        for (u32 r = 0; r < 30; r++) {
          g_gbe_diag.write(chunk, sizeof(chunk));
        }
        g_gbe_diag.flush(true);
        g_gbe_diag.tx_started_ = false;
      }
      // Report the driver's *software* state, not hardware registers. The
      // earlier reply read registers through g_gbe.bar_, so once bar_ was
      // corrupted every field was garbage and said nothing about what corrupted
      // it. These fields are read directly out of the objects.
      struct {
        u32 mode;
        u32 addr_gbe;       // &g_gbe
        u32 addr_diag;      // &g_gbe_diag
        u32 sizeof_gbe;     // sizeof(Gbe) -- how far the buffers reach
        u32 bar;
        u32 tx_buf;
        u32 tx_idx;
        u32 tx_desc;
        u32 rx_desc;
        u32 tx_frame_ptr;   // where build_frame() actually writes
        u32 sp;             // inherited from the loader; not set by entry()
        u32 image_end;      // last byte of .code, i.e. top of the globals
      } d{cmd.enter_loop,
          (u32)&g_gbe,
          (u32)&g_gbe_diag,
          (u32)sizeof(Gbe),
          g_gbe.bar_,
          g_gbe.tx_buf_,
          g_gbe.tx_idx_,
          g_gbe.tx_desc_,
          g_gbe.rx_desc_,
          (u32)g_gbe.tx_frame(),
          0,
          (u32)&g_gbe + (u32)sizeof(g_gbe)};
      asm volatile("mov %0, sp" : "=r"(d.sp));
      uart_.write(d);
      return true;
    }

    // Wait for the PHY before entering the loop. init() above re-runs the PHY
    // bring-up, which drops a link that was already up: handovers were reporting
    // link_sts 0x00000B18 -- bit 0 clear -- right after a census had read
    // 0x00000B19. The server then ran on a dead PHY, received nothing and
    // answered nothing, which is indistinguishable from a broken transport. The
    // reply below is sent after the wait so the host sees the settled state.
    for (u32 i = 0; i < 4000000 && !(g_gbe.reg(0x04) & 1); i++) {
      asm volatile("" ::: "memory");
    }
    // Arm the RX ring explicitly before serving. Mode 13 (arm, then service)
    // consumes hundreds of frames while the server, which relied on init()'s own
    // arming, received nothing at all -- it sent its hello and then sat silent
    // through every request. That is the difference mode 14 was added to isolate.
    g_gbe.arm_rx();
    struct {
      u32 ok;
      u32 link_sts;
    } resp{1, g_gbe.reg(0x04)};
    uart_.write(resp);
    gbe_server_run(cmd.mac);
  }
#endif  // !ROM_ENV

  bool dummy() { return false; }

  [[noreturn]] void run() {
    while (1) {
      u32 cmd;
      if (!uart_.read(&cmd)) {
        continue;
      }
      using handler_t = bool (Server::*)();
      handler_t handlers[] = {
          &Server::ping,          &Server::mem_access,
          &Server::reg_read,      &Server::reg_write,
          &Server::int_disable,   &Server::int_enable,
          &Server::dabort_status, &Server::cp15_access,
          &Server::core_n_cmd,    &Server::call,
          &Server::dummy,         &Server::dummy,
          &Server::mem_fill,      &Server::dummy,
          &Server::irq_test,      &Server::crc16,
          &Server::mmio_drain,
#if !defined(ROM_ENV)
          &Server::gbe_start,     // 17
          &Server::nand_read_pages,  // 18
          &Server::nand_start,       // 19
          &Server::nand_wait,        // 20
          &Server::nand_stream,      // 21
#endif
      };
      if (cmd >= std::size(handlers)) {
        continue;
      }
      (this->*handlers[cmd])();
    }
  }
  // Named uart_ for continuity with the 33 existing uses; it is the transport,
  // whatever that is for this build.
  Io uart_;
};

using UartServer = Server<Uart>;

#if !defined(ROM_ENV)
using GbeServer = Server<Gbe>;

// All 18 handlers come along unchanged -- they only ever touch the wire through
// uart_.read()/uart_.write(), which is the whole point of the template.
// File-scope, not a function-local static: a local would need __cxa_guard_*,
// which a freestanding build has no business pulling in.
static GbeServer g_gbe_server;

[[noreturn]] void gbe_server_run(const u8* mac) {
  // Reset the transport's state before first use: a non-zero txlen_ makes
  // build_frame() copy a garbage length out of txpay_ and smash memory, and
  // the symptom is a server that never gets a frame onto the wire at all.
  //
  // Caches stay off. SCTLR.C measures 735 KB/s against 826 KB/s without, data
  // correct either way, because the MPU maps this range non-cacheable so the
  // cache never fills and the maintenance is pure cost.
  g_gbe_server.uart_.txlen_ = 0;
  g_gbe_server.uart_.rxlen_ = 0;
  g_gbe_server.uart_.rxpos_ = 0;
  g_gbe_server.uart_.seq_ = 0;
  g_gbe_server.uart_.tx_started_ = false;
  g_gbe_server.uart_.set_mac(mac);
  // A hello-less server was tried, to test whether transmitting first parked the
  // shared DMA engine on the TX ring: it received nothing either, so the TX->RX
  // transition is not the fault. The real one was RX_PAD.
  // No opening hello. Measured both ways with everything else identical: with
  // the hello the server answers nothing at all (PING 0/20, every bulk read
  // failing); without it the transport works and sustains ~114 KB/s. A transmit
  // issued before the first receive blinds RX, and the host does not need the
  // hello -- it learns the board is up from the handover reply over UART.
  g_gbe_server.run();
}
#endif  // !ROM_ENV

#if defined(CPU_EFC) && !defined(ROM_ENV)

extern "C" void install_exception_handlers_efc();

void core_n_server() {
  install_exception_handlers_efc();

  // cpu0 will write into our btcm
  auto req = (vu32*)0x800000;
  // respond to per-cpu slot on cpu0 btcm
  auto resp = (u32*)(0x08800010 + 0x10 * (cpu_id() - 1));

  req[4] = 0xcafec0de;

  while (true) {
    req[0] = 0;
    while (!req[0]) {
    }
    switch (req[0]) {
    case 1:
      coproc_read(&resp[1], &resp[2], req[1]);
      break;
    case 2:
      coproc_write(&resp[1], req[1], req[2]);
      break;
    case 3:
      memcpy((void*)req[1], (void*)req[2], req[3]);
      break;
    case 4:
      resp[1] = *(vu32*)req[1];
      break;
    case 5:
      *(vu32*)req[1] = req[2];
      break;
    case 6: {
      // Call an arbitrary function ON THIS CORE.
      //
      // core_n_cmd only carries three args, and a call needs a target plus up
      // to four arguments, so req[1] points at a descriptor in memory:
      //     [0] func  [1] arg0  [2] arg1  [3] arg2  [4] arg3
      // The result is returned in resp[1].
      //
      // This exists because cpu1's firmware cannot be invoked from cpu0:
      // running it through cpu1's TCM AXI alias keeps relative branches valid
      // but resolves cpu1-private globals (e.g. Mem_AllocVs_Multi's
      // dword_908644) against cpu0's data, which faults. Diagnosing the NAND
      // page-read path needs cpu1 firmware to run on cpu1.
      auto p = (vu32*)req[1];
      using func_t = u32(u32, u32, u32, u32);
      resp[1] = ((func_t*)p[0])(p[1], p[2], p[3], p[4]);
      break;
    }
    }
    resp[0] = 1;
  }
}

// MUST BE PIC!!
[[gnu::section("core_n_entry")]]
void core_n_entry() {
  asm volatile("cpsid aif" : : : "memory");
  auto sctlr = arm_rsr(SCTLR);
  // disable caches
  sctlr &= ~((1 << 12) | (1 << 2));
  // disable mpu
  sctlr &= ~1;
  arm_dsb();
  arm_wsr(SCTLR, sctlr);
  // core_n_server();
  //  a big hack because i cba with gcc's pic
  auto func = *(vu32*)0x8800000;
  ((void (*)(void))(void*)func)();
}

#endif

extern "C" {
[[noreturn]] void entry(void);

[[noreturn]] void uart_server() {
#if defined(CPU_EFC) && !defined(ROM_ENV)
  // if left alone, the other cpus will eventually panic because they recv no
  // response from cpu0 anymore (after ~3 seconds). take over control of them,
  // too.
  *(vu32*)0x800000 = (u32)core_n_server;  // hack
  Ipc ipc;
  // TODO unknown issue using this for cpu3. when using python it works fine.
  for (size_t cpu = 1; cpu < 4; cpu++) {
    // copy to the target's atcm
    extern char __start_core_n_entry[];
    extern char __stop_core_n_entry[];
    size_t core_n_entry_size = __stop_core_n_entry - __start_core_n_entry;

    u32 cpu_atcm_axi = 0x08000000 + 0x02000000 * cpu;
    u32 tcm_addr = 0x11000;
    u32 tcm_addr_axi = cpu_atcm_axi + tcm_addr;

    // memcpy((void*)tcm_addr_axi, __start_core_n_entry, core_n_entry_size);
    for (size_t i = 0; i < core_n_entry_size / 4; i++) {
      ((u32*)tcm_addr_axi)[i] = ((u32*)__start_core_n_entry)[i];
    }
    arm_dsb();
    // run it
    ipc.jump(cpu, tcm_addr);
  }
#endif
  auto server = UartServer();
  server.run();
}

[[noreturn]] static void die(const char* reason) {
  auto uart = Uart();
  uart.write(reason);
  while (true) {
  }
}
void exception_reset() {}
[[gnu::interrupt("UNDEF")]] void exception_undef() {
  die("und");
}
[[gnu::interrupt("SWI")]] void exception_swi() {
  die("swi");
}
[[gnu::naked]] void exception_pabort() {
  asm volatile(
      "push {r0, r1}\n"
      "ldr  r0, =g_paborted\n"
      "mov  r1, #1\n"
      "str  r1, [r0]\n"
      "pop  {r0, r1}\n"
      "movs pc, lr\n");
}
[[gnu::interrupt("ABORT")]] void exception_dabort() {
  g_abort_status = {
      // might be off by 4 but not sure i trust gcc
      .iaddr = (u32)__builtin_return_address(0) - 4,
      .daddr = arm_rsr(DFAR),
      .status = arm_rsr(DFSR),
  };
}
[[gnu::interrupt("IRQ")]] void exception_irq() {
  g_irq_fired += 1;
  Wdt().int_ack_all();
}
[[gnu::interrupt("FIQ")]] void exception_fiq() {
  die("fiq");
}

void setup_rom_env() {
  // in rom ctx, the thunk doesn't disable ints/caches/mpu; do that here
  asm volatile("cpsid aif" : : : "memory");
  auto sctlr = arm_rsr(SCTLR);
  // disable caches
  sctlr &= ~((1 << 12) | (1 << 2));
  // disable mpu
  // XXX very important to NOT disable mpu if we plan on e.g. executing from rom
  // sctlr &= ~1;
  // low vectors
  // XXX exception between when either cpu2 or cpu0 executes this and when
  // vectors are fully setup is dangerous, but it would cause exploit to fail
  // anyway, so doesn't matter
  sctlr &= ~(1 << 13);
  // disable VIC so our irq vector gets used if irq is ever triggered
  sctlr &= ~(1 << 24);
  arm_dsb();
  arm_wsr(SCTLR, sctlr);

  if (cpu_id() == 0) {
    // restore cpu0 bss from cpu2's btcm
    for (u32 i = 0; i < 0x200; i += 4) {
      *(vu32*)(0x00800000 + i) = *(vu32*)(0x0c800000 + i);
    }
    // send a beacon
    Uart().write("uart_shell alive!");
  } else {
    // we can init the cpu0 exception vectors from here
    // setup same as fw, so install_exception_handlers_efc works
    auto cpu0_atcm_axi = (vu32*)0x08000000;
    cpu0_atcm_axi[0] = 0xe59ff018;  // ldr pc, [pc, 0x18]
    cpu0_atcm_axi[1] = 0xe59ff018;
    cpu0_atcm_axi[2] = 0xe59ff018;
    cpu0_atcm_axi[3] = 0xe59ff018;
    cpu0_atcm_axi[4] = 0xe59ff018;
    cpu0_atcm_axi[5] = 0xe320f000;  // nop
    cpu0_atcm_axi[6] = 0xe59ff014;
    cpu0_atcm_axi[7] = 0xe59ff014;  // ldr pc, [pc, 0x14]

    // overwrite cpu0 stacks
    // XXX unfortunately it seems way more reliable to overwrite entire btcm
    // (mainly, bss; overwriting stacks seems less effective), meaning bss has
    // to be restored afterwards.

    // given the above, stash out a copy of cpu0 bss for it to restore later 8)
    for (u32 i = 0; i < 0x200; i += 4) {
      *(vu32*)(0x00800000 + i) = *(vu32*)(0x08800000 + i);
    }
    // now overwrite all of cpu0 btcm
    for (u32 i = 0; i < 0x10000; i += 4) {
      *(vu32*)(0x08800000 + i) = (u32)entry;
    }
    // hang this cpu
    while (true)
      ;
  }
}

void install_exception_handlers_efc() {
  auto sctlr = arm_rsr(SCTLR);
  // disable VIC so our irq vector gets used if irq is ever triggered
  sctlr &= ~(1 << 24);
  arm_wsr(SCTLR, sctlr);

  // overwrite the constant pool in a0tcm
  using handler_t = void (*)(void);
  const handler_t handlers[]{exception_reset,  exception_undef,  exception_swi,
                             exception_pabort, exception_dabort, exception_irq,
                             exception_fiq};
  auto ptrs = (handler_t*)0x20;
  if (cpu_id() != 0) {
    ptrs = (handler_t*)0x24;
  }
  for (size_t i = 0; i < std::size(handlers); i++) {
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Warray-bounds"
#pragma GCC diagnostic ignored "-Wstringop-overflow"
    ptrs[i] = handlers[i];
#pragma GCC diagnostic pop
  }
}

[[noreturn]] void entry() {
  // Reach here in SVC mode(on EAP, SYS on EFC?) with SP in dram.
  // MMU and caches are disabled.
  // IRQ/FIQ are masked.
  u32 tmp0, tmp1;
  asm volatile(
#if defined(CPU_EFC)
      ".cpu cortex-r5\n"
#elif defined(CPU_EAP)
      ".cpu cortex-a7\n"
#endif
      ".syntax unified\n"
      ".arm\n"

      ".equ     MASK_AIF, (1 << 8) | (1 << 7) | (1 << 6)\n"
      ".equ     MODE_ABORT, 0b10111\n"
      ".equ     MODE_SVC, 0b10011\n"

      "setup_stacks:\n"
#if defined(ROM_ENV)
      // when running from sram, move sp into sram
      "ldr      sp, =stack_base\n"
#endif
      // save sp
      "mov      %1, sp\n"
      // abort
      "mov      %0, MASK_AIF | MODE_ABORT\n"
      "msr      cpsr_c, %0\n"
      "add      sp, %1, 0x1100\n"
      // svc
      "mov      %0, MASK_AIF | MODE_SVC\n"
      "msr      cpsr_c, %0\n"
      // move svc sp a bit so we can relaunch the exploit in same location after
      // reset (for EAP)
      "add      sp, %1, 0x1000\n"

#if defined(CPU_EFC)
#if defined(ROM_ENV)
      "bl       setup_rom_env\n"
#endif
      "bl       install_exception_handlers_efc\n"
#elif defined(CPU_EAP)
      // set vbar
      "ldr      %0, =vbar_dram\n"
      "mcr      p15, 0, %0, c12, c0, 0\n"
#endif
      "b        uart_server\n"

#if defined(CPU_EAP)
      ".align 5\n"
      "vbar_dram:\n"
      "b        exception_reset\n"
      "b        exception_undef\n"
      "b        exception_swi\n"
      "b        exception_pabort\n"
      "b        exception_dabort\n"
      "nop\n"
      "b        exception_irq\n"
      "b        exception_fiq\n"
#endif
      : "=r"(tmp0), "=r"(tmp1));
  __builtin_unreachable();
}
}