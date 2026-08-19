# ps5-uart

Tooling for talking to the service processors on a PS5 mainboard over UART, and
for reading, decoding and verifying the NAND behind its Titania storage
controller.

The repository is two halves that meet at one cable:

* **On the board.** A Raspberry Pi Pico sits between your host and the console's
  UARTs, bridging them to USB and driving reset. `tool.py` speaks to the EMC
  (syscon); `uart_client.py` speaks to the shell loaded onto the EFC or EAP.
* **Off the board.** `titania/` reads a NAND capture from disk and decodes it
  all the way to plaintext — scrambler, LDPC, FTL, namespaces. It needs no
  console and no Pico; a capture and a Python install are enough.

You can use either half on its own. The second half is the larger one, and if
you only want to read a dump someone else took, start at
[`titania/README.md`](titania/README.md).

## Typical build

```
mkdir build && cd build && cmake -GNinja .. && cmake --build .
```

Outputs are placed in `bin/`. Program your Pico with `bin/uart.uf2`, then run
`tool.py`. See [`uart/README.md`](uart/README.md) for wiring.

## Usage — the board

From the `tool.py` interactive shell:

| call | what it does |
|---|---|
| `emc.screset()` | resets syscon (EMC) and brings the rest of the board into a consistent state |
| `emc.unlock()` | unlocks the full EMC command set (UCMD protocol) |
| `emc.unlock_efc()` | loads `bin_blobs/uart_shell.cpp` onto the EFC |
| `emc.load_eap()` | loads the same shell onto the EAP |

`uart_client.py` is what drives `uart_shell.cpp` once it is running — memory
access, register access, the NAND commands, and the Gigabit Ethernet transport
described below.

## Usage — the NAND stack

```
pip install -e .          # the library and the `titania` command
pip install -e '.[gui]'   # ...and the viewer
```

```
titania --help                 # the offline CLI
py -m nandgui --raw <store> --decoded <store>
```

Nothing about paths is compiled in: point it at a capture with `--device
dump:PATH`, `$TITANIA_DUMP`, or by running it beside one. The full story,
including what is verified against silicon and what is reconstruction, is in
[`titania/README.md`](titania/README.md).

## Getting a capture

Reading the NAND over the UART is slow enough to be impractical for the whole
device — the link saturates at ~46 KB/s, and a page is 18432 bytes. Two things
in this tree exist to get around that, both by moving work onto the board:

* **Device-side reduction.** `titania/stubs/{nd_scan,nd_drain,tdev_scan}.c` are
  small ARM stubs, compiled on demand by `titania/armbuild.py` and uploaded to
  the EFC. They drain the NAND FIFO and summarise on the device, so a page costs tens of
  bytes over the wire instead of 18432 when all you need is a verdict, a CRC or
  the aux fields.
* **Gigabit Ethernet.** `titania_gbe.py` and the GbE half of `uart_shell.cpp`
  bring up the board's own Ethernet MAC and carry the same protocol over it.

## Layout

| path | what |
|---|---|
| `uart/` | Pico firmware — the USB/UART bridge, reset control, wiring notes |
| `bin_blobs/` | payloads built for the console's own cores, and the UART/GbE shell |
| `tool.py`, `uart_client.py` | host side of the two protocols |
| `titania/` | the offline NAND package: codec, FTL, RAID, namespaces, CLI |
| `nandgui/` | viewer over a capture — drive map, block stripe, page inspector |
| `rs/` | Rust workers the Python drives as subprocesses (LDPC, state search) |
| `tests/` | offline test suite; no board, no capture, no store required |
| `NAND_DATA_FORMAT.md` | the format reference |

## Documentation

**[`NAND_DATA_FORMAT.md`](NAND_DATA_FORMAT.md)** is the format reference: how
data is laid out on the media, from the page down through the AU to the
codewords, and how a host address reaches it. It marks each claim as measured,
disassembly-derived, self-consistent or unknown, so a reader can tell what rests
on silicon and what rests on a reconstruction.

Some modules cite companion documents (`NAND_RAW_IMAGE.md`,
`NAND_TP_VENDOR_VARIANTS.md`, and others) that are not published here. Where a
comment points at one, the reasoning it refers to is summarised at the point of
use; the citation is a provenance note, not a prerequisite.

Per-component documents: [`titania/README.md`](titania/README.md),
[`nandgui/README.md`](nandgui/README.md), [`uart/README.md`](uart/README.md).

## A note on scope

This repository is about understanding a storage format and reading media that
is already in your possession. The published tree deliberately does not carry
the methods used to extract the boot ROMs or the keys held by the security
block; what is here operates on a console you have already opened and on
captures you have already taken.
