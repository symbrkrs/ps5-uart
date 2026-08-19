// tdev_scan.c -- EFC-side EXACT page reduction for titania.hwdev.HardwareDevice.
//
// A strict superset of nd_scan.c's `nand_scan`, which this is modelled on line for
// line.  The one thing it adds is `first_nonff`, the byte offset of the first byte
// that is not 0xFF -- free to compute while draining, and the difference between
// "this page is not blank" and "this page has three programmed bytes at offset
// 9000, which is what a torn program looks like".
//
// WHY DEVICE-SIDE, AND WHY EXACT
// ------------------------------
// The UART is the bottleneck, not the NAND read: 0.44 s per page, of which 0.40 s
// is shipping 18432 bytes over a 460800-baud line already running at 91% of its
// line rate.  Reading the page into the EFC costs nothing we care about.  So every
// byte is examined here, on the device, and only a 64-byte verdict crosses the
// wire.  There is NO SAMPLING: an approximate "blank" is worse than no fast path,
// because the error is silent and self-confirming -- a survey that skips pages it
// wrongly believes empty reports exactly the emptiness it assumed.  That already
// happened once in this project, across 19 WERUs.
//
// SAFETY -- a compile-time guarantee, not a runtime check
// ------------------------------------------------------
// This file issues NO NAND COMMANDS AT ALL.  It touches exactly two registers:
// NFIF+0x20 (read, to poll FIFO_READY) and NFIF+0x2C (read, the FIFO data port).
// It never writes a command port, so no opcode -- read, program (0x80/0x10),
// erase (0x60/0xD0) or set-feature (0xEF/0xD5) -- is reachable from the host
// through it.  The host arms the read exactly as `nd_fwpage.FwPage.read_page`
// does; this only replaces the drain loop.
//
// There is deliberately no `..._range` entry point.  nd_scan.c's version drives
// the command ports itself, and it hangs the stub (it reproduces `im_setup` too
// literally, writing whole halfwords where the Python does a read-modify-write).
// Recovering from that needs `nd_reset.py --full`, so it is not worth the risk for
// a saving that is small next to the 384x the drain-side reduction already gives.
//
// Build/load: titania/hwdev.py, `_load_scan_stub()`.
#include <stdint.h>

#define REG16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define REG32(a) (*(volatile uint32_t *)(uintptr_t)(a))

#define FIFO_READY 0x4000u      /* NFIF+0x20 bit14 */

#define AU_WORDS 1152u          /* 4608 bytes per AU */
#define NO_NONFF 0xFFFFFFFFu    /* first_nonff when every byte is 0xFF */

struct trec {                   /* 64 bytes; keep in sync with hwdev.SCAN_FMT */
    uint32_t words;             /* 32-bit words actually popped; 0 => read failed */
    uint32_t nonff;             /* bytes that are NOT 0xFF, over the whole page   */
    uint32_t first_nonff;       /* byte offset of the first such byte, or ~0      */
    uint32_t crc;               /* CRC32, reflected, poly 0xEDB88320              */
    uint32_t zero;              /* bytes that are 0x00                            */
    uint32_t aux[8];            /* AU0..AU3 aux words, raw (still scrambled)      */
    uint32_t pad[3];
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

/* Drain nwords from the FIFO the host has already armed, reducing as we go.
 * Buffers nothing: the page never exists in memory, only the running totals. */
int tdev_scan(uint32_t nfif, uint32_t dst, uint32_t nwords, uint32_t spin)
{
    volatile struct trec *r = (volatile struct trec *)(uintptr_t)dst;
    const uint32_t cfg = nfif + 0x20;
    const uint32_t data = nfif + 0x2C;
    uint32_t i, t, v, k, b, s;
    uint32_t nonff = 0, zz = 0, crc = 0xFFFFFFFFu, first = NO_NONFF;

    if (spin == 0)
        spin = 200000;
    for (k = 0; k < 8; k++)
        r->aux[k] = 0;

    for (i = 0; i < nwords; i++) {
        t = spin;
        while (t != 0 && (REG16(cfg) & FIFO_READY) == 0)
            t--;
        if ((REG16(cfg) & FIFO_READY) == 0)
            break;                    /* short drain -- report how far we got */
        v = REG32(data);

        {   /* the two aux words at the head of each of the four AUs */
            uint32_t q = i / AU_WORDS, m = i - q * AU_WORDS;
            if (q < 4u && m < 2u)
                r->aux[2u * q + m] = v;
        }

        for (s = 0; s < 4u; s++) {
            b = (v >> (8u * s)) & 0xFFu;
            if (b != 0xFFu) {
                nonff++;
                if (first == NO_NONFF)
                    first = i * 4u + s;
                if (b == 0u)
                    zz++;
            }
            crc = crc_byte(crc, b);
        }
    }

    r->words = i;
    r->nonff = nonff;
    r->first_nonff = first;
    r->crc = crc ^ 0xFFFFFFFFu;
    r->zero = zz;
    return (int)i;
}
