from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from . import EXIT_FATAL, EXIT_TERMINATED
from .atomicio import atomic_write_json
from .config import (
    DEFAULT_MEMORY_BUDGET,
    load_job_documents,
    load_source_manifest,
    validate_job,
)
from .engine import (
    SourceLibrary,
    TerminationToken,
    apply_anomaly,
    prepare_digest_corruption,
    run_batch,
)
from .errors import BusinessError
from .imaging import render_halftone, require_consistent
from .seed import generate_samples
from .state import StateStore


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError("E_CONFIG", f"cannot read JSON: {path}") from exc


def _install_signal_handlers(token: TerminationToken) -> None:
    def handler(signum: int, _frame: Any) -> None:
        token.request(f"signal:{signum}")

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signum = getattr(signal, signame, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def _attach_injection_results(
    report: Dict[str, Any],
    anomalies: Sequence[Mapping[str, Any]],
) -> None:
    outcomes = {
        outcome.get("job_id"): outcome
        for outcome in report.get("jobs", [])
        if isinstance(outcome, dict)
    }
    results: List[Dict[str, Any]] = []
    expected_job_ids = set()
    matched_count = 0
    for anomaly in anomalies:
        job_id = anomaly.get("job_id")
        expected_code = anomaly.get("expected_code")
        outcome = outcomes.get(job_id, {})
        actual_code = outcome.get("code") if not outcome.get("ok") else None
        matched = actual_code == expected_code
        matched_count += int(matched)
        expected_job_ids.add(job_id)
        results.append(
            {
                "id": anomaly.get("id"),
                "type": anomaly.get("type"),
                "job_id": job_id,
                "expected_code": expected_code,
                "actual_code": actual_code,
                "matched": matched,
            }
        )
    report["injection_results"] = results
    report["injection_summary"] = {
        "total": len(results),
        "matched": matched_count,
        "all_matched": matched_count == len(results),
    }
    report["expected_failed_job_ids"] = sorted(
        job_id for job_id in expected_job_ids if isinstance(job_id, str)
    )


def cmd_seed(args: argparse.Namespace) -> int:
    result = generate_samples(args.root, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        raw = _load_json(Path(args.job))
        job = validate_job(raw)
        print(
            json.dumps(
                {"ok": True, "job": job.canonical_dict()},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_FATAL


def cmd_consistency_demo(_args: argparse.Namespace) -> int:
    raw_job = {
        "job_id": "consistency_demo",
        "source_id": "synthetic",
        "output_width": 32,
        "output_height": 32,
        "dpi": 240,
        "lpi": 20,
        "angle_deg": 15,
        "dot_shape": "circle",
        "ink_coverage_max": 1.0,
        "levels": 2,
        "tile_cols": 2,
        "tile_rows": 2,
    }
    job = validate_job(raw_job)
    x_axis = np.linspace(0.0, 255.0, 32)[None, :]
    y_axis = np.linspace(0.0, 180.0, 32)[:, None]
    gray = np.clip(x_axis + y_axis, 0, 255).astype(np.uint8)
    reference = render_halftone(gray, job)
    assembled = reference.copy().astype(np.int16)
    assembled[9, 13] = 0 if reference[9, 13] != 0 else 255
    try:
        require_consistent(assembled, reference, job)
    except BusinessError as exc:
        document = exc.to_dict()
        document["demonstration"] = True
        print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(json.dumps({"ok": True, "demonstration": True}, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    jobs_path = Path(args.jobs) if args.jobs else root / "samples" / "jobs.json"
    manifest_path = (
        Path(args.manifest) if args.manifest else root / "samples" / "images.json"
    )
    inject_path = (
        Path(args.inject) if args.inject else root / "samples" / "anomalies.json"
    )
    state_dir = Path(args.state) if args.state else root / "state"
    out_dir = Path(args.out) if args.out else root / "out"
    report_path = Path(args.report) if args.report else out_dir / "report.json"

    try:
        job_dicts = load_job_documents(jobs_path)
        manifest = load_source_manifest(manifest_path)
        sources = SourceLibrary(manifest, manifest_path.parent)
        store = StateStore(state_dir)

        anomalies: List[Mapping[str, Any]] = []
        if inject_path.exists() and not args.no_inject:
            injection_document = _load_json(inject_path)
            raw_anomalies = injection_document.get(
                "anomalies",
                injection_document if isinstance(injection_document, list) else [],
            )
            if not isinstance(raw_anomalies, list):
                raise BusinessError("E_INJECTION", "anomalies must be an array")
            anomalies = raw_anomalies

        jobs_by_id = {str(job.get("job_id")): job for job in job_dicts}
        for anomaly in anomalies:
            base_id = anomaly.get("base_job_id")
            base_job = jobs_by_id.get(str(base_id))
            if base_job is None:
                raise BusinessError(
                    "E_INJECTION",
                    "anomaly base_job_id is missing",
                    {"base_job_id": base_id},
                )
            if anomaly.get("type") == "corrupt_persisted_digest":
                prepare_digest_corruption(anomaly, base_job, sources, store)
            injected = apply_anomaly(dict(base_job), anomaly)
            job_dicts.append(injected)

        token = TerminationToken(stop_after_completed=args.term_after_tiles)
        _install_signal_handlers(token)
        report = run_batch(
            job_dicts,
            sources,
            state=store,
            out_dir=out_dir,
            workers=args.workers,
            memory_budget_bytes=args.memory_budget_bytes,
            token=token,
            tile_delay_ms=args.tile_delay_ms,
        )
        if anomalies:
            _attach_injection_results(report, anomalies)

        atomic_write_json(report_path, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "exit_code": report["exit_code"],
                    "report": str(report_path),
                    "successful_jobs": report["successful_jobs"],
                    "failed_jobs": report["failed_jobs"],
                    "injection_summary": report.get("injection_summary"),
                    "memory_peak_bytes": report["memory_peak_bytes"],
                    "memory_wait_count": report["memory_wait_count"],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )

        if report["exit_code"] == EXIT_TERMINATED:
            return EXIT_TERMINATED
        if args.check:
            expected_failed = set(report.get("expected_failed_job_ids", []))
            unexpected_failure = any(
                not job.get("ok") and job.get("job_id") not in expected_failed
                for job in report["jobs"]
            )
            injections_ok = report.get("injection_summary", {}).get(
                "all_matched", True
            )
            if unexpected_failure or not injections_ok:
                return 1
        return 0
    except BusinessError as exc:
        error_document = exc.to_dict()
        error_document["ok"] = False
        print(json.dumps(error_document, ensure_ascii=False, indent=2, sort_keys=True))
        try:
            atomic_write_json(
                report_path,
                {
                    "schema": "halftone-batch-report/v1",
                    "status": "fatal",
                    "exit_code": EXIT_FATAL,
                    **error_document,
                },
            )
        except OSError:
            pass
        return EXIT_FATAL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m halftone",
        description="Deterministic tiled halftone generator and verifier",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser(
        "seed", help="generate fixed-seed local samples"
    )
    seed_parser.add_argument("--root", default=".")
    seed_parser.add_argument("--force", action="store_true")
    seed_parser.set_defaults(func=cmd_seed)

    run_parser = subparsers.add_parser(
        "run", help="run jobs and write atomic report/images"
    )
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--jobs")
    run_parser.add_argument("--manifest")
    run_parser.add_argument("--inject")
    run_parser.add_argument("--no-inject", action="store_true")
    run_parser.add_argument("--state")
    run_parser.add_argument("--out")
    run_parser.add_argument("--report")
    run_parser.add_argument("--workers", type=int, default=2)
    run_parser.add_argument(
        "--memory-budget-bytes",
        type=int,
        default=DEFAULT_MEMORY_BUDGET,
    )
    run_parser.add_argument("--tile-delay-ms", type=int, default=0)
    run_parser.add_argument(
        "--term-after-tiles",
        type=int,
        default=None,
        help="test hook: request termination after N newly completed tiles",
    )
    run_parser.add_argument("--check", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    validate_parser = subparsers.add_parser(
        "validate", help="validate one job JSON object"
    )
    validate_parser.add_argument("--job", required=True)
    validate_parser.set_defaults(func=cmd_validate)

    consistency_parser = subparsers.add_parser(
        "consistency-demo",
        help="print an E_CONSIST document for a deliberately mismatched tile",
    )
    consistency_parser.set_defaults(func=cmd_consistency_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
