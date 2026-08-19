// tnldpc -- the Titania EE3 hard-decision LDPC corrector, in Rust.
//
// WHY THIS EXISTS
// ---------------
// `DumpDevice.adjudicate()` decides a contested page on CONTENT, and the
// strongest content evidence is how many corrections the LDPC decoder needs.
// That means running the min-sum decoder over 8 codewords per candidate, and it
// dominated everything: 62.3 s for 19 conflicts, of which 55.7 s was
// `nd_ldpc_fix.Decoder.minsum` and 8.9 s the syndrome.  It is not I/O and it is
// not interpreter overhead that a tighter loop body would remove -- it is
// 137 328 edges x up to 80 iterations x 2 candidates x 8 codewords of genuine
// arithmetic, with numpy allocating a full (41, 70, 48) temporary for each of
// `partition`, `abs`, `argmin`, `where` and `prod` on every iteration.
//
// EXACTNESS IS THE WHOLE REQUIREMENT
// ----------------------------------
// This is not a reimplementation "in the spirit of" the Python.  Adjudication
// COMPARES CORRECTION COUNTS between two candidate reads, so a decoder that
// reached the same codeword by a different path and reported a different
// iteration count would change verdicts.  Every operation is in the same order
// and the same precision as `nd_ldpc_fix.py`.  The traps:
//
//   * the check-node update is an f32 chain -- numpy 2.x (NEP 50) treats the
//     Python float `alpha` as WEAK and casts it to the array's f32, so
//     `alpha * prod * sg * mag` never widens to f64;
//   * the variable-node accumulation is `np.bincount(..., weights=R)`, which
//     accumulates in **f64** in raveled edge order and is only then cast to
//     f32.  Accumulating in f32 here would drift within a few iterations;
//   * `np.argmin` returns the FIRST minimum and `np.partition(kth=1)` returns
//     the second smallest WITH multiplicity.  A strict `<` in the running
//     two-smallest scan reproduces both; a `<=` would not.
//
// THREE DEPARTURES FROM THE NUMPY SHAPE, EACH PROVEN NEUTRAL
// ----------------------------------------------------------
//  1. The numpy decoder pads every layer out to W = max circulants and gives
//     the padding |Q| = 1e3 so it can never be a minimum, and sign +1 so it
//     cannot change the sign product.  Here the padding is simply not
//     iterated.  Same values, same order, no padding arithmetic.
//  2. The per-iteration syndrome is FUSED into the check-node pass.  numpy
//     computes `v = (tot < 0)` and then gathers `v[idx]`; this gathers
//     `tot[idx]` once, for both the syndrome bit (`< 0`) and Q.  One gather
//     pass instead of two, from the identical index array -- the edge set and
//     the syndrome's index set are literally the same `idx`.
//  3. The accumulator skips the padding's `+= 0.0`.  Adding +0.0 cannot change
//     an f64 that is never -0.0, and this one never is: it starts at +0.0 and
//     `+0.0 + -0.0 == +0.0`.  The order of every NON-zero add is unchanged.
//
// THE RUN DECOMPOSITION -- WHY THIS IS NOT A GATHER KERNEL
// -------------------------------------------------------
// H is quasi-cyclic: `nd_ldpc.Code` builds
//
//     idx[c][r] = col[c] * Z + (r + shift[c]) % Z
//
// so the Z edges of a circulant do not touch Z scattered bits -- they touch ONE
// contiguous Z-wide block column, cyclically rotated.  Read in `r` order that
// is two contiguous runs, `tot[cb+shift .. cb+Z]` then `tot[cb .. cb+shift]`.
//
// So `Code` precomputes, per circulant, the maximal runs of consecutive `idx`
// (`run_src`, `run_dst`, `run_len`).  Every hot loop then walks runs instead of
// indices and both sides are contiguous: the source in `tot`/`acc`/`v` and the
// destination in `q`/`r`/`m1`/`m2`/`am`/`prod`.  The gather and the scatter are
// gone -- and being contiguous, straight-line and non-aliasing, LLVM
// auto-vectorises them.
//
// This is a pure re-INDEXING.  It is general: the runs are found by scanning
// `idx`, so a non-quasi-cyclic code simply yields Z runs of length 1 and the
// identical kernel degenerates to the old element-at-a-time gather.  There is
// no second decoder to drift, and no code-shape assumption that could be
// silently wrong.
//
// WHY THE VECTORISATION IS EXACT, WHICH IS NOT OBVIOUS FOR THE f64 ACCUMULATOR
// ---------------------------------------------------------------------------
// Vectorising across `r` reassociates NOTHING: lane r is an independent copy of
// the scalar computation, in the same order and precision.  min/max, the +-1.0
// sign product and the elementwise transforms are all per-lane.
//
// The one that needs an argument is `acc[idx] += val`, an f64 running sum whose
// order numpy fixes.  It stays exact because within ONE circulant the Z indices
// are a permutation of one block column, hence DISTINCT: each accumulator takes
// exactly one add per circulant, so vectorising inside a circulant cannot
// reorder any accumulator's own sequence of adds.  Circulants are still walked
// in raveled edge order, so the sequence each accumulator sees is byte for byte
// the sequence `np.bincount` sees.  (This is why the accumulation is vectorised
// per-run and NOT hoisted across circulants: across circulants the indices do
// collide, and there the order is load-bearing.)
//
// NOT DONE, DELIBERATELY: no `-ffast-math`, no FP contraction, and no
// reassociated or horizontal reduction over the check-node f32 chain or the f64
// accumulation.  Nor any BAKED-IN `target-cpu`: wider vectors are worth 1.5x
// here and are taken, but through runtime detection (see the dispatch below),
// because a binary compiled for this machine would SIGILL on an older one and
// the crate is built from a checkout by whoever is holding the dump.
//
// PROTOCOL
// --------
// A persistent worker on stdin/stdout, because a decode is a few ms and
// starting this process and handing it the code measures ~12 ms (73 ms cold).
// That cost is paid ONCE -- `nd_ldpc_rs` holds the worker in a module
// singleton -- not per batch.  Little-endian throughout.
//
//   init:      "TNLD" u32 z u32 mb u32 nb u32 ncirc u32 nbits
//              i32 idx[ncirc * z]        bit index per (circulant, check row)
//              u32 lens[mb]              circulants per layer
//              u8  free[nbits]           1 = a received bit, 0 = pinned zero
//   request:   u8 op    1 = decode one, 2 = decode a batch,
//                       3 = syndrome weights, 4 = AU MCRCs, 0 = quit
//              (ops 2 and 3) u32 count
//              u8 v0[count][nbits]
//              (op 4)        u32 count, u32 msglen, u8 width
//              u8 msg[count][msglen]
//   response:  op 1/2, per item: u8 converged, u32 iters, u8 stage, u8 v[nbits]
//              stage 0 = already clean, 1 = bit flipping, 2 = min-sum, 3 = fail
//              op 3, per item: u32 weight
//              op 4, per item: u8 crc[width], big-endian
//
// OP 4 IS THE OTHER HALF OF A PAGE'S VERDICT.  The map asks two independent
// questions of a page -- "is it a codeword" (op 3) and "does its MCRC check" --
// and the second was the last pure-Python loop on the path: `crc48` clocks 4104
// bytes a byte at a time through a 48-bit register, 1.04 ms an AU, 4.1 ms a
// page, and being interpreter-bound no thread count helped it.  The message is
// COMPLEMENTED here, as `titania_nand.au_mcrc` does, because that is part of
// the definition rather than something a caller should have to remember.
//
// The de-interleave (`au_logical`) and the erased test stay in Python: they
// measured 2 us each against the CRC's 1041, so moving them would duplicate
// format knowledge for no gain.
//
// OP 3 EXISTS BECAUSE MOST PAGES ARE CLEAN.  The viewer's ECC map asks "is this
// page a codeword" of every page it colours, and only decodes the ones that are
// not.  That question is `H . v`, which is the first thing a decode does anyway
// -- but the caller was computing it in Python as a 1968-bit integer built from
// ~3850 rotated chunks per codeword: 10.7 ms of a 17.5 ms page, and the single
// largest cost in the map.  Here it is the same `syndrome_weight` the decoder
// uses on its first iteration, so the two can never disagree about whether a
// page needed correcting.
//
// A BATCH IS DECODED IN PARALLEL, over a pool of at most
// `available_parallelism()` threads.  Batches are not four codewords any more:
// `nd_deepparity.decode_pages` hands over every even codeword of every page in
// a request at once -- 48 for a 12-page block scan, hundreds for a sweep step
// -- and then every odd container (the odd container reads two bytes out of the
// CORRECTED even block, so the two PHASES stay ordered; the jobs within a phase
// need not).
//
// The pool replaced one-thread-per-job.  Per-job threads were fine at four and
// wrong at four hundred: each job owns a `Scratch` of ~1.4 MB, so the working
// set grew with the batch instead of with the machine, and 400 of them is
// 560 MB and 400 threads contending for 64 cores.  A thread here takes jobs
// from a shared counter and REUSES its scratch, so the memory is bounded by the
// core count and the per-job allocation is paid once per thread rather than
// once per job.  Ordering is unaffected: results are written into the caller's
// slot by index, not in completion order.
use std::io::{self, Read, Write};
use std::sync::Mutex;

const PIN: f32 = 50.0;
const ALPHA: f32 = 0.45;
const BITFLIP_ITERS: u32 = 12;
const MINSUM_ITERS: u32 = 80;

struct Code {
    z: usize,
    mb: usize,
    ncirc: usize,
    nbits: usize,
    starts: Vec<usize>,
    lens: Vec<usize>,
    lay: Vec<usize>,     // circulant -> layer
    dv: Vec<u8>,         // column degree (see Code::new)
    free: Vec<u8>,
    // Runs of consecutive `idx` within a circulant.  `run_off[c]..run_off[c+1]`
    // are circulant c's runs; run t copies/updates `run_len[t]` elements
    // starting at bit `run_src[t]` and at check row `run_dst[t]`.
    run_off: Vec<u32>,
    run_src: Vec<u32>,
    run_dst: Vec<u32>,
    run_len: Vec<u32>,
}

impl Code {
    fn new(z: usize, mb: usize, ncirc: usize, nbits: usize, idx: Vec<i32>,
           lens: Vec<usize>, free: Vec<u8>) -> Code {
        let mut starts = Vec::with_capacity(mb);
        let mut acc = 0usize;
        for &l in &lens {
            starts.push(acc);
            acc += l;
        }
        assert_eq!(acc, ncirc, "lens do not sum to the circulant count");
        assert!(lens.iter().all(|&l| l >= 2),
                "a layer with fewer than 2 circulants would make the padding \
                 visible to the two-smallest scan");
        let mut lay = Vec::with_capacity(ncirc);
        for (i, &l) in lens.iter().enumerate() {
            for _ in 0..l {
                lay.push(i);
            }
        }
        // Column degree.  Stage A only ever compares `2 * unsat[j]` against it
        // and `unsat[j] <= dv[j]`, so u8 holds both for any dv < 128 -- and dv
        // cannot exceed the number of layers (no block column repeats within a
        // layer), which is 41 here.  Keeping the counters one byte wide puts
        // the whole `unsat` array in L1 and quadruples the SIMD width of the
        // accumulation, which is stage A's inner loop.  If some future code
        // really had a column in 128+ layers this assert fires before the
        // worker acknowledges the code and `nd_ldpc_rs` falls back to numpy --
        // slower, identical answers.
        let mut dv = vec![0u8; nbits];
        for &b in &idx {
            assert!((b as usize) < nbits, "idx out of range");
            let d = &mut dv[b as usize];
            assert!(*d < 127, "column degree >= 127 would overflow 2*unsat");
            *d += 1;
        }
        // -- the run decomposition (see the header).  Purely a re-indexing of
        //    `idx`: maximal stretches of consecutive check rows whose bit
        //    indices are also consecutive.  Quasi-cyclic -> 1 or 2 runs per
        //    circulant; anything else -> shorter runs, same answers.
        let mut run_off = Vec::with_capacity(ncirc + 1);
        let (mut run_src, mut run_dst, mut run_len) =
            (Vec::new(), Vec::new(), Vec::new());
        for c in 0..ncirc {
            run_off.push(run_src.len() as u32);
            let base = c * z;
            let mut r = 0usize;
            while r < z {
                let s0 = idx[base + r];
                let mut l = 1usize;
                while r + l < z && idx[base + r + l] == s0 + l as i32 {
                    l += 1;
                }
                run_src.push(s0 as u32);
                run_dst.push(r as u32);
                run_len.push(l as u32);
                r += l;
            }
        }
        run_off.push(run_src.len() as u32);
        Code { z, mb, ncirc, nbits, starts, lens, lay, dv, free,
               run_off, run_src, run_dst, run_len }
    }

    /// s[i][r] = XOR over the circulants of layer i of v[idx[c][r]].
    #[inline(always)]
    fn syndrome(&self, v: &[u8], s: &mut [u8]) {
        for i in 0..self.mb {
            let row = &mut s[i * self.z..(i + 1) * self.z];
            row.iter_mut().for_each(|x| *x = 0);
            for c in self.starts[i]..self.starts[i] + self.lens[i] {
                for t in self.run_off[c] as usize..self.run_off[c + 1] as usize {
                    let (sp, dp, l) = (self.run_src[t] as usize,
                                       self.run_dst[t] as usize,
                                       self.run_len[t] as usize);
                    let src = &v[sp..sp + l];
                    let dst = &mut row[dp..dp + l];
                    for j in 0..l {
                        dst[j] ^= src[j];
                    }
                }
            }
        }
    }

    #[inline(always)]
    fn syndrome_weight(&self, v: &[u8], s: &mut [u8]) -> u32 {
        self.syndrome(v, s);
        s.iter().map(|&x| x as u32).sum()
    }
}

/// Per-decode scratch, allocated once per worker thread and reused across every
/// iteration of every stage -- nothing in the hot loops allocates.
struct Scratch {
    ch: Vec<f32>,
    tot: Vec<f32>,
    r: Vec<f32>,
    q: Vec<f32>,
    acc: Vec<f64>,
    m1: Vec<f32>,
    m2: Vec<f32>,
    am: Vec<u32>,
    prod: Vec<f32>,
    s: Vec<u8>,
    sm: Vec<u32>,
    unsat: Vec<u8>,
    v: Vec<u8>,
    hist: Vec<u32>,
}

impl Scratch {
    fn new(c: &Code) -> Scratch {
        let e = c.ncirc * c.z;
        Scratch {
            ch: vec![0.0; c.nbits],
            tot: vec![0.0; c.nbits],
            r: vec![0.0; e],
            q: vec![0.0; e],
            acc: vec![0.0; c.nbits],
            m1: vec![0.0; c.mb * c.z],
            m2: vec![0.0; c.mb * c.z],
            am: vec![0; c.mb * c.z],
            prod: vec![0.0; c.mb * c.z],
            s: vec![0u8; c.mb * c.z],
            sm: vec![0u32; c.mb * c.z],
            unsat: vec![0u8; c.nbits],
            v: vec![0u8; c.nbits],
            hist: Vec::with_capacity(BITFLIP_ITERS as usize),
        }
    }
}

/// Stage A: parallel Gallager-B majority flipping.  Integer throughout, so it
/// is exact by construction.  -> (converged, iterations)
#[inline(always)]
fn bitflip(c: &Code, sc: &mut Scratch, sw0: u32) -> (bool, u32) {
    sc.hist.clear();
    for it in 1..=BITFLIP_ITERS {
        // `sw0` is the caller's syndrome weight of the SAME vector -- `v` is
        // still v0 on the first pass -- so recomputing it here was a whole
        // extra syndrome (137 328 XORs) per decode for a number already known.
        let sw = if it == 1 {
            sw0
        } else {
            c.syndrome_weight(&sc.v, &mut sc.s)
        };
        if sw == 0 {
            return (true, it);
        }
        sc.hist.push(sw);
        sc.unsat.iter_mut().for_each(|x| *x = 0);
        // `s` is 0/1, so `+= s` is the original `if s != 0 { += 1 }` -- and it
        // is branchless and contiguous on both sides, so it vectorises whole
        // cache lines at a time.
        for ci in 0..c.ncirc {
            let srow = &sc.s[c.lay[ci] * c.z..c.lay[ci] * c.z + c.z];
            for t in c.run_off[ci] as usize..c.run_off[ci + 1] as usize {
                let (sp, dp, l) = (c.run_src[t] as usize, c.run_dst[t] as usize,
                                   c.run_len[t] as usize);
                let src = &srow[dp..dp + l];
                let dst = &mut sc.unsat[sp..sp + l];
                for j in 0..l {
                    dst[j] += src[j];
                }
            }
        }
        // Branchless, so this vectorises: the original `if cond { v ^= 1; any =
        // true }` is a data-dependent branch per bit over 18 480 bits, and the
        // flips are rare and unpredictable.  `free` and the comparison are both
        // 0/1, so the AND is the condition and OR-ing it accumulates `any`.
        let mut any = 0u8;
        for j in 0..c.nbits {
            let f = c.free[j] & ((2 * sc.unsat[j] > c.dv[j]) as u8);
            sc.v[j] ^= f;
            any |= f;
        }
        if any == 0 {
            return (false, it);
        }
        // the numpy oscillation guard: the last 8 syndrome weights taking at
        // most 2 distinct values means it is cycling, not converging.
        let n = sc.hist.len();
        if n >= 8 {
            let mut d = [0u32; 8];
            d.copy_from_slice(&sc.hist[n - 8..]);
            d.sort_unstable();
            let mut distinct = 1;
            for k in 1..8 {
                if d[k] != d[k - 1] {
                    distinct += 1;
                }
            }
            if distinct <= 2 {
                return (false, it);
            }
        }
    }
    (false, BITFLIP_ITERS)
}

/// Check-node pass over one contiguous run.  Lane `j` is an independent copy of
/// the scalar body -- nothing here is reassociated, so this vectorises and is
/// still bit-exact.  `sbit` is the fused per-iteration syndrome.
#[inline(always)]
fn cn_run(g: &[f32], rprev: &[f32], qout: &mut [f32], m1: &mut [f32],
          m2: &mut [f32], am: &mut [u32], ap: &mut [f32], sbit: &mut [u32],
          k: u32) {
    let l = g.len();
    for j in 0..l {
        let gv = g[j];
        // The syndrome accumulator is the raw COMPARISON MASK, not a 0/1 byte.
        // Only "are they all zero" is ever asked of it, and XOR-ing all-ones
        // answers that identically -- while a 0/1 byte made LLVM narrow every
        // vector back down (VEXTRACTF128 + VPACKSSDW + VPACKSSWB + VPAND) for
        // seven instructions where this needs two.
        sbit[j] ^= if gv < 0.0 { !0u32 } else { 0 };
        let qv = gv - rprev[j];
        qout[j] = qv;
        // `prod *= +-1.0`, written as the sign-bit XOR it is.  `x * -1.0` IS
        // `-x` and negation IS a sign-bit flip, both exactly, for every finite
        // x -- so this is the identical value.  Written as a multiply, LLVM
        // if-converts the "unchanged" lanes into a masked store, and
        // VMASKMOVPS is microcoded on Zen 2; unconditional load/xor/store is
        // several times cheaper for the same bits.
        //
        // ALPHA IS FOLDED IN: this runs from ALPHA rather than 1.0, so it holds
        // `alpha * prod` and the variable-node pass loads it instead of
        // multiplying.  `prod` is only ever +-1.0, and `ALPHA * +-1.0` is
        // exactly `+-ALPHA`, which is exactly ALPHA with its sign bit flipped.
        let flip: u32 = if qv >= 0.0 { 0 } else { 0x8000_0000 };
        ap[j] = f32::from_bits(ap[j].to_bits() ^ flip);
        let a = if qv < 0.0 { -qv } else { qv };     // f32::abs, branchless
        let (o1, o2) = (m1[j], m2[j]);
        // `<` and not `<=`: np.argmin takes the FIRST minimum and
        // np.partition(kth=1) keeps the second smallest WITH multiplicity.
        let c1 = a < o1;
        m1[j] = if c1 { a } else { o1 };
        m2[j] = if c1 { o1 } else if a < o2 { a } else { o2 };
        am[j] = if c1 { k } else { am[j] };
    }
}

/// Variable-node pass over one contiguous run.  `acc` is the f64 running sum;
/// within a circulant the Z bit indices are distinct, so each element of `acc`
/// takes exactly one add here and vectorising cannot reorder any accumulator's
/// own sequence.  See the header.
#[inline(always)]
fn vn_run(qin: &[f32], m1: &[f32], m2: &[f32], am: &[u32], ap: &[f32],
          rout: &mut [f32], acc: &mut [f64], k: u32) {
    let l = qin.len();
    for j in 0..l {
        let qv = qin[j];
        let sg: f32 = if qv >= 0.0 { 1.0 } else { -1.0 };
        // BOTH magnitudes are loaded and then the VALUE is selected.  Written
        // as `if am[j] == k { m2[j] } else { m1[j] }` LLVM selects the ADDRESS
        // instead, which it cannot vectorise: it emitted a per-lane branch and
        // rebuilt the vector with VINSERTPS.  Same values, same order.
        let (lo, hi) = (m1[j], m2[j]);
        let mag = if am[j] == k { hi } else { lo };
        // f32 throughout: numpy 2.x casts the weak Python `alpha` to f32, so
        // this chain never widens.  Left to right, no contraction.  `ap` IS
        // `ALPHA * prod` -- carried that way by `cn_run`, exactly.
        let val = (ap[j] * sg) * mag;
        rout[j] = val;
        acc[j] += val as f64;
    }
}

/// Stage B: normalized min-sum, flooding schedule.  -> (converged, iterations)
#[inline(always)]
fn minsum(c: &Code, v0: &[u8], sc: &mut Scratch) -> (bool, u32) {
    let Scratch { ch, tot, r, q, acc, m1, m2, am, prod, sm, v, .. } = sc;
    let z = c.z;
    for j in 0..c.nbits {
        let x = if c.free[j] == 0 {
            PIN                       // the 64 structurally-known zero bits
        } else if v0[j] > 0 {
            -1.0
        } else {
            1.0
        };
        ch[j] = x;
        tot[j] = x;
    }
    r.iter_mut().for_each(|x| *x = 0.0);

    for it in 1..=MINSUM_ITERS {
        // -- pass 1: Q, the two smallest magnitudes, the sign product, and the
        //            syndrome -- all off ONE read of tot per edge.
        let mut syn_any = 0u32;
        for i in 0..c.mb {
            let cell0 = i * z;
            for j in cell0..cell0 + z {
                m1[j] = f32::INFINITY;
                m2[j] = f32::INFINITY;
                am[j] = 0;
                prod[j] = ALPHA;          // `prod` carries ALPHA * prod
                sm[j] = 0;
            }
            let (c0, n) = (c.starts[i], c.lens[i]);
            for k in 0..n {
                let ci = c0 + k;
                let base = ci * z;
                for t in c.run_off[ci] as usize..c.run_off[ci + 1] as usize {
                    let (sp, dp, l) = (c.run_src[t] as usize,
                                       c.run_dst[t] as usize,
                                       c.run_len[t] as usize);
                    cn_run(&tot[sp..sp + l],
                           &r[base + dp..base + dp + l],
                           &mut q[base + dp..base + dp + l],
                           &mut m1[cell0 + dp..cell0 + dp + l],
                           &mut m2[cell0 + dp..cell0 + dp + l],
                           &mut am[cell0 + dp..cell0 + dp + l],
                           &mut prod[cell0 + dp..cell0 + dp + l],
                           &mut sm[cell0 + dp..cell0 + dp + l],
                           k as u32);
                }
            }
            for j in cell0..cell0 + z {
                syn_any |= sm[j];
            }
        }
        if syn_any == 0 {
            for j in 0..c.nbits {
                v[j] = (tot[j] < 0.0) as u8;
            }
            return (true, it);
        }
        // -- pass 2: R, and the f64 variable-node accumulation ---------------
        acc.iter_mut().for_each(|x| *x = 0.0);
        for i in 0..c.mb {
            let cell0 = i * z;
            let (c0, n) = (c.starts[i], c.lens[i]);
            for k in 0..n {
                let ci = c0 + k;
                let base = ci * z;
                for t in c.run_off[ci] as usize..c.run_off[ci + 1] as usize {
                    let (sp, dp, l) = (c.run_src[t] as usize,
                                       c.run_dst[t] as usize,
                                       c.run_len[t] as usize);
                    vn_run(&q[base + dp..base + dp + l],
                           &m1[cell0 + dp..cell0 + dp + l],
                           &m2[cell0 + dp..cell0 + dp + l],
                           &am[cell0 + dp..cell0 + dp + l],
                           &prod[cell0 + dp..cell0 + dp + l],
                           &mut r[base + dp..base + dp + l],
                           &mut acc[sp..sp + l],
                           k as u32);
                }
            }
        }
        for j in 0..c.nbits {
            tot[j] = ch[j] + (acc[j] as f32);
        }
    }
    for j in 0..c.nbits {
        v[j] = (tot[j] < 0.0) as u8;
    }
    (c.syndrome_weight(&sc.v, &mut sc.s) == 0, MINSUM_ITERS)
}

/// `nd_ldpc_fix.decode_mother` -> (converged, iterations, stage, vector).
#[inline(always)]
fn decode_one_inner(c: &Code, sc: &mut Scratch, v0: &[u8], out: &mut [u8])
                    -> (bool, u32, u8) {
    sc.v.copy_from_slice(v0);
    let sw0 = c.syndrome_weight(v0, &mut sc.s);
    if sw0 == 0 {
        out.copy_from_slice(v0);
        return (true, 0, 0);
    }
    let (a_ok, a_it) = bitflip(c, sc, sw0);
    if a_ok {
        out.copy_from_slice(&sc.v);
        return (true, a_it, 1);
    }
    let (b_ok, b_it) = minsum(c, v0, sc);
    if b_ok {
        out.copy_from_slice(&sc.v);
        return (true, a_it + b_it, 2);
    }
    out.copy_from_slice(v0);          // failure returns the ORIGINAL vector
    (false, a_it + b_it, 3)
}

// ---------------------------------------------------------------- dispatch --
// The kernels above are ordinary safe Rust that LLVM auto-vectorises; how WIDE
// it may go is a property of the target, and the default x86-64 target is
// SSE2.  Compiling the whole binary for this machine (`target-cpu=native` or a
// baked-in `+avx2`) would be a portability regression -- the crate is built
// from a checkout by whoever is holding the dump, and a binary that SIGILLs on
// an older CPU is worse than a slow one.
//
// So: `decode_one_inner` is `#[inline(always)]` and is instantiated TWICE, once
// plain and once inside an `avx2` function.  Everything it calls is
// `#[inline(always)]` too, so the whole decoder lands inside the AVX2 island
// and is code-generated at 8 lanes; the choice is made once, at start-up, from
// `is_x86_feature_detected!`.  Nothing here is a dependency: `std::arch` and
// `std::sync::OnceLock` are std.
//
// AVX2 IS NOT A FAST-MATH SWITCH.  VEX-encoded SSE/AVX arithmetic is
// bit-identical to the legacy encodings; `+fma` is a SEPARATE feature and is
// deliberately NOT enabled, and rustc never sets the `contract` fast-math flag
// anyway, so no multiply-add is ever fused.  The two paths therefore agree bit
// for bit -- `TNLDPC_NO_SIMD=1` forces the scalar one and `--selftest` runs the
// whole corpus through BOTH and requires identical vectors, stages and
// ITERATION COUNTS.  A decoder whose verdict depended on the host CPU would be
// worse than a slow one.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn decode_one_avx2(c: &Code, sc: &mut Scratch, v0: &[u8],
                          out: &mut [u8]) -> (bool, u32, u8) {
    decode_one_inner(c, sc, v0, out)
}

fn use_avx2() -> bool {
    #[cfg(target_arch = "x86_64")]
    {
        use std::sync::OnceLock;
        static OK: OnceLock<bool> = OnceLock::new();
        return *OK.get_or_init(|| {
            std::env::var_os("TNLDPC_NO_SIMD").is_none()
                && is_x86_feature_detected!("avx2")
        });
    }
    #[cfg(not(target_arch = "x86_64"))]
    false
}

fn decode_one(c: &Code, sc: &mut Scratch, v0: &[u8], out: &mut [u8])
              -> (bool, u32, u8) {
    #[cfg(target_arch = "x86_64")]
    if use_avx2() {
        return unsafe { decode_one_avx2(c, sc, v0, out) };
    }
    decode_one_inner(c, sc, v0, out)
}

fn rd_u32(r: &mut impl Read) -> Option<u32> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b).ok()?;
    Some(u32::from_le_bytes(b))
}

/// CRC-48 (x^48+x^47+x^6+x^5+x+1) and CRC-32Q, byte-at-a-time with a 256-entry
/// table -- the same construction as `titania_nand._mktable`, so the register
/// state after every byte is identical to the Python and the two cannot drift.
fn crc_table(poly: u64, width: u32) -> Vec<u64> {
    let top = 1u64 << (width - 1);
    let mask = if width == 64 { !0u64 } else { (1u64 << width) - 1 };
    (0..256)
        .map(|b| {
            let mut r = ((b as u64) << (width - 8)) & mask;
            for _ in 0..8 {
                r = if r & top != 0 {
                    ((r << 1) ^ poly) & mask
                } else {
                    (r << 1) & mask
                };
            }
            r
        })
        .collect()
}

/// `crc(~msg)`, returned big-endian in `width / 8` bytes.
fn crc_complemented(tab: &[u64], width: u32, msg: &[u8], out: &mut [u8]) {
    let mask = if width == 64 { !0u64 } else { (1u64 << width) - 1 };
    let shift = width - 8;
    let mut r: u64 = 0;
    for &b in msg {
        let idx = (((r >> shift) as u8) ^ (b ^ 0xFF)) as usize;
        r = ((r << 8) ^ tab[idx]) & mask;
    }
    let n = (width / 8) as usize;
    for i in 0..n {
        out[i] = (r >> (8 * (n - 1 - i))) as u8;
    }
}

fn main() {
    let stdin = io::stdin();
    let mut inp = stdin.lock();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());

    let mut magic = [0u8; 4];
    inp.read_exact(&mut magic).expect("no init header");
    assert_eq!(&magic, b"TNLD", "bad init magic");
    let z = rd_u32(&mut inp).unwrap() as usize;
    let mb = rd_u32(&mut inp).unwrap() as usize;
    let _nb = rd_u32(&mut inp).unwrap() as usize;
    let ncirc = rd_u32(&mut inp).unwrap() as usize;
    let nbits = rd_u32(&mut inp).unwrap() as usize;

    let mut raw = vec![0u8; ncirc * z * 4];
    inp.read_exact(&mut raw).expect("short idx");
    let idx: Vec<i32> = raw
        .chunks_exact(4)
        .map(|c| i32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect();
    let mut raw = vec![0u8; mb * 4];
    inp.read_exact(&mut raw).expect("short lens");
    let lens: Vec<usize> = raw
        .chunks_exact(4)
        .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]) as usize)
        .collect();
    let mut free = vec![0u8; nbits];
    inp.read_exact(&mut free).expect("short free");

    let code = Code::new(z, mb, ncirc, nbits, idx, lens, free);
    let mut solo = Scratch::new(&code);

    // ready: one byte, so the driver can tell "built and initialised" from
    // "started and then died on the code description".
    out.write_all(b"K").unwrap();
    out.flush().unwrap();

    let mut v0 = vec![0u8; nbits];
    let mut res = vec![0u8; nbits];
    loop {
        let mut op = [0u8; 1];
        if inp.read_exact(&mut op).is_err() || op[0] == 0 {
            break;
        }
        if op[0] == 1 {
            if inp.read_exact(&mut v0).is_err() {
                break;
            }
            let (ok, iters, stage) = decode_one(&code, &mut solo, &v0,
                                                &mut res);
            out.write_all(&[ok as u8]).unwrap();
            out.write_all(&iters.to_le_bytes()).unwrap();
            out.write_all(&[stage]).unwrap();
            out.write_all(&res).unwrap();
            out.flush().unwrap();
            continue;
        }
        if op[0] == 3 {
            // Syndrome weights, no decode.  Same pool, same shape of work.
            let count = match rd_u32(&mut inp) {
                Some(n) => n as usize,
                None => break,
            };
            let mut batch = vec![0u8; count * nbits];
            if inp.read_exact(&mut batch).is_err() {
                break;
            }
            let mut wts = vec![0u32; count];
            {
                let cref = &code;
                let nthreads = std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(1)
                    .min(count.max(1))
                    .max(1);
                let mut queue: Vec<(usize, &mut u32)> =
                    wts.iter_mut().enumerate().collect();
                queue.reverse();
                let queue = Mutex::new(queue);
                let bref = &batch;
                std::thread::scope(|sp| {
                    for _ in 0..nthreads {
                        sp.spawn(|| {
                            let mut s = vec![0u8; cref.mb * cref.z];
                            loop {
                                let job = queue.lock().unwrap().pop();
                                let (i, w) = match job {
                                    Some(j) => j,
                                    None => break,
                                };
                                let v = &bref[i * nbits..(i + 1) * nbits];
                                *w = cref.syndrome_weight(v, &mut s);
                            }
                        });
                    }
                });
            }
            for w in &wts {
                out.write_all(&w.to_le_bytes()).unwrap();
            }
            out.flush().unwrap();
            continue;
        }
        if op[0] == 4 {
            let count = match rd_u32(&mut inp) { Some(n) => n as usize, None => break };
            let msglen = match rd_u32(&mut inp) { Some(n) => n as usize, None => break };
            let mut wbuf = [0u8; 1];
            if inp.read_exact(&mut wbuf).is_err() {
                break;
            }
            let width: u32 = (wbuf[0] as u32) * 8;
            if !(width == 32 || width == 48) {
                break;
            }
            let poly: u64 = if width == 48 { 0x1_8000_0000_0063 } else { 0x8141_41AB };
            let tab = crc_table(poly, width);
            let mut msgs = vec![0u8; count * msglen];
            if inp.read_exact(&mut msgs).is_err() {
                break;
            }
            let nb = (width / 8) as usize;
            let mut outs = vec![0u8; count * nb];
            {
                let tref = &tab;
                let mref = &msgs;
                let nthreads = std::thread::available_parallelism()
                    .map(|n| n.get()).unwrap_or(1).min(count.max(1)).max(1);
                let mut queue: Vec<(usize, &mut [u8])> =
                    outs.chunks_mut(nb).enumerate().collect();
                queue.reverse();
                let queue = Mutex::new(queue);
                std::thread::scope(|sp| {
                    for _ in 0..nthreads {
                        sp.spawn(|| loop {
                            let job = queue.lock().unwrap().pop();
                            let (i, o) = match job { Some(j) => j, None => break };
                            crc_complemented(tref, width,
                                             &mref[i * msglen..(i + 1) * msglen], o);
                        });
                    }
                });
            }
            out.write_all(&outs).unwrap();
            out.flush().unwrap();
            continue;
        }
        if op[0] != 2 {
            break;
        }
        let count = match rd_u32(&mut inp) {
            Some(n) => n as usize,
            None => break,
        };
        let mut batch = vec![0u8; count * nbits];
        if inp.read_exact(&mut batch).is_err() {
            break;
        }
        let mut outs = vec![0u8; count * nbits];
        let mut meta = vec![(false, 0u32, 0u8); count];
        {
            let cref = &code;
            // A bounded pool: `nthreads` workers taking jobs from one atomic
            // counter, each keeping ONE `Scratch` for the life of the batch.
            //
            // Why a pool and not a thread per job.  Each job's scratch is
            // ~1.4 MB touched every iteration, which is the whole reason the
            // decode is parallel per JOB and not split inside one (that was
            // built and measured -- src/main.rs.pool-experiment, exact at every
            // thread count -- and it shares the 1.4 MB, so on this chiplet part
            // it ran from 2.8x faster to no faster, run to run, depending on
            // whether the scheduler landed the threads inside one CCX's L3; a
            // speed-up that is not reproducible is not one).  Per-job threads
            // made that working set scale with the BATCH: fine at four, and at
            // four hundred it is 560 MB and 400 threads over 64 cores.  Sized
            // by the machine instead, the memory is bounded and each thread
            // pays for its scratch once rather than once per job.
            //
            // Spawn cost was measured at 75 us a thread against jobs of several
            // ms, so it was never the problem; it is under 0.2% here, where a
            // thread is reused across a whole batch.
            // The queue hands out each job's OWN output slice and meta slot, so
            // the borrow checker carries the disjointness and there is no
            // `unsafe` here.  The lock is held only to pop -- nanoseconds
            // against a job of several ms -- and dynamic handoff is what keeps
            // the pool balanced: a clean codeword returns in microseconds while
            // an uncorrectable one runs all 80 min-sum iterations, so a static
            // split would leave threads idle behind one slow job.
            let nthreads = std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(1)
                .min(count)
                .max(1);
            let mut queue: Vec<(usize, &mut [u8], &mut (bool, u32, u8))> =
                outs.chunks_mut(nbits)
                    .zip(meta.iter_mut())
                    .enumerate()
                    .map(|(i, (o, m))| (i, o, m))
                    .collect();
            queue.reverse();                    // pop() takes job 0 first
            let queue = Mutex::new(queue);
            let bref = &batch;
            std::thread::scope(|sp| {
                for _ in 0..nthreads {
                    sp.spawn(|| {
                        let mut sc = Scratch::new(cref);
                        loop {
                            let job = queue.lock().unwrap().pop();
                            let (i, o, m) = match job {
                                Some(j) => j,
                                None => break,
                            };
                            let src = &bref[i * nbits..(i + 1) * nbits];
                            *m = decode_one(cref, &mut sc, src, o);
                        }
                    });
                }
            });
        }
        for i in 0..count {
            let (ok, iters, stage) = meta[i];
            out.write_all(&[ok as u8]).unwrap();
            out.write_all(&iters.to_le_bytes()).unwrap();
            out.write_all(&[stage]).unwrap();
            out.write_all(&outs[i * nbits..(i + 1) * nbits]).unwrap();
        }
        out.flush().unwrap();
    }
}
