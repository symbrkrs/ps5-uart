"""`titania.extract.Manifest` -- the resume log.

At ~47 MiB/hour off the live board, a run that dies at hour three must not start
over, so the manifest is the difference between a usable tool and an unusable
one.  All of it is testable with a tmp file and no media at all.

THE ORDERING RULE, which these tests encode: data bytes hit the output file
BEFORE the manifest line that claims them.  So the manifest is always a SUBSET
of what is on disk, never a superset -- a crash loses records and those LMAs get
redone (safe), rather than losing bytes the manifest swears are there (not).
"""
import json

import pytest


@pytest.fixture
def header():
    return {"mso": 40, "base_lma": 0, "lma_bytes": 4096,
            "layout": "sparse", "fill": "zero", "out": "user.bin"}


def read_lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_new_manifest_writes_a_header_line(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.close()
    lines = read_lines(p)
    assert len(lines) == 1
    assert lines[0]["kind"] == extract.MANIFEST_KIND
    assert lines[0]["v"] == extract.MANIFEST_VERSION
    assert lines[0]["mso"] == 40
    assert m.resumed is False


def test_records_survive_a_reopen(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    for lma in range(5):
        m.add({"lma": lma, "st": extract.VERIFIED, "off": lma * 4096,
               "len": 4096})
    m.close()

    m2 = extract.Manifest(p, header)
    assert m2.resumed is True
    assert len(m2.records) == 5
    assert m2.status(3) == extract.VERIFIED
    assert all(m2.done(lma) for lma in range(5))
    assert not m2.done(99)
    m2.close()


def test_identity_mismatch_refuses_to_resume(extract, tmp_path, header):
    """Resuming into a manifest with a different base_lma would put every byte
    at the wrong offset.  It must raise, not warn."""
    p = tmp_path / "m.jsonl"
    extract.Manifest(p, header).close()
    for field, value in [("mso", 41), ("base_lma", 8), ("lma_bytes", 512),
                         ("layout", "packed"), ("fill", "skip"),
                         ("out", "other.bin")]:
        bad = dict(header, **{field: value})
        with pytest.raises(extract.ResumeMismatch) as e:
            extract.Manifest(p, bad)
        assert field in str(e.value)


def test_widening_the_lma_range_is_allowed(extract, tmp_path, header):
    """Deliberately NOT part of IDENTITY: a later run covering more LMAs must
    add work, not be refused."""
    assert "first_lma" not in extract.Manifest.IDENTITY
    assert "last_lma" not in extract.Manifest.IDENTITY
    assert "base_lma" in extract.Manifest.IDENTITY
    p = tmp_path / "m.jsonl"
    extract.Manifest(p, dict(header, first_lma=0, last_lma=10)).close()
    m = extract.Manifest(p, dict(header, first_lma=0, last_lma=999))
    assert m.resumed is True
    m.close()


def test_a_torn_final_line_is_dropped_not_fatal(extract, tmp_path, header):
    """JSONL survives being killed halfway; that is why it was chosen."""
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    for lma in range(4):
        m.add({"lma": lma, "st": extract.VERIFIED, "off": lma * 4096})
    m.close()
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"lma": 4, "st": "verif')          # killed mid-write

    m2 = extract.Manifest(p, header)
    assert m2.resumed is True
    assert len(m2.records) == 4                      # the torn record is gone
    assert 4 not in m2.records
    m2.add({"lma": 4, "st": extract.VERIFIED, "off": 4 * 4096})
    m2.close()
    # the file must still be wholly parseable afterwards
    lines = read_lines(p)
    assert lines[0]["kind"] == extract.MANIFEST_KIND
    assert sum(1 for x in lines if "lma" in x) == 5


def test_a_file_that_is_not_a_manifest_is_refused(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    p.write_text('{"kind": "something-else"}\n')
    with pytest.raises(extract.ResumeMismatch) as e:
        extract.Manifest(p, header)
    assert "not a titania extract manifest" in str(e.value)


def test_an_unreadable_file_is_refused_rather_than_silently_restarted(
        extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    p.write_bytes(b"\x00\x01\x02 not json at all\n")
    with pytest.raises(extract.ResumeMismatch):
        extract.Manifest(p, header)


def test_restart_discards(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.add({"lma": 1, "st": extract.VERIFIED, "off": 0})
    m.close()
    m2 = extract.Manifest(p, header, restart=True)
    assert m2.resumed is False
    assert m2.records == {}
    m2.close()


def test_retry_revisits_only_the_non_final_statuses(extract, tmp_path, header):
    """`verified`/`corrected`/`unmapped` are done; everything else CAN change.

    A better WERU rule turns `unresolved` into an address and a grown dump turns
    `unreadable` into data, so --retry must revisit those and only those.
    """
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    statuses = list(extract.STATUSES)
    for i, st in enumerate(statuses):
        m.add({"lma": i, "st": st, "off": i * 4096})
    m.close()

    m2 = extract.Manifest(p, header)
    for i, st in enumerate(statuses):
        assert m2.done(i, retry=False) is True             # plain resume: all
        assert m2.done(i, retry=True) is (st in extract.FINAL_STATUSES)
    assert set(extract.FINAL_STATUSES) == {extract.VERIFIED, extract.CORRECTED,
                                           extract.UNMAPPED}
    m2.close()


def test_next_offset_counts_only_written_statuses(extract, tmp_path, header):
    """Packed layout appends after the last record that actually wrote bytes."""
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.add({"lma": 0, "st": extract.VERIFIED, "off": 0, "len": 4096})
    m.add({"lma": 1, "st": extract.CORRECTED, "off": 4096, "len": 4096})
    m.add({"lma": 2, "st": extract.UNREADABLE, "off": 8192, "len": 4096})
    m.flush()
    assert set(extract.WRITTEN_STATUSES) == {extract.VERIFIED,
                                             extract.CORRECTED}
    assert m.next_offset() == 8192            # not 12288: lma 2 wrote nothing
    m.close()


def test_tally_counts_by_status(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    for lma in range(3):
        m.add({"lma": lma, "st": extract.VERIFIED})
    m.add({"lma": 9, "st": extract.FAILED})
    m.flush()
    assert m.tally() == {extract.VERIFIED: 3, extract.FAILED: 1}
    m.close()


def test_a_re_added_lma_replaces_rather_than_duplicates(extract, tmp_path,
                                                        header):
    """The last record for an LMA wins; both lines stay on disk as history."""
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.add({"lma": 7, "st": extract.UNREADABLE})
    m.add({"lma": 7, "st": extract.VERIFIED, "off": 0, "len": 4096})
    m.close()
    m2 = extract.Manifest(p, header)
    assert len(m2.records) == 1
    assert m2.status(7) == extract.VERIFIED
    assert sum(1 for x in read_lines(p) if x.get("lma") == 7) == 2
    m2.close()


def test_runs_accumulate_across_resumes(extract, tmp_path, header):
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.begin_run({"argv": "first"})
    m.close()
    m2 = extract.Manifest(p, header)
    assert len(m2.runs) == 1
    m2.begin_run({"argv": "second"})
    m2.close()
    m3 = extract.Manifest(p, header)
    assert [r["argv"] for r in m3.runs] == ["first", "second"]
    m3.close()


def test_add_signals_the_batch_boundary(extract, tmp_path, header):
    """`add` returns True when the caller should flush -- that is the hook the
    ordering rule hangs off."""
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header, batch=4)
    got = [m.add({"lma": i, "st": extract.VERIFIED}) for i in range(4)]
    assert got == [False, False, False, True]
    m.close()


def test_synthesised_records_stay_distinguishable(extract, tmp_path, header):
    """A filled LMA is marked and must not read back as a real one."""
    p = tmp_path / "m.jsonl"
    m = extract.Manifest(p, header)
    m.add({"lma": 0, "st": extract.VERIFIED, "off": 0, "len": 4096})
    m.add({"lma": 1, "st": extract.UNREADABLE, "off": 4096, "len": 4096,
           "syn": True})
    m.close()
    recs = extract.Manifest(p, header).records
    assert recs[1].get("syn") is True
    assert recs[0].get("syn") is None
