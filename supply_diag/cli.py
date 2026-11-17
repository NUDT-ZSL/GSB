"""Command-line interface for generation, diagnostics, and environment checks."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Iterable

from .checkpoint import (
    checkpoint_identity,
    load_checkpoint,
    make_checkpoint_entry,
    save_checkpoint,
)
from .diagnostics import DiagnosticEngine
from .errors import E_DUPLICATE_PROJECT, E_IO, Issue
from .generator import DEFAULT_SEED, generate_samples
from .manifest import ParsedManifest, parse_manifest_file
from .report import atomic_write_json, build_graph_data, build_report


EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="supply-diag",
        description="Offline deterministic dependency supply-chain integrity workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="write a deterministic local manifest sample set"
    )
    generate.add_argument(
        "--output", default="samples", help="sample root directory (default: samples)"
    )
    generate.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"deterministic nonce seed (default: {DEFAULT_SEED})",
    )

    diagnose = subparsers.add_parser(
        "diagnose", help="diagnose a local batch of JSON manifests"
    )
    diagnose.add_argument(
        "inputs",
        nargs="*",
        help="manifest files/directories; defaults to samples/manifests",
    )
    diagnose.add_argument(
        "--report",
        default="artifacts/diagnostic_report.json",
        help="atomic JSON report destination",
    )
    diagnose.add_argument(
        "--graph",
        default="artifacts/dependency_graph.json",
        help="atomic dependency graph destination",
    )
    diagnose.add_argument(
        "--checkpoint",
        default=None,
        help="optional atomic incremental checkpoint path",
    )
    diagnose.add_argument(
        "--rebuild",
        action="store_true",
        help="ignore existing checkpoint entries and rebuild everything",
    )
    diagnose.add_argument(
        "--compact",
        action="store_true",
        help="write compact JSON instead of indented JSON",
    )

    doctor = subparsers.add_parser(
        "doctor", help="check Python, numpy, and pytest availability"
    )
    doctor.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _resolve_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _collect_inputs(
    raw_inputs: Iterable[str],
    *,
    exclusions: set[Path],
) -> tuple[list[Path], list[str]]:
    discovered: list[Path] = []
    errors: list[str] = []
    for raw in raw_inputs:
        path = _resolve_path(raw)
        if path.is_file():
            if path not in exclusions:
                discovered.append(path)
            continue
        if path.is_dir():
            for candidate in sorted(path.rglob("*.json")):
                resolved = candidate.resolve()
                if resolved in exclusions:
                    continue
                if candidate.name == "generator_metadata.json":
                    continue
                discovered.append(resolved)
            continue
        errors.append(f"input path does not exist or is not a file/directory: {raw}")
    unique = sorted(set(discovered), key=lambda item: str(item))
    return unique, errors


def _mark_duplicate_projects(manifests: list[ParsedManifest]) -> set[str]:
    counts: dict[str, int] = {}
    for manifest in manifests:
        if manifest.project_id is not None:
            counts[manifest.project_id] = counts.get(manifest.project_id, 0) + 1
    duplicates = {project_id for project_id, count in counts.items() if count > 1}
    for manifest in manifests:
        if manifest.project_id in duplicates:
            manifest.errors.append(
                Issue(
                    E_DUPLICATE_PROJECT,
                    "project_id occurs more than once in the diagnostic batch",
                    field="project_id",
                    details={"project_id": manifest.project_id},
                )
            )
    return duplicates


def _checkpoint_info(
    *,
    enabled: bool,
    rebuild: bool,
    state,
    reused: int,
) -> dict:
    if not enabled:
        return {"mode": "disabled", "reused_manifest_count": reused}
    if rebuild:
        return {
            "mode": "full_rebuild",
            "reason": "rebuild_requested",
            "reused_manifest_count": reused,
        }
    if not state.compatible:
        return {
            "mode": "full_rebuild",
            "reason": state.reason,
            "reused_manifest_count": reused,
        }
    if state.present:
        return {
            "mode": "incremental",
            "reason": "complete_checkpoint_loaded",
            "reused_manifest_count": reused,
        }
    return {
        "mode": "initial",
        "reason": "checkpoint_not_found",
        "reused_manifest_count": reused,
    }


def _run_diagnose(args: argparse.Namespace) -> int:
    raw_inputs = list(args.inputs)
    if not raw_inputs:
        default_input = Path("samples/manifests")
        if not default_input.exists():
            print(
                "error: no inputs supplied and samples/manifests does not exist",
                file=sys.stderr,
            )
            return EXIT_USAGE
        raw_inputs = [str(default_input)]

    report_path = _resolve_path(args.report)
    graph_path = _resolve_path(args.graph)
    checkpoint_path = _resolve_path(args.checkpoint) if args.checkpoint else None
    if report_path == graph_path:
        print("error: --report and --graph must be different files", file=sys.stderr)
        return EXIT_USAGE
    if checkpoint_path is not None and checkpoint_path in {report_path, graph_path}:
        print(
            "error: --checkpoint must not overwrite the report or graph file",
            file=sys.stderr,
        )
        return EXIT_USAGE
    exclusions = {report_path, graph_path}
    if checkpoint_path is not None:
        exclusions.add(checkpoint_path)
    paths, input_errors = _collect_inputs(raw_inputs, exclusions=exclusions)
    if input_errors:
        for message in input_errors:
            print(f"error: {message}", file=sys.stderr)
        return EXIT_USAGE
    if not paths:
        print("error: no JSON manifests found", file=sys.stderr)
        return EXIT_USAGE

    manifests = [parse_manifest_file(path) for path in paths]
    duplicate_projects = _mark_duplicate_projects(manifests)

    state = None
    reuse_enabled = False
    if checkpoint_path is not None:
        state = load_checkpoint(checkpoint_path)
        reuse_enabled = state.compatible and not args.rebuild
    else:
        state = None

    engine = DiagnosticEngine()
    cached_entries: dict[str, dict] = (
        dict(state.entries)
        if state is not None and state.compatible and not args.rebuild
        else {}
    )
    current_report_results: dict[str, dict] = {}
    standalone_results: list[dict] = []
    current_cache_results: dict[str, dict | None] = {}
    reused_count = 0

    for manifest in manifests:
        identity = checkpoint_identity(manifest)
        duplicate = manifest.project_id in duplicate_projects
        cached = (
            cached_entries.get(identity)
            if identity is not None and reuse_enabled and not duplicate
            else None
        )
        if (
            cached is not None
            and cached.get("content_digest") == manifest.content_digest
            and cached.get("result", {}).get("status") in {"success", "partial"}
        ):
            result = cached["result"]
            reused_count += 1
        else:
            result = engine.diagnose(manifest).to_dict()

        if identity is not None and not duplicate:
            current_report_results[identity] = result
            current_cache_results[identity] = (
                result if result.get("status") != "failed" else None
            )
        else:
            standalone_key = (
                f"standalone:{manifest.content_digest}:{manifest.source_path}"
            )
            standalone_results.append(result)
            if identity is not None:
                current_report_results.pop(identity, None)
                current_cache_results[identity] = None

    combined_results = []
    for key in sorted(cached_entries):
        if key in current_report_results or key in current_cache_results:
            continue
        result = cached_entries[key].get("result")
        if isinstance(result, dict):
            combined_results.append(result)
    combined_results.extend(current_report_results.values())
    combined_results.extend(standalone_results)

    checkpoint_info = _checkpoint_info(
        enabled=checkpoint_path is not None,
        rebuild=bool(args.rebuild),
        state=state,
        reused=reused_count,
    )
    report = build_report(
        combined_results,
        checkpoint=checkpoint_info,
        reused_manifest_count=reused_count,
    )
    graph = build_graph_data(combined_results, run_id=report["run_id"])

    try:
        atomic_write_json(report_path, report, pretty=not args.compact)
        atomic_write_json(graph_path, graph, pretty=not args.compact)
    except OSError as exc:
        print(f"error: cannot write report/graph atomically: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if checkpoint_path is not None:
        next_entries: dict[str, dict] = {}
        if state is not None and state.compatible and not args.rebuild:
            next_entries.update(cached_entries)
        for key, cached in current_cache_results.items():
            if cached is None:
                next_entries.pop(key, None)
        for manifest in manifests:
            identity = checkpoint_identity(manifest)
            if identity is None or identity not in current_cache_results:
                continue
            result = current_cache_results[identity]
            if result is None:
                continue
            entry = make_checkpoint_entry(manifest, result)
            if entry is not None:
                next_entries[identity] = entry
        try:
            save_checkpoint(
                checkpoint_path,
                entries=next_entries.values(),
                sequence=(state.sequence + 1 if state is not None else 1),
            )
        except OSError as exc:
            print(f"error: cannot write checkpoint atomically: {exc}", file=sys.stderr)
            return EXIT_USAGE
    print(
        json.dumps(
            {
                "report": str(report_path),
                "graph": str(graph_path),
                "checkpoint": str(checkpoint_path) if checkpoint_path else None,
                "conclusion": report["conclusion"],
                "summary": report["summary"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return EXIT_OK if report["exit_code"] == 0 else EXIT_PARTIAL


def _run_generate(args: argparse.Namespace) -> int:
    try:
        metadata = generate_samples(args.output, seed=args.seed)
    except OSError as exc:
        print(f"error: cannot generate samples: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(metadata, sort_keys=True, ensure_ascii=False))
    return EXIT_OK


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _run_doctor(args: argparse.Namespace) -> int:
    python_ok = sys.version_info >= (3, 10)
    numpy_ok = _module_available("numpy")
    pytest_ok = _module_available("pytest")
    result = {
        "python": {
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "executable": sys.executable,
            "ok": python_ok,
        },
        "numpy": {"available": numpy_ok},
        "pytest": {"available": pytest_ok},
        "offline_ready": python_ok and numpy_ok and pytest_ok,
    }
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print(
            "Python {version}: {python_status}; numpy: {numpy_status}; "
            "pytest: {pytest_status}".format(
                version=result["python"]["version"],
                python_status="OK" if python_ok else "MISSING/OLD",
                numpy_status="OK" if numpy_ok else "MISSING",
                pytest_status="OK" if pytest_ok else "MISSING",
            )
        )
    return EXIT_OK if result["offline_ready"] else EXIT_PARTIAL


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return _run_generate(args)
    if args.command == "diagnose":
        return _run_diagnose(args)
    if args.command == "doctor":
        return _run_doctor(args)
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())

