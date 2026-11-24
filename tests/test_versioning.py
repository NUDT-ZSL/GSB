"""Declaration evolution: generation arbitration, tombstones, affected-set
recompute equal to a from-scratch full rebuild."""

import json

from memguard import codes
from memguard.engine import import_batch
from memguard.report import build_report, query_region

from conftest import import_all, load_truth


def test_generation_evolution_and_tombstone(sample_dir, decl_file, batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)

    before = build_report(workdir)
    io_before = {r["region_id"]: r for r in before["regions"]}["R_IO"]
    assert io_before["verdict"] == "CLEAN"

    # evolve: R_IO shrinks, R_OLD tombstoned
    v2 = str(sample_dir / "declarations_v2.json")
    outcome = import_batch(workdir, [v2], [])
    assert outcome["batch_seq"] is not None

    after = build_report(workdir)
    regions = {r["region_id"]: r for r in after["regions"]}
    assert "R_OLD" not in regions                       # tombstoned -> removed
    io_after = regions["R_IO"]
    assert io_after["verdict"] == "VIOLATION"
    assert io_after["kinds"]["write"]["codes"] == ["E_BOUND_OVER"]
    ev = io_after["kinds"]["write"]["evidence"]
    assert len(ev) == 1 and ev[0]["base_offset"] == 2440

    # affected-set recompute must equal a from-scratch full rebuild
    fresh = str(tmp_path / "w_fresh")
    import_all(fresh, [decl_file, v2], batch_files)
    fresh_report = build_report(fresh)
    assert {r["region_id"]: r for r in after["regions"]} == \
           {r["region_id"]: r for r in fresh_report["regions"]}

    # and it must match the independent v2 truth
    truth = load_truth(sample_dir)["expected_v2"]
    got = {}
    for region in after["regions"]:
        for kind, k in region["kinds"].items():
            got[f"{region['region_id']}|{kind}"] = {
                "total_count": k["total_count"], "oob_count": k["oob_count"],
                "oob_max_end": k["oob_max_end"], "watermark": k["watermark"],
                "dup_count": k["dup_count"], "gaps": k["gaps"],
                "evidence": k["evidence"],
            }
    assert got == truth


def test_tombstone_overrides_earlier_generation_only(tmp_path):
    decls = {"regions": [
        {"region_id": "X", "generation": 3, "arena_size": 64, "start": 0,
         "end": 32, "life_start_ms": 0, "life_end_ms": None, "parent_id": None},
        {"region_id": "X", "generation": 2, "tombstone": True, "arena_size": 64,
         "start": 0, "end": 32, "life_start_ms": 0, "life_end_ms": None,
         "parent_id": None},
    ]}
    path = tmp_path / "d.json"
    path.write_text(json.dumps(decls), encoding="utf-8")
    workdir = str(tmp_path / "w")
    outcome = import_batch(workdir, [str(path)], [])
    assert outcome["batch_seq"] is not None
    # generation 3 (non-tombstone) beats generation 2 tombstone
    assert query_region(workdir, "X")["verdict"] == "CLEAN"


def test_region_cycle_aborts_batch_without_touching_ledger(sample_dir, decl_file,
                                                           batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)
    before = build_report(workdir)

    cyclic = {"regions": [
        {"region_id": "C1", "generation": 1, "arena_size": 64, "start": 0,
         "end": 32, "life_start_ms": 0, "life_end_ms": None, "parent_id": "C2"},
        {"region_id": "C2", "generation": 1, "arena_size": 64, "start": 32,
         "end": 64, "life_start_ms": 0, "life_end_ms": None, "parent_id": "C1"},
    ]}
    cpath = tmp_path / "cyclic.json"
    cpath.write_text(json.dumps(cyclic), encoding="utf-8")
    outcome = import_batch(workdir, [str(cpath)], [])
    assert outcome["batch_seq"] is None
    assert outcome["code"] == codes.E_REGION_CYCLE
    cycle_reject = [r for r in outcome["rejects"] if r["code"] == codes.E_REGION_CYCLE]
    assert cycle_reject and "C1" in cycle_reject[0]["detail"] \
        and "C2" in cycle_reject[0]["detail"]

    after = build_report(workdir)
    # ledger state untouched by the failed batch
    assert after["regions"] == before["regions"]
    assert after["batch_watermark"] == before["batch_watermark"]


def test_unknown_parent_rejected(tmp_path):
    decls = {"regions": [
        {"region_id": "ORPHAN", "generation": 1, "arena_size": 64, "start": 0,
         "end": 32, "life_start_ms": 0, "life_end_ms": None, "parent_id": "GHOST"},
    ]}
    path = tmp_path / "d.json"
    path.write_text(json.dumps(decls), encoding="utf-8")
    outcome = import_batch(str(tmp_path / "w"), [str(path)], [])
    assert outcome["batch_seq"] is None
    assert outcome["code"] == codes.E_PARENT_UNKNOWN
