#!/usr/bin/env python3
"""The group-2 (format B / A10R) AU MCRC.  Solved.  Offline -- never touches hardware.

    py -3 nd_g2mcrc.py            # full derivation + verification report

RESULT
------
The 4-byte AU MCRC of the 512x4 / A10R AU sits at AU+2948 (inside codeword 3's
message, immediately before parity 3) and is

    CRC-32Q / AIXM,  p(x) = 0x814141AB
                          = x^32 + x^31 + x^24 + x^22 + x^16 + x^14 + x^8
                            + x^7 + x^5 + x^3 + x + 1
    message   the AU's 2048 stored (scrambled) payload bytes, in keystream order
              = raw[AU + j*812 : AU + j*812 + 512] for j = 0..3, concatenated
    register  augmented / shift-in form:  crc = rem( ~msg(x) * x^32 , p )
    bytes clocked in complemented, MSB first, unreflected
    init 0, no final xor, no reflection  (MCRC_INV_MODE = 0)
    field     stored big-endian at AU+2948..2951

    mcrc(au_message(raw, chunk)) -> 4 bytes

This is the exact 32-bit analogue of the group 0/1 6-byte AU MCRC
(`nd_mcrc_solve.py`): same augmented register, same complement-the-message
convention, same "computed over the scrambled image, no address input".

VERIFIED: 9196 / 9200 AU-instances -- every live raw group-2 page on disk
(CE 1..5 x 368 rows of block 0).  The 4 failures are exactly the 4 rows where a CE
replica carries a single flipped bit *in the payload*; each differs from the 5-CE
majority in one bit and verifies once that bit is taken from the majority.  The other
5 replica disagreements are bit flips in LDPC PARITY and the MCRC correctly does not
flag them -- parity is not in its message.  9/9 accounted for, no false positives and
no false negatives.

The generator was recovered by polynomial GCD from a training set of four AU pairs
(CE1 rows 20/22/24/26); the other 9180 AUs are strict hold-out and 9176 of them
verify -- the 4 exceptions being the media errors above.
"""
import sys
from pathlib import Path
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

POLY = 0x814141AB                  # CRC-32Q / AIXM
MASK = 0xFFFFFFFF
AU_BYTES = 3252
CW_BYTES = 812
CW_PAYLOAD = 512
AU_MCRC_OFF = 2948                 # within the AU -- not 3248
AU_MCRC_LEN = 4
N_CHUNK = 5

_TBL = None


def _table():
    global _TBL
    if _TBL is None:
        t = []
        for i in range(256):
            c = i << 24
            for _ in range(8):
                c = ((c << 1) ^ POLY) & MASK if c & 0x80000000 else (c << 1) & MASK
            t.append(c)
        _TBL = t
    return _TBL


def crc32q(data, init=0):
    """rem( (init*x^(8n) + data(x)) * x^32 , p ) -- MSB first, unreflected."""
    t = _table()
    c = init & MASK
    for b in data:
        c = ((c << 8) & MASK) ^ t[((c >> 24) ^ b) & 0xFF]
    return c


def mcrc(message):
    """The AU MCRC of a stored (scrambled) 2048-byte AU message -> 4 bytes, BE."""
    return crc32q(bytes(b ^ 0xFF for b in message)).to_bytes(AU_MCRC_LEN, "big")


def au_message(raw, chunk):
    """The chunk's 2048 stored payload bytes (4 x 512), in keystream order."""
    b = chunk * AU_BYTES
    return b"".join(bytes(raw[b + j * CW_BYTES: b + j * CW_BYTES + CW_PAYLOAD])
                    for j in range(4))


def au_mcrc(raw, chunk):
    """The stored 4-byte AU MCRC field."""
    b = chunk * AU_BYTES + AU_MCRC_OFF
    return bytes(raw[b:b + AU_MCRC_LEN])


def check_au(raw, chunk):
    """True if the AU's stored MCRC matches.  Needs no seed, no plaintext, no ECC."""
    return mcrc(au_message(raw, chunk)) == au_mcrc(raw, chunk)


def check_page(raw):
    """-> (n_ok, n_checked, [failing chunk indices]).  Erased AUs are skipped."""
    ok, n, bad = 0, 0, []
    for c in range(N_CHUNK):
        m = au_message(raw, c)
        if m.count(0xFF) == len(m):
            continue
        n += 1
        if check_au(raw, c):
            ok += 1
        else:
            bad.append(c)
    return ok, n, bad


# ------------------------------------------------------------------ self-test
def _pmod(a, b):
    db = b.bit_length() - 1
    while True:
        da = a.bit_length() - 1
        if da < db:
            return a
        a ^= b << (da - db)


def _pgcd(a, b):
    while b:
        a, b = b, _pmod(a, b)
    return a


def _poly_str(p):
    return " + ".join(f"x^{i}" if i > 1 else ("x" if i == 1 else "1")
                      for i in range(p.bit_length() - 1, -1, -1) if (p >> i) & 1)


def _pages():
    """Every raw group-2 page on disk -> [(ce, row, raw)].  Dead CE 0 excluded.

    Covers both the old `<dump>/g2/g2_ceN_rowNNN.bin` dump and the full
    `<dump>/raw/ceNN/b000_pPPP.bin` archive (CE 1..5 x 368 rows)."""
    from . import nd_g2extract as E
    out = []
    for (ce, row), p in sorted(E.find_pages(config.dump_path(), ces=(1, 2, 3, 4, 5)).items()):
        raw = p.read_bytes()
        if len(raw) == 18432 and not E.looks_dead(raw):
            out.append((ce, row, raw))
    return out


def _report():
    from collections import Counter, defaultdict
    from itertools import combinations
    G2 = config.dump_path("g2")
    train_files = sorted(G2.glob("g2_ce1_row*.bin"))[:4] if G2 else []
    if not train_files:
        print("no group-2 pages under <dump>/g2 -- this report re-derives the "
              "generator from media and needs them")
        return 1

    print(__doc__.split("RESULT")[0].strip() + "\n")
    print(f"poly {POLY:#010x}  field AU+{AU_MCRC_OFF}..{AU_MCRC_OFF + 3}\n")

    # ---- 0. recover the generator from a training set, by GCD ------------
    # For any polynomial CRC of fixed-length messages, two AUs differing by D
    # with digests differing by E satisfy  p | D(x)*x^32 + E(x).  Init, final
    # xor and the bytewise complement all cancel, so this prejudges nothing.
    TRAIN = tuple(int(p.stem.split("row")[1]) for p in train_files)
    print(f"[0] generator by polynomial GCD -- TRAINING SET = CE1 rows {TRAIN} only")
    tr = []
    for p in train_files:
        raw = p.read_bytes()
        for c in range(N_CHUNK):
            tr.append((au_message(raw, c), au_mcrc(raw, c)))
    g = 0
    npair = 0
    for (m1, d1), (m2, d2) in list(combinations(tr, 2))[:4]:
        Dv = int.from_bytes(bytes(a ^ b for a, b in zip(m1, m2)), "big")
        E = int.from_bytes(bytes(a ^ b for a, b in zip(d1, d2)), "big")
        r = (Dv << 32) ^ E
        g = r if g == 0 else _pgcd(g, r)
        npair += 1
    while g and not (g & 1):
        g >>= 1
    print(f"    {npair} pairs -> gcd = {g:#x}   degree {g.bit_length() - 1}")
    print(f"        = {_poly_str(g)}")
    print(f"    matches the compiled-in POLY (CRC-32Q / AIXM): "
          f"{g == (POLY | 1 << 32)}")

    # ---- 1. the residual constant ---------------------------------------
    print("[1] K = crc32q(~stored_message) ^ stored_MCRC  -- one constant?")
    ks = Counter()
    where = defaultdict(list)
    ctrl_plain = Counter()
    ctrl_old = Counter()
    pgs = _pages()
    print(f"    ({len(pgs)} live raw pages: CE 1..5 of block 0)")
    for ce, row, raw in pgs:
        for c in range(N_CHUNK):
            m = au_message(raw, c)
            d = int.from_bytes(au_mcrc(raw, c), "big")
            k = crc32q(bytes(b ^ 0xFF for b in m)) ^ d
            ks[k] += 1
            if len(where[k]) < 3:
                where[k].append(f"ce{ce} row{row} chunk{c}")
            ctrl_plain[crc32q(m) ^ d] += 1
            b = c * AU_BYTES + 3248                              # the old offset
            ctrl_old[crc32q(bytes(x ^ 0xFF for x in m))
                     ^ int.from_bytes(raw[b:b + 4], "big")] += 1
    for k, n in ks.most_common(4):
        print(f"    K = {k:#010x}  on {n:5d} AUs   e.g. {', '.join(where[k])}")
    print(f"    distinct K, message complemented : {len(ks)}   <- 1 + the media errors")
    print(f"    control, message NOT complemented: {len(ctrl_plain)} distinct, "
          f"top {hex(ctrl_plain.most_common(1)[0][0])}"
          f" == crc32q(0xFF*2048) {hex(crc32q(b'\xff' * 2048))}")
    print(f"    control, field read at the OLD AU+3248 : {len(ctrl_old)} distinct"
          "  <- parity bytes, no constant")

    # ---- 2. verification -------------------------------------------------
    print("\n[2] verification over every raw group-2 page on disk")
    byce = defaultdict(lambda: [0, 0])
    fails = []
    for ce, row, raw in pgs:
        ok, n, bad = check_page(raw)
        byce[ce][0] += ok
        byce[ce][1] += n
        for c in bad:
            fails.append((ce, row, c))
    tot = [sum(v[0] for v in byce.values()), sum(v[1] for v in byce.values())]
    for ce in sorted(byce):
        v = byce[ce]
        print(f"    CE{ce}: {v[0]}/{v[1]} AUs verify")
    print(f"    TOTAL {tot[0]}/{tot[1]}")
    print(f"    failures: {fails}")
    n_train = len(TRAIN) * N_CHUNK
    print(f"    STRICT HOLD-OUT: {n_train} AUs (CE1 rows {TRAIN}) were used to fit; "
          f"{tot[1] - n_train} were not, and {tot[0] - n_train} of those verify")

    # ---- 2b. negative controls ------------------------------------------
    print("\n[2b] negative controls -- these must NOT give a constant")
    from . import nd_descrambler as _D
    from .titania_nand import seed_state
    ctrl = {"plaintext 2048": Counter(), "payload[0:1024]": Counter(),
            "payload+parity 3248": Counter(), "message byte-reversed": Counter()}
    for p in sorted(G2.glob("g2_ce1_row*.bin")):
        row = int(p.stem.split("row")[1])
        raw = p.read_bytes()
        kstr = _D.keystream(seed_state(row), 2048)
        for c in range(N_CHUNK):
            m = au_message(raw, c)
            d = int.from_bytes(au_mcrc(raw, c), "big")
            pl = bytes(a ^ b for a, b in zip(m, kstr))
            ctrl["plaintext 2048"][crc32q(bytes(b ^ 0xFF for b in pl)) ^ d] += 1
            ctrl["payload[0:1024]"][crc32q(bytes(b ^ 0xFF for b in m[:1024])) ^ d] += 1
            b = c * AU_BYTES
            full = m + b"".join(bytes(raw[b + j * CW_BYTES + CW_PAYLOAD:
                                          b + (j + 1) * CW_BYTES]) for j in range(4))
            ctrl["payload+parity 3248"][crc32q(bytes(x ^ 0xFF for x in full)) ^ d] += 1
            ctrl["message byte-reversed"][crc32q(bytes(x ^ 0xFF for x in m[::-1])) ^ d] += 1
    for k, v in ctrl.items():
        print(f"    {k:24s}: {len(v):5d} distinct K over {sum(v.values())} AUs")

    # ---- 3. every failure is a real media error -------------------------
    # CE 1..5 hold byte-identical replicas, so the majority copy of a row is
    # ground truth.  Adjudicate each MCRC failure against it.
    print("\n[3] are the failures real media errors?  Adjudicate against the CE replicas")
    byrow = defaultdict(dict)
    for ce, row, raw in pgs:
        byrow[row][ce] = raw
    for ce, row, c in fails:
        d = byrow[row]
        maj = Counter(bytes(r[:16260]) for r in d.values()).most_common(1)[0][0]
        m_bad = au_message(bytes(d[ce]), c)
        m_ref = au_message(maj + b"\xff" * (18432 - 16260), c)
        diff = [(i, m_bad[i] ^ m_ref[i]) for i in range(2048) if m_bad[i] != m_ref[i]]
        nbits = sum(bin(v).count("1") for _, v in diff)
        print(f"    ce{ce} row{row} chunk{c}: differs from the {len(d)}-CE majority in "
              f"{len(diff)} byte(s), {nbits} bit(s) {diff[:4]}")
        fixed = bytearray(d[ce])
        base = c * AU_BYTES
        for i, v in diff:
            fixed[base + (i // CW_PAYLOAD) * CW_BYTES + i % CW_PAYLOAD] ^= v
        print(f"        after taking the majority bytes, MCRC verifies = "
              f"{check_au(bytes(fixed), c)}")

    print("\n[3b] the reverse direction: rows where the CE replicas DISAGREE")
    dis = [row for row, d in sorted(byrow.items())
           if len({bytes(r[:16260]) for r in d.values()}) > 1]
    caught = 0
    for row in dis:
        d = byrow[row]
        maj = Counter(bytes(r[:16260]) for r in d.values()).most_common(1)[0][0]
        odd = [ce for ce in sorted(d) if bytes(d[ce][:16260]) != maj]
        flagged = sorted({ce for ce, r, c in fails if r == row})
        caught += (flagged == odd)
        # where in the AU does the minority copy differ?  payload or parity?
        where_ = set()
        for ce_ in odd:
            for i in range(16260):
                if d[ce_][i] != maj[i]:
                    o = i % AU_BYTES
                    j = o // CW_BYTES
                    where_.add("payload" if (j < 4 and o - j * CW_BYTES < CW_PAYLOAD)
                               else ("MCRC field" if AU_MCRC_OFF <= o < AU_MCRC_OFF + 4
                                     else "parity"))
        print(f"    row {row:3d}: minority CE(s) {odd}, differing bytes are in "
              f"{'/'.join(sorted(where_))}, MCRC flags {flagged or '[]'}"
              f"   {'CAUGHT' if flagged == odd else 'not covered' if not flagged else 'MISMATCH'}")
    print(f"    {caught}/{len(dis)} disagreements localised by the MCRC alone; the rest "
          f"are bit flips in LDPC PARITY, which the MCRC does not cover by design.")

    return 0 if len(ks) <= 40 else 1


if __name__ == "__main__":
    sys.exit(_report())
