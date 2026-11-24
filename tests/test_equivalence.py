"""Batched / out-of-order arrival must equal from-scratch serial processing."""

import json
import random

from memguard.report import build_report

from conftest import import_all, load_truth


def _region_map(report):
    return {r["region_id"]: r for r in report["regions"]}


def test_batched_import_matches_truth(sample_dir, decl_file, batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)
    report = build_report(workdir)
    truth = load_truth(sample_dir)["expected_v1"]
    got = {}
    for region in report["regions"]:
        for kind, k in region["kinds"].items():
            got[f"{region['region_id']}|{kind}"] = {
                "total_count": k["total_count"], "oob_count": k["oob_count"],
                "oob_max_end": k["oob_max_end"], "watermark": k["watermark"],
                "dup_count": k["dup_count"], "gaps": k["gaps"],
                "evidence": k["evidence"],
            }
    assert got == truth


def test_arbitrary_arrival_order_equals_serial(sample_dir, decl_file, tmp_path):
    # Load every valid trace record and replay them in random batch splits.
    records = []
    for name in ("traces_batch1.jsonl", "traces_batch2.jsonl"):
        with open(sample_dir / name, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("access_kind") in ("read", "write") \
                        and isinstance(rec.get("base_offset"), int) \
                        and not isinstance(rec.get("base_offset"), bool) \
                        and rec["base_offset"] <= rec["arena_size"]:
                    records.append(rec)

    rng = random.Random(7)
    shuffled = records[:]
    rng.shuffle(shuffled)
    cuts = sorted(rng.sample(range(1, len(shuffled)), 4))
    parts, prev = [], 0
    for cut in cuts + [len(shuffled)]:
        parts.append(shuffled[prev:cut])
        prev = cut

    batch_dir = tmp_path / "randbatches"
    batch_dir.mkdir()
    batch_files = []
    for idx, part in enumerate(parts):
        path = batch_dir / f"rb{idx}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for rec in part:
                fh.write(json.dumps(rec) + "\n")
        batch_files.append(str(path))

    w_random = str(tmp_path / "w_random")
    import_all(w_random, [decl_file], batch_files)

    # serial reference: one single batch, sorted by (access_seq, trace_id)
    serial_path = batch_dir / "serial.jsonl"
    with open(serial_path, "w", encoding="utf-8") as fh:
        for rec in sorted(records, key=lambda r: (r["access_seq"], r["trace_id"])):
            fh.write(json.dumps(rec) + "\n")
    w_serial = str(tmp_path / "w_serial")
    import_all(w_serial, [decl_file], [str(serial_path)])

    rep_random = build_report(w_random)
    rep_serial = build_report(w_serial)
    assert _region_map(rep_random) == _region_map(rep_serial)
    # reject lists must also converge
    rand_rejects = sorted((r["line_no"], r["code"], r["detail"])
                          for r in rep_random["rejects"])
    serial_rejects = sorted((r["line_no"], r["code"], r["detail"])
                            for r in rep_serial["rejects"])
    assert rand_rejects == serial_rejects


def test_duplicate_seq_first_wins_and_gap_reported(sample_dir, decl_file,
                                                   batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)
    report = build_report(workdir)
    regions = _region_map(report)
    buf_read = regions["R_BUF"]["kinds"]["read"]
    assert buf_read["dup_count"] == 1            # duplicated seq 40
    assert buf_read["gaps"] == [[7, 7], [23, 23]]
    assert "E_SEQ_GAP" in regions["R_BUF"]["codes"]
    # R_TMP gap at seq 11 was filled by batch 2 -> no gap remains
    assert regions["R_TMP"]["kinds"]["read"]["gaps"] == []
