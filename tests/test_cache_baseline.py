"""Requirement 5 (cache invalidation sources) and baseline comparison."""

import json
import os

from conftest import fresh_workbench, make_workspace


def test_reuse_without_changes(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()
    second = wb.run()
    ok = [e for e in second["requests"]]
    # req-006 always fails on the injected empty score entry, so it is never
    # cached; every decodable request is reused without re-decoding.
    assert all(e["cache"]["status"] == "reused" for e in ok)
    assert second["cache_stats"]["hits"] == 5
    assert second["cache_stats"]["invalidations"]["missing"] == 1


def test_request_level_invalidation(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()
    wb.apply_edit({"kind": "override", "request_id": "req-003", "fields": {"seed": 7}})
    result = wb.run()
    by_id = {e["request_id"]: e for e in result["requests"]}
    assert by_id["req-003"]["cache"] == {
        "status": "recomputed",
        "invalidation": "request",
    }
    for rid in ("req-001", "req-002", "req-004", "req-005"):
        assert by_id[rid]["cache"]["status"] == "reused"


def test_scoretable_level_invalidation(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()

    # Perturb the score table: one logit changes -> table fingerprint changes.
    scores_path = os.path.join(root, "samples", "scores.jsonl")
    lines = open(scores_path, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[0])
    rec["steps"][0][0] += 0.25
    lines[0] = json.dumps(rec)
    with open(scores_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    # Fresh process on the same workdir: prev state recovered from manifest.
    wb2 = fresh_workbench(root, config)
    result = wb2.run()
    for entry in result["requests"]:
        assert entry["cache"] == {
            "status": "recomputed",
            "invalidation": "scoretable",
        }, entry["request_id"]


def test_policy_level_invalidation_classification(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()
    wb.apply_edit({"kind": "global_set", "fields": {"top_k": 5}})
    result = wb.run()
    # All six requests (including the failing req-006) are policy-invalidated.
    assert result["cache_stats"]["invalidations"]["policy"] == 6
    for entry in result["requests"]:
        assert entry["cache"]["invalidation"] == "policy"


def test_baseline_matches(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    result = wb.run()
    with open(os.path.join(root, "samples", "baseline.json"), encoding="utf-8") as fh:
        baseline = json.load(fh)

    tokens = {e["request_id"]: e["tokens"] for e in result["requests"]}
    finish = {e["request_id"]: e["finish_reason"] for e in result["requests"]}
    assert tokens == baseline["expected_tokens"]
    assert finish == baseline["expected_finish"]
    assert result["injection_hits"] == baseline["injection_hits"]
    assert sorted({f["code"] for f in result["failures"]}) == baseline["failure_codes"]

    # Downgrade loss manifest of the sample v2 policy matches the baseline.
    from workbench.convert import convert_policy
    from workbench.policy import parse_policy_text

    with open(os.path.join(root, "samples", "policy_v2.json"), encoding="utf-8") as fh:
        v2_policy = parse_policy_text(fh.read(), "v2")
    _, sidecar, losses = convert_policy(v2_policy, "v1")
    assert [e.to_dict() for e in losses] == baseline["downgrade_losses"]
    assert sidecar == baseline["downgrade_sidecar"]
