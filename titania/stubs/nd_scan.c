// EFC-side page scanning: drain the FIFO and summarise ON THE DEVICE, so a page
// costs ~48 bytes of UART instead of 18432.
//
// WHY
// ---
// Measured: a page read is 0.44 s, of which 0.40 s is shipping 18432 bytes over a
// 460800-baud link (46 KB/s line rate, 91% utilised).  Protocol tweaks cannot help
// -- the link is saturated.  The only lever is to not send the page at all.
//
// Almost every sweep we run needs a verdict, not the bytes:
//   * block survey        -> "is this block erased?"          1 bit
//   * LMA / WERU mapping  -> the 4 AU aux fields              32 bytes
//   * dedupe / verify     -> a CRC                            4 bytes
// nand_scan() computes all three in one pass while draining, and never buffers the
// page, so 18432 bytes become a 48-byte record: 384x less UART traffic.
//
// SAFETY
// ------
// The guarantee this code actually rests on is a COMPILE-TIME one:
//
//   * nand_scan() issues no NAND commands at all -- it only pops the FIFO data port,
//     exactly like nd_drain.c.
//   * nand_scan_range() drives the command ports, but every opcode it can emit is a
//     literal constant in this file (0x00 READ, 0x30 CONFIRM, 0x70 READ STATUS).  No
//     opcode is ever taken from a caller argument, so no program (0x80/0x10/0x15/0x1A),
//     erase (0x60/0xD0) or set-feature (0xEF/0xD5) byte is reachable from the host at all.
//   * The only config words written are 0x1A6 / 0x180, both with bit 4 (IN_OUT, the
//     write-direction bit) clear.  The program and erase arm values 0x1B6 / 0x1B4 differ
//     from these ONLY in that bit, and neither appears in this file.
//
// NOT relied on: WP_N (NFIF+0x00C bit 4).  Secondary sources describe it as a
// write-protect that firmware raises only around program/erase, which would make "bit4
// clear" a silicon-level interlock.  MEASURED ON THIS BOARD IT READS 1 AT REST
// (NFIF+0x00C = 0xecdc) with nothing running, so that description does not hold here --
// either the polarity is inverted, or 0x00C is not that register on Titania.  It is
// reported in the result for visibility but deliberately NOT used as a gate: refusing on
// it would block every read, and trusting it would be a safety claim we have not earned.
#include <stdint.h>

#define REG16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define REG32(a) (*(volatile uint32_t *)(uintptr_t)(a))

#define FIFO_READY   0x4000u    /* NFIF+0x20 bit14 */
#define ARM_BUSY     0x0080u    /* NFIF+0x20 bit7  */
#define CFG_READ_ARM 0x01A6u    /* bit4 clear = read.  0x1B6 would be PROGRAM */
#define CFG_DATA_OUT 0x0180u
#define WP_N_BIT     0x0010u    /* NFIF+0x00C bit4; must stay 0 */

/* AU aux lives at page byte offset k*4608, 8 bytes each -> words k*1152 + {0,1}. */
#define AU_WORDS 1152u

struct rec {                 /* 48 bytes per page */
    uint32_t words;          /* 32-bit words actually popped; 0 => read failed */
    uint32_t ff;             /* count of 0xFF bytes  (== 4*words => erased)    */
    uint32_t zero;           /* count of 0x00 bytes                            */
    uint32_t crc;            /* CRC32 (reflected, poly 0xEDB88320) of the page */
    uint32_t aux[8];         /* AU0..AU3 aux, 8 bytes each, raw (scrambled)    */
};

static const uint32_t crc_nib[16] = {
    0x00000000u, 0x1DB71064u, 0x3B6E20C8u, 0x26D930ACu,
    0x76DC4190u, 0x6B6B51F4u, 0x4DB26158u, 0x5005713Cu,
    0xEDB88320u, 0xF00F9344u, 0xD6D6A3E8u, 0xCB61B38Cu,
    0x9B64C2B0u, 0x86D3D2D4u, 0xA00AE278u, 0xBDBDF21Cu
};

static inline uint32_t crc_byte(uint32_t crc, uint32_t b)
{
    crc ^= b;
    crc = (crc >> 4) ^ crc_nib[crc & 0xF];
    crc = (crc >> 4) ^ crc_nib[crc & 0xF];
    return crc;
}

/* Drain nwords from the FIFO, summarising as we go. Buffers nothing. */
static uint32_t scan_one(uint32_t nfif, volatile struct rec *r,
                         uint32_t nwords, uint32_t spin)
{
    const uint32_t cfg = nfif + 0x20;
    const uint32_t data = nfif + 0x2C;
    uint32_t i, t, v, k, b;
    uint32_t ff = 0, zz = 0, crc = 0xFFFFFFFFu;

    for (k = 0; k < 8; k++)
        r->aux[k] = 0;

    for (i = 0; i < nwords; i++) {
        t = spin;
        while (t != 0 && (REG16(cfg) & FIFO_READY) == 0)
            t--;
        if ((REG16(cfg) & FIFO_READY) == 0)
            break;                       /* short drain -- report how far we got */
        v = REG32(data);

        {   /* capture the two aux words at the head of each of the 4 AUs */
            uint32_t q = i / AU_WORDS, m = i - q * AU_WORDS;
            if (q < 4u && m < 2u)
                r->aux[2u * q + m] = v;
        }

        b = v & 0xFFu;         if (b == 0xFFu) ff++; else if (b == 0) zz++;
        crc = crc_byte(crc, b);
        b = (v >> 8) & 0xFFu;  if (b == 0xFFu) ff++; else if (b == 0) zz++;
        crc = crc_byte(crc, b);
        b = (v >> 16) & 0xFFu; if (b == 0xFFu) ff++; else if (b == 0) zz++;
        crc = crc_byte(crc, b);
        b = (v >> 24) & 0xFFu; if (b == 0xFFu) ff++; else if (b == 0) zz++;
        crc = crc_byte(crc, b);
    }

    r->words = i;
    r->ff = ff;
    r->zero = zz;
    r->crc = crc ^ 0xFFFFFFFFu;
    return i;
}

/* ---- entry 1: summarise a page the HOST has already armed -------------------
 * Drop-in replacement for nand_drain(): same arming sequence on the host side,
 * but the host reads back 48 bytes instead of nwords*4.  Issues no commands.
 */
int nand_scan(uint32_t nfif, uint32_t dst, uint32_t nwords, uint32_t spin)
{
    if (spin == 0)
        spin = 200000;
    return (int)scan_one(nfif, (volatile struct rec *)(uintptr_t)dst, nwords, spin);
}

/* ---- entry 2: the whole sweep on-device -------------------------------------
 * params (host-written, at `prm`):
 *   [0] ce   [1] dev   [2] first_block   [3] nblocks
 *   [4] page [5] block_shift (11)        [6] nwords (4608)
 * Writes nblocks consecutive struct rec at dst. Returns blocks completed.
 *
 * This removes the ~80 register round-trips per page as well as the bulk read,
 * so a sweep costs one call plus 48 bytes per block.
 */
int nand_scan_range(uint32_t nfif, uint32_t dst, uint32_t prm, uint32_t spin)
{
    const volatile uint32_t *p = (const volatile uint32_t *)(uintptr_t)prm;
    volatile struct rec *out = (volatile struct rec *)(uintptr_t)dst;
    const uint32_t cfg = nfif + 0x20;
    uint32_t ce = p[0], dev = p[1], first = p[2], nblk = p[3];
    uint32_t page = p[4], bshift = p[5], nwords = p[6];
    uint32_t n, t, row, tries;

    if (spin == 0)
        spin = 200000;
    if (bshift == 0 || nwords == 0)
        return -2;
    /* WP_N is observed, not enforced -- see the SAFETY note at the top of this file.
       Stashed in the params block so the host can see it without a second round-trip. */
    ((volatile uint32_t *)(uintptr_t)prm)[7] = REG16(nfif + 0x00C);

    for (n = 0; n < nblk; n++) {
        row = ((first + n) << bshift) | page;

        /* ---- READ: cmd1 0x00, 2 col + 3 row cycles, cmd2 0x30 ---- */
        REG16(nfif + 0x002) = (uint16_t)((dev & 0xFFu) | ((ce & 0xFu) << 8));
        REG16(nfif + 0x022) = (uint16_t)0x0000u;              /* cmd1=0x00, col[7:0]=0 */
        REG16(nfif + 0x024) = (uint16_t)(((row & 0xFFu) << 8));/* col[15:8]=0, row[7:0] */
        REG16(nfif + 0x026) = (uint16_t)(((row >> 8) & 0xFFu) |
                                         (((row >> 16) & 0xFFu) << 8));
        REG16(nfif + 0x028) = (uint16_t)0x0030u;              /* cmd2 = 0x30 CONFIRM */
        REG16(nfif + 0x02A) = (uint16_t)0u;                   /* count = 0 */
        REG16(cfg) = (uint16_t)(REG16(cfg) | 0x40u);          /* pulse GO */
        REG16(cfg) = (uint16_t)(REG16(cfg) & (uint16_t)~0x40u);
        REG16(cfg) = CFG_READ_ARM;
        t = spin;
        while (t != 0 && (REG16(cfg) & ARM_BUSY) != 0)
            t--;

        /* ---- tR: poll 0x70 until ARDY (bit5) ---- */
        for (tries = 0; tries < 20u; tries++) {
            uint32_t st;
            REG16(nfif + 0x022) = (uint16_t)0x0070u;          /* cmd1 = 0x70 STATUS */
            REG16(nfif + 0x028) = (uint16_t)0u;
            REG16(nfif + 0x02A) = (uint16_t)4u;
            REG16(cfg) = (uint16_t)(REG16(cfg) | 0x40u);
            REG16(cfg) = (uint16_t)(REG16(cfg) & (uint16_t)~0x40u);
            REG16(cfg) = CFG_DATA_OUT;
            t = spin;
            while (t != 0 && (REG16(cfg) & ARM_BUSY) != 0)
                t--;
            t = spin;
            while (t != 0 && (REG16(cfg) & FIFO_READY) == 0)
                t--;
            if ((REG16(cfg) & FIFO_READY) == 0)
                break;
            st = REG32(nfif + 0x2C);
            if (st & 0x20u)
                break;
        }

        /* ---- data out: cmd1 0x00, no address, count = nwords*4 ---- */
        REG16(nfif + 0x022) = (uint16_t)0x0000u;
        REG16(nfif + 0x028) = (uint16_t)0u;
        REG16(nfif + 0x02A) = (uint16_t)(nwords * 4u);
        REG16(cfg) = (uint16_t)(REG16(cfg) | 0x40u);
        REG16(cfg) = (uint16_t)(REG16(cfg) & (uint16_t)~0x40u);
        REG16(cfg) = CFG_DATA_OUT;

        scan_one(nfif, &out[n], nwords, spin);
    }
    return (int)n;
}
