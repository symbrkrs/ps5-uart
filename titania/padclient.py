"""A uart_shell client wrapper that stops small GbE reads from stalling.

The problem, MEASURED (gbe_lat.py, gbe_reg_bench.py, this board)

    reply size      mean            min
        2 B         315-2153 ms     2.3 ms
        4 B         1389 ms         10.6 ms
       64 B         905 ms          2.5 ms
      256 B         3.0 ms          1.4 ms
     1024 B         4.3 ms          4.3 ms
    65536 B         80.6 ms         80.5 ms   (794 KB/s)

A reply of 256 bytes or more moves immediately, every time.  Below that it
usually sits until `GbePort.read`'s retry backoff (0.15 s, doubling) shakes it
loose -- which is exactly the shape of a ~1 s mean with a 2 ms best case.  It is
not bandwidth: the same link streams a 20 MB block in ~9 s.

WHY THIS MATTERS SO MUCH HERE
One set/GET FEATURES is ~18 register accesses, of which 8-10 are reads, and a
retry-level apply is 2 sets + 2 gets on one CE.  At ~1.5 s a read that is 35-90
s per CE, ~10 min per level across 11 CEs, and ~95% of the ladder's wall clock
once level 1 had done the bulk of the rescuing.  The register sequence is not
the problem and is not changed here; only how each read is carried.

THE FIX
Issue the real read and a dummy DRAM read as one exchange.  The shell reads
commands as a byte stream -- GbePort buffers writes and flushes them on the
next read -- so two mem_access requests go out together and the board answers
both in order.  The wanted bytes come back at the front of a reply big enough
to move.  MEASURED: 2153 ms -> 57 ms per register read, same register value.

WHY THE PADDING READS DRAM AND NEVER MMIO
An MMIO read can have side effects -- reading the indirect FIFO data port pops
it, which is how `im_read` drains a page -- so a padding read into a register
window would corrupt the very engine this is used to program.  0x40000000 is
ordinary DRAM and reading it is free of consequence.

WHAT IS NOT WRAPPED
`write*` is passed straight through: writes cost ~0 already because GbePort
buffers them and they draw no reply.  Bulk reads of >= PAD_MIN bytes are passed
through too -- they are already on the fast side of the threshold, and padding
them would only add transfer time.

The timings above are one board on one link.  The threshold is a property of the
transport's retry backoff, not of the silicon, so expect the shape to hold --
small replies stall, large ones do not -- while the exact milliseconds will not.
`PAD_MIN` and `PAD_BYTES` are module constants so another link can be measured
and the wrapper retuned in one place.
"""
import struct

#: Ordinary DRAM.  Read-only here, and never an MMIO window -- see the header.
PAD_ADDR = 0x40000000

#: Replies at or above this size were fast in every sample; below it they stall.
PAD_MIN = 256

#: How much DRAM to append.  512 keeps the reply comfortably over the threshold
#: without paying for transfer we throw away: at the measured 794 KB/s the pad
#: itself is ~0.6 ms.
PAD_BYTES = 512


class PadClient:
    """Wraps a uart_shell Client, padding the reads that would otherwise stall.

    Presents the slice of the Client surface the NFIF code uses.  Everything
    else is delegated, so it can stand in for the real client anywhere.
    """

    def __init__(self, client, pad=PAD_BYTES, pad_addr=PAD_ADDR, tries=4):
        self.c = client
        self.pad = int(pad)
        self.pad_addr = int(pad_addr)
        self.tries = int(tries)
        self.padded = 0
        self.passed = 0
        self.short = 0

    def __getattr__(self, name):
        return getattr(self.c, name)

    def warmup(self):
        """Absorb the first-exchange loss before any register read sees it.

        MEASURED (`pad_repro.py`): the first exchange after bring-up costs
        1704 ms and every one after it 3-5 ms, so something about the first is
        routinely lost and recovered by the transport's own retry.  A bulk DRAM
        read is the right thing to spend that on -- it is idempotent, it touches
        no MMIO, and it means the ladder's first SET FEATURES is not the request
        standing in the blast radius.  Failure here is not fatal: the retry in
        `_read_padded` covers the same ground, this just moves the cost.
        """
        try:
            self.c.read(self.pad_addr, 4096)
        except Exception:                                    # noqa: BLE001
            pass

    # -- the padded path --------------------------------------------------
    def _read_padded(self, addr, size, stride):
        """One register read, carried inside a reply big enough to move.

        Retries a short reply rather than raising on it.  This is not
        defensiveness: the first exchange after bring-up really does come back
        empty sometimes, and ladder_run died on exactly that -- `got 0 of 514`
        on its first register read, after the identical call had run 20/20
        standalone.  A retry is safe here in a way it would not be for a write:
        both halves are mem_access reads, the board holds no state across them,
        and GbePort re-issues under a fresh sequence number.

        The prefix of a short reply is still never returned.  A partial reply
        means the exchange is out of step, so the bytes on the wire may belong
        to the other request -- serving them would be a register value invented
        from a DRAM byte.  Drop it, resynchronise, ask again.
        """
        c = self.c
        want = size + self.pad
        last = 0
        for _attempt in range(self.tries):
            c._write_mem_access(addr, size // stride, stride, 0)
            c._write_mem_access(self.pad_addr, self.pad, 1, 0)
            data = c.port.read(want)
            if len(data) == want:
                self.padded += 1
                return data[:size]
            last = len(data)
            self.short += 1
            try:
                c.port.reset_input_buffer()
            except Exception:                                # noqa: BLE001
                pass
        raise IOError("padded read of %d B at 0x%08X: got %d of %d after %d "
                      "attempts" % (size, addr, last, want, self.tries))

    def read(self, addr, size):
        if size >= PAD_MIN:
            self.passed += 1
            return self.c.read(addr, size)
        stride = 4 if (addr % 4 == 0 and size % 4 == 0) else 1
        return self._read_padded(addr, size, stride)

    def read8(self, addr):
        return struct.unpack("<B", self._read_padded(addr, 1, 1))[0]

    def read16(self, addr):
        return struct.unpack("<H", self._read_padded(addr, 2, 2))[0]

    def read32(self, addr):
        return struct.unpack("<I", self._read_padded(addr, 4, 4))[0]

    # -- pass-through ------------------------------------------------------
    def write(self, addr, data):
        return self.c.write(addr, data)

    def write8(self, addr, val):
        return self.c.write8(addr, val)

    def write16(self, addr, val):
        return self.c.write16(addr, val)

    def write32(self, addr, val):
        return self.c.write32(addr, val)

    @property
    def port(self):
        return self.c.port
