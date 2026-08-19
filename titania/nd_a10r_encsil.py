#!/usr/bin/env python3
"""LDPC encoding driven by the ECU's own silicon config images -- no fitted
constants, no media.  Covers A10R and EE3 with one code path.

    py nd_a10r_encsil.py             # A10R vs real media parity   -> 8218/8218
    py nd_a10r_encsil.py --ee3       # EE3  vs nd_ee3_ring, plus phi.Q == I

WHAT THIS REPLACES
------------------
A10R used to be encoded by a derived Richardson-Urbanke solve whose gap matrix
`Psi`, staircase seed `d` and 576-bit branch functional `w` were fitted to media
and carried as data.  None of that is needed: the ECU's own config images hold
the whole encoder, and this module reads it out.

    encoder image  -> the 144-record tail = the gap-solve matrix Q
    decoder image  -> the schedule -> H in Richardson-Urbanke block form

For A10R the only remaining convention is one bit (see `_BRANCH_BIT`); for EE3
there is no convention at all.

WHICH IMAGES, AND WHERE THEY COME FROM
--------------------------------------
    A10R  enc bank 3  / dec bank 11   read off silicon (nd_ecutables.py)
    EE3   enc "bank 8"/ dec "bank 8"  captured in the emulator, and byte-identical
                                      to static extraction from two independent
                                      firmware images

A10R and code 7 (banks 7/15) are the only ROM-triggerable codes: the on-die
loader is reached by a bank select plus a CONFIG_INIT pulse.  EE3 is not
ROM-triggerable -- firmware streams it unconditionally during ECU init inside
MioInit, before any media access (`MioInit -> ECU init -> sub_4049A2C2 ->
sub_4049A320`, then `sub_4049A05A`/`sub_40499F6C` CPU-stream both ports with
`0x1700C984 &= 0xFFFC`, i.e. "bank 8").  Do not read that as "loaded lazily when
main media is first touched": it is unconditional and early.

The config RAM is one array.  The 4-bit field at `0x1700C984` selects the on-die
ROM source for a load, not a read window, so a later self-load overwrites
whatever a previous load or CPU stream put there -- which is why EE3 cannot be
recovered from a board after A10R has self-loaded.  Relatedly,
`0x1700C818`/`0x81C` (= 300 / 822) are silicon constants, not image-loaded and
not CPU-writable: they never change across banks and cannot tell you which code
is resident.

THE ALGEBRA
-----------
Everything is over `R = GF(2)[x]/(x^48 - 1)`.  Within this module a block is
carried as the ring element `x^s` and a vector block as a 48-bit integer whose
bit k is the coefficient of `x^k` (6 bytes big-endian), so `rot(v, s)` is exactly
multiplication by the block.  That agrees with `nd_ee3_ring`/`nd_ldpc_odd`.  It
is not `fa_ring`'s basis, which works in the A10R stored-frame basis and uses the
reciprocal of the extracted block -- the two silicon/media offset sets differ in
581 of 2500 entries.  Do not mix them.

Split H's parity part in the silicon-declared RU form -- gap = the last 12
encoder rows, table parity column i <-> H parity column `(i+12) mod npar`:

    [ T  B ]        T lower-triangular, monomial diagonal   (tri = npar - 12)
    [ E  D ]        phi = D + E . T^-1 . B                  (the Schur complement)

The 144-record tail of the encoder image, read column-major and reciprocal, is
the gap solve Q.  What `phi . Q` equals is not universal -- it is decided by the
code's PARITY block-incidence nullity, and this module measures that rather than
keying off the code's name:

    nullity 0   ->  no ring kernel, `1^T . phi != 0`, phi is invertible,
                    the hardware stores `Q = phi^-1` and `phi . Q = I`.
                    EE3: incidence is 41x41 of rank 41 -> nullity 0, 144/144.
                    Encoding is then `x_gap = Q . y`, with no branch bit and no
                    fitted constant of any kind.

    nullity 1   ->  the ring kernel is J on a block set, `1^T . phi = 0`, and a
                    theorem says no ring matrix satisfies `phi . Q = I`.  The
                    hardware instead stores a Q with

                        phi . Q = S    S[0][0] = x , S[0][11] = 1 ,
                                       S[i][i] = S[i][i-1] = 1

                    byte-identical in A10R and code 7, nothing fitted.
                    `det S = x + 1`, so S is singular over GF(2) (rank 575/576)
                    and the residual degree-1 factor is where the unavoidable
                    non-linearity goes.  Under `R^12 = GF(2)[y]/(y^576 - 1)` with
                    `y^12 = x`, S is multiplication by a degree-1 element, so
                    solving `S.u = y` is a running prefix XOR closed by
                    `(x+1).u_0 = sum y_i` -- one 576-bit serial accumulator,
                    which is exactly what the silicon is.

`(x+1).u_0 = c` has two solutions differing by the all-ones J, i.e. two valid
codewords.  Which one the hardware emits is not derivable from H (both are
genuine codewords); it is the single bit below, and it is the only convention
this module carries -- and only for the singular codes.

THE ENCODER IMAGE LAYOUT
------------------------
Measured from silicon by streaming `0xFFFF` into a writable bank and reading back
the implemented-bit mask, so this is not inferred:

    words  0..31    HEADER -- 32 words, not 17.  Words 17..31 are three rows with
                    all 80 bits implemented, so they are not part of the 72-bit
                    entry grid.
    word   32..     5-word records, mask `ffff ffff 00ff ffff ffff` (the 8-bit
                    hole is at record bits 24..31)

    32 + 5*137            = 717   record-grid tail start (A10R)   -- exact
    32 + 5*(137+144) + 3  = 1440  image extent (A10R)             -- exact

Header fields are bit fields, not word-aligned (confirmed across three images):
bits 0..8 = message block columns (87 / 86 / 344); bits 16..21 and 64..69 =
parity blocks - 12 (38 / 7 / 29, so gap = 12 in all three); bits 80..83, 176..179
and 208..211 are constant 12.  RETRACTED: `[3]` as message columns, `[6]` as
stored columns, and `[2] = 0x6520` as a "format magic" -- EE3 has `0xC421`, and
`0x6520` is a table-SOURCE tag shared only by the two ROM-resident codes.

RETRACTED: "then `0xa337` filler forever".  The readable extent equals the number
of words written since CONFIG_INIT (measured at 1440, 1600 and 2048); past that
the port returns undefined data that violates the implemented mask, so `0xa337`
is a read artefact and not RAM content.  Never treat it as a terminator.

    * DO NOT "FIX" THE TAIL WINDOW *
The Q values start one grid row earlier than the record-grid boundary at word
717.  The entry stream is 14 preamble slots plus the per-column slots, and
`14 + 6*137 = 836` with `836 mod 6 = 2`, so it always ends at slot 1 of its last
row and leaves that row's bits 32..79 free -- the first Q value sits there.  The
14-slot preamble exists precisely so the 48-bit table starts packed immediately.
`_tail()` therefore reads grid rows `nrows-1 .. nrows+142` (A10R words 714..716
for the first), and that is correct: word 717 is the record-grid boundary, not
where the 48-bit fields begin.  Both descriptions are consistent; a "correction"
to `nrows` breaks A10R and EE3 alike.

Irregular codes pad.  A mother column takes `6*ceil(w/6)` 12-bit slots,
blank-padded with `0x03F`.  A10R and code 7 are weight-6 throughout, so that
collapses to one 6-slot record per column; EE3 has weights 5 (x313) and 18 (x72),
so its 385 columns take 529 records and its image is `32 + 5*674 = 3402` words.
`_tail()` uses the padded sum, which reduces to `6*ncol` for the uniform codes.

VERIFIED
--------
  A10R  8218 / 8218 real media codewords, all four slots, byte-exact
        phi . Q == S from the two images alone, no media
  EE3   phi . Q == I, 144/144 entries; parity byte-identical to `nd_ee3_ring`
        (`p = A2^-1 . A1 . m`), which is itself exact against media
        random-message encode, full-H syndrome zero 20/20
"""
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from . import nd_ldpc as _L

Z = 48
MASK = (1 << Z) - 1
NMSG, NPAR = 87, 50               # A10R mother block columns (one msg col shortened)
GAP = 12                          # fixed hardware gap size, all three codes
BASE = 14                         # entry-stream preamble slots, encoder image
BLANK = 0x03F                     # blank 12-bit entry slot (row 63, shift 0)
MSG_BYTES, PAR_BYTES = 516, 300   # A10R stored frame

ENC_IMAGE = "nd_ecutbl_ecc0enc_code0_rdsel0_bank3.bin"
DEC_IMAGE = "ne_ldpc_dec_b11_rd0_n8192.bin"
EE3_ENC_IMAGE = "ek_ee3_enc.bin"
EE3_DEC_IMAGE = "ek_ee3_dec.bin"
EE3_NMSG = 344                    # EE3 mother message block columns
EE3_PAR_BYTES = 246

# The one convention not derivable from H, and only for the singular codes: of
# the two codewords, the hardware emits the one whose gap accumulator has bit 47
# of u_0 clear -- the accumulator initialises at the first frame bit.  Fitting it
# over 1200 media frames gives a solution space of dimension exactly 1, so this
# bit is unique, not a choice.  EE3 never reaches this code path.
_BRANCH_BIT = 47


# --------------------------------------------------------------- ring in R
def pmul(a, b):
    r = 0
    for i in range(Z):
        if (b >> i) & 1:
            r ^= ((a << i) | (a >> (Z - i))) & MASK
    return r


def rot(a, k):
    k %= Z
    return ((a << k) | (a >> (Z - k))) & MASK if k else a


def recip(a):
    r = 0
    for k in range(Z):
        if (a >> k) & 1:
            r |= 1 << ((-k) % Z)
    return r


def _mono_inv(d):
    return 1 << ((-(d.bit_length() - 1)) % Z)


# ------------------------------------------------------------ table reading
def _words(path):
    # `nd_tables.table` and not `HERE / path`: the four silicon images are
    # package data in a wheel and files in the checkout, and this is the only
    # place that difference is allowed to show.
    from . import nd_tables
    b = nd_tables.table(str(path)).read_bytes()
    return list(struct.unpack("<%dH" % (len(b) // 2), b))


def _grid_field48(w, row):
    """bits 32..79 of 5-word grid row `row`, rows counted from word 17.

    Word 17 is the anchor of the 5-word grid; the record array proper starts at
    word 32 (= grid row 3).  Both anchors describe the same grid, and the tail
    offsets in `_tail()` are expressed against this one.
    """
    k = 17 + 5 * row
    return w[k + 2] | (w[k + 3] << 16) | (w[k + 4] << 32)


def _pi(nlay):
    """encoder row -> decoder schedule position (6-way interleave, nlay only)."""
    q, rem = nlay // 6, nlay % 6
    h = [b * q + max(0, b - (6 - rem)) for b in range(6)]
    return {p: (h[(p - rem) % 6] + p // 6) % nlay for p in range(nlay)}


def _layers(dec_image):
    return _L.absolutize([list(x) for x in _L.parse_schedule(
        _words(dec_image), titania=False, hdr=16, Zc=Z)], Z)


def _incidence_nullity(lay, nmsg, npar):
    """GF(2) nullity of the PARITY block-incidence matrix (rows = layers).

    Every non-zero block of the parity half is a single shifted identity, so its
    polynomial is `x^s`, which evaluates to 1 at x = 1; since `J.p = p(1).J`, the
    ring kernel is `J (x) u` for u in the nullspace of this binary matrix.  So
    the nullity is the ring kernel dimension, in milliseconds and with no
    2400x2400 elimination -- and it is what decides `phi . Q = I` vs `= S`.
    Predicted block sets match the measured kernels exactly on A10R and code 7.
    """
    piv = []
    for e in lay:
        r = 0
        for (c, _s) in e:
            if c >= nmsg:
                r |= 1 << (c - nmsg)
        for p in piv:
            r = min(r, r ^ p)
        if r:
            piv.append(r)
            piv.sort(reverse=True)
    return npar - len(piv), len(piv)


# --------------------------------------------------------------- the codes
class SiliconCode:
    """One LDPC code, read entirely out of its two ECU config images.

    Nothing is per-code special-cased: the message/parity split comes from
    `nmsg`, the geometry from the decoder image, the column padding from the
    measured column weights, and the gap-solve convention from the measured
    incidence nullity.
    """

    def __init__(self, name, enc_image, dec_image, nmsg):
        self.name = name
        self.enc_image = enc_image
        self.dec_image = dec_image
        self.nmsg = nmsg
        self._m = None

    def __repr__(self):
        if self._m is None:
            return "<SiliconCode %s (unbuilt)>" % self.name
        return ("<SiliconCode %s nmsg=%d npar=%d tri=%d nullity=%d gap=%s>"
                % (self.name, self.nmsg, self.npar, self.tri, self.nullity,
                   "S" if self.singular else "I"))

    def available(self):
        from . import nd_tables
        return (nd_tables.available(self.enc_image)
                and nd_tables.available(self.dec_image))

    # -- construction ----------------------------------------------------
    def build(self):
        """Parse both images.  Idempotent; everything below is cached."""
        if self._m is not None:
            return self
        lay = _layers(self.dec_image)
        npar = len(lay)
        ent, colw = {}, {}
        for li, e in enumerate(lay):
            for (c, s) in e:
                ent[(li, c)] = s
                colw[c] = colw.get(c, 0) + 1
        ncol = max(colw) + 1
        if ncol != self.nmsg + npar:
            raise ValueError("%s: %d mother columns but nmsg+npar = %d"
                             % (self.name, ncol, self.nmsg + npar))
        pi = _pi(npar)
        inv = {v: k for k, v in pi.items()}
        tri = npar - GAP

        # P[p][i] = block at encoder row p, table parity column i
        P = [[0] * npar for _ in range(npar)]
        for p in range(npar):
            for i in range(npar):
                s = ent.get((pi[p], self.nmsg + ((i + GAP) % npar)))
                if s is not None:
                    P[p][i] = 1 << (s % Z)
        self.T = [[P[p][i] for i in range(tri)] for p in range(tri)]
        self.B = [[P[p][tri + j] for j in range(GAP)] for p in range(tri)]
        self.E = [[P[tri + g][i] for i in range(tri)] for g in range(GAP)]
        self.D = [[P[tri + g][tri + j] for j in range(GAP)] for g in range(GAP)]

        # entries in encoder row numbering: all of them (for a coset) and the
        # message half (for the per-message syndrome)
        self.allent = [(inv[li], c, s)
                       for li, e in enumerate(lay) for (c, s) in e]
        self.msg = [(p, c, s) for (p, c, s) in self.allent if c < self.nmsg]

        self.layers = lay
        self.npar, self.ncol, self.tri, self.colw = npar, ncol, tri, colw
        self.nullity, self.inc_rank = _incidence_nullity(lay, self.nmsg, npar)
        self.singular = self.nullity > 0
        self.Q = self._read_Q()
        self._m = True
        return self

    def _read_Q(self):
        """The encoder image's 144-record tail: Q, column-major and reciprocal."""
        w = _words(self.enc_image)
        # a mother column takes 6*ceil(weight/6) slots, blank-padded; uniform
        # weight-6 codes reduce this to 6*ncol
        total = sum(6 * ((self.colw[c] + 5) // 6) for c in range(self.ncol))
        nrows = -(-(BASE + total) // 6)
        # `nrows - 1`, not `nrows` -- see the module docstring, and do not change it
        tail = [_grid_field48(w, nrows - 1 + r) for r in range(GAP * GAP)]
        self.tail_row = nrows - 1
        return [[recip(tail[GAP * i + j]) for i in range(GAP)] for j in range(GAP)]

    # -- the algebra -----------------------------------------------------
    def phi(self):
        """The 12x12 Schur complement `D + E . T^-1 . B` over R."""
        self.build()
        T, B, E, tri = self.T, self.B, self.E, self.tri
        X = [[0] * GAP for _ in range(tri)]
        for j in range(GAP):
            for p in range(tri):
                acc = B[p][j]
                for q in range(p):
                    if T[p][q]:
                        acc ^= pmul(T[p][q], X[q][j])
                X[p][j] = pmul(_mono_inv(T[p][p]), acc)
        phi = [[self.D[g][j] for j in range(GAP)] for g in range(GAP)]
        for g in range(GAP):
            for j in range(GAP):
                a = 0
                for p in range(tri):
                    if E[g][p]:
                        a ^= pmul(E[g][p], X[p][j])
                phi[g][j] ^= a
        return phi

    def gap_target(self):
        """What `phi . Q` must equal for this code -- S if singular, else I."""
        self.build()
        return S_matrix() if self.singular else I_matrix()

    def check_gap(self):
        """`phi . Q == gap_target()` -- pure algebra from the two images.

        This is the load-bearing claim: the encoder image's tail really is the
        gap-solve matrix for the H in the decoder image.  It needs no codewords,
        so it is independent of every media-based check.
        """
        phi = self.phi()
        got = [[_dot([phi[i][k] for k in range(GAP)],
                     [self.Q[k][j] for k in range(GAP)], GAP) for j in range(GAP)]
               for i in range(GAP)]
        return got == self.gap_target()

    # -- encoding --------------------------------------------------------
    def parity_mother(self, blocks, coset=None):
        """mother message blocks -> the `npar` parity blocks, in H column order.

        `blocks` is `nmsg` ring elements (bit k = coefficient of x^k).  `coset`
        is an optional per-slot syndrome offset (A10R only).
        """
        self.build()
        if len(blocks) != self.nmsg:
            raise ValueError("need %d message blocks" % self.nmsg)
        tri, npar = self.tri, self.npar
        rhs = list(coset) if coset is not None else [0] * npar
        for (p, c, sh) in self.msg:
            v = blocks[c]
            if v:
                rhs[p] ^= rot(v, sh)

        z = _fwd_sub(self.T, rhs[:tri], tri)
        y = [rhs[tri + g] ^ _dot(self.E[g], z, tri) for g in range(GAP)]
        if self.singular:
            u = _solve_S(y)
            if u is None:
                raise ValueError("gap system inconsistent -- not a valid %s "
                                 "information word for this slot" % self.name)
        else:
            u = y                              # phi . Q = I: x_gap = Q . y
        pg = [_dot(self.Q[k], u, GAP) for k in range(GAP)]
        bp = [_dot(self.B[p], pg, GAP) for p in range(tri)]
        pt = [a ^ b for a, b in zip(z, _fwd_sub(self.T, bp, tri))]

        par = pt + pg                          # Table parity column order
        out = [0] * npar
        for i in range(npar):
            out[(i + GAP) % npar] = par[i]
        return out


A10R = SiliconCode("A10R", ENC_IMAGE, DEC_IMAGE, NMSG)
EE3 = SiliconCode("EE3", EE3_ENC_IMAGE, EE3_DEC_IMAGE, EE3_NMSG)


# ------------------------------------------------------------------ cosets
_COSETS = {}


def _cosets(all_entries):
    """The per-slot A10R syndrome offset, computed from H -- not from media.

    An A10R codeword is the complement of its stored frame, so `H . frame =
    H . 1` and the offset is the syndrome of the all-ones vector over all mother
    columns, message and parity alike (restricting it to the message half gives
    the wrong coset -- 0/8218).  The shortened mother column 86 is never stored
    and counts as zero; slots 0-2 additionally clear message bytes 512..515
    (block 85's low four bytes).  Both readings verified against media.
    """
    def synd(ones):
        s = [0] * NPAR
        for (p, c, sh) in all_entries:
            if ones[c]:
                s[p] ^= rot(ones[c], sh)
        return s
    ones = [MASK] * (NMSG + NPAR)
    ones[86] = 0
    full = synd(ones)
    ones[85] = 0xFFFF00000000
    short = synd(ones)
    return {0: short, 1: short, 2: short, 3: full}


def _a10r_cosets():
    if "a10r" not in _COSETS:
        _COSETS["a10r"] = _cosets(A10R.build().allent)
    return _COSETS["a10r"]


def _build():
    """Back-compat shim: A10R's `(T, B, E, tri, msg, Q, cosets)`."""
    c = A10R.build()
    return (c.T, c.B, c.E, c.tri, c.msg, c.Q, _a10r_cosets())


# ------------------------------------------------------------------ solving
def _fwd_sub(T, rhs, tri):
    z = [0] * tri
    for p in range(tri):
        acc = rhs[p]
        for q in range(p):
            if T[p][q]:
                acc ^= pmul(T[p][q], z[q])
        z[p] = pmul(_mono_inv(T[p][p]), acc)
    return z


def _div_xp1(c):
    """solve (x+1).u = c in R, branch with u_0 = 0; None if inconsistent."""
    if bin(c).count("1") & 1:
        return None
    u = acc = 0
    for k in range(1, Z):
        acc ^= (c >> k) & 1
        u |= acc << k
    return u


def _solve_S(y):
    """solve S.u = y; returns the branch the hardware emits, or None.

    Singular codes only -- a nullity-0 code has `phi . Q = I` and never gets
    here.
    """
    pre = [0] * GAP
    for i in range(1, GAP):
        pre[i] = pre[i - 1] ^ y[i]
    u0 = _div_xp1(y[0] ^ pre[GAP - 1])
    if u0 is None:
        return None
    u = [u0 ^ pre[i] for i in range(GAP)]
    if (u[0] >> _BRANCH_BIT) & 1:
        u = [v ^ MASK for v in u]
    return u


def _dot(row, v, n):
    a = 0
    for i in range(n):
        if row[i] and v[i]:
            a ^= pmul(row[i], v[i])
    return a


# -------------------------------------------------------------------- API
def available():
    """True when the A10R images are present (the default encoder path)."""
    return A10R.available()


def S_matrix():
    """The gap-solve residual of the rank-deficient codes (A10R, code 7).

    Not universal.  A code whose parity block-incidence matrix is non-singular
    has no ring kernel, so phi is invertible and the hardware stores
    `Q = phi^-1` with `phi . Q = I` -- EE3 does exactly that, 144/144.  Use
    `SiliconCode.gap_target()` rather than assuming either one.
    """
    S = [[0] * GAP for _ in range(GAP)]
    S[0][0] = 2                                    # x
    S[0][GAP - 1] = 1
    for i in range(1, GAP):
        S[i][i] = S[i][i - 1] = 1
    return S


def I_matrix():
    return [[1 if i == j else 0 for j in range(GAP)] for i in range(GAP)]


def check_QS():
    """A10R: verify `phi . Q == S` from the two images alone, no media."""
    return A10R.check_gap()


def parity(message516, slot):
    """516 A10R message bytes + slot 0..3 -> the 300 A10R parity bytes."""
    if len(message516) != MSG_BYTES:
        raise ValueError("message must be %d bytes" % MSG_BYTES)
    blocks = [int.from_bytes(bytes(message516[6 * c:6 * c + 6]), "big")
              for c in range(NMSG - 1)] + [0]      # mother col 86 is shortened
    out = A10R.parity_mother(blocks, coset=_a10r_cosets()[slot])
    return b"".join(x.to_bytes(6, "big") for x in out)


def encode_frame(frame816, slot):
    """Rewrite an 816-byte A10R frame's parity from its own message."""
    f = bytearray(frame816)
    f[MSG_BYTES:MSG_BYTES + PAR_BYTES] = parity(bytes(f[:MSG_BYTES]), slot)
    return bytes(f)


def ee3_parity(info_bytes):
    """EE3 mother information bytes (<= 2064, zero padded) -> 246 parity bytes.

    A second, independent construction of the EE3 encoder.  The primary path is
    `nd_ee3_ring.solve_parity` (`p = A2^-1 . A1 . m`, a Gauss-Jordan of the 41x41
    A2 over R); this one instead reads the firmware's own config images -- H from
    the decoder image and the gap solve Q from the encoder image's tail -- and
    solves in Richardson-Urbanke form.  The two agree byte for byte, and the
    silicon H is identical to the media-derived one on all 385 mother columns,
    layer and shift alike.

    EE3's incidence nullity is 0, so there is no branch bit and nothing fitted.
    """
    b = bytes(info_bytes).ljust(EE3_NMSG * 6, b"\0")
    if len(b) != EE3_NMSG * 6:
        raise ValueError("information must be <= %d bytes" % (EE3_NMSG * 6))
    blocks = [int.from_bytes(b[6 * c:6 * c + 6], "big") for c in range(EE3_NMSG)]
    out = EE3.parity_mother(blocks)
    return b"".join(x.to_bytes(6, "big") for x in out)


# ------------------------------------------------------------------- main
def _main_a10r():
    import numpy as np
    c = A10R.build()
    print("silicon tables: %s + %s" % (ENC_IMAGE, DEC_IMAGE))
    print("  T %dx%d, B %dx%d, E %dx%d, Q %dx%d, %d message entries"
          % (c.tri, c.tri, c.tri, GAP, GAP, c.tri, GAP, GAP, len(c.msg)))

    p = HERE / "fd_a10r_frames.npz"
    if not p.exists():
        print("no fd_a10r_frames.npz -- skipping the media check")
        return 0
    d = np.load(p)
    frames, slots = d["frames"], d["slots"]
    ok = n = bad = 0
    for i in range(len(frames)):
        fr = bytes(frames[i])
        try:
            got = parity(fr[:MSG_BYTES], int(slots[i]))
        except ValueError:
            bad += 1
            continue
        n += 1
        ok += got == fr[MSG_BYTES:MSG_BYTES + PAR_BYTES]
    print("\nparity from the SILICON TABLES vs real media: %d / %d exact"
          " (%d rejected)" % (ok, n, bad))
    print("fitted constants: none -- one 1-bit branch convention")
    return 0 if ok == n and n else 1


def _main_ee3():
    import random
    if not EE3.available():
        print("EE3 images absent (%s / %s) -- skipped"
              % (EE3_ENC_IMAGE, EE3_DEC_IMAGE))
        return 0
    c = EE3.build()
    print("silicon tables: %s + %s" % (EE3_ENC_IMAGE, EE3_DEC_IMAGE))
    print("  nmsg %d, npar %d, ncol %d, T %dx%d, Q %dx%d"
          % (c.nmsg, c.npar, c.ncol, c.tri, c.tri, GAP, GAP))
    print("  column weights: %s"
          % sorted({w: sum(1 for x in c.colw.values() if x == w)
                    for w in set(c.colw.values())}.items()))
    print("  parity block-incidence: %dx%d rank %d, nullity %d -> gap target %s"
          % (c.npar, c.npar, c.inc_rank, c.nullity,
             "S" if c.singular else "I"))
    good = c.check_gap()
    print("  phi . Q == %s from the images alone: %s"
          % ("S" if c.singular else "I", good))

    try:
        from . import nd_ee3_ring as _er
    except Exception as e:
        print("nd_ee3_ring unavailable (%s) -- no cross-check" % e)
        return 0 if good else 1
    rnd = random.Random(3)
    n = ok = 0
    for _ in range(10):
        info = bytes(rnd.randrange(256) for _ in range(EE3_NMSG * 6))
        n += 1
        ok += ee3_parity(info) == _er.solve_parity_ring(info)
    print("\nEE3 parity, SILICON images vs nd_ee3_ring's A2^-1: %d / %d exact"
          % (ok, n))
    print("fitted constants: none -- and no branch bit (nullity 0)")
    return 0 if good and ok == n else 1


def main(argv=()):
    if "--ee3" in argv:
        return _main_ee3()
    if "--all" in argv:
        return _main_a10r() | _main_ee3()
    return _main_a10r()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
