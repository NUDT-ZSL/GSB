"""Deterministic local acceptance verification."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager
from .codes import E_DEP_CYCLE
from .engine import AuditEngine, Batch
from .graph import DependencyGraph
from .merge import canonical_report_digest
from .model import AuditError, canonical_json
from .parser import parse_dependency_file, parse_sync_map, parse_v1_file, parse_v2_snapshot
from .storage import atomic_write_json


CORE_REPORT_FIELDS = ("integrity_view", "conflicts", "closure", "seeds", "reverse_dependents", "dependency")


def _core(report: dict[str, Any]) -> dict[str, Any]:
    return {key: report[key] for key in CORE_REPORT_FIELDS}


def verify_sample(sample_dir: str | Path, work_dir: str | Path | None = None) -> dict[str, Any]:
    sample = Path(sample_dir)
    truth = json.loads((sample / "truth.json").read_text(encoding="utf-8"))
    sync = parse_sync_map(sample / "sync_markers.json")
    v1 = parse_v1_file(sample / "v1_snapshot.json")
    v2 = parse_v2_snapshot(sample / "v2")
    edges, dep_rejections, _ = parse_dependency_file(sample / "dependencies.json")
    if v1.rejections or v2.rejections or dep_rejections:
        raise AssertionError("baseline sample must parse without rejections")

    from .merge import build_integrity_report

    direct = build_integrity_report(v1.records + v2.records, edges, sync_markers=sync)
    reversed_order = build_integrity_report(list(reversed(v2.records + v1.records)), edges, sync_markers=sync)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    check("expected_seeds", direct["seeds"] == truth["expected_seeds"], direct["seeds"])
    check("expected_closure", direct["closure"] == truth["expected_closure"], direct["closure"])
    check("expected_reverse_dependents", direct["reverse_dependents"] == truth["expected_reverse_dependents"])
    violation_pairs = {(v["from"], v["to"], v["actual_version"]) for v in direct["dependency"]["version_violations"]}
    expected_violations = {(v["from"], v["to"], v["actual_version"]) for v in truth["expected_version_violations"]}
    check("version_constraint_violations", expected_violations <= violation_pairs, sorted(violation_pairs))
    conflict_keys = {c["canonical_key"] for c in direct["conflicts"]}
    check("conflict_keys", set(truth["expected_conflict_keys"]) <= conflict_keys, sorted(conflict_keys))
    check("import_order_independent", canonical_json(_core(direct)) == canonical_json(_core(reversed_order)))

    views = {view["canonical_key"]: view for view in direct["integrity_view"]}
    for component in truth["components"]:
        key = component["canonical_key"]
        view = views[key]
        marker = component["sync_marker"]
        generation_one = next((basis for basis in view["source_basis"] if basis["generation"] == 1), None)
        check(f"sender_digest:{key}", generation_one and generation_one["digest"] == component["sender_original_digest"])
        if marker == "synced":
            check(f"marker:{marker}:{key}", view["hash"]["hash_code"] is None and view["hash"]["hash_status"] == "match", view["hash"])
        elif marker == "hmac-derived":
            check(f"marker:{marker}:{key}", view["hash"]["hash_status"] == "synchronized_derived_match" and view["hash"]["hash_code"] is None, view["hash"])
        elif marker == "digest-rotated":
            check(f"marker:{marker}:{key}", view["hash"]["hash_code"] == "E_HASH_MISMATCH" and "digest_changed" in view["seed_reasons"], view["hash"])
        elif marker == "revoked":
            check(f"marker:{marker}:{key}", view["revoked"] is True and "E_REVOKED" in view["codes"])
        elif marker == "tombstoned":
            check(f"marker:{marker}:{key}", view["tombstoned"] is True and "E_TOMBSTONE" in view["codes"])
        elif marker == "version-drift":
            check(f"marker:{marker}:{key}", view["version"] == component["expected_v2_version"] and any(c["field"] == "version" and c["canonical_key"] == key for c in direct["conflicts"]))

    invalid = parse_v1_file(sample / "invalid" / "v1_invalid_records.json")
    invalid_codes = {r.code for r in invalid.rejections}
    check("invalid_record_codes", set(truth["invalid_expected_codes"]) <= invalid_codes, sorted(invalid_codes))
    check("invalid_records_continue", len(invalid.records) == 2, len(invalid.records))

    cycle_edges, cycle_rejections, _ = parse_dependency_file(sample / "invalid" / "dependency_cycles.json")
    graph = DependencyGraph(cycle_edges)
    cycles = graph.find_cycles()
    cycle_keys = {key for cycle in cycles for key in cycle[:-1]}
    cycle_code = None
    try:
        graph.require_acyclic()
    except AuditError as exc:
        cycle_code = exc.code
    check("dependency_cycle_code", cycle_code == E_DEP_CYCLE and {"pkg:generic/cycle-a", "pkg:generic/cycle-b", "pkg:generic/cycle-c", "pkg:generic/self-loop"} <= cycle_keys, cycles)
    check("cycle_parse_without_edge_rejections", not cycle_rejections, [r.to_dict() for r in cycle_rejections])

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="sbom-verify-"))
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ledger_path = work / "ledger.sqlite3"
    checkpoint_path = work / "checkpoint.json"
    engine = AuditEngine(ledger_path, sync)
    dep_batch = Batch(batch_id="deps", edges=edges)
    v1_batch = Batch(batch_id="v1", records=v1.records)
    v2_batch = Batch(batch_id="v2", records=v2.records)
    for batch in (dep_batch, v1_batch, v2_batch):
        result = engine.apply_batch(batch)
        check(f"apply:{batch.batch_id}", result["status"] in {"applied", "partial"}, result)

    incremental_data = json.loads((sample / "incremental" / "revoke_lib_util.json").read_text(encoding="utf-8"))
    inc_result = engine.apply_batch(Batch(batch_id=incremental_data["batch_id"], records=incremental_data["records"]))
    check("incremental_no_divergence", inc_result.get("code") != "E_RECOMPUTE_DIVERGED", inc_result)
    check("incremental_equivalence_flag", bool(inc_result.get("incremental", {}).get("equivalent_to_full_recompute")), inc_result.get("incremental"))
    check("incremental_expected_closure", engine.report and engine.report["closure"] == truth["incremental_expected_closure"], engine.report and engine.report["closure"])

    fresh_records = list(v1.records) + list(v2.records) + incremental_data["records"]
    fresh = build_integrity_report(fresh_records, edges, sync_markers=sync)
    assert engine.report is not None
    check("incremental_vs_full_fields", canonical_json(_core(engine.report)) == canonical_json(_core(fresh)), _first_diff(_core(engine.report), _core(fresh)))

    duplicate = engine.apply_batch(Batch(batch_id="duplicate-util-delivery", records=incremental_data["records"]))
    check("duplicate_counted_first_wins", duplicate["duplicate_count"] >= 1, duplicate)
    check("duplicate_does_not_diverge", duplicate.get("code") != "E_RECOMPUTE_DIVERGED", duplicate)

    manager = CheckpointManager(ledger_path, checkpoint_path)
    checkpoint = manager.save(engine.report)
    verification = manager.verify()
    check("checkpoint_valid", verification["valid"] is True, verification)
    check("checkpoint_digest", verification["view_digest"] == canonical_report_digest(engine.report))

    temp_file = checkpoint_path.parent / f".{checkpoint_path.name}.simulated-half-write.tmp"
    temp_file.write_text('{"checkpoint_version":', encoding="utf-8")
    half_write_verify = manager.verify()
    check("half_write_residue_rejected", any(r["code"] == "E_HALF_WRITE_RESIDUE" for r in half_write_verify["half_write_residues"]), half_write_verify)

    corrupted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    corrupted["merge_policy_version"] = "merge-policy-v0"
    atomic_write_json(checkpoint_path, corrupted)
    version_verify = manager.verify()
    check("policy_version_forces_rebuild", version_verify.get("code") == "E_CHECKPOINT_VERSION" and version_verify.get("rebuild_required") is True, version_verify)

    result = {
        "verified": True,
        "sample_dir": str(sample),
        "work_dir": str(work),
        "checks_passed": len(checks),
        "expected_closure": truth["expected_closure"],
        "incremental_closure": truth["incremental_expected_closure"],
        "view_digest": checkpoint["view_digest"],
        "checks": checks,
    }
    atomic_write_json(work / "verification-result.json", result)
    return result


def _first_diff(left: Any, right: Any, path: str = "$") -> dict[str, Any]:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return {"path": f"{path}.{key}", "issue": "missing"}
            diff = _first_diff(left[key], right[key], f"{path}.{key}")
            if diff:
                return diff
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return {"path": path, "left_len": len(left), "right_len": len(right)}
        for i, (a, b) in enumerate(zip(left, right)):
            diff = _first_diff(a, b, f"{path}[{i}]")
            if diff:
                return diff
    elif left != right:
        return {"path": path, "left": repr(left)[:160], "right": repr(right)[:160]}
    return {}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify deterministic sample acceptance")
    parser.add_argument("--sample-dir", default="samples/generated")
    parser.add_argument("--work-dir", default=".verify-work")
    parser.add_argument("--json", action="store_true", help="print full JSON result")
    args = parser.parse_args()
    result = verify_sample(args.sample_dir, args.work_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"PASS: {result['checks_passed']} deterministic checks")
        print(f"closure: {len(result['expected_closure'])} components")
        print(f"incremental closure: {len(result['incremental_closure'])} components")
        print(f"result: {Path(result['work_dir']) / 'verification-result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
