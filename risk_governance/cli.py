"""Command line interface for the governance pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, read_json, sha256_json
from .pipeline import GovernancePipeline
from .snapshots import SnapshotManager


def _run(args: argparse.Namespace) -> int:
    pipeline = GovernancePipeline(
        workdir=args.workdir,
        samples_path=args.samples,
        config_path=args.config,
        events_path=args.events,
        now_ms=args.now_ms,
        interrupt_after_batch=args.interrupt_after_batch,
        strict=args.strict,
        seed=args.seed,
        batch_delay_ms=args.batch_delay_ms,
    )
    exit_code, report = pipeline.execute()
    output = {
        "exit_code": exit_code,
        "status": report.get("status"),
        "checksum": report.get("checksum"),
        "error_codes": report.get("error_codes"),
        "planned_recompute_set": report.get("planned_recompute_set"),
        "reused_set": report.get("reused_set"),
        "report_path": str(Path(args.workdir) / "reports" / "execution_report.json"),
        "interrupted_report_path": str(Path(args.workdir) / "reports" / "interrupted_report.json"),
    }
    if report.get("status") == "interrupted":
        output["recovery"] = report.get("recovery")
        output["report_path"] = str(Path(args.workdir) / "reports" / "interrupted_report.json")
    print(canonical_dumps(output) if args.quiet else _pretty(output))
    return exit_code


def _pretty(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _verify(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    report_path = workdir / "reports" / "execution_report.json"
    report = read_json(report_path)
    stored_checksum = report.pop("checksum", None)
    expected_checksum = sha256_json(report)
    report["checksum"] = stored_checksum
    snapshot_manager = SnapshotManager(workdir)
    result = {
        "report_checksum_valid": stored_checksum == expected_checksum,
        "stored_checksum": stored_checksum,
        "expected_checksum": expected_checksum,
        "reference_chain": report.get("cache_reference_chain", {}),
        "v2_snapshot": snapshot_manager.verify_current_v2(),
    }
    result["valid"] = bool(
        result["report_checksum_valid"]
        and result["reference_chain"].get("valid")
        and result["v2_snapshot"].get("valid")
    )
    print(_pretty(result))
    return 0 if result["valid"] else 1


def _replay(args: argparse.Namespace) -> int:
    source_report_path = Path(args.report)
    source_report = read_json(source_report_path)
    parameters = source_report.get("replay_parameters", {})
    source_workdir = source_report_path.parents[1]
    target_workdir = Path(args.target_workdir) if args.target_workdir else source_workdir.parent / f"{source_workdir.name}-replay"
    pipeline = GovernancePipeline(
        workdir=target_workdir,
        samples_path=parameters["samples"],
        config_path=parameters["config"],
        events_path=parameters.get("events"),
        now_ms=parameters.get("now_ms"),
        seed=parameters.get("seed"),
    )
    exit_code, replayed_report = pipeline.execute()
    source_fingerprints = [
        (item["batch_id"], item["fingerprint"], item["anomaly_count"])
        for item in source_report.get("batch_summaries", [])
    ]
    replayed_fingerprints = [
        (item["batch_id"], item["fingerprint"], item["anomaly_count"])
        for item in replayed_report.get("batch_summaries", [])
    ]
    stable_match = {
        "exit_code": exit_code,
        "fingerprints": source_fingerprints == replayed_fingerprints,
        "error_codes": source_report.get("error_codes") == replayed_report.get("error_codes"),
        "trace_checksum": source_report.get("checksums", {}).get("traces_sha256")
        == replayed_report.get("checksums", {}).get("traces_sha256"),
        "v1_content_checksum": source_report.get("checksums", {}).get("v1_content_checksum")
        == replayed_report.get("checksums", {}).get("v1_content_checksum"),
        "v2_content_checksum": source_report.get("checksums", {}).get("v2_content_checksum")
        == replayed_report.get("checksums", {}).get("v2_content_checksum"),
    }
    result = {
        "replay_workdir": str(target_workdir),
        "stable_match": stable_match,
        "replayed_checksum": replayed_report.get("checksum"),
        "match": stable_match["exit_code"] == 0
        and all(value is True for key, value in stable_match.items() if key != "exit_code"),
    }
    print(_pretty(result))
    return 0 if result["match"] else 1


def _rollback(args: argparse.Namespace) -> int:
    manager = SnapshotManager(args.workdir)
    result = manager.attempt_rollback(args.to_version)
    print(_pretty(result))
    return 0 if result.get("allowed") else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Risk-model diagnosis governance pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the pipeline")
    run.add_argument("--samples", default="data/generated/samples.jsonl")
    run.add_argument("--config", default="data/generated/calibration_config.json")
    run.add_argument("--events", default="data/generated/change_events.jsonl")
    run.add_argument("--workdir", default="out/pipeline")
    run.add_argument("--now-ms", type=int, default=None)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--interrupt-after-batch", type=int, default=None)
    run.add_argument("--batch-delay-ms", type=int, default=0)
    run.add_argument("--strict", action="store_true")
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(handler=_run)

    verify = subparsers.add_parser("verify", help="verify report, cache chain and v2 checksums")
    verify.add_argument("--workdir", default="out/pipeline")
    verify.set_defaults(handler=_verify)

    replay = subparsers.add_parser("replay", help="replay a report into a new directory and compare stable hashes")
    replay.add_argument("--report", required=True)
    replay.add_argument("--target-workdir", default=None)
    replay.set_defaults(handler=_replay)

    rollback = subparsers.add_parser("snapshot-rollback", help="attempt an old-version rollback")
    rollback.add_argument("--workdir", default="out/pipeline")
    rollback.add_argument("--to-version", type=int, required=True)
    rollback.set_defaults(handler=_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
