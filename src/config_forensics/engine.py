"""High-level deterministic offline adjudication engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .adjudication import Adjudication, adjudicate
from .baseline import BaselineResult, parse_baseline_file
from .checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointDecision,
    CheckpointError,
    CheckpointManager,
    hmac_keys_fingerprint,
)
from .codes import Code
from .digests import DEFAULT_ALGORITHMS, normalize_algorithm
from .importer import FileTask, ImportOutcome, import_files
from .issues import Issue, issue_sort_key
from .models import FileResult
from .parser import ParseContext, process_evidence_file
from .storage import LockConflict, atomic_write_json


@dataclass
class EngineConfig:
    workers: int = 4
    queue_limit: int = 16
    max_file_seconds: float = 10.0
    allowed_algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    hmac_keys: dict[str, str | bytes] = field(default_factory=dict)


@dataclass
class RunResult:
    report: dict[str, Any]
    graph: dict[str, Any]
    output_errors: list[Issue] = field(default_factory=list)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _collect_sources(
    evidence_paths: Path | str | Iterable[Path | str],
) -> list[tuple[str, str, Path]]:
    if isinstance(evidence_paths, (str, Path)):
        root = Path(evidence_paths)
        if root.is_dir():
            files = sorted(
                (path for path in root.rglob("*.json") if path.is_file()),
                key=lambda path: path.relative_to(root).as_posix(),
            )
            return [
                (path.relative_to(root).as_posix(), str(path), path)
                for path in files
            ]
        paths = [root]
    else:
        paths = [Path(item) for item in evidence_paths]

    normalized: list[tuple[str, str, Path]] = []
    seen_names: dict[str, int] = {}
    for path in paths:
        name = path.name
        seen_names[name] = seen_names.get(name, 0) + 1
        normalized.append((name, str(path), path))
    for index, (name, display, path) in enumerate(normalized):
        if seen_names[name] > 1:
            source_id = path.resolve().as_posix()
            normalized[index] = (source_id, display, path)
    normalized.sort(key=lambda item: item[0])
    return normalized


def canonical_verdict(report: dict[str, Any]) -> dict[str, Any]:
    """Stable conclusion projection used to compare serial and parallel runs."""
    return {
        "evidence": report["evidence"],
        "chains": report["chains"],
        "drift_points": report["drift_points"],
        "event_clusters": report["event_clusters"],
        "chain_breaks": report["chain_breaks"],
        "tampered": report["tampered"],
        "conflicts": report["conflicts"],
        "errors": report["errors"],
        "summary": report["summary"],
    }


class ForensicEngine:
    def __init__(self, config: EngineConfig | None = None):
        self.config = config or EngineConfig()

    def _normalized_algorithms(self) -> tuple[str, ...]:
        values = {normalize_algorithm(item) for item in self.config.allowed_algorithms}
        values.discard(None)
        if not values:
            raise ValueError("allowed_algorithms must contain sha256 or hmac-sha256")
        return tuple(sorted(values))

    def run(
        self,
        evidence_paths: Path | str | Iterable[Path | str],
        baseline_path: Path | str,
        output_path: Path | str,
        graph_path: Path | str,
        *,
        checkpoint_path: Path | str | None = None,
        crash_hook: Callable[[str], None] | None = None,
        processor: Callable[[FileTask], FileResult] | None = None,
    ) -> RunResult:
        algorithms = self._normalized_algorithms()
        sources = _collect_sources(evidence_paths)
        source_hashes = {source_id: _file_sha256(path) for source_id, _, path in sources}
        source_index = {
            source_id: index for index, (source_id, _, _) in enumerate(sources, start=1)
        }

        baseline: BaselineResult = parse_baseline_file(Path(baseline_path))
        hmac_fingerprint = hmac_keys_fingerprint(self.config.hmac_keys)
        checkpoint_manager = CheckpointManager(checkpoint_path)
        decision = checkpoint_manager.load(
            algorithm_whitelist=algorithms,
            baseline_sha256=baseline.sha256,
            hmac_fingerprint=hmac_fingerprint,
            source_hashes=source_hashes,
        )
        context_issues: list[Issue] = list(decision.issues)
        completed: dict[str, FileResult] = {}
        expected_generation = decision.generation
        checkpoint_disabled = False

        if decision.action == "resume":
            completed = dict(decision.completed)
            for source_id, file_result in completed.items():
                file_result.file_index = source_index[source_id]
                for record in file_result.records:
                    record.file_index = source_index[source_id]
        elif decision.action == "unavailable":
            checkpoint_manager = CheckpointManager(None)
            checkpoint_disabled = True
            expected_generation = 0

        tasks: list[FileTask] = []
        for source_id, source_name, path in sources:
            if source_id in completed:
                continue
            tasks.append(
                FileTask(
                    source_id=source_id,
                    source_name=source_name,
                    path=path,
                    file_index=source_index[source_id],
                    sha256=source_hashes.get(source_id, ""),
                )
            )

        parse_context = ParseContext(
            allowed_algorithms=algorithms,
            hmac_keys=dict(self.config.hmac_keys),
        )
        if processor is None:
            def processor(task: FileTask) -> FileResult:
                return process_evidence_file(
                    task.path,
                    source_id=task.source_id,
                    source_name=task.source_name,
                    file_index=task.file_index,
                    context=parse_context,
                )

        run_results: dict[str, FileResult] = dict(completed)

        def persist_result(result: FileResult) -> None:
            nonlocal expected_generation, checkpoint_disabled
            run_results[result.source_id] = result
            if result.status in ("timed_out", "queue_rejected"):
                return
            completed[result.source_id] = result
            if checkpoint_disabled or checkpoint_manager.path is None:
                return
            try:
                expected_generation = checkpoint_manager.save(
                    completed=completed,
                    expected_generation=expected_generation,
                    algorithm_whitelist=algorithms,
                    baseline_sha256=baseline.sha256,
                    hmac_fingerprint=hmac_fingerprint,
                    run_complete=False,
                )
            except CheckpointError as exc:
                context_issues.append(
                    Issue(
                        exc.code,
                        file_id=str(checkpoint_path),
                        details={"reason": exc.message},
                    )
                )
                checkpoint_disabled = True
                return
            if crash_hook is not None:
                crash_hook(result.source_id)

        outcome: ImportOutcome = import_files(
            tasks,
            workers=self.config.workers,
            queue_limit=self.config.queue_limit,
            max_file_seconds=self.config.max_file_seconds,
            processor=processor,
            on_result=persist_result,
        )

        if not checkpoint_disabled and checkpoint_manager.path is not None:
            run_complete = outcome.timed_out_count == 0 and outcome.queue_rejected_count == 0
            try:
                expected_generation = checkpoint_manager.save(
                    completed=completed,
                    expected_generation=expected_generation,
                    algorithm_whitelist=algorithms,
                    baseline_sha256=baseline.sha256,
                    hmac_fingerprint=hmac_fingerprint,
                    run_complete=run_complete,
                )
            except CheckpointError as exc:
                context_issues.append(
                    Issue(
                        exc.code,
                        file_id=str(checkpoint_path),
                        details={"reason": exc.message},
                    )
                )

        all_file_results = sorted(run_results.values(), key=lambda item: item.source_id)
        adjudication: Adjudication = adjudicate(
            file_results=all_file_results,
            baseline_rules=baseline.rules,
            baseline_issues=baseline.issues,
            context_issues=context_issues,
        )
        report = adjudication.report
        graph = adjudication.graph
        report["budget"] = {
            "workers": self.config.workers,
            "queue_limit": self.config.queue_limit,
            "max_file_seconds": self.config.max_file_seconds,
            "admitted_files": outcome.admitted_count,
            "queue_rejected_files": outcome.queue_rejected_count,
            "timed_out_files": outcome.timed_out_count,
        }
        report["checkpoint"] = {
            "action": decision.action,
            "reason": decision.reason,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "resumed_file_count": len(decision.completed)
            if decision.action == "resume"
            else 0,
            "processed_file_count": len(tasks),
            "generation": expected_generation,
            "disabled_due_to_lock": decision.action == "unavailable",
        }
        report["schema_version"] = 1
        report["application"] = "config-forensics"
        report["errors"].sort(key=issue_sort_key)

        output_errors: list[Issue] = []
        try:
            atomic_write_json(Path(graph_path), graph, conflict_code=Code.OUTPUT_LOCK)
        except LockConflict as exc:
            output_errors.append(
                Issue(
                    Code.OUTPUT_LOCK,
                    file_id=str(graph_path),
                    details={"lock_path": str(exc.path)},
                )
            )
        except OSError as exc:
            output_errors.append(
                Issue(
                    Code.OUTPUT_WRITE,
                    file_id=str(graph_path),
                    details={"reason": str(exc)},
                )
            )
        if output_errors:
            report["errors"].extend(issue.to_dict() for issue in output_errors)
            report["errors"].sort(key=issue_sort_key)
            report["summary"]["error_code_counts"] = {
                code: sum(1 for item in report["errors"] if item["code"] == code)
                for code in sorted({item["code"] for item in report["errors"]})
            }
        try:
            atomic_write_json(Path(output_path), report, conflict_code=Code.OUTPUT_LOCK)
        except LockConflict as exc:
            output_errors.append(
                Issue(
                    Code.OUTPUT_LOCK,
                    file_id=str(output_path),
                    details={"lock_path": str(exc.path)},
                )
            )
        except OSError as exc:
            output_errors.append(
                Issue(
                    Code.OUTPUT_WRITE,
                    file_id=str(output_path),
                    details={"reason": str(exc)},
                )
            )
        return RunResult(report=report, graph=graph, output_errors=output_errors)
