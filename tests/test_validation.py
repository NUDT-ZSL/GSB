from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from conftest import make_snapshot_context, write_json
from trustman.engine import validate_dataset
from trustman.models import (
    CERT_SCHEMA,
    SNAPSHOT_SCHEMA,
    canonical_json,
)
from trustman.parser import parse_certificate_files, parse_chain_files
from trustman.samples import OBSERVED_AT_MS
from trustman.scheduler import SchedulerConfig


def _report_for(
    samples: Path,
    snapshot_files: list[Path],
    *,
    workers: int,
    queue_limit: int = 1_000_000,
    max_tasks: int = 1_000_000,
    step_hook=None,
):
    cert_parse = parse_certificate_files([samples / "certificates.json"])
    chain_parse = parse_chain_files([samples / "chains.json"])
    snapshot_context = make_snapshot_context(snapshot_files)
    output = validate_dataset(
        certificate_parse=cert_parse,
        chain_parse=chain_parse,
        snapshot_context=snapshot_context,
        source_rejections=snapshot_context.rejections,
        config=SchedulerConfig(
            workers=workers,
            max_tasks=max_tasks,
            queue_limit=queue_limit,
        ),
        step_hook=step_hook,
    )
    return output.report


def test_sample_conclusions_and_concurrency_invariance(
    samples: Path, snapshot_files: list[Path]
):
    serial = _report_for(samples, snapshot_files, workers=1)
    parallel = _report_for(
        samples, list(reversed(snapshot_files)), workers=8
    )
    assert canonical_json(serial) == canonical_json(parallel)
    assert serial["effective_snapshot"]["version"] == 3
    assert serial["summary"]["tasks_total"] == 9
    assert serial["summary"]["succeeded"] == 9
    assert serial["summary"]["by_conclusion"] == {
        "trusted": 1,
        "partially_trusted": 4,
        "untrusted": 4,
    }
    assert all(serial["reconciliation"].values())
    assert [chain["chain_id"] for chain in serial["chains"]] == sorted(
        chain["chain_id"] for chain in serial["chains"]
    )
    assert [item["version"] for item in serial["snapshots"]] == [1, 2, 3]

    chains = {item["chain_id"]: item for item in serial["chains"]}
    expected_codes = {
        "chain-ok": [],
        "chain-broken": ["E_CHAIN_BROKEN"],
        "chain-empty": ["E_CHAIN_EMPTY"],
        "chain-expired": ["E_EXPIRED_LEAF"],
        "chain-int-revoked": ["E_REVOKED_INTERMEDIATE"],
        "chain-late": ["E_REVOKED_LEAF"],
        "chain-missing-intermediate": ["E_MISSING_INTERMEDIATE"],
        "chain-revoked-leaf": ["E_REVOKED_LEAF"],
        "chain-root-untrusted": ["E_ROOT_UNTRUSTED"],
    }
    for chain_id, codes in expected_codes.items():
        assert [error["code"] for error in chains[chain_id]["errors"]] == codes
        assert chains[chain_id]["basis"]["snapshot_version"] == 3
        assert chains[chain_id]["basis"]["policy_version"] == "1.2.0"
        assert chains[chain_id]["matches_expected"] is True

    positions = {
        "chain-empty": 0,
        "chain-broken": 0,
        "chain-missing-intermediate": 1,
        "chain-root-untrusted": 2,
    }
    for chain_id, position in positions.items():
        assert chains[chain_id]["errors"][0]["position"] == position

    revocations = {
        item["cert_id"]: item for item in serial["revocation_propagation"]
    }
    assert revocations["int-revoked"]["affected_chain_ids"] == [
        "chain-int-revoked"
    ]
    assert revocations["int-revoked"]["occurrences"][0]["kind"] == "intermediate"
    assert revocations["leaf-revoked"]["occurrences"][0]["kind"] == "leaf"
    assert revocations["leaf-late"]["affected_chain_ids"] == ["chain-late"]
    assert serial["expiration_propagation"][0]["cert_id"] == "leaf-expired"
    assert serial["expiration_propagation"][0]["affected_chain_ids"] == [
        "chain-expired"
    ]


def test_late_revocation_and_observed_time_are_recomputed(
    samples: Path, snapshot_files: list[Path]
):
    v1 = _report_for(samples, [snapshot_files[0]], workers=2)
    v1_chains = {item["chain_id"]: item for item in v1["chains"]}
    assert v1["effective_snapshot"]["version"] == 1
    assert v1_chains["chain-late"]["conclusion"] == "trusted"
    assert v1_chains["chain-int-revoked"]["conclusion"] == "trusted"
    assert v1_chains["chain-root-untrusted"]["conclusion"] == "trusted"

    v3 = _report_for(samples, [snapshot_files[2], snapshot_files[0]], workers=2)
    v3_chains = {item["chain_id"]: item for item in v3["chains"]}
    assert v3["effective_snapshot"]["version"] == 3
    assert v3_chains["chain-late"]["conclusion"] == "partially_trusted"
    assert v3_chains["chain-late"]["invalidations"][0]["code"] == "E_REVOKED_LEAF"
    assert v3_chains["chain-int-revoked"]["conclusion"] == "partially_trusted"
    assert v3_chains["chain-root-untrusted"]["conclusion"] == "untrusted"

    certificates = {
        "id": "leaf-time",
        "issuer_id": "root-time",
        "public_key_fingerprint": "sha256:" + "1" * 64,
        "valid_from_ms": OBSERVED_AT_MS - 10_000,
        "valid_to_ms": OBSERVED_AT_MS + 10_000,
        "revoked": False,
    }
    root = {
        "id": "root-time",
        "issuer_id": "root-time",
        "public_key_fingerprint": "sha256:" + "2" * 64,
        "valid_from_ms": OBSERVED_AT_MS - 10_000,
        "valid_to_ms": OBSERVED_AT_MS + 10_000,
        "revoked": False,
    }
    cert_path = write_json(
        samples / "time-certificates.json",
        {"schema": CERT_SCHEMA, "certificates": [root, certificates]},
    )
    chain_path = write_json(
        samples / "time-chains.json",
        {
            "schema": "trustman.chains/v1",
            "chains": [
                {
                    "chain_id": "time-chain",
                    "certificate_ids": ["leaf-time", "root-time"],
                    "deadline_ms": 1_000,
                    "expected_summary": "trusted:snapshot=1:policy=1.0.0",
                }
            ],
        },
    )
    before_expiry = write_json(
        samples / "time-before.json",
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root-time"],
                    "revoked_entries": [],
                    "observed_at_ms": OBSERVED_AT_MS - 1,
                }
            ],
        },
    )
    after_expiry = write_json(
        samples / "time-after.json",
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 2,
                    "policy_version": "1.0.1",
                    "trusted_roots": ["root-time"],
                    "revoked_entries": [],
                    "observed_at_ms": OBSERVED_AT_MS + 10_001,
                }
            ],
        },
    )
    cert_parse = parse_certificate_files([cert_path])
    chain_parse = parse_chain_files([chain_path])
    context = make_snapshot_context([before_expiry])
    before = validate_dataset(
        certificate_parse=cert_parse,
        chain_parse=chain_parse,
        snapshot_context=context,
        source_rejections=context.rejections,
        config=SchedulerConfig(workers=1),
    ).report
    context_after = make_snapshot_context([after_expiry])
    after = validate_dataset(
        certificate_parse=cert_parse,
        chain_parse=chain_parse,
        snapshot_context=context_after,
        source_rejections=context_after.rejections,
        config=SchedulerConfig(workers=1),
    ).report
    assert before["chains"][0]["conclusion"] == "trusted"
    assert after["chains"][0]["conclusion"] == "partially_trusted"
    assert after["chains"][0]["errors"][0]["code"] == "E_EXPIRED_LEAF"
    assert after["chains"][0]["basis"]["observed_at_ms"] == OBSERVED_AT_MS + 10_001


def test_timeout_does_not_stop_other_tasks(samples: Path, snapshot_files: list[Path]):
    cert_parse = parse_certificate_files([samples / "certificates.json"])
    original_chain_parse = parse_chain_files([samples / "chains.json"])
    chain_parse = replace(
        original_chain_parse,
        items=[
            replace(chain, deadline_ms=5)
            for chain in original_chain_parse.items
        ],
    )
    context = make_snapshot_context([snapshot_files[2]])

    def step_hook(chain_id: str, position: int, cert_id: str) -> None:
        if chain_id == "chain-ok" and position == 0:
            import time

            time.sleep(0.03)

    output = validate_dataset(
        certificate_parse=cert_parse,
        chain_parse=chain_parse,
        snapshot_context=context,
        source_rejections=context.rejections,
        config=SchedulerConfig(workers=3),
        step_hook=step_hook,
    )
    chains = {item["chain_id"]: item for item in output.report["chains"]}
    assert chains["chain-ok"]["status"] == "timeout"
    assert chains["chain-ok"]["errors"][0]["code"] == "E_TIMEOUT"
    assert chains["chain-ok"]["basis"]["snapshot_version"] == 3
    assert output.report["summary"]["timed_out"] == 1
    assert output.report["summary"]["succeeded"] == 8
    assert all(
        item["status"] == "success"
        for chain_id, item in chains.items()
        if chain_id != "chain-ok"
    )
    assert all(output.report["reconciliation"].values())


def test_budget_and_queue_admission_are_concurrency_invariant(
    samples: Path, snapshot_files: list[Path]
):
    serial = _report_for(
        samples,
        snapshot_files,
        workers=1,
        queue_limit=4,
        max_tasks=2,
    )
    parallel = _report_for(
        samples,
        snapshot_files,
        workers=8,
        queue_limit=4,
        max_tasks=2,
    )
    assert canonical_json(serial) == canonical_json(parallel)
    assert serial["summary"]["tasks_total"] == 9
    assert serial["summary"]["succeeded"] == 2
    assert serial["summary"]["over_budget"] == 2
    assert serial["summary"]["rejected"] == 5
    assert serial["summary"]["queue_rejected"] == 5
    assert serial["summary"]["task_rejections"] == 5
    assert serial["reconciliation"]["task_counts_match"] is True
    assert serial["reconciliation"]["rejection_counts_match"] is True

    chains = {item["chain_id"]: item for item in serial["chains"]}
    ordered = sorted(chains)
    assert chains[ordered[0]]["status"] == "success"
    assert chains[ordered[1]]["status"] == "success"
    assert chains[ordered[2]]["status"] == "budget"
    assert chains[ordered[2]]["errors"][0]["code"] == "E_BUDGET"
    assert chains[ordered[3]]["status"] == "budget"
    for chain_id in ordered[4:]:
        assert chains[chain_id]["status"] == "rejected"
        assert chains[chain_id]["errors"][0]["code"] == "E_QUEUE_FULL"


def test_revoked_intermediate_lists_all_dependent_chains(tmp_path: Path):
    def cert(cert_id: str, issuer_id: str, seed: str):
        return {
            "id": cert_id,
            "issuer_id": issuer_id,
            "public_key_fingerprint": "sha256:" + seed * 64,
            "valid_from_ms": 100,
            "valid_to_ms": 300,
            "revoked": False,
        }

    cert_path = write_json(
        tmp_path / "certificates.json",
        {
            "schema": CERT_SCHEMA,
            "certificates": [
                cert("root", "root", "a"),
                cert("intermediate", "root", "b"),
                cert("leaf-one", "intermediate", "c"),
                cert("leaf-two", "intermediate", "d"),
            ],
        },
    )
    snapshot_path = write_json(
        tmp_path / "snapshots.json",
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root"],
                    "revoked_entries": [
                        {
                            "cert_id": "intermediate",
                            "revoked_at_ms": 190,
                            "reason": "compromise",
                        }
                    ],
                    "observed_at_ms": 200,
                }
            ],
        },
    )
    chain_path = write_json(
        tmp_path / "chains.json",
        {
            "schema": "trustman.chains/v1",
            "chains": [
                {
                    "chain_id": "chain-one",
                    "certificate_ids": ["leaf-one", "intermediate", "root"],
                    "deadline_ms": 1_000,
                    "expected_summary": "partially_trusted:snapshot=1:policy=1.0.0",
                },
                {
                    "chain_id": "chain-two",
                    "certificate_ids": ["leaf-two", "intermediate", "root"],
                    "deadline_ms": 1_000,
                    "expected_summary": "partially_trusted:snapshot=1:policy=1.0.0",
                },
            ],
        },
    )
    cert_parse = parse_certificate_files([cert_path])
    chain_parse = parse_chain_files([chain_path])
    context = make_snapshot_context([snapshot_path])
    report = validate_dataset(
        certificate_parse=cert_parse,
        chain_parse=chain_parse,
        snapshot_context=context,
        source_rejections=context.rejections,
        config=SchedulerConfig(workers=2),
    ).report
    assert report["summary"]["by_conclusion"]["partially_trusted"] == 2
    event = report["revocation_propagation"][0]
    assert event["cert_id"] == "intermediate"
    assert event["affected_chain_ids"] == ["chain-one", "chain-two"]
    assert {item["kind"] for item in event["occurrences"]} == {"intermediate"}
    for chain in report["chains"]:
        assert chain["invalidations"][0]["code"] == "E_REVOKED_INTERMEDIATE"
