from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def provision(tmp_path: Path, name: str = "data") -> Path:
    data_dir = tmp_path / name
    shutil.copytree(ROOT / "data", data_dir)
    state_dir = data_dir / "state"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    for path in (data_dir / "shards").glob("*.sqlite*"):
        path.unlink()
    lock = data_dir / ".shard-router.lock"
    if lock.exists():
        lock.unlink()
    return data_dir


def run_cli(
    data_dir: Path,
    *args: str,
    timeout: float = 30.0,
) -> tuple[int, dict | None, str]:
    command = [
        sys.executable,
        "-m",
        "shard_router",
        *args,
        "--data-dir",
        str(data_dir),
        "--compact",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = json.loads(completed.stdout) if completed.stdout.strip() else None
    return completed.returncode, output, completed.stdout


def load(data_dir: Path, *extra: str) -> dict:
    code, output, raw = run_cli(data_dir, "load", *extra)
    assert code == 0, raw
    assert output is not None
    return output


def apply_snapshot(data_dir: Path, *extra: str) -> tuple[int, dict, str]:
    return run_cli(data_dir, "apply", *extra)


def query(data_dir: Path, template: str, params: dict, *extra: str) -> tuple[int, dict, str]:
    return run_cli(
        data_dir,
        "query",
        "--template",
        template,
        "--params",
        json.dumps(params, sort_keys=True),
        *extra,
    )


def status(data_dir: Path, *extra: str) -> dict:
    code, output, raw = run_cli(data_dir, "status", *extra)
    assert code == 0, raw
    assert output is not None
    return output


def mutate_json(path: Path, mutator) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutator(value)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def assert_no_persistent_state(data_dir: Path) -> None:
    assert not (data_dir / "state").exists()
    assert not list((data_dir / "shards").glob("*.sqlite*"))


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda value: value["logical_tables"][0].__setitem__("shard_key", "missing"),
            "E_SHARD_KEY_MISSING",
        ),
        (
            lambda value: value["logical_tables"][1].__setitem__(
                "range_boundaries", [0, 400, 300, 900, 1200]
            ),
            "E_BOUNDARIES_INVALID",
        ),
        (
            lambda value: value["logical_tables"][0].__setitem__("shard_count", 0),
            "E_MODULUS_INVALID",
        ),
    ],
)
def test_invalid_logical_samples_do_not_persist(
    tmp_path: Path, mutator, expected_code: str
) -> None:
    data_dir = provision(tmp_path)
    mutate_json(data_dir / "logical_tables.json", mutator)
    code, output, _ = run_cli(data_dir, "load")
    assert code != 0
    assert output is not None and output["code"] in {
        expected_code,
        "E_DUPLICATE_LOGICAL_TABLE",
    }
    assert_no_persistent_state(data_dir)


def test_invalid_v2_digest_does_not_persist(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    database = data_dir / "rules" / "v2" / "meta.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE v2_rules SET content_digest=? WHERE version='rule-v2'",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    code, output, _ = run_cli(data_dir, "load")
    assert code == 2
    assert output is not None and output["code"] == "E_V2_DIGEST_MISMATCH"
    assert_no_persistent_state(data_dir)


def test_invalid_mapping_does_not_persist(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    mutate_json(
        data_dir / "physical_mapping.json",
        lambda value: value["logical_tables"]["ledger"].pop(),
    )
    code, output, _ = run_cli(data_dir, "load")
    assert code == 2
    assert output is not None and output["code"] == "E_MAPPING_INCOMPLETE"
    assert_no_persistent_state(data_dir)


def test_invalid_seed_constraints_do_not_persist(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    mutate_json(
        data_dir / "seed_data.json",
        lambda value: value["rows"]["ledger"][0].pop("status"),
    )
    code, output, _ = run_cli(data_dir, "load")
    assert code == 2
    assert output is not None and output["code"] == "E_VALIDATION"
    assert output["details"]["table"] == "ledger"
    assert_no_persistent_state(data_dir)


def test_snapshot_interoperability_and_rewrite_clipping(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)

    v1_code, v1_route, v1_raw = run_cli(
        data_dir,
        "route",
        "--template",
        "ledger_by_user",
        "--params",
        json.dumps({"user_id": 5}),
        "--source",
        "v1",
    )
    v2_code, v2_route, v2_raw = run_cli(
        data_dir,
        "route",
        "--template",
        "ledger_by_user",
        "--params",
        json.dumps({"user_id": 5}),
        "--source",
        "v2",
    )
    assert v1_code == v2_code == 0, (v1_raw, v2_raw)
    assert v1_route["plan"]["target_shards"] == [1]
    assert v1_route["plan"]["target_shards"] == v2_route["plan"]["target_shards"]
    assert (
        v1_route["plan"]["shards"][0]["statement_digest"]
        == v2_route["plan"]["shards"][0]["statement_digest"]
    )

    code, route, raw = run_cli(
        data_dir,
        "route",
        "--template",
        "event_range",
        "--params",
        json.dumps({"low": 250, "high": 650}),
    )
    assert code == 0, raw
    assert route["plan"]["target_shards"] == [0, 1, 2]
    first = route["plan"]["shards"][0]
    assert first["pruned"] is True
    assert first["predicate_after"] == (
        "event_id BETWEEN :__sr_0_low AND :__sr_0_high"
    )
    assert first["params"]["__sr_0_low"] == 250
    assert first["params"]["__sr_0_high"] == 299

    all_code, all_route, all_raw = run_cli(
        data_dir,
        "route",
        "--template",
        "ledger_all",
    )
    assert all_code == 0, all_raw
    assert all_route["plan"]["all_route"] is True
    assert all_route["plan"]["target_shards"] == [0, 1, 2, 3]


def test_v1_and_v2_snapshots_load_independently(tmp_path: Path) -> None:
    v1_dir = provision(tmp_path, "v1-data")
    v2_dir = provision(tmp_path, "v2-data")
    shutil.rmtree(v1_dir / "rules" / "v2")
    (v2_dir / "rules" / "v1.json").unlink()
    load(v1_dir, "--source", "v1")
    load(v2_dir, "--source", "v2")

    v1_code, v1_result, v1_raw = query(
        v1_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--query-id",
        "independent-v1",
    )
    v2_code, v2_result, v2_raw = query(
        v2_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--query-id",
        "independent-v1",
    )
    assert v1_code == v2_code == 0, (v1_raw, v2_raw)
    assert v1_result["rule_version"] == v2_result["rule_version"] == "rule-v1"
    assert v1_result["rule_source"] == "v1"
    assert v2_result["rule_source"] == "v2"
    assert v1_result["target_shards"] == v2_result["target_shards"] == [1]
    assert v1_result["aggregate"] == v2_result["aggregate"]
    assert v1_result["rows"] == v2_result["rows"]


def test_cross_shard_aggregation_idempotency_and_stable_status(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    code, first, raw = query(
        data_dir,
        "ledger_range",
        {"low": 2, "high": 9},
        "--query-id",
        "aggregate-q",
    )
    assert code == 0, raw
    assert first["outcome"] == "success"
    assert first["aggregate"] == {"count": 6, "sum": 4000}
    assert [row["id"] for row in first["rows"]] == [3, 5, 6, 7, 9, 10]
    assert first["merge_order"]["shards"] == [0, 1, 2, 3]

    code2, second, raw2 = query(
        data_dir,
        "ledger_range",
        {"low": 2, "high": 9},
        "--query-id",
        "aggregate-q",
    )
    assert code2 == 0, raw2
    assert second == first

    state1 = status(data_dir)
    state2 = status(data_dir)
    assert state1 == state2
    record = next(item for item in state1["queries"] if item["query_id"] == "aggregate-q")
    assert all(shard["attempt"] == 1 for shard in record["shards"])
    assert state1["counts"]["committed"] == 1


def test_hot_update_queue_activation_idempotency_and_rollback(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)

    code, queued, raw = apply_snapshot(data_dir)
    assert code == 0, raw
    assert queued["result"]["action"] == "queued"
    assert queued["current_rule"]["version"] == "rule-v1"
    queued_state = status(data_dir)
    rule_v2 = next(item for item in queued_state["rules"] if item["version"] == "rule-v2")
    assert rule_v2["lifecycle"] == "queued"

    code, applied, raw = apply_snapshot(data_dir, "--now-ms", "2000000")
    assert code == 0, raw
    assert applied["result"]["action"] == "applied"
    assert applied["current_rule"]["version"] == "rule-v2"
    assert applied["current_rule"]["pointer_seq"] == 2

    code, repeated, raw = apply_snapshot(data_dir, "--now-ms", "2000000")
    assert code == 0, raw
    assert repeated["result"]["action"] == "idempotent"
    assert repeated["current_rule"]["pointer_seq"] == 2

    code, rollback, raw = apply_snapshot(
        data_dir,
        "--version",
        "rule-v1",
        "--now-ms",
        "2000001",
    )
    assert code == 4
    assert rollback["code"] == "E_ROLLBACK_REJECTED"

    code, versioned, raw = query(
        data_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--now-ms",
        "2000000",
    )
    assert code == 0, raw
    assert versioned["rule_version"] == "rule-v2"

    final_state = status(data_dir)
    assert final_state["current_rule"]["version"] == "rule-v2"
    assert any(
        decision["action"] == "rejected"
        and decision["details"]["code"] == "E_ROLLBACK_REJECTED"
        for decision in final_state["rule_decisions"]
    )


def test_inflight_query_keeps_bound_rule_across_hot_update(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    pause_manifest = tmp_path / "pause.json"
    pause_manifest.write_text(
        json.dumps(
            {
                "injections": [
                    {
                        "id": "pause",
                        "stage": "execute",
                        "template_id": "ledger_all",
                        "shard_no": 0,
                        "error_type": "execution_failed",
                        "code": "E_EXECUTION_FAILED",
                        "action": "delay",
                        "delay_ms": 1800,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "shard_router",
        "query",
        "--template",
        "ledger_all",
        "--query-id",
        "binding-q",
        "--injections",
        str(pause_manifest),
        "--data-dir",
        str(data_dir),
        "--compact",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10
        seen = False
        while time.time() < deadline:
            current = status(data_dir)
            if any(item["query_id"] == "binding-q" for item in current["queries"]):
                seen = True
                break
            time.sleep(0.02)
        assert seen
        code, applied, raw = apply_snapshot(data_dir, "--now-ms", "2000000")
        assert code == 0, raw
        assert applied["current_rule"]["version"] == "rule-v2"
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["query_id"] == "binding-q"
        assert result["rule_version"] == "rule-v1"
        assert status(data_dir)["current_rule"]["version"] == "rule-v2"
    finally:
        if process.poll() is None:
            process.kill()


def test_injections_are_stage_scoped_and_do_not_pollute_other_queries(
    tmp_path: Path,
) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)

    code, route_error, _ = query(
        data_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--query-id",
        "route-injected",
        "--injections",
        str(ROOT / "data" / "injections" / "route-conflict.json"),
    )
    assert code == 4
    assert route_error["code"] == "E_RULE_VERSION_CONFLICT"
    assert route_error["details"]["stage"] == "route"
    assert route_error["details"]["template_id"] == "ledger_by_user"
    assert route_error["details"]["rule_version"] == "rule-v1"

    code, rewrite_error, _ = query(
        data_dir,
        "event_by_id",
        {"event_id": 650},
        "--query-id",
        "rewrite-injected",
        "--injections",
        str(ROOT / "data" / "injections" / "rewrite-out-of-range.json"),
    )
    assert code == 5
    assert rewrite_error["code"] == "E_SHARD_KEY_OUT_OF_RANGE"
    assert rewrite_error["details"]["stage"] == "rewrite"

    code, partial, raw = query(
        data_dir,
        "ledger_range",
        {"low": 2, "high": 9},
        "--query-id",
        "execute-injected",
        "--injections",
        str(ROOT / "data" / "injections" / "execute-fail-and-abandon.json"),
    )
    assert code == 0, raw
    assert partial["outcome"] == "partial"
    assert partial["aggregate"] == {"count": 4, "sum": 3000}
    shard_status = {
        shard["shard_no"]: shard["status"] for shard in partial["shard_outcomes"]
    }
    assert shard_status[2] == "failed"
    assert shard_status[3] == "abandoned"
    assert partial["failures"][0]["shard_no"] == 2
    assert (
        partial["failures"][0]["error"]["raw_error"]["error_type"]
        == "physical_table_missing"
    )
    assert partial["abandoned"][0]["shard_no"] == 3
    assert partial["merge_order"]["shards"] == [0, 1]

    code, merge_error, _ = query(
        data_dir,
        "ledger_in",
        {"u0": 0, "u1": 5, "u2": 10, "u3": 3},
        "--query-id",
        "merge-injected",
        "--injections",
        str(ROOT / "data" / "injections" / "merge-digest-mismatch.json"),
    )
    assert code == 5
    assert merge_error["code"] == "E_SHARD_RESULT_DIGEST_MISMATCH"
    assert merge_error["details"]["stage"] == "merge"
    assert merge_error["details"]["shard_no"] == 1
    assert merge_error["details"]["trace"]["execution_stage"] == "execute"
    assert merge_error["details"]["trace"]["shard_no"] == 1

    code, unaffected, raw = query(
        data_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--query-id",
        "unaffected",
    )
    assert code == 0, raw
    assert unaffected["outcome"] == "success"
    assert unaffected["aggregate"] == {"count": 1, "sum": 600}


def test_all_shards_failed_is_overall_failure(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    manifest = tmp_path / "all-failed.json"
    manifest.write_text(
        json.dumps(
            {
                "injections": [
                    {
                        "id": "all-failed",
                        "stage": "execute",
                        "template_id": "ledger_by_user",
                        "shard_no": 1,
                        "error_type": "physical_table_missing",
                        "code": "E_PHYSICAL_TABLE_MISSING",
                        "action": "fail_shard",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    code, output, _ = query(
        data_dir,
        "ledger_by_user",
        {"user_id": 5},
        "--query-id",
        "all-failed",
        "--injections",
        str(manifest),
    )
    assert code == 5
    assert output is not None
    assert output["code"] == "E_EXECUTION_FAILED"
    assert output["details"]["failed_shards"] == [1]
    record = status(data_dir, "--query-id", "all-failed")["queries"][0]
    assert record["outcome"] == "failed"
    assert record["commit"] is None


def test_crash_during_rewrite_and_execution_resumes_to_identical_result(
    tmp_path: Path,
) -> None:
    baseline_dir = provision(tmp_path, "baseline")
    load(baseline_dir)
    _, baseline_exec, _ = query(
        baseline_dir, "ledger_all", {}, "--query-id", "recover-exec"
    )
    _, baseline_rewrite, _ = query(
        baseline_dir, "ledger_all", {}, "--query-id", "recover-rewrite"
    )

    data_dir = provision(tmp_path, "crash")
    load(data_dir)
    code, output, _ = query(
        data_dir,
        "ledger_all",
        {},
        "--query-id",
        "recover-exec",
        "--injections",
        str(ROOT / "data" / "injections" / "crash-execute-shard-2.json"),
    )
    assert code == 137
    assert output is None
    interrupted = status(data_dir, "--query-id", "recover-exec")
    interrupted_query = interrupted["queries"][0]
    assert interrupted_query["status"] == "running"
    assert interrupted_query["resume_watermark"] == 2
    assert [shard["attempt"] for shard in interrupted_query["shards"]] == [1, 1, 0, 0]

    resume_code, resumed, raw = run_cli(
        data_dir,
        "resume",
        "--query-id",
        "recover-exec",
        "--no-injection",
    )
    assert resume_code == 0, raw
    assert resumed["results"][0] == baseline_exec
    recovered_state = status(data_dir, "--query-id", "recover-exec")
    assert [shard["attempt"] for shard in recovered_state["queries"][0]["shards"]] == [
        1,
        1,
        1,
        1,
    ]

    rewrite_code, output, _ = query(
        data_dir,
        "ledger_all",
        {},
        "--query-id",
        "recover-rewrite",
        "--injections",
        str(ROOT / "data" / "injections" / "crash-rewrite.json"),
    )
    assert rewrite_code == 137
    assert output is None
    rewrite_state = status(data_dir, "--query-id", "recover-rewrite")
    assert rewrite_state["queries"][0]["status"] == "routing"
    assert rewrite_state["queries"][0]["shards"] == []
    assert rewrite_state["queries"][0]["resume_watermark"] == 0

    resume_code, resumed_rewrite, raw = run_cli(
        data_dir,
        "resume",
        "--query-id",
        "recover-rewrite",
        "--no-injection",
    )
    assert resume_code == 0, raw
    assert resumed_rewrite["results"][0] == baseline_rewrite


def test_concurrent_duplicate_query_commits_once(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    command = [
        sys.executable,
        "-m",
        "shard_router",
        "query",
        "--template",
        "ledger_in",
        "--params",
        json.dumps({"u0": 0, "u1": 5, "u2": 10, "u3": 3}),
        "--query-id",
        "concurrent-q",
        "--data-dir",
        str(data_dir),
        "--compact",
    ]
    first = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    second = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    assert json.loads(first_out) == json.loads(second_out)
    state = status(data_dir, "--query-id", "concurrent-q")
    record = state["queries"][0]
    assert record["commit"] is not None
    assert all(shard["attempt"] == 1 for shard in record["shards"])


def test_concurrent_rule_apply_advances_pointer_once(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    command = [
        sys.executable,
        "-m",
        "shard_router",
        "apply",
        "--now-ms",
        "2000000",
        "--data-dir",
        str(data_dir),
        "--compact",
    ]
    first = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    second = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    actions = sorted(
        [json.loads(first_out)["result"]["action"], json.loads(second_out)["result"]["action"]]
    )
    assert actions == ["applied", "idempotent"]
    state = status(data_dir)
    assert state["current_rule"]["version"] == "rule-v2"
    assert state["current_rule"]["pointer_seq"] == 2


def test_verify_checks_both_snapshots_and_committed_digests(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    load(data_dir)
    code, _, raw = query(
        data_dir,
        "event_range",
        {"low": 250, "high": 650},
        "--query-id",
        "verified-q",
    )
    assert code == 0, raw
    code, report, raw = run_cli(data_dir, "verify")
    assert code == 0, raw
    assert report is not None and report["ok"] is True
    names = {check["name"] for check in report["checks"]}
    assert "v1_v2_shared_version_routes_match" in names
    assert "committed_results_are_recomputable" in names


def test_verify_without_state_fails_with_nonzero_exit(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    code, report, raw = run_cli(data_dir, "verify")
    assert code == 5, raw
    assert report is not None
    assert report["ok"] is False
    assert report["code"] == "E_VERIFY_FAILED"


def test_query_without_load_does_not_create_state(tmp_path: Path) -> None:
    data_dir = provision(tmp_path)
    code, output, _ = query(
        data_dir,
        "ledger_all",
        {},
        "--query-id",
        "not-loaded",
    )
    assert code == 3
    assert output is not None and output["code"] == "E_STATE_NOT_LOADED"
    assert not (data_dir / "state").exists()


def test_status_is_identical_across_repeated_independent_samples(tmp_path: Path) -> None:
    first_dir = provision(tmp_path, "first")
    second_dir = provision(tmp_path, "second")
    load(first_dir)
    load(second_dir)
    for data_dir in (first_dir, second_dir):
        code, _, raw = query(
            data_dir,
            "ledger_in",
            {"u0": 0, "u1": 5, "u2": 10, "u3": 3},
            "--query-id",
            "stable-q",
        )
        assert code == 0, raw
    first_json = json.dumps(status(first_dir), sort_keys=True)
    second_json = json.dumps(status(second_dir), sort_keys=True)
    assert first_json == second_json
