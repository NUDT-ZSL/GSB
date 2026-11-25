"""Command line entry points for deterministic local QC runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .codes import BusinessCode
from .detect import replay_report
from .dsp import estimate_envelope_serial, read_pcm_s16
from .errors import QCError
from .hashing import json_default, sha256_bytes
from .pipeline import PipelineRequest, run_pipeline
from .resources import ResourceBudget
from .samples import DEFAULT_SEED, write_sample_bundle
from .thresholds import read_thresholds
from .verify import verify_migration_gates, verify_serial_vs_oneshot


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=json_default))


def load_sample_inputs(sample_dir: str | Path, threshold_name: str = "v1") -> tuple[Any, str, dict[str, Any]]:
    sample_dir = Path(sample_dir)
    pcm, raw = read_pcm_s16(sample_dir / "sample_16k_mono_s16.pcm")
    threshold_path = sample_dir / f"thresholds_{threshold_name}.json"
    threshold_doc = read_thresholds(threshold_path)
    return pcm, sha256_bytes(raw), threshold_doc


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    report = result["report"]
    trace = result["trace"]
    replacement = report.get("replacement_plan", {})
    affected = replacement.get("affected_chunks", [])
    return {
        "code": result["code"],
        "budget_code": result.get("budget_code"),
        "recovered": result.get("recovered", False),
        "report_path": result.get("report_path"),
        "snapshot_revision": result.get("snapshot_revision"),
        "generation": report.get("generation"),
        "block_count": report.get("block_count"),
        "event_count": report.get("event_count"),
        "report_checksum": report.get("checksum"),
        "envelope_hash": report.get("envelope_hash"),
        "drift_hash": report.get("drift_hash"),
        "envelope_snapshot_loaded": trace["envelope_loaded_snapshot"],
        "alignment_snapshot_loaded": trace["alignment_loaded_snapshot"],
        "drift_snapshot_loaded": trace["drift_loaded_snapshot"],
        "report_snapshot_loaded": trace["report_loaded_snapshot"],
        "envelope_recomputed_count": len(trace["envelope_recomputed_chunks"]),
        "envelope_reused_cache_count": len(trace["envelope_reused_cache_chunks"]),
        "drift_recomputed_count": len(trace["drift_recomputed_chunks"]),
        "drift_reused_cache_count": len(trace["drift_reused_cache_chunks"]),
        "replacement_mode": replacement.get("mode"),
        "changed_fields": replacement.get("changed_fields"),
        "affected_chunk_count": len(affected),
        "affected_chunks_preview": affected[:20],
        "recompute_sample_range": replacement.get("recompute_sample_range"),
        "budget_plan": report.get("budget_plan"),
        "cache_stats": result.get("cache_stats"),
        "self_check": report.get("self_check"),
        "migration": report.get("migration"),
    }


def cmd_generate(args: argparse.Namespace) -> int:
    paths = write_sample_bundle(args.out, args.seed)
    print_json({"code": BusinessCode.OK, "paths": {key: str(value) for key, value in paths.items()}})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pcm, pcm_hash, doc = load_sample_inputs(args.sample_dir, args.threshold)
    budget = ResourceBudget(
        max_resident_blocks=args.max_resident_blocks,
        max_resident_bytes=args.max_memory_bytes,
        max_cache_entries=args.max_cache_entries,
        max_parallel_chunks=args.max_parallel_chunks,
        requested_workers=args.workers,
    )
    request = PipelineRequest(
        pcm=pcm,
        pcm_hash=pcm_hash,
        threshold_doc=doc,
        state_dir=args.state_dir,
        report_path=args.report,
        workers=args.workers,
        budget=budget,
        crash_after_chunks=args.crash_after,
        crash_after_stage=args.crash_after_stage,
        run_self_check=args.self_check,
    )
    result = run_pipeline(request)
    print_json(_summary(result))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    output: dict[str, Any] = {"ok": True, "checks": {}}
    for version in ("v1", "v2"):
        pcm, _, doc = load_sample_inputs(args.sample_dir, version)
        check = verify_serial_vs_oneshot(pcm, doc)
        blocks = estimate_envelope_serial(pcm, doc["frame_length"], doc["frame_hop"])
        migration = verify_migration_gates(blocks, doc)
        output["checks"][version] = {"self_check": check, "migration_gate_equivalence": {
            "ok": migration["ok"],
            "code": migration["code"],
            "compared_windows": migration["equivalence"]["compared_windows"],
            "first_mismatch": migration["equivalence"]["first_mismatch"],
        }}
        output["ok"] = output["ok"] and check["ok"] and migration["ok"]

    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        replay = replay_report(report)
        output["replay"] = {"ok": True, "replayed_events": replay["replayed_events"]}

    output["code"] = BusinessCode.VERIFY_OK if output["ok"] else BusinessCode.E_INCONSISTENT
    print_json(output)
    return 0 if output["ok"] else 2


def cmd_replay(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    result = replay_report(report)
    print_json(
        {
            "code": BusinessCode.VERIFY_OK,
            "replayed_events": result["replayed_events"],
            "first_results": result["results"][:10],
        }
    )
    return 0


def cmd_acceptance(args: argparse.Namespace) -> int:
    from .acceptance import run_acceptance

    summary = run_acceptance(Path(args.workdir), seed=args.seed)
    print_json(summary)
    return 0 if summary.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audio-qc", description="Deterministic audio QC first component")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic PCM and threshold samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.set_defaults(handler=cmd_generate)

    run = subparsers.add_parser("run", help="run four-stage pipeline")
    run.add_argument("--sample-dir", default="samples")
    run.add_argument("--threshold", choices=("v1", "v2"), default="v1")
    run.add_argument("--state-dir", default=".audio-qc-state")
    run.add_argument("--report", default=None)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--max-resident-blocks", type=int, default=2_048)
    run.add_argument("--max-memory-bytes", type=int, default=128 * 1024 * 1024)
    run.add_argument("--max-cache-entries", type=int, default=20_000)
    run.add_argument("--max-parallel-chunks", type=int, default=4)
    run.add_argument("--crash-after", type=int, default=None, help="hard exit after N newly computed envelope chunks")
    run.add_argument(
        "--crash-after-stage",
        choices=("envelope", "alignment", "drift", "report"),
        default=None,
        help="hard exit after the selected stage snapshot commits",
    )
    run.add_argument("--self-check", action="store_true")
    run.set_defaults(handler=cmd_run)

    verify = subparsers.add_parser("verify", help="run serial/oneshot and migration consistency checks")
    verify.add_argument("--sample-dir", default="samples")
    verify.add_argument("--report", default=None)
    verify.set_defaults(handler=cmd_verify)

    replay = subparsers.add_parser("replay", help="replay drift events from a report")
    replay.add_argument("--report", required=True)
    replay.set_defaults(handler=cmd_replay)

    acceptance = subparsers.add_parser("acceptance", help="run local crash/recovery acceptance and write summary")
    acceptance.add_argument("--workdir", default="artifacts/acceptance")
    acceptance.add_argument("--seed", type=int, default=DEFAULT_SEED)
    acceptance.set_defaults(handler=cmd_acceptance)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except QCError as error:
        print_json({"code": error.code, "message": error.message, "details": error.details})
        return 2
    except Exception as error:
        print_json({"code": "E_INTERNAL", "message": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
