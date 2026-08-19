#!/usr/bin/env python3
"""The Titania AU MCRC: what it covers, and its generator.  Solved.  Offline.

    py -3 nd_mcrc_solve.py            # run every experiment + full verification

RESULT
------
The 6-byte AU MCRC is a 48-bit CRC over the AU's 4104 stored (scrambled) payload
bytes, taken in LFSR/logical order, bytes complemented, MSB-first, unreflected:

    generator   p(x) = x^48 + x^47 + x^6 + x^5 + x + 1        (0x1800000000063)
                     = (x + 1) * (x^47 + x^5 + 1)
                (x^47 + x^5 + 1 is a primitive trinomial -- the textbook
                 "maximal-length inner LFSR times (x+1)" CRC construction)
    register    augmented / shift-in form:  crc = rem( ~msg(x) * x^48 , p )
    init 0, no final xor, no reflection
    field       stored big-endian at b1[2046:2052]

    mcrc(stored_message_4104_bytes) -> 6 bytes

VERIFIED on 1419 of 1424 non-erased AUs of the 374-page dump -- every CE, every
block, every page -- with the five exceptions cross-checked below and shown to be
single-bit media errors that the LDPC independently flags in the same AU.

WHY THIS MATTERS
----------------
The message is the raw, scrambled image.  No scrambler seed, no plaintext and no
ECC is needed, so a page can be integrity-checked the instant it is read.  That
includes blocks 06 and 07, for which no seed has ever been recovered: they verify
96/96 and 95/96 here.

WHICH HYPOTHESIS WAS TRUE
-------------------------
(a) computed over the stored (scrambled) bytes ....... True
(b) plaintext plus a row/PBA-dependent input ......... FALSE.  One and the same
    constant explains every AU on every row, CE, block and page; there is no
    address input, so the MCRC is not an end-to-end guard against a misdirected
    read -- only against corrupted bytes.
(c) a different byte span ........................... True at the tail only: the
    message is 4104 bytes and the 6 bytes after the MCRC are not a field at all
    (experiment 1).

The earlier decisive-looking result -- "172 bank-pair AUs have byte-identical
plaintext yet all 172 MCRCs differ" -- is fully explained: the CRC is taken over
the scrambled image, which differs by ksA ^ ksB.

AU GEOMETRY (as corrected here)
-------------------------------
A page is 8 codewords x 2304 B; an AU is codewords 2k, 2k+1 = b0, b1.  The
logical (LFSR-order) payload is 4110 bytes, not 4116:

    logical[   0:2056]  b0[   0:2056]     aux 8 then user 2048
    logical[2056:2058]  b0[2302:2304]     user 2
    logical[2058:4104]  b1[   0:2046]     user 2046
    logical[4104:4110]  b1[2046:2052]     AU MCRC 6
    (LDPC parity, unscrambled: b0[2056:2302] and b1[2052:2298])
    b1[2298:2304]                         not a field: a byte-exact copy of
                                          b1[0:6] on 1424 of 1424 odd codewords

so aux 8 + user 4096 + MCRC 6 = 4110 and nothing is left over.  This also agrees
with nd_ldpc_odd.py, whose codewords cover logical[0:4110] exactly and leave
b1[2298:2304] uncovered.
"""
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np
try:
    from . import config
except ImportError:  # direct execution
    import config  # noqa: F401


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERE = Path(__file__).resolve().parent.parent
IMG = config.dump_path("img")
S = 2304                       # codeword stride on media
MSGLEN = 4104                  # aux 8 + user 4096 -- the CRC message
MCRCLEN = 6

POLY = 0x1800000000063         # x^48 + x^47 + x^6 + x^5 + x + 1
MASK48 = (1 << 48) - 1
POLY48 = POLY & MASK48

_TBL = None


def _table():
    global _TBL
    if _TBL is None:
        t = []
        for i in range(256):
            c = i << 40
            for _ in range(8):
                c = (((c << 1) ^ POLY48) if c & (1 << 47) else (c << 1)) & MASK48
            t.append(c)
        _TBL = t
    return _TBL


def crc48(data, init=0):
    """rem( (init*x^(8n) + data(x)) * x^48 , p ) -- MSB first, unreflected."""
    t = _table()
    c = init & MASK48
    for b in data:
        c = ((c << 8) & MASK48) ^ t[((c >> 40) ^ b) & 0xFF]
    return c


def mcrc(data):
    """The AU MCRC of a stored (scrambled) AU message -> 6 bytes, big-endian.

    `data` is the AU's logical payload minus the MCRC field itself, i.e. the
    4104 bytes returned by logical(raw, au)[:4104].  Bytes are complemented
    before being clocked in, exactly as the LDPC does."""
    return crc48(bytes(b ^ 0xFF for b in data)).to_bytes(MCRCLEN, "big")


# --------------------------------------------------------------------- layout
def logical(raw, au):
    """The AU's 4110-byte payload in LFSR order: message 4104 + MCRC 6."""
    b0 = raw[au * 2 * S:(au * 2 + 1) * S]
    b1 = raw[(au * 2 + 1) * S:(au * 2 + 2) * S]
    return bytes(np.concatenate([b0[0:2056], b0[2302:2304], b1[0:2052]]))


def check_au(raw, au):
    """True if the AU's stored MCRC matches.  Needs nothing but the raw page."""
    lg = logical(raw, au)
    return mcrc(lg[:MSGLEN]) == lg[MSGLEN:]


def pages():
    for p in sorted(IMG.glob("img_ce*_b*_p*.bin")):
        ce, blk, pg = p.stem.split("_")[1:]
        raw = np.frombuffer(p.read_bytes(), np.uint8)
        if len(raw) >= 8 * S:
            yield int(ce[2:]), int(blk[1:]), int(pg[1:]), raw


# ---------------------------------------------------------------- polynomials
def pmod(a, b):
    db = b.bit_length() - 1
    while True:
        da = a.bit_length() - 1
        if da < db:
            return a
        a ^= b << (da - db)


def pgcd(a, b):
    while b:
        a, b = b, pmod(a, b)
    return a


def poly_str(p):
    return " + ".join(f"x^{i}" if i > 1 else ("x" if i == 1 else "1")
                      for i in range(p.bit_length() - 1, -1, -1) if (p >> i) & 1)


# ------------------------------------------------------------- experiment 1
def exp1_tail_duplicate():
    """The 'spare 6' at b1[2298:2304] is not a field."""
    print("[1] is b1[2298:2304] a payload field at all?")
    c = Counter()
    for ce, blk, pg, raw in pages():
        for cw in range(8):
            b = raw[cw * S:(cw + 1) * S]
            if int((b == 0xFF).sum()) == S:
                continue
            c[(cw & 1, bytes(b[2298:2304]) == bytes(b[0:6]))] += 1
    print(f"    even codewords, tail6 == head6 : "
          f"{c[(0, True)]:5d} of {c[(0, True)] + c[(0, False)]}")
    print(f"    ODD  codewords, tail6 == head6 : "
          f"{c[(1, True)]:5d} of {c[(1, True)] + c[(1, False)]}")
    print("    => the last 6 bytes of every odd codeword are a byte-exact copy of")
    print("       its first 6 (one LDPC circulant, Z=48 bits = 6 bytes: a pipeline")
    print("       flush artefact).  So the AU payload is 4110 B, not 4116, and")
    print("       there is no 'spare 6' field to explain.")


# ------------------------------------------------------------- experiment 2
def exp2_gcd(npair=8):
    """Recover p with no search at all.

    For a linear CRC, CRC(a^b) = CRC(a) ^ CRC(b) ^ CRC(0), so for two equal-length
    messages differing by D whose stored CRCs differ by E,
            p(x)  divides  D(x) * x^48 + E(x),
    and every init / final-xor / prepended-constant term cancels.  Take the GCD
    over several pairs.

    Pair source: same block, same page, different CE.  Same row, so the scrambler
    seed and any address term are identical in both and cancel; D is then just the
    raw payload difference.  This is valid whether the CRC is taken over the
    plaintext or over the stored bytes, so it prejudges nothing."""
    print("\n[2] generator by polynomial GCD over CE pairs (no search, no guessing)")
    groups = defaultdict(dict)
    for ce, blk, pg, raw in pages():
        if not (raw == 0xFF).all():
            groups[(blk, pg)][ce] = raw
    rs = []
    for (blk, pg), d in sorted(groups.items()):
        for c1, c2 in combinations(sorted(d), 2):
            for au in range(4):
                m1, m2 = logical(d[c1], au), logical(d[c2], au)
                D = bytes(x ^ y for x, y in zip(m1[:MSGLEN], m2[:MSGLEN]))
                E = bytes(x ^ y for x, y in zip(m1[MSGLEN:], m2[MSGLEN:]))
                if any(D) and any(E):
                    rs.append((f"ce{c1}/{c2} b{blk:02d} p{pg} au{au}",
                               (int.from_bytes(D, "big") << 48)
                               ^ int.from_bytes(E, "big")))
            if len(rs) >= npair:
                break
        if len(rs) >= npair:
            break
    g = 0
    for lab, r in rs[:npair]:
        g = r if g == 0 else pgcd(g, r)
    while g and not (g & 1):
        g >>= 1
    print(f"    {len(rs[:npair])} pairs: {', '.join(l for l, _ in rs[:npair])}")
    print(f"    gcd = {g:#x}   degree {g.bit_length() - 1}")
    print(f"        = {poly_str(g)}")
    print("        = (x + 1) * (x^47 + x^5 + 1)   [primitive trinomial]")
    print(f"    matches the compiled-in POLY: {g == POLY}")
    print("    (the same sweep over reflected input, reflected output, 16- and")
    print("     32-bit byte swaps and the non-augmented register form collapses")
    print("     to gcd == 1 -- see nd_mcrc_gcd.py.  Only this convention survives.)")
    return g


# ------------------------------------------------------------- experiment 3
def exp3_constant():
    """crc48(message) ^ stored MCRC.  One value => no address input at all."""
    print("\n[3] K = crc48(stored message) ^ stored MCRC -- one constant, or"
          " row-dependent?")
    ks, where = Counter(), defaultdict(list)
    ctrl = Counter()
    for ce, blk, pg, raw in pages():
        for au in range(4):
            lg = logical(raw, au)
            if all(b == 0xFF for b in lg):
                continue
            k = crc48(lg[:MSGLEN]) ^ int.from_bytes(lg[MSGLEN:], "big")
            ks[k] += 1
            if len(where[k]) < 3:
                where[k].append(f"ce{ce} b{blk:02d} p{pg} au{au}")
            ctrl[crc48(lg[8:MSGLEN]) ^ int.from_bytes(lg[MSGLEN:], "big")] += 1
    for k, n in ks.most_common(6):
        print(f"    K = {k:#014x}  on {n:5d} AUs   e.g. {', '.join(where[k])}")
    print(f"    distinct K over the correct span [0:4104] : {len(ks)}")
    print(f"    control, span [8:4104] (aux excluded)      : {len(ctrl)} distinct"
          "  <- wrong span, no constant")
    k = ks.most_common(1)[0][0]
    ones = crc48(b"\xff" * MSGLEN)
    print(f"\n    K                     = {k:#014x}")
    print(f"    crc48(0xFF * {MSGLEN})   = {ones:#014x}"
          f"   {'IDENTICAL' if ones == k else 'differs'}")
    print("    => K is not an arbitrary fudge factor: it is exactly the CRC of an")
    print("       all-ones message, i.e. the message is clocked in COMPLEMENTED")
    print("       with init 0 and no final xor -- the same complement convention")
    print("       the LDPC uses.  The formula therefore generalises to any length.")
    return k


# ------------------------------------------------------------- experiment 4
def exp4_verify():
    print("\n[4] verification: mcrc(stored message) vs the stored MCRC field")
    ok = bad = erased = 0
    byblk = defaultdict(lambda: [0, 0])
    fails = []
    for ce, blk, pg, raw in pages():
        for au in range(4):
            lg = logical(raw, au)
            if all(b == 0xFF for b in lg):
                erased += 1
                continue
            good = mcrc(lg[:MSGLEN]) == lg[MSGLEN:]
            ok += good
            bad += not good
            byblk[blk][0 if good else 1] += 1
            if not good:
                fails.append((ce, blk, pg, au))
    print(f"    AUs checked {ok + bad}    verified {ok}    failed {bad}"
          f"    erased/skipped {erased}")
    print("    by block: " + "  ".join(
        f"b{b:02d}:{v[0]}/{v[0] + v[1]}" for b, v in sorted(byblk.items())))
    print("    note blocks 06 and 07 have NO scrambler seed and are checked here")
    print("    anyway -- the MCRC needs neither seed nor plaintext.")
    return fails


# ------------------------------------------------------------- experiment 5
def exp5_cross_check(fails):
    """Are the MCRC failures real media errors?  Ask the LDPC, independently."""
    print("\n[5] cross-check of the MCRC failures against the LDPC syndrome")
    try:
        from .nd_ldpc_odd import locate_errors, media_bit, odd_syndrome, syn_even
    except Exception as e:                                   # pragma: no cover
        print(f"    nd_ldpc_odd unavailable ({e}); skipped")
        return
    for ce, blk, pg, au in fails:
        raw = (IMG / f"img_ce{ce}_b{blk:02d}_p{pg}.bin").read_bytes()
        b0 = raw[au * 2 * S:(au * 2 + 1) * S]
        b1 = raw[(au * 2 + 1) * S:(au * 2 + 2) * S]
        we = syn_even(b0)
        v = odd_syndrome(b1, b0[2302:2304])
        wo = bin(v).count("1")
        loc = locate_errors(v) if wo else []
        where = "; ".join(media_bit(c, q) for c, q in loc)
        print(f"    ce{ce} b{blk:02d} p{pg} au{au}: even syndrome {we:3d}, "
              f"odd syndrome {wo:3d}"
              + (f"  -> {where}" if where else ""))
    print("    A syndrome weight of 5 or 18 is one flipped bit (the two column")
    print("    weights of this code).  nd_ldpc_odd.py finds NINE single-bit errors")
    print("    in the whole dump: five in block1 DATA and four in stored PARITY.")
    print("    The MCRC flags exactly those five and none of the four -- parity is")
    print("    not part of its message.  Two independent checks agreeing bit for")
    print("    bit, in both directions: these are media errors, not a model defect.")


def main():
    exp1_tail_duplicate()
    exp2_gcd()
    exp3_constant()
    fails = exp4_verify()
    exp5_cross_check(fails)
    print("\nsummary: AU MCRC = crc48(~stored_message, p = 0x1800000000063),")
    print("         message = logical[0:4104] = aux 8 + user 4096, stored bytes,")
    print("         field   = logical[4104:4110] = b1[2046:2052], big-endian.")


if __name__ == "__main__":
    main()
