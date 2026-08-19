// Device-side NFIF FIFO drain.
//
// The read path costs ~9200 serial round-trips per 18432-byte page: FwPage.pop() polls
// the FIFO status (a 16-bit read) and then pops 4 bytes (a 32-bit read), per word, and
// every one of those is a uart_shell transaction.  That is the entire ~6 s per page --
// the cost is round-trips, not bandwidth.
//
// This runs the whole drain loop on Titania instead.  One c.call() plus one bulk read
// replaces the 9200 round-trips.
//
// r0 = NFIF base (0x1700E000)
// r1 = destination buffer
// r2 = number of 32-bit words to pop
// r3 = spin limit per word (0 means use a default)
// returns the number of words actually popped, so the caller can detect a short drain
// rather than silently accepting a truncated page.
//
// Only reads the FIFO data port; issues no NAND commands of its own.
#include <stdint.h>

#define REG16(a) (*(volatile uint16_t *)(uintptr_t)(a))
#define REG32(a) (*(volatile uint32_t *)(uintptr_t)(a))

#define FIFO_READY 0x4000u          /* NFIF+0x20 bit14, same bit FwPage.poll() waits on */

int nand_drain(uint32_t nfif, uint32_t dst, uint32_t nwords, uint32_t spin)
{
    volatile uint32_t *out = (volatile uint32_t *)(uintptr_t)dst;
    const uint32_t cfg = nfif + 0x20;
    const uint32_t data = nfif + 0x2C;
    uint32_t i, t;

    if (spin == 0)
        spin = 200000;

    for (i = 0; i < nwords; i++) {
        t = spin;
        while (t != 0 && (REG16(cfg) & FIFO_READY) == 0)
            t--;
        if ((REG16(cfg) & FIFO_READY) == 0)
            return (int)i;                  /* timed out -- report how far we got */
        out[i] = REG32(data);
    }
    return (int)i;
}
