from __future__ import annotations

import itertools
import json
from pathlib import Path

from sandbox_guard.parsing import parse_policy
from sandbox_guard.workspace import Workspace


def load_policy(sample_dir: Path):
    return parse_policy(json.loads((sample_dir / "policy.json").read_text("utf-8")))


def apply_order(root: Path, sample_dir: Path, names: tuple[str, ...]) -> Workspace:
    ws = Workspace(root)
    ws.initialize_policy(load_policy(sample_dir))
    for name in names:
        result = ws.import_batch_file(sample_dir / "patches" / name)
        assert result["status"] in {"applied", "duplicate"}
    return ws


def normalized_adjudication(ws: Workspace) -> dict:
    view = ws.unified_view()
    closure = ws.closure_report()
    return {
        "grants": [
            {
                "key": grant["canonical_key"],
                "generation": grant["generation"],
                "effective_at_ms": grant["effective_at_ms"],
                "tombstone": grant["tombstone"],
                "adjudicated_mask": grant["adjudicated_mask"],
                "effective_mask": grant["effective_mask"],
                "dependencies": grant["dependencies"],
                "authoritative_fact_id": grant["authoritative_fact_id"],
                "reduction_facts": grant["applied_reduction_fact_ids"],
            }
            for grant in view["grants"]
        ],
        "processes": [
            {
                "key": process["canonical_key"],
                "effective_mask": process["effective_mask"],
                "alive": process["alive"],
                "decision": process["decision"],
                "missing": process["missing_names"],
            }
            for process in view["processes"]
        ],
        "closure": {
            "triggers": closure["trigger_keys"],
            "grants": closure["closure_grant_keys"],
            "pids": closure["affected_pids"],
            "denied": closure["final_denied_process_keys"],
            "profiles": closure["profile_keys"],
        },
        "conflicts": [
            (conflict["canonical_key"], conflict["field"])
            for conflict in view["conflicts"]
        ],
    }


def test_fixed_sample_unified_view_conflicts_and_closure(sample_dir, tmp_path):
    patch_names = tuple(sorted(path.name for path in (sample_dir / "patches").glob("*.json")))
    ws = apply_order(tmp_path / "main", sample_dir, patch_names)
    expected = json.loads((sample_dir / "expected-summary.json").read_text("utf-8"))
    closure = ws.closure_report()

    assert [(c["canonical_key"], c["field"]) for c in closure["conflicts"]] == [
        tuple(item) for item in expected["conflict_fields"]
    ]
    assert closure["trigger_keys"] == expected["valid_patch_closure"]["trigger_keys"]
    assert closure["closure_grant_keys"] == expected["valid_patch_closure"]["closure_grant_keys"]
    assert closure["affected_pids"] == expected["valid_patch_closure"]["affected_pids"]

    view = ws.unified_view()
    by_pid = {process["pid"]: process for process in view["processes"]}
    for pid in expected["valid_patch_closure"]["denied_pids"]:
        assert by_pid[pid]["alive"] is False
    for pid in expected["valid_patch_closure"]["alive_pids"]:
        assert by_pid[pid]["alive"] is True

    revived = by_pid[expected["valid_patch_closure"]["revived_pid"]]
    assert revived["alive"] is True
    assert revived["effective_names"] == ["network_outbound"]

    all_events = ws.closure_report(include_all_events=True)
    assert "grant:g-revive" in all_events["closure_grant_keys"]
    assert "grant:g-revive" in all_events["revived_trigger_keys"]


def test_every_patch_permutation_has_same_adjudication(sample_dir, tmp_path):
    names = tuple(sorted(path.name for path in (sample_dir / "patches").glob("*.json")))
    assert len(names) == 4
    snapshots = []
    digests = set()
    for index, order in enumerate(itertools.permutations(names)):
        ws = apply_order(tmp_path / f"order-{index}", sample_dir, order)
        snapshots.append(normalized_adjudication(ws))
        verify = ws.verify_checkpoint()
        assert verify["ok"] is True
        digests.add(verify["ledger_digest"])

    first = snapshots[0]
    for snapshot in snapshots[1:]:
        assert snapshot == first
    assert len(digests) == 1


def test_invalid_batch_rejections_are_order_independent(sample_dir, tmp_path):
    valid_names = tuple(sorted(path.name for path in (sample_dir / "patches").glob("*.json")))
    invalid_path = sample_dir / "invalid" / "bad-batch.json"
    rejection_sets = []
    for position in range(len(valid_names) + 1):
        root = tmp_path / f"invalid-order-{position}"
        ws = Workspace(root)
        ws.initialize_policy(load_policy(sample_dir))
        order = list(valid_names)
        result = ws.import_batch_file(invalid_path)
        assert result["status"] == "rejected"
        rejection_sets.append(result["rejections"])
        for name in order:
            applied = ws.import_batch_file(sample_dir / "patches" / name)
            assert applied["status"] == "applied"

    first = rejection_sets[0]
    for rejections in rejection_sets[1:]:
        assert rejections == first
    codes = {item["code"] for item in first}
    assert {"E_WATERMARK", "E_GRANT_KEY", "E_V2_GENERATION", "E_DEP_KEY", "E_TARGET_KEY"} <= codes
