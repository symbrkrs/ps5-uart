"""GbE transport for uart_client.Client -- Phase 5 of GBE_UART_SHELL_PLAN.md.

`Client` reaches the wire only through `self.port`, using a small slice of the
pyserial surface (read/write/timeout/in_waiting/reset_*_buffer/open/close/
is_open). `GbePort` implements exactly that slice over raw Ethernet, so all
eighteen command encoders work unchanged -- only the byte-mover differs, which is
the same split the payload uses.

Raw frames go through Npcap's wpcap.dll via ctypes rather than scapy: scapy hangs
during interface enumeration on this host, and ctypes has no import cost or
dependency to manage. Injection generally needs an elevated prompt.

Framing matches `struct Gbe` in uart_shell.cpp:

    ethernet(14)  dst | src | 0x88B5
    header(12)    magic 'PS5G' | seq u16 | flags u16 | len u16 | rsvd u16
    payload       <= 1400 bytes

A request is one frame with FINAL set. A response is one or more fragments with
the request's seq echoed; the last carries FINAL. Reassembly is by seq, so a
stale or duplicated response is discarded rather than silently sliding the
protocol out of step -- which is the failure mode the UART byte stream has after
a data abort.
"""
import ctypes as C
import os
import socket
import struct
import time

ETHERTYPE = 0x88B5
MAGIC = 0x50533547          # 'PS5G' as a little-endian u32 on the target
FLAG_FINAL = 1
FLAG_ERASED = 2   # payload is a u32 byte count; expand to that many 0xFF
MTU = 1400
ETH_HDR = 14
HDR = ETH_HDR + 12

# Link settings. Requests leave through the OS as UDP broadcasts; replies come
# back as raw frames and are captured with pcap. Every value is discovered by
# default and can be pinned by environment variable when a host has several
# adapters and the automatic choice is wrong.
#
#   TITANIA_GBE_IFACE      substring matched against the pcap device name or
#                          description, e.g. "Intel" or an NPF GUID. Unset means
#                          the only non-loopback device, which is right on a
#                          machine with one wired adapter.
#   TITANIA_GBE_HOST_IP    local address to send from. Unset binds to any, which
#                          is what you want unless the broadcast leaves by the
#                          wrong route.
#   TITANIA_GBE_BCAST      broadcast destination. The limited broadcast address
#                          needs no knowledge of the subnet; set a directed
#                          broadcast (x.y.z.255) if a switch or firewall drops
#                          it.
#   TITANIA_GBE_BOARD_MAC  the station address uart_shell is told to use. Change
#                          it only together with the value in the shell.
#
# If the capture adapter is bridged (a Hyper-V or similar virtual switch), the
# host IP belongs to the virtual adapter while the frames are only visible on
# the physical one -- capture on the physical device and send from the virtual
# address.
#
# There is no host-MAC setting: the OS fills it in on the way out, and replies
# are matched on the BOARD's address rather than ours.
DEFAULT_IFACE_HINT = os.environ.get("TITANIA_GBE_IFACE", "")
DEFAULT_BOARD_MAC = os.environ.get("TITANIA_GBE_BOARD_MAC", "02:11:22:33:44:55")
DEFAULT_HOST_IP = os.environ.get("TITANIA_GBE_HOST_IP", "")
DEFAULT_BCAST_IP = os.environ.get("TITANIA_GBE_BCAST", "255.255.255.255")
RETRY_INTERVAL = 0.15    # first resend of a request that has drawn no response
RETRY_MAX_INTERVAL = 0.8  # backoff ceiling. The board does not service RX while it is
                          # streaming a reply, so a re-poke sent mid-stream is simply not
                          # heard and the attempt is wasted. Doubling past ~1s therefore
                          # just adds dead time: a single lost fragment was costing 9s,
                          # which is exactly 0.15+0.3+0.6+1.2+2+2+2. Keep the ceiling near
                          # the duration of a reply burst instead.
MAX_ATTEMPTS = 4         # whole-exchange retries when a response comes back short
UDP_PORT = 20533       # must match Gbe::UDP_PORT in uart_shell.cpp

PCAP_ERRBUF_SIZE = 256
CAPTURE_BUFFER = 32 << 20   # kernel capture buffer; bulk replies arrive in bursts


class _PcapIf(C.Structure):
    pass


_PcapIf._fields_ = [
    ("next", C.POINTER(_PcapIf)),
    ("name", C.c_char_p),
    ("description", C.c_char_p),
    ("addresses", C.c_void_p),
    ("flags", C.c_uint),
]


class _PcapPktHdr(C.Structure):
    _fields_ = [
        ("ts_sec", C.c_long),
        ("ts_usec", C.c_long),
        ("caplen", C.c_uint),
        ("len", C.c_uint),
    ]


def _load_wpcap():
    for name in ("wpcap.dll", r"C:\Windows\System32\Npcap\wpcap.dll"):
        try:
            return C.WinDLL(name)
        except OSError:
            continue
    raise RuntimeError("wpcap.dll not found -- is Npcap installed?")


class Pcap:
    """Minimal ctypes binding: open a device, send frames, receive frames."""

    def __init__(self, device, snaplen=65536, to_ms=10):
        self.lib = _load_wpcap()
        lib = self.lib
        lib.pcap_open_live.argtypes = [C.c_char_p, C.c_int, C.c_int, C.c_int,
                                       C.c_char_p]
        lib.pcap_open_live.restype = C.c_void_p
        lib.pcap_sendpacket.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
        lib.pcap_sendpacket.restype = C.c_int
        lib.pcap_next_ex.argtypes = [C.c_void_p, C.POINTER(C.POINTER(_PcapPktHdr)),
                                     C.POINTER(C.POINTER(C.c_ubyte))]
        lib.pcap_next_ex.restype = C.c_int
        lib.pcap_close.argtypes = [C.c_void_p]
        lib.pcap_geterr.argtypes = [C.c_void_p]
        lib.pcap_geterr.restype = C.c_char_p

        err = C.create_string_buffer(PCAP_ERRBUF_SIZE)
        self.h = lib.pcap_open_live(device.encode(), snaplen, 1, to_ms, err)
        if not self.h:
            raise RuntimeError(f"pcap_open_live({device}) failed: "
                               f"{err.value.decode(errors='replace')}")
        # Enlarge the kernel capture buffer. A bulk reply arrives as a solid burst
        # -- 590 KB is ~420 back-to-back 1400 B frames in under a second -- and the
        # default buffer is around 1 MB, so a burst sits right at the edge of
        # overrunning it. Dropped fragments were costing whole seconds of
        # retransmission. Npcap-specific and best-effort: if it is missing, the
        # transport still works, just with more loss.
        try:
            lib.pcap_setbuff.argtypes = [C.c_void_p, C.c_int]
            lib.pcap_setbuff.restype = C.c_int
            lib.pcap_setbuff(self.h, CAPTURE_BUFFER)
        except AttributeError:
            pass
        self._hdr = C.POINTER(_PcapPktHdr)()
        self._data = C.POINTER(C.c_ubyte)()

    @staticmethod
    def find_device(hint=DEFAULT_IFACE_HINT):
        lib = _load_wpcap()
        lib.pcap_findalldevs.argtypes = [C.POINTER(C.POINTER(_PcapIf)), C.c_char_p]
        lib.pcap_findalldevs.restype = C.c_int
        lib.pcap_freealldevs.argtypes = [C.POINTER(_PcapIf)]
        err = C.create_string_buffer(PCAP_ERRBUF_SIZE)
        head = C.POINTER(_PcapIf)()
        if lib.pcap_findalldevs(C.byref(head), err) != 0:
            raise RuntimeError(err.value.decode(errors="replace"))
        found = []
        node = head
        while node:
            d = node.contents
            found.append((d.name.decode(), (d.description or b"").decode()))
            node = d.next
        lib.pcap_freealldevs(head)
        if hint:
            for name, desc in found:
                if hint.lower() in desc.lower() or hint.lower() in name.lower():
                    return name
            raise RuntimeError(
                "no pcap device matches %r. Set TITANIA_GBE_IFACE to a "
                "substring of one of:\n%s"
                % (hint, "\n".join("  %s  %s" % nd for nd in found)))

        # No hint: take the only real adapter. Loopback and the "any" pseudo
        # device never carry the board link.
        def usable(name, desc):
            blob = (name + " " + desc).lower()
            return "loopback" not in blob and name != "any"

        real = [(n, d) for n, d in found if usable(n, d)]
        if len(real) == 1:
            return real[0][0]
        raise RuntimeError(
            "%d capture devices are present and none was named, so none is "
            "assumed. Set TITANIA_GBE_IFACE to a substring of one of:\n%s"
            % (len(real), "\n".join("  %s  %s" % nd for nd in real or found)))

    def send(self, frame: bytes):
        if len(frame) < 60:
            frame = frame + b"\x00" * (60 - len(frame))
        if self.lib.pcap_sendpacket(self.h, frame, len(frame)) != 0:
            msg = self.lib.pcap_geterr(self.h)
            raise IOError(f"pcap_sendpacket failed: "
                          f"{(msg or b'').decode(errors='replace')} "
                          f"(raw injection usually needs an elevated prompt)")

    def next_frame(self):
        """One captured frame, or None if the read timed out."""
        rc = self.lib.pcap_next_ex(self.h, C.byref(self._hdr), C.byref(self._data))
        if rc != 1:
            return None
        n = self._hdr.contents.caplen
        return bytes(bytearray(self._data[:n]))

    def close(self):
        if getattr(self, "h", None):
            self.lib.pcap_close(self.h)
            self.h = None


def _mac_bytes(s):
    return bytes(int(x, 16) for x in s.replace("-", ":").split(":"))


class GbePort:
    """A pyserial-shaped object that moves bytes as Ethernet frames."""

    def __init__(self, board_mac=DEFAULT_BOARD_MAC, device=None,
                 timeout=2.0, iface_hint=DEFAULT_IFACE_HINT,
                 host_ip=DEFAULT_HOST_IP, bcast_ip=DEFAULT_BCAST_IP):
        self.board = _mac_bytes(board_mac)
        self.bcast_ip = bcast_ip
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp.bind((host_ip, 0))
        # to_ms=1, not the 10 ms default: Npcap holds captured frames until the
        # read timeout expires, so a 10 ms timeout delivers responses in batches
        # and paces the whole transport at roughly one batch per 10 ms.
        self.pcap = Pcap(device or Pcap.find_device(iface_hint), to_ms=1)
        self.timeout = timeout
        self.is_open = True
        self._rx = bytearray()      # contiguous reassembled prefix
        self._tx = bytearray()      # request bytes not yet sent
        self._seq = 0
        self._complete = True       # response for the last request fully received
        self._frags = {}            # fragment index -> payload, for this seq
        self._next = 0              # next index needed to extend self._rx
        self._final = None          # index of the fragment carrying FLAG_FINAL
        self._early = False         # return partial data instead of re-poking

    # -- pyserial surface ---------------------------------------------------
    def write(self, data: bytes) -> int:
        self._tx += data
        return len(data)

    def read(self, n: int) -> bytes:
        """Read n bytes, re-driving the whole exchange if a response is lost.

        Two distinct failures, both seen on this bench and both handled here:

        * The request never reaches the board. Nothing in the path retransmits --
          it is one UDP broadcast over a vSwitch and a further physical device --
          so `_resend_request` re-sends it while no part of a response has come
          back. This alone took PING from 0/3 to 3/3.
        * A fragment of a multi-frame response is lost. Retransmitting the
          request is useless once the reply has started, and worse, the partial
          response leaves the protocol out of step: every later request then fails
          too, which is why one bad read used to wedge the whole session. So on an
          incomplete response, restart the exchange from scratch under a fresh
          sequence number, which also resynchronises. Safe because the commands
          carried are idempotent reads.
        """
        if self._tx:
            self._payload = bytes(self._tx)
            self._send_request()

        for attempt in range(MAX_ATTEMPTS):
            deadline = time.time() + (self.timeout if self.timeout else 3600)
            # Keep re-poking, with exponential backoff, for the WHOLE deadline.
            #
            # A capped burst of retries was wrong for long-running commands. The
            # board stops servicing RX while it works -- nand_read_pages runs
            # 161 Mcyc for a 32-page batch -- so early retransmits are simply not
            # heard, and if the single reply frame is then lost there is nothing
            # left to re-trigger it: the exchange sat idle to the full timeout and
            # a 32-page batch measured 120 s of wall clock against 161 Mcyc of
            # identical on-device work.
            #
            # Backoff serves both cases: a quick command is retried in 150 ms, a
            # slow one backs off to ~2 s so the target is not re-run faster than
            # it can finish. Re-running is harmless -- the commands are idempotent
            # reads and the sequence number is unchanged, so a duplicate reply is
            # just discarded.
            # Re-poke whenever PROGRESS stalls, not only when nothing at all has
            # arrived. Before the fragment index this had to be the weaker
            # condition: a re-sent reply would have appended its fragments a
            # second time and corrupted the stream, so a fragment lost mid-reply
            # was unrecoverable and the whole request had to be re-run. Now
            # duplicates are dropped by index and gaps are filled, so re-issuing
            # the same request is a safe merge and a single lost fragment costs
            # one round trip instead of a full timeout.
            interval = RETRY_INTERVAL
            retry_at = time.time() + interval
            seen = -1
            while len(self._rx) < n and not (self._complete and self._rx):
                now = time.time()
                if now > deadline:
                    break
                if len(self._frags) != seen:
                    seen = len(self._frags)          # progress: reset the backoff
                    interval = RETRY_INTERVAL
                    retry_at = now + interval
                elif now > retry_at:
                    # Give up early only once there is a prefix worth keeping.
                    # Before the first fragment there is nothing to recover
                    # surgically -- and a stream legitimately takes a moment to
                    # start, since the board runs the NAND setup first -- so an
                    # early bail there just returns nothing.
                    if self._early and self._rx:
                        return self._finish(n)
                    self._resend_request()
                    interval = min(interval * 2, RETRY_MAX_INTERVAL)
                    retry_at = now + interval
                self._pump()
            if len(self._rx) >= n or (self._complete and self._rx):
                break
            if attempt + 1 < MAX_ATTEMPTS and getattr(self, "_payload", None):
                self._tx += self._payload      # rebuild and re-issue, new seq
                self._send_request()

        return self._finish(n)

    def _finish(self, n):
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def stream_progress(self):
        """(bytes contiguous from the start, response complete?).

        A caller that can re-fetch by address -- nand_stream can, it just asks for
        a different page range -- uses this to recover surgically: everything up
        to the first gap is good, so only the remainder needs re-reading. Without
        it the only recovery is to re-run the entire request, which for a 16-page
        batch means re-streaming 294912 bytes to replace one 1400-byte fragment.
        """
        return len(self._rx), self._complete

    def give_up_early(self, on=True):
        """Stop re-poking on a stall and hand back what arrived.

        Only for callers that will do their own targeted re-fetch; the default
        blind retry is right for everything else.
        """
        self._early = on

    @property
    def in_waiting(self) -> int:
        self._pump()
        return len(self._rx)

    def reset_input_buffer(self):
        self._rx.clear()
        for _ in range(256):
            if self.pcap.next_frame() is None:
                break

    def reset_output_buffer(self):
        self._tx.clear()

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False
        self._udp.close()
        self.pcap.close()

    # -- framing ------------------------------------------------------------
    def _send_request(self):
        """Requests go out as a UDP broadcast, not a raw frame.

        Two independent reasons, both measured: pcap_sendpacket does not
        physically transmit on this adapter (frames appear on the capture path
        but never reach the board, not even broadcast), and the board's MAC
        drops unicast addressed to its own station address while accepting
        broadcast. A UDP broadcast from the Windows IP stack is the one path
        that demonstrably arrives. Replies still come back as raw frames, which
        pcap captures reliably.
        """
        payload = bytes(self._tx)
        self._tx.clear()
        if len(payload) > MTU:
            raise ValueError(f"request of {len(payload)} B exceeds one frame; "
                             "bulk writes need fragmentation on the host side")
        self._seq = (self._seq + 1) & 0xFFFF
        self._rx.clear()
        self._frags.clear()
        self._next = 0
        self._final = None
        self._complete = False
        hdr = struct.pack("<IHHHH", MAGIC, self._seq, FLAG_FINAL, len(payload), 0)
        self._req = hdr + payload      # kept for retransmission
        self._udp.sendto(self._req, (self.bcast_ip, UDP_PORT))

    def _resend_request(self):
        """Re-send the last request, unchanged, same sequence number.

        A request is a single UDP broadcast crossing a Hyper-V vSwitch and a
        further physical device, and nothing in the path retransmits: one dropped
        packet loses the whole exchange. That is the bulk of the observed
        unreliability -- typical handovers answered one read in five. The board
        keys its reply off the sequence number in the request, so a duplicate is
        harmless: the host takes the first complete response and ignores the rest.
        Only ever called while no part of a response has arrived, so this cannot
        interrupt a bulk reply already streaming back.
        """
        self._udp.sendto(self._req, (self.bcast_ip, UDP_PORT))

    def _pump(self, block=True) -> bool:
        f = self.pcap.next_frame()
        if f is None:
            return False
        if len(f) < HDR:
            return False
        if struct.unpack_from(">H", f, 12)[0] != ETHERTYPE:
            return False
        # Npcap also hands back our own outbound frames. Without this the
        # request echoes straight into the reassembly buffer and is mistaken for
        # the response.
        if f[6:12] != self.board:
            return False
        magic, seq, flags, ln, frag = struct.unpack_from("<IHHHH", f, ETH_HDR)
        if magic != MAGIC or seq != self._seq:
            return False            # stale or foreign: discard, stay in step
        if HDR + ln > len(f):
            return False
        if frag in self._frags:
            return True             # duplicate -- safe to drop, this is the point
        if flags & FLAG_ERASED:
            # An erased page travels as 4 bytes instead of 18432. The marker
            # carries its own expanded length so the host needs no page geometry.
            if ln < 4 or HDR + 4 > len(f):
                return False
            self._frags[frag] = bytes([0xFF]) * struct.unpack_from("<I", f, HDR)[0]
        else:
            self._frags[frag] = f[HDR:HDR + ln]
        if flags & FLAG_FINAL:
            self._final = frag
        # Extend the contiguous prefix as far as the fragments allow. Out-of-order
        # and re-sent fragments therefore cost nothing: each lands in the map and
        # the prefix grows when its turn comes.
        while self._next in self._frags:
            self._rx += self._frags[self._next]
            self._next += 1
        if self._final is not None and self._next > self._final:
            self._complete = True
        return True


def start_gbe(uart_client, bar=0x90008000, ring_base=0x58100000,
              pcs=0x90604000,
              board_mac=DEFAULT_BOARD_MAC,
              device=None, timeout=2.0, rx_arm=0, iface_hint=None):
    """Hand the board's shell over to GbE and return a Client speaking to it.

    `uart_client` must be a live UART Client with the PCIe path set up
    (recover -> pcie_setup -> gbe_bringup). The payload now runs the PHY init
    itself, in the driver's order, so phy_init.py must NOT be run first --
    doing so leaves CTRL/SERDES/MODE applied against a stale PHY setup.
    ring_base MUST be DRAM:
    SRAM is denied to the PCIe master by the locked SRAM_PROT, and rings there
    are read but never written back.
    """
    import uart_client as _uc

    mac = _mac_bytes(board_mac)
    uart_client._write32(_uc.Client.CMD_GBE_START)
    uart_client._write_fmt("<II", bar, ring_base)
    uart_client.port.write(mac + b"\x00\x00")
    uart_client._write32(1)                     # enter_loop
    uart_client._write32(pcs)                   # Salina Glue BAR4 + 0x4000
    # The payload's command struct ends with rx_arm. Omitting it desynchronises
    # the command: the shell consumes the next host word as rx_arm and every
    # field after that is shifted.
    uart_client._write32(rx_arm)
    ok = uart_client._read32()
    link = uart_client._read32()
    if not ok:
        raise RuntimeError("payload refused to start the GbE transport")

    kw = {"iface_hint": iface_hint} if iface_hint else {}
    port = GbePort(board_mac=board_mac, device=device,
                   timeout=timeout, **kw)
    return _uc.Client(port), link
