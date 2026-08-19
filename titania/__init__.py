"""titania -- a software package for working with PS5 Titania NAND.

    from titania import DumpDevice, decode_page, Ftl

    dev = DumpDevice("<dump>/g2")
    page = dev.read_page(ce=1, block=8, page=0)
    aus  = decode_page(page)
    ftl  = Ftl.from_device(dev)
    print(ftl.msos())

LAYERS, bottom to top.  Each one is usable on its own; nothing above reaches
past the layer below it.

    geometry   addressing arithmetic: rows, pbids, WERUs, cell modes
    device     Device: read_page / write_page / erase_block
                 DumpDevice   -- raw dump files on disk
                 HardwareDevice -- the live board, via the UART bridge
                 RawImageDevice -- a flat image from an external NAND reader,
                   addressed by (die, block, page); see NAND_RAW_IMAGE.md for
                   what such a capture must contain to be decodable at all
    codec      the media format: scrambler, MCRC, AU layout, EE3 + A10R LDPC
    fw         firmware images: FW_ID table, group 0/1/2 headers, extraction
    ftl        the storage layer: MSOs, WERUs, NSIDs, SystemMetadata, block maps
    verdict    "did this page read?" -- one verdict per page, with the rules
                 chosen by `geometry.region` and by the media's own descriptors

Beside the LAYERS: `titania.nd_hw`
----------------------------------
A complete NAND driver written from scratch, and the only thing here that does
not go through `device`.  It calls no EFC firmware routine and reads no
firmware-owned structure: its inputs are MMIO registers it programs itself plus
constants transcribed from bootrom and firmware *code*, so
`Nand.init_controller()` brings the flash interface up from any state -- it
replays the bootrom's cold init instruction-by-instruction from `sub_FFFE10C2`,
read-modify-write masks preserved, which is why the outcome does not depend on
what the boot path left in the registers.

It is read-only by construction, not by convention: RESET, READ ID, READ STATUS
and read are the only opcodes implemented, and program/erase are absent from the
source.  `HardwareDevice` is the supported path for normal work; reach for this
when the controller is in an unknown state, or when you need to prove a result
without the firmware anywhere in the picture.

    py -m titania.nd_hw --init      # from the repository root

It imports `uart_client` and `tool` from the root of the checkout, so unlike the
rest of the package it does not work from an installed wheel.

WHAT IS SOLID AND WHAT IS NOT
-----------------------------
The codec layer is complete and verified byte-exact against real media: an AU
re-encodes identically over all 4608 bytes, and both LDPC codes encode and
decode.  Build on it freely.

The ftl layer is a *reconstruction*.  Much of it was derived from a board whose
CE0 is dead and whose main user data was never fully surveyed, so an empty result
frequently means "not found here", not "not present".  Functions that can tell
the difference say so in their return value; functions that cannot say so in
their docstring.  Do not silently treat absence as evidence.

Writing is implemented but disabled by default at every level -- see
`titania.device` for the reasoning and the opt-in.
"""

from .device import Device, DeviceError, WriteProtected, CONFIRM
from . import geometry

__all__ = [
    "Device", "DeviceError", "WriteProtected", "CONFIRM", "geometry",
    "DumpDevice", "HardwareDevice", "RawImageDevice", "RawImage",
    "decode_page", "encode_page", "decode_au",
    "Ftl", "FirmwareImage",
]

__version__ = "0.1"


def __getattr__(name):
    """Lazy imports.

    The hardware backing pulls in the serial stack and the codec pulls in numpy;
    neither should be a cost for someone who only wants `geometry`.
    """
    if name == "DumpDevice":
        from .dumpdev import DumpDevice
        return DumpDevice
    if name == "HardwareDevice":
        from .hwdev import HardwareDevice
        return HardwareDevice
    if name in ("RawImageDevice", "RawImage"):
        from . import rawimage
        return getattr(rawimage, name)
    if name in ("decode_page", "encode_page", "decode_au"):
        from . import codec
        return getattr(codec, name)
    if name == "Ftl":
        from .ftl import Ftl
        return Ftl
    if name == "FirmwareImage":
        from .fw import FirmwareImage
        return FirmwareImage
    raise AttributeError(name)
