"""Names for the numbers: msoId -> "Type/Group", binId -> name, layout -> name.

An msoId is meaningless on screen. `44` is the partition holding 37,920 of this
drive's 48,000 blocks and it is called `Migration/MediaSpace1-NSID2`; `57` is
`NeoFwPartition/System`, the firmware. The viewer should say so.

The source is `ftl.MsoIdTable.embedded()`, a firmware constant -- it needs no
device, no image and no dump, and it is validated at import by the same
`_validate` a located table goes through. So naming costs one dict build.

WHAT IS DELIBERATELY NOT INVENTED
`MsoIdTable.name()` returns None for msoIds below 32, and that is not a gap in
the table: those are the dynamic active-logical-partition pool, slot index used
as the msoId (0..15 NSID2, 16..31 NSID3), which the static table genuinely never
assigns a Type/Group. This module says "dynamic ... slot N" for them -- which is
what the module docstring establishes -- and never borrows a static name for a
dynamic id. Measured on this capture: all 15 owners present are statically
named, so the dynamic branch is a correctness guard rather than a common path.
"""

from __future__ import annotations

__all__ = [
    "mso_name",
    "mso_label",
    "bin_label",
    "layout_label",
    "available",
]

_TABLE: object | None = None
_TRIED = False
_STATIC: dict[int, str] = {}


def _table() -> object | None:
    """The embedded msoIdTable, or None if titania is not importable.

    Cached including the failure: a viewer on a machine without the WSL share
    should degrade to bare numbers, not retry an import every frame.
    """
    global _TABLE, _TRIED
    if _TRIED:
        return _TABLE
    _TRIED = True
    try:
        from . import ensure_titania_on_path

        ensure_titania_on_path()
        from titania.ftl import MsoIdTable

        _TABLE = MsoIdTable.embedded()
        for mso in range(256):
            nm = _TABLE.name(mso)
            if nm:
                _STATIC[mso] = nm
    except Exception:                                        # noqa: BLE001
        _TABLE = None
    return _TABLE


def available() -> bool:
    """True when names can be resolved at all -- for a status line."""
    _table()
    return bool(_STATIC)


def mso_name(mso_id: int) -> str | None:
    """-> "Type/Group" for a statically assigned msoId, else None.

    None means "the static table does not assign this id", which for ids under
    32 means dynamic rather than unknown. Callers that want something printable
    should use `mso_label`.
    """
    if mso_id is None or int(mso_id) < 0:
        return None
    _table()
    return _STATIC.get(int(mso_id))


def mso_label(mso_id: int, *, with_id: bool = True) -> str:
    """-> a string that is always safe to print.

        57            -> "57 NeoFwPartition/System"
        9             -> "9 (dynamic: NSID2 slot 9)"
        -1            -> "no owner"
        (no titania)  -> "57"

    `with_id` drops the leading number for places that already show it.
    """
    if mso_id is None or int(mso_id) < 0:
        return "no owner"
    mso_id = int(mso_id)
    nm = mso_name(mso_id)
    if nm is None:
        if not _STATIC:
            # titania unavailable: a bare number is honest, "unknown" is not
            return str(mso_id)
        if mso_id < 16:
            nm = "(dynamic: NSID2 slot %d)" % mso_id
        elif mso_id < 32:
            nm = "(dynamic: NSID3 slot %d)" % (mso_id - 16)
        else:
            nm = "(not in the static table)"
    return "%d %s" % (mso_id, nm) if with_id else nm


def bin_label(bin_id: int, bin_names=None) -> str:
    """-> "43 ActiveWrite" style label for a WERU allocation bin.

    `bin_names` is summary.npz's own `bin_names` array when the caller has it;
    the module falls back to ftl.WERU_BIN_NAMES so a caller without the summary
    still gets words. An id with no name stays a number rather than becoming
    "unknown" -- the 53 bins include unnamed bands and a number is the truth.
    """
    if bin_id is None or int(bin_id) < 0:
        return "no bin"
    bin_id = int(bin_id)
    nm = None
    if bin_names is not None:
        try:
            if 0 <= bin_id < len(bin_names):
                nm = str(bin_names[bin_id])
        except Exception:                                    # noqa: BLE001
            nm = None
    if nm is None:
        try:
            from . import ensure_titania_on_path

            ensure_titania_on_path()
            from titania.ftl import WERU_BIN_NAMES

            nm = WERU_BIN_NAMES.get(bin_id)
        except Exception:                                    # noqa: BLE001
            nm = None
    if not nm or nm == str(bin_id):
        return str(bin_id)
    return "%d %s" % (bin_id, nm)


def layout_label(layout: int | None) -> str:
    """-> the aux layout with its firmware name.

    Layout 1 has no name in this firmware image, and `aux.LAYOUT_NAMES` stores
    None for it on purpose. That None is reported as "unnamed in this firmware",
    never filled in with a plausible label -- callers test for it.
    """
    if layout is None:
        return "UNKNOWN"
    try:
        from . import ensure_titania_on_path

        ensure_titania_on_path()
        from titania.auxdata import LAYOUT_NAMES

        nm = LAYOUT_NAMES.get(int(layout), "__missing__")
    except Exception:                                        # noqa: BLE001
        return str(layout)
    if nm is None:
        return "%d (unnamed in this firmware)" % int(layout)
    if nm == "__missing__":
        return "%d (not a layout this firmware defines)" % int(layout)
    return "%d %s" % (int(layout), nm)
