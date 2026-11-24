import json
import shutil
from pathlib import Path

from perm_audit.codes import E_DENY_SHADOW, E_INHERIT_AMP, E_POLICY_DIVERGE
from perm_audit.engine import AuditEngine
from perm_audit.parser import parse_bundle
from perm_audit.pipeline import run_audit


def _report(sample_dir, tmp_path):
    result = run_audit(sample_dir, tmp_path / "workdir")
    return result["report"]


def test_key_decisions_and_hit_ace_keys(sample_dir, tmp_path):
    report = _report(sample_dir, tmp_path / "a")
    decisions = {item["sync_marker"]: item for item in report["decisions"]}

    alice = decisions["SYNC-0001"]
    assert alice["decision"] == "allow"
    assert any("A100_ENG_READ" in key for key in alice["hit_ace_keys"])

    bob_write = decisions["SYNC-0003"]
    assert bob_write["decision"] == "deny"
    assert len(bob_write["hit_ace_keys"]) == 1
    assert "A101_BOB_WRITE_DENY" in bob_write["hit_ace_keys"][0]
    assert bob_write["deny_hit_ace_keys"] == bob_write["hit_ace_keys"]


def test_truncation_and_named_missing_are_uncertain_not_silent(sample_dir, tmp_path):
    report = _report(sample_dir, tmp_path / "b")
    decisions = {item["sync_marker"]: item for item in report["decisions"]}

    append = decisions["SYNC-0010"]
    delete = decisions["SYNC-0011"]
    named_write = decisions["SYNC-0012"]

    assert append["decision"] == "allow"
    assert delete["decision"] == "uncertain"
    assert delete["uncertain_bits"][0]["name"] == "delete"
    assert delete["error_bound"] & (1 << 4)
    assert named_write["decision"] == "uncertain"
    assert named_write["uncertain_bits"][0]["name"] == "write"


def test_deny_shadow_conflict_has_two_canonical_keys(sample_dir, tmp_path):
    report = _report(sample_dir, tmp_path / "c")
    shadows = report["deny_shadows"]
    assert len(shadows) == 1
    shadow = shadows[0]
    assert shadow["code"] == E_DENY_SHADOW
    assert "A300_SHADOW_DENY" in shadow["deny_ace_key"]
    assert "A301_SHADOW_ALLOW" in shadow["allow_ace_key"]
    assert shadow["deny_ace_key"] != shadow["allow_ace_key"]
    assert shadow["covered_mask"] == 18


def test_inheritance_amplification_names_source(sample_dir, tmp_path):
    report = _report(sample_dir, tmp_path / "d")
    amps = [x for x in report["inheritance_amplifications"] if x["trustee_id"] == "u_amp"]
    assert len(amps) == 1
    amp = amps[0]
    assert amp["code"] == E_INHERIT_AMP
    assert "A400_INHERIT_ONLY_EXEC" in amp["source_ace_key"]
    assert amp["source_object_id"] == "O_AMPLIFY"
    assert amp["object_id"] == "O_AMPLIFY_CHILD"
    assert amp["amplified_mask"] == 8

    decisions = {item["sync_marker"]: item for item in report["decisions"]}
    assert decisions["SYNC-0008"]["decision"] == "allow"
    assert decisions["SYNC-0009"]["decision"] == "deny"


def test_policy_divergence_and_byte_aliases(sample_dir, tmp_path):
    report = _report(sample_dir, tmp_path / "e")
    divergence = report["policy_divergences"]
    assert len(divergence) == 1
    assert divergence[0]["code"] == E_POLICY_DIVERGE
    assert divergence[0]["sync_marker"] == "SYNC-0013"
    decisions = divergence[0]["policy_decisions"]
    values = {item["decision"] for item in decisions.values()}
    assert values == {"allow", "deny"}

    windows_aliases = report["canonical_aliases"]["windows-casefold-nfc-v1"]
    keys = {item["canonical_key"] for item in windows_aliases}
    assert "/aliases/dotfile.txt" in keys
    assert "/aliases/casefile.txt" in keys
    assert any("caf" in key for key in keys)
    assert all(item["distinct_raw_byte_forms"] >= 2 for item in windows_aliases)


def test_conflicts_are_ace_arrival_order_independent(sample_dir, tmp_path):
    shuffled = tmp_path / "shuffled_sample"
    shuffled.mkdir()
    for name in [
        "principals.jsonl",
        "objects.jsonl",
        "requests.jsonl",
        "truth.jsonl",
        "manifest.json",
    ]:
        shutil.copy(sample_dir / name, shuffled / name)

    lines = (sample_dir / "aces.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    rows = list(reversed(rows))
    (shuffled / "aces.jsonl").write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )

    original = AuditEngine(parse_bundle(sample_dir)).analyze()
    reordered = AuditEngine(parse_bundle(shuffled)).analyze()
    assert original["deny_shadows"] == reordered["deny_shadows"]
    assert original["inheritance_amplifications"] == reordered["inheritance_amplifications"]


def test_error_bounds_accumulate_from_root_to_leaf(sample_dir):
    engine = AuditEngine(parse_bundle(sample_dir))
    root = engine.compute_rights("O_ERROR_ROOT", "u_error")
    mid = engine.compute_rights("O_ERROR_MID", "u_error")
    leaf = engine.compute_rights("O_ERROR_LEAF", "u_error")

    assert root.error_bound_sum < mid.error_bound_sum < leaf.error_bound_sum
    assert root.error_bound < mid.error_bound
    assert leaf.uncertain_mask & (1 << 4)

    reports = {item["sync_marker"]: item for item in engine.build_request_reports()}
    assert reports["SYNC-0015"]["decision"] == "allow"
    assert reports["SYNC-0016"]["decision"] == "allow"
    assert reports["SYNC-0017"]["decision"] == "uncertain"
