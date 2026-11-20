from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from tailsample.engine import TailSamplingEngine


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def convergence_view(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": {
            "outcome": trace["decision"]["outcome"],
            "reason_code": trace["decision"]["reason_code"],
            "policy_version": trace["decision"]["policy_version"],
            "quota_consumed": trace["decision"]["quota_consumed"],
        },
        "chain": trace["chain"],
        "late_convergence": trace["late_convergence"],
    }


def test_late_spans_converge_without_flipping_decision_or_quota(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    before = json.loads(
        run_cli(
            ["query", "--db", str(decided_db), "--trace-id", "trace-checkout-001"]
        ).stdout
    )
    applied = run_cli(
        [
            "sample",
            "--db",
            str(decided_db),
            "--advance-to",
            "4000",
            "--late-spans",
            str(SAMPLES / "late_spans.json"),
        ]
    )
    assert applied.returncode == 0, applied.stdout
    late_result = json.loads(applied.stdout)
    assert len(late_result["late_convergence"]) == 2
    after = json.loads(
        run_cli(
            ["query", "--db", str(decided_db), "--trace-id", "trace-checkout-001"]
        ).stdout
    )
    assert before["decision"]["outcome"] == "KEEP"
    assert after["decision"]["outcome"] == "KEEP"
    assert after["decision"]["quota_consumed"] is True
    assert after["late_convergence"]["late_span_count"] == 1
    assert after["late_convergence"]["digest_changed"] is True
    assert after["chain"]["digest"] != before["chain"]["digest"]
    status = json.loads(
        run_cli(["status", "--db", str(decided_db)]).stdout
    )
    assert status["quota"]["logical_second"] == 4
    assert status["late_spans"]["count"] == 2
    assert status["late_spans"]["affected_decided_traces"] == 2
    for _ in range(2):
        replay = run_cli(
            [
                "sample",
                "--db",
                str(decided_db),
                "--advance-to",
                "4000",
                "--late-spans",
                str(SAMPLES / "late_spans.json"),
            ]
        )
        assert replay.returncode == 0, replay.stdout
        assert json.loads(replay.stdout)["late_convergence"] == []
    assert json.loads(run_cli(["status", "--db", str(decided_db)]).stdout)[
        "late_spans"
    ]["count"] == 2


def test_late_span_arrival_order_has_identical_final_convergence(
    tmp_path: Path,
    run_cli: Callable[..., Any],
) -> None:
    late_events = json.loads((SAMPLES / "late_spans.json").read_text(encoding="utf-8"))[
        "events"
    ]
    views = []
    for index, order in enumerate(([0, 1], [1, 0])):
        db = tmp_path / f"late-order-{index}.db"
        loaded = run_cli(
            [
                "load",
                "--db",
                str(db),
                "--topology",
                str(SAMPLES / "topology.json"),
                "--policies",
                str(SAMPLES / "policies.json"),
                "--events",
                str(SAMPLES / "events.json"),
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
        with TailSamplingEngine(db) as engine:
            engine.sample(advance_to_ms=3500)
            for event_index in order:
                event = late_events[event_index]
                engine.sample(
                    advance_to_ms=4000,
                    late_events=[event],
                )
            trace_one = engine.query_trace("trace-checkout-001")
            trace_three = engine.query_trace("trace-slow-003")
        views.append(
            {
                "one": convergence_view(trace_one),
                "three": convergence_view(trace_three),
            }
        )
    assert views[0] == views[1]


def test_bad_late_span_is_isolated_and_does_not_pollute_committed_decision(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    valid_event = json.loads((SAMPLES / "late_spans.json").read_text(encoding="utf-8"))[
        "events"
    ][0]
    invalid_event = json.loads(
        (SAMPLES / "unknown_service.json").read_text(encoding="utf-8")
    )["events"][0]
    before = json.loads(
        run_cli(
            ["query", "--db", str(decided_db), "--trace-id", "trace-checkout-001"]
        ).stdout
    )
    with TailSamplingEngine(decided_db) as engine:
        result = engine.sample(
            advance_to_ms=5000,
            late_events=[valid_event, invalid_event],
        )
        assert result["errors"][0]["error"]["code"] == "E_UNKNOWN_SERVICE"
        assert len(result["late_convergence"]) == 1
        after = engine.query_trace("trace-checkout-001")
    assert before["decision"]["outcome"] == after["decision"]["outcome"]
    assert before["decision"]["reason_code"] == after["decision"]["reason_code"]
    assert before["decision"]["quota_consumed"] == after["decision"]["quota_consumed"]
    assert after["late_convergence"]["late_span_count"] == 1


def test_policy_rollback_is_rejected(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    failed = run_cli(
        [
            "policy",
            "activate",
            "--db",
            str(decided_db),
            "--version",
            "policy-v1",
            "--at-ms",
            "5000",
        ]
    )
    assert failed.returncode == 16
    assert json.loads(failed.stderr)["error"]["code"] == "E_POLICY_ROLLBACK"


def test_bound_policy_activation_is_immutable(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    failed = run_cli(
        [
            "policy",
            "activate",
            "--db",
            str(decided_db),
            "--version",
            "policy-v2",
            "--at-ms",
            "4000",
        ]
    )
    assert failed.returncode == 16
    details = json.loads(failed.stderr)["error"]["details"]
    assert details["bound_decisions"] == 3


def test_policy_list_tracks_close_time_version(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    payload = json.loads(
        run_cli(["policy", "list", "--db", str(decided_db)]).stdout
    )
    assert payload["active_version"] == "policy-v2"
    assert [policy["revision"] for policy in payload["policies"]] == [1, 2]

