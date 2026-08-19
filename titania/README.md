# titania

Read, decode and verify the NAND of a PS5 Titania controller (Marvell
88SS1098/88SS1099, the successor to "Eldora"/"Zao").

The media format is solved and the codec is verified byte-exact against real
silicon. The storage layer above it is a *reconstruction*, and this README is
careful about which is which — that distinction is the most valuable thing the
project has, and overstating it would throw it away.

```python
from titania import DumpDevice, decode_page, Ftl

dev  = DumpDevice()          # $TITANIA_DUMP, or a dump beside the CWD
page = dev.read_page(ce=1, block=8, page=0)   # raw, exactly as stored
aus  = decode_page(page)                      # ECC, MCRC, descramble
ftl  = Ftl.from_device(dev)
print(ftl.msos())
```

---

## Installing

```
pip install -e .        # develop against the checkout
pip install .           # or a plain wheel — both work
```

### Telling it where your dump is

Nothing is compiled in. Resolution runs most-explicit-first:

| what | how |
|---|---|
| `--device dump:PATH` | the command line wins over everything |
| `$TITANIA_DUMP` | a dump tree or a page store |
| discovery | a `dump/` or `nand/` directory, or any page store, in the working directory or beside the checkout |

Discovery is deliberately narrow. It looks in two places, and if two candidates
match it **raises and names them both** rather than picking — a tool that
silently reads a different capture than you meant is worse than one that asks.
A dump tree and the page store built from it count once, not twice: the store
records which tree it came from, and `dumpdev.canonical_root` verifies that
claim before redirecting.

One more variable, optional:

* **`$TITANIA_ARM_PREFIX`** — the cross-toolchain triple for the on-device
  stubs, default `arm-none-eabi-`. `titania.armbuild` runs the compiler directly
  when it is on PATH and falls back to WSL on Windows, translating the working
  directory with `wslpath` rather than assuming one.

### The measured tables

The codec loads six measured tables, and every one of them is a config image
read out of the controller: the ECU's LDPC encoder and decoder RAM for the three
codes the silicon implements, plus A10R's base matrix in the stored-frame basis.
They describe Marvell silicon rather than any particular drive — the same bytes
are right on every Titania, and none of them came off a NAND die — which is why
they ship. Anything derivable from them was dropped rather than carried:
`nd_ldpc.ee3_layers()` parses EE3's 41 × 2861 schedule straight out of the
decoder image, and `titania_nand.seed_state()` reproduces all 16384 scrambler
seeds from 325 bytes of ROM constants, so neither is a file any more.

They live in **`titania/data/`**, and every load goes through
`nd_tables.table()`, which resolves that relative to `__file__`. It is the same
directory in a checkout, under `pip install -e .` and in an unpacked wheel, so
there is one location and no stale-copy question to get wrong.

`tests/test_packaging.py` checks every table against a digest recorded in
`nd_tables.SHA256` when it was read off the silicon, and `nd_tables.verify()`
does the same at runtime. There used to be a second copy at the repository root
and a test that the two were byte-identical; that test could not catch what
actually happened — a line-ending sweep matching `*.bin` stripped a byte from
two tables in *both* locations at once, and copy-vs-copy stayed green because
the copies still agreed. A digest catches that, and needs only one copy. **If
you regenerate a table, update its digest.**

Proving a wheel works, from a directory that is not the checkout:

```
py -m pip wheel . --no-deps -w <outdir>
py -m venv <venv> && <venv>/Scripts/python -m pip install <outdir>/titania-0.1.0-py3-none-any.whl
cd <somewhere else> && <venv>/Scripts/python -c "from titania import nd_ldpc_odd, nd_tables; print(nd_tables.verify() or 'tables OK')"
```

Extras:

```
pip install -e .[hardware]    # pyserial/xmodem/tqdm/hexdump2 — the live board
pip install -e .[dev]         # pytest
```

### The LDPC accelerator (optional)

`rs/tnldpc` is the EE3 corrector in Rust. It is optional — with no toolchain,
no binary, or `TITANIA_NO_RUST=1`, everything falls back to the numpy decoder
and the answers do not change.

```
py -m titania.nd_ldpc_rs --build       # cargo build --release, once
py -m titania.nd_ldpc_rs --selftest    # Rust vs numpy on real codewords
```

It matters because **`dumpdev.adjudicate()` decides a contested page by decoding
both candidates**, which is 8 codewords per candidate of genuine min-sum
arithmetic and dominates everything else — 62 s for 19 conflicts under numpy,
of which 56 s was `minsum`. Measured here: 9.5× on the selftest workload.

The crate has **no dependencies**, so it builds offline from a bare checkout.
That is a requirement, not a happy accident: it is a decoder whose *exact*
output matters. Adjudication compares correction counts, so a decoder that
reached the same codeword by a different path and reported a different iteration
count would change verdicts. `main.rs` reproduces `nd_ldpc_fix.py` operation for
operation — same order, same precision, including numpy's f64 `bincount`
accumulation and NEP 50 weak-scalar f32 rules — and the selftest checks the
vector, the converged flag, the stage *and* the iteration count.
`rs/tnldpc/verify_adjudicate.py` is the end-to-end version: run it with and
without the Rust path and diff the digest over every verdict.

`numpy` and `pycryptodome` are hard requirements: the LDPC layer is dense GF(2)
linear algebra, and the group-2 firmware images are AES-encrypted.

---

## The layer model

Each layer is usable on its own, and nothing reaches past the layer below it.

```
    geometry   addressing arithmetic: rows, pbids, WERUs, cell modes
       |
    device     read_page / write_page / erase_block
       |         DumpDevice      raw dump files, or a PageStore
       |         HardwareDevice  the live board, over the UART bridge
       |
    codec      the media format: scrambler, MCRC, AU layout, EE3 + A10R LDPC
       |
    fw ------- firmware images: FW_ID table, group 0/1/2 headers, extraction
    ftl ------ the storage layer: MSOs, WERUs, NSIDs, SystemMetadata, LUTs
       |
    extract    an MSO's logical address space, off the media, with evidence
    cli        `titania <cmd>` / `python -m titania <cmd>`
```

Acquisition sits beside that stack rather than inside it, and is only reachable
with a board attached:

```
    readretry  the die's read references: cell-mode latch, the firmware's own
       |       32-step ladder, and a VERIFIED restore to level 0
    smartdump  a capture that uses them -- `titania capture`
    summary    the per-block table the viewer renders -- `python -m titania.summary`
```

`titania capture` differs from a plain sweep in four ways, each because the
plain sweep got something wrong:

* the **cell mode** is chosen per block from the cell map and latched
  explicitly, so a native-TLC block is never read in a pseudo-SLC latch — which
  makes a written page come back all-0xFF and be recorded as erased media;
* only blocks that **may hold data** are read in full, by the union of a page-0
  probe (asks the media) and the FTL's allocated WERUs (asks the firmware),
  because each misses what the other catches;
* every page is judged by `titania.verdict`, which knows that block 0 on the
  group-2 dies has **its own ECC geometry** and is not a failure — testing it as
  an ordinary page reports ~1840 false failures;
* a page that does not decode is **re-read up the retry ladder** until it does,
  and the level it needed goes in its source name, because the level is the
  provenance.

Everything on that path is read-only by construction: the only opcodes reachable
are READ, READ STATUS, GET/SET FEATURES (a volatile die register) and the
cell-mode selects. There is no program or erase in the call graph, and the die's
references are restored to level 0 and verified by 0xEE readback on every exit
path — including errors and Ctrl-C. A die left shifted silently changes every
later read on that CE.

`geometry` and `device` import nothing heavy — not even numpy — so a consumer
that only wants address arithmetic pays nothing for the rest. `titania.cli`
imports every layer lazily, so `titania --help` does not load numpy either.
Both facts are asserted in `tests/test_packaging.py`, because both have been
true by accident before.

---

## What is verified, and how

Run these. They are integration checks against real media, and they are the
contract:

Counts below were **measured on 2026-08-07**; they move as work lands, so treat
the number as a tripwire (a drop means something regressed) rather than as a
constant.

| command | checks |
|---|---|
| `py titania_nand.py --selftest` | the codec: seed map 16384/16384, a real page **re-encodes byte-identical over all 4608 bytes** (message, MCRC, scrambling, both parities, pipeline tail), both LDPC codes encode *and* decode |
| `py -m titania.ftl --selftest` | **116 PASS, 0 FAIL, 0 SKIPPED** on the storage layer, including two independent WMA implementations over 20000 addresses |
| `py -m titania.namespace --selftest` | **31 PASS** — SLBA/lpId/LBA arithmetic, the LPT, and the L2S array rebuilt from media aux |
| `py -m titania.auxdata --selftest` | **18 PASS, 3 SKIPPED** — the four aux layouts and the acceptance rule, plus a random-aux negative control |
| `py -m titania.raid --selftest` | **PASS with 2 SKIPPED** — inline RAID placement is measured; the XOR **cannot** be closed on this board (dead CE0 puts 4 erasures in a 1-erasure code) and is reported SKIPPED, never PASS |
| `py -m titania.trim --selftest` | **25 PASS, 0 FAIL, 3 SKIPPED** — the trim log and its retirement watermark |
| `py -m titania.verdict --selftest` | **6 PASS** — the per-page verdict vocabulary and the dead-CE detector |
| `py -m titania.rawimage --selftest` | **29 PASS, 0 FAIL** — third-party raw captures, retry-level selection, mode checking |
| `py -m titania.fw --selftest` | **28 passed, 0 skipped, of which 3 are REGRESSION** (see below), incl. group-2 decrypting to the staged SRAM copy |
| `py -m titania.errorlog --selftest` | the ErrorLog record format. **This drive logged no errors** — the one line in it is a version banner |
| `py -m titania.ddmdata --selftest` | the DDM blob field layout |
| `py -m titania.dumpdev --selftest` | the dump index, folding, conflict adjudication |
| `py -m titania.extract --selftest` | the extraction promises, incl. the dead-CE detector |
| `py -m titania info` | end-to-end against the discovered dump |
| `py -m titania.store` | the page store: key order, tier filtering, and compaction |

**Not every PASS is the same strength, and the suites say which is which.**
Three of `titania.fw`'s 28 are labelled `REGRESSION`: they compare this
extractor against `<dump>/fw_group_{0,1}_thisboard.bin`, which *this extractor
produced*. They catch a change in behaviour; they are not independent evidence
that the behaviour is right. The same suite's non-REGRESSION rows (the MCRCs,
the SLB2 container's self-consistency, the group-2 header magic) are properties
of the media instead. Likewise a `SKIPPED` here is a deliberate refusal to claim
something the board cannot show — `titania.raid` skips the cRaidTypeMso XOR
rather than infer it, and `titania.auxdata` skips the layout-2 shard order because
every sequence on this board is below 65536.

### Keeping a page store lean

The blobs are append-only and the builder's `finish()` is the **last** thing a
build does. So a `pc_migrate.py` run that appends pages and then cannot replace
`index.tix` — because another process on this tree has it mmapped, which on
Windows is a hard `PermissionError`, and several agents run against one store —
leaves the store **correct** and the blobs **fat**: the bytes went in, the
manifest line never did, and the next run appends the same page again. Nothing
is lost and nothing is wrong; there is just a second copy no record points at.

That is structural — append-only plus concurrency — not an accident, so the
repair lives next to the thing that creates it:

```
py -m titania.store --compact <store> --dry-run    # measure, change nothing
py -m titania.store --compact <store>              # reclaim
```

It rewrites only **store-owned** blobs (a blob outside `<store>/blobs/` is a raw
capture the store merely references, and is never written through), verifies each
new blob against the original's live ranges *before* swapping anything, and uses
the `index.tix` rename as its own exclusivity check — if a reader still holds the
store open it refuses with nothing changed. Live means referenced by `index.tix`
**or** `ingest.tsv`: a manifest line whose source file left the tree is
deliberately unindexed while its bytes still back the incremental build.

Verify a compaction the same way a migration is verified — every page, both
backings, byte for byte:

```
py pc_migrate.py <tree> <store> --verify
```

The strongest single statement the project can make is the codec one: an AU
taken off the die re-encodes to the identical 4608 bytes, parity included. That
is not "we can read it" — it is "we know what the controller would have
written".

The load-bearing check in the *extraction* path is different and worth
understanding: **an AU is verified when its own aux LMA equals the LMA that was
looked up.** That one equality exercises the whole chain — LUT entry, WMA
arithmetic, WERU slot rule, page address, cell mode, scrambler seed, ECC —
because any of them being wrong lands on an AU whose aux says a different
number. MCRC is recorded too but is *not* the proof: it is computed over the
complemented **stored** bytes, so it attests the page came off the die intact
and says nothing about whether the address that produced it was the right one.

---

## What is NOT verified

Read this part before quoting any result from this tool.

**The user payload is ciphertext.** `extract` hands back the plaintext the FTL
would hand the host, and main user data is AES-encrypted *above* that layer.
Extracted payloads measure ~7.997 bits of entropy per byte. Turning that into
anything readable needs console keys, which are entirely outside this tool. An
extract that looks like noise is very probably a *correct* extract of
ciphertext; the aux-LMA verified count is how you tell the difference.

**The ftl layer is a reconstruction.** Much of it was derived from one board
whose CE0 is dead and whose main user data was never fully surveyed. An empty
result frequently means "not found *here*", not "not present". Functions that
can tell the difference say so in their return value; functions that cannot say
so in their docstring. Do not treat absence as evidence.

**`UNKNOWN` is not `None` and not `[]`.** `None` means "looked for,
definitively absent"; `[]` means "looked for, there are none"; `UNKNOWN` means
"no evidence either way". It is falsey, so `if not x` still guards, but it
raises `TypeError` on `len()`, iteration and arithmetic so that a gap in the
survey cannot be silently summed into a total and reported as a finding.

**Statuses are not collapsed into ok/not-ok.** `verified`, `corrected`,
`failed`, `erased`, `unmapped`, `unresolved`, `unreadable`, `dead-ce` — which
kind of failure it was is usually the whole question. `unresolved` is *correct*,
not a bug to paper over — and since 2026-08-07 it is no longer the common answer
for user data: the 48-block slot law is measured against media (3568 right / 0
wrong, `sd_slotlaw.py`), so what lands there now is a bounded set of refusals
(partial, `substituted`, invalid and irregular `blockMapping` records).

**CE0 on this board is dead and still answers**, with a constant page built from
only `0x00` and `0xA7` and containing no `0xFF` at all — neither programmed nor
erased. Nothing else in the stack would flag it, so it is detected by that
signature as well as by CE number.

**Retraction discipline.** Several docstrings record conclusions that turned out
to be wrong, and several say explicitly *do not "fix" this* or *do not re-test
this*. They mean it. Those banners are load-bearing documentation; a "cleanup"
that removes them costs more than it saves.

---

## Writing to media: the safety model

`write_page` and `erase_block` exist, because a NAND tool that cannot write is
half a tool. They are also irreversible and can destroy the only copy of the
data this project exists to recover. So there are **two independent opt-ins,
and neither is ever persisted**:

1. **Per instance, with a phrase.** Every `Device` starts with
   `writes_enabled = False`. `enable_writes("yes, write to the media")` must be
   called explicitly. A fresh `Device` over the same backing starts disabled
   again — there is no config file, no environment variable, and no flag that
   survives the process.
2. **Per backing, at construction.** `HardwareDevice` additionally refuses
   unless it was built with `allow_hardware_writes=True`. So a script that
   works correctly against a dump cannot reach the board by a typo: the phrase
   alone is not enough.

`DumpDevice` writes only into its own copy, which is what makes the write path
fully testable offline. Refusals are always a `WriteProtected` exception, never
a silent no-op a caller could mistake for success.

**Nothing in this repository is ever armed against media by default, and no
default should ever be changed to arm it.**

---

## A worked example

```
$ py -m titania fw list
```

```
[<FirmwareImage fw_id=0x10 group=0 bank=1 ce=[6..11] blocks=[6, 7] rows=0-341   size=67108864 ENCRYPTED>,
 <FirmwareImage fw_id=0x12 group=1 bank=1 ce=[6..11] blocks=[8, 9] rows=0-10    size=2097152>,
 <FirmwareImage fw_id=0x14 group=1 bank=1 ce=[6..11] blocks=[8, 9] rows=10-319  size=60817408>,
 <FirmwareImage fw_id=0x15 group=1 bank=1 ce=[6..11] blocks=[8, 9] rows=320-330 size=2097152>,
 <FirmwareImage fw_id=0x1  group=2 ce=[1..5] blocks=[0] rows=1-151   size=1536000 ENCRYPTED>,
 <FirmwareImage fw_id=0x2  group=2 ce=[1..5] blocks=[0] rows=155-181 size=266240  ENCRYPTED>]

header: tried CE 6..11, all 'checksum ok, mcrc 4/4', digests agree (30d45a231e3e)
group2: live_ce 1, 232 rows read, problems: ['CE0 is dead on this board']
```

Every part of that output is a claim with its evidence attached: which CEs were
tried, what each said, whether the six copies agreed, and — the important line —
that CE0, which is supposed to hold the real group-2 image, is dead, so what you
are looking at is a *replica*. That is availability, not format.

Extracting a range of an MSO's logical space:

```
$ py -m titania extract --mso 32 \
                        --lma 1149856:1149872 -o out.bin
```

writes `out.bin` plus `out.bin.manifest.jsonl`: one JSON record per LMA with its
status, physical address, cell mode, WMA/WERU, whether LDPC correction was
needed, and the page CRC. Re-running the same command resumes — by set
membership, not by a high-water mark, so widening the range later picks up the
LMAs that were never attempted rather than skipping to the end. Data bytes are
flushed *before* the manifest lines that claim them, so the manifest is always a
subset of what is on disk and a crash costs you re-work, never bytes it swears
are there.

Gaps are never invented. In `sparse` layout a missing LMA is a hole in the file;
`--fill` will synthesise `0x00`/`0xFF` only if asked, and marks every
synthesised LMA `"syn": true`.

---

## Tests

```
pytest
```

**175 unit tests** (measured 2026-08-07) that need **no board, no dump and no
store**. This is what an outside contributor can run:

- geometry arithmetic — row and pbid round trips, field overlap, cell-mode
  brackets, capacity constants against the products they document;
- the WMA ↔ physical map, round-tripped, plus the two independent
  implementations checked against each other and checked for *still being
  independent*;
- both MCRCs, including the one genuinely external vector in the project:
  `crc32q("123456789") == 0x3010BF7F`, the published check value for CRC-32Q,
  which confirms polynomial, bit order, init and reflection from outside;
- the 53-bit scrambler LFSR — tap recurrence, 14-bit fold, "the state is the
  first 53 keystream bits", `state_valid` over all 16384 seeds;
- LUT and block-mapping sentinels, including that the block mapping is
  *slot*-indexed and a hole must not renumber what follows it;
- manifest and resume — reopen, identity mismatch, torn final line, `--retry`
  semantics, offset accounting;
- store key packing, and that byte order equals tuple order;
- `UNKNOWN` raising on `len` / `iter` / arithmetic / unpacking;
- the packaging and write-safety claims this README makes — including that
  `titania/data/` is byte-identical to the root tables, so an installed codec
  can never decode differently from an in-tree one.

**What `pytest` deliberately does not cover:** anything that needs real media.
No LDPC decode of a real codeword, no page re-encode, no FTL walk, no device
I/O. Those live in the `--selftest` blocks next to the evidence that justifies
them, and they cannot run for someone who does not have this board. The suite
enforces its own contract — a test that names a media directory fails the suite.

So: green `pytest` means the arithmetic is sound. It does **not** mean the tool
works on your media. Run the selftests for that.

---

## Known issues

Found while packaging; **reported, not fixed**, because fixing them means
editing verified decode logic:

1. ~~**The codec chain is not relocatable.**~~ **FIXED.** The eight tables now
   ship as `titania/data/` package data and resolve through `nd_tables.table()`,
   checkout first and package data second. `pip install .` builds a wheel that
   imports and decodes from any directory; verified in a throwaway venv outside
   the checkout. Only path resolution changed — no decode, addressing or
   verification logic was touched.
2. **`titania/hwdev.py` needs the checkout.** It puts the repo root on
   `sys.path` itself and imports `nd_nfif` → `tool` / `uart_client`. Those two
   are deliberately not declared as installable modules (the names are far too
   generic for a global import path), so hardware use requires running from a
   checkout. Offline use does not.

---

## Repository layout

This document covers the `titania/` package. For the repository as a whole —
the Pi Pico firmware, the board-side payloads and the UART tooling that the
NAND work grew up beside — see the [top-level README](../README.md).

The derivations behind the format were done in a much larger tree of research
scripts, which is not published. What survives here is the part that is
load-bearing: the package, the tests, and
[`NAND_DATA_FORMAT.md`](../NAND_DATA_FORMAT.md), which records what was measured
and how.

**Start with [`NAND_DATA_FORMAT.md`](../NAND_DATA_FORMAT.md)** — it walks the
address chain from host LBA down to bytes off the die, and marks every claim as
measured, disassembly-derived, self-consistent or unknown, including the
contradictions that are still open.
