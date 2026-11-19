from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowguard import Adjudicator
from flowguard.cli import main
from flowguard.errors import ErrorCode, FlowGuardError
from flowguard.models import parse_sample
from flowguard.verifier import Verifier

from .conftest import (
    event,
    events_doc,
    fixed_sample,
    make_sample,
    simple_policy,
    simple_topology,
    write_json,
)


def test_explain_is_stable_sorted_json(tmp_path: Path) -> None:
    db = tmp_path / "explain.db"
    adjudicator = Adjudicator(db)
    adjudicator.load_sample(fixed_sample(), reset=True)
    adjudicator.run()
    first = tmp_path / "explain-1.json"
    second = tmp_path / "explain-2.json"
    assert main(["explain", "--db", str(db), "--seq", "14", "--output", str(first)]) == 0
    assert main(["explain", "--db", str(db), "--seq", "14", "--output", str(second)]) == 0
    first_text = first.read_text(encoding="utf-8")
    assert first_text == second.read_text(encoding="utf-8")
    payload = json.loads(first_text)
    assert list(payload.keys()) == sorted(payload.keys())
    decision = payload["decision"]
    assert decision["stats_mode"] == "layered"
    assert decision["window_evidence"]["error_upper_bound"] > 0
    assert [item["stage"] for item in decision["chain"]] == [
        "logical_clock",
        "token_bucket",
        "breaker_gate",
        "degrade",
        "window_statistics",
        "final",
    ]


def test_sample_digest_mismatch_is_diagnostic(tmp_path: Path) -> None:
    db = tmp_path / "digest.db"
    adjudicator = Adjudicator(db)
    sample = fixed_sample()
    adjudicator.load_sample(sample, reset=True)
    adjudicator.run()
    verifier = Verifier(db)
    wrong = {
        "topology": sample.topology_digest,
        "events": "0" * 64,
        "policy": sample.policy_digest,
    }
    with pytest.raises(FlowGuardError) as exc_info:
        verifier.verify(expected_digests=wrong)
    assert exc_info.value.code == ErrorCode.SAMPLE_DIGEST_MISMATCH
    assert exc_info.value.fields["actual"]["events"] == sample.events_digest


def test_lenient_ingestion_isolates_bad_events_by_service(tmp_path: Path) -> None:
    topology = simple_topology("svc-a", "svc-b")
    policy = simple_policy()
    seed = parse_sample(
        topology,
        events_doc([event(1, service="svc-a")]),
        policy,
    )
    db = tmp_path / "lenient.db"
    adjudicator = Adjudicator(db)
    adjudicator.load_sample(seed, reset=True)

    new_events = events_doc(
        [
            event(2, service="svc-a", clock_ms=10, result="not-a-marker"),
            event(3, service="svc-a", clock_ms=11),
            event(4, service="svc-b", clock_ms=12, latency_ms=-2),
            event(5, service="svc-b", clock_ms=13),
        ]
    )
    events_path = write_json(tmp_path / "extra-events.json", new_events)
    output_path = tmp_path / "run.json"
    exit_code = main(
        [
            "run",
            "--db",
            str(db),
            "--events",
            str(events_path),
            "--lenient-events",
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 3
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    codes = [item["error"]["code"] for item in payload["ingest_errors"]]
    assert codes == [
        ErrorCode.EVENT_RESULT_INVALID,
        ErrorCode.EVENT_LATENCY_INVALID,
    ]
    counts = adjudicator.store.counts()
    assert counts["events"] == {"total": 3, "processed": 3, "pending": 0}
    assert adjudicator.explain(5)["decision"]["service"] == "svc-b"
    assert adjudicator.explain(3)["decision"]["final"]["decision"] == "allow"


def test_resume_detects_pending_staged_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "manual-crash.db"
    adjudicator = Adjudicator(db)
    adjudicator.load_sample(fixed_sample(), reset=True)

    def simulated_exit(code: int):
        raise SystemExit(code)

    monkeypatch.setattr("flowguard.engine.os._exit", simulated_exit)
    with pytest.raises(SystemExit) as exc_info:
        adjudicator.run(crash_after_stage=5)
    assert exc_info.value.code == 137
    status = adjudicator.status()
    assert status["counts"]["events"]["processed"] == 4
    assert status["counts"]["events"]["pending"] == 24
    resumed = adjudicator.run()
    assert resumed["ok"] is True
    assert len(resumed["decision_digest"]) == 64
    assert Verifier(db).verify(replay=True)["ok"] is True
