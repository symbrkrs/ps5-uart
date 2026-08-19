#!/usr/bin/env python3
"""verify_adjudicate.py -- the end-to-end guard for rs/tnldpc.

    py rs/tnldpc/verify_adjudicate.py kp_store            # Rust decoder
    py rs/tnldpc/verify_adjudicate.py kp_store --numpy    # the reference

`adjudicate()` is what the decoder exists for, and what it decides is a
FUNCTION OF THE CORRECTION COUNTS, so "the decoder still converges" is not the
property that has to hold -- "the decoder still returns the same counts, and
therefore the same winners" is.  This prints the wall time and a digest over
every record: key, winner, basis, reason, and the SHA-256 of the winning bytes.

Run it both ways and diff the digest.  Anything that moves it has changed a
verdict about which copy of a page is authoritative, whatever the timings say.
"""
import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--numpy", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    if a.numpy:
        os.environ["TITANIA_NO_RUST"] = "1"

    from titania.dumpdev import DumpDevice

    t0 = time.perf_counter()
    dev = DumpDevice(a.root)
    t_idx = time.perf_counter() - t0

    t0 = time.perf_counter()
    adj = dev.adjudicate(limit=a.limit)
    t_adj = time.perf_counter() - t0

    h = hashlib.sha256()
    rows = []
    for r in adj:
        key = getattr(r, "key", None)
        win = getattr(r, "winner", None)
        basis = getattr(r, "basis", None)
        reason = getattr(r, "reason", None)
        wname = Path(str(win)).name if win is not None else "-"
        try:
            wsha = hashlib.sha256(Path(str(win)).read_bytes()).hexdigest()
        except Exception:                                   # noqa: BLE001
            wsha = "-"
        line = "%r|%s|%s|%s|%s" % (key, wname, basis, reason, wsha)
        rows.append(line)
        h.update(line.encode())

    print("  index build  : %8.3f s  (%d pages)" % (t_idx, len(dev.entries)))
    print("  adjudicate() : %8.3f s  (%d records)  [%s]"
          % (t_adj, len(adj), "numpy" if a.numpy else "rust"))
    print("  DIGEST       : %s" % h.hexdigest())
    for line in rows[:5]:
        print("    " + line[:150])
    dev.close()
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv[1:])
    finally:
        try:
            import nd_ldpc_rs
            nd_ldpc_rs.close()
        except Exception:                                   # noqa: BLE001
            pass
    sys.exit(rc)
