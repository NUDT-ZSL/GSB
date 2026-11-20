from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable


def decisions_by_trace(
    result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {decision["trace_id"]: decision for decision in result["decisions"]}


def test_tail_sampling_outcomes_quota_and_policy_binding(
    loaded_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    sampled = run_cli(
        ["sample", "--db", str(loaded_db), "--advance-to", "3500"]
    )
    assert sampled.returncode == 0, sampled.stderr
    payload = json.loads(sampled.stdout)
    assert payload["processed_events"] == 15
    decisions = decisions_by_trace(payload)
    assert decisions["trace-checkout-001"]["outcome"] == "KEEP"
    assert decisions["trace-order-002"]["outcome"] == "KEEP"
    assert decisions["trace-slow-003"]["outcome"] == "DROP"
    assert decisions["trace-slow-003"]["reason_code"] == "QUOTA_EXHAUSTED"
    assert decisions["trace-batch-004"]["outcome"] == "KEEP"
    assert decisions["trace-policy-006"]["outcome"] == "KEEP"
    assert decisions["trace-policy-006"]["policy_version"] == "policy-v2"
    assert decisions["trace-orphan-005"]["close_reason"] == "WINDOW_EXPIRED"
    assert decisions["trace-orphan-005"]["outcome"] == "KEEP"
    assert decisions["trace-fast-000"]["outcome"] == "DROP"
    assert (
        decisions["trace-fast-000"]["reason_code"]
        == "SAMPLING_RATE_NOT_SELECTED"
    )
    assert decisions["trace-fast-000"]["sampling_rate"] == 0.05
    assert decisions["trace-checkout-001"]["evidence"]["quota"]["used_after"] == 1
    assert decisions["trace-slow-003"]["sampling_rate_estimate"][
        "effective_rate"
    ] == 0.666667
    status = json.loads(run_cli(["status", "--db", str(loaded_db)]).stdout)
    assert status["waiting_pool"]["traces"] == 0
    assert status["decisions"] == {"total": 7, "kept": 5, "dropped": 2}
    assert status["late_spans"]["count"] == 0
    verified = run_cli(["verify", "--db", str(loaded_db)])
    assert verified.returncode == 0, verified.stdout
    assert json.loads(verified.stdout)["ok"] is True


def test_same_input_is_bit_identical_across_databases(
    tmp_path: Path,
    run_cli: Callable[..., Any],
) -> None:
    root = Path(__file__).resolve().parents[1]
    samples = root / "samples"
    outputs = []
    for name in ("determinism-a.db", "determinism-b.db"):
        db = tmp_path / name
        loaded = run_cli(
            [
                "load",
                "--db",
                str(db),
                "--topology",
                str(samples / "topology.json"),
                "--policies",
                str(samples / "policies.json"),
                "--events",
                str(samples / "events.json"),
                "--window-ms",
                "900",
                "--quota-per-second",
                "2",
                "--clock-tolerance-ms",
                "10",
                "--reset",
            ]
        )
        assert loaded.returncode == 0, loaded.stderr
        sampled = run_cli(["sample", "--db", str(db), "--advance-to", "3500"])
        assert sampled.returncode == 0, sampled.stderr
        query = run_cli(
            ["query", "--db", str(db), "--trace-id", "trace-policy-006"]
        )
        status = run_cli(["status", "--db", str(db)])
        outputs.append((sampled.stdout, query.stdout, status.stdout))
    assert outputs[0] == outputs[1]


def test_clock_rollback_beyond_tolerance_is_rejected(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    failed = run_cli(
        ["sample", "--db", str(decided_db), "--advance-to", "0"]
    )
    assert failed.returncode == 18
    assert json.loads(failed.stderr)["error"]["code"] == "E_CLOCK_ROLLBACK"

