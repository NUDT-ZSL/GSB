from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

from offline_authz.engine import adjudicate_bundle
from offline_authz.graph import build_closure_report
from offline_authz.parsing import load_bundle
from offline_authz.policy import build_unified_view


def _truth(sample_dir: Path, name: str):
    return json.loads((sample_dir / "truth" / name).read_text(encoding="utf-8"))


def test_rejections_match_stable_truth(sample_dir: Path) -> None:
    bundle = load_bundle(sample_dir)
    build_unified_view(bundle)
    expected = {
        (item["source_category"], item["record_seq"], item["code"])
        for item in _truth(sample_dir, "expected_rejections.json")["rejections"]
    }
    actual = {
        (item["source_category"], item["record_seq"], item["code"])
        for item in bundle.rejections
    }
    assert expected <= actual


def test_malformed_jsonl_line_is_isolated(sample_dir: Path, tmp_path: Path) -> None:
    copied = tmp_path / "sample"
    shutil.copytree(sample_dir, copied)
    with (copied / "user_credentials.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"user_id": "broken",\n')
    bundle = load_bundle(copied)
    json_errors = [r for r in bundle.rejections if r["code"] == "E_INVALID_JSON"]
    assert json_errors
    assert json_errors[0]["source_category"] == "user_credential"
    assert json_errors[0]["record_seq"] == 16
    assert bundle.users


def test_decisions_match_truth_and_are_concurrency_independent(sample_dir: Path) -> None:
    bundle = load_bundle(sample_dir)
    view = build_unified_view(bundle)
    serial = adjudicate_bundle(bundle, view, workers=1)
    concurrent = adjudicate_bundle(bundle, view, workers=4)
    assert json.dumps(serial, sort_keys=True, ensure_ascii=False) == json.dumps(
        concurrent, sort_keys=True, ensure_ascii=False
    )
    truth = _truth(sample_dir, "expected_decisions.json")["decisions"]
    actual = {(row["user_id"], row["attempt_seq"]): row for row in serial}
    for expected in truth:
        row = actual[(expected["user_id"], expected["attempt_seq"])]
        assert row["decision"] == expected["decision"]
        assert row["codes"] == expected["codes"]
        assert row["canonical_fingerprint_key"]
        assert "fingerprint_error_bound" in row


def test_v1_relative_expiry_uses_observation_clock(sample_dir: Path) -> None:
    bundle = load_bundle(sample_dir)
    view = build_unified_view(bundle)
    policy = view["policy_groups"]["policy:mfa-push"]["revisions"][0]
    assert policy["expires_at_ms"] == bundle.observation_clock_ms + 8 * 3_600_000


def test_closure_contains_truth_nodes_cycles_and_is_order_independent(sample_dir: Path) -> None:
    bundle = load_bundle(sample_dir)
    view = build_unified_view(bundle)
    first = build_closure_report(bundle, view, watermark=2)

    rng = random.Random(7)
    rng.shuffle(bundle.users)
    rng.shuffle(bundle.devices)
    rng.shuffle(bundle.factors)
    rng.shuffle(bundle.edges)
    rng.shuffle(bundle.revocations)
    second = build_closure_report(bundle, view, watermark=2)

    assert first["closure_nodes"] == second["closure_nodes"]
    assert [c["nodes"] for c in first["cycles"]] == [c["nodes"] for c in second["cycles"]]
    truth = _truth(sample_dir, "expected_closure.json")
    closure_nodes = set(first["closure_nodes"])
    assert set(truth["must_contain_nodes"]) <= closure_nodes
    cycle_types = {cycle["cycle_type"] for cycle in first["cycles"]}
    assert set(truth["must_contain_cycle_types"]) <= cycle_types
    cycle_nodes = {node for cycle in first["cycles"] for node in cycle["nodes"]}
    assert set(truth["factor_cycle_nodes"] + truth["binding_cycle_nodes"]) <= cycle_nodes
