#!/usr/bin/env python3
"""Run the deterministic end-to-end demonstration twice and compare bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quota_ledger import LedgerError, QuotaLedger  # noqa: E402
from quota_ledger.utils import canonical_json, read_json  # noqa: E402
from quota_ledger.validation import (  # noqa: E402
    parse_event,
    parse_event_document,
)


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": result["event"]["seq"],
        "code": result["code"],
        "decision": result["decision"],
        "first_violation": result["first_violation"],
        "soft_transitions": result["soft_transitions"],
        "state_effect": result["state_effect"],
    }


def _run_cli_crash(
    db_path: Path,
    event: Any,
    stage: str,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "quota_ledger",
        "write",
        "--db",
        str(db_path),
        "--event-json",
        canonical_json(event.payload()),
        "--crash-stage",
        stage,
    ]
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_scenario(run_dir: Path) -> dict[str, Any]:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    db_path = run_dir / "ledger.db"

    quota_document = read_json(PROJECT_ROOT / "samples" / "quotas.json")
    limiter_document = read_json(PROJECT_ROOT / "samples" / "limiter.json")
    manifest = read_json(PROJECT_ROOT / "samples" / "clock_manifest.json")
    if manifest["system_wall_clock_allowed"] is not False:
        raise RuntimeError("business clock must come from the manifest")
    random.seed(int(manifest["random_seed"]))
    ledger = QuotaLedger.load(db_path, quota_document, limiter_document)
    raw_events = list(
        read_json(PROJECT_ROOT / "samples" / "events.json")["events"]
    )
    manifest_clocks = {
        int(step["seq"]): int(step["clock_ms"])
        for step in manifest["steps"]
    }
    event_sequences = {int(event["seq"]) for event in raw_events}
    if event_sequences != set(manifest_clocks):
        raise RuntimeError("clock manifest must cover exactly the event stream")
    injected_events = [
        dict(event, clock_ms=manifest_clocks[int(event["seq"])])
        for event in raw_events
    ]
    events = parse_event_document(injected_events, ledger.quota)
    first_results: dict[int, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []

    for event in events[:-1]:
        result = ledger.write(event)
        first_results[event.seq] = result
        decisions.append(_public_result(result))

    status_after_13 = ledger.status()

    replay_payload = read_json(PROJECT_ROOT / "samples" / "replay_event.json")
    replay_payload["clock_ms"] = int(manifest["idempotent_replay"]["clock_ms"])
    replay_event = parse_event(replay_payload, ledger.quota)
    replay_result = ledger.write(replay_event)
    replay = {
        "seq": replay_event.seq,
        "idempotency_key": replay_event.idempotency_key,
        "same_as_first_result": (
            canonical_json(replay_result)
            == canonical_json(first_results[replay_event.seq])
        ),
        "decision": replay_result["decision"],
        "code": replay_result["code"],
    }

    crash_event = events[-1]
    crash_process = _run_cli_crash(
        db_path,
        crash_event,
        manifest["forced_termination"]["stage"],
    )
    status_after_crash = ledger.status()
    resume_after_crash = ledger.resume()
    crash_result = ledger.write(crash_event)
    first_results[crash_event.seq] = crash_result
    decisions.append(_public_result(crash_result))

    orphan_event = parse_event(
        manifest["orphan_intent_probe"]["event"],
        ledger.quota,
    )
    orphan_process = _run_cli_crash(
        db_path,
        orphan_event,
        manifest["orphan_intent_probe"]["stage"],
    )
    status_with_orphan = ledger.status()
    resume_after_orphan = ledger.resume()

    rollback_probe_event = {
        "seq": 15,
        "tenant": "rate",
        "bucket": "media",
        "prefix": "p",
        "object_key": "rollback-probe",
        "bytes": 1,
        "deleted": False,
        "clock_ms": manifest["clock_rollback_probe"]["event_clock_ms"],
        "idempotency_key": "rollback-probe-15",
    }
    try:
        ledger.write(rollback_probe_event)
        rollback_probe: dict[str, Any] = {
            "ok": False,
            "unexpected": "write unexpectedly succeeded",
        }
    except LedgerError as exc:
        rollback_probe = {
            "ok": False,
            "code": exc.code,
            "details": exc.details,
        }

    final_status = ledger.status()
    verification = ledger.verify(events)

    return {
        "scenario": "hierarchical-quota-ledger-v1",
        "load": {
            "tenants": len(ledger.quota.tenants),
            "layers": len(ledger.quota.layers),
        },
        "decisions": decisions,
        "status_after_seq_13": {
            "last_decided_seq": status_after_13["last_decided_seq"],
            "counts": status_after_13["counts"],
            "demo_layers": [
                layer
                for layer in status_after_13["layers"]
                if layer["tenant"] == "demo"
            ],
            "rate_tokens": [
                token
                for token in status_after_13["tokens"]
                if token["tenant"] == "rate"
            ],
        },
        "idempotent_replay": replay,
        "forced_termination": {
            "stage": manifest["forced_termination"]["stage"],
            "exit_code": crash_process.returncode,
            "stdout": crash_process.stdout,
            "stderr": crash_process.stderr,
            "status_after_crash": {
                "last_decided_seq": status_after_crash["last_decided_seq"],
                "event_count": status_after_crash["counts"]["events"],
                "crash_tenant_used_bytes": _tenant_bytes(
                    status_after_crash,
                    "crash",
                ),
            },
            "resume": {
                "ok": resume_after_crash["ok"],
                "cleaned_count": resume_after_crash["cleaned_count"],
                "verify_ok": resume_after_crash["verify"]["ok"],
            },
            "retry_after_resume": {
                "code": crash_result["code"],
                "bytes_delta": crash_result["applied"]["bytes_delta"],
            },
        },
        "orphan_intent_recovery": {
            "stage": manifest["orphan_intent_probe"]["stage"],
            "exit_code": orphan_process.returncode,
            "stdout": orphan_process.stdout,
            "stderr": orphan_process.stderr,
            "status_before_resume": {
                "last_decided_seq": status_with_orphan["last_decided_seq"],
                "pending_operations": status_with_orphan["counts"][
                    "pending_operations"
                ],
            },
            "resume": {
                "ok": resume_after_orphan["ok"],
                "cleaned_count": resume_after_orphan["cleaned_count"],
                "verify_ok": resume_after_orphan["verify"]["ok"],
            },
        },
        "clock_rollback_probe": rollback_probe,
        "final_status": {
            "last_decided_seq": final_status["last_decided_seq"],
            "counts": final_status["counts"],
            "layers": final_status["layers"],
            "tokens": final_status["tokens"],
        },
        "verify": verification,
    }


def _tenant_bytes(status: dict[str, Any], tenant: str) -> int:
    for layer in status["layers"]:
        if layer["level"] == "tenant" and layer["tenant"] == tenant:
            return layer["used_bytes"]
    raise KeyError(tenant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="artifacts/demo",
        help="directory for run-1/run-2 outputs",
    )
    args = parser.parse_args(argv)
    output_dir = (PROJECT_ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    run_1 = run_scenario(output_dir / "run-1")
    run_2 = run_scenario(output_dir / "run-2")
    encoded_1 = canonical_json(run_1).encode("utf-8")
    encoded_2 = canonical_json(run_2).encode("utf-8")
    identical = encoded_1 == encoded_2

    (output_dir / "run-1.output.json").write_bytes(encoded_1 + b"\n")
    (output_dir / "run-2.output.json").write_bytes(encoded_2 + b"\n")
    comparison = {
        "ok": identical,
        "byte_identical": identical,
        "run_1_bytes": len(encoded_1) + 1,
        "run_2_bytes": len(encoded_2) + 1,
        "run_1_sha256": hashlib.sha256(encoded_1 + b"\n").hexdigest(),
        "run_2_sha256": hashlib.sha256(encoded_2 + b"\n").hexdigest(),
    }
    (output_dir / "comparison.json").write_text(
        canonical_json(comparison) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": identical,
        "comparison": comparison,
        "run_1_output": "run-1.output.json",
        "run_2_output": "run-2.output.json",
        "scenarios": [
            "hard_quota_reject",
            "soft_quota_enter_exit",
            "rate_limit_reject",
            "idempotent_replay",
            "delete_return",
            "forced_termination_recovery",
            "clock_rollback_guard",
        ],
    }
    sys.stdout.write(canonical_json(summary) + "\n")
    return 0 if identical else 1


if __name__ == "__main__":
    raise SystemExit(main())

