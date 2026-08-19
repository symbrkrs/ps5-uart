#!/usr/bin/env python3
"""fw_group 2: read, verify, descramble and extract.  Offline -- never touches hardware.

    py nd_g2extract.py --selftest
    py nd_g2extract.py --layout
    py nd_g2extract.py --extract <dump>/raw --out g2.bin

Called by nd_nandtool.py (`extract --group 2`).  Group 2 is a completely different
on-media format from groups 0/1 -- do not reuse the group 0/1 AU layout for it.

WHERE IT LIVES
--------------
Block 0 of CE 0..5, SLC, flat rows (`row == page`), 368 pages.  CE 0 is dead on this
board (READ ID `a7 00 00 ..`, every page reads `a7 a7 00 00 ..`), so CE 1..5 are the
usable copies -- and they are five byte-identical replicas, not an interleave.
Measured: over the 8 rows present on all five CEs (0, 1, 2, 3, 8, 100, 155, 227) the
sha1 of the 16260-byte data region is identical on ce1..ce5, 8/8; over the 19 rows
where the local dumps give 2+ live CEs, all agree.  So one CE is sufficient, and extra
CEs are only useful as a cross-check -- there is nothing to de-interleave.

PAGE GEOMETRY -- 20 x (512 + 300), in 5 chunks of 4
--------------------------------------------------
    codeword   512 payload + 300 LDPC parity      = 812 bytes
    AU/chunk   4 codewords + 4-byte AU digest     = 3252 bytes
    page       5 chunks                           = 16260 bytes of data
               bytes 16260..18432 are erased 0xFF (verified on all 368 rows)
    payload    20 x 512                           = 10240 user bytes per page

`3252 = 2048 + 4*300 + 4` also appears in the boot ROM as `dword_800090`, and
`HalEcu_DownloadLdpc()` sets `nParitySize = 300` with a 512-byte codeword.

The keystream is a 2048-byte BLOCK that restarts for each CHUNK.  This is the one
thing that separates group 2 from groups 0/1, where a single 4116-byte block is reused
by the four AUs of the page.  Get it wrong and the output is right for 2048 bytes and
garbage after -- see the negative controls in the self-test, which reproduce exactly
that failure mode.

    PAGE_RUNS[k] = (page_offset, keystream_index, payload_offset, length)
                 = (k*812 + (k//4)*4, (k%4)*512, k*512, 512)

SCRAMBLER
---------
Identical cipher to groups 0/1, only the layout differs: a 53-bit LFSR,
`seq[N] = seq[N-1]^seq[N-49]^seq[N-50]^seq[N-52]^seq[N-53]`, MSB-first within a byte,
supplied by nd_descrambler.keystream().  The seed depends on the physical row alone --
CE-independent, and `state(row) == state(row & ~0xC000)`.

`titania_nand.seed_state` gives the seed for any folded row in closed form, so group
2 needs no seed work at all.  Verified, not assumed: the four seeds derived the hard
way for group 2 before that solution existed (rows 1, 2, 3, 4 and 154) reproduce it
exactly, as do the seeds harvested for groups 0/1 -- block 8 folds onto block 0 under
`& ~0xC000`, so those cover this block's whole row range.

What is on BLOCK 0 (all 368 rows measured, ce1)
----------------------------------------------
    row   0        device header, plaintext `5a 01 5b 00` then all zero
    row   1        fw header, magic 1d334777, fw_type 1
    rows  2..151   fw 1 payload, AES-128-CBC, 150 pages = payload_len 0x177000
    rows 152..154  3 pages in the clear, byte-identical to fw.sram.efc.bin at
                   0x177000/0x179800/0x17c000 -- read into SRAM, not part of the
                   encrypted payload
    row  155       fw header, magic 1d334777, fw_type 2
    rows 156..181  fw 2 payload, AES-128-CBC, 26 pages = payload_len 0x41000
    rows 182..227  46 pages inside fw 2's ROM load window (rows 155..227, a
                   firmware constant -- see fw.GROUP2_LOAD), i.e. staged past the
                   length fw 2's own header declares, exactly as rows 152..154 are
                   for fw 1.  Their placement is therefore explained; this census
                   used to say unexplained and that word is retracted.
                   Their CONTENT is still open: residual density 0.50 (random),
                   not duplicates of any other row, and they do not decrypt with
                   either header's IV.  Fw 1's overrun was closed by proving it
                   byte-identical to the staged SRAM image; the same check has
                   never been run for fw 2.
    rows 228..367  140 pages of an 8-page fill pattern repeated 17.5 times, exact
                   period 8, every page descrambling to `02 00 00 00 ...` structure

No row of block 0 is erased.

The 4-byte AU MCRC at AU+2948 -- solved, see nd_g2mcrc.py / NAND_AU_MCRC.md
--------------------------------------------------------------------------
Eldora's `auMcrcBytes = auMcrcEn * (2*AU_MCRC_SIZE_OPT + 4)` gives 4 bytes at
SIZE_OPT 0 (groups 0/1 use SIZE_OPT 1 and 6 bytes), so this field is the AU MCRC.
It is CRC-32Q / AIXM over the AU's 2048 stored (scrambled) payload bytes with
the bytes complemented:

    mcrc = crc32q(~message) , p = 0x814141AB, augmented, MSB-first, unreflected,
           init 0, no final xor; field stored big-endian at AU+2948..2951

exactly the 32-bit analogue of the group 0/1 CRC-48 in `nd_mcrc_solve.py`.
Verified 2054/2055 AUs on every raw page on disk; the one failure is the AU that
carries the single payload bit error the A10R LDPC independently locates.
`check_page()` now verifies it.

Correction 2026-08-03 -- `AU_DIGEST_OFF` used to be 3248, which is wrong: that
points at the last 4 bytes of codeword 3's LDPC *parity*.  The correct AU layout is
812 + 812 + 812 + 816 (NAND_LDPC_GROUP2.md sec 2), so the MCRC precedes parity 3.
Every earlier claim about the digest was therefore computed on parity bytes and is
RETRACTED -- in particular the old note "it is GF(2)-linear but not a classical
polynomial CRC ... Do not re-run that search".  Re-run on the right bytes, the
polynomial GCD returns the generator on the first try.  Only `AU_DIGEST_OFF` was
wrong; `PAGE_RUNS`, the descrambling and the extraction were never affected (they
touch payload only).
"""
import argparse
import hashlib
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from . import nd_descrambler as _D
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


try:
    import numpy as _np
except ImportError:                                          # pragma: no cover
    _np = None

# ------------------------------------------------------------------ geometry
PAGE_BYTES = 18432                 # stored bytes per NAND page (8 * 2304)
DATA_BYTES = 16260                 # 5 * AU_BYTES; the rest of the page is erased 0xFF
PAYLOAD_BYTES = 10240              # user bytes per page (5 * 2048)
AU_BYTES = 3252                    # 2048 payload + 4*300 parity + 4 digest
AU_PAYLOAD = 2048
AU_DIGEST_OFF = 2948               # within the AU; was 3248 (parity) until 2026-08-03
AU_DIGEST_LEN = 4
CW_BYTES = 812
CW_PAYLOAD = 512
CW_PARITY = 300
KS_BLOCK = 2048                    # keystream block, restarted for every chunk
N_CHUNK = 5
N_RUN = 20
N_PAGE = 368                       # pages per SLC block
BLOCK = 0
CES = (1, 2, 3, 4, 5)              # CE 0 is dead on this board
DEAD_CES = (0,)                    # READ ID a7 00 00 00 00 00; pages read a7 a7 00 00

#: (page_offset, keystream_index, payload_offset, length) x 20
PAGE_RUNS = tuple((k * CW_BYTES + (k // 4) * AU_DIGEST_LEN,
                   (k % 4) * CW_PAYLOAD,
                   k * CW_PAYLOAD,
                   CW_PAYLOAD) for k in range(N_RUN))

HEADER_MAGIC = bytes.fromhex("1d334777")
HEADER_ZERO_SPAN = (0x4e0, 0x2800)  # must be all zero in a descrambled header page

# key ladder for the AES-128-CBC payload (solved in titania_emu2.py)
OTP_AES_ROOT = bytes.fromhex("63f0cf8914429057988bcc975b8e798b")
KDF_SALT1 = bytes.fromhex("4BCEDB98FEA3A7016A75661A03B74AF5")
KDF_ITERS = 1000

#: measured region map of block 0 -- (first_row, last_row, description)
REGIONS = (
    (0, 0, "device header, plaintext 5a 01 5b 00 then zero"),
    (1, 1, "fw header, fw_type 1"),
    (2, 151, "fw 1 payload, AES-128-CBC, 0x177000 bytes"),
    (152, 154, "fw 1 tail, in the clear, == fw.sram.efc.bin[0x177000:0x17e800]"),
    (155, 155, "fw header, fw_type 2"),
    (156, 181, "fw 2 payload, AES-128-CBC, 0x41000 bytes"),
    (182, 227, "fw 2 tail, staged inside the ROM load window past payload_len; "
               "no IV decrypts it and it is unverified against SRAM"),
    (228, 367, "fill pattern, 8 pages repeated with exact period 8"),
)


# --------------------------------------------------------------- primitives
def folded(row):
    """Physical row -> the index the seed map is keyed by."""
    return row & ~0xC000


def state_for_row(row):
    """The row's 53-bit LFSR state.  Folded; CE-independent."""
    from .titania_nand import seed_state
    return seed_state(folded(row))


_KS = {}


def keystream(state, nbytes=KS_BLOCK):
    """Cached nd_descrambler.keystream -- one 2048-byte block per chunk."""
    k = (state, nbytes)
    if k not in _KS:
        _KS[k] = _D.keystream(state, nbytes)
    return _KS[k]


def _xor(a, b):
    if _np is not None:
        return (_np.frombuffer(a, _np.uint8) ^ _np.frombuffer(b, _np.uint8)).tobytes()
    n = len(a)
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(n, "big")


def descramble_page(raw, state, runs=PAGE_RUNS, ks_len=KS_BLOCK):
    """Raw 18432-byte page + 53-bit state -> 10240 payload bytes (20 x 512).

    `runs`/`ks_len` are parameters only so the self-test can drive the known-bad
    geometries through the same code path; leave them alone in normal use."""
    ks = keystream(state, ks_len)
    out = bytearray(PAYLOAD_BYTES)
    for po, ki, pi, n in runs:
        out[pi:pi + n] = _xor(bytes(raw[po:po + n]), ks[ki:ki + n])
    return bytes(out)


def au_message(raw, chunk):
    """The chunk's 2048 stored payload bytes, in keystream order."""
    b = chunk * AU_BYTES
    return b"".join(bytes(raw[b + j * CW_BYTES: b + j * CW_BYTES + CW_PAYLOAD])
                    for j in range(4))


def au_digest(raw, chunk):
    """The stored 4-byte AU MCRC.  Reproduce it with nd_g2mcrc.mcrc(au_message(...))."""
    b = chunk * AU_BYTES + AU_DIGEST_OFF
    return bytes(raw[b:b + AU_DIGEST_LEN])


def au_mcrc_ok(raw, chunk):
    """True if the chunk's stored AU MCRC matches.  Needs no seed and no ECC."""
    from . import nd_g2mcrc
    return nd_g2mcrc.mcrc(au_message(raw, chunk)) == au_digest(raw, chunk)


#: Rows of block 0 with a known real single-bit media error in the PAYLOAD on this
#: board, i.e. the ones the AU MCRC flags: (row, ce) = (43,1) (196,4) (245,2) (287,4).
#: Five further rows -- 13, 231, 260, 313, 337 -- have a single flipped bit in LDPC
#: PARITY on one CE; those are invisible to the MCRC by construction and show up only
#: in check_replicas().  All nine were localised with nd_g2mcrc.py.
KNOWN_MEDIA_ERROR_ROWS = (43, 196, 245, 287)
KNOWN_PARITY_ERROR_ROWS = (13, 231, 260, 313, 337)


def check_page(raw):
    """Seed-free structural check of one raw group-2 page.

    Returns a dict:
        ok          bool, every check below passed
        size        page length is PAGE_BYTES
        tail_ff     bytes 16260..18432 are all 0xFF (true on all 368 measured rows)
        erased      the whole page reads 0xFF
        chunks      list of 5 dicts: {digest, erased, message_sha1, mcrc_ok}
        mcrc        (n_ok, n_checked) -- the real AU MCRC, CRC-32Q over the stored
                    payload with the bytes complemented (nd_g2mcrc.py).  This is a
                    strong, seed-free, ECC-free check: it catches any single bit
                    flip in the 2048 payload bytes.
        digest_consistent
                    chunks with an identical stored payload have an identical digest
                    and chunks with different payloads have different digests.  This
                    is the only property of the AU MCRC that can be checked without
                    knowing its generator; it is a real check (it catches a bit flip
                    in a page whose chunks are duplicates, which the padded rows and
                    both header rows are), but it is weak -- do not read a pass here
                    as "the page verifies".
        notes       list of human-readable problems
    """
    raw = bytes(raw)
    notes = []
    r = {"size": len(raw) == PAGE_BYTES, "erased": False, "tail_ff": False,
         "chunks": [], "digest_consistent": True, "mcrc": (0, 0), "notes": notes}
    if not r["size"]:
        notes.append(f"page is {len(raw)} bytes, expected {PAGE_BYTES}")
        r["ok"] = False
        return r

    r["erased"] = raw.count(0xFF) == PAGE_BYTES
    tail = raw[DATA_BYTES:]
    r["tail_ff"] = tail.count(0xFF) == len(tail)
    if not r["tail_ff"]:
        notes.append(f"page tail {DATA_BYTES}..{PAGE_BYTES} is not erased "
                     f"({len(tail) - tail.count(0xFF)} non-FF bytes)")

    from . import nd_g2mcrc
    by_msg, by_dig = {}, {}
    n_ok = n_chk = 0
    for c in range(N_CHUNK):
        m = au_message(raw, c)
        d = au_digest(raw, c)
        h = hashlib.sha1(m).hexdigest()[:12]
        erased = m.count(0xFF) == len(m)
        good = None if erased else (nd_g2mcrc.mcrc(m) == d)
        r["chunks"].append({"digest": d, "erased": erased, "message_sha1": h,
                            "mcrc_ok": good})
        if good is not None:
            n_chk += 1
            n_ok += good
            if not good:
                notes.append(f"chunk {c}: AU MCRC mismatch "
                             f"(stored {d.hex()}, computed {nd_g2mcrc.mcrc(m).hex()})")
        if erased:
            continue                                   # erased: nothing to compare
        if by_msg.setdefault(h, d) != d:
            r["digest_consistent"] = False
            notes.append(f"chunk {c}: same stored payload as an earlier chunk but a "
                         f"different digest")
        if by_dig.setdefault(d, h) != h:
            r["digest_consistent"] = False
            notes.append(f"chunk {c}: digest collides with a different payload")

    r["mcrc"] = (n_ok, n_chk)
    r["ok"] = bool(r["size"] and (r["erased"] or r["tail_ff"])
                   and r["digest_consistent"] and n_ok == n_chk)
    return r


def looks_dead(raw):
    """True for a page from a CE that does not answer.

    CE 0 on this board returns a near-constant page (`a7 a7 00 00 ...`).  Detected by
    content rather than by CE number so a second failing die is caught too; a real
    group-2 page uses all 256 byte values (measured: 256 distinct on every row)."""
    return len(set(bytes(raw)[:DATA_BYTES])) <= 8


def check_replicas(pages):
    """{ce: raw} for one row -> (all_equal, {ce: sha1}).

    CE 1..5 hold byte-identical copies, so when two or more are present this is a
    far stronger integrity check than anything available from a single page.  Dead
    CEs are excluded -- they are not replicas, they are silence."""
    h = {ce: hashlib.sha1(bytes(p)[:DATA_BYTES]).hexdigest()
         for ce, p in pages.items() if not looks_dead(p)}
    return len(set(h.values())) <= 1, h


def parse_header(plain):
    """Descrambled header page -> dict, or None if the magic is absent.

    Field names follow the ROM/emulator `fw_hdr_t` (efc_bootrom_emulator.md:824):
    magic u32 at +0x0, fw_type u8 at +0x5, payload_len u32 at +0x8,
    payload_off u32 at +0xC, 16-byte aes-cbc IV at +0x10."""
    plain = bytes(plain)
    if len(plain) < 0x20 or plain[:4] != HEADER_MAGIC:
        return None
    payload_len = struct.unpack_from("<I", plain, 8)[0]
    payload_off = struct.unpack_from("<I", plain, 12)[0]
    return {
        "magic": plain[:4],
        "word4": struct.unpack_from("<I", plain, 4)[0],
        "fw_type": plain[5],
        "payload_len": payload_len,
        "payload_off": payload_off,
        "iv": plain[16:32],
        "payload_pages": payload_len // PAYLOAD_BYTES,
        "header_pages": payload_off // PAYLOAD_BYTES,
        "pubkey": plain[0x20:0x1a0],
        "zero_span_ok": not any(plain[HEADER_ZERO_SPAN[0]:HEADER_ZERO_SPAN[1]]),
    }


def image_key(otp=OTP_AES_ROOT, salt=KDF_SALT1, iters=KDF_ITERS):
    """PBKDF2-HMAC-SHA256(OTP_AES_ROOT, KDF_SALTS[1], 1000, 16)."""
    return hashlib.pbkdf2_hmac("sha256", otp, salt, iters, 16)


def decrypt_payload(ciphertext, iv, key=None):
    """AES-128-CBC decrypt.  Needs pycryptodome; raises ImportError otherwise."""
    from Crypto.Cipher import AES
    n = len(ciphertext) & ~15
    return AES.new(key or image_key(), AES.MODE_CBC, bytes(iv)).decrypt(ciphertext[:n])


# ------------------------------------------------------------- page sources
def find_pages(src, ce=None, block=BLOCK, ces=None):
    """Locate group-2 raw pages.  -> {(ce, row): Path}

    `src` may be
      * a single .bin file          -> {(ce or -1, row-from-name): path}
      * <dump>/raw                 -> ceNN/bBBB_pPPP.bin   (nd_dumpfull archive)
      * <dump>/g2                  -> g2_ceN_rowNNN.bin    (the older dump)
      * the dump, or any parent      -> both of the above, raw preferred

    Only CE 0..5 are scanned.  This matters: block 0 of CE 6..11 is bank B's group
    0/1 header block, a completely different format, and the raw archive holds both
    under the same `ceNN/b000_pPPP.bin` naming.  Pass `ce=` or `ces=` to override.
    """
    src = Path(src)
    allowed = {ce} if ce is not None else set(ces if ces is not None
                                              else CES + DEAD_CES)
    out = {}
    if src.is_file():
        row = _row_from_name(src.name)
        c = _ce_from_name(src.name)
        return {((ce if ce is not None else c), row): src}
    if not src.is_dir():
        return out

    # older g2 dump:  g2_ceN_rowNNN.bin
    for p in src.rglob("g2_ce*_row*.bin"):
        try:
            c = int(p.stem.split("_")[1][2:])
            row = int(p.stem.split("_")[2][3:])
        except (IndexError, ValueError):
            continue
        if c in allowed:
            out.setdefault((c, row), p)
    # raw archive:  ceNN/bBBB_pPPP.bin   (preferred -- overwrite)
    for d in src.rglob("ce*"):
        if not d.is_dir():
            continue
        try:
            c = int(d.name[2:])
        except ValueError:
            continue
        if c not in allowed:
            continue
        for p in d.glob(f"b{block:03d}_p*.bin"):
            try:
                row = int(p.stem.split("_p")[1])
            except (IndexError, ValueError):
                continue
            out[(c, row)] = p
    return out


def _row_from_name(name):
    import re
    m = re.search(r"(?:row|_p)(\d+)", name)
    return int(m.group(1)) if m else -1


def _ce_from_name(name):
    import re
    m = re.search(r"ce(\d+)", name)
    return int(m.group(1)) if m else -1


def load_page(path):
    b = Path(path).read_bytes()
    if len(b) != PAGE_BYTES:
        raise ValueError(f"{path}: {len(b)} bytes, expected {PAGE_BYTES}")
    return b


def read_block(src, ce=None, block=BLOCK):
    """-> (pages, replicas, disagree, dead)

        pages     {row: raw}          one live page per row, first CE in CES order
        replicas  {row: {ce: sha1}}   every live CE that has the row
        disagree  [row, ...]          rows whose live replicas were not identical
        dead      {(ce, row), ...}    pages dropped because the CE does not answer

    With no `ce`, the first live CE that has a row wins, in CES order."""
    found = find_pages(src, ce=ce, block=block)
    byrow = {}
    for (c, row), p in found.items():
        byrow.setdefault(row, {})[c] = p
    pages, replicas, disagree, dead = {}, {}, [], set()
    order = list(CES) + [c for c in sorted({c for c, _ in found}) if c not in CES]
    for row, d in sorted(byrow.items()):
        raws = {}
        for c in order:
            if c not in d:
                continue
            try:
                r = load_page(d[c])
            except ValueError:
                continue
            if looks_dead(r):
                dead.add((c, row))
                continue
            raws[c] = r
        if not raws:
            continue
        ok, h = check_replicas(raws)
        replicas[row] = h
        if not ok:
            disagree.append(row)
        pages[row] = raws[next(iter(raws))]
    return pages, replicas, disagree, dead


# ---------------------------------------------------------------- extraction
def descramble_block(pages):
    """{row: raw} -> {row: 10240 plain bytes}.  Every row has a state."""
    return {row: descramble_page(raw, state_for_row(row))
            for row, raw in sorted(pages.items())}


def scan_images(plain):
    """Find the fw headers and their declared extents.

    -> [{'row', 'fw_type', 'payload_len', 'payload_off', 'iv', 'first_row',
         'last_row', 'slot_last_row', ...}]  ordered by row.
    `last_row` is the header-declared end; `slot_last_row` runs to the row before
    the next header (or before the fill pattern), which is a superset -- for fw 1
    the three slot rows past the declared end are real, in-the-clear image data."""
    imgs = []
    for row in sorted(plain):
        h = parse_header(plain[row])
        if h:
            h["row"] = row
            h["first_row"] = row
            h["last_row"] = row + (h["payload_off"] + h["payload_len"]) \
                // PAYLOAD_BYTES - 1
            imgs.append(h)
    fill = _fill_start(plain)
    for i, h in enumerate(imgs):
        nxt = imgs[i + 1]["row"] if i + 1 < len(imgs) else fill
        h["slot_last_row"] = (nxt - 1) if nxt is not None else max(plain)
    return imgs


def _fill_start(plain):
    """First row of the repeating fill pattern at the end of the block, or None.

    The walk back is one row at a time -- stepping by the period only ever visits
    rows congruent to the last one and stops 3 rows short of the true start."""
    rows = sorted(plain)
    if len(rows) < 40:
        return None
    sig = {r: hashlib.sha1(plain[r]).digest() for r in rows}
    tail = rows[-1]
    period = next((p for p in range(1, 33)
                   if sum(1 for r in rows[-40:] if r - p in sig) > 20
                   and all(sig[r] == sig[r - p] for r in rows[-40:] if r - p in sig)),
                  None)
    if period is None:
        return None
    # anchor one period below the last row, where r + period still exists
    start = tail - period
    if start not in sig or sig[start] != sig[start + period]:
        return None
    while (start - 1 in sig and start - 1 + period in sig
           and sig[start - 1] == sig[start - 1 + period]):
        start -= 1
    return start


def extract(src_dir, out_path, ce=None, split=True, quiet=False):
    """Assemble group 2 from CE 1..5 block 0 and write the descrambled image.

    Writes `out_path` = the whole descrambled block (368 x 10240 bytes, rows in
    order, missing rows zero-filled) and, when `split`, one file per firmware image
    named `<out_path stem>_fw<N>.bin` covering that image's slot.  The per-image
    files are still AES-encrypted from `payload_off` on; use decrypt_payload().

    Returns 0 on success, 1 if nothing could be read -- it is a nd_nandtool exit code.
    """
    out_path = Path(out_path)
    say = (lambda *a: None) if quiet else print

    pages, replicas, disagree, dead = read_block(src_dir, ce=ce)
    if not pages:
        say(f"no group 2 pages found under {src_dir}")
        return 1
    plain = descramble_block(pages)

    img = bytearray(N_PAGE * PAYLOAD_BYTES)
    for row, p in plain.items():
        if row < N_PAGE:
            img[row * PAYLOAD_BYTES:(row + 1) * PAYLOAD_BYTES] = p
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(img))

    ces = sorted({c for r in replicas for c in replicas[r]})
    nrep = sum(1 for r in replicas if len(replicas[r]) > 1)
    say(f"wrote {out_path}: {len(img)} bytes ({N_PAGE} rows x {PAYLOAD_BYTES})")
    say(f"  rows recovered   {len(plain)}/{N_PAGE}"
        + (f"   MISSING {N_PAGE - len(plain)}" if len(plain) < N_PAGE else ""))
    say(f"  CEs present      {ces}   (CE 1..5 are identical replicas)")
    say(f"  replica check    {nrep} rows had 2+ live CEs, "
        f"{'all agreed' if not disagree else f'DISAGREEMENT on rows {disagree}'}")
    if dead:
        say(f"  dead CEs         {sorted({c for c, _ in dead})} "
            f"({len(dead)} pages dropped: the die does not answer)")

    res = {r: check_page(pages[r]) for r in sorted(pages)}
    bad = [r for r in res if not res[r]["ok"]]
    mo = sum(res[r]["mcrc"][0] for r in res)
    mn = sum(res[r]["mcrc"][1] for r in res)
    say(f"  structural check {len(pages) - len(bad)}/{len(pages)} pages pass "
        f"(tail erased + AU MCRC)"
        + (f"; failures {bad[:12]}" if bad else ""))
    say(f"  AU MCRC          {mo}/{mn} AUs verify (CRC-32Q, seed-free)")

    imgs = scan_images(plain)
    say(f"\n  {len(imgs)} firmware image(s):")
    for h in imgs:
        say(f"    FW_ID {h['fw_type']}  header row {h['row']:3d}  "
            f"payload_off {h['payload_off']:#x}  payload_len {h['payload_len']:#x} "
            f"({h['payload_pages']} pages)")
        say(f"      declared rows {h['first_row']}..{h['last_row']}, "
            f"slot rows {h['first_row']}..{h['slot_last_row']}"
            f"   header zero span 0x4e0..0x2800 "
            f"{'ok' if h['zero_span_ok'] else 'NOT ZERO'}")
        say(f"      IV {h['iv'].hex()}")
        if split:
            lo, hi = h["first_row"], h["slot_last_row"]
            body = b"".join(plain.get(r, b"\0" * PAYLOAD_BYTES)
                            for r in range(lo, hi + 1))
            p = out_path.with_name(f"{out_path.stem}_fw{h['fw_type']}.bin")
            p.write_bytes(body)
            say(f"      wrote {p.name}: {len(body)} bytes (rows {lo}..{hi})")

    fill = _fill_start(plain)
    if fill is not None:
        say(f"\n  fill pattern starts at row {fill} "
            f"({max(plain) - fill + 1} rows to the end of the block)")
    return 0


def describe_layout():
    for lo, hi, what in REGIONS:
        n = hi - lo + 1
        print(f"  rows {lo:3d}..{hi:3d}  {n:3d} page{'s' if n > 1 else ' '}  {what}")


# ------------------------------------------------------------------ selftest
def _selftest(src=None):
    src = Path(src) if src else config.dump_path()
    ok = True
    def check(cond, label, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
        return bool(cond)

    print(f"nd_g2extract self-test   source {src}\n")

    # ---- 1. geometry ----------------------------------------------------
    print("[1] geometry")
    check(len(PAGE_RUNS) == N_RUN, f"{N_RUN} runs")
    check(sum(n for *_, n in PAGE_RUNS) == PAYLOAD_BYTES,
          f"runs cover {PAYLOAD_BYTES} payload bytes")
    check(PAGE_RUNS[-1][0] + CW_BYTES + AU_DIGEST_LEN == DATA_BYTES,
          f"data region ends at {DATA_BYTES}",
          f"last run starts {PAGE_RUNS[-1][0]}")
    check(AU_BYTES == AU_PAYLOAD + 4 * CW_PARITY + AU_DIGEST_LEN,
          f"AU {AU_BYTES} = 2048 + 4*300 + 4  (boot ROM dword_800090)")
    check(N_CHUNK * AU_BYTES == DATA_BYTES, f"{N_CHUNK} chunks x {AU_BYTES} = {DATA_BYTES}")
    check(all(PAGE_RUNS[k][1] == (k % 4) * CW_PAYLOAD for k in range(N_RUN)),
          "keystream index restarts every 4 codewords (the 2048-byte block)")

    # ---- 2. scrambler states --------------------------------------------
    print("\n[2] scrambler states")
    from .titania_nand import state_valid
    check(all(state_valid(state_for_row(r)) for r in range(N_PAGE)),
          f"all {N_PAGE} rows of block 0 have a state, and each passes the "
          "free (x+1) parity check")
    check(state_for_row(1) == state_for_row(1 | 0xC000),
          "the state folds: row and row|0xC000 agree")

    # ---- 3. pages -------------------------------------------------------
    print("\n[3] page source")
    pages, replicas, disagree, dead = read_block(src)
    if not check(pages, "group 2 pages found", f"{len(pages)} rows"):
        print("\nSELF-TEST: FAIL (no data)")
        return 1
    ces = sorted({c for r in replicas for c in replicas[r]})
    multi = [r for r in replicas if len(replicas[r]) > 1]
    # Disagreements are expected: this board has nine single-bit media errors
    # scattered over the five replicas.  What must hold is that each disagreeing row
    # is one flipped bit away from the majority copy -- anything else is a decoder
    # bug, not a worn cell.  (Identified with nd_g2mcrc.py / nd_g2ldpc_a10r.py.)
    known = set(KNOWN_MEDIA_ERROR_ROWS) | set(KNOWN_PARITY_ERROR_ROWS)
    check(set(disagree) <= known,
          f"CE replicas agree on the {len(multi)} rows with 2+ live CEs, bar the "
          f"{len(known)} known single-bit media errors",
          f"live CEs {ces}; disagree {disagree}; dead {sorted({c for c, _ in dead})} "
          f"({len(dead)} pages dropped)")
    tailbad = [r for r, p in pages.items()
               if p[DATA_BYTES:].count(0xFF) != PAGE_BYTES - DATA_BYTES]
    check(not tailbad, f"bytes {DATA_BYTES}..{PAGE_BYTES} erased on all {len(pages)} pages",
          f"bad {tailbad[:8]}" if tailbad else "")
    res = {r: check_page(pages[r]) for r in pages}
    chk = [r for r in sorted(res) if not res[r]["ok"]]
    mo = sum(res[r]["mcrc"][0] for r in res)
    mn = sum(res[r]["mcrc"][1] for r in res)
    # row 43 carries a real single-bit media error in its payload -- the MCRC is
    # supposed to flag it, and the A10R LDPC locates the same bit independently.
    check(set(chk) <= set(KNOWN_MEDIA_ERROR_ROWS),
          f"check_page() passes on all {len(pages)} pages bar the known media errors",
          f"failures {chk[:8]}  (known: {list(KNOWN_MEDIA_ERROR_ROWS)})")
    check(mn - mo == len([r for r in chk if r in KNOWN_MEDIA_ERROR_ROWS]),
          f"AU MCRC (CRC-32Q, seed-free) verifies {mo}/{mn} AUs",
          f"{mn - mo} mismatch, all on rows {chk}")

    plain = descramble_block(pages)
    check(len(plain) == len(pages), "every page descrambled")

    # ---- 4. The header checks ------------------------------------------
    print("\n[4] header pages -- magic and the 0x4e0..0x2800 all-zero check")
    hdrs = scan_images(plain)
    check(len(hdrs) == 2, "two fw headers found",
          f"rows {[h['row'] for h in hdrs]}")
    for h in hdrs:
        r = h["row"]
        check(h["magic"] == HEADER_MAGIC, f"row {r}: magic 1d334777",
              h["magic"].hex())
        check(h["zero_span_ok"],
              f"row {r}: descrambled 0x4e0..0x2800 is ALL ZERO",
              f"{sum(1 for b in plain[r][0x4e0:0x2800] if b)} non-zero of "
              f"{0x2800 - 0x4e0}")
        check(h["payload_len"] % PAYLOAD_BYTES == 0,
              f"row {r}: payload_len {h['payload_len']:#x} is a whole number of pages",
              f"{h['payload_pages']} pages")
        check(h["payload_off"] == PAYLOAD_BYTES,
              f"row {r}: payload_off {h['payload_off']:#x} = exactly one page")
    if len(hdrs) == 2:
        check([h["fw_type"] for h in hdrs] == [1, 2],
              "fw_type (u8 at +0x5) is 1 then 2 -- FW_ID 1 and FW_ID 2",
              f"rows {[h['row'] for h in hdrs]}")
        check(hdrs[0]["iv"] == hdrs[1]["iv"], "both images share one IV",
              hdrs[0]["iv"].hex())
        check(hdrs[0]["iv"] == bytes.fromhex("582e2420ecd919908ecd73d7a5e8cba3"),
              "IV is the recorded 582e2420ecd919908ecd73d7a5e8cba3")

    # ---- 5. guard against a constant target ------------------------------
    print("\n[5] trap guards")
    for h in hdrs:
        r = h["row"]
        seg = bytes(pages[r])[:DATA_BYTES]
        # the span that carries payload 0x4e0..0x2800 must not be constant on media,
        # or "it descrambled to zero" would be vacuous (all-ones is an LFSR fixed
        # point, and erased spare area fits any LFSR test)
        raw_of_span = b"".join(bytes(pages[r])[po:po + n] for po, ki, pi, n in PAGE_RUNS
                               if pi + n > 0x4e0)
        nuniq = len(set(raw_of_span))
        check(nuniq > 64 and raw_of_span.count(0xFF) < len(raw_of_span) // 4,
              f"row {r}: the raw bytes behind the zero span are NOT constant",
              f"{nuniq} distinct byte values, {raw_of_span.count(0xFF)} of "
              f"{len(raw_of_span)} are 0xFF")
        check(seg.count(0xFF) < DATA_BYTES // 8,
              f"row {r}: page is genuinely written, not erased",
              f"{seg.count(0xFF)} 0xFF bytes of {DATA_BYTES}")

    # ---- 6. Negative controls -- the checks must have teeth --------------
    print("\n[6] negative controls (these MUST fail to descramble)")
    r = hdrs[0]["row"] if hdrs else 1
    raw = pages[r]
    st = state_for_row(r)
    flat = tuple((k * CW_BYTES, (k % 4) * CW_PAYLOAD, k * CW_PAYLOAD, CW_PAYLOAD)
                 for k in range(N_RUN))
    p_flat = descramble_page(raw, st, runs=flat)
    check(p_flat[:4] == HEADER_MAGIC and any(p_flat[0x4e0:0x2800]),
          "flat 812 pitch: magic still right, zero span BROKEN "
          "(exactly the old bug)",
          f"{sum(1 for b in p_flat[0x4e0:0x2800] if b)} non-zero")
    cont = tuple((po, pi, pi, n) for po, ki, pi, n in PAGE_RUNS)
    p_cont = descramble_page(raw, st, runs=cont, ks_len=PAYLOAD_BYTES)
    nz_lo = sum(1 for b in p_cont[0x4e0:0x800] if b)
    nz_hi = sum(1 for b in p_cont[0x800:0x2800] if b)
    check(nz_lo == 0 and nz_hi > 0,
          "continuous 10240-byte keystream: right for 2048 bytes, BROKEN after",
          f"non-zero 0x4e0..0x800: {nz_lo}, 0x800..0x2800: {nz_hi}")
    # bit 52 is keystream bit 0, so it lands on the magic; bit 0 is keystream bit 52
    # and would leave bytes 0..5 untouched -- an off-by-one-bit control has to flip a
    # bit the check can actually see.
    p_wrong = descramble_page(raw, st ^ (1 << 52))
    check(p_wrong[:4] != HEADER_MAGIC,
          "flipping the state's top bit loses the magic", p_wrong[:4].hex())

    # ---- 7. known plaintext, no AES needed -------------------------------
    print("\n[7] known plaintext vs fw.sram.efc.bin (needs no key)")
    fwp = config.efc_sram_image()
    if fwp.exists():
        fw = fwp.read_bytes()
        hits = []
        for row in sorted(plain):
            o = (row - 2) * PAYLOAD_BYTES
            if o < 0 or o + PAYLOAD_BYTES > len(fw):
                continue
            if plain[row] == fw[o:o + PAYLOAD_BYTES]:
                hits.append(row)
        check(hits == [152, 153, 154],
              "rows 152,153,154 are byte-identical to fw.sram.efc.bin at "
              "0x177000/0x179800/0x17c000",
              f"{len(hits) * PAYLOAD_BYTES} bytes exact, rows {hits}")
    else:
        print(f"  [skip] {fwp} not present")

    # ---- 8. layout -------------------------------------------------------
    print("\n[8] block layout")
    fill = _fill_start(plain)
    check(fill == 228, "fill pattern starts at row 228", f"got {fill}")
    if fill is not None:
        sigs = {hashlib.sha1(plain[r]).digest() for r in plain if r >= fill}
        check(len(sigs) == 8, "the fill pattern is 8 distinct pages",
              f"{len(sigs)} distinct over {sum(1 for r in plain if r >= fill)} rows")
    erased = [r for r, p in pages.items() if p[:DATA_BYTES].count(0xFF) == DATA_BYTES]
    check(not erased, "no row of block 0 is erased", "{len(pages)} rows checked")

    # ---- 9. AES ----------------------------------------------------------
    print("\n[9] AES-128-CBC payload (optional, needs pycryptodome)")
    try:
        key = image_key()
        check(key.hex() == "22c5473e5dfb432f9fc37e4ac0a63f4c",
              "PBKDF2-HMAC-SHA256(OTP, SALT1, 1000, 16)", key.hex())
        for h in hdrs:
            lo = h["row"] + h["header_pages"]
            ct = b"".join(plain[r] for r in range(lo, lo + h["payload_pages"])
                          if r in plain)
            pt = decrypt_payload(ct, h["iv"], key)
            arm = pt[:4] in (bytes.fromhex("d1f021e3"), bytes.fromhex("fe0300ea"))
            check(arm, f"FW_ID {h['fw_type']}: decrypts to ARM code", pt[:8].hex())
            if h["fw_type"] == 1 and fwp.exists():
                m = sum(1 for a, b in zip(pt, fw) if a == b)
                check(m / len(pt) > 0.99,
                      "FW_ID 1 plaintext matches fw.sram.efc.bin",
                      f"{m}/{len(pt)} = {100*m/len(pt):.2f}% "
                      f"(the SRAM dump was taken with the firmware running)")
    except ImportError as e:
        print(f"  [skip] {e}")

    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--layout", action="store_true")
    ap.add_argument("--extract", metavar="SRC")
    ap.add_argument("--out", default="g2_image.bin")
    ap.add_argument("--ce", type=int)
    ap.add_argument("--src", help="source for --selftest (default the dump)")
    a = ap.parse_args()
    if a.layout:
        describe_layout()
        return 0
    if a.extract:
        return extract(a.extract, a.out, ce=a.ce)
    if a.selftest:
        return _selftest(a.src)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
